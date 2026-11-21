// Stage idle motion (#669): no fixed period. The hair sways on seeded value noise, the breath
// is uneven (a shorter rise, a longer fall, a rest) and its tempo drifts. All of it stays
// data in the catalog, deterministic for a seed and driven by dt.
// node --test tests/stage_idle_test.cjs
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { pathToFileURL } = require('node:url');

const ROOT = path.join(__dirname, '..', 'dashboard', 'stage');
const load = (name) => import(pathToFileURL(path.join(ROOT, 'js', name)).href);
const META = JSON.parse(fs.readFileSync(path.join(ROOT, 'assets', 'char', 'hair.json'), 'utf8'));
const POSE = { emotion: 'calm', eyes: 'open', mouth: 'neutral', headY: 0, ears: 0, blush: 0, sweat: 0, breathRate: 1 };

async function setup() {
  const { buildCatalog, CATALOG, validateEntry } = await load('catalog.js');
  const { Animator, evalCurve } = await load('anim.js');
  return { map: buildCatalog(CATALOG).map, CATALOG, validateEntry, Animator, evalCurve };
}

// channel values at a fixed frame rate: [{ t, sway, breath }]
function trace(Animator, map, seconds, fps = 60, pose = POSE) {
  const a = new Animator(map, 341);
  const out = [];
  for (let f = 0; f < seconds * fps; f += 1) {
    const t = f / fps;
    const { numeric } = a.tick(t, pose);
    out.push({ t, sway: numeric['hair.sway'], breath: numeric['body.breath'] });
  }
  return out;
}

// times at which the breath pixel (motion.js: body.breath > 0.35) goes up
function breathStarts(tr) {
  const out = [];
  for (let i = 1; i < tr.length; i += 1) if (tr[i - 1].breath <= 0.35 && tr[i].breath > 0.35) out.push(tr[i].t);
  return out;
}
const gaps = (ts) => ts.slice(1).map((t, i) => t - ts[i]);

test('catalog: the noise curve is data (clamped numbers), for idle entries and numeric channels only', async () => {
  const { validateEntry } = await setup();
  const idle = (channels, extra = {}) => validateEntry({ id: 'idle.x', scope: 'idle', trigger: { always: true }, channels, ...extra });
  const e = idle({ 'hair.sway': { fn: 'noise', amp: 99, scale: 0.001, seed: -5.5, octaves: 9, offset: 7, src: 'alert(1)' } });
  assert.deepEqual({ ...e.channels['hair.sway'] }, { fn: 'noise', amp: 2, scale: 0.25, seed: 0, octaves: 3, offset: 2 });
  assert.deepEqual({ ...idle({ 'hair.sway': { fn: 'noise' } }).channels['hair.sway'] }, { fn: 'noise', amp: 0, scale: 1, seed: 0, octaves: 1, offset: 0 });
  assert.equal(idle({ eyes: { fn: 'noise', amp: 1, scale: 1 } }), null, 'enum channel');
  const noise = { fn: 'noise', amp: 1, scale: 8 };
  assert.equal(validateEntry({ id: 'gesture.x', scope: 'gesture', duration: 1, channels: { 'head.x': noise } }), null, 'gesture');
  assert.equal(validateEntry({ id: 'state.x', scope: 'state', trigger: { state: 'steam' }, channels: { 'steam.puff': noise } }), null, 'state');
  assert.equal(validateEntry({ id: 'ambient.x', scope: 'ambient', trigger: { always: true }, loop: true, duration: 8, channels: { 'neon.ambient': noise } }), null, 'ambient');
});

