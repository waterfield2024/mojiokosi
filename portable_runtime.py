"""Repair a copied venv using the Python bundled beside it, before launching it."""
from pathlib import Path
import subprocess
import sys


def bundled_python(root):
    root = Path(root).resolve()
    for relative in ('.python/runtime/python.exe', '.python/3.12.10/python.exe'):
        candidate = root / relative
        if candidate.is_file() and candidate.resolve().is_relative_to(root):
            return candidate
    return None


def repair_environment(root=None):
    root = Path(root or Path(__file__).resolve().parent).resolve()
    if sys.platform == "darwin": return False
    base = bundled_python(root)
    environment = root / '.venv'
    if base is None or not environment.is_dir(): return False
    if environment.is_symlink() or environment.is_junction():
        raise RuntimeError('このフォルダー内のPython環境を使ってください。')
    config = environment / 'pyvenv.cfg'
    values = {}
    if config.is_file():
        for line in config.read_text('utf-8').splitlines():
            key, separator, value = line.partition('=')
            if separator: values[key.strip()] = value.strip()
    if (Path(values.get('home', '')).resolve() == base.parent.resolve()
            and (environment / 'Scripts/python.exe').is_file()):
        return False
    from setup_local import setup_lock
    with setup_lock(root):
        print('コピー先のPython環境を準備しています…', flush=True)
        # --upgrade preserves site-packages; no downloads or package changes.
        subprocess.run([str(base), '-X', 'utf8', '-m', 'venv', '--upgrade', '--without-pip', str(environment)],
                       cwd=root, check=True, timeout=120, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    return True


if __name__ == '__main__':
    try:
        repair_environment()
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print('コピー先の実行環境を準備できませんでした：' + str(exc), file=sys.stderr)
        raise SystemExit(1)
