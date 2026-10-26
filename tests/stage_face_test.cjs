// Stage face (#341, the left eye): both eyes read in every eye shape x gaze (in front of the
// bangs), no fill artefacts around the eyes, and nothing outside the face changes.
// node --test tests/stage_face_test.cjs
// Re-record the outside-face fixture from another checkout (e.g. main before a face change):
//   STAGE_ROOT=/path/to/checkout STAGE_FACE_FIXTURE=write node --test tests/stage_face_test.cjs
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { decodePng } = require('./stage_png.cjs');

const ROOT = process.env.STAGE_ROOT || path.join(__dirname, '..');
const STAGE = path.join(ROOT, 'dashboard', 'stage');
const FIXTURE = path.join(__dirname, 'fixtures', 'stage_face_outside.json');
const load = (name) => import(path.join(STAGE, 'js', name));
const readJson = (...p) => JSON.parse(fs.readFileSync(path.join(STAGE, ...p), 'utf8'));
const CALM = { emotion: 'calm', eyes: 'open', mouth: 'neutral', headY: 0, ears: 0, blush: 0, sweat: 0 };
const W = 278;

let cache = null;
async function rig() {
  if (cache) return cache;
  const R = await load('char_rig.js');
  const meta = readJson('assets', 'char', 'parts.json');
  const atlas = decodePng(fs.readFileSync(path.join(STAGE, 'assets', 'char', 'parts.png')));
  cache = { ...R, meta, atlas, ids: R.partIds(meta), make: () => new R.CharRig(meta, atlas) };
  return cache;
}

// The face region of a pose: a rectangle from that pose's eye and mouth anchors (the eyes, the
// brows, the bangs over them, the forehead under the bangs, the cheeks, the mouth and the chin).
function faceBox(meta, sheet) {
  const A = (meta.sheets[sheet] || meta.sheets.base).anchors;
  return [Math.floor(A.eye_l[0] - 20), Math.floor(A.eye_l[1] - 38), Math.ceil(A.eye_r[0] + 26), Math.ceil(A.mouth[1] + 26)];
}

// Every front frame the motions reach, plus every face sprite on the rest pose.
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
  for (const key of Object.keys(meta.eyes.l)) {
    const [eyes, gaze] = key.split('.');
    add({ ...NEUTRAL_FRAME, eyes, gaze });
  }
  for (const mouth of Object.keys(meta.mouths)) add({ ...NEUTRAL_FRAME, mouth });
  for (const brows of Object.keys(meta.brows.l)) add({ ...NEUTRAL_FRAME, brows });
  return [...out.entries()].filter(([, f]) => (meta.sheets[f.sheet] || meta.sheets.base).view === 'front');
}

test('outside the face region every front frame is the same picture as before (character and hair mask)', async () => {
  const { make, meta } = await rig();
  const r = make();
  const got = {};
  for (const [key, f] of await reachedFrames()) {
    const res = r.compose(f);
    const [x0, y0, x1, y1] = faceBox(meta, f.sheet);
    const h = crypto.createHash('sha1');
    for (let y = 0; y < W; y += 1) {
      for (let x = 0; x < W; x += 1) {
        if (x >= x0 && x <= x1 && y >= y0 && y <= y1) continue;
        const i = (y * W + x) * 4;
        h.update(res.pixels.subarray(i, i + 4));
        h.update(r.mask.subarray(i, i + 4));
      }
    }
    got[key] = h.digest('hex').slice(0, 16);
  }
  if (process.env.STAGE_FACE_FIXTURE === 'write') {
    fs.writeFileSync(FIXTURE, `${JSON.stringify(got).replace(/,"/g, ',\n"')}\n`);
  }
  const before = JSON.parse(fs.readFileSync(FIXTURE, 'utf8'));
  assert.ok(Object.keys(before).length > 300);
  const changed = Object.keys(before).filter((k) => got[k] !== undefined && got[k] !== before[k]);
  assert.deepEqual(changed, [], 'frames that changed outside the face');
});

// The character with the front hair layer at rest over it (as hair_layer.js draws it: first
// segment with a pixel wins, never over mask G = arm, mug, and the overHair layers).
async function withHair(r, f) {
  const { meta, poseDisplacement } = await rig();
  const H = readJson('assets', 'char', 'hair.json');
  const HA = decodePng(fs.readFileSync(path.join(STAGE, 'assets', 'char', 'hair.png')));
  const res = r.compose(f);
  const out = Uint8ClampedArray.from(res.pixels);
  const hairAt = new Uint8Array(W * W);
  const pose = meta.sheets[f.sheet] || meta.sheets.base;
  const offs = H.segments.map((s) => {
    const d = poseDisplacement(pose, s.pivot[0], s.pivot[1]);
    return [Math.round(d[0]), Math.round(d[1]) - (f.breath ? 1 : 0)];
  });
  for (let y = 0; y < W; y += 1) {
    for (let x = 0; x < W; x += 1) {
      const i = (y * W + x) * 4;
      if (r.mask[i + 1] > 127) continue;
      for (let k = 0; k < H.segments.length; k += 1) {
        const s = H.segments[k];
        if (s.z === 'back' && r.mask[i] > 127) continue;
        const sx = x - offs[k][0] - s.box[0];
        const sy = y - offs[k][1] - s.box[1];
        if (sx < 0 || sy < 0 || sx >= s.box[2] || sy >= s.box[3]) continue;
        const q = ((s.at[1] + sy) * HA.width + s.at[0] + sx) * 4;
        if (HA.data[q + 3] > 127) {
          out.set([HA.data[q], HA.data[q + 1], HA.data[q + 2], 255], i);
          hairAt[y * W + x] = 1;
          break;
        }
      }
    }
  }
  return { out, hairAt, layer: r.layer, order: r.order };
}

