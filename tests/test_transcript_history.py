import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
DATA = tempfile.TemporaryDirectory(prefix='whisper-history-tests-')
for key in list(os.environ):
    if key.upper().startswith('WHISPER_'):
        del os.environ[key]
os.environ['WHISPER_USER_DATA_DIR'] = str(Path(DATA.name) / 'user-data')
os.environ['WHISPER_LEGACY_DATA_DIR'] = str(Path(DATA.name) / 'empty-legacy')

from transcript_history import organized_paragraphs, select_page, match_spans, preferences
from recording_settings import settings_for, save_settings, load_settings


def rows(count):
    return [{'segment_id': str(i), 'text': f'発言{i}', 'speaker': 'speaker_0', 'start': i * 2, 'end': i * 2 + 1}
            for i in range(count)]


class HistorySearch(unittest.TestCase):
    def test_latest_is_bounded_without_trimming_transcript(self):
        source = rows(280)
        page = select_page(source, {}, limit=100)
        self.assertEqual(len(page.entries), 100)
        self.assertEqual((page.start, page.end, page.total), (180, 280, 280))
        self.assertEqual(page.entries[0].index, 180)
        self.assertEqual(len(source), 280)

    def test_search_finds_older_rows_outside_visible_limit(self):
        source = rows(300)
        source[3]['text'] = '古い発言にも正当な理由があります'
        page = select_page(source, {}, query='正当', limit=50)
        self.assertEqual([r.index for r in page.entries], [3])

    def test_literal_query_not_regex_and_case_is_ignored(self):
        source = rows(3)
        source[0]['text'] = 'CUDA [v1.0]で認識する'
        source[1]['text'] = 'cuda v1x0'
        self.assertEqual(len(select_page(source, {}, '[v1.0]').matches), 1)
        self.assertEqual(len(select_page(source, {}, 'CuDa').matches), 2)
        self.assertEqual(match_spans('🙂正当な理由は正当', '正当'), [(1, 3), (7, 9)])

    def test_speaker_name_and_corrections_use_current_values(self):
        source = rows(1)
        source[0]['text'] = '政党な理由'
        self.assertEqual(len(select_page(source, {'speaker_0': '田中'}, '田中').matches), 1)
        source[0]['text'] = '正当な理由'
        self.assertEqual(len(select_page(source, {'speaker_0': '佐藤'}, '政党').matches), 0)
        self.assertEqual(len(select_page(source, {'speaker_0': '佐藤'}, '佐藤').matches), 1)

    def test_pause_keeps_same_rows_while_new_speech_arrives(self):
        source = rows(160)
        before = select_page(source, {}, limit=50)
        source.extend(rows(200)[160:])
        after = select_page(source, {}, limit=50, end_key=before.entries[-1].key)
        self.assertEqual(before.entries, after.entries)
        self.assertTrue(after.newer)
        self.assertEqual(after.total, 200)

    def test_paging_reaches_all_matches_without_gaps(self):
        source = rows(225)
        page = select_page(source, {}, '発言', 100)
        seen = {e.key for e in page.entries}
        while page.older:
            page = select_page(source, {}, '発言', 100, page.adjacent_anchor(100, older=True))
            seen.update(e.key for e in page.entries)
        self.assertEqual(len(seen), 225)
        self.assertEqual(page.start, 0)
        while page.newer:
            page = select_page(source, {}, '発言', 100, page.adjacent_anchor(100, older=False))
        self.assertEqual(page.end, 225)

    def test_empty_and_no_match_are_safe(self):
        self.assertEqual(select_page([], {}).entries, [])
        self.assertEqual(select_page(rows(3), {}, '存在しない言葉').entries, [])
        source = rows(1)
        source[0]['text'] = ' \n '
        self.assertEqual(select_page(source, {}).total, 0)


