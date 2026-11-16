"""One runnable check: `clipper selftest` (or python -m tests.selftest). Offline; the ffmpeg e2e part is skipped if no ffmpeg."""
import json, os, random, re, subprocess, sys, tempfile, time, urllib.request
from pathlib import Path
from clipper import bench, boundaries as B, critics as C, extras, upscale, captions, job as J, live, llm, signals as S, render as R
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
    one = R.crop_expr([[[0, 0.48]], [[10, 0.5]], [[20, 0.52]]], [10, 20])
    assert "if(" not in one                             # shots agree -> no movement at all
    pw = R.crop_expr([[[0, 0.2]], [[10, 0.7]]], [10])
    assert pw == "if(lt(t,10),0.200,0.700)"             # changes only at the cut, never interpolates
    assert "0.120*max(0" in R.zoom_expr(1, [{"t": 3, "dur": 2, "amt": 1.12}])


def check_bench():
    clips = [{"final_start": 10, "final_end": 40, "issues": []}, {"final_start": 100, "final_end": 120, "issues": ["dead_air"]}]
    r = bench.score_plan(clips, [{"start": 12, "end": 41}, {"start": 500, "end": 530}])
    assert r == {"recall": 0.5, "start_err": 2.0, "end_err": 1.0, "issue_rate": 0.5}, r


def check_phase2_pure():
    # tracking: a still subject gives no movement; a mover glides (and only after leaving the dead zone)
    still = extras.smooth_track([(t, 0.5 + (0.02 if t % 2 else -0.02)) for t in range(10)])
    assert len(still) == 1, still
    mover = extras.smooth_track([(t, 0.3 if t < 5 else 0.7) for t in range(10)])
    assert mover[-1][1] == 0.7 and len(mover) <= 4, mover
    e = R.crop_expr([[list(k) for k in mover]], [])
    assert "(t-" in e and "if(lt(t" in e
    # two people persistently apart -> split pair; one person -> none
    assert extras.pair_of([[(0.25, .1), (0.75, .1)]] * 5) == (0.25, 0.75)
    assert extras.pair_of([[(0.5, .1)]] * 5) is None
    # PANNs / visual / chat-file / speech scoring helpers
    assert S.event_scores([[0.1, 0.9, 0.2]], ["Speech", "Laughter", "Music"]) == [0.9]
    assert len(S.fuse_visual([1, 5, 2], [0, 1, 0])) == 3
    assert live.parse_chat_line('{"u":"a","m":"x","ts":5}') == (5.0, "a", "x") and live.parse_chat_line("nope") is None
    assert S.word_score({"w": "haha!"}) > S.word_score({"w": "the"})
    # YouTube adapter with a fake API (no network)
    got, calls = [], []

    def fetch(url):
        calls.append(url)
        if "videos?" in url:
            return {"items": [{"liveStreamingDetails": {"activeLiveChatId": "CHAT"}}]}
        if len(calls) > 2:
            return {"items": [], "pollingIntervalMillis": 10}
        return {"items": [{"snippet": {"publishedAt": "2026-01-01T00:00:10Z", "displayMessage": "wow"}, "authorDetails": {"displayName": "u"}}],
                "nextPageToken": None, "pollingIntervalMillis": 10}
    import threading
    live.youtube_chat("https://youtu.be/abcdefghijk", lambda *a: got.append(a), threading.Event(), "KEY", fetch)
    assert got and got[0][2] == "wow" and "liveChatId=CHAT" in calls[-1]
    # b-roll keyword matching
    with tempfile.TemporaryDirectory() as d:
        (Path(d) / "pizza.png").write_bytes(b"x")
        (Path(d) / "golden_goal.png").write_bytes(b"x")
        ws = gen(["We ordered a pizza after the match.", "That golden goal was unreal."], t=100)
        m = extras.match_broll(ws, d, 100, 120, gap=2.0)
        assert [Path(b["file"]).stem for b in m] == ["pizza", "golden_goal"] and m[0]["t"] > 0, m


