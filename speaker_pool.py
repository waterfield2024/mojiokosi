"""Durable, explicitly assigned speech excerpts shared by all recordings."""
from pathlib import Path
from datetime import datetime, timezone
import hashlib
import json
import re
import shutil
import uuid
import wave

from speaker_review import ReviewStore

MAX_CLIPS = 64

def clip_members(correction):
    return set(correction.get("members", [correction["key"]]))

def retract(profiles, members):
    affected = set()
    for profile in profiles.values():
        kept = []
        for correction in profile.get("corrections", []):
            used = clip_members(correction)
            if used.intersection(members):
                affected.update(used)
            else:
                kept.append(correction)
        if "corrections" in profile:
            profile["corrections"] = kept
    return affected

def validate_pool(profile):
    removed = profile.get("removed_clips", [])
    if not isinstance(removed, list) or not all(isinstance(k, str) and k for k in removed):
        raise ValueError()
    clips = profile.get("clips", {})
    if not isinstance(clips, dict) or len(clips) > MAX_CLIPS:
        raise ValueError()
    if not isinstance(profile.get("pool_revision", 0), int):
        raise ValueError()
    from speaker_relearning import correction_ranges
    for key, clip in clips.items():
        if not isinstance(key, str) or not key or not isinstance(clip, dict):
            raise ValueError()
        correction_ranges([[clip["start"], clip["end"]]])
        if not isinstance(clip["origin"], str) or not isinstance(clip["source"], str):
            raise ValueError()
        if clip.get("file") and not re.fullmatch(r"[0-9a-f]{32}\.wav", clip["file"]):
            raise ValueError()
        if not isinstance(clip.get("rejected", []), list):
            raise ValueError()

class PoolStore(ReviewStore):
    @property
    def clips_dir(self):
        return self.path.with_name(self.path.stem + "_clips")

    def clip_path(self, name):
        if not isinstance(name, str) or not re.fullmatch(r"[0-9a-f]{32}\.wav", name):
            raise ValueError("蓄積音声のファイル名が正しくありません。")
        return self.clips_dir / name

    def delete_clip_files(self, names):
        for name in set(names):
            self.clip_path(name).unlink(missing_ok=True)

    def stage_clips(self, name, rows, source, origin, existing_id=None):
        from speaker_memory import valid_name
        from speaker_relearning import correction_ranges
        name = valid_name(name)
        for row in rows:
            correction_ranges([[row["start"], row["end"]]])
        with self.locked():
            profiles = self.load()
            if existing_id and existing_id not in profiles:
                raise ValueError("この登録話者は削除されています。登録済みの話者を選び直してください。")
            target = existing_id
            if not target:
                named = [key for key, p in profiles.items() if p["name"] == name]
                if len(named) > 1:
                    raise ValueError("同じ名前の登録が複数あります。「登録済みの話者」から選択してください。")
                target = named[0] if named else "voice_" + uuid.uuid4().hex
            now = datetime.now(timezone.utc).isoformat()
            if target not in profiles:
                profiles[target] = {"name": name, "created": now, "updated": now,
                                    "pending_only": True, "clips": {}, "pool_revision": 0}
            profile = profiles[target]
            clips = profile.setdefault("clips", {})
            changed, obsolete = set(), []
            for row in rows:
                key = row["key"]
                if key in profile.get("removed_clips", []):
                    continue
                previous = clips.get(key)
                for other, owner in profiles.items():
                    if other != target and key in owner.get("clips", {}):
                        previous = owner["clips"].pop(key)
                        changed.add(other)
                        retract(profiles, {key})
                same_span = previous and previous["start"] == row["start"] and previous["end"] == row["end"]
                reference = {"source": str(source), "origin": origin, "start": row["start"], "end": row["end"],
                             "rejected": sorted(set(row.get("rejected", [])))}
                if previous:
                    reference["rejected"] = sorted((set(reference["rejected"]) | set(previous.get("rejected", []))) - {target})
                for field in ("title", "text", "mode"):
                    if field in row:
                        reference[field] = str(row[field])[:4000]
                    elif previous and field in previous:
                        reference[field] = previous[field]
                # A transcript can move; its persistent utterance key keeps its cached audio.
                if same_span and previous.get("file"):
                    reference.update({k: previous[k] for k in ("file", "sha256", "seconds", "origin") if k in previous})
                if previous and not same_span:
                    retract(profiles, {key})
                    if previous.get("file"):
                        obsolete.append(previous["file"])
                old = clips.get(key)
                if old and all(old.get(k) == v for k, v in reference.items()) and set(old) - {"updated"} == set(reference):
                    continue
                clips[key] = {**reference, "updated": now}
                changed.add(target)
            while len(clips) > MAX_CLIPS:
                key = min(clips, key=lambda k: clips[k].get("updated", ""))
                old = clips.pop(key)
                if old.get("file"):
                    obsolete.append(old["file"])
            for key in changed:
                profiles[key]["pool_revision"] = profiles[key].get("pool_revision", 0) + 1
                profiles[key]["updated"] = now
            self.save(profiles)
            self.delete_clip_files(obsolete)
            return target, profiles[target]["name"], changed

    def pool_snapshot(self, target):
        profile = self.load().get(target)
        if not profile:
            return None
        selected, duration = [], 0.
        for key, clip in sorted(profile.get("clips", {}).items(),
                                key=lambda item: item[1].get("updated", ""), reverse=True):
            if clip.get("excluded"):
                continue
            seconds = clip["end"] - clip["start"]
            if len(selected) >= 32 or duration + seconds > 60:
                continue
            selected.append({"key": key, **clip,
                             "cached": str(self.clip_path(clip["file"])) if clip.get("file") else ""})
            duration += seconds
        return {"target": target, "name": profile["name"], "revision": profile.get("pool_revision", 0),
                "checked": profile.get("pool_checked_revision", -1),
                "learned": profile.get("pool_learned_revision", -1), "clips": selected}

    def cache_pool(self, target, revision, prepared, checked=True):
        with self.locked():
            profiles = self.load()
            profile = profiles.get(target)
            if not profile or profile.get("pool_revision", 0) != revision:
                return False
            self.clips_dir.mkdir(parents=True, exist_ok=True)
            obsolete = []
            for item in prepared:
                clip = profile.get("clips", {}).get(item["key"])
                if clip is None:
                    return False
                with wave.open(str(item["path"]), "rb") as reader:
                    if (reader.getframerate(), reader.getnchannels(), reader.getsampwidth()) != (16000, 1, 2):
                        raise ValueError("蓄積音声の形式が正しくありません。")
                    frames = reader.getnframes()
                    if not 0 < frames <= 30 * 16000 + 1:
                        raise ValueError("蓄積音声の長さが正しくありません。")
                    digest = hashlib.sha256(reader.readframes(frames)).hexdigest()
                if clip.get("sha256") == digest and clip.get("file") and self.clip_path(clip["file"]).is_file():
                    continue
                name = uuid.uuid4().hex + ".wav"
                shutil.copyfile(item["path"], self.clip_path(name))
                old = clip.get("file")
                clip.update(file=name, sha256=digest, seconds=frames / 16000)
                if old:
                    obsolete.append(old)
            if checked:
                profile["pool_checked_revision"] = revision
            self.save(profiles)
            self.delete_clip_files(obsolete)
            return True

