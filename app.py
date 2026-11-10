"""Japanese desktop UI for local Whisper, with editable timestamped segments."""
from __future__ import annotations
from platform_support import IS_MAC, open_path

import ctypes
import json
import os
import queue
import subprocess
import sys
import threading
import time
import tkinter as tk
import uuid
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

# Hold the installation lock before importing modules that an update can replace.
# A power loss during replacement is recovered before settings or models are read.
_installation_session = None
if __name__ == '__main__':
    import atexit
    from app_update import installation_lock, recover_pending
    try:
        _installation_session = installation_lock(Path(__file__).resolve().parent)
        _installation_session.__enter__()
        atexit.register(_installation_session.__exit__, None, None, None)
        recover_pending(Path(__file__).resolve().parent)
        if os.name == 'nt':
            from setup_local import read_state
            if read_state().get('device') == 'cpu':
                os.environ['CUDA_VISIBLE_DEVICES'] = '-1'
    except (OSError, ValueError) as exc:
        messagebox.showerror('Whisperを起動できません', str(exc))
        raise SystemExit(1)

from engine import ROOT, RESULTS, NO_WINDOW, subtitle_text, timestamp, transcript_text, transcript_file_text, youtube_url

from speaker_ui import SpeakerControls
from speaker_memory_ui import SpeakerMemoryControls
from correction_ui import CorrectionControls
from context_ui import ContextControls
from realtime_ui import RealtimeControls
from history_ui import HistoryControls
from comparison_ui import ComparisonControls
from context_ai import ensure_segment_ids
from speakers import normalize_names
from asr import COMPUTE_OPTIONS, load_compute, save_compute
from ui_theme import (ThemeController, brand, section_title, style_text, TranscriptTree,
                      THEME_OPTIONS, load_theme, save_theme)

from recognition_models import FILE_MODELS as MODEL_OPTIONS,model_hint
LANG_OPTIONS = {"日本語": "ja", "自動判定": "auto", "英語": "en"}


