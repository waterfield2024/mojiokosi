"""In-app update flow; no personal data is sent to GitHub."""
from datetime import datetime
import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from app_update import check_latest, current_version, prepare_update


class UpdateDialog(tk.Toplevel):
    def __init__(self, app):
        from engine import ROOT
        super().__init__(app.root)
        self.app, self.installation = app, ROOT
        self.busy, self.closed, self.release = False, False, None
        self.events = queue.Queue()
        self.title('GitHubから最新版に更新')
        self.transient(app.root)
        self.protocol('WM_DELETE_WINDOW', self.close)
        frame = ttk.Frame(self, padding=24)
        frame.pack(fill='both', expand=True)
        ttk.Label(frame, text='Whisperを最新版に更新', font=('Yu Gothic UI', 16, 'bold')).pack(anchor='w')
        ttk.Label(frame, text='更新元：waterfield2024 / mojiokosi（main）').pack(anchor='w', pady=(12, 4))
        ttk.Label(frame, text=f'現在の更新番号：{current_version(ROOT)}').pack(anchor='w')
        self.detail = tk.StringVar(value='最新版を確認しています…')
        ttk.Label(frame, textvariable=self.detail, wraplength=540, justify='left').pack(fill='x', pady=18)
        ttk.Separator(frame).pack(fill='x')
        ttk.Label(frame, text='設定・話者の名前・学習音声・誤変換辞書・録音はそのまま残ります。\n'
                  'モデルも引き継ぎ、必要なライブラリだけ自動で準備します。\n'
                  '更新が終わるとWhisperが自動で開き直します。',
                  wraplength=540, justify='left').pack(fill='x', pady=16)
        self.status = tk.StringVar()
        ttk.Label(frame, textvariable=self.status, wraplength=540).pack(fill='x', pady=(0, 10))
        self.progress = ttk.Progressbar(frame, mode='indeterminate')
        self.progress.pack(fill='x')
        buttons = ttk.Frame(frame)
        buttons.pack(fill='x', pady=(18, 0))
        self.check_button = ttk.Button(buttons, text='再確認', command=self.check)
        self.check_button.pack(side='left')
        self.update_button = ttk.Button(buttons, text='更新して再起動', style='Accent.TButton', command=self.install)
        self.update_button.pack(side='right')
        self.close_button = ttk.Button(buttons, text='閉じる', command=self.close)
        self.close_button.pack(side='right', padx=8)
        self.minsize(590, 380)
        self.grab_set()
        self.timer = self.after(100, self.poll)
        self.check()

    def run(self, kind, work):
        self.busy = True
        for button in (self.check_button, self.update_button, self.close_button):
            button.configure(state='disabled')
        self.progress.start(15)
        def worker():
            try:
                self.events.put((kind, work()))
            except Exception as exc:
                self.events.put(('error', str(exc)))
        threading.Thread(target=worker, daemon=True).start()

    def idle(self):
        self.busy = False
        self.progress.stop()
        self.progress.configure(value=0)
        self.check_button.configure(state='normal')
        self.close_button.configure(state='normal')
        self.update_button.configure(state='normal' if self.release and self.release['changes'] else 'disabled')

    def check(self):
        if self.busy:
            return
        self.release = None
        self.detail.set('最新版を確認しています…')
        self.status.set('GitHubに接続しています。')
        self.run('checked', lambda: check_latest(self.installation))

    def install(self):
        if self.busy or not self.release or not self.release['changes']:
            return
        self.status.set('更新を準備しています。完了後にアプリを再起動します…')
        self.run('prepared', lambda: prepare_update(self.installation, self.release,
                 progress=lambda text: self.events.put(('progress', text))))

    def poll(self):
        if self.closed:
            return
        while True:
            try:
                kind, value = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == 'progress':
                self.status.set(value)
                continue
            self.idle()
            if kind == 'error':
                self.status.set(value)
                self.detail.set('更新を完了できませんでした。アプリと保存データは変更していません。')
            elif kind == 'checked':
                self.release = value
                try:
                    date = datetime.fromisoformat(value['date'].replace('Z', '+00:00')).astimezone().strftime('%Y/%m/%d %H:%M')
                except (ValueError, TypeError):
                    date = value['date']
                self.detail.set(f'最新の更新番号：{value["sha"][:8]}　／　{date}\n{value["message"][:250]}')
                self.status.set(f'更新できます（{value["changes"]}ファイル）。' if value['changes'] else '最新版です。更新は必要ありません。')
                self.idle()
            elif kind == 'prepared':
                try:
                    self.app.commit()
                    self.app.persist_edits()
                    self.app.window_settings.flush_all()
                except (OSError, ValueError) as exc:
                    self.status.set(f'修正内容を保存できなかったため、更新を中止しました。\n{exc}')
                    continue
                self.app._pending_app_update = value
                self.close(resume=False)
                self.app.close()
                return
        self.timer = self.after(100, self.poll)

    def close(self, resume=True):
        if self.busy or self.closed:
            return
        self.closed = True
        self.after_cancel(self.timer)
        self.grab_release()
        self.app.update_dialog = None
        self.app.data_transfer_active = False
        self.destroy()
        if resume:
            self.app.root.after(0, self.app.resume_saved_voice_pools)


def open_update(app):
    dialog = getattr(app, 'update_dialog', None)
    if dialog and dialog.winfo_exists():
        dialog.lift()
        return
    if (app.proc or app.correction_pending or getattr(app, 'data_transfer_active', False)
            or any(p.get('extracting') for p in app.memory_pending.values())):
        messagebox.showinfo('アプリの更新', '文字起こし・録画・話者の学習・バックアップが終わってから開いてください。', parent=app.root)
        return
    preview, review = getattr(app, 'video_preview', None), getattr(app, 'voice_review', None)
    if (preview and preview.winfo_exists()) or (review and not review.closed):
        messagebox.showinfo('アプリの更新', 'MP4プレビューと学習音声の画面を閉じてから開いてください。', parent=app.root)
        return
    try:
        app.commit()
        app.persist_edits()
        app.window_settings.flush_all()
        if app.correction_pending or any(p.get('extracting') for p in app.memory_pending.values()):
            messagebox.showinfo('アプリの更新', '修正した話者を覚えています。完了後に開いてください。', parent=app.root)
            return
        app.data_transfer_active = True
        app.update_dialog = UpdateDialog(app)
    except Exception as exc:
        app.data_transfer_active = False
        messagebox.showerror('アプリの更新', str(exc), parent=app.root)
