// YOLO button, badge and log line (ADR 0012 §12, phase 5b): node --test tests/yolo_ui_test.cjs
// The pure parts run everywhere; the DOM parts need jsdom (npm install --no-save jsdom axe-core).
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { resolve } = require('node:path');
const { polyfillDialog } = require('./dialog_polyfill.cjs');

let JSDOM = null;
try { ({ JSDOM } = require('jsdom')); } catch (err) { /* DOM tests skipped below */ }
const skip = JSDOM ? false : 'jsdom not installed';
const ROOT = resolve(__dirname, '..');
const SRC = readFileSync(resolve(ROOT, 'dashboard/static/js/yolo_ui.js'), 'utf8');
const CAT = {
  ja: JSON.parse(readFileSync(resolve(ROOT, 'dashboard/static/i18n/catalog.ja.json'), 'utf8')),
  en: JSON.parse(readFileSync(resolve(ROOT, 'dashboard/static/i18n/catalog.en.json'), 'utf8')),
};
const Y = require('../dashboard/static/js/yolo_ui.js');
const tr = (lang) => (key, vars) => {
  let text = Object.hasOwn(CAT[lang], key) ? CAT[lang][key] : `!${key}`;
  if (vars) text = text.replace(/\{(\w+)\}/g, (m, n) => (n in vars ? String(vars[n]) : m));
  return text;
};
const tick = () => new Promise((r) => setTimeout(r, 0));
async function settle() { for (let i = 0; i < 120; i += 1) await tick(); }

const W = 'ab'.repeat(16);
const R = 'cd'.repeat(16);
const base = (over = {}) => ({
  allowance: 'internal', offered: true, unavailable: [], clock_supported: true, window: null,
  request: null, last_end: null, latched: false, latch_reason: null, latches: [],
  host_channel: 'browser', auto_approvals: { window: null, items: [] }, ...over,
});
const win = (over = {}) => ({
  id: W.slice(0, 8), state: 'active', tier: 'internal', reason: null, end_class: null, pause_reason: null,
  t0: 1000, ends_at: 1000 + 21600, remaining_sec: 21000, cap: 2000, used: 37, reserved: 0, limit: 2000,
  auto_approved: 37, paused: false, pauses_last_hour: 0, resume_in_sec: null, effective_tier: 'internal', ...over,
});
const active = (over = {}, items = []) => base({
  offered: false, unavailable: ['yolo_active'], window: win(over), auto_approvals: { window: W.slice(0, 8), items },
});
const item = (n, ref = 'a3f2') => ({ n, tool: 'file_write', ref, limit: 2000, tier: 'internal' });

// --- pure ------------------------------------------------------------------------------

test('the log line is the ADR 0012 §12 text, ja and en', () => {
  assert.equal(Y.lineText(item(37), tr('ja')), '自動承認 (YOLO 内部): file_write #a3f2 — 37/2000');
  assert.equal(Y.lineText(item(37), tr('en')), 'Auto-approved (YOLO internal): file_write #a3f2 — 37/2000');
  // Only codes reach the line: anything else is replaced, never shown.
  assert.equal(Y.lineText({ ...item(1), tool: '<img onerror=x>', ref: 'zz' }, tr('en')),
    'Auto-approved (YOLO internal): ? #???? — 1/2000');
  assert.equal(Y.lineText({ n: -1 }, tr('en')), null);
});

test('an external tool\'s line says external and counts the sub-cap, never a destination (phase 6)', () => {
  const ext = { n: 38, tool: 'web_fetch', ref: 'b4c1', limit: 2000, tier: 'external', external_n: 5, external_limit: 200 };
  assert.equal(Y.lineText(ext, tr('ja')), '自動承認 (YOLO 外部): web_fetch #b4c1 — 38/2000 · 外部 5/200');
  assert.equal(Y.lineText(ext, tr('en')), 'Auto-approved (YOLO external): web_fetch #b4c1 — 38/2000 · external 5/200');
  // A field the server never sends is never drawn either (the line has no slot for it).
  const text = Y.lineText({ ...ext, destination: 'collector.example.org', url: 'https://x.example/' }, tr('en'));
  assert.ok(!/example/.test(text), text);
  // An odd sub-cap count falls back to the plain line rather than drawing it.
  assert.match(Y.lineText({ ...ext, external_n: -1 }, tr('en')), /^Auto-approved \(YOLO internal\): web_fetch/);
});

test('the "external tools ask again" note needs the sub-cap in the status answer (phase 6b)', () => {
  const ext = (over) => Y.viewOf(active({ tier: 'external', ...over })).ext;
  assert.equal(Y.backToAsk(ext({ external_used: 3, external_limit: 200 })), false);
  assert.equal(Y.backToAsk(ext({ external_used: 200, external_limit: 200 })), true);
  assert.equal(Y.backToAsk(ext({ external_used: 0, external_limit: 0 })), true);
  // external_limit missing (or odd): no sub-cap is known, so no "limit reached" claim.
  assert.equal(ext({ external_used: 3 }), null);
  assert.equal(Y.backToAsk(ext({ external_used: 3 })), false);
  assert.equal(Y.backToAsk(ext({ external_used: 3, external_limit: null })), false);
  assert.equal(Y.backToAsk(ext({ external_limit: 'x' })), false);
});

test('an exec tool\'s line says exec and counts the exec sub-cap, never the command (phase 7)', () => {
  const ex = { n: 39, tool: 'shell', ref: 'c0de', limit: 2000, tier: 'exec', exec_n: 1, exec_limit: 200 };
  assert.equal(Y.lineText(ex, tr('ja')), '自動承認 (YOLO 実行系): shell #c0de — 39/2000 · 実行 1/200');
  assert.equal(Y.lineText(ex, tr('en')), 'Auto-approved (YOLO exec): shell #c0de — 39/2000 · exec 1/200');
  const text = Y.lineText({ ...ex, command: 'rm -rf ~', args: { command: 'x' } }, tr('en'));
  assert.ok(!/rm -rf|command/.test(text), text);
  assert.match(Y.lineText({ ...ex, exec_n: 'x' }, tr('en')), /^Auto-approved \(YOLO internal\): shell/);
});

test('viewOf: the exec choice and the exec badge follow the status answer (phase 7)', () => {
  assert.deepEqual(Y.viewOf(base({ allowance: 'external', exec_offered: true })).tiers, ['internal', 'external', 'exec']);
  assert.deepEqual(Y.viewOf(base({ allowance: 'external', exec_offered: false })).tiers, ['internal', 'external']);
  // Never offered with allowance internal, whatever the flag says.
  assert.deepEqual(Y.viewOf(base({ allowance: 'internal', exec_offered: true })).tiers, ['internal']);
  const v = Y.viewOf(active({ tier: 'external', exec: true, exec_used: 2, exec_limit: 200, external_limit: 200 }));
  assert.equal(v.exec, true); assert.deepEqual(v.execCap, { used: 2, limit: 200 });
  assert.equal(Y.backToAsk(v.execCap), false);
  assert.equal(Y.backToAsk(Y.viewOf(active({ tier: 'external', exec: true, exec_used: 0, exec_limit: 0 })).execCap), true);
  // exec on an internal window (not a shape the server sends) or not exactly true: nothing.
  assert.equal(Y.viewOf(active({ tier: 'internal', exec: true, exec_limit: 200 })).exec, false);
  assert.equal(Y.viewOf(active({ tier: 'external', exec: 'yes', exec_limit: 200 })).execCap, null);
});

