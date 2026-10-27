// Stage body (#341, art fix 2026-10-18): the wrist, the sleeve's end and the lab coat / turtleneck
// when the mug arm moves, and the coat's left side at rest — what the owner circled on the demo
// at 1300 px (c / e / o / i: the mug arm's wrist and sleeve; 1–4, 7, 8: dark blotches on the
// coat's left; c: the coat's hem on the right), as pixel rules. And nothing above the body
// changes.
// node --test tests/stage_body_test.cjs
// STAGE_ROOT=/path/to/checkout node --test tests/stage_body_test.cjs   (the rules on another build)
// STAGE_BODY_REPORT=1 prints the counts. Re-record the outside fixture from another checkout
// (e.g. main before a body change): STAGE_ROOT=<main> STAGE_BODY_FIXTURE=write node --test …
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const crypto = require('node:crypto');
const { decodePng } = require('./stage_png.cjs');

const ROOT = process.env.STAGE_ROOT || path.join(__dirname, '..');
const STAGE = path.join(ROOT, 'dashboard', 'stage');
const CHAR = path.join(STAGE, 'assets', 'char');
const FIXTURE = path.join(__dirname, 'fixtures', 'stage_body_outside.json');
const REPORT = process.env.STAGE_BODY_REPORT === '1';
const load = (name) => import(pathToFileURL(path.join(STAGE, 'js', name)).href);
const readJson = (name) => JSON.parse(fs.readFileSync(path.join(CHAR, name), 'utf8'));
const png = (name) => decodePng(fs.readFileSync(path.join(CHAR, name)));
const CALM = { emotion: 'calm', eyes: 'open', mouth: 'neutral', headY: 0, ears: 0, blush: 0, sweat: 0 };
const W = 278;
const N8 = [[-1, -1], [0, -1], [1, -1], [-1, 0], [1, 0], [-1, 1], [0, 1], [1, 1]];
const hexrgb = (h) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16));
const key3 = (r, g, b) => (r << 16) | (g << 8) | b;
const keyHex = (h) => key3(...hexrgb(h));
const luma = (r, g, b) => (299 * r + 587 * g + 114 * b) / 1000;
// build_char.RAMPS / LINE / OUTLINE
const NAVY = new Set(['#0f1124', '#1a1f3c', '#283158', '#3a4778'].map(keyHex));
const SKIN = new Set(['#5e3646', '#9a6270', '#d1a09c', '#efcfc5', '#fcece4', '#5c2f3d'].map(keyHex));
const COAT_LIGHT = new Set(['#988c98', '#c9bfc0', '#ece5de'].map(keyHex));
// the lines a drawn sleeve can carry inside it (char_arms INNER / CONTOUR: coat1, coat0, the
// coat's line, the outline)
const COAT_LINES = new Set(['#665a70', '#3b3247', '#2a2533', '#0b0a16'].map(keyHex));
const ARM = new Set(['forearm_r', 'hand_r']);
// specks on the coat / turtleneck (hair at rest over them) in all the frames: measured 0 (main 1)
const SPECK_BODY_MAX = 0;
const HEAD_ROWS = 136; // rows above this are the head (nothing of the body is drawn there)

let cache = null;
async function rig() {
  if (cache) return cache;
  const R = await load('char_rig.js');
  const meta = readJson('parts.json');
  const atlas = png('parts.png');
  const poses = fs.existsSync(path.join(CHAR, 'poses.json')) ? { meta: readJson('poses.json'), atlas: png('poses.png') } : null;
  cache = { R, meta, atlas, rig: new R.CharRig(meta, atlas, null, null, poses) };
  return cache;
}

// one composed frame (no hair layer: the rules are about the parts): pixels, layer ids, mask
async function frame(f) {
  const { R, rig: r } = await rig();
  const res = r.compose({ ...R.NEUTRAL_FRAME, ...f });
  return {
    px: Uint8ClampedArray.from(res.pixels),
    layer: Array.from(r.layer, (l) => (l ? r.order[l - 1] : null)),
    mask: Uint8ClampedArray.from(r.mask),
  };
}

const frontSheets = (meta) => Object.keys(meta.sheets).filter((s) => (meta.sheets[s].view || 'front') === 'front');
// the arm sprites drawn instead of the mug arm in the front view (the mug moved, typing, poses)
function rightArms(meta) {
  return Object.keys(meta.arms.r).filter((k) => !meta.arms.r[k].view);
}
const mugArms = (meta) => Object.keys(meta.arms.r).filter((k) => meta.arms.r[k].mug);

