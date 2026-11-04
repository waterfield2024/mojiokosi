import json
import os
from pathlib import Path
import queue
import sys
import tempfile
import threading
from types import SimpleNamespace
import unittest
from collections import deque
from unittest.mock import Mock,patch
import wave

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
DATA=tempfile.TemporaryDirectory(prefix='whisper-utterance-tests-')
for key in list(os.environ):
    if key.upper().startswith('WHISPER_'):del os.environ[key]
os.environ.update(WHISPER_USER_DATA_DIR=str(Path(DATA.name)/'data'),WHISPER_LEGACY_DATA_DIR=str(Path(DATA.name)/'empty'))
from utterance_audio import AudioChunk,UtteranceChunker,RATE
from realtime import next_live_chunk


def voice(seconds):return b'\x00\x10'*round(seconds*RATE)
def silence(seconds):return bytes(round(seconds*RATE)*2)


class UtteranceBoundaries(unittest.TestCase):
    def test_short_breath_does_not_finalize_but_pause_does(self):
        items=[];chunker=UtteranceChunker(items.append)
        chunker.feed(voice(2.5)+silence(.4)+voice(2.5))
        self.assertTrue(items and all(not c.final for c in items))
        self.assertEqual(len({c.utterance for c in items}),1)
        chunker.feed(silence(.7))
        final=[c for c in items if c.final]
        self.assertEqual(len(final),1)
        self.assertEqual((final[0].start,final[0].end),(0,round(6.1*RATE)))
        self.assertEqual({c.start for c in items},{0})

    def test_maximum_is_bounded_and_overlap_does_not_drop_new_audio(self):
        items=[];c=UtteranceChunker(items.append)
        c.feed(voice(25));c.finish()
        finals=[x for x in items if x.final]
        self.assertEqual(len(finals),3)
        self.assertTrue(all(x.end-x.start<=12*RATE for x in items))
        self.assertEqual([(x.core,x.end) for x in finals],[(0,192000),(192000,368000),(368000,400000)])
        self.assertEqual(finals[1].start,finals[0].end-RATE)

    def test_stop_finalizes_even_immediately_after_preview_once(self):
        items=[];c=UtteranceChunker(items.append)
        c.feed(voice(2));self.assertFalse(items[-1].final)
        c.finish();c.finish()
        self.assertEqual(len(items),2)
        self.assertTrue(items[-1].final)
        self.assertEqual(items[-1].end,items[0].end)

    def test_short_quiet_reply_and_stop_tail_follow_sensitivity(self):
        for duration,finish in ((.08,True),(.1,False)):
            items=[];c=UtteranceChunker(items.append,sensitivity='sensitive')
            c.feed(b'\x40\0'*round(duration*RATE))
            if not finish:c.feed(silence(.7))
            c.finish()
            self.assertEqual(len(items),1)
            self.assertTrue(items[0].final)

    def test_silence_keeps_time_and_two_phrases_have_distinct_ids(self):
        items=[];c=UtteranceChunker(items.append)
        c.feed(silence(5));self.assertEqual(items,[])
        c.feed(voice(.5)+silence(1)+voice(.5)+silence(1));c.finish()
        self.assertEqual(len(items),2)
        self.assertEqual([x.utterance for x in items],[0,1])
        self.assertTrue(all(x.final for x in items))
        self.assertEqual(c.total,8*RATE)

    def test_detected_speech_can_end_while_background_sound_continues(self):
        activity=Mock(feed=Mock(side_effect=[True]*100+[False]*100))
        items=[];c=UtteranceChunker(items.append,activity=activity)
        c.feed(voice(4))
        final=next(x for x in items if x.final)
        self.assertEqual(final.end,round(2.7*RATE))
        # An unavailable detector still permits an energy-based short reply.
        items=[];c=UtteranceChunker(items.append,activity=Mock(feed=Mock(return_value=None)),sensitivity='sensitive')
        c.feed(voice(.1)+silence(.7));self.assertTrue(items[-1].final)

    def test_backlog_keeps_finals_and_only_latest_draft(self):
        q=queue.Queue();pending=deque()
        items=[AudioChunk(0,2*RATE,0,0,False),AudioChunk(0,4*RATE,0,0,False),AudioChunk(0,5*RATE,0,0,True),
               AudioChunk(6*RATE,8*RATE,5*RATE,1,False),AudioChunk(6*RATE,10*RATE,5*RATE,1,False),None]
        for x in items:q.put(x)
        self.assertEqual(next_live_chunk(q,pending,8*RATE),items[2])
        self.assertEqual(next_live_chunk(q,pending,8*RATE),items[4])
        self.assertIsNone(next_live_chunk(q,pending,8*RATE))

    def test_settings_default_and_legacy_choice_saved_and_backed_up(self):
        from recording_settings import processing_preferences,save_settings,load_settings,settings_for
        from data_backup import _validate
        self.assertEqual(processing_preferences({'chunk_seconds':5})['segmentation'],'utterance')
        for value in (False,None,[],{},'unknown'):
            with self.assertRaises(ValueError):processing_preferences({'segmentation':value})
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp)
            with patch('recording_settings.settings_path',return_value=folder/'recording_settings.json'):
                save_settings(settings_for('audio',{},processing={'segmentation':'timed','chunk_seconds':3}))
                saved=load_settings()['processing']
                self.assertEqual((saved['segmentation'],saved['chunk_seconds']),('timed',3))
                self.assertEqual(_validate(folder,folder)['settings'],1)


