"""Portable contract tests for Mac adapters; hardware checks run in mac_smoke.py."""
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import platform_support as platform
import mac_audio


class PlatformRouting(unittest.TestCase):
    def test_mac_uses_separate_environment_and_never_windows_tool(self):
        with tempfile.TemporaryDirectory() as folder, patch.object(platform, 'IS_MAC', True):
            root = Path(folder)
            (root/'.tools').mkdir()
            (root/'.tools/ffmpeg.exe').touch()
            self.assertEqual(platform.environment_python(root), root/'.venv-macos/bin/python')
            with patch('shutil.which', return_value=None):
                self.assertNotEqual(platform.tool_path('ffmpeg', root), root/'.tools/ffmpeg.exe')

    def test_metal_then_cpu_and_explicit_cpu(self):
        cuda = Mock(return_value=True)
        with patch.object(platform, 'IS_MAC', True), patch('platform.machine', return_value='arm64'):
            self.assertEqual(platform.torch_backends('auto', cuda), ['mps', 'cpu'])
            self.assertEqual(platform.torch_backends('gpu', cuda), ['mps', 'cpu'])
            self.assertEqual(platform.torch_backends('cpu', cuda), ['cpu'])
        cuda.assert_not_called()
        with patch.object(platform, 'IS_MAC', False):
            self.assertEqual(platform.torch_backends('auto', cuda), ['cuda', 'cpu'])

    def test_mac_sources_can_update_but_native_data_cannot(self):
        from app_update import source_path
        for name in ('start.command', 'setup.command', 'setup_mac.py', 'requirements-macos.txt'):
            self.assertEqual(source_path(name), name)
        for name in ('.venv-macos/bin/python', '.setup-macos.json', 'UserData/settings.json'):
            with self.assertRaises(ValueError): source_path(name)

    def test_mac_does_not_start_copied_cuda_backends(self):
        import asr
        with patch.object(asr, 'IS_MAC', True):
            self.assertEqual(asr.native_backends('auto'), [])
            with self.assertRaises(ValueError): asr.native_backends('invalid')


class AudioAdapters(unittest.TestCase):
    def test_only_blackhole_is_exposed_never_a_microphone(self):
        info = [{'name':'MacBook Microphone', 'max_input_channels':1, 'default_samplerate':48000},
                {'name':'BlackHole 2ch', 'max_input_channels':2, 'default_samplerate':48000},
                {'name':'Speakers', 'max_input_channels':0, 'default_samplerate':48000}]
        with patch.dict(sys.modules, sounddevice=SimpleNamespace(query_devices=lambda: info)):
            self.assertEqual([r['name'] for r in mac_audio.devices()], ['BlackHole 2ch'])
            self.assertEqual(mac_audio.PyAudio().get_device_info_by_index(1)['maxInputChannels'], 2)
            with self.assertRaises(RuntimeError): mac_audio.PyAudio().get_device_info_by_index(0)

    def test_clock_pause_resume_and_tail_drain(self):
        stream = SimpleNamespace(time=0., start=Mock(), abort=Mock(), close=Mock())
        factory = Mock(return_value=stream)
        with patch.dict(sys.modules, sounddevice=SimpleNamespace(RawOutputStream=factory)):
            audio = mac_audio.WaveOutput()
        callback = factory.call_args.kwargs['callback']
        timing = SimpleNamespace(outputBufferDacTime=.02)
        block = bytearray(480*4)
        pcm = b'\x01\x00\x01\x00'*480
        audio.write(pcm)
        callback(block, 480, timing, None)
        self.assertEqual(block, bytes(len(block)))  # initially paused
        self.assertEqual(len(audio.pending), 1)
        audio.resume()
        callback(block, 480, timing, None)
        self.assertEqual(block, pcm)
        self.assertTrue(audio.pending)  # queued to hardware, not yet audible
        stream.time=.025
        self.assertAlmostEqual(audio.position(), .005)
        stream.time=.031
        self.assertAlmostEqual(audio.position(), .01)
        self.assertFalse(audio.pending)
        audio.pause()
        audio.write(pcm)
        callback(block, 480, SimpleNamespace(outputBufferDacTime=.04), None)
        self.assertEqual(block, bytes(len(block)))
        self.assertEqual(len(audio.pending), 1)
        audio.close()
        audio.close()
        stream.close.assert_called_once()


class CrossOSData(unittest.TestCase):
    def test_windows_and_mac_paths_rebase_without_changing_speech_or_outside_paths(self):
        import user_data as ud
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for old in (r'C:\Users\User\whisper-local\UserData', '/Users/person/whisper-local/UserData'):
                old = ud.stored_path(old)
                value = dict(source=str(old/'speaker_sources'/'a.wav'), text=str(old/'speaker_sources'/'a.wav'),
                             audio_file=str(old/'..'/'outside.wav'))
                result = ud._relocate(value, old, root, root, require_exists=False)
                self.assertEqual(result['source'], str(root/'speaker_sources/a.wav'))
                self.assertEqual(result['text'], value['text'])
                self.assertEqual(result['audio_file'], value['audio_file'])

    def test_foreign_speaker_sources_validate_then_relocate_on_copy(self):
        import json
        import wave
        import user_data as ud
        from data_backup import _validate
        for old in (r'C:\Users\old\UserData', '/Users/old/UserData'):
            with self.subTest(old=old), tempfile.TemporaryDirectory() as folder:
                root = Path(folder)
                original = ud.stored_path(old)
                audio = root/'speaker_sources/short.wav'
                audio.parent.mkdir()
                with wave.open(str(audio), 'wb') as f:
                    f.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
                    f.writeframes(b'\x01\x10' * 16000)
                content = audio.read_bytes()
                memory = dict(version=1, profiles={'voice_1': dict(name='田中', pending_only=True,
                              clips={'clip1': dict(source=str(original/'speaker_sources/short.wav'), origin=str(original/'transcripts/one/result.json'), start=0., end=1.)})})
                (root/'speaker_memory.json').write_text(json.dumps(memory), 'utf-8')
                _validate(root, original)
                (root/ud.MARKER).write_text(json.dumps({'storage_root': str(original), 'storage_device': 'another-pc'}), 'utf-8')
                ud._prepare_location(root)
                restored = json.loads((root/'speaker_memory.json').read_text('utf-8'))['profiles']['voice_1']
                self.assertEqual(restored['name'], '田中')
                self.assertEqual(restored['clips']['clip1']['source'], str(audio))
                self.assertEqual(audio.read_bytes(), content)
                _validate(root, root)


if __name__ == '__main__': unittest.main()
