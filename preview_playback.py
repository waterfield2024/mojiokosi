"""Bounded continuous AV decoding; audio-device position is the master clock."""
import base64
import queue
import subprocess
import threading
import time
from engine import TOOLS,NO_WINDOW,emit
from video import NORMALIZE
from preview_audio import WaveOutput


def has_audio(source):
    probe=subprocess.run([str(TOOLS/"ffmpeg.exe"),"-nostdin","-hide_banner","-loglevel","error",
        "-i",str(source),"-map","0:a:0","-t","0","-f","null","-"],capture_output=True,timeout=30,creationflags=NO_WINDOW)
    return probe.returncode==0


class Playback:
    FPS=20
    def __init__(self,source,start,duration,audio,epoch):
        self.start=start;self.duration=duration;self.epoch=epoch;self.audio=None
        self.stop=threading.Event();self.frames=queue.Queue(12);self.pcm=queue.Queue(16);self.errors=queue.Queue()
        self.video_done=threading.Event();self.audio_done=threading.Event();self.processes=[];self.threads=[]
        self.next_frame=None;self.last_frame=None;self.ready=False;self.playing=True;self.ended=False
        self.silent_elapsed=0.;self.anchor=None;self.last_report=0.;self.created=time.monotonic()
        try:
            if audio:self.audio=WaveOutput()
            common=[str(TOOLS/"ffmpeg.exe"),"-nostdin","-hide_banner","-loglevel","error","-ss",str(start),"-i",str(source)]
            video=self.spawn(common+["-map","0:V:0","-an","-vf",NORMALIZE+
                ",scale=w='min(960,iw)':h='min(540,ih)':force_original_aspect_ratio=decrease,fps=20:start_time=0",
                "-threads","2","-c:v","mjpeg","-q:v","5","-f","image2pipe","pipe:1"])
            self.thread(self.read_video,video)
            if audio:
                limit=["-t",str(max(.001,duration-start))] if duration else []
                filters="aresample=48000:async=1:first_pts=0"+( ",apad" if duration else "")
                sound=self.spawn(common+["-map","0:a:0","-vn","-af",filters,*limit,
                    "-ac","2","-ar","48000","-f","s16le","pipe:1"])
                self.thread(self.read_audio,sound)
            else:self.audio_done.set()
        except Exception:self.close();raise

    def spawn(self,command):
        process=subprocess.Popen(command,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,creationflags=NO_WINDOW)
        self.processes.append(process);return process

    def thread(self,target,process):
        worker=threading.Thread(target=target,args=(process,),daemon=True);self.threads.append(worker);worker.start()

    def put(self,target,value):
        while not self.stop.is_set():
            try:target.put(value,timeout=.1);return True
            except queue.Full:pass
        return False

    def read_video(self,process):
        buffer=bytearray();index=0
        try:
            while not self.stop.is_set():
                block=process.stdout.read1(65536)
                if not block:break
                buffer.extend(block)
                while True:
                    end=buffer.find(b"\xff\xd9")
                    if end<0:break
                    image=bytes(buffer[:end+2]);del buffer[:end+2]
                    if not image.startswith(b"\xff\xd8"):raise RuntimeError("再生用の映像を読み込めません。")
                    if not self.put(self.frames,(index/self.FPS,image)):return
                    index+=1
                if len(buffer)>16*1024*1024:raise RuntimeError("再生用の映像が大きすぎます。")
            if not self.stop.is_set() and (process.wait()!=0 or not index):raise RuntimeError("再生用の映像を読み込めません。")
        except Exception as exc:
            if not self.stop.is_set():self.errors.put(exc)
        finally:process.stdout.close();self.video_done.set()

    def read_audio(self,process):
        try:
            while not self.stop.is_set():
                block=process.stdout.read(48000*4//25)
                if not block:break
                if not self.put(self.pcm,block):return
            if not self.stop.is_set() and process.wait()!=0:raise RuntimeError("動画の音声を読み込めません。")
        except Exception as exc:
            if not self.stop.is_set():self.errors.put(exc)
        finally:process.stdout.close();self.audio_done.set()

    def elapsed(self):
        if not self.ready:return 0.
        if self.audio:return self.audio.position()
        return self.silent_elapsed+(time.monotonic()-self.anchor if self.playing and self.anchor is not None else 0.)

    def report(self,state=None):
        emit("play_state",epoch=self.epoch,state=state or ("playing" if self.playing else "paused"),
             seconds=self.start+self.elapsed(),audio=self.audio is not None)

    def pause(self):
        if self.audio:self.audio.pause()
        elif self.ready and self.playing:self.silent_elapsed=self.elapsed();self.anchor=None
        self.playing=False;self.report("paused")

    def resume(self):
        if self.playing:return
        self.playing=True
        if self.ready:
            if self.audio:self.audio.resume()
            else:self.anchor=time.monotonic()
        self.report("playing" if self.ready else "starting")

    def tick(self):
        if not self.errors.empty():raise self.errors.get_nowait()
        if self.audio:
            self.audio.collect()
            while len(self.audio.pending)<10:
                try:self.audio.write(self.pcm.get_nowait())
                except queue.Empty:break
        if self.next_frame is None:
            try:self.next_frame=self.frames.get_nowait()
            except queue.Empty:pass
        if not self.ready:
            enough_audio=not self.audio or len(self.audio.pending)>=4 or (self.audio_done.is_set() and self.audio.pending)
            if self.next_frame is not None and enough_audio:
                self.ready=True
                if self.playing:
                    if self.audio:self.audio.resume()
                    else:self.anchor=time.monotonic()
                self.report()
            elif time.monotonic()-self.created>15:raise RuntimeError("動画の再生準備が完了しませんでした。再度お試しください。")
            else:return
        elapsed=self.elapsed();chosen=None
        while self.next_frame is not None and self.next_frame[0]<=elapsed+.005:
            chosen=self.next_frame;self.last_frame=self.next_frame
            try:self.next_frame=self.frames.get_nowait()
            except queue.Empty:self.next_frame=None
        if chosen:
            emit("play_frame",epoch=self.epoch,seconds=self.start+chosen[0],
                 clock=self.start+elapsed,jpeg=base64.b64encode(chosen[1]).decode("ascii"))
        ended=False
        if self.audio:
            ended=self.audio_done.is_set() and self.pcm.empty() and not self.audio.pending
            if ended and self.duration and self.start+elapsed<self.duration-.15:
                raise RuntimeError("音声の再生が途中で停止しました。再度お試しください。")
        elif self.video_done.is_set() and self.frames.empty() and self.next_frame is None and self.last_frame:
            ended=elapsed>=self.last_frame[0]+1/self.FPS
        if self.duration and self.start+elapsed>=self.duration-.005:ended=True
        if ended and self.playing:
            self.ended=True;self.report("ended")
        elif time.monotonic()-self.last_report>.2:
            self.last_report=time.monotonic();self.report()

    def close(self):
        self.stop.set()
        if self.audio:self.audio.close()
        for process in self.processes:
            if process.poll() is None:process.terminate()
        for process in self.processes:
            try:process.wait(timeout=2)
            except subprocess.TimeoutExpired:process.kill()
        for worker in self.threads:worker.join(timeout=1)
