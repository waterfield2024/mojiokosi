"""Windows monitor selection and an owned, audio-clocked screen recorder."""
import ctypes
from ctypes import wintypes as w
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

from engine import ROOT, TOOLS, NO_WINDOW
from live_process import ProcessJob

FPS = 15


def monitors():
    user = ctypes.windll.user32
    class MonitorInfo(ctypes.Structure):
        _fields_ = [('size', w.DWORD), ('rect', w.RECT), ('work', w.RECT), ('flags', w.DWORD), ('device', w.WCHAR * 32)]
    callback_type = ctypes.WINFUNCTYPE(w.BOOL, w.HMONITOR, w.HDC, ctypes.POINTER(w.RECT), w.LPARAM)
    user.GetMonitorInfoW.argtypes = [w.HMONITOR, ctypes.POINTER(MonitorInfo)]
    user.EnumDisplayMonitors.argtypes = [w.HDC, ctypes.POINTER(w.RECT), callback_type, w.LPARAM]
    user.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
    user.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
    previous = user.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
    found = []
    def visit(handle, dc, rect, data):
        info = MonitorInfo(); info.size = ctypes.sizeof(info)
        if user.GetMonitorInfoW(handle, ctypes.byref(info)):
            r = info.rect
            found.append({'device': info.device, 'rect': [r.left, r.top, r.right, r.bottom], 'primary': bool(info.flags & 1)})
        return True
    try:
        if not user.EnumDisplayMonitors(None, None, callback_type(visit), 0):
            raise OSError('録画できる画面を取得できませんでした。')
    finally:
        if previous:user.SetThreadDpiAwarenessContext(previous)
    found.sort(key=lambda m: (not m['primary'], m['device']))
    for i, m in enumerate(found, 1):
        left, top, right, bottom = m['rect']
        m['label'] = f"画面 {i}（{right-left}×{bottom-top}）" + ('・メイン' if m['primary'] else '')
    return found


def validate_monitor(selected):
    for current in monitors():
        if current['device'] == selected.get('device') and current['rect'] == selected.get('rect'):
            return current
    raise ValueError('画面の接続・サイズが変わりました。画面一覧を更新して録画先を選び直してください。')


def validate_target(selected):
    if selected.get('kind') == 'window':
        from window_capture import validate_window
        return validate_window(selected)
    return validate_monitor(selected)


def exclude_window(window):
    """Keep our live subtitle overlay out of the underlying recording."""
    window.update_idletasks()
    user = ctypes.windll.user32
    user.GetParent.argtypes = [ctypes.c_void_p]; user.GetParent.restype = ctypes.c_void_p
    user.SetWindowDisplayAffinity.argtypes = [ctypes.c_void_p, w.DWORD]
    user.SetWindowDisplayAffinity.restype = w.BOOL
    handle = user.GetParent(window.winfo_id()) or window.winfo_id()
    return bool(user.SetWindowDisplayAffinity(handle, 0x11))


def recorded_video(result, folder):
    recording = result.get('screen_recording') or {}
    if not recording.get('complete'):
        return None
    # The local relative path survives moving the entire result folder.
    relative = recording.get('file', '')
    if relative:
        candidate = (Path(folder) / relative).resolve()
        if candidate.is_relative_to(Path(folder).resolve()) and candidate.is_file():
            return candidate
    source = result.get('video_source')
    return Path(source) if source and Path(source).is_file() else None


