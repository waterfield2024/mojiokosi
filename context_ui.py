"""Contextual correction controls with durable before/after review."""
from __future__ import annotations

from copy import deepcopy
from difflib import SequenceMatcher
import tkinter as tk
from tkinter import messagebox, ttk

from context_ai import (SETTINGS_PATH, ensure_segment_ids, load_settings, save_settings,
                        review_state, set_review_applied, find_review_segment)
from engine import timestamp
from speakers import speaker_name
from ui_theme import style_text, style_change
import correction_memory


class ContextControls:
    def init_context(self):
        self.context_settings_path = SETTINGS_PATH
        self.context_window = None
        self.context_checkpoint = None
        self.pending_context_review = False
        try:
            settings = load_settings(self.context_settings_path)
        except (OSError, ValueError) as exc:
            settings = {"auto_apply": False}
            self.stage = f"文脈AI校正の設定を読み込めませんでした: {exc}"
        self.context_auto_apply = tk.BooleanVar(value=settings["auto_apply"])

    def build_context_controls(self, parent, inline=False):
        self.context_button = ttk.Button(parent, text="文脈AI校正", command=self.open_context_review)
        if inline:
            self.context_button.configure(style="Soft.TButton")
            self.context_button.pack(side="right", padx=(0, 8))
        else:
            self.context_button.grid(row=0, column=7, padx=(5, 0))

    def update_context_controls(self):
        self.context_button.configure(state="disabled" if self.proc and not self.live_speaker_editable() else "normal")

    def save_context_settings(self):
        try:
            save_settings(self.context_auto_apply.get(), self.context_settings_path)
            self.stage = "文字起こし後の文脈AI校正を" + ("ON" if self.context_auto_apply.get() else "OFF") + "にしました。"
        except OSError as exc:
            self.context_auto_apply.set(not self.context_auto_apply.get())
            messagebox.showerror("設定を保存できません", str(exc), parent=self.root)

    def open_context_review(self):
        if self.proc and not self.live_speaker_editable():
            return
        self.commit()
        if self.context_window is not None and self.context_window.winfo_exists():
            self.window_settings.show("context_review", self.context_window)
            self.context_window.refresh()
            return
        self.context_window = ContextReviewDialog(self)

    def start_context_correction(self):
        if self.proc or not self.segments or self.result_data is None:
            return
        try:
            self.persist_edits()
            data = deepcopy(self.result_data)
            ensure_segment_ids(data["segments"])
            if self.context_window is not None and self.context_window.winfo_exists():
                self.context_window.destroy()
            self.launch_worker({"operation": "context", "result": data,
                                "result_folder": str(self.result_path.parent),
                                "correction_rules": self.automatic_correction_rules()})
        except Exception as exc:
            messagebox.showerror("文脈校正を開始できません", str(exc), parent=self.root)

    def context_history(self):
        return self.result_data.get("context_review", []) if self.result_data else []

    def toggle_context_change(self, record, applied):
        if self.proc and not self.live_speaker_editable():
            return
        self.commit()
        set_review_applied(record, self.segments, applied)
        if self.live_speaker_editable():
            row = find_review_segment(record, self.segments)
            self.result_data.setdefault("live_text_edits", {})[record["segment_id"]] = {
                "text": row["text"], "words": deepcopy(row.get("words", []))}
            self.result_data.setdefault("live_context_review_states", {})[record["id"]] = record["status"]
            self.refresh_live_corrected_text()
            self.persist_edits()
            if self.overlay:
                self.overlay.update_text(record["segment_id"], row["text"])
        else:
            # Keep a prior live undo synchronized when reviewing the saved recording.
            if record.get("live") or record["segment_id"] in self.result_data.get("live_text_edits", {}):
                row = find_review_segment(record, self.segments)
                self.result_data.setdefault("live_text_edits", {})[record["segment_id"]] = {"text": row["text"], "words": deepcopy(row.get("words", []))}
                self.result_data.setdefault("live_context_review_states", {})[record["id"]] = record["status"]
            self.refresh_replaced_text()
        self.stage = ("修正を再適用しました。" if applied else
                      "元に戻して覚えました。次回から同じ表記と前後の言葉が一致する誤修正を止めます。")

    def show_context_segment(self, record):
        segment = find_review_segment(record, self.segments)
        if segment is None:
            return
        index = next(i for i, item in enumerate(self.segments) if item is segment)
        if self.context_window is not None and self.context_window.winfo_exists():
            self.context_window.destroy()
        self.table.selection_set(str(index))
        self.select()
        self.table.see(str(index))
        self.editor.focus_set()


