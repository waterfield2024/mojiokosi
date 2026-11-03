# NVIDIA NemotronLabs RE-USE model code

Upstream: https://huggingface.co/nvidia/NVIDIA-NemotronLabs-RE-USE

Pinned revision: `31f82f2efad1095034492f236e8dac9239ba713b`

`codec_module_time_d4.py`, `generator_SEMamba_time_d4.py`,
`mamba_block2_SEMamba.py`, `stfts.py`, and `config.json` come from that revision.
Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES. All rights reserved.
Their OpenMDW-1.1 license and notices are retained in [LICENSE](LICENSE).

Local changes: the generator no longer inherits the Hugging Face download mixin;
the Mamba import points to the local inference-only implementation in `mamba.py`.
`mamba.py` and `scan_cuda.py` are application-side implementations of the
Mamba-1 recurrence, compatible with the original checkpoint parameter names.
The CUDA recurrence is compiled in memory with the NVIDIA NVRTC library shipped
with PyTorch. It uses float32 and the active PyTorch CUDA stream. No Mamba/Triton
extension or host compiler is installed. CPU reference code exists for numerical
verification, while the application's enhancement feature requires CUDA.

The original 9.6M-parameter weights are downloaded separately and verified with
the SHA-256 in `reuse_assets.py`; they are not included in this repository.
This is a local Windows adaptation, not an official NVIDIA Windows distribution.
It uses the regular NemotronLabs RE-USE model, not `Real-time_RE-USE`.
