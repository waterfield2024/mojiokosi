"""Light/dark desktop themes, applied in place without rebuilding editor widgets."""
from platform_support import IS_MAC
import ctypes
import json
import os
from pathlib import Path
import tkinter as tk
from tkinter import ttk

FONT = "Yu Gothic UI"
THEME_OPTIONS = {("Macに合わせる" if IS_MAC else "Windowsに合わせる"): "system", "ライト": "light", "ダーク": "dark"}
PALETTES = {
    "light": dict(page="#f3f5fa", surface="#ffffff", ink="#20283f", muted="#64748b",
                  line="#dfe5ef", accent="#5256d8", primary="#5256d8", on_primary="#ffffff",
                  tint="#eef0ff", disabled="#f7f8fb", disabled_ink="#8b95a7",
                  pressed="#e8ebf6", hover="#f0f2f9", hover_line="#bcc5d9",
                  primary_disabled="#e5e7f3", primary_disabled_ink="#7c85a5",
                  primary_pressed="#383cb2", primary_hover="#4448c8", soft_hover="#e2e5ff",
                  selected="#e9ecff", selected_ink="#3035a4", heading="#f5f7fb",
                  heading_hover="#edf0f8", track="#e6eaf2", scroll="#cdd4e2", scroll_hover="#a9b3c7",
                  stripe="#f8f9fd", before="#ffe1e1", before_ink="#7d202c",
                  after="#d9f1df", after_ink="#205931",
                  diff_whisper="#ffe4bd", diff_whisper_ink="#713c08",
                  diff_granite="#dbeaff", diff_granite_ink="#16477e"),
    "dark": dict(page="#111622", surface="#1b2232", ink="#e7ecf5", muted="#a5b2c9",
                 line="#354158", accent="#b0adff", primary="#7068e8", on_primary="#ffffff",
                 tint="#2d3050", disabled="#202838", disabled_ink="#7f8ca5",
                 pressed="#363f57", hover="#2a3449", hover_line="#566582",
                 primary_disabled="#303651", primary_disabled_ink="#8e9ab4",
                 primary_pressed="#544cbe", primary_hover="#7d74f2", soft_hover="#3b3d65",
                 selected="#3b3e6b", selected_ink="#f1efff", heading="#242d40",
                 heading_hover="#303b52", track="#2c364b", scroll="#46546e", scroll_hover="#647595",
                 stripe="#20293b", before="#512e3c", before_ink="#ffc3ce",
                 after="#24483e", after_ink="#b7f0cd",
                 diff_whisper="#543d24", diff_whisper_ink="#ffdaa1",
                 diff_granite="#263e61", diff_granite_ink="#c2dcff"),
}


def settings_path():
    from user_data import data_path
    return data_path("appearance_settings.json", "WHISPER_APPEARANCE_SETTINGS")


def load_theme():
    try:
        data = json.loads(settings_path().read_text("utf-8-sig"))
        mode = data.get("theme") if isinstance(data, dict) else None
        return mode if isinstance(mode, str) and mode in THEME_OPTIONS.values() else "light"
    except (OSError, ValueError):
        return "light"


def save_theme(mode):
    if mode not in THEME_OPTIONS.values():
        raise ValueError("外観の設定が正しくありません。")
    from context_ai import write_json
    write_json(settings_path(), {"theme": mode})


def windows_theme():
    """Read the current user's app mode, including Windows' custom color mode."""
    if IS_MAC:
        from AppKit import NSUserDefaults
        return 'dark' if NSUserDefaults.standardUserDefaults().stringForKey_('AppleInterfaceStyle') == 'Dark' else 'light'
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER,
                            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize",
                            0, winreg.KEY_READ) as key:
            value, kind = winreg.QueryValueEx(key, "AppsUseLightTheme")
        if kind == winreg.REG_DWORD and value in (0, 1):
            return "light" if value else "dark"
    except (ImportError, OSError):
        pass
    return None


class ThemeController:
    """Keep the saved preference separate from the currently displayed palette."""
    POLL_MS = 1000

    def __init__(self, root, mode):
        self.root = root
        self.mode = None
        self._timer = None
        self._closed = False
        root.bind("<Destroy>", self._on_destroy, add="+")
        self.set_mode(mode)

    def set_mode(self, mode):
        if mode not in THEME_OPTIONS.values():
            raise ValueError("外観の設定が正しくありません。")
        self._cancel_timer()
        self.mode = mode
        self._update()

    def _update(self):
        self._timer = None
        if self._closed:
            return
        current = getattr(self.root, "_whisper_theme", None)
        # A missing/unreadable setting keeps the last palette; startup uses light.
        resolved = (windows_theme() or current or "light") if self.mode == "system" else self.mode
        if resolved != current:
            apply_theme(self.root, resolved)
        if self.mode == "system":
            self._timer = self.root.after(self.POLL_MS, self._update)

    def _cancel_timer(self):
        if self._timer is not None:
            try:
                self.root.after_cancel(self._timer)
            except tk.TclError:
                pass
            self._timer = None

    def _on_destroy(self, event):
        if event.widget is self.root:
            self._closed = True
            self._cancel_timer()


