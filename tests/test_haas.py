import hashlib
import io
import json
import os
from pathlib import Path
import queue
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
DATA = tempfile.TemporaryDirectory(prefix='whisper-haas-tests-')
for key in list(os.environ):
    if key.upper().startswith('WHISPER_'):
        del os.environ[key]
os.environ['WHISPER_USER_DATA_DIR'] = str(Path(DATA.name) / 'user-data')
os.environ['WHISPER_LEGACY_DATA_DIR'] = str(Path(DATA.name) / 'empty-legacy')

import asr
import haas_model
from haas_asr import HaasRecognizer, transcribe_file
from haas_decode import timed_result
from recognition_models import HAAS_MODEL, HAAS_LABEL, FILE_MODELS, LIVE_MODELS, recognition_language
from realtime_asr import LiveStopped


class HaasDownloads(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        info = {'size': 4, 'sha256': hashlib.sha256(b'good').hexdigest()}
        (self.root / 'haas_model_manifest.json').write_text(json.dumps(
            {'repo': 'author/model', 'revision': 'abc123', 'files': {'encoder.data': info}}))
        self.folder_model = self.root / 'models' / haas_model.FOLDER
        self.folder_model.mkdir(parents=True)
        self.target = self.folder_model / 'encoder.data'
        self.patch = patch.object(haas_model, 'ROOT', self.root)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_corrupt_same_size_asset_is_replaced_and_revision_pinned(self):
        self.target.write_bytes(b'evil')
        with patch('urllib.request.urlopen', return_value=io.BytesIO(b'good')) as request:
            haas_model.ensure_haas(self.root / 'models')
        self.assertEqual(self.target.read_bytes(), b'good')
        self.assertIn('/resolve/abc123/encoder.data', request.call_args.args[0].full_url)

    def test_checksum_failure_preserves_previous_asset_and_cleans_partial(self):
        self.target.write_bytes(b'old!')
        with patch('urllib.request.urlopen', return_value=io.BytesIO(b'evil')):
            with self.assertRaisesRegex(RuntimeError, '検証'):
                haas_model.ensure_haas(self.root / 'models')
        self.assertEqual(self.target.read_bytes(), b'old!')
        self.assertEqual(list(self.folder_model.iterdir()), [self.target])

    def test_verified_asset_reused_offline(self):
        self.target.write_bytes(b'good')
        with patch('urllib.request.urlopen') as request:
            haas_model.ensure_haas(self.root / 'models')
        request.assert_not_called()

    def test_download_can_be_cancelled_without_leaving_partial(self):
        calls = iter([False, True])
        with patch('urllib.request.urlopen', return_value=io.BytesIO(b'good')):
            with self.assertRaises(InterruptedError):
                haas_model.ensure_haas(self.root / 'models', stopped=lambda: next(calls))
        self.assertEqual(list(self.folder_model.iterdir()), [])


class HaasTiming(unittest.TestCase):
    def test_native_times_are_preserved_and_sentences_split(self):
        result = timed_result('正当です。次です。', [' 正', '当です', '。', '次です', '。'],
                              [.2, .4, .8, 2.1, 2.7], [.2, .4, .1, .6, .1], 3.)
        self.assertEqual([r['text'] for r in result['segments']], ['正当です。', '次です。'])
        self.assertEqual(result['segments'][1]['start'], 2.1)
        self.assertEqual(result['segments'][1]['end'], 2.8)
        self.assertEqual(result['language'], 'ja')

    def test_bad_or_nonmatching_token_times_never_fabricate_alignment(self):
        for tokens, starts, durations in [(['別の文字'], [.1], [.2]),
                                         (['正当です'], [float('nan')], [.2]),
                                         (['正当です'], [.1], [-.1]),
                                         (['正当です'], [], [])]:
            with self.subTest(tokens=tokens, starts=starts):
                result = timed_result('正当です', tokens, starts, durations, 2.)
                self.assertEqual(result['segments'], [{'start': 0., 'end': 2., 'text': '正当です', 'words': []}])

    def test_emissions_are_bounded_by_audio_duration(self):
        result = timed_result('はい', ['はい'], [.9], [.4], 1.)
        self.assertEqual(result['segments'][0]['end'], 1.)

    def test_late_punctuation_is_not_dropped_by_sentence_grouping(self):
        result = timed_result('はい。', ['はい', '。'], [.1, 2.0], [.2, .1], 3.)
        self.assertEqual(''.join(row['text'] for row in result['segments']), 'はい。')

    def test_blank_and_punctuation_only_are_empty(self):
        self.assertEqual(timed_result('。', ['。'], [0], [1], 1)['segments'], [])


class HaasIntegration(unittest.TestCase):
    def test_live_pipeline_publishes_and_saves_haas_captions(self):
        import engine
        import realtime

        class Capture:
            def __init__(self, device, wav, chunks, stopped, seconds, meter):
                self.total, self.error = 16000, None
                self.ready, self.done, self.halt = threading.Event(), threading.Event(), threading.Event()
                self.thread = Mock()
                with wave.open(str(wav), 'wb') as output:
                    output.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
                    output.writeframes(b'\xff\x0f' * self.total)
                chunks.put((0, self.total, 0))
                chunks.put(None)
                self.ready.set()
                self.done.set()
            def start(self):
                pass
            def stop(self):
                self.halt.set()

        with tempfile.TemporaryDirectory() as folder:
            fake = Mock(device='CPU', backend='haas-ja-v1.0/sherpa-onnx-quantized/cpu', failures=[])
            fake.transcribe.return_value = timed_result('正当です', ['正当です'], [.1], [.6], 1.)
            events = []
            request = {'model': HAAS_MODEL, 'language': 'en', 'device': {'name': 'test fixture'},
                       'live_diarize': False, 'diarize': False, 'live_correct': False, 'context_correct': False}
            with patch.object(engine, 'RESULTS', Path(folder) / 'results'), patch.object(realtime, 'LoopbackCapture', Capture), \
                 patch('haas_asr.HaasRecognizer', return_value=fake), patch.object(engine, 'emit', side_effect=lambda kind, **values: events.append({'kind': kind, **values})), \
                 patch.object(engine, 'cache_audio', side_effect=lambda wav, _: str(wav)):
                realtime.run_live(request, Path(folder))
            completed = next(event for event in events if event['kind'] == 'complete')
            data = json.loads(Path(completed['result_path']).read_text('utf-8'))
            self.assertFalse(data['live_incomplete'], data.get('live_error'))
            self.assertTrue(data['recording_complete'])
            self.assertEqual(data['timestamp_method'], 'tdt-tokens-or-chunk')
            self.assertEqual(data['language'], 'ja')
            self.assertEqual(data['segments'][0]['text'], '正当です')
            self.assertTrue(any(event['kind'] == 'live_segments' for event in events))
            fake.close.assert_called_once()

    def test_file_dispatch_and_cpu_option_do_not_prepare_whisper(self):
        with patch('haas_asr.transcribe_file', return_value={'device': 'CPU'}) as haas:
            with patch('asr.ensure_model') as whisper:
                self.assertEqual(asr.transcribe('wav', HAAS_MODEL, 'en', 'hint', 'job', 'models', 'gpu')['device'], 'CPU')
        whisper.assert_not_called()
        self.assertEqual(haas.call_args.args[:6], ('wav', 'en', 'hint', 'job', 'models', 'gpu'))

    def test_live_preload_reuses_worker_until_model_changes(self):
        from live_models import LiveModels
        with tempfile.TemporaryDirectory() as folder:
            models = LiveModels(folder)
            fake = Mock()
            fake.proc.poll.return_value = None
            with patch('haas_asr.HaasRecognizer', return_value=fake) as haas:
                request = {'model': HAAS_MODEL, 'compute': 'gpu'}
                one = models.recognizer_for(request, 'ja', '', lambda _: None, lambda: False)
                models.park()
                two = models.recognizer_for(request, 'ja', '', lambda _: None, lambda: False)
                self.assertIs(one, two)
                haas.assert_called_once()
                fake.transcribe.assert_called_once()  # first prepare only
                with patch('granite_asr.GraniteRecognizer') as granite:
                    granite.return_value.proc.poll.return_value = None
                    models.recognizer_for({'model': 'granite-turboctc'}, 'auto', '', lambda _: None, lambda: False)
                    fake.close.assert_called_once()
                models.close()

    def test_cancelled_inference_closes_worker_so_stale_reply_cannot_be_reused(self):
        instance = HaasRecognizer.__new__(HaasRecognizer)
        instance.stopped = lambda: True
        instance.lock = threading.Lock()
        instance.close = Mock()
        with self.assertRaises(LiveStopped):
            instance.transcribe(b'\0\0' * 16000)
        instance.close.assert_called_once()

    def test_worker_error_is_reported(self):
        instance = HaasRecognizer.__new__(HaasRecognizer)
        instance.stopped = lambda: False
        instance.responses = queue.Queue()
        instance.responses.put({'kind': 'error', 'message': 'test error'})
        with self.assertRaisesRegex(RuntimeError, 'test error'):
            instance.receive()

    def test_long_file_offsets_include_words_without_losing_samples(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'long.wav'
            with wave.open(str(source), 'wb') as output:
                output.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
                output.writeframes(b'\xff\x0f' * (16000 * 43))
            fake = Mock()
            fake.backend = 'haas-test/cpu'
            fake.transcribe.return_value = {'segments': [{'start': .1, 'end': .9, 'text': '声',
                                            'words': [{'word': '声', 'start': .1, 'end': .9}]}]}
            with patch('haas_asr.HaasRecognizer', return_value=fake):
                result = transcribe_file(source, 'ja', '', folder, folder, 'cpu', lambda _: None)
            self.assertEqual([r['start'] for r in result['segments']], [.1, 20.1, 40.1])
            self.assertEqual(result['segments'][-1]['words'][0]['end'], 40.9)
            self.assertEqual(sum(len(c.args[0]) for c in fake.transcribe.call_args_list), 43 * 32000)
            fake.close.assert_called_once()

    def test_settings_restore_haas_and_language_is_japanese(self):
        from recording_settings import load_settings, save_settings, settings_for
        with tempfile.TemporaryDirectory() as folder:
            with patch('recording_settings.settings_path', return_value=Path(folder) / 'recording_settings.json'):
                save_settings(settings_for('audio', {}, model=HAAS_MODEL))
                self.assertEqual(load_settings()['model'], HAAS_MODEL)
        self.assertEqual(FILE_MODELS[HAAS_LABEL], HAAS_MODEL)
        self.assertEqual(LIVE_MODELS[HAAS_LABEL], HAAS_MODEL)
        self.assertEqual(recognition_language(HAAS_MODEL, 'en'), 'ja')
        self.assertEqual(recognition_language('base', 'en'), 'en')
        self.assertEqual(recognition_language('granite-turboctc', 'en'), 'auto')


if __name__ == '__main__':
    unittest.main()
