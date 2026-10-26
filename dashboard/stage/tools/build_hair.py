"""Hair rig for Stage (#341): cut the character's hair into parts and tip segments.

Offline tool (not used at runtime), called by ``build_char.py`` while it builds the sheet,
so one command rebuilds everything:

    python3 dashboard/stage/tools/build_char.py      # or: python3 .../build_hair.py

The hair of the icon is split into five parts (back, side_l, side_r, bangs, wisps). Each part
has one or two chains, and each chain is cut along its length into a root segment and 1-3 tip
segments (child segments hang from their parent). The remaining hair (the crown between the
ears) stays in the head layer of ``base.png`` and is the root everything hangs from.

Outputs (in assets/char/):
  hair.png       atlas of the segment sprites (RGBA, palette of the sheet)
  hair_fx.png    same layout, R = rim-light pixel (from the uncut silhouette)
  hair_mask.png  sheet space, R = body in front of the back hair, G = arm/hand/mug (in front
                 of all hair), B = hidden pixel under the hair (filled, for the debug view)
  hair.json      parts, chains, segments (id, parent, pivot, box, atlas position, draw order)

Seams: every sprite overlaps its parent (or the crown) by ``OVERLAP`` px and continues under
the pixels that hide it (the arm for front hair, the body for back hair), and the pixels the
hair covered in ``base.png`` are filled: skin/coat/turtleneck where the body continues under
the hair, a dark hair underlay where the back of the hair mass is, nothing where a strand stuck
out into the background. With every offset at 0 the composite equals the uncut sheet
(``check_rest`` asserts it pixel by pixel, rim light included).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw
from scipy import ndimage as ndi

OVERLAP = 2  # px a segment reaches into its parent / the crown
HIDDEN = 3  # px a segment continues under the pixels in front of it
EAR_KEEP = 4  # px of the original hair kept in base.png around the ears
BODY_REACH = 4  # px the body is continued under the hair (then the dark hair underlay)

# Parts, front-most first. A chain is one strand group hanging from ``parent`` ('head' = the
# crown in base.png). ``poly`` selects its hair pixels (first match wins, in this order);
# ``axis`` is the direction from root to tip and ``cuts`` the segment borders along it
# (projection x*ax + y*ay), so a chain with n cuts has n + 1 segments.
PARTS = [
    {
        "id": "wisps",
        "z": "front",
        "slot": "over:body",
        "chains": [
            {
                "id": "wisps_l",
                "side": -1,
                "parent": "side_l.0",
                "axis": (-1, 0),
                "cuts": [-91],
                "poly": [(78, 74), (95, 74), (95, 91), (78, 91)],  # above the drawn lock
            },
            {
                "id": "wisps_r",
                "side": 1,
                "parent": "side_r.1",
                "axis": (1, 0),
                "cuts": [200],
                "poly": [(194, 106), (210, 106), (210, 140), (194, 140)],
            },
        ],
    },
    {
        "id": "bangs",
        "z": "front",
        "slot": "over:face",
        "chains": [
            {
                "id": "bangs",
                "side": 0,
                "parent": "head",
                "axis": (0, 1),
                "cuts": [78, 92],
                "poly": [
                    (104, 58),
                    (170, 58),
                    (172, 98),
                    (160, 94),
                    (150, 92),
                    (142, 106),
                    (128, 106),
                    (122, 116),
                    (110, 116),
                    (104, 100),
                ],
            },
        ],
    },
    {
        "id": "side_l",
        "z": "front",
        "slot": "over:body",
        "chains": [
            {
                "id": "side_l",
                "side": -1,
                "parent": "head",
                "axis": (0, 1),
                "cuts": [100, 128, 154],
                "poly": [
                    (86, 56),
                    (106, 56),
                    (104, 100),
                    (110, 116),
                    (122, 116),
                    (126, 136),
                    (118, 150),
                    (114, 178),
                    (98, 182),
                    (76, 180),
                    (70, 166),
                    (84, 150),
                    (88, 118),
                    (82, 104),
                    (80, 76),
                ],
            },
        ],
    },
    {
        "id": "side_r",
        "z": "front",
        "slot": "over:body",
        "chains": [
            {
                "id": "side_r",
                "side": 1,
                "parent": "head",
                "axis": (0, 1),
                "cuts": [100, 136, 172],
                "poly": [
                    (170, 56),
                    (190, 56),
                    (200, 100),
                    (208, 150),
                    (214, 172),
                    (204, 184),
                    (186, 196),
                    (178, 240),
                    (150, 240),
                    (150, 150),
                    (158, 132),
                    (162, 98),
                    (172, 98),
                ],
            },
        ],
    },
    {
        "id": "back",
        "z": "back",
        "slot": "behind:body",
        "chains": [
            {
                "id": "back_l",
                "side": -1,
                "parent": "head",
                "axis": (0, 1),
                "cuts": [204, 236],
                "poly": [(0, 150), (84, 150), (76, 180), (100, 186), (104, 278), (0, 278)],
            },
            {
                "id": "back_r",
                "side": 1,
                "parent": "head",
                "axis": (0, 1),
                "cuts": [210, 244],
                "poly": [(200, 150), (278, 150), (278, 278), (196, 278), (196, 200)],
            },
        ],
    },
]

# Where the body continues under the hair (filled with skin / coat / turtleneck when the hair
# is cut out): the face up to the hairline and the cheeks, the neck, the shoulders.
BODY_UNDER = [
    [(108, 68), (168, 68), (168, 104), (164, 124), (160, 140), (128, 142), (114, 126),
     (110, 104)],
    [(104, 128), (166, 128), (186, 150), (204, 176), (206, 210), (196, 278), (100, 278),
     (90, 200), (78, 178), (96, 150)],
]  # fmt: skip
UNDERLAY = ["#3e3f66", "#5f628d"]  # hair in shade behind the parts (hair ramp 1-2; #341 art
# unify: ramp 0-1 showed as dark clumps by the ear where the parts part in a tilt)
# Draw order contract with the character layer (back to front). The slot of each part says
# where it is inserted: "behind:body" = before the torso, "over:body" = after the torso / coat,
# "over:face" = after the face and eyes. The arms (and the mug) come after every hair part
# unless a segment sets overArm (data in hair_rig.js HAIR_RIG, per depth).
LAYER_ORDER = [
    "hair:back",
    "char:body",
    "hair:side_l",
    "hair:side_r",
    "hair:wisps",
    "char:face",
    "hair:bangs",
    "char:arms",
]
# Debug tint per part (?layers=1 and the structure image).
TINTS = {
    "back": "#9a5cf0",
    "side_l": "#e0a030",
    "side_r": "#30c070",
    "bangs": "#e04070",
    "wisps": "#40d8e8",
}


def _poly(size, pts) -> np.ndarray:
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).polygon(pts, fill=1)
    return np.asarray(mask).astype(bool)


def _hex(value: str) -> np.ndarray:
    return np.array([int(value[i : i + 2], 16) for i in (1, 3, 5)], dtype=np.uint8)


def rim_mask(alpha: np.ndarray) -> np.ndarray:
    """Rim-light pixels exactly as the character shader picks them (scene.js CHAR_FRAG)."""
    a = np.pad(alpha, 2)
    h, w = alpha.shape

    def out(dx, dy):  # alpha at (x + dx, y + dy) < 0.5
        return ~a[2 + dy : 2 + dy + h, 2 + dx : 2 + dx + w]

    e2 = out(2, 0) | out(-2, 0) | out(0, -2)
    e1 = out(1, 0) | out(-1, 0) | out(0, -1)
    rows = np.arange(h)[:, None] < 205
    return alpha & e2 & ~e1 & rows


CUT_BAND = 4  # px each side of a cut between two hair parts (their sprites overlap by 2)
CUT_DARK = 50  # luma below the hair around it


def soften_cuts(rgba, segs, crown, sil):
    """No dark pixel on a cut between two hair parts (#341, art unify 2): in a pose the parts
    of a lock move by different whole pixels, and a strand line's pixel on one side of a cut
    met the light hair of the other side as an isolated dark speck. Near every cut, a pixel
    much darker (luma 60) than the hair around it takes the colour most of its lighter
    neighbours have; the strand lines get a short gap there instead."""
    own = np.full(sil.shape, -1, np.int32)
    for i, sg in enumerate(segs):
        own[sg["own"]] = i
    own[crown] = len(segs)
    pad = np.pad(own, 1, constant_values=-2)
    h, w = own.shape
    cut = np.zeros(sil.shape, bool)
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            nb = pad[1 + dy : 1 + dy + h, 1 + dx : 1 + dx + w]
            cut |= (own >= 0) & (nb >= 0) & (nb != own)
    band = ndi.binary_dilation(cut, iterations=CUT_BAND) & (own >= 0)
    out = rgba.copy()
    rgb = rgba[..., :3].astype(np.int32)
    luma = rgb @ np.array([299, 587, 114]) // 1000
    for y, x in zip(*np.nonzero(band), strict=True):
        # the hair around it (5 x 5): a pixel well below its middle tone is a line piece
        y0, y1, x0, x1 = max(0, y - 2), min(h, y + 3), max(0, x - 2), min(w, x + 3)
        hair_nb = own[y0:y1, x0:x1].ravel() >= 0
        nl = luma[y0:y1, x0:x1].ravel()
        if hair_nb.sum() < 8 or np.median(nl[hair_nb]) < luma[y, x] + CUT_DARK:
            continue
        nb = rgb[y0:y1, x0:x1].reshape(-1, 3)
        ok = hair_nb & (nl >= luma[y, x] + CUT_DARK)
        cols, counts = np.unique(nb[ok], axis=0, return_counts=True)
        out[y, x, :3] = cols[np.argmax(counts)]
    # a dark pixel with no dark neighbour inside its own part: wherever the part moves, its
    # neighbours from other layers can all be light (a strand's last pixel at a lock's edge)
    rgb = out[..., :3].astype(np.int32)
    luma = rgb @ np.array([299, 587, 114]) // 1000
    for y, x in zip(*np.nonzero(own >= 0), strict=True):
        y0, y1, x0, x1 = max(0, y - 1), min(h, y + 2), max(0, x - 1), min(w, x + 2)
        mine = own[y0:y1, x0:x1].ravel() == own[y, x]
        mine[(y - y0) * (x1 - x0) + (x - x0)] = False
        nl = luma[y0:y1, x0:x1].ravel()[mine]
        if len(nl) < 3 or (nl < luma[y, x] + CUT_DARK).any():
            continue
        nb = rgb[y0:y1, x0:x1].reshape(-1, 3)[mine]
        cols, counts = np.unique(nb, axis=0, return_counts=True)
        out[y, x, :3] = cols[np.argmax(counts)]
    return out


def bc_mod():
    import build_char as bc

    return bc


def split_hair(rgba: np.ndarray, mat: np.ndarray, sil: np.ndarray, out_dir: Path, parts=None,
               view: str = "front", stem: str = "hair", lab=None):  # fmt: skip
    """Cut the hair out of ``rgba``; write the hair assets; return (base, mat) without it.

    ``parts`` / ``view`` / ``stem``: another view of the character (another sheet) gets its
    own part set as ``<stem>.json`` + ``<stem>*.png`` (hair_layer.js HAIR_VIEWS).
    """
    parts = PARTS if parts is None else parts
    if view == "front":
        if lab is not None:  # the ear beside the face: skin and earbud, not hair colour
            rgba, mat = restore_ear(rgba, mat, sil, lab)
        rgba, mat, sil = draw_locks(rgba, mat, sil)  # the lock beside the cheek, drawn
        rgba, mat, sil, _ = extend_hair(rgba, mat, sil)  # long hair below the round crop
        from char_unify import unify  # grey hair lines, no specks (#341, art unify)

        rgba = unify(rgba)
        # the hair's own outline is a hair grey everywhere (#341, art unify 2): a hair part's
        # outline pixel next to the coat or the face kept the black, and in a pose it landed
        # inside the hair (the remaining seams); the silhouette keeps its rim light
        black = (rgba[..., :3] == _hex(bc_mod().OUTLINE)).all(2) & (mat == 0) & sil
        rgba[black, :3] = _hex(bc_mod().RAMPS["hair"][1])
    import build_char as bc  # shared palette and region polygons

    h, w = mat.shape
    size = (w, h)
    hair = (mat == 0) & sil
    ears = ndi.binary_dilation(_poly(size, bc.EAR_L) | _poly(size, bc.EAR_R), iterations=1)
    free = hair & ~ears
    arm_region = _poly(size, bc.HAND) | _poly(size, bc.MUG)
    body = sil & ~hair  # original non-hair pixels
    arm = body & ndi.binary_dilation(arm_region, iterations=2)

    # 1. assign hair pixels to chains, then to segments along each chain
    segs = []  # dicts with id, part, chain, depth, own (mask)
    taken = np.zeros_like(free)
    for part in parts:
        for ch in part["chains"]:
            own = free & _poly(size, ch["poly"]) & ~taken
            taken |= own
            yy, xx = np.mgrid[0:h, 0:w]
            t = xx * ch["axis"][0] + yy * ch["axis"][1]
            bounds = [-np.inf, *ch["cuts"], np.inf]
            for d in range(len(bounds) - 1):
                m = own & (t >= bounds[d]) & (t < bounds[d + 1])
                segs.append({
                    "id": f"{ch['id']}.{d}",
                    "part": part["id"], "chain": ch["id"], "depth": d, "z": part["z"],
                    "side": ch["side"], "own": m, "axis": ch["axis"],
                    "cut": bounds[d] if d else None,
                    "parent": f"{ch['id']}.{d - 1}" if d else ch["parent"],
                })  # fmt: skip
    removed = taken
    crown = hair & ~removed  # stays in base.png (head layer)
    if view == "front":
        from char_unify import specks

        rgba = specks(soften_cuts(rgba, segs, crown, sil))  # (no speck left by a gap)

    # 2. base without the cut hair: fill what the hair covered
    under = np.zeros_like(removed)
    for pts in BODY_UNDER:
        under |= _poly(size, pts)
    # the body continues under the hair for a few px (no part moves further than that)
    line_rgb = [_hex(c) for c in [*bc.LINE.values(), bc.OUTLINE]]
    is_line = np.zeros_like(body)
    for c in line_rgb:
        is_line |= (rgba[..., :3] == c).all(2)
    src = body & ~is_line
    eyes = _poly(size, bc.EYE_L) | _poly(size, bc.EYE_R)
    dist, (iy, ix) = ndi.distance_transform_edt(~src, return_indices=True)
    # outside the eyes, never continue the iris (a blue streak would show beside the eye)
    d2, (jy, jx) = ndi.distance_transform_edt(~(src & (mat != 5)), return_indices=True)
    dist, iy, ix = (np.where(eyes, a, b) for a, b in ((dist, d2), (iy, jy), (ix, jx)))
    fill_body = removed & under & (dist <= BODY_REACH)
    env = ndi.binary_opening(sil, structure=np.ones((5, 5)))
    fill_hair = removed & ~fill_body & env
    # a hole right next to the body would change the body's rim light: underlay there instead
    fill_hair |= removed & ~fill_body & ndi.binary_dilation(body, iterations=2)
    clear = removed & ~fill_body & ~fill_hair
    base = rgba.copy()
    mat2 = mat.copy()
    base[fill_body] = rgba[iy[fill_body], ix[fill_body]]  # nearest body colour
    mat2[fill_body] = mat[iy[fill_body], ix[fill_body]]
    yy, xx = np.mgrid[0:h, 0:w]
    dith = bc.BAYER4[yy % 4, xx % 4]
    under_rgb = np.where((dith > -0.3)[..., None], _hex(UNDERLAY[1]), _hex(UNDERLAY[0]))
    base[fill_hair, :3] = under_rgb[fill_hair]
    base[fill_hair, 3] = 255
    base[clear] = 0
    # next to the ears keep the hair itself: the ears move by their weight map (which reaches
    # 1 px into the hair) and show the base pixels around them, not the underlay
    keep = removed & ndi.binary_dilation(ears, iterations=EAR_KEEP)
    base[keep] = rgba[keep]
    fill_hair &= ~keep
    clear &= ~keep
    new_sil = base[..., 3] > 127
    ring = fill_hair & ~ndi.binary_erosion(new_sil)
    base[ring, :3] = _hex(bc.RAMPS["hair"][1])  # the hair's outline grey (art unify 2)
    mat2[fill_hair | clear] = 6  # hidden: not hair any more (no sway weight, no bangs repaint)

    # 3. segment sprites: own pixels + overlap into the parent + hidden continuation
    rim = rim_mask(sil)
    by_id = {s["id"]: s for s in segs}
    disk = ndi.generate_binary_structure(2, 1)
    sprites = []
    for s in segs:
        own = s["own"]
        if not own.any():
            raise SystemExit(f"hair segment {s['id']} has no pixels: fix its polygon or cuts")
        # (never the ears: they move by their own weight map, the hair must not cover them)
        parent = by_id[s["parent"]]["own"] if s["parent"] in by_id else crown & ~ears
        near = ndi.binary_dilation(own, disk, iterations=OVERLAP)
        overlap = near & parent & ~own
        hidden_by = body if s["z"] == "back" else arm
        hid = ndi.binary_dilation(own, disk, iterations=HIDDEN) & hidden_by & ~own
        pix = rgba.copy()
        if hid.any():
            _, (jy, jx) = ndi.distance_transform_edt(~own, return_indices=True)
            pix[hid] = rgba[jy[hid], jx[hid]]
        m = own | overlap | hid
        ys, xs = np.nonzero(m)
        x0, y0, x1, y1 = xs.min(), ys.min(), xs.max() + 1, ys.max() + 1
        spr = np.zeros((y1 - y0, x1 - x0, 4), np.uint8)
        fx = np.zeros((y1 - y0, x1 - x0, 3), np.uint8)
        sub = m[y0:y1, x0:x1]
        spr[sub] = pix[y0:y1, x0:x1][sub]
        spr[sub, 3] = 255
        fx[..., 0] = (rim[y0:y1, x0:x1] & sub & ~hid[y0:y1, x0:x1]) * 255
        oy, ox = np.nonzero(own)
        if s["cut"] is None:
            tt = ox * s["axis"][0] + oy * s["axis"][1]
            sel = tt <= tt.min() + 1.5
        else:
            tt = ox * s["axis"][0] + oy * s["axis"][1]
            sel = tt <= s["cut"] + 1.5
        pivot = [round(float(ox[sel].mean()), 1), round(float(oy[sel].mean()), 1)]
        sprites.append((s, spr, fx, [int(x0), int(y0), int(x1 - x0), int(y1 - y0)], pivot,
                        int(own.sum())))  # fmt: skip

    # 4. pack the atlas (shelves), write the files
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
    for (_, spr, fx, _, _, _), (px, py) in zip(sprites, places, strict=True):
        sh, sw = spr.shape[:2]
        atlas[py : py + sh, px : px + sw] = spr
        atlas_fx[py : py + sh, px : px + sw] = fx
    hm = np.zeros((h, w, 3), np.uint8)
    hm[..., 0] = body * 255
    hm[..., 1] = arm * 255
    hm[..., 2] = (fill_body | fill_hair) * 255
    out_dir.mkdir(parents=True, exist_ok=True)
    Image.fromarray(atlas).save(out_dir / f"{stem}.png", optimize=True)
    Image.fromarray(atlas_fx).save(out_dir / f"{stem}_fx.png", optimize=True)
    Image.fromarray(hm).save(out_dir / f"{stem}_mask.png", optimize=True)

    order = {"bangs": 0, "wisps": 1, "side_l": 2, "side_r": 2, "back": 3}
    entries = []
    for (s, _, _, box, pivot, n), (px, py) in zip(sprites, places, strict=True):
        entries.append({
            "id": s["id"], "part": s["part"], "chain": s["chain"], "parent": s["parent"],
            "depth": s["depth"], "z": s["z"], "side": s["side"], "box": box, "at": [px, py],
            "pivot": pivot, "pixels": n,
            # share of the breath offset at the pivot (build_char.rig_weights: breath weight)
            "breath": round(float(np.clip((196 - pivot[1]) / 40.0, 0, 1)), 2),
        })  # fmt: skip
    # draw order: front parts before back, within a chain the tip before its parent
    entries.sort(key=lambda e: (order[e["part"]], e["chain"], -e["depth"]))
    meta = {
        "view": view,
        "size": [w, h],
        "atlas": [aw, int(ah)],
        "parts": [
            {
                "id": p["id"],
                "z": p["z"],
                "slot": p["slot"],
                "chains": [c["id"] for c in p["chains"]],
                "tint": TINTS[p["id"]],
            }
            for p in parts
        ],
        # where the parts go in the character's draw order (back to front); "char:*" are the
        # character layer's own groups. Segments with overArm (hair_rig.js HAIR_RIG) are drawn
        # after char:arms as well.
        "layerOrder": LAYER_ORDER,
        "segments": entries,
        "hairPixels": int(removed.sum()),
        "crownPixels": int(crown.sum()),
        # sha256 of the uncut sheet at rest (alpha, colour, rim): tests/stage_hair_test.cjs
        # composites base.png + the hair assets and must get the same hash
        "restHash": rest_hash(rgba[..., :3], rgba[..., 3] > 127, rim_mask(rgba[..., 3] > 127)),
    }
    (out_dir / f"{stem}.json").write_text(json.dumps(meta, indent=1) + "\n", encoding="utf-8")
    check_rest(rgba, base, meta, atlas, atlas_fx, hm)
    # Below the crown, "hair" pixels outside every part are shadowed skin / coat that the
    # material split could not tell from hair (the neck shadow, the turtleneck hem). They stay
    # in base.png as they are, but no longer count as hair, so the old per-pixel sway weight
    # (build_char.rig_weights, "hair tips") is empty: hair moves only through the rig.
    mat2[crown & (np.mgrid[0:h, 0:w][0] > 118)] = 6
    mat2[~(base[..., 3] > 127)] = 6  # pixels the drawing above took away are not hair either
    return base, mat2


# --- the ear beside the face ----------------------------------------------------------------

# build_char finds skin only inside its FACE polygon, so the ear on the image's right (between
# the hair strands, with the earbud in it) was coloured with the hair ramp. Here the pixels of
# EAR_R whose source colour is skin get the skin ramp, the dark earbud the mug (black) ramp;
# the hair strands that pass in front of the ear stay hair.
EAR_R = [
    (158, 95),
    (165, 90),
    (172, 88),
    (179, 90),
    (181, 97),
    (180, 106),
    (177, 113),
    (171, 117),
    (163, 121),
    (158, 116),
]  # fmt: skip  (ear + the side of the face before it)


def restore_ear(rgba: np.ndarray, mat: np.ndarray, sil: np.ndarray, lab: np.ndarray):
    import build_char as bc

    h, w = mat.shape
    lc, ac, bc_ = lab[..., 0], lab[..., 1], lab[..., 2]
    ear = _poly((w, h), EAR_R) & sil
    skin = ear & (lc > 52) & ((bc_ > 3.0) | (ac > 1.5))
    bud = ear & ~skin & (lc < 32) & (np.abs(ac) < 6) & (np.abs(bc_) < 8)
    skin = ndi.binary_opening(skin | ndi.binary_closing(skin), structure=np.ones((2, 2))) & ear
    bud &= ~skin
    yy, xx = np.mgrid[0:h, 0:w]
    dith = bc.BAYER4[yy % 4, xx % 4]
    out, mat2 = rgba.copy(), mat.copy()
    for m, name, k in ((skin, "skin", 1), (bud, "mug", 4)):
        if not m.any():
            continue
        ref = (mat == k) & sil
        lo, hi = np.percentile(lc[ref], [3, 98]) if ref.any() else (lc[m].min(), lc[m].max())
        t = np.clip((lc - lo) / max(hi - lo, 1), 0, 1) ** 0.9
        ramp = np.array([_hex(c) for c in bc.RAMPS[name]])
        idx = np.clip(np.floor(t * (len(ramp) - 1) + 0.5 + dith * 0.55), 0, len(ramp) - 1)
        if name == "skin":  # soft light skin, as in the icon (the dark steps are for the lines)
            idx = np.clip(idx, 2, len(ramp) - 1)
        out[m, :3] = ramp[idx[m].astype(int)]
        mat2[m] = k
    # a soft shadow step where the skin meets the hair in front of / behind it
    edge = skin & ndi.binary_dilation((mat2 == 0) & sil) & ~ndi.binary_erosion(skin)
    out[edge, :3] = _hex(bc.RAMPS["skin"][1])
    if bud.any():
        out = clean_earbud(out, bud, skin)
    return out, mat2


def clean_earbud(out: np.ndarray, bud: np.ndarray, skin: np.ndarray) -> np.ndarray:
    """The earbud as one clean shape (03 / 06: a black earbud with a small highlight): the
    biggest blob of the dark pixels, filled without holes, shaded with the mug ramp (lit from
    the upper left), its stray dark pixels on the skin taken back to the skin (#341, art unify:
    it read as a dark smudge beside the cheek)."""
    import build_char as bc

    labels, n = ndi.label(bud)
    if not n:
        return out
    sizes = ndi.sum(bud, labels, range(1, n + 1))
    main = ndi.binary_fill_holes(labels == int(np.argmax(sizes)) + 1)
    ys, xs = np.nonzero(main)
    cy, cx = ys.mean(), xs.mean()
    ramp = [_hex(c) for c in bc.RAMPS["mug"]]
    for y, x in zip(ys, xs, strict=True):
        u, v = x - cx, y - cy
        k = 1 if u + v > -1.5 else 2  # the body; the upper left a step lighter
        if u + v < -3.0:
            k = 3  # the highlight
        if u + v > 2.5:
            k = 0  # the shade on the lower right
        out[y, x, :3] = ramp[k]
    stray = bud & ~main
    if stray.any():
        # back to the skin around them (the restored skin's nearest pixel)
        _, (iy, ix) = ndi.distance_transform_edt(~skin, return_indices=True)
        out[stray] = out[iy[stray], ix[stray]]
    return out


# --- the lock beside the cheek, drawn -------------------------------------------------------

# Beside the cheek (image left, cheek to throat) the icon's hair is a few thin, light strands
# against a light background, so the subject mask cut them into a ragged edge with holes. That
# part of the lock is drawn instead, as one lock: an outer and an inner contour along the hair
# flow (smooth, meeting in a pointed tip), filled without holes, shaded across its width with
# the hair ramp (dark at the edges, a sheen band toward the rim light, darker toward the tip),
# ordered dither, strand lines that follow the contour and thin out toward the tip, and the
# outline on the outer side. Hair pixels of the old ragged edge in LOCK_CLEAR are removed
# first; the lock lies over the coat's shoulder, never over the skin.
LOCK_CLEAR = [(78, 90), (101, 90), (100, 150), (97, 172), (74, 172)]
LOCK = {
    "outer": [(97, 90), (95, 100), (94, 112), (93, 124), (92, 136), (91, 147), (89, 157)],
    "inner": [(104, 90), (103, 102), (102, 116), (102, 130), (101, 144), (99, 156), (96, 164)],
    "tip": (90, 167),
    "v": 3.0,  # base shade (hair ramp index)
    "lines": [(0.42, 0.0, 0.85), (0.7, 0.1, 0.95)],  # across (0 outer .. 1 inner), from, to
}


def _curve_x(pts, y):
    ys = np.array([p[1] for p in pts], float)
    xs = np.array([p[0] for p in pts], float)
    return float(np.interp(y, ys, xs))


def draw_locks(rgba: np.ndarray, mat: np.ndarray, sil: np.ndarray):
    import build_char as bc

    h, w = mat.shape
    yy, xx = np.mgrid[0:h, 0:w]
    dith = bc.BAYER4[yy % 4, xx % 4]
    ramp = np.array([_hex(c) for c in bc.RAMPS["hair"]])
    out, mat2, sil2 = rgba.copy(), mat.copy(), sil.copy()
    # the old ragged edge: hair and the dark specks the material split called turtleneck
    clear = _poly((w, h), LOCK_CLEAR) & np.isin(mat, (0, 3)) & sil
    out[clear] = 0
    sil2[clear] = False
    lock = np.zeros((h, w), bool)
    outer, inner, (tx, ty) = LOCK["outer"], LOCK["inner"], LOCK["tip"]
    y_top, y_split = outer[0][1], min(outer[-1][1], inner[-1][1])
    for y in range(y_top, ty + 1):
        if y <= y_split:
            xl, xr = _curve_x(outer, y), _curve_x(inner, y)
        else:  # both contours run into the tip (smooth taper)
            f = (y - y_split) / max(ty - y_split, 1)
            e = 1 - (1 - f) ** 1.6
            xl = _curve_x(outer, min(y, outer[-1][1])) + (tx - _curve_x(outer, outer[-1][1])) * e
            xr = (
                _curve_x(inner, min(y, inner[-1][1]))
                + (tx + 0.6 - _curve_x(inner, inner[-1][1])) * e
            )
        t = (y - y_top) / max(ty - y_top, 1)  # root .. tip
        for x in range(int(np.floor(xl + 0.5)), int(np.floor(xr + 0.5)) + 1):
            if not (0 <= x < w) or (sil2[y, x] and mat2[y, x] == 1):
                continue  # never over the skin (the lock lies over the coat's shoulder)
            u = (x - xl) / max(xr - xl, 1)  # 0 outer .. 1 inner
            v = LOCK["v"] - 1.3 * (2 * u - 1) ** 4 - 1.0 * t**1.6
            v += 1.2 * max(0.0, 1 - ((u - 0.25) / 0.2) ** 2) * (1 - t) ** 0.8  # sheen
            idx = int(np.clip(np.floor(v + 0.5 + dith[y, x] * 0.55), 0, len(ramp) - 1))
            col = ramp[idx]
            for lu, l0, l1 in LOCK["lines"]:
                if l0 <= t <= l1 and abs(u - lu) * max(xr - xl, 1) < 0.5:
                    col = _hex(bc.LINE["hair"])
            out[y, x, :3] = col
            out[y, x, 3] = 255
            lock[y, x] = True
            mat2[y, x] = 0
            sil2[y, x] = True
    # outline on the outer side (next to nothing), a hair line where the lock meets the body
    near_out = ndi.binary_dilation(~sil2) & lock
    out[near_out, :3] = _hex(bc.OUTLINE)
    near_body = ndi.binary_dilation(sil2 & (mat2 != 0) & (mat2 != 6)) & lock & ~near_out
    out[near_body, :3] = _hex(bc.LINE["hair"])
    return out, mat2, sil2


# --- long hair below the round crop ---------------------------------------------------------

# The icon is a circle crop: below it build_char only stretched the edge pixels, so the hair
# outside the shoulders ran into the frame as streaks. Here those strands are drawn on: each
# pixel beyond the crop follows the hair flow back to the last real hair pixel, continues its
# shade (re-dithered at its own position, the strand lines continued) and ends in clumps of
# pointed tips at about upper-arm height, with the outline. The coat is never painted over.
EXT_TIP_Y = (258, 276)  # sheet rows where the clumps end (upper arm), shortest .. longest
EXT_CLUMP = 7.0  # px width of a clump of strands (one pointed tip)
EXT_SIDES = [  # region, side, flow direction toward the tips
    {"poly": [(0, 186), (84, 150), (76, 180), (100, 186), (104, 278), (0, 278)], "side": -1},
    {"poly": [(196, 186), (278, 186), (278, 278), (196, 278)], "side": 1},
]


def _hash(k: int) -> float:
    return float(np.modf(np.sin(k * 12.9898 + 4.1) * 43758.5453)[0] % 1.0)


def extend_hair(rgba: np.ndarray, mat: np.ndarray, sil: np.ndarray):
    """Draw the long back hair below the round crop. Returns (rgba, mat, sil, drawn mask)."""
    import build_char as bc

    h, w = sil.shape
    size = (w, h)
    yy, xx = np.mgrid[0:h, 0:w]
    cx = cy = (w - 1) / 2
    r = w / 2
    dist = np.hypot(xx - cx, yy - cy)
    real = dist < r - 3  # pixels from the icon (build_char.extend_circle_cut skips the rim)
    hair_rgb = np.array([_hex(c) for c in bc.RAMPS["hair"]]).astype(int)
    line_rgb = _hex(bc.LINE["hair"]).astype(int)
    px = rgba[..., :3].astype(int)
    ramp_idx = np.full((h, w), -1.0)
    for i, c in enumerate(hair_rgb):
        ramp_idx[(px == c).all(2)] = i
    is_line = (px == line_rgb).all(2)
    hair = (mat == 0) & sil
    body = sil & (mat != 0)
    # the coat as a closed area: the stretched hair inside it belongs to the coat's texture
    coat = ndi.binary_fill_holes(ndi.binary_closing(body, structure=np.ones((7, 7)))) & sil
    out, mat2, sil2 = rgba.copy(), mat.copy(), sil.copy()
    drawn = np.zeros_like(sil)
    dith = bc.BAYER4[yy % 4, xx % 4]
    for side in EXT_SIDES:
        region = _poly(size, side["poly"])
        src = hair & real & region & (ramp_idx >= 0) | (hair & real & region & is_line)
        # flow: the outer silhouette of the real hair (rows 170-235), as in extend_circle_cut
        pts = []
        for y in range(170, 236):
            xs = np.nonzero(sil[y] & real[y] & region[y])[0]
            if len(xs):
                pts.append((y, xs.min() if side["side"] < 0 else xs.max()))
        ys_, xs_ = np.array(pts).T
        slope = float(np.polyfit(ys_, xs_, 1)[0])
        d = np.array([slope, 1.0]) / np.hypot(slope, 1.0)
        nrm = np.array([d[1], -d[0]])
        cand = region & ~real & (yy >= 186) & ~coat
        # also the old stretched hair of the circle's rim, and the strip outside the silhouette
        reach = ndi.binary_dilation(sil & region, iterations=6) & region & ~real & (yy >= 186)
        cand |= reach & ~coat
        ys, xs = np.nonzero(cand)
        # remove what build_char stretched there; it is redrawn below (or stays empty)
        clear = cand & hair
        out[clear] = 0
        sil2[clear] = False
        for y, x in zip(ys, xs, strict=True):
            t_hit = None
            for t in range(1, 90):
                qx, qy = round(x - d[0] * t), round(y - d[1] * t)
                if not (0 <= qx < w and 0 <= qy < h):
                    break
                if src[qy, qx]:
                    t_hit = t
                    break
                if body[qy, qx] and not hair[qy, qx] and real[qy, qx]:
                    break  # the coat is in the way: this strand is behind it
            if t_hit is None:
                continue
            qx, qy = round(x - d[0] * t_hit), round(y - d[1] * t_hit)
            s_par = float(np.dot([qx, qy], nrm))
            k = int(np.floor(s_par / EXT_CLUMP))
            u = s_par / EXT_CLUMP - k
            lo, hi = EXT_TIP_Y
            y_end = lo + (hi - lo) * _hash(k)
            y_end -= abs(2 * u - 1) ** 1.6 * 6  # pointed: the clump's middle hangs longest
            if y > y_end:
                continue
            # shade: mean ramp index of the last few real pixels of this strand
            vals = []
            line = False
            for t in range(t_hit, t_hit + 5):
                ax, ay = round(x - d[0] * t), round(y - d[1] * t)
                if 0 <= ax < w and 0 <= ay < h and src[ay, ax]:
                    if is_line[ay, ax]:
                        line = line or t == t_hit
                    else:
                        vals.append(ramp_idx[ay, ax])
            v = float(np.mean(vals)) if vals else 1.5
            frac = (y - 186) / max(y_end - 186, 1)
            v -= 0.5 * frac  # the back hair darkens toward the tips
            if line and frac < 0.85:
                out[y, x, :3] = line_rgb
            else:
                idx = int(np.clip(np.floor(v + 0.5 + dith[y, x] * 0.55), 0, len(hair_rgb) - 1))
                out[y, x, :3] = hair_rgb[idx]
            out[y, x, 3] = 255
            sil2[y, x] = True
            mat2[y, x] = 0
            drawn[y, x] = True
    # outline of the new silhouette, and of the coat where the stretched hair was taken away
    edge = sil2 & ~ndi.binary_erosion(sil2) & ~real
    out[edge & (drawn | (mat2 == 0)), :3] = _hex(bc.OUTLINE)
    out[edge & ~drawn & (mat2 != 0), :3] = _hex(bc.OUTLINE)
    return out, mat2, sil2, drawn


def composite(base, meta, atlas, atlas_fx, hm, offsets=None):
    """CPU model of scene.js + hair_layer.js for one pose (head, breath and arm at 0).

    Returns (rgb, rim) of the sheet: the base layer with its rim, then the hair segments in
    draw order with their own rim flag; back segments hide behind the body, all behind the arm.
    """
    h, w = base.shape[:2]
    alpha = base[..., 3] > 127
    rgb = base[..., :3].copy()
    rim = rim_mask(alpha)
    body, arm = hm[..., 0] > 127, hm[..., 1] > 127
    done = np.zeros((h, w), bool)
    for e in meta["segments"]:
        ox, oy = (offsets or {}).get(e["id"], (0, 0))
        x0, y0, sw, sh = e["box"]
        ax, ay = e["at"]
        spr = atlas[ay : ay + sh, ax : ax + sw]
        sfx = atlas_fx[ay : ay + sh, ax : ax + sw, 0] > 127
        for yy in range(sh):
            for xx in range(sw):
                if spr[yy, xx, 3] < 128:
                    continue
                X, Y = x0 + xx + ox, y0 + yy + oy
                if not (0 <= X < w and 0 <= Y < h) or done[Y, X] or arm[Y, X]:
                    continue
                if e["z"] == "back" and body[Y, X]:
                    continue
                rgb[Y, X] = spr[yy, xx, :3]
                rim[Y, X] = sfx[yy, xx]
                done[Y, X] = True
                alpha[Y, X] = True
    return rgb, rim, alpha


def rest_hash(rgb, alpha, rim) -> str:
    buf = np.zeros(alpha.shape + (5,), np.uint8)
    buf[..., 0] = alpha
    buf[..., 1:4] = rgb * alpha[..., None]
    buf[..., 4] = rim & alpha
    return hashlib.sha256(buf.tobytes()).hexdigest()


def check_rest(orig, base, meta, atlas, atlas_fx, hm) -> None:
    a0 = orig[..., 3] > 127
    rgb, rim, alpha = composite(base, meta, atlas, atlas_fx, hm)
    bad = (alpha != a0) | (a0 & ((rgb != orig[..., :3]).any(2) | (rim != rim_mask(a0))))
    if bad.any():
        ys, xs = np.nonzero(bad)
        raise SystemExit(f"hair rig: rest pose differs from the uncut sheet at {len(ys)} px, "
                         f"e.g. {list(zip(xs[:6].tolist(), ys[:6].tolist(), strict=True))}")  # fmt: skip


if __name__ == "__main__":
    import build_char

    build_char.build()
