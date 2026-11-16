"""Resumable pipeline. Every stage writes one artifact; a stage whose artifact exists is skipped.
So a crash, a failed render or an edit never repeats download, transcription or analysis."""
import time
from pathlib import Path
from . import ingest, llm, render as R, score, signals as S, transcribe as T
from .util import digest, hms, jload, jsave, probe

STAGES = ["ingest", "transcribe", "signals", "candidates", "select", "render", "qc"]
ART = {"ingest": "source.json", "transcribe": "transcript.json", "signals": "signals.json", "candidates": "candidates.json",
       "select": "plan.json", "render": "render.json", "qc": "report.json"}
DEFAULTS = {"clips": 10, "profile": "auto", "mode": "smart", "style": "bold_pop", "model": "balanced", "chat": None,
            "cookies": None, "transcript": None, "draft": False, "upscale": "auto", "max_calls": 6, "max_tokens": 60000}


class Job:
    def __init__(self, root, jid):
        self.dir = Path(root) / jid
        self.id = jid

    def p(self, *a):
        return self.dir.joinpath(*a)

    @property
    def opts(self):
        return jload(self.p("job.json"), {})

    def done(self, st):
        return self.p(ART[st]).exists()

    def invalidate(self, frm):
        for st in STAGES[STAGES.index(frm):]:
            self.p(ART[st]).unlink(missing_ok=True)


def create(src, root, **opts):
    jid = digest(src)[:10]
    j = Job(root, jid)
    old = j.opts
    new = {**DEFAULTS, **old, "src": src, **{k: v for k, v in opts.items() if v is not None}}
    jsave(j.p("job.json"), new)
    return j


def next_step(j):
    for st in STAGES:
        if not j.done(st):
            return st
    return None


def run_stage(j, st):
    o = j.opts
    t0 = time.time()
    globals()[f"_{st}"](j, o)
    return round(time.time() - t0, 1)


def run(j, stop_after="qc", log=print):
    while (st := next_step(j)) and STAGES.index(st) <= STAGES.index(stop_after):
        log(f"[{st}] ...")
        log(f"[{st}] done in {run_stage(j, st)}s")
    return next_step(j)


EDIT_KEYS = {"start_shift", "end_shift", "captions", "caption_scale", "zoom", "layout", "focus_x", "drop", "broll_dir", "no_broll", "music",
             "music_db", "audio", "upscale", "zooms_clear", "zoom_add"}


def edit_clip(j, cid, ops):
    """The single place clips are modified (CLI, MCP and critic auto-fixes all come here). Invalidates only the render."""
    from . import boundaries as B, extras
    bad = set(ops) - EDIT_KEYS
    if bad:
        raise SystemExit(f"unknown edit op(s): {sorted(bad)}")
    plan, words = jload(j.p("plan.json")), jload(j.p("transcript.json"))["words"]
    c = next((c for c in plan["clips"] if c["id"] == cid), None)
    if c is None:
        raise SystemExit(f"no such clip {cid}")
    if ops.get("start_shift") or ops.get("end_shift"):
        c["focus_auto"] = None  # shots changed
        c["final_start"] = round(max(0.0, c["final_start"] + ops.get("start_shift", 0)), 3)
        c["final_end"] = round(c["final_end"] + ops.get("end_shift", 0), 3)
    if "captions" in ops:
        c["caption"]["style"] = ops["captions"]
    if "caption_scale" in ops:
        c["caption"]["scale"] = ops["caption_scale"]
    if "zoom" in ops:
        c["zoom"] = None if ops["zoom"] <= 1 else ops["zoom"]
    if "zoom_add" in ops:
        c.setdefault("zooms", []).append(ops["zoom_add"])
    if ops.get("zooms_clear"):
        c["zooms"] = []
    for k in ("layout", "focus_x", "audio", "upscale"):
        if k in ops:
            c[k] = ops[k]
    if "broll_dir" in ops:
        c["broll"] = extras.match_broll(words, ops["broll_dir"], c["final_start"], c["final_end"])
    if ops.get("no_broll"):
        c["broll"] = []
    if ops.get("music"):
        c["music"] = {"file": str(Path(ops["music"]).resolve()), "db": ops.get("music_db", -22.0)}
    if ops.get("drop"):
        c["dropped"] = True
    ids = [i for i, w in enumerate(words) if w["s"] >= c["final_start"] - 0.05 and w["e"] <= c["final_end"] + 0.05]
    if ids:
        c["final_words"] = [ids[0], ids[-1]]
        c["issues"] = B.validate(words, c)
    jsave(j.p("plan.json"), plan)
    j.invalidate("render")
    return c


# ---- stages -------------------------------------------------------------------------------------------------
def _ingest(j, o):
    meta = ingest.inspect(o["src"], o["cookies"])
    if meta["state"] == "live":
        raise SystemExit("This source is LIVE. Use `clipper monitor <url>` (captures + records), then `clipper process` the recording.")
    if meta["state"] in ("auth_required", "unavailable"):
        raise SystemExit(f"{meta['state']}: {meta.get('error')} {meta.get('hint', '')}".strip())
    path = o["src"] if meta["kind"] == "file" else str(ingest.download(o["src"], j.p("media"), o["cookies"]))
    if not path or path == "None":
        raise SystemExit("download produced no media file")
    chat = o["chat"] or next((str(p) for p in j.p("media").glob("*live_chat*")), None)
    jsave(j.p("source.json"), {"path": str(Path(path).resolve()), "info": probe(path), "meta": meta, "chat": chat})


