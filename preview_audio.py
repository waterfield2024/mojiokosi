"""Per-player Windows PCM output with a pausable hardware playback clock.

The handle affects only this preview. No device/global volume is changed.
API contracts: learn.microsoft.com/windows/win32/api/mmeapi/
"""
import ctypes as C
from collections import deque
import os


class WaveFormat(C.Structure):
    _fields_ = [("tag",C.c_ushort),("channels",C.c_ushort),("rate",C.c_uint32),
                ("bytes_per_second",C.c_uint32),("align",C.c_ushort),("bits",C.c_ushort),("extra",C.c_ushort)]


class WaveHeader(C.Structure):
    _fields_ = [("data",C.c_void_p),("length",C.c_uint32),("recorded",C.c_uint32),
                ("user",C.c_size_t),("flags",C.c_uint32),("loops",C.c_uint32),
                ("next",C.c_void_p),("reserved",C.c_size_t)]


class MMTime(C.Structure):
    _fields_ = [("kind",C.c_uint32),("value",C.c_uint32),("padding",C.c_uint32)]


class WaveOutput:
    RATE = 48000
    def __init__(self, device=None):
        self.api=C.WinDLL("winmm");self.handle=C.c_void_p();self.pending=deque();self.submitted=0
        self.last_raw=0;self.wrap=0;self.closed=False
        self.api.waveOutOpen.argtypes=[C.POINTER(C.c_void_p),C.c_uint,C.POINTER(WaveFormat),C.c_size_t,C.c_size_t,C.c_uint32]
        for name in ("waveOutPrepareHeader","waveOutUnprepareHeader","waveOutWrite"):
            getattr(self.api,name).argtypes=[C.c_void_p,C.POINTER(WaveHeader),C.c_uint]
        for name in ("waveOutPause","waveOutRestart","waveOutReset","waveOutClose"):
            getattr(self.api,name).argtypes=[C.c_void_p]
        self.api.waveOutGetPosition.argtypes=[C.c_void_p,C.POINTER(MMTime),C.c_uint]
        self.api.waveOutGetErrorTextW.argtypes=[C.c_uint,C.c_wchar_p,C.c_uint]
        format=WaveFormat(1,2,self.RATE,self.RATE*4,4,16,0)
        if device is None:device=int(os.environ.get("WHISPER_PREVIEW_AUDIO_DEVICE","-1"))
        self.check(self.api.waveOutOpen(C.byref(self.handle),device & 0xffffffff,C.byref(format),0,0,0))
        try:self.pause()
        except Exception:self.close();raise

    def check(self, code):
        if code:
            text=C.create_unicode_buffer(256);self.api.waveOutGetErrorTextW(code,text,256)
            raise RuntimeError("音声を再生できません："+(text.value or str(code)))

    def collect(self):
        while self.pending and self.pending[0][0].flags & 1:
            header,buffer=self.pending[0]
            self.check(self.api.waveOutUnprepareHeader(self.handle,C.byref(header),C.sizeof(header)))
            self.pending.popleft()

    def write(self, pcm):
        if not pcm:return
        self.collect()
        buffer=C.create_string_buffer(pcm);header=WaveHeader(data=C.addressof(buffer),length=len(pcm))
        self.check(self.api.waveOutPrepareHeader(self.handle,C.byref(header),C.sizeof(header)))
        try:self.check(self.api.waveOutWrite(self.handle,C.byref(header),C.sizeof(header)))
        except Exception:
            self.api.waveOutUnprepareHeader(self.handle,C.byref(header),C.sizeof(header));raise
        self.pending.append((header,buffer));self.submitted+=len(pcm)//4

    def position(self):
        value=MMTime(kind=2)
        self.check(self.api.waveOutGetPosition(self.handle,C.byref(value),C.sizeof(value)))
        if value.value < self.last_raw and self.last_raw-value.value > 2**31:self.wrap+=2**32
        self.last_raw=value.value
        divisor={1:1000,2:self.RATE,4:self.RATE*4}.get(value.kind)
        if divisor is None:raise RuntimeError("音声出力から再生位置を取得できません。")
        return min(self.submitted/self.RATE,(self.wrap+value.value)/divisor)

    def pause(self):self.check(self.api.waveOutPause(self.handle))
    def resume(self):self.check(self.api.waveOutRestart(self.handle))

    def close(self):
        if self.closed:return
        self.closed=True
        if self.handle:
            self.api.waveOutReset(self.handle)
            for header,buffer in self.pending:
                self.api.waveOutUnprepareHeader(self.handle,C.byref(header),C.sizeof(header))
            self.pending.clear();self.api.waveOutClose(self.handle);self.handle=None