class OrganizedReading(unittest.TestCase):
    def test_connects_current_text_without_changing_originals_or_timing(self):
        source = rows(3)
        for row, text in zip(source, ['今日は', 'いい天気なので、', '散歩に行きます。']):
            row.update(text=text, live_original_text='認識直後の原文')
        before = json.dumps(source, ensure_ascii=False)
        result = organized_paragraphs(source, {'speaker_0': '田中'})
        self.assertEqual([(p.name, p.text) for p in result], [('田中', '今日はいい天気なので、散歩に行きます。')])
        self.assertEqual(json.dumps(source, ensure_ascii=False), before)

    def test_speakers_and_long_silences_stay_separate(self):
        source = rows(5)
        source[1]['speaker'] = 'speaker_1'
        source[3].update(start=30, end=31)
        source[4].update(start=32, end=33, speaker_review=True)
        # Even equal display names must not join different speaker identities.
        result = organized_paragraphs(source, {'speaker_0': '田中', 'speaker_1': '田中'})
        self.assertEqual([p.text for p in result], [r['text'] for r in source])
        self.assertEqual(result[-1].name, '田中 ？')

    def test_latest_limit_and_empty_rows_do_not_hide_recent_speech(self):
        source = rows(80)
        source.append({'text': '  '})
        # Separate names make the number of included utterances observable.
        for index, row in enumerate(source):
            row['speaker'] = str(index)
        result = organized_paragraphs(source, {}, limit=50)
        self.assertEqual(len(result), 50)
        self.assertEqual((result[0].text, result[-1].text), ('発言30', '発言79'))
        self.assertEqual(organized_paragraphs([], {}), [])
        with self.assertRaises(ValueError):
            organized_paragraphs(source, {}, limit=1)

    def test_manual_correction_rename_and_undo_are_reflected(self):
        source = rows(2)
        source[0]['text'], source[1]['text'] = '政党な', '理由です。'
        self.assertEqual(organized_paragraphs(source, {})[0].text, '政党な理由です。')
        source[0]['text'] = '正当な'
        result = organized_paragraphs(source, {'speaker_0': '佐藤'})
        self.assertEqual((result[0].name, result[0].text), ('佐藤', '正当な理由です。'))
        source[0]['text'] = '政党な'
        self.assertEqual(organized_paragraphs(source, {})[0].text, '政党な理由です。')

    def test_keeps_english_numbers_and_intentional_repetitions(self):
        source = rows(6)
        for row, text in zip(source, ['Version', '1.5', 'is ready.', 'Next time.', 'はい、', 'はい。']):
            row['text'] = text
            row['speaker'] = None
        result = organized_paragraphs(source, {})
        self.assertEqual(result[0].name, '')
        self.assertEqual(result[0].text, 'Version 1.5 is ready. Next time.はい、はい。')

    def test_long_paragraphs_and_invalid_times_keep_all_words(self):
        source = rows(3)
        for row in source:
            row['text'] = 'あ' * 300
        self.assertEqual([len(p.text) for p in organized_paragraphs(source, {})], [300, 300, 300])
        for value in (None, 'invalid', float('nan')):
            with self.subTest(value=value):
                source = rows(2)
                source[1]['start'] = value
                self.assertEqual(len(organized_paragraphs(source, {})), 2)


