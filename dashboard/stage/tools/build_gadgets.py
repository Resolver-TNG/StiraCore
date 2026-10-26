"""Build the gadget sprites for Stage (#341, stage 2: actions and gadgets).

Offline tool (not used at runtime). Needs numpy and Pillow:

    python3 dashboard/stage/tools/build_gadgets.py            # writes assets/gadgets/
    python3 dashboard/stage/tools/build_gadgets.py --preview  # also a 3x preview in /tmp

Every gadget is drawn in code with the lab's palette (tools/build_bg.py) and the
character's ramps, 1 px dark outlines and 4x4 ordered dither, so props read as the same
pixel art as the room. Nothing is traced or imported.

Outputs (assets/gadgets/):
  gadgets.png     colour atlas (RGBA)
  gadgets_fx.png  effect map, same layout: B = region id (see REGIONS), R/G = region
                  coordinates (e.g. screen u/v, key column/row) read by the runtime shader
  gadgets.json    sprite rectangles, anchors and region ids
"""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "assets" / "gadgets"
BAYER4 = np.array([[0, 8, 2, 10], [12, 4, 14, 6], [3, 11, 1, 9], [15, 7, 13, 5]]) / 16

# Lab palette (tools/build_bg.py) + character ramps (tools/build_char.py) + a few prop tones.
P = {
    "k0": "#05050c", "k1": "#0a0a17", "k2": "#100f22", "k3": "#17162f", "k4": "#1f1e3d",
    "k5": "#29294f", "k6": "#353865", "k7": "#45497d", "k8": "#5a6096", "k9": "#7a80b4",
    "k10": "#a4a9d4",
    "ol": "#0b0a16",
    "gl0": "#16303d", "gl1": "#1f4e5a", "gl2": "#2f7f86", "gl3": "#55c1b8", "gl4": "#a8f0de",
    "cy0": "#123a4a", "cy1": "#1d6f86", "cy2": "#52e6ff", "cy3": "#c4f7ff",
    "am0": "#4a2a1c", "am1": "#8a5228", "am2": "#c98a3a", "am3": "#f2c46b",
    "mg0": "#3a1f45", "mg1": "#6e3478", "mg2": "#b25bb0", "mg3": "#f09be0",
    "gr0": "#16301f", "gr1": "#25603a", "gr2": "#47a35a", "gr3": "#9ff08a",
    "rd0": "#3a0f1c", "rd1": "#7a1a2c", "rd2": "#d8344a", "rd3": "#ff8a92",
    "wood0": "#1a1424", "wood1": "#2a2033", "wood2": "#3d3047", "wood3": "#54465c",
    "hair0": "#25243f", "hair1": "#3e3f66", "hair2": "#5f628d", "hair3": "#8b8fb5",
    "hair4": "#b9bdd8", "hair5": "#e6e8f3",
    "coat0": "#3b3247", "coat1": "#665a70", "coat2": "#988c98", "coat3": "#c9bfc0",
    "coat4": "#ece5de",
    "card0": "#2e2230", "card1": "#5a4038", "card2": "#86624a", "card3": "#b08a62",
    "card4": "#d6b486",
    "pap0": "#6f6c8c", "pap1": "#b4b2cc", "pap2": "#dcdaea", "pap3": "#f4f2fb",
    "ver0": "#5a1420", "ver1": "#a8283a", "ver2": "#e0485a",
    "scr": "#0a1020", "scr1": "#0f1a30",
    "glass_hi": "#9fb4e6", "glass_mid": "#4c5a8f",
    "white": "#ffffff",
}  # fmt: skip

# Effect regions (gadgets_fx.png, blue channel). The runtime shader keys off these ids.
REGIONS = {
    "screen": 1,  # R,G = screen u,v (0..255)
    "keys": 2,  # R = key column * 16, G = key row * 32
    "lamp_red": 3,
    "lamp_amber": 4,
    "lamp_green": 5,  # R = v within the lamp (for the highlight)
    "eye": 6,  # drone visor
    "beacon": 7,  # R = u around the lens (rotation)
    "lens": 8,  # magnifier glass
    "sand_top": 9,  # R = v (0 top .. 255 neck)
    "sand_bot": 10,  # R = v (0 neck .. 255 bottom)
    "thruster": 11,
    "led": 12,  # generic small status LED (colour from the instance)
}


