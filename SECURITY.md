# Security policy

Report vulnerabilities privately to the repository owner (GitHub: Security > Report a vulnerability). Do not open public issues for exploitable bugs.

## Design (what is enforced, and tested in `tests/test_unit.py` / `tests/test_agents.py`)
- **No shell**: every subprocess call takes an argument list (`util.run` asserts it). User input never reaches a shell.
- **Inputs validated at the boundary**: sources must be a local path or `http(s)/rtmp(s)/rtsp/srt` URL without credentials; leading `-`, `file:`, `ftp:`, `data:`, control characters are rejected. yt-dlp is called with `--`.
- **Paths**: MCP/HTTP path arguments must stay inside the working directory. Archive extraction (MediaMTX) refuses `..`, absolute paths and links.
- **Secrets**: `OPENAI_API_KEY` (or `OPENAI_API_KEY_FILE`) is read from the environment only; it is never written to job JSON, logs, task output or reports (`util.redact` covers key shapes). The config file loader ignores `openai_api_key` entries.
- **HTTP API**: binds `127.0.0.1` only, bearer token (constant-time compare), `Host` header check against DNS rebinding, 64 KB body cap. It is never exposed publicly by default.
- **Kick relay**: RSA-SHA256 signature verified against Kick's *live* public key, 5-minute freshness window, duplicate-id suppression; binds `127.0.0.1` (put an HTTPS tunnel in front).
- **Budget**: a hard per-job cap on OpenAI calls, tokens and USD, reserved before each call.
- **Models**: downloaded over HTTPS with SHA-256 pinning, atomic replacement, corruption detection; `CLIPPER_OFFLINE=1` forbids downloads.
- **Resource limits**: one heavy task at a time by default (`CLIPPER_MAX_HEAVY`), disk-space checks, bounded memory in live mode, per-stage timeouts.

## Known limits
- Model checksums are trust-on-first-use against the upstream URL when they were pinned.
- The task queue trusts local processes sharing the jobs directory (same user).
- Windows: process-tree cancel uses `taskkill`; not exercised in CI yet.
