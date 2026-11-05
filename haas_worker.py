"""Local CPU Haas worker. Only PCM over stdin; no ports or downloaded Python."""
import base64
import json
import os
from pathlib import Path
import sys
import traceback


def emit(**value):
    print(json.dumps(value, ensure_ascii=False), flush=True)


def serve(folder):
    from native_errors import handle_native_errors
    handle_native_errors()
    import numpy as np
    import sherpa_onnx
    from haas_decode import timed_result
    folder = Path(folder)
    recognizer = sherpa_onnx.OfflineRecognizer.from_transducer(
        encoder=str(folder / 'encoder.quantized.onnx'), decoder=str(folder / 'decoder.onnx'),
        joiner=str(folder / 'joiner.onnx'), tokens=str(folder / 'tokens.txt'),
        num_threads=min(4, max(1, int(os.environ.get('WHISPER_BACKGROUND_THREADS', os.cpu_count() or 1)))), sample_rate=16000,
        feature_dim=128, model_type='nemo_transducer', provider='cpu')
    emit(kind='ready', device='CPU')
    for line in sys.stdin:
        request = json.loads(line)
        if request.get('command') == 'close':
            return
        if request.get('command') != 'transcribe':
            raise ValueError('Haasの認識要求が正しくありません。')
        pcm = base64.b64decode(request['pcm'], validate=True)
        if len(pcm) % 2 or len(pcm) > 16000 * 2 * 30:
            raise ValueError('Haasの音声区間は16kHz・モノラル・30秒以内にしてください。')
        audio = np.frombuffer(pcm, dtype='<i2').astype('float32') / 32768
        if len(audio) < 512 or not np.any(np.abs(audio) > 1 / 32768):
            emit(kind='result', result={'text': '', 'segments': [], 'language': 'ja'})
            continue
        stream = recognizer.create_stream()
        stream.accept_waveform(16000, audio)
        recognizer.decode_stream(stream)
        result = stream.result
        emit(kind='result', result=timed_result(result.text, result.tokens, result.timestamps,
                                               result.durations, len(pcm) / 32000))


if __name__ == '__main__':
    try:
        serve(sys.argv[1])
    except Exception as exc:
        traceback.print_exc(file=sys.stderr)
        emit(kind='error', message=str(exc))
        raise SystemExit(1)
