"""Capture on a separate process; frame timestamps share the WASAPI recording epoch."""
import ctypes
import json
import math
from pathlib import Path
import queue
import subprocess
import sys
import threading
import time

from engine import TOOLS, NO_WINDOW, emit


def main():
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
    request=json.loads(sys.stdin.readline())
    source=None
    try:
        if request.get('window'):
            from window_capture import WindowFrames
            source=WindowFrames(request['window'])
        record(request,source)
    finally:
        if source:source.close()


def record(request,source=None):
    from PIL import ImageGrab, Image, ImageOps
    rect=tuple(int(n) for n in request['rect']);fps=int(request['fps'])
    if len(rect)!=4 or rect[2]<=rect[0] or rect[3]<=rect[1] or fps!=15:
        raise ValueError('録画する画面の設定が正しくありません。')
    folder=Path(request['folder'])
    width,height=rect[2]-rect[0],rect[3]-rect[1]
    if source:width,height=source.image().size
    scale=min(1,1920/width,1080/height)
    size=(max(2,int(width*scale)//2*2),max(2,int(height*scale)//2*2))
    def grab():
        frame=source.image() if source else ImageGrab.grab(bbox=rect,all_screens=True,include_layered_windows=True).convert('RGB')
        if frame.size!=size:
            frame=ImageOps.pad(frame,size,method=Image.Resampling.BILINEAR,color='black') if source else frame.resize(size,Image.Resampling.BILINEAR)
        return frame.tobytes()
    last=grab()
    monitor_device=request.get('device');checked_at=0
    command=[str(TOOLS/'ffmpeg.exe'),'-nostdin','-hide_banner','-loglevel','error','-n',
             '-f','rawvideo','-pixel_format','rgb24','-video_size',f'{size[0]}x{size[1]}','-framerate',str(fps),
             '-i','pipe:0','-an','-c:v','libx264','-preset','ultrafast','-crf','23','-pix_fmt','yuv420p',
             '-threads','2','-g',str(fps*2),'-flush_packets','1','-cluster_time_limit','1000',str(folder/'screen.mkv')]
    log=(folder/'encoder.log').open('wb')
    proc=subprocess.Popen(command,stdin=subprocess.PIPE,stdout=subprocess.DEVNULL,stderr=log,creationflags=NO_WINDOW)
    commands=queue.Queue()
    def read():
        try:
            for line in sys.stdin:commands.put(json.loads(line))
        finally:commands.put({'op':'cancel'})
    threading.Thread(target=read,daemon=True).start()
    epoch=None;count=0;frozen=False;finish=None;cancelled=False;last_grab=None;paused=False
    try:
        emit('ready',width=size[0],height=size[1],fps=fps,encoder_pid=proc.pid)
        while True:
            try:
                event=commands.get(timeout=.02 if epoch is None or frozen else max(.001,min(.02,epoch+count/fps-time.monotonic())))
            except queue.Empty:event=None
            if event:
                if event['op']=='cancel':cancelled=True;break
                if event['op']=='start':
                    epoch=float(event['epoch'])
                    if not math.isfinite(epoch) or abs(time.monotonic()-epoch)>30:raise ValueError('録音との同期時刻が正しくありません。')
                elif event['op']=='freeze':
                    frozen=True
                    if source:source.close()
                elif event['op']=='finish':finish=float(event['duration']);frozen=True
            if proc.poll() is not None:raise RuntimeError('映像の保存処理が終了しました。録画先の空き容量を確認してください。')
            if epoch is None:continue
            if finish is not None:
                if not math.isfinite(finish) or finish<0:raise ValueError('録画時間が正しくありません。')
                target=math.ceil(finish*fps)
                if target-count>fps*30:raise RuntimeError('映像の保存が追いつかず停止しました。')
                while count<target:proc.stdin.write(last);count+=1
                break
            if frozen:continue
            sample_time=time.monotonic()
            if source and sample_time-checked_at>.2:
                from window_capture import window_state
                state='closed' if source.closed.is_set() else window_state(request['window'])
                checked_at=sample_time
                if state=='closed':
                    frozen=True;emit('target_closed');source.close();continue
                if (state=='paused') != paused:
                    paused=state=='paused'
                    emit('status',text='ウィンドウが非表示・最小化中です。映像は最後の状態を保持し、録音は続けています。' if paused else
                         'ウィンドウの録画を再開しました。停止後に字幕を付けてMP4保存できます。')
                source.check()
            if monitor_device and sample_time-checked_at>1:
                from screen_capture import validate_monitor
                validate_monitor({'device':monitor_device,'rect':list(rect)})
                checked_at=sample_time
            due=max(0,int((sample_time-epoch)*fps))
            if due<count:continue
            if due-count>fps*5:raise RuntimeError('画面録画が追いつかず停止しました。ほかの処理を減らして再試行してください。')
            frame=last if paused else grab()
            # Fill missed frames with the previous image, never speed up the video
            # or shift captions when recognition or the encoder briefly uses more CPU.
            while count<due:proc.stdin.write(last);count+=1
            proc.stdin.write(frame);count+=1;last=frame;last_grab=sample_time
        proc.stdin.close()
        proc.wait(timeout=20)
        if not cancelled:
            if proc.returncode:raise RuntimeError('画面録画を保存できませんでした。')
            emit('complete',frames=count,duration=count/fps,last_capture=last_grab,width=size[0],height=size[1],fps=fps)
    finally:
        if proc.poll() is None:
            proc.kill();proc.wait(timeout=10)
        if not proc.stdin.closed:proc.stdin.close()
        log.close()


if __name__=='__main__':
    try:main()
    except Exception as exc:
        emit('error',text=str(exc));sys.exit(1)
