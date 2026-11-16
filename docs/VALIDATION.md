# Validation report (release 0.4.0)

Everything below was run on this release's code. "Verified" means executed here with the result shown. "Unverified" means implemented and tested only with mocks or constructed inputs.

## Automated tests (`pytest`, 104 tests, all passing)
| layer | file | tests | what it covers |
|---|---|---|---|
| unit | `tests/test_unit.py` | 75 | boundary engine on 8 hard cases (unpunctuated run, mid-sentence pause, delayed answer, delayed punchline + laughter tail, interrupting speakers, reaction after the statement, transcript ending at video end, sentence across a scene cut); chat spikes and spam/giveaway filtering; bounded detector memory; event-only candidates; LLM index-window validation, hard budget (reservation before the call, USD/token/call caps), retries with backoff, malformed JSON, key redaction, provider fallback; job identity and per-stage invalidation; model manager (resume, checksum, corruption, offline); 9 caption presets + custom; crop/zoom maths; encoder test-encode fallback; hostile-source rejection; path and archive traversal; Kick signature, replay, idempotency; `audio_rms` regression |
| integration + e2e | `tests/test_integration.py` | 15 | real ffmpeg rendering and QC, cache keys follow style/source/options, hardware-encoder failure falls back, logo/music/intro/outro, style previews, chunked transcription with a stand-in model (offsets, overlap, resume), full pipeline on synthetic media (complete boundaries, delayed answer, visual-only highlight), cache reuse and targeted invalidation, one-shot `run` and honest report, degraded run with no models, live extraction accuracy, shared live/final LLM budget |
| agents / security | `tests/test_agents.py` | 9 | MCP initialize/list/ping/unknown method, argument validation (11 hostile inputs), non-blocking tools, task lifecycle, **cancel kills a real process tree**, capabilities never leak secrets, HTTP auth/Host/size, loopback bind, queue recovery only for dead workers |
| live (real RTMP via MediaMTX) | `tests/test_live.py` | 4 | watch -> detect -> auto-finalize -> process -> export with no second command (states recording < finalizing < processing < done; preliminary clip cut mid-stream; chat+audio event); interrupted session resumed; stream drop and return reconnects and joins parts; CANCEL marker |

Run: `clipper selftest` (offline, 100 tests) and `clipper selftest --live` (adds the 4 RTMP tests). Full run: 104 passed in 6m20s (Python 3.11, Linux, CPU).

## Python versions (CPU-only, no optional packages)
3.10.20, 3.12.13, 3.13.13: unit suite 74/74 each (before one later test was added); integration + agents 24/24 on 3.10 and on 3.13. 3.11: the full 103.
**Windows and macOS: not run** (CI is defined in `.github/workflows/ci.yml`; it has never executed).

## Compilation / grammar
Every source file parses under the Python 3.10 grammar (`ast.parse(feature_version=(3,10))`). One real 3.10/3.11 bug was found this way (backslash inside an f-string expression in the installer) and fixed.

## Real-footage speaker validation (`python -m tests.validate_real speaker REF_DIR`)
Re-run on this release through the new model manager: 4 of 4 real two-person scenes accepted, pooled accuracy 99.6% (154/154, 141/142, 114/115, 127/127); 9 of 9 negatives refused (8 single-speaker stretches and a still photo with podcast audio). Small sample: four scenes of English speech from four events.

## Reproducible install
`requirements-lock.txt` installs into a clean venv and `pip check` is clean. The lock *alone* produced a broken OpenCV (GUI build wanting libGL); `scripts/install.py` repairs that and was verified on the broken venv (`cv2` failed before, imported after).

## Golden benchmark (`clipper bench golden/golden.json`, 4 real scenes, 11 human-labelled moments)
Default settings: pooled recall 10/11 (91%); per scene 1.0, 0.667, 1.0, 1.0; mean boundary quality 0.94-1.0; incomplete-sentence rate 0 except one clip that ends where the 150 s source slice ends mid-sentence (the critic now trims it back). Mean start error is large (4-7 s on 3 scenes): clips tend to include more setup than the reference. Precision is 0.4-0.75 because we export up to 2x as many clips as there are labelled moments, and unlabelled clips count as false positives.
Synthetic set (`golden/synthetic.py`, exact ground truth): the pipeline finds 2 of 3 constructed stories with start error 0.0 s and end error 0.11 s **only with `CLIPPER_DENSE_CORE=1`**; with it the real-scene result is mixed (panel2 and bk fell, ch rose), so it is off by default. Visual-only highlight: found (recall 1.0).
**Coverage: interviews, panels and conversation only.** No human-labelled gaming, comedy, sports or IRL video exists in this repo. Do not read these numbers as general "viral detection" accuracy; no such accuracy is claimed.

## Bugs found by this validation and fixed
`audio_rms` returned 6 values per second (every audio peak was placed at the wrong time since the first release); resumable model download never resumed; `candidates()` crashed when two signal series differed by one sample; a non-video file passed ingest and gave "0 clips, exit 0"; an "And ..." chain walked a clip start back through the whole transcript; the critic proposed extending a clip past the end of the media; installer failed to parse on Python 3.10/3.11; the lockfile produced a broken OpenCV; the first cancel test was vacuous (replaced by one that kills a real process tree).

## Not verified (honest list)
* Real Twitch, YouTube, Kick live connections and chat (adapters exist; mocked only). The Kick webhook receiver verifies against Kick's real public key and an independent signer, but no real event was delivered.
* OpenAI calls: a real key was never used. The request/response handling, budget and retries are tested against fakes. The default price constants are assumptions: check current pricing.
* Windows and macOS runs; hardware encoders (NVENC/QSV/AMF/VideoToolbox: only the fallback path ran, here every test encode failed and libx264 was used); GPU upscalers.
* Live speech transcription on a real stream (the stand-in model path is tested; faster-whisper was not run live).
* Ollama (not installed here).
* Quality on gaming, comedy, sports and IRL content.
* The speaker detector's thresholds on anything beyond the four scenes.
