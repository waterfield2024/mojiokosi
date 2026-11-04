"""Movable two-line captions above other Windows applications."""
from __future__ import annotations
from collections import deque
import ctypes
import math
import time
import tkinter as tk
from tkinter import font as tkfont
from speakers import speaker_name

def wrap_pages(text, measure, width):
    lines, line = [], ""
    for char in " ".join(text.split()):
        if line and measure(line + char) > width:
            lines.append(line); line = char
        else:
            line += char
    if line:
        lines.append(line)
    return ["\n".join(lines[i:i + 2]) for i in range(0, len(lines), 2)]

class SubtitleOverlay:
    KEY = "#010203"
    def __init__(self, root, font_size=32, *, extent=None, direction="horizontal",
                 vertical_position="right", positions=None, on_change=None, visible=True, locked=False):
        from overlay_preferences import preferences
        saved = preferences({"font_size": font_size, "extent": extent, "positions": positions or {}})
        if direction not in {"horizontal", "vertical"} or vertical_position not in {"left", "right"}:
            raise ValueError("字幕の向き・位置が正しくありません。")
        self.root = root
        self.window = tk.Toplevel(root)
        self.window.withdraw()
        self.window.title("Whisper リアルタイム字幕")
        self.window.overrideredirect(True)
        self.window.configure(bg=self.KEY)
        self.window.attributes("-topmost", True)
        self.window.attributes("-transparentcolor", self.KEY)
        self.canvas = tk.Canvas(self.window, bg=self.KEY, highlightthickness=0)
        self.canvas.pack(fill="both", expand=True)
        self.font = tkfont.Font(family="Yu Gothic UI", size=font_size, weight="bold")
        self.direction = direction
        self.vertical_position = vertical_position
        self.width = extent or min(1100, max(480, root.winfo_screenwidth() - 120))
        self.extent = self.width
        self.positions = saved["positions"]
        self.on_change = on_change
        self.dragging = False
        self.vertical_image = None
        self.vertical_key = None
        self.height = self.font.metrics("linespace") * 2 + 28
        self.pages = deque()
        self.text = ""
        self.notice = ""
        self.notice_until = 0
        self.current_page = None
        self.partial_utterance = None
        self.speakers = {}
        self.until = 0
        self.shown_at = 0
        self.locked = bool(locked)
        self.closed = False
        self.visible = bool(visible)
        self.after_id = None
        self.canvas.bind("<ButtonPress-1>", self.drag_start)
        self.canvas.bind("<B1-Motion>", self.drag)
        self.canvas.bind("<ButtonRelease-1>", self.finish_drag)
        self.canvas.bind("<Configure>", lambda event: self.draw())
        self.resize_box()
        self.window.update_idletasks()
        from screen_capture import exclude_window
        self.capture_excluded = exclude_window(self.window)
        self.set_locked(self.locked)
        self.preview()
        if self.visible:self.window.deiconify()
        self.tick()

    def set_locked(self, locked):
        if locked:self.finish_drag()
        self.locked = bool(locked)
        self.window.update_idletasks()
        user = ctypes.windll.user32
        user.GetParent.argtypes = [ctypes.c_void_p]
        user.GetParent.restype = ctypes.c_void_p
        hwnd = user.GetParent(self.window.winfo_id()) or self.window.winfo_id()
        get_style = user.GetWindowLongPtrW
        get_style.argtypes = [ctypes.c_void_p, ctypes.c_int]
        get_style.restype = ctypes.c_ssize_t
        set_style = user.SetWindowLongPtrW
        set_style.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t]
        style = get_style(hwnd, -20) | 0x80 | 0x08000000  # tool window; never steal focus
        style = style | 0x20 if self.locked else style & ~0x20
        set_style(hwnd, -20, style)
        self.canvas.configure(cursor="" if locked else "fleur")
        self.draw()

    def set_size(self, size):
        self.set_layout(int(size), self.extent)

    def set_width(self, width):
        self.set_layout(int(self.font.cget("size")), int(width))

    def set_layout(self, size, extent):
        from overlay_preferences import preferences
        preferences({"font_size": size, "extent": extent})
        self.finish_drag()
        self.remember_position()
        self.font.configure(size=size)
        self.extent = extent
        self.resize_box()
        self.reflow_named()
        self.draw()
        self.remember_position()
        self.notify_settings()

    def settings_snapshot(self):
        from overlay_preferences import preferences
        return preferences({"font_size": int(self.font.cget("size")), "extent": self.extent, "positions": self.positions})

    def notify_settings(self):
        if self.on_change:self.on_change(self.settings_snapshot())

    def remember_position(self):
        from overlay_preferences import display_areas, closest_area
        self.window.update_idletasks()
        x, y = self.window.winfo_x(), self.window.winfo_y()
        area = closest_area(x, y, self.width, self.height, display_areas(self.root))
        self.positions[self.position_key()] = {"x": x, "y": y, "device": area["device"],
                                               "rect": list(area["rect"]), "size": [self.width, self.height]}

    def move_to(self, x, y):
        """Absolute desktop coordinates, including displays left/above the primary.

        Tk's negative geometry offsets mean distance from the opposite edge.
        SetWindowPos avoids confusing those offsets with negative desktop pixels.
        """
        self.window.update_idletasks()
        user = ctypes.windll.user32
        user.GetParent.argtypes = [ctypes.c_void_p];user.GetParent.restype = ctypes.c_void_p
        hwnd = user.GetParent(self.window.winfo_id()) or self.window.winfo_id()
        user.SetWindowPos.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int,
                                      ctypes.c_int, ctypes.c_int, ctypes.c_uint]
        if not user.SetWindowPos(hwnd, None, int(x), int(y), 0, 0, 0x15):
            raise OSError("字幕の表示位置を変更できませんでした。")
        self.window.update_idletasks()

    def vertical_size(self):
        return max(8, round(abs(self.font.cget("size"))*self.root.winfo_fpixels("1p")))

    def resize_box(self):
        from overlay_preferences import display_areas, restored_position, clamp_position
        areas = display_areas(self.root)
        saved = self.positions.get(self.position_key())
        area = next((a for a in areas if saved and a["device"] == saved["device"]), areas[0])
        left, top, right, bottom = area["rect"]
        if self.direction == "vertical":
            from vertical_subtitles import box_width
            self.width = min(box_width(self.vertical_size()), right-left)
            self.height = min(max(160, min(self.extent, bottom-top-120)), bottom-top)
        else:
            self.width = min(self.extent, right-left)
            self.height = min(self.font.metrics("linespace")*2+28, bottom-top)
        self.window.geometry(f"{self.width}x{self.height}")
        if saved:
            x, y = restored_position(saved, self.width, self.height, area)
        elif self.direction == "vertical":
            x = left+60 if self.vertical_position == "left" else right-self.width-60
            x, y = clamp_position(x, top+(bottom-top-self.height)//2, self.width, self.height, area)
        else:
            x, y = clamp_position(left+(right-left-self.width)//2, bottom-self.height-90, self.width, self.height, area)
        self.move_to(x, y)

    def set_direction(self, direction):
        if direction not in {"horizontal", "vertical"}:
            raise ValueError("字幕の向きが正しくありません。")
        if direction == self.direction:
            return
        if direction == "vertical":
            from vertical_subtitles import font_at
            font_at(self.vertical_size())
        self.finish_drag()
        self.remember_position()
        self.direction = direction
        self.resize_box()
        self.reflow_named()
        self.draw()
        self.notify_settings()

    def position_key(self):
        return self.direction+":"+(self.vertical_position if self.direction == "vertical" else "center")

    def set_vertical_position(self, position):
        if position not in {"left", "right"}:
            raise ValueError("字幕の位置が正しくありません。")
        if position == self.vertical_position:
            return
        self.finish_drag()
        self.remember_position()
        self.vertical_position = position
        if self.direction == "vertical":
            self.resize_box()
        self.notify_settings()

    def drag_start(self, event):
        if not self.locked:
            self.origin = (event.x_root, event.y_root, self.window.winfo_x(), self.window.winfo_y())

    def drag(self, event):
        if not self.locked and hasattr(self, "origin"):
            x, y, wx, wy = self.origin
            self.dragging = True
            self.move_to(wx+event.x_root-x, wy+event.y_root-y)

    def finish_drag(self, event=None):
        if not self.dragging:return
        self.dragging = False
        if hasattr(self, "origin"):del self.origin
        self.remember_position()
        self.resize_box()
        self.remember_position()
        self.notify_settings()

    def preview(self):
        self.pages.clear()
        self.current_page = None
        self.text = "ここに字幕を表示します\nドラッグして位置を調整できます"
        self.until = float("inf")
        self.draw()

    def push(self, text):
        pages = self.caption_pages(text)
        if not pages:
            return
        for page in pages:
            self.pages.append(page)
        # Preserve all text in the transcript; keep the overlay close to live playback.
        while len(self.pages) > 5:
            self.pages.popleft()
        if self.until == float("inf") or not self.text:
            self.until = 0

    def set_speakers(self, names):
        self.speakers = dict(names)
        self.draw()

    def named_lines(self, text):
        if self.direction == "vertical":
            from vertical_subtitles import wrap_columns
            return wrap_columns(text, self.vertical_size(), self.height-24)
        return [line for page in wrap_pages(text, self.font.measure, self.width - 32)
                for line in page.splitlines()]

    def caption_pages(self, text):
        if self.direction == "vertical":
            from vertical_subtitles import pages
            return pages(text, self.vertical_size(), self.height-24)
        return wrap_pages(text, self.font.measure, self.width-32)

    def push_segment(self, row):
        self.pages.extend(self.segment_pages(row["text"], {"speaker": row.get("speaker", ""),
            "review": bool(row.get("speaker_review")), "id": row.get("segment_id") or row.get("id")}))
        while len(self.pages) > 5:
            self.pages.popleft()
        if self.until == float("inf") or not self.text:
            self.until = 0

    def segment_pages(self, text, metadata):
        full = " ".join(text.split())
        lines = self.named_lines(full) if metadata.get("speaker") else self.caption_pages(full)
        pages, offset = [], 0
        for line in lines:
            pages.append({**metadata, "text": line, "full_text": full, "offset": offset})
            offset += len(line.replace("\n", ""))
        return pages

    def update_text(self, segment_id, text):
        """Patch a visible/pending caption in place; never replay an expired caption."""
        current = self.current_page if self.current_page and self.current_page.get("id") == segment_id else None
        pending = list(self.pages)
        match = current or next((p for p in pending if isinstance(p, dict) and p.get("id") == segment_id), None)
        if match is None:
            return False
        pages = self.segment_pages(text, match)
        if not pages:
            return False
        offset = match.get("offset", 0)
        index = next((i for i, p in enumerate(pages)
                      if p["offset"] + len(p["text"].replace("\n", "")) > offset), len(pages) - 1)
        pages = pages[index:]
        rest = []
        inserted = False
        for page in pending:
            if isinstance(page, dict) and page.get("id") == segment_id:
                if not current and not inserted:
                    rest.extend(pages); inserted = True
            else:
                rest.append(page)
        if current:
            self.current_page = pages[0]
            rest = pages[1:] + rest
        self.pages = deque(rest)
        self.draw()
        return True

    def push_segments(self, rows):
        """Replace stale pending captions, keeping the complete text in the table."""
        if not rows:
            return
        self.pages.clear()
        for row in rows:
            self.push_segment(row)
        # Do not wait through the old caption's reading time after new speech arrives.
        # A short minimum prevents rapid multi-speaker events from flickering.
        now = time.monotonic()
        self.until = min(self.until, max(now, self.shown_at + .75))
        self.advance(now)

    def clear_partial(self, utterance=None):
        if self.partial_utterance is not None and (utterance is None or utterance == self.partial_utterance):
            self.clear()

    def show_partial(self, utterance, rows):
        if not rows:
            self.clear_partial(utterance)
            return
        self.show_utterance_tail(rows, seconds=10)
        self.partial_utterance = utterance

    def commit_utterance(self, utterance, rows):
        if self.partial_utterance == utterance:
            self.clear_partial(utterance)
            # The beginning was already shown while speaking. Update the final
            # visible tail instead of replaying the entire long utterance.
            self.show_utterance_tail(rows)
        else:
            self.push_segments(rows)

    def show_utterance_tail(self, rows, *, seconds=None):
        pages = []
        for row in rows:
            pages.extend(self.segment_pages(row['text'], {'speaker':row.get('speaker',''),
                         'review':bool(row.get('speaker_review')), 'id':row.get('segment_id') or row.get('id')}))
        if not pages:
            return
        self.pages.clear()
        self.current_page = pages[-1]
        self.shown_at = time.monotonic()
        self.until = self.shown_at + (seconds if seconds is not None else min(6, max(1.5, len(pages[-1]['text'])/10)))
        self.draw()

    def update_assignment(self, segment_id, speaker):
        for page in ([self.current_page] if self.current_page else []) + list(self.pages):
            if isinstance(page, dict) and page.get("id") == segment_id:
                page["speaker"] = speaker
                page["review"] = False
        self.reflow_named()
        self.draw()

    def page_text(self, page):
        if not page.get("speaker"):
            return page["text"]
        name = speaker_name(page["speaker"], self.speakers)
        if page.get("review") and page["speaker"] != "unknown":
            name += " ？"
        if self.direction == "vertical":
            from vertical_subtitles import fit_name
            name = fit_name(name, max(8, round(self.vertical_size()*.85)), self.height-24)
        elif self.font.measure(name) > self.width - 32:
            while name and self.font.measure(name + "…") > self.width - 32:
                name = name[:-1]
            name += "…"
        return name + "\n" + page["text"]

    def reflow_named(self):
        pending = ([self.current_page] if self.current_page else []) + list(self.pages)
        seen = set()
        for page in pending:
            if isinstance(page, dict) and page.get("id") not in seen:
                seen.add(page.get("id"))
                self.update_text(page.get("id"), page.get("full_text", page["text"]))

    def clear(self):
        self.pages.clear()
        self.partial_utterance = None
        self.current_page = None
        self.text = ""
        self.until = 0
        self.draw()

    def notify(self, text, seconds=3):
        self.notice = text
        self.notice_until = time.monotonic() + seconds
        self.draw()

    def draw(self):
        if self.closed:
            return
        self.canvas.delete("all")
        if not self.locked:
            self.canvas.create_rectangle(2, 2, self.width - 2, self.height - 2,
                                         outline="#699bdf", width=2, fill="#182232")
        # Re-wrap on resize, and never render more than two lines.
        notice = self.notice if time.monotonic() < self.notice_until else ""
        if notice:
            pages = self.caption_pages(notice)
            text = pages[0] if pages else ""
        elif self.current_page:
            text = self.page_text(self.current_page)
            self.text = text
        else:
            pages = self.caption_pages(self.text)
            text = pages[0] if pages else ""
        if not text:
            return
        if self.direction == "vertical":
            from PIL import ImageTk
            from vertical_subtitles import render_columns
            named = bool(not notice and self.current_page and self.current_page.get("speaker"))
            key = (text, self.vertical_size(), self.width, self.height, named)
            if key != self.vertical_key:
                bitmap = render_columns(text.split("\n"), self.vertical_size(), self.width, self.height, named)
                self.vertical_image = ImageTk.PhotoImage(bitmap, master=self.window)
                self.vertical_key = key
            self.canvas.create_image(0, 0, image=self.vertical_image, anchor="nw")
            return
        x, y = self.width / 2, self.height / 2
        stroke = max(2, round(abs(self.font.cget("size")) / 14))
        for dx, dy in [(math.cos(i * math.pi / 4) * stroke, math.sin(i * math.pi / 4) * stroke) for i in range(8)]:
            self.canvas.create_text(x + dx, y + dy, text=text, fill="black", font=self.font,
                                    justify="center", anchor="center")
        self.canvas.create_text(x, y, text=text, fill="white", font=self.font, justify="center", anchor="center")

    def advance(self, now):
        if now >= self.until:
            if self.pages:
                page = self.pages.popleft()
                self.current_page = page if isinstance(page, dict) else None
                self.text = self.page_text(page) if self.current_page else page
                self.shown_at = now
                self.until = now + min(6, max(1.5, len(page["text"] if self.current_page else self.text) / 10))
                self.draw()
            elif self.text:
                self.current_page = None
                self.text = ""; self.draw()
    def tick(self):
        if self.closed:
            return
        now = time.monotonic()
        if self.notice and now >= self.notice_until:
            self.notice = ""
            self.draw()
        self.advance(now)
        self.after_id = self.root.after(30, self.tick)

    def show(self, visible=True):
        self.visible = bool(visible)
        if visible:
            self.resize_box()
            self.window.deiconify()
            self.window.attributes("-topmost", True)
            self.set_locked(self.locked)
        else:
            self.window.withdraw()

    def close(self):
        if not self.closed:
            self.finish_drag()
            self.closed = True
            if self.after_id:
                self.root.after_cancel(self.after_id)
            self.window.destroy()
