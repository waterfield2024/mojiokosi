from copy import deepcopy
import io
import json
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
import wave

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from overlap import regions, plan, apply_reply, set_state
from overlap_worker import read_pcm, distinct_streams


def row(key, start, end, text='原文', speaker='speaker_00', **kwargs):
    return dict(segment_id=key, start=start, end=end, text=text, speaker=speaker, **kwargs)


class OverlapDetection(unittest.TestCase):
    def test_duplicate_turns_and_touching_speakers_are_not_overlap(self):
        self.assertEqual(regions([row('a',0,2),row('b',0,2),row('c',2,4,speaker='speaker_01')]), [])

    def test_true_overlap_keeps_exact_intersection_and_count(self):
        self.assertEqual(regions([row('a',0,4),row('b',1,3,speaker='speaker_01'),row('c',2,3,speaker='speaker_02')]),
                         [dict(start=1.,end=3.,voices=3)])

    def test_short_overlap_ignored_and_local_ids_work_before_clustering(self):
        self.assertEqual(regions([dict(start=0,end=2,local=0),dict(start=1.95,end=3,local=1)]), [])
        self.assertEqual(regions([dict(start=0,end=3,local=0),dict(start=1,end=2,local=1)])[0]['start'], 1)


class Transaction(unittest.TestCase):
    def setUp(self):
        self.data = {'segments': [row('before',0,1,'その前'),row('mixed',1,5),row('later',5,7,'その後')], 'speakers': {}}
        self.intervals = [dict(start=2,end=3,voices=2),dict(start=3.5,end=4,voices=2)]

    def prepare(self):
        task, = plan(self.data,self.intervals,set())
        reply = {'id':task['id'],'after':[row('one',1,5,'甲',overlap_separated=True),row('two',1,5,'乙',speaker='speaker_01',overlap_separated=True)]}
        return task,reply

    def test_whole_subtitle_selected_once_and_nonoverlap_preserved(self):
        task,reply = self.prepare()
        self.assertEqual((task['start'],task['end']), (1,5))
        self.assertEqual([r['segment_id'] for r in task['before']], ['mixed'])
        original = deepcopy(self.data['segments'])
        record=apply_reply(self.data,reply)
        self.assertEqual([r['text'] for r in self.data['segments']],['その前','甲','乙','その後'])
        self.assertTrue(set_state(self.data,record,'reverted'))
        self.assertEqual(self.data['segments'],original)
        self.assertTrue(set_state(self.data,record,'applied'))
        self.assertFalse(set_state(self.data,record,'applied'))

    def test_adjacent_overlapping_subtitles_share_one_separation_task(self):
        data={'segments':[row('one',0,3),row('two',3,6),row('clean',6,8)]}
        tasks=plan(data,[dict(start=.5,end=2,voices=2),dict(start=3.5,end=5,voices=2)],set())
        self.assertEqual(len(tasks),1)
        self.assertEqual([r['segment_id'] for r in tasks[0]['before']],['one','two'])
        self.assertEqual((tasks[0]['start'],tasks[0]['end']),(0,6))

    def test_manual_edit_during_inference_is_never_overwritten(self):
        task,reply=self.prepare()
        self.data['segments'][1].update(text='手で直した',text_manual=True)
        self.assertEqual(apply_reply(self.data,reply)['status'],'skipped')
        self.assertEqual(self.data['segments'][1]['text'],'手で直した')

    def test_automatic_punctuation_can_be_replaced_and_undo_restores_it(self):
        task,reply=self.prepare()
        self.data['segments'][1]['text']='原文。'
        record=apply_reply(self.data,reply)
        self.assertEqual(record['status'],'applied')
        set_state(self.data,record,'reverted')
        self.assertEqual(self.data['segments'][1]['text'],'原文。')

    def test_reassigned_speaker_or_timing_change_rejects_stale_reply(self):
        for field,value in [('speaker','speaker_03'),('start',1.1)]:
            self.setUp();task,reply=self.prepare()
            self.data['segments'][1][field]=value
            self.assertEqual(apply_reply(self.data,reply)['status'],'skipped')

    def test_failed_or_out_of_bounds_results_keep_all_original_text(self):
        task,reply=self.prepare();original=deepcopy(self.data['segments'])
        reply['after'][0]['end']=7
        self.assertEqual(apply_reply(self.data,reply)['status'],'skipped')
        self.assertEqual(self.data['segments'],original)

    def test_cannot_undo_over_manual_edits(self):
        task,reply=self.prepare();record=apply_reply(self.data,reply)
        self.data['segments'][1]['text']='編集後'
        self.data['segments'][1]['text_manual']=True
        with self.assertRaises(ValueError):set_state(self.data,record,'reverted')
        self.assertEqual(self.data['segments'][1]['text'],'編集後')

    def test_three_voices_and_long_spans_are_saved_for_review(self):
        self.assertEqual(plan(self.data,[dict(start=2,end=3,voices=3)],set()),[])
        self.assertEqual(self.data['overlap_review'][0]['status'],'skipped')
        data={'segments':[row('long',0,31)]}
        self.assertEqual(plan(data,[dict(start=2,end=3,voices=2)],set()),[])

    def test_reverted_and_already_separated_rows_not_automatically_reprocessed(self):
        task,reply=self.prepare();record=apply_reply(self.data,reply)
        self.assertEqual(plan(self.data,self.intervals,set()),[])
        set_state(self.data,record,'reverted')
        self.assertEqual(plan(self.data,self.intervals,{'mixed'}),[])


