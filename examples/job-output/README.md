# Example completed job (real output)
`clipper run golden/media/bk.mkv --transcript ... --clips 3 --mode smart --style bold_pop --quality normal --export out`
A real two-person interview (CC BY, see validation/SOURCES.md). Videos are not included; these are the JSON artefacts:
- `metadata.json` — exported clips with times, boundary quality, warnings; the honest run report (OpenAI calls/tokens/USD, local models, features skipped, per-clip critic verdicts incl. what was NOT assessed).
- `plan.json` — the ranked clips with their word-index boundaries and signals. `report.json` — QC per clip. `stages.json` — the per-stage input hashes that drive cache invalidation.
