"""Side profile of the Stage character (#341): the in-between of the turn from the front to the
back (she turns toward the room desk on the right of the picture, so her right side faces us).
Drawn in code like the back view (char_back.py), with the icon's ramps, dither, line art and
outline; there is no profile in the owner's icon. Called by char_parts.py (offline).

Painter's order: the long hair behind her, the lab coat seen from the side (the turtleneck in
the front opening), the near (right) arm reaching forward to the desk, the neck and the
turtleneck collar, the hair over the skull, the face (forehead, nose, lips, chin) with the eye
in profile, the bangs and the lock in front of the ear, the fox ears (the far one behind).
"""

from __future__ import annotations

import build_char as bc
import numpy as np
from char_arms import COAT, NAVY, SKIN
from char_back import EYE, HAIR, HCanvas, cv_low, spline

FACE = [
    (160, 60), (174, 64), (181, 74), (184, 84), (184, 91), (186, 97), (192, 106), (191, 109),
    (186, 110), (187, 113), (185, 115), (186, 118), (184, 121), (183, 126), (178, 131), (170, 133),
    (162, 129), (155, 118), (152, 100), (153, 80),
]  # fmt: skip
COAT_SIDE = [
    (124, 134), (150, 140), (174, 138), (186, 156), (193, 184), (194, 230), (192, 300),
    (104, 300), (102, 240), (104, 196), (110, 160),
]  # fmt: skip


