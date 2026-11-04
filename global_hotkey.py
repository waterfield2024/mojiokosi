"""One registered Windows shortcut; no keyboard hook or text input capture."""
from platform_support import IS_MAC
import ctypes
from ctypes import wintypes as w
import threading
import time

LABEL = 'Ctrl+Alt+F9'
MODIFIERS = 0x0002 | 0x0001 | 0x4000  # CONTROL | ALT | NOREPEAT
KEY = 0x78  # F9
HOTKEY_ID = 0x5752
WM_HOTKEY, WM_QUIT = 0x0312, 0x0012


class GlobalHotkey:
    def __init__(self, events):
        self.events = events
        self.closed = threading.Event()
        self.ready = threading.Event()
        self.thread_id = None
        self.registered = False
        self.user = ctypes.WinDLL('user32', use_last_error=True)
        self.user.RegisterHotKey.argtypes = [w.HWND, ctypes.c_int, w.UINT, w.UINT]
        self.user.RegisterHotKey.restype = w.BOOL
        self.user.UnregisterHotKey.argtypes = [w.HWND, ctypes.c_int]
        self.user.UnregisterHotKey.restype = w.BOOL
        self.user.PeekMessageW.argtypes = [ctypes.POINTER(w.MSG), w.HWND, w.UINT, w.UINT, w.UINT]
        self.user.PeekMessageW.restype = w.BOOL
        self.user.GetMessageW.argtypes = [ctypes.POINTER(w.MSG), w.HWND, w.UINT, w.UINT]
        self.user.GetMessageW.restype = ctypes.c_int
        self.user.PostThreadMessageW.argtypes = [w.DWORD, w.UINT, w.WPARAM, w.LPARAM]
        self.user.PostThreadMessageW.restype = w.BOOL
        self.thread = threading.Thread(target=self._listen, name='Whisper shortcut', daemon=True)
        self.thread.start()

    def _listen(self):
        try:
            kernel = ctypes.WinDLL('kernel32', use_last_error=True)
            kernel.GetCurrentThreadId.restype = w.DWORD
            message = w.MSG()
            # Create the message queue before exposing its thread id to close().
            self.user.PeekMessageW(ctypes.byref(message), None, 0, 0, 0)
            self.thread_id = kernel.GetCurrentThreadId()
            if self.closed.is_set(): return
            self.registered = bool(self.user.RegisterHotKey(None, HOTKEY_ID, MODIFIERS, KEY))
            code = ctypes.get_last_error() if not self.registered else 0
            self.events.put({'kind': 'live_hotkey_status', 'available': self.registered, 'error_code': code})
            self.ready.set()
            if not self.registered: return
            while not self.closed.is_set():
                result = self.user.GetMessageW(ctypes.byref(message), None, 0, 0)
                if result == -1: raise ctypes.WinError(ctypes.get_last_error())
                if result == 0: break
                if message.message == WM_HOTKEY and message.wParam == HOTKEY_ID:
                    self.events.put({'kind': 'live_hotkey', 'posted': time.monotonic()})
        except Exception as exc:
            if not self.closed.is_set():
                self.events.put({'kind': 'live_hotkey_status', 'available': False, 'error': str(exc)})
        finally:
            if self.registered:
                self.user.UnregisterHotKey(None, HOTKEY_ID)
                self.registered = False
            self.ready.set()

    def close(self):
        if self.closed.is_set(): return
        self.closed.set()
        if self.thread_id and self.thread.is_alive():
            self.user.PostThreadMessageW(self.thread_id, WM_QUIT, 0, 0)
        if threading.current_thread() is not self.thread:
            self.thread.join(timeout=1)


if IS_MAC:
    from mac_native import GlobalHotkey
    LABEL = 'Control+Option+F9'
