"""Source-only GitHub updates. This file also runs as an independent recovery helper.

Never extract an archive over the installation: only validated source paths are
copied. UserData, runtimes, models, recordings and Git metadata are not managed.
"""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
import zipfile

REPOSITORY = 'waterfield2024/mojiokosi'
BRANCH = 'main'
API = f'https://api.github.com/repos/{REPOSITORY}'
MAX_FILE = 16 * 1024 * 1024
MAX_TOTAL = 80 * 1024 * 1024
NO_WINDOW = 0x08000000 if os.name == 'nt' else 0
SOURCE_DIRS = {'docs', 'licenses', 'tests', 'reuse_model', '.github'}
ROOT_FILES = {'.gitignore', '.gitattributes', 'LICENSE', 'README.md',
              'THIRD_PARTY_NOTICES.md', 'requirements.txt', 'requirements-lock.txt', 'whisper.ico'}
REQUIRED = {'app.py', 'app_update.py', 'update_ui.py', 'run.py', 'setup_local.py',
            'portable_runtime.py', 'bootstrap.cmd', 'start.cmd', 'requirements-lock.txt'}


def source_path(name):
    """A positive allowlist, independent of GitHub's contents and .gitignore."""
    if not isinstance(name, str) or not name or '\\' in name or ':' in name:
        raise ValueError('更新ファイルのパスが不正です。')
    parts = name.split('/')
    if any(p in ('', '.', '..') or p.endswith((' ', '.')) for p in parts):
        raise ValueError(f'更新ファイルのパスが不正です：{name}')
    for part in parts:
        if (re.search(r'[\x00-\x1f<>"|?*]', part)
                or re.fullmatch(r'(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?', part)):
            raise ValueError(f'Windowsで使えない更新ファイル名です：{name}')
    path = PurePosixPath(name)
    if len(parts) == 1:
        allowed = (name in ROOT_FILES or path.suffix in ('.py', '.cmd')
                   or name.endswith('_manifest.json'))
    else:
        allowed = (parts[0] in SOURCE_DIRS and all(not p.startswith('.') for p in parts[1:])
                   and (path.suffix in ('.py', '.md', '.txt', '.yml', '.yaml')
                        or path.name.endswith(('LICENSE', 'NOTICE'))
                        or name == 'reuse_model/config.json'))
    if not allowed:
        raise ValueError(f'アプリ以外のファイルが更新に含まれています：{name}')
    return name


def inside(base, name):
    """Reject links/junctions as well as lexical traversal before any mutation."""
    base = Path(base).resolve()
    parts = PurePosixPath(name).parts
    if not parts or any(p in ('', '.', '..') for p in parts) or PurePosixPath(name).is_absolute():
        raise ValueError('保存先がアプリのフォルダー外です。')
    current = base
    for part in parts:
        current = current / part
        if current.is_symlink() or (hasattr(current, 'is_junction') and current.is_junction()):
            raise ValueError(f'リンク先には更新できません：{current}')
    if not current.resolve().is_relative_to(base):
        raise ValueError('保存先がアプリのフォルダー外です。')
    return current


def updates_dir(root):
    return inside(root, '.cache/app-updates')


def blob_hash(data):
    return hashlib.sha1(b'blob ' + str(len(data)).encode('ascii') + b'\0' + data).hexdigest()


def file_hash(path):
    if not path.exists():
        return None
    if not path.is_file() or path.stat().st_size > MAX_FILE:
        raise ValueError(f'更新対象を通常のソースファイルとして読めません：{path}')
    return blob_hash(path.read_bytes())


