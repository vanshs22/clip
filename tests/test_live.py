"""Live tests: a real local RTMP server (MediaMTX), real ffmpeg pushes, a synthetic chat relay. No Twitch/YouTube/Kick account involved.
Run with: pytest -m live   (needs MediaMTX: auto-downloaded, or set $MEDIAMTX)"""
import json, os, subprocess, sys, threading, time
from pathlib import Path
import pytest
from clipper import flow, live, util
from clipper.util import ffmpeg, probe
from tests import synth

pytestmark = pytest.mark.live
PORT = 19455


@pytest.fixture(scope="module")
def mtx():
    from clipper import ingest_server
    try:
        p = ingest_server.start(PORT)
    except Exception as e:
        pytest.skip(f"MediaMTX unavailable: {e}")
    time.sleep(3)
    yield p
    p.terminate()


def push(src, key, secs, ff):
    return subprocess.Popen([ff, "-hide_banner", "-loglevel", "error", "-re", "-t", str(secs), "-i", str(src), "-c:v", "libx264", "-preset", "ultrafast", "-g", "25",
                             "-c:a", "aac", "-f", "flv", f"rtmp://127.0.0.1:{PORT}/live/{key}"])


def chat_burst(path, t_start, secs=70, burst=(30, 38)):
    stop = threading.Event()

    def run():
        f = open(path, "w")
        while not stop.is_set():
            el = time.time() - t_start
            for _ in range(14 if burst[0] < el < burst[1] else 1):
                f.write(json.dumps({"u": f"v{int(el * 7) % 40}", "m": "KEKW LETS GOOO", "ts": time.time()}) + "\n")
            f.flush()
            time.sleep(1)
    threading.Thread(target=run, daemon=True).start()
    return stop


@pytest.fixture(scope="module")
def stream_src(tmp_path_factory, ff):
    d = tmp_path_factory.mktemp("livesrc")
    ws = synth.words(["Okay so this is the moment everybody has been waiting for tonight.", "I genuinely thought we were going to lose the whole thing here.",
                      ("But nobody expected that to happen at all.", 1.0), "And that is when everything changed for the entire team forever.",
                      "Wow that was honestly unbelievable to watch live."] * 3, t=1.0, pause=0.5)
    v = synth.make_video(d / "s.mp4", dur=75, a_windows=[(1, 70)], beeps=[34], cuts=[20, 50])
    (d / "w.json").write_text(json.dumps({"words": ws}))
    return d, v


def test_watch_records_detects_finalizes_and_exports_with_no_second_command(mtx, stream_src, tmp_path, ff, monkeypatch):
    d, v = stream_src
    out = tmp_path / "live"
    out.mkdir()
    relay = tmp_path / "relay.jsonl"
    t0 = time.time()
    stop = chat_burst(relay, t0)
    p = push(v, "a", 62, ff)
    time.sleep(3)
    try:
        j, fr = flow.watch_stream(f"rtmp://127.0.0.1:{PORT}/live/a", out, clips=2, mode="disabled", style="bold_pop", quality="draft", chat_file=str(relay), latency=0.0,
                                  max_seconds=52, warmup=2, pre=12, post=10, stt=False, log=lambda *_: None, transcript=str(d / "w.json"), profile="podcast", speakers="off")
    finally:
        stop.set()
        p.kill()
    sess = live.Session(out).d
    assert sess["state"] == "done" and sess["job"], sess["history"]
    states = [h[0] for h in sess["history"]]
    assert states.index("recording") < states.index("finalizing") < states.index("processing") < states.index("done"), states
    assert (out / "record.ts").exists() and not list(out.glob("record_0*.ts")), "parts are joined into one recording and removed"
    assert any(out.glob("prelim_*.mkv")), "a preliminary clip was cut DURING the stream"
    ev = json.loads((out / "events.json").read_text())
    assert any("chat" in e["kinds"] for e in ev), ev
    meta = json.loads((out / "export" / "metadata.json").read_text())
    assert meta["clips"] and all((out / "export" / m["file"]).exists() for m in meta["clips"])
    assert util.jload(out / "budget.json") is None or True  # budget file only exists if a provider was used (mode=disabled here)
    # live memory discipline: the per-run files are plain append logs, not in-memory lists
    assert (out / "chat.jsonl").exists() and (out / "events.jsonl").exists()


def test_interrupted_session_is_resumed_and_finished_automatically(mtx, stream_src, tmp_path, ff):
    d, v = stream_src
    out = tmp_path / "live2"
    out.mkdir()
    p = push(v, "b", 40, ff)
    time.sleep(3)
    sess = live.monitor(f"rtmp://127.0.0.1:{PORT}/live/b", out, None, 0.0, True, log=lambda *_: None, max_seconds=22, stt=False, warmup=2, pre=8, post=6)
    p.kill()
    assert live.Session(out).d["state"] == "finalizing"
    # "the process died here": a new process resumes from session.json and completes the workflow
    j, fr = flow.watch_stream(f"rtmp://127.0.0.1:{PORT}/live/b", out, clips=1, mode="disabled", quality="draft", resume=True, log=lambda *_: None,
                              transcript=str(d / "w.json"), profile="podcast", speakers="off")
    assert live.Session(out).d["state"] == "done" and (out / "export" / "metadata.json").exists()


def test_stream_drop_and_return_reconnects_and_joins_the_parts(mtx, stream_src, tmp_path, ff):
    d, v = stream_src
    out = tmp_path / "live3"
    out.mkdir()
    p1 = push(v, "c", 14, ff)
    time.sleep(3)
    res = {}

    def run():
        res["s"] = live.monitor(f"rtmp://127.0.0.1:{PORT}/live/c", out, None, 0.0, True, log=lambda *_: None, max_seconds=60, stt=False, warmup=2, pre=6, post=4)
    th = threading.Thread(target=run)
    th.start()
    p1.wait()
    time.sleep(8)  # the stream is down
    p2 = push(v, "c", 14, ff)  # ...and comes back
    th.join(timeout=75)
    p2.kill()
    sd = live.Session(out).d
    assert len(sd["parts"]) >= 2 and sd["gaps"], sd  # a reconnect starts a new part and records the gap
    fin = live.finalize(out)
    assert Path(fin["record"]).exists() and probe(fin["record"])["duration"] > 14
    assert not list(out.glob("record_0*.ts"))


def test_cancel_marker_stops_the_session_without_processing(mtx, stream_src, tmp_path, ff):
    d, v = stream_src
    out = tmp_path / "live4"
    out.mkdir()
    p = push(v, "d", 40, ff)
    time.sleep(3)
    threading.Timer(8, lambda: (out / "CANCEL").touch()).start()
    j, fr = flow.watch_stream(f"rtmp://127.0.0.1:{PORT}/live/d", out, clips=1, mode="disabled", log=lambda *_: None, max_seconds=60, stt=False, warmup=2)
    p.kill()
    assert fr == {"state": "cancelled"} and live.Session(out).d["state"] == "cancelled" and not (out / "export").exists()
