// Stage art lint (#341, art unify): one look for every part of the character. Scans the drawn
// assets and every frame the motions reach (with the hair layer at rest over it) for what broke
// the look before: colours outside the palette, black lines / pixels inside the hair, hands that
// are not drawn like the icon's hand (black ring, dark skin), isolated dark specks.
// node --test tests/stage_art_lint_test.cjs
// STAGE_ROOT=/path/to/checkout STAGE_LINT_REPORT=1 node --test tests/stage_art_lint_test.cjs
//   prints the counts (e.g. for main before a change; the palette test needs parts.json palette)
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { decodePng } = require('./stage_png.cjs');

const ROOT = process.env.STAGE_ROOT || path.join(__dirname, '..');
const STAGE = path.join(ROOT, 'dashboard', 'stage');
const CHAR = path.join(STAGE, 'assets', 'char');
const load = (name) => import(path.join(STAGE, 'js', name));
const readJson = (name) => JSON.parse(fs.readFileSync(path.join(CHAR, name), 'utf8'));
const png = (name) => decodePng(fs.readFileSync(path.join(CHAR, name)));
const REPORT = process.env.STAGE_LINT_REPORT === '1';
const CALM = { emotion: 'calm', eyes: 'open', mouth: 'neutral', headY: 0, ears: 0, blush: 0, sweat: 0 };
const W = 278;
const hexrgb = (h) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16));
const key3 = (r, g, b) => (r << 16) | (g << 8) | b;
const keyHex = (h) => key3(...hexrgb(h));
const luma = (r, g, b) => (299 * r + 587 * g + 114 * b) / 1000;

// build_char.RAMPS / LINE / OUTLINE (the palette is also written to parts.json by the build)
const HAIR = ['#25243f', '#3e3f66', '#5f628d', '#8b8fb5', '#b9bdd8', '#e6e8f3'];
const SKIN = ['#5e3646', '#9a6270', '#d1a09c', '#efcfc5', '#fcece4'];
const SKIN_LINE = '#5c2f3d';
const OUTLINE = '#0b0a16';
const HAIR_LINE_BLACK = '#1d2036'; // the old near-black strand line (no longer drawn)
const BLACKS = new Set([keyHex(OUTLINE), keyHex(HAIR_LINE_BLACK)]);
const HAIRS = new Set(HAIR.map(keyHex));
const SKINS = new Set([...SKIN, SKIN_LINE].map(keyHex));
const SKIN_RAMP = new Set(SKIN.map(keyHex));

// --- the rule thresholds (what is allowed) ---------------------------------------------------
// Black inside the hair: a black pixel (OUTLINE or the old strand line) with at least
// HAIR_INSIDE of its 8 neighbours in hair colours lies inside the hair. Allowed: on the
// silhouette (a transparent neighbour) and on a border to another material (fewer hair
// neighbours: hair over the coat, the skin, the earbud). Budget per frame:
const HAIR_INSIDE = 5;
// None in any frame since art unify 2 (#341): the hair's own outline is a hair grey
// everywhere (it kept the black next to the coat and the face, and in a pose that edge landed
// inside the hair). Round 1 left at most 7 per frame, 1512 in all; main before round 1: 996 per
// frame, about 500 000 in all.
const HAIR_BLACK_MAX = 0;
const HAIR_BLACK_TOTAL = 0;
// Drawn hands: no black pixel next to a skin pixel (the contour is the skin line, as the
// icon's hand), and the light skin steps carry the hand (share of the two lightest skin
// colours among its skin pixels, contour included).
const HAND_LIGHT_MIN = 0.66;
// Isolated dark specks: a pixel this much darker (luma) than every one of its 8 neighbours.
// Measured after art unify 2: at most 3 per frame, 284 in all 595 frames (round 1: 11 /
// 2150; main before it: 14 435), none at rest. What is left are strand pixels of the hair layer
// where its parts, moved by different whole pixels in a pose, meet (the hair layer's atlas is
// shared by every pose). The per-pose fix at build time composites the hair layer at rest for
// each pose and fixes every speck the parts draw. Budgets keep it there.
const SPECK_DELTA = 60;
const SPECK_MAX = 3;
const SPECK_TOTAL = 320;

