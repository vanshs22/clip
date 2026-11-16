#!/usr/bin/env bash
# Rebuilds the golden media from public-domain sources (needs curl + ffmpeg + `pip install faster-whisper`). Not stored in git.
set -euo pipefail
cd "$(dirname "$0")" && mkdir -p media && cd media
UA="clipper-golden/0.1 (https://github.com/vanshs22/clip)"
U="https://upload.wikimedia.org/wikipedia/commons/1/14/President_Trump_Participates_in_a_Panel_Discussion_at_the_Generation_Next_Summit.webm"
[ -f full.webm ] || curl -sL -A "$UA" -o full.webm "$U"
ffmpeg -v error -y -ss 120 -t 180 -i full.webm -c copy panel1.mkv
ffmpeg -v error -y -ss 420 -t 180 -i full.webm -c copy panel2.mkv
python3 - <<'PY'
import json
from clipper import transcribe as T
from clipper.util import probe
for n in ("panel1", "panel2"):
    f = f"{n}.mkv"
    json.dump({"words": T.transcribe(f, probe(f)["duration"], f"{n}.cache", "small")}, open(f"{n}.words.json", "w"))
PY
# extra validation scenes (CC BY / CC BY-SA, see validation/SOURCES.md): fetch by title via the Commons API
python3 - <<'PY'
import json, subprocess, urllib.parse, urllib.request
UA = {"User-Agent": "clipper-golden/0.1 (https://github.com/vanshs22/clip)"}
for key, title in {"bk": "Backroom Comics Podcast - Mark Waid Interview at ECCC 2010 Part 2.webm", "ch": "Ecology, Ethics, Anarchism - In Conversation with Noam Chomsky - Imperiled Life.webm"}.items():
    q = urllib.parse.urlencode({"action": "query", "titles": "File:" + title, "prop": "imageinfo", "iiprop": "url", "format": "json"})
    pages = json.load(urllib.request.urlopen(urllib.request.Request("https://commons.wikimedia.org/w/api.php?" + q, headers=UA)))["query"]["pages"]
    url = list(pages.values())[0].get("imageinfo", [{}])[0].get("url")
    print(key, url or "TITLE NOT FOUND: search Commons for the file named in validation/SOURCES.md")
PY
echo "golden media ready: now run  python -m clipper bench golden/golden.json"