class AudioIntegrity(unittest.TestCase):
    def test_reads_riff_metadata_and_growing_wav_without_modifying_file(self):
        pcm=np.arange(100,dtype='<i2').tobytes()
        stream=io.BytesIO()
        with wave.open(stream,'wb') as f:
            f.setparams((1,2,16000,0,'NONE','not compressed'));f.writeframes(pcm)
        original=stream.getvalue()
        metadata=b'JUNK'+struct.pack('<I',4)+b'ABCD'
        original=original[:36]+metadata+original[36:]
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'recording.wav';path.write_bytes(original)
            self.assertEqual(read_pcm(path,10,20),pcm[20:40]);self.assertEqual(path.read_bytes(),original)
            with self.assertRaises(ValueError):read_pcm(path,90,110)

    def test_one_voice_copies_silence_and_nan_are_rejected(self):
        rng=np.random.default_rng(1);a=rng.normal(size=1600).astype('float32');b=rng.normal(size=1600).astype('float32')
        self.assertFalse(distinct_streams(np.stack([a,a]),a))
        self.assertFalse(distinct_streams(np.stack([a,a*0]),a))
        self.assertFalse(distinct_streams(np.stack([a,a*np.nan]),a))
        self.assertTrue(distinct_streams(np.stack([a,b]),a+b))

    def test_history_plays_separated_audio_at_original_offset(self):
        from history_audio import playback_target
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);path=root/'.audio/separated/a.wav';path.parent.mkdir(parents=True);path.touch()
            data=row('a',10.5,11.5,separated_audio='.audio/separated/a.wav',separated_offset=10)
            self.assertEqual(playback_target({},root,data),(path,(.5,1.5)))


class DisplayAndSettings(unittest.TestCase):
    def test_two_simultaneous_voices_survive_caption_timing(self):
        from caption_timing import timed_segments
        from overlap_captions import cues
        rows=[row('a',0,4,'甲',overlap_group='g',overlap_separated=True),row('b',0,4,'乙',speaker='speaker_01',overlap_group='g',overlap_separated=True)]
        timed=timed_segments(rows)
        self.assertEqual(len(timed),2)
        result=cues(timed,lambda text,name:[name+'：'+text],{'speaker_00':'一郎','speaker_01':'花子'})
        self.assertEqual(result[0]['lines'],['一郎：甲','花子：乙'])
        self.assertEqual((result[0]['start_cs'],result[0]['end_cs']),(0,400))
        self.assertTrue(all(len(c['lines'])<=2 for c in result))

    def test_overlap_switch_defaults_on_and_persists_off(self):
        from recording_settings import processing_preferences
        self.assertTrue(processing_preferences()['live_overlap'])
        self.assertFalse(processing_preferences({'live_overlap':False})['live_overlap'])

    def test_worker_queue_is_bounded_and_submission_does_not_load_models(self):
        from collections import deque
        from overlap_service import OverlapService
        service=OverlapService.__new__(OverlapService)
        service.pending=deque();service.active=None;service.failed=service.closed=False;service.capacity=2
        self.assertTrue(service.submit({'id':'a'}));self.assertTrue(service.submit({'id':'b'}))
        self.assertFalse(service.submit({'id':'c'}))
        self.assertEqual(len(service.pending),2)


