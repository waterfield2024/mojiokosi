"""Local Whisper transcription worker. Network is used only for YouTube/model downloads."""
from __future__ import annotations
from platform_support import tool_path

import argparse
import json
import os
import re
import subprocess
import sys
import threading
import time
import traceback
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from speakers import segment_text, speaker_name, diarize, assign_speakers, normalize_names, reuse_speaker_ids, default_name
from corrections import normalize_rules, replace_segments

ROOT = Path(__file__).resolve().parent
TOOLS = ROOT / ".tools"
MODELS = ROOT / "models"
from user_data import data_path
RESULTS = data_path("transcripts")
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
from recognition_models import ALLOWED_MODELS
_EMIT_LOCK = threading.Lock()


def emit(kind, **values):
    # Loopback capture and recognition report from separate threads.
    with _EMIT_LOCK:
        print(json.dumps({"kind": kind, **values}, ensure_ascii=False), flush=True)


def youtube_url(value):
    value = value.strip()
    if "://" not in value:
        value = "https://" + value
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password:
        raise ValueError("YouTubeの動画URLを入力してください。")
    host = (parsed.hostname or "").lower()
    if parsed.port not in (None, 80, 443):
        raise ValueError("通常のYouTubeのURLを入力してください。")
    parts = parsed.path.strip("/").split("/")
    if host == "youtu.be":
        video_id = parts[0]
    elif host in {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}:
        if parsed.path == "/watch":
            video_id = parse_qs(parsed.query).get("v", [""])[0]
        elif len(parts) == 2 and parts[0] in {"shorts", "live", "embed"}:
            video_id = parts[1]
        else:
            video_id = ""
    else:
        raise ValueError("youtube.com または youtu.be の動画URLを入力してください。")
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", video_id):
        raise ValueError("動画を1本開き、その動画のURLをコピーしてください。")
    return "https://www.youtube.com/watch?v=" + video_id


def timestamp(seconds, srt=False):
    total = max(0, round(float(seconds) * 1000))
    sec, ms = divmod(total, 1000)
    minutes, sec = divmod(sec, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02}:{minutes:02}:{sec:02}{',' if srt else '.'}{ms:03}"


def transcript_text(segments, speakers=None):
    return "\n".join(segment_text(s, speakers) for s in segments if s["text"].strip()) + "\n"


def transcript_file_text(segments, speakers=None):
    """One utterance per TXT line: speaker, start/end time, edited text."""
    lines = []
    for segment in segments:
        text = " ".join(segment["text"].splitlines()).strip()
        if not text:
            continue
        name = " ".join(speaker_name(segment.get("speaker"), speakers).splitlines()).strip() or "未設定"
        start = max(0.0, float(segment["start"]))
        end = max(start, float(segment["end"]))
        lines.append(f"{name}、{timestamp(start)}〜{timestamp(end)}、{text}")
    return "\n".join(lines) + ("\n" if lines else "")


def subtitle_text(segments, speakers=None):
    from caption_timing import timed_segments
    blocks = []
    ordered = timed_segments(segments) if any("caption_timing" in s for s in segments) else segments
    for segment in ordered:
        text = segment_text(segment, speakers)
        if text:
            start = max(0.0, float(segment["start"]))
            end = max(start + 0.001, float(segment["end"]))
            blocks.append(
                f"{len(blocks)+1}\n{timestamp(start, True)} --> {timestamp(end, True)}\n{text}"
            )
    return "\n\n".join(blocks) + "\n"


