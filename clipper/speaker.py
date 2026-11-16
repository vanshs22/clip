"""Faces, tracks and active speaker, using only OpenCV's bundled-API YuNet face model (230 KB ONNX, cached on first use).

Active speaker = whose mouth moves while speech is happening. For every tracked face we measure frame-to-frame change of a
landmark-aligned mouth patch, then compare it with the transcript's speech activity. It is a light stand-in for TalkNet:
no training, CPU only, validated on real footage in tests/validate_real.py (see docs/SPEC.md for the measured accuracy)."""
import os, shutil, subprocess, urllib.request
from pathlib import Path
from .util import ffmpeg, median

YUNET = "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx"
CACHE = Path(os.environ.get("CLIPPER_MODELS", Path.home() / ".cache" / "clipper"))


def fetch(name, url):
    """Download a model once. Returns its path."""
    p = CACHE / name
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".part")
        with urllib.request.urlopen(url, timeout=60) as r, open(tmp, "wb") as f:
            shutil.copyfileobj(r, f)
        tmp.replace(p)
    return p


def frames(src, t0, t1, fps, width=1280):
    """Yield (t_abs, BGR ndarray) at `fps` through ffmpeg (any codec; sequential, no seeking per frame)."""
    import numpy as np
    from .util import probe
    info = probe(src)
    w = min(width, info["width"]) // 2 * 2
    h = int(info["height"] * w / info["width"]) // 2 * 2
    p = subprocess.Popen([ffmpeg(), "-v", "error", "-ss", f"{t0:.3f}", "-t", f"{t1 - t0:.3f}", "-i", str(src), "-an",
                          "-vf", f"fps={fps},scale={w}:{h}", "-f", "rawvideo", "-pix_fmt", "bgr24", "-"], stdout=subprocess.PIPE)
    n, k = w * h * 3, 0
    try:
        while True:
            buf = p.stdout.read(n)
            if len(buf) < n:
                break
            yield t0 + k / fps, np.frombuffer(buf, np.uint8).reshape(h, w, 3)
            k += 1
    finally:
        p.stdout.close()
        p.kill()


_YN = {}


def detect_faces(fr, score=0.7):
    """-> [dict(cx, cy, size, lm)] normalised to the frame; lm = pixel landmarks (r-eye, l-eye, nose, r-mouth, l-mouth)."""
    import cv2
    h, w = fr.shape[:2]
    d = _YN.get("d")
    if d is None or _YN.get("sz") != (w, h):
        d = _YN["d"] = cv2.FaceDetectorYN.create(str(fetch("yunet.onnx", YUNET)), "", (w, h), score, 0.3, 5000)
        _YN["sz"] = (w, h)
    _, f = d.detect(fr)
    return [] if f is None else [{"cx": (r[0] + r[2] / 2) / w, "cy": (r[1] + r[3] / 2) / h, "size": r[2] * r[3] / (w * h),
                                  "lm": [(r[4 + 2 * i], r[5 + 2 * i]) for i in range(5)]} for r in f]


def mouth_patch(gray, lm):
    import cv2
    import numpy as np
    (rx, ry), (lx, ly) = lm[3], lm[4]
    cx, cy, mw = (rx + lx) / 2, (ry + ly) / 2, max(8.0, ((rx - lx) ** 2 + (ry - ly) ** 2) ** .5 * 1.6)
    x0, x1, y0, y1 = int(cx - mw / 2), int(cx + mw / 2), int(cy - mw * .3), int(cy + mw * .3)
    h, w = gray.shape
    if x0 < 0 or y0 < 0 or x1 > w or y1 > h or x1 - x0 < 6 or y1 - y0 < 4:
        return None
    p = cv2.resize(gray[y0:y1, x0:x1], (24, 14)).astype(np.float32)
    return (p - p.mean()) / (p.std() + 1e-3)  # lighting-invariant


