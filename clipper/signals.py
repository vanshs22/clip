"""Local signals, all returning a per-second series (list of floats, index = second).
No GPU or paid API. PANNs / YOLO / TalkNet plug in later as extra series with the same shape."""
import json, re
from .util import ffmpeg, run, median, mad, note_skipped

LAUGH = re.compile(r"\b(ha(ha)+|lol|lmao|laugh\w*|oh my god|omg|no way|what the|insane|crazy|unbelievable|holy)\b", re.I)


def audio_rms(path, duration):
    """Per-second RMS level (dB) via ffmpeg astats. Fast: no Python sample loop."""
    r = run([ffmpeg(), "-hide_banner", "-nostats", "-i", path, "-vn", "-ac", "1",
             "-af", "aresample=8000,asetnsamples=n=8000:p=0,astats=metadata=1:reset=1,ametadata=print:key=lavfi.astats.Overall.RMS_level:file=-",  # resample FIRST: n counts samples at the INPUT rate, so otherwise a "second" is 1/6 s
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


SPAM = re.compile(r"(^\s*!|giveaway|\bgiv\w*away\b|https?://|\buse code\b|\bsub4sub\b|\bfollow (me|us)\b|\bfree (skins?|v-?bucks|nitro|robux)\b)", re.I)


def chat_series(chat, duration, with_info=False, per_user_cap=2, repeat_window=30):
    """chat: [{"t": pts_seconds, "u": user, "m": text}]. Messages per second + unique-user bonus, after removing what is not audience reaction:
    bot commands (!...), links, giveaway/promo text, more than `per_user_cap` messages from one user in a second, and the SAME user repeating
    the SAME text within `repeat_window` s. Identical text from many DIFFERENT users (KEKW, GG) is kept: that is the signal."""
    n = int(duration) + 1
    s, users = [0.0] * n, [set() for _ in range(n)]
    per_user, last_text = {}, {}
    kept = spam = 0
    for c in sorted(chat, key=lambda c: c["t"]):
        i, u, m = int(c["t"]), c.get("u"), str(c.get("m", ""))
        if not 0 <= i < n:
            continue
        if SPAM.search(m) or (u is not None and (per_user.get((i, u), 0) >= per_user_cap or (last_text.get(u) and last_text[u][0] == m and c["t"] - last_text[u][1] < repeat_window))):
            spam += 1
            continue
        if u is not None:
            per_user[(i, u)] = per_user.get((i, u), 0) + 1
            last_text[u] = (m, c["t"])
            if len(per_user) > 50000:  # bounded memory on very long chats
                per_user = {k: v for k, v in per_user.items() if k[0] >= i - 5}
        s[i] += 1
        users[i].add(u)
        kept += 1
    out = [a + 0.5 * len(u) for a, u in zip(s, users)]
    return (out, {"messages": len(chat), "kept": kept, "spam_dropped": spam}) if with_info else out


def heat_series(heatmap, duration):
    """YouTube 'most replayed' heatmap segments [{start_time,end_time,value}] -> per-second series (empty if the source has none)."""
    if not heatmap:
        return []
    out = [0.0] * (int(duration) + 1)
    for h in heatmap:
        for t in range(int(h.get("start_time", 0)), min(len(out), int(h.get("end_time", 0)) + 1)):
            out[t] = max(out[t], float(h.get("value", 0)))
    return out


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

    def __init__(self, window=60, k=6.0, min_abs=6.0, warmup=10):
        self.hist, self.window, self.k, self.min_abs, self.warmup = __import__('collections').deque(maxlen=window), window, k, min_abs, warmup  # bounded: hours of stream, constant memory

    def feed(self, x):
        h = list(self.hist)
        hit = False
        if len(h) >= self.warmup:
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

EVENT_LABELS = {"Laughter", "Giggle", "Snicker", "Chuckle, chortle", "Belly laugh", "Applause", "Cheering", "Crowd", "Screaming", "Shout", "Yell", "Whoop", "Explosion", "Gunshot, gunfire"}
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
    Verified on real audio (it tagged audience chuckling after a joke)."""
    try:
        import numpy as np
        from . import models
        ck, lb = models.try_ensure("panns_cnn14", "sound events (PANNs)"), models.try_ensure("panns_labels", "sound events (PANNs)")
        if not (ck and lb):
            return []
        with models.panns_home():  # panns-inference reads $HOME/panns_data at import time
            from panns_inference import AudioTagging, labels
            tagger = AudioTagging(checkpoint_path=str(ck), device="cuda" if __import__("shutil").which("nvidia-smi") else "cpu")
    except ImportError as e:
        note_skipped("sound events (PANNs)", f"not installed: {e}")
        return []
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
    Score = keyword hits + a little for text that changed. Returns [] if rapidocr is missing. Verified: reads on-screen text from real frames."""
    try:
        from rapidocr_onnxruntime import RapidOCR
    except ImportError as e:
        note_skipped("on-screen text (OCR)", f"not installed: {e}")
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
