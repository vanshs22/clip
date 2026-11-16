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


NEGATIVES = [  # single-speaker stretches (re-encoded slices, so video and transcript stay aligned): the gate must say NOT reliable
    ("panel2", 72, 113), ("panel2", 129, 156), ("panel2", 34, 68), ("panel1", 60, 100), ("panel1", 100, 170), ("panel1", 50, 170),
]
FILES = {"panel1": ("panel.mkv", "panel_words.json"), "panel2": ("panel2.mkv", "panel2_words.json")}


def speaker(ref_dir):
    """Active-speaker following on real footage. Positives: accuracy of whole-video timelines. Negatives: the gate must refuse."""
    import subprocess
    from clipper.util import ffmpeg, probe
    R = ref_dir.rstrip("/") + "/"
    results = {"pos": [], "neg": []}
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
        print(f"POS {name}: reliable={spk['reliable']} {spk['info']} accuracy={ok}/{ok + bad} = {ok / max(1, ok + bad):.0%} ({time.time() - t0:.0f}s)")
        results["pos"].append((spk["reliable"], ok, bad))
    for name, a, b in NEGATIVES:
        f, wj = FILES[name]
        out = R + "slice.mkv"
        subprocess.run([ffmpeg(), "-y", "-v", "error", "-i", R + f, "-ss", str(a), "-t", str(b - a), "-c:v", "libx264", "-preset", "ultrafast", "-crf", "23", "-c:a", "aac", out], check=True)
        ws = [{**w, "s": w["s"] - a, "e": w["e"] - a} for w in json.load(open(R + wj))["words"] if a <= w["s"] < b]
        spk = SP.timeline(out, ws, probe(out)["duration"])
        print(f"NEG {name} {a}-{b}s: reliable={spk['reliable']} {spk['info']}")
        results["neg"].append(spk["reliable"])
    return results


if __name__ == "__main__":
    {"speaker": lambda: speaker(sys.argv[2])}[sys.argv[1]]()
