"""ASS caption generator (deterministic, libass-rendered). Word timing comes straight from the transcript.
Fonts are bundled (SIL OFL, clipper/fonts) and passed to libass via fontsdir, so rendering never depends on system fonts (Arial is not on Linux)."""
import json, re, shutil
from pathlib import Path
from .boundaries import END

W, H = 1080, 1920
FONTS = Path(__file__).parent / "fonts"
WHITE, YELLOW, BLACK, GREEN, CYAN, ORANGE, PINK, GREY = "&H00FFFFFF", "&H0000E5FF", "&H00000000", "&H0055FF55", "&H00FFE600", "&H000080FF", "&H00B469FF", "&H00C8C8C8"  # &HAABBGGRR
FONT_FAMILIES = {"Poppins ExtraBold", "Poppins", "Poppins Medium", "Anton", "Bebas Neue"}  # families present in clipper/fonts
# mode: pop = whole chunk, active word recoloured | karaoke = \kf fill | plain = no highlight | speaker = plain, colour by who speaks
# border: 1 = outline+shadow, 3 = opaque box.  pos: bottom margin (px of 1920); anim: none | pop (scale-in) | fade
BASE = dict(font="Poppins ExtraBold", size=80, primary=WHITE, hi=YELLOW, outline_color=BLACK, outline=6, shadow=0, border=1, box_color="&H80000000",
            mv=430, words=3, max_chars=0, mode="pop", upper=False, anim="none", bold=0, spacing=0, align=2)
PRESETS = {
    "bold_pop":        dict(size=86, outline=7, words=3, upper=True, anim="pop"),
    "karaoke":         dict(font="Poppins Bold", size=78, hi=GREEN, outline=6, words=4, mode="karaoke"),
    "clean_lower":     dict(font="Poppins Medium", size=54, outline=3, shadow=1, mv=300, words=7, mode="plain"),
    "boxed":           dict(font="Poppins Bold", size=62, outline=14, border=3, mv=380, words=5, mode="pop"),
    "minimal_white":   dict(font="Poppins Medium", size=50, outline=2, shadow=2, mv=280, words=8, mode="plain", anim="fade"),
    "highlight_words": dict(font="Poppins Bold", size=70, hi=ORANGE, outline=5, words=4, mode="pop"),
    "podcast_speaker": dict(font="Poppins Bold", size=60, outline=5, words=6, mode="speaker", mv=360),
    "gaming_bold":     dict(font="Anton", size=96, hi=CYAN, outline=8, shadow=3, words=3, upper=True, anim="pop", mv=470),
    "cinematic":       dict(font="Bebas Neue", size=84, primary=GREY, hi=WHITE, outline=2, shadow=2, words=6, mode="pop", mv=250, spacing=3, upper=True, anim="fade"),
}
SPEAKER_COLORS = [YELLOW, CYAN, PINK, GREEN]


def style(name_or_dict, style_file=None):
    """-> full style dict. `custom` takes overrides from a JSON file (style_file) or a dict; unknown keys are rejected, fonts must be bundled
    or you accept system lookup (a warning key `font_is_system` is set)."""
    if isinstance(name_or_dict, dict) or name_or_dict == "custom":
        over = name_or_dict if isinstance(name_or_dict, dict) else json.loads(Path(style_file).read_text())
        bad = set(over) - set(BASE)
        if bad:
            raise ValueError(f"unknown caption style keys: {sorted(bad)}")
        st = {**BASE, **PRESETS["bold_pop"], **over}
    else:
        if name_or_dict not in PRESETS:
            raise ValueError(f"unknown caption style {name_or_dict!r}; choose from {sorted(PRESETS)} or custom")
        st = {**BASE, **PRESETS[name_or_dict]}
    st["font_is_system"] = st["font"] not in FONT_FAMILIES
    return st


def _t(x):
    x = max(0.0, x)
    return f"{int(x // 3600)}:{int(x % 3600 // 60):02d}:{x % 60:05.2f}"


def _clean(w):
    return re.sub(r"[{}\\]", "", w)


