"""Wireframe sheets of 9:16 clip LAYOUTS (where cam / content / title / captions go). Run: python make_layouts.py <fonts dir>"""
import sys
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

FONTS = Path(sys.argv[1] if len(sys.argv) > 1 else "clipper-skill/clipper/fonts")
OUT = Path(__file__).parent / "layouts"
W, H = 270, 480
FACE, GAME, BLUR, TITLE, CAP, UI = "#b08a78", "#2f6f4f", "#3a3f6a", "#d63a3a", "#f2c200", "#7a4fb5"
FONT = ImageFont.truetype(str(FONTS / "Poppins-Bold.ttf"), 12)
BIG = ImageFont.truetype(str(FONTS / "Poppins-Bold.ttf"), 16)


def cell(boxes, caption=True, bg=BLUR):
    """boxes: (kind, x0, y0, x1, y1[, round]) as fractions of the frame; kind = cam|game|title|ui|card"""
    im = Image.new("RGB", (W, H), bg)
    d = ImageDraw.Draw(im)
    for b in boxes:
        k, x0, y0, x1, y1 = b[:5]
        r = [x0 * W, y0 * H, x1 * W, y1 * H]
        col = {"cam": FACE, "game": GAME, "title": TITLE, "ui": UI, "card": "#eeeeee", "bar": "#ffffff", "filler": "#b5651d"}[k]
        if len(b) > 5:
            d.ellipse(r, fill=col, outline="#fff", width=2)
        else:
            d.rectangle(r, fill=col, outline="#fff", width=1)
        d.text(((r[0] + r[2]) / 2, (r[1] + r[3]) / 2), {"cam": "CAM", "game": "CONTENT", "title": "HOOK", "ui": "UI", "card": "CARD", "bar": "", "filler": "FILLER"}[k],
               font=FONT, fill="#fff" if k != "card" else "#222", anchor="mm")
    if caption:
        d.rectangle([20, H * .74, W - 20, H * .74 + 26], fill=CAP)
        d.text((W / 2, H * .74 + 13), "CAPTIONS", font=FONT, fill="#000", anchor="mm")
    return im


OURS = {
    "fill (face-follow crop)": [("cam", 0, 0, 1, 1)],
    "fit (blurred backdrop)": [("game", 0, .3, 1, .7)],
    "split (2 people stacked)": [("cam", 0, 0, 1, .5), ("cam", 0, .5, 1, 1)],
}
OTHERS = {
    "KITTY split (cam top)": [("cam", 0, 0, 1, .38), ("game", 0, .38, 1, 1)],
    "KITTY basecam": [("game", 0, 0, 1, .62), ("cam", 0, .62, 1, 1)],
    "KITTY half": [("cam", 0, 0, 1, .5), ("game", 0, .5, 1, 1)],
    "KITTY small facecam": [("game", 0, 0, 1, 1), ("cam", .62, .05, .95, .2)],
    "KITTY circle facecam": [("game", 0, 0, 1, 1), ("cam", .6, .05, .95, .24, 1)],
    "KITTY game UI": [("cam", 0, 0, 1, .3), ("game", 0, .3, 1, 1), ("ui", .05, .3, .4, .4)],
    "KITTY mosaic": [("cam", 0, 0, .5, .3), ("ui", .5, 0, 1, .3), ("game", 0, .3, 1, 1)],
    "KITTY dual facecam": [("game", 0, 0, 1, 1), ("cam", .15, .04, .45, .2, 1), ("cam", .55, .04, .85, .2, 1)],
    "KITTY duo split": [("cam", 0, 0, .5, .3), ("cam", .5, 0, 1, .3), ("game", 0, .3, 1, 1)],
    "SUPOCLIP interview cuts (stack / close-up / graphic)": [("cam", 0, 0, 1, .5), ("cam", 0, .5, 1, 1)],
    "OPENSHORTS TRACK / GENERAL per scene": [("cam", 0, 0, 1, 1)],
}
NEW = {
    "hook title card on top": [("title", 0, .08, 1, .2), ("game", 0, .26, 1, .66)],
    "story card + satisfying filler": [("card", .05, .05, .95, .3), ("filler", 0, .55, 1, 1)],
    "content top + filler bottom": [("game", 0, 0, 1, .5), ("filler", 0, .5, 1, 1)],
    "reaction / stitch": [("game", 0, 0, 1, .5), ("cam", 0, .5, 1, 1)],
    "podcast auto-switch speaker": [("cam", 0, 0, 1, 1), ("cam", .62, .06, .94, .2, 1)],
    "podcast 2-stack + name tags": [("cam", 0, 0, 1, .5), ("cam", 0, .5, 1, 1), ("title", .05, .02, .45, .07)],
    "progress bar + Part N": [("game", 0, .12, 1, .72), ("bar", 0, .955, .6, .975), ("title", .72, .03, .95, .08)],
    "circle cam on content": [("game", 0, .15, 1, .75), ("cam", .62, .6, .95, .79, 1)],
}


def sheet(group, title, cols, name):
    from math import ceil
    rows = ceil(len(group) / cols)
    cw = W + 12
    s = Image.new("RGB", (cols * cw + 12, rows * (H + 56) + 44), "#111")
    d = ImageDraw.Draw(s)
    d.text((12, 12), title, font=BIG, fill="#fff")
    for i, (n, b) in enumerate(group.items()):
        x, y = 12 + i % cols * cw, 44 + i // cols * (H + 56)
        im = cell(b, caption=n != "fit (blurred backdrop)" or True)
        s.paste(im, (x, y))
        short = n if len(n) < 30 else n[:28] + ".."
        d.text((x, y + H + 6), short, font=FONT, fill="#ccc")
        im.save(OUT / f"{name}_{n.split('(')[0].strip().replace(' ', '_').replace('/', '-')[:30]}.png")
    s.save(OUT / f"SHEET_{name}.png", optimize=True)


OUT.mkdir(exist_ok=True)
sheet(OURS, "OURS (clipper-skill 0.4.0): 3 layouts", 3, "ours")
sheet(OTHERS, "OTHER REPOS: layouts we don't have", 6, "others")
sheet(NEW, "PROPOSED viral layouts", 4, "proposed")
print("ok")
