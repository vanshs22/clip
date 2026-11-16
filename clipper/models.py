"""Central model manager. Every optional model (face detector, PANNs, speaker embeddings, ...) is fetched through here:
manifest entry (url, sha256, size, licence), resumable download to a .part file, SHA-256 verification, atomic replace, corruption detection,
offline mode (CLIPPER_OFFLINE=1) and a clear ModelUnavailable error that callers turn into a graceful fallback (never a crash).
Checksums pin the exact files validated in this project; they are trust-on-first-use against the upstream URL at that time."""
import json, os, time, urllib.request
from contextlib import contextmanager
from pathlib import Path
from .util import sha256_file, note_skipped

CACHE = Path(os.environ.get("CLIPPER_MODELS") or Path.home() / ".cache" / "clipper")
MANIFEST = json.loads((Path(__file__).parent / "models_manifest.json").read_text())


class ModelUnavailable(RuntimeError):
    """A model could not be provided (offline, download failed, checksum mismatch). Callers must degrade, not crash."""


def offline():
    return os.environ.get("CLIPPER_OFFLINE", "").lower() in ("1", "true", "yes")


def dest(name):
    d = MANIFEST[name]["dest"]
    if d.startswith("@panns/"):  # panns-inference insists on $HOME/panns_data: we give it a private HOME (see panns_home)
        return CACHE / "home" / "panns_data" / d[len("@panns/"):]
    return CACHE / d


def _verified(p, entry):
    """Hash check, memoised by (mtime,size) so a 330 MB model is not re-hashed on every run."""
    memo = CACHE / ".verified.json"
    try:
        known = json.loads(memo.read_text())
    except (OSError, ValueError):
        known = {}
    st = p.stat()
    key = f"{p}:{st.st_mtime_ns}:{st.st_size}"
    if known.get(key) == entry["sha256"]:
        return True
    ok = st.st_size == entry["size"] and sha256_file(p) == entry["sha256"]
    if ok:
        known[key] = entry["sha256"]
        try:
            CACHE.mkdir(parents=True, exist_ok=True)
            memo.write_text(json.dumps(known))
        except OSError:
            pass
    return ok


def _download(url, part, entry, tries=4, log=print):
    """Resumable: a connection that drops mid-body yields a SHORT read (not an exception), so the byte count is checked against the size we
    expect and the loop continues with a Range request from where the .part file ends."""
    last = None
    for attempt in range(tries):
        have = part.stat().st_size if part.exists() else 0
        if entry.get("size") and have >= entry["size"]:
            return
        req = urllib.request.Request(url, headers={"User-Agent": "clipper/0.4", **({"Range": f"bytes={have}-"} if have else {})})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                if have and r.status != 206:  # server ignored Range: restart cleanly
                    have = 0
                with open(part, "ab" if have else "wb") as f:
                    while chunk := r.read(1 << 20):
                        f.write(chunk)
            got = part.stat().st_size
            if not entry.get("size") or got >= entry["size"]:
                return
            last = f"connection closed early ({got}/{entry['size']} bytes)"
        except Exception as e:  # network errors, HTTP errors, IncompleteRead: back off and resume
            last = e
        time.sleep(2 ** attempt)
    raise ModelUnavailable(f"download failed after {tries} tries: {last}")


def ensure(name, log=print):
    """-> Path of a verified model file. Raises ModelUnavailable (never anything else)."""
    if name not in MANIFEST:
        raise ModelUnavailable(f"unknown model {name}")
    e, p = MANIFEST[name], dest(name)
    if p.exists():
        if _verified(p, e):
            return p
        p.unlink()  # corrupted or a different version: never use it silently
        log(f"model {name}: checksum mismatch, discarding")
    if offline():
        raise ModelUnavailable(f"{name} is not installed and CLIPPER_OFFLINE is set (run: clipper models fetch {name})")
    p.parent.mkdir(parents=True, exist_ok=True)
    part = p.with_name(p.name + ".part")
    log(f"downloading {name} ({e['size'] / 1e6:.1f} MB, licence: {e['license']})")
    try:
        _download(e["url"], part, e)
        if not (part.stat().st_size == e["size"] and sha256_file(part) == e["sha256"]):
            part.unlink(missing_ok=True)
            raise ModelUnavailable(f"{name}: downloaded file failed SHA-256 verification (upstream changed?)")
        os.replace(part, p)  # atomic
    except OSError as ex:
        raise ModelUnavailable(f"{name}: {ex}")
    return p


def ensure_group(prefix, log=print):
    """Fetch every manifest entry whose name starts with `prefix` (e.g. 'ecapa/'); -> directory containing them."""
    names = [n for n in MANIFEST if n.startswith(prefix)]
    paths = [ensure(n, log) for n in names]
    return paths[0].parent


def try_ensure(name, feature, log=print):
    """-> Path or None; records why in the job's 'features skipped' list."""
    try:
        return ensure(name, log)
    except ModelUnavailable as e:
        note_skipped(feature, str(e))
        return None


def status():
    out = []
    for n, e in MANIFEST.items():
        if n.startswith("_"):
            continue
        p = dest(n)
        out.append({"name": n, "installed": p.exists(), "verified": p.exists() and _verified(p, e), "size_mb": round(e["size"] / 1e6, 1),
                    "license": e["license"], "needed_for": e["needed_for"]})
    return out


@contextmanager
def panns_home():
    """panns-inference reads/writes $HOME/panns_data at import time; point HOME at a private folder holding our verified files."""
    saved = {k: os.environ.get(k) for k in ("HOME", "USERPROFILE")}
    os.environ["HOME"] = os.environ["USERPROFILE"] = str(CACHE / "home")
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
