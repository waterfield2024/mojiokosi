"""Draft and applied listening ranges, separate from transcript/caption times."""
import math
import re
import unicodedata
import tkinter as tk
from tkinter import ttk

from engine import timestamp


def parse_audio_time(text):
    value=unicodedata.normalize("NFKC",str(text)).strip().replace(",",".")
    if not re.fullmatch(r"(?:[0-9]+:){0,2}(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)",value):
        raise ValueError("時刻か秒数で入力してください（例 1:23.5 / 83.5）。")
    parts=[float(part) for part in value.split(":")]
    if not all(math.isfinite(part) for part in parts):raise ValueError("時刻が大きすぎます。")
    if len(parts)>1 and (parts[-1]>=60 or len(parts)==3 and parts[-2]>=60):
        raise ValueError("時刻の秒・分は60未満にしてください。")
    result=0.
    for part in parts:result=result*60+part
    if not math.isfinite(result*1000):raise ValueError("時刻が大きすぎます。")
    return round(result,3)


def audio_duration(value):
    try:
        value=float(value)
        return value if math.isfinite(value) and value>0 else None
    except (TypeError,ValueError,OverflowError):return None


def seek_audio_intervals(spans, position, duration):
    """Return the first and repeat passes in the waveform's time coordinates."""
    duration = audio_duration(duration)
    position = float(position)
    if duration is None or not math.isfinite(position):
        raise ValueError("音声の再生位置を確認できません。")
    if position >= duration - .0005:
        return [], []
    position = max(0., round(position, 3))
    if position >= duration:
        return [], []
    for index, (start, end) in enumerate(spans):
        if start <= position < end:
            return [(position, end), *spans[index+1:]], list(spans)
    return [(position, duration)], [(position, duration)]


def checked_audio_range(start, end, duration=None, *, minimum=.1):
    start, end = parse_audio_time(start), parse_audio_time(end)
    duration = audio_duration(duration)
    if duration is not None:
        if end > round(duration, 3):
            raise ValueError(f"終了は {timestamp(duration)} 以内にしてください。")
        end = min(end, duration)
    if end <= start:
        raise ValueError("終了は開始より後にしてください。")
    if end - start < minimum - 1e-6:
        raise ValueError(f"{minimum:g}秒以上の範囲を指定してください。")
    return start, end


