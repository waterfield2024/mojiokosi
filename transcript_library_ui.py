"""Browse saved transcripts and follow a noun to an utterance in another recording."""
from collections import Counter, defaultdict
import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from engine import RESULTS, timestamp
from result_media import result_file
from transcript_library import index_nouns, load_records, noun_key, related_rows
from ui_theme import palette, style_text


def scan_library(root, events, stop):
    try:
        records, skipped = load_records(root, stop.is_set)
        events.put(('records', (records, skipped)))
        if records and not stop.is_set():
            from janome.tokenizer import Tokenizer
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
        self.origin = self.focus_row = None
        self.matches = []
        self.offset = self.skipped = 0
        self.done = False
        self.stop = threading.Event()
        self.events = queue.Queue()
        self.timer = None

        outer = ttk.Frame(self, padding=12)
        outer.pack(fill='both', expand=True)
        header = ttk.Frame(outer)
        header.pack(fill='x', pady=(0, 8))
        ttk.Label(header, text='保存した記録', font=('Yu Gothic UI', 15, 'bold')).pack(side='left')
        ttk.Button(header, text='ファイルから開く…', command=self.open_file).pack(side='right')
        self.edit = ttk.Button(header, text='メイン画面で編集', command=self.edit_record, state='disabled')
        self.edit.pack(side='right', padx=6)
        self.status = tk.StringVar(value='記録を読み込み中…')
        ttk.Label(outer, textvariable=self.status, style='Sub.TLabel').pack(side='bottom', anchor='w', pady=(6, 0))
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
        ttk.Label(bar, text='名詞でほかの記録を探す').pack(side='left')
        self.word = ttk.Combobox(bar, state='disabled', width=22)
        self.word.pack(side='left', fill='x', expand=True, padx=(8, 0))
        self.word.bind('<<ComboboxSelected>>', self.pick_word)
        self.note = tk.StringVar(value='本文の名詞をクリックするか、一覧から選んでください。')
        ttk.Label(related, textvariable=self.note, style='Sub.TLabel').pack(anchor='w', pady=4)
        actions = ttk.Frame(related)
        actions.pack(side='bottom', fill='x', pady=(5, 0))
        self.prev = ttk.Button(actions, text='前へ', command=lambda: self.page(-1), state='disabled')
        self.next = ttk.Button(actions, text='次へ', command=lambda: self.page(1), state='disabled')
        self.prev.pack(side='left')
        self.next.pack(side='left', padx=4)
        self.back = ttk.Button(actions, text='照合元に戻る', command=self.go_back)
        self.jump = ttk.Button(actions, text='該当する発言を表示', command=self.follow_hit, state='disabled')
        self.jump.pack(side='right')
        self.hits = record_table(related, [('日付', 86), ('記録名', 115), ('時刻・話者', 120), ('発言', 200)], 5)
        self.hits.bind('<<TreeviewSelect>>', lambda e: self.jump.configure(
            state='normal' if self.hits.selection() else 'disabled'))
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
            self.status.set(message)
        if changed:
            self.update_matches(keep_selection=True)
        if not self.done:
            self.timer = self.after(100, self.poll)

    def pick_record(self, event=None):
        selection = self.listing.selection()
        if selection:
            self.choose_record(int(selection[0]))

    def choose_record(self, i):
        if i == self.selected:
            return
        self.selected, self.key = i, None
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
        self.word.configure(values=values, state='readonly' if values else 'disabled')
        self.word.set(index.labels.get(self.key, '') if index else '')

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
        self.key = noun_key(self.word.get())
        self.offset = 0
        self.update_matches()

    def update_matches(self, keep_selection=False):
        selection = self.hits.selection() if keep_selection else ()
        self.matches = list(related_rows(self.records, self.indexes, self.selected, self.key)) if self.key else []
        self.offset = min(self.offset, max(0, (len(self.matches) - 1) // self.PAGE_SIZE * self.PAGE_SIZE))
        self.draw_hits()
        if selection and self.hits.exists(selection[0]):
            self.hits.selection_set(selection[0])

    def page(self, step):
        self.offset += step * self.PAGE_SIZE
        self.draw_hits()

    def draw_hits(self):
        self.hits.delete(*self.hits.get_children())
        end = min(self.offset + self.PAGE_SIZE, len(self.matches))
        for n in range(self.offset, end):
            i, row = self.matches[n]
            record = self.records[i]
            line = record.lines[row]
            self.hits.insert('', 'end', iid=str(n), values=(record.date.strftime('%Y/%m/%d'), record.title,
                                                          timestamp(line.start) + ' ' + line.speaker, line.text))
        self.jump.configure(state='disabled')
        self.prev.configure(state='normal' if self.offset else 'disabled')
        self.next.configure(state='normal' if end < len(self.matches) else 'disabled')
        if self.matches:
            self.note.set(f'{self.offset + 1}〜{end} / {len(self.matches)}件の発言')
        elif self.key:
            self.note.set('照合できた記録に一致する発言はありません。' if self.done else 'ほかの記録を照合中…')
        elif self.selected in self.indexes and not self.indexes[self.selected].labels:
            self.note.set('この記録からは名詞が見つかりませんでした。')
        else:
            self.note.set('本文の名詞をクリックするか、一覧から選んでください。')

    def follow_hit(self, event=None):
        selected = self.hits.selection()
        if selected:
            i, row = self.matches[int(selected[0])]
            origin = self.selected, self.key, self.offset
            self.choose_record(i)
            self.origin, self.focus_row = origin, row
            self.back.pack(side='left', padx=4)
            pos = f'{3 * row + 2}.0'
            self.body.tag_remove('match', '1.0', 'end')
            self.body.tag_add('match', pos, pos + ' lineend')
            self.body.see(pos)

    def go_back(self):
        i, key, offset = self.origin
        self.choose_record(i)
        self.key, self.offset = key, offset
        self.word.set(self.indexes[i].labels[key])
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