def palette(widget):
    return PALETTES[getattr(widget._root(), "_whisper_theme", "light")]


def _style_window(window):
    # The transparent subtitle overlay owns its colors independently of the app.
    if window.overrideredirect():
        return
    colors = palette(window)
    window.configure(background=colors["page"] if isinstance(window, tk.Tk) else colors["surface"])
    if os.name == "nt" and window.winfo_ismapped():
        try:
            parent = ctypes.windll.user32.GetParent
            parent.argtypes = [ctypes.c_void_p]
            parent.restype = ctypes.c_void_p
            hwnd = parent(window.winfo_id()) or window.winfo_id()
            dark = ctypes.c_int(window._root()._whisper_theme == "dark")
            ctypes.windll.dwmapi.DwmSetWindowAttribute(ctypes.c_void_p(hwnd), 20,
                                                      ctypes.byref(dark), ctypes.sizeof(dark))
        except (OSError, AttributeError, ValueError):
            pass  # Older Windows versions keep their native title bar.


def _map_window(event):
    if isinstance(event.widget, (tk.Tk, tk.Toplevel)):
        _style_window(event.widget)


def _refresh_widgets(widget):
    colors = palette(widget)
    if isinstance(widget, (tk.Tk, tk.Toplevel)):
        _style_window(widget)
    role = getattr(widget, "_whisper_theme_role", None)
    if role == "text":
        style_text(widget)
        if hasattr(widget, '_whisper_refresh_tags'):
            widget._whisper_refresh_tags()
    elif role == "brand":
        widget.configure(background=colors["page"])
        widget.itemconfigure("wave", fill=colors["accent"])
    if isinstance(widget, TranscriptTree):
        widget.tag_configure("stripe", background=colors["stripe"])
    for child in widget.winfo_children():
        _refresh_widgets(child)


def _refresh_listboxes(interpreter, widget, colors):
    # Combobox popdowns are Tcl widgets, absent from Tkinter's widget registry.
    if interpreter.call("winfo", "class", widget) == "Listbox":
        interpreter.call(widget, "configure", "-background", colors["surface"],
                         "-foreground", colors["ink"], "-selectbackground", colors["selected"],
                         "-selectforeground", colors["selected_ink"], "-highlightbackground", colors["line"])
    for child in interpreter.splitlist(interpreter.call("winfo", "children", widget)):
        _refresh_listboxes(interpreter, child, colors)


