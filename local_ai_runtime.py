"""A private, loopback-only Ollama process owned by the transcription worker."""
from __future__ import annotations
import atexit
import os
from pathlib import Path
import socket
import subprocess
import time
import signal
from platform_support import IS_MAC, tool_path

ROOT = Path(__file__).resolve().parent
EXECUTABLE = tool_path('ollama') if IS_MAC else ROOT / ".tools" / "ollama" / "ollama.exe"
_process = None
_log = None
_url = None

def available():
    return EXECUTABLE.is_file()

def stop():
    global _process, _log, _url
    if _process is not None:
        if _process.poll() is None:
            if IS_MAC:
                try: os.killpg(_process.pid, signal.SIGTERM)
                except ProcessLookupError: pass
                try: _process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(_process.pid, signal.SIGKILL)
                    _process.wait(timeout=5)
            else:
                subprocess.run(["taskkill", "/PID", str(_process.pid), "/T", "/F"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=15)
        _process = None
    if _log is not None:
        _log.close()
        _log = None
    _url = None

def start(compute, progress):
    global _process, _log, _url
    if _process is not None and _process.poll() is None:
        return _url
    stop()
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    _url = f"http://127.0.0.1:{port}"
    env = os.environ.copy()
    env.update(OLLAMA_HOST=f"127.0.0.1:{port}", OLLAMA_MODELS=str(ROOT / "models" / "ollama"),
               OLLAMA_NO_CLOUD="1", OLLAMA_KEEP_ALIVE="30s", OLLAMA_NUM_PARALLEL="1",
               OLLAMA_MAX_LOADED_MODELS="1")
    if compute == "cpu":
        env.update(CUDA_VISIBLE_DEVICES="-1", ROCR_VISIBLE_DEVICES="-1", GGML_VK_VISIBLE_DEVICES="-1")
    logdir = ROOT / ".cache" / "ollama"
    logdir.mkdir(parents=True, exist_ok=True)
    _log = (logdir / f"server-{os.getpid()}.log").open("wb")
    progress("同梱の文脈AIを起動しています…")
    try:
        _process = subprocess.Popen([str(EXECUTABLE), "serve"], cwd=EXECUTABLE.parent,
                                    stdout=_log, stderr=_log, env=env,
                                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), start_new_session=IS_MAC)
    except OSError:
        stop()
        raise
    return _url

def alive():
    return _process is not None and _process.poll() is None

atexit.register(stop)
