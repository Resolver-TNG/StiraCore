# Stage assets — origin and rights

**License:** the assets in this directory (`char/`, `bg/`, `gadgets/`, `source/` and their
JSON), and the README screenshots in `docs/images/`, are licensed under
[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/); the full
text is in [`LICENSE-ASSETS`](../../../LICENSE-ASSETS) at the repository root. Attribution
example: `Stage art by Resolver-TNG (https://github.com/Resolver-TNG), CC BY 4.0`. The bundled
three.js (`../vendor/`) is MIT and the code is Apache-2.0; project names and logos are not
part of any licence.

| Path | What | Origin |
|---|---|---|
| `source/resolver-icon.jpg` | The character "Re:Solver" (circle crop of the icon, 1112×1112) | An original character by the project owner: generated from the owner's own text description with Google Gemini and then edited by the owner (not based on the settings or characters of any existing work). The rights are the owner's; published under CC BY 4.0 with the other Stage assets. Only the round icon is kept (no profile page around it). |
| `source/resolver-mask.png` | Subject mask of the icon | Derived: macOS Vision foreground mask (`tools/subject_mask.swift`). |
| `char/*.png`, `char/char.json` | Pixel-art character sheet, layer weight maps, expression sprites | Derived from the icon by `tools/build_char.py` (palette, dither, line art and expressions authored in code). |
| `char/parts.png`, `char/parts.json` | Layered parts of the character (turtleneck, coat, neck, face, hair placeholder, ears, mug arm), their variants per pose, the back view, eyes / mouths / brows, arm sprites, the desk mug, layer order and anchors | Derived from the icon by `tools/char_parts.py` (run by `build_char.py`): the icon's pixels re-pixelized per pose; the body below the circle, the hidden parts' completion, the free hand, the sleeves, the back view and the face sprites are drawn in code. No external image. |
| `char/hair*.png`, `char/hair.json` | Hair parts and tip segments (atlas, rim map, depth mask, rig definition) | Cut from the same sheet by `tools/build_hair.py` (no new drawing, no external image). |
| `char/hair_back*.png`, `char/hair_back.json` | The back view's hanging hair locks (the hair layer's back set: atlas, rim map, mask, rig definition) | Drawn in code by `tools/char_back.py` (the back view is constructed; no external image) and cut into segments there. |
| `char/desk.png`, `char/desk.json` | The desk shot (#341): her figure walking to the room desk and back, seated there with her back to us (typing frames), the office chair, the tail (six poses), her mug on the desk | Built by `tools/char_desk.py` from the back / front / profile views the rig draws (`tools/char_desk_compose.mjs`), downscaled into her palette; the coat skirt, legs, chair, tail and mug are drawn in code. The design follows the commander's reference sheets (not in the repository). No external image. |
| `char/extras.png`, `char/extras.json` | Extras for the motion layer (#341): ear variants (rest, flattened, twitch, blushing at every ear angle), the tail beside her in the portrait sheets (front sway / up / down, back view), the earbud | Built by `tools/char_extras.py`: the ears re-pixelized from the icon's own ears (turned, shortened, recoloured in her palette), the tail and the head-top dome drawn in code, the earbud cut from base.png. The design follows the commander's reference sheets (not in the repository). No external image. |
| `char/fx.png`, `char/fx.json` | Face effects for the motion layer (#341): blush (soft / strong / nose), sweat (drop / flying), the fluster's blue lines, the anger mark, a tear, sparkles (three twinkle frames) | Drawn in code by `tools/char_fx.py` as small pixel shapes in her palette (no font symbols, no icons). The design follows the commander's expression sheet (not in the repository). No external image. |
| `char/poses.png`, `char/poses.json` | Whole-pose arm sprites for the motion layer (#341): arms crossed (ドヤ), thinking with the arm across the waist (考え事) — both arms per pose | Drawn in code by `tools/char_poses.py` with the same sleeves, knit cuffs and hands as the other free arms (`char_arms.py`). The design follows the commander's reference sheets (not in the repository). No external image. |
| `bg/*.png`, `bg/bg.json` | Lab background: sky and city per sky variant (night / dawn / rain), window glass and rain drops, wall (frame, blind, curtains, posters), neon orbits, pipes, shelf, desk, foreground, light layers | Drawn procedurally in code by `tools/build_bg.py` (+ `build_bg_sky.py`, `build_bg_room.py`, `build_bg_props.py`); no external image. Sign glyphs are abstract, no real text or logo. |
| `gadgets/*.png`, `gadgets/gadgets.json` | Props for actions (laptop, signal lamp, drones, beacon, magnifier, binder, sticky note, archive box, approval slip, seals 可 / 否, stamper, hourglass, bell, bubbles, digits) and their effect map | Drawn in code by `tools/build_gadgets.py` (pixel maps and a small 3/4 projection, lab and character palettes); no external image. |
| `../../../docs/images/*.png` | README screenshots: the Stage, its whiteboard, and the chat, approval, memory and observability pages (English and `-ja` Japanese UI) | Screenshots of the Stage and the web UI, generated from the running build, 2026-10-27: captured from a local server with a fresh data directory on 2026-10-26, downscaled to at most 1600 px wide and reduced to 256 colours. They show the assets above; no external image. |

A commercial game screenshot was looked at only as a *style* reference (limited palette,
dithering, neon outline shapes, smoke, text window). Nothing from it is traced, copied,
embedded or committed.

`../vendor/three.stage.min.js` is three.js r186 (MIT, see `../vendor/LICENSE.three.txt`).
