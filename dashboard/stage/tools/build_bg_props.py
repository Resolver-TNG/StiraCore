"""Shelf (memory) and desk (tools) for the Stage background (#341). Offline, imported by
build_bg.py. Each returns a colour layer and its light layer (black = no light); the light
layers are shown only while their event runs (shelf: a memory tool, desk: any other tool).

Shelf compartments follow the memory layers that light them (top to bottom): procedural =
binders and manuals, semantic = reference books, jars and a card index, episodic = file boxes,
diaries, a photo and letters; the bottom row is the archive.
"""

from __future__ import annotations

import math

import numpy as np
from build_bg import DESK_Y, SHELF, Canvas, W
from build_bg_room import bevel, dith, ramp_px, tiny_text

BOOKS = ["#2a3c4c", "#3a2a48", "#4a3a2a", "#2c3a30", "#3c2c34", "#30304e", "#463a44", "#24343c"]
PAPER = ["#4a4864", "#5e5c78", "#706e8a"]


def _shade(col: str, f: float) -> str:
    v = np.array([int(col[i : i + 2], 16) for i in (1, 3, 5)], float)
    if f < 0:
        v = v * (1 + f) + np.array([10, 6, 22]) * (-f) * 0.4  # shadows lean violet
    else:
        v = v + (np.array([150, 160, 200]) - v) * f
    return "#{:02x}{:02x}{:02x}".format(*tuple(np.clip(np.round(v), 0, 255).astype(int)))


