"""Incremental speaker tracking and durable live-session speaker edits."""
from __future__ import annotations
from collections import defaultdict
from copy import deepcopy
from pathlib import Path
import json
import numpy as np
from speakers import UNKNOWN, normalize_names

RATE = 16000
EDITS_FILE = "live_speaker_edits.json"

def apply_live_edits(data, folder):
    """Separate UI edits from worker checkpoints so neither writer loses the other."""
    path = Path(folder) / EDITS_FILE
    if not path.is_file():
        return data
    edits = json.loads(path.read_text("utf-8"))
    from overlap import set_state
    data['live_overlap_states'] = edits.get('overlap_states', {})
    for record in data.get('overlap_review', []):
        state = data['live_overlap_states'].get(record['id'])
        if state in {'applied', 'reverted'}:
            set_state(data, record, state, force=True)
    names = edits.get("names", {})
    assignments = edits.get("assignments", {})
    data["speaker_name_edits"] = dict(names)
    data["speaker_memory_links"] = edits.get("memory_links", {})
    data["live_text_edits"] = edits.get("texts", {})
    data["live_context_review_states"] = edits.get("review_states", {})
    if not isinstance(names, dict) or not isinstance(assignments, dict):
        raise ValueError("ライブ話者の編集ファイルの形式が正しくありません。")
    data["speakers"] = normalize_names(data.get("segments", []), {**data.get("speakers", {}), **names})
    for row in data.get("segments", []):
        key = row.get("segment_id") or row.get("id")
        if key in assignments:
            row["speaker"] = str(assignments[key])
            row["speaker_review"] = False
            row["speaker_manual"] = True
            correction = edits.get("voice_corrections", {}).get(key, {})
            for field in ("voice_correction_key", "voice_correction", "voice_learning_pending", "voice_rejected_profiles"):
                if field in correction:
                    row[field] = correction[field]
        if key in data["live_text_edits"]:
            text = data["live_text_edits"][key]
            row.update(text=str(text["text"]), words=deepcopy(text.get("words", [])))
    for record in data.get("context_review", []):
        state = data["live_context_review_states"].get(record["id"])
        if state in {"applied", "reverted"}:
            record["status"] = state
    data["speakers"] = normalize_names(data.get("segments", []), data["speakers"])
    return data

class SpeakerTracker:
    def __init__(self, count=0, threshold=.6):
        self.limit = int(count) or 30
        self.threshold = threshold
        self.profiles = {}
        self.weights = {}
        self.next_id = 0

    def reserve(self, keys):
        for key in keys:
            if key.startswith("speaker_") and key[8:].isdigit():
                self.next_id = max(self.next_id, int(key[8:]) + 1)

    def match(self, vectors, durations):
        """One-to-one matching prevents two simultaneous local voices sharing an ID."""
        normalized = {}
        for key, vector in vectors.items():
            vector = np.asarray(vector, dtype="float32")
            norm = float(np.linalg.norm(vector))
            if np.isfinite(vector).all() and norm > 1e-8:
                normalized[key] = vector / norm
        pairs = sorted(((float(v @ profile), key, speaker)
                        for key, v in normalized.items() for speaker, profile in self.profiles.items()), reverse=True)
        result, used = {}, set()
        for score, key, speaker in pairs:
            if key in result or speaker in used or score < self.threshold:
                continue
            others = [float(normalized[key] @ p) for s, p in self.profiles.items() if s != speaker]
            uncertain = durations.get(key, 0) < 1.2 or (others and score - max(others) < .08)
            result[key] = (speaker, bool(uncertain))
            used.add(speaker)
        for key in vectors:
            if key not in result:
                duration = durations.get(key, 0)
                if key not in normalized or duration < .8:
                    # A short response is still displayed, but never invent an identity.
                    result[key] = (UNKNOWN, True)
                elif len(self.profiles) < self.limit:
                    speaker = f"speaker_{self.next_id:02d}"
                    self.next_id += 1
                    self.profiles[speaker] = normalized[key]
                    self.weights[speaker] = 0.
                    result[key] = (speaker, duration < 1.2)
                else:
                    available = [(score, speaker) for score, k, speaker in pairs if k == key and speaker not in used]
                    if available and available[0][0] >= .35:
                        result[key] = (available[0][1], True)
                        used.add(available[0][1])
                    else:
                        result[key] = (UNKNOWN, True)
            speaker, uncertain = result[key]
            # Never adapt a profile using an uncertain assignment.
            if speaker in self.profiles and not uncertain:
                weight = min(4., durations.get(key, 0))
                old = min(20., self.weights[speaker])
                mixed = self.profiles[speaker] * old + normalized[key] * weight
                self.profiles[speaker] = mixed / max(float(np.linalg.norm(mixed)), 1e-8)
                self.weights[speaker] = old + weight
        return result

