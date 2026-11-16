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
    """Median face x (0..1) over a few sampled frames of one shot, if OpenCV exists; else centre.
    ponytail: one static crop per SHOT. Smooth tracking (YOLO/TalkNet) is the upgrade; never animate across a cut."""
    try:
        import cv2
    except ImportError:
        return 0.5
    det = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    cap, xs = cv2.VideoCapture(str(src)), []
    for k in range(5):
        cap.set(cv2.CAP_PROP_POS_MSEC, (t0 + (t1 - t0) * (k + .5) / 5) * 1000)
        ok, fr = cap.read()
        if ok:
            fs = det.detectMultiScale(cv2.cvtColor(fr, cv2.COLOR_BGR2GRAY), 1.2, 5, minSize=(h // 12,) * 2)
            if len(fs):
                x, _, fw, _ = max(fs, key=lambda f: f[2])
                xs.append((x + fw / 2) / w)
    cap.release()
    return sorted(xs)[len(xs) // 2] if xs else 0.5


def shot_cuts(s, e, cuts, min_len=1.5):
    """Clip-relative cut times. Cuts within min_len of an edge or of each other are ignored (detector noise)."""
    out = []
    for c in sorted(cuts):
        r = c - s
        if min_len <= r <= (e - s) - min_len and (not out or r - out[-1] >= min_len):
            out.append(round(r, 2))
    return out


def crop_expr(xs, cuts_rel):
    """Piecewise-constant crop position, one value per shot: it can only change AT a cut, never animate across one.
    If shots agree (within 6% of width) a single static value is used: no needless movement."""
    if max(xs) - min(xs) < 0.06:
        return f"{sorted(xs)[len(xs) // 2]:.3f}"
    expr = f"{xs[-1]:.3f}"
    for i in range(len(cuts_rel) - 1, -1, -1):
        expr = f"if(lt(t,{cuts_rel[i]}),{xs[i]:.3f},{expr})"
    return expr


def zoom_expr(base, zooms):
    """Scale factor over time: `base` plus an eased punch-in (0.3 s ramps) for each motivated zoom."""
    return str(base) + "".join(f"+{z['amt'] - 1:.3f}*max(0,min(1,min((t-{z['t']:.2f})/0.3,({z['t'] + z['dur']:.2f}-t)/0.3)))" for z in zooms)


def vfilter(info, layout, fx, ass, zoom, w=W, h=H, zooms=(), sharp=False):
    """filter_complex ending in [v]. fx: 0..1 crop position or an expression from crop_expr.
    fill = 9:16 crop on the subject; fit = blurred backdrop + full frame (gameplay/webcam keep their relationship)."""
    sub = f"subtitles={ass}" if ass else "null"
    z = ""
    if zoom or zooms:
        ze = zoom_expr(zoom or 1, zooms)
        z = f",scale=w='trunc({w}*({ze})/2)*2':h='trunc({h}*({ze})/2)*2':eval=frame,crop={w}:{h}"
    sh = ",unsharp=5:5:0.5" if sharp else ""
    sc = f"flags=lanczos{sh}"
    if layout == "fit":
        return (f"[0:v]split[a][b];[a]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},boxblur=30:5[bg];"
                f"[b]scale={w}:-2:{sc}[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2{z},{sub}[v]")
    if info["width"] / max(1, info["height"]) <= 9 / 16 + 0.01:  # already vertical
        return f"[0:v]scale={w}:{h}:force_original_aspect_ratio=increase:{sc},crop={w}:{h}{z},{sub}[v]"
    return f"[0:v]crop=ih*9/16:ih:'(iw-ih*9/16)*({fx})':0,scale={w}:{h}:{sc}{z},{sub}[v]"


def render_clip(src, clip, out_dir, words, info, draft=False, force=False, cuts=()):
    """clip: plan entry (final_start/end, caption, layout, zoom, zooms, focus_x). cuts: absolute scene-cut times of the source."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    spec = {k: clip.get(k) for k in ("final_start", "final_end", "caption", "layout", "zoom", "zooms", "focus_x", "id")}
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
    cr = shot_cuts(s, e, cuts)
    if clip.get("focus_x") is not None:  # explicit user value wins; auto-detected shots are not part of the cache key
        fx = f"{clip['focus_x']:.3f}"
    else:
        if clip.get("focus_auto") is None or len(clip["focus_auto"]) != len(cr) + 1:
            edges = [s] + [s + c for c in cr] + [e]
            clip["focus_auto"] = [focus_x(src, a, b, info["width"], info["height"]) for a, b in zip(edges, edges[1:])]
        fx = crop_expr(clip["focus_auto"], cr)
    # zooms must sit inside one shot and inside the clip
    dur = e - s
    zooms = [z for z in clip.get("zooms") or [] if z["t"] >= 0 and z["t"] + z["dur"] <= dur and not any(z["t"] < c < z["t"] + z["dur"] for c in cr)]
    up = (H / info["height"]) if layout_is_fill(clip, info) else (W / max(1, info["width"]))
    clip["warn"] = ["low_res_source"] if up >= 1.5 else []
    enc = encoder()
    q = ["-preset", "veryfast", "-crf", "28"] if draft else (["-preset", "medium", "-crf", "18"] if enc == "libx264" else ["-b:v", "10M"])
    vf = vfilter(info, clip.get("layout", "fill"), fx, ass, clip.get("zoom"), *((540, 960) if draft else (W, H)), zooms=zooms, sharp=up >= 1.5)
    cmd = [ffmpeg(), "-hide_banner", "-y", "-ss", f"{s:.3f}", "-i", os.path.abspath(src), "-t", f"{dur:.3f}",
           "-filter_complex", vf, "-map", "[v]", "-map", "0:a?", "-af", "loudnorm=I=-14:TP=-1.5:LRA=11",
           "-c:v", enc, *q, "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", out.name]
    r = run(cmd, cwd=out_dir)
    if r.returncode:
        out.unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg failed for clip {clip['id']}: {r.stderr[-600:]}")
    return out


def layout_is_fill(clip, info):
    return clip.get("layout", "fill") == "fill" and info["width"] / max(1, info["height"]) > 9 / 16 + 0.01


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
