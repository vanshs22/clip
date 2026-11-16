---
name: clipper
description: Turn a long video, VOD or livestream (YouTube, Twitch, Kick, direct file/HLS/RTMP) into finished 1080x1920 Shorts/Reels/TikToks. Local-first highlight detection, exact word-level clip boundaries, animated captions, FFmpeg rendering, QC and structured edits. Use when the user asks to clip, find highlights, make shorts, or monitor a stream.
---

# clipper

Short version for any agent: read `RUNBOOK.md`, then loop on `clipper next JOB`.
No paid API is needed. `OPENAI_API_KEY` is optional and only used on borderline candidates.

## Setup (once)
`pip install -e ".[all]"` then `clipper doctor` (lists what is missing and the fix) then `clipper selftest` (must print "All checks passed").
Tell the user plainly what `doctor` reports missing. Do not work around it silently.

## Request -> command
| user says | run |
|---|---|
| "best 10 clips from this video" | `clipper process URL --clips 10 --stop-after select` |
| funny / reactions | `--profile comedy` (podcast, interview, gaming, irl, sports, auto) |
| caption style | `--style bold_pop\|karaoke\|clean_lower\|boxed\|none` |
| no OpenAI / offline | `--mode disabled` (or `local_only` with Ollama) |
| has chat file | `--chat chat.jsonl` (also TwitchDownloader JSON, YouTube live_chat) |
| already has transcript | `--transcript words.json` |
| the stream is live now | `clipper monitor URL --out live-out`, then `clipper process live-out/record.ts --chat live-out/chat.jsonl` |
| login required | ask the user for THEIR cookies file: `--cookies cookies.txt`. Never bypass DRM or logins. |

## Workflow
1. `process ... --stop-after select`: download, transcribe, detect, rank. Writes `plan.json`.
2. `clipper status JOB`: show the user each clip (time, title, text, issues, LLM cost). One question: keep / change / drop?
3. Edits (only that clip re-renders): `clipper edit JOB 3 --start-shift -3`, `--end-shift 2`, `--captions karaoke`, `--caption-scale 0.8`, `--zoom-at 12.5 --zoom-for 2 --zoom-amt 1.12` (punch-in on a named punchline; `--no-zooms` clears), `--zoom 1.15` (whole clip), `--layout fit`, `--focus-x 0.3`, `--broll-dir DIR` (files named after spoken keywords), `--music FILE --music-db -22` (your own track, ducked under speech), `--audio clean`, `--upscale off`, `--drop`.
4. `clipper render JOB --draft` (540x960 quick look), then `clipper render JOB --no-draft`.
5. `clipper polish JOB`: the editor-critic reviews the real renders, applies mechanical fixes once, re-renders (max 3 rounds). Then do the vision pass: `clipper critique JOB --packet`, OPEN every sheet, submit findings (see `skills/editor-critic/SKILL.md`). Anything left goes to the user with the critic's wording.
6. Every clip must show `OK` in QC. A `FIX` lists issue codes: see `docs/SPEC.md` section QC for the fix for each. Max 3 fix rounds, then tell the user what is left.
7. `clipper export JOB --out DIR`: mp4 + `metadata.json` (titles, times, cost).

## Rules
- Boundaries: completeness beats length. The LLM returns word indices; timestamps come from real word timings. Never hand-pick timestamps except via `--start-shift/--end-shift`.
- Camera work is motivated: no timer-based zooms or cuts. `--zoom` only for a named punchline.
- Say what you did not verify. If you cannot view images, say so. Never write "looks good" without opening a frame.
- Never upload or publish. Never send media to any API: only compact transcript text goes to the LLM.
- Chat spikes are candidates, not proof. Report API cost (`status` shows calls/tokens/USD).
- Reframing is per shot: the crop can change only at a scene cut. The plan may hold ONE auto punch-in on a strong clip; it is dropped if a cut is near. A `low_res_source` warning means the source is too small for clean 1080x1920: tell the user, do not hide it.
- Other agents: `clipper mcp` (MCP stdio: clip_video, get_job_status, get_clip_candidates, edit_clip, render_clips, export_clips, monitor_livestream, get_task) or `clipper serve` (HTTP on 127.0.0.1, bearer token printed at start). Slow tools return a task id; poll `get_task`.
- Measure changes with `clipper bench golden.json` (recall, boundary error, issue rate against reviewed timestamps).
- Live: `clipper monitor` uses Twitch chat automatically, YouTube chat with your own `YOUTUBE_API_KEY`, anything else (Kick) via `--chat-file relay.jsonl`; live speech transcription runs if faster-whisper is installed.
- Not implemented (say so if asked): TalkNet active-speaker following (two steady people get a stacked split layout showing both), a Kick chat reader, Video2X/RIFE. Unverified without model weights or a GPU: PANNs, YOLO, RapidOCR, Real-ESRGAN, live Whisper on a real stream. See `docs/SPEC.md`.