def _transcribe(j, o):
    src = jload(j.p("source.json"))
    if o["transcript"]:
        words = T.from_file(o["transcript"])
    else:
        try:
            words = T.transcribe(src["path"], src["info"]["duration"], j.p("cache", "whisper"), o["model"])
        except ImportError:
            raise SystemExit("faster-whisper missing: pip install faster-whisper (or pass --transcript words.json)")
    jsave(j.p("transcript.json"), {"words": words})


def _signals(j, o):
    src, words = jload(j.p("source.json")), jload(j.p("transcript.json"))["words"]
    d = src["info"]["duration"]
    chat = ingest.load_chat(src["chat"]) if src.get("chat") else []
    audio = [x + 90 for x in S.audio_rms(src["path"], d)] if src["info"]["audio"] else []
    vid = bool(src["info"]["width"])
    cutd, cuts = S.scene_density(src["path"], d) if vid else ([], [])
    visual = S.fuse_visual(S.motion_series(src["path"], d), cutd) if vid else []
    sig = {"speech": S.text_series(words, d), "audio": audio, "visual": visual, "chat": S.chat_series(chat, d) if chat else [],
           "events": S.audio_events(src["path"], d) if src["info"]["audio"] else [], "ocr": S.ocr_series(src["path"], d) if vid else []}
    jsave(j.p("signals.json"), {**sig, "cuts": cuts, "chat_spikes": S.spikes(sig["chat"], min_abs=8) if chat else [], "chat_n": len(chat)})


def _candidates(j, o):
    src, words, sig = jload(j.p("source.json")), jload(j.p("transcript.json"))["words"], jload(j.p("signals.json"))
    cs = score.candidates(words, src["info"]["duration"], {k: sig.get(k, []) for k in ("speech", "audio", "visual", "chat", "events", "ocr")}, o["profile"])
    jsave(j.p("candidates.json"), {"candidates": cs})


def _select(j, o):
    words, cs = jload(j.p("transcript.json"))["words"], jload(j.p("candidates.json"))["candidates"]
    top, cost = llm.select(words, cs, o["clips"], o["mode"], j.p("cache", "llm"), llm.Budget(o["max_calls"], o["max_tokens"]))
    layout = score.PROFILES.get(o["profile"], score.PROFILES["auto"])["layout"]
    clips = [{**c, "id": i + 1, "caption": {"style": o["style"], "scale": 1.0}, "layout": layout, "zoom": None, "focus_x": None, "upscale": o["upscale"],
              "zooms": auto_zoom(c, layout)} for i, c in enumerate(top)]
    jsave(j.p("plan.json"), {"clips": clips, "cost": cost})


def auto_zoom(c, layout):
    """Conservative: at most ONE punch-in, only on a strong clip, on the fused peak, in fill layout. Render drops it if a cut is near."""
    d, t = c["final_end"] - c["final_start"], c["peak"] - c["final_start"] - 0.5
    ok = layout == "fill" and c["score"] >= 0.7 and d >= 15 and 1.5 <= t <= d - 3
    return [{"t": round(t, 2), "dur": 2.0, "amt": 1.12}] if ok else []


def _render(j, o):
    src, words, plan = jload(j.p("source.json")), jload(j.p("transcript.json"))["words"], jload(j.p("plan.json"))
    cuts = (jload(j.p("signals.json")) or {}).get("cuts", [])
    out, log = {}, []
    for c in plan["clips"]:
        if c.get("dropped"):
            continue
        try:
            out[c["id"]] = str(R.render_clip(src["path"], c, j.p("renders"), words, src["info"], o["draft"], cuts=cuts))
        except RuntimeError as e:
            log.append(f"clip {c['id']}: {e}")  # one bad clip must not discard the others
    jsave(j.p("plan.json"), plan)  # focus_x discovered during render is persisted
    jsave(j.p("render.json"), {"files": out, "errors": log, "draft": o["draft"]})


def _qc(j, o):
    plan, ren = jload(j.p("plan.json")), jload(j.p("render.json"))
    rep = {"clips": [], "cost": plan["cost"]}
    for c in plan["clips"]:
        if c.get("dropped"):
            continue
        f = ren["files"].get(str(c["id"]))
        iss = list(c.get("issues", []))
        if f:
            ri, _ = R.qc(f, c, ren["draft"])
            iss += ri
        else:
            iss.append("render_failed")
        rep["clips"].append({"id": c["id"], "file": f, "start": hms(c["final_start"]), "end": hms(c["final_end"]),
                             "score": c["final_score"], "title": c.get("title", ""), "issues": iss, "warnings": c.get("warn", []), "pass": not iss})
    rep["errors"] = ren["errors"]
    jsave(j.p("report.json"), rep)
