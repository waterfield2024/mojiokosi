"""Browse saved transcripts and follow a noun to an utterance in another recording."""
from collections import Counter, defaultdict
import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from engine import RESULTS, timestamp
from comparison_audio import ComparisonAudioPlayer, SPEED_LABELS
from result_media import result_file
from noun_search import load_synsets, noun_key
from transcript_library import index_nouns, load_records, record_playback_target, search_nouns
from ui_theme import palette, style_text


def scan_library(root, events, stop):
    try:
        records, skipped = load_records(root, stop.is_set)
        events.put(('records', (records, skipped)))
        if records and not stop.is_set():
            from janome.tokenizer import Tokenizer
            try:
                events.put(('synsets', load_synsets()))
            except (OSError, UnicodeError):
                events.put(('dictionary_error', None))
            tokenizer = Tokenizer()
            for number, record in enumerate(records):
                index = index_nouns(record, tokenizer, stop.is_set)
                if index is None or stop.is_set():
                    return
                events.put(('index', (number, index)))
        events.put(('done', None))
    except Exception as exc:
        events.put(('error', str(exc)))


def record_table(parent, columns, height):
    frame = ttk.Frame(parent)
    frame.pack(fill='both', expand=True)
    table = ttk.Treeview(frame, columns=[name for name, _ in columns], show='headings',
                         selectmode='browse', height=height)
    for name, width in columns:
        table.heading(name, text=name)
        table.column(name, width=width, minwidth=50, stretch=name == columns[-1][0])
    scroll = ttk.Scrollbar(frame, command=table.yview)
    table.configure(yscrollcommand=scroll.set)
    scroll.pack(side='right', fill='y')
    table.pack(side='left', fill='both', expand=True)
    return table


