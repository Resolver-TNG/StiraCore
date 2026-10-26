// #446: the board's activity mode — node --test tests/stage_activity_test.cjs
// Pure parts (rows, state text, stall, YOLO, totals, merge, the feed's cursor and backoff
// result) run everywhere; the board DOM part needs jsdom (npm install --no-save jsdom axe-core).
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

let JSDOM = null;
try {
  ({ JSDOM } = require('jsdom'));
} catch {
  /* DOM tests skipped below */
}
const ROOT = path.join(__dirname, '..');
const STAGE = path.join(ROOT, 'dashboard', 'stage', 'js');
const load = (name) => import(path.join(STAGE, name));
const CAT = {
  ja: JSON.parse(fs.readFileSync(path.join(ROOT, 'dashboard/static/i18n/catalog.ja.json'), 'utf8')),
  en: JSON.parse(fs.readFileSync(path.join(ROOT, 'dashboard/static/i18n/catalog.en.json'), 'utf8')),
};
const tr = (lang) => (key, vars = {}) => {
  const text = CAT[lang][key];
  if (text === undefined) throw new Error(`missing key ${key}`);
  return text.replace(/\{(\w+)\}/g, (m, k) => (k in vars ? String(vars[k]) : m));
};
const SECRET = 'AKIAABCDEFGHIJKLMNOP';

// Fake answers of GET /api/activity (the shape of observability/activity.py).
const NOW = 1792416000;
const yoloWindow = { state: 'active', tier: 'internal', remaining_sec: 21000, used: 3, limit: 50, external_used: 0, ends_at: NOW + 21000 };
const items = [
  { id: 3, ts: NOW - 300, turn: 't1', kind: 'turn', what: 'started' },
  { id: 5, ts: NOW - 290, turn: 't1', kind: 'tool', what: 'done', name: 'file_write', ok: true, duration_ms: 420, artifacts: 2 },
  { id: 6, ts: NOW - 280, turn: 't1', kind: 'tool', what: 'error', name: 'web_fetch', ok: false, duration_ms: 3100 },
  { id: 8, ts: NOW - 200, turn: 't1', kind: 'model', what: 'success', ok: true, duration_ms: 61000 },
  { id: 9, ts: NOW - 100, turn: 't1', kind: 'turn', what: 'completed', ok: true, stop: 'completed', artifacts: 2 },
];
const answer = (over = {}) => ({
  server_time: NOW, cursor: 9, reset: true, items, stall_sec: 120,
  state: { code: 'idle', since: null, last_event: null, running: false },
  yolo: yoloWindow,
  totals: { turns: 1, input: 1200, output: 340, cache_read: 900, cache_write: 0, context_tokens: 4321, context_window: 200000, cost: null },
  ...over,
});

test('a YOLO window with no turn: waiting, the window, the recent rows (newest first)', async () => {
  const A = await load('activity.js');
  for (const lang of ['ja', 'en']) {
    const t = tr(lang);
    const m = A.activityModel({ body: answer(), items }, NOW, t);
    assert.equal(m.state.code, 'idle');
    assert.equal(m.state.text, t('stage.act_state_idle'));
    assert.equal(m.state.stall, false);
    assert.equal(m.rows.length, 5);
    assert.equal(m.rows[0].text, t('stage.act_row_turn_done', { stop: '' }).trim());
    assert.equal(m.rows[3].href, '/artifacts?turn=t1', 'a written file links to the turn artifacts');
    assert.equal(m.rows[0].href, '/artifacts?turn=t1', 'the turn end links to its artifacts (#535)');
    assert.equal(m.rows[0].artifacts, 2);
    assert.equal(m.rows[2].ok, false);
    assert.equal(m.rows[4].href, null);
    assert.deepEqual(m.yolo, [
      t('stage.act_yolo_state', { state: 'active', tier: 'internal' }),
      t('stage.act_yolo_left', { time: t('stage.act_hour_min', { h: 5, m: 50 }) }),
      t('stage.act_yolo_auto', { n: 3, limit: 50 }),
    ]);
    assert.ok(m.totals.some((line) => line.includes('4,321') && line.includes('2%')));
    assert.equal(m.totals[m.totals.length - 1], t('stage.act_totals_cost_unknown'));
  }
});

