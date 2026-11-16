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
3. Edits (only that clip re-renders): `clipper edit JOB 3 --start-shift -3`, `--end-shift 2`, `--captions karaoke`, `--caption-scale 0.8`, `--zoom 1.15`, `--layout fit`, `--focus-x 0.3`, `--drop`.
4. `clipper render JOB --draft` (540x960 quick look), then `clipper render JOB --no-draft`.
5. Every clip must show `OK`. A `FIX` lists issue codes: see `docs/SPEC.md` section QC for the fix for each. Max 3 fix rounds, then tell the user what is left.
6. `clipper export JOB --out DIR`: mp4 + `metadata.json` (titles, times, cost).

## Rules
- Boundaries: completeness beats length. The LLM returns word indices; timestamps come from real word timings. Never hand-pick timestamps except via `--start-shift/--end-shift`.
- Camera work is motivated: no timer-based zooms or cuts. `--zoom` only for a named punchline.
- Say what you did not verify. If you cannot view images, say so. Never write "looks good" without opening a frame.
- Never upload or publish. Never send media to any API: only compact transcript text goes to the LLM.
- Chat spikes are candidates, not proof. Report API cost (`status` shows calls/tokens/USD).
- Not implemented yet (say so if asked): live speech transcription, YOLO/TalkNet speaker tracking, Real-ESRGAN upscaling, Kick/YouTube live-chat adapters, MCP/HTTP wrappers. See `docs/SPEC.md` roadmap.
- Append lessons to `memory/learnings.md` after each job.
