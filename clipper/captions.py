"""ASS caption generator (deterministic, libass-rendered). Word timing comes straight from the transcript."""
import re
from .boundaries import END

W, H = 1080, 1920
WHITE, YELLOW, BLACK, GREEN = "&H00FFFFFF", "&H0000E5FF", "&H00000000", "&H0055FF55"  # &HAABBGGRR
# mode: pop = whole chunk, active word recoloured | karaoke = \kf fill | plain = no highlight
PRESETS = {
    "bold_pop":    dict(font="Arial Black", size=86, hi=YELLOW, outline=7, border=1, mv=430, words=3, mode="pop", upper=True),
    "karaoke":     dict(font="Arial", size=78, hi=GREEN, outline=6, border=1, mv=430, words=4, mode="karaoke", upper=False),
    "clean_lower": dict(font="Arial", size=54, hi=WHITE, outline=3, border=1, mv=300, words=7, mode="plain", upper=False),
    "boxed":       dict(font="Arial", size=62, hi=YELLOW, outline=14, border=3, mv=380, words=5, mode="pop", upper=False),
}


def _t(x):
    x = max(0.0, x)
    return f"{int(x // 3600)}:{int(x % 3600 // 60):02d}:{x % 60:05.2f}"


def _clean(w):
    return re.sub(r"[{}\\]", "", w)


def chunks(words, p, scale=1.0):
    """Group words into short lines; break on punctuation, pauses, word count, or estimated overflow."""
    maxw = int((W - 160) / (p["size"] * scale * 0.58))  # approx chars per line, bold sans
    out, cur = [], []
    for k, w in enumerate(words):
        cur.append(w)
        nxt = words[k + 1] if k + 1 < len(words) else None
        n = sum(len(x["w"]) + 1 for x in cur) + (len(nxt["w"]) + 1 if nxt else 0)
        if nxt is None or len(cur) >= p["words"] or END.search(w["w"]) or nxt["s"] - w["e"] > 0.6 or n > maxw:
            out.append(cur)
            cur = []
    return out


def build(words, start, preset="bold_pop", scale=1.0, y_offset=0):
    """words: only the clip's words (absolute times). start: clip start in source time."""
    p = PRESETS[preset]
    size, mv = int(p["size"] * scale), p["mv"] + y_offset
    lines = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {W}", f"PlayResY: {H}", "WrapStyle: 2", "",
        "[V4+ Styles]",
        "Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding",
        # karaoke: primary = filled colour, secondary = not-yet-sung colour
        f"Style: Default,{p['font']},{size},{p['hi'] if p['mode'] == 'karaoke' else WHITE},{WHITE},{BLACK},&H80000000,-1,0,0,0,100,100,0,0,{p['border']},{p['outline']},0,2,80,80,{mv},1",
        "", "[Events]", "Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text",
    ]
    f = (lambda s: s.upper()) if p["upper"] else (lambda s: s)
    for ch in chunks(words, p, scale):
        txt = [f(_clean(w["w"])) for w in ch]
        t0, t1 = ch[0]["s"] - start, ch[-1]["e"] - start
        if p["mode"] == "plain":
            lines.append(f"Dialogue: 0,{_t(t0)},{_t(t1)},Default,,0,0,0,,{' '.join(txt)}")
        elif p["mode"] == "karaoke":
            body = " ".join(f"{{\\kf{max(1, round((w['e'] - w['s']) * 100))}}}{t}" for w, t in zip(ch, txt))
            lines.append(f"Dialogue: 0,{_t(t0)},{_t(t1)},Default,,0,0,0,,{body}")
        else:
            for k, w in enumerate(ch):
                a = w["s"] - start
                b = (ch[k + 1]["s"] - start) if k + 1 < len(ch) else t1
                body = " ".join(f"{{\\c{p['hi']}&}}{t}{{\\c{WHITE}&}}" if j == k else t for j, t in enumerate(txt))
                lines.append(f"Dialogue: 0,{_t(a)},{_t(max(b, a + 0.05))},Default,,0,0,0,,{body}")
    return "\n".join(lines) + "\n"
