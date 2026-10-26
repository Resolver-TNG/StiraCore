// Stage expressions from the state (#341, night round B): round A's ear variants and tail
// (extras.*) wired through tables — droop when worried / tense, red ears after an approval or
// when fond, a smug look when the turn's tests passed, a sigh when one failed, the tail by the
// emotion. node --test tests/stage_expr_test.cjs
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { decodePng } = require('./stage_png.cjs');

const STAGE = path.join(__dirname, '..', 'dashboard', 'stage');
const load = (name) => import(path.join(STAGE, 'js', name));
const CHAR = path.join(STAGE, 'assets', 'char');
const META = JSON.parse(fs.readFileSync(path.join(CHAR, 'parts.json'), 'utf8'));
const ATLAS = decodePng(fs.readFileSync(path.join(CHAR, 'parts.png')));
const XMETA = JSON.parse(fs.readFileSync(path.join(CHAR, 'extras.json'), 'utf8'));
const XATLAS = decodePng(fs.readFileSync(path.join(CHAR, 'extras.png')));
const CATEGORIES = ['joy', 'threat', 'achievement', 'loss', 'uncertainty', 'connection'];
const N = (values) => ({ type: 'neural', values });
const BASE = { trust: 0.5, curiosity: 0.5, vigilance: 0.1, attachment: 0.5, stability: 0.7, satisfaction: 0.5 };

async function stateOf(events) {
  const { initialState, reduce, derive } = await load('model.js');
  const s = events.reduce(reduce, initialState());
  return { state: s, pose: derive(s).pose };
}

const FXMETA = JSON.parse(fs.readFileSync(path.join(CHAR, 'fx.json'), 'utf8'));
const words = (c) => c.split(/[\s/(),]+/).filter((w) => /^[a-z]+$/.test(w) && !['none', 'at', 'rest', 'curiosity'].includes(w));

test('the tables only name the existing emotion categories, the drawn parts and round A\'s contracts', async () => {
  const { FACE_RULES, TAIL_MOOD, DESK_IDS, RULE_KEYS, MOTIONS } = await load('motion_catalog.js');
  for (const r of FACE_RULES) {
    for (const c of words(r.category)) assert.ok(CATEGORIES.includes(c), `${r.id}: category ${c}`);
    if (r.eyes) assert.ok(META.eyes.l[`${r.eyes}.c`] && META.eyes.r[`${r.eyes}.c`], `${r.id}: eyes ${r.eyes}`);
    if (r.mouth) assert.ok(META.mouths[r.mouth], `${r.id}: mouth ${r.mouth}`);
    if (r.brows) assert.ok(META.brows.l[r.brows], `${r.id}: brows ${r.brows}`);
    for (const n of r.xfx) assert.ok(n === 'sparkle' || FXMETA.fx[n] || (FXMETA.fx[`${n}.l`] && FXMETA.fx[`${n}.r`]), `${r.id}: fx ${n}`);
    if (r.ears) assert.ok(DESK_IDS.ears.includes(r.ears) && (r.ears === 'red' ? XMETA.ears.l['red.0'] : XMETA.ears.l[r.ears]), `${r.id}: ears ${r.ears}`);
    assert.ok(typeof r.why === 'string' && r.why.length > 8);
  }
  assert.equal(new Set(FACE_RULES.map((r) => r.id)).size, FACE_RULES.length);
  // every new face asset of round A 3 is wired (a rule or a motion)
  const usedEyes = new Set([...FACE_RULES.map((r) => r.eyes), ...MOTIONS.flatMap((m) => (m.tracks.eyes || []).map((k) => k[1]))]);
  const usedMouths = new Set([...FACE_RULES.map((r) => r.mouth), ...MOTIONS.flatMap((m) => (m.tracks.mouth || []).map((k) => k[1]))]);
  for (const e of ['jito', 'teary', 'scheme', 'shy', 'arc', 'puzzled', 'panic']) assert.ok(usedEyes.has(e), `eyes ${e}`);
  for (const m of ['small_open', 'mutto', 'smirk', 'hen', 'gasp', 'laugh', 'niko']) assert.ok(usedMouths.has(m), `mouth ${m}`);
  const usedFx = new Set(FACE_RULES.flatMap((r) => r.xfx));
  for (const n of ['blush.soft', 'blush.strong', 'blush.nose', 'sweat.drop', 'sweat.fly', 'gloom', 'anger', 'tear', 'sparkle']) assert.ok(usedFx.has(n), `fx ${n}`);
  for (const [emotion, m] of Object.entries(TAIL_MOOD)) {
    for (const c of words(m.category)) assert.ok(CATEGORIES.includes(c), `${emotion}: ${c}`);
    for (const f of m.frames) assert.ok(XMETA.tail.front[f], `${emotion}: ${f}`);
    // a sway step is held 4-8 frames at 30 fps (the extras contract), a still pose any time
    if (new Set(m.frames).size > 1) assert.ok(m.hold >= 4 / 30 - 1e-9 && m.hold <= 8 / 30 + 1e-9, `${emotion}: hold ${m.hold}`);
  }
  // the emotions are the derived pose's own (model.js EMOTION_RULES); none is defined here
  const { EMOTION_RULES } = await load('model.js');
  const emotions = new Set(EMOTION_RULES.map((r) => r.emotion));
  for (const e of Object.keys(TAIL_MOOD)) assert.ok(emotions.has(e), e);
  for (const r of FACE_RULES) for (const e of r.when.emotion || []) assert.ok(emotions.has(e), e);
  // the facts the rows read exist
  const { faceFacts } = await load('motion_model.js');
  const { state, pose } = await stateOf([N(BASE)]);
  const keys = Object.keys(faceFacts(state, pose));
  for (const r of FACE_RULES) for (const k of Object.keys(r.when)) assert.ok(keys.includes(k), `${r.id}: ${k}`);
  assert.ok(RULE_KEYS.includes('test'));
});

