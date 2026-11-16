"""clipper CLI: the single agent-facing interface (MCP/HTTP wrappers call these same commands)."""
import argparse, importlib, importlib.util, json, os, shutil, sys
from pathlib import Path
from . import boundaries as B, job as J, render as R
from .util import ffmpeg, hms, jload, jsave, safe_path, validate_source

ROOT = os.environ.get("CLIPPER_JOBS", "clipper-jobs")
STYLE_CHOICES = ["bold_pop", "karaoke", "clean_lower", "boxed", "minimal_white", "highlight_words", "podcast_speaker", "gaming_bold", "cinematic", "custom", "none"]
PROFILE_CHOICES = ["podcast", "interview", "gaming", "irl", "comedy", "sports", "auto"]
HELP = {
    "ingest": ("download/inspect the source", "clipper run SRC --export DIR"),
    "transcribe": ("local Whisper (resumable per 20-min chunk)", "pass --transcript words.json to skip"),
    "signals": ("audio/visual/speech/chat/heatmap series", "pass --chat chat.jsonl when you have chat"),
    "candidates": ("fuse signals, snap boundaries, dedupe (event-only clips for speech-free highlights)", ""),
    "select": ("LLM-judge borderline candidates under a hard budget, write plan.json", "REVIEW plan.json with the user if they asked to; fix with `clipper edit`"),
    "speakers": ("whole-video active-speaker map (cached)", "--speakers off to skip"),
    "render": ("ffmpeg per clip (cached per clip)", "use --draft for a quick 540x960 look first"),
    "qc": ("boundary/size/audio/black-frame/frozen/decode checks", "`clipper polish JOB` fixes mechanical issues"),
}


def load_config():
    """Defaults from ./clipper.toml or ~/.config/clipper/config.toml (Python 3.11+). Environment/CLI always win. Never holds secrets."""
    try:
        import tomllib
    except ImportError:
        return {}
    for p in (Path("clipper.toml"), Path.home() / ".config" / "clipper" / "config.toml"):
        if p.is_file():
            try:
                return {k.replace("-", "_"): v for k, v in tomllib.loads(p.read_text()).items() if k not in ("openai_api_key", "api_key")}
            except Exception as e:
                print(f"ignoring unreadable config {p}: {e}", file=sys.stderr)
    return {}


COMMON = ("clips", "profile", "mode", "style", "model", "chat", "cookies", "transcript", "draft", "upscale", "speakers", "max_calls", "max_tokens", "max_usd",
          "quality", "style_file", "logo", "fade")


def opts_of(a):
    return {k: getattr(a, k, None) for k in COMMON if getattr(a, k, None) is not None and not (k in ("draft", "fade") and getattr(a, k) is False)}


def get(a):
    j = J.Job(ROOT, a.job)
    if not j.p("job.json").exists():
        sys.exit(f"no job {a.job} in {ROOT}")
    return j


def cmd_process(a):
    j = J.create(a.src, ROOT, **opts_of(a))
    if a.force_from:
        j.invalidate(a.force_from)
    nxt = J.run(j, a.stop_after)
    print(f"job {j.id}  next={nxt or 'complete'}  ->  clipper status {j.id}")


def cmd_run(a):
    """Recorded video, start to finish: process -> render -> editor-critic polish -> export. No questions, sensible defaults."""
    from . import flow
    o = opts_of(a)
    o.pop("clips", None)
    j, fr = flow.run_video(a.src, ROOT, clips=a.clips or 10, export_dir=a.export or None,
                           auto_polish=not a.no_polish, **o)
    print(json.dumps({"job": j.id, **fr}, indent=1))


def cmd_watch(a):
    """Livestream, start to finish: record, detect, finalize automatically, process, polish, export."""
    from . import flow
    o = opts_of(a)
    o.pop("clips", None)
    j, fr = flow.watch_stream(a.src, a.out, clips=a.clips or 10, auto_finalize=not a.no_finalize, auto_polish=not a.no_polish, latency=a.latency, chat_file=a.chat_file,
                              channel=a.channel, max_seconds=a.max_seconds, resume=a.resume, warmup=a.warmup_bins, pre=a.pre, post=a.post, stt=not a.no_stt, **o)
    print(json.dumps({"job": j.id if j else None, **fr}, indent=1))


