// #350 phase 3: "new conversation" (POST /api/chat/session/new) and the context gauge in % —
// the shared parts in chat_core.js used by the chat page (chat.js) and the Stage (talk.js).
// node --test tests/chat_new_session_test.cjs
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const ROOT = path.join(__dirname, '..');
const core = require('../dashboard/static/js/chat_core.js');
const ja = JSON.parse(fs.readFileSync(path.join(ROOT, 'dashboard/static/i18n/catalog.ja.json'), 'utf8'));
const en = JSON.parse(fs.readFileSync(path.join(ROOT, 'dashboard/static/i18n/catalog.en.json'), 'utf8'));
const tja = (key, vars) => String(ja[key] ?? key).replace(/\{(\w+)\}/g, (_, k) => (vars && k in vars ? vars[k] : `{${k}}`));
const t = (key, vars) => (vars ? `${key}(${JSON.stringify(vars)})` : key);
// LF only: a Windows checkout (core.autocrlf) has CRLF line ends
const read = (rel) => fs.readFileSync(path.join(ROOT, rel), 'utf8').replace(/\r\n/g, '\n');

const json = (status, body) => ({ ok: status >= 200 && status < 300, status, json: async () => body });
function fakeFetch(outcomes) {
  const calls = [];
  const fetch = async (url, init) => {
    calls.push({ url, init });
    const next = outcomes.shift();
    if (next instanceof Error) throw next;
    return next;
  };
  return { fetch, calls };
}
let n = 0;
const makeKey = () => `nk-${(n += 1)}`;

test('newSessionBlock: fail closed while a turn runs, an approval waits, a send is unknown, offline, loading', () => {
  const ok = { ready: true, busy: false, turnOpen: false, approval: false, unsettled: false, offline: false, starting: false };
  assert.equal(core.newSessionBlock(ok), null);
  assert.equal(core.newSessionBlock({ ...ok, ready: false }), 'chat.new_blocked_loading');
  assert.equal(core.newSessionBlock({ ...ok, busy: true }), 'chat.new_blocked_turn');
  assert.equal(core.newSessionBlock({ ...ok, turnOpen: true }), 'chat.new_blocked_turn', 'a turn of another page');
  assert.equal(core.newSessionBlock({ ...ok, approval: true }), 'chat.new_blocked_approval');
  assert.equal(core.newSessionBlock({ ...ok, unsettled: true }), 'chat.new_blocked_unsettled', 'a send whose outcome is unknown (Stop, cut stream)');
  assert.equal(core.newSessionBlock({ ...ok, offline: true }), 'chat.new_blocked_offline');
  assert.equal(core.newSessionBlock({ ...ok, starting: true }), 'chat.new_blocked_starting');
  assert.equal(core.newSessionBlock({}), 'chat.new_blocked_loading', 'nothing known: not ready');
  assert.equal(core.newSessionBlock(), 'chat.new_blocked_loading');
});

test('starter: POST /api/chat/session/new with an Idempotency-Key and an empty JSON body', async () => {
  const { fetch, calls } = fakeFetch([json(200, { status: 'started', session: { id: 's2', reason: 'new', started_at: 1, cleared: 3 } })]);
  const starter = core.createSessionStarter({ fetch, t, makeKey });
  const r = await starter.start();
  assert.deepEqual([r.ok, r.outcome, r.session.id], [true, 'started', 's2']);
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, '/api/chat/session/new');
  assert.equal(calls[0].init.method, 'POST');
  assert.equal(calls[0].init.body, '{}');
  assert.match(calls[0].init.headers['Idempotency-Key'], /^nk-\d+$/);
  assert.equal(starter.pendingKey, null, 'a success settles the key');
});

