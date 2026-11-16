"""FFmpeg renderer + QC. One clip = one cached file; the cache key is the full clip spec PLUS the source fingerprint, resolved caption style,
and quality settings, so an edit re-renders only that clip and a render from a different source/config is never reused."""
import os, re, shutil, sys
from pathlib import Path
from . import captions, extras, speaker, upscale
from .util import digest, ffmpeg, probe, run, jsave, note_skipped, sha256_file

W, H = 1080, 1920
_ENC = []

# encoder -> function(crf) giving quality arguments. NVENC/QSV/AMF/VideoToolbox have no -crf: each uses its own constant-quality control.
ENC_ARGS = {
    "libx264": lambda q: ["-preset", q["preset"], "-crf", str(q["crf"])],
    "h264_nvenc": lambda q: ["-preset", "p5", "-rc", "vbr", "-cq", str(q["crf"] + 1), "-b:v", "0"],
    "h264_qsv": lambda q: ["-global_quality", str(q["crf"] + 2), "-preset", "medium"],
    "h264_amf": lambda q: ["-quality", "quality", "-rc", "cqp", "-qp_i", str(q["crf"] + 2), "-qp_p", str(q["crf"] + 3)],
    "h264_videotoolbox": lambda q: ["-q:v", str(max(30, 80 - 2 * q["crf"] + 20)), "-allow_sw", "1"],
}


def _test_encode(enc):
    """A real 0.2 s test encode: an encoder that is merely listed (no GPU, wrong driver) fails here, not mid-render."""
    r = run([ffmpeg(), "-hide_banner", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=256x256:r=25:d=0.2", "-pix_fmt", "yuv420p",
             "-c:v", enc, *ENC_ARGS[enc]({"preset": "ultrafast", "crf": 23}), "-f", "null", "-"], timeout=60)
    return r.returncode == 0


def encoder(force=None):
    """Best encoder that actually works here: hardware first (NVENC / QSV / AMF / VideoToolbox), libx264 as the guaranteed fallback.
    CLIPPER_ENCODER overrides (still test-encoded; falls back if it fails)."""
    if _ENC and not force:
        return _ENC[0]
    listed = run([ffmpeg(), "-hide_banner", "-encoders"], timeout=30).stdout
    want = force or os.environ.get("CLIPPER_ENCODER")
    order = ([want] if want else []) + (["h264_videotoolbox"] if sys.platform == "darwin" else ["h264_nvenc", "h264_qsv", "h264_amf"])
    pick = "libx264"
    for e in order:
        if e in ENC_ARGS and e in listed and _test_encode(e):
            pick = e
            break
        elif e in ENC_ARGS and e != "libx264":
            note_skipped(f"hardware encoder {e}", "listed by ffmpeg but a test encode failed or it is not available")
    if not force:
        _ENC[:] = [pick]
    return pick


def shot_framing(src, a, b, s, spk=None):
    """Crop keyframes [(t_rel_to_clip, x)] for ONE shot [a,b] plus the people seen as [(x,size)] per frame.
    Whole-video speaker map reliable -> follow the active speaker (min dwell before switching). Otherwise: one person gets a smoothed
    follow; two or more with an unknown speaker keep the centre (plan_framing may then stack both). Needs OpenCV; else centre."""
    if spk and spk.get("reliable"):
        kf = speaker.keyframes_from_timeline(spk, a, b, s)
        if kf:
            if kf[0][0] > a - s:
                kf.insert(0, (round(a - s, 2), kf[0][1]))
            return kf, []
    try:
        an = speaker.analyze(src, a, b, fps=6)
    except ImportError:
        return [(round(a - s, 2), 0.5)], []
    keys = speaker.main_tracks(an)
    frames = [[(t_["x"][i], t_["size"][i]) for t_ in an["tracks"] if t_["x"][i] is not None] for i in range(len(an["t"]))]
    if len(keys) != 1:
        return [(round(a - s, 2), 0.5)], frames
    return extras.smooth_track([(t - s, x) for t, x in zip(an["t"], an["tracks"][keys[0]]["x"])]), frames


def shot_cuts(s, e, cuts, min_len=1.5):
    """Clip-relative cut times. Cuts within min_len of an edge or of each other are ignored (detector noise)."""
    out = []
    for c in sorted(cuts):
        r = c - s
        if min_len <= r <= (e - s) - min_len and (not out or r - out[-1] >= min_len):
            out.append(round(r, 2))
    return out


