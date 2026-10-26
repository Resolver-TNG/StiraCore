// Stage frame pacing (#341): the 30 fps default, ?fps= / localStorage validation, the
// accumulator on 60 / 120 / 144 Hz displays with jitter, big gaps and the hidden tab, the
// deterministic ?frames= path left alone, and motion that keeps its tempo at any frame rate.
// node --test tests/stage_pacer_test.cjs
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const ROOT = path.join(__dirname, '..', 'dashboard', 'stage');
const load = (name) => import(path.join(ROOT, 'js', name));
const META = JSON.parse(fs.readFileSync(path.join(ROOT, 'assets', 'char', 'hair.json'), 'utf8'));

// A display: rAF timestamps at `hz` with deterministic jitter (ms). Calls onRun(dt, tMs) for each
// drawn frame and returns the number of drawn frames.
function display(pacer, { hz, seconds, jitter = 0, start = 1000, minFps = () => 0, onRun = () => {} }) {
  let seed = 7;
  const rnd = () => {
    seed = (seed * 1664525 + 1013904223) >>> 0;
    return seed / 4294967296 - 0.5;
  };
  let runs = 0;
  const n = Math.round(seconds * hz);
  for (let i = 0; i <= n; i += 1) {
    const t = start + (i * 1000) / hz + (i ? rnd() * 2 * jitter : 0);
    const r = pacer.frame(t, { minFps: minFps(t) });
    if (r.run) {
      runs += 1;
      onRun(r.dt, t);
    }
  }
  return runs;
}

test('parseFps: integers 15..120 only; everything else is refused', async () => {
  const { parseFps, DEFAULT_FPS, MIN_FPS, MAX_FPS } = await load('pacer.js');
  assert.equal(DEFAULT_FPS, 30);
  assert.equal(MIN_FPS, 15);
  assert.equal(MAX_FPS, 120);
  for (const v of ['15', '24', '30', '60', '120', ' 45 ', 90, 120]) assert.ok(parseFps(v) !== null, String(v));
  for (const v of ['14', '121', '0', '-30', '30.5', '1e1', '0x1e', '', 'abc', '3 0', '030x', '１２０', '999', '1000', null, undefined, NaN, Infinity, 30.5, {}, [], true]) {
    assert.equal(parseFps(v), null, String(v));
  }
});

test('pickFps: ?fps= wins (a bad value means the default, not the stored one); then localStorage; then 30', async () => {
  const { pickFps } = await load('pacer.js');
  assert.equal(pickFps('', () => null), 30);
  assert.equal(pickFps('?fps=60', () => '24'), 60);
  assert.equal(pickFps('?lang=en&fps=120', () => null), 120);
  assert.equal(pickFps('?fps=24', () => null), 24);
  for (const bad of ['?fps=', '?fps=abc', '?fps=14', '?fps=121', '?fps=30.5', '?fps=-1', '?fps=1e2']) {
    assert.equal(pickFps(bad, () => '60'), 30, bad);
  }
  assert.equal(pickFps('', () => '60'), 60);
  assert.equal(pickFps('', () => '24'), 24);
  for (const bad of ['5', '500', 'x', '', '30.5']) assert.equal(pickFps('', () => bad), 30, bad);
  assert.equal(pickFps('', () => { throw new Error('storage blocked'); }), 30);
  assert.equal(pickFps('?fps=45'), 45);
  assert.equal(pickFps(undefined, undefined), 30);
});

