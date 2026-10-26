// Approvals page polling (#369): node --test tests/approvals_page_poll_test.cjs
//
// The page script runs with the real poll.js and the real approval cards on a manual clock.
// From a fully widened (steady idle) state, an approval request that appears on the server
// must be on screen within 5 s (the agreed idle maximum), and the page sends nothing while
// the tab is hidden.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { resolve } = require('node:path');
const vm = require('node:vm');
const { shimSource } = require('./ui_shim.cjs');
const { Element, byClass, settle } = require('./chat_dom_mock.cjs');

const read = name => readFileSync(resolve(__dirname, '../dashboard/static/js', name), 'utf8');
const NOW = 1_800_000_000;

const request = id => ({
  id, agent_id: 'stira', tool_name: 'memory_write', tool_input: { content: 'note' },
  normalized_input: '{"content":"note"}', args_hash: `hash-${id}`.padEnd(64, '0'),
  break_level: 'normal', reason: 'Tool policy is Ask.', reason_code: 'rule_ask',
  tool_use_id: `use-${id}`, supersedes: null, cwd: null, turn_id: 'turn-abc',
  created_at: NOW - 1, expires_at: NOW + 100000, status: 'pending',
});

function page(hangFromStart = false) {
  let t = 0;
  let seq = 0;
  const timers = new Map();
  const state = { pending: [], calls: [], historyFails: false, historyHangs: hangFromStart, pendingFails: false };
  const nodes = new Map();
  const doc = {
    hidden: false, activeElement: null, listeners: [],
    createElement: tag => new Element(doc, tag),
    addEventListener(name, fn) { if (name === 'visibilitychange') this.listeners.push(fn); },
    dispatchEvent() { return true; },
    getElementById(id) {
      if (!nodes.has(id)) { const n = new Element(doc); n.id = id; nodes.set(id, n); }
      return nodes.get(id);
    },
  };
  class CustomEvent { constructor(type, init) { this.type = type; this.detail = init?.detail; } }
  class FakeDate extends Date { static now() { return NOW * 1000 + t; } }
  const context = {
    document: doc, CustomEvent, JSON, Math, String, Number, Array, Set, Map, Promise, Boolean,
    Date: FakeDate,
    setTimeout: (fn, ms) => { timers.set(++seq, { fn, at: t + ms }); return seq; },
    clearTimeout: id => timers.delete(id),
    setInterval: () => 0,
    crypto: { randomUUID: () => 'uuid-1' },
    fetch: async url => {
      state.calls.push(url);
      if (state.historyHangs && url.startsWith('/api/hitl/history')) return new Promise(() => {});
      const failing = url.startsWith('/api/hitl/pending') ? state.pendingFails : state.historyFails;
      if (failing) return { ok: false, status: 503, json: async () => ({}) };
      const body = url.startsWith('/api/hitl/pending')
        ? { requests: state.pending, server_time: NOW + t / 1000 } : { history: [], server_time: NOW + t / 1000 };
      return { ok: true, status: 200, json: async () => body };
    },
  };
  context.window = context;
  context.globalThis = context;
  vm.runInNewContext(
    shimSource() + read('poll.js') + read('tool_cards.js') + read('approvals.js') + read('approvals_page.js'),
    context,
  );
  async function advance(ms) {
    const end = t + ms;
    for (;;) {
      const due = [...timers.entries()].filter(([, v]) => v.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
      if (!due) break;
      timers.delete(due[0]);
      t = due[1].at;
      due[1].fn();
      await settle();
    }
    t = end;
    await settle();
  }
  const cards = () => doc.getElementById('approval-list').findAll(byClass('approval-card'));
  return { doc, state, advance, cards, now: () => t };
}

test('from steady idle, a new request is on screen within 5 s', async () => {
  const p = page();
  await settle();
  await p.advance(120000); // fully widened
  const n = p.state.calls.filter(u => u.startsWith('/api/hitl/pending')).length;
  assert.ok(n <= 120 / 2 / 2, `${n} reads in 120 s: the poll did widen`);
  assert.equal(p.cards().length, 0);
  p.state.pending = [request('r1')];
  const appeared = p.now();
  let shown = null;
  for (let ms = 0; ms <= 7000; ms += 100) {
    await p.advance(100);
    if (p.cards().length === 1) { shown = p.now() - appeared; break; }
  }
  assert.notEqual(shown, null, 'the card appeared');
  assert.ok(shown <= 5000, `shown after ${shown} ms (limit 5000)`);
});

test('a waiting request keeps the page at the short interval', async () => {
  const p = page();
  p.state.pending = [request('r1')];
  await settle();
  await p.advance(30000);
  const reads = p.state.calls.filter(u => u.startsWith('/api/hitl/pending')).length;
  assert.equal(p.doc.getElementById('approvals-status').textContent, '', 'no refresh error');
  assert.ok(reads >= 14, `${reads} reads in 30 s while a request waits`);
});

test('a hidden tab sends nothing; showing it reads once', async () => {
  const p = page();
  await settle();
  await p.advance(10000);
  p.doc.hidden = true;
  for (const fn of p.doc.listeners) fn();
  const n = p.state.calls.length;
  await p.advance(120000);
  assert.equal(p.state.calls.length, n);
  p.doc.hidden = false;
  for (const fn of p.doc.listeners) fn();
  await settle();
  assert.ok(p.state.calls.length > n);
});

async function shownWithin(p, limit) {
  const appeared = p.now();
  for (let ms = 0; ms <= limit + 3000; ms += 100) {
    await p.advance(100);
    if (p.cards().length === 1) return p.now() - appeared;
  }
  return null;
}

for (const offset of [0, 3000, 7000, 13000, 21000, 29000]) {
  test(`only the history API is down for a long while: a new request is on screen within 5 s (+${offset} ms)`, async () => {
    const p = page();
    await settle();
    p.state.historyFails = true;
    await p.advance(120000 + offset); // the history fails again and again
    assert.equal(p.cards().length, 0);
    p.state.pending = [request('r1')];
    const shown = await shownWithin(p, 5000);
    assert.notEqual(shown, null);
    assert.ok(shown <= 5000, `shown after ${shown} ms with a failing history (limit 5000)`);
  });
}

test('the approval queue itself was down: it is asked at least every 5 s, so the card is back within 5 s of recovery', async () => {
  const p = page();
  await settle();
  p.state.pendingFails = true;
  await p.advance(180000);
  const during = p.state.calls.filter(u => u.startsWith('/api/hitl/pending')).length;
  p.state.pendingFails = false;
  p.state.pending = [request('r1')];
  const shown = await shownWithin(p, 5000);
  assert.notEqual(shown, null);
  assert.ok(shown <= 5000, `shown ${shown} ms after recovery (limit 5000; ${during} failed reads before)`);
});

for (const offset of [0, 1500, 4000, 9000, 12000, 31000]) {
  test(`the history API hangs (no answer at all): a new request is still on screen within 5 s (+${offset} ms)`, async () => {
    const p = page();
    await settle();
    p.state.historyHangs = true;
    await p.advance(120000 + offset);
    assert.equal(p.cards().length, 0);
    p.state.pending = [request('r1')];
    const shown = await shownWithin(p, 5000);
    assert.notEqual(shown, null);
    assert.ok(shown <= 5000, `shown after ${shown} ms with a hanging history (limit 5000)`);
  });
}

test('a hanging history from the very first read does not hold the queue either', async () => {
  const p = page(true);
  await settle();
  await p.advance(3000);
  p.state.pending = [request('r1')];
  const shown = await shownWithin(p, 5000);
  assert.ok(shown !== null && shown <= 5000, `shown after ${shown} ms`);
});
