"""Searchable chat-style transcript panel, independent of the minimized editor."""
import tkinter as tk
from tkinter import ttk, messagebox
from transcript_history import LIMITS, match_spans, select_page
from ui_theme import palette, style_text


class HistoryWindow(tk.Toplevel):
    def __init__(self, app):
        super().__init__(app.root)
        self.withdraw()
        self.app = app
        self.title('文字起こし履歴・検索')
        self.geometry('400x660')
        self.minsize(340, 380)
        # Do not make this transient: it must survive main-window minimization.
        self.attributes('-topmost', True)
        self.source = None
        self.end_key = None
        self.follow = True
        self.page = None
        self.signature = None
        self.timer = None
        self.rendering = False
        self.query = tk.StringVar()
        self.limit = tk.StringVar(value=str(app.history_limit))
        self.side = tk.StringVar(value='右端' if app.history_side == 'right' else '左端')
        self.summary = tk.StringVar()
        outer = ttk.Frame(self, padding=(12, 10))
        outer.pack(fill='both', expand=True)
        outer.columnconfigure(0, weight=1)
        outer.rowconfigure(4, weight=1)
        ttk.Label(outer, text='文字起こし履歴', font=('Yu Gothic UI', 14, 'bold')).grid(row=0, column=0, sticky='w')
        ttk.Label(outer, text='今開いている結果の全文から検索', style='Sub.TLabel').grid(row=1, column=0, sticky='w', pady=(2, 8))
        search = ttk.Frame(outer)
        search.grid(row=2, column=0, sticky='ew')
        ttk.Label(search, text='検索').pack(side='left', padx=(0, 6))
        self.search = ttk.Entry(search, textvariable=self.query)
        self.search.pack(side='left', fill='x', expand=True)
        ttk.Button(search, text='解除', width=5, command=lambda: self.query.set('')).pack(side='right', padx=(6, 0))
        self.search.bind('<Return>', lambda e: self.search_changed())
        self.search.bind('<Escape>', lambda e: self.query.set(''))
        options = ttk.Frame(outer)
        options.grid(row=3, column=0, sticky='ew', pady=8)
        ttk.Label(options, text='表示').pack(side='left')
        limit = ttk.Combobox(options, textvariable=self.limit, values=[str(n) for n in LIMITS], state='readonly', width=4)
        limit.pack(side='left', padx=5)
        limit.bind('<<ComboboxSelected>>', self.change_limit)
        ttk.Label(options, text='件').pack(side='left')
        side = ttk.Combobox(options, textvariable=self.side, values=['右端', '左端'], state='readonly', width=5)
        side.pack(side='right')
        side.bind('<<ComboboxSelected>>', self.change_side)
        ttk.Label(options, text='配置').pack(side='right', padx=5)
        body = ttk.Frame(outer)
        body.grid(row=4, column=0, sticky='nsew')
        self.view = tk.Text(body, wrap='word', font=('Yu Gothic UI', 11), state='disabled', cursor='arrow',
                            spacing1=2, spacing3=5, takefocus=True)
        style_text(self.view)
        self.scroll = ttk.Scrollbar(body, command=self.scroll_view)
        self.view.configure(yscrollcommand=self.scroll.set)
        self.scroll.pack(side='right', fill='y')
        self.view.pack(fill='both', expand=True)
        for sequence in ('<MouseWheel>', '<Button-4>', '<Button-5>', '<Button-1>', '<KeyPress-Up>', '<KeyPress-Prior>', '<KeyPress-Home>'):
            self.view.bind(sequence, self.pause, add='+')
        self.view.bind('<Control-a>', self.select_all)
        self.view.bind('<Control-c>', self.copy_selection)
        self.view.bind('<Configure>', lambda e: self.view.see('end') if self.follow else None, add='+')
        self.bind('<Control-f>', self.focus_search)
        self.view._whisper_refresh_tags = self.apply_colors
        self.apply_colors()
        ttk.Label(outer, textvariable=self.summary, style='Sub.TLabel', wraplength=310).grid(row=5, column=0, sticky='ew', pady=7)
        buttons = ttk.Frame(outer)
        buttons.grid(row=6, column=0, sticky='ew')
        self.older = ttk.Button(buttons, text='← 古い発言', command=lambda: self.turn_page(True))
        self.older.pack(side='left')
        self.newer = ttk.Button(buttons, text='新しい発言 →', command=lambda: self.turn_page(False))
        self.newer.pack(side='left', padx=4)
        ttk.Button(buttons, text='最新へ', style='Accent.TButton', command=self.latest).pack(side='right')
        self.query.trace_add('write', lambda *args: self.search_changed())
        self.protocol('WM_DELETE_WINDOW', app.hide_history)
        self.bind('<Destroy>', self.destroyed, add='+')
        if 'history' not in app.window_settings.data['windows']:
            self.dock()
        from screen_capture import exclude_window
        self.capture_excluded = exclude_window(self)
        app.window_settings.attach(self, 'history')
        self.refresh(reset=True)

    def apply_colors(self):
        colors = palette(self)
        self.view.tag_configure('name', foreground=colors['accent'], font=('Yu Gothic UI', 10, 'bold'))
        self.view.tag_configure('time', foreground=colors['muted'], font=('Yu Gothic UI', 9))
        self.view.tag_configure('match', background=colors['diff_whisper'], foreground=colors['diff_whisper_ink'])
        self.view.tag_raise('sel')

    def dock(self):
        from overlay_preferences import display_areas
        from window_settings import handle, native
        from ctypes import wintypes as w
        import ctypes
        self.update_idletasks()
        areas = display_areas(self)
        saved = self.app.window_settings.data['windows'].get('history')
        area = next((a for a in areas if saved and a['device'] == saved['device']),
                    next((a for a in areas if a.get('primary')), areas[0]))
        left, top, right, bottom = area['rect']
        # Keep a margin for the title bar and the Windows taskbar.
        width = min(max(340, self.winfo_width()), right-left-16)
        height = min(max(380, self.winfo_height()), bottom-top-64)
        self.geometry(f'{width}x{height}')
        self.update_idletasks()
        rect = w.RECT()
        hwnd = handle(self)
        native('GetWindowRect', w.BOOL, w.HWND, ctypes.POINTER(w.RECT))(hwnd, ctypes.byref(rect))
        x = left + 8 if self.side.get() == '左端' else right - (rect.right-rect.left) - 8
        native('SetWindowPos', w.BOOL, w.HWND, w.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, w.UINT)(
            hwnd, None, x, top + 8, 0, 0, 0x0015)

    def change_side(self, event=None):
        self.app.history_side = 'left' if self.side.get() == '左端' else 'right'
        self.dock()
        self.app.window_settings.remember('history', self)
        self.app.persist_live_recording_settings()

    def change_limit(self, event=None):
        self.app.history_limit = int(self.limit.get())
        self.app.persist_live_recording_settings()
        self.refresh()

    def focus_search(self, event=None):
        self.search.focus_set()
        self.search.selection_range(0, 'end')
        return 'break'

    def select_all(self, event=None):
        self.view.tag_add('sel', '1.0', 'end-1c')
        return 'break'

    def copy_selection(self, event=None):
        if self.view.tag_ranges('sel'):
            self.clipboard_clear()
            self.clipboard_append(self.view.get('sel.first', 'sel.last'))
        return 'break'

    def search_changed(self):
        if self.timer is not None:
            self.after_cancel(self.timer)
        self.timer = self.after(160, self.apply_search)

    def apply_search(self):
        self.timer = None
        self.end_key = None
        self.follow = not self.query.get().strip()
        self.refresh(reset=True)

    def pause(self, event=None):
        if self.rendering:
            return
        self.follow = False
        if self.page and self.page.entries:
            self.end_key = self.page.entries[-1].key
        self.update_summary()

    def scroll_view(self, *args):
        self.pause()
        self.view.yview(*args)

    def turn_page(self, older):
        if not self.page or (older and not self.page.older) or (not older and not self.page.newer):
            return
        self.follow = False
        self.end_key = self.page.adjacent_anchor(self.app.history_limit, older)
        self.refresh(reset=True)

    def latest(self):
        self.query.set('')
        if self.timer is not None:
            self.after_cancel(self.timer)
            self.timer = None
        self.follow = True
        self.end_key = None
        self.refresh(reset=True)

    def update_summary(self):
        if not self.page:
            return
        count = len(self.page.matches)
        if self.query.get().strip():
            text = f'検索結果 {count}件'
        else:
            text = f'全{self.page.total}件'
        if count:
            text += f' · {self.page.start+1}〜{self.page.end}件を表示'
        text += '\n最新を自動表示' if self.follow else '\n追従を停止中 ·「最新へ」で戻ります'
        self.summary.set(text)
        self.older.configure(state='normal' if self.page.older else 'disabled')
        self.newer.configure(state='normal' if self.page.newer else 'disabled')

    def highlight(self, start, text):
        for a, b in match_spans(text, self.query.get()):
            self.view.tag_add('match', f'{start}+{a}c', f'{start}+{b}c')

    def refresh(self, reset=False):
        source = str(self.app.result_path.parent) if self.app.result_path else None
        if source != self.source:
            self.source, self.end_key, self.follow = source, None, not self.query.get().strip()
            reset = True
        page = select_page(self.app.segments, self.app.speakers, self.query.get(), self.app.history_limit,
                           None if self.follow else self.end_key)
        self.page = page
        if not self.follow and page.entries:
            self.end_key = page.entries[-1].key
        self.update_summary()
        signature = (page.entries, self.query.get())
        if signature == self.signature and not reset:
            return
        self.signature = signature
        position = self.view.yview()[0]
        self.rendering = True
        try:
            self.view.configure(state='normal')
            self.view.delete('1.0', 'end')
            for item in page.entries:
                start = self.view.index('end-1c')
                self.view.insert('end', item.name, 'name')
                self.highlight(start, item.name)
                self.view.insert('end', '  ' + item.time + '\n', 'time')
                start = self.view.index('end-1c')
                self.view.insert('end', item.text + '\n\n')
                self.highlight(start, item.text)
            if not page.entries:
                self.view.insert('end', '一致する発言がありません。' if self.query.get().strip() else
                                 '文字起こしを始めると、ここに発言が並びます。\n保存した結果を開いて検索することもできます。', 'time')
            self.view.configure(state='disabled')
            if self.follow:
                self.view.see('end')
            elif reset:
                self.view.yview_moveto(0)
            else:
                self.view.yview_moveto(position)
        finally:
            self.rendering = False

    def destroyed(self, event):
        if event.widget is self and self.timer is not None:
            self.after_cancel(self.timer)
            self.timer = None


