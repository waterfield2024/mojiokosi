"""Owned, cancellable video preparation and frame decoding for the preview dialog."""
from platform_support import tool_path
import json
from pathlib import Path
import subprocess
import sys
import queue
import threading
from engine import TOOLS, NO_WINDOW, emit
from video import get_source, inspect_video, NORMALIZE
from preview_playback import Playback, has_audio


def main():
    # Wait for input so the GUI can assign its process job before descendants start.
    request = json.loads(sys.stdin.readline())
    source = get_source(request["result"], request["folder"])
    width, height, duration = inspect_video(source)
    audio = has_audio(source)
    emit("prepared", source=str(source), width=width, height=height, duration=duration, audio=audio)
    commands=queue.Queue();player=None
    def read_commands():
        try:
            for line in sys.stdin:commands.put(json.loads(line))
        finally:commands.put({"op":"close"})
    threading.Thread(target=read_commands,daemon=True).start()
    try:
        while True:
            try:request=commands.get(timeout=.01 if player else .05)
            except queue.Empty:request=None
            if request:
                op=request.get("op","frame")
                if op=="close":break
                if op in ("stop","play","frame") and player:
                    player.close();player=None
                if op=="play":
                    try:
                        player=Playback(source,max(0.,float(request["seconds"])),duration,audio,request["epoch"])
                        player.report("starting")
                    except Exception as exc:emit("play_error",epoch=request["epoch"],text=str(exc))
                elif op in ("pause","resume") and player and player.epoch==request["epoch"]:
                    try:getattr(player,op)()
                    except Exception as exc:
                        emit("play_error",epoch=player.epoch,text=str(exc));player.close();player=None
                elif op=="frame":
                    seconds=max(0.,float(request["seconds"]));target=Path(request["frame"]);target.unlink(missing_ok=True)
                    command=[str(tool_path('ffmpeg')),"-nostdin","-hide_banner","-loglevel","error","-y",
                        "-ss",str(seconds),"-i",str(source),"-map","0:V:0",
                        "-vf",NORMALIZE+",scale=w='min(1280,iw)':h='min(900,ih)':force_original_aspect_ratio=decrease",
                        "-frames:v","1",str(target)]
                    result=subprocess.run(command,capture_output=True,timeout=60,creationflags=NO_WINDOW)
                    if result.returncode or not target.is_file():
                        emit("frame_error",token=request["token"],text="この時刻の映像を読み込めません。別の時刻を選んでください。")
                    else:emit("frame",token=request["token"],seconds=seconds,frame=str(target))
            if player:
                try:
                    player.tick()
                    if player.ended:player.close();player=None
                except Exception as exc:
                    emit("play_error",epoch=player.epoch,text=str(exc));player.close();player=None
    finally:
        if player:player.close()


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        emit("error", text=str(exc))
        sys.exit(1)
