"""Boundary engine. The LLM (or local scorer) points at WORD INDICES; this module turns them into timestamps from real word timings and
snaps to complete thoughts. Semantic completeness beats target length (but never runs away: MAX_S caps what completeness may add).

words: [{"w": str, "s": float, "e": float}, ...]  (word-level timestamps from the transcriber; timings may overlap when speakers interrupt)
"""
import re

END = re.compile(r"[.?!…][\"')\]]*$")
QUESTION = re.compile(r"\?[\"')\]]*$")
# a sentence starting with these leans on the previous one -> pull the setup in
DEPENDENT = {"and", "but", "so", "because", "then", "which", "that", "it", "he", "she", "they", "this",
             "those", "these", "also", "or", "cause", "and,", "yet", "however"}
REACTION = re.compile(r"^(wow|oh|omg|no|what|yes|yeah|haha+|lol|dude|bro|insane|crazy|nice|let'?s|go|right|exactly|sure)\b", re.I)

PAD_IN, PAD_OUT = 0.15, 0.40      # seconds of breathing room, never overlapping neighbouring words
SOFT_MIN_S = 8.0                  # a complete thought this long is a fine clip
MIN_S, MAX_S = 12.0, 90.0         # soft limits; completeness wins over MIN, MAX_S trims only at sentence ends
HARD_MAX_S = 150.0                # completeness may exceed MAX_S up to here, never beyond
MAX_SETUP_SENT, MAX_SETUP_S = 2, 18.0
ANSWER_WINDOW_S = 8.0             # how long after a question we still look for its answer
LAUGH_TAIL_S = 6.0                # keep a laughter/applause tail at most this long


def sentences(words, gap=0.8, max_len_s=25.0):
    """Inclusive (i0, i1) word ranges. Break on terminal punctuation or a long pause. A run without any punctuation (some ASR output)
    that exceeds max_len_s is split at its largest internal pauses, so one 'sentence' can never swallow a whole monologue."""
    out, a = [], 0
    for i, w in enumerate(words):
        last = i == len(words) - 1
        if last or END.search(w["w"]) or words[i + 1]["s"] - w["e"] >= gap:
            out.append((a, i))
            a = i + 1
    res = []
    for a, b in out:
        while words[b]["e"] - words[a]["s"] > max_len_s and b - a > 3:
            k = max(range(a, b), key=lambda i: words[i + 1]["s"] - words[i]["e"])  # biggest internal pause
            if words[k + 1]["s"] - words[k]["e"] < 0.15:  # no usable pause: leave it
                break
            res.append((a, k))
            a = k + 1
        res.append((a, b))
    return res


def _sent_of(sents, wi):
    for k, (a, b) in enumerate(sents):
        if a <= wi <= b:
            return k
    return len(sents) - 1


def _text(words, a, b):
    return " ".join(w["w"] for w in words[a:b + 1])


def _first(words, a):
    return words[a]["w"].lower().strip(".,!?\"'")


