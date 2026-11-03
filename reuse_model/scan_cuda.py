"""Compile a small CUDA state recurrence with NVIDIA NVRTC shipped by PyTorch.

No host compiler, Triton, mamba_ssm, downloaded executable, or system changes.
CUDA driver/NVRTC API: https://docs.nvidia.com/cuda/nvrtc/index.html
"""
import ctypes as C
from pathlib import Path
import threading
import torch

SOURCE = r'''
extern "C" __global__ void reuse_scan(
    const float* u, const float* dt, const float* A, const float* B,
    const float* Cc, const float* D, const float* z, float* out,
    int batch, int length, int channels) {
    int task = (blockIdx.x * blockDim.x + threadIdx.x) / 16;
    int s = threadIdx.x % 16;
    if (task >= batch * channels) return;
    int n = task / channels, d = task % channels;
    float a = A[d*16+s], state = 0.0f;
    unsigned mask = __activemask();
    for (int t=0; t<length; ++t) {
        int i = (n*length+t)*channels+d;
        int j = (n*length+t)*16+s;
        float delta = dt[i], x = u[i];
        state = expf(delta*a)*state + delta*x*B[j];
        float value = state*Cc[j];
        for (int k=8; k>0; k/=2)
            value += __shfl_down_sync(mask, value, k, 16);
        if (s==0) {
            float gate = z[i];
            out[i] = (value+D[d]*x) * (gate/(1.0f+expf(-gate)));
        }
    }
}
'''


class Kernel:
    def __init__(self, device):
        if not torch.cuda.is_available():
            raise RuntimeError('Nemotronノイズ除去にはNVIDIA GPUとCUDA版PyTorchが必要です。')
        root = Path(torch.__file__).parent / 'lib'
        candidates = sorted(p for p in root.glob('nvrtc64_*.dll') if '.alt.' not in p.name)
        if not candidates:
            raise RuntimeError('PyTorchのNVRTCライブラリが見つかりません。CUDA版でセットアップしてください。')
        self.nvrtc = nv = C.CDLL(str(candidates[0]))
        self.driver = driver = C.WinDLL('nvcuda.dll')
        nv.nvrtcCreateProgram.argtypes = [C.POINTER(C.c_void_p), C.c_char_p, C.c_char_p, C.c_int, C.c_void_p, C.c_void_p]
        nv.nvrtcCompileProgram.argtypes = [C.c_void_p, C.c_int, C.POINTER(C.c_char_p)]
        for name in ('nvrtcGetPTXSize', 'nvrtcGetProgramLogSize'):
            getattr(nv, name).argtypes = [C.c_void_p, C.POINTER(C.c_size_t)]
        for name in ('nvrtcGetPTX', 'nvrtcGetProgramLog'):
            getattr(nv, name).argtypes = [C.c_void_p, C.c_void_p]
        nv.nvrtcDestroyProgram.argtypes = [C.POINTER(C.c_void_p)]
        driver.cuModuleLoadData.argtypes = [C.POINTER(C.c_void_p), C.c_void_p]
        driver.cuModuleGetFunction.argtypes = [C.POINTER(C.c_void_p), C.c_void_p, C.c_char_p]
        driver.cuLaunchKernel.argtypes = [C.c_void_p, *([C.c_uint]*7), C.c_void_p, C.POINTER(C.c_void_p), C.c_void_p]
        program = C.c_void_p()
        self.check(nv.nvrtcCreateProgram(C.byref(program), SOURCE.encode(), b'reuse_scan.cu', 0, None, None), 'NVRTC create')
        try:
            major, minor = torch.cuda.get_device_capability(device)
            options = (C.c_char_p * 2)(f'--gpu-architecture=compute_{major}{minor}'.encode(), b'--std=c++11')
            result = nv.nvrtcCompileProgram(program, 2, options)
            if result:
                size = C.c_size_t()
                nv.nvrtcGetProgramLogSize(program, C.byref(size))
                log = C.create_string_buffer(max(1, size.value))
                nv.nvrtcGetProgramLog(program, log)
                raise RuntimeError('Nemotron CUDA初期化: ' + log.value.decode('utf-8', errors='replace')[:1200])
            size = C.c_size_t()
            self.check(nv.nvrtcGetPTXSize(program, C.byref(size)), 'NVRTC size')
            ptx = C.create_string_buffer(size.value)
            self.check(nv.nvrtcGetPTX(program, ptx), 'NVRTC PTX')
        finally:
            nv.nvrtcDestroyProgram(C.byref(program))
        self.module, self.function = C.c_void_p(), C.c_void_p()
        self.check(driver.cuModuleLoadData(C.byref(self.module), ptx), 'CUDA module')
        self.check(driver.cuModuleGetFunction(C.byref(self.function), self.module, b'reuse_scan'), 'CUDA function')

    @staticmethod
    def check(code, where):
        if code:
            raise RuntimeError(f'{where}: CUDA error {code}')

    def run(self, u, delta, a, b, c, skip, z):
        result = torch.empty_like(u)
        batch, length, channels = u.shape
        values = [C.c_void_p(t.data_ptr()) for t in (u, delta, a, b, c, skip, z, result)]
        values += [C.c_int(v) for v in (batch, length, channels)]
        args = (C.c_void_p * len(values))(*[C.cast(C.byref(v), C.c_void_p) for v in values])
        stream = C.c_void_p(torch.cuda.current_stream(u.device).cuda_stream)
        self.check(self.driver.cuLaunchKernel(self.function, (batch*channels+7)//8, 1, 1,
                                            128, 1, 1, 0, stream, args, None), 'CUDA scan')
        return result


_kernels = {}
_lock = threading.Lock()


def selective_scan(u, delta, a, b, c, skip, z):
    if u.ndim != 3 or not u.is_cuda:
        raise ValueError('NemotronのCUDA入力形式が正しくありません。')
    batch, length, channels = u.shape
    if (not batch or not length or not channels or delta.shape != u.shape or z.shape != u.shape
            or a.shape != (channels, 16) or b.shape != (batch, length, 16) or c.shape != b.shape
            or skip.shape != (channels,)):
        raise ValueError('NemotronのCUDA入力サイズが正しくありません。')
    if any(t.dtype != torch.float32 or not t.is_contiguous() or t.device != u.device
                              for t in (u, delta, a, b, c, skip, z)):
        raise ValueError('NemotronのCUDA入力形式が正しくありません。')
    with torch.cuda.device(u.device):
        with _lock:
            if u.device not in _kernels:
                _kernels[u.device] = Kernel(u.device)
        return _kernels[u.device].run(u, delta, a, b, c, skip, z)