def analyze(src, t0, t1, fps=6):
    """Track faces over [t0,t1]. -> {"t": [...], "tracks": [{"x","act","p","size"} dense lists, None where unseen]}.
    Tracks associate by nearest centre (< 12% of frame width, seen within 1 s). Storage is sparse while running and dead tracks
    with fewer than 3 detections (audience faces that flash by) are dropped, so a 2-hour video stays small."""
    import cv2
    import numpy as np
    ts, live, done, nid = [], {}, [], 0
    for t, fr in frames(src, t0, t1, fps):
        i = len(ts)
        ts.append(t)
        gray, used = cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY), set()
        for f in sorted(detect_faces(fr), key=lambda f: -f["size"]):
            cand = [(abs(tr["cx"] - f["cx"]), k) for k, tr in live.items() if k not in used and abs(tr["cx"] - f["cx"]) < 0.12 and t - tr["t"] < 1.0]
            if cand:
                k = min(cand)[1]
            else:
                k, nid = nid, nid + 1
                live[k] = {"d": {}, "cx": f["cx"], "t": t, "p": None}
            used.add(k)
            tr, p = live[k], mouth_patch(gray, f["lm"])
            act = float(np.abs(p - tr["p"]).mean()) if p is not None and tr["p"] is not None else None
            tr["d"][i] = (f["cx"], f["size"], act, p)
            tr["cx"], tr["t"] = f["cx"], t
            if p is not None:
                tr["p"] = p
        for k in [k for k, tr in live.items() if t - tr["t"] > 2.0]:
            tr = live.pop(k)
            if len(tr["d"]) >= 3:
                done.append(tr)
    done += [tr for tr in live.values() if len(tr["d"]) >= 3]
    n, tracks = len(ts), []
    for tr in done:
        d = {"x": [None] * n, "act": [None] * n, "p": [None] * n, "size": [0.0] * n}
        for i, (x, sz, act, p) in tr["d"].items():
            d["x"][i], d["size"][i], d["act"][i], d["p"][i] = x, sz, act, p
        tracks.append(d)
    return {"t": ts, "tracks": stitch(tracks)}


def _med_x(tr):
    return median([x for x in tr["x"] if x is not None])


def stitch(tracks, max_dx=0.10):
    """A head turn or a missed detection splits one person into several tracks. Merge tracks with the same position that never
    appear in the same frame."""
    tracks = sorted(tracks, key=lambda t: next(i for i, x in enumerate(t["x"]) if x is not None))
    out = []
    for tr in tracks:
        for o in out:
            if abs(_med_x(o) - _med_x(tr)) < max_dx and not any(a is not None and b is not None for a, b in zip(o["x"], tr["x"])):
                for key in ("x", "act", "p", "size"):
                    o[key] = [b if a is None or (key == "size" and a == 0.0) else a for a, b in zip(o[key], tr[key])]
                break
        else:
            out.append(dict(tr))
    return out


def main_tracks(an, share=0.4):
    """Indices of the people who matter: total face area at least `share` of the largest track's."""
    sc = [sum(t["size"]) for t in an["tracks"]]
    return [k for k in sorted(range(len(sc)), key=lambda k: -sc[k]) if sc[k] >= share * max(sc, default=1)][:4]


# ---- voice side: neural speaker embeddings (resemblyzer d-vectors, optional dependency) + k-means --------------------------------
_ENC = []


def voice_windows(src, t0, t1, words, win=2.0, hop=1.0, sr=16000):
    """-> (windows [(ts, te)], d-vectors) for windows that are mostly speech. Raises ImportError without `resemblyzer`."""
    import numpy as np
    from resemblyzer import VoiceEncoder, preprocess_wav
    if not _ENC:
        _ENC.append(VoiceEncoder("cpu"))
    raw = subprocess.run([ffmpeg(), "-v", "error", "-ss", f"{t0:.3f}", "-t", f"{t1 - t0:.3f}", "-i", str(src), "-vn", "-ac", "1", "-ar", str(sr), "-f", "s16le", "-"],
                         capture_output=True).stdout
    a = np.frombuffer(raw, np.int16).astype(np.float32) / 32768
    W, E, t = [], [], t0
    while t + win <= t1:
        sp = sum(max(0, min(w["e"], t + win) - max(w["s"], t)) for w in words if w["e"] > t and w["s"] < t + win)
        if sp >= 0.6 * win:
            W.append((t, t + win))
            E.append(_ENC[0].embed_utterance(preprocess_wav(a[int((t - t0) * sr): int((t - t0 + win) * sr)], source_sr=sr)))
        t += hop
    return W, (np.array(E) if E else np.zeros((0, 256)))


