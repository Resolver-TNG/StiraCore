"""The room for the Stage background (#341): wall, ceiling, window frame, blinds, curtains,
posters, pipes and the foreground. Offline, imported by build_bg.py.

Light comes from the window (upper right, cool) and the neon behind the character; edges that
face it get one lighter pixel, the others one darker pixel, and every object has a 1 px dark
outline, like the character sheet. Values stay low (the character is the brightest thing on
screen); colour is muted and shifts towards violet in the shadows.
"""

from __future__ import annotations

import math

import numpy as np
from build_bg import BAYER4, DESK_Y, WIN, Canvas, H, W


def dith(x: int, y: int, t: float) -> bool:
    return BAYER4[y % 4, x % 4] < t


def bevel(c: Canvas, x, y, w, h, fill, hi, lo, line="k0") -> None:
    """Outlined box lit from the upper right."""
    c.rect(x, y, w, h, line)
    c.rect(x + 1, y + 1, w - 2, h - 2, fill)
    c.hline(x + 1, x + w - 2, y + 1, hi)
    c.vline(x + w - 2, y + 1, y + h - 2, hi)
    c.hline(x + 1, x + w - 2, y + h - 2, lo)
    c.vline(x + 1, y + 2, y + h - 2, lo)


def speckle(c: Canvas, x, y, w, h, col, p, rng) -> None:
    for yy in range(y, y + h):
        for xx in range(x, x + w):
            if rng.random() < p and 0 <= xx < W and 0 <= yy < H and c.a[yy, xx, 3]:
                c.px(xx, yy, col)


def ramp_px(c: Canvas, x, y, ramp, t) -> None:
    """Pixel from a ramp at t in [0, 1], dithered between neighbouring steps."""
    n = len(ramp) - 1
    t = min(max(t, 0.0), 1.0) * n
    k = int(t) + (1 if (t - int(t)) > BAYER4[y % 4, x % 4] else 0)
    c.px(x, y, ramp[min(k, n)])


def tiny_text(c: Canvas, x, y, w, col, rng, rows=1, gap=2) -> None:
    """Rows of 1 px 'text' (dashes of random length): reads as writing, says nothing."""
    for r in range(rows):
        xx = x
        while xx < x + w:
            n = int(rng.integers(1, 5))
            c.hline(xx, min(xx + n - 1, x + w - 1), y + r * gap, col)
            xx += n + 1


# --- wall, ceiling, window ---------------------------------------------------------------------


def wall(rng: np.random.Generator) -> Canvas:
    c = Canvas()
    c.grad(0, 0, W, H, ["k1", "k2", "k3", "k3", "k2", "k1"], curve=1.0)
    # concrete panels: seams, a faint lighter lip, rivets, mottled texture
    speckle(c, 0, 0, W, 231, "k2", 0.05, rng)
    speckle(c, 0, 40, W, 180, "k4", 0.012, rng)
    for x in range(24, W, 72):
        c.vline(x, 0, 231, "k1")
        c.vline(x + 1, 0, 231, "k4")
        for yy in (20, 120, 220):
            c.px(x - 3, yy, "k5")
            c.px(x - 3, yy + 1, "k1")
            c.px(x + 4, yy, "k5")
            c.px(x + 4, yy + 1, "k1")
    for yy in (122,):
        c.hline(0, W, yy, "k1")
        c.hline(0, W, yy + 1, "k4")
    # ceiling: dark band, a beam lip, a cable tray along it
    c.grad(0, 0, W, 9, ["k0", "k1"], curve=1)
    c.hline(0, W, 9, "k4")
    c.hline(0, W, 10, "k1")
    # wainscot: rail, slats, skirting
    c.rect(0, 232, W, 3, "k4")
    c.hline(0, W, 231, "k5")
    c.rect(0, 235, W, H - 235, "k1")
    c.grad(0, 235, W, 40, ["k2", "k1"], curve=1)
    for x in range(6, W, 12):
        c.vline(x, 236, H - 1, "k0")
        c.vline(x + 1, 236, H - 1, "k3")
    c.rect(0, 318, W, 3, "k3")
    c.hline(0, W, 318, "k4")
    # moonlight on the wall under the window
    x, y, w, h = WIN
    for j in range(60):
        for i in range(w + 20):
            xx, yy = x - 10 + i + j // 3, y + h + 6 + j
            if 0 <= xx < W and yy < 231 and dith(xx, yy, 0.22 * (1 - j / 60)):
                c.px(xx, yy, "k5")
    posters(c, rng)
    fixtures(c, rng)
    window_frame(c, rng)
    return c


