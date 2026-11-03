"""Keep Haas TDT token times; do not invent character alignment on a mismatch."""
import math
from granite_decode import segment_words


def timed_result(text, tokens, timestamps, durations, duration):
    text = text.strip()
    if not text or not any(char.isalnum() for char in text) or duration <= 0:
        return {'text': '', 'segments': [], 'language': 'ja'}
    words = []
    if len(tokens) == len(timestamps) == len(durations) and ''.join(tokens).strip() == text:
        previous = 0.0
        for token, start, length in zip(tokens, timestamps, durations):
            if not all(math.isfinite(n) for n in (start, length)) or start < previous or length < 0:
                words = []
                break
            previous = start
            a = min(duration, max(0.0, start))
            b = min(duration, max(a, start + length))
            words.append({'word': token, 'start': round(a, 3), 'end': round(b, 3)})
    segments = segment_words(words) if words else []
    preserved = ''.join(''.join(row['text'].split()) for row in segments) == ''.join(text.split())
    if not segments or not preserved or any(row['end'] <= row['start'] for row in segments):
        segments = [{'start': 0.0, 'end': duration, 'text': text, 'words': []}]
    return {'text': text, 'segments': segments, 'language': 'ja'}
