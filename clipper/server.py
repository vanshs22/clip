"""Agent interfaces over the CLI: MCP (stdio, JSON-RPC 2.0) and a local HTTP API, plus a persistent SQLite task queue.
Every tool builds a validated `clipper ...` argv and runs it in a subprocess (never a shell), so the CLI stays the single source of behaviour and a
crashing render cannot take the server down. Slow tools are queued and the MCP loop never blocks; tasks record their pid so recovery after a crash
re-queues only tasks whose worker is really dead (two servers on one jobs dir do not steal each other's work) and `cancel_task` kills the tree."""
import json, os, re, secrets, signal, sqlite3, subprocess, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from .util import digest, pid_alive, redact, validate_source, safe_path

ROOT = Path(os.environ.get("CLIPPER_JOBS", "clipper-jobs"))
STYLES = ["bold_pop", "karaoke", "clean_lower", "boxed", "minimal_white", "highlight_words", "podcast_speaker", "gaming_bold", "cinematic", "custom", "none"]
PROFILES = ["podcast", "interview", "gaming", "irl", "comedy", "sports", "auto"]
MODES = ["disabled", "local_only", "smart", "quality"]
QUALITY = ["draft", "normal", "high"]
JOB = re.compile(r"^[0-9a-f]{10}$")
MAX_HEAVY = int(os.environ.get("CLIPPER_MAX_HEAVY", "1"))  # heavy tasks run at the same time (RAM/GPU protection)
PROTOCOL = "2024-11-05"

COMMON = {"clips": ("int", None), "profile": ("str", PROFILES), "style": ("str", STYLES), "mode": ("str", MODES), "quality": ("str", QUALITY), "style_file": ("path", None),
          "chat": ("path", None), "transcript": ("path", None), "cookies": ("path", None), "logo": ("path", None), "model": ("str", None),
          "upscale": ("str", ["auto", "ai", "off"]), "speakers": ("str", ["auto", "off"]), "max_usd": ("float", None), "max_calls": ("int", None), "fade": ("bool", None)}
# name: (argv prefix, positional args, {param: (type, enum|None)}, lane, description)   lane: sync | work | live
TOOLS = {
    "clip_video": (["run"], ["src"], {**COMMON, "export": ("path", None), "no_polish": ("bool", None)}, "work",
                   "ONE-SHOT: clip a recorded video or local file end to end (download, transcribe, detect, rank, render, editor-critic polish, export). Returns task+job id; poll get_task / get_job_status."),
    "monitor_livestream": (["watch"], ["src"], {**COMMON, "out": ("path", None), "channel": ("str", None), "chat_file": ("path", None), "latency": ("float", None),
                           "max_seconds": ("float", None), "no_stt": ("bool", None), "resume": ("bool", None), "no_finalize": ("bool", None)}, "live",
                           "ONE-SHOT livestream: record, detect moments (chat/audio/speech), finalize automatically when the stream ends, process, polish and export. No second command."),
    "get_job_status": (["status"], ["job"], {}, "sync", "Stage progress, clips (time/title/text/issues), QC result, LLM cost."),
    "get_clip_candidates": (["status"], ["job"], {}, "sync", "The ranked clips in plan.json (alias of get_job_status)."),
    "review_clip": (["critique"], ["job"], {"packet": ("bool", None), "submit": ("path", None), "unavailable": ("str", None)}, "sync",
                    "Editor-critic review of the rendered clips. packet=true writes contact sheets for the vision pass; submit=<findings file>; unavailable=<why> if you cannot view images."),
    "edit_clip": (["edit"], ["job", "clip"], {"start_shift": ("float", None), "end_shift": ("float", None), "captions": ("str", STYLES), "caption_scale": ("float", None),
                  "zoom": ("float", None), "zoom_at": ("float", None), "zoom_for": ("float", None), "zoom_amt": ("float", None), "no_zooms": ("bool", None),
                  "layout": ("str", ["fill", "fit"]), "focus_x": ("float", None), "drop": ("bool", None), "broll_dir": ("path", None), "no_broll": ("bool", None),
                  "music": ("path", None), "music_db": ("float", None), "audio": ("str", ["clean", "raw"]), "upscale": ("str", ["auto", "ai", "off"]), "interpolate": ("bool", None),
                  "logo": ("path", None), "fade": ("bool", None)}, "sync", "Structured edit of ONE clip; only that clip re-renders."),
    "render_clips": (["render"], ["job"], {"draft": ("bool", "neg")}, "work", "Render + QC all clips (cached per clip)."),
    "polish_clips": (["polish"], ["job"], {}, "work", "Editor-critic loop: critique, apply mechanical fixes, re-render (max 3 rounds)."),
    "export_clips": (["export"], ["job"], {"out": ("path", None), "all": ("bool", None)}, "sync", "Copy passing clips + metadata.json (cost, models, skipped features, verdicts)."),
    "get_system_capabilities": (["doctor"], [], {}, "sync", "What works on this machine: ffmpeg/encoder, packages, models, GPU, keys present (never their values)."),
}
LIFECYCLE = {"get_task", "cancel_task"}


