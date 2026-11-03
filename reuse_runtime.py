"""16 kHz PCM enhancement; input and output share the original sample timeline."""
import json
from pathlib import Path
import numpy as np


class Enhancer:
    def __init__(self, weights):
        import torch
        from safetensors.torch import load_file
        from reuse_model.generator_SEMamba_time_d4 import SEMamba
        if not torch.cuda.is_available():
            raise RuntimeError('Nemotronノイズ除去にはNVIDIA GPUが必要です。CUDA版でセットアップしてください。')
        torch.set_num_threads(2)
        self.device = torch.device('cuda')
        cfg = json.loads((Path(__file__).parent / 'reuse_model' / 'config.json').read_text('utf-8'))
        self.model = SEMamba(cfg)
        self.model.load_state_dict(load_file(str(weights)), strict=True)
        self.model.to(self.device).eval()

    def enhance(self, pcm):
        import torch
        from torch.nn import functional as F
        from reuse_model.stfts import mag_phase_stft, mag_phase_istft
        if not pcm or len(pcm) % 2 or len(pcm) > 12*32000:
            raise ValueError('ノイズ除去の入力は16kHz・モノラル・12秒以内のPCM音声にしてください。')
        samples = np.frombuffer(pcm, '<i2').astype(np.float32) / 32768.
        if not np.any(samples):
            return pcm
        with torch.inference_mode():
            audio = torch.from_numpy(samples).unsqueeze(0).to(self.device)
            count = audio.shape[-1]
            # Retain the last partial hop instead of trimming the utterance ending.
            padded = max(640, ((count+79)//80)*80)
            audio = F.pad(audio, (0, padded-count), mode='replicate')
            mag, phase, _ = mag_phase_stft(audio, 640, 80, 640, 'relu_log1p')
            enhanced, phase, _ = self.model(mag, phase)
            zero_portion = (torch.expm1(F.relu(enhanced)) == 0).float().mean(1)
            enhanced = enhanced.masked_fill((zero_portion > .5).unsqueeze(1), 0)
            output = mag_phase_istft(enhanced, phase, 640, 80, 640, 'relu_log1p')[:, :count]
            if output.shape[-1] != count or not torch.isfinite(output).all():
                raise RuntimeError('ノイズ除去の音声出力が正しくありません。')
            values = output.squeeze(0).cpu().numpy()
        return np.clip(np.rint(values*32768), -32768, 32767).astype('<i2').tobytes()
