"""Locate a difference in the shared recording and play bounded PCM buffers."""
from platform_support import tool_path
from dataclasses import dataclass,field
import math
from pathlib import Path
import queue
import subprocess
import threading
import time

from engine import TOOLS,NO_WINDOW,resolve_cached_audio
from preview_audio import WaveOutput

PLAYBACK_SPEEDS=(0.5,0.75,1.0,1.25,1.5)
SPEED_LABELS = {f'{speed:g}倍': speed for speed in PLAYBACK_SPEEDS}
REPEAT_PAUSE_MS = 350


def _word_range(row,first,last):
    words=row.get("words") or []
    if not words:return None
    text=str(row["text"]).strip()
    try:
        original="".join(str(word["word"]) for word in words)
        if original.strip()!=text:return None
        position=-(len(original)-len(original.lstrip()));found=[]
        low,high=float(row["start"]),float(row["end"])
        for word in words:
            end=position+len(str(word["word"]))
            start_time,end_time=float(word["start"]),float(word["end"])
            if not all(math.isfinite(v) for v in (start_time,end_time)) or end_time<start_time or start_time<low-.25 or end_time>high+.25:return None
            if position<last and end>first:found.append((max(low,start_time),min(high,end_time)))
            position=end
        if found and max(v[1] for v in found)>min(v[0] for v in found):
            return min(v[0] for v in found),max(v[1] for v in found)
    except (KeyError,TypeError,ValueError,OverflowError):pass
    return None


def difference_intervals(plan,index,duration=None,context=1.0):
    """Prefer matching word times; use whole utterances when times are absent.
    Disjoint candidates stay separate instead of playing the silence between.
    """
    if not 0<=index<len(plan.spans):return []
    precise=[];fallback=[]
    for side,(start,end) in zip(("whisper","granite"),(plan.spans[index][:2],plan.spans[index][2:])):
        selected={}
        for row,char in plan.positions[side][start:end]:
            if row in selected:selected[row][1]=char+1
            else:selected[row]=[char,char+1]
        for index,(first,last) in selected.items():
            row=plan.results[side]["segments"][index]
            exact=_word_range(row,first,last)
            if exact:precise.append(exact)
            else:fallback.append((float(row["start"]),float(row["end"])))
    intervals=precise+[span for span in fallback if not any(a<span[1] and b>span[0] for a,b in precise)]
    try:
        duration=float(duration)
        if not math.isfinite(duration) or duration<=0:duration=None
    except (TypeError,ValueError):duration=None
    merged=[]
    for a,b in sorted(intervals):
        if not math.isfinite(a) or not math.isfinite(b) or b<a:continue
        a=max(0.,a-context);b=max(a,b+context)
        if duration is not None:b=min(duration,b)
        if b<=a:continue
        if merged and a<=merged[-1][1]+.001:merged[-1]=(merged[-1][0],max(b,merged[-1][1]))
        else:merged.append((a,b))
    return merged


def comparison_audio_source(path,results,data):
    folder=Path(path).parent
    # Local relative caches take priority after a comparison folder is moved.
    for place in (folder,folder/"whisper",folder/"granite"):
        audio=resolve_cached_audio({},place)
        if audio:return audio
    for side in ("whisper","granite"):
        audio=resolve_cached_audio(results.get(side,{}),folder/side)
        if audio:return audio
    if data.get("source_mode")=="file":
        from result_media import MEDIA_EXTENSIONS, result_file
        source = result_file(folder, data.get("source"), MEDIA_EXTENSIONS)
        if source:return source
    raise FileNotFoundError("比較に使った音声が見つかりません。比較フォルダー全体を戻すか、元の動画でもう一度比較してください。")


@dataclass
class _Request:
    generation:int
    source:Path
    intervals:list
    speed:float=1.0
    cancel:threading.Event=field(default_factory=threading.Event)
    done:threading.Event=field(default_factory=threading.Event)
    process:object=None
    activity:float=field(default_factory=time.monotonic)
    failure:str=""


