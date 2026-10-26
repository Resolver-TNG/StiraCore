// #624 / W5b fix round 1: the activity mode's wiring (activity.js startActivityController), run with the real
// board.js / activity.js / poll.js in jsdom and fake timers — node --test tests/stage_activity_ctl_test.cjs
// Off: no request and no element with the board closed. On (the watch): an iteration's start
// lowers the board with the panel; a reply takes it; a pinned board is never taken; a hidden
// tab reads nothing; the person's turn and the open log keep it closed; the cadence.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

let JSDOM = null;
try {
  ({ JSDOM } = require('jsdom'));
} catch {
  /* skipped below */
}
const ROOT = path.join(__dirname, '..');
const STAGE = path.join(ROOT, 'dashboard', 'stage', 'js');
const load = (name) => import(path.join(STAGE, name));
const Poll = require(path.join(ROOT, 'dashboard', 'static', 'js', 'poll.js'));
const CAT = JSON.parse(fs.readFileSync(path.join(ROOT, 'dashboard/static/i18n/catalog.en.json'), 'utf8'));
const t = (key, vars = {}) => {
  if (CAT[key] === undefined) throw new Error(`missing key ${key}`);
  return CAT[key].replace(/\{(\w+)\}/g, (m, k) => (k in vars ? String(vars[k]) : m));
};
const NOW = 1792416000;
const SKIP = { skip: JSDOM ? false : 'jsdom not installed' };
const HTML = `<!doctype html><html lang="en"><body><main>
  <section id="board" hidden><div id="board-head"><h2><span id="board-title-text"></span></h2>
  <button id="board-pin" type="button"></button><button id="board-copy" type="button"></button><button id="board-close" type="button"></button></div>
  <div id="board-body" tabindex="0"></div><div><span id="board-stats"></span><span id="board-note"></span></div></section>
  <button id="btn-activity" type="button" aria-pressed="false">Activity</button><input id="composer"></main></body></html>`;

const grant = (run) => ({
  id: 'abababab', state: 'active', live: true, headline: { subject: 'grant', state: 'active' }, goal_state: 'active',
  iterations: 0, max_iterations: 10, ttl_sec: 7200, ttl_left_sec: 3600, budget_total: 1000, budget_left: 1000, budget_per_tick: 100,
  awaiting_verification: false, cards: 0, card: false, iteration: { run, number: 1, tick_used: 0 }, outcomes: [],
});
const answer = (g) => ({
  server_time: NOW, started_at: NOW - 100, cursor: 0, reset: true, items: [], stall_sec: 120,
  state: { code: 'idle', since: null, last_event: null, running: false }, yolo: null,
  totals: { turns: 0, input: 0, output: 0, cache_read: 0, cache_write: 0, context_tokens: null, cost: null },
  ...(g === undefined ? {} : { grant: g }),
});

