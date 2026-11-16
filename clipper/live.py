"""Live monitor: rolling segment buffer + full recording, chat/audio spike detection, preliminary clips.
Final selection happens AFTER the stream: `clipper process record.ts --chat chat.jsonl` reuses everything here.
v1 live signals: chat (Twitch IRC, or tail --chat-file) + audio level. Live speech/vision run post-stream."""
import json, os, re, shutil, socket, ssl, threading, time
from pathlib import Path
from .signals import SpikeDetector
from .util import ffmpeg, run, hms
import subprocess

SEG = 4          # seconds per buffer segment
BIN = 10         # chat bin (s)
PRE, POST, KEEP = 120, 60, 300   # pre-roll, post-roll, buffer retention (s); KEEP must exceed PRE+POST+BIN


def merge_events(events, pre=PRE, post=POST):
    """Overlapping event windows -> one window. events: [{"pts":..,"kind":..}]."""
    out = []
    for e in sorted(events, key=lambda e: e["pts"]):
        a, b = max(0.0, e["pts"] - pre), e["pts"] + post
        if out and a <= out[-1]["end"]:
            out[-1].update(end=max(out[-1]["end"], b), kinds=sorted(set(out[-1]["kinds"]) | {e["kind"]}), peak=max(out[-1]["peak"], e.get("x", 0)))
        else:
            out.append({"start": a, "end": b, "kinds": [e["kind"]], "peak": e.get("x", 0)})
    for w in out:
        w["confidence"] = round(min(1.0, 0.5 * len(w["kinds"]) + 0.1), 2)  # chat+audio agreeing beats either alone
    return out


def twitch_chat(channel, sink, stop):
    """Anonymous read-only IRC. sink(wall_time, user, text)."""
    s = ssl.create_default_context().wrap_socket(socket.create_connection(("irc.chat.twitch.tv", 6697)), server_hostname="irc.chat.twitch.tv")
    s.sendall(f"NICK justinfan{int(time.time()) % 99999}\r\nJOIN #{channel.lower()}\r\n".encode())
    buf = ""
    while not stop.is_set():
        try:
            s.settimeout(2)
            buf += s.recv(4096).decode(errors="ignore")
        except socket.timeout:
            continue
        except OSError:
            return
        *lines, buf = buf.split("\r\n")
        for ln in lines:
            if ln.startswith("PING"):
                s.sendall(b"PONG :tmi.twitch.tv\r\n")
            elif m := re.match(r":(\w+)!.* PRIVMSG #\w+ :(.*)", ln):
                sink(time.time(), m[1], m[2])


def stream_url(src):
    if os.path.exists(src) or src.startswith(("rtmp", "http")) and ".m3u8" in src:
        return src
    if shutil.which("streamlink"):
        r = run(["streamlink", "--stream-url", src, "best"])
        if r.returncode == 0:
            return r.stdout.strip()
    r = run(["yt-dlp", "-g", "-f", "best", src])
    if r.returncode:
        raise SystemExit(f"cannot resolve a live stream URL: {r.stderr[-300:]}")
    return r.stdout.strip().splitlines()[0]


def extract(buf_dir, seg_names, start_idx, end_idx, a, b, out):
    """Concatenate buffer segments [start_idx, end_idx] and cut [a, b] (stream seconds). Stream copy: keyframe-accurate only."""
    names = [n for n in seg_names if start_idx <= int(n[4:10]) <= end_idx]
    if not names:
        return None
    first = int(names[0][4:10])  # oldest segment may already be pruned
    lst = Path(buf_dir) / f"list_{start_idx}.txt"
    lst.write_text("".join(f"file '{n}'\n" for n in names))
    r = run([ffmpeg(), "-y", "-hide_banner", "-f", "concat", "-safe", "0", "-i", lst, "-ss", f"{max(0.0, a - first * SEG):.2f}", "-t", f"{b - a:.2f}", "-c", "copy", out], cwd=buf_dir)
    lst.unlink(missing_ok=True)
    return out if r.returncode == 0 else None


def monitor(src, out_dir, channel=None, chat_file=None, latency=8.0, record=True, log=print, max_seconds=None):
    out = Path(out_dir)
    buf = out / "buffer"
    shutil.rmtree(buf, ignore_errors=True)
    buf.mkdir(parents=True, exist_ok=True)
    cmd = [ffmpeg(), "-hide_banner", "-loglevel", "error", *(["-re"] if os.path.exists(src) else []), "-i", stream_url(src),
           "-map", "0", "-c", "copy", "-f", "segment", "-segment_time", SEG, "-segment_format", "mpegts", str(buf / "seg_%06d.ts")]
    if record:
        cmd += ["-map", "0", "-c", "copy", "-f", "mpegts", str(out / "record.ts")]
    proc = subprocess.Popen([str(c) for c in cmd])
    t0, stop, chat_t, events, done_extract = time.time(), threading.Event(), [], [], set()
    chat_f = open(out / "chat.jsonl", "a")

    def sink(wall, user, text):  # wall clock -> stream PTS. ponytail: assumes ffmpeg start ~ stream t=0; `latency` calibrates chat-vs-video delay
        t = wall - t0 - latency
        chat_t.append(t)
        chat_f.write(json.dumps({"t": round(t, 2), "u": user, "m": text}) + "\n")
        chat_f.flush()
    if channel:
        threading.Thread(target=twitch_chat, args=(channel, sink, stop), daemon=True).start()
    chat_det, aud_det, seen, bin_i = SpikeDetector(60, 6.0, 8), SpikeDetector(60, 4.0, 1), set(), 0
    try:
        while proc.poll() is None and (max_seconds is None or time.time() - t0 < max_seconds):
            time.sleep(2)
            segs = sorted(p.name for p in buf.glob("seg_*.ts"))
            for n in segs[:-1]:  # the last file is still being written
                if n in seen:
                    continue
                seen.add(n)
                i = int(n[4:10])
                r = run([ffmpeg(), "-hide_banner", "-i", buf / n, "-vn", "-af", "astats=metadata=1,ametadata=print:key=lavfi.astats.Overall.RMS_level:file=-", "-f", "null", "-"])
                lv = [float(x) for x in re.findall(r"RMS_level=(-?[\d.]+)", r.stdout)]
                if lv and aud_det.feed(max(lv[-1], -90) + 90):
                    events.append({"pts": i * SEG, "kind": "audio", "x": lv[-1]})
            now = (len(seen)) * SEG
            while (bin_i + 1) * BIN <= now:  # complete chat bins
                c = sum(1 for t in chat_t if bin_i * BIN <= t < (bin_i + 1) * BIN)
                if chat_det.feed(c):
                    events.append({"pts": bin_i * BIN, "kind": "chat", "x": c})
                bin_i += 1
            for w in merge_events(events):
                key = round(w["start"])
                if w["end"] <= now and key not in done_extract:
                    done_extract.add(key)
                    f = extract(buf, segs, int(w["start"] // SEG), int(w["end"] // SEG), w["start"], w["end"], out / f"prelim_{hms(w['start']).replace(':', '-')}.mkv")
                    log(f"event {hms(w['start'])}-{hms(w['end'])} {w['kinds']} conf={w['confidence']} -> {f}")
            for n in segs:  # rolling retention
                if int(n[4:10]) * SEG < now - KEEP:
                    (buf / n).unlink(missing_ok=True)
            (out / "events.json").write_text(json.dumps(merge_events(events), indent=1))
    finally:
        stop.set()
        proc.terminate()
        chat_f.close()
    log(f"stopped. Next: clipper process {out / 'record.ts'} --chat {out / 'chat.jsonl'}")