class ComparisonAudioPlayer:
    """One owner thread serializes audio handles, including rapid stop/replay.
    Workers never reference Tk. Events are generation-tagged to reject stale work.
    """
    def __init__(self):
        self.events=queue.Queue();self.requests=queue.Queue();self.closed=threading.Event()
        self.lock=threading.Lock();self.generation=0;self.current=None;self.active=None
        self.thread=threading.Thread(target=self._run,daemon=True,name="comparison-audio");self.thread.start()

    @staticmethod
    def _cancel(request):
        if request is None:return
        request.cancel.set()
        process=request.process
        if process is not None:
            try:
                if process.poll() is None:process.terminate()
            except OSError:pass

    def stop(self):
        with self.lock:
            self.generation+=1;self._cancel(self.current);self._cancel(self.active)
            self.current=None
            while True:
                try:request=self.requests.get_nowait();request.cancel.set();request.done.set()
                except queue.Empty:break

    def play(self,source,intervals,speed=1.0):
        if self.closed.is_set():raise RuntimeError("音声プレーヤーは終了しています。")
        if speed not in PLAYBACK_SPEEDS:raise ValueError("再生速度は0.5・0.75・1・1.25・1.5倍から選んでください。")
        source=Path(source)
        if not source.is_file():raise FileNotFoundError("再生する音声が見つかりません。")
        if not intervals or any(not math.isfinite(a) or not math.isfinite(b) or a<0 or b<=a for a,b in intervals):
            raise ValueError("再生する音声の時刻が正しくありません。")
        self.stop()
        with self.lock:
            request=_Request(self.generation,source,list(intervals),float(speed));self.current=request;self.requests.put(request)
        return request.generation

    def close(self):
        self.closed.set();self.stop()
        if threading.current_thread() is not self.thread:self.thread.join(timeout=1.5)

    def _watch(self,request):
        while not request.done.wait(.2):
            if request.cancel.is_set():return
            if time.monotonic()-request.activity>20:
                request.failure="音声の読み込みが時間内に終わりませんでした。もう一度再生してください。"
                self._cancel(request);return

    def _run(self):
        while not self.closed.is_set():
            try:request=self.requests.get(timeout=.1)
            except queue.Empty:continue
            with self.lock:
                if request.cancel.is_set() or request.generation!=self.generation:
                    request.done.set();continue
                self.active=request
            try:self._play(request)
            except Exception as exc:
                if not request.cancel.is_set():self.events.put((request.generation,"error",str(exc)))
            finally:
                request.done.set()
                with self.lock:
                    if self.active is request:self.active=None

    def _play(self,request):
        audio=None;started=False;last_event=0.;last_position=0.;last_motion=time.monotonic();error=None
        playback_spans=[]
        threading.Thread(target=self._watch,args=(request,),daemon=True).start()
        def progress(force=False):
            nonlocal last_event,last_position,last_motion
            now=time.monotonic();request.activity=now
            if not started:return
            elapsed=audio.position()
            if elapsed>last_position+.001:last_position=elapsed;last_motion=now
            elif audio.pending and now-last_motion>10:raise RuntimeError("音声出力が停止しました。出力機器を確認して、もう一度再生してください。")
            if force or now-last_event>=.1:
                remaining=elapsed;position=request.intervals[0][0]
                # Use emitted PCM lengths at interval boundaries: atempo may
                # round each clip slightly, which must not accumulate across gaps.
                for a,b,output_seconds in playback_spans:
                    position=min(b,a+min(remaining,output_seconds)*request.speed)
                    if remaining<=output_seconds:break
                    remaining-=output_seconds
                self.events.put((request.generation,"playing",position));last_event=now
        try:
            self.events.put((request.generation,"loading",None))
            audio=WaveOutput()
            for start,end in request.intervals:
                if request.cancel.is_set():break
                playback_spans.append([start,end,0.])
                command=[str(tool_path('ffmpeg')),"-nostdin","-hide_banner","-loglevel","error",
                         "-ss",str(start),"-t",str(end-start),"-i",str(request.source),"-map","0:a:0","-vn"]
                # Limit the source interval BEFORE tempo conversion so slow
                # playback includes the full ending. atempo preserves pitch.
                if request.speed!=1.0:command.extend(["-af",f"atempo={request.speed:g}"])
                command.extend(["-ac","2","-ar",str(WaveOutput.RATE),"-f","s16le","pipe:1"])
                process=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,creationflags=NO_WINDOW)
                request.process=process;frames=0
                try:
                    if request.cancel.is_set():self._cancel(request)
                    while not request.cancel.is_set():
                        while len(audio.pending)>=8 and not request.cancel.is_set():
                            audio.collect();progress();request.cancel.wait(.01)
                        if request.cancel.is_set():break
                        request.activity=time.monotonic();pcm=process.stdout.read(WaveOutput.RATE*4//25)
                        if not pcm:break
                        if request.cancel.is_set():break
                        if len(pcm)%4:raise ValueError("音声のデータを読み込めませんでした。")
                        frames+=len(pcm)//4;playback_spans[-1][2]=frames/WaveOutput.RATE;audio.write(pcm)
                        if not started and len(audio.pending)>=3:audio.resume();started=True;last_motion=time.monotonic()
                        progress()
                    if not request.cancel.is_set() and (process.wait(timeout=5)!=0 or not frames):
                        raise ValueError("この区間の音声を読み込めませんでした。保存音声または元の動画を確認してください。")
                finally:
                    try:
                        if process.poll() is None:
                            try:process.terminate()
                            except OSError:pass
                        try:process.wait(timeout=1)
                        except subprocess.TimeoutExpired:
                            try:process.kill()
                            except OSError:pass
                            process.wait(timeout=1)
                    finally:process.stdout.close();request.process=None
            if not request.cancel.is_set():
                if not started:audio.resume();started=True;last_motion=time.monotonic()
                while audio.pending and not request.cancel.is_set():
                    audio.collect();progress();request.cancel.wait(.01)
                progress(force=True)
        except Exception as exc:
            if not request.cancel.is_set():error=str(exc)
        finally:
            if audio:audio.close()
            if request.failure:error=request.failure
            if error:self.events.put((request.generation,"error",error))
            elif not request.cancel.is_set():self.events.put((request.generation,"ended",None))
