// W5b Part B (#296): the board code health audit — node --test tests/stage_board_audit_test.cjs
// Each defect found by running the code (docs/board-audit-2026-10.md) has a test here that failed
// before its fix; the rest pins the poll / offline / backoff state machine as a table and the
// exported pure helpers that had no direct test.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { pathToFileURL } = require('node:url');

let JSDOM = null;
let axeSource = null;
try {
  ({ JSDOM } = require('jsdom'));
  axeSource = require('axe-core').source;
} catch {
  /* DOM tests skipped below */
}
const ROOT = path.join(__dirname, '..');
const STAGE = path.join(ROOT, 'dashboard', 'stage', 'js');
const load = (name) => import(pathToFileURL(path.join(STAGE, name)).href);
const Poll = require(path.join(ROOT, 'dashboard', 'static', 'js', 'poll.js'));
const CAT = {
  ja: JSON.parse(fs.readFileSync(path.join(ROOT, 'dashboard/static/i18n/catalog.ja.json'), 'utf8')),
  en: JSON.parse(fs.readFileSync(path.join(ROOT, 'dashboard/static/i18n/catalog.en.json'), 'utf8')),
};
const tr = (lang) => (key, vars = {}) => {
  const text = CAT[lang][key];
  if (text === undefined) throw new Error(`missing key ${key}`);
  return text.replace(/\{(\w+)\}/g, (m, k) => (k in vars ? String(vars[k]) : m));
};
const NOW = 1792416000;

// --- B1/B2 defects -------------------------------------------------------------------------------

test('D1: mergeItems keeps one row per id, also inside one answer and on a reset', async () => {
  const { mergeItems } = await load('activity.js');
  assert.deepEqual(mergeItems([], { items: [{ id: 1 }, { id: 1 }, { id: 2 }] }).map((i) => i.id), [1, 2]);
  assert.deepEqual(mergeItems([{ id: 1 }], { reset: true, items: [{ id: 3 }, { id: 3 }] }).map((i) => i.id), [3]);
  assert.deepEqual(mergeItems([{ id: 1 }, { id: 2 }], { items: [{ id: 2 }, { id: 3 }, { id: 3 }] }).map((i) => i.id), [1, 2, 3]);
  // the first of the duplicates wins (the server never sends two; the page stays bounded)
  assert.equal(mergeItems([], { items: [{ id: 1, what: 'a' }, { id: 1, what: 'b' }] })[0].what, 'a');
  // the cap holds after hours of answers
  let list = [];
  for (let n = 1; n <= 5000; n += 1) list = mergeItems(list, { items: [{ id: n }, { id: n }] });
  assert.equal(list.length, 20);
  assert.deepEqual([list[0].id, list[19].id], [4981, 5000]);
});

test('D2: poll.js — a stopped poller leaves no visibilitychange listener behind', () => {
  const listeners = new Set();
  const doc = { hidden: false, addEventListener: (_t, fn) => listeners.add(fn), removeEventListener: (_t, fn) => listeners.delete(fn) };
  for (let i = 0; i < 50; i += 1) {
    const p = Poll.create({ run: async () => true, base: 1000, doc, setTimer: () => 1, clearTimer: () => {} });
    p.start({ wait: true });
    p.stop();
  }
  assert.equal(listeners.size, 0, 'the activity creates a poller on every open: none may leak');
  // a restarted poller listens again (once) and still polls on return
  const p = Poll.create({ run: async () => true, base: 1000, doc, setTimer: () => 1, clearTimer: () => {} });
  p.start({ wait: true });
  p.stop();
  p.start({ wait: true });
  p.start({ wait: true });
  assert.equal(listeners.size, 1);
  p.stop();
  assert.equal(listeners.size, 0);
  // a doc without removeEventListener (older fakes) still works
  const bare = { hidden: false, addEventListener: () => {} };
  const q = Poll.create({ run: async () => true, base: 1000, doc: bare, setTimer: () => 1, clearTimer: () => {} });
  q.start({ wait: true });
  q.stop();
});

