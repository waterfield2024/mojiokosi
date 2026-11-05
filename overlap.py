"""Overlap planning and transactional, reversible replacement of subtitle rows."""
from collections import defaultdict
from copy import deepcopy
from itertools import groupby
import math
import uuid


def regions(turns, start=0., end=math.inf):
    """Count distinct local voices, not adjacent turns or duplicate detections."""
    events = []
    for turn in turns:
        a, b = max(start, float(turn['start'])), min(end, float(turn['end']))
        key = turn.get('local', turn.get('speaker'))
        if key in (None, '', 'unknown') or not math.isfinite(a) or not math.isfinite(b) or b <= a: continue
        events.extend(((a, key, 1), (b, key, -1)))
    active, result, previous = defaultdict(int), [], start
    for instant, group in groupby(sorted(events), key=lambda e: e[0]):
        count = len(active)
        if count >= 2 and instant > previous:
            if result and previous - result[-1]['end'] <= .15:
                result[-1]['end'] = instant
                result[-1]['voices'] = max(count, result[-1]['voices'])
            else: result.append({'start': previous, 'end': instant, 'voices': count})
        for _, key, delta in group:
            active[key] += delta
            if active[key] == 0: del active[key]
        previous = instant
    return [r for r in result if r['end'] - r['start'] >= .2]


def protected(data, row):
    return (row.get('speaker_manual') or row.get('manual_edit') or row.get('text_manual')
            or row.get('caption_timing') or row.get('manual_text_owner')
            or row.get('segment_id') in data.get('live_text_edits', {}))


def plan(data, intervals, seen):
    """Expand only to whole affected subtitles: never delete text outside a patch."""
    rows = data.get('segments', [])
    for row in rows: row.setdefault('segment_id', uuid.uuid4().hex)
    groups = []
    for interval in intervals:
        affected = [r for r in rows if not r.get('overlap_separated') and r['end'] > interval['start'] and r['start'] < interval['end']]
        if not affected: continue
        ids = {r['segment_id'] for r in affected}
        if any(ids & g['ids'] for g in groups):
            matching = [g for g in groups if ids & g['ids']]
            for group in matching:
                ids |= group['ids']; groups.remove(group)
            voices = max([interval['voices']] + [g['voices'] for g in matching])
        else: voices = interval['voices']
        groups.append({'ids': ids, 'voices': voices})
    # Adjacent subtitles from one overlapping exchange are separated together.
    # This preserves the model's stream order across punctuation/ASR boundaries.
    positions = {r['segment_id']: i for i, r in enumerate(rows)}
    by_id = {r['segment_id']: r for r in rows}
    groups.sort(key=lambda g: min(positions[k] for k in g['ids']))
    merged = []
    for group in groups:
        if merged:
            left = merged[-1]
            left_rows = [by_id[k] for k in left['ids']]
            right_rows = [by_id[k] for k in group['ids']]
            adjacent = min(positions[k] for k in group['ids']) == max(positions[k] for k in left['ids']) + 1
            close = min(r['start'] for r in right_rows) - max(r['end'] for r in left_rows) <= .4
            bounded = max(r['end'] for r in left_rows+right_rows) - min(r['start'] for r in left_rows+right_rows) <= 30
            if adjacent and close and bounded:
                left['ids'] |= group['ids']; left['voices'] = max(left['voices'], group['voices'])
                continue
        merged.append(group)
    tasks = []
    for group in merged:
        ids = group['ids']
        if ids & seen: continue
        affected = [r for r in rows if r['segment_id'] in ids]
        for row in affected: row['overlap_detected'] = True
        seen.update(ids)
        a, b = min(r['start'] for r in affected), max(r['end'] for r in affected)
        reason = ('3人以上の重なりは自動分離の対象外です。' if group['voices'] > 2 else
                  '手動で修正した字幕を保護しました。' if any(protected(data, r) for r in affected) else
                  '30秒を超える字幕区間は元の結果を残しました。' if b-a > 30 else '')
        task = {'id': uuid.uuid4().hex, 'start': a, 'end': b, 'before': deepcopy(affected),
                'status': 'skipped' if reason else 'pending', 'message': reason, 'voices': group['voices']}
        data.setdefault('overlap_review', []).append(task)
        if not reason: tasks.append(deepcopy(task))
    return tasks


def signature(row):
    return (row.get('segment_id'), row.get('speaker'), row['start'], row['end'])


def apply_reply(data, reply):
    record = next((r for r in data.get('overlap_review', []) if r['id'] == reply['id']), None)
    if record is None or record['status'] != 'pending': return None
    before_ids = {r['segment_id'] for r in record['before']}
    current = [r for r in data['segments'] if r.get('segment_id') in before_ids]
    after = deepcopy(reply.get('after', []))
    reason = reply.get('message', '')
    if data.get('live_overlap_states', {}).get(record['id']) == 'reverted': reason = '元の字幕を保護しました。'
    if (len(current) != len(before_ids) or any(protected(data, r) for r in current)
            or {signature(r) for r in current} != {signature(r) for r in record['before']}):
        reason = '処理中に修正された字幕を保護しました。'
    if not after or reason:
        record.update(status='skipped', message=reason or '分離結果を確認できないため元の字幕を残しました。')
        return deepcopy(record)
    if len({r.get('speaker') for r in after}) != 2 or any(
            not r.get('text', '').strip() or not math.isfinite(r['start']) or not math.isfinite(r['end'])
            or r['start'] < record['start']-.01 or r['end'] > record['end']+.01 or r['end'] <= r['start'] for r in after):
        record.update(status='skipped', message='分離結果の時刻・話者を検証できませんでした。')
        return deepcopy(record)
    record.update(before=deepcopy(current), after=after, status='applied',
                  message='2人の声を分離して文字起こししました。話者名に「？」がある場合は確認してください。',
                  seconds=reply.get('seconds', 0), names=reply.get('names', {}))
    data['segments'] = sorted([r for r in data['segments'] if r.get('segment_id') not in before_ids] + deepcopy(after),
                              key=lambda r: (r['start'], r['end'], r.get('segment_id', '')))
    for key, name in reply.get('names', {}).items(): data.setdefault('speakers', {}).setdefault(key, name)
    return deepcopy(record)


def set_state(data, record, state, *, force=False):
    if state not in {'applied', 'reverted'} or not record.get('after'): return False
    if record['status'] == state: return False
    source, target = (record['after'], record['before']) if state == 'reverted' else (record['before'], record['after'])
    ids = {r['segment_id'] for r in source}
    current = [r for r in data['segments'] if r.get('segment_id') in ids]
    expected = {r['segment_id']: r for r in source}
    if not force and (len(current) != len(ids) or any(protected(data, r) or
            signature(r) != signature(expected[r['segment_id']]) for r in current)):
        raise ValueError('この字幕は後から編集されています。編集内容を保護するため、自動では戻しません。')
    if state == 'reverted' and not force: record['after'] = deepcopy(current)
    data['segments'] = sorted([r for r in data['segments'] if r.get('segment_id') not in ids] + deepcopy(target),
                              key=lambda r: (r['start'], r['end'], r.get('segment_id', '')))
    record['status'] = state
    return True
