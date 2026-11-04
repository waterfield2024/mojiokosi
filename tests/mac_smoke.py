"""Native Apple Silicon checks, run by the macOS CI job (no capture permissions)."""
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
data = tempfile.TemporaryDirectory()
os.environ['WHISPER_USER_DATA_DIR'] = str(Path(data.name)/'UserData')
os.environ['WHISPER_LEGACY_DATA_DIR'] = str(Path(data.name)/'legacy')
import tkinter as tk
from app import App
from platform_support import IS_MAC, tool_path, torch_backends
from mac_native import monitors, displays
from vertical_subtitles import font_at
from video import caption_font, make_cues, write_ass

assert IS_MAC
assert torch_backends('auto', lambda: False) == ['mps', 'cpu']
assert '.exe' not in str(tool_path('ffmpeg'))
assert font_at(24).getlength('こんにちは') > 0
assert caption_font(24)[1]
root = tk.Tk()
errors = []
root.report_callback_exception = lambda *args: errors.append(str(args))
with patch.object(App, 'preload_live_models'), patch.object(App, 'schedule_live_preload'), patch.object(App, 'refresh_live_devices'):
    app = App(root)
    app.update_checker.close()
    try:
        assert displays() and monitors()
        app.open_live()
        app.open_history()
        overlay = app.ensure_overlay()
        overlay.set_locked(True)
        overlay.set_locked(False)
        app.theme_controller.set_mode('dark')
        root.update()
        assert not app.live_noise.get()
        assert app.live_noise_check.instate(['disabled'])
        assert not errors, errors
        from global_hotkey import GlobalHotkey
        shortcut = GlobalHotkey(app.events)
        root.update()
        shortcut.close()
        app.window_settings.flush_all()
    finally:
        app.close()
print('Native Mac GUI, fonts, monitors, overlay, history, settings, shortcut: OK')

# Load and exercise a real model in the same persistent worker used by live mode.
from whisper_hf_asr import WhisperHFRecognizer
job = Path(data.name)/'recognition'
job.mkdir()
recognizer = WhisperHFRecognizer('base', 'ja', '', 'cpu', job, print)
try:
    result = recognizer.transcribe(b'\0' * 32000)
    assert result['segments'] == []
    assert recognizer.healthy()
finally:
    recognizer.close()
print('Mac Whisper base worker load, warmup, silence recognition, close: OK')

import subprocess
source = job/'sample.mp4'
subtitles = job/'subtitles.ass'
target = job/'captioned.mp4'
subprocess.run([str(tool_path('ffmpeg')), '-hide_banner', '-loglevel', 'error', '-f', 'lavfi', '-i',
                'color=c=blue:s=640x360:d=1', '-c:v', 'libx264', str(source)], check=True)
write_ass([dict(start=0., end=1., text='Macで日本語の字幕を確認します。')], subtitles, 640, 360, 1.)
subprocess.run([str(tool_path('ffmpeg')), '-hide_banner', '-loglevel', 'error', '-i', str(source),
                '-vf', 'ass=subtitles.ass', '-c:v', 'libx264', str(target)], cwd=job, check=True)
assert target.stat().st_size > 0
print('Mac Japanese ASS subtitles -> MP4: OK')
