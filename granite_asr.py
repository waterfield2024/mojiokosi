"""Resident Granite client shared by file transcription and live captions."""
from platform_support import torch_backends
from platform_support import python_executable, IS_MAC
import base64
import json
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import wave
from asr import ROOT,NO_WINDOW,nvidia_available
from granite_model import ensure_granite
from realtime_asr import LiveStopped


class GraniteRecognizer:
    def __init__(self,model,language,prompt,compute,job,status,stopped=lambda: False,models=None):
        if compute not in {"auto","gpu","cpu"}:raise ValueError("処理方式が正しくありません。")
        self.model_name,self.language,self.prompt = model,language,prompt
        self.job,self.status,self.stopped = Path(job),status,stopped
        self.job.mkdir(parents=True,exist_ok=True)
        self.proc = self.log = self.owner = None
        self.device = ""
        self.failures = []
        self.backends = torch_backends(compute, nvidia_available)
        self.lock = threading.Lock()
        try:
            self.model = ensure_granite(models or ROOT/"models",status,stopped)
            self.start()
        except InterruptedError:
            self.close();raise LiveStopped() from None
        except BaseException:
            self.close();raise

    @property
    def backend(self):return "granite-speech-5.0-turboctc/"+self.device.lower()

    def start(self):
        from live_process import ProcessJob
        while self.backends:
            if self.stopped():raise LiveStopped()
            backend = self.backends.pop(0)
            self.status(f"Graniteモデルを読み込み中… {backend.upper()}")
            try:
                self.responses = queue.Queue()
                responses = self.responses
                self.log = (self.job/("granite-"+backend+".log")).open("ab")
                python = python_executable()
                self.proc = subprocess.Popen([str(python),"-X","utf8","-u",str(ROOT/"granite_worker.py"),str(self.model),backend],
                    stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=self.log,text=True,encoding="utf-8",
                    errors="replace",creationflags=NO_WINDOW)
                self.owner = ProcessJob(self.proc)
                def read(stream):
                    try:
                        for line in stream:
                            try:responses.put(json.loads(line))
                            except ValueError:continue
                    finally:responses.put({"kind":"error","message":"Granite認識が終了しました。"})
                threading.Thread(target=read,args=(self.proc.stdout,),daemon=True).start()
                reply = self.receive(180)
                if reply.get("kind") != "ready":raise RuntimeError("Graniteを準備できませんでした。")
                self.device = reply["device"]
                return
            except LiveStopped:
                self.close_process();raise
            except (OSError,RuntimeError) as exc:
                self.failures.append({"backend":backend,"reason":str(exc)})
                self.close_process()
                if self.backends:self.status("GraniteでGPUを利用できないため、CPUで準備します…")
        raise RuntimeError("Graniteを起動できませんでした。 "+self.failures[-1]["reason"])

    def receive(self,seconds=120):
        deadline = time.monotonic()+seconds
        while time.monotonic()<deadline:
            if self.stopped():raise LiveStopped()
            try:reply = self.responses.get(timeout=.1)
            except queue.Empty:continue
            if reply.get("kind") == "error":raise RuntimeError(reply.get("message","Granite認識に失敗しました。"))
            return reply
        raise RuntimeError("Graniteの応答が時間内に届きませんでした。")

    def healthy(self):return self.proc is not None and self.proc.poll() is None

    def transcribe(self,pcm):
        with self.lock:
            while True:
                if self.stopped():raise LiveStopped()
                try:
                    self.proc.stdin.write(json.dumps({"command":"transcribe","pcm":base64.b64encode(pcm).decode("ascii")})+"\n")
                    self.proc.stdin.flush()
                    reply = self.receive()
                    if reply.get("kind") != "result":raise RuntimeError("Graniteの結果が正しくありません。")
                    return reply["result"]
                except LiveStopped:
                    # A cancelled read must not leave a reply queued for the next session.
                    self.close_process();raise
                except (OSError,RuntimeError) as exc:
                    self.failures.append({"backend":self.device,"reason":str(exc)})
                    self.close_process()
                    if not self.backends:raise RuntimeError("Granite認識に失敗しました。"+str(exc)) from exc
                    self.status("GraniteのGPU認識に失敗したため、同じ音声をCPUで再試行します…")
                    self.start()

    def close_process(self):
        if self.proc:
            if self.proc.poll() is None:
                self.proc.terminate()
                try:self.proc.wait(timeout=3)
                except subprocess.TimeoutExpired:self.proc.kill();self.proc.wait(timeout=3)
            if self.owner:self.owner.close();self.owner=None
            for stream in (self.proc.stdin,self.proc.stdout):stream.close()
            self.proc = None
        if self.log:self.log.close();self.log=None

    def close(self):self.close_process()


def transcribe_file(wav,language,prompt,job,models,compute,progress):
    from granite_decode import segment_words
    recognizer = GraniteRecognizer("granite-turboctc",language,prompt,compute,job,progress,models=models)
    words, fallback_segments = [], []
    try:
        with wave.open(str(wav),"rb") as source:
            if (source.getframerate(),source.getnchannels(),source.getsampwidth()) != (16000,1,2):
                raise ValueError("Graniteには16kHz・モノラル音声が必要です。")
            total = source.getnframes();core_size = 24*16000;context = 2*16000
            for core in range(0,total,core_size):
                end = min(total,core+core_size);a = max(0,core-context);b = min(total,end+context)
                source.setpos(a);pcm = source.readframes(b-a)
                progress(f"Graniteで文字起こし中… {recognizer.device} / {core*100//max(1,total)}%")
                result = recognizer.transcribe(pcm)
                for row in result["segments"]:
                    if row.get("words"):
                        for word in row["words"]:
                            start,stop = word["start"]+a/16000,word["end"]+a/16000
                            if core/16000 <= (start+stop)/2 < end/16000:
                                words.append({"word":word["word"],"start":round(start,3),"end":round(stop,3)})
                    else:
                        start,stop = row["start"]+a/16000,row["end"]+a/16000
                        if core/16000 <= (start+stop)/2 < end/16000:
                            fallback_segments.append({**row,"start":max(core/16000,start),"end":min(end/16000,stop)})
        segments = sorted(segment_words(words)+fallback_segments,key=lambda row:row["start"])
        return {"segments":segments,"language":"auto","device":recognizer.device,"backend":recognizer.backend,
                "fallbacks":recognizer.failures,"timestamp_method":"ctc-emissions"}
    finally:recognizer.close()
