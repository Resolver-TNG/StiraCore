"""Motion parts for the Stage character (#341): pose sheets, ears, face sprites and arms.

Offline tool, run by ``build_char.py`` after the spike's sheet (needs numpy, scipy,
opencv-python and Pillow). Nothing here runs in the browser.

How the poses are made (no imported art; everything comes from the owner's icon or is drawn
in code):

* **Pose sheets** — the hi-res source icon is *warped* with a smooth displacement field (head
  tilt about the neck, nod / look-up pitch, shoulders up / down) and then pixelized again with
  exactly the same ramps, ordered dither, line art and outline as ``base.png``. A tilted head is
  therefore re-drawn as pixel art instead of being a rotated sprite. The sheets have no eyes,
  mouth or ears: those are drawn on top at the anchors of the sheet. Only the pixels that differ
  from the ``base`` sheet are stored.
* **Ears** — cut out of the icon and rotated about their base at hi-res before pixelization,
  in steps of ``EAR_STEP`` degrees.
* **Face** — eyes, mouths and brows are drawn procedurally at 1x (``char_face.py``).
* **Arms** — the mug hand is the icon's own hand and mug, moved rigidly at hi-res; the free
  hand and the sleeves are drawn from capsules and polygons and shaded with the same ramps.
* The hair stays as in the icon (the hair parts and their follow-through belong to the hair
  layer, not to these sheets).

Output: ``assets/char/parts.png`` (atlas) and ``assets/char/parts.json`` (rectangles,
offsets, anchors and the lists of ids the motion catalog may use).
"""

from __future__ import annotations

import itertools
import json
from pathlib import Path

import build_char as bc
import char_face as cf
import cv2
import numpy as np
from PIL import Image
from scipy import ndimage as ndi

OUT = bc.OUT
ROOT_DOCS = bc.ROOT.parents[1] / "docs" / "research" / "stage-motion-2026-10-17"
F = 4
NECK = (139.0, 142.0)  # pivot of the head (portrait pixels)
BREATH_ROW = 176  # rows above this move with the breath (as in the spike's weight map)
EAR_STEP = 6
EAR_ANGLES = list(range(-18, 19, EAR_STEP))
# Ear outlines cut from the icon (tighter than the spike's ear weights) and their pivots.
EARS = {
    "l": {"poly": [(76, 6), (84, 8), (128, 32), (126, 36), (101, 64), (96, 62), (80, 30)],
          "pivot": (112, 50), "base": [(96, 62), (101, 64), (126, 36), (128, 32)]},
    "r": {"poly": [(163, 0), (168, 0), (192, 50), (190, 54), (177, 56), (149, 35), (156, 14)],
          "pivot": (170, 46), "base": [(149, 35), (177, 56), (190, 54)]},
}  # fmt: skip
FOREARM = [(80, 220), (103, 218), (106, 278), (78, 278), (78, 248)]
EYE_RM = [(110, 90, 130, 105), (144, 89, 169, 104)]  # x0, y0, x1, y1 (inclusive)
MOUTH_RM = (127, 119, 146, 127)
EYE_CORE = [(120.0, 98.5, 9.0, 5.2), (156.0, 97.5, 11.0, 4.8)]  # centre, radii

# Pose sheets: tilt (deg, + = the top of the head toward the right of the picture), head
# shift, pitch (+ = nod down, px at the eyes), shoulders (+ = drop, px). Data only: a motion
# may name any of these ids. A new sheet = one more line here + a rebuild.
SHEETS = {
    "base": {},
    "tilt_l1": {"tilt": -4.0},
    "tilt_l2": {"tilt": -8.0},
    "tilt_r1": {"tilt": 4.0},
    "tilt_r2": {"tilt": 8.0},
    "yaw_l": {"tilt": -1.5, "hx": -2.0},
    "yaw_r": {"tilt": 1.5, "hx": 2.0},
    "down1": {"pitch": 2.0},
    "down2": {"pitch": 4.0, "shoulders": 1.0},
    "up1": {"pitch": -2.0},
    "straight": {"pitch": -1.0, "shoulders": -2.0},
    "lift": {"pitch": -2.0, "shoulders": -3.0},
    "relax": {"pitch": 1.0, "shoulders": 2.0},
    "slump": {"pitch": 4.0, "shoulders": 4.0, "tilt": -2.5},
    "ponder": {"tilt": 5.0, "pitch": -1.0},
    "sip": {"tilt": -3.0, "pitch": -1.5},
    "type": {"pitch": 2.5, "tilt": -1.5},
}


HEAD_PARTS = ("face", "crown", "neck", "head_underlay")

# Back view sheets (char_back.py): shoulders raised (px), head shift, turn (0 = straight back).
BACK_SHEETS = {
    "back": {},
    "back_l": {"shoulder_l": 1.5},
    "back_r": {"shoulder_r": 1.5},
    "back_bob": {"head_dy": 1.0, "shoulder_l": 0.5, "shoulder_r": 0.5},
    "back_tilt": {"head_dx": -2.0, "head_dy": 1.0},
    "turn": {"turn": 0.5},
    "turn1": {"turn": 0.85},
}
# The side profile between the front and the 3/4 back (char_profile.py).
PROFILE_SHEETS = {"profile": {}}


# --- pixelization (same steps and ramps as build_char.build) -----------------------------------


