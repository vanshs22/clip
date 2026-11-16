"""Local signals, all returning a per-second series (list of floats, index = second).
No GPU or paid API. PANNs / YOLO / TalkNet plug in later as extra series with the same shape."""
import json, re
from .util import ffmpeg, run, median, mad

LAUGH = re.compile(r"\b(ha(ha)+|lol|lmao|laugh\w*|oh my god|omg|no way|what the|insane|crazy|unbelievable|holy)\b", re.I)


def audio_rms(path, duration):
    """Per-second RMS level (dB) via ffmpeg astats. Fast: no Python sample loop."""
    r = run([ffmpeg(), "-hide_banner", "-nostats", "-i", path, "-vn", "-ac", "1", "-ar", "8000",
             "-af", "asetnsamples=n=8000,astats=metadata=1:reset=1,ametadata=print:key=lavfi.astats.Overall.RMS_level:file=-",
             "-f", "null", "-"])
    vals = []
    for m in re.finditer(r"RMS_level=(-?[\d.]+|-inf)", r.stdout):
        vals.append(-90.0 if m[1] == "-inf" else max(-90.0, float(m[1])))
    return (vals + [-90.0] * int(duration + 1))[: int(duration) + 1]


def scene_density(path, duration, thr=0.3):
    """Scene-cut count per second at 2 fps / 160px (cheap proxy for visual action). Also returns cut times."""
    r = run([ffmpeg(), "-hide_banner", "-nostats", "-i", path, "-an",
             "-vf", f"fps=2,scale=160:-2,select='gt(scene,{thr})',showinfo", "-f", "null", "-"])
    cuts = [float(m[1]) for m in re.finditer(r"pts_time:([\d.]+)", r.stderr)]
    s = [0.0] * (int(duration) + 1)
    for t in cuts:
        if int(t) < len(s):
            s[int(t)] += 1
    return s, cuts


def text_series(words, duration):
    """Speech/context excitement: laughter/exclamation lexicon, ?/! density, speaking-rate surge."""
    s = [0.0] * (int(duration) + 1)
    for w in words:
        i = int(w["s"])
        if i >= len(s):
            continue
        s[i] += 1.0 / 3                              # speech rate
        if re.search(r"[!?]", w["w"]):
            s[i] += 1.5
        if LAUGH.search(w["w"]):
            s[i] += 2.0
    return s


def chat_series(chat, duration):
    """chat: list of {"t": pts_seconds, "u": user, "m": text}. Messages per second, plus unique-user bonus."""
    s, users = [0.0] * (int(duration) + 1), [set() for _ in range(int(duration) + 1)]
    for c in chat:
        i = int(c["t"])
        if 0 <= i < len(s):
            s[i] += 1
            users[i].add(c.get("u"))
    return [a + 0.5 * len(u) for a, u in zip(s, users)]


def box(series, w):
    """Windowed mean (centered), O(n)."""
    n, out, acc = len(series), [], 0.0
    pre = [0.0]
    for x in series:
        pre.append(pre[-1] + x)
    for i in range(n):
        a, b = max(0, i - w // 2), min(n, i + w // 2 + 1)
        out.append((pre[b] - pre[a]) / (b - a))
    return out


class SpikeDetector:
    """Adaptive baseline: median + k*MAD over a rolling window. Not a fixed threshold.
    A spike is a CANDIDATE, never proof of a good clip."""

    def __init__(self, window=60, k=6.0, min_abs=6.0):
        self.hist, self.window, self.k, self.min_abs = [], window, k, min_abs

    def feed(self, x):
        h = self.hist[-self.window:]
        hit = False
        if len(h) >= 10:
            thr = median(h) + self.k * max(1.4826 * mad(h), 1.0)
            hit = x >= thr and x >= self.min_abs
        self.hist.append(x)
        return hit


def spikes(series, **kw):
    d = SpikeDetector(**kw)
    return [i for i, x in enumerate(series) if d.feed(x)]
