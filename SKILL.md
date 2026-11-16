---
name: clipper
description: Turn a long video, VOD or livestream (YouTube, Twitch, Kick, direct file, HLS/RTMP/OBS) into finished 1080x1920 Shorts/Reels/TikToks in ONE command. Local-first highlight detection, word-exact clip boundaries, animated captions, FFmpeg rendering, automatic editor-critic polish, QC and export. Use when the user asks to clip a video, find highlights, make shorts, or clip a live stream.
---

# clipper

**Do the whole job. Do not stop after candidates to ask whether to continue.** Pick sensible defaults, run, check, fix, export, report honestly.

## 1. Pick the command from what the user gave you
| input | run |
|---|---|
| YouTube / Twitch / Kick / direct URL, or a local file | `clipper run "<src>" --clips N --export out/` |
| a LIVE URL (YouTube/Twitch/Kick live, HLS, RTMP) | `clipper watch "<src>" --clips N --out live/` (records, flags moments, finalizes by itself when the stream ends, then processes, polishes and exports to `live/export/`) |
| OBS / your own encoder | `clipper ingest-server` (prints an RTMP URL to push to), then `clipper watch rtmp://127.0.0.1:1935/live/KEY --out live/` |
| an MCP-capable agent | tools `clip_video` (= run) and `monitor_livestream` (= watch): they return a task id, poll `get_task` |
Unknown URL type? `clipper run` classifies it (YouTube/Twitch/Kick/direct/HLS/RTMP/local) and tells you if it is live, unavailable, or needs login.
Needs a login/age-gate? Ask the user for THEIR cookies file and pass `--cookies cookies.txt`. Never bypass DRM or logins.

## 2. Choose options only when the request implies them (otherwise leave the defaults)
`--profile podcast|interview|gaming|irl|comedy|sports` (default: auto-detected) - `--style bold_pop|karaoke|clean_lower|boxed|minimal_white|highlight_words|podcast_speaker|gaming_bold|cinematic|custom` (`clipper styles --preview sheet.png` shows them) - `--quality draft|normal|high` - `--mode disabled|local_only|smart|quality` (`smart` default: OpenAI if `OPENAI_API_KEY` is set, else local Ollama if running, else local scoring; hard cap `--max-usd`, default 0.10) - `--transcript words.json`, `--chat chat.jsonl`, `--logo file.png`, `--style-file my.json`.
Offline machine: `CLIPPER_OFFLINE=1` (features whose models are missing are skipped and reported, nothing crashes).

## 3. After it finishes (it already polished and exported)
1. Read the JSON it printed: `clips_passing/clips_total`, per-clip `verdict`, `openai` (calls, tokens, USD), `features_skipped`.
2. **Look at the clips if your environment can view images**: `clipper critique JOB --packet`, open every sheet, then `clipper critique JOB --submit findings.json` (read `skills/editor-critic/SKILL.md`). If you cannot view images say so: run `clipper critique JOB --unavailable "<why>"`. The verdict then says EMOTION WAS NOT ASSESSED. Never write "looks good" unseen.
3. Fix with structured edits, not by hand: `clipper edit JOB 3 --start-shift -2 | --end-shift 1.5 | --captions karaoke | --zoom-at 12.5 | --layout fit | --drop ...`, then `clipper render JOB`, `clipper export JOB --out out/`. Only the touched clip re-renders.
4. Tell the user where the files are and what was NOT done: skipped features, unviewed frames, warnings (`low_res_source`, `frozen_picture`), unverified platforms.

## 4. Rules
- Completeness beats length: clips start on a complete thought and end after the payoff; the LLM only returns word indices inside each candidate's own window.
- Camera work is motivated: crops change only at scene cuts or when the speaker changes; at most one automatic zoom. Speaker following runs only when its reliability gate passes.
- Chat spikes and loud audio are evidence, never proof. Say what a clip was chosen for (`status` shows the signals).
- Never upload/publish anything. Only compact transcript text goes to an LLM; never media. Never print or store API keys.
- AI upscaling (`--upscale ai`) and interpolation (`edit --interpolate`) are opt-in: the default AI model redraws real faces as cartoons. Default is lanczos + sharpen with a `low_res_source` warning.
- Platform honesty: Twitch live chat (IRC) and YouTube live chat (official API with the user's `YOUTUBE_API_KEY`) are implemented; Kick needs the user's own app + public HTTPS URL (`clipper kick-relay`) or `--chat-file`. Real-platform connections are **unverified in this repo's tests** (see docs/SPEC.md validation table).
- Diagnose: `clipper doctor` (human) or `clipper doctor --json` / MCP `get_system_capabilities`. Resume anything interrupted by re-running the same command (`watch ... --resume`).