def save_result(result, folder):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "transcript.txt").write_text(transcript_file_text(result["segments"], result.get("speakers")), encoding="utf-8-sig")
    (folder / "transcript.srt").write_text(subtitle_text(result["segments"], result.get("speakers")), encoding="utf-8-sig")
    (folder / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


def get_youtube_media(url, job, video=False):
    import yt_dlp
    last_event = [0.0]
    label = "動画" if video else "音声"

    def progress(data):
        if data["status"] == "downloading" and time.monotonic() - last_event[0] > 0.5:
            total = data.get("total_bytes") or data.get("total_bytes_estimate")
            downloaded = data.get("downloaded_bytes", 0)
            text = f"YouTubeの{label}を取得中… {downloaded / 1048576:.1f} MB"
            if total:
                text += f" / {total / 1048576:.1f} MB"
            emit("status", text=text)
            last_event[0] = time.monotonic()

    def reject_live(info, *, incomplete):
        if info.get("is_live"):
            return "配信中の動画は処理できません。配信終了後の動画を指定してください。"
        return None

    opts = {
        "format": "bestaudio/best", "outtmpl": str(job / "source.%(ext)s"),
        "noplaylist": True, "quiet": True, "noprogress": True, "no_warnings": False,
        "socket_timeout": 30, "retries": 3, "fragment_retries": 3,
        "progress_hooks": [progress], "match_filter": reject_live,
        "js_runtimes": {"node": {"path": str(tool_path('node'))}},
        "overwrites": False,
    }
    if video:
        opts.update({
            "format": "bv[height<=1080][vcodec^=avc1]+ba[ext=m4a]/bv[height<=1080]+ba/b[height<=1080]/b",
            "merge_output_format": "mkv",
            "ffmpeg_location": str(tool_path('ffmpeg')),
        })
    emit("status", text="YouTubeの動画情報を取得中…")
    try:
        with yt_dlp.YoutubeDL(opts) as downloader:
            info = downloader.extract_info(youtube_url(url), download=True)
            if not info or info.get("is_live"):
                raise ValueError("配信中の動画は、配信終了後に読み込んでください。")
            downloads = info.get("requested_downloads") or []
            candidates = [info.get("filepath"), downloader.prepare_filename(info)]
            candidates += [d.get("filepath") for d in downloads]
            candidate = next((Path(p) for p in candidates if p and Path(p).is_file()), None)
            if candidate is None:
                raise RuntimeError(f"取得した{label}ファイルが見つかりません。")
            return candidate, info.get("title", "YouTube動画")
    except yt_dlp.utils.DownloadError as exc:
        raise RuntimeError(
            f"YouTubeから{label}を取得できませんでした。\n"
            "URL・ネット接続・動画の公開状態を確認してください。"
            "ログインやアクセス制限がある動画は、お手元の音声・動画ファイルを読み込めます。\n\n"
            + str(exc)[-1400:]
        ) from exc


def get_youtube_audio(url, job):
    return get_youtube_media(url, job)



def convert_audio(media, wav):
    proc = subprocess.run(
        [str(tool_path('ffmpeg')), "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
         "-i", str(media), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav)],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=NO_WINDOW)
    if proc.returncode:
        raise RuntimeError("音声を読み込めませんでした。\n" + proc.stderr.decode("utf-8", errors="replace")[-1200:])


def cache_audio(wav, folder):
    directory = Path(folder) / ".audio"
    directory.mkdir(exist_ok=True)
    target = directory / "source.flac"
    if target.exists():
        return str(target)
    temporary = directory / (Path(wav).parent.name + ".partial.flac")
    try:
        proc = subprocess.run(
            [str(tool_path('ffmpeg')), "-nostdin", "-hide_banner", "-loglevel", "error", "-n",
             "-i", str(wav), "-c:a", "flac", str(temporary)],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=NO_WINDOW)
        if proc.returncode:
            raise RuntimeError("再解析用の音声を保存できませんでした。\n" + proc.stderr.decode("utf-8", errors="replace")[-1000:])
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return str(target)


def resolve_cached_audio(data, folder):
    from result_media import result_file
    return (result_file(folder, ".audio/source.flac", {".flac"})
            or result_file(folder, ".audio/recording.wav", {".wav"})
            or result_file(folder, data.get("audio_file"), {".flac", ".wav"}))


