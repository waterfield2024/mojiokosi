"""Private CPU background worker: separate only selected overlap subtitle spans."""
import json
import os
from pathlib import Path
import sys
import time
import traceback
import wave

RATE = 16000


def read_pcm(path, start, end):
    """Locate RIFF data, including FFmpeg metadata and a growing live WAV."""
    import struct
    with Path(path).open('rb') as source:
        header = source.read(12)
        if header[:4] != b'RIFF' or header[8:] != b'WAVE': raise ValueError('PCM WAVが必要です。')
        valid = False
        while chunk := source.read(8):
            if len(chunk) != 8: break
            kind, size = struct.unpack('<4sI', chunk)
            if kind == b'fmt ':
                fmt = source.read(size)
                valid = len(fmt) >= 16 and struct.unpack('<HHI', fmt[:8]) == (1, 1, RATE) and struct.unpack('<H', fmt[14:16])[0] == 16
                if size % 2: source.read(1)
            elif kind == b'data':
                if not valid: raise ValueError('16kHzモノラルPCM16の録音が必要です。')
                source.seek(start*2, 1)
                pcm = source.read((end-start)*2)
                if len(pcm) != (end-start)*2: raise ValueError('分離する音声はまだ保存されていません。')
                return pcm
            else: source.seek(size + size%2, 1)
    raise ValueError('WAVの音声データがありません。')


def emit(**event):
    print(json.dumps(event, ensure_ascii=False), flush=True)


def distinct_streams(streams, mixture):
    import numpy as np
    if streams.shape != (2, len(mixture)) or not np.isfinite(streams).all(): return False
    energy = np.mean(streams ** 2, axis=1)
    if float(min(energy)) < max(1e-8, float(np.mean(mixture ** 2)) * .002): return False
    norm = np.linalg.norm(streams, axis=1)
    similarity = abs(float(streams[0] @ streams[1] / max(1e-12, norm[0] * norm[1])))
    return similarity < .85