def _lerp(kf, tail):
    """Piecewise-linear expression through keyframes [(t,x)], constant after the last one."""
    expr = f"{kf[-1][1]:.3f}"
    for (t0, x0), (t1, x1) in reversed(list(zip(kf, kf[1:]))):
        expr = f"if(lt(t,{t1}),{x0:.3f}+({x1 - x0:.3f})*(t-{t0})/{max(t1 - t0, 0.01):.2f},{expr})"
    return expr


def crop_expr(shots, cuts_rel):
    """shots: per-shot keyframes [[t,x],..] (clip-relative). One expression: it moves only INSIDE a shot, and jumps AT a cut.
    If everything sits within 6% of width, a single static value is used: no needless movement."""
    allx = [x for kf in shots for _, x in kf]
    if max(allx) - min(allx) < 0.06:
        return f"{sorted(allx)[len(allx) // 2]:.3f}"
    expr = _lerp([tuple(k) for k in shots[-1]], None)
    for i in range(len(cuts_rel) - 1, -1, -1):
        expr = f"if(lt(t,{cuts_rel[i]}),{_lerp([tuple(k) for k in shots[i]], None)},{expr})"
    return expr


def zoom_expr(base, zooms):
    """Scale factor over time: `base` plus an eased punch-in (0.3 s ramps) for each motivated zoom."""
    return str(base) + "".join(f"+{z['amt'] - 1:.3f}*max(0,min(1,min((t-{z['t']:.2f})/0.3,({z['t'] + z['dur']:.2f}-t)/0.3)))" for z in zooms)


def vfilter(info, layout, fx, ass, zoom, w=W, h=H, zooms=(), sharp=False, split=None, broll=(), first_extra=1, logo=None, fade=None):
    """filter_complex ending in [v]. fx: crop position expression. fill = 9:16 crop on the subject; fit = blurred backdrop + full frame;
    split = two people stacked (split=(left_x, right_x)). broll: [{t,dur}] overlays from inputs first_extra.. ."""
    z = ""
    if zoom or zooms:
        ze = zoom_expr(zoom or 1, zooms)
        z = f",scale=w='trunc({w}*({ze})/2)*2':h='trunc({h}*({ze})/2)*2':eval=frame,crop={w}:{h}"
    sc = "flags=lanczos" + (",unsharp=5:5:0.5" if sharp else "")
    hh = h // 2
    if split:
        cw = f"ih*{w / hh:.4f}"
        a = (info["height"] * w / hh) / info["width"]
        cx = lambda v: f"clip(({v:.3f}-{a / 2:.4f})/{max(1 - a, 0.01):.4f},0,1)"
        base = (f"[0:v]split[a][b];[a]crop={cw}:ih:'(iw-{cw})*{cx(split[0])}':0,scale={w}:{hh}:{sc}[t];"
                f"[b]crop={cw}:ih:'(iw-{cw})*{cx(split[1])}':0,scale={w}:{hh}:{sc}[u];[t][u]vstack[base]")
    elif layout == "fit":
        base = (f"[0:v]split[a][b];[a]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},boxblur=30:5[bg];"
                f"[b]scale={w}:-2:{sc}[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2{z}[base]")
    elif info["width"] / max(1, info["height"]) <= 9 / 16 + 0.01:  # already vertical
        base = f"[0:v]scale={w}:{h}:force_original_aspect_ratio=increase:{sc},crop={w}:{h}{z}[base]"
    else:
        a = info["height"] * 9 / 16 / info["width"]  # crop width as a share of frame width; fx is where the SUBJECT is (0 left..1 right), so centre the crop on it
        base = f"[0:v]crop=ih*9/16:ih:'(iw-ih*9/16)*clip((({fx})-{a / 2:.4f})/{1 - a:.4f},0,1)':0,scale={w}:{h}:{sc}{z}[base]"
    cur = "base"
    for i, b in enumerate(broll):  # picture over the upper third while the keyword is spoken
        k = first_extra + i
        base += (f";[{k}:v]setpts=PTS-STARTPTS+{b['t']}/TB,scale={w}:-2[br{i}];"
                 f"[{cur}][br{i}]overlay=0:{int(h * .1)}:enable='between(t,{b['t']},{b['t'] + b['dur']})'[o{i}]")
        cur = f"o{i}"
    if logo:  # fixed corner overlay, under the captions
        k = first_extra + len(broll)
        sc = int(w * logo.get("scale", 0.14))
        mx = int(w * 0.04)
        pos = {"tl": f"{mx}:{mx + int(h * .05)}", "tr": f"W-w-{mx}:{mx + int(h * .05)}", "bl": f"{mx}:H-h-{int(h * .26)}", "br": f"W-w-{mx}:H-h-{int(h * .26)}"}[logo.get("pos", "tr")]
        base += f";[{k}:v]scale={sc}:-2[lg];[{cur}][lg]overlay={pos}[ol]"
        cur = "ol"
    base += f";[{cur}]{'subtitles=' + ass + ':fontsdir=fonts' if ass else 'null'}"
    if fade:  # soft video fade in/out (opt-in)
        base += f",fade=t=in:st=0:d=0.12,fade=t=out:st={max(0.0, fade - 0.12):.2f}:d=0.12"
    return base + "[v]"