// Every front frame the motions reach (intro, loop and exit at 1/30 s) and the face sprites on
// the rest pose, plus every right-arm sprite in every front pose: [[key, frame]].
let reached = null;
async function allFrames() {
  if (reached) return reached;
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
  for (const sheet of frontSheets(meta)) for (const armR of ['mug', ...rightArms(meta)]) add({ ...R.NEUTRAL_FRAME, sheet, armR });
  reached = [...out.entries()].filter(([, f]) => (meta.sheets[f.sheet] || meta.sheets.base).view === 'front');
  return reached;
}

// connected groups (8-neighbour) of the pixels where pred(i) holds: [[i, ...], ...]
function groups(pred) {
  const seen = new Uint8Array(W * W);
  const out = [];
  for (let s = 0; s < W * W; s += 1) {
    if (seen[s] || !pred(s)) continue;
    const g = [s];
    seen[s] = 1;
    for (let k = 0; k < g.length; k += 1) {
      const x = g[k] % W;
      const y = (g[k] / W) | 0;
      for (const [dx, dy] of N8) {
        const xx = x + dx;
        const yy = y + dy;
        if (xx < 0 || yy < 0 || xx >= W || yy >= W) continue;
        const j = yy * W + xx;
        if (!seen[j] && pred(j)) {
          seen[j] = 1;
          g.push(j);
        }
      }
    }
    out.push(g);
  }
  return out;
}

// the layers on the outer border of a group: share of `want` among them
function borderShare(g, layer, want) {
  const inG = new Set(g);
  let all = 0;
  let hit = 0;
  for (const i of g) {
    const x = i % W;
    const y = (i / W) | 0;
    for (const [dx, dy] of N8) {
      const xx = x + dx;
      const yy = y + dy;
      if (xx < 0 || yy < 0 || xx >= W || yy >= W) continue;
      const j = yy * W + xx;
      if (inG.has(j)) continue;
      all += 1;
      if (layer[j] === want) hit += 1;
    }
  }
  return all ? hit / all : 0;
}

// --- the rules --------------------------------------------------------------------------------

// The coat's hem and sleeve on the right of the picture: with an arm sprite instead of the mug
// arm (typing, a sip, the mug lowered), every pixel of her body except the mug arm itself is
// still drawn. On main the free coat lost the drawn body below the circle on the right
// (x 152-251, y 235-277): the room showed through under her left sleeve.
test('with the mug arm moved no piece of her body goes (the coat keeps its hem)', async () => {
  const { meta } = await rig();
  const lost = {};
  let total = 0;
  for (const sheet of frontSheets(meta)) {
    const rest = await frame({ sheet });
    for (const armR of rightArms(meta)) {
      const moved = await frame({ sheet, armR });
      let n = 0;
      for (let i = 0; i < W * W; i += 1) {
        if (rest.px[i * 4 + 3] && !ARM.has(rest.layer[i]) && !moved.px[i * 4 + 3]) n += 1;
      }
      if (n) lost[`${sheet}|${armR}`] = n;
      total += n;
    }
  }
  if (REPORT) console.log('body pixels lost with an arm sprite:', total, 'in', Object.keys(lost).length, 'frames');
  assert.equal(total, 0, `pixels of her body that vanish with an arm sprite: ${total} in ${Object.keys(lost).length} frames, e.g. ${JSON.stringify(Object.entries(lost).slice(0, 3))}`);
});

// Her right shoulder and upper arm (the left of the picture) are the coat's sleeve in the icon.
// With the mug arm moved the free coat was cleared from the outline at the shoulder to the waist
// (char_layers.OPEN_FRONT from (50, 168)): the turtleneck's navy stood at the edge of the figure.
const EDGE = 8; // px from the figure's left edge, every row of the body
test('with the mug arm moved her right shoulder and upper arm stay in the coat (no turtleneck at the outline)', async () => {
  const { meta } = await rig();
  const bad = {};
  let total = 0;
  for (const sheet of frontSheets(meta)) {
    for (const armR of rightArms(meta)) {
      const { px, layer } = await frame({ sheet, armR });
      let n = 0;
      for (let y = 160; y < W; y += 1) {
        let seen = 0;
        for (let x = 0; x < W / 2 && seen < EDGE; x += 1) {
          const i = y * W + x;
          if (!px[i * 4 + 3]) continue;
          seen += 1;
          if (layer[i] === 'turtleneck') n += 1;
        }
      }
      if (n) bad[`${sheet}|${armR}`] = n;
      total += n;
    }
  }
  if (REPORT) console.log('turtleneck at the left outline with an arm sprite:', total, 'in', Object.keys(bad).length, 'frames');
  assert.equal(total, 0, `turtleneck pixels within 8 px of the left outline: ${total} in ${Object.keys(bad).length} frames, e.g. ${JSON.stringify(Object.entries(bad).slice(0, 3))}`);
});

