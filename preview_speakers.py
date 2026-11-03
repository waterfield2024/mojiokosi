"""Per-caption registered identity selection in a private preview draft."""
from speakers import UNKNOWN, speaker_name

TARGET = "_preview_registered_target"


def memory_links(result):
    links = {key: value["id"] for key, value in result.get("remembered_speakers", {}).items() if value.get("id")}
    links.update(result.get("speaker_memory_links", {}))
    return links


def assign_registered(result, index, target, profiles):
    profile = profiles.get(target)
    if profile is None:
        raise ValueError("選んだ登録済みの話者は削除されています。一覧から選び直してください。")
    name = profile["name"]
    links = memory_links(result)
    # Reuse an identity only when its displayed name also matches. Never rebind
    # the old session speaker: other captions may still correctly belong to it.
    speaker = next((key for key, value in links.items() if value == target
                    and key not in ("", UNKNOWN) and speaker_name(key, result.get("speakers")) == name), None)
    if speaker is None:
        used = set(result.get("speakers", {})) | set(links) | {row.get("speaker", "") for row in result["segments"]}
        number = 0
        while f"speaker_{number:02d}" in used:number += 1
        speaker = f"speaker_{number:02d}"
    result["segments"][index].update(speaker=speaker, speaker_manual=True, speaker_review=False)
    result["segments"][index][TARGET] = target
    result.setdefault("speakers", {})[speaker] = name
    result.setdefault("speaker_name_edits", {})[speaker] = name
    result.setdefault("speaker_memory_links", {})[speaker] = target


def validated_links(result, store):
    """Recheck selected identities before saving or touching learning stores."""
    selected = [row for row in result["segments"] if row.get(TARGET)]
    if not selected:return {}
    profiles = store.load()
    links = memory_links(result)
    required = {}
    for row in selected:
        target, speaker = row[TARGET], row["speaker"]
        if target not in profiles:
            raise ValueError("選んだ登録済みの話者は削除されています。「この字幕の話者」で選び直してください。")
        if profiles[target]["name"] != speaker_name(speaker, result.get("speakers")):
            raise ValueError("登録済みの話者名が変更されています。「この字幕の話者」で選び直して反映してください。")
        if links.get(speaker) != target:
            raise ValueError("話者の登録内容が変わりました。プレビューを開き直してください。")
        required[speaker] = target
    return required
