// Stage layers (#341 stage 2): background, character and props are independent layers.
// The character's motion never reaches the room; the room's ambient motion is a short, listed
// set of slow entries. node --test tests/stage_layers_test.cjs
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const ROOT = path.join(__dirname, '..', 'dashboard', 'stage');
const load = (name) => import(path.join(ROOT, 'js', name));

async function setup(extra = []) {
  const { buildCatalog, CATALOG } = await load('catalog.js');
  const { GADGET_ANIMS } = await load('gadget_catalog.js');
  const { AMBIENT_ANIMS } = await load('room.js');
  return buildCatalog([...CATALOG, ...GADGET_ANIMS, ...AMBIENT_ANIMS, ...extra]);
}
async function poseOf(...evs) {
  const { initialState, reduce, derive } = await load('model.js');
  return derive(evs.reduce(reduce, initialState())).pose;
}

test('the room frame does not read any character channel', async () => {
  const { roomFrame, ROOM_CHANNELS } = await load('room.js');
  const { NUMERIC_CHANNELS } = await load('catalog.js');
  const pose = await poseOf({ type: 'turn.start' }, { type: 'tool.start', id: 't', kind: 'tool', name: 'x' });
  const charChannels = Object.keys(NUMERIC_CHANNELS).filter((c) => !ROOM_CHANNELS.includes(c));
  assert.ok(charChannels.includes('body.breath') && charChannels.includes('head.y') && charChannels.includes('arm.y'));
  const base = { 'light.desk.pulse': 0.5, 'steam.puff': 1.2 };
  const ref = JSON.stringify(roomFrame(pose, base, 3.25, { x: 0.2, y: -0.1 }));
  for (let i = 0; i < 50; i += 1) {
    const ch = { ...base };
    for (const c of charChannels) ch[c] = Math.sin(i * 1.7 + c.length) * 3;
    assert.equal(JSON.stringify(roomFrame({ ...pose, eyes: 'closed', mouth: 'grin', headY: 1, ears: -2, blush: 1 }, ch, 3.25, { x: 0.2, y: -0.1 })), ref);
  }
});

test('character motion over time leaves the room still (ambient off) or moving only in ambient fields', async () => {
  const { roomFrame } = await load('room.js');
  const { Animator } = await load('anim.js');
  const { map } = await setup();
  const noAmbient = new Map([...map].filter(([, e]) => e.scope !== 'ambient'));
  const pose = await poseOf();
  for (const [cat, allowed] of [[noAmbient, []], [map, ['neon.core', 'city', 'rain']]]) {
    const a = new Animator(cat, 7);
    a.request('gesture.nod', 0.5);
    a.request('gesture.sip', 3);
    const frames = [];
    const charSeen = new Set();
    for (let f = 0; f < 60 * 40; f += 1) {
      const t = f / 60;
      const { numeric, enums } = a.tick(t, pose);
      charSeen.add(`${Math.round(numeric['body.breath'] || 0)}|${Math.round(numeric['head.y'] || 0)}|${Math.round(numeric['arm.y'] || 0)}|${enums.eyes || '-'}`);
      const r = roomFrame(pose, numeric, t, { x: 0, y: 0 });
      frames.push({ ...r, 'neon.core': r.neon.core, neon: { ...r.neon, core: 0 } });
    }
    assert.ok(charSeen.size >= 4, 'the character did move (breath, nod, sip, blink)');
    const strip = (r) => JSON.stringify(Object.fromEntries(Object.entries(r).filter(([k]) => !allowed.includes(k))));
    for (const r of frames) assert.equal(strip(r), strip(frames[0]));
    if (allowed.length) {
      assert.ok(new Set(frames.map((r) => r['neon.core'])).size > 1, 'neon ambient moves');
      assert.ok(new Set(frames.map((r) => r.city)).size > 1, 'city ambient moves');
      const lv = frames.map((r) => r['neon.core']);
      assert.ok(Math.max(...lv) - Math.min(...lv) <= 0.16 + 1e-9, 'neon ambient is subtle');
      // city: at most one change every ~4 s
      let changes = 0;
      for (let i = 1; i < frames.length; i += 1) if (frames[i].city !== frames[i - 1].city) changes += 1;
      assert.ok(changes <= 40 / 4 + 1, `city changes ${changes}`);
    }
  }
});