class HistoryControls:
    def init_history(self):
        saved = self.live_saved_record_settings['history']
        self.history_window = None
        self.history_visible = tk.BooleanVar(value=saved['visible'])
        self.history_limit, self.history_side = saved['limit'], saved['side']
        self.history_dirty = False
        self.root.bind('<Control-f>', lambda e: self.open_history(focus=True))

    def ensure_history(self):
        if self.history_window is None or not self.history_window.winfo_exists():
            self.history_window = HistoryWindow(self)
        return self.history_window

    def open_history(self, focus=False):
        panel = self.ensure_history()
        if (self.proc and self.operation == 'realtime' and self.live_active_mode == 'screen'
                and not panel.capture_excluded):
            panel.withdraw()
            messagebox.showerror('履歴を表示できません', '履歴を画面録画から除外できません。録画を停止してから開いてください。', parent=self.root)
            return 'break'
        self.history_visible.set(True)
        self.persist_live_recording_settings()
        self.window_settings.show('history', panel)
        panel.refresh()
        if focus:
            panel.focus_search()
        return 'break'

    def hide_history(self):
        self.history_visible.set(False)
        self.persist_live_recording_settings()
        if self.history_window and self.history_window.winfo_exists():
            self.window_settings.hide('history')

    def toggle_history(self):
        if self.history_visible.get():
            self.open_history()
        else:
            self.hide_history()

    def refresh_history(self):
        self.history_dirty = True

    def pump_history(self):
        if (self.history_dirty and self.history_window and self.history_window.winfo_exists()
                and self.history_window.state() not in {'withdrawn', 'iconic'}):
            self.history_window.refresh()
            self.history_dirty = False