const hexrgb = (h) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16));
const IRIS = ['#34507f', '#5a86bb', '#a9cbe9'].map(hexrgb); // build_char.RAMPS.eye without the darkest

test('every eye shape x gaze: both eyes are drawn in front of the bangs and read alike, in every head pose', async () => {
  const { make, meta, NEUTRAL_FRAME } = await rig();
  const r = make();
  const eyesLayer = r.order.indexOf('eyes') + 1;
  const keys = Object.keys(meta.eyes.l);
  const shapes = new Set(keys.map((k) => k.split('.')[0]));
  assert.equal(shapes.size, 17, "seventeen eye shapes (#359's ten + art unify 3's seven, sheet 05)");
  assert.ok(keys.filter((k) => k.startsWith('open.')).length === 9, 'nine gazes');
  const bad = [];
  for (const sheet of ['base', 'tilt_l2', 'tilt_r2', 'yaw_l', 'yaw_r', 'down2', 'up1', 'ponder', 'sip', 'lift', 'slump', 'relax']) {
    if (!meta.sheets[sheet]) continue;
    const A = meta.sheets[sheet].anchors;
    for (const key of keys) {
      const [eyes, gaze] = key.split('.');
      for (const breath of [0, 1]) {
        const f = { ...NEUTRAL_FRAME, sheet, eyes, gaze, breath };
        const { out, hairAt, layer } = await withHair(r, f);
        const count = { l: [0, 0, 0], r: [0, 0, 0] }; // eye pixels drawn, covered by hair, iris pixels
        for (const side of ['l', 'r']) {
          const ax = Math.round(A[`eye_${side}`][0]);
          const ay = Math.round(A[`eye_${side}`][1]) - breath;
          for (let y = ay - 9; y <= ay + 9; y += 1) {
            for (let x = ax - 14; x <= ax + 14; x += 1) {
              const j = y * W + x;
              if (layer[j] !== eyesLayer) continue;
              count[side][0] += 1;
              if (hairAt[j]) count[side][1] += 1;
              const c = [out[j * 4], out[j * 4 + 1], out[j * 4 + 2]];
              if (IRIS.some((q) => Math.abs(q[0] - c[0]) + Math.abs(q[1] - c[1]) + Math.abs(q[2] - c[2]) < 8)) count[side][2] += 1;
            }
          }
        }
        const why = [];
        for (const side of ['l', 'r']) if (count[side][1] > 0) why.push(`${side} eye covered by hair at ${count[side][1]} px`);
        if (count.l[0] < 0.55 * count.r[0]) why.push(`left eye ${count.l[0]} px vs right ${count.r[0]}`);
        if (count.r[2] >= 4 && count.l[2] < 0.4 * count.r[2]) why.push(`left iris ${count.l[2]} px vs right ${count.r[2]}`);
        if (why.length) bad.push(`${sheet} ${key} br${breath}: ${why.join(', ')}`);
      }
    }
  }
  assert.deepEqual(bad, []);
});

test('around the left eye the face has no fill artefacts: no iris-blue outside the eyes, no brown strand specks', async () => {
  const { meta, atlas } = await rig();
  const skinLine = hexrgb('#5c2f3d'); // build_char.LINE.skin
  const eyeBlue = ['#1b2645', '#34507f', '#5a86bb', '#a9cbe9'].map(hexrgb);
  const near = (c, q, d = 6) => Math.abs(c[0] - q[0]) + Math.abs(c[1] - q[1]) + Math.abs(c[2] - q[2]) < d;
  const problems = [];
  for (const [sheet, part] of Object.entries(meta.parts.face)) {
    if ((meta.sheets[sheet] || {}).view !== 'front') continue;
    const A = meta.sheets[sheet].anchors;
    const ex = A.eye_l[0];
    const ey = A.eye_l[1];
    let blue = 0;
    let brown = 0;
    for (let y = 0; y < part.h; y += 1) {
      for (let x = 0; x < part.w; x += 1) {
        const q = ((part.ay + y) * atlas.width + part.ax + x) * 4;
        if (atlas.data[q + 3] < 128) continue;
        const c = [atlas.data[q], atlas.data[q + 1], atlas.data[q + 2]];
        const sx = part.ox + x;
        const sy = part.oy + y;
        const dx = sx - ex;
        const dy = sy - ey;
        // the left eye's surroundings (left of the nose), outside the eye itself
        if (dx < -16 || dx > 19 || dy < -14 || dy > 12) continue;
        const inEye = (dx / 10.5) ** 2 + (dy / 6.2) ** 2 <= 1;
        if (!inEye && eyeBlue.some((b) => near(c, b, 4))) blue += 1;
        if (!inEye && near(c, skinLine)) brown += 1;
      }
    }
    if (blue || brown) problems.push(`${sheet}: ${blue} blue, ${brown} brown`);
  }
  assert.deepEqual(problems, []);
});

