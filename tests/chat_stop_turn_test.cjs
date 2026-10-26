// #452: the chat page's explicit stop. Closing the page never stops a turn (the server runs it on
// its own worker), so ■ Stop is the way: on this page's own turn, and on a turn still running from
// before a reload / another tab (named by its turn id). node --test tests/chat_stop_turn_test.cjs
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { shimSource } = require('./ui_shim.cjs');
const { Element, settle } = require('./chat_dom_mock.cjs');

Element.prototype.prepend = function prepend(...nodes) {
  for (const node of nodes.reverse()) { node.remove(); node.parent = this; this.children.unshift(node); }
};
const read = (rel) => fs.readFileSync(path.join(__dirname, '..', rel), 'utf8');
const ja = JSON.parse(read('dashboard/static/i18n/catalog.ja.json'));

function chatPage({ running = null, stoppable = true, events = [], holdEvents = false, clock = null } = {}) {
  const doc = {
    hidden: false, activeElement: null, documentElement: { lang: 'ja' }, nodes: new Map(),
    getElementById(id) {
      if (!this.nodes.has(id)) { const node = new Element(this); node.id = id; this.nodes.set(id, node); }
      return this.nodes.get(id);
    },
    createElement: (tag) => new Element(doc, tag),
    createTextNode: (text) => Object.assign(new Element(doc, '#text'), { _text: String(text) }),
    querySelectorAll: () => [],
    addEventListener() {},
  };
  doc.getElementById('history-stale').hidden = true;
  doc.getElementById('stop-turn-button').hidden = true;
  const state = { running, stoppable, posts: [], histories: 0, stream: null, events, eventReads: [] };
  const json = (body, status = 200) => ({ ok: status < 300, status, json: async () => body });
  const fetch = async (url, init = {}) => {
    if (url === '/api/chat' && init.method === 'POST') {
      state.posts.push({ url, body: init.body });
      return {
        ok: true, headers: { get: () => 'text/event-stream' },
        body: new ReadableStream({ start(c) { state.stream = c; } }),
      };
    }
    if (url === '/api/chat/cancel') {
      state.posts.push({ url, body: JSON.parse(init.body) });
      return json({ status: 'cancelling' }, 202);
    }
    if (url.startsWith('/api/turns/') && url.includes('/events?after_seq=')) {
      state.eventReads.push(url);
      // holdEvents: the first events read waits until the test calls state.releaseEvents()
      if (holdEvents && state.eventReads.length === 1) await new Promise((r) => { state.releaseEvents = r; });
      const after = Number(new URL(url, 'http://x').searchParams.get('after_seq'));
      return json({ events: state.events.filter((e) => e.seq > after) });
    }
    if (url.startsWith('/api/turns')) {
      return json({ turns: state.running ? [{ turn_id: state.running, completed: false, stoppable: state.stoppable }]
        : [{ turn_id: 'old', completed: true }] });
    }
    if (url.startsWith('/api/chat/history')) {
      state.histories += 1;
      return json({ turns: [], session: { id: 's1', reason: 'start', started_at: 1, request: null } });
    }
    return json({});
  };
  const intervals = [];
  const timers = new Map(); // id -> fn, the live ones (clearInterval removes)
  let nextTimer = 0;
  const context = {
    document: doc, fetch, AbortSignal, TextDecoder, TextEncoder, ReadableStream,
    setInterval(fn) { intervals.push(fn); timers.set(++nextTimer, fn); return nextTimer; },
    clearInterval(id) { timers.delete(id); },
    crypto: { randomUUID: (() => { let n = 0; return () => `k-${++n}`; })() },
  };
  if (clock) context.Date = class extends Date { static now() { return clock.now; } };
  context.window = context;
  context.StiraConfirm = { open: async () => true };
  vm.runInNewContext(shimSource() + read('dashboard/static/js/chat_core.js') + read('dashboard/static/js/chat.js'), context);
  const get = (id) => doc.getElementById(id);
  const tick = async () => { for (const fn of intervals) fn(); await settle(20); };
  // only the live 1 s display timer of the elsewhere turn (no poll, no request)
  const secondTimers = () => [...timers.values()].filter((fn) => fn.name === 'showElsewhere');
  const second = async () => { for (const fn of secondTimers()) fn(); await settle(5); };
  return { get, state, tick, second, secondTimers };
}

