"""Back view of the Stage character (#341, v2): drawn in code, for the motions that turn her
around (typing at the laptop on the room desk behind her). Called by char_parts.py (offline).

There is no back view in the owner's icon, so this one is *constructed*, with the icon's ramps,
ordered dither, line art and outline (``Pixelizer.shade``):

- **Hair** in locks, not a curtain: a dark under-mass (the hair in shadow between the locks),
  then about twenty locks of different width and length, each a tapered ribbon on a spline
  from the crown over the skull and down the back, shaded as a cylinder (light from the upper
  left), with a broken "angel ring" highlight across the skull, the shadow under the skull at
  the nape, darker tips, a few strand lines and split tips. Below the nape the locks are
  handed to the hair layer as its back set (``hair_back.json``, ``export_hair_set``), so the
  tips sway with the hair rig; the skull, the under-mass and the ears stay in the sheet.
- **Lab coat**: collar, shoulder and armhole seams, the centre seam and the vent, the half belt
  with two buttons, tension folds from the right shoulder blade toward the arm that reaches
  forward, soft folds on the left, the shade the hair casts.
- **Right arm** (``back_arm``): shoulder, elbow, forearm, the turtleneck cuff and the hand on
  the room laptop's keyboard (gadget ``monitor``), seen beside the shoulder; typing frames move
  the wrist, the fingers and the elbow by a pixel.
- ``turn`` > 0: 3/4 back (the far side compressed, a sliver of cheek).

Part ids: the sheets ``back*`` / ``turn*`` are the whole figure; the back hair of the sheet is
the part ``hair_back`` (the skull, or all of it while turning), the coat ``coat``, the cheek
``face``.
"""

from __future__ import annotations

import json

import build_char as bc
import numpy as np
from char_arms import COAT, MUG, NAVY, SKIN, Canvas
from scipy import ndimage as ndi

HAIR = 0
EYE = 5
CX = 139.0  # mirror axis (portrait px)
NECK_Y = 84.0  # head centre row of the back view (the skull's widest point is near it)
CUT = 26.0  # px below the head centre where the hanging locks begin (hair layer)
# where the right hand rests: on the keys of the room laptop (gadget `monitor`, gadgets.json)
# seen from the character sheet; tests/stage_back_view_test.cjs checks it against the gadget
KEYS_AT = (266.0, 180.0)


class HCanvas(Canvas):
    """Canvas that also remembers which lock painted each hi-res pixel, and renders all six
    materials (hair, skin, coat, navy, mug, eye)."""

    def __init__(self, px) -> None:
        super().__init__(px)
        self.owner = np.full(self.cov.shape, -1, np.int16)
        self.cur = -1

    def paint(self, sl, inside, t, mat, edge, ink_w=1.7):
        super().paint(sl, inside, t, mat, edge, ink_w)
        self.owner[sl][inside] = self.cur

    def render(self, no_outline=None):
        import cv2

        px = self.px
        cov = self.cov.astype(np.float32)
        tt = np.nan_to_num(self.t, nan=0.0) * cov
        size = px.size
        cov_s = cv2.resize(cov, size, interpolation=cv2.INTER_AREA)
        t_s = cv2.resize(tt, size, interpolation=cv2.INTER_AREA) / np.maximum(cov_s, 1e-3)
        ink_s = cv2.resize(self.ink, size, interpolation=cv2.INTER_AREA)
        sil = cov_s > 0.5
        mats = np.zeros((px.th, px.tw), np.uint8)
        best = np.zeros((px.th, px.tw), np.float32)
        for k in (HAIR, SKIN, COAT, NAVY, MUG, EYE):
            f = cv2.resize(
                (self.cov & (self.mat == k)).astype(np.float32), size, interpolation=cv2.INTER_AREA
            )
            better = f > best
            mats[better] = k
            best[better] = f[better]
        tmap = np.where(sil, np.clip(t_s, 0, 1), np.nan).astype(np.float32)
        col = np.zeros((px.th, px.tw, 3), np.float32)
        self.mats_lo = mats
        return px.shade(col, mats, sil, ink_s, no_outline=no_outline, tmap=tmap)

    def owner_lo(self, ids):
        """Low-res owner (lock index) by area majority; -1 = none."""
        import cv2

        size = self.px.size
        out = np.full((self.px.th, self.px.tw), -1, np.int16)
        best = np.zeros((self.px.th, self.px.tw), np.float32)
        for i in ids:
            f = cv2.resize((self.owner == i).astype(np.float32), size, interpolation=cv2.INTER_AREA)
            better = f > np.maximum(best, 0.2)
            out[better] = i
            best[better] = f[better]
        return out


def _mirror_x(pts):
    return [(2 * CX - x, y) for x, y in pts]


def spline(pts, k=8):
    """Catmull-Rom through the points (portrait px)."""
    pts = [np.array(p, np.float32) for p in pts]
    ext = [2 * pts[0] - pts[1]] + pts + [2 * pts[-1] - pts[-2]]
    out = []
    for j in range(1, len(ext) - 2):
        p0, p1, p2, p3 = ext[j - 1], ext[j], ext[j + 1], ext[j + 2]
        for t in np.linspace(0, 1, k, endpoint=False):
            t2, t3 = t * t, t * t * t
            out.append(
                0.5
                * (
                    (2 * p1)
                    + (-p0 + p2) * t
                    + (2 * p0 - 5 * p1 + 4 * p2 - p3) * t2
                    + (-p0 + 3 * p1 - 3 * p2 + p3) * t3
                )
            )
    out.append(pts[-1])
    return out


# --- hair ------------------------------------------------------------------------------------

