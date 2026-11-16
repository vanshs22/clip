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
                 max_calls=a.max_calls, max_tokens=a.max_tokens, cookies=a.cookies, transcript=a.transcript, draft=a.draft or None)
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
        print(f"NEXT: {'fix ' + str([c['id'] for c in bad]) + ' (see issues in status), then render' if bad else 'clipper export ' + j.id}")
        return
    what, tip = HELP[st]
    print(f"NEXT: {st}  ({what})\nrun : clipper process {j.opts['src']!r} --stop-after {st}\ntip : {tip}")


def cmd_edit(a):
    """Structured edit ops on one clip; only that clip re-renders."""
    j = get(a)
    plan, words = jload(j.p("plan.json")), jload(j.p("transcript.json"))["words"]
    c = next((c for c in plan["clips"] if c["id"] == a.clip), None) or sys.exit("no such clip")
    if a.start_shift:
        c["final_start"] = round(max(0.0, c["final_start"] + a.start_shift), 3)
    if a.end_shift:
        c["final_end"] = round(c["final_end"] + a.end_shift, 3)
    if a.captions:
        c["caption"]["style"] = a.captions
    if a.caption_scale:
        c["caption"]["scale"] = a.caption_scale
    if a.zoom is not None:
        c["zoom"] = None if a.zoom <= 1 else a.zoom
    if a.layout:
        c["layout"] = a.layout
    if a.focus_x is not None:
        c["focus_x"] = a.focus_x
    if a.drop:
        c["dropped"] = True
    ids = [i for i, w in enumerate(words) if w["s"] >= c["final_start"] - 0.05 and w["e"] <= c["final_end"] + 0.05]
    if ids:
        c["final_words"] = [ids[0], ids[-1]]
        c["issues"] = B.validate(words, c)
    jsave(j.p("plan.json"), plan)
    j.invalidate("render")
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
    ch = a.channel or (a.src.rstrip("/").split("/")[-1] if "twitch.tv" in a.src else None)
    live.monitor(a.src, a.out, ch, a.chat_file, a.latency, not a.no_record, max_seconds=a.max_seconds)


def cmd_doctor(a):
    def has_mod(m):
        return importlib.util.find_spec(m) is not None
    try:
        ff = ffmpeg()
    except SystemExit:
        ff = None
    rows = [("ffmpeg", bool(ff), "install ffmpeg or `pip install imageio-ffmpeg`"),
            ("yt-dlp", bool(shutil.which("yt-dlp")), "pip install yt-dlp  (URL sources)"),
            ("faster-whisper", has_mod("faster_whisper"), "pip install faster-whisper  (or pass --transcript)"),
            ("opencv (face focus)", has_mod("cv2"), "pip install opencv-python-headless  (optional; else centre crop)"),
            ("streamlink", bool(shutil.which("streamlink")), "pip install streamlink  (optional; yt-dlp -g is the fallback)"),
            ("nvidia GPU", bool(shutil.which("nvidia-smi")), "optional; CPU uses int8 small model"),
            ("OPENAI_API_KEY", bool(os.environ.get("OPENAI_API_KEY")), "optional; without it everything runs locally")]
    for n, ok, fix in rows:
        print(f"{'ok  ' if ok else 'MISS'} {n}" + ("" if ok else f"   -> {fix}"))
    if ff:
        enc = R.encoder()
        print(f"     encoder: {enc}")


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
    s.add_argument("--max-seconds", type=float)
    s.set_defaults(f=cmd_monitor)
    sp.add_parser("doctor").set_defaults(f=cmd_doctor)
    sp.add_parser("selftest").set_defaults(f=cmd_selftest)
    a = p.parse_args(argv)
    a.f(a)


if __name__ == "__main__":
    main()
