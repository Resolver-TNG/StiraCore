"""Layered parts of the Stage character (#341): the icon split into parts that are drawn
back to front, each one *completed* where the parts in front of it hide it.

The decomposition is data (``LAYERS``, ``GROUPS`` below, written to ``parts.json``): part ids,
the layer order, the group each part moves with, the group pivots and parents. The renderer
composes the parts in this order; a pose moves a group (the head about the neck, the body from
the hips, an arm from its shoulder) and every part of it is re-pixelized for that pose.

Completion (so that a pose never opens a hole):

* ``turtleneck`` — continued under the coat's lapels, the mug, the hand and the hair;
* ``coat`` — continued under the hair strands and the mug arm, and below the icon's circle
  (sleeves, upper arms, front panels: ``char_body.py``);
* ``neck`` — drawn under the chin and the collar;
* ``face`` — the forehead continued under the fringe, eyes and mouth removed (drawn on top as
  expression parts);
* ``forearm_r`` / ``hand_r`` — the mug arm (navy cuff, coat sleeve down to the elbow, hand and
  mug); when the arm moves, these are replaced by the arm sprites of ``char_arms.py``.

Hair belongs to the hair layer (``build_hair.py`` / ``hair_layer.js``): ``hair_back``,
``hair_front`` and ``hair_tips`` are *slots* it draws; ``crown`` is the hair it leaves with the
head. In the back view, ``hair_back``
view's ``hair_back`` is a static drawing (``char_back.py``).
"""

from __future__ import annotations

import build_char as bc
import cv2
import numpy as np
from scipy import ndimage as ndi

# Back to front. kind: icon = cut from the icon (completed), drawn = made in code,
# sprite = expression / arm sprites placed at anchors, slot = filled by another layer
# (the hair rig) if present, else by the placeholder named here.
LAYERS = [
    {"id": "hair_back", "group": "head", "kind": "slot", "note": "back hair (hair rig); the back view's static hair"},
    {"id": "body_underlay", "group": "body", "kind": "icon", "note": "pixels base.png keeps that no part covers (build_hair.py fills under the hair)"},
    {"id": "head_underlay", "group": "head", "kind": "icon", "note": "as body_underlay, on the head: skin and earbud restored by build_hair.py, the dark underlay of the hair"},
    {"id": "neck", "group": "head", "kind": "drawn", "note": "under the collar and the chin"},
    {"id": "turtleneck", "group": "body", "kind": "icon"},
    {"id": "coat", "group": "body", "kind": "icon", "note": "lab coat incl. sleeves and upper arms; drawn below the circle"},
    {"id": "face", "group": "head", "kind": "icon", "note": "skin of the face, forehead completed, no eyes / mouth"},
    {"id": "cheek", "group": "head", "kind": "icon", "overHair": True,
     "note": "the face's pixels that show at rest below the eyes (cheeks, jaw, chin, the ear by the earbud), again: in front of the side locks, so a head pose never shaves the cheek"},
    {"id": "crown", "group": "head", "kind": "icon", "note": "the hair that stays with the head (the hair layer cuts the rest)"},
    {"id": "ear_l", "group": "head", "kind": "icon", "pivot": "ear_l"},
    {"id": "ear_r", "group": "head", "kind": "icon", "pivot": "ear_r"},
    {"id": "eyes", "group": "face", "kind": "sprite", "overHair": True,
     "note": "in front of the bangs where they cross (anime convention; the left eye is crossed by two strands)"},
    {"id": "mouth", "group": "face", "kind": "sprite"},
    {"id": "blush", "group": "face", "kind": "sprite"},
    {"id": "hair_front", "group": "head", "kind": "slot", "note": "hair layer (hair_layer.js): side locks, wisps, bangs"},
    {"id": "brows", "group": "face", "kind": "sprite", "overHair": True, "note": "drawn over the fringe (anime convention)"},
    {"id": "forearm_r", "group": "arm_r", "kind": "icon"},
    {"id": "hand_r", "group": "arm_r", "kind": "icon", "note": "hand and mug; arm sprites replace forearm_r + hand_r"},
    {"id": "mug_desk", "group": "body", "kind": "drawn"},
    {"id": "arm_l", "group": "arm_l", "kind": "sprite"},
    {"id": "hair_tips", "group": "head", "kind": "slot"},
    {"id": "fx", "group": "head", "kind": "sprite"},
]  # fmt: skip
GROUPS = {
    "body": {"parent": None, "pivot": [139, 278]},
    "head": {"parent": "body", "pivot": [139, 142]},
    "face": {"parent": "head", "pivot": [139, 110]},
    "arm_r": {"parent": "body", "pivot": [70, 170]},
    "arm_l": {"parent": "body", "pivot": [208, 170]},
}
ICON_PARTS = [
    "body_underlay",
    "head_underlay",
    "turtleneck",
    "coat",
    "neck",
    "face",
    "crown",
    "forearm_r",
    "hand_r",
]
UNDERLAY_ROW = 150  # base.png pixels above this row move with the head