def cmd_status(a):
    j = get(a)
    st = {s: j.done(s) for s in J.STAGES}
    plan, rep = jload(j.p("plan.json")), jload(j.p("report.json"))
    out = {"job": j.id, "stages": st, "next": J.next_step(j)}
    if plan:
        out["clips"] = [{k: c.get(k) for k in ("id", "final_score", "title", "issues", "layout", "dropped")} | {"start": hms(c["final_start"]), "end": hms(c["final_end"]), "text": c["text"][:90]} for c in plan["clips"]]
        out["cost"] = plan["cost"]
    if rep:
        out["qc"] = [{"id": c["id"], "pass": c["pass"], "issues": c["issues"]} for c in rep["clips"]]
    print(json.dumps(out, indent=1))


def cmd_next(a):
    j = get(a)
    st = J.next_step(j)
    if not st:
        rep = jload(j.p("report.json"))
        bad = [c for c in rep["clips"] if not c["pass"]]
        crit = {f.stem for f in j.p("review", "critics").glob("*.json")} if j.p("review", "critics").exists() else set()
        if bad:
            print(f"NEXT: fix {[c['id'] for c in bad]} (see issues in status), then render")
        elif not crit:
            print(f"NEXT: clipper polish {j.id}   (editor-critic: critique, fix, re-render, max 3 rounds)")
        elif not crit & {"vision", "vision_unavailable"}:
            print(f"NEXT: clipper critique {j.id} --packet   (open every sheet, then --submit, or --unavailable WHY)")
        else:
            print(f"NEXT: show the user the verdicts, then clipper export {j.id}")
        return
    what, tip = HELP[st]
    print(f"NEXT: {st}  ({what})\nrun : clipper process {j.opts['src']!r} --stop-after {st}\ntip : {tip}")


def cmd_edit(a):
    """Structured edit ops on one clip; only that clip re-renders."""
    j = get(a)
    ops = {k: v for k, v in dict(
        start_shift=a.start_shift, end_shift=a.end_shift, captions=a.captions, caption_scale=a.caption_scale, zoom=a.zoom, layout=a.layout,
        focus_x=a.focus_x, drop=a.drop or None, broll_dir=a.broll_dir, no_broll=a.no_broll or None, music=a.music, music_db=a.music_db,
        audio=a.audio, upscale=a.upscale, interpolate=a.interpolate or None, zooms_clear=a.no_zooms or None, logo=a.logo, fade=a.fade or None).items() if v is not None}
    if a.zoom_at is not None:
        ops["zoom_add"] = {"t": a.zoom_at, "dur": a.zoom_for, "amt": a.zoom_amt}
    c = J.edit_clip(j, a.clip, ops)
    print(f"clip {a.clip}: {hms(c['final_start'])}-{hms(c['final_end'])} issues={c.get('issues')}  -> clipper render {j.id}")


def cmd_render(a):
    j = get(a)
    if a.draft is not None:
        o = j.opts
        jsave(j.p("job.json"), {**o, "draft": a.draft})
    j.invalidate("render")
    J.run(j, "qc")
    rep = jload(j.p("report.json"))
    for c in rep["clips"]:
        print(f"clip {c['id']:>2} {'OK ' if c['pass'] else 'FIX'} {c['start']}-{c['end']} {c['issues'] or ''} {c['file']}")


def show_critique(j):
    from . import critics as C
    for n, r in C.load_reports(j).items():
        for cid, cl in r["clips"].items():
            for f in cl["findings"]:
                print(f"{f['severity'].upper():7} clip {cid} {n}/{f['code']}" + (f" @{f['timestamp_sec']}s" if f.get("timestamp_sec") is not None else "") +
                      f"\n        {f['problem']}\n        evidence: {f['evidence']}\n        expected: {f['expected']}\n        editor: {f['editor_note']}\n"
                      f"        fix [{f['fix_target']}]: {C.edit_cli(j.id, f) or f['fix']}")
        if r.get("assessment") == "NOT PERFORMED":
            print(f"NOT PERFORMED  vision critic: {r['reason']}")
    print("--- editor's verdict (Murch-weighted over what was assessed)")
    for cid, v in C.summarize(j).items():
        print(f"clip {cid}: {v['verdict'].upper():6} {v['overall']}/100  assessed={v['assessed']}" + (f"  NOT ASSESSED: {v['unassessed']}" if v["unassessed"] else ""))


