# Setup: how to run clipper

Ten minutes from download to your first clips. No Docker, no cloud account, no paid service.

## 1. What you need
* **Python 3.10 to 3.13.** Check with `python --version`. Windows: `winget install Python.Python.3.11`.
* **ffmpeg.** If you do not have it, the installer adds a bundled copy, so you can skip this.
* About 1 GB of disk for the basics, more if you add the AI extras (torch is large).

## 2. Install

**Windows 11 (PowerShell, in the project folder):**
```powershell
powershell -ExecutionPolicy Bypass -File install.ps1 -With recommended,dev
.\.venv\Scripts\Activate.ps1
```

**Linux / macOS:**
```bash
./install.sh --with recommended,dev
source .venv/bin/activate
```

`recommended` gives you URL downloads (yt-dlp), local speech-to-text (faster-whisper) and face framing (OpenCV). `dev` adds the test tools.
Want more? Add groups: `-With recommended,dev,speaker,audio,ocr` (Windows) or `--with recommended,dev,speaker,audio,ocr` (Linux/macOS).
`speaker` follows whoever is talking, `audio` hears laughter and cheering, `ocr` reads on-screen text. These download large models only when you ask (see step 7).

## 3. Check it works
```bash
clipper doctor        # lists what works on this machine and the exact command to fix anything missing
clipper selftest      # runs the offline tests (about 2 minutes); should end with "passed"
```
`MISS` lines for optional things (OpenAI key, Ollama, GPU) are fine. `ffmpeg` must say `ok`.

## 4. Your first clips (recorded video)
```bash
clipper run "C:\Videos\talk.mp4" --clips 5 --export out      # a local file
clipper run "https://www.youtube.com/watch?v=VIDEO_ID" --clips 5 --export out   # a URL
```
That one command downloads, transcribes, finds the best moments, cuts them on complete sentences, adds captions, renders vertical 1080x1920 video, has the editor-critic check and fix them, and exports.
Look in `out/`: the `.mp4` clips and `metadata.json` (times, titles, what was skipped, OpenAI usage).
The first run downloads the Whisper speech model (a few hundred MB). Later runs reuse everything cached.

Useful options (all optional):
| option | meaning |
|---|---|
| `--clips 10` | how many clips |
| `--style karaoke` | caption style: `bold_pop` (default) `karaoke` `clean_lower` `boxed` `minimal_white` `highlight_words` `podcast_speaker` `gaming_bold` `cinematic` |
| `--profile gaming` | content type: `podcast` `interview` `gaming` `irl` `comedy` `sports` (default: detected automatically) |
| `--quality high` | `draft` (fast preview), `normal`, `high` |
| `--transcript words.json` | use your own transcript instead of running Whisper |
| `--cookies cookies.txt` | your own browser cookies, for videos that need a login or show a bot check |

See the caption styles: `clipper styles --preview styles.png`.

## 5. Livestreams
```bash
clipper watch "https://www.twitch.tv/CHANNEL" --clips 5 --out live
```
It records the stream, watches chat and audio for exciting moments, cuts preliminary clips while it runs, and **when the stream ends it finalizes, analyses the whole recording, polishes and exports to `live/export/` by itself**. Stop it early with Ctrl+C: it still finishes and exports. If your machine crashes, run the same command with `--resume`.

* **YouTube live chat:** set your own free key first: `$env:YOUTUBE_API_KEY="..."` (PowerShell) or `export YOUTUBE_API_KEY=...`.
* **Kick:** chat needs your own Kick app and a public HTTPS address (`clipper kick-relay`, see docs/MCP.md and SECURITY.md), or supply a chat file with `--chat-file relay.jsonl`. Without chat it still works from audio and speech.
* **OBS or your own encoder:** run `clipper ingest-server` in one terminal; in OBS set Stream Server to `rtmp://127.0.0.1:1935/live` and Stream Key to `mystream`; then in another terminal run `clipper watch rtmp://127.0.0.1:1935/live/mystream --out live`.
* Chat usually runs 5 to 15 seconds behind the video. After a run, `clipper sync-check live` measures it; pass the result as `--latency`.

## 6. Optional: use OpenAI to improve ranking
Without any key everything runs locally. With a key, GPT-4o-mini is asked only about borderline moments, under a hard cap.
```powershell
$env:OPENAI_API_KEY = "sk-..."          # PowerShell (this window only)
```
```bash
export OPENAI_API_KEY=sk-...            # Linux / macOS
```
```bash
clipper run VIDEO --mode smart --max-usd 0.10 --export out
```
The key is never written to any file or log. `metadata.json` reports the exact calls, tokens and cost. `--mode disabled` turns all outside AI off.
Free local alternative: install Ollama and use `--mode local_only`.

## 7. Optional AI models (downloaded only when you ask)
```bash
clipper models list                   # what exists, size, licence, installed or not
clipper models fetch yunet.onnx       # tiny face detector (0.2 MB)
clipper models fetch all --yes        # everything (about 420 MB: sound events + speaker voices)
```
Offline machine? Set `CLIPPER_OFFLINE=1`: nothing is downloaded and any feature whose model is missing is skipped and reported, never a crash.

## 8. Adjust a clip afterwards
Each run prints a job id (also in `clipper-jobs/`).
```bash
clipper status JOB                          # clips, times, issues
clipper edit JOB 3 --start-shift -2         # start 2 s earlier
clipper edit JOB 3 --captions boxed --zoom-at 12.5
clipper edit JOB 3 --drop                   # remove a clip
clipper render JOB                          # only edited clips re-render
clipper export JOB --out out
```
`clipper polish JOB` re-runs the automatic review and fixes. `clipper critique JOB --packet` makes contact sheets for an agent or you to look at.

## 9. Use it from an AI agent
**Claude Code:**
```bash
claude mcp add clipper -- /full/path/to/clip/.venv/bin/clipper mcp
```
(Windows: `C:\full\path\to\clip\.venv\Scripts\clipper.exe`.) Then ask: "Clip the best 8 moments of this video and make them punchy: <URL>". Other MCP clients and the HTTP API: [docs/MCP.md](docs/MCP.md), [docs/API.md](docs/API.md). Agents read [SKILL.md](SKILL.md) and [RUNBOOK.md](RUNBOOK.md) automatically via AGENTS.md / CLAUDE.md.

## 10. If something goes wrong
* `clipper doctor` first. It names the missing piece and the fix.
* YouTube says "Sign in to confirm you're not a bot": export your own cookies and pass `--cookies cookies.txt`. clipper does not bypass this.
* `import cv2` fails with libGL: run `python scripts/install.py --with recommended`; it repairs OpenCV.
* Nothing exported? Check `metadata.json`: `not_exported` lists clips that failed quality checks and why.
* More: [TROUBLESHOOTING.md](TROUBLESHOOTING.md). Full install options: [INSTALLATION.md](INSTALLATION.md). What was tested and what was not: [docs/VALIDATION.md](docs/VALIDATION.md).

## 11. Where things live
* `clipper-jobs/<id>/`: one folder per source video (cache, plan, renders, logs). Safe to delete to free space.
* `~/.cache/clipper` (Windows: `C:\Users\YOU\.cache\clipper`): downloaded models.
* `out/` (or wherever you pass `--export`): your finished clips.
* To uninstall: delete the `.venv` folder, `clipper-jobs`, and the model cache.
