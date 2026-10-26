"""Extra parts of the Stage character for the motion layer (#341, night round A2): ear
variants, the tail, the earbud. Written to their own files (``assets/char/extras.png`` +
``extras.json``); the parts atlas (``parts.png`` / ``parts.json``) is not touched. The asset
contract (names, anchors, slots) is in docs/stage.md, "Asset contract: extras".

* **Ears** (05 / 06: grey outer fur, a pinkish inner ear): every variant per side, placed like
  the parts atlas' ears (``ox`` / ``oy`` relative to the ear pivot = the sheet anchor
  ``ear_l`` / ``ear_r``): ``std`` (= the rest ear), ``droop`` (flattened to the side: puzzled /
  flustered, 05), ``twitch1`` / ``twitch2`` (a flick at a peak: up and out, then back past the
  rest), ``red.<angle>`` (blushing, 05: the ear turns pink; at every angle of the ear channel).
* **Tail** (06 / 03 / 01: grey-white, bushy, the tip lighter): ``front.<pose>`` — seen past
  her hip at the picture's right, behind her (it sways: ``l2 l1 c r1 r2``, raised ``up``,
  lowered ``down``); ``back.<pose>`` — in the back view it hangs past the chair at the
  picture's left, in front of the coat (``l1 c r1``). ``ox`` / ``oy`` are sheet pixels (the
  tail does not move with the head).
* **Earbud**: the black earbud in her ear (03 / 06) as its own sprite (``ox`` / ``oy`` from the
  sheet anchor ``eye_r``: it moves with the head), for a layer that covers the ear; it is
  always black, whatever the fox ears do (it sits in her human ear, not on them).
"""

from __future__ import annotations

import json
from pathlib import Path

import build_char as bc
import numpy as np
from scipy import ndimage as ndi

EAR_DROOP = {"l": -42.0, "r": 56.0}  # deg (picture: + = clockwise); flattened to the side
EAR_DROOP_SCALE = 0.88  # the ear is a little shorter seen flat
EAR_TWITCH = {"twitch1": (11.0, 1.04, -1.0), "twitch2": (-4.0, 1.0, 0.0)}  # deg out, scale, dy
EARBUD_ANCHOR_AT = (156.0, 97.0)  # the base sheet's eye_r anchor: the earbud moves with it
TAIL_FRONT = {  # sway angle about the root (deg, + = clockwise: the tip toward the picture's
    # edge), lift (1: raised, the tip higher and curled; -1: lowered)
    "l2": (-10.0, 0.0), "l1": (-5.0, 0.0), "c": (0.0, 0.0), "r1": (2.5, 0.0), "r2": (5.0, 0.0),
    "up": (-5.0, 1.0), "down": (5.0, -1.0),
}  # fmt: skip
TAIL_BACK = {"l1": (-6.0, 0.0), "c": (0.0, 0.0), "r1": (4.0, 0.0)}
TAIL_FRONT_ROOT = (212.0, 302.0)  # below the sheet (her hip): it rises past her side
TAIL_BACK_ROOT = (150.0, 308.0)  # from under the coat's back, below the sheet
# the spine of the tail at rest (sheet px, relative to the root), root .. tip
# art unify 4 (06 / 03): an S — out from her hip, back in, the tip flicking out again
TAIL_FRONT_SPINE = [(0, 0), (22, -30), (36, -62), (30, -92), (32, -116), (46, -132)]
# the back view: up over the coat's lower back and out to her left, the tip curling up
TAIL_BACK_SPINE = [(0, 0), (16, -26), (38, -42), (62, -50), (80, -62), (86, -80)]
TAIL_WIDTH = [(0.0, 10.0), (0.2, 15.0), (0.45, 19.0), (0.7, 17.0), (0.88, 11.0), (1.0, 2.0)]


def _rgb(h):
    return (bc.hexrgb(h) * 255).round().astype(np.uint8)


# --- ears -----------------------------------------------------------------------------------