def cmd_critique(a):
    from . import critics as C
    j = get(a)
    if not j.done("render"):
        sys.exit("render first: clipper render JOB (critics read the real files)")
    if a.packet:
        imgs = C.vision_packet(j)
        print(f"open every image, then fill {j.p('review', 'findings.template.json')} and run: clipper critique {j.id} --submit FILE\nread {j.p('review', 'packet.md')}\n" + "\n".join(imgs))
    elif a.submit:
        C.vision_submit(j, a.submit)
        show_critique(j)
    elif a.unavailable:
        C.vision_unavailable(j, a.unavailable)
        print("recorded: vision checks were NOT performed")
    else:
        C.run_critics(j, a.names or None)
        show_critique(j)


def cmd_fix(a):
    from . import critics as C
    j = get(a)
    done, esc = C.apply_fixes(j)
    print("\n".join(["applied:"] + done) if done else "nothing mechanical to apply")
    if esc:
        print("needs a human/agent decision:\n  " + "\n  ".join(esc))
    if done:
        print(f"next: clipper render {j.id}  then  clipper critique {j.id}")


def cmd_polish(a):
    """critique -> auto-fix -> re-render, max 3 rounds; then hand the rest to the user."""
    from . import critics as C, flow
    j = get(a)
    flow.polish(j, log=print)
    show_critique(j)
    print(f"\nIf findings remain after {C.MAX_ROUNDS} rounds, show them to the user. Eyes still needed: clipper critique {j.id} --packet")


def cmd_export(a):
    from . import flow
    j = get(a)
    meta, bad = flow.export(j, a.out or j.p("export"), a.all)
    print(f"exported {len(meta)} clips to {a.out or j.p('export')} ({len(bad)} failing clips not exported; use --all to include them)")


def cmd_bench(a):
    from . import bench
    for r in bench.run(a.golden, n=a.clips):
        print(json.dumps(r))


def cmd_sync_check(a):
    from . import live
    skew, ct, at = live.sync_check(a.out)
    print(f"busiest chat second t={ct}s, loudest audio second t={at}s: chat is {skew:+d}s {'later' if skew > 0 else 'earlier'} than the video.\n"
          f"Re-run with --latency <current {'+' if skew > 0 else ''}{skew}> (only meaningful if that loud moment is what chat reacted to).")


def cmd_monitor(a):
    from . import live
    cs = live.chat_source(a.src, a.channel, a.chat_file)
    if not cs:
        print("no chat adapter for this source: audio (+ speech) spikes only. Twitch: automatic. YouTube: set YOUTUBE_API_KEY. Kick/others: --chat-file relay.jsonl")
    sess = live.monitor(a.src, a.out, cs, a.latency, not a.no_record, max_seconds=a.max_seconds, stt=not a.no_stt, warmup=a.warmup_bins, pre=a.pre, post=a.post)
    print(f"stopped. Finalize + process automatically next time with `clipper watch`; now: clipper watch {a.src} --out {a.out} --resume")


def cmd_serve(a):
    from . import server
    server.http(a.port)


def cmd_mcp(a):
    from . import server
    server.mcp()


def cmd_kick_relay(a):
    from . import kick
    kick.serve(a.port, a.out, bind=a.bind)


def cmd_ingest_server(a):
    from . import ingest_server
    p = ingest_server.start(a.port, {"MTX_RTMPADDRESS": f"{a.bind}:{a.port}"})
    print(f"RTMP ingest on {a.bind}:{a.port}. Push to rtmp://<host>:{a.port}/live/<key>, then: clipper watch rtmp://127.0.0.1:{a.port}/live/<key> --out live-out", flush=True)
    try:
        p.wait()
    except KeyboardInterrupt:
        p.terminate()


