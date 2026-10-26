// W5b (#296): the goal grant on the Stage board — node --test tests/stage_board_grant_test.cjs
// grantView / outcome rows / the A4 attention table / the A5 watch result / the feed's busy flag
// run everywhere; the panel, the live region and the off-parity of the DOM need jsdom
// (npm install --no-save jsdom axe-core).
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
const MARKER = 'w5bMarker-7c41e09d2b8a5f36';
const NOW = 1792416000;
const grant = (over = {}) => ({
  id: 'abababab', state: 'active', live: true, headline: { subject: 'grant', state: 'active' }, goal_state: 'active',
  iterations: 2, max_iterations: 10, ttl_sec: 7200, ttl_left_sec: 3600, budget_total: 1000, budget_left: 750, budget_per_tick: 100,
  awaiting_verification: false, cards: 0, card: false,
  iteration: { run: 'idle', number: 2, end: 'done', tick_used: 60 },
  outcomes: [
    { what: 'done', number: 1, duration_ms: 3000, tick_used: 40, ts: NOW - 100, tools: ['file_read'] },
    { what: 'failed', number: 2, duration_ms: 61000, tick_used: 60, ts: NOW - 50, tools: ['editor', 'file_read'], detail: 'child_error' },
  ],
  ...over,
});
const answer = (over = {}) => ({
  server_time: NOW, cursor: 9, reset: true, items: [], stall_sec: 120,
  state: { code: 'idle', since: null, last_event: null, running: false },
  yolo: null,
  totals: { turns: 1, input: 1, output: 2, cache_read: 0, cache_write: 0, context_tokens: null, cost: null },
  ...over,
});

// --- grantView ---------------------------------------------------------------------------------

test('no grant key, null: nothing; an error or offline: "unknown", never old numbers', async () => {
  const A = await load('activity.js');
  const t = tr('ja');
  assert.equal(A.grantView(undefined, NOW, NOW, t), null);
  assert.equal(A.grantView(null, NOW, NOW, t), null);
  assert.equal(A.grantView(null, NOW, NOW, t, true), null, 'offline without a grant: still nothing');
  for (const busy of ['grant_busy', 'grant_unreadable']) assert.equal(A.grantView({ error: busy }, NOW, NOW, t).unknown, true, busy);
  const err = A.grantView({ error: 'grant_unreadable' }, NOW, NOW, t);
  assert.equal(err.unknown, true);
  assert.equal(err.headline, t('stage.grant_unknown'));
  assert.equal(err.ttl, null);
  assert.equal(err.budget, null);
  const off = A.grantView(grant(), NOW, NOW, t, true);
  assert.equal(off.unknown, true);
  assert.equal(off.headline, t('stage.grant_unknown_offline'));
  assert.deepEqual([off.lines, off.ttl, off.budget, off.cue, off.outcomes], [[], null, null, null, []]);
  assert.equal(A.grantView('x', NOW, NOW, t).unknown, true);
});

test('a live grant: headline, goal, iterations, TTL counting down from the server time, budget', async () => {
  const A = await load('activity.js');
  for (const lang of ['ja', 'en']) {
    const t = tr(lang);
    const g = A.grantView(grant(), NOW + 65, NOW, t);
    assert.equal(g.title, `${t('stage.grant_h')} abababab`);
    assert.equal(g.headline, t('stage.grant_state_active'));
    assert.equal(g.code, 'active');
    assert.deepEqual(g.lines, [
      t('stage.grant_goal', { state: t('stage.grant_goal_active') }),
      `${t('stage.grant_iterations', { n: 2, max: 10 })} · ${t('stage.grant_run_idle')}`,
      t('stage.grant_last_stop', { reason: t('stage.grant_end_done') }),
    ]);
    assert.equal(g.ttl.left, 3600 - 65, 'the TTL ticks between reads');
    assert.equal(g.ttl.total, 7200);
    assert.equal(g.ttl.text, t('stage.grant_ttl', { time: t('stage.act_min_sec', { m: 58, s: 55 }) }));
    assert.deepEqual([g.budget.left, g.budget.total], [750, 1000]);
    assert.equal(g.budget.text, t('stage.grant_budget', { n: '750', total: '1,000' }));
    assert.equal(g.budget.tick, t('stage.grant_budget_tick', { n: '60', total: '100' }));
    assert.equal(g.cue, null);
    assert.equal(g.announce, t('stage.grant_state_active'));
    assert.equal(g.outcomes.length, 2);
    assert.equal(g.outcomes[0].ok, false, 'newest first');
    assert.equal(g.outcomes[0].text, `#2 · ${t('stage.grant_end_failed')} (child_error) · ${t('stage.act_min_sec', { m: 1, s: 1 })} · editor, file_read`);
    assert.equal(g.outcomes[1].ok, true);
  }
  const t = tr('en');
  assert.equal(A.grantView(grant(), NOW + 99999, NOW, t).ttl.left, 0, 'never below zero');
  assert.equal(A.grantView(grant(), NOW - 50, NOW, t).ttl.left, 3600, 'a clock behind the answer: no time added');
});

