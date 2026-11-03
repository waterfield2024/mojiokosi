"""Local diarization plus stable speaker IDs shared by UI and exporters."""
from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import defaultdict
import math
from pathlib import Path
import re
import time
import wave

UNKNOWN = "unknown"


def default_name(speaker):
    if speaker == UNKNOWN:
        return "話者不明"
    match = re.fullmatch(r"speaker_(\d+)", str(speaker))
    return f"話者{int(match[1]) + 1}" if match else str(speaker)


def speaker_name(speaker, names=None):
    if not speaker:
        return ""
    return str((names or {}).get(speaker) or default_name(speaker)).replace("\n", " ").strip()


def segment_text(segment, names=None):
    text = str(segment["text"]).strip()
    name = speaker_name(segment.get("speaker"), names)
    return f"{name}：{text}" if name and text else text


def normalize_names(segments, names=None):
    result = {str(k): str(v) for k, v in (names or {}).items()}
    for s in segments:
        if s.get("speaker"):
            result.setdefault(s["speaker"], default_name(s["speaker"]))
    return result


def diarize(wav, models, count=0, report=lambda text: None):
    import numpy as np
    import sherpa_onnx as so
    if not 0 <= int(count) <= 30:
        raise ValueError("話者数は自動、または1〜30人で指定してください。")
    models = Path(models)
    if not all((models / p).is_file() for p in ("segmentation.onnx", "embedding.onnx")):
        raise FileNotFoundError("話者分離モデルが見つかりません。models/speakers を確認してください。")
    with wave.open(str(wav), "rb") as reader:
        if reader.getframerate() != 16000 or reader.getnchannels() != 1 or reader.getsampwidth() != 2:
            raise ValueError("話者分離には16kHz・モノラルの音声が必要です。")
        samples = np.frombuffer(reader.readframes(reader.getnframes()), dtype="<i2").astype("float32") / 32768
    if not len(samples):
        return []
    config = so.OfflineSpeakerDiarizationConfig(
        segmentation=so.OfflineSpeakerSegmentationModelConfig(
            pyannote=so.OfflineSpeakerSegmentationPyannoteModelConfig(
                model=str(models / "segmentation.onnx")),
            num_threads=4, provider="cpu"),
        embedding=so.SpeakerEmbeddingExtractorConfig(
            model=str(models / "embedding.onnx"), num_threads=4, provider="cpu"),
        clustering=so.FastClusteringConfig(num_clusters=int(count) or -1, threshold=.9),
        min_duration_on=.3, min_duration_off=.5)
    if not config.validate():
        raise RuntimeError("話者分離の設定を読み込めませんでした。")
    report("話者の声を解析中…")
    pipeline = so.OfflineSpeakerDiarization(config)
    last = [0.]
    def progress(done, total):
        now = time.monotonic()
        if now - last[0] > .5 or done == total:
            report(f"話者の声を解析中… {min(99, int(done * 100 / max(1, total)))}%")
            last[0] = now
        return 0
    result = pipeline.process(samples, callback=progress).sort_by_start_time()
    ids = {}
    turns = []
    for turn in result:
        speaker = ids.setdefault(turn.speaker, f"speaker_{len(ids):02d}")
        start, end = max(0., float(turn.start)), min(len(samples) / 16000, float(turn.end))
        if end > start:
            turns.append({"start": start, "end": end, "speaker": speaker})
    report(f"話者を{len(ids)}人に分けました。")
    return turns


class TurnIndex:
    def __init__(self, turns):
        self.turns = sorted(turns, key=lambda t: t["start"])
        self.starts = [t["start"] for t in self.turns]
        self.max_ends = []
        maximum = 0.
        for turn in self.turns:
            maximum = max(maximum, turn["end"])
            self.max_ends.append(maximum)

    def assign(self, start, end):
        if not math.isfinite(start) or not math.isfinite(end):
            raise ValueError("発言時刻が不正です。")
        end = max(start + .01, end)
        scores = defaultdict(float)
        for turn in self.turns[bisect_left(self.max_ends, start):bisect_right(self.starts, end)]:
            overlap = min(end, turn["end"]) - max(start, turn["start"])
            if overlap > 0:
                scores[turn["speaker"]] += overlap
        if scores:
            ranked = sorted(scores.items(), key=lambda x: -x[1])
            uncertain = len(ranked) > 1 and ranked[1][1] > ranked[0][1] * .6
            return ranked[0][0], uncertain
        # A word boundary can drift slightly outside the detected speech interval.
        nearby = bisect_left(self.starts, (start + end) / 2)
        candidates = self.turns[max(0, nearby - 2):nearby + 2]
        if candidates:
            nearest = min(candidates, key=lambda t: max(t["start"] - end, start - t["end"], 0))
            if max(nearest["start"] - end, start - nearest["end"], 0) <= .8:
                return nearest["speaker"], True
        return UNKNOWN, True


def assign_speakers(segments, turns):
    index = TurnIndex(turns)
    output = []
    for source in segments:
        segment = dict(source)
        words = segment.get("words") or []
        # Do not overwrite edited text using stale word timing information.
        exact = words and "".join(str(w["word"]) for w in words).strip() == segment["text"].strip()
        if not exact:
            speaker, uncertain = index.assign(float(segment["start"]), float(segment["end"]))
            segment.update(speaker=speaker, speaker_review=uncertain or not bool(words))
            output.append(segment)
            continue
        groups = []
        for word in words:
            speaker, uncertain = index.assign(float(word["start"]), float(word["end"]))
            if not groups or groups[-1]["speaker"] != speaker:
                groups.append({"speaker": speaker, "words": [], "speaker_review": False})
            groups[-1]["words"].append(dict(word))
            groups[-1]["speaker_review"] |= uncertain
        for group in groups:
            text = "".join(str(w["word"]) for w in group["words"]).strip()
            if not text:
                continue
            start = max(float(segment["start"]), float(group["words"][0]["start"]))
            end = min(float(segment["end"]), float(group["words"][-1]["end"]))
            output.append({**group, "text": text, "start": start, "end": max(start + .01, end)})
    return output


def reuse_speaker_ids(old_segments, new_segments, old_ids=None):
    """Map new IDs to old IDs by timeline overlap without reusing an old identity."""
    scores = defaultdict(float)
    allowed = set(old_ids) if old_ids is not None else None
    old_index = TurnIndex([s for s in old_segments if s.get("speaker") not in (None, "", UNKNOWN)
                           and s["end"] > s["start"]
                           and (allowed is None or s["speaker"] in allowed)])
    for segment in new_segments:
        start, end = segment["start"], segment["end"]
        for old in old_index.turns[bisect_left(old_index.max_ends, start):bisect_right(old_index.starts, end)]:
            overlap = min(end, old["end"]) - max(start, old["start"])
            if overlap > 0:
                scores[(segment["speaker"], old["speaker"])] += overlap
    mapping, used = {}, set()
    for (new, old), score in sorted(scores.items(), key=lambda p: -p[1]):
        if new != UNKNOWN and new not in mapping and old not in used:
            mapping[new] = old
            used.add(old)
    return mapping


def reuse_names(old_segments, new_segments, old_names):
    mapping = reuse_speaker_ids(old_segments, new_segments, old_names)
    return normalize_names(new_segments, {new: old_names[old] for new, old in mapping.items()})
