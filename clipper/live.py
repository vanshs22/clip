"""Live monitor: rolling segment buffer + full recording, chat/audio spike detection, preliminary clips.
Final selection happens AFTER the stream: `clipper process record.ts --chat chat.jsonl` reuses everything here.
v1 live signals: chat (Twitch IRC, or tail --chat-file) + audio level. Live speech/vision run post-stream."""
import collections, datetime, importlib.util, json, os, re, shutil, signal, socket, ssl, threading, time, urllib.parse, urllib.request
from pathlib import Path
from .signals import SpikeDetector, word_score
from .util import ffmpeg, run, hms, jload, jsave, log_event, note_skipped, check_disk, validate_source
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


STATES = ("starting", "waiting_for_stream", "recording", "finalizing", "processing", "done", "failed", "cancelled")
MAX_CHAT_BINS = 400      # ~66 min of 10 s bins: the adaptive baseline never needs more
MAX_PENDING_CHAT = 20000  # messages waiting for the stream clock; oldest dropped beyond this (bounded memory on multi-hour streams)


class Session:
    """Persistent live-session metadata (session.json): state machine + paths + gaps. Survives a crash; `watch --resume` continues from it."""

    def __init__(self, out):
        self.out = Path(out)
        self.out.mkdir(parents=True, exist_ok=True)
        self.d = jload(self.out / "session.json", {}) or {"state": "starting", "parts": [], "gaps": [], "history": [], "created": time.time()}

    def set(self, state, **kw):
        assert state in STATES, state
        self.d.update(kw, state=state)
        self.d["history"].append([state, round(time.time(), 1)])
        self.d["history"] = self.d["history"][-50:]
        jsave(self.out / "session.json", self.d)
        log_event(self.out / "session.log", "state", state=state, **{k: v for k, v in kw.items() if not isinstance(v, (list, dict))})


def extract(buf_dir, seg_names, start_idx, end_idx, a, b, out, reencode=True):
    """Concatenate buffer segments [start_idx, end_idx] and cut [a, b] (stream seconds). Re-encoded by default: stream-copy can only cut on
    keyframes, so clip edges would be off by up to a GOP (several seconds)."""
    names = [n for n in seg_names if start_idx <= int(n[4:10]) <= end_idx]
    if not names:
        return None
    first = int(names[0][4:10])  # oldest segment may already be pruned
    lst = Path(buf_dir) / f"list_{start_idx}.txt"
    lst.write_text("".join(f"file '{n}'\n" for n in names))
    codec = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-c:a", "aac"] if reencode else ["-c", "copy"]
    r = run([ffmpeg(), "-y", "-hide_banner", "-f", "concat", "-safe", "0", "-i", lst, "-ss", f"{max(0.0, a - first * SEG):.2f}", "-t", f"{b - a:.2f}", *codec, out], cwd=buf_dir, timeout=900)
    lst.unlink(missing_ok=True)
    return out if r.returncode == 0 else None


def _ffmpeg_cmd(src, buf, record, start_number):
    return [ffmpeg(), "-hide_banner", "-loglevel", "error", *(["-re"] if os.path.exists(src) else []), "-i", stream_url(src),
            "-map", "0", "-c", "copy", "-f", "segment", "-segment_time", SEG, "-segment_format", "mpegts", "-segment_start_number", start_number, str(buf / "seg_%06d.ts"),
            *(["-map", "0", "-c", "copy", "-f", "mpegts", str(record)] if record else [])]


