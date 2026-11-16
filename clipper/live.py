"""Live monitor: rolling segment buffer + full recording, chat/audio spike detection, preliminary clips.
Final selection happens AFTER the stream: `clipper process record.ts --chat chat.jsonl` reuses everything here.
v1 live signals: chat (Twitch IRC, or tail --chat-file) + audio level. Live speech/vision run post-stream."""
import datetime, importlib.util, json, os, re, shutil, socket, ssl, threading, time, urllib.parse, urllib.request
from pathlib import Path
from .signals import SpikeDetector, word_score
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


def parse_chat_line(line):
    """One line of a relay file -> (wall_epoch, user, text) or None. {"u","m"} plus optional {"ts": epoch}. Any tool can write this
    (e.g. your own Kick webhook receiver)."""
    try:
        j = json.loads(line)
        return float(j.get("ts") or time.time()), j.get("u"), str(j.get("m", ""))
    except (ValueError, AttributeError):
        return None


def tail_chat(path, sink, stop):
    Path(path).touch()
    with open(path) as f:
        f.seek(0, 2)
        while not stop.is_set():
            ln = f.readline()
            if not ln:
                time.sleep(0.5)
            elif m := parse_chat_line(ln):
                sink(*m)


def _get_json(url):
    with urllib.request.urlopen(url, timeout=20) as r:
        return json.load(r)


def youtube_chat(src, sink, stop, key, fetch=_get_json):
    """Official YouTube Data API v3 liveChatMessages.list with YOUR API key (public chats need no OAuth). Respects pollingIntervalMillis;
    quota applies. `fetch` is injectable for tests."""
    m = re.search(r"(?:v=|youtu\.be/|/live/)([\w-]{11})", src)
    if not m:
        raise SystemExit("cannot find a YouTube video id in the URL")
    q = urllib.parse.urlencode
    d = fetch("https://www.googleapis.com/youtube/v3/videos?" + q({"part": "liveStreamingDetails", "id": m[1], "key": key}))
    try:
        chat_id = d["items"][0]["liveStreamingDetails"]["activeLiveChatId"]
    except (KeyError, IndexError):
        raise SystemExit("this video has no active live chat")
    page = None
    while not stop.is_set():
        r = fetch("https://www.googleapis.com/youtube/v3/liveChat/messages?" + q({"liveChatId": chat_id, "part": "snippet,authorDetails", "key": key, **({"pageToken": page} if page else {})}))
        for it in r.get("items", []):
            ts = datetime.datetime.fromisoformat(it["snippet"]["publishedAt"].replace("Z", "+00:00")).timestamp()
            sink(ts, it["authorDetails"].get("displayName"), it["snippet"].get("displayMessage", ""))
        page = r.get("nextPageToken")
        stop.wait(max(2.0, r.get("pollingIntervalMillis", 5000) / 1000))
        if not r.get("items") and not page:
            return


def chat_source(src, channel=None, chat_file=None):
    """Pick the chat adapter: relay file > Twitch IRC > YouTube API (needs YOUTUBE_API_KEY). Kick: no stable public read API without an
    OAuth app + public webhook, so relay its messages into a jsonl file and pass --chat-file."""
    if chat_file:
        return lambda sink, stop: tail_chat(chat_file, sink, stop)
    if channel or "twitch.tv" in src:
        ch = channel or src.rstrip("/").split("/")[-1]
        return lambda sink, stop: twitch_chat(ch, sink, stop)
    if re.search(r"youtube\.com|youtu\.be", src) and os.environ.get("YOUTUBE_API_KEY"):
        return lambda sink, stop: youtube_chat(src, sink, stop, os.environ["YOUTUBE_API_KEY"])
    return None


