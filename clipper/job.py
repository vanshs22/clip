"""Resumable pipeline. Every stage writes one artifact; a stage whose artifact exists is skipped.
So a crash, a failed render or an edit never repeats download, transcription or analysis."""
import os, time, uuid
from pathlib import Path
from . import ingest, llm, render as R, score, signals as S, transcribe as T
from .util import check_disk, digest, file_fingerprint, hms, jload, jsave, log_event, note_skipped, pid_alive, probe, skipped, validate_source

STAGES = ["ingest", "transcribe", "signals", "candidates", "select", "speakers", "render", "qc"]
ART = {"ingest": "source.json", "transcribe": "transcript.json", "signals": "signals.json", "candidates": "candidates.json",
       "select": "plan.json", "speakers": "speakers.json", "render": "render.json", "qc": "report.json"}
DEFAULTS = {"clips": 10, "profile": "auto", "mode": "smart", "style": "bold_pop", "model": "balanced", "chat": None, "cookies": None,
            "transcript": None, "draft": False, "upscale": "auto", "speakers": "auto", "max_calls": 6, "max_tokens": 60000, "max_usd": 0.10,
            "quality": "normal", "live_events": None, "llm_cache": None, "budget_file": None, "style_file": None, "logo": None, "fade": False}
# which options each stage's result depends on. Upstream stages are chained in automatically, so e.g. `clips` (select) invalidates
# select/speakers?/render/qc but never ingest/transcribe/signals/candidates; `style` is only consulted at render time.
STAGE_OPTS = {
    "ingest": ("cookies",), "transcribe": ("model", "transcript"), "signals": ("chat",),
    "candidates": ("profile", "live_events"),
    "select": ("clips", "mode", "max_calls", "max_tokens", "max_usd", "upscale"),
    "speakers": ("speakers",), "render": ("draft", "style", "style_file", "logo", "fade", "quality", "upscale"), "qc": ("draft",),
}
DEPS = {"ingest": [], "transcribe": ["ingest"], "signals": ["transcribe"], "candidates": ["signals"], "select": ["candidates"],
        "speakers": ["transcribe", "select"], "render": ["select", "speakers"], "qc": ["render"]}


def source_fingerprint(src):
    """Local file: size + sampled content hash. URL: the URL itself (the downloaded file is fingerprinted at ingest)."""
    p = Path(src)
    return "file:" + file_fingerprint(p) if p.exists() and p.is_file() else "url:" + digest(src)


def _optval(o, k):
    v = o.get(k)
    if k in ("chat", "transcript", "live_events", "style_file", "logo") and v and Path(str(v)).is_file():
        return [str(v), file_fingerprint(v)]  # an edited chat/transcript file must invalidate what depends on it
    return v


