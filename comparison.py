"""Run both recognizers on one prepared recording and preserve independent raw results."""
from copy import deepcopy
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import time
import wave

from recognition_models import GRANITE_MODEL

WHISPER_CHOICES = {"Whisper small（標準）": "small", "Whisper base（軽量）": "base", "Whisper turbo": "turbo"}
SIDES = ("whisper", "granite")


def seconds_text(value):
    if value is None:return "—"
    value = float(value)
    if not math.isfinite(value) or value < 0:return "—"
    tenths = round(value*10)
    minutes, rest = divmod(tenths,600)
    return f"{minutes}分{rest/10:.1f}秒" if minutes else f"{rest/10:.1f}秒"


def read_comparison(path):
    data = json.loads(Path(path).read_text("utf-8"))
    if not isinstance(data,dict) or data.get("kind") != "model_comparison" or data.get("version") != 1:
        raise ValueError("このアプリの比較結果ではありません。")
    if not isinstance(data.get("results"),dict) or set(data["results"]) != set(SIDES):
        raise ValueError("比較結果のモデル情報が不正です。")
    for side,entry in data["results"].items():
        if not isinstance(entry,dict) or entry.get("state") not in {"waiting","running","complete","failed","interrupted"}:
            raise ValueError("比較結果の状態が不正です。")
        if entry.get("result_path") not in (None,f"{side}/result.json"):
            raise ValueError("比較結果の保存先が不正です。")
    return data


def result_path(path,side):
    base = Path(path).resolve().parent
    target = (base/side/"result.json").resolve()
    if side not in SIDES or not target.is_relative_to(base):raise ValueError("結果の保存先が不正です。")
    return target


def interrupt_comparison(path, elapsed=None, error=None):
    from context_ai import write_json
    data = read_comparison(path)
    if data.get("state") in {"complete","partial","failed","interrupted"}:return data
    data["state"] = "failed" if error else "interrupted"
    for entry in data["results"].values():
        if entry["state"] in {"waiting","running"}:entry["state"] = "interrupted"
    if elapsed is not None:data["total_seconds"] = max(0,float(elapsed))
    if error:data["error"] = str(error)
    write_json(path,data)
    return data


def aligned_blocks(left,right):
    """Group overlapping time intervals; never drop, duplicate or rewrite speech."""
    events = []
    for side,rows in (("whisper",left),("granite",right)):
        for order,row in enumerate(rows):
            start,end = float(row["start"]),float(row["end"])
            if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end < start:
                raise ValueError("比較する字幕の時刻が不正です。")
            if str(row["text"]).strip():events.append((start,max(start+.001,end),side,order,row))
    blocks = []
    for start,end,side,order,row in sorted(events,key=lambda e:(e[0],e[2],e[3])):
        if not blocks or start >= blocks[-1]["end"]-.0001:
            blocks.append({"start":start,"end":end,"whisper":[],"granite":[]})
        block = blocks[-1];block["end"] = max(block["end"],end);block[side].append(row)
    return blocks