test('D3: a server restart (another started_at) reads the list from scratch, not a diff of another store', async () => {
  const { ActivityFeed } = await load('activity.js');
  const urls = [];
  const answers = {
    0: [
      { server_time: 1, started_at: 100, cursor: 5, reset: true, items: [{ id: 4, kind: 'turn', what: 'started' }, { id: 5, kind: 'turn', what: 'completed' }], state: { code: 'idle' } },
      { server_time: 3, started_at: 200, cursor: 9, reset: true, items: [{ id: 8, kind: 'turn', what: 'started' }, { id: 9, kind: 'tool', what: 'done', name: 'x' }], state: { code: 'idle' } },
    ],
    5: [{ server_time: 2, started_at: 200, cursor: 9, reset: false, items: [{ id: 9, kind: 'tool', what: 'done', name: 'x' }], state: { code: 'idle' } }],
  };
  const feed = new ActivityFeed({
    getJSON: async (u) => {
      urls.push(u);
      const cursor = Number(new URL(u, 'http://x').searchParams.get('after_id'));
      return answers[cursor].shift();
    },
    wall: () => 1,
  });
  assert.equal(await feed.poll(), true);
  assert.equal(await feed.poll(), true);
  assert.deepEqual(urls, ['/api/activity?after_id=0', '/api/activity?after_id=5', '/api/activity?after_id=0']);
  assert.deepEqual(feed.items.map((i) => i.id), [8, 9], 'the new server\'s rows only');
  assert.equal(feed.cursor, 9);
  assert.equal(feed.body.started_at, 200);
  // the same server: a diff as before (no second read)
  answers[9] = [{ server_time: 4, started_at: 200, cursor: 9, reset: false, items: [], state: { code: 'idle' } }];
  assert.equal(await feed.poll(), false);
  assert.equal(urls.length, 4);
  // an answer without started_at (an older server) never triggers it
  const old = new ActivityFeed({ getJSON: async () => ({ server_time: 1, cursor: 1, items: [], state: { code: 'idle' } }), wall: () => 1 });
  await old.poll();
  await old.poll();
  assert.equal(old.cursor, 1);
});

// --- B3: the poll / offline / backoff state machine as a table -------------------------------------

test('B3: poll.js nextDelay — failures win over idleness, idle widens in steps, both capped', () => {
  const rows = [
    // [fails, idle, want ms]  base 4000, max 20000, backoffMax 60000, grace 3
    [0, 0, 4000], [0, 2, 4000], [0, 3, 8000], [0, 6, 16000], [0, 9, 20000], [0, 99, 20000],
    [1, 0, 8000], [2, 0, 16000], [3, 0, 32000], [4, 0, 60000], [9, 0, 60000], [1, 99, 8000],
  ];
  for (const [fails, idle, want] of rows) assert.equal(Poll.nextDelay({ base: 4000, max: 20000, fails, idle, backoffMax: 60000 }), want, `fails ${fails} idle ${idle}`);
});

test('B3: the activity feed — answers, failures, offline at 2, recovery, busy states', async () => {
  const { ActivityFeed } = await load('activity.js');
  let next;
  const feed = new ActivityFeed({ getJSON: async () => { if (next instanceof Error) throw next; return next; }, wall: () => NOW });
  const ok = (code = 'idle', items = []) => ({ server_time: NOW + 5, cursor: items.length ? items[items.length - 1].id : 0, items, state: { code } });
  const rows = [
    // [answer, poll result, fails, offline, busy]
    [ok(), true, 0, false, false],
    [ok(), false, 0, false, false],
    [ok('generating'), true, 0, false, true],
    [ok('generating'), true, 0, false, true], // busy: never "nothing new"
    [new Error('500'), null, 1, false, true],
    [null, null, 2, true, true],
    ['text', null, 3, true, true],
    [ok('approval'), true, 0, false, true],
    [ok('report'), true, 0, false, false],
    [ok('paused'), true, 0, false, false],
    [ok('stopped'), true, 0, false, false],
    [ok('tool', [{ id: 1, kind: 'turn', what: 'started' }]), true, 0, false, true],
  ];
  for (const [answer, result, fails, offline, busy] of rows) {
    next = answer;
    assert.equal(await feed.poll(), result, JSON.stringify(answer));
    assert.equal(feed.fails, fails);
    assert.equal(feed.offline, offline);
    assert.equal(feed.busy, busy);
  }
  assert.equal(feed.skew, 5, 'the server clock is 5 s ahead');
  assert.equal(feed.serverNow(), NOW + 5);
});