class Job:
    def __init__(self, root, jid):
        self.dir = Path(root) / jid
        self.id = jid

    def p(self, *a):
        return self.dir.joinpath(*a)

    @property
    def opts(self):
        return jload(self.p("job.json"), {})

    def log(self, event, **kw):
        log_event(self.p("run.log"), event, job=self.id, **kw)

    def expected_hash(self, st):
        """Hash of everything this stage's output depends on: its options, the source, and the hashes of upstream stages."""
        o = self.opts
        src = jload(self.p("source.json"), {}) if st != "ingest" else {}
        base = o.get("src_fp") if st == "ingest" else src.get("fingerprint")
        if st == "ingest":  # a local file is re-fingerprinted on every run: editing/replacing the file invalidates everything
            base = source_fingerprint(o.get("src", ""))
        return digest(st, base, {k: _optval(o, k) for k in STAGE_OPTS[st]}, [self.stored_hash(d) or "?" for d in DEPS[st]], "v4")

    def stored_hash(self, st):
        return (jload(self.p("stages.json"), {}) or {}).get(st)

    def mark(self, st, h):
        d = jload(self.p("stages.json"), {}) or {}
        d[st] = h
        jsave(self.p("stages.json"), d)

    def done(self, st):
        """Artifact exists AND was produced from inputs identical to the current ones (chained through upstream stages)."""
        if not self.p(ART[st]).exists():
            return False
        stored = self.stored_hash(st)
        return bool(stored) and stored == self.expected_hash(st) and all(self.done(d) for d in DEPS[st])

    def invalidate(self, frm):
        """Remove `frm` and every stage that (transitively) depends on it."""
        todo, seen = [frm], set()
        while todo:
            st = todo.pop()
            if st in seen:
                continue
            seen.add(st)
            self.p(ART[st]).unlink(missing_ok=True)
            todo += [s for s, d in DEPS.items() if st in d]
        h = jload(self.p("stages.json"), {}) or {}
        for st in seen:
            h.pop(st, None)
        jsave(self.p("stages.json"), h)

    def lock(self):
        """One writer per job: a lock file naming this pid; a stale lock (dead pid) is taken over."""
        lf = self.p("job.lock")
        self.dir.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                fd = os.open(lf, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                return True
            except FileExistsError:
                try:
                    pid = int(lf.read_text() or 0)
                except (OSError, ValueError):
                    pid = 0
                if pid_alive(pid) and pid != os.getpid():
                    return False
                lf.unlink(missing_ok=True)
        return False

    def unlock(self):
        self.p("job.lock").unlink(missing_ok=True)


def create(src, root, **opts):
    """One job directory per SOURCE content (so the expensive download/transcription is shared by every run on that source); options
    live in job.json and per-stage input hashes decide what is stale. Each run gets its own run_id for the report."""
    src = validate_source(src)
    jid = digest(source_fingerprint(src))[:10]
    j = Job(root, jid)
    # options are NOT inherited from earlier runs on this source: omitted means default (stage hashes keep what is still valid cached)
    new = {**DEFAULTS, "src": src, **{k: v for k, v in opts.items() if v is not None}, "run_id": uuid.uuid4().hex[:12]}
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
    j.log("stage_start", stage=st)
    globals()[f"_{st}"](j, o)
    j.mark(st, j.expected_hash(st))
    if skipped():
        jsave(j.p("features.json"), {**(jload(j.p("features.json"), {}) or {}), **skipped()})
    dt = round(time.time() - t0, 1)
    j.log("stage_done", stage=st, seconds=dt)
    return dt


def run(j, stop_after="qc", log=print):
    """Run stale stages in order. A stage whose inputs changed is re-run (and its downstream with it); finished stages are skipped."""
    if not j.lock():
        raise SystemExit(f"job {j.id} is being processed by another process (job.lock)")
    try:
        while (st := next_step(j)) and STAGES.index(st) <= STAGES.index(stop_after):
            if j.p(ART[st]).exists():  # stale: remove it and everything downstream
                j.invalidate(st)
            log(f"[{st}] ...")
            log(f"[{st}] done in {run_stage(j, st)}s")
    finally:
        j.unlock()
    return next_step(j)


EDIT_KEYS = {"start_shift", "end_shift", "captions", "caption_scale", "zoom", "layout", "focus_x", "drop", "broll_dir", "no_broll", "music",
             "music_db", "audio", "upscale", "interpolate", "zooms_clear", "zoom_add", "logo", "fade"}


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
    for k in ("layout", "focus_x", "audio", "upscale", "interpolate", "logo", "fade"):
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
    check_disk(j.dir, float(os.environ.get("CLIPPER_MIN_FREE_GB", 2)))
    meta = ingest.inspect(o["src"], o["cookies"])
    if meta["state"] == "live":
        raise SystemExit("This source is LIVE. Use `clipper watch <url>` (records, finds moments, finalizes automatically).")
    if meta["state"] in ("auth_required", "unavailable"):
        raise SystemExit(f"{meta['state']}: {meta.get('error')} {meta.get('hint', '')}".strip())
    path = o["src"] if meta["kind"] == "file" else str(ingest.download(o["src"], j.p("media"), o["cookies"]))
    if not path or path == "None" or not Path(path).exists():
        raise SystemExit("download produced no media file")
    info = probe(path)
    if info["duration"] <= 0 or not (info["width"] or info["audio"]):  # a failed download / renamed text file: stop here with a clear reason, not 0 clips and exit 0
        raise SystemExit(f"not a decodable media file (no video/audio stream or zero duration): {path}")
    chat = o["chat"] or next((str(p) for p in j.p("media").glob("*live_chat*")), None) or ingest.fetch_chat(o["src"], j.p("media"))
    jsave(j.p("source.json"), {"path": str(Path(path).resolve()), "info": info, "meta": {k: v for k, v in meta.items() if k != "heatmap"},
                               "chat": chat, "fingerprint": file_fingerprint(path), "heatmap": meta.get("heatmap")})


def _transcribe(j, o):
    src = jload(j.p("source.json"))
    if o["transcript"]:
        words = T.from_file(o["transcript"])
    else:
        try:
            words = T.transcribe(src["path"], src["info"]["duration"], j.p("cache", "whisper"), o["model"])
        except ImportError:
            # degrade, do not abort: audio/visual/chat highlights still work without words (event-only clips, no captions)
            note_skipped("transcription", "faster-whisper not installed: speech-based selection and captions are off (pip install faster-whisper, or pass --transcript)")
            words = []
    jsave(j.p("transcript.json"), {"words": words})


def _signals(j, o):
    src, words = jload(j.p("source.json")), jload(j.p("transcript.json"))["words"]
    d = src["info"]["duration"]
    chat = ingest.load_chat(src["chat"]) if src.get("chat") else []
    vid = bool(src["info"]["width"])
    audio = [x + 90 for x in S.audio_rms(src["path"], d)] if src["info"]["audio"] else []
    cutd, cuts = S.scene_density(src["path"], d) if vid else ([], [])
    visual = S.fuse_visual(S.motion_series(src["path"], d), cutd) if vid else []
    chat_s, chat_info = S.chat_series(chat, d, with_info=True) if chat else ([], {})
    sig = {"speech": S.text_series(words, d), "audio": audio, "visual": visual, "chat": chat_s, "heat": S.heat_series(src.get("heatmap"), d),
           "events": S.audio_events(src["path"], d) if src["info"]["audio"] else [], "ocr": S.ocr_series(src["path"], d) if vid else []}
    jsave(j.p("signals.json"), {**sig, "cuts": cuts, "chat_spikes": S.spikes(sig["chat"], min_abs=8) if chat else [], "chat_info": chat_info,
                                "models": [m for m, k in (("PANNs", "events"), ("RapidOCR", "ocr")) if any(sig[k])]})


def _candidates(j, o):
    src, words, sig = jload(j.p("source.json")), jload(j.p("transcript.json"))["words"], jload(j.p("signals.json"))
    d = src["info"]["duration"]
    prof = score.auto_profile(words, d, sig) if o["profile"] == "auto" else o["profile"]
    series = {k: sig.get(k, []) for k in ("speech", "audio", "visual", "chat", "events", "ocr", "heat")}
    cs = score.candidates(words, d, series, prof, cuts=sig.get("cuts"), reaction=sig.get("events"))
    if o.get("live_events") and Path(o["live_events"]).exists():  # candidates found during the stream: same refinement, small evidence bonus
        cs = score.dedupe(sorted(cs + score.live_candidates(words, d, jload(o["live_events"], []), sig.get("cuts")), key=lambda c: -c["score"]))
    jsave(j.p("candidates.json"), {"candidates": cs, "profile": prof})


def auto_zoom(c, layout):
    """Conservative: at most ONE punch-in, only on a strong clip, on the fused peak, in fill layout. Render drops it if a cut is near."""
    d, t = c["final_end"] - c["final_start"], c["peak"] - c["final_start"] - 0.5
    ok = layout == "fill" and c["score"] >= 0.7 and d >= 15 and 1.5 <= t <= d - 3
    return [{"t": round(t, 2), "dur": 2.0, "amt": 1.12}] if ok else []


def _select(j, o):
    words, cd = jload(j.p("transcript.json"))["words"], jload(j.p("candidates.json"))
    cs, prof = cd["candidates"], cd.get("profile", o["profile"])
    budget = llm.Budget.load(o.get("budget_file") or j.p("budget.json"), o["max_calls"], o["max_tokens"], o["max_usd"])
    top, cost = llm.select(words, cs, o["clips"], o["mode"], o.get("llm_cache") or j.p("cache", "llm"), budget)
    dur = jload(j.p("source.json"))["info"]["duration"]
    for c in top:  # transcripts can run past the media (rounding, a trimmed file): never plan beyond what exists
        c["final_end"] = round(min(c["final_end"], dur), 3)
        if c["final_end"] - c["final_start"] < 3:
            c["issues"] = list(c.get("issues", [])) + ["clipped_by_media_end"]
    layout = score.PROFILES.get(prof, score.PROFILES["auto"])["layout"]
    clips = [{**c, "id": i + 1, "caption": {"style": None, "scale": 1.0}, "layout": layout, "zoom": None, "focus_x": None, "upscale": o["upscale"],
              "zooms": auto_zoom(c, layout)} for i, c in enumerate(top)]  # style None = the job default, resolved at render time
    jsave(j.p("plan.json"), {"clips": clips, "cost": cost, "profile": prof})


def _speakers(j, o):
    """Whole-video active-speaker map (optional: off for single-speaker sources, gaming/sports, and when models are unavailable)."""
    src, words = jload(j.p("source.json")), jload(j.p("transcript.json"))["words"]
    prof = (jload(j.p("plan.json")) or {}).get("profile", o["profile"])
    spk = {"reliable": False, "reason": "off"}
    if o["speakers"] != "off" and src["info"]["width"] and prof not in ("gaming", "sports"):
        try:
            from . import speaker
            spk = speaker.timeline(src["path"], words, src["info"]["duration"])
        except ImportError as e:
            note_skipped("speaker following", f"opencv/numpy/speechbrain not installed: {e}")
            spk = {"reliable": False, "reason": "not installed"}
        except Exception as e:  # an optional model must never fail the render
            note_skipped("speaker following", f"{type(e).__name__}: {e}")
            spk = {"reliable": False, "reason": "error"}
    jsave(j.p("speakers.json"), spk)


QUALITY = {"draft": {"crf": 28, "preset": "veryfast"}, "normal": {"crf": 20, "preset": "medium"}, "high": {"crf": 17, "preset": "slow"}}


def _render(j, o):
    src, words, plan = jload(j.p("source.json")), jload(j.p("transcript.json"))["words"], jload(j.p("plan.json"))
    cuts = (jload(j.p("signals.json")) or {}).get("cuts", [])
    spk = jload(j.p("speakers.json"))
    ro = {"style": o["style"], "style_file": o.get("style_file"), "logo": o.get("logo"), "fade": o.get("fade"), "src_fp": src["fingerprint"],
          "quality": QUALITY.get(o.get("quality", "normal"), QUALITY["normal"])}
    out, log = {}, []
    for c in plan["clips"]:
        if c.get("dropped"):
            continue
        try:
            out[c["id"]] = str(R.render_clip(src["path"], c, j.p("renders"), words, src["info"], o["draft"], cuts=cuts, spk=spk, opts=ro))
        except RuntimeError as e:
            log.append(f"clip {c['id']}: {e}")  # one bad clip must not discard the others
    jsave(j.p("plan.json"), plan)  # framing discovered during render is persisted
    jsave(j.p("render.json"), {"files": out, "errors": log, "draft": o["draft"], "encoder": R.encoder()})


def _qc(j, o):
    plan, ren = jload(j.p("plan.json")), jload(j.p("render.json"))
    rep = {"clips": [], "cost": plan["cost"]}
    for c in plan["clips"]:
        if c.get("dropped"):
            continue
        f = ren["files"].get(str(c["id"]))
        iss = list(c.get("issues", []))
        warns = list(c.get("warn", []))
        if f:
            ri, _, rw = R.qc(f, c, ren["draft"])
            iss += ri
            warns += rw
        else:
            iss.append("render_failed")
        rep["clips"].append({"id": c["id"], "file": f, "start": hms(c["final_start"]), "end": hms(c["final_end"]), "score": c["final_score"],
                             "title": c.get("title", ""), "issues": iss, "warnings": warns, "boundary_quality": c.get("boundary_quality"), "pass": not iss})
    sig, spk = jload(j.p("signals.json"), {}), jload(j.p("speakers.json"), {})
    rep["errors"] = ren["errors"]
    rep["run"] = {"run_id": o.get("run_id"), "profile": plan.get("profile"), "openai": plan["cost"],
                  "local_models": sorted(set(["faster-whisper" if not o.get("transcript") else "(transcript supplied)"] + sig.get("models", []) +
                                             (["YuNet", "SpeechBrain ECAPA"] if spk.get("reliable") else []))),
                  "features_skipped": {**(jload(j.p("features.json"), {}) or {}), **skipped()}, "encoder": ren.get("encoder")}
    jsave(j.p("report.json"), rep)
