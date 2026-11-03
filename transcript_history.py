"""Bounded views and literal search over the currently opened transcript."""
from dataclasses import dataclass
import math
import re
from speakers import speaker_name

LIMITS = (50, 100, 200)


def preferences(data=None):
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ValueError('履歴表示の設定が正しくありません。')
    visible, limit, side = data.get('visible', True), data.get('limit', 100), data.get('side', 'right')
    if (type(visible) is not bool or type(limit) is not int or limit not in LIMITS
            or not isinstance(side, str) or side not in {'left', 'right'}):
        raise ValueError('履歴表示の設定が正しくありません。')
    return {'visible': visible, 'limit': limit, 'side': side}


def pattern(query):
    value = query.strip()
    return re.compile(re.escape(value), re.IGNORECASE) if value else None


def match_spans(text, query):
    expression = pattern(query)
    return [(m.start(), m.end()) for m in expression.finditer(text)] if expression else []


@dataclass(frozen=True)
class Entry:
    key: str
    index: int
    name: str
    time: str
    text: str


@dataclass
class Page:
    entries: list
    matches: list
    start: int
    end: int
    total: int

    @property
    def older(self):
        return self.start > 0

    @property
    def newer(self):
        return self.end < len(self.matches)

    def adjacent_anchor(self, limit, older):
        end = self.start if older else min(len(self.matches), self.end + limit)
        return self.matches[end - 1].key if end else None


def select_page(segments, names, query='', limit=100, end_key=None):
    if limit not in LIMITS:
        raise ValueError('表示件数は50・100・200件から選んでください。')
    expression, matches, total = pattern(query), [], 0
    for index, row in enumerate(segments):
        text = str(row.get('text', '')).strip()
        if not text:
            continue
        total += 1
        name = speaker_name(row.get('speaker'), names) or '未設定'
        if row.get('speaker_review'):
            name += ' ？'
        if expression and not (expression.search(text) or expression.search(name)):
            continue
        seconds = float(row.get('start', 0))
        seconds = max(0, int(seconds)) if math.isfinite(seconds) else 0
        minute, second = divmod(seconds, 60)
        hour, minute = divmod(minute, 60)
        stamp = f'{hour:02}:{minute:02}:{second:02}'
        matches.append(Entry(str(row.get('segment_id') or row.get('id') or f'row-{index}'),
                             index, name, stamp, text))
    end = next((i + 1 for i, item in enumerate(matches) if item.key == end_key), len(matches))
    start = max(0, end - limit)
    return Page(matches[start:end], matches, start, end, total)
