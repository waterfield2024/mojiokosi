"""Create a project-local environment; never change Windows protection settings."""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import venv

ROOT = Path(__file__).resolve().parent
TORCH_VERSION = '2.11.0'
NODE_VERSION = '24.19.0'
NODE_SHA256 = '3602f2bb1a10f2cbab4c36886218a33c1ab3db87290e73b033c46c77147d0237'
SEGMENTATION_SHA256 = '220ad67ca923bef2fa91f2390c786097bf305bceb5e261d4af67b38e938e1079'
SPEAKER_RELEASE = 'https://github.com/k2-fsa/sherpa-onnx/releases/download/'


def sha256(path):
    result = hashlib.sha256()
    with Path(path).open('rb') as stream:
        while chunk := stream.read(1024 * 1024):
            result.update(chunk)
    return result.hexdigest()


def download(url, target, digest):
    """Verify before replacing; partial downloads never become installed files."""
    target = Path(target)
    if target.is_file() and sha256(target) == digest:
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    request = urllib.request.Request(url, headers={'User-Agent': 'WhisperLocal-Setup/1.0'})
    print('Downloading:', target.name, flush=True)
    with tempfile.NamedTemporaryFile(dir=target.parent, prefix='.download-', delete=False) as output:
        temporary = Path(output.name)
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                shutil.copyfileobj(response, output, 1024 * 1024)
        except BaseException:
            output.close()
            temporary.unlink(missing_ok=True)
            raise
    try:
        if sha256(temporary) != digest:
            raise RuntimeError(f'Download checksum mismatch: {target.name}')
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target


def prepare_speakers(root=ROOT):
    folder = root / 'models/speakers'
    segmentation = folder / 'segmentation.onnx'
    if not segmentation.is_file() or sha256(segmentation) != SEGMENTATION_SHA256:
        archive = download(
            SPEAKER_RELEASE + 'speaker-segmentation-models/sherpa-onnx-pyannote-segmentation-3-0.tar.bz2',
            root / '.cache/downloads/segmentation.tar.bz2',
            '24615ee884c897d9d2ba09bb4d30da6bb1b15e685065962db5b02e76e4996488')
        folder.mkdir(parents=True, exist_ok=True)
        with tarfile.open(archive, 'r:bz2') as bundle:
            entry = bundle.getmember('sherpa-onnx-pyannote-segmentation-3-0/model.onnx')
            if not entry.isfile() or entry.size != 5992913:
                raise RuntimeError('Unexpected speaker segmentation asset')
            # Read this member only: never extract paths, links or other archive entries.
            with bundle.extractfile(entry) as stream:
                data = stream.read()
            if hashlib.sha256(data).hexdigest() != SEGMENTATION_SHA256:
                raise RuntimeError('Speaker segmentation checksum mismatch')
            with tempfile.NamedTemporaryFile(dir=folder, delete=False) as output:
                temporary = Path(output.name)
                output.write(data)
            try:
                os.replace(temporary, segmentation)
            finally:
                temporary.unlink(missing_ok=True)
    download(SPEAKER_RELEASE + 'speaker-recongition-models/3dspeaker_speech_eres2net_base_sv_zh-cn_3dspeaker_16k.onnx',
             folder / 'embedding.onnx', '1a331345f04805badbb495c775a6ddffcdd1a732567d5ec8b3d5749e3c7a5e4b')


def nvidia_available():
    try:
        driver = ctypes.WinDLL('nvcuda.dll')
        count = ctypes.c_int()
        return driver.cuInit(0) == 0 and driver.cuDeviceGetCount(ctypes.byref(count)) == 0 and count.value > 0
    except (OSError, AttributeError):
        return False


def run(args, **kwargs):
    subprocess.run([str(value) for value in args], check=True, cwd=ROOT, **kwargs)


def torch_packages(device):
    build = 'cu128' if device == 'cuda' else 'cpu'
    # The local version suffix matters when switching an existing environment.
    # A plain ==2.11.0 would also accept an already installed +cu128 CPU switch.
    return [f'{name}=={TORCH_VERSION}+{build}' for name in ('torch', 'torchaudio')]


def prepare_assets(model):
    import imageio_ffmpeg
    tools = ROOT / '.tools'
    tools.mkdir(exist_ok=True)
    original = Path(imageio_ffmpeg.get_ffmpeg_exe())
    target = tools / 'ffmpeg.exe'
    if not target.exists() or sha256(target) != sha256(original):
        shutil.copy2(original, target)
    download(f'https://nodejs.org/dist/v{NODE_VERSION}/win-x64/node.exe', tools / 'node.exe', NODE_SHA256)
    prepare_speakers()
    if model != 'none':
        if model == 'granite':
            from granite_model import ensure_granite
            ensure_granite(ROOT / 'models', print)
        elif model == 'haas':
            from haas_model import ensure_haas
            ensure_haas(ROOT / 'models', print)
        else:
            from whisper_hf_model import ensure_whisper_hf
            ensure_whisper_hf(model, ROOT / 'models', print)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Whisper Local: Windows Python setup')
    parser.add_argument('--device', choices=['auto', 'cpu', 'cuda'], default='auto')
    parser.add_argument('--model', choices=['base', 'small', 'turbo', 'granite', 'haas', 'none'], default='base')
    parser.add_argument('--assets-only', action='store_true', help='Prepare downloads using an already installed environment')
    args = parser.parse_args(argv)
    if os.name != 'nt' or struct.calcsize('P') != 8 or sys.version_info[:2] != (3, 12):
        parser.error('Windows x64 and Python 3.12 are required. Run: py -3.12 setup_local.py')
    import tkinter  # Fail early if the selected Python lacks Tcl/Tk.
    if args.assets_only:
        prepare_assets(args.model)
        return
    environment = ROOT / '.venv'
    python = environment / 'Scripts/python.exe'
    if not python.is_file():
        if environment.exists() and any(environment.iterdir()):
            raise RuntimeError('.venv exists but has no interpreter. Rename it before retrying setup.')
        print('Creating .venv...', flush=True)
        venv.EnvBuilder(with_pip=True).create(environment)
    run([python, '-c', 'import sys,struct;assert sys.version_info[:2]==(3,12) and struct.calcsize("P")==8'])
    device = ('cuda' if nvidia_available() else 'cpu') if args.device == 'auto' else args.device
    index = 'cu128' if device == 'cuda' else 'cpu'
    print(f'Installing {device.upper()} dependencies...', flush=True)
    run([python, '-m', 'pip', 'install', *torch_packages(device),
         '--index-url', 'https://download.pytorch.org/whl/' + index])
    run([python, '-m', 'pip', 'install', '-r', ROOT / 'requirements.txt', '--index-url', 'https://pypi.org/simple'])
    run([python, '-m', 'pip', 'check'])
    run([python, '-X', 'utf8', __file__, '--assets-only', '--model', args.model])
    run([python, '-X', 'utf8', ROOT / 'run.py', '--check'])
    (ROOT / '.setup-complete.json').write_text(json.dumps({'device': device, 'model': args.model}, indent=2), 'utf-8')
    print('\nSetup complete. Start with: .venv\\Scripts\\python.exe run.py', flush=True)


if __name__ == '__main__':
    try:
        main()
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f'\nSetup did not finish: {exc}\nFix the reported issue, then run setup again.', file=sys.stderr)
        raise SystemExit(1)
