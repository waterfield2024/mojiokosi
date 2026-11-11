"""Question-driven retrieval with citations to local transcripts and articles."""
from contextlib import closing
from pathlib import Path
import queue
import re
import sqlite3
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from engine import RESULTS, timestamp
from research_library import KINDS, ORDERS, ResearchIndex, collect_sources, import_text, save_article, summarize
from transcript_library import load_records
from ui_theme import palette, style_text


_summary_lock = threading.Lock()


def index_worker(root, snapshot, requests, events, stop):
    try:
        from janome.tokenizer import Tokenizer
        if snapshot is None:
            snapshot = load_records(root, stop.is_set)
        if stop.is_set():
            return
        cache = root.parent / '.research-cache'
        cache.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(cache / 'search-v1.sqlite3', timeout=15)) as db:
            index = ResearchIndex(db, Tokenizer())
            while not stop.is_set():
                action, number, args = requests.get()
                if action == 'close':
                    return
                try:
                    if action == 'refresh':
                        sources, skipped = collect_sources(root, snapshot[0])
                        index.sync(sources, stop.is_set,
                                   lambda n, total: events.put(('progress', number, f'検索索引を準備中… {n} / {total} 資料')))
                        events.put(('ready', number, (sources, skipped, snapshot)))
                    else:
                        events.put(('hits', number, index.search(*args)))
                except InterruptedError:
                    return
                except (sqlite3.Error, OSError, ValueError) as exc:
                    events.put(('error', number, str(exc)))
    except Exception as exc:
        events.put(('unavailable', 0, str(exc)))


def location(src, row):
    p = src.passages[row]
    return f'{p.speaker or "話者未設定"}　{timestamp(p.start)}' if src.kind == '発言' else f'本文 {row+1}'


def article_dialog(parent):
    dialog = tk.Toplevel(parent)
    dialog.title('記事を保存')
    dialog.transient(parent)
    frame = ttk.Frame(dialog, padding=12)
    frame.pack(fill='both', expand=True)
    frame.columnconfigure(1, weight=1)
    frame.rowconfigure(4, weight=1)
    fields = []
    for row, label in enumerate(('記事名', '記事の日付（任意・YYYY-MM-DD）', '出典・URL（任意）')):
        ttk.Label(frame, text=label).grid(row=row, column=0, sticky='w', pady=3)
        entry = ttk.Entry(frame, width=45)
        entry.grid(row=row, column=1, sticky='ew', padx=(6, 0))
        fields.append(entry)
    ttk.Label(frame, text='本文').grid(row=3, column=0, sticky='w', pady=(8, 3))
    body = tk.Text(frame, wrap='word', width=68, height=15, undo=True)
    style_text(body)
    body.grid(row=4, column=0, columnspan=2, sticky='nsew')

    def load_file():
        name = filedialog.askopenfilename(parent=dialog, filetypes=[('テキスト・Markdown', ('*.txt', '*.md'))])
        if not name:
            return
        try:
            content = import_text(Path(name))
        except (ValueError, OSError) as exc:
            messagebox.showerror('ファイルを読めません', str(exc), parent=dialog)
            return
        body.delete('1.0', 'end')
        body.insert('1.0', content)
        if not fields[0].get().strip():
            fields[0].insert(0, Path(name).stem)

    def close():
        dialog.destroy()
        parent.grab_set()

    def save():
        try:
            key = save_article(RESULTS, *(field.get() for field in fields), body.get('1.0', 'end-1c'))
        except (ValueError, OSError) as exc:
            messagebox.showerror('記事を保存できません', str(exc), parent=dialog)
            return
        close()
        parent.refresh(key)

    ttk.Button(frame, text='ファイルから本文を入力…', command=load_file).grid(row=3, column=1, sticky='e', pady=4)
    ttk.Button(frame, text='記事を保存', command=save).grid(row=5, column=1, sticky='e', pady=(8, 0))
    dialog.protocol('WM_DELETE_WINDOW', close)
    dialog.grab_set()
    fields[0].focus_set()


