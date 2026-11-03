"""Pinned community ONNX conversion of Haas; weights only, never remote code."""
import hashlib
import json
import os
from pathlib import Path
import time
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parent
FOLDER = 'haas-ja-v1.0-onnx'


def verified(path, info, stopped):
    if not path.is_file() or path.stat().st_size != info['size']:
        return False
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        while block := stream.read(1024 * 1024):
            if stopped():
                raise InterruptedError('Haasの準備を中止しました。')
            digest.update(block)
    return digest.hexdigest() == info['sha256']


def ensure_haas(models, progress=lambda text: None, stopped=lambda: False):
    manifest = json.loads((ROOT / 'haas_model_manifest.json').read_text('utf-8'))
    folder = Path(models) / FOLDER
    folder.mkdir(parents=True, exist_ok=True)
    progress('Haasモデルを確認中…（Windows用ONNX変換版・CPU）')
    for name, info in manifest['files'].items():
        if stopped():
            raise InterruptedError('Haasの準備を中止しました。')
        target = folder / name
        if verified(target, info, stopped):
            continue
        temporary = folder / (name + '.download-' + uuid.uuid4().hex)
        progress(f"Haasモデルを取得中… {name} / 約{info['size'] / 1000000:.1f}MB")
        url = f"https://huggingface.co/{manifest['repo']}/resolve/{manifest['revision']}/{name}"
        request = urllib.request.Request(url, headers={'User-Agent': 'WhisperLocal-Haas/1.0'})
        try:
            digest, received, previous = hashlib.sha256(), 0, 0
            with urllib.request.urlopen(request, timeout=30) as response, temporary.open('wb') as output:
                while block := response.read(1024 * 1024):
                    if stopped():
                        raise InterruptedError('Haasの準備を中止しました。')
                    output.write(block)
                    digest.update(block)
                    received += len(block)
                    if time.monotonic() - previous > .8:
                        progress(f"Haasモデルを取得中… {name} / {min(100, received * 100 // info['size'])}%")
                        previous = time.monotonic()
            if received != info['size'] or digest.hexdigest() != info['sha256']:
                raise RuntimeError('Haasモデルの検証に失敗しました。もう一度実行してください。')
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
    return folder
