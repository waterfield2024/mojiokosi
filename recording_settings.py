"""Remember recording choices, with conservative matching of reopened windows."""
import json
import ntpath
import os
from pathlib import Path

MODES = {"audio", "screen", "window"}
CHUNK_SECONDS = (2, 3, 5, 8)


def settings_path():
    from user_data import data_path
    return data_path("recording_settings.json", "WHISPER_RECORDING_SETTINGS")


def audio_preference(device):
    if device is None:
        return None
    if not isinstance(device, dict) or not isinstance(device.get("name"), str) or not 0 < len(device["name"]) <= 32768:
        raise ValueError("音声出力先の設定形式が正しくありません。")
    # PortAudio indices change when outputs are reconnected or the PC restarts.
    return {"name": device["name"]}


def match_audio_device(previous, candidates):
    if not previous:
        return next((i for i, item in enumerate(candidates) if item.get("default")), 0) if candidates else None
    matches = [i for i, item in enumerate(candidates) if item.get("name") == previous["name"]]
    return matches[0] if len(matches) == 1 else None


def processing_preferences(data=None, *, context_default=False):
    if data is None:data = {}
    if not isinstance(data, dict):raise ValueError("リアルタイムの処理設定が正しくありません。")
    saved = {}
    for key, default in (("live_diarize", True), ("live_correct", False), ("context_correct", context_default), ("live_noise", False), ('live_previous_text', True)):
        value = data.get(key, default)
        if type(value) is not bool:raise ValueError("リアルタイムの処理設定はオンかオフを選んでください。")
        saved[key] = value
    seconds = data.get("chunk_seconds", 2)
    if type(seconds) is not int or seconds not in CHUNK_SECONDS:
        raise ValueError("音声を区切る間隔は2・3・5・8秒から選んでください。")
    saved["chunk_seconds"] = seconds
    from speech_preferences import profile
    saved['speech_sensitivity'] = profile(data.get('speech_sensitivity', 'balanced')).key
    from utterance_audio import mode, pause_duration, utterance_limit, MAX_SECONDS
    saved['segmentation'] = mode(data.get('segmentation', 'utterance'))
    saved['pause_seconds'] = pause_duration(data.get('pause_seconds', .7))
    saved['max_utterance_seconds'] = utterance_limit(data.get('max_utterance_seconds', MAX_SECONDS))
    return saved


def display_preferences(data=None):
    if data is None:data = {}
    if not isinstance(data, dict):raise ValueError("字幕の表示・固定の設定が正しくありません。")
    saved = {}
    for key, default in (("show", True), ("locked", False)):
        value = data.get(key, default)
        if type(value) is not bool:raise ValueError("字幕の表示・固定はオンかオフを選んでください。")
        saved[key] = value
    return saved


def settings_for(mode, targets, model="base", audio_device=None, overlay=None, processing=None, display=None, history=None):
    if not isinstance(mode, str) or mode not in MODES or not isinstance(targets, dict):
        raise ValueError("録画設定の形式が正しくありません。")
    if not isinstance(model, str) or not 0 < len(model) <= 256:
        raise ValueError("認識モデルの設定形式が正しくありません。")
    saved = {}
    for kind in ("screen", "window"):
        item = targets.get(kind)
        if item is None:
            continue
        if not isinstance(item, dict):
            raise ValueError("録画対象の設定形式が正しくありません。")
        keys = ("device",) if kind == "screen" else ("hwnd", "pid", "created", "class", "title", "exe", "app")
        target = {}
        for key in keys:
            value = item.get(key, "")
            if key in {"hwnd", "pid", "created"}:
                if type(value) is not int or not 0 < value < 2**64:
                    raise ValueError("録画するウィンドウの設定形式が正しくありません。")
            elif not isinstance(value, str) or len(value) > 32768:
                raise ValueError("録画対象の名前の設定形式が正しくありません。")
            target[key] = value
        if not target.get("device" if kind == "screen" else "class"):
            raise ValueError("録画対象の設定が空です。")
        saved[kind] = target
    from overlay_preferences import preferences
    from transcript_history import preferences as history_preferences
    return {"version": 6, "mode": mode, "targets": saved,
            "model": model, "audio_device": audio_preference(audio_device), "overlay": preferences(overlay),
            # Legacy settings inherit the existing global post-correction default once.
            "processing": None if processing is None else processing_preferences(processing),
            "display": display_preferences(display), "history": history_preferences(history)}


def load_settings():
    try:
        data = json.loads(settings_path().read_text("utf-8-sig"))
    except FileNotFoundError:
        return settings_for("audio", {})
    if not isinstance(data, dict) or data.get("version") not in (1, 2, 3, 4, 5, 6):
        raise ValueError("録画設定を読み込めませんでした。保存する内容を選び直してください。")
    return settings_for(data.get("mode"), data.get("targets"), data.get("model", "base"), data.get("audio_device"), data.get("overlay"), data.get("processing"), data.get("display"), data.get("history"))


def save_settings(data):
    from context_ai import write_json
    write_json(settings_path(), settings_for(data.get("mode"), data.get("targets"), data.get("model", "base"), data.get("audio_device"), data.get("overlay"), data.get("processing"), data.get("display"), data.get("history")))


def match_target(mode, previous, candidates, *, allow_reopened=False):
    """Return an index only for an exact live identity or a unique reopened match.

    Callers allow reopened matching only after confirming the old HWND has closed.
    A hidden but still living window must not resolve to a different visible one.
    """
    if mode not in {"window", "screen"}:
        return None
    if not previous:
        return 0 if mode == "screen" and candidates else None
    keys = ("hwnd", "pid", "created", "class") if mode == "window" else ("device",)
    exact = [i for i, item in enumerate(candidates)
             if all(previous.get(key) and item.get(key) == previous[key] for key in keys)]
    if len(exact) == 1:
        return exact[0]
    if mode != "window" or not allow_reopened or exact:
        return None
    if not all(previous.get(key) for key in ("exe", "class", "title")):
        return None
    def signature(item):
        return (ntpath.normcase(ntpath.normpath(item.get("exe", ""))),
                item.get("class"), item.get("title"))
    same = [i for i, item in enumerate(candidates) if signature(item) == signature(previous)]
    return same[0] if len(same) == 1 else None
