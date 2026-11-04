from copy import deepcopy
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

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import live_organization as organize
from live_context import apply_changes


def row(key, text, start=0, end=1, speaker='a'):
    return {'segment_id': key, 'id': key, 'text': text, 'start': start, 'end': end,
            'speaker': speaker, 'words': [{'word': text, 'start': start, 'end': end}]}


class Organization(unittest.TestCase):
    def setUp(self):
        self.memory = patch('live_context.filter_change', side_effect=lambda a, b: (b, 0))
        self.memory.start()
        self.addCleanup(self.memory.stop)

    def test_only_punctuation_and_spacing_not_rewording_or_numbers(self):
        for before, after in [('今日は。', '今日は'), ('天気がいいので散歩します', '天気がいいので、散歩します。'),
                              ('Hello world', 'Hello, world!')]:
            self.assertTrue(organize.punctuation_change(before, after))
        for before, after in [('賛成しません', '賛成します。'), ('田中です', '佐藤です。'),
                              ('1.5です', '15です。'), ('-5です', '5です。'), ('12:30', '1230'),
                              ('now here', 'nowhere'), ('はいはい', 'はい。'), ('本当です', '本当です。本当です。'),
                              ('えーと今日は', '今日は。')]:
            self.assertFalse(organize.punctuation_change(before, after), (before, after))

    def test_boundary_uses_following_speech_without_merging_times_or_voices(self):
        data = {'segments': [row('1', '今日は。'), row('2', 'いい天気なので。', 1.05, 2), row('3', '散歩します', 2.05, 3)]}
        original = deepcopy(data)
        records = apply_changes(data, organize.boundary_changes(data))
        self.assertEqual([r['text'] for r in data['segments']], ['今日は', 'いい天気なので、', '散歩します'])
        self.assertEqual([r['before'] for r in records], ['今日は。', 'いい天気なので。'])
        for before, after in zip(original['segments'], data['segments']):
            self.assertEqual([before[k] for k in ('start', 'end', 'speaker', 'segment_id')],
                             [after[k] for k in ('start', 'end', 'speaker', 'segment_id')])
        self.assertEqual(records[0]['origin'], 'live_organization')
        self.assertEqual(data['segments'][0]['live_asr_text'], '今日は。')

    def test_boundary_respects_pauses_speaker_changes_unknown_voice_and_manual_edits(self):
        for modification in ('pause', 'speaker', 'unknown', 'manual'):
            data = {'segments': [row('1', '今日は。'), row('2', 'いい天気', 1.1, 2)]}
            if modification == 'pause': data['segments'][1]['start'] = 2
            if modification == 'speaker': data['segments'][1]['speaker'] = 'b'
            if modification == 'unknown':
                for r in data['segments']: r['speaker'] = 'unknown'
            if modification == 'manual': data['live_text_edits'] = {'1': {'text': '今日は。'}}
            self.assertFalse(organize.boundary_changes(data), modification)

    def test_snapshot_revisits_prior_rows_with_new_context_but_not_its_own_punctuation(self):
        data = {'segments': [row('1', '今日は。')]}
        first = organize.snapshot(data, {})
        seen = {first['key']: True}
        data['segments'][0]['text'] = '今日は'
        self.assertIsNone(organize.snapshot(data, seen))
        data['segments'].append(row('2', '天気です', 1, 2))
        following = organize.snapshot(data, seen)
        self.assertEqual([r['segment_id'] for r in following['targets']], ['1', '2'])
        seen[following['key']] = True
        self.assertIsNotNone(organize.snapshot(data, seen, final=True))
        data['live_text_edits'] = {'1': {'text': '今日は'}}
        self.assertEqual([r['segment_id'] for r in organize.snapshot(data, {})['targets']], ['2'])

    def test_ai_cannot_reintroduce_a_premature_boundary_stop(self):
        data = {'segments': [row('1', '今日は'), row('2', 'いい天気です', 1.1, 2)]}
        change = dict(segment_id='1', speaker='a', before='今日は', after='今日は。',
                      origin='live_organization', reason='AI proposal')
        self.assertEqual(apply_changes(data, [change]), [])
        self.assertEqual(data['segments'][0]['text'], '今日は')

    def test_snapshot_bounded_and_does_not_reach_across_long_pause(self):
        data = {'segments': [row(str(i), '発言です' * 8, i, i + .8) for i in range(100)]}
        value = organize.snapshot(data, {})
        self.assertLessEqual(len(value['targets']), 4)
        self.assertLessEqual(sum(len(r['text']) for r in value['targets'] + value['context_before']), 640)
        data['segments'].append(row('last', '別の話です', 110, 111))
        self.assertEqual(len(organize.snapshot(data, {})['targets']), 1)

    def test_ai_full_rows_are_validated_and_invented_words_rejected(self):
        import context_ai
        data = {'segments': [row('1', '今日は。'), row('2', '1.5時間です', 1, 2)]}
        request = organize.snapshot(data, {})
        answer = {'message': {'content': json.dumps({'rows': [{'id': 0, 'text': '今日は'},
                                                               {'id': 1, 'text': '15時間です。'}]})}}
        with patch.object(context_ai, 'local_request', return_value=answer) as call:
            changes = organize.proofread(request)
        self.assertEqual([c['segment_id'] for c in changes], ['1'])
        self.assertEqual(call.call_args.kwargs['timeout'], organize.TIMEOUT)
        answer['message']['content'] = '{"rows":[{"id":0,"text":"今日は"},{"id":0,"text":"重複"}]}'
        with patch.object(context_ai, 'local_request', return_value=answer), self.assertRaises(ValueError):
            organize.proofread(request)

    def test_stale_manual_or_changed_speaker_is_not_overwritten(self):
        change = {'segment_id': '1', 'speaker': 'a', 'before': '今日は。', 'after': '今日は',
                  'origin': 'live_organization', 'reason': 'test'}
        for kind in ('text', 'manual', 'speaker'):
            data = {'segments': [row('1', '今日は。')]}
            if kind == 'text': data['segments'][0]['text'] = '手動で直した文章'
            if kind == 'manual': data['live_text_edits'] = {'1': {'text': '今日は。'}}
            if kind == 'speaker': data['segments'][0]['speaker'] = 'b'
            before = deepcopy(data)
            self.assertEqual(apply_changes(data, [change]), [])
            self.assertEqual(data, before)

    def test_undo_restores_word_timing_and_remembers_rejected_punctuation(self):
        import context_ai, correction_memory
        with tempfile.TemporaryDirectory() as tmp, patch('correction_memory.memory_path', return_value=Path(tmp)/'memory.json'):
            data = {'segments': [row('1', '今日は。'), row('2', '天気です', 1, 2)]}
            words = deepcopy(data['segments'][0]['words'])
            records = apply_changes(data, organize.boundary_changes(data))
            context_ai.set_review_applied(records[0], data['segments'], False)
            self.assertEqual(data['segments'][0]['text'], '今日は。')
            self.assertEqual(data['segments'][0]['words'], words)
            self.assertEqual(correction_memory.filter_change('今日は。', '今日は')[0], '今日は。')
            context_ai.set_review_applied(records[0], data['segments'], True)
            self.assertEqual(data['segments'][0]['text'], '今日は')

    def test_overlap_normalizes_punctuation_only_at_repeated_audio_boundary(self):
        from realtime import live_rows, RATE
        previous = [row('0', '今日は、いい天気', 0, 2)]
        result = {'segments': [dict(start=0, end=2, text='今日はいい天気なので散歩します')]}
        trimmed = live_rows(result, RATE, 3*RATE, 2*RATE, previous, speaker='a')
        self.assertEqual(trimmed[0]['text'], 'なので散歩します')
        self.assertEqual(trimmed[0]['live_original_text'], result['segments'][0]['text'])
        natural = live_rows(result, 2*RATE, 4*RATE, 2*RATE, previous, speaker='a')
        other = live_rows(result, RATE, 3*RATE, 2*RATE, previous, speaker='b')
        self.assertEqual(natural[0]['text'], result['segments'][0]['text'])
        self.assertEqual(other[0]['text'], result['segments'][0]['text'])
        self.assertEqual(organize.overlap_prefix('値は1.5です', '値は15ですそして続きます'), '値は15ですそして続きます')

    def test_slow_ai_response_keeps_worker_available_for_following_speech(self):
        import context_ai, live_context, local_ai_runtime
        requests = [{'compute': 'cpu'}, {'task': 'organize', 'session': 'one'}, {'task': 'organize', 'session': 'two'}]
        output = io.StringIO()
        with patch('sys.stdin', io.StringIO('\n'.join(json.dumps(r) for r in requests)+'\n')), \
             patch('sys.stdout', output), patch.object(context_ai, 'COMPUTE_MODE', 'auto'), \
             patch.object(context_ai, 'ensure_model'), patch.object(context_ai, 'local_request', return_value={}), \
             patch.object(local_ai_runtime, 'available', return_value=True), patch.object(local_ai_runtime, 'stop'), \
             patch.object(live_context, 'proofread', side_effect=[RuntimeError('timeout'), []]):
            live_context.worker()
        events = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([e['kind'] for e in events], ['ready', 'done', 'done'])
        self.assertTrue(events[1]['warning'])
        self.assertEqual(events[1]['changes'], [])
        self.assertEqual(events[2]['session'], 'two')

    def test_default_on_and_saved_off_survive_backup_validation(self):
        from recording_settings import processing_preferences, settings_for, save_settings, load_settings
        from data_backup import _validate
        self.assertTrue(processing_preferences({})['live_organize'])
        for invalid in (None, 1, 'on'):
            with self.assertRaises(ValueError): processing_preferences({'live_organize': invalid})
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            with patch('recording_settings.settings_path', return_value=folder/'recording_settings.json'):
                save_settings(settings_for('audio', {}, processing={'live_organize': False}))
                self.assertFalse(load_settings()['processing']['live_organize'])
                self.assertEqual(_validate(folder, folder)['settings'], 1)


