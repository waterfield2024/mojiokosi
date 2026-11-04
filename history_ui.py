"""Searchable chat-style transcript panel, independent of the minimized editor."""
import queue
import tkinter as tk
from tkinter import ttk, messagebox
from comparison_audio import PLAYBACK_SPEEDS
from transcript_history import LIMITS, match_spans, organized_paragraphs, row_key, select_page
from ui_theme import palette, style_text

SPEED_LABELS = {f'{speed:g}倍': speed for speed in PLAYBACK_SPEEDS}


class HistoryWindow(tk.Toplevel):
    def __init__(self, app):
        super().__init__(app.root)
        self.withdraw()
        self.app = app
        self.title('文字起こし履歴・検索')
        self.geometry('400x800')
        self.minsize(340, 560)
        # Do not make this transient: it must survive main-window minimization.
        self.attributes('-topmost', True)
        self.source = None
        self.end_key = None
        self.follow = True
        self.page = None
        self.signature = None
        self.organized_signature = None
        self.organized_text = ''
        self.organized_follow = True
        self.timer = None
        self.rendering = False
        self.audio_player = None
        self.audio_generation = None
        self.audio_key = None
        self.audio_interval = None
        self.audio_requested_interval = None
        self.audio_playing_speed = 1.0
        self.audio_speed = tk.StringVar(value='1倍')
        self.audio_tags = {}
        self.press = None
        self.audio_status = tk.StringVar(value='発言をクリックすると音声を再生します')
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
        self.panes = ttk.Panedwindow(outer, orient='vertical')
        self.panes.grid(row=4, column=0, sticky='nsew')
        history = ttk.Frame(self.panes)
        history.columnconfigure(0, weight=1)
        history.rowconfigure(0, weight=1)
        self.panes.add(history, weight=3)
        body = ttk.Frame(history)
        body.grid(row=0, column=0, sticky='nsew')
        self.view = tk.Text(body, wrap='word', font=('Yu Gothic UI', 11), state='disabled', cursor='arrow',
                            height=5, spacing1=2, spacing3=5, takefocus=True)
        style_text(self.view)
        self.scroll = ttk.Scrollbar(body, command=self.scroll_view)
        self.view.configure(yscrollcommand=self.scroll.set)
        self.scroll.pack(side='right', fill='y')
        self.view.pack(fill='both', expand=True)
        for sequence in ('<MouseWheel>', '<Button-4>', '<Button-5>', '<Button-1>', '<KeyPress-Up>', '<KeyPress-Prior>', '<KeyPress-Home>'):
            self.view.bind(sequence, self.pause, add='+')
        self.view.bind('<ButtonPress-1>', self.press_entry, add='+')
        self.view.bind('<B1-Motion>', self.drag_entry, add='+')
        self.view.bind('<ButtonRelease-1>', self.release_entry, add='+')
        self.view.bind('<Motion>', self.hover_entry, add='+')
        self.view.bind('<Control-a>', self.select_all)
        self.view.bind('<Control-c>', self.copy_selection)
        self.view.bind('<Configure>', lambda e: self.view.see('end-3c') if self.follow else None, add='+')
        self.bind('<Control-f>', self.focus_search)
        self.bind('<Escape>', lambda e: self.stop_audio('再生を停止しました。'))
        self.view._whisper_refresh_tags = self.apply_colors
        ttk.Label(history, textvariable=self.summary, style='Sub.TLabel', wraplength=310).grid(row=1, column=0, sticky='ew', pady=7)
        buttons = ttk.Frame(history)
        buttons.grid(row=2, column=0, sticky='ew')
        self.older = ttk.Button(buttons, text='← 古い発言', command=lambda: self.turn_page(True))
        self.older.pack(side='left')
        self.newer = ttk.Button(buttons, text='新しい発言 →', command=lambda: self.turn_page(False))
        self.newer.pack(side='left', padx=4)
        ttk.Button(buttons, text='最新へ', style='Accent.TButton', command=self.latest).pack(side='right')
        audio_bar = ttk.Frame(history)
        audio_bar.grid(row=3, column=0, sticky='ew', pady=(8, 6))
        audio_bar.columnconfigure(0, weight=1)
        speed_bar = ttk.Frame(audio_bar)
        speed_bar.grid(row=0, column=0, sticky='w')
        ttk.Label(speed_bar, text='再生速度').pack(side='left', padx=(0, 6))
        self.audio_speed_combo = ttk.Combobox(speed_bar, textvariable=self.audio_speed,
                                             values=list(SPEED_LABELS), state='readonly', width=6)
        self.audio_speed_combo.pack(side='left')
        self.audio_speed_combo.bind('<<ComboboxSelected>>', self.change_audio_speed)
        self.audio_stop = ttk.Button(audio_bar, text='音声を停止', command=lambda: self.stop_audio('再生を停止しました。'), state='disabled')
        self.audio_stop.grid(row=0, column=1, sticky='e', padx=(6, 0))
        ttk.Label(audio_bar, textvariable=self.audio_status, style='Sub.TLabel', wraplength=310).grid(
            row=1, column=0, columnspan=2, sticky='w', pady=(5, 0))
        organized = ttk.Frame(self.panes, padding=(0, 8, 0, 0))
        organized.columnconfigure(0, weight=1)
        organized.rowconfigure(2, weight=1)
        self.panes.add(organized, weight=2)
        heading = ttk.Frame(organized)
        heading.grid(row=0, column=0, sticky='ew')
        ttk.Label(heading, text='整理した文章', font=('Yu Gothic UI', 12, 'bold')).pack(side='left')
        self.organized_copy = ttk.Button(heading, text='コピー', width=7, command=self.copy_organized, state='disabled')
        self.organized_copy.pack(side='right')
        self.organized_note = tk.StringVar()
        ttk.Label(organized, textvariable=self.organized_note, style='Sub.TLabel', wraplength=310).grid(
            row=1, column=0, sticky='ew', pady=(2, 6))
        reading = ttk.Frame(organized)
        reading.grid(row=2, column=0, sticky='nsew')
        self.organized_view = tk.Text(reading, wrap='word', font=('Yu Gothic UI', 11), state='disabled',
                                      height=5, cursor='xterm', spacing1=2, spacing3=4, takefocus=True)
        style_text(self.organized_view)
        reading_scroll = ttk.Scrollbar(reading, command=self.scroll_organized)
        self.organized_view.configure(yscrollcommand=reading_scroll.set)
        reading_scroll.pack(side='right', fill='y')
        self.organized_view.pack(fill='both', expand=True)
        self.organized_view.bind('<Control-a>', self.select_all)
        self.organized_view.bind('<Control-c>', self.copy_selection)
        for sequence in ('<MouseWheel>', '<Button-4>', '<Button-5>', '<Button-1>', '<KeyPress-Up>', '<KeyPress-Prior>', '<KeyPress-Home>'):
            self.organized_view.bind(sequence, self.pause_organized, add='+')
        self.organized_view.bind('<Configure>', lambda e: self.organized_view.see('end-1c') if self.organized_follow else None, add='+')
        self.organized_view._whisper_refresh_tags = self.apply_colors
        self.apply_colors()
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
        self.view.tag_configure('playing', background=colors['tint'])
        self.view.tag_raise('match')
        self.view.tag_raise('sel')
        self.organized_view.tag_configure('name', foreground=colors['accent'], font=('Yu Gothic UI', 10, 'bold'))
        self.organized_view.tag_configure('hint', foreground=colors['muted'])

    def entry_at(self, event):
        index = self.view.index(f'@{event.x},{event.y}')
        bounds = self.view.bbox(index)
        if not bounds or not bounds[1] <= event.y < bounds[1] + bounds[3]:
            return None
        return next((self.audio_tags[tag] for tag in self.view.tag_names(index) if tag in self.audio_tags), None)

    def hover_entry(self, event):
        self.view.configure(cursor='hand2' if self.entry_at(event) else 'arrow')

    def press_entry(self, event):
        self.press = (self.entry_at(event), event.x, event.y, False)

    def drag_entry(self, event):
        if self.press:
            key, x, y, moved = self.press
            self.press = (key, x, y, moved or abs(event.x-x) > 4 or abs(event.y-y) > 4)

    def release_entry(self, event):
        pressed, self.press = self.press, None
        if (pressed and pressed[0] and not pressed[3] and pressed[0] == self.entry_at(event)
                and not self.view.tag_ranges('sel')):
            self.play_entry(pressed[0])

    def current_row(self, key):
        return next((row for index, row in enumerate(self.app.segments) if row_key(row, index) == key), None)

    def mark_playing(self):
        self.view.tag_remove('playing', '1.0', 'end')
        if self.audio_key is not None:
            for tag, key in self.audio_tags.items():
                if key == self.audio_key:
                    bounds = self.view.tag_ranges(tag)
                    if bounds:
                        self.view.tag_add('playing', *bounds)

    def play_entry(self, key):
        self.pause()
        self.stop_audio()
        try:
            if getattr(self.app, 'data_transfer_active', False):
                raise RuntimeError('バックアップ・復元が終わってから再生してください。')
            row = self.current_row(key)
            if row is None or self.app.result_path is None:
                raise ValueError('発言が更新されました。表示中の発言をもう一度クリックしてください。')
            from history_audio import playback_target, utterance_interval
            source, interval = playback_target(self.app.result_data or {}, self.app.result_path.parent, row)
            if self.audio_player is None:
                from comparison_audio import ComparisonAudioPlayer
                self.audio_player = ComparisonAudioPlayer()
            comparison = getattr(self.app, 'comparison_window', None)
            if comparison:
                comparison.stop_comparison_audio()
            self.audio_playing_speed = SPEED_LABELS.get(self.audio_speed.get(), 1.0)
            self.audio_generation = self.audio_player.play(source, [interval], speed=self.audio_playing_speed)
            self.audio_key, self.audio_interval = key, interval
            self.audio_requested_interval = utterance_interval(row)
            self.audio_status.set(f'{self.audio_playing_speed:g}倍速で音声を準備中…')
            self.audio_stop.configure(state='normal')
            self.mark_playing()
        except (OSError, RuntimeError, ValueError) as exc:
            self.stop_audio('再生できません：' + str(exc))

    def change_audio_speed(self, event=None):
        speed = SPEED_LABELS.get(self.audio_speed.get(), 1.0)
        if self.audio_generation is not None:
            if speed == self.audio_playing_speed:
                return
            self.play_entry(self.audio_key)
            if self.audio_generation is not None:
                self.audio_status.set(f'{speed:g}倍速で発言の最初から再生します。')
        else:
            self.audio_status.set(f'再生速度：{speed:g}倍。発言をクリックして再生します。')

    def stop_audio(self, message='発言をクリックすると音声を再生します'):
        if self.audio_player:
            self.audio_player.stop()
        self.audio_generation = self.audio_key = self.audio_interval = self.audio_requested_interval = None
        self.audio_status.set(message)
        self.audio_stop.configure(state='disabled')
        self.mark_playing()

    def poll_audio(self):
        if not self.audio_player:
            return
        if self.audio_generation is not None:
            from history_audio import utterance_interval
            row = self.current_row(self.audio_key)
            try:
                unchanged = row is not None and utterance_interval(row) == self.audio_requested_interval
            except ValueError:
                unchanged = False
            if not unchanged:
                self.stop_audio('発言の時刻が変更されました。もう一度クリックして再生できます。')
        while True:
            try:
                generation, state, value = self.audio_player.events.get_nowait()
            except queue.Empty:
                break
            if generation != self.audio_generation:
                continue
            if state == 'playing':
                from engine import timestamp
                text = f'再生中 {self.audio_playing_speed:g}倍 · {timestamp(value)[:-2]} / {timestamp(self.audio_interval[1])[:-2]}'
                if getattr(self.app, 'live_recording', False):
                    text += '\n録音中：再生音も取り込まれる場合があります'
                self.audio_status.set(text)
            elif state == 'ended':
                self.stop_audio('再生が終わりました。クリックで聞き直せます。')
            elif state == 'error':
                self.stop_audio('再生できません：' + str(value))

    def close_audio(self):
        try:
            self.stop_audio()
        except tk.TclError:
            pass  # Child widgets have already gone during <Destroy>.
        if self.audio_player:
            self.audio_player.close()
            self.audio_player = None

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
        height = min(max(560, self.winfo_height()), bottom-top-64)
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
        view = event.widget if event is not None else self.view
        view.tag_add('sel', '1.0', 'end-1c')
        return 'break'

    def copy_selection(self, event=None):
        view = event.widget if event is not None else self.view
        if view.tag_ranges('sel'):
            self.clipboard_clear()
            self.clipboard_append(view.get('sel.first', 'sel.last'))
        return 'break'

    def copy_organized(self):
        if self.organized_text:
            self.clipboard_clear()
            self.clipboard_append(self.organized_text)

    def pause_organized(self, event=None):
        self.organized_follow = False

    def scroll_organized(self, *args):
        self.pause_organized()
        self.organized_view.yview(*args)

    def search_changed(self):
        self.stop_audio()
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
        self.stop_audio()
        self.follow = False
        self.end_key = self.page.adjacent_anchor(self.app.history_limit, older)
        self.refresh(reset=True)

    def latest(self):
        self.query.set('')
        if self.timer is not None:
            self.after_cancel(self.timer)
            self.timer = None
        self.follow = True
        self.organized_follow = True
        self.end_key = None
        self.refresh(reset=True)
        self.organized_view.see('end-1c')

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

    def refresh_organized(self, reset=False):
        paragraphs = organized_paragraphs(self.app.segments, self.app.speakers, self.app.history_limit)
        self.organized_note.set(f'直近{self.app.history_limit}件をつなげて表示 · 検索中も更新')
        if paragraphs == self.organized_signature and not reset:
            return
        self.organized_signature = paragraphs
        self.organized_text = '\n\n'.join((p.name + '\n' if p.name else '') + p.text for p in paragraphs)
        self.organized_copy.configure(state='normal' if paragraphs else 'disabled')
        first = self.organized_view.yview()[0]
        if reset:
            self.organized_follow = True
        self.organized_view.configure(state='normal')
        self.organized_view.delete('1.0', 'end')
        for index, paragraph in enumerate(paragraphs):
            if index:
                self.organized_view.insert('end', '\n\n')
            if paragraph.name:
                self.organized_view.insert('end', paragraph.name + '\n', 'name')
            self.organized_view.insert('end', paragraph.text)
        if not paragraphs:
            self.organized_view.insert('end', '発言が確定すると、ここにつなげた文章を表示します。', 'hint')
        self.organized_view.configure(state='disabled')
        if self.organized_follow:
            self.organized_view.see('end-1c')
        else:
            self.organized_view.yview_moveto(first)

    def refresh(self, reset=False):
        source = str(self.app.result_path.parent) if self.app.result_path else None
        source_changed = source != self.source
        if source_changed:
            self.stop_audio()
            self.source, self.end_key, self.follow = source, None, not self.query.get().strip()
            reset = True
        # The reading pane always follows the current result, even when the
        # history's search/page is unchanged or paused on an older utterance.
        self.refresh_organized(reset=source_changed)
        page = select_page(self.app.segments, self.app.speakers, self.query.get(), self.app.history_limit,
                           None if self.follow else self.end_key)
        self.page = page
        if self.audio_key is not None and not any(item.key == self.audio_key for item in page.entries):
            self.stop_audio()
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
            for tag in self.audio_tags:
                self.view.tag_delete(tag)
            self.audio_tags = {}
            for number, item in enumerate(page.entries):
                row_start = self.view.index('end-1c')
                start = self.view.index('end-1c')
                self.view.insert('end', item.name, 'name')
                self.highlight(start, item.name)
                self.view.insert('end', '  ' + item.time + '\n', 'time')
                start = self.view.index('end-1c')
                self.view.insert('end', item.text + '\n\n')
                self.highlight(start, item.text)
                tag = f'utterance_{number}'
                self.view.tag_add(tag, row_start, 'end-2c')
                self.audio_tags[tag] = item.key
            if not page.entries:
                self.view.insert('end', '一致する発言がありません。' if self.query.get().strip() else
                                 '文字起こしを始めると、ここに発言が並びます。\n保存した結果を開いて検索することもできます。', 'time')
            self.view.configure(state='disabled')
            self.mark_playing()
            if self.follow:
                self.view.see('end-3c')
            elif reset:
                self.view.yview_moveto(0)
            else:
                self.view.yview_moveto(position)
        finally:
            self.rendering = False

    def destroyed(self, event):
        if event.widget is self:
            self.close_audio()
            if self.timer is not None:
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
            self.history_window.stop_audio()
            self.window_settings.hide('history')

    def toggle_history(self):
        if self.history_visible.get():
            self.open_history()
        else:
            self.hide_history()

    def refresh_history(self):
        self.history_dirty = True

    def pump_history(self):
        if self.history_window and self.history_window.winfo_exists():
            if self.history_dirty and self.history_window.state() not in {'withdrawn', 'iconic'}:
                self.history_window.refresh()
                self.history_dirty = False
            self.history_window.poll_audio()
