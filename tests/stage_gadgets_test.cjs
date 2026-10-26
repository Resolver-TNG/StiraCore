// Stage gadgets (#341, stage 2): event categories -> props, determinism, catalog safety.
// node --test tests/stage_gadgets_test.cjs
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { pathToFileURL } = require('node:url');

const ROOT = path.join(__dirname, '..', 'dashboard', 'stage');
const load = (name) => import(pathToFileURL(path.join(ROOT, 'js', name)).href);
const N = (values) => ({ type: 'neural', values });
const T = { type: 'turn.start' };
const start = (id, kind, name, extra = {}) => ({ type: 'tool.start', id, kind, name, ...extra });
const end = (id, ok = true) => ({ type: 'tool.end', id, ok });

async function poseOf(...evs) {
  const { initialState, reduce, derive } = await load('model.js');
  return derive(evs.reduce(reduce, initialState())).pose;
}
const gadgetsOn = (pose) => Object.keys(pose).filter((k) => k.startsWith('gadget.') && ![0, 'off', 'none'].includes(pose[k])).sort();

test('no event, no gadget: the initial state and a plain turn show no prop', async () => {
  assert.deepEqual(gadgetsOn(await poseOf()), []);
  assert.deepEqual(gadgetsOn(await poseOf(T)), []);
  assert.deepEqual(gadgetsOn(await poseOf(T, start('x', 'tool', 'calc'))), []);
  assert.deepEqual(gadgetsOn(await poseOf(T, { type: 'turn.end', ok: true })), []);
});

test('coding: laptop while code runs, typing, and the typing motion is named', async () => {
  const p = await poseOf(N({}), T, start('e', 'code', 'editor', { write: true }));
  assert.equal(p['gadget.laptop'], 1);
  assert.equal(p['gadget.typing'], 1);
  assert.equal(p['gadget.motion'], 'typing');
  // the laptop stays for the rest of the turn, not typing, and leaves with the turn
  const after = await poseOf(T, start('e', 'code', 'editor'), end('e'));
  assert.equal(after['gadget.laptop'], 1);
  assert.equal(after['gadget.typing'], 0);
  assert.equal(after['gadget.motion'], 'look_screen');
  assert.equal((await poseOf(T, start('e', 'code', 'editor'), end('e'), { type: 'turn.end', ok: true }))['gadget.laptop'], 0);
  // the breaker comes first: WARNING while coding asks for the alert motion, not typing
  const worried = await poseOf({ type: 'breaker', level: 'WARNING' }, T, start('e', 'code', 'editor'));
  assert.equal(worried['gadget.laptop'], 1);
  assert.equal(worried['gadget.motion'], 'alert');
  // the props do not touch the face: expression stays the spike's emotion mapping
  assert.equal(p.eyes, 'up', 'eyes follow the spike rule (turn open), not the prop');
  assert.equal(p.headY, 0);
  assert.equal(p.headX, undefined);
});

test('tests: lamp amber while running, green on pass, red on fail (+ flicker, slump motion)', async () => {
  const base = [T, start('t', 'test', 'run_tests')];
  assert.equal((await poseOf(...base))['gadget.lamp'], 'run');
  const pass = await poseOf(...base, end('t', true));
  assert.equal(pass['gadget.lamp'], 'pass');
  assert.equal(pass['gadget.glitch'], 'off');
  const fail = await poseOf(...base, end('t', false));
  assert.equal(fail['gadget.lamp'], 'fail');
  assert.equal(fail['gadget.glitch'], 'laptop');
  assert.equal(fail['gadget.motion'], 'slump');
  assert.equal(pass['gadget.motion'], 'relief');
  // a retry clears the failure mark, the next turn clears the result
  const retry = await poseOf(...base, end('t', false), start('t2', 'test', 'run_tests'));
  assert.equal(retry['gadget.glitch'], 'off');
  assert.equal(retry['gadget.lamp'], 'run');
  assert.equal((await poseOf(...base, end('t', false), { type: 'turn.end' }, T))['gadget.lamp'], 'off');
});

