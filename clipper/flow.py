"""One-shot workflows: `run` (recorded video) and `watch` (livestream). Everything an agent needs in a single call:
process -> render -> editor-critic polish -> export, with a final honest report. Live: record -> detect -> finalize -> the same pipeline."""
import json, os, shutil, signal, threading, time
from pathlib import Path
from . import critics as C, job as J, live, llm
from .util import hms, jload, jsave, redact, skipped


def polish(j, rounds=C.MAX_ROUNDS, log=print):
    """critique -> apply mechanical fixes -> re-render, up to `rounds` times; ends with a final render + critique so the report matches the files."""
    for r in range(1, rounds + 1):
        J.run(j, "qc", log=lambda *_: None)
        C.run_critics(j)
        done, esc = C.apply_fixes(j)
        log(f"polish round {r}: applied {len(done)}")
        if not done:
            break
    J.run(j, "qc", log=lambda *_: None)
    C.run_critics(j)
    return C.summarize(j)


def export(j, out, include_failing=False):
    rep, plan = jload(j.p("report.json")), jload(j.p("plan.json"))
    if j.opts.get("draft"):
        raise SystemExit("last render was a draft; render without --draft before exporting")
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    meta, skipped_clips = [], []
    for c in rep["clips"]:
        if c["file"] and (c["pass"] or include_failing):
            shutil.copy(c["file"], out / Path(c["file"]).name)
            p = next(p for p in plan["clips"] if p["id"] == c["id"])
            meta.append({"file": Path(c["file"]).name, "title": p.get("title", ""), "start": c["start"], "end": c["end"], "text": p["text"], "score": c["score"],
                         "boundary_quality": c.get("boundary_quality"), "warnings": c.get("warnings", []), "issues": c["issues"]})
        else:
            skipped_clips.append({"id": c["id"], "issues": c["issues"]})
    summary = final_report(j)
    jsave(out / "metadata.json", {"clips": meta, "not_exported": skipped_clips, "run": rep.get("run"), "verdicts": summary})
    return meta, skipped_clips


def final_report(j):
    """Honest end-of-job summary: OpenAI usage, local models, skipped features, per-clip critic verdicts (and what was NOT assessed)."""
    rep = jload(j.p("report.json"), {}) or {}
    try:
        verdicts = {cid: {k: v[k] for k in ("overall", "verdict", "assessed", "unassessed")} for cid, v in C.summarize(j).items()}
    except Exception:
        verdicts = {}
    return {"run": rep.get("run"), "clips_passing": sum(c["pass"] for c in rep.get("clips", [])), "clips_total": len(rep.get("clips", [])), "verdicts": verdicts}


def run_video(src, root, clips=10, export_dir=None, auto_polish=True, log=print, **opts):
    j = J.create(src, root, clips=clips, **opts)
    log(f"job {j.id}")
    J.run(j, "qc", log=log)
    if auto_polish:
        polish(j, log=log)
    meta, bad = export(j, export_dir or j.p("export")) if export_dir is not False else ([], [])
    fr = final_report(j)
    log(f"exported {len(meta)} clips to {export_dir or j.p('export')}; {len(bad)} not exported (failing QC)")
    return j, fr


def live_judge(cache_dir, budget_file, mode, max_calls, max_tokens, max_usd):
    """Build the callback monitor() uses to ask the (budgeted, cached) LLM about a live event. Shares the cache and budget file with the final
    VOD pass, so a moment judged live is never paid for twice. Returns None when no provider is available."""
    fn = llm.provider_for(mode)
    if not fn or mode == "disabled":
        return None
    budget = llm.Budget.load(budget_file, max_calls, max_tokens, max_usd)

    def judge(event, words):
        ws = [w for w in words if event["start"] <= w["s"] <= event["end"]]
        if len(ws) < 12:
            return None
        cand = {"final_words": [0, len(ws) - 1], "core_words": [0, len(ws) - 1], "final_start": event["start"], "final_end": event["end"], "text": "", "score": 0.5}
        llm.judge(ws, [cand], fn, budget, cache_dir)
        return {k: cand[k] for k in ("llm_score", "standalone", "title") if k in cand}
    return judge


def watch_stream(src, out, clips=10, mode="smart", style="bold_pop", quality="high", auto_finalize=True, auto_polish=True, latency=8.0,
                 chat_file=None, channel=None, max_seconds=None, resume=False, log=print, warmup=10, pre=live.PRE, post=live.POST, stt=True, **opts):
    """Record + detect + (automatically) finalize + process + polish + export. No second command."""
    out = Path(out)
    sess = live.Session(out)
    stop = threading.Event()
    cancelled = threading.Event()

    def on_signal(signum, frame):  # Ctrl-C / SIGTERM: stop recording gracefully, then finalize as usual
        stop.set()
    for sg in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sg, on_signal)
        except ValueError:  # not the main thread (e.g. called from a test thread): the caller controls stop via the CANCEL file / max_seconds
            pass

    def watch_cancel():
        while not stop.is_set():
            if (out / "CANCEL").exists():
                cancelled.set()
                stop.set()
            time.sleep(1)
    threading.Thread(target=watch_cancel, daemon=True).start()
    try:
        if not (resume and sess.d.get("state") in ("finalizing", "processing", "recording", "waiting_for_stream")):
            chat_src = live.chat_source(src, channel, chat_file)
            jf = live_judge(out / "cache" / "llm", out / "budget.json", mode, opts.get("max_calls", 6), opts.get("max_tokens", 60000), opts.get("max_usd", 0.10))
            live.monitor(src, out, chat_src, latency, True, log=log, max_seconds=max_seconds, stt=stt, warmup=warmup, pre=pre, post=post, stop_event=stop, judge_fn=jf, session=sess)
        if cancelled.is_set():
            sess.set("cancelled")
            return None, {"state": "cancelled"}
        if not auto_finalize:
            return None, {"state": sess.d["state"], "next": f"clipper watch {src} --out {out} --resume"}
        fin = live.finalize(out)
        sess.set("processing", **fin)
        j, fr = run_video(fin["record"], out / "jobs", clips=clips, export_dir=out / "export", auto_polish=auto_polish, log=log, mode=mode, style=style, quality=quality,
                          chat=fin["chat"], live_events=fin["events"], llm_cache=str(out / "cache" / "llm"), budget_file=str(out / "budget.json"), **{k: v for k, v in opts.items() if k not in ("max_calls", "max_tokens", "max_usd")},
                          max_calls=opts.get("max_calls", 6), max_tokens=opts.get("max_tokens", 60000), max_usd=opts.get("max_usd", 0.10))
        sess.set("done", job=j.id, export=str(out / "export"))
        return j, fr
    except BaseException as e:
        sess.set("failed", error=redact(str(e))[:300])
        raise
