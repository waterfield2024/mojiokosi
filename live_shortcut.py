"""Tk-thread stop/resume controller for the registered global shortcut."""
import time
from global_hotkey import GlobalHotkey, LABEL


class LiveShortcut:
    def __init__(self, app):
        self.app = app
        self.pending = False
        self.refresh_needed = False
        self.waiting_for_stop = False
        self.engaged = False
        self.closed = False
        self.available = False
        self.last_press = -float('inf')
        self.listener = GlobalHotkey(app.events)
        app.root.bind('<Destroy>', self.destroyed, add='+')

    def destroyed(self, event):
        if event.widget is self.app.root: self.close()

    def close(self):
        self.closed = True
        self.pending = False
        self.listener.close()

    def announce(self, text):
        app = self.app
        app.live_message.set(text)
        app.stage = text
        if app.overlay and not app.overlay.closed and app.overlay.visible:
            app.overlay.notify(text)

    def reveal_settings(self, text):
        app = self.app
        app.window_settings.show('main', app.root)
        if app.live_window and app.live_window.winfo_exists():
            app.window_settings.show('realtime', app.live_window)
        else:
            app.open_live()
        self.announce(text)

    def handle(self, event):
        if event['kind'] == 'live_hotkey_status':
            if not self.closed:
                self.available = event['available']
                hint = f'{LABEL}：停止・再開' if self.available else f'{LABEL}：使用できません'
                self.app.live_hotkey_hint.set(hint)
                if not self.available:
                    self.app.live_hotkey_error = (f'{LABEL}を登録できません。他のアプリで同じキーを使っている場合は、'
                                                  'そちらの設定を変更してWhisperを起動し直してください。')
                    self.app.show_live_preload_status()
            return True
        if event['kind'] != 'live_hotkey': return False
        now = time.monotonic()
        if self.closed or not self.available or now - event.get('posted', now) > 2 or now - self.last_press < .35:
            return True
        self.last_press = now
        self.toggle()
        return True

    def blocked(self):
        app = self.app
        return bool(getattr(app, 'data_transfer_active', False) or app.root.grab_current()
                    or (app.proc and app.operation != 'realtime')
                    or (app.video_preview and app.video_preview.winfo_exists()))

    def toggle(self):
        app = self.app
        if self.closed: return
        self.engaged = True
        if self.pending:
            self.pending = False
            self.announce('再開予約を取り消しました。')
            return
        if app.proc and app.operation == 'realtime':
            if not app.live_stop_requested and app.live_phase != 'finishing':
                try:
                    app.stop_live()  # Never call cancel(): a second call would force-kill the worker.
                    self.waiting_for_stop = True
                    self.announce('停止して保存しています。')
                except OSError as exc:
                    app.live_stop_requested = False
                    self.announce(f'停止できませんでした：{exc}')
                return
        elif self.blocked():
            self.announce('ほかの処理や開いている確認画面を閉じてから再開してください。')
            return
        if not app.live_window or not app.live_window.winfo_exists():
            self.reveal_settings('最初に音声出力と認識モデルを確認して、リアルタイム開始を押してください。')
            return
        self.pending = True
        self.refresh_needed = True
        if app.proc:self.waiting_for_stop = True
        self.announce('保存が終わったら再開します。もう一度押すと予約を取り消せます。' if app.proc else '再開を準備しています。')

    def started(self):
        self.pending = False
        self.waiting_for_stop = False

    def pump(self):
        app = self.app
        if self.closed: return
        if self.waiting_for_stop and not app.proc:
            self.waiting_for_stop = False
            if app.failed or app.cancelled:
                self.pending = False
            if not self.pending:
                self.announce(f'停止しました。{LABEL}で再開できます。')
        if not self.pending: return
        if self.blocked():
            self.pending = False
            self.announce('再開予約を取り消しました。開いている確認画面を閉じてください。')
            return
        if app.proc: return  # Wait for the exit event and all UI/result updates, not just process.poll().
        if self.refresh_needed:
            self.refresh_needed = False
            app.refresh_live_screens()
            app.refresh_live_devices()
            app.preload_live_models()
        if app.live_devices_loading: return
        if not app.selected_live_audio_device():
            self.pending = False
            self.reveal_settings('前回の音声出力が見つかりません。出力先を選び直してください。')
            return
        if app.live_preload_phase == 'error':
            self.pending = False
            self.reveal_settings('認識モデルを準備できません。「準備を再試行」を押してください。')
            return
        if app.live_preload_phase != 'ready': return
        if (not app.live_preloader or not app.live_preloader.alive()
                or app.live_preload_key != app.live_model_settings()):
            app.preload_live_models()
            return
        self.pending = False
        try:
            app.start_live()
        except Exception as exc:
            self.reveal_settings(f'再開できませんでした：{exc}')
            return
        if app.proc:
            self.announce('リアルタイム文字起こしを再開しています。')
        else:
            self.reveal_settings('再開できませんでした。音声出力・録画対象を確認してください。')