class SpeakerPreview(unittest.TestCase):
    def test_preview_rolls_back_tracker_and_voice_samples_even_on_failure(self):
        from live_speakers import LiveDiarizer
        worker=LiveDiarizer.__new__(LiveDiarizer)
        worker.tracker={'speaker_0':1};worker.samples={'speaker_0':{'seconds':3}}
        original_tracker,original_samples=worker.tracker,worker.samples
        def process(pcm,offset):
            worker.tracker['speaker_0']=10;worker.samples['speaker_0']['seconds']=8
            return ['turn']
        worker.process=process
        turns,profiles=worker.preview(b'',0)
        self.assertEqual((turns,profiles['speaker_0']['seconds']),(['turn'],8))
        self.assertIs(worker.tracker,original_tracker);self.assertIs(worker.samples,original_samples)
        self.assertEqual(worker.samples['speaker_0']['seconds'],3)
        worker.process=Mock(side_effect=RuntimeError('test'))
        with self.assertRaises(RuntimeError):worker.preview(b'',0)
        self.assertIs(worker.tracker,original_tracker)


class CaptureFinalization(unittest.TestCase):
    def test_capture_finalizes_stop_and_device_failure_in_selected_mode(self):
        import numpy as np
        from realtime_audio import LoopbackCapture
        for mode,disconnect in (('utterance',False),('utterance',True),('timed',False)):
            with self.subTest(mode=mode,disconnect=disconnect),tempfile.TemporaryDirectory() as tmp:
                output=Path(tmp)/'source.wav';chunks=queue.Queue();stopped=[False]
                class Driver:
                    def get_device_info_by_index(self,index):
                        return dict(index=0,isLoopbackDevice=True,name='fixture',defaultSampleRate=16000,maxInputChannels=1)
                    def open(self,**kw):
                        self.callback=kw['stream_callback'];return self
                    def start_stream(self):
                        self.callback(np.full(3200,.1,dtype='<f4').tobytes(),3200,None,0)
                        stopped[0]=not disconnect
                    def stop_stream(self):pass
                    def is_active(self):return not disconnect
                    def close(self):pass
                    def terminate(self):pass
                module=SimpleNamespace(PyAudio=Driver,paInputOverflow=1,paComplete=1,paContinue=0,paFloat32=1)
                capture=LoopbackCapture({'index':0,'name':'fixture'},output,chunks,lambda:stopped[0])
                capture.set_segmentation(mode)
                with patch.dict(sys.modules,{'pyaudiowpatch':module}),patch('speech_gate.SpeechActivity',return_value=Mock(warning='',feed=Mock(return_value=None))):
                    capture.run()
                self.assertTrue(capture.done.is_set() and capture.ready.is_set(),capture.error)
                chunk=chunks.get_nowait()
                if mode=='utterance':self.assertTrue(isinstance(chunk,AudioChunk) and chunk.final)
                else:self.assertIsInstance(chunk,tuple)
                self.assertIsNone(chunks.get_nowait())
                self.assertEqual(bool(capture.error),disconnect)
                with wave.open(str(output),'rb') as f:self.assertEqual(f.getnframes(),capture.total)