test('room elements change with their events only', async () => {
  const { roomFrame } = await load('room.js');
  const T = { type: 'turn.start' };
  const calm = roomFrame(await poseOf(), {}, 1, { x: 0, y: 0 });
  const cases = [
    [[T, { type: 'tool.start', id: 'x', kind: 'tool', name: 'calc' }], 'desk'],
    [[T, { type: 'tool.start', id: 'x', kind: 'memory', name: 'memory_search' }], 'shelf'],
    [[T, { type: 'tool.start', id: 'x', kind: 'web', name: 'web_fetch' }], 'window'],
    [[{ type: 'breaker', level: 'CRITICAL' }], 'neon'],
  ];
  for (const [evs, field] of cases) {
    const r = roomFrame(await poseOf(...evs), {}, 1, { x: 0, y: 0 });
    assert.notDeepEqual(r[field], calm[field], field);
  }
  // the scope trace time only runs while a tool runs
  assert.equal(roomFrame(await poseOf(), {}, 5, { x: 0, y: 0 }).desk.scopeTime, 0);
});

test('idle (character life signs) cannot move the camera or the room; ambient is listed and slow', async () => {
  const { validateEntry, CATALOG } = await load('catalog.js');
  const { AMBIENT_ANIMS, AMBIENT_CHANNELS, AMBIENT_MIN_PERIOD } = await load('room.js');
  const idle = (channels) => validateEntry({ id: 'idle.x', scope: 'idle', trigger: { always: true }, loop: true, duration: 5, channels });
  assert.equal(idle({ 'cam.x': { fn: 'sine', amp: 1, period: 11 } }), null, 'no camera drift with the breath');
  assert.equal(idle({ 'light.desk.pulse': { fn: 'const', value: 1 } }), null);
  assert.equal(idle({ 'neon.ambient': { fn: 'sine', amp: 0.05, period: 8 } }), null);
  assert.ok(!CATALOG.some((e) => Object.keys(e.channels).some((c) => c.startsWith('cam.'))), 'no built-in entry moves the camera');
  // the ambient set is exactly the listed one, slow and small
  const { map } = await setup();
  const ambient = [...map.values()].filter((e) => e.scope === 'ambient').map((e) => e.id);
  assert.deepEqual(ambient, AMBIENT_ANIMS.map((e) => e.id));
  assert.deepEqual(ambient, ['ambient.neon', 'ambient.city', 'ambient.rain']);
  for (const e of AMBIENT_ANIMS) for (const c of Object.keys(e.channels)) assert.ok(c in AMBIENT_CHANNELS, c);
  const amb = (channels, extra = {}) => validateEntry({ id: 'ambient.x', scope: 'ambient', trigger: { always: true }, loop: true, duration: 8, channels, ...extra });
  assert.ok(amb({ 'neon.ambient': { fn: 'sine', amp: 0.05, period: 8 } }));
  assert.equal(amb({ 'neon.ambient': { fn: 'sine', amp: 0.05, period: 0.5 } }), null, 'fast flicker');
  assert.equal(amb({ 'neon.ambient': { fn: 'pulse', amp: 0.05, period: 8 } }), null, 'hard on/off');
  assert.equal(amb({ 'neon.flicker': { fn: 'sine', amp: 1, period: 8 } }), null, 'not an ambient channel');
  assert.equal(amb({ 'head.y': { fn: 'sine', amp: 1, period: 8 } }), null, 'ambient cannot move her');
  assert.equal(amb({ 'neon.ambient': { fn: 'sine', amp: 0.05, period: 8 } }, { duration: AMBIENT_MIN_PERIOD / 2 }), null);
  assert.equal(validateEntry({ id: 'gesture.x', scope: 'gesture', duration: 1, channels: { 'city.twinkle': { fn: 'const', value: 1 } } }), null);
  assert.ok(validateEntry({ id: 'ambient.x', scope: 'ambient', trigger: { always: true }, loop: true, duration: 8, channels: { 'neon.ambient': { fn: 'sine', amp: 9, period: 8 } } }).channels['neon.ambient'].amp <= 0.08);
});

test('each layer keeps its own parallax: room < character < props in front', async () => {
  const { layerOffset } = await load('room.js');
  const { GADGETS } = await load('gadget_catalog.js');
  const frame = { cam: [0, 0], parallax: [1, 1] };
  const room = [0.12, 0.3, 0.34, 0.42, 0.55, 0.8].map((d) => layerOffset(frame, d)[0]);
  const char = layerOffset(frame, 1.0)[0];
  const laptop = layerOffset(frame, GADGETS.find((g) => g.id === 'laptop').depth)[0];
  assert.deepEqual(room, [...room].sort((a, b) => b - a), 'deeper layers move less');
  assert.ok(Math.abs(room[0]) < Math.abs(char) && Math.abs(char) < Math.abs(laptop));
  assert.deepEqual(layerOffset({ cam: [0, 0], parallax: [0, 0] }, 1.0), [0, 0], 'no pointer, no parallax');
});

