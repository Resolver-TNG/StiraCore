"""Arms for the Stage character (#341): the mug hand moved rigidly from the icon, drawn free
hands and sleeves. Called by char_parts.py (offline; nothing here runs in the browser).

The free hand and the sleeves are built from capsules and polygons at the icon's resolution
(4x), given a lightness per pixel (cylinder shading, key light from the upper left) and
material ids, and then pixelized with the same ramps, ordered dither, line art and outline as
the rest of the character. Coordinates are portrait pixels.
"""

from __future__ import annotations

import build_char as bc
import numpy as np
from scipy import ndimage as ndi

LIGHT = np.array([-0.55, -0.83])  # key light from the upper left (unit vector)
SKIN, COAT, NAVY, MUG = 1, 2, 3, 4


class Canvas:
    """Hi-res drawing surface: lightness, material, coverage and ink, painter's order."""

    def __init__(self, px) -> None:
        self.px = px
        s = bc.F
        self.s = s
        h, w = px.h0, px.w0
        self.t = np.full((h, w), np.nan, np.float32)
        self.mat = np.zeros((h, w), np.uint8)
        self.cov = np.zeros((h, w), bool)
        self.ink = np.zeros((h, w), np.float32)
        self.Y, self.X = np.mgrid[0:h, 0:w].astype(np.float32)

    def _box(self, pts, pad):
        s = self.s
        xs = [p[0] * s for p in pts]
        ys = [p[1] * s for p in pts]
        x0 = int(max(0, min(xs) - pad * s))
        x1 = int(min(self.px.w0, max(xs) + pad * s + 1))
        y0 = int(max(0, min(ys) - pad * s))
        y1 = int(min(self.px.h0, max(ys) + pad * s + 1))
        return slice(y0, y1), slice(x0, x1)

    group = None

    def paint(self, sl, inside, t, mat, edge, ink_w=1.7):
        """inside: bool mask in the slice; edge: distance to the boundary (hi-res px)."""
        self.t[sl][inside] = t[inside]
        self.mat[sl][inside] = mat
        self.cov[sl][inside] = True
        self.ink[sl][inside] = 0
        if self.group is not None:
            self.group[sl] |= inside
            return
        ring = inside & (edge < ink_w)
        self.ink[sl][ring] = 1

    def begin(self):
        """Parts painted until end() share one outline (no lines at the finger joints)."""
        self.group = np.zeros(self.cov.shape, bool)

    def end(self, ink_w=1.7):
        g = self.group
        self.group = None
        ys, xs = np.where(g)
        if not len(ys):
            return
        sl = (slice(max(0, ys.min() - 2), ys.max() + 3), slice(max(0, xs.min() - 2), xs.max() + 3))
        sub = g[sl]
        edge = ndi.distance_transform_edt(sub)
        self.ink[sl][sub & (edge < ink_w + 0.5)] = 1

    def capsule(self, p0, p1, r0, r1, mat=SKIN, base=0.62, amp=0.3, ink=1.7):
        s = self.s
        sl = self._box([p0, p1], max(r0, r1) + 2)
        X, Y = self.X[sl] / s, self.Y[sl] / s
        a = np.array(p0, np.float32)
        b = np.array(p1, np.float32)
        ab = b - a
        L2 = float(ab @ ab) or 1e-6
        u = np.clip(((X - a[0]) * ab[0] + (Y - a[1]) * ab[1]) / L2, 0, 1)
        cx, cy = a[0] + u * ab[0], a[1] + u * ab[1]
        r = r0 + (r1 - r0) * u
        ox, oy = X - cx, Y - cy
        dist = np.hypot(ox, oy)
        inside = dist <= r
        n = np.hypot(ox, oy) + 1e-6
        nx, ny = ox / n, oy / n
        across = np.clip(dist / np.maximum(r, 1e-3), 0, 1)
        lit = nx * -LIGHT[0] * -1 + ny * -LIGHT[1] * -1  # dot(normal, light)
        t = base + amp * (np.sqrt(1 - across**2) - 0.5) + 0.22 * lit * across
        edge = (r - dist) * s
        self.paint(sl, inside, t.astype(np.float32), mat, edge, ink)

    def polygon(self, pts, mat, base=0.6, amp=0.25, axis=None, ink=1.7, folds=()):
        """Filled polygon; lightness varies across `axis` (a direction), plus fold lines."""
        s = self.s
        sl = self._box(pts, 2)
        from PIL import Image, ImageDraw

        y0, x0 = sl[0].start, sl[1].start
        hh, ww = sl[0].stop - y0, sl[1].stop - x0
        im = Image.new("L", (ww, hh), 0)
        ImageDraw.Draw(im).polygon([(p[0] * s - x0, p[1] * s - y0) for p in pts], fill=1)
        inside = np.asarray(im).astype(bool)
        edge = ndi.distance_transform_edt(inside)
        X, Y = self.X[sl] / s, self.Y[sl] / s
        cx = np.mean([p[0] for p in pts])
        cy = np.mean([p[1] for p in pts])
        if axis is None:
            axis = (1.0, 0.0)
        ax = np.array(axis, np.float32) / (np.hypot(*axis) or 1)
        across = (X - cx) * ax[0] + (Y - cy) * ax[1]
        span = max(1.0, np.abs(across[inside]).max()) if inside.any() else 1.0
        q = across / span
        lit = -(ax @ LIGHT)
        t = base + amp * (np.sqrt(np.clip(1 - q**2, 0, 1)) - 0.5) + 0.2 * lit * q
        self.paint(sl, inside, t.astype(np.float32), mat, edge, ink)
        for f0, f1 in folds:
            self.line(f0, f1)

    def line(self, p0, p1, width=1.8):
        s = self.s
        sl = self._box([p0, p1], 2)
        X, Y = self.X[sl], self.Y[sl]
        a = np.array(p0, np.float32) * s
        b = np.array(p1, np.float32) * s
        ab = b - a
        L2 = float(ab @ ab) or 1e-6
        u = np.clip(((X - a[0]) * ab[0] + (Y - a[1]) * ab[1]) / L2, 0, 1)
        d = np.hypot(X - (a[0] + u * ab[0]), Y - (a[1] + u * ab[1]))
        m = (d < width) & self.cov[sl]
        self.ink[sl][m] = 1

    def shade_tone(self, mask_fn=None):
        pass

    def render(self, soft=False, behind=None):
        """Pixelize: area-average lightness/ink, majority material, then the shared ramps.
        soft: the drawn arms' style (soft_shade); behind: sheet pixels her body covers."""
        px = self.px
        import cv2

        cov = self.cov.astype(np.float32)
        tt = np.nan_to_num(self.t, nan=0.0) * cov
        size = px.size
        cov_s = cv2.resize(cov, size, interpolation=cv2.INTER_AREA)
        t_s = cv2.resize(tt, size, interpolation=cv2.INTER_AREA) / np.maximum(cov_s, 1e-3)
        ink_s = cv2.resize(self.ink, size, interpolation=cv2.INTER_AREA)
        sil = cov_s > 0.5
        mats = np.zeros((px.th, px.tw), np.uint8)
        best = np.zeros((px.th, px.tw), np.float32)
        for k in (SKIN, COAT, NAVY, MUG):
            f = cv2.resize((self.mat == k).astype(np.float32), size, interpolation=cv2.INTER_AREA)
            better = f > best
            mats[better] = k
            best[better] = f[better]
        tmap = np.where(sil, np.clip(t_s, 0, 1), np.nan).astype(np.float32)
        if soft:
            return soft_shade(px, mats, sil, ink_s, tmap, behind)
        col = np.zeros((px.th, px.tw, 3), np.float32)
        return px.shade(col, mats, sil, ink_s, tmap=tmap)


