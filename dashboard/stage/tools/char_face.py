"""Procedural face sprites for the Stage character (#341): eyes, mouths, brows, effects.

Drawn at 1x in code (offline, called by char_parts.py). Geometry is in portrait pixels of
the base sheet; char_parts.py stores each sprite relative to its anchor so it follows the
head of every pose sheet.
"""

import build_char as bc
import numpy as np

H = bc.hexrgb
C = lambda s: (H(s) * 255).astype(np.uint8)
LASH = C("#14152a")
LASH2 = C("#25243f")
SCL = C("#e6e8f3")
SCL_SH = C("#b9bdd8")
IRIS = [C("#1b2645"), C("#34507f"), C("#5a86bb"), C("#a9cbe9")]
PUPIL = C("#0f1428")
WHITE = C("#ffffff")
LID = C("#9a6270")  # skin line
LID_D = C("#5e3646")
SKIN_SH = C("#d1a09c")
MOUTH = C("#5c2f3d")
MOUTH_IN = C("#7a3448")
TONGUE = C("#c86a78")
TEETH = C("#fcece4")
BROW = C("#5f628d")
BROW_D = C("#3e3f66")
BLUSH = C("#e88f97")
# the face's own colours beyond the shared ramps (the palette in parts.json: char_parts.palette)
NAMED = {"LASH": "#14152a", "PUPIL": "#0f1428", "WHITE": "#ffffff", "MOUTH_IN": "#7a3448",
         "TONGUE": "#c86a78", "BLUSH": "#e88f97", "SWEAT_RIM": "#2b3a6e", "SWEAT": "#8fc3ee",
         "ANGER": "#e0414f", "SPARK": "#ffe9a8"}  # fmt: skip

# Eye geometry in sheet pixels (the 'base' sheet). side=-1: outer corner on the left.
EYE_GEOM = {
    "l": {
        "xo": 114,
        "xi": 127,
        "top": 95,
        "bot": 102,
        "side": -1,
        "rx": 3.6,
        "ry": 3.4,
        "icx": 120.5,
    },
    "r": {
        "xo": 165,
        "xi": 147,
        "top": 94,
        "bot": 101,
        "side": 1,
        "rx": 4.2,
        "ry": 3.4,
        "icx": 153.5,
    },
}
GAZE = {
    "c": (0, 0),
    "l": (-2, 0),
    "r": (2, 0),
    "u": (0, -1),
    "d": (0, 1),
    "ul": (-2, -1),
    "ur": (2, -1),
    "dl": (-2, 1),
    "dr": (2, 1),
}

# shape -> params: upper lid drop at the peak (px), inner drop, outer drop, curvature sign,
# lower lid raise, iris scale, lash thickness, extras
SHAPES = {
    "open": {"up": 0.0, "inner": 2.6, "outer": 1.6, "low": 0.0, "iris": 1.0, "lash": 2},
    "half": {"flat": 3.4, "low": 0.0, "iris": 1.0, "lash": 1},
    "down": {"curve": "lowered", "lash": 2},
    "wide": {"up": -1.0, "inner": 2.2, "outer": 1.2, "low": -0.6, "iris": 0.85, "lash": 2},
    "surprised": {
        "up": -1.6,
        "inner": 1.8,
        "outer": 1.0,
        "low": -1.0,
        "iris": 0.62,
        "lash": 1,
        "ring": True,
    },
    "sharp": {"up": 1.2, "inner": 4.4, "outer": 0.8, "low": -0.2, "iris": 0.8, "lash": 2},
    "soft": {"up": 0.6, "inner": 2.8, "outer": 1.8, "low": 2.0, "iris": 1.0, "lash": 2},
    "closed": {"curve": "down", "lash": 2},
    "happy": {"curve": "up", "lash": 2},
    "wince": {"curve": "wince", "lash": 2},
    # --- added for the expression sheet (05), art unify 3 (#341). The sprites above are #359's
    # and stay as they are; these draw a fuller iris (a second highlight, the lid's shadow on
    # the iris's top) toward the reference's eyes.
    # ジト目: the upper lid low and straight over the iris, the lower lid up a little
    "jito": {"flat": 2.4, "low": 0.7, "iris": 1.0, "lash": 2, "shadow": True},
    # 感動・うるうる: open, a bigger iris, three highlights, a wet lower lid
    "teary": {"up": -0.4, "inner": 2.2, "outer": 1.4, "low": 0.5, "iris": 1.12, "lash": 2,
              "hl2": 2, "tear": True, "shadow": True},
    # 企み: narrowed — the upper lid down, the lower lid well up, looking aside
    "scheme": {"up": 1.6, "inner": 3.2, "outer": 1.0, "low": 1.9, "iris": 0.95, "lash": 2,
               "gaze": (2, 0), "shadow": True},
    # 照れ: soft lids, the eyes turned away and down
    "shy": {"up": 0.8, "inner": 2.6, "outer": 2.0, "low": 1.3, "iris": 0.95, "lash": 2,
            "gaze": (-2, 1), "hl2": 1, "shadow": True},
    # 笑顔 (open): the lower lid pushed up in an arc (a crescent of iris)
    "arc": {"up": 0.2, "inner": 2.4, "outer": 1.8, "low": 2.8, "iris": 1.0, "lash": 2,
            "hl2": 1, "shadow": True},
    # 困惑: the inner corner raised (the lid slopes down outward), the iris a little smaller
    "puzzled": {"up": 0.6, "inner": 1.0, "outer": 2.8, "low": 0.6, "iris": 0.95, "lash": 2,
                "shadow": True},
    # 焦り: wide open, a tiny pupil in a white ring
    "panic": {"up": -1.4, "inner": 1.6, "outer": 1.0, "low": -0.8, "iris": 0.5, "lash": 1,
              "ring": True},
}  # fmt: skip
IRIS_SCALE = (1.16, 1.08)  # x, y (art unify 4)
NEW_SHAPES = ("jito", "teary", "scheme", "shy", "arc", "puzzled", "panic")