class LiveTranscriber:
    """Transcribes the newest buffer segments every `every` seconds. Words get stream PTS. Overlap is re-read but never re-emitted.
    `fn(wav_path) -> [{"w","s","e"}]` (times relative to the wav); default is faster-whisper small + VAD."""

    def __init__(self, buf, fn=None, every=32, log=None):
        self.buf, self.fn, self.every, self.last_end, self.next_seg, self.model = Path(buf), fn, every, 0.0, 0, None
        self.log = log

    def _fn(self, wav):
        if self.model is None:
            from faster_whisper import WhisperModel
            self.model = WhisperModel("small", device="cuda" if shutil.which("nvidia-smi") else "cpu", compute_type="float16" if shutil.which("nvidia-smi") else "int8")
        segs, _ = self.model.transcribe(str(wav), word_timestamps=True, vad_filter=True)
        return [{"w": w.word.strip(), "s": w.start, "e": w.end} for s in segs for w in (s.words or []) if w.word.strip()]

    def poll(self, done_segs):
        """done_segs: sorted names of completed segments. Returns new words with absolute stream times."""
        idx = [int(n[4:10]) for n in done_segs]
        if not idx or (idx[-1] + 1 - self.next_seg) * SEG < self.every:
            return []
        first = max(self.next_seg - 1, idx[0], 0)  # one segment of overlap so a word on a boundary is not lost
        names = [n for n in done_segs if first <= int(n[4:10]) <= idx[-1]]
        lst, wav = self.buf / "stt.txt", self.buf / "stt.wav"
        lst.write_text("".join(f"file '{n}'\n" for n in names))
        r = run([ffmpeg(), "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", lst, "-vn", "-ac", "1", "-ar", "16000", wav], cwd=self.buf)
        self.next_seg = idx[-1] + 1
        if r.returncode:
            return []
        off = int(names[0][4:10]) * SEG
        new = []
        for w in (self.fn or self._fn)(wav):
            s, e = off + w["s"], off + w["e"]
            if s >= self.last_end - 0.05:
                new.append({"w": w["w"], "s": round(s, 2), "e": round(e, 2)})
        if new:
            self.last_end = new[-1]["e"]
        return new


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