# --- the drawn arms in the icon's hand style (#341, art unify) --------------------------------
# The icon's own (mug) hand is pale: mostly the two lightest skin steps, a thin brownish line
# where it meets something, light gaps between the fingers. The drawn hands used the same ramps
# but sat one step darker, had a 1 px ink ring *and* the black outline (2 px dark), and the
# fingers were separated by the darkest skin line, so they read orange-brown and heavy next to
# the face. soft_shade() renders the drawn arms like the icon's hand.
DITHER_DRAWN = 0.35  # ordered dither only near a step: flat drawn cloth showed a dot grid
CONTOUR = {  # 1 px contour per material: (over her own body, against the room)
    SKIN: (bc.LINE["skin"], bc.LINE["skin"]),
    COAT: (bc.RAMPS["coat"][1], bc.OUTLINE),
    NAVY: (bc.RAMPS["navy"][0], bc.OUTLINE),
    MUG: (bc.LINE["mug"], bc.OUTLINE),
}
INNER = {  # lines inside a drawn part (finger gaps, folds, the cuff's edge)
    SKIN: bc.RAMPS["skin"][2],
    COAT: bc.RAMPS["coat"][1],
    NAVY: bc.RAMPS["navy"][0],
    MUG: bc.LINE["mug"],
}


def soft_shade(px, mats, sil, ink_s, tmap, behind=None):
    """Ramps with a narrow dither band, a 1 px contour (lighter where the part lies over her
    own body: `behind`, sheet pixels), light inner lines. No black ring around the hand."""
    names = {SKIN: "skin", COAT: "coat", NAVY: "navy", MUG: "mug"}
    out = np.zeros((px.th, px.tw, 3), np.float32)
    for k, name in names.items():
        m = (mats == k) & sil
        if not m.any():
            continue
        ramp = np.array([bc.hexrgb(c) for c in bc.RAMPS[name]])
        n = len(ramp)
        t = np.nan_to_num(tmap, nan=0.5)
        idx = np.clip(np.floor(t * (n - 1) + 0.5 + px.dith * 0.55 * DITHER_DRAWN), 0, n - 1)
        out[m] = ramp[idx.astype(int)[m]]
    contour = sil & ~ndi.binary_erosion(sil)
    line = (ink_s > 0.25) & sil & ~contour
    labels, count = ndi.label(line, structure=np.ones((3, 3)))
    if count:
        sizes = ndi.sum(line, labels, range(1, count + 1))
        line &= ~np.isin(labels, np.where(sizes < 3)[0] + 1)
    # a material edge inside the part (skin / cuff, cuff / sleeve) is a line too
    for k in names:
        mk = (mats == k) & sil
        edge_k = mk & ~ndi.binary_erosion(mk) & ~contour
        if k == SKIN:
            line |= edge_k  # the hand's edge on the cuff
    for k in names:
        out[line & (mats == k)] = bc.hexrgb(INNER[k])
    on_body = np.zeros_like(sil) if behind is None else behind
    # next to the hand the cloth's contour is soft too (no black pixel touches the skin)
    by_skin = ndi.binary_dilation((mats == SKIN) & sil)
    for k, (soft, hard) in CONTOUR.items():
        mk = contour & (mats == k)
        out[mk & (on_body | by_skin)] = bc.hexrgb(soft)
        out[mk & ~(on_body | by_skin)] = bc.hexrgb(hard)
    rgba = np.dstack([out * 255, sil * 255]).astype(np.uint8)
    rgba[~sil] = 0
    return rgba


