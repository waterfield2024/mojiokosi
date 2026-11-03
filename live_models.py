"""Resident live models and a private, stdin-controlled recording worker."""
from pathlib import Path
import json
import queue
import sys
import threading
import traceback
from asr import ROOT
from realtime_asr import LiveStopped
from live_speakers import LiveSpeakerProcess
from user_data import data_path


class LiveModels:
    def __init__(self, folder, closing=lambda: False):
        self.folder = Path(folder)
        self.folder.mkdir(parents=True, exist_ok=True)
        self.closing = closing
        self.recognizer = self.diarizer = None
        self.proofreader = None
        from live_noise import LiveNoiseReducer
        self.noise = LiveNoiseReducer(self.folder, self.closing)
        from speech_gate import SpeechGate
        self.speech = SpeechGate(ROOT / 'models')
        self.key = None

    @staticmethod
    def alive(worker):
        return worker is not None and worker.proc is not None and worker.proc.poll() is None

    def recognizer_for(self, request, language, prompt, status, stopped):
        key = (request.get('model', 'base'), request.get('compute', 'auto'))
        if key != self.key or not self.alive(self.recognizer) or not self.recognizer.healthy():
            if self.recognizer:
                self.recognizer.close()
                self.recognizer = None
            from recognition_models import live_recognizer_type
            recognizer_type = live_recognizer_type(key[0])
            candidate = recognizer_type(key[0], language, prompt, key[1], self.folder, status, stopped)
            try:
                status('認識モデルの初回処理を準備しています…')
                # Exercise lazy GPU initialization before opening the audio input.
                candidate.transcribe(b'\0\0' * 16000)
                if stopped():
                    raise LiveStopped()
            except BaseException:
                candidate.close()
                raise
            self.recognizer, self.key = candidate, key
        self.recognizer.language, self.recognizer.prompt = language, prompt
        self.recognizer.status, self.recognizer.stopped = status, stopped
        return self.recognizer

    def speakers_for(self, request, status, stopped, reset=False):
        if not self.alive(self.diarizer):
            if self.diarizer:
                self.diarizer.close()
                self.diarizer = None
            status('話者判別モデルを先読みしています…')
            self.diarizer = LiveSpeakerProcess(ROOT / 'models' / 'speakers', 0, self.folder, stopped)
        self.diarizer.stopped = stopped
        if reset:
            try:
                self.diarizer.reset(request.get('num_speakers', 0),
                                    request.get('speaker_memory_file') or data_path('speaker_memory.json', 'WHISPER_SPEAKER_MEMORY'))
            except LiveStopped:
                self.diarizer.close()
                self.diarizer = None
                raise
            except Exception:
                # A worker may die just after its process status was checked.
                self.diarizer.close()
                self.diarizer = None
                self.speakers_for(request, status, stopped)
                self.diarizer.reset(request.get('num_speakers', 0),
                                    request.get('speaker_memory_file') or data_path('speaker_memory.json', 'WHISPER_SPEAKER_MEMORY'))
        return self.diarizer

    def prepare(self, request, status, stopped):
        self.recognizer_for(request, 'ja', '', status, stopped)
        warning = ''
        if request.get('live_diarize', True):
            try:
                self.speakers_for(request, status, stopped)
            except LiveStopped:
                raise
            except Exception as exc:
                warning = str(exc)
        if stopped():
            raise LiveStopped()
        self.proofreader_for(request)
        self.speech.prepare(status, stopped)
        noise = self.noise.prepare(request.get('live_noise', False), request.get('compute', 'auto'), status, stopped)
        return {**self.info(), 'speaker_warning': warning, 'noise': noise}

    def proofreader_for(self, request):
        from live_context import LiveProofreader
        enabled = request.get('live_correct', False)
        compute = request.get('compute', 'auto')
        if self.proofreader and (not enabled or self.proofreader.compute != compute
                                or not self.alive(self.proofreader) or self.proofreader.failed):
            self.proofreader.close()
            self.proofreader = None
        if enabled and self.proofreader is None:
            try:
                self.proofreader = LiveProofreader(compute, self.folder)
            except Exception:
                # Optional proofreading must never block model preparation/capture.
                return None
        return self.proofreader

    def info(self):
        return {'device': self.recognizer.device if self.alive(self.recognizer) else '',
                'recognizer_pid': self.recognizer.proc.pid if self.alive(self.recognizer) else None,
                'speaker_pid': self.diarizer.proc.pid if self.alive(self.diarizer) else None,
                'proofreader_pid': self.proofreader.proc.pid if self.alive(self.proofreader) else None,
                'noise': self.noise.snapshot(),
                'speaker_profiles': len(self.diarizer.profiles) if self.alive(self.diarizer) else 0}

    def park(self):
        if self.recognizer:
            self.recognizer.stopped = self.closing
            self.recognizer.prompt = ''
        if self.diarizer:
            self.diarizer.stopped = self.closing

    def close(self):
        self.speech.close()
        self.noise.close()
        for worker in (self.recognizer, self.diarizer, self.proofreader):
            if worker:
                worker.close()
        self.recognizer = self.diarizer = None
        self.proofreader = None


def serve(folder):
    import engine
    from context_ai import set_compute_mode
    from realtime import run_live
    commands = queue.Queue()
    closing = threading.Event()
    state = {'latest': None, 'job_id': None, 'stop_path': None}
    models = LiveModels(folder, closing.is_set)
    original_emit = engine.emit

    def emit(kind, **values):
        original_emit(kind, job_id=state['job_id'], **values)
    engine.emit = emit

    def read_commands():
        try:
            for line in sys.stdin:
                command = json.loads(line)
                if command['command'] == 'shutdown':
                    break
                if command['command'] == 'prepare':
                    state['latest'] = command['token']
                commands.put(command)
        except (OSError, ValueError, KeyError):
            pass
        finally:
            closing.set()
            if state['stop_path']:
                state['stop_path'].touch()
            commands.put(None)
    threading.Thread(target=read_commands, daemon=True).start()
    try:
        while not closing.is_set():
            command = commands.get()
            if command is None:
                break
            if command['command'] == 'release_context':
                if models.proofreader:
                    models.proofreader.close()
                    models.proofreader = None
                continue
            if command['command'] == 'prepare':
                token = command['token']
                if token != state['latest']:
                    continue
                cancelled = lambda: closing.is_set() or token != state['latest']
                try:
                    info = models.prepare(command['settings'],
                        lambda text: emit('live_preload_status', token=token, text=text), cancelled)
                    if not cancelled():
                        emit('live_preload_ready', token=token, **info)
                except LiveStopped:
                    pass
                except Exception as exc:
                    emit('live_preload_error', token=token, message=str(exc))
                finally:
                    models.park()
            elif command['command'] == 'run':
                state['job_id'] = command['job_id']
                code = 0
                try:
                    path = Path(command['request_path'])
                    state['stop_path'] = path.parent / 'stop-live'
                    if closing.is_set():
                        state['stop_path'].touch()
                    request = json.loads(path.read_text('utf-8'))
                    if request.get('operation') != 'realtime':
                        raise ValueError('リアルタイム以外の処理は実行できません。')
                    set_compute_mode(request.get('compute', 'auto'))
                    run_live(request, path.parent, models=models)
                except Exception as exc:
                    traceback.print_exc(file=sys.stderr)
                    emit('error', message=str(exc))
                    code = 1
                finally:
                    models.park()
                    emit('resident_job_exit', code=code, model_processes=models.info())
                    state['job_id'] = state['stop_path'] = None
    finally:
        models.close()


if __name__ == '__main__':
    serve(sys.argv[1])
