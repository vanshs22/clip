# Troubleshooting

| symptom | cause / fix |
|---|---|
| `ffmpeg not found` | install ffmpeg or `pip install imageio-ffmpeg`; set `CLIPPER_FFMPEG` |
| `ImportError: libGL.so.1` | GUI OpenCV installed. `python scripts/install.py` repairs it (see INSTALLATION.md) |
| YouTube: "Sign in to confirm you're not a bot" / `auth_required` | pass YOUR cookies: `--cookies cookies.txt` (export from your browser). We do not bypass this |
| `not a decodable media file` | the download failed or the file is not video/audio; fetch it again |
| no captions / no speech clips | `pip install faster-whisper`, or `--transcript words.json`; without it only audio/visual (event) clips are made |
| speaker following not used | `clipper doctor`: it needs opencv, speechbrain, torch and the models. It also only runs when the reliability gate passes (two clearly different voices matched to two faces); otherwise you get a centred crop or split layout |
| a model "UNAVAILABLE" / offline | `clipper models list`, `clipper models fetch <name>`; unset `CLIPPER_OFFLINE` |
| OpenAI calls 0 | no `OPENAI_API_KEY`, `--mode disabled`, or the budget cap was reached (`features_skipped` says which) |
| chat appears late versus the video | measure with `clipper sync-check live/` and pass `--latency` |
| live: "stream not available yet" | the monitor retries for about 1.5 minutes; start the stream or check the URL (`streamlink URL`) |
| live process died | `clipper watch SRC --out live/ --resume` finalizes what was recorded |
| render slow | install a hardware encoder driver (it is test-encoded automatically); `--quality draft` for a quick look |
| `job ... is being processed by another process` | a live `job.lock`; a dead owner's lock is taken over automatically |
| a clip starts or ends oddly | `clipper critique JOB` names the problem; fix with `clipper edit`; `clipper polish JOB` applies the mechanical fixes |
| captions use the wrong font | custom styles with a non-bundled font fall back to system fonts; `features_skipped` says so |