def knit_cuff(cv, wrist, fwd, width, length, base=0.5):
    """The turtleneck's knit cuff out of the coat sleeve: a short navy band with ribs along the
    arm (two navy steps in 2 px stripes) and a darker rim at the hand."""
    w = np.array(wrist, np.float32)
    u = np.array(fwd, np.float32) / (np.hypot(*fwd) or 1)
    n = np.array([-u[1], u[0]])
    b = w - u * length
    pts = [w + n * width / 2, w - n * width / 2, b - n * width / 2, b + n * width / 2]
    cv.polygon([tuple(p) for p in pts], NAVY, base=base, amp=0.25, axis=tuple(n), ink=0)
    sl = cv._box([tuple(p) for p in pts], 1)
    X, Y = cv.X[sl] / cv.s, cv.Y[sl] / cv.s
    inside = cv.cov[sl] & (cv.mat[sl] == NAVY)
    across = (X - w[0]) * n[0] + (Y - w[1]) * n[1]
    along = (X - w[0]) * u[0] + (Y - w[1]) * u[1]
    rib = np.where(np.floor(across / 2.0) % 2 == 0, 0.16, -0.06)
    rim = np.where(along > -1.5, -0.12, 0.0)
    tt = cv.t[sl]
    tt[inside] = np.clip(tt + rib + rim, 0, 1)[inside]


def over(dst, src):
    m = src[..., 3] > 0
    dst[m] = src[m]


# --- hands (portrait pixels) -------------------------------------------------------------------


def rot(v, deg):
    t = np.radians(deg)
    return np.array([v[0] * np.cos(t) - v[1] * np.sin(t), v[0] * np.sin(t) + v[1] * np.cos(t)])


SKIN_LIT = 0.86  # the drawn hand's lightness (the icon's hand: mostly the two lightest steps)
SKIN_CURLED = 0.8  # fingers curled toward the palm, a little in shade