class TranscriptLibrary(tk.Toplevel):
    PAGE_SIZE = 50

    def __init__(self, app):
        super().__init__(app.root)
        self.withdraw()
        self.app = app
        self.title('保存した記録')
        self.transient(app.root)
        self.geometry('1030x690')
        self.minsize(740, 480)
        self.records = []
        self.indexes = {}
        self.counts = Counter()
        self.shared = set()
        self.error = None
        self.selected = self.key = None
        self.query_text = ''
        self.synsets = {}
        self.dictionary_error = False
        self.include_near = tk.BooleanVar(self, value=True)
        self.origin = self.focus_row = None
        self.matches = []
        self.offset = self.skipped = 0
        self.done = False
        self.stop = threading.Event()
        self.events = queue.Queue()
        self.timer = None
        self.audio_player = self.audio_generation = None
        self.playing_hit = None
        self.playing_speed = 1.0

        outer = ttk.Frame(self, padding=12)
        outer.pack(fill='both', expand=True)
        header = ttk.Frame(outer)
        header.pack(fill='x', pady=(0, 8))
        ttk.Label(header, text='保存した記録', font=('Yu Gothic UI', 15, 'bold')).pack(side='left')
        ttk.Button(header, text='ファイルから開く…', command=self.open_file).pack(side='right')
        self.edit = ttk.Button(header, text='メイン画面で編集', command=self.edit_record, state='disabled')
        self.edit.pack(side='right', padx=6)
        self.status = tk.StringVar(value='記録を読み込み中…')
        footer = ttk.Frame(outer)
        footer.pack(side='bottom', fill='x', pady=(6, 0))
        ttk.Label(footer, textvariable=self.status, style='Sub.TLabel').pack(side='left')
        self.audio_status = tk.StringVar(self)
        ttk.Label(footer, textvariable=self.audio_status, wraplength=510).pack(side='left', padx=8)
        panes = ttk.Panedwindow(outer, orient='horizontal')
        panes.pack(fill='both', expand=True)
        left = ttk.Frame(panes, padding=(0, 0, 6, 0))
        panes.add(left, weight=1)
        self.listing = record_table(left, [('保存日時', 124), ('記録名', 155)], 15)
        self.listing.bind('<<TreeviewSelect>>', self.pick_record)
        right = ttk.Panedwindow(panes, orient='vertical')
        panes.add(right, weight=3)
        reading = ttk.Frame(right, padding=(6, 0, 0, 5))
        right.add(reading, weight=3)
        self.heading = tk.StringVar(value='本文')
        ttk.Label(reading, textvariable=self.heading, font=('Yu Gothic UI', 12, 'bold'),
                  wraplength=620).pack(anchor='w', pady=(0, 4))
        self.body = tk.Text(reading, wrap='word', width=32, height=9, state='disabled', padx=8, pady=4)
        style_text(self.body)
        self.body._whisper_refresh_tags = self.set_colors
        self.set_colors()
        scroll = ttk.Scrollbar(reading, command=self.body.yview)
        self.body.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        self.body.pack(fill='both', expand=True)
        self.body.bind('<ButtonRelease-1>', self.pick_from_text)

        related = ttk.Frame(right, padding=(6, 6, 0, 0))
        right.add(related, weight=2)
        bar = ttk.Frame(related)
        bar.pack(fill='x')
        ttk.Label(bar, text='検索語').pack(side='left')
        self.word = ttk.Combobox(bar, state='disabled', width=14)
        self.word.pack(side='left', fill='x', expand=True, padx=(8, 0))
        self.word.bind('<<ComboboxSelected>>', self.pick_word)
        self.word.bind('<Return>', self.pick_word)
        self.search_button = ttk.Button(bar, text='検索', width=5, command=self.pick_word, state='disabled')
        self.search_button.pack(side='left', padx=(4, 0))
        ttk.Checkbutton(bar, text='近い言葉も含める', variable=self.include_near,
                        command=self.pick_word).pack(side='left', padx=(6, 0))
        self.note = tk.StringVar()
        result_bar = ttk.Frame(related)
        result_bar.pack(fill='x', pady=4)
        self.audio_speed = ttk.Combobox(result_bar, values=list(SPEED_LABELS), state='readonly', width=6)
        self.audio_speed.set('1倍')
        self.audio_speed.pack(side='right')
        self.audio_speed.bind('<<ComboboxSelected>>', self.change_audio_speed)
        ttk.Label(result_bar, text='再生速度').pack(side='right', padx=4)
        ttk.Label(result_bar, textvariable=self.note, style='Sub.TLabel').pack(side='left')
        actions = ttk.Frame(related)
        actions.pack(side='bottom', fill='x', pady=(5, 0))
        self.prev = ttk.Button(actions, text='前へ', command=lambda: self.page(-1), state='disabled')
        self.next = ttk.Button(actions, text='次へ', command=lambda: self.page(1), state='disabled')
        self.prev.pack(side='left')
        self.next.pack(side='left', padx=4)
        self.back = ttk.Button(actions, text='照合元に戻る', command=self.go_back)
        self.jump = ttk.Button(actions, text='該当する発言を表示', command=self.follow_hit, state='disabled')
        self.jump.pack(side='right')
        self.audio_stop = ttk.Button(actions, text='音声を停止', command=self.stop_audio, state='disabled')
        self.audio_stop.pack(side='right', padx=4)
        self.hits = record_table(related, [('日付', 80), ('記録名', 97), ('時刻・話者', 108),
                                          ('一致した語', 140), ('発言', 170)], 5)
        self.hits.bind('<<TreeviewSelect>>', self.show_selection)
        self.hits.bind('<ButtonRelease-1>', self.click_hit)
        self.hits.bind('<space>', self.play_selected_hit)
        self.hits.bind('<Double-1>', self.follow_hit)
        self.hits.bind('<Return>', self.follow_hit)
        self.protocol('WM_DELETE_WINDOW', self.destroy)
        app.window_settings.attach(self, 'library')
        self.grab_set()
        threading.Thread(target=scan_library, args=(RESULTS, self.events, self.stop), daemon=True).start()
        self.timer = self.after(40, self.poll)

    def set_colors(self):
        colors = palette(self)
        self.body.tag_configure('meta', foreground=colors['muted'])
        self.body.tag_configure('match', background=colors['selected'], foreground=colors['selected_ink'])

    def destroy(self):
        self.stop.set()
        if self.timer is not None:
            self.after_cancel(self.timer)
            self.timer = None
        if self.audio_player is not None:
            self.audio_player.close()
        super().destroy()

    def poll(self):
        self.timer = None
        changed = False
        while True:
            try:
                kind, payload = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == 'records':
                self.records, self.skipped = payload
                first = 0
                for i, record in enumerate(self.records):
                    self.listing.insert('', 'end', iid=str(i), values=(record.date.strftime('%Y/%m/%d %H:%M'), record.title))
                    if self.app.result_path and self.app.result_path.parent == record.path.parent:
                        first = i
                if self.records:
                    self.choose_record(first)
            elif kind == 'index':
                i, index = payload
                self.indexes[i] = index
                if i == self.selected:
                    self.set_words()
                changed = True
            elif kind == 'synsets':
                self.synsets = payload
            elif kind == 'dictionary_error':
                self.dictionary_error = True
            elif kind == 'error':
                self.done = True
                self.error = payload
                changed = True
            elif kind == 'done':
                self.done = True
                sessions = defaultdict(set)
                for n, index in self.indexes.items():
                    sessions[self.records[n].session].update(index.labels)
                for keys in sessions.values():
                    self.counts.update(keys)
                self.set_words()
                changed = True
        count = len(self.records)
        if count or self.done:
            message = f'{count}件の記録' if count else '保存した記録はありません。文字起こしが終わるとここに表示されます。'
            if not self.done:
                message += f'　名詞を解析中 {len(self.indexes)} / {count}'
            if self.skipped:
                message += f'　読み込めない結果を含むフォルダーが {self.skipped} 件あります。'
            if self.error:
                message = f'読み込みを完了できませんでした。{self.error}'
            if self.dictionary_error:
                message += '　類義語辞書を読み込めませんでした。'
            self.status.set(message)
        if changed:
            self.update_matches(keep_selection=True)
        self.poll_audio()
        if not self.done or self.audio_generation is not None:
            self.timer = self.after(100, self.poll)

    def pick_record(self, event=None):
        selection = self.listing.selection()
        if selection:
            self.choose_record(int(selection[0]))

    def choose_record(self, i):
        if i == self.selected:
            return
        self.stop_audio()
        self.selected, self.key = i, None
        self.query_text = ''
        self.word.set('')
        self.search_button.configure(state='normal')
        self.focus_row = None
        self.origin = None
        self.back.pack_forget()
        self.listing.selection_set(str(i))
        self.listing.see(str(i))
        record = self.records[i]
        self.heading.set(record.title)
        self.edit.configure(state='normal')
        self.body.configure(state='normal')
        self.body.delete('1.0', 'end')
        for line in record.lines:
            self.body.insert('end', timestamp(line.start) + '　' + (line.speaker or '話者未設定') + '\n', 'meta')
            self.body.insert('end', line.text + '\n\n')
        if not record.lines:
            self.body.insert('end', 'この記録には発言がありません。')
        self.body.configure(state='disabled')
        self.body.yview_moveto(0)
        self.set_words()
        self.update_matches()

    def set_words(self):
        index = self.indexes.get(self.selected)
        self.shared = {k for k in index.labels if self.counts[k] > 1} if index else set()
        values = [index.labels[k] for k in sorted(index.labels, key=lambda k: (-self.counts[k], k))] if index else []
        self.word.configure(values=values, state='normal' if self.selected is not None else 'disabled')

    def pick_from_text(self, event):
        index = self.indexes.get(self.selected)
        if not index or self.body.tag_ranges('sel'):
            return
        pos = self.body.index(f'@{event.x},{event.y}')
        line = int(pos.split('.')[0])
        row, kind = divmod(line - 1, 3)
        if kind != 1 or row >= len(index.spans):
            return
        offset = len(self.body.get(f'{line}.0', pos))
        key = index.at(row, offset, self.shared)
        if key:
            self.word.set(index.labels[key])
            self.pick_word()

    def pick_word(self, event=None):
        if self.selected is None:
            return
        self.stop_audio()
        self.query_text = self.word.get().strip()
        self.key = noun_key(self.query_text)
        self.offset = 0
        self.update_matches()

    def update_matches(self, keep_selection=False):
        selection = self.hits.selection() if keep_selection else ()
        old = self.matches[int(selection[0])] if selection else None
        self.matches = search_nouns(self.records, self.indexes, self.selected, self.key,
                                    include_near=self.include_near.get(), synsets=self.synsets) if self.key else []
        self.offset = min(self.offset, max(0, (len(self.matches) - 1) // self.PAGE_SIZE * self.PAGE_SIZE))
        self.draw_hits()
        if old is not None:
            for n, hit in enumerate(self.matches[self.offset:self.offset + self.PAGE_SIZE], self.offset):
                if (hit.record, hit.row) == (old.record, old.row):
                    self.hits.selection_set(str(n))
                    break

    def page(self, step):
        self.stop_audio()
        self.offset += step * self.PAGE_SIZE
        self.draw_hits()

    def draw_hits(self):
        self.hits.delete(*self.hits.get_children())
        end = min(self.offset + self.PAGE_SIZE, len(self.matches))
        for n in range(self.offset, end):
            hit = self.matches[n]
            record = self.records[hit.record]
            line = record.lines[hit.row]
            self.hits.insert('', 'end', iid=str(n), values=(record.date.strftime('%Y/%m/%d'), record.title,
                             timestamp(line.start) + ' ' + line.speaker, self.hit_label(hit), line.text))
        self.jump.configure(state='disabled')
        self.prev.configure(state='normal' if self.offset else 'disabled')
        self.next.configure(state='normal' if end < len(self.matches) else 'disabled')
        self.show_selection()

    def hit_label(self, hit):
        line = self.records[hit.record].lines[hit.row].text
        start, end = next((a, b) for a, b, key in self.indexes[hit.record].spans[hit.row] if key == hit.key)
        return f'{line[start:end]}（{hit.reason}）'

    def show_selection(self, event=None):
        selection = self.hits.selection()
        self.jump.configure(state='normal' if selection else 'disabled')
        if selection:
            hit = self.matches[int(selection[0])]
            self.note.set(f'「{self.query_text}」の検索結果　{self.hit_label(hit)}')
        elif self.matches:
            end = min(self.offset + self.PAGE_SIZE, len(self.matches))
            self.note.set(f'{self.offset + 1}〜{end} / {len(self.matches)}件の発言　クリックで音声を再生')
        elif self.key:
            self.note.set('照合できた記録に一致する発言はありません。' if self.done else 'ほかの記録を照合中…')
        else:
            self.note.set('本文の名詞をクリックするか、検索語を入力してほかの記録を探せます。')

    def click_hit(self, event):
        if self.hits.identify_region(event.x, event.y) != 'cell':
            return
        row = self.hits.identify_row(event.y)
        if row and row in self.hits.selection():
            self.play_selected_hit()

    def play_selected_hit(self, event=None):
        selection = self.hits.selection()
        if selection:
            self.play_hit(self.matches[int(selection[0])], SPEED_LABELS[self.audio_speed.get()])
        return 'break'

    def change_audio_speed(self, event=None):
        self.poll_audio()
        speed = SPEED_LABELS[self.audio_speed.get()]
        if self.playing_hit is not None and speed != self.playing_speed:
            self.play_hit(self.playing_hit, speed)

    def play_hit(self, hit, speed):
        self.stop_audio()
        try:
            if getattr(self.app, 'data_transfer_active', False):
                raise RuntimeError('更新・復元が終わってから再生してください。')
            source, interval = record_playback_target(RESULTS, self.records[hit.record], hit.row)
            if self.app.history_window:
                self.app.history_window.stop_audio()
            if self.app.comparison_window:
                self.app.comparison_window.stop_comparison_audio()
            if self.audio_player is None:
                self.audio_player = ComparisonAudioPlayer()
            self.audio_generation = self.audio_player.play(source, [interval], speed=speed)
            self.playing_hit, self.playing_speed = hit, speed
            self.audio_stop.configure(state='normal')
            self.audio_status.set(f'発言の音声を準備中… {speed:g}倍速')
            if self.timer is None:
                self.timer = self.after(50, self.poll)
        except (OSError, RuntimeError, ValueError, TypeError, OverflowError) as exc:
            self.stop_audio(f'再生できません：{exc}')

    def stop_audio(self, message=''):
        if self.audio_player is not None and self.audio_generation is not None:
            self.audio_player.stop()
        self.audio_generation = None
        self.playing_hit = None
        self.audio_stop.configure(state='disabled')
        self.audio_status.set(message)

    def poll_audio(self):
        if self.audio_player is None:
            return
        while True:
            try:
                generation, state, value = self.audio_player.events.get_nowait()
            except queue.Empty:
                break
            if generation != self.audio_generation:
                continue
            if state == 'playing':
                self.audio_status.set(f'発言の音声を再生中… {self.playing_speed:g}倍速')
            elif state == 'error':
                self.stop_audio(f'再生できません：{value}')
            elif state == 'ended':
                self.stop_audio('もう一度クリックすると聞き直せます。')

    def follow_hit(self, event=None):
        selected = self.hits.selection()
        if event is not None and event.num == 1:
            if self.hits.identify_region(event.x, event.y) != 'cell' or self.hits.identify_row(event.y) not in selected:
                return
        if selected:
            hit = self.matches[int(selected[0])]
            origin = self.selected, self.query_text, self.offset, self.include_near.get()
            self.choose_record(hit.record)
            self.origin, self.focus_row = origin, hit.row
            self.back.pack(side='left', padx=4)
            pos = f'{3 * hit.row + 2}.0'
            self.body.tag_remove('match', '1.0', 'end')
            self.body.tag_add('match', pos, pos + ' lineend')
            self.body.see(pos)

    def go_back(self):
        i, query, offset, include_near = self.origin
        self.choose_record(i)
        self.query_text, self.key, self.offset = query, noun_key(query), offset
        self.include_near.set(include_near)
        self.word.set(query)
        self.update_matches()

    def open_file(self):
        if self.app.load_saved(parent=self):
            self.destroy()

    def edit_record(self):
        path = self.records[self.selected].path
        try:
            safe = result_file(RESULTS, path.relative_to(RESULTS), {'.json'})
            if not safe:
                raise ValueError('記録が移動または削除されています。')
            self.app.display_result(safe)
            if self.focus_row is not None and self.focus_row < len(self.app.segments):
                self.app.table.selection_set(str(self.focus_row))
                self.app.table.see(str(self.focus_row))
        except (OSError, ValueError, TypeError, KeyError) as exc:
            messagebox.showerror('開けません', str(exc), parent=self)
            return
        self.destroy()


def open_library(app):
    if app.proc:
        return
    try:
        app.persist_edits()
        TranscriptLibrary(app)
    except (OSError, ValueError) as exc:
        messagebox.showerror('保存した記録を開けません', str(exc), parent=app.root)
