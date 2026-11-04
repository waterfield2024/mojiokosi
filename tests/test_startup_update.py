"""Exercise background checks, Tk thread boundaries and manual-check races."""
from pathlib import Path
import sys
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from update_ui import StartupUpdateCheck


class Root:
    def __init__(self):
        self.thread = threading.get_ident()
        self.tasks = {}
        self.next_id = 0

    def assert_ui_thread(self):
        if threading.get_ident() != self.thread:
            raise AssertionError('worker touched Tk')

    def after(self, milliseconds, callback):
        self.assert_ui_thread()
        self.next_id += 1
        self.tasks[self.next_id] = (milliseconds, callback)
        return self.next_id

    def after_cancel(self, timer):
        self.assert_ui_thread()
        self.tasks.pop(timer, None)

    def bind(self, *args, **kwargs):
        self.assert_ui_thread()

    def run(self, timer):
        self.tasks.pop(timer)[1]()


class StartupChecks(unittest.TestCase):
    def setUp(self):
        self.root = Root()
        self.app = SimpleNamespace(root=self.root, update_notice=Mock(), update_button=Mock())
        self.app.update_notice.set.side_effect = lambda text: self.root.assert_ui_thread()
        self.app.update_button.configure.side_effect = lambda **options: self.root.assert_ui_thread()
        self.checker = StartupUpdateCheck(self.app, Path('installation'))
        self.addCleanup(self.checker.close)

    def receive(self):
        result = self.checker.events.get(timeout=3)
        self.checker.events.put(result)
        self.root.run(self.checker.poll_timer)

    def test_checks_after_startup_on_worker_once_and_only_notifies(self):
        calls = []
        def check(root):
            calls.append(threading.get_ident())
            return {'changes': 3}
        with patch('update_ui.check_latest', side_effect=check) as request, patch('update_ui.prepare_update') as install:
            request.assert_not_called()
            self.assertEqual(self.root.tasks[self.checker.start_timer][0], 1500)
            self.root.run(self.checker.start_timer)
            self.receive()
            self.checker.start()
            self.assertEqual(request.call_count, 1)
            self.assertNotEqual(calls[0], self.root.thread)
            install.assert_not_called()
        self.app.update_notice.set.assert_called_once_with('新しいバージョンがあります。')
        self.app.update_button.configure.assert_called_once_with(text='更新内容を見る…', style='Accent.TButton')
        self.assertFalse(self.root.tasks)

    def test_current_version_has_no_notice(self):
        with patch('update_ui.check_latest', return_value={'changes': 0}):
            self.root.run(self.checker.start_timer)
            self.receive()
        self.app.update_notice.set.assert_called_once_with('')
        self.app.update_button.configure.assert_called_once_with(text='アプリを更新…', style='TButton')

    def test_offline_and_invalid_response_are_quiet_and_not_retried(self):
        for error in (OSError('offline'), ValueError('bad JSON')):
            with self.subTest(error=error):
                checker = StartupUpdateCheck(self.app, Path('installation'))
                try:
                    with patch('update_ui.check_latest', side_effect=error) as request, patch('update_ui.messagebox.showerror') as popup:
                        self.root.run(checker.start_timer)
                        item = checker.events.get(timeout=3)
                        checker.events.put(item)
                        self.root.run(checker.poll_timer)
                        request.assert_called_once()
                        popup.assert_not_called()
                    self.assertIsNone(checker.poll_timer)
                    self.app.update_notice.set.assert_not_called()
                    self.app.update_button.configure.assert_not_called()
                finally:
                    checker.close()

    def test_slow_network_does_not_block_ui_and_close_ignores_late_result(self):
        entered, finish = threading.Event(), threading.Event()
        self.addCleanup(finish.set)
        def check(root):
            entered.set()
            finish.wait(3)
            return {'changes': 2}
        with patch('update_ui.check_latest', side_effect=check):
            self.root.run(self.checker.start_timer)
            self.assertTrue(entered.wait(2))
            heartbeat = Mock()
            self.root.run(self.root.after(0, heartbeat))
            heartbeat.assert_called_once()
            self.checker.close(SimpleNamespace(widget=self.root))
            self.assertFalse(self.root.tasks)
            finish.set()
            result = self.checker.events.get(timeout=3)
            self.checker.events.put(result)
            self.checker.poll()
        self.app.update_notice.set.assert_not_called()
        self.app.update_button.configure.assert_not_called()

    def test_close_before_start_prevents_network_request(self):
        with patch('update_ui.check_latest') as request:
            self.checker.close()
            self.checker.start()
            request.assert_not_called()
        self.assertFalse(self.root.tasks)

    def test_manual_check_before_timer_prevents_duplicate_startup_request(self):
        with patch('update_ui.check_latest') as request:
            self.checker.manual_check_started()
            self.checker.start()
            request.assert_not_called()
        self.assertFalse(self.root.tasks)

    def test_old_background_result_cannot_replace_new_manual_result(self):
        entered, finish = threading.Event(), threading.Event()
        self.addCleanup(finish.set)
        def check(root):
            entered.set()
            finish.wait(3)
            return {'changes': 4}
        with patch('update_ui.check_latest', side_effect=check):
            self.root.run(self.checker.start_timer)
            self.assertTrue(entered.wait(2))
            self.checker.manual_check_started()
            self.checker.show_release({'changes': 0})
            finish.set()
            value = self.checker.events.get(timeout=3)
            self.checker.events.put(value)
            self.checker.poll()
        self.app.update_notice.set.assert_called_once_with('')
        self.assertFalse(self.root.tasks)

    def test_child_dialog_destroy_does_not_cancel_startup_check(self):
        self.checker.close(SimpleNamespace(widget=object()))
        self.assertFalse(self.checker.closed)
        self.assertIn(self.checker.start_timer, self.root.tasks)


if __name__ == '__main__':
    unittest.main()