# Finger poses (index, middle, ring, little; then the thumb). A finger: segment lengths and
# bends in degrees (+ = toward the palm side); None = the default for the pose's kind.
CURL = ([4.0, 3.2, 2.5], [60, 75, 45])
FINGERS = {
    "open": {"f": [(None, [0, 4, 4])] * 4, "thumb": (-48, [6.5, 5.5], [0, 10])},
    "relaxed": {"f": [(None, [18, 24, 16]), (None, [24, 28, 18]), (None, [28, 30, 20]),
                      (None, [32, 30, 20])], "thumb": (-36, [6.0, 5.0], [10, 16])},
    "point": {"f": [("straight", [0, 0, 0]), CURL, CURL, CURL], "thumb": (-25, [6, 5], [20, 30])},
    "fist": {"f": [CURL] * 4, "thumb": (-15, [6, 4.5], [35, 40])},
    # 06 顎に手を当てる: the hand under the jaw, its back to us, the index finger laid along the
    # jaw toward the chin (almost straight), the other fingers curled under, the thumb tucked
    "chin": {"f": [([6.8, 5.2, 4.0], [4, 8, 6]), ([4.4, 3.4, 2.6], [52, 66, 40]), CURL, CURL],
             "thumb": (-28, [5.6, 4.2], [34, 38])},
    # 06 ペンで書く: the pen between the thumb and the index finger, the index finger along the
    # pen's top, the middle finger under it, ring and little finger curled
    "pen": {"f": [([6.6, 5.0, 3.8], [6, 14, 10]), ([5.6, 4.4, 3.4], [24, 34, 24]), CURL, CURL],
            "thumb": (-46, [6.2, 4.8], [8, 14])},
}  # fmt: skip


def finger(cv, root, direction, segs, radius, bends, scale=1.0, base=SKIN_LIT):
    """A finger as a chain of capsules; bends in degrees (+ = toward the palm side)."""
    p = np.array(root, np.float32)
    ang = direction
    r = radius
    cv.begin()
    for i, (L, b) in enumerate(zip(segs, bends)):
        ang += b
        q = p + rot((L * scale, 0), ang)
        r1 = r * (0.92 if i < len(segs) - 1 else 0.85)
        cv.capsule(tuple(p), tuple(q), r, r1, SKIN, base=base)
        p, r = q, r1
    cv.end()
    return p


def hand(cv, wrist, angle, pose, side=1, size=1.0, under_index=None):
    """wrist: portrait px; angle: direction wrist -> knuckles (deg, 0 = +x, 90 = down);
    side: +1 / -1 mirrors the thumb; pose: a key of FINGERS, or 'type1' | 'type2'.
    Returns the pinch point (between the thumb tip and the index tip)."""
    w = np.array(wrist, np.float32)
    fwd = rot((1, 0), angle)
    perp = rot((1, 0), angle + 90) * side
    palm_len = 12 * size
    palm_w = 14 * size
    k = w + fwd * palm_len  # knuckle line centre
    # fingers: offsets across the knuckle line (index on the thumb side)
    offs = [-0.42, -0.14, 0.14, 0.4]
    lengths = [[6.5, 5, 4], [7, 5.5, 4.2], [6.5, 5, 4], [5, 4, 3.2]]
    radii = [2.8, 2.9, 2.8, 2.4]
    spread = [-9, -3, 3, 9]
    # palm first (behind the fingers), lit on the back of the hand
    corners = [
        w - perp * palm_w * 0.32,
        w + perp * palm_w * 0.38,
        k + perp * palm_w * 0.5,
        k - perp * palm_w * 0.5,
    ]
    cv.begin()
    cv.polygon([tuple(c) for c in corners], SKIN, base=SKIN_LIT, amp=0.18, axis=tuple(perp))
    for o, r0, r1 in ((-2.0, 5.5, 6.2), (2.0, 5.0, 6.0)):
        # the heel of the hand starts at the wrist (not behind it: the cuff is there)
        p0 = w + fwd * r0 * size * 0.7 + perp * o
        cv.capsule(tuple(p0), tuple(k + perp * (o * 0.75)), r0 * size, r1 * size, SKIN,
                   base=SKIN_LIT - 0.02, amp=0.18, ink=0)  # fmt: skip
    cv.end()
    tips = {}
    for i in (3, 2, 1, 0):
        root = k + perp * palm_w * offs[i] - fwd * 1.0
        if i == 0 and under_index is not None:
            under_index(root)
        d = angle + spread[i] * side
        if pose in ("type1", "type2"):
            lift = (i == 0 and pose == "type1") or (i == 1 and pose == "type2")
            bend = 22 if lift else 48
            tips[i] = finger(cv, root, d, [6.0, 4.4, 3.6], radii[i] * size,
                             [bend * side, 30 * side, 18 * side], size,
                             base=SKIN_LIT if lift else SKIN_CURLED)  # fmt: skip
            continue
        segs, bends = FINGERS[pose]["f"][i]
        curled = bends[0] >= 40
        if segs == "straight":
            segs, d = lengths[i], angle - 4 * side
            tips[i] = finger(cv, root, d, segs, radii[i] * size, bends, size * 1.05)
            continue
        tips[i] = finger(cv, root, d, segs or lengths[i], radii[i] * size,
                         [b * side for b in bends], size,
                         base=SKIN_CURLED if curled else SKIN_LIT)  # fmt: skip
    # thumb on the index side
    troot = w + fwd * 4 * size - perp * palm_w * 0.42
    if pose in ("type1", "type2"):
        ta, tsegs, tb = -30, [6, 4.5], [25, 20]
    else:
        ta, tsegs, tb = FINGERS[pose]["thumb"]
    tip = finger(cv, troot, angle + ta * side, tsegs, 2.8 * size, [b * side for b in tb], size,
                 base=SKIN_LIT - 0.04)  # fmt: skip
    return (tip + tips[0]) / 2


