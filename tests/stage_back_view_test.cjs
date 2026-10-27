// Stage back view v2 (#341): the front of the character is unchanged, the back view (hair locks
// on the hair layer's back set, the typing arm at the room keyboard, coat details, the turn
// with a profile) is deterministic and does not reach the room.
// node --test tests/stage_back_view_test.cjs
// STAGE_FRONT_FIXTURE=write node --test tests/stage_back_view_test.cjs   (re-record the front)
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const crypto = require('node:crypto');
const { decodePng } = require('./stage_png.cjs');

const STAGE = path.join(__dirname, '..', 'dashboard', 'stage');
const FIXTURE = path.join(__dirname, 'fixtures', 'stage_front_frames.json');
const load = (name) => import(pathToFileURL(path.join(STAGE, 'js', name)).href);
const readJson = (...p) => JSON.parse(fs.readFileSync(path.join(STAGE, ...p), 'utf8'));
const CALM = { emotion: 'calm', eyes: 'open', mouth: 'neutral', headY: 0, ears: 0, blush: 0, sweat: 0 };
const hash = (px) => crypto.createHash('sha1').update(px).digest('hex').slice(0, 16);

let cache = null;
async function rig() {
  if (cache) return cache;
  const { CharRig, NEUTRAL_FRAME, frameKey, partIds } = await load('char_rig.js');
  const meta = readJson('assets', 'char', 'parts.json');
  const atlas = decodePng(fs.readFileSync(path.join(STAGE, 'assets', 'char', 'parts.png')));
  // with round A's extras (ear variants, tail), as the browser loads them (char_bridge.js)
  const extras = { meta: readJson('assets', 'char', 'extras.json'), atlas: decodePng(fs.readFileSync(path.join(STAGE, 'assets', 'char', 'extras.png'))) };
  // round A's pose arms (cross, ponder_cross) as the page loads them
  const poses = { meta: readJson('assets', 'char', 'poses.json'), atlas: decodePng(fs.readFileSync(path.join(STAGE, 'assets', 'char', 'poses.png'))) };
  cache = { CharRig, NEUTRAL_FRAME, frameKey, meta, atlas, extras, poses, ids: partIds(meta), make: () => new CharRig(meta, atlas, extras, null, poses) };
  return cache;
}

// Every frame the motions reach (all tracks, intro, loop and exit, 1/30 s), plus every face
// sprite on the rest pose: { frameKey: frame }.
async function reachedFrames() {
  const { NEUTRAL_FRAME, frameKey, ids, meta } = await rig();
  const { buildMotions, MOTIONS } = await load('motion_catalog.js');
  const { MotionPlayer } = await load('motion.js');
  const { map } = buildMotions(MOTIONS, ids);
  const p = new MotionPlayer(new Map());
  const out = new Map();
  const add = (f) => out.set(frameKey(f), { ...f });
  for (const m of map.values()) {
    for (const [tracks, dur] of [[m.tracks, m.duration], ...(m.exit ? [[m.exit.tracks, m.exit.duration]] : [])]) {
      for (let t = 0; t <= dur + 1e-9; t += 1 / 30) {
        const f = { ...NEUTRAL_FRAME };
        p.apply(f, tracks, t, false, CALM);
        add(f);
      }
    }
  }
  for (const eyes of Object.keys(meta.eyes)) for (const gaze of Object.keys(meta.eyes[eyes])) add({ ...NEUTRAL_FRAME, eyes, gaze });
  for (const mouth of Object.keys(meta.mouths)) add({ ...NEUTRAL_FRAME, mouth });
  for (const brows of Object.keys(meta.brows)) add({ ...NEUTRAL_FRAME, brows });
  return out;
}