def rgb(name: str) -> np.ndarray:
    value = P.get(name, name)
    return np.array([int(value[i : i + 2], 16) for i in (1, 3, 5)], np.uint8)


class Sprite:
    def __init__(self, w: int, h: int) -> None:
        self.w, self.h = w, h
        self.c = np.zeros((h, w, 4), np.uint8)
        self.fx = np.zeros((h, w, 4), np.uint8)

    def inside(self, x: int, y: int) -> bool:
        return 0 <= x < self.w and 0 <= y < self.h

    def px(self, x, y, c, region=None, r=0, g=0) -> None:
        x, y = int(x), int(y)
        if not self.inside(x, y):
            return
        self.c[y, x, :3] = rgb(c)
        self.c[y, x, 3] = 255
        if region is not None:
            self.fx[y, x] = (r, g, REGIONS[region], 255)
        else:
            self.fx[y, x] = 0

    def rect(self, x, y, w, h, c, region=None) -> None:
        for yy in range(y, y + h):
            for xx in range(x, x + w):
                self.px(xx, yy, c, region)

    def hline(self, x0, x1, y, c) -> None:
        for x in range(x0, x1 + 1):
            self.px(x, y, c)

    def vline(self, x, y0, y1, c) -> None:
        for y in range(y0, y1 + 1):
            self.px(x, y, c)

    def line(self, x0, y0, x1, y1, c) -> None:
        x0, y0, x1, y1 = int(x0), int(y0), int(x1), int(y1)
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

    def stamp_map(self, x0, y0, rows: list[str], cmap: dict) -> None:
        """Draw a hand-made pixel map: one character per pixel, '.' is transparent."""
        for j, row in enumerate(rows):
            for i, ch in enumerate(row):
                if ch in cmap:
                    spec = cmap[ch]
                    if isinstance(spec, tuple):
                        self.px(x0 + i, y0 + j, spec[0], spec[1])
                    else:
                        self.px(x0 + i, y0 + j, spec)

    def outline(self, colour="ol") -> None:
        """1 px dark outline around the opaque pixels (like the character sheet)."""
        a = self.c[..., 3] > 0
        pad = np.pad(a, 1)
        ring = (pad[:-2, 1:-1] | pad[2:, 1:-1] | pad[1:-1, :-2] | pad[1:-1, 2:]) & ~a
        ys, xs = np.nonzero(ring)
        for y, x in zip(ys, xs):
            self.c[y, x, :3] = rgb(colour)
            self.c[y, x, 3] = 255


def dither(x: int, y: int, t: float) -> bool:
    return t > BAYER4[y % 4, x % 4]


def ramp_pick(ramp: list[str], t: float, x: int, y: int) -> str:
    """Ordered-dithered pick along a ramp, t in 0..1."""
    t = min(max(t, 0.0), 1.0) * (len(ramp) - 1)
    k = int(t)
    if k < len(ramp) - 1 and dither(x, y, t - k):
        k += 1
    return ramp[k]


# --- laptop (orthographic 3/4 view, drawn face by face) ----------------------------------------

LAPTOP = {"w": 62, "d": 36, "lid": 38, "open": 106, "yaw": 24, "pitch": 26, "thick": 3}


