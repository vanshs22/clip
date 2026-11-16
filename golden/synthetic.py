"""Synthetic golden set with EXACT ground truth (the highlight windows are constructed, not judged). Complements the human-labelled real scenes:
it cannot say whether a moment is *interesting*, only whether the pipeline finds and bounds what it was built to find.
   python golden/synthetic.py OUT_DIR   ->  OUT_DIR/golden.json  (then: clipper bench OUT_DIR/golden.json)"""
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests import synth


def build(out):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    entries = []
    # 1) talk: three self-contained stories (setup -> payoff) hidden among filler sentences; the reference is the full story
    filler = "And we will just keep talking about the schedule for a while here."
    stories = [["Everyone said the plan would fail!", "But I did it anyway and nobody believed me!", "Three weeks later it worked brilliantly, oh my god, unbelievable!"],
               ["Why did the old coach never trust the young players?", ("Because he thought they would panic under pressure!", 1.0), "Honestly he was completely wrong about all of it, crazy!"],
               ["So I walked into the room with no slides at all!", "The CEO looked at me and just started laughing, haha, no way!", "That was the day I got the job, insane!"]]
    script, spans = [], []                   # spans[i] = (first, last) index into `script` for story i
    for st in stories:
        script += [filler] * 4
        spans.append((len(script), len(script) + len(st) - 1))
        script += st
    ws = synth.words(script, t=2.0, pause=0.5)
    first_word, n = [], 0                      # index of the first word of each script item
    for item in script:
        first_word.append(n)
        n += len((item if isinstance(item, str) else item[0]).split())
    rs = []
    for si, (a, b) in enumerate(spans):
        wa, wb = first_word[a], first_word[b] + len((script[b] if isinstance(script[b], str) else script[b][0]).split()) - 1
        rs.append({"start": round(ws[wa]["s"] - 0.15, 2), "end": round(ws[wb]["e"] + 0.4, 2), "why": f"constructed story {si}: setup to payoff"})
    end = ws[-1]["e"] + 3
    # speaker A is quiet filler; each story gets an extra loud-voice window (speaker B tone) over its reference span: excitement shows in the audio too
    synth.make_video(out / "talk.mp4", dur=int(end) + 2, a_windows=[(2, end)], b_windows=[(r["start"], r["end"]) for r in rs], cuts=[40, 90])
    (out / "talk.words.json").write_text(json.dumps({"words": ws}))
    entries.append({"video": str(out / "talk.mp4"), "transcript": str(out / "talk.words.json"), "profile": "podcast", "type": "synthetic-talk", "refs": rs})
    # 2) visual/audio-only: a flash with a loud beep, no speech at all
    synth.make_video(out / "event.mp4", dur=120, beeps=[60], cuts=[30, 100], flash=(60, 63))
    (out / "event.words.json").write_text(json.dumps({"words": []}))
    entries.append({"video": str(out / "event.mp4"), "transcript": str(out / "event.words.json"), "profile": "gaming", "type": "synthetic-event",
                    "refs": [{"start": 57.0, "end": 66.0, "why": "constructed: flash + beep, no speech"}]})
    (out / "golden.json").write_text(json.dumps({"about": "synthetic, exact ground truth", "entries": entries}, indent=1))
    return out / "golden.json"


if __name__ == "__main__":
    print(build(sys.argv[1] if len(sys.argv) > 1 else "golden/synthetic-media"))
