import os
from pathlib import Path
import queue
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
DATA = tempfile.TemporaryDirectory(prefix='whisper-history-audio-tests-')
for key in list(os.environ):
    if key.upper().startswith('WHISPER_'):
        del os.environ[key]
os.environ['WHISPER_USER_DATA_DIR'] = str(Path(DATA.name) / 'user-data')
os.environ['WHISPER_LEGACY_DATA_DIR'] = str(Path(DATA.name) / 'empty-legacy')

from history_audio import audio_source, playback_target, utterance_interval
from history_ui import HistoryWindow


def write_wav(path, seconds=3):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), 'wb') as audio:
        audio.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        audio.writeframes(b'\0\0' * int(16000 * seconds))


class HistoryAudioSources(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)

    def test_moved_folder_cache_wins_over_old_absolute_path(self):
        old = self.folder / 'old.flac'
        old.touch()
        local = self.folder / '.audio' / 'source.flac'
        local.parent.mkdir()
        local.touch()
        self.assertEqual(audio_source({'audio_file': str(old)}, self.folder), local)

    def test_in_progress_recording_wins_over_flac_snapshot(self):
        wav = self.folder / '.audio' / 'recording.wav'
        write_wav(wav)
        flac = wav.with_name('source.flac')
        flac.touch()
        self.assertEqual(audio_source({'mode': 'realtime', 'recording_complete': False}, self.folder), wav)
        self.assertEqual(audio_source({'mode': 'realtime', 'recording_complete': True}, self.folder), flac)

    def test_source_file_fallback_without_cache(self):
        source = self.folder / 'video.mp4'
        source.touch()
        self.assertEqual(audio_source({'source_mode': 'file', 'source': str(source)}, self.folder), source)

    def test_missing_youtube_cache_does_not_fetch_source(self):
        with self.assertRaises(FileNotFoundError):
            audio_source({'source_mode': 'youtube', 'source': 'https://www.youtube.com/watch?v=example'}, self.folder)

    def test_edited_interval_is_used_without_extra_context(self):
        wav = self.folder / '.audio' / 'recording.wav'
        write_wav(wav)
        row = {'start': 1.25, 'end': 2.125}
        self.assertEqual(playback_target({}, self.folder, row), (wav, (1.25, 2.125)))

    def test_invalid_intervals_rejected(self):
        for row in [{}, {'start': 2, 'end': 1}, {'start': 1, 'end': 1},
                    {'start': float('nan'), 'end': 3}, {'start': 0, 'end': float('inf')},
                    {'start': None, 'end': 3}]:
            with self.subTest(row=row), self.assertRaises(ValueError):
                utterance_interval(row)
        self.assertEqual(utterance_interval({'start': -1, 'end': 1}), (0., 1.))

    def test_read_growing_wav_clips_to_written_audio_without_disturbing_writer(self):
        wav = self.folder / '.audio' / 'recording.wav'
        wav.parent.mkdir()
        data = {'mode': 'realtime', 'recording_complete': False}
        with open(wav, 'wb') as file, wave.open(file, 'wb') as writer:
            writer.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
            writer.writeframes(b'\0\0' * 16000)
            file.flush()
            self.assertEqual(playback_target(data, self.folder, {'start': .5, 'end': 2})[1], (.5, 1.))
            with self.assertRaises(ValueError):
                playback_target(data, self.folder, {'start': 1.5, 'end': 2})
            writer.writeframes(b'\0\0' * 16000)
            file.flush()
            self.assertEqual(playback_target(data, self.folder, {'start': 1.5, 'end': 2})[1], (1.5, 2.))


class FakePlayer:
    def __init__(self):
        self.events = queue.Queue()
        self.generation = 0
        self.played = []
        self.closed = False

    def stop(self):
        self.generation += 1

    def play(self, source, intervals):
        self.stop()
        self.played.append((source, intervals))
        return self.generation

    def close(self):
        self.stop()
        self.closed = True


