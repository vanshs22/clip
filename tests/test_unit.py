"""Unit tests: pure logic, no ffmpeg, no models, no network."""
import base64, collections, json, os, random, tarfile, time
from pathlib import Path
import pytest
from clipper import boundaries as B, captions, critics, extras, kick, llm, models, render as R, score, signals as S, util
from tests.synth import words


# ---------------------------------------------------------------- boundary engine: the hard cases ----------------------------------------
def sent_ranges(ws):
    return B.sentences(ws)


def test_unpunctuated_run_is_split_at_pauses_not_swallowed():
    ws = words([("so this is a very long ramble with no punctuation at all " * 6).strip()], wd=0.4, gap=0.02)
    for i in (20, 45, 70):  # pauses of 0.3 s inside the ramble
        for w in ws[i:]:
            w["s"] += 0.3
            w["e"] += 0.3
    assert max(b - a for a, b in B.sentences(ws)) < len(ws) - 1, "a punctuation-free monologue must not be one sentence"


def test_long_pause_inside_a_sentence_still_ends_on_the_full_thought():
    ws = words(["I really thought we would lose", ("but then it happened and we won.", 1.0), "Amazing."], pause=1.2)  # pause > 0.8 s splits the sentence
    for w in ws[6:]:
        w["s"] += 1.0
        w["e"] += 1.0
    c = B.refine_checked(ws, 0, 5)
    assert c["text"].rstrip().endswith(("won.", "Amazing.")) and "ends_mid_sentence" not in c["issues"]


def test_question_followed_by_delayed_answer_includes_the_answer():
    ws = words(["Why did nobody see that coming?", ("Because the coach never trained them for it at all.", 0.5), "Next topic starts here with other words entirely now."], pause=0.5)
    for w in ws[6:]:
        w["s"] += 3.0
        w["e"] += 3.0  # 3.5 s of silence before the answer
    c = B.refine(ws, 0, 5, min_s=0)
    assert "coach never trained" in c["text"], c["text"]
    assert "Next topic" not in c["text"]


def test_delayed_punchline_with_laughter_tail_is_kept():
    ws = words(["So I asked the barista for the secret ingredient.", ("Honestly just more sugar.", 6.0), "Anyway moving on to something else completely different today."])
    laugh = [0] * 200
    end = ws[[w["w"] for w in ws].index("sugar.")]["e"]
    for t in range(int(end), int(end) + 4):
        laugh[t] = 1
    c = B.refine(ws, 0, 7, min_s=0, reaction=laugh)
    assert c["final_end"] >= int(end) + 3, c  # the audience reaction is not cut off
    assert c["final_end"] < ws[-9]["s"], "but it must not run into the next topic"


def test_interrupting_speakers_with_overlapping_timestamps_do_not_break_boundaries():
    ws = words(["I think the real problem is the budget.", "No wait that is not what happened at all."])
    ws[8]["s"] = ws[7]["e"] - 0.2  # overlap: second speaker starts before the first finishes
    sents = B.sentences(ws)
    c = B.refine_checked(ws, 0, len(ws) - 1)
    assert c["final_start"] >= 0 and c["final_end"] > c["final_start"] and sents


def test_reaction_after_main_statement_is_included():
    ws = words(["He just walked right through the door and won the whole thing outright.", "Wow.", ("Anyway.", 3.0), "Totally different subject starts a long way after this moment."])
    c = B.refine(ws, 0, 12, min_s=0)
    assert c["text"].rstrip().endswith("Wow."), c["text"]


def test_transcript_ending_at_end_of_video_is_safe():
    ws = words(["First sentence of the clip.", "Last sentence of the whole video ends right here."])
    c = B.refine_checked(ws, 3, len(ws) - 1)
    assert c["final_words"][1] == len(ws) - 1 and c["final_end"] >= ws[-1]["e"]
    assert not any(i in c["issues"] for i in ("ends_mid_sentence", "truncates_word"))


def test_dependent_opening_pulls_setup_and_quality_score_reflects_it():
    ws = words(["Everyone said the plan would fail.", "But I did it anyway.", "Three weeks later it worked brilliantly for us."])
    good = B.refine_checked(ws, 6, len(ws) - 1)
    assert good["text"].startswith("Everyone"), good["text"]
    bad = B._pack(ws, 6, 8, 6, 8)
    bad["issues"] = B.validate(ws, bad, min_s=0)
    assert "missing_setup" in bad["issues"] and B.quality(bad["issues"]) < good["boundary_quality"]


