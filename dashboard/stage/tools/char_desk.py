"""Desk shot of the character (#341, night round B): she walks to the room desk, sits on a chair
with her back to us and types there, then walks back. New parts only (no existing part or tool
is changed); built from the back view the rig draws (tools/char_desk_compose.mjs).

Design reference: the commander's "背中を見せてプログラミング" sheet (chair, desk, tail out from
under the coat over the seat). The figure is about 0.4 of her size at the front: the camera is
farther from her, so the back hair no longer fills the picture.

Frames (assets/char/desk.png + desk.json):
  walk1..walk3  walking away into the room (0.66 -> 0.47 of her size), coat skirt and legs added
                below the bust, steps alternate, the tail hangs from the coat's slit
  stand         at the chair, behind it (0.42)
  sit, sit_l, sit_r, sit_bob, sit_tilt
                seated, from the back sheets back / back_l / back_r / back_bob / back_tilt (0.40);
                the shoulders and head move with the keys
  chair         the office chair (back rest, seat, post, base), drawn in front of her
  tail_c, tail_l, tail_r, tail_up, tail_down, tail_puff
                the tail out from under the coat over the seat (separate so it can move on its
                own; which one shows is decided by the motion data)
  mug           her mug on the room desk while she sits there

All colours come from the character's own palette (coat, hair) plus a chair ramp; downscaling is
a box average with premultiplied alpha, snapped to the nearest palette colour (whole pixels, no
new colours). Deterministic: no randomness.

  python3 tools/char_desk.py           # writes assets/char/desk.png, desk.json
"""

from __future__ import annotations

import json
import math
import subprocess
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

HERE = Path(__file__).resolve().parent
STAGE = HERE.parent
OUT = STAGE / "assets" / "char"
SHEET = 278

# the character's palette (parts.json `palette`, written by build_char.py): every colour of the
# desk shot is one of its named colours (the art lint's rule); the chair, the pants and the mug
# take the navy and mug ramps
PAL = json.loads((OUT / "parts.json").read_text())["palette"]
COAT = {
    "light": PAL["coat4"],
    "mid": PAL["coat3"],
    "shadow": PAL["coat2"],
    "line": PAL["line_coat"],
    "outline": PAL["outline"],
    "belt": PAL["coat1"],
}
HAIR = [PAL[f"hair{i}"] for i in (5, 4, 3, 2, 1, 0)]  # light to dark
CHAIR = {
    "rim": PAL["navy3"],
    "back": PAL["mug2"],
    "dark": PAL["mug1"],
    "seat": PAL["navy2"],
    "seat_dark": PAL["navy1"],
    "metal": PAL["mug3"],
    "outline": PAL["outline"],
}
PANTS = {"mid": PAL["navy0"], "light": PAL["mug2"], "outline": PAL["outline"]}
MUG = {"body": PAL["mug1"], "light": PAL["mug3"], "outline": PAL["line_mug"]}
# downscaling snaps to these (not the old near-black hair line, which is no longer drawn)
SNAP = [v for k, v in PAL.items() if k != "line_hair"]

# placement in the room (image coords of the world, 640 x 340): the room desk spans x 388..640,
# its top at y 224; the laptop on it at x 412..493 (narrow layout: 398..479; gadget `monitor`).
# While she is there the room pans left (main.js DESK_PAN), so the desk comes toward the middle.
SEAT_CX = 452  # where she sits (centre of her back): in front of the laptop (412..493, centre 452)
SEAT_TOP = 140  # top of her ears when seated
NARROW_DX = -14  # phone portrait: the desk props shift left by this much (gadget_catalog narrow)
# The walk (frame, scale, centre x, top y): from where she stands at the front (the sheet: centre
# x 320, top 20, scale 1) to the chair, in WALK_STEPS even steps: the scale shrinks by the same
# factor every step (distance grows evenly), the position moves evenly along the floor. The
# last position is `stand`, behind the chair.
WALK_STEPS = 8
STAND = (0.42, SEAT_CX - 4, 108)
WALK = []
for _i in range(1, WALK_STEPS + 1):
    _t = _i / WALK_STEPS
    _name = "stand" if _i == WALK_STEPS else f"walk{_i}"
    WALK.append(
        (
            _name,
            round(STAND[0] ** _t, 3),
            round(320 + (STAND[1] - 320) * _t),
            round(20 + (STAND[2] - 20) * _t),
        )
    )
