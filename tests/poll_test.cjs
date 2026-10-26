// Polling scheduler (#369): node --test tests/poll_test.cjs
const test = require('node:test');
const assert = require('node:assert/strict');
const { nextDelay, create, GRACE, BACKOFF_MAX } = require('../dashboard/static/js/poll.js');

// A manual clock: timers run when time is advanced, awaiting the run each timer starts.
function rig(options = {}) {
  let t = 0;
  let seq = 0;
  const timers = new Map();
  const doc = {
    hidden: false,
    listeners: [],
    addEventListener(name, fn) { if (name === 'visibilitychange') this.listeners.push(fn); },
    setHidden(v) { this.hidden = v; for (const fn of this.listeners) fn(); },
  };
  const calls = [];
  const results = options.results || [];
  const poller = create({
    base: 1000,
    max: 8000,
    doc,
    now: () => t,
    setTimer: (fn, ms) => { const id = ++seq; timers.set(id, { fn, at: t + ms }); return id; },
    clearTimer: (id) => timers.delete(id),
    run: async () => {
      calls.push(t);
      const next = results.length ? results.shift() : false;
      if (next instanceof Error) throw next;
      return next;
    },
    hold: options.hold,
  });
  async function advance(ms) {
    const end = t + ms;
    for (;;) {
      const due = [...timers.entries()].filter(([, v]) => v.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
      if (!due) break;
      timers.delete(due[0]);
      t = due[1].at;
      due[1].fn();
      await new Promise((r) => setImmediate(r));
    }
    t = end;
  }
  return { poller, doc, calls, advance, timers, results, now: () => t };
}

test('the delay ladder: base for GRACE unchanged runs, then doubling up to max', () => {
  const d = (idle) => nextDelay({ base: 1000, max: 10000, idle });
  assert.equal(GRACE, 3);
  assert.deepEqual([0, 2, 3, 5, 6, 9, 12, 100].map(d), [1000, 1000, 2000, 2000, 4000, 8000, 10000, 10000]);
  assert.equal(nextDelay({ base: 1000, idle: 50 }), 1000, 'no max: never widened');
});

test('failures back off exponentially, capped, and beat idleness', () => {
  const f = (fails) => nextDelay({ base: 1000, max: 5000, fails, idle: 99 });
  assert.deepEqual([1, 2, 3, 4, 5, 6].map(f), [2000, 4000, 8000, 16000, 30000, 30000]);
  assert.equal(BACKOFF_MAX, 30000);
  assert.equal(nextDelay({ base: 40000, fails: 1 }), 40000, 'never below the base');
});

test('an idle poller widens, a change snaps back to the short interval', async () => {
  const h = rig({ results: [false, false, false, false, false, false] });
  h.poller.start();
  await h.advance(0);
  assert.equal(h.poller.state().delay, 1000);
  await h.advance(60000);
  const gaps = h.calls.slice(1).map((c, i) => c - h.calls[i]);
  assert.ok(gaps.slice(0, 2).every((g) => g === 1000), `three runs at the short interval first: ${gaps}`);
  assert.ok(gaps.includes(2000) && gaps.includes(4000), `widened: ${gaps}`);
  assert.ok(Math.max(...gaps) <= 8000);
  assert.ok(h.calls.length < 20, `${h.calls.length} runs in 60 s (was 60)`);
  h.results.push(true); // something new
  await h.advance(8000);
  assert.equal(h.poller.state().delay, 1000);
});

test('hold() keeps the short interval while something needs a quick reaction', async () => {
  let held = true;
  const h = rig({ hold: () => held });
  h.poller.start();
  await h.advance(0);
  await h.advance(30000);
  assert.ok(h.calls.length >= 30, `${h.calls.length}`);
  assert.equal(h.poller.state().idle, 0);
  held = false;
  await h.advance(30000);
  assert.ok(h.poller.state().delay > 1000);
});

test('a failure backs off and one success restores the base interval', async () => {
  const boom = new Error('502');
  const h = rig({ results: [boom, boom, boom, true] });
  h.poller.start();
  await h.advance(0);
  assert.equal(h.poller.state().fails, 1);
  assert.equal(h.poller.state().delay, 2000);
  await h.advance(2000);
  await h.advance(4000);
  assert.equal(h.poller.state().fails, 3);
  assert.equal(h.poller.state().delay, 8000);
  await h.advance(8000);
  assert.equal(h.poller.state().fails, 0);
  assert.equal(h.poller.state().delay, 1000);
});

test('a hidden tab does not poll; returning polls at once', async () => {
  const h = rig();
  h.poller.start();
  await h.advance(0);
  const before = h.calls.length;
  h.doc.setHidden(true);
  assert.equal(h.timers.size, 0, 'timer cleared');
  await h.advance(120000);
  assert.equal(h.calls.length, before, 'no request while hidden');
  h.doc.setHidden(false);
  await h.advance(0);
  assert.equal(h.calls.length, before + 1, 'one request on return');
  assert.equal(h.poller.state().delay, 1000, 'and back to the short interval');
});

test('a tab that starts hidden stays quiet until shown', async () => {
  let t = 0;
  const calls = [];
  const doc = { hidden: true, addEventListener(_n, fn) { this.fn = fn; } };
  const p = create({ base: 1000, doc, now: () => t, setTimer: () => 1, clearTimer: () => {}, run: async () => { calls.push(t); } });
  p.start();
  await new Promise((r) => setImmediate(r));
  assert.equal(calls.length, 0);
  doc.hidden = false;
  doc.fn();
  await new Promise((r) => setImmediate(r));
  assert.equal(calls.length, 1);
});

test('a run in flight when the tab is hidden does not schedule another', async () => {
  let release;
  let t = 0;
  const timers = new Map();
  let seq = 0;
  const doc = { hidden: false, addEventListener(_n, fn) { this.fn = fn; } };
  const p = create({
    base: 1000, doc, now: () => t,
    setTimer: (fn, ms) => { timers.set(++seq, fn); return seq; },
    clearTimer: (id) => timers.delete(id),
    run: () => new Promise((r) => { release = r; }),
  });
  p.start();
  doc.hidden = true;
  doc.fn();
  release(true);
  await new Promise((r) => setImmediate(r));
  assert.equal(timers.size, 0);
});

test('poke() polls now and resets the ladder; boost() shortens a long wait only', async () => {
  const h = rig();
  h.poller.start();
  await h.advance(0);
  await h.advance(40000);
  assert.ok(h.poller.state().delay >= 4000);
  const n = h.calls.length;
  h.poller.boost();
  assert.equal(h.poller.state().delay, 1000);
  assert.equal(h.calls.length, n, 'boost does not poll');
  await h.advance(1000);
  assert.equal(h.calls.length, n + 1, 'the next poll came after the short interval');
  await h.advance(40000);
  const m = h.calls.length;
  h.poller.poke();
  await h.advance(0);
  assert.equal(h.calls.length, m + 1, 'poke polls now');
});

test('an unknown result (undefined) counts as a change: never slowed by mistake', async () => {
  let t = 0;
  const timers = new Map();
  let seq = 0;
  const p = create({
    base: 1000, max: 8000, doc: null, now: () => t,
    setTimer: (fn, ms) => { timers.set(++seq, { fn, at: t + ms }); return seq; },
    clearTimer: (id) => timers.delete(id),
    run: async () => undefined,
  });
  p.start();
  await new Promise((r) => setImmediate(r));
  for (let i = 0; i < 20; i++) {
    const [id, due] = [...timers.entries()][0];
    timers.delete(id);
    t = due.at;
    due.fn();
    await new Promise((r) => setImmediate(r));
  }
  assert.equal(p.state().delay, 1000);
});

test('boost() never pushes a pending run further out (a backoff deadline is kept)', async () => {
  const boom = new Error('502');
  const h = rig({ results: [boom] });
  h.poller.start();
  await h.advance(0); // the first run fails: next in 2000
  assert.equal(h.poller.state().fails, 1);
  const first = h.calls.length;
  // something boosts every 700 ms (a turn is running); the retry still comes at 2000
  for (let i = 0; i < 4; i++) { await h.advance(700); h.poller.boost(); }
  assert.ok(h.calls.length > first, `the failed poll was retried (${h.calls.length - first} runs by 2800 ms)`);
});

test('boost() can bring a long wait forward but not move it back', async () => {
  const h = rig();
  h.poller.start();
  await h.advance(0);
  await h.advance(40000);
  const n = h.calls.length;
  h.poller.boost();
  await h.advance(1000);
  assert.equal(h.calls.length, n + 1, 'forward to base');
  // already due sooner than base: boost leaves it
  h.poller.boost();
  const due = h.timers.size;
  assert.equal(due, 1);
});

test('poke() while a run is in flight is not lost: it runs again right after', async () => {
  let release;
  let calls = 0;
  let t = 0;
  const timers = new Map();
  let seq = 0;
  const p = create({
    base: 1000, doc: null, now: () => t,
    setTimer: (fn, ms) => { timers.set(++seq, { fn, at: t + ms }); return seq; },
    clearTimer: (id) => timers.delete(id),
    run: () => { calls += 1; return new Promise((r) => { release = r; }); },
  });
  p.start();
  assert.equal(calls, 1);
  p.poke(); // an approval request arrives while the first read is in flight
  assert.equal(calls, 1, 'not started twice at once');
  release(false);
  await new Promise((r) => setImmediate(r));
  assert.equal(calls, 2, 'the queue is read again at once: the in-flight answer may predate the event');
  release(false);
  await new Promise((r) => setImmediate(r));
  assert.equal(calls, 2);
});

test('keep() passes a backoff cap through', () => {
  const { keep } = require('../dashboard/static/js/poll.js');
  const timers = [];
  const calls = [];
  const orig = globalThis.setTimeout;
  globalThis.setTimeout = (fn, ms) => { timers.push(ms); return 1; };
  try {
    const p = keep(async () => { calls.push(1); return null; }, 1000, 5000, undefined, null, { wait: true }, 5000);
    assert.equal(timers[0], 1000);
    assert.equal(p.state().delay, 1000);
  } finally { globalThis.setTimeout = orig; }
  assert.equal(nextDelay({ base: 1000, fails: 6, backoffMax: 5000 }), 5000);
  assert.equal(nextDelay({ base: 1000, fails: 1, backoffMax: 5000 }), 2000);
});