def apply_recorded_names(data, wav, turns, request, protected=None):
    """A failed name lookup must not discard an otherwise usable transcription."""
    from recording_speakers import recall_names
    protected = protected or {}
    data["speakers"] = normalize_names(data["segments"], {**data.get("speakers", {}), **protected})
    data["remembered_speakers"] = {}
    data.pop("speaker_memory_error", None)
    try:
        memory_file = request.get("speaker_memory_file") or data_path("speaker_memory.json", "WHISPER_SPEAKER_MEMORY")
        matches = recall_names(wav, turns, MODELS / "speakers" / "embedding.onnx", memory_file,
                               lambda text: emit("status", text=text))
        active = {row.get("speaker") for row in data["segments"]}
        applied = {speaker: match for speaker, match in matches.items() if speaker in active and speaker not in protected}
        for speaker, match in applied.items():
            data["speakers"][speaker] = match["name"]
        data["remembered_speakers"] = applied
        if applied:
            emit("status", text=f"覚えた話者の名前を{len(applied)}人に反映しました。")
    except Exception as exc:
        data["speaker_memory_error"] = str(exc)
        emit("warning", title="覚えた話者の照合",
             message="登録名を照合できませんでした。照合を省略し、文字起こしを保存します。\n" + str(exc))
    return data


def run_overlap_job(request, job):
    from overlap_service import run_file
    data = request['result']
    folder = Path(request['result_folder']).resolve()
    media = resolve_cached_audio(data, folder)
    if not media:
        raise FileNotFoundError('保存した録音が見つかりません。結果フォルダー全体を戻してください。')
    wav = job / 'overlap-audio.wav'
    convert_audio(media, wav)
    # Reuse raw overlap regions retained at capture time when available.
    turns = data.get('diarization', {}).get('turns', [])
    if not data.get('overlap_review'):
        turns = diarize(wav, MODELS/'speakers', 0, lambda text: emit('status', text=text))
    run_file(data, wav, turns, folder, job, request, lambda text: emit('status', text=text))
    path = folder / f'overlap_{job.name[:12]}.json'
    save_result(data, folder)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), 'utf-8')
    emit('complete', result_path=str(path))


def run_speaker_job(request, job):
    data = request["result"]
    folder = Path(request["result_folder"]).resolve()
    cached = resolve_cached_audio(data, folder)
    if cached:
        media = cached
    else:
        source = str(data.get("source") or data.get("video_source") or "")
        try:
            url = youtube_url(source)
        except ValueError:
            from result_media import MEDIA_EXTENSIONS, result_file
            selected = request.get("selected_source")
            media = Path(selected) if selected else result_file(folder, source, MEDIA_EXTENSIONS)
            if not media or not media.is_file():
                raise FileNotFoundError("元の音声・動画ファイルが見つかりません。")
        else:
            media, _ = get_youtube_audio(url, job)
    wav = job / "speaker-audio.wav"
    emit("status", text="話者解析用の音声を準備中…")
    try:
        convert_audio(media, wav)
        turns = diarize(wav, MODELS / "speakers", request.get("num_speakers", 0),
                        lambda text: emit("status", text=text))
        segments = assign_speakers(data["segments"], turns)
        mapping = reuse_speaker_ids(data["segments"], segments)
        manual = dict(data.get("speaker_name_edits", {}))
        if "speaker_name_edits" not in data:
            manual.update({key: name for key, name in data.get("speakers", {}).items()
                           if name != default_name(key) and key not in data.get("remembered_speakers", {})})
        protected = {new: manual[old] for new, old in mapping.items() if old in manual}
        links = data.get("speaker_memory_links", {})
        for segment in segments:
            segment.pop("speaker_manual", None)
            for field in ("voice_correction_key", "voice_correction", "voice_learning_pending", "voice_rejected_profiles"):
                segment.pop(field, None)
        updated = {**data, "segments": segments, "speakers": normalize_names(segments, protected),
                   "speaker_name_edits": protected,
                   "speaker_memory_links": {new: links[old] for new, old in mapping.items() if old in links},
                   "audio_file": cache_audio(wav, folder),
                   "diarization": {"engine": "sherpa-onnx", "num_speakers": request.get("num_speakers", 0), "turns": turns}}
        updated.pop("speaker_error", None)
        apply_recorded_names(updated, wav, turns, request, protected)
        path = folder / f"speakers_{job.name[:12]}.json"
        path.write_text(json.dumps(updated, ensure_ascii=False, indent=2), encoding="utf-8")
        emit("complete", result_path=str(path))
    finally:
        wav.unlink(missing_ok=True)
        if media.parent == job:
            media.unlink(missing_ok=True)


