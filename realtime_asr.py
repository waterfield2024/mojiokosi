"""A private, resident whisper.cpp process for short streaming audio windows."""
from __future__ import annotations
import io
import os
from pathlib import Path
import re
import socket
import subprocess
import time
import uuid
import wave
import requests
from asr import ROOT, NO_WINDOW, backend_order, ensure_model, native_path

class LiveStopped(Exception):
    pass

class CppLiveRecognizer:
    def __init__(self, model, language, prompt, compute, job, status, stopped=lambda: False):
        self.model_name, self.language, self.prompt = model, language, prompt
        self.job, self.status, self.stopped = Path(job), status, stopped
        self.backends = [b for b in backend_order(compute)
                         if (ROOT / ".tools" / ("whisper-" + b) / "whisper-server.exe").is_file()]
        self.failures = []
        self.proc = self.log = None
        self.session = requests.Session()
        self.session.trust_env = False
        self.device = ""
        try:
            self.model = ensure_model(model, ROOT / "models", status, stopped=stopped)
            self.start()
        except InterruptedError:
            self.close()
            raise LiveStopped() from None
        except BaseException:
            self.close()
            raise

    def start(self):
        while self.backends:
            if self.stopped():
                raise LiveStopped()
            backend = self.backends.pop(0)
            try:
                self.status(f"リアルタイム認識を準備中… {self.model_name} / {backend.upper()}")
                with socket.socket() as sock:
                    sock.bind(("127.0.0.1", 0))
                    port = sock.getsockname()[1]
                # An unguessable route restricts access from unrelated local web pages.
                prefix = "/" + uuid.uuid4().hex
                self.url = f"http://127.0.0.1:{port}{prefix}"
                public = self.job / "empty-public"
                public.mkdir(exist_ok=True)
                exe = ROOT / ".tools" / ("whisper-" + backend) / "whisper-server.exe"
                args = [str(exe), "-m", native_path(self.model, self.job),
                        "--host", "127.0.0.1", "--port", str(port), "--request-path", prefix,
                        "--public", "empty-public", "-l", self.language,
                        "-t", str(min(8, os.cpu_count() or 4)), "-nlp", "-bo", "1", "-bs", "1", "-nf", "-sns"]
                args += ["-ng"] if backend == "cpu" else ["-fa"]
                self.log_path = self.job / ("live-" + backend + ".log")
                self.log = self.log_path.open("wb")
                self.proc = subprocess.Popen(args, cwd=self.job, stdout=self.log, stderr=subprocess.STDOUT,
                                             creationflags=NO_WINDOW)
                deadline = time.monotonic() + 120
                while time.monotonic() < deadline:
                    if self.stopped():
                        raise LiveStopped()
                    if self.proc.poll() is not None:
                        raise RuntimeError(f"認識エンジンが終了しました: {self.proc.returncode}")
                    try:
                        r = self.session.get(self.url + "/health", timeout=.5)
                        if r.status_code == 200 and r.json().get("status") == "ok":
                            log = self.log_path.read_text("utf-8", errors="replace")
                            self.device = backend.upper() if re.search(r"using (?:CUDA|Vulkan)\d* backend", log, re.I) else "CPU"
                            if backend == "cpu":
                                self.device = "CPU"
                            return
                    except (requests.RequestException, ValueError):
                        pass
                    time.sleep(.15)
                raise RuntimeError("認識エンジンの準備が時間内に完了しませんでした。")
            except LiveStopped:
                self.close_process()
                raise
            except (OSError, RuntimeError) as exc:
                self.failures.append({"backend": backend, "reason": str(exc)})
                self.close_process()
                self.status(f"{backend.upper()}を利用できないため、別の処理方式で準備します…")
        raise RuntimeError("リアルタイム認識を起動できませんでした。 " +
                           "; ".join(f["reason"] for f in self.failures))

    def healthy(self):
        if not self.proc or self.proc.poll() is not None:
            return False
        try:
            response = self.session.get(self.url + "/health", timeout=(.5, .5))
            return response.status_code == 200 and response.json().get("status") == "ok"
        except (requests.RequestException, ValueError):
            return False

    def transcribe(self, pcm):
        data = io.BytesIO()
        with wave.open(data, "wb") as out:
            out.setnchannels(1); out.setsampwidth(2); out.setframerate(16000)
            out.writeframes(pcm)
        while True:
            try:
                fields = {"file": ("audio.wav", data.getvalue(), "audio/wav"),
                          "response_format": (None, "verbose_json"),
                          "language": (None, self.language),
                          "prompt": (None, self.prompt),
                          "temperature": (None, "0"), "temperature_inc": (None, "0")}
                response = self.session.post(self.url + "/inference", files=fields, timeout=(3, 90))
                response.raise_for_status()
                result = response.json()
                if not isinstance(result.get("segments"), list):
                    raise ValueError("文字起こし結果の形式が正しくありません。")
                return result
            except (requests.RequestException, ValueError) as exc:
                self.failures.append({"backend": self.device, "reason": str(exc)})
                self.close_process()
                if not self.backends:
                    raise RuntimeError("リアルタイム認識に失敗しました。録音と途中の結果は保存されています。") from exc
                self.start()

    def close_process(self):
        if self.proc:
            if self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.proc.kill(); self.proc.wait(timeout=5)
            self.proc = None
        if self.log:
            self.log.close(); self.log = None

    def close(self):
        self.close_process()
        self.session.close()


