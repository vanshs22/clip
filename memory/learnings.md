# Lessons (agent reads first, appends after each job)
Format: `- [area] what went wrong or right -> what to do`.

- [boundaries] Clips that start on "and/but/so" lack setup -> the boundary engine pulls the prior sentence; do not shorten these by hand.
- [captions] Captions sit at ~75% height to clear platform UI; move the layout, not the captions, if they hide a face.
- [live] Chat runs 5-15 s behind video -> calibrate `--latency` on the first real stream.
- [critic] Trimming a filler opening can expose a dependent sentence ("And then..."): fixes can fight. The tool never pulls filler back in; if a clip still fails after 3 rounds, re-select the moment.
- [fixes] One structural edit per clip per round, then re-measure; never stack shifts.
- [speaker] A confidence gate tuned on 8 cases passed a false positive once tested on 12: always re-check thresholds on a different event before trusting them. Voice model quality (ECAPA vs d-vector) mattered more than any threshold.
- [validation] Never compare results against a clock I assumed: put an independent marker (a beep) in the stream. `-c copy` slices misalign video and transcript: re-encode.
- [upscale] Benchmark against the original (downscale then restore); always look at a face crop. Models can be 'better' on a number and worse on a face.
