import json
import os
from pathlib import Path
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import wave

import numpy as np

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
DATA=tempfile.TemporaryDirectory(prefix='whisper-sensitivity-tests-')
for key in list(os.environ):
    if key.upper().startswith('WHISPER_'):del os.environ[key]
os.environ['WHISPER_USER_DATA_DIR']=str(Path(DATA.name)/'data')
os.environ['WHISPER_LEGACY_DATA_DIR']=str(Path(DATA.name)/'empty')
from speech_preferences import profile,read_control,PROFILES
from speech_gate import SpeechGate,audible,overlaps_speech
from realtime_audio import Chunker,LoopbackCapture


def sound(seconds,peak=.0025):
    return np.rint(np.sin(np.arange(round(seconds*16000))*2*np.pi*220/16000)*peak*32768).astype('<i2').tobytes()


class SensitivitySettings(unittest.TestCase):
    def test_default_and_old_settings_remain_balanced(self):
        from recording_settings import processing_preferences
        self.assertEqual(processing_preferences({})['speech_sensitivity'],'balanced')
        self.assertEqual(processing_preferences({'chunk_seconds':5})['speech_sensitivity'],'balanced')

    def test_save_restore_and_backup_keep_selection(self):
        from recording_settings import save_settings,load_settings,settings_for
        from data_backup import _validate
        with tempfile.TemporaryDirectory() as folder:
            folder=Path(folder)
            with patch('recording_settings.settings_path',return_value=folder/'recording_settings.json'):
                save_settings(settings_for('audio',{},processing={'speech_sensitivity':'sensitive'}))
                self.assertEqual(load_settings()['processing']['speech_sensitivity'],'sensitive')
                self.assertEqual(_validate(folder,folder)['settings'],1)

    def test_invalid_values_rejected_and_bad_controls_keep_current(self):
        from recording_settings import processing_preferences
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'speech-control.json'
            for value in (None,True,3,[],{},'unknown'):
                with self.assertRaises(ValueError):processing_preferences({'speech_sensitivity':value})
                path.write_text(json.dumps({'sensitivity':value}),'utf-8')
                self.assertEqual(read_control(path,'sensitive'),'sensitive')
            path.write_text('{','utf-8')
            self.assertEqual(read_control(path,'strict'),'strict')
            path.write_text('{"sensitivity":"balanced"}','utf-8')
            self.assertEqual(read_control(path,'strict'),'balanced')


