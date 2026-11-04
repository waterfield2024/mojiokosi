"""Growing live utterances: replaceable previews, durable final boundaries."""
from dataclasses import dataclass
import audioop
import json
from pathlib import Path
from speech_preferences import profile

RATE = 16000
PAUSE_SECONDS = .7
PAUSE_CHOICES = (.3, .5, .7, 1., 1.2, 1.5, 2., 3.)
MAX_SECONDS = 12
MAX_CHOICES = (8, 12, 20, 30)
MODES = {'話の切れ目で区切る': 'utterance', '秒数で区切る（従来）': 'timed'}


def mode(value='utterance'):
    if not isinstance(value, str) or value not in MODES.values():
        raise ValueError('音声の区切り方を一覧から選んでください。')
    return value


def pause_duration(value=PAUSE_SECONDS):
    if type(value) not in (int, float) or value not in PAUSE_CHOICES:
        raise ValueError('区切るまでの無音は一覧の0.3〜3.0秒から選んでください。')
    return float(value)


def read_pause_control(path, current):
    try:
        return pause_duration(json.loads(Path(path).read_text('utf-8'))['pause_seconds'])
    except (OSError, ValueError, KeyError, TypeError):
        return current


def utterance_limit(value=MAX_SECONDS):
    if type(value) is not int or value not in MAX_CHOICES:
        raise ValueError('話し続けたときの上限は8・12・20・30秒から選んでください。')
    return value


def read_limit_control(path, current):
    try:
        return utterance_limit(json.loads(Path(path).read_text('utf-8'))['max_utterance_seconds'])
    except (OSError, ValueError, KeyError, TypeError):
        return current


@dataclass(frozen=True)
class AudioChunk:
    start: int
    end: int
    core: int
    utterance: int
    final: bool

    def __iter__(self):
        return iter((self.start, self.end, self.core))


class UtteranceChunker:
    def __init__(self, submit, seconds=2, sensitivity='balanced', activity=None, *,
                 pause_seconds=PAUSE_SECONDS, max_utterance_seconds=MAX_SECONDS):
        self.submit = submit
        self.update_samples = int(seconds * RATE)
        self.profile = profile(sensitivity)
        self.total = self.core = self.voiced = self.last_voice = 0
        self.start = None
        self.utterance = 0
        self.next_preview = 0
        self.activity = activity
        self.heard_speech = False
        self.pause_seconds = pause_duration(pause_seconds)
        self.max_utterance_seconds = utterance_limit(max_utterance_seconds)

    def feed(self, pcm):
        # Bound checks and voice durations are independent of input block size.
        if len(pcm) % 2:
            raise ValueError('音声は16ビットPCMで指定してください。')
        offset = 0
        while offset < len(pcm):
            limit = self.max_utterance_seconds * RATE
            # A live decrease commits the already captured draft as-is. Never
            # truncate its audio or reopen a previously finalized utterance.
            if self.start is not None and self.total - self.start >= limit:
                self.close_utterance(overlap=True)
            count = min(RATE//50, (len(pcm)-offset)//2)
            if self.start is not None:
                # Pre-roll can begin between frame boundaries. Split the last
                # frame exactly so even Haas's strict 30 s limit is respected.
                count = min(count, limit - (self.total-self.start))
            self.feed_frame(pcm[offset:offset+count*2])
            offset += count*2

    def feed_frame(self, pcm):
        a = self.total
        self.total += len(pcm)//2
        voice = bool(pcm) and audioop.rms(pcm, 2) >= self.profile.capture_rms
        detected = self.activity.feed(pcm) if self.activity else None
        if voice:
            if self.start is None:
                self.start = max(self.core, a - int(.25*RATE))
                self.next_preview = a + self.update_samples
                self.voiced = 0
            self.voiced += len(pcm)//2
        if detected:
            self.heard_speech = True
        # Start conservatively from energy, preserving very short/quiet replies.
        # Once speech is detected, use the neural VAD to find its end even if
        # background sound continues. A failed detector falls back to energy.
        if detected or (voice and (detected is None or not self.heard_speech)):
            self.last_voice = self.total
        if self.start is None:
            return
        pause = self.total - self.last_voice >= round(self.pause_seconds*RATE)
        full = self.total - self.start >= self.max_utterance_seconds*RATE
        if pause or full:
            self.close_utterance(overlap=full and not pause)
        elif (self.total >= self.next_preview and voice
              and self.voiced >= int(self.profile.capture_seconds*RATE)):
            self.publish(False)
            self.next_preview = self.total + self.update_samples

    def close_utterance(self, *, overlap):
        if self.voiced >= int(self.profile.capture_seconds*RATE):
            self.publish(True)
            self.core = self.total
        self.utterance += 1
        self.start = max(0,self.total-RATE) if overlap else None
        if self.start is None:self.heard_speech = False
        self.voiced = 0
        self.next_preview = self.total + self.update_samples

    def publish(self, final):
        self.submit(AudioChunk(self.start,self.total,self.core,self.utterance,final))

    def finish(self):
        if self.start is not None and self.total > self.core and self.voiced >= int(self.profile.final_seconds*RATE):
            self.publish(True)
            self.core = self.total
        self.start = None
