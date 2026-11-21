"""The binder as a sprite stack (#341). Imported by build_gadgets.py.

`folder.s<n>.<k>`: n = 1..3 binders standing side by side (the number of memory / file reads
running at once), k = 0..3 how far the group has swung out of the shelf (the runtime's
`folder.out` slide, 0..7 px, picks k). All frames share one size. The spine faces the viewer;
a swung binder shows its side and its page-edge top.
"""

from __future__ import annotations

from sprite_stack import render_stack, shade_step

COUNTS = (1, 2, 3)
SWINGS = 4
SWING_STEP = -0.11  # radians per step: the front turns to the right, like a binder pulled out
SIZE = (31, 31)
CENTRE = (
    5,
    28,
)  # the first binder's foot centre, so its left edge sits where the flat sprite's did
LAYERS = 26
SQUASH = 0.55
HEIGHTS = (25, 23, 21)
# (spine, light edge, dark edge) per binder
COLOURS = (("cy1", "cy2", "cy0"), ("gr1", "gr2", "gr0"), ("am1", "am2", "am0"))


def make_voxel(theta: float, n: int):
    def voxel(x: int, y: int, z: int):
        for i in range(n):
            lx = x - i * 8
            if abs(lx) > 3 or abs(y) > 2 or z >= HEIGHTS[i]:
                continue
            spine, light, dark = COLOURS[i]
            top = z == HEIGHTS[i] - 1
            if top:
                return ("pap1" if (lx + y) % 2 else "pap2", None, 0)
            if y == 2:  # spine, facing the viewer
                if abs(lx) <= 1 and 9 <= z <= 14:  # label with two text lines
                    return ("k6" if z in (11, 13) else "pap2", None, 0)
                if lx == -3:
                    return (light, None, 0)
                if lx == 3:
                    return (dark, None, 0)
                if z in (4, 5, 20):
                    return ("k1", None, 0)  # bands
                return (spine, None, 0)
            # side / back faces: shaded by where they face after the swing
            if abs(lx) == 3:
                s = shade_step(1.0 if lx > 0 else -1.0, 0.0, theta)
                return (light if s > 0 else dark if s < 0 else spine, None, 0)
            return (dark, None, 0)
        return None

    return voxel


def frames(sprite_cls) -> dict:
    out = {}
    for n in COUNTS:
        for k in range(SWINGS):
            theta = k * SWING_STEP
            sp = render_stack(
                sprite_cls,
                make_voxel(theta, n),
                LAYERS,
                theta,
                SIZE,
                CENTRE,
                squash=SQUASH,
                extent=28,
            )
            sp.outline()
            out[f"folder.s{n}.{k}"] = sp
    return out