def monitor(src, out_dir, chat_src=None, latency=8.0, record=True, log=print, max_seconds=None, stt=True, warmup=10, pre=PRE, post=POST, open_retries=12):
    out = Path(out_dir)
    buf = out / "buffer"
    shutil.rmtree(buf, ignore_errors=True)
    buf.mkdir(parents=True, exist_ok=True)
    cmd = [ffmpeg(), "-hide_banner", "-loglevel", "error", *(["-re"] if os.path.exists(src) else []), "-i", stream_url(src),
           "-map", "0", "-c", "copy", "-f", "segment", "-segment_time", SEG, "-segment_format", "mpegts", str(buf / "seg_%06d.ts")]
    if record:
        cmd += ["-map", "0", "-c", "copy", "-f", "mpegts", str(out / "record.ts")]
    for attempt in range(open_retries + 1):  # a monitor and a stream never start at the same instant
        proc = subprocess.Popen([str(c) for c in cmd])
        time.sleep(3)
        if proc.poll() is None or attempt == open_retries:
            break
        log(f"stream not available yet, retrying ({attempt + 1}/{open_retries})")
        time.sleep(5)
    t0, stop, chat_t, events, done_extract = time.time(), threading.Event(), [], [], set()
    chat_f = open(out / "chat.jsonl", "a")

    rows, obs = [], []  # chat is held as wall-clock until the stream clock is known

    def sink(wall, user, text):
        rows.append((wall, user, text))

    def flush_chat():
        """wall clock -> stream PTS. Each loop we know the media time `now` of the newest finished segment; wall - now is the clock offset
        (it includes delivery lag + poll delay). The MEDIAN of recent observations ignores the initial burst of buffered segments that a
        relay like MediaMTX sends, and `latency` calibrates the chat-vs-video delay the viewers add on top."""
        if len(obs) < 5:
            return
        off = sorted(obs[-15:])[len(obs[-15:]) // 2]
        while rows:
            wall, user, text = rows.pop(0)
            t = wall - off - latency
            chat_t.append(t)
            chat_f.write(json.dumps({"t": round(t, 2), "u": user, "m": text}) + "\n")
        chat_f.flush()
    if chat_src:
        threading.Thread(target=chat_src, args=(sink, stop), daemon=True).start()
    keep = max(KEEP, pre + post + 2 * BIN)
    chat_det, aud_det, seen, bin_i = SpikeDetector(60, 6.0, 8, warmup), SpikeDetector(60, 4.0, 1, warmup), set(), 0
    lt = LiveTranscriber(buf) if stt and importlib.util.find_spec("faster_whisper") else None
    sp_det, spbin, sp_f = SpikeDetector(60, 5.0, 3, warmup), {}, open(out / "live-transcript.jsonl", "a")
    if stt and not lt:
        log("live speech: faster-whisper not installed, skipping (chat + audio only)")
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
            for w in (lt.poll(sorted(seen)) if lt else []):  # words are scored as they arrive; speech spikes join chat/audio events
                spbin[int(w["s"] // BIN)] = spbin.get(int(w["s"] // BIN), 0.0) + word_score(w)
                sp_f.write(json.dumps(w) + "\n")
            sp_f.flush()
            now = (len(seen)) * SEG
            obs.append(time.time() - now)
            flush_chat()
            while (bin_i + 1) * BIN <= now:  # complete chat bins
                c = sum(1 for t in chat_t if bin_i * BIN <= t < (bin_i + 1) * BIN)
                if chat_det.feed(c):
                    events.append({"pts": bin_i * BIN, "kind": "chat", "x": c})
                if lt and sp_det.feed(spbin.pop(bin_i, 0.0)):
                    events.append({"pts": bin_i * BIN, "kind": "speech", "x": 0})
                bin_i += 1
            for w in merge_events(events, pre, post):
                key = round(w["start"])
                if w["end"] <= now and key not in done_extract:
                    done_extract.add(key)
                    f = extract(buf, segs, int(w["start"] // SEG), int(w["end"] // SEG), w["start"], w["end"], out / f"prelim_{hms(w['start']).replace(':', '-')}.mkv")
                    log(f"event {hms(w['start'])}-{hms(w['end'])} {w['kinds']} conf={w['confidence']} -> {f}")
            for n in segs:  # rolling retention
                if int(n[4:10]) * SEG < now - keep:
                    (buf / n).unlink(missing_ok=True)
            (out / "events.json").write_text(json.dumps(merge_events(events, pre, post), indent=1))
    finally:
        stop.set()
        proc.terminate()
        chat_f.close()
        sp_f.close()
    log(f"stopped. Next: clipper process {out / 'record.ts'} --chat {out / 'chat.jsonl'}")


def sync_check(out_dir, radius=25):
    """Measure chat-vs-video skew from a finished `monitor` run: the busiest chat second vs the loudest audio second within `radius` s.
    -> (skew_seconds, chat_t, audio_t). skew > 0 means chat sits LATER than the video: re-run monitor with --latency <current + skew>.
    Needs one clearly loud moment the audience reacted to (a goal, a scream) and a recording."""
    from .signals import audio_rms
    from .util import probe
    out = Path(out_dir)
    chat = [json.loads(x) for x in open(out / "chat.jsonl")]
    per = {}
    for c in chat:
        per[int(c["t"])] = per.get(int(c["t"]), 0) + 1
    if not per:
        raise SystemExit("no chat recorded")
    ct = max(per, key=per.get)
    rec = out / "record.ts"
    rms = audio_rms(str(rec), probe(str(rec))["duration"])
    lo, hi = max(0, ct - radius), min(len(rms), ct + radius)
    at = max(range(lo, hi), key=lambda i: rms[i])
    return ct - at, ct, at