test('subagents: one drone per agent, report on done, failure stays visible', async () => {
  const spawn = [T, start('a', 'subagent', 'r'), start('b', 'subagent', 'v'), start('c', 'subagent', 't')];
  let p = await poseOf(...spawn);
  assert.equal(p['gadget.drones'], 'run,run,run');
  assert.equal(p['gadget.drones.running'], 3);
  p = await poseOf(...spawn, end('a', true), end('b', false));
  assert.equal(p['gadget.drones'], 'done,err,run');
  assert.equal(p['gadget.drones.running'], 1);
  // turn end: the reported one has docked, the failed one is still shown
  p = await poseOf(...spawn, end('a', true), end('b', false), end('c', true), { type: 'turn.end', ok: true });
  assert.equal(p['gadget.drones'], 'err');
  // the next turn starts clean (the failure is in the event log / raw view)
  p = await poseOf(...spawn, end('a', true), end('b', false), { type: 'turn.end', ok: true }, T);
  assert.equal(p['gadget.drones'], 'run');
  // subagents are not tools: they do not light the desk instrument
  assert.equal((await poseOf(...spawn))['light.desk'], 0);
  // bounded: at most 12 agents are tracked, running ones first
  const many = [T, ...Array.from({ length: 20 }, (_, i) => start(`x${i}`, 'subagent', 'w'))];
  assert.equal((await poseOf(...many))['gadget.drones'].split(',').length, 12);
});

test('memory, files, web, approvals, breaker, scheduler, forgetting map to their props', async () => {
  const read = await poseOf(T, start('m', 'memory', 'memory_search', { layer: 'episodic' }));
  assert.equal(read['gadget.folder'], 1);
  assert.equal(read['gadget.shelf.row'], 'episodic');
  const write = await poseOf(T, start('m', 'memory', 'memory_write', { write: true, layer: 'semantic' }));
  assert.equal(write['gadget.note'], 1);
  assert.equal(write['gadget.folder'], 0);
  assert.equal(write['gadget.shelf.row'], 'semantic');
  assert.equal((await poseOf(T, start('f', 'file', 'file_read')))['gadget.folder'], 1);
  assert.equal((await poseOf(T, start('w', 'web', 'web_fetch')))['gadget.magnifier'], 1);
  const webFail = await poseOf(T, start('w', 'web', 'web_fetch'), end('w', false));
  assert.equal(webFail['gadget.magnifier'], 1, 'a failed fetch keeps its prop, marked');
  assert.equal(webFail['gadget.glitch'], 'magnifier');
  const req = { type: 'approval.request', id: 'a1', summary: 's' };
  assert.equal((await poseOf(T, req, { type: 'approval.resolved', id: 'a1', decision: 'approved' }))['gadget.stamp'], 'approve');
  assert.equal((await poseOf(T, req, { type: 'approval.resolved', id: 'a1', decision: 'deny' }))['gadget.stamp'], 'deny');
  assert.equal((await poseOf(T, req, { type: 'approval.resolved', id: 'a1' }))['gadget.stamp'], 'off');
  assert.equal((await poseOf({ type: 'breaker', level: 'WARNING' }))['gadget.beacon'], 'warning');
  assert.equal((await poseOf({ type: 'breaker', level: 'CRITICAL' }))['gadget.beacon'], 'critical');
  assert.equal((await poseOf({ type: 'job.start', id: 'j', name: 'heartbeat' }))['gadget.hourglass'], 1);
  assert.equal((await poseOf({ type: 'job.start', id: 'j' }, { type: 'job.end', id: 'j' }))['gadget.hourglass'], 0);
  assert.equal((await poseOf({ type: 'memory.forget', count: 3 }))['gadget.box'], 1);
  assert.equal((await poseOf({ type: 'memory.forget', count: 3 }, { type: 'memory.restore', count: 3 }))['gadget.box'], 0);
  // malformed gadget events are ignored
  const { initialState, reduce } = await load('model.js');
  const s = initialState();
  for (const ev of [{ type: 'memory.forget', count: -1 }, { type: 'memory.forget', count: 'x' }, { type: 'job.end', id: 'nope' }]) assert.equal(reduce(s, ev), s);
});