def book(c: Canvas, x, base, w, h, col, rng, bands=True, lean=0) -> None:
    """An upright book seen by its spine; lean shifts the top by `lean` pixels."""
    hi, lo = _shade(col, 0.18), _shade(col, -0.35)
    for j in range(h):
        off = round(lean * (1 - j / max(h - 1, 1)))
        yy = base - h + j
        c.px(x + off - 1, yy, "k0")
        c.px(x + off + w, yy, "k0")
        for i in range(w):
            c.px(x + off + i, yy, hi if i == w - 1 else lo if i == 0 else col)
    top = base - h
    c.hline(x + lean, x + lean + w - 1, top - 1, "k0")
    c.hline(x + lean, x + lean + w - 1, top, _shade(col, 0.3))
    if bands and h > 14:
        for by in (top + 3, base - 5):
            off = round(lean * (1 - (by - top) / max(h - 1, 1)))
            c.hline(x + off + 1, x + off + w - 2, by, "#6a5c4a")
        if w >= 5:
            off = round(lean * 0.5)
            c.rect(x + off + 1, top + h // 2 - 2, w - 2, 4, _shade(col, -0.5))
            c.hline(x + off + 1, x + off + w - 2, top + h // 2 - 1, "#5a5070")


def binder(c: Canvas, x, base, w, h, col, rng) -> None:
    hi, lo = _shade(col, 0.2), _shade(col, -0.4)
    c.rect(x - 1, base - h - 1, w + 2, h + 1, "k0")
    c.rect(x, base - h, w, h, col)
    c.vline(x + w - 1, base - h, base - 1, hi)
    c.vline(x, base - h, base - 1, lo)
    c.hline(x, x + w - 1, base - h, hi)
    # label window with two text lines, finger hole
    c.rect(x + 1, base - h + 4, w - 2, 9, "k0")
    c.rect(x + 1, base - h + 5, w - 2, 7, PAPER[1])
    c.hline(x + 2, x + w - 3, base - h + 5, PAPER[2])
    tiny_text(c, x + 2, base - h + 7, w - 3, "#33324e", rng, rows=2)
    c.disc(x + w // 2, base - 7, 1.2, "k0")
    c.px(x + w // 2 + 1, base - 8, lo)


def flask(c: Canvas, glow: Canvas, x: int, base: int, kind: str, liquid: str) -> None:
    """Glassware with a liquid; the liquid is also written into the glow layer."""
    ramp = {"gl": ["gl1", "gl2", "gl3"], "am": ["am1", "am2", "am3"],
            "mg": ["mg1", "mg2", "mg3"], "gr": ["gr1", "gr2", "gr3"]}[liquid]  # fmt: skip
    dim = {"gl": "#1c3a44", "am": "#4a3624", "mg": "#3c2648", "gr": "#1c3a28"}[liquid]
    dim2 = {"gl": "#24505a", "am": "#62462a", "mg": "#4e3060", "gr": "#265034"}[liquid]
    if kind == "erlen":
        h = 18
        for j in range(h):
            half = 2 + (j * 6) // h if j > 6 else 2
            y = base - h + j
            c.px(x - half - 1, y, "k0")
            c.px(x + half + 1, y, "k0")
            c.px(x - half, y, "glass_mid")
            c.px(x + half, y, "glass_mid")
            for i in range(-half + 1, half):
                if j > 9:
                    c.px(x + i, y, dim2 if i < half - 2 else dim)
                    glow.px(x + i, y, ramp[2] if j == 10 else ramp[1])
                else:
                    c.px(x + i, y, "k2")
        c.hline(x - 8, x + 8, base - 1, "glass_mid")
        c.hline(x - 3, x + 3, base - h, "glass_hi")
        c.hline(x - 3, x + 3, base - h - 1, "k0")
        c.px(x + 2, base - 6, "glass_hi")
        c.px(x + 3, base - 5, "glass_hi")
    elif kind == "round":
        c.disc(x, base - 7, 8, "k0")
        c.disc(x, base - 7, 7, "glass_mid")
        c.disc(x, base - 7, 6, "k2")
        for y in range(base - 9, base):
            for i in range(-6, 7):
                if i * i + (y - (base - 7)) ** 2 <= 30:
                    c.px(x + i, y, dim2 if i < 3 else dim)
                    glow.px(x + i, y, ramp[1])
        c.rect(x - 2, base - 23, 5, 10, "k0")
        c.rect(x - 1, base - 22, 3, 9, "glass_mid")
        c.px(x, base - 20, "k2")
        c.px(x + 3, base - 10, "glass_hi")
        c.hline(x + 1, x + 2, base - 11, "glass_hi")
    elif kind == "beaker":
        c.rect(x - 6, base - 15, 13, 15, "k0")
        c.rect(x - 5, base - 14, 11, 14, "glass_mid")
        c.rect(x - 4, base - 13, 9, 13, "k2")
        c.rect(x - 4, base - 7, 9, 7, dim)
        c.hline(x - 4, x + 4, base - 7, dim2)
        for i in range(-4, 5):
            for j in range(7):
                glow.px(x + i, base - 7 + j, ramp[1] if j else ramp[2])
        c.vline(x + 3, base - 12, base - 2, "glass_hi")
        c.hline(x - 6, x + 6, base - 15, "glass_hi")
        for k in range(3):  # graduation marks
            c.px(x - 3, base - 11 + k * 3, "glass_mid")
    elif kind == "tubes":
        c.rect(x - 10, base - 7, 21, 7, "k0")
        c.rect(x - 9, base - 6, 19, 6, "wood2")
        c.hline(x - 9, x + 9, base - 6, "wood3")
        for k in range(5):
            tx = x - 8 + k * 4
            c.rect(tx - 1, base - 18, 4, 13, "k0")
            c.rect(tx, base - 17, 2, 12, "glass_mid")
            fill = 5 - (k % 2) * 2
            c.rect(tx, base - 6 - fill, 2, fill, [dim, dim2][k % 2])
            glow.rect(tx, base - 6 - fill, 2, fill, ramp[2 - k % 2])
            c.px(tx + 1, base - 16, "glass_hi")
    elif kind == "jar":
        c.rect(x - 7, base - 20, 15, 20, "k0")
        c.rect(x - 6, base - 16, 13, 16, "glass_mid")
        c.rect(x - 5, base - 15, 11, 14, "k2")
        c.rect(x - 6, base - 19, 13, 3, "k6")
        c.hline(x - 6, x + 6, base - 19, "k8")
        for j in range(11):
            for i in range(-5, 6):
                if dith(x + i, base - 12 + j, 0.75):
                    c.px(x + i, base - 12 + j, dim if (i + j) % 3 else dim2)
                    glow.px(x + i, base - 12 + j, ramp[1])
        # specimen: a curled shape floating in it
        for a in range(0, 300, 25):
            rr = math.radians(a)
            r = 1.5 + a / 120
            c.px(round(x + r * math.cos(rr)), round(base - 7 + r * math.sin(rr)), "#6a7a6a")
        c.vline(x + 4, base - 14, base - 3, "glass_hi")
        c.rect(x - 4, base - 9, 4, 3, PAPER[0])  # label
        c.hline(x - 4, x - 1, base - 8, "#33324e")


def plant_pot(c: Canvas, x, base, w, h, rng, trail=0) -> None:
    """Small pot with a leafy plant; `trail` lets a few vines hang down to the right."""
    c.rect(x - 1, base - h - 1, w + 2, h + 1, "k0")
    for yy in range(base - h, base):
        for xx in range(x, x + w):
            ramp_px(c, xx, yy, ["#2a1e2a", "#3a2a32", "#4a3438", "#3a2a32"], (xx - x) / (w - 1))
    c.hline(x - 1, x + w, base - h, "#5a4044")
    leaf = ["#0e1c1a", "#16302a", "#1f4236", "#2c5a44", "#3e7052"]
    cx = x + w // 2
    for k in range(9):
        ang = -math.pi / 2 + (k - 4) * 0.38 + rng.normal(0, 0.08)
        ln = 5 + int(rng.integers(0, 4))
        for s in range(ln):
            xx = round(cx + math.cos(ang) * s)
            yy = round(base - h - 1 + math.sin(ang) * s)
            for d in (-1, 0, 1):
                ramp_px(c, xx + d, yy, leaf, 0.3 + 0.5 * (d + 1) / 2 + 0.2 * (s / ln))
            c.px(xx - 2, yy, "k0")
    for v in range(trail):
        vx = x + w - 1 + v * 2
        vy = base - h + 1
        length = 20 + v * 16
        for s in range(length):
            xx = vx + round(math.sin(s * 0.22 + v) * 1.5)
            yy = vy + s
            c.px(xx, yy, "#16302a")
            if s % 6 == 3:  # a heart-shaped leaf every few pixels
                side = 1 if (s // 6) % 2 else -1
                for dy, dx_list in ((0, (0, 1, 2)), (1, (0, 1, 2, 3)), (2, (1, 2))):
                    for dx in dx_list:
                        ramp_px(c, xx + side * dx, yy + dy - 1, leaf, 0.35 + 0.15 * dx + 0.1 * dy)
                c.px(xx + side * 4, yy, "k0")


def shelf(rng: np.random.Generator) -> tuple[Canvas, Canvas]:
    c, glow = Canvas(), Canvas()
    x, y, w, h = SHELF
    # carcass: back panel boards, side posts with grain, top board
    c.rect(x, y, w, h, "k2")
    for yy in range(y + 3, y + h - 3):
        for xx in range(x + 4, x + w - 4):
            ramp_px(c, xx, yy, ["k1", "k2", "k3"], 0.25 + 0.5 * (xx - x) / w)
    for bx in range(x + 30, x + w - 4, 26):
        c.vline(bx, y, y + h, "k1")
        c.vline(bx + 1, y, y + h, "k3")
    for px0, lit in ((x, False), (x + w - 4, True)):
        for yy in range(y, y + h):
            for i in range(4):
                base = (
                    ["wood1", "wood1", "wood2", "wood2"]
                    if not lit
                    else ["wood1", "wood2", "wood2", "wood3"]
                )
                col = base[i]
                if dith(px0 + i, yy + (i * 7) % 11, 0.12):
                    col = "wood0"
                c.px(px0 + i, yy, col)
        c.vline(px0 - 1 if not lit else px0 + 4, y - 5, y + h + 4, "k0")
    c.rect(x - 4, y - 6, w + 8, 6, "k0")
    c.rect(x - 3, y - 5, w + 6, 4, "wood2")
    c.hline(x - 3, x + w + 2, y - 5, "wood3")
    c.hline(x - 3, x + w + 2, y - 2, "wood1")
    rows = [y + 44, y + 88, y + 132, y + h - 2]
    for by in rows[:3]:
        c.rect(x + 2, by, w - 4, 4, "wood2")
        c.hline(x + 2, x + w - 3, by, "wood3")
        c.hline(x + 2, x + w - 3, by + 3, "wood1")
        c.hline(x + 2, x + w - 3, by + 4, "k0")
        for i in range(4, w - 4):  # shadow under the board
            if dith(x + i, by + 5, 0.6):
                c.px(x + i, by + 5, "k1")
        glow.hline(x + 4, x + w - 5, by + 3, "gl3")
        for j in range(1, 30):
            for i in range(4, w - 4):
                xx, yy = x + i, by + 4 + j
                if dith(xx, yy, 0.55 * (1 - j / 30) ** 1.5):
                    glow.px(xx, yy, "gl1" if j > 8 else "gl2")
    # top-edge LED of the first compartment (under the top board)
    glow.hline(x + 4, x + w - 5, y + 1, "gl3")
    for j in range(1, 24):
        for i in range(4, w - 4):
            xx, yy = x + i, y + 1 + j
            if dith(xx, yy, 0.5 * (1 - j / 24) ** 1.5):
                glow.px(xx, yy, "gl1" if j > 8 else "gl2")

    # 1. procedural: ring binders, a stack of manuals, glassware
    b0 = rows[0]
    for k, col in enumerate((BOOKS[0], BOOKS[1], BOOKS[2], BOOKS[5])):
        binder(c, x + 6 + k * 9, b0, 8, 32 - (k % 2) * 3, col, rng)
    for k, (col, ww) in enumerate(((BOOKS[6], 20), (BOOKS[3], 18), (BOOKS[4], 19))):
        yy = b0 - 4 - k * 4
        c.rect(x + 44 - 1, yy - 1, ww + 2, 5, "k0")
        c.rect(x + 44, yy, ww, 3, col)
        c.hline(x + 44, x + 44 + ww - 1, yy, _shade(col, 0.2))
        c.vline(x + 44 + ww - 1, yy, yy + 2, PAPER[1])  # page edges
    plant_pot(c, x + 48, b0 - 12, 9, 6, rng)
    flask(c, glow, x + 76, b0, "erlen", "gl")
    flask(c, glow, x + 92, b0, "tubes", "am")
    # 2. semantic: reference books (one leaning), a specimen jar, a card index
    b1 = rows[1]
    bx = x + 6
    for k in range(7):
        ww = int(rng.integers(4, 7))
        hh = int(rng.integers(26, 36))
        book(c, bx, b1, ww, hh, BOOKS[(k * 3) % len(BOOKS)], rng)
        bx += ww + 1
    book(c, bx + 1, b1, 5, 30, BOOKS[7], rng, lean=5)
    flask(c, glow, x + 64, b1, "jar", "gr")
    ix, iy = x + 76, b1 - 14
    bevel(c, ix, iy, 24, 14, "wood2", "wood3", "wood1")
    for k in range(2):  # two drawers with brass pulls and label cards
        dx = ix + 2 + k * 11
        c.rect(dx, iy + 3, 9, 9, "k0")
        c.rect(dx + 1, iy + 4, 7, 7, "wood1")
        c.rect(dx + 2, iy + 5, 5, 2, PAPER[1])
        c.hline(dx + 3, dx + 5, iy + 9, "am1")
        c.px(dx + 4, iy + 9, "am2")
    for k in range(5):  # cards sticking up
        c.rect(ix + 3 + k * 4, iy - 3, 3, 3, PAPER[1 + k % 2])
        c.px(ix + 3 + k * 4, iy - 3, PAPER[2])
    flask(c, glow, x + 88, iy, "round", "mg")
    # 3. episodic: file boxes, diaries, a photo, letters
    b2 = rows[2]
    for k in range(2):
        fx = x + 6 + k * 14
        bevel(c, fx, b2 - 24, 13, 24, "#3e3236", "#54444a", "#2a2228")
        c.rect(fx + 3, b2 - 20, 7, 5, "k0")
        c.rect(fx + 4, b2 - 19, 5, 3, PAPER[1])
        c.hline(fx + 4, fx + 7, b2 - 18, "#33324e")
        c.rect(fx + 4, b2 - 9, 5, 2, "k0")  # hand hole
    for k, col in enumerate(("#3a2a3c", "#2c3448", "#40342a", "#2a3a3a")):
        yy = b2 - 4 - k * 4
        ww = 16 - (k % 2) * 2
        c.rect(x + 35 - 1, yy - 1, ww + 2, 5, "k0")
        c.rect(x + 35, yy, ww, 3, col)
        c.hline(x + 35, x + 35 + ww - 1, yy, _shade(col, 0.2))
        c.vline(x + 35 + ww - 1, yy, yy + 2, PAPER[1])
    c.vline(x + 41, b2 - 1, b2 + 5, "#7a3a4a")  # ribbon bookmark hanging over the board
    c.px(x + 41, b2 + 6, "#5a2a3a")
    fx, fy = x + 56, b2 - 20
    bevel(c, fx, fy, 16, 20, "wood2", "wood3", "wood1")
    c.rect(fx + 2, fy + 2, 12, 14, "k0")
    c.grad(fx + 3, fy + 3, 10, 12, ["#1b1f4a", "#2a2a5e", "#3d3270"], curve=1)
    c.px(fx + 10, fy + 5, "#bdb8de")
    for xx in range(fx + 3, fx + 13):  # a hill and two small figures
        top = fy + 11 + round(math.sin((xx - fx) * 0.5))
        c.vline(xx, top, fy + 14, "#141532")
    c.rect(fx + 6, fy + 9, 1, 3, "#0b0c20")
    c.rect(fx + 8, fy + 10, 1, 2, "#0b0c20")
    c.vline(fx + 8, b2 - 1, b2 - 1, "k0")
    lx, ly = x + 76, b2 - 9  # letters tied with string
    for k in range(3):
        c.rect(lx - 1, ly + k * 3 - 1, 16, 4, "k0")
        c.rect(lx, ly + k * 3, 14, 2, PAPER[k % 2])
        c.px(lx + 1 + k * 4, ly + k * 3, PAPER[2])
    c.vline(lx + 7, ly - 1, ly + 8, "#6a4a4a")
    flask(c, glow, x + 94, b2, "beaker", "mg")
    # 4. archive: a long row of older books and binders (the folder prop slides out at right)
    b3 = rows[3]
    bx = x + 6
    while bx < x + 80:
        ww = int(rng.integers(3, 7))
        hh = int(rng.integers(22, 34))
        book(c, bx, b3, ww, hh, BOOKS[int(rng.integers(0, len(BOOKS)))], rng, bands=ww > 3)
        bx += ww + 1
    for k in range(3):
        binder(c, x + 81 + k * 7, b3, 6, 30, (BOOKS[5], BOOKS[0], BOOKS[2])[k], rng)
    c.rect(x - 4, y + h - 3, w + 8, 7, "k0")
    c.rect(x - 3, y + h - 2, w + 6, 5, "wood2")
    c.hline(x - 3, x + w + 2, y + h - 2, "wood3")
    c.hline(x - 3, x + w + 2, y + h + 2, "wood1")
    # on top: a rolled chart and a globe; a trailing plant at the right end
    rx0 = x + 30
    c.rect(rx0, y - 11, 26, 5, "k0")
    for xx in range(rx0 + 1, rx0 + 25):
        for yy in range(y - 10, y - 6):
            ramp_px(c, xx, yy, PAPER, 1 - (yy - (y - 10)) / 3)
    c.disc(rx0 + 26, y - 8, 2.5, "k0")
    c.disc(rx0 + 26, y - 8, 1.5, PAPER[0])
    gx, gy = x + 66, y - 14
    c.disc(gx, gy, 6, "k0")
    c.disc(gx, gy, 5, "#24485a")
    for yy in range(gy - 5, gy + 6):
        for xx in range(gx - 5, gx + 6):
            if (xx - gx) ** 2 + (yy - gy) ** 2 <= 25 and math.sin(xx * 0.9) + math.cos(
                yy * 1.1
            ) > 0.6:
                c.px(xx, yy, "#3a5a3e")
    c.px(gx + 2, gy - 3, "#6a8aa4")
    c.vline(gx + 6, gy - 5, gy + 6, "k5")
    c.rect(gx - 3, gy + 7, 7, 2, "k5")
    plant_pot(c, x + 84, y - 6, 12, 8, rng, trail=2)
    # filing cabinet left of the shelf (seen on wide screens only)
    cx0 = 16
    bevel(c, cx0, 150, 56, 82, "k3", "k5", "k1")
    for k in range(3):
        dy = 154 + k * 26
        bevel(c, cx0 + 3, dy, 50, 24, "k3", "k5", "k2")
        c.rect(cx0 + 22, dy + 6, 12, 3, "k0")
        c.hline(cx0 + 23, cx0 + 32, dy + 6, "k6")
        c.rect(cx0 + 23, dy + 12, 10, 5, "k0")
        c.rect(cx0 + 24, dy + 13, 8, 3, PAPER[0])
    plant_pot(c, cx0 + 20, 150, 14, 10, rng, trail=1)
    return c, glow


# --- desk ----------------------------------------------------------------------------------------


def cable(c: Canvas, pts, col="k1", hi=None) -> None:
    """A cable through control points (quadratic segments), 1 px with an optional highlight."""
    for (x0, y0), (x1, y1), (x2, y2) in zip(pts[0::2], pts[1::2], pts[2::2]):
        n = int(abs(x2 - x0) + abs(y2 - y0) + abs(y1 - y0)) * 2 + 2
        last = None
        for k in range(n + 1):
            t = k / n
            xx = round((1 - t) ** 2 * x0 + 2 * (1 - t) * t * x1 + t * t * x2)
            yy = round((1 - t) ** 2 * y0 + 2 * (1 - t) * t * y1 + t * t * y2)
            if (xx, yy) != last:
                c.px(xx, yy, col)
                if hi:
                    c.px(xx, yy - 1, hi)
                last = (xx, yy)


def desk(rng: np.random.Generator, scope: tuple[int, int, int, int]) -> tuple[Canvas, Canvas]:
    c, glow = Canvas(), Canvas()
    # desk top and body
    c.rect(390, DESK_Y - 1, W - 390, 8, "k0")
    c.rect(391, DESK_Y, W - 391, 6, "k5")
    c.hline(391, W, DESK_Y, "k8")
    c.hline(391, W, DESK_Y + 1, "k6")
    c.hline(391, W, DESK_Y + 5, "k3")
    c.rect(392, DESK_Y + 7, W - 392, 60, "k2")
    c.hline(392, W, DESK_Y + 7, "k0")
    for dx in range(404, W, 76):  # drawer fronts with pulls
        bevel(c, dx, DESK_Y + 10, 66, 22, "k3", "k4", "k1")
        c.rect(dx + 26, DESK_Y + 19, 14, 3, "k0")
        c.hline(dx + 27, dx + 38, DESK_Y + 19, "k6")
    c.rect(392, DESK_Y + 34, W - 392, 2, "k1")
    # PC tower under the desk, its fan grille and a standby pixel (paint, not a light)
    tx, ty = 420, DESK_Y + 38
    bevel(c, tx, ty, 26, 40, "k3", "k5", "k1")
    for k in range(5):
        c.hline(tx + 4, tx + 21, ty + 6 + k * 3, "k1")
    c.px(tx + 21, ty + 26, "#2e4a3a")

    # oscilloscope: case, carry handle, bezel, grid screen, knobs, buttons, BNC inputs, feet
    ox, oy, ow, oh = 410, 176, 64, 48
    c.rect(ox + 14, oy - 4, 36, 2, "k0")
    c.rect(ox + 14, oy - 3, 2, 3, "k0")
    c.rect(ox + 48, oy - 3, 2, 3, "k0")
    c.hline(ox + 15, ox + 48, oy - 4, "k6")
    bevel(c, ox, oy, ow, oh, "k4", "k6", "k2")
    c.hline(ox + 1, ox + ow - 2, oy + 1, "k7")
    for k in range(4):  # side vents
        c.hline(ox + 2, ox + 4, oy + 10 + k * 3, "k1")
    sx, sy, sw, sh = scope
    c.rect(sx - 2, sy - 2, sw + 4, sh + 4, "k0")
    c.rect(sx - 1, sy - 1, sw + 2, sh + 2, "k1")
    c.rect(sx, sy, sw, sh, "gr0")
    for gx in range(sx, sx + sw, 6):
        c.vline(gx, sy, sy + sh - 1, "#1d3a27")
    for gy in range(sy, sy + sh, 6):
        c.hline(sx, sx + sw - 1, gy, "#1d3a27")
    c.vline(sx + sw // 2, sy, sy + sh - 1, "#25482f")
    c.hline(sx, sx + sw - 1, sy + sh // 2, "#25482f")
    c.px(sx + 1, sy, "#3a5a44")  # glass glint
    c.px(sx + 2, sy, "#3a5a44")
    for gy in range(sy, sy + sh):
        for gx in range(sx, sx + sw):
            if dith(gx, gy, 0.3):
                glow.px(gx, gy, "gr0")
    for k in range(3):
        kx, ky = ox + 52, oy + 8 + k * 12
        c.disc(kx, ky, 4, "k0")
        c.disc(kx, ky, 3, "k6")
        c.disc(kx - 1, ky - 1, 1.5, "k8")
        c.px(kx + 1, ky - 3, "k1")  # pointer tick
        c.px(kx + 2, ky + 1, "k3")
    for k in range(5):
        bxx = ox + 7 + k * 7
        c.rect(bxx, oy + 37, 4, 3, "k0")
        c.hline(bxx, bxx + 3, oy + 37, "k6")
        c.px(bxx + 1, oy + 41, "am1")
        glow.px(bxx + 1, oy + 41, "am3")
    for k in range(2):  # BNC inputs
        bx, by = ox + 46 + k * 9, oy + 41
        c.disc(bx, by, 2.5, "k0")
        c.disc(bx, by, 1.5, "#8a8aa6")
        c.px(bx, by, "k1")
    c.rect(ox + 4, oy + oh, 6, 2, "k0")
    c.rect(ox + ow - 10, oy + oh, 6, 2, "k0")
    # rack: 2 units, LED row + display, patch panel with ports and patch cables
    rx, ry = 480, 194
    c.rect(rx - 1, ry - 1, 58, 32, "k0")
    bevel(c, rx, ry, 56, 14, "k4", "k6", "k2")
    bevel(c, rx, ry + 15, 56, 15, "k4", "k6", "k2")
    for yy in (ry + 3, ry + 10, ry + 18, ry + 26):  # rack ears with screws
        c.px(rx + 2, yy, "k7")
        c.px(rx + 53, yy, "k7")
    c.rect(rx + 36, ry + 3, 14, 7, "k0")
    c.rect(rx + 37, ry + 4, 12, 5, "#10201a")
    for k in range(3):
        c.hline(rx + 38, rx + 38 + 3 + k * 3, ry + 5 + k * 1, "#1e3a2c")
        glow.hline(rx + 38, rx + 38 + 3 + k * 3, ry + 5 + k * 1, "gr2")
    for k in range(5):
        c.px(rx + 6 + k * 6, ry + 6, "gr2" if k % 3 else "am2")
        glow.px(rx + 6 + k * 6, ry + 6, "gr3" if k % 3 else "am3")
        glow.px(rx + 6 + k * 6, ry + 5, "gr1" if k % 3 else "am1")
    c.rect(rx + 5, ry + 19, 46, 7, "k1")
    for k in range(9):
        c.rect(rx + 6 + k * 5, ry + 20, 3, 3, "k0")
        c.px(rx + 7 + k * 5, ry + 24, "k5")
    for k, (col, drop) in enumerate(
        (("#1e2e3a", 12), ("#2e1e30", 18), ("#242e22", 9), ("#1e2438", 22))
    ):
        px_ = rx + 7 + k * 10
        # out of the port, over the desk edge, hanging down the front
        cable(c, [(px_, ry + 23), (px_ - 1, DESK_Y - 2), (px_ - 3, DESK_Y + 1)], col)
        cable(
            c, [(px_ - 3, DESK_Y + 1), (px_ - 4, DESK_Y + 8), (px_ - 2 - k, DESK_Y + 8 + drop)], col
        )
    # cables between the scope and the rack, and down over the desk edge
    cable(c, [(ox + 46, oy + 44), (ox + 52, DESK_Y + 2), (rx + 4, ry + 28)], "k1", "k3")
    cable(c, [(ox + 55, oy + 44), (ox + 64, DESK_Y + 6), (rx + 2, DESK_Y + 2)], "k1")
    cable(c, [(rx + 50, ry + 28), (rx + 60, DESK_Y + 30), (rx + 40, DESK_Y + 46)], "k0", "k3")
    cable(c, [(452, DESK_Y + 6), (448, DESK_Y + 24), (434, DESK_Y + 36)], "k0", "k3")
    # monitor on an arm at the right of the rack: terminal lines light with tools
    mx, my, mw, mh = 520, 152, 46, 36
    c.rect(mx + 20, my + mh, 4, ry - (my + mh), "k0")
    c.vline(mx + 21, my + mh, ry - 1, "k5")
    c.rect(mx + 14, ry - 3, 16, 3, "k0")
    c.hline(mx + 15, mx + 28, ry - 3, "k5")
    bevel(c, mx, my, mw, mh, "k3", "k5", "k1")
    c.rect(mx + 3, my + 3, mw - 6, mh - 9, "k0")
    c.rect(mx + 4, my + 4, mw - 8, mh - 11, "#0b1418")
    lines = [(3, 18), (3, 24), (6, 12), (3, 28), (6, 20), (3, 9), (9, 14)]
    for k, (ind, ln) in enumerate(lines):
        yy = my + 6 + k * 3
        col = "#18343a" if k != 5 else "#3a3418"
        c.hline(mx + 5 + ind, mx + 5 + ind + ln, yy, col)
        glow.hline(mx + 5 + ind, mx + 5 + ind + ln, yy, "gl2" if k != 5 else "am2")
    c.rect(mx + 6, my + 6 + 7 * 3, 2, 1, "#2a5a5a")
    glow.rect(mx + 6, my + 6 + 7 * 3, 2, 1, "gl4")
    for gy in range(my + 4, my + mh - 7):
        for gx in range(mx + 4, mx + mw - 4):
            if dith(gx, gy, 0.14):
                glow.px(gx, gy, "gl0")
    c.px(mx + mw - 6, my + mh - 4, "k6")
    # desk lamp (wide screens)
    c.rect(590, DESK_Y - 4, 22, 4, "k5")
    c.hline(590, 611, DESK_Y - 4, "k7")
    c.line(600, DESK_Y - 4, 592, DESK_Y - 34, "k6")
    c.line(601, DESK_Y - 4, 593, DESK_Y - 34, "k7")
    c.rect(582, DESK_Y - 40, 20, 8, "k6")
    c.hline(582, 601, DESK_Y - 40, "k8")
    c.hline(584, 599, DESK_Y - 33, "am3")
    glow.hline(584, 599, DESK_Y - 33, "am3")
    for j in range(1, 26):
        for i in range(-j // 2, 16 + j // 2):
            xx, yy = 584 + i, DESK_Y - 33 + j
            if yy < DESK_Y and dith(xx, yy, 0.4 * (1 - j / 26)):
                glow.px(xx, yy, "am1")
    # small things on the desk top: a mug of pens, a notepad, a cactus on the scope
    c.rect(570, DESK_Y - 10, 8, 10, "k0")
    c.rect(571, DESK_Y - 9, 6, 9, "#3a3048")
    c.vline(576, DESK_Y - 9, DESK_Y - 1, "#4e4260")
    for k, col in enumerate(("#6a4a4a", "#3a5a6a", "#7a7a8a")):
        c.vline(572 + k * 2, DESK_Y - 15 + k, DESK_Y - 10, col)
    c.rect(ox + 22, oy - 10, 8, 6, "k0")
    c.rect(ox + 23, oy - 9, 6, 5, "#3a2a32")
    for k in range(3):
        c.rect(ox + 24 + k * 2 - (k == 1), oy - 16 + (k != 1) * 3, 2, 7 - (k != 1) * 3, "#2c5a44")
        c.px(ox + 24 + k * 2 - (k == 1), oy - 16 + (k != 1) * 3, "#3e7052")
    # light pool of the scope on the desk top
    for j in range(1, 7):
        for i in range(-6, ow + 80):
            xx, yy = ox + i, DESK_Y + j - 1
            if dith(xx, yy, 0.6 * (1 - j / 7)):
                glow.px(xx, yy, "gr1")
    return c, glow