def _ear_sprite(parts, side, ang, scale=1.0, dy=0.0, bend=5.0, rigid=False):
    """The icon's ear re-pixelized after a rotation about its pivot (deg, + = clockwise) and a
    scale along it; it bends at its base as the parts' ears do (char_parts.build_ears)."""
    px = parts.px
    e = parts.EARS[side]
    size = parts.size
    region = ndi.binary_dilation(bc.poly(size, e["poly"]), iterations=1)
    base_band = ndi.binary_dilation(bc.poly(size, e["poly"]), iterations=4) & ~region
    base_band &= ndi.binary_dilation(bc.poly(size, e["base"] + [e["pivot"]]), iterations=3)
    keep = region | base_band
    pvx, pvy = e["pivot"]
    w = np.clip(ndi.distance_transform_edt(~base_band) / bend, 0, 1).astype(np.float32)
    if rigid:  # the whole ear turns about its pivot (its root hides under the crown)
        keep = region
        w = np.ones_like(w)
    no_line = ndi.binary_dilation(base_band, iterations=3) & ~region
    t = np.radians(ang)
    ox, oy = px.xx - pvx, px.yy - pvy - dy
    # exact inverse: the source of a picture pixel is R^-1 S^-1 (p - pivot)
    sx = (np.cos(t) * ox + np.sin(t) * oy) / scale
    sy = (-np.sin(t) * ox + np.cos(t) * oy) / scale
    dx = (px.xx - (pvx + sx)) * w
    dyy = (px.yy - (pvy + sy)) * w
    alpha = px.alpha * px.up(keep)
    img, _, _ = px.render(px.smooth, alpha, px.ink, px.mat, dx, dyy, no_outline=no_line)
    from char_unify import unify

    return unify(img, part=True)


HEAD_DOME = (140.0, 88.0, 52.0, 57.0)  # cx, cy, rx, ry: the top of the head without the ears


def head_cap(parts):
    """The top of the head where the ears stood (the icon has no hair there): a dome in the
    crown's own tones (each column continues its top crown pixel), lighter at the top, its rim
    a hair line."""
    crown = parts.base_imgs["crown"]
    a = crown[..., 3] > 0
    h, w = a.shape
    yy, xx = np.mgrid[0:h, 0:w]
    cx, cy, rx, ry = HEAD_DOME
    dome = ((xx - cx) / rx) ** 2 + ((yy - cy) / ry) ** 2 <= 1.0
    # only where the ears stood (elsewhere the crown or the hair layer draws the head)
    ears = np.zeros_like(a)
    for e in parts.EARS.values():
        ears |= ndi.binary_dilation(bc.poly(parts.size, e["poly"]), iterations=1)
    head = (parts.full[..., 3] > 0) & ~ears
    cap = dome & ~a & ~head & (yy < 76)
    out = np.zeros_like(crown)
    hair = [_rgb(c) for c in bc.RAMPS["hair"]]
    dith = bc.BAYER4[yy % 4, xx % 4]
    for x in range(w):
        col = np.nonzero(cap[:, x])[0]
        if not len(col):
            continue
        below = np.nonzero(a[:, x])[0]
        if not len(below):
            continue
        top = below.min()
        for y in col:
            depth = (top - y) / 12.0
            v = 4.2 + 0.6 * np.clip(depth, 0, 1) + 0.55 * dith[y, x]
            out[y, x, :3] = hair[int(np.clip(np.floor(v), 0, 5))]
            out[y, x, 3] = 255
    rim = cap & ~ndi.binary_erosion(dome)
    out[rim, :3] = _rgb(bc.RAMPS["hair"][1])
    return out


def _shift(img, dx, dy):
    out = np.zeros_like(img)
    h, w = img.shape[:2]
    ys, xs = np.nonzero(img[..., 3] > 0)
    ty, tx = ys + dy, xs + dx
    ok = (ty >= 0) & (ty < h) & (tx >= 0) & (tx < w)
    out[ty[ok], tx[ok]] = img[ys[ok], xs[ok]]
    return out


DISK3 = np.array([[dx * dx + dy * dy <= 9 for dx in range(-3, 4)] for dy in range(-3, 4)])
DISK6 = np.array([[dx * dx + dy * dy <= 36 for dx in range(-6, 7)] for dy in range(-6, 7)])
HAIR_LIGHT = [_rgb(c) for c in bc.RAMPS["hair"][3:]]
HAIR_DARK = [_rgb(c) for c in bc.RAMPS["hair"][:2]] + [_rgb(bc.OUTLINE), _rgb(bc.LINE["hair"])]


def _is(img, cols):
    rgb = img[..., :3].astype(np.int16)
    m = np.zeros(img.shape[:2], bool)
    for c in cols:
        m |= (rgb == c).all(2)
    return m & (img[..., 3] > 0)


