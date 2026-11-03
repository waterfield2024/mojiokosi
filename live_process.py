"""GUI-owned resident worker. Recording jobs expose the usual Popen read/poll API."""
import ctypes
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading
import uuid
from asr import ROOT, NO_WINDOW


class ProcessJob:
    """Kill the resident worker and its children if the GUI exits unexpectedly."""
    def __init__(self, process):
        self.handle = None
        self.lock = threading.Lock()
        if os.name != 'nt':
            return
        from ctypes import wintypes as w
        class Basic(ctypes.Structure):
            _fields_ = [('process_time', ctypes.c_int64), ('job_time', ctypes.c_int64),
                        ('flags', w.DWORD), ('minimum', ctypes.c_size_t), ('maximum', ctypes.c_size_t),
                        ('active', w.DWORD), ('affinity', ctypes.c_size_t), ('priority', w.DWORD), ('scheduling', w.DWORD)]
        class Extended(ctypes.Structure):
            _fields_ = [('basic', Basic), ('io', ctypes.c_uint64 * 6),
                        ('process_memory', ctypes.c_size_t), ('job_memory', ctypes.c_size_t),
                        ('peak_process', ctypes.c_size_t), ('peak_job', ctypes.c_size_t)]
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, w.LPCWSTR]
        kernel.CreateJobObjectW.restype = w.HANDLE
        kernel.SetInformationJobObject.argtypes = [w.HANDLE, ctypes.c_int, ctypes.c_void_p, w.DWORD]
        kernel.AssignProcessToJobObject.argtypes = [w.HANDLE, w.HANDLE]
        kernel.CloseHandle.argtypes = [w.HANDLE]
        handle = kernel.CreateJobObjectW(None, None)
        if handle:
            limits = Extended(); limits.basic.flags = 0x2000  # KILL_ON_JOB_CLOSE
            if (kernel.SetInformationJobObject(handle, 9, ctypes.byref(limits), ctypes.sizeof(limits))
                    and kernel.AssignProcessToJobObject(handle, int(process._handle))):
                self.handle, self.kernel = handle, kernel
            else:
                kernel.CloseHandle(handle)

    def close(self):
        with self.lock:
            if self.handle:
                self.kernel.CloseHandle(self.handle)
                self.handle = None


class JobOutput:
    def __init__(self):
        self.lines = queue.Queue()
    def __iter__(self):
        while True:
            line = self.lines.get()
            if line is None:
                return
            yield line
    def close(self):
        pass


class LiveJob:
    def __init__(self, owner):
        self.owner = owner
        self.pid = owner.proc.pid
        self.id = uuid.uuid4().hex
        self.stdout = JobOutput()
        self.done = threading.Event()
        self.returncode = None
        self.cancelled = False
    def finish(self, code):
        if not self.done.is_set():
            self.returncode = code
            self.stdout.lines.put(None)
            self.done.set()
    def poll(self):
        return self.returncode
    def wait(self, timeout=None):
        if not self.done.wait(timeout):
            raise subprocess.TimeoutExpired('realtime', timeout)
        return self.returncode
    def terminate(self):
        self.owner.close(wait=False)
    kill = terminate


class PersistentLiveProcess:
    def __init__(self, events):
        self.events = events
        self.id = uuid.uuid4().hex
        self.folder = ROOT / '.cache' / 'live-models' / self.id
        self.folder.mkdir(parents=True, exist_ok=True)
        self.active = None
        self.token = None
        self.info = {}
        self.closed = False
        self.lock = threading.Lock()
        self.log = (self.folder / 'resident.log').open('wb')
        self.proc = None
        python = Path(sys.executable).with_name('python.exe') if os.name == 'nt' else Path(sys.executable)
        try:
            self.proc = subprocess.Popen([str(python), '-X', 'utf8', '-u', str(ROOT / 'live_models.py'), str(self.folder)],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log, text=True, encoding='utf-8',
                errors='replace', creationflags=NO_WINDOW)
            self.process_job = ProcessJob(self.proc)
        except BaseException:
            if self.proc:
                self.proc.kill()
                self.proc.wait()
                self.proc.stdin.close()
                self.proc.stdout.close()
            self.log.close()
            raise
        threading.Thread(target=self._read, daemon=True).start()

    def alive(self):
        return not self.closed and self.proc.poll() is None

    def _send(self, command):
        with self.lock:
            if not self.alive():
                raise RuntimeError('認識モデルの準備が終了しています。もう一度準備してください。')
            self.proc.stdin.write(json.dumps(command, ensure_ascii=False) + '\n')
            self.proc.stdin.flush()

    def prepare(self, settings):
        token = uuid.uuid4().hex
        self.token = token
        self._send({'command': 'prepare', 'token': token, 'settings': settings})
        return token

    def release_context(self):
        if self.alive():
            self._send({'command': 'release_context'})

    def run(self, request_path):
        if self.active and self.active.poll() is None:
            raise RuntimeError('前の録音を保存しています。完了後に開始してください。')
        job = LiveJob(self)
        self.active = job
        try:
            self._send({'command': 'run', 'job_id': job.id, 'request_path': str(request_path)})
        except BaseException:
            self.active = None
            job.finish(1)
            raise
        return job

    def _read(self):
        try:
            for line in self.proc.stdout:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get('job_id'):
                    job = self.active
                    if job is None or job.id != event['job_id']:
                        continue
                    if event['kind'] == 'resident_job_exit':
                        self.info = event.get('model_processes', {})
                        self.active = None
                        job.finish(event['code'])
                    else:
                        job.stdout.lines.put(line)
                else:
                    if event['kind'] == 'live_preload_ready' and event.get('token') == self.token:
                        self.info = event
                    self.events.put({**event, 'service_id': self.id})
        finally:
            code = self.proc.wait()
            self.process_job.close()
            job = self.active
            if job:
                if not job.cancelled and not self.closed:
                    job.stdout.lines.put(json.dumps({'kind': 'error', 'message': 'リアルタイム認識が終了しました。途中までの録音は保存されています。'}) + '\n')
                job.finish(code or 1)
                self.active = None
            if not self.closed:
                self.events.put({'kind': 'live_preload_error', 'token': self.token, 'service_id': self.id,
                                 'message': '認識エンジンが終了しました。準備を再試行してください。'})

    def close(self, wait=True):
        if self.closed:
            return
        try:
            if self.proc.poll() is None:
                self._send({'command': 'shutdown'})
        except (OSError, RuntimeError):
            pass
        self.closed = True
        def finish():
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                if os.name == 'nt':
                    subprocess.run(['taskkill', '/PID', str(self.proc.pid), '/T', '/F'],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=NO_WINDOW)
                else:
                    self.proc.kill()
                self.proc.wait(timeout=10)
            finally:
                self.process_job.close()
                for stream in (self.proc.stdin, self.proc.stdout, self.log):
                    try:
                        stream.close()
                    except OSError:
                        pass
        if wait:
            finish()
        else:
            threading.Thread(target=finish, daemon=True).start()
