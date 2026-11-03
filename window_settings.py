"""Remember normal window bounds and maximization, independently of captions."""
import ctypes
from ctypes import wintypes as w
import json
import os
from pathlib import Path
import tkinter as tk

from overlay_preferences import display_areas, restored_position, preferences

KEYS = {"main", "realtime", "comparison", "video_preview", "speaker_names", "speaker_memory", "voice_review",
        "context_review", "dictionary"}


def settings_path():
    from user_data import data_path
    return data_path("window_settings.json", "WHISPER_WINDOW_SETTINGS")


def validated(data):
    if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("windows"), dict):
        raise ValueError("ウィンドウの設定形式が正しくありません。")
    result = {}
    for key, item in data["windows"].items():
        if key not in KEYS or not isinstance(item, dict):
            raise ValueError("ウィンドウの設定形式が正しくありません。")
        entry = preferences({"positions": {"horizontal:center": item}})["positions"]["horizontal:center"]
        if type(item.get("maximized")) is not bool:
            raise ValueError("ウィンドウの最大化設定が正しくありません。")
        dpi = item.get("dpi", 96)
        if type(dpi) is not int or not 48 <= dpi <= 768:
            raise ValueError("ウィンドウの表示倍率が正しくありません。")
        entry.update(maximized=item["maximized"], dpi=dpi)
        result[key] = entry
    return {"version": 1, "windows": result}


def load_settings():
    try:
        return validated(json.loads(settings_path().read_text("utf-8-sig")))
    except FileNotFoundError:
        return {"version": 1, "windows": {}}


def save_settings(data):
    from context_ai import write_json
    write_json(settings_path(), validated(data))


class Placement(ctypes.Structure):
    _fields_ = [("length", w.UINT), ("flags", w.UINT), ("show", w.UINT),
                ("minimum", w.POINT), ("maximum", w.POINT), ("normal", w.RECT)]


class MonitorInfo(ctypes.Structure):
    _fields_ = [("size", w.DWORD), ("monitor", w.RECT), ("work", w.RECT),
                ("flags", w.DWORD), ("device", w.WCHAR * 32)]


def native(name, result, *args):
    # Local prototypes avoid interfering with the recorder and caption window.
    return ctypes.WINFUNCTYPE(result, *args)((name, ctypes.windll.user32))


def handle(window):
    return native("GetParent", w.HWND, w.HWND)(window.winfo_id()) or window.winfo_id()


def monitor_info(hwnd):
    monitor = native("MonitorFromWindow", w.HMONITOR, w.HWND, w.DWORD)(hwnd, 2)
    info = MonitorInfo();info.size = ctypes.sizeof(info)
    if not native("GetMonitorInfoW", w.BOOL, w.HMONITOR, ctypes.POINTER(MonitorInfo))(monitor, ctypes.byref(info)):
        raise OSError("画面の配置を取得できませんでした。")
    return info


def dpi_for(hwnd):
    return native("GetDpiForWindow", w.UINT, w.HWND)(hwnd) or 96


def placement(window):
    value = Placement();value.length = ctypes.sizeof(value)
    if not native("GetWindowPlacement", w.BOOL, w.HWND, ctypes.POINTER(Placement))(handle(window), ctypes.byref(value)):
        raise OSError("ウィンドウの位置を取得できませんでした。")
    return value


def snapshot(window, previous=None):
    value = placement(window)
    # Hiding/minimizing must not replace the last visible state or normal bounds.
    if window.state() in {"withdrawn", "iconic"} and previous:
        return previous
    hwnd = handle(window);info = monitor_info(hwnd);rect = value.normal
    # WINDOWPLACEMENT uses workspace coordinates for ordinary top-level windows.
    x = rect.left + info.work.left - info.monitor.left
    y = rect.top + info.work.top - info.monitor.top
    return {"x": x, "y": y, "size": [rect.right-rect.left, rect.bottom-rect.top],
            "device": info.device, "rect": [info.work.left, info.work.top, info.work.right, info.work.bottom],
            "dpi": dpi_for(hwnd), "maximized": value.show == 3}


def fit(saved, areas, minimum, dpi):
    area = next((item for item in areas if item["device"] == saved["device"]),
                next((item for item in areas if item.get("primary")), areas[0]))
    l, t, r, b = area["rect"]
    scale = dpi / saved["dpi"]
    width, height = [min(limit, max(low, round(size*scale)))
                     for size, low, limit in zip(saved["size"], minimum, (r-l, b-t))]
    x, y = restored_position(saved, width, height, area)
    return x, y, width, height, area