class Processor:
    def __init__(self, settings, job, status):
        self.settings, self.job, self.status = settings, Path(job), status
        self.separator = self.recognizer = self.gate = self.extractor = None
        self.voice_profiles = {}
        self.voice_names = {}
        import uuid
        self.session = uuid.uuid4().hex
        self.next_voice = 0

    def prepare(self):
        from asr import ROOT
        from overlap_model import Separator
        from recognition_models import live_recognizer_type
        from speech_gate import SpeechGate
        import sherpa_onnx as so
        if self.separator is None: self.separator = Separator(ROOT / 'models', self.status)
        if self.recognizer is None:
            self.recognizer = live_recognizer_type(self.settings['model'])(self.settings['model'],
                self.settings.get('language', 'ja'), self.settings.get('prompt', ''),
                self.settings.get('compute', 'cpu'), self.job, self.status)
        if self.gate is None:
            self.gate = SpeechGate(ROOT / 'models', self.settings.get('speech_sensitivity', 'balanced'))
            self.gate.prepare(self.status)
            if self.gate.warning: raise RuntimeError(self.gate.warning)
        if self.extractor is None:
            config = so.SpeakerEmbeddingExtractorConfig(model=str(ROOT/'models/speakers/embedding.onnx'),
                                                        num_threads=1, provider='cpu')
            if not config.validate(): raise RuntimeError('分離した声の照合モデルを読み込めません。')
            self.extractor = so.SpeakerEmbeddingExtractor(config)

    def identities(self, streams, task):
        import numpy as np
        profiles = {**self.voice_profiles, **task.get('profiles', {})}
        vectors, matches = [], {}
        for samples in streams:
            stream = self.extractor.create_stream()
            stream.accept_waveform(sample_rate=RATE, waveform=samples)
            stream.input_finished()
            vector = np.asarray(self.extractor.compute(stream), dtype='float32') if self.extractor.is_ready(stream) else np.zeros(self.extractor.dim)
            vectors.append(vector / max(float(np.linalg.norm(vector)), 1e-9))
        scores = []
        for i, vector in enumerate(vectors):
            for key, profile in profiles.items():
                other = np.asarray(profile.get('embedding', []), dtype='float32')
                if len(other) == len(vector) and np.isfinite(other).all():
                    score = float(vector @ (other / max(float(np.linalg.norm(other)), 1e-9)))
                    scores.append((score, i, key))
        used = set()
        for score, i, key in sorted(scores, reverse=True):
            alternatives = [s for s, j, k in scores if j == i and k != key]
            if (i not in matches and key not in used and score >= .68
                    and (not alternatives or score - max(alternatives) >= .08)):
                matches[i] = key; used.add(key)
        return matches, vectors

    def process(self, task):
        import numpy as np
        from realtime import live_rows
        from speech_gate import keep_spoken_rows
        started = time.monotonic()
        self.prepare()
        a, b = round(task['start'] * RATE), round(task['end'] * RATE)
        if a < 0 or b <= a or b-a > 30 * RATE: raise ValueError('分離区間が正しくありません。')
        pcm = read_pcm(task['wav'], a, b)
        mixture = np.frombuffer(pcm, '<i2').astype('float32') / 32768
        streams = self.separator.separate(mixture)
        if not distinct_streams(streams, mixture):
            return {'id': task['id'], 'message': '2人の声を十分に分離できないため、元の字幕を残しました。'}
        identities, vectors = self.identities(streams, task)
        rows, names, texts = [], {}, []
        folder = Path(task['folder'])
        output_dir = folder / '.audio' / 'separated'
        output_dir.mkdir(parents=True, exist_ok=True)
        for i, samples in enumerate(streams):
            peak = float(np.max(np.abs(samples)))
            samples = samples * min(10., .9 / max(peak, .00001))
            audio = np.round(np.clip(samples, -1, 1)*32767).astype('<i2').tobytes()
            speech = [(a+x, a+y) for x, y in self.gate.intervals(audio)]
            if sum(y-x for x,y in speech) < .16 * RATE:
                return {'id': task['id'], 'message': '分離後に2人分の発話を確認できないため元の字幕を残しました。'}
            result = self.recognizer.transcribe(audio)
            speaker = identities.get(i)
            if speaker is None:
                self.next_voice += 1
                speaker = f'overlap_{self.session}_{self.next_voice}'
                self.voice_names[speaker] = f'分離話者{self.next_voice}（未確定）'
            identities[i] = speaker
            unknown = speaker not in task.get('profiles', {})
            names[speaker] = task.get('names', {}).get(speaker, self.voice_names.get(speaker, speaker))
            current = live_rows(result, a, b, a, [], speaker=speaker)
            current = keep_spoken_rows(current, speech, self.settings.get('speech_sensitivity', 'balanced'))
            path = output_dir / f"{task['id']}-{i+1}.wav"
            with wave.open(str(path), 'wb') as output:
                output.setnchannels(1); output.setsampwidth(2); output.setframerate(RATE); output.writeframes(audio)
            for n, row in enumerate(current):
                row.update(segment_id=f"{task['id']}-{i+1}-{n}", speaker=speaker,
                    speaker_review=unknown, overlap_separated=True, overlap_group=task['id'],
                    live_prompt_eligible=False,
                    separated_audio=path.relative_to(folder).as_posix(), separated_offset=task['start'])
            rows.extend(current)
            texts.append(''.join(r['text'] for r in current).strip())
        import re
        normalized = [re.sub(r'[\W_]', '', t).casefold() for t in texts]
        if not all(normalized) or normalized[0] == normalized[1]:
            return {'id': task['id'], 'message': '2人分の異なる発言を確認できないため元の字幕を残しました。'}
        # Session-only matching helps follow a voice across adjacent overlap
        # chunks. Never write a separated embedding into registered voice memory.
        for i, speaker in identities.items():
            if np.linalg.norm(vectors[i]) > .5:
                self.voice_profiles[speaker] = {'embedding': vectors[i].tolist()}
                self.voice_names[speaker] = names[speaker]
        return {'id': task['id'], 'after': rows, 'names': names, 'seconds': round(time.monotonic()-started, 2)}

    def close(self):
        if self.recognizer: self.recognizer.close()
        if self.gate: self.gate.close()


def serve():
    os.environ['WHISPER_BACKGROUND_THREADS'] = '2'
    os.environ['OMP_NUM_THREADS'] = '2'
    os.environ['MKL_NUM_THREADS'] = '2'
    if os.name != 'nt': os.nice(10)
    settings = json.loads(sys.stdin.readline())
    processor = Processor(settings['settings'], settings['job'], lambda text: emit(kind='status', text=text))
    emit(kind='ready')
    try:
        for line in sys.stdin:
            task = json.loads(line)
            if task.get('command') == 'close': break
            try: emit(kind='result', **processor.process(task))
            except Exception as exc:
                traceback.print_exc(file=sys.stderr)
                emit(kind='result', id=task['id'], message='音声分離を見送りました：' + str(exc))
    finally: processor.close()


if __name__ == '__main__': serve()