class ContextReviewDialog(tk.Toplevel):
    def __init__(self, app):
        super().__init__(app.root)
        self.withdraw()
        self.app = app
        self.records = {}
        self.title("文脈AI校正 — 修正前後の確認")
        self.geometry("1060x700")
        self.minsize(860, 580)
        self.transient(app.root)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        frame = ttk.Frame(self, padding=12)
        frame.grid(sticky="nsew")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(3, weight=2)
        frame.rowconfigure(4, weight=3)
        ttk.Label(frame, text="手動で直した誤変換と、元に戻した修正を覚え、次回の動画・字幕に反映します。\n"
                  "同じ表記と前後の言葉が一致する場合に使います。記憶による修正も元に戻せます。", wraplength=800).grid(row=0, column=0, sticky="w")
        self.auto_check = ttk.Checkbutton(frame, text="文字起こし後に文脈AI校正を自動実行する（PC内で処理）",
                                         variable=app.context_auto_apply, command=app.save_context_settings)
        self.auto_check.grid(row=1, column=0, sticky="w", pady=(7, 5))
        if app.proc:
            self.auto_check.configure(state="disabled")
        actions = ttk.Frame(frame)
        actions.grid(row=2, column=0, sticky="ew", pady=(0, 8))
        self.run_button = ttk.Button(actions, text="今の文章を文脈で自動修正", command=app.start_context_correction,
                                     state="normal" if app.segments and not app.proc else "disabled")
        actions.columnconfigure(0, weight=1)
        self.run_button.grid(row=0, column=0, sticky="w")
        self.memory_button = ttk.Button(actions, text="覚えた修正…", command=self.open_memory)
        self.memory_button.grid(row=0, column=1, sticky="e")
        self.summary = tk.StringVar()
        ttk.Label(actions, textvariable=self.summary, wraplength=800).grid(row=1, column=0, columnspan=2, sticky="w", pady=(5, 0))
        table_frame = ttk.Frame(frame)
        table_frame.grid(row=3, column=0, sticky="nsew")
        self.tree = ttk.Treeview(table_frame, columns=("time", "speaker", "before", "after", "state"),
                                 show="headings", selectmode="browse", height=5)
        for key, title, width in (("time", "開始", 80), ("speaker", "話者", 90),
                                  ("before", "修正前", 260), ("after", "修正後", 260),
                                  ("state", "状態", 115)):
            self.tree.heading(key, text=title)
            self.tree.column(key, width=width, minwidth=60, stretch=key in {"before", "after"})
        scroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", self.select_record)
        preview = ttk.Frame(frame)
        preview.grid(row=4, column=0, sticky="nsew", pady=(10, 4))
        preview.columnconfigure(0, weight=1, uniform="preview")
        preview.columnconfigure(1, weight=1, uniform="preview")
        preview.rowconfigure(1, weight=1)
        self.texts = []
        for column, title in enumerate(("修正前（変更部分を赤で表示）", "修正後（変更部分を緑で表示）")):
            ttk.Label(preview, text=title).grid(row=0, column=column, sticky="w", padx=(0, 8))
            box = ttk.Frame(preview)
            box.grid(row=1, column=column, sticky="nsew", padx=(0, 8) if column == 0 else 0)
            text = tk.Text(box, wrap="word", height=7, width=25, font=("Yu Gothic UI", 11),
                           padx=8, pady=6, relief="solid", borderwidth=1)
            style_text(text)
            scrollbar = ttk.Scrollbar(box, orient="vertical", command=text.yview)
            text.configure(yscrollcommand=scrollbar.set)
            scrollbar.pack(side="right", fill="y")
            text.pack(side="left", fill="both", expand=True)
            style_change(text, "before" if column == 0 else "after")
            text.configure(state="disabled")
            self.texts.append(text)
        self.reason = tk.StringVar()
        ttk.Label(frame, textvariable=self.reason, wraplength=820).grid(row=5, column=0, sticky="w", pady=6)
        buttons = ttk.Frame(frame)
        buttons.grid(row=6, column=0, sticky="ew")
        self.undo_button = ttk.Button(buttons, text="この修正を元に戻す", command=lambda: self.toggle(False))
        self.undo_button.pack(side="left")
        self.redo_button = ttk.Button(buttons, text="修正を再適用", command=lambda: self.toggle(True))
        self.redo_button.pack(side="left", padx=6)
        self.show_button = ttk.Button(buttons, text="対象行を表示・編集", command=self.show_segment)
        self.show_button.pack(side="left")
        ttk.Button(buttons, text="閉じる", command=self.destroy).pack(side="right")
        self.refresh()
        app.window_settings.attach(self, "context_review")
        self.grab_set()

    def destroy(self):
        self.app.window_settings.remember("context_review", self)
        super().destroy()

    def refresh(self, selected=None):
        self.run_button.configure(state="normal" if self.app.segments and not self.app.proc else "disabled")
        self.auto_check.configure(state="disabled" if self.app.proc else "normal")
        if selected is None:
            chosen = self.tree.selection()
            selected = chosen[0] if chosen else None
        self.records = {r["id"]: r for r in self.app.context_history()}
        self.tree.delete(*self.tree.get_children())
        for record in reversed(list(self.records.values())):
            self.tree.insert("", "end", iid=record["id"], values=(
                timestamp(record["start"])[:8],
                speaker_name(record.get("speaker"), self.app.speakers) or "未設定",
                record["before"].replace("\n", " "), record["after"].replace("\n", " "),
                review_state(record, self.app.segments)))
        last = (self.app.result_data or {}).get("context_last_run")
        summary = f"修正履歴 {len(self.records)}行" + (f" / 今回 {last['changed_rows']}行を修正" if last else "")
        if last and last.get("rejected_changes"):
            summary += f" / {last['rejected_changes']}件は自動適用を見送り"
        if last and last.get("remembered_changes"):
            summary += f" / 覚えた誤修正 {last['remembered_changes']}件を防止"
        recalled = (self.app.result_data or {}).get("manual_memory_count", 0)
        if recalled:
            summary += f" / 手動の記憶で{recalled}か所修正"
        self.summary.set(summary)
        if selected not in self.records:
            selected = next(iter(reversed(self.records)), None)
        if selected is not None:
            self.tree.selection_set(selected)
            self.tree.see(selected)
        self.select_record()

    def selected_record(self):
        chosen = self.tree.selection()
        return self.records.get(chosen[0]) if chosen else None

    def select_record(self, event=None):
        record = self.selected_record()
        for text, key in zip(self.texts, ("before", "after")):
            text.configure(state="normal")
            text.delete("1.0", "end")
            text.insert("1.0", record[key] if record else "")
            text.configure(state="disabled")
        for button in (self.undo_button, self.redo_button, self.show_button):
            button.configure(state="disabled")
        if not record:
            self.reason.set("校正後に、修正前後の文章と理由がここに表示されます。")
            return
        for tag, a, b, c, d in SequenceMatcher(None, record["before"], record["after"], autojunk=False).get_opcodes():
            if tag != "equal":
                self.texts[0].tag_add("change", f"1.0+{a}c", f"1.0+{b}c")
                self.texts[1].tag_add("change", f"1.0+{c}c", f"1.0+{d}c")
        state = review_state(record, self.app.segments)
        editable = not self.app.proc or self.app.live_speaker_editable()
        self.undo_button.configure(state="normal" if editable and state == "適用中" else "disabled")
        self.redo_button.configure(state="normal" if editable and state == "元に戻した" else "disabled")
        self.show_button.configure(state="normal" if find_review_segment(record, self.app.segments) else "disabled")
        suffix = "（後から加えた編集は上書きしません。「対象行を表示・編集」で確認できます。）" if state == "その後に編集" else ""
        explanation = record["reason"][:200] + ("…" if len(record["reason"]) > 200 else "")
        label = "記憶による修正" if record.get("origin") == "manual_memory" else "AIの修正理由"
        self.reason.set(f"{label}：{explanation}\n状態：{state}{suffix}")

    def toggle(self, applied):
        record = self.selected_record()
        if record:
            try:
                self.app.toggle_context_change(record, applied)
                self.refresh(record["id"])
            except (OSError, ValueError) as exc:
                messagebox.showerror("修正を変更できません", str(exc), parent=self)

    def show_segment(self):
        record = self.selected_record()
        if record:
            self.app.show_context_segment(record)

    def open_memory(self):
        try:
            entries = correction_memory.list_entries()
            dialog = getattr(self, "memory_window", None)
            if dialog is not None and dialog.winfo_exists():
                dialog.refresh(entries)
                dialog.lift()
            else:
                self.memory_window = CorrectionMemoryDialog(self, entries)
        except (OSError, ValueError) as exc:
            messagebox.showerror("記憶を読み込めません", str(exc), parent=self)


