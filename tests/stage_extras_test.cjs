// Stage extras (#341, night round A2): ear variants, the tail, the earbud as their own asset
// (assets/char/extras.*), for the motion layer to wire (docs/stage.md "Asset contract:
// extras"). The contract (names, anchors) and the art rules (palette, no black inside the
// fur, no specks, the hands' rules do not apply) are checked here.
// node --test tests/stage_extras_test.cjs
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { decodePng } = require('./stage_png.cjs');

const CHAR = path.join(__dirname, '..', 'dashboard', 'stage', 'assets', 'char');
const readJson = (n) => JSON.parse(fs.readFileSync(path.join(CHAR, n), 'utf8'));
const png = (n) => decodePng(fs.readFileSync(path.join(CHAR, n)));
const X = readJson('extras.json');
const XA = png('extras.png');
const P = readJson('parts.json');
const PA = png('parts.png');
const hexrgb = (h) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16));
const key3 = (r, g, b) => (r << 16) | (g << 8) | b;
const keyHex = (h) => key3(...hexrgb(h));
const luma = (r, g, b) => (299 * r + 587 * g + 114 * b) / 1000;
const HAIRS = new Set(['#25243f', '#3e3f66', '#5f628d', '#8b8fb5', '#b9bdd8', '#e6e8f3'].map(keyHex));
const BLACK = new Set(['#0b0a16', '#1d2036'].map(keyHex));

// the contract: these names exist (docs/stage.md lists them; B wires them by these names)
const EAR_VARIANTS = ['std', 'droop', 'twitch1', 'twitch2'];
const TAIL = { front: ['l2', 'l1', 'c', 'r1', 'r2', 'up', 'down'], back: ['l1', 'c', 'r1'] };

function pixels(e, atlas = XA) {
  const out = [];
  for (let y = 0; y < e.h; y += 1) {
    for (let x = 0; x < e.w; x += 1) {
      const q = ((e.ay + y) * atlas.width + e.ax + x) * 4;
      if (atlas.data[q + 3]) out.push([x, y, atlas.data[q], atlas.data[q + 1], atlas.data[q + 2], atlas.data[q + 3]]);
    }
  }
  return out;
}
function grid(e) {
  const g = new Map();
  for (const p of pixels(e)) g.set(`${p[0]},${p[1]}`, p);
  return g;
}
const entries = () => {
  const out = [];
  for (const side of ['l', 'r']) for (const [k, e] of Object.entries(X.ears[side])) out.push([`ears.${side}.${k}`, e]);
  for (const view of ['front', 'back']) for (const [k, e] of Object.entries(X.tail[view])) out.push([`tail.${view}.${k}`, e]);
  for (const [k, e] of Object.entries(X.earbud)) out.push([`earbud.${k}`, e]);
  return out;
};

test('extras: the contract names, anchors and atlas bounds', () => {
  assert.equal(X.version, 1);
  assert.deepEqual(X.anchor, { ears: { l: 'ear_l', r: 'ear_r' }, tail: null, earbud: 'eye_r' });
  for (const side of ['l', 'r']) {
    for (const v of EAR_VARIANTS) assert.ok(X.ears[side][v], `ears.${side}.${v}`);
    for (const a of X.earAngles) assert.ok(X.ears[side][`red.${a}`], `ears.${side}.red.${a}`);
    assert.deepEqual(X.earAngles, P.ears[side].angles, 'the blush follows the ear channel\'s angles');
  }
  for (const [view, names] of Object.entries(TAIL)) for (const n of names) assert.ok(X.tail[view][n], `tail.${view}.${n}`);
  assert.ok(X.earbud.front);
  for (const anchor of ['ear_l', 'ear_r', 'eye_r']) for (const s of Object.values(P.sheets)) assert.ok(s.anchors[anchor], anchor);
  for (const [name, e] of entries()) {
    assert.ok(e.w > 0 && e.h > 0, name);
    assert.ok(e.ax >= 0 && e.ay >= 0 && e.ax + e.w <= XA.width && e.ay + e.h <= XA.height, `${name} in the atlas`);
    assert.ok(pixels(e).length > 20, `${name} is drawn`);
  }
});

test('extras: placed at the rest sheet\'s anchors, every sprite lies inside the sheet', () => {
  const a = P.sheets.base.anchors;
  const place = (name, e) => {
    if (name.startsWith('ears.')) return [a[`ear_${name[5]}`][0] + e.ox, a[`ear_${name[5]}`][1] + e.oy];
    if (name.startsWith('earbud.')) return [a.eye_r[0] + e.ox, a.eye_r[1] + e.oy];
    return [e.ox, e.oy];
  };
  for (const [name, e] of entries()) {
    const [x, y] = place(name, e);
    assert.ok(x >= -1 && y >= -1 && x + e.w <= 279 && y + e.h <= 279, `${name} at ${x},${y} ${e.w}x${e.h}`);
  }
  // the earbud sits by the right eye's outer corner, in her ear (03 / 06)
  const [bx, by] = place('earbud.front', X.earbud.front);
  assert.ok(bx > 160 && bx < 178 && by > 95 && by < 115, `earbud at ${bx},${by}`);
  // the front tail is beside her on the picture's right, the back tail too (below the arm)
  for (const e of Object.values(X.tail.front)) assert.ok(e.ox > 200, 'front tail on the right');
  for (const e of Object.values(X.tail.back)) assert.ok(e.ox > 120 && e.oy > 180, 'back tail low, over the coat');
  // never cut by the sheet's right edge (only its root, below the sheet, is)
  for (const [name, e] of entries()) if (name.startsWith('tail.')) assert.ok(e.ox + e.w <= 276, `${name} ends at ${e.ox + e.w}`);
});

