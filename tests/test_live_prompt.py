import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch
import wave

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
DATA = tempfile.TemporaryDirectory(prefix='whisper-prompt-tests-')
for key in list(os.environ):
    if key.upper().startswith('WHISPER_'): del os.environ[key]
os.environ.update(WHISPER_USER_DATA_DIR=str(Path(DATA.name)/'data'),
                  WHISPER_LEGACY_DATA_DIR=str(Path(DATA.name)/'empty'))
from live_prompt import make_prompt, recent_text, reliable, transcribe_with_prompt
from realtime import live_rows, recognize_parts


def row(text, start, end, **extra):
    return dict(text=text, start=start, end=end, **extra)


class RecentHints(unittest.TestCase):
    def test_two_recent_utterances_and_topic_hint(self):
        rows = [row('古い話',0,1), row('政治の話です。',1,2), row('新しい政党について。',2,3)]
        self.assertEqual(make_prompt('専門用語',rows,3), '専門用語\n政治の話です。 新しい政党について。')
        self.assertEqual(make_prompt('専門用語',[],0), '専門用語')

    def test_bounded_and_no_special_tokens(self):
        text = ''.join(chr(0x4e00+i) for i in range(250)) + '<|startoftranscript|>'
        hint = make_prompt('用語'*800, [row(text,0,1)], 1)
        self.assertLessEqual(len(hint),761)
        self.assertLessEqual(len(hint.split('\n')[-1]),160)
        self.assertNotIn('<|',hint)

    def test_pause_resets_recent_and_does_not_reach_across_old_pause(self):
        self.assertEqual(recent_text([row('前の話',0,1)],7),'')
        self.assertEqual(recent_text([row('前の話',0,1),row('新しい話',9,10)],10),'新しい話')
        self.assertEqual(recent_text([row('続く話',0,1)],6.9),'続く話')

    def test_bad_and_future_timestamps_do_not_become_context(self):
        for a,b in ((0,float('nan')),(4,5),(-1,1),(2,1)):
            self.assertEqual(recent_text([row('使わない',a,b)],2),'')

    def test_repetition_and_unreliable_text_are_not_propagated(self):
        for text in ('字幕字幕字幕字幕','同じです。同じです。同じです。'):
            self.assertEqual(recent_text([row(text,0,1)],1),'')
        self.assertEqual(recent_text([row('はい',i,i+1) for i in range(3)],3),'')
        self.assertEqual(recent_text([row('元の話',0,1),row('怪しい認識',1,2,live_prompt_eligible=False)],2),'')
        self.assertEqual(recent_text([row('はい',0,1)],1),'はい')

    def test_low_confidence_is_not_hidden_from_captions(self):
        for bad in ({'avg_logprob':-1.5},{'no_speech_prob':.7},{'compression_ratio':3.0}, {'avg_logprob':float('nan')}):
            raw = dict(start=0,end=1,text='正当な理由',**bad)
            self.assertFalse(reliable(raw))
            rows = live_rows({'segments':[raw]},0,16000,0,[])
            self.assertEqual(rows[0]['text'],'正当な理由')
            self.assertEqual(recent_text(rows,1),'')


class PromptTransport(unittest.TestCase):
    def test_native_backend_receives_hint_and_restores_original(self):
        from realtime_asr import CppLiveRecognizer
        engine = CppLiveRecognizer.__new__(CppLiveRecognizer)
        engine.prompt,engine.language,engine.url = '用語','ja','http://127.0.0.1/fixture'
        engine.session=Mock()
        engine.session.post.return_value.json.return_value={'segments':[]}
        transcribe_with_prompt(engine,b'\0\0'*100,'用語\n前の文章')
        self.assertEqual(engine.session.post.call_args.kwargs['files']['prompt'],(None,'用語\n前の文章'))
        self.assertEqual(engine.prompt,'用語')

    def test_hf_pipe_receives_hint_and_restores_original(self):
        from whisper_hf_asr import WhisperHFRecognizer
        engine = WhisperHFRecognizer.__new__(WhisperHFRecognizer)
        engine.prompt,engine.language,engine.lock='用語','ja',threading.Lock()
        engine.proc=Mock(stdin=io.StringIO());engine.receive=Mock(return_value={'kind':'result','result':{'segments':[]}})
        transcribe_with_prompt(engine,b'\0\0'*100,'用語\n前の文章')
        request=json.loads(engine.proc.stdin.getvalue())
        self.assertEqual(request['prompt'],'用語\n前の文章')
        self.assertEqual(engine.prompt,'用語')

    def test_failed_inference_restores_prompt(self):
        engine=Mock(prompt='元のヒント',transcribe=Mock(side_effect=RuntimeError('fixture')))
        with self.assertRaises(RuntimeError):transcribe_with_prompt(engine,b'00','前の発言')
        self.assertEqual(engine.prompt,'元のヒント')

    def test_prefetched_and_speaker_split_calls_finish_before_new_hint(self):
        seen=[]
        engine=Mock(prompt='用語')
        def recognize(pcm):
            seen.append(engine.prompt)
            return {'segments':[dict(start=0,end=len(pcm)/32000,text='最初の発言' if len(seen)==2 else '次の発言')]}
        engine.transcribe.side_effect=recognize
        pcm=b'\1\0'*32000
        parts=[dict(start=1,end=2,speaker='a'),dict(start=2,end=3,speaker='b')]
        past=[row('前区間',0,1)]
        with ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(transcribe_with_prompt,engine,pcm,make_prompt('用語',past,1))
            output=list(recognize_parts(engine,pcm,16000,48000,16000,past,parts,future,
                        context_prompt=lambda a,b,rows:make_prompt('用語',rows,a/16000)))
        self.assertEqual(seen[:2],['用語\n前区間','用語\n前区間'])
        self.assertEqual(seen[2],'用語\n前区間 最初の発言')
        self.assertEqual([p['speaker'] for rows,p in output],['a','b'])
        self.assertEqual(engine.prompt,'用語')

    def test_overlapped_caption_is_deduplicated_as_before(self):
        previous=[row('前の文章です',0,2)]
        rows=live_rows({'segments':[row('文章ですそして続きます',0,2)]},16000,48000,32000,previous)
        self.assertEqual(rows[0]['text'],'そして続きます')
        self.assertEqual(rows[0]['start'],2.)