class App(SpeakerMemoryControls, SpeakerControls, CorrectionControls, ContextControls, RealtimeControls, ComparisonControls, HistoryControls):
    def __init__(self, root):
        from user_data import initialize
        initialize()
        if IS_MAC: os.environ['WHISPER_GUI_PID'] = str(os.getpid())
        self.root = root
        self.proc = None
        self.operation = None
        self.render_partial = None
        self.video_path = None
        self.video_preview = None
        self.events = queue.Queue()
        self.termination_done = threading.Event()
        self.termination_done.set()
        self.segments = []
        self.speakers = {}
        self.selected = None
        self.result_path = None
        self.result_data = None
        self.job = None
        self.failed = False
        self.completed = False
        self.cancelled = False
        self.stage = "YouTubeのURLを貼り付けるか、音声・動画ファイルを選んでください。"
        from window_settings import WindowSettings
        root.withdraw()
        self.window_settings = WindowSettings(root, self.window_settings_error)
        self.started = 0
        self.init_corrections()
        self.init_context()
        self.init_realtime()
        self.init_history()
        self.init_comparison()
        self.init_speaker_memory()
        self.build()
        from update_ui import StartupUpdateCheck
        self.update_checker = StartupUpdateCheck(self, ROOT)
        root.after(250, self.resume_saved_voice_pools)
        root.protocol("WM_DELETE_WINDOW", self.close)
        root.after(120, self.poll)

    def build(self):
        r = self.root
        r.title("Whisper — リアルタイム字幕対応版")
        if os.name == "nt" and (ROOT / "whisper.ico").is_file():
            r.iconbitmap(str(ROOT / "whisper.ico"))
        width = min(1180, r.winfo_screenwidth() - 80)
        height = min(920, r.winfo_screenheight() - 100)
        r.geometry(f"{width}x{height}")
        r.minsize(min(900, width), min(800, height))
        mode = load_theme()
        self.theme_controller = ThemeController(r, mode)
        outer = ttk.Frame(r, padding=(20, 14), style="App.TFrame")
        outer.pack(fill="both", expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(4, weight=1)
        heading = ttk.Frame(outer, style="App.TFrame")
        heading.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        brand(heading).pack(side="left")
        self.appearance = tk.StringVar(value=next(k for k, v in THEME_OPTIONS.items() if v == mode))
        self.appearance_combo = ttk.Combobox(heading, textvariable=self.appearance,
                                            values=list(THEME_OPTIONS), state="readonly", width=19)
        self.appearance_combo.pack(side="right", padx=(8, 0))
        self.appearance_combo.bind("<<ComboboxSelected>>", lambda event: self.change_appearance())
        ttk.Label(heading, text="外観", style="App.Sub.TLabel").pack(side="right", padx=(14, 0))
        value = load_compute()
        self.compute = tk.StringVar(value=next(k for k, v in COMPUTE_OPTIONS.items() if v == value))
        self.compute_combo = ttk.Combobox(heading, textvariable=self.compute,
                                          values=list(COMPUTE_OPTIONS), state="readonly", width=10)
        self.compute_combo.pack(side="right")
        self.compute_combo.bind("<<ComboboxSelected>>", lambda event: self.save_compute_choice())
        ttk.Label(heading, text="処理方式", style="App.Sub.TLabel").pack(side="right", padx=(14, 8))

        source_box = ttk.Frame(outer, style="Card.TFrame", padding=(16, 12))
        source_box.grid(row=1, column=0, sticky="ew")
        source_heading = section_title(source_box, "01", "動画・音声を選ぶ")
        source_heading.pack(fill="x", pady=(0, 9))
        from transcript_library_ui import open_library
        self.open_button = ttk.Button(source_heading, text="保存した記録", command=lambda: open_library(self))
        self.open_button.pack(side="right", padx=(16, 0))
        self.mode = tk.StringVar(value="youtube")
        radios = ttk.Frame(source_heading)
        radios.pack(side="right")
        ttk.Radiobutton(radios, text="YouTubeのURL", variable=self.mode, value="youtube", command=self.change_mode).pack(side="left", padx=(0, 18))
        ttk.Radiobutton(radios, text="PCの音声・動画", variable=self.mode, value="file", command=self.change_mode).pack(side="left")
        row = ttk.Frame(source_box)
        row.pack(fill="x")
        self.source = tk.StringVar()
        self.entry = ttk.Entry(row, textvariable=self.source, font=("Yu Gothic UI", 11))
        self.entry.pack(side="left", fill="x", expand=True)
        self.browse_button = ttk.Button(row, text="ファイルを選ぶ", command=self.browse, state="disabled")
        self.browse_button.pack(side="left", padx=(8, 0))
        settings = ttk.Frame(source_box)
        settings.pack(fill="x", pady=(9, 8))
        ttk.Label(settings, text="言語", style="Sub.TLabel").pack(side="left", padx=(0, 7))
        self.language = tk.StringVar(value="日本語")
        ttk.Combobox(settings, textvariable=self.language, values=list(LANG_OPTIONS), state="readonly", width=10).pack(side="left")
        ttk.Label(settings, text="認識モデル", style="Sub.TLabel").pack(side="left", padx=(20, 7))
        self.model = tk.StringVar(value="軽量・base")
        self.model_combo = ttk.Combobox(settings, textvariable=self.model, values=list(MODEL_OPTIONS), state="readonly", width=24)
        self.model_combo.pack(side="left")
        self.build_speaker_options(settings)
        self.model_message = tk.StringVar(value=model_hint(MODEL_OPTIONS[self.model.get()]))
        self.model_combo.bind("<<ComboboxSelected>>",lambda event:self.model_message.set(model_hint(MODEL_OPTIONS[self.model.get()])))
        ttk.Label(source_box,textvariable=self.model_message,style="Sub.TLabel",wraplength=800).pack(anchor="w")
        controls = ttk.Frame(outer, style="App.TFrame")
        controls.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        transcription_controls = ttk.Frame(controls, style="App.TFrame")
        transcription_controls.pack(side="left", anchor="s")
        self.start_button = ttk.Button(transcription_controls, text="話者判別・文字起こしを開始", style="Accent.TButton", command=self.start)
        self.start_button.pack(side="left", fill="y")
        self.cancel_button = ttk.Button(transcription_controls, text="停止", command=self.cancel, state="disabled")
        self.cancel_button.pack(side="left", fill="y", padx=(8, 0))
        self.compare_button = ttk.Button(transcription_controls,text="2モデルで比較…",command=self.open_comparison)
        self.compare_button.pack(side="left",fill="y",padx=(8,0))
        self.build_live_button(transcription_controls)
        self.live_button.configure(style="Soft.TButton")
        self.live_button.pack_configure(side="left", fill="y", padx=(8, 0))
        prompt_row = ttk.Frame(source_box)
        prompt_row.pack(fill="x", pady=(8, 0))
        ttk.Label(prompt_row, text="話題・専門用語（任意）", style="Sub.TLabel").pack(anchor="w", pady=(0, 3))
        self.prompt = tk.Text(prompt_row, height=1, width=25, wrap="word", font=("Yu Gothic UI", 10))
        style_text(self.prompt)
        self.prompt.pack(side="left", fill="x", expand=True)

        progress_area = ttk.Frame(outer, style="App.TFrame")
        progress_area.grid(row=3, column=0, sticky="ew", pady=(8, 9))
        self.status = tk.StringVar(value=self.stage)
        status_label = ttk.Label(progress_area, textvariable=self.status, style="App.Sub.TLabel", wraplength=1080)
        status_label.pack(fill="x")
        status_label.bind("<Configure>", lambda e: e.widget.configure(wraplength=max(300, e.width-8)))
        progress_track = ttk.Frame(progress_area, height=4, style="App.TFrame")
        progress_track.pack(fill="x", pady=(5, 0))
        progress_track.pack_propagate(False)
        self.progress = ttk.Progressbar(progress_track, mode="determinate")
        self.progress.pack(fill="both", expand=True)

        result_box = ttk.Frame(outer, style="Card.TFrame", padding=(16, 12))
        result_box.grid(row=4, column=0, sticky="nsew")
        result_heading = section_title(result_box, "02", "確認・修正する")
        result_heading.pack(fill="x", pady=(0, 8))
        self.reanalyze_button = ttk.Button(result_heading, text="話者を再判別", command=self.analyze_saved, state="disabled")
        self.reanalyze_button.pack(side="right")
        ttk.Button(result_heading, text="履歴・検索", command=self.open_history).pack(side="right", padx=(0, 6))
        from overlap_ui import open_review
        ttk.Button(result_heading, text='声の重なり', command=lambda: open_review(self)).pack(side='right', padx=(0, 6))
        table_frame = ttk.Frame(result_box)
        table_frame.pack(fill="both", expand=True)
        self.table = TranscriptTree(table_frame, columns=("start", "end", "speaker", "text"),
                                    displaycolumns=("speaker", "start", "end", "text"), show="headings", selectmode="browse", height=7)
        for key, title, col_width in (("start", "開始", 80), ("end", "終了", 80), ("speaker", "話者（？は要確認）", 160), ("text", "文字起こし結果", 540)):
            self.table.heading(key, text=title)
            self.table.column(key, width=col_width, minwidth=70, stretch=key == "text")
        scroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.table.yview)
        self.table.configure(yscrollcommand=scroll.set)
        self.table.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.table.show_empty("文字起こしの結果がここに表示されます", "動画を選んで開始するか、以前の結果を開いてください。")
        self.table.bind("<<TreeviewSelect>>", self.select)
        editor_hint = self.build_speaker_editor(result_box, table_frame)
        self.editor = tk.Text(result_box, height=2, wrap="word", font=("Yu Gothic UI", 11), undo=True)
        style_text(self.editor)
        self.editor.pack(side="bottom", fill="x", before=editor_hint, pady=(7, 0))
        self.editor.configure(state="disabled")
        self.manual_memory_status = tk.StringVar(value="手動で直した誤変換は、行の切替・保存時に覚えます。")
        self.editor.bind("<FocusIn>", lambda e: self.memory_status.set(self.manual_memory_status.get()), add="+")
        self.build_correction_controls(result_box, table_frame, toolbar=result_heading)
        self.build_context_controls(result_heading, inline=True)

        output_box = ttk.Frame(outer, style="Card.TFrame", padding=(16, 8))
        output_box.grid(row=5, column=0, sticky="ew", pady=(10, 0))
        output_heading = section_title(output_box, "03", "書き出し")
        output_heading.pack(fill="x", pady=(0, 6))
        ttk.Button(output_heading, text="保存フォルダー", command=self.open_folder).pack(side="right")
        ttk.Button(output_heading, text="使い方", command=self.help).pack(side="right", padx=8)
        from data_backup_ui import open_backup
        self.backup_button = ttk.Button(output_heading, text="バックアップ・復元", command=lambda: open_backup(self))
        self.backup_button.pack(side="right", padx=(0, 8))
        self.caption_position_combo = ttk.Combobox(output_heading, textvariable=self.caption_position, width=6, state="readonly")
        self.caption_position_combo.pack(side="right", padx=(6, 14))
        ttk.Label(output_heading, text="位置").pack(side="right")
        self.caption_direction_combo = ttk.Combobox(output_heading, textvariable=self.caption_direction,
                                                   values=["横書き", "縦書き"], width=7, state="readonly")
        self.caption_direction_combo.pack(side="right", padx=(6, 14))
        ttk.Label(output_heading, text="字幕の向き").pack(side="right")
        self.sync_caption_position_controls()
        actions = ttk.Frame(output_box)
        actions.pack(fill="x")
        self.export_buttons = []
        for label, command in (("全文をコピー", self.copy), ("TXTで保存", lambda: self.export("txt")), ("字幕SRTで保存", lambda: self.export("srt"))):
            button = ttk.Button(actions, text=label, command=command, state="disabled")
            button.pack(side="left", padx=(0, 8))
            self.export_buttons.append(button)
        self.video_button = ttk.Button(actions, text="字幕付きMP4を保存", style="Accent.TButton", command=self.export_video, state="disabled")
        self.video_button.pack(side="left", padx=(8, 0))
        self.play_button = ttk.Button(actions, text="出力したMP4を再生", command=self.play_video, state="disabled")
        self.play_button.pack(side="right")
        footer = ttk.Frame(outer, style="App.TFrame")
        footer.grid(row=6, column=0, sticky="ew", pady=(8, 0))
        from update_ui import open_update
        self.update_button = ttk.Button(footer, text="アプリを更新…", command=lambda: open_update(self))
        self.update_button.pack(side="right")
        self.update_notice = tk.StringVar()
        ttk.Label(footer, textvariable=self.update_notice, style="App.Sub.TLabel").pack(side="right", padx=(0, 12))
        r.update_idletasks()
        r.geometry(f"{width}x{height}+{max(0, (r.winfo_screenwidth() - width) // 2)}+{max(0, (r.winfo_screenheight() - height) // 3)}")
        self.window_settings.attach(r, "main")

    def window_settings_error(self, message):
        self.stage = message
        if hasattr(self, "status"):self.status.set(message)

    def change_appearance(self):
        mode = THEME_OPTIONS[self.appearance.get()]
        self.theme_controller.set_mode(mode)
        try:
            save_theme(mode)
        except OSError as exc:
            messagebox.showerror("外観を保存できません",
                                 f"外観は切り替わりましたが、次回の起動用に保存できませんでした。\n{exc}",
                                 parent=self.root)

    def save_compute_choice(self):
        try:
            save_compute(COMPUTE_OPTIONS[self.compute.get()])
        except OSError as exc:
            messagebox.showerror("設定を保存できません", str(exc))

    def change_mode(self):
        self.source.set("")
        self.browse_button.configure(state="normal" if self.mode.get() == "file" else "disabled")

    def browse(self):
        name = filedialog.askopenfilename(title="音声・動画ファイルを選ぶ", filetypes=[
            ("音声・動画", "*.mp3 *.wav *.m4a *.mp4 *.mov *.mkv *.webm *.ogg *.flac *.aac *.wma"),
            ("すべてのファイル", "*.*")])
        if name:
            self.source.set(name)

    def commit(self):
        if self.selected is not None and self.segments:
            index = int(self.selected)
            text = self.editor.get("1.0", "end-1c").strip()
            row = self.segments[index]
            old = row["text"]
            changed = old != text
            if changed:
                row["words"] = []
                row['text_manual'] = True
            row["text"] = text
            if not self.proc and (changed or row.get("manual_memory_pending")):
                from manual_corrections import learn_edit
                # Editing an automatically recalled spelling teaches its original ASR form too.
                if changed and not (row.get("manual_text_owner") == row["segment_id"] and row.get("manual_text_last") == old):
                    previous = next((r for r in reversed(self.context_history()) if r.get("origin") == "manual_memory"
                                     and r["segment_id"] == row["segment_id"] and r["after"] == old), None)
                    if previous:
                        row.update(manual_text_base=previous["before"], manual_text_last=old, manual_text_owner=row["segment_id"])
                try:
                    count = learn_edit(row, old, text)
                    self.manual_memory_status.set(f"手動修正を{count}か所覚えました。同じ文脈で次回から反映します。" if count else
                        "文章を修正しました。数字・追加・削除・大きな言い換えは自動で覚えません。")
                except (OSError, ValueError) as exc:
                    self.manual_memory_status.set(f"文章は修正済みですが、記憶は未保存です。保存時に再試行します：{exc}")
                self.memory_status.set(self.manual_memory_status.get())
            self.table.item(self.selected, values=self.row_values(row))
            if changed:
                self.refresh_history()

    def select(self, event=None):
        selected = self.table.selection()
        if not selected or selected[0] == self.selected:
            return
        self.commit()
        self.selected = selected[0]
        self.editor.configure(state="normal")
        self.editor.delete("1.0", "end")
        self.editor.insert("1.0", self.segments[int(self.selected)]["text"])
        self.editor.edit_reset()
        self.refresh_speaker_choices()
        if self.proc and self.operation in {"video", "diarize", "context", "realtime", "compare", "overlap"}:
            self.editor.configure(state="disabled")

    def display_result(self, path):
        from transcript_library import read_transcript
        data = read_transcript(path)
        clean = []
        for s in data["segments"]:
            clean.append({**s, "start": max(0, float(s["start"])), "end": float(s["end"]), "text": str(s["text"])})
        if self.history_window:self.history_window.stop_audio()
        self.persist_edits()
        self.selected = None
        self.validate_saved_learning(clean, Path(path).parent)
        self.segments = data["segments"] = clean
        ensure_segment_ids(self.segments)
        self.replacement_undo = []
        self.speakers = normalize_names(clean, data.get("speakers"))
        self.result_data = data
        self.result_path = Path(path)
        from screen_capture import recorded_video
        self.video_path = recorded_video(data, self.result_path.parent)
        self.play_button.configure(state="disabled", text="録画したMP4を再生" if self.video_path else "出力したMP4を再生")
        self.table.delete(*self.table.get_children())
        self.editor.configure(state="normal")
        self.editor.delete("1.0", "end")
        self.editor.configure(state="disabled")
        for i, segment in enumerate(clean):
            self.table.insert("", "end", iid=str(i), values=self.row_values(segment))
        for button in self.export_buttons:
            button.configure(state="normal" if clean else "disabled")
        if clean:
            self.table.selection_set("0")
            self.select()
        self.root.title(f"Whisper（話者対応） — {data.get('title', '文字起こし結果')}")
        self.update_video_controls()
        self.refresh_history()
        if self.context_window and self.context_window.winfo_exists():
            self.context_window.refresh()

    def update_video_controls(self):
        self.video_button.configure(state="normal" if self.segments and not self.proc else "disabled")
        self.play_button.configure(state="normal" if self.video_path and self.video_path.is_file() else "disabled")
        self.update_speaker_controls()
        self.update_correction_controls()
        self.update_context_controls()
        self.update_live_controls()
        self.update_comparison_controls()

    def start(self):
        if self.proc:
            return
        self.commit()
        source = self.source.get().strip().strip('"')
        try:
            if self.mode.get() == "youtube":
                source = youtube_url(source)
            elif not Path(source).is_file():
                raise ValueError("音声・動画ファイルを選んでください。")
            prompt = self.prompt.get("1.0", "end-1c").strip()
            if len(prompt) > 1000:
                raise ValueError("話題・専門用語は1000文字以内で入力してください。")
            req = {"source": source, "mode": self.mode.get(), "model": MODEL_OPTIONS[self.model.get()],
                   "language": LANG_OPTIONS[self.language.get()], "prompt": prompt,
                   "diarize": True, "num_speakers": self.speaker_count(),
                   "correction_rules": self.automatic_correction_rules(),
                   "context_correct": self.context_auto_apply.get()}
            self.launch_worker(req)
        except Exception as exc:
            messagebox.showerror("開始できません", str(exc))

    def launch_worker(self, request):
        if self.comparison_window:self.comparison_window.stop_comparison_audio()
        if self.history_window:self.history_window.stop_audio()
        request = dict(request)
        request.setdefault("compute", COMPUTE_OPTIONS[self.compute.get()])
        request.setdefault("speaker_memory_file", str(self.speaker_memory.path))
        self.job = ROOT / ".cache" / "jobs" / uuid.uuid4().hex
        self.job.mkdir(parents=True)
        request_path = self.job / "request.json"
        request_path.write_text(json.dumps(request, ensure_ascii=False), encoding="utf-8")
        python = Path(sys.executable).with_name("python.exe") if os.name == "nt" else Path(sys.executable)
        env = os.environ.copy()
        env["PYTHONUTF8"] = "1"
        if request.get("operation") == "realtime":
            self.proc = self.ensure_live_preloader().run(request_path)
        else:
            if self.live_preloader:
                if request.get("operation") == "compare":
                    # Free this app's resident GPU models before timing two fresh recognizers.
                    self.live_preloader.close()
                    self.live_preloader = None
                    self.live_preload_key = None
                    self.live_preload_phase = "idle"
                    if self.live_preload_timer:
                        self.root.after_cancel(self.live_preload_timer)
                        self.live_preload_timer = None
                else:
                    try:
                        self.live_preloader.release_context()
                    except (OSError, RuntimeError):
                        pass
            self.proc = subprocess.Popen([str(python), "-u", str(ROOT / "engine.py"), str(request_path)],
                                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                         encoding="utf-8", errors="replace", creationflags=NO_WINDOW, env=env)
        self.operation = request.get("operation", "transcribe")
        self.context_checkpoint = None
        self.pending_context_review = False
        self.render_partial = None
        if self.operation == "video":
            from video import partial_output
            self.render_partial = partial_output(request["output"], self.job)
        if self.operation in {"video", "diarize", "context", "realtime", "compare", "overlap"}:
            self.editor.configure(state="disabled")
        self.started = time.monotonic()
        self.failed = self.completed = self.cancelled = False
        self.stage = "準備中…"
        self.start_button.configure(state="disabled")
        self.open_button.configure(state="disabled")
        self.cancel_button.configure(state="normal")
        self.update_video_controls()
        self.progress.configure(mode="indeterminate")
        self.progress.start(12)
        threading.Thread(target=self.read_worker, args=(self.proc, self.job), daemon=True).start()

    def export_video(self):
        if self.proc or not self.segments or self.result_data is None:
            return
        if self.video_preview is not None and self.video_preview.winfo_exists():
            self.video_preview.lift()
            return
        self.commit()
        if not any(s["text"].strip() for s in self.segments):
            messagebox.showerror("字幕がありません", "表示する字幕を入力してください。")
            return
        try:
            data = dict(self.result_data)
            from screen_capture import recorded_video
            recorded = recorded_video(data, self.result_path.parent)
            if recorded and data.get("video_export_mode") in (None, "recorded_screen"):
                data["video_source"] = str(recorded)
            source = str(data.get("video_source") or data.get("source") or "")
            is_youtube = False
            try:
                source = youtube_url(source)
                is_youtube = True
            except ValueError:
                pass
            from result_media import AUDIO_EXTENSIONS, MEDIA_EXTENSIONS, result_file
            selected_source = None
            if not is_youtube:
                local = result_file(self.result_path.parent, source, MEDIA_EXTENSIONS)
                if local:
                    source = str(local)
                elif not source or source != self.source.get().strip().strip('"'):
                    source = ""
                else:
                    selected_source = source
            if data.get("video_export_mode") == "audio_subtitles" or (
                    not is_youtube and (not Path(source).is_file() or Path(source).suffix.lower() in AUDIO_EXTENSIONS)):
                from video_source_ui import choose_video_source
                choice = choose_video_source(self.root, data, self.result_path.parent)
                if not choice:
                    return
                data.update(choice)
                source = choice["video_source"]
                selected_source = source if choice["video_export_mode"] == "source_video" else None
            data.update(video_source=source, segments=self.segments, speakers=self.speakers)
            from video_preview_ui import VideoPreviewDialog
            self.video_preview = VideoPreviewDialog(self, data, self.result_path.parent, selected_source=selected_source)
        except Exception as exc:
            messagebox.showerror("動画を保存できません", str(exc))

    def save_video_preview(self, preview):
        if self.proc or self.video_preview is not preview:
            return
        from video_layout import normalize_layout
        from vertical_subtitles import DIRECTIONS, POSITIONS
        layout = normalize_layout(preview.settings)
        folder = preview.folder
        audio_only = preview.result.get("video_export_mode") == "audio_subtitles"
        stem = "音声と字幕_黒背景" if audio_only else "字幕付き"
        suggested = stem + ".mp4"
        if (folder / suggested).exists():
            suggested = stem + time.strftime("_%Y%m%d_%H%M%S.mp4")
        name = filedialog.asksaveasfilename(parent=preview,
            title="音声と字幕のMP4を保存（黒背景）" if audio_only else "字幕を焼き込んだMP4を保存",
            initialdir=str(folder), initialfile=suggested, defaultextension=".mp4", filetypes=[("MP4動画", "*.mp4")])
        if not name:
            return
        output = Path(name).resolve()
        if output.suffix.lower() != ".mp4":
            raise ValueError("ファイル名の末尾は .mp4 にしてください。")
        if output == preview.source.resolve():
            raise ValueError("元動画と同じ名前には保存できません。別の名前を指定してください。")
        from preview_edits import save_preview_edits
        save_preview_edits(self,preview,layout)
        self.caption_direction.set(next(k for k,v in DIRECTIONS.items() if v == layout["direction"]))
        if layout["direction"] == "vertical":
            self.caption_position.set(next(k for k,v in POSITIONS.items() if v == layout["position"]))
        data = json.loads(json.dumps(self.result_data))
        self.launch_worker({"operation": "video", "result": data, "caption_layout": layout,
                            "caption_direction": layout["direction"], "caption_position": layout["position"],
                            "prepared_video": str(preview.source), "result_folder": str(folder), "output": str(output)})
        preview.destroy()

    def play_video(self):
        from video_preview_ui import VideoPreviewDialog
        if self.video_preview is not None and self.video_preview.winfo_exists():
            self.video_preview.lift()
            return
        if self.video_path and self.video_path.suffix.lower() == ".mp4" and self.video_path.is_file():
            self.video_preview = VideoPreviewDialog(self, {"segments": []}, self.video_path.parent,
                                                   playback_path=self.video_path)

    def read_worker(self, proc, job):
        try:
            with (job / "worker.log").open("w", encoding="utf-8") as log:
                for line in proc.stdout:
                    log.write(line)
                    log.flush()
                    try:
                        event = json.loads(line)
                        if isinstance(event, dict) and "kind" in event:
                            self.events.put(event)
                    except (ValueError, TypeError):
                        pass
        finally:
            proc.stdout.close()
            code = proc.wait()
            # taskkill can report the parent exit before its child releases file handles.
            self.termination_done.wait(15)
            self.events.put({"kind": "exit", "code": code})

    def poll(self):
        try:
            while True:
                event = self.events.get_nowait()
                kind = event["kind"]
                if kind == "voice_correction_sample":
                    self.complete_voice_correction(event["key"], event["token"], event.get("sample"), event.get("error"),
                                                   event.get("waiting_seconds"), event.get("prepared"))
                    continue
                if kind == "speaker_memory_sample":
                    self.complete_memory_profile(event["key"], event["token"], event.get("sample"), event.get("error"))
                    continue
                if self.handle_live_event(event):
                    continue
                if self.handle_comparison_event(event):
                    continue
                if kind == "status":
                    self.stage = event["text"]
                elif kind == "checkpoint":
                    self.context_checkpoint = Path(event["result_path"])
                elif kind == "complete":
                    try:
                        self.display_result(event["result_path"])
                        self.completed = True
                        count = len({s.get("speaker") for s in self.segments if s.get("speaker") not in (None, "", "unknown")})
                        detail = f" / 話者{count}人" if count else ""
                        recalled = len(self.result_data.get("remembered_speakers", {}))
                        if recalled:
                            detail += f" / 登録名{recalled}人"
                        device = self.result_data.get("device")
                        if device:
                            detail += f" / {device}"
                            if device == "CPU" and self.result_data.get("backend_fallbacks"):
                                detail += "（GPU利用不可のため切替）"
                        corrected = self.result_data.get("correction_count", 0)
                        if corrected:
                            detail += f" / 辞書で{corrected}か所修正"
                        if self.result_data.get("manual_memory_count"):
                            detail += f" / 手動の記憶で{self.result_data['manual_memory_count']}か所修正"
                        context_run = self.result_data.get("context_last_run")
                        if context_run:
                            detail += f" / AI修正{context_run['changed_rows']}行"
                            self.pending_context_review = (self.operation == "context" or
                                (self.operation in {"transcribe", "realtime"} and context_run["changed_rows"] > 0))
                        self.stage = f"完了 — {len(self.segments)}行{detail}。「文脈AI校正」で修正前後を確認できます。"
                        if self.operation == "realtime":
                            self.stage = f"リアルタイム録音を保存しました — {len(self.segments)}行{detail}"
                            if self.result_data.get("screen_recording", {}).get("complete"):
                                self.stage = f"録画MP4と文字起こしを保存しました — {len(self.segments)}行。「字幕付きMP4を保存」で字幕を付けられます。"
                            elif self.result_data.get("screen_recording", {}).get("error"):
                                self.stage = "録画は未完了です。録音と文字起こしは保存しました。"
                            if self.result_data.get("live_error"):
                                self.stage = "音声の取り込みを中断しました。途中までの結果と録音は保存されています。"
                        if self.result_data.get("context_error"):
                            self.stage = "文字起こしは保存しました。文脈AI校正は未完了です。「文脈AI校正」から再実行できます。"
                        if self.result_data.get("speaker_error"):
                            self.stage = "文字起こしは保存しました。話者判別は未完了です。「話者を再判別」で再実行できます。"
                    except Exception as exc:
                        self.failed = True
                        messagebox.showerror("結果を開けません", str(exc))
                elif kind == "video_complete":
                    self.video_path = Path(event["output_path"])
                    self.play_button.configure(text="出力したMP4を再生")
                    self.completed = True
                    self.stage = "MP4を保存しました: " + str(self.video_path)
                    self.update_video_controls()
                elif kind == "warning":
                    messagebox.showwarning(event.get("title", "話者判別について"), event["message"])
                elif kind == "error":
                    self.failed = True
                    self.stage = "処理できませんでした。入力を確認して再実行できます。"
                    messagebox.showerror("文脈校正エラー" if self.operation == "context" else "動画出力エラー" if self.operation == "video" else "文字起こしエラー", event["message"])
                elif kind == "exit":
                    self.progress.stop()
                    self.progress.configure(mode="determinate", value=0)
                    self.proc = None
                    if self.operation == "realtime":
                        self.finish_live_ui()
                    if self.render_partial and not self.completed:
                        try:
                            self.render_partial.unlink(missing_ok=True)
                        except OSError:
                            pass
                    self.render_partial = None
                    if self.selected is not None:
                        self.editor.configure(state="normal")
                    self.update_video_controls()
                    self.start_button.configure(state="normal")
                    self.open_button.configure(state="normal")
                    self.cancel_button.configure(state="disabled")
                    if self.cancelled:
                        if self.context_checkpoint and not self.completed:
                            try:
                                self.display_result(self.context_checkpoint)
                                self.stage = ("途中までの文字起こしと録音を保存しました。" if self.operation == "realtime" else "処理を停止しました。完了した文字起こし結果は保存されています。")
                            except Exception as exc:
                                self.stage = f"停止しました。保存した結果: {self.context_checkpoint}"
                                messagebox.showerror("保存結果を開けません", str(exc))
                        else:
                            self.stage = "停止しました。停止前の結果は保持されています。"
                    elif self.operation == "realtime" and not self.completed and self.context_checkpoint:
                        try:
                            self.display_result(self.context_checkpoint)
                            self.stage = "処理を中断しました。途中の結果と録音は保存されています。"
                        except Exception:
                            self.stage = f"途中の結果: {self.context_checkpoint}"
                    elif not self.failed and not self.completed:
                        self.stage = "処理が終了しました。詳細は .cache/jobs 内の worker.log にあります。"
                        messagebox.showerror("処理が終了しました", self.stage)
                    if self.operation == "compare":
                        self.finish_comparison(event)
                    self.status.set(self.stage)
                    if self.pending_context_review and not self.cancelled and self.root.state() != "iconic":
                        self.pending_context_review = False
                        self.open_context_review()
        except queue.Empty:
            pass
        if self.live_shortcut:self.live_shortcut.pump()
        self.pump_history()
        if self.proc and not self.completed:
            elapsed = int(time.monotonic() - self.started)
            self.status.set(f"{self.stage}   経過 {elapsed // 60:02}:{elapsed % 60:02}")
        else:
            self.status.set(self.stage)
        self.root.after(30 if self.live_recording else 120, self.poll)

    def cancel(self):
        if not self.proc or self.proc.poll() is not None:
            return
        if self.stop_live():
            return
        self.cancelled = True
        self.stage = "停止しています…"
        self.cancel_button.configure(state="disabled")
        proc = self.proc
        if self.operation == "realtime":
            proc.cancelled = True
        self.termination_done.clear()
        def terminate():
            try:
                if os.name == "nt" and proc.poll() is None:
                    subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
                elif proc.poll() is None:
                    proc.terminate()
            finally:
                self.termination_done.set()
            if self.operation == "context" or self.context_checkpoint:
                from context_ai import release_model
                release_model()
        threading.Thread(target=terminate, daemon=True).start()

    def export(self, extension):
        self.commit()
        if not self.segments:
            return
        folder = self.result_path.parent if self.result_path else RESULTS
        name = filedialog.asksaveasfilename(title="修正した結果を保存", initialdir=str(folder),
            initialfile=f"edited.{extension}", defaultextension=f".{extension}",
            filetypes=[("テキスト" if extension == "txt" else "字幕SRT", f"*.{extension}")])
        if not name:
            return
        try:
            text = transcript_file_text(self.segments, self.speakers) if extension == "txt" else subtitle_text(self.segments, self.speakers)
            Path(name).write_text(text, encoding="utf-8-sig")
            # Persist edits alongside the original, keeping the original untouched.
            self.persist_edits()
            self.stage = "保存しました: " + Path(name).name
        except Exception as exc:
            messagebox.showerror("保存できません", str(exc))

    def persist_edits(self):
        self.commit()
        self.persist_live_speaker_edits()
        if self.result_path and self.result_data is not None:
            self.result_data["segments"] = self.segments
            self.result_data["speakers"] = self.speakers
            edited = self.result_path.parent / "edited.json"
            from context_ai import write_json
            write_json(edited, self.result_data)
        self.refresh_history()

    def copy(self):
        self.commit()
        self.root.clipboard_clear()
        self.root.clipboard_append(transcript_text(self.segments, self.speakers))
        self.stage = "全文をクリップボードにコピーしました。"

    def open_folder(self):
        folder = self.result_path.parent if self.result_path else RESULTS
        folder.mkdir(parents=True, exist_ok=True)
        open_path(folder)

    def load_saved(self, parent=None):
        self.commit()
        name = filedialog.askopenfilename(parent=parent or self.root, title="保存済みの結果・比較結果（comparison.json）を開く",
                                         initialdir=str(RESULTS), filetypes=[("文字起こし結果", "*.json")])
        if name:
            try:
                data = json.loads(Path(name).read_text("utf-8"))
                if isinstance(data,dict) and data.get("kind") == "model_comparison":
                    self.open_saved_comparison(name)
                    self.stage = "保存済みの比較結果を開きました。"
                else:
                    self.display_result(name)
                    self.stage = "保存済みの結果を開きました。"
                return True
            except Exception as exc:
                messagebox.showerror("開けません", str(exc), parent=parent or self.root)
        return False

    def help(self):
        messagebox.showinfo("使い方",
            "「PC音声をリアルタイム…」では、再生中の音声を取り込み、動画の上に字幕を表示できます。\n"
            "画面を開くとモデルを先読みします。「モデル準備完了」になったら開始できます。\n"
            "音声の区切りは低遅延の2秒が標準です。誤認識が多い場合は3〜5秒に変更できます。\n"
            "字幕は先に表示し、日本語の漢字の誤変換だけを文脈AIで後から修正します。\n"
            "校正が遅い場合は字幕を優先します。録音中も「文脈AI校正」から確認・取り消しができます。\n"
            "停止後もモデルを保持して再開に使い、Whisperアプリを終了すると解放します。\n"
            "「Nemotronでノイズ除去」は認識用音声だけに適用します。録音・話者判別には元音声を使います。\n"
            "ノイズ除去は録音中も切り替えでき、次の区間から反映します。NVIDIA GPUが必要です。\n"
            "画面端の履歴には直近100件を表示します。検索欄で今の文字起こし全文から単語を探せます。\n"
            "「履歴・検索」から開けます。スクロール中は追従を止め、「最新へ」で最新の発言に戻ります。\n"
            "履歴の発言をクリックすると、その区間の音声を再生します。「音声を停止」で止められます。\n"
            "履歴下部の「再生速度」で0.5〜1.5倍を選べます。再生中に変更すると発言の最初から聞き直します。\n"
            "その下の「整理した文章」で最新の発言をつなげて読めます。修正も反映し、「コピー」で取り出せます。\n"
            "字幕の位置をプレビューで調整し、音声出力を選んで開始してください。\n"
            "リアルタイム中も話者を自動判別し、字幕に名前を表示します。\n"
            "覚えた声の名前は、YouTube・動画の文字起こしとリアルタイム字幕で自動表示します。\n"
            "登録は「覚えた話者…」で管理し、誤判定は「この行の話者」で修正できます。\n"
            "録音は .audio/recording.wav に保存されます。\n\n"
            "1. YouTubeの動画URL、またはPCの音声・動画ファイルを指定します。\n"
            "2. 「話者判別・文字起こしを開始」で、話者判別から順に自動で実行します。\n"
            "   「Haas-ja-v1.0（CPU）」も選べます。日本語専用・非公式ONNX変換版で、初回に約481MBを取得します。\n"
            "   「2モデルで比較…」ではWhisperとGraniteの文章と処理時間を並べて確認できます。\n"
            "   違いごとに「Whisperを採用」「Graniteを採用」を選び、「選んだ文章をまとめる」で統合できます。\n"
            "   「この部分を再生」で元の音声を聞きながら採用できます。再生中は「音声を停止」で止められます。\n"
            "   横の「速度」で0.5〜1.5倍に変更できます。再生中の変更は、その部分を最初から聞き直します。\n"
            "   「繰り返し」をオンにすると、同じ部分を続けて聞けます。解除すると今の再生で終了します。\n"
            "   再生範囲の「開始・終了」を入力し「範囲を反映」で、聞きたい部分だけを再生・繰り返しできます。\n"
            "   波形の「開始・終了」をドラッグしても調整できます。マウスを離すと反映し、ホイールで拡大できます。\n"
            "   波形をクリックするとその位置から再生します。再生中もクリックで移動できます。\n"
            "   選択は自動保存します。比較中は自動修正・話題ヒントを使いません。「全文を編集・出力」では片方をそのまま使えます。\n"
            "3. 行を選んで文章や「この行の話者」を修正します。\n"
            "   「話者の名前を設定」で話者1などを実名・呼び名に変更できます。\n"
            "   必要なら「新しい話者を追加」で別の話者を作れます。\n"
            "   行の途中で話者が変わる場合は、文章の途中にカーソルを置いて分割できます。\n"
            "   人数が分かる場合は上の「人数」を指定すると判別の手掛かりになります。\n"
            "   過去の結果には「話者を再判別」を使えます。\n"
            "   話者名はTXT・SRT・MP4にも反映されます。\n"
            "   「検索」と「置換後」に文字を入れ「すべて置換」で全行を修正できます。\n"
            "   「文脈AI校正」はPC内のAIで誤変換を判断し、修正前後・理由を表示します。\n"
            "   手動で直した短い語句も、行の切替・保存時に覚え、次回から同じ文脈に反映します。\n"
            "   「文脈AI校正」→「覚えた修正…」で、記憶した内容を確認・解除できます。\n"
            "   誤った修正は選択して「この修正を元に戻す」で戻せます。履歴は保存されます。\n"
            "   文字起こし後の自動校正は、同じ画面でON/OFFを切り替えられます。\n"
            "   「誤変換辞書」はAIがONなら判断の参考、OFFなら文字列の一括置換に使います。\n"
            "   辞書を今の結果に適用することもできます。直前の一括置換は「置換を戻す」で戻せます。\n"
            "4. 「字幕付きMP4を保存」でプレビューを開きます。\n"
            "   「再生／一時停止」で音声付きの映像を確認できます。\n"
            "   再生中も字幕をドラッグして位置を調整し、文字サイズを変えられます。\n"
            "   「調整する字幕」を選び、文章・話者名を編集して「反映」で確認できます。\n"
            "   名前は同じ話者の全字幕に反映します。「変更を戻す」で編集を取り消せます。\n"
            "   「調整する字幕」を選び、「今を開始に／今を終了に」で表示時刻を変更できます。\n"
            "   秒数を入力して「反映」で微調整し、「変更を戻す」で取り消せます。\n"
            "   時間バーや前後の字幕で別の場面を確認し、「MP4を保存…」を押します。\n"
            "   白文字・黒縁、横書きは最大2行、縦書きは最大2列で話者名も表示します。\n"
            "   書き出し中は「停止」で中止できます。\n\n"
            "YouTubeの映像は初回のMP4出力時に取得し、次回から再利用します。\n"
            "以前の文字起こし結果を開いて、MP4出力することもできます。\n"
            "リアルタイム画面で「画面も録画」と「録音のみ」を選べます。\n"
            "録画した映像は、そのまま字幕付きMP4のプレビューに使えます。\n"
            "録音のみの結果は、音声と字幕を黒背景のMP4に保存できます。\n"
            "映像に字幕を付けたい場合は、同じ開始位置・長さの元動画を選びます。\n"
            "「開く」は元動画の読み込みです。保存先はプレビューの後で指定します。\n\n"
            "初回セットアップではbaseを準備します。別のモデルは初回利用時にダウンロードします。\n"
            "音声認識はPC内で実行します。APIキーや従量課金はありません。\n"
            "同音異義語などの誤変換は残ることがあります。内容を確認してください。\n"
            "YouTube側のログイン・アクセス制限により取得できない動画もあります。\n"
            "設定・話者・学習音声・辞書・結果は、アプリ内のUserDataフォルダーに自動保存します。\n"
            "別PCへはアプリのフォルダーごとコピーし、start.cmdから起動してください。\n"
            "起動後に最新版を自動で確認し、更新があれば画面右下にお知らせします。\n"
            "「更新内容を見る…」から内容を確認し、「更新して再起動」でデータを引き継いで更新できます。")

    def close(self):
        updater = getattr(self, 'update_dialog', None)
        if updater:
            if updater.busy:
                messagebox.showinfo('アプリの更新', '更新の準備が終わるまでお待ちください。', parent=updater)
                return
            updater.close()
        if getattr(self, 'data_transfer_active', False):
            dialog = getattr(self, 'backup_dialog', None)
            if dialog and dialog.busy:
                messagebox.showinfo('バックアップ・復元', '処理が終わるまでお待ちください。', parent=dialog)
                return
            if dialog: dialog.close()
        self.window_settings.flush_all()
        self.commit()
        if self.proc:
            if not messagebox.askyesno("終了", "実行中の処理を停止して閉じますか？"):
                return
            self.cancel()
        # Keep current edits even if the window is closed without exporting.
        if self.result_path and self.segments:
            try:
                self.persist_edits()
            except OSError as exc:
                if not messagebox.askyesno("修正内容を保存できません", f"{exc}\n\n保存せず終了しますか？"):
                    return
        if self.live_shortcut:self.live_shortcut.close()
        if self.proc:
            self.root.after(250, self.wait_close)
        else:
            if self.overlay:
                self.overlay.close()
            review = getattr(self, "voice_review", None)
            if review:
                review.close()
            self.root.destroy()

    def wait_close(self):
        if self.proc and self.proc.poll() is None:
            self.root.after(250, self.wait_close)
        else:
            self.window_settings.flush_all()
            if self.overlay:
                self.overlay.close()
            review = getattr(self, "voice_review", None)
            if review:
                review.close()
            self.root.destroy()


