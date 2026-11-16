# Architecture

```
 agents: CLI · MCP (stdio) · HTTP (loopback)   ->   clipper run | watch | process | edit | render | polish | critique | export | doctor | models

 recorded (flow.run_video)
 ingest -> transcribe -> signals -> candidates -> select -> speakers -> render -> qc -> polish -> export
   |          |            |           |            |          |           |       |      |
 yt-dlp   faster-whisper  audio/motion  fusion +   LLM, hard   voices +   ffmpeg  checks  editor-critic loop
 file     word times,     chat/heat     boundaries budget,     faces      per-clip + auto  (max 3 rounds)
 HLS/RTMP chunk cache     PANNs/OCR     (word idx) validated   (gated)    cache    fixes

 live (flow.watch_stream)
 live.monitor (rolling buffer, chat/audio/speech spikes, preliminary clips) -> finalize -> the SAME recorded pipeline
   record.ts + chat.jsonl + events.json  -> candidates are merged with the live events -> export
```

**Stages** (`job.py`) each write one artifact and record a hash of their inputs in `stages.json`: the stage's options, the source fingerprint and the hashes of the stages it depends on. A stage is current only if its artifact exists and its hash matches, so changing an option invalidates exactly its dependents (`style` -> render and qc; `clips` -> select and everything after; `model` -> everything after ingest). A job directory is keyed by source content, so different options on the same video share the expensive download and transcript, while a different file never shares renders. A lock file allows one writer per job.

**Engines** are independent modules, so a render failure never repeats download or transcription: `ingest`, `transcribe`, `signals`, `score` (multimodal fusion and profiles), `boundaries` (word indices to exact timestamps; sentence, answer and reaction logic; quality score), `llm` (judge and budget), `speaker` (faces and voices), `render` (ffmpeg, captions, encoders), `critics`, `live`, `flow`, `server`.

**Communication** is files: JSON artifacts in the job directory. The CLI is the single behaviour surface; MCP and HTTP spawn it, so a crash cannot take the server down. A live session persists `session.json` with a state machine: starting, waiting_for_stream, recording, finalizing, processing, done, failed or cancelled.

**Degradation by design.** Every optional model goes through `models.py`. If it is missing, offline or corrupt, the feature is skipped, recorded under `features_skipped`, and the pipeline continues (centred crop instead of speaker following, no sound events, local scoring instead of an LLM).

**Budget.** LLM calls reserve their worst case against caps (calls, tokens, USD) before they happen. The live session and the final pass share one budget file and one cache, so a moment judged live is never paid for twice.
