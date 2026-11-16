"""One runnable check: `clipper selftest` (or python -m tests.selftest). Offline; the ffmpeg e2e part is skipped if no ffmpeg."""
import json, os, random, re, subprocess, sys, tempfile, time, urllib.request
from pathlib import Path
from clipper import bench, boundaries as B, captions, job as J, live, llm, signals as S, render as R
from clipper.util import ffmpeg, probe


def gen(sentences, t=0.0, wd=0.35, gap=0.08, pause=0.5):
    """sentences -> word list with timings; `pause` between sentences."""
    out = []
    for s in sentences:
        for w in s.split():
            out.append({"w": w, "s": round(t, 3), "e": round(t + wd, 3), "p": 1.0})
            t += wd + gap
        t += pause
    return out


TEXT = [
    "I genuinely thought we were going to lose the whole game.",       # 0 setup
    "But nobody expected that to happen.",                              # 1 dependent
    "And that is when I realized that he was actually the person who had been helping us all along.",  # 2
    "Wow.",                                                             # 3 reaction
    "Anyway let us talk about something else entirely for a while now.",  # 4
]


def check_boundaries():
    words = gen(TEXT)
    sents = B.sentences(words)
    assert len(sents) == 5, sents
    # core = sentence 1 (starts with "But"): setup sentence 0 must be pulled in
    c = B.refine(words, *sents[1], sents=sents, min_s=0)
    assert c["final_words"][0] == sents[0][0], c
    # core ends mid-sentence 2 -> must run to the end of the thought, then keep the "Wow." reaction
    mid = sents[2][0] + 8
    c = B.refine(words, sents[2][0], mid, sents=sents, min_s=0)
    assert c["final_words"][1] == sents[3][1], c["text"]
    assert c["text"].rstrip().endswith("Wow."), c["text"]
    assert B.validate(words, c, min_s=0) == [], B.validate(words, c, min_s=0)
    # times come from words, padding never eats the neighbouring word
    f0, f1 = c["final_words"]
    assert c["final_start"] <= words[f0]["s"] and c["final_end"] >= words[f1]["e"]
    assert (f0 == 0 or c["final_start"] >= words[f0 - 1]["e"]) and c["final_end"] <= words[f1 + 1]["s"]
    # validator catches a hard cut mid-sentence
    bad = B._pack(words, 0, 3, sents[2][0], sents[2][0] + 5)
    assert "ends_mid_sentence" in B.validate(words, bad, min_s=0)
    # duration floor wins over "tiny clip"
    assert B.refine(words, *sents[1], sents=sents)["final_end"] - B.refine(words, *sents[1], sents=sents)["final_start"] >= 12


def check_signals():
    random.seed(1)
    quiet = [15 + random.randint(-3, 3) for _ in range(120)]
    assert S.spikes(quiet) == []                       # adaptive baseline: no false alarm on noise
    assert 80 in S.spikes(quiet[:80] + [120] + quiet[81:])   # 15/10s -> 120/10s is caught
    ev = live.merge_events([{"pts": 300, "kind": "chat", "x": 9}, {"pts": 340, "kind": "audio"}, {"pts": 900, "kind": "chat"}])
    assert len(ev) == 2 and ev[0]["kinds"] == ["audio", "chat"] and ev[0]["confidence"] > ev[1]["confidence"], ev


def check_captions():
    words = gen(TEXT)[:20]
    for name in captions.PRESETS:
        ass = captions.build(words, words[0]["s"], name)
        times = [re.search(r"Dialogue: 0,([\d:.]+),([\d:.]+)", l) for l in ass.splitlines() if l.startswith("Dialogue")]
        assert times and all(t[1] <= t[2] for t in times), name
    big = captions.chunks(words, captions.PRESETS["bold_pop"], 1.0)
    assert all(len(c) <= 3 for c in big)


