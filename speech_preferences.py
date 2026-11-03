"""Shared sensitivity choices for capture, speech detection, UI and backups."""
from dataclasses import dataclass
import json
from pathlib import Path

DEFAULT = 'balanced'
LABELS = {'雑音を抑える': 'strict', '標準': 'balanced', '小さい声・短い返事を拾う': 'sensitive'}
HINTS = {
    'strict': '雑音の誤認識を抑えます。小さい声や短い返事は拾いにくくなります。',
    'balanced': '声の拾いやすさと、無音・雑音の除外を両立する標準設定です。',
    'sensitive': '小さい声や短い返事を優先します。雑音も拾いやすくなるため、誤認識が増えたら「標準」に戻してください。',
}


@dataclass(frozen=True)
class Sensitivity:
    key: str
    capture_rms: int
    capture_seconds: float
    final_seconds: float
    vad_threshold: float
    speech_seconds: float
    floor_rms: int
    active_frames: int
    max_gain: float
    overlap_samples: int


PROFILES = {
    'strict': Sensitivity('strict', 180, .3, .16, .55, .16, 12, 3, 10., 640),
    'balanced': Sensitivity('balanced', 110, .2, .1, .35, .096, 8, 2, 20., 640),
    'sensitive': Sensitivity('sensitive', 35, .06, .04, .2, .064, 3, 1, 50., 320),
}


def profile(value=DEFAULT):
    if not isinstance(value, str) or value not in PROFILES:
        raise ValueError('無音判定の感度は一覧から選んでください。')
    return PROFILES[value]


def read_control(path, current):
    """Keep the last valid choice while an atomic control file is absent/invalid."""
    try:
        data = json.loads(Path(path).read_text('utf-8'))
        return profile(data['sensitivity']).key
    except (OSError, ValueError, KeyError, TypeError):
        return current