test('starter: refusals change nothing and drop the key; the reason is the catalog sentence', async () => {
  for (const code of ['turn_running', 'approval_pending', 'resume_pending']) {
    const { fetch } = fakeFetch([json(409, { code, error: 'x' })]);
    const starter = core.createSessionStarter({ fetch, t, makeKey });
    const r = await starter.start();
    assert.deepEqual([r.ok, r.outcome, r.code], [false, 'refused', code]);
    assert.equal(r.error.message, `chat.new_refused_${code}`);
    assert.equal(starter.pendingKey, null);
    assert.ok(ja[`chat.new_refused_${code}`] && en[`chat.new_refused_${code}`], code);
  }
  for (const [status, code] of [[503, 'audit_unavailable'], [503, 'approvals_unavailable'], [400, 'invalid_body'], [403, undefined], [500, 'audit_failed'], [409, 'decision_in_progress'], [400, 'invalid_idempotency_key']]) {
    const { fetch } = fakeFetch([json(status, { code, error: 'server says' })]);
    const r = await core.createSessionStarter({ fetch, t, makeKey }).start();
    assert.equal(r.outcome, 'refused', `${status} ${code}`);
  }
});

test('starter: an unknown outcome keeps the key; the next press repeats the same request; "already" settles', async () => {
  const { fetch, calls } = fakeFetch([
    new TypeError('network'),
    json(500, { code: 'reset_failed' }),
    json(409, { code: 'duplicate_request', in_progress: true }),
    json(409, { code: 'duplicate_request', session: { id: 's7', current: false } }),
    json(200, { session: { id: 's8' } }),
  ]);
  const starter = core.createSessionStarter({ fetch, t, makeKey });
  const a = await starter.start();
  assert.deepEqual([a.ok, a.outcome], [false, 'unknown']);
  const key = starter.pendingKey;
  assert.ok(key);
  const b = await starter.start();
  assert.equal(b.outcome, 'unknown', 'a 5xx other than audit_failed is not a refusal: the reset may have happened');
  const c = await starter.start();
  assert.equal(c.outcome, 'unknown', 'the same key still in progress: not done yet');
  const d = await starter.start();
  assert.deepEqual([d.ok, d.outcome, d.session], [true, 'already', { id: 's7', current: false }]);
  assert.deepEqual(calls.map((x) => x.init.headers['Idempotency-Key']), [key, key, key, key], 'never a second, new reset');
  assert.equal(starter.pendingKey, null, 'a resolved key is finished');
  // the next new conversation is a new request (new key): an old key is never re-sent later
  await starter.start();
  assert.notEqual(calls[4].init.headers['Idempotency-Key'], key);
});

test('starter: a duplicate without the session it started is not a success', async () => {
  const { fetch } = fakeFetch([json(409, { code: 'duplicate_request' })]);
  const starter = core.createSessionStarter({ fetch, t, makeKey });
  const r = await starter.start();
  assert.deepEqual([r.ok, r.outcome], [false, 'unknown']);
  assert.ok(starter.pendingKey);
});

test('resolve: only this key\'s digest in the history settles an unknown outcome (another tab\'s reset does not)', async () => {
  const { fetch } = fakeFetch([new TypeError('network'), new TypeError('network')]);
  const starter = core.createSessionStarter({ fetch, t, makeKey: () => 'the-key' });
  await starter.start();
  const mine = await core.requestDigest('the-key');
  const crypto = require('node:crypto');
  assert.equal(mine, crypto.createHash('sha256').update('the-key').digest('hex').slice(0, 32), 'the same digest as chat_api.request_digest');
  const other = { session: { id: 'x', reason: 'new', request: await core.requestDigest('another-tab') } };
  assert.equal(await starter.resolve(other), null);
  assert.equal(starter.pendingKey, 'the-key', 'still unknown: another reset is not this one');
  assert.equal(await starter.resolve({ session: { id: 'y', reason: 'start', request: null } }), null);
  assert.equal(await starter.resolve(null), null);
  const r = await starter.resolve({ session: { id: 'z', reason: 'new', request: mine } });
  assert.deepEqual([r.ok, r.outcome, r.session], [true, 'already', { id: 'z', current: true }]);
  assert.equal(starter.pendingKey, null);
  assert.equal(await starter.resolve({ session: { id: 'z', request: mine } }), null, 'nothing pending: nothing to resolve');
});

