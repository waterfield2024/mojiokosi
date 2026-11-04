"""Offline Whisper inference over private pipes, without whisper.cpp DLLs."""
import base64
import json
import os
import sys
import traceback
from pathlib import Path

sys.path.insert(0,str(Path(__file__).resolve().parent))


def emit(**value):print(json.dumps(value,ensure_ascii=False),flush=True)


def timed_result(decoded,duration,language):
    rows=[]
    for part in decoded.get('offsets',[]):
        text=part.get('text','').strip();a,b=part['timestamp']
        a=max(0.,min(duration,float(a or 0)));b=max(a,min(duration,float(b if b is not None else duration)))
        if text and b>a:rows.append({'start':a,'end':b,'text':text,'words':[]})
    if not rows and decoded.get('text','').strip():rows=[{'start':0.,'end':duration,'text':decoded['text'].strip(),'words':[]}]
    return {'text':''.join(row['text'] for row in rows),'segments':rows,'language':language}


def serve(folder,device):
    from native_errors import handle_native_errors
    handle_native_errors()
    os.environ['HF_HUB_OFFLINE']='1';os.environ['HF_HUB_DISABLE_TELEMETRY']='1'
    os.environ['HF_HUB_DISABLE_PROGRESS_BARS']='1';os.environ['TOKENIZERS_PARALLELISM']='false'
    import numpy as np
    import torch
    from transformers import WhisperForConditionalGeneration,WhisperProcessor
    torch.set_num_threads(min(8,max(1,(os.cpu_count() or 4)//2)))
    if device=='cuda' and not torch.cuda.is_available():raise RuntimeError('CUDAを利用できません。')
    dtype=torch.float16 if device=='cuda' else torch.float32
    processor=WhisperProcessor.from_pretrained(folder,local_files_only=True,trust_remote_code=False)
    model=WhisperForConditionalGeneration.from_pretrained(folder,local_files_only=True,trust_remote_code=False,dtype=dtype).to(device).eval()
    def recognize(audio,language,prompt):
        inputs=processor(audio,sampling_rate=16000,return_tensors='pt',return_attention_mask=True)
        inputs={key:value.to(device,dtype=dtype) if key=='input_features' else value.to(device) for key,value in inputs.items()}
        options={'task':'transcribe','language':None if language=='auto' else language,'return_timestamps':True,'num_beams':1,'do_sample':False,'max_new_tokens':224,'max_length':None}
        if len(audio) > 12*16000:
            # Longer live utterances need more output tokens. Use the total
            # decoder budget so prompt + start tokens still fit the model.
            options.update(max_new_tokens=None,max_length=model.config.max_target_positions)
        if prompt:
            ids=processor.get_prompt_ids(prompt[-800:],return_tensors='pt')
            # Keep the start-of-previous token and the most recent prompt tokens.
            if len(ids)>180:ids=torch.cat((ids[:1],ids[-179:]))
            options['prompt_ids']=ids.to(device)
        with torch.inference_mode():output=model.generate(**inputs,**options)
        return processor.tokenizer.decode(output[0],skip_special_tokens=True,output_offsets=True,clean_up_tokenization_spaces=False)
    # Exercise lazy kernels before the UI enables capture; the model stays loaded.
    recognize(np.zeros(16000,dtype=np.float32),'ja','')
    emit(kind='ready',device=device.upper())
    for line in sys.stdin:
        request=json.loads(line)
        if request['command']=='close':return
        if request['command']!='transcribe':raise ValueError('認識要求が正しくありません。')
        pcm=base64.b64decode(request['pcm'],validate=True)
        if len(pcm)%2 or len(pcm)>16000*2*32:raise ValueError('音声区間が正しくありません。')
        audio=np.frombuffer(pcm,dtype='<i2').astype('float32')/32768;duration=len(audio)/16000
        language=request.get('language','ja')
        if len(audio)<512 or not np.any(np.abs(audio)>1/32768):result={'text':'','segments':[],'language':language}
        else:result=timed_result(recognize(audio,language,request.get('prompt','')),duration,language)
        emit(kind='result',result=result)


if __name__=='__main__':
    try:serve(sys.argv[1],sys.argv[2])
    except Exception as exc:
        traceback.print_exc(file=sys.stderr);emit(kind='error',message=str(exc));raise SystemExit(1)