let cache = null;
async function rig() {
  if (cache) return cache;
  const R = await load('char_rig.js');
  const meta = readJson('parts.json');
  const atlas = png('parts.png');
  const sets = { front: [readJson('hair.json'), png('hair.png')] };
  if (fs.existsSync(path.join(CHAR, 'hair_back.json'))) sets.back = [readJson('hair_back.json'), png('hair_back.png')];
  cache = { R, meta, atlas, sets, rig: new R.CharRig(meta, atlas) };
  return cache;
}

// Every frame the motions reach (intro, loop and exit at 1/30 s), plus the face sprites on the
// rest pose: [[frameKey, frame]].
async function reachedFrames() {
  const { R, meta } = await rig();
  const { buildMotions, MOTIONS } = await load('motion_catalog.js');
  const { MotionPlayer } = await load('motion.js');
  const { map } = buildMotions(MOTIONS, R.partIds(meta));
  const p = new MotionPlayer(new Map());
  const out = new Map();
  const add = (f) => out.set(R.frameKey(f), { ...f });
  for (const m of map.values()) {
    for (const [tracks, dur] of [[m.tracks, m.duration], ...(m.exit ? [[m.exit.tracks, m.exit.duration]] : [])]) {
      for (let t = 0; t <= dur + 1e-9; t += 1 / 30) {
        const f = { ...R.NEUTRAL_FRAME };
        p.apply(f, tracks, t, false, CALM);
        add(f);
      }
    }
  }
  for (const k of Object.keys(meta.eyes.l)) {
    const [eyes, gaze] = k.split('.');
    add({ ...R.NEUTRAL_FRAME, eyes, gaze });
  }
  return [...out.entries()];
}

// The character of a frame with the hair layer's parts at rest over it (as hair_layer.js draws
// them: the first segment with a pixel wins, front parts never over mask G = arm, mug, the
// overHair layers; back parts only where she is not). Returns { px, layer } (layer: id per pixel).
async function picture(f) {
  const { R, meta, sets, rig: r } = await rig();
  const res = r.compose(f);
  const px = Uint8ClampedArray.from(res.pixels);
  const layer = Array.from(r.layer, (l) => (l ? r.order[l - 1] : null));
  const pose = meta.sheets[f.sheet] || meta.sheets.base;
  const set = sets[pose.view || 'front'];
  if (!set) return { px, layer };
  const [H, HA] = set;
  const offs = H.segments.map((s) => {
    const d = R.poseDisplacement(pose, s.pivot[0], s.pivot[1]);
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
          px.set([HA.data[q], HA.data[q + 1], HA.data[q + 2], 255], i);
          layer[y * W + x] = `hair:${s.part}`;
          break;
        }
      }
    }
  }
  return { px, layer };
}

// the layers that draw hair (the ears are hair-coloured too); a black pixel of another layer
// next to the hair (the mug, the hand, the coat's edge) is a border between materials
const HAIR_LAYERS = new Set(['crown', 'hair_back', 'head_underlay', 'ear_l', 'ear_r']);
const isHairLayer = (l) => Boolean(l) && (HAIR_LAYERS.has(l) || l.startsWith('hair:'));
const N8 = [[-1, -1], [0, -1], [1, -1], [-1, 0], [1, 0], [-1, 1], [0, 1], [1, 1]];
const FACE_SPRITES = new Set(['eyes', 'mouth', 'brows', 'blush', 'fx']);

// Black pixels inside the hair and isolated dark specks of one picture.
function scan({ px, layer }) {
  const at = (x, y) => (y * W + x) * 4;
  const hairBlack = [];
  const specks = [];
  for (let y = 1; y < W - 1; y += 1) {
    for (let x = 1; x < W - 1; x += 1) {
      const i = at(x, y);
      if (!px[i + 3]) continue;
      const k = key3(px[i], px[i + 1], px[i + 2]);
      let opaque = 0;
      let hair = 0;
      let minL = 1e9;
      for (const [dx, dy] of N8) {
        const j = at(x + dx, y + dy);
        if (!px[j + 3]) continue;
        opaque += 1;
        if (HAIRS.has(key3(px[j], px[j + 1], px[j + 2]))) hair += 1;
        minL = Math.min(minL, luma(px[j], px[j + 1], px[j + 2]));
      }
      if (opaque < 8) continue; // the silhouette: the outline is allowed there
      const where = layer[y * W + x];
      if (BLACKS.has(k) && hair >= HAIR_INSIDE && isHairLayer(where)) hairBlack.push(`${where}@${x},${y}`);
      if (!FACE_SPRITES.has(where) && minL - luma(px[i], px[i + 1], px[i + 2]) >= SPECK_DELTA) specks.push(`${where}@${x},${y}`);
    }
  }
  return { hairBlack, specks };
}

