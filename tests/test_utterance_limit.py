import json
import os
from pathlib import Path
import random
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
DATA=tempfile.TemporaryDirectory(prefix='whisper-limit-tests-')
for key in list(os.environ):
    if key.upper().startswith('WHISPER_'):del os.environ[key]
os.environ.update(WHISPER_USER_DATA_DIR=str(Path(DATA.name)/'data'),WHISPER_LEGACY_DATA_DIR=str(Path(DATA.name)/'empty'))
from utterance_audio import UtteranceChunker,MAX_CHOICES,read_limit_control,RATE
from realtime_audio import LoopbackCapture,Chunker


def voice(seconds):return b'\x00\x10'*round(seconds*RATE)
def silence(seconds):return bytes(round(seconds*RATE)*2)


class UtteranceLimit(unittest.TestCase):
    def test_long_sentence_stays_together_with_live_previews(self):
        pcm=voice(18)+silence(.7)
        for limit,expected in ((12,2),(20,1),(30,1)):
            with self.subTest(limit=limit):
                chunks=[];c=UtteranceChunker(chunks.append,max_utterance_seconds=limit)
                c.feed(pcm);c.finish()
                finals=[x for x in chunks if x.final]
                self.assertEqual(len(finals),expected)
                self.assertEqual(finals[-1].end,len(pcm)//2)
                self.assertTrue(any(not x.final and x.end==2*RATE for x in chunks))
                if limit>12:self.assertTrue(any(not x.final and x.end>12*RATE for x in chunks))

    def test_exact_input_bounds_overlap_and_complete_timeline_with_irregular_blocks(self):
        # 250 ms of pre-roll is not a multiple of the 20 ms analysis frame.
        # Also exercise actual resampler-sized blocks, not only whole seconds.
        pcm=silence(.36)+voice(67.137)
        for limit in MAX_CHOICES:
            with self.subTest(limit=limit):
                chunks=[];c=UtteranceChunker(chunks.append,max_utterance_seconds=limit)
                rng=random.Random(83);offset=0
                while offset<len(pcm):
                    n=rng.randint(1,1900)*2;c.feed(pcm[offset:offset+n]);offset+=n
                c.finish()
                finals=[x for x in chunks if x.final]
                self.assertTrue(all(x.end-x.start<=limit*RATE for x in chunks))
                self.assertEqual(c.total,len(pcm)//2)
                self.assertEqual(finals[0].core,0)
                self.assertEqual(finals[-1].end,c.total)
                for previous,current in zip(finals,finals[1:]):
                    self.assertEqual(current.core,previous.end)
                    self.assertEqual(current.start,previous.end-RATE)
                rebuilt=b''.join(pcm[x.core*2:x.end*2] for x in finals)
                self.assertEqual(rebuilt,pcm)

    def test_live_increase_retains_pending_speech_without_resetting_timeline(self):
        with tempfile.TemporaryDirectory() as tmp:
            control=Path(tmp)/'utterance-control.json'
            capture=LoopbackCapture({},None,None,lambda:False)
            capture.set_segmentation('utterance',max_control_path=control)
            chunks=[];c=UtteranceChunker(chunks.append)
            c.feed(voice(10));before=(c.total,c.start,c.core,c.utterance)
            control.write_text('{"max_utterance_seconds":30}','utf-8');capture.update_chunker(c)
            self.assertEqual((c.total,c.start,c.core,c.utterance),before)
            c.feed(voice(15));self.assertFalse(any(x.final for x in chunks))
            c.feed(silence(.7))
            self.assertEqual([(x.start,x.end) for x in chunks if x.final],[(0,round(25.7*RATE))])

    def test_live_decrease_keeps_all_recorded_audio_and_does_not_reopen_finals(self):
        with tempfile.TemporaryDirectory() as tmp:
            control=Path(tmp)/'utterance-control.json'
            capture=LoopbackCapture({},None,None,lambda:False)
            capture.set_segmentation('utterance',max_utterance_seconds=30,max_control_path=control)
            chunks=[];c=UtteranceChunker(chunks.append,max_utterance_seconds=30)
            c.feed(voice(21))
            control.write_text('{"max_utterance_seconds":8}','utf-8');capture.update_chunker(c)
            c.feed(voice(9))
            finals=[x for x in chunks if x.final]
            self.assertEqual([x.end/RATE for x in finals],[21,28])
            self.assertEqual(finals[1].end-finals[1].start,8*RATE)
            control.write_text('{"max_utterance_seconds":30}','utf-8');capture.update_chunker(c)
            c.feed(voice(1));c.finish()
            self.assertEqual([(x.core/RATE,x.end/RATE) for x in chunks if x.final],[(0,21),(21,28),(28,31)])
            self.assertEqual([x for x in chunks if x.final][:2],finals)

    def test_stop_and_pause_still_finalize_before_the_maximum(self):
        chunks=[];c=UtteranceChunker(chunks.append,max_utterance_seconds=30)
        c.feed(voice(.4)+silence(.7)+voice(.2));c.finish();c.finish()
        self.assertEqual([round(x.end/RATE,2) for x in chunks if x.final],[1.1,1.3])
        with self.assertRaises(ValueError):c.feed(b'\x00')

    def test_invalid_controls_and_legacy_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            control=Path(tmp)/'utterance-control.json'
            self.assertEqual(read_limit_control(control,20),20)
            for value in ('{','[]','{}','{"max_utterance_seconds":true}','{"max_utterance_seconds":60}',
                          '{"max_utterance_seconds":"30"}','{"max_utterance_seconds":NaN}',
                          '{"max_utterance_seconds":12.0}'):
                control.write_text(value,'utf-8');self.assertEqual(read_limit_control(control,20),20)
            capture=LoopbackCapture({},None,None,lambda:False)
            capture.set_segmentation('timed',max_utterance_seconds=30,max_control_path=control)
            chunks=[];c=Chunker(lambda *args:chunks.append(args),seconds=2)
            capture.update_chunker(c)
            for _ in range(3):c.feed(voice(1))
            self.assertEqual(chunks,[(0,2*RATE,0)])

    def test_default_settings_roundtrip_and_backup(self):
        from recording_settings import processing_preferences,save_settings,load_settings,settings_for
        from data_backup import _validate
        self.assertEqual(processing_preferences({})['max_utterance_seconds'],12)
        for value in MAX_CHOICES:
            with tempfile.TemporaryDirectory() as tmp:
                folder=Path(tmp)
                with patch('recording_settings.settings_path',return_value=folder/'recording_settings.json'):
                    save_settings(settings_for('audio',{},processing={'max_utterance_seconds':value,'pause_seconds':1.5}))
                    saved=load_settings()['processing']
                    self.assertEqual(saved['max_utterance_seconds'],value)
                    self.assertEqual(saved['pause_seconds'],1.5)
                    self.assertEqual(_validate(folder,folder)['settings'],1)
        for value in (False,True,None,{},[],'20',0,31,60,12.,float('nan'),float('inf')):
            with self.assertRaises(ValueError):processing_preferences({'max_utterance_seconds':value})


if __name__=='__main__':unittest.main()