def afilter(has_audio, clean, music_idx, music_db, dur=None):
    """Audio graph ending in [a]: optional cleanup, optional ducked music, loudness normalisation to -14 LUFS (resampled to 48 kHz: loudnorm
    otherwise upsamples to 192 kHz), and 15/30 ms edge fades so a cut never clicks."""
    if not has_audio:
        return None
    voice = "highpass=f=80,afftdn=nf=-25," if clean else ""
    edge = f",aresample=48000,afade=t=in:st=0:d=0.015" + (f",afade=t=out:st={max(0.0, dur - 0.03):.3f}:d=0.03" if dur else "")
    if music_idx is None:
        return f"[0:a]{voice}loudnorm=I=-14:TP=-1.5:LRA=11{edge}[a]"
    return (f"[0:a]{voice}asplit[v1][v2];[{music_idx}:a]volume={music_db}dB[m];[m][v2]sidechaincompress=threshold=0.03:ratio=8:attack=20:release=400[d];"
            f"[v1][d]amix=inputs=2:duration=first:normalize=0,loudnorm=I=-14:TP=-1.5:LRA=11{edge}[a]")


def layout_is_fill(clip, info):
    return clip.get("layout", "fill") == "fill" and info["width"] / max(1, info["height"]) > 9 / 16 + 0.01


def plan_framing(src, clip, cr, info, spk=None):
    """Detect once per clip (cached in the plan, not in the render key). A steady two-person shot whose speaker could not be
    decided gets the stacked split so both stay visible."""
    s, e = clip["final_start"], clip["final_end"]
    if clip.get("focus_auto") is not None and len(clip["focus_auto"]) == len(cr) + 1 and "split_auto" in clip:
        return
    edges = [s] + [s + c for c in cr] + [e]
    shots, people = [], []
    for a, b in zip(edges, edges[1:]):
        kf, fr = shot_framing(src, a, b, s, spk)
        shots.append([list(k) for k in kf])
        people += fr
    clip["focus_auto"] = shots
    pair = extras.pair_of(people)
    follows = any(len(kf) > 1 for kf in shots)
    clip["split_auto"] = list(pair) if (pair and not cr and not follows) else None


def _speaker_lookup(spk):
    """spk timeline [(t, track)] -> function(t) -> track index of the nearest earlier window (None if > 2.5 s away)."""
    tl = sorted((spk or {}).get("tl") or []) if (spk or {}).get("reliable") else []
    if not tl:
        return None
    ts = [t for t, _ in tl]
    import bisect
    def at(t):
        i = bisect.bisect_right(ts, t) - 1
        return tl[i][1] if i >= 0 and t - ts[i] <= 2.5 else None
    return at