test('every demo state (spike + gadgets) maps to a distinct, repeatable pose', async () => {
  const { initialState, reduce, derive, poseSignature } = await load('model.js');
  const { STEPS } = await load('director.js');
  const { GADGET_STEPS } = await load('demo_gadgets.js');
  const all = [...STEPS, ...GADGET_STEPS];
  const sigs = new Map();
  for (const step of all) {
    const sig = poseSignature(derive(step.events.reduce(reduce, initialState())).pose);
    for (let i = 0; i < 3; i += 1) assert.equal(poseSignature(derive(step.events.reduce(reduce, initialState())).pose), sig, step.id);
    sigs.set(step.id, sig);
  }
  const seen = new Map();
  for (const [id, sig] of sigs) {
    assert.ok(!seen.has(sig), `${id} looks the same as ${seen.get(sig)}`);
    seen.set(sig, id);
  }
  // keys are unique and none of them shadows a control key
  const keys = all.map((s) => s.key);
  assert.equal(new Set(keys).size, keys.length);
  for (const k of ['d', 'l', '0']) assert.ok(!keys.includes(k), k);
});

test('same gadget state from different histories gives the same pose; the drift key sees gadgets', async () => {
  const { initialState, reduce, derive, poseSignature, inputKey } = await load('model.js');
  const run = (evs) => evs.reduce(reduce, initialState());
  const a = run([T, start('e', 'code', 'editor'), end('e'), start('t', 'test', 'run_tests'), end('t', true)]);
  const b = run([T, start('t0', 'test', 'run_tests'), end('t0', false), start('e', 'code', 'editor'), end('e'), start('t', 'test', 'run_tests'), end('t', true)]);
  assert.equal(poseSignature(derive(a).pose), poseSignature(derive(b).pose));
  assert.equal(inputKey(a), inputKey(b));
  const c = run([T, start('a1', 'subagent', 'x')]);
  const d = run([T, start('a1', 'subagent', 'x'), end('a1', false)]);
  assert.notEqual(inputKey(c), inputKey(d), 'drone status is part of the drift key');
  assert.notEqual(poseSignature(derive(c).pose), poseSignature(derive(d).pose));
});

