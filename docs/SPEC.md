> **Superseded for current status by [VALIDATION.md](VALIDATION.md)** (release 0.4.0). This file keeps the earlier research notes and measurements (speaker detection, upscaler benchmark, RIFE, MediaMTX, Kick).

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

### Validation on real footage (phase 3)
Sources: two 3-minute slices of a public-domain US-government panel (two people on stage, audience cutaways, AV1) and a public-domain NASA ISS interview
(720p60). Transcripts by faster-whisper `small`, labels from reading the transcript and the contact sheets. The YouTube link you sent could not be fetched from
this sandbox (YouTube answered with a sign-in/bot check; the tool correctly reported `auth_required`, and I did not try to get around it: pass your own `--cookies`).

| What | Result |
|---|---|
| Real pipeline (ingest, Whisper, signals, select, render, QC, polish) on real video | runs; bugs found and fixed: OpenCV 5 dropped `CascadeClassifier` (face detector replaced by YuNet ONNX), numpy floats broke `jsave`, `-c copy` slices misalign video and transcript, a clip planned past the media end, words selected by end time pulled in the next sentence's first word |
| **Active speaker** (voice d-vectors via resemblyzer + mouth motion; TalkNet is NOT used) | whole-video map: **100% (154/154)** and **99% (141/142)** of speech windows put on the correct person on two 3-minute scenes; the reliability gate (k=2 silhouette >= 0.3 and smaller cluster >= 20%) accepts both and **refuses all 6** single-speaker stretches. Thresholds come from only these 8 cases of one event: treat as calibrated, not proven |
| Why the design pools the whole video | clip-length spans scored 46-85% with every confidence signal I tried (cluster separation, split-half agreement, permutation test, MFCC features): none separated real from single-speaker cases. Only neural speaker embeddings did (silhouette 0.47-0.54 for two speakers vs 0.07-0.20 for one) |
| Framing | crop follows the speaker with a min dwell, never moves across a scene cut; fixed an off-centre crop bug (face x was used as crop offset); checked frames by eye |
| PANNs sound events | model downloaded and run on real audio: tags audience chuckling after a joke ("Chuckle, chortle" was missing from my label set; added) |
| RapidOCR | reads on-screen text from real frames ("WH.GOV") |
| YOLO | ran, found both stage people, but is not needed (YuNet does the work): dependency removed |
| Live: MediaMTX + real RTMP push + `monitor` + chat relay | recording, event detection, preliminary clip all work. A loud beep in the stream and a chat burst at the same instant showed **+7 s chat-vs-video skew**; `clipper sync-check` measured it, feeding it back as `--latency 7` gave **0 s**. Monitor open-retry and stream-clock offset (median of wall-minus-media) fixed along the way |
| Kick | official webhook receiver with RSA signature verification (stdlib), checked against an independent signer and against Kick's REAL public key (RSA-2048 parsed). Kick delivering a live event to a public URL: NOT tested (needs your registered app). The unofficial Pusher socket was not shipped: its app key was rejected |
| AI upscaling | Real-ESRGAN ncnn ran on CPU through a software Vulkan driver. **`realesr-animevideov3` redraws real faces as cartoons; `realesrgan-x4plus` produced scrambled tiles there.** AI upscaling is therefore opt-in (`--upscale ai`), default is lanczos + sharpen with a `low_res_source` warning. 562 s for 4 s of 360p on CPU: needs a GPU in practice |
| RIFE | ran on real footage: clean in-between frames on a talking head (modest motion), 24 -> 48 fps, duration and audio preserved. Opt-in (`edit --interpolate`) |
| Video2X | not integrated: its release is a wrapper around the same ncnn models, so it adds no capability; I did not run it |
| Golden set (`golden/`, run `golden/fetch.sh` then `clipper bench golden/golden.json`) | 6 human-labelled reference clips. Local-only selection: recall 1.0 and 0.67, mean start error 5.6 s and 6.6 s. Known misses: the campus free-speech answer was never a candidate; "vocational school" clip starts after its setup sentence. An LLM pass (`--mode quality`) is untested here (no key) |
| Editor-critic on the real plan | found real problems: dependent openings, mid-clip dead air, a false "ends mid-thought" (a renderer/critic bug that I fixed) |

### Round 4: fixing the limits you named (all measured on real footage)
**Speaker thresholds (was: 8 cases, one event).** Now 4 two-person scenes from 4 different events (government panel x2, convention-floor interview, a Chomsky conversation in an office) plus 9 negatives (8 single-speaker stretches, 1 still photo with a real podcast audio track).
With the first voice model (resemblyzer) the old 0.3 silhouette gate was fragile: one-speaker stretches reached 0.45 and two-speaker scenes were as low as 0.35, so it would have passed a false positive. Replacing the voice model with SpeechBrain ECAPA-TDNN (free, Apache-2.0) fixed it:
two-speaker silhouettes 0.43-0.54 (purity 98-100%), one-speaker 0.06-0.16 (one 0.54 outlier and the photo case at 0.50 are refused by the smaller-cluster share rule, 8% and 15%, so the share rule is load-bearing, not decoration).
Result: gate accepts 4/4 real scenes at 99.6% pooled accuracy and refuses 9/9 negatives. `python -m tests.validate_real speaker REF_DIR` reproduces it. Still a modest sample (4 scenes, 1 language of speech, adult speakers, English): more scenes would tighten the margins.

**"Clip-length spans don't work" (was: 46-85%).** The voice-to-face mapping is a property of the whole video, but it does not need the whole video: estimated from a RANDOM 15% of the speech it was already correct for 3 of 4 scenes (98/65/95/100%), 100% from half. Per-clip estimation from a clip's own few seconds is still not reliable; the map is built once per job (`speakers` stage).

**TalkNet.** Its successor Light-ASD (MIT, 1M parameters, weights in its repo) runs on CPU and I validated it on the same scenes: 91% / 84% per second on its own, 100% / 89% when only its confident half is trusted. It is accurate but slow (6.5 min per 3 min of video on this CPU) and adds nothing over the voice+mouth map that already reaches 99-100%, so I did NOT ship it: no dead code. If you need it later it is a drop-in for the mouth-motion cue.

**Upscaling without a GPU.** Benchmarked on 9 real 720p frames (input = 2x downscale, truth = the original): bicubic 33.23 dB, lanczos 33.63, lanczos+unsharp 33.86 (current default), ESPCN 34.08, FSRCNN 34.17 (~100 ms/frame), LapSRN 33.74, EDSR 35.28 (34 s/frame). Viewing the faces: no method turns real faces into cartoons (unlike the anime Real-ESRGAN model), but the neural CPU models are visually indistinguishable from lanczos+unsharp. The one clearly better model (EDSR, +1.4 dB over the default) is unusable without a GPU. Decision: no new upscaler; lanczos+sharpen stays the default and `--upscale ai` stays opt-in. With a GPU, `realesrgan-x4plus` is the model to try, and it still needs a look at a frame first.

### Still not done
TalkNet itself (replaced by the method above), a proven Kick live delivery, Video2X, AI upscaling that is safe for real faces, OpenAI judging on real data, YOLO-based tracking (removed), a golden set beyond one event.

### Earlier phase-2 table (original plan status)

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
