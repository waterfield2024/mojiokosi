"""Shared model choices; loading heavy recognition libraries stays in workers."""
GRANITE_MODEL = "granite-turboctc"
GRANITE_LABEL = "Granite 5.0 TurboCTC"
HAAS_MODEL = "haas-ja-v1.0"
HAAS_LABEL = "Haas-ja-v1.0（CPU）"
FILE_MODELS = {"標準・small": "small", "軽量・base": "base", "高精度・turbo": "turbo", GRANITE_LABEL: GRANITE_MODEL, HAAS_LABEL: HAAS_MODEL}
LIVE_MODELS = {"速度優先・base": "base", "標準・small": "small", "高精度・turbo": "turbo", GRANITE_LABEL: GRANITE_MODEL, HAAS_LABEL: HAAS_MODEL}
ALLOWED_MODELS = set(FILE_MODELS.values())


def model_hint(name):
    if name == HAAS_MODEL:
        return "Haas：日本語専用・CPUで動作（非公式ONNX変換版）。初回に約481MB取得。話題ヒントは使わず、辞書・文脈校正は使えます。"
    if name == GRANITE_MODEL:
        return "Granite：言語は自動判別。話題ヒントは使いません。誤変換辞書・文脈校正は使えます。"
    return "Whisper：選んだ言語と「話題・専門用語」を使って認識します。"


def recognition_language(name, selected):
    return 'ja' if name == HAAS_MODEL else 'auto' if name == GRANITE_MODEL else selected


def live_recognizer_type(name):
    if name == HAAS_MODEL:
        from haas_asr import HaasRecognizer
        return HaasRecognizer
    if name == GRANITE_MODEL:
        from granite_asr import GraniteRecognizer
        return GraniteRecognizer
    from realtime_asr import LiveRecognizer
    return LiveRecognizer
