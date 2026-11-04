"""Portable, verified backups of settings and explicitly learned local user data."""
from platform_support import tool_path
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import tempfile
import uuid
import zipfile

import user_data as ud
from data_storage import application_session, recover_restore, remove_stage, replace_store

FORMAT = 'whisper-user-backup'
MANIFEST = 'whisper-backup.json'
LIBRARIES = ('speaker_memory_clips', 'speaker_sources')
MAX_FILES = 100000
MAX_METADATA = 32 * 1024 * 1024


def device_id():
    return ud.storage_device()


def _json(path):
    if path.stat().st_size > MAX_METADATA:
        raise ValueError('設定・話者のファイルが大きすぎます。')
    return json.loads(path.read_text('utf-8-sig'))


def _clips(value):
    if isinstance(value, dict):
        if isinstance(value.get('clips'), dict):
            yield from value['clips'].values()
        for child in value.values():
            if isinstance(child, (dict, list)): yield from _clips(child)
    elif isinstance(value, list):
        for child in value: yield from _clips(child)


def _audio_path(folder, name):
    for sub in ('', '.undo'):
        path = folder / 'speaker_memory_clips' / sub / name
        if path.is_file(): return path
    return None


def _validate(folder, original_root, *, allow_local_sources=False):
    from corrections import normalize_settings
    from speaker_memory import MemoryStore
    from speaker_pool import read_pcm
    for name in ud.FILES:
        path = folder / name
        if not path.exists(): continue
        data = _json(path)
        if not isinstance(data, dict): raise ValueError(f'設定の形式が正しくありません：{name}')
        if name == 'corrections.json': normalize_settings(data)
        elif name == 'recording_settings.json':
            from recording_settings import settings_for
            if data.get('version') not in (1, 2, 3, 4, 5, 6): raise ValueError('録画設定のバージョンを確認してください。')
            settings_for(data.get('mode'), data.get('targets'), data.get('model', 'base'), data.get('audio_device'),
                         data.get('overlay'), data.get('processing'), data.get('display'), data.get('history'))
        elif name == 'window_settings.json':
            from window_settings import validated
            validated(data)
        elif name == 'appearance_settings.json':
            if data.get('theme') not in {'light', 'dark', 'system'}: raise ValueError('外観の設定を確認してください。')
        elif name == 'caption_settings.json':
            if data.get('direction') not in {'horizontal', 'vertical'} or data.get('vertical_position', 'right') not in {'left', 'right'}:
                raise ValueError('字幕の設定を確認してください。')
        elif name == 'compute_settings.json':
            from asr import COMPUTE_OPTIONS
            if data.get('compute') not in COMPUTE_OPTIONS.values(): raise ValueError('処理方式の設定を確認してください。')
        elif name == 'context_settings.json':
            if data.get('version') != 1 or type(data.get('auto_apply')) is not bool: raise ValueError('校正の設定を確認してください。')
        elif name == 'correction_memory.json':
            from correction_memory import _read
            _read(path)
    store = MemoryStore(folder / 'speaker_memory.json')
    profiles = store.load()
    if store.path.exists():
        data = _json(store.path)
        for clip in _clips(data):
            audio = _audio_path(folder, clip['file']) if clip.get('file') else None
            if audio:
                pcm = read_pcm(audio)
                if not pcm or hashlib.sha256(pcm).hexdigest() != clip.get('sha256'):
                    raise ValueError('学習音声の内容を確認できません。バックアップを作成し直してください。')
            else:
                source = Path(clip['source'])
                if allow_local_sources:
                    if clip.get('file'):
                        raise ValueError('復元前の学習音声が見つかりません。現在のデータは変更していません。')
                    if source.is_absolute() and source.is_relative_to(original_root):
                        source = folder / source.relative_to(original_root)
                    if not source.is_file() or source.stat().st_size == 0:
                        raise ValueError('学習待ちの発言の元音声が見つからないため、元に戻せません。')
                else:
                    if not source.is_absolute() or not source.is_relative_to(original_root):
                        raise ValueError('バックアップに必要な学習音声が含まれていません。')
                    source = folder / source.relative_to(original_root)
                    if not source.is_file() or not source.is_relative_to(folder / 'speaker_sources') or not read_pcm(source):
                        raise ValueError('バックアップに学習音声が見つかりません。')
        for history in store.review_history():
            for name in history['archived']:
                if not store.undo_audio_path(name).is_file(): raise ValueError('学習音声の取り消し用データが不足しています。')
    dictionary = normalize_settings(_json(folder / 'corrections.json')) if (folder / 'corrections.json').exists() else {'rules': []}
    return {'speakers': len(profiles), 'learning_clips': sum(len(p.get('clips', {})) for p in profiles.values()),
            'dictionary_rules': len(dictionary['rules']), 'settings': sum((folder / name).is_file() for name in ud.FILES),
            'results': len(list((folder / 'transcripts').glob('*/result.json')))}