test('B3: offline shows "offline", never the old state as live; the stall only for a busy state', async () => {
  const A = await load('activity.js');
  const t = tr('en');
  assert.equal(A.stateText({ code: 'tool', tool: 'x', since: NOW - 3 }, NOW, t, 120, true).code, 'offline');
  assert.equal(A.stateText({ code: 'idle', last_event: NOW - 999 }, NOW, t).stall, false);
  assert.equal(A.stateText({ code: 'approval', last_event: NOW - 999 }, NOW, t).stall, false, 'an approval waits for a person');
  assert.equal(A.stateText({ code: 'working', last_event: NOW - 120 }, NOW, t).stall, true);
  assert.equal(A.stateText({ code: 'working', last_event: NOW + 50 }, NOW, t).stall, false, 'a clock ahead: no stall');
  assert.equal(A.stateText(null, NOW, t).code, 'idle');
  assert.equal(A.stateText({ code: '<b>' }, NOW, t).code, 'idle');
});

// --- B3: exported pure helpers without a direct test ---------------------------------------------

test('B3: activity.js elapsed / duration / clock', async () => {
  const A = await load('activity.js');
  const t = tr('en');
  assert.equal(A.elapsed(NOW - 10.9, NOW), 10);
  assert.equal(A.elapsed(NOW + 10, NOW), 0, 'clock skew: never negative');
  assert.equal(A.elapsed(null, NOW), null);
  assert.equal(A.elapsed(NOW, Number.NaN), null);
  assert.equal(A.duration(null, t), '');
  assert.equal(A.duration(undefined, t), '');
  assert.equal(A.duration(59, t), t('stage.act_sec', { n: 59 }));
  assert.equal(A.duration(60, t), t('stage.act_min_sec', { m: 1, s: 0 }));
  assert.equal(A.duration(3600, t), t('stage.act_hour_min', { h: 1, m: 0 }));
  assert.equal(A.clock(null), '--:--:--');
  assert.equal(A.clock('x'), '--:--:--');
  assert.match(A.clock(NOW), /^\d\d:\d\d:\d\d$/);
  assert.equal(A.POLL.base, 4);
  assert.equal(A.POLL.max, 20);
  assert.equal(A.POLL.backoffMax, 60);
  assert.equal(A.RECENT, 20);
});

test('B3: board.js stepProgress / easeOut / boardTitle / boardStats / finalBoardAction / boardAction tables', async () => {
  const B = await load('board.js');
  const t = tr('en');
  assert.equal(B.easeOut(-1), 0);
  assert.equal(B.easeOut(2), 1);
  assert.equal(B.stepProgress(0, 1, B.OPEN_S / 2), 0.5);
  assert.equal(B.stepProgress(0.2, 0, 10), 0);
  assert.equal(B.stepProgress(0.2, 1, -5), 0.2, 'a negative dt does not move');
  assert.equal(B.stepProgress(0.2, 1, 0.01, true), 1, 'reduced motion jumps');
  const c = { blocks: [{ t: 'p', inl: [] }], counts: { heading: 0, code: 2, table: 0, visible: 40 } };
  assert.equal(B.boardTitle(c, t), t('stage.board_title'));
  assert.equal(B.boardStats(c, t), [t('stage.board_n_code', { count: 2 }), t('stage.board_n_chars', { count: 40 })].join(' · '));
  const finals = [
    [{ finalMode: 'board', boardKey: 'k', key: 'k', open: true, pinned: false }, 'keep'],
    [{ finalMode: 'line', boardKey: 'k', key: 'k', open: false, pinned: false }, 'keep'],
    [{ finalMode: 'line', boardKey: 'other', key: 'k', open: true, pinned: false }, 'keep'],
    [{ finalMode: 'line', boardKey: 'k', key: 'k', open: true, pinned: true }, 'keep'],
    [{ finalMode: 'line', boardKey: 'k', key: 'k', open: true, pinned: false }, 'close'],
    [{ finalMode: 'line', boardKey: B.ACTIVITY_KEY, key: 'k', open: true, pinned: false }, 'keep'],
  ];
  for (const [input, want] of finals) assert.equal(B.finalBoardAction(input), want, JSON.stringify(input));
  const actions = [
    // a reply onto ...                                           -> action
    [{ pinned: false, open: false, currentKey: null, key: 'r', explicit: false }, 'open'],
    [{ pinned: false, open: true, currentKey: B.ACTIVITY_KEY, key: 'r', explicit: false }, 'replace'], // a reply takes the activity
    [{ pinned: true, open: true, currentKey: B.ACTIVITY_KEY, key: 'r', explicit: false }, 'held'], // unless pinned
    [{ pinned: true, open: true, currentKey: B.ACTIVITY_KEY, key: 'r', explicit: true }, 'replace'],
    [{ pinned: true, open: false, currentKey: 'x', key: 'r', explicit: false }, 'open'],
    [{ pinned: false, open: true, currentKey: 'r', key: 'r', explicit: false }, 'update'],
    [{ pinned: false, open: false, currentKey: 'r', key: 'r', explicit: false }, 'keep'],
    [{ pinned: false, open: false, currentKey: 'r', key: 'r', explicit: true }, 'update'],
  ];
  for (const [input, want] of actions) assert.equal(B.boardAction(input), want, JSON.stringify(input));
});

