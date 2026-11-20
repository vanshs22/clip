"""Renders still previews of caption styles: our 9 clipper-skill presets + proposed viral styles.
PIL mock-up (not libass): good enough to compare looks. Run: python make_demo.py <clipper-skill/clipper/fonts dir>"""
import sys
from pathlib import Path
from PIL import Image, ImageDraw, ImageFilter, ImageFont

FONTS = Path(sys.argv[1] if len(sys.argv) > 1 else "clipper-skill/clipper/fonts")
OUT = Path(__file__).parent / "ours-vs-proposed"
W, H = 540, 960
TEXT = "this changed everything for me".split()
ACTIVE = 2
F = {"pe": "Poppins-ExtraBold.ttf", "pb": "Poppins-Bold.ttf", "pm": "Poppins-Medium.ttf", "an": "Anton-Regular.ttf", "be": "BebasNeue-Regular.ttf"}
WH, YE, GR, CY, OR, PK, GY, RD, MG, BK = "#fff", "#ffe500", "#55ff55", "#00e5ff", "#ff8000", "#ff69b4", "#c8c8c8", "#ff2d2d", "#ff00ff", "#000"
# font, size(@1080 wide), color, active color, stroke, words/line, y(frac of height), upper, extras: pill, box, glow, solo
OURS = {
    "bold_pop": dict(f="pe", s=86, hi=YE, st=7, n=3, y=.77, up=1),
    "karaoke": dict(f="pb", s=78, hi=GR, st=6, n=4, y=.77),
    "clean_lower": dict(f="pm", s=54, hi=WH, st=3, n=7, y=.84),
    "boxed": dict(f="pb", s=62, hi=YE, st=0, n=5, y=.80, box="#000a"),
    "minimal_white": dict(f="pm", s=50, hi=WH, st=2, n=8, y=.86),
    "highlight_words": dict(f="pb", s=70, hi=OR, st=5, n=4, y=.78),
    "podcast_speaker": dict(f="pb", s=60, hi=YE, st=5, n=6, y=.81),
    "gaming_bold": dict(f="an", s=96, hi=CY, st=8, n=3, y=.75, up=1),
    "cinematic": dict(f="be", s=84, c=GY, hi=WH, st=2, n=6, y=.87, up=1),
}
NEW = {
    "hormozi_pill": dict(f="an", s=92, hi=WH, st=5, n=3, y=.70, up=1, pill="#00bf49"),
    "mrbeast_yellow": dict(f="an", s=110, c=YE, hi=RD, st=12, n=3, y=.68, up=1),
    "tiktok_pink": dict(f="pe", s=84, hi=PK, st=8, n=3, y=.72, up=1),
    "neon_glow": dict(f="pe", s=74, c=CY, hi=MG, st=0, n=3, y=.72, up=1, glow=1),
    "single_word_slam": dict(f="an", s=190, c=WH, hi=YE, st=14, n=1, y=.55, up=1),
    "highlighter_marker": dict(f="pe", s=66, c=BK, hi=BK, st=0, n=3, y=.72, up=1, box=YE),
    "red_alert_hook": dict(f="an", s=100, c=WH, hi=WH, st=0, n=3, y=.70, up=1, box=RD),
    "gradient_fire": dict(f="an", s=104, c=YE, hi=OR, st=10, n=3, y=.70, up=1, glow=1),
}


def bg():
    im = Image.new("RGB", (W, H))
    d = ImageDraw.Draw(im)
    for y in range(H):
        t = y / H
        d.line([(0, y), (W, y)], fill=(int(25 + 40 * t), int(30 + 15 * t), int(60 + 50 * (1 - t))))
    d.ellipse([W * .22, H * .22, W * .78, H * .50], fill=(150, 120, 105))  # face
    d.ellipse([W * .08, H * .46, W * .92, H * 1.0], fill=(40, 45, 70))  # shoulders
    return im


def chunk(n):
    i = ACTIVE // n * n
    return TEXT[i:i + n], ACTIVE - i


def render(name, p):
    im = bg().convert("RGBA")
    font = ImageFont.truetype(str(FONTS / F[p["f"]]), p["s"] // 2)
    words, act = chunk(p["n"])
    words = [w.upper() if p.get("up") else w for w in words]
    size = p["s"] // 2
    while True:  # shrink to fit like a real renderer would wrap
        gap = font.getlength(" ")
        widths = [font.getlength(w) for w in words]
        total = sum(widths) + gap * (len(words) - 1)
        if total <= W * .9: break
        size -= 2
        font = ImageFont.truetype(str(FONTS / F[p["f"]]), size)
    x, y = (W - total) / 2, H * p["y"]
    asc, desc = font.getmetrics()
    if p.get("box") and not p.get("glow"):  # one box behind the whole line
        ImageDraw.Draw(im, "RGBA").rounded_rectangle([x - 14, y - 6, x + total + 14, y + asc + desc + 6], 10, fill=p["box"])
    for i, w in enumerate(words):
        on = i == act
        col = p.get("hi", WH) if on else p.get("c", WH)
        if on and p.get("pill"):
            ImageDraw.Draw(im, "RGBA").rounded_rectangle([x - 8, y - 2, x + widths[i] + 8, y + asc + desc + 2], 12, fill=p["pill"])
        if p.get("glow"):
            g = Image.new("RGBA", im.size, (0, 0, 0, 0))
            ImageDraw.Draw(g).text((x, y), w, font=font, fill=col, stroke_width=3, stroke_fill=col)
            im.alpha_composite(g.filter(ImageFilter.GaussianBlur(9)))
        sk = p["st"] // 2
        ImageDraw.Draw(im).text((x, y), w, font=font, fill=col, stroke_width=sk, stroke_fill=BK if not p.get("glow") else "#0000")
        x += widths[i] + gap
    return im.convert("RGB")


def sheet(group, title, cols):
    from math import ceil
    ims = [(n, render(n, p)) for n, p in group.items()]
    cw, ch = W // 2, H // 2
    rows = ceil(len(ims) / cols)
    s = Image.new("RGB", (cols * (cw + 10) + 10, rows * (ch + 40) + 50), "#111")
    d = ImageDraw.Draw(s)
    lab = ImageFont.truetype(str(FONTS / F["pb"]), 16)
    d.text((10, 12), title, font=lab, fill=WH)
    for k, (n, im) in enumerate(ims):
        ox, oy = 10 + (k % cols) * (cw + 10), 50 + (k // cols) * (ch + 40)
        s.paste(im.resize((cw, ch), Image.LANCZOS), (ox, oy))
        d.text((ox, oy + ch + 8), n, font=lab, fill=GY)
    return s


OUT.mkdir(exist_ok=True)
for grp, tag in ((OURS, "ours"), (NEW, "proposed")):
    for n, p in grp.items():
        render(n, p).save(OUT / f"{tag}_{n}.png", optimize=True)
sheet(OURS, "OURS (clipper-skill 0.4.0): 9 presets", 5).save(OUT / "SHEET_ours.png", optimize=True)
sheet(NEW, "PROPOSED viral styles", 4).save(OUT / "SHEET_proposed.png", optimize=True)
print("ok")
