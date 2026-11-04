"""Create a project-local environment; never change Windows protection settings."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import ctypes
import hashlib
import importlib.metadata
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
SETUP_INPUTS = ('setup_local.py', 'run.py', 'portable_runtime.py', 'requirements.txt', 'requirements-lock.txt',
                'whisper_hf_manifest.json', 'granite_model_manifest.json', 'haas_model_manifest.json',
                'reuse_assets.py', 'reuse_model/config.json', 'speech_gate.py', 'speech_preferences.py')


def fingerprint(root=ROOT):
    digest = hashlib.sha256()
    for name in SETUP_INPUTS:
        digest.update(name.encode('utf-8'))
        digest.update((root / name).read_bytes())
    return digest.hexdigest()


def read_state(root=ROOT):
    try:
        value = json.loads((root / '.setup-complete.json').read_text('utf-8'))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


def required_assets(model, noise, root=ROOT):
    paths = ['.tools/ffmpeg.exe', '.tools/node.exe', 'models/speakers/segmentation.onnx',
             'models/speakers/embedding.onnx', 'models/vad/silero-v5.onnx']
    if model != 'none':
        if model in ('granite', 'haas'):
            manifest = json.loads((root / (model + '_model_manifest.json')).read_text('utf-8'))
            folder = 'granite-speech-5.0-turboctc' if model == 'granite' else 'haas-ja-v1.0-onnx'
        else:
            manifest = json.loads((root / 'whisper_hf_manifest.json').read_text('utf-8'))[model]
            folder = 'whisper-hf-' + model
        paths += ['models/' + folder + '/' + name for name in manifest['files']]
    if noise:
        paths.append('models/nemotronlabs-re-use/model.safetensors')
    return paths


def setup_ready(root=ROOT):
    """Fast, offline check, executed by the project's interpreter before launch."""
    state = read_state(root)
    try:
        if state.get('fingerprint') != fingerprint(root):
            return False
        if state['device'] not in ('cuda', 'cpu'):
            return False
        expected = torch_packages(state['device'])
        expected += [line.strip() for line in (root / 'requirements-lock.txt').read_text('utf-8').splitlines()
                     if line.strip() and not line.startswith('#')]
        for requirement in expected:
            name, version = requirement.split('==')
            if importlib.metadata.version(name) != version:
                return False
        for name in required_assets(state['model'], state['noise'], root):
            size = (root / name).stat().st_size
            if size <= 0 or size != state['assets'][name]:
                return False
        return True
    except (OSError, ValueError, TypeError, KeyError, importlib.metadata.PackageNotFoundError):
        return False


def save_state(device, requested_device, model, root=ROOT):
    state = {'device': device, 'requested_device': requested_device, 'model': model, 'noise': device == 'cuda',
             'fingerprint': fingerprint(root)}
    state['assets'] = {name: (root / name).stat().st_size for name in required_assets(model, state['noise'], root)}
    write_state(state, root)


def write_state(state, root=ROOT):
    temporary = root / '.setup-complete.json.tmp'
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), 'utf-8')
    os.replace(temporary, root / '.setup-complete.json')


@contextmanager
def setup_lock(root=ROOT):
    if os.name == 'nt': import msvcrt
    else: import fcntl
    path = root / '.cache/setup.lock'
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as stream:
        if not stream.tell():
            stream.write(b'0')
            stream.flush()
        stream.seek(0)
        try:
            if os.name == 'nt': msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else: fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError('別のセットアップが実行中です。その画面が完了するまでお待ちください。') from exc
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == 'nt': msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else: fcntl.flock(stream, fcntl.LOCK_UN)


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
    command = [str(value) for value in args]
    log = ROOT / '.cache/setup.log'
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open('a', encoding='utf-8') as output:
        with subprocess.Popen(command, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              text=True, encoding='utf-8', errors='replace', **kwargs) as process:
            for line in process.stdout:
                print(line, end='', flush=True)
                output.write(line)
                output.flush()
            if process.wait():
                raise subprocess.CalledProcessError(process.returncode, command)


def torch_packages(device):
    build = 'cu128' if device == 'cuda' else 'cpu'
    # The local version suffix matters when switching an existing environment.
    # A plain ==2.11.0 would also accept an already installed +cu128 CPU switch.
    return [f'{name}=={TORCH_VERSION}+{build}' for name in ('torch', 'torchaudio')]


def prepare_assets(model, noise=False):
    import imageio_ffmpeg
    tools = ROOT / '.tools'
    tools.mkdir(exist_ok=True)
    original = Path(imageio_ffmpeg.get_ffmpeg_exe())
    target = tools / 'ffmpeg.exe'
    if not target.exists() or sha256(target) != sha256(original):
        shutil.copy2(original, target)
    download(f'https://nodejs.org/dist/v{NODE_VERSION}/win-x64/node.exe', tools / 'node.exe', NODE_SHA256)
    prepare_speakers()
    from speech_gate import ensure_vad
    ensure_vad(ROOT / 'models', print)
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
    if noise:
        from reuse_assets import ensure_reuse
        ensure_reuse(ROOT / 'models', print)


