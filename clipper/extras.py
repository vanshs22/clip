"""Pure helpers for reframing and b-roll. Detectors (YOLO/Haar) live in render.py; everything here is testable without models."""
import re
from pathlib import Path
from .util import median

MEDIA = {".png", ".jpg", ".jpeg", ".webp", ".mp4", ".mov", ".mkv", ".webm"}


def smooth_track(samples, dead=0.08, ramp=0.8):
    """samples: [(t, x|None)] inside ONE shot, t clip-relative. -> keyframes [(t, x)].
    Median-of-3 de-noising, then hysteresis: hold still until the subject leaves a dead zone around the crop,
    then glide to the new position over `ramp` s. Natural framing beats constant camera movement."""
    pts = [(t, x) for t, x in samples if x is not None]
    if not pts:
        return [(samples[0][0] if samples else 0.0, 0.5)]
    xs = [median([pts[max(0, i - 1)][1], pts[i][1], pts[min(len(pts) - 1, i + 1)][1]]) for i in range(len(pts))]
    kf, cur = [(pts[0][0], xs[0])], xs[0]
    for (t, _), x in zip(pts[1:], xs[1:]):
        if abs(x - cur) > dead:
            if kf[-1][0] < t:
                kf.append((t, cur))
            kf.append((t + ramp, x))
            cur = x
    return [(round(t, 2), round(x, 3)) for t, x in kf]


def pair_of(samples, min_gap=0.25, share=0.6):
    """samples: [[(x, size), ...] per sampled frame]. Two people persistently apart -> (left_x, right_x), else None."""
    pairs = []
    for fr in samples:
        top = sorted(sorted(fr, key=lambda d: -d[1])[:2], key=lambda d: d[0])
        if len(top) == 2 and top[1][0] - top[0][0] >= min_gap:
            pairs.append((top[0][0], top[1][0]))
    if not samples or len(pairs) / len(samples) < share:
        return None
    return round(median([p[0] for p in pairs]), 3), round(median([p[1] for p in pairs]), 3)


def match_broll(words, folder, clip_start, clip_end, max_n=3, dur=2.0, gap=6.0):
    """Keyword match between file names (pizza.png, golden_goal.mp4) and spoken words. Returns [{"file","t","dur"}], t clip-relative."""
    files = [p for p in sorted(Path(folder).iterdir()) if p.suffix.lower() in MEDIA]
    out = []
    for p in files:
        key = {k for k in re.split(r"[_\-\s]+", p.stem.lower()) if len(k) >= 4}
        hit = next((w for w in words if clip_start <= w["s"] <= clip_end - dur - 0.5 and re.sub(r"\W", "", w["w"].lower()) in key), None)
        if hit:
            out.append({"file": str(p.resolve()), "t": round(hit["s"] - clip_start, 2), "dur": dur})
    out.sort(key=lambda b: b["t"])
    keep = []
    for b in out:  # spacing: b-roll is seasoning
        if not keep or b["t"] - keep[-1]["t"] >= gap:
            keep.append(b)
    return keep[:max_n]
