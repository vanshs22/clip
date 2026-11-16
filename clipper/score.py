"""Multimodal fusion -> ranked, boundary-refined, de-duplicated candidates."""
import re
from . import boundaries as B
from .signals import box
from .util import percentile_rank

# speech, visual, audio, chat, events (PANNs), ocr; hook is added after boundaries. Renormalised over the signals that exist.
PROFILES = {  # "heat" = YouTube most-replayed heatmap. Weights are configurable starting points, benchmarked in golden/, NOT proven constants
    "podcast":   {"speech": .40, "visual": .05, "audio": .15, "chat": .20, "events": .15, "ocr": .00, "heat": .05, "layout": "fill"},
    "interview": {"speech": .40, "visual": .05, "audio": .20, "chat": .15, "events": .10, "ocr": .00, "heat": .05, "layout": "fill"},
    "gaming":    {"speech": .15, "visual": .25, "audio": .20, "chat": .20, "events": .10, "ocr": .10, "heat": .05, "layout": "fit"},
    "irl":       {"speech": .25, "visual": .20, "audio": .15, "chat": .20, "events": .15, "ocr": .05, "heat": .05, "layout": "fill"},
    "comedy":    {"speech": .30, "visual": .05, "audio": .20, "chat": .15, "events": .30, "ocr": .00, "heat": .05, "layout": "fill"},
    "sports":    {"speech": .15, "visual": .20, "audio": .25, "chat": .15, "events": .10, "ocr": .15, "heat": .05, "layout": "fit"},
    "auto":      {"speech": .25, "visual": .15, "audio": .15, "chat": .20, "events": .15, "ocr": .10, "heat": .05, "layout": "fill"},
}
# Tighter 'dense' core windows: +2/3 stories on the synthetic set (exact boundaries) but MIXED on 4 real scenes (panel2 and bk recall fell, ch rose).
# Off by default until it is measured on more human-labelled real video; enable with CLIPPER_DENSE_CORE=1.
DENSE_CORE = __import__('os').environ.get('CLIPPER_DENSE_CORE', '').lower() in ('1', 'true', 'yes')
HOOK_W = 0.10
HOOK = re.compile(r"^(\W*)(why|how|what|nobody|never|everyone|the (best|worst|biggest)|i (can't|never|thought)|you (won't|have to)|this is|so\b|\d)", re.I)


def hook_score(text):
    first = re.split(r"(?<=[.?!])\s", text.strip(), maxsplit=1)[0]
    s = 0.5 * bool(HOOK.search(first)) + 0.3 * ("?" in first or "!" in first) + 0.2 * (4 <= len(first.split()) <= 20)
    return min(1.0, s)


def auto_profile(words, duration, sig):
    """Pick a scoring profile from the content: little speech + lots of visual activity -> gaming; talk-dominated -> podcast."""
    spoken = len({int(w["s"]) for w in words}) / max(1.0, duration)
    vis = sig.get("visual") or []
    active = (sum(1 for x in vis if x > 1.0) / max(1, len(vis))) if vis else 0
    if spoken < 0.25:
        return "gaming" if active > 0.2 else "auto"
    return "podcast" if spoken > 0.5 else "irl"