def run_context_job(request, job):
    from context_ai import correct_result, write_json
    data = request["result"]
    folder = Path(request["result_folder"]).resolve()
    corrected = correct_result(data, normalize_rules(request.get("correction_rules", [])),
                               lambda text: emit("status", text=text))
    if corrected["context_last_run"]["changed_rows"]:
        original = folder / f"before_context_{job.name[:12]}.json"
        write_json(original, data)
        corrected["before_context"] = str(original)
    path = folder / f"context_{job.name[:12]}.json"
    write_json(path, corrected)
    emit("complete", result_path=str(path))


def run_job(request_path):
    from native_errors import handle_native_errors
    handle_native_errors()
    request_path = Path(request_path).resolve()
    job = request_path.parent
    request = json.loads(request_path.read_text(encoding="utf-8"))
    from context_ai import set_compute_mode
    set_compute_mode(request.get("compute", "auto"))
    if request.get("operation") == "compare":
        from comparison import run_comparison
        return run_comparison(request, job)
    if request.get("operation") == "realtime":
        from realtime import run_live
        return run_live(request, job)
    if request.get("operation") == "video":
        from video import run_render_job
        return run_render_job(request, job)
    if request.get("operation") == "diarize":
        return run_speaker_job(request, job)
    if request.get('operation') == 'overlap':
        return run_overlap_job(request, job)
    if request.get("operation") == "context":
        return run_context_job(request, job)
    correction_rules = normalize_rules(request.get("correction_rules", []))
    model_name = request.get("model", "small")
    if model_name not in ALLOWED_MODELS:
        raise ValueError("認識モデルの指定が不正です。")
    language = request.get("language", "ja")
    if language not in {"ja", "en", "auto"}:
        raise ValueError("言語の指定が不正です。")
    source = request["source"]
    if request["mode"] == "youtube":
        media, title = get_youtube_audio(source, job)
    else:
        media = Path(source).resolve()
        if not media.is_file():
            raise FileNotFoundError("音声・動画ファイルが見つかりません。")
        title = media.stem
    ffmpeg = tool_path('ffmpeg')
    if not ffmpeg.is_file():
        raise FileNotFoundError("音声変換ソフトが見つかりません。アプリの .tools フォルダーを確認してください。")
    emit("status", text="音声を文字起こし用に変換中…")
    wav = job / "audio.wav"
    convert_audio(media, wav)
    # Speaker detection is mandatory for new transcriptions, including requests from an older UI.
    turns = None
    speaker_error = None
    try:
        emit("status", text="最初に話者を判別しています…")
        turns = diarize(wav, MODELS / "speakers", request.get("num_speakers", 0),
                        lambda text: emit("status", text=text))
    except Exception as exc:
        speaker_error = str(exc)
    from asr import transcribe
    started = time.monotonic()
    result = transcribe(wav, model_name, language, request.get("prompt", "").strip(),
                        job, MODELS, request.get("compute", "auto"),
                        lambda text: emit("status", text=text))
    segments = result["segments"]
    device_name = result["device"]
    safe_title = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", title).strip(" .")[:65] or "transcript"
    folder = RESULTS / f"{datetime.now():%Y%m%d_%H%M%S}_{safe_title}_{job.name[:6]}"
    saved = {
        "title": title, "source": source, "source_mode": request["mode"], "language": result["language"],
        "model": model_name, "device": device_name, "backend": result["backend"],
        "backend_fallbacks": result.get("fallbacks", []),
        "prompt": request.get("prompt", ""), "segments": segments,
        "recognition_seconds": round(time.monotonic() - started, 2),
    }
    if result.get("timestamp_method"):
        saved["timestamp_method"] = result["timestamp_method"]
    del result
    folder.mkdir(parents=True, exist_ok=True)
    saved["audio_file"] = cache_audio(wav, folder)
    if turns is not None:
        emit("status", text="判別した話者を文字起こし結果に反映中…")
        saved["segments"] = assign_speakers(segments, turns)
        saved["speakers"] = normalize_names(saved["segments"])
        saved["diarization"] = {"engine": "sherpa-onnx", "num_speakers": request.get("num_speakers", 0), "turns": turns}
        # Keep completed speech recognition recoverable if name lookup is cancelled.
        save_result(saved, folder)
        emit("checkpoint", result_path=str(folder / "result.json"))
        apply_recorded_names(saved, wav, turns, request)
        if request.get('separate_overlap', True):
            from overlap_service import run_file
            try:
                run_file(saved, wav, turns, folder, job, request, lambda text: emit('status', text=text))
            except Exception as exc:
                saved['overlap_error'] = str(exc)
                emit('warning', title='声の重なり', message='音声分離は完了しませんでした。元の文字起こしを保存します。\n'+str(exc))
            save_result(saved, folder)
    else:
        saved["speaker_error"] = speaker_error
        emit("warning", message="話者判別に失敗したため、文字起こしのみ保存します。後で「話者を再判別」で再実行できます。\n\n" + str(speaker_error))
    if correction_rules and not request.get("context_correct", False):
        emit("status", text="誤変換辞書で文章を修正しています…")
        corrected, count, rows = replace_segments(saved["segments"], correction_rules)
        if rows:
            original_path = folder / "before_corrections.json"
            original_path.write_text(json.dumps(saved, ensure_ascii=False, indent=2), encoding="utf-8")
            saved["before_corrections"] = str(original_path)
        saved["segments"] = corrected
        saved["correction_rules"] = correction_rules
        saved["correction_count"] = count
    try:
        from manual_corrections import apply_to_result
        apply_to_result(saved)
    except (OSError, ValueError) as exc:
        saved["manual_memory_error"] = str(exc)
        emit("warning", title="修正の記憶", message="手動修正の記憶を読み込めません。文字起こしは保存します。\n"+str(exc))
    save_result(saved, folder)
    if request.get("context_correct", False):
        # Preserve the completed transcription even if AI correction fails or is cancelled.
        emit("checkpoint", result_path=str(folder / "result.json"))
        try:
            from context_ai import correct_result, write_json
            corrected = correct_result(saved, correction_rules, lambda text: emit("status", text=text))
            if corrected["context_last_run"]["changed_rows"]:
                original = folder / "before_context.json"
                write_json(original, saved)
                corrected["before_context"] = str(original)
            saved = corrected
        except Exception as exc:
            saved["context_error"] = str(exc)
            emit("warning", title="文脈AI校正について",
                 message="文字起こし結果は保存しました。AI校正は後で再実行できます。\n\n" + str(exc))
        save_result(saved, folder)
    # Only remove temporary audio created by this job; never remove the user's input.
    wav.unlink(missing_ok=True)
    if request["mode"] == "youtube" and media.parent == job:
        media.unlink(missing_ok=True)
    emit("complete", result_path=str(folder / "result.json"))


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("request")
    args = parser.parse_args()
    try:
        run_job(args.request)
    except Exception as error:
        traceback.print_exc()
        message = str(error)
        if error.__class__.__name__ == "OutOfMemoryError":
            message = "GPUの空きメモリが足りません。認識モデルをsmallまたはbaseにして再実行してください。"
        emit("error", message=message)
        raise SystemExit(1)