class CaptureSensitivity(unittest.TestCase):
    def collect(self,pcm,setting,finish=True):
        chunks=[];c=Chunker(lambda *args:chunks.append(args),sensitivity=setting)
        for offset in range(0,len(pcm),3200):c.feed(pcm[offset:offset+3200])
        if finish:c.finish()
        return chunks,c

    def test_quiet_and_short_replies_enter_asr_queue_only_at_high_sensitivity(self):
        for voice in (sound(.1,.04),sound(.3,.0025)):
            pcm=bytes(16000)+voice+bytes(16000)
            self.assertEqual(self.collect(pcm,'balanced')[0],[])
            chunks,c=self.collect(pcm,'sensitive')
            self.assertEqual(len(chunks),1)
            self.assertLessEqual(chunks[0][0],8000)
            self.assertGreaterEqual(chunks[0][1],8000+len(voice)//2)
            self.assertEqual(c.total,len(pcm)//2)

    def test_stop_preserves_short_tail_in_sensitive_mode(self):
        chunks,c=self.collect(sound(.06),'sensitive')
        self.assertEqual(chunks,[(0,960,0)])
        self.assertEqual(c.total,960)

    def test_capture_applies_live_control_without_waiting_for_asr(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'speech-control.json'
            capture=LoopbackCapture({},None,None,lambda:False)
            capture.set_sensitivity('balanced',path)
            chunks=[];c=Chunker(lambda *args:chunks.append(args))
            c.feed(bytes(16000))
            previous=c.total
            path.write_text('{"sensitivity":"sensitive"}','utf-8')
            capture.update_chunker(c)
            self.assertEqual(c.profile.key,'sensitive')
            self.assertEqual(c.total,previous)
            c.feed(sound(.1));c.feed(bytes(16000));c.finish()
            self.assertEqual(len(chunks),1)

    def test_all_levels_preserve_silent_timeline_without_submitting_it(self):
        for setting in PROFILES:
            chunks,c=self.collect(bytes(64000),setting)
            self.assertEqual(chunks,[])
            self.assertEqual(c.total,32000)


class DetectorSensitivity(unittest.TestCase):
    def test_change_updates_vad_and_same_selection_reuses_detector(self):
        configs=[]
        def config():
            return SimpleNamespace(silero_vad=SimpleNamespace())
        def create(value,**kwargs):
            configs.append(value)
            return Mock()
        module=SimpleNamespace(VadModelConfig=config,VoiceActivityDetector=create)
        gate=SpeechGate(DATA.name)
        with patch('speech_gate.ensure_vad',return_value=Path('fixture.onnx')),patch.dict(sys.modules,{'sherpa_onnx':module}):
            gate.prepare();first=gate.detector
            gate.configure('balanced');gate.prepare()
            self.assertIs(gate.detector,first)
            gate.configure('sensitive');gate.prepare()
            self.assertIsNot(gate.detector,first)
            self.assertEqual(configs[-1].silero_vad.threshold,.2)
            self.assertEqual(configs[-1].silero_vad.min_speech_duration,.064)
            gate.configure('strict');gate.prepare()
            self.assertEqual(configs[-1].silero_vad.threshold,.55)
            self.assertEqual(len(configs),3)

    def test_quiet_floor_and_short_overlap_are_more_permissive(self):
        samples=np.frombuffer(sound(.02,.0002),'<i2').astype('float32')/32768
        self.assertFalse(audible(samples,'balanced'))
        self.assertTrue(audible(samples,'sensitive'))
        self.assertFalse(overlaps_speech([(100,500)],0,16000,'balanced'))
        self.assertTrue(overlaps_speech([(100,500)],0,16000,'sensitive'))
        for setting in PROFILES:self.assertFalse(audible(np.zeros(16000),setting))


class LiveSensitivity(unittest.TestCase):
    def test_change_reaches_capture_and_filter_without_restart_or_timestamp_shift(self):
        import engine,realtime
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp);source=sound(2);events=[];capture_settings=[]
            class Capture:
                def __init__(self,device,wav,chunks,stopped,seconds,meter):
                    self.total,self.error=32000,None
                    self.ready,self.done=threading.Event(),threading.Event();self.thread=Mock()
                    with wave.open(str(wav),'wb') as audio:
                        audio.setparams((1,2,16000,0,'NONE','not compressed'));audio.writeframes(source)
                    self.ready.set();self.done.set()
                def start(self):pass
                def stop(self):pass
                def set_sensitivity(self,value,*args):capture_settings.append(value)
                def set_segmentation(self, value, **kwargs): pass
            class Gate(SpeechGate):
                def prepare(self,status=lambda _:None,stopped=lambda:False):
                    if stopped():raise RuntimeError('Normal stop must still drain audio')
                def intervals(self,pcm):
                    return [] if self.profile.key=='strict' else [(0,len(pcm)//2)]
            gate=Gate(folder)
            def chunk(*args):
                count=chunk.calls;chunk.calls+=1
                if count==0:return (0,16000,0)
                if count==1:
                    (folder/'speech-control.json').write_text('{"sensitivity":"sensitive"}','utf-8')
                    # Applied on the next service tick; first allow an idle tick.
                    import queue
                    raise queue.Empty
                if count==2:
                    (folder/'stop-live').touch()
                    return (16000,32000,16000)
                return None
            chunk.calls=0
            recognizer=Mock(device='CPU',backend='fixture',failures=[])
            recognizer.transcribe.return_value={'segments':[{'start':.1,'end':.4,'text':'はい'}]}
            request={'model':'haas-ja-v1.0','device':{'name':'fixture'},'speech_sensitivity':'strict',
                     'live_diarize':False,'diarize':False,'context_correct':False}
            with patch.object(engine,'RESULTS',folder/'results'),patch.object(realtime,'LoopbackCapture',Capture), \
                 patch.object(realtime,'SpeechGate',return_value=gate),patch.object(realtime,'next_live_chunk',side_effect=chunk), \
                 patch.object(realtime,'live_recognizer_type',return_value=Mock(return_value=recognizer)) as factory, \
                 patch.object(engine,'cache_audio',side_effect=lambda wav,_:str(wav)), \
                 patch.object(engine,'emit',side_effect=lambda kind,**kw:events.append({'kind':kind,**kw})):
                realtime.run_live(request,folder)
            result=json.loads(Path(next(e['result_path'] for e in events if e['kind']=='complete')).read_text('utf-8'))
            self.assertFalse(result['live_incomplete'],result.get('live_error'))
            self.assertEqual(capture_settings,['strict','sensitive'])
            self.assertEqual(result['speech_filter']['sensitivity'],'sensitive')
            self.assertEqual(result['speech_filter']['skipped_chunks'],1)
            self.assertEqual(result['segments'][0]['start'],1.1)
            self.assertEqual(result['duration'],2.)
            factory.assert_called_once();recognizer.transcribe.assert_called_once()
            with wave.open(result['audio_file'],'rb') as audio:self.assertEqual(audio.readframes(audio.getnframes()),source)


if __name__=='__main__':unittest.main()