test('running: the busy code, no last end, the per-tick tokens, announced once (no TTL in it)', async () => {
  const A = await load('activity.js');
  const t = tr('en');
  const g = A.grantView(grant({ iteration: { run: 'running', number: 3, tick_used: 12 } }), NOW, NOW, t);
  assert.equal(g.code, 'running');
  assert.equal(g.lines[1], `${t('stage.grant_iterations', { n: 3, max: 10 })} · ${t('stage.grant_run_running')}`, 'the running one\'s number');
  assert.equal(g.lines.length, 2, 'no "last end" while one runs');
  assert.equal(g.announce, `${t('stage.grant_state_active')} · ${t('stage.grant_run_running')}`);
  const later = A.grantView(grant({ iteration: { run: 'running', number: 3, tick_used: 12 } }), NOW + 30, NOW, t);
  assert.equal(later.announce, g.announce, 'the seconds do not change the announcement');
  for (const run of ['yielded', 'abandoned', 'idle']) {
    const v = A.grantView(grant({ iteration: { run } }), NOW, NOW, t);
    assert.ok(v.lines[1].endsWith(t(`stage.grant_run_${run}`)));
  }
  const other = A.grantView(grant({ iteration: { run: 'weird_run' } }), NOW, NOW, t);
  assert.ok(other.lines[1].endsWith(t('stage.act_reason_other', { code: 'other' })));
});

test('the approval cue points to the card (never its content), the verification cue to the settings', async () => {
  const A = await load('activity.js');
  const t = tr('ja');
  const card = A.grantView(grant({ card: true, cards: 1 }), NOW, NOW, t);
  assert.deepEqual(card.cue, { kind: 'approval', text: t('stage.grant_cue_approval'), link: t('stage.grant_cue_approval_link'), href: '/approvals' });
  assert.equal(card.announce, `${t('stage.grant_state_active')} · ${t('stage.grant_cue_approval')}`);
  const verify = A.grantView(grant({ awaiting_verification: true, goal_state: 'awaiting_verification', headline: { subject: 'goal', state: 'awaiting_verification' } }), NOW, NOW, t);
  assert.equal(verify.cue.kind, 'verify');
  assert.equal(verify.cue.href, '/settings#sec-grants');
  assert.equal(verify.headline, t('stage.grant_goal_awaiting'));
  // the card wins over the verification; an ended grant has no cue and no TTL
  assert.equal(A.grantView(grant({ card: true, awaiting_verification: true }), NOW, NOW, t).cue.kind, 'approval');
  // flags are booleans: a truthy string is no cue
  assert.equal(A.grantView(grant({ card: 'true', awaiting_verification: 'yes' }), NOW, NOW, t).cue, null);
  const ended = A.grantView(grant({ state: 'revoked', live: false, card: true, awaiting_verification: true, headline: { subject: 'grant', state: 'revoked', reason: 'kill_switch' } }), NOW, NOW, t);
  assert.equal(ended.cue, null);
  assert.equal(ended.ttl, null);
  assert.equal(ended.headline, `${t('stage.grant_state_revoked')} (kill_switch)`);
});

test('hostile values: codes only, a bad id is no id, unknown codes are shown as a code', async () => {
  const A = await load('activity.js');
  const t = tr('en');
  const g = A.grantView({
    id: `${MARKER}<img>`, state: '<b>x</b>', live: 'yes', headline: { subject: 'goal', state: MARKER + ' !', reason: '<i>' },
    goal_state: 'weird_state', iterations: -1, max_iterations: 1.5, ttl_left_sec: '60', budget_left: 5, budget_total: '10',
    card: 'true', awaiting_verification: 1, iteration: { run: '<x>', end: 'a b', tick_used: -3 },
    outcomes: [{ what: '<svg onload=1>', tools: [MARKER + '/../x', 'editor'], duration_ms: 'x', number: 1.5, ts: 'x', detail: 'a b' }, null, 'x'],
  }, NOW, NOW, t);
  const text = JSON.stringify(g);
  assert.equal(text.includes(MARKER), false);
  assert.equal(/[<>]/.test(text), false);
  assert.equal(g.title, t('stage.grant_h'));
  assert.equal(g.ttl, null);
  assert.equal(g.budget, null);
  assert.equal(g.cue, null, 'flags are booleans only');
  assert.equal(g.lines[0], t('stage.grant_goal', { state: t('stage.act_reason_other', { code: 'other' }) }), 'an unknown code is "other", never drawn');
  assert.equal(g.outcomes.length, 3);
  assert.equal(g.outcomes[0].ok, null);
  assert.ok(g.outcomes[2].text.includes('other, editor'), 'a bad tool name is "other"');
  const many = A.grantView(grant({ outcomes: [{ what: 'done', tools: Array(20).fill('editor') }] }), NOW, NOW, t);
  assert.equal(many.outcomes[0].text.split('editor').length - 1, 8, 'at most 8 tool names');
});

