"""Pinned text, speaker-name and timing editor for the playing MP4 preview."""
from copy import deepcopy
import tkinter as tk
from tkinter import ttk
from caption_timing import timed_segments, validate_change, CaptionProblem
from video_layout import CaptionLayout
from speakers import speaker_name, UNKNOWN
from preview_speakers import TARGET, assign_registered
from ui_theme import style_text


class CaptionTimingEditor(ttk.Frame):
    def __init__(self, parent, dialog):
        super().__init__(parent)
        self.dialog = dialog
        self.index = None
        self.history = []
        self.loaded = None
        self.start = tk.StringVar()
        self.end = tk.StringVar()
        self.name = tk.StringVar()
        self.target = None
        self.profile_ids = [None]
        self.profiles = {}
        self.feedback = tk.StringVar(value="話者の選択はこの字幕だけに反映します。話者名の編集は同じ話者の全字幕に反映します。")
        self.indices = [i for i,s in enumerate(dialog.result["segments"]) if str(s["text"]).strip()]
        self.columnconfigure(0,weight=1)
        selection = ttk.Frame(self);selection.grid(row=0,column=0,sticky="ew",pady=(8,4))
        ttk.Label(selection,text="調整する字幕").pack(side="left")
        self.choice = ttk.Combobox(selection,state="readonly",width=25)
        self.choice.pack(side="left",fill="x",expand=True,padx=8)
        self.choice.configure(values=[self.label(i) for i in self.indices])
        self.choice.bind("<<ComboboxSelected>>",self.choose)
        ttk.Button(selection,text="表示中を選ぶ",command=self.select_current).pack(side="right")
        content = ttk.Frame(self);content.grid(row=1,column=0,sticky="ew",pady=(0,5))
        identity = ttk.Frame(content);identity.pack(side="left",padx=(0,10))
        ttk.Label(identity,text="この字幕の話者").grid(row=0,column=0,sticky="w",padx=(0,6))
        self.registered = ttk.Combobox(identity,state="disabled",width=18,postcommand=self.refresh_profiles)
        self.registered.grid(row=0,column=1,sticky="ew")
        self.registered.bind("<<ComboboxSelected>>",self.choose_registered)
        ttk.Label(identity,text="話者名").grid(row=1,column=0,sticky="w",padx=(0,6))
        self.name_entry = ttk.Entry(identity,textvariable=self.name,width=18,state="disabled")
        self.name_entry.grid(row=1,column=1,sticky="ew",pady=(3,0));self.name_entry.bind("<Return>",lambda event:self.apply_pending())
        ttk.Label(content,text="文章").pack(side="left",padx=(0,6))
        self.text = tk.Text(content,height=2,width=20,wrap="word",font=("Yu Gothic UI",10),undo=True,state="disabled")
        style_text(self.text)
        scroll = ttk.Scrollbar(content,orient="vertical",command=self.text.yview);scroll.pack(side="right",fill="y")
        self.text.configure(yscrollcommand=scroll.set);self.text.pack(side="left",fill="both",expand=True)
        self.text.bind("<Control-Return>",self.apply_shortcut)
        controls = ttk.Frame(self);controls.grid(row=2,column=0,sticky="ew")
        self.entries = []
        for label,variable,edge in (("開始（秒）",self.start,"start"),("終了（秒）",self.end,"end")):
            ttk.Label(controls,text=label).pack(side="left")
            entry = ttk.Spinbox(controls,textvariable=variable,from_=0,to=9999999,increment=.1,width=9,
                                format="%.2f",command=self.apply_pending)
            entry.pack(side="left",padx=(4,4));self.entries.append(entry)
            entry.bind("<Return>",lambda event:self.apply_pending())
            ttk.Button(controls,text="今を開始に" if edge=="start" else "今を終了に",
                       command=lambda which=edge:self.capture(which)).pack(side="left",padx=(0,10))
        ttk.Button(controls,text="反映",width=5,command=self.apply_pending).pack(side="left")
        self.undo_button = ttk.Button(selection,text="変更を戻す",command=self.undo,state="disabled")
        self.undo_button.pack(side="right",padx=(8,0))
        ttk.Label(self,textvariable=self.feedback,wraplength=760).grid(row=3,column=0,sticky="w",pady=(4,0))

    def label(self, index):
        row = self.dialog.result["segments"][index]
        name = speaker_name(row.get("speaker"),self.dialog.result.get("speakers"))
        text = " ".join(str(row["text"]).split())
        return f"{index+1}. {name+'：' if name else ''}{text[:100] or '（字幕なし）'}"

    def form(self):
        return self.start.get(),self.end.get(),self.text.get("1.0","end-1c"),self.name.get(),self.target

    def refresh_profiles(self):
        try:self.profiles = self.dialog.app.speaker_memory.load()
        except (OSError,ValueError) as exc:
            self.feedback.set(f"登録済みの話者を読み込めません：{exc}")
        row = self.dialog.result["segments"][self.index] if self.index is not None else {}
        current = speaker_name(row.get("speaker"),self.dialog.result.get("speakers")) or "未設定"
        labels = [f"現在の話者：{current}"]
        self.profile_ids = [None]
        ordered = sorted(self.profiles.items(),key=lambda item:(item[1]["name"],item[0]))
        for key,profile in ordered:
            name = profile["name"]
            same = [other for other,p in ordered if p["name"]==name]
            if len(same)>1:
                length = 6
                while len({other[-length:] for other in same})<len(same):length += 1
                name += "（"+key[-length:]+"）"
            labels.append(name);self.profile_ids.append(key)
        if self.target and self.target not in self.profile_ids:
            labels.append("選択した話者（登録を確認してください）");self.profile_ids.append(self.target)
        self.registered.configure(values=labels,state="readonly")
        self.registered.current(self.profile_ids.index(self.target))

    def choose_registered(self,event=None):
        position = self.registered.current()
        if self.index is None or position<0:return
        row = self.dialog.result["segments"][self.index]
        self.target = self.profile_ids[position] or row.get(TARGET)
        name = self.profiles.get(self.target,{}).get("name") if self.target else None
        self.name.set(name or speaker_name(row.get("speaker"),self.dialog.result.get("speakers")))
        self.name_entry.configure(state="readonly" if self.target else "normal")
        self.feedback.set("「反映」で、この字幕だけを選んだ話者に変更します。" if self.target else
                          "話者の選択はこの字幕だけ、話者名の編集は同じ話者の全字幕に反映します。")

    def apply_shortcut(self, event=None):
        self.apply_pending()
        return "break"

    def interval(self, index):
        for row in timed_segments(self.dialog.result["segments"],getattr(self.dialog,"duration",None)):
            if row["_caption_index"] == index:return row["start"],row["end"]
        row = self.dialog.result["segments"][index]
        value = row.get("caption_timing",row)
        return value["start"],value["end"]

    def load(self, index):
        self.index = index
        self.choice.configure(values=[self.label(i) for i in self.indices])
        self.choice.current(self.indices.index(index))
        start,end = self.interval(index)
        self.start.set(f"{start:.2f}");self.end.set(f"{end:.2f}")
        row = self.dialog.result["segments"][index]
        self.target = row.get(TARGET)
        self.name_entry.configure(state="readonly" if self.target else "normal");self.name.set(speaker_name(row.get("speaker"),self.dialog.result.get("speakers")))
        self.refresh_profiles()
        self.text.configure(state="normal");self.text.delete("1.0","end");self.text.insert("1.0",row["text"]);self.text.edit_reset()
        self.loaded = self.form()

    def choose(self, event=None):
        target = self.indices[self.choice.current()]
        if not self.apply_pending():
            if self.index is not None:self.choice.current(self.indices.index(self.index))
            return
        self.load(target)
        self.feedback.set("話者を選ぶか、文章・時刻を編集して「反映」。話者の選択はこの字幕だけに反映します。")
        if self.dialog.source is not None:self.dialog.seek_to(max(0.,self.interval(target)[0]-.2))

    def select_current(self):
        dialog = self.dialog
        cue = dialog.layout.index_at(dialog.shown_time) if dialog.layout else None
        if cue is None:
            self.feedback.set("この位置には字幕がありません。上の一覧から調整する字幕を選んでください。")
            return
        target = dialog.layout.cues[cue]["segment_index"]
        if self.apply_pending():
            self.load(target);self.feedback.set("表示中の字幕を選びました。再生中もこの字幕を選択したまま調整できます。")

    def ensure_selection(self):
        if self.index is not None or self.dialog.source is None or not self.indices:return
        if self.dialog.layout:
            cue = self.dialog.layout.index_at(self.dialog.shown_time)
            index = self.dialog.layout.cues[cue if cue is not None else 0]["segment_index"]
        else:index = self.dialog.problem_index if self.dialog.problem_index in self.indices else self.indices[0]
        self.load(index)

    def capture(self, edge):
        dialog = self.dialog
        if dialog.frame is None or dialog.busy or dialog.seek_id:
            self.feedback.set("映像の準備が終わってから時刻を設定してください。")
            return
        self.ensure_selection()
        (self.start if edge=="start" else self.end).set(f"{dialog.shown_time:.2f}")
        self.apply_pending()

    def apply_pending(self):
        if self.index is None:return True
        if self.loaded == self.form():return True
        dialog = self.dialog
        if dialog.source is None:
            self.feedback.set("元動画の準備が終わってから反映してください。")
            return False
        try:
            candidate = deepcopy(dialog.result)
            row = candidate["segments"][self.index]
            old = (deepcopy(row),deepcopy(candidate.get("speakers",{})),deepcopy(candidate.get("speaker_name_edits")),
                   deepcopy(candidate.get("speaker_memory_links")))
            if self.loaded[:2] != (self.start.get(),self.end.get()):
                row["caption_timing"] = validate_change(dialog.result["segments"],self.index,self.start.get(),self.end.get(),dialog.duration)
            text = self.text.get("1.0","end-1c").strip()
            if text and not row["text"].strip():
                validate_change(dialog.result["segments"],self.index,self.start.get(),self.end.get(),dialog.duration)
            if row["text"] != text:row.update(text=text,words=[])
            name = self.name.get().strip();speaker = row.get("speaker","")
            if (speaker and not name) or len(name)>30 or any(ord(c)<32 for c in name):
                raise ValueError("話者名は改行を含まない1〜30文字で入力してください。")
            if self.target:
                assign_registered(candidate,self.index,self.target,dialog.app.speaker_memory.load())
            elif name != self.loaded[3]:
                if not speaker or speaker == UNKNOWN:
                    number = 0
                    used = set(candidate.get("speakers",{})) | {s.get("speaker","") for s in candidate["segments"]}
                    while f"speaker_{number:02d}" in used:number += 1
                    speaker = f"speaker_{number:02d}"
                    row.update(speaker=speaker,speaker_manual=True,speaker_review=False)
                candidate.setdefault("speakers",{})[speaker] = name
                candidate.setdefault("speaker_name_edits",{})[speaker] = name
            problem=None
            try:layout = CaptionLayout(candidate,dialog.width,dialog.height,dialog.duration,dialog.settings)
            except CaptionProblem as exc:layout=None;problem=exc
        except (OSError,ValueError) as exc:
            self.feedback.set(str(exc));return False
        if candidate != dialog.result:
            self.history.append((self.index,old));self.history = self.history[-50:]
        dialog.result = candidate;dialog.layout = layout;dialog.set_caption_problem(problem)
        self.load(self.index);self.undo_button.configure(state="normal" if self.history else "disabled")
        self.feedback.set(f"字幕 {self.index+1} の変更を反映しました。" + ("話者の変更はこの字幕だけです。" if self.target else
                          "話者名の編集は同じ話者の全字幕に反映されます。"))
        if dialog.frame is not None and not dialog.busy and dialog.seek_id is None:
            dialog.save_button.configure(state="normal" if layout else "disabled")
        dialog.draw()
        return True

    def undo(self):
        if not self.history:
            if self.index is not None:self.load(self.index)
            return
        index,old = self.history[-1];dialog = self.dialog
        candidate = deepcopy(dialog.result)
        candidate["segments"][index] = deepcopy(old[0]);candidate["speakers"] = deepcopy(old[1])
        if old[2] is None:candidate.pop("speaker_name_edits",None)
        else:candidate["speaker_name_edits"] = deepcopy(old[2])
        if old[3] is None:candidate.pop("speaker_memory_links",None)
        else:candidate["speaker_memory_links"] = deepcopy(old[3])
        problem=None
        try:layout = CaptionLayout(candidate,dialog.width,dialog.height,dialog.duration,dialog.settings)
        except CaptionProblem as exc:layout=None;problem=exc
        except ValueError as exc:self.feedback.set(str(exc));return
        self.history.pop();dialog.result = candidate;dialog.layout = layout;dialog.set_caption_problem(problem)
        self.load(index);self.feedback.set("直前の字幕編集を戻しました。")
        self.undo_button.configure(state="normal" if self.history else "disabled")
        if dialog.frame is not None and not dialog.busy and dialog.seek_id is None:
            dialog.save_button.configure(state="normal" if layout else "disabled")
        dialog.draw()