def pen(cv, at, angle, length=17.0, radius=1.5):
    """A black pen held at `at` (portrait px), pointing along `angle` (deg): body, clip, tip."""
    a = np.array(at, np.float32)
    u = rot((1, 0), angle)
    cv.begin()
    cv.capsule(tuple(a - u * length * 0.45), tuple(a + u * length * 0.55), radius,
               radius * 0.8, MUG, base=0.42, amp=0.4)  # fmt: skip
    cv.end()
    tip = a + u * length * 0.55
    cv.capsule(tuple(tip), tuple(tip + u * 2.2), 0.9, 0.5, MUG, base=0.75, amp=0.1, ink=0)


def sleeve(cv, wrist, elbow, w_wrist, w_elbow, mat, cuff=True, base=0.55, round_end=0.0):
    """A sleeve from the elbow to the wrist (portrait px): a tapered tube with a rounded elbow,
    two creases and a cuff. round_end: the wrist end's corners cut back by this much (px), so
    the opening ends in a rounded edge of the tube itself (no extra shape, no line across)."""
    a = np.array(elbow, np.float32)
    b = np.array(wrist, np.float32)
    d = b - a
    L = float(np.hypot(*d)) or 1.0
    u = d / L
    n = np.array([-u[1], u[0]])
    cv.capsule(
        tuple(a),
        tuple(a + u * min(L * 0.3, w_elbow * 0.6)),
        w_elbow / 2,
        w_elbow / 2 * 0.96,
        mat,
        base=base - 0.04,
        amp=0.3,
    )
    if round_end:
        r = round_end
        hw = w_wrist / 2
        pts = [a + n * w_elbow / 2, b + n * hw - u * r, b + n * (hw - r * 0.35) - u * r * 0.3,
               b + n * (hw - r), b - n * (hw - r), b - n * (hw - r * 0.35) - u * r * 0.3,
               b - n * hw - u * r, a - n * w_elbow / 2]  # fmt: skip
    else:
        pts = [a + n * w_elbow / 2, b + n * w_wrist / 2, b - n * w_wrist / 2, a - n * w_elbow / 2]
    m1 = a + d * 0.45
    m2 = a + d * 0.7
    folds = [
        (tuple(m1 + n * w_elbow * 0.42), tuple(m1 + u * 3.5 + n * w_elbow * 0.05)),
        (tuple(m2 - n * w_wrist * 0.45), tuple(m2 + u * 2.5 - n * w_wrist * 0.08)),
    ]
    cv.polygon([tuple(p) for p in pts], mat, base=base, amp=0.45, axis=tuple(n), folds=folds)
    if mat == COAT:
        # soft folds along the sleeve (as char_body's cloth): flat drawn cloth dithered into a
        # regular dot grid; with the folds it breaks into bands like the icon's coat
        sl = cv._box([tuple(p) for p in pts], 1)
        X, Y = cv.X[sl] / cv.s, cv.Y[sl] / cv.s
        along = (X - a[0]) * u[0] + (Y - a[1]) * u[1]
        across = (X - a[0]) * n[0] + (Y - a[1]) * n[1]
        fold = 0.06 * np.sin(along * 0.21 + across * 0.35) + 0.04 * np.sin(along * 0.47 - 1.3)
        inside = cv.cov[sl] & (cv.mat[sl] == COAT)
        tt = cv.t[sl]
        tt[inside] = np.clip(tt + fold, 0, 1)[inside]
    if cuff:
        c0 = b - u * 3.2
        cv.polygon([tuple(c0 + n * (w_wrist / 2 + 0.6)), tuple(b + n * (w_wrist / 2 + 0.3)),
                    tuple(b - n * (w_wrist / 2 + 0.3)), tuple(c0 - n * (w_wrist / 2 + 0.6))],
                   mat, base=base - 0.08, amp=0.3, axis=tuple(n))  # fmt: skip


