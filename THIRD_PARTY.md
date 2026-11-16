# Third-party code, fonts and models

Nothing from the AGPL project (Clips Studio) was copied: its *ideas* (multimodal fusion, chat/audio/visual evidence, per-genre weighting) were re-implemented independently.
Chopify (MIT) and AutoClip (MIT) were used as design references only (resumable chunked transcription; LLM-picks-word-indices). Reel Studio (the reference you supplied) informed the
stage/gate/critic structure. If you vendor code from any of them, follow their licences.

| component | how it is used | licence | where |
|---|---|---|---|
| FFmpeg | rendering, probing, segmenting (not bundled; `imageio-ffmpeg` can fetch a static build) | LGPL/GPL depending on build | https://ffmpeg.org/legal.html |
| MediaMTX | optional local RTMP ingest, downloaded on demand | MIT | https://github.com/bluenviron/mediamtx |
| faster-whisper / CTranslate2 | local transcription (models downloaded from Hugging Face on first use) | MIT / Whisper model weights MIT | https://github.com/SYSTRAN/faster-whisper |
| yt-dlp, streamlink | URL / live-stream resolution (optional) | Unlicense / BSD-2 | |
| OpenCV (+ YuNet face model, OpenCV Zoo) | face detection | Apache-2.0 / MIT | `clipper/models_manifest.json` |
| SpeechBrain ECAPA-TDNN (VoxCeleb) | speaker voice embeddings | Apache-2.0 | `models_manifest.json` |
| PANNs (Cnn14) + AudioSet labels | sound events | model: see Zenodo 3987831; labels CC BY 4.0 | `models_manifest.json` |
| RapidOCR | on-screen text | Apache-2.0 | |
| Poppins, Anton, Bebas Neue fonts | caption fonts bundled in `clipper/fonts` | SIL Open Font License 1.1 (texts in that folder) | |
| Real-ESRGAN / RIFE ncnn CLIs | OPT-IN upscaling / interpolation, not bundled | BSD-3 / MIT | |
| OpenAI API | optional LLM judging with YOUR key | their terms | |

Validation footage (never redistributed here): see `validation/SOURCES.md`.
