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

        def save(name, title, lines, **last_segment):
            rows = [dict(start=12*i, end=12*i+2, speaker='speaker_0', text=text) for i, text in enumerate(lines)]
            rows[0]['end'] = .18
            rows[-1].update(last_segment)
            save_result(dict(title=title, segments=rows, speakers={'speaker_0': '佐藤'}), RESULTS / name)

        save('20261010_091000_a', '以前の会議', ['医者に相談しました。', '京都で開催します。', '京都に戻ります。'],
             separated_audio='.audio/separated/voice.wav', separated_offset=23.5)
        save('リアルタイム_20261020_143000_b', '今回の会議', ['京都を訪問します。'])
        recording = RESULTS / '20261010_091000_a/.audio/recording.wav'
        recording.parent.mkdir()
        expected = b''.join(level.to_bytes(2, 'little') * 48000 for level in (400, 900, 700, 1100))
        short_pcm = b''.join(level.to_bytes(2, 'little') * 96 for level in range(180))
        with wave.open(str(recording), 'wb') as wav:
            wav.setparams((2, 2, 48000, 0, 'NONE', 'not compressed'))
            wav.writeframes(short_pcm + bytes(12 * 48000 * 4 - len(short_pcm)) + expected + bytes(12 * 48000 * 4))
        separated = recording.parent / 'separated/voice.wav'
        separated.parent.mkdir()
        with wave.open(str(separated), 'wb') as wav:
            wav.setparams((2, 2, 48000, 0, 'NONE', 'not compressed'))
            wav.writeframes(bytes(48000 * 2) + expected)
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
            root.update()
            saved, appearance = app.open_button, app.appearance_combo
            assert saved.winfo_ismapped() and saved.master is appearance.master, (root.state(), root.geometry(), saved.winfo_geometry())
            assert saved.winfo_rootx() == appearance.winfo_rootx() and saved.winfo_width() == appearance.winfo_width()
            assert saved.winfo_rooty() >= appearance.winfo_rooty() + appearance.winfo_height()
            window = open_records()
            assert [r.title for r in window.records] == ['今回の会議', '以前の会議']
            assert not window.waveform.winfo_ismapped()
            window.body.see('2.0')
            root.update()
            bounds = window.body.bbox('2.0')
            assert bounds, (window.geometry(), window.body.winfo_geometry(), window.hits.winfo_geometry())
            x, y, width, height = bounds
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

            wave_view = window.waveform

            def wave_mouse(event, seconds):
                x = round(wave_view.x_at(seconds))
                wave_view.canvas.event_generate(event, x=x, y=wave_view.canvas.winfo_height()//2)
                root.update()
                return round(wave_view.time_at(x), 3)

            def click_wave(seconds):
                wave_mouse('<ButtonPress-1>', seconds)
                return wave_mouse('<ButtonRelease-1>', seconds)

            def wheel_at(x, direction, shift=False):
                unit = 1 if root.tk.call('tk', 'windowingsystem') == 'aqua' else 120
                wave_view.canvas.event_generate('<MouseWheel>', x=x, y=32, delta=direction*unit, state=int(shift))

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
                assert (window.range_start.get(), window.range_end.get()) == ('0.000', '2.000')
                wait_for(lambda: wave_view.data is not None)
                assert wave_view.source == recording and wave_view.origin == 12 and wave_view.data['start'] == 0
                assert max(wave_view.data['high']) > 800 and wave_view.canvas.find_withtag('wave')
                wave_mouse('<ButtonPress-1>', 0)
                low = wave_mouse('<B1-Motion>', .4)
                assert float(window.range_start.get()) == low and not window.audio_ranges, (
                    low, window.range_start.get(), window.audio_ranges, wave_view.drag, errors)
                wave_mouse('<ButtonRelease-1>', .4)
                wave_mouse('<ButtonPress-1>', 2)
                high = wave_mouse('<ButtonRelease-1>', 1.6)
                assert window.audio_ranges[window.range_key] == (low, high)
                assert window.grab_current() == window and window.playing_hit is None
                wave_mouse('<ButtonPress-1>', 1.2)
                wave_mouse('<B1-Motion>', .8)
                wave_view.canvas.focus_force()
                wave_view.canvas.event_generate('<Escape>')
                assert wave_view.spans == [(low, high)] and window.grab_current() == window
                assert (float(window.range_start.get()), float(window.range_end.get())) == (low, high)
                window.range_start.set('0.5')
                window.range_end.set('1.25')
                assert wave_view.spans == [(.5, 1.25)]
                window.update_matches(keep_selection=True)
                window.range_play.invoke()
                assert window.audio_player.current.intervals == [(12.5, 13.25)]
                assert completed_pcm(device.return_value) == expected[96000:240000]
                position = click_wave(.85)
                assert completed_pcm(device.return_value) == expected[round(position * 48000) * 4:240000]
                assert wave_view.spans == [(.5, 1.25)] and window.grab_current() == window
                writing, resume = threading.Event(), threading.Event()

                def hold_output(pcm):
                    writing.set()
                    resume.wait(5)

                device.return_value.write.side_effect = hold_output
                try:
                    click_wave(.9)
                    wait_for(writing.is_set)
                    old = window.audio_player.current
                    x = round(wave_view.x_at(.6))
                    for _ in range(6):
                        wheel_at(x, 1)
                    wait_for(lambda: wave_view._load_after is None and wave_view.generation is None)
                    assert wave_view.data['step'] < wave_view.overview['step']
                    assert abs(wave_view.data['start'] - wave_view.view[0]) < 1e-6
                    assert window.audio_player.current is old and wave_view.spans == [(.5, 1.25)]
                    position = click_wave(.6)
                    assert old.cancel.is_set() and window.audio_player.current.generation != old.generation
                    device.return_value.write.reset_mock()
                finally:
                    device.return_value.write.side_effect = None
                    resume.set()
                assert completed_pcm(device.return_value) == expected[round(position * 48000) * 4:240000]
                for _ in range(8):
                    wheel_at(x, -1)
                assert wave_view.view == (0., 2.)
                for invalid in ('0.4', '5', 'invalid'):
                    window.range_end.set(invalid)
                    window.range_play.invoke()
                    assert '再生範囲' in window.note.get() and window.audio_generation is None
                window.range_end.set('1.25')
                select_speed('0.5倍')
                assert window.audio_generation is None
                window.hits.focus_force()
                window.hits.event_generate('<space>')
                slow_pcm = completed_pcm(device.return_value)
                # atempo can round the last frame. This 0.75 s selection should take about 1.5 s.
                assert 1.4 < len(slow_pcm) / (48000 * 4) < 1.6

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
                assert changed.intervals == [(12.5, 13.25)] and changed.speed == .75
                wait_for(lambda: window.repeat_timer is not None)
                pending = window.repeat_timer
                click_result('1')
                assert pending not in root.tk.call('after', 'info')
                next_hit = window.audio_player.current
                wait_for(lambda: window.repeat_timer is not None and window.audio_player.current is not next_hit)
                assert window.audio_player.current.intervals == [(.5, 2.5)] and window.audio_player.current.speed == .75
                assert window.audio_player.current.source.resolve() == separated.resolve()
                wait_for(lambda: wave_view.data is not None and window.repeat_timer is not None)
                assert wave_view.source.resolve() == separated.resolve() and wave_view.origin == .5
                assert wave_view.data['start'] == 0 and max(wave_view.data['high']) > 800
                pending = window.repeat_timer
                high = wave_mouse('<ButtonPress-1>', 1.)
                low = wave_mouse('<B1-Motion>', .25)
                assert window.repeat_timer == pending
                wave_mouse('<ButtonRelease-1>', .25)
                assert pending not in root.tk.call('after', 'info')
                assert window.audio_player.current.intervals == [(.5+low, .5+high)] and window.audio_player.current.speed == .75
                wait_for(lambda: window.repeat_timer is not None)
                pending = window.repeat_timer
                position = click_wave(.7)
                inside = window.audio_player.current
                assert pending not in root.tk.call('after', 'info')
                assert inside.intervals == [(.5 + position, .5 + high)] and inside.speed == .75
                wait_for(lambda: window.audio_player.current is not inside and window.repeat_timer is not None)
                assert window.audio_player.current.intervals == [(.5 + low, .5 + high)]
                position = click_wave(1.4)
                outside = window.audio_player.current
                assert outside.intervals == [(.5 + position, 2.5)]
                wait_for(lambda: window.audio_player.current is not outside and window.repeat_timer is not None)
                assert window.audio_player.current.intervals == outside.intervals
                assert wave_view.spans == [(low, high)] and window.audio_ranges[window.range_key] == (low, high)
                pending = window.repeat_timer
                click_wave(2)
                assert window.playing_hit is None and pending not in root.tk.call('after', 'info')
                assert '発言の最後' in window.audio_status.get()
                window.range_play.invoke()
                window.range_reset.invoke()
                assert wave_view.spans == [(0, 2)]
                assert window.audio_player.current.intervals == [(.5, 2.5)]
                wait_for(lambda: window.repeat_timer is not None)
                pending = window.repeat_timer
                window.repeat_check.invoke()
                assert pending not in root.tk.call('after', 'info') and window.playing_hit is None
                window.repeat_check.invoke()
                window.hits.focus_force()
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
                assert restarted.generation != first and restarted.intervals == [(12.5, 13.25)] and restarted.speed == .75
                window.repeat_check.invoke()
                assert not window.audio_repeat.get() and window.audio_generation == restarted.generation
                select_speed('0.75倍')
                assert window.audio_generation == restarted.generation
                click_result('1')
                second = window.audio_player.current
                assert second.generation == window.audio_generation and second.generation != restarted.generation
                assert second.intervals == [(.5, 2.5)] and second.speed == .75
                wait_for(lambda: wave_view.data is not None and wave_view.origin == .5)
                for target in (1.4, .3):
                    previous = window.audio_player.current
                    position = click_wave(target)
                    second = window.audio_player.current
                    assert previous.cancel.is_set() and second.generation != previous.generation
                    assert second.intervals == [(.5 + position, 2.5)] and second.speed == .75
                    assert wave_view.playhead == position
                    window.audio_player.events.put((previous.generation, 'ended', None))
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
                assert (window.range_start.get(), window.range_end.get()) == ('0.500', '1.250')
                window.range_reset.invoke()
                window.range_play.invoke()
                assert window.audio_player.current.intervals == [(12, 14)]
                completed_pcm(device.return_value)
                window.word.set('医師')
                window.search_button.invoke()
                window.hits.selection_set('0')
                root.update()
                wait_for(lambda: wave_view.duration == .18 and wave_view.overview is not None)
                x = round(wave_view.x_at(.07))
                anchor = wave_view.time_at(x)
                for _ in range(16):
                    wheel_at(x, 1)
                start, end = zoomed = wave_view.view
                assert abs(end - start - .02) < 1e-6 and abs(wave_view.time_at(x) - anchor) < 3e-6
                assert wave_view.spans == [(0., .18)] and wave_view.canvas.find_withtag('wave')
                low = wave_mouse('<ButtonPress-1>', anchor - .004)
                wave_mouse('<B1-Motion>', anchor + .004)
                wheel_at(x, -1)
                assert wave_view.view == zoomed
                high = wave_mouse('<ButtonRelease-1>', anchor + .004)
                wave_mouse('<ButtonPress-1>', low)
                low = wave_mouse('<ButtonRelease-1>', low + .001)
                assert window.audio_ranges[window.range_key] == (low, high)
                select_speed('1倍')
                position = click_wave(anchor)
                assert completed_pcm(device.return_value) == short_pcm[round(position * 48000)*4:round(high * 48000)*4]
                wheel_at(x, -1, shift=True)
                assert wave_view.view[0] > start and wave_view.spans == [(low, high)]
                window.word.set('京都')
                window.search_button.invoke()
            window.hits.selection_set('0')
            root.update()
            assert wave_view.view == (0., 2.)
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
            assert wave_view.data is None and not wave_view.enabled
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

            from research_ui import ResearchWindow
            from tkinter import ttk

            window.research_btn.invoke()
            research = next(w for w in window.winfo_children() if isinstance(w, ResearchWindow))
            wait_for(lambda: research.ready)
            def button(parent, label):
                for widget in parent.winfo_children():
                    if isinstance(widget, ttk.Button) and widget.cget('text') == label:
                        return widget
                    found = button(widget, label)
                    if found:
                        return found
            button(research, '記事を追加…').invoke()
            dialog = next(w for w in research.winfo_children() if isinstance(w, tk.Toplevel))
            form = dialog.winfo_children()[0]
            form.grid_slaves(row=0, column=1)[0].insert(0, '開催地のお知らせ')
            form.grid_slaves(row=1, column=1)[0].insert(0, '2025-03-01')
            textfile = Path(temp) / 'お知らせ.md'
            textfile.write_text('会場を決めました。\n京都で開催します。', 'utf-8')
            with patch('tkinter.filedialog.askopenfilename', return_value=str(textfile)):
                button(dialog, 'ファイルから本文を入力…').invoke()
            button(dialog, '記事を保存').invoke()
            wait_for(lambda: research.ready and len(research.sources) == 3)
            research.question.insert(0, '昔、京都について何と言っていた？')
            research.order.set('古い順')
            research.search_btn.invoke()
            wait_for(lambda: len(research.hits) == 3)
            assert research.table.set('0', '種類') == '記事' and research.table.set('0', '日付') == '2025-03-01'
            research.open_btn.invoke()
            assert '会場を決めました。' in research.detail.get('1.0', 'end')
            with patch('research_ui.summarize', return_value='会議での発言です。 [2]'):
                research.ai_btn.invoke()
                wait_for(lambda: research.ai_job is None and bool(research.detail.tag_ranges('ref_1')))
            ref = research.detail.tag_ranges('ref_1')[0]
            research.detail.see(ref)
            root.update()
            x, y, width, height = research.detail.bbox(ref)
            for event in ('<Motion>', '<ButtonPress-1>', '<ButtonRelease-1>'):
                research.detail.event_generate(event, x=x+width//2, y=y+height//2)
            root.update()
            assert research.table.selection() == ('1',), (research.table.selection(), research.detail.tag_ranges('ref_1'),
                                                          research.detail_title.get(), errors)
            research.open_btn.invoke()
            root.update()
            assert window.grab_current() == window and window.heading.get() == '以前の会議'
            assert window.body.get('match.first', 'match.last') == '京都で開催します。'
            window.research_btn.invoke()
            root.update()
            assert window.grab_current() == research and research.table.selection() == ('1',)
            research.destroy()
            window.destroy()
            assert not errors, errors
        finally:
            root.destroy()
    print('Saved record playback, article import, cited research and source navigation: OK')


if __name__ == '__main__':
    main()