def main():
    if os.name == "nt":
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("WhisperDesktop.Local")
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass
    root = tk.Tk()
    root.withdraw()
    from data_storage import application_session
    from user_data import data_root
    app = None
    try:
        with application_session(data_root()):
            app = App(root)
            from live_shortcut import LiveShortcut
            app.live_shortcut = LiveShortcut(app)
            if "--smoke-test" in sys.argv:
                root.after(800, root.destroy)
            root.mainloop()
    except (OSError, ValueError) as exc:
        messagebox.showerror("保存データを読み込めません", f"{exc}\n\n元のデータはそのまま残しています。保存先を確認して起動し直してください。", parent=root)
        root.destroy()
        raise SystemExit(1)
    if _installation_session:
        _installation_session.__exit__(None, None, None)
        atexit.unregister(_installation_session.__exit__)
    if app and getattr(app, '_pending_app_update', None):
        from app_update import launch_helper
        try:
            launch_helper(ROOT, app._pending_app_update)
        except (OSError, ValueError) as exc:
            messagebox.showerror('更新を開始できません', f'プログラムと保存データは変更していません。\n{exc}\nstart.cmdで起動して更新を再試行してください。')
    elif app and getattr(app, '_restart_after_restore', False):
        python = Path(sys.executable).with_name('pythonw.exe')
        try:
            subprocess.Popen([str(python if python.is_file() else Path(sys.executable)), '-X', 'utf8', str(ROOT / 'app.py')],
                             cwd=ROOT, env=os.environ.copy(), creationflags=NO_WINDOW)
        except OSError:
            messagebox.showerror('復元しました', 'データは復元済みです。start.command（Windowsはstart.cmd）から起動してください。')


if __name__ == "__main__":
    main()