def check_live_stt(tmp):
    """LiveTranscriber with a stand-in model: words get stream PTS, the overlap segment is never emitted twice."""
    tmp = Path(tmp)
    vid = tmp / "t.mp4"
    if not vid.exists():
        return
    seg = tmp / "buf2"
    seg.mkdir()
    subprocess.run([ffmpeg(), "-y", "-v", "error", "-i", vid, "-t", "40", "-c:v", "libx264", "-preset", "ultrafast", "-g", "25", "-c:a", "aac", "-f", "segment", "-segment_time", "4", "-segment_format", "mpegts", seg / "seg_%06d.ts"], check=True)
    names = sorted(p.name for p in seg.glob("seg_*.ts"))
    calls = []

    def fake(wav):
        calls.append(wav)
        return [{"w": "hi", "s": 5.0, "e": 5.3}, {"w": "there", "s": 5.5, "e": 5.8}]
    lt = live.LiveTranscriber(seg, fake, every=16)
    assert lt.poll(names[:3]) == []                       # not enough new audio yet
    a = lt.poll(names[:5])
    assert [w["w"] for w in a] == ["hi", "there"] and a[0]["s"] == 5.0, a
    b = lt.poll(names[:9])                                # same fake words land at seg-offset 16s+: new, later PTS
    assert b and b[0]["s"] > a[-1]["e"], (a, b)


def check_critics(tmp):
    """Deliberately bad clips: the editor must name the problems, propose the right edit, and apply it once."""
    tmp = Path(tmp)
    sents = ["Um so yeah let me tell you about the game.", "And then the whole team just collapsed completely.", "Why did nobody see that coming?",
             "Because the coach never trained them for it at all.", "Anyway that is the story of that season."]
    words = gen(sents)
    ss = B.sentences(words)
    j = J.Job(tmp / "cj", "0123456789")
    base = {"caption": {"style": "bold_pop", "scale": 1.0}, "layout": "fill", "zoom": None, "zooms": [], "focus_x": None, "peak": 5, "hook": 0.5, "score": .5, "text": ""}

    def clip(i, a, b, sc):
        return {**base, "id": i, "final_score": sc, "final_words": [ss[a][0], ss[b][1]], "final_start": words[ss[a][0]]["s"] - .15, "final_end": words[ss[b][1]]["e"] + .4}
    plan = {"clips": [clip(1, 0, 2, .6), clip(2, 1, 3, .5), clip(3, 3, 3, .4), clip(4, 0, 2, .3)], "cost": {}}
    c3 = plan["clips"][2]
    c3["final_end"] = words[ss[3][0] + 5]["e"] + .2           # cut mid-sentence
    J.jsave(j.p("plan.json"), plan)
    J.jsave(j.p("transcript.json"), {"words": words})
    J.jsave(j.p("source.json"), {"path": "x", "info": {"duration": 60, "width": 1280, "height": 720, "audio": True, "fps": 25}})
    J.jsave(j.p("job.json"), {"profile": "auto", "src": "x"})
    C.run_critics(j, ["cut", "story", "pace", "captions", "framing"])
    got = {(f["clip"], f["code"]): f for r in C.load_reports(j).values() for cl in r["clips"].values() for f in cl["findings"]}
    assert (1, "weak_open") in got and (1, "unanswered_question") in got, sorted(got)
    assert (3, "cut_mid_sentence") in got and (2, "needs_setup") in got and (4, "duplicate_content") in got, sorted(got)
    # the proposed fix for the unanswered question extends to the end of the answer sentence
    e = got[(1, "unanswered_question")]["edit"]["end_shift"]
    assert abs(plan["clips"][0]["final_end"] + e - (words[ss[3][1]]["e"] + .4)) < .01
    applied, esc = C.apply_fixes(j)
    assert applied and all("clip" in a for a in applied)
    plan2 = J.jload(j.p("plan.json"))
    assert plan2["clips"][3].get("dropped"), "duplicate (lower score) must be dropped"
    assert len({a.split(":")[0] for a in applied}) == len(applied), applied   # at most one structural change per clip per round
    again, esc2 = C.apply_fixes(j)                              # same findings (no re-critique): nothing is applied twice
    assert not [a for a in again if any(a.split("->")[0] == b.split("->")[0] for b in applied)], (applied, again)
    try:
        C.vision_submit(j, str(tmp / "nope.json"))
    except (SystemExit, TypeError, AttributeError):
        pass


