# HTTP API (loopback only)

Start: `clipper serve --port 8765`. It prints a bearer token (or set `CLIPPER_TOKEN`). Every request needs `Authorization: Bearer <token>` and a `Host` header of `127.0.0.1:<port>` or `localhost:<port>` (DNS-rebinding guard). Bodies are limited to 64 KB. The server binds 127.0.0.1 only.

* `GET /tools`: the tool schemas (same as MCP).
* `GET /tasks/<id>`: `{status: queued|running|done|failed|cancelled, returncode, argv, log_tail}`.
* `POST /tools/<name>` with a JSON object of arguments. Tools: `clip_video`, `monitor_livestream`, `get_job_status`, `get_clip_candidates`, `review_clip`, `edit_clip`, `render_clips`, `polish_clips`, `export_clips`, `get_system_capabilities`, `get_task`, `cancel_task`.

Slow tools return `{"task": 12, "job": "ab12cd34ef"}`; poll `GET /tasks/12`. Errors: 400 with `{"error": ...}` (validation), 401 (auth or Host), 413 (too large).

```bash
curl -s -H "Authorization: Bearer $TOKEN" -d '{"src":"talk.mp4","clips":5,"mode":"disabled"}' http://127.0.0.1:8765/tools/clip_video
```
