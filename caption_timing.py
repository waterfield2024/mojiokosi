"""Caption-only intervals; original speech/audio ranges remain unchanged."""
import math


class CaptionProblem(ValueError):
    """A caption issue must not prevent the source media from playing."""
    def __init__(self, index, message):
        super().__init__(message)
        self.segment_index = index


def checked_interval(value):
    try:
        start, end = value["start"], value["end"]
        if isinstance(start, bool) or isinstance(end, bool):raise ValueError
        start, end = float(start), float(end)
        if not math.isfinite(start) or not math.isfinite(end) or start < 0:raise ValueError
        start, end = round(start, 2), round(end, 2)
        if start < 0 or end <= start:raise ValueError
    except (KeyError, TypeError, ValueError, OverflowError):
        raise ValueError("開始・終了は秒数で入力し、終了を開始より0.01秒以上後にしてください。") from None
    return start, end


def timed_segments(segments, duration=None):
    """Resolve the old single-caption overlap rule before applying manual times.

    Moving a caption must not lengthen another caption's original interval.
    Source indices survive sorting and pagination to identify the edited utterance.
    """
    original = []
    for index, row in enumerate(segments):
        start, end = float(row["start"]), float(row["end"])
        if not math.isfinite(start) or not math.isfinite(end):
            raise ValueError("字幕の時刻が不正です。")
        start = max(0., start)
        if duration is not None:end = min(end, duration)
        if str(row["text"]).strip() and (end > start or "caption_timing" in row):
            original.append((start, end, index, row))
    original.sort(key=lambda item:item[0])
    resolved = []
    for position, (start, end, index, row) in enumerate(original):
        if position+1 < len(original):
            following = original[position+1]
            if not (row.get('overlap_group') and row.get('overlap_group') == following[3].get('overlap_group')):
                end = min(end, following[0])
        if "caption_timing" in row:start, end = checked_interval(row["caption_timing"])
        if duration is not None:end = min(end, duration)
        if round(end*100) > round(start*100):
            resolved.append({**row, "start":start, "end":end, "_caption_index":index})
    return sorted(resolved, key=lambda row:row["start"])


def validate_change(segments, index, start, end, duration=None):
    start, end = checked_interval({"start":start, "end":end})
    if duration is not None and end > math.floor(duration*100+1e-6)/100:
        raise ValueError(f"終了は動画の長さ（{duration:.2f}秒）以内にしてください。")
    for row in timed_segments(segments, duration):
        if row["_caption_index"] == index:continue
        target = segments[index]
        if (target.get('overlap_group') and target['overlap_group'] == row.get('overlap_group')
                and target.get('speaker') != row.get('speaker')): continue
        other_start, other_end = round(row["start"],2), round(row["end"],2)
        if start < other_end and other_start < end:
            raise ValueError(f"字幕 {row['_caption_index']+1}（{other_start:.2f}〜{other_end:.2f}秒）と重なります。開始・終了を調整してください。")
    return {"start":start, "end":end}
