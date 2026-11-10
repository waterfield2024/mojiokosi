"""Read portable transcript records and match Japanese nouns between sessions."""
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
import json
import math
from pathlib import Path
import re
import stat
import unicodedata

from result_media import result_file
from speakers import speaker_name


@dataclass(frozen=True)
class Utterance:
    start: float
    speaker: str
    text: str


@dataclass(frozen=True)
class Record:
    path: Path
    session: Path
    date: datetime
    title: str
    lines: tuple


def plain_directory(path):
    info = path.lstat()
    return stat.S_ISDIR(info.st_mode) and not getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT


def record_date(folder, original, chosen):
    for name in (folder.name, folder.parent.name):
        match = re.match(r'^(?:リアルタイム_|merged_)?(\d{8}_\d{6})(?:_|$)', name)
        if match:
            try:
                return datetime.strptime(match[1], '%Y%m%d_%H%M%S')
            except ValueError:
                pass
    return datetime.fromtimestamp((original or chosen).stat().st_mtime)


def read_transcript(path):
    path = Path(path)
    data = json.loads(path.read_text('utf-8-sig'))
    if not isinstance(data, dict) or not isinstance(data.get('segments'), list):
        raise ValueError('このアプリの結果ファイルではありません。')
    if (data.get('mode') == 'realtime' and path.name in {'result.json', 'edited.json'}
            and data.get('diarization', {}).get('engine') in (None, 'sherpa-onnx/streaming')):
        from live_speakers import apply_live_edits
        apply_live_edits(data, path.parent)
    return data


def read_record(root, folder, session):
    relative = folder.relative_to(root)
    original = result_file(root, relative / 'result.json', {'.json'})
    edited = result_file(root, relative / 'edited.json', {'.json'})
    candidates = sorted((p for p in (edited, original) if p),
                        key=lambda p: p.stat().st_mtime_ns, reverse=True)
    fallback = None
    failed = False
    for path in candidates:
        try:
            data = read_transcript(path)
            names = data.get('speakers') or {}
            lines = []
            for row in data['segments']:
                start = float(row['start'])
                if not math.isfinite(start * 1000) or not isinstance(row['text'], str):
                    raise ValueError('発言の形式が正しくありません。')
                lines.append(Utterance(max(0., start), speaker_name(row.get('speaker'), names),
                                       ' '.join(row['text'].splitlines())))
            title = str(data.get('title') or session.name)
            if folder != session and folder.name in ('whisper', 'granite'):
                title += f'（{folder.name.title()}）'
            record = Record(path, session, record_date(folder, original, path), title, tuple(lines))
            # The editor can save a live checkpoint just as the worker finishes writing the full recording.
            if path == edited and original and data.get('mode') == 'realtime' and data.get('live_incomplete'):
                fallback = record
                continue
            if fallback and data.get('live_incomplete'):
                return fallback, failed
            return record, failed
        except (OSError, ValueError, KeyError, TypeError, AttributeError, OverflowError, RecursionError):
            failed = True
    return fallback, failed


def load_records(root, cancelled=lambda: False):
    root = Path(root)
    records = []
    skipped = 0
    try:
        if not plain_directory(root):
            raise ValueError('記録の保存先を読み込めません。')
    except FileNotFoundError:
        return [], 0
    for session in root.iterdir():
        if cancelled():
            break
        try:
            if not plain_directory(session):
                continue
            folders = [session]
            if result_file(root, session.relative_to(root) / 'comparison.json', {'.json'}):
                folders += [p for p in session.iterdir()
                            if (p.name in ('whisper', 'granite') or p.name.startswith('merged_')) and plain_directory(p)]
            for folder in folders:
                record, failed = read_record(root, folder, session)
                if record is not None:
                    records.append(record)
                skipped += int(failed)
        except OSError:
            skipped += 1
    return sorted(records, key=lambda r: (r.date, str(r.path)), reverse=True), skipped


def noun_key(text):
    return unicodedata.normalize('NFKC', text).casefold()


# Normalize full-width Latin letters before tokenization without shifting character offsets.
_WIDTH = str.maketrans({code: code - 0xFEE0 for code in range(0xFF01, 0xFF5F)})
_NOUN_TYPES = {'固有名詞', '一般', 'サ変接続', '形容動詞語幹', '副詞可能', 'ナイ形容詞語幹'}


def noun_spans(text, tokenizer):
    source = text.translate(_WIDTH)
    starts = []
    end = 0
    for token in tokenizer.tokenize(source):
        start = source.find(token.surface, end)
        if start < 0:
            starts.clear()
            continue
        if start != end:
            starts.clear()
        end = start + len(token.surface)
        pos, subtype, *_ = token.part_of_speech.split(',')
        if pos == '名詞' and subtype in _NOUN_TYPES and any(c.isalpha() for c in token.surface):
            starts = [*starts[-3:], start]
        elif pos != '名詞' or subtype != '接尾' or not starts:
            starts.clear()
            continue
        for first in starts:
            yield first, end, text[first:end]


@dataclass
class NounIndex:
    labels: dict
    rows: dict
    spans: list

    def at(self, row, offset, shared=()):
        matches = [(key in shared, end - start, key) for start, end, key in self.spans[row] if start <= offset < end]
        return max(matches)[2] if matches else None


def index_nouns(record, tokenizer, cancelled=lambda: False):
    labels, rows, spans = {}, defaultdict(list), []
    for number, line in enumerate(record.lines):
        if cancelled():
            return None
        found = []
        for start, end, surface in noun_spans(line.text, tokenizer):
            key = noun_key(surface)
            labels.setdefault(key, surface)
            if not rows[key] or rows[key][-1] != number:
                rows[key].append(number)
            found.append((start, end, key))
        spans.append(found)
    return NounIndex(labels, dict(rows), spans)


def related_rows(records, indexes, selected, key):
    for number, record in enumerate(records):
        if record.session != records[selected].session and number in indexes:
            for row in indexes[number].rows.get(key, ()):
                yield number, row