test('a long model call: generating with seconds since its start, then the stall hint', async () => {
  const A = await load('activity.js');
  const t = tr('ja');
  const state = { code: 'generating', since: NOW - 75, last_event: NOW - 75, running: true };
  let m = A.activityModel({ body: answer({ state }), items }, NOW, t);
  assert.equal(m.state.text, t('stage.act_state_generating', { time: t('stage.act_min_sec', { m: 1, s: 15 }) }));
  assert.equal(m.state.stall, false);
  m = A.activityModel({ body: answer({ state }), items }, NOW + 60, t);
  assert.equal(m.state.stall, true, 'past 120 s without an event');
  assert.equal(m.state.stallText, t('stage.act_stall', { time: t('stage.act_min_sec', { m: 2, s: 15 }) }));
  // the server's threshold wins
  m = A.activityModel({ body: answer({ state, stall_sec: 600 }), items }, NOW + 60, t);
  assert.equal(m.state.stall, false);
});

test('a running tool and an approval wait (tool name only), paused, stopped, offline', async () => {
  const A = await load('activity.js');
  const t = tr('en');
  const s = (state) => A.stateText(state, NOW, t);
  assert.equal(s({ code: 'tool', tool: 'web_fetch', since: NOW - 7, last_event: NOW - 7 }).text, 'Running a tool: web_fetch (7 s)');
  const wait = s({ code: 'approval', tool: 'shell', waiting: 2, since: NOW - 500, last_event: NOW - 500 });
  assert.equal(wait.text, 'Waiting for approval: shell (2)');
  assert.equal(wait.stall, false, 'an approval waits for a person, not for the model');
  assert.equal(s({ code: 'paused', reason: 'breaker' }).text, 'Paused (breaker)');
  assert.equal(s({ code: 'stopped', reason: 'latched' }).text, 'Stopped (YOLO latch)');
  assert.equal(s({ code: 'tool', tool: `x; ${SECRET}` }).text.includes(SECRET), false, 'not a code: "other"');
  assert.equal(A.stateText({ code: 'idle' }, NOW, t, 120, true).code, 'offline');
  const paused = A.yoloView({ state: 'paused', pause_reason: 'breaker', pause_breaker_reasons: ['vigilance_high'], used: 1, limit: 5 }, t);
  assert.ok(paused.includes('Paused because: breaker vigilance_high'));
});

test('a pending progress report (ADR 0014): the state and the rows name the trigger', async () => {
  const A = await load('activity.js');
  const t = tr('ja');
  const s = A.stateText({ code: 'report', trigger: 'count', count: 'tools', since: NOW - 30 }, NOW, t);
  assert.equal(s.text, t('stage.act_state_report', { trigger: t('stage.act_trigger_tools'), time: t('stage.act_sec', { n: 30 }) }));
  assert.equal(s.stall, false, 'a report waits for a person');
  const row = A.rowView({ id: 1, ts: NOW, kind: 'progress', what: 'report', trigger: 'time' }, t);
  assert.equal(row.text, t('stage.act_row_report', { trigger: t('stage.act_trigger_time') }));
  assert.equal(A.rowView({ id: 2, ts: NOW, kind: 'progress', what: 'continue' }, t).text, t('stage.act_row_reply', { answer: 'continue' }));
});

test('merge: the first read replaces, a diff appends once by id, at most 20', async () => {
  const A = await load('activity.js');
  let list = A.mergeItems([], { reset: true, items });
  assert.deepEqual(list.map((i) => i.id), [3, 5, 6, 8, 9]);
  list = A.mergeItems(list, { reset: false, items: [items[4], { id: 12, kind: 'turn', what: 'started' }] });
  assert.deepEqual(list.map((i) => i.id), [3, 5, 6, 8, 9, 12]);
  const many = Array.from({ length: 30 }, (_, n) => ({ id: 100 + n, kind: 'model', what: 'success' }));
  list = A.mergeItems(list, { reset: false, items: many });
  assert.equal(list.length, 20);
  assert.equal(list[19].id, 129);
  assert.deepEqual(A.mergeItems(list, { reset: true, items: [] }), [], 'a reset store empties it');
});

