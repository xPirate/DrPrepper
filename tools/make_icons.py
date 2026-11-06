"""Draw the built-in branding icons (original artwork). Run: python tools/make_icons.py

Deliberately avoids protected emblems: no red cross or red crescent on white
(Geneva Conventions) and no Star of Life (registered mark for certified EMS).
"""
import math
from pathlib import Path

from PIL import Image, ImageDraw

OUT = Path(__file__).resolve().parents[1] / "src/drprepper/server/static/icons"
S = 1024           # draw large, downsample for smooth edges
W = (255, 255, 255, 255)


def tile(color):
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((24, 24, S - 24, S - 24), radius=210, fill=color)
    return im, d


def plus(d, cx, cy, arm, thick, fill, r=40):
    d.rounded_rectangle((cx - thick / 2, cy - arm, cx + thick / 2, cy + arm), radius=r, fill=fill)
    d.rounded_rectangle((cx - arm, cy - thick / 2, cx + arm, cy + thick / 2), radius=r, fill=fill)


def stroke(d, pts, width, fill):
    """Thick smooth line: densely placed discs along the path (PIL's wide lines are jagged)."""
    r = width / 2
    for (x0, y0), (x1, y1) in zip(pts, pts[1:]):
        n = max(1, int(math.hypot(x1 - x0, y1 - y0) / 3))
        for i in range(n + 1):
            x, y = x0 + (x1 - x0) * i / n, y0 + (y1 - y0) * i / n
            d.ellipse((x - r, y - r, x + r, y + r), fill=fill)


def heart_poly(cx, cy, size, n=200):
    pts = []
    for i in range(n):
        t = 2 * math.pi * i / n
        x = 16 * math.sin(t) ** 3
        y = 13 * math.cos(t) - 5 * math.cos(2 * t) - 2 * math.cos(3 * t) - math.cos(4 * t)
        pts.append((cx + x * size, cy - y * size))
    return pts


def icon_cross():           # green medical cross (not red-on-white)
    im, d = tile((31, 111, 92, 255))
    plus(d, S / 2, S / 2, 300, 210, W)
    return im


def icon_heartbeat():
    im, d = tile((178, 38, 30, 255))
    d.polygon(heart_poly(S / 2, S / 2 - 10, 22), fill=W)
    line = [(150, 520), (380, 520), (440, 400), (520, 660), (590, 470), (630, 520), (874, 520)]
    d.line(line, fill=(178, 38, 30, 255), width=58, joint="curve")
    for x, y in (line[0], line[-1]):
        d.ellipse((x - 29, y - 29, x + 29, y + 29), fill=(178, 38, 30, 255))
    return im


def icon_capsule():
    im, d = tile((45, 90, 140, 255))
    cap = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    c = ImageDraw.Draw(cap)
    c.rounded_rectangle((312, 160, 712, 864), radius=200, fill=W)
    c.rectangle((312, 512, 712, 864), fill=(255, 196, 80, 255))
    c.rounded_rectangle((312, 160, 712, 864), radius=200, outline=W, width=40)
    mask = Image.new("L", (S, S), 0)
    ImageDraw.Draw(mask).rounded_rectangle((312, 160, 712, 864), radius=200, fill=255)
    cap.putalpha(mask)
    cap = cap.rotate(-40, resample=Image.BICUBIC, center=(S / 2, S / 2))
    im.alpha_composite(cap)
    return im


def icon_stethoscope():
    im, d = tile((60, 64, 82, 255))
    # ear tubes and U
    u = [(300, 240)] + [(300, 430)] + [(512 + 212 * math.cos(a), 430 + 212 * math.sin(a))
                                       for a in [math.pi - i * math.pi / 60 for i in range(61)]] + [(724, 430), (724, 240)]
    u = [(x, 430 + (y - 430)) if y >= 430 else (x, y) for x, y in u]
    u = [u[0], u[1]] + [(512 - 212 * math.cos(i * math.pi / 60), 430 + 212 * math.sin(i * math.pi / 60)) for i in range(61)] + [u[-2], u[-1]]
    stroke(d, u, 56, W)
    for x in (300, 724):
        d.ellipse((x - 46, 196, x + 46, 288), fill=W)
    # tube from the bottom of the U down and across to the chest piece
    tube = [(512, 642), (512, 720)] + [(512 + 130 - 130 * math.cos(i * math.pi / 40), 720 + 110 * math.sin(i * math.pi / 40))
                                       for i in range(41)] + [(772, 720), (772, 690)]
    stroke(d, tube, 50, W)
    d.ellipse((682, 520, 862, 700), fill=W)
    d.ellipse((732, 570, 812, 650), fill=(60, 64, 82, 255))
    return im


def icon_kit():
    im, d = tile((214, 120, 40, 255))
    d.rounded_rectangle((380, 200, 644, 330), radius=40, outline=W, width=50)   # handle
    d.rounded_rectangle((170, 300, 854, 820), radius=90, fill=W)
    plus(d, S / 2, 560, 150, 100, (214, 120, 40, 255), r=20)
    return im


def icon_asclepius():       # rod of Asclepius: one staff, one serpent
    im, d = tile((36, 80, 72, 255))
    d.rounded_rectangle((484, 140, 540, 890), radius=28, fill=W)
    pts = [(512 + math.sin(i / 239 * math.pi * 3.2) * 150, 780 - i / 239 * 540) for i in range(240)]
    stroke(d, pts, 46, W)
    hx, hy = pts[-1]
    d.ellipse((hx - 58, hy - 46, hx + 58, hy + 46), fill=W)
    d.ellipse((hx + 6, hy - 18, hx + 30, hy + 6), fill=(36, 80, 72, 255))   # eye
    return im


def icon_tent():            # field hospital tent
    im, d = tile((92, 74, 46, 255))
    d.polygon([(512, 170), (870, 800), (154, 800)], fill=W)
    d.polygon([(512, 450), (610, 800), (414, 800)], fill=(92, 74, 46, 255))
    plus(d, 512, 330, 80, 54, (92, 74, 46, 255), r=12)
    d.line([(512, 170), (512, 120)], fill=W, width=26)
    return im


def icon_shield():
    im, d = tile((48, 70, 160, 255))
    d.polygon([(512, 150), (830, 260), (800, 560), (512, 880), (224, 560), (194, 260)], fill=W)
    plus(d, 512, 480, 160, 110, (48, 70, 160, 255), r=22)
    return im


ICONS = {"cross": icon_cross, "heartbeat": icon_heartbeat, "capsule": icon_capsule, "stethoscope": icon_stethoscope,
         "kit": icon_kit, "asclepius": icon_asclepius, "tent": icon_tent, "shield": icon_shield}

if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    for key, fn in ICONS.items():
        fn().resize((256, 256), Image.LANCZOS).save(OUT / f"{key}.png", optimize=True)
        print("wrote", OUT / f"{key}.png")
