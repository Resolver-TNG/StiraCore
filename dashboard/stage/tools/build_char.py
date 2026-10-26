"""Build the pixel-art character sheet for Stage (#341) from the source icon.

Offline tool (not used at runtime). Needs numpy, scipy, opencv-python and Pillow:

    python3 dashboard/stage/tools/build_char.py

Inputs : assets/source/resolver-icon.jpg (circle crop of the owner's icon) and
         assets/source/resolver-mask.png (subject mask, see subject_mask.swift).
Outputs: assets/char/base.png   278x278 palette-quantised character
         assets/char/rig.png    R=head G=hair sway B=breath (layer weights)
         assets/char/rig2.png   R=left ear G=right ear B=arm and mug
         assets/char/face.png   atlas of expression overlays (eyes / mouth / extras)
         assets/char/char.json  boxes of the overlays, layer table, mug anchor
"""

from __future__ import annotations

import json
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw
from scipy import ndimage as ndi

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "assets" / "source"
OUT = ROOT / "assets" / "char"
F = 4.0  # source pixels per portrait pixel

BAYER4 = np.array([[0, 8, 2, 10], [12, 4, 14, 6], [3, 11, 1, 9], [15, 7, 13, 5]]) / 16 - 0.5

# Hand-picked ramps (dark -> light), hue-shifted toward violet in the shadows.
RAMPS = {
    "hair": ["#25243f", "#3e3f66", "#5f628d", "#8b8fb5", "#b9bdd8", "#e6e8f3"],
    "skin": ["#5e3646", "#9a6270", "#d1a09c", "#efcfc5", "#fcece4"],
    "coat": ["#3b3247", "#665a70", "#988c98", "#c9bfc0", "#ece5de"],
    "navy": ["#0f1124", "#1a1f3c", "#283158", "#3a4778"],
    "mug": ["#0a0a12", "#191a26", "#2c2d40", "#4f526e"],
    "eye": ["#1b2645", "#34507f", "#5a86bb", "#a9cbe9"],
}
MATS = ["hair", "skin", "coat", "navy", "mug", "eye"]
LINE = {
    "hair": "#1d2036",
    "skin": "#5c2f3d",
    "coat": "#2a2533",
    "navy": "#06070f",
    "mug": "#050509",
    "eye": "#0f1428",
}
OUTLINE = "#0b0a16"
SCLERA = "#e6e8f3"
WHITE = "#ffffff"
LASH = "#14152a"
MOUTH = "#5c2f3d"
MOUTH_IN = "#7a3448"
TONGUE = "#c86a78"
BLUSH = "#e88f97"


def hexrgb(value: str) -> np.ndarray:
    return np.array([int(value[i : i + 2], 16) for i in (1, 3, 5)], dtype=np.float32) / 255


def poly(size: tuple[int, int], pts: list[tuple[float, float]]) -> np.ndarray:
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).polygon(pts, fill=1)
    return np.asarray(mask).astype(bool)


# Region polygons in portrait pixels (hand-authored against the 278 px grid).
FACE = [
    (117, 86),
    (161, 84),
    (163, 104),
    (158, 124),
    (160, 140),
    (128, 140),
    (120, 122),
    (115, 104),
]
HAND = [(99, 172), (113, 172), (114, 212), (104, 222), (96, 232), (82, 236), (77, 226), (86, 206),
        (88, 186), (94, 176)]  # fmt: skip
MUG = [(110, 170), (144, 170), (144, 212), (139, 217), (115, 217), (110, 212)]
EYE_L = [(113, 93), (128, 93), (128, 102), (113, 102)]
EYE_R = [(146, 92), (166, 92), (166, 102), (146, 102)]
EAR_L = [(80, 12), (126, 34), (116, 46), (101, 63), (91, 54)]
EAR_R = [(164, 0), (178, 10), (189, 52), (177, 55), (160, 41), (149, 34), (157, 16)]
EAR_L_PIVOT = (110, 50)
EAR_R_PIVOT = (168, 44)
HEAD = [(78, 8), (130, 30), (150, 22), (165, 0), (192, 50), (200, 110), (206, 150), (196, 165),
        (166, 150), (160, 138), (128, 140), (110, 160), (86, 170), (80, 120), (88, 60)]  # fmt: skip