// The face sprites: #359's drawings, the eyes redrawn by art unify 4 (#341: the iris toward
// sheets 03 / 05 — a dark ring, a light bottom, a 2 x 2 pupil, a two-pixel highlight, the lid's
// shadow — for every shape alike, the old and the new); the other passes leave every sprite as
// it is. Re-record only for a face change:
//   STAGE_ROOT=/path/to/checkout STAGE_FACE_SPRITES=write node --test tests/stage_face_test.cjs
const SPRITES = path.join(__dirname, 'fixtures', 'stage_face_sprites.json');
test('the eye, brow and mouth sprites are unchanged (each sprite: placement and pixels)', async () => {
  const { meta, atlas } = await rig();
  const got = {};
  const put = (name, p) => {
    const h = crypto.createHash('sha1').update(`${p.ox},${p.oy},${p.w},${p.h}|`);
    for (let y = 0; y < p.h; y += 1) {
      const q = ((p.ay + y) * atlas.width + p.ax) * 4;
      h.update(atlas.data.subarray(q, q + p.w * 4));
    }
    got[name] = h.digest('hex').slice(0, 16);
  };
  for (const side of ['l', 'r']) {
    for (const [k, p] of Object.entries(meta.eyes[side])) put(`eye.${side}.${k}`, p);
    for (const [k, p] of Object.entries(meta.brows[side])) put(`brow.${side}.${k}`, p);
  }
  for (const [k, p] of Object.entries(meta.mouths)) put(`mouth.${k}`, p);
  if (process.env.STAGE_FACE_SPRITES === 'write') fs.writeFileSync(SPRITES, `${JSON.stringify(got).replace(/,"/g, ',\n"')}\n`);
  const before = JSON.parse(fs.readFileSync(SPRITES, 'utf8'));
  assert.ok(Object.keys(before).length > 100, `${Object.keys(before).length} sprites recorded`);
  // every recorded sprite is as it was; art unify 3 (#341, sheet 05) only adds sprites, by name
  const kept = Object.fromEntries(Object.keys(before).map((k) => [k, got[k]]));
  assert.deepEqual(kept, before);
  // names: #359's and art unify 3's (sheet 05), nothing else
  const want = new Set(Object.keys(before));
  for (const side of ['l', 'r']) {
    for (const s of NEW_EYES) for (const g of ['c', 'l', 'r', 'u', 'd', 'ul', 'ur', 'dl', 'dr']) want.add(`eye.${side}.${s}.${g}`);
  }
  for (const m of NEW_MOUTHS) want.add(`mouth.${m}`);
  assert.deepEqual(Object.keys(got).filter((k) => !want.has(k)), []);
});

// art unify 3 (#341): the eye and mouth sprites added after the expression sheet (05)
const NEW_EYES = ['jito', 'teary', 'scheme', 'shy', 'arc', 'puzzled', 'panic'];
const NEW_MOUTHS = ['small_open', 'mutto', 'smirk', 'hen', 'gasp', 'laugh', 'niko']; // (+ art unify 5)

test('every new eye shape: the two eyes alike (aperture, iris, highlights) at every gaze; the gaze moves both irises the same way', async () => {
  const { meta, atlas } = await rig();
  const iris = new Set(['#1b2645', '#34507f', '#5a86bb', '#a9cbe9'].map((h) => hexrgb(h).join()));
  const stats = (p) => {
    let rows = new Set();
    let n = 0;
    let sx = 0;
    let white = 0;
    for (let y = 0; y < p.h; y += 1) {
      for (let x = 0; x < p.w; x += 1) {
        const q = ((p.ay + y) * atlas.width + p.ax + x) * 4;
        if (!atlas.data[q + 3]) continue;
        const c = [atlas.data[q], atlas.data[q + 1], atlas.data[q + 2]].join();
        if (iris.has(c)) {
          n += 1;
          sx += p.ox + x;
          rows.add(y);
        }
        if (c === '255,255,255') white += 1;
      }
    }
    return { n, cx: n ? sx / n : 0, rows: rows.size, white };
  };
  const bad = [];
  for (const s of NEW_EYES) {
    for (const g of ['c', 'l', 'r', 'u', 'd', 'ul', 'ur', 'dl', 'dr']) {
      const l = stats(meta.eyes.l[`${s}.${g}`]);
      const r = stats(meta.eyes.r[`${s}.${g}`]);
      // (a sideways and downward gaze takes one iris toward the inner corner, the other toward
      // the outer one, where the lids differ: two rows apart at most)
      if (Math.abs(l.rows - r.rows) > (g.length > 1 ? 2 : 1)) bad.push(`${s}.${g}: iris rows ${l.rows} / ${r.rows}`);
      if (l.n < 0.5 * r.n || r.n < 0.5 * l.n) bad.push(`${s}.${g}: iris ${l.n} / ${r.n} px`);
      if ((l.white > 0) !== (r.white > 0)) bad.push(`${s}.${g}: a highlight in one eye only`);
    }
    // a gaze to the side moves both irises that way (relative to their own centre gaze)
    const c = { l: stats(meta.eyes.l[`${s}.c`]).cx, r: stats(meta.eyes.r[`${s}.c`]).cx };
    for (const [g, dir] of [['l', -1], ['r', 1]]) {
      for (const side of ['l', 'r']) {
        const dx = stats(meta.eyes[side][`${s}.${g}`]).cx - c[side];
        if (s !== 'scheme' && s !== 'shy' && dx * dir <= 0) bad.push(`${s}.${g}: the ${side} iris moved ${dx.toFixed(2)}`);
      }
    }
  }
  assert.deepEqual(bad, []);
});

