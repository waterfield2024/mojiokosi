"""Bounded, asynchronous proofreading. ASR never waits for an AI response."""
from copy import deepcopy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading
import time
import uuid

from context_ai import MODEL, SCHEMA, SYSTEM_PROMPT, acceptable_change, decode_edits, validate_changes
from correction_memory import filter_change

RESPONSE_LIMIT = 4.0


def acceptable_live_change(before, after):
    # Live captions must not change the wording, tense, numbers or punctuation.
    # Only kanji spelling changes survive; broader proofreading remains available after stop.
    without_kanji = lambda text: re.sub(r"[\u3400-\u4dbf\u4e00-\u9fff々]", "", text)
    return (acceptable_change(before, after) and without_kanji(before) == without_kanji(after)
            and len(after) <= len(before) + 4)


def snapshot(data, seen):
    rows = data.get("segments", [])[-6:]
    protected = data.get("live_text_edits", {})
    targets = [r for r in rows[-2:] if r.get("segment_id") not in seen
               and r.get("segment_id") not in protected and 3 <= len(r["text"]) <= 140]
    if not targets or not re.search(r"[ぁ-んァ-ヶ]", "".join(r["text"] for r in rows)):
        return None
    selected = {r["segment_id"] for r in targets}
    context = [{"speaker": r.get("speaker", ""), "text": r["text"][-80:]}
               for r in rows if r.get("segment_id") not in selected][-2:]
    return {"targets": [{"id": i, "segment_id": r["segment_id"], "speaker": r.get("speaker", ""), "text": r["text"]}
                        for i, r in enumerate(targets)], "context_before": context,
            "term_hint": str(data.get("prompt", ""))[:120]}


def proofread(request):
    import context_ai as ai
    targets = request["targets"]
    schema = deepcopy(SCHEMA)
    schema["properties"]["changes"]["maxItems"] = 4
    schema["properties"]["changes"]["items"]["properties"]["id"]["enum"] = [r["id"] for r in targets]
    visible = {"context_before": request["context_before"], "term_hint": request.get("term_hint", ""),
               "targets": [{k: r[k] for k in ("id", "speaker", "text")} for r in targets]}
    response = ai.local_request("/api/chat", {
        "model": MODEL, "stream": False, "keep_alive": -1, "format": schema,
        "messages": [{"role": "system", "content": SYSTEM_PROMPT +
                      " For live captions, correct kanji homophones only. Never change kana, tense or politeness. Return no changes when uncertain."},
                     {"role": "user", "content": json.dumps(visible, ensure_ascii=False)}],
        "options": {"temperature": 0, "seed": 42, "num_ctx": 2048, "num_predict": 160,
                    "num_thread": 2, "repeat_penalty": 1.0, **({"num_gpu": 0} if ai.COMPUTE_MODE == "cpu" else {})}},
        timeout=RESPONSE_LIMIT)
    if response.get("done_reason") == "length":
        return []
    changes, _ = validate_changes(decode_edits(json.loads(response["message"]["content"]), targets), targets)
    output = []
    for row in targets:
        if row["id"] not in changes:
            continue
        text, _ = filter_change(row["text"], changes[row["id"]]["text"])
        if acceptable_live_change(row["text"], text):
            output.append({"segment_id": row["segment_id"], "before": row["text"], "after": text,
                           "reason": changes[row["id"]]["reason"]})
    return output


def apply_changes(data, changes):
    """Compare the expected text before writing; manual undo permanently protects a row."""
    records = []
    for change in changes:
        key = change["segment_id"]
        if key in data.get("live_text_edits", {}):
            continue
        row = next((r for r in data["segments"] if r.get("segment_id") == key), None)
        if row is None or row["text"] != change["before"] or not acceptable_live_change(row["text"], change["after"]):
            continue
        text, _ = filter_change(row["text"], change["after"])
        if not acceptable_live_change(row["text"], text):
            continue
        record = {"id": uuid.uuid4().hex, "run_id": "live", "segment_id": key,
                  "start": row["start"], "end": row["end"], "speaker": row.get("speaker", ""),
                  "before": row["text"], "after": text, "reason": change["reason"],
                  "status": "applied", "had_words": "words" in row, "words_before": deepcopy(row.get("words")),
                  "model": MODEL, "created_at": datetime.now(timezone.utc).isoformat(), "live": True}
        row.setdefault("live_asr_text", row["text"])
        row.update(text=text, words=[])
        data.setdefault("context_review", []).append(record)
        records.append(record)
    return records


