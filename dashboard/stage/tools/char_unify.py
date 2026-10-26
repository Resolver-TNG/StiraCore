"""One look for every part of the Stage character (#341, art unify). Offline; run by the
builders on everything they draw or cut, so the parts share the icon's line work:

* **Hair has grey lines, not black ones.** The pixelizer draws the hair's strand lines in a
  near-black (``LINE["hair"]``) and outlines every cut part in ``OUTLINE``. In the icon they
  read as thin grey strokes; at 1 px on the light hair they read as black cracks, and where a
  hair part's edge lands inside the hair in a pose (a tilted head: the crown re-pixelized, the
  hair layer's parts moved rigidly) the part's black outline became a black seam. Here a hair
  line pixel takes a hair grey by the light around it (``HAIR_LINE_LIT`` on light hair,
  ``HAIR_LINE`` elsewhere), and an outline pixel that touches only hair (and the outside) takes
  ``HAIR_LINE``: the hair keeps its strands and its silhouette, in its own colour.
* **No isolated dark specks.** A single pixel much darker than all eight neighbours (a dither
  or line leftover) takes the colour most of its neighbours have; a part's stray islands of
  one or two pixels (hair pixels left on the coat by the cut, re-pixelized per pose) are
  dropped (the part behind is completed there).

The lint (``tests/stage_art_lint_test.cjs``) checks the outputs with the same palette.
"""

from __future__ import annotations

import build_char as bc
import numpy as np


def _rgb(h: str) -> np.ndarray:
    return (bc.hexrgb(h) * 255).round().astype(np.int16)


HAIR_RAMP = [_rgb(c) for c in bc.RAMPS["hair"]]
HAIR_LINE = bc.RAMPS["hair"][1]  # strand lines and the hair's own outline
HAIR_LINE_LIT = bc.RAMPS["hair"][2]  # strand lines on the light hair
SPECK_DELTA = 60  # luma: a pixel this much darker than every neighbour is a speck
ISLAND_MAX = 2  # px: a part's stray groups this small are dropped
HAIR_INSIDE = 5  # of 8 neighbours in hair: a black pixel there lies inside the hair


def _neigh(a: np.ndarray, fill=0):
    """The 8 neighbours of every pixel: array (8, h, w, ...)."""
    h, w = a.shape[:2]
    pad = np.pad(a, ((1, 1), (1, 1)) + ((0, 0),) * (a.ndim - 2), constant_values=fill)
    out = []
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            if dx or dy:
                out.append(pad[1 + dy : 1 + dy + h, 1 + dx : 1 + dx + w])
    return np.stack(out)


def hair_lines(rgba: np.ndarray) -> np.ndarray:
    """Recolour the hair's near-black lines and its outline to hair greys (see the module doc)."""
    out = rgba.copy()
    rgb = rgba[..., :3].astype(np.int16)
    opaque = rgba[..., 3] > 0
    level = np.full(opaque.shape, -1, np.int8)  # hair ramp index, -1 = not a hair ramp colour
    for i, c in enumerate(HAIR_RAMP):
        level[(rgb == c).all(2) & opaque] = i
    is_line = (rgb == _rgb(bc.LINE["hair"])).all(2) & opaque
    is_out = (rgb == _rgb(bc.OUTLINE)).all(2) & opaque
    hairish = (level >= 0) | is_line
    nl = _neigh(level, -1)
    n_hair = _neigh(hairish, False).sum(0)
    n_out = _neigh(is_out, False).sum(0)
    n_opaque = _neigh(opaque, False).sum(0)
    n_other = n_opaque - n_hair - n_out  # skin, coat, navy, eyes ... next to it
    valid = nl >= 0
    mean = np.where(valid, nl, 0).sum(0) / np.maximum(valid.sum(0), 1)
    line_lit = is_line & valid.any(0) & (mean >= 3.5)
    out[is_line, :3] = _rgb(HAIR_LINE)
    out[line_lit, :3] = _rgb(HAIR_LINE_LIT)
    edge = is_out & (n_hair >= 3) & (n_other == 0)
    # inside the hair (most neighbours hair: a seam, not a border to the skin or the coat)
    edge |= is_out & (n_hair >= HAIR_INSIDE)
    out[edge, :3] = _rgb(HAIR_LINE)
    return out


def specks(rgba: np.ndarray) -> np.ndarray:
    """Isolated dark pixels take their neighbours' most common colour."""
    out = rgba.copy()
    rgb = rgba[..., :3].astype(np.int32)
    opaque = rgba[..., 3] > 0
    luma = rgb @ np.array([299, 587, 114]) // 1000
    nl = _neigh(np.where(opaque, luma, -1), -1)
    all_in = (nl >= 0).all(0) & opaque
    speck = all_in & (nl.min(0) - luma >= SPECK_DELTA)
    if not speck.any():
        return out
    nrgb = _neigh(rgb, 0)
    for y, x in zip(*np.nonzero(speck), strict=True):
        cols, counts = np.unique(nrgb[:, y, x], axis=0, return_counts=True)
        out[y, x, :3] = cols[np.argmax(counts)]
    return out


def islands(rgba: np.ndarray, max_px: int = ISLAND_MAX) -> np.ndarray:
    """Drop connected groups (8-neighbour) of at most `max_px` opaque pixels."""
    from scipy import ndimage as ndi

    out = rgba.copy()
    opaque = rgba[..., 3] > 0
    labels, n = ndi.label(opaque, structure=np.ones((3, 3)))
    if not n:
        return out
    sizes = np.bincount(labels.ravel())
    small = (sizes <= max_px) & (np.arange(len(sizes)) > 0)
    # only stray *hair* pixels: a small group of another colour may be all a part has there
    rgb = rgba[..., :3].astype(np.int16)
    hairish = np.zeros(opaque.shape, bool)
    for c in [*HAIR_RAMP, _rgb(bc.LINE["hair"]), _rgb(bc.OUTLINE)]:
        hairish |= (rgb == c).all(2)
    not_hair = np.bincount(labels.ravel(), weights=(opaque & ~hairish).ravel(), minlength=n + 1)
    drop = small & (not_hair == 0)
    out[drop[labels]] = 0
    return out


SKIN_RAMP = [_rgb(c) for c in bc.RAMPS["skin"]]


def skin_contour(rgba: np.ndarray) -> np.ndarray:
    """Arms: a black outline pixel next to the skin becomes the skin line (the icon's hand has a
    thin brownish contour; the moved / drawn arm sprites had a black ring around the hand)."""
    out = rgba.copy()
    rgb = rgba[..., :3].astype(np.int16)
    opaque = rgba[..., 3] > 0
    skin = np.zeros(opaque.shape, bool)
    for c in SKIN_RAMP:
        skin |= (rgb == c).all(2)
    skin &= opaque
    pad = np.pad(skin, 1)
    near = pad[:-2, 1:-1] | pad[2:, 1:-1] | pad[1:-1, :-2] | pad[1:-1, 2:]
    black = (rgb == _rgb(bc.OUTLINE)).all(2) & opaque
    out[black & near, :3] = _rgb(bc.LINE["skin"])
    return out


def unify(rgba: np.ndarray, part: bool = False) -> np.ndarray:
    """part: a cut or drawn part on its own (not a whole sheet): its stray islands go too."""
    out = specks(hair_lines(rgba))
    return islands(out) if part else out
