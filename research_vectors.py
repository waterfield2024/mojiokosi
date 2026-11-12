"""Japanese retrieval embeddings, stored and computed on the local machine."""
import multiprocessing
from pathlib import Path
import sys
import threading


REVISION = '24899e5de370b56d179604a007c0d727bf144504'
VECTOR_VERSION = 'ruri-v3-30m/' + REVISION + '/mean-512-v1'
DIMENSION = 256
FILES = {
    'config.json': '3230a8666ce50d245a0e68f5bff4682c419c541f1212ba001a497f327fb7a9e5',
    'tokenizer.json': '0a94ac9a0a02c067bdef25b72ae9f4ee33f48f552e55988d444f6d25eeb1d062',
    'tokenizer_config.json': '34004511993472f15c7202074284c57788c161fb6567de164b8e0c0266f155e4',
    'special_tokens_map.json': '30bf8256f9a1eb3287af2a9b7940465e29a38aad4a459a3e773bb3a14bd34c0f',
    'model.safetensors': 'e94155e342cbfc33280c345c6e2912905fb5de6e1a3adeb938f395aafd2f8832',
}


class JapaneseEncoder:
    def __init__(self, folder):
        from transformers import AutoModel, AutoTokenizer
        self.tokenizer = AutoTokenizer.from_pretrained(folder, local_files_only=True)
        self.model = AutoModel.from_pretrained(folder, local_files_only=True, use_safetensors=True,
                                               attn_implementation='eager').cpu().eval()

    def encode(self, texts, *, query=False):
        import torch
        prefix = '検索クエリ: ' if query else '検索文書: '
        inputs = self.tokenizer([prefix + text for text in texts], padding=True, truncation=True,
                                max_length=512, return_tensors='pt')
        with torch.inference_mode():
            hidden = self.model(**inputs).last_hidden_state
            mask = inputs.attention_mask.unsqueeze(-1)
            mean = (hidden * mask).sum(1) / mask.sum(1)
            return torch.nn.functional.normalize(mean).numpy().astype('<f4', copy=False)


def _serve(connection, folder):
    # Import the ML runtime in a child so it cannot collect Tk objects on a worker thread.
    # Ruri ships a complete tokenizer.json. Keep Transformers from probing the optional
    # native SentencePiece module in this dedicated process each time a search window opens.
    sys.modules.setdefault('sentencepiece', None)
    with connection:
        try:
            encoder = JapaneseEncoder(folder)
            connection.send((True, None))
            while True:
                texts, query = connection.recv()
                connection.send((True, encoder.encode(texts, query=query)))
        except (EOFError, BrokenPipeError):
            pass
        except Exception as exc:
            connection.send((False, str(exc)))


class LocalEncoder:
    def __init__(self, folder, stopped):
        self.stopped = stopped
        self.lock = threading.Lock()
        context = multiprocessing.get_context('spawn')
        self.connection, child = context.Pipe()
        self.process = context.Process(target=_serve, args=(child, folder), daemon=True)
        try:
            self.process.start()
        finally:
            child.close()
        try:
            self.receive()
        except BaseException:
            self.close()
            raise

    def receive(self):
        while not self.stopped():
            if self.connection.poll(.2):
                ok, value = self.connection.recv()
                if ok:
                    return value
                raise RuntimeError(value)
            if not self.process.is_alive():
                raise RuntimeError('意味検索のプロセスが終了しました。')
        raise InterruptedError()

    def encode(self, texts, *, query=False):
        with self.lock:
            self.connection.send((texts, query))
            return self.receive()

    def close(self):
        with self.lock:
            self.connection.close()
            self.process.join(timeout=1)
            if self.process.is_alive():
                self.process.terminate()
                self.process.join()


def load_encoder(stopped):
    from setup_local import download
    folder = Path(__file__).resolve().parent / 'models' / 'ruri-v3-30m' / REVISION
    base = f'https://huggingface.co/cl-nagoya/ruri-v3-30m/resolve/{REVISION}/'
    for name, checksum in FILES.items():
        if stopped():
            raise InterruptedError()
        download(base + name, folder / name, checksum)
    if stopped():
        raise InterruptedError()
    return LocalEncoder(folder, stopped)