test('every combination of eye shape, brow and mouth composes on every front sheet with the eyes and brows over the bangs', async () => {
  const { make, meta, NEUTRAL_FRAME } = await rig();
  const r = make();
  const eyesLayer = r.order.indexOf('eyes') + 1;
  const browsLayer = r.order.indexOf('brows') + 1;
  const mouthLayer = r.order.indexOf('mouth') + 1;
  const shapes = [...new Set(Object.keys(meta.eyes.l).map((k) => k.split('.')[0]))];
  const bad = [];
  let n = 0;
  // every combination on the rest sheet; the new eyes and mouths also in a tilt, a turn, a slump
  for (const sheet of ['base', 'tilt_r2', 'yaw_l', 'slump']) {
    for (const eyes of sheet === 'base' ? shapes : NEW_EYES) {
      for (const brows of Object.keys(meta.brows.l)) {
        for (const mouth of sheet === 'base' ? Object.keys(meta.mouths) : NEW_MOUTHS) {
          const f = { ...NEUTRAL_FRAME, sheet, eyes, brows, mouth };
          const { hairAt, layer } = await withHair(r, f);
          const seen = { e: 0, b: 0, m: 0, hid: 0 };
          for (let j = 0; j < layer.length; j += 1) {
            const l = layer[j];
            if (l === eyesLayer || l === browsLayer) {
              if (l === eyesLayer) seen.e += 1;
              else seen.b += 1;
              if (hairAt[j]) seen.hid += 1;
            } else if (l === mouthLayer) seen.m += 1;
          }
          n += 1;
          if (!seen.e || !seen.b || !seen.m || seen.hid) bad.push(`${sheet} ${eyes}/${brows}/${mouth}: ${JSON.stringify(seen)}`);
        }
      }
    }
  }
  assert.ok(n > 2000, `${n} combinations`);
  assert.deepEqual(bad.slice(0, 10), []);
});

// --- the cheeks and the jaw in a head pose (#341, 2026-10-18: "the cheek is shaved in some
// expressions" — the thinking pose, the web, the approval tilt) -----------------------------
// The hair layer as the browser draws it at rest for a pose (hair_layer.js update() with
// char_bridge.hairInput()): the rig's offsets for the head at the crown, then each chain moved
// by the displacement at its root's pivot (rootPivot), whole pixels; a back segment only where
// she is not drawn, no segment over mask G unless it is overArm; the first segment with a pixel
// wins. Returns the picture and, per pixel, the index + 1 of the segment drawn there.
async function liveHair(r, f) {
  const { meta, poseDisplacement } = await rig();
  const HR = await load('hair_rig.js');
  const H = readJson('assets', 'char', 'hair.json');
  const HA = decodePng(fs.readFileSync(path.join(STAGE, 'assets', 'char', 'hair.png')));
  const hr = HR.createHairRig(H);
  const pose = meta.sheets[f.sheet] || meta.sheets.base;
  const crown = poseDisplacement(pose, 139, 60);
  const head = [Math.round(crown[0]), Math.round(crown[1])];
  const input = { head, breath: -(f.breath ? 1 : 0), sway: 0, style: 'calm', extra: {} };
  let st = HR.initHairState(hr, input);
  for (let k = 0; k < 30; k += 1) st = HR.stepHair(hr, st, input, 1 / 30);
  const off = HR.hairOffsets(hr, st);
  const byId = Object.fromEntries(H.segments.map((s) => [s.id, s]));
  const segs = H.segments.filter((s) => hr.index[s.id] !== undefined);
  const offs = segs.map((s) => {
    let q = s;
    while (q.parent !== 'head' && byId[q.parent]) q = byId[q.parent];
    const a = poseDisplacement(pose, ...(q.pivot || [139, 60]));
    return [off[s.id][0] + Math.round(a[0] - head[0]), off[s.id][1] + Math.round(a[1] - head[1])];
  });
  const res = r.compose(f);
  const out = Uint8ClampedArray.from(res.pixels);
  const seg = new Uint8Array(W * W);
  for (let y = 0; y < W; y += 1) {
    for (let x = 0; x < W; x += 1) {
      const i = (y * W + x) * 4;
      const body = r.mask[i] > 127;
      const arm = r.mask[i + 1] > 127;
      for (let k = 0; k < segs.length; k += 1) {
        const s = segs[k];
        if (s.z === 'back' && body) continue;
        if (arm && !hr.segments[hr.index[s.id]].overArm) continue;
        const sx = x - offs[k][0] - s.box[0];
        const sy = y - offs[k][1] - s.box[1];
        if (sx < 0 || sy < 0 || sx >= s.box[2] || sy >= s.box[3]) continue;
        const q = ((s.at[1] + sy) * HA.width + s.at[0] + sx) * 4;
        if (HA.data[q + 3] > 127) {
          out.set([HA.data[q], HA.data[q + 1], HA.data[q + 2], 255], i);
          seg[y * W + x] = H.segments.indexOf(s) + 1;
          break;
        }
      }
    }
  }
  return { out, seg, segIds: H.segments.map((s) => s.id), layer: r.layer, order: r.order };
}