// The face over time for a script of [t, events] (FaceHolder; frames at 30 fps).
async function faces(script, until) {
  const { initialState, reduce, derive } = await load('model.js');
  const { FaceHolder } = await load('motion_model.js');
  const h = new FaceHolder();
  let s = initialState();
  const out = [];
  let k = 0;
  for (let i = 0; i <= Math.round(until * 30); i += 1) {
    const t = i / 30;
    while (k < script.length && script[k][0] <= t + 1e-9) {
      for (const ev of script[k][1]) s = reduce(s, ev);
      k += 1;
    }
    out.push({ t, ...h.update(t, s, derive(s).pose) });
  }
  return out;
}
const at = (list, t) => list.find((f) => f.t >= t - 1e-9);

test('faces from real events: threat, uncertainty, loss, achievement, connection (the table), and none at rest', async () => {
  const one = async (events, t = 0.5) => at(await faces([[0, events]], t + 0.1), t);
  assert.equal((await one([N(BASE)])).id, 'none');
  const p = await one([N(BASE), { type: 'breaker', level: 'CRITICAL' }]);
  assert.deepEqual([p.id, p.eyes, p.mouth, p.ears], ['panic', 'panic', 'gasp', 'droop']);
  assert.match(p.xfx, /gloom/);
  assert.match(p.xfx, /sweat\.fly/);
  // the cue ends with the next work event: then the sustained threat (警戒), anger only when vigilant
  const w = await faces([[0, [N({ ...BASE, vigilance: 0.3 }), { type: 'breaker', level: 'CRITICAL' }]], [2, [{ type: 'turn.start' }]]], 2.5);
  assert.equal(at(w, 2.4).id, 'wary');
  assert.equal(at(w, 2.4).xfx, '');
  const a = await faces([[0, [N({ ...BASE, vigilance: 0.7 }), { type: 'breaker', level: 'CRITICAL' }]], [2, [{ type: 'turn.start' }]]], 2.5);
  assert.deepEqual([at(a, 2.4).id, at(a, 2.4).xfx, at(a, 2.4).brows], ['alarm', 'anger', 'angry']);
  const u = await one([N(BASE), { type: 'breaker', level: 'WARNING' }]);
  assert.deepEqual([u.id, u.eyes, u.xfx, u.ears], ['puzzled', 'puzzled', 'sweat.drop', 'droop']);
  const tool = [N(BASE), { type: 'turn.start' }, { type: 'tool.start', id: 'x', kind: 'tool', name: 'fetch' }, { type: 'tool.end', id: 'x', ok: false }];
  assert.equal((await one(tool)).id, 'stumble');
  const test = (ok) => [N(BASE), { type: 'turn.start' }, { type: 'tool.start', id: 't', kind: 'tool', category: 'other', name: 'run_tests' }, { type: 'tool.end', id: 't', ok }, { type: 'turn.end', ok: true }];
  const smug = await one(test(true));
  assert.deepEqual([smug.id, smug.eyes, smug.mouth], ['smug', 'arc', 'smirk']);
  assert.match(smug.xfx, /^sparkle\.[123]$/);
  const tired = await one(test(false));
  assert.deepEqual([tired.id, tired.eyes, tired.mouth], ['tired', 'jito', 'hen']);
  const err = await one([N(BASE), { type: 'turn.start' }, { type: 'turn.end', ok: false }]);
  assert.deepEqual([err.id, err.eyes, err.xfx], ['teary', 'teary', 'tear']);
  const ask = [N(BASE), { type: 'turn.start' }, { type: 'approval.request', id: 'a1', summary: 'x' }, { type: 'approval.resolved', id: 'a1', decision: 'approve' }];
  const shy = await one(ask);
  assert.deepEqual([shy.id, shy.eyes, shy.xfx, shy.ears], ['shy', 'shy', 'blush.soft', 'red']);
  const strong = await one([N({ ...BASE, attachment: 0.8, trust: 0.7 }), ...ask.slice(1)]);
  assert.deepEqual([strong.id, strong.xfx], ['shy_strong', 'blush.strong+blush.nose']);
  const fond = await one([N({ ...BASE, attachment: 0.8, trust: 0.7 })]);
  assert.deepEqual([fond.id, fond.eyes, fond.xfx, fond.ears], ['fond', null, 'blush.soft', 'red']);
  assert.equal((await one([N({ ...BASE, attachment: 0.8, trust: 0.5 })])).id, 'none', 'attachment alone is not enough');
  const denied = await one([...ask.slice(0, 3), { type: 'approval.resolved', id: 'a1', decision: 'deny' }]);
  assert.notEqual(denied.id, 'shy');
  assert.equal((await one([N({ ...BASE, curiosity: 0.82 })])).mouth, 'small_open');
});