def _dense_core(words, sents, ws, comb, t):
    """The fused window is ~20 s wide but the moment is usually 6-15 s of it. Keep the contiguous run of sentences around the peak whose
    mean fused score is above the window's median (sentences carrying the evidence); fall back to the whole window if that leaves too little."""
    ks = sorted({B._sent_of(sents, i) for i in ws})
    per = {}
    for k in ks:
        a, b = sents[k]
        lo, hi = int(words[a]["s"]), int(words[b]["e"]) + 1
        seg = comb[max(0, lo):min(len(comb), hi + 1)]
        per[k] = sum(seg) / len(seg) if seg else 0.0
    if len(ks) < 3:
        return ws
    med = sorted(per.values())[len(per) // 2]
    peak_k = min(ks, key=lambda k: abs((words[sents[k][0]]["s"] + words[sents[k][1]]["e"]) / 2 - t))
    lo = hi = peak_k
    while lo - 1 in per and per[lo - 1] >= med:
        lo -= 1
    while hi + 1 in per and per[hi + 1] >= med:
        hi += 1
    core = [i for i in ws if lo <= B._sent_of(sents, i) <= hi]
    return core if len(core) >= 8 else ws


def _refine_peak(t, series, radius=10):
    """The fused score is smoothed over ~20 s, so its maximum can sit several seconds off the real event. Move to the strongest RAW sample
    (rank-normalised across the available series) within `radius` s."""
    best, bt = -1.0, t
    ranks = [percentile_rank(v) for v in series.values() if v and any(v)]
    for k in range(max(0, int(t) - radius), min(len(ranks[0]) if ranks else 0, int(t) + radius + 1)):
        sc = sum(r[k] for r in ranks)
        if sc > best:
            best, bt = sc, k
    return bt


def _event_clip(t, duration, cuts, audio, win=20):
    """A candidate for a moment WITHOUT usable speech: start at the nearest scene cut before the peak (or 8 s before), end when the audio falls
    back toward its median (or 12 s after). Boundaries are time-based, so these carry no sentence checks."""
    before = [c for c in (cuts or []) if t - 15 <= c <= t - 2]
    s = max(0.0, (before[-1] if before else t - 8))
    med = sorted(audio)[len(audio) // 2] if audio else 0
    e = t + 12
    if audio:
        for k in range(int(t + 4), min(len(audio), int(t + 25))):
            if audio[k] <= med and k - t >= 4:
                e = k + 1.5
                break
    e = min(e, duration)
    return {"kind": "event", "core_words": [0, 0], "final_words": [0, 0], "core_start": t - 2, "core_end": t + 2, "final_start": round(s, 2),
            "final_end": round(max(e, s + 6), 2), "text": "", "issues": [], "boundary_quality": 1.0}


def candidates(words, duration, series, profile="auto", n=40, win=20, min_sep=25, cuts=None, reaction=None):
    """series: {"speech": [...], "audio": [...], "visual": [...], "chat": [...], "events": [...], "ocr": [...], "heat": [...]} (missing keys are skipped).
    Windows with words become speech clips (sentence-snapped); windows without words become event clips, so a silent highlight is still found."""
    prof = PROFILES.get(profile, PROFILES["auto"])
    n_sec = int(duration) + 1
    fit = lambda v: (list(v) + [0.0] * n_sec)[:n_sec]  # series from different tools differ by a sample or two: pad/trim, never crash
    series = {k: fit(v) for k, v in series.items() if v}
    sig = {k: percentile_rank(box(v, win)) for k, v in series.items() if any(v) and k in prof}
    if not sig:
        return []
    wsum = sum(prof[k] for k in sig)
    comb = [sum(prof[k] * sig[k][i] for k in sig) / wsum for i in range(int(duration) + 1)]
    picks, taken = [], []
    for t in sorted(range(len(comb)), key=lambda i: -comb[i]):
        if all(abs(t - u) >= min_sep for u in taken):
            taken.append(t)
            picks.append(t)
        if len(picks) >= n * 2:
            break
    sents, out = (B.sentences(words) if words else []), []
    for t in picks:
        ws = [i for i, w in enumerate(words) if t - win / 2 <= w["s"] <= t + win / 2]
        ws = _dense_core(words, sents, ws, comb, t) if (DENSE_CORE and len(ws) >= 8) else ws
        if len(ws) >= 8:
            c = B.refine_checked(words, ws[0], ws[-1], sents, reaction=reaction)
            h = hook_score(c["text"])
        else:  # (almost) no speech here: an audio/visual/chat/event highlight
            c, h = _event_clip(_refine_peak(t, {k: v for k, v in series.items() if k in prof and prof[k] > 0}), duration, cuts, series.get("audio")), 0.5
        c.update(score=round((1 - HOOK_W) * comb[t] + HOOK_W * h, 4), hook=round(h, 2), peak=t, signals={k: round(sig[k][t], 2) for k in sig})
        out.append(c)
    return dedupe(sorted(out, key=lambda c: -c["score"]))[:n]


def live_candidates(words, duration, events, cuts=None):
    """Moments flagged while the stream was running (events.json windows). They get the same boundary refinement as VOD candidates."""
    sents, out = (B.sentences(words) if words else []), []
    for ev in events:
        mid = (ev["start"] + ev["end"]) / 2 if ev.get("peak_t") is None else ev["peak_t"]
        ws = [i for i, w in enumerate(words) if ev["start"] <= w["s"] <= ev["end"]]
        c = B.refine_checked(words, ws[0], ws[-1], sents) if len(ws) >= 8 else _event_clip(mid, duration, cuts, [])
        c.update(score=round(0.55 + 0.25 * ev.get("confidence", 0.5), 4), hook=round(hook_score(c["text"]), 2), peak=int(mid), signals={"live": ev.get("kinds", [])}, live=True)
        out.append(c)
    return out
def iou(a, b):
    i = min(a["final_end"], b["final_end"]) - max(a["final_start"], b["final_start"])
    u = max(a["final_end"], b["final_end"]) - min(a["final_start"], b["final_start"])
    return max(0.0, i) / u if u > 0 else 0.0


def dedupe(cs, thr=0.3):
    """Best-first: drop a clip if it overlaps an already-kept one by IoU >= thr, or shares more than 2 s of footage with it (the viewer would
    hear/see the same seconds twice). Seconds shared are measured on the final windows."""
    keep = []
    for c in cs:
        if all(iou(c, k) < thr and min(c["final_end"], k["final_end"]) - max(c["final_start"], k["final_start"]) <= 2.0 for k in keep):
            keep.append(c)
    return keep
