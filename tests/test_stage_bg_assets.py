"""Stage background assets (#341): the generated room images are complete, served by the
page's allow-list, keep off the character, and stay darker and calmer than she is.

The numbers below were measured on the committed images (tools/build_bg.py) with headroom;
they are the "keep the room behind her" rule in a checkable form.
"""

from __future__ import annotations

import colorsys
import json
from pathlib import Path

import pytest
from PIL import Image

from stiracore.observability.stage import stage_asset_allowed

STAGE = Path(__file__).resolve().parents[1] / "dashboard" / "stage"
BG = STAGE / "assets" / "bg"
SKIES = ("night", "dawn", "rain")
ROOM = ("glass", "glass_rain", "wall", "pipes", "shelf", "desk", "fore")
GLOW = ("shelf_glow", "desk_glow")
CHAR_POS = (181, 20)  # dashboard/stage/js/scene.js CHAR_POS


def _opaque(name: str) -> list[tuple[int, int, int]]:
    im = Image.open(BG / f"{name}.png").convert("RGBA")
    return [p[:3] for p in im.get_flattened_data() if p[3] > 127]


def _vivid(p) -> bool:
    _, s, v = colorsys.rgb_to_hsv(*(c / 255 for c in p))
    return s > 0.5 and v > 0.6


def _lum(p) -> float:
    return (0.299 * p[0] + 0.587 * p[1] + 0.114 * p[2]) / 255


def _all_files() -> list[str]:
    names = [*ROOM, *GLOW, "neon"]
    for s in SKIES:
        names += [f"sky_{s}", f"city_{s}", f"window_glow_{s}"]
    return names


def test_metadata_lists_the_skies_and_keeps_the_layout():
    meta = json.loads((BG / "bg.json").read_text(encoding="utf-8"))
    assert meta["skies"] == list(SKIES)
    assert meta["size"] == [640, 340]
    # the gadgets and the event lights are placed against this geometry (#345)
    assert meta["window"] == [404, 34, 122, 112]
    assert meta["shelf"] == [104, 44, 104, 178]
    assert meta["deskY"] == 224
    assert meta["scope"] == [416, 182, 36, 24]


@pytest.mark.parametrize("name", _all_files())
def test_every_layer_exists_is_served_and_has_the_room_size(name):
    assert stage_asset_allowed(f"assets/bg/{name}.png"), name
    with Image.open(BG / f"{name}.png") as im:
        assert im.size == (640, 340), name


def test_no_stale_layer_images_are_left():
    on_disk = {p.stem for p in BG.glob("*.png")}
    assert on_disk == set(_all_files())


def _char_sheet() -> Image.Image:
    """Base sheet + hair segments at rest (assets/char/hair.json), as the renderer draws her."""
    char = STAGE / "assets" / "char"
    sheet = Image.open(char / "base.png").convert("RGBA")
    meta = char / "hair.json"
    if meta.exists():
        atlas = Image.open(char / "hair.png").convert("RGBA")
        for seg in json.loads(meta.read_text(encoding="utf-8"))["segments"]:
            x, y, w, h = seg["box"]
            ax, ay = seg["at"]
            sheet.alpha_composite(atlas.crop((ax, ay, ax + w, ay + h)), (x, y))
    return sheet


def test_foreground_keeps_off_the_character():
    # >= 3 px clear of her silhouette even with the hair rig's largest swing (3 px)
    char = _char_sheet()
    cw, ch = char.size
    ca = char.getchannel("A").load()
    occupied = {
        (x + CHAR_POS[0], y + CHAR_POS[1]) for y in range(ch) for x in range(cw) if ca[x, y] > 127
    }
    fore = Image.open(BG / "fore.png").convert("RGBA")
    fa = fore.getchannel("A").load()
    near = []
    r = 5
    for y in range(fore.height):
        for x in range(fore.width):
            if fa[x, y] > 127 and any(
                (x + dx, y + dy) in occupied for dy in range(-r, r + 1) for dx in range(-r, r + 1)
            ):
                near.append((x, y))
    assert near == []
    assert sum(1 for y in range(340) for x in range(640) if fa[x, y] > 127) > 1000


def test_the_room_is_darker_and_calmer_than_the_character():
    char = [p[:3] for p in _char_sheet().get_flattened_data() if p[3] > 127]
    char_lum = sum(map(_lum, char)) / len(char)
    assert char_lum > 0.45
    for name in (
        "wall",
        "pipes",
        "shelf",
        "desk",
        "fore",
        *(f"{k}_{s}" for k in ("sky", "city") for s in SKIES),
    ):
        px = _opaque(name)
        mean = sum(map(_lum, px)) / len(px)
        vivid = sum(map(_vivid, px)) / len(px)
        assert mean < 0.25 and mean < char_lum / 2, (name, mean)
        # inside the room nothing is vivid; outside only the lit windows / signs / dawn horizon
        limit = 0.005 if name in ROOM else 0.03
        assert vivid < limit, (name, vivid)


def test_limited_palette():
    room = set()
    for name in ROOM:
        room |= set(_opaque(name))
    assert len(room) <= 64
    for s in SKIES:
        assert len(set(_opaque(f"sky_{s}")) | set(_opaque(f"city_{s}"))) <= 32, s


def test_phone_view_keeps_shelf_and_desk_instrument():
    # portrait: at least 330 of the 640 columns, centred (scene.js resize, minViewW)
    left, right = 320 - 165, 320 + 165
    meta = json.loads((BG / "bg.json").read_text(encoding="utf-8"))
    sx, _, sw, _ = meta["shelf"]
    ox, _, ow, _ = meta["scope"]
    assert sx < left < sx + sw - 40, "at least 40 columns of the shelf"
    assert left < ox and ox + ow < right, "the whole scope screen"
    # and the shelf's visible part is not empty: each compartment has drawn items there
    im = Image.open(BG / "shelf.png").convert("RGBA").load()
    for y0, y1 in ((44, 88), (88, 132), (132, 176), (176, 220)):
        cols = {im[x, y][:3] for x in range(left, sx + sw - 4) for y in range(y0 + 4, y1)}
        assert len(cols) >= 8, (y0, len(cols))


def test_mesh_rects_cover_every_drawn_pixel():
    # the renderer builds each layer's mesh over bg.json "rects" only: nothing may fall outside
    rects = json.loads((BG / "bg.json").read_text(encoding="utf-8"))["rects"]
    assert set(rects) == set(_all_files())
    light = {*GLOW, "neon", *(f"window_glow_{s}" for s in SKIES)}
    for name, (x0, y0, w, h) in rects.items():
        im = Image.open(BG / f"{name}.png").convert("RGBA")
        px = im.load()
        for y in range(im.height):
            for x in range(im.width):
                p = px[x, y]
                drawn = any(p[:3]) if name in light else p[3] > 0
                if drawn:
                    assert x0 <= x < x0 + w and y0 <= y < y0 + h, (name, x, y)
