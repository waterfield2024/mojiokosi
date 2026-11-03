"""Choose individual differences and save an independent, timed transcript."""
from copy import deepcopy
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
import shutil
from uuid import uuid4

from comparison_diff import _characters,difference_spans

SIDES = ("whisper","granite")


def fingerprint(results):
    content = {side:{key:results[side].get(key) for key in ("source","source_mode","segments","speakers")} for side in SIDES}
    return hashlib.sha256(json.dumps(content,sort_keys=True,ensure_ascii=False,separators=(",",":"),allow_nan=False).encode("utf-8")).hexdigest()


class MergePlan:
    def __init__(self,results):
        from comparison import aligned_blocks
        self.results = results
        for side in SIDES:aligned_blocks(results[side]["segments"],[])
        self.fingerprint = fingerprint(results)
        self.characters = {};self.positions = {};self.texts = {};self.units = {};self.row_ranges = {}
        for side in SIDES:
            chars,positions,texts = _characters(results[side]["segments"])
            self.characters[side],self.positions[side],self.texts[side] = chars,positions,texts
            self.row_ranges[side] = [[] for _ in texts]
            units = []
            for index,(row,start) in enumerate(positions):
                following = positions[index+1] if index+1<len(positions) else None
                end = following[1] if following and following[0]==row else len(texts[row])
                units.append((row,texts[row][start:end]))
            self.units[side] = units
        self.spans = difference_spans(self.characters["whisper"],self.characters["granite"])
        for number,span in enumerate(self.spans):
            for side,(start,end) in zip(SIDES,(span[:2],span[2:])):
                for row,char in self.positions[side][start:end]:
                    ranges = self.row_ranges[side][row]
                    if ranges and ranges[-1][1]==char and ranges[-1][2]==number:ranges[-1]=(ranges[-1][0],char+1,number)
                    else:ranges.append((char,char+1,number))

    def excerpt(self,index,side):
        span = self.spans[index];start,end = span[:2] if side=="whisper" else span[2:]
        return "".join(text for _,text in self.units[side][start:end]).strip()

    def validate_choices(self,choices,complete=False):
        if not isinstance(choices,list) or len(choices)!=len(self.spans) or any(choice not in (*SIDES,None) for choice in choices):
            raise ValueError("比較結果に合う選択内容ではありません。違う箇所を選び直してください。")
        if complete and None in choices:raise ValueError("すべての違いで、使うモデルを選んでください。")

    def _insertion_target(self,position,granite_row):
        positions = self.positions["whisper"]
        before = positions[position-1][0] if position else None
        after = positions[position][0] if position<len(positions) else None
        if before is not None and before==after:return ("whisper",before)
        row = self.results["granite"]["segments"][granite_row]
        candidates = []
        for index in dict.fromkeys((before,after)):
            if index is None:continue
            other = self.results["whisper"]["segments"][index]
            overlap = min(float(row["end"]),float(other["end"]))-max(float(row["start"]),float(other["start"]))
            if overlap>0:candidates.append((overlap,index))
        return ("whisper",max(candidates)[1]) if candidates else ("granite",granite_row)

    def combine(self,choices):
        """Keep Whisper caption slots; replacements are distributed across their
        affected slots in text order. Additional non-overlapping Granite speech
        keeps its own slot. Clear stale word timings whenever a slot is changed.
        """
        self.validate_choices(choices,complete=True)
        emitted = []
        def append(target,text):
            if emitted and emitted[-1][0]==target:emitted[-1][1].append(text)
            else:emitted.append((target,[text]))
        def original(start,end):
            for row,text in self.units["whisper"][start:end]:append(("whisper",row),text)
        position = 0
        for choice,(i,j,k,l) in zip(choices,self.spans):
            original(position,i)
            if choice=="whisper":original(i,j)
            else:
                for offset,(row,text) in enumerate(self.units["granite"][k:l]):
                    if i<j:
                        anchor = i+min(j-i-1,offset*(j-i)//max(1,l-k))
                        target = ("whisper",self.positions["whisper"][anchor][0])
                    else:target = self._insertion_target(i,row)
                    append(target,text)
            position = j
        original(position,len(self.units["whisper"]))
        segments = []
        for (side,index),pieces in emitted:
            text = "".join(pieces).strip()
            if not text:continue
            source = self.results[side]["segments"][index]
            row = deepcopy(source);row["text"] = text
            row.pop("segment_id",None)
            if text!=str(source["text"]).strip():row["words"] = []
            row["comparison_source"] = side
            segments.append(row)
        result = deepcopy(self.results["whisper"])
        for key in ("recognition_seconds","timestamp_method","backend_fallbacks","device","audio_file"):
            result.pop(key,None)
        result.update(title=str(result.get("title","文字起こし"))+"（比較から統合）",model="Whisper + Granite（選択して統合）",
                      backend="comparison/selected",segments=segments,comparison_raw=False,automatic_text_corrections=False,
                      speakers={**self.results["granite"].get("speakers",{}),**self.results["whisper"].get("speakers",{})},
                      comparison_merge={"version":1,"fingerprint":self.fingerprint,"choices":list(choices),"base":"whisper"})
        return result


def load_choices(path,plan):
    file = Path(path).parent/"comparison_choices.json"
    if not file.exists():return [None]*len(plan.spans),0
    data = json.loads(file.read_text("utf-8"))
    if data.get("version")!=1 or data.get("fingerprint")!=plan.fingerprint:
        raise ValueError("元の比較結果が変わったため、採用する文章を選び直してください。")
    choices = data.get("choices");plan.validate_choices(choices)
    index = data.get("current",0)
    if type(index) is not int or not 0<=index<max(1,len(choices)):index=0
    return choices,index


def save_choices(path,plan,choices,index):
    from context_ai import write_json
    plan.validate_choices(choices)
    write_json(Path(path).parent/"comparison_choices.json",{
        "version":1,"fingerprint":plan.fingerprint,"choices":choices,"current":index})


def save_merged(path,plan,choices):
    from comparison import read_comparison,result_path
    from engine import save_result,resolve_cached_audio
    path = Path(path);data = read_comparison(path)
    if any(entry["state"]!="complete" for entry in data["results"].values()):raise ValueError("両モデルの処理が完了してからまとめてください。")
    current = {side:json.loads(result_path(path,side).read_text("utf-8")) for side in SIDES}
    if fingerprint(current)!=plan.fingerprint:raise ValueError("元の結果が変更されました。比較結果を開き直して選び直してください。")
    result = plan.combine(choices)
    folder = path.parent/(datetime.now().strftime("merged_%Y%m%d_%H%M%S_")+uuid4().hex[:8]);folder.mkdir()
    for side in SIDES:
        cached = resolve_cached_audio(current[side],path.parent/side)
        if cached:
            target = folder/".audio"/cached.name;target.parent.mkdir()
            try:os.link(cached,target)
            except OSError:shutil.copy2(cached,target)
            result["audio_file"] = str(target);break
    result["comparison_merge"]["comparison_file"] = "../comparison.json"
    save_result(result,folder)
    return folder/"result.json"
