"""Match diarized local media against explicitly registered voice names."""
from __future__ import annotations
from collections import defaultdict
from itertools import groupby
import math
from pathlib import Path
import wave
import numpy as np
from speaker_memory import MIN_SECONDS, MemoryStore, match_samples, model_signature, vector
from speakers import UNKNOWN

RATE = 16000

def exclusive_ranges(turns, duration):
    """Keep only spans containing one distinct voice, merging duplicate detections."""
    events = []
    for turn in turns:
        a, b = float(turn["start"]), float(turn["end"])
        if not math.isfinite(a) or not math.isfinite(b):
            continue
        a, b = max(0., a), min(duration, b)
        speaker = turn.get("speaker") or UNKNOWN
        if turn.get("speaker_review"):
            speaker = UNKNOWN
        if b > a:
            events.extend([(a, speaker, 1), (b, speaker, -1)])
    events.sort(key=lambda e: e[0])
    active, result, previous = defaultdict(int), defaultdict(list), 0.
    for instant, group in groupby(events, key=lambda e: e[0]):
        if instant > previous and len(active) == 1:
            speaker = next(iter(active))
            if speaker != UNKNOWN:
                ranges = result[speaker]
                if ranges and abs(ranges[-1][1] - previous) < 1e-7:
                    ranges[-1][1] = instant
                else:
                    ranges.append([previous, instant])
        for _, speaker, delta in group:
            active[speaker] += delta
            if active[speaker] == 0:
                del active[speaker]
        previous = instant
    return dict(result)

def recording_samples(wav, turns, model, report=lambda text: None, signature=None):
    """Bound reads and embedding work to a few clean excerpts per detected speaker."""
    import sherpa_onnx as so
    model = Path(model)
    signature = signature or model_signature(model)
    config = so.SpeakerEmbeddingExtractorConfig(model=str(model), num_threads=2, provider="cpu")
    if not config.validate():
        raise ValueError("覚えた声の照合モデルを読み込めません。")
    extractor = so.SpeakerEmbeddingExtractor(config)
    samples = {}
    with wave.open(str(wav), "rb") as reader:
        if (reader.getframerate(), reader.getnchannels(), reader.getsampwidth()) != (RATE, 1, 2):
            raise ValueError("声の照合には16kHz・モノラルの音声が必要です。")
        ranges = exclusive_ranges(turns, reader.getnframes() / RATE)
        for index, (speaker, spans) in enumerate(ranges.items(), 1):
            report(f"覚えた話者の声と照合中… {index}/{len(ranges)}人")
            pieces, frames = [], 0
            for start, end in sorted(spans, key=lambda p: p[1] - p[0], reverse=True)[:6]:
                # Leave a little space around detected changes to avoid neighboring voices.
                start, end = start + .05, end - .05
                count = min(round((end - start) * RATE), 8 * RATE, 12 * RATE - frames)
                if count < RATE // 2:
                    continue
                reader.setpos(min(reader.getnframes(), round(start * RATE)))
                audio = np.frombuffer(reader.readframes(count), "<i2").astype("float32") / 32768
                voiced = [audio[i:i + 1600] for i in range(0, len(audio), 1600)
                          if float(np.sqrt(np.mean(audio[i:i + 1600] ** 2))) >= .0035]
                if voiced:
                    piece = np.concatenate(voiced)
                    pieces.append(piece)
                    frames += len(piece)
                if frames >= 12 * RATE:
                    break
            if frames / RATE < MIN_SECONDS:
                continue
            stream = extractor.create_stream()
            stream.accept_waveform(sample_rate=RATE, waveform=np.concatenate(pieces))
            stream.input_finished()
            if extractor.is_ready(stream):
                samples[speaker] = {"embedding": vector(extractor.compute(stream)).tolist(),
                                    "seconds": frames / RATE, "model": signature}
    return samples

def recall_names(wav, turns, model, memory_file, report=lambda text: None):
    profiles = MemoryStore(memory_file).load()
    if not profiles or not turns:
        return {}
    signature = model_signature(model)
    compatible = {key: value for key, value in profiles.items()
                  if any(s.get("model") == signature for s in [value] + value.get("corrections", []))}
    if not compatible:
        return {}
    samples = recording_samples(wav, turns, model, report, signature)
    return match_samples(samples, compatible)