// The face below the eyes as she is seen at rest (the cheeks, the jaw, the chin, the ear by the
// earbud): the pixels of the face (and the skin under the hair, head_underlay) that no hair
// covers in the rest pose, inside the face box, from just below the eyes' centres down.
async function restFace(r) {
  const { meta, NEUTRAL_FRAME } = await rig();
  const { seg, layer, order } = await liveHair(r, { ...NEUTRAL_FRAME });
  const skin = new Set(['face', 'cheek', 'head_underlay'].map((id) => order.indexOf(id) + 1).filter((v) => v > 0));
  const A = meta.sheets.base.anchors;
  const [x0, , x1, y1] = faceBox(meta, 'base');
  const y0 = Math.round(A.eye_l[1]) + 2;
  const face = new Set([...skin, ...['eyes', 'mouth', 'blush', 'brows'].map((id) => order.indexOf(id) + 1)]);
  // (not the outline against her other parts — the turtleneck, the crown, the room: re-pixelized
  // in a pose it may land a pixel either way; the edge against the hair is what is checked)
  const inner = (x, y) => {
    for (let dy = -1; dy <= 1; dy += 1) for (let dx = -1; dx <= 1; dx += 1) {
      const j = (y + dy) * W + x + dx;
      if (!seg[j] && !face.has(layer[j])) return false;
    }
    return true;
  };
  const px = [];
  for (let y = y0; y <= y1; y += 1) for (let x = x0; x <= x1; x += 1) if (skin.has(layer[y * W + x]) && !seg[y * W + x] && inner(x, y)) px.push([x, y]);
  return px;
}

// The outline of the visible face per row below the eyes, each side (x of the first / last
// pixel of the face no hair covers — the part of it joined to the mouth, so a bit of skin seen
// through a gap of the hair beside her does not count), and the holes in it (pixels inside the
// outline where nothing of her shows).
function faceRows(out, seg, segIds, layer, order, A, above = 0) {
  const parts = new Set(['face', 'cheek', 'head_underlay'].map((id) => order.indexOf(id) + 1));
  const sprites = new Set(['eyes', 'mouth', 'blush', 'brows'].map((id) => order.indexOf(id) + 1));
  const y0 = Math.round(A.eye_l[1]) + 2;
  const y1 = Math.round(A.mouth[1]) + 14;
  // (the bangs may lie over the face: the outline is the skin's against the side locks)
  const lock = (j) => seg[j] && !segIds[seg[j] - 1].startsWith('bangs');
  const skinAt = (j) => {
    if (sprites.has(layer[j])) return true;
    if (!parts.has(layer[j])) return false;
    const c = [out[j * 4], out[j * 4 + 1], out[j * 4 + 2]];
    return SKIN.some((q) => q[0] === c[0] && q[1] === c[1] && q[2] === c[2]);
  };
  const vis = (x, y) => y >= y0 && y <= y1 && !lock(y * W + x) && (seg[y * W + x] ? parts.has(layer[y * W + x]) : skinAt(y * W + x));
  const on = new Uint8Array(W * W);
  const todo = [[Math.round(A.mouth[0]), Math.round(A.mouth[1])]];
  while (todo.length) {
    const [x, y] = todo.pop();
    if (x < 1 || y < 1 || x >= W - 1 || y >= W - 1 || on[y * W + x] || !vis(x, y)) continue;
    on[y * W + x] = 1;
    todo.push([x + 1, y], [x - 1, y], [x, y + 1], [x, y - 1]);
  }
  const rows = [];
  const holes = [];
  const specks = [];
  for (let y = y0; y <= y1; y += 1) {
    let l = null;
    let r = null;
    for (let x = 0; x < W; x += 1) if (on[y * W + x]) { if (l === null) l = x; r = x; }
    if (y >= Math.round(A.mouth[1]) - above && y <= Math.round(A.mouth[1]) + 7) rows.push([l, r]);
    // a hole: the room inside the outline; a speck: a pixel of a side lock alone on the skin
    // (the face on all 4 sides; the icon has one such strand dot by the earbud, which moves with
    // the lock — a lock's strand over the crown's hair at her jaw, or the tip of its edge, is not)
    const faceAround = (x, y) => on[y * W + x - 1] + on[y * W + x + 1] + on[(y - 1) * W + x] + on[(y + 1) * W + x];
    if (l !== null) {
      for (let x = l; x <= r; x += 1) {
        if (!layer[y * W + x] && !seg[y * W + x]) holes.push([x, y]);
        else if (lock(y * W + x) && parts.has(layer[y * W + x]) && faceAround(x, y) === 4) specks.push([x, y]);
      }
    }
  }
  return { rows, holes, specks };
}
// build_char.RAMPS.skin and LINE.skin (the face's skin and its line)
const SKIN = ['#5e3646', '#9a6270', '#d1a09c', '#efcfc5', '#fcece4', '#5c2f3d'].map(hexrgb);