def droop_sheet(parts, side, droop, cap, name):
    """The drooped ear of one front sheet, as (sprite, ox, oy) from the sheet's ear anchor: the
    ear turned about its pivot (placed at the anchor as the rig places every ear), the top of the
    head (cap) and the crown moved with the sheet's field, the ear's root behind them. Then where
    the ear meets the head (#341, 2026-10-18, WARNING / CRITICAL): the V of room between the
    ear's straight root cut and the head is filled with the cap's hair, a corner of the root left
    standing alone is cut, the new edge is the hair's outline, and a dark line inside the hair
    (the cap's rim drawn across the root) is a soft hair line (hair2, as on light hair)."""
    import char_layers as cl
    from char_parts import SHEETS
    from char_unify import hair_lines, specks, unify

    px = parts.px
    imgs = parts.sheet_imgs[name]
    d = parts.sheet_fields[name]
    spec = SHEETS[name]
    pvx, pvy = parts.EARS[side]["pivot"]
    ax, ay = parts.meta["sheets"][name]["anchors"][f"ear_{side}"]
    rx, ry = cl._js_round(ax), cl._js_round(ay)  # where the rig puts the ear's pivot
    ear = _shift(droop, rx - pvx, ry - pvy)
    cap_s = cap if d is None else px.warp_lo(cap, *d)
    crown = imgs["crown"]
    head = np.where((crown[..., 3] > 0)[..., None], crown, cap_s)
    out = ear.copy()
    m = (cap_s[..., 3] > 0) & (ear[..., 3] == 0)
    out[m] = cap_s[m]
    over = (out[..., 3] > 0) & (head[..., 3] > 0)
    out[over] = head[over]  # the ear comes out from behind the head
    out = hair_lines(out)
    # her figure around it in this sheet (the parts and the hair layer at rest, no ears), and
    # the picture there with this ear drawn (for the colours around a pixel)
    fig = np.zeros(out.shape[:2], bool)
    base = np.zeros_like(out)
    for layer in cl.LAYERS:
        if layer["id"] in imgs:
            img = unify(imgs[layer["id"]], part=True)  # as the atlas stores it (Parts.add)
            m = img[..., 3] > 0
            fig |= m
            base[m] = img[m]
    front, _, hair = cl.live_hair(spec, with_rgba=True)
    fig |= front

    def picture(sprite):
        pic = base.copy()
        m = sprite[..., 3] > 0
        pic[m] = sprite[m]
        pic[front] = hair[front]
        return pic

    yy, xx = px.yy, px.xx
    lo, hi = (rx - 50, rx + 28) if side == "l" else (rx - 30, rx + 48)
    zone = (yy <= ry) & (xx >= lo) & (xx <= hi)
    a = out[..., 3] > 0
    sil = fig | a
    for _ in range(2):  # a corner standing alone (2 neighbours of her or fewer)
        n8 = ndi.convolve(sil.astype(np.int16), np.ones((3, 3), np.int16), mode="constant") - sil
        alone = a & ~fig & zone & (n8 <= 2)
        out[alone] = 0
        a = out[..., 3] > 0
        sil = fig | a
    # the V between the ear's root cut and the head: closed with a disk of radius 6 where the ear
    # and the head meet (within 8 px of both), so the head rises into the ear's edge; then any
    # dimple left on the edge (a disk of radius 3)
    ear_a = ear[..., 3] > 0
    root = ndi.binary_dilation(ear_a, iterations=8) & ndi.binary_dilation(
        fig | (cap_s[..., 3] > 0), iterations=8
    )
    filled = np.zeros_like(sil)
    for step in range(4):
        if step == 0:
            notch = ndi.binary_closing(np.pad(sil, 8), structure=DISK6)[8:-8, 8:-8] & root
        else:
            notch = ndi.binary_closing(np.pad(sil, 4), structure=DISK3)[4:-4, 4:-4]
        notch &= ~sil & zone
        if not notch.any():
            continue
        light = _is(out, HAIR_LIGHT)
        _, (iy, ix) = ndi.distance_transform_edt(~light, return_indices=True)
        out[notch] = out[iy[notch], ix[notch]]
        filled |= notch
        a = out[..., 3] > 0
        sil = fig | a
    notch = filled
    cross = np.array([[0, 1, 0], [1, 1, 1], [0, 1, 0]], bool)
    room = ~ndi.binary_erosion(sil, structure=cross, border_value=1) & sil
    edge = notch & room  # the new edge of the head: the hair's outline
    out[edge, :3] = _rgb(bc.RAMPS["hair"][1])
    # beside the room on any side (8 neighbours): an outline, kept
    by_room = ~ndi.binary_erosion(sil, structure=np.ones((3, 3), bool), border_value=1) & sil
    pic = picture(out)
    dark = _is(out, HAIR_DARK) & zone & ~by_room & ~front
    ones = np.ones((3, 3), np.int16)
    n_light = ndi.convolve(_is(pic, HAIR_LIGHT).astype(np.int16), ones, mode="constant")
    out[dark & (n_light >= 5), :3] = _rgb(bc.RAMPS["hair"][2])
    spr, x0, y0 = _crop(specks(out))  # (the sprite on its own: the extras lint's rule)
    return spr, x0 - rx, y0 - ry