// fake time: setTimeout / setInterval for the controller and poll.js
function clock() {
  let now = 0;
  let seq = 0;
  const timers = new Map();
  const add = (fn, ms, every) => {
    seq += 1;
    timers.set(seq, { fn, at: now + ms, every });
    return seq;
  };
  return {
    setTimeout: (fn, ms) => add(fn, ms, 0),
    setInterval: (fn, ms) => add(fn, ms, ms),
    clear: (id) => timers.delete(id),
    now: () => now,
    async advance(ms) {
      const end = now + ms;
      const flush = async () => {
        for (let i = 0; i < 5; i += 1) await new Promise((r) => setImmediate(r));
      };
      await flush(); // a read in flight (a visibility return, a poke) settles first
      for (;;) {
        const due = [...timers.entries()].filter(([, v]) => v.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
        if (!due) break;
        const [id, timer] = due;
        now = timer.at;
        if (timer.every) timer.at += timer.every;
        else timers.delete(id);
        timer.fn();
        await flush();
      }
      now = end;
    },
  };
}

async function setup({ watch, body = () => answer(), status = () => ({ turnOpen: false }), logOpen = () => false, idle = false } = {}) {
  const dom = new JSDOM(HTML);
  const { document } = dom.window;
  global.HTMLElement = dom.window.HTMLElement;
  let hidden = false;
  Object.defineProperty(document, 'hidden', { get: () => hidden, configurable: true });
  const B = await load('board.js');
  const { startActivityController } = await load('activity.js');
  const time = clock();
  const board = B.createBoard({ root: document.getElementById('board'), t, reduced: () => true, copy: async () => true, openLink: () => {} });
  const urls = [];
  const env = {
    document, board, time, urls,
    setHidden(v) {
      hidden = v;
      document.dispatchEvent(new dom.window.Event('visibilitychange'));
    },
  };
  env.started = startActivityController({
    board, btn: document.getElementById('btn-activity'), doc: document,
    poll: { create: (o) => Poll.create({ ...o, setTimer: time.setTimeout, clearTimer: time.clear, now: time.now }) },
    getJSON: async (url) => { urls.push(url); return body(); },
    wall: () => NOW, t, watch, atLoad: false, status, logOpen, idle: { get: () => idle },
    setInterval: time.setInterval, setTimeout: time.setTimeout,
  });
  return env;
}

test('off: no request, no element, no grant panel with the board closed (60 s)', SKIP, async () => {
  const env = await setup({ watch: false, body: () => answer(grant('running')) });
  await env.time.advance(60000);
  assert.equal(env.urls.length, 0);
  assert.equal(env.board.visible, false);
  assert.equal(env.document.getElementById('board-body').children.length, 0);
  // the Activity button: reads while shown, stops after the close
  env.document.getElementById('btn-activity').click();
  await env.time.advance(10);
  assert.ok(env.urls.length >= 1);
  assert.equal(env.board.showingActivity, true);
  env.board.close();
  await env.time.advance(2000);
  const n = env.urls.length;
  await env.time.advance(60000);
  assert.equal(env.urls.length, n, 'closed: the poller stopped');
});

test('on: an iteration starts -> the board comes down with the panel, no focus, no pin', SKIP, async () => {
  let run = 'idle';
  const env = await setup({ watch: true, body: () => answer(grant(run)) });
  const composer = env.document.getElementById('composer');
  composer.focus();
  await env.time.advance(10);
  assert.equal(env.urls.length, 1, 'the watch reads at once');
  assert.equal(env.board.visible, false, 'idle: nothing comes down');
  run = 'running';
  await env.time.advance(20000);
  assert.equal(env.board.showingActivity, true);
  assert.equal(env.board.pinned, false);
  assert.equal(env.document.activeElement, composer, 'focus is not stolen');
  assert.ok(env.document.querySelector('#board-body .act-grant'));
  assert.equal(env.document.getElementById('btn-activity').getAttribute('aria-pressed'), 'true');
  // Esc / close: stays up for the same iteration
  env.board.close();
  await env.time.advance(30000);
  assert.equal(env.board.open, false, 'the same iteration does not lower it again');
});

test('on: a busy read in the middle of an iteration is not a new start (rolled up, stays up)', SKIP, async () => {
  let g = grant('idle');
  const env = await setup({ watch: true, body: () => answer(g) });
  await env.time.advance(10);
  g = grant('running');
  await env.time.advance(20000);
  assert.equal(env.board.showingActivity, true);
  env.board.close();
  env.board.tick(1);
  g = { error: 'grant_busy' };
  await env.time.advance(5000);
  g = grant('running');
  await env.time.advance(10000);
  assert.equal(env.board.visible, false, 'the same iteration, after a busy read: not lowered again');
  const n = env.urls.length;
  g = { error: 'grant_busy' };
  await env.time.advance(12000);
  assert.ok(env.urls.length - n >= 3, 'the busy cadence holds through a busy read');
});

test('on: a reply takes the board from the panel; a pinned board is never taken', SKIP, async () => {
  const { classify } = await load('markdown.js');
  const text = '# Heading\n\n' + 'a'.repeat(200);
  let run = 'idle';
  const env = await setup({ watch: true, body: () => answer(grant(run)) });
  await env.time.advance(10);
  run = 'running';
  await env.time.advance(20000);
  assert.equal(env.board.showingActivity, true);
  assert.equal(env.board.show({ key: 'r1', text, c: classify(text) }), 'replace', 'her reply takes the board');
  await env.time.advance(30000);
  assert.equal(env.board.mode, 'reply', 'the running iteration does not take it back');
  // a reply is up (not pinned) when the next iteration starts: it is not taken
  run = 'idle';
  await env.time.advance(30000);
  run = 'running';
  await env.time.advance(30000);
  assert.equal(env.board.mode, 'reply', 'a reply on the board is never taken by the watch');
  assert.equal(env.board.currentKey, 'r1');
  // pinned and rolled up; the next iteration starts: not lowered, her next reply is shown
  env.document.getElementById('board-pin').click();
  env.board.close();
  env.board.tick(1);
  run = 'idle';
  await env.time.advance(30000);
  run = 'running';
  await env.time.advance(30000);
  assert.equal(env.board.visible, false, 'a pinned board is never lowered by the watch');
  assert.equal(env.board.show({ key: 'r2', text, c: classify(text) }), 'open');
});

test('on: a hidden tab reads nothing; the person\'s turn or the open log keep the board closed', SKIP, async () => {
  let run = 'idle';
  let turnOpen = false;
  let log = false;
  const env = await setup({ watch: true, body: () => answer(grant(run)), status: () => ({ turnOpen }), logOpen: () => log });
  await env.time.advance(10);
  env.setHidden(true);
  const n = env.urls.length;
  run = 'running';
  await env.time.advance(120000);
  assert.equal(env.urls.length, n, 'hidden: no request');
  assert.equal(env.board.visible, false);
  turnOpen = true;
  env.setHidden(false);
  await env.time.advance(10);
  assert.ok(env.urls.length > n, 'shown again: one read at once');
  assert.equal(env.board.visible, false, 'her turn is open: not lowered');
  run = 'idle';
  turnOpen = false;
  await env.time.advance(5000);
  log = true;
  run = 'running';
  await env.time.advance(5000);
  assert.equal(env.board.visible, false, 'the log is open: not lowered');
});

test('on: the closed-board cadence — idle widens to 20 s, 4 s while an iteration runs', SKIP, async () => {
  let run = 'idle';
  const turnOpen = true; // keeps attention off, so the board stays closed
  const env = await setup({ watch: true, body: () => answer(grant(run)), status: () => ({ turnOpen }) });
  await env.time.advance(10);
  let n = env.urls.length;
  await env.time.advance(200000);
  const idleReads = env.urls.length - n;
  assert.ok(idleReads <= 200 / 20 + 4 && idleReads >= 200 / 20 - 1, `idle: ${idleReads} reads in 200 s`);
  run = 'running';
  await env.time.advance(25000); // one widened wait at most, then the busy cadence
  n = env.urls.length;
  await env.time.advance(40000);
  assert.equal(env.urls.length - n, 10, 'running: every 4 s');
  assert.equal(env.board.visible, false);
});

test('without the watch the board-closed controller never reads (main.js passes watch only for data-grants="exec")', SKIP, async () => {
  const src = fs.readFileSync(path.join(STAGE, 'main.js'), 'utf8');
  assert.match(src, /watch: Boolean\(appEl && appEl\.dataset\.grants === 'exec'\)/);
  assert.match(src, /logOpen: \(\) => Boolean\(talk && talk\.logOpen\)/);
  assert.match(src, /status: \(\) => \(liveCtl \? liveCtl\.status\(\) : null\)/);
});

test('the controller returns nothing to stop: it lives as long as the page', SKIP, async () => {
  const src = fs.readFileSync(path.join(STAGE, 'activity.js'), 'utf8');
  const body = src.slice(src.indexOf('export function startActivityController'), src.indexOf('// --- DOM'));
  assert.equal(/clearInterval|return \(\) =>/.test(body), false);
  const env = await setup({ watch: false });
  assert.equal(env.started, undefined);
});

test('off: the page loads the module graph of before W5b (no extra module request)', () => {
  // main.js's own imports at 76189f7 (before W5b); activity.js imports nothing new either
  const BEFORE = ['activity.js', 'anim.js', 'bg_layers.js', 'board.js', 'catalog.js', 'char_bridge.js', 'char_rig.js', 'demo_gadgets.js', 'demo_motions.js', 'desk_seat.js', 'director.js', 'gadget_catalog.js', 'hair_layer.js', 'hair_rig.js', 'i18n.js', 'live_ui.js', 'model.js', 'motion.js', 'motion_catalog.js', 'motion_model.js', 'pacer.js', 'room.js', 'scene.js', 'still_scene.js', 'svg_safe.js', 'talk.js', 'ui.js'];
  // every module request a file can make: `import … from '…'` / `"…"`, a bare `import '…'`,
  // indented or not, `export … from '…'`, and a dynamic `import(` (comments stripped first)
  const source = (name) => fs.readFileSync(path.join(STAGE, name), 'utf8').replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '');
  const imports = (name) => [...source(name).matchAll(/^\s*(?:import|export)\s+(?:[^;'"]*?\s+from\s+)?['"]([^'"]+)['"]/gm)].map((m) => m[1].replace(/^\.\//, '')).sort();
  const dynamic = (name) => /\bimport\s*\(/.test(source(name));
  assert.deepEqual(imports('main.js'), BEFORE);
  assert.deepEqual(imports('activity.js'), []);
  for (const name of ['main.js', 'activity.js']) assert.equal(dynamic(name), false, `${name}: no dynamic import`);
  // the walker sees each form (a self-check on samples)
  const sample = (text) => [...text.matchAll(/^\s*(?:import|export)\s+(?:[^;'"]*?\s+from\s+)?['"]([^'"]+)['"]/gm)].map((m) => m[1]);
  assert.deepEqual(sample(`import "./a.js";\n  import { b } from './b.js';\nexport { c } from "./c.js";\nexport * from './d.js';\nimport e, {\n  f,\n} from './e.js';`), ['./a.js', './b.js', './c.js', './d.js', './e.js']);
  assert.equal(fs.existsSync(path.join(STAGE, 'activity_ctl.js')), false);
});

for (const [what, bad] of [['grant_busy', { kind: 'block', g: { error: 'grant_busy' } }], ['grant_unreadable', { kind: 'block', g: { error: 'grant_unreadable' } }], ['another error', { kind: 'block', g: { error: 'whatever' } }], ['a failed fetch', { kind: 'fail' }]]) {
  test(`round 3 B3 (${what}): running -> rolled up -> unreadable x5 -> running again: the board stays up`, SKIP, async () => {
    let g = grant('idle');
    let failing = false;
    const env = await setup({ watch: true, body: () => { if (failing) throw new Error('down'); return answer(g); } });
    await env.time.advance(10);
    g = grant('running');
    await env.time.advance(20000);
    assert.equal(env.board.showingActivity, true);
    env.board.close();
    env.board.tick(1);
    if (bad.kind === 'fail') failing = true;
    else g = bad.g;
    await env.time.advance(120000); // many unreadable answers (or failures, backed off)
    failing = false;
    g = grant('running');
    await env.time.advance(65000);
    assert.equal(env.board.visible, false, 'the same iteration: no fake start after the unreadable stretch');
  });

  test(`round 3 B3 (${what}): running -> unreadable -> ended -> unreadable: no fake start; a real start still lowers it`, SKIP, async () => {
    let g = grant('idle');
    let failing = false;
    const env = await setup({ watch: true, body: () => { if (failing) throw new Error('down'); return answer(g); } });
    await env.time.advance(10);
    g = grant('running');
    await env.time.advance(20000);
    env.board.close();
    env.board.tick(1);
    const unread = () => { if (bad.kind === 'fail') failing = true; else g = bad.g; };
    unread();
    await env.time.advance(30000);
    failing = false;
    g = grant('idle'); // the iteration ended
    await env.time.advance(65000);
    unread();
    await env.time.advance(30000);
    failing = false;
    g = grant('idle');
    await env.time.advance(65000);
    assert.equal(env.board.visible, false, 'nothing started: nothing comes down');
    g = grant('running'); // a real start
    await env.time.advance(25000);
    assert.equal(env.board.showingActivity, true);
  });
}
