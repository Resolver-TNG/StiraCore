// Stage hair rig (#341): parts and tip segments, follow-through as a deterministic function of
// the head pose, looks per derived state, and the hair staying inside the character layer.
// node --test tests/stage_hair_test.cjs
const test = require('node:test');
const assert = require('node:assert/strict');
const crypto = require('node:crypto');
const fs = require('node:fs');
const path = require('node:path');
const zlib = require('node:zlib');

const ROOT = path.join(__dirname, '..', 'dashboard', 'stage');
const CHAR = path.join(ROOT, 'assets', 'char');
const load = (name) => import(path.join(ROOT, 'js', name));
const META = JSON.parse(fs.readFileSync(path.join(CHAR, 'hair.json'), 'utf8'));

// Minimal PNG reader (8-bit RGB / RGBA, not interlaced: what build_char/build_hair write).
function readPng(file) {
  const buf = fs.readFileSync(file);
  let pos = 8;
  let w = 0;
  let h = 0;
  let ch = 0;
  const idat = [];
  while (pos < buf.length) {
    const len = buf.readUInt32BE(pos);
    const type = buf.toString('ascii', pos + 4, pos + 8);
    const data = buf.subarray(pos + 8, pos + 8 + len);
    if (type === 'IHDR') {
      w = data.readUInt32BE(0);
      h = data.readUInt32BE(4);
      assert.equal(data[8], 8, 'bit depth');
      assert.equal(data[12], 0, 'interlace');
      ch = { 2: 3, 6: 4 }[data[9]];
      assert.ok(ch, `colour type ${data[9]}`);
    } else if (type === 'IDAT') idat.push(data);
    pos += 12 + len;
  }
  const raw = zlib.inflateSync(Buffer.concat(idat));
  const out = new Uint8Array(w * h * 4);
  const stride = w * ch;
  const prev = new Uint8Array(stride);
  const cur = new Uint8Array(stride);
  for (let y = 0; y < h; y += 1) {
    const f = raw[y * (stride + 1)];
    for (let i = 0; i < stride; i += 1) {
      const x = raw[y * (stride + 1) + 1 + i];
      const a = i >= ch ? cur[i - ch] : 0;
      const b = prev[i];
      const c = i >= ch ? prev[i - ch] : 0;
      let v;
      if (f === 0) v = x;
      else if (f === 1) v = x + a;
      else if (f === 2) v = x + b;
      else if (f === 3) v = x + ((a + b) >> 1);
      else {
        const p = a + b - c;
        const pa = Math.abs(p - a);
        const pb = Math.abs(p - b);
        const pc = Math.abs(p - c);
        v = x + (pa <= pb && pa <= pc ? a : pb <= pc ? b : c);
      }
      cur[i] = v & 255;
    }
    for (let x = 0; x < w; x += 1) {
      for (let k = 0; k < 3; k += 1) out[(y * w + x) * 4 + k] = cur[x * ch + k];
      out[(y * w + x) * 4 + 3] = ch === 4 ? cur[x * ch + 3] : 255;
    }
    prev.set(cur);
  }
  return { w, h, px: out };
}

// CPU model of the character layer + the hair layer at rest pose (head, breath, ears, arm 0).
function composite(offsets = {}) {
  const base = readPng(path.join(CHAR, 'base.png'));
  const atlas = readPng(path.join(CHAR, 'hair.png'));
  const fx = readPng(path.join(CHAR, 'hair_fx.png'));
  const mask = readPng(path.join(CHAR, 'hair_mask.png'));
  const { w, h } = base;
  const A = (x, y) => x >= 0 && y >= 0 && x < w && y < h && base.px[(y * w + x) * 4 + 3] > 127;
  const out = Buffer.alloc(w * h * 5);
  for (let y = 0; y < h; y += 1) {
    for (let x = 0; x < w; x += 1) {
      const o = (y * w + x) * 5;
      let a = A(x, y);
      let rgb = [0, 1, 2].map((k) => base.px[(y * w + x) * 4 + k]);
      const e2 = !A(x + 2, y) || !A(x - 2, y) || !A(x, y - 2);
      const e1 = !A(x + 1, y) || !A(x - 1, y) || !A(x, y - 1);
      let rim = a && e2 && !e1 && y < 205;
      const body = mask.px[(y * w + x) * 4] > 127;
      const arm = mask.px[(y * w + x) * 4 + 1] > 127;
      if (!arm) {
        for (const s of META.segments) {
          if (s.z === 'back' && body) continue;
          const [ox, oy] = offsets[s.id] || [0, 0];
          const px = x - ox - s.box[0];
          const py = y - oy - s.box[1];
          if (px < 0 || py < 0 || px >= s.box[2] || py >= s.box[3]) continue;
          const q = ((s.at[1] + py) * atlas.w + s.at[0] + px) * 4;
          if (atlas.px[q + 3] > 127) {
            a = true;
            rgb = [atlas.px[q], atlas.px[q + 1], atlas.px[q + 2]];
            rim = fx.px[q] > 127;
            break;
          }
        }
      }
      out[o] = a ? 1 : 0;
      for (let k = 0; k < 3; k += 1) out[o + 1 + k] = a ? rgb[k] : 0;
      out[o + 4] = a && rim ? 1 : 0;
    }
  }
  composite.last = { w, h, out };
  return crypto.createHash('sha256').update(out).digest('hex');
}