def apply_theme(root, mode="light"):
    if mode not in PALETTES:
        raise ValueError("外観の設定が正しくありません。")
    root._whisper_theme = mode
    c = PALETTES[mode]
    for pattern, key in (("*Toplevel.background", "surface"), ("*Text.background", "surface"),
                         ("*Text.foreground", "ink"), ("*Text.selectBackground", "selected"),
                         ("*Text.selectForeground", "selected_ink"), ("*Listbox.background", "surface"),
                         ("*Listbox.foreground", "ink"), ("*Listbox.selectBackground", "selected"),
                         ("*Listbox.selectForeground", "selected_ink"), ("*Listbox.highlightBackground", "line")):
        root.option_add(pattern, c[key])
    root.option_add("*Listbox.font", (FONT, 10))
    style = ttk.Style(root)
    if style.theme_use() != "clam":
        style.theme_use("clam")
    style.configure(".", font=(FONT, 10), background=c["surface"], foreground=c["ink"],
                    bordercolor=c["line"], lightcolor=c["surface"], darkcolor=c["surface"], focuscolor=c["accent"])
    style.configure("TFrame", background=c["surface"])
    style.configure("App.TFrame", background=c["page"])
    style.configure("Card.TFrame", background=c["surface"], bordercolor=c["line"], borderwidth=1, relief="solid")
    style.configure("TLabel", background=c["surface"], foreground=c["ink"])
    style.configure("Title.TLabel", background=c["page"], foreground=c["ink"], font=(FONT, 23, "bold"))
    style.configure("App.Sub.TLabel", background=c["page"], foreground=c["muted"], font=(FONT, 9))
    style.configure("Sub.TLabel", foreground=c["muted"], font=(FONT, 9))
    style.configure("Empty.TLabel", foreground=c["muted"], font=(FONT, 11, "bold"))
    style.configure("Section.TLabel", font=(FONT, 11, "bold"))
    for side in ("whisper", "granite"):
        style.configure(f"Diff.{side}.TLabel", background=c["diff_"+side], foreground=c["diff_"+side+"_ink"], padding=(6, 2))
    style.configure("Preview.Title.TLabel", background=c["surface"], foreground=c["ink"], font=(FONT, 14, "bold"))
    style.configure("Preview.Horizontal.TScale", background=c["primary"], troughcolor=c["track"],
                    bordercolor=c["line"], lightcolor=c["primary"], darkcolor=c["primary"], sliderlength=24)
    style.map("Preview.Horizontal.TScale", background=[("disabled",c["disabled"]),("active",c["primary_hover"])])
    style.configure("Badge.TLabel", background=c["tint"], foreground=c["accent"], font=("Segoe UI", 10, "bold"), padding=(7, 3))
    style.configure("TLabelframe", background=c["surface"], bordercolor=c["line"], borderwidth=1, relief="solid")
    style.configure("TLabelframe.Label", background=c["surface"], foreground=c["muted"], font=(FONT, 9, "bold"))
    style.configure("TButton", background=c["surface"], foreground=c["ink"], bordercolor=c["line"],
                    lightcolor=c["surface"], darkcolor=c["surface"], borderwidth=1, relief="flat",
                    padding=(11, 6), focusthickness=1, focuscolor=c["accent"])
    style.map("TButton", background=[("disabled", c["disabled"]), ("pressed", c["pressed"]), ("active", c["hover"])],
              foreground=[("disabled", c["disabled_ink"])], bordercolor=[("focus", c["accent"]), ("active", c["hover_line"])])
    style.configure("Accent.TButton", background=c["primary"], foreground=c["on_primary"], bordercolor=c["primary"],
                    lightcolor=c["primary"], darkcolor=c["primary"], font=(FONT, 10, "bold"), padding=(15, 7))
    style.map("Accent.TButton", background=[("disabled", c["primary_disabled"]), ("pressed", c["primary_pressed"]), ("active", c["primary_hover"])],
              foreground=[("disabled", c["primary_disabled_ink"]), ("!disabled", c["on_primary"])],
              bordercolor=[("disabled", c["primary_disabled"]), ("!disabled", c["primary"])],
              lightcolor=[("disabled", c["primary_disabled"]), ("!disabled", c["primary"])],
              darkcolor=[("disabled", c["primary_disabled"]), ("!disabled", c["primary"])])
    style.configure("Soft.TButton", background=c["tint"], foreground=c["accent"], bordercolor=c["tint"])
    style.map("Soft.TButton", background=[("disabled", c["disabled"]), ("active", c["soft_hover"])],
              foreground=[("disabled", c["disabled_ink"]), ("!disabled", c["accent"])])
    style.configure("TEntry", fieldbackground=c["surface"], foreground=c["ink"], padding=(8, 6), bordercolor=c["line"],
                    lightcolor=c["surface"], darkcolor=c["surface"], insertcolor=c["ink"],
                    selectbackground=c["selected"], selectforeground=c["selected_ink"])
    style.map("TEntry", bordercolor=[("focus", c["accent"])], fieldbackground=[("disabled", c["disabled"])],
              foreground=[("disabled", c["disabled_ink"])])
    style.configure("TSpinbox", fieldbackground=c["surface"], foreground=c["ink"], background=c["surface"],
                    padding=(6,4), bordercolor=c["line"], lightcolor=c["surface"], darkcolor=c["surface"],
                    arrowcolor=c["ink"], insertcolor=c["ink"], selectbackground=c["selected"], selectforeground=c["selected_ink"])
    style.map("TSpinbox", bordercolor=[("focus",c["accent"])], fieldbackground=[("disabled",c["disabled"])],
              foreground=[("disabled",c["disabled_ink"])])
    style.configure("TCombobox", fieldbackground=c["surface"], background=c["surface"], foreground=c["ink"],
                    padding=(7, 5), bordercolor=c["line"], arrowcolor=c["muted"], arrowsize=12)
    style.map("TCombobox", fieldbackground=[("disabled", c["disabled"]), ("readonly", c["surface"])],
              foreground=[("disabled", c["disabled_ink"]), ("readonly", c["ink"])],
              selectbackground=[("readonly", c["surface"])], selectforeground=[("readonly", c["ink"])],
              bordercolor=[("focus", c["accent"])], background=[("active", c["hover"])])
    for control in ("TCheckbutton", "TRadiobutton"):
        style.configure(control, background=c["surface"], foreground=c["ink"], padding=(0, 3),
                        indicatorbackground=c["surface"], indicatorforeground=c["on_primary"],
                        upperbordercolor=c["line"], lowerbordercolor=c["line"])
        style.map(control, background=[("active", c["surface"])],
                  indicatorbackground=[("disabled", c["disabled"]), ("selected", c["primary"])],
                  indicatorforeground=[("disabled", c["disabled_ink"])],
                  foreground=[("disabled", c["disabled_ink"])])
    style.configure("TSeparator", background=c["line"])
    style.configure("Treeview", background=c["surface"], fieldbackground=c["surface"], foreground=c["ink"],
                    bordercolor=c["line"], borderwidth=0, rowheight=34, font=(FONT, 10))
    style.map("Treeview", background=[("selected", c["selected"])], foreground=[("selected", c["selected_ink"])])
    style.configure("Treeview.Heading", background=c["heading"], foreground=c["muted"],
                    bordercolor=c["heading"], relief="flat", padding=(8, 7), font=(FONT, 9, "bold"))
    style.map("Treeview.Heading", background=[("active", c["heading_hover"])])
    style.configure("Horizontal.TProgressbar", background=c["primary"], troughcolor=c["track"],
                    borderwidth=0, thickness=4, lightcolor=c["primary"], darkcolor=c["primary"])
    for orientation in ("Vertical", "Horizontal"):
        name = orientation + ".TScrollbar"
        style.configure(name, background=c["scroll"], troughcolor=c["heading"], borderwidth=0,
                        arrowsize=12, arrowcolor=c["muted"], lightcolor=c["scroll"], darkcolor=c["scroll"])
        style.map(name, background=[("active", c["scroll_hover"])])
    if not getattr(root, "_whisper_theme_bound", False):
        root.bind_all("<Map>", _map_window, add="+")
        root._whisper_theme_bound = True
    _refresh_widgets(root)
    _refresh_listboxes(root.tk, str(root), c)
    return style


