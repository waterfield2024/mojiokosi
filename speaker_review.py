"""Review and retract individual local voice examples."""
from datetime import datetime, timezone
from pathlib import Path
import hashlib
import subprocess
import tempfile
import wave
from copy import deepcopy
from speaker_history import HistoryStore

class ReviewStore(HistoryStore):
    def learning_entries(self, target):
        profile = self.load().get(target)
        if profile is None:
            raise ValueError("この登録話者は削除されています。")
        clips = profile.get("clips", {})
        learned = {key for c in profile.get("corrections", []) for key in c.get("members", [c["key"]])}
        rows = []
        for key, clip in sorted(clips.items(), key=lambda x: x[1].get("updated", ""), reverse=True):
            rows.append({**clip, "key": key, "kind": "clip",
                         "state": "一時除外" if clip.get("excluded") else "学習済み" if key in learned else "学習待ち",
                         "playable": bool(clip.get("file") and self.clip_path(clip["file"]).is_file()
                                          or Path(clip["source"]).is_file())})
        if not profile.get("correction_only") and not profile.get("pending_only"):
            rows.append({"key": "baseline", "kind": "baseline", "state": "音声未保存",
                         "title": "初回登録（音声未保存）", "text": "音声を保存していない登録です。再生はできません。",
                         "updated": profile.get("created", ""), "playable": False})
        for correction in profile.get("corrections", []):
            members = correction.get("members", [correction["key"]])
            if not set(members).issubset(clips):
                rows.append({"key": correction["key"], "kind": "legacy", "state": "音声未保存",
                             "title": "音声のない学習記録", "text": "音声が残っていない学習です。削除すると、この学習記録全体を取り消します。",
                             "updated": correction.get("updated", ""), "playable": False})
        return rows

    def move_learning_clip(self, source, destination, key):
        return self.move_learning_clips(source, destination, [key])

    def move_learning_clips(self, source, destination, keys):
        """Validate a whole selection before switching ownership in one save."""
        from speaker_pool import MAX_CLIPS, retract
        if not isinstance(keys, (list, tuple)) or not keys or not all(isinstance(k, str) and k for k in keys):
            raise ValueError("移動する音声を選択してください。")
        keys = list(dict.fromkeys(keys))
        members = set(keys)
        with self.locked():
            profiles = self.load()
            before = deepcopy(profiles)
            if source == destination:
                raise ValueError("移動元と異なる話者を選んでください。")
            if source not in profiles or destination not in profiles:
                raise ValueError("話者の登録が変更されています。一覧から選び直してください。")
            origin, target = profiles[source], profiles[destination]
            if any(key not in origin.get("clips", {}) for key in keys):
                raise ValueError("選択した発言の一部はすでに削除または移動されています。一覧を確認してやり直してください。")
            if any(key in target.get("clips", {}) for key in keys):
                raise ValueError("同じ発言が移動先にも登録されています。内容を確認してください。")
            if len(target.get("clips", {})) + len(keys) > MAX_CLIPS:
                raise ValueError(f"移動先の保存上限は{MAX_CLIPS}件です。選択件数を減らすか、移動先の不要な発言を削除してください。")
            affected = {source, destination}
            for person, profile in profiles.items():
                if any(members.intersection(c.get("members", [c["key"]])) for c in profile.get("corrections", [])):
                    affected.add(person)
            retract(profiles, members)
            origin["removed_clips"] = sorted(set(origin.get("removed_clips", [])) | members)
            origin["evidence_deleted"] = True
            target["removed_clips"] = [k for k in target.get("removed_clips", []) if k not in members]
            now = datetime.now(timezone.utc).isoformat()
            for key in keys:
                clip = origin["clips"].pop(key)
                moved = {**clip, "updated": now,
                         "rejected": sorted((set(clip.get("rejected", [])) | {source}) - {destination})}
                moved.pop("excluded", None)
                target.setdefault("clips", {})[key] = moved
            self._revise_learning(profiles, affected, now)
            self.commit_review_change(profiles, before, kind="move", target=source,
                targets=affected, members=members, entries=[{"kind": "clip", "key": k} for k in keys])
            return {"members": members, "targets": affected, "source": source, "count": len(keys),
                    "destination": destination, "source_name": origin["name"], "name": target["name"]}

    def delete_learning_entry(self, target, kind, key):
        return self.delete_learning_entries(target, [{"kind": kind, "key": key}])

    def delete_learning_entries(self, target, entries):
        """Validate all records before retracting evidence and deleting any cache."""
        from speaker_pool import retract
        if not isinstance(entries, (list, tuple)) or not entries:
            raise ValueError("削除する学習記録を選択してください。")
        selected = []
        for entry in entries:
            if (not isinstance(entry, dict) or entry.get("kind") not in {"clip", "baseline", "legacy"}
                    or not isinstance(entry.get("key"), str) or not entry["key"]):
                raise ValueError("学習記録の種類が正しくありません。")
            item = (entry["kind"], entry["key"])
            if item not in selected:
                selected.append(item)
        with self.locked():
            profiles = self.load()
            before = deepcopy(profiles)
            profile = profiles.get(target)
            if profile is None:
                raise ValueError("この登録話者は削除されています。")
            members, changed, remove_baseline = set(), {target}, False
            # Read the unchanged snapshot first: overlapping legacy groups and
            # selected clips must not cause a partial delete or a false conflict.
            for kind, key in selected:
                if kind == "clip":
                    if key not in profile.get("clips", {}):
                        raise ValueError("選択した発言の一部はすでに削除または移動されています。一覧を確認してやり直してください。")
                    members.add(key)
                elif kind == "baseline":
                    if key != "baseline" or profile.get("correction_only") or profile.get("pending_only"):
                        raise ValueError("この初回登録はすでに削除されています。")
                    remove_baseline = True
                else:
                    found = next((c for c in profile.get("corrections", []) if c["key"] == key), None)
                    if found is None:
                        raise ValueError("選択した学習記録の一部はすでに削除されています。")
                    members.update(found.get("members", [key]))
            obsolete = []
            for key in members:
                clip = profile.get("clips", {}).pop(key, None)
                if clip and clip.get("file"):
                    obsolete.append(clip["file"])
            if remove_baseline:
                profile["correction_only"] = True
            if members:
                for person, owner in profiles.items():
                    if any(members.intersection(c.get("members", [c["key"]])) for c in owner.get("corrections", [])):
                        changed.add(person)
                retract(profiles, members)
                profile["removed_clips"] = sorted(set(profile.get("removed_clips", [])) | members)
            profile["evidence_deleted"] = True
            self._revise_learning(profiles, changed, datetime.now(timezone.utc).isoformat())
            self.commit_review_change(profiles, before, kind="delete", target=target,
                targets=changed, members=members, entries=[{"kind": k, "key": v} for k, v in selected], obsolete=obsolete)
            self.delete_clip_files(obsolete)
            return {"members": members, "targets": changed, "name": profile["name"], "count": len(selected)}

    @staticmethod
    def _revise_learning(profiles, changed, now):
        for person in changed:
            profile = profiles[person]
            profile["pool_revision"] = profile.get("pool_revision", 0) + 1
            profile["updated"] = now
            if profile.get("correction_only") and not profile.get("corrections"):
                profile["pending_only"] = True
                for field in ("embedding", "model", "seconds"):
                    profile.pop(field, None)