class LiveDiarizer:
    def __init__(self, models, count=0):
        import sherpa_onnx as so
        models = Path(models)
        self.tracker = SpeakerTracker(count)
        embedding = so.SpeakerEmbeddingExtractorConfig(model=str(models / "embedding.onnx"), num_threads=2, provider="cpu")
        config = so.OfflineSpeakerDiarizationConfig(
            segmentation=so.OfflineSpeakerSegmentationModelConfig(
                pyannote=so.OfflineSpeakerSegmentationPyannoteModelConfig(model=str(models / "segmentation.onnx")),
                num_threads=2, provider="cpu"),
            embedding=embedding,
            clustering=so.FastClusteringConfig(num_clusters=-1, threshold=.9),
            min_duration_on=.2, min_duration_off=.3)
        if not config.validate() or not embedding.validate():
            raise RuntimeError("リアルタイム話者判別モデルを読み込めません。")
        self.pipeline = so.OfflineSpeakerDiarization(config)
        self.extractor = so.SpeakerEmbeddingExtractor(embedding)
        from speaker_memory import model_signature
        self.model_id = model_signature(models / "embedding.onnx")
        self.samples = {}
        self.overlaps = []

    def reset(self, count=0):
        """Start a fresh recording while retaining native inference models."""
        self.tracker = SpeakerTracker(count)
        self.samples = {}

    def preview(self, pcm, offset):
        # Repeated drafts must not train the speaker tracker on the same voice
        # multiple times or replace the voice samples used for final learning.
        from copy import deepcopy
        original = self.tracker, self.samples
        self.tracker, self.samples = deepcopy(original)
        try:
            return self.process(pcm, offset), self.samples
        finally:
            self.tracker, self.samples = original

    def process(self, pcm, offset):
        self.overlaps = []
        samples = np.frombuffer(pcm, dtype="<i2").astype("float32") / 32768
        if len(samples) < RATE * .3:
            return []
        detected = self.pipeline.process(samples).sort_by_start_time()
        turns, pieces = [], defaultdict(list)
        for t in detected:
            a, b = max(0, float(t.start)), min(len(samples) / RATE, float(t.end))
            if b > a:
                turns.append({"start": a + offset, "end": b + offset, "local": int(t.speaker)})
                pieces[int(t.speaker)].append((a, b))
        from overlap import regions
        self.overlaps = regions(turns, offset, offset + len(samples)/RATE)
        # Mixed voices must not adapt the identity tracker either. Match only
        # clean excerpts; a fully overlapped voice stays unknown until separated.
        if self.overlaps:
            from recording_speakers import exclusive_ranges
            clean = exclusive_ranges([{'start': t['start']-offset, 'end': t['end']-offset,
                                       'speaker': str(t['local'])} for t in turns], len(samples)/RATE)
            pieces = {key: clean.get(str(key), []) for key in pieces}
        vectors, durations = {}, {}
        for key, intervals in pieces.items():
            voice = (np.concatenate([samples[round(a * RATE):round(b * RATE)] for a, b in intervals])
                     if intervals else np.zeros(0, dtype='float32'))
            durations[key] = len(voice) / RATE
            if durations[key] < .5:
                vectors[key] = np.zeros(self.extractor.dim, dtype="float32")
                continue
            stream = self.extractor.create_stream()
            stream.accept_waveform(sample_rate=RATE, waveform=voice)
            stream.input_finished()
            vectors[key] = (np.asarray(self.extractor.compute(stream), dtype="float32")
                            if self.extractor.is_ready(stream) else np.zeros(self.extractor.dim, dtype="float32"))
        identities = self.tracker.match(vectors, durations)
        for key, (speaker, uncertain) in identities.items():
            overlap = any(a["local"] == key and b["local"] != key
                          and min(a["end"], b["end"]) - max(a["start"], b["start"]) > .1
                          for a in turns for b in turns)
            if speaker != UNKNOWN and not uncertain and not overlap and durations[key] >= 3:
                from speaker_memory import vector
                candidate = vector(vectors[key])
                old = self.samples.get(speaker)
                if old is None or (durations[key] > old["seconds"]
                                   and float(candidate @ vector(old["embedding"])) >= .78):
                    self.samples[speaker] = {"embedding": candidate.tolist(),
                                             "seconds": durations[key], "model": self.model_id}
        for turn in turns:
            key = turn.pop("local")
            turn["speaker"], turn["speaker_review"] = identities[key]
        return turns

def speaker_ranges(turns, start, end):
    """Partition all audio at detected changes; each recognizer request has one speaker."""
    relevant = [dict(t) for t in turns if t["end"] > start and t["start"] < end]
    relevant.sort(key=lambda t: (t["start"], t["end"]))
    groups = []
    for t in relevant:
        t["start"], t["end"] = max(start, t["start"]), min(end, t["end"])
        if groups and groups[-1]["speaker"] == t["speaker"]:
            groups[-1]["end"] = max(groups[-1]["end"], t["end"])
            groups[-1]["speaker_review"] |= t.get("speaker_review", False)
        else:
            groups.append(t)
    if not groups:
        return [{"start": start, "end": end, "speaker": UNKNOWN, "speaker_review": True}]
    boundaries = [start]
    for left, right in zip(groups, groups[1:]):
        if left["end"] > right["start"]:
            left["speaker_review"] = right["speaker_review"] = True
        boundaries.append(max(boundaries[-1], min(end, (left["end"] + right["start"]) / 2)))
    boundaries.append(end)
    return [{**g, "start": a, "end": b} for g, a, b in zip(groups, boundaries, boundaries[1:]) if b - a >= .05]

