// Stage face effects (#341, art unify 3): blush, sweat, the fluster's blue lines, the anger
// mark, sparkles, a tear — their own asset (assets/char/fx.*) for the motion layer to wire
// (docs/stage.md "Asset contract: face fx"). Checked: the names, anchors and slots, every
// pixel a palette colour, every sprite inside the sheet at the rest anchors, the face-slot
// effects off the eyes, the fluster's lines only on her skin, no symbol drawn from a font.
// node --test tests/stage_fx_test.cjs
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { decodePng } = require('./stage_png.cjs');

const CHAR = path.join(__dirname, '..', 'dashboard', 'stage', 'assets', 'char');
const readJson = (n) => JSON.parse(fs.readFileSync(path.join(CHAR, n), 'utf8'));
const png = (n) => decodePng(fs.readFileSync(path.join(CHAR, n)));
const FX = readJson('fx.json');
const FA = png('fx.png');
const P = readJson('parts.json');
const PA = png('parts.png');
const key = (r, g, b) => `${r},${g},${b}`;
const hex = (h) => key(...[1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16)));

// the contract (names B wires): name -> [anchor, slot]
const CONTRACT = {
  'blush.soft.l': ['eye_l', 'blush'], 'blush.soft.r': ['eye_r', 'blush'],
  'blush.strong.l': ['eye_l', 'blush'], 'blush.strong.r': ['eye_r', 'blush'],
  'blush.nose': ['mouth', 'blush'],
  'sweat.drop': ['eye_r', 'fx'], 'sweat.fly': ['eye_l', 'fx'],
  gloom: ['eye_l', 'blush'],
  anger: ['eye_r', 'fx'],
  'tear.l': ['eye_l', 'blush'], 'tear.r': ['eye_r', 'blush'],
  'sparkle.1': ['eye_l', 'fx'], 'sparkle.2': ['eye_l', 'fx'], 'sparkle.3': ['eye_l', 'fx'],
};

function pixels(e) {
  const out = [];
  for (let y = 0; y < e.h; y += 1) {
    for (let x = 0; x < e.w; x += 1) {
      const q = ((e.ay + y) * FA.width + e.ax + x) * 4;
      if (FA.data[q + 3]) out.push([x, y, key(FA.data[q], FA.data[q + 1], FA.data[q + 2]), FA.data[q + 3]]);
    }
  }
  return out;
}
const place = (e, sheet = 'base') => {
  const a = P.sheets[sheet].anchors[e.anchor];
  return [Math.round(a[0] + e.ox), Math.round(a[1] + e.oy)];
};

test('fx: the contract names, anchors, slots; inside the atlas and the sheet', () => {
  assert.equal(FX.version, 1);
  assert.deepEqual(Object.keys(FX.fx).sort(), Object.keys(CONTRACT).sort());
  for (const [name, [anchor, slot]] of Object.entries(CONTRACT)) {
    const e = FX.fx[name];
    assert.equal(e.anchor, anchor, name);
    assert.equal(e.slot, slot, name);
    for (const s of Object.values(P.sheets)) assert.ok(s.anchors[anchor], `${anchor} on every sheet`);
    assert.ok(e.ax + e.w <= FA.width && e.ay + e.h <= FA.height, `${name} in the atlas`);
    assert.ok(pixels(e).length >= 4, `${name} is drawn`);
    const [x, y] = place(e);
    assert.ok(x >= 0 && y >= 0 && x + e.w <= 278 && y + e.h <= 278, `${name} at ${x},${y}`);
  }
  // the old effects in parts.json stay (the rig draws them today)
  assert.deepEqual(Object.keys(P.fx).sort(), ['blush', 'shock', 'sweat']);
});

test('fx: every pixel a palette colour, opaque or clear', () => {
  const allowed = new Set(Object.values(P.palette).map(hex));
  const bad = new Set();
  for (let i = 0; i < FA.data.length; i += 4) {
    const a = FA.data[i + 3];
    if (a !== 0 && a !== 255) bad.add(`alpha ${a}`);
    if (a === 255 && !allowed.has(key(FA.data[i], FA.data[i + 1], FA.data[i + 2]))) bad.add(key(FA.data[i], FA.data[i + 1], FA.data[i + 2]));
  }
  assert.deepEqual([...bad], []);
});

test('fx: the face-slot effects keep off the eyes; the blue lines only on her skin; the blush is pink, the anger red, the sweat blue', () => {
  // the eye sprites at rest (open and wide: the biggest apertures) and the face's skin
  const eyeAt = new Set();
  for (const side of ['l', 'r']) {
    for (const shape of ['open.c', 'wide.c', 'surprised.c', 'panic.c', 'teary.c']) {
      const e = P.eyes[side][shape];
      const a = P.sheets.base.anchors[`eye_${side}`];
      for (let y = 0; y < e.h; y += 1) {
        for (let x = 0; x < e.w; x += 1) {
          if (PA.data[((e.ay + y) * PA.width + e.ax + x) * 4 + 3]) eyeAt.add(`${Math.round(a[0] + e.ox) + x},${Math.round(a[1] + e.oy) + y}`);
        }
      }
    }
  }
  const part = (k) => {
    const e = P.parts[k].base;
    const s = new Set();
    for (let y = 0; y < e.h; y += 1) {
      for (let x = 0; x < e.w; x += 1) if (PA.data[((e.ay + y) * PA.width + e.ax + x) * 4 + 3]) s.add(`${e.ox + x},${e.oy + y}`);
    }
    return s;
  };
  const face = part('face');
  const crown = part('crown');
  const bad = [];
  for (const [name, e] of Object.entries(FX.fx)) {
    if (e.slot !== 'blush') continue;
    const [x0, y0] = place(e);
    for (const [x, y] of pixels(e)) {
      const k = `${x0 + x},${y0 + y}`;
      if (eyeAt.has(k)) bad.push(`${name} over an eye at ${k}`);
      if (name === 'gloom' && (!face.has(k) || crown.has(k))) bad.push(`gloom off the skin at ${k}`);
    }
  }
  assert.deepEqual(bad.slice(0, 8), []);
  const share = (name, cols) => {
    const px = pixels(FX.fx[name]);
    return px.filter((p) => cols.has(p[2])).length / px.length;
  };
  const pink = new Set(['#e88f97', '#c86a78', '#d1a09c'].map(hex));
  for (const n of ['blush.soft.l', 'blush.strong.r', 'blush.nose']) assert.ok(share(n, pink) > 0.95, `${n} pink`);
  assert.ok(share('anger', new Set(['#e0414f', '#5c2f3d'].map(hex))) === 1, 'anger red with its rim');
  const blue = new Set(['#2b3a6e', '#8fc3ee', '#ffffff', '#5a86bb'].map(hex));
  for (const n of ['sweat.drop', 'sweat.fly', 'gloom', 'tear.l']) assert.ok(share(n, blue) === 1, `${n} blue`);
  // the sparkle frames are different pictures (a twinkle)
  const pics = ['sparkle.1', 'sparkle.2', 'sparkle.3'].map((n) => JSON.stringify(pixels(FX.fx[n])));
  assert.equal(new Set(pics).size, 3);
});
