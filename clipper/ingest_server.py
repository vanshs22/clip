"""Local ingest point for OBS/ffmpeg pushes: runs MediaMTX (https://github.com/bluenviron/mediamtx) and prints the URLs.
Push RTMP to rtmp://HOST:1935/live/<key>, then `clipper monitor rtmp://127.0.0.1:1935/live/<key>`. Binary: on PATH, $MEDIAMTX, or auto-downloaded
(release asset from GitHub) into the model cache."""
import os, platform, shutil, subprocess, tarfile, tempfile, urllib.request, json
from pathlib import Path
from .models import CACHE
from .util import safe_extract_tar


def binary():
    p = os.environ.get("MEDIAMTX") or shutil.which("mediamtx")
    if p:
        return p
    exe = CACHE / "mediamtx" / "mediamtx"
    if not exe.exists():
        osn = {"Linux": "linux", "Darwin": "darwin"}.get(platform.system())
        arch = {"x86_64": "amd64", "AMD64": "amd64", "arm64": "arm64", "aarch64": "arm64"}.get(platform.machine())
        if not osn or not arch:
            raise SystemExit("unsupported platform for auto-download: install MediaMTX and set $MEDIAMTX")
        rel = json.load(urllib.request.urlopen("https://api.github.com/repos/bluenviron/mediamtx/releases/latest", timeout=30))["tag_name"]
        with tempfile.TemporaryDirectory() as d:
            f = Path(d) / "m.tgz"
            urllib.request.urlretrieve(f"https://github.com/bluenviron/mediamtx/releases/download/{rel}/mediamtx_{rel}_{osn}_{arch}.tar.gz", f)
            exe.parent.mkdir(parents=True, exist_ok=True)
            with tarfile.open(f) as tf:
                safe_extract_tar(tf, exe.parent)  # refuses '..', absolute paths and links
        exe.chmod(0o755)
    return str(exe)


def start(rtmp_port=1935, extra_env=None):
    """-> Popen. Localhost-only RTMP unless MTX_RTMPADDRESS is set by the caller (never exposed by default)."""
    env = {**os.environ, "MTX_RTMPADDRESS": f"127.0.0.1:{rtmp_port}", "MTX_RTSPADDRESS": "127.0.0.1:8554", "MTX_HLSADDRESS": "127.0.0.1:8888",
           "MTX_WEBRTCADDRESS": "127.0.0.1:8889", "MTX_SRTADDRESS": "127.0.0.1:8890", "MTX_API": "no", "MTX_METRICS": "no", "MTX_PLAYBACK": "no", **(extra_env or {})}
    work = Path(tempfile.mkdtemp())  # MediaMTX writes auto-generated TLS files (auto.crt/auto.key) into its working dir: keep them out of the repo
    cfg = work / "mediamtx.yml"
    cfg.write_text("paths:\n  all_others:\n")
    return subprocess.Popen([binary(), str(cfg)], env=env, cwd=work, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT, text=True)