class Pixelizer:
    def __init__(self) -> None:
        src, alpha = bc.load()
        self.h0, self.w0 = alpha.shape
        self.tw, self.th = int(self.w0 / bc.F), int(self.h0 / bc.F)
        self.size = (self.tw, self.th)
        self.alpha = alpha
        self.smooth = (
            cv2.bilateralFilter((src * 255).astype(np.uint8), 9, 30, 5).astype(np.float32) / 255
        )
        lum = cv2.cvtColor(src, cv2.COLOR_RGB2LAB)[..., 0]
        self.ink = np.clip((cv2.GaussianBlur(lum, (0, 0), 3.5) - lum - 4) / 14, 0, 1) * (
            alpha > 0.5
        )
        col, sil, _, filled = self.lowres(self.smooth, self.alpha, self.ink)
        self.mat, lc = self.classify(col)
        self.levels = {}
        for k in range(len(bc.MATS)):
            m = (self.mat == k) & sil
            if m.any():
                ref = m & ~filled
                self.levels[k] = np.percentile(lc[ref if ref.any() else m], [3, 98])
        yy, xx = np.mgrid[0 : self.th, 0 : self.tw]
        self.yy, self.xx = yy.astype(np.float32), xx.astype(np.float32)
        self.dith = bc.BAYER4[yy % 4, xx % 4]

    def lowres(self, smooth, alpha, ink, extend=True):
        rs = cv2.resize(smooth * alpha[..., None], self.size, interpolation=cv2.INTER_AREA)
        a_s = cv2.resize(alpha, self.size, interpolation=cv2.INTER_AREA)
        col = np.clip(rs / np.maximum(a_s[..., None], 1e-3), 0, 1)
        ink_s = cv2.resize(ink, self.size, interpolation=cv2.INTER_AREA)
        sil = a_s > 0.5
        sil = ndi.binary_opening(sil) | (sil & ndi.binary_erosion(sil))
        before = sil.copy()
        if extend:
            col, sil = bc.extend_circle_cut(col, sil)
        return col, sil, ink_s, sil & ~before

    def classify(self, col):
        size = self.size
        lab = cv2.cvtColor(col.astype(np.float32), cv2.COLOR_RGB2LAB)
        lc, ac, bcc = lab[..., 0], lab[..., 1], lab[..., 2]
        yy = np.mgrid[0 : self.th, 0 : self.tw][0]
        face, hand, mug = bc.poly(size, bc.FACE), bc.poly(size, bc.HAND), bc.poly(size, bc.MUG)
        eyes = bc.poly(size, bc.EYE_L) | bc.poly(size, bc.EYE_R)
        mat = np.zeros((self.th, self.tw), np.uint8)
        mat[face & (lc > 55) & ((bcc > 3.3) | (ac > 1.5))] = 1
        torso = (yy > 138) & ~face
        navy = (lc < 45) & (bcc < -3)
        mat[torso & navy] = 3
        mat[torso & ~navy & (bcc > 2.8) & (ac < 1.2)] = 2
        mat[hand & (lc > 50) & (ac > 0.5)] = 1
        counts = np.stack([ndi.uniform_filter((mat == k).astype(float), 3) for k in range(6)])
        mat = counts.argmax(0).astype(np.uint8)
        mat[mug & (lc < 40)] = 4
        mat[eyes & (lc < 72) & (bcc < 0.5)] = 5
        return mat, lc

    def ramp_index(self, t, name):
        n = len(bc.RAMPS[name])
        return np.clip(np.floor(t * (n - 1) + 0.5 + self.dith * 0.55), 0, n - 1).astype(int)

    def shade(self, col, mat, sil, ink_s, no_outline=None, tmap=None):
        """Ramps + dither + line art + outline. tmap (optional): lightness 0..1 per pixel
        for drawn parts (NaN where the colour comes from `col`)."""
        lc = cv2.cvtColor(col.astype(np.float32), cv2.COLOR_RGB2LAB)[..., 0]
        out = np.zeros((self.th, self.tw, 3), np.float32)
        for k, name in enumerate(bc.MATS):
            m = (mat == k) & sil
            if not m.any() or k not in self.levels:
                continue
            lo, hi = self.levels[k]
            t = np.clip((lc - lo) / max(hi - lo, 1), 0, 1) ** 0.9
            if tmap is not None:
                t = np.where(np.isnan(tmap), t, tmap)
            ramp = np.array([bc.hexrgb(c) for c in bc.RAMPS[name]])
            out[m] = ramp[self.ramp_index(t, name)[m]]
        line = (ink_s > 0.20) & sil
        labels, count = ndi.label(line, structure=np.ones((3, 3)))
        if count:
            sizes = ndi.sum(line, labels, range(1, count + 1))
            line &= ~np.isin(labels, np.where(sizes < 3)[0] + 1)
        for k, name in enumerate(bc.MATS):
            out[line & (mat == k)] = bc.hexrgb(bc.LINE[name])
        solid = sil if no_outline is None else (sil | no_outline)
        edge = sil & ~ndi.binary_erosion(solid)
        out[edge] = bc.hexrgb(bc.OUTLINE)
        rgba = np.dstack([out * 255, sil * 255]).astype(np.uint8)
        rgba[~sil] = 0
        return rgba

    # hi-res helpers
    def up(self, a):
        return cv2.resize(a.astype(np.float32), (self.w0, self.h0), interpolation=cv2.INTER_LINEAR)

    def up_mask(self, m):
        return self.up(m) > 0.5

    def warp_hi(self, arrs, dx, dy):
        dxh, dyh = self.up(dx) * bc.F, self.up(dy) * bc.F
        Y, X = np.mgrid[0 : self.h0, 0 : self.w0].astype(np.float32)
        mx, my = X - dxh, Y - dyh
        return [
            cv2.remap(a, mx, my, cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=0)
            for a in arrs
        ]

    def warp_lo(self, a, dx, dy):
        return cv2.remap(a, self.xx - dx.astype(np.float32), self.yy - dy.astype(np.float32), cv2.INTER_NEAREST,
                         borderMode=cv2.BORDER_CONSTANT, borderValue=0)  # fmt: skip

    def render(
        self,
        smooth=None,
        alpha=None,
        ink=None,
        mat=None,
        dx=None,
        dy=None,
        no_outline=None,
        tmap=None,
    ):
        smooth = self.smooth if smooth is None else smooth
        alpha = self.alpha if alpha is None else alpha
        ink = self.ink if ink is None else ink
        mat = self.mat if mat is None else mat
        if dx is not None:
            smooth, alpha, ink = self.warp_hi([smooth, alpha, ink], dx, dy)
            mat = self.warp_lo(mat, dx, dy)
            if no_outline is not None:
                no_outline = self.warp_lo(no_outline.astype(np.uint8), dx, dy).astype(bool)
        col, sil, ink_s, _ = self.lowres(smooth, alpha, ink)
        return self.shade(col, mat, sil, ink_s, no_outline, tmap), mat, sil


def fill_by_material(img, mat, hole, mats, r=8):
    """Fill `hole` (low-res) from neighbours of the same material only (no colour bleeding)."""
    out = img.copy()
    u8 = (np.clip(img, 0, 1) * 255).astype(np.uint8)
    band = ndi.binary_dilation(hole, iterations=r + 4) & ~hole
    for k in np.unique(mats[hole]):
        hk = hole & (mats == k)
        m = hk | (band & (mat != k)) | (hole & (mats != k))
        f = cv2.inpaint(u8, m.astype(np.uint8) * 255, r, cv2.INPAINT_TELEA).astype(np.float32) / 255
        out[hk] = f[hk]
    return out