FACE_EXT = [
    (106, 72),
    (172, 66),
    (174, 104),
    (166, 128),
    (152, 142),
    (126, 142),
    (112, 126),
    (106, 100),
]
TURTLENECK = [(116, 124), (164, 124), (168, 150), (160, 200), (154, 240), (152, 278), (94, 278),
              (96, 240), (100, 200), (110, 150)]  # fmt: skip
WRIST = (86.0, 230.0)
ELBOW = (22.0, 302.0)
FORE_HALF = 16.0
EYE_CORE = [(120.0, 98.5, 9.0, 5.2), (156.0, 97.5, 11.0, 4.8)]
MOUTH_BOX = (127, 118, 146, 127)


def fore_band(size, xx, yy):
    a, b = np.array(WRIST), np.array(ELBOW)
    d = b - a
    u = d / np.hypot(*d)
    n = np.array([-u[1], u[0]])
    along = (xx - a[0]) * u[0] + (yy - a[1]) * u[1]
    across = (xx - a[0]) * n[0] + (yy - a[1]) * n[1]
    return (along > -6) & (np.abs(across) <= FORE_HALF), along, across


def nearest_up(m, f):
    """Low-res mask -> hi-res, whole 4x4 blocks (so the area downscale gives the same colours)."""
    return np.kron(m.astype(np.uint8), np.ones((f, f), np.uint8)).astype(bool)


LEFT_EYE_STRANDS = (104, 84, 140, 110)  # x0, y0, x1, y1 (inclusive): around the left eye
FOREHEAD_NO_RESTORE = (
    90,
    40,
    151,
    113,
)  # the forehead and the eyes, left of the ear (no base.png fill)


# the coat's open front with the mug arm lifted: right of the line from the armpit (50, 168) to
# the waist (100, 246), up to the turtleneck's own edge
OPEN_FRONT = [(50, 168), (101, 160), (101, 278), (100, 246)]
# ... and what the free coat clears of it (#341, art fix): from the armpit (86, 190), the inner
# edge of her right upper arm, down to the waist. From (50, 168) — outside the figure, the
# line entered it at the shoulder's outline — the turtleneck took her shoulder and upper arm
# whenever the mug arm moved (in the icon the coat's sleeve is there, up to the lapel)
OPEN_FRONT_FREE = [(86, 190), (101, 178), (101, 278), (100, 246)]
STRAY_HAIR = 3  # px: a group of kept hair pixels this small below the head is coat
STRAY_DASH, STRAY_DASH_ROW = 8, 180  # px, row: ... and this small over the coat
NAVY_ISLAND = 60  # px: a group of navy pixels this small, off the turtleneck, is coat


