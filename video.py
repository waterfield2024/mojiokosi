"""Burn edited captions into an MP4. Runs in the existing cancellable worker."""
from __future__ import annotations
from platform_support import IS_MAC
from platform_support import tool_path

import json
import math
import os
import re
import struct
import subprocess
import unicodedata
from pathlib import Path

from engine import TOOLS, NO_WINDOW, emit, get_youtube_media, youtube_url
from speakers import speaker_name

# The same normalization is used for probing and encoding, including autorotation.
NORMALIZE = "scale=trunc(iw*sar/2)*2:trunc(ih/2)*2,setsar=1"


def inspect_video(media):
    command = [str(tool_path('ffmpeg')), "-nostdin", "-hide_banner",
               "-i", str(media), "-map", "0:V:0", "-vf", NORMALIZE,
               "-frames:v", "1", "-c:v", "png", "-f", "image2pipe", "pipe:1"]
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            creationflags=NO_WINDOW, timeout=90)
    detail = result.stderr.decode("utf-8", errors="replace")
    if result.returncode or not result.stdout.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("このファイルから映像を読み込めませんでした。音声だけのファイルではなく、元の動画を選んでください。\n" + detail[-1200:])
    width, height = struct.unpack(">II", result.stdout[16:24])
    duration_match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", detail)
    duration = None
    if duration_match:
        h, m, s = map(float, duration_match.groups())
        duration = h * 3600 + m * 60 + s
    return width, height, duration


def caption_font(size):
    from PIL import ImageFont
    if IS_MAC:
        from vertical_subtitles import font_at
        font = font_at(size)
        return font, font.getname()[0]
    fonts = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    for filename, family in (("meiryob.ttc", "Meiryo"), ("YuGothB.ttc", "Yu Gothic"),
                             ("msgothic.ttc", "MS Gothic")):
        path = fonts / filename
        if path.is_file():
            return ImageFont.truetype(str(path), size), family
    raise RuntimeError("日本語字幕に使うフォントがありません。Windowsにメイリオまたは游ゴシックが必要です。")


def clusters(text):
    """Keep combining marks and emoji joins with their preceding character."""
    result = []
    for char in text:
        if result and (unicodedata.combining(char) or char in "\ufe0e\ufe0f\u200d"
                       or result[-1].endswith("\u200d")):
            result[-1] += char
        else:
            result.append(char)
    return result


def wrap_caption(text, font, width):
    lines = []
    forbidden_start = "、。，．！？!?：；:;)]}）］｝」』】〉》ぁぃぅぇぉっゃゅょァィゥェォッャュョー"
    forbidden_end = "([{（［｛「『【〈《"
    for paragraph in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        paragraph = re.sub(r"[^\S\n]+", " ", paragraph).strip()
        chars = clusters(paragraph)
        while chars:
            end = 0
            while end < len(chars) and font.getlength("".join(chars[:end + 1])) <= width:
                end += 1
            end = max(1, end)
            if end < len(chars):
                # Prefer a word or punctuation boundary if it does not waste most of the row.
                boundaries = [i + 1 for i in range(end)
                              if chars[i].isspace() or chars[i][-1] in "、。！？!?;:,"]
                eligible = [i for i in boundaries if i >= end * .55]
                if eligible:
                    end = eligible[-1]
                natural_end = end
                while end > 1 and (chars[end][0] in forbidden_start or chars[end - 1][-1] in forbidden_end):
                    end -= 1
                # Long small-kana/punctuation runs cannot obey kinsoku at every
                # boundary. Keep useful line lengths instead of one glyph/page.
                if end < natural_end * .55:end = natural_end
            line = "".join(chars[:end]).strip()
            if line:
                lines.append(line)
            chars = chars[end:]
            while chars and chars[0].isspace():
                chars.pop(0)
    return lines


