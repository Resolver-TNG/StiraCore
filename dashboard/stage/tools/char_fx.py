"""Face effects of the Stage character for the motion layer (#341, art unify 3), after the
owner's expression sheet (05): blush, sweat, the blue lines of a fluster, the anger mark,
sparkles, a tear. Their own files (``assets/char/fx.png`` + ``fx.json``); the parts atlas'
three old effects (``parts.json`` ``fx``: blush, sweat, shock) stay as they are. The asset
contract (names, anchors, slots, meanings) is in docs/stage.md, "Asset contract: face fx".

Every sprite is drawn in code in the character's palette (``parts.json`` ``palette``) as
small pixel shapes: no symbol from a font, no icon. Each entry is placed from a sheet anchor
(``anchor``: eye_l, eye_r, mouth ...; the same point every pose sheet moves) with ``ox`` /
``oy``, and names the layer slot it belongs in (``slot``: ``blush`` = on the face under the
bangs and over the eyes' layer, so it keeps off the eyes itself; ``fx`` = on top of everything).
"""

from __future__ import annotations

import json
from pathlib import Path

import build_char as bc
import numpy as np

H = lambda s: (bc.hexrgb(s) * 255).round().astype(np.uint8)
# colours (all in the palette: the ramps, the lines and char_face.NAMED)
COL = {
    "P": H("#e88f97"),  # blush
    "h": H("#c86a78"),  # blush hatch (the tongue's rose)
    "p": H("#d1a09c"),  # skin2: the blush's soft edge
    "q": H("#efcfc5"),  # skin3
    "o": H("#2b3a6e"),  # sweat rim
    "L": H("#8fc3ee"),  # sweat / tear
    "W": H("#ffffff"),
    "b": H("#5a86bb"),  # iris blue: the fluster's lines
    "d": H("#34507f"),  # iris dark blue
    "R": H("#e0414f"),  # anger red
    "r": H("#5c2f3d"),  # the skin line: the anger mark's rim
    "Y": H("#ffe9a8"),  # sparkle warm white
    "s": H("#e6e8f3"),  # hair5: sparkle cool white
}

# ASCII sprites ('.' clear); the anchor offset (ox, oy) of the sprite's top left is set below
SPRITES = {
    # 照れ: a soft pink patch with three diagonal strokes (per cheek)
    "blush.soft": [
        "..ppppppp..",
        ".pPPPPPPPp.",
        "pPPhPPhPPhp",
        ".pPhPPhPPp.",
        "..ppppppp..",
    ],
    # 照れ (strong) / 怒り: redder and wider, more strokes
    "blush.strong": [
        "...ppppppp...",
        ".pPPPPPPPPPp.",
        "pPhPPhPPhPPhp",
        "pPhPhPhPhPhPp",
        ".pPPPPPPPPPp.",
        "...ppppppp...",
    ],
    # 照れ (strong): a faint band over the nose between the cheeks
    "blush.nose": [
        ".ppppppppp.",
        "pPpPpPpPpPp",
        ".ppppppppp.",
    ],
    # 困惑: one drop at the temple
    "sweat.drop": [
        "....o....",
        "...oLo...",
        "...oLo...",
        "..oLLLo..",
        "..oLWLo..",
        ".oLLWLLo.",
        ".oLLLLLo.",
        ".oLLLLLo.",
        "..oLLLo..",
        "...ooo...",
    ],
    # 焦り: three small drops flying off the head
    "sweat.fly": [
        "......o.........",
        ".....oLo........",
        ".....oLo.....o..",
        "....oLWLo...oLo.",
        "....oLLLo...oWo.",
        ".....ooo.....o..",
        "................",
        "..o.............",
        ".oLo............",
        ".oWo............",
        "..o.............",
    ],
    # 怒り: the vein mark (four bent strokes round a cross), red with a dark rim
    "anger": [
        ".....rr.rr.....",
        "....rRRrRRr....",
        "....rRr.rRr....",
        ".rr.rRr.rRr.rr.",
        "rRRrrRr.rRrrRRr",
        "rRRRRr...rRRRRr",
        ".rrrr.....rrrr.",
        "...............",
        ".rrrr.....rrrr.",
        "rRRRRr...rRRRRr",
        "rRRrrRr.rRrrRRr",
        ".rr.rRr.rRr.rr.",
        "....rRr.rRr....",
        "....rRRrRRr....",
        ".....rr.rr.....",
    ],
    # 感動: a tear on the lower lid running down the cheek (per eye)
    "tear": [
        ".L.",
        ".L.",
        "LWL",
        "LLL",
        ".L.",
    ],
}

# sparkles (感動・ドヤ): four-point stars around the head, three frames to twinkle
STAR_BIG = ["...Y...", "...Y...", "..YsY..", "YYsWsYY", "..YsY..", "...Y...", "...Y..."]
STAR_MID = ["..Y..", "..Y..", "YYWYY", "..Y..", "..Y.."]
STAR_SMALL = [".Y.", "YWY", ".Y."]
STAR_DOT = ["W"]
# star positions in the sheet (base pose) and size per frame
SPARKLES = [((70, 46), ("big", "small", "mid")), ((206, 64), ("mid", "big", "small")),
            ((196, 22), ("small", "mid", "big")), ((84, 18), ("dot", "small", "dot")),
            ((222, 98), ("dot", "dot", "small"))]  # fmt: skip
