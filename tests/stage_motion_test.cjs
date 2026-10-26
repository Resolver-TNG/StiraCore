// Stage character motions (#341): catalog validation, layered parts, distinguishable motions and
// faces, deterministic selection from events, the room unaffected by the character's motion.
// node --test tests/stage_motion_test.cjs
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { decodePng } = require('./stage_png.cjs');

const STAGE = path.join(__dirname, '..', 'dashboard', 'stage');
const load = (name) => import(path.join(STAGE, 'js', name));
const N = (values) => ({ type: 'neural', values });

let rigCache = null;
async function rig() {
  if (rigCache) return rigCache;
  const { CharRig, partIds } = await load('char_rig.js');
  const meta = JSON.parse(fs.readFileSync(path.join(STAGE, 'assets', 'char', 'parts.json'), 'utf8'));
  const atlas = decodePng(fs.readFileSync(path.join(STAGE, 'assets', 'char', 'parts.png')));
  // with round A's extras (ear variants, tail), as the browser loads them (char_bridge.js)
  const extras = { meta: JSON.parse(fs.readFileSync(path.join(STAGE, 'assets', 'char', 'extras.json'), 'utf8')), atlas: decodePng(fs.readFileSync(path.join(STAGE, 'assets', 'char', 'extras.png'))) };
  rigCache = { CharRig, meta, atlas, extras, ids: partIds(meta), make: () => new CharRig(meta, atlas, extras) };
  return rigCache;
}
const hash = (px) => crypto.createHash('sha1').update(px).digest('hex').slice(0, 16);
const CALM = { emotion: 'calm', eyes: 'open', mouth: 'neutral', headY: 0, ears: 0, blush: 0, sweat: 0 };
const diffCount = (a, b) => {
  let n = 0;
  for (let i = 0; i < a.length; i += 4) if (a[i] !== b[i] || a[i + 1] !== b[i + 1] || a[i + 2] !== b[i + 2] || a[i + 3] !== b[i + 3]) n += 1;
  return n;
};

async function catalog() {
  const { ids } = await rig();
  const { buildMotions, MOTIONS } = await load('motion_catalog.js');
  return buildMotions(MOTIONS, ids);
}

// Frames of a motion over its duration (no life signs): [{t, px, pixels}]
async function frames(m, step = 1 / 30) {
  const { MotionPlayer } = await load('motion.js');
  const { NEUTRAL_FRAME } = await load('char_rig.js');
  const { make } = await rig();
  const r = make();
  const p = new MotionPlayer(new Map());
  const out = [];
  for (let t = 0; t < m.duration; t += step) {
    const f = { ...NEUTRAL_FRAME };
    p.apply(f, m.tracks, t, false, CALM);
    // at the room desk (desk shot) the picture is her figure there, not the front layer
    const pixels = f.desk && f.desk !== 'none' ? deskPixels(f) : Uint8Array.from(r.compose(f).pixels);
    out.push({ t, px: hash(pixels), pixels });
  }
  return out;
}

// The desk shot's sprites (desk.json) for a frame, in a 278 x 278 buffer (origin: image 300, 54).
let deskCache = null;
function deskPixels(f) {
  if (!deskCache) {
    const meta = JSON.parse(fs.readFileSync(path.join(STAGE, 'assets', 'char', 'desk.json'), 'utf8'));
    deskCache = { meta, atlas: decodePng(fs.readFileSync(path.join(STAGE, 'assets', 'char', 'desk.png'))) };
  }
  const { meta, atlas } = deskCache;
  const W = 278;
  const out = new Uint8Array(W * W * 4);
  const ids = [f.desk]; // (the tail is round A's, drawn by desk_seat.js; checked in stage_desk_test)
  for (const id of ids) {
    const { rect, at } = meta.frames[id];
    for (let y = 0; y < rect[3]; y += 1) {
      for (let x = 0; x < rect[2]; x += 1) {
        const q = ((rect[1] + y) * atlas.width + rect[0] + x) * 4;
        if (atlas.data[q + 3] < 128) continue;
        const X = at[0] - 300 + x;
        const Y = at[1] - 54 + y;
        if (X < 0 || Y < 0 || X >= W || Y >= W) continue;
        out.set(atlas.data.subarray(q, q + 4), (Y * W + X) * 4);
      }
    }
  }
  return out;
}