# --- the parts ---------------------------------------------------------------------------------


class Parts:
    def __init__(self) -> None:
        import char_body
        import char_layers as cl

        self.px = px = Pixelizer()
        self.size = px.size
        # base.png is the spike's render without the hair that build_hair.py cut out
        split = np.asarray(Image.open(OUT / "base.png").convert("RGBA"))
        full, _, _ = px.render()
        # the parts start from exactly the spike's pixels
        body = (split[..., 3] > 0) & (px.mat != 0)
        same = (split == full).all(2) & body
        assert same.sum() > 0.9 * body.sum(), "char_parts: pixelizer differs from build_char"
        self.full = full
        self.EARS = EARS
        self.atlas: list[tuple[str, np.ndarray]] = []
        self.meta: dict = {
            "size": [px.tw, px.th],
            "layers": cl.LAYERS,
            "groups": cl.GROUPS,
            "parts": {},
            "sheets": {},
            "ears": {},
            "eyes": {},
            "mouths": {},
            "brows": {},
            "arms": {"r": {}, "l": {}},
            "fx": {},
        }
        self.mk = cl.masks(self, [e["poly"] for e in EARS.values()], split)
        self.src = cl.complete_sources(self, self.mk)
        self.body = char_body.build(self)
        yy = px.yy
        core = (bc.poly(self.size, bc.HEAD) | bc.poly(self.size, bc.FACE)) & (yy < 150)
        hair = self.mk["vis"]["crown"] | self.mk["hair"]
        attached = hair * np.clip(1 - (yy - 140) / 60, 0, 1)
        w = np.maximum(core.astype(np.float32), attached)
        self.w_head = np.clip(ndi.gaussian_filter(w, 2.0) * 1.15, 0, 1)
        self.w_body = np.clip((262 - yy) / 110, 0, 1)
        union = self.mk["sil0"] | self.body["zone"]
        for v in self.mk["full"].values():
            union |= v
        # pixels that base.png keeps (build_hair.py's fill under the hair, the skin and the
        # earbud it restores from the icon) and that no part covers: kept as they are
        free_px = (split[..., 3] > 0) & ~self.mk["ears"] & ~self.body["zone"]
        for v in self.mk["full"].values():
            free_px &= ~v
        self.face_restore = np.zeros_like(split)
        fr = self.mk["face_restore"]
        self.face_restore[fr] = split[fr]
        self.underlay = {}
        for k, m in (
            ("head_underlay", free_px & (px.yy < cl.UNDERLAY_ROW)),
            ("body_underlay", free_px & (px.yy >= cl.UNDERLAY_ROW)),
        ):
            img = np.zeros_like(split)
            img[m] = split[m]
            self.underlay[k] = img
            union |= m
        self.union = union
        self.neck_img = self.draw_neck()

    def draw_neck(self):
        from char_arms import Canvas

        cv = Canvas(self.px)
        cv.capsule((139.0, 112.0), (139.0, 150.0), 13.0, 15.0, 1, base=0.42, amp=0.2, ink=0)
        img = cv.render()
        img[~self.union] = 0
        return img

    def field(self, spec: dict, head: bool = True):
        """Displacement of a pose. head=False: only the body's own motion (shoulders) - the
        parts of the body group do not follow the head."""
        if not head:
            spec = {"shoulders": spec.get("shoulders", 0.0)}
        px = self.px
        xx, yy = px.xx, px.yy
        w = self.w_head
        dx = np.zeros_like(xx)
        dy = np.zeros_like(xx)
        t = np.radians(spec.get("tilt", 0.0))
        if t:
            ox, oy = xx - NECK[0], yy - NECK[1]
            dx += ((np.cos(t) * ox - np.sin(t) * oy) - ox) * w
            dy += ((np.sin(t) * ox + np.cos(t) * oy) - oy) * w
        dx += spec.get("hx", 0.0) * w
        dy += spec.get("hy", 0.0) * w
        p = spec.get("pitch", 0.0)
        if p:
            dy += p * w * np.clip(1 - np.maximum(0, yy - 95) / 43 * 0.6, 0.4, 1)
        sh = spec.get("shoulders", 0.0)
        if sh:
            dy += sh * self.w_body
        small = np.hypot(dx, dy) < 0.08
        dx[small] = 0
        dy[small] = 0
        return dx, dy

    def fwd(self, d, pt):
        x, y = pt
        if d is None:
            return [float(x), float(y)]
        dx, dy = d
        xi, yi = (
            int(np.clip(round(x), 0, self.px.tw - 1)),
            int(np.clip(round(y), 0, self.px.th - 1)),
        )
        return [float(x + dx[yi, xi]), float(y + dy[yi, xi])]

    def add(self, key: str, img: np.ndarray) -> dict:
        """Crop to the opaque pixels, queue for the atlas; returns {ox, oy, w, h}."""
        if key.startswith(("part.", "ear.", "arm.", "prop.")) and key.split(".")[-1] not in (
            BACK_SHEETS
        ):
            from char_unify import unify  # grey hair lines, no specks (#341, art unify)

            img = unify(img, part=True)
            if key.startswith("arm."):
                from char_unify import skin_contour

                img = skin_contour(img)
        a = img[..., 3] > 0
        if not a.any():
            return {"ox": 0, "oy": 0, "w": 0, "h": 0, "key": key}
        ys, xs = np.where(a)
        y0, y1, x0, x1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
        self.atlas.append((key, img[y0:y1, x0:x1]))
        return {"ox": int(x0), "oy": int(y0), "w": int(x1 - x0), "h": int(y1 - y0), "key": key}

    def render_part(self, k, d=None, free=False):
        """One icon part, pixelized alone (same ramps and dither as the whole), outlined only
        where it borders the outside of the figure; below the circle, the drawn body."""
        px = self.px
        if k in ("head_underlay", "body_underlay"):
            img = self.underlay[k]
            return img if d is None else px.warp_lo(img, *d)
        if k == "neck":
            return self.neck_img if d is None else px.warp_lo(self.neck_img, *d)
        P = self.src[k]
        sm, al, ink, mat = P["smooth"], P["alpha"], P["ink"], P["mat"]
        regions = self.body["regions"]
        reg = regions.get("coat_free" if (k == "coat" and free) else k)
        own = P["full"] | (reg if reg is not None else False)
        others = self.union & ~own
        fig = self.union
        if d is not None:
            sm, al, ink = px.warp_hi([sm, al, ink], *d)
            mat = px.warp_lo(mat, *d)
            others = px.warp_lo(others.astype(np.uint8), *d).astype(bool)
            fig = px.warp_lo(fig.astype(np.uint8), *d).astype(bool)
        rs = cv2.resize(sm * al[..., None], self.size, interpolation=cv2.INTER_AREA)
        a_s = cv2.resize(al, self.size, interpolation=cv2.INTER_AREA)
        col = np.clip(rs / np.maximum(a_s[..., None], 1e-3), 0, 1)
        ink_s = cv2.resize(ink, self.size, interpolation=cv2.INTER_AREA)
        sil = (a_s > 0.5) & fig
        img = px.shade(col, mat, sil, ink_s, no_outline=others)
        if k == "crown":
            # all hair: its outline is the hair's grey (char_unify; art unify 2), wherever the
            # crown's edge lands in a pose
            black = (img[..., :3] == (bc.hexrgb(bc.OUTLINE) * 255).round()).all(2)
            img[black & (img[..., 3] > 0), :3] = (bc.hexrgb(bc.RAMPS["hair"][1]) * 255).round()
        if k == "face":
            fr = self.face_restore if d is None else px.warp_lo(self.face_restore, *d)
            m = fr[..., 3] > 0
            img[m] = fr[m]
            # strand edges around the left eye: hair line colour, not the skin's (char_layers)
            st = (
                self.mk["strands"]
                if d is None
                else px.warp_lo(self.mk["strands"].astype(np.uint8), *d) > 0
            )
            skin_line = (bc.hexrgb(bc.LINE["skin"]) * 255).round().astype(int)
            brown = (
                st & (img[..., 3] > 0) & (np.abs(img[..., :3].astype(int) - skin_line).sum(2) < 6)
            )
            img[brown, :3] = (bc.hexrgb(bc.LINE["hair"]) * 255).round().astype(np.uint8)
        if reg is not None and k in ("coat", "turtleneck", "forearm_r"):
            lay = self.body["free" if free else "rest"]
            r = reg
            if d is not None:
                lay = px.warp_lo(lay, *d)
                r = px.warp_lo(reg.astype(np.uint8), *d).astype(bool)
            img[r] = lay[r]
            img[r & (lay[..., 3] == 0)] = 0
        if k == "coat":
            img = coat_marks(img, d)
            if free:
                img = open_front(img, d)
                arm = (
                    ndi.binary_dilation(
                        bc.poly(self.size, bc.HAND) | bc.poly(self.size, bc.MUG), iterations=2
                    )
                    | self.mk["full"]["forearm_r"]
                ) & self.mk["full"]["turtleneck"]  # the turtleneck is behind: no hole
                if d is not None:
                    arm = px.warp_lo(arm.astype(np.uint8), *d).astype(bool)
                img = free_islands(img, arm)
        if k == "turtleneck":
            img = knit(img, d)
        if k == "hand_r":
            img = mug_fingers(img)
        return img

    # --- parts per pose (sheets): only variants that differ from the rest pose are stored
    def build_sheets(self) -> None:
        import char_layers as cl

        names = [*cl.ICON_PARTS]
        order = [layer["id"] for layer in cl.LAYERS]
        base_imgs = {k: self.render_part(k) for k in names}
        base_imgs["coat@free"] = self.render_part("coat", free=True)
        base_anchors = {
            "eye_l": [120.5, 98.0],
            "eye_r": [156.0, 97.0],
            "mouth": [135.0, 121.5],
        }
        # the cheeks keep their rest outline in every pose (char_layers.rest_face / cheek_part):
        # the face's pixels that show at rest below the eyes are drawn again in front of the
        # side locks, moved with the head
        rest_face = cl.rest_face(self, base_imgs, order, base_anchors)
        fix_pose(base_imgs, order, {})
        for k in names:
            self.meta["parts"][k] = {"base": self.add(f"part.{k}.base", base_imgs[k])}
        self.meta["parts"]["coat"]["base@free"] = self.add(
            "part.coat.base@free", base_imgs["coat@free"]
        )
        self.base_imgs = base_imgs
        # every front sheet's parts and field, for the extras drawn per sheet (char_extras: the
        # drooped ears, aligned with the sheet's crown)
        self.sheet_imgs = {"base": base_imgs}
        self.sheet_fields = {"base": None}
        base_cheek = cl.cheek_part(base_imgs, {}, rest_face)
        self.meta["parts"]["cheek"] = {"base": self.add("cheek.base", base_cheek)}
        for name, spec in SHEETS.items():
            d = None if name == "base" else self.field(spec)
            db = None
            if d is not None:
                db = self.field(spec, head=False)
                if not (db[0].any() or db[1].any()):
                    db = None
                imgs = {}
                for k in names:
                    dk = d if k in HEAD_PARTS else db
                    imgs[k] = self.render_part(k, dk) if dk is not None else base_imgs[k].copy()
                imgs["coat@free"] = (
                    self.render_part("coat", db, free=True)
                    if db is not None
                    else base_imgs["coat@free"].copy()
                )
                fix_pose(imgs, order, spec)
                # the cheek takes the pose face's colours before the hair beside the jaw is
                # drawn into it (#447)
                cheek = cl.cheek_part(imgs, spec, rest_face)
                cl.jaw_hair(imgs, spec, rest_face)
                self.sheet_imgs[name] = imgs
                self.sheet_fields[name] = d
                for k in names:
                    img = imgs[k]
                    if not (img == base_imgs[k]).all():
                        self.meta["parts"][k][name] = self.add(f"part.{k}.{name}", img)
                if not (cheek == base_cheek).all():
                    self.meta["parts"]["cheek"][name] = self.add(f"cheek.{name}", cheek)
                img = imgs["coat@free"]
                if not (img == base_imgs["coat@free"]).all():
                    self.meta["parts"]["coat"][f"{name}@free"] = self.add(
                        f"part.coat.{name}@free", img
                    )
            anchors = {
                "eye_l": self.fwd(d, (120.5, 98.0)),
                "eye_r": self.fwd(d, (156.0, 97.0)),
                "brow_l": self.fwd(d, (119.0, 89.0)),
                "brow_r": self.fwd(d, (157.5, 88.0)),
                "mouth": self.fwd(d, (135.0, 121.5)),
                "ear_l": self.fwd(d, EARS["l"]["pivot"]),
                "ear_r": self.fwd(d, EARS["r"]["pivot"]),
                "chest": self.fwd(db if d is not None else None, (120.0, 200.0)),
                "shoulder_l": self.fwd(db if d is not None else None, (205.0, 175.0)),
                "neck": self.fwd(d, NECK),
            }
            self.meta["sheets"][name] = {
                "view": "front",
                "tilt": spec.get("tilt", 0.0),
                "spec": spec,
                "anchors": anchors,
            }

    # --- before / after and the coloured part map (debug images, not used at runtime)
    def debug_images(self, out_dir: Path) -> None:
        import char_layers as cl

        out_dir.mkdir(parents=True, exist_ok=True)
        comp = np.zeros_like(self.full)
        tint = np.zeros_like(self.full)
        colours = {
            "body_underlay": (120, 120, 140), "head_underlay": (200, 140, 200), "turtleneck": (60, 80, 220), "coat": (230, 200, 90), "neck": (240, 140, 120),
            "face": (250, 120, 160), "crown": (80, 220, 230), "forearm_r": (90, 220, 120),
            "hand_r": (40, 160, 80),
        }  # fmt: skip
        for layer in cl.LAYERS:
            k = layer["id"]
            if k in self.base_imgs:
                img = self.base_imgs[k]
                m = img[..., 3] > 0
                comp[m] = img[m]
                tint[m, :3] = (img[m, :3].astype(int) // 2 + np.array(colours[k]) // 2).astype(
                    np.uint8
                )
                tint[m, 3] = 255
        hidden = np.zeros_like(self.full)
        for k in ("turtleneck", "coat", "face"):
            img = self.base_imgs[k]
            vis = self.mk["vis"][k]
            m = (img[..., 3] > 0) & ~vis
            hidden[m] = img[m]

        def on_bg(a):
            b = np.zeros_like(a)
            b[..., :3] = (38, 42, 66)
            b[..., 3] = 255
            m = a[..., 3] > 0
            b[m] = a[m]
            return b

        row = np.concatenate([on_bg(self.full), on_bg(comp), on_bg(tint), on_bg(hidden)], 1)
        Image.fromarray(row).resize((row.shape[1] * 2, row.shape[0] * 2), Image.NEAREST).save(
            out_dir / "parts-before-after.png"
        )

    # --- ears
    def build_ears(self) -> None:
        px = self.px
        for side, e in EARS.items():
            region = ndi.binary_dilation(bc.poly(self.size, e["poly"]), iterations=1)
            base_band = ndi.binary_dilation(bc.poly(self.size, e["poly"]), iterations=4) & ~region
            base_band &= ndi.binary_dilation(
                bc.poly(self.size, e["base"] + [e["pivot"]]), iterations=3
            )
            keep = region | base_band
            out = {}
            pvx, pvy = e["pivot"]
            # the ear bends at its base: full rotation away from the base band, none in it
            w = np.clip(ndi.distance_transform_edt(~base_band) / 5.0, 0, 1).astype(np.float32)
            no_line = ndi.binary_dilation(base_band, iterations=3) & ~region
            for ang in EAR_ANGLES:
                t = np.radians(ang)
                ox, oy = px.xx - pvx, px.yy - pvy
                dx = ((np.cos(t) * ox - np.sin(t) * oy) - ox) * w
                dy = ((np.sin(t) * ox + np.cos(t) * oy) - oy) * w
                alpha = px.alpha * px.up(keep)
                img, _, _ = px.render(px.smooth, alpha, px.ink, px.mat, dx, dy, no_outline=no_line)
                entry = self.add(f"ear.{side}.{ang}", img)
                entry["ox"] -= pvx
                entry["oy"] -= pvy
                out[str(ang)] = entry
            self.meta["ears"][side] = {"angles": EAR_ANGLES, "frames": out}

    # --- face
    def build_face(self) -> None:
        for which, centre in (("l", (120.5, 98.0)), ("r", (156.0, 97.0))):
            eyes = {}
            for shape in cf.SHAPES:
                gazes = cf.GAZE if "curve" not in cf.SHAPES[shape] else {"c": (0, 0)}
                for gz in gazes:
                    img = cf.eye_sprite(which, shape, gz)
                    e = self.add(f"eye.{which}.{shape}.{gz}", img)
                    e["ox"] = round(e["ox"] - centre[0])
                    e["oy"] = round(e["oy"] - centre[1])
                    eyes[f"{shape}.{gz}"] = e
            self.meta["eyes"][which] = eyes
            brows = {}
            bc_centre = (119.0, 89.0) if which == "l" else (157.5, 88.0)
            for name in cf.BROWS:
                e = self.add(f"brow.{which}.{name}", cf.brow_sprite(which, name))
                e["ox"] = round(e["ox"] - bc_centre[0])
                e["oy"] = round(e["oy"] - bc_centre[1])
                brows[name] = e
            self.meta["brows"][which] = brows
        ox, oy = cf.MOUTH_ORIGIN
        for name in cf.MOUTHS:
            full = np.zeros((self.px.th, self.px.tw, 4), np.uint8)
            spr = cf.mouth_sprite(name)
            full[oy : oy + spr.shape[0], ox : ox + spr.shape[1]] = spr
            e = self.add(f"mouth.{name}", full)
            e["ox"] = round(e["ox"] - 135.0)
            e["oy"] = round(e["oy"] - 121.5)
            self.meta["mouths"][name] = e
        for name, spr in cf.fx_sprites().items():
            full = np.zeros((self.px.th, self.px.tw, 4), np.uint8)
            full[: spr.shape[0], : spr.shape[1]] = spr
            e = self.add(f"fx.{name}", full)
            self.meta["fx"][name] = e

    # --- back view (see char_back.py): the same layers, drawn from behind; the hanging locks
    # of the straight back sheets are the hair layer's back set (hair_back.json)
    def build_back(self) -> None:
        import char_back
        import char_profile

        base_anchors = {k: list(v) for k, v in self.meta["sheets"]["base"]["anchors"].items()}
        for name, kw in BACK_SHEETS.items():
            turn = kw.get("turn", 0)
            _, layers, info = char_back.back_view(self, **kw, split=turn == 0)
            if name == "back":
                self.meta["hairBack"] = char_back.export_hair_set(self, OUT, info)["view"]
            view = "back" if turn == 0 else "turn"
            for k, img in layers.items():
                if (img[..., 3] > 0).any():
                    self.meta["parts"].setdefault(k, {})[name] = self.add(f"part.{k}.{name}", img)
            anchors = dict(base_anchors)
            anchors["neck"] = [
                139.0 + kw.get("head_dx", 0) - 8 * turn,
                142.0 + kw.get("head_dy", 0),
            ]
            spec = {"view": view, **{k: float(v) for k, v in kw.items()}}
            sheet = {"view": view, "tilt": 0.0, "spec": spec, "anchors": anchors}
            if view == "back":
                # the typing arm follows the right shoulder
                sheet["armShift"] = [0, -round(kw.get("shoulder_r", 0.0))]
            self.meta["sheets"][name] = sheet
        for name, kw in PROFILE_SHEETS.items():
            _, layers = char_profile.profile_view(self, **kw)
            for k, img in layers.items():
                if (img[..., 3] > 0).any():
                    self.meta["parts"].setdefault(k, {})[name] = self.add(f"part.{k}.{name}", img)
            anchors = dict(base_anchors)
            anchors["neck"] = [164.0, 142.0]
            self.meta["sheets"][name] = {
                "view": "profile",
                "tilt": 0.0,
                "spec": {"view": "profile"},
                "anchors": anchors,
            }
        # the right arm on the room laptop's keys, seen from behind (typing frames)
        for name in char_back.TYPE_FRAMES:
            e = self.add(f"arm.r.{name}", char_back.back_arm(self, name))
            e["view"] = "back"
            self.meta["arms"]["r"][name] = e
        self.meta["keysAt"] = list(char_back.KEYS_AT)

    # --- arms (see char_arms.py)
    def build_arms(self) -> None:
        import char_arms

        for side, name, img, anchor in char_arms.build(self):
            e = self.add(f"arm.{side}.{name}", img)
            if anchor is not None:
                e["mug"] = anchor
            self.meta["arms"][side][name] = e
        img, top = char_arms.desk_mug(self)
        e = self.add("prop.mug_desk", img)
        e["top"] = top
        self.meta["props"] = {"mug_desk": e}
        self.meta["mugRest"] = [127, 168]

    # --- atlas
    def write(self) -> dict:
        items = sorted(self.atlas, key=lambda kv: -kv[1].shape[0])
        width = 1024
        x = y = row_h = 0
        placed = {}
        for key, img in items:
            h, w = img.shape[:2]
            if x + w > width:
                x, y, row_h = 0, y + row_h, 0
            placed[key] = (x, y)
            x += w
            row_h = max(row_h, h)
        height = y + row_h
        atlas = np.zeros((height, width, 4), np.uint8)
        for key, img in items:
            ax, ay = placed[key]
            atlas[ay : ay + img.shape[0], ax : ax + img.shape[1]] = img

        def fix(node):
            if isinstance(node, dict):
                if "key" in node and node["key"] in placed:
                    node["ax"], node["ay"] = placed[node.pop("key")]
                elif "key" in node:
                    node.pop("key")
                for v in node.values():
                    fix(v)

        fix(self.meta)
        self.meta["atlas"] = [width, height]
        self.meta["palette"] = palette()
        self.meta["breathRow"] = BREATH_ROW
        save_indexed(atlas, OUT / "parts.png")
        (OUT / "parts.json").write_text(
            json.dumps(self.meta, separators=(",", ":")) + "\n", encoding="utf-8"
        )
        return {"atlas": [width, height], "parts": len(items)}


