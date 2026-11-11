from contextlib import closing
from datetime import date
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from janome.tokenizer import Tokenizer
from research_library import (Passage, ResearchIndex, Source, collect_sources, import_text, question_dates,
                              save_article, summarize)
from transcript_library import load_records


class ResearchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tokenizer = Tokenizer()

    def test_search_across_years_types_and_speakers(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp) / 'transcripts'
            folder = root / '20240405_090000_a'
            folder.mkdir(parents=True)
            data = {'title': '春の会議', 'speakers': {'a': '田中'},
                    'segments': [{'start': 13, 'speaker': 'a', 'text': '予算の上限は100万円です。'}]}
            (folder / 'result.json').write_text(json.dumps(data), 'utf-8')
            article = save_article(root, '予算改定', '2025-02-01', '広報', '改定します。\n予算の上限を150万円にしました。')
            undated = save_article(root, '覚書', '', '', '予算について調査中。')
            records, _ = load_records(root)
            catalog, skipped = collect_sources(root, records)
            self.assertEqual((len(catalog), skipped), (3, 0))
            record = records[0].path.relative_to(root).as_posix()
            with closing(sqlite3.connect(':memory:')) as db:
                index = ResearchIndex(db, self.tokenizer)
                index.sync(catalog)
                hits, partial, _ = index.search('以前、予算の上限について何と言っていた？', order='古い順')
                self.assertFalse(partial)
                self.assertEqual(hits, [(record, 0), (article, 1)])
                self.assertEqual(index.search('昨年、田中さんは予算について何と言っていた？',
                                              today=date(2025, 7, 1))[0], [(record, 0)])
                self.assertEqual(index.search('2025年2月、予算の上限は？', kind='記事')[0], [(article, 1)])
                self.assertEqual(index.search('広報では上限について何と言っていた？')[0], [(article, 1)])
                self.assertEqual(index.search('2024年までの予算')[0], [(record, 0)])
                self.assertNotIn((undated, 0), index.search('2024年から2026年の予算')[0])
                self.assertEqual(index.search('予算', kind='発言')[0], [(record, 0)])
                self.assertFalse(index.search('深海潜水艦')[0])
                self.assertTrue(index.search('予算 宇宙船')[1])
                self.assertTrue(index.search('予算', limit=1)[2])
                self.assertEqual(catalog[record].passages[0], Passage(data['segments'][0]['text'], '田中', 13))
                self.assertEqual(catalog[article].credit, '広報')

    def test_cache_reuse_edits_and_removal(self):
        src = Source('a', '発言', '会議', '2024-12-01', '', (Passage('医者に相談します。', '佐藤', 1.),))
        with tempfile.TemporaryDirectory() as temp:
            filename = Path(temp) / 'cache.db'
            with closing(sqlite3.connect(filename)) as db:
                ResearchIndex(db, self.tokenizer).sync({'a': src})
            with closing(sqlite3.connect(filename)) as db:
                index = ResearchIndex(db, self.tokenizer)
                with patch('research_library.terms', side_effect=AssertionError('unchanged source reindexed')):
                    index.sync({'a': src})
                self.assertEqual(index.search('医師')[0], [('a', 0)])
                edited = Source('a', '発言', '会議', '2024-12-01', '', (Passage('返事は保留します。', '伊藤', 2.),))
                index.sync({'a': edited})
                self.assertFalse(index.search('医者')[0])
                self.assertEqual(index.search('保留')[0], [('a', 0)])
                index.sync({})
                self.assertFalse(index.search('保留')[0])

    def test_article_import_and_calendar_bounds(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / '記事.md'
            path.write_bytes('費用を見直す。'.encode('cp932'))
            self.assertEqual(import_text(path), '費用を見直す。')
            with self.assertRaises(ValueError):
                save_article(root, '記事', '2025-02-30', '', '本文')
            with self.assertRaises(ValueError):
                save_article(root, '記事', '', '', '  ')
            key = save_article(root, '記事', '', '', '一' * 800)
            self.assertEqual(''.join(p.text for p in collect_sources(root, [])[0][key].passages), '一' * 800)
            (root / key).write_bytes(b'{')
            self.assertEqual(collect_sources(root, []), ({}, 1))
        today = date(2026, 4, 1)
        self.assertEqual(question_dates('2024年2月', today)[1], [('2024-02-01', '2024-02-29')])
        self.assertEqual(question_dates('一昨年から今年まで', today)[1], [('2024-01-01', '2026-12-31')])

    def test_ai_only_displays_locally_verifiable_citations(self):
        items = [{'citations': [1], 'answer': '100万円を予定。'},
                 {'citations': [1, 2], 'answer': '存在しない資料を参照。'}]
        with patch('context_ai.ensure_model'), patch('context_ai.local_request',
                return_value={'message': {'content': json.dumps({'items': items})}}):
            answer = summarize('予算は？', [{'id': 1, 'text': '予算は100万円。', 'date': '2024-03-01',
                                           'title': '会議', 'location': '佐藤'}])
        self.assertEqual(answer, '100万円を予定。 [1]\n\n引用元\n[1] 2024-03-01　会議　佐藤\n「予算は100万円。」')
