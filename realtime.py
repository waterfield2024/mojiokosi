"""Record PC playback, publish live captions, and save an editable result."""
from __future__ import annotations
from datetime import datetime
from collections import deque
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import queue
import sys
import time
import uuid
from realtime_audio import LoopbackCapture, RATE
from realtime_asr import LiveStopped
from live_speakers import LiveSpeakerProcess, apply_live_edits, speaker_ranges
from recognition_models import ALLOWED_MODELS, GRANITE_MODEL, HAAS_MODEL, recognition_language, live_recognizer_type
from user_data import data_path
from speech_gate import SpeechGate, overlaps_speech, keep_spoken_rows
from speech_preferences import profile as speech_profile, read_control as speech_control
from live_prompt import WHISPER_MODELS, make_prompt, reliable, transcribe_with_prompt
from utterance_audio import AudioChunk, mode as segmentation_mode, pause_duration, read_pause_control
from utterance_audio import utterance_limit, read_limit_control, MAX_SECONDS

def next_live_chunk(chunks, pending, max_samples):
    """Catch up by joining queued audio, never dropping unrecognized speech."""
    chunk = pending.popleft() if pending else chunks.get(timeout=.1)
    if chunk is None:
        return None
    if isinstance(chunk, AudioChunk):
        # A newer snapshot (including its final result) supersedes the draft of
        # the SAME utterance. Never merge or discard distinct final utterances.
        while not chunk.final:
            try:
                following = chunks.get_nowait()
            except queue.Empty:
                break
            if not isinstance(following, AudioChunk) or following.utterance != chunk.utterance:
                pending.append(following)
                break
            chunk = following
        return chunk
    a, b, core = chunk
    while True:
        try:
            following = chunks.get_nowait()
        except queue.Empty:
            break
        if following is None or isinstance(following, AudioChunk) or following[1] - a > max_samples:
            pending.append(following)
            break
        b = following[1]
    return a, b, core

def recognize_parts(recognizer, pcm, a, b, core, previous, ranges, prefetched=None, speech=None, sensitivity='balanced', context_prompt=None):
    """Reuse concurrent recognition for one voice; retain exact cuts for turn changes."""
    result = prefetched.result() if prefetched is not None else None
    rows = []
    for part in ranges:
        pa, pb = max(a, round(part["start"] * RATE)), min(b, round(part["end"] * RATE))
        if pb <= core:
            continue
        if speech is not None and not overlaps_speech(speech, max(core, pa), pb, sensitivity):
            continue
        if result is None or pa != a or pb != b:
            hint = context_prompt(max(core, pa), pb, previous + rows) if context_prompt else None
            current_result = transcribe_with_prompt(recognizer, pcm[(pa - a) * 2:(pb - a) * 2], hint)
        else:
            current_result = result
        current = live_rows(current_result, pa, pb, max(core, pa), previous + rows)
        if speech is not None:
            current = keep_spoken_rows(current, speech, sensitivity)
        for row in current:
            row.update(speaker=part["speaker"], speaker_review=part.get("speaker_review", False))
        rows.extend(current)
        yield current, part

