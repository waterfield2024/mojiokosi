import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

from comparison_merge import MergePlan, save_merged
from data_backup import FORMAT, MANIFEST, inspect_backup, restore_backup
from result_media import result_file
from screen_capture import recorded_video
from speaker_memory_ui import SpeakerMemoryControls


class ImportedResults(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name)
        self.folder = self.base / 'received'
        self.folder.mkdir()

    def test_only_local_mp4_is_a_recording(self):
        for name in ('click.cmd', 'click.lnk', 'video.mp4.exe'):
            (self.folder / name).touch()
            self.assertIsNone(recorded_video({'screen_recording': {'complete': True, 'file': name}}, self.folder))
        outside = self.base / 'outside.mp4'
        outside.touch()
        self.assertIsNone(recorded_video({'screen_recording': {'complete': True}, 'video_source': str(outside)}, self.folder))
        path = self.folder / '.recording' / '録画.mp4'
        path.parent.mkdir()
        path.touch()
        self.assertEqual(recorded_video({'screen_recording': {'complete': True, 'file': '.recording/録画.mp4'}}, self.folder), path)

    def test_reject_network_and_traversal_before_io(self):
        for value in (r'\\example.invalid\share\a.mp4', '//example.invalid/share/a.mp4',
                      r'\\?\UNC\example.invalid\share\a.mp4', '../elsewhere/a.mp4', 'bad:stream.mp4'):
            with self.subTest(value=value), patch.object(Path, 'lstat', side_effect=AssertionError('unexpected stat')):
                self.assertIsNone(result_file(self.folder, value, {'.mp4'}))

    def test_comparison_merge_does_not_import_arbitrary_file(self):
        private = self.base / 'private.wav'
        private.write_bytes(b'private fixture')
        path = self.folder / 'comparison.json'
        sides = ('whisper', 'granite')
        path.write_text(json.dumps({'kind': 'model_comparison', 'version': 1,
                                   'results': {s: {'state': 'complete'} for s in sides}}), 'utf8')
        results = {}
        for side in sides:
            folder = self.folder / side
            folder.mkdir()
            results[side] = {'audio_file': str(private), 'segments': [{'start': 0, 'end': 1, 'text': 'test'}]}
            (folder / 'result.json').write_text(json.dumps(results[side]), 'utf8')
        plan = MergePlan(results)
        merged = save_merged(path, plan, [])
        self.assertFalse((merged.parent / '.audio').exists())
        cache = self.folder / 'whisper' / '.audio' / 'source.flac'
        cache.parent.mkdir()
        cache.write_bytes(b'local recording')
        merged = save_merged(path, plan, [])
        copy = merged.parent / '.audio' / 'source.flac'
        self.assertEqual(copy.read_bytes(), cache.read_bytes())
        self.assertFalse(copy.samefile(cache))

    def test_imported_assignment_is_display_only_without_local_enrollment(self):
        control = SpeakerMemoryControls()
        control.speaker_memory = Mock()
        control.speaker_memory.load.return_value = {}
        row = {'speaker': 's1', 'speaker_manual': True, 'start': 0, 'end': 4,
               'voice_learning_pending': True, 'voice_correction_key': 'planted'}
        control.validate_saved_learning([row], self.folder)
        self.assertTrue(row['speaker_manual'])
        self.assertNotIn('voice_learning_pending', row)
        self.assertNotIn('voice_correction_key', row)
        control.speaker_memory.stage_clips.assert_not_called()

    def test_manual_correction_enrolls_only_the_selected_new_clip(self):
        from speaker_memory import MemoryStore
        audio = self.folder / '.audio' / 'recording.wav'
        audio.parent.mkdir()
        audio.touch()
        control = SpeakerMemoryControls()
        control.speaker_memory = MemoryStore(self.base / 'data' / 'speaker_memory.json')
        control.result_path = self.folder / 'result.json'
        control.result_data = {'title': 'fixture'}
        control.speakers = {'s1': '田中'}
        control.memory_status = Mock()
        control.queue_voice_pool = Mock()
        control.refresh_speaker_rows = Mock()
        control.persist_edits = Mock()
        selected = {'speaker': 's1', 'start': 0., 'end': 1., 'speaker_manual': True}
        imported = dict(selected, start=2., end=3., voice_correction_key='forged', voice_learning_pending=True)
        control.segments = [selected, imported]
        control.validate_saved_learning(control.segments, self.folder)
        control.queue_corrected_speaker(selected)
        profile, = control.speaker_memory.load().values()
        self.assertEqual(set(profile['clips']), {selected['voice_correction_key']})
        self.assertEqual(profile['name'], '田中')
        control.queue_voice_pool.assert_called_once()

    def archive(self, name, data, declared_hash=None):
        path = self.base / 'fixture.zip'
        manifest = {'format': FORMAT, 'version': 1, 'created': '', 'device': 'test',
                    'source_root': str(self.base), 'include_results': True,
                    'summary': dict.fromkeys(('speakers', 'learning_clips', 'dictionary_rules', 'settings', 'results'), 0),
                    'files': {name: {'bytes': len(data), 'sha256': declared_hash or hashlib.sha256(data).hexdigest()}}}
        with zipfile.ZipFile(path, 'w', zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(MANIFEST, json.dumps(manifest))
            archive.writestr('data/' + name, data)
        return path

    def test_preview_only_reads_inventory_and_blocks_compression_bomb(self):
        path = self.archive('transcripts/session/result.json', b'{}')
        real_open = zipfile.ZipFile.open
        def inventory_only(archive, member, *args, **kwargs):
            self.assertEqual(member, MANIFEST)
            return real_open(archive, member, *args, **kwargs)
        with patch.object(zipfile.ZipFile, 'open', inventory_only):
            self.assertEqual(inspect_backup(path)['expanded_bytes'], 2)
        path = self.archive('transcripts/session/padding', b'0' * (4 * 1024**2))
        with self.assertRaises(ValueError):
            inspect_backup(path)

    def test_bad_payload_fails_before_replacing_store(self):
        path = self.archive('transcripts/test/result.json', b'{}', '0' * 64)
        root = self.base / 'UserData'
        root.mkdir()
        sentinel = root / 'original.txt'
        sentinel.write_bytes(b'original')
        with self.assertRaises(ValueError):
            restore_backup(path, expected_fingerprint=inspect_backup(path)['fingerprint'], root=root)
        self.assertEqual(sentinel.read_bytes(), b'original')

    def test_backup_metadata_never_stats_foreign_share(self):
        network = r'\\audit.invalid\share\clip.wav'
        data = {'version': 1, 'profiles': {}, 'extra': {'clips': {'injected': {'file': network, 'source': ''}}}}
        path = self.archive('speaker_memory.json', json.dumps(data).encode())
        real_stat = Path.stat
        def local_stat(p, *args, **kwargs):
            if str(p).startswith(('\\\\', '//')):
                self.fail('network path accessed')
            return real_stat(p, *args, **kwargs)
        with patch.object(Path, 'stat', local_stat):
            restore_backup(path, expected_fingerprint=inspect_backup(path)['fingerprint'], root=self.base / 'new')
