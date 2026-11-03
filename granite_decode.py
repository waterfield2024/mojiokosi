"""Translate CTC emissions to timestamped text without inventing token timings."""
import re


def timed_tokens(ids, tokenizer, duration, frame_seconds=.08, blank=0):
    groups = []
    for frame, token in enumerate(ids):
        if groups and groups[-1][0] == token:groups[-1][2] = frame+1
        else:groups.append([token,frame,frame+1])
    groups = [group for group in groups if group[0] != blank]
    prefix, text, words, pending = [], "", [], None
    for token, first, last in groups:
        prefix.append(token)
        if pending is None:pending = first
        decoded = tokenizer.decode(prefix,group_tokens=False,skip_special_tokens=True,clean_up_tokenization_spaces=False)
        if decoded.endswith("\ufffd"):continue  # wait for the rest of a UTF-8 byte sequence
        if not decoded.startswith(text):
            # A tokenizer may revise previous characters: retain the text, never
            # manufacture character boundaries for a non-prefix-preserving decode.
            final = tokenizer.decode([g[0] for g in groups],group_tokens=False,skip_special_tokens=True,
                                     clean_up_tokenization_spaces=False)
            return final.strip(), []
        piece = decoded[len(text):]
        if piece:
            start = min(duration,max(0,pending*frame_seconds))
            end = min(duration,max(start,last*frame_seconds))
            if end > start:words.append({"word":piece,"start":round(start,3),"end":round(end,3)})
            text = decoded;pending = None
    final = tokenizer.decode([g[0] for g in groups],group_tokens=False,skip_special_tokens=True,
                             clean_up_tokenization_spaces=False).strip()
    if not any(character.isalnum() for character in final):return "", []
    if "".join(word["word"] for word in words).strip() != final:return final, []
    return final, words


def segment_words(words):
    """Break on sentences, pauses or bounded spans; keep original CTC evidence."""
    segments, current = [], []
    def flush():
        if current:
            text = "".join(word["word"] for word in current).strip()
            if any(char.isalnum() for char in text):
                segments.append({"start":current[0]["start"],"end":current[-1]["end"],
                                 "text":text,"words":list(current)})
            current.clear()
    for word in words:
        if current and (word["start"]-current[-1]["end"]>.65 or word["end"]-current[0]["start"]>6):flush()
        current.append(word)
        if re.search(r"[。！？!?]|[.]\s*$",word["word"]):flush()
    flush()
    return segments
