"""Pinned NVIDIA weights only; inference code ships with this repository."""
import hashlib
import os
from pathlib import Path
import time
import urllib.request
import uuid

REPO = 'nvidia/NVIDIA-NemotronLabs-RE-USE'
REVISION = '31f82f2efad1095034492f236e8dac9239ba713b'
SHA256 = '06badaf4029b297f97f7fe48d62990b48d5482608210df577354e0d4e0ed70e1'
SIZE = 38592940


def verified(path, stopped=lambda: False):
    if not path.is_file() or path.stat().st_size != SIZE:
        return False
    digest = hashlib.sha256()
    with path.open('rb') as source:
        while block := source.read(1024*1024):
            if stopped():
                raise InterruptedError('ノイズ除去の準備を中止しました。')
            digest.update(block)
    return digest.hexdigest() == SHA256


def ensure_reuse(models, status=lambda text: None, stopped=lambda: False):
    folder = Path(models) / 'nemotronlabs-re-use'
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / 'model.safetensors'
    if verified(target, stopped):
        return target
    status('Nemotronノイズ除去モデルを取得中… 約39MB（初回のみ）')
    temporary = target.with_name(target.name + '.download-' + uuid.uuid4().hex)
    url = f'https://huggingface.co/{REPO}/resolve/{REVISION}/model.safetensors'
    try:
        previous = 0
        with urllib.request.urlopen(url, timeout=30) as source, temporary.open('wb') as output:
            while block := source.read(1024*1024):
                if stopped():
                    raise InterruptedError('ノイズ除去の準備を中止しました。')
                output.write(block)
                if time.monotonic()-previous > .8:
                    status(f'Nemotronノイズ除去モデルを取得中… {min(100, output.tell()*100//SIZE)}%')
                    previous = time.monotonic()
        if not verified(temporary, stopped):
            raise RuntimeError('Nemotronモデルの検証に失敗しました。準備を再試行してください。')
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    return target
