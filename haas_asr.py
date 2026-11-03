"""Resident Windows Haas client, shared by file and chunked live recognition."""
import base64
import json
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time
import wave

from asr import ROOT, NO_WINDOW
from haas_model import ensure_haas
from realtime_asr import LiveStopped


class HaasRecognizer:
    def __init__(self, model, language, prompt, compute, job, status, stopped=lambda: False, models=None):
        if compute not in {'auto', 'gpu', 'cpu'}:
            raise ValueError('処理方式が正しくありません。')
        self.model_name, self.language, self.prompt = model, 'ja', prompt
        self.status, self.stopped = status, stopped
        self.job = Path(job)
        self.job.mkdir(parents=True, exist_ok=True)
        self.proc = self.log = self.owner = None
        self.device, self.failures = 'CPU', []
        self.lock = threading.Lock()
        self.responses = queue.Queue()
        try:
            self.model = ensure_haas(models or ROOT / 'models', status, stopped)
            if stopped():
                raise LiveStopped()
            status('Haas-ja-v1.0を読み込み中… CPU（Windows用ONNX変換版）')
            from live_process import ProcessJob
            self.log = (self.job / 'haas-cpu.log').open('ab')
            python = Path(sys.executable).with_name('python.exe')
            self.proc = subprocess.Popen([str(python), '-X', 'utf8', '-u', str(ROOT / 'haas_worker.py'), str(self.model)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log,
                text=True, encoding='utf-8', errors='replace', creationflags=NO_WINDOW)
            self.owner = ProcessJob(self.proc)
            threading.Thread(target=self.read, args=(self.proc.stdout,), daemon=True).start()
            if self.receive(180).get('kind') != 'ready':
                raise RuntimeError('Haasの準備応答が正しくありません。')
        except InterruptedError:
            self.close()
            raise LiveStopped() from None
        except BaseException:
            self.close()
            raise

    @property
    def backend(self):
        return 'haas-ja-v1.0/sherpa-onnx-quantized/cpu'

    def read(self, stream):
        try:
            for line in stream:
                try:
                    reply = json.loads(line)
                except ValueError:
                    continue
                if isinstance(reply, dict):
                    self.responses.put(reply)
        finally:
            self.responses.put({'kind': 'error', 'message': 'Haas認識が終了しました。ログ: ' + str(self.job / 'haas-cpu.log')})

    def receive(self, seconds=120):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self.stopped():
                raise LiveStopped()
            try:
                reply = self.responses.get(timeout=.1)
            except queue.Empty:
                continue
            if reply.get('kind') == 'error':
                raise RuntimeError(reply.get('message', 'Haas認識に失敗しました。'))
            return reply
        raise RuntimeError('Haasの応答が時間内に届きませんでした。')

    def healthy(self):
        return self.proc is not None and self.proc.poll() is None

    def transcribe(self, pcm):
        if len(pcm) % 2 or len(pcm) > 30 * 32000:
            raise ValueError('Haasの音声区間は16kHz・モノラル・30秒以内にしてください。')
        with self.lock:
            try:
                if self.stopped():
                    raise LiveStopped()
                if not self.healthy():
                    raise RuntimeError('Haasモデルを読み込み直してください。')
                self.proc.stdin.write(json.dumps({'command': 'transcribe', 'pcm': base64.b64encode(pcm).decode('ascii')}) + '\n')
                self.proc.stdin.flush()
                reply = self.receive()
                if reply.get('kind') != 'result':
                    raise RuntimeError('Haasの認識結果が正しくありません。')
                return reply['result']
            except BaseException:
                # A cancelled request must not be reused as the next session's reply.
                self.close()
                raise

    def close(self):
        if self.proc:
            if self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait(timeout=3)
            if self.owner:
                self.owner.close()
                self.owner = None
            for stream in (self.proc.stdin, self.proc.stdout):
                if stream:
                    stream.close()
            self.proc = None
        if self.log:
            self.log.close()
            self.log = None


def transcribe_file(wav, language, prompt, job, models, compute, progress):
    from recognition_models import HAAS_MODEL
    from whisper_hf_file import audio_chunks, RATE
    with wave.open(str(wav), 'rb') as source:
        if (source.getframerate(), source.getnchannels(), source.getsampwidth(), source.getcomptype()) != (RATE, 1, 2, 'NONE'):
            raise ValueError('Haasには16kHz・モノラルのPCM音声が必要です。')
        recognizer = HaasRecognizer(HAAS_MODEL, language, prompt, compute, job, progress, models=models)
        rows = []
        duration = source.getnframes() / RATE
        try:
            # 20-second chunks with quiet boundaries, below the model's 30-second limit.
            for offset, pcm in audio_chunks(source):
                progress(f'Haasで文字起こし中… CPU / {int(100 * offset / max(duration, .01))}%')
                result = recognizer.transcribe(pcm)
                for row in result['segments']:
                    rows.append({**row, 'start': round(offset + row['start'], 3), 'end': round(offset + row['end'], 3),
                        'words': [{**word, 'start': round(offset + word['start'], 3), 'end': round(offset + word['end'], 3)}
                                  for word in row.get('words', [])]})
            return {'segments': rows, 'language': 'ja', 'device': 'CPU', 'backend': recognizer.backend,
                    'fallbacks': [], 'timestamp_method': 'tdt-tokens-or-chunk'}
        finally:
            recognizer.close()