def laptop() -> tuple[Sprite, dict]:
    p = LAPTOP
    th, ph = math.radians(p["yaw"]), math.radians(p["pitch"])
    op = math.radians(p["open"])

    def proj(x, y, z):
        xr = x * math.cos(th) + z * math.sin(th)
        zr = -x * math.sin(th) + z * math.cos(th)
        return xr, -y * math.cos(ph) + zr * math.sin(ph)

    hw, d, lh, t = p["w"] / 2, p["d"], p["lid"], p["thick"]
    lid_dir = (0.0, math.sin(op), math.cos(op))  # leans back from the hinge (z = 0)

    def lid_pt(x, s):  # s: 0 hinge .. 1 top
        return (x, t + lid_dir[1] * lh * s, lid_dir[2] * lh * s)

    faces = {
        "deck_top": [(-hw, t, 0), (hw, t, 0), (hw, t, d), (-hw, t, d)],
        "deck_front": [(-hw, t, d), (hw, t, d), (hw, 0, d), (-hw, 0, d)],
        "deck_left": [(-hw, t, 0), (-hw, t, d), (-hw, 0, d), (-hw, 0, 0)],
        "lid": [lid_pt(-hw, 0), lid_pt(hw, 0), lid_pt(hw, 1), lid_pt(-hw, 1)],
    }
    pts2 = {k: [proj(*q) for q in v] for k, v in faces.items()}
    allp = [q for v in pts2.values() for q in v]
    minx = math.floor(min(q[0] for q in allp)) - 2
    miny = math.floor(min(q[1] for q in allp)) - 2
    maxx = math.ceil(max(q[0] for q in allp)) + 2
    maxy = math.ceil(max(q[1] for q in allp)) + 3
    s = Sprite(maxx - minx + 1, maxy - miny + 1)

    def raster(name, shade):
        a, b, _, dd = pts2[name]
        o = np.array(a) - (minx, miny)
        eu = np.array(b) - np.array(a)
        ev = np.array(dd) - np.array(a)
        m = np.linalg.inv(np.array([eu, ev]).T)
        for y in range(s.h):
            for x in range(s.w):
                u, v = m @ (np.array([x + 0.5, y + 0.5]) - o)
                if 0 <= u < 1 and 0 <= v < 1:
                    shade(x, y, u, v)

    # lid (front face = screen side). v: 0 hinge .. 1 top
    def lid_shade(x, y, u, v):
        bez_u, bez_b, bez_t = 0.06, 0.12, 0.08
        if bez_u <= u < 1 - bez_u and bez_b <= v < 1 - bez_t:
            su = (u - bez_u) / (1 - 2 * bez_u)
            sv = (1 - bez_t - v) / (1 - bez_b - bez_t)  # 0 top .. 1 bottom
            s.px(x, y, "scr", "screen", int(su * 255), int(sv * 255))
        else:
            edge = u < 0.03 or v > 0.965
            s.px(x, y, "k7" if edge else ramp_pick(["k3", "k4", "k5"], 0.3 + 0.6 * v, x, y))

    raster("lid", lid_shade)

    cols, rows = 12, 4

    def deck_shade(x, y, u, v):  # v: 0 hinge .. 1 front
        ku0, ku1, kv0, kv1 = 0.07, 0.93, 0.1, 0.56
        if ku0 <= u < ku1 and kv0 <= v < kv1:
            cu = (u - ku0) / (ku1 - ku0) * cols
            cv = (v - kv0) / (kv1 - kv0) * rows
            gap = (cu % 1) < 0.18 or (cv % 1) < 0.22
            if gap:
                s.px(x, y, "k1")
            else:
                s.px(x, y, "k3", "keys", int(cu) * 16, int(cv) * 32)
            return
        if 0.36 <= u < 0.64 and 0.64 <= v < 0.92:
            s.px(x, y, "k6" if v < 0.67 else ramp_pick(["k4", "k5"], 0.5, x, y))
            return
        front = v > 0.94
        s.px(x, y, "k8" if front else ramp_pick(["k5", "k6", "k7"], 0.25 + 0.6 * v, x, y))

    raster("deck_top", deck_shade)
    raster("deck_left", lambda x, y, u, v: s.px(x, y, ramp_pick(["k2", "k3"], v, x, y)))
    raster("deck_front", lambda x, y, u, v: s.px(x, y, "k4" if v < 0.5 else "k2"))

    # rim light from the neon behind: lid top edge and left edge
    for name, i0, i1 in (("lid", 3, 0), ("lid", 2, 3)):
        a = np.array(pts2[name][i0]) - (minx, miny)
        b = np.array(pts2[name][i1]) - (minx, miny)
        n = int(max(abs(b - a))) + 1
        for k in range(n + 1):
            q = a + (b - a) * k / n
            xx, yy = int(q[0]), int(q[1])
            if s.inside(xx, yy) and s.c[yy, xx, 3]:
                s.px(xx, yy, "cy1" if name == "lid" and i0 == 3 else "k8")
    # power LED on the deck front-left (lit only through the fx map)
    fl = np.array(pts2["deck_front"][0]) - (minx, miny)
    s.px(int(fl[0]) + 4, int(fl[1]) + 1, "gr1", "led")
    s.outline()
    # contact shadow under the deck (dithered, drawn behind the outline)
    shadow_y = int(max(q[1] for q in pts2["deck_front"]) - miny) + 1
    x0 = int(min(q[0] for q in pts2["deck_front"]) - minx)
    x1 = int(max(q[0] for q in pts2["deck_front"]) - minx)
    for yy in range(shadow_y, min(shadow_y + 2, s.h)):
        for xx in range(x0 - 1, x1 + 3):
            if (
                s.inside(xx, yy)
                and s.c[yy, xx, 3] == 0
                and dither(xx, yy, 0.6 - 0.25 * (yy - shadow_y))
            ):
                s.px(xx, yy, "k0")
    sa, sb, sc_, sd = (np.array(q) - (minx, miny) for q in pts2["lid"])
    meta = {"screen": [list(map(float, q)) for q in (sa, sb, sc_, sd)], "cols": cols, "rows": rows}
    return s, meta


