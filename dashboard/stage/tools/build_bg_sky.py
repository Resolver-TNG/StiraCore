"""The world outside the window for the Stage background (#341): sky variants and the city.

Offline, imported by build_bg.py. Each variant (night / dawn / rain) gives three images:
``sky_<id>`` (sky, moon or clouds, the far skyline; the deepest layer), ``city_<id>`` (the
nearer blocks: lit windows, rooftop gear, signs, the elevated line) and ``window_glow_<id>``
(the light the window throws when the web is used; black = no light). The rain variant also
gives ``glass_rain`` (drops on the pane). Everything is drawn in code; signs carry abstract
glyph blocks, never real text or logos.
"""

from __future__ import annotations

import math

import numpy as np
from build_bg import BAYER4, WIN, Canvas, H, W, rgb

# Region drawn behind the window opening (wider than the opening: the layers move a little).
SKY = (WIN[0] - 30, WIN[1] - 18, WIN[2] + 60, WIN[3] + 34)

# Variant palettes. Every colour stays dark and muted so that the character in front reads
# first; only the small lit windows and sign strokes are bright.
VARIANTS = {
    "night": {
        "sky": ["#0a0c22", "#0f1230", "#151a3e", "#1e2150", "#2c2860", "#40306c", "#56386f"],
        "curve": 1.7,
        "far": "#171a3c",
        "far_lit": ["#3a3a6e", "#4a4278"],
        "bd": ["#07081a", "#0b0d22", "#10132c", "#151a38"],
        "edge": "#232a52",
        "lit": ["#e9c06a", "#e9c06a", "#d8b46e", "#7fc7d8", "#c983c9", "#d9d6f2"],
        "lit_p": 0.17,
        "stars": 26,
        "moon": True,
        "clouds": 0,
        "haze": 0.0,
        "signs": ["#d26fb4", "#5ec8d8", "#e0a24c"],
        "glow": ["#24285a", "#3a3170"],
    },
    "dawn": {
        "sky": [
            "#141a40",
            "#20224e",
            "#36295c",
            "#5a3466",
            "#83416a",
            "#b05a66",
            "#d27f6a",
            "#e9a774",
        ],
        "curve": 1.25,
        "far": "#3f2c5c",
        "far_lit": ["#6a4a72", "#7a5576"],
        "bd": ["#120f26", "#181430", "#1f1938", "#271e42"],
        "edge": "#6a4766",
        "lit": ["#e8b46a", "#e3a35c", "#cfa98a"],
        "lit_p": 0.05,
        "stars": 6,
        "moon": False,
        "clouds": 2,
        "haze": 0.0,
        "signs": ["#b06a9a", "#5a9aa8", "#c08a54"],
        "glow": ["#4a2c50", "#7a4458"],
    },
    "rain": {
        "sky": ["#0c0e20", "#12152c", "#181d38", "#1f2544", "#282e50", "#333a5c"],
        "curve": 1.3,
        "far": "#1a1f3a",
        "far_lit": ["#2e3456", "#353b5e"],
        "bd": ["#090b1a", "#0d1022", "#12162a", "#171c33"],
        "edge": "#262d4c",
        "lit": ["#c9a666", "#c9a666", "#76aebc", "#b07fae", "#b9b8d4"],
        "lit_p": 0.13,
        "stars": 0,
        "moon": False,
        "clouds": 4,
        "haze": 0.35,
        "signs": ["#b0609c", "#4fa6b6", "#bf8c4c"],
        "glow": ["#1c2546", "#28305a"],
    },
}


def _hex(c) -> str:
    return "#{:02x}{:02x}{:02x}".format(*tuple(int(v) for v in c[:3]))


def _mix(a: str, b: str, t: float) -> str:
    ca, cb = rgb(a).astype(float), rgb(b).astype(float)
    c = np.round(ca + (cb - ca) * t).astype(int)
    return "#{:02x}{:02x}{:02x}".format(*tuple(c))


