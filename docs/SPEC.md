# clipper: design, QC and roadmap

## What we took from the references
| source | taken | where |
|---|---|---|
| Clips Studio (AGPL, reimplemented, no code copied) | multimodal fusion, chat + audio signals, per-genre weights, replay chat | `score.py`, `signals.py`, `PROFILES` |
| Chopify (MIT) | overlapping resumable transcription chunks, cheap candidate pass before expensive work, draft previews | `transcribe.py`, `render --draft` |
| AutoClip (MIT) | LLM picks word indices, code looks up timestamps, per-stage artifacts, render QC | `boundaries.py`, `llm.py`, `job.py`, `render.qc` |
| reel-studio (this repo's zip) | `next` loop for cheap agents, stage artifacts + gates, `doctor`/`selftest`, learnings file, critics that read real output with named fix targets, "motivated camera only" | `cli.py`, `RUNBOOK.md`, QC table below |

## Pipeline (each stage = one artifact, skipped if present)
ingest `source.json` > transcribe `transcript.json` > signals `signals.json` > candidates `candidates.json` > select `plan.json` > render `render.json` > qc `report.json`.
Live: `monitor` writes `record.ts`, `chat.jsonl`, `events.json`, `prelim_*.mkv`; final selection re-runs the VOD pipeline on the recording with chat.

## Scoring
Per-second series (speech lexicon/rate, audio RMS, scene cuts, chat rate + unique users) are smoothed (20 s), percentile-ranked and fused with profile weights renormalised over the signals that exist. Peaks (min 25 s apart) become candidates, refined to sentence boundaries, plus 10% hook score, de-duplicated by overlap (IoU >= 0.3).
Chat spikes use rolling median + 6 x MAD, so there is no fixed threshold.

## LLM cost policy
`disabled` none | `local_only` Ollama | `smart` OpenAI only for candidates within 0.12 of the cut-off score | `quality` all shortlisted. Batches of 8, JSON out, cached by prompt hash, hard caps `--max-calls 6` / 60k tokens, cost reported in `plan.json`. No key = local scoring, never an error. Price constants in `llm.PRICE`: verify before trusting the USD figure.

## QC issue codes and fixes
| code | fix |
|---|---|
| starts_mid_sentence / ends_mid_sentence | `edit --start-shift -N` / `--end-shift +N` (N = the missing seconds) |
| truncates_word | shift 0.3 s outward |
| too_long / too_short | shift the end; or `--drop` |
| dead_air | trim `--start-shift`/`--end-shift`, or drop |
| bad_size, decode_error, no_audio, black_frames | `render` again; if it persists check source with `doctor` and report |
| silent_tail | `--end-shift -N` |
| render_failed | read `report.json` errors, fix the named input |

## Roadmap
### Phase 1 (done)
Scene-aware per-shot crop (never animates across a cut), motivated punch-in zoom (max one automatic), lanczos+sharpen and `low_res_source` warning, persistent SQLite task queue, MCP (stdio) and HTTP (localhost, token, Host check, argument validation) wrappers, `bench` golden-set scorer. Earlier: ingest, chunked resumable Whisper, signal fusion, boundary engine, LLM judge with budget/cache, 4 caption presets, fill/fit layouts, loudness, QC, edit ops, live buffer + Twitch chat/audio spikes.

### Phase 2 (done, with honest verification status)
| item | status | verified here |
|---|---|---|
| Editor-critic (cut, story, pace, captions, framing, frames, audio, vision; Murch-weighted verdict; `critique` / `fix` / `polish`) | built | yes, on bad fixtures and real renders |
| Live Whisper on the rolling buffer (speech spikes join chat/audio events) | built | plumbing with a stand-in model; real faster-whisper on a live stream: no |
| YouTube live chat (official Data API v3, your own API key, public chats need no OAuth) | built | with a fake API; real key: no |
| Chat relay file (`--chat-file`) for Kick or anything else | built | yes |
| PANNs sound events, RapidOCR on-screen text, motion series | built | motion: yes. PANNs/RapidOCR: scoring helpers only, models not downloaded here |
| YOLO/Haar people tracking: per-shot, smoothed, in-shot glide, never across a cut | built | maths + ffmpeg expressions yes; detectors no |
| Two-person split layout (both always visible) | built | render yes; detection no |
| AI upscaling via Real-ESRGAN ncnn-vulkan for low-res sources | built | plumbing with a stand-in binary; real binary/GPU: no |
| B-roll by keyword, ducked user music, audio cleanup | built | yes |
| Golden-set benchmark | scorer yes; no real videos shipped (you supply them: `clipper bench`) | scorer yes |

Not done, on purpose: TalkNet active-speaker following (needs model weights I cannot validate; the split layout keeps both people visible instead), a Kick chat *reader* (Kick's official API delivers chat by webhook to an OAuth app with a public URL; relay it into `--chat-file`), Video2X, RIFE, MediaMTX ingest.

Golden file format: `{"entries":[{"video":"a.mp4","transcript":"a.json","profile":"podcast","refs":[{"start":120,"end":151}]}]}`.
