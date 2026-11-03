"""Identical subtitle bitmaps and placement for MP4 preview and final encoding."""
from bisect import bisect_right
from collections import OrderedDict
import math
from pathlib import Path
from PIL import Image, ImageDraw


def normalize_layout(value=None, direction="horizontal", position="right"):
    value = {} if value is None else value
    if not isinstance(value, dict):
        raise ValueError("字幕レイアウトの設定が正しくありません。")
    result = {"version": 1, "direction": value.get("direction", direction),
              "position": value.get("position", position), "font_scale": value.get("font_scale", 1.0),
              "x": value.get("x"), "y": value.get("y")}
    if result["direction"] not in ("horizontal", "vertical") or result["position"] not in ("left", "right"):
        raise ValueError("字幕の向き・位置が正しくありません。")
    for key, low, high in (("font_scale", .5, 2.), ("x", 0., 1.), ("y", 0., 1.)):
        number = result[key]
        if number is None and key != "font_scale":
            continue
        if isinstance(number, bool) or not isinstance(number, (float, int)) or not math.isfinite(number) or not low <= number <= high:
            raise ValueError("字幕の文字サイズ・座標が範囲外です。")
        result[key] = float(number)
    return result


class CaptionLayout:
    def __init__(self, result, width, height, duration=None, settings=None):
        from video import make_cues
        from vertical_subtitles import video_cues
        self.width, self.height, self.duration = int(width), int(height), duration
        self.settings = normalize_layout(settings)
        self.cache = OrderedDict()
        if self.settings["direction"] == "vertical":
            self.cues, self.size, self.box_w, self.box_h = video_cues(
                result["segments"], width, height, duration, result.get("speakers"), self.settings["font_scale"])
        else:
            self.cues, self.size, _, _ = make_cues(
                result["segments"], width, height, duration, result.get("speakers"), self.settings["font_scale"])
        if not self.cues:
            from caption_timing import CaptionProblem
            raise CaptionProblem(None, "この動画に表示できる字幕がありません。字幕の時刻と動画の長さを確認してください。")
        self.starts = [cue["start_cs"] for cue in self.cues]

    def index_at(self, seconds):
        time_cs = seconds * 100
        index = bisect_right(self.starts, time_cs) - 1
        return index if index >= 0 and time_cs < self.cues[index]["end_cs"] else None

    def bitmap(self, index):
        if index in self.cache:
            self.cache.move_to_end(index)
            return self.cache[index]
        cue = self.cues[index]
        if self.settings["direction"] == "vertical":
            from vertical_subtitles import render_columns
            image = render_columns(cue["lines"], self.size, self.box_w, self.box_h, cue["name_first"])
            bounds = image.getbbox()
            if bounds:
                image = image.crop(bounds)
        else:
            from video import caption_font
            rows = []
            for i, text in enumerate(cue["lines"]):
                size = max(1, round(cue["speaker_font_size"])) if i == 0 and cue.get("speaker_font_size") else self.size
                font, _ = caption_font(size)
                stroke = max(1, round(self.size*.075))
                box = font.getbbox(text, stroke_width=stroke)
                row = Image.new("RGBA", (max(1, box[2]-box[0]+4), max(1, box[3]-box[1]+4)))
                ImageDraw.Draw(row).text((2-box[0], 2-box[1]), text, font=font, fill="white", stroke_width=stroke, stroke_fill="black")
                rows.append(row)
            gap = max(4, round(self.size*.24))
            image = Image.new("RGBA", (max(row.width for row in rows), sum(row.height for row in rows)+gap*(len(rows)-1)))
            y = 0
            for row in rows:
                image.alpha_composite(row, ((image.width-row.width)//2, y)); y += row.height+gap
        if image.width > self.width or image.height > self.height:
            scale = min(self.width/image.width, self.height/image.height)
            image = image.resize((max(1, int(image.width*scale)), max(1, int(image.height*scale))), Image.Resampling.LANCZOS)
        self.cache[index] = image
        if len(self.cache) > 12:
            self.cache.popitem(last=False)
        return image

    def placement(self, image):
        settings = self.settings
        if settings["x"] is not None:
            x = self.width*settings["x"]-image.width/2
        elif settings["direction"] == "vertical":
            margin = max(8, round(self.width*.05))
            x = margin if settings["position"] == "left" else self.width-image.width-margin
        else:
            x = (self.width-image.width)/2
        if settings["y"] is not None:
            y = self.height*settings["y"]-image.height/2
        elif settings["direction"] == "vertical":
            y = (self.height-image.height)/2
        else:
            y = self.height-image.height-max(8, round(self.height*.05))
        return round(max(0, min(self.width-image.width, x))), round(max(0, min(self.height-image.height, y)))

    def layer(self, index):
        layer = Image.new("RGBA", (self.width, self.height))
        if index is not None:
            bitmap = self.bitmap(index)
            layer.alpha_composite(bitmap, self.placement(bitmap))
        return layer

    def write_track(self, job):
        job = Path(job); (job/"layout").mkdir(exist_ok=True)
        blank = "layout/blank.png"
        self.layer(None).save(job/blank)
        timeline, cursor = [], 0
        for index, cue in enumerate(self.cues):
            if cue["start_cs"] > cursor:
                timeline.append((blank, cue["start_cs"]-cursor))
            name = f"layout/caption-{index:06d}.png"
            self.layer(index).save(job/name)
            timeline.append((name, cue["end_cs"]-cue["start_cs"]))
            cursor = cue["end_cs"]
        timeline.append((blank, max(1, round((self.duration or cursor/100)*100)-cursor)))
        lines = ["ffconcat version 1.0"]
        for name, span in timeline:
            lines += [f"file '{name}'", "option framerate 100", f"duration {span/100:.2f}"]
        lines += [f"file '{blank}'", "option framerate 100"]
        (job/"layout.ffconcat").write_text("\n".join(lines)+"\n", "utf-8")
        return self.cues
