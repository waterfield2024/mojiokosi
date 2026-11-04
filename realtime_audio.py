"""WASAPI loopback capture. Only the explicitly selected output device is opened."""
from __future__ import annotations
import audioop
import queue
import threading
import time
import wave
import numpy as np
from speech_preferences import profile, read_control
from utterance_audio import UtteranceChunker, mode as segmentation_mode

RATE = 16000

def devices():
    import pyaudiowpatch as pa
    with pa.PyAudio() as audio:
        try:
            default = audio.get_default_wasapi_loopback()["index"]
        except (OSError, ValueError):
            default = None
        return [{"index": int(d["index"]), "name": str(d["name"]),
                 "rate": int(d["defaultSampleRate"]), "channels": int(d["maxInputChannels"]),
                 "default": int(d["index"]) == default}
                for d in audio.get_loopback_device_info_generator() if d["maxInputChannels"] > 0]

class Chunker:
    """Split on pauses, with short shared context across forced long-speech cuts."""
    def __init__(self, submit, seconds=2, sensitivity='balanced'):
        self.submit = submit
        self.limit = int(seconds * RATE)
        self.total = 0
        self.start = None
        self.last_voice = 0
        self.core = 0
        self.voiced = 0
        self.profile = profile(sensitivity)

    def feed(self, pcm):
        count = len(pcm) // 2
        a, b = self.total, self.total + count
        self.total = b
        voice = bool(pcm) and audioop.rms(pcm, 2) >= self.profile.capture_rms
        if voice:
            if self.start is None:
                self.start = max(self.core, a - int(.25 * RATE))
                self.voiced = 0
            self.last_voice = b
            self.voiced += count
        if self.start is None:
            return
        pause = b - self.last_voice >= int(.35 * RATE)
        full = b - max(self.start, self.core) >= self.limit
        if pause or full:
            if self.voiced >= int(self.profile.capture_seconds * RATE):
                self.submit(self.start, b, self.core)
                self.core = b
            if full and not pause:
                self.start = max(0, b - RATE)
                self.voiced = 0
            else:
                self.start = None
                self.voiced = 0

    def finish(self):
        if self.start is not None and self.voiced >= int(self.profile.final_seconds * RATE) and self.total > self.core:
            self.submit(self.start, self.total, self.core)
            self.core = self.total
        self.start = None