class LiveProofreader:
    def __init__(self, compute, folder):
        from live_process import ProcessJob
        self.compute = compute
        self.ready = self.busy = self.failed = False
        self.events = queue.Queue()
        self.state = "字幕の自動修正を準備中。字幕は先に表示します。"
        self.proc = self.job = self.log = None
        self.started = time.monotonic()
        try:
            self.log = (Path(folder) / "live-context.log").open("wb")
            python = Path(sys.executable).with_name("python.exe") if os.name == "nt" else Path(sys.executable)
            self.proc = subprocess.Popen([str(python), "-X", "utf8", "-u", str(Path(__file__).resolve()), "--worker"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.log, text=True, encoding="utf-8",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0) | getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0))
            self.job = ProcessJob(self.proc)
            self.send({"compute": compute})  # Attach ownership before the child starts native models.
            def read():
                try:
                    for line in self.proc.stdout:
                        try: self.events.put(json.loads(line))
                        except ValueError: pass
                except (OSError, ValueError):
                    pass
                finally:
                    self.events.put({"kind": "error", "message": "字幕の自動修正は休止中。録音・字幕は続きます。"})
            threading.Thread(target=read, daemon=True).start()
        except BaseException:
            self.close()
            raise

    def send(self, value):
        self.proc.stdin.write(json.dumps(value, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()

    def poll(self):
        replies = []
        while True:
            try: event = self.events.get_nowait()
            except queue.Empty: break
            if event["kind"] == "ready":
                self.ready = True
                self.state = "字幕の自動修正：有効（先に表示し、校正後に差し替え）"
            elif event["kind"] == "done":
                self.busy = False
                replies.append(event)
            elif event["kind"] == "error":
                if not self.failed:
                    self.state = event["message"]
                self.failed = True
        if not self.ready and not self.failed and time.monotonic() - self.started > 60:
            self.failed = True
            self.state = "字幕の自動修正は準備できませんでした。録音・字幕は続きます。"
        return replies

    def submit(self, value, session):
        if not self.ready or self.busy or self.failed:
            return False
        try:
            self.send({**value, "session": session})
            self.busy = True
            return True
        except (OSError, ValueError):
            self.failed = True
            self.state = "字幕の自動修正は休止中。録音・字幕は続きます。"
            return False

    def close(self):
        if self.proc:
            if self.job and self.job.handle:
                self.job.close()
            elif self.proc.poll() is None:
                subprocess.run(["taskkill", "/PID", str(self.proc.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=5)
            self.proc.wait(timeout=5)
            for stream in (self.proc.stdin, self.proc.stdout):
                if stream: stream.close()
        if self.log: self.log.close()
        self.ready = False


def worker():
    import context_ai as ai
    import local_ai_runtime as runtime
    def emit(**value):
        print(json.dumps(value, ensure_ascii=False), flush=True)
    try:
        config = json.loads(sys.stdin.readline())
        ai.set_compute_mode(config.get("compute", "auto"))
        # Use only the private bundled service, never attach to the user's own server.
        if not runtime.available():
            raise RuntimeError("同梱の校正AIが見つかりません。")
        ai.ensure_model()
        ai.local_request("/api/generate", {"model": MODEL, "keep_alive": -1,
            "options": {"num_ctx": 2048, "num_thread": 2, **({"num_gpu": 0} if ai.COMPUTE_MODE == "cpu" else {})}}, timeout=50)
        emit(kind="ready")
        for line in sys.stdin:
            request = json.loads(line)
            started = time.monotonic()
            changes = proofread(request)
            elapsed = time.monotonic() - started
            if elapsed > RESPONSE_LIMIT:
                raise TimeoutError()
            emit(kind="done", session=request["session"], changes=changes, seconds=round(elapsed, 3))
    except Exception as exc:
        print(str(exc), file=sys.stderr, flush=True)
        emit(kind="error", message="字幕の自動修正は速度不足・エラーで休止中。録音・字幕は続きます。")
    finally:
        runtime.stop()


if __name__ == "__main__":
    worker()