test('starter: a second press while one runs sends nothing', async () => {
  let release;
  const fetch = () => new Promise((resolve) => { release = () => resolve(json(200, { session: { id: 's' } })); });
  const starter = core.createSessionStarter({ fetch, t, makeKey });
  const first = starter.start();
  assert.equal(starter.running, true);
  const second = await starter.start();
  assert.equal(second.ok, false);
  release();
  assert.equal((await first).ok, true);
});

test('confirmation: the agreed wording, Cancel default (shared dialog), nothing without the dialog', async () => {
  assert.equal(ja['chat.new_intro'], 'これまでの文脈を引き継ぎません。記憶・Soul・承認履歴は保持されます');
  assert.equal(await core.confirmNewSession(null, tja), false);
  assert.equal(await core.confirmNewSession({}, tja), false);
  let seen = null;
  const dialog = { open: async (o) => { seen = o; return true; } };
  assert.equal(await core.confirmNewSession(dialog, tja), true);
  assert.equal(seen.intro, ja['chat.new_intro']);
  assert.equal(seen.title, ja['chat.new_title']);
  assert.equal(seen.cancelLabel, ja['hub.cancel']);
  assert.ok(!seen.tone, 'danger tone (the default): the context is lost');
  assert.equal(await core.confirmNewSession({ open: async () => false }, tja), false);
  assert.equal(await core.confirmNewSession({ open: async () => 'yes' }, tja), false, 'only true confirms');
  assert.equal(await core.confirmNewSession({ open: async () => { throw new Error('x'); } }, tja), false);
  // the shared dialog: Cancel first and focused, held keys ignored (#357)
  const dlg = read('dashboard/static/js/confirm_dialog.js');
  assert.match(dlg, /actions\.append\(cancel, confirm\)/);
  // Cancel unless a word must be typed or a box ticked (ADR 0012 phase 2c); /new has neither.
  assert.match(dlg, /\(typed \? p\.typedInput : boxes\.length \? boxes\[0\]\.box : p\.cancel\)\.focus\(\)/);
  assert.match(dlg, /event\.repeat && \(event\.key === "Enter" \|\| event\.key === " "\)/);
});

test('history: the session marks a conversation started with /new', async () => {
  const fetch = async () => json(200, { turns: [{ message: 'a' }], session: { id: 'abc', reason: 'new', started_at: 1792240000, cleared: 2 } });
  const c = await core.fetchConversation(fetch);
  assert.deepEqual(c.session, { id: 'abc', reason: 'new', startedAt: 1792240000, cleared: 2, request: null });
  const d = await core.fetchConversation(async () => json(200, { turns: [], session: { id: 'q', reason: 'new', request: 'ab12' } }));
  assert.equal(d.session.request, 'ab12');
  assert.equal(c.turns.length, 1);
  assert.match(core.sessionNote(c.session, tja, 'ja'), /^新しい会話を始めました（\d\d:\d\d）$/);
  assert.equal(core.sessionNote({ id: 'x', reason: 'start' }, tja), null, 'a process start draws no line');
  assert.equal(core.sessionNote(null, tja), null);
  const old = await core.fetchConversation(async () => json(200, { turns: [] }));
  assert.equal(old.session, null, 'an older server: no session');
  assert.deepEqual(await core.fetchHistory(fetch), [{ message: 'a' }]);
});

test('gauge: the last model call is preferred over the turn sum (tool turns read high)', () => {
  const tool = { input_tokens: 2200, context_tokens: 1200 };
  assert.equal(core.promptTokens(tool, 'bedrock'), 1200);
  assert.equal(core.promptTokens({ input_tokens: 2200 }, 'bedrock'), 2200, 'older record: the sum');
  const last = core.contextGauge({ turn: tool, provider: 'bedrock', window: 200000, managed: false, source: 'model' });
  assert.equal(last.lastCall, true);
  const view = core.gaugeText(last, tja);
  assert.equal(view.text, 'ctx 1%');
  assert.match(view.title, /直近のモデル呼び出し 1 回の入力 1,200 \/ コンテキストウィンドウ 200,000/);
  assert.match(view.title, /自動要約はオフ/);
  assert.doesNotMatch(view.title, /合計/);
  const sum = core.gaugeText(core.contextGauge({ turn: { input_tokens: 2200 }, provider: 'bedrock', window: 200000 }), tja);
  assert.match(sum.title, /ターン内のすべてのモデル呼び出しの合計/, 'the limit is said when only the sum is known');
});