const CALM = { emotion: 'calm', ears: 0 };
const still = (n, f = {}) => Array.from({ length: n }, () => ({ head: [0, 0], breath: 0, sway: 0, pose: CALM, ...f }));

test('at rest the cut hair + the filled base look exactly like the uncut sheet (rim light included)', () => {
  assert.match(META.restHash, /^[0-9a-f]{64}$/);
  assert.equal(composite(), META.restHash);
  const tips = Object.fromEntries(META.segments.filter((s) => s.depth > 0).map((s) => [s.id, [s.side || 1, 1]]));
  assert.notEqual(composite(tips), META.restHash, 'moving the tips changes the picture (the check is not blind)');
});

test('the old per-pixel hair sway is gone: hair moves only through the rig', () => {
  const rig = readPng(path.join(CHAR, 'rig.png'));
  let n = 0;
  for (let i = 0; i < rig.w * rig.h; i += 1) if (rig.px[i * 4 + 1] > 0) n += 1;
  assert.equal(n, 0, `rig.png G (hair sway weight) is set on ${n} px`);
});

test('five parts, each split into a root and tip segments hanging from their parent', async () => {
  const { HAIR_RIG } = await load('hair_rig.js');
  assert.deepEqual(META.parts.map((p) => p.id).sort(), ['back', 'bangs', 'side_l', 'side_r', 'wisps']);
  const ids = new Set(META.segments.map((s) => s.id));
  for (const p of META.parts) {
    assert.ok(HAIR_RIG[p.id], `tuning for ${p.id}`);
    for (const c of p.chains) {
      const segs = META.segments.filter((s) => s.chain === c);
      assert.ok(segs.length >= 2, `${c}: root + at least one tip segment`);
      for (const s of segs) {
        assert.equal(s.parent, s.depth ? `${c}.${s.depth - 1}` : s.parent);
        assert.ok(s.parent === 'head' || ids.has(s.parent), `${s.id} parent ${s.parent}`);
        assert.ok(s.at[0] + s.box[2] <= META.atlas[0] && s.at[1] + s.box[3] <= META.atlas[1], `${s.id} inside the atlas`);
        assert.ok(s.pixels > 0);
      }
    }
  }
  assert.ok(META.segments.filter((s) => s.depth >= 2).length >= 6, 'side locks, bangs and back hair have 2-3 tip segments');
  // draw order: a tip is drawn before (= over) its parent; front parts before back parts
  const at = Object.fromEntries(META.segments.map((s, i) => [s.id, i]));
  for (const s of META.segments) if (s.parent !== 'head' && META.segments[at[s.parent]].chain === s.chain) assert.ok(at[s.id] < at[s.parent], s.id);
  const lastFront = Math.max(...META.segments.map((s, i) => (s.z === 'front' ? i : -1)));
  const firstBack = Math.min(...META.segments.map((s, i) => (s.z === 'back' ? i : Infinity)));
  assert.ok(lastFront < firstBack);
});

