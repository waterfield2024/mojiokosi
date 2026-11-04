"""Optional ASR-only enhancement. Recording and speaker analysis remain original."""
from platform_support import IS_MAC
import json
from pathlib import Path
import threading
import time
from reuse_process import ReuseProcess


class LiveNoiseReducer:
    def __init__(self, folder, closing=lambda: False, worker_factory=ReuseProcess):
        self.folder, self.closing, self.factory = Path(folder), closing, worker_factory
        self.worker = None
        self.enabled, self.closed, self.loading = False, False, False
        self.problem, self.message = '', ''
        self.compute = 'auto'
        self.revision = 0
        self.control_token = None
        self.slow = 0
        self.last_seconds = 0.
        self.thread = None
        self.lock = threading.RLock()

    def configure(self, enabled, compute=None, retry=False):
        if type(enabled) is not bool:
            raise ValueError('ノイズ除去はオンかオフを選んでください。')
        with self.lock:
            if enabled != self.enabled or (compute is not None and compute != self.compute) or retry:
                self.revision += 1
                self.problem, self.slow = '', 0
            if IS_MAC and enabled:
                self.problem = 'NemotronはNVIDIA CUDA専用です。Macでは元の音声で認識します。'
            self.enabled = enabled
            if compute is not None:
                self.compute = compute
            if self.compute == 'cpu':
                if self.worker:
                    self.worker.close()
                    self.worker = None
                if enabled:
                    self.problem = 'NVIDIA GPUが必要です。メイン画面の処理方式をGPU優先にしてください。'

    def pump(self):
        with self.lock:
            if not self.enabled or self.closed or self.closing() or self.problem or self.loading:
                return
            if self.worker and self.worker.healthy():
                return
            if self.worker:
                self.worker.close()
                self.worker = None
            revision = self.revision
            self.loading = True
            self.message = 'ノイズ除去を準備中。準備中の発言には元の音声を使います。'

            def cancelled():
                return self.closed or self.closing() or revision != self.revision or not self.enabled

            def progress(text):
                if not cancelled():
                    self.message = text

            def load():
                candidate = None
                try:
                    candidate = self.factory(self.folder, progress, cancelled)
                    with self.lock:
                        if not cancelled():
                            candidate.stopped = lambda: self.closed or self.closing()
                            self.worker, candidate = candidate, None
                except InterruptedError:
                    pass
                except Exception as exc:
                    with self.lock:
                        if not cancelled():
                            self.problem = str(exc)[:250]
                finally:
                    if candidate:
                        candidate.close()
                    with self.lock:
                        self.loading = False
            self.thread = threading.Thread(target=load, daemon=True, name='noise-preload')
            self.thread.start()

    def prepare(self, enabled, compute, status, stopped):
        from realtime_asr import LiveStopped
        self.configure(enabled, compute, retry=True)
        previous = None
        while True:
            if stopped():
                self.configure(False)
                raise LiveStopped()
            self.pump()
            if not self.loading:
                break
            if self.message != previous:
                status(self.message)
                previous = self.message
            time.sleep(.1)
        return self.snapshot()

    def snapshot(self):
        with self.lock:
            if not self.enabled:
                state, text = 'off', 'ノイズ除去：OFF（元の音声で認識）'
            elif self.problem:
                state, text = 'suspended', 'ノイズ除去を休止し、元の音声で認識中：' + self.problem
            elif self.loading or self.worker is None:
                state, text = 'loading', self.message or 'ノイズ除去を準備しています。'
            else:
                state, text = 'ready', 'ノイズ除去：ON／Nemotron（認識用のみ・録音は元の音声）'
                if self.last_seconds:
                    text += f'／前区間 {self.last_seconds:.2f}秒'
            return {'enabled': self.enabled, 'state': state, 'text': text}

    def process(self, pcm):
        self.pump()
        with self.lock:
            if not self.enabled or self.problem or self.loading or self.worker is None:
                return pcm, False
            worker, revision = self.worker, self.revision
        started = time.monotonic()
        try:
            duration = len(pcm)/32000
            result = worker.enhance(pcm, timeout=min(3., max(1., duration*.8)))
            if len(result) != len(pcm):
                raise ValueError('音声の長さが一致しません。')
            self.last_seconds = time.monotonic()-started
            self.slow = self.slow+1 if self.last_seconds > max(.5, duration*.6) else 0
            if self.slow >= 2:
                self.problem = '処理が遅いため字幕を優先しました。再試行するにはオフ→オンにしてください。'
            if revision != self.revision or not self.enabled:
                return pcm, False
            return result, True
        except Exception as exc:
            worker.close()
            with self.lock:
                if self.worker is worker:
                    self.worker = None
                self.problem = str(exc)[:250] + ' 再試行するにはオフ→オンにしてください。'
            return pcm, False

    def close(self):
        self.closed = True
        if self.thread:
            self.thread.join(timeout=1)
        if self.worker:
            self.worker.close()
            self.worker = None


def update_control(reducer, job):
    """Each job has its own atomic control file; old recordings cannot change it."""
    try:
        value = json.loads((Path(job)/'noise-control.json').read_text('utf-8'))
        token = value.get('token')
        retry = isinstance(token, str) and token != reducer.control_token
        reducer.configure(value['enabled'], retry=retry)
        reducer.control_token = token
    except (OSError, ValueError, KeyError, TypeError):
        pass