class LivePipeline(unittest.TestCase):
    def test_stream_preview_original_audio_stop_cleanup_and_durable_review(self):
        import context_ai, engine, live_context, realtime
        from utterance_audio import AudioChunk
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp).resolve()
            source = b'\x01\x02' * (3 * realtime.RATE)
            events, requests = [], []
            class Capture:
                def __init__(self, device, wav, *args):
                    self.total, self.error = 3 * realtime.RATE, None
                    self.ready, self.done, self.halt = threading.Event(), threading.Event(), threading.Event()
                    self.thread = Mock()
                    self.ready.set(); self.done.set()
                    with wave.open(str(wav), 'wb') as f:
                        f.setparams((1, 2, realtime.RATE, 0, 'NONE', 'not compressed')); f.writeframes(source)
                def start(self): pass
                def stop(self): self.halt.set()
                def set_sensitivity(self, *args): pass
                def set_segmentation(self, *args, **kwargs): pass
            class Proofreader:
                ready, busy, failed, state = True, False, False, 'ready'
                proc = Mock(poll=Mock(return_value=0))
                def __init__(self): self.replies = []
                def poll(self):
                    result, self.replies = self.replies, []
                    self.busy = False
                    return result
                def submit(self, value, session):
                    if self.busy or self.failed: return False
                    requests.append(deepcopy(value))
                    changes = []
                    for r in value['targets']:
                        text = r['text']
                        after = text + '。' if text == '散歩に行きます' else text
                        if after != text:
                            changes.append(dict(segment_id=r['segment_id'], speaker=r['speaker'], before=text, after=after,
                                                origin='live_organization', reason='文章末の句点'))
                    self.replies.append(dict(session=session, changes=changes))
                    self.busy = True
                    return True
                def close(self): self.ready = False
            proofreader = Proofreader()
            recognizer = Mock(prompt='', device='CPU', backend='fixture', failures=[])
            texts = iter(['暫定の文', '今日は。', 'いい天気なので。', '散歩に行きます'])
            recognizer.transcribe.side_effect = lambda pcm: {'segments': [dict(start=0, end=len(pcm)/32000, text=next(texts))]}
            gate = Mock(warning='', intervals=lambda pcm: [(0, len(pcm)//2)])
            chunks = [AudioChunk(0, 8000, 0, 0, False), AudioChunk(0, 16000, 0, 0, True),
                      AudioChunk(16000, 32000, 16000, 1, True), AudioChunk(32000, 48000, 32000, 2, True), None]
            request = dict(model='base', device={'name': 'fixture'}, live_diarize=False, diarize=False,
                           context_correct=False, live_correct=False, live_organize=True)
            with patch.object(engine, 'RESULTS', folder/'results'), patch.object(realtime, 'LoopbackCapture', Capture), \
                 patch.object(realtime, 'SpeechGate', return_value=gate), \
                 patch.object(realtime, 'live_recognizer_type', return_value=Mock(return_value=recognizer)), \
                 patch.object(realtime, 'next_live_chunk', side_effect=chunks), \
                 patch.object(live_context, 'LiveProofreader', return_value=proofreader), \
                 patch.object(live_context, 'filter_change', side_effect=lambda a,b:(b,0)), \
                 patch('manual_corrections.apply_to_result', return_value=[]), \
                 patch.object(engine, 'cache_audio', side_effect=lambda wav, target: str(wav)), \
                 patch.object(engine, 'emit', side_effect=lambda kind,**kw:events.append(deepcopy(dict(kind=kind,**kw)))):
                realtime.run_live(request, folder)
            result_path = Path(next(e['result_path'] for e in events if e['kind']=='complete'))
            result = json.loads(result_path.read_text('utf-8'))
            self.assertFalse(result['live_incomplete'], result.get('live_error'))
            self.assertEqual([r['text'] for r in result['segments']], ['今日は', 'いい天気なので、', '散歩に行きます。'])
            self.assertEqual([r['live_original_text'] for r in result['segments']], ['今日は。', 'いい天気なので。', '散歩に行きます'])
            self.assertEqual([r['start'] for r in result['segments']], [0.,1.,2.])
            self.assertTrue(any(r['final'] for r in requests))
            self.assertTrue(result['context_review'])
            self.assertTrue(any(e['kind']=='live_corrections' for e in events))
            self.assertFalse(any(r['text']=='暫定の文' for request in requests for r in request['targets']))
            with wave.open(result['audio_file'], 'rb') as f:
                self.assertEqual(f.readframes(f.getnframes()), source)


if __name__ == '__main__':
    unittest.main()
