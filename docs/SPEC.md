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

## Roadmap (honest status)
Done: ingest (yt-dlp/file/HLS), chunked resumable Whisper, signal fusion, boundary engine, LLM judge with budget/cache, 4 caption presets, fill/fit layouts, static face-centred crop (OpenCV optional), loudness -14 LUFS, QC, edit ops, live buffer + chat/audio spikes + preliminary clips (Twitch chat).
Not yet: live Whisper, YouTube/Kick live chat, PANNs/YOLO/TalkNet/RapidOCR signals, animated zoom/scene-aware crop, Real-ESRGAN/Video2X, MCP + HTTP wrapper, persistent worker queue, golden-video benchmark set. Each plugs in as a new series in `signals.py` or a filter in `render.vfilter`.
