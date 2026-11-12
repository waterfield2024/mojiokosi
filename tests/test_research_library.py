from contextlib import closing
from datetime import date
import importlib.util
import json
from pathlib import Path
import queue
import sqlite3
import sys
import tempfile
import threading
import unittest
from unittest.mock import MagicMock, patch

import numpy as np
from janome.tokenizer import Tokenizer
from research_library import (Passage, ResearchIndex, ResearchSession, Source, collect_sources, import_text, question_dates,
                              save_article, summarize)
from transcript_library import load_records
from research_vectors import DIMENSION


class ResearchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tokenizer = Tokenizer()

    def test_search_worker_skips_optional_sentencepiece_even_when_installed(self):
        from research_vectors import _serve
        connection = MagicMock()
        connection.recv.side_effect = EOFError
        with tempfile.TemporaryDirectory() as temp, patch.dict(sys.modules), patch.object(sys, 'path', [temp, *sys.path]):
            sys.modules.pop('sentencepiece', None)
            (Path(temp) / 'sentencepiece.py').write_text('raise RuntimeError("unexpected native import")', 'utf-8')
            self.assertIsNotNone(importlib.util.find_spec('sentencepiece'))

            def load_model(folder):
                if importlib.util.find_spec('sentencepiece') is not None:
                    __import__('sentencepiece')

            with patch('research_vectors.JapaneseEncoder', side_effect=load_model):
                _serve(connection, Path(temp))
            connection.send.assert_called_once_with((True, None))

    def test_portable_research_session_and_invalid_preferences(self):
        with tempfile.TemporaryDirectory() as temp, patch.dict('os.environ', {'WHISPER_USER_DATA_DIR': temp}):
            path = Path(temp) / 'research_settings.json'
            self.assertEqual(ResearchSession.load(), ResearchSession())
            saved = ResearchSession('過去の会議', '記事', '古い順', False, 'キーワード一致',
                                    {'キーワード一致': 3, 'すべて': 2})
            saved.save()
            self.assertEqual(ResearchSession.load(), saved)
            data = json.loads(path.read_text('utf-8'))
            data.update(order=[], hybrid='off', reason='両方（7件）',
                        pages={'すべて': 2, 'キーワード一致': 0, '両方': True, '余分': 3})
            path.write_text(json.dumps(data), 'utf-8')
            self.assertEqual(ResearchSession.load(), ResearchSession('過去の会議', '記事', pages={'すべて': 2}))
            for invalid in (b'[]', b'{', b'\xff'):
                path.write_bytes(invalid)
                self.assertEqual(ResearchSession.load(), ResearchSession())

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
                self.assertEqual(index.search_with_reasons('医師')[0], [('a', 0, 'キーワード一致')])
                edited = Source('a', '発言', '会議', '2024-12-01', '', (Passage('返事は保留します。', '伊藤', 2.),))
                index.sync({'a': edited})
                self.assertFalse(index.search('医者')[0])
                self.assertEqual(index.search('保留')[0], [('a', 0)])
                index.sync({})
                self.assertFalse(index.search('保留')[0])

    def test_change_question_finds_before_and_after_statements(self):
        lines = ('予算の上限は100万円です。', '予算の上限が変わりました。', '予算の上限は150万円です。')
        docs = {str(i): Source(str(i), '記事', 'お知らせ', f'202{i}-03-01', '', (Passage(line),))
                for i, line in enumerate(lines)}
        with closing(sqlite3.connect(':memory:')) as db:
            index = ResearchIndex(db, self.tokenizer)
            index.sync(docs)
            hits, partial, _ = index.search('予算の上限はどう変わりましたか？', order='古い順')
        self.assertEqual(hits, [('0', 0), ('1', 0), ('2', 0)])
        self.assertFalse(partial)

    def test_hybrid_independent_candidates_filters_and_persistent_updates(self):
        first = '家計への負担を考え、当面は今の価格を維持します。'
        second = '当面、料金を据え置く方針です。'
        literal = '値上げを見送った理由を説明します。'

        class Encoder:
            def encode(self, texts, *, query=False):
                result = np.zeros((len(texts), DIMENSION), dtype='<f4')
                for i, text in enumerate(texts):
                    result[i, 0 if query or text in (first, second) else 1] = 1
                return result

        docs = {
            'a': Source('a', '記事', 'お知らせ', '2024-01-02', '', (Passage('桜が咲いた。'), Passage(first))),
            'b': Source('b', '発言', '会議', '2025-01-03', '', (Passage(second, '佐藤', 15),)),
            'c': Source('c', '発言', '会議', '', '', (Passage(literal, '田中', 42),)),
        }
        encoder = Encoder()
        query = '値上げを見送った理由は？'
        with tempfile.TemporaryDirectory() as temp:
            filename = Path(temp) / 'cache.db'
            with closing(sqlite3.connect(filename)) as db:
                index = ResearchIndex(db, self.tokenizer)
                index.sync(docs)
                self.assertEqual(index.search(query)[0], [('c', 0)])
                self.assertEqual(list(index.vector_batches(docs, encoder, batch_size=1)), [1]*4)
                self.assertCountEqual(index.search(query, encoder=encoder)[0], [('a', 1), ('b', 0), ('c', 0)])
                self.assertEqual(index.search(query, kind='記事', encoder=encoder)[0], [('a', 1)])
                self.assertEqual(index.search('2025年の' + query, encoder=encoder)[0], [('b', 0)])
                self.assertEqual(index.search(query, order='新しい順', limit=2, encoder=encoder),
                                 ([('b', 0), ('a', 1)], False, True))
                with patch.object(encoder, 'encode', side_effect=AssertionError('date-only query encoded')):
                    self.assertEqual(index.search('2024年', encoder=encoder)[0], [('a', 0), ('a', 1)])
            with closing(sqlite3.connect(filename)) as db:
                index = ResearchIndex(db, self.tokenizer)
                index.sync(docs)
                self.assertEqual(list(index.vector_batches(docs, encoder)), [])
                del docs['b']
                docs['a'] = Source('a', '記事', 'お知らせ', '', '', (Passage('桜が散った。'),))
                index.sync(docs)
                self.assertEqual(list(index.vector_batches(docs, encoder)), [1])
                self.assertEqual(index.search(query, encoder=encoder)[0], [('c', 0)])
                db.execute("UPDATE passage_vectors SET version='old'")
                self.assertEqual(list(index.vector_batches(docs, encoder)), [2])

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

    def test_match_reasons_follow_both_searches_even_below_lexical_cutoff(self):
        passages = ('予算', '予算', '予算', '予算を来年度へ繰り越す予定だ。', '当面は今の金額を維持します。')
        source = Source('a', '発言', '記録', '2025-02-01', '', tuple(Passage(text) for text in passages))
        docs = {'a': source}

        class Encoder:
            def encode(self, texts, *, query=False):
                values = np.zeros((len(texts), DIMENSION), dtype='<f4')
                for i, text in enumerate(texts):
                    values[i, 0 if query or text in passages[3:] else 1] = 1
                return values

        with closing(sqlite3.connect(':memory:')) as db:
            index = ResearchIndex(db, self.tokenizer)
            index.sync(docs)
            encoder = Encoder()
            list(index.vector_batches(docs, encoder))
            self.assertEqual(index.search('予算')[0][-1], ('a', 3))
            hits, _, _ = index.search_with_reasons('予算', encoder=encoder, order='古い順')
            self.assertEqual([reason for _, _, reason in hits],
                             ['キーワード一致'] * 3 + ['両方', '意味が近い'])
            self.assertEqual(index.search_with_reasons('予算', encoder=encoder, limit=2),
                             ([('a', 0, 'キーワード一致'), ('a', 3, '両方')], False, True))
            self.assertEqual(index.search_with_reasons('予算 宇宙船', limit=1),
                             ([('a', 0, 'キーワード一致')], True, True))
            self.assertEqual(index.search_with_reasons('2025年', encoder=encoder, limit=1)[0],
                             [('a', 0, '日付一致')])
            pages = index.search_pages('予算', encoder=encoder, limit=2)
            self.assertEqual(pages['キーワード一致'], ([('a', i, 'キーワード一致') for i in (0, 1)], False, True))
            self.assertEqual(pages['意味が近い'], ([('a', 4, '意味が近い')], False, False))
            self.assertEqual(pages['両方'], ([('a', 3, '両方')], False, False))
            dates = index.search_pages('2025年', encoder=encoder, limit=1)
            self.assertTrue(all(page == ([], False, False) for reason, page in dates.items() if reason != 'すべて'))

    def test_reason_filters_reach_past_the_first_hundred_passages(self):
        lines = [Passage('予算')] * 105 + [Passage('予算を別途検討します。'), Passage('資金を調達します。')]
        source = Source('a', '記事', '資料', '2025-02-01', '', tuple(lines))
        with closing(sqlite3.connect(':memory:')) as db:
            index = ResearchIndex(db, self.tokenizer)
            index.sync({'a': source})
            semantic = db.execute('SELECT source,ordinal,?,id FROM parts WHERE ordinal != 105 ORDER BY ordinal',
                                  (source.day,)).fetchall()
            with patch.object(index, 'semantic_hits', return_value=semantic) as search:
                pages = index.search_pages('予算', kind='記事', order='古い順', encoder=object())
                search.assert_called_once()
            self.assertEqual(pages['すべて'], ([('a', i, '両方') for i in range(100)], False, True))
            self.assertEqual(pages['両方'], pages['すべて'])
            self.assertEqual(pages['キーワード一致'], ([('a', 105, 'キーワード一致')], False, False))
            self.assertEqual(pages['意味が近い'], ([('a', 106, '意味が近い')], False, False))
            # A semantic match can also be lexical even beyond the extra FTS rows.
            with patch.object(index, 'semantic_hits', return_value=semantic[-2:]):
                pages = index.search_pages('予算', kind='記事', order='古い順', limit=2, encoder=object())
            self.assertEqual(pages['両方'], ([('a', 104, '両方')], False, False))
            self.assertEqual(pages['意味が近い'], ([('a', 106, '意味が近い')], False, False))

    def test_reason_counts_include_all_matches_within_kind_and_period(self):
        rows = (Passage('予算を検討します。'),) * 151 + (Passage('料金を据え置きます。'),) * 121
        sources = {key: Source(key, kind, '資料', day, '', rows) for key, kind, day in (
            ('a', '記事', '2025-01-01'), ('b', '記事', '2024-01-01'), ('c', '発言', '2025-01-01'))}
        with closing(sqlite3.connect(':memory:')) as db:
            index = ResearchIndex(db, self.tokenizer)
            index.sync(sources)
            # Only row 150 matches both, below the lexical fetch window with limit=2.
            semantic = db.execute(
                "SELECT source,ordinal,'2025-01-01',id FROM parts WHERE source='a' AND ordinal>=150"
            ).fetchall()
            with patch.object(index, 'semantic_hits', return_value=semantic) as vector_search:
                pages = index.search_pages_with_counts('2025年の予算', '記事', limit=2, encoder=object())
                vector_search.assert_called_once()
            self.assertEqual({reason: page.total for reason, page in pages.items()},
                             {'すべて': 272, 'キーワード一致': 150, '意味が近い': 121, '両方': 1})
            self.assertTrue(all(len(page.hits) <= 2 for page in pages.values()))
            partial = index.search_pages_with_counts('2025年の予算 宇宙船', '記事', limit=2)
            self.assertTrue(partial['すべて'].partial)
            self.assertEqual(partial['キーワード一致'].total, 151)
            dates = index.search_pages_with_counts('2025年', '記事', limit=2)
            self.assertEqual({reason: page.total for reason, page in dates.items()},
                             {'すべて': 272, 'キーワード一致': 0, '意味が近い': 0, '両方': 0})

    def test_every_page_preserves_first_page_order_counts_and_filtered_membership(self):
        texts = ('予算', '資金を調達します。', '予算を検討します。', '明日は晴れます。')
        lines = tuple(Passage(text) for _ in range(103) for text in texts)
        sources = {k: Source(k, '記事', '資料', day, '', lines) for k, day in (
            ('a', '2025-02-01'), ('b', '2025-01-01'))}
        with closing(sqlite3.connect(':memory:')) as db:
            index = ResearchIndex(db, self.tokenizer)
            index.sync(sources)
            semantic = db.execute('SELECT p.source,p.ordinal,s.day,p.id FROM parts p JOIN sources s ON s.key=p.source '
                                  'WHERE p.ordinal % 4 IN (1,2) ORDER BY p.id DESC').fetchall()
            with patch.object(index, 'semantic_hits', return_value=semantic):
                for order in ('関連度順', '古い順', '新しい順'):
                    with self.subTest(order=order):
                        first = index.search_pages_with_counts('2025年の予算', '記事', order, encoder=object())
                        pages = index.search_pages_with_counts('2025年の予算', '記事', order, encoder=object(), limit=None)
                        all_rows = pages['すべて'].hits
                        self.assertEqual(len({(k, row) for k, row, reason in all_rows}), 618)
                        for reason, page in pages.items():
                            self.assertEqual(page.hits[:100], first[reason].hits)
                            self.assertEqual(len(page.hits), page.total)
                            if reason != 'すべて':
                                self.assertEqual(page.hits, [r for r in all_rows if r[2] == reason])
                                self.assertEqual(len(page.hits[200:]), 6)
                        if order != '関連度順':
                            days = [sources[key].day for key, row, reason in all_rows]
                            self.assertEqual(days, sorted(days, reverse=order == '新しい順'))

    def test_full_text_available_while_model_loads_and_after_failure(self):
        from research_ui import index_worker
        requests, events = queue.Queue(), queue.Queue()
        stop, release = threading.Event(), threading.Event()

        def offline(stopped):
            release.wait(10)
            raise OSError('offline')

        def wait_for(kind):
            while True:
                action, _, value = events.get(timeout=10)
                self.assertNotIn(action, ('error', 'unavailable'), value)
                if action == kind:
                    return value

        with tempfile.TemporaryDirectory() as temp, patch('research_ui.load_encoder', side_effect=offline):
            root = Path(temp) / 'transcripts'
            key = save_article(root, 'お知らせ', '', '', '予算について相談します。')
            worker = threading.Thread(target=index_worker, args=(root, ([], 0), requests, events, stop))
            worker.start()
            try:
                requests.put(('refresh', 1, ()))
                wait_for('ready')
                query = ('予算', '記事', '関連度順', True)
                requests.put(('search', 2, query))
                pages = wait_for('lexical_hits')
                self.assertEqual(pages['キーワード一致'].hits, [(key, 0, 'キーワード一致')])
                self.assertEqual(pages['キーワード一致'].total, 1)
                self.assertEqual(pages['両方'].total, 0)
                release.set()
                self.assertIn('全文検索を使えます', wait_for('vector_status'))
                requests.put(('search', 3, query))
                self.assertEqual(wait_for('lexical_hits')['すべて'].hits, [(key, 0, 'キーワード一致')])
            finally:
                stop.set()
                release.set()
                requests.put(('close', 0, ()))
                worker.join(10)
                self.assertFalse(worker.is_alive())

    def test_ai_only_displays_locally_verifiable_citations(self):
        items = [{'citations': [1], 'answer': '100万円を予定。'},
                 {'citations': [1, 2], 'answer': '存在しない資料を参照。'}]
        with patch('context_ai.ensure_model'), patch('context_ai.local_request',
                return_value={'message': {'content': json.dumps({'items': items})}}):
            answer = summarize('予算は？', [{'id': 1, 'text': '予算は100万円。', 'date': '2024-03-01',
                                           'title': '会議', 'location': '佐藤'}])
        self.assertEqual(answer, '100万円を予定。 [1]\n\n引用元\n[1] 2024-03-01　会議　佐藤\n「予算は100万円。」')
