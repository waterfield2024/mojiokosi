import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
import wave

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
DATA = tempfile.TemporaryDirectory(prefix='whisper-speech-tests-')
for key in list(os.environ):
    if key.upper().startswith('WHISPER_'):
        del os.environ[key]
os.environ['WHISPER_USER_DATA_DIR'] = str(Path(DATA.name) / 'data')
os.environ['WHISPER_LEGACY_DATA_DIR'] = str(Path(DATA.name) / 'empty')

import speech_gate as sg


def tone(seconds=1, volume=.02):
    samples = np.sin(np.arange(round(seconds*16000))*2*np.pi*220/16000)*volume
    return np.rint(samples*32768).astype('<i2').tobytes()


class SpeechEvidence(unittest.TestCase):
    def test_digital_silence_dc_and_rounding_residue_are_not_speech(self):
        gate = sg.SpeechGate(DATA.name)
        gate.detector = Mock()
        for pcm in (b'', b'\0\0'*32000, b'\x01\x00'*32000, b'\xff\x3f'*32000,
                    np.tile(np.array([-2, 2], '<i2'), 16000).tobytes()):
            self.assertEqual(gate.intervals(pcm), [])
        gate.detector.accept_waveform.assert_not_called()

    def test_quiet_short_signal_is_not_removed_by_level_check(self):
        # Two-hundred-millisecond reply at a level below the capture threshold.
        samples = np.frombuffer(tone(.2, .002), '<i2').astype('float32')/32768
        self.assertTrue(sg.audible(np.pad(samples, (16000, 16000))))

    def test_neural_state_resets_and_partial_tail_keeps_original_sample_count(self):
        gate = sg.SpeechGate(DATA.name)
        detector = Mock()
        gate.detector = detector
        detector.front = Mock(start=0, samples=np.zeros(512))
        detector.empty.side_effect = [False, True, True]
        audio = tone(.02)+tone(.02)+tone(.001)
        self.assertEqual(gate.intervals(audio), [(0, 512)])
        self.assertEqual(gate.intervals(audio), [])
        self.assertEqual(detector.reset.call_count, 2)
        self.assertEqual(detector.flush.call_count, 2)
        self.assertTrue(all(len(call.args[0]) == 512 for call in detector.accept_waveform.call_args_list))

    def test_detector_failure_preserves_possible_speech_and_reports_fallback(self):
        gate = sg.SpeechGate(DATA.name)
        gate.detector = Mock()
        gate.detector.reset.side_effect = RuntimeError('fixture error')
        pcm = tone()
        self.assertEqual(gate.intervals(pcm), [(0, 16000)])
        self.assertIn('基本の無音判定', gate.warning)
        self.assertEqual(gate.intervals(b'\0\0'*16000), [])

    def test_quiet_gain_affects_analysis_copy_only(self):
        gate = sg.SpeechGate(DATA.name)
        gate.detector = Mock()
        gate.detector.empty.return_value = True
        pcm = tone(.2, .004)
        original = bytes(pcm)
        gate.intervals(pcm)
        analyzed = np.concatenate([call.args[0] for call in gate.detector.accept_waveform.call_args_list])
        self.assertGreater(float(abs(analyzed).max()), .07)
        self.assertEqual(pcm, original)

    def test_prepare_failure_does_not_repeat_network_for_every_chunk(self):
        gate = sg.SpeechGate(DATA.name)
        with patch.object(sg, 'ensure_vad', side_effect=OSError('offline')) as fetch:
            self.assertIn('基本の無音判定', gate.prepare())
            gate.prepare()
            fetch.assert_called_once()
        self.assertEqual(gate.intervals(tone()), [(0, 16000)])

    def test_prior_overlap_does_not_authorize_new_silent_audio(self):
        self.assertFalse(sg.overlaps_speech([(0, 12000)], 16000, 32000))
        self.assertTrue(sg.overlaps_speech([(18000, 21000)], 16000, 32000))

    def test_real_n_is_kept_but_silent_tail_caption_is_removed(self):
        spoken = {'start': .2, 'end': .4, 'text': 'ん'}
        invented = {'start': 2., 'end': 3., 'text': 'ん'}
        self.assertEqual(sg.keep_spoken_rows([spoken, invented], [(3000, 7000)]), [spoken])

    def test_silent_speaker_part_never_calls_asr(self):
        import realtime
        recognizer = Mock()
        parts = [{'start': 0., 'end': 1., 'speaker': 'a'}, {'start': 1., 'end': 2., 'speaker': 'b'}]
        recognizer.transcribe.return_value = {'segments': [{'start': .1, 'end': .3, 'text': 'ん'}]}
        rows = list(realtime.recognize_parts(recognizer, tone(2), 0, 32000, 0, [], parts,
                                            speech=[(17600, 20800)]))
        recognizer.transcribe.assert_called_once()
        self.assertEqual(rows[0][0][0]['text'], 'ん')
        self.assertEqual(rows[0][0][0]['start'], 1.1)
        self.assertEqual(rows[0][0][0]['speaker'], 'b')