def test_sentence_continuing_across_a_scene_cut_is_not_split_by_the_cut():
    ws = words(["The thing that nobody expected was that the whole plan simply worked."])
    c = B.refine_checked(ws, 0, len(ws) - 1)
    cuts = R.shot_cuts(c["final_start"], c["final_end"], [3.0])
    assert c["text"].rstrip().endswith("worked.") and len(cuts) <= 1  # boundaries never depend on cuts; framing handles the cut separately


# ------------------------------------------------------------------ signals / chat -------------------------------------------------------
def test_chat_spike_detected_with_adaptive_baseline_and_no_false_alarm():
    random.seed(1)
    quiet = [15 + random.randint(-3, 3) for _ in range(120)]
    assert S.spikes(quiet) == []
    assert 80 in S.spikes(quiet[:80] + [120] + quiet[81:])


def test_spike_detector_memory_is_bounded():
    d = S.SpikeDetector(window=60)
    for i in range(100000):
        d.feed(i % 7)
    assert len(d.hist) == 60


def test_spam_giveaway_bots_and_single_user_floods_are_filtered_but_crowd_hype_is_kept():
    chat = [{"t": 5.1, "u": f"v{i}", "m": "KEKW"} for i in range(30)]                       # 30 different users: real reaction
    chat += [{"t": 5.2, "u": "bot", "m": "!giveaway enter now"}] * 10                          # command/giveaway spam
    chat += [{"t": 5.3, "u": "spam", "m": "BUY FOLLOWERS http://x.co"}] * 10                   # links
    chat += [{"t": 5.4 + i * 0.01, "u": "flooder", "m": "aaaa"} for i in range(40)]            # one user flooding
    s, info = S.chat_series(chat, 20, with_info=True)
    assert info["kept"] <= 32 and info["spam_dropped"] >= 55, info
    assert s[5] > 25


def test_heatmap_series_and_empty():
    assert S.heat_series(None, 10) == []
    h = S.heat_series([{"start_time": 2, "end_time": 4, "value": 0.9}], 10)
    assert h[3] == 0.9 and h[8] == 0.0


# --------------------------------------------------------------------- scoring ---------------------------------------------------------------
def test_event_only_candidates_without_any_words():
    n = 120
    audio = [10.0] * n
    for t in range(60, 64):
        audio[t] = 80.0
    visual = [0.1] * n
    for t in range(58, 66):
        visual[t] = 2.0
    cs = score.candidates([], n, {"audio": audio, "visual": visual}, "gaming", cuts=[58.0, 66.0])
    assert cs and all(c["kind"] == "event" and c["text"] == "" for c in cs)
    assert any(c["final_start"] <= 64 and c["final_end"] >= 60 for c in cs[:2]), [(c["final_start"], c["final_end"]) for c in cs[:3]]  # the 60-64 s burst is found


def test_auto_profile_and_live_candidates():
    assert score.auto_profile([], 100, {"visual": [2.0] * 100}) == "gaming"
    ws = words(["talk"] * 40)
    assert score.auto_profile(ws, 25, {}) in ("podcast", "irl")
    lc = score.live_candidates(ws, 100, [{"start": 0, "end": 10, "confidence": 1.0, "kinds": ["chat", "audio"]}])
    assert lc and lc[0]["live"] and lc[0]["score"] > 0.7


def test_duplicate_overlaps_are_merged_keeping_the_better_clip():
    a = {"final_start": 10, "final_end": 40, "score": .9}
    b = {"final_start": 12, "final_end": 41, "score": .5}
    c = {"final_start": 100, "final_end": 120, "score": .4}
    assert score.dedupe([a, b, c]) == [a, c]


# ------------------------------------------------------------------------ LLM -----------------------------------------------------------------
def cand_for(ws, a, b):
    return {"final_words": [a, b], "core_words": [a, b], "final_start": ws[a]["s"], "final_end": ws[b]["e"], "text": "", "score": .5, "peak": 5}