test('B3: boardGeometry — wide, narrow (front), portrait, odd inputs stay finite and inside', async () => {
  const B = await load('board.js');
  const wide = B.boardGeometry({ width: 1300, height: 800, scale: 2, viewW: 650, tbTop: 600, portrait: false });
  assert.equal(wide.front, false);
  assert.ok(wide.width >= B.MIN_BOARD_W);
  assert.equal(wide.aside, wide.shift + wide.pan);
  const narrow = B.boardGeometry({ width: 700, height: 600, scale: 2, viewW: 350, tbTop: 450, portrait: false });
  assert.equal(narrow.front, true);
  const phone = B.boardGeometry({ width: 390, height: 700, scale: 1, viewW: 390, tbTop: 400, portrait: true });
  assert.equal(phone.front, true);
  assert.equal(phone.pan, 8);
  assert.equal(phone.width, 390 - 6 - 56);
  for (const geo of [
    B.boardGeometry({ width: 100, height: 100, scale: 0, viewW: 100, tbTop: 300, portrait: true }),
    B.boardGeometry({ width: 1300, height: 800, scale: -1, viewW: 650, tbTop: 900, portrait: false }),
  ]) {
    for (const v of [geo.shift, geo.pan, geo.aside, geo.left, geo.width, geo.top, geo.bottom]) assert.ok(Number.isFinite(v));
    assert.ok(geo.bottom >= 0 && geo.width >= 160);
  }
});

// --- the controller, run: rapid open/close, pinned + activity, Esc, language, reduced motion ------

const BOARD_HTML = `<!doctype html><html lang="ja"><head><title>Stage</title></head><body><main>
  <section id="board" hidden aria-label="Board"><div id="board-head"><h2><span id="board-title-text"></span></h2>
  <button id="board-pin" type="button"></button><button id="board-copy" type="button">Copy</button><button id="board-close" type="button">Close</button></div>
  <div id="board-body" tabindex="0"></div><div><span id="board-stats"></span><span id="board-note"></span></div></section></main></body></html>`;

