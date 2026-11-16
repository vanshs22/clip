"""MCP protocol, HTTP API and security tests. The server is exercised through its real stdio/HTTP interfaces."""
import json, os, subprocess, sys, threading, time, urllib.error, urllib.request
from pathlib import Path
import pytest
from clipper import server, util

ROOT = Path(__file__).resolve().parents[1]


class MCP:
    def __init__(self, cwd, env=None):
        e = {**os.environ, "PYTHONPATH": str(ROOT), "CLIPPER_JOBS": str(cwd / "jobs"), **(env or {})}
        self.p = subprocess.Popen([sys.executable, "-m", "clipper", "mcp"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, cwd=cwd, env=e)
        self.n = 0
        self.notes = []

    def send(self, method, params=None, notify=False):
        m = {"jsonrpc": "2.0", "method": method, **({"params": params} if params is not None else {})}
        if not notify:
            self.n += 1
            m["id"] = self.n
        self.p.stdin.write(json.dumps(m) + "\n")
        self.p.stdin.flush()
        return self.n

    def recv(self, mid, timeout=60):
        t = time.time()
        while time.time() - t < timeout:
            line = self.p.stdout.readline()
            if not line:
                raise EOFError
            m = json.loads(line)
            if m.get("id") == mid:
                return m
            self.notes.append(m)
        raise TimeoutError

    def call(self, name, args=None, **kw):
        return self.recv(self.send("tools/call", {"name": name, "arguments": args or {}, **kw}))

    def close(self):
        self.p.kill()


@pytest.fixture()
def mcp(tmp_path):
    m = MCP(tmp_path)
    yield m
    m.close()


def text(r):
    return r["result"]["content"][0]["text"]


def test_mcp_initialize_list_ping_and_unknown_method(mcp):
    r = mcp.recv(mcp.send("initialize", {"protocolVersion": "2024-11-05", "capabilities": {}, "clientInfo": {"name": "t"}}))
    assert r["result"]["serverInfo"]["name"] == "clipper" and "tools" in r["result"]["capabilities"] and r["result"]["instructions"]
    mcp.send("notifications/initialized", notify=True)
    tools = mcp.recv(mcp.send("tools/list"))["result"]["tools"]
    names = {t["name"] for t in tools}
    assert {"clip_video", "monitor_livestream", "get_job_status", "get_clip_candidates", "review_clip", "edit_clip", "render_clips", "polish_clips", "export_clips",
            "get_task", "cancel_task", "get_system_capabilities"} <= names
    for t in tools:  # every tool has a description, typed properties and forbids unknown arguments
        assert t["description"] and t["inputSchema"]["type"] == "object" and t["inputSchema"]["additionalProperties"] is False
    assert mcp.recv(mcp.send("ping"))["result"] == {}
    assert mcp.recv(mcp.send("nonsense/method"))["error"]["code"] == -32601


def test_mcp_validates_arguments_and_never_crashes(mcp):
    bad = [("clip_video", {"src": "-rf"}), ("clip_video", {"src": "file:///etc/passwd"}), ("clip_video", {"src": "x.mp4", "style": "nope"}), ("clip_video", {"src": "x.mp4", "evil": 1}),
           ("clip_video", {"src": "x.mp4", "clips": "ten"}), ("clip_video", {}), ("edit_clip", {"job": "../../x", "clip": 1}), ("get_job_status", {"job": "zzzz"}),
           ("clip_video", {"src": "x.mp4", "chat": "../../../etc/passwd"}), ("clip_video", {"src": "x.mp4", "max_usd": float("nan")}), ("no_such_tool", {})]
    for name, args in bad:
        r = mcp.call(name, args)
        assert r["result"]["isError"], (name, args)
    assert mcp.recv(mcp.send("ping"))["result"] == {}  # still alive


def test_mcp_does_not_block_on_slow_tools_and_task_lifecycle_works(mcp, tmp_path):
    (tmp_path / "x.mp4").write_bytes(b"not a video")
    r = mcp.call("clip_video", {"src": "x.mp4", "mode": "disabled"}, _meta={"progressToken": "tok1"})
    t = json.loads(text(r))
    assert "task" in t and "job" in t
    t0 = time.time()
    assert mcp.recv(mcp.send("ping"))["result"] == {} and time.time() - t0 < 2  # the loop answered while the task is queued/running
    for _ in range(40):
        st = json.loads(text(mcp.call("get_task", {"task": t["task"]})))
        if st["status"] in ("failed", "done", "cancelled"):
            break
        time.sleep(0.5)
    assert st["status"] == "failed" and st["log_tail"]  # garbage input fails cleanly with a log, the server survives
    assert mcp.recv(mcp.send("ping"))["result"] == {}


def test_cancel_task_really_kills_the_running_process_tree(tmp_path, monkeypatch):
    """Queue a REAL long-running child (a python sleeper that spawns its own child), prove it runs, cancel it, prove the OS processes are gone."""
    monkeypatch.setattr(server, "ROOT", tmp_path / "q")
    sleeper = tmp_path / "sleeper.py"
    sleeper.write_text("import subprocess, sys, time\nc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\nopen(sys.argv[1], 'w').write(str(c.pid))\ntime.sleep(120)\n")
    pidfile = tmp_path / "child.pid"
    real_popen = subprocess.Popen

    def popen(cmd, **kw):  # run our sleeper instead of `python -m clipper ...`, with the REAL process-group/creation flags the server sets
        return real_popen([sys.executable, str(sleeper), str(pidfile)], **kw)
    monkeypatch.setattr(server.subprocess, "Popen", popen)
    tid = server.submit(["run", "x.mp4"], "live")
    for _ in range(60):
        if pidfile.exists() and server.get_task(tid)["status"] == "running":
            break
        time.sleep(0.2)
    child = int(pidfile.read_text())
    with server._lock, server._db() as c:
        parent = c.execute("select pid from tasks where id=?", (tid,)).fetchone()[0]
    assert util.pid_alive(parent) and util.pid_alive(child), "both the task and its child must be running before the cancel"
    out = server.cancel_task(tid)
    assert out["status"] == "cancelled"
    for _ in range(40):
        if not util.pid_alive(child) and not util.pid_alive(parent):
            break
        time.sleep(0.2)
    assert not util.pid_alive(parent) and not util.pid_alive(child), "cancel must kill the whole process group, not just the parent"
    assert server.cancel_task(tid)["note"] == "already finished" or server.get_task(tid)["status"] == "cancelled"


def test_mcp_get_system_capabilities_never_exposes_secret_values(mcp):
    r = mcp.call("get_system_capabilities", {})
    body = text(r)
    caps = json.loads(body)
    assert caps["ffmpeg"] is True and "features" in caps and isinstance(caps["openai_key"], bool)
    m2 = MCP(Path(os.getcwd()), env={"OPENAI_API_KEY": "sk-SECRETSECRETSECRET123456", "YOUTUBE_API_KEY": "AIzaSECRETSECRETSECRETSECRET123"})
    try:
        out = text(m2.call("get_system_capabilities", {}))
    finally:
        m2.close()
    assert "SECRET" not in out and json.loads(out)["openai_key"] is True


def test_http_requires_token_and_host_and_validates(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "ROOT", tmp_path / "h")
    monkeypatch.setattr(server, "worker", lambda stop=None: None)
    port = 18999
    threading.Thread(target=server.http, args=(port, "tok123"), daemon=True).start()
    time.sleep(1.0)

    def req(path, hdr=None, data=None):
        r = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, headers=hdr or {})
        try:
            return urllib.request.urlopen(r).status
        except urllib.error.HTTPError as e:
            return e.code
    A = {"Authorization": "Bearer tok123"}
    assert req("/tools") == 401 and req("/tools", {**A, "Host": "evil.com"}) == 401 and req("/tools", {"Authorization": "Bearer nope"}) == 401
    assert req("/tools", A) == 200
    assert req("/tools/clip_video", A, b'{"src":"-rf"}') == 400 and req("/tools/clip_video", A, b"not json") == 400 and req("/tools/clip_video", A, b"x" * 70000) in (413, 400)


def test_http_server_binds_loopback_only():
    import inspect
    assert '("127.0.0.1", port)' in inspect.getsource(server.http)


def test_queue_recovery_requeues_only_dead_workers_and_runs_one_heavy_task(tmp_path, monkeypatch):
    monkeypatch.setattr(server, "ROOT", tmp_path / "q")
    with server._lock, server._db() as c:
        c.execute("insert into tasks(argv,lane,status,owner,pid,ts) values('[\"a\"]','work','running',99999999,1,0)")   # worker died
        c.execute("insert into tasks(argv,lane,status,owner,pid,ts) values('[\"b\"]','work','running',?,1,0)", (os.getppid(),))  # another LIVE server owns it
    server.recover()
    with server._lock, server._db() as c:
        st = dict(c.execute("select argv, status from tasks").fetchall())
    assert st['["a"]'] == "queued" and st['["b"]'] == "running"
    assert server.MAX_HEAVY == 1


def test_kick_relay_binds_loopback_by_default():
    from clipper import cli
    import argparse
    p = argparse.ArgumentParser()
    src = (ROOT / "clipper" / "cli.py").read_text()
    assert 's.add_argument("--bind", default="127.0.0.1", help="expose via a tunnel' in src