class ToolError(Exception):
    pass


def _val(name, v, typ, enum):
    if typ == "bool":
        if not isinstance(v, bool):
            raise ToolError(f"{name}: boolean expected")
        return v
    if typ in ("int", "float"):
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ToolError(f"{name}: number expected")
        if v != v or abs(v) > 1e9:
            raise ToolError(f"{name}: out of range")
        return str(int(v) if typ == "int" else float(v))
    if not isinstance(v, str) or not v or len(v) > 4096 or re.search(r"[\x00-\x1f]", v):
        raise ToolError(f"{name}: non-empty single-line string expected")
    if v.startswith("-"):
        raise ToolError(f"{name}: must not start with '-'")  # option injection
    if isinstance(enum, list) and v not in enum:
        raise ToolError(f"{name}: one of {enum}")
    if typ == "path" and name not in ("src",):
        try:
            safe_path(v)
        except ValueError:
            raise ToolError(f"{name}: path must be inside the working directory")
    return v


def build_argv(tool, args):
    if tool not in TOOLS:
        raise ToolError(f"unknown tool {tool}")
    prefix, pos, params, lane, _ = TOOLS[tool]
    argv = list(prefix)
    if not isinstance(args, dict):
        raise ToolError("arguments must be an object")
    for p in pos:
        if p not in args:
            raise ToolError(f"missing {p}")
        if p == "job" and not JOB.match(str(args[p])):
            raise ToolError("job: bad id")
        if p == "src":
            try:
                validate_source(args[p])
            except ValueError as e:
                raise ToolError(f"src: {e}")
        argv.append(_val(p, args[p], "int" if p == "clip" else "str", None))
    for k, v in args.items():
        if k in pos or k.startswith("_"):
            continue
        if k not in params:
            raise ToolError(f"unknown argument {k}")
        typ, enum = params[k]
        s, flag = _val(k, v, typ, enum if typ != "bool" else None), "--" + k.replace("_", "-")
        if typ == "bool":
            if s:
                argv.append(flag)
            elif enum == "neg":
                argv.append("--no-" + k)
        else:
            argv += [flag, s]
    return argv, lane


def schema(tool):
    _, pos, params, lane, desc = TOOLS[tool]
    t = {"int": "integer", "float": "number", "bool": "boolean"}
    props = {p: {"type": "integer" if p == "clip" else "string", "description": {"src": "URL or local path", "job": "10-hex job id", "clip": "clip number"}.get(p, "")} for p in pos}
    for k, (typ, enum) in params.items():
        props[k] = {"type": t.get(typ, "string"), **({"enum": enum} if isinstance(enum, list) else {})}
    return {"name": tool, "description": desc + (" (queued: poll get_task)" if lane != "sync" else ""),
            "inputSchema": {"type": "object", "properties": props, "required": pos, "additionalProperties": False}}