test('the feed asks for rows after its cursor, keeps the server clock, reports failures', async () => {
  const A = await load('activity.js');
  const urls = [];
  const answers = [answer(), answer({ reset: false, items: [], cursor: 9, server_time: NOW + 4 })];
  let fail = false;
  const feed = new A.ActivityFeed({
    getJSON: async (url) => {
      urls.push(url);
      if (fail) throw new Error('500');
      return answers.shift();
    },
    wall: () => NOW - 10,
  });
  assert.equal(await feed.poll(), true);
  assert.equal(feed.skew, 10);
  assert.equal(await feed.poll(), false, 'nothing new, nothing running: the poll widens');
  assert.deepEqual(urls, ['/api/activity?after_id=0', '/api/activity?after_id=9']);
  fail = true;
  assert.equal(await feed.poll(), null);
  assert.equal(await feed.poll(), null);
  assert.equal(feed.offline, true);
  assert.equal(feed.items.length, 5, 'a failure keeps the list');
});

test('busy keeps the short interval (poll.js hold)', async () => {
  const A = await load('activity.js');
  const feed = new A.ActivityFeed({ getJSON: async () => answer({ state: { code: 'tool', tool: 'x', since: NOW } }), wall: () => NOW });
  await feed.poll();
  assert.equal(feed.busy, true);
  assert.equal(await feed.poll(), true, 'a running tool counts as a change');
});

test('poll budget: idle answers widen the 4 s interval to 20 s (at most ~4 calls a minute)', () => {
  const P = require('../dashboard/static/js/poll.js');
  const delays = [];
  for (let idle = 0; idle < 30; idle += 1) delays.push(P.nextDelay({ base: 4000, max: 20000, idle }));
  assert.equal(delays[0], 4000);
  assert.equal(delays[29], 20000);
  let t = 0;
  let calls = 0;
  for (const d of delays) {
    t += d;
    if (t > 120000 && t <= 180000) calls += 1;
  }
  assert.ok(calls <= 4, `calls in the third minute: ${calls}`);
});

test('rows never hold anything but codes, counts and names', async () => {
  const A = await load('activity.js');
  const t = tr('ja');
  const hostile = [
    { id: 1, ts: NOW, kind: 'tool', what: 'done', ok: true, name: `<img src=x onerror=alert(1)>${SECRET}`, input_preview: SECRET },
    { id: 2, ts: NOW, kind: 'approval', what: 'requested', name: 'shell', reason: SECRET, command: 'rm -rf /' },
    { id: 3, ts: NOW, kind: 'turn', what: 'completed', ok: false, stop: '<b>x</b>' },
  ];
  const rows = hostile.map((i) => A.rowView(i, t));
  const text = JSON.stringify(rows);
  assert.equal(text.includes(SECRET), false);
  assert.equal(text.includes('<img'), false);
  assert.equal(text.includes('rm -rf'), false);
  assert.equal(rows[0].text, t('stage.act_row_tool_done', { tool: 'other', time: '' }));
});

test('the board: activity mode in place, a reply takes it over, the setting', { skip: JSDOM ? false : 'jsdom not installed' }, async () => {
  const dom = new JSDOM(`<!doctype html><section id="board" hidden><div id="board-head"><h2><span id="board-title-text"></span></h2>
    <button id="board-pin"></button><button id="board-copy"></button><button id="board-close"></button></div>
    <div id="board-body" tabindex="0"></div><div><span id="board-stats"></span><span id="board-note"></span></div></section>`);
  const { document } = dom.window;
  global.HTMLElement = dom.window.HTMLElement;
  const B = await load('board.js');
  const A = await load('activity.js');
  const t = tr('ja');
  let stored = false;
  const board = B.createBoard({ root: document.getElementById('board'), t, reduced: () => true, copy: async () => true, openLink: () => {}, idle: { get: () => stored, set: (v) => { stored = v; } } });
  const nodes = (body) => A.renderActivity(document, A.activityModel({ body, items: body.items }, NOW, t), t);
  assert.equal(board.showActivity(nodes(answer()), { foot: 'f' }), 'open');
  assert.equal(board.showingActivity, true);
  assert.equal(document.getElementById('board-title-text').textContent, t('stage.act_title'));
  assert.equal(document.getElementById('board-copy').hidden, true, 'nothing to copy');
  const body = document.getElementById('board-body');
  assert.equal(body.querySelectorAll('.act-row').length, 5);
  assert.match(body.querySelector('.act-link').getAttribute('href'), /^\/artifacts\?turn=t1$/);
  assert.equal(body.querySelector('.act-link').textContent, t('stage.act_artifacts_n', { n: 2 }));
  const box = document.getElementById('board-idle-activity');
  box.checked = true;
  box.dispatchEvent(new dom.window.Event('change'));
  assert.equal(stored, true);
  // a refresh keeps the setting element (focus is not lost) and updates the state
  const state = { code: 'tool', tool: 'file_write', since: NOW - 3, last_event: NOW - 3 };
  assert.equal(board.updateActivity(nodes(answer({ state })), 'g'), true);
  assert.equal(document.getElementById('board-idle-activity'), box);
  assert.equal(body.querySelector('.act-state').textContent, t('stage.act_state_tool', { tool: 'file_write', time: t('stage.act_sec', { n: 3 }) }));
  assert.equal(body.textContent.includes(SECRET), false);
  // a board reply replaces the activity; the activity no longer updates
  const { classify } = await load('markdown.js');
  const text = '# 見出し\n\n' + 'あ'.repeat(200);
  assert.equal(board.show({ key: 'r1', text, c: classify(text) }), 'replace');
  assert.equal(board.mode, 'reply');
  assert.equal(board.showingActivity, false);
  assert.equal(document.getElementById('board-copy').hidden, false);
  assert.equal(board.updateActivity(nodes(answer()), ''), false);
  assert.equal(body.querySelector('.act-row'), null);
  // a pinned reply holds the board unless the person asks
  document.getElementById('board-pin').click();
  assert.equal(board.showActivity(nodes(answer())), 'held');
  assert.equal(board.showActivity(nodes(answer()), { explicit: true }), 'open');
  assert.equal(board.showingActivity, true);
});