def sky(vid: str, rng: np.random.Generator) -> Canvas:
    v = VARIANTS[vid]
    c = Canvas()
    x, y, w, h = SKY
    c.grad(x, y, w, h, v["sky"], curve=v["curve"])
    # stars: single pixels, a few with a 1 px cross
    for _ in range(v["stars"]):
        sx, sy = int(rng.integers(x + 2, x + w - 2)), int(rng.integers(y + 2, y + h // 2))
        tone = "#8f92c4" if rng.random() < 0.7 else "#c8c8ea"
        c.px(sx, sy, tone)
        if rng.random() < 0.15:
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                c.px(sx + dx, sy + dy, "#4a4c84")
    if v["moon"]:
        mx, my = WIN[0] + 90, WIN[1] + 20
        for r, col in ((10, "#232659"), (9, "#2c2e66")):  # dithered halo
            for yy in range(my - r, my + r + 1):
                for xx in range(mx - r, mx + r + 1):
                    if (xx - mx) ** 2 + (yy - my) ** 2 <= r * r and BAYER4[yy % 4, xx % 4] < 0.5:
                        c.px(xx, yy, col)
        c.disc(mx, my, 6, "#bdb8de")
        c.disc(mx - 1, my - 1, 5, "#dcd8f2")
        c.disc(mx - 2, my - 2, 3, "#ece8fb")
        for cx_, cy_, cr in ((mx + 2, my + 1, 1.5), (mx - 2, my + 3, 1), (mx + 1, my - 3, 1)):
            c.disc(cx_, cy_, cr, "#aaa4cc")
        # terminator shade on the lower right
        for yy in range(my - 6, my + 7):
            for xx in range(mx - 6, mx + 7):
                d = (xx - mx) ** 2 + (yy - my) ** 2
                if d <= 36 and (xx - mx) + (yy - my) > 4 and BAYER4[yy % 4, xx % 4] < 0.6:
                    c.px(xx, yy, "#9890be")
    for k in range(v["clouds"]):
        cy0 = y + 8 + k * 13 + int(rng.integers(0, 6))
        col = _mix(v["sky"][min(2 + k, len(v["sky"]) - 1)], "#5a5a7e", 0.18)
        hi = _mix(col, "#9a8aa8" if vid == "dawn" else "#3c4266", 0.45)
        for xx in range(x, x + w):
            t = math.sin(xx * 0.09 + k * 1.7) * 2.5 + math.sin(xx * 0.031 + k) * 3
            for j in range(9):
                yy = int(cy0 + j + t)
                fall = 1 - abs(j - 4) / 5
                if BAYER4[yy % 4, xx % 4] < fall * 0.85:
                    c.px(xx, yy, hi if j < 3 else col)
    # far skyline: flat silhouettes with a few dim lights and a mast
    base = y + h - 30
    bx = x
    while bx < x + w:
        bw, bh = int(rng.integers(5, 13)), int(rng.integers(8, 26))
        c.rect(bx, base - bh, bw, y + h - (base - bh), v["far"])
        for _ in range(int(bw * bh * 0.03)):
            c.px(int(rng.integers(bx, bx + bw)), int(rng.integers(base - bh + 2, base + 8)),
                 str(rng.choice(v["far_lit"])))  # fmt: skip
        bx += bw + int(rng.integers(0, 2))
    mx = WIN[0] + 30
    c.vline(mx, base - 44, base - 20, v["far"])
    c.vline(mx + 1, base - 36, base - 20, v["far"])
    c.px(mx, base - 45, "#c0505e")
    if v["haze"]:
        _haze(c, v["sky"][4], v["haze"], y + h - 46, y + h)
    return c


def _haze(c: Canvas, col: str, amount: float, y0: int, y1: int) -> None:
    """Dither a flat haze colour over everything drawn between y0 and y1 (rain, distance)."""
    hc = rgb(col)
    for yy in range(max(y0, 0), min(y1, H)):
        for xx in range(W):
            if c.a[yy, xx, 3] and BAYER4[yy % 4, xx % 4] < amount:
                c.a[yy, xx, :3] = hc


def dith(x: int, y: int, t: float) -> bool:
    return BAYER4[y % 4, x % 4] < t


def _glyph(c: Canvas, x: int, y: int, col: str, rng: np.random.Generator) -> None:
    """A 3x3 abstract sign glyph (not a real character)."""
    bits = rng.integers(0, 2, size=(3, 3))
    bits[1, 1] = 1
    for j in range(3):
        for i in range(3):
            if bits[j, i]:
                c.px(x + i, y + j, col)


def _building(c: Canvas, glow: Canvas, v: dict, rng, bx: int, top: int, bw: int, bottom: int,
              tone: int) -> None:  # fmt: skip
    body = v["bd"][tone]
    c.rect(bx, top, bw, bottom - top, body)
    # moonlit / dawn-lit right edge, dark left edge
    c.vline(bx + bw - 1, top, bottom, v["edge"] if tone >= 2 else v["bd"][tone + 1])
    if tone == 3:  # far row: haze toward the bottom
        for yy in range(top, bottom):
            for xx in range(bx, bx + bw):
                if dith(xx, yy, 0.35 * (yy - top) / max(bottom - top, 1)):
                    c.px(xx, yy, v["far"])
    c.vline(bx, top, bottom, v["bd"][max(tone - 1, 0)])
    c.hline(bx, bx + bw - 1, top, v["edge"] if tone else v["bd"][2])
    # window grid: 1x2 or 2x1 cells
    style = int(rng.integers(0, 3))
    sx, sy = (3, 4) if style == 0 else (2, 3) if style == 1 else (4, 3)
    for wy in range(top + 3, bottom - 1, sy):
        lit_row = rng.random() < 0.85
        for wx in range(bx + 2, bx + bw - 2, sx):
            if rng.random() < v["lit_p"] * (1.4 if lit_row else 0.3):
                col = str(rng.choice(v["lit"]))
                c.px(wx, wy, col)
                if style == 0:
                    c.px(wx, wy + 1, _mix(col, body, 0.45))
            else:
                c.px(wx, wy, v["bd"][max(tone - 1, 0)] if tone else v["bd"][1])
    # rooftop gear
    r = rng.random()
    if r < 0.3 and bw >= 8:  # water tank on legs
        tx = bx + int(rng.integers(1, bw - 6))
        c.rect(tx, top - 6, 5, 4, v["bd"][tone])
        c.hline(tx, tx + 4, top - 6, v["edge"])
        c.px(tx, top - 2, v["bd"][tone])
        c.px(tx + 4, top - 2, v["bd"][tone])
        c.px(tx + 1, top - 1, v["bd"][tone])
        c.px(tx + 3, top - 1, v["bd"][tone])
    elif r < 0.55:  # antenna with an aircraft light (static paint)
        ax = bx + bw // 2
        c.vline(ax, top - 9, top - 1, v["bd"][tone])
        c.hline(ax - 1, ax + 1, top - 6, v["bd"][tone])
        c.px(ax, top - 10, "#b84a58")
    elif r < 0.75 and bw >= 10:  # stepped top / AC boxes
        c.rect(bx + 2, top - 3, bw - 4, 3, body)
        c.hline(bx + 2, bx + bw - 3, top - 3, v["edge"])
        c.rect(bx + 3, top - 5, 3, 2, v["bd"][max(tone - 1, 0)])


def _sign(c: Canvas, v: dict, rng, x: int, y: int, n: int, col: str, vertical: bool) -> None:
    """A dark sign box with neon-ish glyph strokes and a dithered halo."""
    bw, bh = (5, 4 * n + 2) if vertical else (4 * n + 2, 5)
    dark = _mix(col, "#06060e", 0.82)
    for j in range(-2, bh + 2):
        for i in range(-2, bw + 2):
            xx, yy = x + i, y + j
            if 0 <= xx < W and 0 <= yy < H and BAYER4[yy % 4, xx % 4] < 0.22 and c.a[yy, xx, 3]:
                c.a[yy, xx, :3] = rgb(_mix(_hex(c.a[yy, xx, :3]), col, 0.35))
    c.rect(x, y, bw, bh, dark)
    c.hline(x, x + bw - 1, y, _mix(col, "#06060e", 0.55))
    for k in range(n):
        gx, gy = (x + 1, y + 1 + 4 * k) if vertical else (x + 1 + 4 * k, y + 1)
        _glyph(c, gx, gy, col, rng)


def city(vid: str, rng: np.random.Generator) -> tuple[Canvas, Canvas]:
    v = VARIANTS[vid]
    c, glow = Canvas(), Canvas()
    x, y, w, h = SKY
    bottom = y + h
    # two rows of blocks: back row (dimmer) and front row
    # farther rows are lighter (the city's own haze), nearer rows darker
    for tone, base, hmin, hmax in ((3, bottom - 18, 18, 46), (2, bottom - 6, 26, 70),
                                   (0, bottom + 2, 10, 26)):  # fmt: skip
        bx = x - int(rng.integers(0, 6))
        while bx < x + w:
            bw = int(rng.integers(8, 18))
            bh = int(rng.integers(hmin, hmax))
            _building(c, glow, v, rng, bx, base - bh, bw, bottom, tone)
            bx += bw + int(rng.integers(1, 4))
    # a tall tower with a spire on the right of the opening
    tx, tt = WIN[0] + 96, WIN[1] + 40
    _building(c, glow, v, rng, tx, tt, 14, bottom, 2)
    c.vline(tx + 7, tt - 16, tt - 1, v["bd"][2])
    c.vline(tx + 6, tt - 8, tt - 1, v["bd"][2])
    c.px(tx + 7, tt - 17, "#c0505e")
    # signs: vertical banners on building faces and one billboard
    s0, s1, s2 = v["signs"]
    _sign(c, v, rng, WIN[0] + 14, WIN[1] + 56, 5, s0, True)
    _sign(c, v, rng, WIN[0] + 80, WIN[1] + 66, 4, s1, True)
    _sign(c, v, rng, WIN[0] + 36, WIN[1] + 82, 5, s2, False)
    # elevated line with a train (lit windows), just above the sill
    ly = WIN[1] + WIN[3] - 12
    c.rect(x, ly, w, 3, v["bd"][0])
    c.hline(x, x + w - 1, ly, v["edge"])
    for px_ in range(x + 4, x + w, 22):
        c.rect(px_, ly + 3, 2, 12, v["bd"][0])
    tx0 = WIN[0] + 6
    c.rect(tx0, ly - 5, 54, 5, v["bd"][2])
    c.hline(tx0, tx0 + 53, ly - 5, v["edge"])
    for k in range(10):
        c.rect(tx0 + 2 + k * 5, ly - 3, 3, 1, v["lit"][k % len(v["lit"])] if k % 4 else v["bd"][1])
    if v["haze"]:
        _haze(c, v["sky"][2], v["haze"] * 0.6, y, bottom)
    return c, glow


def window_glow(vid: str) -> Canvas:
    """Light the window throws into the room while the web is used (state-driven level)."""
    v = VARIANTS[vid]
    g = Canvas()
    x, y, w, h = WIN
    for j in range(h):
        for i in range(w):
            xx, yy = x + i, y + j
            t = j / h
            if BAYER4[yy % 4, xx % 4] < 0.22 + 0.4 * t:
                g.px(xx, yy, v["glow"][0] if t < 0.6 else v["glow"][1])
    # light falling on the wall below the sill and on the left reveal
    for j in range(40):
        for i in range(w + 30):
            xx, yy = x - 15 + i + j // 2, y + h + 10 + j
            if yy < 222 and BAYER4[yy % 4, xx % 4] < 0.32 * (1 - j / 40):
                g.px(xx, yy, "k6")
    return g


def glass_rain(rng: np.random.Generator) -> Canvas:
    """Drops on the pane (rain variant): small lens drops with a highlight, a few trails."""
    c = Canvas()
    x, y, w, h = WIN
    for _ in range(70):
        dx, dy = int(rng.integers(x + 1, x + w - 2)), int(rng.integers(y + 1, y + h - 2))
        big = rng.random() < 0.25
        c.px(dx, dy, "#3e4a78")
        if big:
            c.px(dx, dy + 1, "#2a3360")
            c.px(dx + 1, dy, "#2a3360")
            c.px(dx + 1, dy + 1, "#1e254a")
            c.px(dx, dy - 1, "#8a98c4")
        else:
            c.px(dx, dy - 1, "#64729e")
    for _ in range(9):  # trails a drop left on the way down
        dx, dy = int(rng.integers(x + 2, x + w - 2)), int(rng.integers(y + 10, y + h - 30))
        for k in range(int(rng.integers(8, 24))):
            if BAYER4[(dy + k) % 4, dx % 4] < 0.7:
                c.px(dx + (1 if k > 12 else 0), dy + k, "#26305a")
        c.px(dx, dy - 1, "#7c8ab8")
    return c