class LiveRecognizer:
    """Keep Whisper available via Transformers when its native server cannot run."""
    _forwarded={"language","prompt","status","stopped"}

    def __init__(self,model,language,prompt,compute,job,status,stopped=lambda:False):
        from native_errors import handle_native_errors
        handle_native_errors()
        self.model_name=model;self.compute=compute;self.job=Path(job)
        self.engine=None;self.native_failures=[];self.compatible=False
        self.language=language;self.prompt=prompt;self.status=status;self.stopped=stopped
        self.job.mkdir(parents=True,exist_ok=True)
        from asr import native_backends
        if not native_backends(compute, 'whisper-server.exe'):
            from whisper_hf_asr import WhisperHFRecognizer
            self.compatible=True
            self.engine=WhisperHFRecognizer(model,language,prompt,compute,job,status,stopped)
        else:
            try:self.engine=CppLiveRecognizer(model,language,prompt,compute,job,status,stopped)
            except (OSError,RuntimeError) as exc:self.use_compatible(exc)

    def __setattr__(self,name,value):
        object.__setattr__(self,name,value)
        if name in self._forwarded and self.__dict__.get('engine') is not None:setattr(self.engine,name,value)

    def use_compatible(self,error):
        from whisper_hf_asr import WhisperHFRecognizer
        if self.stopped():raise LiveStopped()
        if self.engine:
            self.native_failures.extend(self.engine.failures);self.engine.close();self.engine=None
        self.native_failures.append({'backend':'whisper.cpp/server','reason':str(error)[:1800]})
        self.status('従来の認識エンジンを起動できないため、互換Whisperで準備します…')
        self.compatible=True
        try:self.engine=WhisperHFRecognizer(self.model_name,self.language,self.prompt,self.compute,self.job,self.status,self.stopped)
        except (OSError,RuntimeError) as exc:
            raise RuntimeError('Whisperを準備できませんでした。 '+str(exc)+' / 従来エンジン: '+str(error)[:500]) from exc

    @property
    def proc(self):return self.engine.proc if self.engine else None
    @property
    def device(self):return self.engine.device if self.engine else ''
    @property
    def backend(self):return self.engine.backend if self.compatible else 'whisper.cpp/server/'+self.device.lower()
    @property
    def failures(self):return self.native_failures+(self.engine.failures if self.engine else [])
    def healthy(self):return self.engine is not None and self.engine.healthy()
    def transcribe(self,pcm):
        try:return self.engine.transcribe(pcm)
        except (OSError,RuntimeError) as exc:
            if self.compatible:raise
            self.use_compatible(exc);return self.engine.transcribe(pcm)
    def close(self):
        if self.engine:self.engine.close()