class LoopbackCapture:
    def __init__(self, device, output, chunks, stopped, seconds=2, meter=lambda **kw: None):
        self.device, self.output, self.chunks, self.stopped = device, output, chunks, stopped
        self.seconds, self.meter = seconds, meter
        self.profile = profile()
        self.speech_control = None
        self.segmentation = 'utterance'
        self.total = 0
        self.error = None
        self.epoch = None
        self.ready = threading.Event()
        self.done = threading.Event()
        self.halt = threading.Event()
        self.thread = threading.Thread(target=self.run, daemon=True)

    def start(self):
        self.thread.start()

    def stop(self):
        self.halt.set()

    def set_sensitivity(self, value, control_path=None):
        # One immutable profile is published atomically to the capture thread.
        self.profile = profile(value)
        if control_path is not None:
            self.speech_control = control_path

    def set_segmentation(self, value):
        self.segmentation = segmentation_mode(value)

    def update_chunker(self, chunker):
        # Apply live changes even while the ASR thread is busy with inference.
        if self.speech_control is not None:
            self.set_sensitivity(read_control(self.speech_control, self.profile.key))
        chunker.profile = self.profile
        if isinstance(chunker, UtteranceChunker) and chunker.activity:
            chunker.activity.configure(self.profile.key)

    def run(self):
        import pyaudiowpatch as pa
        raw = queue.Queue(maxsize=128)
        overflow = threading.Event()
        audio = stream = writer = chunker = activity = None
        last_meter = 0
        converter = None
        epoch = time.monotonic()
        stop_time = None
        try:
            if self.segmentation == 'utterance':
                from speech_gate import SpeechActivity
                from asr import ROOT
                activity = SpeechActivity(ROOT/'models', self.profile.key)
                activity.prepare(stopped=self.stopped)
            audio = pa.PyAudio()
            info = audio.get_device_info_by_index(int(self.device["index"]))
            if not info.get("isLoopbackDevice") or info["name"] != self.device["name"]:
                raise RuntimeError("再生デバイスが変わりました。音声出力の一覧を更新して選び直してください。")
            rate, channels = int(info["defaultSampleRate"]), int(info["maxInputChannels"])
            def callback(data, frame_count, time_info, status):
                if status & pa.paInputOverflow:
                    overflow.set()
                try:
                    raw.put_nowait((data, time.monotonic() - epoch - frame_count / rate))
                except queue.Full:
                    overflow.set()
                return (None, pa.paComplete if self.halt.is_set() else pa.paContinue)
            stream = audio.open(format=pa.paFloat32, channels=channels, rate=rate, input=True,
                                input_device_index=int(info["index"]), frames_per_buffer=max(256, rate // 10),
                                stream_callback=callback, start=False)
            writer = wave.open(str(self.output), "wb")
            writer.setnchannels(1); writer.setsampwidth(2); writer.setframerate(RATE)
            def submit(a, b, core):
                writer._file.flush()
                self.chunks.put((a, b, core))
            chunker = (UtteranceChunker(self.chunks.put, self.seconds, self.profile.key, activity)
                       if self.segmentation == 'utterance' else Chunker(submit, self.seconds, self.profile.key))
            epoch = time.monotonic()
            self.epoch = epoch
            stream.start_stream()
            self.ready.set()
            activity_warning = ''
            def write_pcm(pcm):
                nonlocal activity_warning
                writer.writeframes(pcm)
                writer._file.flush()
                self.update_chunker(chunker)
                chunker.feed(pcm)
                self.total += len(pcm) // 2
                if activity and activity.warning != activity_warning:
                    activity_warning = activity.warning
                    self.meter(seconds=self.total/RATE,level=0,segmentation_warning=activity_warning)

            def silence_until(seconds):
                missing = max(0, round(seconds * RATE) - self.total)
                while missing:
                    n = min(missing, RATE // 10)
                    write_pcm(b"\x00" * (n * 2))
                    missing -= n

            while True:
                should_stop = self.stopped() or self.halt.is_set()
                if should_stop and stop_time is None:
                    stop_time = time.monotonic() - epoch
                    if stream.is_active():
                        stream.stop_stream()
                if overflow.is_set():
                    raise RuntimeError("音声の取り込みが追いつかず停止しました。途中までの音声は保存されています。")
                try:
                    data, sample_time = raw.get(timeout=.1)
                except queue.Empty:
                    if should_stop:
                        silence_until(stop_time)
                        break
                    if not stream.is_active():
                        raise RuntimeError("音声出力との接続が切れました。再生デバイスを確認してください。")
                    # WASAPI supplies no packets while the rendering endpoint is idle.
                    # Leave a small jitter margin and explicitly preserve that silent timeline.
                    silence_until(max(0, time.monotonic() - epoch - .25))
                    self.meter(seconds=self.total / RATE, level=0)
                    continue
                if sample_time - self.total / RATE > .12:
                    silence_until(sample_time)
                    converter = None
                samples = np.frombuffer(data, dtype="<f4").reshape(-1, channels)
                mono = samples.mean(axis=1)
                pcm = (np.clip(mono, -1, 1) * 32767).astype("<i2").tobytes()
                pcm, converter = audioop.ratecv(pcm, 2, 1, rate, RATE, converter)
                # writeframes patches the WAV header each block, allowing recovery after force stop.
                write_pcm(pcm)
                now = time.monotonic()
                if now - last_meter >= .5:
                    self.meter(seconds=self.total / RATE, level=min(100, audioop.rms(pcm, 2) * 100 / 4000))
                    last_meter = now
        except Exception as exc:
            self.error = str(exc)
        finally:
            if chunker:
                # Finalize the captured tail even on a device disconnect.
                chunker.finish()
            if activity:activity.close()
            if stream:
                try:
                    stream.close()
                except Exception:
                    pass
            if audio:
                audio.terminate()
            if writer:
                writer.close()
            self.done.set()
            self.chunks.put(None)
