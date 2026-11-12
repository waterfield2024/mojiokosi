"""Speaker editing controls kept separate from the transcription worker."""
from __future__ import annotations
import json
import uuid
from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from speakers import default_name, normalize_names, speaker_name
from engine import timestamp, youtube_url, resolve_cached_audio


class SpeakerControls:
    def build_speaker_options(self, settings):
        self.num_speakers = tk.StringVar(value="自動")
        count = ttk.Combobox(settings, textvariable=self.num_speakers, state="readonly", width=6,
                             values=["自動"] + [str(i) for i in range(1, 31)])
        count.grid(row=1, column=2, sticky="ew", padx=(0, 14))

    def build_speaker_editor(self, parent, table_frame):
        bar = ttk.Frame(parent)
        bar.pack(side="bottom", fill="x", pady=(5, 3), before=table_frame)
        ttk.Label(bar, text="この行の話者").pack(side="left", padx=(0, 6))
        self.speaker_choice = tk.StringVar(value="未設定")
        self.speaker_ids = [""]
        self.speaker_combo = ttk.Combobox(bar, textvariable=self.speaker_choice, values=["未設定"],
                                         state="disabled", width=20)
        self.speaker_combo.pack(side="left")
        self.speaker_combo.bind("<<ComboboxSelected>>", self.assign_selected_speaker)
        self.names_button = ttk.Button(bar, text="話者の名前を設定", command=self.manage_speakers, state="disabled")
        self.names_button.pack(side="left", padx=8)
        self.memory_button = ttk.Button(bar, text="覚えた話者…", command=self.open_speaker_memory)
        self.memory_button.pack(side="left", padx=(0, 8))
        self.split_button = ttk.Button(bar, text="カーソル位置で行を分割", command=self.split_selected, state="disabled")
        self.split_button.pack(side="left")
        status = ttk.Label(parent, textvariable=self.memory_status, wraplength=780, font=("Yu Gothic UI", 9))
        status.pack(side="bottom", fill="x", pady=(1, 2), before=table_frame)
        status.bind("<Configure>", lambda event: status.configure(wraplength=max(200, event.width - 8)))
        self.memory_status_label = status
        return bar

    def speaker_count(self):
        return 0 if self.num_speakers.get() == "自動" else int(self.num_speakers.get())

    def row_values(self, s):
        label = speaker_name(s.get("speaker"), self.speakers) or "未設定"
        if s.get("speaker_review"):
            label += " ？"
        return (timestamp(s["start"])[:8], timestamp(s["end"])[:8], label, s["text"].replace("\n", " "))

    def refresh_speaker_choices(self):
        self.speakers = normalize_names(self.segments, self.speakers)
        self.speaker_ids = [""] + list(self.speakers)
        values = ["未設定"]
        for key, name in self.speakers.items():
            base = default_name(key)
            values.append(name if name == base else f"{name}（{base}）")
        self.speaker_combo.configure(values=values)
        key = self.segments[int(self.selected)].get("speaker", "") if self.selected is not None and self.segments else ""
        self.speaker_combo.current(self.speaker_ids.index(key) if key in self.speaker_ids else 0)

    def update_speaker_controls(self):
        editable = bool(self.segments) and (not self.proc or self.live_speaker_editable())
        self.names_button.configure(state="normal" if editable else "disabled")
        for button in (self.split_button, self.reanalyze_button):
            button.configure(state="normal" if self.segments and not self.proc else "disabled")
        self.speaker_combo.configure(state="readonly" if editable else "disabled")

    def refresh_speaker_rows(self):
        for i, segment in enumerate(self.segments):
            self.table.item(str(i), values=self.row_values(segment))
        self.refresh_speaker_choices()

    def assign_selected_speaker(self, event=None):
        if (self.proc and not self.live_speaker_editable()) or self.selected is None:
            return
        selected = self.speaker_combo.current()
        if not 0 <= selected < len(self.speaker_ids):
            return
        self.commit()
        segment = self.segments[int(self.selected)]
        previous = segment.get("speaker")
        segment["speaker"] = self.speaker_ids[selected]
        segment["speaker_review"] = False
        segment["speaker_manual"] = True
        self.table.item(self.selected, values=self.row_values(segment))
        self.persist_edits()
        self.stage = "この発言の話者を修正しました。"
        self.learn_corrected_speaker(segment, previous)
        self.persist_edits()

    def set_speaker_name(self, speaker_id, name):
        name = name.strip()
        if not name or len(name) > 30 or any(ord(c) < 32 for c in name):
            raise ValueError("名前は改行を含まない1〜30文字で入力してください。")
        self.speakers[speaker_id] = name
        if self.result_data is not None:
            self.result_data.setdefault("speaker_name_edits", {})[speaker_id] = name
        self.refresh_speaker_rows()
        self.persist_edits()
        self.stage = "話者名を更新しました。字幕にも反映されます。"
        self.remember_named_speaker(speaker_id, name)

    def add_speaker(self):
        number = 0
        while f"speaker_{number:02d}" in self.speakers:
            number += 1
        key = f"speaker_{number:02d}"
        if self.live_speaker_editable():
            # The worker may have identified a new voice that has not reached the UI yet.
            # Use a separate namespace for manual additions during capture.
            number = 1
            while f"追加話者{number}" in self.speakers:
                number += 1
            key = f"追加話者{number}"
        self.speakers[key] = default_name(key)
        self.refresh_speaker_choices()
        self.persist_edits()
        return key

    def manage_speakers(self):
        if (self.proc and not self.live_speaker_editable()) or not self.segments:
            return
        self.commit()
        previous = self.window_settings.windows.get("speaker_names")
        if previous is not None and previous.winfo_exists():
            self.window_settings.show("speaker_names", previous)
            return previous
        window = tk.Toplevel(self.root)
        window.withdraw()
        window.title("話者の名前を設定")
        window.geometry("680x540")
        window.minsize(620, 500)
        window.transient(self.root)
        frame = ttk.Frame(window, padding=16)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="名前を変更すると、その話者のすべての発言・字幕に反映されます。").pack(anchor="w", pady=(0, 8))
        ttk.Label(frame, textvariable=self.memory_status, wraplength=570).pack(anchor="w", pady=(0, 8))
        ttk.Button(frame, text="覚えた話者を確認・削除…", command=self.open_speaker_memory).pack(anchor="w", pady=(0, 8))
        tree = ttk.Treeview(frame, columns=("id", "name"), show="headings", height=7, selectmode="browse")
        tree.heading("id", text="話者"); tree.heading("name", text="表示する名前")
        tree.column("id", width=130); tree.column("name", width=360)
        tree.pack(fill="both", expand=True)
        name = tk.StringVar()
        entry = ttk.Entry(frame, textvariable=name, font=("Yu Gothic UI", 11))
        entry.pack(side="bottom", fill="x", pady=10, before=tree)
        def refresh(selected=None):
            tree.delete(*tree.get_children())
            for key, value in self.speakers.items():
                tree.insert("", "end", iid=key, values=(default_name(key), value))
            if selected in self.speakers:
                tree.selection_set(selected)
                tree.see(selected)
                name.set(self.speakers[selected])
        def select(event=None):
            chosen = tree.selection()
            if chosen:
                name.set(self.speakers[chosen[0]])
        def apply():
            chosen = tree.selection()
            if not chosen:
                return
            try:
                self.set_speaker_name(chosen[0], name.get())
                refresh(chosen[0])
            except (ValueError, OSError) as exc:
                messagebox.showerror("名前を保存できません", str(exc), parent=window)
        def add():
            key = self.add_speaker()
            refresh(key)
            entry.focus_set()
            entry.selection_range(0, "end")
        registered = ttk.Frame(frame)
        registered.pack(side="bottom", fill="x", pady=(0, 8), before=entry)
        ttk.Label(registered, text="登録済みの話者").pack(side="left", padx=(0, 6))
        try:
            saved = self.speaker_memory.load()
        except ValueError:
            saved = {}
        saved_ids = list(saved)
        saved_choice = ttk.Combobox(registered, state="readonly", width=24,
            values=[f"{saved[k]['name']}（登録{i+1}）" for i,k in enumerate(saved_ids)])
        saved_choice.pack(side="left", fill="x", expand=True)
        if saved_ids:
            saved_choice.current(0)
        def use_saved():
            chosen, number = tree.selection(), saved_choice.current()
            if not chosen or not 0 <= number < len(saved_ids):
                return
            try:
                self.use_registered_speaker(chosen[0], saved_ids[number])
                refresh(chosen[0])
            except (ValueError, OSError) as exc:
                messagebox.showerror("話者を選択できません", str(exc), parent=window)
        ttk.Button(registered, text="この話者を使う", command=use_saved).pack(side="left", padx=(6, 0))
        tree.bind("<<TreeviewSelect>>", select)
        entry.bind("<Return>", lambda event: apply())
        row = ttk.Frame(frame); row.pack(side="bottom", fill="x", before=entry)
        ttk.Button(row, text="名前を反映", command=apply).pack(side="left")
        ttk.Button(row, text="新しい話者を追加", command=add).pack(side="left", padx=8)
        def close():
            self.window_settings.remember("speaker_names", window)
            window.destroy()
        ttk.Button(row, text="閉じる", command=close).pack(side="right")
        window.protocol("WM_DELETE_WINDOW", close)
        key = self.segments[int(self.selected)].get("speaker") if self.selected is not None else None
        refresh(key or next(iter(self.speakers), None))
        self.window_settings.attach(window, "speaker_names")
        if not self.live_speaker_editable():
            window.grab_set()
        refresh_after = None
        def refresh_new():
            nonlocal refresh_after
            if not window.winfo_exists():
                return
            for speaker, value in self.speakers.items():
                if not tree.exists(speaker):
                    tree.insert("", "end", iid=speaker, values=(default_name(speaker), value))
                else:
                    tree.item(speaker, values=(default_name(speaker), value))
            refresh_after = window.after(500, refresh_new)
        refresh_after = window.after(500, refresh_new)
        def dispose(event):
            if event.widget == window and refresh_after:
                window.after_cancel(refresh_after)
        window.bind("<Destroy>", dispose, add="+")
        return window

    def split_selected(self):
        if self.proc or self.selected is None:
            return
        raw = self.editor.get("1.0", "end-1c")
        offset = len(self.editor.get("1.0", "insert"))
        left, right = raw[:offset].strip(), raw[offset:].strip()
        if not left or not right:
            messagebox.showinfo("行を分割", "下の文章の途中をクリックして、分けたい位置にカーソルを置いてください。")
            return
        self.commit()
        i = int(self.selected)
        old = self.segments[i]
        cut = old["start"] + (old["end"] - old["start"]) * offset / len(raw)
        self.segments[i:i + 1] = [
            {**old, "segment_id": uuid.uuid4().hex, "text": left, "end": cut, "words": [], "timing_estimated": True},
            {**old, "segment_id": uuid.uuid4().hex, "text": right, "start": cut, "words": [], "timing_estimated": True}]
        try:
            self.invalidate_voice_corrections([old.get("voice_correction_key")])
        except Exception as exc:
            self.memory_status.set(f"行を分割しましたが、以前の学習を更新できませんでした：{exc}")
        for part in self.segments[i:i + 2]:
            for field in ("voice_correction_key", "voice_correction", "voice_learning_pending"):
                part.pop(field, None)
        self.selected = None
        self.table.delete(*self.table.get_children())
        for number, s in enumerate(self.segments):
            self.table.insert("", "end", iid=str(number), values=self.row_values(s))
        self.table.selection_set(str(i + 1))
        self.select()
        self.persist_edits()
        self.stage = "行を分割しました。時刻は文字量から推定しています。後半の話者を選び直せます。"

    def analyze_saved(self):
        if self.proc or not self.segments or not self.result_data:
            return
        try:
            self.persist_edits()
            data = json.loads(json.dumps(self.result_data))
            cached = resolve_cached_audio(data, self.result_path.parent)
            source = str(data.get("source") or data.get("video_source") or "")
            try:
                youtube_url(source)
                remote = True
            except ValueError:
                remote = False
            from result_media import MEDIA_EXTENSIONS, result_file
            selected = None
            local = result_file(self.result_path.parent, source, MEDIA_EXTENSIONS)
            if not cached and not remote and not local:
                source = filedialog.askopenfilename(title="同じ開始位置・長さの元の音声または動画を選ぶ")
                if not source:
                    return
                data["source"] = selected = source
            self.launch_worker({"operation": "diarize", "result": data, "selected_source": selected,
                                "result_folder": str(self.result_path.parent),
                                "num_speakers": self.speaker_count()})
        except Exception as exc:
            messagebox.showerror("話者を解析できません", str(exc))
