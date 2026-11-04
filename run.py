"""Start the source distribution with its own Python environment."""
import argparse
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import sys
from platform_support import IS_MAC, environment_python, tool_path

ROOT = Path(__file__).resolve().parent
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


def check_environment():
    import tkinter
    if IS_MAC:
        import sounddevice
        import ScreenCaptureKit
    else:
        import pyaudiowpatch
        import windows_capture
    import sherpa_onnx
    import torch
    import transformers
    import yt_dlp
    import einops
    import safetensors
    ffmpeg = tool_path('ffmpeg')
    result = subprocess.run([str(ffmpeg), '-hide_banner', '-filters'], capture_output=True,
                            text=True, errors='replace', check=True, timeout=20, creationflags=NO_WINDOW)
    if not any(' ass ' in line for line in result.stdout.splitlines()):
        raise RuntimeError('FFmpeg has no ASS subtitle filter. Run setup again.')
    subprocess.run([str(tool_path('node')), '--version'], check=True, timeout=15, creationflags=NO_WINDOW)
    for name in ('segmentation.onnx', 'embedding.onnx'):
        if not (ROOT / 'models/speakers' / name).is_file():
            raise RuntimeError('Speaker models are missing. Run setup again.')
    print(json.dumps({'python': sys.version.split()[0], 'torch': torch.__version__,
                      'cuda_available': torch.cuda.is_available(), 'mps_available': torch.backends.mps.is_available(), 'transformers': transformers.__version__,
                      'ffmpeg_subtitles': True, 'speaker_models': True}, indent=2), flush=True)


def main():
    parser = argparse.ArgumentParser(description='Whisper Local')
    parser.add_argument('--check', action='store_true', help='Check installed dependencies without recording audio')
    parser.add_argument('--smoke-test', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    # --check also runs inside the updater while it owns the installation lock.
    if not args.check:
        from app_update import installation_lock, recover_pending
        with installation_lock(ROOT):
            recover_pending(ROOT)
    from portable_runtime import repair_environment
    repair_environment(ROOT)
    python = environment_python(ROOT)
    if not python.is_file():
        raise RuntimeError('start.command（Windowsはstart.cmd）を開くと、初回セットアップから起動まで自動で進みます。')
    if Path(sys.prefix).resolve() != python.parent.parent.resolve():
        return subprocess.call([str(python), '-X', 'utf8', str(Path(__file__).resolve()), *sys.argv[1:]], cwd=ROOT)
    if args.check:
        check_environment()
        return 0
    if IS_MAC:
        from setup_mac import main as setup
    else:
        from setup_local import main as setup
    setup(['--ensure'])
    packages = ('torch', 'transformers', 'pillow', 'sherpa-onnx') + (('sounddevice', 'pyobjc-framework-ScreenCaptureKit') if IS_MAC else ('PyAudioWPatch', 'windows-capture'))
    for package in packages:
        importlib.metadata.version(package)
    if not tool_path('ffmpeg').is_file():
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