def make_cues(segments, width, height, duration=None, speakers=None, font_scale=1.0):
    from caption_timing import timed_segments
    font_size = max(8, round(max(12, min(height * .058, width * .075)) * font_scale))
    font, family = caption_font(font_size)
    margin = max(8, round(width * .05))
    # Extra room absorbs differences between Pillow and libass font metrics.
    available = (width - 2 * margin) * .90
    clean = []
    separated = []
    for segment in timed_segments(segments, duration):
        if segment.get('overlap_separated'):
            separated.append(segment)
            continue
        start, end = float(segment["start"]), float(segment["end"])
        if not (math.isfinite(start) and math.isfinite(end)):
            raise ValueError("字幕の時刻が不正です。")
        start = max(0, start)
        if duration is not None:
            end = min(duration, end)
        text = str(segment["text"]).strip()
        if end > start and text:
            clean.append((start, end, text, speaker_name(segment.get("speaker"), speakers), segment["_caption_index"]))
    clean.sort(key=lambda s: s[0])
    cues = []
    for index, (start, end, text, name, segment_index) in enumerate(clean):
        # Keep only one caption on screen even if input timestamps overlap.
        if index + 1 < len(clean):
            end = min(end, clean[index + 1][0])
        start_cs, end_cs = round(start * 100), round(end * 100)
        if end_cs <= start_cs:
            continue
        lines = wrap_caption(text, font, available)
        # Names repeat on every page; name + speech still uses at most two rows.
        pages = [[name, line] for line in lines] if name else [lines[i:i + 2] for i in range(0, len(lines), 2)]
        name_size = None
        if name:
            name_size = min(font_size * .85, font_size * available / max(1, font.getlength(name)))
            name_size = max(1., round(name_size, 2))
        if not pages:
            continue
        if len(pages) > end_cs - start_cs:
            from caption_timing import CaptionProblem
            raise CaptionProblem(segment_index, f"字幕 {segment_index+1} の表示時間（{(end_cs-start_cs)/100:.2f}秒）が短すぎます。文章か開始・終了時刻を調整してください。")
        weights = [max(1, sum(len(line) for line in (page[1:] if name else page))) for page in pages]
        total = sum(weights)
        consumed = 0
        cursor = start_cs
        for page_index, (page, weight) in enumerate(zip(pages, weights)):
            consumed += weight
            boundary = start_cs + round((end_cs - start_cs) * consumed / total)
            boundary = max(cursor + 1, min(boundary, end_cs - (len(pages) - page_index - 1)))
            cues.append({"start_cs": cursor, "end_cs": boundary, "lines": page, "speaker_font_size": name_size,
                         "segment_index": segment_index})
            cursor = boundary
    if separated:
        from overlap_captions import cues as overlap_cues
        def named_lines(text, name):
            while len(name) > 1 and font.getlength(name+'：') > available*.45:
                name = name[:-2]+'…' if name.endswith('…') else name[:-1]+'…'
            prefix = name+'：' if name else ''
            return [prefix+line for line in wrap_caption(text, font, max(font_size*2, available-font.getlength(prefix)))]
        cues.extend(overlap_cues(separated, named_lines, speakers))
        cues.sort(key=lambda cue: cue['start_cs'])
    return cues, font_size, family, margin


def ass_time(centiseconds):
    seconds, fraction = divmod(centiseconds, 100)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02}:{seconds:02}.{fraction:02}"


def ass_escape(text):
    # Literal user text must never become ASS override tags or extra line breaks.
    # Fullwidth equivalents render safely, even on libass versions with different escaping.
    return text.replace("\\", "＼").replace("{", "｛").replace("}", "｝")


