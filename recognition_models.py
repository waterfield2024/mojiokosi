"""Shared model choices; loading heavy recognition libraries stays in workers."""
GRANITE_MODEL = "granite-turboctc"
GRANITE_LABEL = "Granite 5.0 TurboCTC"
FILE_MODELS = {"標準・small": "small", "軽量・base": "base", "高精度・turbo": "turbo", GRANITE_LABEL: GRANITE_MODEL}
LIVE_MODELS = {"速度優先・base": "base", "標準・small": "small", "高精度・turbo": "turbo", GRANITE_LABEL: GRANITE_MODEL}
ALLOWED_MODELS = set(FILE_MODELS.values())


def model_hint(name):
    if name == GRANITE_MODEL:
        return "Granite：言語は自動判別。話題ヒントは使いません。誤変換辞書・文脈校正は使えます。"
    return "Whisper：選んだ言語と「話題・専門用語」を使って認識します。"