def capabilities():
    """What works on THIS machine (used by `doctor --json` and the MCP get_system_capabilities tool)."""
    from . import models

    def can(m):
        try:
            importlib.import_module(m)
            return True
        except Exception:
            return False
    try:
        ff, enc = ffmpeg(), R.encoder()
    except SystemExit:
        ff, enc = None, None
    pk = {n: can(m) for n, m in (("yt-dlp", "yt_dlp"), ("faster-whisper", "faster_whisper"), ("opencv", "cv2"), ("numpy", "numpy"), ("torch", "torch"), ("speechbrain", "speechbrain"),
                                 ("panns-inference", "panns_inference"), ("rapidocr", "rapidocr_onnxruntime"), ("python_speech_features", "python_speech_features"))}
    pk["yt-dlp"] = pk["yt-dlp"] or bool(shutil.which("yt-dlp"))
    caps = {"ffmpeg": bool(ff), "encoder": enc, "packages": pk, "gpu_nvidia": bool(shutil.which("nvidia-smi")), "offline": models.offline(),
            "openai_key": bool(os.environ.get("OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY_FILE")), "youtube_api_key": bool(os.environ.get("YOUTUBE_API_KEY")),
            "tools": {t: bool(shutil.which(t)) for t in ("streamlink", "mediamtx", "ollama", "realesrgan-ncnn-vulkan", "rife-ncnn-vulkan", "TwitchDownloaderCLI")},
            "models": models.status()}
    caps["features"] = {
        "transcribe_local": pk["faster-whisper"], "url_download": pk["yt-dlp"], "face_framing": pk["opencv"] and pk["numpy"],
        "speaker_following": pk["opencv"] and pk["speechbrain"] and pk["torch"], "sound_events": pk["panns-inference"] and pk["torch"],
        "ocr": pk["rapidocr"], "live_chat_twitch": True, "live_chat_youtube": caps["youtube_api_key"], "live_chat_kick": "relay-file or signed webhooks only",
        "llm_openai": caps["openai_key"], "llm_local_ollama": caps["tools"]["ollama"], "ai_upscale_opt_in": caps["tools"]["realesrgan-ncnn-vulkan"]}
    return caps


def cmd_doctor(a):
    caps = capabilities()
    if a.json:
        print(json.dumps(caps, indent=1))
        return
    fixes = {"yt-dlp": "pip install yt-dlp  (URL sources)", "faster-whisper": "pip install faster-whisper  (or pass --transcript; also enables live speech)",
             "opencv": "pip install opencv-python-headless  (if import fails with libGL: pip uninstall opencv-python && pip install --force-reinstall opencv-python-headless)",
             "numpy": "pip install numpy", "torch": "pip install torch  (CPU build is fine)", "speechbrain": "pip install speechbrain  (active-speaker following)",
             "panns-inference": "pip install panns-inference  (laughter/cheering signal)", "rapidocr": "pip install rapidocr-onnxruntime --no-deps (see INSTALLATION.md)",
             "python_speech_features": "pip install python_speech_features"}
    print(f"{'ok  ' if caps['ffmpeg'] else 'MISS'} ffmpeg" + ("" if caps["ffmpeg"] else "   -> install ffmpeg or `pip install imageio-ffmpeg`"))
    print(f"     encoder in use: {caps['encoder']} (test-encoded)")
    for n, ok in caps["packages"].items():
        print(f"{'ok  ' if ok else 'MISS'} {n}" + ("" if ok else f"   -> {fixes.get(n, '')}"))
    for t, ok in caps["tools"].items():
        print(f"{'ok  ' if ok else 'MISS'} {t}   (optional)")
    for k in ("openai_key", "youtube_api_key", "gpu_nvidia"):
        print(f"{'ok  ' if caps[k] else 'MISS'} {k}   (optional)")
    inst = [m for m in caps["models"] if m["installed"]]
    print(f"models: {len(inst)}/{len(caps['models'])} installed (clipper models list)")


def cmd_models(a):
    from . import models
    if a.action == "list":
        for m in models.status():
            print(f"{'ok  ' if m['verified'] else ('BAD ' if m['installed'] else '    ')} {m['name']:28s} {m['size_mb']:7.1f} MB  {m['license']}  ({m['needed_for']})")
        return
    names = [n for n in models.MANIFEST if not n.startswith("_")] if a.name in (None, "all") else [a.name]
    total = sum(models.MANIFEST[n]["size"] for n in names if n in models.MANIFEST and not models.dest(n).exists()) / 1e6
    if total > 100 and not a.yes:
        sys.exit(f"this would download {total:.0f} MB; re-run with --yes to confirm")
    for n in names:
        try:
            print(f"{n}: {models.ensure(n)}")
        except models.ModelUnavailable as e:
            print(f"{n}: UNAVAILABLE ({e})")