def _portable_voices(stage, root, ffmpeg, progress):
    path = stage / 'speaker_memory.json'
    if not path.exists(): return
    from speaker_memory import MemoryStore
    from speaker_pool import read_pcm
    MemoryStore(path).load()
    data = _json(path)
    for clip in _clips(data):
        if clip.get('file') and _audio_path(stage, clip['file']): continue
        source = Path(clip['source'])
        if not source.is_file(): raise ValueError('学習音声の元ファイルが見つかりません。学習音声の画面で確認してください。')
        if source.is_relative_to(root / 'speaker_sources') and (stage / source.relative_to(root)).is_file():
            try:
                pcm = read_pcm(stage / source.relative_to(root))
                if clip['start'] == 0 and abs(clip['end'] - len(pcm) / 32000) < .001: continue
            except (ValueError, OSError):
                pass
        identity = json.dumps([str(source.resolve()), clip['start'], clip['end']], ensure_ascii=False)
        name = hashlib.sha256(identity.encode('utf-8')).hexdigest() + '.wav'
        target = stage / 'speaker_sources' / name
        if not target.exists():
            progress('短い発言の学習音声をバックアップにまとめています…')
            target.parent.mkdir(parents=True, exist_ok=True)
            result = subprocess.run([str(ffmpeg), '-nostdin', '-v', 'error', '-ss', str(clip['start']),
                '-t', str(clip['end'] - clip['start']), '-i', str(source), '-vn', '-ac', '1', '-ar', '16000',
                '-c:a', 'pcm_s16le', '-n', str(target)], capture_output=True, timeout=90,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            if result.returncode or not target.is_file(): raise ValueError('学習音声を読み込めません。元の録音を確認してください。')
        pcm = read_pcm(target)
        if not pcm: raise ValueError('学習音声が空です。学習音声の画面で確認してください。')
        clip.update(source=str(root / 'speaker_sources' / name), start=0.0, end=len(pcm) / 32000)
        clip.pop('file', None); clip.pop('sha256', None)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), 'utf-8')


def create_backup(destination, *, include_results=False, root=None, ffmpeg=None, progress=lambda text: None):
    root = Path(root or ud.data_root()).resolve()
    destination = Path(destination).resolve()
    if destination.is_relative_to(root): raise ValueError('バックアップは保存データフォルダーの外に保存してください。')
    if ffmpeg is None:
        from engine import TOOLS
        ffmpeg = tool_path('ffmpeg')
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(destination.name + '.' + uuid.uuid4().hex + '.partial')
    with application_session(root), ud._locked(root):
        recover_restore(root)
        with tempfile.TemporaryDirectory(prefix='whisper-backup-') as temporary:
            stage = Path(temporary)
            try:
                selected = [p for p in ud._files(root) if include_results or not p.is_relative_to(root / 'transcripts')]
                before = {}
                for index, source in enumerate(selected, 1):
                    if source.is_symlink() or source.is_junction(): raise ValueError('リンク先のデータはバックアップできません。')
                    relative = source.relative_to(root)
                    progress(f'保存データをまとめています… {index}/{len(selected)}')
                    before[relative.as_posix()] = ud._copy(source, stage / relative)
                _portable_voices(stage, root, ffmpeg, progress)
                summary = _validate(stage, root)
                current = [p for p in ud._files(root) if include_results or not p.is_relative_to(root / 'transcripts')]
                if {p.relative_to(root).as_posix(): ud._digest(p) for p in current} != before:
                    raise OSError('バックアップ中に保存データが変わりました。処理が終わってから再実行してください。')
                files = {p.relative_to(stage).as_posix(): {'bytes': p.stat().st_size, 'sha256': ud._digest(p)}
                         for p in stage.rglob('*') if p.is_file()}
                manifest = {'format': FORMAT, 'version': 1, 'created': datetime.now(timezone.utc).isoformat(),
                            'source_root': str(root), 'device': device_id(), 'include_results': bool(include_results),
                            'files': files, 'summary': summary}
                progress('バックアップファイルを保存しています…')
                with zipfile.ZipFile(partial, 'x', compression=zipfile.ZIP_DEFLATED, compresslevel=1, allowZip64=True) as archive:
                    archive.writestr(MANIFEST, json.dumps(manifest, ensure_ascii=False, indent=2))
                    for name in files: archive.write(stage / name, 'data/' + name)
                progress('バックアップの内容を検証しています…')
                verified = inspect_backup(partial, progress=progress)
                partial.replace(destination)
                return {**verified, 'path': str(destination)}
            finally:
                partial.unlink(missing_ok=True)


