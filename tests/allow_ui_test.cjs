// Session allowances: badge, list, revoke, log lines and the card's buttons (ADR 0025 PR 2):
// node --test tests/allow_ui_test.cjs
// The pure parts run everywhere; the DOM parts need jsdom (npm install --no-save jsdom axe-core).
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { resolve } = require('node:path');

let JSDOM = null;
try { ({ JSDOM } = require('jsdom')); } catch (err) { /* DOM tests skipped below */ }
const skip = JSDOM ? false : 'jsdom not installed';
const ROOT = resolve(__dirname, '..');
const SRC = readFileSync(resolve(ROOT, 'dashboard/static/js/allow_ui.js'), 'utf8');
const APPROVALS = readFileSync(resolve(ROOT, 'dashboard/static/js/approvals.js'), 'utf8');
const CAT = {
  ja: JSON.parse(readFileSync(resolve(ROOT, 'dashboard/static/i18n/catalog.ja.json'), 'utf8')),
  en: JSON.parse(readFileSync(resolve(ROOT, 'dashboard/static/i18n/catalog.en.json'), 'utf8')),
};
const A = require('../dashboard/static/js/allow_ui.js');
const tr = (lang) => (key, vars) => {
  let text = Object.hasOwn(CAT[lang], key) ? CAT[lang][key] : `!${key}`;
  if (vars) text = text.replace(/\{(\w+)\}/g, (m, n) => (n in vars ? String(vars[n]) : m));
  return text;
};
const tick = () => new Promise((r) => setTimeout(r, 0));
async function settle() { for (let i = 0; i < 60; i += 1) await tick(); }

const ID = 'ab'.repeat(16);
const row = (over = {}) => ({
  allow_id: ID, ref: ID.slice(0, 8), tool: 'file_write', class_kind: 'dir_subtree', label: 'projects/game/',
  whole_root: false, expires_at: 9000, remaining_sec: 7140, uses: 3, ...over,
});

test('the class words: directory, whole workspace, host, GET-only host, layer, tool (ja/en)', () => {
  const ja = tr('ja'), en = tr('en');
  assert.equal(A.classText('file_write', { class_kind: 'dir_subtree', label: 'projects/game/', max_hours: 2 }, ja),
    'file_write · projects/game/ 配下 · 最長2時間');
  assert.equal(A.classText('file_write', { class_kind: 'dir_subtree', label: 'projects/game/', max_hours: 2 }, en),
    'file_write · under projects/game/ · up to 2 h');
  assert.equal(A.classText('editor', { class_kind: 'dir_subtree', label: '', whole_root: true }, ja),
    'editor · ワークスペース全体 · 最長2時間');
  assert.equal(A.classText('web_fetch', { class_kind: 'host', label: 'docs.python.org' }, ja),
    'web_fetch · docs.python.org へのリクエスト · 最長2時間');
  assert.equal(A.classText('http_request', { class_kind: 'host_read', label: 'api.example.org' }, en),
    'http_request · GET/HEAD requests to api.example.org · up to 2 h');
  assert.equal(A.classText('memory_write', { class_kind: 'layer', label: 'episodic' }, en),
    'memory_write · the episodic layer · up to 2 h');
  assert.equal(A.classText('memory_search', { class_kind: 'tool', label: 'memory_search' }, ja),
    'memory_search · 引数を問わず · 最長2時間');
  // Anything the page does not know is never a guessed class.
  assert.equal(A.targetText({ class_kind: 'glob', label: '*' }, ja), '(終了した継続許可)');
});

test('viewOf draws the live rows only; a malformed row is dropped', () => {
  const v = A.viewOf({ enabled: true, allowances: [row(), { allow_id: 'x', tool: 'file_write' }] }, tr('ja'));
  assert.equal(v.count, 1);
  assert.deepEqual(v.rows[0], {
    id: ID, ref: ID.slice(0, 8), tool: 'file_write', text: 'file_write · projects/game/ 配下',
    left: '残り 1:59', uses: '3回使用',
  });
  assert.equal(A.viewOf(null, tr('en')).count, 0);
});

test('a log line takes its class from the live list, else says it ended', () => {
  const use = { seq: 4, ref: ID.slice(0, 8), tool: 'file_write', class_kind: 'dir_subtree' };
  assert.equal(A.lineText(use, { allowances: [row()] }, tr('ja')), '↻ 継続許可で承認: file_write · projects/game/ 配下');
  assert.equal(A.lineText(use, { allowances: [] }, tr('en')), '↻ Approved by allowance: file_write · (ended allowance)');
});

