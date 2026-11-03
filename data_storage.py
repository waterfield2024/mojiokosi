"""Exclusive app sessions and recoverable replacement of the local data directory."""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import shutil
import threading
import uuid

_sessions = {}
_mutex = threading.RLock()


@contextmanager
def application_session(root):
    root = Path(root).resolve()
    with _mutex:
        entry = _sessions.get(root)
        if entry:
            entry[1] += 1
        else:
            root.parent.mkdir(parents=True, exist_ok=True)
            stream = (root.parent / (root.name + '.application.lock')).open('a+b')
            if stream.seek(0, 2) == 0:
                stream.write(b'0'); stream.flush()
            try:
                stream.seek(0)
                if os.name == 'nt':
                    import msvcrt
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                stream.close()
                raise OSError('同じ保存データを使うWhisperが開いています。先にそちらを終了してください。') from exc
            _sessions[root] = [stream, 1]
    try:
        yield
    finally:
        with _mutex:
            entry = _sessions[root]
            entry[1] -= 1
            if entry[1] == 0:
                entry[0].close()
                del _sessions[root]


def remove_stage(stage, root):
    stage, root = Path(stage).resolve(), Path(root).resolve()
    if stage.parent != root.parent or not re.fullmatch(re.escape('.' + root.name + '.restore-stage-') + '[0-9a-f]{32}', stage.name):
        raise ValueError('復元用の一時フォルダーを確認できません。')
    if stage.exists(): shutil.rmtree(stage)


def _journal(root):
    return root.parent / (root.name + '.restore-pending.json')


def recover_restore(root):
    """Called with the migration lock before any settings are loaded."""
    root = Path(root).resolve()
    journal = _journal(root)
    if not journal.exists(): return
    record = json.loads(journal.read_text('utf-8'))
    token = record.get('id', '')
    if (record.get('version') != 1 or not re.fullmatch('[0-9a-f]{32}', token)
            or record.get('stage') != '.' + root.name + '.restore-stage-' + token
            or record.get('backup') != root.name + '.before-restore-' + token):
        raise ValueError('未完了の復元記録を確認できません。保存データは変更していません。')
    stage, backup = root.parent / record['stage'], root.parent / record['backup']
    if not root.exists():
        if backup.is_dir():
            backup.rename(root)
        elif stage.is_dir() and not record.get('had_original'):
            stage.rename(root)
        else:
            raise OSError('復元前のデータが見つかりません。復元記録とバックアップを確認してください。')
    elif backup.exists():
        marker = root / '.user-data-v1.json'
        data = json.loads(marker.read_text('utf-8')) if marker.is_file() else {}
        if data.get('restore_id') != token:
            raise OSError('未完了の復元と保存データが競合しています。復元前のバックアップを残しています。')
    remove_stage(stage, root)
    journal.unlink()


def replace_store(stage, root, token):
    """The caller has completely verified the stage and holds the app/migration locks."""
    root = Path(root).resolve()
    stage = Path(stage).resolve()
    if stage != root.parent / ('.' + root.name + '.restore-stage-' + token):
        raise ValueError('復元用フォルダーが一致しません。')
    backup = root.parent / (root.name + '.before-restore-' + token)
    journal = _journal(root)
    temporary = journal.with_suffix('.tmp')
    record = {'version': 1, 'id': token, 'stage': stage.name, 'backup': backup.name, 'had_original': root.exists()}
    with temporary.open('w', encoding='utf-8') as stream:
        json.dump(record, stream); stream.flush(); os.fsync(stream.fileno())
    temporary.replace(journal)
    try:
        if root.exists(): root.rename(backup)
        stage.rename(root)
    except OSError:
        recover_restore(root)
        raise
    try:
        journal.unlink()
    except OSError:
        pass  # The next startup recognizes the committed restore_id and cleans up.
    return backup if backup.exists() else None
