# External software and model sources

The Git repository distributes the application source, asset manifests and notices.
It does not bundle a Python installation, native executables, Python wheels, model weights or user data.
Installations/downloads made by `bootstrap.cmd` and `setup_local.py` remain outside version control.
When no compatible Python is installed, the bootstrap downloads the official full
Python 3.12.10 x64 runtime ZIP (including Tcl/Tk), retaining its bundled licenses.
The URL and SHA-256 are pinned from https://www.python.org/ftp/python/index-windows.json.

| Component | Source / license |
| --- | --- |
| Python 3.12 | https://www.python.org/ — PSF |
| PyTorch / TorchAudio 2.11.0 | https://pytorch.org/ — BSD-style; CUDA dependencies have NVIDIA terms |
| Transformers and Hugging Face libraries | https://github.com/huggingface/transformers — Apache-2.0 and component licenses |
| Whisper models | https://github.com/openai/whisper — MIT; pinned Hugging Face revisions in `whisper_hf_manifest.json` |
| Granite Speech 5.0 TurboCTC | https://huggingface.co/ibm-granite/granite-speech-5.0-470m-turboctc — Apache-2.0; revision in `granite_model_manifest.json` |
| Haas-ja-v1.0 | Third Intelligence: https://huggingface.co/third-intelligence/Haas-ja-v1.0 — Apache-2.0 |
| NVIDIA NemotronLabs RE-USE | https://huggingface.co/nvidia/NVIDIA-NemotronLabs-RE-USE — OpenMDW-1.1. Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. Model code and configuration in `reuse_model/`; full license retained in `reuse_model/LICENSE`. Revision and weights checksum in `reuse_assets.py`. See `reuse_model/README.md` for local modifications. |
| einops 0.8.1 | https://github.com/arogozhnikov/einops — MIT |
| Haas community ONNX conversion | oboroge0: https://huggingface.co/oboroge0/Haas-ja-v1.0-onnx-quantized — Apache-2.0; pinned revision and hashes in `haas_model_manifest.json`; original LICENSE and NOTICE retained in `licenses/Haas-LICENSE` and `licenses/Haas-NOTICE`. Unofficial quantized CPU conversion, not endorsed by Third Intelligence. |
| sherpa-onnx | https://github.com/k2-fsa/sherpa-onnx — Apache-2.0 |
| Silero VAD v5.0 | https://github.com/snakers4/silero-vad — MIT, Copyright (c) 2020-present Silero Team. Official ONNX weights pinned to commit `5cd2ba54db059f961e7545d4f211b44f005a6dfb`; URL and SHA-256 in `speech_gate.py`, license in `licenses/Silero-VAD-LICENSE`. |
| Pyannote segmentation 3.0 ONNX | sherpa-onnx `speaker-segmentation-models` release; supplied model license retained in `licenses/segmentation-LICENSE` |
| 3D-Speaker ERes2Net embedding | sherpa-onnx `speaker-recongition-models` release; https://github.com/modelscope/3D-Speaker — Apache-2.0 |
| FFmpeg from imageio-ffmpeg 0.6.0 | https://github.com/imageio/imageio-ffmpeg — wrapper BSD-2-Clause; the downloaded FFmpeg executable has its own LGPL/GPL build terms; https://ffmpeg.org/legal.html |
| Node.js 24.19.0 | https://nodejs.org/dist/v24.19.0/ — MIT and bundled component notices |
| yt-dlp / yt-dlp-ejs | https://github.com/yt-dlp/yt-dlp — Unlicense and upstream component notices |
| PyAudioWPatch | https://github.com/s0d3s/PyAudioWPatch — MIT / PortAudio notices |
| windows-capture | https://github.com/NiiightmareXD/windows-capture — MIT |
| python-sounddevice (Mac) | https://github.com/spatialaudio/python-sounddevice — MIT; bundled PortAudio notices apply. |
| PyObjC (Mac) | https://github.com/ronaldoussoren/pyobjc — MIT; Cocoa, Quartz, CoreMedia and ScreenCaptureKit bindings. |
| Homebrew ffmpeg-full (Mac) | https://formulae.brew.sh/formula/ffmpeg-full — installed by Homebrew, not bundled; includes libass and its FFmpeg build/component license terms. |
| OpenCV Python | https://github.com/opencv/opencv-python — Apache-2.0 and bundled components |
| Ollama / Qwen2.5 7B (optional, installed separately) | https://ollama.com/ / https://huggingface.co/Qwen/Qwen2.5-7B-Instruct — MIT / Apache-2.0 |

Installed wheel license texts are available in `.venv/Lib/site-packages/*dist-info`.
Use media you have permission to process and follow each service's terms.

Optional native whisper.cpp adapters are retained for development compatibility;
this source setup uses Transformers/PyTorch and does not download whisper.cpp binaries.
