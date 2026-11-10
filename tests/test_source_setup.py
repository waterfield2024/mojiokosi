import hashlib
import io
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
DATA = tempfile.TemporaryDirectory(prefix='whisper-source-tests-')
for key in list(os.environ):
    if key.upper().startswith('WHISPER_'):
        del os.environ[key]
os.environ['WHISPER_USER_DATA_DIR'] = str(Path(DATA.name) / 'user-data')
os.environ['WHISPER_LEGACY_DATA_DIR'] = str(Path(DATA.name) / 'empty-legacy')

import asr
import setup_local


class Downloads(unittest.TestCase):
    def test_bad_download_keeps_previous_file(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / 'asset'
            target.write_bytes(b'original')
            with patch('urllib.request.urlopen', return_value=io.BytesIO(b'bad')):
                with self.assertRaisesRegex(RuntimeError, 'checksum'):
                    setup_local.download('https://example.org/asset', target, hashlib.sha256(b'good').hexdigest())
            self.assertEqual(target.read_bytes(), b'original')
            self.assertEqual(list(Path(folder).iterdir()), [target])

    def test_verified_asset_not_downloaded_again(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / 'asset'
            target.write_bytes(b'good')
            with patch('urllib.request.urlopen') as request:
                setup_local.download('https://example.org/asset', target, hashlib.sha256(b'good').hexdigest())
                request.assert_not_called()

    def test_interrupted_download_cleans_temporary(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / 'asset'
            with patch('urllib.request.urlopen', side_effect=OSError('offline')):
                with self.assertRaises(OSError):
                    setup_local.download('https://example.org/asset', target, '0' * 64)
            self.assertEqual(list(Path(folder).iterdir()), [])

    def test_good_download_replaces_asset(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / 'asset'
            with patch('urllib.request.urlopen', return_value=io.BytesIO(b'good')):
                setup_local.download('https://example.org/asset', target, hashlib.sha256(b'good').hexdigest())
            self.assertEqual(target.read_bytes(), b'good')


class SourceRecognition(unittest.TestCase):
    def test_blocked_cpu_dll_uses_official_cpu_capable_build(self):
        with patch.object(setup_local, 'install_torch_wheel') as install, patch.object(setup_local, 'run') as run:
            with patch.object(setup_local.subprocess, 'run', return_value=Mock(returncode=1, stderr=b'[WinError 4551]')):
                self.assertTrue(setup_local.install_torch('python', 'cpu'))
            self.assertEqual(install.call_args.args, ('python', setup_local.torch_packages('cpu', cpu_compat=True)[0]))
            self.assertEqual(run.call_args.kwargs['env']['CUDA_VISIBLE_DEVICES'], '-1')

    def test_device_switch_uses_distinct_wheel_build(self):
        self.assertEqual(setup_local.torch_packages('cpu'), [f'torch=={setup_local.TORCH_VERSION}+cpu', f'torchaudio=={setup_local.TORCHAUDIO_VERSION}+cpu'])
        self.assertEqual(setup_local.torch_packages('cuda'), [f'torch=={setup_local.TORCH_VERSION}+cu130', f'torchaudio=={setup_local.TORCHAUDIO_VERSION}+cu132'])

    def test_file_skips_native_model_when_not_installed(self):
        with patch.object(asr, 'native_backends', return_value=[]), patch.object(asr, 'ensure_model') as native:
            fake = Mock(return_value={'segments': [], 'device': 'CPU'})
            with patch('whisper_hf_file.transcribe_file', fake):
                result = asr.transcribe('audio.wav', 'base', 'ja', 'hint', 'job', 'models', 'cpu')
            native.assert_not_called()
            self.assertEqual(fake.call_args.args[:6], ('audio.wav', 'base', 'ja', 'hint', 'job', 'cpu'))
            self.assertEqual(result['fallbacks'], [])

    def test_comparison_prepares_hf_model(self):
        with patch.object(asr, 'native_backends', return_value=[]), patch.object(asr, 'ensure_model') as native:
            with patch('whisper_hf_model.ensure_whisper_hf', return_value='prepared') as hf:
                self.assertEqual(asr.prepare_whisper_model('turbo', 'models'), 'prepared')
            native.assert_not_called()
            self.assertEqual(hf.call_args.args[:2], ('turbo', 'models'))

    def test_live_starts_hf_directly_without_native_download(self):
        from realtime_asr import LiveRecognizer
        with tempfile.TemporaryDirectory() as folder:
            with patch.object(asr, 'native_backends', return_value=[]), patch('realtime_asr.CppLiveRecognizer') as native:
                with patch('whisper_hf_asr.WhisperHFRecognizer') as hf:
                    recognizer = LiveRecognizer('base', 'ja', '', 'cpu', folder, lambda _: None)
                    self.assertTrue(recognizer.compatible)
                    self.assertEqual(recognizer.native_failures, [])
                    native.assert_not_called()
                    recognizer.close()
                    hf.return_value.close.assert_called_once()

    def test_native_mode_keeps_validation(self):
        with self.assertRaises(ValueError):
            asr.native_backends('unknown')

    def test_new_user_context_correction_is_optional(self):
        from context_ai import load_settings
        self.assertFalse(load_settings(Path(DATA.name) / 'missing-context.json')['auto_apply'])

    def test_new_live_user_does_not_require_ollama(self):
        from recording_settings import processing_preferences
        preferences = processing_preferences({}, context_default=False)
        self.assertFalse(preferences['live_correct'])
        self.assertFalse(preferences['context_correct'])
        self.assertTrue(preferences['live_diarize'])


if __name__ == '__main__':
    unittest.main()
