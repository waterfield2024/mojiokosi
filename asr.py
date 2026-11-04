"""Portable whisper.cpp backends, with isolated GPU attempts and CPU fallback."""
from __future__ import annotations
from platform_support import IS_MAC
import ctypes
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import time
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parent
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
MODELS = {
    "base": ("ggml-base.bin", 147951465, "60ed5bc3dd14eea856493d334349b405782ddcaf0028d4b5df4088345fba2efe"),
    "small": ("ggml-small.bin", 487601967, "1be3a9b2063867b937e64e2ec7483364a79917e157fa98c5d94b5c1fffea987b"),
    "turbo": ("ggml-large-v3-turbo.bin", 1624555275, "1fc70f774d38eb169993ac391eea357ef47c88757ef72ee5943879b7e8e2bc69"),
}
COMPUTE_OPTIONS = {"自動": "auto", "GPU優先": "gpu", "CPU": "cpu"}
from user_data import data_path
SETTINGS = data_path("compute_settings.json", "WHISPER_COMPUTE_SETTINGS")


class NativeBackendError(RuntimeError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def native_policy_rejection(error):
    return ((isinstance(error, NativeBackendError) and (error.code & 0xffffffff) in {0xc0e90002, 0xc0000428})
            or getattr(error, 'winerror', None) in {4551, 577})

def load_compute():
    try:
        value = json.loads(SETTINGS.read_text("utf-8"))["compute"]
        return value if value in COMPUTE_OPTIONS.values() else "auto"
    except (OSError, ValueError, KeyError, TypeError):
        return "auto"

def save_compute(value):
    if value not in COMPUTE_OPTIONS.values():
        raise ValueError("処理方式が正しくありません。")
    from context_ai import write_json
    write_json(SETTINGS, {"compute": value})

def nvidia_available():
    if os.name != "nt":
        return False
    try:
        cuda = ctypes.WinDLL("nvcuda.dll")
        count = ctypes.c_int()
        return cuda.cuInit(0) == 0 and cuda.cuDeviceGetCount(ctypes.byref(count)) == 0 and count.value > 0
    except OSError:
        return False

def backend_order(compute):
    if compute not in COMPUTE_OPTIONS.values():
        raise ValueError("処理方式が正しくありません。")
    if compute == "cpu":
        return ["cpu"]
    return (["cuda"] if nvidia_available() else []) + ["vulkan", "cpu"]

def native_path(path, job=None):
    """Older Windows CLI builds need an ANSI-safe existing filename."""
    value = str(Path(path).resolve())
    if job is not None:
        try:
            relative = os.path.relpath(value, str(Path(job).resolve()))
            if relative.isascii():
                return relative
        except ValueError:
            pass
    if os.name == "nt" and not value.isascii():
        buffer = ctypes.create_unicode_buffer(32768)
        if ctypes.windll.kernel32.GetShortPathNameW(value, buffer, len(buffer)):
            return buffer.value
    return value

def ensure_model(name, models, progress, stopped=lambda: False):
    if name not in MODELS:
        raise ValueError("認識モデルが正しくありません。")
    filename, size, digest = MODELS[name]
    if stopped():
        raise InterruptedError("モデルの準備を中止しました。")
    target = Path(models) / filename
    if target.is_file() and target.stat().st_size == size:
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(".download-" + uuid.uuid4().hex)
    progress(f"認識モデルをダウンロード中… {name} / 約{size // 1000000}MB")
    try:
        request = urllib.request.Request("https://huggingface.co/ggerganov/whisper.cpp/resolve/main/" + filename,
                                         headers={"User-Agent": "WhisperDesktop/2.0"})
        checksum, received, previous = hashlib.sha256(), 0, 0
        with urllib.request.urlopen(request, timeout=60) as response, temporary.open("wb") as output:
            while block := response.read(1024 * 1024):
                if stopped():
                    raise InterruptedError("モデルの準備を中止しました。")
                output.write(block)
                checksum.update(block)
                received += len(block)
                now = time.monotonic()
                if now - previous > .8:
                    progress(f"認識モデルをダウンロード中… {name} / {min(100, received * 100 // size)}%")
                    previous = now
        if received != size or checksum.hexdigest() != digest:
            raise RuntimeError("モデルのダウンロードを検証できませんでした。再実行してください。")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target

def parse_result(data):
    segments = []
    for row in data["transcription"]:
        text = row["text"].strip()
        if not text:
            continue
        start, end = row["offsets"]["from"] / 1000, row["offsets"]["to"] / 1000
        words = []
        for token in row.get("tokens", []):
            if token.get("id", 99999) >= 50257 or "offsets" not in token:
                continue
            word = token["text"]
            if not word:
                continue
            a = max(start, token["offsets"]["from"] / 1000)
            b = min(end, max(a, token["offsets"]["to"] / 1000))
            words.append({"start": a, "end": b, "word": word})
        # Never fabricate word timing if an older runtime cannot represent CJK tokens.
        if "".join(w["word"] for w in words).strip() != text:
            words = []
        segments.append({"start": start, "end": max(start, end), "text": text, "words": words})
    return {"language": data["result"]["language"], "segments": segments}

def run_backend(backend, wav, model, language, prompt, job, progress):
    from native_errors import handle_native_errors
    handle_native_errors()
    executable = ROOT / ".tools" / ("whisper-" + backend) / "whisper-cli.exe"
    if not executable.is_file():
        raise RuntimeError(f"{backend}版の認識エンジンがありません。")
    output = Path(job) / ("recognition_" + backend + ".json")
    output.unlink(missing_ok=True)
    command = [str(executable), "-m", native_path(model, job), "-f", native_path(wav, job),
               "-l", language, "-ojf", "-of", output.stem, "-pp",
               "-t", str(min(8, os.cpu_count() or 4))]
    if backend == "cpu":
        command.append("-ng")
    if prompt:
        command.extend(["--prompt", " ".join(prompt.splitlines())])
    # The CLI reads argv in Windows ACP, but Whisper expects UTF-8 text.
    # Its response file accepts raw argument bytes, one argument per line.
    response_file = Path(job) / ("arguments_" + backend + ".txt")
    encoded = []
    for index, argument in enumerate(command[1:], 1):
        encoding = "utf-8" if command[index - 1] == "--prompt" else ("mbcs" if os.name == "nt" else "utf-8")
        encoded.append(argument.encode(encoding))
    response_file.write_bytes(b"\n".join(encoded) + b"\n")
    command = [str(executable), "@" + response_file.name]
    progress(f"文字起こし中… {model.stem} / {backend.upper()}")
    lines = []
    log_path = Path(job) / ("recognition_" + backend + ".log")
    with log_path.open("w", encoding="utf-8") as logfile, subprocess.Popen(
            command, cwd=job, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            creationflags=NO_WINDOW) as process:
        for raw in process.stdout:
            line = raw.decode("utf-8", errors="replace")
            lines.append(line)
            logfile.write(line)
            logfile.flush()
            match = re.search(r"progress\s*=\s*(\d+)%", line)
            if match:
                progress(f"文字起こし中… {backend.upper()} / {match[1]}%")
        code = process.wait()
    log = "".join(lines)
    (Path(job) / ("recognition_" + backend + ".log")).write_text(log, "utf-8")
    if code or not output.is_file():
        raise NativeBackendError(code, f"{backend.upper()}を利用できませんでした（終了コード {code}）。" + log[-600:])
    result = parse_result(json.loads(output.read_text("utf-8", errors="replace")))
    # A runtime can fall back internally; report the backend actually used.
    used = backend
    if backend != "cpu" and not re.search(r"using (?:CUDA|Vulkan)\d* backend", log, re.I):
        used = "cpu"
    result.update(device=used.upper(), backend="whisper.cpp/" + used)
    return result

def native_backends(compute="auto", executable="whisper-cli.exe"):
    if compute not in COMPUTE_OPTIONS.values(): raise ValueError('処理方式が正しくありません。')
    if IS_MAC: return []  # Never execute copied Windows backends.
    return [backend for backend in backend_order(compute)
            if (ROOT / '.tools' / ('whisper-' + backend) / executable).is_file()]


def prepare_whisper_model(name, models, progress=lambda s: None):
    if native_backends():
        return ensure_model(name, models, progress)
    from whisper_hf_model import ensure_whisper_hf
    return ensure_whisper_hf(name, models, progress)


def transcribe(wav, name, language, prompt, job, models, compute="auto", progress=lambda s: None):
    from recognition_models import GRANITE_MODEL, HAAS_MODEL
    if name == HAAS_MODEL:
        from haas_asr import transcribe_file
        return transcribe_file(wav,language,prompt,job,models,compute,progress)
    if name == GRANITE_MODEL:
        from granite_asr import transcribe_file
        return transcribe_file(wav,language,prompt,job,models,compute,progress)
    backends = native_backends(compute)
    model = ensure_model(name, models, progress) if backends else None
    failures = []
    for backend in backends:
        try:
            result = run_backend(backend, wav, model, language, prompt, job, progress)
            result["fallbacks"] = failures
            return result
        except (OSError, RuntimeError, ValueError, KeyError) as exc:
            failures.append({"backend": backend, "reason": str(exc)[:1200]})
            if native_policy_rejection(exc):
                break
            if backend != "cpu":
                progress(f"{backend.upper()}を利用できないため、別の処理方式で再試行します…")
    from whisper_hf_file import transcribe_file
    progress("Whisperで文字起こしします…")
    try:
        result = transcribe_file(wav, name, language, prompt, job, compute, progress)
        result['fallbacks'] = failures + result.get('fallbacks', [])
        return result
    except (OSError, RuntimeError, ValueError) as exc:
        detail = '; '.join(item['reason'] for item in failures)
        raise RuntimeError('Whisperで文字起こしできませんでした。\n互換方式: ' + str(exc) + '\n従来方式: ' + detail) from exc
