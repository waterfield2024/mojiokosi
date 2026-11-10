import json
import os
from pathlib import Path
import tempfile
import unittest

from janome.tokenizer import Tokenizer
from noun_search import load_synsets, noun_key
from transcript_library import index_nouns, load_records, record_playback_target, related_rows, search_nouns


class TranscriptLibraryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tokenizer = Tokenizer()

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name) / 'transcripts'

    def save(self, folder, lines, filename='result.json', **fields):
        path = self.root / folder / filename
        path.parent.mkdir(parents=True, exist_ok=True)
        rows = [dict(start=i, end=i+1, text=text, speaker='speaker_0', segment_id=str(i)) for i, text in enumerate(lines)]
        path.write_text(json.dumps(dict(title='同じ記録名', segments=rows, speakers={'speaker_0': '佐藤'}, **fields)), 'utf8')
        return path

    def test_exact_nouns_cross_record_and_edits(self):
        first = 'リアルタイム_20261023_104522_ab12cd'
        self.save(first, ['廃案にした。'])
        self.save(first, ['😀生成ＡＩを調べた。'], 'edited.json')
        other = self.save('20261021_103000_file_a', ['aiとAIを使った。', 'AIで試す。', 'MAILを送った。'])
        records, skipped = load_records(self.root)
        self.assertEqual((len(records), skipped), (2, 0))
        self.assertEqual(records[0].path.name, 'edited.json')
        self.assertEqual(records[0].date.strftime('%Y-%m-%d'), '2026-10-23')
        self.assertEqual(records[1].lines[1].speaker, '佐藤')
        indexes = {i: index_nouns(r, self.tokenizer) for i, r in enumerate(records)}
        self.assertEqual(indexes[0].at(0, 3), noun_key('生成ＡＩ'))
        self.assertEqual(indexes[0].at(0, 3, {'ai'}), 'ai')
        self.assertNotIn('を', indexes[0].labels)
        self.assertEqual(list(related_rows(records, indexes, 0, 'ai')), [(1, 0), (1, 1)])
        self.assertNotIn('廃案', indexes[0].labels)
        other.unlink()
        self.assertEqual(len(load_records(self.root)[0]), 1)

    def test_partial_live_edit_does_not_hide_final_result(self):
        original = self.save('live', ['前半', '後半'], mode='realtime', live_incomplete=False)
        self.save('live', ['前半'], 'edited.json', mode='realtime', live_incomplete=True)
        records, _ = load_records(self.root)
        self.assertEqual(records[0].path, original)
        self.assertEqual(len(records[0].lines), 2)

    def test_playback_uses_matched_records_edited_interval_and_separated_track(self):
        self.save('meeting', ['原文'])
        path = self.save('meeting', ['修正後'], 'edited.json')
        data = json.loads(path.read_text('utf8'))
        data['segments'][0].update(start=10.5, end=11.25, separated_audio='.audio/separated/voice.wav',
                                   separated_offset=10)
        path.write_text(json.dumps(data), 'utf8')
        track = path.parent / '.audio/separated/voice.wav'
        track.parent.mkdir(parents=True)
        track.touch()
        mix = path.parent / '.audio/source.flac'
        mix.touch()
        record = load_records(self.root)[0][0]
        self.assertEqual(record_playback_target(self.root, record, 0), (track.resolve(), (.5, 1.25)))
        track.unlink()
        self.assertEqual(record_playback_target(self.root, record, 0), (mix, (10.5, 11.25)))

    def test_comparison_shared_playback_and_missing_media(self):
        path = self.save('comparison/granite', ['京都'])
        session = path.parent.parent
        (session / 'comparison.json').write_text('{}', 'utf8')
        shared = session / '.audio/source.flac'
        shared.parent.mkdir()
        shared.touch()
        record = load_records(self.root)[0][0]
        self.assertEqual(record_playback_target(self.root, record, 0), (shared, (0, 1)))
        shared.unlink()
        with self.assertRaises(FileNotFoundError):
            record_playback_target(self.root, record, 0)
        self.assertEqual(record.lines[0].text, '京都')

    def test_synonym_order_and_deduplication_across_recordings(self):
        self.save('20261021_000000_a', ['自動車'])
        self.save('20261020_000000_b', ['車と乗用車を調べた。', '車も自動車も扱う。'])
        self.save('20261019_000000_c', ['自動車。'])
        records, _ = load_records(self.root)
        indexes = {i: index_nouns(r, self.tokenizer) for i, r in enumerate(records)}
        hits = search_nouns(records, indexes, 0, '自動車', include_near=True, synsets=load_synsets())
        self.assertEqual([(h.record, h.row, h.reason) for h in hits],
                         [(1, 1, '完全一致'), (2, 0, '完全一致'), (1, 0, '類義語')])
        self.assertIn(hits[-1].key, {'車', '乗用車'})
        self.assertEqual(len(search_nouns(records, indexes, 0, '自動車')), 2)

    def test_spelling_reading_and_distinct_homophones(self):
        self.save('20261021_000000_a', [])
        self.save('20261020_000000_b', ['コンピュータを使う。', '林檎を食べる。', '政党の議論。', 'ＡＩで処理する。'])
        records, _ = load_records(self.root)
        indexes = {i: index_nouns(r, self.tokenizer) for i, r in enumerate(records)}
        def query(term):
            return [(h.row, h.reason) for h in search_nouns(records, indexes, 0, term,
                    include_near=True, synsets=load_synsets())]
        self.assertEqual(query('こんぴゅーたー'), [(0, '表記違い')])
        self.assertEqual(query('りんご'), [(1, '同じ読み')])
        self.assertEqual(query('正当'), [])
        self.assertEqual(query('人工知能'), [(3, '類義語')])
        self.assertEqual(query('  ai  '), [(3, '完全一致')])
        self.assertEqual(query('  '), [])

    def test_live_corrections_are_used_in_body_and_index(self):
        path = self.save('live', ['東京を訪問した。'], mode='realtime')
        path.with_name('live_speaker_edits.json').write_text(json.dumps({
            'names': {'speaker_0': '田中'}, 'texts': {'0': {'text': '京都を訪問した。'}}
        }), 'utf8')
        records, skipped = load_records(self.root)
        self.assertEqual(skipped, 0)
        self.assertEqual(records[0].lines[0].speaker, '田中')
        index = index_nouns(records[0], self.tokenizer)
        self.assertIn('京都', index.labels)
        self.assertNotIn('東京', index.labels)

    def test_original_fallback_and_skipped_corrupt_record(self):
        good = self.save('good', ['京都で会議をした。'])
        edited = good.with_name('edited.json')
        edited.write_text('{', 'utf8')
        os.utime(edited, ns=(good.stat().st_mtime_ns + 10**9,) * 2)
        invalid = self.save('broken', [])
        invalid.write_text('{"segments":[3]}', 'utf8')
        (good.parent / 'before_corrections.json').write_text('{}', 'utf8')
        records, skipped = load_records(self.root)
        self.assertEqual([r.path for r in records], [good])
        self.assertEqual(skipped, 2)

    def test_comparison_variants_are_browsable_but_not_independent_sources(self):
        base = '20260912_164000_モデル比較_123'
        self.save(base + '/whisper', ['東京大学'])
        self.save(base + '/granite', ['東京大学'])
        self.save(base + '/merged_20260912_184100_abcd', ['東京大学'])
        (self.root / base / 'comparison.json').write_text('{}', 'utf8')
        self.save(base + '/.audio', ['録音'])
        self.save('20250101_000000_file', ['東京大学に行った。'])
        records, skipped = load_records(self.root)
        self.assertEqual((len(records), skipped), (4, 0))
        indexes = {i: index_nouns(r, self.tokenizer) for i, r in enumerate(records)}
        self.assertEqual(list(related_rows(records, indexes, 0, '東京大学')), [(3, 0)])

    def test_missing_directory_and_external_link(self):
        self.assertEqual(load_records(self.root), ([], 0))
        other = self.root.parent / 'external'
        other.mkdir()
        self.root.mkdir()
        try:
            (self.root / 'link').symlink_to(other, target_is_directory=True)
        except OSError:
            self.skipTest('Symbolic links unavailable')
        (other / 'result.json').write_text('{"segments":[]}', 'utf8')
        self.assertEqual(load_records(self.root), ([], 0))