test('gauge: 80 % warns; without context management it says "off", never "soon"', () => {
  const g = (tokens, managed) => core.contextGauge({ turn: { context_tokens: tokens }, provider: 'bedrock', window: 100000, threshold: 0.85, managed, source: 'setting' });
  assert.equal(g(79999, false).level, 'ok');
  assert.equal(g(80000, false).level, 'warn');
  const off = core.gaugeText(g(90000, false), tja);
  assert.equal(off.text, 'ctx 90% · 自動要約はオフ');
  assert.doesNotMatch(off.text + off.title, /近い/);
  const onFar = core.gaugeText(g(80000, true), tja);
  assert.equal(onFar.text, 'ctx 80%', 'managed and below 95 % of the threshold: no sentence');
  assert.match(onFar.title, /自動要約: 窓の 85% に近づくと/);
  const onNear = core.gaugeText(g(81000, true), tja);
  assert.equal(onNear.text, 'ctx 81% · 自動要約が近い');
  assert.equal(core.gaugeText(g(50000, false), tja).text, 'ctx 50%', 'below 80 %: no sentence');
});

test('gauge: an unknown window falls back to the tokens; an assumed default window is said', () => {
  const none = core.contextGauge({ turn: { context_tokens: 32000 }, provider: 'bedrock' });
  assert.equal(core.gaugeText(none, tja).text, 'ctx 32.0k');
  assert.match(core.gaugeText(none, tja).title, /コンテキストウィンドウの大きさは不明/);
  const status = { provider: 'bedrock', context_management: { enabled: false }, context_window: { tokens: 200000, source: 'default', compression_threshold: 0.85 } };
  const assumed = core.contextGauge({ turn: { context_tokens: 32000 }, ...core.contextOf(status) });
  assert.match(core.gaugeText(assumed, tja).title, /既定値（200,000）を仮定/);
  assert.equal(core.contextGauge({ turn: {}, ...core.contextOf(status) }), null, 'no count: hidden');
  assert.equal(core.contextGauge({ turn: { context_tokens: 5 }, ...core.contextOf({ context_window: null }) }).ratio, null);
});

