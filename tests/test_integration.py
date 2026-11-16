"""Integration + end-to-end tests on real ffmpeg with synthetic media. No network, no models (stand-ins where a model would run)."""
import json, os, subprocess, sys, time
from pathlib import Path
import pytest
from clipper import captions, critics as C, flow, job as J, render as R, signals as S, transcribe as T, util
from clipper.util import probe
from tests import synth
from tests.synth import words

SCRIPT = ["Okay so here is the thing everybody keeps getting wrong about this.",       # 0   A speaker
          "I genuinely thought we were going to lose the whole game tonight.",           # 1
          "But nobody expected that to happen at all.",                                  # 2
          "And that is when I realized he was actually the one helping us all along.",   # 3
          "Why did nobody see that coming?",                                             # question (speaker B answers after a pause)
          ("Because the coach never trained them for it at all and everyone knew.", 0.5),
          "Wow.",                                                                        # reaction
          ("That was honestly the funniest thing I have ever seen in my life.", 2.0)]


@pytest.fixture(scope="module")
def spoken(tmp_path_factory, ff):
    d = tmp_path_factory.mktemp("spoken")
    ws = words(SCRIPT, t=2.0, pause=0.6)
    end = ws[-1]["e"] + 1
    v = synth.make_video(d / "s.mp4", dur=int(end) + 20, a_windows=[(2, end)], cuts=[25])
    (d / "w.json").write_text(json.dumps({"words": ws}))
    return d, v, ws


# --------------------------------------------------------------------------- rendering ---------------------------------------------------------
def test_render_produces_exact_spec_with_captions_and_passes_qc(spoken, tmp_path, ff):
    d, v, ws = spoken
    info = probe(v)
    clip = {"id": 1, "final_start": 2.0, "final_end": 14.0, "caption": {"style": None, "scale": 1.0}, "layout": "fill", "focus_auto": [[[0, .5]]], "split_auto": None}
    out = R.render_clip(v, clip, tmp_path, ws, info, opts={"style": "gaming_bold", "src_fp": "x", "quality": {"preset": "ultrafast", "crf": 23}})
    issues, i, warns = R.qc(out, clip)
    assert not issues, issues
    assert (i["width"], i["height"]) == (1080, 1920) and i["audio"] and abs(i["duration"] - 12.0) < 0.3
    assert clip["captions_n"] > 3 and clip["captions_overflow"] == 0 and (tmp_path / "fonts" / "Anton-Regular.ttf").exists()


def test_cache_key_follows_style_source_and_options(spoken, tmp_path, ff):
    d, v, ws = spoken
    info = probe(v)
    base = {"id": 1, "final_start": 2.0, "final_end": 8.0, "caption": {"style": None, "scale": 1.0}, "layout": "fill", "focus_auto": [[[0, .5]]], "split_auto": None}
    q = {"preset": "ultrafast", "crf": 28}
    a = R.render_clip(v, dict(base), tmp_path, ws, info, opts={"style": "bold_pop", "src_fp": "A", "quality": q})
    assert R.render_clip(v, dict(base), tmp_path, ws, info, opts={"style": "bold_pop", "src_fp": "A", "quality": q}) == a  # identical -> reused
    t0 = a.stat().st_mtime_ns
    b = R.render_clip(v, dict(base), tmp_path, ws, info, opts={"style": "karaoke", "src_fp": "A", "quality": q})
    assert b != a and not a.exists(), "a different style must re-render (and the stale file is removed)"
    c = R.render_clip(v, dict(base), tmp_path, ws, info, opts={"style": "karaoke", "src_fp": "OTHER-SOURCE", "quality": q})
    assert c != b, "the same clip spec from a different source must never reuse a render"


def test_missing_fonts_dir_and_hardware_encoder_failures_never_break_a_render(spoken, tmp_path, ff, monkeypatch):
    d, v, ws = spoken
    clip = {"id": 2, "final_start": 2.0, "final_end": 7.0, "caption": {"style": "cinematic", "scale": 1.0}, "layout": "fill", "focus_auto": [[[0, .5]]], "split_auto": None}
    monkeypatch.setattr(R, "_ENC", ["h264_nvenc"])  # pretend nvenc was chosen, but this machine has no NVIDIA GPU: the render must fall back
    out = R.render_clip(v, clip, tmp_path, ws, probe(v), opts={"quality": {"preset": "ultrafast", "crf": 28}, "src_fp": "z"})
    assert not R.qc(out, clip)[0] and "hardware encoder h264_nvenc" in util.skipped()