def test_llm_validation_rejects_indices_outside_the_candidate_window_and_bad_types():
    ranges = [(10, 40)]
    ok = llm.validate_response({"results": [{"id": 0, "score": 8, "start_word": 12, "end_word": 30, "title": "t"}]}, 1, ranges)
    assert ok and ok[0]["span"] == (12, 30)
    for bad in ({"start_word": 2, "end_word": 30}, {"start_word": 12, "end_word": 99}, {"start_word": 30, "end_word": 12}, {"start_word": "12", "end_word": 30}, {"start_word": True, "end_word": 30}):
        r = llm.validate_response({"results": [{"id": 0, "score": 8, **bad}]}, 1, ranges)
        assert r and "span" not in r[0], bad  # scored, but boundaries NOT taken from an invalid suggestion
    assert llm.validate_response({"results": [{"id": 5, "score": 8}, {"id": 0, "score": 99}, {"id": 0, "score": "x"}]}, 1, ranges) == []
    assert llm.validate_response("nope", 1, ranges) is None and llm.validate_response({"results": "x"}, 1, ranges) is None
    dup = llm.validate_response({"results": [{"id": 0, "score": 5}, {"id": 0, "score": 9}]}, 1, ranges)
    assert len(dup) == 1 and dup[0]["score"] == .5


def test_llm_never_adopts_an_unrelated_part_of_the_transcript(tmp_path):
    ws = words([f"Sentence number {i} goes on for a little while." for i in range(40)])
    c = cand_for(ws, 60, 90)
    before = dict(c)
    fake = lambda prompt, max_out=900: (json.dumps({"results": [{"id": 0, "score": 9, "start_word": 0, "end_word": 5, "title": "x"}]}), 500, 40)  # points at the video start
    llm.judge(ws, [c], fake, llm.Budget(), tmp_path)
    assert c["llm_score"] == .9 and c["final_words"] == before["final_words"] and c["final_start"] == before["final_start"]


def test_budget_is_a_hard_limit_even_when_one_request_would_cross_it(tmp_path):
    ws = words([f"Sentence number {i} goes on for a little while." for i in range(40)])
    calls = []
    fake = lambda prompt, max_out=900: (calls.append(1), (json.dumps({"results": []}), 100, 10))[1]
    b = llm.Budget(max_calls=5, max_tokens=500, max_usd=1.0)  # far too small for even one request (reservation = est input + 900 output)
    llm.judge(ws, [cand_for(ws, 0, 20)], fake, b, tmp_path)
    assert not calls and b.tin + b.tout == 0 and b.calls == 0
    b2 = llm.Budget(max_calls=5, max_tokens=60000, max_usd=0.00001)
    llm.judge(ws, [cand_for(ws, 0, 20)], fake, b2, tmp_path)
    assert not calls, "USD cap must block before the call, not after"


def test_budget_exact_accounting_cache_hits_and_persistence(tmp_path):
    ws = words([f"Sentence number {i} goes on for a little while." for i in range(40)])
    fake = lambda prompt, max_out=900: (json.dumps({"results": [{"id": 0, "score": 7, "title": "a"}]}), 321, 45)
    bf = tmp_path / "budget.json"
    b = llm.Budget.load(bf, 3, 60000, 0.10)
    llm.judge(ws, [cand_for(ws, 0, 20)], fake, b, tmp_path / "c")
    assert (b.calls, b.tin, b.tout) == (1, 321, 45)  # reservation replaced by EXACT usage from the response
    llm.judge(ws, [cand_for(ws, 0, 20)], fake, b, tmp_path / "c")
    assert b.calls == 1 and b.cached == 1             # same prompt: cache, no second paid call
    b2 = llm.Budget.load(bf, 3, 60000, 0.10)           # a later process (the final VOD pass) continues the SAME allowance
    assert (b2.calls, b2.tin, b2.tout) == (1, 321, 45)
    assert llm.Budget.usd_of(321, 45) == pytest.approx(321 / 1e6 * llm.PRICE[0] + 45 / 1e6 * llm.PRICE[1])


def test_transient_errors_retry_with_backoff_then_fall_back_locally(tmp_path):
    ws = words([f"Sentence number {i} goes on for a little while." for i in range(40)])
    sleeps, n = [], []

    def flaky(prompt, max_out=900):
        n.append(1)
        raise llm.Transient("HTTP 429")
    b = llm.Budget(max_calls=6)
    c = cand_for(ws, 0, 20)
    llm.judge(ws, [c], flaky, b, tmp_path, sleep=sleeps.append)
    assert len(n) == 3 and sleeps == [1, 2, 4] and "llm_score" not in c and b.fallbacks == 1
    assert b.tin + b.tout == 0, "failed transient attempts are not billed"


