"""Persistent undo for explicit review operations, including deleted local audio."""
from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import shutil
import uuid

MAX_HISTORY = 10


class HistoryStore:
    @staticmethod
    def validate_history(history):
        from speaker_memory import MemoryStore
        if not isinstance(history, list) or len(history) > MAX_HISTORY:
            raise ValueError("取り消し履歴を読み込めません。")
        for item in history:
            if (not isinstance(item, dict) or item.get("kind") not in {"move", "delete"}
                    or not isinstance(item.get("id"), str) or not item["id"]
                    or not isinstance(item.get("count"), int) or item["count"] < 1):
                raise ValueError("取り消し履歴を読み込めません。")
            for field in ("members", "targets", "archived"):
                if not isinstance(item.get(field), list) or not all(isinstance(k, str) and k for k in item[field]):
                    raise ValueError("取り消し履歴を読み込めません。")
            for field in ("before", "after"):
                MemoryStore.validate_profiles(item[field])
                if set(item[field]) != set(item["targets"]):
                    raise ValueError("取り消し履歴を読み込めません。")
            if item.get("target") not in item["targets"]:
                raise ValueError("取り消し履歴を読み込めません。")
            if not isinstance(item.get("entries"), list) or not all(
                    isinstance(e, dict) and e.get("kind") in {"clip", "baseline", "legacy"}
                    and isinstance(e.get("key"), str) for e in item["entries"]):
                raise ValueError("取り消し履歴を読み込めません。")

    def review_history(self):
        if not self.path.exists():
            return []
        history = json.loads(self.path.read_text("utf-8")).get("history", [])
        self.validate_history(history)
        for item in history:
            for name in item["archived"]:
                self.clip_path(name)
        return history

    def last_review_change(self):
        history = self.review_history()
        return history[-1] if history else None

    def undo_audio_path(self, name):
        return self.clips_dir / ".undo" / self.clip_path(name).name

    def clean_undo_audio(self, history):
        keep = {name for item in history for name in item["archived"]}
        for path in (self.clips_dir / ".undo").glob("*.wav"):
            try:
                if path.name not in keep:
                    self.clip_path(path.name)  # Only our own UUID filenames.
                    path.unlink(missing_ok=True)
            except (ValueError, OSError):
                pass  # A committed operation must not be reported as failed.

    def commit_review_change(self, profiles, before, *, kind, target, targets, members,
                             entries, obsolete=()):
        history = self.review_history()
        archived, created = [], []
        try:
            for name in set(obsolete):
                source, backup = self.clip_path(name), self.undo_audio_path(name)
                if source.is_file():
                    backup.parent.mkdir(parents=True, exist_ok=True)
                    if not backup.exists():
                        created.append(backup)
                        shutil.copyfile(source, backup)
                    archived.append(name)
            record = {"id": uuid.uuid4().hex, "kind": kind, "target": target,
                      "count": len(entries), "created": datetime.now(timezone.utc).isoformat(),
                      "members": sorted(members), "targets": sorted(targets), "entries": entries,
                      "before": {k: before[k] for k in targets},
                      "after": {k: deepcopy(profiles[k]) for k in targets}, "archived": archived}
            history = (history + [record])[-MAX_HISTORY:]
            self.save(profiles, history=history)
        except Exception:
            for path in created:
                path.unlink(missing_ok=True)
            raise
        self.clean_undo_audio(history)

    @staticmethod
    def same_utterance(left, right):
        fields = ("source", "origin", "start", "end", "excluded", "rejected")
        return all(left.get(k) == right.get(k) for k in fields)

    def undo_review_change(self, expected_id):
        from speaker_pool import MAX_CLIPS, retract
        with self.locked():
            profiles = self.load()
            history = self.review_history()
            if not history or history[-1]["id"] != expected_id:
                raise ValueError("取り消し履歴が変わりました。一覧を確認してやり直してください。")
            record = history[-1]
            members, targets = set(record["members"]), set(record["targets"])
            before, after = record["before"], record["after"]
            if not targets.issubset(profiles):
                raise ValueError("関係する話者が削除されているため、元に戻せません。")
            if any(person not in targets and any(members.intersection(c.get("members", [c["key"]]))
                   for c in profile.get("corrections", [])) for person, profile in profiles.items()):
                raise ValueError("対象の発言はその後に別の話者へ学習されています。現在の学習を守るため、元に戻せません。")
            # Ignore subsequent cache writes and model rebuilds, but never undo a
            # later manual reassignment or a changed utterance range.
            for key in members:
                current_owners = {k for k, p in profiles.items() if key in p.get("clips", {})}
                expected_owners = {k for k, p in after.items() if key in p.get("clips", {})}
                if current_owners != expected_owners or any(not self.same_utterance(
                        profiles[k]["clips"][key], after[k]["clips"][key]) for k in current_owners):
                    raise ValueError("対象の発言はその後に修正されています。現在の学習を守るため、元に戻せません。")
            current = deepcopy(profiles)
            for person in targets:
                size = len(set(profiles[person].get("clips", {})) - members)
                size += len(set(before[person].get("clips", {})) & members)
                if size > MAX_CLIPS:
                    raise ValueError(f"復元先の保存上限{MAX_CLIPS}件を超えます。不要な発言を整理してください。")
            retract(profiles, members)
            restore_baseline = any(e["kind"] == "baseline" for e in record["entries"])
            for person in targets:
                profile, original = profiles[person], before[person]
                clips = profile.setdefault("clips", {})
                for key in members:
                    clips.pop(key, None)
                    old = original.get("clips", {}).get(key)
                    if old is not None:
                        restored = deepcopy(old)
                        # An uncached excerpt may have been cached after moving.
                        latest = next((p["clips"][key] for p in current.values()
                                       if key in p.get("clips", {})), None)
                        if latest and latest.get("file"):
                            for field in ("file", "sha256", "seconds"):
                                if field in latest:
                                    restored[field] = latest[field]
                        clips[key] = restored
                removed = set(profile.get("removed_clips", [])) - members
                removed.update(set(original.get("removed_clips", [])) & members)
                profile["removed_clips"] = sorted(removed)
                # Restore old grouped voice examples only if their other members
                # are still the same; the UI rebuilds the current pool afterwards.
                for correction in original.get("corrections", []):
                    used = set(correction.get("members", [correction["key"]]))
                    if not used.intersection(members):
                        continue
                    safe = True
                    for key in used - members:
                        old, now = original.get("clips", {}).get(key), clips.get(key)
                        if ((old is None) != (now is None) or old is not None and not self.same_utterance(old, now)
                                or key in removed):
                            safe = False
                    if safe:
                        existing = profile.setdefault("corrections", [])
                        existing[:] = [c for c in existing if c["key"] != correction["key"]]
                        existing.append(deepcopy(correction))
                        profile["corrections"] = existing[-64:]
                if restore_baseline and person == record["target"]:
                    for field in ("embedding", "model", "seconds"):
                        profile[field] = deepcopy(original[field])
                    profile.pop("pending_only", None)
                    profile.pop("correction_only", None)
                elif profile.get("pending_only") or profile.get("correction_only"):
                    profile["correction_only"] = True
                    if profile.get("corrections"):
                        for field in ("embedding", "model", "seconds"):
                            profile[field] = deepcopy(profile["corrections"][-1][field])
                        profile.pop("pending_only", None)
                # Preserve later unrelated removals; clear the marker when this
                # operation was the sole removal and its original had none.
                if not removed and not original.get("evidence_deleted"):
                    profile.pop("evidence_deleted", None)
            self._revise_learning(profiles, targets, datetime.now(timezone.utc).isoformat())
            self.validate_profiles(profiles)
            restored_files = []
            try:
                for person in targets:
                    for key in members.intersection(profiles[person].get("clips", {})):
                        clip = profiles[person]["clips"][key]
                        if not clip.get("file"):
                            continue
                        active, backup = self.clip_path(clip["file"]), self.undo_audio_path(clip["file"])
                        if not active.is_file() and backup.is_file():
                            active.parent.mkdir(parents=True, exist_ok=True)
                            restored_files.append(active)
                            shutil.copyfile(backup, active)
                        elif not active.is_file() and not Path(clip["source"]).is_file():
                            raise ValueError("復元用の音声が見つかりません。学習記録は変更していません。")
                self.save(profiles, history=history[:-1])
            except Exception:
                for path in restored_files:
                    path.unlink(missing_ok=True)
                raise
            self.clean_undo_audio(history[:-1])
            return {"members": members, "targets": targets, "target": record["target"],
                    "entries": record["entries"], "count": record["count"], "kind": record["kind"],
                    "name": profiles[record["target"]]["name"]}