test('a turn still running from before a reload: shown, stoppable by its id, history read at its end', async () => {
  const { get, state, tick } = chatPage({ running: 't-1' });
  await settle(20);
  assert.equal(get('stop-turn-button').hidden, false, 'the stop is offered');
  assert.equal(get('stop-turn-help').hidden, false);
  assert.equal(get('connection-status').textContent, ja['chat.turn_running_elsewhere']);
  get('stop-turn-button').click();
  await settle(10);
  assert.deepEqual(state.posts, [{ url: '/api/chat/cancel', body: { turn_id: 't-1' } }]);
  assert.equal(get('stop-turn-button').disabled, true, 'one press');
  const before = state.histories;
  state.running = null; // the server recorded the end
  await tick();
  assert.equal(get('stop-turn-button').hidden, true);
  assert.equal(get('stop-turn-button').disabled, false);
  assert.equal(state.histories, before + 1, 'the reply is read from the history');
});

test('a running turn the chat cannot stop (resumed after an approval): shown, no stop', async () => {
  const { get, state, tick } = chatPage({ running: 'r-1', stoppable: false });
  await settle(20);
  assert.equal(get('connection-status').textContent, ja['chat.turn_running_elsewhere']);
  assert.equal(get('stop-turn-button').hidden, true, 'no button that could only fail');
  assert.equal(get('stop-turn-help').hidden, true);
  get('stop-turn-button').click();
  await settle(10);
  assert.deepEqual(state.posts, []);
  const before = state.histories;
  state.running = null;
  await tick();
  assert.equal(state.histories, before + 1, 'its end still reads the history');
});

test('no running turn: no stop button, nothing is cancelled', async () => {
  const { get, state } = chatPage();
  await settle(20);
  assert.equal(get('stop-turn-button').hidden, true);
  assert.deepEqual(state.posts, []);
});

test("this page's own turn: ■ Stop sends one cancel; the turn's end says it was stopped", async () => {
  const { get, state } = chatPage();
  await settle(20);
  get('message').value = 'a long job';
  for (const fn of get('chat-form').listeners.submit) void fn({ preventDefault() {} });
  await settle(10);
  assert.equal(get('stop-turn-button').hidden, false, 'shown while the turn runs');
  get('stop-turn-button').click();
  get('stop-turn-button').click();
  await settle(10);
  assert.deepEqual(state.posts.map((p) => p.url), ['/api/chat', '/api/chat/cancel']);
  assert.deepEqual(state.posts[1].body, {}, 'the own turn: no id needed');
  assert.equal(get('connection-status').textContent, ja['chat.stopping_turn']);
  state.stream.enqueue(new TextEncoder().encode('event: delta\ndata: {"text":"part"}\n\nevent: turn\ndata: {"stop":"cancelled"}\n\n'));
  state.stream.close();
  await settle(20);
  assert.equal(get('connection-status').textContent, ja['chat.turn_stopped']);
  assert.equal(get('stop-turn-button').hidden, true);
});

// #555: the other tab's turn counts its seconds like this page's own: "生成中 N 秒" while a model
// call is open, the time since it began between calls, and ■ Stop stays offered.
const fmt = (key, vars) => ja[key].replace(/\{(\w+)\}/g, (_, name) => String(vars[name]));
const ev = (seq, type, agoSec, data = {}) => ({ turn_id: 't-9', seq, type, ts: Date.now() / 1000 - agoSec, data });
const CALL = 'a'.repeat(32);

test('a turn running in another tab shows its generation seconds and the stop', async () => {
  const events = [ev(1, 'turn.started', 30), ev(2, 'model.started', 12, { call_id: CALL })];
  const { get, state, tick } = chatPage({ running: 't-9', events });
  await settle(20);
  const status = get('connection-status').textContent;
  assert.ok(status.startsWith(ja['chat.turn_running_elsewhere']), status);
  assert.match(status, new RegExp(fmt('chat.generating', { s: '1[23]' })));
  assert.equal(get('stop-turn-button').hidden, false, 'the stop is offered');
  // the model call ended, a tool runs: the time since the turn began (read after seq 2 only)
  state.events.push(ev(3, 'model.completed', 1, { call_id: CALL }));
  await tick();
  assert.ok(state.eventReads.at(-1).endsWith('after_seq=2'), state.eventReads.at(-1));
  assert.match(get('connection-status').textContent, new RegExp(fmt('chat.turn_elapsed', { s: '3[01]' })));
  assert.equal(get('stop-turn-button').hidden, false);
});