def masks(parts, ears_poly, split):
    """Visible and completed (full) masks of every icon part, low-res. split: base.png after
    build_hair.split_hair() (the crown is the icon hair it keeps)."""
    px = parts.px
    size = parts.size
    mat = px.mat
    _, sil0, _, _ = px.lowres(px.smooth, px.alpha, px.ink, extend=False)
    yy, xx = np.mgrid[0 : px.th, 0 : px.tw]
    ears = np.zeros_like(sil0)
    for pts in ears_poly:
        ears |= bc.poly(size, pts)
    ears &= sil0
    core = np.zeros_like(sil0)
    for cx, cy, rx, ry in EYE_CORE:
        core |= ((xx - cx) / rx) ** 2 + ((yy - cy) / ry) ** 2 <= 1
    x0, y0, x1, y1 = MOUTH_BOX
    mouth = (xx >= x0) & (xx <= x1) & (yy >= y0) & (yy <= y1)
    hand_poly = bc.poly(size, bc.HAND) | bc.poly(size, bc.MUG)
    band, _, _ = fore_band(size, xx, yy)
    vis = {}
    vis["hand_r"] = hand_poly & sil0 & np.isin(mat, [1, 4, 5])
    vis["forearm_r"] = band & sil0 & np.isin(mat, [2, 3]) & ~vis["hand_r"] & (yy >= 214)
    face_area = bc.poly(size, FACE_EXT)
    vis["face"] = (
        ((np.isin(mat, [1, 5]) & (yy < 150) & ~hand_poly) | core | (mouth & face_area))
        & sil0
        & ~ears
    )
    hair_px = (mat == 0) & sil0 & ~ears & ~core & ~(mouth & face_area)
    from char_unify import unify

    # (base.png went through char_unify before the cut: compare with the same look)
    kept = (split[..., 3] > 0) & (split[..., :3] == unify(parts.full)[..., :3]).all(2)
    vis["crown"] = hair_px & kept
    removed = hair_px & ~kept
    vis["coat"] = (mat == 2) & sil0 & ~vis["forearm_r"]
    vis["turtleneck"] = (mat == 3) & sil0 & ~vis["forearm_r"]
    taken = np.zeros_like(sil0)
    for k, v in vis.items():
        v &= ~taken
        taken |= v
    rest = (
        sil0 & ~taken & ~ears & ~removed
    )  # anything unclassified (eye pixels of mat 4 etc.) -> nearest part
    if rest.any():
        lab = np.zeros(sil0.shape, np.int16)
        names = list(vis)
        for i, k in enumerate(names):
            lab[vis[k]] = i + 1
        _, (iy, ix) = ndi.distance_transform_edt(lab == 0, return_indices=True)
        near = lab[iy, ix]
        for i, k in enumerate(names):
            vis[k] |= rest & (near == i + 1)
    # (#341, art fix) navy islands on the coat: the icon's thin dark hair strands and fold
    # shadows over the coat's sleeve come out of the downscale as small blobs of the navy
    # material, drawn by the turtleneck through holes in the coat (dark blotches on her right
    # sleeve at rest). Away from the turtleneck they are coat, completed from the coat around.
    turtle_area = bc.poly(size, TURTLENECK)
    labels, n = ndi.label(vis["turtleneck"], structure=np.ones((3, 3)))
    island = np.zeros_like(sil0)
    if n:
        sizes = np.bincount(labels.ravel())
        on_neck = np.bincount(labels.ravel(), weights=turtle_area.ravel(), minlength=n + 1)
        small = (sizes <= NAVY_ISLAND) & (on_neck == 0)
        small[0] = False
        island = small[labels]
        vis["turtleneck"] &= ~island
    # stray hair pixels the cut left on the coat (groups of a few pixels below the head): they
    # are not hair any more; the coat is completed under them (#341, art unify: they showed as
    # dark specks on the coat, and as holes where the back hair showed through)
    stray = np.zeros_like(sil0)
    labels, n = ndi.label(vis["crown"], structure=np.ones((3, 3)))
    if n:
        sizes = np.bincount(labels.ravel())
        stray = (sizes <= STRAY_HAIR)[labels] & vis["crown"] & (yy >= 140)
        # over the coat (below the shoulders) also the short strand dashes the cut left
        stray |= (sizes <= STRAY_DASH)[labels] & vis["crown"] & (yy >= STRAY_DASH_ROW)
        vis["crown"] &= ~stray
    full = {k: v.copy() for k, v in vis.items()}
    full["coat"] |= stray & ~turtle_area  # hidden: completed from the coat around them
    full["coat"] |= island
    # ... and under the hair the crown keeps on her right sleeve (strands over the coat below the
    # shoulders, left of the turtleneck): when the head moves in a pose they move with it and
    # showed the turtleneck through the hole they left in the coat (a 2 px navy blot in `down2`)
    full["coat"] |= vis["crown"] & (yy >= 180) & (xx < 100) & ~turtle_area & sil0
    full["turtleneck"] |= stray & turtle_area
    # under the mug hand the coat hangs open (#341 art unify 2): the turtleneck continues there,
    # shown when the arm is lifted (char_parts: the free coat is clear in OPEN_FRONT)
    full["turtleneck"] |= bc.poly(size, OPEN_FRONT) & sil0 & ~vis["face"]
    full["turtleneck"] |= turtle_area & sil0 & ~vis["face"]
    # (art fix) and a little past both, under the coat's light strands by the lapel: the free coat
    # drops the strands that float on the sweater once the mug is lifted (char_parts.free_islands)
    near = ndi.binary_dilation(bc.poly(size, OPEN_FRONT) | turtle_area, iterations=3)
    full["turtleneck"] |= near & sil0 & ~vis["face"] & (yy >= 168)
    full["face"] |= face_area & sil0 & ~ears
    full["coat"] |= (
        sil0
        & (yy >= 140)
        & ~turtle_area
        & ~full["face"]
        & (removed | vis["hand_r"] | vis["forearm_r"])
    )
    # under the hair, the face takes base.png's pixels where build_hair.py restored them from the
    # icon (cheek, ear, earbud, the collar under the chin) instead of its own fill; not at the
    # redrawn eyes and mouth, and not on the forehead and around the eyes left of the ear: there
    # base.png has build_hair's nearest-colour fill under the cut bangs (a light-blue block left
    # of the left eye, brown specks), which showed wherever the bangs moved off the face (#341)
    x0, y0, x1, y1 = FOREHEAD_NO_RESTORE
    forehead = (xx >= x0) & (xx <= x1) & (yy >= y0) & (yy <= y1)
    face_restore = (
        full["face"]
        & ~vis["face"]
        & (split[..., 3] > 0)
        & ~ndi.binary_dilation(core, iterations=2)
        & ~mouth
        & ~forehead
    )
    # the bangs' strands that cross the face around the left eye (from the brow over the inner
    # corner down to the cheek, and beside the outer corner): the icon draws them as thin hair
    # strands; pixelized over the skin their edges became the skin's line colour (brown specks).
    # They are hair lines: the face part draws them in the hair's line colour.
    x0, y0, x1, y1 = LEFT_EYE_STRANDS
    strands = (xx >= x0) & (xx <= x1) & (yy >= y0) & (yy <= y1)
    return {
        "strands": strands,
        "vis": vis,
        "full": full,
        "sil0": sil0,
        "ears": ears,
        "band": band,
        "hair": removed,
        "face_restore": face_restore,
    }


