"""clipper CLI: the single agent-facing interface (an MCP/HTTP wrapper would just call these functions)."""
import argparse, importlib.util, json, os, shutil, sys
from pathlib import Path
from . import boundaries as B, job as J, render as R
from .util import ffmpeg, hms, jload, jsave

ROOT = os.environ.get("CLIPPER_JOBS", "clipper-jobs")
HELP = {
    "ingest": ("download/inspect the source", "clipper process SRC --stop-after select"),
    "transcribe": ("local Whisper (resumable per 20-min chunk)", "pass --transcript words.json to skip"),
    "signals": ("audio/visual/speech/chat series", "pass --chat chat.jsonl when you have chat"),
    "candidates": ("fuse signals, snap boundaries, dedupe", ""),
    "select": ("LLM-judge borderline candidates, write plan.json", "REVIEW plan.json with the user; fix with `clipper edit`"),
    "speakers": ("whole-video active-speaker map (cached)", "--speakers off to skip"),
    "render": ("ffmpeg per clip (cached per clip)", "use --draft for a quick 540x960 look first"),
    "qc": ("boundary/size/audio/black-frame/decode checks", "fix every issue via `clipper edit`, then `clipper render`"),
}


def get(a):
    j = J.Job(ROOT, a.job)
    if not j.p("job.json").exists():
        sys.exit(f"no job {a.job} in {ROOT}")
    return j


def cmd_process(a):
    j = J.create(a.src, ROOT, clips=a.clips, profile=a.profile, mode=a.mode, style=a.style, model=a.model, chat=a.chat,
                 max_calls=a.max_calls, max_tokens=a.max_tokens, cookies=a.cookies, transcript=a.transcript, upscale=a.upscale, speakers=a.speakers, draft=a.draft or None)
    if a.force_from:
        j.invalidate(a.force_from)
    nxt = J.run(j, a.stop_after)
    print(f"job {j.id}  next={nxt or 'complete'}  ->  clipper status {j.id}")


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
        audio=a.audio, upscale=a.upscale, interpolate=a.interpolate or None, zooms_clear=a.no_zooms or None).items() if v is not None}
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


def cmd_export(a):
    j = get(a)
    rep, plan = jload(j.p("report.json")), jload(j.p("plan.json"))
    if j.opts.get("draft"):
        sys.exit("last render was a draft; run `clipper render JOB --no-draft` first")
    out = Path(a.out or j.p("export"))
    out.mkdir(parents=True, exist_ok=True)
    meta = []
    for c in rep["clips"]:
        if c["file"] and (c["pass"] or a.all):
            shutil.copy(c["file"], out / Path(c["file"]).name)
            p = next(p for p in plan["clips"] if p["id"] == c["id"])
            meta.append({"file": Path(c["file"]).name, "title": p.get("title", ""), "start": c["start"], "end": c["end"], "text": p["text"], "score": c["score"]})
    jsave(out / "metadata.json", {"clips": meta, "cost": plan["cost"]})
    print(f"exported {len(meta)} clips to {out} (skipped failing clips; use --all to include them)")


def cmd_monitor(a):
    from . import live
    cs = live.chat_source(a.src, a.channel, a.chat_file)
    if not cs:
        print("no chat adapter for this source: audio (+ speech) spikes only. Twitch: automatic. YouTube: set YOUTUBE_API_KEY. Kick/others: --chat-file relay.jsonl")
    live.monitor(a.src, a.out, cs, a.latency, not a.no_record, max_seconds=a.max_seconds, stt=not a.no_stt, warmup=a.warmup_bins, pre=a.pre, post=a.post)