test('the controller: rapid open/close, Esc, pinned activity holds a reply, language switch, reduced motion', { skip: JSDOM ? false : 'jsdom not installed' }, async () => {
  const dom = new JSDOM(BOARD_HTML);
  const { document } = dom.window;
  global.HTMLElement = dom.window.HTMLElement;
  const A = await load('activity.js');
  const B = await load('board.js');
  let lang = 'ja';
  let reduced = false;
  let closed = 0;
  const board = B.createBoard({ root: document.getElementById('board'), t: (k, v) => tr(lang)(k, v), reduced: () => reduced, copy: async () => true, openLink: () => {}, onClose: () => { closed += 1; } });
  const nodes = () => A.renderActivity(document, A.activityModel({ body: { server_time: NOW, state: { code: 'idle' }, items: [] }, items: [] }, NOW, tr(lang)), tr(lang));
  // 200 rapid open/close pairs with a few frames between: the state settles, one body child set
  for (let i = 0; i < 200; i += 1) {
    board.showActivity(nodes());
    board.tick(0.05);
    board.close();
    board.tick(0.01);
  }
  for (let f = 0; f < 40; f += 1) board.tick(0.05);
  assert.equal(board.visible, false);
  assert.equal(document.getElementById('board').hidden, true);
  assert.equal(closed, 200);
  assert.equal(document.getElementById('board-body').querySelectorAll('.act-board').length, 1, 'no pile-up of bodies');
  // reduced motion: open and close are a single frame
  reduced = true;
  board.showActivity(nodes());
  assert.equal(board.tick(0.001), 1);
  // Esc on the board rolls it up (and stops there)
  let bubbled = false;
  document.body.addEventListener('keydown', () => { bubbled = true; });
  document.getElementById('board').dispatchEvent(new dom.window.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
  assert.equal(board.open, false);
  assert.equal(bubbled, false);
  assert.equal(board.tick(0.001), 0);
  // a pinned activity holds the next reply (it waits in the log), explicit takes it
  board.showActivity(nodes(), { explicit: true });
  document.getElementById('board-pin').click();
  const { classify } = await load('markdown.js');
  const text = '# 見出し\n\n' + 'あ'.repeat(200);
  assert.equal(board.show({ key: 'r1', text, c: classify(text) }), 'held');
  assert.equal(board.showingActivity, true);
  assert.equal(board.show({ key: 'r1', text, c: classify(text) }, { explicit: true }), 'replace');
  // a pinned reply holds the activity unless asked
  assert.equal(board.showActivity(nodes()), 'held');
  assert.equal(board.mode, 'reply');
  // language switch: the pin label and the title follow
  lang = 'en';
  board.relabel();
  assert.equal(document.getElementById('board-pin').textContent, tr('en')('stage.board_pinned'));
  board.showActivity(nodes(), { explicit: true });
  lang = 'ja';
  board.relabel();
  assert.equal(document.getElementById('board-title-text').textContent, tr('ja')('stage.act_title'));
});

test('D4: the board\'s note follows a language switch (held / copied)', { skip: JSDOM ? false : 'jsdom not installed' }, async () => {
  const dom = new JSDOM(BOARD_HTML);
  const { document } = dom.window;
  global.HTMLElement = dom.window.HTMLElement;
  const B = await load('board.js');
  const { classify } = await load('markdown.js');
  let lang = 'ja';
  const board = B.createBoard({ root: document.getElementById('board'), t: (k, v) => tr(lang)(k, v), reduced: () => true, copy: async () => true, openLink: () => {} });
  const text = '# 見出し\n\n' + 'あ'.repeat(200);
  board.show({ key: 'r1', text, c: classify(text) });
  document.getElementById('board-pin').click();
  assert.equal(board.show({ key: 'r2', text, c: classify(text) }), 'held');
  const note = document.getElementById('board-note');
  assert.equal(note.textContent, tr('ja')('stage.board_held'));
  lang = 'en';
  board.relabel();
  assert.equal(note.textContent, tr('en')('stage.board_held'));
  document.getElementById('board-copy').click();
  await new Promise((r) => setTimeout(r, 0));
  assert.equal(note.textContent, tr('en')('stage.board_copied'));
  lang = 'ja';
  board.relabel();
  assert.equal(note.textContent, tr('ja')('stage.board_copied'));
  board.close();
  board.relabel();
  assert.equal(note.textContent, '');
});

test('axe: the activity board with the grant panel has no violations (contrast: board tokens, docs)', { skip: JSDOM && axeSource ? false : 'jsdom/axe-core not installed' }, async () => {
  const dom = new JSDOM(BOARD_HTML, { runScripts: 'outside-only', pretendToBeVisual: true });
  const { window } = dom;
  const { document } = window;
  global.HTMLElement = window.HTMLElement;
  const A = await load('activity.js');
  const B = await load('board.js');
  for (const lang of ['ja', 'en']) {
    const t = tr(lang);
    const board = B.createBoard({ root: document.getElementById('board'), t, reduced: () => true, copy: async () => true, openLink: () => {}, idle: { get: () => false, set: () => {} } });
    const grant = {
      id: 'abababab', state: 'active', live: true, headline: { subject: 'grant', state: 'active' }, goal_state: 'awaiting_verification',
      iterations: 2, max_iterations: 10, ttl_sec: 7200, ttl_left_sec: 3600, budget_total: 1000, budget_left: 750, budget_per_tick: 100,
      awaiting_verification: true, cards: 1, card: true, iteration: { run: 'running', number: 3, tick_used: 12 },
      outcomes: [{ what: 'done', number: 2, duration_ms: 1000, ts: NOW, tools: ['editor'] }],
    };
    const m = A.activityModel({ body: { server_time: NOW, state: { code: 'idle' }, items: [], grant, totals: {} }, items: [] }, NOW, t);
    board.showActivity(A.renderActivity(document, m, t), { explicit: true, foot: 'f', announce: m.grant.announce });
    window.eval(axeSource);
    const result = await window.axe.run(document, { rules: { 'color-contrast': { enabled: false } } });
    const found = Array.from(result.violations, (v) => `${v.id}: ${Array.from(v.nodes, (n) => String(n.target)).join(' ')}`);
    assert.equal(found.join('\n'), '', lang);
    // keyboard: the panel's links are reachable (native anchors with href, no negative tabindex)
    const links = [...document.querySelectorAll('.act-grant a')];
    assert.ok(links.length >= 1);
    for (const a of links) {
      assert.ok(a.getAttribute('href'));
      assert.equal(a.getAttribute('tabindex'), null);
    }
  }
});