# Locks (painter's order, back first): v = position across the back (-1 left .. 1 right of the
# picture), w = width at the widest (px), tip = length (row of the tip at rest), split = the
# tip forks, curl = the tip turns out (+) or in (-) by px, chain = the hair-layer chain the
# hanging part belongs to. Hand-authored, seeded wobble only.
LOCKS = [
    # the under layer: broad locks
    {"v": -1.00, "w": 20, "tip": 224, "curl": -3, "chain": "locks_l"},
    {"v": 1.00, "w": 20, "tip": 228, "curl": 3, "chain": "locks_r"},
    {"v": -0.70, "w": 28, "tip": 248, "split": True, "chain": "locks_l"},
    {"v": 0.72, "w": 28, "tip": 244, "chain": "locks_r"},
    {"v": -0.34, "w": 30, "tip": 264, "chain": "locks_cl"},
    {"v": 0.36, "w": 30, "tip": 260, "split": True, "chain": "locks_cr"},
    {"v": 0.0, "w": 30, "tip": 274, "chain": "locks_c"},
    # the top layer: narrower locks over the gaps
    {"v": -0.86, "w": 16, "tip": 238, "curl": -2, "chain": "locks_l", "top": True},
    {"v": -0.52, "w": 18, "tip": 256, "chain": "locks_cl", "top": True},
    {"v": -0.16, "w": 18, "tip": 270, "split": True, "chain": "locks_c", "top": True},
    {"v": 0.2, "w": 18, "tip": 266, "chain": "locks_c", "top": True},
    {"v": 0.54, "w": 18, "tip": 252, "split": True, "chain": "locks_cr", "top": True},
    {"v": 0.88, "w": 16, "tip": 234, "curl": 2, "chain": "locks_r", "top": True},
    # thin strands on top
    {"v": -0.42, "w": 7, "tip": 246, "chain": "locks_cl", "thin": True},
    {"v": 0.08, "w": 7, "tip": 256, "chain": "locks_c", "thin": True},
    {"v": 0.68, "w": 7, "tip": 238, "chain": "locks_cr", "thin": True},
]
CHAINS = {  # chain -> (part, side)
    "locks_l": ("locks_side", -1),
    "locks_cl": ("locks", -1),
    "locks_c": ("locks", 0),
    "locks_cr": ("locks", 1),
    "locks_r": ("locks_side", 1),
}
CUTS = [150.0, 200.0, 238.0]  # rows between the segments of a chain (root, tips)


def lock_paths(hcx, hcy, rx, turn, hy, seed=11):
    rng = np.random.default_rng(seed)
    crown = np.array([hcx + 3 + 6 * turn, hcy - 50])
    out = []
    for i, L in enumerate(LOCKS):
        v = L["v"]
        if turn > 0 and v > 0:
            # the near side of the head turns toward us: its hair is swept back, the face shows
            if v > 0.84:
                continue
            v *= 1 - 0.35 * turn
        wob = rng.uniform(-3.5, 3.5)
        wob2 = rng.uniform(-2.5, 2.5)
        tip = L["tip"] + hy + rng.uniform(-3, 3)
        side = 1 if v >= 0 else -1
        av = abs(v)
        # over the skull, hugging the neck a little at the nape, out over the shoulder blades
        pts = [
            (hcx + v * rx * 0.42, hcy - 30 + 8 * av),
            (hcx + v * rx * 0.8, hcy - 10 + 4 * av),
            (hcx + v * rx * 0.92, hcy + 6),
            (hcx + v * rx * 0.94 + wob * 0.3, hcy + 26),
            (hcx + v * (rx * 0.9 + 2) + wob, hcy + 64),
            (hcx + v * (rx * 0.92 + 3) + wob2, (hcy + 64 + tip) / 2 + 8),
            (hcx + v * (rx * 0.9) + side * L.get("curl", 0) + wob2 * 1.2, tip),
        ]
        w = L["w"] * rng.uniform(0.92, 1.08)
        thin = L.get("thin", False)
        widths = (
            [w * 0.7, w * 0.85, w * 0.9, w * 0.92, w, w * 0.8, 1.0]
            if not thin
            else [1.5, w * 0.6, w, w, w * 0.9, w * 0.7, 0.8]
        )
        out.append({"i": i, "pts": pts, "widths": widths, "split": L.get("split", False), "chain": L["chain"], "v": v,
                    "base": 0.6 + 0.05 * (1 - av) + (0.05 if L.get("top") else 0) + rng.uniform(-0.03, 0.03),
                    "thin": thin, "top": L.get("top", False)})  # fmt: skip
    return out, crown