# 03 / 06: the lab coat is worn — a few soft stains and short creases, drawn so that they show
# at this dot density (a one-step stain vanished in the coat's own mottling; a darker single
# pixel would be a speck): sheet px of the rest pose, moved with the body in a pose.
STAINS = [(62, 250, 2.6), (75, 263, 1.8), (182, 244, 2.2), (208, 262, 2.8), (228, 232, 1.9),
          (196, 210, 1.6)]  # fmt: skip
CREASES = [((40, 236), (46, 244)), ((48, 246), (52, 252)), ((214, 214), (218, 222)),
           ((222, 226), (225, 233)), ((168, 258), (173, 263)), ((190, 252), (193, 258)),
           # art unify 4: longer folds where the coat bends — under the right lapel, the left
           # sleeve's elbow, the side seams (soft line, 5-9 px)
           ((176, 206), (181, 214), (183, 222)), ((58, 228), (63, 233), (70, 236)),
           ((30, 252), (36, 258), (44, 262)), ((232, 238), (236, 246), (237, 254)),
           ((204, 232), (208, 240)), ((150, 266), (156, 271), (163, 273))]  # fmt: skip


def coat_marks(img, d=None):
    """Stains (a core two coat steps down, a dithered ring one step down) and short creases
    (2-3 px strokes of the coat's soft line), only on the light coat."""
    out = img.copy()
    h, w = img.shape[:2]
    coat = [(bc.hexrgb(c) * 255).round().astype(np.uint8) for c in bc.RAMPS["coat"]]
    rgb = img[..., :3]
    level = np.full((h, w), -1, np.int8)
    for i, c in enumerate(coat):
        level[(rgb == c).all(2) & (img[..., 3] > 0)] = i
    yy, xx = np.mgrid[0:h, 0:w]

    def moved(x, y):
        if d is None:
            return x, y
        xi, yi = int(np.clip(round(x), 0, w - 1)), int(np.clip(round(y), 0, h - 1))
        return x + float(d[0][yi, xi]), y + float(d[1][yi, xi])

    for x, y, r in STAINS:
        cx, cy = moved(x, y)
        ang = np.arctan2(yy - cy, xx - cx)
        rr = r * (1 + 0.22 * np.sin(3 * ang + x) + 0.12 * np.sin(5 * ang + y))
        dist = np.hypot(xx - cx, (yy - cy) * 1.15)
        light = level >= 3
        core = light & (dist <= rr * 0.55)
        ring = light & (dist <= rr) & ~core & ((xx + yy) % 2 == 0)
        out[core, :3] = coat[2]
        out[ring, :3] = np.array(coat)[np.maximum(level[ring] - 1, 0)]
    for crease in CREASES:
        for (x0, y0), (x1, y1) in itertools.pairwise(crease):
            a, b = np.array(moved(x0, y0)), np.array(moved(x1, y1))
            n = int(np.ceil(np.hypot(*(b - a)))) + 1
            for t in np.linspace(0, 1, n):
                px_, py_ = np.round(a + (b - a) * t).astype(int)
                if 0 <= px_ < w and 0 <= py_ < h and level[py_, px_] >= 2:
                    out[py_, px_, :3] = coat[1]
                    # a light edge beside the fold (it catches the light on its upper side)
                    if 0 <= py_ - 1 < h and level[py_ - 1, px_] >= 2 and (px_ + py_) % 2 == 0:
                        out[py_ - 1, px_, :3] = coat[4]
    return out