def blush_ear(img):
    """照れ (05): the ear warms from the inside — its middle in pale skin and blush (the fur's
    own tones kept), the rim of fur, its lines and its outline stay grey (art unify 4: the
    whole ear in strong pink read as a different ear)."""
    out = img.copy()
    rgb = img[..., :3]
    a = img[..., 3] > 0
    hair = [_rgb(c) for c in bc.RAMPS["hair"]]
    level = np.full(a.shape, -1, np.int8)
    for i, c in enumerate(hair):
        level[(rgb == c).all(2) & a] = i
    # the inner ear: the ear's middle (more than 3 px in from its edge), its tones kept and
    # warmed — dark steps to the darker pale skin, the middle steps to blush on a checker, the
    # light fur half touched with pale skin; the 3 px rim of fur stays grey
    dist = ndi.distance_transform_edt(a)
    inner = a & (dist > 3.0) & (level >= 0)
    skin = [_rgb(c) for c in bc.RAMPS["skin"]]
    blush = _rgb("#e88f97")
    yy, xx = np.mgrid[0 : a.shape[0], 0 : a.shape[1]]
    check = (xx + yy) % 2 == 0
    out[inner & (level <= 1), :3] = skin[2]
    out[inner & (level == 2), :3] = np.where(check[..., None], blush, skin[2])[inner & (level == 2)]
    out[inner & (level == 3), :3] = np.where(check[..., None], blush, skin[3])[inner & (level == 3)]
    m = inner & (level >= 4) & check
    out[m, :3] = skin[3]
    from char_unify import specks

    return specks(out)  # (no dark fur pixel left alone among the lighter skin)


# --- tail -----------------------------------------------------------------------------------


def _spine(root, pts, ang, lift):
    """The spine rotated about the root (deg, + = clockwise) and lifted (0..1: the tip higher
    and further out; -1: drooped)."""
    t = np.radians(ang)
    r = np.array(root, np.float32)
    out = []
    n = len(pts) - 1
    for i, (x, y) in enumerate(pts):
        f = i / n
        y = y - 14 * lift * f**1.5
        x = x * (1 + 0.12 * lift * f)
        out.append(r + np.array([np.cos(t) * x - np.sin(t) * y, np.sin(t) * x + np.cos(t) * y]))
    return out