def cmd_styles(a):
    """List caption presets; --preview OUT.png renders each one over a sample frame (uses the bundled fonts)."""
    from . import captions
    names = [n for n in STYLE_CHOICES if n not in ("custom", "none")]
    if not a.preview:
        for n in names:
            st = captions.style(n)
            print(f"{n:16s} font={st['font']:18s} size={st['size']} mode={st['mode']} words/line={st['words']}")
        return
    from .util import run
    words = [{"w": w, "s": 0.0 + i * 0.3, "e": 0.28 + i * 0.3} for i, w in enumerate("This is the moment nobody saw coming".split())]
    work = Path(a.preview).resolve().parent / "_styles"
    work.mkdir(parents=True, exist_ok=True)
    captions.install_fonts(work)
    pngs = []
    for n in names:
        (work / f"{n}.ass").write_text(captions.build(words, 0.0, n), encoding="utf-8")
        r = run([ffmpeg(), "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=0x2b4a6f:s=1080x1920:d=2", "-vf", f"subtitles={n}.ass:fontsdir=fonts", "-ss", "1.0", "-frames:v", "1", f"{n}.png"], cwd=work)
        if r.returncode:
            print(f"{n}: preview failed: {r.stderr[-200:]}")
        else:
            pngs.append(work / f"{n}.png")
    ins = sum((["-i", str(p)] for p in pngs), [])
    run([ffmpeg(), "-y", "-v", "error", *ins, "-filter_complex", "".join(f"[{i}:v]scale=270:480[s{i}];" for i in range(len(pngs))) + "".join(f"[s{i}]" for i in range(len(pngs))) + f"hstack=inputs={len(pngs)}[o]", "-map", "[o]", "-frames:v", "1", a.preview])
    print(f"preview of {len(pngs)} styles written to {a.preview}")


def cmd_selftest(a):
    """Runs the offline test suite (unit + integration + agents; add --live for the RTMP end-to-end tests). Needs: pip install clipper[dev]."""
    try:
        import pytest
    except ImportError:
        sys.exit("pytest is not installed: pip install \"clipper[dev]\"")
    root = next((r for r in (Path.cwd(), *Path.cwd().parents, Path(__file__).resolve().parents[1]) if (r / "tests" / "test_unit.py").is_file()), None)  # works from an installed package too
    if root is None:
        sys.exit("tests/ not found: run `clipper selftest` from the source folder (the one containing tests/)")
    files = ["tests/test_unit.py", "tests/test_integration.py", "tests/test_agents.py"] + (["tests/test_live.py"] if a.live else [])
    sys.exit(pytest.main(["-q", "-p", "no:cacheprovider", "-m", "live or not live" if a.live else "not live", *[str(root / f) for f in files]]))


def add_common(s):
    s.add_argument("--clips", type=int)
    s.add_argument("--profile", choices=PROFILE_CHOICES, help="default: auto-detected from the content")
    s.add_argument("--mode", choices=["disabled", "local_only", "smart", "quality"], help="LLM use; default smart (OpenAI key if present, else Ollama if running, else local scoring)")
    s.add_argument("--style", choices=STYLE_CHOICES), s.add_argument("--style-file", help="JSON overrides for --style custom")
    s.add_argument("--model", help="light|balanced|quality|<whisper model>"), s.add_argument("--quality", choices=["draft", "normal", "high"])
    s.add_argument("--chat"), s.add_argument("--cookies"), s.add_argument("--transcript"), s.add_argument("--logo"), s.add_argument("--fade", action="store_true")
    s.add_argument("--draft", action="store_true"), s.add_argument("--speakers", choices=["auto", "off"])
    s.add_argument("--upscale", choices=["auto", "ai", "off"], help="auto: lanczos+sharpen (default). ai: Real-ESRGAN, opt-in, LOOK at a frame: the default model cartoonises faces")
    s.add_argument("--max-calls", type=int), s.add_argument("--max-tokens", type=int), s.add_argument("--max-usd", type=float, help="hard cap on OpenAI spend for the job (default 0.10)")


