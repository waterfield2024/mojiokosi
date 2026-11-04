"""Undo the latest ZIP restore using its exact, locally retained pre-restore store."""
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import uuid

import user_data as ud
from data_storage import application_session, recover_restore, remove_stage, replace_store


def snapshot_inventory(folder, progress=lambda text: None):
    folder = Path(folder).resolve()
    files = []
    def unreadable(error):
        raise error  # An incomplete directory listing must never become an undo snapshot.
    for current, directories, names in os.walk(folder, followlinks=False, onerror=unreadable):
        for name in [*directories, *names]:
            path = Path(current) / name
            if path.is_symlink() or path.is_junction():
                raise ValueError('復元前のデータにリンクがあるため、元に戻せません。')
        files.extend(Path(current) / name for name in names)
    inventory = {}
    for index, path in enumerate(sorted(files), 1):
        progress(f'復元前のデータを確認しています… {index}/{len(files)}')
        before = path.stat()
        digest = ud._digest(path)
        after = path.stat()
        if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
            raise OSError('確認中にデータが変更されました。処理が終わってから再実行してください。')
        inventory[path.relative_to(folder).as_posix()] = {'bytes': after.st_size, 'sha256': digest}
    return inventory


def undo_info(root=None):
    """Only the marker for the active store selects a snapshot; never guess by dates."""
    root = Path(root or ud.data_root()).resolve()
    unavailable = {'available': False, 'reason': 'まだ取り消せる復元はありません。'}
    path = root / ud.MARKER
    if not path.is_file(): return unavailable
    marker = json.loads(path.read_text('utf-8'))
    if not isinstance(marker, dict) or marker.get('version') != 1 or marker.get('complete') is not True:
        raise ValueError('復元の記録を読み込めません。')
    if marker.get('restore_action') == 'undo':
        return {'available': False, 'reason': '直前の復元は取り消し済みです。'}
    token = marker.get('restore_id')
    if token is None: return unavailable
    if not isinstance(token, str) or not re.fullmatch('[0-9a-f]{32}', token):
        raise ValueError('復元前の保存先を確認できません。')
    snapshot = root.parent / (root.name + '.before-restore-' + token)
    if snapshot.is_symlink() or snapshot.is_junction() or not snapshot.is_dir() or snapshot.resolve().parent != root.parent:
        return {'available': False, 'reason': '復元前のデータが見つからないため、元に戻せません。'}
    return {'available': True, 'id': token, 'restored_at': marker.get('restored_at', ''), 'snapshot': str(snapshot)}


def undo_restore(*, expected_id, root=None, progress=lambda text: None):
    root = Path(root or ud.data_root()).resolve()
    with application_session(root), ud._locked(root):
        recover_restore(root)
        info = undo_info(root)
        if not info['available']: raise ValueError(info['reason'])
        if info['id'] != expected_id:
            raise ValueError('別の復元が行われています。画面を開き直して確認してください。')
        snapshot = Path(info['snapshot'])
        marker = json.loads((root / ud.MARKER).read_text('utf-8'))
        inventory = snapshot_inventory(snapshot, progress)
        # 2.48 snapshots have no inventory; they are still copied and schema/audio checked.
        if 'undo_inventory' in marker and marker['undo_inventory'] != inventory:
            raise ValueError('復元前のデータが変更されているか、一部が失われています。現在のデータは変更していません。')
        if sum(item['bytes'] for item in inventory.values()) > shutil.disk_usage(root.parent).free - 64 * 1024 * 1024:
            raise OSError('元に戻すための空き容量が足りません。現在のデータは変更していません。')
        token = uuid.uuid4().hex
        stage = root.parent / ('.' + root.name + '.restore-stage-' + token)
        stage.mkdir()
        try:
            for index, (relative, expected) in enumerate(inventory.items(), 1):
                progress(f'復元前の状態を準備しています… {index}/{len(inventory)}')
                if ud._copy(snapshot / relative, stage / relative) != expected['sha256']:
                    raise OSError('復元前のデータが途中で変更されました。現在のデータは変更していません。')
            if snapshot_inventory(snapshot, progress) != inventory:
                raise OSError('復元前のデータが途中で変更されました。現在のデータは変更していません。')
            from data_backup import _validate
            old_marker = json.loads((stage / ud.MARKER).read_text('utf-8')) if (stage / ud.MARKER).is_file() else {}
            previous = Path(old_marker.get('storage_root', str(root)))
            if previous != root: ud.relocate_store(stage, previous, root)
            if old_marker.get('storage_device') not in (None, ud.storage_device()) and (stage / 'recording_settings.json').is_file():
                recording = json.loads((stage / 'recording_settings.json').read_text('utf-8'))
                recording.update(targets={}, audio_device=None)
                ud._write_json(stage / 'recording_settings.json', recording)
            _validate(stage, root, allow_local_sources=True)
            restored = {'version': 1, 'complete': True, 'restore_id': token, 'restore_action': 'undo',
                        'undo_of': expected_id, 'undone_at': datetime.now(timezone.utc).isoformat(),
                        **ud.storage_identity(root)}
            (stage / ud.MARKER).write_text(json.dumps(restored), 'utf-8')
            progress('復元前の状態に戻しています…')
            current_backup = replace_store(stage, root, token)
            ud._ready.discard(root)
            return {'previous_data': str(current_backup) if current_backup else None, 'undo_of': expected_id}
        finally:
            remove_stage(stage, root)