def kmeans(X, k, seed=0, iters=30, restarts=6):
    """Plain k-means on cosine-normalised rows. -> labels."""
    import numpy as np
    X = X / (np.linalg.norm(X, axis=1, keepdims=True) + 1e-9)
    best = None
    for r in range(restarts):
        rng = np.random.default_rng(seed + r)
        C = X[rng.choice(len(X), k, replace=False)]
        for _ in range(iters):
            lab = ((X[:, None] - C[None]) ** 2).sum(-1).argmin(1)
            C2 = np.array([X[lab == j].mean(0) if (lab == j).any() else C[j] for j in range(k)])
            if np.allclose(C, C2):
                break
            C = C2
        inertia = ((X - C[lab]) ** 2).sum()
        if best is None or inertia < best[0]:
            best = (inertia, lab)
    return best[1]


def silhouette(X, lab):
    import numpy as np
    X = X / (np.linalg.norm(X, axis=1, keepdims=True) + 1e-9)
    D, out = 1 - X @ X.T, []
    for i in range(len(X)):
        same = (lab == lab[i]) & (np.arange(len(X)) != i)
        if same.any():
            a = D[i, same].mean()
            b = min(D[i, lab == c].mean() for c in set(lab.tolist()) if c != lab[i])
            out.append((b - a) / max(a, b, 1e-9))
    return float(np.mean(out)) if out else 0.0


def mouth_series(tr, n):
    """Per-frame mouth-patch change for one track (centre of the mouth patch only)."""
    import numpy as np
    out, prev = [], None
    for p in tr["p"][:n]:
        out.append(float(np.abs(p[3:11, 4:20] - prev[3:11, 4:20]).mean()) if p is not None and prev is not None else float("nan"))
        if p is not None:
            prev = p
    return np.array(out)


SIL_MIN, SHARE_MIN = 0.3, 0.2  # measured: two real speakers 0.47-0.51; one speaker 0.07-0.20 (docs/SPEC.md)


def diarized_speakers(src, an, words, t0, t1, win=2.0):
    """Two-person conversations. Voices -> faces over [t0,t1] (pass the WHOLE video: evidence pooled over a scene is what makes
    this accurate; clip-length spans alone were 46-85%).
    1. d-vector windows, k-means with k=2; the split must be real (silhouette >= SIL_MIN) and balanced (smaller side >= SHARE_MIN),
       otherwise this is one person's delivery and we DO NOT follow anyone;
    2. match each voice to the face whose mouth moves most while that voice speaks (z-scored per face), best of the 2 assignments.
    Returns (timeline [(t, track)], info). info["reliable"] is the gate callers must respect."""
    import numpy as np
    keys = main_tracks(an)
    if len(keys) < 2:
        return [], {"reason": "fewer than two faces", "reliable": False}
    try:
        W, E = voice_windows(src, t0, t1, words, win)
    except ImportError:
        return [], {"reason": "resemblyzer not installed", "reliable": False}
    if len(W) < 20:
        return [], {"reason": "not enough speech", "reliable": False}
    lab = kmeans(E, 2)
    sil, share = silhouette(E, lab), float(np.bincount(lab).min() / len(lab))
    ts = np.array(an["t"])
    Zw = np.zeros((len(W), len(keys)))
    for j, q in enumerate(keys):
        v = mouth_series(an["tracks"][q], len(ts))
        m, sd = np.nanmedian(v), np.nanmedian(np.abs(v - np.nanmedian(v))) * 1.4826 + 1e-6
        z = (v - m) / sd
        for i, (a_, b_) in enumerate(W):
            x = z[(ts >= a_) & (ts < b_)]
            Zw[i, j] = np.nan if np.isnan(x).all() else np.nanmean(x)
    Zw = np.nan_to_num(Zw, nan=0.0)
    A = np.array([Zw[lab == c].mean(0) for c in (0, 1)])
    pairs = [(i, j) for i in range(len(keys)) for j in range(len(keys)) if i != j]
    fa, fb = max(pairs, key=lambda p: A[0, p[0]] + A[1, p[1]])
    full = {0: keys[fa], 1: keys[fb]}
    reliable = sil >= SIL_MIN and share >= SHARE_MIN
    tl = [(round(a_, 2), full[int(l)]) for (a_, b_), l in zip(W, lab)]
    return tl, {"reliable": bool(reliable), "silhouette": round(sil, 3), "share": round(share, 2), "map": {0: int(full[0]), 1: int(full[1])}}


def speech_at(words, t, pad=0.15):
    return any(w["s"] - pad <= t <= w["e"] + pad for w in words)