def read_pcm(path):
    with wave.open(str(path), "rb") as reader:
        if (reader.getframerate(), reader.getnchannels(), reader.getsampwidth()) != (16000, 1, 2):
            raise ValueError("蓄積音声の形式が正しくありません。")
        if reader.getnframes() > 30 * 16000 + 1:
            raise ValueError("蓄積音声が長すぎます。")
        return reader.readframes(reader.getnframes())

def run_pool(request):
    """No library writes here: UI commits only the current revision."""
    import subprocess
    import numpy as np
    from speaker_relearning import sample_from_pieces, WaitingForSpeech
    clips = request["clips"]
    prepared, decoded = [], []
    try:
        for item in clips:
            cached = Path(item["cached"]) if item.get("cached") else None
            if cached and cached.is_file():
                pcm = read_pcm(cached)
                if hashlib.sha256(pcm).hexdigest() != item["sha256"]:
                    raise ValueError("蓄積した音声の内容が変わっています。元の発言を修正し直してください。")
            else:
                source = Path(item["source"])
                if not source.is_file():
                    raise ValueError("まだ保存できていない発言の元音声が見つかりません。元の結果を開いて音声を確認してください。")
                proc = subprocess.run([str(request["ffmpeg"]), "-v", "error", "-nostdin",
                    "-ss", str(item["start"]), "-t", str(item["end"]-item["start"]), "-i", str(source),
                    "-vn", "-ac", "1", "-ar", "16000", "-f", "s16le", "pipe:1"],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                if proc.returncode:
                    raise ValueError("短い発言の音声を読み込めませんでした。")
                pcm = proc.stdout
                if not pcm:
                    raise ValueError("指定した発言の音声が空です。")
                path = Path(request["job"]) / (uuid.uuid4().hex + ".wav")
                with wave.open(str(path), "wb") as writer:
                    writer.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                    writer.writeframes(pcm)
                prepared.append({"key": item["key"], "path": str(path)})
            decoded.append((item, pcm))
        pieces, hashes, covered = [], set(), {}
        # Within one recording, count overlapping time only once. Across recordings,
        # exact repeated audio also contributes only once.
        for item, pcm in sorted(decoded, key=lambda pair: (pair[0]["origin"], pair[0]["start"])):
            digest = hashlib.sha256(pcm).hexdigest()
            if digest in hashes:
                continue
            hashes.add(digest)
            origin = item["origin"]
            cut = max(0, round((covered.get(origin, item["start"]) - item["start"]) * 16000))
            covered[origin] = max(covered.get(origin, 0), item["start"] + len(pcm) / 32000)
            audio = np.frombuffer(pcm, "<i2").astype("float32") / 32768
            if len(audio) > cut:
                pieces.append(audio[cut:])
        if not pieces or sum(map(len, pieces)) < 3 * 16000:
            raise WaitingForSpeech(sum(map(len, pieces)) / 16000)
        sample = sample_from_pieces(pieces, request["model"])
        return {"sample": sample, "prepared": prepared}
    except Exception as exc:
        return {"error": str(exc), "waiting_seconds": getattr(exc, "waiting_seconds", None), "prepared": prepared}

if __name__ == "__main__":
    import sys
    request = json.loads(Path(sys.argv[1]).read_text("utf-8"))
    print(json.dumps(run_pool(request), ensure_ascii=False), flush=True)