test('the card view: nothing while the code constant is off or the server says not_enabled', () => {
  const context = { t: tr('ja'), StiraAllow: A, CustomEvent: class {} };
  context.window = context;
  new Function('window', 'globalThis', 't', `${APPROVALS}`)(context, context, context.t);
  const { allowView, ALLOW_AGAIN_ON } = context.StiraApprovals;
  assert.equal(ALLOW_AGAIN_ON, true, 'PR 3: both buttons on');
  const offered = { tool_name: 'file_write', allow_again: { offered: true, token: 'tk', class_kind: 'dir_subtree', label: 'game/', max_hours: 2 } };
  assert.deepEqual(allowView(offered, false), { buttons: false, reason: null }, 'the constant off hides them');
  assert.deepEqual(allowView({ allow_again: { offered: false, reason: 'not_enabled' } }, true), { buttons: false, reason: null });
  // As PR 3 draws it (the constant passed on for the test).
  assert.deepEqual(allowView(offered, true), { buttons: true, full: false, classText: 'file_write · game/ 配下 · 最長2時間', reason: null });
  assert.equal(allowView({ allow_again: { offered: true, token: 'tk', class_kind: 'host', label: 'a.b', list_full: true } }, true).full, true);
  assert.deepEqual(allowView({ allow_again: { offered: false, reason: 'exec' } }, true),
    { buttons: false, reason: 'この操作は毎回確認します(実行系のツール)' });
  assert.match(allowView({ allow_again: { offered: false, reason: 'some_new_code' } }, true).reason, /some_new_code/);
});

function dom() {
  const page = new JSDOM(`<!doctype html><html><body>
    <header><div id="allow-slot-chat" class="allow-slot" hidden></div></header>
    <div id="conversation" role="log"></div></body></html>`, { url: 'http://127.0.0.1/chat' });
  return page.window;
}

function harness(states, { posts = [] } = {}) {
  const w = dom();
  let n = 0;
  const lines = [];
  const ui = A.create({
    document: w.document, window: w, t: tr('ja'),
    getJSON: async () => states[Math.min(n++, states.length - 1)],
    post: async (url) => { posts.push(url); return { json: async () => ({ ...states[states.length - 1], allowances: [] }) }; },
    onLine: (text) => lines.push(text),
  });
  return { w, ui, lines, posts };
}

test('the badge is hidden without allowances; a list with revoke per row and Revoke all', { skip }, async () => {
  const live = { enabled: true, allowances: [row()], uses: [], seq: 0 };
  const h = harness([{ enabled: true, allowances: [], uses: [], seq: 0 }, live]);
  await h.ui.start();
  const slot = h.w.document.getElementById('allow-slot-chat');
  assert.equal(slot.hidden, true);
  await h.ui.read();
  assert.equal(slot.hidden, false);
  const toggle = slot.querySelector('.allow-toggle');
  assert.equal(toggle.textContent, '継続許可 1件');
  assert.equal(toggle.getAttribute('aria-expanded'), 'false');
  toggle.click();
  const panel = slot.querySelector('.allow-panel');
  assert.equal(panel.hidden, false);
  assert.equal(slot.querySelector('.allow-toggle').getAttribute('aria-expanded'), 'true');
  assert.equal(slot.querySelector('.allow-text').textContent, 'file_write · projects/game/ 配下');
  assert.equal(slot.querySelector('.allow-meta').textContent, '残り 1:59 · 3回使用');
  slot.querySelector('.allow-revoke').click();
  await settle();
  assert.deepEqual(h.posts, [`/api/allowances/${ID}/revoke`]);
  assert.ok(h.lines.includes('継続許可を取り消しました'));
  h.ui.stop();
});

test('Revoke all is one press, no confirmation', { skip }, async () => {
  const live = { enabled: true, allowances: [row(), row({ allow_id: 'cd'.repeat(16), tool: 'web_fetch', class_kind: 'host', label: 'a.example' })], uses: [], seq: 0 };
  const h = harness([live]);
  await h.ui.start();
  const slot = h.w.document.getElementById('allow-slot-chat');
  slot.querySelector('.allow-toggle').click();
  slot.querySelector('.allow-revoke-all').click();
  await settle();
  assert.deepEqual(h.posts, ['/api/allowances/revoke-all']);
  assert.ok(h.lines.includes('すべての継続許可を取り消しました'));
  h.ui.stop();
});

test('log lines: none for answers before this page loaded, one per new answer, then "N more"', { skip }, async () => {
  const use = (seq) => ({ seq, ref: ID.slice(0, 8), tool: 'file_write', class_kind: 'dir_subtree' });
  const h = harness([
    { enabled: true, allowances: [row()], uses: [use(1), use(2)], seq: 2 },
    { enabled: true, allowances: [row()], uses: [use(1), use(2), use(3)], seq: 3 },
    { enabled: true, allowances: [row()], uses: [use(30)], seq: 30 },
  ]);
  await h.ui.start();
  assert.deepEqual(h.lines, []);
  await h.ui.read();
  assert.deepEqual(h.lines, ['↻ 継続許可で承認: file_write · projects/game/ 配下']);
  await h.ui.read();
  assert.deepEqual(h.lines.slice(1), ['↻ ほかに 26 件を継続許可で承認しました', '↻ 継続許可で承認: file_write · projects/game/ 配下']);
  const log = h.w.document.getElementById('conversation');
  assert.equal(log.querySelectorAll('.allow-line').length, 3);
  h.ui.stop();
});

