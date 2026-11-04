import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import urllib.error
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import app_update as update


def archive_of(files, extra=None):
    memory = io.BytesIO()
    with zipfile.ZipFile(memory, 'w') as archive:
        for name, data in files.items():
            archive.writestr('mojiokosi-sha/' + name, data)
        if extra:
            for name, data in extra:
                archive.writestr(name, data)
    return memory.getvalue()


class Installation(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='whisper-update-')
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve() / '日本語 & space'
        self.root.mkdir()
        self.files = {name: b'# new\n' for name in update.REQUIRED}
        self.files.update({'docs/使い方.txt': '説明'.encode(), 'whisper.ico': b'icon',
                           'start.cmd': b'@echo off\r\n', 'tests/test_example.py': b'# test\n'})
        self.release = {'sha': 'a' * 40, 'files': {name: update.blob_hash(data) for name, data in self.files.items()},
                        'changes': len(self.files), 'message': 'update', 'date': '2026-11-04T00:00:00Z'}
        self.sentinels = {}
        for name in ('UserData/recording_settings.json', 'UserData/speaker_memory.json',
                     'UserData/speaker_memory_clips/voice.wav', 'UserData/corrections.json',
                     'UserData/transcripts/one/capture.mp4', 'UserData.before-restore-123/data.json',
                     '.venv/Lib/package.py', '.python/runtime/python.exe', 'models/model.bin',
                     '.tools/ffmpeg.exe', 'backups/my.zip', '.git/HEAD',
                     'transcripts/old/audio.wav', 'correction_memory.json', 'compute_settings.json'):
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(('personal:' + name).encode())
            self.sentinels[name] = path.read_bytes()
        (self.root / 'app.py').write_bytes(b'# previous\r\n')

    def prepare(self):
        with patch.object(update, 'get_url', return_value=archive_of(self.files)) as request:
            job = update.prepare_update(self.root, self.release)
            self.assertIn('/zip/' + 'a' * 40, request.call_args.args[0])
        return job

    def assert_personal_data(self):
        for name, data in self.sentinels.items():
            self.assertEqual((self.root / name).read_bytes(), data, name)

    def test_update_preserves_all_personal_data_models_environment_and_git(self):
        job = self.prepare()
        with update.installation_lock(self.root):
            update.apply_update(self.root, job)
        for name, data in self.files.items():
            self.assertEqual((self.root / name).read_bytes(), data)
        self.assertEqual((job / 'before/app.py').read_bytes(), b'# previous\r\n')
        self.assertEqual(update.current_version(self.root), 'aaaaaaaa')
        self.assertFalse((update.updates_dir(self.root) / 'pending.json').exists())
        self.assert_personal_data()

    def test_dependency_failure_restores_original_source_and_removes_new_files(self):
        job = self.prepare()
        with self.assertRaisesRegex(OSError, '更新前に戻しました'):
            update.apply_update(self.root, job, validate=lambda: (_ for _ in ()).throw(OSError('setup failed')))
        self.assertEqual((self.root / 'app.py').read_bytes(), b'# previous\r\n')
        self.assertFalse((self.root / 'update_ui.py').exists())
        self.assertFalse((update.updates_dir(self.root) / 'installed.json').exists())
        self.assert_personal_data()

    def test_interrupted_replacement_recovers_on_next_start(self):
        class PowerLoss(BaseException):
            pass
        job = self.prepare()
        original = update.atomic_copy
        def copy(source, target):
            original(source, target)
            if target == self.root / 'app.py':
                raise PowerLoss()
        with patch.object(update, 'atomic_copy', side_effect=copy):
            with self.assertRaises(PowerLoss):
                update.apply_update(self.root, job)
        self.assertTrue((update.updates_dir(self.root) / 'pending.json').exists())
        self.assertTrue(update.recover_pending(self.root))
        self.assertFalse(update.recover_pending(self.root))
        self.assertEqual((self.root / 'app.py').read_bytes(), b'# previous\r\n')
        self.assert_personal_data()

    def test_changed_source_after_download_is_not_overwritten(self):
        job = self.prepare()
        (self.root / 'app.py').write_bytes(b'user edit')
        with self.assertRaisesRegex(ValueError, '更新準備後'):
            update.apply_update(self.root, job)
        self.assertEqual((self.root / 'app.py').read_bytes(), b'user edit')
        self.assert_personal_data()

    def test_tampered_payload_rejected_before_installation(self):
        job = self.prepare()
        (job / 'payload/app.py').write_bytes(b'tampered')
        with self.assertRaisesRegex(ValueError, '準備した更新ファイル'):
            update.apply_update(self.root, job)
        self.assertEqual((self.root / 'app.py').read_bytes(), b'# previous\r\n')
        self.assert_personal_data()

    def test_second_update_removes_only_previously_managed_obsolete_source(self):
        self.files['obsolete.py'] = b'old module'
        self.release['files']['obsolete.py'] = update.blob_hash(b'old module')
        update.apply_update(self.root, self.prepare())
        (self.root / 'my_notes.txt').write_bytes(b'personal notes')
        del self.files['obsolete.py']
        del self.release['files']['obsolete.py']
        update.apply_update(self.root, self.prepare())
        self.assertFalse((self.root / 'obsolete.py').exists())
        self.assertEqual((self.root / 'my_notes.txt').read_bytes(), b'personal notes')
        self.assert_personal_data()

    def test_rollback_of_second_update_restores_previous_installed_version(self):
        update.apply_update(self.root, self.prepare())
        state = (update.updates_dir(self.root) / 'installed.json').read_bytes()
        self.release['sha'] = 'b' * 40
        self.files['app.py'] = b'# next\n'
        self.release['files']['app.py'] = update.blob_hash(self.files['app.py'])
        with patch.object(update, 'get_url', return_value=archive_of(self.files)):
            job = update.prepare_update(self.root, self.release)
        with self.assertRaises(OSError):
            update.apply_update(self.root, job, validate=lambda: (_ for _ in ()).throw(OSError('setup')))
        self.assertEqual((self.root / 'app.py').read_bytes(), b'# new\n')
        self.assertEqual((update.updates_dir(self.root) / 'installed.json').read_bytes(), state)
        self.assert_personal_data()

    def test_network_error_changes_no_installed_files(self):
        with patch.object(update, 'get_url', side_effect=OSError('offline')):
            with self.assertRaises(OSError):
                update.prepare_update(self.root, self.release)
        self.assertEqual((self.root / 'app.py').read_bytes(), b'# previous\r\n')
        self.assert_personal_data()

    def test_target_symlink_rejected_without_changing_destination(self):
        other = self.root / 'UserData/protected.txt'
        other.write_bytes(b'protected')
        (self.root / 'app.py').unlink()
        try:
            (self.root / 'app.py').symlink_to(other)
        except OSError:
            self.skipTest('symlink privilege unavailable')
        with self.assertRaisesRegex(ValueError, 'リンク先'):
            self.prepare()
        self.assertEqual(other.read_bytes(), b'protected')

    def test_application_lock_excludes_a_separate_process(self):
        module = Path(update.__file__).resolve().parent
        code = ('import sys; from pathlib import Path; '
                'sys.path.insert(0,sys.argv[1]); from app_update import installation_lock; '
                'lock=installation_lock(Path(sys.argv[2])); lock.__enter__()')
        with update.installation_lock(self.root):
            result = subprocess.run([sys.executable, '-X', 'utf8', '-c', code, str(module), str(self.root)],
                                    capture_output=True, timeout=15, creationflags=update.NO_WINDOW)
        self.assertNotEqual(result.returncode, 0)
        with update.installation_lock(self.root):
            pass

    def test_latest_check_uses_pinned_tree_and_ignores_checkout_newlines(self):
        for name, data in self.files.items():
            target = self.root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data.replace(b'\n', b'\r\n') if name.endswith('.py') else data)
        commit = {'sha': self.release['sha'], 'commit': {'message': 'new change\nbody',
                  'committer': {'date': self.release['date']}}}
        tree = {'tree': [{'path': name, 'sha': digest, 'type': 'blob', 'mode': '100644', 'size': len(self.files[name])}
                         for name, digest in self.release['files'].items()]}
        with patch.object(update, 'get_url', side_effect=[json.dumps(commit).encode(), json.dumps(tree).encode()]) as request:
            result = update.check_latest(self.root)
        self.assertEqual(result['changes'], 0)
        self.assertEqual(result['message'], 'new change')
        self.assertIn(self.release['sha'], request.call_args.args[0])