def profile_view(parts, dx=0.0, dy=0.0):
    px = parts.px
    s = bc.F
    cv = HCanvas(px)
    Y, X = cv.Y / s, cv.X / s
    o = np.array([dx, dy], np.float32)

    def P(p):
        return tuple(np.asarray(p, np.float32) + o)

    def poly(pts, mat, base, amp=0.25, axis=(1.0, 0.0), folds=()):
        cv.polygon(
            [P(p) for p in pts],
            mat,
            base=base,
            amp=amp,
            axis=axis,
            folds=[(P(a), P(b)) for a, b in folds],
        )

    def ribbon(pts, widths, base, amp=0.24):
        path = spline([P(p) for p in pts])
        ws = np.interp(np.linspace(0, len(widths) - 1, len(path)), range(len(widths)), widths)
        for a, b, r0, r1 in zip(path[:-1], path[1:], ws[:-1], ws[1:]):
            cv.capsule(tuple(a), tuple(b), r0 / 2, r1 / 2, HAIR, base=base, amp=amp)
        return path, ws

    hair = np.zeros(cv.cov.shape, bool)
    # 1. the long hair behind her back
    cv.begin()
    for k, (x0, tip, w, b) in enumerate(
        (
            (104, 248, 26, 0.5),
            (116, 262, 28, 0.56),
            (128, 252, 24, 0.6),
            (98, 228, 18, 0.48),
            (138, 240, 18, 0.62),
        )
    ):
        ribbon(
            [
                (x0 + 18, 84),
                (x0 + 6, 120),
                (x0 - 2, 170),
                (x0 - 4, 210),
                (x0 - 2 + (k % 2) * 3, tip),
            ],
            [w * 0.8, w, w, w * 0.8, 1.0],
            b,
        )
    g = cv.group.copy()
    cv.end(ink_w=0.9)
    hair |= g
    # 2. the coat from the side, the turtleneck in the front opening, a seam and folds
    poly(
        COAT_SIDE,
        COAT,
        0.8,
        amp=0.3,
        axis=(1.0, 0.0),
        folds=[((150, 142), (148, 166)), ((112, 200), (118, 236))],
    )
    poly(
        [(176, 140), (186, 156), (190, 182), (184, 182), (178, 158), (172, 144)], NAVY, 0.4, amp=0.2
    )
    cv.line(P((172, 142)), P((186, 196)), 1.4)  # the lapel's edge
    # 3. the near (right) arm reaching forward to the desk
    cv.begin()
    cv.capsule(P((150, 160)), P((148, 220)), 15.0, 12.0, COAT, base=0.74, amp=0.4)
    cv.capsule(P((148, 220)), P((186, 210)), 12.0, 9.0, COAT, base=0.72, amp=0.4)
    cv.end(ink_w=1.6)
    cv.line(P((140, 214)), P((150, 210)), 1.2)  # elbow crease
    cv.begin()
    cv.capsule(P((184, 210)), P((192, 207)), 7.0, 6.4, NAVY, base=0.42, amp=0.25)
    cv.end(ink_w=1.3)
    cv.begin()
    cv.capsule(P((192, 207)), P((202, 203)), 5.6, 5.0, SKIN, base=0.66, amp=0.3)
    cv.capsule(P((202, 203)), P((208, 204)), 3.4, 2.6, SKIN, base=0.62, amp=0.3)
    cv.capsule(P((196, 204)), P((203, 199)), 2.2, 2.0, SKIN, base=0.68, amp=0.3)  # thumb
    cv.end(ink_w=1.3)
    # 4. neck and the turtleneck collar
    poly([(152, 112), (172, 116), (176, 136), (152, 138)], SKIN, 0.56, amp=0.2)
    cv.begin()
    poly([(150, 128), (175, 129), (179, 143), (148, 144)], NAVY, 0.42, amp=0.25)
    cv.end(ink_w=1.4)
    cv.line(P((152, 136)), P((176, 137)), 1.0)  # its fold
    # 5a. the fox ears (behind the skull hair, which covers their bases)
    ears = np.zeros(cv.cov.shape, bool)
    cv.begin()
    poly([(142, 40), (158, 4), (168, 40)], HAIR, 0.4, amp=0.2, axis=(1.0, -0.3))  # the far ear
    poly([(120, 48), (130, 2), (156, 42)], HAIR, 0.62, amp=0.25, axis=(1.0, -0.3))
    poly(
        [(140, 42), (132, 12), (152, 40)], HAIR, 0.8, amp=0.2, axis=(1.0, -0.3)
    )  # the lighter fur at the opening
    g = cv.group.copy()
    cv.end(ink_w=1.3)
    ears |= g
    # 5. the hair over the skull (lit from the upper left)
    cx, cy = 142.0 + dx, 80.0 + dy
    skull = ((X - cx) / 46.0) ** 2 + ((Y - cy) / 50.0) ** 2 <= 1.0
    nx, ny = (X - cx) / 46.0, (Y - cy) / 50.0
    t = 0.6 + 0.14 * np.sqrt(np.clip(1 - nx**2 - ny**2, 0, 1)) - 0.12 * (nx * 0.55 + ny * 0.83)
    cv.t[skull] = t[skull]
    cv.mat[skull] = HAIR
    cv.cov[skull] = True
    cv.ink[skull] = 0
    hair |= skull
    # 6. the face, the eye in profile, the mouth
    cv.begin()
    poly(FACE, SKIN, 0.66, amp=0.22, axis=(1.0, 0.25))
    cv.end(ink_w=1.4)
    face = np.zeros(cv.cov.shape, bool)
    from PIL import Image, ImageDraw

    im = Image.new("L", (px.w0, px.h0), 0)
    ImageDraw.Draw(im).polygon([(x * s, y * s) for x, y in (P(p) for p in FACE)], fill=1)
    face = np.asarray(im).astype(bool)
    # shade toward the back of the jaw (the hair's shadow), a little blush on the cheek
    cv.t[face] -= (0.12 * np.clip((168 + dx - X) / 14.0, 0, 1))[face]
    cheek = face & (np.hypot(X - (172 + dx), Y - (112 + dy)) < 5)
    cv.t[cheek] -= 0.05
    # the eye: upper lashes (thick, toward the back), the iris, the lower lid
    cv.capsule(P((176, 98)), P((181, 99)), 2.0, 1.4, EYE, base=0.45, amp=0.3)
    cv.line(P((170, 95.0)), P((182, 97.0)), 2.4)
    cv.line(P((171, 95.5)), P((169, 98)), 1.6)
    cv.line(P((177, 101.4)), P((181, 100.6)), 1.0)
    cv.line(P((186, 116.2)), P((183, 116.6)), 1.0)  # mouth
    # 7. the bangs and the lock in front of the ear
    cv.begin()
    for pts, w, b in (
        ([(150, 32), (170, 46), (182, 66), (186, 84), (184, 92)], [14, 20, 18, 10, 1.0], 0.66),
        ([(146, 36), (164, 56), (176, 76), (178, 92)], [12, 16, 12, 1.0], 0.6),
        ([(152, 60), (156, 84), (158, 108), (159, 128), (157, 140)], [10, 13, 11, 7, 1.0], 0.58),
    ):
        ribbon(pts, w, b)
    g = cv.group.copy()
    cv.group = None  # no line around the bangs: the hair / skin change draws the edge
    hair |= g
    face &= ~g
    # ring highlight across the skull
    ring = np.exp(-(((Y - (cy - 24) - 0.18 * (X - cx)) / 3.0) ** 2)) * np.clip(
        1 - np.abs(X - cx) / 40.0, 0, 1
    )
    cv.t[hair] = np.clip(cv.t[hair] + 0.26 * ring[hair], 0, 1)
    # cel-like bands on the hair, as in the back view
    hm = hair | ears
    q = np.round(np.nan_to_num(cv.t[hm]) * 5) / 5
    cv.t[hm] = 0.4 * cv.t[hm] + 0.6 * q
    out = cv.render()
    opaque = out[..., 3] > 0
    mats = cv.mats_lo
    hair_lo = cv_low(px, hm) & opaque
    face_lo = opaque & ~hair_lo & ((mats == SKIN) | (mats == EYE)) & (px.yy < 140)
    layers = {k: np.zeros_like(out) for k in ("hair_back", "coat", "face")}
    layers["hair_back"][hair_lo] = out[hair_lo]
    layers["face"][face_lo] = out[face_lo]
    rest = opaque & ~hair_lo & ~face_lo
    layers["coat"][rest] = out[rest]
    return out, layers
