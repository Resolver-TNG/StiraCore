// One page, two reads in trouble at once (#601 E1 + E13): the YOLO status read fails while the
// approval queue's read hangs until its time limit. node --test tests/yolo_approvals_concurrent_test.cjs
// Needs jsdom (npm install --no-save jsdom axe-core); skipped without it.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { resolve } = require('node:path');
const { shimSource } = require('./ui_shim.cjs');

let JSDOM = null;
try { ({ JSDOM } = require('jsdom')); } catch (err) { /* skipped below */ }
const skip = JSDOM ? false : 'jsdom not installed';
const Y = require('../dashboard/static/js/yolo_ui.js');
const JS = (name) => readFileSync(resolve(__dirname, '../dashboard/static/js', name), 'utf8');
const tick = () => new Promise((r) => setTimeout(r, 0));
async function settle() { for (let i = 0; i < 60; i += 1) await tick(); }

const NOW = 1_800_000_000;
const R = 'cd'.repeat(16);
const base = (over = {}) => ({
  allowance: 'internal', offered: true, unavailable: [], clock_supported: true, window: null,
  request: null, last_end: null, latched: false, latch_reason: null, latches: [],
  host_channel: 'browser', auto_approvals: { window: null, items: [] }, ...over,
});
const request = (id) => ({
  id, agent_id: 'stira', tool_name: 'memory_write', tool_input: { content: 'n' }, normalized_input: '{}',
  args_hash: 'h'.repeat(64), break_level: 'critical', reason: 'r', reason_code: 'breaker_critical_halt',
  tool_use_id: `use-${id}`, supersedes: null, cwd: null, turn_id: 'turn-1', created_at: NOW - 5, expires_at: NOW + 90,
  status: 'pending',
});

test('YOLO read fails while the approval queue read hangs: Stop works, the queue times out, both recover', { skip }, async () => {
  const dom = new JSDOM(`<!doctype html><html lang="en"><body><div id="slot" class="yolo-slot" hidden></div>
    <div id="conversation" role="log"></div><div id="panel" hidden><div id="list"></div><p id="live"></p>
    <p id="status"></p><p id="empty"></p></div></body></html>`, { runScripts: 'outside-only', pretendToBeVisual: true });
  const w = dom.window;
  // one fake clock for both modules (approvals.js uses window.setTimeout for its limit)
  const timers = [];
  w.setTimeout = (fn, ms) => { timers.push({ fn, ms, done: false }); return timers.length; };
  w.clearTimeout = (id) => { if (timers[id - 1]) timers[id - 1].done = true; };
  w.eval(shimSource() + JS('tool_cards.js') + JS('approvals.js'));
  const doc = w.document;
  const $ = (id) => doc.getElementById(id);

  let yoloDown = false, queueHangs = false;
  const posts = [];
  const badge = Y.create({
    doc, win: w, t: (k) => k, storage: null, slots: [$('slot')],
    fetch: async () => {
      if (yoloDown) throw new Error('down');
      return { status: 200, ok: true, json: async () => base() };
    },
    post: async (url) => { posts.push(url); return { status: 200, ok: true, json: async () => base() }; },
    setTimer: (fn, ms) => { timers.push({ fn, ms, done: false, yolo: true }); return timers.length; },
    clearTimer: (id) => { if (timers[id - 1]) timers[id - 1].done = true; },
  });
  const queue = w.StiraApprovals.create({
    document: doc, list: $('list'), panel: $('panel'), live: $('live'), status: $('status'), empty: $('empty'),
    now: () => NOW, clientId: 'tab-me',
    getJSON: async () => (queueHangs ? new Promise(() => {}) : { requests: [request('r1')], server_time: NOW }),
  });

  await badge.start();
  assert.equal(await queue.refresh(), true);
  assert.equal($('slot').querySelector('.yolo-badge').dataset.mode, 'offered');

  // both go wrong at the same time
  yoloDown = true;
  queueHangs = true;
  const hung = queue.refresh();
  await badge.read();
  await settle();
  assert.equal($('slot').querySelector('.yolo-badge').dataset.mode, 'unreadable');
  // Stop needs no status read and is not held by the hung queue read
  $('slot').querySelector('.yolo-stop').click();
  await settle();
  assert.deepEqual(posts, ['/api/yolo/stop']);
  // the queue read reaches its limit: a failure (the pollers back off), and the next refresh runs
  const limit = timers.find((t) => !t.yolo && !t.done && t.ms === 10000);
  assert.ok(limit, 'the queue read has its time limit');
  limit.fn();
  assert.equal(await hung, null);
  assert.equal($('status').textContent, w.t('chat.approval_refresh_failed'));
  // both recover
  yoloDown = false;
  queueHangs = false;
  assert.equal(await queue.refresh(), true);
  await badge.read();
  assert.equal($('slot').querySelector('.yolo-badge').dataset.mode, 'offered');
  assert.equal($('status').textContent, '');
});

test('viewOf with a failed read: ended / latched -> unreadable; window / pending / issuing keep their Stop (#601 E13)', () => {
  const end = { id: 'abababab', tier: 'internal', reason: 'expired_cap', class: 'normal', at: 5 };
  assert.equal(Y.viewOf(base({ last_end: end }), { failed: true }).mode, 'unreadable');
  assert.equal(Y.viewOf(base({ offered: false, latched: true, latches: [] }), { failed: true }).mode, 'unreadable');
  const pending = { id: R, short: R.slice(0, 8), state: 'pending', tier: 'internal', expires_in: 9, channel: 'browser' };
  assert.equal(Y.viewOf(base({ offered: false, request: pending }), { failed: true }).mode, 'pending');
  assert.equal(Y.viewOf(base({ offered: false, request: { ...pending, state: 'issuing' } }), { failed: true }).mode, 'issuing');
  const win = { id: 'abababab', state: 'active', tier: 'internal', remaining_sec: 100, used: 1, limit: 10 };
  assert.equal(Y.viewOf(base({ offered: false, window: win }), { failed: true }).mode, 'window');
  // without a failure the older answer is drawn as it is
  assert.equal(Y.viewOf(base({ last_end: end })).mode, 'ended');
});
