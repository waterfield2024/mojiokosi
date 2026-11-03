"""Pinned multilingual Granite assets, installed locally with verified downloads."""
import hashlib
import json
import os
from pathlib import Path
import time
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parent
FOLDER = "granite-speech-5.0-turboctc"


def ensure_granite(models, progress=lambda text: None, stopped=lambda: False):
    manifest = json.loads((ROOT / "granite_model_manifest.json").read_text("utf-8"))
    folder = Path(models) / FOLDER
    folder.mkdir(parents=True, exist_ok=True)
    for name, info in manifest["files"].items():
        if stopped():raise InterruptedError("Graniteの準備を中止しました。")
        target = folder / name
        if target.is_file() and target.stat().st_size == info["size"]:continue
        temporary = folder / (name + ".download-" + uuid.uuid4().hex)
        progress(f"Graniteモデルを取得中… {name} / 約{info['size']//1000000}MB")
        try:
            url = f"https://huggingface.co/{manifest['repo']}/resolve/{manifest['revision']}/{name}"
            request = urllib.request.Request(url,headers={"User-Agent":"WhisperDesktop/2.26"})
            checksum, received, previous = hashlib.sha256(), 0, 0
            with urllib.request.urlopen(request,timeout=30) as response,temporary.open("wb") as output:
                while block := response.read(1024*1024):
                    if stopped():raise InterruptedError("Graniteの準備を中止しました。")
                    output.write(block);checksum.update(block);received += len(block)
                    now = time.monotonic()
                    if now-previous > .8:
                        progress(f"Graniteモデルを取得中… {received*100//info['size']}%")
                        previous = now
            if received != info["size"] or checksum.hexdigest() != info["sha256"]:
                raise RuntimeError("Graniteモデルを検証できませんでした。もう一度実行してください。")
            os.replace(temporary,target)
        finally:temporary.unlink(missing_ok=True)
    return folder
