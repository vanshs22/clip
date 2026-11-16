"""The editor-critic. A veteran editor's review of the REAL plan, transcript and rendered files.
Rubric: Walter Murch's Rule of Six (In the Blink of an Eye) plus technical craft: emotion 51%, story 23%, rhythm 10%,
eye-trace 7%, plane 5%, space 4% (our mapping of checks to criteria is an adaptation, see CRITIC below).
Every finding names evidence, expectation, a fix and, when the fix is mechanical, a structured `edit` the tool can apply.
Automatic critics measure; the vision critic needs the agent's eyes and refuses unsupported claims."""
import json, re
from pathlib import Path
from . import boundaries as B, captions, extras, job as J, render as R
from .util import ffmpeg, hms, jload, jsave, median, run

WEIGHT = {"emotion": .51, "story": .23, "rhythm": .10, "eye_trace": .07, "plane": .05, "space": .04, "craft": .05}
CRITIC = {"cut": "rhythm", "pace": "rhythm", "story": "story", "captions": "eye_trace", "framing": "space",
          "frames": "plane", "audio": "craft", "vision": "emotion"}
AUTO = ["cut", "story", "pace", "captions", "framing", "frames", "audio"]
FILLER = re.compile(r"^(um+|uh+|er+|so yeah|okay so|ok so|anyway|like|you know|i mean|well)\b", re.I)
PENALTY = {"blocker": 25, "major": 10, "minor": 2}
MAX_ROUNDS = 3
EDIT_FIELDS = J.EDIT_KEYS - {"broll_dir", "music", "music_db"}  # what a vision finding may request


class Ctx:
    def __init__(self, j):
        self.j = j
        self.plan, self.words = jload(j.p("plan.json")), jload(j.p("transcript.json"))["words"]
        self.src, self.sents = jload(j.p("source.json")), B.sentences(self.words)
        self.clips = [c for c in self.plan["clips"] if not c.get("dropped")]
        ren = jload(j.p("render.json")) or {"files": {}}
        self.files, self.draft = ren["files"], ren.get("draft", False)
        self.cuts = (jload(j.p("signals.json")) or {}).get("cuts", [])
        self.profile = j.opts.get("profile", "auto")

    def cw(self, c):
        return [w for w in self.words if c["final_start"] - 0.01 <= w["s"] < c["final_end"] - 0.05]  # by START: a word that begins after the cut is not in the clip

    def sent_idx(self, t):
        return next((k for k, (a, b) in enumerate(self.sents) if self.words[a]["s"] <= t <= self.words[b]["e"] + 0.05), None)

    def file(self, c):
        return self.files.get(str(c["id"]))


class Rep:
    def __init__(self, name):
        self.name, self.clips, self.checked = name, {}, []

    def check(self, what):
        self.checked.append(what)

    def add(self, c, sev, code, problem, evidence, expected, fix, note, t=None, edit=None, target="boundaries"):
        self.clips.setdefault(c["id"], []).append({
            "id": f"{self.name[:3].upper()}-{c['id']}-{code}", "code": code, "severity": sev, "clip": c["id"], "timestamp_sec": t,
            "problem": problem, "evidence": evidence, "expected": expected, "fix": fix, "fix_target": target, "edit": edit,
            "editor_note": note})


def score(findings):
    return max(0, 100 - sum(PENALTY[f["severity"]] for f in findings))


def verdict(findings):
    s = {f["severity"] for f in findings}
    return "fail" if "blocker" in s else "revise" if "major" in s else "pass"


# ---- automatic critics --------------------------------------------------------------------------------------------
def _dependent(w):
    return w[:1].islower() or w.lower().strip(".,!?\"'") in B.DEPENDENT


