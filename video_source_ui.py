"""Explain audio-only exports before asking for an optional original video."""
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog

from audio_video import saved_audio


class VideoSourceDialog(tk.Toplevel):
    def __init__(self, parent, result, folder):
        super().__init__(parent)
        self.withdraw()
        self.choice = None
        self.audio = saved_audio(result, folder)
        self.title("MP4に保存する内容")
        self.transient(parent)
        self.resizable(False, False)
        outer = ttk.Frame(self, padding=24)
        outer.pack(fill="both", expand=True)
        ttk.Label(outer, text="この録音には映像がありません" if result.get("mode")=="realtime" and not result.get("screen_recording") else
                  "MP4に使う映像を選ぶ", style="Preview.Title.TLabel").pack(anchor="w")
        explanation = ("この回は「録音のみ」で保存されています。\n音声を再生して確認できますが、再生していた動画の映像は残っていません。"
                       if result.get("mode") == "realtime" else
                       "この結果には、利用できる元動画がありません。\n保存した音声があれば、音声と字幕だけでもMP4にできます。")
        if result.get("screen_recording"):
            explanation = "録画した映像を利用できない場合も、録音と字幕をMP4に保存できます。\n映像を付けたい場合は、対応する元動画を選んでください。"
        ttk.Label(outer, text=explanation, wraplength=560).pack(anchor="w", pady=(12, 20))
        self.audio_button = ttk.Button(outer, text="録音を再生・確認する（映像なし）", style="Accent.TButton",
                                       command=self.use_audio, state="normal" if self.audio else "disabled")
        self.audio_button.pack(fill="x")
        ttk.Label(outer, text="黒い背景に字幕を表示します。確認後、そのままMP4として保存できます。" if self.audio else
                  "保存した音声が見つかりません。録音を含む結果フォルダーが必要です。",
                  wraplength=560).pack(anchor="w", pady=(6, 20))
        self.video_button = ttk.Button(outer, text="手元の動画に字幕を付ける…", command=self.use_video)
        self.video_button.pack(fill="x")
        ttk.Label(outer, text="録音と同じ開始位置の動画ファイルがある場合に選んでください。",
                  wraplength=560).pack(anchor="w", pady=(6, 18))
        ttk.Separator(outer).pack(fill="x")
        ttk.Label(outer, text="次回から映像も残すには、開始前に「ウィンドウを録画」か「画面全体を録画」を選びます。" if result.get("mode")=="realtime" else
                  "保存先は、プレビューの「MP4を保存…」で指定します。",
                  wraplength=560).pack(anchor="w", pady=(14, 12))
        ttk.Button(outer, text="キャンセル", command=self.destroy).pack(anchor="e")
        self.bind("<Escape>", lambda event: self.destroy())
        self.update_idletasks()
        x = max(0, parent.winfo_rootx() + (parent.winfo_width() - self.winfo_reqwidth()) // 2)
        y = max(0, parent.winfo_rooty() + (parent.winfo_height() - self.winfo_reqheight()) // 2)
        self.geometry(f"+{x}+{y}")
        self.deiconify()
        self.grab_set()
        (self.audio_button if self.audio else self.video_button).focus_set()

    def use_audio(self):
        if self.audio is not None:
            self.choice = {"video_export_mode": "audio_subtitles", "video_source": str(self.audio)}
            self.destroy()

    def use_video(self):
        source = filedialog.askopenfilename(parent=self,
            title="字幕を付ける元動画を開く（保存先はプレビュー後に指定）",
            filetypes=[("動画", "*.mp4 *.mov *.mkv *.webm *.avi *.m4v *.mts *.wmv"), ("すべて", "*.*")])
        if source:
            self.choice = {"video_export_mode": "source_video", "video_source": str(Path(source).resolve())}
            self.destroy()


def choose_video_source(parent, result, folder):
    dialog = VideoSourceDialog(parent, result, folder)
    parent.wait_window(dialog)
    return dialog.choice
