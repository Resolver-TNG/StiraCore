// Stage background (#341): the room's layers are declarative data with their own parallax,
// sky variants swap only the far band, the character never moves the room, and the room's
// ambient motion is the listed, slow set. node --test tests/stage_bg_test.cjs
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { pathToFileURL } = require('node:url');

const ROOT = path.join(__dirname, '..', 'dashboard', 'stage');
const load = (name) => import(pathToFileURL(path.join(ROOT, 'js', name)).href);
// The page's asset allow-list (src/stiracore/observability/stage.py, _ASSETS)
const ALLOWED = /^assets\/(?!source\/)[a-z0-9_-]+\/[a-z0-9_-]+\.(?:png|json)$/;

test('every sky has the full, valid layer stack with the event-driven roles', async () => {
  const { layersFor, SKIES, BG_LAYERS } = await load('bg_layers.js');
  for (const sky of Object.keys(SKIES)) {
    const { list, rejected } = layersFor(sky);
    assert.deepEqual(rejected, [], sky);
    const roles = list.filter((e) => e.role).map((e) => e.role).sort();
    const want = ['city', 'desk', 'neon', 'shelf', 'window', ...(sky === 'rain' ? ['rain'] : [])].sort();
    assert.deepEqual(roles, want, sky);
    for (const e of list) {
      assert.ok(Object.isFrozen(e));
      if (e.file) {
        assert.match(`assets/bg/${e.file}.png`, ALLOWED);
        assert.ok(fs.existsSync(path.join(ROOT, 'assets', 'bg', `${e.file}.png`)), `${sky}: ${e.file}.png`);
      }
    }
    // drawn back to front
    assert.deepEqual(list.map((e) => e.order), [...list.map((e) => e.order)].sort((a, b) => a - b));
  }
  // the variants differ only in the far band (and the glass in front of it)
  const ids = (s) => layersFor(s).list.filter((e) => e.band !== 'far').map((e) => `${e.id}:${e.file}`);
  assert.deepEqual(ids('night').filter((x) => !x.startsWith('glass')), ids('dawn').filter((x) => !x.startsWith('glass')));
  assert.deepEqual(ids('night').filter((x) => !x.startsWith('glass')), ids('rain').filter((x) => !x.startsWith('glass')));
  assert.notEqual(layersFor('night').list.find((e) => e.id === 'city').file, layersFor('dawn').list.find((e) => e.id === 'city').file);
  assert.equal(BG_LAYERS.length, 14);
});

test('bands: far < mid < near < the character (1.0) < fore, each layer its own parallax', async () => {
  const { layersFor, BAND_DEPTH } = await load('bg_layers.js');
  const { layerOffset } = await load('room.js');
  const { list } = layersFor('rain');
  const bands = ['far', 'mid', 'near', 'fore'];
  for (let i = 1; i < bands.length; i += 1) {
    const prev = list.filter((e) => e.band === bands[i - 1]).map((e) => e.depth);
    const cur = list.filter((e) => e.band === bands[i]).map((e) => e.depth);
    assert.ok(Math.max(...prev) < Math.min(...cur), `${bands[i - 1]} behind ${bands[i]}`);
  }
  assert.ok(BAND_DEPTH.near[1] < 1 && BAND_DEPTH.fore[0] > 1, 'the character sits between near and fore');
  // a full pointer swing: deeper layers move less, the character moves by its own depth
  const frame = { cam: [0, 0], parallax: [1, 1] };
  const dx = (d) => Math.abs(-1 * 6 * d);
  const sky = list.find((e) => e.id === 'sky');
  const fore = list.find((e) => e.id === 'fore');
  assert.ok(dx(sky.depth) < dx(1.0) && dx(1.0) < dx(fore.depth));
  assert.deepEqual(layerOffset(frame, fore.depth), [-7, -3]);
  assert.ok(layerOffset(frame, sky.depth).every((v) => v === 0), 'the sky barely moves');
  assert.ok(new Set(list.map((e) => e.depth)).size >= 8, 'at least 8 distinct parallax depths');
  // CRITICAL shake: the foreground (depth > 1) is part of the room and stays put
  const { roomFrame, SHAKE_MIN_DEPTH } = await load('room.js');
  const { initialState, reduce, derive } = await load('model.js');
  const crit = derive(reduce(initialState(), { type: 'breaker', level: 'CRITICAL' })).pose;
  assert.ok(fore.depth >= SHAKE_MIN_DEPTH);
  const seen = new Set();
  for (let f = 0; f < 60; f += 1) {
    const r = roomFrame(crit, { shake: 1 }, f / 60, { x: 0, y: 0 });
    seen.add(layerOffset(r, 1.0)[0]);
    assert.deepEqual(layerOffset({ ...r, shake: null }, fore.depth), [0, 0]);
  }
  assert.ok(seen.size > 1, 'she does shake');
});