def section_title(parent, number, title):
    row = ttk.Frame(parent)
    ttk.Label(row, text=number, style="Badge.TLabel").pack(side="left", padx=(0, 9))
    ttk.Label(row, text=title, style="Section.TLabel").pack(side="left")
    return row


def brand(parent):
    row = ttk.Frame(parent, style="App.TFrame")
    c = palette(parent)
    icon = tk.Canvas(row, width=36, height=36, background=c["page"], highlightthickness=0)
    icon._whisper_theme_role = "brand"
    icon.pack(side="left", padx=(0, 11))
    for x, height in ((4, 10), (11, 22), (18, 32), (25, 18), (32, 8)):
        icon.create_line(x, 18-height/2, x, 18+height/2, fill=c["accent"], width=4, capstyle="round", tags="wave")
    ttk.Label(row, text="Whisper", style="Title.TLabel").pack(side="left")
    return row


def style_text(widget):
    c = palette(widget)
    widget._whisper_theme_role = "text"
    widget.configure(background=c["surface"], foreground=c["ink"], insertbackground=c["accent"],
                     selectbackground=c["selected"], selectforeground=c["selected_ink"], relief="flat", borderwidth=0,
                     highlightthickness=1, highlightbackground=c["line"], highlightcolor=c["accent"],
                     padx=10, pady=6)
    if hasattr(widget, "_whisper_change_role"):
        style_change(widget, widget._whisper_change_role)


def style_change(widget, role):
    c = palette(widget)
    widget._whisper_change_role = role
    widget.tag_configure("change", background=c[role], foreground=c[role + "_ink"])


class TranscriptTree(ttk.Treeview):
    """An unobtrusive empty state and alternating rows, with normal Treeview APIs."""
    def __init__(self, master, **kwargs):
        super().__init__(master, **kwargs)
        self.tag_configure("stripe", background=palette(self)["stripe"])
        self._empty = None
        self.bind("<Configure>", lambda event: self._update_empty(), add="+")

    def show_empty(self, title, detail):
        self._empty = ttk.Frame(self)
        ttk.Label(self._empty, text=title, style="Empty.TLabel").pack()
        ttk.Label(self._empty, text=detail, style="Sub.TLabel").pack(pady=(7, 0))
        self._update_empty()

    def _update_empty(self):
        if self._empty is not None:
            if self.get_children() or self.winfo_height() < 100:
                self._empty.place_forget()
            else:
                self._empty.place(relx=.5, rely=.6, anchor="center")

    def insert(self, parent, index, iid=None, **kwargs):
        if len(self.get_children(parent)) % 2:
            kwargs["tags"] = tuple(kwargs.get("tags", ())) + ("stripe",)
        result = super().insert(parent, index, iid=iid, **kwargs)
        self._update_empty()
        return result

    def delete(self, *items):
        super().delete(*items)
        self._update_empty()