# --- the mug hand, moved rigidly from the icon ---------------------------------------------------


def coat_sleeve(cv, wrist, elbow, w_elbow=32.0):
    """The mug arm's sleeve as in the icon: the navy turtleneck cuff at the wrist (knit), then
    the lab coat's sleeve down to the elbow (matches the drawn body below the circle)."""
    w = np.array(wrist, np.float32)
    e = np.array(elbow, np.float32)
    u = (e - w) / (np.hypot(*(e - w)) or 1)
    cuff_end = w + u * 9
    # (art fix, #341) the opening is the tube's own rounded end, under the knit cuff: art unify
    # 5 rounded it with a disc on the end, whose ring drew an arc across the sleeve and read as
    # a ball at the wrist (`mug_up` ... `mug_sip`); the coat's turned-back cuff band drew a line
    # across it too. Rounded corners (no flat end sticking out beside the cuff), no line.
    sleeve(cv, tuple(cuff_end), tuple(e), 27.0, w_elbow, COAT, cuff=False, base=0.8, round_end=4.0)
    # the knit reaches 2 px further under the hand (the hand is drawn over it): with the icon's
    # cuff kept only on the drawn one, the raised hand (`mug_sip`) stood 1-2 px off the cuff
    knit_cuff(cv, tuple(w - u * 3), tuple(-u), 15.0, 13.0, base=0.46)


def hand_and_mug_only(img, mat_lo, wrist=None):
    """(art unify 5) What moves with the mug hand is the hand and the mug: the icon's turtleneck
    round the mug and the hair strand over the hand stayed in the cut and moved with it (a navy
    patch, a grey hand in `mug_low`). The turtleneck is dropped (the part behind is completed);
    the hair's pixels inside the hand take the nearest skin (the hand closed under the strand)."""
    out = img.copy()
    a = img[..., 3] > 0
    skin = a & (mat_lo == 1)
    keep = skin | (a & (mat_lo == 4))
    if wrist is not None:  # the icon's own cuff at the wrist stays (it joins the drawn one)
        yy, xx = np.mgrid[0 : a.shape[0], 0 : a.shape[1]]
        keep |= a & (mat_lo == 3) & (np.hypot(xx - wrist[0], yy - wrist[1]) < 10)
    hole = ndi.binary_closing(skin, iterations=3) & ~keep & a & (mat_lo != 4)
    if skin.any() and hole.any():
        _, (iy, ix) = ndi.distance_transform_edt(~skin, return_indices=True)
        out[hole] = img[iy[hole], ix[hole]]
        keep |= hole
    # no dark pixel left alone on the new edge: tested against a light ground (the coat it is
    # drawn over), as the art lint sees it in the frame
    from char_unify import specks

    probe = out.copy()
    probe[~keep] = (bc.hexrgb(bc.RAMPS["coat"][3]) * 255).round().astype(np.uint8).tolist() + [255]
    probe = specks(probe)
    out[keep] = probe[keep]
    out[~keep] = 0
    return out