test('layer entries are data: anything else is refused', async () => {
  const { validateLayer, layersFor, BG_LAYERS } = await load('bg_layers.js');
  const ok = { id: 'x', band: 'mid', kind: 'color', file: 'wall', depth: 0.3, order: 3 };
  assert.ok(validateLayer(ok));
  const bad = [
    { ...ok, band: 'sky' },
    { ...ok, kind: 'script' },
    { ...ok, depth: 0.9 }, // outside the mid band
    { ...ok, depth: Number.NaN },
    { ...ok, order: 10.5 }, // in front of the character's band
    { ...ok, file: '../tools/build_bg' },
    { ...ok, file: 'source/resolver-icon' },
    { ...ok, file: 'https://example.com/x' },
    { ...ok, file: () => 'wall' },
    { ...ok, role: 'character' },
    { ...ok, skies: ['storm'] },
    { ...ok, skies: ['__proto__'] },
    { ...ok, id: 'Wall!' },
    { id: 'r', band: 'far', kind: 'rain', file: 'wall', depth: 0.1, order: 0 }, // rain draws itself
    { ...ok, band: 'fore', depth: 1.0, order: 10.2 }, // fore must be in front of her depth
    null,
    [ok],
  ];
  for (const b of bad) assert.equal(validateLayer(b), null, JSON.stringify(b));
  // duplicates and a second owner of a role are rejected, not drawn
  const dup = layersFor('night', [...BG_LAYERS, { ...ok, id: 'wall' }, { ...ok, id: 'wall2', role: 'desk' }]);
  assert.deepEqual(dup.rejected, ['wall', 'wall2']);
});

test('sky selection: default night, explicit, or from the clock; junk falls back', async () => {
  const { pickSky, layersFor, DEFAULT_SKY } = await load('bg_layers.js');
  assert.equal(DEFAULT_SKY, 'night');
  assert.equal(pickSky(null, 12), 'night');
  assert.equal(pickSky('rain', 6), 'rain');
  assert.equal(pickSky('dawn', 23), 'dawn');
  assert.equal(pickSky('auto', 5), 'dawn');
  assert.equal(pickSky('auto', 6), 'dawn');
  assert.equal(pickSky('auto', 7), 'night');
  assert.equal(pickSky('auto', 23), 'night');
  assert.equal(pickSky('auto', undefined), 'night');
  for (const j of ['toString', '__proto__', 'constructor', 'storm', '', 'NIGHT']) assert.equal(pickSky(j, 6), 'night', j);
  assert.equal(layersFor('toString').sky, 'night');
});

test('the character does not reach the background: room fields incl. rain ignore her channels', async () => {
  const { roomFrame, ROOM_CHANNELS } = await load('room.js');
  const { NUMERIC_CHANNELS } = await load('catalog.js');
  const { initialState, derive } = await load('model.js');
  const pose = derive(initialState()).pose;
  const charChannels = Object.keys(NUMERIC_CHANNELS).filter((c) => !ROOM_CHANNELS.includes(c));
  const base = { 'rain.fall': 0.37, 'city.twinkle': 0.5, 'neon.ambient': 0.02 };
  const ref = roomFrame(pose, base, 2, { x: 0, y: 0 });
  assert.equal(ref.rain, 0.37);
  for (let i = 0; i < 40; i += 1) {
    const ch = { ...base };
    for (const c of charChannels) ch[c] = Math.cos(i + c.length) * 3;
    assert.deepEqual(roomFrame({ ...pose, eyes: 'happy', headY: -1, ears: 2 }, ch, 2, { x: 0, y: 0 }), ref);
  }
});