def draw_hair(cv, X, Y, hcx, hcy, rx, turn, hy):
    """Paint the under-mass and the locks; returns (hair mask, locks)."""
    locks, crown = lock_paths(hcx, hcy, rx, turn, hy)
    # under-mass: the hair between the locks below the skull, dark (shadowed hair)
    yy = Y - hy
    half = np.interp(yy, [84, 110, 130, 160, 220], [56, 54, 52, 56, 54]) * (1 - 0.1 * turn)
    half = np.where(X > hcx, half * (1 - 0.3 * turn), half)
    u = (X - hcx) / half
    bottom = 226 + hy - 14 * u * u
    rxx = np.where(X > hcx, rx * (1 - 0.22 * turn), rx)
    skull = ((X - hcx) / rxx) ** 2 + ((Y - hcy) / 54.0) ** 2 <= 1.0
    under = (np.abs(u) <= 1) & (Y >= hcy) & (Y <= bottom) & ~skull
    cv.cur = -1
    tt = (0.2 - 0.06 * np.abs(u)).astype(np.float32)
    cv.t[under] = tt[under]
    cv.mat[under] = HAIR
    cv.cov[under] = True
    cv.ink[under] = 0
    cv.owner[under] = -1
    # the skull cap: smooth hair over the back of the head, lit from the upper left, with a few
    # flow lines from the whorl
    r2 = ((X - hcx) / rxx) ** 2 + ((Y - hcy) / 54.0) ** 2
    nx, ny = (X - hcx) / rxx, (Y - hcy) / 54.0
    cap_t = (
        0.56
        + 0.16 * np.sqrt(np.clip(1 - r2, 0, 1))
        - 0.12 * (nx * 0.55 + ny * 0.83)
        - 0.1 * np.clip((Y - hcy) / 30.0, 0, 1)
    )
    cv.t[skull] = cap_t[skull]
    cv.mat[skull] = HAIR
    cv.cov[skull] = True
    cv.ink[skull] = 0
    cv.owner[skull] = -1
    for k, ang in enumerate((-150, -128, -108, -90, -72, -52, -30)):
        a0 = np.radians(ang)
        d = np.array([np.cos(a0) * rx, np.sin(a0) * 54.0 * -1])
        p0 = crown + d * 0.18
        p1 = np.array([hcx, hcy]) + np.array(
            [np.cos(a0) * rx * 0.96, -np.sin(a0) * 54.0 * 0.2 + 20 + 6 * abs(np.cos(a0))]
        )
        for j in range(8):
            q0 = p0 + (p1 - p0) * (j / 8)
            q1 = p0 + (p1 - p0) * ((j + 1) / 8)
            bend = np.array([np.cos(a0) * 4 * np.sin(np.pi * j / 8), 0])
            if 2 <= j <= 6 and (k + j) % 3:
                cv.line(tuple(q0 + bend), tuple(q1 + bend), 0.95)
    hair = under | skull
    for L in locks:
        path = spline(L["pts"])
        ws = np.interp(
            np.linspace(0, len(L["widths"]) - 1, len(path)), range(len(L["widths"])), L["widths"]
        )
        cv.cur = L["i"]
        cv.begin()
        n = len(path)
        fork = int(n * 0.8) if L["split"] else n
        for j, (a, b, r0, r1) in enumerate(zip(path[:-1], path[1:], ws[:-1], ws[1:])):
            if j >= fork:
                break
            cv.capsule(tuple(a), tuple(b), r0 / 2, r1 / 2, HAIR, base=L["base"], amp=0.24)
        if L["split"]:
            # two tips of different length from the fork
            p = np.array(path[fork], np.float32)
            d = np.array(path[-1], np.float32) - p
            nrm = np.array([-d[1], d[0]]) / (np.hypot(*d) or 1)
            r = ws[fork] / 2
            for k, (off, ln) in enumerate(((-0.5, 1.0), (0.55, 0.72))):
                q0 = p + nrm * r * off
                q1 = p + d * ln + nrm * r * off * 1.6
                cv.capsule(
                    tuple(q0),
                    tuple(q0 + (q1 - q0) * 0.5),
                    r * 0.55,
                    r * 0.4,
                    HAIR,
                    base=L["base"] - 0.02 * k,
                    amp=0.4,
                )
                cv.capsule(
                    tuple(q0 + (q1 - q0) * 0.5),
                    tuple(q1),
                    r * 0.4,
                    0.4,
                    HAIR,
                    base=L["base"] - 0.02 * k,
                    amp=0.4,
                )
        g = cv.group.copy()
        cv.group = None
        if L["top"] or L["thin"]:
            # the edge of a lock that lies on another: a dark line along its side, not around it
            side = -1 if L["v"] >= 0 else 1
            for j in range(int(n * 0.28), int(n * 0.92)):
                off = ws[j] / 2 * side
                cv.line((path[j][0] + off, path[j][1]), (path[j + 1][0] + off, path[j + 1][1]), 0.9)
        # a strand line along the lock (not on the thin strands)
        if not L["thin"] and not L["top"]:
            a = int(n * 0.3)
            b = int(n * 0.78)
            off = ws[a] * 0.18 * (1 if L["v"] < 0 else -1)
            for j in range(a, b - 1):
                p0 = np.array(path[j]) + (off, 0)
                p1 = np.array(path[j + 1]) + (off, 0)
                cv.line(tuple(p0), tuple(p1), 0.9)
        hair |= g
    cv.cur = -1
    # light: the broken angel ring across the skull, the shade under the skull (the nape),
    # darker toward the tips
    ring_y = hcy - 18 + 2.6 * np.sin((X - hcx) * 0.42) + 0.0
    att = np.clip(1 - np.abs(X - hcx) / (rx * 0.95), 0, 1)
    ring = np.exp(-(((Y - ring_y) / 3.2) ** 2)) * att
    nape = np.clip((Y - (hcy + 10)) / 18.0, 0, 1) * np.clip(1 - (Y - (hcy + 40)) / 30.0, 0, 1)
    tips = np.clip((Y - 214 - hy) / 50.0, 0, 1)
    # a second, softer sheen where the hair bends over the shoulder blades
    blade = np.exp(-(((Y - (hcy + 84)) / 9.0) ** 2)) * np.clip(
        1 - np.abs(X - hcx) / (rx * 1.1), 0, 1
    )
    d_t = 0.3 * ring - 0.16 * nape - 0.18 * tips + 0.06 * blade
    lock_px = hair & ~under | (hair & (cv.owner >= 0))
    t = np.clip(cv.t[lock_px] + d_t[lock_px], 0, 1)
    # cel-like bands: tones gather on the ramp steps, the dither stays at the transitions
    q = np.round(t * 5) / 5
    cv.t[lock_px] = 0.4 * t + 0.6 * q
    return hair, locks, crown