TOOLS_LIST = [schema(t) for t in TOOLS] + [
    {"name": "get_task", "description": "Status + log tail of a queued task.", "inputSchema": {"type": "object", "properties": {"task": {"type": "integer"}}, "required": ["task"], "additionalProperties": False}},
    {"name": "cancel_task", "description": "Cancel a queued or running task (kills its process tree; a watch session is marked cancelled and not processed).",
     "inputSchema": {"type": "object", "properties": {"task": {"type": "integer"}}, "required": ["task"], "additionalProperties": False}}]


# ---- persistent queue -------------------------------------------------------------------------------------------------
_lock = threading.Lock()


def _db():
    ROOT.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(ROOT / "queue.db", timeout=30)
    c.execute("create table if not exists tasks(id integer primary key, argv text, lane text, status text, rc integer, ts real, pid integer, owner integer)")
    for col in ("pid", "owner"):  # databases created by older versions
        try:
            c.execute(f"alter table tasks add column {col} integer")
        except sqlite3.OperationalError:
            pass
    return c


def submit(argv, lane):
    with _lock, _db() as c:
        tid = c.execute("insert into tasks(argv,lane,status,ts) values(?,?,?,?)", (json.dumps(argv), lane, "queued", time.time())).lastrowid
    if lane == "live":  # long-running sessions get their own process immediately (they are mostly waiting, not computing)
        with _lock, _db() as c:
            c.execute("update tasks set status='running', owner=? where id=?", (os.getpid(), tid))
        threading.Thread(target=_exec, args=(tid, argv), daemon=True).start()
    return tid


def _exec(tid, argv):
    (ROOT / "tasks").mkdir(parents=True, exist_ok=True)
    with open(ROOT / "tasks" / f"{tid}.log", "ab") as log:
        kw = {"start_new_session": True} if os.name != "nt" else {"creationflags": 0x200}  # own process group so cancel can kill the tree
        p = subprocess.Popen([sys.executable, "-m", "clipper", *argv], stdout=log, stderr=subprocess.STDOUT, **kw)
        with _lock, _db() as c:
            c.execute("update tasks set pid=? where id=?", (p.pid, tid))
        rc = p.wait()
    with _lock, _db() as c:
        cur = c.execute("select status from tasks where id=?", (tid,)).fetchone()
        if cur and cur[0] != "cancelled":
            c.execute("update tasks set status=?, rc=? where id=?", ("done" if rc == 0 else "failed", rc, tid))


def recover():
    """Re-queue tasks that were running under a worker that no longer exists. A task whose owner process is alive is left alone."""
    with _lock, _db() as c:
        for tid, owner, pid, lane in c.execute("select id, owner, pid, lane from tasks where status='running'").fetchall():
            if owner and pid_alive(owner) and owner != os.getpid():
                continue
            if lane == "work":
                c.execute("update tasks set status='queued', owner=NULL, pid=NULL where id=?", (tid,))
            else:
                c.execute("update tasks set status='failed', rc=-1 where id=?", (tid,))  # a live session cannot be re-run; `watch --resume` can finish it


def _heavy_running():
    with _lock, _db() as c:
        return c.execute("select count(*) from tasks where status='running' and lane='work'").fetchone()[0]


def worker(stop=None):
    """Claims queued heavy tasks atomically; at most MAX_HEAVY run at once across all servers sharing this jobs dir."""
    recover()
    while not (stop and stop.is_set()):
        if _heavy_running() < MAX_HEAVY:
            with _lock, _db() as c:
                row = c.execute("select id, argv from tasks where status='queued' and lane='work' order by id limit 1").fetchone()
                got = row and c.execute("update tasks set status='running', owner=? where id=? and status='queued'", (os.getpid(), row[0])).rowcount
            if got:
                threading.Thread(target=_exec, args=(row[0], json.loads(row[1])), daemon=True).start()
                continue
        time.sleep(1)


