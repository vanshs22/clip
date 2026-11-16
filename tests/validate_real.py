"""Validation on REAL footage (needs network + a few minutes; not part of `clipper selftest`).
   python -m tests.validate_real speaker REF_DIR   (REF_DIR holds panel.mkv/panel2.mkv + *_words.json; see docs/SPEC.md for how they are made)
Ground truth for the White House panel slice (public domain; the interviewer on the right asks 12.4-46.3 s, the guest on the left otherwise),
read from the Whisper transcript and checked against the contact sheet."""
import json, sys, time
from clipper import speaker as SP


SEGMENTS = {  # side of the speaker, from transcript + contact sheets (mic at mouth). Ambiguous stretches are left out.
    "panel1": {"L": [(0, 9.5), (47, 175)], "R": [(12.8, 46)]},
    "panel2": {"L": [(1, 19), (70, 115), (162, 173)], "R": [(34, 68), (119, 125), (128, 157)]},
}


SEGMENTS.update({"bk": {"L": [(1, 34), (131, 144)], "R": [(38, 118), (121, 126)]}, "ch": {"L": [(4, 61), (112, 149)], "R": [(64, 105)]}})
# name -> (video, words json) relative to REF_DIR; labels come from the transcripts + contact sheets (see docs/SPEC.md)
FILES = {"panel1": ("panel.mkv", "panel_words.json"), "panel2": ("panel2.mkv", "panel2_words.json"), "bk": ("scan/bk.mkv", "scan/bk_words.json"), "ch": ("scan/ch.mkv", "scan/ch_words.json")}
# single-speaker stretches (re-encoded slices so video and transcript stay aligned): the gate must say NOT reliable
NEGATIVES = [("panel2", 72, 113), ("panel2", 129, 156), ("panel1", 60, 100), ("panel1", 100, 170), ("bk", 38, 118), ("ch", 111, 149), ("ch", 4, 61), ("ch", 64, 105)]
# whole scenes where voices exist but nobody's mouth moves (a still photo with a podcast audio track): must also be refused
STATIC = [("scan/fr.mkv", "scan/fr_words.json")]


def speaker(ref_dir):
    """Active-speaker following on real footage. Positives: accuracy of whole-video timelines + the gate must accept. Negatives: the gate must refuse."""
    import subprocess
    from clipper.util import ffmpeg, probe
    R = ref_dir.rstrip("/") + "/"
    out = {"pos": [], "neg": []}
    for name, (f, wj) in FILES.items():
        words = json.load(open(R + wj))["words"]
        seg = SEGMENTS[name]
        truth = lambda t: next((s for s, rs in seg.items() if any(a <= t <= b for a, b in rs)), None)
        t0 = time.time()
        spk = SP.timeline(R + f, words, probe(R + f)["duration"])
        side = lambda k: "L" if SP.median([x for x in spk["tracks"][k]["x"] if x is not None]) < 0.5 else "R"
        ok = bad = 0
        for t, k in spk["tl"]:
            gt = truth(t + 1.0)
            if gt:
                ok, bad = ok + (side(k) == gt), bad + (side(k) != gt)
        print(f"POS {name:7s} reliable={spk['reliable']} sil={spk['info'].get('silhouette')} share={spk['info'].get('share')} accuracy={ok}/{ok + bad} = {ok / max(1, ok + bad):.0%} ({time.time() - t0:.0f}s)")
        out["pos"].append((name, spk["reliable"], ok, bad))
    for name, a, b in NEGATIVES:
        f, wj = FILES[name]
        sl = R + "slice.mkv"
        subprocess.run([ffmpeg(), "-y", "-v", "error", "-i", R + f, "-ss", str(a), "-t", str(b - a), "-c:v", "libx264", "-preset", "ultrafast", "-crf", "24", "-c:a", "aac", sl], check=True)
        ws = [{**w, "s": w["s"] - a, "e": w["e"] - a} for w in json.load(open(R + wj))["words"] if a <= w["s"] < b]
        spk = SP.timeline(sl, ws, probe(sl)["duration"])
        print(f"NEG {name:7s} {a}-{b}s reliable={spk['reliable']} {spk['info'].get('silhouette')}/{spk['info'].get('share')} {spk['info'].get('reason', '')}")
        out["neg"].append((name, a, b, spk["reliable"]))
    for f, wj in STATIC:
        words = json.load(open(R + wj))["words"]
        spk = SP.timeline(R + f, words, probe(R + f)["duration"])
        print(f"NEG static photo {f}: reliable={spk['reliable']} {spk['info']}")
        out["neg"].append((f, 0, 0, spk["reliable"]))
    pos_ok = all(r for _, r, *_ in out["pos"])
    acc = sum(o for *_, o, b in out["pos"]) / max(1, sum(o + b for *_, o, b in out["pos"]))
    neg_ok = not any(r for *_, r in out["neg"])
    print(f"SUMMARY: gate accepts {sum(r for _, r, *_ in out['pos'])}/{len(out['pos'])} real two-person scenes, refuses {sum(not r for *_, r in out['neg'])}/{len(out['neg'])} negatives; pooled accuracy {acc:.1%}")
    return pos_ok and neg_ok and acc >= 0.95


if __name__ == "__main__":
    sys.exit(0 if {"speaker": lambda: speaker(sys.argv[2])}[sys.argv[1]]() else 1)
