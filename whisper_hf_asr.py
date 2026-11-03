"""Private resident Whisper worker using the app's existing PyTorch runtime."""
import base64
import json
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
from asr import ROOT,NO_WINDOW,nvidia_available
from realtime_asr import LiveStopped
from whisper_hf_model import ensure_whisper_hf


class WhisperHFRecognizer:
    def __init__(self,model,language,prompt,compute,job,status,stopped=lambda:False):
        if compute not in {'auto','gpu','cpu'}:raise ValueError('処理方式が正しくありません。')
        self.model_name,self.language,self.prompt=model,language,prompt
        self.job,self.status,self.stopped=Path(job),status,stopped
        self.proc=self.log=self.owner=None;self.device='';self.failures=[]
        self.backends=(['cuda'] if compute!='cpu' and nvidia_available() else [])+['cpu']
        self.lock=threading.Lock()
        try:
            self.model=ensure_whisper_hf(model,ROOT/'models',status,stopped);self.start()
        except InterruptedError:self.close();raise LiveStopped() from None
        except BaseException:self.close();raise

    @property
    def backend(self):return 'whisper/transformers/'+self.device.lower()

    def start(self):
        from live_process import ProcessJob
        while self.backends:
            if self.stopped():raise LiveStopped()
            backend=self.backends.pop(0)
            self.status(f'互換Whisperを先読み中… {self.model_name} / {backend.upper()}')
            try:
                self.responses=queue.Queue();responses=self.responses
                self.log=(self.job/('whisper-compatible-'+backend+'.log')).open('ab')
                python=Path(sys.executable).with_name('python.exe')
                self.proc=subprocess.Popen([str(python),'-X','utf8','-u',str(ROOT/'whisper_hf_worker.py'),str(self.model),backend],
                    stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=self.log,text=True,encoding='utf-8',
                    errors='replace',creationflags=NO_WINDOW)
                self.owner=ProcessJob(self.proc)
                def read(stream):
                    try:
                        for line in stream:
                            try:responses.put(json.loads(line))
                            except ValueError:continue
                    finally:responses.put({'kind':'error','message':'互換Whisperの認識処理が終了しました。'})
                threading.Thread(target=read,args=(self.proc.stdout,),daemon=True).start()
                reply=self.receive(240)
                if reply.get('kind')!='ready':raise RuntimeError('互換Whisperを準備できませんでした。')
                self.device=reply['device'];return
            except LiveStopped:self.close_process();raise
            except (OSError,RuntimeError) as exc:
                self.failures.append({'backend':'whisper/transformers/'+backend,'reason':str(exc)})
                self.close_process()
                if self.backends:self.status('互換WhisperのGPU処理を利用できないため、CPUで準備します…')
        raise RuntimeError('互換Whisperを起動できませんでした。 '+self.failures[-1]['reason'])

    def receive(self,seconds=120,interruptible=True):
        deadline=time.monotonic()+seconds
        while time.monotonic()<deadline:
            if interruptible and self.stopped():raise LiveStopped()
            try:reply=self.responses.get(timeout=.1)
            except queue.Empty:continue
            if reply.get('kind')=='error':raise RuntimeError(reply.get('message','互換Whisper認識に失敗しました。'))
            return reply
        raise RuntimeError('互換Whisperの応答が時間内に届きませんでした。')

    def healthy(self):return self.proc is not None and self.proc.poll() is None

    def transcribe(self,pcm):
        with self.lock:
            while True:
                try:
                    request={'command':'transcribe','pcm':base64.b64encode(pcm).decode('ascii'),'language':self.language,'prompt':self.prompt}
                    self.proc.stdin.write(json.dumps(request,ensure_ascii=False)+'\n');self.proc.stdin.flush()
                    # Normal Stop halts capture first, then drains recorded audio.
                    # Consume each outstanding reply so the resident model can be
                    # reused. Force-stop/app exit still terminates the owned process.
                    reply=self.receive(interruptible=False)
                    if reply.get('kind')!='result':raise RuntimeError('互換Whisperの結果が正しくありません。')
                    return reply['result']
                except LiveStopped:self.close_process();raise
                except (OSError,RuntimeError) as exc:
                    self.failures.append({'backend':self.backend,'reason':str(exc)});self.close_process()
                    if not self.backends:raise RuntimeError('互換Whisper認識に失敗しました。 '+str(exc)) from exc
                    self.status('互換WhisperのGPU認識に失敗したため、同じ音声をCPUで再試行します…');self.start()

    def close_process(self):
        if self.proc:
            if self.proc.poll() is None:
                self.proc.terminate()
                try:self.proc.wait(timeout=3)
                except subprocess.TimeoutExpired:self.proc.kill();self.proc.wait(timeout=3)
            if self.owner:self.owner.close();self.owner=None
            for stream in (self.proc.stdin,self.proc.stdout):stream.close()
            self.proc=None
        if self.log:self.log.close();self.log=None

    def close(self):self.close_process()