def render_clip(src, clip, out_dir, words, info, draft=False, force=False, cuts=(), spk=None, opts=None):
    """clip: plan entry. cuts: absolute scene-cut times. opts: job-level render options (default caption style, style file, logo, fade, quality, src_fp).
    Returns the output path. The cache key covers the clip spec, the SOURCE fingerprint and every resolved option."""
    opts = opts or {}
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    cap = clip.get("caption") or {}
    sty = cap.get("style") or opts.get("style") or "bold_pop"  # clip override > job default
    sfile = opts.get("style_file")
    spec = {k: clip.get(k) for k in ("final_start", "final_end", "layout", "zoom", "zooms", "focus_x", "id", "broll", "music", "audio", "upscale", "interpolate")}
    spec.update(style=sty, scale=cap.get("scale", 1.0), style_file=sha256_file(sfile) if sfile and Path(sfile).is_file() else None, draft=draft,
                src=opts.get("src_fp"), logo=opts.get("logo"), fade=opts.get("fade"), quality=opts.get("quality"), intro=opts.get("intro"), outro=opts.get("outro"),
                reliable_spk=bool((spk or {}).get("reliable")))
    out = out_dir / f"clip_{clip['id']:02d}{'_draft' if draft else ''}_{digest(spec)}.mp4"
    if out.exists() and not force:
        return out
    for old in out_dir.glob(f"clip_{clip['id']:02d}{'_draft' if draft else ''}_*.mp4"):
        old.unlink()  # stale render of an earlier spec
    s, e = clip["final_start"], clip["final_end"]
    dur = e - s
    cw = [w for w in words if s - 0.01 <= w["s"] < e - 0.05]  # by START: a word that begins after the cut is not in the clip
    ass, clip["captions_n"], clip["captions_overflow"] = None, 0, 0
    if sty != "none" and cw:
        st = captions.style(sty, sfile)
        txt = captions.build(cw, s, st, cap.get("scale", 1.0), speaker_at=_speaker_lookup(spk))
        (out_dir / f"clip_{clip['id']:02d}.ass").write_text(txt, encoding="utf-8")
        captions.install_fonts(out_dir)
        ass = f"clip_{clip['id']:02d}.ass"
        clip["captions_n"], clip["captions_overflow"] = txt.count("\nDialogue:"), captions.overflow(cw, st, cap.get("scale", 1.0))
        if st.get("font_is_system"):
            note_skipped("caption font", f"{st['font']} is not bundled: libass uses whatever the system has")
    cr = shot_cuts(s, e, cuts)
    split = None
    if clip.get("focus_x") is not None:  # explicit user value wins; auto-detected framing is not part of the cache key
        fx = f"{clip['focus_x']:.3f}"
    else:
        plan_framing(src, clip, cr, info, spk)
        fx = crop_expr(clip["focus_auto"], cr)
        if clip.get("layout", "fill") == "fill" and clip.get("split_auto") and layout_is_fill(clip, info):
            split = tuple(clip["split_auto"])
    zooms = [z for z in clip.get("zooms") or [] if z["t"] >= 0 and z["t"] + z["dur"] <= dur and not any(z["t"] < c < z["t"] + z["dur"] for c in cr)]
    if split:
        zooms = []
    # --- source: original, or an AI-upscaled copy of just this segment
    up = (H / info["height"]) if layout_is_fill(clip, info) else (W / max(1, info["width"]))
    low = up >= 1.5
    ss, inp, vinfo = s, os.path.abspath(src), info
    if low and not draft and clip.get("upscale", "auto") == "ai" and upscale.available():  # opt-in: see upscale.py
        inp = str(upscale.upscale_clip(src, s, e, out_dir, info["fps"]))
        ss, vinfo = 0.0, probe(inp)
        low = False
    if clip.get("interpolate") and not draft and upscale.rife_available():
        a0 = ss if inp == os.path.abspath(src) else 0.0
        inp = str(upscale.interpolate_clip(inp, a0, a0 + dur, out_dir, vinfo["fps"]))
        ss, vinfo = 0.0, probe(inp)
    clip["warn"] = ["low_res_source"] if low else []
    enc = encoder()
    qual = {"preset": "veryfast", "crf": 28} if draft else (opts.get("quality") or {"preset": "medium", "crf": 20})
    extra, broll = [], [b for b in clip.get("broll") or [] if b["t"] + b["dur"] <= dur and os.path.exists(b["file"])]
    for b in broll:
        extra += (["-loop", "1"] if Path(b["file"]).suffix.lower() in (".png", ".jpg", ".jpeg", ".webp") else []) + ["-i", b["file"]]
    logo = {"file": opts["logo"], "pos": opts.get("logo_pos", "tr")} if opts.get("logo") and Path(opts["logo"]).is_file() else None
    if logo:
        extra += ["-i", logo["file"]]
    mus = clip.get("music") if clip.get("music") and os.path.exists(clip["music"]["file"]) else None
    if mus:
        extra += ["-stream_loop", "-1", "-i", mus["file"]]
    vf = vfilter(vinfo, clip.get("layout", "fill"), fx, ass, clip.get("zoom"), *((540, 960) if draft else (W, H)),
                 zooms=zooms, sharp=low, split=split, broll=broll, logo=logo, fade=dur if opts.get("fade") else None)
    af = afilter(info["audio"], clip.get("audio") == "clean", 1 + len(broll) + (1 if logo else 0) if mus else None, (mus or {}).get("db", -22), dur)

    def go(codec):
        cmd = [ffmpeg(), "-hide_banner", "-y", "-ss", f"{ss:.3f}", "-i", inp, *extra, "-t", f"{dur:.3f}",
               "-filter_complex", vf + (";" + af if af else ""), "-map", "[v]", *(["-map", "[a]"] if af else []),
               "-c:v", codec, *ENC_ARGS[codec](qual), "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", out.name]
        return run(cmd, cwd=out_dir, timeout=7200)
    r = go(enc)
    if r.returncode and enc != "libx264":  # a hardware encoder failed on this job (resolution/profile limits): fall back, once
        note_skipped(f"hardware encoder {enc}", "failed during render; used libx264")
        r = go("libx264")
    if r.returncode:
        out.unlink(missing_ok=True)
        raise RuntimeError(f"ffmpeg failed for clip {clip['id']}: {r.stderr[-600:]}")
    if opts.get("intro") or opts.get("outro"):
        _wrap(out, opts.get("intro"), opts.get("outro"), out_dir, qual)
    return out