test('the same head-pose sequence gives the same hair, frame for frame (no randomness)', async () => {
  const { createHairRig, simulateHair } = await load('hair_rig.js');
  const rig = createHairRig(META);
  const frames = [];
  for (let i = 0; i < 400; i += 1) {
    const t = i / 60;
    frames.push({
      head: [Math.round(Math.sin(t * 2.3) * 1.6), i % 97 < 40 ? 1 : 0],
      breath: -Math.round(Math.sin((2 * Math.PI * t) / 4.2) * 0.5 + 0.5),
      sway: Math.sin(2 * Math.PI * (t / 5.3 + 0.2)),
      pose: i < 200 ? CALM : { emotion: 'tense', ears: -3 },
      dt: i % 3 ? 1 / 60 : 1 / 45,
    });
  }
  const a = simulateHair(rig, frames);
  const b = simulateHair(createHairRig(structuredClone(META)), structuredClone(frames));
  assert.deepEqual(a, b);
  for (const o of a) for (const v of Object.values(o)) assert.ok(Number.isInteger(v[0]) && Number.isInteger(v[1]), 'whole pixels');
  for (const n of ['hair_rig.js', 'hair_layer.js']) {
    const src = fs.readFileSync(path.join(ROOT, 'js', n), 'utf8');
    assert.doesNotMatch(src, /Math\.random|Date\.now|performance\.now|\beval\(|new Function/, n);
  }
  // the state step is pure: stepping does not modify the state it was given
  const { initHairState, stepHair } = await load('hair_rig.js');
  const s0 = initHairState(rig);
  const copy = structuredClone(s0);
  stepHair(rig, s0, frames[5], 1 / 60);
  assert.deepEqual(s0, copy);
});

test('follow-through: after a head step only the free tips trail (a pixel per segment), never past the head, then settle', async () => {
  const { createHairRig, simulateHair } = await load('hair_rig.js');
  const rig = createHairRig(META);
  const frames = [...still(30), ...still(150, { head: [2, 0] }), ...still(240)];
  const out = simulateHair(rig, frames);
  const head = (o) => frames[out.indexOf(o)].head;
  // over the face: exactly with the head in every frame (the fringe never slips off the face)
  for (const s of META.segments.filter((x) => x.chain === 'bangs')) {
    for (const o of out) assert.deepEqual(o[s.id], head(o), `${s.id} moves with the head`);
  }
  for (const chain of ['side_l', 'side_r']) {
    const segs = META.segments.filter((s) => s.chain === chain).sort((a, b) => a.depth - b.depth);
    const lag = segs.map((s) => Math.max(...out.map((o) => Math.abs(o[s.id][0] - head(o)[0]))));
    for (let d = 1; d < segs.length; d += 1) assert.ok(lag[d] >= lag[d - 1], `${chain}: the lag grows toward the tip ${lag}`);
    assert.deepEqual(lag.slice(0, 2), [0, 0], `${chain}: root and the lock along the cheek move with the head`);
    assert.ok(lag[segs.length - 1] >= 1 && lag[segs.length - 1] <= segs.length - 2, `${chain}: the tip trails a little ${lag}`);
    // never past the head (no overshoot: it reads as swinging too far)
    for (const s of segs) {
      for (const o of out) {
        const h = head(o)[0];
        const prev = frames[Math.max(0, out.indexOf(o) - 1)].head[0];
        const lo = Math.min(h, prev, 0) - 0;
        const hi = Math.max(h, prev, 2);
        assert.ok(o[s.id][0] >= lo && o[s.id][0] <= hi, `${s.id} stays between where the head was and is`);
      }
      assert.deepEqual(out[out.length - 1][s.id], [0, 0], `${s.id} settles where it hangs`);
    }
  }
  // caps and connection: at most 1 px from its parent; a root within its cap
  for (const o of out) {
    for (const s of rig.segments) {
      const p = s.parent === 'head' ? head(o) : o[s.parent];
      assert.ok(Math.abs(o[s.id][0] - p[0]) <= 1 && Math.abs(o[s.id][1] - p[1]) <= 1, `${s.id} stays joined`);
      if (s.parent === 'head') assert.ok(Math.abs(o[s.id][0] - p[0]) <= s.maxAmp, `${s.id} within its cap`);
    }
  }
});

test('breath and idle sway are in phase: the whole hair moves in the same frame, by one pixel at most', async () => {
  const { createHairRig, simulateHair, HAIR_RIG } = await load('hair_rig.js');
  const rig = createHairRig(META);
  const frames = [];
  for (let i = 0; i < 30 * 12; i += 1) {
    const t = i / 30;
    frames.push({ head: [0, 0], breath: Math.sin((2 * Math.PI * t) / 4.2) > 0.35 ? -1 : 0, sway: Math.sin(2 * Math.PI * (t / 5.3 + 0.2)), pose: CALM, dt: 1 / 30 });
  }
  const out = simulateHair(rig, frames);
  const swaying = rig.segments.filter((s) => s.sway > 0).map((s) => s.id);
  assert.ok(swaying.length >= 6, 'the free tips sway');
  for (let k = 0; k < out.length; k += 1) {
    const o = out[k];
    // one sway value for every segment that sways, one breath value for all: no row bands
    assert.equal(new Set(swaying.map((id) => o[id][0])).size, 1, `frame ${k}: the tips sway as one piece`);
    assert.equal(new Set(rig.segments.map((s) => o[s.id][1])).size, 1, `frame ${k}: the hair rises with the chest as one piece`);
    for (const s of rig.segments) {
      assert.ok(Math.abs(o[s.id][0]) <= 1, `${s.id}: at most one pixel sideways`);
      if (!(s.sway > 0)) assert.equal(o[s.id][0], 0, `${s.id}: does not sway`);
    }
  }
  // the bangs and the locks along the cheeks never sway (they lie on the face)
  for (const id of ['bangs']) assert.ok(HAIR_RIG[id].sway.every((v) => v === 0));
  // and it does sway (a few times in 12 s), not flicker
  const changes = out.slice(1).filter((o, i) => o[swaying[0]][0] !== out[i][swaying[0]][0]).length;
  assert.ok(changes >= 4 && changes <= 12, `${changes} sway steps in 12 s`);
});

test('the hair look per state comes only from the derived pose (same state, same hair)', async () => {
  const { hairStyle, withHairStyle, createHairRig, simulateHair, HAIR_STYLES } = await load('hair_rig.js');
  const { initialState, reduce, derive } = await load('model.js');
  const pose = (...evs) => derive(evs.reduce(reduce, initialState())).pose;
  const N = (values) => ({ type: 'neural', values });
  assert.equal(hairStyle(pose()).style, 'calm');
  assert.equal(hairStyle(pose(N({ satisfaction: 0.9 }))).style, 'bounce');
  assert.equal(hairStyle(pose(N({ curiosity: 0.9 }))).style, 'perk');
  assert.equal(hairStyle(pose(N({ vigilance: 0.8 }))).style, 'alert');
  assert.equal(hairStyle(pose({ type: 'breaker', level: 'WARNING' })).style, 'droop');
  assert.equal(hairStyle(pose({ type: 'breaker', level: 'CRITICAL' })).style, 'bristle');
  // event history does not matter, only the state
  const a = pose(N({ vigilance: 0.8 }), { type: 'breaker', level: 'CRITICAL' }, { type: 'breaker', level: 'NORMAL' });
  const b = pose({ type: 'turn.start' }, { type: 'turn.end' }, N({ vigilance: 0.8 }));
  assert.equal(hairStyle(a).style, hairStyle(b).style);
  const d = withHairStyle(derive(initialState()));
  assert.equal(d.pose.hair, 'calm');
  assert.ok(d.causes.some((c) => c.channel === 'hair' && c.value === 'calm' && c.cause));
  // steady state per style: distinct from calm where the style lifts something, repeatable
  const rig = createHairRig(META);
  const settle = (p) => simulateHair(rig, still(300, { pose: p })).at(-1);
  const calm = settle(pose());
  for (const [style, p] of [['bounce', pose(N({ satisfaction: 0.9 }))], ['droop', pose({ type: 'breaker', level: 'WARNING' })], ['alert', pose(N({ vigilance: 0.8 }))], ['bristle', pose({ type: 'breaker', level: 'CRITICAL' })]]) {
    assert.ok(HAIR_STYLES[style]);
    const s1 = settle(p);
    assert.notDeepEqual(s1, calm, `${style} changes the hair`);
    assert.deepEqual(settle(p), s1, `${style}: same state, same hair`);
  }
  const alert = settle(pose(N({ vigilance: 0.8 })));
  assert.ok(alert['wisps_l.1'][1] < calm['wisps_l.1'][1], 'alert: the wisps stand up');
  const droop = settle(pose({ type: 'breaker', level: 'WARNING' }));
  assert.ok(droop['wisps_l.1'][1] > calm['wisps_l.1'][1], 'worried: the wisps hang down');
  // joy: one hop when the style starts (the tips go up first), then it settles
  const hop = simulateHair(rig, [...still(30), ...still(120, { pose: pose(N({ satisfaction: 0.9 })) })]);
  assert.ok(Math.min(...hop.map((o) => o['side_r.3'][1])) < 0, 'joy: the hair hops');
});

test('tuning is data: functions and junk are dropped, numbers clamped, unknown parts rejected', async () => {
  const { createHairRig, HAIR_RIG } = await load('hair_rig.js');
  const bad = { ...HAIR_RIG, side_l: { follow: [() => 1, 'x', 9], delay: [99], maxAmp: [50], stiffness: 1e9, damping: [-4], sway: [Infinity] } };
  const rig = createHairRig(META, bad, { calm: {}, 'Bad Id': { lift: { side_l: [[9, 9]] } }, evil: { lift: { nope: [[1, 1]] }, stiffness: () => 9 } });
  for (const s of rig.segments) {
    for (const v of Object.values(s)) assert.notEqual(typeof v, 'function');
    assert.ok(s.follow >= 0 && s.follow <= 1 && s.maxAmp <= 4 && s.stiffness <= 600 && s.damping >= 0.05 && s.delay <= 0.53);
  }
  assert.ok(!('Bad Id' in rig.styles));
  assert.deepEqual(rig.styles.evil.lift, {});
  assert.equal(rig.styles.evil.stiffness, 1);
  assert.ok(Object.isFrozen(rig) && Object.isFrozen(rig.segments[0]));
  const partial = createHairRig({ ...META, segments: [...META.segments, { ...META.segments[0], id: 'x.0', part: 'unknown' }] });
  assert.deepEqual(partial.rejected, ['x.0']);
});

test('docs/stage.md lists the hair styles as in the code', async () => {
  const { HAIR_STYLE_RULES } = await load('hair_rig.js');
  const md = fs.readFileSync(path.join(__dirname, '..', 'docs', 'stage.md'), 'utf8');
  const block = md.split('<!-- stage-hair:begin -->')[1].split('<!-- stage-hair:end -->')[0];
  const rows = block.split('\n').filter((l) => /^\| `/.test(l)).map((l) => l.split('|').map((c) => c.trim()));
  assert.deepEqual(rows.map((r) => r[1].replace(/`/g, '')), HAIR_STYLE_RULES.map((r) => r.style));
  assert.deepEqual(rows.map((r) => r[2]), HAIR_STYLE_RULES.map((r) => r.when));
});

