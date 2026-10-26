// Chat page polling (#369): node --test tests/chat_poll_test.cjs
//
// The page's polls go through poll.js (a recording stand-in here; the scheduler itself is in
// tests/poll_test.cjs): a turn on this page or a waiting approval holds them short, a turn
// start boosts them, and an approval request that arrives on the page's own stream is read
// at once, so the approval card appears no later than before.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { resolve } = require('node:path');
const vm = require('node:vm');
const source = ['chat_core.js', 'chat.js']
  .map(name => readFileSync(resolve(__dirname, '../dashboard/static/js', name), 'utf8')).join('\n');

class Element {
  constructor() {
    this.children = []; this.attributes = {}; this.listeners = {}; this.dataset = {};
    this.classList = { add() {}, remove() {} }; this.textContent = ''; this.value = '';
  }
  append(...nodes) { for (const node of nodes) { node.parent = this; this.children.push(node); } }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter(n => n !== this); }
  setAttribute(key, value) { this.attributes[key] = value; }
  removeAttribute(key) { delete this.attributes[key]; }
  addEventListener(key, fn) { this.listeners[key] = fn; }
  focus() {}
}

async function setup({ withPoll = true } = {}) {
  const nodes = new Map();
  const get = id => { if (!nodes.has(id)) nodes.set(id, new Element()); return nodes.get(id); };
  const pollers = [];
  const refreshes = [];
  let queueOptions;
  let resolveRequest;
  const request = new Promise(yes => { resolveRequest = yes; });
  const intervals = [];
  const server = { statusFails: false, uptime: 0 };
  const context = {
    document: { hidden: false, getElementById: get, createElement: () => new Element(),
      createTextNode: text => Object.assign(new Element(), { textContent: text }),
      querySelectorAll: () => [] },
    fetch: async url => {
      if (url === '/api/chat') return request;
      if (url === '/api/gateway/status') {
        if (server.statusFails) return { ok: false, status: 502, json: async () => ({}) };
        server.uptime += 1; // moves on every read: not a change
        return { ok: true, json: async () => ({ uptime_seconds: server.uptime, agent_id: 'a', breaker: { level: 'normal' }, neural_state: {} }) };
      }
      return { ok: true, json: async () => (url.includes('history') ? { turns: [] } : {}) };
    },
    AbortSignal, TextDecoder, setInterval(fn, ms) { intervals.push(ms); },
    StiraApprovals: {
      create(options) {
        queueOptions = options;
        return { refresh: async () => { refreshes.push('approvals'); return false; } };
      },
    },
  };
  if (withPoll) {
    context.StiraPoll = {
      create(options) {
        const poller = { options, started: 0, pokes: 0, boosts: 0,
          start() { this.started += 1; }, poke() { this.pokes += 1; }, boost() { this.boosts += 1; } };
        pollers.push(poller);
        return poller;
      },
    };
  }
  vm.runInNewContext(require('./ui_shim.cjs').shimSource() + source, context);
  await new Promise(setImmediate);
  return { get, server, pollers, refreshes, intervals, resolveRequest, queueOptions: () => queueOptions };
}

function stream(ctx) {
  let controller;
  const body = new ReadableStream({ start(c) { controller = c; } });
  ctx.resolveRequest({ ok: true, headers: { get: () => 'text/event-stream' }, body });
  return {
    async emit(event, data) {
      controller.enqueue(new TextEncoder().encode(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`));
      await new Promise(setImmediate);
    },
    close() { controller.close(); },
  };
}

const byBase = (ctx, base) => ctx.pollers.find(p => p.options.base === base);

test('status and approvals go through poll.js with the intended intervals', async () => {
  const ctx = await setup();
  const status = byBase(ctx, 5000);
  const approvals = byBase(ctx, 1000);
  assert.ok(status && approvals, 'both pollers created');
  assert.equal(status.options.max, 20000);
  assert.equal(approvals.options.max, 5000);
  assert.equal(approvals.options.backoffMax, 5000, 'a failing queue is asked at least every 5 s');
  assert.equal(status.started, 1);
  assert.equal(approvals.started, 1);
  assert.deepEqual(ctx.intervals, [], 'no fixed timers left');
});

test('without poll.js the plain timers of before run (and nothing breaks)', async () => {
  const ctx = await setup({ withPoll: false });
  assert.deepEqual(ctx.intervals.sort((a, b) => a - b), [1000, 5000]); // approvals, status
});

test('a turn on this page holds the polls short, boosts them at its start, and ends with a status read', async () => {
  const ctx = await setup();
  const approvals = byBase(ctx, 1000);
  const status = byBase(ctx, 5000);
  assert.equal(approvals.options.hold(), false);
  assert.equal(status.options.hold(), false);
  ctx.get('message').value = 'hello';
  const completed = ctx.get('chat-form').listeners.submit({ preventDefault() {} });
  assert.equal(approvals.options.hold(), true, 'a turn runs: short interval');
  assert.equal(status.options.hold(), true);
  assert.equal(approvals.boosts, 1);
  assert.equal(status.boosts, 1);
  const s = stream(ctx);
  await s.emit('turn', {});
  s.close();
  await completed;
  assert.equal(approvals.options.hold(), false, 'the turn is over: free to widen again');
});

test('an approval.requested on the page stream reads the queue now; other events do not', async () => {
  const ctx = await setup();
  const approvals = byBase(ctx, 1000);
  ctx.get('message').value = 'hello';
  const completed = ctx.get('chat-form').listeners.submit({ preventDefault() {} });
  const s = stream(ctx);
  await s.emit('ui_event', { type: 'turn.started', turn_id: 't1', seq: 1, data: {} });
  await s.emit('ui_event', { type: 'tool_call.created', turn_id: 't1', seq: 2, data: { name: 'file_write' } });
  assert.equal(approvals.pokes, 0);
  await s.emit('ui_event', { type: 'approval.requested', turn_id: 't1', seq: 3, data: { request_id: 'r1' } });
  assert.equal(approvals.pokes, 1, 'the card is fetched at once, not at the next poll');
  await s.emit('ui_event', { type: 'approval.resolved', turn_id: 't1', seq: 4, data: { request_id: 'r1', decision: 'approved' } });
  assert.equal(approvals.pokes, 2);
  await s.emit('turn', {});
  s.close();
  await completed;
});

test('a waiting approval holds the approval poll short even with no turn running', async () => {
  const ctx = await setup();
  const approvals = byBase(ctx, 1000);
  assert.equal(approvals.options.hold(), false);
  ctx.queueOptions().onCount(2);
  assert.equal(approvals.options.hold(), true);
  ctx.queueOptions().onCount(0);
  assert.equal(approvals.options.hold(), false);
});

test('a failed status read is a failure for the scheduler; an unchanged one may widen', async () => {
  const ctx = await setup();
  const { run } = byBase(ctx, 5000).options;
  assert.equal(await run(), true, 'first read: the status is new');
  assert.equal(await run(), false, 'same status (the uptime aside): nothing changed');
  ctx.server.statusFails = true;
  await assert.rejects(run(), /read failed/);
  assert.match(ctx.get('gateway-health').textContent, /.+/);
  ctx.server.statusFails = false;
  assert.equal(await run(), false, 'recovered: same status as before the outage');
});
