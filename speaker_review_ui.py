"""Audio review window for a registered person's learning evidence."""
from platform_support import tool_path
from pathlib import Path
import queue
import tempfile
import threading
import time
import tkinter as tk
from tkinter import ttk, messagebox
import wave
from platform_support import play_wav
from engine import TOOLS, timestamp
from speaker_review import preview_pcm
from ui_theme import TranscriptTree

class VoiceReviewWindow:
    def __init__(self, app, target):
        self.app, self.target = app, target
        self.closed = False
        self.generation = 0
        self.playback = None
        self.preview_busy = False
        self.next_refresh = time.monotonic() + 1
        self.timer = None
        self.events = queue.Queue()
        self.window = tk.Toplevel(app.root)
        self.window.withdraw()
        self.window.title("学習に使った音声")
        self.window.geometry("940x620")
        self.window.minsize(760, 560)
        self.window.transient(app.root)
        self.window.protocol("WM_DELETE_WINDOW", self.close)
        self.window.bind("<Destroy>", self.on_destroy, add="+")
        frame = ttk.Frame(self.window, padding=16)
        frame.pack(fill="both", expand=True)
        self.heading = tk.StringVar()
        ttk.Label(frame, textvariable=self.heading, font=("Yu Gothic UI", 12, "bold")).pack(anchor="w")
        ttk.Label(frame, text="音声を選んで操作してください。複数選択は Ctrl＋クリック、範囲選択は Shift＋クリック。",
                  wraplength=720).pack(anchor="w", pady=(6, 10))
        selection_bar = ttk.Frame(frame); selection_bar.pack(fill="x")
        self.all_button = ttk.Button(selection_bar, text="すべて選択", command=self.toggle_all)
        self.all_button.pack(side="left")
        self.selection_count = tk.StringVar(value="0件選択")
        ttk.Label(selection_bar, textvariable=self.selection_count).pack(side="left", padx=8)
        table = ttk.Frame(frame)
        table.pack(fill="both", expand=True)
        columns = ("source", "time", "text", "state")
        self.tree = TranscriptTree(table, columns=columns, show="headings", selectmode="extended")
        for key, title, width in (("source", "動画・録音", 180), ("time", "発言の時間", 180),
                                  ("text", "発言の文章", 330), ("state", "状態", 90)):
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, minwidth=70, stretch=key=="text")
        y = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        x = ttk.Scrollbar(table, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=y.set, xscrollcommand=x.set)
        self.tree.grid(row=0, column=0, sticky="nsew"); y.grid(row=0, column=1, sticky="ns")
        x.grid(row=1, column=0, sticky="ew")
        table.rowconfigure(0, weight=1); table.columnconfigure(0, weight=1)
        self.detail = tk.StringVar()
        detail_label = ttk.Label(frame, textvariable=self.detail, wraplength=900)
        detail_label.pack(fill="x", pady=8)
        detail_label.bind("<Configure>", lambda event: event.widget.configure(wraplength=max(300, event.width-8)))
        bar = ttk.Frame(frame); bar.pack(fill="x")
        self.play_button = ttk.Button(bar, text="再生", width=8, style="Soft.TButton", command=self.play, state="disabled")
        self.play_button.pack(side="left", padx=(0, 16))
        ttk.Label(bar, text="移動先").pack(side="left", padx=(0, 8))
        self.move_ids = []
        self.move_combo = ttk.Combobox(bar, state="readonly", width=20)
        self.move_combo.pack(side="left", fill="x", expand=True)
        self.move_combo.bind("<<ComboboxSelected>>", self.update_move_button)
        self.move_button = ttk.Button(bar, text="移動", width=8, style="Accent.TButton", command=self.move, state="disabled")
        self.move_button.pack(side="left", padx=8)
        self.delete_button = ttk.Button(bar, text="削除", width=8, command=self.delete, state="disabled")
        self.delete_button.pack(side="left")
        self.status = tk.StringVar(value="別の人の声は、移動先を選んで「移動」。不要な音声は「削除」。")
        status_label = ttk.Label(frame, textvariable=self.status, wraplength=900)
        status_label.pack(fill="x", pady=(8, 0))
        status_label.bind("<Configure>", lambda event: event.widget.configure(wraplength=max(300, event.width-8)))
        learning_bar = ttk.Frame(frame); learning_bar.pack(fill="x")
        self.learning_notice = tk.StringVar(value="声の学習は自動で更新します。")
        ttk.Label(learning_bar, textvariable=self.learning_notice).pack(side="left")
        self.retry_button = ttk.Button(learning_bar, text="再試行", command=self.retry)
        bar2 = ttk.Frame(frame); bar2.pack(fill="x", pady=(8, 0))
        self.undo_id = None
        self.undo_button = ttk.Button(bar2, text="元に戻す", command=self.undo, state="disabled")
        self.undo_button.pack(side="left")
        ttk.Label(bar2, text="直前の移動・削除を取り消す").pack(side="left", padx=10)
        ttk.Button(bar2, text="閉じる", command=self.close).pack(side="right")
        # Allocate fixed controls first; only the list shrinks in a small window.
        for widget in frame.winfo_children():
            widget.pack_forget()
        for index, widget in enumerate(frame.winfo_children()):
            widget.grid(row=index, column=0, sticky="nsew" if widget is table else "ew", pady=(0, 7))
            frame.rowconfigure(index, weight=1 if widget is table else 0)
        frame.columnconfigure(0, weight=1)
        self.tree.bind("<<TreeviewSelect>>", self.select)
        self.tree.bind("<Control-a>", self.select_all)
        self.tree.bind("<Control-A>", self.select_all)
        self.tree.bind("<Double-1>", lambda event: self.play())
        self.refresh()
        app.window_settings.attach(self.window, "voice_review")
        self.poll_id = self.window.after(100, self.poll)

    def refresh(self):
        if self.closed:
            return
        self.seen_state = self.store_stamp()
        selected = {(row["kind"], row["key"]) for row in self.selected_rows()}
        self.stop()
        self.tree.delete(*self.tree.get_children())
        self.entries = {}
        try:
            profiles = self.app.speaker_memory.load()
            profile = profiles.get(self.target)
            self.refresh_destinations(profiles)
            if not profile:
                raise ValueError("この登録話者は削除されています。")
            rows = self.app.speaker_memory.learning_entries(self.target)
            count = sum(r["kind"] == "clip" for r in rows)
            self.heading.set(f"{profile['name']} の学習音声（{count}件）")
            for i, row in enumerate(rows):
                iid = str(i)
                self.entries[iid] = row
                title = row.get("title") or Path(row.get("origin", "")).name or "録音"
                when = (f"{timestamp(row['start'])} ～ {timestamp(row['end'])}"
                        if row["kind"] == "clip" else "区間の記録なし")
                self.tree.insert("", "end", iid=iid, values=(title, when, row.get("text") or "（文章の記録なし）", row["state"]))
                if (row["kind"], row["key"]) in selected:
                    self.tree.selection_add(iid)
            if not self.tree.selection() and rows:
                self.tree.selection_set("0")
            if not rows:
                self.status.set("学習用の音声はありません。正しい話者に修正した発言を追加してください。")
        except Exception as exc:
            self.status.set(str(exc))
        self.select()
        self.refresh_history()

    def refresh_destinations(self, profiles):
        index = self.move_combo.current()
        previous = self.move_ids[index] if 0 <= index < len(self.move_ids) else None
        self.move_ids = [person for person in profiles if person != self.target]
        numbers = {person: index + 1 for index, person in enumerate(profiles)}
        self.move_combo.configure(values=[f"{profiles[person]['name']}（登録{numbers[person]}）"
                                          for person in self.move_ids],
                                  state="readonly" if self.move_ids else "disabled")
        if previous in self.move_ids:
            self.move_combo.current(self.move_ids.index(previous))
        else:
            self.move_combo.set("移動先を選択" if self.move_ids else "別の登録話者がいません")
        self.update_move_button()

    def update_move_button(self, event=None):
        rows = self.selected_rows()
        index = self.move_combo.current()
        enabled = bool(rows and all(row["kind"] == "clip" for row in rows) and 0 <= index < len(self.move_ids))
        self.move_button.configure(state="normal" if enabled else "disabled")

    def move(self):
        rows = self.selected_rows()
        index = self.move_combo.current()
        if not rows or any(row["kind"] != "clip" for row in rows) or not 0 <= index < len(self.move_ids):
            return
        destination = self.move_ids[index]
        self.stop()
        try:
            result = self.app.move_voice_learning_bulk(self.target, destination, [row["key"] for row in rows])
            self.target = destination
            self.refresh()
            self.status.set(result.get("warning") or
                f"「{result['source_name']}」から「{result['name']}」へ{result['count']}件の発言を移しました。「元に戻す」で取り消せます。")
        except Exception as exc:
            messagebox.showerror("音声を移動できません", str(exc), parent=self.window)

    def selected_rows(self):
        entries = getattr(self, "entries", {})
        return [entries[iid] for iid in self.tree.selection() if iid in entries]

    def selected(self):
        rows = self.selected_rows()
        return rows[0] if len(rows) == 1 else None

    def toggle_all(self):
        if self.tree.get_children() and len(self.tree.selection()) == len(self.tree.get_children()):
            self.clear_selection()
        else:
            self.select_all()

    def select_all(self, event=None):
        self.tree.selection_set(self.tree.get_children())
        self.select()
        return "break"

    def clear_selection(self):
        self.tree.selection_remove(self.tree.selection())
        self.select()

    def select(self, event=None):
        self.stop()
        rows = self.selected_rows()
        row = rows[0] if len(rows) == 1 else None
        self.selection_count.set(f"{len(rows)}件選択")
        self.update_move_button()
        self.play_button.configure(state="normal" if row and row["playable"] else "disabled")
        self.delete_button.configure(state="normal" if rows else "disabled")
        all_selected = bool(rows) and len(rows) == len(self.tree.get_children())
        self.all_button.configure(text="選択を解除" if all_selected else "すべて選択",
                                  state="normal" if self.tree.get_children() else "disabled")
        if not rows:
            self.detail.set("操作する音声を選んでください。")
        elif any(r["kind"] != "clip" for r in rows):
            self.detail.set("音声のない旧登録が含まれています。削除はできますが、移動・再生はできません。")
        elif len(rows) > 1:
            self.detail.set(f"選んだ{len(rows)}件をまとめて移動・削除できます。再生は1件ずつ選んでください。")
        elif not row["playable"]:
            self.detail.set("この音声ファイルは見つからないため再生できません。")
        else:
            self.detail.set("「再生」で声を確認できます。")

    def play(self):
        if self.preview_busy:
            self.stop()
            self.status.set("再生を停止しました。")
            return
        row = self.selected()
        if not row or not row["playable"]:
            return
        if self.app.proc and self.app.operation == "realtime" or self.app.live_speaker_editable():
            self.status.set("再生音の再取り込みを避けるため、リアルタイム文字起こしを停止してから再生してください。")
            return
        self.stop()
        generation = self.generation
        self.preview_busy = True
        self.play_button.configure(text="停止", state="normal")
        self.status.set("音声を読み込んでいます…")
        store, target, events = self.app.speaker_memory, self.target, self.events
        def work():
            try:
                pcm, revision = preview_pcm(store, target, row["key"], tool_path('ffmpeg'))
                events.put((generation, row["key"], pcm, revision, None))
            except Exception as exc:
                events.put((generation, row["key"], None, None, str(exc)))
        threading.Thread(target=work, daemon=True).start()

    def poll(self):
        if self.closed:
            return
        try:
            while True:
                generation, key, pcm, revision, error = self.events.get_nowait()
                if generation != self.generation:
                    continue
                if error:
                    self.stop()
                    self.status.set(error)
                    continue
                try:
                    profile = self.app.speaker_memory.load().get(self.target, {})
                    if profile.get("pool_revision", 0) != revision or key not in profile.get("clips", {}):
                        raise ValueError("学習記録が変わりました。一覧から選び直してください。")
                    if self.app.proc and self.app.operation == "realtime" or self.app.live_speaker_editable():
                        raise ValueError("リアルタイム文字起こしを停止してから再生してください。")
                    # A private playback copy keeps deletion/re-caching from hitting a file in use.
                    self.playback = tempfile.TemporaryDirectory(prefix="whisper-voice-play-")
                    path = Path(self.playback.name) / "preview.wav"
                    with wave.open(str(path), "wb") as writer:
                        writer.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
                        writer.writeframes(pcm)
                    self.audio_process = play_wav(path, getattr(self, "audio_process", None))
                    self.status.set(f"再生中（{len(pcm)/32000:.1f}秒）")
                    self.timer = self.window.after(round(len(pcm)/32)+150, self.finish_playback)
                except Exception as exc:
                    self.stop()
                    self.status.set(f"再生できませんでした：{exc}")
        except queue.Empty:
            pass
        if time.monotonic() >= self.next_refresh:
            self.next_refresh = time.monotonic() + 1
            if not self.preview_busy and self.store_stamp() != self.seen_state:
                self.refresh()
            self.update_learning_notice()
        self.poll_id = self.window.after(100, self.poll)

    def store_stamp(self):
        try:
            stat = self.app.speaker_memory.path.stat()
            return stat.st_mtime_ns, stat.st_size
        except OSError:
            return None

    def update_learning_notice(self):
        text = self.app.memory_status.get()
        failed = any(word in text for word in ("ませんでした", "再試行"))
        if failed:
            self.learning_notice.set("声の更新に失敗しました。再試行できます。")
            if not self.retry_button.winfo_manager():
                self.retry_button.pack(side="left", padx=10)
        else:
            self.retry_button.pack_forget()
            if self.app.correction_pending:
                self.learning_notice.set("声を覚え直しています…")
            elif "記憶待ち" in text:
                self.learning_notice.set("音声が短いため、ほかの発言が増えると自動で覚えます。")
            else:
                self.learning_notice.set("声の学習は自動で更新します。")

    def finish_playback(self):
        self.timer = None
        self.stop()
        self.status.set("再生が終わりました。")

    def stop(self):
        self.generation += 1
        self.preview_busy = False
        if not self.closed:
            self.play_button.configure(text="再生")
        if self.timer is not None:
            self.window.after_cancel(self.timer)
            self.timer = None
        if self.playback:
            self.audio_process = play_wav(None, getattr(self, "audio_process", None))
            self.playback.cleanup()
            self.playback = None
            self.status.set("再生を停止しました。")

    def delete(self):
        rows = self.selected_rows()
        if not rows:
            return
        label = rows[0].get("text") or rows[0].get("title") or "選択した発言"
        question = (f"選択した{len(rows)}件の学習記録を削除しますか？" if len(rows) > 1
                    else f"「{str(label)[:120]}」を学習から削除しますか？")
        legacy = sum(row["kind"] != "clip" for row in rows)
        if legacy:
            question += f"\n音声未保存の記録{legacy}件を含みます。その学習記録全体を取り消します。"
        question += ("\n\n残った音声で覚え直します。元動画・録音・文字起こしの文章は残ります。"
                     "\n直近10操作は「元に戻す」で復元できます。復元用の音声はPC内に保管します。")
        if not messagebox.askyesno("学習用音声を削除", question, parent=self.window):
            return
        self.stop()
        try:
            result = self.app.delete_voice_learning_bulk(self.target, [{"kind": row["kind"], "key": row["key"]} for row in rows])
            self.refresh()
            self.status.set(result.get("warning") or
                            f"選択した{result['count']}件の学習記録を削除しました。「元に戻す」で復元できます。")
        except Exception as exc:
            messagebox.showerror("削除できません", str(exc), parent=self.window)

    def refresh_history(self):
        try:
            latest = self.app.speaker_memory.last_review_change()
            self.undo_id = latest["id"] if latest else None
            self.undo_button.configure(state="normal" if latest else "disabled")
            if latest and latest["id"] != getattr(self, "shown_undo_id", None):
                name = latest["before"][latest["target"]]["name"]
                action = "移動" if latest["kind"] == "move" else "削除"
                self.status.set(f"直前の操作：「{name}」の{latest['count']}件を{action}。「元に戻す」で取り消せます。")
            self.shown_undo_id = self.undo_id
        except Exception as exc:
            self.undo_id = None
            self.undo_button.configure(state="disabled")
            self.status.set(str(exc))

    def undo(self):
        if not self.undo_id:
            return
        self.stop()
        try:
            result = self.app.undo_voice_learning(self.undo_id)
            self.target = result["target"]
            self.refresh()
            wanted = {(e["kind"], e["key"]) for e in result["entries"]}
            self.tree.selection_set([iid for iid, row in self.entries.items()
                                     if (row["kind"], row["key"]) in wanted])
            self.select()
            label = "移動" if result["kind"] == "move" else "削除"
            self.status.set(result.get("warning") or
                f"{result['count']}件の{label}を元に戻しました。「{result['name']}」の音声を復元し、関係する話者を覚え直します。")
        except Exception as exc:
            messagebox.showerror("元に戻せません", str(exc), parent=self.window)
            self.refresh_history()

    def retry(self):
        try:
            self.app.queue_voice_pool(self.target)
        except Exception as exc:
            self.status.set(str(exc))

    def on_destroy(self, event):
        if event.widget == self.window and not self.closed:
            self.close(destroy=False)

    def close(self, destroy=True):
        if self.closed:
            return
        self.app.window_settings.remember("voice_review", self.window)
        self.closed = True
        self.stop()
        if hasattr(self, "poll_id"):
            self.window.after_cancel(self.poll_id)
        if destroy:
            self.window.destroy()
        # Release Tk variables on the UI thread, before an audio worker can
        # become the thread that collects an abandoned review window.
        self.heading = self.detail = self.status = self.selection_count = self.learning_notice = None
        self.app = None