test('scene.js places the room from roomFrame() only and the character by its own depth', () => {
  const src = fs.readFileSync(path.join(ROOT, 'js', 'scene.js'), 'utf8');
  const update = src.slice(src.indexOf('  update(frame) {'), src.indexOf('    // character\n'));
  // #350: the board's aside input (a UI progress, not a character channel) is the only addition
  assert.match(update, /roomFrame\(pose, ch, this\.time, this\.parallax, this\.aside\)/);
  assert.match(update, /layerOffset\(layer\.band \? still : room, layer\.depth\)/);
  assert.match(update, /const still = \{ \.\.\.room, shake: null, aside: null \};/);
  assert.doesNotMatch(update, /head\.|body\.breath|arm\.y|ear\.|hair\.|cam\.x|cam\.y/, 'room placement reads no character channel');
});

test('docs/stage.md lists the ambient entries', async () => {
  const { AMBIENT_ANIMS } = await load('room.js');
  const md = fs.readFileSync(path.join(__dirname, '..', 'docs', 'stage.md'), 'utf8');
  const block = md.split('<!-- stage-ambient:begin -->')[1].split('<!-- stage-ambient:end -->')[0];
  for (const e of AMBIENT_ANIMS) assert.match(block, new RegExp(`\`${e.id.replace('.', '\\.')}\``));
  assert.equal((block.match(/`ambient\.[a-z]+`/g) || []).length, AMBIENT_ANIMS.length);
});

test('CRITICAL shake moves the character and the props in front of her, never the room', async () => {
  const { roomFrame, layerOffset, SHAKE_MIN_DEPTH } = await load('room.js');
  const { GADGETS } = await load('gadget_catalog.js');
  const { Animator } = await load('anim.js');
  const { map } = await setup();
  const pose = await poseOf({ type: 'breaker', level: 'CRITICAL' });
  assert.equal(pose.shake, 1);
  const roomDepths = [0.12, 0.3, 0.34, 0.42, 0.55, 0.8, ...GADGETS.filter((g) => g.depth < SHAKE_MIN_DEPTH).map((g) => g.depth)];
  const front = GADGETS.filter((g) => g.depth >= SHAKE_MIN_DEPTH).map((g) => g.depth);
  assert.ok(front.length >= 3, 'laptop, lamp, drones are in front');
  const a = new Animator(map, 3);
  const charSeen = new Set();
  for (let f = 0; f < 120; f += 1) {
    const t = f / 60;
    const room = roomFrame(pose, a.tick(t, pose).numeric, t, { x: 0, y: 0 });
    for (const d of roomDepths) assert.deepEqual(layerOffset(room, d), [0, 0], `depth ${d} at frame ${f}`);
    const her = layerOffset(room, 1.0);
    charSeen.add(her[0]);
    for (const d of front) assert.deepEqual(layerOffset(room, d), her, 'props in front shake with her');
  }
  assert.deepEqual([...charSeen].sort(), [-1, 0, 1], 'she shakes by one pixel');
  // with the pointer, every layer keeps its own parallax under the shake
  const room = roomFrame(pose, { shake: 1 }, 1 / 30, { x: 1, y: 0 });
  assert.notDeepEqual(layerOffset(room, 0.12), layerOffset(room, 0.8));
});

test('mug steam is thin: few, small, faint particles from the catalog values', async () => {
  const { STEAM } = await load('room.js');
  const { CATALOG } = await load('catalog.js');
  const puff = CATALOG.find((e) => e.id === 'state.steam').channels['steam.puff'];
  const maxPerSecond = STEAM.rate * (puff.offset + puff.amp);
  const alive = maxPerSecond * (STEAM.life[0] + STEAM.life[1]);
  assert.ok(alive <= 48, `particle budget: ${alive.toFixed(1)} alive at most`);
  assert.ok(STEAM.wisps.length <= 2, 'one or two wisps');
  assert.ok(STEAM.size[0] + STEAM.size[1] <= 8, 'small points (was 6 -> 25)');
  assert.ok(STEAM.alpha[0] <= 1 && STEAM.alpha[1] >= 1, 'faint, thinning upward');
  assert.ok(STEAM.rise <= 30 && STEAM.drift <= 6, 'short, does not climb over her face');
});
