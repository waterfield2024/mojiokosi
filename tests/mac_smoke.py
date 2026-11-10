"""Native Apple Silicon checks, run by the macOS CI job (no capture permissions)."""
import os
from pathlib import Path
import sys
import tempfile
import subprocess
import wave
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
data = tempfile.TemporaryDirectory()
os.environ['WHISPER_USER_DATA_DIR'] = str(Path(data.name)/'UserData')
os.environ['WHISPER_LEGACY_DATA_DIR'] = str(Path(data.name)/'legacy')
import tkinter as tk
from app import App
from live_shortcut import LiveShortcut
from platform_support import IS_MAC, tool_path, torch_backends
from mac_native import cocoa_window, monitors, displays
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
    app.live_shortcut = LiveShortcut(app)
    try:
        assert displays() and monitors()
        app.open_history()
        root.update()
        panel = app.history_window
        assert panel.live_button.cget('text') == '開始設定を開く'
        panel.live_button.invoke()
        root.update()
        assert app.live_window.winfo_viewable()
        app.window_settings.remember('main', root)
        root.iconify()
        root.update()
        app.open_main_from_history()
        root.update()
        assert root.winfo_viewable() and app.history_window.winfo_viewable()
        overlay = app.ensure_overlay()
        overlay.show(True)
        overlay.set_locked(True)
        root.update()
        assert cocoa_window(overlay.window).ignoresMouseEvents()
        assert not cocoa_window(overlay.control_window).ignoresMouseEvents()
        assert overlay.live_button.winfo_viewable()
        app.window_settings.hide('realtime')
        overlay.live_button.invoke()
        root.update()
        assert app.live_window.winfo_viewable()
        overlay.show(False)
        root.update()
        assert not overlay.control_window.winfo_viewable()
        overlay.show(True)
        overlay.set_locked(False)
        app.theme_controller.set_mode('dark')
        root.update()
        assert not app.live_noise.get()
        assert app.live_noise_check.instate(['disabled'])
        assert not errors, errors
        app.window_settings.flush_all()
    finally:
        app.close()
print('Native Mac GUI, fonts, monitors, overlay, history, settings, shortcut: OK')

# Load and exercise a real model in the same persistent worker used by live mode.
from whisper_hf_asr import WhisperHFRecognizer
job = Path(data.name)/'recognition'
job.mkdir()
speech = job/'speech.aiff'
pcm = job/'speech.wav'
subprocess.run(['/usr/bin/say', '-o', str(speech), 'Hello. This is a test of local speech recognition on a Mac.'], check=True)
subprocess.run([str(tool_path('ffmpeg')), '-hide_banner', '-loglevel', 'error', '-i', str(speech),
                '-ar', '16000', '-ac', '1', '-c:a', 'pcm_s16le', str(pcm)], check=True)
recognizer = WhisperHFRecognizer('base', 'en', '', 'auto', job, print)
try:
    import torch
    if torch.backends.mps.is_available():
        assert recognizer.device == 'MPS', recognizer.failures
    result = recognizer.transcribe(b'\0' * 32000)
    assert result['segments'] == []
    with wave.open(str(pcm)) as stream:
        result = recognizer.transcribe(stream.readframes(stream.getnframes()))
    assert result['text'].strip(), result
    print('Mac real speech result:', recognizer.device, result['text'])
    assert recognizer.healthy()
finally:
    recognizer.close()
print('Mac Whisper base worker load, Metal inference, silence recognition, close: OK')

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

# The separator uses CPU in its background process, leaving Metal for live ASR.
from overlap_model import Separator
import numpy as np
separator = Separator(ROOT/'models', print)
separated = separator.separate(np.zeros(16000, dtype='float32'))
assert separated.shape == (2, 16000) and np.isfinite(separated).all()
print('Mac RE-SepFormer weights, CPU inference, two aligned streams: OK')