def tail_sprite(parts, root, spine_pts, ang, lift, seed):
    """A bushy tail along a spine: a tube whose half width swells and narrows to a point, its
    edge in fur clumps, lit from the upper left, the underside in shade, the tip lighter, a
    few fur strokes along it."""
    from char_back import HAIR, HCanvas, spline

    px = parts.px
    cv = HCanvas(px)
    s = bc.F
    pts = np.array(spline(_spine(root, spine_pts, ang, lift), k=16), np.float32)
    seg = np.diff(pts, axis=0)
    L = np.hypot(seg[:, 0], seg[:, 1])
    cum = np.concatenate([[0], np.cumsum(L)])
    total = cum[-1]
    X, Y = cv.X / s, cv.Y / s
    best = np.full(X.shape, np.inf, np.float32)
    s_of = np.zeros(X.shape, np.float32)
    side = np.zeros(X.shape, np.float32)
    for i in range(len(seg)):
        a, d = pts[i], seg[i]
        l2 = float(d @ d) or 1e-6
        u = np.clip(((X - a[0]) * d[0] + (Y - a[1]) * d[1]) / l2, 0, 1)
        qx, qy = X - (a[0] + u * d[0]), Y - (a[1] + u * d[1])
        dist = np.hypot(qx, qy)
        closer = dist < best
        best[closer] = dist[closer]
        s_of[closer] = ((cum[i] + u * L[i]) / total)[closer]
        cross = d[0] * qy - d[1] * qx
        side[closer] = np.sign(cross)[closer]
    ws, wv = zip(*TAIL_WIDTH, strict=True)
    half = np.interp(s_of, ws, wv)
    # fur clumps on the edge: rounded bumps of uneven size along each side, some ending in a
    # pointed tuft that sweeps toward the tip
    rng = np.random.default_rng(seed)
    n_clumps = int(total / 9.0) + 2
    amp = rng.uniform(0.08, 0.2, size=(2, n_clumps + 2))
    tuft = rng.uniform(0, 1, size=(2, n_clumps + 2)) < 0.45
    k = s_of * total / 9.0 + rng.uniform(0, 1) + 0.5 * (side > 0)
    ki = np.clip(np.floor(k).astype(int), 0, n_clumps + 1)
    frac = k - np.floor(k)
    si = (side > 0).astype(int)
    bump = 0.5 - 0.5 * np.cos(2 * np.pi * frac)
    point = tuft[si, ki] * np.clip(1 - np.abs(frac - 0.85) / 0.1, 0, 1)
    rad = half * (0.86 + amp[si, ki] * bump + 0.16 * point)
    inside = best <= rad
    inside &= s_of < 0.995
    q = np.clip(best / np.maximum(rad, 1e-3), 0, 1) * side  # -1 .. 1 across
    # volume: lit on the upper left side, the core a step darker toward the shade side, the
    # outer fur light, the tip grey-white, her shade at the root
    lit_side = -1.0  # q < 0: left of the spine's direction (the up-left side)
    t = 0.72 + 0.12 * np.sqrt(np.clip(1 - q**2, 0, 1)) + 0.16 * lit_side * q
    t -= 0.1 * np.clip((np.abs(q) - 0.85) / 0.15, 0, 1) * (q > 0)  # the shade side's rim
    t += 0.12 * np.clip((s_of - 0.62) / 0.28, 0, 1)
    t -= 0.18 * np.clip(1 - s_of / 0.2, 0, 1)
    cv.t[inside] = np.clip(t[inside], 0, 1)
    cv.mat[inside] = HAIR
    cv.cov[inside] = True
    # fur strokes along the flow: a few short curved lines inside, on the shade side
    ink = np.zeros(X.shape, bool)
    for off, s0, s1 in ((0.5, 0.15, 0.4), (0.3, 0.42, 0.62), (0.62, 0.55, 0.78), (0.1, 0.7, 0.86),
                        (-0.25, 0.3, 0.5)):  # fmt: skip
        ink |= (np.abs(q - off) < 0.05) & (s_of > s0) & (s_of < s1)
    cv.ink[inside & ink] = 1
    img = cv.render()
    from char_unify import unify

    img = unify(img, part=True)
    # all fur: its silhouette too is the hair's own outline grey (as the hair layer's)
    black = (img[..., :3] == _rgb(bc.OUTLINE)).all(2) & (img[..., 3] > 0)
    img[black, :3] = _rgb(bc.RAMPS["hair"][1])
    return img


# --- earbud ---------------------------------------------------------------------------------


def earbud_sprite(parts):
    """The clean earbud of base.png (build_hair.clean_earbud), at its sheet position."""
    from PIL import Image

    base = np.asarray(Image.open(bc.OUT / "base.png").convert("RGBA"))
    mug = [_rgb(c) for c in bc.RAMPS["mug"]]
    m = np.zeros(base.shape[:2], bool)
    for c in mug:
        m |= (base[..., :3] == c).all(2)
    region = bc.poly(parts.size, [(158, 95), (182, 88), (182, 120), (158, 121)])
    m &= region & (base[..., 3] > 0)
    labels, n = ndi.label(m)
    if n:
        sizes = ndi.sum(m, labels, range(1, n + 1))
        m = labels == int(np.argmax(sizes)) + 1
    out = np.zeros_like(base)
    out[m] = base[m]
    return out


# --- the file -------------------------------------------------------------------------------


def _crop(img):
    a = img[..., 3] > 0
    ys, xs = np.where(a)
    y0, y1, x0, x1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
    return img[y0:y1, x0:x1], int(x0), int(y0)


