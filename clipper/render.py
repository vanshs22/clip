"""FFmpeg renderer + QC. One clip = one cached file; the cache key is the full clip spec, so an edit
re-renders only that clip and a crash never re-renders finished ones."""
import os, re, shutil, sys
from pathlib import Path
from . import captions
from .util import digest, ffmpeg, probe, run, jsave

W, H = 1080, 1920


def encoder():
    # ponytail: NVENC/VideoToolbox are detected by presence, not by a test encode. AMF/QSV: pass --encoder.
    if os.environ.get("CLIPPER_ENCODER"):
        return os.environ["CLIPPER_ENCODER"]
    if shutil.which("nvidia-smi") and "h264_nvenc" in run([ffmpeg(), "-hide_banner", "-encoders"]).stdout:
        return "h264_nvenc"
    return "h264_videotoolbox" if sys.platform == "darwin" else "libx264"


def focus_x(src, t0, t1, w, h):
    """Median face x (0..1) over a few sampled frames, if OpenCV exists; else centre.
    ponytail: one static crop per clip. Per-shot tracking (YOLO/TalkNet) is the upgrade; never animate across cuts."""
    try:
        import cv2
    except ImportError:
        return 0.5
    det = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    cap, xs = cv2.VideoCapture(str(src)), []
    for k in range(6):
        cap.set(cv2.CAP_PROP_POS_MSEC, (t0 + (t1 - t0) * (k + .5) / 6) * 1000)
        ok, fr = cap.read()
        if ok:
            fs = det.detectMultiScale(cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY), 1.2, 5, minSize=(h // 12,) * 2)
            if len(fs):
                x, _, fw, _ = max(fs, key=lambda f: f[2])
                xs.append((x + fw / 2) / w)
    cap.release()
    return sorted(xs)[len(xs) // 2] if xs else 0.5


def vfilter(info, layout, fx, ass, zoom, w=W, h=H):
    """Return filter_complex ending in [v]. fill = crop to 9:16 on the subject; fit = blurred backdrop + full frame."""
    sub = f"subtitles={ass}" if ass else "null"
    z = f",scale=iw*{zoom}:-2,crop={w}:{h}" if zoom else ""
    if layout == "fit":
        return (f"[0:v]split[a][b];[a]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},boxblur=30:5[bg];"
                f"[b]scale={w}:-2[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2{z},{sub}[v]")
    if info["width"] / max(1, info["height"]) <= 9 / 16 + 0.01:  # already vertical
        return f"[0:v]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h}{z},{sub}[v]"
    return f"[0:v]crop=ih*9/16:ih:(iw-ih*9/16)*{fx:.3f}:0,scale={w}:{h}{z},{sub}[v]"


def render_clip(src, clip, out_dir, words, info, draft=False, force=False):
    """clip: plan entry with final_start/final_end, caption{style,scale}, layout, zoom, focus_x. Returns output path."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    spec = {k: clip.get(k) for k in ("final_start", "final_end", "caption", "layout", "zoom", "focus_x", "id")}
    spec["draft"] = draft
    out = out_dir / f"clip_{clip['id']:02d}{'_draft' if draft else ''}_{digest(spec)}.mp4"
    if out.exists() and not force:
        return out
    for old in out_dir.glob(f"clip_{clip['id']:02d}{'_draft' if draft else ''}_*.mp4"):
        old.unlink()  # stale render of an earlier spec
    s, e = clip["final_start"], clip["final_end"]
    cw = [w for w in words if w["s"] >= s - 0.01 and w["e"] <= e + 0.3]
    cap = clip.get("caption") or {}
    ass = None
    if cap.get("style", "bold_pop") != "none" and cw:
        (out_dir / f"clip_{clip['id']:02d}.ass").write_text(captions.build(cw, s, cap.get("style", "bold_pop"), cap.get("scale", 1.0)))
        ass = f"clip_{clip['id']:02d}.ass"
    fx = clip.get("focus_x")  # explicit user value is part of the cache key; the auto-detected one is not
    if fx is None:
        fx = clip["focus_auto"] = clip.get("focus_auto") or focus_x(src, s, e, info["width"], info["height"])
    enc = encoder()
    q = ["-preset", "veryfast", "-crf", "28"] if draft else (["-preset", "medium", "-crf", "18"] if enc == "libx264" else ["-b:v", "10M"])
    vf = vfilter(info, clip.get("layout", "fill"), fx, ass, clip.get("zoom"), *((540, 960) if draft else (W, H)))
    cmd = [ffmpeg(), "-hide_banner", "-y", "-ss", f"{s:.3f}", "-i", os.path.abspath(src), "-t", f"{e - s:.3f}",
           "-filter_complex", vf, "-map", "[v]", "-map", "0:a?", "-af", "loudnorm=I=-14:TP=-1.5:LRA=11",
           "-c:v", enc, *q, "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", out.name]
    r = run(cmd, cwd=out_dir)
    if r.returncode:
        out.unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg failed for clip {clip['id']}: {r.stderr[-600:]}")
    return out


def qc(path, clip, draft=False):
    """Render-level checks. Boundary/duplicate checks run earlier in boundaries.validate and the plan stage."""
    issues, info = [], probe(path)
    w, h = (540, 960) if draft else (W, H)
    if (info["width"], info["height"]) != (w, h):
        issues.append(f"bad_size:{info['width']}x{info['height']}")
    if not info["audio"]:
        issues.append("no_audio")
    want = clip["final_end"] - clip["final_start"]
    if abs(info["duration"] - want) > 0.6:
        issues.append(f"duration_off:{info['duration']:.1f}!={want:.1f}")
    r = run([ffmpeg(), "-hide_banner", "-v", "error", "-i", path, "-f", "null", "-"])
    if r.returncode or r.stderr.strip():
        issues.append("decode_error")
    bd = run([ffmpeg(), "-hide_banner", "-i", path, "-vf", "blackdetect=d=0.5:pix_th=0.05", "-an", "-f", "null", "-"]).stderr
    if re.search(r"black_start", bd):
        issues.append("black_frames")
    sd = run([ffmpeg(), "-hide_banner", "-i", path, "-af", "silencedetect=n=-45dB:d=1.2", "-vn", "-f", "null", "-"]).stderr
    starts, ends = re.findall(r"silence_start: ([\d.]+)", sd), re.findall(r"silence_end: ([\d.]+)", sd)
    if starts and (len(ends) < len(starts) or float(ends[-1]) >= info["duration"] - 0.1):
        issues.append("silent_tail")
    return issues, info
