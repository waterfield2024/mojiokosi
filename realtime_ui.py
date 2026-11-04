"""Desktop controls for live loopback captions."""
from __future__ import annotations
import json
from pathlib import Path
import subprocess
import sys
import threading
import uuid
import tkinter as tk
from tkinter import ttk, messagebox, font as tkfont
from engine import ROOT, NO_WINDOW
from asr import COMPUTE_OPTIONS
from subtitle_overlay import SubtitleOverlay
from recognition_models import LIVE_MODELS,model_hint
from speech_preferences import LABELS as SPEECH_LABELS, HINTS as SPEECH_HINTS
from live_prompt import WHISPER_MODELS
from utterance_audio import MODES as SEGMENTATION_MODES

class RealtimeControls:
    def init_realtime(self):
        self.live_window = None
        self.live_shortcut = None
        from global_hotkey import LABEL
        self.live_hotkey_hint = tk.StringVar(value=f"{LABEL}：停止・再開")
        self.live_hotkey_error = ""
        self.overlay = None
        self.live_devices = []
        self.live_screens = []
        self.live_targets_mode = None
        from recording_settings import load_settings, settings_for, processing_preferences
        self.live_record_settings_error = ""
        try:
            settings = load_settings()
        except (OSError, ValueError) as exc:
            settings = settings_for("audio", {})
            self.live_record_settings_error = f"前回の録画設定を読み込めませんでした：{exc}"
        self.live_saved_record_settings = settings
        self.live_overlay_settings = settings["overlay"]
        self.live_saved_audio_device = settings["audio_device"]
        self.live_device_notice = ""
        self.live_devices_loading = False
        self.live_model_warning = ""
        self.live_target_selections = dict(settings["targets"])
        self.live_capture_mode = tk.StringVar(value=settings["mode"])
        self.live_target_error = ""
        self.live_screen_name = tk.StringVar()
        self.live_record_status = tk.StringVar(value="録音と文字起こしを保存します。画面の映像は録画しません。")
        self.live_active_mode = "audio"
        self.live_screen_failed = False
        self.live_capture_mode.trace_add("write", self.change_live_recording_mode)
        self.live_stop_requested = False
        self.live_recording = False
        self.live_phase = ""
        self.live_device_name = tk.StringVar()
        model_label = next((label for label, value in LIVE_MODELS.items() if value == settings["model"]), None)
        if model_label is None:
            model_label = "速度優先・base"
            self.live_model_warning = "前回の認識モデルが使えないため「速度優先・base」に戻しました。"
        self.live_model = tk.StringVar(value=model_label)
        processing = processing_preferences(settings["processing"], context_default=self.context_auto_apply.get())
        self.live_chunk = tk.StringVar(value=str(processing["chunk_seconds"]))
        self.live_segmentation = tk.StringVar(value=next(k for k,v in SEGMENTATION_MODES.items() if v == processing['segmentation']))
        self.live_interval_label = tk.StringVar()
        self.live_boundary_hint = tk.StringVar()
        self.live_final_utterance = -1
        self.update_live_boundary_hint()
        self.live_diarize = tk.BooleanVar(value=processing["live_diarize"])
        self.live_context = tk.BooleanVar(value=processing["context_correct"])
        self.live_correct = tk.BooleanVar(value=processing["live_correct"])
        self.live_noise = tk.BooleanVar(value=processing['live_noise'])
        self.live_previous_text = tk.BooleanVar(value=processing['live_previous_text'])
        self.live_sensitivity = tk.StringVar(value=next(label for label, key in SPEECH_LABELS.items()
                                                       if key == processing['speech_sensitivity']))
        self.live_speech_message = tk.StringVar(value=SPEECH_HINTS[processing['speech_sensitivity']])
        self.live_noise_message = tk.StringVar(value='ノイズ除去：準備前' if self.live_noise.get() else 'ノイズ除去：OFF（元の音声で認識）')
        self.live_context_message = tk.StringVar(value="字幕は先に表示し、校正できた部分を後から差し替えます。" if self.live_correct.get() else "字幕の自動修正：OFF")
        self.live_show = tk.BooleanVar(value=settings["display"]["show"])
        self.live_lock = tk.BooleanVar(value=settings["display"]["locked"])
        self.live_overlay_preview = False
        self.live_overlay_hint = tk.StringVar(value="白文字・黒縁／表示・固定も自動保存。")
        self.live_font = tk.IntVar(value=self.live_overlay_settings["font_size"])
        self.live_width = tk.IntVar(value=self.live_overlay_settings["extent"] or min(1100, max(480, self.root.winfo_screenwidth() - 120)))
        from vertical_subtitles import DIRECTIONS, POSITIONS, load_settings
        try:
            settings = load_settings()
        except (OSError, ValueError) as exc:
            settings = {"direction": "horizontal", "vertical_position": "right"}
            self.stage = f"字幕は標準設定で開始します：{exc}"
        direction = settings["direction"]
        self.vertical_position = settings["vertical_position"]
        self.caption_direction = tk.StringVar(value=next(k for k, v in DIRECTIONS.items() if v == direction))
        self.caption_position = tk.StringVar(value=next(k for k, v in POSITIONS.items() if v == self.vertical_position) if direction == "vertical" else "下中央")
        self.caption_direction.trace_add("write", self.change_caption_direction)
        self.caption_position.trace_add("write", self.change_caption_position)
        self.live_message = tk.StringVar(value="取り込む音声出力を選んでください。")
        self.live_level = tk.DoubleVar(value=0)
        self.live_preloader = None
        self.live_preload_phase = "idle"
        self.live_preload_key = None
        self.live_preload_token = None
        self.live_preload_text = ""
        self.live_preload_timer = None
        self.root.bind("<Destroy>", self.close_live_preloader, add="+")

    def ensure_live_preloader(self):
        from live_process import PersistentLiveProcess
        if self.live_preloader is None or not self.live_preloader.alive():
            if self.live_preloader:
                self.live_preloader.close(wait=False)
            self.live_preloader = PersistentLiveProcess(self.events)
        return self.live_preloader

    def live_model_settings(self):
        return {"model": LIVE_MODELS[self.live_model.get()],
                "compute": COMPUTE_OPTIONS[self.compute.get()], "live_diarize": self.live_diarize.get(),
                "live_correct": self.live_correct.get(), "live_noise": self.live_noise.get(),
                'speech_sensitivity': SPEECH_LABELS[self.live_sensitivity.get()]}

    def schedule_live_preload(self, *args):
        if self.proc or not self.live_window or not self.live_window.winfo_exists():
            return
        if self.live_preload_timer:
            self.root.after_cancel(self.live_preload_timer)
        self.live_preload_phase = "loading"
        self.live_preload_text = "選択したモデルを準備しています…"
        self.live_preload_token = None
        self.show_live_preload_status()
        self.update_live_controls()
        self.live_preload_timer = self.root.after(200, self.preload_live_models)

    def preload_live_models(self):
        if self.live_preload_timer:
            self.root.after_cancel(self.live_preload_timer)
        self.live_preload_timer = None
        if self.proc:
            return
        settings = self.live_model_settings()
        if (self.live_preloader and self.live_preloader.alive() and self.live_preload_key == settings
                and self.live_preload_phase == "ready"):
            self.show_live_preload_status()
            self.update_live_controls()
            return
        self.live_preload_phase = "loading"
        self.live_preload_text = "文字起こし・話者判別のモデルを先読みしています…"
        try:
            self.live_preload_key = settings
            self.live_preload_token = self.ensure_live_preloader().prepare(settings)
        except Exception as exc:
            self.live_preload_phase = "error"
            self.live_preload_text = str(exc)
        self.show_live_preload_status()
        self.update_live_controls()

    def show_live_preload_status(self):
        if self.proc:
            return
        if self.live_preload_phase == "ready":
            device = self.live_preloader.info.get("device", "") if self.live_preloader else ""
            message = f"モデル準備完了 / {device}。停止後も保持します。開始後「取り込み中」になったら動画を再生してください。"
            if self.live_device_notice:
                message = self.live_device_notice + "\nモデル準備完了。音声出力を選ぶと開始できます。"
            if self.live_preloader and self.live_preloader.info.get("speaker_warning"):
                message += " 話者判別の準備は開始時に再試行します。"
        elif self.live_preload_phase == "error":
            message = "モデルを準備できません。「準備を再試行」を押してください。 " + self.live_preload_text[:120]
        else:
            message = self.live_preload_text
        if self.live_device_notice and self.live_preload_phase != "ready":
            message = self.live_device_notice + "\n" + message
        if self.live_model_warning:
            message = self.live_model_warning + "\n" + message
        if self.live_hotkey_error:
            message = self.live_hotkey_error + "\n" + message
        self.live_message.set(message)

    def close_live_preloader(self, event):
        if event.widget is self.root:
            if self.live_preload_timer:
                self.root.after_cancel(self.live_preload_timer)
                self.live_preload_timer = None
            if self.live_preloader:
                self.live_preloader.close(wait=False)

    def build_live_button(self, parent):
        self.live_button = ttk.Button(parent, text="PC音声をリアルタイム…", command=self.open_live)
        self.live_button.pack(side="right")
        self.live_model.trace_add("write", self.change_live_model)
        self.live_noise.trace_add('write', self.change_live_noise)
        self.live_sensitivity.trace_add('write', self.change_live_sensitivity)
        self.compute.trace_add("write", self.schedule_live_preload)
        for variable in (self.live_diarize, self.live_correct):
            variable.trace_add("write", lambda *args: self.change_live_processing(preload=True))
        for variable in (self.live_context, self.live_chunk, self.live_previous_text):
            variable.trace_add("write", lambda *args: self.change_live_processing(preload=False))
        self.live_segmentation.trace_add('write', self.change_live_segmentation)

    def update_live_controls(self):
        self.live_button.configure(state="normal" if not self.proc or self.operation == "realtime" else "disabled")
        if self.live_window and self.live_window.winfo_exists():
            busy = bool(self.proc)
            ready = self.live_preload_phase == "ready"
            retry = self.live_preload_phase == "error"
            self.live_start_button.configure(
                state="normal" if not busy and (retry or (ready and not self.live_devices_loading and self.selected_live_audio_device())) else "disabled",
                text="準備を再試行" if retry and not busy else "モデル準備中…" if not ready and not busy else "リアルタイム開始")
            if hasattr(self, "live_names_button"):
                self.live_names_button.configure(state="normal" if self.segments and (not busy or self.live_speaker_editable()) else "disabled")
            self.live_stop_button.configure(state="normal" if busy and self.operation == "realtime" else "disabled",
                                            text="処理を中止" if self.live_stop_requested else "停止して保存")
            self.live_devices_combo.configure(state="disabled" if busy or self.live_devices_loading else "readonly")
            self.live_refresh.configure(state="disabled" if busy or self.live_devices_loading else "normal")
            self.live_model_combo.configure(state="disabled" if busy else "readonly")
            self.live_chunk_combo.configure(state="disabled" if busy else "readonly")
            self.live_segmentation_combo.configure(state='disabled' if busy else 'readonly')
            self.live_previous_check.configure(state='normal' if not busy and LIVE_MODELS[self.live_model.get()] in WHISPER_MODELS else 'disabled')
            self.update_screen_controls()
            for control in self.live_post_controls:
                control.configure(state="disabled" if busy else "normal")

    def open_live(self):
        if self.proc and self.operation != "realtime":
            return
        if self.live_window and self.live_window.winfo_exists():
            self.window_settings.show("realtime", self.live_window)
            if not self.proc:
                self.refresh_live_screens()
                self.refresh_live_devices()
                self.preload_live_models()
            return
        win = self.live_window = tk.Toplevel(self.root)
        win.withdraw()
        win.title("PC音声のリアルタイム文字起こし")
        win.geometry("840x820")
        win.minsize(760, 780)
        win.transient(self.root)
        shell = ttk.Frame(win, padding=18)
        shell.pack(fill='both', expand=True)
        actions = ttk.Frame(shell)
        actions.pack(side='bottom', fill='x', pady=(8, 0))
        body = ttk.Frame(shell)
        body.pack(fill='both', expand=True)
        from ui_theme import palette
        canvas = self.live_settings_canvas = tk.Canvas(body, highlightthickness=0, background=palette(win)['surface'])
        scroll = ttk.Scrollbar(body, orient='vertical', command=canvas.yview)
        scroll.pack(side='right', fill='y')
        canvas.configure(yscrollcommand=scroll.set)
        canvas.pack(side='left', fill='both', expand=True)
        canvas._whisper_refresh_tags = lambda: canvas.configure(background=palette(win)['surface'])
        frame = ttk.Frame(canvas)
        content = canvas.create_window(0, 0, window=frame, anchor='nw')
        frame.bind('<Configure>', lambda e: canvas.configure(scrollregion=canvas.bbox('all')))
        canvas.bind('<Configure>', lambda e: canvas.itemconfigure(content, width=e.width))
        def scroll_settings(event):
            if event.widget.winfo_class() not in {'TCombobox', 'Text'} and canvas.yview() != (0., 1.):
                canvas.yview_scroll(-int(event.delta/120), 'units')
        win.bind('<MouseWheel>', scroll_settings, add='+')
        ttk.Label(frame, text="動画を再生しながら、字幕を重ねて表示", font=("Yu Gothic UI", 16, "bold")).pack(anchor="w")
        ttk.Label(frame, text="選んだスピーカー／ヘッドホンの音声を取り込みます。マイクは使いません。").pack(anchor="w", pady=(4, 12))
        ttk.Label(frame, text="動画の音声を再生している出力先").pack(anchor="w")
        row = ttk.Frame(frame); row.pack(fill="x", pady=(4, 12))
        self.live_devices_combo = ttk.Combobox(row, textvariable=self.live_device_name, state="readonly")
        self.live_devices_combo.pack(side="left", fill="x", expand=True)
        self.live_devices_combo.bind("<<ComboboxSelected>>", self.choose_live_audio_device)
        self.live_refresh = ttk.Button(row, text="一覧を更新", command=self.refresh_live_devices)
        self.live_refresh.pack(side="left", padx=(8, 0))
        recording = ttk.Frame(frame); recording.pack(fill="x", pady=(0, 4))
        ttk.Label(recording, text="保存する内容").pack(side="left", padx=(0, 8))
        self.live_record_buttons = []
        for label, value in (("録音のみ", "audio"), ("画面全体を録画", "screen"), ("ウィンドウを録画", "window")):
            button = ttk.Radiobutton(recording, text=label, variable=self.live_capture_mode, value=value)
            button.pack(side="left", padx=(0, 10)); self.live_record_buttons.append(button)
        targets = ttk.Frame(frame); targets.pack(fill="x", pady=(0, 4))
        ttk.Label(targets, text="録画する対象").pack(side="left", padx=(0, 8))
        self.live_screen_combo = ttk.Combobox(targets, textvariable=self.live_screen_name, width=27, state="disabled")
        self.live_screen_combo.pack(side="left", fill="x", expand=True)
        self.live_screen_combo.bind("<<ComboboxSelected>>", self.choose_live_recording_target)
        self.live_screen_refresh = ttk.Button(targets, text="一覧を更新", command=self.refresh_live_screens)
        self.live_screen_refresh.pack(side="left", padx=(8, 0))
        record_hint = ttk.Label(frame, textvariable=self.live_record_status, style="Sub.TLabel", wraplength=720)
        record_hint.pack(anchor="w", fill="x", pady=(0, 6))
        record_hint.bind("<Configure>", lambda e: record_hint.configure(wraplength=max(300, e.width)))
        row = ttk.Frame(frame); row.pack(fill="x", pady=(0, 8))
        ttk.Label(row, text="認識モデル").pack(side="left")
        self.live_model_combo = ttk.Combobox(row, textvariable=self.live_model,
                                             values=list(LIVE_MODELS), state="readonly", width=24)
        self.live_model_combo.pack(side="left", padx=(8, 18))
        ttk.Label(row, textvariable=self.live_interval_label).pack(side="left")
        from recording_settings import CHUNK_SECONDS
        self.live_chunk_combo = ttk.Combobox(row, textvariable=self.live_chunk, values=[str(n) for n in CHUNK_SECONDS],
                                             state="readonly", width=4)
        self.live_chunk_combo.pack(side="left", padx=6)
        ttk.Label(row, text="秒").pack(side="left")
        self.live_model_message = tk.StringVar(value=model_hint(LIVE_MODELS[self.live_model.get()]))
        ttk.Label(frame,textvariable=self.live_model_message,style="Sub.TLabel",wraplength=650).pack(anchor="w",pady=(0,8))
        self.live_model_combo.bind("<<ComboboxSelected>>",lambda event:self.live_model_message.set(model_hint(LIVE_MODELS[self.live_model.get()])),add="+")
        row = ttk.Frame(frame); row.pack(fill='x', pady=(0,3))
        ttk.Label(row,text='音声の区切り方').pack(side='left',padx=(0,8))
        self.live_segmentation_combo = ttk.Combobox(row,textvariable=self.live_segmentation,
            values=list(SEGMENTATION_MODES),state='readonly',width=25)
        self.live_segmentation_combo.pack(side='left')
        ttk.Label(frame,textvariable=self.live_boundary_hint,style='Sub.TLabel',wraplength=680).pack(anchor='w',pady=(0,8))
        self.live_previous_check = ttk.Checkbutton(frame, text='前の文章を参考に認識する（Whisper）', variable=self.live_previous_text)
        self.live_previous_check.pack(anchor='w')
        ttk.Label(frame, text='直近2つの発言を参考にします。6秒以上の無音でリセット／開始前に変更可',
                  style='Sub.TLabel', wraplength=650).pack(anchor='w', pady=(0, 8))
        noise_row = ttk.Frame(frame)
        noise_row.pack(fill='x', pady=(0, 3))
        self.live_noise_check = ttk.Checkbutton(noise_row, text='Nemotronでノイズ除去（認識用のみ）', variable=self.live_noise)
        self.live_noise_check.pack(side='left')
        ttk.Label(noise_row, text='元の録音を保持／録音中も切り替え可', style='Sub.TLabel').pack(side='left', padx=8)
        noise_hint = ttk.Label(frame, textvariable=self.live_noise_message, style='Sub.TLabel', wraplength=680)
        noise_hint.pack(anchor='w', fill='x', pady=(0, 6))
        noise_hint.bind('<Configure>', lambda e: noise_hint.configure(wraplength=max(200, e.width)))
        speech_row = ttk.Frame(frame)
        speech_row.pack(fill='x', pady=(2, 3))
        ttk.Label(speech_row, text='無音判定の感度').pack(side='left', padx=(0, 8))
        self.live_sensitivity_combo = ttk.Combobox(speech_row, textvariable=self.live_sensitivity,
                                                  values=list(SPEECH_LABELS), state='readonly', width=28)
        self.live_sensitivity_combo.pack(side='left')
        ttk.Label(speech_row, text='録音中も変更可', style='Sub.TLabel').pack(side='left', padx=8)
        speech_hint = ttk.Label(frame, textvariable=self.live_speech_message, style='Sub.TLabel', wraplength=680)
        speech_hint.pack(fill='x', pady=(0, 8))
        speech_hint.bind('<Configure>', lambda e: speech_hint.configure(wraplength=max(200, e.width)))
        subtitle = ttk.LabelFrame(frame, text="動画に重ねる字幕", padding=10)
        subtitle.pack(fill="x")
        row = ttk.Frame(subtitle); row.pack(fill="x")
        self.live_show_check = ttk.Checkbutton(row, text="字幕を表示", variable=self.live_show, command=self.toggle_overlay)
        self.live_show_check.pack(side="left")
        self.live_lock_check = ttk.Checkbutton(row, text="位置を固定（動画をクリック可能）", variable=self.live_lock,
                                              command=self.lock_overlay)
        self.live_lock_check.pack(side="left", padx=12)
        history = ttk.Frame(subtitle)
        history.pack(fill="x", pady=(6, 0))
        ttk.Checkbutton(history, text="画面端に文字起こし履歴を表示", variable=self.history_visible,
                        command=self.toggle_history).pack(side="left")
        ttk.Button(history, text="履歴・検索を開く", command=self.open_history).pack(side="left", padx=12)
        row = ttk.Frame(subtitle); row.pack(fill="x", pady=8)
        ttk.Button(row, text="位置を調整・プレビュー", command=self.preview_overlay).pack(side="left")
        ttk.Label(row, text="文字サイズ").pack(side="left", padx=(18, 5))
        size = ttk.Combobox(row, textvariable=self.live_font, values=[22, 26, 32, 40, 48], width=4, state="readonly")
        size.pack(side="left"); size.bind("<<ComboboxSelected>>", lambda e: self.resize_overlay())
        ttk.Label(row, text="長さ").pack(side="left", padx=(12, 5))
        width = ttk.Combobox(row, textvariable=self.live_width, values=[480, 720, 900, 1100, 1400, 1800], width=6, state="readonly")
        width.pack(side="left"); width.bind("<<ComboboxSelected>>", lambda e: self.resize_overlay())
        row = ttk.Frame(subtitle); row.pack(fill="x")
        ttk.Label(row, text="字幕の向き").pack(side="left", padx=(0, 6))
        self.live_direction_combo = ttk.Combobox(row, textvariable=self.caption_direction, values=["横書き", "縦書き"], width=7, state="readonly")
        self.live_direction_combo.pack(side="left")
        ttk.Label(row, text="位置").pack(side="left", padx=(10, 6))
        self.live_position_combo = ttk.Combobox(row, textvariable=self.caption_position, width=6, state="readonly")
        self.live_position_combo.pack(side="left")
        self.sync_caption_position_controls()
        ttk.Label(row, textvariable=self.live_overlay_hint).pack(side="left", padx=10)
        self.live_post_controls = []
        for label, variable in [("リアルタイムで話者を自動判別", self.live_diarize),
                                ("字幕の誤変換を文脈で自動修正（字幕を先に表示）", self.live_correct),
                                ("停止後に文脈AI校正（修正前後を確認できます）", self.live_context)]:
            control = ttk.Checkbutton(frame, text=label, variable=variable)
            control.pack(anchor="w", pady=(8, 0)); self.live_post_controls.append(control)
        ttk.Label(frame, textvariable=self.live_context_message, style="Sub.TLabel").pack(anchor="w", pady=(5, 0))
        ttk.Label(frame, text="「話者の名前を設定」で名前を付けられます。判別が難しい発言は「？」を表示します。",
                  style="Sub.TLabel").pack(anchor="w", pady=(8, 8))
        speaker_row = ttk.Frame(frame); speaker_row.pack(fill="x", pady=(0, 8))
        self.live_names_button = ttk.Button(speaker_row, text="話者の名前を設定", command=self.manage_speakers)
        self.live_names_button.pack(side="left")
        ttk.Label(speaker_row, text="誤判定はメイン画面の行を選び「この行の話者」で修正できます。").pack(side="left", padx=8)
        meter = ttk.Frame(frame); meter.pack(fill="x")
        ttk.Label(meter, text="音量").pack(side="left")
        ttk.Progressbar(meter, variable=self.live_level, maximum=100).pack(side="left", fill="x", expand=True, padx=8)
        status_font = tkfont.Font(font=ttk.Style(self.root).lookup("TLabel", "font"))
        status_area = ttk.Frame(frame, height=status_font.metrics("linespace") * 3)
        status_area.pack(fill="x", pady=(8, 8))
        status_area.pack_propagate(False)
        status_label = ttk.Label(status_area, textvariable=self.live_message, wraplength=640)
        status_label.pack(fill="x", anchor="w")
        status_area.bind("<Configure>", lambda e: status_label.configure(wraplength=max(300, e.width)))
        row = actions
        self.live_start_button = ttk.Button(row, text="リアルタイム開始", style="Accent.TButton", command=self.start_live)
        self.live_start_button.pack(side="left")
        self.live_stop_button = ttk.Button(row, text="停止して保存", command=self.cancel)
        self.live_stop_button.pack(side="left", padx=10)
        ttk.Label(row, textvariable=self.live_hotkey_hint, style="Sub.TLabel").pack(side="left", padx=5)
        close_window = lambda: self.window_settings.hide("realtime")
        ttk.Button(row, text="設定画面を閉じる", command=close_window).pack(side="right")
        win.protocol("WM_DELETE_WINDOW", close_window)
        self.window_settings.attach(win, "realtime")
        self.update_live_controls()
        self.refresh_live_screens()
        self.refresh_live_devices()
        self.preload_live_models()

    def update_screen_controls(self, *args):
        if not hasattr(self, "live_screen_combo") or not self.live_screen_combo.winfo_exists():
            return
        busy = bool(self.proc)
        mode = self.live_capture_mode.get()
        screen = mode in {"screen", "window"}
        for button in self.live_record_buttons:
            button.configure(state="disabled" if busy else "normal")
        self.live_screen_combo.configure(state="readonly" if screen and not busy else "disabled")
        self.live_screen_refresh.configure(state="normal" if screen and not busy else "disabled")
        if not busy:
            if mode != self.live_targets_mode:self.refresh_live_screens()
            self.show_live_recording_hint()

    def remember_live_recording_target(self):
        if self.live_targets_mode not in {"screen", "window"} or not hasattr(self, "live_screen_combo"):
            return
        if not self.live_screen_combo.winfo_exists():return
        index = self.live_screen_combo.current()
        if 0 <= index < len(self.live_screens):
            self.live_target_selections[self.live_targets_mode] = dict(self.live_screens[index])

    def persist_live_recording_settings(self):
        if getattr(self, 'data_transfer_active', False):return
        from recording_settings import settings_for, save_settings
        try:
            data = settings_for(self.live_capture_mode.get(), self.live_target_selections,
                                LIVE_MODELS[self.live_model.get()], self.live_saved_audio_device, self.live_overlay_settings,
                                self.live_processing_settings(), {"show": self.live_show.get(), "locked": self.live_lock.get()},
                                {"visible": self.history_visible.get(), "limit": self.history_limit, "side": self.history_side})
            if data != self.live_saved_record_settings:
                save_settings(data)
                self.live_saved_record_settings = data
                self.live_record_settings_error = ""
        except (OSError, ValueError) as exc:
            self.live_record_settings_error = f"次回用の録画設定を保存できませんでした：{exc}"

    def change_live_recording_mode(self, *args):
        if self.proc:return
        self.remember_live_recording_target()
        self.update_screen_controls()
        self.persist_live_recording_settings()
        self.show_live_recording_hint()

    def choose_live_recording_target(self, event=None):
        if self.proc:return
        self.remember_live_recording_target()
        self.persist_live_recording_settings()
        self.show_live_recording_hint()

    def show_live_recording_hint(self):
        mode = self.live_capture_mode.get()
        if self.live_record_settings_error:
            text = self.live_record_settings_error
        elif self.live_target_error:
            text = self.live_target_error
        elif mode == "audio":
            text = "録音と文字起こしを保存します。映像は録画しません。この設定は次回も復元します。"
        elif not hasattr(self, "live_screen_combo") or not self.live_screen_combo.winfo_exists() or self.live_screen_combo.current() < 0:
            name = "ウィンドウ" if mode == "window" else "画面"
            if self.live_target_selections.get(mode):
                text = f"前回の{name}を特定できません。「一覧を更新」または録画する対象を選び直してください。"
            else:
                text = f"録画する{name}を選んでください。選んだ設定は次回も復元します。"
        elif mode == "window":
            text = "選んだ設定は次回も復元します。最小化中は映像を保持し、閉じると停止。音声は出力先全体です。"
        else:
            text = "選んだ画面と設定は次回も復元します（最大1080p・15fps）。字幕は停止後に付けて保存できます。"
        self.live_record_status.set(text)

    def refresh_live_screens(self):
        if self.proc:return
        self.remember_live_recording_target()
        mode = self.live_capture_mode.get()
        self.live_targets_mode = mode
        previous = self.live_target_selections.get(mode, {})
        self.live_target_error = ""
        try:
            from screen_capture import monitors
            from window_capture import windows, window_state
            from recording_settings import match_target
            self.live_screens = windows() if mode == "window" else monitors() if mode == "screen" else []
            labels = [m["label"] for m in self.live_screens]
            self.live_screen_combo.configure(values=labels)
            reopened = bool(mode == "window" and previous and window_state(previous) == "closed")
            matched = match_target(mode, previous, self.live_screens, allow_reopened=reopened)
            if matched is not None:self.live_screen_combo.current(matched)
            else:self.live_screen_name.set("")
            self.remember_live_recording_target()
        except Exception as exc:
            self.live_screens = []
            self.live_screen_combo.configure(values=[]);self.live_screen_name.set("")
            self.live_target_error = f"録画対象の一覧を取得できませんでした：{exc}"
        self.persist_live_recording_settings()
        self.show_live_recording_hint()

    def refresh_live_devices(self):
        if self.proc or self.live_devices_loading:
            return
        self.live_devices_loading = True
        self.live_device_notice = "音声出力を確認しています…"
        self.show_live_preload_status()
        self.update_live_controls()
        python = Path(sys.executable).with_name("python.exe")
        def read():
            try:
                result = subprocess.run([str(python), "-X", "utf8", str(ROOT / "realtime.py"), "--list-devices"],
                                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=20, creationflags=NO_WINDOW)
                parsed = json.loads(result.stdout.decode("utf-8"))
                self.events.put({"kind": "live_devices", **parsed})
            except Exception as exc:
                self.events.put({"kind": "live_devices", "error": str(exc)})
        threading.Thread(target=read, daemon=True).start()

    def selected_live_audio_device(self):
        combo = getattr(self, "live_devices_combo", None)
        if combo is None or not combo.winfo_exists():return None
        index = combo.current()
        return self.live_devices[index] if 0 <= index < len(self.live_devices) else None

    def choose_live_audio_device(self, event=None):
        if self.proc or self.live_devices_loading:return
        device = self.selected_live_audio_device()
        if device:
            from recording_settings import audio_preference
            self.live_saved_audio_device = audio_preference(device)
            self.live_device_notice = ""
            self.persist_live_recording_settings()
            self.show_live_recording_hint()
        self.show_live_preload_status()
        self.update_live_controls()

    def change_live_model(self, *args):
        if self.live_model.get() not in LIVE_MODELS:return
        self.live_model_warning = ""
        self.persist_live_recording_settings()
        self.show_live_recording_hint()
        if hasattr(self, "live_model_message"):
            self.live_model_message.set(model_hint(LIVE_MODELS[self.live_model.get()]))
        self.schedule_live_preload()

    def live_processing_settings(self):
        from recording_settings import processing_preferences
        return processing_preferences({"live_diarize": self.live_diarize.get(), "live_correct": self.live_correct.get(),
                                       "context_correct": self.live_context.get(), "chunk_seconds": int(self.live_chunk.get()),
                                       'live_noise': self.live_noise.get(),
                                       'live_previous_text': self.live_previous_text.get(),
                                       'segmentation': SEGMENTATION_MODES[self.live_segmentation.get()],
                                       'speech_sensitivity': SPEECH_LABELS[self.live_sensitivity.get()]})

    def update_live_boundary_hint(self):
        utterance = SEGMENTATION_MODES[self.live_segmentation.get()] == 'utterance'
        self.live_interval_label.set('字幕の更新間隔' if utterance else '音声を区切る間隔')
        self.live_boundary_hint.set('話の途中は暫定字幕を表示し、切れ目で確定します。長い発言は最大12秒で区切ります。'
                                    if utterance else '設定した秒数、または短い無音で区切って認識します。')

    def change_live_segmentation(self, *args):
        self.update_live_boundary_hint()
        self.change_live_processing(preload=False)

    def change_live_sensitivity(self, *args):
        value = SPEECH_LABELS.get(self.live_sensitivity.get())
        if value is None:
            return
        self.persist_live_recording_settings()
        self.live_speech_message.set(SPEECH_HINTS[value])
        if self.proc and self.operation == 'realtime':
            from context_ai import write_json
            try:
                write_json(self.job / 'speech-control.json', {'sensitivity': value})
                self.live_speech_message.set('次の音声区間から反映します。' + SPEECH_HINTS[value])
            except OSError as exc:
                self.live_speech_message.set('感度の変更を送信できませんでした：' + str(exc))
        elif not self.proc:
            self.schedule_live_preload()

    def change_live_noise(self, *args):
        self.persist_live_recording_settings()
        if self.proc and self.operation == 'realtime':
            from context_ai import write_json
            try:
                write_json(self.job / 'noise-control.json', {'enabled': self.live_noise.get(), 'token': uuid.uuid4().hex})
                self.live_noise_message.set('ノイズ除去を次の音声区間から' + ('オンにします。準備中は元の音声で認識します。' if self.live_noise.get() else 'オフにします。'))
            except OSError as exc:
                self.live_noise_message.set('ノイズ除去の切り替えを送信できませんでした：' + str(exc))
        elif not self.proc:
            self.live_noise_message.set('ノイズ除去を準備しています…' if self.live_noise.get() else 'ノイズ除去：OFF（元の音声で認識）')
            self.schedule_live_preload()

    def change_live_processing(self, *, preload):
        self.persist_live_recording_settings()
        self.show_live_recording_hint()
        if not self.proc:
            self.live_context_message.set("字幕は先に表示し、校正できた部分を後から差し替えます。" if self.live_correct.get() else "字幕の自動修正：OFF")
        if preload:self.schedule_live_preload()

    def ensure_overlay(self):
        if not self.overlay or self.overlay.closed:
            from vertical_subtitles import DIRECTIONS
            self.overlay = SubtitleOverlay(self.root, self.live_font.get(), extent=self.live_width.get(),
                direction=DIRECTIONS[self.caption_direction.get()], vertical_position=self.vertical_position,
                positions=self.live_overlay_settings["positions"], on_change=self.save_live_overlay,
                visible=self.live_overlay_preview or self.live_show.get(), locked=False if self.live_overlay_preview else self.live_lock.get())
        return self.overlay

    def save_live_overlay(self, settings):
        self.live_overlay_settings = settings
        self.live_font.set(settings["font_size"])
        self.live_width.set(settings["extent"])
        self.persist_live_recording_settings()
        self.show_live_recording_hint()
        if self.live_record_settings_error:self.stage = self.live_record_settings_error

    def change_caption_direction(self, *args):
        from vertical_subtitles import DIRECTIONS
        direction = DIRECTIONS.get(self.caption_direction.get())
        if not direction:
            return
        if self.overlay and not self.overlay.closed:
            self.overlay.set_direction(direction)
        self.sync_caption_position_controls()
        self.save_caption_settings()

    def sync_caption_position_controls(self):
        from vertical_subtitles import POSITIONS
        vertical = self.caption_direction.get() == "縦書き"
        self.caption_position.set(next(k for k, v in POSITIONS.items() if v == self.vertical_position) if vertical else "下中央")
        for name in ("caption_position_combo", "live_position_combo"):
            combo = getattr(self, name, None)
            if combo is not None and combo.winfo_exists():
                combo.configure(values=list(POSITIONS) if vertical else ["下中央"], state="readonly" if vertical else "disabled")

    def change_caption_position(self, *args):
        from vertical_subtitles import POSITIONS
        position = POSITIONS.get(self.caption_position.get())
        if self.caption_direction.get() != "縦書き" or not position or position == self.vertical_position:
            return
        self.vertical_position = position
        if self.overlay and not self.overlay.closed:
            self.overlay.set_vertical_position(position)
        self.save_caption_settings()

    def save_caption_settings(self):
        from vertical_subtitles import DIRECTIONS, save_settings
        try:
            save_settings(DIRECTIONS[self.caption_direction.get()], self.vertical_position)
            self.stage = f"字幕を{self.caption_direction.get()}・{self.caption_position.get()}にしました。リアルタイム表示と次回のMP4出力に使います。"
        except OSError as exc:
            self.stage = f"字幕は切り替えましたが、設定を保存できません：{exc}"

    def preview_overlay(self):
        self.live_overlay_preview = True
        self.live_overlay_hint.set("位置調整中：一時的に表示・固定解除。")
        overlay = self.ensure_overlay()
        overlay.show(True)
        overlay.set_locked(False)
        if not self.live_recording:
            overlay.preview()

    def toggle_overlay(self):
        self.apply_live_overlay_display()
        self.persist_live_recording_settings()
        self.show_live_recording_hint()

    def lock_overlay(self):
        self.toggle_overlay()

    def apply_live_overlay_display(self):
        self.live_overlay_preview = False
        self.live_overlay_hint.set("白文字・黒縁／表示・固定も自動保存。")
        if self.live_show.get():self.ensure_overlay()
        if self.overlay and not self.overlay.closed:
            self.overlay.set_locked(self.live_lock.get())
            self.overlay.show(self.live_show.get())

    def resize_overlay(self):
        overlay = self.ensure_overlay()
        overlay.set_layout(self.live_font.get(), self.live_width.get())

    def start_live(self):
        if self.proc or self.live_devices_loading:
            return
        if (self.live_preload_phase != "ready" or self.live_preload_key != self.live_model_settings()
                or not self.live_preloader or not self.live_preloader.alive()):
            self.preload_live_models()
            return
        choice = self.live_devices_combo.current()
        if choice < 0 or choice >= len(self.live_devices):
            messagebox.showerror("音声出力", "一覧を更新して音声の出力先を選んでください。", parent=self.live_window)
            return
        selected_screen = None
        if self.live_capture_mode.get() in {"screen", "window"}:
            try:
                screen_index = self.live_screen_combo.current()
                if screen_index < 0 or screen_index >= len(self.live_screens):
                    raise ValueError("一覧を更新して、録画する画面・ウィンドウを選んでください。")
                from screen_capture import validate_target
                selected_screen = validate_target(self.live_screens[screen_index])
                if selected_screen.get("kind") != "window" and not self.ensure_overlay().capture_excluded:
                    raise RuntimeError("字幕を録画から除外できませんでした。Windowsを更新してアプリを起動し直してください。録音のみは利用できます。")
                if self.history_visible.get() and not self.ensure_history().capture_excluded:
                    raise RuntimeError("履歴を録画から除外できませんでした。「画面端に文字起こし履歴を表示」をオフにしてから録画してください。")
            except Exception as exc:
                messagebox.showerror("画面録画を開始できません", str(exc), parent=self.live_window)
                return
        if selected_screen:
            self.live_target_selections[self.live_capture_mode.get()] = dict(selected_screen)
        from recording_settings import audio_preference
        self.live_saved_audio_device = audio_preference(self.live_devices[choice])
        self.persist_live_recording_settings()
        self.live_active_mode = "screen" if selected_screen else "audio"
        self.live_screen_failed = False
        review = getattr(self, "voice_review", None)
        if review and not review.closed:
            review.stop()
        self.persist_edits()
        self.live_stop_requested = False
        self.live_phase = "preparing"
        self.live_final_utterance = -1
        self.update_live_boundary_hint()
        self.live_recording = False
        self.live_level.set(0)
        self.live_context_message.set("字幕の自動修正を準備中。字幕は先に表示します。" if self.live_correct.get() else "字幕の自動修正：OFF")
        self.live_message.set("準備中です。「取り込み中」と表示されたら動画を再生してください。")
        overlay = self.ensure_overlay()
        overlay.clear()
        self.apply_live_overlay_display()
        from app import LANG_OPTIONS
        request = {"operation": "realtime", "device": self.live_devices[choice],
                   "recording_mode": self.live_active_mode, "screen_monitor": selected_screen,
                   "model": LIVE_MODELS[self.live_model.get()],
                   "language": LANG_OPTIONS[self.language.get()], "prompt": self.prompt.get("1.0", "end-1c").strip(),
                   "chunk_seconds": int(self.live_chunk.get()), "live_diarize": self.live_diarize.get(), "diarize": False,
                   "num_speakers": self.speaker_count(), "context_correct": self.live_context.get(),
                   "live_correct": self.live_correct.get(),
                   "live_noise": self.live_noise.get(),
                   'live_previous_text': self.live_previous_text.get(),
                   'segmentation': SEGMENTATION_MODES[self.live_segmentation.get()],
                   'speech_sensitivity': SPEECH_LABELS[self.live_sensitivity.get()],
                   "correction_rules": self.automatic_correction_rules(),
                   "speaker_memory_file": str(self.speaker_memory.path)}
        try:
            self.launch_worker(request)
            self.editor.configure(state="disabled")
            for button in self.export_buttons:
                button.configure(state="disabled")
        except Exception as exc:
            self.live_phase = ""
            messagebox.showerror("開始できません", str(exc), parent=self.live_window)
        else:
            if self.live_shortcut:self.live_shortcut.started()
            self.window_settings.hide("realtime")
            self.window_settings.remember("main", self.root)
            self.root.iconify()
        self.update_live_controls()

    def stop_live(self):
        if self.operation != "realtime" or not self.proc or self.live_stop_requested:
            return False
        self.live_stop_requested = True
        (self.job / "stop-live").touch()
        self.stage = "録画・録音を停止し、MP4と残りの認識結果を保存しています…" if self.live_active_mode == "screen" else "音声の取り込みを停止し、残りの認識結果を保存しています…"
        self.live_message.set(self.stage)
        self.cancel_button.configure(text="処理を中止", state="normal")
        self.update_live_controls()
        return True

    def handle_live_event(self, event):
        kind = event["kind"]
        if self.live_shortcut and self.live_shortcut.handle(event):return True
        if kind == 'live_speech_status':
            value = event.get('sensitivity')
            if value == SPEECH_LABELS.get(self.live_sensitivity.get()):
                self.live_speech_message.set('設定を反映しました。' + SPEECH_HINTS[value])
            return True
        if kind == 'live_noise_status':
            self.live_noise_message.set(event['text'])
            return True
        if kind == "live_recording_status":
            self.live_record_status.set(event["text"])
            if event["text"].startswith("画面録画が停止"):
                self.live_screen_failed = True
            return True
        if kind == "live_context_status":
            self.live_context_message.set(event["text"])
            return True
        if kind == "live_corrections":
            from correction_memory import filter_change
            self.commit()
            history = self.result_data.setdefault("context_review", [])
            known = {r["id"] for r in history}
            protected = False
            for record in event["records"]:
                if record["id"] in known:
                    continue
                history.append(record)
                known.add(record["id"])
                key = record["segment_id"]
                row = next((r for r in self.segments if r.get("segment_id") == key), None)
                if (row is not None and row["text"] == record["before"]
                        and key not in self.result_data.get("live_text_edits", {})):
                    try:
                        _, blocked = filter_change(record["before"], record["after"])
                    except (OSError, ValueError):
                        blocked = True
                        self.live_context_message.set("覚えた誤修正を読み込めないため、この自動修正を見送りました。")
                    if blocked:
                        # Undo may have happened after the worker emitted this event.
                        # Keep the original row and checkpoint that decision back to the worker.
                        record["status"] = "reverted"
                        self.result_data.setdefault("live_text_edits", {})[key] = {
                            "text": row["text"], "words": list(row.get("words", []))}
                        self.result_data.setdefault("live_context_review_states", {})[record["id"]] = "reverted"
                        protected = True
                        continue
                    row.setdefault("live_asr_text", row["text"])
                    row.update(text=record["after"], words=[])
                    if self.overlay:
                        self.overlay.update_text(key, row["text"])
            self.refresh_live_corrected_text()
            if protected:
                try:
                    self.persist_edits()
                except OSError as exc:
                    self.stage = f"誤修正を見送りましたが、結果を保存できませんでした：{exc}"
            if self.context_window and self.context_window.winfo_exists():
                self.context_window.refresh()
            return True
        if kind.startswith("live_preload_"):
            if (not self.live_preloader or event.get("service_id") != self.live_preloader.id
                    or event.get("token") != self.live_preload_token):
                return True
            if kind == "live_preload_ready":
                self.live_preload_phase = "ready"
                if event.get('noise'):
                    self.live_noise_message.set(event['noise']['text'])
            elif kind == "live_preload_error":
                self.live_preload_phase = "error"
                self.live_preload_text = event["message"]
            else:
                self.live_preload_text = event["text"]
            self.show_live_preload_status()
            self.update_live_controls()
            return True
        if kind == "live_devices":
            self.live_devices_loading = False
            if self.proc:return True
            self.live_devices = event.get("devices", [])
            if self.live_window and self.live_window.winfo_exists():
                from collections import Counter
                from recording_settings import match_audio_device, audio_preference
                totals = Counter(d["name"] for d in self.live_devices)
                labels = [d["name"] + ("（既定）" if d.get("default") else "") for d in self.live_devices]
                labels = [label + (f"（出力 {i + 1}）" if totals[self.live_devices[i]["name"]] > 1 else "")
                          for i, label in enumerate(labels)]
                self.live_devices_combo.configure(values=labels)
                matched = match_audio_device(self.live_saved_audio_device, self.live_devices)
                if matched is not None:
                    self.live_devices_combo.current(matched)
                    self.live_saved_audio_device = audio_preference(self.live_devices[matched])
                    self.live_device_notice = ""
                    self.persist_live_recording_settings()
                else:
                    self.live_device_name.set("")
                    if self.live_saved_audio_device:
                        self.live_device_notice = "前回の音声出力先が見つからないか、同名の候補が複数あります。接続して「一覧を更新」するか、出力先を選び直してください。"
                    else:
                        self.live_device_notice = "音声出力を取得できません。スピーカー／ヘッドホンを接続して「一覧を更新」してください。"
                    if event.get("error"):self.live_device_notice += " " + str(event["error"])[:100]
                self.update_live_controls()
                self.show_live_preload_status()
            return True
        if kind == "live_ready":
            self.display_result(event["result_path"])
            self.live_recording = True
            self.live_phase = "capturing"
            self.live_active_mode = event.get("recording_mode", "audio")
            label = "取り込み中・画面録画ON" if self.live_active_mode == "screen" else "取り込み中・録音のみ"
            self.stage = f"{label} / {event['device']} — 動画を再生してください。"
            self.live_message.set(self.stage)
            if self.history_visible.get():
                self.open_history()
            self.update_video_controls()
            return True
        if kind == "live_meter":
            if 'segmentation_warning' in event:
                if event['segmentation_warning']:
                    self.live_boundary_hint.set(event['segmentation_warning'])
                else:
                    self.update_live_boundary_hint()
            self.live_level.set(event["level"])
            seconds = int(event["seconds"])
            if self.live_recording and not self.live_stop_requested:
                label = "画面録画停止・録音は継続" if self.live_screen_failed else "画面録画ON" if self.live_active_mode == "screen" else "録音のみ"
                self.live_message.set(f"取り込み中 {seconds // 60:02}:{seconds % 60:02}・{label} / 音量が動かないときは出力先を確認してください。")
            return True
        if kind == 'live_preview':
            if event['utterance'] > self.live_final_utterance and self.overlay:
                names = {**self.speakers, **event.get('speakers',{})}
                for key in (self.result_data or {}).get('speaker_name_edits',{}):
                    if key in self.speakers:names[key] = self.speakers[key]
                self.overlay.set_speakers(names)
                self.overlay.show_partial(event['utterance'], event['segments'])
            return True
        if kind == "live_segments":
            from correction_memory import filter_change
            protected = False
            for record in event.get("learned_records", []):
                try:
                    _, blocked = filter_change(record["before"], record["after"])
                except (OSError, ValueError):
                    blocked = True
                if blocked:
                    row = next((r for r in event["segments"] if r["segment_id"] == record["segment_id"]), None)
                    if row is not None:
                        row.update(text=record["before"], words=record.get("words_before") or [])
                        record["status"] = "reverted"
                        self.result_data.setdefault("live_text_edits", {})[record["segment_id"]] = {
                            "text": row["text"], "words": row["words"]}
                        self.result_data.setdefault("live_context_review_states", {})[record["id"]] = "reverted"
                        protected = True
            manual_names = self.result_data.get("speaker_name_edits", {}) if self.result_data else {}
            for key, name in event.get("speakers", {}).items():
                if key not in manual_names:
                    self.speakers[key] = name
            if self.result_data is not None:
                self.result_data["remembered_speakers"] = event.get("remembered_speakers", {})
                history = self.result_data.setdefault("context_review", [])
                known = {r["id"] for r in history}
                history.extend(r for r in event.get("learned_records", []) if r["id"] not in known)
            for row in event["segments"]:
                index = len(self.segments)
                self.segments.append(row)
                self.table.insert("", "end", iid=str(index), values=self.row_values(row))
            if event["segments"]:
                self.table.see(str(len(self.segments) - 1))
            if self.overlay:
                self.overlay.set_speakers(self.speakers)
                if 'utterance' in event:
                    self.overlay.commit_utterance(event['utterance'], event['segments'])
                elif event['segments']:
                    self.overlay.push_segments(event["segments"])
            if 'utterance' in event:
                self.live_final_utterance = max(self.live_final_utterance,event['utterance'])
            self.refresh_speaker_rows()
            self.refresh_history()
            self.check_pending_memories()
            self.update_speaker_controls()
            self.update_live_controls()
            if protected:
                try:
                    self.persist_edits()
                except OSError as exc:
                    self.stage = f"記憶による修正を取り消しましたが、保存できませんでした：{exc}"
            if self.context_window and self.context_window.winfo_exists():
                self.context_window.refresh()
            return True
        if kind == "live_stopped":
            self.live_recording = False
            self.live_phase = "finishing"
            self.live_level.set(0)
            self.live_message.set("録音停止済み。結果を保存・整理しています。")
            if self.overlay:
                self.overlay.clear()
            if self.context_window and self.context_window.winfo_exists():
                self.context_window.refresh()
            return True
        return False

    def live_speaker_editable(self):
        return bool(self.proc and self.operation == "realtime" and self.live_phase == "capturing")

    def refresh_live_corrected_text(self):
        self.refresh_speaker_rows()
        self.refresh_history()
        if self.selected is not None:
            self.editor.configure(state="normal")
            self.editor.delete("1.0", "end")
            self.editor.insert("1.0", self.segments[int(self.selected)]["text"])
            self.editor.edit_reset()
            if self.proc:
                self.editor.configure(state="disabled")
        self.result_data["segments"] = self.segments

    def persist_live_speaker_edits(self):
        if not self.result_path or not self.result_data or self.result_data.get("mode") != "realtime":
            return
        if self.result_data.get("diarization", {}).get("engine") not in (None, "sherpa-onnx/streaming"):
            return
        from live_speakers import EDITS_FILE
        from context_ai import write_json
        assignments = {row.get("segment_id") or row.get("id"): row["speaker"] for row in self.segments
                       if row.get("speaker_manual") and (row.get("segment_id") or row.get("id"))}
        correction_fields = ("voice_correction_key", "voice_correction", "voice_learning_pending", "voice_rejected_profiles")
        corrections = {row.get("segment_id") or row.get("id"): {k: row[k] for k in correction_fields if k in row}
                       for row in self.segments if row.get("voice_correction_key")}
        write_json(self.result_path.parent / EDITS_FILE, {"version": 3,
            "names": self.result_data.get("speaker_name_edits", {}), "assignments": assignments,
            "memory_links": self.result_data.get("speaker_memory_links", {}), "voice_corrections": corrections,
            "texts": self.result_data.get("live_text_edits", {}),
            "review_states": self.result_data.get("live_context_review_states", {})})
        if self.overlay and not self.overlay.closed:
            self.overlay.set_speakers(self.speakers)
            for row in self.segments:
                if row.get("speaker_manual"):
                    self.overlay.update_assignment(row.get("segment_id") or row.get("id"), row["speaker"])

    def finish_live_ui(self):
        self.check_pending_memories()
        if self.result_path:
            folder = str(self.result_path.parent.resolve())
            for key, intent in list(self.memory_pending.items()):
                if key[0] == folder:
                    self.extract_memory_profile(key, intent)
        self.live_recording = False
        self.live_phase = ""
        self.live_level.set(0)
        self.live_message.set("停止しました。結果はメイン画面で確認・修正できます。")
        self.cancel_button.configure(text="停止")
        if self.overlay:
            self.overlay.clear()
        if self.live_overlay_preview:self.apply_live_overlay_display()
        self.update_live_controls()
        if self.live_preloader:
            self.preload_live_models()
        if self.context_window and self.context_window.winfo_exists():
            self.context_window.refresh()