class CorrectionMemoryDialog(tk.Toplevel):
    def __init__(self, parent, entries):
        super().__init__(parent)
        self.owner = parent
        self.title("覚えた修正")
        self.geometry("820x520")
        self.minsize(700, 440)
        self.transient(parent)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        frame = ttk.Frame(self, padding=16)
        frame.grid(sticky="nsew")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(2, weight=2)
        frame.rowconfigure(4, weight=1)
        ttk.Label(frame, text="手動修正は「自動で直す」、取り消した誤修正は「修正を止める」として覚えます。\n"
                  "前後の言葉も一致する場合に使います。不要な記憶は選んで解除できます。",
                  wraplength=650).grid(row=0, column=0, sticky="w")
        self.status = tk.StringVar()
        ttk.Label(frame, textvariable=self.status).grid(row=1, column=0, sticky="w", pady=8)
        table = ttk.Frame(frame)
        table.grid(row=2, column=0, sticky="nsew")
        self.tree = ttk.Treeview(table, columns=("kind", "before", "after"), show="headings", selectmode="extended", height=5)
        for key, title in (("kind", "記憶の使い方"), ("before", "修正前（前後の言葉を含む）"), ("after", "修正後")):
            self.tree.heading(key, text=title)
            self.tree.column(key, width=110 if key == "kind" else 270, minwidth=100 if key == "kind" else 150, stretch=key != "kind")
        scrollbar = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", self.select)
        ttk.Label(frame, text="記憶したときの文章").grid(row=3, column=0, sticky="w", pady=(10, 4))
        detail = ttk.Frame(frame)
        detail.grid(row=4, column=0, sticky="nsew")
        self.detail = tk.Text(detail, height=6, wrap="word", width=30, font=("Yu Gothic UI", 11), padx=8, pady=8)
        style_text(self.detail)
        scroll = ttk.Scrollbar(detail, orient="vertical", command=self.detail.yview)
        self.detail.configure(yscrollcommand=scroll.set, state="disabled")
        scroll.pack(side="right", fill="y")
        self.detail.pack(side="left", fill="both", expand=True)
        buttons = ttk.Frame(frame)
        buttons.grid(row=5, column=0, sticky="ew", pady=(12, 0))
        self.forget_button = ttk.Button(buttons, text="選択した記憶を解除", command=self.forget_selected)
        self.forget_button.pack(side="left")
        ttk.Button(buttons, text="閉じる", command=self.close).pack(side="right")
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.refresh(entries)
        self.grab_set()

    def refresh(self, entries):
        self.entries = {e["id"]: e for e in entries}
        self.tree.delete(*self.tree.get_children())
        for entry in reversed(entries):
            if entry["exact"]:
                before, after = entry["before"], entry["after"]
            else:
                before = entry["left"] + entry["source"] + entry["right"]
                after = entry["left"] + entry["target"] + entry["right"]
            kind = "自動で直す" if entry.get("kind") == "replace" else "修正を止める"
            self.tree.insert("", "end", iid=entry["id"], values=(kind, before.replace("\n", " "), after.replace("\n", " ")))
        self.status.set(f"{len(entries)}件を記憶しています。" if entries else
                        "記憶はまだありません。手動で誤変換を直したり、修正を元に戻すと追加されます。")
        self.select()

    def select(self, event=None):
        ids = self.tree.selection()
        self.forget_button.configure(state="normal" if ids else "disabled")
        entry = self.entries.get(ids[0]) if ids else None
        if entry and entry.get("kind") == "replace":
            text = f"修正前：\n{entry['before']}\n\n正しい表記として覚えた文章：\n{entry['after']}"
        else:
            text = f"残す文章：\n{entry['before']}\n\n取り消した修正：\n{entry['after']}" if entry else "一覧から記憶を選んで確認できます。"
        self.detail.configure(state="normal")
        self.detail.delete("1.0", "end")
        self.detail.insert("1.0", text)
        self.detail.configure(state="disabled")

    def forget_selected(self):
        ids = self.tree.selection()
        if not ids:
            return
        try:
            correction_memory.forget(ids)
            self.refresh(correction_memory.list_entries())
            self.status.set(f"{len(ids)}件の記憶を解除しました。文章はそのまま残ります。")
        except (OSError, ValueError) as exc:
            messagebox.showerror("記憶を解除できません", str(exc), parent=self)

    def close(self):
        self.destroy()
        if self.owner.winfo_exists():
            self.owner.grab_set()
