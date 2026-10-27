// Free hands are paused (the commander, 2026-10-18): the only hand she shows is the one that
// holds the mug. Every frame every motion reaches (intro, loop and exit, 1/30 s; the paused desk
// shot aside) uses the mug arm only, no free left arm, and no hand in front of her face unless
// it brings the mug to her mouth. node --test tests/stage_hands_test.cjs
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const { decodePng } = require('./stage_png.cjs');

const STAGE = path.join(__dirname, '..', 'dashboard', 'stage');
const load = (name) => import(pathToFileURL(path.join(STAGE, 'js', name)).href);
const CHAR = path.join(STAGE, 'assets', 'char');
const json = (n) => JSON.parse(fs.readFileSync(path.join(CHAR, n), 'utf8'));
const png = (n) => decodePng(fs.readFileSync(path.join(CHAR, n)));
const CALM = { trust: 0.5, curiosity: 0.5, vigilance: 0.1, attachment: 0.5, stability: 0.7, satisfaction: 0.5 };
// the mug comes up to her mouth in these (drinking): that hand may be in front of the face
const DRINKING = new Set(['mug_up', 'mug_lift', 'mug_sip']);

async function frames() {
  const { buildMotions, MOTIONS, PAUSED_MOTIONS } = await load('motion_catalog.js');
  const { MotionPlayer } = await load('motion.js');
  const { NEUTRAL_FRAME, partIds } = await load('char_rig.js');
  const { map, rejected } = buildMotions(MOTIONS, partIds(json('parts.json')));
  assert.deepEqual(rejected, []);
  const p = new MotionPlayer(new Map());
  const out = [];
  for (const m of map.values()) {
    if (PAUSED_MOTIONS.includes(m.id)) continue;
    for (const [tracks, dur] of [[m.tracks, m.duration], ...(m.exit ? [[m.exit.tracks, m.exit.duration]] : [])]) {
      for (let t = 0; t <= dur + 1e-9; t += 1 / 30) {
        const f = { ...NEUTRAL_FRAME };
        p.apply(f, tracks, t, false, CALM);
        out.push({ id: m.id, t, f });
      }
    }
  }
  return out;
}

test('every motion shows the mug hand only: no free arm frame on either side (the paused ones stay in the assets)', async () => {
  const { PAUSED_ARMS, PAUSED_MOTIONS, MOTIONS } = await load('motion_catalog.js');
  const meta = json('parts.json');
  const mugArms = new Set(['mug', ...Object.entries(meta.arms.r).filter(([, a]) => a.mug).map(([k]) => k)]);
  const list = await frames();
  assert.ok(list.length > 1000, `${list.length} frames`);
  for (const { id, t, f } of list) {
    assert.ok(mugArms.has(f.armR), `${id} at ${t.toFixed(2)} s: right arm ${f.armR} (no mug)`);
    assert.equal(f.armL, 'none', `${id} at ${t.toFixed(2)} s: free left arm ${f.armL}`);
  }
  // the paused arm frames are still drawn assets (kept for later), but no motion names them
  for (const id of PAUSED_ARMS.r) assert.ok(meta.arms.r[id] || json('poses.json').arms.r[id], id);
  for (const id of PAUSED_ARMS.l) assert.ok(meta.arms.l[id] || json('poses.json').arms.l[id], id);
  const named = new Set(MOTIONS.filter((m) => !PAUSED_MOTIONS.includes(m.id)).flatMap((m) => [m.tracks, m.exit ? m.exit.tracks : {}]).flatMap((t) => [...(t.armR || []), ...(t.armL || [])]).map(([, v]) => v));
  for (const id of [...PAUSED_ARMS.r, ...PAUSED_ARMS.l]) assert.ok(!named.has(id), `${id} is paused`);
  // the desk shot is paused: no rule picks it
  const { MOTION_RULES } = await load('motion_catalog.js');
  for (const r of MOTION_RULES) assert.ok(!PAUSED_MOTIONS.includes(r.motion), r.motion);
});

test('drawn, the frames have no free arm pixels and no hand in front of her face except the mug brought to her mouth', async () => {
  const { CharRig } = await load('char_rig.js');
  const meta = json('parts.json');
  const rig = new CharRig(meta, png('parts.png'), { meta: json('extras.json'), atlas: png('extras.png') }, null, { meta: json('poses.json'), atlas: png('poses.png') });
  const li = (id) => rig.order.indexOf(id) + 1;
  const ARM_L = li('arm_l');
  const HANDS = new Set([li('hand_r'), li('forearm_r')]);
  const W = meta.size[0];
  const seen = new Set();
  let checked = 0;
  for (const { id, t, f } of await frames()) {
    const sheet = meta.sheets[f.sheet] || meta.sheets.base;
    if (sheet.view !== 'front') continue;
    const key = JSON.stringify(f);
    if (seen.has(key)) continue;
    seen.add(key);
    rig.compose(f);
    checked += 1;
    // the face: from the eyes' row to just under the mouth, between the eyes' outer ends
    const A = sheet.anchors;
    const top = Math.round(Math.min(A.eye_l[1], A.eye_r[1]) - 6);
    const bottom = Math.round(A.mouth[1] + 8);
    const left = Math.round(Math.min(A.eye_l[0], A.eye_r[0]) - 14);
    const right = Math.round(Math.max(A.eye_l[0], A.eye_r[0]) + 14);
    let armL = 0;
    let face = 0;
    for (let i = 0; i < rig.layer.length; i += 1) {
      const l = rig.layer[i];
      if (l === ARM_L) armL += 1;
      if (HANDS.has(l)) {
        const x = i % W;
        const y = (i - x) / W;
        if (x >= left && x <= right && y >= top && y <= bottom) face += 1;
      }
    }
    assert.equal(armL, 0, `${id} at ${t.toFixed(2)} s: free left arm drawn`);
    if (!DRINKING.has(f.armR)) assert.equal(face, 0, `${id} at ${t.toFixed(2)} s: a hand in front of her face (${f.armR})`);
  }
  assert.ok(checked > 100, `${checked} front frames drawn`);
});