def get_task(tid):
    with _lock, _db() as c:
        r = c.execute("select status, rc, argv, pid from tasks where id=?", (int(tid),)).fetchone()
    if not r:
        raise ToolError("no such task")
    f = ROOT / "tasks" / f"{tid}.log"
    tail = redact(f.read_text(errors="ignore")[-1500:]) if f.exists() else ""
    return {"task": int(tid), "status": r[0], "returncode": r[1], "argv": json.loads(r[2]), "log_tail": tail}


def cancel_task(tid):
    t = get_task(tid)
    if t["status"] in ("done", "failed", "cancelled"):
        return {**t, "note": "already finished"}
    with _lock, _db() as c:
        pid = c.execute("select pid from tasks where id=?", (int(tid),)).fetchone()[0]
        c.execute("update tasks set status='cancelled', rc=-15 where id=?", (int(tid),))
    argv = t["argv"]
    if argv and argv[0] == "watch" and "--out" in argv:  # a watch session must not process what it recorded: mark it cancelled
        try:
            (Path(argv[argv.index("--out") + 1]) / "CANCEL").touch()
        except OSError:
            pass
    if pid and pid_alive(pid):
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True)
            else:
                os.killpg(os.getpgid(pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            pass
    return {**get_task(tid), "note": "cancelled"}


def call_tool(name, args, timeout=900):
    """-> JSON-serialisable result. Raises ToolError on bad input."""
    if name == "get_task":
        return get_task((args or {}).get("task", -1))
    if name == "cancel_task":
        return cancel_task((args or {}).get("task", -1))
    argv, lane = build_argv(name, args or {})
    if name == "get_system_capabilities":
        from .cli import capabilities
        caps = capabilities()
        caps.pop("models", None) if not (args or {}).get("models") else None
        return caps
    if lane == "sync":
        r = subprocess.run([sys.executable, "-m", "clipper", *argv], capture_output=True, text=True, timeout=timeout)
        out = r.stdout.strip()
        if r.returncode:
            raise ToolError(redact((r.stderr or out)[-800:]))
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            return {"output": redact(out)}
    res = {"task": submit(argv, lane), "queued": " ".join(argv)}
    if name == "clip_video":
        res["job"] = digest(__import__("clipper.job", fromlist=["x"]).source_fingerprint(args["src"]))[:10]  # the id the pipeline will use
    return res


# ---- MCP (stdio, newline-delimited JSON-RPC 2.0) -------------------------------------------------------------------------------
def mcp(stdin=None, stdout=None):
    stdin, stdout = stdin or sys.stdin, stdout or sys.stdout
    wlock, tokens, cancelled = threading.Lock(), {}, set()

    def send(obj):
        with wlock:
            stdout.write(json.dumps(obj) + "\n")
            stdout.flush()

    def reply(mid, result=None, error=None):
        send({"jsonrpc": "2.0", "id": mid, **({"error": error} if error else {"result": result})})

    def tool_result(text, is_error=False):
        return {"isError": is_error, "content": [{"type": "text", "text": text if isinstance(text, str) else json.dumps(text, indent=1)}]}

    def run_call(mid, params):
        name, args = params.get("name"), params.get("arguments") or {}
        tok = (params.get("_meta") or {}).get("progressToken")
        try:
            res = call_tool(name, args)
            if tok is not None and isinstance(res, dict) and "task" in res:
                tokens[res["task"]] = tok
            if mid in cancelled:
                return
            reply(mid, tool_result(res))
        except ToolError as e:
            reply(mid, tool_result(str(e), True))
        except subprocess.TimeoutExpired:
            reply(mid, tool_result("tool timed out", True))
        except Exception as e:  # never let a tool bug kill the server
            reply(mid, tool_result(f"internal error: {redact(type(e).__name__ + ': ' + str(e))[:300]}", True))

    def progress():  # MCP notifications/progress for queued tasks whose caller supplied a progressToken
        last = {}
        while True:
            time.sleep(3)
            for tid, tok in list(tokens.items()):
                try:
                    t = get_task(tid)
                except ToolError:
                    tokens.pop(tid, None)
                    continue
                tail = [l for l in t["log_tail"].splitlines() if l.strip()][-1:] or [""]
                key = (t["status"], tail[0])
                if last.get(tid) != key:
                    last[tid] = key
                    send({"jsonrpc": "2.0", "method": "notifications/progress", "params": {"progressToken": tok, "progress": {"queued": 0, "running": 1, "done": 3, "failed": 3, "cancelled": 3}.get(t["status"], 0), "total": 3, "message": f"{t['status']}: {tail[0][:200]}"}})
                if t["status"] in ("done", "failed", "cancelled"):
                    tokens.pop(tid, None)
    threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=progress, daemon=True).start()
    for line in stdin:
        if not line.strip():
            continue
        try:
            m = json.loads(line)
        except json.JSONDecodeError:
            reply(None, error={"code": -32700, "message": "parse error"})
            continue
        if not isinstance(m, dict) or "method" not in m:
            continue
        mid, meth, params = m.get("id"), m["method"], m.get("params") or {}
        if mid is None:  # notification
            if meth == "notifications/cancelled" and params.get("requestId") is not None:
                cancelled.add(params["requestId"])
            continue
        if meth == "initialize":
            reply(mid, {"protocolVersion": params.get("protocolVersion", PROTOCOL), "capabilities": {"tools": {"listChanged": False}}, "serverInfo": {"name": "clipper", "version": "0.4.0"},
                        "instructions": "Use clip_video for recorded videos and monitor_livestream for live streams; both run the whole workflow. Poll get_task for progress."})
        elif meth == "tools/list":
            reply(mid, {"tools": TOOLS_LIST})
        elif meth == "tools/call":
            threading.Thread(target=run_call, args=(mid, params), daemon=True).start()  # the loop never blocks on a tool
            continue
        elif meth == "ping":
            reply(mid, {})
        else:
            reply(mid, error={"code": -32601, "message": f"method not found: {meth}"})


# ---- HTTP (127.0.0.1 only, bearer token, Host check) ------------------------------------------------------------------------------
def http(port=8765, token=None):
    token = token or os.environ.get("CLIPPER_TOKEN") or secrets.token_urlsafe(24)
    threading.Thread(target=worker, daemon=True).start()

    class H(BaseHTTPRequestHandler):
        def _send(self, code, obj):
            b = json.dumps(obj).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def _auth(self):  # token blocks other local apps; Host check blocks DNS-rebinding from web pages
            ok = secrets.compare_digest(self.headers.get("Authorization", ""), f"Bearer {token}") and self.headers.get("Host", "") in (f"127.0.0.1:{port}", f"localhost:{port}")
            if not ok:
                self._send(401, {"error": "unauthorized"})
            return ok

        def do_GET(self):
            if not self._auth():
                return
            try:
                if self.path == "/tools":
                    return self._send(200, TOOLS_LIST)
                if m := re.fullmatch(r"/tasks/(\d+)", self.path):
                    return self._send(200, get_task(m[1]))
                self._send(404, {"error": "not found"})
            except ToolError as e:
                self._send(400, {"error": str(e)})

        def do_POST(self):
            if not self._auth():
                return
            n = int(self.headers.get("Content-Length", 0))
            if n > 65536:
                return self._send(413, {"error": "too large"})
            try:
                m = re.fullmatch(r"/tools/(\w+)", self.path)
                if not m:
                    return self._send(404, {"error": "not found"})
                self._send(200, call_tool(m[1], json.loads(self.rfile.read(n) or b"{}")))
            except (ToolError, json.JSONDecodeError, subprocess.TimeoutExpired) as e:
                self._send(400, {"error": redact(str(e))})

        def log_message(self, *a):
            pass
    print(f"clipper HTTP on http://127.0.0.1:{port}  token: {token}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