def live_rows(result, start, end, core, previous):
    """Convert relative word times and remove only the already consumed overlap."""
    rows = []
    duration = (end - start) / RATE
    for raw in result.get("segments", []):
        if float(raw.get("no_speech_prob", 0)) > .8 and float(raw.get("avg_logprob", 0)) < -1:
            continue
        text = str(raw.get("text", "")).strip()
        if not text:
            continue
        a = max(0, min(duration, float(raw.get("start", 0))))
        b = max(a, min(duration, float(raw.get("end", duration))))
        offset = start / RATE
        words = []
        original_words = raw.get("words", [])
        valid_times = bool(original_words) and all(
            0 <= float(w.get("start", -1)) <= float(w.get("end", -1)) <= duration + .15
            for w in original_words)
        # Short windows can yield collapsed word timing from Whisper. Preserve text
        # using segment timing instead of pretending these are precise word boundaries.
        if original_words and sum(float(w.get("end", 0)) - float(w.get("start", 0)) < .005 for w in original_words) > max(2, len(original_words) // 3):
            valid_times = False
        if valid_times and "".join(w.get("word", "") for w in original_words).strip() == text:
            for w in original_words:
                wa = offset + max(0, min(duration, float(w.get("start", a))))
                wb = offset + max(0, min(duration, float(w.get("end", b))))
                if (wa + wb) / 2 < core / RATE:
                    continue
                words.append({"word": w["word"], "start": max(core / RATE, wa), "end": max(core / RATE, wa, wb)})
            if not words:
                continue
            text = "".join(w["word"] for w in words).strip()
            a, b = words[0]["start"], words[-1]["end"]
        else:
            a, b = a + offset, b + offset
            if b <= core / RATE:
                continue
            a = max(a, core / RATE)
        # Short-window word timing can drift across the overlap boundary even when
        # structurally valid. Remove an exact repeated prefix only at that boundary.
        if start < core and previous and a <= core / RATE + .35:
            old = previous[-1].get("live_asr_text", previous[-1]["text"]).rstrip("。、.!?！？ \t\n")
            for n in range(min(len(old), len(text), 80), 1, -1):
                if old.endswith(text[:n]):
                    text = text[n:].lstrip()
                    words = []  # A removed prefix may cut through a tokenizer word.
                    break
        if not text or b < a:
            continue
        row = {"id": uuid.uuid4().hex, "start": round(a, 3), "end": round(max(a, b), 3),
               "text": text, "words": words, "speaker": "", "speaker_review": True}
        row['live_prompt_eligible'] = reliable(raw)
        row["segment_id"] = row["id"]
        rows.append(row)
    return rows

def run_live(request, job, models=None):
    from engine import ROOT, RESULTS, emit, save_result, cache_audio
    from context_ai import write_json, correct_result
    from speakers import diarize, assign_speakers, normalize_names, default_name
    from corrections import normalize_rules, replace_segments
    job = Path(job)
    stop_path = job / "stop-live"
    stopped = (lambda: stop_path.exists() or models.closing()) if models else stop_path.exists
    model = request.get("model", "base")
    if model not in ALLOWED_MODELS or request.get("language", "ja") not in {"ja", "en", "auto"}:
        raise ValueError("認識設定が正しくありません。")
    seconds = max(2, min(10, int(request.get("chunk_seconds", 2))))
    segmentation = segmentation_mode(request.get('segmentation', 'utterance'))
    pause_seconds = pause_duration(request.get('pause_seconds', .7))
    max_utterance_seconds = utterance_limit(request.get('max_utterance_seconds', MAX_SECONDS))
    sensitivity = speech_profile(request.get('speech_sensitivity', 'balanced')).key
    use_previous = request.get('live_previous_text', True)
    if type(use_previous) is not bool:
        raise ValueError('前の文章を参考に認識する設定はオンかオフを選んでください。')
    use_previous = use_previous and model in WHISPER_MODELS
    prompt = str(request.get("prompt", ""))[:1000]
    rules = normalize_rules(request.get("correction_rules", []))
    if rules:
        prompt = (prompt + " " + "、".join(r["after"] for r in rules))[:1400]
    title = datetime.now().strftime("リアルタイム_%Y%m%d_%H%M%S")
    folder = RESULTS / (title + "_" + job.name[:6])
    folder.mkdir(parents=True, exist_ok=True)
    audio_dir = folder / ".audio"
    audio_dir.mkdir(exist_ok=True)
    wav = audio_dir / "recording.wav"
    data = {"title": title, "mode": "realtime", "source": str(wav), "audio_file": str(wav),
            "model": model, "language": recognition_language(model, request.get("language", "ja")), "prompt": prompt,
            "segments": [], "speakers": {}, "live_incomplete": True, "recording_complete": False,
            "recording_device": request["device"]["name"]}
    data['live_previous_text'] = use_previous
    data['segmentation'] = segmentation
    data['pause_seconds'] = pause_seconds
    data['max_utterance_seconds'] = max_utterance_seconds
    # A checkpoint exists even if capture or the engine later fails.
    write_json(folder / "result.json", data)
    emit("checkpoint", result_path=str(folder / "result.json"))
    recognizer = capture = live_diarizer = recorder = None
    recording_mode = request.get("recording_mode", "audio")
    if recording_mode not in {"audio", "screen"}:
        raise ValueError("録音・録画の選択が正しくありません。")
    data["recording_mode"] = recording_mode
    if recording_mode == "screen":
        data["screen_recording"] = {"complete": False, "monitor": request.get("screen_monitor", {})}
    live_speakers = request.get("live_diarize", True)
    live_turns = []
    chunks = queue.Queue()
    pending = deque()
    inference = None
    error = None
    memory_warning = ""
    correction_memory_warning = ""
    proofreader = None
    context_session = uuid.uuid4().hex
    context_seen = set()
    context_state = ""
    slow_chunks = 0
    from live_noise import LiveNoiseReducer, update_control
    from reuse_assets import REPO, REVISION
    noise = models.noise if models else LiveNoiseReducer(job, stopped)
    speech_gate = models.speech if models else SpeechGate(ROOT / 'models')
    data['speech_filter'] = {'engine': 'silero-v5/cpu', 'skipped_chunks': 0}
    speech_warning = ''
    speech_applied = None
    data['noise_reduction'] = {'model': REPO, 'revision': REVISION, 'recording_audio': 'original',
                               'applied_chunks': 0, 'bypassed_chunks': 0}
    noise_state = None

    def service_speech():
        nonlocal sensitivity, speech_applied, pause_seconds, max_utterance_seconds
        pause_seconds = read_pause_control(job / 'pause-control.json', pause_seconds)
        data['pause_seconds'] = pause_seconds
        max_utterance_seconds = read_limit_control(job / 'utterance-control.json', max_utterance_seconds)
        data['max_utterance_seconds'] = max_utterance_seconds
        sensitivity = speech_control(job / 'speech-control.json', sensitivity)
        if sensitivity != speech_applied:
            speech_gate.configure(sensitivity)
            # Normal Stop still drains recorded chunks; only initial preparation
            # or application exit should cancel a detector change mid-session.
            cancelled = stopped if capture is None else (models.closing if models else lambda: False)
            speech_gate.prepare(lambda text: emit('status', text=text), cancelled)
            if capture:
                capture.set_sensitivity(sensitivity)
            data['speech_filter']['sensitivity'] = sensitivity
            speech_applied = sensitivity
            emit('live_speech_status', sensitivity=sensitivity)

    def service_noise():
        nonlocal noise_state
        update_control(noise, job)
        noise.pump()
        state = noise.snapshot()
        data['noise_reduction'].update(requested=state['enabled'], state=state['state'], message=state['text'])
        if state != noise_state:
            emit('live_noise_status', **state)
            noise_state = state

    def service_context(submit=True):
        nonlocal context_state
        if not proofreader:
            return
        from live_context import snapshot, apply_changes
        for reply in proofreader.poll():
            if reply.get("session") != context_session:
                continue
            apply_live_edits(data, folder)
            try:
                records = apply_changes(data, reply["changes"])
            except (OSError, ValueError):
                proofreader.failed = True
                proofreader.state = "覚えた誤修正を読み込めないため自動修正は休止中。録音・字幕は続きます。"
                break
            if records:
                write_json(folder / "result.json", data)
                emit("live_corrections", records=records)
        if proofreader.failed and proofreader.proc.poll() is None:
            proofreader.close()
        if proofreader.state != context_state:
            context_state = proofreader.state
            emit("live_context_status", text=context_state)
        if (submit and not stopped() and capture and not capture.halt.is_set()
                and data["segments"] and capture.total / RATE - data["segments"][-1]["end"] < 6):
            value = snapshot(data, context_seen)
            if value and proofreader.submit(value, context_session):
                context_seen.update(r["segment_id"] for r in value["targets"])
    try:
        status = lambda text: emit("status", text=text)
        if models:
            recognizer = models.recognizer_for(request, data["language"], prompt, status, stopped)
        else:
            recognizer_type = live_recognizer_type(model)
            recognizer = recognizer_type(model, data["language"], prompt, request.get("compute", "auto"), job,
                                        status, stopped)
        noise.prepare(request.get('live_noise', False), request.get('compute', 'auto'), status, stopped)
        service_speech()
        service_noise()
        if live_speakers:
            emit("status", text="リアルタイム話者判別を準備中…")
            try:
                if models:
                    live_diarizer = models.speakers_for(request, status, stopped, reset=True)
                else:
                    live_diarizer = LiveSpeakerProcess(ROOT / "models" / "speakers", request.get("num_speakers", 0), job, stopped,
                        request.get("speaker_memory_file") or data_path("speaker_memory.json", "WHISPER_SPEAKER_MEMORY"))
            except LiveStopped:
                raise
            except Exception as exc:
                data["speaker_error"] = str(exc)
                emit("warning", title="リアルタイム話者判別", message="話者判別を開始できません。話者不明として録音・文字起こしを続けます。\n" + str(exc))
        if request.get("live_correct", False):
            if models:
                proofreader = models.proofreader_for(request)
            else:
                try:
                    from live_context import LiveProofreader
                    proofreader = LiveProofreader(request.get("compute", "auto"), job)
                except Exception:
                    pass
            if not proofreader:
                emit("live_context_status", text="字幕の自動修正を準備できません。録音・字幕は続きます。")
        if recording_mode == "screen":
            from screen_capture import ScreenRecorder, validate_target
            selected = validate_target(request.get("screen_monitor") or {})
            data["screen_recording"].update(target=selected, kind=selected.get("kind", "screen"))
            emit("live_recording_status", text="選んだ対象の録画を準備しています…")
            recorder = ScreenRecorder(selected, folder, stopped, lambda text: emit("live_recording_status", text=text))
        capture = LoopbackCapture(request["device"], wav, chunks, stopped, seconds,
                                   lambda **values: emit("live_meter", **values))
        capture.set_sensitivity(sensitivity, job / 'speech-control.json')
        capture.set_segmentation(segmentation, pause_seconds=pause_seconds, control_path=job / 'pause-control.json',
                                 max_utterance_seconds=max_utterance_seconds, max_control_path=job / 'utterance-control.json')
        capture.start()
        inference = ThreadPoolExecutor(max_workers=1, thread_name_prefix="live-asr")
        deadline = time.monotonic() + 15
        while not capture.ready.wait(.1):
            if stopped():
                raise LiveStopped()
            if capture.done.is_set():
                raise RuntimeError(capture.error or "音声を取り込めませんでした。")
            if time.monotonic() > deadline:
                raise LiveStopped()
        data["device"] = recognizer.device
        if recorder:
            recorder.start(capture)
            data["screen_recording"].update(width=recorder.details["width"],height=recorder.details["height"],fps=recorder.details["fps"])
            emit("live_recording_status", text=f"録画中：{selected['label'][:65]}。停止後に字幕を付けてMP4保存できます。")
        write_json(folder / "result.json", data)
        emit("live_ready", result_path=str(folder / "result.json"), device=recognizer.device,
             device_name=request["device"]["name"], recording_mode=recording_mode, model_processes=models.info() if models else {})
        start_time = time.monotonic()
        # Optional bounded sessions are also useful for automated end-to-end validation.
        limit = float(request.get("max_seconds", 0))
        while True:
            service_noise()
            service_speech()
            service_context()
            if limit and time.monotonic() - start_time >= limit:
                capture.stop()
            try:
                chunk = next_live_chunk(chunks, pending, max(8, seconds) * RATE)
            except queue.Empty:
                continue
            if chunk is None:
                if capture.error:
                    error = capture.error
                break
            a, b, core = chunk
            utterance = chunk.utterance if isinstance(chunk, AudioChunk) else None
            provisional = isinstance(chunk, AudioChunk) and not chunk.final
            processing_started = time.monotonic()
            with wav.open("rb") as file:
                file.seek(44 + a * 2)
                pcm = file.read((b - a) * 2)
            # Only this in-memory ASR copy is enhanced. The recording WAV remains
            # the source for playback, video export, voice profiles and diarization.
            pcm, noise_applied = noise.process(pcm)
            data['noise_reduction']['applied_chunks' if noise_applied else 'bypassed_chunks'] += 1
            service_noise()
            apply_live_edits(data, folder)
            speech = [(a+x, a+y) for x, y in speech_gate.intervals(pcm)]
            if speech_gate.warning != speech_warning:
                speech_warning = speech_gate.warning
                data['speech_filter']['warning'] = speech_warning
                if speech_warning:
                    emit('warning', title='発話判定', message=speech_warning)
            # Never ask the ASR to invent text for silence/noise, including an
            # almost-silent result from Nemotron or speech only in old context.
            if not overlaps_speech(speech, max(a, core), b, sensitivity):
                data['speech_filter']['skipped_chunks'] += 1
                data['duration'] = capture.total / RATE
                write_json(folder / 'result.json', data)
                emit('status', text='発話待ち…（無音・雑音の区間は字幕にしません）')
                if utterance is not None:
                    emit('live_preview', utterance=utterance, segments=[], speakers={})
                continue
            def context_prompt(start, end, history):
                # Use audio time, not processing time: delayed chunks must keep
                # their original context and long pauses must break it.
                voice_start = min((max(start, x) for x, y in speech if y > start and x < end), default=start)
                return make_prompt(prompt, history, voice_start / RATE)
            prompt_for = context_prompt if use_previous else None
            previous = list(data['segments'])
            hint = prompt_for(max(a, core), b, previous) if prompt_for and live_diarizer else None
            # ASR runs in a separate native process and can overlap the CPU speaker model.
            prefetched = inference.submit(transcribe_with_prompt, recognizer, pcm, hint) if live_diarizer else None
            turns = []
            speaker_data = ({'speakers': dict(data['speakers']),
                             'remembered_speakers': dict(data.get('remembered_speakers', {})),
                             'speaker_name_edits': data.get('speaker_name_edits', {})} if provisional else data)
            if live_diarizer:
                emit("status", text="再生中の声を判別しています…")
                # Reuse a bounded recent window; never reprocess the whole recording.
                context_start = max(0, min(a, b - 8 * RATE))
                try:
                    turns = live_diarizer.process(wav, context_start, b, speaker_data['speakers'], preview=provisional)
                    from speaker_memory import PROFILE_FILE
                    if not provisional:
                        write_json(folder / PROFILE_FILE, {"version": 1, "profiles": live_diarizer.profiles})
                    manual_names = speaker_data.get("speaker_name_edits", {})
                    for speaker in speaker_data.get("remembered_speakers", {}):
                        if speaker not in manual_names:
                            speaker_data['speakers'][speaker] = default_name(speaker)
                    speaker_data['remembered_speakers'] = live_diarizer.remembered
                    for speaker, remembered in live_diarizer.remembered.items():
                        if speaker not in manual_names:
                            speaker_data['speakers'][speaker] = remembered['name']
                    if live_diarizer.memory_error and live_diarizer.memory_error != memory_warning:
                        memory_warning = live_diarizer.memory_error
                        emit("warning", title="覚えた話者", message=memory_warning + "\n通常の話者判別で続けます。")
                except Exception as exc:
                    data["speaker_error"] = str(exc)
                    live_diarizer.close()
                    live_diarizer = None
                    emit("warning", title="リアルタイム話者判別", message="話者解析を継続できないため、話者不明で録音を続けます。\n" + str(exc))
            ranges = (speaker_ranges(turns, a / RATE, b / RATE) if live_speakers else
                      [{"start": a / RATE, "end": b / RATE, "speaker": "", "speaker_review": False}])
            lag = max(0, capture.total / RATE - b / RATE)
            completed_rows, completed_records = [], []
            for rows, part in recognize_parts(recognizer, pcm, a, b, core, previous, ranges, prefetched, speech, sensitivity, prompt_for):
                for row in rows:
                    row['noise_reduced'] = noise_applied
                completed_rows.extend(rows)
                if provisional:
                    continue
                if live_speakers:
                    live_turns.append({**part, "start": max(core / RATE, part["start"])})
                data["segments"].extend(rows)
                data["speakers"] = normalize_names(data["segments"], data["speakers"])
                if live_speakers:
                    data["diarization"] = {"engine": "sherpa-onnx/streaming", "turns": live_turns}
                apply_live_edits(data, folder)
                try:
                    from manual_corrections import apply_to_result
                    learned_records = apply_to_result(data, rows, live=True)
                except (OSError, ValueError) as exc:
                    learned_records = []
                    if str(exc) != correction_memory_warning:
                        correction_memory_warning = str(exc)
                        emit("live_context_status", text="手動修正の記憶を読み込めないため、通常の字幕を続けます。")
                data["device"] = recognizer.device
                data["backend"] = getattr(recognizer,"backend","whisper.cpp/server/" + recognizer.device.lower())
                if model == GRANITE_MODEL:
                    data["timestamp_method"] = "ctc-emissions"
                elif model == HAAS_MODEL:
                    data["timestamp_method"] = "tdt-tokens-or-chunk"
                data["backend_fallbacks"] = recognizer.failures
                data["duration"] = capture.total / RATE
                write_json(folder / "result.json", data)
                lag = max(0, capture.total / RATE - b / RATE)
                completed_records.extend(learned_records)
                if utterance is None:
                    emit("live_segments", segments=rows, speakers=data["speakers"], remembered_speakers=data.get("remembered_speakers", {}),
                         learned_records=learned_records, device=recognizer.device, lag=round(lag, 1),
                         timing={"audio_end": b / RATE, "processing_seconds": round(time.monotonic() - processing_started, 3)})
                emit("status", text=f"PCの音声を文字起こし中 / {recognizer.device} / 認識待ち {lag:.1f}秒")
            if provisional:
                emit('live_preview', utterance=utterance, segments=completed_rows,
                     speakers=normalize_names(completed_rows, speaker_data['speakers']))
                continue
            if utterance is not None:
                emit('live_segments', utterance=utterance, segments=completed_rows, speakers=data['speakers'],
                     remembered_speakers=data.get('remembered_speakers', {}), learned_records=completed_records,
                     device=recognizer.device, lag=round(lag,1),
                     timing={'audio_end':b/RATE,'processing_seconds':round(time.monotonic()-processing_started,3)})
            slow_chunks = slow_chunks + 1 if lag > 1.2 else 0
            if proofreader and slow_chunks >= 2 and not proofreader.failed:
                proofreader.failed = True
                proofreader.state = "文字認識を優先して字幕の自動修正を休止中。"
                proofreader.close()
    except LiveStopped:
        pass
    except Exception as exc:
        error = str(exc)
    finally:
        if capture:
            capture.stop()
            capture.thread.join(timeout=8)
            if not capture.done.is_set():
                error = error or "録音の終了を確認できませんでした。"
            data["duration"] = capture.total / RATE
        if recorder:
            try:
                if recorder.monitor and capture and capture.done.is_set() and capture.total:
                    video = recorder.finish(wav, capture.total / RATE)
                    data["screen_recording"].update(complete=True,file=video.relative_to(folder).as_posix(),
                        frames=recorder.details.get("frames"),duration=capture.total / RATE,
                        stop_reason=recorder.details.get("stop_reason", "user_stop"))
                    data.update(video_source=str(video),video_export_mode="recorded_screen")
                else:
                    data["screen_recording"]["error"] = "画面録画は完了していません。"
            except Exception as exc:
                data["screen_recording"]["error"] = str(exc)
                emit("live_recording_status", text="録画を完了できませんでした。録音と文字起こしは保存します。")
            finally:
                recorder.close()
        if inference:
            inference.shutdown(wait=True, cancel_futures=True)
        if not models:
            noise.close()
            speech_gate.close()
        service_context(submit=False)
        if proofreader and (not models or request.get("context_correct", False)):
            proofreader.close()
        if recognizer and not models:
            recognizer.close()
        if live_diarizer and not models:
            live_diarizer.close()
        if models:
            models.park()
        data['pause_seconds'] = read_pause_control(job / 'pause-control.json', pause_seconds)
        data['max_utterance_seconds'] = read_limit_control(job / 'utterance-control.json', max_utterance_seconds)
        data["recording_complete"] = bool(capture and capture.done.is_set() and not capture.error and wav.is_file())
        if capture:
            data['duration'] = capture.total / RATE
        data["live_incomplete"] = bool(error)
        if error:
            data["live_error"] = error
        apply_live_edits(data, folder)
        save_result(data, folder)
    emit("live_stopped")
    if wav.is_file() and capture and capture.total:
        try:
            data["audio_file"] = cache_audio(wav, folder)
            # Keep the recoverable WAV as well. It is the explicit input for re-transcription.
        except Exception as exc:
            data["audio_warning"] = str(exc)
        save_result(data, folder)
    if not error and data["segments"] and request.get("diarize", True) and not live_speakers:
        try:
            emit("status", text="録音は停止しました。話者を判別しています…")
            turns = diarize(wav, ROOT / "models" / "speakers", request.get("num_speakers", 0),
                            lambda text: emit("status", text="録音停止済み / " + text))
            data["segments"] = assign_speakers(data["segments"], turns)
            data["speakers"] = normalize_names(data["segments"])
            data["diarization"] = {"engine": "sherpa-onnx", "turns": turns}
        except Exception as exc:
            data["speaker_error"] = str(exc)
        save_result(data, folder)
    if not error and data["segments"] and request.get("context_correct", False):
        try:
            emit("status", text="録音停止済み / 文脈AI校正を実行しています…")
            corrected = correct_result(data, rules, lambda text: emit("status", text="録音停止済み / " + text))
            if corrected["context_last_run"]["changed_rows"]:
                write_json(folder / "before_context.json", data)
            data = corrected
        except Exception as exc:
            data["context_error"] = str(exc)
        save_result(data, folder)
    elif rules and data["segments"] and not error:
        corrected, count, rows = replace_segments(data["segments"], rules)
        if rows:
            write_json(folder / "before_corrections.json", data)
        data["segments"], data["correction_count"] = corrected, count
        save_result(data, folder)
    apply_live_edits(data, folder)
    save_result(data, folder)
    emit("complete", result_path=str(folder / "result.json"))
    if data.get("screen_recording", {}).get("error"):
        emit("warning", title="画面録画", message=data["screen_recording"]["error"] + "\n\n録音と文字起こしは保存しました。音声と字幕のMP4として保存できます。")
    if error:
        emit("warning", title="リアルタイム文字起こし", message=error + "\n\n途中までの結果と録音を保存しました。")

if __name__ == "__main__":
    if "--list-devices" in sys.argv:
        try:
            from realtime_audio import devices
            print(json.dumps({"devices": devices()}, ensure_ascii=False))
        except Exception as exc:
            print(json.dumps({"error": str(exc)}, ensure_ascii=False))
            raise SystemExit(1)
