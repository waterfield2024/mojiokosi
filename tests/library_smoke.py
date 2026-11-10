"""Open two portable records in the native app and follow a noun across them."""
import os
from pathlib import Path
import sys
import tempfile
import threading
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

            def select_speed(label):
                window.audio_speed.set(label)
                window.audio_speed.event_generate('<<ComboboxSelected>>')

            def wait_for(predicate):
                deadline = time.monotonic() + 10
                while not predicate() and time.monotonic() < deadline and not errors:
                    root.update()
                    time.sleep(.01)
                assert not errors and predicate(), (errors, window.audio_status.get())

            def completed_pcm(output):
                wait_for(lambda: window.audio_generation is None)
                assert '聞き直せます' in window.audio_status.get(), window.audio_status.get()
                pcm = b''.join(call.args[0] for call in output.write.call_args_list)
                output.write.reset_mock()
                return pcm

            # Decode through the real player and FFmpeg, and capture its output before the hardware.
            with patch('comparison_audio.WaveOutput') as device:
                device.RATE = 48000
                device.return_value.pending = ()
                device.return_value.position.return_value = 0
                assert window.audio_speed.get() == '1倍'
                assert not window.audio_repeat.get()
                assert tuple(window.audio_speed.cget('values')) == ('0.5倍', '0.75倍', '1倍', '1.25倍', '1.5倍')
                click_result()
                assert window.audio_generation is not None, window.audio_status.get()
                assert completed_pcm(device.return_value) == expected
                select_speed('0.5倍')
                assert window.audio_generation is None
                window.hits.focus_force()
                window.hits.event_generate('<space>')
                slow_pcm = completed_pcm(device.return_value)
                # atempo can round the last frame. Two seconds of speech should take about four.
                assert 3.9 < len(slow_pcm) / (48000 * 4) < 4.1

                passes_before = device.call_count
                window.repeat_check.invoke()
                assert window.playing_hit is None
                window.hits.event_generate('<space>')
                window.hits.selection_set('1')
                wait_for(lambda: device.call_count >= passes_before + 2 and window.repeat_timer is not None)
                pcm = b''.join(call.args[0] for call in device.return_value.write.call_args_list)
                assert pcm == slow_pcm * 2
                select_speed('0.75倍')
                changed = window.audio_player.current
                assert changed.intervals == [(12, 14)] and changed.speed == .75
                wait_for(lambda: window.repeat_timer is not None)
                pending = window.repeat_timer
                click_result('1')
                assert pending not in root.tk.call('after', 'info')
                next_hit = window.audio_player.current
                wait_for(lambda: window.repeat_timer is not None and window.audio_player.current is not next_hit)
                assert window.audio_player.current.intervals == [(24, 26)] and window.audio_player.current.speed == .75
                pending = window.repeat_timer
                window.repeat_check.invoke()
                assert pending not in root.tk.call('after', 'info') and window.playing_hit is None
                window.repeat_check.invoke()
                window.hits.event_generate('<space>')
                wait_for(lambda: window.repeat_timer is not None)
                pending = window.repeat_timer
                window.audio_stop.invoke()
                assert window.playing_hit is None and pending not in root.tk.call('after', 'info')
                assert window.audio_player.current is None
                window.hits.selection_set('0')
                select_speed('0.5倍')

                # Keep playback active while the user selects another row with the keyboard.
                release_output = threading.Event()
                device.return_value.write.side_effect = lambda pcm: release_output.wait(3)
                window.hits.event_generate('<space>')
                first = window.audio_generation
                assert first is not None
                window.hits.selection_set('1')
                select_speed('0.75倍')
                restarted = window.audio_player.current
                assert restarted.generation != first and restarted.intervals == [(12, 14)] and restarted.speed == .75
                window.repeat_check.invoke()
                assert not window.audio_repeat.get() and window.audio_generation == restarted.generation
                select_speed('0.75倍')
                assert window.audio_generation == restarted.generation
                click_result('1')
                second = window.audio_player.current
                assert second.generation == window.audio_generation and second.generation != restarted.generation
                assert second.intervals == [(24, 26)] and second.speed == .75
                window.audio_player.events.put((restarted.generation, 'error', 'stale event'))
                window.poll_audio()
                assert window.audio_generation == second.generation
                window.audio_stop.invoke()
                release_output.set()
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
    print('Saved records, variable-speed repeat PCM, target switching, stop, navigation and edit: OK')


if __name__ == '__main__':
    main()
