import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
DATA=tempfile.TemporaryDirectory(prefix='whisper-pause-tests-')
for key in list(os.environ):
    if key.upper().startswith('WHISPER_'):del os.environ[key]
os.environ.update(WHISPER_USER_DATA_DIR=str(Path(DATA.name)/'data'),WHISPER_LEGACY_DATA_DIR=str(Path(DATA.name)/'empty'))
from utterance_audio import UtteranceChunker,PAUSE_CHOICES,read_pause_control,RATE
from realtime_audio import LoopbackCapture,Chunker


def voice(seconds):return b'\x00\x10'*round(seconds*RATE)
def silence(seconds):return bytes(round(seconds*RATE)*2)


class PauseSettings(unittest.TestCase):
    def test_longer_pause_keeps_breath_and_phrase_together(self):
        pcm=voice(2.5)+silence(.9)+voice(2.5)+silence(1.5)
        results={}
        for value in (.7,1.2,1.5):
            chunks=[];c=UtteranceChunker(chunks.append,pause_seconds=value)
            c.feed(pcm);c.finish()
            results[value]=chunks
            self.assertEqual(c.total,len(pcm)//2)
        self.assertEqual(len([x for x in results[.7] if x.final]),2)
        for value in (1.2,1.5):
            finals=[x for x in results[value] if x.final]
            self.assertEqual(len(finals),1)
            self.assertEqual(finals[0].end,round((5.9+value)*RATE))
            self.assertTrue(any(not x.final and x.end<=2.5*RATE for x in results[value]))

    def test_stop_does_not_wait_and_long_speech_stays_bounded(self):
        chunks=[];c=UtteranceChunker(chunks.append,pause_seconds=3.)
        c.feed(voice(2));c.finish()
        self.assertFalse(chunks[0].final)
        self.assertTrue(chunks[-1].final)
        self.assertEqual(chunks[-1].end,2*RATE)
        chunks=[];c=UtteranceChunker(chunks.append,pause_seconds=3.)
        c.feed(voice(25));c.finish()
        self.assertTrue(all(x.end-x.start<=12*RATE for x in chunks))
        self.assertEqual([x.end for x in chunks if x.final],[12*RATE,23*RATE,25*RATE])

    def test_capture_applies_change_without_asr_or_resetting_pending_speech(self):
        with tempfile.TemporaryDirectory() as tmp:
            control=Path(tmp)/'pause-control.json'
            capture=LoopbackCapture({},None,None,lambda:False)
            capture.set_segmentation('utterance',pause_seconds=.7,control_path=control)
            chunks=[];c=UtteranceChunker(chunks.append)
            c.feed(voice(.5)+silence(.6))
            previous=(c.total,c.start,c.last_voice,c.voiced,c.core,c.utterance)
            control.write_text('{"pause_seconds":1.5}','utf-8')
            capture.update_chunker(c)
            self.assertEqual(previous,(c.total,c.start,c.last_voice,c.voiced,c.core,c.utterance))
            c.feed(silence(.4));self.assertFalse(any(x.final for x in chunks))
            c.feed(voice(.4)+silence(1.5))
            self.assertEqual(len([x for x in chunks if x.final]),1)
            self.assertEqual(chunks[-1].start,0)
            self.assertEqual(chunks[-1].end,round(3.4*RATE))

    def test_shortening_wait_finishes_pending_silence_without_reopening_final(self):
        with tempfile.TemporaryDirectory() as tmp:
            control=Path(tmp)/'pause-control.json'
            capture=LoopbackCapture({},None,None,lambda:False)
            capture.set_segmentation('utterance',pause_seconds=1.5,control_path=control)
            chunks=[];c=UtteranceChunker(chunks.append,pause_seconds=1.5)
            c.feed(voice(.5)+silence(.9));self.assertEqual(chunks,[])
            control.write_text('{"pause_seconds":0.5}','utf-8');capture.update_chunker(c)
            c.feed(silence(.02))
            self.assertTrue(chunks[0].final)
            final=chunks[0]
            control.write_text('{"pause_seconds":3.0}','utf-8');capture.update_chunker(c)
            c.feed(silence(3));c.finish()
            self.assertEqual(chunks,[final])

    def test_invalid_control_keeps_last_value_and_timed_mode_is_unaffected(self):
        with tempfile.TemporaryDirectory() as tmp:
            control=Path(tmp)/'pause-control.json'
            self.assertEqual(read_pause_control(control,1.5),1.5)
            for value in ('{','[]','{}','{"pause_seconds":false}','{"pause_seconds":100}',
                          '{"pause_seconds":"1.2"}','{"pause_seconds":NaN}','{"pause_seconds":Infinity}'):
                control.write_text(value,'utf-8')
                self.assertEqual(read_pause_control(control,1.5),1.5)
            capture=LoopbackCapture({},None,None,lambda:False)
            capture.set_segmentation('timed',pause_seconds=3.,control_path=control)
            chunks=[];c=Chunker(lambda *args:chunks.append(args))
            capture.update_chunker(c)
            c.feed(voice(.5));c.feed(silence(.4))
            self.assertEqual(len(chunks),1)

    def test_defaults_roundtrip_and_backup_validation(self):
        from recording_settings import processing_preferences,save_settings,load_settings,settings_for
        from data_backup import _validate
        self.assertEqual(processing_preferences({})['pause_seconds'],.7)
        for value in PAUSE_CHOICES:self.assertEqual(processing_preferences({'pause_seconds':value})['pause_seconds'],value)
        for bad in (False,None,{},[],'.7',0,4.,float('nan'),float('inf')):
            with self.assertRaises(ValueError):processing_preferences({'pause_seconds':bad})
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp)
            with patch('recording_settings.settings_path',return_value=folder/'recording_settings.json'):
                save_settings(settings_for('audio',{},processing={'pause_seconds':1.5}))
                self.assertEqual(load_settings()['processing']['pause_seconds'],1.5)
                self.assertEqual(_validate(folder,folder)['settings'],1)


if __name__=='__main__':unittest.main()