test('scene.js builds the background from the layer data and places it with layerOffset only', () => {
  const src = fs.readFileSync(path.join(ROOT, 'js', 'scene.js'), 'utf8');
  assert.match(src, /this\.bg = layersFor\(this\.opts\.sky\)/);
  assert.match(src, /for \(const e of this\.bg\.list\)/);
  // no background layer is hard-coded any more
  assert.doesNotMatch(src, /L\('(city|wall|pipes|shelf|desk|glass|neon)'/);
  const build = src.slice(src.indexOf('  build() {'), src.indexOf('    const m = this.charMeta;'));
  assert.doesNotMatch(build, /head\.|body\.breath|arm\.y|ear\.|hair\./);
  // each mesh covers only its image's drawn rectangle (bg.json rects), sampled at room pixels
  assert.match(src, /this\.bgMeta\.rects && this\.bgMeta\.rects\[e\.file\]/);
  assert.equal((src.match(/vec2 ip = uRect\.xy \+/g) || []).length, 4, 'layer, rain, glow and neon shaders');
  // the rain phase comes from the room frame (ambient), never from a character channel
  assert.match(src, /uPhase\.value = room\.rain;/);
});

test('ambient catalog: exactly the listed slow entries, rain included, and documented', async () => {
  const { AMBIENT_ANIMS, AMBIENT_CHANNELS } = await load('room.js');
  const { buildCatalog, CATALOG } = await load('catalog.js');
  const { GADGET_ANIMS } = await load('gadget_catalog.js');
  const { map, rejected } = buildCatalog([...CATALOG, ...GADGET_ANIMS, ...AMBIENT_ANIMS]);
  assert.deepEqual(rejected, []);
  const ambient = [...map.values()].filter((e) => e.scope === 'ambient');
  assert.deepEqual(ambient.map((e) => e.id), ['ambient.neon', 'ambient.city', 'ambient.rain']);
  assert.deepEqual(Object.keys(AMBIENT_CHANNELS), ['neon.ambient', 'city.twinkle', 'rain.fall']);
  const rain = ambient.find((e) => e.id === 'ambient.rain');
  assert.ok(rain.duration >= 4 && rain.loop);
  assert.deepEqual(Object.keys(rain.channels), ['rain.fall']);
  // the rain loop is seamless: the shader moves the pattern 640 rows per phase, a multiple of
  // its 64-row period and of the 8-row slant step
  const src = fs.readFileSync(path.join(ROOT, 'js', 'scene.js'), 'utf8');
  assert.match(src, /uPhase \* 640\.0/);
  assert.match(src, /mod\(s \+ floor\(r \* 640\.0\), 64\.0\)/);
  assert.match(src, /floor\(s \/ 8\.0\)/);
  const md = fs.readFileSync(path.join(__dirname, '..', 'docs', 'stage.md'), 'utf8');
  const block = md.split('<!-- stage-ambient:begin -->')[1].split('<!-- stage-ambient:end -->')[0];
  assert.match(block, /`ambient\.rain`/);
});

test('ambient never shows on the reduced-motion path and is off with ?ambient=0', () => {
  const src = fs.readFileSync(path.join(ROOT, 'js', 'main.js'), 'utf8');
  assert.match(src, /'neon\.ambient', 'city\.twinkle', 'rain\.fall'\]\) delete numeric\[k\]/);
  assert.match(src, /params\.get\('ambient'\) === '0' \? \[\] : AMBIENT_ANIMS/);
});

test('docs/stage.md lists every background layer and sky', async () => {
  const { BG_LAYERS, SKIES } = await load('bg_layers.js');
  const md = fs.readFileSync(path.join(__dirname, '..', 'docs', 'stage.md'), 'utf8');
  const block = md.split('<!-- stage-bg:begin -->')[1].split('<!-- stage-bg:end -->')[0];
  for (const e of BG_LAYERS) assert.match(block, new RegExp(`\`${e.id}\``), e.id);
  for (const s of Object.keys(SKIES)) assert.match(block, new RegExp(`\`${s}\``), s);
});
