# Installation

Requirements: Python 3.10-3.13 and ffmpeg (the installer adds a bundled one if you have none). No Docker, no cloud account, no paid service, no admin rights.

## Windows 11 (PowerShell)
```powershell
git clone https://github.com/vanshs22/clip; cd clip
winget install Python.Python.3.11          # if you do not have Python
powershell -ExecutionPolicy Bypass -File install.ps1                          # core + recommended (yt-dlp, faster-whisper, face framing)
powershell -ExecutionPolicy Bypass -File install.ps1 -With speaker,audio,ocr  # add who-is-speaking, sound events, on-screen text (torch is large)
.\.venv\Scripts\Activate.ps1
clipper doctor                  # shows what is missing and the command that fixes it
clipper run "C:\Videos\talk.mp4" --export out
```
Optional: `winget install Gyan.FFmpeg` (system ffmpeg), `winget install Ollama.Ollama` (local LLM). Paths with spaces work and the encoder is test-selected (NVENC, QSV, AMF or libx264). CI for Windows is defined in `.github/workflows/ci.yml` but was **not run** by the author.

## Linux / macOS
```bash
git clone https://github.com/vanshs22/clip && cd clip
./install.sh                                  # or: ./install.sh --with speaker,audio,ocr | --all | --lock (exact tested versions)
source .venv/bin/activate && clipper doctor
```
Debian/Ubuntu may need `sudo apt install python3-venv ffmpeg`; macOS: `brew install python ffmpeg`.

## Required, recommended, optional
* **Required**: Python and ffmpeg. That alone gives rendering, QC, captions, audio/motion/scene highlights, boundaries and critics (supply transcripts with `--transcript`).
* **Recommended**: `yt-dlp` (URLs), `faster-whisper` (speech), `opencv` (face framing).
* **Optional**: `speaker`, `audio` (PANNs), `ocr`, `livestream`, Ollama, MediaMTX (auto-downloaded), `realesrgan` and `rife-ncnn-vulkan` (opt-in).

Model downloads are small except PANNs (330 MB) and ECAPA (90 MB). They are **never fetched automatically**: `clipper models list`, `clipper models fetch yunet.onnx`, `clipper models fetch all --yes`. Offline: `CLIPPER_OFFLINE=1`.

## The OpenCV conflict (handled by the installer)
`rapidocr-onnxruntime` depends on the GUI `opencv-python`, which needs libGL and breaks `import cv2` on servers. `scripts/install.py` removes it, reinstalls `opencv-python-headless` and verifies `import cv2`. Manual fix: `pip uninstall -y opencv-python && pip install --force-reinstall --no-deps opencv-python-headless==5.0.0.93`.

## Reproducible install
`pip install -r requirements-lock.txt && pip install --no-deps .` reproduces the tested environment (Python 3.11); `pip check` passes on it.

## Upgrade and uninstall
Upgrade: `git pull` then `python scripts/install.py --with <your groups>`. Uninstall: delete the `.venv` folder and, if you want, `~/.cache/clipper` (models) and your `clipper-jobs` folder. Nothing else is written elsewhere.