def cut(x, R_):
    """Rhythm: where the clip opens and closes."""
    for k in ("opening", "ending", "length", "interior dead air"):
        R_.check(k)
    for c in x.clips:
        if c.get("kind") == "event":
            continue  # speech-based critics do not apply to speech-free highlights
        ws = x.cw(c)
        d = c["final_end"] - c["final_start"]
        if c.get("kind") == "event":  # a speech-free highlight is a first-class result: only the length rule applies (no words, so no opening/ending/pace checks)
            if d > 60:
                R_.add(c, "minor", "long", f"{d:.0f}s event clip", f"duration {d:.1f}s", "<= 45s", "tighten", "Tighter plays better.")
            continue
        if not ws:
            R_.add(c, "blocker", "no_speech", "No speech inside the clip window", f"0 words in {hms(c['final_start'])}-{hms(c['final_end'])}",
                   "a spoken thought", "drop it or re-select", "A clip with nothing to say is not a clip.", edit={"drop": True}, target="selection")
            continue
        head, tail = ws[0]["s"] - c["final_start"], c["final_end"] - ws[-1]["e"]
        if head > 0.8:
            R_.add(c, "minor", "dead_head", f"{head:.1f}s of nothing before the first word", f"first word at +{head:.1f}s", "<= 0.5s lead-in",
                   "start later", "Come in on the first breath, not before it.", t=0, edit={"start_shift": round(head - 0.25, 2)})
        first = " ".join(w["w"] for w in ws[:4])
        if FILLER.match(first):
            nxt = next((a for a, b in x.sents if ws[0]["s"] + 0.3 < x.words[a]["s"] < ws[0]["s"] + 15
                        and not FILLER.match(" ".join(w["w"] for w in x.words[a:min(b, a + 3) + 1])) and not _dependent(x.words[a]["w"])), None)  # first sentence that is neither filler nor needs setup
            R_.add(c, "major", "weak_open", f"Opens on filler: \"{first}\"", f"first words: {first!r}", "open on the point",
                   "start at the next sentence" if nxt is not None else "re-select", "The first two seconds are the whole audition. Cut the throat-clearing.",
                   t=0, edit={"start_shift": round(x.words[nxt]["s"] - 0.15 - c["final_start"], 2)} if nxt is not None else None)
        last = ws[-1]
        si = x.sent_idx(last["s"])
        nxt_end = x.words[x.sents[si + 1][1]]["e"] if si is not None and si + 1 < len(x.sents) else None
        own_end = x.words[x.sents[si][1]]["e"] if si is not None else last["e"] + 2
        if not B.END.search(last["w"]):
            src_ends = si is not None and si >= len(x.sents) - 1 and not B.END.search(x.words[x.sents[si][1]]["w"])  # the SOURCE stops mid-thought: nothing to extend to
            if src_ends:
                done = [w for w in ws if B.END.search(w["w"])]  # trim back to the last complete sentence still inside the clip
                keep = done[-1]["e"] + 0.4 - c["final_start"] if done else 0
                R_.add(c, "major", "cut_mid_sentence", f"Source ends mid-thought on \"{last['w']}\"", f"transcript ends at {x.words[-1]['e']:.1f}s without a full stop",
                       "a finished sentence", "trim back to the last complete sentence" if keep >= 8 else "re-select: no complete ending available", "The footage itself stops mid-sentence: end on the last finished thought.",
                       t=round(d, 1), edit={"end_shift": round(done[-1]["e"] + 0.4 - c["final_end"], 2)} if keep >= 8 else None, target="boundaries" if keep >= 8 else "selection")
            else:
                R_.add(c, "major", "cut_mid_sentence", f"Ends mid-thought on \"{last['w']}\"", f"last word {last['w']!r} has no terminal punctuation",
                       "a finished sentence", "extend to the end of the thought", "Never cut a person off mid-sentence. The audience feels it.",
                       t=round(d, 1), edit={"end_shift": round(min(15, own_end + 0.4 - c["final_end"]), 2)})
        elif last["w"].rstrip("\"')").endswith("?") and nxt_end and nxt_end - c["final_end"] < 12:
            R_.add(c, "major", "unanswered_question", "Ends on a question with no answer", f"last word {last['w']!r}", "the answer or the reaction",
                   "extend to the next sentence", "A question is a promise. Pay it off or the viewer swipes.", t=round(d, 1),
                   edit={"end_shift": round(nxt_end + 0.4 - c["final_end"], 2)})
        if tail > 1.0:
            R_.add(c, "minor", "dead_tail", f"{tail:.1f}s of dead tail", f"last word ends {tail:.1f}s before the cut", "<= 0.6s after the last word",
                   "trim the end", "Get out once the beat has landed.", t=round(d, 1), edit={"end_shift": -round(tail - 0.4, 2)})
        elif tail < 0.15:
            R_.add(c, "minor", "tight_tail", "No breath after the last word", f"{tail:.2f}s tail", ">= 0.25s", "extend slightly",
                   "Let the line breathe.", t=round(d, 1), edit={"end_shift": 0.25})
        if d < 8:
            R_.add(c, "minor", "too_short", f"{d:.0f}s is too short to land a beat", f"duration {d:.1f}s", ">= 8s", "extend or drop", "A beat needs room.", target="selection")
        elif d > 75:
            R_.add(c, "major", "too_long", f"{d:.0f}s is long for a Short", f"duration {d:.1f}s", "<= 60s unless every second earns it", "split or drop",
                   "If you cannot name what each ten seconds adds, cut them.", target="selection")
        elif d > 60:
            R_.add(c, "minor", "long", f"{d:.0f}s runs past a minute", f"duration {d:.1f}s", "<= 60s", "tighten if the payoff allows", "Tighter plays better.")
        gaps = [(ws[i]["e"], ws[i + 1]["s"]) for i in range(len(ws) - 1) if ws[i + 1]["s"] - ws[i]["e"] > 2.5]
        for a, b in gaps[:2]:
            rel = (a - c["final_start"]) / d
            ed = {"start_shift": round(b - 0.15 - c["final_start"], 2)} if rel < .35 else {"end_shift": round(a + 0.4 - c["final_end"], 2)} if rel > .65 else None
            R_.add(c, "major", "dead_air", f"{b - a:.1f}s of silence in the middle", f"gap {hms(a)}-{hms(b)}", "no gap over 1.5s",
                   "trim the edge or drop", "Silence mid-clip is where thumbs move.", t=round(a - c["final_start"], 1), edit=ed)