# --- coat ------------------------------------------------------------------------------------

# half widths of the coat from behind (row, px from the axis); the right side (picture) is the
# torso only: the right arm is its own sprite (back_arm), reaching to the keyboard
LEFT = [(22, 126), (52, 138), (78, 150), (94, 164), (102, 186), (108, 230), (112, 300)]
RIGHT = [(22, 126), (52, 138), (78, 150), (92, 162), (95, 180), (91, 204), (93, 240), (96, 300)]


def coat_shape(W, H, s, shoulder_l, shoulder_r):
    from PIL import Image as _Im
    from PIL import ImageDraw as _Dr

    pts = [(CX + w, y - shoulder_r * np.clip((262 - y) / 90.0, 0, 1)) for w, y in RIGHT]
    pts += [(CX - w, y - shoulder_l * np.clip((262 - y) / 90.0, 0, 1)) for w, y in reversed(LEFT)]
    im = _Im.new("L", (W, H), 0)
    _Dr.Draw(im).polygon([(x * s, y * s) for x, y in pts], fill=1)
    return np.asarray(im).astype(bool)


def coat_details(cv, Xb, Yb, coat, hair, s):
    """Lightness and ink of the coat's back (in the turned coordinates Xb, Yb)."""
    t = 0.84 + 0.1 * np.exp(-(((Yb - 160) / 16.0) ** 2)) - 0.18 * np.clip((Yb - 190) / 80.0, 0, 1)
    t -= 0.16 * np.clip((np.abs(Xb - CX) - 72) / 40.0, 0, 1)
    t -= 0.05 * np.clip((Xb - CX) / 90.0, 0, 1)  # light from the left
    # shoulder blades: a soft roundness either side of the spine
    for sx in (-1, 1):
        t += 0.05 * np.exp(-(((Xb - (CX + sx * 40)) / 18.0) ** 2 + ((Yb - 186) / 16.0) ** 2))
    # the shade the hair casts on the coat (just below / beside it)
    near_hair = ndi.binary_dilation(hair, iterations=int(5 * s)) & ~hair
    t = np.where(near_hair, t - 0.12, t)

    def band(x0, y0, x1, y1, width=0.45):
        d = np.array([x1 - x0, y1 - y0], np.float32)
        L2 = float(d @ d) or 1e-6
        uu = np.clip(((Xb - x0) * d[0] + (Yb - y0) * d[1]) / L2, 0, 1)
        dist = np.hypot(Xb - (x0 + uu * d[0]), Yb - (y0 + uu * d[1]))
        return dist < width

    ink = np.zeros(coat.shape, bool)
    # collar: the turned-down collar around the neck (its lower edge and a highlight above it)
    collar = coat & (np.abs(Xb - CX) < 40) & (Yb < 140 + 6 * ((Xb - CX) / 40.0) ** 2) & (Yb > 118)
    t = np.where(collar, t + 0.06, t)
    ink |= coat & (np.abs(Yb - (140 + 6 * ((Xb - CX) / 40.0) ** 2)) < 0.5) & (np.abs(Xb - CX) < 40)
    # shoulder seams (collar -> shoulder point) and armhole seams
    for sx in (-1, 1):
        ink |= coat & band(CX + sx * 38, 138, CX + sx * 90, 160)
        ink |= coat & band(CX + sx * 90, 160, CX + sx * 95, 182) | coat & band(
            CX + sx * 95, 182, CX + sx * 90, 204
        )
    # side-back seams from the armholes down (beside the hair)
    for sx in (-1, 1):
        for x0, y0, x1, y1 in (
            (CX + sx * 90, 204, CX + sx * 74, 240),
            (CX + sx * 74, 240, CX + sx * 68, 300),
        ):
            ink |= coat & band(x0, y0, x1, y1, 0.42)
        t = np.where(coat & band(CX + sx * 92, 206, CX + sx * 72, 300, 1.6), t - 0.05, t)
    # centre seam, the vent below the half belt, the half belt with two buttons
    ink |= coat & (np.abs(Xb - CX) < 0.45) & (Yb > 204) & (Yb < 250)
    ink |= coat & band(CX, 258, CX - 1.5, 300)
    t = np.where(
        coat & (Xb > CX - 1) & (Xb < CX + 5) & (Yb > 258), t - 0.12, t
    )  # under the vent's overlap
    belt = coat & (np.abs(Xb - CX) < 34) & (Yb > 250) & (Yb < 257)
    t = np.where(belt, t + 0.04 - 0.08 * ((Yb - 250) / 7.0), t)
    t = np.where(
        coat & (np.abs(Xb - CX) < 34) & (Yb >= 257) & (Yb < 259.5), t - 0.1, t
    )  # its shadow
    ink |= coat & (np.abs(Xb - CX) < 34) & ((np.abs(Yb - 250) < 0.45) | (np.abs(Yb - 257) < 0.45))
    ink |= coat & (np.abs(np.abs(Xb - CX) - 34) < 0.45) & (Yb > 250) & (Yb < 257)
    buttons = coat & (
        (np.hypot(Xb - (CX - 27), Yb - 253.5) < 1.6) | (np.hypot(Xb - (CX + 27), Yb - 253.5) < 1.6)
    )
    t = np.where(buttons, 0.35, t)
    # tension folds from the right shoulder blade toward the arm that reaches forward: a shadow
    # under a lit ridge (no line), the deepest part of each fold a short line
    for x0, y0, x1, y1 in (
        (CX + 30, 172, CX + 88, 184),
        (CX + 22, 190, CX + 86, 198),
        (CX + 34, 208, CX + 88, 210),
    ):
        t = np.where(coat & band(x0, y0 - 1.6, x1, y1 - 1.6, 1.3), t + 0.05, t)
        t = np.where(coat & band(x0, y0 + 1.0, x1, y1 + 1.0, 1.4), t - 0.1, t)
        mx, my = x0 + (x1 - x0) * 0.62, y0 + (y1 - y0) * 0.62
        ink |= coat & band(mx, my + 0.6, x1 - 4, y1 - (y1 - y0) * 0.05 + 0.6, 0.42)
    # soft folds on the left (the arm hangs) and at the waist: shadows only
    for x0, y0, x1, y1 in (
        (CX - 44, 176, CX - 52, 214),
        (CX - 86, 186, CX - 72, 214),
        (CX - 70, 222, CX - 40, 232),
        (CX + 40, 228, CX + 72, 222),
    ):
        t = np.where(coat & band(x0, y0, x1, y1, 1.3), t - 0.08, t)
        t = np.where(coat & band(x0 - 1.5, y0, x1 - 1.5, y1, 1.0), t + 0.04, t)
    ink |= coat & band(CX - 84, 192, CX - 76, 208, 0.42)
    return t, ink


