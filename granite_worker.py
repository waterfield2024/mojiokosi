"""Private stdin/stdout Granite worker. No network, remote code or listening port."""
import base64
import json
import os
import sys
import traceback


def emit(**value):
    print(json.dumps(value,ensure_ascii=False),flush=True)


def serve(folder,device):
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    os.environ["HF_HUB_DISABLE_PROGRESS_BARS"] = "1"
    os.environ["TOKENIZERS_PARALLELISM"] = "false"
    if device == 'mps': os.environ['PYTORCH_ENABLE_MPS_FALLBACK'] = '1'
    import numpy as np
    import torch
    from transformers import AutoProcessor,AutoModelForCTC
    from granite_decode import timed_tokens,segment_words
    if device == 'mps' and not torch.backends.mps.is_available():
        raise RuntimeError('Metalを利用できません。CPUで再試行します。')
    torch.set_num_threads(min(8,max(1,(os.cpu_count() or 4)//2)))
    if device == "cuda" and not torch.cuda.is_available():raise RuntimeError("CUDAを利用できません。")
    processor = AutoProcessor.from_pretrained(folder,local_files_only=True,trust_remote_code=False)
    # FP32 also supports older GeForce cards without BF16 hardware.
    model = AutoModelForCTC.from_pretrained(folder,local_files_only=True,trust_remote_code=False,
                                          dtype=torch.float32).to(device).eval()
    step = processor.feature_extractor.hop_length * processor.feature_extractor.frame_stacking
    step *= 2**len(model.config.encoder_config.subsample_layers)
    frame_seconds = step/processor.feature_extractor.sampling_rate
    # Finish lazy CUDA/kernel initialization before announcing that capture can start.
    with torch.inference_mode():
        warm = processor(np.zeros(16000,dtype=np.float32),sampling_rate=16000,return_tensors="pt",device=device)
        model.generate(**warm).cpu()
    emit(kind="ready",device=device.upper())
    for line in sys.stdin:
        request = json.loads(line)
        if request["command"] == "close":return
        if request["command"] != "transcribe":raise ValueError("認識要求が正しくありません。")
        pcm = base64.b64decode(request["pcm"],validate=True)
        if len(pcm)%2 or len(pcm)>16000*2*32:raise ValueError("音声区間が正しくありません。")
        duration = len(pcm)/32000
        audio = np.frombuffer(pcm,dtype="<i2").astype("float32")/32768
        if len(audio)<512 or not np.any(np.abs(audio)>1/32768):
            emit(kind="result",result={"text":"","segments":[],"language":"auto"});continue
        with torch.inference_mode():
            inputs = processor(audio,sampling_rate=16000,return_tensors="pt",device=device)
            predicted = model.generate(**inputs).cpu().tolist()[0]
        text, words = timed_tokens(predicted,processor.tokenizer,duration,frame_seconds,model.config.pad_token_id)
        segments = segment_words(words) if words else ([{"start":0.,"end":duration,"text":text,"words":[]}] if text else [])
        emit(kind="result",result={"text":text,"segments":segments,"language":"auto"})


if __name__ == "__main__":
    try:serve(sys.argv[1],sys.argv[2])
    except Exception as exc:
        traceback.print_exc(file=sys.stderr)
        emit(kind="error",message=str(exc))
        raise SystemExit(1)
