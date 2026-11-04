import base64
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
DATA = tempfile.TemporaryDirectory(prefix='whisper-noise-tests-')
for key in list(os.environ):
    if key.upper().startswith('WHISPER_'):
        del os.environ[key]
os.environ['WHISPER_USER_DATA_DIR'] = str(Path(DATA.name) / 'user-data')
os.environ['WHISPER_LEGACY_DATA_DIR'] = str(Path(DATA.name) / 'empty-legacy')

from live_noise import LiveNoiseReducer, update_control
from reuse_process import ReuseProcess
import reuse_assets


class FakeWorker:
    def __init__(self, *args):
        self.closed = False
        self.calls = []
    def healthy(self): return not self.closed
    def enhance(self, pcm, timeout=3):
        self.calls.append(pcm)
        return b'\x01\x00' * (len(pcm)//2)
    def close(self): self.closed = True


class NoiseLifecycle(unittest.TestCase):
    def setUp(self):
        self.factory = Mock(side_effect=FakeWorker)
        self.reducer = LiveNoiseReducer(DATA.name, worker_factory=self.factory)
        self.addCleanup(self.reducer.close)
        self.pcm = b'\xff\x1f'*32000

    def prepare(self):
        return self.reducer.prepare(True, 'auto', lambda _: None, lambda: False)

    def test_off_never_loads_model_and_is_bit_exact(self):
        self.reducer.prepare(False, 'auto', lambda _: None, lambda: False)
        output, used = self.reducer.process(self.pcm)
        self.assertIs(output, self.pcm)
        self.assertFalse(used)
        self.factory.assert_not_called()

    def test_on_off_on_reuses_model_and_preserves_sample_count(self):
        self.assertEqual(self.prepare()['state'], 'ready')
        original = hashlib.sha256(self.pcm).hexdigest()
        output, used = self.reducer.process(self.pcm)
        self.assertTrue(used)
        self.assertNotEqual(output, self.pcm)
        self.assertEqual(len(output), len(self.pcm))
        self.assertEqual(hashlib.sha256(self.pcm).hexdigest(), original)
        self.reducer.configure(False)
        self.assertEqual(self.reducer.process(self.pcm), (self.pcm, False))
        self.reducer.configure(True)
        self.assertTrue(self.reducer.process(self.pcm)[1])
        self.factory.assert_called_once()

    def test_loading_does_not_block_original_recognition(self):
        gate = threading.Event()
        def load(*args):
            gate.wait(1)
            return FakeWorker()
        self.reducer.factory = load
        self.reducer.configure(True)
        try:
            self.assertEqual(self.reducer.process(self.pcm), (self.pcm, False))
            self.assertEqual(self.reducer.snapshot()['state'], 'loading')
        finally:
            gate.set()
            self.reducer.thread.join(2)
        self.assertTrue(self.reducer.process(self.pcm)[1])

    def test_off_during_loading_releases_candidate(self):
        gate = threading.Event()
        worker = FakeWorker()
        def load(*args):
            gate.wait(1)
            return worker
        self.reducer.factory = load
        self.reducer.configure(True)
        self.reducer.pump()
        self.reducer.configure(False)
        gate.set()
        self.reducer.thread.join(2)
        self.assertTrue(worker.closed)
        self.assertIsNone(self.reducer.worker)
        self.assertEqual(self.reducer.snapshot()['state'], 'off')

    def test_failure_keeps_original_and_does_not_retry_each_chunk(self):
        self.prepare()
        worker = self.reducer.worker
        worker.enhance = Mock(side_effect=TimeoutError('test timeout'))
        self.assertEqual(self.reducer.process(self.pcm), (self.pcm, False))
        self.assertTrue(worker.closed)
        self.assertEqual(self.reducer.snapshot()['state'], 'suspended')
        self.reducer.process(self.pcm)
        self.factory.assert_called_once()
        self.reducer.configure(False)
        self.reducer.configure(True)
        self.prepare()
        self.assertTrue(self.reducer.process(self.pcm)[1])

    def test_invalid_audio_length_cannot_shift_subtitles(self):
        self.prepare()
        self.reducer.worker.enhance = Mock(return_value=b'\0\0')
        self.assertEqual(self.reducer.process(self.pcm), (self.pcm, False))
        self.assertEqual(self.reducer.snapshot()['state'], 'suspended')

    def test_repeated_slow_processing_suspends_without_dropping_audio(self):
        self.prepare()
        with patch('live_noise.time.monotonic', side_effect=[10., 11.4, 20., 21.4]):
            self.assertTrue(self.reducer.process(self.pcm)[1])
            self.assertTrue(self.reducer.process(self.pcm)[1])
        self.assertEqual(self.reducer.snapshot()['state'], 'suspended')
        self.assertEqual(self.reducer.process(self.pcm), (self.pcm, False))
        self.assertEqual(len(self.reducer.worker.calls), 2)

    def test_cpu_choice_does_not_start_gpu_worker(self):
        state = self.reducer.prepare(True, 'cpu', lambda _: None, lambda: False)
        self.assertEqual(state['state'], 'suspended')
        self.assertEqual(self.reducer.process(self.pcm), (self.pcm, False))
        self.factory.assert_not_called()

    def test_invalid_or_other_job_controls_cannot_enable(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'noise-control.json'
            path.write_text('{bad', 'utf-8')
            update_control(self.reducer, folder)
            self.assertFalse(self.reducer.enabled)
            path.write_text(json.dumps({'enabled': 'yes'}), 'utf-8')
            update_control(self.reducer, folder)
            self.assertFalse(self.reducer.enabled)
            path.write_text(json.dumps({'enabled': True}), 'utf-8')
            update_control(self.reducer, folder)
            self.assertTrue(self.reducer.enabled)

    def test_choice_restored_and_backup_validated(self):
        from recording_settings import processing_preferences, save_settings, load_settings, settings_for
        from data_backup import _validate
        self.assertFalse(processing_preferences({})['live_noise'])
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            with patch('recording_settings.settings_path', return_value=folder/'recording_settings.json'):
                save_settings(settings_for('audio', {}, processing={'live_noise': True}))
                self.assertTrue(load_settings()['processing']['live_noise'])
                self.assertEqual(_validate(folder, folder)['settings'], 1)

    def test_fast_off_on_retry_is_not_lost_while_a_chunk_was_running(self):
        self.prepare()
        self.reducer.problem = 'previous failure'
        with tempfile.TemporaryDirectory() as folder:
            control = Path(folder)/'noise-control.json'
            control.write_text(json.dumps({'enabled': True, 'token': 'new-choice'}), 'utf-8')
            update_control(self.reducer, folder)
            self.assertFalse(self.reducer.problem)
            self.reducer.problem = 'another failure'
            update_control(self.reducer, folder)
            self.assertEqual(self.reducer.problem, 'another failure')


class NoiseWeightsAndProtocol(unittest.TestCase):
    def test_download_mismatch_keeps_old_weights_and_removes_partial(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'nemotronlabs-re-use/model.safetensors'
            path.parent.mkdir()
            path.write_bytes(b'original')
            with patch('urllib.request.urlopen', return_value=io.BytesIO(b'bad')):
                with self.assertRaises(RuntimeError):
                    reuse_assets.ensure_reuse(folder)
            self.assertEqual(path.read_bytes(), b'original')
            self.assertEqual(list(path.parent.iterdir()), [path])

    def test_verified_weights_reused_offline(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'nemotronlabs-re-use/model.safetensors'
            path.parent.mkdir()
            path.write_bytes(b'fixture')
            with patch.object(reuse_assets, 'SIZE', 7), patch.object(reuse_assets, 'SHA256', hashlib.sha256(b'fixture').hexdigest()), \
                 patch('urllib.request.urlopen') as network:
                self.assertEqual(reuse_assets.ensure_reuse(folder), path)
                network.assert_not_called()

    def test_wrong_length_response_closes_worker_preventing_stale_results(self):
        process = ReuseProcess.__new__(ReuseProcess)
        process.lock = threading.Lock()
        process.proc = Mock()
        process.proc.poll.return_value = None
        process.receive = Mock(return_value={'kind': 'result', 'pcm': base64.b64encode(b'\0\0').decode()})
        process.close = Mock()
        with self.assertRaises(ValueError):
            process.enhance(b'\0\0'*100)
        process.close.assert_called_once()


class NoisePipeline(unittest.TestCase):
    def test_only_asr_copy_changes_while_recording_speakers_and_cache_stay_original(self):
        import engine
        import realtime
        original = b'\x02\x20'*32000
        events, heard, speaker_audio = [], [], []
        with tempfile.TemporaryDirectory() as folder:
            folder = Path(folder)
            reducer = LiveNoiseReducer(folder, worker_factory=FakeWorker)
            self.addCleanup(reducer.close)
            class Capture:
                def set_sensitivity(self, *args): pass
                def set_segmentation(self, value, **kwargs): pass
                def __init__(self, device, wav, chunks, stopped, seconds, meter):
                    self.total, self.error = 32000, None
                    self.ready, self.done, self.halt = threading.Event(), threading.Event(), threading.Event()
                    self.thread = Mock()
                    with wave.open(str(wav), 'wb') as output:
                        output.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
                        output.writeframes(original)
                    self.ready.set(); self.done.set()
                def start(self): pass
                def stop(self): self.halt.set()
            def recognize(pcm):
                heard.append(pcm)
                (folder/'noise-control.json').write_text(json.dumps({'enabled': False}), 'utf-8')
                return {'segments': [{'start': 0., 'end': 1., 'text': '発言'+str(len(heard))}]}
            def speakers(path, start, end, names, *, preview=False):
                with wave.open(str(path), 'rb') as audio:
                    speaker_audio.append(audio.readframes(audio.getnframes()))
                return [{'start': 0., 'end': 2., 'speaker': 'speaker_0'}]
            recognizer = Mock(device='CPU', backend='test', failures=[], transcribe=Mock(side_effect=recognize))
            diarizer = Mock(profiles={}, remembered={}, memory_error='', process=Mock(side_effect=speakers))
            request = {'model': 'haas-ja-v1.0', 'language': 'ja', 'device': {'name': 'test fixture'},
                       'live_diarize': True, 'live_noise': True, 'diarize': False, 'context_correct': False}
            with patch.object(engine, 'RESULTS', folder/'results'), patch.object(realtime, 'LoopbackCapture', Capture), \
                 patch.object(realtime, 'SpeechGate', return_value=Mock(warning='', intervals=lambda pcm: [(0, len(pcm)//2)])), \
                 patch.object(realtime, 'next_live_chunk', side_effect=[(0, 16000, 0), (16000, 32000, 16000), None]), \
                 patch('live_noise.LiveNoiseReducer', return_value=reducer), patch('haas_asr.HaasRecognizer', return_value=recognizer), \
                 patch.object(realtime, 'LiveSpeakerProcess', return_value=diarizer), \
                 patch.object(engine, 'emit', side_effect=lambda kind, **values: events.append({'kind': kind, **values})), \
                 patch.object(engine, 'cache_audio', side_effect=lambda wav, _: str(wav)):
                realtime.run_live(request, folder)
            completed = next(event for event in events if event['kind'] == 'complete')
            data = json.loads(Path(completed['result_path']).read_text('utf-8'))
            self.assertFalse(data['live_incomplete'], data.get('live_error'))
            with wave.open(data['audio_file'], 'rb') as audio:
                self.assertEqual(audio.readframes(audio.getnframes()), original)
            self.assertEqual(speaker_audio, [original, original])
            self.assertEqual(heard, [b'\x01\x00'*16000, original[32000:]])
            self.assertEqual([row['noise_reduced'] for row in data['segments']], [True, False])
            self.assertEqual([(row['start'], row['end']) for row in data['segments']], [(0., 1.), (1., 2.)])
            self.assertEqual(data['noise_reduction']['applied_chunks'], 1)
            self.assertEqual(data['noise_reduction']['bypassed_chunks'], 1)
            self.assertEqual(data['noise_reduction']['recording_audio'], 'original')


if __name__ == '__main__':
    unittest.main()