def _safe_name(name):
    if not isinstance(name, str) or '\\' in name or ':' in name or '\x00' in name:
        raise ValueError('バックアップ内のファイル名が正しくありません。')
    parts = name.split('/')
    for part in parts:
        if (not part or part in {'.', '..'} or part.endswith((' ', '.'))
                or any(ord(c) < 32 for c in part) or any(c in part for c in '<>"|?*')
                or re.fullmatch(r'(?i)(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(?:\..*)?', part)):
            raise ValueError('バックアップ内のファイル名が正しくありません。')
    return PurePosixPath(name)


def _extract(archive_path, stage, progress):
    with zipfile.ZipFile(archive_path) as archive:
        infos = archive.infolist()
        if len(infos) > MAX_FILES + 1: raise ValueError('バックアップのファイル数が多すぎます。')
        names = [info.filename for info in infos]
        if len(set(n.casefold() for n in names)) != len(names): raise ValueError('バックアップのファイル名が重複しています。')
        if MANIFEST not in names or archive.getinfo(MANIFEST).file_size > MAX_METADATA:
            raise ValueError('Whisperのバックアップファイルを選んでください。')
        for info in infos:
            _safe_name(info.filename)
            if (info.is_dir() or stat.S_ISLNK(info.external_attr >> 16) or info.flag_bits & 1
                    or info.compress_type not in (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)):
                raise ValueError('バックアップの形式が対応していません。')
        manifest = json.loads(archive.read(MANIFEST).decode('utf-8'))
        if (not isinstance(manifest, dict) or manifest.get('format') != FORMAT or manifest.get('version') != 1
                or type(manifest.get('include_results')) is not bool or not isinstance(manifest.get('files'), dict)
                or not isinstance(manifest.get('created'), str) or not isinstance(manifest.get('device'), str)
                or not isinstance(manifest.get('source_root'), str) or not Path(manifest['source_root']).is_absolute()):
            raise ValueError('バックアップの形式・バージョンを確認してください。')
        files = manifest['files']
        if set(names) != {MANIFEST} | {'data/' + n for n in files}: raise ValueError('バックアップの一覧と内容が一致しません。')
        total = 0
        for name, entry in files.items():
            relative = _safe_name(name)
            allowed = len(relative.parts) == 1 and name in ud.FILES
            allowed |= len(relative.parts) > 1 and relative.parts[0] in LIBRARIES
            allowed |= manifest['include_results'] and len(relative.parts) > 1 and relative.parts[0] == 'transcripts'
            if (not allowed or not isinstance(entry, dict) or type(entry.get('bytes')) is not int or entry['bytes'] < 0
                    or not isinstance(entry.get('sha256'), str) or not re.fullmatch('[0-9a-f]{64}', entry['sha256'])
                    or archive.getinfo('data/' + name).file_size != entry['bytes']):
                raise ValueError('バックアップ内の保存データを確認できません。')
            total += entry['bytes']
        if total > shutil.disk_usage(stage).free - 64 * 1024 * 1024:
            raise OSError('バックアップの展開に必要な空き容量がありません。')
        for index, (name, entry) in enumerate(files.items(), 1):
            progress(f'バックアップを確認しています… {index}/{len(files)}')
            target = stage / name
            target.parent.mkdir(parents=True, exist_ok=True)
            h, count = hashlib.sha256(), 0
            with archive.open('data/' + name) as source, target.open('xb') as dest:
                while block := source.read(1024 * 1024):
                    count += len(block)
                    if count > entry['bytes']: raise ValueError('バックアップのサイズが一致しません。')
                    h.update(block); dest.write(block)
            if count != entry['bytes'] or h.hexdigest() != entry['sha256']:
                raise ValueError('バックアップが破損しています。元のPCで作成し直してください。')
    summary = _validate(stage, Path(manifest['source_root']))
    return {**manifest, 'summary': summary}


