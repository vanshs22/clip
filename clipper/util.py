"""Shared helpers: ffmpeg lookup, safe subprocess, JSON artifacts, structured logs, input validation, fingerprints, robust stats."""
import collections, hashlib, json, os, re, shutil, subprocess, sys, time
from pathlib import Path
from urllib.parse import urlparse

CFG = {"latency_s": 0.0}
_SKIPPED = collections.OrderedDict()


def ffmpeg():
    p = os.environ.get("CLIPPER_FFMPEG") or shutil.which("ffmpeg")
    if p:
        return p
    try:  # optional: pip install imageio-ffmpeg ships a static binary
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except ImportError:
        sys.exit("ffmpeg not found. Install it or `pip install imageio-ffmpeg` (run `clipper doctor`).")


def run(cmd, timeout=3600, **kw):
    """List-only subprocess (never a shell). A timeout returns returncode 124 instead of raising, so callers treat it like any failure."""
    assert isinstance(cmd, (list, tuple)), "commands must be argument lists"
    try:
        return subprocess.run([str(c) for c in cmd], capture_output=True, text=True, timeout=timeout, **kw)
    except subprocess.TimeoutExpired as e:
        return subprocess.CompletedProcess(cmd, 124, "", f"timeout after {timeout}s: {e}")


def probe(path):
    """Duration, size, audio presence from `ffmpeg -i` (no ffprobe needed)."""
    err = run([ffmpeg(), "-hide_banner", "-i", path], timeout=120).stderr
    d = re.search(r"Duration: (\d+):(\d+):([\d.]+)", err)
    v = re.search(r"Video:.*?(\d{2,5})x(\d{2,5})", err)
    fps = re.search(r"([\d.]+) fps", err)
    return {
        "duration": int(d[1]) * 3600 + int(d[2]) * 60 + float(d[3]) if d else 0.0,
        "width": int(v[1]) if v else 0, "height": int(v[2]) if v else 0,
        "fps": float(fps[1]) if fps else 30.0, "audio": "Audio:" in err,
    }


def _plain(o):
    """numpy scalars/arrays (from the optional vision/speaker models) -> plain JSON."""
    if hasattr(o, "tolist"):
        return o.tolist()
    raise TypeError(f"not JSON serialisable: {type(o).__name__}")


def jload(p, default=None):
    p = Path(p)
    if not p.exists():
        return default
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:  # a corrupt artifact must not wedge the job: treat as missing
        return default


def jsave(p, obj):
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + f".{os.getpid()}.tmp")  # atomic: a crash never leaves a half-written artifact
    tmp.write_text(json.dumps(obj, indent=1, ensure_ascii=False, default=_plain), encoding="utf-8")
    os.replace(tmp, p)


def digest(*parts):
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:16]


def file_fingerprint(path):
    """size + SHA-256 of three 1 MB samples (start/middle/end): cheap on 50 GB files, changes when content changes."""
    p = Path(path)
    size = p.stat().st_size
    h = hashlib.sha256(str(size).encode())
    with open(p, "rb") as f:
        for off in (0, max(0, size // 2 - 524288), max(0, size - 1048576)):
            f.seek(off)
            h.update(f.read(1048576))
    return h.hexdigest()[:16]


def sha256_file(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while b := f.read(chunk):
            h.update(b)
    return h.hexdigest()


# ---- input validation ---------------------------------------------------------------------------------------------
ALLOWED_SCHEMES = {"http", "https", "rtmp", "rtmps", "rtsp", "srt"}


def validate_source(src):
    """Local path or a URL with an allowed scheme. Rejects option injection, file://, ftp/data/javascript, control chars, credentials-in-URL."""
    if not isinstance(src, str) or not src.strip() or len(src) > 4096 or re.search(r"[\x00-\x1f]", src):
        raise ValueError("source must be a non-empty single-line string")
    if src.startswith("-"):
        raise ValueError("source must not start with '-'")
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", src):
        u = urlparse(src)
        if u.scheme.lower() not in ALLOWED_SCHEMES:
            raise ValueError(f"URL scheme not allowed: {u.scheme}")
        if not u.hostname:
            raise ValueError("URL has no host")
        if u.username or u.password:
            raise ValueError("credentials inside URLs are not accepted (use --cookies)")
        return src
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", src) and not re.match(r"^[a-zA-Z]:[\\/]", src):  # data:, javascript:, but allow C:\ paths
        raise ValueError("unsupported source scheme")
    return src


def safe_path(p, root=None):
    """Resolve `p` and require it to stay under `root` (default: cwd)."""
    root = Path(root or os.getcwd()).resolve()
    q = Path(p).resolve()
    if q != root and root not in q.parents:
        raise ValueError(f"path escapes {root}")
    return q


def safe_extract_tar(tf, dest):
    """tarfile extraction that refuses absolute paths, '..' and links outside dest."""
    dest = Path(dest).resolve()
    for m in tf.getmembers():
        t = (dest / m.name).resolve()
        if dest not in t.parents and t != dest or m.issym() or m.islnk() or m.isdev():
            raise ValueError(f"unsafe archive member: {m.name}")
    tf.extractall(dest)


def check_disk(path, need_gb=2.0):
    p = Path(path)
    while not p.exists() and p != p.parent:
        p = p.parent
    free = shutil.disk_usage(p).free / 1e9
    if free < need_gb:
        raise OSError(f"only {free:.1f} GB free at {p}; need >= {need_gb:.1f} GB (set CLIPPER_MIN_FREE_GB to change)")


# ---- logging / secrets ----------------------------------------------------------------------------------------------
_SECRET = re.compile(r"(sk-[A-Za-z0-9_\-]{12,}|Bearer\s+[A-Za-z0-9._\-]{8,}|(?i:api[_-]?key|token|secret)=[^&\s\"']+|AIza[0-9A-Za-z_\-]{20,})")


def redact(s):
    return _SECRET.sub("[redacted]", str(s))


def log_event(path, event, **kw):
    """Structured JSON-lines log. Values are redacted; failures to log never break a job."""
    try:
        rec = {"t": round(time.time(), 3), "event": event, **{k: redact(v) if isinstance(v, str) else v for k, v in kw.items()}}
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, default=str) + "\n")
    except OSError:
        pass


def note_skipped(feature, reason):
    _SKIPPED[feature] = redact(reason)


def skipped():
    return dict(_SKIPPED)


def pid_alive(pid):
    if not pid:
        return False
    if os.name == "nt":
        import ctypes
        h = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        if h:
            ctypes.windll.kernel32.CloseHandle(h)
        return bool(h)
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


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