test('catalog: rateJitter is clamped data and only for always-on idle entries', async () => {
  const { validateEntry } = await setup();
  const ch = { 'body.breath': { fn: 'const', value: 1 } };
  const e = validateEntry({ id: 'idle.x', scope: 'idle', trigger: { always: true }, channels: ch, rateJitter: { amp: 9, scale: 0, seed: 1e12, code: 'x' } });
  assert.deepEqual({ ...e.rateJitter }, { amp: 0.3, scale: 2, seed: 2147483647 });
  assert.equal(validateEntry({ id: 'idle.x', scope: 'idle', trigger: { always: true }, channels: ch }).rateJitter, null);
  assert.equal(validateEntry({ id: 'idle.x', scope: 'idle', trigger: { always: true }, channels: ch, rateJitter: 'fast' }), null);
  assert.equal(validateEntry({ id: 'idle.x', scope: 'idle', trigger: { every: [1, 2] }, channels: ch, rateJitter: { amp: 0.1 } }), null);
  assert.equal(validateEntry({ id: 'gesture.x', scope: 'gesture', channels: { 'head.x': { fn: 'const', value: 1 } }, rateJitter: { amp: 0.1 } }), null);
  assert.equal(validateEntry({ id: 'ambient.x', scope: 'ambient', trigger: { always: true }, loop: true, duration: 8, channels: { 'neon.ambient': { fn: 'sine', amp: 0.05, period: 8 } }, rateJitter: { amp: 0.1 } }), null);
});

test('noise curve: same seed and time give the same value; bounded, smooth, different per seed', async () => {
  const { evalCurve } = await setup();
  const c = { fn: 'noise', amp: 1.4, scale: 2.6, seed: 669, octaves: 2, offset: 0 };
  let prev = evalCurve(c, 0);
  let differs = 0;
  const seen = [];
  for (let f = 1; f < 60 * 120; f += 1) {
    const t = f / 60;
    const v = evalCurve(c, t);
    assert.equal(v, evalCurve({ ...c }, t));
    assert.ok(Math.abs(v) <= 1.4, `in range at ${t}: ${v}`);
    assert.ok(Math.abs(v - prev) < 0.06, `no jump at ${t}: ${prev} -> ${v}`);
    if (Math.abs(v - evalCurve({ ...c, seed: 670 }, t)) > 0.05) differs += 1;
    prev = v;
    seen.push(v);
  }
  assert.ok(differs > 60 * 60, 'another seed is another curve');
  assert.ok(Math.max(...seen) > 0.7 && Math.min(...seen) < -0.7, 'it uses its range');
  assert.equal(evalCurve({ ...c, offset: 0.5, amp: 0 }, 3.3), 0.5);
});

test('idle.hair: the sway is noise — no jump at the entry duration, no period, same run twice', async () => {
  const { map, Animator } = await setup();
  assert.equal(map.get('idle.hair').channels['hair.sway'].fn, 'noise');
  const tr = trace(Animator, map, 120);
  assert.deepEqual(trace(Animator, map, 120), tr, 'deterministic');
  for (let i = 1; i < tr.length; i += 1) {
    assert.ok(Math.abs(tr[i].sway) <= 2, 'channel range');
    assert.ok(Math.abs(tr[i].sway - tr[i - 1].sway) < 0.06, `sway jumps at ${tr[i].t}`);
  }
  // the curve never comes back onto itself: for every shift of 2..40 s it differs somewhere
  for (let shift = 2; shift <= 40; shift += 0.1) {
    const k = Math.round(shift * 60);
    let worst = 0;
    for (let i = 0; i + k < tr.length; i += 6) worst = Math.max(worst, Math.abs(tr[i].sway - tr[i + k].sway));
    assert.ok(worst > 0.5, `repeats after ${shift.toFixed(1)} s (max difference ${worst})`);
  }
});

test('idle.hair on the rig: whole pixels within one of rest, both sides, no flicker, uneven timing', async () => {
  const { map, Animator } = await setup();
  const { createHairRig, initHairState, stepHair } = await load('hair_rig.js');
  const rig = createHairRig(META);
  const run = (fps) => {
    const tr = trace(Animator, map, 300, fps);
    let hair = initHairState(rig, { sway: tr[0].sway });
    const px = [];
    for (const s of tr) {
      hair = stepHair(rig, hair, { head: [0, 0], breath: 0, sway: s.sway, pose: POSE }, 1 / fps);
      px.push(hair.swayPx);
    }
    return px;
  };
  const px = run(60);
  const share = (v) => px.filter((p) => p === v).length / px.length;
  assert.deepEqual([...new Set(px)].sort(), [-1, 0, 1], 'one pixel either way, as before');
  for (const v of [-1, 0, 1]) assert.ok(share(v) > 0.12, `${v} px for ${share(v)} of the time`);
  const changes = [];
  for (let i = 1; i < px.length; i += 1) if (px[i] !== px[i - 1]) changes.push(i / 60);
  const dwell = gaps(changes);
  assert.ok(changes.length >= 300 / 6 && changes.length <= 300 / 1.2, `${changes.length} changes in 300 s`);
  assert.ok(Math.min(...dwell) >= 0.3, `shortest dwell ${Math.min(...dwell)} s (flicker)`);
  assert.ok(Math.max(...dwell) - Math.min(...dwell) > 2, 'the dwell times are not all alike');
  // the same sway at 30 fps: the pixel agrees with the 60 fps run nearly all the time
  const px30 = run(30);
  let same = 0;
  for (let i = 0; i < px30.length; i += 1) if (px30[i] === px[i * 2]) same += 1;
  assert.ok(same / px30.length > 0.97, `30 fps agrees with 60 fps for ${same / px30.length}`);
});