def source_matches(path, digest):
    actual = file_hash(path)
    if actual == digest:
        return True
    # Git may check text out as CRLF; .cmd and the icon retain their exact bytes.
    return bool(actual and path.suffix not in ('.cmd', '.ico')
                and blob_hash(path.read_bytes().replace(b'\r\n', b'\n')) == digest)


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with temp.open('w', encoding='utf-8', newline='\n') as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def atomic_copy(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(target.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        with source.open('rb') as src, temp.open('wb') as dst:
            shutil.copyfileobj(src, dst)
            dst.flush()
            os.fsync(dst.fileno())
        os.replace(temp, target)
    finally:
        temp.unlink(missing_ok=True)


def get_url(url, limit):
    request = urllib.request.Request(url, headers={
        'User-Agent': 'Whisper-Local-Updater', 'Accept': 'application/vnd.github+json'})
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            data = response.read(limit + 1)
        if len(data) > limit:
            raise ValueError('更新データがサイズの上限を超えています。')
        return data
    except urllib.error.HTTPError as exc:
        if exc.code in (403, 429):
            raise OSError('GitHubのアクセス制限です。しばらくしてから「再確認」を押してください。') from exc
        raise OSError(f'GitHubから更新を取得できません（HTTP {exc.code}）。') from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise OSError('GitHubに接続できません。インターネット接続を確認して再試行してください。') from exc


def valid_sha(value):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{40}', value) is not None


def validate_files(files):
    if not isinstance(files, dict) or not 1 <= len(files) <= 5000:
        raise ValueError('更新ファイルの一覧が不正です。')
    seen = set()
    for name, digest in files.items():
        source_path(name)
        if name.casefold() in seen or not valid_sha(digest):
            raise ValueError('重複したファイル名、または不正なハッシュがあります。')
        seen.add(name.casefold())
    for name in seen:
        if any(str(parent) in seen for parent in PurePosixPath(name).parents if str(parent) != '.'):
            raise ValueError('ファイルとフォルダーの名前が衝突しています。')


def check_latest(root):
    commit = json.loads(get_url(f'{API}/commits/{BRANCH}', 2 * 1024 * 1024))
    sha = commit['sha']
    if not valid_sha(sha):
        raise ValueError('GitHubの更新番号を確認できません。')
    tree = json.loads(get_url(f'{API}/git/trees/{sha}?recursive=1', 4 * 1024 * 1024))
    if tree.get('truncated'):
        raise ValueError('更新ファイルの一覧をすべて取得できませんでした。')
    files, total = {}, 0
    for entry in tree['tree']:
        if entry['type'] == 'tree':
            continue
        if entry['type'] != 'blob' or entry.get('mode') not in ('100644', '100755'):
            raise ValueError('通常のファイル以外が更新に含まれています。')
        size = entry.get('size', 0)
        if not 0 <= size <= MAX_FILE:
            raise ValueError('更新ファイルが大きすぎます。')
        total += size
        if entry['path'] in files:
            raise ValueError('更新ファイルが重複しています。')
        files[entry['path']] = entry['sha']
    if total > MAX_TOTAL:
        raise ValueError('更新全体のサイズが大きすぎます。')
    validate_files(files)
    if not REQUIRED.issubset(files):
        raise ValueError('この更新方式に必要なファイルがGitHubに揃っていません。後で再確認してください。')
    changes = sum(not source_matches(inside(root, name), digest) for name, digest in files.items())
    state = updates_dir(root) / 'installed.json'
    if state.is_file():
        previous = json.loads(state.read_text('utf-8'))
        validate_files(previous['files'])
        changes += sum(name not in files and inside(root, name).exists() for name in previous['files'])
    return {'sha': sha, 'message': commit['commit']['message'].splitlines()[0],
            'date': commit['commit']['committer']['date'], 'files': files, 'changes': changes}


def current_version(root):
    state = updates_dir(root) / 'installed.json'
    if state.is_file():
        try:
            sha = json.loads(state.read_text('utf-8'))['sha']
            if valid_sha(sha):
                return sha[:8]
        except (OSError, ValueError, KeyError):
            pass
    if (Path(root) / '.git').exists():
        try:
            result = subprocess.run(['git', '-C', str(root), 'rev-parse', 'HEAD'],
                                    capture_output=True, text=True, timeout=5, creationflags=NO_WINDOW)
            if result.returncode == 0 and valid_sha(result.stdout.strip()):
                return result.stdout.strip()[:8]
        except (OSError, subprocess.TimeoutExpired):
            pass
    return '初回確認（ファイルの内容で比較します）'


def extract_verified(archive, destination, files):
    validate_files(files)
    seen, prefixes, total = set(), set(), 0
    with zipfile.ZipFile(archive) as bundle:
        if len(bundle.infolist()) > 10000:
            raise ValueError('更新ZIPの項目数が多すぎます。')
        for info in bundle.infolist():
            name = info.filename
            parts = name.rstrip('/').split('/')
            if ('\\' in name or any(p in ('', '.', '..') or ':' in p for p in parts)
                    or (info.external_attr >> 16) & 0o170000 == 0o120000):
                raise ValueError('更新ZIPに不正なパスまたはリンクがあります。')
            prefixes.add(parts[0])
            if info.is_dir():
                continue
            relative = '/'.join(parts[1:])
            source_path(relative)
            if relative not in files or relative.casefold() in seen:
                raise ValueError('更新ZIPとGitHubのファイル一覧が一致しません。')
            total += info.file_size
            if info.file_size > MAX_FILE or total > MAX_TOTAL:
                raise ValueError('更新ZIPの展開サイズが大きすぎます。')
            data = bundle.read(info)
            if blob_hash(data) != files[relative]:
                raise ValueError(f'更新ファイルの検証に失敗しました：{relative}')
            target = inside(destination, relative)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
            seen.add(relative.casefold())
    if len(prefixes) != 1 or seen != {name.casefold() for name in files}:
        raise ValueError('更新ZIPのファイルが不足しています。')


def prepare_update(root, release, progress=lambda message: None):
    root = Path(root).resolve()
    if not valid_sha(release['sha']):
        raise ValueError('更新番号が不正です。')
    validate_files(release['files'])
    if not REQUIRED.issubset(release['files']):
        raise ValueError('更新に必要なファイルが不足しています。')
    job = updates_dir(root) / uuid.uuid4().hex
    job.mkdir(parents=True)
    progress('最新版のプログラムをダウンロードしています…')
    archive = job / 'source.zip'
    archive.write_bytes(get_url(f'https://codeload.github.com/{REPOSITORY}/zip/{release["sha"]}', MAX_TOTAL))
    progress('ファイルの内容を検証しています…')
    extract_verified(archive, job / 'payload', release['files'])
    previous_path = updates_dir(root) / 'installed.json'
    previous = json.loads(previous_path.read_text('utf-8')) if previous_path.is_file() else None
    obsolete = {}
    if previous:
        validate_files(previous['files'])
        for name, digest in previous['files'].items():
            if name not in release['files']:
                actual = file_hash(inside(root, name))
                if actual is not None and actual != digest:
                    raise ValueError(f'以前のソースが手動で変更されています。先に退避してください：{name}')
                obsolete[name] = actual
    before = {name: file_hash(inside(root, name)) for name in release['files']}
    before.update(obsolete)
    plan = {'version': 1, 'id': job.name, 'sha': release['sha'], 'files': release['files'],
            'before': before, 'previous': previous}
    atomic_json(job / 'plan.json', plan)
    # Run this already-trusted helper, not the downloaded code, during replacement.
    shutil.copyfile(__file__, job / 'helper.py')
    return job


def read_plan(root, job):
    job = Path(job).resolve()
    if job.parent != updates_dir(root).resolve() or not re.fullmatch('[0-9a-f]{32}', job.name):
        raise ValueError('更新用の作業フォルダーが一致しません。')
    inside(updates_dir(root), job.name)
    plan = json.loads((job / 'plan.json').read_text('utf-8'))
    if plan.get('version') != 1 or plan.get('id') != job.name or not valid_sha(plan.get('sha')):
        raise ValueError('更新の記録が不正です。')
    validate_files(plan['files'])
    validate_files({name: digest or '0' * 40 for name, digest in plan['before'].items()})
    if not set(plan['files']).issubset(plan['before']):
        raise ValueError('更新前のファイル一覧が不足しています。')
    inside(job, 'payload')
    inside(job, 'before')
    return plan


def recover_pending(root):
    """Called under installation_lock, including before importing the app at startup."""
    folder = updates_dir(root)
    journal = folder / 'pending.json'
    if not journal.is_file():
        return False
    record = json.loads(journal.read_text('utf-8'))
    token = record.get('id', '')
    if not re.fullmatch('[0-9a-f]{32}', token):
        raise ValueError('未完了の更新記録を確認できません。')
    job = folder / token
    plan = read_plan(root, job)
    # Verify every backup before changing even one installed file.
    for name, digest in plan['before'].items():
        if digest is not None and file_hash(inside(job / 'before', name)) != digest:
            raise ValueError(f'更新前のプログラムを確認できません：{name}')
        inside(root, name)
    for name, digest in plan['before'].items():
        target = inside(root, name)
        if file_hash(target) == digest:
            continue
        if digest is None:
            target.unlink(missing_ok=True)
        else:
            atomic_copy(inside(job / 'before', name), target)
    if plan['previous'] is None:
        (folder / 'installed.json').unlink(missing_ok=True)
    else:
        atomic_json(folder / 'installed.json', plan['previous'])
    journal.unlink()
    return True


def apply_update(root, job, validate=lambda: None, progress=lambda message: None):
    """Caller owns installation_lock. Recoverable if killed at any replacement step."""
    root, job = Path(root).resolve(), Path(job).resolve()
    folder = updates_dir(root)
    if (folder / 'pending.json').exists():
        raise ValueError('前の更新が未完了です。Whisperを一度起動し直してください。')
    plan = read_plan(root, job)
    for name, digest in plan['files'].items():
        if file_hash(inside(job / 'payload', name)) != digest:
            raise ValueError(f'準備した更新ファイルが変わっています：{name}')
    for name, digest in plan['before'].items():
        if file_hash(inside(root, name)) != digest:
            raise ValueError(f'更新準備後にファイルが変更されました。再確認してください：{name}')
    progress('変更前のプログラムを保管しています…')
    for name, digest in plan['before'].items():
        if digest is not None:
            atomic_copy(inside(root, name), inside(job / 'before', name))
    atomic_json(folder / 'pending.json', {'id': job.name})
    try:
        progress('プログラムを更新しています。設定・学習データはそのまま残ります。')
        for name in plan['before']:
            if name in plan['files']:
                atomic_copy(inside(job / 'payload', name), inside(root, name))
            else:
                inside(root, name).unlink(missing_ok=True)
        validate()
        atomic_json(folder / 'installed.json', {'sha': plan['sha'], 'files': plan['files']})
        (folder / 'pending.json').unlink()
    except Exception as exc:
        progress('更新前のプログラムに戻しています…')
        try:
            recover_pending(root)
        except Exception as rollback_error:
            raise OSError(f'更新を中断しました。元のプログラムは {job / "before"} に残しています。\n'
                          f'復旧できなかった理由：{rollback_error}') from exc
        raise OSError(f'更新できなかったため、プログラムを更新前に戻しました。\n{exc}') from exc


@contextmanager
def installation_lock(root, timeout=0):
    folder = updates_dir(root)
    folder.mkdir(parents=True, exist_ok=True)
    stream = (folder / 'application.lock').open('a+b')
    if stream.seek(0, 2) == 0:
        stream.write(b'0'); stream.flush()
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                stream.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if time.monotonic() >= deadline:
                    raise OSError('このフォルダーのWhisper、または更新処理が既に起動しています。終了してから再試行してください。') from exc
                time.sleep(0.2)
        yield
    finally:
        stream.close()


def launch_helper(root, job):
    read_plan(root, job)
    python = Path(sys.executable).with_name('pythonw.exe')
    if not python.is_file():
        python = Path(sys.executable)
    return subprocess.Popen([str(python), '-X', 'utf8', str(Path(job) / 'helper.py'),
                             '--apply', str(Path(root).resolve()), str(Path(job).resolve())],
                            cwd=root, creationflags=NO_WINDOW)


def verify_installation(root, job, progress):
    python = Path(root) / '.venv' / 'Scripts' / 'python.exe'
    progress('必要なライブラリを確認しています。追加の準備には数分かかる場合があります…')
    with (job / 'setup.log').open('w', encoding='utf-8') as log:
        env = dict(os.environ, PYTHONUTF8='1', PYTHONIOENCODING='utf-8')
        for args in ([str(root / 'setup_local.py'), '--ensure'], ['-c', 'import app']):
            result = subprocess.run([str(python), '-X', 'utf8', *args], cwd=root, env=env,
                                    stdout=log, stderr=subprocess.STDOUT, timeout=3600, creationflags=NO_WINDOW)
            if result.returncode:
                raise OSError(f'起動に必要な準備に失敗しました。詳細：{job / "setup.log"}')


def helper_main(root, job):
    """Visible Tk progress with hidden subprocess consoles; survives app termination."""
    import queue
    import threading
    import tkinter as tk
    from tkinter import ttk
    root, job = Path(root).resolve(), Path(job).resolve()
    window = tk.Tk()
    window.title('Whisperを更新')
    frame = ttk.Frame(window, padding=24)
    frame.pack(fill='both', expand=True)
    ttk.Label(frame, text='Whisperを更新しています', font=('Yu Gothic UI', 15, 'bold')).pack(anchor='w')
    status = tk.StringVar(value='Whisperの終了を待っています…')
    ttk.Label(frame, textvariable=status, wraplength=520).pack(fill='x', pady=16)
    bar = ttk.Progressbar(frame, mode='indeterminate', length=520)
    bar.pack(fill='x'); bar.start(15)
    ttk.Label(frame, text='設定・話者・学習音声・辞書・録音は保存したまま引き継ぎます。').pack(anchor='w', pady=12)
    window.protocol('WM_DELETE_WINDOW', lambda: None)
    events = queue.Queue()

    def work():
        try:
            with installation_lock(root, timeout=30):
                apply_update(root, job, validate=lambda: verify_installation(root, job, lambda text: events.put(('progress', text))),
                             progress=lambda text: events.put(('progress', text)))
            events.put(('done', None))
        except Exception as exc:
            events.put(('error', str(exc)))

    def restart():
        try:
            python = root / '.venv' / 'Scripts' / 'pythonw.exe'
            subprocess.Popen([str(python), '-X', 'utf8', str(root / 'app.py')], cwd=root, creationflags=NO_WINDOW)
            window.destroy()
        except OSError as exc:
            status.set(f'自動で起動できませんでした。start.cmdから起動してください。\n{exc}')
            window.protocol('WM_DELETE_WINDOW', window.destroy)

    def poll():
        try:
            while True:
                kind, message = events.get_nowait()
                if kind == 'progress':
                    status.set(message)
                elif kind == 'done':
                    bar.stop(); restart(); return
                elif kind == 'error':
                    bar.stop()
                    status.set(message)
                    window.protocol('WM_DELETE_WINDOW', window.destroy)
                    if not (updates_dir(root) / 'pending.json').exists():
                        ttk.Button(frame, text='Whisperを起動', command=restart).pack(side='left')
                    ttk.Button(frame, text='閉じる', command=window.destroy).pack(side='right')
                    return
        except queue.Empty:
            pass
        window.after(100, poll)

    threading.Thread(target=work, daemon=True).start()
    window.after(100, poll)
    window.mainloop()


if __name__ == '__main__':
    if len(sys.argv) == 4 and sys.argv[1] == '--apply':
        helper_main(sys.argv[2], sys.argv[3])
    elif len(sys.argv) == 3 and sys.argv[1] == '--recover':
        try:
            with installation_lock(sys.argv[2]):
                if recover_pending(sys.argv[2]):
                    print('中断した更新を取り消し、更新前のプログラムに戻しました。')
        except (OSError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            raise SystemExit(1)
