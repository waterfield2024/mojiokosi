"""Open two portable records in the native app and follow a noun across them."""
import os
from pathlib import Path
import sys
import tempfile
import time
import tkinter as tk
from unittest.mock import patch
import wave

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

        save('20261010_091000_a', '以前の会議', ['医者に相談しました。', '京都で開催します。', '京都に戻ります。'])
        save('リアルタイム_20261020_143000_b', '今回の会議', ['京都を訪問します。'])
        recording = RESULTS / '20261010_091000_a/.audio/recording.wav'
        recording.parent.mkdir()
        expected = b'\x84\x03' * (48000 * 2 * 2)
        with wave.open(str(recording), 'wb') as wav:
            wav.setparams((2, 2, 48000, 0, 'NONE', 'not compressed'))
            wav.writeframes(bytes(12 * 48000 * 4) + expected + bytes(12 * 48000 * 4))
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
            kyoto_hits = [(1, 1), (1, 2)]
            assert window.word.get() == '京都' and [(h.record, h.row) for h in window.matches] == kyoto_hits

            def click_result(iid='0'):
                window.hits.see(iid)
                root.update_idletasks()
                x, y, width, height = window.hits.bbox(iid, '#1')
                window.hits.event_generate('<ButtonPress-1>', x=x+width//2, y=y+height//2)
                window.hits.event_generate('<ButtonRelease-1>', x=x+width//2, y=y+height//2)

            # Decode through the real player and FFmpeg, and capture its output before the hardware.
            with patch('comparison_audio.WaveOutput') as device:
                device.RATE = 48000
                device.return_value.pending = ()
                device.return_value.position.return_value = 0
                click_result()
                assert window.audio_generation is not None, window.audio_status.get()
                deadline = time.monotonic() + 10
                while window.audio_generation is not None and time.monotonic() < deadline:
                    root.update()
                    time.sleep(.01)
                pcm = b''.join(call.args[0] for call in device.return_value.write.call_args_list)
                assert pcm == expected, (len(pcm), window.audio_status.get())
                assert window.audio_generation is None and '聞き直せます' in window.audio_status.get()
                window.hits.focus_force()
                window.hits.event_generate('<space>')
                first = window.audio_generation
                assert first is not None
                click_result('1')
                second = window.audio_player.current
                assert second.generation == window.audio_generation and second.generation != first
                assert second.intervals == [(24, 26)]
                window.audio_player.events.put((first, 'error', 'stale event'))
                window.audio_stop.invoke()
                assert second.done.wait(3) and window.audio_generation is None
                window.poll_audio()
                assert 'stale event' not in window.audio_status.get()
            window.hits.selection_set('0')
            root.update()
            window.jump.invoke()
            root.update()
            assert window.heading.get() == '以前の会議'
            assert window.body.get('match.first', 'match.last') == '京都で開催します。'
            window.back.invoke()
            root.update()
            assert window.heading.get() == '今回の会議' and [(h.record, h.row) for h in window.matches] == kyoto_hits
            window.word.set('医師')
            window.search_button.invoke()
            assert [(h.record, h.row, h.reason) for h in window.matches] == [(1, 0, '類義語')]
            assert window.hits.set('0', '一致した語') == '医者（類義語）'
            recording.unlink()
            click_result()
            root.update()
            assert '音声が見つかりません' in window.audio_status.get()
            window.jump.invoke()
            root.update()
            assert window.body.get('match.first', 'match.last') == '医者に相談しました。'
            window.back.invoke()
            root.update()
            assert window.word.get() == '医師' and len(window.matches) == 1
            window.include_near.set(False)
            window.search_button.invoke()
            assert not window.matches
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
    print('Saved records, clicked utterance PCM, replay, stop, missing audio, navigation and edit: OK')


if __name__ == '__main__':
    main()
