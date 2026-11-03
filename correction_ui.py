"""Bulk replacement and dictionary controls for the desktop UI."""
from __future__ import annotations

from copy import deepcopy
import tkinter as tk
from tkinter import messagebox, ttk

from corrections import DEFAULT_PATH, load_settings, normalize_rules, replace_segments, save_settings


class CorrectionControls:
    def init_corrections(self):
        self.correction_path = DEFAULT_PATH
        self.replacement_undo = []
        self.dictionary_window = None
        try:
            self.correction_settings = load_settings(self.correction_path)
        except (OSError, ValueError) as exc:
            self.correction_settings = {"version": 1, "auto_apply": False, "rules": []}
            self.stage = f"誤変換辞書を読み込めませんでした: {exc}"

    def build_correction_controls(self, parent, table_frame, toolbar=None):
        bar = ttk.Frame(parent)
        self.correction_bar = bar
        self.replacement_table_frame = table_frame
        if toolbar is None:
            bar.pack(side="top", fill="x", pady=(0, 8), before=table_frame)
        else:
            self.replacement_toggle = ttk.Button(toolbar, text="検索・置換", command=self.toggle_replacement_bar)
            self.replacement_toggle.pack(side="right", padx=(0, 8))
        bar.columnconfigure(1, weight=1)
        bar.columnconfigure(3, weight=1)
        self.find_text = tk.StringVar()
        self.replace_text = tk.StringVar()
        ttk.Label(bar, text="検索").grid(row=0, column=0, padx=(0, 5))
        self.find_entry = ttk.Entry(bar, textvariable=self.find_text, width=8)
        self.find_entry.grid(row=0, column=1, sticky="ew")
        ttk.Label(bar, text="置換後").grid(row=0, column=2, padx=(10, 5))
        self.replace_entry = ttk.Entry(bar, textvariable=self.replace_text, width=8)
        self.replace_entry.grid(row=0, column=3, sticky="ew")
        self.replace_all_button = ttk.Button(bar, text="すべて置換", command=self.replace_all)
        self.replace_all_button.grid(row=0, column=4, padx=(8, 5))
        self.undo_replace_button = ttk.Button(bar, text="置換を戻す", command=self.undo_replacement)
        self.undo_replace_button.grid(row=0, column=5, padx=(0, 5))
        self.dictionary_button = ttk.Button(toolbar if toolbar is not None else bar, command=self.manage_dictionary)
        if toolbar is None:
            self.dictionary_button.grid(row=0, column=6)
        else:
            self.dictionary_button.pack(side="right", padx=(0, 8))
        self.update_correction_controls()

    def toggle_replacement_bar(self):
        if self.correction_bar.winfo_manager():
            self.correction_bar.pack_forget()
            self.replacement_toggle.configure(text="検索・置換")
        else:
            self.correction_bar.pack(side="top", fill="x", pady=(0, 8), before=self.replacement_table_frame)
            self.replacement_toggle.configure(text="置換を閉じる")
            self.find_entry.focus_set()

    def update_correction_controls(self):
        editable = bool(self.segments) and not self.proc
        self.replace_all_button.configure(state="normal" if editable else "disabled")
        self.undo_replace_button.configure(state="normal" if editable and self.replacement_undo else "disabled")
        for entry in (self.find_entry, self.replace_entry):
            entry.configure(state="normal" if editable else "disabled")
        self.dictionary_button.configure(
            text=f"誤変換辞書（{len(self.correction_settings['rules'])}件）",
            state="disabled" if self.proc else "normal")

    def automatic_correction_rules(self):
        return deepcopy(self.correction_settings["rules"]) if self.correction_settings["auto_apply"] else []

    def replace_all(self):
        if self.proc or not self.segments:
            return
        self.apply_replacements([{"before": self.find_text.get(), "after": self.replace_text.get()}])

    def apply_dictionary(self):
        if self.proc or not self.segments:
            return
        if not self.correction_settings["rules"]:
            self.stage = "誤変換辞書に言葉を登録してください。"
            return
        self.apply_replacements(self.correction_settings["rules"])

    def apply_replacements(self, rules):
        if self.proc or not self.segments:
            return
        try:
            rules = normalize_rules(rules)
            self.commit()
            updated, count, rows = replace_segments(self.segments, rules)
            if not rows:
                self.stage = "置換対象はありませんでした。"
                return
            undo = []
            for old, new in zip(self.segments, updated):
                if old["text"] != new["text"]:
                    undo.append({"segment": old, "text": old["text"], "had_words": "words" in old,
                                 "words": deepcopy(old.get("words")), "after": new["text"]})
                    old.update(text=new["text"], words=[])
            self.replacement_undo = undo
            self.refresh_replaced_text()
            self.stage = f"{rows}行・{count}か所を置換しました。「置換を戻す」で直前の置換を取り消せます。"
        except (OSError, ValueError) as exc:
            messagebox.showerror("置換できません", str(exc), parent=self.root)

    def refresh_replaced_text(self):
        self.refresh_speaker_rows()
        if self.selected is not None:
            cursor = self.editor.index("insert")
            self.editor.configure(state="normal")
            self.editor.delete("1.0", "end")
            self.editor.insert("1.0", self.segments[int(self.selected)]["text"])
            self.editor.mark_set("insert", cursor)
            self.editor.edit_reset()
        self.update_correction_controls()
        self.persist_edits()

    def undo_replacement(self):
        if self.proc or not self.replacement_undo:
            return
        try:
            self.commit()
            current_ids = {id(s) for s in self.segments}
            restored, skipped = 0, 0
            for item in self.replacement_undo:
                segment = item["segment"]
                # Preserve manual edits or row splits made after the bulk replacement.
                if id(segment) not in current_ids or segment["text"] != item["after"]:
                    skipped += 1
                    continue
                segment["text"] = item["text"]
                if item["had_words"]:
                    segment["words"] = item["words"]
                else:
                    segment.pop("words", None)
                restored += 1
            self.replacement_undo = []
            self.refresh_replaced_text()
            self.stage = f"{restored}行の置換を元に戻しました。"
            if skipped:
                self.stage += f" その後に編集・分割した{skipped}行は保持しました。"
        except (OSError, ValueError) as exc:
            messagebox.showerror("置換を戻せません", str(exc), parent=self.root)

    def manage_dictionary(self):
        if self.proc:
            return
        if self.dictionary_window is not None and self.dictionary_window.winfo_exists():
            self.window_settings.show("dictionary", self.dictionary_window)
            return
        self.dictionary_window = DictionaryDialog(self)


