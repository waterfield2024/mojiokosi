"""Local, explicitly named voice profiles. Inference never updates this library."""
from __future__ import annotations
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import time
import uuid
import numpy as np
from speaker_pool import PoolStore, validate_pool, retract

MIN_SECONDS = 3.0
MATCH_THRESHOLD = .78
MATCH_MARGIN = .10
PROFILE_FILE = "speaker_profiles.json"
CORRECTION_THRESHOLD = .90
MAX_CORRECTIONS = 64

def vector(value):
    v = np.asarray(value, dtype="float32")
    if v.ndim != 1 or not 2 <= v.size <= 4096 or not np.isfinite(v).all():
        raise ValueError("声の特徴データが正しくありません。")
    norm = float(np.linalg.norm(v))
    if norm < 1e-8:
        raise ValueError("声の特徴を取得できませんでした。")
    return v / norm

def model_signature(model):
    h = hashlib.sha256()
    with Path(model).open("rb") as f:
        while block := f.read(1024 * 1024):
            h.update(block)
    return h.hexdigest()

def valid_name(name):
    name = str(name).strip()
    if not name or len(name) > 30 or any(ord(c) < 32 for c in name):
        raise ValueError("名前は改行を含まない1〜30文字で入力してください。")
    return name

def validate_sample(sample):
    if not isinstance(sample, dict) or not isinstance(sample.get("model"), str) or not sample["model"]:
        raise ValueError("声の特徴モデルを確認できません。")
    seconds = float(sample.get("seconds", 0))
    if not np.isfinite(seconds) or seconds < MIN_SECONDS:
        raise ValueError("声を覚えるには、判別できる発言が3秒以上必要です。")
    return {"embedding": vector(sample["embedding"]).tolist(), "seconds": seconds, "model": sample["model"]}