test('the front of the character is unchanged: every front frame the motions reach is the same picture as before', async () => {
  const { make, meta } = await rig();
  const r = make();
  const frames = await reachedFrames();
  const front = {};
  for (const [key, f] of frames) {
    const sheet = meta.sheets[f.sheet] || meta.sheets.base;
    if (sheet.view !== 'front') continue;
    front[key] = hash(r.compose(f).pixels);
  }
  if (process.env.STAGE_FRONT_FIXTURE === 'write') {
    fs.mkdirSync(path.dirname(FIXTURE), { recursive: true });
    fs.writeFileSync(FIXTURE, `${JSON.stringify(front, null, 0).replace(/,"/g, ',\n"')}\n`);
  }
  const before = JSON.parse(fs.readFileSync(FIXTURE, 'utf8'));
  const keys = Object.keys(before);
  assert.ok(keys.length > 300, `${keys.length} front frames recorded`);
  const changed = keys.filter((k) => front[k] !== undefined && front[k] !== before[k]);
  const missing = keys.filter((k) => front[k] === undefined);
  assert.deepEqual(changed, [], 'front frames that look different');
  // a front frame no motion reaches any more is fine only for the back-view motions' turn
  for (const k of missing) assert.match(k, /^(yaw_r|base)\|/, `front frame no longer reached: ${k}`);
});

// --- back view ---------------------------------------------------------------------------------

const BACK = readJson('assets', 'char', 'hair_back.json');
const decodeAsset = (...p) => decodePng(fs.readFileSync(path.join(STAGE, ...p)));

test('back hair: the locks are the hair layer\'s back set (hair.json schema), parented, tuned in HAIR_RIG', async () => {
  const { createHairRig, HAIR_RIG } = await load('hair_rig.js');
  const { HAIR_VIEWS } = await load('hair_layer.js');
  assert.equal(HAIR_VIEWS.back, 'hair_back');
  assert.equal(BACK.view, 'back');
  const rig = createHairRig(BACK);
  assert.deepEqual(rig.rejected, [], 'every segment validates');
  assert.ok(BACK.segments.length >= 12 && BACK.segments.length <= 24, `${BACK.segments.length} segments (shader uniform budget like the front set)`);
  const chains = new Set(BACK.segments.map((s) => s.chain));
  assert.ok(chains.size >= 5, 'several locks, not one curtain');
  for (const p of BACK.parts) assert.ok(HAIR_RIG[p.id], `tuning for ${p.id}`);
  for (const s of BACK.segments) {
    assert.ok(s.parent === 'head' || BACK.segments.some((t) => t.id === s.parent), `${s.id} parent`);
    assert.ok(s.at[0] + s.box[2] <= BACK.atlas[0] && s.at[1] + s.box[3] <= BACK.atlas[1], `${s.id} inside the atlas`);
  }
  // the tips lag and sway a little: deeper segments follow less and may swing more
  for (const id of ['locks', 'locks_side']) {
    const t = HAIR_RIG[id];
    assert.ok(t.delay[t.delay.length - 1] > t.delay[0] && t.sway[t.sway.length - 1] > 0);
    assert.ok(Math.max(...t.maxAmp) <= 2, 'only a slight swing');
  }
});