def cmd_doctor(a):
    def has_mod(m):  # really import: a package can be installed yet broken (e.g. opencv without libGL)
        try:
            importlib.import_module(m)
            return True
        except Exception:
            return False
    try:
        ff = ffmpeg()
    except SystemExit:
        ff = None
    rows = [("ffmpeg", bool(ff), "install ffmpeg or `pip install imageio-ffmpeg`"),
            ("yt-dlp", bool(shutil.which("yt-dlp")), "pip install yt-dlp  (URL sources)"),
            ("faster-whisper", has_mod("faster_whisper"), "pip install faster-whisper  (or pass --transcript; also enables live speech)"),
            ("panns-inference (sound events)", has_mod("panns_inference"), "pip install panns-inference  (optional: laughter/cheering signal)"),
            ("resemblyzer (who is speaking)", has_mod("resemblyzer"), "pip install resemblyzer  (optional: active-speaker following)"),
            ("rapidocr (on-screen text)", has_mod("rapidocr_onnxruntime"), "pip install rapidocr-onnxruntime  (optional)"),
            ("Real-ESRGAN upscaler", bool(shutil.which("realesrgan-ncnn-vulkan")), "optional: realesrgan-ncnn-vulkan on PATH for low-res sources"),
            ("YOUTUBE_API_KEY (live chat)", bool(os.environ.get("YOUTUBE_API_KEY")), "optional: your own free YouTube Data API key"),
            ("opencv (faces)", has_mod("cv2"), "pip install opencv-python-headless; if import fails with libGL: pip uninstall opencv-python && pip install --force-reinstall opencv-python-headless"),
            ("streamlink", bool(shutil.which("streamlink")), "pip install streamlink  (optional; yt-dlp -g is the fallback)"),
            ("nvidia GPU", bool(shutil.which("nvidia-smi")), "optional; CPU uses int8 small model"),
            ("OPENAI_API_KEY", bool(os.environ.get("OPENAI_API_KEY")), "optional; without it everything runs locally")]
    for n, ok, fix in rows:
        print(f"{'ok  ' if ok else 'MISS'} {n}" + ("" if ok else f"   -> {fix}"))
    if ff:
        enc = R.encoder()
        print(f"     encoder: {enc}")


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
    from . import critics as C
    j = get(a)
    for r in range(1, C.MAX_ROUNDS + 1):
        J.run(j, "qc", log=lambda *_: None)
        C.run_critics(j)
        done, esc = C.apply_fixes(j)
        print(f"round {r}: applied {len(done)}" + "".join(f"\n  {d}" for d in done))
        if not done:
            break
    J.run(j, "qc", log=lambda *_: None)
    C.run_critics(j)
    show_critique(j)
    print(f"\nIf findings remain after {C.MAX_ROUNDS} rounds, show them to the user. Eyes still needed: clipper critique {j.id} --packet")


def cmd_bench(a):
    from . import bench
    for r in bench.run(a.golden, n=a.clips):
        print(json.dumps(r))


def cmd_serve(a):
    from . import server
    server.http(a.port)


def cmd_mcp(a):
    from . import server
    server.mcp()


def cmd_ingest_server(a):
    from . import ingest_server
    p = ingest_server.start(a.port, {"MTX_RTMPADDRESS": f"{a.bind}:{a.port}"})
    print(f"RTMP ingest on {a.bind}:{a.port}. Push to rtmp://<host>:{a.port}/live/<key>, then: clipper monitor rtmp://127.0.0.1:{a.port}/live/<key>", flush=True)
    try:
        p.wait()
    except KeyboardInterrupt:
        p.terminate()


def cmd_kick_relay(a):
    from . import kick
    kick.serve(a.port, a.out)


def cmd_sync_check(a):
    from . import live
    skew, ct, at = live.sync_check(a.out)
    print(f"busiest chat second t={ct}s, loudest audio second t={at}s: chat is {skew:+d}s {'later' if skew > 0 else 'earlier'} than the video.\n"
          f"Re-run monitor with --latency <current {'+' if skew > 0 else ''}{skew}> (only meaningful if that loud moment is what chat reacted to).")


def cmd_selftest(a):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from tests import selftest
    selftest.main()