def _put(img, x, y, col):
    if 0 <= y < img.shape[0] and 0 <= x < img.shape[1]:
        img[y, x, :3] = col
        img[y, x, 3] = 255


def eye_sprite(which, shape, gaze="c", box=(0, 0, 278, 278)):
    """Returns (rgba full-sheet-size layer) of one eye drawn at the base geometry."""
    g = EYE_GEOM[which]
    p = SHAPES[shape]
    img = np.zeros((box[3], box[2], 4), np.uint8)
    xs = range(min(g["xo"], g["xi"]), max(g["xo"], g["xi"]) + 1)
    side = g["side"]

    def u_of(x):  # position 0 (outer) .. 1 (inner)
        return (x - g["xo"]) / (g["xi"] - g["xo"])

    top, bot = g["top"], g["bot"]
    if "curve" in p:
        kind = p["curve"]
        y0 = top + 3
        for x in xs:
            u = u_of(x)
            if kind == "lowered":
                y = y0 + 0.6 + 1.6 * np.sin(np.pi * min(1, u * 1.05))
            elif kind == "down":
                y = y0 + 2.2 * np.sin(np.pi * min(1, u * 1.05))
            elif kind == "up":
                y = y0 + 2.2 - 2.4 * np.sin(np.pi * u)
            else:  # wince: > <
                y = y0 + 1.5 + 2.0 * abs(u - 0.45) * (1 if u < 0.45 else -1) * -1
            yi = round(y)
            _put(img, x, yi, LASH)
            if p["lash"] > 1 and 0.08 < u < 0.75:
                _put(
                    img,
                    x,
                    yi,
                    LASH,
                )
                _put(img, x, yi - 1 if kind == "up" else yi + 1, LASH2 if kind != "down" else LASH)
        if kind == "lowered":
            # downcast: a thin crescent of iris under a lowered, downward-curved lid
            icx = g["icx"] + (-0.8 if which == "l" else 0)
            for x in xs:
                u = u_of(x)
                yl = round(y0 + 0.6 + 1.6 * np.sin(np.pi * min(1, u * 1.05)))
                if abs(x - icx) <= g["rx"] * 0.95:
                    _put(img, x, yl + 1, IRIS[1] if abs(x - icx) > 1 else PUPIL)
                    if abs(x - icx) <= g["rx"] * 0.7:
                        _put(img, x, yl + 2, IRIS[2])
                elif 0.1 < u < 0.9:
                    _put(img, x, yl + 1, SCL_SH)
                if 0.2 < u < 0.8:
                    _put(img, x, yl + 3, LID)
                # the lid itself (skin crease above)
                if 0.15 < u < 0.85:
                    _put(img, x, top + 1, LID)
            xo = g["xo"]
            _put(img, xo + side, top + 5, LASH)
            _put(img, xo + side * 2, top + 6, LASH2)
            return img
        # outer lash tip
        xo = g["xo"]
        if kind == "down":
            _put(img, xo + side * 1, top + 4, LASH)
            _put(img, xo + side * 1, top + 5, LASH2)
        elif kind == "up":
            _put(img, xo + side * 1, top + 5, LASH)
        else:
            _put(img, xo, top + 2, LASH)
        return img

    # upper lid boundary U(x) and lower lid L(x)
    def U(x):
        u = u_of(x)
        if "flat" in p:
            return top + p["flat"] + 0.3 * u
        if "downlid" in p:
            return top + p["downlid"] - 1.4 * np.sin(np.pi * u) + 0.6 * u
        # peak near 35 % from the outer corner
        prof = np.interp(u, [0, 0.35, 1], [p["outer"], 0, p["inner"]])
        return top + p["up"] + prof

    def L(x):
        u = u_of(x)
        base = bot - 0.6 * np.sin(np.pi * u) + 0.5 * u
        return base - p["low"] * np.sin(np.pi * u) ** 0.7

    gx, gy = GAZE[gaze]
    if "gaze" in p and gaze == "c":
        gx, gy = p["gaze"]
    icx = g["icx"] + gx * (1 if which == "r" else 0.8)
    icy = (top + bot) / 2 + 0.6 + gy
    # art unify 4: the iris a little bigger (sheets 03 / 05: it fills more of the eye's width)
    rx, ry = g["rx"] * p["iris"] * IRIS_SCALE[0], g["ry"] * p["iris"] * IRIS_SCALE[1]
    lash = p["lash"]
    # an eye open enough (5 px at the iris) gets the ring and the 2 x 2 pupil
    open_eye = L(round(icx)) - U(round(icx)) >= 5.0
    for x in xs:
        uy, ly = U(x), L(x)
        u = u_of(x)
        y_top = int(np.ceil(uy))
        y_bot = int(np.floor(ly))
        for y in range(y_top, y_bot + 1):
            dx_, dy_ = (x - icx) / max(rx, 0.5), (y - icy) / max(ry, 0.5)
            r2 = dx_ * dx_ + dy_ * dy_
            if r2 <= 1.0:
                t = (y - (icy - ry)) / (2 * ry)
                # art unify 4 (sheets 03 / 05): a dark ring round the iris, the iris light
                # toward the bottom, a 2 x 2 pupil a little above the middle
                col = IRIS[1] if t < 0.38 else IRIS[2] if t < 0.72 else IRIS[3]
                if open_eye and r2 > 0.58 and t < 0.8:
                    col = IRIS[0] if t < 0.5 else IRIS[1]
                pw = 1.0 if open_eye else 0.6  # a narrow eye: the pupil one pixel wide
                if abs(x - icx) <= pw and -1.3 <= y - icy <= 0.5 and p["iris"] > 0.7:
                    col = PUPIL
                if p.get("ring") and abs(x - icx) <= 0.6 and abs(y - icy) <= 0.6:
                    col = PUPIL
            else:
                col = SCL_SH if y == y_top else SCL
            _put(img, x, y, col)
        # lash line above the aperture
        th = lash + (1 if u < 0.3 and lash > 1 else 0)
        for k in range(1, th + 1):
            _put(img, x, y_top - k, LASH if k <= max(1, th - 1) or u < 0.5 else LASH2)
        # lower lid line (middle part only)
        if 0.15 < u < 0.85:
            _put(img, x, y_bot + 1, LID if p["low"] < 1 else LID_D)
        if p.get("crease") and 0.1 < u < 0.9:
            _put(img, x, round(top + 0.6 * np.sin(np.pi * u) * -1 + 1), LID)
    if p.get("shadow", True):
        # the lid's shadow on the iris's top row (IRIS[0] where the iris meets the lid)
        for x in xs:
            y = int(np.ceil(U(x)))
            if (
                (x - icx) ** 2 <= rx * rx
                and img[y, x, 3]
                and any((img[y, x, :3] == c).all() for c in IRIS[1:])
            ):
                _put(img, x, y, IRIS[0])
    if p.get("tear"):
        # a wet lower lid: the lid line light blue, a glint on it
        for x in xs:
            u = u_of(x)
            if 0.15 < u < 0.85:
                _put(img, x, int(np.floor(L(x))) + 1, C("#8fc3ee"))
        gx_ = round(icx + (2 if which == "l" else -2))
        _put(img, gx_, int(np.floor(L(gx_))) + 1, WHITE)
    # highlight
    if shape not in ("half",) and p["iris"] >= 0.6:
        hx, hy = round(icx - 1), round(icy - ry * 0.45)
        if p.get("shadow", True):
            # a narrow eye: the highlight on the iris's first row under the lid
            while (
                hy < img.shape[0] - 1
                and img[hy, hx, 3]
                and ((img[hy, hx, :3] == LASH).all() or (img[hy, hx, :3] == IRIS[0]).all())
            ):
                hy += 1
        if img[hy, hx, 3] and (img[hy, hx, :3] != LASH).any():
            _put(img, hx, hy, WHITE)
            # the main highlight two pixels wide (toward the outer corner) where the iris is
            qx = hx - 1 if which == "l" else hx + 1
            if (
                p["iris"] >= 0.8
                and img[hy, qx, 3]
                and any((img[hy, qx, :3] == c).all() for c in IRIS)
            ):
                _put(img, qx, hy, WHITE)
        if p.get("hl2", 1 if p["iris"] >= 0.8 else 0):
            # a second (and third) highlight low on the other side of the iris
            n_hl = p.get("hl2", 1)
            for k, (ddx, ddy) in enumerate(((1.6, 0.5), (-0.4, 0.2))[:n_hl]):
                qx, qy = round(icx + ddx), round(icy + ry * ddy)
                if img[qy, qx, 3] and (img[qy, qx, :3] != LASH).any():
                    _put(img, qx, qy, WHITE if k == 0 else IRIS[3])
    # outer corner flick
    xo = g["xo"]
    yo = int(np.ceil(U(xo)))
    _put(img, xo + side, yo - 2, LASH)
    if "downlid" in p:
        _put(img, xo + side, yo, LASH)
        _put(img, xo, yo + 1, LASH2)
    return img