class UtterancePipeline(unittest.TestCase):
    def test_stop_during_capture_preparation_is_not_reported_as_device_error(self):
        import engine,realtime
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp);events=[]
            class Capture:
                def __init__(self,*args):
                    self.total,self.error=0,None
                    self.ready,self.done=threading.Event(),threading.Event();self.thread=Mock()
                def start(self):
                    (folder/'stop-live').touch();self.done.set()
                def stop(self):pass
                def set_sensitivity(self,*args):pass
                def set_segmentation(self,*args,**kwargs):pass
            recognizer=Mock(device='CPU')
            request=dict(model='base',device={'name':'fixture'},live_diarize=False,diarize=False,context_correct=False)
            with patch.object(engine,'RESULTS',folder/'results'),patch.object(realtime,'LoopbackCapture',Capture), \
                 patch.object(realtime,'SpeechGate',return_value=Mock()), \
                 patch.object(realtime,'live_recognizer_type',return_value=Mock(return_value=recognizer)), \
                 patch.object(engine,'emit',side_effect=lambda kind,**kw:events.append(dict(kind=kind,**kw))):
                realtime.run_live(request,folder)
            result=json.loads(Path(next(e['result_path'] for e in events if e['kind']=='complete')).read_text('utf-8'))
            self.assertFalse(result['recording_complete'])
            self.assertFalse(result['live_incomplete'])
            self.assertFalse(any(e['kind']=='warning' for e in events))
            recognizer.transcribe.assert_not_called()

    def test_previews_are_never_saved_learned_or_reused_as_context(self):
        import engine,realtime
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp);source=voice(7);events=[];hints=[];speaker_requests=[];capture_options={}
            class Capture:
                def __init__(self,device,wav,chunks,stopped,seconds,meter):
                    self.total,self.error=len(source)//2,None
                    self.ready,self.done=threading.Event(),threading.Event();self.thread=Mock()
                    with wave.open(str(wav),'wb') as f:
                        f.setparams((1,2,RATE,0,'NONE','not compressed'));f.writeframes(source)
                    self.ready.set();self.done.set()
                def start(self):pass
                def stop(self):pass
                def set_sensitivity(self,*args):pass
                def set_segmentation(self,value,**kwargs):self.mode=value;capture_options.update(kwargs)
            recognizer=Mock(prompt='',device='CPU',backend='test',failures=[])
            texts=['暫定の誤字','まだ暫定の誤字','確定した正しい文章','次の発言']
            def recognize(pcm):
                text=texts[len(hints)];hints.append(recognizer.prompt)
                (folder/'pause-control.json').write_text('{"pause_seconds":2.0}','utf-8')
                return {'segments':[dict(start=0,end=len(pcm)/32000,text=text)]}
            recognizer.transcribe.side_effect=recognize
            diarizer=Mock(profiles={},remembered={},memory_error='')
            def speakers(wav,a,b,names,*,preview=False):
                speaker_requests.append((a,b,preview))
                return [dict(start=a/RATE,end=b/RATE,speaker='speaker_0')]
            diarizer.process.side_effect=speakers
            gate=Mock(warning='',intervals=lambda pcm:[(0,len(pcm)//2)])
            requests=[AudioChunk(0,2*RATE,0,0,False),AudioChunk(0,4*RATE,0,0,False),
                      AudioChunk(0,5*RATE,0,0,True),AudioChunk(5*RATE,7*RATE,5*RATE,1,True),None]
            request=dict(model='base',device={'name':'fixture'},live_diarize=True,diarize=False,context_correct=False,pause_seconds=1.5)
            with patch.object(engine,'RESULTS',folder/'results'),patch.object(realtime,'LoopbackCapture',Capture), \
                 patch.object(realtime,'SpeechGate',return_value=gate),patch.object(realtime,'next_live_chunk',side_effect=requests), \
                 patch.object(realtime,'LiveSpeakerProcess',return_value=diarizer), \
                 patch.object(realtime,'live_recognizer_type',return_value=Mock(return_value=recognizer)), \
                 patch.object(engine,'emit',side_effect=lambda kind,**kw:events.append(dict(kind=kind,**kw))), \
                 patch.object(engine,'cache_audio',side_effect=lambda wav,_:str(wav)), \
                 patch('manual_corrections.apply_to_result',return_value=[]) as learned:
                realtime.run_live(request,folder)
            result=json.loads(Path(next(e['result_path'] for e in events if e['kind']=='complete')).read_text('utf-8'))
            self.assertFalse(result['live_incomplete'],result.get('live_error'))
            self.assertEqual(capture_options['pause_seconds'],1.5)
            self.assertEqual(capture_options['control_path'],folder/'pause-control.json')
            self.assertEqual(result['pause_seconds'],2.)
            self.assertEqual([r['text'] for r in result['segments']],texts[2:])
            self.assertEqual([e['kind'] for e in events if e['kind'] in {'live_preview','live_segments'}],
                             ['live_preview','live_preview','live_segments','live_segments'])
            self.assertEqual(hints,['','','','確定した正しい文章'])
            self.assertEqual([x[2] for x in speaker_requests],[True,True,False,False])
            self.assertEqual(learned.call_count,2)
            transcript=Path(result['audio_file']).parents[1]/'transcript.txt'
            self.assertNotIn('暫定',transcript.read_text('utf-8-sig'))
            self.assertEqual(result['duration'],7.)
            with wave.open(result['audio_file'],'rb') as f:self.assertEqual(f.readframes(f.getnframes()),source)


if __name__=='__main__':unittest.main()