test('the face holds: a reaction lasts its `for`, a new face waits FACE_MIN_HOLD (the breaker does not), the sparkle twinkles 1 2 3 2', async () => {
  const { FACE_MIN_HOLD, FACE_RULES, SPARKLE } = await load('motion_catalog.js');
  const forOf = (id) => FACE_RULES.find((r) => r.id === id).for;
  const test = [N(BASE), { type: 'turn.start' }, { type: 'tool.start', id: 't', kind: 'tool', category: 'other', name: 'run_tests' }, { type: 'tool.end', id: 't', ok: true }, { type: 'turn.end', ok: true }];
  const f = await faces([[0, test]], 6);
  assert.equal(at(f, 0.1).id, 'smug');
  assert.equal(at(f, forOf('smug') - 0.1).id, 'smug');
  assert.equal(at(f, forOf('smug') + 0.1).id, 'none', 'after its `for` the face returns to the emotion\'s');
  const seq = [];
  for (let i = 0; i < 4; i += 1) seq.push(at(f, i * SPARKLE.hold + 0.04).xfx);
  assert.deepEqual(seq, ['sparkle.1', 'sparkle.2', 'sparkle.3', 'sparkle.2']);
  // a new face waits for the minimum hold: approved at 0, the turn ends ok (tests passed) at 0.5
  const ask = [N(BASE), { type: 'turn.start' }, { type: 'approval.request', id: 'a1', summary: 'x' }, { type: 'approval.resolved', id: 'a1', decision: 'approve' }];
  const g = await faces([[0, ask], [0.5, [{ type: 'tool.start', id: 't', kind: 'tool', category: 'other', name: 'run_tests' }, { type: 'tool.end', id: 't', ok: true }, { type: 'turn.end', ok: true }]]], 3);
  assert.equal(at(g, 0.6).id, 'shy', 'held');
  assert.equal(at(g, FACE_MIN_HOLD + 0.05).id, 'smug', 'then the new face');
  // the breaker does not wait
  const b = await faces([[0, ask], [0.3, [{ type: 'breaker', level: 'CRITICAL' }]]], 1);
  assert.equal(at(b, 0.34).id, 'panic');
  // the face changes only at events or when a hold / for runs out
  const switches = (list) => list.filter((x, i) => i && x.id !== list[i - 1].id).length;
  assert.ok(switches(f) <= 1 && switches(g) <= 3, `${switches(f)} / ${switches(g)}`);
  assert.deepEqual(await faces([[0, ask]], 2), await faces([[0, ask]], 2), 'deterministic');
});