def check_render_extras(tmp):
    """b-roll overlay, ducked music, audio cleanup, two-person split, Real-ESRGAN plumbing (stand-in binary)."""
    tmp = Path(tmp)
    vid = tmp / "t.mp4"
    if not vid.exists():
        return
    ff = ffmpeg()
    os.environ["CLIPPER_FFMPEG"] = ff
    sh = lambda *a: subprocess.run([ff, "-y", "-v", "error", *a], check=True)
    sh("-f", "lavfi", "-i", "color=red:s=400x300", "-frames:v", "1", tmp / "pizza.png")
    sh("-f", "lavfi", "-i", "sine=f=500:d=5", tmp / "m.wav")
    words = gen([f"Sentence about pizza number {i} goes on for a while." for i in range(10)])
    info = probe(vid)
    c = {"id": 21, "final_start": 2, "final_end": 10, "caption": {"style": "karaoke"}, "layout": "fill", "focus_auto": [[[0, .5]]], "split_auto": None,
         "broll": [{"file": str(tmp / "pizza.png"), "t": 2.0, "dur": 2.0}], "music": {"file": str(tmp / "m.wav"), "db": -22}, "audio": "clean"}
    o = R.render_clip(vid, c, tmp / "x1", words, info)
    assert not R.qc(o, c)[0], R.qc(o, c)
    c2 = {**c, "id": 22, "broll": [], "music": None, "audio": None, "split_auto": [0.25, 0.75]}
    o2 = R.render_clip(vid, c2, tmp / "x1", words, info)
    assert not R.qc(o2, c2)[0], R.qc(o2, c2)
    # AI upscale plumbing: a stand-in for realesrgan-ncnn-vulkan that doubles the frames
    sh("-f", "lavfi", "-i", "testsrc=size=640x360:rate=25", "-f", "lavfi", "-i", "sine=f=300", "-t", "3", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", tmp / "s.mp4")
    fake = tmp / "fake-esrgan"
    fake.write_text("#!/usr/bin/env python3\nimport sys, subprocess, os\na = sys.argv\ni, o = a[a.index('-i') + 1], a[a.index('-o') + 1]\n"
                    "subprocess.run([os.environ['CLIPPER_FFMPEG'], '-y', '-v', 'error', '-i', i + '/%06d.png', '-vf', 'scale=iw*2:ih*2', o + '/%06d.png'], check=True)\n")
    fake.chmod(0o755)
    small = probe(tmp / "s.mp4")
    upscale.TOOL = str(fake)
    c3 = {"id": 23, "final_start": 0.0, "final_end": 2.0, "caption": {"style": "none"}, "layout": "fill", "focus_auto": [[[0, .5]]], "split_auto": None}
    o3 = R.render_clip(tmp / "s.mp4", c3, tmp / "x3", words, small)
    ups = list((tmp / "x3").glob("up_*.mp4"))
    assert ups and probe(ups[0])["width"] == 1280 and c3["warn"] == [] and not R.qc(o3, c3)[0]
    upscale.TOOL = "definitely-not-installed"
    c4 = {**c3, "id": 24}
    R.render_clip(tmp / "s.mp4", c4, tmp / "x3", words, small)
    assert c4["warn"] == ["low_res_source"], c4["warn"]          # no binary: lanczos + honest warning


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
    # editor-critic on the real renders: measures frames + loudness, vision flow refuses unsupported claims
    C.run_critics(j, ["cut", "framing", "frames", "audio"])
    assert {"frames", "audio"} <= set(C.load_reports(j)), C.load_reports(j).keys()
    imgs = C.vision_packet(j)
    assert imgs and all(j.p("review", "sheets", n).exists() for n in imgs)
    bad = tmp / "bad.json"
    bad.write_text(json.dumps({"opened": [], "findings": []}))
    try:
        C.vision_submit(j, str(bad))
        raise AssertionError("vision submit must reject unopened images")
    except SystemExit:
        pass
    ok = tmp / "ok.json"
    ok.write_text(json.dumps({"opened": imgs, "findings": [{"clip": 1, "severity": "minor", "problem": "caption low", "evidence": "frame 3 shows the caption in the lower third over the grid", "fix": "none"}]}))
    C.vision_submit(j, str(ok))
    assert "emotion" in C.summarize(j)[1]["assessed"]
    # scene-aware crop + motivated zoom: a cut at 6 s with different focus per shot, a zoom inside shot 2, one crossing the cut (dropped)
    c = {"id": 9, "final_start": 3.0, "final_end": 15.0, "caption": {"style": "boxed"}, "layout": "fill", "focus_auto": [[[0, 0.2]], [[6, 0.8]]], "split_auto": None,
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
                         ("phase2 pure", check_phase2_pure), ("critics", lambda: check_critics(d)), ("llm", lambda: check_llm(d + "/c")), ("e2e", lambda: check_e2e(d)), ("render extras", lambda: check_render_extras(d)), ("live stt", lambda: check_live_stt(d)), ("server", lambda: check_server(d))]:
            fn()
            print("ok ", name)
    print("All checks passed")


if __name__ == "__main__":
    main()