class ComparisonRangeControls:
    def build_comparison_audio_range(self,parent):
        self._audio_ranges={};self._audio_range_key=None;self._audio_range_loaded=("","");self._audio_range_loading=False
        self._audio_range_summary="違いを選ぶと調整できます。"
        self.audio_range_start=tk.StringVar();self.audio_range_end=tk.StringVar();self.audio_range_note=tk.StringVar(value=self._audio_range_summary)
        bar=ttk.Frame(parent);bar.pack(fill="x",pady=(0,8));self.audio_range_bar=bar
        ttk.Label(bar,text="再生範囲").pack(side="left",padx=(0,8))
        self.audio_range_entries=[]
        for title,variable in (("開始",self.audio_range_start),("終了",self.audio_range_end)):
            ttk.Label(bar,text=title).pack(side="left",padx=(0,4))
            entry=ttk.Entry(bar,textvariable=variable,width=13);entry.pack(side="left",padx=(0,8))
            self.audio_range_entries.append(entry)
            entry.bind("<Return>",self.apply_comparison_audio_range)
            entry.bind("<Up>",lambda event,v=variable:self.step_comparison_audio_range(v,.1))
            entry.bind("<Down>",lambda event,v=variable:self.step_comparison_audio_range(v,-.1))
            variable.trace_add("write",self._comparison_audio_range_edited)
        self.audio_range_apply=ttk.Button(bar,text="範囲を反映",command=self.apply_comparison_audio_range)
        self.audio_range_apply.pack(side="left",padx=(0,6))
        self.audio_range_reset=ttk.Button(bar,text="自動に戻す",command=self.reset_comparison_audio_range)
        self.audio_range_reset.pack(side="left",padx=(0,8))
        self.audio_range_note_label=ttk.Label(bar,textvariable=self.audio_range_note,style="Sub.TLabel",wraplength=180)
        self.audio_range_note_label.pack(side="left",fill="x",expand=True)
        self.audio_range_note_label.bind("<Configure>",lambda e:self.audio_range_note_label.configure(wraplength=max(60,e.width)))
        from comparison_waveform_ui import ComparisonWaveform
        self.audio_waveform=ComparisonWaveform(parent,self);self.audio_waveform.pack(fill="x",pady=(0,6))

    def clear_comparison_audio_ranges(self):
        self.audio_waveform.cancel_drag(restore=False)
        self._audio_ranges.clear();self._audio_range_key=object()

    def _comparison_audio_range_form(self):return self.audio_range_start.get(),self.audio_range_end.get()

    def _comparison_audio_range_edited(self,*args):
        if not self._audio_range_loading:
            self.audio_range_note.set("未反映：「範囲を反映」で切替" if self._comparison_audio_range_form()!=self._audio_range_loaded else self._audio_range_summary)

    def step_comparison_audio_range(self,variable,step):
        if self.busy() or self._audio_range_key is None:return "break"
        try:value=max(0.,parse_audio_time(variable.get())+step)
        except ValueError as exc:self.audio_range_note.set(str(exc));return "break"
        limit=audio_duration((self.data or {}).get("audio_seconds"))
        if limit is not None:value=min(value,limit)
        variable.set(timestamp(value));return "break"

    def _load_comparison_audio_range(self):
        spans=self.comparison_audio_intervals() if self._audio_range_key is not None else []
        self._audio_range_loading=True
        try:
            self.audio_range_start.set(timestamp(spans[0][0]) if spans else "")
            self.audio_range_end.set(timestamp(spans[-1][1]) if spans else "")
            self._audio_range_loaded=self._comparison_audio_range_form()
        finally:self._audio_range_loading=False
        if not spans:self._audio_range_summary="違いを選ぶと調整できます。"
        elif self.merge_index in self._audio_ranges:self._audio_range_summary="指定範囲 / 秒数でも入力可"
        else:self._audio_range_summary=f"自動：{len(spans)}区間（間は省略）" if len(spans)>1 else "自動 / 秒数でも入力可"
        self.audio_range_note.set(self._audio_range_summary)
        self.update_comparison_waveform()

    def update_comparison_audio_range(self):
        ready=bool(self.merge_plan is not None and self.path and self.merge_choices)
        key=(id(self.merge_plan),self.path,self.merge_index) if ready else None
        if key!=self._audio_range_key:
            self._audio_range_key=key;self._load_comparison_audio_range()
        state="normal" if ready and not self.busy() else "disabled"
        for widget in (*self.audio_range_entries,self.audio_range_apply,self.audio_range_reset):widget.configure(state=state)
        self.update_comparison_waveform()

    def update_comparison_waveform(self):
        ready=bool(self.merge_plan is not None and self.path and self.merge_choices)
        key=(id(self.merge_plan),self.path,self.merge_index) if ready else None
        self.audio_waveform.sync(key,self.comparison_audio_intervals() if ready else [],
                                 (self.data or {}).get("audio_seconds"),ready and not self.busy())

    def comparison_audio_intervals(self):
        custom=self._audio_ranges.get(self.merge_index)
        return [custom] if custom is not None else self.default_comparison_audio_intervals()

    def commit_pending_comparison_audio_range(self):
        if self._comparison_audio_range_form()==self._audio_range_loaded:return True
        return self.apply_comparison_audio_range(restart=False)

    def apply_comparison_audio_range(self,event=None,restart=True):
        if self.busy() or self._audio_range_key is None:return False
        try:span=checked_audio_range(*self._comparison_audio_range_form(),(self.data or {}).get("audio_seconds"))
        except ValueError as exc:self.audio_range_note.set(str(exc));return False
        changed=self._audio_ranges.get(self.merge_index)!=span
        playing=self._audio_generation is not None
        self._audio_ranges[self.merge_index]=span;self._load_comparison_audio_range()
        if changed and playing and restart:
            self.stop_comparison_audio();self._start_comparison_audio()
        elif not playing:self.status.set("再生範囲を反映しました。「この部分を再生」で聞けます。")
        return True

    def reset_comparison_audio_range(self):
        if self.busy() or self._audio_range_key is None:return
        changed=self._audio_ranges.pop(self.merge_index,None) is not None
        playing=self._audio_generation is not None;self._load_comparison_audio_range()
        if changed and playing:
            self.stop_comparison_audio();self._start_comparison_audio()
        elif not playing:self.status.set("この部分の再生範囲を自動設定に戻しました。")