def refine(words, i0, i1, sents=None, setup=MAX_SETUP_SENT, min_s=MIN_S, max_s=MAX_S, reaction=None):
    """Core word range -> final clip with context. Returns dict with core/context/final word idx + times.
    reaction: optional per-second list (1/0 or score) of laughter/applause; a tail is kept while it continues (max LAUGH_TAIL_S)."""
    sents = sents or sentences(words)
    i0, i1 = max(0, min(i0, i1)), min(len(words) - 1, max(i0, i1))
    s0, s1 = _sent_of(sents, i0), _sent_of(sents, i1)
    c0, c1 = s0, s1
    # --- start: pull in setup while the opening sentence cannot stand alone
    for _ in range(setup):
        if c0 == 0:
            break
        a, b = sents[c0]
        prev_b = sents[c0 - 1][1]
        gap = words[a]["s"] - words[prev_b]["e"]
        # a dependent line right after a SHORT line is a reply/quip to it ("Don't run for president." / "But we're glad you did."): keep the
        # pair even across a long pause; otherwise only pull across a short one
        close = gap < 2.0 or (gap < 6.0 and sents[c0 - 1][1] - sents[c0 - 1][0] < 8)
        if (_first(words, a) in DEPENDENT or words[a]["w"][:1].islower()) and close and words[sents[s0][0]]["s"] - words[sents[c0 - 1][0]]["s"] <= MAX_SETUP_S:
            c0 -= 1
            pa = sents[c0][0]
            if not (_first(words, pa) in DEPENDENT or words[pa]["w"][:1].islower()):
                break  # the sentence we just pulled in stands on its own: the setup is complete (a chain of "And..." lines must not walk back through the document)
        else:
            break
    # --- end: finish a cut-off thought, then the answer to a question, then a short reaction/laughter tail
    last = len(sents) - 1
    while c1 < last and not END.search(words[sents[c1][1]]["w"]):
        c1 += 1
    for _ in range(2):  # a question wants its answer, even after a pause (never beyond ANSWER_WINDOW_S)
        if c1 < last and QUESTION.search(words[sents[c1][1]]["w"]):
            a, b = sents[c1 + 1]
            if words[a]["s"] - words[sents[c1][1]]["e"] <= ANSWER_WINDOW_S:
                c1 += 1
                while c1 < last and not END.search(words[sents[c1][1]]["w"]):
                    c1 += 1
    if c1 < last:
        a, b = sents[c1 + 1]
        if words[a]["s"] - words[sents[c1][1]]["e"] < 1.5 and (b - a < 4 or REACTION.match(_text(words, a, min(b, a + 2)))):
            c1 += 1
    # --- duration limits (completeness may exceed max_s up to HARD_MAX_S)
    def span(x, y):
        return words[sents[y][1]]["e"] - words[sents[x][0]]["s"]
    # Reach the minimum length by growing toward whichever neighbouring sentence is more EXCITING (exclamation/question/laughter words), not blindly
    # forward: a 6-second punchline should not be padded with the next 20 seconds of small talk. A complete thought of >= SOFT_MIN_S is accepted.
    def heat(k):
        return sum(1 for w in words[sents[k][0]:sents[k][1] + 1] if re.search(r"[!?]", w["w"]) or re.search(r"\b(ha(ha)+|lol|omg|wow|no way|insane|crazy|unbelievable|god)\b", w["w"], re.I)) / max(1, sents[k][1] - sents[k][0] + 1)
    while span(c0, c1) < min(min_s, SOFT_MIN_S) and (c1 < last or c0 > 0):
        fwd = heat(c1 + 1) if c1 < last else -1.0
        back = heat(c0 - 1) if c0 > 0 and words[sents[c0][0]]["s"] - words[sents[c0 - 1][1]]["e"] < 2.0 else -1.0
        if fwd < 0 and back < 0:
            break
        if fwd >= back:
            c1 += 1
        else:
            c0 -= 1
    while span(c0, c1) > max_s and c1 > s1 and span(c0, c1 - 1) >= min_s:  # trim only down to the core's own sentence end
        c1 -= 1
    while span(c0, c1) > HARD_MAX_S and c0 < s0:
        c0 += 1
    f0, f1 = sents[c0][0], sents[c1][1]
    clip = _pack(words, i0, i1, f0, f1)
    # --- laughter/applause tail: do not cut the audience reaction off
    if reaction:
        e = clip["final_end"]
        t, end_t = int(e), e
        while t < len(reaction) and t - e <= LAUGH_TAIL_S and reaction[t] and (t + 1 - clip["final_start"]) <= HARD_MAX_S:
            end_t = t + 1
            t += 1
        nxt = words[f1 + 1]["s"] if f1 + 1 < len(words) else float("inf")
        clip["final_end"] = round(min(max(e, end_t), nxt - 0.05 if nxt > e else e), 3)
    return clip


