# Changelog

## 0.4.0 — production hardening (this release)
Backup of the previous state: git tag `pre-hardening` (also `backup/clipper-skill-pre-hardening.zip` at release time). Baseline: `validation/baseline-before-hardening.txt` (14/14 old selftest checks).

### Added
- **One-shot workflows**: `clipper run URL --export out/` and `clipper watch LIVE --out dir/` (record, detect, auto-finalize, process, polish, export; no second command). MCP `clip_video` / `monitor_livestream` do the same.
- **Live engine**: persistent session state machine (`session.json`), `watch --resume`, reconnect with recorded gaps, frame-accurate re-encoded extraction, bounded memory, live LLM judging sharing the cache and budget with the final pass, CANCEL marker, graceful SIGINT/SIGTERM.
- **Model manager** (`clipper/models.py`, `models_manifest.json`): SHA-256 pinned, resumable, atomic, offline mode; optional-model failure degrades features and is reported.
- **Job identity**: source content fingerprints, per-stage input hashes chained through dependencies, per-clip render keys incl. source + options, job lock.
- **LLM safety**: indices validated against each candidate's own window, strict typed validation, retries with backoff, hard token/USD/call budget with reservation, persistent budget file, provider fallback (OpenAI -> Ollama -> local).
- **Detection**: event-only candidates for speech-free highlights, chat spam/giveaway filtering, YouTube heatmap signal, auto profile, live-candidate merge, optional bounded dense core (off by default).
- **Boundaries**: delayed-answer and laughter-tail handling, unpunctuated-run splitting, bounded setup chains, boundary-quality score, trim-back when the source ends mid-sentence.
- **Editing**: 9 caption presets + custom style file, bundled OFL fonts, speaker colours, logo, fades, intro/outro, per-encoder quality args with real test encodes, frozen-picture and caption-overflow QC, previews (`clipper styles --preview`).
- **Agents**: 12 MCP tools, non-blocking calls, progress notifications, cancel, schema validation, queue recovery by pid.
- **Packaging**: dependency groups, tested lockfile, Windows/Linux/macOS installers incl. the OpenCV repair, CI matrix, THIRD_PARTY, SECURITY.

### Fixed (found by the new tests / real footage)
- **`audio_rms` returned 6 values per second** (resampled after counting samples): every audio-based pick was misplaced. Regression-tested.
- Resumable model download never resumed (short reads were treated as complete).
- `candidates()` crashed when signal series differed in length by one sample.
- A non-video file passed ingest and produced "0 clips, exit 0".
- Setup-sentence chain walked back through a whole transcript of "And ..." lines.
- Critic proposed extending a clip past the end of the media.
- Chat-to-video clock offset, OpenCV 5 Haar removal, numpy values in JSON, captions selecting words by end time, clip planned beyond media end (earlier releases).

### Known limits: see docs/SPEC.md and the final report.
