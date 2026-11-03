"""Compact per-difference choices for the comparison window."""
import tkinter as tk
from tkinter import ttk,messagebox

from comparison_merge import MergePlan,load_choices,save_choices,save_merged
from comparison_audio_ui import ComparisonAudioControls


class MergeControls(ComparisonAudioControls):
    def build_merge_controls(self,parent):
        self.merge_plan = None;self.merge_choices = [];self.merge_index = 0
        self.merge_buttons = {};self.merge_ranges = {};self._active_difference = None
        self.merge_load_note="";self._merge_needs_scroll=False
        toolbar = ttk.Frame(parent);toolbar.pack(fill="x",pady=(0,8))
        self.merge_previous = ttk.Button(toolbar,text="前へ",width=6,command=lambda:self.move_difference(-1))
        self.merge_previous.pack(side="left")
        self.merge_label = tk.StringVar(value="違いごとに、使う文章を選べます。")
        ttk.Label(toolbar,textvariable=self.merge_label,style="Sub.TLabel",wraplength=180).pack(side="left",padx=8)
        self.merge_next = ttk.Button(toolbar,text="次へ",width=6,command=lambda:self.move_difference(1))
        self.merge_next.pack(side="left")
        self.merge_button = ttk.Button(toolbar,text="選んだ文章をまとめる",style="Accent.TButton",command=self.merge_result)
        self.merge_button.pack(side="right")
        self.build_comparison_audio(toolbar)
        self.build_comparison_audio_range(parent)

    def prepare_merge(self,sources):
        self.stop_comparison_audio()
        self.clear_comparison_audio_ranges()
        self.merge_plan = None;self.merge_choices = [];self.merge_index = 0;self.merge_ranges = {}
        self.merge_load_note="";self._merge_needs_scroll=False
        if sources is not None:
            self.merge_plan = MergePlan(self.results)
            self.merge_choices = [None]*len(self.merge_plan.spans)
            if self.path:
                try:self.merge_choices,self.merge_index=load_choices(self.path,self.merge_plan)
                except (OSError,ValueError,TypeError,AttributeError) as exc:
                    self.merge_load_note="採用する文章を選び直してください："+str(exc)
                    self.status.set(self.merge_load_note)
            self._merge_needs_scroll=True
            self.merge_ranges = {id(row):ranges for side,rows in zip(("whisper","granite"),sources)
                                 for row,ranges in zip(rows,self.merge_plan.row_ranges[side])}

    def update_merge_controls(self):
        ready = self.merge_plan is not None and self.path is not None and not self.busy()
        total = len(self.merge_choices);chosen = sum(choice is not None for choice in self.merge_choices)
        self.merge_button.configure(state="normal" if ready and chosen==total else "disabled")
        for button in (self.merge_previous,self.merge_next):button.configure(state="normal" if ready and total>1 else "disabled")
        if not self.merge_plan:self.merge_label.set("両モデルの結果がそろうと選べます。")
        elif not total:self.merge_label.set("文章は一致しています。まとめて保存できます。")
        else:
            self.merge_label.set(f"違い {self.merge_index+1}/{total}　選択済み {chosen}/{total}")
        for side,button in self.merge_buttons.items():
            empty = ready and total and not self.merge_plan.excerpt(self.merge_index,side)
            button.configure(state="normal" if ready and total else "disabled",
                             text=("✓ " if total and self.merge_choices[self.merge_index]==side else "")+("Whisper" if side=="whisper" else "Granite")+"を採用"+("（この部分なし）" if empty else ""))
        self.update_comparison_audio()

    def mark_active_difference(self,scroll=False):
        if self._active_difference is not None:
            for text in self.texts.values():
                tag=f"difference_{self._active_difference}"
                if tag in text.tag_names():text.tag_configure(tag,underline=False)
        self._active_difference = self.merge_index if self.merge_choices else None
        if self._active_difference is not None:
            tag=f"difference_{self.merge_index}";target=None
            for text in self.texts.values():
                if tag in text.tag_names():
                    text.tag_configure(tag,underline=True)
                    ranges=text.tag_ranges(tag)
                    if ranges and target is None:target=(text,ranges[0])
            if scroll and target:
                text,index=target;text.see(index);self.update_idletasks()
                self.scroll("moveto",text.yview()[0])
        self.update_merge_controls()

    def click_difference(self,event):
        if self.busy():return
        for tag in event.widget.tag_names(event.widget.index(f"@{event.x},{event.y}")):
            if tag.startswith("difference_"):
                if int(tag.removeprefix("difference_"))!=self.merge_index:self.stop_comparison_audio()
                self.merge_index=int(tag.removeprefix("difference_"));self.mark_active_difference();break

    def move_difference(self,step):
        if self.busy() or not self.merge_choices:return
        self.stop_comparison_audio()
        self.merge_index=(self.merge_index+step)%len(self.merge_choices);self.mark_active_difference(scroll=True)

    def choose_difference(self,side):
        if self.busy() or not self.merge_plan or not self.path or not self.merge_choices:return
        previous=list(self.merge_choices);index=self.merge_index
        self.merge_choices[index]=side
        remaining=[(index+step)%len(previous) for step in range(1,len(previous)) if self.merge_choices[(index+step)%len(previous)] is None]
        next_index=remaining[0] if remaining else index
        try:save_choices(self.path,self.merge_plan,self.merge_choices,next_index)
        except Exception as exc:
            self.merge_choices=previous
            messagebox.showerror("選択を保存できません",str(exc),parent=self);return
        self.stop_comparison_audio()
        self.merge_index=next_index;self.mark_active_difference(scroll=True)
        self.merge_load_note=""
        self.status.set("選択を保存しました。「前へ・次へ」や色付きの文章を押して、いつでも選び直せます。")

    def merge_result(self):
        if self.busy() or not self.merge_plan or not self.path:return
        self.stop_comparison_audio()
        try:
            self.merge_plan.validate_choices(self.merge_choices,complete=True)
            save_choices(self.path,self.merge_plan,self.merge_choices,self.merge_index)
            path=save_merged(self.path,self.merge_plan,self.merge_choices)
            self.app.display_result(path)
            self.app.stage="選んだ文章を1つにまとめて保存しました。文章・字幕時刻を確認し、TXT・SRT・字幕付きMP4を出力できます。"
            self.withdraw()
        except Exception as exc:messagebox.showerror("文章をまとめられません",str(exc),parent=self)

    def insert_comparison_text(self,widget,row):
        text=str(row.get("text","")).strip();position=0
        for start,end,number in self.merge_ranges.get(id(row),()):
            if start>position:widget.insert("end",text[position:start],())
            widget.insert("end",text[start:end],("change",f"difference_{number}"));position=end
        if position<len(text):widget.insert("end",text[position:],())
