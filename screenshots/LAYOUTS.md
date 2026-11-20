# Clip LAYOUTS (not caption styles): other repos vs ours

Wireframes: `layouts/SHEET_ours.png`, `layouts/SHEET_others.png`, `layouts/SHEET_proposed.png`. Regenerate with `make_layouts.py`.
Real reference: `reference/clips-kitty/gaming-layout.png` (Clips Kitty's layout picker, 11 cards).

## Ours (clipper-skill 0.4.0, `render.py`)
- fill: 9:16 crop that follows the subject (face/speaker keyframes)
- fit: whole frame over a blurred copy of itself
- split: two people stacked (auto when two speakers are visible)

## In other repos, missing from ours
- Clips Kitty (11, defined as data in `gaming/layouts.json`): Split (webcam top 25-50%), Basecam (webcam bottom), Half, Fullscreen, Blurred, Small facecam, Circle facecam, Game UI (scoreboard/map layer), Mosaic, Dual facecam, Duo split. Plus a pick-before-processing UI with draggable boxes and TikTok-UI safe-zone preview.
- SupoClip: interview clips framed shot by shot (stacked when both on camera, tight face crop for a close-up, full frame for graphics/b-roll), switching on the video's own cuts.
- Chopify: podcast split, two adjustable crop centres (`--layout split --centers 0.25 0.75`), no speaker detection.
- OpenShorts: per-scene TRACK (one face, follow it) vs GENERAL (group/wide, blurred backdrop).

## Proposed viral layouts
1. Facecam + game/content presets (Kitty's set): biggest gap, covers gaming and reaction.
2. Hook title card on top: red/white headline bar for the first 2-3 s.
3. Content top + filler bottom (parkour/satisfying loop): common retention trick. Needs user-supplied filler footage.
4. Story card + filler: Reddit-style text card top, filler below.
5. Reaction/stitch: source top, reactor bottom.
6. Podcast auto-switch: full-frame active speaker with a small circle of the other (we already have `speaker.py`).
7. Progress bar + "Part N": series hook.
8. Circle cam over content.
Safe-zone: keep captions and faces clear of platform UI (top ~12%, bottom ~25%); Kitty previews this.

## Implemented in clipper-skill (see `rendered/`)
Real ffmpeg/libass renders, not mock-ups: `rendered/SHEET_layouts.png`, `rendered/SHEET_captions.png`, one PNG per item in `rendered/layouts/` and `rendered/captions/`.
- Layouts added (11): split, basecam, half, fullscreen, small_facecam, circle_facecam, game_ui, mosaic, dual_facecam, duo_split, filler. Plus `--swap`, `--layout-pct`, `--hook`, `--progress`. This covers reaction/stitch (`half --swap`) and circle cam on content (`circle_facecam`).
- Caption presets added (11) + 5 aliases: see CHANGELOG in the zip.
- Not implemented: story card, podcast auto-switch speaker layout, emoji on keywords, automatic webcam detection (you pass `--cam x,y,w,h`).

## Real render of the newest features (`rendered/real/`)
Frames from a clip made with the installed CLI on real speech (JFK recording over a test pattern): judge -> caption edits (emphasis + emoji) -> layout proposal -> approval -> render. Shows the `fit` layout, the Hormozi highlight, emoji (world, trophy), headline option and progress bar. `layout_proposal_preview.png` is the image the user is shown before approving.

## Newest layouts and auto features, real renders (`rendered/real/new_features_sheet.png`)
Left to right: `circle_facecam` with the webcam box found automatically (real face composited into a moving game frame, IoU 0.99 on this case), `split` with the same auto box, `story_card` (white card + text, captions in the auto-chosen gaming style), `speaker_pip` (preset pair; the window shows the other person's crop, here mostly background). Measured webcam detection over 8 corner/border cases: mean IoU 0.96, min 0.88.
