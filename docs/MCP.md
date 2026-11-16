# MCP integration (stdio, JSON-RPC 2.0, protocol 2024-11-05)

## Claude Code
```bash
claude mcp add clipper -- /path/to/clip/.venv/bin/clipper mcp
```
or in `.mcp.json`:
```json
{ "mcpServers": { "clipper": { "command": "/path/to/clip/.venv/bin/clipper", "args": ["mcp"], "env": { "CLIPPER_JOBS": "/path/to/jobs" } } } }
```
Windows: `"command": "C:\\path\\to\\clip\\.venv\\Scripts\\clipper.exe"`.

## Any other MCP client (Cursor, Cline, Continue, a custom SDK client)
Run the same command and arguments over stdio.

## Tools
`clip_video` (one-shot recorded video), `monitor_livestream` (one-shot live stream), `get_job_status`, `get_clip_candidates`, `review_clip`, `edit_clip`, `render_clips`, `polish_clips`, `export_clips`, `get_task`, `cancel_task`, `get_system_capabilities`.

All schemas forbid unknown arguments. Paths must be inside the working directory, and `src` is validated (no leading `-`, no `file:`, no credentials in URLs). Long tools return immediately with a task id and never block the server. Pass `_meta.progressToken` to receive `notifications/progress`. `notifications/cancelled` is honoured and `cancel_task` kills the process tree.

Example: the user says "Find the best 8 clips of this stream and make them punchy". The agent calls `monitor_livestream {src, clips: 8, style: "bold_pop"}`, polls `get_task`, reads `get_job_status` and the export folder, views frames if it can, and reports.

Protocol check from a shell:
```bash
printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05"}}' '{"jsonrpc":"2.0","id":2,"method":"tools/list"}' | clipper mcp
```
