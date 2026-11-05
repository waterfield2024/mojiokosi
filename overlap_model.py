"""Pinned SpeechBrain RE-SepFormer weights; no remote Python/YAML execution."""
import hashlib
import os
from pathlib import Path
import urllib.request
import uuid

REPO = 'speechbrain/resepformer-wsj02mix'
REVISION = 'b8e127bf2b3585c95eebbe7b786e9d3f16675156'
FILES = {
    'encoder.ckpt': (9067, '3908eb3146ab80c464c4a05a5cd9824ddd341694f38fae641f2625d2bb3bd97f'),
    'decoder.ckpt': (9003, '5504e2cecb7e0f94164a3bda231491bbef242e22d41762759e87f6f7699a9049'),
    'masknet.ckpt': (185533341, 'c7d93084e4189c384fb46f93df477977813afc19b01b2d4aa632e70fe04de2ad'),
}


def ensure_model(models, status=lambda text: None, stopped=lambda: False):
    folder = Path(models) / 'overlap-resepformer'
    folder.mkdir(parents=True, exist_ok=True)
    for name, (size, digest) in FILES.items():
        target = folder / name
        def valid(path):
            if not path.is_file() or path.stat().st_size != size:
                return False
            checksum = hashlib.sha256()
            with path.open('rb') as source:
                while block := source.read(1024 * 1024):
                    if stopped(): raise InterruptedError('音声分離の準備を中止しました。')
                    checksum.update(block)
            return checksum.hexdigest() == digest
        if valid(target): continue
        status('重なった声の分離モデルを取得中… 約186MB（初回のみ）')
        temporary = target.with_name(name + '.download-' + uuid.uuid4().hex)
        try:
            url = f'https://huggingface.co/{REPO}/resolve/{REVISION}/{name}'
            with urllib.request.urlopen(url, timeout=30) as source, temporary.open('wb') as output:
                while block := source.read(1024 * 1024):
                    if stopped(): raise InterruptedError('音声分離の準備を中止しました。')
                    output.write(block)
                    if output.tell() > size: raise RuntimeError('音声分離モデルのサイズが正しくありません。')
            if not valid(temporary): raise RuntimeError('音声分離モデルの検証に失敗しました。')
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
    return folder


class Separator:
    def __init__(self, models, status=lambda text: None):
        folder = ensure_model(models, status)
        import torch
        from speechbrain.lobes.models.dual_path import Encoder, Decoder
        from speechbrain.lobes.models.resepformer import SBTransformerBlock_wnormandskip, ResourceEfficientSeparator
        torch.set_num_threads(2)
        self.encoder = Encoder(kernel_size=16, out_channels=128)
        self.decoder = Decoder(in_channels=128, out_channels=1, kernel_size=16, stride=8, bias=False)
        def transformer():
            return SBTransformerBlock_wnormandskip(num_layers=8, d_model=128, nhead=8,
                d_ffn=1024, dropout=0, use_positional_encoding=True,
                norm_before=True, use_norm=True, use_skip=True)
        self.masknet = ResourceEfficientSeparator(input_dim=128, num_spk=2, causal=False,
            unit=256, segment_size=150, layer=2, mem_type='av',
            seg_model=transformer(), mem_model=transformer())
        for name in ('encoder', 'decoder', 'masknet'):
            module = getattr(self, name)
            module.load_state_dict(torch.load(folder / (name + '.ckpt'), map_location='cpu', weights_only=True))
            module.eval()

    def separate(self, samples):
        import numpy as np
        import torch
        import torchaudio.functional as audio
        source = torch.from_numpy(np.asarray(samples, dtype='float32')).reshape(1, -1)
        if source.shape[1] > 30 * 16000: raise ValueError('音声分離の区間は30秒以内にしてください。')
        with torch.inference_mode():
            mix = audio.resample(source, 16000, 8000)
            encoded = self.encoder(mix)
            masked = self.masknet(encoded) * encoded.unsqueeze(0)
            streams = torch.stack([self.decoder(masked[i])[0] for i in range(2)])
            streams = audio.resample(streams, 8000, 16000)
            streams = torch.nn.functional.pad(streams, (0, max(0, source.shape[1] - streams.shape[1])))
            return streams[:, :source.shape[1]].numpy()
