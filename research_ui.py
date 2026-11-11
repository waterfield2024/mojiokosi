"""Question-driven retrieval with citations to local transcripts and articles."""
from contextlib import closing
import gc
from pathlib import Path
import queue
import re
import sqlite3
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from engine import RESULTS, timestamp
from research_library import (KINDS, ORDERS, PAGE_SIZE, QUESTION_LIMIT, REASON_FILTERS, ResearchIndex,
                              ResearchSession, SemanticSearchError, collect_sources, import_text, save_article, summarize)
from research_vectors import load_encoder
from transcript_library import load_records
from ui_theme import palette, style_text


_summary_lock = threading.Lock()


def index_worker(root, snapshot, requests, events, stop):
    def prepare_model():
        try:
            model = load_encoder(stop.is_set)
            try:
                requests.put(('model', 0, model))
                stop.wait()
            finally:
                model.close()
        except Exception as exc:
            requests.put(('model_error', 0, str(exc)))

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
            encoder = batches = None
            started = vectors_ready = False
            sources = {}
            generation = processed = 0

            def vector_error(exc):
                events.put(('vector_status', generation, f'意味検索を準備できません。全文検索を使えます。'
                            f'　画面を開き直すと再試行します。({exc})'))

            while not stop.is_set():
                if batches is None:
                    action, number, args = requests.get()
                else:
                    try:
                        action, number, args = requests.get_nowait()
                    except queue.Empty:
                        try:
                            processed += next(batches)
                            events.put(('vector_status', generation, f'意味検索の索引を作成中… {processed} 件処理'))
                        except StopIteration:
                            batches = None
                            vectors_ready = True
                            events.put(('vectors_ready', generation, '全文検索と意味検索を利用できます。'))
                        except Exception as exc:
                            batches = encoder = None
                            vector_error(exc)
                        continue
                if action == 'close':
                    return
                try:
                    if action == 'refresh':
                        generation, processed = number, 0
                        vectors_ready = False
                        sources, skipped = collect_sources(root, snapshot[0])
                        index.sync(sources, stop.is_set,
                                   lambda n, total: events.put(('progress', number, f'検索索引を準備中… {n} / {total} 資料')))
                        batches = index.vector_batches(sources, encoder, stop.is_set) if encoder else None
                        events.put(('ready', number, (sources, skipped, snapshot)))
                    elif action == 'model':
                        encoder = args
                        batches = index.vector_batches(sources, encoder, stop.is_set)
                    elif action == 'model_error':
                        vector_error(args)
                    elif action == 'search':
                        question, kind, order, hybrid = args
                        if hybrid and question and sources and not started:
                            started = True
                            events.put(('vector_status', generation, '意味検索モデルを準備中（初回は約154 MB）。全文検索を使えます。'))
                            threading.Thread(target=prepare_model, daemon=True).start()
                        active = encoder if hybrid and vectors_ready else None
                        try:
                            result = index.search_pages_with_counts(question, kind, order, encoder=active, limit=None)
                        except SemanticSearchError as exc:
                            vectors_ready = False
                            vector_error(exc)
                            result = index.search_pages_with_counts(question, kind, order, limit=None)
                        # Full-text results can arrive before the semantic matches change every filter's count.
                        provisional = bool(hybrid and question and sources and not vectors_ready)
                        events.put(('lexical_hits' if provisional else 'hits', number, result))
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
        self.pages = {}
        session = ResearchSession.load()
        self.offset_by_reason = {reason: (n - 1) * PAGE_SIZE for reason, n in session.pages.items()}
        self.offset = 0
        self.requests, self.events = queue.Queue(), queue.Queue()
        self.stop = threading.Event()
        self.number = 0
        self.saved_key = self.ai_job = None
        self.last_question = ''
        self.ready = False
        self.refresh_number = 0
        self.last_request = (session.question, session.kind, session.order, session.hybrid) if session.question else None
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
        self.question.insert(0, session.question)
        self.question.pack(side='left', fill='x', expand=True)
        self.question.bind('<Return>', self.search)
        self.kind = ttk.Combobox(bar, values=KINDS, width=5, state='readonly')
        self.order = ttk.Combobox(bar, values=ORDERS, width=8, state='readonly')
        for box, value in ((self.kind, session.kind), (self.order, session.order)):
            box.set(value)
            box.pack(side='left', padx=(5, 0))
            box.bind('<<ComboboxSelected>>', self.search)
        self.search_btn = ttk.Button(bar, text='調べる', command=self.search, state='disabled')
        self.search_btn.pack(side='left', padx=(5, 0))
        options = ttk.Frame(frame)
        options.pack(fill='x', pady=(5, 0))
        self.hybrid = tk.BooleanVar(self, value=session.hybrid)
        ttk.Checkbutton(options, text='意味の近い表現も探す', variable=self.hybrid,
                        command=lambda: self.search() if self.question.get().strip() else None).pack(side='left')
        self.vector_status = tk.StringVar(self, value='初回の検索で意味検索モデルを準備します。')
        ttk.Label(options, textvariable=self.vector_status, wraplength=520).pack(side='left', padx=8)
        self.status = tk.StringVar(self)
        result_bar = ttk.Frame(frame)
        result_bar.pack(fill='x', pady=6)
        ttk.Label(result_bar, text='一致理由').pack(side='left')
        self.reason = ttk.Combobox(result_bar, values=REASON_FILTERS, width=26, state='readonly')
        self.reason.current(REASON_FILTERS.index(session.reason))
        self.reason.pack(side='left', padx=(4, 10))
        self.reason.bind('<<ComboboxSelected>>', self.filter_results)
        ttk.Label(result_bar, textvariable=self.status, wraplength=470).pack(side='left')
        panes = ttk.Panedwindow(frame, orient='vertical')
        panes.pack(fill='both', expand=True)
        upper = ttk.Frame(panes)
        lower = ttk.Frame(panes)
        panes.add(upper, weight=1)
        panes.add(lower, weight=1)
        from transcript_library_ui import record_table
        font = ttk.Style(self).lookup('Treeview', 'font') or 'TkDefaultFont'
        reason_width = int(self.tk.call('font', 'measure', font, 'キーワード一致')) + 16
        self.table = record_table(upper, [('番号', 45), ('一致理由', reason_width), ('日付', 90), ('種類', 50),
                                          ('出典', 130), ('話者・位置', 120), ('原文の抜粋', 200)], 7)
        self.table.bind('<<TreeviewSelect>>', self.preview)
        self.table.bind('<ButtonRelease-1>', lambda e: self.preview() if self.detail_title.get().startswith('AIの回答') else None)
        self.table.bind('<Return>', self.open_source)
        self.table.bind('<Double-1>', self.open_source)
        self.pager = ttk.Frame(upper)
        self.prev_btn = ttk.Button(self.pager, text='前の100件', command=lambda: self.move_to_offset(self.offset - PAGE_SIZE))
        self.next_btn = ttk.Button(self.pager, text='次の100件', command=lambda: self.move_to_offset(self.offset + PAGE_SIZE))
        self.prev_btn.pack(side='left', padx=(0, 4))
        self.next_btn.pack(side='left')
        ttk.Label(self.pager, text='ページ番号').pack(side='left', padx=(12, 4))
        self.page_number = tk.StringVar(self)
        self.page_entry = ttk.Entry(self.pager, textvariable=self.page_number, width=6, justify='right')
        self.page_entry.pack(side='left')
        self.page_entry.bind('<FocusIn>', lambda event: self.page_entry.after_idle(self.page_entry.selection_range, 0, 'end'))
        self.page_entry.bind('<Return>', self.jump_to_page)
        self.page_entry.bind('<KP_Enter>', self.jump_to_page)
        self.page_range = tk.StringVar(self)
        ttk.Label(self.pager, textvariable=self.page_range).pack(side='left', padx=4)
        self.jump_btn = ttk.Button(self.pager, text='移動', command=self.jump_to_page)
        self.jump_btn.pack(side='left')
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
        # Dispose of cycles from previously closed Tk dialogs on their owning thread.
        gc.collect()
        threading.Thread(target=index_worker, args=(RESULTS, self.snapshot, self.requests, self.events, self.stop),
                         daemon=True).start()
        self.refresh()
        self.timer = self.after(40, self.poll)

    def destroy(self):
        if not getattr(self.app, '_restart_after_restore', False):
            request = self.last_request or ('', self.kind.get(), self.order.get(), self.hybrid.get())
            pages = {reason: offset // PAGE_SIZE + 1 for reason, offset in self.offset_by_reason.items()}
            try:
                ResearchSession(*request, REASON_FILTERS[self.reason.current()], pages).save()
            except (OSError, ValueError) as exc:
                self.app.status.set(f'調べる画面の検索条件を保存できませんでした。({exc})')
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
        if saved is not None:
            self.offset_by_reason.clear()
            self.offset = 0
        self.set_pages({})
        self.refresh_number = self.number
        self.ready = False
        self.search_btn.configure(state='disabled')
        self.saved_key = saved
        self.display([])
        self.status.set('記事を保存しました。検索に反映中…' if saved else '発言と記事の検索索引を準備中…')
        self.requests.put(('refresh', self.number, ()))

    def current_request(self):
        return (self.question.get().strip(), self.kind.get(), self.order.get(), self.hybrid.get())

    def search(self, event=None, *, keep_page=False):
        if not self.ready:
            return
        request = self.current_request()
        if len(request[0]) > QUESTION_LIMIT:
            self.status.set(f'質問は{QUESTION_LIMIT}文字以内で入力してください。')
            return
        self.number += 1
        if not keep_page:
            self.offset_by_reason.clear()
            self.offset = 0
        self.set_pages({})
        self.last_request = request
        self.last_question = request[0]
        self.display([])
        self.status.set('検索中…')
        self.requests.put(('search', self.number, self.last_request))

    def poll(self):
        while True:
            try:
                op, number, value = self.events.get_nowait()
            except queue.Empty:
                break
            if op in ('vector_status', 'vectors_ready'):
                if number == self.refresh_number:
                    self.vector_status.set(value)
                    current = self.current_request()
                    if (op == 'vectors_ready' and self.hybrid.get() and current == self.last_request
                            and self.state() != 'withdrawn' and self.ai_job is None):
                        self.search(keep_page=True)
                continue
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
                    self.search(keep_page=self.current_request() == self.last_request)
                elif self.saved_key in self.sources:
                    self.display([(self.saved_key, 0, '')])
                self.saved_key = None
            elif op in ('hits', 'lexical_hits'):
                self.set_pages(value, provisional=op == 'lexical_hits')
                self.filter_results()
            elif op == 'summary':
                self.show_summary(value)
            elif op in ('progress', 'error', 'unavailable'):
                self.status.set(value)
        self.timer = self.after(250 if self.state() == 'withdrawn' else 80, self.poll)

    def set_pages(self, pages, *, provisional=False):
        selected = self.reason.current()
        self.pages = pages
        self.provisional = provisional
        labels = [f'{reason}（{pages[reason].total:,}件）' for reason in REASON_FILTERS] if pages else REASON_FILTERS
        self.reason.configure(values=labels)
        self.reason.current(selected)
        if not pages:
            self.pager.pack_forget()
            self.prev_btn.state(['disabled'])
            self.next_btn.state(['disabled'])

    def filter_results(self, event=None):
        if not self.pages:
            return
        if event is not None:
            # AI citation numbers belong to the previously displayed rows.
            self.number += 1
        self.show_page()

    def jump_to_page(self, event=None):
        if not self.pages:
            return 'break'
        total = self.pages[REASON_FILTERS[self.reason.current()]].total
        last_page = (total + PAGE_SIZE - 1) // PAGE_SIZE
        value = self.page_number.get().strip()
        try:
            target = int(value) if value.isdecimal() else 0
        except ValueError:
            target = 0
        if 1 <= target <= last_page:
            self.move_to_offset((target - 1) * PAGE_SIZE)
            if event is not None:
                self.page_entry.selection_range(0, 'end')
        elif last_page:
            self.page_range.set(f'1～{last_page:,}の整数を入力してください')
            self.page_entry.focus_set()
            self.page_entry.selection_range(0, 'end')
        return 'break'

    def move_to_offset(self, new_offset):
        if not self.pages:
            return
        if 0 <= new_offset < self.pages[REASON_FILTERS[self.reason.current()]].total:
            self.number += 1
            self.show_page(new_offset)

    def show_page(self, offset=None):
        reason = REASON_FILTERS[self.reason.current()]
        page = self.pages[reason]
        last_page = (page.total + PAGE_SIZE - 1) // PAGE_SIZE
        last_offset = max(0, last_page - 1) * PAGE_SIZE
        if offset is None:
            offset = self.offset_by_reason.get(reason, 0)
        self.offset = min(offset, last_offset)
        self.offset_by_reason[reason] = offset if self.provisional else self.offset
        self.page_number.set(str(self.offset // PAGE_SIZE + 1))
        self.page_range.set(f'／ 全{last_page:,}ページ')
        hits = page.hits[self.offset:self.offset + PAGE_SIZE]
        self.prev_btn.state(['!disabled'] if self.offset else ['disabled'])
        self.next_btn.state(['!disabled'] if self.offset < last_offset else ['disabled'])
        if page.total > PAGE_SIZE:
            self.pager.pack(side='bottom', anchor='e', pady=(3, 0), before=self.table.master)
        else:
            self.pager.pack_forget()
        self.display(hits)
        if hits:
            shown = f'{len(hits)} 件を表示'
            if page.total > PAGE_SIZE:
                shown = f'{page.total:,} 件中 {self.offset + 1:,}～{self.offset + len(hits):,} 件を表示'
            self.status.set(('全文検索は一部の検索語で照合　' if page.partial else '') + shown)
        elif reason in ('意味が近い', '両方') and not self.hybrid.get():
            self.status.set('この一致理由で探すには「意味の近い表現も探す」をオンにしてください。')
        elif self.pages['すべて'].total:
            self.status.set(f'「{reason}」は0件です。「すべて」でほかの結果を表示できます。')
        else:
            self.status.set('該当する資料は見つかりませんでした。話題や年代を変えて検索できます。')

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
        references = {f'[{self.offset + i + 1}]': i for i in range(len(self.hits))}
        for part in re.split(r'(\[\d+\])', answer):
            if part in references:
                index = references[part]
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
        self.hits = [(key, row) for key, row, _ in hits]
        self.table.delete(*self.table.get_children())
        self.detail_title.set('引用と前後の原文')
        self.set_detail('')
        self.open_btn.configure(state='normal' if hits else 'disabled')
        self.enable_ai()
        for i, (key, row, reason) in enumerate(hits):
            src = self.sources[key]
            self.table.insert('', 'end', iid=str(i), values=(f'[{self.offset+i+1}]', reason, src.day or '日付不明', src.kind,
                                                            src.title, location(src, row), src.passages[row].text[:180]))
        if hits:
            self.table.yview_moveto(0)
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
        self.set_detail(f'[{self.offset+i+1}] {src.title}　{src.day or "日付不明"}　{src.credit}\n\n')
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
            evidence.append(dict(id=self.offset+i+1, kind=src.kind, title=src.title[:200], date=src.day or '日付不明',
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