def check_edit_rules():
    cr = R.shot_cuts(100, 130, [100.5, 110, 110.8, 129.5, 120])
    assert cr == [10.0, 20.0], cr                       # cuts near edges / each other are noise
    one = R.crop_expr([0.48, 0.5, 0.52], [10, 20])
    assert "if(" not in one                             # shots agree -> no movement at all
    pw = R.crop_expr([0.2, 0.7], [10])
    assert pw == "if(lt(t,10),0.200,0.700)"             # changes only at the cut, never interpolates
    assert "0.120*max(0" in R.zoom_expr(1, [{"t": 3, "dur": 2, "amt": 1.12}])


def check_bench():
    clips = [{"final_start": 10, "final_end": 40, "issues": []}, {"final_start": 100, "final_end": 120, "issues": ["dead_air"]}]
    r = bench.score_plan(clips, [{"start": 12, "end": 41}, {"start": 500, "end": 530}])
    assert r == {"recall": 0.5, "start_err": 2.0, "end_err": 1.0, "issue_rate": 0.5}, r


def check_llm(tmp):
    words = gen(TEXT)
    sents = B.sentences(words)
    cands = [B.refine_checked(words, *sents[1], sents) | {"score": .5, "peak": 5}]
    calls = []

    def fake(prompt):
        calls.append(prompt)
        a = int(re.search(r"\[(\d+)\]But", prompt)[1])
        return json.dumps({"results": [{"id": 0, "score": 9, "standalone": True, "start_word": a, "end_word": a + 5, "title": "t"},
                                       {"id": 7, "score": 9}, {"id": "x"}]}), 1000, 100   # bad ids must be ignored
    b = llm.Budget(2)
    llm.judge(words, cands, fake, b, tmp)
    assert cands[0]["llm_score"] == .9 and b.calls == 1
    llm.judge(words, cands, fake, b, tmp)               # same prompt -> cache, no second paid call
    assert b.calls == 1 and b.cached == 1 and len(calls) == 1
    assert b.report()["usd"] > 0
    assert llm.provider_for("disabled") is None


