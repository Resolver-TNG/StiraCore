"""The warning beacon as a sprite stack (spike, #341). Imported by build_gadgets.py.

A base, a wire-guarded red dome and, inside it, a reflector paddle that turns. 16 baked paddle
angles `beacon.s<k>` (k/16 of a turn = the runtime's `beacon.spin` phase, so the paddle and the
shader's sweeping glow stay in step: dome pixels carry their world azimuth in R, region
"beacon", exactly like the flat sprite's lens). Same size for every frame.
"""

from __future__ import annotations

import math

from sprite_stack import render_stack, shade_step

HEADINGS = 16
SIZE = (17, 19)
CENTRE = (8, 14)
LAYERS = 11
SQUASH = 0.55
BASE = ["k3", "k4", "k5", "k6", "k7", "k8", "k9"]
DOME_R = {3: 5.7, 4: 5.6, 5: 5.4, 6: 5.1, 7: 4.6, 8: 3.8, 9: 2.6, 10: 1.2}


def make_voxel(theta: float):
    c, s = math.cos(theta), math.sin(theta)

    def azimuth(x: int, y: int) -> int:
        wx, wy = x * c - y * s, x * s + y * c
        return int(((math.atan2(wy, wx) - math.pi / 2) / (2 * math.pi)) % 1.0 * 255)

    def base_col(k: int, nx: float, ny: float) -> str:
        return BASE[max(0, min(len(BASE) - 1, k + shade_step(nx, ny, theta)))]

    def voxel(x: int, y: int, z: int, u: int, v: int):
        r = math.hypot(x, y)
        nx, ny = (x / r, y / r) if r else (0.0, 1.0)
        # dome shell: a wire guard seen as a 50% dither, so the paddle shows through
        rad = DOME_R.get(z)
        if rad and r <= rad and (r > rad - 1.4 or z == 10) and (z >= 8 or (u + v) % 2 == 0):
            t = (10 - z) / 7
            from build_gadgets import ramp_pick

            col = ramp_pick(["rd1", "rd0", "k4"], t, x, y)
            return (col, "beacon", azimuth(x, y))
        # paddle (turns with the frame): reflector bowl on its +y end, weight on the other
        if 4 <= z <= 8:
            if abs(x) <= 0 and -4 <= y <= 4:
                return ("rd2" if y > 0 else "k6", None, 0)
            if abs(x) <= 1 and 3 <= y <= 5 and z in (5, 6, 7):
                return ("rd3" if z == 6 else "rd2", None, 0)
        if 3 <= z <= 9 and x == 0 and y == 0:
            return ("k10", None, 0)
        # base: foot (z0-1, r 7), neck (z2, r 6)
        if z in (0, 1) and r <= 7.0:
            top = z == 1
            return (base_col(2 if z == 0 else 3, nx, ny) if not top or r > 6.2 else "k7", None, 0)
        if z == 2 and r <= 6.2:
            return ("k8" if r > 5.2 else "k7", None, 0)
        return None

    return voxel


def frames(sprite_cls) -> dict:
    out = {}
    for k in range(HEADINGS):
        theta = k * 2 * math.pi / HEADINGS
        sp = render_stack(
            sprite_cls, make_voxel(theta), LAYERS, theta, SIZE, CENTRE, squash=SQUASH, pixel=True
        )
        sp.outline()
        out[f"beacon.s{k}"] = sp
    return out
