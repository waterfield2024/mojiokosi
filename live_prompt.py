"""Short-lived transcript hints for live Whisper, independent of AI proofreading."""
import math
import re

RESET_SECONDS = 6.0
MAX_ROWS = 2
MAX_CHARS = 160
WHISPER_MODELS = {'base', 'small', 'turbo'}


def clean_text(text):
    # Whisper prompt tokens must be supplied by the tokenizer, never by a caption.
    return ' '.join(re.sub(r'<\|.*?\|>', '', str(text)).split())


def repeated(text):
    compact = re.sub(r'[\s。、,.!?！？]', '', text)
    return bool(re.search(r'(.{2,40}?)\1{2,}', compact))


def reliable(raw):
    """Exclude doubtful text from future hints without deleting its caption."""
    try:
        values = [float(raw.get(key, default)) for key, default in
                  (('avg_logprob', 0), ('no_speech_prob', 0), ('compression_ratio', 1))]
        return (all(math.isfinite(v) for v in values) and values[0] >= -1
                and values[1] <= .6 and values[2] <= 2.4)
    except (TypeError, ValueError):
        return False


def recent_text(rows, start):
    # Inspect only the immediate past: never reach across a rejected utterance.
    recent = rows[-MAX_ROWS:]
    if len(rows) >= 3 and len({clean_text(r.get('text', '')) for r in rows[-3:]}) == 1:
        return ''
    parts = []
    next_start = start
    for row in reversed(recent):
        try:
            a, b = float(row['start']), float(row['end'])
        except (KeyError, TypeError, ValueError):
            break
        if not (math.isfinite(a) and math.isfinite(b) and 0 <= a <= b <= start + .15):
            break
        if next_start - b >= RESET_SECONDS or not row.get('live_prompt_eligible', True):
            break
        text = clean_text(row.get('text', ''))
        if not text or repeated(text):
            break
        if not parts or text != parts[-1]:
            parts.append(text)
        next_start = a
    return ' '.join(reversed(parts))[-MAX_CHARS:]


def make_prompt(base, rows, start):
    recent = recent_text(rows, start)
    if not recent:
        return base
    # Stay inside the HF worker's 800-character input bound. It additionally
    # caps tokenizer output, so long Japanese/English hints cannot overflow.
    return '\n'.join(part for part in (clean_text(base)[-600:], recent) if part)


def transcribe_with_prompt(recognizer, pcm, prompt=None):
    if prompt is None:
        return recognizer.transcribe(pcm)
    # Both native and HF backends expose this property. Restore it on failure
    # too, so a resident model cannot carry a prior recording into the next one.
    original = recognizer.prompt
    try:
        recognizer.prompt = prompt
        return recognizer.transcribe(pcm)
    finally:
        recognizer.prompt = original