def check_e2e(tmp):
    try:
        ffmpeg()
    except SystemExit:
        print("  (ffmpeg missing: e2e skipped)")
        return
    tmp = Path(tmp)
    vid = tmp / "t.mp4"
    subprocess.run([ffmpeg(), "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=1280x720:rate=25", "-f", "lavfi", "-i", "sine=f=300",
                    "-t", "150", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", vid], check=True)
    words = gen([f"This is sentence number {i} and it keeps going for a bit." for i in range(26)] + ["Wow."], pause=1.0)
    (tmp / "words.json").write_text(json.dumps({"words": words}))
    j = J.create(str(vid), tmp / "jobs", clips=3, mode="disabled", transcript=str(tmp / "words.json"), profile="podcast")
    assert J.run(j, "qc", log=lambda *_: None) is None
    rep = json.loads(j.p("report.json").read_text())
    assert rep["clips"], rep
    for c in rep["clips"]:
        i = probe(c["file"])
        assert (i["width"], i["height"]) == (1080, 1920) and i["audio"], (c, i)
        assert c["pass"], c                              # boundary + render QC pass end to end
    # scene-aware crop + motivated zoom: a cut at 6 s with different focus per shot, a zoom inside shot 2, one crossing the cut (dropped)
    c = {"id": 9, "final_start": 3.0, "final_end": 15.0, "caption": {"style": "boxed"}, "layout": "fill", "focus_auto": [0.2, 0.8],
         "zooms": [{"t": 4.0, "dur": 3.0, "amt": 1.2}, {"t": 7.0, "dur": 2.0, "amt": 1.15}]}
    o = R.render_clip(vid, c, tmp / "sc", words, probe(vid), cuts=[9.0])
    assert not R.qc(o, c)[0], R.qc(o, c)
    # 720p source cropped to 9:16 then scaled to 1920 high = 2.7x -> flagged
    assert c["warn"] == ["low_res_source"]
    # resume: nothing re-runs; an edit re-renders only the touched clip
    stamp = {c["id"]: Path(c["file"]).stat().st_mtime_ns for c in rep["clips"]}
    plan = json.loads(j.p("plan.json").read_text())
    plan["clips"][0]["caption"]["style"] = "karaoke"
    j.p("plan.json").write_text(json.dumps(plan))
    j.invalidate("render")
    J.run(j, "qc", log=lambda *_: None)
    rep2 = json.loads(j.p("report.json").read_text())
    same = [c["id"] for c in rep2["clips"] if Path(c["file"]).stat().st_mtime_ns == stamp.get(c["id"])]
    assert len(rep2["clips"]) > 1 and same == [c["id"] for c in rep2["clips"][1:]], (same, stamp)
    # live: buffer segments -> concat -> cut a window
    seg = tmp / "buf"
    seg.mkdir()
    subprocess.run([ffmpeg(), "-y", "-v", "error", "-i", vid, "-c", "copy", "-f", "segment", "-segment_time", "4", "-segment_format", "mpegts", seg / "seg_%06d.ts"], check=True)
    names = sorted(p.name for p in seg.glob("seg_*.ts"))
    f = live.extract(seg, names, 2, 7, 10, 25, tmp / "pre.mkv")
    assert f and abs(probe(f)["duration"] - 15) < 1.5, f


def check_server(tmp):
    from clipper import server
    for bad in ({"src": "-x"}, {"src": "a", "evil": 1}, {"src": "a", "style": "nope"}):
        try:
            server.build_argv("clip_video", bad)
            raise AssertionError(bad)
        except server.ToolError:
            pass
    assert server.build_argv("render_clips", {"job": "0123456789", "draft": False})[0] == ["render", "0123456789", "--no-draft"]
    tmp = Path(tmp)
    if not (tmp / "t.mp4").exists():
        return
    env = {**os.environ, "CLIPPER_JOBS": str(tmp / "jobs2"), "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    p = subprocess.Popen([sys.executable, "-m", "clipper", "mcp"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, cwd=tmp, env=env)

    def rpc(i, method, params=None):
        p.stdin.write(json.dumps({"jsonrpc": "2.0", "id": i, "method": method, "params": params or {}}) + "\n")
        p.stdin.flush()
        return json.loads(p.stdout.readline())["result"]
    try:
        assert rpc(1, "initialize", {"protocolVersion": "2024-11-05"})["serverInfo"]["name"] == "clipper"
        assert {"clip_video", "edit_clip", "monitor_livestream"} <= {t["name"] for t in rpc(2, "tools/list")["tools"]}
        assert rpc(3, "tools/call", {"name": "clip_video", "arguments": {"src": "x", "style": "nope"}})["isError"]
        r = json.loads(rpc(4, "tools/call", {"name": "clip_video", "arguments": {"src": "t.mp4", "transcript": "words.json", "mode": "disabled", "clips": 2, "stop_after": "select"}})["content"][0]["text"])
        for _ in range(120):                       # queued task runs in the persistent worker
            t = json.loads(rpc(5, "tools/call", {"name": "get_task", "arguments": {"task": r["task"]}})["content"][0]["text"])
            if t["status"] in ("done", "failed"):
                break
            time.sleep(1)
        assert t["status"] == "done", t
        st = json.loads(rpc(6, "tools/call", {"name": "get_job_status", "arguments": {"job": r["job"]}})["content"][0]["text"])
        assert st["clips"], st
    finally:
        p.kill()


def main():
    with tempfile.TemporaryDirectory() as d:
        for name, fn in [("boundaries", check_boundaries), ("signals", check_signals), ("captions", check_captions), ("edit rules", check_edit_rules), ("bench", check_bench),
                         ("llm", lambda: check_llm(d + "/c")), ("e2e", lambda: check_e2e(d)), ("server", lambda: check_server(d))]:
            fn()
            print("ok ", name)
    print("All checks passed")


if __name__ == "__main__":
    main()