test('every stage.grant_* key the panel uses is in both catalogs; no HTML sink in activity.js', async () => {
  const src = fs.readFileSync(path.join(STAGE, 'activity.js'), 'utf8');
  const keys = [...new Set(src.match(/stage\.grant_[a-z_]+/g))];
  assert.ok(keys.length >= 45, `keys found: ${keys.length}`);
  for (const k of keys) for (const lang of ['ja', 'en']) assert.equal(typeof CAT[lang][k], 'string', `${lang} ${k}`);
  for (const k of Object.keys(CAT.en).filter((k) => k.startsWith('stage.grant_'))) assert.ok(keys.includes(k), `unused key ${k}`);
  assert.equal(/innerHTML|outerHTML|insertAdjacentHTML|document\.write|\.html\(/.test(src.replace(/\/\/.*$/gm, '')), false);
  const board = fs.readFileSync(path.join(STAGE, 'board.js'), 'utf8').replace(/\/\/.*$/gm, '');
  assert.equal(/innerHTML|outerHTML|insertAdjacentHTML/.test(board), false);
});

// --- A4: the attention table (docs/stage.md) --------------------------------------------------------

test('A4: an iteration lowers the board only at its start, only onto a board that is up for nothing', async () => {
  const { attention } = await load('activity.js');
  const base = { running: true, wasRunning: false, boardVisible: false, pinned: false, turnOpen: false, logOpen: false, hidden: false };
  const rows = [
    ['no iteration runs', { running: false }, 'none'],
    ['still the same iteration (rolled up with Esc: stays up)', { wasRunning: true }, 'none'],
    ['a reply (pinned or not) or the activity is on the board', { boardVisible: true }, 'none'],
    ['the board is pinned (rolled up with the pin on: the next reply must not be held)', { pinned: true }, 'none'],
    ['pinned unknown', { pinned: undefined }, 'none'],
    ['the person\'s own turn is open', { turnOpen: true }, 'none'],
    ['the log is open', { logOpen: true }, 'none'],
    ['the tab is hidden', { hidden: true }, 'none'],
    ['unknown running state', { running: 'yes' }, 'none'],
    ['an iteration starts, the board is closed, nothing else', {}, 'open'],
    ['was running unknown (first read) counts as not running', { wasRunning: undefined }, 'open'],
  ];
  for (const [what, over, want] of rows) assert.equal(attention({ ...base, ...over }), want, what);
});

test('A5: a watch read with the board closed widens unless an iteration runs; failures back off', async () => {
  const { watchResult } = await load('activity.js');
  assert.equal(watchResult(null, { showing: false, running: false }), null, 'a failure stays a failure');
  assert.equal(watchResult(null, { showing: true, running: true }), null);
  assert.equal(watchResult(true, { showing: true, running: false }), true, 'on the board: the feed decides');
  assert.equal(watchResult(false, { showing: true, running: true }), false);
  assert.equal(watchResult(true, { showing: false, running: false }), false, 'closed: idle cadence');
  assert.equal(watchResult(false, { showing: false, running: true }), true, 'closed, running: busy cadence');
});

test('the feed: an iteration running is busy (never while offline); the grant joins the change key', async () => {
  const { ActivityFeed } = await load('activity.js');
  let next = answer({ grant: grant({ iteration: { run: 'running' } }) });
  const feed = new ActivityFeed({ getJSON: async () => { if (next instanceof Error) throw next; return next; }, wall: () => NOW });
  assert.equal(await feed.poll(), true);
  assert.equal(feed.iterationRunning, true);
  assert.equal(feed.busy, true);
  next = answer({ grant: grant() });
  assert.equal(await feed.poll(), true, 'the iteration ended: a change');
  assert.equal(feed.iterationRunning, false);
  assert.equal(feed.busy, false);
  assert.equal(await feed.poll(), false, 'nothing new');
  next = answer({ grant: grant({ ttl_left_sec: 3000 }) });
  assert.equal(await feed.poll(), false, 'only the TTL moved: not a change');
  next = answer({ grant: grant({ budget_left: 700 }) });
  assert.equal(await feed.poll(), true, 'the budget moved');
  next = answer({ grant: grant({ iteration: { run: 'running' } }) });
  await feed.poll();
  next = new Error('down');
  assert.equal(await feed.poll(), null);
  assert.equal(feed.iterationRunning, true, 'one failure: not offline yet');
  assert.equal(await feed.poll(), null);
  assert.equal(feed.offline, true);
  assert.equal(feed.iterationRunning, false, 'offline: never "running" from an old answer');
  // off: no grant key, busy exactly as before
  const off = new ActivityFeed({ getJSON: async () => answer(), wall: () => NOW });
  await off.poll();
  assert.equal(off.iterationRunning, false);
  assert.equal(off.busy, false);
});

// --- DOM -----------------------------------------------------------------------------------------

const BOARD_HTML = `<!doctype html><section id="board" hidden><div id="board-head"><h2><span id="board-title-text"></span></h2>
    <button id="board-pin"></button><button id="board-copy"></button><button id="board-close"></button></div>
    <div id="board-body" tabindex="0"></div><div><span id="board-stats"></span><span id="board-note"></span></div></section>`;

function domBoard() {
  const dom = new JSDOM(BOARD_HTML);
  global.HTMLElement = dom.window.HTMLElement;
  return dom;
}

// renderActivity of main (76189f7) for answer() with no grant key (recorded with main's activity.js)
const OFF_HTML = "<section class=\"act-now\" data-state=\"idle\"><h3 class=\"act-h\">いまの状態</h3><p class=\"act-state\">待機中</p></section><section class=\"act-recent\"><h3 class=\"act-h\">直近の動き</h3><p class=\"act-empty\">まだ記録がありません</p></section><section class=\"act-side\"><div class=\"act-group\"><h3 class=\"act-h\">YOLO の窓</h3><p class=\"act-line act-muted\">窓は開いていません</p></div><div class=\"act-group\"><h3 class=\"act-h\">累計（サーバー起動から）</h3><p class=\"act-line\">ターン 1</p><p class=\"act-line\">入力 1 · 出力 2 · キャッシュ読 0 / 書 0</p><p class=\"act-line\">概算コスト: 単価が未設定</p></div></section>";

test('off: no grant key — the activity DOM is main\'s, no panel, no live region', { skip: JSDOM ? false : 'jsdom not installed' }, async () => {
  const dom = domBoard();
  const { document } = dom.window;
  const A = await load('activity.js');
  const B = await load('board.js');
  const t = tr('ja');
  const m = A.activityModel({ body: answer(), items: [] }, NOW, t);
  assert.equal('grant' in m, false);
  const wrap = document.createElement('div');
  wrap.append(...A.renderActivity(document, m, t));
  assert.equal(wrap.innerHTML, OFF_HTML);
  const board = B.createBoard({ root: document.getElementById('board'), t, reduced: () => true, copy: async () => true, openLink: () => {} });
  board.showActivity(A.renderActivity(document, m, t), { foot: 'f', announce: undefined });
  board.updateActivity(A.renderActivity(document, m, t), 'f', undefined);
  const body = document.getElementById('board-body');
  assert.equal(body.querySelector('.act-grant'), null);
  assert.equal(body.querySelector('[aria-live]'), null);
  // with stage C on and no grant (null): nothing either (the quieter choice)
  const none = A.activityModel({ body: answer({ grant: null }), items: [] }, NOW, t);
  assert.equal(none.grant, null);
  const wrap2 = document.createElement('div');
  wrap2.append(...A.renderActivity(document, none, t));
  assert.equal(wrap2.innerHTML, OFF_HTML);
});

test('the panel: a labelled region, meters with names, the cue link, textContent only', { skip: JSDOM ? false : 'jsdom not installed' }, async () => {
  const dom = domBoard();
  const { document } = dom.window;
  const A = await load('activity.js');
  const t = tr('en');
  const body = answer({ grant: grant({ card: true, cards: 1, iteration: { run: 'running', number: 3, tick_used: 12 } }) });
  const m = A.activityModel({ body, items: [] }, NOW, t);
  const nodes = A.renderActivity(document, m, t);
  assert.equal(nodes.length, 4, 'now, grant, recent, side');
  const box = nodes[1];
  assert.equal(box.className, 'act-grant');
  assert.equal(box.getAttribute('aria-labelledby'), 'act-grant-h');
  assert.equal(box.querySelector('h3').id, 'act-grant-h');
  assert.equal(box.querySelector('h3').textContent, `${t('stage.grant_h')} abababab`);
  assert.equal(box.dataset.state, 'running');
  const meters = box.querySelectorAll('meter');
  assert.equal(meters.length, 2);
  assert.deepEqual([...meters].map((x) => x.getAttribute('aria-label')), [t('stage.grant_ttl_label'), t('stage.grant_budget_label')]);
  assert.deepEqual([...meters].map((x) => [x.getAttribute('value'), x.getAttribute('max')]), [['3600', '7200'], ['750', '1000']]);
  const cue = box.querySelector('.act-cue');
  assert.equal(cue.dataset.cue, 'approval');
  assert.equal(cue.querySelector('a').getAttribute('href'), '/approvals');
  const links = [...box.querySelectorAll('a')].map((a) => a.getAttribute('href'));
  assert.deepEqual(links, ['/approvals', '/settings#sec-grants'], 'display only: plain same-origin links');
  assert.equal(box.querySelectorAll('button, input, form').length, 0, 'no control on the board');
  assert.equal(box.querySelectorAll('.act-outcomes li').length, 2);
  // a hostile code never becomes markup
  const hostile = A.renderActivity(document, A.activityModel({ body: answer({ grant: grant({ headline: { subject: 'grant', state: '<img src=x onerror=1>' }, outcomes: [{ what: '<b>', tools: ['<i>'] }] }) }), items: [] }, NOW, t), t)[1];
  assert.equal(hostile.querySelector('img, b, i'), null);
  // unknown: no numbers, no settings link
  const unknown = A.renderActivity(document, A.activityModel({ body: answer({ grant: grant() }), items: [], offline: true }, NOW, t), t)[1];
  assert.equal(unknown.querySelector('meter'), null);
  assert.equal(unknown.querySelector('a'), null);
  assert.equal(unknown.querySelector('.act-state').textContent, t('stage.grant_unknown_offline'));
});

test('the live region: one, kept across refreshes, written only when the text changes', { skip: JSDOM ? false : 'jsdom not installed' }, async () => {
  const dom = domBoard();
  const { document } = dom.window;
  const A = await load('activity.js');
  const B = await load('board.js');
  const t = tr('ja');
  const board = B.createBoard({ root: document.getElementById('board'), t, reduced: () => true, copy: async () => true, openLink: () => {} });
  const draw = (g, now = NOW) => {
    const m = A.activityModel({ body: answer({ grant: g }), items: [] }, now, t);
    return [A.renderActivity(document, m, t), m.grant ? m.grant.announce : undefined];
  };
  let [nodes, say] = draw(grant());
  board.showActivity(nodes, { foot: 'f', announce: say });
  const body = document.getElementById('board-body');
  const live = body.querySelector('[aria-live]');
  assert.equal(live.getAttribute('aria-live'), 'polite');
  assert.equal(live.getAttribute('role'), 'status');
  assert.equal(live.textContent, t('stage.grant_state_active'));
  let writes = 0;
  new dom.window.MutationObserver((list) => { writes += list.length; }).observe(live, { childList: true, characterData: true, subtree: true });
  for (let s = 1; s <= 5; s += 1) {
    [nodes, say] = draw(grant(), NOW + s);
    board.updateActivity(nodes, 'f', say);
  }
  await new Promise((r) => setTimeout(r, 0));
  assert.equal(writes, 0, 'the seconds tick: no announcement');
  assert.equal(body.querySelectorAll('[aria-live]').length, 1);
  assert.equal(body.querySelector('[aria-live]'), live, 'the same node');
  [nodes, say] = draw(grant({ iteration: { run: 'running' } }));
  board.updateActivity(nodes, 'f', say);
  assert.equal(live.textContent, `${t('stage.grant_state_active')} · ${t('stage.grant_run_running')}`);
  // a reply takes the board: the live region goes with the activity body
  const { classify } = await load('markdown.js');
  const text = '# 見出し\n\n' + 'あ'.repeat(200);
  assert.equal(board.show({ key: 'r1', text, c: classify(text) }), 'replace');
  assert.equal(body.querySelector('[aria-live]'), null);
});

test('A4 PIN-CARRY: a pinned, rolled-up board is not lowered by an iteration; her next reply is shown', { skip: JSDOM ? false : 'jsdom not installed' }, async () => {
  const dom = domBoard();
  const { document } = dom.window;
  const A = await load('activity.js');
  const B = await load('board.js');
  const { classify } = await load('markdown.js');
  const t = tr('en');
  const board = B.createBoard({ root: document.getElementById('board'), t, reduced: () => true, copy: async () => true, openLink: () => {} });
  const text = '# Heading\n\n' + 'a'.repeat(200);
  assert.equal(board.show({ key: 'r1', text, c: classify(text) }), 'open');
  document.getElementById('board-pin').click();
  board.close();
  board.tick(1);
  assert.equal(board.visible, false);
  assert.equal(board.pinned, true, 'the pin is board-level and survives the roll-up');
  // the same arguments main.js notice() passes
  const verdict = A.attention({ running: true, wasRunning: false, boardVisible: board.visible, pinned: board.pinned, turnOpen: false, logOpen: false, hidden: false });
  assert.equal(verdict, 'none');
  assert.equal(board.show({ key: 'r2', text, c: classify(text) }), 'open', 'her next reply takes the board');
  // unpinned and closed: the iteration's start lowers it, without a pin
  document.getElementById('board-pin').click();
  board.close();
  board.tick(1);
  assert.equal(A.attention({ running: true, wasRunning: false, boardVisible: board.visible, pinned: board.pinned, turnOpen: false, logOpen: false, hidden: false }), 'open');
  assert.equal(board.showActivity([], { explicit: false }), 'open');
  assert.equal(board.pinned, false, 'the auto path never sets the pin');
  assert.equal(board.show({ key: 'r3', text, c: classify(text) }), 'replace');
});

test('the live region goes with the grant: running -> grant null / no key -> no stale announcement', { skip: JSDOM ? false : 'jsdom not installed' }, async () => {
  const dom = domBoard();
  const { document } = dom.window;
  const A = await load('activity.js');
  const B = await load('board.js');
  const t = tr('en');
  const board = B.createBoard({ root: document.getElementById('board'), t, reduced: () => true, copy: async () => true, openLink: () => {} });
  const draw = (body) => {
    const m = A.activityModel({ body, items: [] }, NOW, t);
    return [A.renderActivity(document, m, t), m.grant ? m.grant.announce : undefined];
  };
  let [nodes, say] = draw(answer({ grant: grant({ iteration: { run: 'running' } }) }));
  board.showActivity(nodes, { announce: say });
  const body = document.getElementById('board-body');
  assert.equal(body.querySelector('[aria-live]').textContent, `${t('stage.grant_state_active')} · ${t('stage.grant_run_running')}`);
  for (const next of [answer({ grant: null }), answer()]) {
    [nodes, say] = draw(next);
    board.updateActivity(nodes, 'f', say);
    assert.equal(body.querySelector('[aria-live]'), null, 'no stale "running" after the grant is gone');
    assert.equal(body.querySelector('.act-grant'), null, 'the panel goes too');
    assert.ok(body.querySelector('.act-now') && body.querySelector('.act-recent'), 'the rest of the activity stays');
    assert.equal(body.textContent.includes(t('stage.grant_run_running')), false);
    [nodes, say] = draw(answer({ grant: grant({ iteration: { run: 'running' } }) }));
    board.updateActivity(nodes, 'f', say);
    assert.equal(body.querySelectorAll('[aria-live]').length, 1);
  }
});

test('B5: the exact marker in every slot (state, reason, headline, end, detail, tool, outcome) is never drawn', { skip: JSDOM ? false : 'jsdom not installed' }, async () => {
  const dom = domBoard();
  const { document } = dom.window;
  const A = await load('activity.js');
  for (const lang of ['ja', 'en']) {
    const t = tr(lang);
    const m = MARKER;
    const hostile = grant({
      state: m, reason: m, goal_state: m, goal_reason: m, headline: { subject: 'goal', state: m, reason: m },
      iteration: { run: m, end: m, detail: m, number: 1 },
      outcomes: [{ what: m, detail: m, tools: [m, 'file_read'], number: 1, ts: NOW }],
    });
    for (const head of [{ subject: 'goal', state: m, reason: m }, { subject: 'grant', state: m, reason: m }]) {
      const view = A.grantView({ ...hostile, headline: head }, NOW, NOW, t);
      assert.equal(JSON.stringify(view).includes(m), false);
      const nodes = A.renderActivity(document, A.activityModel({ body: answer({ grant: { ...hostile, headline: head } }), items: [] }, NOW, t), t);
      const wrap = document.createElement('div');
      wrap.append(...nodes);
      assert.equal(wrap.textContent.includes(m), false, lang);
      assert.equal(wrap.innerHTML.includes(m), false, lang);
    }
    // a reason of the other subject is not drawn either
    const mixed = A.grantView(grant({ headline: { subject: 'grant', state: 'revoked', reason: 'verified' } }), NOW, NOW, t);
    assert.equal(mixed.headline, t('stage.grant_state_revoked'));
    // known codes are drawn
    const known = A.grantView(grant({ headline: { subject: 'grant', state: 'revoked', reason: 'kill_switch' }, outcomes: [{ what: 'failed', detail: 'child_error', tools: ['run_checks'] }] }), NOW, NOW, t);
    assert.equal(known.headline, `${t('stage.grant_state_revoked')} (kill_switch)`);
    assert.ok(known.outcomes[0].text.includes('(child_error)') && known.outcomes[0].text.endsWith('run_checks'));
  }
});

test('B3: a focused link of the panel (and an artifacts link) keeps the focus across the 1 s refresh; focus elsewhere is untouched', { skip: JSDOM ? false : 'jsdom not installed' }, async () => {
  const dom = new JSDOM(BOARD_HTML.replace('</section>', '</section><input id="composer">'));
  global.HTMLElement = dom.window.HTMLElement;
  const { document } = dom.window;
  const A = await load('activity.js');
  const B = await load('board.js');
  const t = tr('en');
  const board = B.createBoard({ root: document.getElementById('board'), t, reduced: () => true, copy: async () => true, openLink: () => {} });
  const rows = [
    { id: 1, ts: NOW, turn: 't1', kind: 'turn', what: 'completed', ok: true, artifacts: 2 },
    { id: 2, ts: NOW, turn: 't1', kind: 'tool', what: 'done', name: 'file_write', ok: true, artifacts: 2 },
  ];
  // the production refresh (main.js draw): a new model, new nodes, updateActivity
  const refresh = (g, now = NOW) => {
    const m = A.activityModel({ body: answer({ grant: g, items: rows }), items: rows }, now, t);
    return board.updateActivity(A.renderActivity(document, m, t), 'f', m.grant ? m.grant.announce : undefined);
  };
  const m0 = A.activityModel({ body: answer({ grant: grant({ card: true, cards: 1 }), items: rows }), items: rows }, NOW, t);
  board.showActivity(A.renderActivity(document, m0, t), { announce: m0.grant.announce });
  const body = document.getElementById('board-body');
  for (const [selector, nth] of [['.act-grant a[href="/settings#sec-grants"]', 0], ['.act-cue a', 0], ['.act-row a.act-link', 1]]) {
    const link = body.querySelectorAll(selector)[nth];
    link.focus();
    assert.equal(document.activeElement, link);
    for (let s = 1; s <= 3; s += 1) assert.equal(refresh(grant({ card: true, cards: 1 }), NOW + s), true);
    const now = document.activeElement;
    assert.notEqual(now, link, 'a new node');
    assert.equal(body.contains(now), true, `${selector}: the focus stays in the panel`);
    assert.equal(now.getAttribute('href'), link.getAttribute('href'));
    assert.equal(now, body.querySelectorAll(selector)[nth], 'the same occurrence');
  }
  // the focus is given back without scrolling the board (preventScroll)
  const calls = [];
  const proto = dom.window.HTMLElement.prototype;
  const real = proto.focus;
  proto.focus = function focus(options) {
    calls.push(options);
    return real.call(this, options);
  };
  body.querySelector('.act-grant a[href="/settings#sec-grants"]').focus();
  calls.length = 0;
  refresh(grant({ card: true, cards: 1 }));
  proto.focus = real;
  assert.equal(calls.length, 1);
  assert.equal(calls[0] && calls[0].preventScroll, true);
  // the focused control went away (the cue ended): the focus is not moved to another control
  body.querySelector('.act-cue a').focus();
  refresh(grant());
  assert.equal(body.contains(document.activeElement) && document.activeElement.tagName === 'A', false);
  // focus outside the board (the composer) is never taken
  const composer = document.getElementById('composer');
  composer.focus();
  refresh(grant({ card: true, cards: 1 }));
  assert.equal(document.activeElement, composer);
  // nothing focused before: nothing focused after
  composer.blur();
  refresh(grant({ card: true, cards: 1 }));
  assert.equal(document.activeElement, document.body);
});

test('round 2: a busy read between two versions is drawn "unknown" — never an old count with a new end', { skip: JSDOM ? false : 'jsdom not installed' }, async () => {
  const dom = domBoard();
  const { document } = dom.window;
  const A = await load('activity.js');
  const t = tr('en');
  const running = grant({ iterations: 0, budget_left: 1000, iteration: { run: 'running', number: 1, tick_used: 10 }, outcomes: [] });
  const settled = grant({ iterations: 1, budget_left: 990, iteration: { run: 'idle', number: 1, end: 'done', tick_used: 10 }, outcomes: [{ what: 'done', number: 1, tick_used: 10, ts: NOW, tools: [] }] });
  const answers = [answer({ grant: running }), answer({ grant: { error: 'grant_busy' } }), answer({ grant: settled })];
  const feed = new A.ActivityFeed({ getJSON: async () => answers.shift(), wall: () => NOW });
  const texts = [];
  for (let n = 0; n < 3; n += 1) {
    await feed.poll();
    const wrap = document.createElement('div');
    wrap.append(...A.renderActivity(document, A.activityModel(feed.snapshot(), NOW, t), t));
    texts.push(wrap.querySelector('.act-grant'));
  }
  const [first, busy, last] = texts;
  assert.ok(first.textContent.includes(t('stage.grant_iterations', { n: 1, max: 10 })));
  // the busy read keeps the previous panel, marked as the last read's values (silently)
  assert.equal(busy.dataset.stale, '1');
  assert.ok(busy.textContent.includes(t('stage.grant_stale')));
  assert.equal(busy.textContent.replace(t('stage.grant_stale'), ''), first.textContent, 'the same panel as before, nothing mixed');
  assert.equal(busy.textContent.includes(t('stage.grant_iterations', { n: 0, max: 10 })), false);
  assert.equal(busy.textContent.includes(t('stage.grant_last_stop', { reason: t('stage.grant_end_done') })), false);
  assert.equal(busy.textContent.includes(t('stage.grant_unknown')), false, 'not "could not be read" for one busy read');
  assert.ok(last.textContent.includes(t('stage.grant_iterations', { n: 1, max: 10 })));
  assert.ok(last.textContent.includes(t('stage.grant_last_stop', { reason: t('stage.grant_end_done') })));
  assert.equal(last.querySelectorAll('.act-outcomes li').length, 1);
});

test('round 2: a busy read makes no announcement and keeps the panel; two failures (offline) say unknown', { skip: JSDOM ? false : 'jsdom not installed' }, async () => {
  const dom = domBoard();
  const { document } = dom.window;
  const A = await load('activity.js');
  const B = await load('board.js');
  const t = tr('en');
  const board = B.createBoard({ root: document.getElementById('board'), t, reduced: () => true, copy: async () => true, openLink: () => {} });
  let next = answer({ grant: grant({ iteration: { run: 'running', number: 1 } }) });
  const feed = new A.ActivityFeed({ getJSON: async () => { if (next instanceof Error) throw next; return next; }, wall: () => NOW });
  const draw = (first) => {
    const m = A.activityModel(feed.snapshot(), NOW, t);
    const nodes = A.renderActivity(document, m, t);
    if (first) board.showActivity(nodes, { announce: m.grant ? m.grant.announce : undefined });
    else board.updateActivity(nodes, 'f', m.grant ? m.grant.announce : undefined);
  };
  await feed.poll();
  draw(true);
  const live = document.querySelector('#board-body [aria-live]');
  const said = live.textContent;
  let writes = 0;
  new dom.window.MutationObserver((list) => { writes += list.length; }).observe(live, { childList: true, characterData: true, subtree: true });
  next = answer({ grant: { error: 'grant_busy' } });
  await feed.poll();
  draw(false);
  await new Promise((r) => setTimeout(r, 0));
  assert.equal(writes, 0, 'a single busy read announces nothing');
  assert.equal(document.querySelector('#board-body [aria-live]'), live);
  assert.equal(live.textContent, said);
  assert.equal(feed.iterationRunning, true, 'the last readable block still says running');
  assert.equal(document.querySelector('.act-grant').dataset.stale, '1');
  // a readable answer: the stale mark goes
  next = answer({ grant: grant({ iteration: { run: 'running', number: 1 } }) });
  await feed.poll();
  draw(false);
  assert.equal(document.querySelector('.act-grant').dataset.stale, undefined);
  // grant_unreadable (an error, not contention) and offline still say unknown, no numbers
  next = answer({ grant: { error: 'grant_unreadable' } });
  await feed.poll();
  draw(false);
  assert.equal(document.querySelector('.act-grant .act-state').textContent, t('stage.grant_unknown'));
  next = answer({ grant: grant() });
  await feed.poll();
  next = answer({ grant: { error: 'grant_busy' } });
  await feed.poll();
  next = new Error('down');
  await feed.poll();
  await feed.poll();
  draw(false);
  assert.equal(document.querySelector('.act-grant .act-state').textContent, t('stage.grant_unknown_offline'));
  assert.equal(document.querySelector('.act-grant meter'), null);
});

test('round 3 B1: busy retention has an age limit (3 answers / 20 s); only a readable block resets it', { skip: JSDOM ? false : 'jsdom not installed' }, async () => {
  const dom = domBoard();
  const { document } = dom.window;
  const A = await load('activity.js');
  const t = tr('en');
  assert.equal(A.BUSY_KEEP_READS, 3);
  assert.equal(A.BUSY_KEEP_SEC, 20);
  let wall = NOW;
  let g = grant({ budget_left: 990, iteration: { run: 'running', number: 1, tick_used: 10 } });
  const feed = new A.ActivityFeed({ getJSON: async () => ({ ...answer({ grant: g }), server_time: wall }), wall: () => wall });
  const view = () => A.activityModel(feed.snapshot(), feed.serverNow(), t).grant;
  const panel = () => A.renderGrant(document, view(), t);
  await feed.poll();
  // Astra's shape: 1,000 busy answers over 20,000 s -> unknown, not running, no numbers
  g = { error: 'grant_busy' };
  for (let i = 0; i < 1000; i += 1) {
    wall += 20;
    await feed.poll();
  }
  assert.equal(view().unknown, true);
  assert.equal(feed.iterationRunning, false);
  assert.equal(panel().querySelectorAll('meter').length, 0);
  // the count: 3 busy answers within 20 s stay "Updating", the 4th is unknown
  g = grant({ iteration: { run: 'running', number: 2 } });
  await feed.poll();
  g = { error: 'grant_busy' };
  for (let i = 1; i <= 3; i += 1) {
    wall += 1;
    await feed.poll();
    assert.equal(view().stale, true, `busy answer ${i}`);
    assert.equal(feed.iterationRunning, true);
  }
  wall += 1;
  await feed.poll();
  assert.equal(view().unknown, true, 'the 4th busy answer');
  assert.equal(feed.iterationRunning, false);
  // the time: 2 busy answers but 21 s after the last readable block -> unknown
  g = grant({ iteration: { run: 'running', number: 3 } });
  await feed.poll();
  g = { error: 'grant_busy' };
  wall += 5;
  await feed.poll();
  assert.equal(view().stale, true);
  wall += 16;
  await feed.poll();
  assert.equal(view().unknown, true, '21 s since the last readable block');
  // an unreadable answer counts too and is drawn unknown; a readable one resets
  g = grant({ iteration: { run: 'running', number: 4 } });
  await feed.poll();
  g = { error: 'grant_unreadable' };
  await feed.poll();
  assert.equal(view().unknown, true);
  g = { error: 'grant_busy' };
  await feed.poll();
  await feed.poll();
  await feed.poll();
  assert.equal(view().unknown, true, 'unreadable + 3 busy = 4 unreadable answers');
  g = grant({ iteration: { run: 'running', number: 4 } });
  await feed.poll();
  g = { error: 'grant_busy' };
  await feed.poll();
  assert.equal(view().stale, true, 'reset by the readable block');
  assert.equal(A.isUnreadableGrant({ error: 'grant_busy' }), true);
  assert.equal(A.isUnreadableGrant({ error: 'grant_unreadable' }), true);
  assert.equal(A.isUnreadableGrant({ error: 'x' }), true);
  assert.equal(A.isUnreadableGrant('x'), true);
  assert.equal(A.isUnreadableGrant(null), false, 'no grant is readable');
  assert.equal(A.isUnreadableGrant(undefined), false, 'stage C off');
  assert.equal(A.isUnreadableGrant(grant()), false);
});
