"""Small platform boundary; never reuse Windows executables on a copied Mac install."""
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
IS_MAC = sys.platform == 'darwin'


def python_executable():
    return Path(sys.executable).with_name('python.exe') if os.name == 'nt' else Path(sys.executable)


def environment_python(root=ROOT, windowed=False):
    if IS_MAC:
        return Path(root) / '.venv-macos/bin/python'
    return Path(root) / '.venv/Scripts' / ('pythonw.exe' if windowed else 'python.exe')


def tool_path(name, root=ROOT):
    if not IS_MAC:
        return Path(root) / '.tools' / (name + '.exe')
    candidates = [Path(root) / '.tools/macos-arm64' / name]
    if name == 'ffmpeg': candidates.append(Path('/opt/homebrew/opt/ffmpeg-full/bin/ffmpeg'))
    candidates.append(Path('/opt/homebrew/bin') / name)
    for path in candidates:
        if path.is_file():
            return path
    return Path(shutil.which(name) or '/opt/homebrew/bin/' + name)


def torch_backends(compute, cuda_available):
    if compute == 'cpu':
        return ['cpu']
    if IS_MAC and platform.machine().lower() in {'arm64', 'aarch64'}:
        return ['mps', 'cpu']
    return (['cuda'] if cuda_available() else []) + ['cpu']


def open_path(path):
    if os.name == 'nt':
        os.startfile(str(path))
    else:
        subprocess.Popen(['/usr/bin/open' if IS_MAC else 'xdg-open', str(path)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def play_wav(path, previous=None):
    if IS_MAC:
        if previous and previous.poll() is None:
            previous.terminate()
            previous.wait(timeout=3)
        return subprocess.Popen(['/usr/bin/afplay', str(path)]) if path else None
    import winsound
    winsound.PlaySound(str(path), winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT) if path else winsound.PlaySound(None, 0)
    return None
