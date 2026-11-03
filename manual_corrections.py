"""Apply remembered human corrections with the same undoable history as AI edits."""
from copy import deepcopy
from datetime import datetime, timezone
import uuid
import correction_memory


def apply_to_result(data, rows=None, live=False):
    rows = data.get("segments", []) if rows is None else rows
    protected = data.get("live_text_edits", {})
    # Validate the whole batch before mutation if the memory file is unavailable.
    updates = []
    for row in rows:
        if (row.get("segment_id") in protected or row.get("manual_text_last") == row["text"]
                or row.get("manual_memory_applied") == row["text"]):
            continue
        text, count = correction_memory.apply_learned(row["text"])
        if count and text != row["text"]:
            updates.append((row, text, count))
    records = []
    for row, text, count in updates:
        key = row.setdefault("segment_id", uuid.uuid4().hex)
        record = {"id": uuid.uuid4().hex, "run_id": "manual-memory", "segment_id": key,
                  "start": row["start"], "end": row["end"], "speaker": row.get("speaker", ""),
                  "before": row["text"], "after": text, "reason": "同じ表記と前後の言葉に対して、以前の手動修正を反映しました。",
                  "status": "applied", "had_words": "words" in row, "words_before": deepcopy(row.get("words")),
                  "model": "手動修正の記憶", "origin": "manual_memory",
                  "created_at": datetime.now(timezone.utc).isoformat(), "live": live}
        if live:
            row.setdefault("live_asr_text", row["text"])
        row.update(text=text, words=[], manual_memory_applied=text)
        data.setdefault("context_review", []).append(record)
        data["manual_memory_count"] = data.get("manual_memory_count", 0)+count
        records.append(record)
    return records


def prepare_edit(row, old_text, text):
    owner = row["segment_id"]
    base = (row["manual_text_base"] if row.get("manual_text_owner") == owner and
            row.get("manual_text_last") == old_text else old_text)
    # Keep retry metadata even if the write fails. The user's text must still be saved.
    row.update(manual_text_base=base, manual_text_last=text, manual_text_owner=owner,
               manual_memory_pending=True)


def learn_edit(row, old_text, text):
    prepare_edit(row, old_text, text)
    count = correction_memory.learn_manual(row["manual_text_base"], text, row["segment_id"])
    row.pop("manual_memory_pending", None)
    return count
