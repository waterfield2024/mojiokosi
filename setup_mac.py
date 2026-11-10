"""Apple Silicon setup; models/UserData are shared, native environments are not."""
import argparse
from hashlib import sha256
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import venv
from platform_support import environment_python, tool_path

ROOT = Path(__file__).resolve().parent
STATE = ROOT / '.setup-macos.json'
INPUTS = ('setup_mac.py', 'run.py', 'platform_support.py', 'requirements-macos.txt',
          'whisper_hf_manifest.json', 'granite_model_manifest.json', 'haas_model_manifest.json', 'speech_gate.py')


def fingerprint():
    digest = sha256()
    for name in INPUTS:
        digest.update((ROOT / name).read_bytes())
    return digest.hexdigest()


def requirements():
    return [line.strip() for line in (ROOT/'requirements-macos.txt').read_text('utf-8').splitlines()
            if line.strip() and not line.startswith('#')]


def state():
    try: return json.loads(STATE.read_text('utf-8'))
    except (OSError, ValueError): return {}


def current_media_tool(name):
    try:
        path = str(tool_path(name))
        if name == 'node':
            value = subprocess.check_output([path, '--version'], text=True, timeout=10).strip()
            version = tuple(map(int, value.removeprefix('v').split('.')))
            fixed = {22: (22, 23, 4), 24: (24, 22, 1), 26: (26, 12, 1)}
            return version[0] > 26 or (version[0] in fixed and version >= fixed[version[0]])
        output = subprocess.check_output([path, '-version'], text=True, timeout=10)
        version = re.match(r'ffmpeg version (\d+)\.(\d+)(?:\.(\d+))?', output)
        return bool(version and tuple(int(v or 0) for v in version.groups()) >= (9, 0, 2))
    except (IndexError, ValueError, OSError, subprocess.SubprocessError):
        return False


def ensure_media_tools():
    """Both the launcher and already-installed update helpers enter setup here."""
    from setup_local import run
    brew = '/opt/homebrew/bin/brew'
    for name, formula in (('ffmpeg', 'ffmpeg-full'), ('node', 'node')):
        if current_media_tool(name):
            continue
        installed = subprocess.run([brew, 'list', '--versions', formula], capture_output=True, check=False)
        run([brew, 'upgrade' if installed.stdout.strip() else 'install', formula])
        if not current_media_tool(name):
            raise RuntimeError(f'{formula} の更新後のバージョンを確認できません。')


def ready():
    from setup_local import required_assets
    saved = state()
    if saved.get('fingerprint') != fingerprint(): return False
    try:
        for item in requirements():
            name, version = item.split('==')
            if importlib.metadata.version(name) != version: return False
        if not all(current_media_tool(name) for name in ('ffmpeg', 'node')): return False
        for name in ('ffmpeg', 'node', 'ollama'):
            if not tool_path(name).is_file(): return False
        for name in required_assets(saved['model'], False)[2:]:
            if (ROOT/name).stat().st_size != saved['assets'][name]: return False
        return True
    except (OSError, KeyError, ValueError, importlib.metadata.PackageNotFoundError): return False


def assets(model):
    from setup_local import prepare_speakers
    from speech_gate import ensure_vad
    prepare_speakers()
    ensure_vad(ROOT/'models', print)
    if model == 'none': return
    if model == 'granite':
        from granite_model import ensure_granite
        ensure_granite(ROOT/'models', print)
    elif model == 'haas':
        from haas_model import ensure_haas
        ensure_haas(ROOT/'models', print)
    else:
        from whisper_hf_model import ensure_whisper_hf
        ensure_whisper_hf(model, ROOT/'models', print)


def prepare_context():
    import time
    import context_ai as ai
    import local_ai_runtime as runtime
    from setup_local import run
    try:
        ai.BASE_URL = runtime.start('auto', print)
        for _ in range(120):
            try:
                listing = ai.local_request('/api/tags', timeout=2)
                break
            except RuntimeError:
                if not runtime.alive(): raise
                time.sleep(.25)
        else: raise RuntimeError('文脈AIを起動できませんでした。')
        if not any(row.get('name') == ai.MODEL for row in listing.get('models', [])):
            print('文脈AIを取得します（初回のみ約5GB）…', flush=True)
            run([tool_path('ollama'), 'pull', ai.MODEL], env=dict(os.environ, OLLAMA_HOST=ai.BASE_URL))
    finally:
        runtime.stop()