test('idle.breath: a shorter rise than fall, a rest, and a period that drifts inside its band', async () => {
  const { map, Animator } = await setup();
  const e = map.get('idle.breath');
  assert.ok(e.rateJitter && e.rateJitter.amp > 0);
  const tr = trace(Animator, map, 240);
  assert.deepEqual(trace(Animator, map, 240), tr, 'deterministic');
  for (const s of tr) assert.ok(s.breath >= -1 && s.breath <= 1);
  for (let i = 1; i < tr.length; i += 1) assert.ok(Math.abs(tr[i].breath - tr[i - 1].breath) < 0.08, `breath jumps at ${tr[i].t}`);
  // rise / fall / rest shares of the time
  let up = 0;
  let down = 0;
  let rest = 0;
  for (let i = 1; i < tr.length; i += 1) {
    const d = tr[i].breath - tr[i - 1].breath;
    if (d > 1e-9) up += 1;
    else if (d < -1e-9) down += 1;
    else rest += 1;
  }
  const n = tr.length - 1;
  assert.ok(up / n > 0.35 && up / n < 0.45, `rise ${up / n}`);
  assert.ok(down / n > 0.43 && down / n < 0.53, `fall ${down / n}`);
  assert.ok(rest / n > 0.07 && rest / n < 0.17, `rest ${rest / n}`);
  // the period: around the entry's duration, never the same twice in a row for long
  const g = gaps(breathStarts(tr));
  const band = [e.duration / (1 + e.rateJitter.amp), e.duration / (1 - e.rateJitter.amp)];
  assert.ok(g.length > 45);
  for (const x of g) assert.ok(x > band[0] - 0.05 && x < band[1] + 0.05, `period ${x} outside ${band}`);
  assert.ok(Math.max(...g) - Math.min(...g) > 0.5, `periods ${Math.min(...g)}..${Math.max(...g)}`);
  // the chest pixel is up for about a third of a breath, as with the old sine (0.39)
  const upShare = tr.filter((s) => s.breath > 0.35).length / tr.length;
  assert.ok(upShare > 0.28 && upShare < 0.42, `chest up ${upShare}`);
});

test('idle: the amount of motion does not depend on the frame rate, the tempo still follows breathRate', async () => {
  const { map, Animator } = await setup();
  const at = (fps, pose = POSE) => trace(Animator, map, 90, fps, pose);
  const r60 = at(60);
  const r30 = at(30);
  const r20 = at(20);
  const n60 = breathStarts(r60);
  for (const [name, r] of [['30', r30], ['20', r20]]) {
    const n = breathStarts(r);
    assert.equal(n.length, n60.length, `${name} fps: breaths`);
    n.forEach((t, i) => assert.ok(Math.abs(t - n60[i]) < 0.12, `${name} fps: breath ${i} at ${t} vs ${n60[i]}`));
  }
  // the sway is a function of time alone
  for (let i = 0; i < r30.length; i += 1) assert.ok(Math.abs(r30[i].sway - r60[i * 2].sway) < 1e-9);
  const fast = breathStarts(at(60, { ...POSE, breathRate: 1.6 })).length;
  assert.ok(fast > n60.length * 1.4 && fast < n60.length * 1.8, `${fast} vs ${n60.length}`);
});

test('docs/stage.md names the noise curve and the breath drift', () => {
  const md = fs.readFileSync(path.join(__dirname, '..', 'docs', 'stage.md'), 'utf8');
  assert.match(md, /`const`, `sine`, `pulse`, `noise`, `keys`/);
  assert.match(md, /`rateJitter`/);
});
