"""Local spelling, reading and direct Japanese WordNet synonym lookups."""
from collections import defaultdict
from functools import lru_cache
from pathlib import Path
import re
import unicodedata


def noun_key(text):
    return unicodedata.normalize('NFKC', text).casefold()


_KATAKANA = str.maketrans({i: i + 0x60 for i in range(0x3041, 0x3097)})


def spelling(text):
    return noun_key(text).translate(_KATAKANA)


def kana(text):
    return bool(re.fullmatch('[ァ-ヺー]+', text))


def spelling_variant(text):
    form = spelling(text)
    # Common loanword endings such as コンピュータ(ー) and サーバ(ー).
    if len(form) >= 4 and kana(form) and form.endswith(('ター', 'ダー', 'バー', 'パー', 'ザー', 'サー', 'ラー', 'ナー')):
        return form[:-1]
    return form


def token_reading(token):
    value = spelling(token.reading if token.reading != '*' else token.surface)
    return value if kana(value) else ''


@lru_cache(maxsize=1)
def query_tokenizer():
    # Used by interactive queries only. The indexing worker owns its own tokenizer.
    from janome.tokenizer import Tokenizer
    return Tokenizer()


@lru_cache(maxsize=256)
def query_reading(key):
    form = spelling(key)
    if kana(form):
        return form
    if not any(is_kanji(c) for c in form):
        return ''
    parts = [token_reading(t) for t in query_tokenizer().tokenize(key)]
    return ''.join(parts) if all(parts) else ''


def is_kanji(c):
    return '\u3400' <= c <= '\u9fff' or c == '々' or '\U00020000' <= c <= '\U000323af'


def same_written_stem(a, b):
    stem = ''.join(c for c in a if is_kanji(c))
    return bool(stem) and stem == ''.join(c for c in b if is_kanji(c))


@lru_cache(maxsize=1)
def load_synsets():
    path = Path(__file__).parent / 'docs/dictionaries/wnja-noun-synsets.txt'
    entries = defaultdict(list)
    with path.open(encoding='utf-8') as stream:
        for line in stream:
            if line.startswith('#'):
                continue
            _, *lemmas = line.rstrip('\n').split('\t')
            group = frozenset(spelling(word) for word in lemmas)
            if len(group) > 1:
                for word in group:
                    entries[word].append(group)
    return dict(entries)


def synonyms(key, synsets):
    # Preserve sense boundaries: a synonym's other meanings are not related to the query.
    return set().union(*synsets.get(spelling(key), ()))