test('viewOf: every state of the status answer', () => {
  assert.equal(Y.viewOf(null).mode, 'none');
  assert.equal(Y.viewOf(null, { failed: true }).mode, 'unreadable');
  assert.deepEqual(Y.viewOf(base()).tiers, ['internal']);
  assert.deepEqual(Y.viewOf(base({ allowance: 'external' })).tiers, ['internal', 'external']);
  assert.equal(Y.viewOf(base({ offered: false, unavailable: ['clock_not_measured'] })).mode, 'unavailable');
  const pending = { id: R, short: R.slice(0, 8), state: 'pending', tier: 'internal', expires_in: 97, channel: 'browser' };
  const p = Y.viewOf(base({ offered: false, request: pending }), { own: R });
  assert.equal(p.mode, 'pending'); assert.equal(p.own, true); assert.equal(p.channel, 'browser');
  const host = Y.viewOf(base({ offered: false, request: { ...pending, channel: 'dialog' } }));
  assert.equal(host.channel, 'host'); assert.equal(host.left, 97); assert.equal(host.own, false);
  assert.equal(Y.viewOf(base({ offered: false, request: { ...pending, state: 'issuing' } })).mode, 'issuing');
  const a = Y.viewOf(active());
  assert.equal(a.mode, 'window'); assert.equal(a.paused, false); assert.equal(a.used, 37); assert.equal(a.soon, false);
  assert.equal(Y.viewOf(active({ remaining_sec: 600 })).soon, true);
  const paused = Y.viewOf(active({ state: 'paused', paused: true, pause_reason: 'breaker_warning', resume_in_sec: 420 }));
  assert.equal(paused.paused, true); assert.equal(paused.pause, 'breaker_warning'); assert.equal(paused.resumeIn, 420);
  assert.equal(Y.viewOf(active({ state: 'paused', pause_reason: 'nonsense' })).pause, 'breaker_unknown');
  // A window that ended but is still closing: Stop stays (a narrowing never hurts).
  assert.equal(Y.viewOf(active({ state: 'ended', reason: 'expired_cap' })).mode, 'issuing');
  const end = { id: W.slice(0, 8), tier: 'internal', reason: 'expired_cap', class: 'normal', at: 5 };
  const ended = Y.viewOf(base({ last_end: end }));
  assert.equal(ended.mode, 'ended'); assert.equal(ended.ended.reason, 'expired_cap');
  assert.equal(Y.viewOf(base({ last_end: end }), { acked: `${W.slice(0, 8)}:5` }).mode, 'offered');
  assert.equal(Y.viewOf(base({ last_end: { ...end, reason: 'evil<b>' } })).ended.reason, 'unknown');
  const latched = Y.viewOf(base({ offered: false, latched: true, latches: [{ subject: 'window', window_id: W, reason: 'revoked_clock_anomaly' }] }));
  assert.equal(latched.mode, 'latched'); assert.equal(latched.windowId, W); assert.equal(latched.subject, 'window');
  const files = Y.viewOf(base({ offered: false, latched: true, latches: [{ subject: 'unreadable', window_id: null, reason: 'marker_malformed' }] }));
  assert.equal(files.windowId, null); assert.equal(files.subject, 'unreadable');
});

// #594: after an abnormal end the machine keeps the window as ``ended`` (ADR 0012 §11) and
// latches; the badge drew "Starting…" for good (no Unlatch, no start) until a restart.
const abnormal = (over = {}) => base({
  offered: false, unavailable: ['yolo_latched'], latched: true,
  latches: [{ subject: 'window', window_id: W, reason: 'revoked_clock_anomaly' }],
  window: win({ state: 'ended', reason: 'revoked_clock_anomaly', end_class: 'abnormal', remaining_sec: 0 }),
  last_end: { id: W.slice(0, 8), tier: 'internal', reason: 'revoked_clock_anomaly', class: 'abnormal', at: 9 },
  ...over,
});

test('viewOf #594: latched with the ended window still there -> latched, not issuing', () => {
  const v = Y.viewOf(abnormal());
  assert.equal(v.mode, 'latched'); assert.equal(v.windowId, W); assert.equal(v.subject, 'window');
  assert.equal(v.reason, 'revoked_clock_anomaly');
  // Still closing (not latched yet): the window that ended keeps Stop, as before.
  assert.equal(Y.viewOf(abnormal({ latched: false, latches: [], unavailable: [] })).mode, 'issuing');
  // A request still being issued keeps "Starting…" (never a narrowing to hide).
  const issuing = { id: R, short: R.slice(0, 8), state: 'issuing', tier: 'internal', expires_in: 5, channel: 'browser' };
  assert.equal(Y.viewOf(abnormal({ latched: false, latches: [], request: issuing })).mode, 'issuing');
  // The window is null after Unlatch (state.unlatch): the start button, no restart.
  const after = Y.viewOf(abnormal({ latched: false, latches: [], window: null, offered: true, unavailable: [] }), { acked: `${W.slice(0, 8)}:9` });
  assert.equal(after.mode, 'offered');
  assert.equal(Y.viewOf(abnormal({ latched: false, latches: [], window: null, offered: true, unavailable: [] })).mode, 'ended');
});

test('remaining time: the unit is written out, the same on both sides of an hour', () => {
  // Phase 5b showed "6:00" (hours:minutes or minutes:seconds?) and 3599 s as "60 min".
  assert.equal(Y.remainingText(21600, tr('ja')), '残り 6時間');
  assert.equal(Y.remainingText(21000, tr('ja')), '残り 5時間 50分');
  assert.equal(Y.remainingText(720, tr('ja')), '残り 12分');
  assert.equal(Y.remainingText(21600, tr('en')), '6 h left');
  assert.equal(Y.remainingText(21000, tr('en')), '5 h 50 min left');
  assert.equal(Y.remainingText(3661, tr('en')), '1 h 2 min left');
  assert.equal(Y.remainingText(3600, tr('en')), '1 h left');
  assert.equal(Y.remainingText(3599, tr('en')), '1 h left', 'rounded up like every other value: not "60 min"');
  assert.equal(Y.remainingText(3540, tr('en')), '59 min left');
  assert.equal(Y.remainingText(61, tr('en')), '2 min left');
  assert.equal(Y.remainingText(1, tr('ja')), '残り 1分');
  assert.equal(Y.remainingText(-1, tr('en')), '');
});