def window_frame(c: Canvas, rng) -> None:
    x, y, w, h = WIN
    # reveal (depth of the wall) and the opening
    c.rect(x - 7, y - 7, w + 14, h + 14, "k1")
    c.a[y : y + h, x : x + w] = 0
    frame = [(x - 4, y - 4, w + 8, 4), (x - 4, y + h, w + 8, 4), (x - 4, y, 4, h), (x + w, y, 4, h)]
    for fx, fy, fw, fh in frame:
        c.rect(fx, fy, fw, fh, "k4")
    c.hline(x - 4, x + w + 3, y - 4, "k6")
    c.vline(x + w + 3, y - 4, y + h + 3, "k6")
    c.vline(x - 4, y - 4, y + h + 3, "k3")
    c.hline(x - 1, x + w, y - 1, "k2")  # inner shadow line
    c.vline(x - 1, y - 1, y + h, "k2")
    # mullion and transom with a highlight on the lit side
    c.rect(x + w // 2 - 1, y, 3, h, "k4")
    c.vline(x + w // 2 + 1, y, y + h, "k6")
    c.vline(x + w // 2 - 1, y, y + h, "k2")
    c.rect(x, y + h // 2 - 1, w, 3, "k4")
    c.hline(x, x + w - 1, y + h // 2 - 1, "k6")
    c.hline(x, x + w - 1, y + h // 2 + 1, "k2")
    for lx in (x + w // 2 - 6, x + w - 8):  # latches
        c.rect(lx, y + h // 2 - 2, 3, 5, "k5")
        c.px(lx + 2, y + h // 2 - 2, "k7")
    # sill with a lit top and a shadow below
    c.rect(x - 9, y + h + 4, w + 18, 5, "k5")
    c.hline(x - 9, x + w + 8, y + h + 4, "k7")
    c.hline(x - 9, x + w + 8, y + h + 5, "k6")
    c.hline(x - 9, x + w + 8, y + h + 9, "k1")
    for i in range(w + 18):
        if dith(x - 9 + i, y + h + 10, 0.5):
            c.px(x - 9 + i, y + h + 10, "k1")
    # roller blind rolled up under the head, with a pull cord
    c.rect(x, y, w, 6, "k3")
    for yy in range(y, y + 6):
        for xx in range(x, x + w):
            ramp_px(c, xx, yy, ["k2", "k4", "k5", "k4", "k2"], (yy - y) / 5)
    c.hline(x, x + w - 1, y + 6, "k1")
    for k in range(7):  # the hem bar
        c.px(x + 10 + k * 17, y + 6, "k6")
    cx = x + w - 14
    c.vline(cx, y + 7, y + 34, "k5")
    c.rect(cx - 1, y + 35, 3, 4, "k6")
    c.px(cx, y + 35, "k8")
    curtains(c)


def curtains(c: Canvas) -> None:
    """Heavy curtains drawn aside: folds as vertical ramps, a tie-back on the left one."""
    x, y, w, h = WIN
    rod = y - 9
    c.hline(x - 18, x + w + 22, rod, "k6")
    c.hline(x - 18, x + w + 22, rod + 1, "k3")
    c.disc(x - 19, rod, 1.5, "k7")
    c.disc(x + w + 23, rod, 1.5, "k7")
    ramp = ["#120f24", "#1b1733", "#251f42", "#30284f", "#3a305a"]
    for left, x0, width in ((True, x - 16, 22), (False, x + w - 4, 26)):
        for yy in range(rod + 2, y + h + 4):
            t = (yy - rod) / (h + 10)
            pinch = 0.55 + 0.45 * abs(t - 0.62) / 0.62 if left else 1.0
            ww = max(6, int(width * pinch))
            xs = range(x0 + width - ww, x0 + width) if left else range(x0, x0 + ww)
            for i, xx in enumerate(xs):
                f = 0.5 + 0.5 * math.sin(i * (2 * math.pi / 7.0) + (0 if left else 1.3))
                lit = f * (0.9 if not left else 0.75) + 0.08
                ramp_px(c, xx, yy, ramp, lit)
            # outline on the window side
            edge = xs[-1] + 1 if left else xs[0] - 1
            c.px(edge, yy, "k0")
        # hem
        if not left:
            c.hline(x0, x0 + width - 1, y + h + 4, "#120f24")
    # tie-back band on the left curtain
    ty = y + 68
    c.rect(x - 14, ty, 16, 3, "#3d3048")
    c.hline(x - 14, x + 1, ty, "#54465c")


def posters(c: Canvas, rng) -> None:
    """Paper on the wall between the shelf and the character: a faded diagram poster, notes."""
    # big poster: a node diagram (the agent's 'mind map'), faded, taped at the corners
    px0, py0, pw, ph = 214, 44, 40, 58
    c.rect(px0 - 1, py0 - 1, pw + 2, ph + 2, "k0")
    c.grad(px0, py0, pw, ph, ["#24233f", "#211f3a", "#1d1b33"], curve=1)
    c.rect(px0 + 3, py0 + 3, pw - 6, 6, "#2f2c50")
    tiny_text(c, px0 + 5, py0 + 5, pw - 10, "#4a4676", rng)
    nodes = [(px0 + 20, py0 + 30), (px0 + 9, py0 + 20), (px0 + 31, py0 + 19), (px0 + 8, py0 + 40),
             (px0 + 32, py0 + 41), (px0 + 20, py0 + 50), (px0 + 20, py0 + 16)]  # fmt: skip
    for a, b in ((0, 1), (0, 2), (0, 3), (0, 4), (0, 5), (1, 6), (2, 6), (3, 5), (4, 5), (1, 3)):
        c.line(*nodes[a], *nodes[b], "#34406a")
    for i, (nx, ny) in enumerate(nodes):
        col = "#4e6c94" if i else "#6a5a96"
        c.disc(nx, ny, 2 if i else 3, col)
        c.px(nx + 1, ny - 1, "#7c8ab0" if i else "#8e7cb8")
    tiny_text(c, px0 + 4, py0 + ph - 4, 20, "#3a3864", rng)
    for tx, ty in ((px0 - 1, py0 - 2), (px0 + pw - 4, py0 - 2)):  # tape
        c.rect(tx, ty, 5, 3, "#4a4660")
        c.px(tx + 4, ty, "#5c5878")
    # bottom right corner curls
    for k in range(4):
        c.hline(px0 + pw - 4 + k, px0 + pw - 1, py0 + ph - 4 + k, "#3a3858")
    c.px(px0 + pw - 1, py0 + ph - 1, "k1")
    # notes and a pinned print below
    for nx, ny, col, rot in (
        (216, 110, "#2e3a3e", 0),
        (230, 112, "#3a3242", 1),
        (222, 126, "#36344a", 0),
    ):
        c.rect(nx, ny + rot, 12, 11, "k0")
        c.rect(nx + 1, ny + 1 + rot, 10, 9, col)
        tiny_text(c, nx + 2, ny + 3 + rot, 7, "#55547a", rng, rows=3)
        c.px(nx + 5, ny + rot, "#7a4a5a")  # pin
    c.rect(240, 128, 12, 9, "k0")
    c.rect(241, 129, 10, 7, "#283048")
    c.rect(242, 132, 3, 3, "#3a4a6a")
    c.hline(241, 250, 134, "#2f3a52")
    c.px(246, 128, "#6a7aa0")


def fixtures(c: Canvas, rng) -> None:
    """Conduit, switch, vent and sockets: small hardware that makes the wall read as a room."""
    # conduit from the ceiling down the right of the character, to a switch
    cx = 396
    c.rect(cx, 11, 3, 140, "k3")
    c.vline(cx + 2, 11, 150, "k5")
    c.vline(cx, 11, 150, "k1")
    for yy in (30, 70, 110, 150):
        c.rect(cx - 1, yy, 5, 2, "k5")
        c.hline(cx - 1, cx + 3, yy, "k6")
    c.rect(cx - 2, 152, 7, 10, "k0")
    c.rect(cx - 1, 153, 5, 8, "k4")
    c.rect(cx, 155, 3, 3, "k6")
    c.px(cx + 1, 155, "k8")
    # air vent under the window
    vx, vy = 420, 156
    bevel(c, vx, vy, 28, 13, "k3", "k5", "k1")
    for k in range(4):
        c.hline(vx + 3, vx + 24, vy + 3 + k * 2, "k1")
        c.hline(vx + 3, vx + 24, vy + 4 + k * 2, "k4")
    # headphones hanging from a hook below the card grid
    hx, hy = 230, 178
    c.rect(hx - 1, hy - 2, 3, 3, "k5")
    c.px(hx, hy - 2, "k7")
    for a in range(180, 361, 6):  # headband
        rr = math.radians(a)
        c.px(round(hx + 7 * math.cos(rr)), round(hy + 8 + 7 * math.sin(rr)), "k5")
        c.px(round(hx + 6 * math.cos(rr)), round(hy + 8 + 6 * math.sin(rr)), "k3")
    for ex in (hx - 9, hx + 6):  # ear cups
        bevel(c, ex, hy + 7, 4, 9, "k3", "k5", "k1")
        c.vline(ex + (3 if ex < hx else 0), hy + 9, hy + 13, "#2a2440")
    for k in range(14):  # cord
        c.px(hx - 8 + (k // 5), hy + 16 + k, "k1")
    # sockets low on the wall
    for sx in (300, 538):
        bevel(c, sx, 214, 10, 8, "k4", "k6", "k2")
        c.px(sx + 3, 217, "k0")
        c.px(sx + 6, 217, "k0")
    # a wall calendar-like grid card, left of the window (dates are dots, no text)
    gx, gy = 216, 146
    c.rect(gx, gy, 20, 22, "k0")
    c.rect(gx + 1, gy + 1, 18, 20, "#24223a")
    c.rect(gx + 1, gy + 1, 18, 4, "#3a2a44")
    for r in range(4):
        for k in range(5):
            c.px(gx + 3 + k * 3, gy + 8 + r * 3, "#3e3c5e")
    c.px(gx + 9, gy + 11, "#7a4a5a")
    c.px(gx + 10, gy, "#6a6a8a")


# --- pipes ---------------------------------------------------------------------------------------

PIPE = ["#16152c", "#22223e", "#2f3152", "#3e4266", "#545a86", "#6c74a0", "#3e4266", "#22223e"]


def vpipe(c: Canvas, x, y0, y1, w=6) -> None:
    for xx in range(x, x + w):
        t = (xx - x) / max(w - 1, 1)
        for yy in range(y0, y1):
            ramp_px(c, xx, yy, PIPE, 1 - abs(t - 0.62) * 1.5)
    c.vline(x - 1, y0, y1 - 1, "k0")
    c.vline(x + w, y0, y1 - 1, "k0")
    for yy in range(y0 + 10, y1 - 4, 38):
        c.rect(x - 2, yy, w + 4, 4, "k0")
        c.rect(x - 1, yy + 1, w + 2, 2, "k6")
        c.hline(x - 1, x + w, yy + 1, "k8")
        c.px(x - 1, yy + 2, "k3")


def hpipe(c: Canvas, x0, x1, y, h=6, hang=True) -> None:
    for yy in range(y, y + h):
        t = (yy - y) / max(h - 1, 1)
        for xx in range(x0, x1):
            ramp_px(c, xx, yy, PIPE, 1 - abs(t - 0.3) * 1.4)
    c.hline(x0, x1 - 1, y - 1, "k0")
    c.hline(x0, x1 - 1, y + h, "k0")
    for xx in range(x0 + 12, x1 - 2, 46):
        c.rect(xx, y - 2, 4, h + 4, "k0")
        c.rect(xx + 1, y - 1, 2, h + 2, "k6")
        c.vline(xx + 2, y - 1, y + h, "k8")
        if hang:  # strap to the ceiling
            c.vline(xx + 1, 0, y - 2, "k2")
            c.vline(xx + 2, 0, y - 2, "k4")


def pipes(rng: np.random.Generator) -> Canvas:
    c = Canvas()
    hpipe(c, 330, W, 12)
    hpipe(c, 360, W, 22, 4, hang=False)
    hpipe(c, 96, 330, 3, 4)
    vpipe(c, 552, 12, DESK_Y)
    vpipe(c, 566, 26, DESK_Y, 4)
    vpipe(c, 84, 0, 232)
    hpipe(c, 0, 90, 236, 5, hang=False)
    # elbow where the ceiling run meets the left riser
    c.rect(82, 1, 12, 8, "k0")
    c.rect(83, 2, 10, 6, "k5")
    c.hline(83, 92, 2, "k7")
    # cable bundle following the ceiling pipe: three muted cables with sag between straps
    for k, col in enumerate(("#2a3a4a", "#3a2a40", "#2a3a34")):
        for xx in range(330, W):
            seg = (xx - 342) % 46
            sag = round(math.sin(math.pi * seg / 46) * (2 + k)) if xx > 342 else 0
            c.px(xx, 19 + k + sag, col)
    # valve wheel and a pressure gauge on the right riser
    cx, cy = 555, 120
    for a in range(0, 360, 10):
        rr = math.radians(a)
        c.px(round(cx + 7 * math.cos(rr)), round(cy + 7 * math.sin(rr)), "k6")
        c.px(round(cx + 6 * math.cos(rr)), round(cy + 6 * math.sin(rr)), "k4")
    c.line(cx - 6, cy, cx + 6, cy, "k5")
    c.line(cx, cy - 6, cx, cy + 6, "k5")
    c.disc(cx, cy, 1.5, "k7")
    c.disc(590, 78, 9, "k0")
    c.disc(590, 78, 8, "k6")
    c.disc(590, 78, 7, "#8e8cac")
    c.disc(589, 77, 5, "#a4a2c0")
    for a in range(200, 341, 35):
        rr = math.radians(a)
        c.px(round(590 + 6 * math.cos(rr)), round(78 + 6 * math.sin(rr)), "k2")
    c.line(590, 78, 594, 73, "#a84050")
    c.px(590, 78, "k1")
    c.rect(582, 88, 16, 3, "k5")
    c.hline(582, 597, 88, "k7")
    # a junction box on the left riser with a small label plate
    bevel(c, 78, 150, 18, 22, "k4", "k6", "k2")
    c.rect(82, 155, 10, 5, "#2c2a44")
    c.hline(83, 90, 157, "#4a4870")
    for k in range(3):
        c.px(84 + k * 3, 166, "k1")
    return c


# 3 px clear + 3 px for the hair rig's largest swing (js/hair_rig.js maxAmp)
FORE_MARGIN = 6

# --- foreground --------------------------------------------------------------------------------


def foreground(char_alpha: np.ndarray, char_pos: tuple[int, int], rng) -> Canvas:
    """Dark shapes nearest the viewer, kept off the character's silhouette (base sheet + hair at rest, FORE_MARGIN px).

    A pendant lamp hanging into the upper left and leaves of a plant in the lower left. They
    frame the picture; they never cover the character (build-time check and a test)."""
    c = Canvas()
    # pendant lamp: cord, cap, shade (unlit; a faint neon rim on its right)
    lx, top = 140, 8
    c.vline(lx, 0, top, "k1")
    c.rect(lx - 2, top, 5, 3, "k2")
    for j in range(10):
        half = 3 + j
        yy = top + 3 + j
        for xx in range(lx - half, lx + half + 1):
            t = (xx - (lx - half)) / (2 * half)
            ramp_px(c, xx, yy, ["k0", "k1", "k2", "k3", "k2"], t)
        c.px(lx - half - 1, yy, "k0")
        c.px(lx + half + 1, yy, "#2e5a6a" if j > 2 else "k0")
    c.hline(lx - 13, lx + 13, top + 13, "k0")
    c.hline(lx - 9, lx + 9, top + 14, "k1")
    # big leaves in the lower left
    # (base x, base y, length, angle): stems rise from below the frame's lower-left corner
    leaves = [(96, 300, 92, -1.15), (104, 310, 84, -0.85), (88, 296, 70, -1.45), (118, 318, 74, -0.6),
              (92, 320, 60, -0.35)]  # fmt: skip
    ramp = ["#070a12", "#0b1219", "#101a22", "#16242c", "#1d3036"]
    for bx, by, ln, ang in leaves:
        dx, dy = math.cos(ang), math.sin(ang)
        nx, ny = -dy, dx
        for s in range(ln):
            t = s / ln
            if t < 0.45:  # bare stem first, then the blade
                c.px(round(bx + dx * s), round(by + dy * s), "#0b1219")
                c.px(round(bx + dx * s) + 1, round(by + dy * s), "k0")
                continue
            tb = (t - 0.45) / 0.55
            wd = math.sin(math.pi * tb**0.8) * ln * 0.12
            cxp, cyp = bx + dx * s, by + dy * s
            for k in range(-int(wd) - 1, int(wd) + 2):
                xx, yy = round(cxp + nx * k), round(cyp + ny * k)
                edge = abs(k) >= wd
                lit = 0.3 + 0.25 * (k / (wd + 1)) + 0.35 * tb
                if edge:
                    c.px(xx, yy, "k0")
                else:
                    ramp_px(c, xx, yy, ramp, lit)
            c.px(round(cxp), round(cyp), "#203a40" if s % 5 else "#16242c")  # midrib
    # keep clear of the character (2 px margin)
    cx0, cy0 = char_pos
    mask = np.zeros((H, W), bool)
    hh, ww = char_alpha.shape
    mask[cy0 : cy0 + hh, cx0 : cx0 + ww] = char_alpha > 127
    from scipy import ndimage as ndi

    mask = ndi.binary_dilation(mask, iterations=FORE_MARGIN)
    c.a[mask] = 0
    return c
