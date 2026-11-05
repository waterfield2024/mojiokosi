"""Shared vertical Japanese layout for the live overlay and burned-in video."""
from platform_support import IS_MAC
from functools import lru_cache
import json
import math
import os
from pathlib import Path
import re
import unicodedata

DIRECTIONS = {"横書き": "horizontal", "縦書き": "vertical"}
POSITIONS = {"右側": "right", "左側": "left"}
ROTATE = set("ー―−‐－-…‥（）()［］[]｛｝{}「」『』【】〈〉《》〔〕：:；;")
SMALL = set("ぁぃぅぇぉっゃゅょァィゥェォッャュョヵヶ")
FORBIDDEN_START = set("、。，．！？!?）)]}］｝」』】〉》" + "".join(SMALL) + "ー")
FORBIDDEN_END = set("（([{［｛「『【〈《")


def settings_path():
    from user_data import data_path
    return data_path("caption_settings.json", "WHISPER_CAPTION_SETTINGS")


def load_settings():
    try:
        data = json.loads(settings_path().read_text("utf-8-sig"))
        if not isinstance(data, dict) or data.get("direction") not in DIRECTIONS.values():
            raise ValueError("字幕の向きの設定形式が正しくありません。")
        position = data.get("vertical_position", "right")
        if position not in POSITIONS.values():
            raise ValueError("字幕の位置の設定形式が正しくありません。")
        return {"direction": data["direction"], "vertical_position": position}
    except FileNotFoundError:
        return {"direction": "horizontal", "vertical_position": "right"}


def load_direction():
    return load_settings()["direction"]


def save_settings(direction, vertical_position):
    if direction not in DIRECTIONS.values():
        raise ValueError("字幕の向きが正しくありません。")
    if vertical_position not in POSITIONS.values():
        raise ValueError("字幕の位置が正しくありません。")
    from context_ai import write_json
    write_json(settings_path(), {"version": 2, "direction": direction, "vertical_position": vertical_position})


def save_direction(value):
    save_settings(value, load_settings()["vertical_position"])


@lru_cache(maxsize=24)
def font_at(size):
    from PIL import ImageFont
    if IS_MAC:
        for path in (Path('/System/Library/Fonts/ヒラギノ角ゴシック W6.ttc'), Path('/System/Library/Fonts/ヒラギノ角ゴシック W3.ttc'), Path('/System/Library/Fonts/ヒラギノ丸ゴ ProN W4.ttc')):
            if path.is_file(): return ImageFont.truetype(str(path), max(8, int(size)))
        raise RuntimeError('Macの日本語フォントが見つかりません。')
    fonts = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts"
    for name in ("meiryob.ttc", "YuGothB.ttc", "msgothic.ttc"):
        if (fonts / name).is_file():
            return ImageFont.truetype(str(fonts / name), max(8, int(size)))
    raise RuntimeError("日本語字幕のフォントが見つかりません。")


def cell_size(size):
    return max(12, math.ceil(size * 1.16))


def units(text, size, limit):
    """Keep combining sequences together; rotate Latin runs, with two-digit tate-chu-yoko."""
    font = font_at(size)
    chars = []
    for char in " ".join(str(text).split()):
        if chars and (unicodedata.combining(char) or char in "\ufe0e\ufe0f\u200d" or chars[-1].endswith("\u200d")):
            chars[-1] += char
        else:
            chars.append(char)
    result, i = [], 0
    while i < len(chars):
        char = chars[i]
        if re.fullmatch(r"[A-Za-z0-9]", char):
            end = i+1
            while end < len(chars) and re.fullmatch(r"[A-Za-z0-9]", chars[end]):
                end += 1
            run = "".join(chars[i:end])
            if run.isdigit() and len(run) <= 2:
                result.append((run, "upright", cell_size(size)))
            else:
                piece = ""
                for letter in run:
                    if piece and font.getlength(piece+letter)+size*.18 > limit:
                        result.append((piece, "rotate", max(cell_size(size), math.ceil(font.getlength(piece)+size*.18))))
                        piece = ""
                    piece += letter
                if piece:
                    result.append((piece, "rotate", max(cell_size(size), math.ceil(font.getlength(piece)+size*.18))))
            i = end
        else:
            result.append((char, "rotate" if char in ROTATE else "upright", cell_size(size)))
            i += 1
    return result


def wrap_columns(text, size, height):
    remaining = units(text, size, height)
    output = []
    while remaining:
        used, end = 0, 0
        while end < len(remaining) and used+remaining[end][2] <= height:
            used += remaining[end][2]
            end += 1
        end = max(1, end)
        if end < len(remaining):
            natural_end = end
            while end > 1 and (remaining[end][0][0] in FORBIDDEN_START or remaining[end-1][0][-1] in FORBIDDEN_END):
                end -= 1
            if end < natural_end * .55:end = natural_end
        output.append("".join(u[0] for u in remaining[:end]))
        remaining = remaining[end:]
    return output


def pages(text, size, height, columns=2):
    lines = wrap_columns(text, size, height)
    return ["\n".join(lines[i:i+columns]) for i in range(0, len(lines), columns)]


def fit_name(name, size, height):
    items = units(name, size, height)
    if sum(u[2] for u in items) <= height:
        return name
    while items and sum(u[2] for u in items)+cell_size(size) > height:
        items.pop()
    return "".join(u[0] for u in items)+"…"


@lru_cache(maxsize=1024)
def glyph(text, orientation, size):
    from PIL import Image, ImageDraw
    font = font_at(size)
    stroke = max(1, round(size*.07))
    box = font.getbbox(text, stroke_width=stroke)
    image = Image.new("RGBA", (max(1, box[2]-box[0]+4), max(1, box[3]-box[1]+4)))
    ImageDraw.Draw(image).text((2-box[0], 2-box[1]), text, font=font,
                              fill="white", stroke_width=stroke, stroke_fill="black")
    if orientation == "rotate":
        image = image.transpose(Image.Transpose.ROTATE_270)
    return image