class SpeechDownload(unittest.TestCase):
    def test_bad_download_preserves_old_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/sg.MODEL_PATH
            path.parent.mkdir();path.write_bytes(b'old')
            with patch.object(sg.urllib.request, 'urlopen', return_value=io.BytesIO(b'bad')):
                with self.assertRaisesRegex(RuntimeError, '検証'):
                    sg.ensure_vad(folder)
            self.assertEqual(path.read_bytes(), b'old')
            self.assertEqual(list(path.parent.iterdir()), [path])

    def test_verified_weight_is_reused_offline(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/sg.MODEL_PATH
            path.parent.mkdir();path.write_bytes(b'valid')
            with patch.object(sg, 'MODEL_SIZE', 5), patch.object(sg, 'MODEL_SHA256', hashlib.sha256(b'valid').hexdigest()), \
                 patch.object(sg.urllib.request, 'urlopen') as fetch:
                self.assertEqual(sg.ensure_vad(folder), path)
                fetch.assert_not_called()


class LiveSilencePipeline(unittest.TestCase):
    def test_filtered_chunks_keep_recording_and_timing_with_noise_on_or_off(self):
        import engine
        import realtime
        for noise_on in (False, True):
            with self.subTest(noise_on=noise_on), tempfile.TemporaryDirectory() as temp:
                folder = Path(temp)
                # Three source chunks are all non-silent; the middle is speech.
                source = tone(1, .01) + tone(1, .04) + tone(1, .01)
                events, calls = [], []
                class Capture:
                    def set_sensitivity(self, *args): pass
                    def set_segmentation(self, value, **kwargs): pass
                    def __init__(self, device, wav, chunks, stopped, seconds, meter):
                        self.total, self.error = 48000, None
                        self.ready, self.done = threading.Event(), threading.Event()
                        self.thread = Mock()
                        with wave.open(str(wav), 'wb') as out:
                            out.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
                            out.writeframes(source)
                        self.ready.set();self.done.set()
                    def start(self): pass
                    def stop(self): pass
                def transcribe(pcm):
                    calls.append(pcm)
                    return {'segments': [{'start': .1, 'end': .5, 'text': 'ん'}]}
                recognizer = Mock(device='CPU', backend='fixture', failures=[], transcribe=Mock(side_effect=transcribe))
                gate = Mock(warning='', intervals=Mock(side_effect=[[], [(1600, 8000)], []]))
                # Simulate Nemotron making noise quiet; the gate receives the ASR copy.
                processed = [b'\1\0'*16000, source[32000:64000], b'\1\0'*16000] if noise_on else [source[i:i+32000] for i in (0,32000,64000)]
                reducer = Mock()
                reducer.snapshot.return_value = {'enabled':noise_on, 'state':'ready', 'text':''}
                reducer.process.side_effect = [(pcm, noise_on) for pcm in processed]
                request = {'model':'haas-ja-v1.0', 'device':{'name':'fixture'}, 'language':'ja', 'live_noise':noise_on,
                           'live_diarize':False, 'diarize':False, 'context_correct':False}
                with patch.object(engine,'RESULTS',folder/'results'), patch.object(realtime,'LoopbackCapture',Capture), \
                     patch.object(realtime,'SpeechGate',return_value=gate), \
                     patch('live_noise.LiveNoiseReducer',return_value=reducer), \
                     patch.object(realtime,'live_recognizer_type',return_value=Mock(return_value=recognizer)), \
                     patch.object(realtime,'next_live_chunk',side_effect=[(0,16000,0),(16000,32000,16000),(32000,48000,32000),None]), \
                     patch.object(engine,'cache_audio',side_effect=lambda wav,_:str(wav)), \
                     patch.object(engine,'emit',side_effect=lambda kind,**kw:events.append({'kind':kind,**kw})):
                    realtime.run_live(request,folder)
                result=json.loads(Path(next(e['result_path'] for e in events if e['kind']=='complete')).read_text('utf-8'))
                self.assertFalse(result['live_incomplete'],result.get('live_error'))
                self.assertEqual(result['speech_filter']['skipped_chunks'],2)
                self.assertEqual(result['duration'],3.)
                self.assertEqual([row['text'] for row in result['segments']],['ん'])
                self.assertEqual(result['segments'][0]['start'],1.1)
                self.assertEqual(calls,[processed[1]])
                self.assertEqual([c.args[0] for c in gate.intervals.call_args_list],processed)
                with wave.open(result['audio_file'],'rb') as audio:
                    self.assertEqual(audio.readframes(audio.getnframes()),source)


if __name__ == '__main__':
    unittest.main()