test('the player puts the state\'s face over the motion\'s (closed eyes stay), and its effects replace the old blush / sweat marks', async () => {
  const { buildMotions, MOTIONS } = await load('motion_catalog.js');
  const { partIds } = await load('char_rig.js');
  const { MotionPlayer } = await load('motion.js');
  const { faceOf } = await load('motion_model.js');
  const { FACE_RULES } = await load('motion_catalog.js');
  const { map } = buildMotions(MOTIONS, partIds(META));
  const row = (id) => FACE_RULES.find((r) => r.id === id);
  const pose = { emotion: 'joy', eyes: 'happy', mouth: 'grin', headY: 0, ears: 0, blush: 1, sweat: 0 };
  const p = new MotionPlayer(map, 3);
  const f = p.tick(0, pose, undefined, faceOf(row('shy'))).frame;
  assert.deepEqual([f.eyes, f.gaze, f.xfx, f.blush, f.ears], ['shy', 'c', 'blush.soft', 0, 'red']);
  const q = new MotionPlayer(map, 3);
  q.select('sip', 'x');
  q.tick(0, pose, undefined, faceOf(row('smug')));
  const fs2 = [];
  for (let t = 0; t < 2.7; t += 1 / 30) fs2.push(q.tick(t, pose, undefined, faceOf(row('smug'), t)).frame);
  assert.ok(fs2.some((x) => x.eyes === 'closed'), 'the sip still closes the eyes');
  assert.ok(fs2.some((x) => x.eyes === 'arc'));
  assert.ok(fs2.every((x) => x.mouth === 'smirk'));
  const n = new MotionPlayer(map, 3).tick(0, { ...pose, sweat: 1 }, undefined, faceOf(row('panic'))).frame;
  assert.deepEqual([n.eyes, n.mouth, n.sweat], ['panic', 'gasp', 0]);
});

test('the rig draws the face effects of a frame in their slots at their anchors (fx.json), whole pixels in the palette', async () => {
  const { CharRig, NEUTRAL_FRAME } = await load('char_rig.js');
  const FXATLAS = decodePng(fs.readFileSync(path.join(CHAR, 'fx.png')));
  const plain = new CharRig(META, ATLAS, { meta: XMETA, atlas: XATLAS });
  const rig = new CharRig(META, ATLAS, { meta: XMETA, atlas: XATLAS }, { meta: FXMETA, atlas: FXATLAS });
  const px = (r, f) => Uint8Array.from(r.compose({ ...NEUTRAL_FRAME, ...f }).pixels);
  const W = META.size[0];
  const A = META.sheets.base.anchors;
  assert.deepEqual(px(rig, {}), px(plain, {}), 'no effect: the same picture');
  const pal = new Set(Object.values(META.palette).map((h) => parseInt(h.slice(1), 16)));
  for (const name of ['blush.soft', 'blush.strong+blush.nose', 'sweat.drop', 'sweat.fly', 'gloom', 'anger', 'tear', 'sparkle.1', 'sparkle.3']) {
    const pic = px(rig, { xfx: name });
    let seen = 0;
    let all = 0;
    for (const n of name.split('+')) for (const id of FXMETA.fx[n] ? [n] : [`${n}.l`, `${n}.r`]) {
      const part = FXMETA.fx[id];
      const x0 = Math.round(A[part.anchor][0]) + part.ox;
      const y0 = Math.round(A[part.anchor][1]) + part.oy;
      for (let j = 0; j < part.h; j += 1) for (let i = 0; i < part.w; i += 1) {
        const q = ((part.ay + j) * FXATLAS.width + part.ax + i) * 4;
        if (FXATLAS.data[q + 3] !== 255) continue;
        const o = ((y0 + j) * W + x0 + i) * 4;
        all += 1;
        if (pic[o] === FXATLAS.data[q] && pic[o + 1] === FXATLAS.data[q + 1] && pic[o + 2] === FXATLAS.data[q + 2]) seen += 1;
      }
    }
    // blush-slot effects sit under the bangs (the hair layer may cover a few), fx-slot ones on top
    assert.ok(seen / all > 0.6, `${name}: ${seen}/${all}`);
    for (let i = 0; i < pic.length; i += 4) if (pic[i + 3]) assert.ok(pal.has((pic[i] << 16) | (pic[i + 1] << 8) | pic[i + 2]), name);
  }
  // the frame key carries the effects (the texture cache)
  const { frameKey } = await load('char_rig.js');
  assert.notEqual(frameKey({ ...NEUTRAL_FRAME, xfx: 'gloom' }), frameKey(NEUTRAL_FRAME));
});

