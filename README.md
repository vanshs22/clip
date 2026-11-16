# clipper

Give an AI agent (or yourself) a video URL, a local file or a live stream. Get back finished 1080x1920 clips: highlights found with local signals (speech, audio, motion, chat), cut on complete thoughts, captioned, rendered, reviewed by an automatic editor-critic, quality-checked and exported.

```bash
clipper run "https://youtu.be/…" --clips 10 --export out/          # recorded video, one command
clipper watch "https://www.twitch.tv/…" --clips 10 --out live/     # livestream: records, finalizes by itself, exports
```

No paid service is required. The core is the Python standard library plus an ffmpeg binary; every AI model is optional and downloaded only when you ask. An OpenAI key (optional) is spent sparingly under a hard per-job cap.

* **Start here: [SETUP.md](SETUP.md)** (install, first run, livestreams, agents). Install details: [INSTALLATION.md](INSTALLATION.md) (Windows 11 first). Use from an agent: [SKILL.md](SKILL.md), [docs/MCP.md](docs/MCP.md).
* How it works: [ARCHITECTURE.md](ARCHITECTURE.md). Choices and licences: [TECH_STACK.md](TECH_STACK.md), [THIRD_PARTY.md](THIRD_PARTY.md). Problems: [TROUBLESHOOTING.md](TROUBLESHOOTING.md).
* What was actually tested, and what was not: [docs/VALIDATION.md](docs/VALIDATION.md) and [docs/SPEC.md](docs/SPEC.md). Security: [SECURITY.md](SECURITY.md). Changes: [CHANGELOG.md](CHANGELOG.md).

**Honest scope.** Tested on interviews, panels and podcast-style conversation and on constructed media. There is no human-labelled gaming, sports, comedy or IRL footage in this repo, so quality claims there are untested. Platform connections (Twitch IRC, YouTube API, Kick webhooks) are implemented and tested with mocks; real-account connections were not verified.