def complete_sources(parts, mk):
    """Hi-res inputs of every icon part: the hidden pixels are filled from neighbours of the
    same material (no colour bleeding), their ink cleared, their alpha opaque."""
    px = parts.px
    f = int(bc.F)
    small = cv2.resize(px.smooth, parts.size, interpolation=cv2.INTER_AREA)
    mat_of = {"turtleneck": 3, "coat": 2, "face": 1}
    out = {}
    for k in ("turtleneck", "coat", "face", "crown", "forearm_r", "hand_r"):
        full, vis = mk["full"][k], mk["vis"][k]
        hidden = full & ~vis
        sm = px.smooth
        ink = px.ink
        mat = px.mat.copy()
        if hidden.any():
            from char_parts import fill_by_material

            mats = px.mat.copy()
            mats[hidden] = mat_of[k]
            fill = fill_by_material(small, px.mat, hidden, mats, 8 if k != "face" else 4)
            fh = cv2.resize(fill, (px.w0, px.h0), interpolation=cv2.INTER_LINEAR)
            H = nearest_up(hidden, f)
            sm = px.smooth.copy()
            sm[H] = fh[H]
            ink = px.ink.copy()
            ink[H] = 0
            mat[hidden] = mat_of[k]
        alpha = px.alpha * nearest_up(full, f)
        alpha[nearest_up(hidden, f)] = 1.0
        out[k] = {"smooth": sm, "ink": ink, "alpha": alpha, "mat": mat, "full": full}
    return out