def render_columns(columns, size, width, height, name_first=False):
    from PIL import Image
    image = Image.new("RGBA", (width, height))
    pad, cell = 12, cell_size(size)
    available = max(cell, height-2*pad)
    layouts = []
    for index, text in enumerate(columns[:2]):
        current_size = max(8, round(size*.85)) if name_first and index == 0 else size
        text = fit_name(text, current_size, available) if name_first and index == 0 else text
        layouts.append((current_size, units(text, current_size, available)))
    used = max((sum(u[2] for u in items) for _, items in layouts), default=0)
    top = max(pad, (height-used)/2)
    # Columns are read from right to left; name occupies the right-hand column.
    for index, (current_size, items) in enumerate(layouts):
        x = width-pad-cell/2-index*(cell+size*.18)
        y = top
        for text, orientation, advance in items:
            bitmap = glyph(text, orientation, current_size)
            max_width = cell*.96
            if bitmap.width > max_width or bitmap.height > advance:
                ratio = min(max_width/bitmap.width, advance/bitmap.height)
                bitmap = bitmap.resize((max(1, round(bitmap.width*ratio)), max(1, round(bitmap.height*ratio))), Image.Resampling.LANCZOS)
            dx, dy = x-bitmap.width/2, y+(advance-bitmap.height)/2
            if text in "、。，．":
                dx, dy = x+cell*.02, y+cell*.03
            elif text in SMALL:
                dx += cell*.1; dy -= cell*.1
            image.alpha_composite(bitmap, (round(dx), round(dy)))
            y += advance
    return image


def box_width(size):
    return math.ceil(2*cell_size(size)+size*.18+24)


def video_cues(segments, width, height, duration=None, speakers=None, font_scale=1.0):
    from speakers import speaker_name
    from caption_timing import timed_segments
    size = max(8, round(max(12, min(height*.052, width*.075)) * font_scale))
    box_height = max(cell_size(size)+24, round(height*.82))
    rows = []
    separated = []
    for row in timed_segments(segments, duration):
        if row.get('overlap_separated'):
            separated.append(row)
            continue
        start, end = float(row["start"]), float(row["end"])
        if not math.isfinite(start) or not math.isfinite(end):
            raise ValueError("字幕の時刻が不正です。")
        start = max(0, start); end = min(end, duration) if duration is not None else end
        if end > start and row["text"].strip():
            rows.append((start, end, row["text"], speaker_name(row.get("speaker"), speakers), row["_caption_index"]))
    rows.sort(key=lambda row: row[0])
    cues = []
    for i, (start, end, text, name, segment_index) in enumerate(rows):
        if i+1 < len(rows):end = min(end, rows[i+1][0])
        a, b = round(start*100), round(end*100)
        if b <= a:continue
        parts = pages(text, size, box_height-24, 1 if name else 2)
        if len(parts) > b-a:
            from caption_timing import CaptionProblem
            raise CaptionProblem(segment_index, f"字幕 {segment_index+1} の表示時間（{(b-a)/100:.2f}秒）が短すぎます。文章か開始・終了時刻を調整してください。")
        total = sum(len(p.replace("\n", "")) for p in parts)
        used, cursor = 0, a
        for j, part in enumerate(parts):
            used += len(part.replace("\n", ""))
            boundary = max(cursor+1, min(a+round((b-a)*used/total), b-(len(parts)-j-1)))
            cues.append({"start_cs": cursor, "end_cs": boundary,
                         "lines": [name, part] if name else part.split("\n"), "name_first": bool(name),
                         "segment_index": segment_index})
            cursor = boundary
    if separated:
        from overlap_captions import cues as overlap_cues
        def named_columns(text, name):
            prefix = fit_name(name, size, (box_height-24)*.35)+'：' if name else ''
            used = sum(u[2] for u in units(prefix, size, box_height-24))
            return [prefix+part for part in pages(text, size, max(cell_size(size)*2, box_height-24-used), 1)]
        cues.extend(overlap_cues(separated, named_columns, speakers, vertical=True))
        cues.sort(key=lambda cue: cue['start_cs'])
    return cues, size, box_width(size), box_height


def write_video_track(segments, job, width, height, duration=None, speakers=None):
    """Sparse PNG frames at subtitle boundaries; source video keeps its own cadence."""
    from PIL import Image
    job = Path(job)
    cues, size, box_w, box_h = video_cues(segments, width, height, duration, speakers)
    if not cues:
        raise ValueError("この動画に表示できる字幕がありません。")
    (job / "vertical").mkdir(exist_ok=True)
    blank = "vertical/blank.png"
    Image.new("RGBA", (box_w, box_h)).save(job / blank)
    timeline, cursor = [], 0
    for i, cue in enumerate(cues):
        if cue["start_cs"] > cursor:timeline.append((blank, cue["start_cs"]-cursor))
        filename = f"vertical/caption-{i:06d}.png"
        render_columns(cue["lines"], size, box_w, box_h, cue["name_first"]).save(job / filename)
        timeline.append((filename, cue["end_cs"]-cue["start_cs"]))
        cursor = cue["end_cs"]
    timeline.append((blank, max(1, round((duration or cursor/100)*100)-cursor)))
    lines = ["ffconcat version 1.0"]
    for filename, span in timeline:
        lines += [f"file '{filename}'", "option framerate 100", f"duration {span/100:.2f}"]
    lines += [f"file '{blank}'", "option framerate 100"]
    (job / "vertical.ffconcat").write_text("\n".join(lines)+"\n", "utf-8")
    return cues