class HistoryLiveControls(unittest.TestCase):
    def setUp(self):
        from live_shortcut import LiveActionButton, LiveShortcut
        self.app = Mock(proc=None, operation=None, overlay=None, video_preview=None, data_transfer_active=False)
        self.app.root.tk.call.return_value = ''
        with patch('live_shortcut.GlobalHotkey'):
            self.controller = self.app.live_shortcut = LiveShortcut(self.app)
        self.panel = Mock(app=self.app, live_action=None)
        self.panel.sync = lambda: LiveActionButton.sync(self.panel)
        self.click = lambda: LiveActionButton.run_action(self.panel)
        self.panel.sync()

    def test_stop_save_and_resume_without_registered_hotkey(self):
        self.assertEqual(self.panel.live_action, ('開始設定を開く', self.controller.open_settings))
        self.app.operation, self.app.proc = 'realtime', object()
        self.app.live_stop_requested, self.app.live_phase = False, 'capturing'
        self.app.stop_live.side_effect = lambda: setattr(self.app, 'live_stop_requested', True)
        self.controller.started()
        self.panel.sync()
        self.click()
        self.click()
        self.app.stop_live.assert_called_once()
        self.assertEqual(self.panel.live_action, ('保存中…', None))
        self.assertFalse(self.controller.pending)
        self.app.proc = None
        self.panel.sync()
        self.assertEqual(self.panel.live_action, ('再開', self.controller.toggle))
        self.click()
        self.click()
        self.assertTrue(self.controller.pending)
        self.assertEqual(self.panel.live_action, ('再開を準備中…', None))

    def test_stale_click_does_not_queue_resume_after_shortcut_stop(self):
        self.app.operation, self.app.proc = 'realtime', object()
        self.app.live_stop_requested, self.app.live_phase = False, 'capturing'
        self.controller.started()
        self.panel.sync()
        self.app.live_stop_requested = True
        self.click()
        self.assertFalse(self.controller.pending)
        self.assertEqual(self.panel.live_action, ('保存中…', None))
        self.app.operation = 'transcribe'
        self.panel.sync()
        self.click()
        self.assertEqual(self.panel.live_action, ('再開', None))
        self.app.stop_live.assert_not_called()

    def test_caption_button_fits_next_to_captions_on_secondary_monitor(self):
        from overlay_preferences import control_position
        area = {'rect': [-800, -100, 0, 500]}
        for caption in [(-750, 300, 700, 100), (-800, -100, 700, 100),
                        (-60, -100, 60, 600), (-800, -100, 40, 600)]:
            x, y, w, h = caption
            bx, by = control_position(*caption, 120, 30, area)
            self.assertTrue(-800 <= bx <= -120 and -100 <= by <= 470)
            self.assertTrue(bx + 120 <= x or bx >= x + w or by + 30 <= y or by >= y + h)


class HistoryPreferences(unittest.TestCase):
    def test_legacy_settings_keep_model_and_recording_choices(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'recording_settings.json'
            data = settings_for('window', {}, model='haas-ja-v1.0', processing={'chunk_seconds': 5})
            data.pop('history')
            data['version'] = 5
            path.write_text(json.dumps(data), 'utf-8')
            with patch('recording_settings.settings_path', return_value=path):
                restored = load_settings()
            self.assertEqual(restored['model'], 'haas-ja-v1.0')
            self.assertEqual(restored['mode'], 'window')
            self.assertEqual(restored['processing']['chunk_seconds'], 5)
            self.assertEqual(restored['history'], {'visible': True, 'limit': 100, 'side': 'right'})

    def test_history_settings_survive_save_and_backup_validation(self):
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            data = settings_for('audio', {}, history={'visible': False, 'limit': 200, 'side': 'left'})
            with patch('recording_settings.settings_path', return_value=folder / 'recording_settings.json'):
                save_settings(data)
                self.assertEqual(load_settings()['history'], data['history'])
            from data_backup import _validate
            self.assertEqual(_validate(folder, folder)['settings'], 1)

    def test_invalid_history_settings_rejected(self):
        for value in [{'limit': 100000}, {'visible': 'yes'}, {'side': []}, {'limit': True}, []]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                preferences(value)

    def test_history_window_position_can_be_saved(self):
        from window_settings import validated
        geometry = {'x': 100, 'y': 50, 'size': [400, 660], 'device': 'monitor',
                    'rect': [0, 0, 1920, 1080], 'dpi': 96, 'maximized': False}
        self.assertEqual(validated({'version': 1, 'windows': {'history': geometry}})['windows']['history'], geometry)


if __name__ == '__main__':
    unittest.main()
