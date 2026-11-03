"""Learn from explicitly corrected speech, combining short utterances from one recording."""
from pathlib import Path
import math
import subprocess
import numpy as np
from speaker_memory import MIN_SECONDS, validate_sample, vector
from live_speakers import LiveDiarizer

class WaitingForSpeech(ValueError):
    def __init__(self, seconds):
        self.waiting_seconds = max(0., float(seconds))
        super().__init__(f"判別できる音声が計{int(self.waiting_seconds * 10) / 10:.1f}秒です。同じ話者の発言を追加し、合計3秒以上にしてください。")

def correction_ranges(ranges):
    if not ranges or len(ranges) > 32:
        raise ValueError("まとめる発言は1〜32行で指定してください。")
    spans = []
    for pair in ranges:
        start, end = map(float, pair)
        if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end <= start:
            raise ValueError("発言の時刻が正しくありません。")
        if end - start > 30:
            raise ValueError("30秒を超える行は、行を分割してから話者を修正してください。")
        spans.append((start, end))
    merged = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    if sum(b-a for a,b in merged) > 60:
        raise ValueError("一度に確認する発言は合計60秒までです。")
    return merged

def single_voice_audio(audio, turns, allowed=None):
    speakers = {t["speaker"] for t in turns if t["end"] > t["start"]}
    if len(speakers) > 1:
        raise ValueError("この行には複数の声を検出しました。話者ごとに行を分割して修正してください。")
    if not speakers:
        raise WaitingForSpeech(0.)
    mask = np.zeros(len(audio), dtype=bool)
    for turn in turns:
        a, b = max(0, round(turn["start"] * 16000)), min(len(audio), round(turn["end"] * 16000))
        mask[a:b] = True
    if allowed is not None:
        mask &= allowed
    selected = audio[mask]
    voiced = [selected[i:i + 1600] for i in range(0, len(selected), 1600)
              if float(np.sqrt(np.mean(selected[i:i + 1600] ** 2))) >= .0035]
    clean = np.concatenate(voiced) if voiced else np.empty(0, dtype="float32")
    if len(clean) / 16000 < MIN_SECONDS:
        raise WaitingForSpeech(len(clean) / 16000)
    return clean

def check_voice_consistency(audio, extractor):
    # Short-clip clustering can merge two people. Compare speech windows as an
    # independent check before adding a persistent correction example.
    size = 3 * 16000
    if len(audio) < int(4.5 * 16000):
        return
    starts = sorted(set(list(range(0, len(audio) - size + 1, size)) + [len(audio) - size]))
    vectors = []
    for start in starts:
        stream = extractor.create_stream()
        stream.accept_waveform(sample_rate=16000, waveform=audio[start:start + size])
        stream.input_finished()
        if not extractor.is_ready(stream):
            raise ValueError("声の特徴が安定していないため、この発言は覚え直せません。")
        candidate = vector(extractor.compute(stream))
        if any(float(candidate @ previous) < .60 for previous in vectors):
            raise ValueError("複数の声、または声の大きな変化を検出しました。単独の発言を選んで修正してください。")
        vectors.append(candidate)


def extract_correction_sample(request):
    ranges = correction_ranges(request["ranges"])
    seconds = sum(end-start for start,end in ranges)
    if seconds < MIN_SECONDS:
        raise WaitingForSpeech(seconds)
    source = Path(request["source"])
    if not source.is_file():
        raise ValueError("元の録音が見つかりません。音声が保存された結果を開いてください。")
    pieces = []
    for start, end in ranges:
        result = subprocess.run([str(request["ffmpeg"]), "-v", "error", "-nostdin",
            "-ss", str(start), "-t", str(end-start), "-i", str(source),
            "-vn", "-ac", "1", "-ar", "16000", "-f", "s16le", "pipe:1"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode:
            raise ValueError("修正した発言の音声を読み込めませんでした。")
        pieces.append(np.frombuffer(result.stdout, "<i2").astype("float32") / 32768)
    return sample_from_pieces(pieces, request["model"])

def sample_from_pieces(pieces, model):
    # Separators help detect boundaries but contribute no voiced duration.
    audio = np.concatenate([part for piece in pieces for part in (piece, np.zeros(3200, dtype="float32"))])
    allowed = np.concatenate([part for piece in pieces
                              for part in (np.ones(len(piece), dtype=bool), np.zeros(3200, dtype=bool))])
    worker = LiveDiarizer(Path(model).parent)
    detected = worker.pipeline.process(audio).sort_by_start_time()
    turns = [{"start": float(t.start), "end": float(t.end), "speaker": int(t.speaker)} for t in detected]
    clean = single_voice_audio(audio, turns, allowed)
    check_voice_consistency(clean, worker.extractor)
    # Two short people can be merged by clustering. Compare usable individual
    # excerpts too; use a conservative cutoff for these less stable short vectors.
    vectors = []
    for piece in pieces:
        voiced = [piece[i:i+1600] for i in range(0, len(piece), 1600)
                  if float(np.sqrt(np.mean(piece[i:i+1600] ** 2))) >= .0035]
        voice = np.concatenate(voiced) if voiced else np.empty(0, dtype="float32")
        if len(voice) < 16000:
            continue
        stream = worker.extractor.create_stream()
        stream.accept_waveform(sample_rate=16000, waveform=voice[:12*16000])
        stream.input_finished()
        if worker.extractor.is_ready(stream):
            candidate = vector(worker.extractor.compute(stream))
            if any(float(candidate @ old) < .30 for old in vectors):
                raise ValueError("まとめた発言に複数の声、または声の大きな変化を検出しました。話者の指定を確認してください。")
            vectors.append(candidate)
    clean = clean[:12 * 16000]
    stream = worker.extractor.create_stream()
    stream.accept_waveform(sample_rate=16000, waveform=clean)
    stream.input_finished()
    if not worker.extractor.is_ready(stream):
        raise ValueError("この発言から声の特徴を取得できませんでした。")
    return validate_sample({"embedding": worker.extractor.compute(stream),
                            "seconds": len(clean)/16000, "model": worker.model_id})
