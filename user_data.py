"""Portable user data, with recoverable import and relocation of media paths."""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import threading
import time
import uuid

FILES = ('appearance_settings.json', 'caption_settings.json', 'compute_settings.json',
         'context_settings.json', 'recording_settings.json', 'window_settings.json',
         'corrections.json', 'correction_memory.json', 'speaker_memory.json')
DIRECTORIES = ('speaker_memory_clips', 'speaker_sources', 'transcripts')
MARKER = '.user-data-v1.json'
_ready = set()
_thread_lock = threading.RLock()
APP_ROOT = Path(__file__).resolve().parent


def data_root():
    custom = os.environ.get('WHISPER_USER_DATA_DIR')
    if custom:
        return Path(custom).expanduser().resolve()
    return (APP_ROOT / 'UserData').resolve()


def shared_data_root():
    local = Path(os.environ.get('LOCALAPPDATA') or Path.home() / 'AppData' / 'Local')
    return (local / 'WhisperYouTube' / 'UserData').resolve()


def storage_device():
    value = os.environ.get('COMPUTERNAME', '') + '|' + str(Path.home())
    return hashlib.sha256(value.casefold().encode('utf-8')).hexdigest()


def storage_identity(root):
    return {'storage_root': str(Path(root).resolve()), 'storage_device': storage_device()}


def data_path(name, override=None):
    value = os.environ.get(override) if override else None
    return Path(value) if value else data_root() / name


def _digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        while block := stream.read(1024 * 1024):
            h.update(block)
    return h.hexdigest()


def _copy(source, target):
    stamp = source.stat()
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    digest = _digest(source)
    current = source.stat()
    if (stamp.st_mtime_ns, stamp.st_size) != (current.st_mtime_ns, current.st_size) or _digest(target) != digest:
        raise OSError(f'引き継ぎ中にデータが変更されました。アプリを終了して再実行してください。\n{source}')
    return digest


def _files(folder):
    for name in FILES:
        path = folder / name
        if path.is_file():
            yield path
    for name in DIRECTORIES:
        base = folder / name
        if base.is_symlink() or base.is_junction():
            raise OSError(f'引き継ぎ元の保存先を確認してください。\n{base}')
        if base.is_dir():
            for current, directories, files in os.walk(base, followlinks=False):
                for entry in [*directories, *files]:
                    path = Path(current) / entry
                    if path.is_symlink() or path.is_junction():
                        raise OSError(f'引き継ぎ元の保存先を確認してください。\n{path}')
                for name in files:
                    yield Path(current) / name


def _read_marker(root):
    path = root / MARKER
    if not path.exists():
        return False
    data = json.loads(path.read_text('utf-8'))
    if not isinstance(data, dict) or data.get('version') != 1 or data.get('complete') is not True:
        raise ValueError(f'保存データの引き継ぎ記録を確認できません。データは変更していません。\n{path}')
    return True


@contextmanager
def _locked(root):
    root.parent.mkdir(parents=True, exist_ok=True)
    with (root.parent / (root.name + '.migration.lock')).open('a+b') as stream:
        if stream.seek(0, 2) == 0:
            stream.write(b'0'); stream.flush()
        if os.name == 'nt':
            import msvcrt
            deadline = time.monotonic() + 120
            while True:
                try:
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise OSError('別のWhisperがデータを引き継いでいます。完了後に起動し直してください。')
                    time.sleep(.1)
        else:
            import fcntl
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == 'nt':
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def _relocate(value, legacy, stage, root, key='', *, require_exists=True):
    if isinstance(value, dict):
        return {k: _relocate(v, legacy, stage, root, k, require_exists=require_exists) for k, v in value.items()}
    if isinstance(value, list):
        return [_relocate(v, legacy, stage, root, key, require_exists=require_exists) for v in value]
    if key not in {'source', 'audio_file', 'video_file', 'video_source', 'before_context', 'origin', 'folder', 'result_path'} or not isinstance(value, str):
        return value
    path = Path(value)
    if path.is_absolute() and path.is_relative_to(legacy):
        relative = path.relative_to(legacy)
        if '..' not in relative.parts and (not require_exists or (stage / relative).exists()):
            return str(root / relative)
    return value


def _write_json(path, data):
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    try:
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), 'utf-8')
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def relocate_store(folder, previous, destination):
    """Change metadata only. Audio and undo snapshots retain their exact bytes."""
    changes = []
    for file in _files(folder):
        relative = file.relative_to(folder)
        if file.suffix.lower() != '.json' or not (relative.as_posix() == 'speaker_memory.json' or relative.parts[0] == 'transcripts'):
            continue
        original = json.loads(file.read_text('utf-8-sig'))
        updated = _relocate(original, previous, folder, destination, require_exists=False)
        if updated != original: changes.append((file, updated))
    # Parse everything before changing files. If interrupted during replacement,
    # the old marker remains and the same remapping safely resumes next startup.
    for file, updated in changes: _write_json(file, updated)


