# Runbook (one page)
Needs a shell. `clipper` = `python -m clipper` if not installed. Jobs live in `./clipper-jobs/<id>` (`CLIPPER_JOBS` overrides).

1. First time: `clipper doctor`, `clipper selftest`. Read `memory/learnings.md`.
2. `clipper process SRC --clips N --stop-after select` prints a job id.
3. After `render`: `clipper polish JOB`, then the vision pass (`clipper critique JOB --packet`, open every sheet). Read `skills/editor-critic/SKILL.md`.
4. Loop: `clipper next JOB` prints the one next step. Do it, repeat until it says `clipper export`.
5. Gate: after `select`, show `clipper status JOB` to the user and STOP for their answer.

Rules that matter most
1. Look at a frame of every rendered clip before saying it is good (`ffmpeg -ss 3 -i clip.mp4 -frames:v 1 f.png`).
2. Fix problems with `clipper edit`, not by editing code or JSON by hand.
3. Interrupted job? Re-run the same `process` command: finished stages and clips are cached.
4. A stage failed on bad input? Fix the input it names and re-run; use `--force-from STAGE` to redo from a stage.