# --- signal tower (test result lamp) -----------------------------------------------------------


def stacklight() -> Sprite:
    s = Sprite(11, 40)
    # three lamp segments, top red, middle amber, bottom green (lit via fx)
    seg = [("lamp_red", "rd0", 2), ("lamp_amber", "am0", 10), ("lamp_green", "gr0", 18)]
    for region, dark, y0 in seg:
        for j in range(7):
            for i in range(1, 10):
                edge = i in (1, 9)
                s.px(
                    i,
                    y0 + j,
                    "k2" if edge else dark,
                    None if edge else region,
                    int(j / 6 * 255),
                    int((i - 1) / 8 * 255),
                )
        s.hline(1, 9, y0 + 7, "k7")
    s.rect(2, 0, 7, 2, "k7")
    s.hline(2, 8, 0, "k9")
    s.rect(4, 26, 3, 9, "k6")
    s.vline(4, 26, 34, "k8")
    s.rect(0, 35, 11, 4, "k5")
    s.hline(0, 10, 35, "k8")
    s.hline(0, 10, 38, "k3")
    s.outline()
    return s


# --- subagent drone (a small "fox-ear" helper in her colours) --------------------------------

DRONE = [
    ".oo...................oo.",
    ".oHo.................oHo.",
    ".oHHo...............oHHo.",
    ".oHPHo.............oHPHo.",
    ".oHPPHoooooooooooooHPPHo.",
    "..oHHMLLLLLWWLLLLLLMHHo..",
    "..oMLLLLWWWLLLLLLLLLLMo..",
    ".oMLLLLLLLLLLLLLLLLLLLMo.",
    ".oMLVVVVVVVVVVVVVVVVVLMo.",
    ".oMLVVVEEVVVVVVVEEVVVLMo.",
    ".oMLVVVEEVVVVVVVEEVVVLMo.",
    ".oMLVVVVVVVVVVVVVVVVVLMo.",
    ".oMMLLLLLLLLLLLLLLLLLMMo.",
    "..oMMMMMMMMMMMMMMMMMMMo..",
    "...ooSSSSoooooooSSSSoo...",
    ".....oSSo.......oSSo.....",
    "......oo.........oo......",
]
assert all(len(r) == 25 for r in DRONE)