// Small navy blotches on the coat: the turtleneck seen through holes of the coat (the icon's thin
// dark strands and fold shadows over the sleeve, classified as the navy material by the
// downscale; main: a 46 px blot at 48-54 / 220-231 and smaller ones on her right sleeve at rest).
const BLOT = 60;
test('no navy blotches on the coat: no small patch of the turtleneck inside the coat (every front frame)', async () => {
  const bad = {};
  let total = 0;
  for (const [key, f] of await allFrames()) {
    const { layer } = await frame(f);
    const blots = groups((i) => layer[i] === 'turtleneck').filter((g) => g.length <= BLOT && borderShare(g, layer, 'coat') >= 0.75);
    if (blots.length) bad[key] = blots.map((g) => `${g.length}px@${g[0] % W},${(g[0] / W) | 0}`);
    total += blots.length;
  }
  if (REPORT) console.log('navy blotches on the coat:', total, 'in', Object.keys(bad).length, 'frames', JSON.stringify(Object.values(bad)[0]));
  assert.equal(total, 0, `small turtleneck patches surrounded by the coat: ${total} in ${Object.keys(bad).length} frames, e.g. ${JSON.stringify(Object.values(bad).slice(0, 2))}`);
});

// With the mug arm moved, the turtleneck under where the mug and the hand were at rest: no piece
// of the coat floats on it (main: the icon's light hair strands over the sweater, classified as
// coat and completed under the hand, showed as light blobs and a dark line below the lifted mug).
const FLOAT = 100;
test('with the mug arm moved no piece of the coat floats on the turtleneck below the mug', async () => {
  const { meta } = await rig();
  const bad = {};
  let total = 0;
  for (const sheet of frontSheets(meta)) {
    const rest = await frame({ sheet });
    for (const armR of rightArms(meta)) {
      const { layer } = await frame({ sheet, armR });
      const bits = groups((i) => layer[i] === 'coat').filter(
        (g) => g.length <= FLOAT && borderShare(g, layer, 'turtleneck') >= 0.6 && g.some((i) => ARM.has(rest.layer[i])),
      );
      if (bits.length) bad[`${sheet}|${armR}`] = bits.map((g) => `${g.length}px@${g[0] % W},${(g[0] / W) | 0}`);
      total += bits.length;
    }
  }
  if (REPORT) console.log('coat pieces on the turtleneck under the moved mug arm:', total, 'in', Object.keys(bad).length, 'frames', JSON.stringify(Object.entries(bad).slice(0, 2)));
  assert.equal(total, 0, `small pieces of the coat on the turtleneck where the mug arm was: ${total}, e.g. ${JSON.stringify(Object.entries(bad).slice(0, 2))}`);
});