KNIT_PITCH = 3  # px between the turtleneck's ribs


def knit(img, d=None):
    """The turtleneck's knit (03 / 06: a ribbed navy knit): every third column a step darker
    on alternating rows (a rib), the column phase moved with the body in a pose; only on its
    lighter steps (the shadow stays smooth)."""
    out = img.copy()
    h, w = img.shape[:2]
    navy = [(bc.hexrgb(c) * 255).round().astype(np.uint8) for c in bc.RAMPS["navy"]]
    rgb = img[..., :3]
    level = np.full((h, w), -1, np.int8)
    for i, c in enumerate(navy):
        level[(rgb == c).all(2) & (img[..., 3] > 0)] = i
    yy, xx = np.mgrid[0:h, 0:w]
    sx = xx - (np.round(d[0]).astype(int) if d is not None else 0)
    sy = yy - (np.round(d[1]).astype(int) if d is not None else 0)
    rib = (sx % KNIT_PITCH == 0) & (sy % 2 == 0) & (level >= 2)
    out[rib, :3] = np.array(navy)[level[rib] - 1]
    # the stitch between two ribs catches a little light
    lit = (sx % KNIT_PITCH == 1) & (sy % 4 == 1) & (level == 2)
    out[lit, :3] = navy[3]
    return out


