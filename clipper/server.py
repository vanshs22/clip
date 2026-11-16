"""Agent interfaces over the CLI: MCP (stdio) and local HTTP, plus a persistent SQLite task queue.
Every tool builds a validated `clipper ...` argv and runs it in a subprocess, so the CLI stays the single source of behaviour
and a crashing render cannot take the server down. Slow tools are queued; a restart re-queues interrupted tasks
(finished stages are cached, so the retry is cheap)."""
import json, os, re, secrets, sqlite3, subprocess, sys, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from .util import digest

ROOT = Path(os.environ.get("CLIPPER_JOBS", "clipper-jobs"))
STYLES = ["bold_pop", "karaoke", "clean_lower", "boxed", "none"]
PROFILES = ["podcast", "interview", "gaming", "irl", "comedy", "sports", "auto"]
JOB = re.compile(r"^[0-9a-f]{10}$")

# name: (argv prefix, positional args, {param: (type, enum|None)}, lane, description)
#   lane: sync (run inline) | work (single queued worker) | live (own long-running process)
TOOLS = {
    "clip_video": (["process"], ["src"], {"clips": ("int", None), "profile": ("str", PROFILES), "style": ("str", STYLES),
                   "mode": ("str", ["disabled", "local_only", "smart", "quality"]), "stop_after": ("str", None), "chat": ("path", None),
                   "transcript": ("path", None), "cookies": ("path", None), "draft": ("bool", None)}, "work",
                   "Download, transcribe, detect and (optionally) render clips. Returns job id + task id. Use stop_after=select to review first."),
    "get_job_status": (["status"], ["job"], {}, "sync", "Stage progress, clips (time/title/text/issues), QC result, LLM cost."),
    "get_clip_candidates": (["status"], ["job"], {}, "sync", "Alias of get_job_status: the ranked clips in plan.json."),
    "next_step": (["next"], ["job"], {}, "sync", "The one next action for a job."),
    "edit_clip": (["edit"], ["job", "clip"], {"start_shift": ("float", None), "end_shift": ("float", None), "captions": ("str", STYLES),
                  "caption_scale": ("float", None), "zoom": ("float", None), "zoom_at": ("float", None), "zoom_for": ("float", None),
                  "zoom_amt": ("float", None), "no_zooms": ("bool", None), "layout": ("str", ["fill", "fit"]), "focus_x": ("float", None),
                  "drop": ("bool", None)}, "sync", "Structured edit of one clip; only that clip re-renders."),
    "render_clips": (["render"], ["job"], {"draft": ("bool", "neg")}, "work", "Render + QC all clips (cached per clip)."),
    "export_clips": (["export"], ["job"], {"out": ("path", None), "all": ("bool", None)}, "sync", "Copy passing clips + metadata.json."),
    "monitor_livestream": (["monitor"], ["src"], {"out": ("path", None), "channel": ("str", None), "latency": ("float", None),
                           "max_seconds": ("float", None), "chat_file": ("path", None)}, "live",
                           "Record + watch a live stream; emits events.json and preliminary clips. Then process record.ts."),
}


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
        return str(int(v) if typ == "int" else float(v))
    v = str(v)
    if v.startswith("-"):
        raise ToolError(f"{name}: must not start with '-'")  # option injection
    if isinstance(enum, list) and v not in enum:
        raise ToolError(f"{name}: one of {enum}")
    if typ == "path" and name != "src" and not (Path.cwd() in (p := Path(v).resolve()).parents or p == Path.cwd()):
        raise ToolError(f"{name}: path must be inside the working directory")
    return v


def build_argv(tool, args):
    if tool not in TOOLS:
        raise ToolError(f"unknown tool {tool}")
    prefix, pos, params, lane, _ = TOOLS[tool]
    argv = list(prefix)
    for p in pos:
        if p not in args:
            raise ToolError(f"missing {p}")
        if p == "job" and not JOB.match(str(args[p])):
            raise ToolError("job: bad id")
        argv.append(_val(p, args[p], "int" if p == "clip" else "str", None))
    for k, v in args.items():
        if k in pos:
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
    props = {p: {"type": "integer" if p == "clip" else "string"} for p in pos}
    for k, (typ, enum) in params.items():
        props[k] = {"type": t.get(typ, "string"), **({"enum": enum} if isinstance(enum, list) else {})}
    return {"name": tool, "description": desc + (" (queued: poll get_task)" if lane != "sync" else ""),
            "inputSchema": {"type": "object", "properties": props, "required": pos}}


# ---- persistent queue ----------------------------------------------------------------------------------------
_lock = threading.Lock()


def _db():
    ROOT.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(ROOT / "queue.db", timeout=30)
    c.execute("create table if not exists tasks(id integer primary key, argv text, lane text, status text, rc integer, ts real)")
    return c