test('draw order is data: part slots in the character layer order, overArm per depth', async () => {
  const { createHairRig, HAIR_RIG } = await load('hair_rig.js');
  assert.deepEqual(META.layerOrder, ['hair:back', 'char:body', 'hair:side_l', 'hair:side_r', 'hair:wisps', 'char:face', 'hair:bangs', 'char:arms']);
  const slot = Object.fromEntries(META.parts.map((p) => [p.id, p.slot]));
  assert.deepEqual(slot, { back: 'behind:body', side_l: 'over:body', side_r: 'over:body', wisps: 'over:body', bangs: 'over:face' });
  for (const p of META.parts) assert.ok(META.layerOrder.includes(`hair:${p.id}`), p.id);
  assert.ok(createHairRig(META).segments.every((s) => s.overArm === false), 'by default the arm and mug are in front (as in the icon)');
  const tipsOver = createHairRig(META, { ...HAIR_RIG, side_l: { ...HAIR_RIG.side_l, overArm: [false, false, true] } });
  assert.deepEqual(tipsOver.segments.filter((s) => s.overArm).map((s) => s.id).sort(), ['side_l.2', 'side_l.3']);
  const src = fs.readFileSync(path.join(ROOT, 'js', 'hair_layer.js'), 'utf8');
  assert.match(src, /if \(arm && uOverArm\[i\] < 0\.5\) continue;/);
});

