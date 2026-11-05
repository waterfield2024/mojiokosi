"""One visual row/column per simultaneous speaker, at most two at a time."""
from speakers import speaker_name


def cues(rows, split, names, *, vertical=False):
    """Paginate each voice on its own clock, then combine at shared boundaries."""
    pages = []
    for row in rows:
        start, end = round(row['start']*100), round(row['end']*100)
        if end <= start: continue
        name = speaker_name(row.get('speaker'), names)
        pieces = split(row['text'], name)
        if not pieces: continue
        if len(pieces) > end-start:
            from caption_timing import CaptionProblem
            raise CaptionProblem(row['_caption_index'], '分離した字幕の表示時間が短すぎます。時刻か文字サイズを調整してください。')
        weights = [max(1, len(piece)) for piece in pieces]
        total, used, cursor = sum(weights), 0, start
        for i, (piece, weight) in enumerate(zip(pieces, weights)):
            used += weight
            boundary = max(cursor+1, min(start+round((end-start)*used/total), end-(len(pieces)-i-1)))
            pages.append((cursor, boundary, row.get('speaker'), piece, row['_caption_index']))
            cursor = boundary
    boundaries = sorted({time for a,b,_,_,_ in pages for time in (a,b)})
    result = []
    for a,b in zip(boundaries, boundaries[1:]):
        active = [p for p in pages if p[0] <= a and p[1] >= b]
        if not active: continue
        active.sort(key=lambda p: (p[2], p[4]))
        lines = [p[3] for p in active[:2]]
        result.append({'start_cs': a, 'end_cs': b, 'lines': lines,
                       'segment_index': active[0][4], 'segment_indices': [p[4] for p in active],
                       'speaker_font_size': None, 'name_first': False})
    return result