def mug_fingers(img):
    """The hand round the mug (the icon's): softer gaps between the fingers (the skin line one
    step lighter where it runs between two fingers) and a light edge on each finger's top."""
    out = img.copy()
    skin = [(bc.hexrgb(c) * 255).round().astype(np.uint8) for c in bc.RAMPS["skin"]]
    line = (bc.hexrgb(bc.LINE["skin"]) * 255).round().astype(np.uint8)
    rgb = img[..., :3]
    a = img[..., 3] > 0
    is_skin = np.zeros(a.shape, bool)
    for c in skin[2:]:
        is_skin |= (rgb == c).all(2)
    dark = a & ((rgb == line).all(2) | (rgb == skin[0]).all(2) | (rgb == skin[1]).all(2))
    up = np.zeros_like(is_skin)
    up[1:] = is_skin[:-1]
    down = np.zeros_like(is_skin)
    down[:-1] = is_skin[1:]
    gap = dark & up & down  # a dark row between two fingers
    out[gap & (rgb == line).all(2), :3] = skin[1]
    out[gap & (rgb == skin[1]).all(2), :3] = skin[2]
    top = is_skin & np.roll(dark, 1, axis=0) & ~(rgb == skin[4]).all(2)
    out[top & ((np.mgrid[0 : a.shape[0], 0 : a.shape[1]][1]) % 2 == 0), :3] = skin[4]
    return out