def chunks(words, p, scale=1.0):
    """Group words into short lines; break on punctuation, pauses, word count, character limit, or estimated overflow (never overflows the frame)."""
    maxw = int((W - 160) / (p["size"] * scale * 0.58))
    if p.get("max_chars"):
        maxw = min(maxw, int(p["max_chars"]))
    out, cur = [], []
    for k, w in enumerate(words):
        cur.append(w)
        nxt = words[k + 1] if k + 1 < len(words) else None
        n = sum(len(x["w"]) + 1 for x in cur) + (len(nxt["w"]) + 1 if nxt else 0)
        if nxt is None or len(cur) >= p["words"] or END.search(w["w"]) or nxt["s"] - w["e"] > 0.6 or n > maxw:
            out.append(cur)
            cur = []
    return out


def overflow(words, p, scale=1.0):
    """Number of caption groups whose estimated width exceeds the safe width (should always be 0 given chunks())."""
    return sum(1 for ch in chunks(words, p, scale) if sum(len(x["w"]) + 1 for x in ch) * p["size"] * scale * 0.58 > W - 100)


def _fx(p, dur):
    if p["anim"] == "pop":
        return r"{\fscx70\fscy70\t(0,120,\fscx100\fscy100)}"
    if p["anim"] == "fade":
        return r"{\fad(120,80)}"
    return ""


def build(words, start, preset="bold_pop", scale=1.0, y_offset=0, style_file=None, speaker_at=None):
    """words: only the clip's words (absolute times). start: clip start in source time. speaker_at(t) -> speaker index or None (podcast_speaker)."""
    p = style(preset, style_file) if not isinstance(preset, dict) or "font_is_system" not in preset else preset
    size, mv = int(p["size"] * scale), p["mv"] + y_offset
    base = p["hi"] if p["mode"] == "karaoke" else p["primary"]
    lines = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {W}", f"PlayResY: {H}", "WrapStyle: 2", "ScaledBorderAndShadow: yes", "",
        "[V4+ Styles]",
        "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding",
        f"Style: Default,{p['font']},{size},{base},{p['primary']},{p['box_color'] if p['border'] == 3 else p['outline_color']},&H80000000,{p['bold']},0,0,0,100,100,{p['spacing']},0,{p['border']},{p['outline']},{p['shadow']},{p['align']},80,80,{mv},1",
        "", "[Events]", "Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text",
    ]
    f = (lambda s: s.upper()) if p["upper"] else (lambda s: s)
    for ch in chunks(words, p, scale):
        txt = [f(_clean(w["w"])) for w in ch]
        t0, t1 = ch[0]["s"] - start, ch[-1]["e"] - start
        fx = _fx(p, t1 - t0)
        if p["mode"] in ("plain", "speaker"):
            col = ""
            if p["mode"] == "speaker" and speaker_at:
                k = speaker_at(ch[0]["s"])
                col = f"{{\\c{SPEAKER_COLORS[k % len(SPEAKER_COLORS)]}&}}" if k is not None else ""
            lines.append(f"Dialogue: 0,{_t(t0)},{_t(t1)},Default,,0,0,0,,{fx}{col}{' '.join(txt)}")
        elif p["mode"] == "karaoke":
            body = " ".join(f"{{\\kf{max(1, round((w['e'] - w['s']) * 100))}}}{t}" for w, t in zip(ch, txt))
            lines.append(f"Dialogue: 0,{_t(t0)},{_t(t1)},Default,,0,0,0,,{fx}{body}")
        else:
            for k, w in enumerate(ch):
                a = w["s"] - start
                b = (ch[k + 1]["s"] - start) if k + 1 < len(ch) else t1
                body = " ".join(f"{{\\c{p['hi']}&}}{t}{{\\c{p['primary']}&}}" if j == k else t for j, t in enumerate(txt))
                lines.append(f"Dialogue: 0,{_t(a)},{_t(max(b, a + 0.05))},Default,,0,0,0,,{fx if k == 0 else ''}{body}")
    return "\n".join(lines) + "\n"


def install_fonts(dst):
    """Copy bundled fonts next to the ASS file so `fontsdir=fonts` is a short RELATIVE path (no Windows drive-letter escaping problems)."""
    d = Path(dst) / "fonts"
    if not d.exists():
        shutil.copytree(FONTS, d, ignore=shutil.ignore_patterns("OFL*"))
    return "fonts"
