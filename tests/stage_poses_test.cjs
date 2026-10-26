// Stage pose arms (#341, art unify 5): whole-pose arm sprites for the motion layer
// (assets/char/poses.*; docs/stage.md "Asset contract: pose arms"): arms crossed (05 ドヤ) and
// the thinking pose with the arm across (01 考え事). Checked: the names, both arms per pose,
// inside the sheet, palette colours only, the hand rules of the art lint (light skin, no black
// on a hand's edge, a navy cuff), the arms really cross / the chin hand reaches the chin.
// node --test tests/stage_poses_test.cjs
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { decodePng } = require('./stage_png.cjs');

const CHAR = path.join(__dirname, '..', 'dashboard', 'stage', 'assets', 'char');
const X = JSON.parse(fs.readFileSync(path.join(CHAR, 'poses.json'), 'utf8'));
const XA = decodePng(fs.readFileSync(path.join(CHAR, 'poses.png')));
const P = JSON.parse(fs.readFileSync(path.join(CHAR, 'parts.json'), 'utf8'));
const hex = (h) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16)).join();
const SKIN_LIGHT = new Set(['#efcfc5', '#fcece4'].map(hex));
const SKIN = new Set(['#5e3646', '#9a6270', '#d1a09c', '#efcfc5', '#fcece4'].map(hex));
const NAVY = new Set(['#0f1124', '#1a1f3c', '#283158', '#3a4778'].map(hex));
const BLACK = new Set(['#0b0a16'].map(hex));
const POSES = ['cross', 'ponder_cross'];

function pixels(e) {
  const out = [];
  for (let y = 0; y < e.h; y += 1) {
    for (let x = 0; x < e.w; x += 1) {
      const q = ((e.ay + y) * XA.width + e.ax + x) * 4;
      if (XA.data[q + 3]) out.push([e.ox + x, e.oy + y, [XA.data[q], XA.data[q + 1], XA.data[q + 2]].join()]);
    }
  }
  return out;
}

test('poses: both arms of every pose, inside the sheet and the atlas, palette colours only', () => {
  assert.equal(X.version, 1);
  const allowed = new Set(Object.values(P.palette).map(hex));
  for (const name of POSES) {
    for (const side of ['r', 'l']) {
      const e = X.arms[side][name];
      assert.ok(e, `arms.${side}.${name}`);
      assert.ok(!(name in P.arms[side]), `${name} is not in the parts atlas (its own file)`);
      assert.ok(e.ox >= 0 && e.oy >= 0 && e.ox + e.w <= 278 && e.oy + e.h <= 278, `${side}.${name} in the sheet`);
      assert.ok(e.ax + e.w <= XA.width && e.ay + e.h <= XA.height);
      const px = pixels(e);
      assert.ok(px.length > 400, `${side}.${name} drawn`);
      assert.deepEqual(px.filter((p) => !allowed.has(p[2])).length, 0, `${side}.${name} palette`);
    }
  }
});

test('poses: the hands as the art lint wants them (light skin, no black on the edge, a navy cuff)', () => {
  for (const name of POSES) {
    for (const side of ['r', 'l']) {
      const px = pixels(X.arms[side][name]);
      const at = new Map(px.map((p) => [`${p[0]},${p[1]}`, p[2]]));
      const skin = px.filter((p) => SKIN.has(p[2]));
      const light = skin.filter((p) => SKIN_LIGHT.has(p[2])).length / skin.length;
      assert.ok(light >= 0.6, `${side}.${name}: light skin ${light.toFixed(2)}`);
      let blackOnSkin = 0;
      for (const [x, y, c] of px) {
        if (!BLACK.has(c)) continue;
        for (const [dx, dy] of [[1, 0], [-1, 0], [0, 1], [0, -1]]) if (SKIN.has(at.get(`${x + dx},${y + dy}`))) blackOnSkin += 1;
      }
      assert.equal(blackOnSkin, 0, `${side}.${name}: black next to skin`);
      assert.ok(px.filter((p) => NAVY.has(p[2])).length >= 20, `${side}.${name}: a navy cuff`);
    }
  }
});

test('poses: the arms cross (05) and the thinking hand reaches the chin over the arm across (01)', () => {
  const box = (e) => {
    const px = pixels(e);
    const xs = px.map((p) => p[0]);
    const ys = px.map((p) => p[1]);
    return { x0: Math.min(...xs), x1: Math.max(...xs), y0: Math.min(...ys), y1: Math.max(...ys), px };
  };
  // cross: each forearm reaches past the middle of the body to the other side
  const r = box(X.arms.r.cross);
  const l = box(X.arms.l.cross);
  assert.ok(r.x1 > 160 && l.x0 < 110, `cross: right arm to x ${r.x1}, left arm to x ${l.x0}`);
  const rSet = new Set(r.px.map((p) => `${p[0]},${p[1]}`));
  const overlap = l.px.filter((p) => rSet.has(`${p[0]},${p[1]}`)).length;
  assert.ok(overlap > 100, `cross: the forearms overlap at ${overlap} px`);
  // ponder_cross: the chin hand's skin reaches the chin (the ponder sheet's chin ~139, 131) and
  // the elbow comes down to the arm across
  const c = box(X.arms.l.ponder_cross);
  const skin = c.px.filter((p) => SKIN.has(p[2]));
  assert.ok(Math.min(...skin.map((p) => p[0])) <= 150 && Math.min(...skin.map((p) => p[1])) <= 142, 'the finger at the chin');
  const across = box(X.arms.r.ponder_cross);
  assert.ok(c.y1 >= across.y0, `the elbow (to y ${c.y1}) rests on the arm across (from y ${across.y0})`);
});