// #446: one more stored choice, the board's "show the activity while she waits" tick
// (stira.stage.board_idle, written only by that tick). The fps stays read only.
test('stored settings: stira.stage.fps is only read; the only write is the board idle tick (no new route)', async () => {
  const { FPS_KEY } = await load('pacer.js');
  const { IDLE_KEY } = await load('activity.js');
  assert.equal(FPS_KEY, 'stira.stage.fps');
  assert.equal(IDLE_KEY, 'stira.stage.board_idle');
  const main = fs.readFileSync(path.join(ROOT, 'js', 'main.js'), 'utf8');
  assert.match(main, /pickFps\(location\.search, \(\) => localStorage\.getItem\(FPS_KEY\)\)/);
  const writes = main.match(/localStorage\.setItem\([^,]*/g) || [];
  assert.deepEqual(writes, ['localStorage.setItem(IDLE_KEY']);
});

test('pacer: the first frame is drawn at once with dt 0', async () => {
  const { createPacer } = await load('pacer.js');
  const p = createPacer({ fps: 30 });
  assert.deepEqual(p.frame(5000), { run: true, dt: 0 });
  assert.equal(p.frame(5016.7).run, false);
});

test('pacer: 30 fps gives 30 drawn frames a second on 60, 120 and 144 Hz displays (jitter included)', async () => {
  const { createPacer } = await load('pacer.js');
  for (const [hz, jitter] of [[60, 0], [60, 1.2], [120, 0], [120, 0.8], [144, 0.5], [75, 1]]) {
    const runs = display(createPacer({ fps: 30 }), { hz, seconds: 10, jitter });
    assert.ok(Math.abs(runs - 301) <= 2, `${hz} Hz jitter ${jitter}: ${runs} frames in 10 s`);
  }
});

test('pacer: 60 fps on a 60 Hz display draws every frame, on 120 Hz every other; a target above the display draws every rAF', async () => {
  const { createPacer } = await load('pacer.js');
  assert.ok(Math.abs(display(createPacer({ fps: 60 }), { hz: 60, seconds: 10, jitter: 1 }) - 601) <= 2);
  assert.ok(Math.abs(display(createPacer({ fps: 60 }), { hz: 120, seconds: 10, jitter: 0.8 }) - 601) <= 2);
  assert.equal(display(createPacer({ fps: 120 }), { hz: 60, seconds: 5 }), 301); // cannot exceed the display
  assert.ok(Math.abs(display(createPacer({ fps: 24 }), { hz: 60, seconds: 10, jitter: 1 }) - 241) <= 3);
  assert.ok(Math.abs(display(createPacer({ fps: 15 }), { hz: 120, seconds: 10, jitter: 1 }) - 151) <= 2);
});

test('pacer: dt is the real time since the last drawn frame; the dts add up to the elapsed time', async () => {
  const { createPacer } = await load('pacer.js');
  let sum = 0;
  let max = 0;
  let min = Infinity;
  let n = 0;
  let lastT = null;
  display(createPacer({ fps: 30 }), {
    hz: 60,
    seconds: 20,
    jitter: 1,
    onRun: (dt, t) => {
      sum += dt;
      if (lastT !== null) {
        max = Math.max(max, dt);
        min = Math.min(min, dt);
        n += 1;
        assert.ok(Math.abs(dt - (t - lastT) / 1000) < 1e-9);
      }
      lastT = t;
    },
  });
  assert.ok(Math.abs(sum - 20) < 0.05, `sum ${sum}`);
  assert.ok(min > 0.025 && max < 0.045, `dt range ${min}..${max}`);
  assert.ok(n > 500);
});

test('pacer: small dt (a very fast display) is accumulated, never drawn early', async () => {
  const { createPacer } = await load('pacer.js');
  const p = createPacer({ fps: 30 });
  p.frame(0);
  let drawn = 0;
  for (let t = 1; t <= 100; t += 1) if (p.frame(t).run) drawn += 1; // a 1000 Hz "display", 100 ms
  assert.ok(drawn >= 2 && drawn <= 4, `${drawn} frames in 100 ms`);
  const q = createPacer({ fps: 30 });
  q.frame(0);
  const at = [];
  for (let t = 1; t <= 200; t += 1) if (q.frame(t).run) at.push(t);
  for (let i = 1; i < at.length; i += 1) assert.ok(Math.abs(at[i] - at[i - 1] - 33.3) <= 2, at.join());
});

test('pacer: a big gap (stall, tab restore) is one frame with dt capped, not a replay', async () => {
  const { createPacer, MAX_DT } = await load('pacer.js');
  assert.equal(MAX_DT, 0.1);
  const p = createPacer({ fps: 30 });
  p.frame(0);
  p.frame(16.7);
  assert.equal(p.frame(33.4).run, true);
  const r = p.frame(33.4 + 60000); // a minute later
  assert.equal(r.run, true);
  assert.equal(r.dt, 0.1);
  // the backlog was dropped: the next rAF 16.7 ms later does not draw, 33 ms later does
  assert.equal(p.frame(33.4 + 60000 + 16.7).run, false);
  assert.equal(p.frame(33.4 + 60000 + 33.4).run, true);
  // a stall of 80 ms (under the cap) is passed on whole
  const q = createPacer({ fps: 30 });
  q.frame(0);
  assert.ok(Math.abs(q.frame(80).dt - 0.08) < 1e-9);
});

test('pacer: hidden tab -> reset -> the first frame back is drawn at once with dt 0 (no big dt)', async () => {
  const { createPacer } = await load('pacer.js');
  const p = createPacer({ fps: 30 });
  display(p, { hz: 60, seconds: 1 });
  p.reset();
  assert.deepEqual(p.frame(900000), { run: true, dt: 0 });
  assert.equal(p.frame(900016.7).run, false);
  assert.equal(p.frame(900033.4).run, true);
});

test('pacer: minFps lifts the rate for a while (the whiteboard) and the default comes back', async () => {
  const { createPacer, BOARD_FPS } = await load('pacer.js');
  assert.equal(BOARD_FPS, 60);
  const p = createPacer({ fps: 30 });
  const times = [];
  display(p, { hz: 120, seconds: 1, start: 0, minFps: (t) => (t < 270 ? BOARD_FPS : 0), onRun: (dt, t) => times.push(t) });
  const inBoost = times.filter((t) => t < 270).length;
  const after = times.filter((t) => t >= 270 && t < 1000).length;
  assert.ok(inBoost >= 15 && inBoost <= 18, `boost frames ${inBoost}`); // 0.27 s * 60
  assert.ok(after >= 21 && after <= 23, `after ${after}`); // 0.73 s * 30
  // a boost never lowers the rate below the target
  assert.ok(Math.abs(display(createPacer({ fps: 120 }), { hz: 120, seconds: 2, minFps: () => 60 }) - 241) <= 1);
});

test('pacer: setFps validates, and an out-of-range constructor value is the default', async () => {
  const { createPacer } = await load('pacer.js');
  assert.equal(createPacer({ fps: 7 }).fps, 30);
  assert.equal(createPacer({ fps: '60' }).fps, 60);
  const p = createPacer({ fps: 60 });
  p.setFps(500);
  assert.equal(p.fps, 30);
  p.setFps(24);
  assert.equal(p.fps, 24);
  assert.equal(createPacer().fps, 30);
  assert.equal(p.frame(NaN).run, false);
});

test('main.js: ?frames= stays a fixed 1/60 s step outside the pacer; a hidden tab stops the loop; rAF is scheduled in one place', () => {
  const main = fs.readFileSync(path.join(ROOT, 'js', 'main.js'), 'utf8');
  // deterministic mode: dt 1/60 and the pacer is only consulted when fixedFrames is 0
  assert.match(main, /let dt = 1 \/ 60;\s*\n\s*if \(!fixedFrames\) \{\s*\n\s*const p = pacer\.frame\(/);
  assert.match(main, /fixedFrames && frameNo >= fixedFrames/);
  // hidden: no drawing, no rAF; deterministic captures are not stopped by it
  assert.match(main, /if \(!fixedFrames && document\.hidden\) \{\s*\n\s*pacer\.reset\(\);/);
  assert.match(main, /addEventListener\('visibilitychange'/);
  assert.equal((main.match(/requestAnimationFrame\(loop\)/g) || []).length, 1);
  // the debug panel gets the target next to the measured rate
  assert.match(main, /target: targetFps/);
});

test('motion tempo does not depend on the frame rate: clocks, typewriter, board and hair at 60 / 30 / 15 fps', async () => {
  const { createPacer } = await load('pacer.js');
  const { Animator } = await load('anim.js');
  const { buildCatalog, CATALOG } = await load('catalog.js');
  const { AMBIENT_ANIMS } = await load('room.js');
  const { Typewriter } = await load('typewriter.js');
  const { stepProgress, OPEN_S } = await load('board.js');
  const { createHairRig, initHairState, stepHair } = await load('hair_rig.js');
  const { map } = buildCatalog([...CATALOG, ...AMBIENT_ANIMS]);
  const POSE = { emotion: 'calm', eyes: 'open', mouth: 'neutral', headY: 0, ears: 0, blush: 0, sweat: 0, breathRate: 1.5 };
  const text = 'こんばんは。ラボは静か。何から片付けようか。'.repeat(12);

  function run(fps) {
    const pacer = createPacer({ fps });
    const animator = new Animator(map, 341);
    const tw = new Typewriter();
    tw.reset(text);
    const rig = createHairRig(META);
    let hair = initHairState(rig);
    let prog = 0;
    let progAt = null;
    let now = 0;
    let typed = null;
    const hairTrace = new Map();
    display(pacer, {
      hz: 120,
      seconds: 12,
      jitter: 0.7,
      start: 0,
      onRun: (dt) => {
        now += dt;
        animator.tick(now, POSE);
        tw.tick(dt);
        prog = stepProgress(prog, 1, dt);
        if (progAt === null && prog >= 1) progAt = now;
        const head = [Math.round(Math.sin(now * 2.3) * 1.6), 0];
        hair = stepHair(rig, hair, { head, breath: 0, sway: Math.sin((2 * Math.PI * now) / 5.3), pose: POSE }, dt);
        const sec = Math.round(now * 2) / 2;
        if (Math.abs(now - sec) < 0.02 && !hairTrace.has(sec)) hairTrace.set(sec, hair.out.map((o) => [...o]));
        if (typed === null && now >= 3) typed = tw.shown;
      },
    });
    return { now, clocks: new Map(animator.clocks), typed, progAt, hairTrace };
  }
  const r60 = run(60);
  const r30 = run(30);
  const r15 = run(15);
  for (const [name, r, tol] of [['60', r60, 0.03], ['30', r30, 0.06], ['15', r15, 0.1]]) assert.ok(Math.abs(r.now - 12) < tol, `${name} fps: clock ${r.now}`);
  // always-on clocks (breathing, ambient) advance by real time at any rate
  assert.ok(r60.clocks.size >= 3);
  for (const [id, c60] of r60.clocks) {
    const rate = Math.max(1, c60 / r60.now);
    assert.ok(Math.abs(r30.clocks.get(id) - c60) < 0.08 * rate + 0.1, `${id}: ${c60} vs ${r30.clocks.get(id)}`);
    assert.ok(Math.abs(r15.clocks.get(id) - c60) < 0.15 * rate + 0.2, `${id}: ${c60} vs ${r15.clocks.get(id)}`);
  }
  // typewriter: the same number of characters after 3 s (38 cps), within a frame's worth
  assert.ok(r60.typed >= 100, `typed ${r60.typed} chars in 3 s`);
  assert.ok(Math.abs(r30.typed - r60.typed) <= 3, `${r30.typed} vs ${r60.typed}`);
  assert.ok(Math.abs(r15.typed - r60.typed) <= 6, `${r15.typed} vs ${r60.typed}`);
  // the whiteboard rolls down in OPEN_S whatever the rate (to one frame of the slow one)
  assert.ok(Math.abs(r60.progAt - OPEN_S) <= 0.03, `60: ${r60.progAt}`);
  assert.ok(Math.abs(r30.progAt - OPEN_S) <= 0.05, `30: ${r30.progAt}`);
  assert.ok(Math.abs(r15.progAt - OPEN_S) <= 0.09, `15: ${r15.progAt}`);
  // hair: the whole-pixel offsets follow the same swing; at the same moments at most 1 px apart
  // on at least 90% of the samples (a physical sim sampled at other instants)
  let total = 0;
  let near = 0;
  for (const [sec, a] of r60.hairTrace) {
    const b = r30.hairTrace.get(sec);
    if (!b) continue;
    a.forEach((o, i) => {
      total += 1;
      if (Math.abs(o[0] - b[i][0]) <= 1 && Math.abs(o[1] - b[i][1]) <= 1) near += 1;
    });
  }
  assert.ok(total > 100, `${total} hair samples`);
  assert.ok(near / total >= 0.9, `hair within 1 px: ${near}/${total}`);
});

test('the pixel-art tracks hold every pose for at least 2 frames at 30 fps (no key frame falls between two drawn frames)', async () => {
  const { MOTIONS } = await load('motion_catalog.js');
  let min = Infinity;
  for (const m of MOTIONS) {
    for (const tracks of [m.tracks, m.exit && m.exit.tracks]) {
      if (!tracks) continue;
      for (const keys of Object.values(tracks)) for (let i = 1; i < keys.length; i += 1) min = Math.min(min, keys[i][0] - keys[i - 1][0]);
    }
  }
  assert.ok(min >= 2 / 30 - 1e-9, `shortest hold ${min.toFixed(3)} s`);
});

test('the character follows real time: the motion tracks at the instants both rates draw are the same; blinks keep their tempo', async () => {
  const { MotionPlayer } = await load('motion.js');
  const { buildMotions, MOTIONS } = await load('motion_catalog.js');
  const { partIds } = await load('char_rig.js');
  const parts = JSON.parse(fs.readFileSync(path.join(ROOT, 'assets', 'char', 'parts.json'), 'utf8'));
  const { map } = buildMotions(MOTIONS, partIds(parts));
  const POSE = { emotion: 'calm', eyes: 'open', mouth: 'neutral', headY: 0, ears: 0, blush: 0, sweat: 0, motion: 'typing_back' };
  const trace = (step, seconds) => {
    const p = new MotionPlayer(map, 341);
    p.select('typing_back', 'test');
    const out = [];
    for (let k = 0; k * step < seconds - 1e-9; k += 1) out.push(p.tick(k * step, POSE).frame);
    return out;
  };
  const a = trace(1 / 60, 6);
  const b = trace(1 / 30, 6);
  assert.equal(b.length * 2, a.length);
  // the eyes carry the life signs (a blink starts on the first drawn frame after it is due, so
  // it can start one frame later at 30 fps); every other part is the same pixels
  b.forEach((f, i) => assert.deepEqual({ ...f, eyes: 0 }, { ...a[i * 2], eyes: 0 }, `frame ${i}`));
  // blinks over a minute: about the same count at 60, 30 and 15 fps
  const blinks = (step) => {
    let n = 0;
    let prev = 'open';
    for (const f of trace(step, 60)) {
      if (f.eyes !== 'open' && prev === 'open') n += 1;
      prev = f.eyes;
    }
    return n;
  };
  const n60 = blinks(1 / 60);
  assert.ok(n60 >= 8, `${n60} blinks in 60 s`);
  assert.ok(Math.abs(blinks(1 / 30) - n60) <= 2, `${blinks(1 / 30)} vs ${n60}`);
  assert.ok(Math.abs(blinks(1 / 15) - n60) <= 3, `${blinks(1 / 15)} vs ${n60}`);
});
