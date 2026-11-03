"""Interactive frame preview before exporting a subtitled MP4."""
from copy import deepcopy
import base64
from io import BytesIO
import json
from pathlib import Path
import queue
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
from tkinter import ttk, messagebox
from PIL import Image, ImageTk
from engine import ROOT, NO_WINDOW, timestamp
from live_process import ProcessJob
from vertical_subtitles import DIRECTIONS, POSITIONS
from video_layout import CaptionLayout, normalize_layout
from caption_timing_ui import CaptionTimingEditor


class VideoPreviewDialog(tk.Toplevel):
    def __init__(self, app, result, folder):
        super().__init__(app.root)
        self.withdraw()
        self.app, self.result, self.folder = app, deepcopy(result), Path(folder)
        self.closed = False; self.proc = None; self.owner = None; self.temp = None
        self.poll_id = self.seek_id = self.size_id = None
        self.events = queue.Queue(); self.layout = None; self.frame = None; self.busy = False
        self.play_frames=queue.Queue(1);self.play_epoch=0;self.play_state="stopped";self.want_playing=False
        self.resume_after_seek=False;self.has_audio=False;self.seek_value=tk.DoubleVar(value=0)
        self.target_time = self.shown_time = 0.; self.sequence = 0; self.bounds = None; self.drag_origin = None
        self.source = None; self.photo = None
        self.media_failed = False; self.caption_error = ""; self.problem_index = None; self.panel = None
        direction = DIRECTIONS[app.caption_direction.get()]
        try:
            initial = normalize_layout(result.get("video_caption_layout"), direction, app.vertical_position)
        except ValueError:
            initial = normalize_layout(None, direction, app.vertical_position)
        if initial["direction"] != direction or initial["position"] != app.vertical_position:
            initial.update(direction=direction, position=app.vertical_position, x=None, y=None)
        self.settings = initial
        self.direction = tk.StringVar(value=next(k for k,v in DIRECTIONS.items() if v == direction))
        self.side = tk.StringVar(); self.size = tk.DoubleVar(value=initial["font_scale"]*100)
        self.size_text = tk.StringVar(); self.time_text = tk.StringVar(value="")
        audio_only = self.audio_only = result.get("video_export_mode") == "audio_subtitles"
        self.caption_notice = tk.StringVar()
        self.status = tk.StringVar(value="録音した音声と黒背景を準備中…" if audio_only else "元動画を準備中… YouTubeは必要に応じて取得します。")
        self.title("音声と字幕のプレビュー・MP4保存（黒背景）" if audio_only else "字幕のプレビュー・MP4保存")
        self.transient(app.root)
        self.geometry(f"{min(1080,self.winfo_screenwidth()-80)}x{min(820,self.winfo_screenheight()-100)}")
        self.minsize(820, 620)
        outer = ttk.Frame(self, padding=16); outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1); outer.rowconfigure(2, weight=1)
        ttk.Label(outer, text="録音した音声を確認して、MP4に保存" if audio_only else "動画を確認して、字幕付きMP4に保存",
                  style="Preview.Title.TLabel").grid(row=0,column=0,sticky="w")
        self.source_hint = ttk.Label(outer, text="この録音に映像はありません。再生すると音声が流れ、字幕を黒背景で表示します。" if audio_only else
            "まず再生して確認してください。必要なときだけ、下のボタンから字幕を編集できます。", wraplength=980)
        self.source_hint.grid(row=1,column=0,sticky="ew",pady=(6,10))
        self.source_hint.bind("<Configure>",lambda e:self.source_hint.configure(wraplength=max(300,e.width)))
        self.style_panel = ttk.Frame(outer);self.style_panel.grid(row=5,column=0,sticky="ew",pady=6)
        controls = ttk.Frame(self.style_panel); controls.pack(fill="x",pady=(0,6))
        ttk.Label(controls,text="字幕の向き").pack(side="left")
        self.direction_combo = ttk.Combobox(controls,textvariable=self.direction,values=list(DIRECTIONS),width=7,state="readonly")
        self.direction_combo.pack(side="left",padx=(6,12)); self.direction_combo.bind("<<ComboboxSelected>>",self.change_direction)
        ttk.Label(controls,text="基準位置").pack(side="left")
        self.side_combo = ttk.Combobox(controls,textvariable=self.side,width=6,state="readonly")
        self.side_combo.pack(side="left",padx=6); self.side_combo.bind("<<ComboboxSelected>>",self.change_side)
        ttk.Button(controls,text="位置・サイズをリセット",command=self.reset).pack(side="right")
        self.canvas = tk.Canvas(outer,background="#10141d",highlightthickness=0,cursor="fleur")
        self.canvas.grid(row=2,column=0,sticky="nsew")
        self.canvas.bind("<Configure>",lambda e:self.draw())
        self.canvas.bind("<ButtonPress-1>",self.drag_start); self.canvas.bind("<B1-Motion>",self.drag)
        self.canvas.bind("<ButtonRelease-1>",lambda e:setattr(self,"drag_origin",None))
        size_row = ttk.Frame(self.style_panel);size_row.pack(fill="x",pady=(4,0))
        ttk.Label(size_row,text="文字サイズ").pack(side="left")
        self.size_scale = ttk.Scale(size_row,from_=50,to=200,variable=self.size,command=self.change_size,style="Preview.Horizontal.TScale")
        self.size_scale.pack(side="left",fill="x",expand=True,padx=12)
        ttk.Label(size_row,textvariable=self.size_text,width=7).pack(side="right")
        seek_row = ttk.Frame(outer);seek_row.grid(row=3,column=0,sticky="ew",pady=(12,6))
        self.play_button=ttk.Button(seek_row,text="準備中…",width=12,style="Accent.TButton",command=self.toggle_play,state="disabled")
        self.play_button.pack(side="left",padx=(0,8))
        self.restart_button = ttk.Button(seek_row,text="最初から",command=self.restart_playback,state="disabled")
        self.restart_button.pack(side="left")
        self.seek_scale = ttk.Scale(seek_row,from_=0,to=1,variable=self.seek_value,command=self.seek_changed,state="disabled",style="Preview.Horizontal.TScale")
        self.seek_scale.pack(side="left",fill="x",expand=True,padx=8)
        ttk.Label(seek_row,textvariable=self.time_text,width=25).pack(side="right")
        toolbar=ttk.Frame(outer);toolbar.grid(row=4,column=0,sticky="ew",pady=(0,4))
        self.edit_button=ttk.Button(toolbar,text="字幕を編集…",command=lambda:self.show_panel("edit"),state="disabled")
        self.edit_button.pack(side="left")
        self.style_button=ttk.Button(toolbar,text="位置・文字サイズ…",command=lambda:self.show_panel("style"),state="disabled")
        self.style_button.pack(side="left",padx=8)
        self.timing = CaptionTimingEditor(outer,self);self.timing.grid(row=6,column=0,sticky="ew")
        self.previous_button = ttk.Button(toolbar,text="前の字幕",command=lambda:self.step(-1))
        self.next_button = ttk.Button(toolbar,text="次の字幕",command=lambda:self.step(1))
        self.timing.grid_remove();self.style_panel.grid_remove()
        notice=self.notice_row=ttk.Frame(outer);notice.grid(row=7,column=0,sticky="ew")
        self.problem_button=ttk.Button(notice,text="問題の字幕を直す",command=self.edit_problem)
        self.problem_button.pack(side="right",padx=(8,0));self.problem_button.pack_forget()
        self.notice_label=ttk.Label(notice,textvariable=self.caption_notice,wraplength=760)
        self.notice_label.pack(side="left",fill="x",expand=True)
        self.notice_label.bind("<Configure>",lambda e:self.notice_label.configure(wraplength=max(300,e.width)))
        status_row=ttk.Frame(outer);status_row.grid(row=8,column=0,sticky="ew",pady=5)
        self.preparing=ttk.Progressbar(status_row,mode="indeterminate",length=110)
        self.preparing.pack(side="right",padx=(10,0));self.preparing.start(12)
        self.retry_button=ttk.Button(status_row,text="もう一度読み込む",command=self.start_prepare)
        status_label=ttk.Label(status_row,textvariable=self.status,wraplength=760)
        status_label.pack(side="left",fill="x",expand=True)
        status_label.bind("<Configure>",lambda e:status_label.configure(wraplength=max(300,e.width)))
        actions=ttk.Frame(outer);actions.grid(row=9,column=0,sticky="ew")
        self.save_button=ttk.Button(actions,text="MP4を保存…",style="Accent.TButton",command=self.save,state="disabled")
        self.save_button.pack(side="right")
        ttk.Button(actions,text="閉じる",command=self.destroy).pack(side="right",padx=8)
        self.sync_side();self.size_text.set(f"{round(self.size.get())}%")
        self.protocol("WM_DELETE_WINDOW",self.destroy)
        app.window_settings.attach(self,"video_preview")
        self.grab_set()
        self.start_prepare()

    def show_panel(self, name):
        self.panel = None if self.panel == name else name
        self.timing.grid_remove();self.style_panel.grid_remove()
        self.previous_button.pack_forget();self.next_button.pack_forget()
        if self.panel == "edit":
            self.timing.grid();self.previous_button.pack(side="left");self.next_button.pack(side="left",padx=6)
        elif self.panel == "style":self.style_panel.grid()
        self.edit_button.configure(text="字幕編集を閉じる" if self.panel=="edit" else "字幕を編集…")
        self.style_button.configure(text="見た目の調整を閉じる" if self.panel=="style" else "位置・文字サイズ…")

    def edit_problem(self):
        if self.panel != "edit":self.show_panel("edit")
        index=self.problem_index if self.problem_index is not None else (self.timing.indices[0] if self.timing.indices else None)
        if index is not None:
            if not self.timing.apply_pending():return
            self.timing.load(index)
            self.timing.feedback.set("文章を短くするか、開始・終了を調整して「反映」を押してください。")
            self.seek_to(max(0.,self.timing.interval(index)[0]-.2))

    def set_caption_problem(self, exc=None):
        self.caption_error = str(exc) if exc else "";self.problem_index=getattr(exc,"segment_index",None)
        self.caption_notice.set((self.caption_error+" 再生はできます。") if exc else "")
        if exc:
            self.notice_row.grid();self.problem_button.pack(side="right",padx=(8,0))
        else:self.problem_button.pack_forget();self.notice_row.grid_remove()
        if exc:self.save_button.configure(state="disabled")

    def finish_preparing(self):
        self.preparing.stop();self.preparing.pack_forget()

    def media_error(self, text):
        self.resume_after_seek=False;self.stop_playback()
        self.media_failed=True;self.busy=False;self.finish_preparing()
        self.status.set(text);self.save_button.configure(state="disabled")
        self.play_button.configure(text="再生できません",state="disabled")
        self.seek_scale.configure(state="disabled");self.restart_button.configure(state="disabled")
        self.retry_button.pack(side="right",padx=(8,0));self.draw()

    def release_worker(self):
        if self.owner:self.owner.close();self.owner=None
        if self.proc:
            if self.proc.poll() is None:self.proc.terminate()
            try:self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:self.proc.kill();self.proc.wait(timeout=5)
            if self.proc.stdin:self.proc.stdin.close()
            self.proc=None
        if self.temp:
            try:self.temp.cleanup()
            except OSError:pass
            self.temp=None

    def start_prepare(self):
        for timer in (self.poll_id,self.seek_id):
            if timer:self.after_cancel(timer)
        self.poll_id=self.seek_id=None;self.release_worker()
        self.events=queue.Queue();self.play_frames=queue.Queue(1);self.play_epoch+=1
        self.source=self.frame=self.layout=None;self.media_failed=False;self.busy=False
        self.want_playing=False;self.play_state="stopped";self.resume_after_seek=False
        self.set_caption_problem();self.retry_button.pack_forget()
        self.play_button.configure(text="準備中…",state="disabled");self.save_button.configure(state="disabled")
        for button in (self.edit_button,self.style_button,self.restart_button):button.configure(state="disabled")
        self.seek_scale.configure(state="disabled");self.time_text.set("")
        self.status.set("録音した音声を準備しています…" if self.audio_only else "動画を準備しています…")
        self.preparing.pack(side="right",padx=(10,0));self.preparing.start(12);self.draw()
        try:
            cache=ROOT/".cache"/"previews";cache.mkdir(parents=True,exist_ok=True)
            self.temp=tempfile.TemporaryDirectory(prefix="video-",dir=cache)
            python=Path(sys.executable).with_name("python.exe")
            self.proc=subprocess.Popen([str(python),"-X","utf8","-u",str(ROOT/"video_preview_worker.py")],stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,encoding="utf-8",errors="replace",creationflags=NO_WINDOW)
            self.owner=ProcessJob(self.proc)
            if not self.owner.handle:
                self.proc.terminate()
                raise RuntimeError("プレビュー用の動画処理を安全に起動できませんでした。画面を閉じて再度お試しください。")
            threading.Thread(target=self.read_events,args=(self.proc,self.events,self.play_frames),daemon=True).start()
            self.send({"result":self.result,"folder":str(self.folder)})
            self.poll_id=self.after(50,self.poll)
        except Exception as exc:
            self.release_worker();self.media_error("準備できませんでした："+str(exc))

    def sync_side(self):
        vertical=self.settings["direction"]=="vertical"
        self.side.set(next(k for k,v in POSITIONS.items() if v==self.settings["position"]) if vertical else "下中央")
        self.side_combo.configure(values=list(POSITIONS) if vertical else ["下中央"],state="readonly" if vertical else "disabled")

    def send(self, value):
        self.proc.stdin.write(json.dumps(value,ensure_ascii=False)+"\n");self.proc.stdin.flush()

    @staticmethod
    def read_events(proc, events, frames):
        detail=[]
        try:
            for line in proc.stdout:
                try:
                    event=json.loads(line)
                    if event.get("kind")=="play_frame":
                        try:frames.get_nowait()
                        except queue.Empty:pass
                        frames.put_nowait(event)
                    else:events.put(event)
                except ValueError:
                    if line.strip():detail.append(line.strip()[:500]);detail=detail[-6:]
        finally:
            proc.stdout.close()
            events.put({"kind":"exit","code":proc.wait(),"detail":"\n".join(detail)})

    def poll(self):
        self.poll_id=None
        if self.closed:return
        try:
            while True:
                event=self.events.get_nowait();kind=event.get("kind")
                if kind=="prepared":
                    self.source=Path(event["source"]);self.width=event["width"];self.height=event["height"];self.duration=event["duration"]
                    self.has_audio=event.get("audio",False)
                    self.maximum=max(.01,(self.duration or max(float(c["end"]) for c in self.result["segments"]))-.04)
                    self.seek_scale.configure(to=self.maximum,state="normal")
                    for button in (self.edit_button,self.style_button,self.restart_button):button.configure(state="normal")
                    self.rebuild()
                elif kind=="frame":
                    self.busy=False
                    if abs(event["seconds"]-self.target_time)<.001:
                        if self.seek_id:self.after_cancel(self.seek_id);self.seek_id=None
                        with Image.open(event["frame"]) as image:self.frame=image.convert("RGB")
                        self.shown_time=event["seconds"];self.finish_preparing()
                        self.status.set("再生できます。確認が済んだら「MP4を保存…」を押してください。")
                        self.draw();self.save_button.configure(state="normal" if self.layout else "disabled")
                        self.play_button.configure(text="▶ 再生",state="normal");self.update_timebar(self.shown_time)
                        if self.resume_after_seek:
                            self.resume_after_seek=False;self.start_playback()
                    else:self.request_frame()
                elif kind=="frame_error":
                    self.busy=False;self.finish_preparing();self.status.set(event["text"])
                    if abs(self.requested_time-self.target_time)>=.001:self.request_frame()
                    else:
                        self.resume_after_seek=False
                        self.play_button.configure(text="▶ 再生",state="normal" if self.frame is not None else "disabled")
                        self.retry_button.pack(side="right",padx=(8,0))
                elif kind=="play_state" and event["epoch"]==self.play_epoch:
                    state=event["state"]
                    if state in ("starting","playing") and not self.want_playing:continue
                    if state=="paused" and self.want_playing:continue
                    self.play_state=state
                    if state in ("paused","ended"):
                        self.want_playing=False;self.play_button.configure(text="再生")
                        self.shown_time=min(self.duration or event["seconds"],event["seconds"])
                        self.target_time=self.shown_time;self.update_timebar(self.shown_time);self.draw()
                        self.status.set("一時停止中。字幕を調整して再生を再開できます。" if state=="paused" else "最後まで再生しました。「再生」で最初から再生できます。")
                    else:
                        self.status.set("再生準備中…" if state=="starting" else "再生中。文章・話者名・時刻を編集できます。位置・サイズは動画全体に適用します。"+("（音声のない動画）" if not self.has_audio else ""))
                elif kind=="play_error" and event["epoch"]==self.play_epoch:
                    self.stop_playback();self.status.set(event["text"].splitlines()[0][:240])
                elif kind in ("status","error"):
                    text=event.get("text", "動画を準備中…")
                    self.status.set(text.splitlines()[0][:240] if kind=="error" else text)
                    if kind=="error":
                        self.media_error(text)
                elif kind=="exit" and not self.closed:
                    self.save_button.configure(state="disabled")
                    self.want_playing=False;self.play_state="stopped";self.play_button.configure(text="再生",state="disabled")
                    if not self.media_failed:
                        detail=event.get("detail","").splitlines()
                        self.media_error("プレビュー処理が終了しました。「もう一度読み込む」で再試行できます。"+
                                         (("\n"+detail[-1][:240]) if detail else ""))
        except queue.Empty:pass
        except Exception as exc:
            self.media_error("プレビューを表示できません："+str(exc))
        try:
            event=self.play_frames.get_nowait()
            if event["epoch"]==self.play_epoch and self.play_state in ("starting","playing","pausing","paused","ended"):
                with Image.open(BytesIO(base64.b64decode(event["jpeg"]))) as image:self.frame=image.convert("RGB")
                if self.play_state not in ("paused","ended"):
                    self.shown_time=event["seconds"];self.target_time=self.shown_time;self.update_timebar(self.shown_time)
                self.draw()
        except queue.Empty:pass
        except Exception as exc:
            self.stop_playback();self.status.set(f"映像を表示できません：{exc}")
        self.poll_id=self.after(15 if self.want_playing else 50,self.poll)

    def update_timebar(self, seconds):
        self.seek_value.set(max(0.,min(self.maximum,seconds)))
        self.time_text.set(f"{timestamp(seconds)[:-1]} / {timestamp(self.duration or self.maximum)[:-1]}")

    def toggle_play(self):
        if self.source is None or self.media_failed or self.frame is None:return
        if self.want_playing:
            self.want_playing=False;self.play_state="pausing";self.play_button.configure(text="再生")
            self.send({"op":"pause","epoch":self.play_epoch})
        elif self.play_state in ("paused","pausing"):
            self.want_playing=True;self.play_button.configure(text="一時停止")
            self.send({"op":"resume","epoch":self.play_epoch})
        elif self.play_state=="ended" or self.shown_time>=self.maximum:
            self.resume_after_seek=True;self.seek_to(0)
        else:self.start_playback()

    def start_playback(self):
        if self.busy or self.seek_id or self.frame is None or self.source is None or self.media_failed:return
        self.play_epoch+=1;self.want_playing=True;self.play_state="starting"
        self.play_button.configure(text="一時停止");self.status.set("再生準備中…")
        try:self.send({"op":"play","epoch":self.play_epoch,"seconds":self.shown_time})
        except (OSError,ValueError) as exc:
            self.want_playing=False;self.play_state="stopped";self.play_button.configure(text="再生")
            self.status.set(f"再生を開始できません：{exc}")

    def restart_playback(self):
        if self.source is None or self.media_failed:return
        self.resume_after_seek=True;self.seek_to(0)

    def stop_playback(self):
        active=self.play_state in ("starting","playing","pausing","paused")
        self.play_epoch+=1;self.want_playing=False;self.play_state="stopped";self.play_button.configure(text="再生")
        if active:
            try:self.send({"op":"stop","epoch":self.play_epoch})
            except (OSError,ValueError):pass

    def rebuild(self):
        if self.source is None:return False
        try:
            self.layout=CaptionLayout(self.result,self.width,self.height,self.duration,self.settings)
            self.set_caption_problem()
        except ValueError as exc:
            self.layout=None;self.set_caption_problem(exc)
        if self.frame is not None and not self.busy and abs(self.shown_time-self.target_time)<.001:
            self.save_button.configure(state="normal" if self.layout and not self.media_failed else "disabled")
        elif self.frame is None and not self.busy and hasattr(self,"maximum"):
            first=self.layout.cues[0] if self.layout else None
            self.seek_to((first["start_cs"]+first["end_cs"])/200 if first else 0)
        self.draw()
        return self.layout is not None

    def change_direction(self, event=None):
        self.settings.update(direction=DIRECTIONS[self.direction.get()],x=None,y=None)
        self.sync_side();self.rebuild()

    def change_side(self, event=None):
        self.settings.update(position=POSITIONS[self.side.get()],x=None,y=None);self.rebuild()

    def reset(self):
        self.settings.update(font_scale=1.,x=None,y=None);self.size.set(100);self.size_text.set("100%");self.rebuild()

    def change_size(self, value=None):
        self.size_text.set(f"{round(self.size.get())}%")
        if self.size_id:self.after_cancel(self.size_id)
        self.size_id=self.after(80,self.apply_size)

    def apply_size(self):
        self.size_id=None;self.settings["font_scale"]=max(.5,min(2.,round(self.size.get())/100))
        self.rebuild()

    def seek_changed(self, value):
        if self.source is None or self.media_failed:return
        self.resume_after_seek=self.resume_after_seek or self.want_playing
        if self.play_state!="stopped":self.stop_playback()
        self.target_time=max(0.,min(self.maximum,float(value)))
        self.time_text.set(f"{timestamp(self.target_time)[:-1]} / {timestamp(self.duration or self.maximum)[:-1]}")
        self.save_button.configure(state="disabled")
        self.play_button.configure(state="disabled")
        if self.seek_id:self.after_cancel(self.seek_id)
        self.seek_id=self.after(140,self.request_frame)

    def seek_to(self, value):
        self.seek_value.set(max(0.,min(self.maximum,value)))
        self.seek_changed(self.seek_value.get())

    def request_frame(self):
        if self.seek_id:self.after_cancel(self.seek_id)
        self.seek_id=None
        if self.busy or self.closed or self.source is None or self.media_failed:return
        self.busy=True;self.sequence+=1;self.requested_time=self.target_time
        self.status.set("選んだ時刻の映像を読み込み中…")
        try:self.send({"seconds":self.target_time,"token":self.sequence,"frame":str(Path(self.temp.name)/"frame.png")})
        except (OSError,ValueError) as exc:
            self.media_error(f"映像を読み込めません：{exc}")

    def step(self, delta):
        if self.source is None:return
        if not self.timing.apply_pending():return
        if not self.layout:
            if not self.timing.indices:return
            current=self.timing.indices.index(self.timing.index) if self.timing.index in self.timing.indices else 0
            target=self.timing.indices[max(0,min(len(self.timing.indices)-1,current+delta))]
            self.timing.load(target);self.seek_to(max(0.,self.timing.interval(target)[0]-.2));return
        index=self.layout.index_at(self.target_time)
        if index is None:
            candidates=[i for i,c in enumerate(self.layout.cues) if (c["start_cs"]/100>self.target_time if delta>0 else c["end_cs"]/100<=self.target_time)]
            if not candidates:return
            index=candidates[0] if delta>0 else candidates[-1]
        else:index=max(0,min(len(self.layout.cues)-1,index+delta))
        cue=self.layout.cues[index];self.timing.load(cue["segment_index"])
        self.seek_to((cue["start_cs"]+cue["end_cs"])/200)

    def draw(self):
        if self.closed:return
        self.canvas.delete("all");self.bounds=None
        if self.frame is None:
            self.canvas.create_text(max(1,self.canvas.winfo_width())/2,max(1,self.canvas.winfo_height())/2,
                text="読み込みを完了できませんでした\n下の説明を確認して、もう一度読み込んでください。" if self.media_failed else
                     ("音声を準備しています…\nこの録音には映像がないため、背景は黒で表示します。" if self.audio_only else "動画を準備しています…"),
                fill="#d8deeb",font=("Yu Gothic UI",13),justify="center",width=max(260,self.canvas.winfo_width()-60))
            return
        self.timing.ensure_selection()
        cw,ch=max(1,self.canvas.winfo_width()),max(1,self.canvas.winfo_height())
        factor=min(cw/self.width,ch/self.height)
        width,height=max(1,round(self.width*factor)),max(1,round(self.height*factor))
        ox,oy=(cw-width)/2,(ch-height)/2
        composite=self.frame.resize((width,height),Image.Resampling.LANCZOS).convert("RGBA")
        index=self.layout.index_at(self.shown_time) if self.layout else None
        if index is not None:
            bitmap=self.layout.bitmap(index);x,y=self.layout.placement(bitmap)
            displayed=bitmap.resize((max(1,round(bitmap.width*factor)),max(1,round(bitmap.height*factor))),Image.Resampling.LANCZOS)
            px,py=round(x*factor),round(y*factor);composite.alpha_composite(displayed,(px,py))
            self.bounds=(ox+px,oy+py,ox+px+displayed.width,oy+py+displayed.height)
        self.photo=ImageTk.PhotoImage(composite,master=self)
        self.canvas.create_image(ox,oy,anchor="nw",image=self.photo)
        if self.audio_only and index is None:
            self.canvas.create_text(cw/2,ch/2,text="音声のみの録音です"+("\n字幕は下の「問題の字幕を直す」から修正できます。" if self.caption_error else
                "\nこの位置には字幕がありません。再生すると音声が流れます。"),fill="#adb7cb",font=("Yu Gothic UI",12),justify="center",width=max(260,cw-60))
        if self.bounds:self.canvas.create_rectangle(*self.bounds,outline="#8d85ff",width=2,dash=(5,3))
        self.transform=(factor,ox,oy)

    def drag_start(self,event):
        if self.bounds and self.bounds[0]-8<=event.x<=self.bounds[2]+8 and self.bounds[1]-8<=event.y<=self.bounds[3]+8:
            factor,ox,oy=self.transform
            cx=(self.bounds[0]+self.bounds[2])/2;cy=(self.bounds[1]+self.bounds[3])/2
            self.drag_origin=(event.x-cx,event.y-cy)

    def drag(self,event):
        if self.drag_origin is None:return
        factor,ox,oy=self.transform;dx,dy=self.drag_origin
        self.settings["x"]=max(0.,min(1.,(event.x-dx-ox)/factor/self.width))
        self.settings["y"]=max(0.,min(1.,(event.y-dy-oy)/factor/self.height))
        self.layout.settings.update(x=self.settings["x"],y=self.settings["y"]);self.draw()

    def save(self):
        if self.media_failed or not self.layout or self.frame is None or self.busy or self.seek_id:return
        if not self.timing.apply_pending():return
        self.resume_after_seek=False;self.stop_playback()
        if self.size_id:self.after_cancel(self.size_id);self.size_id=None
        try:
            self.apply_size()
            if self.layout is None:return
            self.settings=normalize_layout(self.settings)
            self.app.save_video_preview(self)
        except Exception as exc:messagebox.showerror("動画を保存できません",str(exc),parent=self)

    def destroy(self):
        if self.closed:return
        self.app.window_settings.remember("video_preview",self)
        self.closed=True
        for timer in (self.poll_id,self.seek_id,self.size_id):
            if timer:
                try:self.after_cancel(timer)
                except tk.TclError:pass
        self.preparing.stop();self.release_worker()
        if getattr(self.app,"video_preview",None) is self:self.app.video_preview=None
        super().destroy()
