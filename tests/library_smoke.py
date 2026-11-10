"""Open two portable records in the native app and follow a noun across them."""
import os
from pathlib import Path
import sys
import tempfile
import time
import tkinter as tk
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    with tempfile.TemporaryDirectory(prefix='whisper-library-') as temp:
        os.environ['WHISPER_USER_DATA_DIR'] = str(Path(temp) / 'UserData')
        os.environ['WHISPER_LEGACY_DATA_DIR'] = str(Path(temp) / 'legacy')
        from app import App
        from engine import RESULTS, save_result
        from transcript_library_ui import TranscriptLibrary

        def save(name, title, lines):
            rows = [dict(start=12*i, end=12*i+2, speaker='speaker_0', text=text) for i, text in enumerate(lines)]
            save_result(dict(title=title, segments=rows, speakers={'speaker_0': '佐藤'}), RESULTS / name)

        save('20261010_091000_a', '以前の会議', ['前の発言です。', '京都で開催します。'])
        save('リアルタイム_20261020_143000_b', '今回の会議', ['京都を訪問します。'])
        root = tk.Tk()
        errors = []
        root.report_callback_exception = lambda *args: errors.append(str(args))
        app = App(root)
        app.update_checker.close()

        def open_records():
            app.open_button.invoke()
            window = next(child for child in root.winfo_children() if isinstance(child, TranscriptLibrary))
            deadline = time.monotonic() + 20
            while not window.done and time.monotonic() < deadline and not errors:
                root.update()
                time.sleep(.01)
            root.update()
            assert not errors and len(window.indexes) == 2, (errors, window.status.get())
            return window

        try:
            window = open_records()
            assert [r.title for r in window.records] == ['今回の会議', '以前の会議']
            x, y, width, height = window.body.bbox('2.0')
            window.body.event_generate('<ButtonPress-1>', x=x+width//2, y=y+height//2)
            window.body.event_generate('<ButtonRelease-1>', x=x+width//2, y=y+height//2)
            root.update()
            assert window.word.get() == '京都' and window.matches == [(1, 1)]
            window.hits.selection_set('0')
            root.update()
            window.jump.invoke()
            root.update()
            assert window.heading.get() == '以前の会議'
            assert window.body.get('match.first', 'match.last') == '京都で開催します。'
            window.back.invoke()
            root.update()
            assert window.heading.get() == '今回の会議' and window.matches == [(1, 1)]
            with patch('tkinter.filedialog.askopenfilename', return_value=''):
                window.open_file()
            assert window.winfo_viewable()
            window.edit.invoke()
            root.update()
            assert app.result_data['title'] == '今回の会議'
            app.editor.delete('1.0', 'end')
            app.editor.insert('1.0', '大阪で開催します。')
            window = open_records()
            assert '大阪' in window.word.cget('values') and '京都' not in window.word.cget('values')
            assert '大阪で開催します。' in window.body.get('1.0', 'end')
            window.destroy()
            assert not errors, errors
        finally:
            root.destroy()
    print('Saved records, noun click, related utterance, return, and saved edit: OK')


if __name__ == '__main__':
    main()