class ResearchWindow(tk.Toplevel):
    def __init__(self, app, *, library=None):
        super().__init__(library if library is not None else app.root)
        self.app = app
        self.library = library
        self.snapshot = (tuple(library.records), library.skipped) if library is not None else None
        self.transient(self.master)
        self.title('過去の発言・記事を調べる')
        self.geometry('960x630')
        self.minsize(630, 440)
        self.sources, self.hits = {}, []
        self.requests, self.events = queue.Queue(), queue.Queue()
        self.stop = threading.Event()
        self.number = 0
        self.saved_key = self.ai_job = None
        self.last_question = ''
        self.ready = False
        frame = ttk.Frame(self, padding=12)
        frame.pack(fill='both', expand=True)
        top = ttk.Frame(frame)
        top.pack(fill='x')
        ttk.Label(top, text='過去の発言・記事を調べる', font=('Yu Gothic UI', 14, 'bold')).pack(side='left')
        ttk.Button(top, text='記事を追加…', command=lambda: article_dialog(self)).pack(side='right')
        ttk.Label(frame, text='質問やキーワードを入力　例「昨年、予算について何と言っていた？」').pack(anchor='w', pady=(10, 3))
        bar = ttk.Frame(frame)
        bar.pack(fill='x')
        self.question = ttk.Entry(bar)
        self.question.pack(side='left', fill='x', expand=True)
        self.question.bind('<Return>', self.search)
        self.kind = ttk.Combobox(bar, values=KINDS, width=5, state='readonly')
        self.order = ttk.Combobox(bar, values=ORDERS, width=8, state='readonly')
        for box in (self.kind, self.order):
            box.current(0)
            box.pack(side='left', padx=(5, 0))
            box.bind('<<ComboboxSelected>>', self.search)
        self.search_btn = ttk.Button(bar, text='調べる', command=self.search, state='disabled')
        self.search_btn.pack(side='left', padx=(5, 0))
        self.status = tk.StringVar(self)
        ttk.Label(frame, textvariable=self.status, wraplength=860).pack(anchor='w', pady=6)
        panes = ttk.Panedwindow(frame, orient='vertical')
        panes.pack(fill='both', expand=True)
        upper = ttk.Frame(panes)
        lower = ttk.Frame(panes)
        panes.add(upper, weight=1)
        panes.add(lower, weight=1)
        from transcript_library_ui import record_table
        self.table = record_table(upper, [('番号', 45), ('日付', 95), ('種類', 55), ('出典', 155),
                                          ('話者・位置', 150), ('原文の抜粋', 200)], 7)
        self.table.bind('<<TreeviewSelect>>', self.preview)
        self.table.bind('<ButtonRelease-1>', lambda e: self.preview() if self.detail_title.get().startswith('AIの回答') else None)
        self.table.bind('<Return>', self.open_source)
        self.table.bind('<Double-1>', self.open_source)
        bar = ttk.Frame(lower)
        bar.pack(fill='x', pady=5)
        self.detail_title = tk.StringVar(self, value='引用と前後の原文')
        ttk.Label(bar, textvariable=self.detail_title).pack(side='left')
        self.open_btn = ttk.Button(bar, text='出典を開く', command=self.open_source, state='disabled')
        self.open_btn.pack(side='right')
        self.ai_btn = ttk.Button(bar, text='引用からAIで回答', command=self.ask_ai, state='disabled')
        self.ai_btn.pack(side='right', padx=5)
        self.detail = tk.Text(lower, height=8, wrap='word', state='disabled', padx=8, pady=5)
        style_text(self.detail)
        self.detail.tag_configure('quoted', font=('Yu Gothic UI', 10, 'bold'))
        self.detail.bind('<ButtonRelease-1>', self.follow_citation)
        scroll = ttk.Scrollbar(lower, command=self.detail.yview)
        self.detail.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        self.detail.pack(fill='both', expand=True)
        self.grab_set()
        self.question.focus_set()
        threading.Thread(target=index_worker, args=(RESULTS, self.snapshot, self.requests, self.events, self.stop),
                         daemon=True).start()
        self.refresh()
        self.timer = self.after(40, self.poll)

    def destroy(self):
        self.stop.set()
        self.requests.put(('close', 0, ()))
        self.after_cancel(self.timer)
        if self.library is not None:
            self.library.research_window = None
        super().destroy()
        if self.library is not None and self.library.winfo_exists():
            self.library.grab_set()

    def refresh(self, saved=None):
        self.number += 1
        self.ready = False
        self.search_btn.configure(state='disabled')
        self.saved_key = saved
        self.display([])
        self.status.set('記事を保存しました。検索に反映中…' if saved else '発言と記事の検索索引を準備中…')
        self.requests.put(('refresh', self.number, ()))

    def search(self, event=None):
        if not self.ready:
            return
        self.number += 1
        self.last_question = self.question.get().strip()
        self.display([])
        self.status.set('検索中…')
        self.requests.put(('search', self.number, (self.last_question, self.kind.get(), self.order.get())))

    def poll(self):
        while True:
            try:
                op, number, value = self.events.get_nowait()
            except queue.Empty:
                break
            if op == 'summary':
                self.ai_job = None
                self.enable_ai()
            if number != self.number and op != 'unavailable':
                continue
            if op == 'ready':
                self.ready = True
                self.search_btn.configure(state='normal')
                self.sources, skipped, self.snapshot = value
                articles = sum(s.kind == '記事' for s in self.sources.values())
                self.status.set(f'記録 {len(self.sources)-articles} 件・記事 {articles} 件を検索できます。'
                                + (f'　読み込めない記録 {self.snapshot[1]} 件' if self.snapshot[1] else '')
                                + (f'　読み込めない記事 {skipped} 件' if skipped else ''))
                if self.question.get().strip():
                    self.search()
                elif self.saved_key in self.sources:
                    self.display([(self.saved_key, 0)])
                self.saved_key = None
            elif op == 'hits':
                hits, partial, more = value
                self.display(hits)
                self.status.set(('一部の検索語に一致　' if partial else '') +
                                (f'{len(hits)} 件を表示' + ('（さらに候補があります）' if more else '') if hits
                                 else '該当する資料は見つかりませんでした。話題や年代を変えて検索できます。'))
            elif op == 'summary':
                self.show_summary(value)
            elif op in ('progress', 'error', 'unavailable'):
                self.status.set(value)
        self.timer = self.after(250 if self.state() == 'withdrawn' else 80, self.poll)

    def set_detail(self, text):
        self.detail.configure(state='normal')
        self.detail.delete('1.0', 'end')
        self.detail.insert('1.0', text)
        self.detail.configure(state='disabled')

    def enable_ai(self):
        self.ai_btn.configure(state='normal' if self.hits and self.last_question and self.ai_job is None else 'disabled')

    def show_summary(self, answer):
        self.detail_title.set('AIの回答（番号をクリックすると原文を表示）')
        self.set_detail('')
        self.detail.configure(state='normal')
        for part in re.split(r'(\[\d{1,3}\])', answer):
            if re.fullmatch(r'\[\d{1,3}\]', part) and 1 <= int(part[1:-1]) <= len(self.hits):
                index = int(part[1:-1]) - 1
                tag = f'ref_{index}'
                self.detail.tag_configure(tag, foreground=palette(self)['accent'], underline=True)
                self.detail.insert('end', part, tag)
            else:
                self.detail.insert('end', part)
        self.detail.configure(state='disabled')

    def follow_citation(self, event):
        for tag in self.detail.tag_names(f'@{event.x},{event.y}'):
            if tag.startswith('ref_'):
                iid = tag[4:]
                self.table.selection_set(iid)
                self.table.see(iid)
                self.table.focus(iid)
                self.preview()
                return 'break'

    def display(self, hits):
        self.hits = hits
        self.table.delete(*self.table.get_children())
        self.detail_title.set('引用と前後の原文')
        self.set_detail('')
        self.open_btn.configure(state='normal' if hits else 'disabled')
        self.enable_ai()
        for i, (key, row) in enumerate(hits):
            src = self.sources[key]
            self.table.insert('', 'end', iid=str(i), values=(f'[{i+1}]', src.day or '日付不明', src.kind, src.title,
                                                            location(src, row), src.passages[row].text[:180]))
        if hits:
            self.table.selection_set('0')
            self.table.focus('0')
            self.preview()

    def preview(self, event=None, *, full=False):
        selected = self.table.selection()
        if not selected:
            return
        i = int(selected[0])
        key, row = self.hits[i]
        src = self.sources[key]
        self.detail_title.set('記事の原文' if full else '引用と前後の原文')
        self.set_detail(f'[{i+1}] {src.title}　{src.day or "日付不明"}　{src.credit}\n\n')
        self.detail.configure(state='normal')
        for n in range(0 if full else max(0, row-1), len(src.passages) if full else min(len(src.passages), row+2)):
            if n == row:
                self.detail.mark_set('citation', 'end-1c')
                self.detail.mark_gravity('citation', 'left')
            self.detail.insert('end', location(src, n) + '\n' + src.passages[n].text + '\n\n', 'quoted' if n == row else ())
        self.detail.configure(state='disabled')
        self.detail.see('citation')

    def open_source(self, event=None):
        selection = self.table.selection()
        if not selection:
            return
        key, row = self.hits[int(selection[0])]
        if self.sources[key].kind == '記事':
            self.preview(full=True)
            return
        for i, record in enumerate(self.snapshot[0]):
            if record.path.relative_to(RESULTS).as_posix() == key:
                if self.library is None:
                    from transcript_library_ui import TranscriptLibrary
                    self.library = TranscriptLibrary(self.app, snapshot=self.snapshot, citation=(i, row))
                    self.library.research_window = self
                    self.transient(self.library)
                else:
                    self.library.choose_record(i)
                    self.library.highlight_row(row)
                self.withdraw()
                self.library.grab_set()
                return

    def ask_ai(self):
        if self.ai_job is not None or not self.hits or not self.last_question:
            return
        evidence = []
        # Include different sources before taking more quotes from the same meeting.
        first_in_source = {}
        for i, (key, _) in enumerate(self.hits):
            first_in_source.setdefault(key, i)
        chosen = list(dict.fromkeys([*first_in_source.values(), *range(len(self.hits))]))[:8]
        for i in chosen:
            key, row = self.hits[i]
            src = self.sources[key]
            evidence.append(dict(id=i+1, kind=src.kind, title=src.title[:200], date=src.day or '日付不明',
                                 location=location(src, row), text=src.passages[row].text[:800]))
        question, number = self.last_question, self.number
        self.ai_job = number
        self.enable_ai()
        self.detail_title.set('引用からAIで回答')
        self.set_detail('PC内のAIが引用を確認しています…')

        def run():
            try:
                with _summary_lock:
                    if self.stop.is_set():
                        return
                    result = summarize(question, evidence)
            except Exception as exc:
                result = f'AIで回答を作成できませんでした。上の検索結果から原文を確認できます。\n{exc}'
            self.events.put(('summary', number, result))
        threading.Thread(target=run, daemon=True).start()