def story(x, R_):
    """Story: can it stand alone, is there a hook, is it new."""
    for k in ("standalone", "hook", "duplicates"):
        R_.check(k)
    for c in x.clips:
        if c.get("kind") == "event":
            continue  # speech-based critics do not apply to speech-free highlights
        ws = x.cw(c)
        if not ws:
            continue
        w0 = ws[0]["w"]
        if w0[:1].islower() or w0.lower().strip(".,!?\"'") in B.DEPENDENT:
            si = x.sent_idx(ws[0]["s"])
            prev = x.words[x.sents[si - 1][0]]["s"] if si else None
            ok = prev is not None and ws[0]["s"] - prev <= 25 and not FILLER.match(" ".join(w["w"] for w in x.words[x.sents[si - 1][0]:x.sents[si - 1][0] + 4]))  # never "fix" by pulling in filler
            R_.add(c, "major", "needs_setup", f"Opens on \"{w0}\", which depends on what came before", f"first word {w0!r}", "a sentence that makes sense cold",
                   "start one sentence earlier" if ok else "re-select", "If the viewer must have seen the last minute, it is not a Short.",
                   t=0, edit={"start_shift": round(prev - 0.15 - c["final_start"], 2)} if ok else None)
        if c.get("standalone") is False:
            R_.add(c, "major", "not_standalone", "The judge marked this as needing earlier context", "llm standalone=false", "stands alone", "re-select",
                   "Context is not optional if the payoff depends on it.", target="selection")
        if c.get("hook", 1) < 0.3:
            R_.add(c, "minor", "weak_hook", "The first sentence has no hook", f"hook score {c.get('hook')}", "a question, a claim or a number up front",
                   "consider a stronger start in this moment", "Open on the point, not the preamble.")
    seen = {c["id"]: {w["w"].lower().strip(".,!?\"'") for w in x.cw(c) if len(w["w"]) > 3} for c in x.clips}
    for a in x.clips:
        for b in x.clips:
            if a["id"] < b["id"] and seen[a["id"]] and seen[b["id"]]:
                jac = len(seen[a["id"]] & seen[b["id"]]) / len(seen[a["id"]] | seen[b["id"]])
                if jac >= 0.5:
                    lo = a if a["final_score"] < b["final_score"] else b
                    R_.add(lo, "major", "duplicate_content", f"Says the same thing as clip {b['id'] if lo is a else a['id']}", f"word overlap {jac:.0%}",
                           "each clip adds something new", "drop the weaker one", "Two cuts of one joke is one joke.", edit={"drop": True}, target="selection")