def monitor(src, out_dir, chat_src=None, latency=8.0, record=True, log=print, max_seconds=None, stt=True, warmup=10, pre=PRE, post=POST,
            open_retries=12, reconnect=True, stop_event=None, judge_fn=None, session=None):
    """Watch a live source until it ends (or max_seconds / stop_event). Keeps memory bounded however long the stream runs.
    Returns the Session. After it returns the caller finalizes (see `finalize`)."""
    validate_source(src)
    out = Path(out_dir)
    check_disk(out, float(os.environ.get("CLIPPER_MIN_FREE_GB", 2)))
    sess = session or Session(out)
    buf = out / "buffer"
    shutil.rmtree(buf, ignore_errors=True)
    buf.mkdir(parents=True, exist_ok=True)
    stop = stop_event or threading.Event()
    sess.set("waiting_for_stream", src=src, pre=pre, post=post, latency=latency)
    parts = []                      # record_000.ts, record_001.ts ... (one per ffmpeg run; a reconnect starts a new part)
    next_seg = 0
    chat_f = open(out / "chat.jsonl", "a")
    rows = collections.deque(maxlen=MAX_PENDING_CHAT)
    obs = collections.deque(maxlen=15)
    bins = collections.OrderedDict()   # chat bin -> message count (rolling)
    t0 = time.time()

    def sink(wall, user, text):
        rows.append((wall, user, text))

    def flush_chat():
        """wall clock -> stream PTS via the MEDIAN of (wall - media time of newest finished segment): robust to the initial burst of buffered
        segments a relay sends and to reconnect gaps. `latency` is the chat-vs-video delay viewers add on top."""
        if len(obs) < 5:
            return
        off = sorted(obs)[len(obs) // 2]
        while rows:
            wall, user, text = rows.popleft()
            t = wall - off - latency
            bins[int(t // BIN)] = bins.get(int(t // BIN), 0) + 1
            chat_f.write(json.dumps({"t": round(t, 2), "u": user, "m": text}) + "\n")
        while len(bins) > MAX_CHAT_BINS:
            bins.popitem(last=False)
        chat_f.flush()
    if chat_src:
        threading.Thread(target=chat_src, args=(sink, stop), daemon=True).start()
    chat_det, aud_det, seen, bin_i = SpikeDetector(60, 6.0, 8, warmup), SpikeDetector(60, 4.0, 1, warmup), collections.OrderedDict(), 0
    lt = LiveTranscriber(buf) if stt and importlib.util.find_spec("faster_whisper") else None
    sp_det, spbin = SpikeDetector(60, 5.0, 3, warmup), {}
    sp_f = open(out / "live-transcript.jsonl", "a")
    live_words = collections.deque(maxlen=6000)   # recent words for live judging context
    if stt and not lt:
        note_skipped("live speech transcription", "faster-whisper not installed: chat + audio spikes only")
    events, done_extract, all_events = [], set(), []
    ev_f = open(out / "events.jsonl", "a")

    def add_event(e):
        events.append(e)
        ev_f.write(json.dumps(e) + "\n")
        ev_f.flush()
        if len(events) > 400:  # old events are already merged/extracted; the file keeps the full history
            del events[:100]
    try:
        while not stop.is_set():
            record_part = out / f"record_{len(parts):03d}.ts" if record else None
            if record_part:
                parts.append(record_part)
            proc = subprocess.Popen([str(c) for c in _ffmpeg_cmd(src, buf, record_part, next_seg)], stderr=subprocess.DEVNULL if not os.environ.get("CLIPPER_DEBUG") else None)
            ok = False
            for attempt in range(open_retries + 1):
                time.sleep(3)
                if proc.poll() is None:
                    ok = True
                    break
                if attempt == open_retries:
                    break
                log(f"stream not available yet, retrying ({attempt + 1}/{open_retries})")
                time.sleep(5)
                proc = subprocess.Popen([str(c) for c in _ffmpeg_cmd(src, buf, record_part, next_seg)], stderr=subprocess.DEVNULL)
            if not ok:
                if record_part and parts and parts[-1] == record_part and not record_part.exists():
                    parts.pop()
                break  # never came up (or already ended): finalize with what we have
            sess.set("recording", parts=[p.name for p in parts], gaps=sess.d.get("gaps", []))
            while proc.poll() is None and not stop.is_set() and (max_seconds is None or time.time() - t0 < max_seconds):
                time.sleep(2)
                segs = sorted(p.name for p in buf.glob("seg_*.ts"))
                for n in segs[:-1]:  # the last file is still being written
                    if n in seen:
                        continue
                    seen[n] = 1
                    while len(seen) > 600:
                        seen.popitem(last=False)
                    i = int(n[4:10])
                    r = run([ffmpeg(), "-hide_banner", "-i", buf / n, "-vn", "-af", "astats=metadata=1,ametadata=print:key=lavfi.astats.Overall.RMS_level:file=-", "-f", "null", "-"], timeout=60)
                    lv = [float(x) for x in re.findall(r"RMS_level=(-?[\d.]+)", r.stdout)]
                    if lv and aud_det.feed(max(lv[-1], -90) + 90):
                        add_event({"pts": i * SEG, "kind": "audio", "x": lv[-1]})
                for w in (lt.poll(sorted(seen)) if lt else []):
                    spbin[int(w["s"] // BIN)] = spbin.get(int(w["s"] // BIN), 0.0) + word_score(w)
                    sp_f.write(json.dumps(w) + "\n")
                    live_words.append(w)
                sp_f.flush()
                now = (int(segs[-2][4:10]) + 1 if len(segs) >= 2 else 0) * SEG  # media time at the end of the newest FINISHED segment
                obs.append(time.time() - now)
                flush_chat()
                while (bin_i + 1) * BIN <= now:  # complete bins
                    c = bins.get(bin_i, 0)
                    if chat_det.feed(c):
                        add_event({"pts": bin_i * BIN, "kind": "chat", "x": c})
                    if lt and sp_det.feed(spbin.pop(bin_i, 0.0)):
                        add_event({"pts": bin_i * BIN, "kind": "speech", "x": 0})
                    bin_i += 1
                    for k in [k for k in spbin if k < bin_i - 6]:
                        spbin.pop(k, None)
                for w in merge_events(events, pre, post):
                    key = round(w["start"])
                    if w["end"] <= now and key not in done_extract:
                        done_extract.add(key)
                        f = extract(buf, segs, int(w["start"] // SEG), int(w["end"] // SEG), w["start"], w["end"], out / f"prelim_{hms(w['start']).replace(':', '-')}.mkv")
                        w["clip"] = str(f) if f else None
                        if judge_fn and w["confidence"] >= 0.6:  # local evidence justifies a (budgeted, cached) LLM opinion
                            try:
                                w["llm"] = judge_fn(w, list(live_words))
                            except Exception as e:  # a judge failure must not stop the recording
                                log_event(out / "session.log", "judge_error", error=str(e)[:200])
                        all_events.append(w)
                        log(f"event {hms(w['start'])}-{hms(w['end'])} {w['kinds']} conf={w['confidence']} -> {f}")
                keepn = max(KEEP, pre + post + 2 * BIN)
                for n in segs:  # rolling retention
                    if int(n[4:10]) * SEG < now - keepn:
                        (buf / n).unlink(missing_ok=True)
                jsave(out / "events.json", all_events[-500:])
            ended_by_limit = stop.is_set() or (max_seconds is not None and time.time() - t0 >= max_seconds)
            try:
                proc.terminate()
                proc.wait(timeout=10)
            except Exception:
                proc.kill()
            segs_now = sorted(buf.glob("seg_*.ts"))
            next_seg = (int(segs_now[-1].name[4:10]) + 1) if segs_now else next_seg
            if ended_by_limit or not reconnect:
                break
            # the source dropped: try to reconnect for a while (a reconnect = a new record part, a timestamp reset, a recorded gap)
            gap_start = time.time()
            sess.set("waiting_for_stream", gaps=sess.d.get("gaps", []))
            back = False
            for _ in range(6):
                time.sleep(5)
                if stop.is_set():
                    break
                if run([ffmpeg(), "-hide_banner", "-v", "error", "-i", stream_url(src), "-t", "1", "-f", "null", "-"], timeout=30).returncode == 0:
                    back = True
                    break
            if not back:
                break
            sess.d.setdefault("gaps", []).append({"at_media_s": next_seg * SEG, "seconds": round(time.time() - gap_start, 1)})
    finally:
        stop.set()
        for fh in (chat_f, sp_f, ev_f):
            try:
                fh.close()
            except Exception:
                pass
        jsave(out / "events.json", all_events[-500:])
    sess.set("finalizing", parts=[p.name for p in parts if p.exists()])
    return sess


def finalize(out_dir, log=print):
    """Stop-of-stream housekeeping, run automatically: (1) join record parts into record.ts (re-encoded if a reconnect reset timestamps),
    (2) return the paths the VOD pipeline needs."""
    out = Path(out_dir)
    sess = Session(out)
    parts = [out / n for n in sess.d.get("parts", []) if (out / n).exists() and (out / n).stat().st_size > 10000]
    if not parts:
        raise SystemExit("nothing was recorded (the stream never came up?)")
    rec = out / "record.ts"
    if len(parts) == 1:
        os.replace(parts[0], rec)
    else:
        lst = out / "parts.txt"
        lst.write_text("".join(f"file '{p.name}'\n" for p in parts))
        r = run([ffmpeg(), "-y", "-hide_banner", "-f", "concat", "-safe", "0", "-i", lst, "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-c:a", "aac", "-f", "mpegts", rec], cwd=out, timeout=7200)
        if r.returncode:
            raise SystemExit("could not join the recorded parts: " + r.stderr[-300:])
        for p in parts:
            p.unlink(missing_ok=True)
        lst.unlink(missing_ok=True)
    evs = [e for e in (jload(out / "events.json", []) or [])]
    return {"record": str(rec), "chat": str(out / "chat.jsonl"), "events": str(out / "events.json"), "n_events": len(evs)}


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
