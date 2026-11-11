"""Click to seek; drag to commit a range once on release."""
import math
import queue
import tkinter as tk
from tkinter import ttk

from comparison_audio import comparison_audio_source
from comparison_audio_range import audio_duration
from comparison_waveform import RATE, WaveformLoader
from engine import timestamp
from ui_theme import palette


class RangeWaveform(ttk.Frame):
    def __init__(self, parent, start_var, end_var, apply_range, *, compact=False, seek=None, drag_note=None):
        super().__init__(parent)
        self.start_var, self.end_var = start_var, end_var
        self.apply_range, self.seek, self.drag_note = apply_range, seek, drag_note
        self.compact = compact
        self.minimum = .001 if compact else .1
        self.overview = None
        self.origin = 0.
        self._poll_after = None
        self.previous_grab = None
        self.loader=None;self.generation=None;self.key=None;self.source=None;self.duration=None
        self.view=(0.,1.);self.spans=[];self.data=None;self.enabled=False;self.paused=False;self.drag=None
        self._load_after=None;self._colors=None;self.playhead=None;self.closed=False
        self.empty_note = "発言を選ぶと音声の波形を表示します。" if compact else "波形：違いを選ぶと表示します。"
        self.note = tk.StringVar(self, value=self.empty_note)
        if not compact:
            header=ttk.Frame(self);header.pack(fill="x")
            ttk.Label(header,textvariable=self.note,style="Sub.TLabel").pack(side="left")
            self.full_button=ttk.Button(header,text="全体",width=6,command=lambda:self.set_view(0,self.duration or 1))
            self.full_button.pack(side="right")
            self.fit_button=ttk.Button(header,text="選択部分",width=9,command=self.fit)
            self.fit_button.pack(side="right",padx=(4,4))
        self.canvas=tk.Canvas(self,height=64 if compact else 80,highlightthickness=1,takefocus=True)
        self.canvas.pack(fill="x",pady=(2,0))
        if not compact:
            self.scrollbar=ttk.Scrollbar(self,orient="horizontal",command=self.scroll);self.scrollbar.pack(fill="x")
        self.canvas.bind("<MouseWheel>",self.wheel)
        self.canvas.bind("<Configure>",lambda e:self.draw())
        self.canvas.bind("<ButtonPress-1>",self.press);self.canvas.bind("<B1-Motion>",self.motion)
        self.canvas.bind("<ButtonRelease-1>",self.release);self.canvas.bind("<Escape>",lambda e:self.cancel_drag())
        self.canvas.bind("<Motion>",self.hover)
        self.bind("<<ThemeChanged>>",lambda e:self.draw())
        self.bind("<Destroy>",lambda e:self.close() if e.widget is self else None,add="+")

    def sync_clip(self, key, spans, duration, enabled, source, origin=0.):
        if self.closed:return
        colors=palette(self);changed=colors is not self._colors
        self._colors=colors
        duration = audio_duration(duration)
        self.enabled = bool(enabled and key is not None and duration)
        state="normal" if self.enabled else "disabled"
        if not self.compact:
            self.fit_button.configure(state=state);self.full_button.configure(state=state)
        if (key, source, origin, duration) != (self.key, self.source, self.origin, self.duration):
            self.cancel_drag(restore=False)
            self.pause()
            self.key, self.source, self.origin, self.duration = key, source, origin, duration
            self.data = self.overview = self.playhead = None
            self.spans = list(spans)
            self.note.set(self.empty_note if key is None else "波形用の音声が見つかりません。")
            self.paused=False
            if self.compact:
                self.view = (0., self.duration or 1.)
                self.schedule_load()
            else:
                self.fit()
            changed=True
        elif self.drag is None and self.spans!=list(spans):
            self.spans=list(spans);changed=True
            if not self.compact and spans and (spans[0][0]<self.view[0] or spans[-1][1]>self.view[1]):self.fit()
        if not self.enabled or self.winfo_toplevel().state()=="withdrawn":self.pause()
        elif self.paused:
            self.paused=False
            if self.data is None:self.schedule_load()
        if changed:self.draw()

    def fit(self):
        if not self.spans or not self.duration:self.draw();return
        a,b=self.spans[0][0],self.spans[-1][1];width=max(6.,(b-a)*1.5)
        self.set_view((a+b-width)/2,(a+b+width)/2)

    def bounded_width(self, width):
        return min(self.duration, max(.02 if self.compact else .5, width))

    def set_view(self,start,end):
        if not self.duration or self.drag is not None:return
        width = self.bounded_width(end-start)
        start = max(0., min(start, self.duration-width))
        if width == self.duration:
            new = (0., self.duration)
        else:
            new = (round(start, 6), min(self.duration, round(start+width, 6)))
        if new!=self.view or self.data is None:
            self.view = new
            # Draw existing peaks immediately, even while loading a finer envelope.
            if not (self.compact and self.data and self.data["start"] <= new[0] and new[1] <= self.data["end"]):
                self.data = self.overview
            if self.data and (self.data["step"] <= 1 / RATE or width == self.duration and self.data is self.overview):
                self.cancel_load()
            else:
                self.schedule_load()
            self.draw()

    def schedule_load(self):
        self.cancel_load()
        if self.source is None or not self.enabled or self.paused or self.closed:return
        self.note.set("波形を読み込み中…（時刻入力・音声再生も使えます）")
        self._load_after=self.after(120,self.load)

    def cancel_load(self):
        if self._load_after is not None:
            self.after_cancel(self._load_after)
            self._load_after=None
        if self._poll_after is not None:
            self.after_cancel(self._poll_after)
            self._poll_after = None
        if self.loader is not None:self.loader.stop()
        self.generation=None

    def load(self):
        self._load_after=None
        if self.closed or self.paused or not self.enabled or self.source is None:return
        try:
            if self.loader is None:self.loader=WaveformLoader()
            start, end = self.view
            self.generation = self.loader.load(self.source, self.origin + start, self.origin + end)
            self._poll_after = self.after(40, self.poll)
        except (OSError,ValueError,RuntimeError):
            self.note.set("波形を表示できません。時刻入力でも調整できます。")
            self.draw()

    def poll(self):
        self._poll_after = None
        while True:
            try:generation,state,data=self.loader.events.get_nowait()
            except queue.Empty:break
            if generation!=self.generation:continue
            if state=="ready":
                self.data = dict(data, start=data["start"] - self.origin, end=data["end"] - self.origin)
                if self.compact and self.view == (0., self.duration):
                    self.overview = self.data
                elif not self.compact:
                    self.note.set("波形：クリックで再生・移動 / ドラッグで範囲調整 / ホイールで拡大")
            elif state=="error":
                self.data = self.overview
                self.note.set("波形を表示できません。時刻入力でも調整できます。")
            self.draw()
            if state in ("ready", "error"):
                self.generation = None
        if self.generation is not None:
            self._poll_after = self.after(40, self.poll)

    def bounds(self):return 12.,max(13.,self.canvas.winfo_width()-12.)
    def x_at(self,seconds):
        left,right=self.bounds();return left+(seconds-self.view[0])/(self.view[1]-self.view[0])*(right-left)
    def time_at(self,x):
        left,right=self.bounds();return self.view[0]+max(0.,min(1.,(x-left)/(right-left)))*(self.view[1]-self.view[0])

    def draw(self):
        if self.closed or not self.canvas.winfo_exists():return
        c=palette(self);self._colors=c;canvas=self.canvas;canvas.delete("all")
        canvas.configure(background=c["surface"],highlightbackground=c["line"])
        left,right=self.bounds();height=max(40,canvas.winfo_height());top,bottom=18.,height-18.;mid=(top+bottom)/2
        if self.compact and self.data is None:
            canvas.create_text((left+right)/2,height/2,text=self.note.get(),fill=c["muted"],width=right-left)
            return
        for a,b in self.spans:
            if b>=self.view[0] and a<=self.view[1]:
                canvas.create_rectangle(max(left,self.x_at(a)),top,min(right,self.x_at(b)),bottom,fill=c["tint"],outline="",tags="selection")
        canvas.create_line(left,mid,right,mid,fill=c["line"])
        if self.data:
            data=self.data;low,high=data["low"],data["high"]
            first = max(0, math.floor((self.view[0] - data["start"]) / data["step"]))
            last = min(len(low), math.ceil((self.view[1] - data["start"]) / data["step"]))
            stride = max(1, math.ceil((last - first) / max(1, right-left)))
            peak=max(1,max(high,default=0),-min(low,default=0));upper=[];lower=[]
            for index in range(first, last, stride):
                stop = min(index + stride, last)
                x=self.x_at(data["start"]+(index+stop)/2*data["step"])
                if x<left or x>right:continue
                upper.extend((x,mid-max(high[index:stop])/peak*(bottom-top)/2))
                lower.append((x,mid-min(low[index:stop])/peak*(bottom-top)/2))
            points=upper+[v for pair in reversed(lower) for v in pair]
            if len(points)>=6:canvas.create_polygon(*points,fill=c["accent"] if self.enabled else c["muted"],outline="",tags="wave")
        if self.spans:
            for name,t in (("開始",self.spans[0][0]),("終了",self.spans[-1][1])):
                if self.view[0]<=t<=self.view[1]:
                    x=self.x_at(t);canvas.create_line(x,top-2,x,bottom+2,width=3,fill=c["ink"],tags="handle")
                    canvas.create_rectangle(x-4,top-4,x+4,top+3,fill=c["ink"],outline="",tags="handle")
                    anchor="e" if name=="開始" else "w";label_x=x-6 if name=="開始" else x+6
                    if label_x<left+30:anchor="w";label_x=max(left,label_x)
                    if label_x>right-30:anchor="e";label_x=min(right,label_x)
                    canvas.create_text(label_x,8,text=name,anchor=anchor,fill=c["ink"],font=("Yu Gothic UI",9))
        for i in range(3):
            t=self.view[0]+(self.view[1]-self.view[0])*i/2
            label = f"{t:.3f}秒" if self.compact else timestamp(t)
            if self.compact and i == 1:
                label = "離して反映 / Escで取消" if self.drag else "ホイールで拡大・縮小 / クリックで再生・移動 / ドラッグで範囲調整"
            canvas.create_text(self.x_at(t),height-8,text=label,anchor=("w","center","e")[i],fill=c["muted"],font=("Yu Gothic UI",8))
        canvas.create_line(0,top,0,bottom,fill=c["diff_whisper_ink"],width=2,tags="playhead",state="hidden")
        self.set_playhead(self.playhead)
        if not self.compact:
            if self.duration:self.scrollbar.set(self.view[0]/self.duration,self.view[1]/self.duration)
            else:self.scrollbar.set(0,1)

    def set_playhead(self,position):
        self.playhead=position
        if self.closed:return
        visible=position is not None and self.view[0]<=position<=self.view[1]
        self.canvas.itemconfigure("playhead",state="normal" if visible else "hidden")
        if visible:
            x=self.x_at(position);self.canvas.coords("playhead",x,18,x,max(22,self.canvas.winfo_height()-18));self.canvas.tag_raise("playhead")

    def handle_at(self,x):
        if not self.spans:return None
        times = (self.spans[0][0], self.spans[-1][1])
        distances = [abs(x-self.x_at(t)) if self.view[0] <= t <= self.view[1] else math.inf for t in times]
        index=0 if distances[0]<=distances[1] else 1
        return ("start","end")[index] if distances[index]<=10 else None

    def hover(self,event):
        self.canvas.configure(cursor="sb_h_double_arrow" if self.enabled and self.data and self.handle_at(event.x) else "crosshair" if self.enabled and self.data else "")

    def press(self,event):
        if self.drag is not None or not self.enabled or not self.data or not self.spans or self.duration < self.minimum:
            return
        self.previous_grab = self.canvas.grab_current()
        self.canvas.focus_set();self.canvas.grab_set()
        self.drag={"mode":self.handle_at(event.x) or "new","x":event.x,"y":event.y,"anchor":self.time_at(event.x),"clickable":True,
                   "span":(self.spans[0][0],self.spans[-1][1]),"spans":list(self.spans),
                   "form":(self.start_var.get(), self.end_var.get()),"key":self.key,"moved":False}

    def motion(self,event):
        drag=self.drag
        if drag is None:return
        if not self.enabled or self.key!=drag["key"]:self.cancel_drag(restore=False);return
        if abs(event.y-drag["y"])>=3:drag["clickable"]=False
        if abs(event.x-drag["x"])<3 and not drag["moved"]:return
        drag["moved"]=True;t=self.time_at(event.x);a,b=drag["span"]
        if drag["mode"]=="start":a=max(0.,min(t,b-self.minimum))
        elif drag["mode"]=="end":b=min(self.duration,max(t,a+self.minimum))
        else:
            a,b=sorted((drag["anchor"],t))
            if b-a<self.minimum:a=max(0.,min(a,self.duration-self.minimum));b=a+self.minimum
        a=round(a,3);b=min(self.duration,round(b,3))
        self.spans=[(a,b)]
        self.start_var.set(f"{a:.3f}" if self.compact else timestamp(a))
        self.end_var.set(f"{b:.3f}" if self.compact else timestamp(b))
        if self.drag_note is not None:self.drag_note.set("ドラッグ中：離すと反映 / Escで取消")
        self.draw()

    def release(self,event):
        if self.drag is None:return
        self.motion(event);drag=self.drag
        if drag is None:return
        self.drag=None;self.ungrab()
        if drag["moved"] and drag["key"]==self.key and self.enabled:
            if not self.apply_range():self.restore_drag(drag)
        else:
            self.spans=drag["spans"]
            if (self.seek and drag["clickable"] and drag["key"]==self.key and self.enabled
                    and 0<=event.x<self.canvas.winfo_width() and 0<=event.y<self.canvas.winfo_height()):
                self.seek(drag["anchor"])
        self.draw()

    def ungrab(self):
        try:
            if self.canvas.grab_current()==self.canvas:
                self.canvas.grab_release()
                if self.previous_grab is not None and self.previous_grab.winfo_exists():
                    self.previous_grab.grab_set()
        except tk.TclError:pass
        self.previous_grab = None

    def restore_drag(self,drag):
        self.start_var.set(drag["form"][0]);self.end_var.set(drag["form"][1])
        self.spans=drag["spans"]

    def cancel_drag(self,restore=True):
        drag=self.drag;self.drag=None
        if drag is not None:
            self.ungrab()
            if restore:self.restore_drag(drag)
            self.draw()
        return "break"

    def wheel(self,event):
        if not self.enabled or self.drag is not None or not event.delta:return "break"
        a,b=self.view;width=b-a
        if event.state&1:
            shift=width*.2*(-1 if event.delta>0 else 1);self.set_view(a+shift,b+shift)
        else:
            anchor=self.time_at(event.x);ratio=(anchor-a)/width
            new_width = self.bounded_width(width * (.75 if event.delta > 0 else 1/.75))
            if math.isclose(width, new_width, abs_tol=1e-6):return "break"
            self.set_view(anchor-ratio*new_width,anchor+(1-ratio)*new_width)
        return "break"

    def scroll(self,operation,value,units=None):
        if not self.enabled or self.drag is not None or not self.duration:return
        width=self.view[1]-self.view[0]
        start=float(value)*self.duration if operation=="moveto" else self.view[0]+int(value)*width*(.85 if units=="pages" else .1)
        self.set_view(start,start+width)

    def pause(self):
        if self.paused:return
        self.cancel_drag();self.cancel_load();self.paused=True

    def close(self):
        if self.closed:return
        self.pause();self.closed=True
        if self.loader is not None:self.loader.close()


class ComparisonWaveform(RangeWaveform):
    def __init__(self, parent, dialog):
        super().__init__(parent, dialog.audio_range_start, dialog.audio_range_end, dialog.apply_comparison_audio_range,
                         seek=dialog.seek_comparison_audio, drag_note=dialog.audio_range_note)
        self.dialog = dialog

    def sync(self, key, spans, duration, enabled):
        source = self.source
        if key != self.key:
            source = None
            if key is not None and audio_duration(duration):
                try:
                    source = comparison_audio_source(self.dialog.path, self.dialog.results, self.dialog.data or {})
                except (OSError, ValueError):
                    pass
        self.sync_clip(key, spans, duration, enabled, source)