def drone(frame: int) -> Sprite:
    s = Sprite(25, 19)
    cmap = {
        "o": "ol", "H": "hair4", "P": "hair1", "M": "hair2", "L": "hair3", "W": "hair5",
        "V": ("k1", "eye"), "E": ("cy2", "eye"), "S": ("k6", "thruster"),
    }  # fmt: skip
    s.stamp_map(0, 2, DRONE, cmap)
    # eye pixels carry a marker in R so the shader can light only them
    for y in range(s.h):
        for x in range(s.w):
            if s.fx[y, x, 2] == REGIONS["eye"] and tuple(s.c[y, x, :3]) == tuple(rgb("cy2")):
                s.fx[y, x, 0] = 255
    # visor glint and rim light (the neon is behind)
    s.px(5, 10, "k6")
    s.px(6, 10, "k5")
    # mast and rotor (two frames)
    s.vline(12, 2, 6, "k8")
    s.px(12, 2, "k10")
    if frame == 0:
        s.hline(6, 18, 1, "k10")
        s.px(6, 1, "k8")
        s.px(18, 1, "k8")
    else:
        s.hline(10, 14, 1, "k10")
        s.px(9, 1, "k7")
        s.px(15, 1, "k7")
        s.px(12, 0, "k9")
    return s


BUBBLE = [
    ".ooooooooo.",
    "oWWWWWWWWWo",
    "oWWWWWWWWWo",
    "oWWWWWWWWWo",
    "oWWWWWWWWWo",
    "oWWWWWWWWWo",
    "oWWWWWWWWWo",
    ".oooWWoooo.",
    "....oWo....",
    ".....o.....",
]


