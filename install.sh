#!/usr/bin/env bash
# Linux / macOS installer:  ./install.sh [--with speaker,audio] [--all] [--models] [--lock]
set -euo pipefail
cd "$(dirname "$0")"
PY="${PYTHON:-python3}"
command -v "$PY" >/dev/null || { echo "python3 (3.10+) not found. Linux: install python3 + python3-venv. macOS: brew install python"; exit 1; }
"$PY" -c 'import sys; sys.exit(sys.version_info < (3, 10))' || { echo "Python 3.10+ required"; exit 1; }
if [ "$(uname)" = "Linux" ] && ! "$PY" -c "import venv, ensurepip" 2>/dev/null; then echo "Debian/Ubuntu: sudo apt install python3-venv"; exit 1; fi
"$PY" scripts/install.py "$@"
echo
echo "Optional system tools: ffmpeg (brew install ffmpeg / apt install ffmpeg), ollama (local LLM), mediamtx (auto-downloaded on first 'clipper ingest-server')"