// The mug arm's sprites (and the typing hands, which share the sleeve): one piece (main: a dark
// hook of the icon's own cuff stood beside the drawn knit cuff, off the wrist); the hand meets
// the knit cuff and the cuff meets the sleeve (no gap); the coat's sleeve ends *behind* the knit
// cuff (main: art unify 5's disc on the opening reached past the cuff toward the hand, a light
// ball round the wrist with the cuff on top).
const SHORT_OF_CUFF = 9; // px: the sleeve ends at least this far behind the cuff's hand side
const SLEEVE_MIN = 100; // px of sleeve in the frame for the sleeve rules (mug_down's is below it)
function spriteGrid(atlas, part) {
  const a = new Uint8Array(part.w * part.h);
  const k = new Int32Array(part.w * part.h);
  for (let y = 0; y < part.h; y += 1) {
    for (let x = 0; x < part.w; x += 1) {
      const q = ((part.ay + y) * atlas.width + part.ax + x) * 4;
      a[y * part.w + x] = atlas.data[q + 3] === 255 ? 1 : 0;
      k[y * part.w + x] = key3(atlas.data[q], atlas.data[q + 1], atlas.data[q + 2]);
    }
  }
  return { a, k, w: part.w, h: part.h };
}
const COAT = new Set(['#3b3247', '#665a70', '#988c98', '#c9bfc0', '#ece5de', '#2a2533'].map(keyHex));
function armStats(atlas, part) {
  const { a, k, w, h } = spriteGrid(atlas, part);
  const at = (x, y) => (x < 0 || y < 0 || x >= w || y >= h ? -1 : y * w + x);
  const seen = new Uint8Array(w * h);
  let pieces = 0;
  for (let s0 = 0; s0 < w * h; s0 += 1) {
    if (!a[s0] || seen[s0]) continue;
    pieces += 1;
    const st = [s0];
    seen[s0] = 1;
    while (st.length) {
      const i = st.pop();
      for (const [dx, dy] of N8) {
        const j = at((i % w) + dx, ((i / w) | 0) + dy);
        if (j >= 0 && a[j] && !seen[j]) {
          seen[j] = 1;
          st.push(j);
        }
      }
    }
  }
  let skinNavy = 0;
  let navyCoat = 0;
  const cen = { navy: [0, 0, 0], coat: [0, 0, 0], skin: [0, 0, 0] };
  for (let y = 0; y < h; y += 1) {
    for (let x = 0; x < w; x += 1) {
      const i = y * w + x;
      if (!a[i]) continue;
      const m = NAVY.has(k[i]) ? 'navy' : COAT.has(k[i]) ? 'coat' : SKIN.has(k[i]) ? 'skin' : null;
      if (m) cen[m] = [cen[m][0] + x, cen[m][1] + y, cen[m][2] + 1];
      for (const [dx, dy] of [[1, 0], [0, 1]]) {
        const j = at(x + dx, y + dy);
        if (j < 0 || !a[j]) continue;
        const p = [k[i], k[j]];
        if ((SKIN.has(p[0]) && NAVY.has(p[1])) || (SKIN.has(p[1]) && NAVY.has(p[0]))) skinNavy += 1;
        if ((NAVY.has(p[0]) && COAT_LIGHT.has(p[1])) || (NAVY.has(p[1]) && COAT_LIGHT.has(p[0]))) navyCoat += 1;
      }
    }
  }
  // the sleeve: the biggest connected piece of coat colours (the icon's hand carries a few)
  const sleeveOf = new Uint8Array(w * h);
  {
    const lab = new Int32Array(w * h);
    let best = 0;
    let bestN = 0;
    let n = 0;
    for (let s0 = 0; s0 < w * h; s0 += 1) {
      if (!a[s0] || !COAT.has(k[s0]) || lab[s0]) continue;
      n += 1;
      let cnt = 0;
      const st = [s0];
      lab[s0] = n;
      while (st.length) {
        const i = st.pop();
        cnt += 1;
        for (const [dx, dy] of N8) {
          const j = at((i % w) + dx, ((i / w) | 0) + dy);
          if (j >= 0 && a[j] && COAT.has(k[j]) && !lab[j]) {
            lab[j] = n;
            st.push(j);
          }
        }
      }
      if (cnt > bestN) {
        bestN = cnt;
        best = n;
      }
    }
    for (let i = 0; i < w * h; i += 1) sleeveOf[i] = lab[i] === best && best ? 1 : 0;
  }
  cen.coat = [0, 0, 0];
  for (let i = 0; i < w * h; i += 1) if (sleeveOf[i]) cen.coat = [cen.coat[0] + (i % w), cen.coat[1] + ((i / w) | 0), cen.coat[2] + 1];
  // toward the hand: from the sleeve's middle to the hand's middle; how far the sleeve reaches
  // past the knit cuff's hand side along it
  const c = (m) => [cen[m][0] / cen[m][2], cen[m][1] / cen[m][2]];
  const [sx, sy] = c('coat');
  const [hx, hy] = c('skin');
  const len = Math.hypot(hx - sx, hy - sy) || 1;
  const u = [(hx - sx) / len, (hy - sy) / len];
  let coatMax = -1e9;
  let navyMax = -1e9;
  for (let y = 0; y < h; y += 1) {
    for (let x = 0; x < w; x += 1) {
      const i = y * w + x;
      if (!a[i]) continue;
      const pr = x * u[0] + y * u[1];
      if (sleeveOf[i]) coatMax = Math.max(coatMax, pr);
      if (NAVY.has(k[i])) navyMax = Math.max(navyMax, pr);
    }
  }
  return { pieces, skinNavy, navyCoat, sleeve: cen.coat[2], past: Math.round((coatMax - navyMax) * 10) / 10 };
}
test('the mug arm: one piece, the hand into the knit cuff into the sleeve, the sleeve ends behind the cuff', async () => {
  const { meta, atlas } = await rig();
  const report = {};
  const bad = [];
  for (const name of [...mugArms(meta), 'type1', 'type2']) {
    const part = meta.arms.r[name];
    if (!part) continue;
    const st = armStats(atlas, part);
    report[name] = st;
    if (st.pieces !== 1) bad.push(`${name}: ${st.pieces} pieces`);
    if (st.skinNavy < 3) bad.push(`${name}: the hand meets the knit cuff along ${st.skinNavy} px`);
    if (st.sleeve < SLEEVE_MIN) continue;
    if (st.navyCoat < 3) bad.push(`${name}: the knit cuff meets the sleeve along ${st.navyCoat} px`);
    if (st.past > -SHORT_OF_CUFF) bad.push(`${name}: the sleeve ends ${-st.past} px behind the knit cuff's hand side`);
  }
  if (REPORT) console.log('mug arm sprites', JSON.stringify(report));
  assert.deepEqual(bad, []);
});