def preview_pcm(store, target, key, ffmpeg):
    """Read/cache one excerpt without training; never use a changed/deleted example."""
    from speaker_pool import read_pcm
    profile = store.load().get(target, {})
    revision = profile.get("pool_revision", 0)
    clip = profile.get("clips", {}).get(key)
    if not clip:
        raise ValueError("この発言はすでに削除または移動されています。")
    path = store.clip_path(clip["file"]) if clip.get("file") else None
    if path and path.is_file():
        pcm = read_pcm(path)
        if hashlib.sha256(pcm).hexdigest() != clip.get("sha256"):
            raise ValueError("保存音声の内容が変わっています。この発言を削除して、正しい音声を登録し直してください。")
    else:
        source = Path(clip["source"])
        if not source.is_file():
            raise ValueError("音声がまだ保存されておらず、元の録音も見つかりません。")
        result = subprocess.run([str(ffmpeg), "-v", "error", "-nostdin", "-ss", str(clip["start"]),
                                 "-t", str(clip["end"] - clip["start"]), "-i", str(source),
                                 "-vn", "-ac", "1", "-ar", "16000", "-f", "s16le", "pipe:1"],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        pcm = result.stdout
        if result.returncode or not 0 < len(pcm) <= 30 * 32000 + 2:
            raise ValueError("この発言の音声を読み込めませんでした。")
        with tempfile.TemporaryDirectory(prefix="whisper-voice-cache-") as tmp:
            extracted = Path(tmp) / "clip.wav"
            with wave.open(str(extracted), "wb") as writer:
                writer.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                writer.writeframes(pcm)
            if not store.cache_pool(target, revision, [{"key": key, "path": str(extracted)}], checked=False):
                raise ValueError("確認中に学習記録が更新されました。一覧から選び直してください。")
    current = store.load().get(target, {})
    if current.get("pool_revision", 0) != revision or key not in current.get("clips", {}):
        raise ValueError("確認中に学習記録が更新されました。一覧から選び直してください。")
    return pcm, revision