test('the cheeks and the jaw: in every front pose no side lock covers the face that shows at rest (no shaved cheek), no hole, no break in the outline', async () => {
  const { make, meta, NEUTRAL_FRAME, poseDisplacement } = await rig();
  const r = make();
  const rest = await restFace(r);
  assert.ok(rest.length > 1000, `${rest.length} px of the face show at rest below the eyes`);
  // (the outline from the mouth's row 7 rows down — the side of the jaw, above the chin's curve — below the
  // bangs' reach: the bangs hang from the crown
  // and slide over the face around the eyes in a tilt, by design — what they uncover there is
  // the forehead's own fill, docs/research/stage-face-left-eye-2026-10-17)
  const jumps = (rows, side) => {
    let m = 0;
    for (let k = 1; k < rows.length; k += 1) if (rows[k][side] !== null && rows[k - 1][side] !== null) m = Math.max(m, Math.abs(rows[k][side] - rows[k - 1][side]));
    return m;
  };
  const base = await liveHair(r, { ...NEUTRAL_FRAME });
  // (the rest outline from 3 rows higher: a pose's anchors fall a row or two either way)
  const b = faceRows(base.out, base.seg, base.segIds, base.layer, base.order, meta.sheets.base.anchors, 3);
  const maxJump = [jumps(b.rows, 0), jumps(b.rows, 1)];
  assert.ok(maxJump[0] <= 6 && maxJump[1] <= 6, `rest outline steps ${maxJump}`);
  const bad = [];
  const report = [];
  for (const [sheet, def] of Object.entries(meta.sheets)) {
    if (def.view !== 'front') continue;
    for (const breath of [0, 1]) {
      const f = { ...NEUTRAL_FRAME, sheet, breath };
      const { out, seg, segIds, layer, order } = await liveHair(r, f);
      let shaved = 0;
      for (const [x, y] of rest) {
        // where this pixel of the face is in the pose (the head's displacement, whole pixels)
        const d = poseDisplacement(def, x, y);
        const j = (y + Math.round(d[1]) - breath) * W + x + Math.round(d[0]);
        const s = seg[j] ? segIds[seg[j] - 1] : '';
        // the bangs move over the face in a tilt by design (they hang from the crown); the side
        // locks and the wisps beside the cheeks must not
        if (s && !s.startsWith('bangs')) shaved += 1;
      }
      const A = { eye_l: [def.anchors.eye_l[0], def.anchors.eye_l[1] - breath], mouth: [def.anchors.mouth[0], def.anchors.mouth[1] - breath] };
      const p = faceRows(out, seg, segIds, layer, order, A);
      const steps = [jumps(p.rows, 0), jumps(p.rows, 1)];
      report.push(`${sheet}/${breath}: ${shaved} shaved, ${p.holes.length} holes, ${p.specks.length} specks ${JSON.stringify([...p.holes, ...p.specks].slice(0, 6))}, steps ${steps}`);
      // (up to 4 px: where the face part's own field fades at its edge — by the ear, under the
      // chin — its pixel lands one off the head's displacement; main: 44-343 px per tilted pose)
      if (shaved > 4 || p.holes.length || p.specks.length > 1 || steps[0] > maxJump[0] + 1 || steps[1] > maxJump[1] + 1) bad.push(report[report.length - 1]);
    }
  }
  if (process.env.STAGE_CHEEK_REPORT) console.log(`rest steps ${maxJump}\n${report.join('\n')}`);
  assert.deepEqual(bad, []);
});

