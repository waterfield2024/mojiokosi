"""Commit the private preview draft only after the user chooses an MP4 destination."""
from copy import deepcopy
from context_ai import write_json
from manual_corrections import prepare_edit, learn_edit
from speakers import normalize_names
from preview_speakers import TARGET, validated_links


def save_preview_edits(app, preview, layout):
    edited_rows = preview.result["segments"]
    if [s.get("segment_id") for s in edited_rows] != [s.get("segment_id") for s in app.segments]:
        raise ValueError("字幕の一覧が変わりました。プレビューを開き直してください。")
    links = validated_links(preview.result,app.speaker_memory)
    data = deepcopy(app.result_data)
    rows = deepcopy(app.segments)
    changed_text, assignments = [], []
    for index,(row,edited) in enumerate(zip(rows,edited_rows)):
        if "caption_timing" in edited:row["caption_timing"] = dict(edited["caption_timing"])
        else:row.pop("caption_timing",None)
        text = str(edited["text"])
        if row["text"] != text:
            old = row["text"]
            if not (row.get("manual_text_owner")==row["segment_id"] and row.get("manual_text_last")==old):
                previous = next((r for r in reversed(data.get("context_review",[])) if r.get("origin")=="manual_memory"
                                 and r["segment_id"]==row["segment_id"] and r["after"]==old),None)
                if previous:row.update(manual_text_base=previous["before"],manual_text_last=old,manual_text_owner=row["segment_id"])
            prepare_edit(row,old,text)
            row.update(text=text,words=[]);changed_text.append(index)
            if data.get("mode")=="realtime" or row["segment_id"] in data.get("live_text_edits",{}):
                data.setdefault("live_text_edits",{})[row["segment_id"]] = {"text":text,"words":[]}
        speaker = edited.get("speaker","")
        if row.get("speaker","") != speaker or edited.get(TARGET):
            assignments.append((index,row.get("speaker","")))
            row.update(speaker=speaker,speaker_manual=True,speaker_review=False)
    # Discard unused identities created by intermediate dropdown choices.
    used = set(app.speakers) | {row.get("speaker") for row in rows}
    names = normalize_names(rows,{key:value for key,value in preview.result.get("speakers",{}).items() if key in used})
    if links:data.setdefault("speaker_memory_links",{}).update(links)
    changed_names = [(key,value) for key,value in names.items() if value != app.speakers.get(key)]
    for key,value in changed_names:data.setdefault("speaker_name_edits",{})[key] = value
    data.update(segments=rows,speakers=names,video_source=preview.result["video_source"],video_caption_layout=layout)
    data["video_export_mode"] = preview.result.get("video_export_mode", "source_video")

    # Keep the app and the learning stores untouched if the result cannot be saved.
    write_json(preview.folder/"edited.json",data)
    app.result_data=data;app.segments=rows;app.speakers=names
    if app.selected is not None:
        app.editor.configure(state="normal");app.editor.delete("1.0","end")
        app.editor.insert("1.0",rows[int(app.selected)]["text"]);app.editor.edit_reset()
    app.refresh_speaker_rows()
    app.persist_live_speaker_edits()

    # Learn only the final saved edits, never intermediate preview changes or undo.
    count=0;errors=[]
    for index in changed_text:
        row=rows[index]
        try:count+=learn_edit(row,row["text"],row["text"])
        except (OSError,ValueError) as exc:errors.append(str(exc))
    if changed_text:
        app.manual_memory_status.set(f"プレビューの手動修正を{count}か所覚えました。" if not errors else
                                    "文章は保存済みです。手動修正の記憶は再試行が必要です："+errors[0])
        app.memory_status.set(app.manual_memory_status.get())
    assigned={rows[index].get("speaker") for index,_ in assignments}
    for index,previous in assignments:
        try:app.learn_corrected_speaker(rows[index],previous)
        except Exception as exc:app.memory_status.set(f"話者は保存済みです。声の記憶を更新できませんでした：{exc}")
    for key,name in changed_names:
        if key in assigned:continue
        try:app.remember_named_speaker(key,name)
        except Exception as exc:app.memory_status.set(f"話者名は保存済みです。声の記憶を更新できませんでした：{exc}")
    app.persist_edits()
    if app.context_window and app.context_window.winfo_exists():app.context_window.refresh()