# --- the cheeks in front of the side locks (#341, 2026-10-18) -----------------------------------
# The hair layer moves each chain by the displacement at its root (hair_layer.js rootPivot): the
# side locks hang from the temples, which move most in a head pose (far from the neck), while the
# face below them is re-pixelized rotated about the neck. In a tilt toward one side the lock beside
# the other cheek slid 3-9 px over it (the thinking pose 5 px, the approval tilt 9 px): the cheek
# and the corner of the jaw were cut off by the lock's straight edge. The fix keeps the face she
# shows at rest: those pixels, moved with the head, are drawn again in the `cheek` layer, which is
# in front of the hair (overHair, mask G). At rest it covers nothing more (no hair is there).
CHEEK_BELOW_EYES = (
    2  # rows below the eye anchor where the cheek band starts (the eyes are overHair)
)
BANG_PREFIX = "bangs"  # the bangs hang from the crown and cross the face in a tilt by design


def _js_round(v: float) -> int:
    """Math.round (half up), as the browser rounds the hair offsets."""
    return int(np.floor(v + 0.5))


def live_hair(spec: dict, with_rgba: bool = False):
    """The hair layer at rest for a pose as the browser draws it (hair_layer.js update() with
    char_bridge.hairInput(): the head at the crown in whole pixels, each chain moved by the
    displacement at its root's pivot). Returns (front, bangs): the pixels a front segment covers,
    and those the bangs cover; with_rgba: also the picture of the front segments."""
    import json

    from char_parts import pose_displacement
    from PIL import Image

    meta = json.loads((bc.OUT / "hair.json").read_text(encoding="utf-8"))
    atlas = np.asarray(Image.open(bc.OUT / "hair.png").convert("RGBA"))
    w, h = meta["size"]
    by_id = {s["id"]: s for s in meta["segments"]}
    cx, cy = pose_displacement(spec, 139.0, 60.0)
    head = (_js_round(cx), _js_round(cy))
    front = np.zeros((h, w), bool)
    bangs = np.zeros((h, w), bool)
    rgba = np.zeros((h, w, 4), np.uint8)
    for sg in meta["segments"]:
        if sg["z"] == "back":
            continue
        root = sg
        while root["parent"] != "head" and root["parent"] in by_id:
            root = by_id[root["parent"]]
        rx, ry = pose_displacement(spec, *(root.get("pivot") or (139.0, 60.0)))
        ox = head[0] + _js_round(rx - head[0])
        oy = head[1] + _js_round(ry - head[1])
        x0, y0, sw, sh = sg["box"]
        ax, ay = sg["at"]
        a = atlas[ay : ay + sh, ax : ax + sw, 3] > 127
        ys, xs = np.nonzero(a)
        ty, tx = ys + y0 + oy, xs + x0 + ox
        ok = (ty >= 0) & (ty < h) & (tx >= 0) & (tx < w)
        first = ok.copy()  # the first segment with a pixel wins (hair_layer.js)
        first[ok] = ~front[ty[ok], tx[ok]]
        rgba[ty[first], tx[first]] = atlas[ay + ys[first], ax + xs[first]]
        front[ty[ok], tx[ok]] = True
        if sg["part"] == BANG_PREFIX:
            bangs[ty[ok], tx[ok]] = True
    if with_rgba:
        return front, bangs, rgba
    return front, bangs