class ArchiveValidation(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.files = {'app.py': b'good'}
        self.hashes = {'app.py': update.blob_hash(b'good')}

    def extract(self, data):
        archive = self.root / 'source.zip'
        archive.write_bytes(data)
        update.extract_verified(archive, self.root / 'payload', self.hashes)

    def test_bad_hash_is_rejected(self):
        with self.assertRaisesRegex(ValueError, '検証に失敗'):
            self.extract(archive_of({'app.py': b'bad'}))

    def test_missing_file_is_rejected(self):
        with self.assertRaises(ValueError):
            self.extract(archive_of({}))

    def test_archive_cannot_escape_or_overwrite_personal_data(self):
        for malicious in ('../escape.py', 'bundle/../../escape.py', 'bundle/C:/escape.py',
                          'bundle/..\\escape.py', 'bundle/UserData/corrections.json',
                          'bundle/.venv/evil.py', 'bundle/correction_memory.json'):
            with self.subTest(path=malicious), self.assertRaises(ValueError):
                self.extract(archive_of(self.files, [(malicious, b'bad')]))
        self.assertFalse((self.root / 'escape.py').exists())

    def test_symlink_entry_is_rejected(self):
        info = zipfile.ZipInfo('bundle/app.py')
        info.create_system = 3
        info.external_attr = (0o120777 << 16)
        with self.assertRaisesRegex(ValueError, 'リンク'):
            self.extract(archive_of({}, [(info, b'good')]))

    def test_duplicate_entry_is_rejected(self):
        with self.assertRaises(ValueError):
            self.extract(archive_of(self.files, [('other-prefix/app.py', b'good')]))

    def test_windows_ambiguous_names_and_case_collisions_rejected(self):
        for name in ('../app.py', '/app.py', 'app.py.', 'app.py ', 'docs/aux.txt',
                     'docs/a:b.py', 'docs/x\\y.py', 'UserData/app.py', '.git/config', 'models/model.py'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                update.source_path(name)
        with self.assertRaises(ValueError):
            update.validate_files({'app.py': 'a' * 40, 'App.py': 'a' * 40})

    def test_size_limit_rejects_before_writing_file(self):
        with patch.object(update, 'MAX_FILE', 2), self.assertRaises(ValueError):
            self.extract(archive_of(self.files))
        self.assertFalse((self.root / 'payload/app.py').exists())

    def test_http_rate_limit_and_offline_have_actionable_errors(self):
        for error, message in ((urllib.error.HTTPError('url', 403, 'limited', {}, None), 'アクセス制限'),
                               (urllib.error.URLError('offline'), '接続できません')):
            with patch('urllib.request.urlopen', side_effect=error), self.assertRaisesRegex(OSError, message):
                update.get_url('https://api.github.com/', 1024)


if __name__ == '__main__':
    unittest.main()
