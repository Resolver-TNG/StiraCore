# Stage — pixel-art view of the agent's activity and emotion (#341)

Stage shows what the agent is doing and how its state moves, as a 2.5D pixel-art
visual-novel scene: a character in a lab at night, a text window with a name plate, and a
choice window for approvals. It is meant to make the state **easier to read at a glance**
and to make the character's **consistency observable**. It adds no permission. Its only
writes go through two existing routes with their existing protections: approval decisions
(`/api/hitl/<id>/decision`) and chat messages (`POST /api/chat`, CSRF + `Idempotency-Key`,
the chat page's own code; see "Conversation" below).

Two ways to open it:

- **Live (stage 2), opt-in:** start the chat server with `STIRA_STAGE=1` and open
  `http://127.0.0.1:<port>/stage/` (a "Stage" item appears in the sidebar; the page sits in
  the dashboard shell, right of the side menu). The page reads Stira's own APIs and shows
  real turns, tools, approvals, Neural State and the breaker, and the text window holds the
  composer (#350). Without `STIRA_STAGE=1` there is no `/stage` route and no nav item.
  Gateway `ui.home: "stage"` (default `"chat"`) makes `/` open the Stage and lists it first
  in the menu; it needs `STIRA_STAGE=1` (otherwise `/` stays the chat). `/chat` always stays.
  A reply's ```` ```svg ```` block is drawn on the whiteboard as a picture rebuilt from an
  allow-list (`js/svg_safe.js`: no script, link, outside resource or animation; #453) — **on
  by default** since the ADR 0012 addendum of 2026-10-24; Settings → "Board (Stage)" turns it
  off (`ui.board_svg: false`), turning it back on is confirmed. The log keeps showing code.
- **Demo (stage 1):** `python3 -m http.server 8765 -d dashboard` and open
  `http://127.0.0.1:8765/stage/index.html` (served from `dashboard/` so that the page finds
  the translation catalog in `static/i18n/`). It replays dummy events; a yellow `DEMO`
  badge says so. `?demo=1` on the live page plays the demo too.
- Keys: `Tab` raw view, `D` debug panel (hidden by default), `L` language (ja / en; shared
  with the dashboard's language setting), `Enter` finishes the typed line. They apply only
  while the page itself has the focus: typing in the composer, the menu or any button keeps
  its keys (Tab moves the focus there). In the composer: `Enter` sends, `Shift+Enter` new
  line, `Enter` while an IME is converting never sends, `↑` in an empty composer opens the
  log, `Esc` closes it. `Esc` also closes the raw view (it covers the text window) and rolls the
  whiteboard up (when the log is not open). Demo only: `Enter`
  next step, `1`–`9` jump to a demo state, letters `c t y u s r g m k h x a n` jump to the
  gadget demo states (see "Gadgets"), `0` autoplay.
- URL parameters (for screenshots): `?lang=ja|en&raw=1&debug=1&layers=1&seed=N`, demo:
  `&state=<id>&instant=1`; `&frames=N` (fixed 1/60 s steps, frozen after N frames: the same
  state gives the same picture), `&ambient=0` (room ambient off), `&sky=night|dawn|rain|auto`
  (the window's sky; default `night`, `auto` = dawn from 05:00 to 06:59 local time, otherwise
  night), `&layers=1` also tints the background bands. Live page checks (#350 phase 2):
  `&split=1` draws with two canvases even while the board is up (to compare with one canvas),
  `&webgl=0` shows the still fallback. `&fps=15..120` sets the render rate (below).

## Rules

1. **Nothing is decoration.** Expression, steam, prop lights, neon and the choice window are
   derived only from state carried by events (Neural State, breaker level, open turn, running
   tools, pending approval). With no event the scene shows no "busy" look. Only life signs that
   encode nothing run on their own: breathing, blinking, hair sway, a small ear flick on the
   character, and the room's short list of slow ambient entries (see "Layers").
2. **Every derived value names its cause.** `derive()` returns, for each channel, the value and
   the event that set it (`steam = 2 ← turn open (#3 turn.start)`). The debug panel (`D`) and
   the raw view list them.
3. **Raw view.** `Tab` hides the character and the scene and shows the same state as numbers,
   the derived values with their causes, and the event log. Same events, no
   anthropomorphism — so the picture cannot be trusted more than the data behind it.
4. **The agent cannot run code here.** See "Animation catalog" below.

## Architecture

```
events ──reduce()──▶ state ──derive()──▶ pose (+ causes) ──▶ animator ──▶ renderer / UI
 (demo, or Stira's        pure            pure, deterministic     catalog         three.js
  APIs via adapter.js)                                              (data)
```

- `js/model.js` — `reduce(state, event)` and `derive(state)`; no DOM, tested in Node.
- `js/catalog.js` — the declarative animation catalog and its validator.
- `js/anim.js` — interpreter for catalog entries (curves, envelopes, seeded idle timing).
- `js/scene.js` — three.js renderer: the scene is drawn at a low internal resolution
  (about 270–330 rows, e.g. 433×286 in a 1300×860 window) into a `NearestFilter` render
  target and blitted at an integer scale. Glow, light pools, the neon halo and the steam are
  quantised with a 4×4 ordered dither so they stay pixel art.
- `js/pacer.js` — the frame pacer (pure): the default 30 fps cap, the `?fps=` / `localStorage`
  validation and the accumulator; see "Frame rate (30 fps)".
- `js/director.js` — the demo: a list of *events* per step, not looks.
- `js/room.js` — the room (background) frame: which room values a frame has, from room
  channels and the parallax only; the ambient list.
- `js/gadget_model.js`, `js/gadget_catalog.js`, `js/gadget_scene.js`, `js/demo_gadgets.js` —
  props brought in by actions (stage 2, see "Gadgets"): the pure mapping, the declarative
  data, the renderer layer and the demo events.
- `js/adapter.js` (pure), `js/live.js`, `js/live_ui.js`, `js/approval.js` — live mode:
  Stira's APIs → Stage events, polling and pacing, the approval choice window (below).
- `js/ui.js`, `js/i18n.js` — text window (typewriter), choices, raw view, debug panel; the
  strings are the dashboard catalog's `stage.*` keys (ja / en).
  All text is set with `textContent`.
- `vendor/three.stage.min.js` — three.js r186 (MIT, `vendor/LICENSE.three.txt`), bundled
  locally with esbuild. No CDN, no inline script or handler, no web fonts, no request other
  than the page's own assets (compatible with Stira's CSP: `script-src 'self'`).

### Layers

(Spike; superseded by the layered parts in "The character: parts and motions" below. The
spike's sheet, weight maps and overlays are still built and loaded, but the character layer
now shows the composited parts.)

The character is one 278×278 palette-quantised sheet plus weight maps (`rig.png`,
`rig2.png`) for its layers: body, head (face, crown, earbud), left ear, right ear,
arm + mug. Each layer moves by whole pixels with its own depth, so the pointer parallax and
the idle motion never blur. The hair is cut into parts with their own rig (see "Hair rig"
below). `?layers=1` tints the layers and each hair part.

Expressions are small overlay sprites (`face.png`): eyes `open closed half up happy wide
down`, mouths `neutral smile grin flat frown open`, blush, sweat drop. The eye variants are
made from the original eye pixels (lid moved, iris shifted); the bangs are a hair part drawn
on top, so they stay in front of the eyes.

Background layers (sky, city, rain, window glass, wall, neon, pipes, shelf, desk, foreground)
are drawn in code by `tools/build_bg.py` and listed as data in `js/bg_layers.js` (see
"Background" below). Prop meaning: **shelf = memory**, **desk instrument = tools**,
**window = the outside world** (web), **mug steam = a turn is open (thinking)**,
**neon = breaker level / approval**.

## Layers (room, character, props)

The scene is a 2.5D stack of three independent groups, each with its own parallax depth and
its own animation:

- **Room** (the background layers of `js/bg_layers.js`: sky, city, rain, window glass, wall,
  neon, pipes, shelf, desk, foreground and their light layers) is
  placed from `room.js` `roomFrame()` only: state channels from events (prop lights, scope
  trace, neon colour and flicker, window glow, shelf scan), the pointer parallax and the
  ambient entries below. It reads no character channel, so breathing, blinking, posture and
  motions never move or relight the room (`tests/stage_layers_test.cjs` feeds random character
  channels and a 40 s run of idle and gestures and checks that the room frame does not change).
- **Character** is placed by the parallax at its own depth (1.0); her motion happens inside
  her own texture (the layered parts composited by `char_rig.js`), not by moving the camera. The spike's idle camera drift is
  removed, and the idle scope can no longer drive the camera or any room channel.
- **Props** (gadgets) use their own depth (shelf props 0.55, beacon 0.36, laptop 1.18, drones
  1.3) and their own state animations.
- Parallax comes from the pointer only and settles exactly (no residual drift); deeper layers
  move less than the character, props in front move more.
- The CRITICAL shake is something that happens to her: it moves (1 px) only the layers at her
  depth or in front (`SHAKE_MIN_DEPTH = 1.0`: the character, laptop, lamp, drones), never the
  room or the props fixed in it (shelf props, beacon, slip, hourglass). The steam follows her
  mug, so it shakes with her.
- Mug steam (a turn is open) is one or two thin wisps: small points (3 → 7 px) that join into
  a thread, rise about 26 art pixels over the turtleneck, curl a little and thin out upward,
  drawn in the 3 steam tones with ordered dither so the edges are half-transparent; it fades
  below her chin and does not cover her face or hair (`STEAM` in `room.js`, rate × the
  `state.steam` puff).

**Two canvases while the whiteboard is down (#350 phase 2).** The board (DOM) sits between the
room and her: the character group (her layer, the hair, the mug steam, the sparkles and the
props at depth ≥ 1) is on its own three.js render layer; while the board is shown the room is
drawn first and copied to a back canvas (`#view-room`), then the character group is drawn on
the transparent front canvas (`#view`). Additive lights of the character group keep the
target's alpha, so they still add to what is behind. With the board up, the room's `cam`
pans 12 art pixels (8 on a phone, × depth) and the layers at depth ≥ 1 get `aside` (she steps
to the right edge); both are whole pixels from the board's eased progress, an input of
`roomFrame()` that carries no character channel and nothing of the board's content. The
background bands and the foreground do not step aside. With the board up the foreground is on
the back canvas, so it is behind her. Without the board one canvas, one pass, as before:
`?split=1` (two passes always) gives the same picture pixel for pixel at 1300 and 390 px
(docs/research/stage-main-ui-2026-10-17/phase2/).

The room's ambient motion is a fixed, short list (scope `ambient`; only the channels in
`AMBIENT_CHANNELS`, always on, looping, at least 4 s per cycle, no pulse; checked by the
validator and by the test against this list). `?ambient=0` turns it off.

<!-- stage-ambient:begin -->
| Entry | What moves |
|---|---|
| `ambient.neon` | neon rings breathe very slightly (±5 %, 7.5 s cycle) |
| `ambient.city` | a few lit windows of the night city go on or off, one change every ~4 s |
| `ambient.rain` | rain falls past the window (4 s loop; only the `rain` sky has a rain layer) |
<!-- stage-ambient:end -->

The mug steam is not ambient: it is the open turn (state). Nothing else in the room moves on its
own.

## Background (layers, bands, skies)

The room is a stack of flat pixel-art images, one entry each in `js/bg_layers.js` (`BG_LAYERS`,
validated by `validateLayer` and frozen; `layersFor(sky)` gives the list to draw). An entry
names its band, kind (`color`, `glow` = additive light shown only by its event, `neon`,
`rain` = drawn by the shader), image, parallax depth and draw order; a `role` marks the layer
an event lights (`room.js`). The renderer builds one mesh per entry and places every one with
the same `layerOffset(roomFrame, depth)`, so each layer has its own parallax and none reads a
character channel (`tests/stage_bg_test.cjs`, `tests/stage_layers_test.cjs`). A depth or order
outside the band, an unknown kind/role/sky, or a file name outside the page's asset allow-list
is refused. `?layers=1` tints the bands (far blue, mid green, near orange, fore magenta).
Each mesh covers only the rectangle its image draws (`bg.json` `rects`, measured by the
builder; the shaders still address room pixels), so the extra layers cost less fill work than
the spike's full-room quads (SwiftShader, 1300×860: 581–760 ms/s main-thread task time
against 690 ms/s before; measurements in `docs/research/stage-2026-10-17-background/`).

<!-- stage-bg:begin -->
| Band | Entry | Depth | What |
|---|---|---|---|
| far | `sky` | 0.06 | sky, moon or clouds, far skyline (per sky) |
| far | `city` | 0.12 | nearer blocks: lit windows, rooftop tanks and masts, signs with abstract glyphs, the elevated line with a train (per sky); `ambient.city` |
| far | `rain` | 0.16 | rain streaks past the window (`rain` sky only); `ambient.rain` |
| far | `window_glow` | 0.12 | the window's light — **web** tools only (per sky) |
| mid | `glass` / `glass_rain` | 0.3 | reflections on the pane / drops and trails (`rain` sky) |
| mid | `wall` | 0.3 | concrete panels, ceiling, wainscot, window frame with sill and latches, rolled blind, curtains, a node-diagram poster, notes, a card grid, conduit with a switch, vent, sockets |
| mid | `neon` | 0.34 | the neon orbits — breaker level / approval colour |
| mid | `pipes` | 0.42 | pipes with straps and flanges, a cable bundle, valve wheel, gauge, junction box |
| near | `shelf` | 0.55 | the shelf = **memory**: top to bottom procedural (binders, manuals), semantic (reference books, specimen jar, card index), episodic (file boxes, diaries, a photo, letters), archive row; a globe, a chart roll and a trailing plant on top; a filing cabinet at the far left |
| near | `shelf_glow` | 0.55 | shelf LEDs, light pools, liquids — **memory** tools only |
| near | `desk` | 0.8 | the desk = **tools**: oscilloscope (handle, knobs, BNC inputs), rack with LEDs, display and patch cables over the edge, a monitor on an arm, a lamp, pens, a cactus, a PC under the desk |
| near | `desk_glow` | 0.8 | scope trace, LEDs, monitor lines, lamp — **tool** runs only |
| fore | `fore` | 1.1 | a dark pendant lamp and leaves in the corners, kept 6 px off the character's silhouette at rest — base sheet and hair — so at least 3 px stay clear at the hair rig's largest swing (checked when built and by a test); part of the room, so it does not shake at CRITICAL |

Skies (`SKIES`; they swap only the far band's images and the glass): `night` (default: moon,
stars, lit windows), `dawn` (warm horizon, few windows lit, thin clouds), `rain` (low
clouds, haze, rain, drops on the glass). The sky is a setting (`?sky=`) or the local clock
(`?sky=auto`); it never changes what an event shows.
<!-- stage-bg:end -->

How the room is kept behind her: every colour layer is dark and muted (mean luminance
0.08–0.23 against the character's 0.52, base sheet + hair; no strongly saturated bright pixel inside the room,
and outside the window at most 2.5 % — lit windows, signs, the dawn horizon),
and the room shares one 64-colour palette (each sky 32 more), quantised at build time
(`tests/test_stage_bg_assets.py` measures these). The phone view (330 of the 640 columns,
x 155–485) keeps the shelf's right half, the scope and the rack.

## Hair rig (parts, tip segments, follow-through)

The hair of the icon is cut into five parts, each into a root and tip segments that hang from
their parent (`tools/build_hair.py`, run by `build_char.py`; outputs `assets/char/hair.json`,
`hair.png`, `hair_fx.png`, `hair_mask.png`):

```
head (crown + ears stay in the head layer)
├─ bangs   bangs.0 ─ bangs.1 ─ bangs.2                       front
├─ side_l  side_l.0 ─ side_l.1 ─ side_l.2 ─ side_l.3        front (her right lock, image left)
│           └─ wisps_l.0 ─ wisps_l.1                         front (stray strands, "ahoge")
├─ side_r  side_r.0 ─ side_r.1 ─ side_r.2 ─ side_r.3        front
│                      └─ wisps_r.0 ─ wisps_r.1               front
└─ back    back_l.0 ─ back_l.1 ─ back_l.2                    back (behind the coat)
           back_r.0 ─ back_r.1 ─ back_r.2
```

- **Definition is data.** `hair.json` lists every segment: id, part, chain, parent, depth,
  `z` (front / back), side, pivot, rest box in sheet pixels, atlas position, and the share of
  the breath offset at its pivot, in draw order. The polygons and cut lines that make them are
  at the top of `build_hair.py` (hand-authored against the 278 px grid, like the other regions).
- **Long hair below the crop.** The icon is a circle crop, so its hair stopped at the circle
  (`build_char.py` only stretched the edge pixels into the corners). `extend_hair()` draws the
  back hair on outside the shoulders down to upper-arm height: every pixel beyond the crop
  follows the hair flow (the slope of the real silhouette) back to the last real hair pixel,
  continues its shade from the hair ramp (re-dithered at its own position, darker toward the
  tips, strand lines continued) and ends in 7 px clumps with pointed tips between rows 258 and
  276 (not at the frame's edge), with the outline. It never paints over the coat (the coat's
  closed area keeps its own texture; completing the coat and arms is the motion layer's work).
  The drawn hair belongs to `back_l.2` / `back_r.2` and moves with the rig.
- **Drawn where the cut was too strong.** Beside the cheek (image left, cheek to throat) the
  icon's hair is thin light strands on a light background, so the subject mask left a ragged
  edge with holes; that part of the lock is drawn instead (`draw_locks()`: one lock between a
  smooth outer and inner contour meeting in a tip, no holes, shaded across its width with the
  hair ramp and a sheen band, ordered dither, strand lines along the flow, the outline outside).
  Beside the face on the image right, the ear and the earbud between the strands had been
  coloured as hair (the material split found skin only inside the face polygon);
  `restore_ear()` gives the pixels whose source colour is skin the skin ramp and the dark earbud
  the mug ramp, so the hair strands pass in front of a skin-coloured ear.
- **No seams.** Each sprite reaches 2 px into its parent (or the crown) and continues 3 px
  under what hides it (the arm for front hair, the body for back hair). The pixels the hair
  covered in `base.png` are filled: the body continues 4 px under the hair (nearest skin /
  coat / turtleneck colour, never the iris), then a dark hair underlay; strands that stuck out
  into the background leave nothing behind; next to the ears the original hair stays (the ears
  move by their own weight map). Rim light comes from the uncut silhouette. With every offset
  at 0 the picture is the uncut sheet, pixel for pixel: `build_hair.py` checks it on the CPU
  and stores a hash (`restHash`) that `tests/stage_hair_test.cjs` recomputes from the committed
  files; the headless-Chrome captures in `docs/research/stage-2026-10-17-hair/` compare it
  on screen.
- **Depth without a depth buffer.** `js/hair_layer.js` draws all segments in one pass right
  after the character (render order 10.1, before front props and steam): front parts over the
  body except the arm and mug, back parts only where no body pixel is. The mask comes from
  the character layer for every frame (`char_bridge.js`, see "Layered parts"), so the arm
  moving to the mouth or the keys is in front of the hair where it is; without it
  (`hair_mask.png`) the body pixel is found with the spike shader's offset formula.
- **Follow-through** (`js/hair_rig.js`, pure): each segment hangs from its parent's position
  (a root from the head the character layer actually drew) on a damped spring that sees its
  anchor late. Only a change of the **head pose** goes through the springs. Per part and depth,
  in `HAIR_RIG`: `follow` (share of the parent's motion it keeps; the rest stays with the body),
  `delay` (s), `maxAmp` (px cap of the swing; 0 = exactly with its parent), `stiffness`,
  `damping` (per depth; ≥ 0.8 everywhere, so no tip swings past), `sway` (> 0: takes the shared
  idle sway). The step is fixed (1/240 s) with the input interpolated inside a frame, offsets
  snap to whole pixels with hysteresis, and a segment is never more than 1 px from its parent.
  The hair over the face (the bangs; the root and the lock along each cheek; the wisps' roots)
  follows the head exactly, so the fringe never slips off the face when the pose changes;
  only the free tips trail a head move, a pixel per segment. Nothing is random, so the same
  sequence of head poses always gives the same hair (tested frame by frame).
- **In phase** (#341, night round): the **breath** (1 px) and the idle **sway** (`hair.sway`,
  `SWAY_PX` = 1 px, with hysteresis) are added after the springs as one whole-pixel offset
  shared by every segment that takes them, so all of the hair rises with the chest and the
  tips sway as one piece in the same frame. Before, each depth saw them through its own delay
  and spring and switched pixels at its own moment: row bands moved one after the other (a
  wave running down her) and the tips swung up to 2 px (a 3 px head step: up to 3 px, with an
  overshoot). Measured in the browser, calm, 10 s: 110 offset changes, up to 3 different x
  offsets and up to 3 different y offsets in one frame → 12 changes, 1 and 1 (tests:
  `tests/stage_hair_test.cjs`; report: `docs/research/stage-motion-night-2026-10-18/`).
- **Looks per state** come only from the derived pose (`hairStyle(pose)`, first match wins);
  the style is added to the pose as `hair` with its cause (raw view, drift check) and sets
  per-part lifts, a one-time kick when it starts and stiffness / damping multipliers
  (`HAIR_STYLES`):

<!-- stage-hair:begin -->
| Style | When | Hair |
|---|---|---|
| `bristle` | emotion is tense | wisps stand 2 px up and out, side tips flare 1 px, stiff and quivering |
| `droop` | emotion is worried | tips hang 1 px down and in, the wisps fall 2 px, heavier |
| `alert` | ears folded back by vigilance | wisps stand 2 px up and out |
| `bounce` | emotion is joy | one hop when it starts, the wisps stay up 1 px |
| `perk` | emotion is curious | the wisp tips lift 1 px, a little stiffer |
| `calm` | otherwise | rest |
<!-- stage-hair:end -->

- **Draw order is data.** `hair.json` `layerOrder` is the insertion contract with the
  character layer's own groups, back to front: `hair:back`, `char:body`, `hair:side_l`,
  `hair:side_r`, `hair:wisps`, `char:face`, `hair:bangs`, `char:arms`; each part names its
  `slot` (`behind:body`, `over:body`, `over:face`). Whether a segment is in front of the arm and
  the mug is `overArm` per part and depth in `HAIR_RIG` (default: behind, as in the icon; e.g.
  `overArm: [false, false, true]` puts `side_l.2` and the tip over the arm).
- **Contract with the other layers.** Motion entries refer to hair parts by id: a numeric
  channel `hair.<part>.x` / `hair.<part>.y` (px, if the catalog allows it) is added to that
  part's targets, more toward the tips; `hair.sway` stays the idle wind. The hair layer reads
  the character layer's uniforms and writes only its own mesh, placed at the character layer's
  position: it cannot move the room, the camera or the props (`tests/stage_hair_test.cjs`
  drives it for 600 frames and checks that nothing else changes).
- **Views.** `hair_layer.js` `HAIR_VIEWS` maps a view id to a part set
  (`front` → `hair.json`, `back` → `hair_back.json`); `HairLayer.setView(id)` swaps the set at
  run time (`main.js` follows the character's view; `preload('back')` at start, so the first
  turn shows no frame without the locks). The back set is cut by `tools/char_back.py`
  (`export_hair_set`, same schema): 5 chains (`locks_l`, `locks_cl`, `locks_c`, `locks_cr`,
  `locks_r`) × 4 segments = 20 segments, parts `locks` / `locks_side` in `HAIR_RIG` (a small
  swing, the tips lag). In the turn and the profile the hair is part of the character's sheet
  (no set for those views: the layer hides).
- `?hair=rigid` moves every part with the head (no follow-through), for before / after
  captures.

## Event → pose

| Event | State | Shown as |
|---|---|---|
| `turn.start` / `turn.end` | `thinking` | steam from the mug; eyes up |
| `tool.start` / `tool.end` (`kind: tool`) | `tools[]` | desk instrument lit, scope trace runs; narrowed eyes |
| `tool.start` (`kind: memory`) | `tools[]` | shelf lights and a scan band; eyes up |
| `tool.start` (`kind: web`) | `tools[]` | window glow pulses |
| `approval.request` / `approval.resolved` | `approval` | choice window; neon turns amber; open mouth |
| `breaker` | `breaker` | neon colour (NORMAL cyan, WARNING violet, CRITICAL red), shake at CRITICAL |
| `neural` | `neural.*` | emotion (below) |
| `turn.end` with `ok` while joyful | `lastOutcome` | sparkles |
| `tool.start` / `tool.end` with `kind: code \| test \| subagent \| file`, `write`, `layer`, `ok` | `tools[]`, `turnKinds`, `agents[]`, `lastTest`, `lastError` | gadgets (below) |
| `approval.resolved` with `decision` | `stamp` | seal on the approval slip |
| `job.start` / `job.end` | `jobs[]` | hourglass and bell |
| `memory.forget` / `memory.restore` with `count` | `archived` | archive box on the shelf |

Unknown event types and malformed values are ignored (`reduce` returns the same state).

## Emotion mapping (Neural State and breaker → expression, posture, lighting)

Rules are checked top to bottom; the first match wins. The real binding to Stira's SSE comes
in stage 2; these thresholds are display thresholds of Stage, not engine settings.

<!-- stage-emotions:begin -->
| Emotion | When | Eyes | Mouth | Head | Ears | Lighting |
|---|---|---|---|---|---|---|
| tense | breaker is CRITICAL | wide | flat | down 1 px | back 2 | dim 0.18, desaturate 0.35, red neon, shake, sweat drop |
| worried | breaker is WARNING | down | frown | down 1 px | back 1 | dim 0.28, desaturate 0.55, violet neon at half level |
| joy | satisfaction >= 0.72 | happy | grin | up 1 px | up 1 | slightly brighter, blush |
| curious | curiosity >= 0.70 | wide | open | up 1 px | up 1 | — |
| calm | otherwise | open | neutral | 0 | 0 | — |
<!-- stage-emotions:end -->

Modifiers: `vigilance >= 0.5` folds the ears back one more step; `attachment >= 0.7` and
`trust >= 0.6` adds the blush. Work overrides the eyes while the event is open (tool →
narrowed, memory / web / thinking → up) except when worried or tense; a pending approval
shows open eyes and an open mouth unless CRITICAL. `trust`, `attachment` and `stability`
otherwise act through the breaker and the modifiers above. Breathing tempo follows the
emotion (tense ×1.6, worried ×0.8, joy and curious ×1.1).

## Consistency (drift) check

The mapping is a pure function: the same state always gives the same pose. Three checks
keep it that way:

1. `node --test tests/stage_model_test.cjs` replays different event histories that end in
   the same state and asserts the same pose signature, asserts that every demo step maps to
   a distinct and repeatable pose, and that this table matches `EXPRESSIONS` in the code.
2. At runtime a `DriftMonitor` records, for each input key (breaker, open turn, tool kinds,
   approval, outcome, the six Neural State values), the signature of the derived pose. If one
   key ever produces a second signature, the raw view shows **DRIFT detected** with the two
   signatures. Idle motion (blink, breath) is not part of the signature.
3. Gestures picked by the agent cannot change state channels (steam, prop lights, neon,
   shake) and cannot override the face while worried or tense, so the agent cannot make
   itself look calmer or busier than its state.

To check by hand: open the raw view (`Tab`), press `1`–`9` and `0` a few times in any order,
and confirm that the check stays at "no drift" and that each state shows the same causes.

## Gadgets (actions bring props, stage 2)

Some actions bring a prop into the scene: a laptop when she codes, small drones when she
delegates, a binder or a sticky note at the shelf, a magnifier at the window, a seal on an
approval slip, a warning beacon, an hourglass, an archive box. The same rules hold:

- **A prop comes only from the category of a real event.** `gadget_model.deriveGadgets()`
  reads the state that events left (running tools by kind, the kinds used in the open turn,
  subagents, the last test result, the last failed tool, the approval decision, scheduler
  jobs, the archive count) and sets `gadget.*` channels with their cause, like every other
  channel. No event, no prop (asserted in `tests/stage_gadgets_test.cjs`). Adapters that only
  know a tool name can use `classifyTool(name)` (e.g. `editor` → code + write, `web_fetch` →
  web, `sessions_spawn` / `delegate_*` → subagent; unknown names stay a generic tool).
- **Data, not code.** Where a prop stands, which channel shows it, its depth, its light pool
  and its phone placement are entries of `GADGETS`; how it moves is `GADGET_ANIMS`, ordinary
  entries for the animation interpreter, all of scope `state` (they run while their derived
  channel is on, `trigger: { state, is }` narrows to one value). `validateGadget` refuses
  unknown programs, triggers or sprites, functions and out-of-range values, and freezes what it
  accepts. The drawing programs (`laptop`, `drones`, …) are a fixed, human-written list in
  `gadget_scene.js`; data picks one by name. Gestures — the only scope the agent may select —
  cannot drive any gadget channel.
- **Raw view and debug.** The raw view lists the new state (subagents with status, last test,
  failed tool, seal, jobs, archive count) and every `gadget.*` value with its cause; the debug
  panel adds the props on screen.
- **Same state, same look.** The gadget part of the state is in the drift key, and every demo
  state — the nine of the spike and the thirteen below — has its own pose signature.

<!-- stage-gadgets:begin -->
| Prop | Shown by | From events | Look and motion |
|---|---|---|---|
| `laptop` | `gadget.laptop` | `code` / `test` tool running, or used in the open turn | laptop on the text window's edge; code typed line by line and lit keys while a code tool runs; status bar amber / green / red; screen light on her coat |
| `stacklight` | `gadget.laptop` | `test` tool running / ended | signal lamp: amber blinking while tests run, green on pass, red on fail |
| `monitor` | `gadget.laptop` | as `laptop`, while her motion is `typing_back` / `watch_back` | the same laptop on the room's desk, screen toward her (and us), code running on it; she sits at it, to its left, its screen and keys beside her (desk shot) |
| `stacklight_back` | `gadget.laptop` | as `stacklight`, with the back view | the lamp next to it |
| `drones` | `gadget.drones` | `subagent` tool start / end | one fox-eared drone per subagent (six drawn, then "+N"); flies out from beside her and hovers while running; on done flies back beside her on its own side (`docks` [left, right], split at her centre `centerX`: never across her face or over her chest and hands), shows a check and dissolves; on failure stays, sinks, blinks red with "!" until the next turn |
| `beacon` | `gadget.beacon` | breaker `WARNING` / `CRITICAL` | beacon on the pipe: slow violet glow at WARNING, turning red beams at CRITICAL |
| `magnifier` | `gadget.magnifier` | `web` tool running (or failed) | magnifier with dithered glass scanning the window |
| `folder` | `gadget.folder` | `memory` / `file` read running (or failed) | binder slides out of the book row |
| `note` | `gadget.note` | `memory` / `file` write running (or failed) | sticky note pressed onto the shelf; a named `layer` holds the shelf light on its row (procedural top, semantic, episodic) |
| `stamp` | `gadget.stamp` | `approval.resolved` with `decision` | slip pinned on the wall; the stamper comes down once and leaves 可 (approve, vermilion) or 否 (deny, grey) for the rest of the turn |
| `hourglass` | `gadget.hourglass` | `job.start` … `job.end` | hourglass on the window sill, sand runs |
| `bell` | `gadget.hourglass` | `job.start` | desk bell rings once |
| `box` | `gadget.box` | `memory.forget` (and `memory.restore`) | archive box with a restore mark slides to the back of the shelf top; stays while anything is archived (forgetting is reversible) |
<!-- stage-gadgets:end -->

Live mode: the adapter's `category` (`code`, `subagent`) and `op: write` on `tool.start` pick
the laptop and the drones, a tool whose name looks like a test runner (`run_tests`, `pytest`,
`test_*`) lights the lamp, `tool.end` with status `error` / `timeout` counts as a failure, and
a decision made in Stage's choice window carries `approve` / `reject` for the seal. Scheduler
jobs (`job.*`) and forgetting (`memory.forget`) have no live source yet (see "Limits"), so
the hourglass, the bell and the archive box appear in the demo only.

A failed tool (`tool.end` with `ok: false`) makes the prop of that category flicker once
(`gadget.glitch`); the prop stays marked until a retry (`tool.start`) or the end of the turn.
A failed generic tool flickers the desk instrument.

### Motions that come with the props (contract)

Her own motions (typing pose, pointing at the drones, receiving a report, slumping, …) are
defined by the motion catalog, a separate part of stage 2. The props only **name** the motion
that belongs with them, by id: `deriveGadgets()` sets `gadget.motion` to the first matching
cue of `MOTION_CUES` (data in `gadget_catalog.js`, with its cause), and the motion catalog's
rules read it (`MOTION_RULES`, see "The character: parts and motions"; the coding cues map to
the back-view motions `typing_back` / `watch_back`). Ids: `typing`, `look_screen`,
`delegate_point`, `receive_report`, `ponder`, `head_tilt`, `sip`, `alert`, `relief`, `slump`.
An id that no entry uses plays nothing, so she keeps her current pose until the motion
exists (asserted in `tests/stage_gadgets_test.cjs`).

| Cue (first match wins) | Motion |
|---|---|
| beacon on (breaker WARNING / CRITICAL) | `alert` |
| a prop flickers (tool or test failed), a subagent failed, approval denied | `slump` |
| code tool running | `typing` |
| tests running; laptop out (rest of the turn) | `look_screen` |
| a subagent was sent / came back | `delegate_point` / `receive_report` |
| tests passed, approval given | `relief` |
| web tool running (magnifier) | `head_tilt` (the rules pick `peer` for the web, rule 7) |

Props are drawn like the room: palette of the lab and of the character, 1 px dark outline,
ordered dither for shading, glass and light, the room's colour grade (so they dim and
desaturate with her at WARNING / CRITICAL), integer positions with their own parallax depth,
and a dithered materialise / dissolve instead of a fade. Props in front of her (laptop, lamp,
drones) stand on the text window's top edge (or the bottom of the view in portrait). In a
view narrower than 400 columns (phone portrait shows about 330) an entry may use its
`narrow` placement or hide: the pinned slip, the hourglass, the bell and the archive box
move inward so that they stay on screen.

### Adding a gadget as data

```json
{ "id": "kettle", "show": "gadget.hourglass", "program": "bell", "sprite": "bell",
  "at": [470, 134], "depth": 0.42, "layer": "back",
  "description": "Another bell on the sill." }
```

A new look needs a sprite in `tools/build_gadgets.py` (drawn in code; no imported art) and,
for new behaviour, a new program reviewed like any other code. A new trigger needs a mapping
in `gadget_model.js` from a real event category. The agent can neither add entries nor run
code; packs of entries come with stage 3 (human-approved, hash-pinned).

## The character: parts and motions (stage 2)

### Layered parts (contract with the hair layer)

The character is composed per frame from **parts drawn back to front** (`js/char_rig.js`,
pure, whole pixels only). The decomposition is data in `assets/char/parts.json`, generated by
`tools/build_char.py` → `tools/char_parts.py` from the icon: `layers` (id, group, kind, note),
`groups` (parent, pivot), `parts` (the variants of each part per pose), `sheets` (the poses
with their anchors for eyes, brows, mouth, ears, chest, shoulder, neck).

| # | Layer (back → front) | Group | Kind |
|---|---|---|---|
| 1 | `hair_back` | head | slot — the hair layer's back hair; in the back view the static back hair of `char_back.py` (part id `hair_back`) |
| 2 | `body_underlay`, `head_underlay` | body / head | pixels of `base.png` that no part covers (what `build_hair.py` fills under the hair and restores from the icon: ear skin, earbud), kept as they are |
| 2 | `neck` | head | drawn, under the collar and the chin |
| 3 | `turtleneck` | body | icon, completed under the lapels, the mug, the hand and the hair |
| 4 | `coat` | body | icon, completed under the hair and the mug arm; sleeves, upper arms and front panels drawn below the icon's circle |
| 5 | `face` | head | icon, forehead completed under the fringe, no eyes / mouth; under the hair it takes `base.png`'s restored cheek / ear / earbud / collar pixels, but not on the forehead and around the eyes (`FOREHEAD_NO_RESTORE`: there `base.png` has build_hair's fill under the cut bangs); the bangs' strand edges around the left eye are hair lines, not skin lines (`LEFT_EYE_STRANDS`) |
| 5b | `cheek` | head | the face's pixels that show at rest below the eyes (cheeks, jaw, chin, the ear by the earbud), moved with the head per pose; `overHair` (in front of the side locks, see "The cheeks in a head pose" below) |
| 6 | `crown` | head | icon: the hair the hair layer leaves with the head (`build_hair.py`) |
| 7–8 | `ear_l`, `ear_r` | head | icon, rotated about their base (−18° … 18°) |
| 9–11 | `eyes`, `mouth`, `blush` | face | sprites at the pose's anchors; the eyes are `overHair` (in front of the bangs) |
| 12 | `hair_front` | head | slot — the hair layer (side locks, wisps, bangs) |
| 13 | `brows` | face | sprites, `overHair` (in front of the fringe, anime convention) |
| 14–15 | `forearm_r`, `hand_r` | arm_r | icon (cuff, coat sleeve to the elbow, hand and mug); an arm sprite replaces both when the mug arm moves |
| 16 | `mug_desk` | body | drawn: the mug set down on the desk edge (typing, back view) |
| 17 | `arm_l` | arm_l | sprites of the free hand (raise, point, wave, chin, pen) |
| 18 | `hair_tips` | head | slot |
| 19 | `fx` | head | sweat drop, shock mark |

Groups: `body` (pivot at the bottom centre) → `head` (pivot at the neck, 139/142), `face`,
`arm_r` (shoulder 70/170), `arm_l` (shoulder 208/170). The **hair layer** (`hair_layer.js`,
drawn right after the character) fills the slots. It is connected to the composited frame,
not to the spike shader's offsets: `char_bridge.js` gives it (1) a **mask** of every frame
(R = she is drawn here, G = an arm, the hand, the mug, and the layers marked `overHair` in `parts.json` (the eyes and the brows: the bangs cross the left eye in the icon; drawn under them, the left eye was hidden in most shapes and gazes; the `cheek`: the face she shows at rest, so a side lock never shaves it in a pose) — in front of the hair unless a segment
is `overArm`), used directly (`uDirect`), and (2) `hairInput()`: the head offset at the
crown, the breath, and `at(x, y)`, the displacement of any point of the head for the pose, so
each hair chain hangs from its own point of a tilted head; in the back view the hair layer
draws its back set (the locks below the nape) over the sheet's skull, in the turn and the
profile it hides and the sheet's `hair_back` is all the hair. The **head pose** that
`CharRig.compose()` returns for every frame:
`{ sheet, view: front | back | turn | profile, tilt (deg, + = the top of the head to the right),
pitch (px at the eyes, + = nod down), dx, dy, shoulders, breath (0 / 1), neck: [x, y] }`
(also shown in the debug panel's frame key).

Completion: every part is filled where the parts in front of it hide it, so a pose never opens
a hole (the turtleneck behind the mug, the coat behind the arm, the forehead behind the
fringe). At rest the composited character keeps the icon's pixels except the redrawn eyes,
brows and mouth and the drawn body below the circle (`tests/stage_motion_test.cjs`: more than
91 % of the icon's pixels unchanged — 93 % before art unify 4 repainted the turtleneck's knit
(a rib every third column, the phase moved with the body), longer folds on the coat with a
light edge, and softer gaps and lit tops on the mug hand's fingers; before / after and a part map in
`docs/research/stage-motion-2026-10-17/parts-before-after.png`). `?layers=1` tints the parts.

**The cheeks in a head pose** (#341, 2026-10-18; `tools/char_layers.py` `rest_face`,
`cheek_part`). The hair layer moves each chain by the displacement at its root's pivot
(`hair_layer.js`, `rootPivot`): the side locks hang from the temples, which move most in a head
pose (far from the neck), while the face under them is re-pixelized rotated about the neck. In a
tilt the lock beside the other cheek slid 3–9 px over it (the thinking pose `ponder` 5 px, the web
`tilt_r1` 4 px, the approval tilt `tilt_r2` 9 px; to the left `sip`, `tilt_l*`, `yaw_l` the right
cheek): the cheek and the corner of the jaw were cut off by the lock's straight edge. The `cheek`
layer keeps the face she shows at rest: the pixels of the face (and of `head_underlay`) that no
hair covers at rest, below the eyes and inside the face box, moved with the head
(`poseDisplacement`, whole pixels, and back from every pose pixel, so the rounding leaves no
hole), drawn again in front of the hair (mask G); not where the bangs lie in that pose (they hang
from the crown and cross the face in a tilt by design). The mouth, the blush and the eyes drawn
over it keep it in front (`CharRig` marks the cheek's pixels per frame: a lock went over the end
of the mouth in the approval tilt). At rest it covers nothing more, and every front frame is the
same picture as before (only the hair mask changes). `tests/stage_face_test.cjs` checks, with the
hair layer placed as the browser places it, that no side lock covers that face in any front pose
(main: 42–317 px per tilted pose), no hole, no lock pixel alone on the skin beyond the icon's
one strand dot by the earbud, and no new step in the jaw's outline (`STAGE_CHEEK_REPORT=1` prints
the counts per pose, `STAGE_EAR_REPORT=1` those of the drooped ears).

**The hair beside the jaw** (#447, 2026-10-19; `tools/char_layers.py` `jaw_hair`). The other
side of the same mismatch: on the side the head turns or tilts to (`type` / tests-run, `yaw_l`,
`tilt_l*`, `sip`, `slump` on the left; `tilt_r*`, `ponder`, `yaw_r`, `down*` on the right), the
lock's inner edge moved off the jaw, and the `face` part's own pixels under it (the face is
completed under the hair) showed between the cheek and the lock — the jaw read 1–7 px wider than
at rest, in a paler fill, and met the chin with a step; in `type` and `yaw_l` a row between the
bangs and the lock (rounded separately) showed skin as a streak out from the cheek. In each pose
the `face` part now takes, where it shows skin outside the moved rest face, the side locks' (and
the bangs' tips beside the face) rest pixels moved with the head as the cheek is: they lie under
the hair layer, so they show only where the live lock has moved off. The rest pose's parts are
unchanged (no `calm` frame changes; the front-frame fixtures were re-recorded for the poses).
`tests/stage_face_test.cjs` ("the jaw line") takes the outline of the face she shows per row,
each side, from 6 rows above the mouth (below the bangs on that side) to 12 below it, against the
rest outline moved with the head: the offset may change by at most 1 art px from row to row and
stay within 2 px (rows beside the crown's hair under the chin are not compared; main: steps up to
5 px, offsets up to 8 px; `STAGE_JAW_REPORT=1` prints them per pose). Left as before: in a tilt
to the right (`ponder`, `tilt_r*`) the bangs, which hang from the crown, lie over the left cheek
below the eye and end in a 3–4 px step where the cheek continues (hair over the face, above the
checked band).

**Poses.** A pose (`sheet` id: `tilt_l1/l2`, `tilt_r1/r2`, `yaw_l/r`, `down1/2`, `up1`,
`straight`, `lift`, `relax`, `slump`, `ponder`, `sip`, `type`, the side `profile`, the 3/4
back `turn1` / `turn`, and the back view `back`, `back_l`, `back_r`, `back_bob`,
`back_tilt`) moves the groups: the head about the
neck (tilt, nod, look up, turn), the body from the hips (shoulders up / down). Each part of a
moved group is **re-pixelized for the pose** from the warped icon (same ramps, dither, line
art and outline), so a tilted head is drawn again as pixel art, not rotated. Only variants
that differ from the rest pose are stored.

**Faces** (`tools/char_face.py`, drawn at 1×): eyes `open half down wide surprised sharp soft
closed happy wince` — `half` is a flat lid over a half iris, `down` (downcast) a lowered,
downward-curved lid with a thin crescent of iris and the lid crease above; open-type eyes
look in 9 directions (`c l r u d ul ur dl dr`); mouths `neutral smile grin flat frown open
small_o wavy pout talk exhale`; brows `neutral up worried angry soft focus`; blush, sweat
drop, shock mark. Life signs that encode nothing: blinks (single, double, slow), a glance
now and then while nothing happens, an ear flick, breathing (the upper body 1 px).

**Back view** (`tools/char_back.py`, v2; constructed — the icon has no back): the same
layers drawn from behind, with the icon's ramps, dither, line art and outline.

- **Hair in locks**: a smooth skull cap (lit from the upper left, a broken "angel ring"
  highlight, a few flow lines from the whorl), a dark under-mass between the locks, 16 locks
  in two layers of different width and length (tapered ribbons on splines over the skull and
  down the back, cylinder-shaded, cel-like tone bands, a dark line on the side where a lock
  lies on another, strand lines, split tips, tips turning out at the sides), the shade under
  the skull at the nape, a softer sheen over the shoulder blades, darker tips. Below the nape
  the locks are the hair layer's back set (above), so they lag and sway a little; the sheet
  keeps the skull, the under-mass and the coat under them (no hole when they swing; at rest
  sheet + set = the uncut drawing, `restRgbHash`).
- **Lab coat's back**: the collar's edge, shoulder and armhole seams, side-back seams,
  the centre seam, the vent under a half belt with two buttons, tension folds from the right
  shoulder blade toward the arm that reaches forward (lit ridge, shadow, a short line at the
  deepest part), soft folds on the left and at the waist, the shade the hair casts.
- **The typing arm** (`back_arm`, arm sprites `back_type1`–`back_type4`, `view: back`): the
  right arm reaches past the shoulder to the keys of the room laptop (gadget `monitor`, moved
  to `[412, 148]` / narrow `[398, 148]` so the keyboard is beside her shoulder in both
  layouts; `KEYS_AT` in `char_back.py`, checked against the gadget's key region by
  `tests/stage_back_view_test.cjs`). The typing frames move the wrist, the pressing finger and
  the elbow by a pixel; the arm joins the coat at the shoulder without a line and follows the
  right shoulder's lift (`armShift`).
- **The turn**: `base` → `yaw_r` → `profile` (side profile, `tools/char_profile.py`: the hair
  behind, the coat from the side with the turtleneck in the front opening, the near arm
  reaching forward, the neck and collar, the face in profile — forehead, nose, lips, chin, the
  eye — the bangs and the lock before the ear, the fox ears with the far one behind) →
  `turn1` (3/4 back, the cheek, lashes and nose past the swept-back hair) → `turn` → `back`,
  0.1 s each, and the same way back in the exit.
- **No steam while she is turned away** (the mug stands on the desk): `main.js` gives the
  scene `steam: 0` and a faster fade (`scene.steamFade`) while the view is not `front`.

### One look for every part (art unify)

The parts come from different builders (the icon re-pixelized per pose, the hair layer's cut
parts, hands and sleeves drawn in code, the back view drawn in code). They share the ramps, and
since the art unify pass (`tools/char_unify.py`, run by every builder on what it draws or cuts)
they also share the line work. The reference is the owner's character sheet: grey hair with
thin grey strokes (no black), pale hands with a thin brownish contour, a navy knit cuff under
the coat sleeve, a black earbud.

- **Hair lines are hair greys.** A strand line takes `hair2` on light hair and `hair1`
  elsewhere (it was a near-black, `LINE.hair`); an outline pixel that touches only hair (the
  hair's silhouette, a seam where a hair part's edge lands inside the hair in a tilted pose)
  takes `hair1`; since art unify 2 every outline pixel of the hair (next to the coat or the
  face too) is `hair1`. Black stays on the silhouette of the coat, the mug and the turtleneck. Under the cut hair the underlay is hair in shade (`hair1` /
  `hair2`), so a gap between the hair parts in a tilt does not show a dark clump by the ear.
- **No specks.** A pixel much darker than all eight neighbours takes their colour; the parts
  of every pose are composited at build time (with the mug arm, and with an arm sprite and the
  free coat) and such a pixel is fixed in the part that draws it. Stray groups of up to three
  hair pixels the cut left below the head, and short strand dashes (up to eight) on the coat
  below the shoulders, are coat (completed under them).
- **Hands and arms** (`tools/char_arms.py`, 06: typing, holding the mug, hand to the chin,
  pointing, writing): the drawn hands are lit like the icon's hand (mostly the two lightest
  skin steps), with a 1 px contour in the skin line (no black ring) and light gaps between the
  fingers; every free hand comes out of a navy knit cuff (ribs in two navy steps) under the
  coat sleeve; the sleeves get soft folds and a narrower dither band (a flat drawn sleeve
  dithered into a regular dot grid). Contours lying over her own body are soft (`coat1`,
  `navy0`), only those against the room are the outline. `raise` is the in-between of all the
  free-hand gestures, close to each, so the change to a gesture is a short step (`ponder`
  went from the hand beside the hair to the chin in one frame). `pen` (a hand writing with a
  pen) is drawn but no motion uses it yet. The mug hand stays the icon's own hand; its black
  ring where it lies on the body is the skin line now. Art unify 5 (01 / 06 checked again):
  the free gestures' elbows are inside the frame at her side (y 256–274; below the frame
  every raised forearm was a ~150 px pole, longer than her head) with the upper arm's sleeve
  drawn from her left shoulder (214, 186); the moved mug hands (`mug_up` … `mug_down`) carry
  only the hand, the mug and the icon's own cuff at the wrist — the turtleneck round the mug
  and the hair strand over the hand stayed in the cut and moved with it (a navy patch, a grey
  hand in `mug_low`), the strand's pixels in the hand now take the nearest skin; the mug arm's
  coat sleeve has a rounded opening (its flat end stuck out under the raised hand in
  `mug_sip`). Art fix (2026-10-18, the owner's circles at 1300 px): that opening was a disc
  on the sleeve's end whose edge read as a ball round the wrist — the tube's own end is
  rounded now (`sleeve(round_end=)`), without the coat's turned-back cuff line; the icon's
  own cuff stays only on the drawn knit cuff (beside it, its edge was a dark hook off the
  wrist; over it, its outline a black line across the knit), the knit reaches 2 px further
  under the hand (the raised hand stood off it), no pixel is left on its own.
- **The earbud** is one clean shape (the biggest blob of the dark pixels in the ear, filled,
  shaded with the mug ramp, a highlight on the upper left); its stray dark pixels on the skin
  are skin.
- **The free coat** (the mug arm lifted or typing): its edges inside the figure are a soft
  coat line, not the outline (the panel read as a cut-out box). Art fix (2026-10-18):
  - it keeps the rest pose's drawn body on the right of the picture (her left panel, sleeve
    and hem below the circle: every arm sprite cut them off, the room showed through);
  - the open front runs from the armpit (86, 190) to the waist (`OPEN_FRONT_FREE`; from
    (50, 168), outside the figure, it took her right shoulder and upper arm: the turtleneck
    stood at the outline);
  - the drawn panel is drawn from y 150 and used from y 190 (its top edge was a soft line
    straight across the coat);
  - the coat's small pieces left on the turtleneck under the mug arm (the icon's light
    strands over the sweater, completed as coat) go (`free_islands`: light blobs and a dark
    line under the lifted mug).
- **The coat's left at rest** (art fix): small groups of the navy material off the
  turtleneck (≤ 60 px: the icon's thin dark strands and fold shadows over her right sleeve,
  turned navy by the downscale) are coat, completed from the coat around (`NAVY_ISLAND`: dark
  blotches on the sleeve, 46 px the biggest); the coat is completed under the crown's strands
  on that sleeve too (they move with the head and showed the sweater through the hole).

**The lint** (`tests/stage_art_lint_test.cjs`, in CI) checks the assets and every frame the
motions reach (with the hair layer at rest over it): every pixel is a colour of the palette
the build declares (`parts.json` `palette`); no near-black strand line in the hair assets;
**no black inside the hair in any frame** (a black pixel of a hair layer with at least 5 hair
neighbours; since art unify 2 the hair's own outline is a hair grey everywhere, 0 in all
frames; round 1: 7 / 1512; main before it: 996 / about 500 000); isolated dark specks none at
rest, at most 3 per frame and 320 in all (measured 3 / 284; round 1 11 / 2150; main 14 435);
every drawn hand with at least 66 % of its skin in the two lightest steps (main 0.56–0.64,
now 0.69–0.74) and no black pixel on any hand's edge (main up to 141); a navy cuff on every
free hand. A last test draws a black line into the hair, a black ring on a hand and a speck on
the coat and checks that each is found. The per-pose fix at build time composites the hair
layer at rest for each pose (`char_parts.hair_overlay`) and fixes every speck a part draws;
what is left are strand pixels of the hair layer where its parts, moved by different whole
pixels in a pose, meet (its atlas is shared by every pose; near the cuts between its parts no
pixel is much darker than the hair around it, `build_hair.soften_cuts`). The budgets
keep it from growing. `STAGE_LINT_REPORT=1` prints the counts, `STAGE_ROOT=<checkout>` runs it
on another build.

Picture fixtures: `tests/fixtures/stage_front_frames.json` (every front frame, back-view work
must not change it) and `stage_face_outside.json` (outside the face box, face work must not
change it) pin *another* change's scope; an art change like this one re-records them
(`STAGE_FRONT_FIXTURE=write`, `STAGE_FACE_FIXTURE=write`) and says what changed.
`stage_face_sprites.json` pins every eye, brow and mouth sprite (#359's drawings): the art
pass does not touch them.

**The body rules** (`tests/stage_body_test.cjs`, in CI; art fix 2026-10-18), in every front
pose with every right-arm sprite and every reached front frame: no pixel of her body but the
mug arm goes when an arm sprite replaces it (main: 1505 per frame, the hem); no turtleneck
within 8 px of the figure's left edge (main: up to 278 per frame); no patch of the turtleneck
(≤ 60 px) inside the coat (main: 2038 in 819 frames); no piece of the coat (≤ 100 px) left on
the turtleneck where the mug arm was (main: 49); every mug-arm sprite one piece, the hand
meeting the knit cuff and the cuff the sleeve, the sleeve ending ≥ 9 px behind the cuff's hand
side (main: two or three pieces in three of them, 1.7–8.5 px in five of the six used); no
isolated dark speck on the coat, the turtleneck or a moved arm with the hair at rest over them
(main: 1). `stage_body_outside.json` pins the rows above 136 (the head) of every frame: a body
change must not change it. `STAGE_BODY_REPORT=1` prints the counts, `STAGE_ROOT=<checkout>`
runs the rules on another build.

### Asset contract: extras (ear variants, tail, earbud)

Drawn for the motion layer to wire (#341, night round A2); **wired** by night round B (see
"Expressions: ears and tail" below): the rig draws the ear variants and the tail
(`char_rig.js`, `CharRig(meta, atlas, extras)`, loaded by `char_bridge.js`), the desk shot
draws the back tail at the seated scale (`desk_seat.js deskTails()`). The earbud is in the
face part already; `earbud.front` is not drawn on top (no layer covers her ear). Built by `tools/char_extras.py` (from
`build_char.py`, after the parts), in their own files: the parts atlas is unchanged.

- **Files**: `assets/char/extras.png` (8-bit palette PNG, every colour from `parts.json`
  `palette`, alpha 0 / 255) and `assets/char/extras.json` (`version: 1`).
- **Entries**: `{ox, oy, w, h, ax, ay}` as in `parts.json` — `ax`, `ay`, `w`, `h` the rectangle
  in `extras.png`; `ox`, `oy` the sprite's top left counted from its anchor
  (`extras.json` `anchor`): a sheet anchor of the current sheet (`parts.json`
  `sheets.<sheet>.anchors`, the same point every pose sheet moves) or, for `null`, the sheet's
  top left. Draw at `round(anchor.x + ox), round(anchor.y + oy)`, whole pixels, no scaling.

| Group | Names | Anchor | Draw it | Meaning (reference) |
|---|---|---|---|---|
| `ears.l` / `ears.r` | `std` | `ear_l` / `ear_r` | in the ear layer, instead of the angle sprite (`std` = the parts' ear at 0°, pixel for pixel) | the rest ear |
| | `droop`, `droop.<sheet>` | same | instead of the angle sprite; do not add `earL` / `earR` (the angle is in the drawing). It carries the top of the head where the ears stood (the icon has no hair there). One drawing per front sheet where it differs (`droop.<sheet>`, else `droop`): the top of the head and the crown it meets move with the sheet's field (#341, 2026-10-18: with one drawing a tilt left a crack across the top of the head); where the ear meets the head the V between its straight root cut and the head is filled with the cap's hair, no corner of the root stands alone, and a line inside the hair is a soft hair line (`hair2`) | 困惑・焦り: the ears flattened to the side (05) |
| | `twitch1`, `twitch2` | same | instead of the angle sprite, as a quick sequence `std → twitch1 → twitch2 → std` (about 2 + 2 frames at 30 fps) | ピクッ: a flick at a peak (up and out, back past the rest) |
| | `red.<angle>` for every angle of `parts.json` `ears.<side>.angles` (−18 … 18, step 6) | same | instead of the angle sprite of that angle: the ear channel keeps working while she blushes | 照れ: the ears warm from the inside (05) — the inner ear pale skin and blush, the rim of fur grey (art unify 4; was the whole ear pink) |
| `tail.front` | `l2`, `l1`, `c`, `r1`, `r2` (sway, tip toward her … out), `up` (raised, curled), `down` (lowered) | `null` (sheet) | front-view sheets (`view: "front"`) only, **behind her**: before `body_underlay` (after `hair_back`); it does not breathe or follow the head. A sway is `l1 c r1 c` or wider at ~4–8 frames per step | 尻尾: grey-white, bushy, the tip lighter (06 / 03); seen past her hip on the picture's right |
| `tail.back` | `l1`, `c`, `r1` | `null` (sheet) | back-view sheets (`view: "back"`) only, **in front of the coat**, before the typing arm | the big back view (03): from under the coat's back, up over its lower part and out to her left, the tip curling up (art unify 4; was a small tail at the edge) |
| `earbud` | `front` | `eye_r` | front view, after the face and the hair layer if a layer would cover her ear (it is already in the face part) | the black earbud in her (human) ear (03 / 06): always black, whatever the fox ears do |

The desk shot (`desk.*`, `desk_seat.js`) has its own small tail drawn with the seated figure;
`tail.*` here is the tail at portrait size, an S beside her in the front sheets (art unify 4:
out from her hip, back in, the tip flicking out; about 110 px of it shows) and over the big
back view's coat.

Rules the lint keeps for them (`tests/stage_extras_test.cjs`, in CI): every name above exists
(`red.*` for every ear angle), every sprite lies inside the 278 px sheet at the rest anchors,
every pixel is a palette colour, no black inside the fur and no isolated dark specks, `std`
is the rest ear exactly, `droop` lies flat (wider than tall, further out) — and on the
composited frame in every front sheet (`tests/stage_face_test.cjs`) has no notch of room at its
root (a disk of radius 3 closes none), no spike and no dark seam across the root —, the red ears warm
from the inside (10–50 % skin / blush, at least 8 blush pixels, more than 45 % still grey fur),
the tail is grey-white fur (hair ramp, more than half
light), every tail pose is a different picture, the earbud is black (the mug ramp).

### Asset contract: face fx and the added eyes / mouths (sheet 05)

Drawn for the motion layer to wire (#341, art unify 3), after the owner's expression sheet
(05: 基本, 困惑, 焦り, ドヤ, 照れ, 怒り, 大笑い, ジト目, 企み, 感動); **wired** by night round
B (`FACE_RULES`, see "Expressions" below; the rig draws `fx.*` in their slots,
`CharRig(…, faceFx)`, frame field `xfx`). Round A 3 needed no JS change; the eyes and mouths below need none either (the rig already picks
`eyes.<side>["<shape>.<gaze>"]` and `mouths[<name>]` by name — a motion track or a state rule
naming them is enough), the effects need a small draw step.

**Eyes** (`parts.json` `eyes.l` / `eyes.r`, every gaze `c l r u d ul ur dl dr`; drawn in the
eye layer, in front of the bangs, as #359's). Since art unify 4 every shape — #359's ten and
the seven added ones — draws the iris alike, toward sheets 03 / 05: a little bigger (×1.16
wide, ×1.08 tall), a dark ring round it, light toward the bottom, the lid's shadow on its top
row, a 2 × 2 pupil a little above the middle, a two-pixel highlight and a second one low on
the other side (an eye less than 5 px open at the iris keeps a one-pixel pupil and no ring).
The shapes (lids) are #359's; `tests/fixtures/stage_face_sprites.json` was re-recorded for
the eyes only (brows and mouths unchanged). The seven added shapes:

| Shape | Meaning (05) | Drawing |
|---|---|---|
| `jito` | ジト目・呆れ | the upper lid low and straight over the iris, the lower lid up a little |
| `teary` | 感動・うるうる | open, a bigger iris, three highlights, a wet (light blue) lower lid with a glint |
| `scheme` | 企み | narrowed: the upper lid down, the lower lid well up; at `c` it looks aside |
| `shy` | 照れ (目を逸らす) | soft lids, at `c` the eyes turned away and down |
| `arc` | 笑顔 (open) | the lower lid pushed up in an arc: a crescent of iris (`happy` is the closed arc) |
| `puzzled` | 困惑・戸惑い | the inner corner raised (the lid slopes outward), the iris a little smaller |
| `panic` | 焦り・慌て | wide open, a tiny pupil in a white ring |

**Mouths** (`parts.json` `mouths`, the mouth layer): `small_open` (小開き: a little open),
`mutto` (むっと: lips pressed, the lower lip pushed up), `smirk` (ニヤリ: a closed smile,
her left corner higher), `hen` (への字: the corners down in a sharp bend), `gasp` (おどろき:
open tall); art unify 5 adds `laugh` (大笑い: wide open, the upper teeth and the tongue — with
`happy` or `arc` eyes) and `niko` (ニコニコ: a small closed smile). The old ones (`neutral smile
grin flat frown open small_o wavy pout talk exhale`) are unchanged.

**Face fx** — `assets/char/fx.png` (palette PNG, `parts.json` `palette` colours, alpha 0 /
255) and `assets/char/fx.json` (`version: 1`, `fx.<name>: {ox, oy, w, h, ax, ay, anchor,
slot}`). Draw at `round(anchors[anchor].x + ox), round(anchors[anchor].y + oy)` of the current
sheet, whole pixels; `slot` names the layer it belongs in: `blush` = the `blush` layer (on the
face, under the bangs; the sprites keep off the eyes themselves), `fx` = the `fx` layer (on
top). The three old effects in `parts.json` `fx` (`blush`, `sweat`, `shock`) stay as the rig
draws them today.

| Name | Anchor | Slot | Meaning (05) |
|---|---|---|---|
| `blush.soft.l`, `blush.soft.r` | `eye_l`, `eye_r` | blush | 照れ: a soft pink patch with three diagonal strokes on each cheek |
| `blush.strong.l`, `blush.strong.r` | `eye_l`, `eye_r` | blush | 照れ (strong) / 怒り: redder, wider, more strokes |
| `blush.nose` | `mouth` | blush | 照れ (strong): a faint band over the nose, with `blush.strong.*` |
| `sweat.drop` | `eye_r` | fx | 困惑: one drop at the temple |
| `sweat.fly` | `eye_l` | fx | 焦り: three small drops flying off the head |
| `gloom` | `eye_l` | blush | 焦り: blue vertical lines over the upper face, only on her skin, off the eyes |
| `anger` | `eye_r` | fx | 怒り: the vein mark (four bent red strokes round a cross) above her head on the right |
| `tear.l`, `tear.r` | `eye_l`, `eye_r` | blush | 感動: a tear on the lower lid, with `teary` eyes |
| `sparkle.1`, `sparkle.2`, `sparkle.3` | `eye_l` | fx | 感動・ドヤ: four-point stars round the head; three frames to twinkle (`1 2 3 2`, ~6 frames each at 30 fps) |

No icon-like symbols (no "!", "?", notes, token marks): every effect is a small drawn shape.
Suggested combinations (05): 困惑 = `puzzled` + `sweat.drop` (+ the ears' `droop`); 焦り =
`panic` + `gasp` + `gloom` + `sweat.fly`; 照れ = `shy` + `blush.soft.*` (strong: +
`blush.strong.*`, `blush.nose`, the ears' `red.*`); 怒り = `sharp` + brows `angry` + `mutto`
+ `anger` (+ `blush.strong.*`); 感動 = `teary` + `tear.*` + `sparkle.*`; ドヤ = `arc` +
`smirk` + `sparkle.*`; ジト目 = `jito` + `flat` or `hen`; 企み = `scheme` + `smirk`.

Tests: `tests/stage_fx_test.cjs` (in CI: the names, anchors and slots above, every sprite
inside the sheet at the rest anchors, palette colours only, the blush-slot sprites off the
eyes of the widest shapes, `gloom` only on the face's skin, the colours of each effect, three
different sparkle frames) and `tests/stage_face_test.cjs` (#359's sprites unchanged and only
the names above added; every new shape's two eyes alike — iris rows, iris size, highlights —
at every gaze, and a sideways gaze moves both irises that way; every eye shape × brow × mouth
composes on the rest sheet with the eyes and brows in front of the bangs, the new ones also
in a tilt, a turn and a slump).

### Asset contract: pose arms (arms crossed, thinking with the arm across)

Drawn for the motion layer to wire (#341, art unify 5); **wired** by night round B 7.
- The rig merges `poses.json` `arms` into a copy of its arms: `CharRig(…, poses)`, blitted
  from `poses.png`.
- `char_rig.POSE_ARM_IDS` makes them valid motion ids.
- Without `poses.json` the ids fall back to the mug arm / no free arm.

Built by
`tools/char_poses.py` (from `build_char.py`) into their own files, `assets/char/poses.png`
(palette PNG, `parts.json` `palette` colours) and `assets/char/poses.json` (`version: 1`,
`arms.r.<name>` / `arms.l.<name>`: `{ox, oy, w, h, ax, ay}`, the parts atlas' arm entries'
semantics: sheet pixels of the base pose, the right arm moved with the `chest` anchor, the left
with `shoulder_l`). The parts atlas is unchanged.

| Name | `arms.r.<name>` (drawn instead of the mug arm: the `forearm_r` / `hand_r` slot, free coat) | `arms.l.<name>` (the `arm_l` slot) | Meaning |
|---|---|---|---|
| `cross` | her right arm: the upper arm's sleeve from the shoulder, the forearm across the chest under the other, the hand by the left elbow | her left forearm over it, the hand gripping the right upper arm | 05 ドヤ顔: arms crossed (with `arc` / `smirk`, brows `soft`, `sparkle.*`) |
| `ponder_cross` | her right forearm laid across the waist (the upper arm's sleeve from the shoulder) | the left elbow resting on it, the hand at the chin (the index finger along the jaw, as `chin`) | 01 考え事 (a fuller `ponder`: with the `ponder` sheet) |

Wiring: a pose uses **both** sprites of its name at once (`armR` and `armL` = the name, once B
merges `poses.json` `arms` into the rig's arms). With either, no mug is in her hand: the rig
then draws the desk mug (`props.mug_desk`) — hide it, or let the pose start after the mug is
put down. Sizes: the forearms 21 → 31 px wide (wrist → elbow; the chin arm 19 → 29), as the
other free arms (`char_arms.build`).
Tests: `tests/stage_poses_test.cjs` (in CI: both arms of every pose, inside the sheet, palette
only, the hand rules of the art lint, the forearms really cross and overlap, the thinking
hand's finger at the chin with its elbow down on the arm across).

### Motions (data)

A motion (`js/motion_catalog.js`, `MOTIONS`) is keyed tracks: `sheet`, `eyes`, `gaze`,
`brows`, `mouth`, `armR`, `armL`, `fx` (part ids — the validator refuses ids that are not in
`parts.json`), `desk` / `tail` (frames of the desk shot, `DESK_IDS` = `desk.json`; `none` =
she is at the front), `earL` / `earR` (degrees, + = folds back), `blush`, `sweat`. One-shots play
once (`then` may continue into another motion); looping motions have an `intro` (played once)
and an optional `exit` (played when the motion ends, e.g. she turns back and takes the mug);
motions of one `group` pass to each other without exit and intro (from `typing_back` to
`watch_back` she stays turned round). Tracks hold whole frames (pixel-art timing, 2–10
frames per pose at 60 fps); the player (`js/motion.js`) interprets data only.

**Coding is the laptop in front of her (2026-10-18, the commander: 「PC が出てくるだけ」).**
- A code tool brings the laptop prop out in front of her. It fades in, its keys light and its
  screen scrolls.
- She stays facing us with the mug in hand:
  - `typing` (rule 5): eyes on the screen, the head and shoulders bob a little with the
    typing;
  - `look_screen` (rule 6): watching its screen while code or tests are on it in the turn.

**Free hands are paused (2026-10-18, the commander: 「マグが無い方の手は無理に描写しない」).**
- The only hand she shows is the one that holds the mug: `mug`, `mug_up` / `mug_lift` /
  `mug_sip` / `mug_low` / `mug_down`, so lifting, drinking and lowering stay.
- No motion uses a free arm frame (`motion_catalog.PAUSED_ARMS`):
  - right: `type1`, `type2`, `cross`, `ponder_cross`;
  - left: `point`, `wave1`, `wave2`, `raise`, `chin`, `chin_in`, `pen`, `cross`,
    `ponder_cross`.
- The other hand beside the mug is not drawn either.
- The assets stay: `parts.json` arms, `poses.png` / `.json` and their tests.
- The expression goes on without the hands:
  - **thinking:** the head's tilt, eyes up and narrowed;
  - **sending a subagent:** the head and eyes follow the drone (the prop shows where it goes);
  - **smug:** chest out and chin up, the face and the sparkle;
  - **greeting (`wave`):** a tilt of the head, happy eyes, the ears.
- Measured by `tests/stage_hands_test.cjs`:
  - every frame of every motion (the paused desk shot aside) uses a mug arm and no free left
    arm;
  - drawn, no free arm pixel and no hand in front of her face except the mug at her mouth.

**Paused: the desk shot (休止中, モーションから外した).**
- Walking to the room desk, the chair, typing with her back to us and walking back
  (`typing_back` / `watch_back`, #371 #373 #383 #386) are no longer chosen by any rule
  (`motion_catalog.PAUSED_MOTIONS`).
- Kept for a later return: `desk_seat.js`, `assets/desk/`, `char_desk.py`, the
  `monitor` / `stacklight_back` props for the back view, and the desk tail.
- Their tests still run (`tests/stage_desk_test.cjs`).
- To bring it back, point rules 5 / 6 at them again.

<!-- stage-motions:begin -->
| Motion | Chosen by | Length | What she does |
|---|---|---|---|
| `typing_back` | **paused** (no rule; kept for a later return) | loop 3.5 s (intro 1.42 s), exit 1.3 s | Coding at the room desk (paused 2026-10-18): turns round, walks to the room desk, sits with her back to us and types; walks back. |
| `watch_back` | **paused** (no rule; kept) | loop 5.4 s (intro 1.42 s), exit 1.3 s | At the room desk, seated with her back to us, reading (paused 2026-10-18). |
| `typing` | rule 5; props | loop 1.5 s (intro 0.30 s), exit 0.30 s | Coding: the laptop comes out in front of her (the prop fades in, its keys light and its screen scrolls); she stays facing us with the mug lowered in her hand, eyes on its screen, and the head and shoulders bob a little with the typing — no hand on the keys (free hands are paused). |
| `look_screen` | rule 6; props | loop 3.2 s (intro 0.20 s), exit 0.20 s | Code was written in this turn / tests run: watches the laptop screen in front of her, head and eyes to the lower left, the mug in hand. |
| `look_desk` | rule 10 | loop 3.0 s (intro 0.20 s) | Watches the instrument on the desk (a tool is running): head and eyes to the right. |
| `look_shelf` | rule 9 | loop 5.6 s (intro 0.20 s), exit 0.16 s | Memory or files are read: looks up at the shelf on the left, then reads what came down (reference 08: 資料探索) — head lowered, eyes down the page line by line, a small nod per line; then up to the shelf again. |
| `delegate_point` | props (`$gadget`) | 2.3 s | Sends a subagent: turns her head and eyes up and out after the drone, says a word, looks back (no pointing hand: free hands are paused; the drone shows where it goes). |
| `receive_report` | props (`$gadget`) | 2.0 s | A subagent reports back: two small nods with soft eyes and a smile, ears up. |
| `ponder` | rule 11 | loop 4.3 s (intro 0.50 s), exit 0.30 s | Thinks (reference 01 考え事): the mug stays in hand; the head turns and tilts through two in-betweens, eyes up to the side, now and then narrowed, the mouth pursed. Back the same way (no hand at the chin: free hands are paused). |
| `await_mug` | rule 3 | loop 6.0 s (intro 2.00 s), exit 0.30 s | Waiting for the commander's answer (an approval is pending): asks with a tilt of the head, then rests with the mug lowered in her hands (reference 08: 解析待ち・休息) — soft eyes on us, a glance at the mug, now and then a small sip. |
| `peer` | rule 7 | loop 3.6 s (intro 0.30 s), exit 0.20 s | The web is searched or fetched (the magnifier at the window): turns toward it and leans in to read, eyes narrowed, the mouth a little open; now and then she squints closer and pulls back (reference 07 / 08). |
| `head_tilt` | props (`$gadget`); agent (selectable) | loop 2.6 s (intro 0.30 s), exit 0.20 s | Tilts the head (asking, or curious about something outside); ears uneven. |
| `sip` | rule 15; agent (selectable) | 2.7 s | Brings the mug to the mouth, tilts it and drinks with closed eyes, lowers it, breathes out. |
| `alert` | rule 2; after `surprised` | loop 3.6 s (intro 0.60 s), exit 0.30 s | Straightens up, eyes sharp, ears up; scans left and right while the breaker is up. |
| `relief` | rule 4; props (`$gadget`) | 2.6 s | Breathes in, then the shoulders drop and relax; eyes close and open soft; a small smile. |
| `slump` | rule 16; props (`$gadget`) | 3.4 s | Disappointed: the head drops, shoulders fall, eyes downcast, the ears flattened (`droop`) and the tail low; recovers slowly. |
| `surprised` | rule 1 | 1.4 s → alert | Startled: jumps up a little, eyes wide with small pupils, the ears flick (`twitch1` → `twitch2`) and stand straight up, the tail up, a shock mark. |
| `smug` | rule 12 | 2.6 s, then `sip` | Tests passed and the turn is done: chest out and chin up (`straight`, `up1`) with closed eyes and a small smile, then a side glance under half-closed lids, ears perked, the tail up, the mug in hand (ドヤ顔, reference 05; the crossed arms are paused). |
| `sigh` | rule 13 | 2.4 s, then `sip` | The turn is done but a test failed in it: eyes close, a long breath out with the head down, the ears flattened and the tail low, then she looks up again (お疲れ・ため息). |
| `scheme` | agent (selectable) | 1.8 s | A little plan (企み): narrowed eyes looking aside and a one-sided smile, the head tilted, then back. |
| `stretch` | rule 14; agent (selectable) | 2.6 s | Satisfied: shoulders up in a stretch with closed eyes, then a happy look. |
| `wave` | agent (selectable) | 1.9 s | Greets: a tilt of the head with happy eyes and a grin, the ears flick (the hand wave is paused: free hands are not drawn). |
| `nod` | agent (selectable) | 0.9 s | A clear nod (yes). |
| `shake_head` | agent (selectable) | 1.2 s | Shakes the head (no), eyes closed. |
<!-- stage-motions:end -->

**Which motion: rules (data, first match wins).** `motion_model.selectMotion()` evaluates
`MOTION_RULES` on the state that events left; the result is the `motion` channel of the
derived pose, with its cause (`motion = typing_back ← a code tool is running: gadget.typing →
typing (code running: editor (#3 tool.start))`), shown in the raw view (`Tab`) and the debug
panel (`D`, with the playing motion, its phase and the frame). The same state always selects
the same motion: `motion` is part of the pose signature, and the drift key includes the one
event fact the rules need beyond the state (the breaker just rose / went back to NORMAL,
cleared by the next work event). A one-shot restarts when its cause changes (a new event); a
looping motion only when the id changes. Events that arrive together select only the last
motion (applied once per frame).

<!-- stage-motion-rules:begin -->
| # | Motion | When (all must hold) | Why |
|---|---|---|---|
| 1 | `surprised` | cue breaker.rise, breaker CRITICAL | the breaker just tripped to CRITICAL |
| 2 | `alert` | breaker CRITICAL / WARNING | breaker above NORMAL |
| 3 | `await_mug` | approval true | asking for approval, then waiting for the answer |
| 4 | `relief` | cue breaker.calm | the breaker went back to NORMAL |
| 5 | `typing` | gadget typing | a code tool is running: the laptop out in front of her, typing |
| 6 | `look_screen` | gadget look_screen | code / tests on the laptop in this turn: watching its screen |
| 7 | `peer` | kinds web | the web is searched or fetched |
| 8 | `$gadget` | gadget * | the props ask for it |
| 9 | `look_shelf` | kinds memory / file | reading the shelf (memory / files) |
| 10 | `look_desk` | kinds tool | a desk tool is running |
| 11 | `ponder` | thinking true | a turn is open |
| 12 | `smug` | outcome ok, test pass | the turn ended well and its tests passed (achievement) |
| 13 | `sigh` | outcome ok, test fail | the turn ended, but a test failed in it (loss) |
| 14 | `stretch` | outcome ok, emotion joy | the turn ended well (satisfied) |
| 15 | `sip` | outcome ok | the turn ended |
| 16 | `slump` | outcome error | the turn ended with an error |
<!-- stage-motion-rules:end -->

Coding: when a code tool starts (`gadget.typing`), she turns round (front → head turned →
3/4 back → back), **walks to the room desk and sits there with her back to us** (the desk
shot below); the laptop moves from the text window's edge to the room's desk (gadget entries
`monitor` / `stacklight_back`, `when: { motion: [typing_back, watch_back] }`). While code or
tests stay in the turn she keeps watching it (`watch_back`, still seated); when the turn ends
she turns at the chair and walks back toward us, the mug in her hand.

#### Desk shot (#341, night round B)

The commander found the back view at the front too close: her back hair filled the picture.
Now she works at the room desk, as in his reference sheet ("背中を見せてプログラミング":
chair, desk, the tail out from under the coat over the seat):

- **Frames** (`assets/char/desk.png` + `desk.json`, built by `tools/char_desk.py` from the
  back, front and profile views the rig draws, `tools/char_desk_compose.mjs`; new parts only,
  the existing parts are not changed):
  - `walk1`–`walk7`, then `stand`: walking away in 8 even steps.
    - The scale shrinks by the same factor each step (0.90 → 0.42 of her size at the front,
      9–11 % per step).
    - She moves evenly along the floor from her place to the chair.
    - The walk cycle is contact / passing (a pixel higher) / contact / passing.
    - The coat skirt, the legs and the tail are added below the bust.
  - `sit`, `sit_l`, `sit_r`, `sit_bob`, `sit_tilt`: seated, 0.40, from the back sheets.
    - The shoulders and head move with the keys.
    - **Her arms** (reference 01) are drawn on the seated frames: from each shoulder the
      coat's sleeve runs down and out to the elbow beside her body. The forearm goes forward
      and in, and at the keyboard's edge the navy knit cuff and the pale hand show (round A's
      hand palette: light skin, the skin line as contour, no black next to the skin).
    - In `sit_l` / `sit_r` / `sit_bob` a hand is a pixel lower: it presses a key.
  - `turn`: side view at the chair, to come back.
  - `back7`–`back1`: walking back toward us, front view with the mug, the same steps.
  - `chair`, the tails `tail_c` / `tail_l` / `tail_r` / `tail_up` / `tail_down` /
    `tail_puff`, and her `mug` on the desk.
  - `monitor`: a display on a stand behind the laptop, wider than her head, so its code shows
    above and beside her head while she sits in front of it (reference 01).
  - She sits **in front of the laptop** (centre x 452, the laptop's centre; it stays at
    `[412, 148]`).

  All of it uses whole pixels and only `parts.json`'s palette.
  - The figure is downscaled from the rig's drawing.
  - The chair, pants, mug and display take the navy / mug ramps.
  - Lone dark pixels left by the downscale are removed (the art lint's speck rule).
- **Motion data**: the `desk` and `tail` tracks of `typing_back` / `watch_back`.
  - Intro: the turn (0.64 s), then `walk1` … `stand` 0.08 s each (5 frames at 60 fps), then
    `sit` at 1.42 s.
  - Loop: the seated frames with the keys; the tail swings now and then.
  - Exit: `stand` → `turn` → `back7` … `back1` (0.08 s each) → the front layer again.
- **Drawn by** `js/desk_seat.js`, in the room at the desk's depth (0.8, with the laptop):
  - Order: after the laptop, before the front props. The chair is behind her while she walks
    on our side of it and in front of her when she is at it.
  - The room's grading applies, plus a rim from the screen's light.
  - The phone layout shifts with the laptop (`narrowDx`).
- **While she is there** (`frame.desk ≠ none`):
  - the character layer at the front and its hair are not drawn;
  - no steam;
  - the room pans 40 px left over 0.6 s (`DESK_PAN`, as the board does; the board wins when
    both), so the desk comes toward the middle;
  - on a wide screen the camera also looks lower: the room moves up (`lift`,
    `deskLiftFor()`, at most 36 px). The goal is that the chair's seat (image row 258) is
    above the text window, so her shoulders, arms, the chair's back and the tail's top show
    on the PC. On a phone the text window is under the view and nothing changes.
- **Size**: seated, her figure is 111 px tall (0.40 of the 278 px front sheet).

#### Expressions: ears and tail (#341, night round B)

From the state and the derived pose, through tables in `motion_catalog.js`. The same state and
time always give the same picture. A motion's own `ears` / `tail` track wins. The emotion is
the derived pose's (`EMOTION_RULES`, not changed); each row names the Stira category it shows.
Neural State values are only read.

**The face** (`FACE_RULES`, first match; `motion_model.FaceHolder`, frame fields `eyes` /
`gaze` / `mouth` / `brows` / `xfx` / `ears`). It goes over the motion's face, except closed
eyes (a sip, a stretch, a blink). Its effects replace the old blush / sweat marks.
- **Holding** — each row shows for `for` s after its facts first hold (a reaction to the event,
  not for the rest of the turn), then the next matching row.
- **No flicker** — a new face waits until the current one has shown `FACE_MIN_HOLD` = 1.5 s,
  unless it is `urgent` (the breaker rows).
- **Sparkle** — twinkles `1 2 3 2`, 0.2 s each.

| Face | When | Eyes · mouth (· brows) | Effects | Ears | For | Category |
|---|---|---|---|---|---|---|
| `panic` (焦り) | breaker CRITICAL, just tripped (cue breaker.rise) | `panic` · `gasp` | `gloom`, `sweat.fly` | droop | while | threat (urgent) |
| `alarm` (警戒・怒り) | CRITICAL and vigilance ≥ 0.6 (a strong threat) | `sharp` · `mutto` · `angry` | `anger` | droop | while | threat (urgent) |
| `wary` (警戒) | CRITICAL | `sharp` · `mutto` | — | droop | while | threat (urgent) |
| `puzzled` (困惑) | breaker WARNING | `puzzled` | `sweat.drop` | droop | while | uncertainty (urgent) |
| `stumble` (戸惑い) | a tool failed in the open turn | `puzzled` · `wavy` | `sweat.drop` | droop | 3 s | uncertainty |
| `teary` | the turn ended with an error | `teary` · `hen` | `tear` | droop | 5 s | loss |
| `tired` (お疲れ・ジト目) | the turn ended ok but its last test failed | `jito` · `hen` | — | — | 5 s | loss |
| `triumph` (大笑い) | the turn ended ok, its last test passed, and satisfaction ≥ 0.85 (a strong joy) | `arc` · `laugh` | `sparkle` | — | 4 s | achievement |
| `smug` (ドヤ) | the turn ended ok and its last test passed | `arc` · `smirk` | `sparkle` | — | 4 s | achievement |
| `shy_strong` (照れ) | an approval approved in the turn, and fond | `shy` (gaze c) · `small_open` | `blush.strong`, `blush.nose` | red | 4 s | connection |
| `shy` (照れ) | an approval approved in the turn | `shy` (gaze c) | `blush.soft` | red | 4 s | connection |
| `content` (ニコニコ) | the turn ended ok, no test in it | — · `niko` | — | — | 3 s | achievement |
| `fond` (照れ, quiet) | attachment ≥ 0.7 and trust ≥ 0.6 | — | `blush.soft` | red | while | connection |
| `curious` | emotion curious | — · `small_open` | — | — | while | uncertainty |

Where the face assets are used:
- **Motion tracks:** `surprised` flicks the ears (`twitch1` → `twitch2`). `slump` and `sigh`
  flatten them (`droop`).
- **Agent request only:** `scheme` (企み) is a selectable motion with the `scheme` eyes. It is
  not derived from a state.
- **Pose arms (round A 5):** `smug` crosses the arms (`cross`, 05 ドヤ顔) and `ponder` takes
  `ponder_cross` (01 考え事).
  - Both arms of a pose switch together.
  - The mug goes down first (`mug_down`).
  - While a pose arm is on, the rig draws no mug and the steam stops.
    - The contract's other choice, the desk mug, would float over her coat in this framing:
      the desk edge is in front of her chest.
  - Each motion takes the mug again at its end.
- **Unexpected success:** no fact says a success was unexpected (the state keeps only the last
  test of the turn), so `laugh` comes from the strong joy only.

**Tail** (`TAIL_MOOD` by the derived emotion, on the clock; frame field `tail`):

| Emotion | Tail (extras `tail.front`) | Hold | Category |
|---|---|---|---|
| calm | `c c c r1 c l1`: a slow sway now and then | 0.26 s | — |
| curious | `l1 c r1 c`: swings side to side | 0.2 s | uncertainty (curiosity) |
| joy | `l1 c r1 r2 r1 c`: a wide, quick wag | 0.14 s | joy |
| worried / tense | `down`: lowered, still | — | uncertainty / threat |

Motion tracks:
- `smug` raises it (`up`, achievement).
- `surprised` raises it.
- `slump` and `sigh` lower it (loss).

**Face effects and the room's light.**
- The face effects keep their own colours under the room's grading: CRITICAL's red,
  WARNING's grey.
  - The rig marks their pixels (`CharRig.fxMask`, uploaded as `tRig2`'s alpha).
  - The character shader leaves those pixels out of `grade()` and the rim.
- The `fx`-slot effects (`sweat.*`, `anger`, `sparkle.*`) are also marked in the hair mask's
  G, so the hair layer is not drawn over them ("on top", the contract). Before this,
  `sweat.drop` was entirely under her hair. The `blush`-slot ones stay under the bangs.

**Why the face and ears come first, not the tail (the PC layout).** On a wide screen the text
window covers her from the chest down, and the front tail hangs past her hip, so it is mostly
under the window there.
- This is accepted: the expression is carried by the face, the ears and the face effects,
  which are always above the window.
- The tail adds to the moment where it can be seen: the phone (the text window is under the
  view), the desk shot (it hangs past the chair), the back view and the turn.
- Moving the tail up to the waist would draw it where it does not grow. Lifting the whole
  character would push the ears and the face into the room's top edge and change every
  layout.

Where the tail is drawn:
- **Front view:** behind her, past her hip on the picture's right.
- **Back sheets:** in front of the coat, before the typing arm (`TAIL_BACK` maps the front
  names to `l1` / `c` / `r1`).
- **Desk shot:** round A's back tail downscaled to the seated scale with whole pixels
  (`deskTails()`), beside the chair.
  - It snaps to the fur ramp only (`hair5` … `hair0`). Averaging grey with dark gave skin
    tones before.
  - Its light pixels on the silhouette take `hair2` (`edgeSprite()`): at 0.4 the fur's soft
    edge read as a blur.
  - The desk atlas' own `tail_*` frames are no longer used.

**Demo steps:**
- `e` `tests-done`: tests passed. Face `smug`, motion `smug` (chest out).
- `o` `tests-elated`: tests passed with satisfaction 0.9. Face `triumph` (laugh), motion `smug`.
- `i` `tests-tired`: a test failed but the turn ended. Face `tired`, motion `sigh`.
- `p` `fond`: blush and red ears.
- `8`: WARNING (`puzzled`).
- `9`: CRITICAL (`panic`, then `wary` / `alarm`).
- `j`: a failed turn (`teary`).
- the gadget steps `approved` / `tool-fail`: `shy` / `stumble`.

**The agent may request** a motion by id with `{ "type": "anim", "id": "wave" }` — only the
`selectable` ones (`head_tilt sip stretch wave nod shake_head`). A request is refused (and
listed in the debug panel) when the id is unknown or not selectable (it would fake a state:
typing, alerts, relief …), while a motion selected by the state is playing, while worried or
tense, or within 2 s of the last one. Agent motions never change a state channel and never
override the face while worried or tense.

**The room does not move with her.** Motion tracks are character parts only (the validator
has no room or camera channel); the composited character is uploaded into the character
layer's own texture (`js/char_bridge.js`: the spike shader's weight maps are set to zero and
its overlays to empty, it only adds the neon rim light); `roomFrame()` reads no character
channel. Checked by `tests/stage_motion_test.cjs` (every motion, the room frame identical,
the bridge writes nothing of the scene but the character's own uniforms and mug anchor) and in
the browser (pixels outside the character unchanged while motions play, see the report).

**Maintenance (open):** `char_bridge.js` drives the spike shader of `scene.js` from outside
(its weight maps set to zero, its texture replaced by the composited character). Folding the
compositor's texture and uniforms into `scene.js` itself is a follow-up for the owner of
`scene.js`; until then a change to the character uniforms in `scene.js` must keep the bridge's
overrides working (`tests/stage_motion_test.cjs` checks what the bridge writes).

**Adding a motion = one entry**, e.g.:

```json
{ "id": "shrug", "duration": 1.2, "selectable": true,
  "tracks": { "sheet": [[0, "lift"], [0.7, "base"]], "eyes": [[0, "half"], [0.8, "open"]],
              "mouth": [[0, "flat"]], "earL": [[0, 8], [0.8, 0]], "earR": [[0, 8], [0.8, 0]] },
  "description": "Lifts the shoulders: who knows." }
```

and, if the state should choose it, one rule in `MOTION_RULES` naming a real event category.
A new *drawing* (pose, hand, face) is a change to `tools/char_*.py` and a rebuild, reviewed
like code. The agent cannot add entries or run code (stage 3: human-approved packs).

## Animation catalog (extension = data, never agent code)

Animations are JSON-like entries interpreted by `anim.js`. An entry names the channels it
moves, one whitelisted curve per channel (`const`, `sine`, `pulse`, `keys` with `linear`,
`smooth` or `step` easing), a duration, and what starts it. Scopes:

- `idle` — always on (`trigger: { always: true }`) or at random intervals
  (`trigger: { every: [min, max] }`); only body/hair/ear/camera/eye channels. An always-on
  entry may take its tempo from a derived number (`rateFrom: "breathRate"`, whitelisted).
- `state` — runs only while a derived state is on (`trigger: { state: "steam" }`); the only
  scope that may drive steam, prop lights, neon flicker and shake.
- `gesture` — the only scope the agent may select, by id (head, ears, arm, hair, eyes,
  mouth). Unknown ids, non-gesture ids and repeated requests (2 s cooldown, at most 3 at a
  time) are ignored and listed in the debug panel.

`validateEntry` drops any entry with a function, an unknown channel or curve, a channel
outside its scope, a bad id, or enum values outside the sprite list; numbers are clamped to
the channel range; accepted entries are frozen. Nothing is ever evaluated as code.

**Safety rule:** Stage is extended with data (entries like the one below) or with a pack of
entries that a human approved and pinned by hash (stage 3, same propose → approve flow as
procedural memory, #144). Agent-supplied JavaScript is never executed: it would be an XSS
and privilege-escalation path into the dashboard.

### Adding an animation as data

Append one entry to `CATALOG` in `js/catalog.js` (or, later, to an approved pack):

```json
{
  "id": "gesture.head-tilt",
  "scope": "gesture",
  "description": "Tilt the head one pixel to the side, as if puzzled.",
  "duration": 1.4,
  "channels": {
    "head.x": { "fn": "keys", "keys": [[0, 0], [0.25, 1], [1.1, 1], [1.4, 0]], "ease": "smooth" },
    "ear.l": { "fn": "keys", "keys": [[0, 0], [0.2, -1], [1.2, -1], [1.4, 0]], "ease": "smooth" }
  }
}
```

The agent then selects it with an event `{ "type": "anim", "id": "gesture.head-tilt" }`.
No other code changes.

## Assets

- The character comes from the project owner's own AI-generated icon (rights clear); see
  `dashboard/stage/assets/ASSETS.md`. `tools/subject_mask.swift` (macOS Vision) makes the
  subject mask once; `tools/build_char.py` turns the icon into the pixel-art sheet
  (bilateral smoothing, area downscale ×4, hand-picked hue-shifted ramps for hair, skin,
  coat, turtleneck, mug and eyes — 35 colours — 4×4 ordered dither between ramp steps,
  line art from the source ink, 1 px outline) and builds the weight maps and the expression
  sprites; `tools/build_hair.py` cuts the hair into its parts (see "Hair rig").
- The motion parts are made by `tools/char_parts.py` (run by `build_char.py`; helpers
  `char_layers.py` parts and completion, `char_body.py` the body below the circle,
  `char_face.py` eyes / mouths / brows, `char_arms.py` arms, `char_back.py` the back view,
  `char_unify.py` one look for all of them, see "One look for every part"; `char_extras.py`
  the ear variants, tail and earbud in `extras.*`, see "Asset contract: extras"; `char_fx.py`
  the face effects in `fx.*`, see "Asset contract: face fx"):
  the same pixelization, warped per pose; the drawn pieces (body below the circle, free hand,
  sleeves, back view, face sprites) are code, no imported art. Output `assets/char/parts.png`
  (8-bit palette, about 180 KB) and `parts.json`; the build is byte-reproducible.
- Gadgets are drawn in code (`tools/build_gadgets.py`): the laptop is projected face by face
  from a small 3/4 model (yaw, pitch, lid angle) and shaded with the lab ramps; its effect map
  carries the screen's u/v so the runtime draws the code, the status bar and the lit keys at
  the screen's own pixels.
- The lab is drawn in code: `tools/build_bg.py` (entry point, palette, neon, shared-palette
  quantisation) with `build_bg_sky.py` (skies, city, rain drops), `build_bg_room.py` (wall,
  window, curtains, posters, pipes, foreground) and `build_bg_props.py` (shelf, desk). Light
  comes from the window (upper right): lit edges get one lighter pixel, the others one darker,
  every object has a 1 px dark outline and shading is ordered-dithered between ramp steps,
  like the character sheet. Signs carry abstract glyph blocks, never real text or logos. A commercial game screenshot was used only
  as a style reference (limited palette, dithering, neon outlines, smoke, text window); none
  of its pixels are used, traced or committed.

## Live mode (stage 2)

### Serving

`STIRA_STAGE=1` (off by default; read at start-up) registers three GET page routes in
The chat server (`src/stiracore/observability/stage.py`):

| Route | Serves |
|---|---|
| `/stage` | redirect to `/stage/` |
| `/stage/` | `dashboard/templates/stage.html`: extends the shell (`base.html`: side menu, the dashboard's translation catalog as JSON data, the CSRF meta tag); the demo page's scene DOM plus the composer and the log, `data-stage="live"` |
| `/stage/<asset>` | an allow-list only: `stage.css`, `js/*.js`, the bundled three.js and its licence, generated `assets/<dir>/*.png|json`. Not served: `tools/`, `assets/source/` (the source icon), `README.md`, the demo `index.html` |

No API route and no permission is added; the CSP is Stira's usual one (`script-src 'self'`,
`connect-src 'self'`), no inline script or handler, no CDN, no web font. The page loads
the shell's scripts (`i18n.js`, `api.js` — `stiraFetch` with the CSRF token — `shell.js`)
and `static/js/chat_core.js` before its module.

### Sources and the event adapter

The page polls the APIs the chat and approvals pages already read, all same-origin (relative
URLs; the server refuses cross-origin API reads, #185/#338) and masked by the server:

| Source | Every | Read fields | Stage events |
|---|---|---|---|
| `GET /api/gateway/status` | 2 s, up to 15 s idle | `neural_state` (6 numbers), `breaker.level`, `uptime_seconds` | `neural`, `breaker` — only when they change |
| `GET /api/turns?limit=1` | 1 s, up to 10 s idle | `turn_id`, `started_at`, `completed` | which turn to follow |
| `GET /api/turns/<id>/events?after_seq=N` | with the above | the turn contract (#186): `turn.started`, `tool_call.created/updated` (`tool_use_id`, `name`, `status`), `breaker.transition` (`to`), `turn.summary` (`after`), `turn.completed` (`error`) | `turn.start`, `tool.start` / `tool.end`, `breaker`, `neural`, `turn.end` |
| `GET /api/hitl/pending` | 1.5 s, up to 5 s idle | `id`, `tool_name`, `args_hash`, `normalized_input` (masked), `reason`, `reason_code`, `reason_params`, `break_level`, `expires_at` | `approval.request` / `approval.resolved` (oldest pending request first) |

The intervals are the shortest ones (#369, `static/js/poll.js`). A poll that finds nothing new
widens step by step (×2 after every 3 unchanged answers) up to the "idle" value; it is short
again at once when something changes, while a turn runs or an approval waits, and when an
event arrives on the page's own chat stream (an `approval.requested` reads the queue
immediately). A failing server is asked less and less (×2 per failure, at most every 30 s).
A hidden tab does not poll at all and polls once when it is shown again. `dashboard/stage/js/adapter.js` is the mapping:
pure functions, no DOM, no clock, tested in Node against events recorded from real chat server
turns (`tests/stage_adapter_test.cjs`, fixtures in `tests/fixtures/stage_*.json`). Rules:

- **Whitelisted fields only.** Tool previews, tool payloads, prompt/response text, the SOUL
  path, credentials and paths are never copied into a Stage event. The scene and the text
  window show the **tool name** at most. The masked arguments of an approval stay in the
  approval record and are shown only when the person asks for them (below).
- Tool name → kind: `memory_*` and `retrieve_offloaded_content` → **memory** (shelf),
  `web_*`, `http_request`, `perplexity_search` → **web** (window), anything else → **tool**
  (desk). A finer `category` (`code`, `subagent`, `other`) and `op` (`read` / `write` for
  memory tools) travel with `tool.start` for gadgets; the model reads `kind`.
- Breaker `coherence` is drawn with the WARNING look (the raw view and the text window keep
  its name).
- A turn found on page load that has already completed is history: it is not replayed. A
  turn that started before the server process (`server_time - uptime_seconds`) and never
  completed was cut by a restart and is not shown as running.
- Status snapshots are applied after the turn events of the same moment; while a turn is
  open its own events carry Neural State and breaker changes.
- **Pacing:** polling often sees a fast tool's start and end in one batch. An end is held
  until its start has been on screen 1.2 s (a turn end: 1.6 s after its start); order is
  kept and a backlog of more than 24 events is applied at once. Only the display is delayed;
  the raw view lists each source event with its sequence number.

The text window describes the state in a neutral voice from catalog keys (`stage.say_*`):
idle, turn running, tool/memory/web running (with the tool name), waiting for approval
(with the tool name and how many more are pending), WARNING / COHERENCE / CRITICAL, turn
finished (ok / error), cannot reach the API. The narration itself never quotes the agent's
text; the only agent text on the Stage is the conversation's reply (next section).

### Conversation (#350 phase 1)

**What is shown.** Stage shows only the conversation's reply text (`delta` of
`POST /api/chat` and `response` of `GET /api/chat/history`) and the person's own messages,
in the text window, the whiteboard and the log. Tool output, file contents, approval arguments (except the
masked "show the arguments" panel above), memory, observation events and secrets are not
read. Sending uses the same API and protections as the chat page (CSRF, `Idempotency-Key`)
and adds no permission. The reply is shown as the chat page shows it (no extra masking,
the same text); rendering is `createElement` + `textContent` only, so HTML or `on*`
attributes in a reply stay plain text (`tests/stage_talk_test.cjs`).

- **Shared code.** `static/js/chat_core.js` holds what the chat page and the Stage both
  need: `POST /api/chat` with `stiraFetch`, the SSE reader, the `Idempotency-Key`, the
  "unknown outcome" rule (never re-sent automatically: *re-send the same* or *discard*,
  nothing else can be sent meanwhile), the image rules (5 images / 10 MiB, PNG/JPEG/GIF/WebP,
  paste or the image button), the IME guard and the history read. `chat.js` uses it too
  (`tests/chat_core_test.cjs`; the chat tests run against it).
- **Typewriter** (`js/typewriter.js`): 38 characters per second; a backlog longer than 3 s is
  shown faster, at most 90 characters per second. It never shows a character that has not
  arrived and never splits a character (grapheme clusters). Click the line or press
  `Enter`/`Space` (page focus) to show the rest; `prefers-reduced-motion` shows each line at
  once. Screen readers hear the finished line once (`#line-live`), not each frame.
- **Line or whiteboard** (#350 phase 2, `js/markdown.js`, docs/stage-main-ui.md §5). A reply
  goes on the **whiteboard** when it has a code block (also unclosed), a table, a heading, a
  quote, a list of 3 or more items, more than 140 visible characters, more than 3 paragraphs or
  a paragraph of more than 4 rows; otherwise it is a **line** in the text window, spoken
  without Markdown marks (at most 3 rows / 120 characters; longer: "Read the rest in the log").
  The rule reads the text only (the same reply always gets the same answer) and is display
  constants, never learnt. A stream is classified again as it arrives (at most every 0.25 s):
  a line can become a board, a board never becomes a line while it streams. When the reply has
  ended, its whole text is classified once more from scratch — the same answer the log and a
  reload give; if that says "line" (a stream can look like a table for a moment, `|-` before
  `|-x|`), the board this reply lowered rolls up again unless it was pinned. With a board, the line speaks the reply's first
  paragraph when it is short (≤ 120 characters, ≤ 4 rows, no code) and the board shows the
  rest; otherwise the line says "I put it on the board."
- **Props while the board is down.** The laptop and the signal lamp of a code turn stand on the
  text window's edge in front of her and would cover the board's lower right: while the board is
  down they wait (`gadget_scene.boardHides`; still caused by the turn's events, back as soon as
  the board rolls up; the debug panel lists them under "hidden for the board"). Drones and the
  room's props (the back-view laptop on the desk) stay.
- **Whiteboard** (`js/board.js`). A projection screen comes down from the ceiling (the case,
  the cloth unrolled with `clip-path`, the weight bar; 270 ms ease-out) and, with the same
  progress, the character steps to the right edge (her layer moves about 140 art pixels at
  1300 px, the room pans 12; see "Layers"). One reply is one board: its headings, code blocks
  and tables in order, the title is the first heading, the footer says what it holds
  (`見出し 4 · コード 3 · 表 1 · 1716 字`). **× / Esc / swipe up** rolls it up and she steps back;
  **📌 Pin** keeps it when the next board reply comes (the new one waits in the log: "▶ Open on
  the board"), otherwise the next board reply replaces the contents without lowering the
  screen again; **Copy** copies the reply's whole Markdown source (also when the board shows
  only its head; from the log: the recorded response, not the log's cut copy), and every code
  block has its own copy button; a refused clipboard says so. A reply from the history (page load, the log) does not lower the board by
  itself: the line offers "Open on the board ▶". Replies longer than 64 KiB show their head
  and say that the whole text is on the chat page. `prefers-reduced-motion`: the screen and her
  step are immediate. On a phone (portrait) and when there is too little room beside her, the
  board is in front of her and she looks out at the right edge (docs/stage-main-ui.md §4-6).
- **Rendering** (`js/md_render.js`, the board and the log). A Markdown subset (headings,
  paragraphs, lists one level deep, quotes, rules, GFM tables, fenced code, inline code,
  strong, emphasis, links) built with `createElement` / `createTextNode` / `textContent` only.
  Raw HTML, images, footnotes, nested lists and links with any scheme other than absolute
  `http(s)` (no credentials) stay plain text: nothing is parsed as HTML, nothing is loaded, no
  attribute handler or style is set. A link never gets an `href` before it is confirmed: it is a
  button (so middle click, the context menu or dragging have nothing to open), a click opens the
  shell's confirmation dialog with the real address (and the link's text when it differs), and
  only "Open" calls `window.open(url, '_blank', 'noopener,noreferrer')`; without the dialog the
  link is plain text with its address. Work is bounded on every path (the board, the log, a
  stream): the parser keeps the first 64 KiB, scans lines by hand in linear time (no regular
  expression that can backtrack; every step consumes a line), keeps at most 2,000 blocks and,
  per table, 24 columns × 400 rows; the renderer draws at most 20,000 elements (the log 3,000
  per reply), counted where every element is made: once the budget is spent no element is
  made any more — the rest of the paragraph, cell or item is one text node and everything after
  it one plain-text paragraph under "shown without formatting" (nothing of the reply is lost).
  A link's confirmation dialog opens with **Cancel** focused, and auto-repeated Enter / Space
  are ignored inside it (`confirm_dialog.js`): the press that opened it cannot also answer it.
  Code is monospace with its language label and colours from a small
  built-in highlighter (Python, JavaScript/TypeScript, shell, JSON, CSS, SQL; tokens join back
  to the exact source, never run); long lines and wide tables scroll sideways inside their
  block. Colours are dashboard tokens (`--board-*`, `--code-*`, `--tok-*` in `ui/tokens.css`;
  contrast in docs/stage-main-ui.md §9). Tests: `tests/stage_board_test.cjs` (hostile Markdown,
  links, the rule and its determinism, the highlighter round trip).
- **Log** (`ログ` button or `↑`): the recorded turns (`/api/chat/history`, the last 50), the
  turn in flight, and local notes for sends that the server did not record (stopped, failed).
  Each entry is the person's message or the reply text only; replies are rendered by the same
  Markdown renderer, and a board reply has "▶ Open on the board".
- **Context gauge** (`ctx 38%`, left of the Log button; the chat page shows the same next to
  the turn tokens; `chat_core.contextGauge`). The prompt of the turn's **last model call**
  (`context_tokens` on the `turn` event and the history: uncached + cache read + cache write,
  normalized per provider like `providers/usage.py`; Strands records each call's usage per
  event-loop cycle), divided by the context window from `/api/gateway/status` →
  `context_window` (read-only: `tokens`, `source`, `compression_threshold`; the same resolution
  summarization uses, docs/context-management.md). It warns from 80 %, says "auto-summary soon"
  near the summary threshold only with context management on, and "auto-summary off" from 80 %
  when it is off (never "soon" when nothing summarizes). The tooltip names the window source
  when it is the assumed default (200,000), and the summary setting. Without a window it shows
  the token count; a record without `context_tokens` (an older server, or an SDK that reported
  no per-call usage) falls back to the turn's sum, and the tooltip says that a tool turn then
  reads high (docs/stage-main-ui.md §12).
- **New conversation** (`新しい会話` next to the Log button, or `/new` typed alone; the chat page
  has the same button next to the gauge). `/new` is the page's command exactly when the
  message, trimmed of surrounding white space, is the ASCII text `/new` in any case (`/new`,
  `/NEW`, ` /New `), with or without images; it is never sent to the model — the shared sender
  (`chat_core.createSender().send`) refuses it too (whether the message or the request body says
  it), before a key or a request exists. Anything else is an ordinary message: `/new please`,
  full-width `／ｎｅｗ`, `/new` with a zero-width space. The command opens the shell's
  confirmation dialog ("The context so far is not carried over. Memory, SOUL and the approval
  history are kept"; **Cancel** focused, held keys ignored) and then calls
  `POST /api/chat/session/new` (`chat_core.createSessionStarter`, CSRF + `Idempotency-Key`).
  The button is disabled with the reason as its description while a turn runs (this page's or
  another's), an approval is waiting, a send's outcome is unknown (re-send or discard it first:
  it might still be recorded in the old conversation), or the server is unreachable; the server
  refuses the same cases with 409 and a code. The page forgets the old conversation only after
  the server reset it: the line says "A new conversation has started.", the log starts with
  "New conversation started (HH:MM)" (from `/api/chat/history` → `session`), the board rolls up
  unless pinned, and the gauge hides until the next turn; after any success the page re-reads
  the history and shows the server's conversation as it is. Sending stays off until that history
  is on screen; if the re-read fails the page says "The new history could not be loaded. Please
  reload it." with a Reload button, and nothing is sent until a re-read succeeds. An unknown outcome (network error,
  5xx) keeps the key and **blocks sending messages** (one could land in the conversation about to
  be dropped): pressing again repeats the same key, and the server answers with this key's
  session ("already", also after the generic 300 s) instead of clearing twice. The outcome is
  also resolved when the history's `session.request` equals this key's digest — a changed
  session id alone (another tab's reset) never counts. A resolved key is finished; the next new
  conversation uses a new key. Another open page notices the new session the next time it reads
  the history (known limit; the status API does not carry the session).
- **Stop showing (■).** While this page's turn runs, the send button becomes **■ Stop
  showing**: the page closes its stream (`AbortController`) and stops waiting. It does not
  cancel anything on the server, so it is not treated as a cancel (since #452 the server runs
  the turn on its own worker: a closed stream never stops it; the explicit stop is the chat
  page's **■ Stop**, `POST /api/chat/cancel`, docs/configuration.md "A turn outlives its page"). Measured on the chat server
  (offline fake model): the server does not interrupt a model call in progress; it finishes
  the turn and records it, and the composer says so until the turn is over. The outcome is
  therefore **unknown**, exactly like a cut connection (fail closed): the send keeps its
  `Idempotency-Key`, the message goes back into an empty composer, and
  - the same text (and images) sent again — Enter in the composer or *re-send the same* —
    goes with **the same key**: the server answers "already received" (409
    `duplicate_request`) and the turn is not run twice (measured: one model call; the log
    shows the recorded turn once, with the note after it);
  - an edited or new message is held back (the chat page's rule) until the unknown send is
    **discarded**. Discard is its own step with a confirmation dialog saying the send may
    already have been processed and that sending the same content afterwards may run it
    twice; without the dialog nothing is discarded. After a discard the next send has a new
    key (`tests/chat_core_test.cjs`, `tests/stage_talk_test.cjs`).
  - The text put back into the composer is tied to that send's key and to the composer's
    edit generation (every `input` event, whitespace included, bumps it). It is cleared only
    when **that** send settles (delivered or "already received") and the composer was not
    edited since, so pressing Send again does not send it once more as a new message. Another
    send completing never clears it, an edited draft is never cleared, and after a confirmed
    discard the text is an ordinary draft (`refillOutcome`, tests/stage_talk_test.cjs).
- **Why it cannot send now** (one line under the composer): loading, server unreachable,
  approval pending (the server would answer "busy"), a turn from another page running (or
  the stopped one still running), images still being read, or an unknown outcome waiting
  for *re-send / discard*. *Re-send the same* passes exactly the same gate as the send
  button (`status().canSend`); it only differs in reusing the unsettled key.
- **Log notes.** A re-send refused as a duplicate is noted after the turn the stream
  announced for that key earlier (the stopped turn); without such an id it is a standalone
  note naming the message, in time order, not tied to any turn. A stopped or failed send is
  noted after the turn that was newest when it was sent. If the server recorded that send, the note is matched to the recorded turn by
  the turn id its stream announced (a send that never got an id falls back to the turn right
  after the anchor with the same text and image count, each recorded turn matched once).
- **The character follows real events only.** The page feeds its own stream's `ui_event`s
  (the same turn contract as `/api/turns/<id>/events`) into the live feed, so "turn running"
  (thinking) starts with the real `turn.started` and ends with `turn.completed`; the poll
  skips the events it already has (same turn, same `seq`).
- **Text window priority.** A reply stays on screen while the narration only says "turn
  finished" / "idle"; new activity (another turn, a tool, an approval, a breaker change,
  offline) takes the window. During this page's own turn only urgent states (approval,
  WARNING/COHERENCE/CRITICAL, offline) interrupt the reply.
- **Main page, minimum information** (docs/stage-main-ui.md §3): name plate, the person's
  last message, the line, the composer, the reason line, *re-send / discard*, the approval
  choices, and two buttons (Log, Raw view). Numbers, tool details, tokens and the breaker
  history stay in the raw view (`Tab`), the debug panel (`D`) and the chat page. The menu's
  chips show approvals / breaker / connection; the Stage's own connection badge appears only
  when offline.
- **Fallbacks.** `/chat` always exists; without JavaScript a `<noscript>` link points there.
  Without WebGL (or `?webgl=0`) the scene is a still picture (`js/still_scene.js`: the room's
  colour layers and her rest pose composited on the CPU from the same parts and hair
  segments) with "WebGL is not available: showing a still picture. Open the chat"; the
  composer, the log, the board (in front of the picture) and the approvals work as usual.

### Approvals in the choice window

A pending request opens the choice window (amber neon). The choices are what the existing
approval API supports, and nothing that weakens it:

1. **Show the arguments (masked)** — opens a panel with the tool, why it is asked (the
   approvals page's wording, #279/#284), the expiry, the request id, the `args_hash` and the
   server-masked normalized arguments (the same display as `/approvals`).
2. **Approve once** — disabled until the arguments of *this* request were shown here, and
   when the request has expired (client clock corrected by `server_time`).
3. **Reject.**
4. **Open on the approvals page** — editing, responding and rejecting with a reason stay
   there.

A decision is one `POST /api/hitl/<id>/decision` with the CSRF token, the displayed
`args_hash`, this tab's `client_id` and an `Idempotency-Key` (one per attempted decision,
reused when the same decision is retried after a network error, a new one after
`binding_mismatch`). The server stays the authority (stale, expired, already decided, raced);
its refusals are shown with the approvals page's messages. "Show me the diff first" from the
spike is gone: the API has no diff. There is no "always allow" or session permission.

### Activity on the board (#446)

The board has a second mode beside the reply: **activity** (`js/activity.js`). It answers
"what is she doing now, what did she just do, how much of the YOLO window is left" while no
reply is on the board — also outside a turn, and after a reload.

- **Opening it.** The text window's **稼働状況 / Activity** button (`aria-pressed`, toggles
  it), or, when the board's own tick 「待機中はボードに稼働状況を出す」 / "Show the activity on
  the board while she waits" is on, by itself when the page loads and when the followed turn
  ends while the board is closed (a board reply that is down stays). The tick is this
  browser's choice (`localStorage` `stira.stage.board_idle` = `on` / `off`, default off: the
  Stage looks as before until someone turns it on). `?board=activity` opens it at load
  (captures). A reply always takes the board from the activity, unless the person pinned it;
  closing the board (×, Esc, swipe) ends the mode.
- **What it shows.** *Now*: waiting / generating (seconds since the model call started) /
  running a tool (name, seconds) / the turn between steps / waiting for approval (the tool
  name, how many) / a progress report waiting for an answer (ADR 0014; its trigger: time,
  tool calls, tokens) / paused (the YOLO window is paused; the reason code) / stopped (breaker
  CRITICAL or the YOLO latch) / the server cannot be reached. *Stall*: while the model or a
  tool runs, when the running turn has written no event (besides `model.progress`) for
  `stall_sec` (120 s), the line 「応答待ち: 最後の動きから N」 is added. *Recent*: the last 20
  rows across turns, newest first — time, ✓ / ✗, the kind (turn start / end, tool done /
  failed / not run — one row per call, its end — or 「区切り」 / *Checkpoint* (·, neither ✓
  nor ✗) for a declared checkpoint's own `handoff_to_user`, which is cancelled on purpose and
  not counted as denied in the YOLO window (#545), model call with its duration, approval asked / decided, breaker level,
  progress report / answer) and the tool name; when a turn wrote workspace artifacts
  (#454), its end row and its done `file_write` / `editor` rows carry the count and link to
  the artifacts page filtered to that turn, 「成果物 N 件」 → `/artifacts?turn=<turn>` (#535;
  same origin, the opaque turn id only, no path or body; a turn that wrote none has no link). *YOLO window*: state and scope, time left, automatic
  approvals n / limit, external and exec n / limit when the window has them, the pause reason
  with the breaker's reason codes (#455). *Totals* since the server started: turns, input /
  output / cache read / cache write tokens, the last context size (and its share of the
  context window), the estimated cost only when token prices are configured for the model
  (`gateway-config.json` `token_prices`, [configuration.md](configuration.md#the-token_prices-section-451);
  else 「単価が未設定」 — Stira ships no prices). Each turn is priced by its own model; a turn
  that cannot be priced is left out of the sum and the line says so
  (「単価が未設定のターンを除く」).
- **One source.** `GET /api/activity?after_id=<row id>` (`observability/activity.py`, read
  only, observe). The first read (no cursor) brings the last 20 rows of the listed types from
  `turns.db` and the cursor; later reads bring only rows after it (`events_after`), so a
  reload restores the list from the server and the polls stay small. The same answer holds
  the state, the YOLO numbers (read from `yolo.runtime.status()`), the totals and
  `server_time` (the page corrects its clock and ticks the seconds itself, once a second,
  without a request).
- **Allow-list, not filtering.** The rows and the state hold event types, codes, counts,
  durations, timestamps and tool names (`[A-Za-z0-9_.-]`, else `other`) only. The server never
  reads previews, payloads, arguments, outputs, approval reasons or commands into them; the
  page draws with `createElement` + `textContent` (`tests/test_activity_board.py`,
  `tests/stage_activity_test.cjs`).
- **Polling (#369).** Only while the activity is on the board, through `static/js/poll.js`:
  4 s while something runs (a turn, a tool, an approval), widened step by step to 20 s while
  nothing changes (about 3–4 reads a minute when idle), none while the tab is hidden (one read
  on return), ×2 per failure up to 60 s. With the board closed the page reads nothing more than
  before.
- **Not here.** Stopping or approving from the board (the YOLO badge, the approval window and
  the composer's stop do that); notifications; history beyond the last rows.

#### The goal grant on the board (ADR 0011 stage C, W5b of #296)

**Alpha** (the overview and its known limits: [autonomy](autonomy.md); the grant rules:
[grants](grants.md)). Only with gateway `autonomy.stage_c` on. **Off (the default) nothing changes**: `GET
/api/activity` has no `grant` key and its answer is the one of before, byte for byte
(`tests/test_activity_grant.py` compares it with a golden recorded on main's code); the
page draws no extra element, adds no timer and makes no extra request
(`tests/stage_board_grant_test.cjs` compares the activity DOM with main's).

- **What it shows** (in the activity mode, between *Now* and *Recent*; a region labelled
  「目標グラント <id>」 / "Goal grant <id>"): the newest grant issued since the server started —
  its headline (the grant's or the goal's state, the higher-priority one, with the reason
  code), the goal state, iterations n / max and the iteration's run state (idle / running /
  yielded to your turn / a stopped call still open), the last end in words with its detail
  code, the TTL left (a `<meter>`, counted down between reads from the answer's
  `server_time`) and the budget left (a `<meter>`) with this iteration's tokens / the
  per-tick limit, and the last five iterations as rows (✓ / ✗ / ·, number, end, duration,
  and the scope tools that **ran** in it, in call order — none for an iteration that called
  none; never the admitted scope). A waiting card says 「あなたの承認待ちです（承認ウィンドウのカード）」 and links to
  the approvals page — the card itself (tool, arguments) is never drawn on the board; an
  agent's completion claim says it awaits your verification — W6c: with the claim's evidence
  (below). A plain link leads to Settings
  → Goal grants (`/settings#sec-grants`), where Revoke / Pause / Verify / Run one iteration
  stay: **the board is display only** (no button, no route, no state change).
- **A completion claim's evidence (W6c).** While the goal awaits your verification, and
  read-only after you confirmed it (the audit trail), a group in the panel shows what the
  harness measured at the claim — codes and observed counts, no percentage, never the model's
  text: 「反復 k/最大 で申告 · チェック · 作業ツリー · 進捗」 / "Claimed at iteration k/max ·
  checks: passed / failed / not run / unknown · workspace: changed / unchanged / unknown ·
  progress: a new outcome / a repeated outcome / nothing observed / unknown", one line per
  check (kind, status, counts), the tokens used / left and the time left at the claim, and
  「<時刻> からあなたを待っています（n 分）」 / "Waiting for you since <time> (n)". A claim whose
  checks **failed** is amber (`data-tone="amber"`) **and** has a 「⚠ …」 / "⚠ The checks failed
  at this claim: look before you confirm." line — the words carry it, not the colour alone.
  Past gateway `autonomy.claim_stale_sec` (default 30 min) the cue becomes 「まだあなたを待って
  います」 / "Still waiting for you" — counted on between reads from the answer's
  `server_time` like the TTL, by the existing once-a-second redraw (no timer, no request, no
  model call, no action). The seconds are never announced (they are in the "since" line, not
  in the cue), and like the TTL they are no news for the poll (a waiting claim does not keep
  it at 4 s); a measured code or `stale` turning true is. The block is `grant.claim` (an allow-list: `activity.claim_view`;
  every code in its fixed set from `grants/schema.py`, else `unknown` — never `pass`; a check
  row that cannot be read makes the checks `unknown`); absent unless the goal awaits
  verification or is complete. A grant with no claim to show draws W5b's panel node for node
  (`tests/stage_board_claim_test.cjs` compares it with main's `activity.js`).
- **Heartbeat start** (W6d, only with `autonomy.heartbeat_start` and only for a grant issued
  with its `heartbeat_start` flag; otherwise no key — the answer and the DOM are as before):
  the block `grant.tick` (an allow-list: `activity.tick_view` — the starts, the last outcome
  `started` or a skip reason of `scheduler/grant_tick.SKIP_REASONS` (else `other`), the counts
  per known reason, the spacing, `unavailable: scheduler_off` / `heartbeat_off` when the server
  started without a running scheduler / heartbeat (a line in words), and the last
  heartbeat-started iteration's numbers: model
  calls, tokens in / out, cache read / write when reported, wall time). Drawn as lines under
  the iteration count ("Heartbeat start: n started · last: …", "Skipped: … ×n" — the four most
  frequent —, "Last heartbeat iteration: in / out tokens · time"); never announced. The
  ticker's counters are read without blocking inside the same record bracket (a held lock:
  `grant_busy`); the cost rows come in the executor's own seqlock snapshot.
- **On with no grant** (or one issued before the server started: it ended at the restart):
  `grant: null` and nothing is drawn (the quieter of the two choices). A read that fails
  is `grant: {"error": "grant_unreadable"}` (never a 500 of the whole answer), a read that
  found a lock on its path held is `grant: {"error": "grant_busy"}` (it never waits), and
  the panel says it could not be read; while the server cannot be reached (two failed reads) it says
  「不明」 / "unknown" and shows no number, never an old one as live.
- **Allow-list** (`activity.grant_view`, like `window_view`): the short id, state / reason /
  goal state / goal reason / headline codes, counts (iterations, TTL, budget, per-tick,
  tokens this tick), the `awaiting_verification` and card flags (and the count of waiting
  grant cards), the iteration's run state / number / end / detail codes, and per ended
  iteration its end / detail codes, number, duration, tokens, time and the tools that ran.
  Never the goal, a path, an argument, a card's content or any other text. **An allow-list of
  values, not of shapes:** every code must be a member of its fixed set — the state
  machine's statuses and reasons (`grants/state.py`), the executor's end and detail codes
  (`END_CODES`, `DETAIL_CODES`), the scope's tool names (`grants/schema.TOOLS`); anything
  else is `other` (a state, an end, a tool) or left out (a reason, a detail). The page keeps
  the same sets (a test keeps them equal) and draws nothing outside them. A marker in the goal,
  echoed by the model in its text and in a tool argument, is in no answer (tested).
- **Known limit (accounting failures).** The version check keeps *concurrent* reads from
  mixing; it does not repair a failed write. If a charge or an iteration count raises, the
  board shows what the accounting then says (e.g. the tick's tokens without the grant's
  budget moving) as a consistent version — the writer's failure itself takes the existing
  path (the grant store / authority goes unhealthy and the grant is revoked by the rule
  for a failed write).
- **Memory only.** The block comes from `GrantService.board_view()` (the newest record in
  memory — no look, no store read or write, no event) and `GoalIterationExecutor.board()`
  (the iteration's codes and the last five ends, kept in memory, read as one snapshot);
  nothing runs under a lock of the chat API, and **no lock on the path is waited for**: each
  (the grant service's, the executor's, the iteration's, its budget's) is taken without
  blocking, and a held one gives `grant_busy` at once (no helper thread).
  **One version** (fix round 2): the block is built from one consistent version of the
  grant. The record is immutable and every change replaces it, so the route reads it, then
  the executor's pieces, then the record again — a different record means a change was
  written in between. The executor runs every change that spans several pieces (an
  iteration's start, a call's settle + charge, an iteration's end with its counts and row)
  as a *board write*; its version `_board_seq` is **one integer** (a seqlock): even = stable,
  odd = a write is open (it moves when the first open write begins and when the last one
  ends; writes may overlap). The read takes the integer once (odd → busy), its pieces, then
  the integer again (moved → busy); the route's two record reads bracket that read. Either check
  failing gives `grant_busy` — never a mix such as "iteration 1 ended" with "0 iterations"
  and no row. No wait, no retry, no new lock on the read path. The page keeps the last
  readable panel for a busy answer, marked as the last read's values (no new announcement),
  and the watch takes it as no news (not an iteration's start or end); `grant_unreadable`
  and offline still draw "unknown" with no number. The state is as of the last look (the authority watchdog's pass every
  15 s and every control request), so a TTL can read 0 for a few seconds before the grant
  shows `expired`.
- **Polite announcements.** The board has one `role="status"` / `aria-live="polite"` region
  (created only with a grant), kept across the once-a-second refreshes and written only when
  its text — headline, "an iteration is running", the cue — changes; the seconds never
  announce; when the grant goes away (a restart, no grant, stage C off) the region goes
  with it, so no old "running" stays. Reduced motion: the board jumps as before; the panel
  has no animation.
- **Focus.** The once-a-second refresh rebuilds the activity's nodes; a link in them that had
  the focus (Settings, the approvals page, an artifacts link of a row — the same loss
  affected those before W5b) gets it back on its new copy (same tag, href, class and
  occurrence). Focus elsewhere is never touched and nothing is focused that was not.
- **Watching for an iteration** (only with `autonomy.stage_c_exec`: the server writes
  `data-grants="exec"` on `#app`). The activity's one poll (`/api/activity`, the same
  `poll.js` loop, no second poll or timer) then also runs while the board is closed: at the
  idle cadence (widening to 20 s), 4 s while an iteration runs, none while the tab is hidden,
  backed off on failure. The start of an iteration may lower the board in the activity mode:

  | An iteration … | Board / page | Result |
  |---|---|---|
  | does not run (or the read failed / offline) | any | nothing |
  | is read as `grant_busy` (a change was being written) | any | nothing: the watch's history stands (no start, no end); the panel keeps the last readable values, marked 「更新中」 / "Updating", with no new announcement — for at most 3 unreadable answers in a row and 20 s since the last readable block (`BUSY_KEEP_READS`, `BUSY_KEEP_SEC`), then "unknown" (no numbers, not running) |
  | is read as `grant_unreadable` or any other `error`, or the read fails | any | nothing: the watch's history stands (no start, no end: an iteration that still runs after it does not come down again; one that ended in between is no start); the panel says "unknown" |
  | still runs (already seen at the last read, e.g. the person rolled the board up with Esc) | any | nothing — it stays up |
  | starts | a reply is on the board (pinned or not) | nothing: never taken, she does not move |
  | starts | the activity is on the board | nothing new (it updates in place) |
  | starts | the board is still rolling up or down | nothing |
  | starts | the board is closed but **pinned** (the pin is board-level and survives a roll-up) | nothing: her next reply must not find the board held |
  | starts | the person's own turn is open | nothing (her reply takes the board) |
  | starts | the log is open | nothing |
  | starts | the tab is hidden | nothing (on return, the first read decides) |
  | starts | the board is closed and not pinned, no turn, no log, tab shown | the activity comes down — not explicit: no focus, no pin, the log stays closed |
  | ends with a completion claim (goal `awaiting_verification`, W6c) | any | nothing moves: an end is no start (no lowering, no focus, no pin); a board showing the activity draws the claim's evidence and "awaiting your verification", announced once in the polite region |
  | … whose checks failed | any | the same; the claim group is amber with a "⚠" line in words, and the announcement says it once |
  | … that waited past `autonomy.claim_stale_sec` | any | nothing moves: the cue becomes "still waiting for you" (the once-a-second redraw, from `server_time`; no new timer or request), announced once |
  | … that you confirmed | any | nothing moves: the evidence stays, read-only (the audit trail); no cue |
  | starts from the heartbeat (`grant_tick`, W6d: a grant issued with `heartbeat_start`) | any | the same rows as any start above (a heartbeat start is an iteration's start like a press: the board is lowered only onto a closed, unpinned board with no turn, no log, the tab shown; no focus, no pin); the panel's "Heartbeat start" lines update in place, never announced |
  | is skipped by the heartbeat (`tick_skipped(<reason>)`) | any | nothing moves: a skip is no start and no end; the skip counts and "last" update in place (allow-listed codes and counts, no announcement, no new timer or request) |

  Unchanged around it (tested in `tests/stage_board_audit_test.cjs`): a reply from the
  person's own turn takes the board from the activity unless the board is pinned
  (`boardAction`); a pinned reply is never taken by the activity unless the person asks;
  Esc / × / the swipe roll it up; the two canvases, the room pan and the foreground follow
  the board's progress as before. `activity.attention()` is the table
  (`tests/stage_board_grant_test.cjs` covers each row).

#### Who owns the board's control (W5b audit)

`board.js` `createBoard` is the **only** place that opens, closes, pins and switches the
board's mode (`show` / `showActivity` / `updateActivity` / `close`); every caller asks it
and it decides (`boardAction`, `finalBoardAction`, the pinned-reply guard of
`showActivity`). The callers: `talk.js` (a reply of this page's own send, "▶ Open on the
board", the final re-classification, `/new`), `main.js` (the Activity button, the idle
default, `?board=activity`, the iteration watch — wired in `activity.js`
`startActivityController`, tested in jsdom: `tests/stage_activity_ctl_test.cjs`; Esc when
the log is closed — the board's own key handler takes Esc first), and the board's own ×,
pin, copy and swipe. The controller is started once per page and never stopped (it
returns nothing). Besides the controller, `activity.js` builds nodes and decisions
(`renderActivity`, `attention`, `watchResult`); only the controller calls the board, and
only through `showActivity` / `updateActivity` / `close` / its properties. The audit is `docs/board-audit-2026-10.md`.

### Frame rate (30 fps)

The Stage draws at **30 fps** by default, whatever the display's rate (a 120 Hz ProMotion screen
included): 30 reads better as pixel art, and the GPU does a quarter of the work it did at 120.
`requestAnimationFrame` still fires at the display's rate; `js/pacer.js` decides on each call
whether a frame is due, and only then does the loop update the scene and render. The pacer is an
accumulator: a frame is due once the time since the last rAF has added up to 1/30 s (less
2 ms of tolerance for timestamp jitter), the remainder carries over, so the average is exactly
30 on 60, 75, 120 and 144 Hz displays; a backlog is dropped, never replayed. The first frame is
drawn at once.

- **Real time, not frames.** The loop hands every system the real time since the last drawn
  frame (`dt`, capped at 0.1 s) and the running clock; nothing counts frames. The animation
  catalog, the motion player (holds are in seconds; the shortest is 0.08 s, so no pose falls
  between two 30 fps frames, tested), blinks, the hair (fixed 1/240 s steps inside a frame), the
  steam, the props, the typewriter and the whiteboard's 270 ms roll all run on that `dt`, so
  the tempo is the same at 15, 30 and 60 fps (`tests/stage_pacer_test.cjs`).
- **Setting it.** `?fps=N` (an integer, 15 to 120) or the one `localStorage` key
  `stira.stage.fps` (the same range; the page only reads it). `?fps=` wins when present; a
  value that is not a plain integer in range (`30.5`, `1e1`, `14`, `121`, empty) means the
  default 30 — not the stored value, so a link always behaves the same. Nothing else: no
  settings page, no server option. The target cannot exceed the display's rate.
- **Whiteboard.** While the board rolls down or up (270 ms, and she steps aside with it) the
  loop runs at 60 fps if the target is lower (`BOARD_FPS`): at 30 the 270 ms roll is only 8
  frames, and the screen (CSS) and her step aside (canvas) would move in coarse steps; at 60
  it is 16. It settles back to the target when the board has stopped. (A judgement from the
  frame counts, not a measured preference; `BOARD_FPS` in `js/pacer.js` is the knob.)
- **Deterministic paths.** `?frames=N` is not paced: every rAF is one fixed 1/60 s step, frozen
  after N frames, as before, so captures and the pixel checks are unchanged (`?fps=` has no
  effect there). The still fallback (`?webgl=0`) has nothing to render; its text window, board
  and approvals run on the same pacer.
- **Hidden tab.** When `document.hidden` the loop stops asking for frames (the browser pauses
  rAF there anyway) and the pacer is reset; when the tab is visible again the first frame is
  drawn at once with `dt` 0, so the time spent away is never played at once. The Stage's clock
  does not advance while hidden. Deterministic captures ignore this.
- **Debug panel (`D`).** Shows `measured/target fps` (for example `30/30 fps`); the page also
  carries `data-fps` (measured over the last 90 drawn frames) and `data-fps-target`.

### Raw view and debug panel

Both work on live events. The raw view adds the mode, the followed turn (`id #seq`) and each
source with its state and age to the state table; the event log lists each source event and
what it became (`turn 3fa2c1 #4 tool_call.created memory_search → tool.start memory`),
without previews. The debug panel lists the sources instead of the demo steps.

### Phone layout

In portrait the scene is drawn at a whole number of *device* pixels per art pixel and at
least 330 art pixels wide (CSS scale 1 on a 390 px phone at DPR 1–3), so the shelf (memory),
the character and the desk instrument (tools) stay on screen. The view stops at the
character's waist (280 rows). The text window follows below the scene. While an approval is
pending the choices sit over the bottom of the scene, just above the text window, and the
masked arguments over its top, so both are reachable without scrolling (#350 phase 2; in
phase 1 the choices were below the text window and needed a scroll). While the on-screen
keyboard is up (the visual viewport is smaller than 80 % of the window) the scene gives up its
height so that the text window and the composer stay in view.

The character sheet's lower corners (outside the icon's circle crop) are now filled by
copying the nearest original pixel *up along the fitted silhouette line* from a softened
source (`tools/build_char.py`, `extend_circle_cut`), instead of stretching the edge pixels
sideways. Folds and hair strands continue downward instead of turning into horizontal
streaks; it is still a fill, not drawn art.

### Limits (stage 2)

- Polling (≤ 1 s latency while a turn runs or an approval waits, up to 5–10 s when idle: a turn or approval request from another tab/channel is seen within that), not a push stream: the chat server's live turn events are delivered only
  on the chat request's own SSE response, while `/api/observability/events` now streams the same
  turns.db rows to `/observability` (#286). The Stage does not use it; a stream for the Stage
  would cut the latency and is not added here.
- One approval at a time in the window (the oldest); the text says how many more wait.
- `memory.injected` (#186) is emitted only by the opt-in associative recall (#170) and the
  Stage does not read it, so memory *context* injection does not light the shelf; only
  memory tool calls do.
- Heartbeat ticks and scheduler runs are not shown (no per-tick event on these APIs).

## Stage 3 plan

- Agent-side gesture selection tool, hash-pinned human-approved packs, default Soul with a
  synthetic character.