# the walk cycle: contact (one leg forward), passing (legs together, a pixel higher), contact (the
# other leg), passing
CYCLE = [-1, 0, 1, 0]
SEATED = [
    ("sit", "back"),
    ("sit_l", "back_l"),
    ("sit_r", "back_r"),
    ("sit_bob", "back_bob"),
    ("sit_tilt", "back_tilt"),
]
SIT_SCALE = 0.40


def rgb(h: str) -> np.ndarray:
    return np.array([int(h[i : i + 2], 16) for i in (1, 3, 5)], np.uint8)


def compose_back(frames: list[dict]) -> np.ndarray:
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "back.raw"
        subprocess.run(
            ["node", str(HERE / "char_desk_compose.mjs"), str(out), json.dumps(frames)],
            check=True,
            capture_output=True,
        )
        raw = np.frombuffer(out.read_bytes(), np.uint8)
    return raw.reshape(len(frames), SHEET, SHEET, 4).copy()


def palette() -> np.ndarray:
    cols = (
        list(COAT.values())
        + HAIR
        + list(CHAIR.values())
        + list(PANTS.values())
        + list(MUG.values())
    )
    return np.unique(np.stack([rgb(c) for c in cols]), axis=0)


def downscale(img: np.ndarray, s: float, pal: np.ndarray) -> np.ndarray:
    """Box average with premultiplied alpha to round(size * s), snapped to the palette."""
    h, w = img.shape[:2]
    nw, nh = max(1, round(w * s)), max(1, round(h * s))
    a = img[..., 3:4].astype(np.float64) / 255.0
    pm = img[..., :3].astype(np.float64) * a
    ys = np.arange(h) * nh // h
    xs = np.arange(w) * nw // w
    acc = np.zeros((nh, nw, 4))
    cnt = np.zeros((nh, nw))
    for y in range(h):
        np.add.at(acc, (ys[y], xs), np.concatenate([pm[y], a[y]], 1))
        np.add.at(cnt, (ys[y], xs), 1)
    al = acc[..., 3] / cnt
    col = acc[..., :3] / np.maximum(acc[..., 3:4], 1e-6)
    out = np.zeros((nh, nw, 4), np.uint8)
    m = al > 0.5
    d = ((col[m][:, None, :] - pal[None].astype(np.float64)) ** 2).sum(2)
    out[m, :3] = pal[d.argmin(1)]
    out[m, 3] = 255
    return out


def despeckle(img: np.ndarray) -> np.ndarray:
    """Downscaling leaves a lone dark pixel here and there (a lash, a line end): a pixel that is
    at least 60 darker (luma) than all its 8 neighbours, inside the figure, takes the colour most
    of its neighbours have (the art lint's rule for dark specks)."""
    out = img.copy()
    h, w = img.shape[:2]
    lum = img[..., :3].astype(int) @ np.array([299, 587, 114]) / 1000
    for y in range(1, h - 1):
        for x in range(1, w - 1):
            if img[y, x, 3] == 0:
                continue
            nb = [(y + dy, x + dx) for dy in (-1, 0, 1) for dx in (-1, 0, 1) if dy or dx]
            if any(img[p][3] == 0 for p in nb):
                continue
            if all(lum[p] - lum[y, x] >= 60 for p in nb):
                cols = [tuple(img[p][:3]) for p in nb]
                out[y, x, :3] = max(set(cols), key=cols.count)
    return out