class LiveIntegration(unittest.TestCase):
    def test_original_captions_arrive_before_async_patch_and_audio_stays_unchanged(self):
        import threading
        import engine, realtime
        from utterance_audio import AudioChunk
        pcm=(np.sin(np.arange(64000)*.1)*4000).astype('<i2').tobytes()
        events=[];owned=[]
        class Capture:
            def __init__(self,device,wav,chunks,stopped,seconds,meter):
                self.total=len(pcm)//2;self.error=None;self.chunks=chunks;self.thread=Mock()
                self.ready=threading.Event();self.done=threading.Event();self.halt=threading.Event()
                with wave.open(str(wav),'wb') as f:
                    f.setparams((1,2,16000,0,'NONE','not compressed'));f.writeframes(pcm)
                self.ready.set();self.done.set()
            def start(self):self.chunks.put(AudioChunk(0,self.total,0,0,True));self.chunks.put(None)
            def stop(self):self.halt.set()
            def set_sensitivity(self,*args):pass
            def set_segmentation(self,*args,**kwargs):pass
        class Service:
            message='分離中';failed=False
            def __init__(self,*args,**kwargs):self.tasks=[];self.closed=False;owned.append(self)
            def submit(self,task):self.tasks.append(task);return True
            @property
            def busy(self):return bool(self.tasks)
            def poll(self):
                if not self.tasks:return []
                task=self.tasks.pop(0)
                return [dict(id=task['id'],after=[row('a',0,4,'甲',overlap_separated=True),row('b',0,4,'乙',speaker='speaker_01',overlap_separated=True)])]
            def close(self):self.closed=True
        recognizer=Mock(prompt='',device='CPU',backend='fixture',failures=[])
        recognizer.transcribe.return_value={'segments':[dict(start=0,end=4,text='先に表示する原文')]}
        speaker=Mock(profiles={},remembered={},memory_error='',overlaps=[dict(start=1,end=3,voices=2)])
        speaker.process.return_value=[dict(start=0,end=4,speaker='unknown',speaker_review=True)]
        gate=Mock(warning='',intervals=lambda audio:[(0,len(audio)//2)])
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp)
            with patch.object(engine,'RESULTS',folder/'results'),patch.object(realtime,'LoopbackCapture',Capture), \
                 patch.object(realtime,'SpeechGate',return_value=gate),patch.object(realtime,'LiveSpeakerProcess',return_value=speaker), \
                 patch.object(realtime,'live_recognizer_type',return_value=Mock(return_value=recognizer)), \
                 patch('overlap_service.OverlapService',Service),patch('manual_corrections.apply_to_result',return_value=[]), \
                 patch.object(engine,'cache_audio',side_effect=lambda path,target:str(path)), \
                 patch.object(engine,'emit',side_effect=lambda kind,**values:events.append(deepcopy(dict(kind=kind,**values)))):
                realtime.run_live(dict(model='base',device={'name':'fixture'},live_diarize=True,live_overlap=True,
                    live_correct=False,live_organize=False,context_correct=False,diarize=False),folder)
            kinds=[event['kind'] for event in events]
            self.assertLess(kinds.index('live_segments'),kinds.index('live_overlap_patch'))
            original=next(e for e in events if e['kind']=='live_segments')
            self.assertTrue(original['segments'][0]['overlap_detected'])
            result=json.loads(Path(next(e['result_path'] for e in events if e['kind']=='complete')).read_text('utf8'))
            self.assertFalse(result['live_incomplete'],result.get('live_error'))
            self.assertEqual([r['text'] for r in result['segments']],['甲','乙'])
            self.assertEqual(result['overlap_review'][0]['before'][0]['text'],'先に表示する原文')
            with wave.open(result['source'],'rb') as f:self.assertEqual(f.readframes(f.getnframes()),pcm)
            self.assertTrue(owned[0].closed)


if __name__ == '__main__':unittest.main()