// --- the drooped ears (#341, 2026-10-18: WARNING / CRITICAL, both ears circled) ---------------
// The flattened ear (extras ears.<side>.droop) turns about a pivot inside the head and carries
// the top of the head where the ears stood. Checked on the composited frame with the hair layer
// at rest, above the ears' anchors (the head's top, where the ears meet it):
//  - no notch: no pixel of the room that a disk of radius 3 closes into her silhouette there
//    (main: the right ear's root stood as a straight vertical cut above the head, a V of room
//    between it and the head; the left ear's root corner stood up beside the head)
//  - no spike: no pixel of the ear's drawing with 2 neighbours of her or fewer (of 8)
//  - no dark seam inside: no pixel of the ear's drawing in the darkest hair greys (hair ramp 0-1,
//    the outline greys) with no room beside it and light hair (ramp 3-5) on 5 of its 8 sides
//    (main: the cap's rim drawn across the ear's root, a dark line through the hair)
const DARK = new Set(['#25243f', '#3e3f66', '#0b0a16', '#1d2036'].map((h) => hexrgb(h).join()));
const LIGHT = new Set(['#8b8fb5', '#b9bdd8', '#e6e8f3'].map((h) => hexrgb(h).join()));
function earFlaws(out, seg, layer, earLayers, A, side) {
  const [ax, ay] = A[`ear_${side}`];
  const x0 = Math.round(side === 'l' ? ax - 50 : ax - 30);
  const x1 = Math.round(side === 'l' ? ax + 28 : ax + 48);
  const y1 = Math.round(ay);
  const her = (x, y) => x >= 0 && y >= 0 && x < W && y < W && (out[(y * W + x) * 4 + 3] > 0 || seg[y * W + x] > 0);
  const col = (x, y) => [out[(y * W + x) * 4], out[(y * W + x) * 4 + 1], out[(y * W + x) * 4 + 2]].join();
  const R = 3;
  const disk = [];
  for (let dy = -R; dy <= R; dy += 1) for (let dx = -R; dx <= R; dx += 1) if (dx * dx + dy * dy <= R * R) disk.push([dx, dy]);
  const dil = new Uint8Array(W * W);
  for (let y = 0; y <= y1 + R; y += 1) for (let x = x0 - R; x <= x1 + R; x += 1) if (x >= 0 && x < W && disk.some(([dx, dy]) => her(x + dx, y + dy))) dil[y * W + x] = 1;
  const notch = [];
  const spike = [];
  const seam = [];
  for (let y = 0; y <= y1; y += 1) {
    for (let x = x0; x <= x1; x += 1) {
      if (!her(x, y)) {
        // closing: every point of the disk around it is in the dilated silhouette
        if (disk.every(([dx, dy]) => dil[(y + dy) * W + x + dx] || y + dy < 0)) notch.push([x, y]);
        continue;
      }
      let n = 0;
      let room = false;
      let light = 0;
      for (let dy = -1; dy <= 1; dy += 1) for (let dx = -1; dx <= 1; dx += 1) {
        if (!dx && !dy) continue;
        if (her(x + dx, y + dy)) {
          n += 1;
          if (LIGHT.has(col(x + dx, y + dy))) light += 1;
        } else room = true;
      }
      const ear = !seg[y * W + x] && earLayers.has(layer[y * W + x]);
      if (n <= 2 && ear) spike.push([x, y]);
      if (!room && ear && DARK.has(col(x, y)) && light >= 5) seam.push([x, y]);
    }
  }
  return { notch, spike, seam };
}

test('the drooped ears (WARNING / CRITICAL): no notch at the root, no spike, no dark seam across the root, in every front pose', async () => {
  const { meta, atlas, NEUTRAL_FRAME, CharRig } = await rig();
  const extras = { meta: readJson('assets', 'char', 'extras.json'), atlas: decodePng(fs.readFileSync(path.join(STAGE, 'assets', 'char', 'extras.png'))) };
  const r = new CharRig(meta, atlas, extras);
  const bad = [];
  const report = [];
  for (const [sheet, def] of Object.entries(meta.sheets)) {
    if (def.view !== 'front') continue;
    for (const ears of ['droop', 'angle']) {
      const f = { ...NEUTRAL_FRAME, sheet, ears };
      const { out, seg, layer, order } = await liveHair(r, f);
      const earLayers = new Set([order.indexOf('ear_l') + 1, order.indexOf('ear_r') + 1]);
      for (const side of ['l', 'r']) {
        const { notch, spike, seam } = earFlaws(out, seg, layer, earLayers, def.anchors, side);
        const line = `${sheet} ${ears} ${side}: notch ${notch.length}, spike ${spike.length}, seam ${seam.length} ${JSON.stringify([...notch, ...spike, ...seam].slice(0, 4))}`;
        report.push(line);
        if (ears === 'droop' && (notch.length || spike.length || seam.length)) bad.push(line);
      }
    }
  }
  if (process.env.STAGE_EAR_REPORT) console.log(report.join('\n'));
  assert.deepEqual(bad, []);
});