// one scan per reached frame, shared by the tests below
let scans = null;
async function scanAll() {
  if (scans) return scans;
  scans = [];
  for (const [key, f] of await reachedFrames()) scans.push([key, scan(await picture(f))]);
  return scans;
}

test('palette: every pixel of the character assets is a declared palette colour', async (t) => {
  const { meta } = await rig();
  if (!meta.palette) {
    t.skip('parts.json has no palette (an older build)');
    return;
  }
  const allowed = new Set(Object.values(meta.palette).map(keyHex));
  // the palette is the builder's named colours: the ramps, lines and outline are all in it
  for (const h of [...HAIR, ...SKIN, SKIN_LINE, OUTLINE]) assert.ok(allowed.has(keyHex(h)), `palette has ${h}`);
  const bad = {};
  for (const name of ['parts.png', 'hair.png', 'hair_back.png', 'base.png']) {
    const img = png(name);
    for (let i = 0; i < img.data.length; i += 4) {
      const a = img.data[i + 3];
      if (a !== 0 && a !== 255) bad[`${name} alpha ${a}`] = (bad[`${name} alpha ${a}`] || 0) + 1;
      if (a !== 255) continue;
      const k = key3(img.data[i], img.data[i + 1], img.data[i + 2]);
      if (!allowed.has(k)) {
        const hex = `#${k.toString(16).padStart(6, '0')}`;
        bad[`${name} ${hex}`] = (bad[`${name} ${hex}`] || 0) + 1;
      }
    }
  }
  assert.deepEqual(bad, {}, 'colours outside the palette');
});

test('hair: black inside the hair only within the budget, in every frame (the outline only on the silhouette and at material borders)', async () => {
  const frames = await scanAll();
  const bad = [];
  let worst = 0;
  let total = 0;
  for (const [key, { hairBlack }] of frames) {
    total += hairBlack.length;
    worst = Math.max(worst, hairBlack.length);
    if (hairBlack.length > HAIR_BLACK_MAX) bad.push(`${key}: ${hairBlack.length} (${hairBlack.slice(0, 4).join(' ')})`);
  }
  if (REPORT) console.log(`hair black: ${frames.length} frames, total ${total}, worst ${worst}, frames over budget ${bad.length}`);
  assert.deepEqual(bad.slice(0, 12), [], `${bad.length} frames with black inside the hair`);
  assert.ok(total <= HAIR_BLACK_TOTAL, `black pixels inside the hair in all frames: ${total} > ${HAIR_BLACK_TOTAL}`);
  const { R } = await rig();
  const rest = scan(await picture({ ...R.NEUTRAL_FRAME })).hairBlack;
  assert.deepEqual(rest, [], 'no black inside the hair at rest');
});

test('the hair assets carry no near-black strand lines (hair lines are hair greys)', () => {
  const black = keyHex(HAIR_LINE_BLACK);
  const counts = {};
  for (const name of ['hair.png', 'hair_back.png', 'base.png']) {
    const img = png(name);
    let n = 0;
    for (let i = 0; i < img.data.length; i += 4) if (img.data[i + 3] === 255 && key3(img.data[i], img.data[i + 1], img.data[i + 2]) === black) n += 1;
    counts[name] = n;
  }
  if (REPORT) console.log('near-black strand pixels', counts);
  assert.deepEqual(counts, { 'hair.png': 0, 'hair_back.png': 0, 'base.png': 0 });
});

// The drawn hands (arm sprites): skin pixels and the black pixels that touch them.
function handStats(meta, atlas, part) {
  const aw = atlas.width;
  const isSkin = (x, y) => {
    if (x < 0 || y < 0 || x >= part.w || y >= part.h) return false;
    const q = ((part.ay + y) * aw + part.ax + x) * 4;
    return atlas.data[q + 3] === 255 && SKIN_RAMP.has(key3(atlas.data[q], atlas.data[q + 1], atlas.data[q + 2]));
  };
  let skin = 0;
  let light = 0;
  let blackOnSkin = 0;
  const lightSet = new Set(SKIN.slice(3).map(keyHex));
  for (let y = 0; y < part.h; y += 1) {
    for (let x = 0; x < part.w; x += 1) {
      const q = ((part.ay + y) * aw + part.ax + x) * 4;
      if (atlas.data[q + 3] !== 255) continue;
      const k = key3(atlas.data[q], atlas.data[q + 1], atlas.data[q + 2]);
      if (SKINS.has(k)) {
        skin += 1;
        if (lightSet.has(k)) light += 1;
      } else if (k === keyHex(OUTLINE) && (isSkin(x - 1, y) || isSkin(x + 1, y) || isSkin(x, y - 1) || isSkin(x, y + 1))) blackOnSkin += 1;
    }
  }
  return { skin, light: skin ? light / skin : 0, blackOnSkin };
}

