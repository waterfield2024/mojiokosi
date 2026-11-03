"""Local contextual corrections and exclusions, without changing model weights."""
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timezone
from difflib import SequenceMatcher
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import time

_cache = {}
_boundaries = re.compile(r"[。！？!?\n、，,「」『』]")


def memory_path():
    from user_data import data_path
    return data_path("correction_memory.json", "WHISPER_CORRECTION_MEMORY")


def compact(text):
    return re.sub(r"\s+", "", text)


def edits(before, after):
    return [(a, b, before[a:b], after[c:d])
            for tag, a, b, c, d in SequenceMatcher(None, before, after, autojunk=False).get_opcodes()
            if tag != "equal"]


def rules_for(before, after):
    rules = []
    for start, end, source, target in edits(before, after):
        left = compact(_boundaries.split(before[:start])[-1])[-12:]
        right = compact(_boundaries.split(before[end:])[0])[:12]
        # Context-free fragments must not turn a single kanji into a global ban.
        exact = before if len(left + source + right) < 4 else ""
        rule = {"source": source, "target": target, "left": left, "right": right, "exact": exact}
        rule["id"] = hashlib.sha256(json.dumps(rule, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        rules.append(rule)
    return rules


def _matches(rule, before, edit):
    start, end, source, target = edit
    if rule["source"] != source or rule["target"] != target:
        return False
    if rule["exact"]:
        return compact(before) == compact(rule["exact"])
    left = compact(_boundaries.split(before[:start])[-1])
    right = compact(_boundaries.split(before[end:])[0])
    return left.endswith(rule["left"]) and right.startswith(rule["right"])


def _read(path):
    try:
        data = json.loads(path.read_text("utf-8-sig"))
        if not isinstance(data, dict) or data.get("version") not in (1, 2):
            raise ValueError("version")
        entries = data["entries"]
        if not isinstance(entries, list):
            raise ValueError("entries")
        seen = set()
        for entry in entries:
            if not isinstance(entry, dict) or not all(isinstance(entry.get(k), str) for k in
                       ("id", "source", "target", "left", "right", "exact", "before", "after", "created_at")):
                raise ValueError("entry")
            if not entry["id"] or entry["id"] in seen or entry["source"] == entry["target"]:
                raise ValueError("duplicate or empty entry")
            if entry.get("kind", "block") not in {"block", "replace"}:
                raise ValueError("kind")
            if entry.get("kind") == "replace" and not isinstance(entry.get("owner"), str):
                raise ValueError("owner")
            seen.add(entry["id"])
        return entries
    except FileNotFoundError:
        return []
    except (ValueError, KeyError, TypeError) as exc:
        raise ValueError("修正の記憶を読み込めません。correction_memory.jsonを確認してください。") from exc


def _entries():
    path = memory_path()
    try:
        stat = path.stat()
        stamp = (stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size)
    except FileNotFoundError:
        stamp = None
    key = str(path.resolve())
    cached = _cache.get(key)
    if cached is None or cached[0] != stamp:
        cached = (stamp, _read(path))
        _cache.clear()
        _cache[key] = cached
    return cached[1]


def list_entries():
    return deepcopy(_entries())


@contextmanager
def _locked(path):
    """Serialize writers from separate app windows; readers use atomic snapshots."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(path.suffix + ".lock").open("a+b") as lock:
        lock.seek(0, 2)
        if not lock.tell():
            lock.write(b"0")
            lock.flush()
        deadline = time.monotonic() + 1
        while True:
            try:
                lock.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise OSError("覚えた誤修正を別の画面で更新中です。もう一度操作してください。")
                time.sleep(.02)
        try:
            yield
        finally:
            lock.seek(0)
            if os.name == "nt":
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _update(transform):
    path = memory_path()
    with _locked(path):
        old = _read(path)
        entries = transform(deepcopy(old))
        if entries == old:
            return
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                    prefix=path.name + ".", suffix=".tmp", delete=False) as file:
                temporary = Path(file.name)
                json.dump({"version": 2, "entries": entries}, file, ensure_ascii=False, indent=2)
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())
            os.replace(temporary, path)
        finally:
            if temporary:
                temporary.unlink(missing_ok=True)
        _cache.clear()


def remember(before, after):
    rules = rules_for(before, after)
    def add(entries):
        changes = edits(before, after)
        entries = [e for e in entries if not (e.get("kind") == "replace" and
                   any(_matches(e, before, edit) for edit in changes))]
        existing = {e["id"] for e in entries}
        created = datetime.now(timezone.utc).isoformat()
        for rule in rules:
            if rule["id"] not in existing:
                entries.append({**rule, "before": before, "after": after, "created_at": created})
                existing.add(rule["id"])
        return entries
    _update(add)


def forget(ids):
    selected = set(ids)
    _update(lambda entries: [e for e in entries if e["id"] not in selected])


def allow(before, after):
    """Explicit redo also forgets any exclusion that would prevent that correction."""
    changes = edits(before, after)
    _update(lambda entries: [r for r in entries if r.get("kind") == "replace" or
                            not any(_matches(r, before, e) for e in changes)])


def filter_change(before, after):
    """Remove only remembered edits; retain independent corrections in the same row."""
    entries = _entries()
    if not entries or before == after:
        return after, 0
    changes = edits(before, after)
    protected = []
    for rule in entries:
        if rule.get("kind") == "replace":
            protected.extend((a, b) for a, b, _, _ in occurrences(rule, before, corrected=True))
    output, blocked = before, 0
    for edit in reversed(changes):
        start, end, source, target = edit
        if (any(_matches(rule, before, edit) for rule in entries if rule.get("kind", "block") == "block")
                or any(start < b and end > a or start == end and a < start < b for a, b in protected)):
            blocked += 1
        else:
            output = output[:start] + target + output[end:]
    return output, blocked


def manual_rules(before, after):
    """Only short lexical substitutions, never number edits, insertions or deletions."""
    if not before or not after or len(before) > 4000 or len(after) > 4000:
        return []
    matcher = SequenceMatcher(None, before, after, autojunk=False)
    changed = sum(max(b-a, d-c) for tag, a, b, c, d in matcher.get_opcodes() if tag != "equal")
    # A very short standalone word is scoped to that exact row.
    short_word = max(len(before), len(after)) <= 3 and before.isalpha() and after.isalpha()
    if not short_word and (matcher.ratio() < .72 or changed > max(3, min(24, len(before)*.3))):
        return []
    result = []
    for rule in rules_for(before, after):
        a, b = rule["source"], rule["target"]
        if (not a or not b or max(len(a), len(b)) > 16 or not a.isalpha() or not b.isalpha()
                or not re.search(r"[\u3400-\u4dbf\u4e00-\u9fffァ-ヶA-Za-z]", a+b)):
            continue
        result.append(rule)
    return result


def learn_manual(before, after, owner, *, restore=False):
    """Replace this row's previous learning; the latest explicit spelling wins."""
    rules = rules_for(before, after) if restore else manual_rules(before, after)
    changes = edits(before, after)
    def update(entries):
        retained = []
        for entry in entries:
            if entry.get("kind") == "replace":
                if entry.get("owner") == owner:
                    continue
                # Retire contradictory source->target or target->source memories.
                if any(entry["left"] == r["left"] and entry["right"] == r["right"] and
                       ((entry["source"] == r["source"] and entry["exact"] == r["exact"])
                        or (entry["source"] == r["target"] and entry["target"] == r["source"])) for r in rules):
                    continue
            elif any(_matches(entry, before, e) for e in changes) and rules:
                continue  # A new manual correction explicitly permits this spelling.
            retained.append(entry)
        created = datetime.now(timezone.utc).isoformat()
        for rule in rules:
            key = hashlib.sha256(("replace:"+owner+":"+rule["id"]).encode("utf-8")).hexdigest()
            retained.append({**rule, "id": key, "kind": "replace", "owner": owner,
                             "before": before, "after": after, "created_at": created})
        return retained
    _update(update)
    return len(rules)


def occurrences(rule, text, corrected=False):
    """Find exact spelling and contextual anchors in unmodified input."""
    source = rule["target"] if corrected else rule["source"]
    target = rule["source"] if corrected else rule["target"]
    if not source:
        return []
    test = {**rule, "source": source, "target": target}
    if corrected and rule["exact"]:
        test["exact"] = rule["exact"].replace(rule["source"], rule["target"], 1)
    found, offset = [], 0
    while True:
        start = text.find(source, offset)
        if start < 0:
            break
        edit = (start, start+len(source), source, target)
        if _matches(test, text, edit):
            found.append(edit)
        offset = start+max(1, len(source))
    return found


def apply_learned(text):
    """One non-cascading pass; ambiguous/overlapping memories are not guessed."""
    entries = _entries()
    candidates = {}
    for rule in entries:
        if rule.get("kind") != "replace":
            continue
        for edit in occurrences(rule, text):
            if any(_matches(block, text, edit) for block in entries if block.get("kind", "block") == "block"):
                continue
            candidates.setdefault((edit[0], edit[1]), set()).add(edit[3])
    spans = [(a, b, next(iter(targets))) for (a, b), targets in candidates.items() if len(targets) == 1]
    spans = [s for s in spans if not any(s is not t and s[0] < t[1] and s[1] > t[0] for t in spans)]
    output = text
    for start, end, target in sorted(spans, reverse=True):
        output = output[:start]+target+output[end:]
    return output, len(spans)