def main(argv=None):
    p = argparse.ArgumentParser("clipper")
    sp = p.add_subparsers(dest="cmd", required=True)
    s = sp.add_parser("process", help="clip a video URL/file (resumable)")
    s.add_argument("src")
    s.add_argument("--clips", type=int)
    s.add_argument("--profile", choices=list(__import__("clipper.score", fromlist=["x"]).PROFILES), help="podcast|gaming|irl|comedy|sports|interview|auto")
    s.add_argument("--mode", choices=["disabled", "local_only", "smart", "quality"], help="LLM use; default smart (needs OPENAI_API_KEY, else local)")
    s.add_argument("--style", choices=["bold_pop", "karaoke", "clean_lower", "boxed", "none"])
    s.add_argument("--model", help="light|balanced|quality|<whisper model>")
    s.add_argument("--chat"), s.add_argument("--cookies"), s.add_argument("--transcript")
    s.add_argument("--draft", action="store_true"), s.add_argument("--stop-after", default="qc", choices=J.STAGES)
    s.add_argument("--force-from", choices=J.STAGES)
    s.add_argument("--speakers", choices=["auto", "off"], help="whole-video active-speaker map (needs opencv); off = skip")
    s.add_argument("--upscale", choices=["auto", "ai", "off"], help="auto: lanczos+sharpen (default). ai: Real-ESRGAN, opt-in, check a frame: the default model cartoonises real faces")
    s.add_argument("--max-calls", type=int), s.add_argument("--max-tokens", type=int)
    s.set_defaults(f=cmd_process)
    for n, f in (("status", cmd_status), ("next", cmd_next)):
        sp.add_parser(n).add_argument("job"), sp.choices[n].set_defaults(f=f)
    s = sp.add_parser("edit")
    s.add_argument("job"), s.add_argument("clip", type=int)
    s.add_argument("--start-shift", type=float), s.add_argument("--end-shift", type=float)
    s.add_argument("--captions", choices=["bold_pop", "karaoke", "clean_lower", "boxed", "none"]), s.add_argument("--caption-scale", type=float)
    s.add_argument("--zoom", type=float, help="1.0 = none"), s.add_argument("--layout", choices=["fill", "fit"])
    s.add_argument("--focus-x", type=float), s.add_argument("--drop", action="store_true")
    s.add_argument("--zoom-at", type=float, help="punch-in at clip-relative seconds (use on a named punchline)")
    s.add_argument("--zoom-for", type=float, default=2.0), s.add_argument("--zoom-amt", type=float, default=1.12)
    s.add_argument("--no-zooms", action="store_true")
    s.add_argument("--broll-dir", help="folder of images/videos named after spoken keywords (pizza.png)"), s.add_argument("--no-broll", action="store_true")
    s.add_argument("--music", help="your own royalty-cleared track; ducked under speech"), s.add_argument("--music-db", type=float, default=-22.0)
    s.add_argument("--interpolate", action="store_true", help="2x frames with RIFE (opt-in; look at a frame)"), s.add_argument("--audio", choices=["clean", "raw"], help="clean = high-pass + denoise"), s.add_argument("--upscale", choices=["auto", "ai", "off"])
    s.set_defaults(f=cmd_edit)
    s = sp.add_parser("render")
    s.add_argument("job"), s.add_argument("--draft", action=argparse.BooleanOptionalAction, default=None)
    s.set_defaults(f=cmd_render)
    s = sp.add_parser("export")
    s.add_argument("job"), s.add_argument("--out"), s.add_argument("--all", action="store_true")
    s.set_defaults(f=cmd_export)
    s = sp.add_parser("monitor", help="watch a live stream, record it, emit preliminary clips")
    s.add_argument("src"), s.add_argument("--out", default="live-out"), s.add_argument("--channel"), s.add_argument("--chat-file")
    s.add_argument("--latency", type=float, default=8.0, help="chat-vs-video delay in seconds"), s.add_argument("--no-record", action="store_true")
    s.add_argument("--warmup-bins", type=int, default=10, help="baseline history (10 s bins) before spikes can fire"), s.add_argument("--pre", type=float, default=120), s.add_argument("--post", type=float, default=60)
    s.add_argument("--max-seconds", type=float), s.add_argument("--no-stt", action="store_true", help="skip live speech transcription")
    s.set_defaults(f=cmd_monitor)
    s = sp.add_parser("critique", help="editor-critic review of the real files (auto critics, or --packet/--submit/--unavailable for the vision critic)")
    s.add_argument("job"), s.add_argument("names", nargs="*", help="cut story pace captions framing frames audio"), s.add_argument("--packet", action="store_true")
    s.add_argument("--submit"), s.add_argument("--unavailable", metavar="WHY")
    s.set_defaults(f=cmd_critique)
    sp.add_parser("fix", help="apply the critics' mechanical edits (once per clip+issue)").add_argument("job"), sp.choices["fix"].set_defaults(f=cmd_fix)
    sp.add_parser("polish", help="critique -> fix -> re-render, max 3 rounds").add_argument("job"), sp.choices["polish"].set_defaults(f=cmd_polish)
    s = sp.add_parser("bench", help="score selection/boundaries against reviewed reference timestamps")
    s.add_argument("golden"), s.add_argument("--clips", type=int)
    s.set_defaults(f=cmd_bench)
    s = sp.add_parser("serve", help="local HTTP API (127.0.0.1, bearer token printed at start)")
    s.add_argument("--port", type=int, default=8765)
    s.set_defaults(f=cmd_serve)
    sp.add_parser("mcp", help="MCP server over stdio").set_defaults(f=cmd_mcp)
    s = sp.add_parser("ingest-server", help="local RTMP ingest (MediaMTX) for OBS/ffmpeg pushes")
    s.add_argument("--port", type=int, default=1935), s.add_argument("--bind", default="127.0.0.1", help="use 0.0.0.0 only on a trusted network")
    s.set_defaults(f=cmd_ingest_server)
    s = sp.add_parser("kick-relay", help="receive Kick's official chat webhooks (signature-verified) into a jsonl for monitor --chat-file")
    s.add_argument("--port", type=int, default=8787), s.add_argument("--out", default="kick-chat.jsonl")
    s.set_defaults(f=cmd_kick_relay)
    s = sp.add_parser("sync-check", help="measure chat-vs-video skew from a finished monitor run")
    s.add_argument("out", help="the monitor --out folder")
    s.set_defaults(f=cmd_sync_check)
    sp.add_parser("doctor").set_defaults(f=cmd_doctor)
    sp.add_parser("selftest").set_defaults(f=cmd_selftest)
    a = p.parse_args(argv)
    a.f(a)


if __name__ == "__main__":
    main()