test('parts: declared layer order, groups and hair slots; every drawn part has its layer', async () => {
  const { meta } = await rig();
  const ids = meta.layers.map((l) => l.id);
  assert.equal(new Set(ids).size, ids.length);
  // back to front, the contract the hair layer relies on
  const before = (a, b) => assert.ok(ids.indexOf(a) < ids.indexOf(b), `${a} before ${b}`);
  before('hair_back', 'turtleneck');
  before('neck', 'turtleneck');
  before('turtleneck', 'coat');
  before('coat', 'face');
  before('face', 'eyes');
  before('eyes', 'hair_front');
  before('hair_front', 'hand_r');
  before('hand_r', 'hair_tips');
  for (const id of ['hair_back', 'hair_front', 'hair_tips']) assert.equal(meta.layers.find((l) => l.id === id).kind, 'slot');
  for (const l of meta.layers) assert.ok(l.group in meta.groups, l.id);
  for (const [g, def] of Object.entries(meta.groups)) if (def.parent) assert.ok(def.parent in meta.groups, g);
  for (const k of Object.keys(meta.parts)) assert.ok(ids.includes(k), `part ${k} has a layer`);
});

// The hair layer over a picture of her (as hair_layer.js does at rest): front hair over her
// except where an arm is, back hair only where she is not drawn. mask: R her, G arm.
function hairOver(px, mask, W) {
  const meta = JSON.parse(fs.readFileSync(path.join(STAGE, 'assets', 'char', 'hair.json'), 'utf8'));
  const atlas = decodePng(fs.readFileSync(path.join(STAGE, 'assets', 'char', 'hair.png')));
  const out = Uint8ClampedArray.from(px);
  for (let y = 0; y < W; y += 1) {
    for (let x = 0; x < W; x += 1) {
      const i = (y * W + x) * 4;
      if (mask[i + 1] > 127) continue;
      for (const s of meta.segments) {
        if (s.z === 'back' && mask[i] > 127) continue;
        const sx = x - s.box[0];
        const sy = y - s.box[1];
        if (sx < 0 || sy < 0 || sx >= s.box[2] || sy >= s.box[3]) continue;
        const q = ((s.at[1] + sy) * atlas.width + s.at[0] + sx) * 4;
        if (atlas.data[q + 3] > 127) {
          out.set([atlas.data[q], atlas.data[q + 1], atlas.data[q + 2], 255], i);
          break;
        }
      }
    }
  }
  return out;
}

test('parts: at rest the layered character (with the hair layer) is the icon, except the redrawn face; hidden pixels are completed', async () => {
  const { make, meta } = await rig();
  const W = 278;
  const base = decodePng(fs.readFileSync(path.join(STAGE, 'assets', 'char', 'base.png')));
  const hm = decodePng(fs.readFileSync(path.join(STAGE, 'assets', 'char', 'hair_mask.png')));
  const icon = hairOver(base.data, hm.data, W); // the uncut sheet (see stage_hair_test)
  const r = make();
  const mine = hairOver(r.compose({}).pixels, r.mask, W);
  let same = 0;
  let total = 0;
  for (let y = 0; y < W; y += 1) {
    for (let x = 0; x < W; x += 1) {
      const inCircle = (x - 139) ** 2 + (y - 139) ** 2 <= 136 ** 2;
      const face = x >= 106 && x <= 172 && y >= 82 && y <= 130; // eyes, brows, mouth redrawn
      const i = (y * W + x) * 4;
      if (!inCircle || face || !icon[i + 3]) continue;
      total += 1;
      if (icon[i] === mine[i] && icon[i + 1] === mine[i + 1] && icon[i + 2] === mine[i + 2] && mine[i + 3]) same += 1;
    }
  }
  // (art unify 4, #341: the turtleneck's knit, the coat's folds and the mug hand's fingers are
  // repainted on purpose — about 2 % of the icon's pixels; was > 93 %, now 91.9 %)
  assert.ok(same / total > 0.91, `${((100 * same) / total).toFixed(1)} % of the icon's pixels unchanged`);
  const px = r.compose({}).pixels;
  void px;
  // the turtleneck goes on under the mug, the coat under the arm: with the arm away nothing is empty
  const away = r.compose({ armR: 'type1' }).pixels;
  let holes = 0;
  for (let y = 170; y < 230; y += 1) for (let x = 100; x < 145; x += 1) if (!away[(y * W + x) * 4 + 3]) holes += 1;
  assert.equal(holes, 0, 'no hole behind the mug');
  assert.ok(meta.parts.coat['base@free'] && meta.parts.turtleneck.base);
});