# Mouths: ASCII sprites (12 x 6), origin at (129, 119) in the base sheet. 'M' line, 'm'
# inner, 't' tongue, 'w' teeth, 's' lip shade.
MOUTH_ORIGIN = (129, 119)
MOUTHS = {
    "neutral": [
        "............",
        "............",
        ".MMssssssMM.",
        "............",
        "............",
        "............",
    ],
    "smile": [
        "............",
        "............",
        ".M........M.",
        "..M......M..",
        "...MMMMMM...",
        "............",
    ],
    "grin": [
        "............",
        ".M........M.",
        "..MMMMMMMM..",
        "..Mwwwwwwm..",
        "...mmttmm...",
        "....MMMM....",
    ],
    "flat": [
        "............",
        "............",
        "............",
        "...MMMMMM...",
        "............",
        "............",
    ],
    "frown": [
        "............",
        "............",
        "....MMMM....",
        "...M....M...",
        "..M......M..",
        "............",
    ],
    "open": [
        "............",
        ".....MMM....",
        "....MmmmM...",
        "....MmtmM...",
        ".....MMM....",
        "............",
    ],
    "small_o": [
        "............",
        "............",
        ".....MM.....",
        "....MmmM....",
        ".....MM.....",
        "............",
    ],
    "wavy": [
        "............",
        "............",
        "...M..M.....",
        "..M.MM.MM...",
        ".........M..",
        "............",
    ],
    "pout": [
        "............",
        "............",
        ".....MMM....",
        "....M...M...",
        "............",
        "............",
    ],
    "talk": [
        "............",
        "............",
        "....MMMMM...",
        "....MmmmM...",
        ".....MMM....",
        "............",
    ],
    "exhale": [
        "............",
        "............",
        "....MMM.....",
        "...MmmmM....",
        "....MMM.....",
        "............",
    ],
    # --- added for the expression sheet (05), art unify 3 (#341)
    # 小開き: a little open, talking softly
    "small_open": [
        "............",
        "............",
        "....MMMM....",
        "....MmmM....",
        ".....MM.....",
        "............",
    ],
    # むっと: lips pressed, the lower lip pushed up a little
    "mutto": [
        "............",
        "............",
        "....MMMMM...",
        ".....sss....",
        "............",
        "............",
    ],
    # ニヤリ: a closed smile, one corner (her left) higher
    "smirk": [
        "............",
        "..........M.",
        "...M.....M..",
        "....MMMMM...",
        "............",
        "............",
    ],
    # への字: the corners down in a sharp bend
    "hen": [
        "............",
        "............",
        ".....MM.....",
        "....M..M....",
        "...M....M...",
        "..M......M..",
    ],
    # おどろき: open tall
    "gasp": [
        "............",
        ".....MMM....",
        "....MmmmM...",
        "....MmmmM...",
        "....MmtmM...",
        ".....MMM....",
    ],
    # --- art unify 5 (#341, sheet 05)
    # 大笑い: wide open, the upper teeth, the tongue (with `happy` / `arc` eyes)
    "laugh": [
        "............",
        ".MMMMMMMMMM.",
        ".MwwwwwwwwM.",
        ".MmmmmmmmmM.",
        "..MmmttmmM..",
        "...MMMMMM...",
    ],
    # ニコニコ: a small closed smile
    "niko": [
        "............",
        "............",
        "...M....M...",
        "....MMMM....",
        "............",
        "............",
    ],
}
NEW_MOUTHS = ("small_open", "mutto", "smirk", "hen", "gasp")
LATE_MOUTHS = ("laugh", "niko")  # art unify 5
MCOL = {"M": MOUTH, "m": MOUTH_IN, "t": TONGUE, "w": TEETH, "s": SKIN_SH}