def mug_hand(parts, rot_deg, shift, elbow, w_elbow=32.0):
    """The icon's hand + mug rotated about the mug centre and shifted; the cuff and the coat
    sleeve from the new wrist to `elbow`. Returns (rgba, mug top anchor)."""
    px = parts.px
    size = parts.size
    region = ndi.binary_dilation(bc.poly(size, bc.HAND) | bc.poly(size, bc.MUG), iterations=1)
    cx, cy = 127.0, 193.0
    t = np.radians(rot_deg)
    # inverse map: dst p -> src = R^-1 (p - c - shift) + c
    ox, oy = px.xx - cx - shift[0], px.yy - cy - shift[1]
    sx = np.cos(t) * ox + np.sin(t) * oy + cx
    sy = -np.sin(t) * ox + np.cos(t) * oy + cy
    dx, dy = px.xx - sx, px.yy - sy
    alpha = px.alpha * px.up(region)
    img, mat_lo, _ = px.render(px.smooth, alpha, px.ink, px.mat, dx, dy)
    wrist_src = np.array([91.0, 230.0]) - (cx, cy)
    wrist = rot(wrist_src, rot_deg) + (cx, cy) + shift
    img = hand_and_mug_only(img, mat_lo, wrist)
    cv = Canvas(px)
    coat_sleeve(cv, wrist + rot((0, 2), rot_deg), elbow, w_elbow)
    out = cv.render(soft=True, behind=parts.union)
    # (art fix, #341) the icon's own cuff joins the drawn one only where the drawn cuff / sleeve
    # is, and without its outline: beside it, its edge stood alone off the wrist (a dark hook
    # next to the knit cuff), and over it the outline drew a black line across the knit
    cuff = (img[..., 3] > 0) & (mat_lo == 3)
    black = (img[..., :3] == (bc.hexrgb(bc.OUTLINE) * 255).round()).all(2)
    img[cuff & ((out[..., 3] == 0) | black)] = 0
    over(out, img)
    # no pixel left on its own (a fingertip pixel the cut kept off the hand)
    a = out[..., 3] > 0
    labels, n = ndi.label(a, structure=np.ones((3, 3)))
    if n > 1:
        sizes = np.bincount(labels.ravel())
        sizes[0] = 0
        out[(sizes < 4)[labels] & a] = 0
    top = rot(np.array([127.0, 168.0]) - (cx, cy), rot_deg) + (cx, cy) + shift
    return out, [round(float(top[0]), 1), round(float(top[1]), 1)]


def free_hand(
    parts,
    wrist,
    angle,
    pose,
    elbow,
    mat=COAT,
    w_elbow=24.0,
    w_wrist=17.0,
    side=1,
    size=1.0,
    sleeve_base=0.74,
    pen_angle=None,
    shoulder=None,
):
    """The free hand: the coat sleeve from the elbow (and from the shoulder to the elbow), the
    navy knit cuff, the hand (and a pen held under the index finger)."""
    cv = Canvas(parts.px)
    fwd = rot((1, 0), angle)
    w = np.array(wrist, np.float32)
    cuff = 4.5 * size
    if shoulder is not None:
        # (art unify 5) the upper arm's sleeve from the shoulder to the elbow, behind the forearm
        sleeve(
            cv, elbow, shoulder, w_elbow + 1, w_elbow + 3, mat, cuff=False, base=sleeve_base - 0.04
        )
    sleeve(cv, tuple(w - fwd * (cuff + 1.0)), elbow, w_wrist, w_elbow, mat, base=sleeve_base)
    knit_cuff(cv, tuple(w + fwd * 1.0), tuple(fwd), 9.5 * size, cuff + 2.0, base=0.46)
    under = None
    if pen_angle is not None:

        def under(index_root):
            # the pen from over the knuckles, under the index finger, down past its tip
            at = np.array(index_root, np.float32) + fwd * 9.0 * size
            pen(cv, tuple(at), pen_angle, length=27.0, radius=1.9)

    hand(cv, wrist, angle, pose, side, size, under_index=under)
    return cv.render(soft=True, behind=parts.union)


