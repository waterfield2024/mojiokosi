"""Resolve one history utterance to local audio and its edited time interval."""
import math
from pathlib import Path
import wave
from result_media import MEDIA_EXTENSIONS, result_file


def utterance_interval(row):
    try:
        start, end = float(row['start']), float(row['end'])
    except (KeyError, TypeError, ValueError, OverflowError):
        raise ValueError('この発言の開始・終了時刻を確認してください。') from None
    if not math.isfinite(start) or not math.isfinite(end) or end <= max(0, start):
        raise ValueError('この発言の開始・終了時刻を確認してください。')
    return max(0., start), end


def audio_source(data, folder):
    from engine import resolve_cached_audio
    folder = Path(folder)
    # The WAV is still growing while capture runs. Never select an older FLAC
    # snapshot if a recording is in progress; neither source is modified here.
    recording = result_file(folder, '.audio/recording.wav', {'.wav'})
    if data.get('mode') == 'realtime' and not data.get('recording_complete', True) and recording:
        return recording
    cached = resolve_cached_audio(data, folder)
    if cached:
        return cached
    if data.get('source_mode') == 'file' or data.get('mode') == 'file':
        source = result_file(folder, data.get('source'), MEDIA_EXTENSIONS)
        if source:
            return source
    raise FileNotFoundError('発言の音声が見つかりません。保存した結果のフォルダー全体、または元の音声・動画を戻してください。')


def playback_target(data, folder, row):
    start, end = utterance_interval(row)
    if row.get('separated_audio'):
        root = Path(folder).absolute()
        source = result_file(root, row['separated_audio'], {'.wav'})
        if source and source.is_relative_to(root / '.audio' / 'separated'):
            offset = float(row.get('separated_offset', 0))
            # Expand Windows short names in the selected folder only after the link checks.
            recording = root.resolve() / source.relative_to(root)
            return recording, (max(0., start-offset), max(.01, end-offset))
    source = audio_source(data, folder)
    if source.suffix.lower() == '.wav':
        try:
            with wave.open(str(source), 'rb') as audio:
                duration = audio.getnframes() / audio.getframerate()
        except (wave.Error, EOFError):
            # Other WAV encodings are read by FFmpeg; its errors go to the UI.
            duration = None
        if duration is not None:
            end = min(end, duration)
            if end <= start:
                raise ValueError('この発言の音声はまだ保存されていないか、音声の範囲外です。少し待って再生するか、時刻を確認してください。')
    return source, (start, end)
