#!/usr/bin/env python3
"""Cross-platform installer (Windows 11 / Linux / macOS). Creates a virtualenv, installs the chosen groups, and REPAIRS the one known conflict:
rapidocr-onnxruntime depends on `opencv-python` (the GUI build, which needs libGL and clobbers `cv2/`), while this project wants
`opencv-python-headless`. After installing, the repair step removes the GUI build and force-reinstalls headless, then verifies `import cv2`.
Nothing here needs admin rights, Docker, a cloud account or a paid service.

  python scripts/install.py                       # core + recommended (download URLs, local Whisper, face framing)
  python scripts/install.py --with speaker,audio,ocr
  python scripts/install.py --all                 # every optional group (large: torch)
  python scripts/install.py --models              # also fetch small models (YuNet 0.2 MB); big ones ask first
"""
import argparse, os, platform, shutil, subprocess, sys, venv
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GROUPS = {"recommended": "yt-dlp, faster-whisper, opencv (face framing)", "downloader": "yt-dlp + streamlink", "transcription": "faster-whisper (local speech-to-text)",
          "vision": "opencv + numpy (face framing)", "audio": "PANNs sound events (torch, ~330 MB model)", "speaker": "who-is-speaking following (torch + SpeechBrain, ~90 MB model)",
          "ocr": "on-screen text (RapidOCR)", "livestream": "websocket client", "ffmpeg": "bundled ffmpeg if you have none", "dev": "pytest etc."}


def sh(cmd, check=True, **kw):
    print("  $", " ".join(map(str, cmd)))
    return subprocess.run([str(c) for c in cmd], check=check, **kw)


def py_of(v):
    return v / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def has_cv2(py):
    r = subprocess.run([str(py), "-c", "import cv2;print(cv2.__version__)"], capture_output=True, text=True)
    return r.returncode == 0


def repair_opencv(py):
    """Make `import cv2` work with the HEADLESS build regardless of what pulled in the GUI build."""
    if has_cv2(py):
        return True
    print("opencv is broken (typically libGL missing from the GUI build): repairing")
    sh([py, "-m", "pip", "uninstall", "-y", "opencv-python", "opencv-python-headless", "opencv-contrib-python", "opencv-contrib-python-headless"], check=False)
    sh([py, "-m", "pip", "install", "--force-reinstall", "--no-deps", "opencv-python-headless==5.0.0.93"], check=False)
    return has_cv2(py)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--venv", default=str(ROOT / ".venv")), ap.add_argument("--with", dest="groups", default="recommended", help="comma list: " + ",".join(GROUPS))
    ap.add_argument("--all", action="store_true"), ap.add_argument("--models", action="store_true"), ap.add_argument("--no-venv", action="store_true", help="install into the current Python")
    ap.add_argument("--lock", action="store_true", help="install the exact tested versions from requirements-lock.txt (needs Python 3.11)")
    a = ap.parse_args()
    if sys.version_info < (3, 10):
        sys.exit("Python 3.10+ is required")
    if a.no_venv:
        py = Path(sys.executable)
    else:
        v = Path(a.venv)
        if not v.exists():
            print(f"creating {v}")
            venv.EnvBuilder(with_pip=True).create(v)
        py = py_of(v)
    groups = [g for g in GROUPS if g not in ("dev",)] if a.all else [g.strip() for g in a.groups.split(",") if g.strip()]
    bad = [g for g in groups if g not in GROUPS]
    if bad:
        sys.exit(f"unknown group(s) {bad}; choose from {sorted(GROUPS)}")
    sh([py, "-m", "pip", "install", "--upgrade", "pip"], check=False)
    extra = ["--extra-index-url", "https://download.pytorch.org/whl/cpu"] if any(g in groups for g in ("audio", "speaker")) and platform.system() != "Darwin" else []
    if a.lock:
        sh([py, "-m", "pip", "install", "-r", ROOT / "requirements-lock.txt", *extra])
        sh([py, "-m", "pip", "install", "--no-deps", ROOT])
    else:
        sh([py, "-m", "pip", "install", f"{ROOT}[{','.join(groups)}]" if groups else str(ROOT), *extra])
    if "ocr" in groups or a.all or a.lock:
        if "ocr" in groups or a.all:
            sh([py, "-m", "pip", "install", "rapidocr-onnxruntime==1.4.4"], check=False)
        repair_opencv(py)
    if not any(shutil.which(x) for x in ("ffmpeg",)):
        print("no ffmpeg on PATH: installing the bundled one (imageio-ffmpeg)")
        sh([py, "-m", "pip", "install", "imageio-ffmpeg>=0.4.9"])
    if a.models:
        sh([py, "-m", "clipper", "models", "fetch", "yunet.onnx"], check=False)
        print("Larger optional models (PANNs 330 MB, ECAPA 90 MB) are NOT downloaded automatically: run `clipper models list`, then `clipper models fetch all --yes` if you want them.")
    print("\n== verification ==")
    sh([py, "-m", "clipper", "doctor"], check=False)
    scripts = py.parent
    activate = ".venv\\Scripts\\Activate.ps1" if os.name == "nt" else "source .venv/bin/activate"  # (no backslashes inside f-string expressions on Python < 3.12)
    exe = scripts / ("clipper.exe" if os.name == "nt" else "clipper")
    print("\nDone. Activate with:  " + activate + "   then:  clipper run <URL> --export out/")
    print("(or call " + str(exe) + " directly)")

if __name__ == "__main__":
    main()
