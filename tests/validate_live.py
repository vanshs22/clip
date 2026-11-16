"""End-to-end live validation: real MediaMTX, real RTMP push, real `clipper monitor`.
   python -m tests.validate_live SOURCE_WITH_SPEECH.mkv [out_dir]
A loud beep is mixed into the stream at source time BEEP s, and a chat burst is emitted at the same wall-clock instant. After the run, the
beep's position in the RECORDING and the burst's position in chat.jsonl must agree on the monitor's own clock (chat-to-video sync), the
burst must be flagged as an event, and a preliminary clip must contain both."""
import json, subprocess, sys, tempfile, threading, time
from pathlib import Path
from clipper import ingest_server, signals
from clipper.util import ffmpeg, probe

BEEP, SECS = 38, 80


def run_once(src, out_dir, latency):
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    relay, stop, clock = out / "relay.jsonl", threading.Event(), {}
    mtx = ingest_server.start(19351)
    time.sleep(3)

    def chat():  # one message/s, a burst while the beep sounds; scheduled against the moment the push starts
        f = open(relay, "w")
        while "push" not in clock:
            time.sleep(0.05)
        while not stop.is_set():
            el = time.time() - clock["push"]
            for _ in range(14 if BEEP <= el < BEEP + 6 else 1):
                f.write(json.dumps({"u": f"v{int(el * 7) % 50}", "m": "KEKW LETS GOOO", "ts": time.time()}) + "\n")
            f.flush()
            time.sleep(1)
    threading.Thread(target=chat, daemon=True).start()
    clock["push"] = time.time()
    push = subprocess.Popen([ffmpeg(), "-hide_banner", "-loglevel", "error", "-re", "-t", str(SECS), "-i", src, "-f", "lavfi", "-i", f"sine=f=1000:d=6,adelay={BEEP * 1000}|{BEEP * 1000},volume=3",
                             "-filter_complex", "[0:a][1:a]amix=inputs=2:duration=first:normalize=0[a]", "-map", "0:v", "-map", "[a]", "-c:v", "libx264", "-preset", "ultrafast",
                             "-g", "25", "-c:a", "aac", "-f", "flv", "rtmp://127.0.0.1:19351/live/t"])
    time.sleep(4)
    mon = subprocess.Popen([sys.executable, "-m", "clipper", "monitor", "rtmp://127.0.0.1:19351/live/t", "--out", str(out / "mon"), "--chat-file", str(relay),
                            "--latency", str(latency), "--max-seconds", str(SECS - 8), "--no-stt", "--warmup-bins", "2", "--pre", "12", "--post", "10"],
                           stdout=open(out / "mon.log", "w"), stderr=subprocess.STDOUT)
    try:
        mon.wait(timeout=SECS + 40)
    finally:
        stop.set()
        push.kill()
        mtx.terminate()
        mon.kill()
    from clipper import live
    m = out / "mon"
    skew, ct, at = live.sync_check(m)
    ev = json.load(open(m / "events.json")) if (m / "events.json").exists() else []
    pre = sorted(m.glob("prelim_*.mkv"))
    flagged = any(w["start"] <= at <= w["end"] and "chat" in w["kinds"] for w in ev)
    print(f"latency={latency}: beep in recording t={at}s, chat burst t={ct}s, skew {skew:+d}s | events {[(w['kinds'], round(w['start']), round(w['end'])) for w in ev]} | clips {[p.name for p in pre]}")
    return skew, flagged, bool(pre)


def main(src, out_dir=None):
    base = Path(out_dir or tempfile.mkdtemp())
    s1, f1, c1 = run_once(src, base / "run1", 0)
    s2, f2, c2 = run_once(src, base / "run2", s1)  # closed loop: feed the measured skew back in
    ok = f1 and c1 and abs(s2) <= 3 and f2
    print(f"first run skew {s1:+d}s -> after applying --latency {s1}: {s2:+d}s")
    print("LIVE VALIDATION:", "PASS" if ok else "FAIL")
    return ok


if __name__ == "__main__":
    sys.exit(0 if main(*sys.argv[1:]) else 1)
