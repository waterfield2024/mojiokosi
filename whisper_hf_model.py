"""Verified, pinned Whisper data for the existing Transformers runtime."""
import hashlib
import json
import os
from pathlib import Path
import time
import urllib.request
import uuid

ROOT=Path(__file__).resolve().parent


def ensure_whisper_hf(name,models,progress=lambda text:None,stopped=lambda:False):
    manifest=json.loads((ROOT/'whisper_hf_manifest.json').read_text('utf-8'))
    if name not in manifest:raise ValueError('Whisperモデルが正しくありません。')
    info=manifest[name];folder=Path(models)/('whisper-hf-'+name);folder.mkdir(parents=True,exist_ok=True)
    for filename,entry in info['files'].items():
        if stopped():raise InterruptedError('Whisperの準備を中止しました。')
        target=folder/filename
        if target.is_file() and target.stat().st_size==entry['size']:continue
        temporary=folder/(filename+'.download-'+uuid.uuid4().hex)
        progress(f'互換Whisperモデルを準備中… {name} / {filename}')
        try:
            request=urllib.request.Request(f'https://huggingface.co/{info["repo"]}/resolve/{info["revision"]}/{filename}',headers={'User-Agent':'WhisperDesktop/2.35.1'})
            checksum=hashlib.sha256();received=0;previous=0.
            with urllib.request.urlopen(request,timeout=30) as response,temporary.open('wb') as output:
                while block:=response.read(1024*1024):
                    if stopped():raise InterruptedError('Whisperの準備を中止しました。')
                    output.write(block);checksum.update(block);received+=len(block)
                    now=time.monotonic()
                    if now-previous>.8:
                        progress(f'互換Whisperモデルを準備中… {name} / {min(100,received*100//entry["size"])}%');previous=now
            if received!=entry['size'] or checksum.hexdigest()!=entry['sha256']:raise RuntimeError('Whisperモデルを検証できませんでした。準備を再試行してください。')
            os.replace(temporary,target)
        finally:temporary.unlink(missing_ok=True)
    return folder
