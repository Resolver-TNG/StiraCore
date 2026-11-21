"""Sprite stacking for Stage props (spike, #341).

A prop is a stack of flat layers (z = 0 is the bottom). To draw it from a heading angle we do not
rotate the finished picture: every output pixel looks down through the layers (top first),
rotates its position into the prop's own frame and picks the first layer that has a pixel
there; each layer sits `lift` px higher than the one below it. The result is pixel art with a
solid look at any heading, and every frame is built with the lab palette and the same
effect-region ids as the flat sprites (so the runtime shader tints the visor, the thruster, ...).

Frames are baked offline (deterministic, integer pixels, art-lint friendly); nothing here
runs at runtime. A prop is a function `voxel(x, y, z) -> (colour, region, r) | None` in the prop's
own frame: +y is its front, x is to its right, integers around (0, 0).
"""

from __future__ import annotations

import math
from collections.abc import Callable

Voxel = tuple  # (colour name, region name | None, r value)
VoxelFn = Callable[[int, int, int], "Voxel | None"]


def render_stack(
    sprite_cls,
    voxel: VoxelFn,
    layers: int,
    theta: float,
    size: tuple[int, int],
    centre: tuple[int, int],
    lift: float = 1.0,
    squash: float = 0.8,
    extent: int = 12,
    pixel: bool = False,
):
    """Draw one frame. `sprite_cls` is build_gadgets.Sprite; `centre` is the prop's (0,0,0)
    on the canvas; `theta` turns the prop (0 = front towards the viewer)."""
    w, h = size
    cx, cy0 = centre
    sp = sprite_cls(w, h)
    c, s = math.cos(theta), math.sin(theta)
    for v in range(h):
        for u in range(w):
            for z in range(layers - 1, -1, -1):
                dx = u - cx
                dy = (v - (cy0 - z * lift)) / squash
                lx = dx * c + dy * s
                ly = -dx * s + dy * c
                ix, iy = round(lx), round(ly)
                if abs(ix) > extent or abs(iy) > extent:
                    continue
                hit = voxel(ix, iy, z, u, v) if pixel else voxel(ix, iy, z)
                if hit is None:
                    continue
                name, region, r = hit
                sp.px(u, v, name, region, r=r)
                break
    return sp


def shade_step(nx: float, ny: float, theta: float, light=(-0.55, -0.45)) -> int:
    """-1 / 0 / +1: is a side whose local outward normal is (nx, ny) lit from the upper left?"""
    c, s = math.cos(theta), math.sin(theta)
    wx = nx * c - ny * s
    wy = nx * s + ny * c
    d = wx * light[0] + wy * light[1]
    return 1 if d > 0.35 else (-1 if d < -0.35 else 0)
