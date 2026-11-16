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
    """recall: refs matched by a selected clip (IoU >= thr). start_err/end_err: mean abs seconds on matched.
    issue_rate: share of selected clips failing the boundary validator."""
    hit, se, ee = 0, [], []
    for r in refs:
        best = max(clips, key=lambda c: _iou((c["final_start"], c["final_end"]), (r["start"], r["end"])), default=None)
        if best and _iou((best["final_start"], best["final_end"]), (r["start"], r["end"])) >= thr:
            hit += 1
            se.append(abs(best["final_start"] - r["start"]))
            ee.append(abs(best["final_end"] - r["end"]))
    n = max(1, len(clips))
    return {"recall": round(hit / max(1, len(refs)), 3), "start_err": round(sum(se) / len(se), 2) if se else None,
            "end_err": round(sum(ee) / len(ee), 2) if ee else None, "issue_rate": round(sum(bool(c.get("issues")) for c in clips) / n, 3)}


def run(golden, root="clipper-bench", n=None):
    g, rows = jload(golden), []
    for e in g["entries"]:
        j = J.create(e["video"], root, clips=n or max(10, len(e["refs"]) * 2), mode="disabled", profile=e.get("profile", "auto"),
                     transcript=e.get("transcript"), chat=e.get("chat"))
        J.run(j, "select", log=lambda *_: None)
        rows.append({"video": e["video"], **score_plan(jload(j.p("plan.json"))["clips"], e["refs"])})
    jsave(Path(root) / "bench.json", rows)
    return rows
