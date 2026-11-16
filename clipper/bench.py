"""Golden-set benchmark: does a change make selection and boundaries better? `clipper bench golden.json`.
golden.json: {"entries":[{"video": path, "transcript": path?, "chat": path?, "profile": "podcast",
                          "refs": [{"start": sec, "end": sec}, ...]}]}   refs = manually reviewed good clips."""
import json
from pathlib import Path
from . import job as J
from .util import jload, jsave


def _iou(a, b):
    i = min(a[1], b[1]) - max(a[0], b[0])
    return max(0.0, i) / (max(a[1], b[1]) - min(a[0], b[0]))


def score_plan(clips, refs, thr=0.5):
    """recall: refs matched by a selected clip (IoU >= thr). precision: selected clips that match a ref. start_err/end_err: mean abs seconds on matched.
    issue_rate: clips failing the boundary validator. incomplete_rate: clips that start/end mid-sentence. duplicates: selected pairs overlapping > 30%.
    missed: refs with no match. false_positives: selected clips matching no ref. boundary_quality: mean score."""
    hit, se, ee, matched = 0, [], [], set()
    for r in refs:
        best = max(clips, key=lambda c: _iou((c["final_start"], c["final_end"]), (r["start"], r["end"])), default=None)
        if best and _iou((best["final_start"], best["final_end"]), (r["start"], r["end"])) >= thr:
            hit += 1
            matched.add(id(best))
            se.append(abs(best["final_start"] - r["start"]))
            ee.append(abs(best["final_end"] - r["end"]))
    n = max(1, len(clips))
    inc = sum(1 for c in clips if {"starts_mid_sentence", "ends_mid_sentence", "truncates_word"} & set(c.get("issues") or []))
    dup = sum(1 for i, a in enumerate(clips) for b in clips[i + 1:] if _iou((a["final_start"], a["final_end"]), (b["final_start"], b["final_end"])) > 0.3)
    bq = [c["boundary_quality"] for c in clips if c.get("boundary_quality") is not None]
    return {"recall": round(hit / max(1, len(refs)), 3), "precision": round(len(matched) / n, 3), "start_err": round(sum(se) / len(se), 2) if se else None,
            "end_err": round(sum(ee) / len(ee), 2) if ee else None, "issue_rate": round(sum(bool(c.get("issues")) for c in clips) / n, 3),
            "incomplete_rate": round(inc / n, 3), "duplicates": dup, "missed": len(refs) - hit, "false_positives": len(clips) - len(matched),
            "boundary_quality": round(sum(bq) / len(bq), 3) if bq else None, "n_refs": len(refs), "n_clips": len(clips)}


def run(golden, root="clipper-bench", n=None):
    g, rows = jload(golden), []
    for e in g["entries"]:
        j = J.create(e["video"], root, clips=n or max(10, len(e["refs"]) * 2), mode="disabled", profile=e.get("profile", "auto"),
                     transcript=e.get("transcript"), chat=e.get("chat"), speakers="off")
        J.run(j, "select", log=lambda *_: None)
        rows.append({"video": e["video"], "type": e.get("type", e.get("profile", "auto")), **score_plan(jload(j.p("plan.json"))["clips"], e["refs"])})
    by = {}
    for r in rows:
        by.setdefault(r["type"], []).append(r)
    cov = {t: {"videos": len(v), "refs": sum(x["n_refs"] for x in v)} for t, v in by.items()}
    jsave(Path(root) / "bench.json", {"rows": rows, "coverage": cov, "note": "Coverage is the truth: types with no row have NO human-labelled data."})
    return rows