test('how the turn ended by its tests: smug when they passed, a sigh when one failed (the rules, the demo steps)', async () => {
  const run = (ok) => [N(BASE), { type: 'turn.start' }, { type: 'tool.start', id: 't', kind: 'tool', category: 'other', name: 'run_tests' }, { type: 'tool.end', id: 't', ok }, { type: 'turn.end', ok: true }];
  assert.equal((await stateOf(run(true))).pose.motion, 'smug');
  assert.equal((await stateOf(run(false))).pose.motion, 'sigh');
  // a turn without tests: as before (satisfied -> stretch, else sip)
  assert.equal((await stateOf([N(BASE), { type: 'turn.start' }, { type: 'turn.end', ok: true }])).pose.motion, 'sip');
  // the turn failed: slump, whatever the tests
  assert.equal((await stateOf([...run(true).slice(0, -1), { type: 'turn.end', ok: false }])).pose.motion, 'slump');
  // the drift key carries the test result (equal keys -> equal motion)
  const { inputKey } = await load('model.js');
  assert.notEqual(inputKey((await stateOf(run(true))).state), inputKey((await stateOf(run(false))).state));
  const { MOTION_STEPS } = await load('demo_motions.js');
  for (const [id, motion] of [['tests-done', 'smug'], ['tests-tired', 'sigh']]) {
    assert.equal((await stateOf(MOTION_STEPS.find((s) => s.id === id).events)).pose.motion, motion, id);
  }
  const fond = await stateOf(MOTION_STEPS.find((s) => s.id === 'fond').events);
  const { FaceHolder } = await load('motion_model.js');
  assert.equal(new FaceHolder().update(0, fond.state, fond.pose).ears, 'red');
});

test('the player: the tail follows the emotion on the clock, a motion\'s own ears / tail track wins', async () => {
  const { buildMotions, MOTIONS, tailFor, TAIL_MOOD } = await load('motion_catalog.js');
  const { partIds } = await load('char_rig.js');
  const { MotionPlayer } = await load('motion.js');
  const { map } = buildMotions(MOTIONS, partIds(META));
  const pose = (emotion) => ({ emotion, eyes: 'open', mouth: 'neutral', headY: 0, ears: 0, blush: 0, sweat: 0 });
  const p = new MotionPlayer(map, 3);
  for (const emotion of Object.keys(TAIL_MOOD)) {
    const seen = [];
    for (let t = 0; t < 3; t += 1 / 30) seen.push(p.tick(t, pose(emotion)).frame.tail);
    const want = [];
    for (let t = 0; t < 3; t += 1 / 30) want.push(tailFor(emotion, t));
    assert.deepEqual(seen, want, emotion);
  }
  assert.ok(new Set(Array.from({ length: 90 }, (_, i) => tailFor('joy', i / 30))).size >= 3, 'joy wags');
  assert.deepEqual(new Set(Array.from({ length: 90 }, (_, i) => tailFor('worried', i / 30))), new Set(['down']));
  // the state's ears unless the motion has a track
  assert.equal(p.tick(10, pose('calm'), undefined, { ears: 'red' }).frame.ears, 'red');
  assert.equal(p.tick(10.1, pose('calm')).frame.ears, 'angle');
  const q = new MotionPlayer(map, 3);
  q.select('surprised', 'breaker.rise');
  const f0 = q.tick(0, pose('tense'), undefined, { ears: 'droop' }).frame;
  assert.equal(f0.ears, 'twitch1', 'the flick wins over the state');
  assert.equal(f0.tail, 'up');
  assert.equal(q.tick(0.1, pose('tense'), undefined, { ears: 'droop' }).frame.ears, 'twitch2');
  assert.equal(q.tick(0.3, pose('tense'), undefined, { ears: 'droop' }).frame.ears, 'droop', 'after the flick: the state\'s ears');
  const s = new MotionPlayer(map, 3);
  s.select('smug', 'tests');
  assert.equal(s.tick(0, pose('joy')).frame.tail, 'c');
  assert.equal(s.tick(0.5, pose('joy')).frame.tail, 'up');
});