# --- the right arm, seen from behind -----------------------------------------------------------

TYPE_FRAMES = {  # wrist offset, elbow offset, which finger presses
    "back_type1": ((0.0, 0.0), (0.0, 0.0), 0),
    "back_type2": ((1.0, -1.0), (0.0, -1.0), 1),
    "back_type3": ((0.0, 1.0), (1.0, 0.0), 2),
    "back_type4": ((1.0, 0.0), (0.0, 0.0), 1),
}


def draw_back_arm(cv, wrist_off=(0.0, 0.0), elbow_off=(0.0, 0.0), press=0, xform=None):
    """Right arm reaching to the keyboard beside the shoulder: upper arm and forearm in the
    coat's sleeve, the navy cuff, the back of the hand, three fingertips on the keys."""
    T = xform or (lambda p: p)
    sh = np.array([226.0, 168.0])
    el = np.array([247.0, 203.0]) + elbow_off
    wr = np.array([258.0, 186.0]) + wrist_off
    kn = wr + (6.0, -3.0)
    P = lambda p: tuple(T(np.asarray(p, np.float32)))
    cv.cur = -2
    cv.begin()
    cv.capsule(P(sh), P(el), 12.0, 9.5, COAT, base=0.74, amp=0.4)
    cv.capsule(P(el), P(wr - (wr - el) * 0.12), 9.5, 7.2, COAT, base=0.7, amp=0.4)
    cv.end(ink_w=1.6)
    # elbow crease and the sleeve's folds
    cv.line(P(el + (-6, -3)), P(el + (1, -6)), 1.2)
    cv.line(P(sh + (6, 14)), P(sh + (14, 18)), 1.1)
    cv.begin()
    cv.capsule(P(wr - (wr - el) * 0.16), P(wr + (1.0, -0.6)), 5.6, 5.0, NAVY, base=0.42, amp=0.25)
    cv.end(ink_w=1.3)
    cv.begin()
    cv.capsule(P(wr + (0.5, -0.5)), P(kn), 4.6, 4.2, SKIN, base=0.66, amp=0.3)
    for k, (dx, dy) in enumerate(((-1.0, 1.0), (1.2, 0.6), (3.0, -0.6))):
        down = 1.6 if k == press else 0.4
        a = kn + (dx, dy)
        cv.capsule(P(a), P(a + (1.6, 3.0 + down)), 1.7, 1.5, SKIN, base=0.6 - 0.04 * k, amp=0.3)
    cv.end(ink_w=1.3)
    cv.cur = -1


def back_arm(parts, name, shoulder_r=0.0):
    """The typing arm as its own sprite; it joins the coat at the shoulder without a line."""
    wo, eo, press = TYPE_FRAMES[name]
    px = parts.px
    cv = HCanvas(px)
    draw_back_arm(cv, wo, eo, press)
    s = bc.F
    coat_hi = coat_shape(px.w0, px.h0, s, 0.0, shoulder_r)
    cv.ink[coat_hi & (cv.Y / s < 186)] = 0
    return cv.render(no_outline=cv_low(px, coat_hi))


# --- the whole back view ------------------------------------------------------------------------


