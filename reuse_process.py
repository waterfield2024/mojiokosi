"""Bounded, cancellable requests to this application's noise-removal worker."""
from platform_support import python_executable, IS_MAC
import base64
import json
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time

from reuse_assets import ensure_reuse

ROOT = Path(__file__).resolve().parent


class ReuseProcess:
    def __init__(self, folder, status=lambda text: None, stopped=lambda: False):
        self.stopped = stopped
        self.proc = self.log = self.owner = None
        self.responses = queue.Queue()
        self.lock = threading.Lock()
        try:
            weights = ensure_reuse(ROOT / 'models', status, stopped)
            if stopped():
                raise InterruptedError('ノイズ除去の準備を中止しました。')
            status('Nemotronノイズ除去を先読み中…（初回のGPU準備を含みます）')
            from live_process import ProcessJob
            folder = Path(folder)
            folder.mkdir(parents=True, exist_ok=True)
            self.log = (folder / 'noise-removal.log').open('ab')
            python = python_executable()
            self.proc = subprocess.Popen([str(python), '-X', 'utf8', '-u', str(ROOT / 'reuse_worker.py'), str(weights)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log,
                text=True, encoding='utf-8', errors='replace', creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            self.owner = ProcessJob(self.proc)
            self.reader = threading.Thread(target=self.read, args=(self.proc.stdout,), daemon=True)
            self.reader.start()
            if self.receive(120).get('kind') != 'ready':
                raise RuntimeError('ノイズ除去モデルの準備応答が正しくありません。')
        except BaseException:
            self.close()
            raise

    def read(self, stream):
        try:
            for line in stream:
                try:
                    response = json.loads(line)
                except ValueError:
                    continue
                if isinstance(response, dict):
                    self.responses.put(response)
        finally:
            self.responses.put({'kind': 'error', 'message': 'ノイズ除去エンジンが終了しました。'})

    def receive(self, seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if self.stopped():
                raise InterruptedError('ノイズ除去を中止しました。')
            try:
                response = self.responses.get(timeout=.05)
            except queue.Empty:
                continue
            if response.get('kind') == 'error':
                raise RuntimeError(response.get('message', 'ノイズ除去に失敗しました。'))
            return response
        raise TimeoutError('ノイズ除去が時間内に終わりませんでした。')

    def healthy(self):
        return self.proc is not None and self.proc.poll() is None

    def enhance(self, pcm, timeout=3):
        with self.lock:
            try:
                if not pcm or len(pcm) % 2 or len(pcm) > 12*32000:
                    raise ValueError('ノイズ除去の音声区間が正しくありません。')
                if not self.healthy():
                    raise RuntimeError('ノイズ除去モデルを読み込み直してください。')
                self.proc.stdin.write(json.dumps({'command': 'enhance', 'pcm': base64.b64encode(pcm).decode('ascii')})+'\n')
                self.proc.stdin.flush()
                response = self.receive(timeout)
                if response.get('kind') != 'result':
                    raise ValueError('ノイズ除去の応答が正しくありません。')
                result = base64.b64decode(response['pcm'], validate=True)
                if len(result) != len(pcm):
                    raise ValueError('ノイズ除去で音声の長さが変わったため、元の音声を使います。')
                return result
            except BaseException:
                self.close()
                raise

    def close(self):
        if self.proc:
            if self.proc.poll() is None:
                self.proc.terminate()
                try:
                    self.proc.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    self.proc.kill()
                    self.proc.wait(timeout=2)
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
