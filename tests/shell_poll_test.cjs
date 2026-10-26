// Nav chips polling (#369): node --test tests/shell_poll_test.cjs
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { resolve } = require('node:path');
const vm = require('node:vm');
const { shimSource } = require('./ui_shim.cjs');

const source = readFileSync(resolve(__dirname, '../dashboard/static/js/shell.js'), 'utf8');

function setup({ queueOnPage = false, withPoll = true } = {}) {
  const chips = {};
  const nodes = new Map();
  const node = (id) => {
    if (!nodes.has(id)) {
      nodes.set(id, {
        id, dataset: {}, listeners: {},
        querySelector: () => chips[`${id}.value`] || (chips[`${id}.value`] = { textContent: '' }),
        addEventListener(name, fn) { this.listeners[name] = fn; },
      });
    }
    return nodes.get(id);
  };
  const docListeners = {};
  const document = {
    readyState: 'complete', hidden: false,
    getElementById: (id) => (id === 'approval-list' ? (queueOnPage ? node(id) : null) : id.startsWith('nav-') ? node(id) : null),
    addEventListener: (name, fn) => { docListeners[name] = fn; },
  };
  const state = { pending: { requests: [] }, status: { breaker: { level: 'normal' } }, calls: [], fail: false };
  const created = [];
  const intervals = [];
  const context = {
    document, Promise, JSON, Boolean, String, Number, Array, Object,
    matchMedia: () => ({ matches: false, addEventListener() {} }),
    fetch: async (url) => {
      state.calls.push(url);
      if (state.fail) return { ok: false, status: 503, json: async () => ({}) };
      return { ok: true, json: async () => (url.includes('pending') ? state.pending : state.status) };
    },
    setInterval(fn, ms) { intervals.push(ms); },
    CustomEvent: class {},
  };
  context.window = context;
  context.window.matchMedia = context.matchMedia;
  if (withPoll) {
    context.StiraPoll = {
      create(options) {
        created.push(options);
        return { start() { this.started = true; } };
      },
    };
  }
  vm.runInNewContext(shimSource() + source, context);
  const chip = (id) => (nodes.get(id) || {}).dataset;
  return { state, created, intervals, chip, docListeners, chips };
}

test('the chips poll through poll.js: 10 s, widening to 20 s, a hidden tab is paused there', async () => {
  const h = setup();
  assert.equal(h.created.length, 1);
  assert.equal(h.created[0].base, 10000);
  assert.equal(h.created[0].max, 20000);
  assert.ok(h.created[0].doc, 'the document is handed over so a hidden tab does not poll');
  assert.deepEqual(h.intervals, []);
});

test('an unchanged chip may widen; a changed or non-empty queue stays short; a failure is a failure', async () => {
  const h = setup();
  const { run } = h.created[0];
  assert.equal(await run(), true, 'first read: new');
  assert.equal(await run(), false, 'same chips');
  h.state.pending = { requests: [{ id: 'a' }] };
  assert.equal(await run(), true, 'an approval is waiting');
  assert.equal(await run(), true, 'and keeps the poll short while it waits');
  h.state.pending = { requests: [] };
  assert.equal(await run(), true, 'it went away');
  assert.equal(await run(), false);
  h.state.fail = true;
  await assert.rejects(run());
  assert.equal(h.chip('nav-connection').state, 'crit');
});

test('a page with its own approval queue does not ask for the approvals a second time', async () => {
  const h = setup({ queueOnPage: true });
  await h.created[0].run();
  assert.deepEqual(h.state.calls, ['/api/gateway/status']);
  // the cards publish the count; the chip follows
  h.docListeners['stira:approvals']({ detail: { count: 2 } });
  assert.equal(h.chip('nav-approvals').state, 'warn');
  assert.equal(await h.created[0].run(), true, 'a published, waiting queue keeps the poll short');
});

test('a page without a queue reads both endpoints', async () => {
  const h = setup();
  await h.created[0].run();
  assert.deepEqual(h.state.calls.sort(), ['/api/gateway/status', '/api/hitl/pending']);
});

test('without poll.js: a plain timer, one read at once', async () => {
  const h = setup({ withPoll: false });
  await new Promise((r) => setImmediate(r));
  assert.deepEqual(h.intervals, [10000]);
  assert.ok(h.state.calls.length >= 1);
});