def pace(x, R_):
    for k in ("speech rate", "speech coverage"):
        R_.check(k)
    for c in x.clips:
        if c.get("kind") == "event":
            continue  # speech-based critics do not apply to speech-free highlights
        ws = x.cw(c)
        if len(ws) < 5:
            continue
        span = ws[-1]["e"] - ws[0]["s"]
        wpm = len(ws) / span * 60
        cov = sum(w["e"] - w["s"] for w in ws) / (c["final_end"] - c["final_start"])
        if wpm > 230:
            R_.add(c, "major", "too_fast", f"{wpm:.0f} words/min: captions cannot be read", f"{len(ws)} words in {span:.0f}s", "<= 200 wpm",
                   "pick a calmer moment or use smaller captions", "If they cannot read it, they did not hear it.", edit={"caption_scale": 0.85})
        elif wpm < 100 and x.profile not in ("gaming", "sports"):
            R_.add(c, "minor", "slow_delivery", f"{wpm:.0f} words/min drags", f"{len(ws)} words in {span:.0f}s", ">= 110 wpm",
                   "tighten or choose a livelier moment", "Pace is the clip's heartbeat.")
        if cov < 0.45 and x.profile not in ("gaming", "sports"):
            R_.add(c, "minor", "mostly_silence", f"Only {cov:.0%} of the clip is speech", f"speech coverage {cov:.0%}", ">= 55% for talk content",
                   "trim or re-select", "A talking clip should talk.", target="selection")


def captions_(x, R_):
    for k in ("presence", "reading speed", "transcript confidence"):
        R_.check(k)
    for c in x.clips:
        if c.get("kind") == "event":
            continue  # speech-based critics do not apply to speech-free highlights
        ws = x.cw(c)
        if not ws:
            continue
        style = (c.get("caption") or {}).get("style") or x.j.opts.get("style") or "bold_pop"
        if style == "none" and len(ws) > 8:
            R_.add(c, "minor", "no_captions", "Speech without captions", "caption style none", "captions on (most viewers watch muted)", "enable a style",
                   "Sound-off is the default.", edit={"captions": "bold_pop"})
            continue
        if style != "none":
            ch = captions.chunks(ws, captions.style(style, x.j.opts.get("style_file")), (c.get("caption") or {}).get("scale", 1.0))
            fast = [k for k in ch if len(" ".join(w["w"] for w in k)) / max(k[-1]["e"] - k[0]["s"], 0.05) > 24]
            if len(fast) > 0.2 * len(ch):
                R_.add(c, "minor", "reading_speed", f"{len(fast)}/{len(ch)} caption groups flash by faster than 24 chars/s", "chunk timing", "<= 20 chars/s",
                       "smaller type or a calmer moment", "Captions should be read, not decoded.", edit={"caption_scale": 0.9})
        low = sum(1 for w in ws if w.get("p", 1) < 0.5) / len(ws)
        if low > 0.15:
            R_.add(c, "major", "captions_unreliable", f"{low:.0%} of words were low-confidence in transcription", f"{low:.0%} words with p<0.5", "accurate captions",
                   "re-transcribe with a larger model: clipper process SRC --model quality --force-from transcribe", "A wrong caption is worse than none.", target="transcript")


def framing(x, R_):
    """Eye-trace and space: motivated camera only."""
    for k in ("zoom discipline", "crop movement", "source resolution", "source cut density"):
        R_.check(k)
    for c in x.clips:
        d = c["final_end"] - c["final_start"]
        z = c.get("zooms") or []
        if len(z) > max(1, d / 20):
            R_.add(c, "major", "zoom_spam", f"{len(z)} zooms in {d:.0f}s", f"zooms at {[i['t'] for i in z]}", "at most 1 per 20s, each tied to a punchline",
                   "remove them", "A zoom is punctuation. Used every few seconds it is noise.", edit={"zooms_clear": True}, target="framing")
        if any(i["amt"] > 1.25 for i in z):
            R_.add(c, "minor", "zoom_too_big", "Zoom beyond 25% softens the image", "amt > 1.25", "<= 1.15", "reduce", "Push in, do not crash in.", edit={"zooms_clear": True})
        cr = R.shot_cuts(c["final_start"], c["final_end"], x.cuts)
        if len(cr) > d / 3:
            R_.add(c, "major", "cut_storm", f"{len(cr)} source cuts in {d:.0f}s", "scene detector", "<= 1 cut per 3s",
                   "use the blurred 'fit' layout so the crop stops jumping", "A reframe that jumps at every cut feels like a glitch.",
                   edit={"layout": "fit"}, target="framing")
        fa = c.get("focus_auto") or []
        moves = sum(len(kf) - 1 for kf in fa if kf)
        if moves > 6:
            xs = [p[1] for kf in fa for p in kf]
            R_.add(c, "minor", "restless_framing", f"{moves} crop moves", "tracking keyframes", "framing that mostly holds",
                   "lock the crop", "If the camera moves, there should be a reason.", edit={"focus_x": round(median(xs), 3)}, target="framing")
        if "low_res_source" in (c.get("warn") or []):
            R_.add(c, "major" if x.src["info"]["height"] < 540 else "minor", "low_res_source",
                   f"Source is {x.src['info']['height']}p: upscaled to 1080x1920 it will look soft", f"source height {x.src['info']['height']}",
                   "native 1080p", "get a higher-resolution source; `--upscale ai` exists but its default model redraws faces as cartoons: only use it after looking at a frame. Tell the user",
                   "You cannot cut your way out of missing pixels.", target="source")