class HistoryPlaybackLifecycle(unittest.TestCase):
    def setUp(self):
        self.panel = panel = object.__new__(HistoryWindow)
        panel.app = SimpleNamespace(segments=[{'segment_id': 'a', 'start': 1., 'end': 2., 'text': '最初'},
                                             {'segment_id': 'b', 'start': 5., 'end': 6., 'text': '次'}],
                                    result_path=Path(DATA.name) / 'result.json', result_data={},
                                    live_recording=False)
        panel.audio_player = self.player = FakePlayer()
        panel.audio_key = panel.audio_generation = panel.audio_interval = panel.audio_requested_interval = None
        panel.audio_status = Mock()
        panel.audio_stop = Mock()
        panel.mark_playing = Mock()
        panel.pause = Mock()
        panel.view = Mock()
        panel.view.tag_ranges.return_value = ()
        self.target = patch('history_audio.playback_target', side_effect=lambda data, folder, row:
                            (Path(DATA.name) / 'recording.wav', utterance_interval(row)))
        self.target.start()
        self.addCleanup(self.target.stop)

    def test_switch_uses_current_segment_and_ignores_stale_completion(self):
        panel = self.panel
        panel.play_entry('a')
        old = panel.audio_generation
        panel.play_entry('b')
        self.assertEqual(self.player.played[-1][1], [(5., 6.)])
        self.player.events.put((old, 'ended', None))
        panel.poll_audio()
        self.assertEqual(panel.audio_key, 'b')
        self.player.events.put((panel.audio_generation, 'ended', None))
        panel.poll_audio()
        self.assertIsNone(panel.audio_key)
        panel.audio_stop.configure.assert_called_with(state='disabled')

    def test_changed_timing_stops_but_changed_text_does_not(self):
        panel = self.panel
        panel.play_entry('a')
        panel.app.segments[0]['text'] = '文章のみ修正'
        panel.poll_audio()
        self.assertEqual(panel.audio_key, 'a')
        panel.app.segments[0]['end'] = 3.
        panel.poll_audio()
        self.assertIsNone(panel.audio_key)
        panel.play_entry('a')
        self.assertEqual(self.player.played[-1][1], [(1., 3.)])

    def test_deleted_utterance_stops(self):
        self.panel.play_entry('a')
        self.panel.app.segments.pop(0)
        self.panel.poll_audio()
        self.assertIsNone(self.panel.audio_key)

    def test_close_releases_player_and_resets_controls(self):
        self.panel.play_entry('a')
        self.panel.close_audio()
        self.assertTrue(self.player.closed)
        self.assertIsNone(self.panel.audio_player)
        self.assertIsNone(self.panel.audio_requested_interval)
        self.panel.audio_stop.configure.assert_called_with(state='disabled')

    def test_errors_are_inline_and_restore_stop_state(self):
        panel = self.panel
        panel.play_entry('a')
        self.player.events.put((panel.audio_generation, 'error', '音声がありません'))
        panel.poll_audio()
        self.assertIsNone(panel.audio_generation)
        panel.audio_status.set.assert_called_with('再生できません：音声がありません')

    def test_backup_prevents_playback(self):
        self.panel.app.data_transfer_active = True
        self.panel.play_entry('a')
        self.assertFalse(self.player.played)

    def test_live_playback_shows_loopback_notice(self):
        panel = self.panel
        panel.app.live_recording = True
        panel.play_entry('a')
        self.player.events.put((panel.audio_generation, 'playing', 1.2))
        panel.poll_audio()
        self.assertIn('再生音も取り込まれる', panel.audio_status.set.call_args.args[0])

    def test_drag_and_text_selection_do_not_start_playback(self):
        panel = self.panel
        panel.entry_at = Mock(return_value='a')
        panel.play_entry = Mock()
        start = SimpleNamespace(x=5, y=5)
        end = SimpleNamespace(x=25, y=5)
        panel.press_entry(start)
        panel.drag_entry(end)
        panel.release_entry(end)
        panel.play_entry.assert_not_called()
        panel.view.tag_ranges.return_value = ('1.0', '1.3')
        panel.press_entry(start)
        panel.release_entry(start)
        panel.play_entry.assert_not_called()
        panel.view.tag_ranges.return_value = ()
        panel.press_entry(start)
        panel.release_entry(start)
        panel.play_entry.assert_called_once_with('a')


if __name__ == '__main__':
    unittest.main()