def open_front(img, d=None):
    """The free coat (the mug arm lifted): clear where the coat hangs open (char_layers
    OPEN_FRONT_FREE: the turtleneck shows there), its new edge a soft coat line (06: no box)."""
    import char_layers as cl

    h, w = img.shape[:2]
    m = bc.poly((w, h), cl.OPEN_FRONT_FREE)
    if d is not None:
        m = cv2.remap(m.astype(np.uint8), *[np.mgrid[0:h, 0:w][::-1][i].astype(np.float32) - d[i]
                      for i in (0, 1)], cv2.INTER_NEAREST) > 0  # fmt: skip
    out = img.copy()
    out[m] = 0
    edge = (out[..., 3] > 0) & ndi.binary_dilation(m) & ~m
    out[edge, :3] = (bc.hexrgb(bc.RAMPS["coat"][1]) * 255).round()
    return out


FREE_ISLAND = 100  # px: a piece of the coat this small, on its own, under the mug arm


def free_islands(img, arm):
    """(#341, art fix) The free coat (the mug arm away): small pieces of the coat, cut off from the
    rest of it, that lie (all of them) under where the mug arm is at rest, on the turtleneck
    (`arm`), go. They are the icon's light hair strands over the sweater, classified as coat and
    completed under the hand and the mug; with the mug lifted they floated on the turtleneck below
    it (light blobs, a dark line). The turtleneck is completed there (char_layers); pieces seen at
    rest (above the mug) stay."""
    out = img.copy()
    a = out[..., 3] > 0
    # 1 px wide strands sticking out of the coat's edge there go too (a thin light line)
    thin = a & ~ndi.binary_opening(a, structure=np.ones((3, 3))) & arm
    out[thin] = 0
    a &= ~thin
    labels, n = ndi.label(a, structure=np.ones((3, 3)))
    if not n:
        return out
    flat = labels.ravel()
    sizes = np.bincount(flat, minlength=n + 1)
    on_arm = np.bincount(flat, weights=arm.ravel(), minlength=n + 1)
    drop = (sizes <= FREE_ISLAND) & (on_arm == sizes)
    drop[0] = False
    out[drop[labels]] = 0
    return out