def write_ass(segments, path, width, height, duration=None, speakers=None):
    cues, size, family, margin = make_cues(segments, width, height, duration, speakers)
    if not cues:
        raise ValueError("この動画に表示できる字幕がありません。文字起こし結果と動画の長さを確認してください。")
    bottom = max(8, round(height * .05))
    outline = max(1.2, round(size * .075, 2))
    header = (
        "[Script Info]\nScriptType: v4.00+\n"
        f"PlayResX: {width}\nPlayResY: {height}\n"
        "WrapStyle: 2\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, "
        "Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, "
        "Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,{family},{size},&H00FFFFFF,&H00FFFFFF,&H00000000,&H00000000,"
        f"-1,0,0,0,100,100,0,0,1,{outline},0,2,{margin},{margin},{bottom},1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    events = []
    for cue in cues:
        if cue.get("speaker_font_size"):
            text = "{\\fs" + str(cue["speaker_font_size"]) + "}" + ass_escape(cue["lines"][0])
            text += "{\\fs" + str(size) + "}\\N" + ass_escape(cue["lines"][1])
        else:
            text = r"\N".join(ass_escape(line) for line in cue["lines"])
        events.append(f"Dialogue: 0,{ass_time(cue['start_cs'])},{ass_time(cue['end_cs'])},Default,,0,0,0,,{text}")
    Path(path).write_text(header + "\n".join(events) + "\n", encoding="utf-8-sig")
    return cues


def partial_output(output, job):
    output = Path(output).resolve()
    return output.with_name(f".{output.stem}.{Path(job).name}.partial.mp4")


def get_source(result, folder, *, selected_source=None):
    if result.get("video_export_mode") == "audio_subtitles":
        from audio_video import prepare_audio_video
        return prepare_audio_video(result, folder)
    from screen_capture import recorded_video
    recorded = recorded_video(result, folder)
    if recorded and result.get("video_export_mode") in (None, "recorded_screen"):
        return recorded
    source = str(result.get("video_source") or result.get("source") or "").strip()
    try:
        url = youtube_url(source)
    except ValueError:
        from result_media import MEDIA_EXTENSIONS, result_file
        path = Path(selected_source) if selected_source else result_file(folder, source, MEDIA_EXTENSIONS)
        if not path or not path.is_file():
            raise FileNotFoundError("元動画が見つかりません。「字幕付きMP4を保存」から元動画を選び直してください。")
        return path
    video_id = url.rsplit("=", 1)[-1]
    cache = Path(folder) / ".video" / video_id
    cache.mkdir(parents=True, exist_ok=True)
    manifest = cache / "video.json"
    if manifest.is_file():
        try:
            name = json.loads(manifest.read_text("utf-8"))["filename"]
            from result_media import MEDIA_EXTENSIONS, result_file
            candidate = result_file(cache, name, MEDIA_EXTENSIONS)
            if candidate and candidate.parent == cache.absolute():
                emit("status", text="取得済みの元動画を使用します…")
                return candidate
        except (ValueError, KeyError, OSError):
            pass
    media, _ = get_youtube_media(url, cache, video=True)
    manifest.write_text(json.dumps({"filename": media.name}, ensure_ascii=False), encoding="utf-8")
    return media


def run_render_job(request, job):
    job = Path(job).resolve()
    result = request["result"]
    output = Path(request["output"]).resolve()
    if output.suffix.lower() != ".mp4":
        raise ValueError("保存先の拡張子は .mp4 にしてください。")
    folder = Path(request["result_folder"]).resolve()
    prepared = request.get("prepared_video")
    source = Path(prepared).resolve() if prepared and Path(prepared).is_file() else get_source(result, folder)
    if source.resolve() == output:
        raise ValueError("元動画と同じファイル名には保存できません。別の名前を指定してください。")
    emit("status", text="元動画のサイズと長さを確認中…")
    width, height, duration = inspect_video(source)
    direction = request.get("caption_direction", "horizontal")
    if direction not in {"horizontal", "vertical"}:
        raise ValueError("字幕の向きが正しくありません。")
    position = request.get("caption_position", "right")
    if position not in {"left", "right"}:
        raise ValueError("字幕の位置が正しくありません。")
    if request.get("caption_layout") is not None:
        from video_layout import CaptionLayout
        layout = CaptionLayout(result, width, height, duration, request["caption_layout"])
        emit("status", text="プレビューで調整した字幕を準備中…")
        cues = layout.write_track(job)
        filters = ["-f", "concat", "-safe", "0", "-i", "layout.ffconcat",
                   "-filter_complex", f"[0:V:0]{NORMALIZE}[base];[1:v]format=rgba[subs];[base][subs]overlay=x=0:y=0:eof_action=pass[out]",
                   "-map", "[out]", "-map", "0:a:0?", "-fps_mode", "passthrough"]
    elif direction == "vertical":
        from vertical_subtitles import write_video_track
        emit("status", text="縦書き字幕を準備中…")
        cues = write_video_track(result["segments"], job, width, height, duration, result.get("speakers"))
        margin = max(8, round(width*.05))
        x = str(margin) if position == "left" else f"W-w-{margin}"
        filters = ["-f", "concat", "-safe", "0", "-i", "vertical.ffconcat",
                   "-filter_complex", f"[0:V:0]{NORMALIZE}[base];[1:v]format=rgba[subs];[base][subs]overlay=x={x}:y=(H-h)/2:eof_action=pass[out]",
                   "-map", "[out]", "-map", "0:a:0?", "-fps_mode", "passthrough"]
    else:
        cues = write_ass(result["segments"], job / "captions.ass", width, height, duration, result.get("speakers"))
        filters = ["-map", "0:V:0", "-map", "0:a:0?", "-vf", NORMALIZE+",ass=filename=captions.ass"]
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = partial_output(output, job)
    command = [
        str(tool_path('ffmpeg')), "-nostdin", "-hide_banner", "-nostats", "-n",
        "-i", str(source), *filters,
        "-c:v", "libx264", "-preset", "fast", "-crf", "20", "-pix_fmt", "yuv420p",
        "-threads", str(min(8, os.cpu_count() or 4)),
        "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart",
        "-map_metadata", "-1", "-map_chapters", "-1", "-sn", "-dn",
        "-progress", "pipe:1", str(temporary),
    ]
    emit("status", text=f"字幕付きMP4を書き出し中… {width} × {height} / {len(cues)}字幕")
    try:
        with (job / "ffmpeg.log").open("w", encoding="utf-8") as log:
            process = subprocess.Popen(command, cwd=job, stdout=subprocess.PIPE, stderr=log,
                                       text=True, encoding="utf-8", errors="replace",
                                       creationflags=NO_WINDOW)
            try:
                for line in process.stdout:
                    if line.startswith("out_time_us="):
                        try:
                            seconds = max(0, int(line.split("=", 1)[1]) / 1000000)
                        except ValueError:
                            continue
                        if duration and duration > 0:
                            percent = min(99, round(100 * seconds / duration))
                            emit("status", text=f"字幕付きMP4を書き出し中… {percent}%")
                        else:
                            emit("status", text=f"字幕付きMP4を書き出し中… {int(seconds)}秒まで処理")
                code = process.wait()
            finally:
                process.stdout.close()
                if process.poll() is None:
                    process.terminate()
                    process.wait()
        if code or not temporary.is_file() or temporary.stat().st_size == 0:
            detail = (job / "ffmpeg.log").read_text("utf-8", errors="replace")[-1800:]
            raise RuntimeError("動画を書き出せませんでした。\n" + detail)
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    emit("video_complete", output_path=str(output))