// Isolated dark specks (the art lint's rule: a pixel 60 darker than all eight neighbours) on the
// coat, the turtleneck and the arm, with the hair layer at rest over them (as the page draws
// it), in every front frame: counted before / after; none on a moved mug arm.
const SPECK_DELTA = 60;
let hairSet = null;
async function withHair(f) {
  const { R, meta, rig: r } = await rig();
  if (!hairSet) hairSet = [readJson('hair.json'), png('hair.png')];
  const [H, HA] = hairSet;
  const res = r.compose({ ...R.NEUTRAL_FRAME, ...f });
  const px = Uint8ClampedArray.from(res.pixels);
  const layer = Array.from(r.layer, (l) => (l ? r.order[l - 1] : null));
  const pose = meta.sheets[f.sheet] || meta.sheets.base;
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
          layer[y * W + x] = 'hair';
          break;
        }
      }
    }
  }
  return { px, layer };
}
test('the coat, the turtleneck and the sleeves: isolated dark specks counted, none on a moved mug arm', async () => {
  let body = 0;
  let arm = 0;
  const where = [];
  for (const [key, f] of await allFrames()) {
    const { px, layer } = await withHair(f);
    for (let y = 1; y < W - 1; y += 1) {
      for (let x = 1; x < W - 1; x += 1) {
        const i = y * W + x;
        const l = layer[i];
        if (!px[i * 4 + 3] || !(l === 'coat' || l === 'turtleneck' || ARM.has(l))) continue;
        let minL = 1e9;
        let opaque = 0;
        for (const [dx, dy] of N8) {
          const j = (y + dy) * W + x + dx;
          if (!px[j * 4 + 3]) continue;
          opaque += 1;
          minL = Math.min(minL, luma(px[j * 4], px[j * 4 + 1], px[j * 4 + 2]));
        }
        if (opaque < 8 || minL - luma(px[i * 4], px[i * 4 + 1], px[i * 4 + 2]) < SPECK_DELTA) continue;
        if (ARM.has(l) && f.armR !== 'mug') {
          arm += 1;
          where.push(`${f.sheet}|${f.armR}@${x},${y}`);
        } else body += 1;
      }
    }
  }
  if (REPORT) console.log('specks (hair at rest over it): coat / turtleneck / mug arm at rest', body, 'moved arm', arm, where.slice(0, 5));
  assert.equal(arm, 0, `dark specks on a moved mug arm: ${where.slice(0, 5)}`);
  assert.ok(body <= SPECK_BODY_MAX, `specks on the coat / turtleneck: ${body}`);
});

// Nothing above the body changes: every reached front frame, the rows above HEAD_ROWS (the head,
// the ears, the hair over them), character and hair mask, hashed against main.
test('above the body every front frame is the same picture as before (character and hair mask)', async () => {
  const got = {};
  for (const [key, f] of await allFrames()) {
    const { px, mask } = await frame(f);
    got[key] = crypto.createHash('sha1').update(px.subarray(0, HEAD_ROWS * W * 4)).update(mask.subarray(0, HEAD_ROWS * W * 4)).digest('hex').slice(0, 16);
  }
  if (process.env.STAGE_BODY_FIXTURE === 'write') {
    fs.writeFileSync(FIXTURE, `${JSON.stringify(got).replace(/,"/g, ',\n"')}\n`);
  }
  const before = JSON.parse(fs.readFileSync(FIXTURE, 'utf8'));
  assert.ok(Object.keys(before).length > 300);
  const changed = Object.keys(before).filter((k) => got[k] !== undefined && got[k] !== before[k]);
  if (REPORT) console.log('frames compared above the body:', Object.keys(before).filter((k) => got[k] !== undefined).length, 'changed', changed.length);
  assert.equal(changed.length, 0, `frames that changed above the body: ${changed.slice(0, 3)}`);
});