class MemoryStore(PoolStore):
    def __init__(self, path):
        self.path = Path(path)

    def load(self):
        if not self.path.exists():
            return {}
        try:
            data = json.loads(self.path.read_text("utf-8"))
            if data.get("version") != 1 or not isinstance(data.get("profiles"), dict):
                raise ValueError()
            profiles = data["profiles"]
            self.validate_profiles(profiles)
            self.validate_history(data.get("history", []))
            return profiles
        except (ValueError, TypeError, KeyError) as exc:
            raise ValueError("覚えた話者のファイルを読み込めません。元のファイルは変更していません。") from exc

    @staticmethod
    def validate_profiles(profiles):
        if not isinstance(profiles, dict):
            raise ValueError()
        for key, p in profiles.items():
            if not isinstance(key, str) or not key:
                raise ValueError()
            valid_name(p["name"])
            if not p.get("pending_only"):
                validate_sample(p)
            validate_pool(p)
            corrections = p.get("corrections", [])
            if not isinstance(corrections, list) or len(corrections) > MAX_CORRECTIONS:
                raise ValueError()
            for correction in corrections:
                validate_sample(correction)
                members = correction.get("members", [correction.get("key")])
                if not isinstance(members, list) or not 1 <= len(members) <= 32 or not all(
                        isinstance(k, str) and k for k in members):
                    raise ValueError()
                if not isinstance(correction.get("key"), str) or not correction["key"]:
                    raise ValueError()
                if not isinstance(correction.get("rejected", []), list) or not all(
                        isinstance(k, str) for k in correction.get("rejected", [])):
                    raise ValueError()

    @contextmanager
    def locked(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock = self.path.with_suffix(".lock")
        with lock.open("a+b") as f:
            f.seek(0, 2)
            if not f.tell():
                f.write(b"\0"); f.flush()
            if os.name == "nt":
                import msvcrt
                deadline = time.monotonic() + 5
                while True:
                    try:
                        f.seek(0); msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise OSError("別のウィンドウが話者を保存しています。少し待って再実行してください。")
                        time.sleep(.05)
            try:
                yield
            finally:
                if os.name == "nt":
                    f.seek(0); msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)

    def save(self, profiles, history=None):
        from context_ai import write_json
        if history is None:
            history = self.review_history()
        data = {"version": 1, "profiles": profiles}
        if history:
            data["history"] = history
        write_json(self.path, data)

    def remember(self, name, sample, existing_id=None):
        name, sample = valid_name(name), validate_sample(sample)
        with self.locked():
            profiles = self.load()
            key = existing_id if existing_id in profiles else None
            if key is None:
                waiting = [k for k, p in profiles.items() if p["name"] == name and p.get("pending_only")]
                if len(waiting) == 1:
                    key = waiting[0]
            if key is None:
                # Re-applying the same name to the same voice does not create duplicates.
                matches = match_samples({"sample": sample}, profiles)
                match = matches.get("sample")
                if match and match["name"] == name:
                    key = match["id"]
            now = datetime.now(timezone.utc).isoformat()
            if key is None:
                key = "voice_" + uuid.uuid4().hex
                profiles[key] = {**sample, "created": now}
            if profiles[key].get("pending_only") and profiles[key].get("evidence_deleted"):
                raise ValueError("削除した学習は復元しません。正しい発言を選んで話者を指定してください。")
            if profiles[key].get("pending_only"):
                profiles[key].update(sample)
                profiles[key].pop("pending_only", None)
            # A rename does not retrain the stored voice from an automatic prediction.
            profiles[key].update(name=name, updated=now)
            self.save(profiles)
            return key

    def learn_correction(self, name, sample, correction_key, existing_id=None, rejected=(), members=None, expected_pool_revision=None):
        """Only explicit row corrections add voice examples; repeated edits replace them."""
        name, sample = valid_name(name), validate_sample(sample)
        if not isinstance(correction_key, str) or not correction_key:
            raise ValueError("修正した発言を確認できません。")
        members = sorted(set(members or [correction_key]))
        if not all(isinstance(k, str) and k for k in members) or len(members) > 32:
            raise ValueError("まとめる発言の情報が正しくありません。")
        with self.locked():
            profiles = self.load()
            if existing_id and existing_id not in profiles:
                raise ValueError("この話者は削除されています。話者を選び直してください。")
            key = existing_id
            if expected_pool_revision is not None and (
                    key not in profiles or profiles[key].get("pool_revision", 0) != expected_pool_revision):
                return None
            if key is None:
                named = [k for k, p in profiles.items() if p["name"] == name]
                if len(named) > 1:
                    raise ValueError("同じ名前の登録が複数あります。「覚えた話者…」で名前を分けてください。")
                key = named[0] if named else "voice_" + uuid.uuid4().hex
            if key in profiles and set(members).intersection(profiles[key].get("removed_clips", [])):
                return None
            denied = set(rejected)
            # A second correction of this utterance supersedes its first teaching.
            for other, p in profiles.items():
                kept = []
                for c in p.get("corrections", []):
                    if c["key"] == correction_key or set(c.get("members", [c["key"]])).intersection(members):
                        denied.update(c.get("rejected", []))
                        if other != key:
                            denied.add(other)
                    else:
                        kept.append(c)
                if "corrections" in p:
                    p["corrections"] = kept
            denied.discard(key)
            now = datetime.now(timezone.utc).isoformat()
            if key not in profiles:
                # The seed is retained for the v1 file format, but is not a second
                # permanent example: correcting this row again must remove its voice.
                profiles[key] = {**sample, "created": now, "correction_only": True}
            profile = profiles[key]
            if profile.get("pending_only"):
                profile.update(sample)
                profile.pop("pending_only", None)
                profile["correction_only"] = True
            if expected_pool_revision is not None:
                profile["pool_learned_revision"] = expected_pool_revision
            profile.update(name=name, updated=now)
            examples = profile.setdefault("corrections", [])
            examples.append({**sample, "key": correction_key, "members": members, "rejected": sorted(denied),
                             "updated": now})
            profile["corrections"] = examples[-MAX_CORRECTIONS:]
            self.save(profiles)
            return key

    def forget_correction_members(self, members, suspend=False):
        """Retract grouped evidence; optionally retain a clip for reassignment."""
        members, affected = set(members), set()
        if not members or not self.path.exists():
            return affected
        with self.locked():
            profiles = self.load()
            affected = retract(profiles, members)
            obsolete, changed = [], False
            for profile in profiles.values():
                clips = profile.get("clips", {})
                touched = False
                for key in members.intersection(clips):
                    if suspend:
                        clips[key]["excluded"] = True
                    else:
                        clip = clips.pop(key)
                        if clip.get("file"):
                            obsolete.append(clip["file"])
                    touched = changed = True
                    affected.add(key)
                if touched:
                    profile["pool_revision"] = profile.get("pool_revision", 0) + 1
            if affected or changed:
                self.save(profiles)
                self.delete_clip_files(obsolete)
        return affected

    def rename(self, key, name):
        name = valid_name(name)
        with self.locked():
            profiles = self.load()
            if key not in profiles:
                raise ValueError("この話者はすでに削除されています。")
            profiles[key].update(name=name, updated=datetime.now(timezone.utc).isoformat())
            profiles[key]["pool_revision"] = profiles[key].get("pool_revision", 0) + 1
            self.save(profiles)

    def forget(self, key):
        with self.locked():
            profiles = self.load()
            removed = profiles.pop(key, {})
            for profile in profiles.values():
                for correction in profile.get("corrections", []):
                    correction["rejected"] = [k for k in correction.get("rejected", []) if k != key]
            history = [item for item in self.review_history() if key not in item["targets"]]
            self.save(profiles, history=history)
            self.clean_undo_audio(history)
            self.delete_clip_files([c["file"] for c in removed.get("clips", {}).values() if c.get("file")])