def test_malformed_json_is_retried_then_rejected(tmp_path):
    ws = words([f"Sentence number {i} goes on for a little while." for i in range(40)])
    n = []
    bad = lambda prompt, max_out=900: (n.append(1), ("not json {", 100, 5))[1]
    b = llm.Budget(max_calls=6)
    c = cand_for(ws, 0, 20)
    llm.judge(ws, [c], bad, b, tmp_path, sleep=lambda s: None)
    assert len(n) == 3 and "llm_score" not in c


def test_api_key_never_appears_in_errors_logs_or_reports(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-TESTKEY1234567890abcdef")
    assert "TESTKEY" not in util.redact("Authorization: Bearer sk-TESTKEY1234567890abcdef failed api_key=sk-TESTKEY1234567890abcdef")
    util.log_event(tmp_path / "x.log", "e", msg="token=abcdef123456 and sk-TESTKEY1234567890abcdef")
    assert "TESTKEY" not in (tmp_path / "x.log").read_text() and "abcdef123456" not in (tmp_path / "x.log").read_text()
    b = llm.Budget()
    assert "TESTKEY" not in json.dumps(b.report())


def test_provider_fallback_order(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY_FILE", raising=False)
    monkeypatch.setattr(llm, "ollama_ok", lambda timeout=1.0: False)
    assert llm.provider_for("smart") is None and llm.provider_for("disabled") is None and llm.provider_for("local_only") is None
    monkeypatch.setattr(llm, "ollama_ok", lambda timeout=1.0: True)
    assert llm.provider_for("smart") is llm.ollama_chat and llm.provider_for("local_only") is llm.ollama_chat
    monkeypatch.setenv("OPENAI_API_KEY", "sk-x")
    assert llm.provider_for("smart") is llm.openai_chat and llm.provider_for("disabled") is None


# ---------------------------------------------------------------- jobs: identity & invalidation -------------------------------------------------------
def make_job(tmp_path, **opts):
    from clipper import job as J
    src = tmp_path / "v.mp4"
    src.write_bytes(os.urandom(5000))
    j = J.create(str(src), tmp_path / "jobs", **opts)
    j.dir.mkdir(parents=True, exist_ok=True)
    util.jsave(j.p("source.json"), {"fingerprint": "abc"})
    for st in J.STAGES:  # pretend every stage already ran with its CURRENT inputs, in dependency order
        util.jsave(j.p(J.ART[st]), {"x": 1})
        j.mark(st, j.expected_hash(st))
    return j, src


def test_changing_caption_style_invalidates_only_render_and_qc(tmp_path):
    from clipper import job as J
    j, src = make_job(tmp_path)
    assert all(j.done(s) for s in J.STAGES)
    j2 = J.create(str(src), tmp_path / "jobs", style="karaoke")
    assert j2.id == j.id
    assert {s for s in J.STAGES if not j2.done(s)} == {"render", "qc"}


def test_changing_clip_count_invalidates_selection_and_everything_downstream_but_not_transcription(tmp_path):
    from clipper import job as J
    j, src = make_job(tmp_path)
    J.create(str(src), tmp_path / "jobs", clips=3)
    assert {s for s in J.STAGES if not j.done(s)} == {"select", "speakers", "render", "qc"}


def test_changing_whisper_model_invalidates_the_whole_chain_after_ingest(tmp_path):
    from clipper import job as J
    j, src = make_job(tmp_path)
    J.create(str(src), tmp_path / "jobs", model="quality")
    assert {s for s in J.STAGES if not j.done(s)} == set(J.STAGES) - {"ingest"}


def test_replacing_the_source_content_changes_job_identity_and_ingest_hash(tmp_path):
    from clipper import job as J
    j, src = make_job(tmp_path)
    old = j.expected_hash("ingest")
    src.write_bytes(os.urandom(7000))
    j2 = J.create(str(src), tmp_path / "jobs")
    assert j2.id != j.id, "different content must never share a job directory (or its renders)"
    assert j.expected_hash("ingest") != old


def test_editing_a_chat_file_invalidates_signals_and_downstream(tmp_path):
    from clipper import job as J
    chat = tmp_path / "chat.jsonl"
    chat.write_text('{"t":1,"u":"a","m":"x"}\n')
    j, src = make_job(tmp_path, chat=str(chat))
    assert j.done("signals")
    chat.write_text('{"t":1,"u":"a","m":"x"}\n{"t":2,"u":"b","m":"y"}\n')
    assert not j.done("signals") and j.done("transcribe")


def test_stage_invalidate_cascades_and_artifacts_are_atomic(tmp_path):
    from clipper import job as J
    j, _ = make_job(tmp_path)
    j.invalidate("signals")
    assert not any(j.p(J.ART[s]).exists() for s in ("signals", "candidates", "select", "speakers", "render", "qc")) and j.p("transcript.json").exists()
    util.jsave(j.p("a.json"), {"k": 1})
    assert not list(j.dir.glob("*.tmp")) and util.jload(j.p("a.json")) == {"k": 1}
    j.p("bad.json").write_text("{not json")
    assert util.jload(j.p("bad.json"), "fallback") == "fallback"  # a corrupt artifact is treated as missing, never crashes the job


def test_job_lock_blocks_a_second_writer_and_recovers_from_a_stale_lock(tmp_path):
    from clipper import job as J
    j, _ = make_job(tmp_path)
    assert j.lock()
    j2 = J.Job(tmp_path / "jobs", j.id)
    j2.p("job.lock").write_text(str(os.getppid()))  # another live process owns it
    j.unlock()
    j2.p("job.lock").write_text(str(os.getppid()))
    assert not j2.lock()
    j2.p("job.lock").write_text("99999999")  # dead pid
    assert j2.lock()
    j2.unlock()


# --------------------------------------------------------------------- model manager --------------------------------------------------------------
@pytest.fixture()
def fake_model(tmp_path, monkeypatch):
    import http.server, threading, hashlib
    data = os.urandom(300_000)

    class H(http.server.BaseHTTPRequestHandler):
        fail_first = [True]

        def do_GET(self):
            rng = self.headers.get("Range")
            start = int(rng.split("=")[1].split("-")[0]) if rng else 0
            body = data[start:]
            if H.fail_first[0] and not rng:  # first attempt dies half-way: the manager must RESUME with a Range request
                H.fail_first[0] = False
                self.send_response(200); self.send_header("Content-Length", str(len(data))); self.end_headers()
                self.wfile.write(data[:100_000]); self.wfile.flush(); self.connection.close(); return
            self.send_response(206 if rng else 200); self.send_header("Content-Length", str(len(body))); self.end_headers(); self.wfile.write(body)

        def log_message(self, *a):
            pass
    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setattr(models, "CACHE", tmp_path / "cache")
    entry = {"url": f"http://127.0.0.1:{srv.server_port}/m.bin", "sha256": hashlib.sha256(data).hexdigest(), "size": len(data), "license": "test", "dest": "m.bin", "needed_for": "test"}
    monkeypatch.setitem(models.MANIFEST, "fake", entry)
    yield entry, data
    srv.shutdown()


def test_model_download_resumes_verifies_and_replaces_atomically(fake_model, monkeypatch):
    monkeypatch.setattr(models.time, "sleep", lambda s: None)
    p = models.ensure("fake", log=lambda *_: None)
    assert p.read_bytes() == fake_model[1] and not p.with_name(p.name + ".part").exists()


def test_corrupted_model_is_discarded_and_offline_mode_refuses_to_download(fake_model, monkeypatch):
    monkeypatch.setattr(models.time, "sleep", lambda s: None)
    p = models.ensure("fake", log=lambda *_: None)
    p.write_bytes(b"corrupt")
    monkeypatch.setenv("CLIPPER_OFFLINE", "1")
    with pytest.raises(models.ModelUnavailable, match="offline|OFFLINE"):
        models.ensure("fake", log=lambda *_: None)
    assert not p.exists(), "a corrupted file must be removed, never used"


def test_wrong_checksum_from_upstream_is_rejected(fake_model, monkeypatch):
    monkeypatch.setattr(models.time, "sleep", lambda s: None)
    monkeypatch.setitem(models.MANIFEST, "fake", {**fake_model[0], "sha256": "0" * 64})
    with pytest.raises(models.ModelUnavailable, match="SHA-256"):
        models.ensure("fake", log=lambda *_: None)
    assert not list((models.CACHE).glob("*.part")) and not (models.CACHE / "m.bin").exists()


def test_manifest_is_complete_and_try_ensure_degrades_with_a_recorded_reason(monkeypatch, tmp_path):
    for n, e in models.MANIFEST.items():
        if not n.startswith("_"):
            assert len(e["sha256"]) == 64 and e["size"] > 0 and e["url"].startswith("http") and e["license"], n
    monkeypatch.setattr(models, "CACHE", tmp_path / "empty")
    monkeypatch.setenv("CLIPPER_OFFLINE", "1")
    assert models.try_ensure("yunet.onnx", "test feature") is None and "test feature" in util.skipped()


# ------------------------------------------------------------------- captions / render maths -----------------------------------------------------
PRESET_NAMES = ["bold_pop", "karaoke", "clean_lower", "boxed", "minimal_white", "highlight_words", "podcast_speaker", "gaming_bold", "cinematic"]


@pytest.mark.parametrize("name", PRESET_NAMES)
def test_every_caption_preset_builds_valid_timed_ass_without_overflow(name):
    ws = words(["This is the moment nobody saw coming at all.", "Wow that was crazy, honestly I did not expect it."])
    ass = captions.build(ws, 0.0, name, speaker_at=lambda t: 0 if t < 3 else 1)
    ev = [l for l in ass.splitlines() if l.startswith("Dialogue")]
    assert ev and "Fontname" in ass
    times = [l.split(",")[1:3] for l in ev]
    assert all(a <= b for a, b in times)
    assert captions.overflow(ws, captions.style(name)) == 0


def test_custom_style_from_file_rejects_unknown_keys_and_flags_system_fonts(tmp_path):
    f = tmp_path / "s.json"
    f.write_text(json.dumps({"font": "Anton", "size": 70, "hi": captions.GREEN, "words": 2, "mv": 500, "anim": "pop"}))
    st = captions.style("custom", f)
    assert st["font"] == "Anton" and st["words"] == 2 and not st["font_is_system"]
    f.write_text(json.dumps({"fontt": "x"}))
    with pytest.raises(ValueError):
        captions.style("custom", f)
    assert captions.style({"font": "Comic Sans MS"})["font_is_system"]


def test_bundled_fonts_and_licences_exist():
    names = {p.name for p in captions.FONTS.glob("*")}
    assert {"Poppins-ExtraBold.ttf", "Anton-Regular.ttf", "BebasNeue-Regular.ttf"} <= names and any(n.startswith("OFL") for n in names)


def test_caption_groups_never_overflow_and_a_single_oversize_word_is_isolated():
    ws = words(["Supercalifragilisticexpialidocious pneumonoultramicroscopicsilicovolcanoconiosis antidisestablishmentarianism extraordinarily."])
    for n in PRESET_NAMES:
        st = captions.style(n)
        ch = captions.chunks(ws, st, 1.0)
        assert all(len(g) == 1 for g in ch if sum(len(w["w"]) + 1 for w in g) * st["size"] * 0.58 > captions.W - 100), n  # an oversize word sits ALONE; it never drags neighbours off-screen
    ok = words(["This is a perfectly normal caption line for any style."])
    assert all(captions.overflow(ok, captions.style(n), 1.0) == 0 for n in PRESET_NAMES)


def test_crop_expression_changes_only_at_cuts_and_zoom_never_crosses_a_cut():
    assert R.crop_expr([[[0, .2]], [[10, .7]]], [10]) == "if(lt(t,10),0.200,0.700)"
    assert "if(" not in R.crop_expr([[[0, .48]], [[10, .5]], [[20, .52]]], [10, 20])
    assert "(t-" in R.crop_expr([[[0, .3], [4, .3], [5, .7]]], [])
    assert R.shot_cuts(100, 130, [100.5, 110, 110.8, 129.5, 120]) == [10.0, 20.0]
    assert "0.120*max(0" in R.zoom_expr(1, [{"t": 3, "dur": 2, "amt": 1.12}])


def test_filtergraph_uses_bundled_fonts_dir_and_overlays_in_order():
    info = {"width": 1280, "height": 720, "fps": 25, "audio": True}
    vf = R.vfilter(info, "fill", "0.5", "c.ass", None, logo={"file": "l.png", "pos": "tr"}, fade=10.0)
    assert "subtitles=c.ass:fontsdir=fonts" in vf and vf.index("overlay") < vf.index("subtitles") and "fade=t=in" in vf


def test_encoder_selection_uses_a_real_test_encode_and_falls_back(monkeypatch):
    seen = []
    monkeypatch.setattr(R, "_ENC", [])
    monkeypatch.setattr(R, "run", lambda cmd, **kw: type("P", (), {"stdout": " V..... h264_nvenc\n V..... h264_qsv\n V..... libx264", "stderr": "", "returncode": 0})())
    monkeypatch.setattr(R, "_test_encode", lambda e: (seen.append(e), e == "h264_qsv")[1])
    assert R.encoder() == "h264_qsv" and seen == ["h264_nvenc", "h264_qsv"]  # nvenc is LISTED but fails its test encode -> next candidate
    monkeypatch.setattr(R, "_ENC", [])
    monkeypatch.setattr(R, "_test_encode", lambda e: False)
    assert R.encoder() == "libx264"
    assert set(R.ENC_ARGS) == {"libx264", "h264_nvenc", "h264_qsv", "h264_amf", "h264_videotoolbox"}


# ---------------------------------------------------------------------- ingest / validation ----------------------------------------------------------
@pytest.mark.parametrize("bad", ["", "-rf", "--exec=touch x", "file:///etc/passwd", "ftp://x/y", "javascript:alert(1)", "data:text/html,x", "http://user:pw@host/x", "http://", "a\nb", "x" * 5000])
def test_hostile_sources_are_rejected(bad):
    with pytest.raises(ValueError):
        util.validate_source(bad)


@pytest.mark.parametrize("ok", ["https://www.youtube.com/watch?v=abc", "https://twitch.tv/videos/1", "rtmp://127.0.0.1:1935/live/k", "https://x.com/a.m3u8", "video.mp4", "C:\\Videos\\a.mp4", "/home/u/a.mkv"])
def test_legit_sources_pass(ok):
    assert util.validate_source(ok) == ok


def test_path_and_archive_traversal_blocked(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    util.safe_path(tmp_path / "a" / "b")
    with pytest.raises(ValueError):
        util.safe_path("../../etc/passwd")
    evil = tmp_path / "evil.tar"
    with tarfile.open(evil, "w") as tf:
        (tmp_path / "x.txt").write_text("x")
        ti = tf.gettarinfo(str(tmp_path / "x.txt"), arcname="../escape.txt")
        tf.addfile(ti, open(tmp_path / "x.txt", "rb"))
    with tarfile.open(evil) as tf, pytest.raises(ValueError, match="unsafe"):
        util.safe_extract_tar(tf, tmp_path / "out")
    assert not (tmp_path / "escape.txt").exists()


def test_subprocess_helper_refuses_strings_and_survives_timeouts():
    with pytest.raises(AssertionError):
        util.run("echo hi; rm -rf /")
    r = util.run(["sleep", "5"], timeout=0.3) if os.name != "nt" else util.run(["ping", "-n", "6", "127.0.0.1"], timeout=0.3)
    assert r.returncode == 124


def test_chat_loaders_and_fingerprint(tmp_path):
    from clipper import ingest
    j = tmp_path / "t.json"
    j.write_text(json.dumps({"comments": [{"content_offset_seconds": 3.0, "commenter": {"name": "a"}, "message": {"body": "hi"}}]}))
    assert ingest.load_chat(j)[0]["t"] == 3.0
    y = tmp_path / "y.jsonl"
    y.write_text(json.dumps({"replayChatItemAction": {"videoOffsetTimeMsec": "4000", "actions": [{"addChatItemAction": {}}, {"addChatItemAction": {}}]}}) + "\n")
    assert len(ingest.load_chat(y)) == 2
    a = tmp_path / "a.bin"
    a.write_bytes(b"x" * 3_000_000)
    f1 = util.file_fingerprint(a)
    a.write_bytes(b"x" * 1_500_000 + b"y" + b"x" * 1_499_999)
    assert util.file_fingerprint(a) != f1


# ------------------------------------------------------------------------- kick ---------------------------------------------------------------------
def test_kick_signature_replay_tamper_and_idempotency():
    crypto = pytest.importorskip("cryptography")
    from cryptography.hazmat.primitives.asymmetric import rsa, padding
    from cryptography.hazmat.primitives import hashes, serialization
    k = rsa.generate_private_key(65537, 2048)
    pub = kick.parse_public_key(k.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
    body = json.dumps({"sender": {"username": "v1"}, "content": "KEKW"}).encode()
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    sig = base64.b64encode(k.sign(f"m1.{ts}.".encode() + body, padding.PKCS1v15(), hashes.SHA256())).decode()
    H = {"Kick-Event-Message-Id": "m1", "Kick-Event-Message-Timestamp": ts, "Kick-Event-Signature": sig, "Kick-Event-Type": "chat.message.sent"}
    got = []
    assert kick.handle(H, body, pub, lambda *a: got.append(a))[0] == 200 and len(got) == 1
    assert kick.handle(H, body, pub, lambda *a: got.append(a)) == (200, "duplicate") and len(got) == 1  # redelivery is processed once
    kick._SEEN.clear()
    assert kick.handle(H, body + b"x", pub, lambda *a: None)[0] == 401
    assert kick.handle(H, body, pub, lambda *a: None, now=time.time() + 3600)[0] == 401
    assert kick.handle({}, body, pub, lambda *a: None)[0] == 401


# ----------------------------------------------------------------------- critics ---------------------------------------------------------------
def test_weights_sum_and_critic_criteria_cover_murch_six():
    assert abs(sum(v for k, v in critics.WEIGHT.items() if k != "craft") - 1.0) < 1e-9
    assert {"emotion", "story", "rhythm", "eye_trace", "plane", "space"} <= set(critics.WEIGHT)


def test_audio_rms_gives_one_value_per_second_aligned_to_the_signal(tmp_path):
    """Regression: asetnsamples counted samples at the INPUT rate, so a 'second' was 1/6 s and every audio peak was placed at the wrong time."""
    import subprocess
    try:
        ff = util.ffmpeg()
    except SystemExit:
        pytest.skip("no ffmpeg")
    w = tmp_path / "step.wav"
    subprocess.run([ff, "-y", "-v", "error", "-f", "lavfi", "-i", "anullsrc=r=48000:cl=mono:d=4", "-f", "lavfi", "-i", "sine=f=440:d=4:r=48000", "-f", "lavfi", "-i", "sine=f=440:d=4:r=48000",
                    "-filter_complex", "[1:a]volume=0.05[q];[0:a][q][2:a]concat=n=3:v=0:a=1[a]", "-map", "[a]", str(w)], check=True)
    r = S.audio_rms(str(w), 12)
    assert len(r) == 13 and max(r[:3]) == -90 and -50 < r[5] < -40 and r[9] > -25, r  # silence, quiet tone, loud tone at 4 s each


def test_critic_trims_back_when_the_source_itself_ends_mid_sentence(tmp_path):
    """A clip cannot be 'extended' to a sentence end that does not exist in the media: trim to the last finished sentence instead."""
    from clipper import job as J
    ws = words(["This is the first complete sentence of the clip.", "And here is a second one that is also finished properly.", "But the footage stops while he is saying that the"])
    j = J.Job(tmp_path / "jobs", "0123456789")
    plan = {"clips": [{"id": 1, "final_start": 0.0, "final_end": ws[-1]["e"] + 0.1, "final_words": [0, len(ws) - 1], "final_score": .5, "caption": {"style": None, "scale": 1.0},
                       "layout": "fill", "zooms": [], "text": "", "hook": .5, "score": .5}], "cost": {}}
    util.jsave(j.p("plan.json"), plan), util.jsave(j.p("transcript.json"), {"words": ws}), util.jsave(j.p("job.json"), {"profile": "auto"})
    util.jsave(j.p("source.json"), {"info": {"duration": 60, "width": 1280, "height": 720, "audio": True, "fps": 25}})
    x = critics.Ctx(j)
    r = critics.Rep("cut")
    critics.cut(x, r)
    f = next(f for f in r.clips[1] if f["code"] == "cut_mid_sentence")
    assert f["edit"] and f["edit"]["end_shift"] < 0, f  # trims BACK; never extends past the end of the media
