"""Small, validated overlay preferences and placement in Tk's desktop coordinates."""
from platform_support import IS_MAC
import ctypes
from ctypes import wintypes as w

POSITION_KEYS = {"horizontal:center", "vertical:left", "vertical:right"}


def preferences(data=None):
    if data is None:data = {}
    if not isinstance(data, dict):raise ValueError("字幕の表示設定が正しくありません。")
    font_size, extent = data.get("font_size", 32), data.get("extent")
    if type(font_size) is not int or not 8 <= font_size <= 96:
        raise ValueError("字幕の文字サイズが正しくありません。")
    if extent is not None and (type(extent) is not int or not 160 <= extent <= 4000):
        raise ValueError("字幕の長さが正しくありません。")
    positions = data.get("positions", {})
    if not isinstance(positions, dict):raise ValueError("字幕の位置設定が正しくありません。")
    saved = {}
    for key, item in positions.items():
        if key not in POSITION_KEYS or not isinstance(item, dict):raise ValueError("字幕の位置設定が正しくありません。")
        rect, size = item.get("rect"), item.get("size")
        if (not isinstance(rect, (list, tuple)) or len(rect) != 4
                or any(type(n) is not int or abs(n) > 200000 for n in rect)
                or rect[2] <= rect[0] or rect[3] <= rect[1]):
            raise ValueError("字幕を表示する画面の設定が正しくありません。")
        if (not isinstance(size, (list, tuple)) or len(size) != 2
                or any(type(n) is not int or not 0 < n <= 10000 for n in size)):
            raise ValueError("字幕の表示範囲が正しくありません。")
        if any(type(item.get(k)) is not int or abs(item[k]) > 200000 for k in ("x", "y")):
            raise ValueError("字幕の座標が正しくありません。")
        device = item.get("device", "")
        if not isinstance(device, str) or len(device) > 256:raise ValueError("字幕を表示する画面の名前が正しくありません。")
        saved[key] = {"device": device, "rect": list(rect), "size": list(size), "x": item["x"], "y": item["y"]}
    return {"font_size": font_size, "extent": extent, "positions": saved}


def display_areas(root):
    """Use the GUI thread's DPI context, not the recorder's physical-pixel context."""
    if IS_MAC:
        from mac_native import displays
        return displays()
    user = ctypes.windll.user32
    class Info(ctypes.Structure):
        _fields_ = [("size", w.DWORD), ("rect", w.RECT), ("work", w.RECT),
                    ("flags", w.DWORD), ("device", w.WCHAR * 32)]
    callback_type = ctypes.WINFUNCTYPE(w.BOOL, w.HMONITOR, w.HDC, ctypes.POINTER(w.RECT), w.LPARAM)
    # Use local function prototypes: the recorder has its own MonitorInfo type.
    get_info = ctypes.WINFUNCTYPE(w.BOOL, w.HMONITOR, ctypes.POINTER(Info))(("GetMonitorInfoW", user))
    enumerate_monitors = ctypes.WINFUNCTYPE(w.BOOL, w.HDC, ctypes.POINTER(w.RECT), callback_type, w.LPARAM)(("EnumDisplayMonitors", user))
    found = []
    def visit(handle, dc, rect, data):
        info = Info();info.size = ctypes.sizeof(info)
        if get_info(handle, ctypes.byref(info)):
            r = info.work
            found.append({"device": info.device, "rect": [r.left, r.top, r.right, r.bottom], "primary": bool(info.flags & 1)})
        return True
    if not enumerate_monitors(None, None, callback_type(visit), 0) or not found:
        return [{"device": "", "rect": [0, 0, root.winfo_screenwidth(), root.winfo_screenheight()], "primary": True}]
    return sorted(found, key=lambda item: (not item["primary"], item["device"]))


def closest_area(x, y, width, height, areas):
    def score(area):
        l, t, r, b = area["rect"]
        overlap = max(0, min(x+width, r)-max(x, l)) * max(0, min(y+height, b)-max(y, t))
        cx, cy = x+width/2, y+height/2
        distance = (cx-min(r, max(l, cx)))**2 + (cy-min(b, max(t, cy)))**2
        return overlap, -distance
    return max(areas, key=score)


def clamp_position(x, y, width, height, area):
    l, t, r, b = area["rect"]
    return (max(l, min(round(x), max(l, r-width))), max(t, min(round(y), max(t, b-height))))


def restored_position(saved, width, height, area):
    old, new = saved["rect"], area["rect"]
    result = []
    for axis, extent in enumerate((width, height)):
        offset = saved[("x", "y")[axis]]-old[axis]
        old_span, new_span = old[axis+2]-old[axis], new[axis+2]-new[axis]
        if old_span != new_span:
            fraction = max(0, min(1, offset/max(1, old_span-saved["size"][axis])))
            offset = fraction*max(0, new_span-extent)
        result.append(round(new[axis]+offset))
    return clamp_position(*result, width, height, area)
