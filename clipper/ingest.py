"""Source classification, download (yt-dlp), chat loading. No DRM circumvention; auth is the user's own cookies."""
import json, re, shutil
from pathlib import Path
from .util import run, probe, jload, validate_source, safe_path

HOSTS = [("youtube", r"(youtube\.com|youtu\.be)"), ("twitch", r"twitch\.tv"), ("kick", r"kick\.com"), ("facebook", r"(facebook\.com|fb\.watch)")]
LOCAL = (".mp4", ".mov", ".mkv", ".webm", ".ts", ".m4v", ".flv")


def classify(src):
    validate_source(src)
    if Path(src).exists():
        return {"kind": "file", "platform": "local"}
    if re.match(r"rtmps?://", src):
        return {"kind": "live", "platform": "rtmp"}
    if re.search(r"\.m3u8(\?|$)", src):
        return {"kind": "live", "platform": "hls"}
    for name, rx in HOSTS:
        if re.search(rx, src):
            return {"kind": "url", "platform": name}
    if src.lower().split("?")[0].endswith(LOCAL):
        return {"kind": "url", "platform": "direct"}
    return {"kind": "unknown", "platform": "?"}


def inspect(src, cookies=None):
    """-> {"kind": file|vod|live|unavailable|auth_required, ...}. Never downloads."""
    c = classify(src)
    if c["kind"] == "file":
        return {**c, "state": "vod", "title": Path(src).stem, "duration": probe(src)["duration"]}
    if c["kind"] == "live":
        return {**c, "state": "live"}
    if not shutil.which("yt-dlp"):
        return {**c, "state": "unavailable", "error": "yt-dlp not installed (pip install yt-dlp)"}
    r = run(["yt-dlp", "--dump-single-json", "--no-warnings", "--skip-download", *(["--cookies", cookies] if cookies else []), "--", src], timeout=300)
    if r.returncode:
        err = r.stderr.strip().splitlines()[-1] if r.stderr.strip() else "unknown error"
        auth = re.search(r"sign in|login|private|members|age|cookies", err, re.I)
        return {**c, "state": "auth_required" if auth else "unavailable", "error": err,
                "hint": "Export your own authorized session: --cookies cookies.txt" if auth else ""}
    j = json.loads(r.stdout)
    return {**c, "state": "live" if j.get("is_live") else "vod", "title": j.get("title"), "duration": j.get("duration") or 0,
            "heatmap": j.get("heatmap")}  # YouTube "most replayed" segments, when the video has them


def download(src, dest, cookies=None):
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    have = next(iter(dest.glob("source.*")), None)
    if have and have.suffix not in (".part", ".json", ".vtt"):
        return have  # resume: never download twice
    r = run(["yt-dlp", "-f", "bv*[height<=1080]+ba/b[height<=1080]/b", "--merge-output-format", "mp4",
             "--write-subs", "--sub-langs", "live_chat", "-o", str(dest / "source.%(ext)s"),
             *(["--cookies", cookies] if cookies else []), "--", src], timeout=7200)
    out = next((p for p in dest.glob("source.*") if p.suffix in LOCAL), None)
    if r.returncode and not out:
        raise RuntimeError(r.stderr[-500:])
    return out


def load_chat(path):
    """Accepts our chat.jsonl ({t,u,m}), a yt-dlp YouTube live_chat.json, or TwitchDownloader JSON."""
    path = Path(path)
    if not path.exists():
        return []
    txt = path.read_text(errors="ignore").strip()
    out = []
    try:
        j = json.loads(txt)
        if isinstance(j, dict) and "comments" in j:  # TwitchDownloader
            return [{"t": c["content_offset_seconds"], "u": (c.get("commenter") or {}).get("name"), "m": (c.get("message") or {}).get("body", "")} for c in j["comments"]]
    except json.JSONDecodeError:
        pass
    for line in txt.splitlines():
        try:
            j = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "t" in j:
            out.append(j)
        elif "replayChatItemAction" in j:  # YouTube replay: one JSON per line
            a = j["replayChatItemAction"]
            n = len([x for x in a.get("actions", []) if "addChatItemAction" in x])
            out += [{"t": int(a.get("videoOffsetTimeMsec", 0)) / 1000, "u": None, "m": ""}] * n
    return out


def fetch_chat(src, dest):
    """Best-effort chat replay download for Twitch VODs with the free TwitchDownloaderCLI (MIT) if it is on PATH. -> path or None.
    UNVERIFIED against a live Twitch VOD in this project: if it fails the pipeline simply continues without chat."""
    m = re.search(r"twitch\.tv/videos/(\d+)", src)
    exe = shutil.which("TwitchDownloaderCLI")
    if not (m and exe):
        return None
    out = Path(dest) / "twitch_chat.json"
    r = run([exe, "chatdownload", "--id", m[1], "-o", str(out)], timeout=900)
    return str(out) if r.returncode == 0 and out.exists() else None