def submit(argv, lane):
    with _lock, _db() as c:
        tid = c.execute("insert into tasks(argv,lane,status,ts) values(?,?,?,?)", (json.dumps(argv), lane, "queued", time.time())).lastrowid
    if lane == "live":
        threading.Thread(target=_exec, args=(tid, argv), daemon=True).start()
    return tid


def _exec(tid, argv):
    with _lock, _db() as c:
        c.execute("update tasks set status='running' where id=?", (tid,))
    (ROOT / "tasks").mkdir(parents=True, exist_ok=True)
    with open(ROOT / "tasks" / f"{tid}.log", "ab") as log:
        rc = subprocess.run([sys.executable, "-m", "clipper", *argv], stdout=log, stderr=subprocess.STDOUT).returncode
    with _lock, _db() as c:
        c.execute("update tasks set status=?, rc=? where id=?", ("done" if rc == 0 else "failed", rc, tid))


def worker(stop=None):
    """Single worker: one heavy job at a time (GPU/CPU contention). Interrupted tasks are re-queued on start.
    ponytail: assumes one server per jobs dir (a second server would re-queue the first one's running task)."""
    with _lock, _db() as c:
        c.execute("update tasks set status='queued' where status='running' and lane='work'")
    while not (stop and stop.is_set()):
        with _lock, _db() as c:
            row = c.execute("select id, argv from tasks where status='queued' and lane='work' order by id limit 1").fetchone()
        with _lock, _db() as c:  # atomic claim so two servers never run the same task
            got = row and c.execute("update tasks set status='running' where id=? and status='queued'", (row[0],)).rowcount
        if got:
            _exec(row[0], json.loads(row[1]))
        else:
            time.sleep(1)


def get_task(tid):
    with _lock, _db() as c:
        r = c.execute("select status, rc, argv from tasks where id=?", (int(tid),)).fetchone()
    if not r:
        raise ToolError("no such task")
    f = ROOT / "tasks" / f"{tid}.log"
    return {"task": int(tid), "status": r[0], "returncode": r[1], "argv": json.loads(r[2]), "log_tail": f.read_text(errors="ignore")[-1500:] if f.exists() else ""}


def call_tool(name, args):
    """-> JSON-serialisable result. Raises ToolError on bad input."""
    if name == "get_task":
        return get_task(args.get("task", -1))
    argv, lane = build_argv(name, args or {})
    if lane == "sync":
        r = subprocess.run([sys.executable, "-m", "clipper", *argv], capture_output=True, text=True, timeout=600)
        out = r.stdout.strip()
        if r.returncode:
            raise ToolError((r.stderr or out)[-800:])
        try:
            return json.loads(out)
        except json.JSONDecodeError:
            return {"output": out}
    res = {"task": submit(argv, lane), "queued": " ".join(argv)}
    if name == "clip_video":
        res["job"] = digest(args["src"])[:10]  # same id the pipeline will use
    return res


TOOLS_LIST = [schema(t) for t in TOOLS] + [{"name": "get_task", "description": "Status + log tail of a queued task.",
              "inputSchema": {"type": "object", "properties": {"task": {"type": "integer"}}, "required": ["task"]}}]


# ---- MCP (stdio, JSON-RPC 2.0, newline-delimited) ---------------------------------------------------------------
def mcp():
    threading.Thread(target=worker, daemon=True).start()
    for line in sys.stdin:
        if not line.strip():
            continue
        m = json.loads(line)
        mid, meth = m.get("id"), m.get("method", "")
        if mid is None:  # notification
            continue
        if meth == "initialize":
            res = {"protocolVersion": m["params"].get("protocolVersion", "2024-11-05"), "capabilities": {"tools": {}},
                   "serverInfo": {"name": "clipper", "version": "0.2.0"}}
        elif meth == "tools/list":
            res = {"tools": TOOLS_LIST}
        elif meth == "tools/call":
            try:
                res = {"content": [{"type": "text", "text": json.dumps(call_tool(m["params"]["name"], m["params"].get("arguments", {})), indent=1)}]}
            except (ToolError, subprocess.TimeoutExpired) as e:
                res = {"isError": True, "content": [{"type": "text", "text": str(e)}]}
        elif meth == "ping":
            res = {}
        else:
            print(json.dumps({"jsonrpc": "2.0", "id": mid, "error": {"code": -32601, "message": "method not found"}}), flush=True)
            continue
        print(json.dumps({"jsonrpc": "2.0", "id": mid, "result": res}), flush=True)


# ---- HTTP (localhost only, bearer token) ---------------------------------------------------------------------------
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
            ok = secrets.compare_digest(self.headers.get("Authorization", ""), f"Bearer {token}") and \
                self.headers.get("Host", "") in (f"127.0.0.1:{port}", f"localhost:{port}")
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
                self._send(400, {"error": str(e)})

        def log_message(self, *a):
            pass
    print(f"clipper HTTP on http://127.0.0.1:{port}  token: {token}", flush=True)
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