def _wrap(out, intro, outro, out_dir, qual):
    """Optional intro/outro: each is scaled/padded to 1080x1920@30 and given silent audio if it has none, then concatenated (re-encode)."""
    parts = [p for p in (intro, str(out), outro) if p and Path(p).is_file()]
    if len(parts) < 2:
        return
    ins, fl = [], []
    for i, p in enumerate(parts):
        ins += ["-i", p]
        fl.append(f"[{i}:v]scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30,format=yuv420p[v{i}]")
        fl.append(f"[{i}:a]aresample=48000,aformat=channel_layouts=stereo[a{i}]" if probe(p)["audio"] else f"anullsrc=r=48000:cl=stereo,atrim=duration={max(probe(p)['duration'], 0.1):.2f}[a{i}]")
    n = len(parts)
    tmp = Path(out_dir) / ("wrap_" + Path(out).name)
    r = run([ffmpeg(), "-hide_banner", "-y", *ins, "-filter_complex", ";".join(fl) + ";" + "".join(f"[v{i}][a{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=1[v][a]",
             "-map", "[v]", "-map", "[a]", "-c:v", encoder(), *ENC_ARGS[encoder()](qual), "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart", tmp], timeout=3600)
    if r.returncode:
        note_skipped("intro/outro", r.stderr[-200:])
        tmp.unlink(missing_ok=True)
        return
    os.replace(tmp, out)


def qc(path, clip, draft=False, intro_outro=False):
    """Render-level checks -> (issues, info, warnings). Issues fail the clip; warnings are advisory (e.g. a frozen picture can be legitimate).
    Boundary/duplicate checks run earlier in boundaries.validate and the plan stage."""
    issues, warns, info = [], [], probe(path)
    w, h = (540, 960) if draft else (W, H)
    if (info["width"], info["height"]) != (w, h):
        issues.append(f"bad_size:{info['width']}x{info['height']}")
    if not info["audio"]:
        issues.append("no_audio")
    want = clip["final_end"] - clip["final_start"]
    if not intro_outro and abs(info["duration"] - want) > 0.6:
        issues.append(f"duration_off:{info['duration']:.1f}!={want:.1f}")
    r = run([ffmpeg(), "-hide_banner", "-v", "error", "-i", path, "-f", "null", "-"])
    if r.returncode or r.stderr.strip():
        issues.append("decode_error")
    bd = run([ffmpeg(), "-hide_banner", "-i", path, "-vf", "blackdetect=d=0.5:pix_th=0.05,freezedetect=n=-60dB:d=4", "-an", "-f", "null", "-"]).stderr
    if re.search(r"black_start", bd):
        issues.append("black_frames")
    if re.search(r"freeze_start", bd):
        warns.append("frozen_picture")
    sd = run([ffmpeg(), "-hide_banner", "-i", path, "-af", "silencedetect=n=-45dB:d=1.2", "-vn", "-f", "null", "-"]).stderr
    starts, ends = re.findall(r"silence_start: ([\d.]+)", sd), re.findall(r"silence_end: ([\d.]+)", sd)
    if starts and (len(ends) < len(starts) or float(ends[-1]) >= info["duration"] - 0.1):
        issues.append("silent_tail")
    if clip.get("captions_overflow"):
        issues.append("caption_overflow")
    if clip.get("captions_n") == 0 and clip.get("expect_captions"):
        issues.append("captions_missing")
    return issues, info, warns
