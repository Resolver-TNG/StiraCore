// Stage spike (#341): state mapping, catalog validation and interpreter safety.
// node --test tests/stage_model_test.cjs
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { pathToFileURL } = require('node:url');

const STAGE = path.join(__dirname, '..', 'dashboard', 'stage', 'js');
const load = (name) => import(pathToFileURL(path.join(STAGE, name)).href);

const N = (values) => ({ type: 'neural', values });

test('derive is deterministic and independent of the path to a state', async () => {
  const { initialState, reduce, derive, poseSignature } = await load('model.js');
  const a = [N({ stability: 0.42 }), { type: 'breaker', level: 'WARNING' }, { type: 'turn.start' }];
  const b = [{ type: 'turn.start' }, { type: 'breaker', level: 'CRITICAL' }, N({ stability: 0.9 }),
    { type: 'breaker', level: 'WARNING' }, N({ stability: 0.42 })];
  const run = (evs) => evs.reduce(reduce, initialState());
  const sa = run(a);
  const sb = run(b);
  assert.equal(poseSignature(derive(sa).pose), poseSignature(derive(sb).pose));
  assert.equal(poseSignature(derive(sa).pose), poseSignature(derive(sa).pose));
  assert.deepEqual(derive(sa).pose, derive(structuredClone(sa)).pose);
});

test('every step of the demo maps to a distinct, stable pose', async () => {
  const { initialState, reduce, derive, poseSignature } = await load('model.js');
  const { STEPS } = await load('director.js');
  const sigs = new Map();
  for (const step of STEPS) {
    const s = step.events.reduce(reduce, initialState());
    const sig = poseSignature(derive(s).pose);
    for (let i = 0; i < 5; i += 1) assert.equal(poseSignature(derive(step.events.reduce(reduce, initialState())).pose), sig);
    sigs.set(step.id, sig);
  }
  assert.equal(new Set(sigs.values()).size, STEPS.length, 'two demo states look the same');
});

test('emotion table: breaker wins over neural, then satisfaction, then curiosity', async () => {
  const { initialState, reduce, derive } = await load('model.js');
  const pose = (...evs) => derive(evs.reduce(reduce, initialState())).pose;
  assert.equal(pose().emotion, 'calm');
  assert.equal(pose(N({ satisfaction: 0.9 })).emotion, 'joy');
  assert.equal(pose(N({ curiosity: 0.9 })).emotion, 'curious');
  assert.equal(pose(N({ satisfaction: 0.9 }), { type: 'breaker', level: 'WARNING' }).emotion, 'worried');
  assert.equal(pose(N({ satisfaction: 0.9 }), { type: 'breaker', level: 'CRITICAL' }).emotion, 'tense');
  assert.equal(pose({ type: 'breaker', level: 'CRITICAL' }).neon, 'red');
  assert.equal(pose({ type: 'approval.request', id: 'a' }).neon, 'amber');
  assert.equal(pose({ type: 'breaker', level: 'CRITICAL' }, { type: 'approval.request', id: 'a' }).neon, 'red');
});

