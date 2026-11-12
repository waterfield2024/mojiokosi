"""Light/dark desktop themes, applied in place without rebuilding editor widgets."""
from platform_support import IS_MAC
import ctypes
import json
import os
from pathlib import Path
import tkinter as tk
from tkinter import ttk

FONT = "Hiragino Sans" if IS_MAC else "Yu Gothic UI"
THEME_OPTIONS = {("Macに合わせる" if IS_MAC else "Windowsに合わせる"): "system", "ライト": "light", "ダーク": "dark"}
PALETTES = {
    "light": dict(page="#eef2ef", surface="#ffffff", ink="#192f2c", muted="#506661",
                  line="#cdd9d3", accent="#076c56", primary="#086c56", on_primary="#ffffff",
                  tint="#dff2e8", disabled="#f1f4f2", disabled_ink="#687b73",
                  pressed="#dce8e1", hover="#eff6f1", hover_line="#92afa1",
                  primary_disabled="#e1eae4", primary_disabled_ink="#637a6c",
                  primary_pressed="#074a3c", primary_hover="#075b48", soft_hover="#c6e8d6",
                  selected="#d7eedf", selected_ink="#103e30", heading="#f2f6f3",
                  heading_hover="#e5eee8", track="#dbe5de", scroll="#c0cec5", scroll_hover="#93a69a",
                  mast="#183932", mast_ink="#f4fbf5", mast_muted="#c2d7cb", mark="#b7eed0",
                  mast_button="#294c43", mast_line="#507065", mast_hover="#365d51",
                  stripe="#f7faf8", before="#ffe1e1", before_ink="#7d202c",
                  after="#d9f1df", after_ink="#205931",
                  diff_whisper="#ffe4bd", diff_whisper_ink="#713c08",
                  diff_granite="#dbeaff", diff_granite_ink="#16477e"),
    "dark": dict(page="#101816", surface="#1c2824", ink="#edf5ef", muted="#b1c3b8",
                 line="#3c5046", accent="#98dfb9", primary="#94dfb6", on_primary="#102e22",
                 tint="#294438", disabled="#24312b", disabled_ink="#91a297",
                 pressed="#344c3e", hover="#2a3a31", hover_line="#658671",
                 primary_disabled="#314438", primary_disabled_ink="#a1b8a8",
                 primary_pressed="#badfc9", primary_hover="#aaedc7", soft_hover="#345641",
                 selected="#32533f", selected_ink="#e6f9eb", heading="#24342b",
                 heading_hover="#304537", track="#2d3f33", scroll="#42574a", scroll_hover="#637a6b",
                 mast="#132e26", mast_ink="#eff9f0", mast_muted="#b6cec0", mark="#a8e7bc",
                 mast_button="#254438", mast_line="#496354", mast_hover="#345645",
                 stripe="#202e27", before="#512e3c", before_ink="#ffc3ce",
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
    elif role == "brand":
        widget.configure(background=colors["mast"])
        widget.itemconfigure("tile", fill=colors["mark"])
        widget.itemconfigure("wave", fill=colors["mast"])
    if isinstance(widget, TranscriptTree):
        widget.tag_configure("stripe", background=colors["stripe"])
    if hasattr(widget, '_whisper_refresh_tags'):
        widget._whisper_refresh_tags()
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


def _rounded_controls(root, style, mode):
    """Nine-slice backgrounds keep native widget sizing, focus and key bindings."""
    from PIL import Image, ImageDraw, ImageTk
    c = PALETTES[mode]
    if not hasattr(root, '_whisper_tiles'):
        root._whisper_tiles = {}
    def tile(fill, edge, stroke=1, radius=7):
        key = (fill, edge, stroke, radius)
        if key not in root._whisper_tiles:
            # ttk tiles the centre. A broad solid area also paints large panels efficiently.
            size = radius * 2 + 66
            bitmap = Image.new('RGBA', (size * 3, size * 3))
            ImageDraw.Draw(bitmap).rounded_rectangle((0, 0, bitmap.width - 1, bitmap.height - 1),
                                                     radius * 3, fill, edge, width=stroke * 3)
            root._whisper_tiles[key] = ImageTk.PhotoImage(bitmap.resize((size, size), Image.Resampling.LANCZOS), master=root)
        return root._whisper_tiles[key]

    prefix = f'Whisper.{mode}.'
    controls = (
        ('TButton', 'surface', 'line', 'hover', 'pressed', 'accent'),
        ('Accent.TButton', 'primary', 'primary', 'primary_hover', 'primary_pressed', 'on_primary'),
        ('Soft.TButton', 'tint', 'tint', 'soft_hover', 'soft_hover', 'accent'),
        ('Mast.TButton', 'mast_button', 'mast_line', 'mast_hover', 'mast', 'mark'),
        ('Source.TRadiobutton', 'surface', 'line', 'hover', 'pressed', 'accent'),
    )
    for name, fill, edge, hover, pressed, focus in controls:
        element = prefix + name
        if element not in style.element_names():
            inactive = 'primary_disabled' if name == 'Accent.TButton' else 'disabled'
            states = [('disabled', tile(c[inactive], c[inactive])),
                      ('pressed', tile(c[pressed], c[edge]))]
            if name == 'Source.TRadiobutton':
                states.insert(0, ('disabled', 'selected', tile(c['primary_disabled'], c['muted'])))
                states.extend([('selected', 'focus', tile(c['tint'], c['accent'], 2)),
                               ('selected', tile(c['tint'], c['accent']))])
            states.extend([('focus', tile(c[fill], c[focus], 2)), ('active', tile(c[hover], c[edge]))])
            style.element_create(element, 'image', tile(c[fill], c[edge]), *states,
                                 width=16, height=16, border=8, padding=0, sticky='nswe')
        label = 'Radiobutton' if name == 'Source.TRadiobutton' else 'Button'
        style.layout(name, [(element, {'sticky': 'nswe', 'children': [
            (label + '.padding', {'sticky': 'nswe', 'children': [(label + '.label', {'sticky': 'nswe'})]})]})])
    card = prefix + 'card'
    if card not in style.element_names():
        style.element_create(card, 'image', tile(c['surface'], c['line'], radius=10),
                             width=22, height=22, border=11, padding=1, sticky='nswe')
    style.layout('Card.TFrame', [(card, {'sticky': 'nswe'})])


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
    style.configure("Mast.TFrame", background=c["mast"])
    style.configure("Mast.TLabel", background=c["mast"], foreground=c["mast_ink"], font=(FONT, 18, "bold"))
    style.configure("Mast.Sub.TLabel", background=c["mast"], foreground=c["mast_muted"], font=(FONT, 9))
    style.configure("Wordmark.TLabel", background=c["mast"], foreground=c["mast_ink"],
                    font=("Helvetica Neue" if IS_MAC else "Segoe UI", 24, "bold"))
    style.configure("Card.TFrame", background=c["page"])
    style.configure("TLabel", background=c["surface"], foreground=c["ink"])
    style.configure("Title.TLabel", background=c["page"], foreground=c["ink"], font=(FONT, 23, "bold"))
    style.configure("App.Sub.TLabel", background=c["page"], foreground=c["muted"], font=(FONT, 9))
    style.configure("Sub.TLabel", foreground=c["muted"], font=(FONT, 9))
    style.configure("Empty.TLabel", foreground=c["muted"], font=(FONT, 11, "bold"))
    style.configure("Section.TLabel", font=(FONT, 13, "bold"))
    for side in ("whisper", "granite"):
        style.configure(f"Diff.{side}.TLabel", background=c["diff_"+side], foreground=c["diff_"+side+"_ink"], padding=(6, 2))
    style.configure("Preview.Title.TLabel", background=c["surface"], foreground=c["ink"], font=(FONT, 14, "bold"))
    style.configure("Preview.Horizontal.TScale", background=c["primary"], troughcolor=c["track"],
                    bordercolor=c["line"], lightcolor=c["primary"], darkcolor=c["primary"], sliderlength=24)
    style.map("Preview.Horizontal.TScale", background=[("disabled",c["disabled"]),("active",c["primary_hover"])])
    style.configure("Step.TLabel", foreground=c["accent"], font=(FONT, 10, "bold"))
    style.configure("TLabelframe", background=c["surface"], bordercolor=c["line"], borderwidth=1, relief="solid")
    style.configure("TLabelframe.Label", background=c["surface"], foreground=c["muted"], font=(FONT, 9, "bold"))
    style.configure("TButton", background=c["surface"], foreground=c["ink"], padding=(11, 6))
    style.map("TButton", background=[("!disabled", c["surface"]), ("disabled", c["surface"])],
              foreground=[("disabled", c["disabled_ink"])])
    style.configure("Accent.TButton", foreground=c["on_primary"], font=(FONT, 10, "bold"), padding=(15, 7))
    style.map("Accent.TButton", foreground=[("disabled", c["primary_disabled_ink"]), ("!disabled", c["on_primary"])])
    style.configure("Soft.TButton", foreground=c["accent"])
    style.map("Soft.TButton", foreground=[("disabled", c["disabled_ink"]), ("!disabled", c["accent"])])
    style.configure("Mast.TButton", foreground=c["mast_ink"], padding=(12, 5))
    style.map("Mast.TButton", background=[("!disabled", c["mast"]), ("disabled", c["mast"])])
    for action_style in ("Action.TButton", "Action.Accent.TButton", "Action.Soft.TButton"):
        style.map(action_style, background=[("!disabled", c["page"]), ("disabled", c["page"])])
    style.configure("Source.TRadiobutton", foreground=c["muted"], padding=(10, 5))
    style.map("Source.TRadiobutton", foreground=[("disabled", c["disabled_ink"]), ("selected", c["accent"])])
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
    style.configure("Mast.TCombobox", bordercolor=c["mast_line"], arrowcolor=c["mast_ink"],
                    lightcolor=c["mast_button"], darkcolor=c["mast_button"])
    style.map("Mast.TCombobox", fieldbackground=[("readonly", c["mast_button"])],
              foreground=[("readonly", c["mast_ink"])], selectbackground=[("readonly", c["mast_button"])],
              selectforeground=[("readonly", c["mast_ink"])], background=[("readonly", c["mast_button"])],
              bordercolor=[("focus", c["mark"]), ("!focus", c["mast_line"])])
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
    _rounded_controls(root, style, mode)
    if not getattr(root, "_whisper_theme_bound", False):
        root.bind_all("<Map>", _map_window, add="+")
        root._whisper_theme_bound = True
    _refresh_widgets(root)
    _refresh_listboxes(root.tk, str(root), c)
    return style


def section_title(parent, number, title):
    row = ttk.Frame(parent)
    ttk.Label(row, text=number, style="Step.TLabel").pack(side="left", padx=(0, 9))
    ttk.Label(row, text=title, style="Section.TLabel").pack(side="left")
    return row


def window_heading(parent, title):
    header = ttk.Frame(parent, style="Mast.TFrame", padding=(18, 10))
    header.pack(fill="x")
    ttk.Label(header, text=title, style="Mast.TLabel").pack(side="left")
    return header


def action_icon(button, symbol):
    from PIL import Image, ImageDraw, ImageTk

    def refresh():
        style = ttk.Style(button)
        images = []
        for state in ('!disabled', 'disabled'):
            color = style.lookup(button.cget('style'), 'foreground', (state,))
            bitmap = Image.new('RGBA', (60, 48))
            draw = ImageDraw.Draw(bitmap)
            if symbol == 'play':
                draw.polygon(((10, 6), (10, 42), (40, 24)), fill=color)
            elif symbol == 'stop':
                draw.rounded_rectangle((8, 8, 38, 38), radius=4, fill=color)
            elif symbol == 'compare':
                for x in (3, 25):
                    draw.rounded_rectangle((x, 7, x + 16, 40), radius=3, outline=color, width=4)
            elif symbol == 'wave':
                for x, height in ((4, 10), (15, 26), (26, 38), (37, 16)):
                    draw.rounded_rectangle((x, 24-height//2, x+5, 24+height//2), radius=2, fill=color)
            images.append(ImageTk.PhotoImage(bitmap.resize((20, 16), Image.Resampling.LANCZOS), master=button))
        button.configure(image=(images[0], 'disabled', images[1]), compound='left')
        button._whisper_icons = images

    button._whisper_refresh_tags = refresh
    refresh()


def brand(parent):
    row = ttk.Frame(parent, style="Mast.TFrame")
    c = palette(parent)
    icon = tk.Canvas(row, width=44, height=44, background=c["mast"], highlightthickness=0)
    icon._whisper_theme_role = "brand"
    icon.pack(side="left", padx=(0, 11))
    icon.create_polygon(2, 12, 2, 2, 12, 2, 32, 2, 42, 2, 42, 12, 42, 32, 42, 42, 32, 42,
                        12, 42, 2, 42, 2, 32, smooth=True, fill=c["mark"], tags="tile")
    for x, height in ((10, 6), (16, 15), (22, 25), (28, 13), (34, 5)):
        icon.create_line(x, 22-height/2, x, 22+height/2, fill=c["mast"], width=3, capstyle="round", tags="wave")
    ttk.Label(row, text="Whisper", style="Wordmark.TLabel").pack(side="left")
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
