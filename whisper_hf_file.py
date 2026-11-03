"""Bounded-memory file transcription with the same compatible Whisper models."""
import audioop
import wave

RATE = 16000
MAX_FRAMES = 20 * RATE


def audio_chunks(source):
    total = source.getnframes()
    while source.tell() < total:
        start = source.tell()
        pcm = source.readframes(MAX_FRAMES)
        frames = len(pcm) // 2
        if not frames: raise ValueError('音声ファイルの末尾が不足しています。')
        cut = frames
        # Prefer a quiet boundary in the final four seconds, without duplicating
        # or dropping any samples. Each request remains below Whisper's 30 s limit.
        if frames == MAX_FRAMES and start + frames < total:
            width = RATE // 5
            candidates = range(frames - 4 * RATE, frames - width + 1, RATE // 50)
            quiet = min(candidates, key=lambda frame: audioop.rms(pcm[frame * 2:(frame + width) * 2], 2))
            if audioop.rms(pcm[quiet * 2:(quiet + width) * 2], 2) < 150:
                cut = quiet + width // 2
                source.setpos(start + cut)
        yield start / RATE, pcm[:cut * 2]


def transcribe_file(wav, name, language, prompt, job, compute, progress):
    from whisper_hf_asr import WhisperHFRecognizer
    with wave.open(str(wav), 'rb') as source:
        if (source.getframerate(), source.getnchannels(), source.getsampwidth(), source.getcomptype()) != (RATE, 1, 2, 'NONE'):
            raise ValueError('互換Whisperには16kHz・モノラルのPCM音声が必要です。')
        recognizer = WhisperHFRecognizer(name, language, prompt, compute, job, progress)
        rows = []
        duration = source.getnframes() / RATE
        try:
            for offset, pcm in audio_chunks(source):
                end = min(duration, offset + len(pcm) / (2 * RATE))
                progress(f'互換Whisperで文字起こし中… {name} / {recognizer.device} / {int(100 * offset / max(duration, .01))}%')
                result = recognizer.transcribe(pcm)
                for row in result['segments']:
                    start = max(offset, min(end, offset + float(row['start'])))
                    stop = max(start, min(end, offset + float(row['end'])))
                    if row['text'].strip() and stop > start:
                        rows.append({**row, 'start': round(start, 3), 'end': round(stop, 3), 'words': []})
            progress('互換Whisperの文字起こしが完了しました。')
            return {'segments': rows, 'language': language, 'device': recognizer.device,
                    'backend': recognizer.backend, 'fallbacks': recognizer.failures,
                    'timestamp_method': 'whisper-timestamps'}
        finally:
            recognizer.close()
