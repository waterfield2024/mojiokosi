"""Side-by-side raw model results and clearly scoped elapsed times."""
import json
from pathlib import Path
import time
import tkinter as tk
from tkinter import ttk,messagebox

from comparison import WHISPER_CHOICES,SIDES,read_comparison,result_path,aligned_blocks,seconds_text,interrupt_comparison
from comparison_diff import difference_ranges
from comparison_merge_ui import MergeControls
from engine import timestamp,youtube_url
from speakers import speaker_name
from ui_theme import style_text,style_change


class ComparisonDialog(MergeControls,tk.Toplevel):
    def __init__(self,app,request):
        super().__init__(app.root)
        super().withdraw()
        self.app,self.request = app,dict(request)
        self.path = None;self.data = None;self.results = {};self.rows = []
        self._resize = self._timer = None;self._rendering = False;self._width = 0
        self._running_at = {};self._run_key = None
        self._diff_sources = None;self._differences = {}
        self.title("WhisperとGraniteを比較")
        self.geometry("1100x860");self.minsize(840,680)
        self.transient(app.root)
        outer = ttk.Frame(self,padding=16);outer.pack(fill="both",expand=True)
        ttk.Label(outer,text="WhisperとGraniteを比較",font=("Yu Gothic UI",16,"bold")).pack(anchor="w")
        self.source = tk.StringVar(value=request.get("source",""))
        ttk.Entry(outer,textvariable=self.source,state="readonly").pack(fill="x",pady=(8,10))
        toolbar = ttk.Frame(outer);toolbar.pack(fill="x")
        ttk.Label(toolbar,text="Whisperのモデル").pack(side="left",padx=(0,8))
        selected = request.get("whisper_model","small")
        self.model = tk.StringVar(value=next((k for k,v in WHISPER_CHOICES.items() if v==selected),next(iter(WHISPER_CHOICES))))
        self.model_combo = ttk.Combobox(toolbar,textvariable=self.model,values=list(WHISPER_CHOICES),state="readonly",width=24)
        self.model_combo.pack(side="left")
        self.model_combo.bind("<<ComboboxSelected>>",lambda event:self.render())
        self.stop_button = ttk.Button(toolbar,text="停止",command=app.cancel);self.stop_button.pack(side="right")
        self.start_button = ttk.Button(toolbar,text="比較を開始",style="Accent.TButton",command=self.start)
        self.start_button.pack(side="right",padx=(12,8))
        self.conditions = tk.StringVar()
        ttk.Label(outer,textvariable=self.conditions,style="Sub.TLabel",wraplength=980).pack(anchor="w",pady=(9,3))
        self.summary = tk.StringVar(value="同じ動画・音声を、一度の操作で両モデルにかけられます。")
        ttk.Label(outer,textvariable=self.summary,style="Section.TLabel",wraplength=980).pack(anchor="w",pady=(3,6))
        legend = ttk.Frame(outer);legend.pack(fill="x",pady=(0,8))
        ttk.Label(legend,text="違う部分",style="Sub.TLabel").pack(side="left",padx=(0,8))
        for side,label in (("whisper","Whisper：オレンジ"),("granite","Granite：青")):
            ttk.Label(legend,text=label,style=f"Diff.{side}.TLabel").pack(side="left",padx=(0,8))
        self.diff_summary = tk.StringVar(value="両方の文字起こしが完了すると色分けします。")
        ttk.Label(legend,textvariable=self.diff_summary,style="Sub.TLabel").pack(side="left")
        self.build_merge_controls(outer)
        panes = ttk.Frame(outer);panes.pack(fill="both",expand=True)
        panes.columnconfigure(0,weight=1,uniform="models");panes.columnconfigure(1,weight=1,uniform="models");panes.rowconfigure(2,weight=1)
        self.texts = {};self.titles = {};self.times = {};self.use_buttons = {}
        for column,side in enumerate(SIDES):
            self.titles[side] = tk.StringVar(value="Whisper" if side=="whisper" else "Granite 5.0 TurboCTC")
            self.times[side] = tk.StringVar(value="未実行")
            ttk.Label(panes,textvariable=self.titles[side],style="Section.TLabel").grid(row=0,column=column,sticky="w",padx=(0 if column==0 else 12,0))
            ttk.Label(panes,textvariable=self.times[side],style="Sub.TLabel").grid(row=1,column=column,sticky="w",pady=(4,7),padx=(0 if column==0 else 12,0))
            text = tk.Text(panes,wrap="word",font=("Yu Gothic UI",11),width=1,height=4,spacing1=0,spacing2=0,spacing3=0)
            style_text(text);style_change(text,"diff_"+side);text.tag_raise("sel")
            text.tag_configure("time",font=("Yu Gothic UI",11,"bold"))
            text.grid(row=2,column=column,sticky="nsew",padx=(0,6) if column==0 else (6,0))
            text.configure(state="disabled");self.texts[side] = text
            text.bind("<MouseWheel>",self.wheel)
            for event,amount,units in (("<Prior>",-1,"pages"),("<Next>",1,"pages"),("<Up>",-1,"units"),("<Down>",1,"units")):
                text.bind(event,lambda e,a=amount,u=units:self.scroll_key("scroll",a,u))
            text.bind("<Control-Home>",lambda e:self.scroll_key("moveto",0))
            text.bind("<Control-End>",lambda e:self.scroll_key("moveto",1))
            text.bind("<ButtonRelease-1>",self.click_difference,add="+")
            actions=ttk.Frame(panes);actions.grid(row=3,column=column,sticky="w",pady=(8,0),padx=(0 if column==0 else 12,0))
            choose=ttk.Button(actions,text=side+"を採用",command=lambda s=side:self.choose_difference(s))
            choose.pack(side="left",padx=(0,6));self.merge_buttons[side]=choose
            button = ttk.Button(actions,text="全文を編集・出力",command=lambda s=side:self.use_result(s))
            button.pack(side="left");self.use_buttons[side] = button
        self.scrollbar = ttk.Scrollbar(panes,orient="vertical",command=self.scroll)
        self.scrollbar.grid(row=2,column=2,sticky="ns",padx=(6,0))
        self.texts["whisper"].configure(yscrollcommand=self.scrollbar.set)
        panes.bind("<Configure>",self.resized)
        footer=ttk.Frame(outer);footer.pack(side="bottom",fill="x",before=panes,pady=(8,0))
        self.merge_help_label=ttk.Label(footer,text="共通文・字幕時刻・話者はWhisperが基準。追加の発言はGraniteを引き継ぎます。選択は自動保存します。",style="Sub.TLabel",wraplength=980)
        self.merge_help_label.pack(fill="x")
        self.merge_help_label.bind("<Configure>",lambda e:self.merge_help_label.configure(wraplength=max(300,e.width)))
        self.status = tk.StringVar(value="波形をクリックすると、その位置から再生します。再生中もクリックで移動でき、ドラッグで範囲を調整できます。")
        status = ttk.Label(footer,textvariable=self.status,style="Sub.TLabel",wraplength=980);self.status_label=status
        status.pack(fill="x",pady=(6,0));status.bind("<Configure>",lambda e:status.configure(wraplength=max(300,e.width)))
        self.protocol("WM_DELETE_WINDOW",self.withdraw)
        self.bind("<Destroy>",self.on_destroy,add="+")
        app.window_settings.attach(self,"comparison")
        self.render();self.tick()

    def busy(self):return self.app.proc is not None

    def start(self):
        if self.busy():return
        try:
            req = dict(self.request);req["whisper_model"] = WHISPER_CHOICES[self.model.get()]
            if req["mode"] == "youtube":req["source"] = youtube_url(req["source"])
            elif not Path(req["source"]).is_file():raise ValueError("元の音声・動画が見つかりません。メイン画面で選び直してください。")
            req.update(operation="compare",prompt="",context_correct=False,correction_rules=[])
            self.request = req;self.path = self.app.comparison_path = None
            self.data = None;self.results = {};self._running_at = {};self.render()
            self.status.set("比較を準備しています…")
            self.app.launch_worker(req)
            self.update_controls()
        except Exception as exc:messagebox.showerror("比較を開始できません",str(exc),parent=self)

    def show_saved(self,path):
        path = Path(path);data = read_comparison(path)
        if self.path != path:self._running_at = {};self.scroll("moveto",0)
        self.path,self.data = path,data
        self.source.set(data["source"])
        self.results = {}
        for side,entry in data["results"].items():
            if entry["state"] == "complete":
                try:
                    result = json.loads(result_path(path,side).read_text("utf-8"))
                    aligned_blocks(result["segments"],[])  # validate times before rendering or using a result
                    self.results[side] = result
                except (OSError,ValueError,KeyError,TypeError) as exc:entry["load_error"] = "保存結果を開けません："+str(exc)
            if entry["state"] == "running" and side not in self._running_at:self._running_at[side] = time.monotonic()
        self.render();self.update_controls()

    def update_controls(self):
        busy = self.busy()
        self.start_button.configure(state="disabled" if busy else "normal",text="もう一度比較" if self.data else "比較を開始")
        self.stop_button.configure(state="normal" if busy and self.app.operation=="compare" and not self.app.cancelled else "disabled")
        self.model_combo.configure(state="disabled" if busy else "readonly")
        for side,button in self.use_buttons.items():button.configure(state="normal" if not busy and side in self.results else "disabled")
        self.update_merge_controls()

    def render(self):
        if self._rendering or not self.winfo_exists():return
        self._rendering = True
        try:
            fraction = self.texts["whisper"].yview()[0]
            data = self.data or {}
            language = {"ja":"日本語","en":"英語","auto":"自動判別"}.get(data.get("whisper_language",self.request.get("language","ja")),"自動判別")
            compute = {"auto":"自動","gpu":"GPU優先","cpu":"CPU"}.get(data.get("compute",self.request.get("compute","auto")),"自動")
            self.conditions.set(f"Whisper：{language} ／ Granite：言語自動判別 ／ 処理方式：{compute} ／ 自動修正・話題ヒントなし")
            if data:
                self.summary.set(f"動画・音声 {seconds_text(data.get('audio_seconds'))}　 共通準備 {seconds_text(data.get('shared_seconds'))}　 全体 {seconds_text(data.get('total_seconds'))}")
            else:self.summary.set("同じ動画・音声を、一度の操作で両モデルにかけられます。")
            left = self.results.get("whisper",{}).get("segments",[]);right = self.results.get("granite",{}).get("segments",[])
            sources = (left,right) if all(side in self.results for side in SIDES) else None
            if sources is None:
                self._diff_sources = None;self._differences = {}
                self.prepare_merge(None)
                self.diff_summary.set("両方の文字起こしが完了すると色分けします。")
            elif self._diff_sources is None or any(a is not b for a,b in zip(sources,self._diff_sources)):
                self._diff_sources = sources
                self.prepare_merge(sources)
                self._differences = {id(row):ranges for rows,changes in zip(sources,difference_ranges(*sources)) for row,ranges in zip(rows,changes)}
                self.diff_summary.set("色は正誤を示しません。空白・改行は比較対象外。" if any(self._differences.values()) else "文章は一致しています（空白・改行を除く）。")
            self.rows = aligned_blocks(left,right)
            for side,text in self.texts.items():
                entry = data.get("results",{}).get(side,{"state":"waiting"})
                self.titles[side].set("Whisper "+entry.get("model",WHISPER_CHOICES[self.model.get()]) if side=="whisper" else "Granite 5.0 TurboCTC")
                text.configure(state="normal");text.delete("1.0","end")
                for tag in text.tag_names():
                    if tag.startswith("difference_"):text.tag_delete(tag)
                for mark in text.mark_names():
                    if mark.startswith("block_"):text.mark_unset(mark)
                pending = self.pane_message(side)
                for i,block in enumerate(self.rows):
                    text.mark_set(f"block_{i}","end-1c");text.mark_gravity(f"block_{i}","left")
                    text.insert("end",f"{timestamp(block['start'])} ～ {timestamp(block['end'])}\n","time")
                    for number,row in enumerate(block[side]):
                        if number:text.insert("end","\n",())
                        name = speaker_name(row.get("speaker"),self.results.get(side,{}).get("speakers"))
                        if name:text.insert("end",name+"：",())
                        self.insert_comparison_text(text,row)
                    if not block[side]:text.insert("end","（この区間の文字起こしなし）" if side in self.results else pending,())
                    text.insert("end","\n\n",())
                if not self.rows:text.insert("end",pending)
                text.mark_set("block_end","end-1c");text.mark_gravity("block_end","right")
            self.update_idletasks()
            if min(text.winfo_width() for text in self.texts.values())>20:
                for i in range(len(self.rows)):
                    end = f"block_{i+1}" if i+1<len(self.rows) else "block_end"
                    counts = {side:(text.count(f"block_{i}",end,"displaylines") or (0,))[0] for side,text in self.texts.items()}
                    maximum = max(counts.values())
                    for side,text in self.texts.items():
                        if maximum>counts[side]:
                            # Keep the following block's marker after the inserted padding.
                            text.mark_gravity(end,"right");text.insert(end,"\n"*(maximum-counts[side]),());text.mark_gravity(end,"left")
            for text in self.texts.values():text.configure(state="disabled");text.yview_moveto(fraction)
            self.mark_active_difference(scroll=self._merge_needs_scroll);self._merge_needs_scroll=False
            self.update_times()
        finally:self._rendering = False

    def pane_message(self,side):
        entry = (self.data or {}).get("results",{}).get(side,{})
        if entry.get("load_error"):return entry["load_error"]
        state = entry.get("state","waiting")
        if state == "failed":return "処理できませんでした。\n"+str(entry.get("error",""))
        if state == "complete":return "文字起こしされた文章はありません。"
        if state == "interrupted":return "このモデルの処理は完了していません。"
        if state == "running":return "文字起こし中…" if self.busy() else "途中で終了した処理です。もう一度比較できます。"
        return "比較の開始を待っています。"

    def update_times(self):
        for side,variable in self.times.items():
            entry = (self.data or {}).get("results",{}).get(side,{})
            state = entry.get("state","waiting")
            if state == "complete":value = f"文字起こし {seconds_text(entry.get('recognition_seconds'))} ／ {entry.get('device','')}"
            elif state == "running" and self.busy():value = "文字起こし中　"+seconds_text(time.monotonic()-self._running_at.get(side,time.monotonic()))
            elif state == "failed":value = "失敗"+(f"（{seconds_text(entry['attempt_seconds'])}）" if entry.get("attempt_seconds") is not None else "")
            elif state in {"running","interrupted"}:value = "未完了"
            else:value = "待機中" if self.busy() else "未実行"
            variable.set(value)

    def tick(self):
        self._timer = None
        self.poll_comparison_audio()
        if self.busy() and self.app.operation=="compare":self.status.set(self.app.status.get())
        self.update_times();self.update_controls()
        self._timer = self.app.root.after(200,self.tick)

    def resized(self,event):
        if self._rendering or event.width==self._width:return
        self._width = event.width
        if self._resize:self.app.root.after_cancel(self._resize)
        self._resize = self.app.root.after(150,self.resize_render)

    def resize_render(self):self._resize=None;self.render()
    def scroll(self,*args):
        for text in self.texts.values():text.yview(*args)
    def scroll_key(self,*args):self.scroll(*args);return "break"
    def wheel(self,event):return self.scroll_key("scroll",(-1 if event.delta>0 else 1)*max(1,abs(event.delta)//120)*3,"units")

    def use_result(self,side):
        if self.busy() or side not in self.results:return
        self.stop_comparison_audio()
        try:
            self.app.display_result(result_path(self.path,side))
            self.app.stage = f"{self.titles[side].get()}の結果を開きました。編集してTXT・SRT・字幕付きMP4を保存できます。"
            self.withdraw()
        except Exception as exc:messagebox.showerror("結果を開けません",str(exc),parent=self)

    def on_destroy(self,event):
        if event.widget is not self:return
        self.close_comparison_audio()
        for task in (self._resize,self._timer):
            if task:self.app.root.after_cancel(task)
        if self.app.comparison_window is self:self.app.comparison_window=None

    def withdraw(self):
        self.app.window_settings.remember("comparison",self)
        self.stop_comparison_audio()
        self.audio_waveform.pause()
        return super().withdraw()

    def destroy(self):
        self.app.window_settings.remember("comparison",self)
        super().destroy()


class ComparisonControls:
    def init_comparison(self):
        self.comparison_window = None;self.comparison_path = None;self.compare_button = None

    def open_comparison(self):
        if self.proc and self.operation!="compare":return
        if self.comparison_window and self.comparison_window.winfo_exists():
            current = self.source.get().strip().strip('"')
            if self.mode.get()=="youtube":
                try:current=youtube_url(current)
                except ValueError:pass
            if self.proc or current==self.comparison_window.request.get("source"):
                self.window_settings.show("comparison",self.comparison_window);return
            self.comparison_window.destroy()
        try:
            from app import MODEL_OPTIONS,LANG_OPTIONS
            from asr import COMPUTE_OPTIONS
            source = self.source.get().strip().strip('"')
            if self.mode.get()=="youtube":source=youtube_url(source)
            elif not Path(source).is_file():raise ValueError("比較する動画・音声を選んでください。")
            model = MODEL_OPTIONS[self.model.get()]
            req = {"source":source,"mode":self.mode.get(),"whisper_model":model if model in WHISPER_CHOICES.values() else "small",
                   "language":LANG_OPTIONS[self.language.get()],"compute":COMPUTE_OPTIONS[self.compute.get()],"num_speakers":self.speaker_count()}
            self.comparison_window = ComparisonDialog(self,req)
        except Exception as exc:messagebox.showerror("比較する動画・音声",str(exc))

    def open_saved_comparison(self,path):
        if self.proc:return
        data = read_comparison(path)
        req = {"source":data["source"],"mode":data["source_mode"],"whisper_model":data["results"]["whisper"]["model"],
               "language":data.get("whisper_language","ja"),"compute":data.get("compute","auto"),"num_speakers":data.get("num_speakers",0)}
        if self.comparison_window:self.comparison_window.destroy()
        self.comparison_window = ComparisonDialog(self,req);self.comparison_path=Path(path)
        self.comparison_window.show_saved(path)
        self.comparison_window.status.set(self.comparison_window.merge_load_note or "保存済みの比較結果です。違いごとに採用する文章を選び、1つの結果にまとめられます。")

    def update_comparison_controls(self):
        if self.compare_button:self.compare_button.configure(state="normal" if not self.proc or self.operation=="compare" else "disabled")
        if self.comparison_window:self.comparison_window.update_controls()

    def handle_comparison_event(self,event):
        if event["kind"] not in {"comparison_update","comparison_complete"}:return False
        self.comparison_path = Path(event["comparison_path"])
        try:
            if self.comparison_window:self.comparison_window.show_saved(self.comparison_path)
        except (OSError,ValueError,KeyError,TypeError) as exc:
            self.stage = "比較結果を開けません："+str(exc)
            return True
        if event["kind"]=="comparison_complete":
            self.completed = True
            data = read_comparison(self.comparison_path)
            count = sum(entry["state"]=="complete" for entry in data["results"].values())
            outcome = "比較が完了しました。" if count==2 else "比較終了：1モデルが失敗しました。" if count else "両モデルの比較を完了できませんでした。"
            self.stage = outcome+f" 全体 {seconds_text(data.get('total_seconds'))}。比較画面で結果を確認できます。"
        return True

    def finish_comparison(self,event):
        if self.comparison_path:
            try:
                if not self.completed:
                    interrupt_comparison(self.comparison_path,time.monotonic()-self.started,None if self.cancelled else "処理が終了しました。ログを確認して再実行できます。")
                    self.stage = "比較を停止しました。完了したモデルの結果は保存されています。" if self.cancelled else "比較を中断しました。保存できた結果は比較画面で確認できます。"
                if self.comparison_window:
                    self.comparison_window.show_saved(self.comparison_path)
                    self.comparison_window.status.set(self.stage)
            except (OSError,ValueError,KeyError) as exc:self.stage = "比較結果の確認が必要です："+str(exc)
        self.update_comparison_controls()
        if self.live_window and self.live_window.winfo_exists():self.schedule_live_preload()
