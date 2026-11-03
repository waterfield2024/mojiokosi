"""Display-only transcript differences; timestamps, names and whitespace are excluded."""
from collections import Counter
from difflib import SequenceMatcher
import re
import unicodedata


def _pair_count(a, b):
    left, right = Counter(a), Counter(b)
    return sum(count * right.get(value, 0) for value, count in left.items())


def _changes(a, b, offset_a=0, offset_b=0):
    # Trim common edges before matching: even very long, repetitive transcripts
    # with a local change then need only a small comparison.
    prefix = 0
    limit = min(len(a), len(b))
    while prefix < limit and a[prefix] == b[prefix]:prefix += 1
    a, b = a[prefix:], b[prefix:]
    offset_a += prefix;offset_b += prefix
    suffix = 0
    limit = min(len(a), len(b))
    # For ordinary passages let the matcher place trailing punctuation: blindly
    # trimming a shared final '。' can attach a deleted sentence's punctuation to
    # the preceding, otherwise identical sentence.
    if max(len(a),len(b)) > 4000 or _pair_count(a,b) > 250_000:
        while suffix < limit and a[-suffix-1] == b[-suffix-1]:suffix += 1
    if suffix:a, b = a[:-suffix], b[:-suffix]
    if not a or not b:
        if a or b:yield offset_a, offset_a+len(a), offset_b, offset_b+len(b)
        return
    costly = _pair_count(a, b) > 250_000 or max(len(a), len(b)) > 4000
    if costly:
        # Use whole-sentence anchors before character refinement. Bound token
        # lengths and suppress very frequent anchors to avoid quadratic UI stalls.
        chunks_a = list(re.finditer(r"[^。！？.!?]{1,96}[。！？.!?]?|[。！？.!?]", a))
        chunks_b = list(re.finditer(r"[^。！？.!?]{1,96}[。！？.!?]?|[。！？.!?]", b))
        tokens_a = [m.group() for m in chunks_a];tokens_b = [m.group() for m in chunks_b]
        matcher = SequenceMatcher(None, tokens_a, tokens_b, autojunk=_pair_count(tokens_a, tokens_b)>250_000)
        opcodes = matcher.get_opcodes()
        if any(tag == "equal" for tag, *_ in opcodes):
            for tag, i, j, k, l in opcodes:
                if tag == "equal":continue
                start_a = chunks_a[i].start() if i < len(chunks_a) else len(a)
                end_a = chunks_a[j-1].end() if j > i else start_a
                start_b = chunks_b[k].start() if k < len(chunks_b) else len(b)
                end_b = chunks_b[l-1].end() if l > k else start_b
                yield from _changes(a[start_a:end_a], b[start_b:end_b], offset_a+start_a, offset_b+start_b)
            return
    # The bounded fallback is only needed for large passages without sentence
    # anchors. Small passages always retain frequent Japanese characters.
    for tag, i, j, k, l in SequenceMatcher(None, a, b, autojunk=costly).get_opcodes():
        if tag != "equal":yield offset_a+i, offset_a+j, offset_b+k, offset_b+l


def _characters(segments):
    characters = [];locations = [];texts = []
    for index, segment in enumerate(segments):
        value = str(segment.get("text", "")).strip();texts.append(value)
        for position, char in enumerate(value):
            if not char.isspace():characters.append(char);locations.append((index, position))
    return "".join(characters), locations, texts


def _extend_glyph(text, start, end):
    # Keep combining accents, variation selectors, skin tones and ZWJ emoji
    # together when inserting colored chunks into Tk (no UTF-16 index arithmetic).
    def continuation(char):
        return bool(unicodedata.combining(char)) or unicodedata.category(char) in {"Mn", "Mc", "Me"} or "\U0001f3fb" <= char <= "\U0001f3ff"
    while start and (continuation(text[start]) or text[start] == "\u200d" or text[start-1] == "\u200d"):start -= 1
    while end < len(text) and (continuation(text[end]) or text[end] == "\u200d" or text[end-1] == "\u200d"):end += 1
    return start, end


def difference_spans(a, b):
    """Paired, non-overlapping differences, including complete Unicode glyphs."""
    spans = []
    for i,j,k,l in _changes(a,b):
        ai,aj = _extend_glyph(a,i,j) if i<j else (i,j)
        bk,bl = _extend_glyph(b,k,l) if k<l else (k,l)
        before = max(i-ai,k-bk);after = max(aj-j,bl-l)
        span = (max(0,i-before),min(len(a),j+after),max(0,k-before),min(len(b),l+after))
        if spans and (span[0]<spans[-1][1] or span[2]<spans[-1][3]):
            previous = spans.pop();span = (previous[0],max(previous[1],span[1]),previous[2],max(previous[3],span[3]))
        spans.append(span)
    return spans


def difference_ranges(left, right):
    """Return character ranges per original segment, without changing either input.

    Compare the continuous text so different subtitle boundaries cannot create
    false differences. Returned offsets address stripped Python strings, not Tk.
    """
    a, positions_a, texts_a = _characters(left)
    b, positions_b, texts_b = _characters(right)
    ranges_a = [[] for _ in left];ranges_b = [[] for _ in right]
    for i, j, k, l in difference_spans(a, b):
        for positions, ranges, start, end in ((positions_a, ranges_a, i, j), (positions_b, ranges_b, k, l)):
            for segment, character in positions[start:end]:
                row = ranges[segment]
                if row and row[-1][1] == character:row[-1] = (row[-1][0], character+1)
                else:row.append((character, character+1))
    for texts, ranges in ((texts_a, ranges_a), (texts_b, ranges_b)):
        for index, row in enumerate(ranges):
            merged = []
            for start, end in row:
                start, end = _extend_glyph(texts[index], start, end)
                if merged and start <= merged[-1][1]:merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
                else:merged.append((start, end))
            ranges[index] = merged
    return ranges_a, ranges_b


def text_runs(text, ranges):
    """Yield lossless (text, highlighted) chunks for insertion, including emoji."""
    position = 0
    for start, end in ranges:
        if start > position:yield text[position:start], False
        yield text[start:end], True
        position = end
    if position < len(text):yield text[position:], False