test('extras: every pixel is a palette colour, opaque or clear', () => {
  const allowed = new Set(Object.values(P.palette).map(keyHex));
  const bad = {};
  for (let i = 0; i < XA.data.length; i += 4) {
    const a = XA.data[i + 3];
    if (a !== 0 && a !== 255) bad[`alpha ${a}`] = (bad[`alpha ${a}`] || 0) + 1;
    if (a !== 255) continue;
    const k = key3(XA.data[i], XA.data[i + 1], XA.data[i + 2]);
    if (!allowed.has(k)) bad[`#${k.toString(16).padStart(6, '0')}`] = (bad[`#${k.toString(16).padStart(6, '0')}`] || 0) + 1;
  }
  assert.deepEqual(bad, {});
});

test('extras: no black inside the fur, no isolated dark specks (the art lint\'s rules, per sprite)', () => {
  const bad = [];
  for (const [name, e] of entries()) {
    if (name.startsWith('earbud')) continue;
    const g = grid(e);
    let black = 0;
    let specks = 0;
    for (const [x, y, r, gg, b] of g.values()) {
      let n = 0;
      let hair = 0;
      let minL = 1e9;
      for (const [dx, dy] of [[-1, -1], [0, -1], [1, -1], [-1, 0], [1, 0], [-1, 1], [0, 1], [1, 1]]) {
        const p = g.get(`${x + dx},${y + dy}`);
        if (!p) continue;
        n += 1;
        if (HAIRS.has(key3(p[2], p[3], p[4]))) hair += 1;
        minL = Math.min(minL, luma(p[2], p[3], p[4]));
      }
      if (n < 8) continue;
      if (BLACK.has(key3(r, gg, b)) && hair >= 5) black += 1;
      if (minL - luma(r, gg, b) >= 60) specks += 1;
    }
    if (black || specks) bad.push(`${name}: black ${black} specks ${specks}`);
  }
  assert.deepEqual(bad, []);
});

test('extras: std is the rest ear; droop and twitch are other shapes; red is pink; the tail is grey-white fur', () => {
  for (const side of ['l', 'r']) {
    const rest = P.ears[side].frames['0'];
    const std = X.ears[side].std;
    assert.deepEqual([std.ox, std.oy, std.w, std.h], [rest.ox, rest.oy, rest.w, rest.h]);
    assert.deepEqual(pixels(std), pixels(rest, PA), `ears.${side}.std = the parts atlas' ear at 0°`);
    for (const v of ['droop', 'twitch1', 'twitch2']) {
      const e = X.ears[side][v];
      const same = e.w === std.w && e.h === std.h && e.ox === std.ox && e.oy === std.oy;
      assert.ok(!same || JSON.stringify(pixels(e)) !== JSON.stringify(pixels(std)), `${side}.${v} differs`);
    }
    // flattened: wider than tall, reaching further out to the side than the rest ear
    const d = X.ears[side].droop;
    assert.ok(d.w > d.h, `${side}.droop lies flat (${d.w}x${d.h})`);
    if (side === 'l') assert.ok(d.ox < std.ox, 'the left ear flattens to the left');
    else assert.ok(d.ox + d.w > std.ox + std.w, 'the right ear flattens to the right');
    // blush (05, art unify 4): warm from the inside — some blush in the inner ear, the outer fur
    // still grey (not the whole ear pink)
    const pink = new Set(['#9a6270', '#d1a09c', '#efcfc5', '#fcece4', '#e88f97', '#5c2f3d'].map(keyHex));
    const px = pixels(X.ears[side]['red.0']);
    const share = px.filter((p) => pink.has(key3(p[2], p[3], p[4]))).length / px.length;
    const blushPx = px.filter((p) => key3(p[2], p[3], p[4]) === keyHex('#e88f97')).length;
    const grey = px.filter((p) => HAIRS.has(key3(p[2], p[3], p[4]))).length / px.length;
    assert.ok(share > 0.1 && share < 0.5 && blushPx >= 8 && grey > 0.45, `ears.${side}.red.0: ${share.toFixed(2)} pink, ${blushPx} blush, ${grey.toFixed(2)} grey`);
  }
  for (const [view, names] of Object.entries(TAIL)) {
    for (const n of names) {
      const px = pixels(X.tail[view][n]);
      const hair = px.filter((p) => HAIRS.has(key3(p[2], p[3], p[4])));
      const light = hair.filter((p) => luma(p[2], p[3], p[4]) > 140);
      assert.ok(hair.length / px.length > 0.97, `tail.${view}.${n} in the hair ramp`);
      assert.ok(light.length / hair.length > 0.5, `tail.${view}.${n} grey-white`);
    }
    // the sway frames are different pictures
    const pics = names.map((n) => JSON.stringify([X.tail[view][n].ox, X.tail[view][n].oy, pixels(X.tail[view][n])]));
    assert.equal(new Set(pics).size, names.length, `tail.${view}: every pose differs`);
  }
  // the earbud is black (the mug ramp)
  const mug = new Set(['#0a0a12', '#191a26', '#2c2d40', '#4f526e'].map(keyHex));
  const bud = pixels(X.earbud.front);
  assert.ok(bud.every((p) => mug.has(key3(p[2], p[3], p[4]))), 'earbud in the mug (black) ramp');
});

test('extras: the parts atlas does not carry them (a separate file; parts.json unchanged in shape)', () => {
  assert.ok(!('tail' in P), 'no tail in parts.json');
  for (const side of ['l', 'r']) assert.deepEqual(Object.keys(P.ears[side]).sort(), ['angles', 'frames']);
});
