"""Click to seek; drag to commit a range once on release."""
import math
import queue
import tkinter as tk
from tkinter import ttk

from comparison_audio import comparison_audio_source
from comparison_audio_range import audio_duration
from comparison_waveform import WaveformLoader
from engine import timestamp
from ui_theme import palette


class ComparisonWaveform(ttk.Frame):
    def __init__(self,parent,dialog):
        super().__init__(parent);self.dialog=dialog;self.root=dialog.app.root
        self.loader=None;self.generation=None;self.key=None;self.source=None;self.duration=None
        self.view=(0.,1.);self.spans=[];self.data=None;self.enabled=False;self.paused=False;self.drag=None
        self._load_after=None;self._colors=None;self.playhead=None;self.closed=False
        header=ttk.Frame(self);header.pack(fill="x")
        self.note=tk.StringVar(value="波形：違いを選ぶと表示します。")
        ttk.Label(header,textvariable=self.note,style="Sub.TLabel").pack(side="left")
        self.full_button=ttk.Button(header,text="全体",width=6,command=lambda:self.set_view(0,self.duration or 1))
        self.full_button.pack(side="right")
        self.fit_button=ttk.Button(header,text="選択部分",width=9,command=self.fit)
        self.fit_button.pack(side="right",padx=(4,4))
        self.canvas=tk.Canvas(self,height=80,highlightthickness=1,takefocus=True)
        self.canvas.pack(fill="x",pady=(2,0))
        self.scrollbar=ttk.Scrollbar(self,orient="horizontal",command=self.scroll);self.scrollbar.pack(fill="x")
        self.canvas.bind("<Configure>",lambda e:self.draw())
        self.canvas.bind("<ButtonPress-1>",self.press);self.canvas.bind("<B1-Motion>",self.motion)
        self.canvas.bind("<ButtonRelease-1>",self.release);self.canvas.bind("<Escape>",lambda e:self.cancel_drag())
        self.canvas.bind("<MouseWheel>",self.wheel)
        self.canvas.bind("<Motion>",self.hover)
        self.bind("<Destroy>",lambda e:self.close() if e.widget is self else None,add="+")

    def sync(self,key,spans,duration,enabled):
        if self.closed:return
        colors=palette(self);changed=colors is not self._colors
        self._colors=colors
        self.enabled=bool(enabled and key is not None and audio_duration(duration))
        state="normal" if self.enabled else "disabled"
        self.fit_button.configure(state=state);self.full_button.configure(state=state)
        if key!=self.key:
            self.cancel_drag(restore=False);self.pause();self.key=key;self.source=None;self.data=None;self.playhead=None
            self.spans=list(spans);self.duration=audio_duration(duration)
            if key is not None and self.duration:
                try:self.source=comparison_audio_source(self.dialog.path,self.dialog.results,self.dialog.data or {})
                except (OSError,ValueError):self.note.set("波形用の音声がありません。時刻入力でも調整できます。")
            else:self.note.set("波形：違いを選ぶと表示します。")
            self.paused=False;self.fit();changed=True
        elif self.drag is None and self.spans!=list(spans):
            self.spans=list(spans);changed=True
            if spans and (spans[0][0]<self.view[0] or spans[-1][1]>self.view[1]):self.fit()
        if not self.enabled or self.dialog.state()=="withdrawn":self.pause()
        elif self.paused:
            self.paused=False
            if self.data is None:self.schedule_load()
        if changed:self.draw()
        self.poll()

    def fit(self):
        if not self.spans or not self.duration:self.draw();return
        a,b=self.spans[0][0],self.spans[-1][1];width=max(6.,(b-a)*1.5)
        self.set_view((a+b-width)/2,(a+b+width)/2)

    def set_view(self,start,end):
        if not self.duration or self.drag is not None:return
        width=min(self.duration,max(min(.5,self.duration),end-start))
        start=max(0.,min(start,self.duration-width));new=(round(start,6),round(start+width,6))
        if new!=self.view or self.data is None:
            self.view=new;self.data=None;self.schedule_load();self.draw()

    def schedule_load(self):
        self.cancel_load()
        if self.source is None or not self.enabled or self.paused or self.closed:return
        self.note.set("波形を読み込み中…（時刻入力・音声再生も使えます）")
        self._load_after=self.root.after(120,self.load)

    def cancel_load(self):
        if self._load_after is not None:
            try:self.root.after_cancel(self._load_after)
            except tk.TclError:pass
            self._load_after=None
        if self.loader is not None:self.loader.stop()
        self.generation=None

    def load(self):
        self._load_after=None
        if self.closed or self.paused or not self.enabled or self.source is None:return
        try:
            if self.loader is None:self.loader=WaveformLoader()
            self.generation=self.loader.load(self.source,*self.view)
        except (OSError,ValueError,RuntimeError):self.note.set("波形を表示できません。時刻入力でも調整できます。")

    def poll(self):
        if self.loader is None:return
        while True:
            try:generation,state,data=self.loader.events.get_nowait()
            except queue.Empty:break
            if generation!=self.generation:continue
            if state=="ready":
                self.data=data;self.note.set("波形：クリックで再生・移動 / ドラッグで範囲調整 / ホイールで拡大")
            elif state=="error":self.data=None;self.note.set("波形を表示できません。時刻入力でも調整できます。")
            self.draw()

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
        for a,b in self.spans:
            if b>=self.view[0] and a<=self.view[1]:
                canvas.create_rectangle(max(left,self.x_at(a)),top,min(right,self.x_at(b)),bottom,fill=c["tint"],outline="",tags="selection")
        canvas.create_line(left,mid,right,mid,fill=c["line"])
        if self.data:
            data=self.data;low,high=data["low"],data["high"];stride=max(1,math.ceil(len(low)/max(1,right-left)))
            peak=max(1,max(high,default=0),-min(low,default=0));upper=[];lower=[]
            for index in range(0,len(low),stride):
                x=self.x_at(data["start"]+(index+min(stride,len(low)-index)/2)*data["step"])
                if x<left or x>right:continue
                upper.extend((x,mid-max(high[index:index+stride])/peak*(bottom-top)/2))
                lower.append((x,mid-min(low[index:index+stride])/peak*(bottom-top)/2))
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
            canvas.create_text(self.x_at(t),height-8,text=timestamp(t),anchor=("w","center","e")[i],fill=c["muted"],font=("Yu Gothic UI",8))
        canvas.create_line(0,top,0,bottom,fill=c["diff_whisper_ink"],width=2,tags="playhead",state="hidden")
        self.set_playhead(self.playhead)
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
        positions=[self.x_at(self.spans[0][0]),self.x_at(self.spans[-1][1])]
        distances=[abs(x-v) for v in positions]
        index=0 if distances[0]<=distances[1] else 1
        return ("start","end")[index] if distances[index]<=10 else None

    def hover(self,event):
        self.canvas.configure(cursor="sb_h_double_arrow" if self.enabled and self.data and self.handle_at(event.x) else "crosshair" if self.enabled and self.data else "")

    def press(self,event):
        if not self.enabled or not self.data or not self.spans or not self.duration or self.duration<.1:return
        self.canvas.focus_set();self.canvas.grab_set()
        self.drag={"mode":self.handle_at(event.x) or "new","x":event.x,"y":event.y,"anchor":self.time_at(event.x),"clickable":True,
                   "span":(self.spans[0][0],self.spans[-1][1]),"spans":list(self.spans),
                   "form":self.dialog._comparison_audio_range_form(),"key":self.key,"moved":False}

    def motion(self,event):
        drag=self.drag
        if drag is None:return
        if not self.enabled or self.key!=drag["key"]:self.cancel_drag(restore=False);return
        if abs(event.y-drag["y"])>=3:drag["clickable"]=False
        if abs(event.x-drag["x"])<3 and not drag["moved"]:return
        drag["moved"]=True;t=self.time_at(event.x);a,b=drag["span"]
        if drag["mode"]=="start":a=max(0.,min(t,b-.1))
        elif drag["mode"]=="end":b=min(self.duration,max(t,a+.1))
        else:
            a,b=sorted((drag["anchor"],t))
            if b-a<.1:a=max(0.,min(a,self.duration-.1));b=a+.1
        a=round(a,3);b=min(self.duration,round(b,3))
        self.spans=[(a,b)];self.dialog.audio_range_start.set(timestamp(a));self.dialog.audio_range_end.set(timestamp(b))
        self.dialog.audio_range_note.set("ドラッグ中：離すと反映 / Escで取消")
        self.draw()

    def release(self,event):
        if self.drag is None:return
        self.motion(event);drag=self.drag
        if drag is None:return
        self.drag=None;self.ungrab()
        if drag["moved"] and drag["key"]==self.key and self.enabled:
            if not self.dialog.apply_comparison_audio_range():self.restore_drag(drag)
        else:
            self.spans=drag["spans"]
            if (drag["clickable"] and drag["key"]==self.key and self.enabled
                    and 0<=event.x<self.canvas.winfo_width() and 0<=event.y<self.canvas.winfo_height()):
                self.dialog.seek_comparison_audio(drag["anchor"])
        self.draw()

    def ungrab(self):
        try:
            if self.canvas.grab_current()==self.canvas:self.canvas.grab_release()
        except tk.TclError:pass

    def restore_drag(self,drag):
        self.spans=drag["spans"]
        self.dialog.audio_range_start.set(drag["form"][0]);self.dialog.audio_range_end.set(drag["form"][1])

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
            anchor=self.time_at(event.x);ratio=(anchor-a)/width;new_width=width*(.75 if event.delta>0 else 1/.75)
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