def back_view(
    parts, shoulder_l=0.0, shoulder_r=0.0, head_dx=0.0, head_dy=0.0, turn=0.0, seed=7, split=False
):
    """shoulder_l / _r: px the left / right shoulder is raised (picture sides); head_dx/dy: head
    shift; turn: 0 = straight back, >0 = turning toward the viewer's right (3/4 back).
    split: the hanging locks go to the hair layer (returned separately), not in the sheet.
    Returns (rgba, layers, info)."""
    px = parts.px
    s = bc.F
    cv = HCanvas(px)
    H, W = px.h0, px.w0
    Y, X = cv.Y / s, cv.X / s
    squeeze = 1.0 - 0.3 * turn
    Xb = CX + (X - CX) / squeeze - 10 * turn
    lift_l = shoulder_l * np.clip((CX - X) / 90.0, 0, 1)
    lift_r = shoulder_r * np.clip((X - CX) / 90.0, 0, 1)
    Yb = Y + (lift_l + lift_r) * np.clip((262 - Y) / 90.0, 0, 1)
    xi = np.clip(np.round(Xb * s), 0, W - 1).astype(int)
    yi = np.clip(np.round(Y * s), 0, H - 1).astype(int)
    coat = coat_shape(W, H, s, shoulder_l, shoulder_r)[yi, xi]
    hy = head_dy - (shoulder_l + shoulder_r) / 2
    hx = head_dx - 8 * turn
    hcx, hcy = CX + hx, NECK_Y + hy
    rx = 56.0 * (1 - 0.14 * turn)
    # coat first (painter's order: coat, arm, hair, ears)
    t_coat, ink_coat = coat_details(cv, Xb, Yb, coat, np.zeros_like(coat), s)
    cv.cur = -1
    cv.t[coat] = t_coat[coat]
    cv.mat[coat] = COAT
    cv.cov[coat] = True
    cv.ink[coat] = 0
    cv.ink[coat & ink_coat] = 1
    # a turtleneck sliver at the nape above the collar
    nape = (np.abs(X - hcx) < 20) & (Y > hcy + 30) & (Y < hcy + 40) & ~coat
    cv.t[nape] = 0.4
    cv.mat[nape] = NAVY
    cv.cov[nape] = True
    if turn > 0:
        # the right arm, seen as the body turns (the same arm, compressed toward the axis)
        def xf(p):
            return np.array([CX + (p[0] + 10 * turn - CX) * squeeze, p[1]], np.float32)

        draw_back_arm(cv, xform=xf)
    face_side = np.zeros((H, W), bool)
    if turn > 0:
        # the side of the face coming into view past the hair (cheek, lashes, nose, lips, chin)
        E = hcx + rx * (1 - 0.22 * turn) * 0.92
        t_ = turn
        pts = [(E - 8, hcy - 4), (E + 2 + 6 * t_, hcy + 4), (E + 3 + 7 * t_, hcy + 12), (E + 6 + 10 * t_, hcy + 22),
               (E + 3 + 8 * t_, hcy + 26), (E + 4 + 8 * t_, hcy + 30), (E + 2 + 7 * t_, hcy + 34), (E + 1 + 6 * t_, hcy + 41),
               (E - 4, hcy + 46), (E - 10, hcy + 40)]  # fmt: skip
        cv.cur = -1
        # the neck and the turtleneck collar under the jaw
        cv.begin()
        cv.polygon(
            [(E - 16, hcy + 36), (E - 2, hcy + 40), (E + 2, hcy + 50), (E - 16, hcy + 52)],
            SKIN,
            base=0.5,
            amp=0.2,
        )
        cv.polygon(
            [(E - 22, hcy + 46), (E + 3, hcy + 47), (E + 6, hcy + 62), (E - 24, hcy + 64)],
            NAVY,
            base=0.42,
            amp=0.25,
        )
        cv.end(ink_w=1.3)
        cv.begin()
        cv.polygon(pts, SKIN, base=0.64, amp=0.2, axis=(1.0, 0.2))
        face_side = cv.group.copy()
        cv.end(ink_w=1.3)
        if turn > 0.6:
            cv.line((E + 0 + 5 * t_, hcy + 11), (E + 3 + 7 * t_, hcy + 12.5), 2.0)  # lashes
    hair, locks, _crown = draw_hair(cv, X, Y, hcx, hcy, rx, turn, hy)
    # the shade the hair casts on the coat, now that the hair is known
    near = (
        ndi.binary_dilation(hair, iterations=int(4 * s))
        & ~hair
        & coat
        & (cv.owner == -1)
        & (cv.mat == COAT)
    )
    cv.t[near] -= 0.1
    # --- ears (mirrored outlines, the back of the ear)
    ears = np.zeros((H, W), bool)
    ear_t = np.full((H, W), np.nan, np.float32)
    ear_line = np.zeros((H, W), bool)
    from PIL import Image, ImageDraw

    for side, e in parts.EARS.items():
        pts = [(x + hx, y + hy) for x, y in _mirror_x(e["poly"])]
        im = Image.new("L", (W, H), 0)
        ImageDraw.Draw(im).polygon([(x * s, y * s) for x, y in pts], fill=1)
        m = np.asarray(im).astype(bool)
        pv = np.array(_mirror_x([e["pivot"]])[0]) + (hx, hy)
        tip = np.array(min(_mirror_x(e["poly"]), key=lambda p: p[1])) + (hx, hy)
        axis = (tip - pv) / (np.hypot(*(tip - pv)) or 1)
        nrm = np.array([-axis[1], axis[0]])
        across = (X - pv[0]) * nrm[0] + (Y - pv[1]) * nrm[1]
        along = (X - pv[0]) * axis[0] + (Y - pv[1]) * axis[1]
        q = np.clip(across / 14.0, -1, 1)
        t = 0.6 + 0.24 * np.sqrt(np.clip(1 - q**2, 0, 1)) - 0.12 * q * (1 if side == "l" else -1)
        t -= 0.12 * np.clip(along / 40.0, 0, 1)
        ear_t[m] = t[m]
        ears |= m
        ear_line |= m & (np.abs(across + 1.5) < 0.5) & (along > 8)
    ears &= ~hair
    cv.t[ears] = ear_t[ears]
    cv.mat[ears] = HAIR
    cv.cov[ears] = True
    cv.ink[ears] = 0
    cv.ink[ear_line & ears] = 1
    cv.owner[ears] = -1
    cheek = face_side & ~hair & ~ears
    cv.cov[ears] = False
    cv.t[ears] = np.nan
    out = cv.render()
    ear_px = back_ears(parts, hx, hy)
    under_ears = (out[..., 3] == 0) & (ear_px[..., 3] > 0)
    out[under_ears] = ear_px[under_ears]
    opaque = out[..., 3] > 0
    # ownership at low res: lock index, -2 = arm, -1 = other
    own = cv.owner_lo([L["i"] for L in locks] + [-2])
    hair_lo = cv_low(px, hair | ears) | under_ears
    face = cv_low(px, cheek) & opaque & ~hair_lo if turn > 0 else np.zeros(opaque.shape, bool)
    from char_unify import unify  # grey hair lines, no specks (#341, art unify)

    out = unify(out)
    opaque = out[..., 3] > 0
    info = {"locks": locks, "owner": own, "hcy": hcy, "hx": hx, "hy": hy, "rgba": out.copy()}
    hanging = np.zeros(opaque.shape, bool)
    if split:
        rows = px.yy
        hanging = opaque & (own >= 0) & (rows >= hcy + CUT)
        info["hanging"] = hanging
    hm = hair_lo & opaque & ~hanging
    layers = {k: np.zeros_like(out) for k in ("hair_back", "coat", "face")}
    layers["hair_back"][hm] = out[hm]
    layers["face"][face] = out[face]
    rest = opaque & ~hm & ~face & ~hanging
    if split:
        # under the hanging locks: the coat drawn without them (no hole when they swing)
        bare = coat_only(parts, shoulder_l, shoulder_r, turn)
        fill = hanging & (bare[..., 3] > 0)
        out_rest = out.copy()
        out_rest[fill] = bare[fill]
        # elsewhere the dark of the hair in shadow, except at the outer edge (no dark rim when a
        # lock swings inward)
        dark = hanging & ~fill & ndi.binary_erosion(opaque, iterations=2)
        out_rest[dark, :3] = (bc.hexrgb(bc.RAMPS["hair"][0]) * 255).astype(np.uint8)
        out_rest[dark, 3] = 255
        rest = rest | fill
        layers["coat"][rest] = out_rest[rest]
        layers["hair_back"][dark] = out_rest[dark]
    else:
        layers["coat"][rest] = out[rest]
    return out, layers, info


