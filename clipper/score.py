"""Multimodal fusion -> ranked, boundary-refined, de-duplicated candidates."""
import re
from . import boundaries as B
from .signals import box
from .util import percentile_rank

# speech, visual, audio, chat, (hook is added after boundaries). Renormalised over the signals that exist.
PROFILES = {
    "podcast":   {"speech": .45, "visual": .05, "audio": .20, "chat": .20, "layout": "fill"},
    "interview": {"speech": .45, "visual": .05, "audio": .25, "chat": .15, "layout": "fill"},
    "gaming":    {"speech": .20, "visual": .30, "audio": .25, "chat": .25, "layout": "fit"},
    "irl":       {"speech": .30, "visual": .25, "audio": .20, "chat": .25, "layout": "fill"},
    "comedy":    {"speech": .35, "visual": .10, "audio": .35, "chat": .20, "layout": "fill"},
    "sports":    {"speech": .20, "visual": .25, "audio": .35, "chat": .20, "layout": "fit"},
    "auto":      {"speech": .30, "visual": .20, "audio": .20, "chat": .20, "layout": "fill"},
}
HOOK_W = 0.10
HOOK = re.compile(r"^(\W*)(why|how|what|nobody|never|everyone|the (best|worst|biggest)|i (can't|never|thought)|you (won't|have to)|this is|so\b|\d)", re.I)


def hook_score(text):
    first = re.split(r"(?<=[.?!])\s", text.strip(), maxsplit=1)[0]
    s = 0.5 * bool(HOOK.search(first)) + 0.3 * ("?" in first or "!" in first) + 0.2 * (4 <= len(first.split()) <= 20)
    return min(1.0, s)


def candidates(words, duration, series, profile="auto", n=40, win=20, min_sep=25):
    """series: {"speech": [...], "audio": [...], "visual": [...], "chat": [...]} (missing keys are skipped)."""
    prof = PROFILES.get(profile, PROFILES["auto"])
    sig = {k: percentile_rank(box(v, win)) for k, v in series.items() if v and any(v) and k in prof}
    if not sig:
        return []
    wsum = sum(prof[k] for k in sig)
    comb = [sum(prof[k] * sig[k][i] for k in sig) / wsum for i in range(int(duration) + 1)]
    # greedy peak picking with minimum separation (non-max suppression)
    picks, taken = [], []
    for t in sorted(range(len(comb)), key=lambda i: -comb[i]):
        if all(abs(t - u) >= min_sep for u in taken):
            taken.append(t)
            picks.append(t)
        if len(picks) >= n * 2:
            break
    sents, out = B.sentences(words), []
    for t in picks:
        ws = [i for i, w in enumerate(words) if t - win / 2 <= w["s"] <= t + win / 2]
        if not ws:
            continue
        c = B.refine_checked(words, ws[0], ws[-1], sents)
        h = hook_score(c["text"])
        c.update(score=round((1 - HOOK_W) * comb[t] + HOOK_W * h, 4), hook=round(h, 2), peak=t,
                 signals={k: round(sig[k][t], 2) for k in sig})
        out.append(c)
    return dedupe(sorted(out, key=lambda c: -c["score"]))[:n]


def iou(a, b):
    i = min(a["final_end"], b["final_end"]) - max(a["final_start"], b["final_start"])
    u = max(a["final_end"], b["final_end"]) - min(a["final_start"], b["final_start"])
    return max(0.0, i) / u if u > 0 else 0.0


def dedupe(cs, thr=0.3):
    keep = []
    for c in cs:  # sorted best-first, so the better of two overlapping clips wins
        if all(iou(c, k) < thr for k in keep):
            keep.append(c)
    return keep