test('hands: every drawn hand is drawn like the icon hand (light skin, a thin skin-line contour, no black ring)', async () => {
  const { meta, atlas } = await rig();
  const bad = [];
  const rows = [];
  for (const side of ['l', 'r']) {
    for (const [name, part] of Object.entries(meta.arms[side])) {
      // the mug hand (mug_*) is the icon's own hand moved rigidly (its own shading); the back
      // view's arm is seen from behind: both are checked for the black ring only
      const drawn = !name.startsWith('mug_') && !part.view;
      const s = handStats(meta, atlas, part);
      rows.push(`${side}.${name}: skin ${s.skin} light ${s.light.toFixed(2)} black-on-skin ${s.blackOnSkin}`);
      if (s.skin < 40) bad.push(`${side}.${name}: only ${s.skin} skin pixels`);
      if (drawn && s.light < HAND_LIGHT_MIN) bad.push(`${side}.${name}: light skin ${s.light.toFixed(2)} < ${HAND_LIGHT_MIN}`);
      if (s.blackOnSkin > 0) bad.push(`${side}.${name}: ${s.blackOnSkin} black pixels on the hand's edge`);
    }
  }
  if (REPORT) console.log(rows.join('\n'));
  assert.deepEqual(bad, []);
});

test('hands: the free hand comes out of a navy knit cuff (06: the turtleneck sleeve under the coat)', async () => {
  const { meta, atlas } = await rig();
  const navy = new Set(['#0f1124', '#1a1f3c', '#283158', '#3a4778'].map(keyHex));
  const bad = [];
  for (const [name, part] of Object.entries(meta.arms.l)) {
    let n = 0;
    const tones = new Set();
    for (let y = 0; y < part.h; y += 1) {
      for (let x = 0; x < part.w; x += 1) {
        const q = ((part.ay + y) * atlas.width + part.ax + x) * 4;
        const k = key3(atlas.data[q], atlas.data[q + 1], atlas.data[q + 2]);
        if (atlas.data[q + 3] === 255 && navy.has(k)) {
          n += 1;
          tones.add(k);
        }
      }
    }
    // a band of some pixels, in at least two navy steps (the ribs)
    if (n < 20 || tones.size < 2) bad.push(`l.${name}: ${n} navy pixels in ${tones.size} tones`);
  }
  assert.deepEqual(bad, []);
});

test('isolated dark specks: none at rest, within the budget in every frame (outside the eyes, mouth and brows)', async () => {
  const frames = await scanAll();
  const bad = [];
  let total = 0;
  for (const [key, { specks }] of frames) {
    total += specks.length;
    if (specks.length > SPECK_MAX) bad.push(`${key}: ${specks.length} (${specks.slice(0, 4).join(' ')})`);
  }
  if (REPORT) console.log(`specks: total ${total}, frames over budget ${bad.length}`);
  assert.deepEqual(bad.slice(0, 12), [], `${bad.length} frames with isolated dark pixels`);
  assert.ok(total <= SPECK_TOTAL, `isolated dark pixels in all frames: ${total} > ${SPECK_TOTAL}`);
  const { R } = await rig();
  const rest = scan(await picture({ ...R.NEUTRAL_FRAME })).specks;
  assert.deepEqual(rest, [], 'none at rest');
});