def _pack(words, i0, i1, f0, f1):
    prev_e = words[f0 - 1]["e"] if f0 > 0 else 0.0
    next_s = words[f1 + 1]["s"] if f1 + 1 < len(words) else words[f1]["e"] + PAD_OUT
    start = max(words[f0]["s"] - PAD_IN, min(words[f0]["s"], (prev_e + words[f0]["s"]) / 2) if f0 else 0.0)  # never clip the previous word
    end = min(words[f1]["e"] + PAD_OUT, (words[f1]["e"] + next_s) / 2 if next_s > words[f1]["e"] else words[f1]["e"] + PAD_OUT)
    c = {"core_words": [i0, i1], "final_words": [f0, f1],
         "core_start": words[i0]["s"], "core_end": words[i1]["e"],
         "final_start": round(max(0.0, start), 3), "final_end": round(max(end, words[f1]["e"] + 0.05), 3),
         "text": _text(words, f0, f1)}
    return c


def validate(words, clip, min_s=MIN_S, max_s=MAX_S, sents=None):
    """Boundary quality gate. Returns a list of issue codes; empty means pass."""
    f0, f1 = clip["final_words"]
    issues = []
    first, last = words[f0], words[f1]
    if f0 > 0 and not END.search(words[f0 - 1]["w"]) and first["s"] - words[f0 - 1]["e"] < 0.35:
        issues.append("starts_mid_sentence")
    if not END.search(last["w"]):
        issues.append("ends_mid_sentence")
    if QUESTION.search(last["w"]):
        issues.append("unanswered_question")
    if clip["final_start"] > first["s"] or clip["final_end"] < last["e"]:
        issues.append("truncates_word")
    d = clip["final_end"] - clip["final_start"]
    if d > HARD_MAX_S + 1:
        issues.append("too_long")
    if d < min_s - 4 and not (f1 >= len(words) - 1 and END.search(last["w"])):  # a complete thought at the very end of the media cannot be longer
        issues.append("too_short")
    gaps = [max(0.0, words[i + 1]["s"] - words[i]["e"]) for i in range(f0, f1)]
    if gaps and max(gaps) > 3.0:
        issues.append("dead_air")
    if f0 < len(words) and (_first(words, f0) in DEPENDENT or first["w"][:1].islower()) and f0 > 0 and first["s"] - words[f0 - 1]["e"] < 2.0:
        issues.append("missing_setup")
    return issues


WEIGHTS = {"starts_mid_sentence": .30, "ends_mid_sentence": .35, "truncates_word": .30, "unanswered_question": .20, "missing_setup": .20,
           "dead_air": .10, "too_long": .10, "too_short": .10}


def quality(issues):
    """Boundary-quality score in [0,1]: 1 = a complete, self-contained, tight clip."""
    return round(max(0.0, 1.0 - sum(WEIGHTS.get(i, 0.1) for i in issues)), 2)


def refine_checked(words, i0, i1, sents=None, rounds=3, reaction=None):
    """refine + validate; on failure widen the setup/tail allowance and retry."""
    sents = sents or sentences(words)
    clip = refine(words, i0, i1, sents, reaction=reaction)
    hard = ("starts_mid_sentence", "ends_mid_sentence", "truncates_word")
    for r in range(rounds):
        issues = validate(words, clip, sents=sents)
        if not [i for i in issues if i in hard]:
            break
        a, b = clip["final_words"]
        if "starts_mid_sentence" in issues and words[i0]["s"] - words[sents[max(0, _sent_of(sents, a) - 1)][0]]["s"] <= MAX_SETUP_S + 10:  # bounded: never walk back across the document
            a = sents[max(0, _sent_of(sents, a) - 1)][0]
        if "ends_mid_sentence" in issues:
            b = sents[min(len(sents) - 1, _sent_of(sents, b) + 1)][1]
        clip = _pack(words, i0, i1, a, b)
    clip["issues"] = validate(words, clip, sents=sents)
    clip["boundary_quality"] = quality(clip["issues"])
    return clip
