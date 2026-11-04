"""Exact HWND selection and Windows Graphics Capture (never desktop cropping)."""
from platform_support import IS_MAC
import ctypes
from ctypes import wintypes as w
import os
from pathlib import Path
import threading
import time


def _api():
    user, kernel = ctypes.windll.user32, ctypes.windll.kernel32
    for name in ('IsWindow', 'IsWindowVisible', 'IsIconic'):
        getattr(user, name).argtypes = [w.HWND]
    user.GetWindowThreadProcessId.argtypes = [w.HWND, ctypes.POINTER(w.DWORD)]
    user.GetWindowTextLengthW.argtypes = [w.HWND]
    user.GetWindowTextW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]
    user.GetClassNameW.argtypes = [w.HWND, w.LPWSTR, ctypes.c_int]
    user.GetWindowRect.argtypes = [w.HWND, ctypes.POINTER(w.RECT)]
    user.GetWindowLongPtrW.argtypes = [w.HWND, ctypes.c_int]
    user.GetWindowLongPtrW.restype = ctypes.c_ssize_t
    kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
    kernel.OpenProcess.restype = w.HANDLE
    kernel.GetProcessTimes.argtypes = [w.HANDLE] + [ctypes.POINTER(w.FILETIME)] * 4
    kernel.QueryFullProcessImageNameW.argtypes = [w.HANDLE, w.DWORD, w.LPWSTR, ctypes.POINTER(w.DWORD)]
    kernel.CloseHandle.argtypes = [w.HANDLE]
    return user, kernel


def window_info(hwnd):
    if IS_MAC:
        from mac_native import window_info as mac_info
        return mac_info(hwnd)
    user, kernel = _api()
    if not user.IsWindow(hwnd):return None
    pid = w.DWORD(); user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    process = kernel.OpenProcess(0x1000, False, pid.value)
    if not process:return None
    try:
        times = [w.FILETIME() for _ in range(4)]
        if not kernel.GetProcessTimes(process, *(ctypes.byref(t) for t in times)):return None
        created = (times[0].dwHighDateTime << 32) | times[0].dwLowDateTime
        path = ctypes.create_unicode_buffer(32768); length = w.DWORD(len(path))
        exe = path.value if kernel.QueryFullProcessImageNameW(process, 0, path, ctypes.byref(length)) else ""
        app = Path(exe).name if exe else str(pid.value)
    finally:kernel.CloseHandle(process)
    title = ctypes.create_unicode_buffer(user.GetWindowTextLengthW(hwnd) + 1)
    user.GetWindowTextW(hwnd, title, len(title))
    cls = ctypes.create_unicode_buffer(256); user.GetClassNameW(hwnd, cls, len(cls))
    rect = w.RECT()
    if not user.GetWindowRect(hwnd, ctypes.byref(rect)):return None
    cloaked = w.DWORD()
    dwm = ctypes.windll.dwmapi
    dwm.DwmGetWindowAttribute.argtypes = [w.HWND, w.DWORD, ctypes.c_void_p, w.DWORD]
    dwm.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(cloaked), ctypes.sizeof(cloaked))
    return {'kind': 'window', 'hwnd': int(hwnd), 'pid': pid.value, 'created': created,
            'class': cls.value, 'title': title.value, 'app': app, 'exe': exe,
            'rect': [rect.left, rect.top, rect.right, rect.bottom],
            'minimized': bool(user.IsIconic(hwnd)),
            'hidden': not user.IsWindowVisible(hwnd) or bool(cloaked.value),
            'label': f'{title.value}  —  {Path(app).stem}'}


def windows():
    if IS_MAC:
        from mac_native import windows as mac_windows
        return mac_windows()
    user, _ = _api(); found = []
    callback_type = ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
    user.EnumWindows.argtypes = [callback_type, w.LPARAM]
    def visit(hwnd, data):
        if not user.IsWindowVisible(hwnd) or user.GetWindowLongPtrW(hwnd, -20) & 0x80:return True
        item = window_info(hwnd)
        if (item and item['pid'] != os.getpid() and item['title'] and not item['hidden']
                and item['class'] not in {'Progman', 'WorkerW', 'Shell_TrayWnd', 'Shell_SecondaryTrayWnd'}):
            if item['minimized']:item['label'] += '（最小化中）'
            found.append(item)
        return True
    if not user.EnumWindows(callback_type(visit), 0):raise OSError('ウィンドウ一覧を取得できませんでした。')
    from collections import Counter
    totals = Counter(item['label'] for item in found); seen = Counter()
    for item in found:
        label = item['label']; seen[label] += 1
        if totals[label] > 1:item['label'] += f'（同名 {seen[label]}）'
    return found


def window_state(selected):
    current = window_info(int(selected.get('hwnd', 0)))
    if not current or any(current[k] != selected.get(k) for k in ('pid', 'created', 'class')):
        return 'closed'
    return 'paused' if current['minimized'] or current['hidden'] else 'active'


def validate_window(selected):
    state = window_state(selected)
    if state == 'closed':raise ValueError('選んだウィンドウは閉じられました。一覧を更新して選び直してください。')
    if state == 'paused':raise ValueError('選んだウィンドウを通常の表示に戻してから開始してください。')
    current = window_info(int(selected['hwnd']))
    if not current:raise ValueError('選んだウィンドウを確認できません。一覧を更新してください。')
    return current


class WindowFrames:
    """One latest frame only; resize/occlusion never change the selected target."""
    def __init__(self, selected):
        from windows_capture import WindowsCapture
        self.selected = validate_window(selected)
        self.lock = threading.Lock(); self.ready = threading.Event(); self.closed = threading.Event()
        self.latest = None; self.stopping = False; self.control = None
        # Optional newer Windows properties are deliberately left unset. The
        # default excludes secondary windows, including on Windows 10.
        self.capture = WindowsCapture(window_hwnd=self.selected['hwnd'], cursor_capture=False)
        @self.capture.event
        def on_frame_arrived(frame, control):
            if self.stopping:control.stop();return
            user = ctypes.windll.user32
            if user.IsIconic(self.selected['hwnd']) or not user.IsWindowVisible(self.selected['hwnd']):return
            if frame.width < 2 or frame.height < 2:return
            # Own the bytes before returning the borrowed GPU mapping to Windows.
            # Keep every final update: a static window may never send another
            # event after a short burst (especially during restore/resize).
            # RGB conversion runs only at the encoder's 15fps, outside callbacks.
            owned = ((frame.width, frame.height), frame.frame_buffer.tobytes())
            with self.lock:self.latest = owned
            self.ready.set()
        @self.capture.event
        def on_closed():
            if not self.stopping:self.closed.set()
        try:
            self.control = self.capture.start_free_threaded()
            deadline = time.monotonic() + 12
            while not self.ready.wait(.05):
                self.check()
                if self.closed.is_set():raise RuntimeError('録画の準備中にウィンドウが閉じられました。')
                if time.monotonic() > deadline:raise RuntimeError('ウィンドウの映像を取得できません。動画を表示してから再試行してください。')
        except BaseException:
            self.close();raise

    def check(self):
        if self.control and self.control.is_finished() and not self.closed.is_set() and not self.stopping:
            self.control.wait()  # Propagate native capture errors; no desktop fallback.
            raise RuntimeError('ウィンドウの映像を取得できなくなりました。')

    def image(self):
        self.check()
        from PIL import Image
        with self.lock:size, pixels = self.latest
        return Image.frombytes('RGB', size, pixels, 'raw', 'BGRX')

    def close(self):
        self.stopping = True
        if self.control:
            control, self.control = self.control, None
            control.stop()
