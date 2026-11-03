# External software and model sources

The Git repository distributes the application source, asset manifests and notices.
It does not bundle a Python installation, native executables, Python wheels, model weights or user data.
Installations/downloads made by `setup_local.py` remain outside version control.

| Component | Source / license |
| --- | --- |
| Python 3.12 | https://www.python.org/ — PSF |
| PyTorch / TorchAudio 2.11.0 | https://pytorch.org/ — BSD-style; CUDA dependencies have NVIDIA terms |
| Transformers and Hugging Face libraries | https://github.com/huggingface/transformers — Apache-2.0 and component licenses |
| Whisper models | https://github.com/openai/whisper — MIT; pinned Hugging Face revisions in `whisper_hf_manifest.json` |
| Granite Speech 5.0 TurboCTC | https://huggingface.co/ibm-granite/granite-speech-5.0-470m-turboctc — Apache-2.0; revision in `granite_model_manifest.json` |
| sherpa-onnx | https://github.com/k2-fsa/sherpa-onnx — Apache-2.0 |
| Pyannote segmentation 3.0 ONNX | sherpa-onnx `speaker-segmentation-models` release; supplied model license retained in `licenses/segmentation-LICENSE` |
| 3D-Speaker ERes2Net embedding | sherpa-onnx `speaker-recongition-models` release; https://github.com/modelscope/3D-Speaker — Apache-2.0 |
| FFmpeg from imageio-ffmpeg 0.6.0 | https://github.com/imageio/imageio-ffmpeg — wrapper BSD-2-Clause; the downloaded FFmpeg executable has its own LGPL/GPL build terms; https://ffmpeg.org/legal.html |
| Node.js 24.19.0 | https://nodejs.org/dist/v24.19.0/ — MIT and bundled component notices |
| yt-dlp / yt-dlp-ejs | https://github.com/yt-dlp/yt-dlp — Unlicense and upstream component notices |
| PyAudioWPatch | https://github.com/s0d3s/PyAudioWPatch — MIT / PortAudio notices |
| windows-capture | https://github.com/NiiightmareXD/windows-capture — MIT |
| OpenCV Python | https://github.com/opencv/opencv-python — Apache-2.0 and bundled components |
| Ollama / Qwen2.5 7B (optional, installed separately) | https://ollama.com/ / https://huggingface.co/Qwen/Qwen2.5-7B-Instruct — MIT / Apache-2.0 |

Installed wheel license texts are available in `.venv/Lib/site-packages/*dist-info`.
Use media you have permission to process and follow each service's terms.

Optional native whisper.cpp adapters are retained for development compatibility;
this source setup uses Transformers/PyTorch and does not download whisper.cpp binaries.