def build(parts, out_dir: Path) -> dict:
    items = []  # (key, img, ox, oy) — ox/oy: the sprite's top left in its frame of reference
    pivots = {k: e["pivot"] for k, e in parts.EARS.items()}
    import char_parts as cp

    for side in ("l", "r"):
        pvx, pvy = pivots[side]
        out_sign = -1 if side == "l" else 1  # outward = away from her face

        def add_ear(name, img, side=side, pvx=pvx, pvy=pvy):
            spr, x0, y0 = _crop(img)
            items.append((f"ears.{side}.{name}", spr, x0 - pvx, y0 - pvy))

        add_ear("std", _ear_sprite(parts, side, 0.0))
        droop = _ear_sprite(parts, side, EAR_DROOP[side], EAR_DROOP_SCALE, 3.0, rigid=True)
        cap = head_cap(parts)
        half = (np.mgrid[0 : cap.shape[0], 0 : cap.shape[1]][1] < 139) == (side == "l")
        cap[~half] = 0
        # one drawing per front sheet: the cap and the crown it meets move with the head's field
        # (a tilt moved the cap with the ear's anchor: a crack across the top of the head)
        for name in parts.sheet_imgs:
            spr, ox, oy = droop_sheet(parts, side, droop, cap, name)
            if name == "base":
                items.append((f"ears.{side}.droop", spr, ox, oy))
                rest = (spr, ox, oy)
            elif not (
                spr.shape == rest[0].shape and (spr == rest[0]).all() and (ox, oy) == rest[1:]
            ):
                items.append((f"ears.{side}.droop.{name}", spr, ox, oy))
        for name, (ang, scale, dy) in EAR_TWITCH.items():
            add_ear(name, _ear_sprite(parts, side, out_sign * ang, scale, dy))
        for ang in cp.EAR_ANGLES:
            add_ear(f"red.{ang}", blush_ear(_ear_sprite(parts, side, float(ang))))
    for name, (ang, lift) in TAIL_FRONT.items():
        spr, x0, y0 = _crop(tail_sprite(parts, TAIL_FRONT_ROOT, TAIL_FRONT_SPINE, ang, lift, 3))
        items.append((f"tail.front.{name}", spr, x0, y0))
    for name, (ang, lift) in TAIL_BACK.items():
        spr, x0, y0 = _crop(tail_sprite(parts, TAIL_BACK_ROOT, TAIL_BACK_SPINE, ang, lift, 5))
        items.append((f"tail.back.{name}", spr, x0, y0))
    spr, x0, y0 = _crop(earbud_sprite(parts))
    ex, ey = EARBUD_ANCHOR_AT
    items.append(("earbud.front", spr, x0 - ex, y0 - ey))
    # pack (shelves, 1 px apart)
    width = 512
    order = sorted(range(len(items)), key=lambda i: -items[i][1].shape[0])
    x = y = row_h = 0
    placed = {}
    for i in order:
        h, w = items[i][1].shape[:2]
        if x + w > width:
            x, y, row_h = 0, y + row_h + 1, 0
        placed[i] = (x, y)
        x += w + 1
        row_h = max(row_h, h)
    atlas = np.zeros((y + row_h, width, 4), np.uint8)
    meta = {
        "version": 1,
        "size": [parts.px.tw, parts.px.th],
        "ears": {"l": {}, "r": {}},
        "tail": {"front": {}, "back": {}},
        "earbud": {},
        # where ox / oy count from: a sheet anchor (parts.json sheets.<name>.anchors) or the
        # sheet's top left (null)
        "anchor": {"ears": {"l": "ear_l", "r": "ear_r"}, "tail": None, "earbud": "eye_r"},
        "tailRoot": {"front": list(TAIL_FRONT_ROOT), "back": list(TAIL_BACK_ROOT)},
        "earAngles": cp.EAR_ANGLES,
    }
    for i, (key, spr, ox, oy) in enumerate(items):
        ax, ay = placed[i]
        atlas[ay : ay + spr.shape[0], ax : ax + spr.shape[1]] = spr
        group, a, *rest = key.split(".")
        name = ".".join(rest)
        entry = {"ox": int(ox), "oy": int(oy), "w": int(spr.shape[1]), "h": int(spr.shape[0]),
                 "ax": int(ax), "ay": int(ay)}  # fmt: skip
        if group == "earbud":
            meta["earbud"][a] = entry
        else:
            meta[group][a][name] = entry
    cp.save_indexed(atlas, out_dir / "extras.png")
    (out_dir / "extras.json").write_text(json.dumps(meta, indent=1) + "\n", encoding="utf-8")
    return {"extras": [width, int(atlas.shape[0])], "sprites": len(items)}