// --- the jaw line in every front pose (#447, 2026-10-19: "in tests-run the cheek's outline steps
// out and back on its way to the chin") ---------------------------------------------------------
// The face she shows (the face, cheek and skin-under-hair parts in skin colours, and the face
// sprites, where no hair segment is drawn, joined to the mouth) per row, each side: its first and
// last pixel. In a pose it should be the rest outline moved with the head (poseDisplacement,
// whole pixels, as the cheek layer and the hair are placed). The check: from JAW_ROWS[0] rows
// above the mouth (beside the mouth; on each side below the last row where the bangs border the
// outline — they cross the face in a tilt by design) to JAW_ROWS[1] rows below it (the corner
// of the jaw), how far the pose's outline is from the moved rest outline may change by at most
// JAW_STEP art px from one row to the next and stay within JAW_OFF: no step, bulge or notch the
// rest drawing does not have. (main before #447: steps up to 5 px, offsets up to 8 px — the face
// part's own pixels showed between the cheek and a side lock that had moved off the jaw, on the
// side the head turns or tilts to; tests-run's `type`: a 4 px step, 7 px off.)
const JAW_STEP = 1;
const JAW_OFF = 2; // and the outline stays within 2 art px of the moved rest outline
const JAW_ROWS = [6, 12];
const SKIN_HEX = new Set(SKIN.map((c) => c.join()));
function shownFace(out, seg, segIds, layer, order, A, breath) {
  const parts = new Set(['face', 'cheek', 'head_underlay'].map((id) => order.indexOf(id) + 1));
  const sprites = new Set(['eyes', 'mouth', 'blush', 'brows'].map((id) => order.indexOf(id) + 1));
  const y0 = Math.round(A.eye_l[1]) - breath + 2;
  const vis = (j) => !seg[j] && (sprites.has(layer[j]) || (parts.has(layer[j]) && SKIN_HEX.has([out[j * 4], out[j * 4 + 1], out[j * 4 + 2]].join())));
  const on = new Uint8Array(W * W);
  const todo = [[Math.round(A.mouth[0]), Math.round(A.mouth[1]) - breath]];
  while (todo.length) {
    const [x, y] = todo.pop();
    if (x < 1 || y < y0 || x >= W - 1 || y >= W - 1 || on[y * W + x] || !vis(y * W + x)) continue;
    on[y * W + x] = 1;
    todo.push([x + 1, y], [x - 1, y], [x, y + 1], [x, y - 1]);
  }
  const bang = (j) => seg[j] > 0 && segIds[seg[j] - 1].startsWith('bangs');
  const crown = order.indexOf('crown') + 1;
  const chin = (j) => !seg[j] && layer[j] === crown;
  return { on, edges: rowEdges(on, (y, l, r) => [bang(y * W + l - 1), bang(y * W + r + 1), chin(y * W + l - 1), chin(y * W + r + 1)]) };
}
function rowEdges(on, extra = () => []) {
  const rows = new Map();
  for (let y = 0; y < W; y += 1) {
    let l = null;
    let r = null;
    for (let x = 0; x < W; x += 1) if (on[y * W + x]) { if (l === null) l = x; r = x; }
    if (l !== null) rows.set(y, [l, r, ...extra(y, l, r)]);
  }
  return rows;
}
// The rest face moved with the head (forward, and back for the pixels no rest pixel lands on),
// as tools/char_layers.cheek_part places the cheek layer.
function movedFace(on, def, breath, poseDisplacement) {
  const m = new Uint8Array(W * W);
  for (let y = 0; y < W; y += 1) {
    for (let x = 0; x < W; x += 1) {
      if (on[y * W + x]) {
        const d = poseDisplacement(def, x, y);
        const tx = x + Math.round(d[0]);
        const ty = y + Math.round(d[1]) - breath;
        if (tx >= 0 && ty >= 0 && tx < W && ty < W) m[ty * W + tx] = 1;
      }
      const d = poseDisplacement(def, x, y + breath);
      const sx = x - Math.round(d[0]);
      const sy = y + breath - Math.round(d[1]);
      if (sx >= 0 && sy >= 0 && sx < W && sy < W && on[sy * W + sx]) m[y * W + x] = 1;
    }
  }
  return rowEdges(m);
}
// Per side: the rows checked, the largest change of the offset from one row to the next and the
// largest offset. (A row whose outline borders the crown's hair under the chin is not checked:
// that part is re-pixelized with the pose's own field and lands a pixel either way.)
function jawSteps(edges, moved, A, breath) {
  const my = Math.round(A.mouth[1]) - breath;
  const ys = [...edges.keys()].filter((y) => moved.has(y) && y >= my - JAW_ROWS[0] && y <= my + JAW_ROWS[1]).sort((a, b) => a - b);
  return [0, 1].map((side) => {
    let start = ys[0];
    for (const y of ys) if (edges.get(y)[2 + side]) start = y + 1;
    const rows = ys.filter((y) => y >= start && !edges.get(y)[4 + side]);
    const off = (y) => edges.get(y)[side] - moved.get(y)[side];
    let worst = { step: 0, row: null };
    let away = 0;
    for (let k = 0; k < rows.length; k += 1) {
      away = Math.max(away, Math.abs(off(rows[k])));
      if (k && rows[k] === rows[k - 1] + 1) {
        const step = Math.abs(off(rows[k]) - off(rows[k - 1]));
        if (step > worst.step) worst = { step, row: rows[k] };
      }
    }
    return { rows: rows.length, away, ...worst };
  });
}

test(`the jaw line: in every front pose the outline from beside the mouth to the corner of the jaw follows the rest outline moved with the head (a step of at most ${JAW_STEP} art px from row to row), both sides`, async () => {
  const { make, meta, NEUTRAL_FRAME, poseDisplacement } = await rig();
  const r = make();
  const base = await liveHair(r, { ...NEUTRAL_FRAME });
  const rest = shownFace(base.out, base.seg, base.segIds, base.layer, base.order, meta.sheets.base.anchors, 0);
  const bad = [];
  const report = [];
  for (const [sheet, def] of Object.entries(meta.sheets)) {
    if (def.view !== 'front') continue;
    for (const breath of [0, 1]) {
      const { out, seg, segIds, layer, order } = await liveHair(r, { ...NEUTRAL_FRAME, sheet, breath });
      const shown = shownFace(out, seg, segIds, layer, order, def.anchors, breath);
      const steps = jawSteps(shown.edges, movedFace(rest.on, def, breath, poseDisplacement), def.anchors, breath);
      const line = `${sheet}/${breath}: ${['left', 'right'].map((side, k) => `${side} step ${steps[k].step} px at row ${steps[k].row}, off ${steps[k].away} px (${steps[k].rows} rows)`).join('; ')}`;
      report.push(line);
      // (not blind: most of the band is checked on both sides)
      if (steps.some((s) => s.step > JAW_STEP || s.away > JAW_OFF || s.rows < 12)) bad.push(line);
    }
  }
  if (process.env.STAGE_JAW_REPORT) console.log(report.join('\n'));
  assert.deepEqual(bad, []);
});