def mouth_sprite(name):
    rows = MOUTHS[name]
    img = np.zeros((len(rows), len(rows[0]), 4), np.uint8)
    for y, line in enumerate(rows):
        for x, ch in enumerate(line):
            if ch in MCOL:
                img[y, x, :3] = MCOL[ch]
                img[y, x, 3] = 255
    return img


# Brows: (inner dy, outer dy) relative to the neutral line; drawn over the bangs.
BROW_GEOM = {"l": {"xo": 113, "xi": 125, "y": 89}, "r": {"xo": 166, "xi": 149, "y": 88}}
BROWS = {
    "neutral": (0, 0),
    "up": (-2, -1),
    "worried": (-2, 1),
    "angry": (2, -1),
    "soft": (0, 1),
    "focus": (1, 0),
}


def brow_sprite(which, name):
    g = BROW_GEOM[which]
    di, do = BROWS[name]
    img = np.zeros((278, 278, 4), np.uint8)
    n = abs(g["xi"] - g["xo"])
    for i in range(n + 1):
        u = i / n  # 0 outer .. 1 inner
        x = g["xo"] + (1 if g["xi"] > g["xo"] else -1) * i
        y = (
            g["y"]
            + do
            + (di - do) * u
            - 1.0 * np.sin(np.pi * u) * (1 if name != "worried" else 0.3)
        )
        yi = round(y)
        _put(img, x, yi, BROW_D if 0.2 < u < 0.85 else BROW)
        if 0.35 < u < 0.8:
            _put(img, x, yi + 1, BROW)
    return img