def restore(window, saved):
    hwnd = handle(window)
    # Derive frame margins in the same DPI context as Tk; saved size includes them.
    outer = w.RECT();client = w.RECT()
    native("GetWindowRect", w.BOOL, w.HWND, ctypes.POINTER(w.RECT))(hwnd, ctypes.byref(outer))
    native("GetClientRect", w.BOOL, w.HWND, ctypes.POINTER(w.RECT))(hwnd, ctypes.byref(client))
    frame = (max(0, outer.right-outer.left-client.right), max(0, outer.bottom-outer.top-client.bottom))
    minimum = tuple(n+border for n, border in zip(window.minsize(), frame))
    x, y, width, height, area = fit(saved, display_areas(window), minimum, dpi_for(hwnd))
    window.minsize(*[min(n, max(1, size-border)) for n, size, border in zip(window.minsize(), (width, height), frame)])
    # Let Tk own the client size, then place the outer frame using absolute coords.
    # A negative Tk geometry offset would anchor to the opposite screen edge.
    window.geometry(f"{max(1, width-frame[0])}x{max(1, height-frame[1])}+0+0")
    window.update_idletasks()
    native("SetWindowPos", w.BOOL, w.HWND, w.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, w.UINT)(
        hwnd, None, x, y, width, height, 0x0004 | 0x0010)
    window.deiconify()
    if saved["maximized"]:window.state("zoomed")


class WindowSettings:
    """Capture changes in memory immediately; debounce disk writes and flush on close."""
    def __init__(self, root, on_error=None):
        self.root = root;self.on_error = on_error
        self.windows = {};self.timer = None;self.closed = False;self.restoring = False
        self.dirty = False
        try:self.data = load_settings()
        except (OSError, ValueError) as exc:
            self.data = {"version": 1, "windows": {}}
            self.report(f"ウィンドウ設定を読み込めないため、標準の位置で開きます：{exc}")
        root.bind("<Destroy>", self.destroyed, add="+")

    def report(self, message):
        if self.on_error:self.on_error(message)

    def attach(self, window, key):
        self.restoring = True
        try:
            window.update_idletasks()
            saved = self.data["windows"].get(key) or snapshot(window)
            restore(window, saved)
            window.update_idletasks()
        except (OSError, tk.TclError, ValueError) as exc:
            window.deiconify();self.report(f"ウィンドウの位置を復元できませんでした：{exc}")
        finally:self.restoring = False
        self.windows[key] = window
        for event in ("<Configure>", "<Map>", "<Unmap>"):
            window.bind(event, lambda e, k=key, win=window: self.changed(k) if e.widget is win else None, add="+")
        window.bind("<Destroy>", lambda e, k=key, win=window: self.removed(k, win) if e.widget is win else None, add="+")
        self.changed(key)

    def changed(self, key):
        if self.closed or self.restoring:return
        window = self.windows.get(key)
        if window is None:return
        try:
            if window.state() in {"withdrawn", "iconic"}:return
            entry = snapshot(window, self.data["windows"].get(key))
            validated({"version": 1, "windows": {key: entry}})
        except (OSError, tk.TclError, ValueError):return
        if self.data["windows"].get(key) != entry:
            self.data["windows"][key] = entry;self.dirty = True
        if self.dirty:
            self.cancel_timer();self.timer = self.root.after(500, self.flush)

    def cancel_timer(self):
        if self.timer is not None:
            try:self.root.after_cancel(self.timer)
            except tk.TclError:pass
            self.timer = None

    def flush(self):
        self.cancel_timer()
        if not self.dirty:return
        try:
            save_settings(self.data);self.dirty = False
        except (OSError, ValueError) as exc:
            self.report(f"ウィンドウの位置・大きさを保存できません：{exc}")

    def flush_all(self):
        for key in list(self.windows):self.changed(key)
        self.flush()

    def remember(self, key, window):
        if self.windows.get(key) is window:
            self.changed(key);self.flush()

    def show(self, key, window):
        if self.windows.get(key) is window and window.state() in {"withdrawn", "iconic"}:
            saved = self.data["windows"].get(key)
            self.restoring = True
            try:
                # Tk deiconify alone loses maximization after withdraw.
                if window.state() == "iconic":
                    window.deiconify();window.update_idletasks()
                if saved:restore(window, saved)
                else:window.deiconify()
                window.update_idletasks()
            except (OSError, tk.TclError, ValueError) as exc:
                window.deiconify();self.report(f"ウィンドウの位置を復元できませんでした：{exc}")
            finally:self.restoring = False
            self.changed(key)
        window.lift()

    def hide(self, key):
        self.changed(key);self.flush()
        self.windows[key].withdraw()

    def removed(self, key, window):
        if self.windows.get(key) is window:self.windows.pop(key, None)
        if not self.closed:self.flush()

    def destroyed(self, event):
        if event.widget is self.root:
            self.closed = True;self.flush();self.cancel_timer()