def fix_specks(imgs: dict, order: list, hair=None) -> None:
    """Composite the parts of one pose in layer order (and the hair layer's parts at their rest
    offset for the pose, `hair`: (rgba, owned) from hair_overlay), find the isolated dark pixels
    and the black seams inside the hair there (char_unify) and recolour each in the part that
    draws it, from that part's own neighbours (in place). Pixels of the hair layer are shared by
    every pose and are not changed here."""
    from char_unify import hair_lines, specks

    first = next(iter(imgs.values()))
    comp = np.zeros_like(first)
    owner = np.full(first.shape[:2], -1, np.int16)
    keys = [k for k in order if k in imgs]
    for i, k in enumerate(keys):
        m = imgs[k][..., 3] > 0
        comp[m] = imgs[k][m]
        owner[m] = i
    if hair is not None:
        h_rgba, h_back = hair
        arm = np.zeros(owner.shape, bool)
        for k in ("forearm_r", "hand_r"):
            if k in imgs:
                arm |= imgs[k][..., 3] > 0
        front = (h_rgba[..., 3] > 0) & ~h_back & ~arm
        back = (h_rgba[..., 3] > 0) & h_back & (owner < 0)
        comp[front | back] = h_rgba[front | back]
        owner[front | back] = -2
    fixed = specks(hair_lines(comp))
    hgt, wid = owner.shape
    for y, x in zip(*np.nonzero((fixed != comp).any(2)), strict=True):
        o = owner[y, x]
        if o < 0:
            continue
        col = fixed[y, x]
        # the colour from the same part's neighbours where it has any (no hair colour on the coat)
        same = []
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                yy, xx = y + dy, x + dx
                if (dy or dx) and 0 <= yy < hgt and 0 <= xx < wid and owner[yy, xx] == o:
                    same.append(tuple(comp[yy, xx]))
        if same:
            vals, counts = np.unique(np.array(same), axis=0, return_counts=True)
            luma = vals[:, :3].astype(int) @ np.array([299, 587, 114])
            lighter = luma >= int(col[:3].astype(int) @ np.array([299, 587, 114]))
            if lighter.any():  # the most common of its own lighter neighbours
                vals, counts = vals[lighter], counts[lighter]
            col = vals[np.argmax(counts)]
        imgs[keys[o]][y, x] = col


def hair_overlay(spec: dict):
    """The hair layer's parts at rest for a pose (hair_layer.js: each segment moves by the pose's
    displacement at its pivot, char_rig.poseDisplacement), as one picture and the mask of the
    back parts. None for a view without the front set."""
    meta = json.loads((OUT / "hair.json").read_text(encoding="utf-8"))
    atlas = np.asarray(Image.open(OUT / "hair.png").convert("RGBA"))
    w, h = meta["size"]
    out = np.zeros((h, w, 4), np.uint8)
    back = np.zeros((h, w), bool)
    taken = np.zeros((h, w), bool)
    for sg in meta["segments"]:
        dx, dy = pose_displacement(spec, *sg["pivot"])
        ox, oy = round(dx), round(dy)
        x0, y0, sw, sh = sg["box"]
        ax, ay = sg["at"]
        spr = atlas[ay : ay + sh, ax : ax + sw]
        for yy in range(sh):
            ty = y0 + yy + oy
            if not 0 <= ty < h:
                continue
            for xx in range(sw):
                tx = x0 + xx + ox
                if 0 <= tx < w and spr[yy, xx, 3] > 127 and not taken[ty, tx]:
                    out[ty, tx] = spr[yy, xx]
                    taken[ty, tx] = True
                    back[ty, tx] = sg["z"] == "back"
    return out, back


def pose_displacement(spec: dict, x: float, y: float):
    """char_rig.js poseDisplacement (the head about the neck, the shoulders), for a pivot."""
    nx, ny = NECK
    t = np.radians(spec.get("tilt", 0.0))
    ox, oy = x - nx, y - ny
    dx = np.cos(t) * ox - np.sin(t) * oy - ox + spec.get("hx", 0.0)
    dy = np.sin(t) * ox + np.cos(t) * oy - oy + spec.get("hy", 0.0)
    p = spec.get("pitch", 0.0)
    if p:
        dy += p * min(1.0, max(0.4, 1 - max(0.0, y - 95) / 43 * 0.6))
    sh = spec.get("shoulders", 0.0)
    if sh:
        dy += sh * min(1.0, max(0.0, (262 - y) / 110))
    return float(dx), float(dy)


def fix_pose(imgs: dict, order: list, spec=None) -> None:
    """The parts of one pose drawn together, as with the mug arm (coat) and with an arm sprite
    (the free coat, no forearm / hand), with the hair layer at rest over them: a pixel of one
    part left alone among lighter pixels of the others (a speck), or a black seam inside the
    hair, takes the look of that part's neighbours."""
    hair = hair_overlay(spec or {})
    rest = {k: v for k, v in imgs.items() if k != "coat@free"}
    fix_specks(rest, order, hair)
    free = {k: v for k, v in rest.items() if k not in ("coat", "forearm_r", "hand_r")}
    free["coat"] = imgs["coat@free"]
    fix_specks(free, order, hair)


def palette() -> dict:
    """The named colours the character is drawn with (tests/stage_art_lint_test.cjs: every
    pixel of the character's assets is one of them)."""
    out = {f"{k}{i}": c for k, ramp in bc.RAMPS.items() for i, c in enumerate(ramp)}
    out.update({f"line_{k}": c for k, c in bc.LINE.items()})
    out["outline"] = bc.OUTLINE
    out.update({k.lower(): v for k, v in cf.NAMED.items()})
    return dict(sorted(out.items()))


def save_indexed(rgba: np.ndarray, path: Path) -> None:
    """Save as an 8-bit palette PNG with per-entry alpha (exact colours, no dithering)."""
    flat = rgba.reshape(-1, 4)
    cols, inv = np.unique(flat, axis=0, return_inverse=True)
    if len(cols) > 256:
        Image.fromarray(rgba).save(path, optimize=True)
        return
    im = Image.fromarray(inv.reshape(rgba.shape[:2]).astype(np.uint8), "P")
    pal = np.zeros((256, 3), np.uint8)
    pal[: len(cols)] = cols[:, :3]
    im.putpalette(pal.reshape(-1).tolist())
    im.save(path, optimize=True, transparency=bytes(cols[:, 3].tolist()))


def build() -> dict:
    p = Parts()
    p.build_sheets()
    p.build_ears()
    p.build_face()
    p.build_arms()
    p.build_back()
    p.debug_images(ROOT_DOCS)
    out = p.write()
    import char_extras  # ear variants, the tail, the earbud: their own files (extras.*)

    out.update(char_extras.build(p, OUT))
    import char_fx  # face effects (blush, sweat, fluster lines, anger, sparkle, tear): fx.*

    out.update(char_fx.build(OUT, p))
    import char_poses  # whole-pose arm sprites (arms crossed, ponder with the arm across): poses.*

    out.update(char_poses.build(p, OUT))
    return out


if __name__ == "__main__":
    print(json.dumps(build()))
