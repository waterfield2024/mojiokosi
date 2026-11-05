"""Review the original and separated transcript, undo, and retry after recording."""
from copy import deepcopy
import tkinter as tk
from tkinter import ttk, messagebox


def refresh(app, before=(), after=()):
    selected = app.segments[int(app.selected)].get('segment_id') if app.selected is not None and int(app.selected) < len(app.segments) else None
    app.selected = None
    app.segments = app.result_data['segments']
    app.speakers = app.result_data.setdefault('speakers', {})
    children = app.table.get_children()
    if children: app.table.delete(*children)
    for index, row in enumerate(app.segments):
        app.table.insert('', 'end', iid=str(index), values=app.row_values(row))
    app.editor.configure(state='normal'); app.editor.delete('1.0', 'end')
    app.refresh_speaker_rows(); app.refresh_history(); app.update_speaker_controls()
    if selected:
        index = next((i for i, r in enumerate(app.segments) if r.get('segment_id') == selected), None)
        if index is not None: app.table.selection_set(str(index)); app.select()
    if app.overlay:
        app.overlay.set_speakers(app.speakers)
        app.overlay.replace_segments(before, after)


def receive_patch(app, record):
    if not app.result_data: return
    from overlap import apply_reply
    app.commit()
    app.result_data['segments'] = app.segments
    app.result_data['speakers'] = app.speakers
    history = app.result_data.setdefault('overlap_review', [])
    if any(r['id'] == record['id'] for r in history): return
    if record['status'] != 'applied':
        history.append(deepcopy(record)); return
    candidate = deepcopy(record)
    candidate['status'] = 'pending'
    history.append(candidate)
    patch = apply_reply(app.result_data, {'id': record['id'], 'after': record['after'],
                                        'names': record.get('names', {}), 'seconds': record.get('seconds', 0)})
    if patch and patch['status'] == 'applied':
        refresh(app, patch['before'], patch['after'])
    else:
        app.result_data.setdefault('live_overlap_states', {})[record['id']] = 'reverted'
        app.persist_live_speaker_edits()


def open_review(app):
    if not app.result_data:
        messagebox.showinfo('声の重なり', '文字起こし結果を開いてから確認してください。', parent=app.root)
        return
    existing = getattr(app, 'overlap_review_window', None)
    if existing and existing.winfo_exists(): existing.lift(); return
    app.commit()
    window = tk.Toplevel(app.root)
    app.overlap_review_window = window
    window.title('声の重なり・補正前後の確認')
    window.geometry('850x620'); window.minsize(600, 440)
    app.window_settings.attach(window, 'overlap_review')
    def close():
        app.window_settings.remember('overlap_review', window)
        window.destroy()
    window.protocol('WM_DELETE_WINDOW', close)
    frame = ttk.Frame(window, padding=16); frame.pack(fill='both', expand=True)
    ttk.Label(frame, text='声の重なり', style='Title.TLabel').pack(anchor='w')
    ttk.Label(frame, text='2人の同時発話を後から分離します。元の録音は変更しません。\n分離が不十分な区間・3人以上の重なりは、元の字幕を残します。', wraplength=780).pack(anchor='w', pady=(8, 12))
    tree = ttk.Treeview(frame, columns=('time', 'state'), show='headings', height=6, selectmode='browse')
    tree.heading('time', text='重なった区間'); tree.heading('state', text='結果')
    tree.column('time', width=160, stretch=False); tree.column('state', width=570)
    tree.pack(fill='x')
    from ui_theme import FONT
    content = tk.Text(frame, wrap='word', height=12, state='disabled', font=(FONT, 10), spacing1=3, spacing3=3)
    content.pack(fill='both', expand=True, pady=10)
    controls = ttk.Frame(frame); controls.pack(fill='x')
    status = tk.StringVar()
    ttk.Label(frame, textvariable=status, wraplength=780).pack(anchor='w', pady=(8,0))
    bound_data = app.result_data
    def records(): return bound_data.get('overlap_review', [])
    def selected():
        ids = tree.selection()
        return next((r for r in records() if ids and r['id'] == ids[0]), None)
    def update(event=None):
        record = selected()
        from speakers import segment_text
        text = '重なりが検出されると、ここに補正前後が表示されます。'
        if record:
            text = '補正前\n' + '\n'.join(segment_text(r, bound_data.get('speakers')) for r in record['before'])
            text += '\n\n分離後\n' + ('\n'.join(segment_text(r, bound_data.get('speakers')) for r in record.get('after', [])) or record.get('message', '処理待ち'))
        content.configure(state='normal'); content.delete('1.0','end'); content.insert('1.0', text); content.configure(state='disabled')
        undo.configure(state='normal' if record and record['status'] in {'applied','reverted'} and record.get('after') else 'disabled',
                       text='分離結果を再適用' if record and record['status'] == 'reverted' else 'この区間を元に戻す')
    def toggle():
        record = selected()
        if not record or app.result_data is not bound_data: return
        from overlap import set_state
        try:
            app.commit(); bound_data['segments'] = app.segments
            state = 'reverted' if record['status'] == 'applied' else 'applied'
            if set_state(bound_data, record, state):
                bound_data.setdefault('live_overlap_states', {})[record['id']] = state
                before, after = (record['after'], record['before']) if state == 'reverted' else (record['before'], record['after'])
                refresh(app, before, after); app.persist_edits(); update()
        except (OSError, ValueError) as exc: status.set(str(exc))
    def retry():
        if app.proc:
            status.set('録音・文字起こしが完了してから再解析してください。'); return
        try:
            app.persist_edits()
            from app import COMPUTE_OPTIONS
            app.launch_worker({'operation': 'overlap', 'result': deepcopy(app.result_data),
                'result_folder': str(app.result_path.parent), 'compute': COMPUTE_OPTIONS[app.compute.get()]})
            close()
        except Exception as exc: status.set(str(exc))
    undo = ttk.Button(controls, text='この区間を元に戻す', command=toggle); undo.pack(side='left')
    ttk.Button(controls, text='未処理の重なりを再解析', command=retry).pack(side='left', padx=8)
    ttk.Button(controls, text='閉じる', command=close).pack(side='right')
    tree.bind('<<TreeviewSelect>>', update)
    def tick():
        if not window.winfo_exists(): return
        if app.result_data is not bound_data: close(); return
        for record in records():
            values = (f"{record['start']:.1f}〜{record['end']:.1f}秒", {'pending':'処理待ち','applied':'分離して補正済み','reverted':'元の字幕に戻しました'}.get(record['status'], record.get('message', '未処理')))
            if tree.exists(record['id']): tree.item(record['id'], values=values)
            else: tree.insert('', 'end', iid=record['id'], values=values)
        if not tree.selection() and tree.get_children(): tree.selection_set(tree.get_children()[0]); update()
        window.after(500, tick)
    from ui_theme import style_text
    style_text(content)
    update(); tick()