def bubble(kind: str) -> Sprite:
    s = Sprite(11, 10)
    s.stamp_map(0, 0, BUBBLE, {"o": "ol", "W": "pap3"})
    icons = {
        "ok": (["......g", ".....g.", "g...g..", ".g.g...", "..g...."], {"g": "gr1"}),
        "err": (["..r..", "..r..", "..r..", ".....", "..r.."], {"r": "rd2"}),
        "dots": (["g.g.g"], {"g": "k7"}),
    }
    rows, cmap = icons[kind]
    w = len(rows[0])
    s.stamp_map((11 - w) // 2, 1 + (6 - len(rows)) // 2, rows, cmap)
    return s


DIGITS = {
    "0": ["ooo", "o.o", "o.o", "o.o", "ooo"], "1": [".o.", "oo.", ".o.", ".o.", "ooo"],
    "2": ["ooo", "..o", "ooo", "o..", "ooo"], "3": ["ooo", "..o", ".oo", "..o", "ooo"],
    "4": ["o.o", "o.o", "ooo", "..o", "..o"], "5": ["ooo", "o..", "ooo", "..o", "ooo"],
    "6": ["ooo", "o..", "ooo", "o.o", "ooo"], "7": ["ooo", "..o", ".o.", ".o.", ".o."],
    "8": ["ooo", "o.o", "ooo", "o.o", "ooo"], "9": ["ooo", "o.o", "ooo", "..o", "ooo"],
    "+": ["...", ".o.", "ooo", ".o.", "..."],
}  # fmt: skip


def digits() -> Sprite:
    """3x5 digits on a dark plate, 5 px per cell: 0-9 then '+'."""
    s = Sprite(11 * 5, 7)
    for k, ch in enumerate("0123456789+"):
        x = k * 5
        s.rect(x, 0, 5, 7, "ol")
        s.stamp_map(x + 1, 1, DIGITS[ch], {"o": "cy3"})
    return s


# --- breaker beacon (hangs from the pipe) -------------------------------------------------------

BEACON = [
    ".......ooo.......",
    ".......oKo.......",
    "...ooooooooooo...",
    "..oKKKkkkkkKKKo..",
    "..ooooooooooooo..",
    "..oGGGGGGGGGGGo..",
    ".oGGGGGGGGGGGGGo.",
    ".oGGGGGGGGGGGGGo.",
    ".oGGGGGGGGGGGGGo.",
    ".oGGGGGGGGGGGGGo.",
    "..oGGGGGGGGGGGo..",
    "...oGGGGGGGGGo...",
    ".....ooooooo.....",
]


def beacon() -> Sprite:
    s = Sprite(17, 13)
    s.stamp_map(0, 0, BEACON, {"o": "ol", "K": "k8", "k": "k6", "G": "k4"})
    for y in range(s.h):
        for x in range(s.w):
            if BEACON[y][x] == "G":
                if x % 4 == 0 and y < 11:  # wire guard over the lens
                    s.px(x, y, "k6")
                    continue
                s.px(
                    x,
                    y,
                    ramp_pick(["rd0", "k4"], (y - 5) / 6, x, y),
                    "beacon",
                    int(x / 16 * 255),
                    int((y - 5) / 6 * 255),
                )
    return s


# --- magnifier (web) ----------------------------------------------------------------------------


def magnifier() -> Sprite:
    s = Sprite(30, 30)
    cx, cy, r = 11.0, 11.0, 9.6
    for y in range(s.h):
        for x in range(s.w):
            d = math.hypot(x + 0.5 - cx, y + 0.5 - cy)
            if d < r - 2.2:
                s.px(x, y, "cy0", "lens", int(min(x / 22, 1) * 255), int(min(y / 22, 1) * 255))
            elif d < r:
                ang = math.atan2(y + 0.5 - cy, x + 0.5 - cx)
                t = 0.5 - 0.5 * math.cos(ang + 2.4)
                s.px(x, y, ramp_pick(["am3", "am2", "am1"], t, x, y))
    for k in range(10):
        x, y = 18 + k, 18 + k
        s.px(x, y, "wood3")
        s.px(x + 1, y, "wood2")
        s.px(x, y + 1, "wood1")
        s.px(x + 1, y + 1, "wood1")
    s.px(18, 18, "am1")
    s.px(19, 18, "am1")
    s.outline()
    return s


# --- shelf props: binder (read), sticky note (write), archive box (forgetting) ---------------


def folder() -> Sprite:
    s = Sprite(9, 28)
    s.rect(1, 1, 7, 26, "cy1")
    s.vline(1, 1, 26, "cy2")
    s.vline(7, 1, 26, "cy0")
    s.rect(2, 7, 5, 8, "pap2")
    s.hline(3, 5, 9, "k6")
    s.hline(3, 5, 11, "k6")
    s.hline(3, 4, 13, "k6")
    s.px(4, 21, "k1")
    s.px(4, 22, "pap1")
    s.outline()
    return s


def note() -> Sprite:
    s = Sprite(13, 13)
    s.rect(1, 1, 11, 11, "am3")
    s.hline(1, 11, 1, "#ffe9a8")
    s.hline(1, 11, 2, "#ffe9a8")
    for x, y in ((11, 11), (10, 11), (11, 10), (9, 11), (11, 9)):
        s.px(x, y, "am2")
    s.px(10, 10, "am1")
    for y in (4, 6, 8):
        s.hline(3, 9 if y < 8 else 6, y, "am1")
    s.outline()
    return s


def box() -> Sprite:
    s = Sprite(30, 21)
    s.rect(1, 4, 28, 16, "card2")
    s.rect(1, 1, 28, 4, "card3")
    s.hline(1, 28, 1, "card4")
    s.hline(1, 28, 5, "card1")
    s.vline(28, 4, 19, "card1")
    s.vline(1, 4, 19, "card3")
    for x in range(2, 28):
        for y in range(6, 20):
            if dither(x, y, (y - 6) / 13 * 0.55):
                s.px(x, y, "card1")
    # label with a restore arrow (restorable)
    s.rect(9, 8, 12, 8, "pap2")
    s.hline(9, 20, 8, "pap3")
    arrow = ["..ooo..", ".o...o.", "o.o...o", "oo....o", "o.....o", ".o...o.", "..ooo.."]
    s.stamp_map(12, 8, arrow[:7], {"o": "cy1"})
    # hand holes
    s.hline(3, 5, 11, "card0")
    s.hline(24, 26, 11, "card0")
    s.outline()
    return s


# --- approval slip, seals and the stamper ------------------------------------------------------

KANJI = {
    # 可 (approve) and 否 (deny), 9x9, placed by hand
    "approve": [
        "ooooooooo",
        ".......o.",
        ".ooooo.o.",
        ".o...o.o.",
        ".o...o.o.",
        ".ooooo.o.",
        ".......o.",
        ".......o.",
        ".....ooo.",
    ],
    "deny": [
        "ooooooooo",
        "....o....",
        "...oo.o..",
        "..o.o..o.",
        ".o..o....",
        ".ooooooo.",
        ".o.....o.",
        ".o.....o.",
        ".ooooooo.",
    ],
}


def seal(kind: str) -> Sprite:
    s = Sprite(17, 17)
    ink = "ver1" if kind == "approve" else "k7"
    for y in range(17):
        for x in range(17):
            d = math.hypot(x - 8, y - 8)
            # worn ink on the ring only, so it reads as a hand stamp
            if 6.9 <= d < 8.2 and BAYER4[(y * 3) % 4, (x * 5) % 4] < 0.9:
                s.px(x, y, ink)
    s.stamp_map(4, 4, KANJI[kind], {"o": ink})
    return s


def slip() -> Sprite:
    s = Sprite(26, 32)
    s.rect(1, 2, 24, 29, "pap2")
    s.hline(1, 24, 2, "pap3")
    s.vline(1, 2, 30, "pap3")
    s.vline(24, 3, 30, "pap1")
    s.hline(2, 24, 30, "pap1")
    for y in (6, 8, 10):
        s.hline(4, 16 if y == 10 else 21, y, "pap0")
    s.rect(4, 13, 6, 1, "cy1")
    s.hline(15, 22, 27, "pap0")
    s.outline()
    # push pin
    s.rect(11, 0, 4, 3, "rd2")
    s.hline(11, 14, 0, "rd3")
    s.px(12, 3, "k8")
    return s


def stamper() -> Sprite:
    s = Sprite(11, 15)
    s.rect(3, 1, 5, 8, "wood3")
    s.vline(3, 1, 8, "card3")
    s.vline(7, 1, 8, "wood1")
    s.rect(4, 0, 3, 1, "wood3")
    s.rect(1, 9, 9, 3, "wood2")
    s.hline(1, 9, 9, "card2")
    s.rect(1, 12, 9, 2, "ver1")
    s.outline()
    return s


# --- scheduler: hourglass + bell --------------------------------------------------------------


def hourglass() -> Sprite:
    s = Sprite(13, 23)
    for y0 in (0, 20):
        s.rect(0, y0, 13, 3, "wood2")
        s.hline(0, 12, y0, "wood3")
        s.hline(0, 12, y0 + 2, "wood1")
    s.vline(1, 3, 19, "wood1")
    s.vline(11, 3, 19, "wood1")
    rows_top = [(3, 9), (3, 9), (4, 8), (4, 8), (5, 7), (5, 7), (6, 6), (6, 6)]
    for j, (a, b) in enumerate(rows_top):
        y = 3 + j
        for x in range(a - 1, b + 2):
            edge = x in (a - 1, b + 1)
            s.px(
                x, y, "glass_mid" if edge else "k3", None if edge else "sand_top", int(j / 7 * 255)
            )
    for j, (a, b) in enumerate(reversed(rows_top)):
        y = 12 + j
        for x in range(a - 1, b + 2):
            edge = x in (a - 1, b + 1)
            s.px(
                x, y, "glass_mid" if edge else "k3", None if edge else "sand_bot", int(j / 7 * 255)
            )
    s.px(6, 11, "k3")
    s.fx[11, 6] = (128, 0, REGIONS["sand_bot"], 255)
    s.px(3, 4, "glass_hi")
    s.px(3, 5, "glass_hi")
    s.outline()
    return s


def bell() -> Sprite:
    s = Sprite(15, 13)
    rows = [
        ".......o.......",
        "......oYo......",
        ".....ooooo.....",
        "....oYYYYYo....",
        "...oYYWYYyYo...",
        "...oYWYYYYyo...",
        "..oYYYYYYYyyo..",
        ".oYYYYYYYYYyyo.",
        "oKKKKKKKKKKKKKo",
        "okkkkkkkkkkkkko",
        "ooooooooooooooo",
    ]
    s.stamp_map(0, 1, rows, {"o": "ol", "Y": "am2", "y": "am1", "W": "am3", "K": "k8", "k": "k5"})
    return s


# --- atlas -------------------------------------------------------------------------------------


def pack(sprites: dict[str, Sprite], width=256) -> tuple[np.ndarray, np.ndarray, dict]:
    x = y = row_h = 0
    rects = {}
    for name, sp in sorted(sprites.items(), key=lambda kv: -kv[1].h):
        if x + sp.w + 1 > width:
            x, y, row_h = 0, y + row_h + 1, 0
        rects[name] = [x, y, sp.w, sp.h]
        x += sp.w + 1
        row_h = max(row_h, sp.h)
    height = 1 << max(5, (y + row_h).bit_length())
    col = np.zeros((height, width, 4), np.uint8)
    fx = np.zeros((height, width, 4), np.uint8)
    for name, (rx, ry, w, h) in rects.items():
        col[ry : ry + h, rx : rx + w] = sprites[name].c
        fx[ry : ry + h, rx : rx + w] = sprites[name].fx
    return col, fx, rects


def build() -> dict:
    lap, lap_meta = laptop()
    sprites = {
        "laptop": lap,
        "stacklight": stacklight(),
        "drone.0": drone(0),
        "drone.1": drone(1),
        "bubble.ok": bubble("ok"),
        "bubble.err": bubble("err"),
        "bubble.dots": bubble("dots"),
        "digits": digits(),
        "beacon": beacon(),
        "magnifier": magnifier(),
        "folder": folder(),
        "note": note(),
        "box": box(),
        "slip": slip(),
        "seal.approve": seal("approve"),
        "seal.deny": seal("deny"),
        "stamper": stamper(),
        "hourglass": hourglass(),
        "bell": bell(),
    }
    col, fx, rects = pack(sprites)
    OUT.mkdir(parents=True, exist_ok=True)
    Image.fromarray(col).save(OUT / "gadgets.png", optimize=True)
    Image.fromarray(fx).save(OUT / "gadgets_fx.png", optimize=True)
    meta = {
        "size": [col.shape[1], col.shape[0]],
        "sprites": rects,
        "regions": REGIONS,
        "laptop": lap_meta,
    }
    (OUT / "gadgets.json").write_text(json.dumps(meta, indent=1) + "\n", encoding="utf-8")
    return {"sprites": sprites, "meta": meta}


# --- preview (offline only): composite onto the room at the runtime positions ------------------


def preview(info: dict, placements: dict, out="/tmp/t341g-gadgets-preview.png") -> None:
    bg = ROOT / "assets" / "bg"
    img = Image.new("RGBA", (640, 340), (5, 5, 12, 255))
    for n in ["city", "glass", "wall", "pipes", "shelf", "desk"]:
        img.alpha_composite(Image.open(bg / f"{n}.png").convert("RGBA"))
    img.alpha_composite(
        Image.open(ROOT / "assets" / "char" / "base.png").convert("RGBA"), (181, 20)
    )
    for name, (x, y) in placements.items():
        sp = info["sprites"][name]
        img.alpha_composite(Image.fromarray(sp.c), (x, y))
    img = img.crop((104, 10, 537, 296)).resize((433 * 3, 286 * 3), Image.NEAREST)
    img.save(out)


if __name__ == "__main__":
    info = build()
    if "--preview" in sys.argv:
        preview(info, {"laptop": (188, 158), "stacklight": (176, 194), "drone.0": (234, 96),
                       "bubble.ok": (240, 82), "drone.1": (394, 90), "beacon": (392, 16),
                       "magnifier": (404, 98), "folder": (195, 186), "note": (182, 150),
                       "box": (146, 22), "slip": (384, 200), "seal.approve": (388, 210),
                       "stamper": (389, 180), "hourglass": (506, 129), "bell": (492, 137)})  # fmt: skip
    print(json.dumps({k: v for k, v in info["meta"]["sprites"].items()}))