STARS = {"big": STAR_BIG, "mid": STAR_MID, "small": STAR_SMALL, "dot": STAR_DOT}

# where each sprite goes: (anchor, x, y of the sprite's centre in the base sheet, slot)
PLACE = {
    "blush.soft.l": ("eye_l", 119, 108, "blush"),
    "blush.soft.r": ("eye_r", 157, 107, "blush"),
    "blush.strong.l": ("eye_l", 119, 108, "blush"),
    "blush.strong.r": ("eye_r", 157, 107, "blush"),
    "blush.nose": ("mouth", 138, 106, "blush"),
    "sweat.drop": ("eye_r", 173, 84, "fx"),
    "sweat.fly": ("eye_l", 82, 70, "fx"),
    "anger": ("eye_r", 186, 40, "fx"),
    "tear.l": ("eye_l", 125, 107, "blush"),
    "tear.r": ("eye_r", 151, 106, "blush"),
    "gloom": ("eye_l", 139, 96, "blush"),
}
ANCHORS = {"eye_l": (120.5, 98.0), "eye_r": (156.0, 97.0), "mouth": (135.0, 121.5)}
# the eyes (base sheet): the fluster's lines keep off them
EYES = [(120.5, 97.5, 11.0, 7.5), (156.0, 96.5, 13.0, 7.5)]


def _ascii(rows):
    h, w = len(rows), len(rows[0])
    img = np.zeros((h, w, 4), np.uint8)
    for y, line in enumerate(rows):
        for x, ch in enumerate(line):
            if ch in COL:
                img[y, x, :3] = COL[ch]
                img[y, x, 3] = 255
    return img


def gloom(skin=None):
    """焦り (05): blue vertical lines over the upper face, longer in the middle, ending in a
    dither, off the eyes (they are drawn before this layer) and only on her skin (`skin`: the
    face part's skin at rest; the crown and the bangs keep their hair)."""
    w, h = 62, 22
    cx0, cy0 = 139 - w // 2, 96 - h // 2  # the sprite's top left in the base sheet
    img = np.zeros((h, w, 4), np.uint8)
    rng = np.random.default_rng(7)
    for x in range(1, w - 1, 2):
        sx = cx0 + x
        mid = 1 - abs(x - w / 2) / (w / 2)
        length = int(6 + 11 * mid + rng.integers(-2, 3))
        for y in range(length):
            sy = cy0 + y
            if any(((sx - ex) / rx) ** 2 + ((sy - ey) / ry) ** 2 <= 1 for ex, ey, rx, ry in EYES):
                continue
            if skin is not None and not skin[sy, sx]:
                continue
            if y >= length - 3 and (y + x) % 2:
                continue  # the end of a line dithers out
            img[y, x, :3] = COL["b"] if y < length - 4 else COL["L"]
            img[y, x, 3] = 255
    return img


def sparkle_frame(k):
    """One twinkle frame: the stars round the head (sheet coordinates of the base pose)."""
    xs = [p[0] for p, _ in SPARKLES]
    ys = [p[1] for p, _ in SPARKLES]
    x0, y0 = min(xs) - 4, min(ys) - 4
    img = np.zeros((max(ys) - y0 + 5, max(xs) - x0 + 5, 4), np.uint8)
    for (px, py), sizes in SPARKLES:
        star = _ascii(STARS[sizes[k]])
        sh, sw = star.shape[:2]
        ty, tx = py - y0 - sh // 2, px - x0 - sw // 2
        m = star[..., 3] > 0
        img[ty : ty + sh, tx : tx + sw][m] = star[m]
    return img, x0, y0


def build(out_dir: Path, parts=None) -> dict:
    items = []  # (name, img, anchor, ox, oy, slot)
    skin = None
    if parts is not None:
        b = parts.base_imgs
        skin = (b["face"][..., 3] > 0) & (b["crown"][..., 3] == 0)
    for key, (anchor, cx, cy, slot) in PLACE.items():
        if key == "gloom":
            img = gloom(skin)
        else:
            base = key.rsplit(".", 1)[0] if key.endswith((".l", ".r")) else key
            img = _ascii(SPRITES[base])
        h, w = img.shape[:2]
        ax, ay = ANCHORS[anchor]
        items.append((key, img, anchor, round(cx - w // 2 - ax), round(cy - h // 2 - ay), slot))
    for k in range(3):
        img, x0, y0 = sparkle_frame(k)
        ax, ay = ANCHORS["eye_l"]
        items.append((f"sparkle.{k + 1}", img, "eye_l", round(x0 - ax), round(y0 - ay), "fx"))
    # pack
    width = 256
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
    meta = {"version": 1, "size": [278, 278], "fx": {}}
    for i, (key, img, anchor, ox, oy, slot) in enumerate(items):
        ax_, ay_ = placed[i]
        atlas[ay_ : ay_ + img.shape[0], ax_ : ax_ + img.shape[1]] = img
        meta["fx"][key] = {"ox": int(ox), "oy": int(oy), "w": int(img.shape[1]),
                           "h": int(img.shape[0]), "ax": int(ax_), "ay": int(ay_),
                           "anchor": anchor, "slot": slot}  # fmt: skip
    import char_parts as cp

    cp.save_indexed(atlas, out_dir / "fx.png")
    (out_dir / "fx.json").write_text(json.dumps(meta, indent=1) + "\n", encoding="utf-8")
    return {"fx": [width, int(atlas.shape[0])], "fxSprites": len(items)}