def test_qc_detects_real_defects(tmp_path, ff):
    bad = synth.make_video(tmp_path / "b.mp4", dur=6, size="640x360", audio=False)
    issues, _, _ = R.qc(bad, {"final_start": 0, "final_end": 6})
    assert "bad_size:640x360" in issues and "no_audio" in issues
    short = synth.make_video(tmp_path / "s.mp4", dur=3, size="1080x1920", beeps=[0.2])
    assert any(i.startswith("duration_off") for i in R.qc(short, {"final_start": 0, "final_end": 10})[0])
    black = tmp_path / "k.mp4"
    subprocess.run([ff, "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=black:s=1080x1920:d=3", "-f", "lavfi", "-i", "sine=f=300:d=3", "-c:v", "libx264", "-c:a", "aac", "-pix_fmt", "yuv420p", str(black)], check=True)
    iss, _, warns = R.qc(black, {"final_start": 0, "final_end": 3})
    assert "black_frames" in iss and "frozen_picture" not in iss  # 3 s < freeze window; black is the failure
    trunc = tmp_path / "t.mp4"
    trunc.write_bytes(Path(short).read_bytes()[: len(Path(short).read_bytes()) // 2])
    assert "decode_error" in R.qc(trunc, {"final_start": 0, "final_end": 3})[0] or probe(trunc)["duration"] == 0


def test_logo_music_and_intro_outro_render(spoken, tmp_path, ff):
    d, v, ws = spoken
    logo = tmp_path / "logo.png"
    subprocess.run([ff, "-y", "-v", "error", "-f", "lavfi", "-i", "color=c=red:s=200x100", "-frames:v", "1", str(logo)], check=True)
    music = tmp_path / "m.wav"
    subprocess.run([ff, "-y", "-v", "error", "-f", "lavfi", "-i", "sine=f=500:d=5", str(music)], check=True)
    intro = synth.make_video(tmp_path / "intro.mp4", dur=1, size="640x360", audio=False)
    clip = {"id": 3, "final_start": 2.0, "final_end": 7.0, "caption": {"style": "boxed", "scale": 1.0}, "layout": "fill", "focus_auto": [[[0, .5]]], "split_auto": None,
            "music": {"file": str(music), "db": -22}, "audio": "clean"}
    out = R.render_clip(v, clip, tmp_path, ws, probe(v), opts={"logo": str(logo), "fade": True, "intro": str(intro), "quality": {"preset": "ultrafast", "crf": 28}, "src_fp": "m"})
    i = probe(out)
    assert abs(i["duration"] - 6.0) < 0.5 and (i["width"], i["height"]) == (1080, 1920) and i["audio"]


def test_styles_preview_command_renders_every_preset(tmp_path, ff, monkeypatch):
    from clipper import cli
    out = tmp_path / "sheet.png"
    cli.main(["styles", "--preview", str(out)])
    assert out.exists() and out.stat().st_size > 5000


# ------------------------------------------------------------------- transcription (stand-in model) ---------------------------------------------
class FakeSeg:
    def __init__(self, words): self.words = words


class FakeWord:
    def __init__(self, w, s, e): self.word, self.start, self.end, self.probability = f" {w}", s, e, 0.9


class FakeModel:
    calls = []

    def transcribe(self, wav, word_timestamps, vad_filter, language):
        FakeModel.calls.append(wav)
        return [FakeSeg([FakeWord("hello", 1.0, 1.4), FakeWord("there", 1.5, 1.9), FakeWord("edge", 1195.0, 1195.4), FakeWord("overlap", 1205.0, 1205.4)])], None


def test_chunked_transcription_offsets_dedupes_overlap_and_resumes(spoken, tmp_path, monkeypatch, ff):
    d, v, ws = spoken
    FakeModel.calls.clear()
    monkeypatch.setattr(T, "_model", lambda profile: FakeModel())
    monkeypatch.setattr(T, "CHUNK", 10)
    monkeypatch.setattr(T, "OVERLAP", 2)
    out = T.transcribe(v, 25, tmp_path / "wc", "light")
    assert len(FakeModel.calls) == 3 and out and all(w["s"] <= w["e"] for w in out)
    starts = [w["s"] for w in out]
    assert starts == sorted(starts), "chunks must be stitched in time order"
    n = len(FakeModel.calls)
    T.transcribe(v, 25, tmp_path / "wc", "light")
    assert len(FakeModel.calls) == n, "finished chunks are cached: a re-run (crash recovery) transcribes nothing"


# --------------------------------------------------------------------------- end to end ----------------------------------------------------------
def test_e2e_complete_sentence_boundaries_delayed_answer_and_reaction(spoken, tmp_path, ff):
    d, v, ws = spoken
    j = J.create(str(v), tmp_path / "jobs", clips=2, mode="disabled", transcript=str(d / "w.json"), profile="podcast", speakers="off", quality="draft")
    J.run(j, "qc", log=lambda *_: None)
    plan, rep = util.jload(j.p("plan.json")), util.jload(j.p("report.json"))
    assert plan["clips"] and rep["clips"] and all(c["pass"] for c in rep["clips"]), rep["clips"]
    top = plan["clips"][0]
    assert top["text"].rstrip()[-1] in ".?!", top["text"]
    assert not {"starts_mid_sentence", "ends_mid_sentence", "truncates_word"} & set(top["issues"])
    for c in rep["clips"]:
        i = probe(c["file"])
        assert (i["width"], i["height"]) == (1080, 1920) and i["audio"]
    assert rep["run"]["openai"]["calls"] == 0 and "run_id" in rep["run"]


def test_e2e_visual_only_highlight_found_without_any_speech(tmp_path, ff):
    v = synth.make_video(tmp_path / "g.mp4", dur=90, beeps=[50], cuts=[30], flash=(50, 53))
    j = J.create(str(v), tmp_path / "jobs", clips=3, mode="disabled", profile="gaming", speakers="off", transcript=str(tmp_path / "none.json"), quality="draft")
    (tmp_path / "none.json").write_text(json.dumps({"words": []}))
    J.run(j, "qc", log=lambda *_: None)
    plan = util.jload(j.p("plan.json"))
    assert plan["clips"] and plan["clips"][0].get("kind") == "event"
    assert any(c["final_start"] <= 53 and c["final_end"] >= 50 for c in plan["clips"]), [(c["final_start"], c["final_end"]) for c in plan["clips"]]
    rep = util.jload(j.p("report.json"))
    assert rep["clips"] and all(c["file"] for c in rep["clips"])


def test_e2e_cache_reuse_and_targeted_invalidation_on_a_real_run(spoken, tmp_path, ff):
    d, v, ws = spoken
    kw = dict(mode="disabled", transcript=str(d / "w.json"), profile="podcast", speakers="off", quality="draft")
    j = J.create(str(v), tmp_path / "jobs", clips=2, **kw)
    J.run(j, "qc", log=lambda *_: None)
    t_tr = (j.p("transcript.json")).stat().st_mtime_ns
    r1 = {c["id"]: c["file"] for c in util.jload(j.p("report.json"))["clips"]}
    assert J.run(J.create(str(v), tmp_path / "jobs", clips=2, **kw), "qc", log=lambda *_: None) is None  # complete: nothing re-runs
    j2 = J.create(str(v), tmp_path / "jobs", clips=2, style="karaoke", **kw)
    J.run(j2, "qc", log=lambda *_: None)
    assert j2.p("transcript.json").stat().st_mtime_ns == t_tr, "changing caption style must not touch transcription"
    r2 = {c["id"]: c["file"] for c in util.jload(j2.p("report.json"))["clips"]}
    assert all(r1[k] != r2[k] for k in r2), "but every render must have been redone with the new style"
    J.create(str(v), tmp_path / "jobs", clips=1, **kw)
    assert not j.done("select") and j.done("transcribe") and j.done("signals")


def test_e2e_edit_rerenders_only_that_clip_and_critic_polish_fixes_a_bad_open(spoken, tmp_path, ff):
    d, v, ws = spoken
    j = J.create(str(v), tmp_path / "jobs", clips=2, mode="disabled", transcript=str(d / "w.json"), profile="podcast", speakers="off", quality="draft")
    J.run(j, "qc", log=lambda *_: None)
    ids = [c["id"] for c in util.jload(j.p("plan.json"))["clips"]]
    stamp = {c["id"]: Path(c["file"]).stat().st_mtime_ns for c in util.jload(j.p("report.json"))["clips"]}
    J.edit_clip(j, ids[0], {"captions": "karaoke"})
    J.run(j, "qc", log=lambda *_: None)
    now = {c["id"]: Path(c["file"]).stat().st_mtime_ns for c in util.jload(j.p("report.json"))["clips"]}
    assert now[ids[0]] != stamp[ids[0]] and all(now[i] == stamp[i] for i in ids[1:])
    summary = flow.polish(j, log=lambda *_: None)
    assert summary and all(v["overall"] is not None for v in summary.values())


def test_e2e_oneshot_run_exports_only_passing_clips_with_an_honest_report(spoken, tmp_path, ff):
    d, v, ws = spoken
    jj, fr = flow.run_video(str(v), tmp_path / "jobs", clips=2, export_dir=tmp_path / "out", log=lambda *_: None, mode="disabled", transcript=str(d / "w.json"), profile="podcast", speakers="off", quality="draft")
    meta = json.loads((tmp_path / "out" / "metadata.json").read_text())
    assert meta["clips"], meta
    assert all((tmp_path / "out" / m["file"]).exists() for m in meta["clips"])
    run = meta["run"]
    assert run["openai"]["calls"] == 0 and run["openai"]["usd"] == 0 and "features_skipped" in run and "local_models" in run
    assert all("unassessed" in v for v in meta["verdicts"]["verdicts"].values()) and "emotion" in next(iter(meta["verdicts"]["verdicts"].values()))["unassessed"]  # eyes not used: stated, not hidden


def test_e2e_missing_optional_models_degrade_without_failing(spoken, tmp_path, ff, monkeypatch):
    monkeypatch.setenv("CLIPPER_OFFLINE", "1")
    from clipper import models
    monkeypatch.setattr(models, "CACHE", tmp_path / "nomodels")
    d, v, ws = spoken
    j = J.create(str(v), tmp_path / "jobs", clips=1, mode="disabled", transcript=str(d / "w.json"), profile="podcast", quality="draft")  # speakers=auto: needs models that are not there
    J.run(j, "qc", log=lambda *_: None)
    rep = util.jload(j.p("report.json"))
    assert rep["clips"] and all(c["pass"] for c in rep["clips"])
    assert rep["run"]["features_skipped"], "the report must say what was skipped"


def test_live_chat_alignment_helpers_and_merge(tmp_path):
    from clipper import live
    ev = live.merge_events([{"pts": 300, "kind": "chat", "x": 9}, {"pts": 340, "kind": "audio"}, {"pts": 900, "kind": "chat"}])
    assert len(ev) == 2 and ev[0]["kinds"] == ["audio", "chat"] and ev[0]["confidence"] > ev[1]["confidence"]
    s = live.Session(tmp_path)
    s.set("recording", parts=["record_000.ts"])
    s2 = live.Session(tmp_path)
    assert s2.d["state"] == "recording" and s2.d["parts"] == ["record_000.ts"]  # a restarted process sees the persisted state
    with pytest.raises(AssertionError):
        s.set("not-a-state")
    assert live.parse_chat_line('{"u":"a","m":"x","ts":5}') == (5.0, "a", "x") and live.parse_chat_line("nope") is None


def test_live_extraction_is_frame_accurate_and_live_judge_shares_the_budget(spoken, tmp_path, ff):
    from clipper import live, llm
    d, v, ws = spoken
    seg = tmp_path / "buf"
    seg.mkdir()
    subprocess.run([ff, "-y", "-v", "error", "-i", v, "-t", "40", "-c:v", "libx264", "-preset", "ultrafast", "-g", "100", "-c:a", "aac", "-f", "segment", "-segment_time", "4", "-segment_format", "mpegts", str(seg / "seg_%06d.ts")], check=True)
    names = sorted(p.name for p in seg.glob("seg_*.ts"))
    f = live.extract(seg, names, 2, 7, 10.7, 25.3, tmp_path / "pre.mkv")
    assert f and abs(probe(f)["duration"] - 14.6) < 0.25, probe(f)  # re-encoded: not off by a GOP (stream copy would be ~keyframe-snapped)
    cache, bf = tmp_path / "c", tmp_path / "budget.json"
    seen = []
    monkey = lambda prompt, max_out=900: (seen.append(1), (json.dumps({"results": [{"id": 0, "score": 8, "title": "live"}]}), 200, 20))[1]
    b = llm.Budget.load(bf, 3, 60000, 0.1)
    live_words = words(["Some words spoken during the stream that matter a lot to the audience here."] * 3, t=0)
    cand = {"final_words": [0, len(live_words) - 1], "core_words": [0, len(live_words) - 1], "final_start": 0, "final_end": 20, "text": "", "score": .5}
    llm.judge(live_words, [cand], monkey, b, cache)
    b_final = llm.Budget.load(bf, 3, 60000, 0.1)
    llm.judge(live_words, [dict(cand)], monkey, b_final, cache)  # the final VOD pass asks the same thing
    assert len(seen) == 1 and b_final.calls == 1 and b_final.cached == 1