def match_samples(samples, profiles):
    """Require a strong, unambiguous match; stored people don't consume session slots."""
    proposals = []
    for speaker, sample in samples.items():
        try:
            sample = validate_sample(sample)
            v = vector(sample["embedding"])
        except (ValueError, TypeError, KeyError):
            continue
        scores_by_id, corrections = {}, []
        for key, p in profiles.items():
            examples = ([] if p.get("correction_only") or p.get("pending_only") else [p]) + p.get("corrections", [])
            for example in examples:
                if example["model"] != sample["model"] or len(example["embedding"]) != len(v):
                    continue
                score = float(v @ vector(example["embedding"]))
                scores_by_id[key] = max(scores_by_id.get(key, -1.), score)
                if example is not p:
                    corrections.append((score, key, example.get("rejected", [])))
        # Explicitly corrected, closely resembling speech must not fall back to
        # the rejected identity. Other voices and ambiguous corrections stay cautious.
        blocked = set()
        for score, target, rejected in corrections:
            if score >= CORRECTION_THRESHOLD:
                for wrong in rejected:
                    if wrong != target and score >= scores_by_id.get(wrong, 2.) - .02:
                        blocked.add(wrong)
        scores = [(score, key) for key, score in scores_by_id.items() if key not in blocked]
        scores.sort(reverse=True)
        if not scores or scores[0][0] < MATCH_THRESHOLD:
            continue
        if len(scores) > 1 and scores[0][0] - scores[1][0] < MATCH_MARGIN:
            continue
        proposals.append((scores[0][0], speaker, scores[0][1]))
    # If two session voices both resemble one stored person, withhold both labels.
    counts = {}
    for _, _, key in proposals:
        counts[key] = counts.get(key, 0) + 1
    return {speaker: {"id": key, "name": profiles[key]["name"], "score": round(score, 4)}
            for score, speaker, key in proposals if counts[key] == 1}

def extract_sample(request):
    """Enroll from an existing local recording when no live profile is available."""
    import subprocess
    import sherpa_onnx as so
    if request.get("correction"):
        from speaker_relearning import extract_correction_sample
        return extract_correction_sample(request)
    model = Path(request["model"])
    source = Path(request["source"])
    if not source.is_file():
        raise ValueError("元の録音が見つかりません。音声が保存された結果を開いてください。")
    pieces, total = [], 0.
    ranges = sorted(request["ranges"], key=lambda r: r[1] - r[0], reverse=True)
    for start, end in ranges[:6]:
        seconds = min(8., max(0., end - start), 12. - total)
        if start < 0 or seconds < .5:
            continue
        result = subprocess.run([str(request["ffmpeg"]), "-v", "error", "-nostdin",
            "-ss", str(start), "-t", str(seconds), "-i", str(source),
            "-vn", "-ac", "1", "-ar", "16000", "-f", "s16le", "pipe:1"],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if result.returncode:
            raise RuntimeError("登録用の音声を読み込めませんでした。")
        audio = np.frombuffer(result.stdout, "<i2").astype("float32") / 32768
        voiced = [audio[i:i + 1600] for i in range(0, len(audio), 1600)
                  if float(np.sqrt(np.mean(audio[i:i + 1600] ** 2))) >= .0035]
        if voiced:
            pieces.append(np.concatenate(voiced))
            total += len(pieces[-1]) / 16000
        if total >= 12:
            break
    if total < MIN_SECONDS:
        raise ValueError("声を覚えるには、判別できる発言が3秒以上必要です。")
    config = so.SpeakerEmbeddingExtractorConfig(model=str(model), num_threads=2, provider="cpu")
    if not config.validate():
        raise ValueError("話者判別モデルを読み込めません。")
    extractor = so.SpeakerEmbeddingExtractor(config)
    stream = extractor.create_stream()
    stream.accept_waveform(sample_rate=16000, waveform=np.concatenate(pieces))
    stream.input_finished()
    if not extractor.is_ready(stream):
        raise ValueError("この音声から声の特徴を取得できませんでした。")
    return validate_sample({"embedding": extractor.compute(stream), "seconds": total,
                            "model": model_signature(model)})

if __name__ == "__main__":
    import sys
    try:
        request = json.loads(Path(sys.argv[1]).read_text("utf-8"))
        print(json.dumps({"sample": extract_sample(request)}), flush=True)
    except Exception as exc:
        print(json.dumps({"error": str(exc), "waiting_seconds": getattr(exc, "waiting_seconds", None)}, ensure_ascii=False), flush=True)
        raise SystemExit(1)
