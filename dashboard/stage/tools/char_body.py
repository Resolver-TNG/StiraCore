"""Body below the icon's circle crop (#341): the coat, turtleneck and sleeves drawn in code.

The owner's icon is a circle crop, so the lower corners of the figure (the sleeves, the
coat's front panels below the circle, the forearm of the mug hand going down to the elbow)
are not in it. The spike filled them by stretching edge pixels. Here they are *drawn*: the
regions of a simple body model (sleeve and upper arm on each side, the coat's front panels,
the turtleneck between them) are filled with the coat / navy ramps, cylinder shading, creases
and seams, and their tone is blended into the icon's own tone where they meet the circle, so
the join does not show. Hair is not drawn here (the hair layer extends it); hair strands that
reach the circle edge end in short pointed tips.

Two layers: ``rest`` (with the mug hand's forearm sleeve) and ``free`` (the arm away, for
motions that lift the mug or type).
"""

from __future__ import annotations

import build_char as bc
import cv2
import numpy as np
from char_arms import COAT, NAVY, Canvas
from scipy import ndimage as ndi

CIRCLE = (139.0, 139.0, 138.5)
WRIST = (86.0, 230.0)
ELBOW = (22.0, 302.0)


def _line_x(p0, p1, y):
    (x0, y0), (x1, y1) = p0, p1
    return x0 + (x1 - x0) * (y - y0) / (y1 - y0)


def silhouette_lines(parts):
    """The outer edges of the figure (left / right), fitted on the icon's own outline."""
    px = parts.px
    _, sil, _, _ = px.lowres(px.smooth, px.alpha, px.ink, extend=False)
    cx, cy, r = CIRCLE
    out = []
    for side in (-1, 1):
        pts = []
        for y in range(170, 231):
            xs = np.where(sil[y])[0]
            if not len(xs):
                continue
            x = xs.min() if side < 0 else xs.max()
            if np.hypot(x - cx, y - cy) < r - 3:
                pts.append((y, x))
        ys, xs = np.array(pts).T
        out.append(np.polyfit(ys, xs, 1))
    return out, sil


def tone_of(parts, img, mat):
    """Lightness 0..1 of each pixel on its material's ramp, smoothed (no dither)."""
    t = np.full(mat.shape, np.nan, np.float32)
    rgb = img[..., :3].astype(int)
    for k, name in enumerate(bc.MATS):
        ramp = [(bc.hexrgb(c) * 255).round().astype(int) for c in bc.RAMPS[name]]
        for i, c in enumerate(ramp):
            hit = (mat == k) & (np.abs(rgb - c).sum(2) <= 3) & (img[..., 3] > 0)
            t[hit] = i / (len(ramp) - 1)
    out = np.full_like(t, np.nan)
    for k in range(len(bc.MATS)):
        m = (mat == k) & ~np.isnan(t)
        if not m.any():
            continue
        num = ndi.gaussian_filter(np.where(m, t, 0), 2.5)
        den = ndi.gaussian_filter(m.astype(np.float32), 2.5)
        out[m] = (num / np.maximum(den, 1e-3))[m]
    return out