test('pages: the button and /new go through the confirmation; the page forgets only after the server reset', () => {
  const talk = read('dashboard/stage/js/talk.js');
  const startNew = talk.slice(talk.indexOf('async function startNew('), talk.indexOf('// --- wiring'));
  assert.ok(startNew.indexOf('confirmNewSession') < startNew.indexOf('starter.start()'));
  assert.ok(startNew.indexOf('starter.start()') < startNew.indexOf('showServerConversation('));
  assert.match(startNew, /if \(result\.ok\) \{/);
  assert.match(startNew, /starter\.resolve\(/, 'an unknown outcome is resolved by its own key only');
  assert.doesNotMatch(startNew, /session\.id !== before/, 'a changed session id alone never counts as success');
  const show = talk.slice(talk.indexOf('async function showServerConversation('), talk.indexOf('async function startNew('));
  assert.match(show, /await refreshHistory\(\)/, 'the page re-reads the server instead of assuming an empty conversation');
  assert.match(startNew, /const again = newBlock\(\)/, 're-checked after the dialog');
  assert.match(talk, /if \(newCommand\(core, message\)\) \{\n\s+await startNew\(\{ fromComposer: true \}\);/);
  const chat = read('dashboard/static/js/chat.js');
  const nc = chat.slice(chat.indexOf('async function newConversation('));
  assert.ok(nc.indexOf('confirmNewSession(window.StiraConfirm') < nc.indexOf('starter.start()'));
  assert.ok(nc.indexOf('starter.start()') < nc.indexOf('showServerConversation('));
  assert.match(nc, /starter\.resolve\(/);
  assert.match(chat, /if \(starter\?\.pendingKey\) \{[\s\S]{0,300}chat\.new_unknown_blocks_send/, 'no message while a reset is unknown');
  assert.match(chat, /await newConversation\(true\);/);
  assert.match(read('dashboard/templates/chat.html'), /js\/confirm_dialog\.js[\s\S]*js\/chat\.js/);
  const stage = read('dashboard/templates/stage.html');
  assert.match(stage, /id="btn-new"[^>]*disabled[^>]*aria-describedby="btn-new-reason"/);
  assert.match(read('dashboard/templates/chat.html'), /id="new-session"[^>]*disabled[^>]*aria-describedby="new-session-reason"/);
  for (const src of [talk, chat]) assert.doesNotMatch(src, /innerHTML|insertAdjacentHTML|document\.write/);
});

test('i18n: every phase-3 key exists in both catalogs with the same placeholders', () => {
  const sources = [read('dashboard/static/js/chat_core.js'), read('dashboard/static/js/chat.js'), read('dashboard/stage/js/talk.js'), read('dashboard/templates/stage.html'), read('dashboard/templates/chat.html')];
  const keys = new Set(sources.flatMap((s) => [...s.matchAll(/['"`](chat\.(?:new_|ctx_)[a-z_]*)['"`]/g)].map((m) => m[1])));
  for (const code of ['turn_running', 'approval_pending', 'resume_pending', 'approvals_unavailable', 'audit_unavailable', 'invalid_body', 'idempotency_key_required']) keys.add(`chat.new_refused_${code}`);
  keys.delete('chat.new_refused_');
  assert.ok(keys.size >= 30, [...keys].join(' '));
  const ph = (s) => [...String(s).matchAll(/\{(\w+)\}/g)].map((m) => m[1]).sort().join();
  for (const k of keys) {
    assert.ok(k in ja && k in en, k);
    assert.equal(ph(ja[k]), ph(en[k]), k);
  }
  assert.ok(!('chat.new_unavailable' in ja), 'the "not available yet" note is gone');
});

test('composer: no message while a reset is unknown (it could land in the dropped conversation)', async () => {
  const { composerState } = await import('../dashboard/stage/js/talk.js');
  const base = { ready: true, busy: false, reading: false, offline: false, approval: false, turnOpen: false, unsettled: false };
  assert.equal(composerState(base).canSend, true);
  const c = composerState({ ...base, resetUnknown: true });
  assert.deepEqual([c.canSend, c.reason], [false, 'chat.new_unknown_blocks_send']);
  const talk = read('dashboard/stage/js/talk.js');
  assert.match(talk, /resetUnknown: Boolean\(starter && starter\.pendingKey\)/);
});

test('while a new conversation starts (until its history is on screen) nothing is sent', async () => {
  const { composerState } = await import('../dashboard/stage/js/talk.js');
  const base = { ready: true, busy: false, reading: false, offline: false, approval: false, turnOpen: false, unsettled: false };
  assert.deepEqual([composerState({ ...base, starting: true }).canSend, composerState({ ...base, starting: true }).reason], [false, 'chat.new_blocked_starting']);
  const talk = read('dashboard/stage/js/talk.js');
  const startNew = talk.slice(talk.indexOf('async function startNew('), talk.indexOf('// --- wiring'));
  assert.ok(startNew.indexOf('await showServerConversation(result)') < startNew.indexOf('st.starting = false'), 'starting ends after the history is shown');
  assert.match(talk, /starting: st\.starting, historyStale: st\.historyStale, \.\.\.live/);
  const chat = read('dashboard/static/js/chat.js');
  const nc = chat.slice(chat.indexOf('async function newConversation('));
  assert.ok(nc.indexOf('await showServerConversation(result)') < nc.indexOf('starting = false'));
  // #601 E5: also off while a history re-read is on the way
  assert.match(chat, /\$\("send-button"\)\.disabled = !ready \|\| busy \|\| readingImages \|\| starting \|\| historyStale \|\| reloading > 0;/);
  assert.match(chat, /if \(!ready \|\| busy \|\| readingImages \|\| starting \|\|/);
});