test('long hair: the back hair continues below the round crop and ends in tips above the frame', () => {
  const atlas = readPng(path.join(CHAR, 'hair.png'));
  const mask = readPng(path.join(CHAR, 'hair_mask.png'));
  const [w] = META.size;
  const c = (w - 1) / 2;
  let beyond = 0;
  let lowest = 0;
  for (const s of META.segments.filter((x) => x.part === 'back')) {
    for (let y = 0; y < s.box[3]; y += 1) {
      for (let x = 0; x < s.box[2]; x += 1) {
        if (atlas.px[((s.at[1] + y) * atlas.w + s.at[0] + x) * 4 + 3] < 128) continue;
        const X = s.box[0] + x;
        const Y = s.box[1] + y;
        if (mask.px[(Y * w + X) * 4] > 127) continue; // behind the body: hidden continuation
        if (Math.hypot(X - c, Y - c) >= w / 2 - 3) beyond += 1;
        lowest = Math.max(lowest, Y);
      }
    }
  }
  assert.ok(beyond > 400, `drawn hair beyond the crop: ${beyond} px`);
  assert.ok(lowest >= 255 && lowest < META.size[1] - 1, `the tips end at row ${lowest}, not at the sheet's bottom edge`);
});

test('the lock beside the cheek is one drawn lock: no holes, a continuous outer contour', () => {
  composite();
  const { w, out } = composite.last;
  const A = (x, y) => out[(y * w + x) * 5] === 1;
  let prev = null;
  for (let y = 95; y <= 155; y += 1) {
    let x0 = 70;
    while (!A(x0, y)) x0 += 1;
    for (let x = x0; x < 104; x += 1) assert.ok(A(x, y), `hole at (${x}, ${y})`);
    if (prev !== null) assert.ok(Math.abs(x0 - prev) <= 1, `contour jumps ${prev} -> ${x0} at row ${y}`);
    prev = x0;
  }
});

