# Clipping caption styles: other repos vs ours

Sources (checked from each repo's code/README, shallow clones):
- SupoClip (FujiwaraChoki/supoclip): `backend/src/caption_templates.py`. Templates: default, hormozi, mrbeast, minimal, tiktok, neon, podcast. Per-template fields: active-word pop, emoji injection, emphasis colour on keywords, pill behind active word, uppercase, glow, words per line, Y position.
- OpenShorts (faris-sait/openshorts): Remotion subtitles, 4 animations (none, word-highlight, pop, karaoke), font/colour/border picker.
- Chopify (mehbul/chopify): 13 presets: default, clean, minimal, bold, boxed, karaoke, pop, neon, typewriter, retro, hormozi, mrbeast, podcast. Also auto emphasis + emoji, brand presets, SRT export.
- AutoShorts (divyaprakash0426/autoshorts): `CAPTION_STYLE=auto` picks a style per detected moment (gaming, funny, dramatic, minimal, genz, story_*).
- Clips Kitty (ColinGPT9/clips-studio): editable word-synced burned-in captions, colour/size, second speaker gets a different colour.

Ours (clipper-skill 0.4.0, `clipper/captions.py`): bold_pop, karaoke, clean_lower, boxed, minimal_white, highlight_words, podcast_speaker, gaming_bold, cinematic.

## What others have that we lack
1. Pill behind the active word (Hormozi). Highest-impact missing style.
2. Emoji on keywords and a separate emphasis colour for power words.
3. Glow (neon).
4. Per-moment auto style (AutoShorts).
5. Typewriter and retro looks (Chopify).

## Proposed additions (previews in `ours-vs-proposed/`)
hormozi_pill, mrbeast_yellow, tiktok_pink, neon_glow, single_word_slam (one huge word, centred), highlighter_marker, red_alert_hook (for the first 2-3 s), gradient_fire.
Previews are PIL mock-ups with our bundled fonts, not libass output. Emoji, typewriter and per-word animation are not shown in stills.

Files: `reference/<repo>/` (their screenshots and frames pulled from their demo GIFs), `ours-vs-proposed/SHEET_*.png` (contact sheets), `make_demo.py` (regenerates previews).
