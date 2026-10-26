"""Build the procedural pixel-art lab background for Stage (#341).

Offline tool (not used at runtime). Needs numpy, scipy and Pillow:

    python3 dashboard/stage/tools/build_bg.py

Every layer is drawn in code on a 640x340 canvas (image y grows downward) with a small
palette and ordered dithering; nothing is traced from a reference picture.

Outputs (assets/bg/), one per entry of js/bg_layers.js:
- RGBA colour layers: sky_<sky>, city_<sky> (sky = night / dawn / rain), glass, glass_rain,
  wall, pipes, shelf, desk, fore. The room's layers share one 64-colour palette, each sky's
  outside 32 (median cut, applied after drawing).
- RGB light layers (black = no light; shown only while their event runs): window_glow_<sky>,
  shelf_glow, desk_glow.
- neon.png (R = orbit core, G = orbit glow falloff, B = which orbit) and bg.json (geometry).
The drawing lives in build_bg_sky.py (outside), build_bg_room.py (wall, window, pipes,
foreground) and build_bg_props.py (shelf, desk). Seeds are fixed: the output is reproducible.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np
from PIL import Image
from scipy import ndimage as ndi

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "assets" / "bg"
W, H = 640, 340
BAYER4 = np.array([[0, 8, 2, 10], [12, 4, 14, 6], [3, 11, 1, 9], [15, 7, 13, 5]]) / 16

P = {
    "k0": "#05050c", "k1": "#0a0a17", "k2": "#100f22", "k3": "#17162f", "k4": "#1f1e3d",
    "k5": "#29294f", "k6": "#353865", "k7": "#45497d", "k8": "#5a6096",
    "sky0": "#0b0d24", "sky1": "#121637", "sky2": "#1b1f4a", "sky3": "#2a2a5e",
    "sky4": "#3d3270", "sky5": "#5a3a7c",
    "bd0": "#07081a", "bd1": "#0c0e24", "bd2": "#121631", "bd3": "#1a1f40",
    "lit_y": "#f2c46b", "lit_c": "#7fd6e8", "lit_p": "#d27ad8", "lit_w": "#e8e4ff",
    "red": "#ff4b5c",
    "gl0": "#16303d", "gl1": "#1f4e5a", "gl2": "#2f7f86", "gl3": "#55c1b8", "gl4": "#a8f0de",
    "am0": "#4a2a1c", "am1": "#8a5228", "am2": "#c98a3a", "am3": "#f2c46b",
    "mg0": "#3a1f45", "mg1": "#6e3478", "mg2": "#b25bb0", "mg3": "#f09be0",
    "gr0": "#16301f", "gr1": "#25603a", "gr2": "#47a35a", "gr3": "#9ff08a",
    "wood0": "#1a1424", "wood1": "#2a2033", "wood2": "#3d3047", "wood3": "#54465c",
    "glass_hi": "#9fb4e6", "glass_mid": "#4c5a8f",
}  # fmt: skip


def rgb(name: str) -> np.ndarray:
    value = P.get(name, name)
    return np.array([int(value[i : i + 2], 16) for i in (1, 3, 5)], np.uint8)


class Canvas:
    def __init__(self) -> None:
        self.a = np.zeros((H, W, 4), np.uint8)

    def px(self, x: int, y: int, c: str) -> None:
        if 0 <= x < W and 0 <= y < H:
            self.a[y, x, :3] = rgb(c)
            self.a[y, x, 3] = 255

    def rect(self, x: int, y: int, w: int, h: int, c: str) -> None:
        x0, y0, x1, y1 = max(x, 0), max(y, 0), min(x + w, W), min(y + h, H)
        if x1 > x0 and y1 > y0:
            self.a[y0:y1, x0:x1, :3] = rgb(c)
            self.a[y0:y1, x0:x1, 3] = 255

    def hline(self, x0: int, x1: int, y: int, c: str) -> None:
        self.rect(x0, y, x1 - x0 + 1, 1, c)

    def vline(self, x: int, y0: int, y1: int, c: str) -> None:
        self.rect(x, y0, 1, y1 - y0 + 1, c)

    def grad(self, x, y, w, h, ramp, vertical=True, curve=1.0) -> None:
        """Ordered-dithered gradient across a palette ramp."""
        cols = [rgb(c) for c in ramp]
        n = len(cols)
        for j in range(h):
            for i in range(w):
                xx, yy = x + i, y + j
                if not (0 <= xx < W and 0 <= yy < H):
                    continue
                t = (j / max(h - 1, 1)) if vertical else (i / max(w - 1, 1))
                t = t**curve * (n - 1)
                k = int(t) + (1 if (t - int(t)) > BAYER4[yy % 4, xx % 4] else 0)
                self.a[yy, xx, :3] = cols[min(k, n - 1)]
                self.a[yy, xx, 3] = 255

    def line(self, x0, y0, x1, y1, c) -> None:
        dx, dy = abs(x1 - x0), -abs(y1 - y0)
        sx, sy = (1 if x0 < x1 else -1), (1 if y0 < y1 else -1)
        err = dx + dy
        while True:
            self.px(x0, y0, c)
            if x0 == x1 and y0 == y1:
                return
            e2 = 2 * err
            if e2 >= dy:
                err += dy
                x0 += sx
            if e2 <= dx:
                err += dx
                y0 += sy

    def disc(self, cx, cy, r, c) -> None:
        for y in range(int(cy - r), int(cy + r) + 1):
            for x in range(int(cx - r), int(cx + r) + 1):
                if (x - cx) ** 2 + (y - cy) ** 2 <= r * r:
                    self.px(x, y, c)

    def save(self, name: str, rgb_only: bool = False) -> int:
        path = OUT / f"{name}.png"
        img = Image.fromarray(self.a[..., :3] if rgb_only else self.a)
        img.save(path, optimize=True)
        return path.stat().st_size


# --- layout (image coordinates) --------------------------------------------------------------

WIN = (404, 34, 122, 112)  # x, y, w, h of the window opening
SHELF = (104, 44, 104, 178)
DESK_Y = 224


def glass_streaks() -> Canvas:
    """Reflections on the window glass (drawn above the city, below the frame)."""
    c = Canvas()
    x, y, _, _ = WIN
    for sx in (x + 8, x + 70):
        for k in range(14):
            c.px(sx + k, y + 4 + k, "glass_mid")
            c.px(sx + k + 3, y + 4 + k, "glass_mid")
    return c


def orbit_points(cx, cy, a, b, rot, n=1400):
    pts = []
    for k in range(n):
        t = 2 * math.pi * k / n
        x, y = a * math.cos(t), b * math.sin(t)
        pts.append((cx + x * math.cos(rot) - y * math.sin(rot),
                    cy + x * math.sin(rot) + y * math.cos(rot)))  # fmt: skip
    return pts


def neon() -> np.ndarray:
    core = np.zeros((H, W), np.float32)
    group = np.zeros((H, W), np.float32)
    for gid, (rot, a, b) in enumerate(((0.32, 178, 58), (-0.30, 170, 54))):
        for x, y in orbit_points(320, 130, a, b, rot):
            xi, yi = round(x), round(y)
            for dx, dy in ((0, 0), (1, 0), (0, 1), (1, 1)):
                if 0 <= xi + dx < W and 0 <= yi + dy < H:
                    core[yi + dy, xi + dx] = 1.0
                    group[yi + dy, xi + dx] = 0.5 + gid * 0.5
    dist, (iy, ix) = ndi.distance_transform_edt(core < 0.5, return_indices=True)
    glow = np.clip(1 - dist / 12.0, 0, 1) ** 1.6
    edge = (dist > 0) & (dist <= 1.0)
    r = np.where(core > 0, 1.0, np.where(edge, 0.55, 0.0))
    grp = group[iy, ix] * (glow > 0)
    return (np.dstack([r, glow, grp]) * 255).astype(np.uint8)


SKIES = ("night", "dawn", "rain")
SCOPE = (416, 182, 36, 24)  # oscilloscope screen (the desk light draws its trace here)
CHAR_POS = (181, 20)
LIGHT = {
    "shelf_glow",
    "desk_glow",
    "neon",
    *(f"window_glow_{s}" for s in SKIES),
}  # RGB, black = off
PALETTE_ROOM = 64  # shared palette of the room's colour layers (the character has 35)
PALETTE_OUTSIDE = 32  # per sky variant: sky + city  # must match scene.js (the foreground keeps off her silhouette)


def quantise(canvases: list[Canvas], n: int) -> int:
    """Map the opaque pixels of a group of layers onto one shared palette of at most n colours
    (median cut over the distinct colours, so rare accents such as lit windows keep their own
    entry). Returns the palette size."""
    cols = np.unique(np.concatenate([c.a[c.a[..., 3] > 0][:, :3] for c in canvases]), axis=0)
    if len(cols) <= n:
        return len(cols)
    strip = Image.fromarray(cols[None, :, :].astype(np.uint8))
    pal = np.array(strip.quantize(n, method=Image.Quantize.MEDIANCUT).getpalette()[: n * 3])
    pal = pal.reshape(-1, 3).astype(int)
    for c in canvases:
        m = c.a[..., 3] > 0
        px = c.a[m][:, :3].astype(int)
        idx = ((px[:, None, :] - pal[None, :, :]) ** 2).sum(-1).argmin(1)
        c.a[m, :3] = pal[idx]
    return len(np.unique(pal, axis=0))


def char_silhouette() -> np.ndarray:
    """The character's pixels at rest in sheet space: the base sheet plus the hair segments
    (assets/char/hair.json boxes; the hair rig moves them by a few pixels at most)."""
    char = ROOT / "assets" / "char"
    alpha = np.array(Image.open(char / "base.png").convert("RGBA"))[..., 3].copy()
    meta = char / "hair.json"
    if meta.exists():
        atlas = np.array(Image.open(char / "hair.png").convert("RGBA"))[..., 3]
        for seg in json.loads(meta.read_text(encoding="utf-8"))["segments"]:
            x, y, w, h = seg["box"]
            ax, ay = seg["at"]
            alpha[y : y + h, x : x + w] = np.maximum(
                alpha[y : y + h, x : x + w], atlas[ay : ay + h, ax : ax + w]
            )
    return alpha


def main() -> None:
    import build_bg_props as props
    import build_bg_room as room
    import build_bg_sky as sky

    OUT.mkdir(parents=True, exist_ok=True)
    sizes = {}

    def put(name: str, canvas: Canvas, rgb_only: bool = False) -> None:
        sizes[name] = canvas.save(name, rgb_only)

    palettes = {}
    for i, vid in enumerate(SKIES):
        outside = [sky.sky(vid, np.random.default_rng(3410 + i)),
                   sky.city(vid, np.random.default_rng(3420 + i))[0]]  # fmt: skip
        palettes[vid] = quantise(outside, PALETTE_OUTSIDE)
        put(f"sky_{vid}", outside[0])
        put(f"city_{vid}", outside[1])
        put(f"window_glow_{vid}", sky.window_glow(vid), rgb_only=True)
    s, sg = props.shelf(np.random.default_rng(3434))
    d, dg = props.desk(np.random.default_rng(3435), SCOPE)
    char_alpha = char_silhouette()
    inside = {
        "glass": glass_streaks(),
        "glass_rain": sky.glass_rain(np.random.default_rng(3431)),
        "wall": room.wall(np.random.default_rng(3432)),
        "pipes": room.pipes(np.random.default_rng(3433)),
        "shelf": s,
        "desk": d,
        "fore": room.foreground(char_alpha, CHAR_POS, np.random.default_rng(3436)),
    }
    palettes["room"] = quantise(list(inside.values()), PALETTE_ROOM)
    for name, canvas in inside.items():
        put(name, canvas)
    put("shelf_glow", sg, rgb_only=True)
    put("desk_glow", dg, rgb_only=True)
    Image.fromarray(neon()).save(OUT / "neon.png", optimize=True)
    sizes["neon"] = (OUT / "neon.png").stat().st_size
    # the part of the room each image draws (opaque / lit pixels, +2 px): the renderer builds
    # each mesh over this rectangle only
    rects = {}
    for png in sorted(OUT.glob("*.png")):
        a = np.array(Image.open(png).convert("RGBA"))
        on = a[..., 3] > 0 if png.stem not in LIGHT else a[..., :3].any(-1)
        ys, xs = np.where(on)
        x0, y0 = max(int(xs.min()) - 2, 0), max(int(ys.min()) - 2, 0)
        x1, y1 = min(int(xs.max()) + 3, W), min(int(ys.max()) + 3, H)
        rects[png.stem] = [x0, y0, x1 - x0, y1 - y0]
    meta = {"size": [W, H], "window": WIN, "shelf": SHELF, "deskY": DESK_Y, "scope": SCOPE,
            "skies": list(SKIES), "rects": rects}  # fmt: skip
    (OUT / "bg.json").write_text(json.dumps(meta, indent=1) + "\n", encoding="utf-8")
    print(json.dumps({"bytes": sizes, "palettes": palettes}))


if __name__ == "__main__":
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    main()