test('every key the badge can use exists in ja and en', () => {
  const keys = new Set();
  for (const m of SRC.matchAll(/\btr\("((?:yolo|host)\.[a-z_]+)"/g)) keys.add(m[1]);
  for (const m of SRC.matchAll(/setNotice\("((?:yolo|host)\.[a-z_]+)"/g)) keys.add(m[1]);
  keys.add('yolo.auto_approved_line');
  keys.add('yolo.auto_approved_line_external');
  keys.add('yolo.auto_approved_line_exec');
  const ends = ['expired_time', 'expired_sleep', 'expired_cap', 'revoked_stop', 'revoked_new_session',
    'revoked_flapping', 'issue_aborted_stop', 'issue_aborted_new_session', 'issue_aborted_cancel',
    'issue_aborted_epoch', 'revoked_audit_failed', 'revoked_clock_anomaly', 'aborted_issue_failed', 'unknown'];
  for (const e of ends) keys.add(`yolo.end_${e}`);
  for (const p of ['warning', 'coherence', 'critical', 'unknown']) keys.add(`yolo.pause_breaker_${p}`);
  for (const t of ['internal', 'external', 'exec']) keys.add(`yolo.tier_${t}`);
  for (const s of ['window', 'orphan_marker', 'unreadable']) keys.add(`yolo.latch_${s}`);
  for (const u of ['clock_not_measured', 'host_channel_unavailable', 'other']) keys.add(`yolo.unavailable_${u}`);
  for (const c of ['yolo_rate_limited', 'yolo_active', 'yolo_latched', 'yolo_request_pending', 'yolo_tier_not_allowed',
    'yolo_clock_failed', 'yolo_audit_failed', 'yolo_busy', 'yolo_closing', 'yolo_not_allowed', 'clock_not_measured',
    'other', 'stale_unlatch', 'yolo_not_latched', 'tool_call_running', 'yolo_exec_not_allowed']) keys.add(`yolo.code_${c}`);
  for (const k of ['yolo.text_internal_host', 'yolo.text_internal_browser', 'yolo.pending_host', 'yolo.pending_command',
    'yolo.remaining_h', 'yolo.remaining_hm', 'yolo.remaining_min', 'yolo.lines_skipped', 'yolo.lines_before',
    'host.code_tool_call_running']) keys.add(k);
  const missing = [...keys].filter((k) => !Object.hasOwn(CAT.ja, k) || !Object.hasOwn(CAT.en, k));
  assert.deepEqual(missing, []);
  // The §12 wording is the ADR's, word for word.
  assert.equal(CAT.ja['yolo.text_internal_browser'], 'YOLO を開始します。押した時点から6時間、選んだ範囲で『確認』が必要な操作を確認なしで実行します。『しない』の操作、保護された場所への書込み、CRITICAL 時の操作は変わりません。この画面でもう一度確認します。いつでも停止できます。');
  assert.equal(CAT.en['yolo.text_internal_host'], 'Start YOLO. For six hours from now, operations in the chosen range that would ask you run without asking. Never rules, protected locations and operations at CRITICAL do not change. You confirm once more on the host. You can stop at any time.');
  assert.equal(CAT.en['yolo.start'], 'Start YOLO');
  assert.equal(CAT.ja['yolo.start'], 'YOLO を開始');
  assert.equal(CAT.ja['yolo.pause_breaker_warning'], '一時停止中 (WARNING)');
  assert.equal(CAT.en['yolo.pause_breaker_warning'], 'Paused (WARNING)');
});

// --- DOM -------------------------------------------------------------------------------

function page({ statuses, posts = {}, body = '', lang = 'en' } = {}) {
  const dom = new JSDOM(`<!doctype html><html lang="${lang}"><body><header><div id="slot" class="yolo-slot" hidden></div></header>
    <div id="conversation" role="log"></div>${body}</body></html>`, { runScripts: 'outside-only', pretendToBeVisual: true });
  polyfillDialog(dom.window);
  const w = dom.window;
  const log = [];
  const queue = Array.isArray(statuses) ? statuses.slice() : [];
  let last = queue[0];
  const timers = [];
  const env = {
    w, log, timers,
    set next(s) { queue.push(s); },
    fetch: async (url) => {
      log.push(['GET', url]);
      if (statuses === 404) return { status: 404, ok: false, json: async () => ({}) };
      if (statuses === 'fail') throw new Error('down');
      if (queue.length) last = queue.shift();
      return { status: 200, ok: true, json: async () => JSON.parse(JSON.stringify(last)) };
    },
    post: async (url, body) => {
      log.push(['POST', url, body]);
      const answer = posts[url];
      const value = typeof answer === 'function' ? answer(body) : answer;
      if (value instanceof Error) throw value;
      const [status, data] = value || [200, {}];
      return { status, ok: status >= 200 && status < 300, json: async () => data };
    },
    setTimer: (fn, ms) => { timers.push({ fn, ms }); return timers.length; },
    clearTimer: () => {},
  };
  return env;
}
function make(env, extra = {}) {
  const doc = env.w.document;
  const badge = Y.create({
    doc, win: env.w, t: tr(doc.documentElement.lang), fetch: env.fetch, post: env.post,
    setTimer: env.setTimer, clearTimer: env.clearTimer, storage: null,
    slots: [doc.getElementById('slot')], ...extra,
  });
  return badge;
}
const $ = (env, sel) => env.w.document.querySelector(sel);

test('without the allowance (404) nothing is drawn and nothing is polled', { skip }, async () => {
  const env = page({ statuses: 404 });
  await make(env).start();
  assert.equal($(env, '#slot').hidden, true);
  assert.equal($(env, '#slot').children.length, 0);
  assert.equal(env.timers.length, 0);
});

test('idle (the button) is read once and not polled; a window is polled', { skip }, async () => {
  const env = page({ statuses: [base()] });
  await make(env).start();
  assert.equal($(env, '.yolo-start').textContent, 'Start YOLO');
  assert.equal($(env, '#slot').hidden, false);
  assert.equal(env.timers.length, 0, 'no poll while nothing is current');
  const env2 = page({ statuses: [active()] });
  await make(env2).start();
  assert.equal(env2.timers.length, 1);
  assert.equal(env2.timers[0].ms, 3000);
});

test('the press: tier dialog, request, same-page confirmation, then the badge', { skip }, async () => {
  const pending = { id: R, short: R.slice(0, 8), state: 'pending', tier: 'internal', expires_in: 120, channel: 'browser', ends_at: 22600 };
  const env = page({
    statuses: [base()],
    posts: { '/api/yolo/request': [202, { request: pending, confirm: { token: 'ef'.repeat(16), external: false, instance: '0123456789abcdef' } }] },
  });
  const seen = [];
  const badge = make(env, {
    chooseTier: async (tiers, channel) => { seen.push([tiers, channel]); return 'internal'; },
    confirmYolo: async (answer) => {
      seen.push(answer.request.id);
      await settle();
      // While the page's own dialog is open the badge says "waiting" and offers Stop.
      seen.push(badge.view().mode, Boolean($(env, '.yolo-stop')));
      env.next = active({ used: 0, auto_approved: 0 });
      return 'confirmed';
    },
  });
  env.next = base({ offered: false, unavailable: ['yolo_request_pending'], request: pending });
  await badge.start();
  $(env, '.yolo-start').focus();
  $(env, '.yolo-start').click();
  for (let i = 0; i < 1000 && seen.length < 4; i += 1) await tick();
  await settle();
  assert.deepEqual(seen[0], [['internal'], 'browser']);
  assert.equal(seen[1], R);
  assert.deepEqual(seen.slice(2), ['pending', true]);
  assert.deepEqual(env.log.find((l) => l[0] === 'POST'), ['POST', '/api/yolo/request', { tier: 'internal' }]);
  assert.equal($(env, '.yolo-badge').dataset.mode, 'window');
  assert.equal($(env, '.yolo-tag').textContent, 'YOLO Internal only');
  assert.equal($(env, '.yolo-count').textContent, 'Auto-approved 0/2000');
  assert.equal(env.w.document.activeElement, $(env, '.yolo-stop'), 'focus moves to Stop');
});

test('cancelling the tier dialog sends nothing; a refused press says why', { skip }, async () => {
  const env = page({ statuses: [base()], posts: { '/api/yolo/request': [429, { code: 'yolo_rate_limited' }] } });
  let answer = null;
  const badge = make(env, { chooseTier: async () => answer });
  await badge.start();
  $(env, '.yolo-start').click();
  await settle();
  assert.equal(env.log.filter((l) => l[0] === 'POST').length, 0);
  answer = 'internal';
  $(env, '.yolo-start').click();
  await settle();
  assert.match($(env, '.yolo-notice').textContent, /Too many requests/);
  assert.equal($(env, '.yolo-notice').hidden, false);
});

test('a press refused while an agent tool call runs says to press again (ja/en)', { skip }, async () => {
  for (const [lang, text] of [['en', 'An agent tool call is running; press again when it has finished'],
    ['ja', 'エージェントのツール呼び出しが実行中です。終わってからもう一度押してください']]) {
    const env = page({ lang, statuses: [base()], posts: { '/api/yolo/request': [409, { code: 'tool_call_running' }] } });
    await make(env, { chooseTier: async () => 'internal' }).start();
    $(env, '.yolo-start').click();
    await settle();
    assert.equal($(env, '.yolo-notice').textContent, text);
    assert.equal($(env, '.yolo-notice').hidden, false);
    assert.ok($(env, '.yolo-start'), 'the button is still offered');
  }
});

test('Stop is sent at once in every current state, also when the status cannot be read', { skip }, async () => {
  for (const status of [active(), active({ state: 'paused', paused: true, pause_reason: 'breaker_warning' }),
    base({ offered: false, request: { id: R, short: R.slice(0, 8), state: 'pending', channel: 'dialog', expires_in: 50 } }),
    base({ offered: false, request: { id: R, short: R.slice(0, 8), state: 'issuing', channel: 'browser' } })]) {
    const env = page({ statuses: [status], posts: { '/api/yolo/stop': [200, base({ last_end: { id: 'x', reason: 'revoked_stop', class: 'normal', at: 1 } })] } });
    await make(env).start();
    const reads = env.log.length;
    $(env, '.yolo-stop').click();
    assert.deepEqual(env.log[reads], ['POST', '/api/yolo/stop', {}], 'sent before anything else');
    await settle();
    assert.equal($(env, '.yolo-badge').dataset.mode, 'ended');
    assert.equal($(env, '.yolo-ended').textContent, `YOLO: ${CAT.en['yolo.end_revoked_stop']}`);
    // the YOLO stop closes the window only; the text says how to stop the turn (#452)
    assert.match($(env, '.yolo-ended').textContent, /running turn goes on.*■ Stop/);
  }
  const down = page({ statuses: 'fail', posts: { '/api/yolo/stop': new Error('offline') } });
  await make(down).start();
  assert.equal($(down, '.yolo-badge').dataset.mode, 'unreadable');
  $(down, '.yolo-stop').click();
  await settle();
  assert.equal(down.log.filter((l) => l[1] === '/api/yolo/stop').length, 1);
  assert.match($(down, '.yolo-notice').textContent, /press it again/);
  assert.ok($(down, '.yolo-stop'), 'Stop stays');
});

test('the badge follows the window: paused, 10 minutes left, the end reason until OK', { skip }, async () => {
  const env = page({ statuses: [active()] });
  const badge = make(env);
  await badge.start();
  assert.equal($(env, '.yolo-time').textContent, '5 h 50 min left');
  env.next = active({ state: 'paused', paused: true, pause_reason: 'breaker_warning', resume_in_sec: 30 });
  await badge.read();
  assert.equal($(env, '.yolo-pause').textContent, 'Paused (WARNING) · Resumes after 30 s more at NORMAL');
  assert.equal($(env, '.yolo-live').textContent, 'Paused (WARNING)');
  env.next = active({ remaining_sec: 540 });
  await badge.read();
  assert.equal($(env, '.yolo-soon').textContent, 'Ends in 9 min (cannot be extended)');
  for (const [reason, text] of [['expired_time', 'six hours'], ['expired_cap', 'cap'], ['revoked_new_session', 'new conversation'],
    ['revoked_flapping', 'paused too often'], ['revoked_stop', 'Stopped']]) {
    env.next = base({ last_end: { id: W.slice(0, 8), reason, class: 'normal', at: reason.length } });
    await badge.read();
    assert.equal($(env, '.yolo-badge').dataset.mode, 'ended');
    assert.match($(env, '.yolo-ended').textContent, new RegExp(text));
  }
  $(env, '.yolo-ack').click();
  assert.equal($(env, '.yolo-badge').dataset.mode, 'offered');
});

test('re-reading the same status changes no node; a new time changes only its text', { skip }, async () => {
  const env = page({ statuses: [active()] });
  const badge = make(env);
  await badge.start();
  const box = $(env, '.yolo-badge');
  const stop = $(env, '.yolo-stop');
  const time = $(env, '.yolo-time');
  const textNode = time.firstChild;
  let mutations = 0;
  const observer = new env.w.MutationObserver((list) => { mutations += list.length; });
  observer.observe($(env, '#slot'), { subtree: true, childList: true, characterData: true, attributes: true });
  env.next = active();
  await badge.read();
  await settle();
  assert.equal(mutations, 0, 'the same answer touches nothing');
  env.next = active({ remaining_sec: 20000 });
  await badge.read();
  await settle();
  assert.equal($(env, '.yolo-badge'), box);
  assert.equal($(env, '.yolo-stop'), stop);
  assert.equal($(env, '.yolo-time'), time);
  assert.notEqual(time.firstChild, textNode);
  assert.equal(time.textContent, '5 h 34 min left');
  observer.disconnect();
});

test('log lines: new automatic approvals only, once each, in the chat log and as an event', { skip }, async () => {
  const env = page({ statuses: [active({}, [item(1, 'aaaa')])] });
  const events = [];
  env.w.document.addEventListener('stira:yolo-line', (e) => events.push(e.detail.text));
  const badge = make(env);
  await badge.start();
  const texts = () => [...env.w.document.querySelectorAll('#conversation .yolo-line')].map((n) => n.textContent);
  // Lines from before the page loaded are not repeated: one summary line says how many.
  assert.deepEqual(texts(), ['Auto-approved before this page was opened: 1 (see the badge)']);
  env.next = active({ used: 3 }, [item(1, 'aaaa'), item(2, 'bbbb'), item(3, 'cccc')]);
  await badge.read();
  env.next = active({ used: 3 }, [item(1, 'aaaa'), item(2, 'bbbb'), item(3, 'cccc')]);
  await badge.read();
  const lines = texts();
  assert.deepEqual(lines.slice(1), [
    'Auto-approved (YOLO internal): file_write #bbbb — 2/2000',
    'Auto-approved (YOLO internal): file_write #cccc — 3/2000',
  ]);
  assert.deepEqual(events, lines);
  // A new window starts counting again.
  env.next = { ...active({ id: 'ffffffff' }, [item(1, 'dddd')]), auto_approvals: { window: 'ffffffff', items: [item(1, 'dddd')] } };
  await badge.read();
  assert.equal(env.w.document.querySelectorAll('.yolo-line').length, 4);
});

test('log lines: no summary line when the page opens with nothing approved yet', { skip }, async () => {
  const env = page({ statuses: [active({ used: 0 }, [])] });
  await make(env).start();
  assert.equal(env.w.document.querySelectorAll('.yolo-line').length, 0);
  const idle = page({ statuses: [base({ auto_approvals: { window: 'eeeeeeee', items: [item(4)] } })] });
  await make(idle).start();
  assert.equal(idle.w.document.querySelectorAll('.yolo-line').length, 0, 'no current window: nothing to sum up');
});

test('log lines: more than 20 approvals between two reads -> one "N more" line, then the 20', { skip }, async () => {
  const env = page({ statuses: [active({ used: 0 }, [])], lang: 'ja' });
  const badge = make(env);
  await badge.start();
  const range = (a, b) => Array.from({ length: b - a + 1 }, (_, i) => item(a + i));
  env.next = active({ used: 3 }, range(1, 3));
  await badge.read();
  // The server keeps the latest 20 (RECENT_LINES): 4..25 happened, 6..25 are listed.
  env.next = active({ used: 25 }, range(6, 25));
  await badge.read();
  const lines = [...env.w.document.querySelectorAll('#conversation .yolo-line')].map((n) => n.textContent);
  assert.equal(lines.length, 3 + 1 + 20);
  assert.equal(lines[3], '…ほか 2 件の自動承認 (件数はバッジを参照)');
  assert.equal(lines[4], '自動承認 (YOLO 内部): file_write #a3f2 — 6/2000');
  assert.ok(env.w.document.querySelectorAll('.yolo-line-summary').length === 1);
});

test('polling widens to 15 s while only the clock moves (remaining_sec changes every read)', { skip }, async () => {
  let sec = 21000;
  const env = page({ statuses: [active({ remaining_sec: sec })] });
  const badge = make(env);
  await badge.start();
  assert.equal(env.timers.at(-1).ms, 3000);
  for (let i = 0; i < 12; i += 1) {
    sec -= 3;
    env.next = active({ remaining_sec: sec });
    await badge.read();
  }
  assert.equal(env.timers.at(-1).ms, 15000, 'the same window state with a new time is not a change');
  const paused = (s) => active({ state: 'paused', paused: true, pause_reason: 'breaker_warning', resume_in_sec: s, remaining_sec: s + 9000 });
  for (let i = 0; i < 12; i += 1) { env.next = paused(500 - i); await badge.read(); }
  assert.equal(env.timers.at(-1).ms, 15000, 'resume_in_sec counting down is not a change either');
  // A real change (a new approval) polls quickly again.
  env.next = active({ remaining_sec: sec - 30, used: 38 }, [item(38)]);
  await badge.read();
  assert.equal(env.timers.at(-1).ms, 3000);
});

test('a read sent before Stop does not bring the window back after Stop answered', { skip }, async () => {
  const env = page({
    statuses: [active()],
    posts: { '/api/yolo/stop': [200, base({ last_end: { id: W.slice(0, 8), reason: 'revoked_stop', class: 'normal', at: 9 } })] },
  });
  let release = null;
  let held = 0;
  const fetch = async (url) => {
    held += 1;
    if (held === 1) return env.fetch(url); // start(): answered at once
    // The second read is slow: it was answered (active) before Stop, it arrives after.
    const stale = await env.fetch(url);
    await new Promise((r) => { release = r; });
    return stale;
  };
  const badge = make(env, { fetch });
  await badge.start();
  env.next = active();
  const slow = badge.read();
  await settle();
  $(env, '.yolo-stop').click();
  await settle();
  assert.equal($(env, '.yolo-badge').dataset.mode, 'ended');
  release();
  await slow;
  await settle();
  assert.equal($(env, '.yolo-badge').dataset.mode, 'ended', 'the stale active answer is dropped');
  assert.equal($(env, '.yolo-ended').textContent, `YOLO: ${CAT.en['yolo.end_revoked_stop']}`);
});

// Stop's POST is held; a read sent after it gets its headers (active) before Stop answers.
function stopRace(env, { bodyHeld }) {
  const ended = base({ last_end: { id: W.slice(0, 8), reason: 'revoked_stop', class: 'normal', at: 9 } });
  let answerStop = null, releaseBody = null, reads = 0;
  const post = async (url, body) => {
    env.log.push(['POST', url, body]);
    await new Promise((r) => { answerStop = r; });
    return { status: 200, ok: true, json: async () => JSON.parse(JSON.stringify(ended)) };
  };
  const fetch = async (url) => {
    reads += 1;
    const answer = await env.fetch(url);
    if (reads === 1 || !bodyHeld) return answer; // start(); or a read whose body comes at once
    return { ...answer, json: async () => { await new Promise((r) => { releaseBody = r; }); return answer.json(); } };
  };
  return { post, fetch, answerStop: () => answerStop(), releaseBody: () => releaseBody() };
}

test('Stop wins over a later read whose headers came first and whose body is still on the way', { skip }, async () => {
  const env = page({ statuses: [active()] });
  const race = stopRace(env, { bodyHeld: true });
  const badge = make(env, { fetch: race.fetch, post: race.post });
  await badge.start();
  $(env, '.yolo-stop').click(); // Stop sent (held)
  await settle();
  env.next = active();
  const slow = badge.read(); // sent after Stop: the headers (active) arrive, the body is held
  await settle();
  race.answerStop(); // Stop succeeds: the window has ended
  await settle();
  assert.equal($(env, '.yolo-badge').dataset.mode, 'ended', 'drawn without waiting for the read body');
  assert.equal($(env, '.yolo-ended').textContent, `YOLO: ${CAT.en['yolo.end_revoked_stop']}`);
  race.releaseBody(); // the read's body (active, answered before Stop) comes last
  await slow;
  await settle();
  assert.equal($(env, '.yolo-badge').dataset.mode, 'ended', 'the read answered before Stop is dropped');
});

test('Stop wins over a later read that was answered and drawn before Stop answered', { skip }, async () => {
  const env = page({ statuses: [active()] });
  const race = stopRace(env, { bodyHeld: false });
  const badge = make(env, { fetch: race.fetch, post: race.post });
  await badge.start();
  $(env, '.yolo-stop').click();
  await settle();
  env.next = active();
  await badge.read(); // sent after Stop, fully answered (active) while Stop is still running
  assert.equal($(env, '.yolo-badge').dataset.mode, 'window');
  race.answerStop();
  await settle();
  assert.equal($(env, '.yolo-badge').dataset.mode, 'ended', 'Stop answered after it: Stop is the newer state');
  // A read sent after Stop's answer is a real new state again (no window: the button).
  env.next = base();
  await badge.read();
  assert.equal($(env, '.yolo-badge').dataset.mode, 'offered');
});

test('a host channel request: the countdown and a cancel that narrows', { skip }, async () => {
  const req = { id: R, short: R.slice(0, 8), state: 'pending', tier: 'internal', expires_in: 88, channel: 'command' };
  const env = page({ statuses: [base({ offered: false, host_channel: 'command', request: req })], posts: { '/api/yolo/request/cancel': [200, {}] } });
  const badge = make(env);
  await badge.start();
  assert.match($(env, '.yolo-text').textContent, /Confirm on the host.*cdcdcdcd.*88 s/);
  assert.equal(env.timers.at(-1).ms, 1000, 'the countdown is read every second');
  env.next = base();
  $(env, '.yolo-cancel').click();
  await settle();
  assert.deepEqual(env.log.find((l) => l[1] === '/api/yolo/request/cancel'), ['POST', '/api/yolo/request/cancel', { request_id: R }]);
  assert.equal($(env, '.yolo-badge').dataset.mode, 'offered');
});

test('latched: Unlatch posts the window id (the 428 confirmation is stiraFetch\'s); files: no button', { skip }, async () => {
  const latched = base({ offered: false, unavailable: ['yolo_latched'], latched: true, latches: [{ subject: 'window', window_id: W, reason: 'revoked_clock_anomaly' }] });
  const env = page({ statuses: [latched], posts: { '/api/yolo/unlatch': [200, { unlatched: true }] } });
  const badge = make(env);
  await badge.start();
  assert.match($(env, '.yolo-text').textContent, /did not end cleanly/);
  assert.match($(env, '.yolo-reason').textContent, /revoked_clock_anomaly/);
  env.next = base();
  $(env, '.yolo-unlatch').click();
  await settle();
  assert.deepEqual(env.log.find((l) => l[1] === '/api/yolo/unlatch'), ['POST', '/api/yolo/unlatch', { window_id: W }]);
  assert.equal($(env, '.yolo-badge').dataset.mode, 'offered');
  assert.match($(env, '.yolo-notice').textContent, /Unlatched/);
  const files = page({ statuses: [base({ offered: false, latched: true, latches: [{ subject: 'unreadable', window_id: null, reason: 'marker_malformed' }] })] });
  await make(files).start();
  assert.equal($(files, '.yolo-unlatch'), null);
  assert.match($(files, '.yolo-badge').textContent, /yolo-quickstart/);
});

test('#594: an abnormal end with the ended window still there shows Unlatch; after it the start button', { skip }, async () => {
  const env = page({ statuses: [abnormal()], posts: { '/api/yolo/unlatch': [200, { unlatched: true }] } });
  const badge = make(env);
  await badge.start();
  assert.equal($(env, '.yolo-badge').dataset.mode, 'latched');
  assert.equal($(env, '.yolo-stop'), null, 'not "Starting…" with Stop');
  assert.match($(env, '.yolo-reason').textContent, /revoked_clock_anomaly/);
  // What the server answers after Unlatch: no window, not latched, offered again.
  env.next = base({ last_end: { id: W.slice(0, 8), tier: 'internal', reason: 'revoked_clock_anomaly', class: 'abnormal', at: 9 } });
  $(env, '.yolo-unlatch').click();
  await settle();
  assert.deepEqual(env.log.find((l) => l[1] === '/api/yolo/unlatch'), ['POST', '/api/yolo/unlatch', { window_id: W }]);
  assert.equal($(env, '.yolo-badge').dataset.mode, 'offered', 'the end shown with Unlatch is acknowledged by it');
  assert.ok($(env, '.yolo-start'), 'the start button is back without a restart');
  assert.match($(env, '.yolo-notice').textContent, /Unlatched/);
  // A refused Unlatch acknowledges nothing: the latch (and its end) stay on screen.
  const refused = page({ statuses: [abnormal()], posts: { '/api/yolo/unlatch': [409, { code: 'stale_operation' }] } });
  await make(refused).start();
  refused.next = abnormal();
  $(refused, '.yolo-unlatch').click();
  await settle();
  assert.equal($(refused, '.yolo-badge').dataset.mode, 'latched');
  assert.ok($(refused, '.yolo-ended'), 'the end line is still shown');
});

test('the tier dialog: §12 text, keyboard only, external only when allowed', { skip }, async () => {
  const env = page({ statuses: [base({ allowance: 'external' })], lang: 'ja' });
  const doc = env.w.document;
  const posts = [];
  const badge = make(env, { post: async (url, body) => { posts.push([url, body]); return { status: 409, ok: false, json: async () => ({ code: 'yolo_busy' }) }; } });
  await badge.start();
  const start = $(env, '.yolo-start');
  start.focus();
  start.click();
  await settle();
  const dialog = doc.getElementById('yolo-start-dialog');
  assert.equal(dialog.open, true);
  assert.match(doc.getElementById('yolo-start-text').textContent, /この画面でもう一度確認します/);
  const radios = [...dialog.querySelectorAll('input[type=radio]')];
  assert.deepEqual(radios.map((r) => r.value), ['internal', 'external']);
  assert.equal(doc.activeElement, radios[0]);
  assert.equal(doc.getElementById('yolo-start-external').hidden, true);
  radios[1].checked = true;
  radios[1].dispatchEvent(new env.w.Event('change', { bubbles: true }));
  assert.equal(doc.getElementById('yolo-start-external').hidden, false);
  assert.match(doc.getElementById('yolo-start-external').textContent, /外部への送信も確認なしで実行されます/);
  // Enter on a choice does nothing (it would submit Cancel).
  const enter = new env.w.KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true });
  radios[1].dispatchEvent(enter);
  assert.equal(enter.defaultPrevented, true);
  // Cancel is the first submit button (Enter's default), Esc cancels.
  assert.equal(dialog.querySelector('button[type=submit]').value, 'cancel');
  doc.dispatchEvent(new env.w.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
  await settle();
  assert.equal(posts.length, 0);
  assert.equal(doc.activeElement, $(env, '.yolo-start'), 'focus returns to the button');
  // Next with external chosen -> the request names external.
  $(env, '.yolo-start').click();
  await settle();
  const again = [...doc.querySelectorAll('#yolo-start-dialog input[type=radio]')];
  again[1].checked = true;
  doc.getElementById('yolo-start-next').click();
  await settle();
  assert.deepEqual(posts, [['/api/yolo/request', { tier: 'external' }]]);
  // Only internal allowed: one choice.
  const env2 = page({ statuses: [base()], lang: 'en' });
  const b2 = make(env2);
  await b2.start();
  $(env2, '.yolo-start').click();
  await settle();
  assert.deepEqual([...env2.w.document.querySelectorAll('#yolo-start-dialog input[type=radio]')].map((r) => r.value), ['internal']);
  env2.w.document.getElementById('yolo-start-cancel').click(); // one tier dialog per page set
  await settle();
});

test('ADR 0014: the tier dialog offers the progress-report tick (with the report panel loaded)', { skip }, async () => {
  const env = page({ statuses: [base()], lang: 'ja' });
  const doc = env.w.document;
  env.w.StiraProgress = { defaultOn: false, intervalMin: 30, continueAfterMin: 10, maxAuto: 3 };
  const posts = [];
  const badge = make(env, { post: async (url, body) => { posts.push([url, body]); return { status: 409, ok: false, json: async () => ({ code: 'yolo_busy' }) }; } });
  await badge.start();
  $(env, '.yolo-start').click();
  await settle();
  const box = doc.getElementById('yolo-progress');
  assert.equal(box.checked, false, 'off unless the settings say on');
  assert.match(box.closest('label').textContent, /30 分ごとに中間報告する/);
  assert.match(doc.getElementById('yolo-progress-help').textContent, /10 分返答がなければ自動で続けます（連続 3 回まで）/);
  box.checked = true;
  doc.getElementById('yolo-start-next').click();
  await settle();
  assert.deepEqual(posts, [['/api/yolo/request', { tier: 'internal', progress: true }]]);
  // The settings' default ticks it; unticked, the press says false.
  env.w.StiraProgress.defaultOn = true;
  $(env, '.yolo-start').click();
  await settle();
  assert.equal(doc.getElementById('yolo-progress').checked, true);
  doc.getElementById('yolo-progress').checked = false;
  doc.getElementById('yolo-start-next').click();
  await settle();
  assert.deepEqual(posts[1], ['/api/yolo/request', { tier: 'internal', progress: false }]);
});

test('an external window shows its sub-cap; used up, external tools ask again (phase 6)', { skip }, async () => {
  const open = { tier: 'external', external_used: 3, external_limit: 200, effective_tier: 'external' };
  const used = { ...open, external_used: 200, effective_tier: 'internal' };
  const env = page({ statuses: [active(open), active(used)] });
  const b = make(env);
  await b.start();
  assert.equal($(env, '.yolo-tag').textContent, 'YOLO External allowed');
  assert.equal($(env, '.yolo-ext').textContent, 'External 3/200');
  assert.equal($(env, '.yolo-note').hidden, true);
  await b.read();
  await settle();
  assert.equal($(env, '.yolo-ext').textContent, 'External 200/200');
  assert.equal($(env, '.yolo-note').hidden, false);
  assert.equal($(env, '.yolo-note').textContent, 'External tools ask you again (the external limit is reached)');
  // ja, and a sub-cap of 0: external tools ask from the start.
  const ja = page({ statuses: [active({ tier: 'external', external_used: 0, external_limit: 0 })], lang: 'ja' });
  await make(ja).start();
  assert.equal($(ja, '.yolo-ext').textContent, '外部 0/0');
  assert.equal($(ja, '.yolo-note').textContent, '外部ツールは確認に戻りました (外部の上限に達しました)');
  // external_limit missing from the answer: no counter, and no "limit reached" note.
  const bare = page({ statuses: [active({ tier: 'external', external_used: 3 })] });
  await make(bare).start();
  assert.equal($(bare, '.yolo-ext'), null);
  assert.equal($(bare, '.yolo-note').hidden, true);
  assert.equal($(bare, '.yolo-note').textContent, '');
  // A window of tier internal has neither.
  const internal = page({ statuses: [active()] });
  await make(internal).start();
  assert.equal($(internal, '.yolo-ext'), null);
  assert.equal($(internal, '.yolo-note'), null);
});

test('the tier dialog offers "Include exec" only when offered; it sends exec: true (phase 7)', { skip }, async () => {
  const env = page({ statuses: [base({ allowance: 'external', exec_offered: true })], lang: 'ja' });
  const doc = env.w.document;
  const posts = [];
  const badge = make(env, { post: async (url, body) => { posts.push([url, body]); return { status: 409, ok: false, json: async () => ({ code: 'yolo_exec_not_allowed' }) }; } });
  await badge.start();
  $(env, '.yolo-start').click();
  await settle();
  const dialog = doc.getElementById('yolo-start-dialog');
  const radios = [...dialog.querySelectorAll('input[type=radio]')];
  assert.deepEqual(radios.map((r) => r.value), ['internal', 'external', 'exec']);
  assert.equal(radios[0].checked, true, 'the narrowest scope is the default');
  assert.match(dialog.querySelector('label[for=yolo-tier-exec]').textContent, /実行系も含める \(外部も含む\)/);
  assert.equal(doc.getElementById('yolo-start-exec').hidden, true);
  radios[2].checked = true;
  radios[2].dispatchEvent(new env.w.Event('change', { bubbles: true }));
  // exec includes external: both texts are shown.
  assert.equal(doc.getElementById('yolo-start-external').hidden, false);
  assert.equal(doc.getElementById('yolo-start-exec').hidden, false);
  assert.match(doc.getElementById('yolo-start-exec').textContent, /迂回/);
  radios[1].checked = true;
  radios[1].dispatchEvent(new env.w.Event('change', { bubbles: true }));
  assert.equal(doc.getElementById('yolo-start-exec').hidden, true);
  radios[2].checked = true;
  doc.getElementById('yolo-start-next').click();
  await settle();
  assert.deepEqual(posts, [['/api/yolo/request', { tier: 'external', exec: true }]]);
  // The refusal is worded (ja).
  assert.match($(env, '.yolo-notice').textContent, /実行系を含められません/);
  // Not offered: two choices only.
  const env2 = page({ statuses: [base({ allowance: 'external' })], lang: 'en' });
  await make(env2).start();
  $(env2, '.yolo-start').click();
  await settle();
  assert.deepEqual([...env2.w.document.querySelectorAll('#yolo-start-dialog input[type=radio]')].map((r) => r.value), ['internal', 'external']);
  env2.w.document.getElementById('yolo-start-cancel').click();
  await settle();
});

test('an exec window: the badge says so and counts the exec sub-cap; used up, exec asks again (phase 7)', { skip }, async () => {
  const open = { tier: 'external', external_used: 3, external_limit: 200, exec: true, exec_used: 1, exec_limit: 200 };
  const used = { ...open, exec_used: 200 };
  const env = page({ statuses: [active(open), active(used)] });
  const b = make(env);
  await b.start();
  assert.equal($(env, '.yolo-exec-tag').textContent, 'Includes exec');
  assert.equal($(env, '.yolo-exec').textContent, 'Exec 1/200');
  assert.equal($(env, '.yolo-exec-note').hidden, true);
  await b.read();
  await settle();
  assert.equal($(env, '.yolo-exec').textContent, 'Exec 200/200');
  assert.equal($(env, '.yolo-exec-note').hidden, false);
  assert.equal($(env, '.yolo-exec-note').textContent, 'Exec tools ask you again (the exec limit is reached)');
  const ja = page({ statuses: [active({ ...open, exec_used: 0, exec_limit: 0 })], lang: 'ja' });
  await make(ja).start();
  assert.equal($(ja, '.yolo-exec-tag').textContent, '実行系も含む');
  assert.equal($(ja, '.yolo-exec').textContent, '実行 0/0');
  assert.equal($(ja, '.yolo-exec-note').textContent, '実行系は確認に戻りました (実行系の上限に達しました)');
  // A window without exec has none of them.
  const plain = page({ statuses: [active({ tier: 'external', external_limit: 200 })] });
  await make(plain).start();
  assert.equal($(plain, '.yolo-exec-tag'), null);
  assert.equal($(plain, '.yolo-exec'), null);
  assert.equal($(plain, '.yolo-exec-note'), null);
});

// --- #450: the tools a scope can approve, in the tier dialog --------------------------------

const targets = (over = {}) => ({
  ask_total: 4,
  scopes: {
    internal: { count: 2, tools: ['editor', 'file_write'] },
    external: { count: 3, tools: ['editor', 'file_write', 'web_fetch'] },
    exec: { count: 4, tools: ['editor', 'file_write', 'shell', 'web_fetch'] },
  },
  excluded: [{ name: 'cron', reason: 'exec_runs_later' }],
  locked: false,
  ...over,
});
const none = () => targets({
  ask_total: 0, excluded: [],
  scopes: { internal: { count: 0, tools: [] }, external: { count: 0, tools: [] }, exec: { count: 0, tools: [] } },
});

test('targetsView: the chosen scope\'s count and names, none, unknown, and no field at all (#450)', () => {
  assert.equal(Y.targetsView(undefined, 'internal'), null, 'an answer without targets draws nothing');
  assert.deepEqual(Y.targetsView(null, 'internal'), { kind: 'unknown' });
  assert.deepEqual(Y.targetsView({ scopes: { internal: { count: -1, tools: [] } } }, 'internal'), { kind: 'unknown' });
  const some = Y.targetsView(targets(), 'internal');
  assert.deepEqual(some, { kind: 'some', count: 2, shown: ['editor', 'file_write'], more: 0, excluded: ['cron'], hidden: [] });
  // An exec tool that is not offered at all is said apart from a person's card.
  const off = Y.targetsView(targets({ excluded: [{ name: 'shell', reason: 'exec_not_exposed' }, { name: 'cron', reason: 'exec_runs_later' }] }), 'internal');
  assert.deepEqual([off.excluded, off.hidden], [['cron'], ['shell']]);
  // "External allowed" counts internal + external; "Include exec" all three.
  assert.equal(Y.targetsView(targets(), 'external').count, 3);
  assert.deepEqual(Y.targetsView(targets(), 'exec').shown, ['editor', 'file_write', 'shell', 'web_fetch']);
  assert.deepEqual(Y.targetsView(none(), 'external'), { kind: 'none', excluded: [], hidden: [] });
  // Only tool-like names are drawn; a long list folds into "N more".
  const odd = Y.targetsView(targets({ scopes: { internal: { count: 1, tools: ['<img src=x>'] } } }), 'internal');
  assert.deepEqual(odd.shown, ['?']);
  const many = Array.from({ length: 15 }, (_, i) => `tool_${i}`);
  const long = Y.targetsView(targets({ scopes: { internal: { count: 15, tools: many } } }), 'internal');
  assert.equal(long.shown.length, 12);
  assert.equal(long.more, 3);
});

test('the tier dialog warns when the scope can approve no tool, and links to the tool settings (ja/en, #450)', { skip }, async () => {
  for (const [lang, warn, link] of [
    ['ja', /この範囲には自動承認の対象になる「毎回確認」のツールがないため、自動承認は働きません/, 'ツールの設定を開く'],
    ['en', /No tool set to Ask is covered by this scope, so nothing will be approved automatically/, 'Open the tool settings'],
  ]) {
    const env = page({ statuses: [base({ allowance: 'external', targets: none() })], lang });
    const doc = env.w.document;
    const posts = [];
    const badge = make(env, { post: async (url, body) => { posts.push([url, body]); return { status: 409, ok: false, json: async () => ({ code: 'yolo_busy' }) }; } });
    await badge.start();
    $(env, '.yolo-start').click();
    await settle();
    const box = doc.getElementById('yolo-start-targets');
    assert.equal(box.hidden, false);
    assert.equal(box.dataset.state, 'none');
    assert.match(box.querySelector('.yolo-targets-warn').textContent, warn);
    const a = box.querySelector('a');
    assert.equal(a.getAttribute('href'), '/settings#sec-tools');
    assert.equal(a.textContent, link);
    // A warning only: the window can still be started.
    assert.equal(doc.getElementById('yolo-start-next').disabled, false);
    doc.getElementById('yolo-start-next').click();
    await settle();
    assert.deepEqual(posts, [['/api/yolo/request', { tier: 'internal' }]]);
  }
});

test('the tier dialog names the tools of the chosen scope and follows the choice (#450)', { skip }, async () => {
  const env = page({ statuses: [base({ allowance: 'external', exec_offered: true, targets: targets() })], lang: 'en' });
  const doc = env.w.document;
  await make(env).start();
  $(env, '.yolo-start').click();
  await settle();
  const box = doc.getElementById('yolo-start-targets');
  assert.equal(box.dataset.state, 'some');
  assert.equal(box.querySelector('.yolo-targets-text').textContent, 'Tools this scope can approve: 2 — editor, file_write');
  assert.equal(box.querySelector('.yolo-targets-warn'), null);
  assert.equal(box.querySelector('.yolo-targets-excluded').textContent, 'Not approved automatically (still asks you): cron');
  assert.equal(box.querySelector('.yolo-targets-hidden'), null);
  const radios = [...doc.querySelectorAll('#yolo-start-dialog input[type=radio]')];
  radios[2].checked = true;
  radios[2].dispatchEvent(new env.w.Event('change', { bubbles: true }));
  assert.equal(box.querySelector('.yolo-targets-text').textContent, 'Tools this scope can approve: 4 — editor, file_write, shell, web_fetch');
  doc.getElementById('yolo-start-cancel').click();
  await settle();
  // ja wording, and the rules could not be read (targets: null): said so, with the link.
  const ja = page({ statuses: [base({ targets: null })], lang: 'ja' });
  await make(ja).start();
  $(ja, '.yolo-start').click();
  await settle();
  const jbox = ja.w.document.getElementById('yolo-start-targets');
  assert.equal(jbox.dataset.state, 'unknown');
  assert.match(jbox.textContent, /自動承認の対象になるツールを確かめられませんでした/);
  ja.w.document.getElementById('yolo-start-cancel').click();
  await settle();
  // A server without the field: no box.
  const old = page({ statuses: [base()], lang: 'en' });
  await make(old).start();
  $(old, '.yolo-start').click();
  await settle();
  assert.equal(old.w.document.getElementById('yolo-start-targets').hidden, true);
  old.w.document.getElementById('yolo-start-cancel').click();
  await settle();
});

test('the targets survive an answer without them (Stop\'s) and reach the dialog (#450)', { skip }, async () => {
  const env = page({
    statuses: [{ ...active(), targets: none() }],
    posts: { '/api/yolo/stop': [200, base({ last_end: { id: 'x', reason: 'revoked_stop', class: 'normal', at: 1 } })] },
  });
  const seen = [];
  const badge = make(env, { chooseTier: async (tiers, channel, found) => { seen.push(found); return null; } });
  await badge.start();
  $(env, '.yolo-stop').click();
  await settle();
  assert.equal($(env, '.yolo-badge').dataset.mode, 'ended');
  $(env, '.yolo-ack').click();
  await settle();
  $(env, '.yolo-start').click();
  await settle();
  assert.equal(seen.length, 1);
  assert.equal(seen[0].scopes.internal.count, 0, 'the last read targets, not undefined');
});

test('an exec tool that is not offered is listed as unavailable, not as "still asks you" (ja, #450)', { skip }, async () => {
  const env = page({ statuses: [base({ targets: targets({ excluded: [{ name: 'shell', reason: 'exec_not_exposed' }] }) })], lang: 'ja' });
  await make(env).start();
  $(env, '.yolo-start').click();
  await settle();
  const box = env.w.document.getElementById('yolo-start-targets');
  assert.equal(box.querySelector('.yolo-targets-hidden').textContent, '実行系ツールが無効なため使えません: shell');
  assert.equal(box.querySelector('.yolo-targets-excluded:not(.yolo-targets-hidden)'), null);
  env.w.document.getElementById('yolo-start-cancel').click();
  await settle();
});

test('the 0-Ask warning and the progress-report tick share the tier dialog (#450 with ADR 0014)', { skip }, async () => {
  const env = page({ statuses: [base({ targets: none() })], lang: 'en' });
  const doc = env.w.document;
  env.w.StiraProgress = { defaultOn: true, intervalMin: 30, continueAfterMin: 10, maxAuto: 3 };
  const posts = [];
  const badge = make(env, { post: async (url, body) => { posts.push([url, body]); return { status: 409, ok: false, json: async () => ({ code: 'yolo_busy' }) }; } });
  await badge.start();
  $(env, '.yolo-start').click();
  await settle();
  const ids = [...doc.querySelectorAll('#yolo-start-dialog form > *')].map((n) => n.id || n.tagName.toLowerCase());
  assert.ok(ids.indexOf('yolo-start-targets') < ids.indexOf('label'), 'the targets come before the tick');
  assert.equal(doc.getElementById('yolo-start-targets').dataset.state, 'none');
  assert.equal(doc.getElementById('yolo-progress').checked, true);
  doc.getElementById('yolo-start-next').click();
  await settle();
  assert.deepEqual(posts, [['/api/yolo/request', { tier: 'internal', progress: true }]]);
});

// --- #448: what ran in the window (every tool call by decision, counts only) ----------------

const calls = (over = {}) => ({
  total: 7, failed: 1, by_decision: { auto_approved: 3, always: 2, manual: 1, denied: 1 },
  by_category: { internal: 6, external: 1, exec: 0 }, by_tool: { file_read: 2, file_write: 4, web_search: 1 }, ...over,
});

test('the tool-call breakdown text, ja and en; nothing without a breakdown (#448)', () => {
  for (const k of ['yolo.tools', 'yolo.tools_failed', 'yolo.tools_cap_note', 'yolo.tools_line']) {
    assert.ok(Object.hasOwn(CAT.ja, k) && Object.hasOwn(CAT.en, k), k);
  }
  const tools = Y.toolsOf(calls());
  assert.equal(Y.toolsText(tools, tr('ja')), 'ツール 7 回 (自動承認 3 / 常に許可 2 / 手動 1 / 拒否 1) · 失敗 1');
  assert.equal(Y.toolsText(tools, tr('en')),
    'Tools 7 (auto-approved 3 / always 2 / manual 1 / denied 1) · failed 1');
  assert.equal(Y.toolsText(Y.toolsOf(calls({ failed: 0 })), tr('en')),
    'Tools 7 (auto-approved 3 / always 2 / manual 1 / denied 1)');
  // An older server (no breakdown) or a malformed one: nothing is claimed.
  for (const bad of [undefined, null, 'seven', { total: -1, by_decision: {} }, { total: 3 }]) {
    assert.equal(Y.toolsOf(bad), null);
  }
  // Only integers reach the text.
  const odd = Y.toolsOf(calls({ by_decision: { auto_approved: '<b>', always: 1.5, manual: null, denied: 2 } }));
  assert.deepEqual([odd.auto, odd.always, odd.manual, odd.denied], [0, 0, 0, 2]);
  assert.equal(CAT.ja['yolo.tools_cap_note'], '上限は自動承認だけを数えます');
  assert.equal(CAT.en['yolo.tools_cap_note'], 'the limit counts automatic approvals only');
});

test('the badge shows every tool call of the window and that the cap counts auto only (#448)', { skip }, async () => {
  const env = page({ statuses: [active({ tool_calls: calls({ failed: 0 }), turns: 1, used: 3 })], lang: 'ja' });
  const badge = make(env);
  await badge.start();
  assert.equal($(env, '.yolo-count').textContent, '自動承認 3/2000');
  assert.equal($(env, '.yolo-tools').textContent,
    'ツール 7 回 (自動承認 3 / 常に許可 2 / 手動 1 / 拒否 1) · 上限は自動承認だけを数えます');
  // An answer without the breakdown (an older server): no line, nothing else changes.
  const old = page({ statuses: [active()] });
  await make(old).start();
  assert.equal($(old, '.yolo-tools'), null);
  assert.equal($(old, '.yolo-count').textContent, 'Auto-approved 37/2000');
});

test('when a window this page watched ends: one summary line, and the ended badge keeps it (#448)', { skip }, async () => {
  const end = { id: W.slice(0, 8), reason: 'revoked_stop', class: 'normal', at: 5, tool_calls: calls(), turns: 2 };
  const env = page({ statuses: [active({ tool_calls: calls({ total: 0, failed: 0, by_decision: { auto_approved: 0, always: 0, manual: 0, denied: 0 } }) })] });
  const badge = make(env);
  await badge.start();
  const texts = () => [...env.w.document.querySelectorAll('#conversation .yolo-line')].map((n) => n.textContent);
  assert.deepEqual(texts(), []);
  env.next = base({ last_end: end });
  await badge.read();
  env.next = base({ last_end: end });
  await badge.read();
  assert.deepEqual(texts(), [
    'What ran in YOLO window abababab: Tools 7 (auto-approved 3 / always 2 / manual 1 / denied 1) · failed 1 · 2 turns',
  ]);
  assert.equal($(env, '.yolo-badge').dataset.mode, 'ended');
  assert.equal($(env, '.yolo-tools').textContent,
    'Tools 7 (auto-approved 3 / always 2 / manual 1 / denied 1) · failed 1');
  // A page opened after the end shows the badge's line but writes no log line.
  const later = page({ statuses: [base({ last_end: end })] });
  await make(later).start();
  assert.equal(later.w.document.querySelectorAll('.yolo-line').length, 0);
  assert.match($(later, '.yolo-tools').textContent, /^Tools 7 /);
});

// #601 E13/E14: a read that fails after an idle answer (offered / unavailable / latched) must
// not leave that older answer on screen without Stop, and must keep being retried.
test('a failed read after an idle answer shows "unreadable" with Stop and is retried (#601 E13)', { skip }, async () => {
  for (const idle of [base(), base({ offered: false, unavailable: ['clock_not_measured'] }),
    base({ offered: false, latched: true, latches: [{ subject: 'window', window_id: W, reason: 'revoked_clock_anomaly' }] })]) {
    const env = page({ statuses: [idle] });
    let down = false;
    const fetch = async (url) => { if (down) throw new Error('down'); return env.fetch(url); };
    const badge = make(env, { fetch });
    await badge.start();
    assert.notEqual($(env, '.yolo-badge').dataset.mode, 'unreadable');
    const before = env.timers.length;
    down = true;
    await badge.read();
    assert.equal($(env, '.yolo-badge').dataset.mode, 'unreadable');
    assert.ok($(env, '.yolo-stop'), 'Stop is offered while the status cannot be read');
    assert.ok(env.timers.length > before, 'the failed read is retried');
    // the next good read draws the answer again
    down = false;
    await badge.read();
    assert.notEqual($(env, '.yolo-badge').dataset.mode, 'unreadable');
  }
  // an older answer that already shows Stop (a window) stays drawn through a failed read
  const env = page({ statuses: [active()] });
  let down = false;
  const badge = make(env, { fetch: async (url) => { if (down) throw new Error('down'); return env.fetch(url); } });
  await badge.start();
  down = true;
  await badge.read();
  assert.equal($(env, '.yolo-badge').dataset.mode, 'window');
  assert.ok($(env, '.yolo-stop'));
});