class PromptSettings(unittest.TestCase):
    def test_defaults_saved_off_and_backup_validation(self):
        from recording_settings import processing_preferences,save_settings,load_settings,settings_for
        from data_backup import _validate
        self.assertTrue(processing_preferences({})['live_previous_text'])
        for value in (None,1,'false',[]):
            with self.assertRaises(ValueError):processing_preferences({'live_previous_text':value})
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp)
            with patch('recording_settings.settings_path',return_value=folder/'recording_settings.json'):
                save_settings(settings_for('audio',{},processing={'live_previous_text':False}))
                self.assertFalse(load_settings()['processing']['live_previous_text'])
                self.assertEqual(_validate(folder,folder)['settings'],1)


class LivePromptPipeline(unittest.TestCase):
    def test_context_pause_restart_opt_out_and_other_models(self):
        import engine,realtime
        from live_models import LiveModels
        with tempfile.TemporaryDirectory() as tmp:
            folder=Path(tmp);source=b'\1\0'*16000*12
            prompts=[];outputs=['政治について話します。','新しい政党です。','別の話です。']
            recognizer=Mock(prompt='',device='CPU',backend='fixture',failures=[])
            def transcribe(pcm):
                text=outputs[len(prompts)%3];prompts.append(recognizer.prompt)
                return {'segments':[row(text,.1,.8)]}
            recognizer.transcribe.side_effect=transcribe
            gate=Mock(warning='',intervals=Mock(return_value=[(1600,12800)]))
            models=Mock(recognizer=recognizer,speech=gate,closing=lambda:False)
            models.noise.snapshot.return_value={'enabled':False,'state':'off','text':''}
            models.noise.process.side_effect=lambda pcm:(pcm,False)
            models.park.side_effect=lambda:LiveModels.park(models)
            models.diarizer=None
            def get_recognizer(req,language,prompt,status,stopped):
                recognizer.prompt=prompt
                return recognizer
            models.recognizer_for.side_effect=get_recognizer
            class Capture:
                def __init__(self,device,wav,chunks,stopped,seconds,meter):
                    self.total,self.error=192000,None
                    self.ready,self.done=threading.Event(),threading.Event();self.thread=Mock()
                    with wave.open(str(wav),'wb') as out:
                        out.setparams((1,2,16000,0,'NONE','not compressed'));out.writeframes(source)
                    self.ready.set();self.done.set()
                def start(self):pass
                def stop(self):pass
                def set_sensitivity(self,*args):pass
            for index,(model,enabled) in enumerate((('base',True),('base',True),('base',False),('granite-turboctc',True),('haas-ja-v1.0',True))):
                prompts.clear();events=[];job=folder/str(index);job.mkdir()
                request=dict(model=model,device={'name':'fixture'},prompt='用語',live_previous_text=enabled,
                             live_diarize=False,diarize=False,context_correct=False)
                with patch.object(engine,'RESULTS',folder/'results'),patch.object(realtime,'LoopbackCapture',Capture), \
                     patch.object(realtime,'next_live_chunk',side_effect=[(0,16000,0),(16000,32000,16000),(160000,176000,160000),None]), \
                     patch.object(engine,'emit',side_effect=lambda kind,**kw:events.append(dict(kind=kind,**kw))), \
                     patch.object(engine,'cache_audio',side_effect=lambda wav,_:str(wav)):
                    realtime.run_live(request,job,models)
                result=json.loads(Path(next(e['result_path'] for e in events if e['kind']=='complete')).read_text('utf-8'))
                self.assertFalse(result['live_incomplete'],result.get('live_error'))
                self.assertEqual(prompts,['用語','用語\n政治について話します。' if model=='base' and enabled else '用語','用語'])
                self.assertEqual(recognizer.prompt,'')
                self.assertEqual([r['text'] for r in result['segments']],outputs)
                self.assertEqual([r['start'] for r in result['segments']],[.1,1.1,10.1])
                self.assertEqual(result['duration'],12.)
                with wave.open(result['audio_file'],'rb') as audio:self.assertEqual(audio.readframes(audio.getnframes()),source)


if __name__=='__main__':unittest.main()
