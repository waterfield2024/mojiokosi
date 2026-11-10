"""A small backup/restore dialog; all replacement work happens before restart."""
from datetime import datetime
from pathlib import Path
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from data_backup import create_backup, inspect_backup, restore_backup
from restore_undo import undo_info, undo_restore


class BackupDialog(tk.Toplevel):
    def __init__(self, app):
        super().__init__(app.root)
        self.app = app
        self.busy = False
        self.closed = False
        self.events = queue.Queue()
        self.timer = None
        self.title('バックアップ・復元')
        self.geometry('640x520')
        self.minsize(600, 520)
        self.transient(app.root)
        self.protocol('WM_DELETE_WINDOW', self.close)
        frame = ttk.Frame(self, padding=22)
        frame.pack(fill='both', expand=True)
        ttk.Label(frame, text='設定と学習データを持ち運ぶ', font=('Yu Gothic UI', 15, 'bold')).pack(anchor='w')
        ttk.Label(frame, text='設定・話者の名前と学習音声・誤変換辞書・覚えた修正を\n1つのZIPファイルにまとめます。',
                  justify='left').pack(anchor='w', pady=(10, 12))
        self.include_results = tk.BooleanVar(value=False)
        self.results_check = ttk.Checkbutton(frame, text='録音・録画・文字起こし結果も含める', variable=self.include_results)
        self.results_check.pack(anchor='w', pady=(0, 10))
        self.backup_button = ttk.Button(frame, text='バックアップを保存…', style='Accent.TButton', command=self.backup)
        self.backup_button.pack(fill='x', pady=(0, 15))
        ttk.Separator(frame).pack(fill='x')
        ttk.Label(frame, text='別のPCではWhisperを起動し、下のボタンからZIPを選びます。',
                  wraplength=520).pack(anchor='w', pady=(12, 8))
        self.restore_button = ttk.Button(frame, text='バックアップを選んで復元…', command=self.restore)
        self.restore_button.pack(fill='x')
        ttk.Separator(frame).pack(fill='x', pady=(15, 10))
        self.undo_button = ttk.Button(frame, text='復元前の状態に戻す', command=self.undo)
        self.undo_button.pack(fill='x')
        self.undo_notice = tk.StringVar()
        ttk.Label(frame, textvariable=self.undo_notice, wraplength=520).pack(fill='x', pady=(6, 0))
        self.refresh_undo()
        self.status = tk.StringVar(value='復元前のデータは自動で保管します。復元後はアプリを再起動します。')
        ttk.Label(frame, textvariable=self.status, wraplength=520).pack(fill='x', pady=(12, 7))
        self.progress = ttk.Progressbar(frame, mode='determinate', value=0)
        self.progress.pack(fill='x')
        self.close_button = ttk.Button(frame, text='閉じる', command=self.close)
        self.close_button.pack(anchor='e', pady=(10, 0))
        self.update_idletasks()
        height = max(520, frame.winfo_reqheight() + 10)
        self.geometry(f'640x{height}')
        self.minsize(600, height)
        self.grab_set()
        self.timer = self.after(80, self.poll)

    def run(self, kind, work):
        self.busy = True
        for widget in (self.backup_button, self.restore_button, self.undo_button, self.results_check, self.close_button):
            widget.configure(state='disabled')
        self.progress.configure(mode='indeterminate')
        self.progress.start(12)
        events = self.events
        def progress(text): events.put(('progress', text))
        def worker():
            try: events.put((kind, work(progress)))
            except Exception as exc: events.put(('error', str(exc)))
        threading.Thread(target=worker, daemon=True).start()

    def idle(self):
        self.busy = False
        self.progress.stop()
        self.progress.configure(mode='determinate', value=0)
        for widget in (self.backup_button, self.restore_button, self.results_check, self.close_button):
            widget.configure(state='normal')
        self.refresh_undo()

    def refresh_undo(self):
        try:
            self.undo_state = undo_info()
        except (OSError, ValueError) as exc:
            self.undo_state = {'available': False, 'reason': str(exc)}
        if self.undo_state['available']:
            try:
                date = datetime.fromisoformat(self.undo_state['restored_at']).astimezone().strftime('%Y/%m/%d %H:%M')
                notice = f'{date} の復元を取り消して再起動します。'
            except (ValueError, TypeError):
                notice = '直前の復元を取り消して再起動します。'
            self.undo_notice.set(notice + '\n復元後の変更も戻ります。今のデータも自動保管します。')
        else:
            self.undo_notice.set(self.undo_state['reason'])
        self.undo_button.configure(state='normal' if self.undo_state['available'] and not self.busy else 'disabled')

    def undo(self):
        if self.busy or not self.undo_state['available']: return
        token = self.undo_state['id']
        self.status.set('復元前の状態に戻しています…')
        self.run('undone', lambda progress: undo_restore(expected_id=token, progress=progress))

    def backup(self):
        if self.busy: return
        path = filedialog.asksaveasfilename(parent=self, title='バックアップを保存', defaultextension='.zip',
            initialfile='Whisperバックアップ_' + datetime.now().strftime('%Y%m%d_%H%M%S') + '.zip',
            filetypes=[('Whisperバックアップ', '*.zip')])
        if not path: return
        include = self.include_results.get()
        self.status.set('保存データをまとめています…')
        self.run('saved', lambda progress: create_backup(path, include_results=include, progress=progress))

    def restore(self):
        if self.busy: return
        path = filedialog.askopenfilename(parent=self, title='復元するバックアップを選ぶ',
                                         filetypes=[('Whisperバックアップ', '*.zip')])
        if not path: return
        self.restore_path = path
        self.status.set('バックアップの内容を確認しています…')
        self.run('checked', lambda progress: inspect_backup(path, progress=progress))

    def confirm_restore(self, result):
        summary = result['summary']
        try: created = datetime.fromisoformat(result['created']).astimezone().strftime('%Y/%m/%d %H:%M')
        except ValueError: created = '不明'
        included = f"録音・録画・文字起こし結果も復元します（結果 {summary['results']} 件）。" if result['include_results'] else '現在の録音・録画・文字起こし結果はそのまま残します。'
        text = (f"展開後のサイズ：{result['expanded_bytes'] / 1024**2:,.1f} MiB\n作成日時：{created}\n話者 {summary['speakers']} 人 ／ 学習音声 {summary['learning_clips']} 件 ／ 辞書 {summary['dictionary_rules']} 件\n\n"
                '現在の設定・話者・学習音声・辞書・覚えた修正を、バックアップ時点の内容に置き換えます。\n'
                + included + '\n復元前のデータは自動で保管します。\n\n復元して再起動しますか？')
        if not messagebox.askyesno('復元して再起動', text, parent=self):
            self.status.set('復元を取り消しました。保存データは変更していません。')
            return
        path, fingerprint = self.restore_path, result['fingerprint']
        self.run('restored', lambda progress: restore_backup(path, expected_fingerprint=fingerprint, progress=progress))

    def poll(self):
        if self.closed: return
        while True:
            try: kind, result = self.events.get_nowait()
            except queue.Empty: break
            if kind == 'progress':
                self.status.set(result)
                continue
            self.idle()
            if kind == 'error':
                self.status.set('処理を完了できませんでした。内容を確認して再実行してください。')
                messagebox.showerror('バックアップ・復元', result, parent=self)
            elif kind == 'saved':
                self.status.set('バックアップを保存しました。USBメモリーなどで別のPCへコピーできます。')
                messagebox.showinfo('バックアップを保存しました', str(result['path']), parent=self)
            elif kind == 'checked': self.confirm_restore(result)
            elif kind == 'undone':
                self.status.set('復元前の状態に戻しました。再起動します…')
                self.restart()
                return
            elif kind == 'restored':
                self.status.set('復元しました。再起動します…')
                previous = result.get('previous_data')
                detail = '\n\n復元前のデータ：\n' + previous if previous else ''
                device = '\n別のPCの録画対象は選び直してください。' if result['different_pc'] else ''
                messagebox.showinfo('復元しました', '設定と学習データを復元しました。Whisperを再起動します。' + device + detail, parent=self)
                self.restart()
                return
        self.timer = self.after(80, self.poll)

    def restart(self):
        app = self.app
        self.closed = True
        app._restart_after_restore = True
        # Destroy callbacks must not write the old in-memory preferences over restored files.
        app.window_settings.cancel_timer()
        app.window_settings.dirty = False
        app.window_settings.closed = True
        app.result_path = app.result_data = None
        app.segments = []
        if app.overlay:
            app.overlay.on_change = None
            app.overlay.close()
        app.root.destroy()

    def close(self):
        if self.busy or self.closed: return
        self.closed = True
        if self.timer: self.after_cancel(self.timer)
        self.grab_release()
        self.app.data_transfer_active = False
        self.app.window_settings.restoring = False
        self.app.backup_dialog = None
        self.destroy()
        self.app.root.after(0, self.app.resume_saved_voice_pools)


def open_backup(app):
    existing = getattr(app, 'backup_dialog', None)
    if existing and existing.winfo_exists():
        existing.lift(); return
    if (app.proc or app.correction_pending or any(p.get('extracting') for p in app.memory_pending.values())):
        messagebox.showinfo('バックアップ・復元', '文字起こし・録画・話者の学習が終わってから開いてください。', parent=app.root)
        return
    preview = getattr(app, 'video_preview', None)
    review = getattr(app, 'voice_review', None)
    if (preview and preview.winfo_exists()) or (review and not review.closed):
        messagebox.showinfo('バックアップ・復元', 'MP4プレビューと学習音声の画面を閉じてから開いてください。', parent=app.root)
        return
    app.commit()
    app.persist_edits()
    if app.correction_pending or any(p.get('extracting') for p in app.memory_pending.values()):
        messagebox.showinfo('バックアップ・復元', '修正した話者を覚えています。完了後に開いてください。', parent=app.root)
        return
    app.window_settings.flush_all()
    if app.history_window:
        app.history_window.close_audio()
    app.data_transfer_active = True
    app.window_settings.restoring = True
    try: app.backup_dialog = BackupDialog(app)
    except Exception:
        app.data_transfer_active = False
        app.window_settings.restoring = False
        raise