test('the lint is not blind: a black line drawn into the hair, a black ring on a hand and a speck are found', async () => {
  const { R, meta, atlas } = await rig();
  const pic = await picture({ ...R.NEUTRAL_FRAME });
  const before = scan(pic);
  const px = pic.px;
  const k = hexrgb(OUTLINE);
  // a vertical black line inside the hair: the bangs at x 140, y 56..66
  assert.equal(pic.layer[60 * W + 140], 'hair:bangs');
  for (let y = 56; y < 66; y += 1) px.set([...k, 255], (y * W + 140) * 4);
  // a speck in the coat: the first coat pixel whose neighbours are all light
  let spot = -1;
  for (let j = 200 * W; j < 260 * W && spot < 0; j += 1) {
    if (pic.layer[j] !== 'coat') continue;
    const light = N8.every(([dx, dy]) => {
      const q = (j + dy * W + dx) * 4;
      return px[q + 3] && luma(px[q], px[q + 1], px[q + 2]) > 120;
    });
    if (light) spot = j;
  }
  assert.ok(spot >= 0, 'a light coat pixel to put a speck on');
  px.set([...k, 255], spot * 4);
  const after = scan(pic);
  assert.ok(after.hairBlack.length >= before.hairBlack.length + 8, 'the hair line is found');
  assert.ok(after.specks.length >= before.specks.length + 1, 'the speck is found');
  // a hand with a black ring: paint the contour of the chin hand black in a copy of the atlas
  const part = meta.arms.l.chin;
  const copy = { width: atlas.width, height: atlas.height, data: Uint8ClampedArray.from(atlas.data) };
  for (let y = 1; y < part.h - 1; y += 1) {
    for (let x = 1; x < part.w - 1; x += 1) {
      const q = ((part.ay + y) * atlas.width + part.ax + x) * 4;
      if (copy.data[q + 3] !== 255) continue;
      const left = ((part.ay + y) * atlas.width + part.ax + x - 1) * 4;
      if (copy.data[left + 3] === 0) copy.data.set([...k, 255], q);
    }
  }
  assert.ok(handStats(meta, copy, part).blackOnSkin > 0, 'the black ring is found');
});

test('gestures (06): the chin hand reaches the jaw with a finger, the pen shows, the lifted arm leaves the coat open', async () => {
  const { R, meta, atlas } = await rig();
  const skinOf = (part) => {
    const out = [];
    for (let y = 0; y < part.h; y += 1) {
      for (let x = 0; x < part.w; x += 1) {
        const q = ((part.ay + y) * atlas.width + part.ax + x) * 4;
        if (atlas.data[q + 3] === 255) out.push([part.ox + x, part.oy + y, key3(atlas.data[q], atlas.data[q + 1], atlas.data[q + 2])]);
      }
    }
    return out;
  };
  // 顎に手を当てる: a finger reaches the chin (the ponder sheet's chin is near 139, 131), not a
  // fist below the jaw
  const chin = skinOf(meta.arms.l.chin).filter((p) => SKIN_RAMP.has(p[2]));
  const reach = Math.min(...chin.map((p) => p[0]));
  const top = Math.min(...chin.map((p) => p[1]));
  assert.ok(reach <= 150 && top <= 142, `chin hand reaches x ${reach}, y ${top}`);
  // ペンで書く: a black pen of some length in the hand
  // (the mug ramp and its line / outline: the pen is drawn in the mug's black)
  const mug = new Set(['#0a0a12', '#191a26', '#2c2d40', '#4f526e', '#050509', OUTLINE].map(keyHex));
  const pen = skinOf(meta.arms.l.pen).filter((p) => mug.has(p[2]));
  const span = Math.hypot(Math.max(...pen.map((p) => p[0])) - Math.min(...pen.map((p) => p[0])), Math.max(...pen.map((p) => p[1])) - Math.min(...pen.map((p) => p[1])));
  assert.ok(pen.length >= 25 && span >= 14, `pen ${pen.length} px, ${span.toFixed(1)} px long`);
  // the mug arm lifted: inside the coat's open front the turtleneck shows (no coat box). The
  // open front runs from the armpit (86, 190) to the waist (100, 246) (char_layers
  // OPEN_FRONT_FREE; #341 art fix: from (50, 168) it took her shoulder and upper arm too)
  const navy = new Set(['#0f1124', '#1a1f3c', '#283158', '#3a4778'].map(keyHex));
  const pic = await picture({ ...R.NEUTRAL_FRAME, sheet: 'type', armR: 'type1' });
  let n = 0;
  let nv = 0;
  for (let y = 200; y < 240; y += 1) {
    for (let x = 84; x < 100; x += 1) {
      if (x < 86 + ((y - 190) * 14) / 56 + 2) continue;
      const i = (y * W + x) * 4;
      n += 1;
      if (navy.has(key3(pic.px[i], pic.px[i + 1], pic.px[i + 2]))) nv += 1;
    }
  }
  assert.ok(nv / n > 0.8, `open front: ${nv}/${n} navy`);
});