def coat_only(parts, shoulder_l, shoulder_r, turn):
    """The coat alone (what the hanging locks cover), with the hair's cast shade."""
    px = parts.px
    s = bc.F
    cv = HCanvas(px)
    H, W = px.h0, px.w0
    Y, X = cv.Y / s, cv.X / s
    squeeze = 1.0 - 0.22 * turn
    Xb = CX + (X - CX) / squeeze - 10 * turn
    lift_l = shoulder_l * np.clip((CX - X) / 90.0, 0, 1)
    lift_r = shoulder_r * np.clip((X - CX) / 90.0, 0, 1)
    Yb = Y + (lift_l + lift_r) * np.clip((262 - Y) / 90.0, 0, 1)
    xi = np.clip(np.round(Xb * s), 0, W - 1).astype(int)
    yi = np.clip(np.round(Y * s), 0, H - 1).astype(int)
    coat = coat_shape(W, H, s, shoulder_l, shoulder_r)[yi, xi]
    t_coat, ink_coat = coat_details(cv, Xb, Yb, coat, np.zeros_like(coat), s)
    t_coat -= 0.1 * (np.abs(X - CX) < 58)  # in the hair's shade
    cv.t[coat] = t_coat[coat]
    cv.mat[coat] = COAT
    cv.cov[coat] = True
    cv.ink[coat & ink_coat] = 1
    return cv.render()


