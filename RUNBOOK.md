# Runbook (one page)
`clipper` = `python -m clipper` if not installed. Jobs: `./clipper-jobs/<id>` (`CLIPPER_JOBS`). Read `SKILL.md` for the decision table. Human setup walkthrough: `SETUP.md`.

1. First time: `clipper doctor` (what works here), then `clipper selftest` (offline tests; `--live` adds the RTMP end-to-end tests). Read `memory/learnings.md`.
2. Recorded video: `clipper run SRC --clips N --export out/` — one command, everything included.
3. Live stream: `clipper watch SRC --clips N --out live/` — one command; if the machine or process dies, `clipper watch SRC --out live/ --resume` finishes the job from what was recorded.
4. Read the printed JSON. Look at frames if you can (`critique --packet`). Edit with `clipper edit`, re-`render`, `export`.
5. Stage-level control when needed: `clipper process SRC --stop-after select` then `status`, `next JOB`, `edit`, `render`, `polish`, `export`.

Rules that matter most
1. Never claim a clip is good without opening a frame; if you cannot, say the vision check was not performed.
2. Fix with `clipper edit`, not by editing JSON or code.
3. Re-running the same command is safe and cheap: every stage is cached by the hash of its inputs, so a changed option invalidates exactly what depends on it (a new caption style re-renders only; a new clip count re-selects and re-renders; transcription is reused).
4. A model or optional package missing is never fatal: the report lists `features_skipped`. Install what you want with `python scripts/install.py --with speaker,audio,ocr` and `clipper models fetch all --yes`.
5. Budget: OpenAI use is capped per job (`--max-usd`, default 0.10) and shared between the live session and the final pass. The job's JSON reports exact calls/tokens/USD.