def _stat(path, key):
    return re.findall(rf"signalstats\.{key}=([\d.]+)", path)


def frames(x, R_):
    """Plane: the picture itself, measured on the rendered file."""
    for k in ("exposure", "flat/black frames", "frozen picture"):
        R_.check(k)
    for c in x.clips:
        f = x.file(c)
        if not f:
            continue
        out = run([ffmpeg(), "-hide_banner", "-nostats", "-i", f, "-an", "-vf", "fps=2,signalstats,metadata=print:file=-", "-f", "null", "-"]).stdout
        ya, lo, hi, df = ([float(v) for v in _stat(out, k)] for k in ("YAVG", "YMIN", "YMAX", "YDIF"))
        if not ya:
            continue
        m = median(ya)
        if m < 40:
            R_.add(c, "major", "too_dark", f"Median luma {m:.0f}/255", f"YAVG median {m:.0f}", ">= 50", "source is dark: check the stream's own exposure", "Dark picture, dead clip.", target="source")
        elif m > 205:
            R_.add(c, "minor", "blown_out", f"Median luma {m:.0f}/255", f"YAVG median {m:.0f}", "<= 200", "source is overexposed", "Highlights are gone.", target="source")
        flat = [i for i in range(len(hi)) if hi[i] - lo[i] < 24]
        if len(flat) >= 3:
            R_.add(c, "major", "flat_frames", f"{len(flat) / 2:.1f}s of flat/blank picture", f"first at {flat[0] / 2:.1f}s", "no blank frames",
                   "trim or re-select", "A blank frame reads as a bug.", t=flat[0] / 2, target="selection")
        run_len = best = 0
        for v in df:
            run_len = run_len + 1 if v < 0.02 else 0
            best = max(best, run_len)
        if best >= 8:
            R_.add(c, "minor", "frozen_picture", f"Picture does not move for {best / 2:.0f}s", f"YDIF≈0 for {best / 2:.0f}s", "motion or a reason to hold",
                   "fine for slides/audio-led content; otherwise re-select", "A held frame needs a voice worth holding for.")


def audio(x, R_):
    for k in ("integrated loudness", "true peak"):
        R_.check(k)
    for c in x.clips:
        f = x.file(c)
        if not f or not x.src["info"]["audio"]:
            continue
        err = run([ffmpeg(), "-hide_banner", "-nostats", "-i", f, "-vn", "-af", "ebur128=peak=true", "-f", "null", "-"]).stderr
        li, pk = re.findall(r"I:\s+(-?[\d.]+) LUFS", err), re.findall(r"Peak:\s+(-?[\d.]+) dBFS", err)
        # single-pass loudnorm cannot lift a clip that is mostly silence (integrated loudness is dominated by the gaps): judge loudness only on clips with real programme audio
        gated = re.findall(r"LRA:\s+(-?[\d.]+) LU", err)
        quiet_programme = bool(gated) and float(gated[-1]) < 1.0 and li and float(li[-1]) < -20
        if li and abs(float(li[-1]) + 14) > 2.5 and not quiet_programme:
            R_.add(c, "major", "loudness_off", f"{li[-1]} LUFS (target -14)", f"ebur128 integrated {li[-1]}", "-14 ±2.5 LUFS",
                   "re-render; if it persists the source is too quiet or too dynamic", "Volume jumps between clips lose the viewer.", target="audio")
        if pk and float(pk[-1]) > -0.9:
            R_.add(c, "major", "true_peak", f"True peak {pk[-1]} dBFS clips on some players", f"ebur128 peak {pk[-1]}", "<= -1.0 dBFS", "re-render",
                   "Distortion is audible on phone speakers.", target="audio")


