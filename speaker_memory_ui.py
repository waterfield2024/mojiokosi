"""UI actions for persistent local voice names."""
from __future__ import annotations
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import tkinter as tk
from tkinter import ttk, messagebox
import uuid
from engine import ROOT, TOOLS, NO_WINDOW, resolve_cached_audio
from speaker_memory import MemoryStore, PROFILE_FILE

class SpeakerMemoryControls:
    def init_speaker_memory(self):
        from user_data import data_path
        self.speaker_memory = MemoryStore(data_path("speaker_memory.json", "WHISPER_SPEAKER_MEMORY"))
        self.memory_pending = {}
        self.correction_pending = {}
        self.correction_worker_lock = threading.Lock()
        self.memory_status = tk.StringVar(value="名前を付けると声を覚え、動画の文字起こしとリアルタイム字幕に使います。")

    def remember_named_speaker(self, speaker, name):
        from speakers import UNKNOWN, default_name
        if speaker in ("", UNKNOWN):
            self.memory_status.set("話者不明の声は覚えられません。先に発言の話者を修正してください。")
            return
        if not self.result_path or not self.result_data or name == default_name(speaker):
            return
        corrected = [row for row in self.segments if row.get("speaker") == speaker
                     and row.get("voice_learning_pending") and row.get("speaker_manual")]
        if corrected:
            existing = (self.result_data.get("speaker_memory_links", {}).get(speaker)
                        or self.result_data.get("remembered_speakers", {}).get(speaker, {}).get("id"))
            saved = self.speaker_memory.load().get(existing) if existing else None
            if saved and saved["name"] != name:
                self.speaker_memory.rename(existing, name)
            self.queue_corrected_speaker(corrected[-1])
            return
        folder = self.result_path.parent
        key = (str(folder.resolve()), speaker)
        intent = {"name": name, "token": uuid.uuid4().hex,
                  "existing": (self.result_data.get("speaker_memory_links", {}).get(speaker)
                               or self.result_data.get("remembered_speakers", {}).get(speaker, {}).get("id"))}
        self.memory_pending[key] = intent
        self.memory_status.set(f"「{name}」の声を確認しています…")
        if intent["existing"]:
            try:
                profile = self.speaker_memory.load().get(intent["existing"])
                if profile and profile["name"] != name:
                    self.speaker_memory.rename(intent["existing"], name)
                    profile["name"] = name
                if profile and not profile.get("pending_only"):
                    # Renaming an already identified voice does not need to enroll its audio again.
                    self.complete_memory_profile(key, intent["token"], sample=profile)
                    return
                if profile and profile.get("pending_only") and (profile.get("clips") or profile.get("removed_clips") or profile.get("evidence_deleted")):
                    if profile["name"] != name:
                        self.speaker_memory.rename(intent["existing"], name)
                    self.memory_pending.pop(key, None)
                    if profile.get("evidence_deleted") or not self.queue_named_voice(speaker, name, intent["existing"]):
                        self.queue_voice_pool(intent["existing"])
                    return
            except Exception as exc:
                self.complete_memory_profile(key, intent["token"], error=str(exc))
                return
        if self.queue_named_voice(speaker, name, intent.get("existing")):
            self.memory_pending.pop(key, None)
            return
        if self.try_memory_profile(key):
            return
        if self.selected is not None:
            selected_row = self.segments[int(self.selected)]
            if selected_row.get("speaker") == speaker and selected_row["end"] - selected_row["start"] < 3:
                # Explicitly naming the selected short utterance can start a new
                # registered identity before enough audio exists for a voice vector.
                self.memory_pending.pop(key, None)
                selected_row.setdefault("voice_correction_key", uuid.uuid4().hex)
                selected_row["speaker_manual"] = True
                selected_row["speaker_review"] = False
                selected_row["voice_learning_pending"] = True
                self.queue_corrected_speaker(selected_row)
                return
        if self.live_speaker_editable():
            self.memory_status.set(f"「{name}」を記憶待ち：判別できる発言が3秒以上たまると保存します。")
            return
        self.extract_memory_profile(key, intent)

    def queue_named_voice(self, speaker, name, existing=None):
        """New enrollments retain the exact excerpts used, so they can be reviewed."""
        source = resolve_cached_audio(self.result_data, self.result_path.parent) or self.result_data.get("audio_file") or self.result_data.get("source", "")
        if not source or not Path(source).is_file():
            return False
        from speaker_relearning import correction_ranges
        rows, seconds = [], 0.
        for row in self.segments:
            if row.get("speaker") != speaker or row.get("speaker_review") and not row.get("speaker_manual"):
                continue
            try:
                correction_ranges([[row["start"], row["end"]]])
            except ValueError:
                continue
            length = row["end"] - row["start"]
            if len(rows) >= 32 or seconds + length > 60:
                continue
            row.setdefault("voice_correction_key", uuid.uuid4().hex)
            rows.append(row); seconds += length
        if not rows:
            return False
        try:
            refs = [{"key": r["voice_correction_key"], "start": r["start"], "end": r["end"],
                     "title": self.result_data.get("title", self.result_path.parent.name), "text": r.get("text", ""),
                     "mode": self.result_data.get("mode", "")} for r in rows]
            target, registered, changed = self.speaker_memory.stage_clips(name, refs, source, str(self.result_path.parent.resolve()), existing)
            self.result_data.setdefault("speaker_memory_links", {})[speaker] = target
            self.speakers[speaker] = registered
            self.persist_edits()
            self.queue_voice_pool(target, speaker=speaker)
        except Exception as exc:
            self.memory_status.set(f"名前は反映済みです。学習音声を保存できませんでした：{exc}")
        return True

    def try_memory_profile(self, key):
        intent = self.memory_pending.get(key)
        if not intent:
            return True
        if not self.live_speaker_editable() and (not self.result_data or self.result_data.get("diarization", {}).get("engine") != "sherpa-onnx/streaming"):
            return False
        path = Path(key[0]) / PROFILE_FILE
        if not path.is_file():
            return False
        try:
            sample = json.loads(path.read_text("utf-8")).get("profiles", {}).get(key[1])
            if not sample:
                return False
            self.complete_memory_profile(key, intent["token"], sample=sample)
            return True
        except Exception as exc:
            self.complete_memory_profile(key, intent["token"], error=str(exc))
            return True

    def check_pending_memories(self):
        if not self.result_path:
            return
        folder = str(self.result_path.parent.resolve())
        for key in list(self.memory_pending):
            if key[0] == folder:
                self.try_memory_profile(key)

    def extract_memory_profile(self, key, intent):
        if intent.get("extracting"):
            return
        intent["extracting"] = True
        data = self.result_data
        source = resolve_cached_audio(data, Path(key[0])) or data.get("audio_file") or data.get("source", "")
        ranges = [[float(row["start"]), float(row["end"])] for row in self.segments
                  if row.get("speaker") == key[1] and
                  (not row.get("speaker_review") or row.get("speaker_manual"))]
        if not source or not Path(source).is_file() or not ranges:
            self.complete_memory_profile(key, intent["token"], error="声を登録できる録音・発言がありません。")
            return
        from context_ai import write_json
        job = ROOT / ".cache" / "speaker-memory" / intent["token"]
        job.mkdir(parents=True, exist_ok=True)
        request = job / "request.json"
        write_json(request, {"source": str(source), "ranges": ranges,
                             "model": str(ROOT / "models/speakers/embedding.onnx"), "ffmpeg": str(TOOLS / "ffmpeg.exe")})
        python = Path(sys.executable).with_name("python.exe")
        def work():
            event = {"kind": "speaker_memory_sample", "key": key, "token": intent["token"]}
            try:
                result = subprocess.run([str(python), "-X", "utf8", str(ROOT / "speaker_memory.py"), str(request)],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180, creationflags=NO_WINDOW)
                event.update(json.loads(result.stdout.decode("utf-8")))
            except Exception as exc:
                event["error"] = str(exc)
            self.events.put(event)
        threading.Thread(target=work, daemon=True).start()

    def complete_memory_profile(self, key, token, sample=None, error=None):
        key = tuple(key)
        intent = self.memory_pending.get(key)
        if not intent or intent["token"] != token:
            return
        try:
            if error:
                raise ValueError(error)
            profile = self.speaker_memory.remember(intent["name"], sample, intent.get("existing"))
            if (self.result_path and str(self.result_path.parent.resolve()) == key[0]
                    and self.speakers.get(key[1]) == intent["name"]):
                self.result_data.setdefault("speaker_memory_links", {})[key[1]] = profile
                self.persist_edits()
            self.memory_status.set(f"「{intent['name']}」を覚えました。次回も同じ声に名前を表示します。")
        except Exception as exc:
            self.memory_status.set(f"名前は反映済みですが、声は記憶できませんでした：{exc}")
        finally:
            self.memory_pending.pop(key, None)

    def invalidate_voice_corrections(self, keys, suspend=False):
        keys = {k for k in keys if k}
        for task, intent in list(self.correction_pending.items()):
            if any(r["key"] in keys for r in intent["rows"]):
                self.correction_pending.pop(task, None)
        affected = self.speaker_memory.forget_correction_members(keys, suspend=suspend)
        for row in self.segments:
            if row.get("voice_correction_key") in affected:
                row["voice_learning_pending"] = True
                row.pop("voice_correction", None)
        return affected

    def learn_corrected_speaker(self, row, previous=None):
        if not self.result_path or self.result_data is None:
            return
        row.setdefault("voice_correction_key", uuid.uuid4().hex)
        speaker = row.get("speaker", "")
        links = {k: v["id"] for k, v in self.result_data.get("remembered_speakers", {}).items() if v.get("id")}
        links.update(self.result_data.get("speaker_memory_links", {}))
        try:
            target_profile = self.speaker_memory.load().get(links.get(speaker), {})
        except Exception as exc:
            self.memory_status.set(f"話者の修正は反映済みです。声の記録を確認できませんでした：{exc}")
            return
        if row["voice_correction_key"] in target_profile.get("removed_clips", []):
            row["voice_learning_pending"] = False
            row.pop("voice_correction", None)
            self.memory_status.set("この発言は、この話者の学習から外されています。学習音声の一覧から正しい話者へ移動できます。")
            return
        rejected = set(row.get("voice_rejected_profiles", []))
        if previous != speaker and links.get(previous) and links.get(previous) != links.get(speaker):
            rejected.add(links[previous])
        row["voice_rejected_profiles"] = sorted(rejected)
        row["voice_learning_pending"] = True
        folder = str(self.result_path.parent.resolve())
        for session_speaker in (speaker, previous):
            self.memory_pending.pop((folder, session_speaker), None)
        if previous is not None and previous != speaker:
            try:
                self.invalidate_voice_corrections([row["voice_correction_key"]], suspend=True)
            except Exception as exc:
                self.memory_status.set(f"話者の修正は反映済みです。以前の学習を更新できませんでした：{exc}")
                return
            remaining = [r for r in self.segments if r.get("speaker") == previous
                         and r.get("speaker_manual") and r.get("voice_learning_pending")]
            if remaining:
                self.queue_corrected_speaker(remaining[-1])
        self.queue_corrected_speaker(row)

    def queue_corrected_speaker(self, row):
        from speakers import UNKNOWN, default_name
        from speaker_relearning import correction_ranges
        speaker = row.get("speaker", "")
        name = self.speakers.get(speaker, "")
        if speaker in ("", UNKNOWN) or not name or name == default_name(speaker):
            self.memory_status.set("話者を修正しました。声を覚え直すには名前を付けるか、「登録済みの話者」から選んでください。")
            return
        folder = str(self.result_path.parent.resolve())
        source = resolve_cached_audio(self.result_data, Path(folder)) or self.result_data.get("audio_file") or self.result_data.get("source", "")
        candidates, seconds = [], 0.
        try:
            for candidate in [row] + [r for r in reversed(self.segments) if r is not row]:
                if candidate.get("speaker") != speaker or not candidate.get("speaker_manual"):
                    continue
                start, end = float(candidate["start"]), float(candidate["end"])
                try:
                    correction_ranges([[start, end]])
                except ValueError:
                    if candidate is row:
                        raise
                    continue
                if len(candidates) >= 32 or seconds + end-start > 60:
                    continue
                candidate.setdefault("voice_correction_key", uuid.uuid4().hex)
                candidates.append(candidate)
                seconds += end-start
            if not candidates:
                return
            links = {k: v["id"] for k, v in self.result_data.get("remembered_speakers", {}).items() if v.get("id")}
            links.update(self.result_data.get("speaker_memory_links", {}))
            rows = [{"key": r["voice_correction_key"], "start": float(r["start"]), "end": float(r["end"]),
                     "rejected": r.get("voice_rejected_profiles", []), "text": r.get("text", ""),
                     "title": self.result_data.get("title", Path(folder).name),
                     "mode": self.result_data.get("mode", "")} for r in candidates]
            target, registered_name, changed = self.speaker_memory.stage_clips(
                name, rows, str(source), folder, links.get(speaker))
            self.result_data.setdefault("speaker_memory_links", {})[speaker] = target
            self.result_data.setdefault("speaker_name_edits", {})[speaker] = registered_name
            self.speakers[speaker] = registered_name
            removed = set(self.speaker_memory.load()[target].get("removed_clips", []))
            for candidate in candidates:
                candidate["voice_learning_pending"] = candidate["voice_correction_key"] not in removed
                if candidate["voice_correction_key"] in removed:
                    candidate.pop("voice_correction", None)
            self.refresh_speaker_rows()
            self.persist_edits()
            for old_target in changed - {target}:
                self.queue_voice_pool(old_target)
            self.queue_voice_pool(target, key=row["voice_correction_key"], speaker=speaker)
        except Exception as exc:
            self.memory_status.set(f"話者の修正は反映済みです。声の蓄積を更新できませんでした：{exc}")

    def queue_voice_pool(self, target, key=None, speaker=None, only_unchecked=False):
        snapshot = self.speaker_memory.pool_snapshot(target)
        if not snapshot:
            return
        if only_unchecked and snapshot["checked"] == snapshot["revision"]:
            return
        for task, intent in list(self.correction_pending.items()):
            if intent["existing"] == target:
                self.correction_pending.pop(task, None)
        if not snapshot["clips"]:
            self.memory_status.set(f"「{snapshot['name']}」には再学習用の音声がありません。新しい発言を追加できます。")
            return
        key = key or "pool:" + target
        clips = snapshot["clips"]
        intent = {"token": uuid.uuid4().hex, "existing": target, "name": snapshot["name"],
                  "revision": snapshot["revision"], "rows": clips, "speaker": speaker,
                  "folder": str(self.result_path.parent.resolve()) if self.result_path else "",
                  "rejected": sorted({v for c in clips for v in c.get("rejected", [])})}
        self.correction_pending[key] = intent
        count = len({c["origin"] for c in clips})
        self.memory_status.set(f"「{snapshot['name']}」の発言{len(clips)}件・{count}件の録音を確認しています…")
        from context_ai import write_json
        job = ROOT / ".cache" / "speaker-memory" / intent["token"]
        job.mkdir(parents=True, exist_ok=True)
        request = job / "request.json"
        write_json(request, {**snapshot, "job": str(job), "model": str(ROOT / "models/speakers/embedding.onnx"),
                             "ffmpeg": str(TOOLS / "ffmpeg.exe")})
        python = Path(sys.executable).with_name("python.exe")
        worker_lock, pending, events = self.correction_worker_lock, self.correction_pending, self.events
        def work():
            event = {"kind": "voice_correction_sample", "key": key, "token": intent["token"]}
            with worker_lock:
                if pending.get(key, {}).get("token") != intent["token"]:
                    return
                try:
                    result = subprocess.run([str(python), "-X", "utf8", str(ROOT / "speaker_pool.py"), str(request)],
                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180, creationflags=NO_WINDOW)
                    event.update(json.loads(result.stdout.decode("utf-8")))
                except Exception as exc:
                    event["error"] = str(exc)
            events.put(event)
        threading.Thread(target=work, daemon=True).start()

    def resume_correction_learning(self):
        if not self.result_path or not self.result_data:
            return
        pending = {}
        for row in self.segments:
            if row.get("speaker_manual") and row.get("voice_learning_pending"):
                pending[row.get("speaker")] = row
        for row in pending.values():
            self.queue_corrected_speaker(row)

    def resume_saved_voice_pools(self):
        if getattr(self, 'data_transfer_active', False):return
        try:
            for target, profile in self.speaker_memory.load().items():
                if profile.get("clips"):
                    self.queue_voice_pool(target, only_unchecked=True)
        except Exception as exc:
            self.memory_status.set(f"蓄積した声を再開できませんでした：{exc}")

    def complete_voice_correction(self, key, token, sample=None, error=None, waiting_seconds=None, prepared=None):
        intent = self.correction_pending.get(key)
        if not intent or intent["token"] != token:
            return
        self.correction_pending.pop(key, None)
        target, revision = intent["existing"], intent["revision"]
        # The persistent pool is authoritative even after switching to another result.
        current = self.speaker_memory.pool_snapshot(target)
        if not current:
            return
        if current["revision"] != revision:
            self.queue_voice_pool(target)
            return
        by_key = {r.get("voice_correction_key"): r for r in self.segments}
        for expected in intent["rows"]:
            row = by_key.get(expected["key"])
            if row and (row["start"] != expected["start"] or row["end"] != expected["end"]):
                return
        try:
            if not self.speaker_memory.cache_pool(target, revision, prepared or []):
                self.queue_voice_pool(target)
                return
            count = len({r["origin"] for r in intent["rows"]})
            if waiting_seconds is not None:
                self.memory_status.set(f"「{intent['name']}」の発言{len(intent['rows'])}件・{count}件を記憶待ち（音声{int(waiting_seconds * 10) / 10:.1f}秒／3秒）。別の動画・ライブでも同じ登録話者に修正すると追加できます。")
                return
            if error:
                raise ValueError(error)
            profile = self.speaker_memory.learn_correction(intent["name"], sample, "pool:" + target,
                        existing_id=target, rejected=intent["rejected"],
                        members=[r["key"] for r in intent["rows"]], expected_pool_revision=revision)
            if profile is None:
                self.queue_voice_pool(target)
                return
            for expected in intent["rows"]:
                row = by_key.get(expected["key"])
                if row and self.result_data.get("speaker_memory_links", {}).get(row.get("speaker")) == target:
                    row["voice_learning_pending"] = False
                    row["voice_correction"] = {"id": profile, "name": intent["name"]}
            if self.result_path:
                self.persist_edits()
            self.memory_status.set(f"「{intent['name']}」の声を覚え直しました（{len(intent['rows'])}行・{count}件の録音）。次回の字幕に使います。")
        except Exception as exc:
            self.memory_status.set(f"話者の修正は反映済みです。声は覚え直せませんでした：{exc}")

    def use_registered_speaker(self, speaker, target):
        profile = self.speaker_memory.load().get(target)
        if not profile:
            raise ValueError("この登録話者は削除されています。")
        previous_link = (self.result_data.get("speaker_memory_links", {}).get(speaker)
                         or self.result_data.get("remembered_speakers", {}).get(speaker, {}).get("id"))
        if self.selected is not None:
            chosen = self.segments[int(self.selected)]
            if chosen.get("speaker") == speaker:
                chosen.setdefault("voice_correction_key", uuid.uuid4().hex)
                chosen["speaker_manual"] = True
                chosen["speaker_review"] = False
                chosen["voice_learning_pending"] = True
        rows = [r for r in self.segments if r.get("speaker") == speaker and r.get("speaker_manual")
                and r.get("voice_correction_key") not in profile.get("removed_clips", [])]
        if previous_link and previous_link != target:
            self.invalidate_voice_corrections([r.get("voice_correction_key") for r in rows], suspend=True)
            for row in rows:
                row["voice_rejected_profiles"] = sorted(set(row.get("voice_rejected_profiles", [])) | {previous_link})
                row["voice_learning_pending"] = True
        self.result_data.setdefault("speaker_memory_links", {})[speaker] = target
        self.result_data.setdefault("speaker_name_edits", {})[speaker] = profile["name"]
        self.speakers[speaker] = profile["name"]
        self.refresh_speaker_rows()
        self.persist_edits()
        if rows:
            self.queue_corrected_speaker(rows[-1])
        else:
            self.memory_status.set(f"「{profile['name']}」を選びました。学習する発言を「この行の話者」でこの話者に修正してください。")

    def move_voice_learning(self, source, destination, key):
        return self.move_voice_learning_bulk(source, destination, [key])

    def move_voice_learning_bulk(self, source, destination, keys):
        outcome = self.speaker_memory.move_learning_clips(source, destination, keys)
        self.finish_voice_learning_change(outcome, destination)
        return outcome

    def delete_voice_learning(self, target, kind, key):
        return self.delete_voice_learning_bulk(target, [{"kind": kind, "key": key}])

    def delete_voice_learning_bulk(self, target, entries):
        outcome = self.speaker_memory.delete_learning_entries(target, entries)
        self.finish_voice_learning_change(outcome)
        return outcome

    def undo_voice_learning(self, expected_id):
        outcome = self.speaker_memory.undo_review_change(expected_id)
        self.finish_voice_learning_change(outcome, outcome["target"])
        return outcome

    def finish_voice_learning_change(self, outcome, destination=None):
        self.memory_pending.clear()
        for task, intent in list(self.correction_pending.items()):
            if intent["existing"] in outcome["targets"]:
                self.correction_pending.pop(task, None)
        for row in self.segments:
            if row.get("voice_correction_key") in outcome["members"]:
                row["voice_learning_pending"] = False
                row.pop("voice_correction", None)
        # A batch advances each pool once and starts at most one rebuild per person.
        order = sorted(outcome["targets"] - {destination})
        if destination is not None:
            order.append(destination)
        problems = []
        for person in order:
            try:
                self.queue_voice_pool(person)
            except Exception as exc:
                problems.append(str(exc))
        if self.result_path:
            try:
                self.persist_edits()
            except OSError as exc:
                problems.append(str(exc))
        if problems:
            outcome["warning"] = "学習音声の変更は保存済みです。再学習または結果の保存を再試行してください：" + " / ".join(problems)
            self.memory_status.set(outcome["warning"])

    def open_voice_review(self, target):
        from speaker_review_ui import VoiceReviewWindow
        previous = getattr(self, "voice_review", None)
        if previous and not previous.closed:
            previous.close()
        self.voice_review = VoiceReviewWindow(self, target)
        return self.voice_review

    def open_speaker_memory(self):
        previous = self.window_settings.windows.get("speaker_memory")
        if previous is not None and previous.winfo_exists():
            previous.refresh_speakers()
            self.window_settings.show("speaker_memory", previous)
            return previous
        window = tk.Toplevel(self.root)
        window.withdraw()
        window.title("覚えた話者")
        window.geometry("740x460"); window.minsize(680, 420)
        window.transient(self.root)
        frame = ttk.Frame(window, padding=16); frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="声と名前はこのPC内に保存され、YouTube・動画・リアルタイム字幕で使われます。",
                  wraplength=520).pack(anchor="w", pady=(0, 10))
        tree = ttk.Treeview(frame, columns=("name", "updated"), show="headings", selectmode="browse", height=8)
        tree.heading("name", text="覚えた名前"); tree.heading("updated", text="更新日")
        tree.column("name", width=330); tree.column("updated", width=170)
        tree.pack(fill="both", expand=True)
        name = tk.StringVar()
        entry = ttk.Entry(frame, textvariable=name); entry.pack(fill="x", pady=10)
        def refresh(selected=None):
            try:
                profiles = self.speaker_memory.load()
                tree.delete(*tree.get_children())
                for key, p in profiles.items():
                    tree.insert("", "end", iid=key, values=(p["name"], "声を記憶待ち" if p.get("pending_only") else p.get("updated", "")[:10]))
                if selected and tree.exists(selected):
                    tree.selection_set(selected)
                if not profiles:
                    self.memory_status.set("登録はまだありません。「話者の名前を設定」で声と名前を覚えます。")
            except Exception as exc:
                messagebox.showerror("覚えた話者を開けません", str(exc), parent=window)
        def select(event=None):
            chosen = tree.selection()
            if chosen:
                name.set(tree.item(chosen[0])["values"][0])
        def review():
            chosen = tree.selection()
            if chosen:
                self.open_voice_review(chosen[0])
        def rename():
            chosen = tree.selection()
            if not chosen:
                return
            try:
                self.speaker_memory.rename(chosen[0], name.get())
                # A later explicit library rename supersedes queued enrollments.
                self.memory_pending.clear()
                self.correction_pending.clear()
                refresh(chosen[0])
                self.memory_status.set("自動で付ける名前を変更しました。")
            except Exception as exc:
                messagebox.showerror("名前を変更できません", str(exc), parent=window)
        def forget():
            chosen = tree.selection()
            if not chosen:
                return
            label = tree.item(chosen[0])["values"][0]
            if not messagebox.askyesno("覚えた話者を削除", f"「{label}」の声と名前の登録を削除しますか？", parent=window):
                return
            try:
                self.speaker_memory.forget(chosen[0])
                # Ignore in-flight registrations started before this explicit deletion.
                self.memory_pending.clear()
                self.correction_pending.clear()
                refresh(); name.set("")
                self.memory_status.set(f"「{label}」の登録を削除しました。")
            except Exception as exc:
                messagebox.showerror("削除できません", str(exc), parent=window)
        tree.bind("<<TreeviewSelect>>", select)
        tree.bind("<Double-1>", lambda event: review())
        entry.bind("<Return>", lambda event: rename())
        buttons = ttk.Frame(frame); buttons.pack(fill="x")
        for text, command in [("学習に使った音声…", review), ("名前を変更", rename), ("登録を削除", forget), ("一覧を更新", refresh)]:
            ttk.Button(buttons, text=text, command=command).pack(side="left", padx=(0, 6))
        def close():
            self.window_settings.remember("speaker_memory", window)
            window.destroy()
        ttk.Button(buttons, text="閉じる", command=close).pack(side="right")
        window.protocol("WM_DELETE_WINDOW", close)
        ttk.Label(frame, textvariable=self.memory_status, wraplength=520).pack(anchor="w", pady=(10, 0))
        # Reserve room for the actions even when the window is made smaller.
        for widget in frame.winfo_children():
            widget.pack_forget()
        for index, widget in enumerate(frame.winfo_children()):
            widget.grid(row=index, column=0, sticky="nsew" if widget is tree else "ew", pady=(0, 8))
            frame.rowconfigure(index, weight=1 if widget is tree else 0)
        frame.columnconfigure(0, weight=1)
        refresh()
        window.refresh_speakers = lambda: refresh(next(iter(tree.selection()), None))
        self.window_settings.attach(window, "speaker_memory")
        return window