def export_hair_set(parts, out_dir, info, stem="hair_back"):
    """The hanging locks of the straight back sheet as the hair layer's back set: one chain per
    lock group (CHAINS), segments cut at CUTS, the hair.json schema (hair_layer.js)."""
    import build_hair as bh
    from PIL import Image

    rgba = info["rgba"]
    own = info["owner"]
    hanging = info["hanging"]
    h, w = own.shape
    rows = parts.px.yy
    alpha = rgba[..., 3] > 0
    rim = bh.rim_mask(alpha)
    chain_of = {L["i"]: L["chain"] for L in info["locks"]}
    stay = alpha & ~hanging  # the sheet (skull, under-mass): parents of the root segments
    segs = []
    for chain, (part, side) in CHAINS.items():
        mine = hanging & np.isin(own, [i for i, c in chain_of.items() if c == chain])
        bounds = [-np.inf, *CUTS, np.inf]
        prev = None
        for d in range(len(bounds) - 1):
            m = mine & (rows >= bounds[d]) & (rows < bounds[d + 1])
            if not m.any():
                continue
            segs.append(
                {
                    "id": f"{chain}.{len(segs) and d}",
                    "chain": chain,
                    "part": part,
                    "side": side,
                    "depth": d,
                    "own": m,
                    "parent_mask": prev if prev is not None else stay,
                    "parent": None,
                }
            )
            prev = m
    # ids and parents: depth order inside a chain
    by_chain = {}
    for sg in segs:
        by_chain.setdefault(sg["chain"], []).append(sg)
    for chain, lst in by_chain.items():
        for d, sg in enumerate(lst):
            sg["depth"] = d
            sg["id"] = f"{chain}.{d}"
            sg["parent"] = f"{chain}.{d - 1}" if d else "head"
    disk = ndi.generate_binary_structure(2, 1)
    sprites = []
    for sg in segs:
        own_m = sg["own"]
        overlap = (
            ndi.binary_dilation(own_m, disk, iterations=bh.OVERLAP) & sg["parent_mask"] & ~own_m
        )
        m = own_m | overlap
        ys, xs = np.nonzero(m)
        x0, y0, x1, y1 = xs.min(), ys.min(), xs.max() + 1, ys.max() + 1
        spr = np.zeros((y1 - y0, x1 - x0, 4), np.uint8)
        fx = np.zeros((y1 - y0, x1 - x0, 3), np.uint8)
        sub = m[y0:y1, x0:x1]
        spr[sub] = rgba[y0:y1, x0:x1][sub]
        fx[..., 0] = (rim[y0:y1, x0:x1] & sub) * 255
        oy, ox = np.nonzero(own_m)
        top = oy <= oy.min() + 1.5
        pivot = [round(float(ox[top].mean()), 1), round(float(oy[top].mean()), 1)]
        sprites.append(
            (sg, spr, fx, [int(x0), int(y0), int(x1 - x0), int(y1 - y0)], pivot, int(own_m.sum()))
        )
    # atlas (shelves)
    aw = 256
    x = y = row_h = 0
    places = []
    for _, spr, _, _, _, _ in sprites:
        sh, sw = spr.shape[:2]
        if x + sw > aw:
            x, y, row_h = 0, y + row_h + 1, 0
        places.append((x, y))
        x += sw + 1
        row_h = max(row_h, sh)
    ah = y + row_h
    atlas = np.zeros((ah, aw, 4), np.uint8)
    atlas_fx = np.zeros((ah, aw, 3), np.uint8)
    for (_, spr, fx, _, _, _), (ax, ay) in zip(sprites, places, strict=True):
        sh, sw = spr.shape[:2]
        atlas[ay : ay + sh, ax : ax + sw] = spr
        atlas_fx[ay : ay + sh, ax : ax + sw] = fx
    hm = np.zeros((h, w, 3), np.uint8)
    hm[..., 0] = (
        alpha & ~hanging
    ) * 255  # her body under the hair (the character layer's mask is used at run time)
    Image.fromarray(atlas).save(out_dir / f"{stem}.png", optimize=True)
    Image.fromarray(atlas_fx).save(out_dir / f"{stem}_fx.png", optimize=True)
    Image.fromarray(hm).save(out_dir / f"{stem}_mask.png", optimize=True)
    # draw order: the top layer of locks first (later painted = in front), tips over parents
    paint_rank = {}
    for L in info["locks"]:
        paint_rank[L["chain"]] = max(paint_rank.get(L["chain"], -1), L["i"])
    entries = []
    for (sg, _, _, box, pivot, n), (ax, ay) in zip(sprites, places, strict=True):
        entries.append({
            "id": sg["id"], "part": sg["part"], "chain": sg["chain"], "parent": sg["parent"],
            "depth": sg["depth"], "z": "front", "side": sg["side"], "box": box, "at": [ax, ay],
            "pivot": pivot, "pixels": n,
            "breath": round(float(np.clip((196 - pivot[1]) / 40.0, 0, 1)), 2),
        })  # fmt: skip
    # chains whose locks were painted last are in front; within a chain the tip first
    entries.sort(key=lambda e: (-paint_rank[e["chain"]], e["chain"], -e["depth"]))
    meta = {
        "view": "back",
        "size": [w, h],
        "atlas": [aw, int(ah)],
        "parts": [
            {
                "id": "locks_side",
                "z": "front",
                "slot": "over:body",
                "chains": ["locks_l", "locks_r"],
                "tint": "#e0a030",
            },
            {
                "id": "locks",
                "z": "front",
                "slot": "over:body",
                "chains": ["locks_cl", "locks_c", "locks_cr"],
                "tint": "#9a5cf0",
            },
        ],
        "layerOrder": ["char:body", "hair:locks_side", "hair:locks", "char:arms"],
        "segments": entries,
        "hairPixels": int(hanging.sum()),
        "crownPixels": int((alpha & ~hanging & (own >= 0)).sum()),
        "restHash": bh.rest_hash(rgba[..., :3], alpha, rim),
        # sha256 of the uncut back sheet's alpha + colour (tests/stage_back_view_test.cjs: the
        # character layer's back sheet + these segments at rest must give the same picture)
        "restRgbHash": rgb_hash(rgba),
    }
    (out_dir / f"{stem}.json").write_text(json.dumps(meta, indent=1) + "\n", encoding="utf-8")
    return meta


def rgb_hash(rgba):
    import hashlib

    a = (rgba[..., 3] > 127).astype(np.uint8)
    buf = np.dstack([a, rgba[..., :3] * a[..., None]]).astype(np.uint8)
    return hashlib.sha256(buf.tobytes()).hexdigest()


def cv_low(px, m):
    import cv2

    return cv2.resize(m.astype(np.float32), px.size, interpolation=cv2.INTER_AREA) > 0.5


def back_ears(parts, hx, hy):
    """The icon's ears mirrored (positions of a back view), one ramp step darker."""
    full = parts.full
    m = np.zeros(full.shape[:2], bool)
    for e in parts.EARS.values():
        m |= ndi.binary_dilation(bc.poly(parts.size, e["poly"]), iterations=1)
    src = np.zeros_like(full)
    src[m] = full[m]
    mir = src[:, ::-1].copy()
    shift = round(2 * CX - (full.shape[1] - 1))
    mir = np.roll(mir, shift, axis=1)
    mir = np.roll(np.roll(mir, round(hy), axis=0), round(hx), axis=1)
    ramp = [(bc.hexrgb(c) * 255).round().astype(int) for c in bc.RAMPS["hair"]]
    rgb = mir[..., :3].astype(int)
    out = mir.copy()
    for i in range(1, len(ramp)):
        hit = (np.abs(rgb - ramp[i]).sum(2) <= 3) & (mir[..., 3] > 0)
        out[hit, :3] = ramp[i - 1]
    return out