CRITICS = {"cut": cut, "story": story, "pace": pace, "captions": captions_, "framing": framing, "frames": frames, "audio": audio}


# ---- running, reporting, fixing ----------------------------------------------------------------------------------
def run_critics(j, names=None):
    x = Ctx(j)
    names = names or AUTO
    rounds = jload(j.p("review", "rounds.json"), {})
    out = {}
    for n in names:
        r = Rep(n)
        CRITICS[n](x, r)
        rounds[n] = rounds.get(n, 0) + 1
        rep = {"critic": n, "criterion": CRITIC[n], "assessment": "automatic", "round": rounds[n], "checked": r.checked,
               "clips": {str(c["id"]): {"score": score(r.clips.get(c["id"], [])), "verdict": verdict(r.clips.get(c["id"], [])),
                                       "findings": r.clips.get(c["id"], [])} for c in x.clips}}
        jsave(j.p("review", "critics", f"{n}.json"), rep)
        out[n] = rep
    jsave(j.p("review", "rounds.json"), rounds)
    return out


def load_reports(j):
    return {f.stem: jload(f) for f in sorted(j.p("review", "critics").glob("*.json"))}


def summarize(j):
    """Per clip: Murch-weighted overall over the criteria that were actually assessed."""
    reps, x = load_reports(j), Ctx(j)
    res = {}
    for c in x.clips:
        per, allf = {}, []
        for n, r in reps.items():
            cl = r["clips"].get(str(c["id"]))
            if cl:
                per.setdefault(r["criterion"], []).append(cl["score"])
                allf += cl["findings"]
        tot = sum(WEIGHT[k] for k in per)
        overall = round(sum(WEIGHT[k] * (sum(v) / len(v)) for k, v in per.items()) / tot) if tot else None
        res[c["id"]] = {"overall": overall, "verdict": verdict(allf), "assessed": sorted(per), "unassessed": sorted(set(WEIGHT) - set(per) - {"craft"}),
                        "findings": allf}
    return res


def edit_cli(job_id, f):
    e = f.get("edit")
    if not e:
        return None
    flag = {"start_shift": "--start-shift", "end_shift": "--end-shift", "captions": "--captions", "caption_scale": "--caption-scale", "layout": "--layout",
            "focus_x": "--focus-x", "audio": "--audio", "zoom": "--zoom"}
    parts = [f"{flag[k]} {v}" for k, v in e.items() if k in flag] + (["--drop"] if e.get("drop") else []) + (["--no-zooms"] if e.get("zooms_clear") else [])
    return f"clipper edit {job_id} {f['clip']} " + " ".join(parts)


def apply_fixes(j, log=print):
    """Apply every mechanical `edit` once per (clip, code), most severe first. Returns (applied, escalated)."""
    st = jload(j.p("review", "applied.json"), [])
    rounds = jload(j.p("review", "rounds.json"), {})
    if max(rounds.values(), default=0) > MAX_ROUNDS:
        return [], ["round limit reached: show the user what is left"]
    fs = [f for n, r in load_reports(j).items() for cl in r["clips"].values() for f in cl["findings"] if f["severity"] in ("major", "blocker", "minor")]
    fs.sort(key=lambda f: (not (f.get("edit") or {}).get("drop"), ["blocker", "major", "minor"].index(f["severity"])))  # a drop makes other fixes moot
    applied, esc, touched = [], [], set()
    for f in fs:
        key = f"{f['clip']}:{f['code']}"
        if f["severity"] == "minor" and not f.get("edit"):
            continue
        if f.get("edit") and key not in st and f["clip"] not in touched:  # one structural change per clip per round: re-measure before the next
            J.edit_clip(j, f["clip"], f["edit"])
            st.append(key)
            touched.add(f["clip"])
            applied.append(f"clip {f['clip']}: {f['code']} -> {edit_cli(j.id, f)}")
        elif f["severity"] != "minor" and (not f.get("edit") or key in st):
            esc.append(f"clip {f['clip']} [{f['severity']}] {f['code']}: {f['problem']}. Needs: {f['fix']} (target {f['fix_target']})")
    jsave(j.p("review", "applied.json"), st)
    return applied, esc


