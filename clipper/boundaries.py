"""Boundary engine. The LLM (or local scorer) points at WORD INDICES; this module
turns them into timestamps from real word timings and snaps to complete thoughts.
Semantic completeness beats target length.

words: [{"w": str, "s": float, "e": float}, ...]  (word-level timestamps from the transcriber)
"""
import re

END = re.compile(r"[.?!…][\"')\]]*$")
# a sentence starting with these leans on the previous one -> pull the setup in
DEPENDENT = {"and", "but", "so", "because", "then", "which", "that", "it", "he", "she", "they", "this",
             "those", "these", "also", "or", "cause", "and,", "yet", "however"}
REACTION = re.compile(r"^(wow|oh|omg|no|what|yes|yeah|haha+|lol|dude|bro|insane|crazy|nice|let'?s|go)\b", re.I)

PAD_IN, PAD_OUT = 0.15, 0.40      # seconds of breathing room, never overlapping neighbouring words
MIN_S, MAX_S = 12.0, 90.0         # soft limits; completeness wins over MIN, MAX_S trims only at sentence ends
MAX_SETUP_SENT, MAX_SETUP_S = 3, 25.0


def sentences(words, gap=0.8):
    """Inclusive (i0, i1) word ranges. Break on terminal punctuation or a long pause."""
    out, a = [], 0
    for i, w in enumerate(words):
        last = i == len(words) - 1
        if last or END.search(w["w"]) or words[i + 1]["s"] - w["e"] >= gap:
            out.append((a, i))
            a = i + 1
    return out


def _sent_of(sents, wi):
    for k, (a, b) in enumerate(sents):
        if a <= wi <= b:
            return k
    return len(sents) - 1


def _text(words, a, b):
    return " ".join(w["w"] for w in words[a:b + 1])


def refine(words, i0, i1, sents=None, setup=MAX_SETUP_SENT, min_s=MIN_S, max_s=MAX_S):
    """Core word range -> final clip with context. Returns dict with core/context/final word idx + times."""
    sents = sents or sentences(words)
    i0, i1 = max(0, min(i0, i1)), min(len(words) - 1, max(i0, i1))
    s0, s1 = _sent_of(sents, i0), _sent_of(sents, i1)
    c0, c1 = s0, s1
    # --- start: pull in setup while the opening sentence cannot stand alone
    for _ in range(setup):
        if c0 == 0:
            break
        a, b = sents[c0]
        first = words[a]["w"].lower().strip(".,!?\"'")
        lower = words[a]["w"][:1].islower()
        prev_b = sents[c0 - 1][1]
        gap = words[a]["s"] - words[prev_b]["e"]
        # a dependent line right after a SHORT line is a reply/quip to it (e.g. "Don't run for president." / "But we're glad you did."): keep the pair
        # even across a long pause; otherwise only pull across a short one
        close = gap < 2.0 or (gap < 6.0 and sents[c0 - 1][1] - sents[c0 - 1][0] < 8)
        if (first in DEPENDENT or lower) and close and words[sents[s0][0]]["s"] - words[sents[c0 - 1][0]]["s"] <= MAX_SETUP_S:
            c0 -= 1
        else:
            break
    # --- end: finish a cut-off thought, then keep a short reaction/laughter tail
    while c1 < len(sents) - 1 and not END.search(words[sents[c1][1]]["w"]):
        c1 += 1
    if c1 < len(sents) - 1:
        a, b = sents[c1 + 1]
        near = words[a]["s"] - words[sents[c1][1]]["e"] < 1.5
        if near and (b - a < 4 or REACTION.match(_text(words, a, min(b, a + 2)))):
            c1 += 1
    # --- duration limits
    def span(x, y):
        return words[sents[y][1]]["e"] - words[sents[x][0]]["s"]
    while c1 < len(sents) - 1 and span(c0, c1) < min_s:
        c1 += 1
    while span(c0, c1) > max_s and c1 > s1:  # trim only down to the core's own sentence end
        c1 -= 1
    while span(c0, c1) > max_s and c0 < s0:
        c0 += 1
    f0, f1 = sents[c0][0], sents[c1][1]
    return _pack(words, i0, i1, f0, f1)


def _pack(words, i0, i1, f0, f1):
    prev_e = words[f0 - 1]["e"] if f0 > 0 else 0.0
    next_s = words[f1 + 1]["s"] if f1 + 1 < len(words) else words[f1]["e"] + PAD_OUT
    start = max(words[f0]["s"] - PAD_IN, (prev_e + words[f0]["s"]) / 2 if f0 else 0.0)  # never clip the previous word
    end = min(words[f1]["e"] + PAD_OUT, (words[f1]["e"] + next_s) / 2 if next_s > words[f1]["e"] else words[f1]["e"] + PAD_OUT)
    return {"core_words": [i0, i1], "final_words": [f0, f1],
            "core_start": words[i0]["s"], "core_end": words[i1]["e"],
            "final_start": round(start, 3), "final_end": round(max(end, words[f1]["e"] + 0.05), 3),
            "text": _text(words, f0, f1)}


def validate(words, clip, min_s=MIN_S, max_s=MAX_S, sents=None):
    """Boundary quality gate. Returns a list of issue codes; empty means pass."""
    f0, f1 = clip["final_words"]
    issues = []
    first, last = words[f0], words[f1]
    if f0 > 0 and not END.search(words[f0 - 1]["w"]) and first["s"] - words[f0 - 1]["e"] < 0.35:
        issues.append("starts_mid_sentence")
    if not END.search(last["w"]):
        issues.append("ends_mid_sentence")
    if clip["final_start"] > first["s"] or clip["final_end"] < last["e"]:
        issues.append("truncates_word")
    d = clip["final_end"] - clip["final_start"]
    if d > max_s + 1:
        issues.append("too_long")
    if d < min_s - 4:
        issues.append("too_short")
    gaps = [words[i + 1]["s"] - words[i]["e"] for i in range(f0, f1)]
    if gaps and max(gaps) > 3.0:
        issues.append("dead_air")
    return issues


def refine_checked(words, i0, i1, sents=None, rounds=3):
    """refine + validate; on failure widen the setup/tail allowance and retry."""
    sents = sents or sentences(words)
    clip = refine(words, i0, i1, sents)
    for r in range(rounds):
        issues = validate(words, clip, sents=sents)
        if not [i for i in issues if i in ("starts_mid_sentence", "ends_mid_sentence", "truncates_word")]:
            break
        a, b = clip["final_words"]
        if "starts_mid_sentence" in issues:
            a = sents[max(0, _sent_of(sents, a) - 1)][0]
        if "ends_mid_sentence" in issues:
            b = sents[min(len(sents) - 1, _sent_of(sents, b) + 1)][1]
        clip = _pack(words, i0, i1, a, b)
    clip["issues"] = validate(words, clip, sents=sents)
    return clip
