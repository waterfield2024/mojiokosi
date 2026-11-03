"""Literal, non-cascading transcript corrections and a persistent user dictionary."""
from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path

from user_data import data_path
DEFAULT_PATH = data_path("corrections.json", "WHISPER_CORRECTIONS")


def normalize_rules(rules):
    if not isinstance(rules, list):
        raise ValueError("誤変換辞書の形式が正しくありません。")
    result, seen = [], set()
    for rule in rules:
        if not isinstance(rule, dict):
            raise ValueError("辞書には置換前・置換後の組を登録してください。")
        before, after = rule.get("before"), rule.get("after")
        if not isinstance(before, str) or not isinstance(after, str):
            raise ValueError("置換前・置換後には文字列を入力してください。")
        if not before:
            raise ValueError("検索する文字（置換前）を入力してください。")
        if any(c in before + after for c in ("\r", "\n", "\0")):
            raise ValueError("置換前・置換後は改行を含まない1行で入力してください。")
        if before == after:
            raise ValueError("置換前と置換後に異なる文字を入力してください。")
        if before in seen:
            raise ValueError(f"「{before}」の置換先が重複しています。既存の登録を更新してください。")
        seen.add(before)
        result.append({"before": before, "after": after})
    return result


def normalize_settings(data):
    if not isinstance(data, dict) or data.get("version", 1) != 1:
        raise ValueError("誤変換辞書の形式・バージョンを確認してください。")
    enabled = data.get("auto_apply", True)
    if not isinstance(enabled, bool):
        raise ValueError("辞書の自動適用設定が正しくありません。")
    return {"version": 1, "auto_apply": enabled, "rules": normalize_rules(data.get("rules", []))}


def load_settings(path=DEFAULT_PATH):
    path = Path(path)
    if not path.exists():
        return normalize_settings({})
    return normalize_settings(json.loads(path.read_text(encoding="utf-8-sig")))


def save_settings(settings, path=DEFAULT_PATH):
    data = normalize_settings(settings)
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=path.name + ".", suffix=".tmp", delete=False) as file:
            temporary = Path(file.name)
            json.dump(data, file, ensure_ascii=False, indent=2)
            file.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return data


def replace_segments(segments, rules):
    """Return copies, occurrence count, changed row count; never rewrite speaker/times."""
    rules = normalize_rules(rules)
    replacements = {r["before"]: r["after"] for r in rules}
    if not replacements:
        return [dict(s) for s in segments], 0, 0
    pattern = re.compile("|".join(re.escape(key) for key in sorted(replacements, key=len, reverse=True)))
    output, total, rows = [], 0, 0
    for segment in segments:
        text, count = pattern.subn(lambda match: replacements[match.group()], segment["text"])
        changed = text != segment["text"]
        # Stale word text must not restore misrecognitions during later speaker analysis.
        output.append({**segment, "text": text, "words": []} if changed else dict(segment))
        total += count
        rows += int(changed)
    return output, total, rows