def rest_face(parts, imgs: dict, order: list, anchors: dict) -> np.ndarray:
    """The face she shows at rest below the eyes: pixels where the face (or the skin under the
    hair, head_underlay) is the top part and no hair is drawn, inside the face box (as
    tests/stage_face_test.cjs faceBox: from the eye and mouth anchors)."""
    from char_unify import unify

    top = np.full(parts.px.yy.shape, "", object)
    for k in order:
        if k in imgs:  # as the atlas stores them (Parts.add)
            top[unify(imgs[k], part=True)[..., 3] > 0] = k
    front, _ = live_hair({})
    yy, xx = parts.px.yy, parts.px.xx
    el, er, mo = anchors["eye_l"], anchors["eye_r"], anchors["mouth"]
    box = (
        (xx >= np.floor(el[0] - 20))
        & (xx <= np.ceil(er[0] + 26))
        & (yy >= round(el[1]) + CHEEK_BELOW_EYES)
        & (yy <= np.ceil(mo[1] + 26))
    )
    return np.isin(top, ["face", "head_underlay"]) & ~front & box


def _moved(mask: np.ndarray, spec: dict) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Rest positions of `mask` and where they are in a pose (the head's displacement, whole
    pixels as the browser rounds: char_rig.poseDisplacement)."""
    from char_parts import pose_displacement

    ys, xs = np.nonzero(mask)
    tx, ty = xs.copy(), ys.copy()
    if spec:
        for i, (y, x) in enumerate(zip(ys, xs, strict=True)):
            dx, dy = pose_displacement(spec, float(x), float(y))
            tx[i], ty[i] = x + _js_round(dx), y + _js_round(dy)
    return ys, xs, np.stack([ty, tx])


def cheek_part(imgs: dict, spec: dict, rest: np.ndarray) -> np.ndarray:
    """The `cheek` part of a pose: the rest face moved with the head (char_rig.poseDisplacement,
    whole pixels, as the hair is placed), with the pixels the pose's face (or head_underlay with
    nothing between them) draws there, except where the bangs lie in this pose."""
    from char_parts import pose_displacement
    from char_unify import unify

    h, w = rest.shape
    out = np.zeros((h, w, 4), np.uint8)
    face = unify(imgs["face"], part=True)  # the pixels the atlas stores (Parts.add)
    under = unify(imgs["head_underlay"], part=True)
    between = np.zeros((h, w), bool)
    for k in ("neck", "turtleneck", "coat"):
        if k in imgs:
            between |= imgs[k][..., 3] > 0
    _, bangs = live_hair(spec)
    _, _, (ty, tx) = _moved(rest, spec)
    # (and back: a pose pixel whose source is in the rest face — rounding can leave a pixel of the
    # moved face that no rest pixel lands on, a hole the hair showed through)
    if spec:
        ys, xs = np.nonzero(ndi.binary_dilation(rest, iterations=12))
        sx, sy = xs.copy(), ys.copy()
        for i, (y, x) in enumerate(zip(ys, xs, strict=True)):
            dx, dy = pose_displacement(spec, float(x), float(y))
            sx[i], sy[i] = x - _js_round(dx), y - _js_round(dy)
        ok = (sx >= 0) & (sx < w) & (sy >= 0) & (sy < h)
        back = np.zeros(len(xs), bool)
        back[ok] = rest[sy[ok], sx[ok]]
        ty, tx = np.concatenate([ty, ys[back]]), np.concatenate([tx, xs[back]])
    for y, x in zip(ty, tx, strict=True):
        if not (0 <= x < w and 0 <= y < h) or bangs[y, x]:
            continue
        if face[y, x, 3] > 0:
            out[y, x] = face[y, x]
        elif under[y, x, 3] > 0 and not between[y, x]:
            out[y, x] = under[y, x]
    return out


# --- the hair beside the jaw moves with the face (#447, 2026-10-19) ------------------------------
# A side lock is placed by the displacement at its root (up at the temple); the jaw below it moves
# less (the tilt turns about the neck, the nod fades toward the chin). On the side the head turns
# or tilts to, the lock's inner edge moved off the jaw and the face part's own pixels under it (the
# face is completed under the hair) showed between the cheek layer and the lock: the jaw read 1-7
# px wider than at rest and met the chin with a step; in a nod-and-tilt (type, yaw_l) a row
# between the bangs and the lock showed skin as a streak out from the cheek. The fix keeps the
# rest look there: the side locks' rest pixels beside the face, moved with the head as the cheek
# is, are drawn into the face part where it has skin outside the moved rest face. They lie under
# the hair layer, so they show only where the live lock has moved off (or sways a pixel away).
JAW_HAIR_REACH = 8  # px beside the rest face whose side-lock pixels move with the face


def _moved_mask(mask: np.ndarray, spec: dict) -> tuple[np.ndarray, np.ndarray]:
    """(target, source) pixel pairs of `mask` moved with the head (forward, and back for the
    pose pixels no rest pixel lands on), as cheek_part places the rest face. Arrays of [y, x]."""
    from char_parts import pose_displacement

    h, w = mask.shape
    ys, xs, (ty, tx) = _moved(mask, spec)
    src = [np.stack([ys, xs], 1)]
    dst = [np.stack([ty, tx], 1)]
    by, bx = np.nonzero(ndi.binary_dilation(mask, iterations=12))
    sx, sy = bx.copy(), by.copy()
    for i, (y, x) in enumerate(zip(by, bx, strict=True)):
        dx, dy = pose_displacement(spec, float(x), float(y))
        sx[i], sy[i] = x - _js_round(dx), y - _js_round(dy)
    ok = (sx >= 0) & (sx < w) & (sy >= 0) & (sy < h)
    back = np.zeros(len(bx), bool)
    back[ok] = mask[sy[ok], sx[ok]]
    src.append(np.stack([sy[back], sx[back]], 1))
    dst.append(np.stack([by[back], bx[back]], 1))
    dst, src = np.concatenate(dst), np.concatenate(src)
    ok = (dst[:, 0] >= 0) & (dst[:, 0] < h) & (dst[:, 1] >= 0) & (dst[:, 1] < w)
    return dst[ok], src[ok]


def jaw_hair(imgs: dict, spec: dict, rest: np.ndarray) -> None:
    """In a pose, the face part (imgs['face'], in place) takes the side locks' rest pixels beside
    the rest face, moved with the head, where it (or head_underlay with nothing between them)
    shows skin outside the moved rest face (the cheek's place)."""
    from char_unify import unify

    if not spec:
        return
    h, w = rest.shape
    front, bangs, hair = live_hair({}, with_rgba=True)
    yy = np.arange(h)[:, None]
    top = np.nonzero(rest.any(1))[0]
    near = ndi.binary_dilation(rest, iterations=JAW_HAIR_REACH) & (yy >= top[0])
    # the side locks, and the bangs' tips beside the face: outside the rest face's own row (a
    # tilt carries them off the cheek; not the bangs over the middle of the face)
    xx = np.arange(w)[None, :]
    lo = np.where(rest.any(1), rest.argmax(1), w)[:, None]
    hi = np.where(rest.any(1), w - 1 - rest[:, ::-1].argmax(1), -1)[:, None]
    beside = (xx < lo) | (xx > hi)
    lock = front & ~rest & near & (beside | ~bangs)
    shown = np.zeros((h, w), bool)
    t, _ = _moved_mask(rest, spec)
    shown[t[:, 0], t[:, 1]] = True
    skin = {
        tuple(int(v) for v in bc.hexrgb(c) * 255 + 0.5)
        for c in [*bc.RAMPS["skin"], bc.LINE["skin"]]
    }
    face = unify(imgs["face"], part=True)  # the pixels the atlas stores (Parts.add)
    under = unify(imgs["head_underlay"], part=True)
    between = np.zeros((h, w), bool)
    for k in ("neck", "turtleneck", "coat"):
        if k in imgs:
            between |= imgs[k][..., 3] > 0
    out = imgs["face"]
    dst, src = _moved_mask(lock, spec)
    for (y, x), (sy, sx) in zip(dst, src, strict=True):
        if shown[y, x]:
            continue
        if face[y, x, 3] > 0:
            top_px = face[y, x]
        elif under[y, x, 3] > 0 and not between[y, x]:
            top_px = under[y, x]
        else:
            continue
        if tuple(int(v) for v in top_px[:3]) in skin:
            out[y, x] = hair[sy, sx]
