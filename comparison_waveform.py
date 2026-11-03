"""Cancellable, bounded waveform envelopes for the visible part of a recording."""
from collections import OrderedDict
import math
import subprocess
import threading
import time

from comparison_audio import ComparisonAudioPlayer
from engine import TOOLS,NO_WINDOW

RATE=8000
MAX_BINS=4096


class WaveformLoader(ComparisonAudioPlayer):
    """Reuse the serialized generation worker, without opening an audio device."""
    def __init__(self):
        self.cache=OrderedDict()
        super().__init__();self.thread.name="comparison-waveform"

    def load(self,source,start,end):return self.play(source,[(start,end)])

    def _play(self,request):
        import numpy as np
        start,end=request.intervals[0];stat=request.source.stat()
        key=(str(request.source.resolve()),stat.st_size,stat.st_mtime_ns,start,end)
        if key in self.cache:
            data=self.cache.pop(key);self.cache[key]=data
            if not request.cancel.is_set():self.events.put((request.generation,"ready",data))
            return
        # Aggregate both channels: opposite-phase stereo must not look silent.
        block=max(1,math.ceil((end-start)*RATE/MAX_BINS));count=min(MAX_BINS,math.ceil((end-start)*RATE/block))
        lows=np.zeros(count,dtype=np.int16);highs=np.zeros(count,dtype=np.int16);frames=0
        command=[str(TOOLS/"ffmpeg.exe"),"-nostdin","-hide_banner","-loglevel","error",
                 "-ss",str(start),"-t",str(end-start),"-i",str(request.source),"-map","0:a:0","-vn",
                 "-ac","2","-ar",str(RATE),"-f","s16le","pipe:1"]
        threading.Thread(target=self._watch,args=(request,),daemon=True).start()
        process=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,creationflags=NO_WINDOW)
        request.process=process
        try:
            if request.cancel.is_set():self._cancel(request)
            while not request.cancel.is_set():
                request.activity=time.monotonic();pcm=process.stdout.read(32768*4)
                if not pcm:break
                if len(pcm)%4:raise ValueError("波形用の音声データを読み込めませんでした。")
                values=np.frombuffer(pcm,dtype="<i2").reshape(-1,2);offset=0
                while offset<len(values) and not request.cancel.is_set():
                    index=frames//block;take=min(len(values)-offset,block-frames%block)
                    if index<count:
                        part=values[offset:offset+take]
                        lows[index]=min(int(lows[index]),int(part.min()));highs[index]=max(int(highs[index]),int(part.max()))
                    frames+=take;offset+=take
            if not request.cancel.is_set() and (process.wait(timeout=5)!=0 or frames==0):
                raise ValueError("この音声の波形を読み込めませんでした。")
        finally:
            try:
                if process.poll() is None:
                    try:process.terminate()
                    except OSError:pass
                try:process.wait(timeout=1)
                except subprocess.TimeoutExpired:process.kill();process.wait(timeout=1)
            finally:process.stdout.close();request.process=None
            if request.failure:self.events.put((request.generation,"error",request.failure))
        if request.cancel.is_set():return
        data={"start":start,"end":end,"step":block/RATE,"low":lows.tolist(),"high":highs.tolist(),"frames":frames}
        self.cache[key]=data
        while len(self.cache)>6:self.cache.popitem(last=False)
        self.events.put((request.generation,"ready",data))