// The character layer's back sheet with the hair layer's back segments over it (as
// hair_layer.js draws them at rest: first segment with a pixel wins; no arm in the way).
function backComposite(sheetPx, mask, W) {
  const atlas = decodeAsset('assets', 'char', 'hair_back.png');
  const out = Uint8ClampedArray.from(sheetPx);
  for (let y = 0; y < W; y += 1) {
    for (let x = 0; x < W; x += 1) {
      const i = (y * W + x) * 4;
      if (mask[i + 1] > 127) continue;
      for (const s of BACK.segments) {
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

test('back hair: at rest the sheet + the hair layer\'s locks are the uncut back view, pixel for pixel; no hole when they swing', async () => {
  const { CharRig, NEUTRAL_FRAME, meta, atlas } = await rig();
  // without the mug set down on the desk (a prop, not part of the back sheet)
  const noMug = { ...meta, props: { ...meta.props, mug_desk: { ...meta.props.mug_desk, w: 0, h: 0 } } };
  const r = new CharRig(noMug, atlas);
  const W = meta.size[0];
  const res = r.compose({ ...NEUTRAL_FRAME, sheet: 'back' });
  const out = backComposite(res.pixels, r.mask, W);
  const buf = Buffer.alloc(W * W * 4);
  for (let i = 0; i < W * W; i += 1) {
    const a = out[i * 4 + 3] > 127 ? 1 : 0;
    buf[i * 4] = a;
    for (let k = 0; k < 3; k += 1) buf[i * 4 + 1 + k] = a ? out[i * 4 + k] : 0;
  }
  assert.equal(crypto.createHash('sha256').update(buf).digest('hex'), BACK.restRgbHash);
  // under every hanging lock the sheet has a pixel (the coat, or the dark of the hair): a swing
  // of the tips never shows the room through her
  let holes = 0;
  for (const s of BACK.segments) {
    for (let y = s.box[1]; y < s.box[1] + s.box[3]; y += 1) {
      for (let x = s.box[0]; x < s.box[0] + s.box[2]; x += 1) {
        const inner = [[-2, 0], [2, 0], [0, -2], [0, 2]].every(([dx, dy]) => out[((y + dy) * W + x + dx) * 4 + 3] > 0);
        if (inner && out[(y * W + x) * 4 + 3] && !res.pixels[(y * W + x) * 4 + 3]) holes += 1;
      }
    }
  }
  assert.equal(holes, 0, 'pixels inside the figure that only the hair layer covers');
});

test('back arm: the right hand reaches the room laptop\'s keys (wide and narrow layout); typing moves wrist, fingers and elbow', async () => {
  const { make, NEUTRAL_FRAME, meta } = await rig();
  const gadgets = readJson('assets', 'gadgets', 'gadgets.json');
  const fx = decodeAsset('assets', 'gadgets', 'gadgets_fx.png');
  const { GADGETS } = await load('gadget_catalog.js');
  const mon = GADGETS.find((g) => g.id === 'monitor');
  const [lx, ly, lw, lh] = gadgets.sprites[mon.sprite];
  let k0 = [Infinity, Infinity, -Infinity, -Infinity];
  for (let y = 0; y < lh; y += 1) for (let x = 0; x < lw; x += 1) {
    if (fx.data[((ly + y) * fx.width + lx + x) * 4 + 2] !== gadgets.regions.keys) continue;
    k0 = [Math.min(k0[0], x), Math.min(k0[1], y), Math.max(k0[2], x), Math.max(k0[3], y)];
  }
  const CHAR = [181, 20]; // scene.js CHAR_POS: the character sheet in world pixels
  const r = make();
  const handLayer = r.order.indexOf('hand_r') + 1;
  const tips = {};
  for (const name of Object.keys(meta.arms.r).filter((n) => n.startsWith('back_'))) {
    const res = r.compose({ ...NEUTRAL_FRAME, sheet: 'back', armR: name });
    // the hand: skin-coloured pixels of the arm sprite right of the shoulder
    let box = [Infinity, Infinity, -Infinity, -Infinity];
    const W = meta.size[0];
    for (let i = 0; i < r.layer.length; i += 1) {
      if (r.layer[i] !== handLayer) continue;
      const [cR, cG, cB] = [res.pixels[i * 4], res.pixels[i * 4 + 1], res.pixels[i * 4 + 2]];
      if (!(cR > cB + 20 && cR > 150)) continue; // skin (warm), not the white sleeve or the navy cuff
      const x = i % W;
      const y = Math.floor(i / W);
      box = [Math.min(box[0], x), Math.min(box[1], y), Math.max(box[2], x), Math.max(box[3], y)];
    }
    assert.ok(box[0] < Infinity, `${name}: a hand`);
    tips[name] = box;
    for (const at of [mon.at, mon.narrow.at]) {
      const keys = [at[0] + k0[0] - CHAR[0], at[1] + k0[1] - CHAR[1], at[0] + k0[2] - CHAR[0], at[1] + k0[3] - CHAR[1]];
      const overlap = Math.min(box[2], keys[2]) - Math.max(box[0], keys[0]);
      const vert = Math.min(box[3], keys[3]) - Math.max(box[1], keys[1]);
      assert.ok(overlap >= 4 && vert >= 2, `${name}: hand ${box} on the keys ${keys} (laptop at ${at})`);
    }
  }
  const frames = Object.keys(tips);
  assert.ok(frames.length >= 4, 'typing frames');
  const pix = frames.map((n) => Uint8Array.from(r.compose({ ...NEUTRAL_FRAME, sheet: 'back', armR: n }).pixels));
  for (let a = 0; a < pix.length; a += 1) for (let b = a + 1; b < pix.length; b += 1) {
    let d = 0;
    for (let i = 0; i < pix[a].length; i += 4) if (pix[a][i] !== pix[b][i] || pix[a][i + 3] !== pix[b][i + 3]) d += 1;
    assert.ok(d >= 6, `${frames[a]} vs ${frames[b]}: ${d} px`);
  }
});

test('the turn has in-betweens and a side profile; she walks to the room desk and back (desk shot)', async () => {
  const { meta } = await rig();
  const { MOTIONS } = await load('motion_catalog.js');
  assert.equal(meta.sheets.profile.view, 'profile');
  for (const id of ['typing_back', 'watch_back']) {
    const m = MOTIONS.find((x) => x.id === id);
    const seq = m.tracks.sheet.map((k) => k[1]);
    assert.deepEqual(seq, ['base', 'yaw_r', 'profile', 'turn1', 'turn', 'back'], `${id}: front -> 3/4 front -> profile -> 3/4 back -> back`);
    // each in-between is held 0.08-0.12 s (5-7 frames): readable, not a pop
    const ts = m.tracks.sheet.map((k) => k[0]);
    for (let i = 1; i < seq.length; i += 1) assert.ok(ts[i] - ts[i - 1] >= 0.08 && ts[i] - ts[i - 1] <= 0.25);
    // then she walks away to the room desk (smaller with every step) and sits there
    const desk = m.tracks.desk.map((k) => k[1]);
    assert.deepEqual(desk.slice(0, 10), ['none', 'walk1', 'walk2', 'walk3', 'walk4', 'walk5', 'walk6', 'walk7', 'stand', 'sit'], `${id}: walks to the chair and sits`);
    // even steps (pixel-art timing: 4-6 frames at 60 fps each), no long hold on the way
    const wt = m.tracks.desk.slice(1, 9).map((k) => k[0]);
    for (let i = 1; i < wt.length; i += 1) assert.ok(Math.abs(wt[i] - wt[i - 1] - 0.08) < 1e-9, `${id}: step ${i}`);
    assert.ok(m.tracks.desk[1][0] > ts.at(-1), `${id}: after the turn`);
    // and back: she turns at the chair and walks toward us, the front layer comes back last
    assert.deepEqual(m.exit.tracks.desk.map((k) => k[1]), ['stand', 'turn', 'back7', 'back6', 'back5', 'back4', 'back3', 'back2', 'back1', 'none'], `${id}: back again`);
    assert.deepEqual(m.exit.tracks.armR, [[0, 'mug']]);
  }
  // the profile and the 3/4 views are their own drawings (distinct from each other and the back)
  const { make, NEUTRAL_FRAME } = await rig();
  const r = make();
  const hs = ['yaw_r', 'profile', 'turn1', 'turn', 'back'].map((sheet) => hash(r.compose({ ...NEUTRAL_FRAME, sheet }).pixels));
  assert.equal(new Set(hs).size, hs.length);
});

test('back view is deterministic and leaves the room alone; no steam while she is turned away', async () => {
  const { make, NEUTRAL_FRAME } = await rig();
  const { createHairRig, simulateHair } = await load('hair_rig.js');
  const a = make();
  const b = make();
  for (const sheet of ['back', 'back_l', 'back_r', 'back_bob', 'back_tilt', 'turn', 'turn1', 'profile']) {
    for (const armR of ['back_type1', 'back_type3', 'mug_down']) {
      const f = { ...NEUTRAL_FRAME, sheet, armR };
      assert.equal(hash(a.compose(f).pixels), hash(b.compose(f).pixels));
    }
  }
  const rigB = createHairRig(BACK);
  const seq = Array.from({ length: 240 }, (_, i) => ({ head: [i % 40 < 20 ? -2 : 0, i % 30 < 15 ? 1 : 0], breath: i % 120 < 60 ? -1 : 0, sway: Math.sin(i / 20), pose: CALM }));
  assert.deepEqual(simulateHair(rigB, seq), simulateHair(rigB, seq), 'same head poses, same hair');
  const offs = simulateHair(rigB, seq);
  assert.ok(new Set(offs.map((o) => JSON.stringify(o))).size > 4, 'the tips move');
  // main.js: no steam (and fast fading of what rose) unless she faces us with the mug in hand
  const main = fs.readFileSync(path.join(STAGE, 'js', 'main.js'), 'utf8');
  assert.match(main, /charLayer\.head\.view !== 'front'/);
  assert.match(main, /steam: 0/);
  assert.match(fs.readFileSync(path.join(STAGE, 'js', 'scene.js'), 'utf8'), /this\.steamFade \|\| 1/);
});