def desk_mug(parts):
    """The mug set down on the desk edge (for typing): body with a highlight, rim, handle.
    Returns (rgba, top anchor)."""
    cv = Canvas(parts.px)
    x0, x1, y0, y1 = 152.0, 180.0, 188.0, 228.0
    cx = (x0 + x1) / 2
    # handle (behind the body's right edge)
    cv.begin()
    cv.capsule((x1 - 2, y0 + 8), (x1 + 6, y0 + 11), 2.6, 2.6, MUG, base=0.32, amp=0.2)
    cv.capsule((x1 + 6, y0 + 11), (x1 + 6, y0 + 23), 2.6, 2.6, MUG, base=0.3, amp=0.2)
    cv.capsule((x1 + 6, y0 + 23), (x1 - 2, y0 + 26), 2.6, 2.6, MUG, base=0.26, amp=0.2)
    cv.end()
    body = [(x0, y0 + 2), (x1, y0 + 2), (x1, y1 - 2), (x1 - 3, y1), (x0 + 3, y1), (x0, y1 - 2)]
    cv.polygon(body, MUG, base=0.3, amp=0.35, axis=(1.0, 0.0))
    # a soft vertical highlight on the lit (left) side, the rim ring
    sl = cv._box(body, 1)
    X, Y = cv.X[sl] / bc.F, cv.Y[sl] / bc.F
    inside = cv.cov[sl] & (cv.mat[sl] == MUG)
    hl = (
        np.exp(-(((X - (x0 + 7)) / 2.2) ** 2))
        * np.clip((Y - y0) / 8.0, 0, 1)
        * np.clip((y1 - Y) / 10.0, 0, 1)
    )
    cv.t[sl][inside] = np.clip(cv.t[sl] + 0.55 * hl, 0, 1)[inside]
    cv.capsule((x0 + 1.5, y0 + 2.2), (x1 - 1.5, y0 + 2.2), 1.9, 1.9, MUG, base=0.75, amp=0.15)
    cv.line((x0 + 3, y0 + 4.2), (x1 - 3, y0 + 4.2), 1.2)
    return cv.render(), [round(cx), round(y0)]


# The free (left) hand's gestures (06: typing, holding the mug, hand to the chin, pointing,
# writing with a pen): wrist, direction wrist -> knuckles, pose, elbow, thumb side, size.
# `raise` is the in-between of every gesture (the hand on its way up), close to all of them so
# the change to the gesture is a short step.
# art unify 5 (01 / 06 checked again): the elbows inside the frame, at her side — with the
# elbows below the frame (y ~300) every raised forearm was a ~150 px pole, longer than her head
FREE = {
    "point": ((212.0, 138.0), -66, "point", (232.0, 262.0), 1, 1.25),
    "wave1": ((217.0, 128.0), -98, "open", (230.0, 262.0), -1, 1.25),
    "wave2": ((221.0, 130.0), -78, "open", (232.0, 262.0), -1, 1.25),
    "raise": ((200.0, 168.0), -100, "relaxed", (222.0, 268.0), -1, 1.2),
    "chin": ((174.0, 148.0), -158, "chin", (206.0, 256.0), -1, 1.1),
    # halfway up to `chin` (the hand on its way: ponder's intro and exit)
    "chin_in": ((190.0, 170.0), -130, "relaxed", (214.0, 268.0), -1, 1.15),
    "pen": ((200.0, 212.0), -168, "pen", (240.0, 274.0), -1, 1.15),
}
PEN_ANGLE = {"pen": 146}
SHOULDER_L = (
    214.0,
    186.0,
)  # her left shoulder (the upper arm of the free gestures starts here)  # down to the left, past the fingertips


def build(parts):
    """Returns [(side, name, rgba, mug_anchor_or_None)]."""
    out = []
    out.append(("r", "mug_up", *mug_hand(parts, 9, (3, -24), (30.0, 296.0))))
    out.append(("r", "mug_lift", *mug_hand(parts, 18, (-6, -42), (34.0, 286.0))))
    out.append(("r", "mug_sip", *mug_hand(parts, 28, (-15, -55), (38.0, 276.0))))
    out.append(("r", "mug_low", *mug_hand(parts, -4, (-2, 12), (22.0, 306.0))))
    out.append(("r", "mug_down", *mug_hand(parts, -6, (-4, 44), (22.0, 322.0))))
    # typing: the mug is set down on the desk edge, the hand on the keys at the lower left
    for name in ("type1", "type2"):
        cv = Canvas(parts.px)
        coat_sleeve(cv, (70.0, 194.0), (40.0, 292.0))
        hand(cv, (70.0, 194.0), 152, name, side=-1, size=1.2)
        out.append(("r", name, cv.render(soft=True, behind=parts.union), None))
    for name, (wrist, angle, pose, elbow, side, size) in FREE.items():
        narrow = name.startswith("chin")
        img = free_hand(parts, wrist, angle, pose, elbow, side=side, size=size,
                        w_elbow=29.0 if narrow else 31.0, w_wrist=19.0 if narrow else 21.0,
                        pen_angle=PEN_ANGLE.get(name), shoulder=SHOULDER_L)  # fmt: skip
        out.append(("l", name, img, None))
    return out