test('motion catalog: every built-in motion validates against the drawn parts and is frozen', async () => {
  const { map, rejected } = await catalog();
  const { MOTIONS, CONTRACT_IDS } = await load('motion_catalog.js');
  assert.deepEqual(rejected, []);
  assert.equal(map.size, MOTIONS.length);
  for (const id of CONTRACT_IDS) assert.ok(map.has(id), `contract id ${id}`);
  const { MOTION_IDS } = await load('gadget_catalog.js');
  for (const id of MOTION_IDS) assert.ok(map.has(id), `gadget motion ${id}`);
  for (const m of map.values()) {
    assert.ok(Object.isFrozen(m) && Object.isFrozen(m.tracks), m.id);
    if (m.then) assert.ok(map.has(m.then));
  }
  for (const id of ['wave', 'nod', 'shake_head', 'surprised', 'stretch', 'typing_back', 'watch_back']) assert.ok(map.has(id), id);
});

test('motion catalog: code, unknown parts, channels and bad values are refused', async () => {
  const { ids } = await rig();
  const { validateMotion, buildMotions } = await load('motion_catalog.js');
  const ok = { id: 'x', duration: 1, tracks: { sheet: [[0, 'tilt_r1']] } };
  assert.ok(validateMotion(ok, ids));
  const bad = [
    { ...ok, id: 'Bad Id' },
    { ...ok, duration: 'long' },
    { ...ok, tracks: { sheet: [[0, 'no_such_sheet']] } },
    { ...ok, tracks: { 'cam.x': [[0, 1]] } }, // a room channel is not a track
    { ...ok, tracks: { shake: [[0, 1]] } },
    { ...ok, tracks: { sheet: [[0, () => 'base']] } },
    { ...ok, tracks: { armL: [[0, '<img src=x onerror=alert(1)>']] } },
    { ...ok, tracks: { sheet: Array.from({ length: 41 }, (_, i) => [i / 50, 'base']) } },
    { ...ok, tracks: {} },
    { ...ok, then: 'Not valid' },
    { ...ok, group: 'Bad Group' },
    { ...ok, exit: { duration: 1, tracks: { mouth: [[0, 'scream']] } } },
    'typing',
    null,
  ];
  for (const b of bad) assert.equal(validateMotion(b, ids), null, JSON.stringify(b && b.tracks));
  const clamp = validateMotion({ ...ok, tracks: { earL: [[0, 99]] } }, ids);
  assert.equal(clamp.tracks.earL[0][1], 18);
  const { rejected } = buildMotions([{ ...ok, id: 'y', then: 'missing' }], ids);
  assert.deepEqual(rejected, ['y']);
  // nothing in the motion modules evaluates code or touches the DOM
  for (const f of ['motion_catalog.js', 'motion_model.js', 'motion.js', 'char_rig.js']) {
    const src = fs.readFileSync(path.join(STAGE, 'js', f), 'utf8');
    assert.doesNotMatch(src, /\beval\(|new Function|innerHTML|document\.|window\./, f);
  }
});

test('every motion is a multi-frame pixel animation, and no two motions look the same', async () => {
  const { map } = await catalog();
  const seqs = new Map();
  for (const m of map.values()) {
    const fr = await frames(m);
    const distinct = new Set(fr.map((f) => f.px));
    assert.ok(distinct.size >= 3, `${m.id}: ${distinct.size} distinct frames`);
    seqs.set(m.id, fr);
  }
  const ids = [...seqs.keys()];
  for (let i = 0; i < ids.length; i += 1) {
    for (let j = i + 1; j < ids.length; j += 1) {
      const A = seqs.get(ids[i]);
      const B = seqs.get(ids[j]);
      assert.notEqual(A.map((f) => f.px).join(), B.map((f) => f.px).join(), `${ids[i]} vs ${ids[j]}`);
      // at some moment the two differ by many pixels (not a 1-2 px offset)
      let best = 0;
      for (const k of [0.2, 0.4, 0.6, 0.8]) best = Math.max(best, diffCount(A[Math.floor(A.length * k)].pixels, B[Math.floor(B.length * k)].pixels));
      assert.ok(best >= 60, `${ids[i]} vs ${ids[j]}: ${best} px`);
    }
  }
});

test('faces: every eye shape, gaze, mouth and brow is its own drawing; half-closed and downcast differ clearly', async () => {
  const { make, ids } = await rig();
  const r = make();
  const px = (f) => Uint8Array.from(r.compose(f).pixels);
  const eyes = Object.fromEntries(ids.eyes.map((e) => [e, px({ eyes: e })]));
  for (const a of ids.eyes) for (const b of ids.eyes) if (a < b) assert.ok(diffCount(eyes[a], eyes[b]) >= 8, `${a} vs ${b}`);
  assert.ok(diffCount(eyes.half, eyes.down) >= 20, `half vs down: ${diffCount(eyes.half, eyes.down)} px`);
  assert.ok(ids.eyes.length >= 10 && ids.gaze.length >= 9 && ids.mouth.length >= 10 && ids.brows.length >= 5);
  assert.equal(new Set(ids.gaze.map((g) => hash(px({ gaze: g })))).size, ids.gaze.length, 'every gaze direction moves the irises');
  assert.equal(new Set(ids.mouth.map((m) => hash(px({ mouth: m })))).size, ids.mouth.length);
  assert.equal(new Set(ids.brows.map((b) => hash(px({ brows: b })))).size, ids.brows.length);
});

test('the rig copies whole pixels: no colour that is not in the atlas; breath lifts the upper body by 1 px', async () => {
  const { make, atlas } = await rig();
  const palette = new Set();
  for (let i = 0; i < atlas.data.length; i += 4) palette.add((atlas.data[i] << 16) | (atlas.data[i + 1] << 8) | atlas.data[i + 2]);
  const r = make();
  for (const f of [{ sheet: 'slump', eyes: 'down', armR: 'mug_sip', armL: 'chin', blush: 1, sweat: 1, fx: 'shock' }, { sheet: 'back' }, { sheet: 'turn' }]) {
    const p = r.compose(f).pixels;
    for (let i = 0; i < p.length; i += 4) if (p[i + 3]) assert.ok(palette.has((p[i] << 16) | (p[i + 1] << 8) | p[i + 2]), `new colour in ${f.sheet}`);
  }
  const a = Uint8Array.from(r.compose({ breath: 0 }).pixels);
  const b = Uint8Array.from(r.compose({ breath: 1 }).pixels);
  const W = 278;
  const row = (px, y) => px.subarray(y * W * 4, (y + 1) * W * 4).join();
  assert.equal(row(b, 100), row(a, 101)); // head: one pixel up
  const seg = (px, y) => px.subarray((y * W + 105) * 4, (y * W + 135) * 4).join();
  assert.equal(seg(b, 265), seg(a, 265)); // the turtleneck below the breath line: unchanged
  assert.equal(r.compose({ sheet: 'tilt_r2' }).head.tilt, 8);
  assert.equal(r.compose({ sheet: 'back' }).head.view, 'back');
});

// expected motion of each demo step (deterministic selection from the events of the step)
const EXPECT = {
  calm: 'none', thinking: 'ponder', tool: 'look_desk', memory: 'look_shelf', approval: 'await_mug', achievement: 'stretch',
  curious: 'peer', worried: 'alert', critical: 'surprised', coding: 'typing', 'tests-run': 'look_screen',
  'tests-pass': 'relief', 'tests-fail': 'slump', delegate: 'delegate_point', 'tool-fail': 'slump', approved: 'relief',
  denied: 'slump', recovered: 'relief', 'turn-done': 'sip', 'turn-error': 'slump', 'agent-back': 'receive_report', 'tests-done': 'smug', 'tests-elated': 'smug', 'tests-tired': 'sigh',
};

test('motion selection is deterministic: same state, same motion; every demo step picks its motion', async () => {
  const { initialState, reduce, derive } = await load('model.js');
  const { STEPS } = await load('director.js');
  const { GADGET_STEPS } = await load('demo_gadgets.js');
  const { MOTION_STEPS } = await load('demo_motions.js');
  const all = [...STEPS, ...GADGET_STEPS, ...MOTION_STEPS];
  const run = (evs) => evs.filter((e) => e.type !== 'anim').reduce(reduce, initialState());
  for (const step of all) {
    const got = derive(run(step.events)).pose.motion;
    if (EXPECT[step.id]) assert.equal(got, EXPECT[step.id], step.id);
    for (let i = 0; i < 3; i += 1) assert.equal(derive(structuredClone(run(step.events))).pose.motion, got);
  }
  const reached = new Set(all.map((s) => derive(run(s.events)).pose.motion));
  for (const id of ['typing', 'look_screen', 'delegate_point', 'receive_report', 'ponder', 'await_mug', 'peer', 'sip', 'alert', 'relief', 'slump', 'surprised', 'stretch', 'smug', 'sigh', 'look_desk', 'look_shelf']) assert.ok(reached.has(id), id);
  // the same state reached by another path selects the same motion
  const a = run([{ type: 'turn.start' }, { type: 'tool.start', id: 'e', kind: 'code', name: 'editor' }]);
  const b = run([{ type: 'breaker', level: 'WARNING' }, { type: 'breaker', level: 'NORMAL' }, N({ curiosity: 0.5 }), { type: 'turn.start' }, { type: 'tool.start', id: 'e', kind: 'code', name: 'editor' }]);
  assert.equal(derive(a).pose.motion, derive(b).pose.motion);
  const c = derive(run([N({}), { type: 'turn.start' }, { type: 'turn.end', ok: true }])).causes.find((x) => x.channel === 'motion');
  assert.match(c.cause, /#\d+ turn\.end/);
  // the breaker cue ends with the next work event
  let s = run([{ type: 'breaker', level: 'WARNING' }, { type: 'breaker', level: 'NORMAL' }]);
  assert.equal(derive(s).pose.motion, 'relief');
  s = reduce(s, { type: 'turn.start' });
  assert.equal(derive(s).pose.motion, 'ponder');
});

test('the room does not move with the character: motions drive only character parts', async () => {
  const { MotionPlayer } = await load('motion.js');
  const { roomFrame, ROOM_CHANNELS } = await load('room.js');
  const { ENUM_TRACKS, NUMBER_TRACKS } = await load('motion_catalog.js');
  const { map } = await catalog();
  for (const m of map.values()) for (const ch of [...Object.keys(m.tracks), ...Object.keys((m.exit && m.exit.tracks) || {})]) {
    assert.ok(ENUM_TRACKS.includes(ch) || ch in NUMBER_TRACKS, `${m.id}.${ch}`);
    assert.ok(!ROOM_CHANNELS.includes(ch));
  }
  const pose = { neon: 'cyan', neonLevel: 1, dim: 0, desat: 0, steam: 2, 'light.desk': 0, 'light.shelf': 0, 'light.window': 0, sparkle: 0, emotion: 'calm', eyes: 'open', mouth: 'neutral', headY: 0, ears: 0 };
  const ch = { 'body.breath': 1, 'steam.puff': 1.2 };
  const ref = JSON.stringify(roomFrame({ ...pose, motion: 'none' }, ch, 3.3, { x: 0.2, y: -0.1 }));
  for (const id of map.keys()) {
    const p = new MotionPlayer(map, 1);
    p.select(id, 'test');
    const before = JSON.stringify(ch);
    for (let t = 0; t < 3; t += 0.1) p.tick(t, { ...pose, motion: id }, { numeric: ch, enums: {}, gesture: false });
    assert.equal(JSON.stringify(ch), before, 'the player does not write channels');
    assert.equal(JSON.stringify(roomFrame({ ...pose, motion: id }, ch, 3.3, { x: 0.2, y: -0.1 })), ref, id);
  }
  // the bridge writes only the character layer's own texture, uniforms and mug anchor
  const bridge = fs.readFileSync(path.join(STAGE, 'js', 'char_bridge.js'), 'utf8');
  const writes = [...bridge.matchAll(/this\.scene\.([a-zA-Z]+)/g)].map((m) => m[1]);
  assert.deepEqual([...new Set(writes)].sort(), ['charMeta']);
  assert.doesNotMatch(bridge, /roomState|placeLayer|camera|parallax\s*=/);
  assert.doesNotMatch(fs.readFileSync(path.join(STAGE, 'js', 'motion_catalog.js'), 'utf8'), /cam\.[xy]/);
});

test('player: agent requests, groups, follow-on motions and exits', async () => {
  const { MotionPlayer } = await load('motion.js');
  const { map } = await catalog();
  const p = new MotionPlayer(map, 3);
  assert.equal(p.request('typing_back', 0, CALM).ok, false); // shows a real state
  assert.equal(p.request('dance', 0, CALM).ok, false);
  assert.equal(p.request('wave', 0, { ...CALM, emotion: 'worried' }).ok, false);
  assert.equal(p.request('wave', 0, CALM).ok, true);
  assert.equal(p.request('nod', 0.5, CALM).reason, 'cooldown');
  p.select('typing_back', 'code');
  p.tick(3, CALM);
  assert.equal(p.request('nod', 5, CALM).reason, 'a state motion is playing');
  // typing_back -> watch_back: she stays turned round (no exit, no second turn)
  p.select('watch_back', 'tests');
  assert.match(p.tick(3.05, CALM).frame.sheet, /^back/);
  // -> none: the exit: she turns at the desk chair and walks back to the front, mug in hand
  p.select('none', 'x');
  const seen = [];
  for (let t = 3.1; t < 4.6; t += 0.04) seen.push(p.tick(t, CALM).frame);
  assert.ok(seen.some((f) => f.desk === 'turn'));
  assert.ok(seen.some((f) => f.desk === 'back1'));
  assert.equal(seen.at(-1).desk, 'none');
  assert.notEqual(seen.at(-1).sheet, 'back');
  assert.equal(seen.at(-1).armR, 'mug');
  // surprised continues into alert
  const q = new MotionPlayer(map, 3);
  q.select('surprised', 'breaker.rise');
  q.tick(0, CALM);
  assert.equal(q.tick(2, CALM).info.id, 'alert');
  // an agent motion cannot change the face while worried
  const w = new MotionPlayer(map, 3);
  w.request('wave', 0, CALM);
  const f = w.tick(0.5, { ...CALM, emotion: 'worried', eyes: 'down', mouth: 'frown' }).frame;
  assert.equal(f.mouth, 'frown');
});

test('gadgets: the laptop moves to the room desk while she works with her back to us', async () => {
  const { GADGETS, buildGadgets, gadgetCondition } = await load('gadget_catalog.js');
  const { list, rejected } = buildGadgets(GADGETS);
  assert.deepEqual(rejected, []);
  const on = (motion) => list.filter((g) => g.show === 'gadget.laptop' && gadgetCondition(g, { motion })).map((g) => g.id).sort();
  assert.deepEqual(on('typing_back'), ['monitor', 'stacklight_back']);
  assert.deepEqual(on('watch_back'), ['monitor', 'stacklight_back']);
  assert.deepEqual(on('none'), ['laptop', 'stacklight']);
  const { validateGadget } = await load('gadget_catalog.js');
  const base = GADGETS.find((g) => g.id === 'monitor');
  assert.equal(validateGadget({ ...base, when: { emotion: ['calm'] } }), null);
  assert.equal(validateGadget({ ...base, when: { motion: 'typing_back' } }), null);
});

test('docs/stage.md motion tables list every motion and every rule', async () => {
  const { MOTIONS, MOTION_RULES } = await load('motion_catalog.js');
  const md = fs.readFileSync(path.join(__dirname, '..', 'docs', 'stage.md'), 'utf8');
  const block = md.split('<!-- stage-motions:begin -->')[1].split('<!-- stage-motions:end -->')[0];
  const rows = block.trim().split('\n').slice(2).map((r) => r.split('|').slice(1, -1).map((c) => c.trim().replace(/`/g, '')));
  assert.deepEqual(rows.map((r) => r[0]), MOTIONS.map((m) => m.id));
  const rules = md.split('<!-- stage-motion-rules:begin -->')[1].split('<!-- stage-motion-rules:end -->')[0];
  const rr = rules.trim().split('\n').slice(2).map((r) => r.split('|').slice(1, -1).map((c) => c.trim().replace(/`/g, '')));
  assert.deepEqual(rr.map((r) => r[1]), MOTION_RULES.map((r) => r.motion));
});