test('the rig draws round A\'s drawings at their anchors: droop / red / flick ears, the tail behind her in front, in front of the coat from behind', async () => {
  const { CharRig, NEUTRAL_FRAME } = await load('char_rig.js');
  const plain = new CharRig(META, ATLAS);
  const rig = new CharRig(META, ATLAS, { meta: XMETA, atlas: XATLAS });
  const px = (r, f) => Uint8Array.from(r.compose({ ...NEUTRAL_FRAME, ...f }).pixels);
  const W = META.size[0];
  // a sprite of extras at (x, y): every opaque pixel of it is in the picture unless a later layer covers it
  const covered = (pic, part, x, y) => {
    let same = 0;
    let all = 0;
    for (let j = 0; j < part.h; j += 1) for (let i = 0; i < part.w; i += 1) {
      const q = ((part.ay + j) * XATLAS.width + part.ax + i) * 4;
      if (XATLAS.data[q + 3] !== 255) continue;
      const tx = x + i;
      const ty = y + j;
      if (tx < 0 || ty < 0 || tx >= W || ty >= W) continue;
      all += 1;
      const o = (ty * W + tx) * 4;
      if (pic[o] === XATLAS.data[q] && pic[o + 1] === XATLAS.data[q + 1] && pic[o + 2] === XATLAS.data[q + 2]) same += 1;
    }
    return same / all;
  };
  const A = META.sheets.base.anchors;
  // without extras nothing changes (the rest frame of the parts)
  assert.deepEqual(px(new CharRig(META, ATLAS), { ears: 'droop', tail: 'up' }), px(plain, {}));
  // std ears = the parts' 0 deg ear (the contract): with extras at rest only the tail is added
  const rest = px(rig, {});
  const t = XMETA.tail.front.c;
  for (let i = 0; i < W * W; i += 1) {
    const x = i % W;
    const y = (i / W) | 0;
    if (x >= t.ox && x < t.ox + t.w && y >= t.oy && y < t.oy + t.h) continue;
    for (let k = 0; k < 4; k += 1) if (rest[i * 4 + k] !== px(plain, {})[i * 4 + k]) assert.fail(`rest changed outside the tail at ${x},${y}`);
    if (i > 2000) break; // (sampled: the full check is the front fixture)
  }
  // the tail behind her: visible where she is not, never over her body
  assert.ok(covered(rest, t, t.ox, t.oy) > 0.3, 'front tail visible');
  for (const name of ['droop', 'twitch1', 'twitch2']) {
    const pic = px(rig, { ears: name });
    for (const side of ['l', 'r']) {
      const part = XMETA.ears[side][name];
      const [ax, ay] = A[`ear_${side}`];
      assert.ok(covered(pic, part, Math.round(ax) + part.ox, Math.round(ay) + part.oy) > 0.8, `${name} ${side}`);
    }
  }
  // red: the pink twin of the ear at the angle the channels ask for
  for (const earL of [0, -12, 12]) {
    const pic = px(rig, { ears: 'red', earL, earR: earL });
    const part = XMETA.ears.r[`red.${earL}`];
    const [ax, ay] = A.ear_r;
    assert.ok(covered(pic, part, Math.round(ax) + part.ox, Math.round(ay) + part.oy) > 0.8, `red ${earL}`);
  }
  // the back view: tail.back in front of the coat
  const back = px(rig, { sheet: 'back', tail: 'r2' });
  const tb = XMETA.tail.back.r1;
  assert.ok(covered(back, tb, tb.ox, tb.oy) > 0.8, 'back tail (r2 -> r1)');
  // whole pixels in the palette
  const pal = new Set(Object.values(META.palette).map((h) => parseInt(h.slice(1), 16)));
  for (const pic of [rest, back, px(rig, { ears: 'red' }), px(rig, { ears: 'droop', tail: 'down' })]) {
    for (let i = 0; i < pic.length; i += 4) {
      if (pic[i + 3] === 0) continue;
      assert.equal(pic[i + 3], 255);
      assert.ok(pal.has((pic[i] << 16) | (pic[i + 1] << 8) | pic[i + 2]), `#${((pic[i] << 16) | (pic[i + 1] << 8) | pic[i + 2]).toString(16)}`);
    }
  }
});

test('the face effects stay out of the room\'s grading and the fx-slot ones stay over the hair (fxMask, mask G)', async () => {
  const { CharRig, NEUTRAL_FRAME } = await load('char_rig.js');
  const FXM = JSON.parse(fs.readFileSync(path.join(CHAR, 'fx.json'), 'utf8'));
  const FXA = decodePng(fs.readFileSync(path.join(CHAR, 'fx.png')));
  const rig = new CharRig(META, ATLAS, { meta: XMETA, atlas: XATLAS }, { meta: FXM, atlas: FXA });
  const count = (f) => {
    rig.compose({ ...NEUTRAL_FRAME, ...f });
    let fx = 0;
    let g = 0;
    for (let i = 0; i < rig.fxl.length; i += 1) {
      if (rig.fxMask[i * 4 + 3] === 255) fx += 1;
      if (rig.fxl[i] === 2 && rig.mask[i * 4 + 1] === 255) g += 1;
      // RGB of the effect mask stays 0 (the old shader's offset channels)
      assert.equal(rig.fxMask[i * 4] + rig.fxMask[i * 4 + 1] + rig.fxMask[i * 4 + 2], 0);
    }
    return { fx, g };
  };
  assert.deepEqual(count({}), { fx: 0, g: 0 });
  const drop = count({ xfx: 'sweat.drop' });
  assert.ok(drop.fx > 10 && drop.g === drop.fx, `sweat.drop: ${JSON.stringify(drop)} (fx slot: over the hair)`);
  const gloom = count({ xfx: 'gloom' });
  assert.ok(gloom.fx > 10 && gloom.g === 0, 'gloom: blush slot, under the bangs');
  // a later layer over an effect clears its mark (the brows over the gloom)
  rig.compose({ ...NEUTRAL_FRAME, xfx: 'gloom' });
  const browLayer = rig.order.indexOf('brows') + 1;
  for (let i = 0; i < rig.layer.length; i += 1) if (rig.layer[i] === browLayer) assert.equal(rig.fxMask[i * 4 + 3], 0);
  // the shader reads it: not graded, no rim
  const src = fs.readFileSync(path.join(STAGE, 'js', 'scene.js'), 'utf8');
  assert.match(src, /bool fxPx = r2\.a > 0\.5;\s*\n\s*if \(!fxPx\) c\.rgb = grade\(c\.rgb\);/);
});