def inspect_backup(path, *, progress=lambda text: None):
    path = Path(path)
    fingerprint = ud._digest(path)
    with tempfile.TemporaryDirectory(prefix='whisper-backup-check-') as temp:
        manifest = _extract(path, Path(temp), progress)
    if ud._digest(path) != fingerprint: raise OSError('確認中にバックアップが変わりました。選び直してください。')
    return {'created': manifest['created'], 'include_results': manifest['include_results'],
            'summary': manifest['summary'], 'fingerprint': fingerprint,
            'different_pc': manifest['device'] != device_id()}


def restore_backup(path, *, expected_fingerprint, root=None, progress=lambda text: None):
    path, root = Path(path).resolve(), Path(root or ud.data_root()).resolve()
    if path.is_relative_to(root): raise ValueError('バックアップを保存データフォルダーの外へコピーしてから復元してください。')
    with application_session(root), ud._locked(root):
        recover_restore(root)
        if ud._digest(path) != expected_fingerprint: raise ValueError('選択後にバックアップが変更されました。選び直してください。')
        token = uuid.uuid4().hex
        stage = root.parent / ('.' + root.name + '.restore-stage-' + token)
        stage.mkdir()
        try:
            manifest = _extract(path, stage, progress)
            if ud._digest(path) != expected_fingerprint: raise OSError('確認中にバックアップが変わりました。選び直してください。')
            for name in manifest['files']:
                file = stage / name
                if file.suffix.lower() == '.json' and (name == 'speaker_memory.json' or name.startswith('transcripts/')):
                    data = _json(file)
                    relocated = ud._relocate(data, Path(manifest['source_root']), stage, root)
                    if data != relocated: file.write_text(json.dumps(relocated, ensure_ascii=False, indent=2), 'utf-8')
            if manifest['device'] != device_id() and (stage / 'recording_settings.json').exists():
                data = _json(stage / 'recording_settings.json')
                data['targets'] = {}  # Handles and process identities belong to the original PC.
                (stage / 'recording_settings.json').write_text(json.dumps(data, ensure_ascii=False, indent=2), 'utf-8')
            progress('復元前のデータを保護しています…')
            # Unselected recording history and unrelated files stay intact; the original directory becomes the rollback copy.
            replace = set(ud.FILES) | set(LIBRARIES) | {ud.MARKER}
            if manifest['include_results']: replace.add('transcripts')
            if root.exists():
                for file in root.iterdir():
                    if file.name in replace: continue
                    if file.is_symlink() or file.is_junction(): raise ValueError('保存先にリンクがあるため復元できません。')
                    if file.is_dir(): shutil.copytree(file, stage / file.name)
                    else: shutil.copy2(file, stage / file.name)
            from restore_undo import snapshot_inventory
            before_restore = snapshot_inventory(root, progress) if root.exists() else {}
            marker = {'version': 1, 'complete': True, 'restore_id': token, 'restore_action': 'restore',
                      'undo_inventory': before_restore, 'restored_at': datetime.now(timezone.utc).isoformat(),
                      **ud.storage_identity(root)}
            (stage / ud.MARKER).write_text(json.dumps(marker), 'utf-8')
            progress('データを復元しています…')
            backup = replace_store(stage, root, token)
            ud._ready.discard(root)
            return {'previous_data': str(backup) if backup else None, 'summary': manifest['summary'],
                    'different_pc': manifest['device'] != device_id()}
        finally:
            remove_stage(stage, root)