def build(parts):
    """Returns (rest, free, zone): two RGBA layers and the mask where they replace the sheet."""
    px = parts.px
    s = bc.F
    (fl, fr), src_sil = silhouette_lines(parts)
    yy, xx = px.yy, px.xx
    cx, cy, r = CIRCLE
    inside = (xx - cx) ** 2 + (yy - cy) ** 2 <= r * r
    xl = np.polyval(fl, yy)
    xr = np.polyval(fr, yy)
    body = (xx >= np.floor(xl)) & (xx <= np.ceil(xr)) & (yy >= 150)
    zone = body & (~inside | ~src_sil) & (yy >= 196)
    tone = tone_of(parts, parts.full, px.mat)
    # nearest icon pixel (inside the circle and the figure) for the tone blend
    known = inside & src_sil & ~np.isnan(tone)
    dist, (iy, ix) = ndi.distance_transform_edt(~known, return_indices=True)
    near_t = tone[iy, ix]
    near_m = px.mat[iy, ix]
    layers = []
    regions = {}
    for with_arm in (True, False):
        cv = Canvas(px)
        Y, X = cv.Y / s, cv.X / s
        XL = cv2.resize(xl.astype(np.float32), (px.w0, px.h0), interpolation=cv2.INTER_LINEAR)
        XR = cv2.resize(xr.astype(np.float32), (px.w0, px.h0), interpolation=cv2.INTER_LINEAR)
        # (art fix) the free layer is drawn from higher up than it is used (y >= 190): its top
        # edge at 190 was a soft line straight across the coat (the icon's coat is above it)
        region = (X >= XL - 0.5) & (X <= XR + 0.5) & (Y >= (190 if with_arm else 150))
        tl = 100.0
        tr = 150.0 - 0.25 * (Y - 240.0)
        sr = _line_x((214, 190), (230, 278), Y)
        neck = (X > tl) & (X < tr) & region
        right_panel = (X >= tr) & (X < sr) & region
        right_sleeve = (X >= sr) & region
        left = (X <= tl) & region
        # forearm sleeve of the mug hand: a band from the wrist down to the elbow
        a, b = np.array(WRIST), np.array(ELBOW)
        d = b - a
        L = float(np.hypot(*d))
        u = d / L
        n = np.array([-u[1], u[0]])
        along = (X - a[0]) * u[0] + (Y - a[1]) * u[1]
        across = (X - a[0]) * n[0] + (Y - a[1]) * n[1]
        fore = left & (along > -4) & (np.abs(across) <= 16.0) if with_arm else np.zeros_like(left)
        upper = left & (X < _line_x((60, 200), (40, 278), Y)) & ~fore
        left_panel = left & ~fore & ~upper
        # tones: coat panels, sleeves (cylinder across), turtleneck, darker toward the bottom
        down = np.clip((Y - 200) / 80.0, 0, 1)
        t = np.full(Y.shape, np.nan, np.float32)
        mat = np.zeros(Y.shape, np.uint8)
        q_r = np.clip((X - (sr + XR) / 2) / np.maximum((XR - sr) / 2, 1), -1, 1)
        q_f = np.clip(across / 16.0, -1, 1)
        q_u = np.clip((X - (XL + _line_x((60, 200), (40, 278), Y)) / 2) / 10.0, -1, 1)
        # soft folds of the cloth (long diagonal waves), so the ramps dither like the icon's
        fold = 0.05 * np.sin((X * 0.35 + Y * 0.9) * 0.18) + 0.04 * np.sin(
            (X * -0.6 + Y) * 0.31 + 1.7
        )
        for m, tv, k in (
            (neck, 0.5 - 0.15 * down, NAVY),
            (left_panel, 0.84 - 0.16 * down - 0.08 * np.clip((tl - X) / 30, 0, 1) + fold, COAT),
            (right_panel, 0.86 - 0.14 * down + fold, COAT),
            (
                right_sleeve,
                0.74 + 0.14 * np.sqrt(np.clip(1 - q_r**2, 0, 1)) - 0.12 * q_r - 0.12 * down + fold,
                COAT,
            ),
            (upper, 0.62 + 0.14 * np.sqrt(np.clip(1 - q_u**2, 0, 1)) - 0.1 * down + fold, COAT),
            (
                fore,
                0.78
                + 0.16 * np.sqrt(np.clip(1 - q_f**2, 0, 1))
                + 0.08 * q_f
                - 0.1 * np.clip(along / 80, 0, 1)
                + fold,
                COAT,
            ),
        ):
            t[m] = (tv if np.ndim(tv) else np.full(Y.shape, tv))[m]
            mat[m] = k
        cover = region & ~np.isnan(t)
        # blend into the icon's own tone where the drawing meets the circle
        nt = cv2.resize(
            np.nan_to_num(near_t, nan=-1).astype(np.float32),
            (px.w0, px.h0),
            interpolation=cv2.INTER_NEAREST,
        )
        nm = cv2.resize(near_m.astype(np.uint8), (px.w0, px.h0), interpolation=cv2.INTER_NEAREST)
        dd = cv2.resize(dist.astype(np.float32), (px.w0, px.h0), interpolation=cv2.INTER_LINEAR)
        w = np.exp(-dd / 7.0)
        ok = (nt >= 0) & (nm == mat)
        t = np.where(ok, t + (nt - t) * w, t)
        cv.t[cover] = t[cover]
        cv.mat[cover] = mat[cover]
        cv.cov[cover] = True
        # seams and creases (continuing the icon's lines across the circle)
        ink = np.zeros(Y.shape, bool)
        ink |= region & (np.abs(X - tl) < 0.45)  # coat front edge (left)
        ink |= region & (np.abs(X - tr) < 0.45)  # coat front edge (right)
        ink |= region & (np.abs(X - sr) < 0.4) & (Y > 200)  # right sleeve seam
        if with_arm:
            ink |= (
                left & (np.abs(np.abs(across) - 16.0) < 0.45) & (along > 0)
            )  # forearm sleeve edges
            for k0, k1, off in ((30, 52, -6), (46, 70, 5), (58, 84, -2)):
                ink |= (
                    fore
                    & (along > k0)
                    & (along < k1)
                    & (np.abs(across - off - (along - k0) * 0.25) < 0.4)
                )
        ink |= upper & (np.abs(X - _line_x((60, 200), (40, 278), Y)) < 0.4)

        for y0, x0, dx in ((214, 222, 0.35), (238, 230, 0.3), (256, 220, 0.4)):
            ink |= (
                right_sleeve & (np.abs((X - x0) - (Y - y0) * dx) < 0.4) & (Y > y0) & (Y < y0 + 22)
            )
        for y0, x0 in ((226, 170), (252, 184), (246, 120)):
            ink |= (
                (right_panel | neck)
                & (np.abs((X - x0) + (Y - y0) * 0.15) < 0.35)
                & (Y > y0)
                & (Y < y0 + 18)
            )
        cv.ink[ink & cover] = 1
        lay = cv.render()
        if not with_arm:
            # the arm is away: the panel's top and inner edges lie on the turtleneck, inside the
            # figure (a black box edge there read as a cut-out); a soft coat line instead
            inner = ndi.binary_erosion(src_sil | body, iterations=2)
            black = (lay[..., :3] == (bc.hexrgb(bc.OUTLINE) * 255).round()).all(2)
            lay[black & inner & (lay[..., 3] > 0), :3] = (
                bc.hexrgb(bc.RAMPS["coat"][1]) * 255
            ).round()
        layers.append(lay)
        if with_arm:
            low = lambda m: (
                cv2.resize(m.astype(np.float32), px.size, interpolation=cv2.INTER_AREA) > 0.5
            )
            regions = {"turtleneck": low(neck) & zone, "forearm_r": low(fore) & zone,
                       "coat": low(left_panel | right_panel | right_sleeve | upper) & zone}  # fmt: skip
        else:
            # (#341, art fix) the free coat is the drawn body where the mug arm was (the left of
            # the picture, her right) *and* the rest pose's drawn body on the right of the
            # picture (her left: the panel, the sleeve and the hem below the circle): with only
            # the left, every arm sprite cut the coat's hem off on the right (the room showed
            # through below her left sleeve)
            right = (
                cv2.resize(
                    (right_panel | right_sleeve).astype(np.float32),
                    px.size,
                    interpolation=cv2.INTER_AREA,
                )
                > 0.5
            )
            regions["coat_free"] = (
                cv2.resize(
                    (left & ~neck & (Y >= 190)).astype(np.float32),
                    px.size,
                    interpolation=cv2.INTER_AREA,
                )
                > 0.5
            ) | (right & zone)
    rest, free = layers
    # ... drawn as at rest there (the free layer only differs where the mug arm was)
    keep = regions["coat_free"] & ~(
        cv2.resize(left.astype(np.float32), px.size, interpolation=cv2.INTER_AREA) > 0.5
    )
    free = free.copy()
    free[keep] = rest[keep]
    tips = hair_tips(parts, zone)
    return {"rest": rest, "free": free, "zone": zone, "regions": regions, "tips": tips}