def who_speaks(an, words, win=1.5, margin=1.25):
    """Per window of `win` s: index of the active track, or None (no speech / no clear winner). Plus overall evidence.
    The winner needs `margin` x the runner-up's mean mouth activity. Returns (timeline, confidence 0..1)."""
    ts, tr = an["t"], an["tracks"]
    if not ts or len(tr) < 1:
        return [], 0.0
    out, t = [], ts[0]
    while t <= ts[-1]:
        idx = [i for i, x in enumerate(ts) if t <= x < t + win]
        talk = [i for i in idx if speech_at(words, ts[i])]
        res = None
        if len(talk) >= max(2, len(idx) // 3):
            m = [sum(tr[k]["act"][i] for i in talk if tr[k]["act"][i] is not None) / max(1, sum(tr[k]["act"][i] is not None for i in talk)) for k in range(len(tr))]
            vis = [k for k in range(len(tr)) if sum(tr[k]["act"][i] is not None for i in talk) >= 2]
            if vis:
                top = max(vis, key=lambda k: m[k])
                rest = [m[k] for k in vis if k != top]
                if not rest or m[top] >= margin * max(rest):
                    res = top
        out.append((round(t, 2), res))
        t += win
    decided = [r for _, r in out if r is not None]
    return out, len(decided) / max(1, len(out))


def follow_keyframes(an, tl, min_dwell=2.0, glide=0.3, t_off=0.0):
    """Active-speaker timeline -> crop keyframes [(t_rel, x)]. Switches only after the new speaker has held for min_dwell
    (no ping-pong on interjections); a quick glide, never during a scene cut (caller applies this per shot)."""
    if not tl:
        return []
    ts = an["t"]

    def xof(k, t):
        i = min(range(len(ts)), key=lambda i: abs(ts[i] - t))
        v = [an["tracks"][k]["x"][j] for j in range(max(0, i - 3), min(len(ts), i + 4)) if an["tracks"][k]["x"][j] is not None]
        return median(v) if v else None
    cur, kf, pending = None, [], None
    for t, k in tl:
        if k is None:
            continue
        if cur is None:
            cur = k
            x = xof(k, t)
            if x is not None:
                kf.append((round(t - t_off, 2), round(x, 3)))
        elif k != cur:
            if pending and pending[0] == k:
                if t - pending[1] >= min_dwell:
                    x = xof(k, pending[1])
                    if x is not None:
                        kf += ([(round(pending[1] - t_off, 2), kf[-1][1])] if kf else []) + [(round(pending[1] - t_off + glide, 2), round(x, 3))]
                    cur, pending = k, None
            else:
                pending = (k, t)
        else:
            pending = None
    return kf


def score_against(tl, an, truth):
    """Validation helper: truth(t) -> 'L'|'R'|None. A decided window is correct when the winning track sits on the right side."""
    ok = bad = 0
    for t, k in tl:
        gt = truth(t + 0.75)
        if k is None or gt is None:
            continue
        xs = [x for x in an["tracks"][k]["x"] if x is not None]
        side = "L" if median(xs) < 0.5 else "R"
        ok, bad = ok + (side == gt), bad + (side != gt)
    return ok, bad


def timeline(src, words, duration, fps=6):
    """Whole-video speaker map, computed ONCE per job. Pooling evidence over the full video is what makes it accurate (96-99% on real
    panels; clip-length spans alone were 46-85%). -> dict(t, tracks[{x}], tl [(t, track)], reliable, info). Compact enough to cache as JSON."""
    an = analyze(src, 0, duration, fps)
    keys = main_tracks(an)
    tl, info = diarized_speakers(src, an, words, 0, duration)
    slim = {"t": [round(t, 2) for t in an["t"]], "tracks": [{"x": [None if x is None else round(x, 3) for x in an["tracks"][k]["x"]]} for k in keys]}
    return {**slim, "tl": [(t, keys.index(q)) for t, q in tl if q in keys], "reliable": bool(info.get("reliable")) and len(keys) >= 2, "info": info, "faces": len(keys)}


def keyframes_from_timeline(spk, a, b, s, min_dwell=2.0):
    """Crop keyframes for shot [a,b] (clip start s) from the whole-video map; [] if the map has nothing for this shot."""
    sub = [(t, k) for t, k in spk["tl"] if a <= t <= b]
    if not sub:
        return []
    return follow_keyframes({"t": spk["t"], "tracks": spk["tracks"]}, sub, min_dwell=min_dwell, t_off=s)