def outline(img: np.ndarray, colour: str) -> np.ndarray:
    """A 1 px outline around the silhouette where it has none (dark pixel next to the outside)."""
    a = img[..., 3] > 0
    out = img.copy()
    pad = np.pad(a, 1)
    inside = pad[1:-1, 1:-1]
    edge = inside & ~(pad[:-2, 1:-1] & pad[2:, 1:-1] & pad[1:-1, :-2] & pad[1:-1, 2:])
    lum = img[..., :3].astype(int).sum(2)
    out[edge & (lum > 200), :3] = rgb(colour)
    return despeckle(out)


def bezier(p0, p1, p2, p3, t):
    u = 1 - t
    return (
        u**3 * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t**3 * p3[0],
        u**3 * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t**3 * p3[1],
    )


# tail shapes: control points of the centre line (root first), in tail units (root at 0, 0;
# +x right, +y down; about 40 units long at rest) and the width multiplier
TAILS = {
    "c": (
        [(0, 0), (-12, 6), (-26, 22), (-16, 40)],
        1.0,
    ),  # hangs beside the seat, the tip curls back
    "l": ([(0, 0), (-14, 2), (-32, 10), (-34, 26)], 1.0),  # swung out to the left
    "r": ([(0, 0), (-6, 10), (-14, 26), (-4, 38)], 1.0),  # swung in, under the seat
    "up": ([(0, 0), (-12, -2), (-24, -14), (-16, -28)], 1.0),  # raised, the tip over (joy)
    "down": ([(0, 0), (-4, 12), (-8, 28), (-4, 42)], 0.8),  # low, close and still (worried)
    "puff": ([(0, 0), (-12, 4), (-28, 16), (-22, 34)], 1.45),  # bristled (tense)
}