test('round 5 work motions from real events: approval -> await_mug (ask, then rest with the mug), web -> peer, memory / files -> look_shelf with reading', async () => {
  const motionOf = async (events) => (await stateOf(events)).pose.motion;
  const ask = [N(BASE), { type: 'turn.start' }, { type: 'approval.request', id: 'a1', summary: 'write_file: x' }];
  assert.equal(await motionOf(ask), 'await_mug');
  // an approval asked while the web is searched: waiting for the answer comes first
  assert.equal(await motionOf([...ask, { type: 'tool.start', id: 'w', kind: 'web', name: 'web_search' }]), 'await_mug');
  const web = [N(BASE), { type: 'turn.start' }, { type: 'tool.start', id: 'w', kind: 'web', name: 'web_search' }];
  assert.equal(await motionOf(web), 'peer');
  // the props still ask for their head tilt (the magnifier), the rule wins
  const { pose } = await stateOf(web);
  assert.equal(pose['gadget.motion'], 'head_tilt');
  // code still wins over the web (the laptop out in front of her)
  assert.equal(await motionOf([...web, { type: 'tool.start', id: 'e', kind: 'tool', category: 'code', name: 'editor' }]), 'typing');
  assert.equal(await motionOf([N(BASE), { type: 'turn.start' }, { type: 'tool.start', id: 'm', kind: 'memory', name: 'memory_search' }]), 'look_shelf');
  // the motions: the rest with the mug, the lean, the reading phase
  const { MOTIONS } = await load('motion_catalog.js');
  const m = Object.fromEntries(MOTIONS.map((x) => [x.id, x]));
  const after = (track, t) => track.filter(([k]) => k >= t).map(([, v]) => v);
  assert.ok(after(m.await_mug.tracks.armR, m.await_mug.intro).includes('mug_lift') && after(m.await_mug.tracks.armR, m.await_mug.intro).includes('mug_sip'), 'rests with the mug, a sip now and then');
  assert.ok(m.await_mug.loop && m.await_mug.intro >= 1.5, 'asks first (intro), then the rest loops');
  assert.ok(m.peer.loop && m.peer.tracks.eyes.some(([, e]) => e === 'sharp') && m.peer.tracks.gaze.some(([, g]) => g === 'ur'), 'peers toward the window');
  assert.ok(after(m.look_shelf.tracks.gaze, 2.0).includes('d') && m.look_shelf.tracks.sheet.some(([, s]) => s === 'down1'), 'reads with the head lowered');
  // ears stay constant in the new motions (no extra frames for the art lint's budget)
  for (const id of ['await_mug', 'peer']) for (const ch of ['earL', 'earR']) assert.equal(m[id].tracks[ch].length, 1, `${id} ${ch}`);
});