def _prepare_location(root):
    marker = json.loads((root / MARKER).read_text('utf-8'))
    identity = storage_identity(root)
    if all(marker.get(k) == v for k, v in identity.items()): return
    previous = marker.get('storage_root')
    if previous is not None:
        if not isinstance(previous, str) or not Path(previous).is_absolute():
            raise ValueError('保存データの以前の場所を確認できません。')
        if Path(previous) != root: relocate_store(root, Path(previous), root)
    if marker.get('storage_device') not in (None, identity['storage_device']):
        recording = root / 'recording_settings.json'
        if recording.is_file():
            settings = json.loads(recording.read_text('utf-8-sig'))
            # HWND/PID and device names belong to the original computer. Keep
            # model/caption preferences, but do not select somebody else's window.
            settings.update(targets={}, audio_device=None)
            _write_json(recording, settings)
    marker.update(identity)
    _write_json(root / MARKER, marker)


def _retain_pending_audio(data, legacy, stage, root):
    """Pending short examples may still refer to a cache beside the old application."""
    if isinstance(data, list):
        for item in data:
            _retain_pending_audio(item, legacy, stage, root)
    elif isinstance(data, dict):
        clips = data.get('clips', {})
        if isinstance(clips, dict):
            for clip in clips.values():
                if not isinstance(clip, dict) or not isinstance(clip.get('source'), str):
                    continue
                cached = clip.get('file')
                if cached and any((stage / 'speaker_memory_clips' / sub / cached).is_file() for sub in ('', '.undo')):
                    continue
                source = Path(clip['source'])
                if source.is_absolute() and source.is_file():
                    relative = source.relative_to(legacy) if source.is_relative_to(legacy) else None
                    if relative is None or not (stage / relative).is_file():
                        name = hashlib.sha256(str(source).encode('utf-8')).hexdigest() + source.suffix
                        relative = Path('speaker_sources') / name
                        if not (stage / relative).exists():
                            _copy(source, stage / relative)
                    clip['source'] = str(root / relative)
        for item in data.values():
            if isinstance(item, (dict, list)):
                _retain_pending_audio(item, legacy, stage, root)


def initialize(legacy=None):
    """Import once; the folder's own data always wins over another PC's data."""
    root = data_root()
    with _thread_lock:
        if root in _ready and not (root.parent / (root.name + '.restore-pending.json')).exists():
            return root
        with _locked(root):
            from data_storage import recover_restore
            recover_restore(root)
            if _read_marker(root):
                _prepare_location(root)
                _ready.add(root)
                return root
            default_legacy = shared_data_root() if shared_data_root().is_dir() else APP_ROOT
            legacy = Path(legacy or os.environ.get('WHISPER_LEGACY_DATA_DIR') or default_legacy).resolve()
            local_legacy = root == (APP_ROOT / 'UserData').resolve() and legacy == APP_ROOT.resolve()
            if root == legacy or (root.is_relative_to(legacy) and not local_legacy) or legacy.is_relative_to(root):
                raise ValueError('アプリ本体とは別のフォルダーを保存先に指定してください。')
            stage = root.parent / ('.' + root.name + '.migrate-' + uuid.uuid4().hex)
            backup = None
            stage.mkdir()
            try:
                # Preserve an explicitly restored store, even before its first startup.
                if root.exists():
                    shutil.copytree(root, stage, dirs_exist_ok=True)
                imported = {}
                for source in _files(legacy):
                    relative = source.relative_to(legacy)
                    target = stage / relative
                    if not target.exists():
                        imported[relative.as_posix()] = _copy(source, target)
                for relative in imported:
                    path = stage / relative
                    if path.suffix.lower() != '.json':
                        continue
                    data = json.loads(path.read_text('utf-8-sig'))
                    if relative in FILES and not isinstance(data, dict):
                        raise ValueError(f'保存ファイルの形式を確認してください。\n{legacy / relative}')
                    if relative == 'speaker_memory.json':
                        _retain_pending_audio(data, legacy, stage, root)
                    # A correction rule's "source" is text, not a media reference.
                    relocated = _relocate(data, legacy, stage, root) if relative == 'speaker_memory.json' or relative.startswith('transcripts/') else data
                    if relocated != json.loads(path.read_text('utf-8-sig')):
                        path.write_text(json.dumps(relocated, ensure_ascii=False, indent=2), 'utf-8')
                record = {'version': 1, 'complete': True, 'legacy': str(legacy),
                          'created': datetime.now(timezone.utc).isoformat(), 'imported_sha256': imported,
                          **storage_identity(root)}
                (stage / MARKER).write_text(json.dumps(record, ensure_ascii=False, indent=2), 'utf-8')
                if root.exists():
                    backup = root.with_name(root.name + '.before-migration-' + uuid.uuid4().hex)
                    root.rename(backup)
                try:
                    stage.rename(root)
                except OSError:
                    if backup is not None:
                        backup.rename(root)
                    raise
            finally:
                # Only this call's generated staging directory may be removed.
                if stage.exists() and stage.resolve().parent == root.parent.resolve() and stage.name.startswith('.' + root.name + '.migrate-'):
                    shutil.rmtree(stage)
            _ready.add(root)
            return root