test('the ear beside the face shows skin and the earbud, not hair colour', () => {
  const base = readPng(path.join(CHAR, 'base.png'));
  const src = fs.readFileSync(path.join(ROOT, 'tools', 'build_char.py'), 'utf8');
  const ramp = (name) => src.match(new RegExp(`"${name}": \\[([^\\]]+)\\]`))[1].match(/#[0-9a-f]{6}/g);
  const count = (name) => {
    const set = new Set(ramp(name));
    let n = 0;
    for (let y = 90; y < 116; y += 1) {
      for (let x = 164; x < 181; x += 1) {
        const k = (y * base.w + x) * 4;
        const hex = `#${[0, 1, 2].map((c) => base.px[k + c].toString(16).padStart(2, '0')).join('')}`;
        if (set.has(hex)) n += 1;
      }
    }
    return n;
  };
  assert.ok(count('skin') >= 150, `skin px ${count('skin')}`);
  assert.ok(count('mug') >= 25, `earbud px ${count('mug')}`);
});

// --- the hair stays in the character layer --------------------------------------------------

async function fakeStage() {
  const THREE = await import(path.join(ROOT, 'vendor', 'three.stage.min.js'));
  const scene = new THREE.Scene();
  const mk = (name, x, y, order) => {
    const mesh = new THREE.Mesh(new THREE.PlaneGeometry(10, 10), new THREE.ShaderMaterial({ uniforms: { uLevel: { value: 0.5 } } }));
    mesh.position.set(x, y, 0);
    mesh.renderOrder = order;
    mesh.name = name;
    scene.add(mesh);
    return { name, mesh };
  };
  const layers = [mk('city', 320, 170, 0), mk('wall', 320, 170, 3), mk('desk', 320, 170, 8)];
  const char = mk('character', 320, 181, 10);
  const u = (value) => ({ value });
  const charMat = { uniforms: {} };
  for (const k of ['tBase', 'tRig', 'tRig2', 'tFace']) charMat.uniforms[k] = u(null);
  Object.assign(charMat.uniforms, {
    uSize: u(new THREE.Vector2(278, 278)), uFaceSize: u(new THREE.Vector2(64, 200)), uHead: u(new THREE.Vector2(0, 0)), uBreath: u(0), uSway: u(0),
    uEarL: u(0), uEarR: u(0), uArm: u(0), uSweatBox: u(new THREE.Vector4(0, 0, 1, 1)), uSweat: u(0), uSweatRow: u(0),
    uRimColor: u(new THREE.Vector3(0, 1, 1)), uRim: u(1), uLayers: u(0),
  });
  const camera = new THREE.OrthographicCamera(0, 433, 330, 44, -10, 10);
  const grade = { uDim: u(0), uDesat: u(0), uAmbient: u(new THREE.Vector3(1, 1, 1)) };
  return { THREE, stage: { scene, layers: [...layers, char], char, charMat, camera, grade }, room: layers };
}

test('hair motion never reaches the room: only the hair mesh moves, inside the character layer', async () => {
  const { HairLayer, HAIR_ORDER } = await load('hair_layer.js');
  const { stage, room } = await fakeStage();
  const snap = () => JSON.stringify({
    room: room.map((l) => [l.name, l.mesh.position.toArray(), l.mesh.renderOrder, l.mesh.material.uniforms.uLevel.value]),
    cam: [stage.camera.left, stage.camera.right, stage.camera.top, stage.camera.bottom, stage.camera.position.toArray()],
    char: stage.char.mesh.position.toArray(),
    grade: [stage.grade.uDim.value, stage.grade.uDesat.value, stage.grade.uAmbient.value.toArray()],
    children: stage.scene.children.map((c) => c.name),
  });
  const before = snap();
  const hair = new HairLayer(stage, { meta: META, tex: { hair: null, fx: null, mask: null } });
  const added = stage.scene.children.filter((c) => c.name === 'hair');
  assert.equal(added.length, 1, 'one mesh added');
  assert.ok(HAIR_ORDER > stage.char.mesh.renderOrder && HAIR_ORDER < 10.3, 'drawn right after the character, before the front props');
  const ref = JSON.parse(before);
  ref.children.push('hair');
  const cu = stage.charMat.uniforms;
  let moved = 0;
  let last = null;
  for (let i = 0; i < 600; i += 1) {
    cu.uHead.value.set(Math.round(Math.sin(i * 0.21) * 3), Math.round(Math.cos(i * 0.13) * 2));
    cu.uBreath.value = i % 120 < 60 ? -1 : 0;
    const pose = i < 300 ? { emotion: 'tense', ears: -3, hair: 'bristle' } : { emotion: 'joy', ears: 1, hair: 'bounce' };
    const off = hair.update({ pose, ch: { 'hair.sway': Math.sin(i * 0.05) * 2, 'head.y': 3, 'cam.x': 3 }, dt: 1 / 60 });
    const key = JSON.stringify(off);
    if (key !== last) moved += 1;
    last = key;
    const now = JSON.parse(snap());
    assert.deepEqual(now, ref, `frame ${i}: room, camera, grade and the character layer untouched`);
    assert.deepEqual(hair.mesh.position.toArray(), stage.char.mesh.position.toArray(), 'the hair is placed with the character');
  }
  assert.ok(moved > 50, 'the hair did move');
  const src = fs.readFileSync(path.join(ROOT, 'js', 'hair_layer.js'), 'utf8');
  assert.doesNotMatch(src, /roomFrame|layerOffset|\.camera|parallax|stage\.layers|gadgets/, 'hair_layer.js reads no room input');
  assert.doesNotMatch(src.replace(/cu\.\w+\.value\.(x|y)|cu\.\w+\.value(?!\s*=)/g, ''), /cu\.\w+\.value\s*=|charMat\.uniforms\.\w+\.value\s*=/, 'it never writes the character uniforms');
  // main.js: the hair is updated after the scene and before the render
  const main = fs.readFileSync(path.join(ROOT, 'js', 'main.js'), 'utf8');
  const loop = main.slice(main.indexOf('scene.update('), main.indexOf('scene.render()'));
  assert.match(loop, /hair\.update\(\{ pose: derived\.pose, ch: numeric, dt(, head: (charLayer\.hairInput\(\)|hin))? \}\)/);
});

test('views: the part set is data per view and can be swapped (front now, a back view later)', async () => {
  const { HAIR_VIEWS, HairLayer } = await load('hair_layer.js');
  assert.deepEqual(HAIR_VIEWS, { front: 'hair', back: 'hair_back' }); // back: tools/char_back.py
  assert.equal(META.view, 'front');
  const { stage } = await fakeStage();
  const hair = new HairLayer(stage, { meta: META, tex: { hair: null, fx: null, mask: null } });
  assert.equal(await hair.setView('nope'), false, 'unknown views are refused');
  assert.equal(await hair.setView('front'), true);
  // a second set (here the same data under another view id) replaces the material, not the mesh
  const mesh = hair.mesh;
  hair.use({ meta: { ...META, view: 'back', segments: META.segments.filter((s) => s.part === 'back') }, tex: { hair: null, fx: null, mask: null } }, 'back');
  assert.equal(hair.mesh, mesh);
  assert.equal(hair.view, 'back');
  assert.ok(Object.keys(hair.info().offsets).every((id) => id.startsWith('back_')));
});