test('#451: the cost line — no price, a configured price, a partial sum', async () => {
  const A = await load('activity.js');
  for (const lang of ['ja', 'en']) {
    const t = tr(lang);
    const base = answer().totals;
    const last = (totals) => { const lines = A.totalsView(totals, t); return lines[lines.length - 1]; };
    assert.equal(last({ ...base, cost: null }), t('stage.act_totals_cost_unknown'));
    assert.equal(last({ ...base, cost: 0.012345 }), t('stage.act_totals_cost', { cost: '0.0123' }));
    assert.equal(last({ ...base, cost: 0 }), t('stage.act_totals_cost', { cost: '0.0000' }));
    const partial = last({ ...base, cost: 0.5, cost_partial: true });
    assert.equal(partial, t('stage.act_totals_cost_partial', { cost: '0.5000' }));
    assert.notEqual(partial, t('stage.act_totals_cost', { cost: '0.5000' }));
    // a cost_partial without a number never shows a price
    assert.equal(last({ ...base, cost: 'x', cost_partial: true }), t('stage.act_totals_cost_unknown'));
  }
});

test('#535: no count, no link; the link is a same-origin path with an opaque id only', async () => {
  const A = await load('activity.js');
  const t = tr('en');
  const end = { id: 9, ts: NOW, turn: 't1', kind: 'turn', what: 'completed', ok: true };
  assert.equal(A.rowView(end, t).href, null, 'a turn without artifacts has no link');
  assert.equal(A.rowView({ ...end, artifacts: 0 }, t).href, null);
  assert.equal(A.rowView({ ...end, artifacts: 3, turn: 'http://evil/x' }, t).href, null, 'not a code: no link');
  assert.equal(A.rowView({ ...end, kind: 'model', what: 'success', artifacts: 3 }, t).href, null);
  assert.equal(A.rowView({ ...end, kind: 'tool', what: 'error', name: 'file_write', ok: false, artifacts: 3 }, t).href, null);
  const row = A.rowView({ ...end, artifacts: 3 }, t);
  assert.equal(row.href, '/artifacts?turn=t1');
  assert.equal(t('stage.act_artifacts_n', { n: row.artifacts }), '3 artifacts');
});

test('#545: a declared checkpoint is 「区切り」 / Checkpoint, a checkpoint-cancelled call stays not run', async () => {
  const A = await load('activity.js');
  const declared = { id: 10, ts: NOW, kind: 'tool', what: 'declared', name: 'handoff_to_user' };
  const cancelled = { id: 11, ts: NOW, kind: 'tool', what: 'denied', ok: false, name: 'file_write' };
  const ja = A.rowView(declared, tr('ja'));
  assert.equal(ja.text, '区切り');
  assert.equal(ja.ok, null, 'not counted as a failure (no ✗)');
  assert.equal(A.rowView(declared, tr('en')).text, 'Checkpoint');
  assert.equal(A.rowView(cancelled, tr('ja')).text, 'file_write 実行されず');
  assert.equal(A.rowView(cancelled, tr('en')).text, 'file_write not run');
  assert.equal(A.rowView(cancelled, tr('ja')).ok, false);
});
