"""Shared helpers: ffmpeg lookup, subprocess, JSON artifacts, robust stats."""
import hashlib, json, os, re, shutil, subprocess, sys
from pathlib import Path

CFG = {"latency_s": 0.0}


def ffmpeg():
    p = os.environ.get("CLIPPER_FFMPEG") or shutil.which("ffmpeg")
    if p:
        return p
    try:  # optional: pip install imageio-ffmpeg ships a static binary
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        sys.exit("ffmpeg not found. Install it or `pip install imageio-ffmpeg` (run `clipper doctor`).")


def run(cmd, **kw):
    return subprocess.run([str(c) for c in cmd], capture_output=True, text=True, **kw)


def probe(path):
    """Duration, size, audio presence from `ffmpeg -i` (no ffprobe needed)."""
    err = run([ffmpeg(), "-hide_banner", "-i", path]).stderr
    d = re.search(r"Duration: (\d+):(\d+):([\d.]+)", err)
    v = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", err)
    fps = re.search(r"([\d.]+) fps", err)
    return {
        "duration": int(d[1]) * 3600 + int(d[2]) * 60 + float(d[3]) if d else 0.0,
        "width": int(v[1]) if v else 0, "height": int(v[2]) if v else 0,
        "fps": float(fps[1]) if fps else 30.0, "audio": "Audio:" in err,
    }


def jload(p, default=None):
    p = Path(p)
    return json.loads(p.read_text()) if p.exists() else default


def jsave(p, obj):
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")  # atomic: a crash never leaves a half-written artifact
    tmp.write_text(json.dumps(obj, indent=1, ensure_ascii=False))
    tmp.replace(p)


def digest(*parts):
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:16]


def median(xs):
    s = sorted(xs)
    n = len(s)
    return 0.0 if not n else (s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2)


def mad(xs):
    m = median(xs)
    return median([abs(x - m) for x in xs])


def percentile_rank(xs):
    """Map each value to its rank in [0,1]; robust to outliers and scale."""
    order = sorted(range(len(xs)), key=lambda i: xs[i])
    out = [0.0] * len(xs)
    for r, i in enumerate(order):
        out[i] = r / max(1, len(xs) - 1)
    return out


def hms(t):
    t = int(t)
    return f"{t // 3600:d}:{t % 3600 // 60:02d}:{t % 60:02d}"