test('a server that does not offer the buttons is read once and never polled (PR 2)', { skip }, async () => {
  let reads = 0;
  const w = dom();
  const ui = A.create({ document: w.document, window: w, t: tr('en'), getJSON: async () => { reads += 1; return { enabled: false, allowances: [], uses: [], seq: 0 }; } });
  await ui.start();
  await new Promise((r) => setTimeout(r, 30));
  assert.equal(reads, 1);
  assert.equal(w.document.getElementById('allow-slot-chat').hidden, true);
});

test('a press result from a card becomes a summary line', { skip }, async () => {
  const h = harness([{ enabled: true, allowances: [], uses: [], seq: 0 }]);
  await h.ui.start();
  h.w.document.dispatchEvent(new h.w.CustomEvent('stira:allow-result', { detail: { text: '今回のみ承認しました — 停止/状態の変化があったため継続許可は作成していません' } }));
  await settle();
  assert.equal(h.lines[0], '今回のみ承認しました — 停止/状態の変化があったため継続許可は作成していません');
  assert.equal(h.w.document.querySelector('#conversation .allow-line-summary').textContent, h.lines[0]);
  h.ui.stop();
});

test('Make Always waits for the terminal result, then the typed-name confirmation and one card save', async () => {
  const views = [
    { state: 'waiting', tool: 'file_write' },
    { state: 'ready', tool: 'file_write', terminal: 'committed', token: 'save-tk', base_version: 'v1', confirm_names: ['file_write'], needs_confirmation: ['tool_policy_loosened'], breaker_ok: true },
  ];
  const said = [], posts = [];
  let asked = null;
  const result = await A.makeAlways({
    requestId: 'r1', tool: 'file_write', t: tr('ja'), wait: async () => {}, say: (text) => said.push(text),
    getJSON: async (url) => { assert.equal(url, '/api/config/hub/card/r1'); return views.shift(); },
    confirm: async (spec) => { asked = spec; return true; },
    post: async (url, body) => { posts.push({ url, body }); return { ok: true, status: 200, json: async () => ({ saved: ['tools'] }) }; },
  });
  assert.equal(result, 'saved');
  assert.equal(asked.title, '『常に許可』に変更: file_write');
  assert.equal(asked.intro, '実行されました');
  assert.deepEqual(asked.names.expected, ['file_write'], 'Q7: the hub\'s typed tool name');
  assert.match(asked.items[0], /今後すべての呼び出し/);
  assert.deepEqual(posts, [{ url: '/api/config/hub', body: { source: 'approval_card', request_id: 'r1', save_token: 'save-tk', base_version: 'v1', acknowledged_dangers: ['tool_policy_loosened'], confirm_always: ['file_write'] } }]);
  assert.deepEqual(said, ['file_write を『常に許可』にしました。取り消し: 設定 → ツール(または 履歴)']);
});

test('Make Always: closing saves nothing; a stale confirmation is said and offered once more', async () => {
  const ready = { state: 'ready', tool: 'web_fetch', terminal: 'failed', token: 't', base_version: 'v', confirm_names: ['web_fetch'], needs_confirmation: [] };
  const closed = await A.makeAlways({ requestId: 'r', tool: 'web_fetch', t: tr('en'), wait: async () => {}, say: () => {},
    getJSON: async () => ready, confirm: async () => false, post: async () => assert.fail('nothing saved') });
  assert.equal(closed, 'closed');
  const said = [];
  let n = 0;
  const stale = await A.makeAlways({ requestId: 'r', tool: 'web_fetch', t: tr('en'), wait: async () => {}, say: (s) => said.push(s),
    getJSON: async () => ready, confirm: async () => true,
    post: async () => { n += 1; return { ok: false, status: 409, json: async () => ({ code: 'stale_confirmation' }) }; } });
  assert.equal(stale, 'stale');
  assert.equal(n, 2, 'confirm anew once (a new token), then stop');
  assert.equal(said[0], 'Something changed after you confirmed; nothing was saved. Check the result and confirm again.');
  const gone = [];
  await A.makeAlways({ requestId: 'r', tool: 'web_fetch', t: tr('en'), wait: async () => {}, say: (s) => gone.push(s), getJSON: async () => ({ state: 'gone' }), confirm: async () => assert.fail('no dialog') });
  assert.match(gone[0], /can no longer be used/);
});