def run_comparison(request,job):
    import engine
    from asr import prepare_whisper_model,transcribe
    from granite_model import ensure_granite
    from context_ai import write_json
    from speakers import assign_speakers,normalize_names
    whisper = request.get("whisper_model","small")
    if whisper not in WHISPER_CHOICES.values():raise ValueError("比較するWhisperモデルが正しくありません。")
    language = request.get("language","ja")
    if language not in {"ja","en","auto"}:raise ValueError("言語の指定が不正です。")
    if request.get("mode") not in {"file","youtube"}:raise ValueError("動画・音声の指定が不正です。")
    compute = request.get("compute","auto")
    if compute not in {"auto","gpu","cpu"}:raise ValueError("処理方式が正しくありません。")
    source = str(request["source"])
    if request["mode"] == "youtube":source = engine.youtube_url(source)
    elif not Path(source).is_file():raise FileNotFoundError("音声・動画ファイルが見つかりません。")
    job = Path(job);job.mkdir(parents=True,exist_ok=True)
    folder = engine.RESULTS/f"{datetime.now():%Y%m%d_%H%M%S}_モデル比較_{job.name[:8]}"
    folder.mkdir(parents=True,exist_ok=True)
    path = folder/"comparison.json"
    started = time.perf_counter()
    data = {"kind":"model_comparison","version":1,"state":"preparing","created_at":datetime.now().astimezone().isoformat(),
            "source":source,"source_mode":request["mode"],"title":Path(source).name if request["mode"]=="file" else "YouTube",
            "compute":compute,"whisper_language":language,"num_speakers":request.get("num_speakers",0),
            "automatic_text_corrections":False,"prompt":"","shared_seconds":None,"total_seconds":None,
            "results":{side:{"model":model,"state":"waiting","recognition_seconds":None}
                       for side,model in (("whisper",whisper),("granite",GRANITE_MODEL))}}
    def publish():
        write_json(path,data)
        engine.emit("comparison_update",comparison_path=str(path))
    def status(text):engine.emit("status",text=text)
    publish()
    media = None;wav = job/"comparison-audio.wav"
    try:
        status("比較用の音声を準備しています…")
        if request["mode"] == "youtube":media,data["title"] = engine.get_youtube_audio(source,job)
        else:media = Path(source).resolve();data["title"] = media.stem
        engine.convert_audio(media,wav)
        with wave.open(str(wav),"rb") as audio:data["audio_seconds"] = audio.getnframes()/audio.getframerate()
        digest = hashlib.sha256()
        with wav.open("rb") as stream:
            while block:=stream.read(1024*1024):digest.update(block)
        data["audio_sha256"] = digest.hexdigest()
        cached = engine.cache_audio(wav,folder)
        # Download/check both models outside the recognition timers.
        for side,prepare in (("whisper",lambda:prepare_whisper_model(whisper,engine.MODELS,status)),
                             ("granite",lambda:ensure_granite(engine.MODELS,status))):
            try:prepare()
            except Exception as exc:data["results"][side].update(state="failed",error=str(exc))
        turns = None;names = {"segments":[],"speakers":{}}
        try:
            status("両モデル共通の話者判別をしています…")
            turns = engine.diarize(wav,engine.MODELS/"speakers",request.get("num_speakers",0),status)
            names["segments"] = [{**turn,"text":""} for turn in turns]
            engine.apply_recorded_names(names,wav,turns,request)
            if names.get("speaker_memory_error"):data["speaker_memory_error"] = names["speaker_memory_error"]
        except Exception as exc:data["speaker_error"] = str(exc)
        data["shared_seconds"] = time.perf_counter()-started
        data["state"] = "running";publish()
        for side in SIDES:
            entry = data["results"][side]
            if entry["state"] == "failed":continue
            entry["state"] = "running";publish()
            label = f"Whisper {whisper}" if side == "whisper" else "Granite 5.0 TurboCTC"
            model_job = job/side;model_job.mkdir(exist_ok=True)
            model_folder = folder/side;model_folder.mkdir(exist_ok=True)
            began = time.perf_counter()
            try:
                result = transcribe(wav,entry["model"],language if side=="whisper" else "auto","",model_job,
                                    engine.MODELS,compute,lambda text:status(label+"："+text))
                elapsed = time.perf_counter()-began
                rows = assign_speakers(result["segments"],turns) if turns is not None else result["segments"]
                audio_target = model_folder/".audio"/"source.flac";audio_target.parent.mkdir(exist_ok=True)
                try:os.link(cached,audio_target)
                except OSError:shutil.copy2(cached,audio_target)
                saved = {"title":data["title"],"source":source,"source_mode":request["mode"],"model":entry["model"],
                         "language":result["language"],"prompt":"","device":result["device"],"backend":result["backend"],
                         "backend_fallbacks":result.get("fallbacks",[]),"segments":rows,
                         "speakers":normalize_names(rows,names["speakers"]),"audio_file":str(audio_target),
                         "recognition_seconds":elapsed,"comparison_raw":True,"automatic_text_corrections":False,
                         "remembered_speakers":deepcopy(names.get("remembered_speakers",{}))}
                if turns is not None:saved["diarization"] = {"engine":"sherpa-onnx","num_speakers":request.get("num_speakers",0),"turns":turns}
                for key in ("speaker_error","speaker_memory_error"):
                    if data.get(key):saved[key] = data[key]
                if result.get("timestamp_method"):saved["timestamp_method"] = result["timestamp_method"]
                engine.save_result(saved,model_folder)
                entry.update(state="complete",recognition_seconds=elapsed,device=result["device"],
                             backend=result["backend"],result_path=f"{side}/result.json")
            except Exception as exc:
                entry.update(state="failed",attempt_seconds=time.perf_counter()-began,error=str(exc))
            publish()
        successes = sum(entry["state"]=="complete" for entry in data["results"].values())
        data["state"] = "complete" if successes==2 else "partial" if successes else "failed"
        data["total_seconds"] = time.perf_counter()-started;publish()
        engine.emit("comparison_complete",comparison_path=str(path))
    except Exception as exc:
        data["state"] = "failed";data["error"] = str(exc);data["total_seconds"] = time.perf_counter()-started
        for entry in data["results"].values():
            if entry["state"] in {"waiting","running"}:entry["state"] = "interrupted"
        publish();raise
    finally:
        wav.unlink(missing_ok=True)
        if request["mode"]=="youtube" and media is not None and Path(media).parent==job:Path(media).unlink(missing_ok=True)