# ---- vision critic (eyes required) -----------------------------------------------------------------------------------
RUBRIC = """# Vision critic: open EVERY image below before judging
Think like an editor cutting for the viewer's eye, in Murch's order of priority.
1. EMOTION (most important): does the first frame earn the next second? Does the moment land? Would a stranger feel something? (blocker if the clip is pointless)
2. STORY: is the visual consistent with what is being said? (major if the picture contradicts the words)
3. EYE-TRACE: where does the eye go in frame 1? Is the speaker's face cropped or off-centre? Do captions cover a face, hand or key object? Is caption text readable on this background? (major each)
4. PLANE: composition and balance; empty or cluttered frame; bars; distracting edges. (minor/major)
5. SPACE: in split/fit layouts, is each speaker fully visible and consistently placed? After a cut, does the subject stay where the eye expects? (major)
6. QUALITY: smearing from upscaling, flicker, stray watermarks/overlays, placeholder text, black frames. (blocker if visible)
Each finding: clip, severity, timestamp_sec (frame position x clip length/6), problem, EVIDENCE = what you actually SAW, expected, fix, optional edit
(allowed edit keys: %s)."""


def vision_packet(j):
    x = Ctx(j)
    d = j.p("review", "sheets")
    d.mkdir(parents=True, exist_ok=True)
    imgs = []
    for c in x.clips:
        f = x.file(c)
        if not f:
            continue
        dur = c["final_end"] - c["final_start"]
        out = d / f"clip_{c['id']:02d}.jpg"
        run([ffmpeg(), "-y", "-v", "error", "-i", f, "-vf", f"fps=6/{dur:.2f},scale=270:-2,tile=6x1", "-frames:v", "1", "-q:v", "3", out])
        imgs.append(out.name)
    (j.p("review") / "packet.md").write_text(RUBRIC % sorted(EDIT_FIELDS) + "\n\nImages (each is 6 frames, left to right across the clip):\n" + "\n".join(f"- {j.p('review', 'sheets', n)}" for n in imgs) + "\n")
    jsave(j.p("review", "findings.template.json"), {"opened": imgs, "findings": [{"clip": 1, "severity": "major", "timestamp_sec": 0, "problem": "", "evidence": "what I saw",
                                                                                    "expected": "", "fix": "", "edit": None}]})
    return imgs


def vision_submit(j, path):
    sub, x = jload(path), Ctx(j)
    need = {f"clip_{c['id']:02d}.jpg" for c in x.clips if x.file(c)}
    missing = need - set(sub.get("opened", []))
    if missing or not need:
        raise SystemExit(f"rejected: you must open and list every image. Missing: {sorted(missing) or 'no sheets (run --packet first)'}")
    r = Rep("vision")
    byid = {c["id"]: c for c in x.clips}
    for f in sub["findings"]:
        if f.get("clip") not in byid or f.get("severity") not in PENALTY or len(str(f.get("evidence", ""))) < 12 or not f.get("problem"):
            raise SystemExit(f"rejected finding {f}: needs a real clip id, severity, problem and EVIDENCE of what you saw (>=12 chars)")
        ed = f.get("edit")
        if ed and set(ed) - EDIT_FIELDS:
            raise SystemExit(f"rejected: edit keys not allowed: {sorted(set(ed) - EDIT_FIELDS)}")
        r.add(byid[f["clip"]], f["severity"], re.sub(r"\W+", "_", f["problem"].lower())[:24], f["problem"], f["evidence"], f.get("expected", ""), f.get("fix", ""),
              f.get("note", "Judged by eye on the contact sheet."), t=f.get("timestamp_sec"), edit=ed, target="framing")
    rep = {"critic": "vision", "criterion": "emotion", "assessment": "vision", "round": 1, "checked": ["emotion", "eye-trace", "plane", "space", "quality"],
           "clips": {str(c["id"]): {"score": score(r.clips.get(c["id"], [])), "verdict": verdict(r.clips.get(c["id"], [])), "findings": r.clips.get(c["id"], [])} for c in x.clips}}
    jsave(j.p("review", "critics", "vision.json"), rep)
    return rep


def vision_unavailable(j, why):
    jsave(j.p("review", "critics", "vision_unavailable.json"), {"critic": "vision", "criterion": "emotion", "assessment": "NOT PERFORMED", "reason": why, "clips": {}})