def main(argv=None):
    p = argparse.ArgumentParser("clipper", description="AI video clipping toolkit. One-shot: `clipper run URL --export out/`  or  `clipper watch LIVE_URL --out live/`")
    sp = p.add_subparsers(dest="cmd", required=True)
    s = sp.add_parser("run", help="recorded video, start to finish (process + render + polish + export)")
    s.add_argument("src"), add_common(s)
    s.add_argument("--export", nargs="?", const="", default=None, help="export folder (default: <job>/export)"), s.add_argument("--no-polish", action="store_true")
    s.add_argument("--auto-polish", action="store_true", help="(default on) accepted for compatibility")
    s.set_defaults(f=cmd_run)
    s = sp.add_parser("watch", help="livestream, start to finish (record + detect + auto-finalize + process + polish + export)")
    s.add_argument("src"), add_common(s)
    s.add_argument("--out", default="live-out"), s.add_argument("--channel"), s.add_argument("--chat-file")
    s.add_argument("--latency", type=float, default=8.0, help="chat-vs-video delay in seconds (measure with `clipper sync-check`)")
    s.add_argument("--max-seconds", type=float), s.add_argument("--no-stt", action="store_true"), s.add_argument("--no-finalize", action="store_true"), s.add_argument("--no-polish", action="store_true")
    s.add_argument("--auto-finalize", action="store_true", help="(default on) accepted for compatibility"), s.add_argument("--export", nargs="?", const="", default=None)
    s.add_argument("--resume", action="store_true", help="continue a session that was interrupted (finalize + process what was recorded)")
    s.add_argument("--warmup-bins", type=int, default=10), s.add_argument("--pre", type=float, default=120), s.add_argument("--post", type=float, default=60)
    s.set_defaults(f=cmd_watch)
    s = sp.add_parser("process", help="stage-by-stage control: download/transcribe/detect/rank (use `run` for everything)")
    s.add_argument("src"), add_common(s)
    s.add_argument("--stop-after", default="qc", choices=J.STAGES), s.add_argument("--force-from", choices=J.STAGES)
    s.set_defaults(f=cmd_process)
    for n, f in (("status", cmd_status), ("next", cmd_next)):
        sp.add_parser(n).add_argument("job"), sp.choices[n].set_defaults(f=f)
    s = sp.add_parser("edit")
    s.add_argument("job"), s.add_argument("clip", type=int)
    s.add_argument("--start-shift", type=float), s.add_argument("--end-shift", type=float)
    s.add_argument("--captions", choices=STYLE_CHOICES), s.add_argument("--caption-scale", type=float)
    s.add_argument("--zoom", type=float, help="1.0 = none"), s.add_argument("--layout", choices=["fill", "fit"])
    s.add_argument("--focus-x", type=float), s.add_argument("--drop", action="store_true")
    s.add_argument("--zoom-at", type=float, help="punch-in at clip-relative seconds (use on a named punchline)")
    s.add_argument("--zoom-for", type=float, default=2.0), s.add_argument("--zoom-amt", type=float, default=1.12), s.add_argument("--no-zooms", action="store_true")
    s.add_argument("--broll-dir", help="folder of images/videos named after spoken keywords (pizza.png)"), s.add_argument("--no-broll", action="store_true")
    s.add_argument("--music", help="your own royalty-cleared track; ducked under speech"), s.add_argument("--music-db", type=float, default=-22.0)
    s.add_argument("--interpolate", action="store_true", help="2x frames with RIFE (opt-in; look at a frame)"), s.add_argument("--audio", choices=["clean", "raw"], help="clean = high-pass + denoise")
    s.add_argument("--upscale", choices=["auto", "ai", "off"]), s.add_argument("--logo"), s.add_argument("--fade", action="store_true")
    s.set_defaults(f=cmd_edit)
    s = sp.add_parser("render")
    s.add_argument("job"), s.add_argument("--draft", action=argparse.BooleanOptionalAction, default=None)
    s.set_defaults(f=cmd_render)
    s = sp.add_parser("export")
    s.add_argument("job"), s.add_argument("--out"), s.add_argument("--all", action="store_true")
    s.set_defaults(f=cmd_export)
    s = sp.add_parser("monitor", help="low-level: record + detect only (use `watch` for the full automatic workflow)")
    s.add_argument("src"), s.add_argument("--out", default="live-out"), s.add_argument("--channel"), s.add_argument("--chat-file")
    s.add_argument("--latency", type=float, default=8.0), s.add_argument("--no-record", action="store_true")
    s.add_argument("--warmup-bins", type=int, default=10), s.add_argument("--pre", type=float, default=120), s.add_argument("--post", type=float, default=60)
    s.add_argument("--max-seconds", type=float), s.add_argument("--no-stt", action="store_true")
    s.set_defaults(f=cmd_monitor)
    s = sp.add_parser("serve", help="local HTTP API (127.0.0.1, bearer token printed at start)")
    s.add_argument("--port", type=int, default=8765)
    s.set_defaults(f=cmd_serve)
    sp.add_parser("mcp", help="MCP server over stdio").set_defaults(f=cmd_mcp)
    s = sp.add_parser("ingest-server", help="local RTMP ingest (MediaMTX) for OBS/ffmpeg pushes")
    s.add_argument("--port", type=int, default=1935), s.add_argument("--bind", default="127.0.0.1", help="use 0.0.0.0 only on a trusted network")
    s.set_defaults(f=cmd_ingest_server)
    s = sp.add_parser("kick-relay", help="receive Kick's official chat webhooks (signature-verified) into a jsonl for --chat-file")
    s.add_argument("--port", type=int, default=8787), s.add_argument("--out", default="kick-chat.jsonl"), s.add_argument("--bind", default="127.0.0.1", help="expose via a tunnel; 0.0.0.0 only if you must")
    s.set_defaults(f=cmd_kick_relay)
    s = sp.add_parser("sync-check", help="measure chat-vs-video skew from a finished run")
    s.add_argument("out"), s.set_defaults(f=cmd_sync_check)
    s = sp.add_parser("critique", help="editor-critic review of the real files (auto critics, or --packet/--submit/--unavailable for the vision critic)")
    s.add_argument("job"), s.add_argument("names", nargs="*", help="cut story pace captions framing frames audio"), s.add_argument("--packet", action="store_true")
    s.add_argument("--submit"), s.add_argument("--unavailable", metavar="WHY")
    s.set_defaults(f=cmd_critique)
    sp.add_parser("fix", help="apply the critics' mechanical edits (once per clip+issue)").add_argument("job"), sp.choices["fix"].set_defaults(f=cmd_fix)
    sp.add_parser("polish", help="critique -> fix -> re-render, max 3 rounds").add_argument("job"), sp.choices["polish"].set_defaults(f=cmd_polish)
    s = sp.add_parser("bench", help="score selection/boundaries against reviewed reference timestamps")
    s.add_argument("golden"), s.add_argument("--clips", type=int)
    s.set_defaults(f=cmd_bench)
    s = sp.add_parser("doctor", help="what works on this machine")
    s.add_argument("--json", action="store_true"), s.set_defaults(f=cmd_doctor)
    s = sp.add_parser("models", help="list/fetch optional AI models (verified, resumable; large ones need --yes)")
    s.add_argument("action", choices=["list", "fetch"]), s.add_argument("name", nargs="?"), s.add_argument("--yes", action="store_true")
    s.set_defaults(f=cmd_models)
    s = sp.add_parser("styles", help="list caption presets; --preview sheet.png renders them")
    s.add_argument("--preview"), s.set_defaults(f=cmd_styles)
    s = sp.add_parser("selftest", help="run the offline test suite (pytest)")
    s.add_argument("--live", action="store_true", help="also run the RTMP/MediaMTX end-to-end tests (minutes)"), s.set_defaults(f=cmd_selftest)
    a = p.parse_args(argv)
    for k, v in load_config().items():  # config-file defaults fill only what the user did not set
        if hasattr(a, k) and getattr(a, k) in (None, False):
            setattr(a, k, v)
    for k in ("src",):
        if hasattr(a, k):
            try:
                validate_source(getattr(a, k))
            except ValueError as e:
                sys.exit(f"invalid source: {e}")
    a.f(a)


if __name__ == "__main__":
    main()