def main(argv=None):
    parser = argparse.ArgumentParser(description='Whisper Local — Apple Silicon')
    parser.add_argument('--ensure', action='store_true')
    parser.add_argument('--launch', action='store_true')
    parser.add_argument('--check-ready', action='store_true')
    parser.add_argument('--assets-only', action='store_true')
    parser.add_argument('--model', choices=['base', 'small', 'turbo', 'granite', 'haas', 'none'])
    parser.add_argument('--device', choices=['auto', 'cpu', 'mps'])
    parser.add_argument('--without-context-ai', action='store_true', help='Skip the optional 7B proofreading model download')
    args = parser.parse_args(argv)
    if sys.platform != 'darwin' or platform.machine() != 'arm64' or sys.version_info[:2] != (3, 12):
        parser.error('Apple SiliconネイティブのPython 3.12でstart.commandを実行してください。')
    if int(platform.mac_ver()[0].split('.')[0]) < 14:
        parser.error('macOS 14 Sonoma以降が必要です。')
    import tkinter
    if args.check_ready: return 0 if ready() else 10
    if args.assets_only: assets(args.model or 'base'); return 0
    from setup_local import setup_lock, run, required_assets
    python = environment_python(ROOT)
    with setup_lock(ROOT):
        ensure_media_tools()
        check = subprocess.run([str(python), '-X', 'utf8', __file__, '--check-ready'], capture_output=True) if python.is_file() else None
        if not (args.ensure and not args.model and not args.device and check and check.returncode == 0):
            saved = state()
            model = args.model or saved.get('model', 'base')
            device = args.device or saved.get('device', 'auto')
            without_context = args.without_context_ai or (args.ensure and saved.get('without_context_ai', False))
            if model not in ('base', 'small', 'turbo', 'granite', 'haas', 'none'): model = 'base'
            print('[1/4] Mac専用のPython環境を準備しています…', flush=True)
            if Path(sys.prefix).resolve() != python.parent.parent.resolve() or not python.is_file():
                venv.EnvBuilder(with_pip=True, upgrade=python.is_file()).create(python.parent.parent)
            print('[2/4] Apple Silicon用のライブラリを準備しています…', flush=True)
            run([python, '-m', 'pip', 'install', '--only-binary=:all:', 'pip==26.3'])
            run([python, '-m', 'pip', 'install', '-r', ROOT/'requirements-macos.txt'])
            run([python, '-m', 'pip', 'check'])
            print('[3/4] 話者・無音判定・認識モデルを準備しています…', flush=True)
            run([python, '-X', 'utf8', __file__, '--assets-only', '--model', model])
            if not without_context:
                run([python, '-X', 'utf8', '-c', 'from setup_mac import prepare_context;prepare_context()'])
            print('[4/4] Mac用の起動機能を確認しています…', flush=True)
            run([python, '-X', 'utf8', ROOT/'run.py', '--check'])
            if args.device:
                run([python, '-c', 'from asr import save_compute;save_compute(' + repr('cpu' if device == 'cpu' else 'auto') + ')'])
            data = dict(fingerprint=fingerprint(), model=model, device=device,
                        without_context_ai=without_context,
                        assets={name: (ROOT/name).stat().st_size for name in required_assets(model, False)[2:]})
            temporary = STATE.with_suffix('.tmp')
            temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), 'utf-8')
            os.replace(temporary, STATE)
    if args.launch:
        return subprocess.call([str(python), '-X', 'utf8', str(ROOT/'run.py')], cwd=ROOT)
    return 0


if __name__ == '__main__':
    try: raise SystemExit(main())
    except (OSError, RuntimeError, ImportError, subprocess.SubprocessError) as exc:
        print(f'セットアップできませんでした：{exc}\n詳細：{ROOT / ".cache/setup.log"}', file=sys.stderr)
        raise SystemExit(1)
