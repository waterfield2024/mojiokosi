"""Start the source distribution with its own Python environment."""
import argparse
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


def check_environment():
    import tkinter
    import pyaudiowpatch
    import sherpa_onnx
    import windows_capture
    import torch
    import transformers
    import yt_dlp
    import einops
    import safetensors
    ffmpeg = ROOT / '.tools/ffmpeg.exe'
    result = subprocess.run([str(ffmpeg), '-hide_banner', '-filters'], capture_output=True,
                            text=True, errors='replace', check=True, timeout=20, creationflags=NO_WINDOW)
    if not any(' ass ' in line for line in result.stdout.splitlines()):
        raise RuntimeError('FFmpeg has no ASS subtitle filter. Run setup again.')
    subprocess.run([str(ROOT / '.tools/node.exe'), '--version'], check=True, timeout=15, creationflags=NO_WINDOW)
    for name in ('segmentation.onnx', 'embedding.onnx'):
        if not (ROOT / 'models/speakers' / name).is_file():
            raise RuntimeError('Speaker models are missing. Run setup again.')
    print(json.dumps({'python': sys.version.split()[0], 'torch': torch.__version__,
                      'cuda_available': torch.cuda.is_available(), 'transformers': transformers.__version__,
                      'ffmpeg_subtitles': True, 'speaker_models': True}, indent=2), flush=True)


def main():
    parser = argparse.ArgumentParser(description='Whisper Local')
    parser.add_argument('--check', action='store_true', help='Check installed dependencies without recording audio')
    parser.add_argument('--smoke-test', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    python = ROOT / '.venv/Scripts/python.exe'
    if not python.is_file():
        raise RuntimeError('start.cmd を開くと、初回セットアップから起動まで自動で進みます。')
    if Path(sys.executable).resolve() != python.resolve():
        return subprocess.call([str(python), '-X', 'utf8', str(Path(__file__).resolve()), *sys.argv[1:]], cwd=ROOT)
    if args.check:
        check_environment()
        return 0
    from setup_local import main as setup
    setup(['--ensure'])
    for package in ('torch', 'transformers', 'pillow', 'PyAudioWPatch', 'sherpa-onnx', 'windows-capture'):
        importlib.metadata.version(package)
    if not (ROOT / '.tools/ffmpeg.exe').is_file():
        raise RuntimeError('セットアップが未完了です。start.cmd を再実行してください。')
    command = [str(python), '-X', 'utf8', str(ROOT / 'app.py')]
    if args.smoke_test:
        command.append('--smoke-test')
    return subprocess.call(command, cwd=ROOT)


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ImportError, importlib.metadata.PackageNotFoundError, subprocess.SubprocessError) as exc:
        print(f'起動を完了できませんでした: {exc}\nセットアップを再実行し、表示されたエラーを確認してください。', file=sys.stderr)
        raise SystemExit(1)