test('every gadget value names its cause event', async () => {
  const { initialState, reduce, derive } = await load('model.js');
  const s = [T, start('e', 'code', 'editor'), start('a', 'subagent', 'review'), end('a', false)].reduce(reduce, initialState());
  const causes = derive(s).causes.filter((c) => c.channel.startsWith('gadget.'));
  assert.ok(causes.length >= 14);
  assert.match(causes.find((c) => c.channel === 'gadget.laptop').cause, /code running: editor \(#\d+ tool\.start\)/);
  assert.match(causes.find((c) => c.channel === 'gadget.drones').cause, /review:err \(#\d+ tool\.end\)/);
  for (const c of causes) assert.equal(typeof c.cause, 'string');
});

test('tool names map to categories (for adapters that only know the name)', async () => {
  const { classifyTool } = await load('gadget_model.js');
  assert.deepEqual(classifyTool('editor'), { kind: 'code', write: true });
  assert.deepEqual(classifyTool('lsp_diagnostics'), { kind: 'code', write: false });
  assert.deepEqual(classifyTool('memory_write'), { kind: 'memory', write: true });
  assert.equal(classifyTool('memory_search').kind, 'memory');
  assert.equal(classifyTool('web_fetch').kind, 'web');
  assert.equal(classifyTool('perplexity_search').kind, 'web');
  assert.equal(classifyTool('file_read').kind, 'file');
  assert.equal(classifyTool('sessions_spawn').kind, 'subagent');
  assert.equal(classifyTool('delegate_research').kind, 'subagent');
  assert.equal(classifyTool('something_else').kind, 'tool');
  assert.equal(classifyTool(42).kind, 'tool');
});

test('gadget catalog: built-in entries validate, point at real sprites and are frozen', async () => {
  const { buildGadgets, GADGETS, GADGET_ANIMS } = await load('gadget_catalog.js');
  const { buildCatalog, CATALOG } = await load('catalog.js');
  const meta = JSON.parse(fs.readFileSync(path.join(ROOT, 'assets', 'gadgets', 'gadgets.json'), 'utf8'));
  const { list, rejected } = buildGadgets(GADGETS, Object.keys(meta.sprites));
  assert.deepEqual(rejected, []);
  assert.equal(list.length, GADGETS.length);
  assert.ok(Object.isFrozen(list[0]) && Object.isFrozen(list[0].at));
  for (const name of ['drone.0', 'drone.1', 'bubble.ok', 'bubble.err', 'digits', 'seal.approve', 'seal.deny', 'stamper']) assert.ok(meta.sprites[name], name);
  const anim = buildCatalog([...CATALOG, ...GADGET_ANIMS]);
  assert.deepEqual(anim.rejected, []);
  for (const e of GADGET_ANIMS) assert.equal(e.scope, 'state', `${e.id} must run from state, not on request`);
});

test('gadget catalog: code, unknown programs/triggers/sprites and bad values are refused', async () => {
  const { validateGadget } = await load('gadget_catalog.js');
  const { validateEntry } = await load('catalog.js');
  const sprites = ['laptop', 'box'];
  const ok = { id: 'pc', show: 'gadget.laptop', program: 'laptop', sprite: 'laptop', at: [10, 10], depth: 1, layer: 'front' };
  assert.ok(validateGadget(ok, sprites));
  assert.equal(validateGadget({ ...ok, program: 'eval' }, sprites), null);
  assert.equal(validateGadget({ ...ok, program: () => 1 }, sprites), null);
  assert.equal(validateGadget({ ...ok, show: 'neural.trust' }, sprites), null, 'shown only from gadget channels');
  assert.equal(validateGadget({ ...ok, sprite: 'remote.png' }, sprites), null);
  assert.equal(validateGadget({ ...ok, sprite: 'https://x' }, sprites), null);
  assert.equal(validateGadget({ ...ok, layer: 'overlay' }, sprites), null);
  assert.equal(validateGadget({ ...ok, at: [1, 'x'] }, sprites), null);
  assert.equal(validateGadget({ ...ok, id: 'Bad Id' }, sprites), null);
  assert.equal(validateGadget({ ...ok, light: { at: [0, 0], r: 5, color: 'url(x)', level: 1 } }, sprites), null);
  assert.equal(validateGadget({ ...ok, narrow: { slots: [[0, 0]] } }, sprites), null, 'slots only for multi-instance props');
  assert.equal(validateGadget({ ...ok, depth: 99 }, sprites).depth, 1.6, 'numbers are clamped');
  // gestures (the only agent-selectable scope) cannot drive gadget channels
  assert.equal(validateEntry({ id: 'gesture.fake', scope: 'gesture', duration: 1, channels: { 'laptop.type': { fn: 'const', value: 1 } } }), null);
  assert.equal(validateEntry({ id: 'state.x', scope: 'state', trigger: { state: 'gadget.lamp', is: '<b>' }, channels: { 'lamp.blink': { fn: 'const', value: 1 } } }), null);
});

test('animator: value triggers (`is`) and off values of gadget channels', async () => {
  const { buildCatalog, CATALOG } = await load('catalog.js');
  const { GADGET_ANIMS } = await load('gadget_catalog.js');
  const { Animator } = await load('anim.js');
  const { map } = buildCatalog([...CATALOG, ...GADGET_ANIMS]);
  const a = new Animator(map, 1);
  const pose = (lamp, glitch = 'off') => ({ emotion: 'calm', eyes: 'open', 'gadget.lamp': lamp, 'gadget.glitch': glitch, 'gadget.drones': 'none' });
  a.tick(0, pose('pass'));
  assert.ok(!a.activeIds().includes('state.lamp-run'), 'pass does not blink');
  assert.ok(!a.activeIds().includes('state.glitch'), "'off' is off");
  a.tick(0.1, pose('run', 'laptop'));
  assert.ok(a.activeIds().includes('state.lamp-run'));
  assert.ok(a.activeIds().includes('state.glitch'));
  // the flicker plays once, then holds 0 (the failure stays marked, not flashing)
  let v = 0;
  for (let t = 0.2; t < 2; t += 0.05) v = a.tick(t, pose('run', 'laptop')).numeric['gadget.glitch'] || 0;
  assert.equal(v, 0);
});

test('docs/stage.md gadget table lists every prop with its trigger', async () => {
  const { GADGETS } = await load('gadget_catalog.js');
  const md = fs.readFileSync(path.join(__dirname, '..', 'docs', 'stage.md'), 'utf8');
  const block = md.split('<!-- stage-gadgets:begin -->')[1].split('<!-- stage-gadgets:end -->')[0];
  const rows = block.trim().split('\n').slice(2).map((r) => r.split('|').slice(1, -1).map((c) => c.trim()));
  assert.deepEqual(rows.map((r) => r[0].replace(/`/g, '')), GADGETS.map((g) => g.id));
  for (const [id, show] of rows) assert.equal(show.replace(/`/g, ''), GADGETS.find((g) => g.id === id.replace(/`/g, '')).show, id);
});

test('motion contract: props name motions by id, unknown ids fall back to the current pose', async () => {
  const { MOTION_IDS, MOTION_CUES, validateCue } = await load('gadget_catalog.js');
  const { buildCatalog, CATALOG } = await load('catalog.js');
  const { GADGET_ANIMS } = await load('gadget_catalog.js');
  const { Animator } = await load('anim.js');
  assert.deepEqual(MOTION_IDS, ['typing', 'look_screen', 'delegate_point', 'receive_report', 'ponder', 'head_tilt', 'sip', 'alert', 'relief', 'slump']);
  for (const c of MOTION_CUES) assert.ok(MOTION_IDS.includes(c.motion), c.motion);
  assert.equal(validateCue({ when: 'gadget.laptop', motion: 'dance' }), null);
  assert.equal(validateCue({ when: 'neural.trust', motion: 'typing' }), null);
  assert.equal(validateCue({ when: 'gadget.motion', motion: 'typing' }), null, 'no cue on itself');
  // each demo prop state names the expected motion
  const want = { delegate: 'delegate_point', report: 'slump', 'tests-run': 'look_screen', approved: 'relief', denied: 'slump', critical: 'alert' };
  const { initialState, reduce, derive } = await load('model.js');
  const { STEPS } = await load('director.js');
  const { GADGET_STEPS } = await load('demo_gadgets.js');
  for (const st of [...STEPS, ...GADGET_STEPS]) {
    if (want[st.id]) assert.equal(derive(st.events.reduce(reduce, initialState())).pose['gadget.motion'], want[st.id], st.id);
  }
  const done = await poseOf(T, start('a', 'subagent', 'x'), end('a', true));
  assert.equal(done['gadget.motion'], 'receive_report');
  // today no catalog entry uses gadget.motion: nothing plays, the pose stays as derived
  const pose = { emotion: 'calm', eyes: 'open', 'gadget.motion': 'typing' };
  const base = buildCatalog([...CATALOG, ...GADGET_ANIMS]);
  const a = new Animator(base.map, 1);
  a.tick(0, pose);
  assert.ok(!a.activeIds().some((id) => id.startsWith('motion')));
  // a motion entry defined later connects by id alone
  const typing = { id: 'motion.typing', scope: 'state', trigger: { state: 'gadget.motion', is: 'typing' }, loop: true, duration: 1, channels: { 'head.y': { fn: 'const', value: 1 } } };
  const withMotion = buildCatalog([...CATALOG, ...GADGET_ANIMS, typing]);
  assert.deepEqual(withMotion.rejected, []);
  const b = new Animator(withMotion.map, 1);
  b.tick(0, pose);
  assert.equal(b.tick(0.5, pose).numeric['head.y'], 1);
  b.tick(1, { ...pose, 'gadget.motion': 'none' });
  assert.equal(b.tick(1.5, { ...pose, 'gadget.motion': 'none' }).numeric['head.y'] || 0, 0, 'stops (after its fade) when the props leave');
});

test('live adapter events (category / op / status) bring the same props as the demo', async () => {
  const { fromTurnEvent } = await load('adapter.js');
  const { initialState, reduce, derive } = await load('model.js');
  let seq = 0;
  const ev = (type, data) => fromTurnEvent({ schema_version: 1, event_id: `e${seq}`, turn_id: 't1', seq: (seq += 1), ts: 0, type, data });
  const run = (...evs) => derive(evs.flat().reduce(reduce, initialState())).pose;
  const T = ev('turn.started', {});
  const code = run(T, ev('tool_call.created', { tool_use_id: 'c1', name: 'editor', status: 'running' }));
  assert.equal(code['gadget.laptop'], 1);
  assert.equal(code['gadget.typing'], 1);
  const read = run(T, ev('tool_call.created', { tool_use_id: 'r1', name: 'file_read', status: 'running' }));
  assert.equal(read['gadget.folder'], 1, 'file_read is a read, not coding');
  assert.equal(read['gadget.laptop'], 0);
  const agent = run(T, ev('tool_call.created', { tool_use_id: 'a1', name: 'sessions_spawn', status: 'running' }), ev('tool_call.updated', { tool_use_id: 'a1', name: 'sessions_spawn', status: 'error' }));
  assert.equal(agent['gadget.drones'], 'err');
  const memw = run(T, ev('tool_call.created', { tool_use_id: 'm1', name: 'memory_write', status: 'running' }));
  assert.equal(memw['gadget.note'], 1);
  const test = run(T, ev('tool_call.created', { tool_use_id: 'x1', name: 'run_tests', status: 'running' }), ev('tool_call.updated', { tool_use_id: 'x1', name: 'run_tests', status: 'done' }));
  assert.equal(test['gadget.lamp'], 'pass');
  const failed = run(T, ev('tool_call.created', { tool_use_id: 'w1', name: 'web_fetch', status: 'running' }), ev('tool_call.updated', { tool_use_id: 'w1', name: 'web_fetch', status: 'timeout' }));
  assert.equal(failed['gadget.glitch'], 'magnifier');
  const denied = run(T, ev('tool_call.created', { tool_use_id: 'd1', name: 'web_fetch', status: 'denied' }));
  assert.equal(denied['gadget.glitch'], 'off', 'a call refused at the gate is not a tool failure');
});

test('drones launch from and land beside her on their own side (docks), clear of her face, chest and hands', async () => {
  const { droneDock } = await load('gadget_scene.js');
  const { GADGETS, validateGadget } = await load('gadget_catalog.js');
  const fs = require('node:fs');
  const STAGE = path.join(__dirname, '..', 'dashboard', 'stage');
  const meta = JSON.parse(fs.readFileSync(path.join(STAGE, 'assets', 'gadgets', 'gadgets.json'), 'utf8'));
  const raw = GADGETS.find((g) => g.id === 'drones');
  const g = validateGadget(raw, Object.keys(meta.sprites));
  assert.ok(g && g.docks && g.docks.length === 2, 'the catalog keeps the docks');
  assert.equal(validateGadget({ ...raw, docks: [[1, 2]] }, Object.keys(meta.sprites)), null, 'two docks or none');
  // each slot takes the dock on its side
  for (const slot of g.slots) {
    const d = droneDock(slot, g.dock, g.docks, g.centerX);
    assert.deepEqual(d, slot[0] + 12 < g.centerX ? g.docks[0] : g.docks[1]);
  }
  assert.ok(g.slots.some((sl) => droneDock(sl, g.dock, g.docks, g.centerX) === g.docks[0]) && g.slots.some((sl) => droneDock(sl, g.dock, g.docks, g.centerX) === g.docks[1]));
  assert.deepEqual(droneDock([228, 94], [362, 148], null), [362, 148], 'no docks: the single dock');
  // the docks (a drone 25 x 19 and its bubble above) lie off her body at rest
  const { decodePng } = require('./stage_png.cjs');
  const { CharRig, NEUTRAL_FRAME } = await import(pathToFileURL(path.join(STAGE, 'js', 'char_rig.js')).href);
  const pm = JSON.parse(fs.readFileSync(path.join(STAGE, 'assets', 'char', 'parts.json'), 'utf8'));
  const pa = decodePng(fs.readFileSync(path.join(STAGE, 'assets', 'char', 'parts.png')));
  const px = new CharRig(pm, pa).compose({ ...NEUTRAL_FRAME }).pixels;
  const CHAR = [181, 20]; // scene.js CHAR_POS
  const [, , dw, dh] = meta.sprites['drone.0'];
  for (const [x0, y0] of g.docks) {
    let on = 0;
    for (let y = y0 - 9; y < y0 + dh; y += 1) for (let x = x0; x < x0 + dw + 12; x += 1) {
      const sx = x - CHAR[0];
      const sy = y - CHAR[1];
      if (sx >= 0 && sy >= 0 && sx < pm.size[0] && sy < pm.size[1] && px[(sy * pm.size[0] + sx) * 4 + 3]) on += 1;
    }
    assert.equal(on, 0, `dock ${x0},${y0}: ${on} pixels over her`);
  }
  // the old dock was on her (the reason for the change)
  let old = 0;
  for (let y = g.dock[1]; y < g.dock[1] + dh; y += 1) for (let x = g.dock[0]; x < g.dock[0] + dw; x += 1) if (px[((y - CHAR[1]) * pm.size[0] + x - CHAR[0]) * 4 + 3]) old += 1;
  assert.ok(old > 50, `the single dock covers her (${old})`);
});
