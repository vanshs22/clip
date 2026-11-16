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


def word_score(w):
    """Excitement of one word: speech rate + exclamation/question + laughter/hype lexicon."""
    return 1.0 / 3 + 1.5 * bool(re.search(r"[!?]", w["w"])) + 2.0 * bool(LAUGH.search(w["w"]))


def text_series(words, duration):
    """Speech/context excitement per second."""
    s = [0.0] * (int(duration) + 1)
    for w in words:
        if int(w["s"]) < len(s):
            s[int(w["s"])] += word_score(w)
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


# ---- phase 2 signals ---------------------------------------------------------------------------------------------
import subprocess
from .util import percentile_rank

EVENT_LABELS = {"Laughter", "Giggle", "Belly laugh", "Applause", "Cheering", "Crowd", "Screaming", "Shout", "Yell", "Whoop", "Explosion", "Gunshot, gunfire"}
OCR_WORDS = re.compile(r"\b(victory|defeat|win(ner)?|kill(s|ed)?|goal|score[sd]?|ace|clutch|gg|round|knockout|k\.?o\.?|final|eliminated|checkmate|epic|new record)\b", re.I)


def motion_series(path, duration):
    """Visual action: mean luma difference between consecutive frames at 2 fps (ffmpeg signalstats; no OpenCV needed)."""
    r = run([ffmpeg(), "-hide_banner", "-nostats", "-i", path, "-an", "-vf",
             "fps=2,scale=160:-2,signalstats,metadata=print:key=lavfi.signalstats.YDIF:file=-", "-f", "null", "-"])
    v = [float(x) for x in re.findall(r"YDIF=([\d.]+)", r.stdout)]
    s = [0.0] * (int(duration) + 1)
    for k, x in enumerate(v):
        if k // 2 < len(s):
            s[k // 2] = max(s[k // 2], x)
    return s


def fuse_visual(motion, cuts):
    """Rank-normalise motion and cut density so neither scale dominates."""
    if not any(motion) and not any(cuts):
        return []
    return [a + b for a, b in zip(percentile_rank(motion), percentile_rank(cuts))]


def event_scores(rows, labels, wanted=EVENT_LABELS):
    """rows: per-window class probabilities; labels: class names. -> max probability over the 'exciting' classes."""
    idx = [i for i, l in enumerate(labels) if l in wanted]
    return [max((row[i] for i in idx), default=0.0) for row in rows]


def audio_events(path, duration, win=5, batch=48):
    """Sound events (laughter, cheering, applause, screams...) with PANNs. Returns [] if panns-inference/numpy are missing.
    UNVERIFIED without the model weights (~300 MB download on first use): see docs/SPEC.md."""
    try:
        import numpy as np
        from panns_inference import AudioTagging, labels
    except ImportError:
        return []
    tagger = AudioTagging(checkpoint_path=None, device="cuda" if __import__("shutil").which("nvidia-smi") else "cpu")
    p = subprocess.Popen([ffmpeg(), "-v", "error", "-i", path, "-vn", "-ac", "1", "-ar", "32000", "-f", "f32le", "-"], stdout=subprocess.PIPE)
    n, out = win * 32000 * 4, []
    while True:
        buf = p.stdout.read(n * batch)
        if len(buf) < n:
            break
        a = np.frombuffer(buf[: len(buf) // n * n], dtype=np.float32).reshape(-1, win * 32000)
        probs, _ = tagger.inference(a)
        out += event_scores(probs.tolist(), labels)
    p.wait()
    s = [0.0] * (int(duration) + 1)
    for i, x in enumerate(out):
        for k in range(win):
            if i * win + k < len(s):
                s[i * win + k] = x
    return s


def ocr_series(path, duration, every=5):
    """On-screen text events (scoreboards, 'VICTORY', kill feeds) via RapidOCR on 1 frame / `every` s.
    Score = keyword hits + a little for text that changed. Returns [] if rapidocr is missing. UNVERIFIED without the model."""
    try:
        from rapidocr_onnxruntime import RapidOCR
    except ImportError:
        return []
    import tempfile, glob
    ocr, s, prev = RapidOCR(), [0.0] * (int(duration) + 1), set()
    with tempfile.TemporaryDirectory() as d:
        run([ffmpeg(), "-v", "error", "-i", path, "-an", "-vf", f"fps=1/{every},scale=960:-2", f"{d}/f%06d.jpg"])
        for k, f in enumerate(sorted(glob.glob(f"{d}/f*.jpg"))):
            res, _ = ocr(f)
            txt = " ".join(r[1] for r in (res or []))
            toks = set(txt.lower().split())
            score = len(OCR_WORDS.findall(txt)) + 0.05 * len(toks - prev)
            prev = toks
            for i in range(k * every, min(len(s), (k + 1) * every)):
                s[i] = score
    return s