test('round A 5: the pose arms paused (smug: chest out, ponder: the head), laugh / niko from the turn\'s end (the table)', async () => {
  const { MOTIONS, FACE_RULES } = await load('motion_catalog.js');
  const m = Object.fromEntries(MOTIONS.map((x) => [x.id, x]));
  const at = (track, t) => {
    let v = track[0][1];
    for (const [k, x] of track) if (k <= t + 1e-9) v = x;
    return v;
  };
  // the pose arms are paused (free hands are not drawn, 2026-10-18): smug and ponder keep the
  // mug in hand (tests/stage_hands_test.cjs measures every frame)
  for (const id of ['smug', 'ponder']) {
    for (const tr of [m[id].tracks, m[id].exit ? m[id].exit.tracks : {}]) {
      assert.ok(!(tr.armL || []).length, `${id}: no free left arm`);
      for (const [, v] of tr.armR || []) assert.ok(!['cross', 'ponder_cross'].includes(v), `${id}: ${v} is paused`);
    }
  }
  assert.ok(m.smug.tracks.sheet.some(([, v]) => v === 'straight') && m.smug.tracks.sheet.some(([, v]) => v === 'up1'), 'smug: chest out, chin up');
  // the faces: laugh + arc for a strong joy at the turn's success, niko for a plain good end
  const one = async (events, t = 0.5) => at2(await facesAt(events, t + 0.1), t);
  const test = (sat) => [N({ ...BASE, satisfaction: sat }), { type: 'turn.start' }, { type: 'tool.start', id: 't', kind: 'tool', category: 'other', name: 'run_tests' }, { type: 'tool.end', id: 't', ok: true }, { type: 'turn.end', ok: true }];
  const tri = await one(test(0.9));
  assert.deepEqual([tri.id, tri.eyes, tri.mouth], ['triumph', 'arc', 'laugh']);
  assert.match(tri.xfx, /^sparkle/);
  assert.equal((await one(test(0.84))).id, 'smug', 'below 0.85: smug (arc + smirk)');
  const done = [N(BASE), { type: 'turn.start' }, { type: 'turn.end', ok: true }];
  const c = await one(done);
  assert.deepEqual([c.id, c.mouth, c.eyes], ['content', 'niko', null]);
  const later = await facesAt(done, 4);
  assert.equal(at2(later, 3.2).id, 'none', 'niko lasts its 3 s');
  assert.equal(FACE_RULES.find((r) => r.id === 'triumph').category, 'achievement');
  // the demo step
  const { MOTION_STEPS } = await load('demo_motions.js');
  const st = MOTION_STEPS.find((s) => s.id === 'tests-elated');
  assert.equal((await stateOf(st.events)).pose.motion, 'smug');
  assert.equal(at2(await facesAt(st.events, 0.6), 0.5).id, 'triumph');
});

async function facesAt(events, until) {
  const { initialState, reduce, derive } = await load('model.js');
  const { FaceHolder } = await load('motion_model.js');
  const h = new FaceHolder();
  let s = initialState();
  for (const ev of events) s = reduce(s, ev);
  const out = [];
  for (let i = 0; i <= Math.round(until * 30); i += 1) out.push({ t: i / 30, ...h.update(i / 30, s, derive(s).pose) });
  return out;
}
const at2 = (list, t) => list.find((f) => f.t >= t - 1e-9);

test('the rig draws round A\'s pose arms from poses.png (both slots), the mug put away while no hand holds it', async () => {
  const { CharRig, NEUTRAL_FRAME, partIds, POSE_ARM_IDS } = await load('char_rig.js');
  const PM = JSON.parse(fs.readFileSync(path.join(CHAR, 'poses.json'), 'utf8'));
  const PA = decodePng(fs.readFileSync(path.join(CHAR, 'poses.png')));
  const rig = new CharRig(META, ATLAS, { meta: XMETA, atlas: XATLAS }, null, { meta: PM, atlas: PA });
  assert.deepEqual([...POSE_ARM_IDS].sort(), Object.keys(PM.arms.r).sort());
  for (const id of POSE_ARM_IDS) assert.ok(partIds(META).armR.includes(id) && partIds(META).armL.includes(id));
  assert.ok(!META.arms.r.cross, 'parts.json itself is not changed (the rig keeps a copy)');
  const W = META.size[0];
  for (const name of POSE_ARM_IDS) {
    const r = rig.compose({ ...NEUTRAL_FRAME, armR: name, armL: name });
    const pic = Uint8Array.from(r.pixels);
    // no mug: the pose put it away (on the desk edge it would float over her coat); no steam
    assert.equal(r.mug, null, `${name}: no mug drawn`);
    for (const side of ['r', 'l']) {
      const part = PM.arms[side][name];
      let seen = 0;
      let all = 0;
      for (let j = 0; j < part.h; j += 1) for (let i = 0; i < part.w; i += 1) {
        const q = ((part.ay + j) * PA.width + part.ax + i) * 4;
        if (PA.data[q + 3] !== 255) continue;
        all += 1;
        const o = ((part.oy + j) * W + part.ox + i) * 4;
        if (pic[o] === PA.data[q] && pic[o + 1] === PA.data[q + 1] && pic[o + 2] === PA.data[q + 2]) seen += 1;
      }
      // later layers cover part of it: the left forearm crosses over the right one (the pose), hair tips
      assert.ok(all > 100 && seen / all > (side === 'r' ? 0.6 : 0.8), `${name}.${side}: ${seen}/${all}`);
    }
  }
  // without poses.json the ids fall back (the mug arm, no free arm): no crash
  const plain = new CharRig(META, ATLAS);
  assert.doesNotThrow(() => plain.compose({ ...NEUTRAL_FRAME, armR: 'cross', armL: 'cross' }));
});
