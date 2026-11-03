import argparse
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import setup_local as setup


class SetupReadiness(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='whisper-auto-setup-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        for name in setup.SETUP_INPUTS:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('# fixture\n', 'utf-8')
        (self.root / 'requirements-lock.txt').write_text('einops==0.8.1\n', 'utf-8')
        (self.root / 'whisper_hf_manifest.json').write_text(json.dumps({'base': {'files': {'model.safetensors': {}}}}), 'utf-8')
        self.versions = {'torch': '2.11.0+cpu', 'torchaudio': '2.11.0+cpu', 'einops': '0.8.1'}
        self.addCleanup(patch.stopall)
        patch('setup_local.importlib.metadata.version', side_effect=lambda name: self.versions[name]).start()
        for name in setup.required_assets('base', True, self.root):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b'asset')
        setup.save_state('cpu', 'cpu', 'base', self.root)

    def test_ready_is_offline_and_does_not_install(self):
        with patch('urllib.request.urlopen', side_effect=AssertionError('network')), patch.object(setup, 'run') as run:
            self.assertTrue(setup.setup_ready(self.root))
            run.assert_not_called()

    def test_dependency_update_and_missing_package_require_setup(self):
        self.versions['einops'] = '0.7.0'
        self.assertFalse(setup.setup_ready(self.root))
        with patch('setup_local.importlib.metadata.version', side_effect=importlib.metadata.PackageNotFoundError):
            self.assertFalse(setup.setup_ready(self.root))

    def test_changed_manifest_requires_setup(self):
        (self.root / 'requirements-lock.txt').write_text('einops==0.8.2\n', 'utf-8')
        self.assertFalse(setup.setup_ready(self.root))

    def test_truncated_or_missing_model_requires_setup(self):
        model = self.root / 'models/whisper-hf-base/model.safetensors'
        model.write_bytes(b'x')
        self.assertFalse(setup.setup_ready(self.root))
        model.unlink()
        self.assertFalse(setup.setup_ready(self.root))

    def test_cuda_setup_requires_nemotron_but_cpu_does_not(self):
        model = self.root / 'models/nemotronlabs-re-use/model.safetensors'
        model.unlink()
        self.assertTrue(setup.setup_ready(self.root))
        model.write_bytes(b'asset')
        self.versions.update(torch='2.11.0+cu128', torchaudio='2.11.0+cu128')
        setup.save_state('cuda', 'auto', 'base', self.root)
        self.assertTrue(setup.setup_ready(self.root))
        model.unlink()
        self.assertFalse(setup.setup_ready(self.root))

    def test_old_or_interrupted_setup_is_not_ready(self):
        marker = self.root / '.setup-complete.json'
        for content in ('{"device":"cuda","model":"base"}', '{', 'null'):
            marker.write_text(content, 'utf-8')
            self.assertFalse(setup.setup_ready(self.root))


class SetupFlow(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='whisper-setup-flow-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        python = self.root / '.venv/Scripts/python.exe'
        python.parent.mkdir(parents=True)
        python.write_bytes(b'fixture')
        self.marker = self.root / '.setup-complete.json'
        self.marker.write_text('{}', 'utf-8')
        self.sentinel = self.root / 'user-data/speaker_memory.json'
        self.sentinel.parent.mkdir()
        self.sentinel.write_bytes(b'preserve voices and settings')
        self.args = argparse.Namespace(ensure=False, device=None, model=None)
        self.addCleanup(patch.stopall)
        patch.object(setup, 'ROOT', self.root).start()
        self.saved = patch.object(setup, 'read_state', return_value={}).start()
        self.run = patch.object(setup, 'run').start()
        self.install_torch = patch.object(setup, 'install_torch').start()
        self.save = patch.object(setup, 'save_state').start()
        self.gpu = patch.object(setup, 'nvidia_available', return_value=True).start()
        self.cuda = patch.object(setup, 'cuda_works', return_value=True).start()

    def test_gpu_setup_prepares_noise_without_changing_user_data(self):
        setup.install(self.args)
        commands = [call.args[0] for call in self.run.call_args_list]
        self.assertTrue(any('--prepare-noise' in command for command in commands))
        self.save.assert_called_once_with('cuda', 'auto', 'base')
        self.assertEqual(self.sentinel.read_bytes(), b'preserve voices and settings')

    def test_explicit_cpu_and_model_persist_on_automatic_update(self):
        self.saved.return_value = {'requested_device': 'cpu', 'model': 'haas'}
        setup.install(self.args)
        self.cuda.assert_not_called()
        self.save.assert_called_once_with('cpu', 'cpu', 'haas')
        self.assertFalse(any('--prepare-noise' in call.args[0] for call in self.run.call_args_list))

    def test_unusable_cuda_falls_back_to_cpu_only_in_auto_mode(self):
        self.cuda.return_value = False
        setup.install(self.args)
        self.assertEqual([call.args[1] for call in self.install_torch.call_args_list], ['cuda', 'cpu'])
        self.save.assert_called_once_with('cpu', 'auto', 'base')
        self.args.device = 'cuda'
        self.save.reset_mock()
        with self.assertRaisesRegex(RuntimeError, 'CUDA'):
            setup.install(self.args)
        self.save.assert_not_called()

    def test_failure_leaves_no_complete_marker_and_preserves_data(self):
        self.args.device = 'cpu'
        self.args.model = 'haas'
        self.run.side_effect = subprocess.CalledProcessError(1, ['fixture'])
        with self.assertRaises(subprocess.CalledProcessError):
            setup.install(self.args)
        incomplete = json.loads(self.marker.read_text('utf-8'))
        self.assertNotIn('fingerprint', incomplete)
        self.assertEqual(incomplete, {'requested_device': 'cpu', 'model': 'haas'})
        self.save.assert_not_called()
        self.assertEqual(self.sentinel.read_bytes(), b'preserve voices and settings')

    def test_ready_start_does_not_probe_gpu_or_reinstall(self):
        self.args.ensure = True
        with patch('setup_local.subprocess.run', return_value=Mock(returncode=0)):
            setup.install(self.args)
        self.gpu.assert_not_called()
        self.install_torch.assert_not_called()
        self.run.assert_not_called()

    @unittest.skipUnless(os.name == 'nt', 'Windows setup lock')
    def test_parallel_setup_is_rejected_and_lock_released(self):
        with self.assertRaisesRegex(RuntimeError, '別のセットアップ'):
            with setup.setup_lock(self.root):
                with setup.setup_lock(self.root):
                    self.fail('second setup entered')
        with setup.setup_lock(self.root):
            pass


if __name__ == '__main__':
    unittest.main()