def fx_sprites():
    """Small effect sprites: one cheek of blush, the sweat drop, a surprise mark."""
    out = {}
    blush = np.zeros((4, 9, 4), np.uint8)
    for k in range(3):
        for i in range(3):
            x = 1 + k * 3 - i + 2
            if 0 <= x < 9:
                blush[1 + i - 0, x, :3] = BLUSH
                blush[1 + i - 0, x, 3] = 255
    out["blush"] = blush
    drop = ["....o....", "...ooo...", "...oLo...", "..oLLLo..", "..oLWLo..", ".oLLWLLo.",
            ".oLLLLLo.", ".oLLLLLo.", "..oLLLo..", "...ooo..."]  # fmt: skip
    cmap = {"o": C("#2b3a6e"), "L": C("#8fc3ee"), "W": WHITE}
    sweat = np.zeros((10, 9, 4), np.uint8)
    for y, line in enumerate(drop):
        for x, ch in enumerate(line):
            if ch in cmap:
                sweat[y, x, :3] = cmap[ch]
                sweat[y, x, 3] = 255
    out["sweat"] = sweat
    # surprise mark: three short strokes (light with a dark outline, readable on any background)
    mark = [
        "..k.....k.....",
        ".kok...kok....",
        ".kok...kok..k.",
        "..kok..kok.kok",
        "..kok...k.kok.",
        "...k.....kok..",
        "..........k...",
    ]
    spark = np.zeros((len(mark), len(mark[0]), 4), np.uint8)
    for y, line in enumerate(mark):
        for x, ch in enumerate(line):
            if ch in "ok":
                spark[y, x, :3] = C("#fcece4") if ch == "o" else C("#14152a")
                spark[y, x, 3] = 255
    out["shock"] = spark
    return out