# Expression overlay boxes (x, y, w, h) in portrait pixels.
EYE_BOX = (110, 89, 58, 14)
MOUTH_BOX = (126, 117, 22, 11)
BLUSH_BOX = (113, 102, 52, 9)
SWEAT_BOX = (166, 78, 9, 13)
EYE_VARIANTS = ["open", "closed", "half", "up", "happy", "wide", "down"]
MOUTH_VARIANTS = ["neutral", "smile", "grin", "flat", "frown", "open"]


def load() -> tuple[np.ndarray, np.ndarray]:
    src = np.asarray(Image.open(SRC / "resolver-icon.jpg").convert("RGB")).astype(np.float32) / 255
    alpha = np.asarray(Image.open(SRC / "resolver-mask.png").convert("L")).astype(np.float32) / 255
    return src, alpha


def extend_circle_cut(rgb: np.ndarray, sil: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """The icon is a circle crop: continue the lower silhouette straight down instead.

    Each new pixel (between the circle edge and the fitted silhouette line) copies the
    nearest original pixel *up along the silhouette line*. The coat's folds and the hair
    strands run roughly parallel to that line, so they continue down instead of turning
    into horizontal streaks (as a row-wise stretch of the edge did). The source is softened
    first so that pixel noise does not become thin stripes; the new part is shaded slightly
    darker toward the outer edge.
    """
    h, w = sil.shape
    cx = cy = (w - 1) / 2
    r = w / 2
    rgb, sil = rgb.copy(), sil.copy()
    src_sil = sil.copy()
    # copy from a softened source: the folds continue, single-pixel noise does not streak
    src_rgb = ndi.gaussian_filter(rgb, (0.8, 1.6, 0))
    for side in (-1, 1):
        edges = []
        for y in range(150, h):
            xs = np.where(src_sil[y])[0]
            if not len(xs):
                continue
            x = xs.min() if side < 0 else xs.max()
            if np.hypot(x - cx, y - cy) < r - 3:
                edges.append((y, x))
        top = [(y, x) for y, x in edges if 170 <= y <= 230]
        if len(top) < 6:
            continue
        ys, xs = np.array(top).T
        slope, icpt = np.polyfit(ys, xs, 1)
        for y in range(200, h):
            xs = np.where(src_sil[y])[0]
            if not len(xs):
                continue
            target = round(slope * y + icpt)
            x0 = xs.min() if side < 0 else xs.max()
            new = np.arange(target, x0) if side < 0 else np.arange(x0 + 1, target + 1)
            for x in new[(new >= 0) & (new < w)]:
                for t in range(1, 120):
                    yy, xx = y - t, round(float(x - slope * t))
                    if yy < 0 or not 0 <= xx < w:
                        break
                    # skip the outermost original pixel (the old outline)
                    if src_sil[yy, xx] and src_sil[yy, xx - side]:
                        depth = abs(x - x0) / max(abs(target - x0), 1)
                        rgb[y, x] = src_rgb[yy, xx] * (1.0 - 0.18 * depth)
                        sil[y, x] = True
                        break
    return rgb, sil


def build() -> dict:
    src, alpha = load()
    h0, w0 = alpha.shape
    th, tw = int(h0 / F), int(w0 / F)
    size = (tw, th)
    smooth = cv2.bilateralFilter((src * 255).astype(np.uint8), 9, 30, 5).astype(np.float32) / 255
    lum = cv2.cvtColor(src, cv2.COLOR_RGB2LAB)[..., 0]
    ink = np.clip((cv2.GaussianBlur(lum, (0, 0), 3.5) - lum - 4) / 14, 0, 1) * (alpha > 0.5)
    rs = cv2.resize(smooth * alpha[..., None], size, interpolation=cv2.INTER_AREA)
    a_s = cv2.resize(alpha, size, interpolation=cv2.INTER_AREA)
    col = np.clip(rs / np.maximum(a_s[..., None], 1e-3), 0, 1)
    ink_s = cv2.resize(ink, size, interpolation=cv2.INTER_AREA)
    sil = a_s > 0.5
    sil = ndi.binary_opening(sil) | (sil & ndi.binary_erosion(sil))
    before = sil.copy()
    col, sil = extend_circle_cut(col, sil)
    filled = sil & ~before  # the ramps are fitted on the original pixels only
    lab = cv2.cvtColor(col.astype(np.float32), cv2.COLOR_RGB2LAB)
    lc, ac, bc = lab[..., 0], lab[..., 1], lab[..., 2]
    yy, xx = np.mgrid[0:th, 0:tw]
    face, hand, mug = poly(size, FACE), poly(size, HAND), poly(size, MUG)
    eyes = poly(size, EYE_L) | poly(size, EYE_R)

    mat = np.zeros((th, tw), np.uint8)
    mat[face & (lc > 55) & ((bc > 3.3) | (ac > 1.5))] = 1
    torso = (yy > 138) & ~face
    navy = (lc < 45) & (bc < -3)
    mat[torso & navy] = 3
    mat[torso & ~navy & (bc > 2.8) & (ac < 1.2)] = 2
    mat[hand & (lc > 50) & (ac > 0.5)] = 1
    counts = np.stack([ndi.uniform_filter((mat == k).astype(float), 3) for k in range(6)])
    mat = counts.argmax(0).astype(np.uint8)
    mat[mug & (lc < 40)] = 4
    mat[eyes & (lc < 72) & (bc < 0.5)] = 5

    dith = BAYER4[yy % 4, xx % 4]
    out = np.zeros((th, tw, 3), np.float32)
    for k, name in enumerate(MATS):
        m = (mat == k) & sil
        if not m.any():
            continue
        ref = m & ~filled
        lo, hi = np.percentile(lc[ref if ref.any() else m], [3, 98])
        t = np.clip((lc - lo) / max(hi - lo, 1), 0, 1) ** 0.9
        ramp = np.array([hexrgb(c) for c in RAMPS[name]])
        n = len(ramp)
        idx = np.clip(np.floor(t * (n - 1) + 0.5 + dith * 0.55), 0, n - 1).astype(int)
        out[m] = ramp[idx[m]]
    line = (ink_s > 0.20) & sil
    labels, count = ndi.label(line, structure=np.ones((3, 3)))
    sizes = ndi.sum(line, labels, range(1, count + 1))
    line &= ~np.isin(labels, np.where(sizes < 3)[0] + 1)
    for k, name in enumerate(MATS):
        out[line & (mat == k)] = hexrgb(LINE[name])
    edge = sil & ~ndi.binary_erosion(sil)
    out[edge] = hexrgb(OUTLINE)

    rgba = np.dstack([out * 255, sil * 255]).astype(np.uint8)
    rgba[~sil] = 0
    # hair rig: parts + tip segments are cut out into hair*.png/json (see build_hair.py)
    from build_hair import split_hair

    rgba, mat = split_hair(rgba, mat, sil, OUT, lab=lab)
    OUT.mkdir(parents=True, exist_ok=True)
    Image.fromarray(rgba).save(OUT / "base.png", optimize=True)

    rig_weights(size, sil, mat)
    face_atlas(rgba, mat)
    meta = {
        "size": [tw, th],
        "eyeBox": EYE_BOX,
        "mouthBox": MOUTH_BOX,
        "blushBox": BLUSH_BOX,
        "sweatBox": SWEAT_BOX,
        "eyeVariants": EYE_VARIANTS,
        "mouthVariants": MOUTH_VARIANTS,
        "mugTop": [127, 168],
        "layers": [
            {"id": 0, "name": "body", "z": 1.0},
            {"id": 1, "name": "head", "z": 1.12},
            {"id": 2, "name": "ear_l", "z": 1.06},
            {"id": 3, "name": "ear_r", "z": 1.06},
            {"id": 4, "name": "arm_mug", "z": 1.2},
            {"id": 5, "name": "hair", "z": 1.0, "rig": "hair.json"},
        ],
        "colours": len(np.unique(rgba[sil].reshape(-1, 4), axis=0)),
    }
    (OUT / "char.json").write_text(json.dumps(meta, indent=1) + "\n", encoding="utf-8")
    return meta


def rig_weights(size, sil, mat) -> None:
    tw, th = size
    yy = np.mgrid[0:th, 0:tw][0]
    head = poly(size, HEAD).astype(np.float32)
    head = np.clip(ndi.gaussian_filter(head, 2.0) * 1.6, 0, 1)
    head[poly(size, FACE) & (yy < 132)] = 1.0
    hair = (mat == 0) & sil
    sway = np.clip((yy - 118) / 90.0, 0, 1) * hair
    sway = np.clip(ndi.gaussian_filter(sway.astype(np.float32), 1.5), 0, 1)
    arm = poly(size, HAND) | poly(size, MUG)
    arm_w = np.clip(ndi.gaussian_filter(ndi.binary_dilation(arm, iterations=2).astype(float), 1.5)
                    * 1.4, 0, 1)  # fmt: skip
    breath = np.clip((196 - yy) / 40.0, 0, 1)
    breath = np.maximum(breath, arm_w)
    ear_l = ear_weight(size, EAR_L, EAR_L_PIVOT)
    ear_r = ear_weight(size, EAR_R, EAR_R_PIVOT)

    def to8(a):
        return (np.clip(a, 0, 1) * 255).astype(np.uint8)

    # RGB only: data in an alpha channel may be lost to premultiplication when decoded.
    Image.fromarray(np.dstack([to8(head), to8(sway), to8(breath)])).save(
        OUT / "rig.png", optimize=True
    )
    Image.fromarray(np.dstack([to8(ear_l), to8(ear_r), to8(arm_w)])).save(
        OUT / "rig2.png", optimize=True
    )


def ear_weight(size, pts, pivot) -> np.ndarray:
    tw, th = size
    yy, xx = np.mgrid[0:th, 0:tw]
    m = ndi.binary_dilation(poly(size, pts), iterations=1)
    tip = max(pts, key=lambda p: -p[1])
    span = np.hypot(tip[0] - pivot[0], tip[1] - pivot[1])
    w = np.clip(np.hypot(xx - pivot[0], yy - pivot[1]) / span, 0, 1)
    return (w * m).astype(np.float32)


# --- expression overlays -------------------------------------------------------------------


def put(img: np.ndarray, pts, colour: str) -> None:
    rgb = (hexrgb(colour) * 255).astype(np.uint8)
    for x, y in pts:
        if 0 <= y < img.shape[0] and 0 <= x < img.shape[1]:
            img[y, x, :3] = rgb
            img[y, x, 3] = 255


def front_hair(base, mat, core) -> np.ndarray:
    """Light hair pixels over the eye (bangs) - repainted on top of every eye variant."""
    x0, y0, x1, y1 = core
    keep = np.zeros(base.shape[:2], bool)
    lum = base[..., :3].astype(int).sum(2)
    region = np.zeros_like(keep)
    region[y0 : y1 + 1, x0 : x1 + 1] = True
    hair_lum = int((hexrgb(RAMPS["hair"][2]) * 255).sum())
    keep[region & (mat == 0) & (lum >= hair_lum)] = True
    labels, _ = ndi.label(keep, structure=np.ones((3, 3)))
    border = set(labels[y0, x0 : x1 + 1]) | set(labels[y1, x0 : x1 + 1])
    border |= set(labels[y0 : y1 + 1, x0]) | set(labels[y0 : y1 + 1, x1])
    border.discard(0)
    return np.isin(labels, list(border))


def fill_skin(img, core, rows=None) -> None:
    x0, y0, x1, y1 = core
    skin = (hexrgb(RAMPS["skin"][4]) * 255).astype(np.uint8)
    for y in rows if rows is not None else range(y0, y1 + 1):
        img[y, x0 : x1 + 1, :3] = skin
        img[y, x0 : x1 + 1, 3] = 255


# Eye geometry (portrait pixels): core box, lash bottom row, closed-lid line span.
EYES = [
    {"core": (113, 93, 126, 101), "lash_y": 95, "span": (114, 125), "cx": 120},
    {"core": (146, 93, 165, 101), "lash_y": 96, "span": (146, 164), "cx": 154},
]


def eye_variant(base, mat, variant: str) -> np.ndarray:
    img = base.copy()
    for e in EYES:
        x0, y0, x1, y1 = e["core"]
        ly = e["lash_y"]
        hair = front_hair(base, mat, e["core"])
        src = base.copy()
        if variant == "open":
            put(img, [(e["cx"] - 2, ly + 2)], WHITE)
        elif variant in ("half", "down"):
            k = 2
            for y in range(y1, y0 - 1, -1):
                if y - k >= y0:
                    img[y, x0 : x1 + 1] = (
                        src[y - k, x0 : x1 + 1] if y <= ly + k else img[y, x0 : x1 + 1]
                    )
            fill_skin(img, e["core"], range(y0, y0 + k))
            if variant == "down":
                for y in range(y1, ly + k, -1):
                    row = src[y - 1, x0 : x1 + 1]
                    eye_px = mat[y - 1, x0 : x1 + 1] == 5
                    img[y, x0 : x1 + 1][eye_px] = row[eye_px]
        elif variant == "up":
            for y in range(ly + 1, y1):
                row = src[y + 1, x0 : x1 + 1]
                eye_px = mat[y + 1, x0 : x1 + 1] == 5
                img[y, x0 : x1 + 1][eye_px] = row[eye_px]
            sclera = mat[y1 - 2, x0 : x1 + 1] == 5
            img[y1 - 2, x0 : x1 + 1][sclera] = [*(hexrgb(SCLERA) * 255).astype(np.uint8), 255]
            put(img, [(e["cx"] - 1, ly + 1)], WHITE)
        elif variant == "wide":
            for y in range(y0, ly):
                img[y, x0 : x1 + 1] = src[y + 1, x0 : x1 + 1]
            row = mat[ly + 1, x0 : x1 + 1] == 5
            lash_row = img[ly, x0 : x1 + 1]
            lash_row[row] = [*(hexrgb(SCLERA) * 255).astype(np.uint8), 255]
            put(img, [(e["cx"], ly + 2), (e["cx"] - 1, ly + 2)], RAMPS["eye"][2])
            put(img, [(e["cx"] - 2, ly + 1)], WHITE)
        elif variant in ("closed", "happy"):
            fill_skin(img, e["core"])
            xa, xb = e["span"]
            y = ly + 3
            n = xb - xa
            mid = range(xa + 2, xb - 1)
            core_mid = range(xa + n // 4, xb - n // 4 + 1)
            if variant == "closed":
                pts = [(x, y) for x in (xa - 1, xa, xa + 1, xb - 1, xb, xb + 1)]
                pts += [(x, y + 1) for x in mid] + [(x, y + 2) for x in core_mid]
                pts += [(xb + 2, y - 1)]
                put(img, pts, LASH)
            else:
                pts = [(x, y + 1) for x in (xa, xa + 1, xb - 1, xb)]
                pts += [(x, y) for x in mid] + [(x, y - 1) for x in core_mid]
                put(img, pts, LASH)
        img[hair] = base[hair]
    return img


def mouth_variant(base, variant: str) -> np.ndarray:
    img = base.copy()
    if variant == "neutral":
        return img
    skin = RAMPS["skin"][4]
    put(img, [(x, y) for y in range(119, 127) for x in range(128, 146)], skin)
    if variant == "smile":
        put(img, [(130, 121), (131, 122)] + [(x, 123) for x in range(132, 141)]
            + [(141, 122), (142, 121)], MOUTH)  # fmt: skip
    elif variant == "grin":
        put(img, [(x, 122) for x in range(131, 142)], MOUTH)
        put(img, [(x, 123) for x in range(132, 141)] + [(x, 124) for x in range(133, 140)],
            MOUTH_IN)  # fmt: skip
        put(img, [(x, 124) for x in range(135, 138)], TONGUE)
        put(img, [(x, 125) for x in range(134, 139)] + [(130, 121), (142, 121)], MOUTH)
    elif variant == "flat":
        put(img, [(x, 123) for x in range(132, 141)], MOUTH)
    elif variant == "frown":
        put(img, [(131, 124), (132, 123)] + [(x, 122) for x in range(133, 140)]
            + [(140, 123), (141, 124)], MOUTH)  # fmt: skip
    elif variant == "open":
        put(img, [(135, 121), (136, 121), (137, 121), (134, 122), (138, 122), (134, 123),
                  (138, 123), (135, 124), (136, 124), (137, 124)], MOUTH)  # fmt: skip
        put(img, [(135, 122), (136, 122), (137, 122), (135, 123), (137, 123)], MOUTH_IN)
        put(img, [(136, 123)], TONGUE)
    return img


def crop(img, box) -> np.ndarray:
    x, y, w, h = box
    return img[y : y + h, x : x + w]


def face_atlas(base: np.ndarray, mat: np.ndarray) -> None:
    ew, eh = EYE_BOX[2], EYE_BOX[3]
    mw, mh = MOUTH_BOX[2], MOUTH_BOX[3]
    width = 64
    height = eh * len(EYE_VARIANTS) + mh * len(MOUTH_VARIANTS) + BLUSH_BOX[3] + SWEAT_BOX[3]
    atlas = np.zeros((height, width, 4), np.uint8)
    row = 0
    for v in EYE_VARIANTS:
        atlas[row : row + eh, :ew] = crop(eye_variant(base, mat, v), EYE_BOX)
        row += eh
    for v in MOUTH_VARIANTS:
        atlas[row : row + mh, :mw] = crop(mouth_variant(base, v), MOUTH_BOX)
        row += mh
    blush = np.zeros((BLUSH_BOX[3], BLUSH_BOX[2], 4), np.uint8)
    for cx in (9, 41):
        for k in range(3):
            for i in range(3):
                x = cx + k * 3 - i
                if 0 <= x < BLUSH_BOX[2]:
                    blush[4 + i, x] = [*(hexrgb(BLUSH) * 255).astype(np.uint8), 255]
    atlas[row : row + BLUSH_BOX[3], : BLUSH_BOX[2]] = blush
    row += BLUSH_BOX[3]
    sweat = np.zeros((SWEAT_BOX[3], SWEAT_BOX[2], 4), np.uint8)
    drop = [
        "....o....",
        "...ooo...",
        "...oLo...",
        "..oLLLo..",
        "..oLWLo..",
        ".oLLWLLo.",
        ".oLLLLLo.",
        ".oLLLLLo.",
        "..oLLLo..",
        "...ooo...",
    ]
    cmap = {"o": "#2b3a6e", "L": "#8fc3ee", "W": WHITE}
    for y, line in enumerate(drop):
        for x, ch in enumerate(line):
            if ch in cmap:
                sweat[y + 1, x] = [*(hexrgb(cmap[ch]) * 255).astype(np.uint8), 255]
    atlas[row : row + SWEAT_BOX[3], : SWEAT_BOX[2]] = sweat
    Image.fromarray(atlas).save(OUT / "face.png", optimize=True)


if __name__ == "__main__":
    info = build()
    print(json.dumps({"size": info["size"], "colours": info["colours"]}))
    # layered parts, poses, faces, arms and the back view for the motions (#341)
    import char_parts

    print(json.dumps(char_parts.build()))