class ScreenRecorder:
    def __init__(self, selected, folder, stopped, status=lambda text: None):
        self.folder = Path(folder) / '.recording'; self.folder.mkdir(parents=True, exist_ok=True)
        self.status, self.stopped = status, stopped
        self.error = ''; self.details = {}; self.ready = threading.Event(); self.done = threading.Event()
        self.lock = threading.Lock(); self.closed = threading.Event(); self.monitor = None
        self.target_closed = threading.Event()
        self.log = (self.folder / 'recorder.log').open('w', encoding='utf-8')
        python = Path(sys.executable).with_name('python.exe')
        self.proc = subprocess.Popen([str(python), '-X', 'utf8', '-u', str(ROOT/'screen_capture_worker.py')],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log, text=True, encoding='utf-8', creationflags=NO_WINDOW)
        self.owner = ProcessJob(self.proc)
        if not self.owner.handle:
            self.proc.terminate(); self.proc.wait(timeout=10); self.log.close()
            raise RuntimeError('画面録画を安全に起動できませんでした。アプリを起動し直してください。')
        threading.Thread(target=self.read, daemon=True).start()
        try:
            self.send({'op':'prepare', 'rect':selected['rect'], 'device':selected.get('device'),
                       'window':selected if selected.get('kind') == 'window' else None, 'folder':str(self.folder), 'fps':FPS})
            deadline = time.monotonic()+20
            while not self.ready.wait(.05):
                if self.done.is_set():raise RuntimeError(self.error or '画面録画を準備できませんでした。')
                if stopped():raise RuntimeError('画面録画の準備を中止しました。')
                if time.monotonic()>deadline:raise RuntimeError('画面録画の準備が時間内に完了しませんでした。')
        except BaseException:
            self.close(); raise

    def send(self, command):
        with self.lock:
            self.proc.stdin.write(json.dumps(command)+'\n'); self.proc.stdin.flush()

    def read(self):
        try:
            for line in self.proc.stdout:
                event = json.loads(line)
                if event['kind']=='ready':self.details.update(event); self.ready.set()
                elif event['kind']=='complete':self.details.update(event)
                elif event['kind']=='target_closed':
                    self.details['stop_reason'] = 'window_closed'; self.target_closed.set()
                    self.status('選んだウィンドウが閉じられたため、録画・録音を停止して保存します。')
                elif event['kind']=='status':self.status(event['text'])
                elif event['kind']=='error':
                    self.error=event['text']
                    self.status('画面録画が停止しました。録音・文字起こしは続きます。 '+self.error)
        except (OSError, ValueError) as exc:
            self.error=self.error or str(exc)
        finally:
            code = self.proc.wait()
            if not self.closed.is_set() and not self.error and (code or (self.monitor and not self.details.get('frames'))):
                self.error = '画面録画の処理が予期せず終了しました。途中までの録音は保存できます。'
                self.status('画面録画が停止しました。録音・文字起こしは続きます。 '+self.error)
            self.done.set()

    def start(self, capture):
        if capture.epoch is None:raise RuntimeError('録音の開始時刻を取得できませんでした。')
        self.send({'op':'start', 'epoch':capture.epoch})
        def monitor():
            try:
                while not self.closed.wait(.03):
                    if self.target_closed.is_set():capture.stop()
                    if self.stopped() or capture.halt.is_set() or capture.done.is_set():
                        self.send({'op':'freeze'})
                        while not capture.done.wait(.03):
                            if self.closed.is_set():return
                        self.send({'op':'finish', 'duration':capture.total/16000})
                        return
                    if self.done.is_set():return
            except (OSError, ValueError):
                pass
        self.monitor=threading.Thread(target=monitor, daemon=True); self.monitor.start()

    def finish(self, audio, duration):
        if not math.isfinite(duration) or duration<=0:
            raise RuntimeError('録画に保存する音声がありません。')
        if not self.done.wait(30):raise RuntimeError('画面録画の終了を確認できませんでした。途中までの映像と録音は残っています。')
        if self.error or self.proc.returncode or not self.details.get('frames'):
            raise RuntimeError(self.error or '画面録画を保存できませんでした。途中までの録音は残っています。')
        target = self.folder/'録画.mp4'; temporary = self.folder/'録画.partial.mp4'
        self.status('録画を停止しました。映像と音声をMP4に保存しています…')
        command=[str(TOOLS/'ffmpeg.exe'), '-nostdin','-hide_banner','-loglevel','error','-n',
                 '-i',str(self.folder/'screen.mkv'),'-i',str(audio),'-map','0:v:0','-map','1:a:0',
                 '-c:v','copy','-c:a','aac','-b:a','160k','-t',str(duration),'-movflags','+faststart',
                 '-map_metadata','-1',str(temporary)]
        try:
            completed=subprocess.run(command,capture_output=True,creationflags=NO_WINDOW)
            if completed.returncode:raise RuntimeError('録画の音声を保存できませんでした。\n'+completed.stderr.decode('utf-8',errors='replace')[-1000:])
            os.replace(temporary,target)
        finally:
            temporary.unlink(missing_ok=True)
        return target.resolve()

    def close(self):
        self.closed.set()
        if self.proc.poll() is None:
            try:self.send({'op':'cancel'})
            except (OSError,ValueError):pass
            try:self.proc.wait(timeout=2)
            except subprocess.TimeoutExpired:self.owner.close();self.proc.wait(timeout=10)
        self.owner.close()
        if self.monitor:self.monitor.join(timeout=1)
        for stream in (self.proc.stdin,self.proc.stdout,self.log):
            try:stream.close()
            except (OSError,ValueError):pass