class LiveSpeakerProcess:
    """Keep native speaker inference away from the audio callback's Python interpreter."""
    def __init__(self, models, count, job, stopped=lambda: False, memory_file=None):
        import os, queue, subprocess, sys, threading
        self.proc = None
        self.events = queue.Queue()
        self.stopped = stopped
        self.profiles, self.remembered, self.memory_error = {}, {}, ""
        self.overlaps = []
        self.log = (Path(job) / "live-speakers.log").open("wb")
        python = Path(sys.executable).with_name("python.exe") if os.name == "nt" else Path(sys.executable)
        try:
            self.proc = subprocess.Popen(
                [str(python), "-X", "utf8", "-u", str(Path(__file__).resolve()), "--worker", str(models), str(count), str(memory_file) if memory_file else ""],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log,
                text=True, encoding="utf-8", creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            stdout = self.proc.stdout
            def read():
                try:
                    for line in stdout:
                        try:
                            self.events.put(json.loads(line))
                        except ValueError:
                            pass
                finally:
                    self.events.put({"error": "話者判別エンジンが終了しました。"})
            threading.Thread(target=read, daemon=True).start()
            ready = self.receive(60, allow_stop=True)
            if not ready.get("ready"):
                raise RuntimeError("話者判別エンジンを準備できませんでした。")
        except Exception:
            self.close()
            raise

    def receive(self, timeout, allow_stop=False):
        import queue, time
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if allow_stop and self.stopped():
                from realtime_asr import LiveStopped
                raise LiveStopped()
            try:
                event = self.events.get(timeout=.1)
                if event.get("error"):
                    raise RuntimeError(event["error"])
                return event
            except queue.Empty:
                pass
        raise RuntimeError("話者判別の応答が時間内に届きませんでした。")

    def reset(self, count=0, memory_file=None):
        self.proc.stdin.write(json.dumps({"command": "reset", "count": count,
                                         "memory_file": str(memory_file) if memory_file else ""}) + "\n")
        self.proc.stdin.flush()
        if not self.receive(10, allow_stop=True).get("reset"):
            raise RuntimeError("次の録音用に話者判別を準備できませんでした。")
        self.profiles, self.remembered, self.memory_error = {}, {}, ""

    def process(self, wav, start, end, names, *, preview=False):
        self.proc.stdin.write(json.dumps({"wav": str(wav), "start": start, "end": end, "names": list(names), 'preview': preview}) + "\n")
        self.proc.stdin.flush()
        response = self.receive(45)
        self.profiles = response.get("profiles", {})
        self.remembered = response.get("remembered", {})
        self.memory_error = response.get("memory_error", "")
        self.overlaps = response.get('overlaps', [])
        return response["turns"]

    def close(self):
        import subprocess
        if self.proc:
            if self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.proc.kill(); self.proc.wait(timeout=5)
            for stream in (self.proc.stdin, self.proc.stdout):
                if stream:
                    stream.close()
            self.proc = None
        self.log.close()

def speaker_worker():
    import sys, traceback
    try:
        worker = LiveDiarizer(Path(sys.argv[2]), int(sys.argv[3]))
        from speaker_memory import MemoryStore, match_samples
        memory = MemoryStore(sys.argv[4]) if len(sys.argv) > 4 and sys.argv[4] else None
        print(json.dumps({"ready": True}), flush=True)
        for line in sys.stdin:
            request = json.loads(line)
            if request.get("command") == "reset":
                worker.reset(int(request.get("count", 0)))
                memory = MemoryStore(request["memory_file"]) if request.get("memory_file") else None
                print(json.dumps({"reset": True}), flush=True)
                continue
            a, b = int(request["start"]), int(request["end"])
            if a < 0 or b <= a or b - a > 30 * RATE:
                raise ValueError("話者解析の区間が正しくありません。")
            with Path(request["wav"]).open("rb") as f:
                f.seek(44 + a * 2)
                pcm = f.read((b - a) * 2)
            worker.tracker.reserve(request.get("names", []))
            if request.get('preview', False):
                turns, profiles = worker.preview(pcm, a / RATE)
            else:
                turns, profiles = worker.process(pcm, a / RATE), worker.samples
            remembered, memory_error = {}, ""
            if memory:
                try:
                    remembered = match_samples(profiles, memory.load())
                except Exception as exc:
                    memory_error = str(exc)
            print(json.dumps({"turns": turns, "profiles": profiles, 'overlaps': worker.overlaps,
                              "remembered": remembered, "memory_error": memory_error}), flush=True)
    except Exception as exc:
        traceback.print_exc(file=sys.stderr)
        print(json.dumps({"error": str(exc)}), flush=True)

if __name__ == "__main__":
    import sys
    if "--worker" in sys.argv:
        speaker_worker()
