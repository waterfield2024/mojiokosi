"""CoreAudio capture adapter and a clocked, independently pausable PCM player."""
from collections import deque
import threading


def devices():
    import sounddevice as sd
    found = []
    for index, info in enumerate(sd.query_devices()):
        # Never silently record the microphone when system audio was requested.
        if info['max_input_channels'] and 'blackhole' in info['name'].lower():
            found.append(dict(index=index, name=info['name'], rate=int(info['default_samplerate']),
                              channels=min(2, info['max_input_channels']), default=not found))
    return found


class CaptureStream:
    def __init__(self, callback, rate, channels, device, frames):
        import sounddevice as sd
        def receive(data, count, timing, status):
            _, done = callback(bytes(data), count, timing, 2 if status.input_overflow else 0)
            if done:
                raise sd.CallbackStop
        self.stream = sd.RawInputStream(samplerate=rate, channels=channels, dtype='float32',
                                        device=device, blocksize=frames, callback=receive)

    def start_stream(self): self.stream.start()
    def stop_stream(self): self.stream.stop()
    def is_active(self): return self.stream.active
    def close(self): self.stream.close()


class PyAudio:
    """The capture engine's narrow callback contract, backed by CoreAudio."""
    def get_device_info_by_index(self, index):
        for item in devices():
            if item['index'] == index:
                return dict(item, isLoopbackDevice=True, defaultSampleRate=item['rate'], maxInputChannels=item['channels'])
        raise RuntimeError('BlackHole 2chが見つかりません。Macの音声設定と入力一覧を確認してください。')

    def open(self, *, format, channels, rate, input, input_device_index, frames_per_buffer, stream_callback, start):
        return CaptureStream(stream_callback, rate, channels, input_device_index, frames_per_buffer)

    def terminate(self): pass


paFloat32, paInputOverflow, paComplete, paContinue = 1, 2, 1, 0


class WaveOutput:
    RATE = 48000

    def __init__(self, device=None):
        import sounddevice as sd
        self.lock = threading.RLock()
        self.blocks = deque()
        self.submitted = self.played = 0
        self.paused = True
        self.closed = False
        self.clock = deque()
        def output(buffer, frames, timing, status):
            block = bytes(frames * 4)
            with self.lock:
                if not self.paused:
                    data = bytearray()
                    while self.blocks and len(data) < frames * 4:
                        size = min(len(self.blocks[0]), frames * 4 - len(data))
                        data.extend(self.blocks[0][:size])
                        del self.blocks[0][:size]
                        if not self.blocks[0]: self.blocks.popleft()
                    size = len(data)
                    block = bytes(data) + block[size:]
                    self.clock.append((float(timing.outputBufferDacTime), self.played, size // 4))
                    self.played += size // 4
            buffer[:] = block
        self.stream = sd.RawOutputStream(samplerate=self.RATE, channels=2, dtype='int16',
                                         device=device, blocksize=480, callback=output)
        self.stream.start()

    def write(self, pcm):
        with self.lock:
            self.blocks.append(bytearray(pcm))
            self.submitted += len(pcm) // 4

    def position(self):
        with self.lock:
            now = self.stream.time
            while len(self.clock) > 1 and self.clock[1][0] <= now:
                self.clock.popleft()
            if not self.clock:
                return 0.
            at, before, count = self.clock[0]
            return (before + min(count, max(0., now - at) * self.RATE)) / self.RATE

    def collect(self): pass
    @property
    def pending(self):
        with self.lock:
            # Callers count queued blocks and wait until hardware has played the tail.
            count = len(self.blocks) + int(self.position() * self.RATE + 1 < self.played)
            return range(count)
    def pause(self):
        with self.lock: self.paused = True
    def resume(self):
        with self.lock: self.paused = False
    def close(self):
        if not self.closed:
            self.closed = True
            self.stream.abort()
            self.stream.close()
