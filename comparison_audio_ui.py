"""Listen to the active difference without blocking selection or merging."""
import queue
import tkinter as tk
from tkinter import ttk

from comparison_audio import REPEAT_PAUSE_MS,SPEED_LABELS,ComparisonAudioPlayer,comparison_audio_source,difference_intervals
from comparison_audio_range import ComparisonRangeControls,seek_audio_intervals
from engine import timestamp


class ComparisonAudioControls(ComparisonRangeControls):
    def build_comparison_audio(self,toolbar):
        self.comparison_player=None;self._audio_generation=None;self._audio_target=None
        self._audio_intervals=[];self._audio_range_text="";self._audio_cycle_intervals=None
        self.audio_repeat=tk.BooleanVar(value=False)
        self._audio_repeat_after=None;self._audio_pass=0
        self.audio_speed=tk.StringVar(value="1倍")
        self._audio_playing_speed=1.0
        self.audio_button=ttk.Button(toolbar,text="この部分を再生",command=self.toggle_comparison_audio)
        self.audio_button.pack(side="right",padx=(8,8))
        self.audio_repeat_check=ttk.Checkbutton(toolbar,text="繰り返し",variable=self.audio_repeat,
                                               command=self.change_comparison_audio_repeat)
        self.audio_repeat_check.pack(side="right",padx=(6,0))
        speed_frame=ttk.Frame(toolbar);speed_frame.pack(side="right")
        ttk.Label(speed_frame,text="速度").pack(side="left",padx=(0,4))
        self.audio_speed_combo=ttk.Combobox(speed_frame,textvariable=self.audio_speed,
                                          values=list(SPEED_LABELS),state="readonly",width=6)
        self.audio_speed_combo.pack(side="left")
        self.audio_speed_combo.bind("<<ComboboxSelected>>",self.change_comparison_audio_speed)

    def change_comparison_audio_repeat(self):
        if self.audio_repeat.get():
            self.status.set("この部分を繰り返し再生します。すぐ止めるときは「音声を停止」を押してください。" if self._audio_generation is not None
                            else "繰り返しをオンにしました。「この部分を再生」で始めます。")
        elif self._audio_repeat_after is not None:
            self.stop_comparison_audio();self.status.set("繰り返しを解除しました。音声は停止しています。")
        else:self.status.set("繰り返しを解除しました。今の再生が終わると停止します。" if self._audio_generation is not None else "繰り返しを解除しました。")

    def _cancel_comparison_audio_repeat(self):
        if self._audio_repeat_after is not None:
            try:self.app.root.after_cancel(self._audio_repeat_after)
            except tk.TclError:pass
            self._audio_repeat_after=None

    def _repeat_comparison_audio(self,generation,target):
        # A stopped/hidden/switched comparison must never restart from an old timer.
        if generation!=self._audio_generation or target!=self._audio_target:return
        self._audio_repeat_after=None
        if (self.audio_repeat.get() and not self.busy() and target==(self.path,self.merge_index)
                and self.winfo_exists() and self.state()!="withdrawn"):
            self._audio_generation=None
            self._start_comparison_audio(repeating=True)
        else:self.stop_comparison_audio()

    def change_comparison_audio_speed(self,event=None):
        speed=SPEED_LABELS.get(self.audio_speed.get(),1.0)
        if self._audio_generation is not None:
            if speed==self._audio_playing_speed:return
            cycle=self._audio_cycle_intervals
            self.stop_comparison_audio();self._audio_cycle_intervals=cycle;self._start_comparison_audio()
            if self._audio_generation is not None:
                self.status.set(f"{speed:g}倍速で、この部分の最初から再生します（声の高さは変わりません）。")
        else:self.status.set(f"再生速度：{speed:g}倍。「この部分を再生」で聞けます（声の高さは変わりません）。")

    def update_comparison_audio(self):
        if self.busy() and self._audio_generation is not None:self.stop_comparison_audio()
        ready=bool(self.merge_plan is not None and self.path and self.merge_choices and not self.busy())
        self.audio_button.configure(state="normal" if ready else "disabled",
                                    text="音声を停止" if self._audio_generation is not None else "この部分を再生")
        self.audio_speed_combo.configure(state="disabled" if self.busy() else "readonly")
        self.audio_repeat_check.configure(state="disabled" if self.busy() else "normal")
        self.update_comparison_audio_range()

    def toggle_comparison_audio(self):
        if self._audio_generation is not None:
            self.stop_comparison_audio();self.status.set("音声を停止しました。同じ部分をもう一度再生できます。");return
        if not self.commit_pending_comparison_audio_range():return
        self._start_comparison_audio()

    def default_comparison_audio_intervals(self):
        if not self.merge_plan or not self.merge_choices:return []
        return difference_intervals(self.merge_plan,self.merge_index,(self.data or {}).get("audio_seconds"))

    def seek_comparison_audio(self,position):
        if self.busy() or not self.merge_plan or not self.path or not self.merge_choices:return
        try:
            spans,cycle=seek_audio_intervals(self.comparison_audio_intervals(),position,(self.data or {}).get("audio_seconds"))
        except (TypeError,ValueError,OverflowError) as exc:
            self.status.set("再生位置を変更できません："+str(exc));return
        self.stop_comparison_audio()
        if not spans:
            self.status.set("音声の最後です。聞きたい位置をクリックしてください。");return
        self._audio_cycle_intervals=cycle
        self._start_comparison_audio(intervals=spans)
        if self._audio_generation is not None:self.audio_waveform.set_playhead(spans[0][0])

    def _start_comparison_audio(self,repeating=False,intervals=None):
        if self.busy() or not self.merge_plan or not self.path or not self.merge_choices:return
        try:
            source=comparison_audio_source(self.path,self.results,self.data or {})
            spans=intervals if intervals is not None else self._audio_cycle_intervals or self.comparison_audio_intervals()
            if not spans:raise ValueError("選択中の違いに対応する音声の時刻がありません。")
            history=getattr(self.app,'history_window',None)
            if history:history.stop_audio()
            if self.comparison_player is None:self.comparison_player=ComparisonAudioPlayer()
            self._audio_intervals=spans
            self._audio_range_text=" / ".join(f"{timestamp(a)}〜{timestamp(b)}" for a,b in spans[:2])
            if len(spans)>2:self._audio_range_text+=f" ほか{len(spans)-2}区間"
            self._audio_target=(self.path,self.merge_index)
            self._audio_playing_speed=SPEED_LABELS.get(self.audio_speed.get(),1.0)
            self._audio_generation=self.comparison_player.play(source,spans,speed=self._audio_playing_speed)
            self._audio_pass=self._audio_pass+1 if repeating else 1
            detail="（クリック位置から）" if intervals is not None else "（指定した範囲）" if self._audio_cycle_intervals or self.merge_index in self._audio_ranges else "（前後1秒を含みます）"
            self.status.set(f"音声を準備中… {self._audio_playing_speed:g}倍速　"+self._audio_range_text+detail)
        except Exception as exc:
            self.stop_comparison_audio();self.status.set("再生できません："+str(exc))
        self.update_comparison_audio()

    def stop_comparison_audio(self):
        self._cancel_comparison_audio_repeat()
        self.audio_waveform.set_playhead(None)
        if self.comparison_player is not None:self.comparison_player.stop()
        self._audio_generation=None;self._audio_target=None;self._audio_pass=0;self._audio_cycle_intervals=None
        if self.audio_button.winfo_exists():self.audio_button.configure(text="この部分を再生")

    def poll_comparison_audio(self):
        if self.comparison_player is None:return
        if self._audio_generation is not None and (self.busy() or self._audio_target!=(self.path,self.merge_index)):
            self.stop_comparison_audio()
        while True:
            try:generation,state,value=self.comparison_player.events.get_nowait()
            except queue.Empty:break
            if generation!=self._audio_generation:continue
            if self._audio_repeat_after is not None:continue
            if state=="playing":
                self.audio_waveform.set_playhead(value)
                repeat=f"（繰り返し {self._audio_pass}回目）" if self.audio_repeat.get() else ""
                self.status.set(f"再生中 {self._audio_playing_speed:g}倍速{repeat}　{timestamp(value)}　範囲 {self._audio_range_text}　聞きながら採用できます。")
            elif state=="ended" and self.audio_repeat.get():
                target=self._audio_target
                self._audio_repeat_after=self.app.root.after(REPEAT_PAUSE_MS,lambda g=generation,t=target:self._repeat_comparison_audio(g,t))
                self.status.set(f"繰り返し：{self._audio_pass}回目が終わりました。少し間を空けて同じ部分を再生します。")
            elif state in {"ended","error"}:
                self.stop_comparison_audio()
                self.status.set("再生が終わりました。聞き直すか、採用する文章を選んでください。" if state=="ended" else "再生できません："+str(value))
        self.update_comparison_audio()

    def close_comparison_audio(self):
        self._cancel_comparison_audio_repeat()
        self.audio_waveform.close()
        if self.comparison_player is not None:self.comparison_player.close()
        self._audio_generation=None;self._audio_target=None;self._audio_pass=0;self._audio_cycle_intervals=None
