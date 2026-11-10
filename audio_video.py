"""Prepare a reusable black background with the saved audio, without recording a screen."""
from platform_support import tool_path
import hashlib
import json
import os
from pathlib import Path
import subprocess
import uuid

from engine import TOOLS, NO_WINDOW, emit, resolve_cached_audio

from result_media import AUDIO_EXTENSIONS, result_file


def saved_audio(result, folder):
    audio = resolve_cached_audio(result, folder)
    if audio:
        return audio.resolve()
    return result_file(folder, result.get("source"), AUDIO_EXTENSIONS)


def prepare_audio_video(result, folder):
    audio = saved_audio(result, folder)
    if audio is None:
        raise FileNotFoundError("保存した音声が見つかりません。録音を含む結果フォルダーを戻すか、元動画を選んでください。")
    stat = audio.stat()
    identity = json.dumps([str(audio), stat.st_size, stat.st_mtime_ns, "black-720p25-v1"])
    key = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
    cache = Path(folder) / ".video" / "audio-subtitles"
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / (key + ".mkv")
    if target.is_file() and target.stat().st_size:
        emit("status", text="準備済みの音声と黒背景を読み込みます…")
        return target.resolve()
    temporary = cache / (key + "." + uuid.uuid4().hex + ".partial.mkv")
    emit("status", text="録音した音声と黒背景を準備中… 長い録音では時間がかかります。閉じると中止します。")
    try:
        # FLAC keeps the intermediate audio lossless; the final MP4 uses AAC.
        # This subprocess belongs to the existing preview/render process job.
        command = [str(tool_path('ffmpeg')), "-nostdin", "-hide_banner", "-loglevel", "error", "-n",
                   "-f", "lavfi", "-i", "color=c=black:s=1280x720:r=25", "-i", str(audio),
                   "-map", "0:v:0", "-map", "1:a:0", "-c:v", "libx264", "-preset", "ultrafast",
                   "-crf", "28", "-pix_fmt", "yuv420p", "-threads", "2", "-c:a", "flac",
                   "-af", "aresample=async=1:first_pts=0", "-shortest", "-map_metadata", "-1",
                   "-map_chapters", "-1", str(temporary)]
        completed = subprocess.run(command, capture_output=True, creationflags=NO_WINDOW)
        if completed.returncode or not temporary.is_file() or not temporary.stat().st_size:
            raise RuntimeError("音声と字幕のMP4を準備できませんでした。\n" +
                               completed.stderr.decode("utf-8", errors="replace")[-1200:])
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target.resolve()
