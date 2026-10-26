"""Whole-pose arm sprites for the motion layer (#341, art unify 5), after the owner's sheets
05 (ドヤ: arms crossed) and 01 (考え事: the hand at the chin, its elbow resting on the other arm
laid across the waist). Their own files (``assets/char/poses.png`` + ``poses.json``); the
parts atlas is unchanged. Contract: docs/stage.md, "Asset contract: pose arms".

Each pose is two sprites, one per arm, with the parts atlas' arm semantics: ``arms.r.<name>``
is drawn instead of the mug arm (the rig's ``forearm_r`` / ``hand_r`` slot, with the free coat;
no mug in the hand), ``arms.l.<name>`` in the ``arm_l`` slot (over the right arm); ``ox`` /
``oy`` are sheet pixels of the base pose, moved like the parts atlas' arms (the right arm with
the chest anchor, the left with the left shoulder anchor).
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np

# name -> per arm: wrist, angle, hand pose, elbow, side, size, sleeve widths (wrist, elbow),
# shoulder (an upper-arm sleeve from the shoulder to the elbow, drawn first; None: the coat's)
POSES = {
    # 05 ドヤ: arms crossed over the chest — her right forearm across under, its hand by the left
    # elbow; her left forearm over it, its hand gripping the right upper arm
    "cross": {
        "r": ((168.0, 226.0), -10, "fist", (64.0, 246.0), 1, 1.1, (21.0, 31.0), (76.0, 184.0)),
        "l": ((100.0, 214.0), 196, "relaxed", (212.0, 244.0), -1, 1.1, (21.0, 31.0), None),
    },
    # 01 考え事: her right forearm laid across the waist (the hand under the left elbow), the left
    # elbow resting on it, the left hand at the chin (the index finger along the jaw)
    "ponder_cross": {
        "r": ((186.0, 250.0), -6, "fist", (62.0, 258.0), 1, 1.1, (21.0, 31.0), (76.0, 184.0)),
        "l": ((174.0, 148.0), -158, "chin", (200.0, 246.0), -1, 1.1, (19.0, 29.0), None),
    },
}


def arm_sprite(parts, wrist, angle, pose, elbow, side, size, widths, shoulder):
    """As char_arms.free_hand (the sleeve, the navy knit cuff, the hand), with the upper arm's
    sleeve first when the coat has none there (the free coat is open in front)."""
    import char_arms as ca
    from char_unify import unify

    cv = ca.Canvas(parts.px)
    if shoulder is not None:
        ca.sleeve(cv, elbow, shoulder, widths[1] + 1, widths[1] + 3, ca.COAT, cuff=False, base=0.72)
    fwd = ca.rot((1, 0), angle)
    w = np.array(wrist, np.float32)
    cuff = 4.5 * size
    ca.sleeve(cv, tuple(w - fwd * (cuff + 1.0)), elbow, widths[0], widths[1], ca.COAT, base=0.74)
    ca.knit_cuff(cv, tuple(w + fwd * 1.0), tuple(fwd), 9.5 * size, cuff + 2.0, base=0.46)
    ca.hand(cv, wrist, angle, pose, side, size)
    return unify(cv.render(soft=True, behind=parts.union), part=True)


def _crop(img):
    a = img[..., 3] > 0
    ys, xs = np.where(a)
    y0, y1, x0, x1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
    return img[y0:y1, x0:x1], int(x0), int(y0)


def build(parts, out_dir: Path) -> dict:
    items = []
    for name, arms in POSES.items():
        for side, spec in arms.items():
            spr, x0, y0 = _crop(arm_sprite(parts, *spec))
            items.append((side, name, spr, x0, y0))
    width = 512
    order = sorted(range(len(items)), key=lambda i: -items[i][2].shape[0])
    x = y = row_h = 0
    placed = {}
    for i in order:
        h, w = items[i][2].shape[:2]
        if x + w > width:
            x, y, row_h = 0, y + row_h + 1, 0
        placed[i] = (x, y)
        x += w + 1
        row_h = max(row_h, h)
    atlas = np.zeros((y + row_h, width, 4), np.uint8)
    meta = {"version": 1, "size": [parts.px.tw, parts.px.th], "arms": {"r": {}, "l": {}}}
    for i, (side, name, spr, ox, oy) in enumerate(items):
        ax, ay = placed[i]
        atlas[ay : ay + spr.shape[0], ax : ax + spr.shape[1]] = spr
        meta["arms"][side][name] = {"ox": ox, "oy": oy, "w": int(spr.shape[1]),
                                    "h": int(spr.shape[0]), "ax": int(ax), "ay": int(ay)}  # fmt: skip
    import char_parts as cp

    cp.save_indexed(atlas, out_dir / "poses.png")
    (out_dir / "poses.json").write_text(json.dumps(meta, indent=1) + "\n", encoding="utf-8")
    return {"poses": [width, int(atlas.shape[0])], "poseSprites": len(items)}