test('a status written meanwhile (stopping) is not overwritten by the seconds', async () => {
  const events = [ev(1, 'turn.started', 5), ev(2, 'model.started', 4, { call_id: CALL })];
  const { get, tick } = chatPage({ running: 't-9', events });
  await settle(20);
  get('stop-turn-button').click();
  await settle(10);
  assert.equal(get('connection-status').textContent, ja['chat.stopping_turn']);
  await tick();
  assert.equal(get('connection-status').textContent, ja['chat.stopping_turn']);
});

test('core: progress folds only this turn and keeps the last seq', () => {
  const core = require('../dashboard/static/js/chat_core.js');
  const now = 1_000_000_000_000;
  const progress = core.turnProgressOf(null, 't', [
    { turn_id: 't', seq: 1, type: 'turn.started', ts: now / 1000 - 50 },
    { turn_id: 'other', seq: 9, type: 'model.started', ts: now / 1000, data: { call_id: CALL } },
    { turn_id: 't', seq: 2, type: 'model.started', ts: now / 1000 - 20, data: { call_id: CALL } },
    { turn_id: 't', seq: 3, type: 'model.progress', ts: now / 1000 - 1, data: { call_id: CALL, elapsed_ms: 25_000 } },
  ]);
  assert.equal(progress.seq, 3);
  const tt = (key, vars) => fmt(key, vars);
  assert.equal(core.elsewhereText(progress, now, tt), `${ja['chat.turn_running_elsewhere']} · ${fmt('chat.generating', { s: 26 })}`);
  const between = core.turnProgressOf(progress, 't', [{ turn_id: 't', seq: 4, type: 'model.completed', ts: now / 1000, data: { call_id: CALL } }]);
  assert.equal(core.elsewhereText(between, now, tt), `${ja['chat.turn_running_elsewhere']} · ${fmt('chat.turn_elapsed', { s: 50 })}`);
  assert.equal(core.elsewhereText(null, now, tt), ja['chat.turn_running_elsewhere']);
  assert.equal(core.turnProgressOf(between, 'next', []).seq, 0, 'another turn starts over');
});

// Review round: a stop pressed while the first events read is still out keeps its message —
// through the 1 s timer and through that read's late answer.
test('a stop during the first events read keeps "stopping" (timer, then the late answer)', async () => {
  const events = [ev(1, 'turn.started', 5), ev(2, 'model.started', 4, { call_id: CALL })];
  const { get, state, second } = chatPage({ running: 't-9', events, holdEvents: true });
  await settle(20);
  assert.equal(state.eventReads.length, 1, 'the first read is held');
  assert.equal(get('connection-status').textContent, ja['chat.turn_running_elsewhere']);
  get('stop-turn-button').click();
  await settle(10);
  assert.equal(get('connection-status').textContent, ja['chat.stopping_turn']);
  await second(); // (a) the 1 s timer while the read is out
  assert.equal(get('connection-status').textContent, ja['chat.stopping_turn']);
  state.releaseEvents(); // (b) the read answers late
  await settle(20);
  assert.equal(get('connection-status').textContent, ja['chat.stopping_turn']);
  await second();
  assert.equal(get('connection-status').textContent, ja['chat.stopping_turn']);
});

test('fixed clock: the seconds advance every second without a new read; the timer stops at the end', async () => {
  const clock = { now: 2_000_000_000_000 };
  const at = (seq, type, ago, data = {}) => ({ turn_id: 't-9', seq, type, ts: clock.now / 1000 - ago, data });
  const events = [at(1, 'turn.started', 20), at(2, 'model.started', 10, { call_id: CALL })];
  const { get, state, tick, second, secondTimers } = chatPage({ running: 't-9', events, clock });
  await settle(20);
  const reads = state.eventReads.length;
  const line = (s) => `${ja['chat.turn_running_elsewhere']} · ${fmt('chat.generating', { s })}`;
  assert.equal(get('connection-status').textContent, line(10));
  for (const s of [11, 12, 13]) {
    clock.now += 1000;
    await second();
    assert.equal(get('connection-status').textContent, line(s));
  }
  assert.equal(state.eventReads.length, reads, 'the display timer reads nothing');
  assert.equal(secondTimers().length, 1, 'one display timer');
  state.running = null; // the server recorded the end
  await tick();
  assert.equal(secondTimers().length, 0, 'cleared when the turn ended');
});