class DictionaryDialog(tk.Toplevel):
    def __init__(self, app):
        super().__init__(app.root)
        self.withdraw()
        self.app = app
        self.rules = deepcopy(app.correction_settings["rules"])
        self.title("誤変換辞書")
        self.geometry("740x500")
        self.minsize(660, 460)
        self.transient(app.root)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)
        frame = ttk.Frame(self, padding=14)
        frame.grid(sticky="nsew")
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(2, weight=1)
        self.enabled = tk.BooleanVar(value=app.correction_settings["auto_apply"])
        ttk.Checkbutton(frame, text="次の文字起こしから、完了後に自動で修正する",
                        variable=self.enabled).grid(row=0, column=0, sticky="w")
        ttk.Label(frame, text="文脈AIがONなら登録語を候補として判断します。「今の結果に適用」は一括置換です。",
                  wraplength=610).grid(row=1, column=0, sticky="w", pady=(5, 8))
        listing = ttk.Frame(frame)
        listing.grid(row=2, column=0, sticky="nsew")
        self.tree = ttk.Treeview(listing, columns=("before", "after"), show="headings", selectmode="browse", height=6)
        for key, title in (("before", "誤変換（置換前）"), ("after", "正しい表記（置換後）")):
            self.tree.heading(key, text=title)
            self.tree.column(key, width=290, minwidth=100)
        scroll = ttk.Scrollbar(listing, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.tree.pack(side="left", fill="both", expand=True)
        self.tree.bind("<<TreeviewSelect>>", self.select_rule)
        fields = ttk.Frame(frame)
        fields.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        fields.columnconfigure(0, weight=1)
        fields.columnconfigure(1, weight=1)
        ttk.Label(fields, text="誤変換（置換前）").grid(row=0, column=0, sticky="w")
        ttk.Label(fields, text="正しい表記（置換後）").grid(row=0, column=1, sticky="w", padx=(10, 0))
        self.before = tk.StringVar()
        self.after_text = tk.StringVar()
        self.before_entry = ttk.Entry(fields, textvariable=self.before)
        self.before_entry.grid(row=1, column=0, sticky="ew")
        self.after_entry = ttk.Entry(fields, textvariable=self.after_text)
        self.after_entry.grid(row=1, column=1, sticky="ew", padx=(10, 0))
        row = ttk.Frame(frame)
        row.grid(row=4, column=0, sticky="ew", pady=8)
        self.register_button = ttk.Button(row, text="登録 / 更新", command=self.register_rule)
        self.register_button.pack(side="left")
        ttk.Button(row, text="新規入力", command=self.new_rule).pack(side="left", padx=6)
        ttk.Button(row, text="選択を削除", command=self.delete_rule).pack(side="left")
        ttk.Label(frame, text="例：誤った固有名詞 → 正しい名前。置換後を空にすると削除します。",
                  wraplength=610).grid(row=5, column=0, sticky="w")
        bottom = ttk.Frame(frame)
        bottom.grid(row=6, column=0, sticky="ew", pady=(10, 0))
        self.save_button = ttk.Button(bottom, text="保存して閉じる", command=self.save)
        self.save_button.pack(side="left")
        self.apply_button = ttk.Button(bottom, text="保存して今の結果に適用", command=lambda: self.save(apply_now=True),
                                       state="normal" if app.segments else "disabled")
        self.apply_button.pack(side="left", padx=6)
        ttk.Button(bottom, text="キャンセル", command=self.destroy).pack(side="right")
        self.refresh()
        app.window_settings.attach(self, "dictionary")
        self.grab_set()

    def destroy(self):
        self.app.window_settings.remember("dictionary", self)
        super().destroy()

    def refresh(self, selected=None):
        self.tree.delete(*self.tree.get_children())
        for i, rule in enumerate(self.rules):
            self.tree.insert("", "end", iid=str(i), values=(rule["before"], rule["after"]))
        if selected is not None:
            self.tree.selection_set(str(selected))
            self.tree.see(str(selected))

    def select_rule(self, event=None):
        selection = self.tree.selection()
        if selection:
            rule = self.rules[int(selection[0])]
            self.before.set(rule["before"])
            self.after_text.set(rule["after"])

    def new_rule(self):
        self.tree.selection_remove(*self.tree.selection())
        self.before.set("")
        self.after_text.set("")
        self.before_entry.focus_set()

    def store_editor(self):
        selected = self.tree.selection()
        rule = {"before": self.before.get(), "after": self.after_text.get()}
        if not selected and not rule["before"] and not rule["after"]:
            return
        candidate = deepcopy(self.rules)
        if selected:
            index = int(selected[0])
            candidate[index] = rule
        else:
            index = len(candidate)
            candidate.append(rule)
        self.rules = normalize_rules(candidate)
        self.refresh(index)

    def register_rule(self):
        try:
            self.store_editor()
        except ValueError as exc:
            messagebox.showerror("登録できません", str(exc), parent=self)

    def delete_rule(self):
        selected = self.tree.selection()
        if selected:
            del self.rules[int(selected[0])]
            self.new_rule()
            self.refresh()

    def save(self, apply_now=False):
        try:
            # Also save a rule that was typed immediately before pressing Save.
            self.store_editor()
            settings = save_settings({"version": 1, "auto_apply": self.enabled.get(), "rules": self.rules},
                                     self.app.correction_path)
        except (OSError, ValueError) as exc:
            messagebox.showerror("辞書を保存できません", str(exc), parent=self)
            return
        self.app.correction_settings = settings
        self.app.update_correction_controls()
        mode = "自動適用ON" if settings["auto_apply"] else "自動適用OFF"
        self.app.stage = f"誤変換辞書を保存しました（{len(self.rules)}件・{mode}）。"
        self.destroy()
        if apply_now:
            self.app.apply_dictionary()
