# Tech stack: choices, free alternatives considered, and why

| need | chosen (free) | considered / measured | note |
|---|---|---|---|
| transcription | faster-whisper (word times, VAD) | WhisperX needs gated pyannote | local, CPU-capable |
| downloads | yt-dlp, streamlink fallback | | YouTube may demand cookies or show a bot check: pass the user's own `--cookies` |
| live ingest | MediaMTX + ffmpeg segments | | tested with real RTMP |
| chat | Twitch IRC (anonymous), YouTube Data API (user key), Kick signed webhooks, JSONL relay, TwitchDownloaderCLI for VODs | `chat-downloader` (key-free YouTube) was blocked from our test IP: unverified | real-account tests not run |
| faces | OpenCV YuNet (ONNX, 0.2 MB) | Haar (removed in OpenCV 5), YOLOv8 (heavier, no gain) | |
| who is speaking | SpeechBrain ECAPA voices + mouth motion, reliability-gated | resemblyzer d-vectors (worse), Light-ASD (accurate, 10x slower, no gain), MFCC (not separable) | 99.6% pooled on 4 scenes; thresholds tuned on small data |
| sound events | PANNs Cnn14 | | verified on real audio |
| on-screen text | RapidOCR | | |
| LLM | OpenAI gpt-4o-mini (optional, capped), then Ollama, then none | | |
| captions | libass via ffmpeg, bundled OFL fonts (Poppins, Anton, Bebas Neue) | system fonts (not portable) | |
| encoding | libx264 default; NVENC, QSV, AMF, VideoToolbox only after a real test encode passes | | |
| upscaling | lanczos + sharpen | FSRCNN, ESPCN, LapSRN, EDSR on CPU; Real-ESRGAN (anime model cartoonises faces; x4plus untestable here) | benchmark in docs/SPEC.md |
| interpolation | RIFE ncnn (opt-in) | | clean on talking heads |
| agents | stdlib MCP + HTTP, SQLite queue | | |