def hair_tips(parts, zone):
    """Strands that reach the circle edge end in short points instead of a cut."""
    full = parts.full
    mat = parts.px.mat
    h, w = mat.shape
    out = np.zeros_like(full)
    edge = ndi.binary_dilation(zone) & ~zone & (mat == 0) & (full[..., 3] > 0)
    labels, n = ndi.label(edge)
    dark = (bc.hexrgb(bc.LINE["hair"]) * 255).round().astype(np.uint8)
    for i in range(1, n + 1):
        ys, xs = np.where(labels == i)
        if len(xs) < 2:
            continue
        x0, x1 = xs.min(), xs.max()
        y0 = ys.max()
        cxm = (x0 + x1) / 2
        half = (x1 - x0 + 1) / 2
        length = int(2 + half * 1.6 + (int(cxm) * 7 % 3))
        col = full[ys[np.argmax(ys)], xs[np.argmax(ys)]]
        for dy in range(1, length + 1):
            hw = half * (1 - dy / (length + 1))
            for x in range(int(np.floor(cxm - hw)), int(np.ceil(cxm + hw)) + 1):
                y = y0 + dy
                if 0 <= y < h and 0 <= x < w and zone[y, x]:
                    edge_px = abs(x - cxm) > hw - 1 or dy == length
                    out[y, x, :3] = dark if edge_px else col[:3]
                    out[y, x, 3] = 255
    return out
