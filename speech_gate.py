"""Speech evidence for live ASR, without altering recorded samples or timestamps."""
import hashlib
import os
from pathlib import Path
import urllib.request
import uuid

import numpy as np

RATE = 16000
REVISION = '5cd2ba54db059f961e7545d4f211b44f005a6dfb'  # official Silero VAD v5.0
MODEL_URL = f'https://raw.githubusercontent.com/snakers4/silero-vad/{REVISION}/files/silero_vad.onnx'
MODEL_SHA256 = '6b99cbfd39246b6706f98ec13c7c50c6b299181f2474fa05cbc8046acc274396'
MODEL_SIZE = 2313101
MODEL_PATH = 'vad/silero-v5.onnx'


def ensure_vad(models, status=lambda text: None, stopped=lambda: False):
    target = Path(models) / MODEL_PATH
    def verified(path):
        return (path.is_file() and path.stat().st_size == MODEL_SIZE
                and hashlib.sha256(path.read_bytes()).hexdigest() == MODEL_SHA256)
    if stopped():
        raise InterruptedError('発話判定の準備を中止しました。')
    if verified(target):
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(target.name + '.download-' + uuid.uuid4().hex)
    status('無音の誤認識を防ぐ発話判定モデルを準備しています… 約2.3MB（初回のみ）')
    try:
        with urllib.request.urlopen(MODEL_URL, timeout=30) as source, temporary.open('wb') as output:
            while block := source.read(128*1024):
                if stopped():
                    raise InterruptedError('発話判定の準備を中止しました。')
                output.write(block)
        if not verified(temporary):
            raise RuntimeError('発話判定モデルの検証に失敗しました。')
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def audible(samples):
    """Ignore digital silence, DC offsets and rounding residue; retain quiet speech."""
    if len(samples) < 2:
        return False
    # 20 ms frames, excluding DC. Two active frames preserve short interjections.
    padded = np.pad(samples, (0, (-len(samples)) % 320))
    frames = padded.reshape(-1, 320)
    centered = frames - frames.mean(axis=1, keepdims=True)
    return np.count_nonzero(np.mean(centered*centered, axis=1) >= (8/32768)**2) >= 2


def overlaps_speech(intervals, start, end):
    """Coordinates are 16 kHz samples; do not reuse speech in an old overlap."""
    if end <= start:
        return False
    overlap = sum(max(0, min(end, b)-max(start, a)) for a, b in intervals)
    return overlap >= min(640, end-start)


def keep_spoken_rows(rows, intervals):
    # ASR timestamps can drift by a few frames. This small tolerance does not
    # authorize subtitles elsewhere in a long silent tail.
    return [row for row in rows if overlaps_speech(intervals,
            round(row['start']*RATE)-2400, round(row['end']*RATE)+2400)]


class SpeechGate:
    def __init__(self, models):
        self.models = Path(models)
        self.detector = None
        self.attempted = False
        self.warning = ''

    def prepare(self, status=lambda text: None, stopped=lambda: False):
        if self.detector is not None or self.attempted:
            return self.warning
        try:
            path = ensure_vad(self.models, status, stopped)
            import sherpa_onnx as so
            config = so.VadModelConfig()
            config.silero_vad.model = str(path)
            config.silero_vad.threshold = .35
            config.silero_vad.min_speech_duration = .096
            config.silero_vad.min_silence_duration = .1
            config.silero_vad.max_speech_duration = 30
            config.sample_rate, config.num_threads, config.provider = RATE, 1, 'cpu'
            self.detector = so.VoiceActivityDetector(config, buffer_size_in_seconds=40)
            self.attempted = True
        except InterruptedError:
            from realtime_asr import LiveStopped
            raise LiveStopped() from None
        except Exception as exc:
            self.attempted = True
            self.warning = '発話判定を準備できないため、基本の無音判定で続けます：' + str(exc)[:200]
        return self.warning

    def intervals(self, pcm):
        if len(pcm) % 2:
            raise ValueError('発話判定のPCM音声が正しくありません。')
        samples = np.frombuffer(pcm, '<i2').astype('float32') / 32768
        if not audible(samples):
            return []
        if self.detector is None:
            return [(0, len(samples))]
        try:
            # Re-evaluate each bounded ASR window independently. Recurrent state
            # must not carry a preceding speaker into a new, silent window.
            self.detector.reset()
            # Quiet short replies otherwise look like silence to the VAD. Apply
            # bounded gain only to its analysis copy, never to ASR or recordings.
            peak = float(np.max(np.abs(samples)))
            if peak < .1:
                samples = samples * min(20., .1/max(peak, 1e-8))
            padded = np.pad(samples, (0, (-len(samples)) % 512))
            for offset in range(0, len(padded), 512):
                self.detector.accept_waveform(padded[offset:offset+512])
            self.detector.flush()
            intervals = []
            while not self.detector.empty():
                segment = self.detector.front
                a, b = max(0, segment.start), min(len(samples), segment.start+len(segment.samples))
                if b > a:
                    intervals.append((a, b))
                self.detector.pop()
            return intervals
        except Exception as exc:
            self.detector = None
            self.warning = '発話判定が停止したため、基本の無音判定で続けます：' + str(exc)[:200]
            return [(0, len(samples))]

    def close(self):
        self.detector = None
        self.attempted = False
        self.warning = ''
