"""Bounded background separation; no model load or waiting in the capture path."""
from collections import deque
from copy import deepcopy
import json
import os
from pathlib import Path
import queue
import subprocess
import threading
import time


class OverlapService:
    def __init__(self, settings, job, *, capacity=4):
        from asr import ROOT, NO_WINDOW
        from live_process import ProcessJob
        from platform_support import python_executable
        self.proc = self.owner = self.log = None
        self.events = queue.Queue()
        self.pending = deque()
        self.active = None
        self.ready = self.failed = False
        self.capacity = capacity
        self.message = '重なった声は字幕を先に表示し、後から分離して補正します（2人まで）。'
        self.started = time.monotonic()
        self.closed = False
        try:
            self.log = (Path(job)/'overlap-worker.log').open('ab')
            self.proc = subprocess.Popen([str(python_executable()), '-X', 'utf8', '-u', str(ROOT/'overlap_worker.py')],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log, text=True, encoding='utf-8',
                creationflags=NO_WINDOW | getattr(subprocess, 'BELOW_NORMAL_PRIORITY_CLASS', 0),
                start_new_session=os.name != 'nt')
            self.owner = ProcessJob(self.proc)
            self.send({'settings': settings, 'job': str(job)})
            def read():
                try:
                    for line in self.proc.stdout:
                        try: self.events.put(json.loads(line))
                        except ValueError: pass
                except (OSError, ValueError): pass
                finally: self.events.put({'kind': 'error'})
            threading.Thread(target=read, daemon=True).start()
        except BaseException:
            self.close(); raise

    def send(self, value):
        self.proc.stdin.write(json.dumps(value, ensure_ascii=False)+'\n')
        self.proc.stdin.flush()

    def submit(self, task):
        if self.failed or self.closed or len(self.pending) + bool(self.active) >= self.capacity: return False
        self.pending.append(deepcopy(task))
        return True

    @property
    def busy(self): return bool(self.active or self.pending)

    def poll(self):
        replies = []
        while True:
            try: event = self.events.get_nowait()
            except queue.Empty: break
            if event['kind'] == 'ready': self.ready = True
            elif event['kind'] == 'status': self.message = event['text']
            elif event['kind'] == 'error' and not self.closed: self.failed = True
            elif event['kind'] == 'result' and self.active and event['id'] == self.active['id']:
                replies.append(event); self.active = None
                self.message = event.get('message') or '重なった声を分離し、字幕を補正しました。'
        if self.active and time.monotonic() - self.started > 240:
            self.failed = True; self.message = '分離に時間がかかったため、元の字幕を残しました。'
        if self.failed:
            for task in ([self.active] if self.active else []) + list(self.pending):
                replies.append({'id': task['id'], 'message': '分離処理を完了できないため元の字幕を残しました。'})
            self.active = None; self.pending.clear(); self.close()
        elif self.ready and not self.active and self.pending:
            self.active = self.pending.popleft(); self.started = time.monotonic()
            try:
                self.send(self.active)
                self.message = '重なった声を分離・再認識中… 通常の字幕表示は続いています。'
            except (OSError, ValueError): self.failed = True
        return replies

    def close(self):
        if self.closed: return
        self.closed = True
        if self.proc:
            if self.proc.poll() is None:
                self.proc.terminate()
                try: self.proc.wait(timeout=3)
                except subprocess.TimeoutExpired: self.proc.kill(); self.proc.wait(timeout=3)
            if self.owner: self.owner.close()
            for stream in (self.proc.stdin, self.proc.stdout):
                if stream: stream.close()
        if self.log: self.log.close()


def run_file(data, wav, turns, folder, job, request, status=lambda text: None):
    """Same conservative replacements for saved video/YouTube, including retries."""
    from overlap import regions, plan, apply_reply
    from recording_speakers import recording_samples
    from asr import ROOT
    previous = data.get('overlap_review', [])
    seen = {row['segment_id'] for record in previous if record['status'] in {'applied', 'reverted'} for row in record['before']}
    retry_regions = [{'start': r['start'], 'end': r['end'], 'voices': r.get('voices', 2)}
                     for r in previous if r['status'] in {'pending', 'skipped'}]
    data['overlap_review'] = [r for r in previous if r['status'] not in {'pending', 'skipped'}]
    tasks = plan(data, retry_regions + regions(turns), seen)
    if not tasks: return
    try:
        profiles = recording_samples(wav, turns, ROOT/'models/speakers/embedding.onnx', status)
    except Exception: profiles = {}
    service = OverlapService({'model': data['model'], 'language': data.get('language', 'ja'),
        'prompt': data.get('prompt', ''), 'compute': request.get('compute', 'auto')}, job)
    waiting = deque(tasks)
    last_message = ''
    try:
        while waiting or service.busy:
            for reply in service.poll(): apply_reply(data, reply)
            if service.failed:
                for task in waiting: apply_reply(data, {'id': task['id'], 'message': '分離処理を完了できませんでした。'})
                break
            if waiting and len(service.pending) + bool(service.active) < service.capacity:
                task = waiting.popleft()
                service.submit({**task, 'wav': str(wav), 'folder': str(folder),
                                'profiles': profiles, 'names': data.get('speakers', {})})
            if last_message != service.message:
                last_message = service.message
                status(service.message)
            time.sleep(.1)
    finally: service.close()