def install_torch(python, device):
    index = 'cu128' if device == 'cuda' else 'cpu'
    run([python, '-m', 'pip', 'install', *torch_packages(device),
         '--index-url', 'https://download.pytorch.org/whl/' + index])


def cuda_works(python):
    code = 'import torch; assert torch.cuda.is_available(); torch.ones(1,device="cuda").sum().item(); print(torch.cuda.get_device_name())'
    try:
        run([python, '-X', 'utf8', '-c', code])
        return True
    except subprocess.CalledProcessError:
        return False


def install(args):
    environment = ROOT / '.venv'
    python = environment / 'Scripts/python.exe'
    if args.ensure and python.is_file() and args.device is None and args.model is None:
        result = subprocess.run([str(python), '-X', 'utf8', __file__, '--check-ready'], cwd=ROOT,
                                capture_output=True, timeout=30)
        if result.returncode == 0:
            print('セットアップ済みです。保存済みの環境を使います。', flush=True)
            return
    saved = read_state()
    model = args.model or saved.get('model', 'base')
    if model not in ('base', 'small', 'turbo', 'granite', 'haas', 'none'):
        model = 'base'
    requested = args.device or saved.get('requested_device', 'auto')
    if requested not in ('auto', 'cpu', 'cuda'):
        requested = 'auto'
    device = ('cuda' if nvidia_available() else 'cpu') if requested == 'auto' else requested
    # Invalidate readiness, but retain the chosen options if installation fails.
    write_state({'requested_device': requested, 'model': model}, ROOT)
    print('[1/5] このフォルダー専用のPython環境を確認しています…', flush=True)
    if not python.is_file():
        # venv can resume an interrupted creation without clearing this directory.
        venv.EnvBuilder(with_pip=True).create(environment)
    run([python, '-X', 'utf8', '-c', 'import sys,struct,tkinter;assert sys.version_info[:2]==(3,12) and struct.calcsize("P")==8'])
    print(f'[2/5] {device.upper()}用の認識ライブラリを準備しています…', flush=True)
    install_torch(python, device)
    if device == 'cuda' and not cuda_works(python):
        if requested == 'cuda':
            raise RuntimeError('CUDAで実行できません。NVIDIAドライバーを確認するか setup.cmd --device cpu を実行してください。')
        print('この環境ではCUDAを使えないため、CPU版で準備します。Nemotronノイズ除去はGPUが必要です。', flush=True)
        device = 'cpu'
        install_torch(python, device)
    print('[3/5] アプリのライブラリを準備しています…', flush=True)
    run([python, '-m', 'pip', 'install', '-r', ROOT / 'requirements.txt', '--index-url', 'https://pypi.org/simple'])
    run([python, '-m', 'pip', 'check'])
    print('[4/5] 動画処理・話者判別・認識モデル' + ('・Nemotron' if device == 'cuda' else '') + 'を準備しています…', flush=True)
    command = [python, '-X', 'utf8', __file__, '--assets-only', '--model', model]
    if device == 'cuda':
        command.append('--prepare-noise')
    run(command)
    print('[5/5] 起動に必要な機能を確認しています…', flush=True)
    run([python, '-X', 'utf8', ROOT / 'run.py', '--check'])
    save_state(device, requested, model)
    print('\nセットアップが完了しました。次回も start.cmd で起動できます。', flush=True)


def main(argv=None):
    if sys.platform == 'darwin':
        from setup_mac import main as mac_setup
        return mac_setup(argv)
    parser = argparse.ArgumentParser(description='Whisper Local: Windows Python setup')
    parser.add_argument('--device', choices=['auto', 'cpu', 'cuda'])
    parser.add_argument('--model', choices=['base', 'small', 'turbo', 'granite', 'haas', 'none'])
    parser.add_argument('--ensure', action='store_true', help='Set up only when dependencies or assets need updating')
    parser.add_argument('--launch', action='store_true', help='Start the application after setup')
    parser.add_argument('--check-ready', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--prepare-noise', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--assets-only', action='store_true', help='Prepare downloads using an already installed environment')
    args = parser.parse_args(argv)
    if os.name != 'nt' or struct.calcsize('P') != 8 or sys.version_info[:2] != (3, 12):
        parser.error('Windows x64 / Python 3.12 が必要です。start.cmd を開いてください。')
    import tkinter  # Fail early if the selected Python lacks Tcl/Tk.
    if args.check_ready:
        return 0 if setup_ready() else 10
    if args.assets_only:
        prepare_assets(args.model or 'base', args.prepare_noise)
        return 0
    with setup_lock():
        install(args)
    if args.launch:
        return subprocess.call([str(ROOT / '.venv/Scripts/python.exe'), '-X', 'utf8', str(ROOT / 'run.py')], cwd=ROOT)
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ImportError, subprocess.SubprocessError) as exc:
        print(f'\nセットアップを完了できませんでした: {exc}\n通信や空き容量を確認し、アプリを閉じて start.cmd を再実行してください。\n詳細: {ROOT / ".cache/setup.log"}', file=sys.stderr)
        raise SystemExit(1)