def tail_sprite(kind: str, length: float = 1.0) -> tuple[np.ndarray, tuple[int, int]]:
    """The tail as in the reference (06: grey-white, bushy, the tip lighter): a curved bushy
    shape, narrow at the root, widest past the middle, pointed tip. Drawn 4x and downscaled.
    Returns the sprite and the root's position in it."""
    ctrl, wide = TAILS[kind]
    ctrl = [(x * 1.7, y * 1.7) for x, y in ctrl]
    big = 4
    k = length * big
    pts = [bezier(*ctrl, i / 40) for i in range(41)]
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    pad = 10
    ox, oy = -min(xs) + pad, -min(ys) + pad
    W = int((max(xs) - min(xs) + 2 * pad) * k) + 1
    H = int((max(ys) - min(ys) + 2 * pad) * k) + 1
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    def width(t):
        # narrow at the root (out from under the coat), bushy past the middle, a soft point
        return wide * (1.6 + 11.5 * math.sin(math.pi * min(1.0, t * 0.92)) ** 0.85)

    def strip(shrink, dx, dy, col, t0=0.0):
        left, right = [], []
        for i in range(len(pts)):
            t = i / (len(pts) - 1)
            if t < t0:
                continue
            x, y = pts[i]
            x2, y2 = pts[min(i + 1, len(pts) - 1)]
            x1, y1 = pts[max(i - 1, 0)]
            nx, ny = -(y2 - y1), x2 - x1
            nl = math.hypot(nx, ny) or 1
            nx, ny = nx / nl, ny / nl
            w = width(t) * shrink
            left.append(((x + ox + nx * w + dx) * k, (y + oy + ny * w + dy) * k))
            right.append(((x + ox - nx * w + dx) * k, (y + oy - ny * w + dy) * k))
        d.polygon(left + right[::-1], fill=col)

    strip(1.0, 0, 0, HAIR[3])  # outline / underside
    # fluffy edge: tufts along both sides, pointing out and toward the tip
    for i in range(3, len(pts) - 2, 3):
        t = i / (len(pts) - 1)
        x, y = pts[i]
        x2, y2 = pts[i + 1]
        tx, ty = x2 - x, y2 - y
        tl = math.hypot(tx, ty) or 1
        tx, ty = tx / tl, ty / tl
        nx, ny = -ty, tx
        for side in (1, -1):
            w = width(t)
            sz = (1.6 + 1.2 * ((i // 3) % 2)) * (0.6 + 0.6 * math.sin(math.pi * t))
            base1 = (x + ox + side * nx * w * 0.9 - tx * sz, y + oy + side * ny * w * 0.9 - ty * sz)
            base2 = (x + ox + side * nx * w * 0.9 + tx * sz, y + oy + side * ny * w * 0.9 + ty * sz)
            tipp = (
                x + ox + side * nx * (w + sz) + tx * sz * 1.4,
                y + oy + side * ny * (w + sz) + ty * sz * 1.4,
            )
            d.polygon(
                [
                    (base1[0] * k, base1[1] * k),
                    (tipp[0] * k, tipp[1] * k),
                    (base2[0] * k, base2[1] * k),
                ],
                fill=HAIR[3] if side < 0 else HAIR[2],
            )
    strip(0.86, 0.0, -0.3, HAIR[2])
    strip(0.66, -0.3, -0.7, HAIR[1])
    strip(0.36, -0.6, -1.0, HAIR[0], t0=0.25)
    # the lighter tip
    strip(0.7, 0, 0, HAIR[0], t0=0.84)
    # fur strokes along the length
    for off in (-0.45, 0.2):
        line = []
        for i in range(6, len(pts) - 6):
            t = i / (len(pts) - 1)
            x, y = pts[i]
            x2, y2 = pts[i + 1]
            nx, ny = -(y2 - y), x2 - x
            nl = math.hypot(nx, ny) or 1
            w = width(t) * off
            line.append(((x + ox + nx / nl * w) * k, (y + oy + ny / nl * w) * k))
        d.line(line, fill=HAIR[2], width=max(1, big // 2))
    arr = np.asarray(img)
    small = outline(downscale(arr, 1 / big, palette()), HAIR[3])
    return small, (round(ox * length), round(oy * length))


NAVY = {PAL["navy0"], PAL["navy1"], PAL["navy2"], PAL["line_navy"], PAL["coat0"], PAL["hair1"]}


def full_figure(bust: np.ndarray, view: str, step: int) -> np.ndarray:
    """The bust (sheet scale, cut at the hips) continued to the feet: the coat's last full row
    extruded down to the knees (flaring a little, the hem swaying with the step), black pants and
    the legs below it; from the back the coat's slit and the tail hanging from it.
    step: -1 / 1 which leg is forward, 0 still."""
    H = 470
    out = np.zeros((H, SHEET, 4), np.uint8)
    cut = 275  # the bust's last rows are its frame outline
    out[:cut] = bust[:cut]
    row = bust[cut - 1].copy()
    navy = {tuple(rgb(c)) for c in NAVY}
    is_navy = np.array([tuple(row[x, :3]) in navy and row[x, 3] > 0 for x in range(SHEET)])
    xs = np.where(row[:, 3] > 0)[0]
    if not len(xs):
        return out
    x0, x1 = int(xs.min()), int(xs.max())
    if view == "front" and is_navy.any():
        # the front sheet's coat drapes wide over her arm: the skirt follows the torso only
        nx = np.where(is_navy)[0]
        tc = (nx.min() + nx.max()) / 2
        x0, x1 = max(x0, round(tc - 72)), min(x1, round(tc + 72))
        # and the drape over her arm narrows into it above the cut
        for y in range(215, cut):
            k = (y - 215) / (cut - 215)
            ys = np.where(out[y, :, 3] > 0)[0]
            if not len(ys):
                continue
            lo = round(ys.min() + (x0 - ys.min()) * k) if ys.min() < x0 else ys.min()
            hi = round(ys.max() + (x1 - ys.max()) * k) if ys.max() > x1 else ys.max()
            out[y, :lo, 3] = 0
            out[y, hi + 1 :, 3] = 0
    cx = (x0 + x1) / 2
    hem = 372
    sw = 3 * step
    # the skirt's columns: coat (shaded in bands from its panel edges) or the turtleneck / pants
    navy_x = [x for x in range(x0, x1 + 1) if is_navy[x]]
    n0, n1 = (min(navy_x), max(navy_x)) if navy_x else (None, None)
    for y in range(cut, hem):
        k = (y - cut) / (hem - cut)
        flare = round(5 * k)
        sway = round(sw * k)
        left, right = x0 - flare + sway, x1 + flare + sway
        for x in range(max(0, left), min(SHEET, right + 1)):
            u = x - sway  # column in the bust's row
            inner = n0 is not None and n0 <= u <= n1
            if inner:
                c = rgb(PAL["navy1"]) if y < cut + 22 else rgb(PANTS["mid"])
            else:
                # panel edges: outer edge, and the inner edge at the turtleneck (front) / slit (back)
                pl, pr = (
                    (left, n0 + sway - 1)
                    if (n0 is not None and x < cx)
                    else ((n1 + sway + 1, right) if n0 is not None else (left, right))
                )
                dist = min(x - pl, pr - x)
                width = max(1, pr - pl)
                pos = (x - pl) / width
                if dist <= 6:
                    c = rgb(COAT["shadow"])
                elif 0.28 <= pos <= 0.4:
                    c = rgb(COAT["light"])
                else:
                    c = rgb(COAT["mid"])
            out[y, x, :3] = c
            out[y, x, 3] = 255
    img = Image.fromarray(out)
    d = ImageDraw.Draw(img)
    # legs below the hem (the forward leg ends a little higher: it is farther away)
    half = 26 if view != "profile" else 18
    for lx, fwd in (
        (cx - half - 22, step < 0),
        (cx + 22 - (0 if view != "profile" else 18), step > 0),
    ):
        bot = H - (12 if fwd else 0)
        d.polygon(
            [(lx - 0, hem - 6), (lx + 46, hem - 6), (lx + 42, bot), (lx + 6, bot)],
            fill=PANTS["mid"],
            outline=PANTS["outline"],
        )
        d.line([(lx + 16, hem), (lx + 18, bot - 4)], fill=PANTS["light"], width=4)
    # hem line
    d.line([(x0 - 5 + sw, hem - 1), (x1 + 5 + sw, hem - 1)], fill=COAT["outline"], width=3)
    if view == "back":
        sx = round(cx)
        d.line([(sx, 300), (sx + sw, hem)], fill=COAT["line"], width=3)
        d.line([(sx + 5, 304), (sx + 5 + sw, hem - 2)], fill=COAT["shadow"], width=3)
    arr = np.asarray(img).copy()
    # legs are behind the coat: redraw the coat rows over them (the hem line stays)
    for y in range(cut, hem - 2):
        m = out[y, :, 3] > 0
        arr[y, m] = out[y, m]
    if view == "back":
        # the tail hangs from the slit, swinging against the step (drawn at this scale)
        # hangs down from the slit and swings a little against the step
        t, (rx, ry) = tail_sprite("down", 1.6)
        ox, oy = round(cx) - rx + 2 * sw + 10, 330 - ry
        h, w = t.shape[:2]
        y0, xa = max(0, oy), max(0, ox)
        sub = t[y0 - oy : min(H, oy + h) - oy, xa - ox : min(SHEET, ox + w) - ox]
        region = arr[y0 : y0 + sub.shape[0], xa : xa + sub.shape[1]]
        m = sub[..., 3] > 0
        region[m] = sub[m]
    return arr


# Typing hands per seated frame: (left, right) hand lift in px (1 = pressing a key: the hand a
# pixel lower, the wrist with it)
ARM_KEYS = {"sit": (0, 0), "sit_l": (1, 0), "sit_r": (0, 1), "sit_bob": (1, 1), "sit_tilt": (0, 0)}
SKIN = {"light": PAL["skin4"], "mid": PAL["skin3"], "line": PAL["line_skin"]}
CUFF = {"mid": PAL["navy2"], "dark": PAL["navy1"], "line": PAL["line_navy"]}


def with_arms(bust: np.ndarray, keys: tuple[int, int]) -> np.ndarray:
    """Her arms reaching forward to the keyboard, seen from behind (reference 01): from each
    shoulder the coat's sleeve runs down and out to the elbow beside her body, the forearm goes
    forward and in (foreshortened: short and up), and at the keyboard's edge the navy knit cuff
    and the pale hand show beside her waist (round A's hand palette: light skin, the skin line as
    contour, no black next to the skin). keys: (left, right) hand pressed."""
    h, w = bust.shape[:2]
    a = bust[..., 3] > 0

    # her body's edges at the shoulder and the waist rows
    def edges(y):
        xs = np.where(a[y])[0]
        return (int(xs.min()), int(xs.max())) if len(xs) else (0, w - 1)

    sy = int(h * 0.58)  # shoulder row (the coat's shoulder, beside the hair)
    ey = int(h * 0.84)  # elbow row
    hy = int(h * 0.73)  # hands: at the keyboard's height (its edge, beside her body)
    ls, rs = edges(sy)
    pad = 8
    out = np.zeros((h, w + 2 * pad, 4), np.uint8)
    out[:, pad : pad + w] = bust
    img = Image.fromarray(out)
    d = ImageDraw.Draw(img)
    for side, press in ((-1, keys[0]), (1, keys[1])):
        sx = (ls + 4 if side < 0 else rs - 4) + pad  # the sleeve starts at the shoulder's edge
        ex = (ls - 5 if side < 0 else rs + 5) + pad  # the elbow, out beside her body
        hx = (ls + 3 if side < 0 else rs - 3) + pad  # the hand at the keyboard's edge
        hyy = hy + press
        # upper arm (sleeve): shoulder -> elbow, lit on the outside
        d.polygon(
            [
                (sx - 4 * side, sy),
                (sx + 3 * side, sy - 1),
                (ex + 3 * side, ey - 1),
                (ex - 2 * side, ey + 2),
            ],
            fill=COAT["mid"],
            outline=COAT["outline"],
        )
        d.line(
            [(sx, sy + 2), (ex, ey - 1)],
            fill=COAT["light"] if side > 0 else COAT["shadow"],
            width=1,
        )
        # forearm: elbow -> wrist, forward and in (short: it points away from us)
        d.polygon(
            [
                (ex - 2 * side, ey + 2),
                (ex + 3 * side, ey - 1),
                (hx + 3 * side, hyy - 2),
                (hx - 1 * side, hyy + 1),
            ],
            fill=COAT["shadow"],
            outline=COAT["outline"],
        )
        # the navy knit cuff and the pale hand on the keys
        cx0, cx1 = sorted((hx, hx + 3 * side))
        d.rectangle([cx0, hyy - 3, cx1, hyy], fill=CUFF["mid"])
        d.line([(cx0, hyy), (cx1, hyy)], fill=CUFF["dark"])
        # the hand: 5 x 4, the light skin on top (the knuckles), the skin line under it
        hx0, hx1 = sorted((hx - 1 * side, hx - 5 * side))
        d.rectangle([hx0, hyy - 4, hx1, hyy - 1], fill=SKIN["mid"])
        d.line([(hx0, hyy - 4), (hx1, hyy - 4)], fill=SKIN["light"])
        d.line([(hx0, hyy), (hx1, hyy)], fill=SKIN["line"])
        d.line(
            [(hx1 if side > 0 else hx0, hyy - 3), (hx1 if side > 0 else hx0, hyy)],
            fill=SKIN["line"],
        )
    return np.asarray(img).copy()


def chair_sprite() -> np.ndarray:
    """Office chair from behind, at the seated scale (reference 01): a rounded back rest, dark
    grey with a navy rim and a lighter top edge, the bar down to the seat, the navy seat cushion
    below it, the post and the five-star base."""
    W, H = 76, 84
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    # seat cushion (wider than the back, seen a little from above)
    d.rounded_rectangle([1, 42, W - 2, 56], radius=7, fill=CHAIR["seat"], outline=CHAIR["outline"])
    d.line([(6, 52), (W - 7, 52)], fill=CHAIR["seat_dark"], width=2)
    # post and base
    d.rectangle([W // 2 - 2, 56, W // 2 + 2, 72], fill=CHAIR["metal"], outline=CHAIR["outline"])
    d.polygon(
        [(W // 2 - 26, 78), (W // 2 + 26, 78), (W // 2 + 4, 71), (W // 2 - 4, 71)],
        fill=CHAIR["dark"],
        outline=CHAIR["outline"],
    )
    for wx in (W // 2 - 27, W // 2 - 2, W // 2 + 23):
        d.ellipse([wx, 77, wx + 5, 82], fill=CHAIR["dark"], outline=CHAIR["outline"])
    # the bar from the back rest to the seat
    d.rectangle([W // 2 - 4, 30, W // 2 + 3, 44], fill=CHAIR["dark"], outline=CHAIR["outline"])
    # back rest: rounded, wider at the top, a navy rim, the top edge lit
    d.rounded_rectangle([7, 0, W - 8, 34], radius=11, fill=CHAIR["rim"], outline=CHAIR["outline"])
    d.rounded_rectangle([10, 3, W - 11, 31], radius=9, fill=CHAIR["back"])
    d.arc([10, 3, W - 11, 24], 200, 340, fill=CHAIR["rim"], width=1)
    d.line([(W // 2 - 9, 9), (W // 2 + 8, 9)], fill=CHAIR["dark"], width=2)  # the grip slot
    return np.asarray(img).copy()


def monitor_sprite() -> np.ndarray:
    """A display on a stand behind the laptop (reference 01: the screens around her head): a dark
    bezel, the screen with lines of code, the stand down to the desk. Wider than her head, so the
    code shows above and beside it while she sits in front of it."""
    W, H = 104, 92
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rectangle([W // 2 - 4, 60, W // 2 + 3, H - 5], fill=PAL["mug2"], outline=PAL["outline"])
    d.rectangle([W // 2 - 16, H - 5, W // 2 + 15, H - 1], fill=PAL["mug1"], outline=PAL["outline"])
    d.rectangle([0, 0, W - 1, 62], fill=PAL["mug1"], outline=PAL["outline"])
    d.rectangle([3, 3, W - 4, 58], fill=PAL["navy0"])
    d.line([(3, 3), (W - 4, 3)], fill=PAL["navy1"])
    # code: indented lines in a few colours (stable pattern)
    cols = [PAL["eye3"], PAL["sweat"], PAL["blush"], PAL["eye2"], PAL["skin3"]]
    y = 6
    k = 0
    while y < 55:
        ind = (2, 6, 10, 6, 2, 6, 10, 14, 6)[k % 9]
        ln = (64, 40, 76, 30, 52, 82, 34, 48, 22)[k % 9]
        x = 6 + ind
        seg = 0
        while x < min(W - 8, 6 + ind + ln):
            sl = (5, 9, 4, 7, 6)[(k + seg) % 5]
            d.line([(x, y), (min(x + sl, W - 8), y)], fill=cols[(k + seg) % len(cols)])
            x += sl + 2
            seg += 1
        y += 3
        k += 1
    d.rectangle([W - 10, 54, W - 7, 55], fill=PAL["eye3"])  # the caret
    return np.asarray(img).copy()


def mug_sprite() -> np.ndarray:
    W, H = 9, 9
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.rectangle([0, 1, 6, 8], fill=MUG["body"], outline=MUG["outline"])
    d.line([(2, 2), (2, 7)], fill=MUG["light"])
    d.arc([4, 2, 8, 6], -90, 90, fill=MUG["outline"])
    return np.asarray(img).copy()


def pack(frames: dict[str, np.ndarray]) -> tuple[np.ndarray, dict[str, list[int]]]:
    """Shelf packing, in the given order (stable)."""
    W = 1024
    x = y = row = 0
    rects = {}
    for k, im in frames.items():
        h, w = im.shape[:2]
        if x + w > W:
            x, y, row = 0, y + row + 1, 0
        rects[k] = [x, y, w, h]
        x += w + 1
        row = max(row, h)
    H = y + row
    atlas = np.zeros((H, W, 4), np.uint8)
    for k, (x, y, w, h) in rects.items():
        atlas[y : y + h, x : x + w] = frames[k]
    return atlas, rects


def build() -> dict:
    pal = palette()
    sheets = ["back", "base", "profile"] + [s for _, s in SEATED[1:]]
    comps = dict(zip(sheets, compose_back([{"sheet": s, "armR": "mug"} for s in sheets])))
    frames: dict[str, np.ndarray] = {}
    place: dict[str, list[int]] = {}
    for i, (name, s, cx, top) in enumerate(WALK):
        step = 0 if name == "stand" else CYCLE[i % 4]
        im = outline(downscale(full_figure(comps["back"], "back", step), s, pal), COAT["outline"])
        frames[name] = im
        place[name] = [cx - im.shape[1] // 2, top - (1 if step == 0 and name != "stand" else 0)]
    # coming back: she turns at the chair (side view) and walks toward us (front view, the mug in
    # her hand), along the same path
    turn = outline(
        downscale(full_figure(comps["profile"], "profile", 0), WALK[-1][1], pal), COAT["outline"]
    )
    frames["turn"] = turn
    place["turn"] = [WALK[-1][2] - turn.shape[1] // 2 + 8, WALK[-1][3]]
    for i, (name, s, cx, top) in enumerate(WALK[:-1]):
        step = CYCLE[i % 4]
        im = outline(downscale(full_figure(comps["base"], "front", step), s, pal), COAT["outline"])
        fname = name.replace(
            "walk", "back"
        )  # back1..back7: walking back to her place (back1 nearest)
        frames[fname] = im
        place[fname] = [cx - im.shape[1] // 2 - round(20 * s), top - (1 if step == 0 else 0)]
    for name, sheet in SEATED:
        im = outline(downscale(comps[sheet], SIT_SCALE, pal), COAT["outline"])
        im = with_arms(im, ARM_KEYS[name])
        frames[name] = im
        place[name] = [SEAT_CX - im.shape[1] // 2, SEAT_TOP]  # (centred: the arms pad both sides)
    ch = chair_sprite()
    frames["chair"] = ch
    bust_h = frames["sit"].shape[0]
    place["chair"] = [SEAT_CX - ch.shape[1] // 2, SEAT_TOP + bust_h - 40]
    root = (
        place["chair"][0] + 20,
        place["chair"][1] + 38,
    )  # from under the coat, over the seat, at the left
    for k in ("c", "l", "r", "up", "down", "puff"):
        t, (rx, ry) = tail_sprite(k)
        frames[f"tail_{k}"] = t
        place[f"tail_{k}"] = [root[0] - rx, root[1] - ry]
    mon = monitor_sprite()
    frames["monitor"] = mon
    place["monitor"] = [SEAT_CX - mon.shape[1] // 2, 222 - mon.shape[0]]  # on the desk (top at 224)
    frames["mug"] = mug_sprite()
    place["mug"] = [498, 214]  # on the desk, right of the laptop
    atlas, rects = pack(frames)
    meta = {
        "note": "desk shot (#341 night round B): built by tools/char_desk.py from the back view; do not edit by hand",
        "size": [int(atlas.shape[1]), int(atlas.shape[0])],
        "narrowDx": NARROW_DX,
        "frames": {k: {"rect": rects[k], "at": place[k]} for k in frames},
        "body": [n for n, *_ in WALK]
        + [n for n, _ in SEATED]
        + ["turn"]
        + [f"back{i}" for i in range(1, WALK_STEPS)],
        "seated": [n for n, _ in SEATED],
        "tails": [f"tail_{k}" for k in ("c", "l", "r", "up", "down", "puff")],
    }
    return {"atlas": atlas, "meta": meta}


def main() -> None:
    r = build()
    Image.fromarray(r["atlas"]).save(OUT / "desk.png", optimize=True)
    (OUT / "desk.json").write_text(json.dumps(r["meta"], indent=1) + "\n")
    print(json.dumps({"atlas": r["meta"]["size"], "frames": len(r["meta"]["frames"])}))


if __name__ == "__main__":
    main()