test('work indicators come only from events (no event, no busy look)', async () => {
  const { initialState, reduce, derive } = await load('model.js');
  const idle = derive(initialState()).pose;
  for (const k of ['steam', 'light.desk', 'light.shelf', 'light.window', 'choice', 'sparkle', 'shake']) assert.equal(idle[k], 0, k);
  let s = reduce(initialState(), { type: 'turn.start' });
  assert.equal(derive(s).pose.steam, 2);
  s = reduce(s, { type: 'tool.start', id: 'x', kind: 'memory', name: 'memory_search' });
  assert.equal(derive(s).pose['light.shelf'], 1);
  s = reduce(s, { type: 'tool.end', id: 'x' });
  assert.equal(derive(s).pose['light.shelf'], 0);
  s = reduce(s, { type: 'turn.end', ok: true });
  assert.equal(derive(s).pose.steam, 0);
  const { causes } = derive(reduce(initialState(), { type: 'turn.start' }));
  assert.match(causes.find((c) => c.channel === 'steam').cause, /#1 turn\.start/);
});

test('unknown or malformed events leave the state untouched', async () => {
  const { initialState, reduce } = await load('model.js');
  const s = initialState();
  for (const ev of [null, 1, {}, { type: 'eval', code: 'x' }, { type: 'breaker', level: 'PANIC' }]) assert.equal(reduce(s, ev), s);
  const n = reduce(s, N({ trust: 5, curiosity: 'high', stability: Number.NaN }));
  assert.equal(n.neural.trust, 1);
  assert.equal(n.neural.curiosity, 0.5);
  assert.equal(n.neural.stability, 0.7);
});

test('drift monitor flags one input key mapping to two poses', async () => {
  const { initialState, derive, DriftMonitor } = await load('model.js');
  const m = new DriftMonitor();
  const s = initialState();
  assert.equal(m.record(s, derive(s).pose), true);
  assert.equal(m.record(s, derive(s).pose), true);
  assert.equal(m.record(s, { ...derive(s).pose, eyes: 'happy' }), false);
  assert.equal(m.drifts.length, 1);
});

test('catalog: the built-in entries all validate', async () => {
  const { buildCatalog, CATALOG } = await load('catalog.js');
  const { map, rejected } = buildCatalog(CATALOG);
  assert.deepEqual(rejected, []);
  assert.equal(map.size, CATALOG.length);
  assert.ok(Object.isFrozen(map.get('idle.blink').channels));
});

test('catalog: code, unknown channels and out-of-scope channels are rejected', async () => {
  const { validateEntry } = await load('catalog.js');
  const ok = { id: 'gesture.tilt', scope: 'gesture', duration: 1, channels: { 'head.x': { fn: 'sine', amp: 1, period: 1 } } };
  assert.ok(validateEntry(ok));
  assert.equal(validateEntry({ ...ok, channels: { 'head.x': () => 1 } }), null);
  assert.equal(validateEntry({ ...ok, channels: { 'head.x': { fn: 'eval', src: 'alert(1)' } } }), null);
  assert.equal(validateEntry({ ...ok, channels: { 'document.body': { fn: 'const', value: 1 } } }), null);
  assert.equal(validateEntry({ ...ok, channels: { 'light.desk.pulse': { fn: 'const', value: 1 } } }), null, 'gesture faking a tool light');
  assert.equal(validateEntry({ ...ok, channels: { 'steam.puff': { fn: 'const', value: 3 } } }), null, 'gesture faking steam');
  assert.equal(validateEntry({ ...ok, channels: { eyes: { fn: 'keys', keys: [[0, '<img src=x>']] } } }), null);
  assert.equal(validateEntry({ ...ok, id: 'Bad Id!' }), null);
  assert.equal(validateEntry({ ...ok, scope: 'root' }), null);
  assert.equal(validateEntry({ ...ok, trigger: { state: 'steam' } }), null, 'gestures cannot bind to state');
  const big = validateEntry({ ...ok, channels: { 'head.x': { fn: 'sine', amp: 999, period: 1 } } });
  assert.equal(big.channels['head.x'].amp, 3, 'amplitudes are clamped to the channel range');
});

test('animator: agent may select gestures only; unknown ids are ignored', async () => {
  const { buildCatalog, CATALOG } = await load('catalog.js');
  const { Animator } = await load('anim.js');
  const { map } = buildCatalog(CATALOG);
  const a = new Animator(map, 1);
  assert.deepEqual(a.request('dance.backflip', 0), { ok: false, reason: 'unknown id' });
  assert.equal(a.request('state.steam', 0).ok, false);
  assert.equal(a.request({ toString: () => 'gesture.nod' }, 0).ok, false);
  assert.equal(a.request('gesture.nod', 0).ok, true);
  assert.equal(a.request('gesture.nod', 0.5).reason, 'cooldown');
  const pose = { emotion: 'calm', eyes: 'open', steam: 0 };
  const out = a.tick(0.2, pose);
  assert.ok(out.numeric['head.y'] > 0);
  assert.equal(out.numeric['steam.puff'] || 0, 0, 'no steam without the state');
});

test('animator: gesture expressions do not override worried/tense faces', async () => {
  const { buildCatalog, CATALOG } = await load('catalog.js');
  const { Animator } = await load('anim.js');
  const { map } = buildCatalog(CATALOG);
  const a = new Animator(map, 1);
  a.request('gesture.glance-up', 0);
  assert.equal(a.tick(0.1, { emotion: 'calm', eyes: 'open' }).enums.eyes, 'up');
  assert.equal(a.tick(0.2, { emotion: 'tense', eyes: 'wide' }).enums.eyes, undefined);
});

test('animator: idle timing is reproducible from the seed', async () => {
  const { buildCatalog, CATALOG } = await load('catalog.js');
  const { Animator } = await load('anim.js');
  const { map } = buildCatalog(CATALOG);
  const trace = () => {
    const a = new Animator(map, 7);
    const eyes = [];
    for (let t = 0; t < 20; t += 0.05) eyes.push(a.tick(t, { emotion: 'calm', eyes: 'open' }).enums.eyes || '-');
    return eyes.join('');
  };
  assert.equal(trace(), trace());
});

test('stage sources: no eval, no Function, no HTML parsing, no remote URLs', () => {
  const dir = path.join(__dirname, '..', 'dashboard', 'stage');
  const files = [...fs.readdirSync(path.join(dir, 'js')).map((f) => path.join(dir, 'js', f)), path.join(dir, 'index.html'), path.join(dir, 'stage.css')];
  for (const f of files) {
    const src = fs.readFileSync(f, 'utf8');
    assert.doesNotMatch(src, /\beval\s*\(|new Function|innerHTML|outerHTML|insertAdjacentHTML|document\.write/, f);
    // XML namespace names (identifiers, never loaded: svg_safe.js, #453) are the one exception
    assert.doesNotMatch(src.replace(/'http:\/\/www\.w3\.org\/(?:2000\/svg|1999\/xlink)'/g, "''"), /https?:\/\/(?!127\.0\.0\.1)/, f);
  }
  const html = fs.readFileSync(path.join(dir, 'index.html'), 'utf8');
  assert.doesNotMatch(html, /<script(?![^>]*\bsrc=)[^>]*>/, 'inline script');
  assert.doesNotMatch(html, /\son[a-z]+=/i, 'inline handler');
  assert.doesNotMatch(html, /\sstyle=/i, 'inline style attribute');
});

test('docs/stage.md emotion table matches the code', async () => {
  const { EMOTION_RULES, EXPRESSIONS } = await load('model.js');
  const md = fs.readFileSync(path.join(__dirname, '..', 'docs', 'stage.md'), 'utf8');
  const block = md.split('<!-- stage-emotions:begin -->')[1].split('<!-- stage-emotions:end -->')[0];
  const rows = block.trim().split('\n').slice(2).map((r) => r.split('|').slice(1, -1).map((c) => c.trim()));
  assert.deepEqual(rows.map((r) => r[0]), EMOTION_RULES.map((r) => r.emotion));
  const px = (s) => (s === '0' ? 0 : (s.startsWith('down') || s.startsWith('back') ? 1 : -1) * Number(s.match(/\d+/)[0]));
  for (const [emotion, when, eyes, mouth, head, ears, lighting] of rows) {
    const ex = EXPRESSIONS[emotion];
    assert.equal(when, EMOTION_RULES.find((r) => r.emotion === emotion).when, emotion);
    assert.equal(eyes, ex.eyes, emotion);
    assert.equal(mouth, ex.mouth, emotion);
    assert.equal(px(head), ex.headY, emotion);
    assert.equal(-px(ears) || 0, ex.ears, emotion);
    const dim = lighting.match(/dim ([0-9.]+)/);
    const desat = lighting.match(/desaturate ([0-9.]+)/);
    assert.equal(dim ? Number(dim[1]) : Math.min(ex.dim, 0), dim ? ex.dim : Math.min(ex.dim, 0), emotion);
    assert.equal(desat ? Number(desat[1]) : 0, ex.desat, emotion);
  }
});

test('animator: breathing tempo follows the derived breathRate without jumps', async () => {
  const { buildCatalog, CATALOG, validateEntry } = await load('catalog.js');
  const { Animator } = await load('anim.js');
  const { map } = buildCatalog(CATALOG);
  const breath = (rate) => {
    const a = new Animator(map, 1);
    let crossings = 0;
    let prev = 0;
    for (let t = 0; t <= 21; t += 0.02) {
      const v = a.tick(t, { emotion: 'calm', eyes: 'open', breathRate: rate }).numeric['body.breath'];
      if (prev <= 0 && v > 0) crossings += 1;
      prev = v;
    }
    return crossings;
  };
  assert.ok(breath(1.6) > breath(0.8));
  assert.equal(validateEntry({ id: 'gesture.x', scope: 'gesture', rateFrom: 'breathRate', channels: { 'head.x': { fn: 'const', value: 1 } } }), null);
  assert.equal(validateEntry({ id: 'idle.x', scope: 'idle', trigger: { always: true }, rateFrom: 'stability', channels: { 'head.x': { fn: 'const', value: 1 } } }), null);
});
