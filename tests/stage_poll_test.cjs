// Stage polling (#369): node --test tests/stage_poll_test.cjs
//
// The live feed's three polls go through poll.js: nothing while the tab is hidden, backed off
// when the server fails, widened while nothing changes, and back to the short interval the
// moment something does. The approval card's delay must not grow: a request found by a poll
// keeps the polls short, and one announced on this page's own stream is read at once.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const poll = require('../dashboard/static/js/poll.js');

const STAGE = path.join(__dirname, '..', 'dashboard', 'stage', 'js');
const load = (name) => import(pathToFileURL(path.join(STAGE, name)).href);
const TURNS = JSON.parse(fs.readFileSync(path.join(__dirname, 'fixtures', 'stage_turn_events.json'), 'utf8'));
const PENDING = JSON.parse(fs.readFileSync(path.join(__dirname, 'fixtures', 'stage_hitl_pending.json'), 'utf8'));
const NEURAL = { trust: 0.5, curiosity: 0.5, vigilance: 0.1, attachment: 0.5, stability: 0.7, satisfaction: 0.5 };

// A manual clock for timers; ``advance`` runs due timers in order and lets their promises settle.
function clock() {
  let t = 0;
  let seq = 0;
  const timers = new Map();
  const settle = () => new Promise((r) => setImmediate(r));
  return {
    now: () => t,
    setTimer: (fn, ms) => { timers.set(++seq, { fn, at: t + ms }); return seq; },
    clearTimer: (id) => timers.delete(id),
    async advance(ms) {
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
    },
    settle,
  };
}

function server() {
  const srv = { calls: [], fail: false, failNext: {}, uptime: 100, turns: [], pending: { requests: [], server_time: 1000 } };
  srv.getJSON = async (url) => {
    srv.calls.push(url);
    if (srv.fail) throw new Error('502');
    if (srv.failNext[url] > 0) { srv.failNext[url] -= 1; throw new Error('502'); }
    srv.uptime += 1; // the uptime moves on every read: not a change
    if (url === '/api/gateway/status') {
      return { uptime_seconds: srv.uptime, neural_state: NEURAL, breaker: { level: 'normal' } };
    }
    if (url === '/api/hitl/pending') return srv.pending;
    if (url.startsWith('/api/turns?')) return { turns: srv.turns };
    if (/^\/api\/turns\/[^/]+\/events/.test(url)) return { events: [] };
    throw new Error('404');
  };
  srv.count = (prefix) => srv.calls.filter((u) => u.startsWith(prefix)).length;
  return srv;
}

async function start({ hidden = false } = {}) {
  const { LiveFeed, schedule } = await load('live.js');
  const clk = clock();
  const srv = server();
  const doc = {
    hidden,
    listeners: [],
    addEventListener(name, fn) { if (name === 'visibilitychange') this.listeners.push(fn); },
    setHidden(v) { this.hidden = v; for (const fn of this.listeners) fn(); },
  };
  const feed = new LiveFeed({ getJSON: srv.getJSON, clock: () => clk.now() / 1000, wall: () => 1000 + clk.now() / 1000 });
  const stop = schedule(feed, doc, { poll, setTimer: clk.setTimer, clearTimer: clk.clearTimer, now: clk.now });
  await clk.settle();
  await clk.settle();
  return { feed, srv, clk, doc, stop };
}

test('idle: the three polls widen to a fraction of the old 2.2 requests/s', async () => {
  const h = await start();
  const boot = h.srv.calls.length;
  await h.clk.advance(120000);
  const idle = h.srv.calls.length - boot;
  const before = 120 * (1 / 1.0 + 1 / 2.0 + 1 / 1.5); // the fixed timers of before: 2.17 req/s
  assert.ok(idle <= before / 3, `${idle} requests in the first 120 s (ladder included); before ${Math.round(before)}`);
  // steady state (after the ladder): at most 1/10 s turns + 1/15 s status + 1/5 s pending
  const n0 = h.srv.calls.length;
  await h.clk.advance(60000);
  const steady = (h.srv.calls.length - n0) / 60;
  assert.ok(steady <= 0.4, `${steady.toFixed(2)} req/s once idle`);
  h.stop();
});

test('hidden tab: no request at all; on return every poll runs once at once', async () => {
  const h = await start();
  h.doc.setHidden(true);
  const n = h.srv.calls.length;
  await h.clk.advance(300000);
  assert.equal(h.srv.calls.length, n, 'nothing sent while hidden');
  h.doc.setHidden(false);
  await h.clk.settle();
  await h.clk.settle();
  const after = h.srv.calls.slice(n);
  for (const url of ['/api/gateway/status', '/api/hitl/pending', '/api/turns?limit=1']) {
    assert.equal(after.filter((u) => u === url).length, 1, `${url} polled once on return`);
  }
  h.stop();
});

test('a tab that loads hidden does not start polling until it is shown', async () => {
  const h = await start({ hidden: true });
  const boot = h.srv.calls.length;
  assert.equal(boot, 0, 'no first read either');
  await h.clk.advance(60000);
  assert.equal(h.srv.calls.length, boot);
  h.doc.setHidden(false);
  await h.clk.settle();
  await h.clk.settle();
  assert.ok(h.srv.calls.length > boot);
  h.stop();
});

test('a failing server is asked less and less; recovery restores polling', async () => {
  const h = await start();
  h.srv.fail = true;
  const n = h.srv.calls.length;
  await h.clk.advance(120000);
  const failing = h.srv.calls.length - n;
  // the queue is asked at least every 5 s even while failing; status and turns back off to 30 s
  assert.ok(failing <= 50, `${failing} requests in 120 s while failing (was ~260)`);
  assert.ok(h.feed.offline);
  h.srv.fail = false;
  await h.clk.advance(60000); // the longest backoff (30 s) has to run out
  assert.ok(!h.feed.offline);
  const m = h.srv.calls.length;
  await h.clk.advance(10000);
  assert.ok(h.srv.calls.length - m >= 4, 'asked again at the short end of the ladder after one success');
  h.stop();
});

test('an approval found by a poll keeps the polls short while it waits', async () => {
  const h = await start();
  await h.clk.advance(120000); // fully widened
  const reqs = PENDING.requests.map((r) => ({ ...r, expires_at: 1000 + 100000 }));
  h.srv.pending = { requests: reqs, server_time: 1000 };
  await h.clk.advance(5000); // within the idle maximum of the pending poll
  assert.equal(h.feed.pendingCount, reqs.length, 'seen within the idle maximum (5 s)');
  const n = h.srv.count('/api/hitl/pending');
  await h.clk.advance(30000);
  const rate = (h.srv.count('/api/hitl/pending') - n) / 30;
  assert.ok(rate >= 0.6, `${rate.toFixed(2)} polls/s while an approval waits`);
  // decided elsewhere: it goes away and the poll widens again
  h.srv.pending = { requests: [], server_time: 1000 };
  await h.clk.advance(60000);
  const m = h.srv.count('/api/hitl/pending');
  await h.clk.advance(60000);
  assert.ok((h.srv.count('/api/hitl/pending') - m) / 60 <= 0.25);
  h.stop();
});

test('an approval request on the page\'s own stream is read at once (no wait for the poll)', async () => {
  const h = await start();
  await h.clk.advance(120000);
  const n = h.srv.count('/api/hitl/pending');
  h.srv.pending = { requests: PENDING.requests, server_time: 1000 };
  // the page's chat stream announces a turn's events (SSE ui_event -> ingestTurnEvent)
  h.feed.ingestTurnEvent({ turn_id: 't1', seq: 1, type: 'turn.started', data: {} });
  await h.clk.settle();
  assert.equal(h.srv.count('/api/hitl/pending'), n, 'a plain event does not poll at once');
  h.feed.ingestTurnEvent({ turn_id: 't1', seq: 2, type: 'approval.requested', data: { request_id: PENDING.requests[0].id } });
  await h.clk.settle();
  await h.clk.settle();
  assert.equal(h.srv.count('/api/hitl/pending'), n + 1, 'approval.requested: the queue is read now');
  assert.equal(h.feed.pendingCount, PENDING.requests.length);
  h.stop();
});

test('an event on the own stream puts every poll back to its short interval', async () => {
  const h = await start();
  await h.clk.advance(120000);
  h.feed.ingestTurnEvent({ turn_id: 't1', seq: 1, type: 'turn.started', data: {} });
  const n = h.srv.calls.length;
  await h.clk.advance(3000);
  assert.ok(h.srv.calls.length - n >= 3, `${h.srv.calls.length - n} requests in the 3 s after an event`);
  h.stop();
});

test('stop() ends all polling', async () => {
  const h = await start();
  h.stop();
  const n = h.srv.calls.length;
  await h.clk.advance(120000);
  assert.equal(h.srv.calls.length, n);
});

test('without poll.js the fixed timers of before still run (and slow down when hidden)', async () => {
  const { LiveFeed, schedule, INTERVALS } = await load('live.js');
  const clk = clock();
  const srv = server();
  const doc = { hidden: false };
  const feed = new LiveFeed({ getJSON: srv.getJSON, clock: () => 0, wall: () => 1000 });
  schedule(feed, doc, { setTimer: clk.setTimer, poll: null });
  await clk.settle();
  await clk.settle();
  const n = srv.calls.length;
  await clk.advance(10000);
  assert.ok(srv.calls.length - n >= 20, 'every second or two');
  doc.hidden = true;
  const m = srv.calls.length;
  await clk.advance(20000);
  assert.ok(srv.calls.length - m <= 15, `${srv.calls.length - m} requests in 20 s while hidden`);
  assert.equal(INTERVALS.hiddenFactor, 5);
});

test('a turn is running and only the approval endpoint failed once: the card still comes (boost must not push the backoff out)', async () => {
  const h = await start();
  await h.clk.advance(30000);
  // a turn starts elsewhere; the turns poll succeeds every second from now on
  h.srv.turns = [{ ...TURNS[0].turn, completed: false, started_at: 1000 + h.clk.now() / 1000 }];
  await h.clk.advance(12000); // the turns poll finds it (idle maximum 10 s); every poll is short now
  assert.ok(h.feed.turnOpen, 'the turn is followed (and stays open)');
  h.srv.failNext['/api/hitl/pending'] = 1;
  const reqs = PENDING.requests.map((r) => ({ ...r, expires_at: 1000 + 100000 }));
  h.srv.pending = { requests: reqs, server_time: 1000 };
  await h.clk.advance(30000);
  assert.equal(h.feed.pendingCount, reqs.length, 'the approval card appears although the first pending read failed');
  assert.equal(h.feed.sources.pending.fails, 0);
  h.stop();
});

test('same for the status poll: a single failure during a turn does not starve it', async () => {
  const h = await start();
  await h.clk.advance(30000);
  h.srv.turns = [{ ...TURNS[0].turn, completed: false, started_at: 1000 + h.clk.now() / 1000 }];
  await h.clk.advance(12000);
  assert.ok(h.feed.turnOpen);
  h.srv.failNext['/api/gateway/status'] = 1;
  const n = h.srv.count('/api/gateway/status');
  await h.clk.advance(20000);
  assert.ok(h.srv.count('/api/gateway/status') - n >= 3, `${h.srv.count('/api/gateway/status') - n} status reads in 20 s`);
  assert.equal(h.feed.sources.status.fails, 0);
  h.stop();
});

test('a tab that is hidden at load sends no request at all (not even the first read)', async () => {
  const { LiveFeed, schedule } = await load('live.js');
  const clk = clock();
  const srv = server();
  const doc = {
    hidden: true,
    listeners: [],
    addEventListener(name, fn) { if (name === 'visibilitychange') this.listeners.push(fn); },
    setHidden(v) { this.hidden = v; for (const fn of [...this.listeners]) fn(); },
  };
  const feed = new LiveFeed({ getJSON: srv.getJSON, clock: () => clk.now() / 1000, wall: () => 1000 });
  const stop = schedule(feed, doc, { poll, setTimer: clk.setTimer, clearTimer: clk.clearTimer, now: clk.now });
  await clk.advance(120000);
  assert.equal(srv.calls.length, 0, 'nothing is sent while the tab has never been shown');
  doc.setHidden(false);
  await clk.settle();
  await clk.settle();
  assert.ok(srv.calls.length >= 3, 'shown: the first reads (status, queue, turns) run');
  const n = srv.calls.length;
  await clk.advance(5000);
  assert.ok(srv.calls.length > n, 'and the polls follow');
  stop();
});

test('the approval queue was down for minutes: the card is on screen within 5 s of recovery', async () => {
  const h = await start();
  await h.clk.advance(30000);
  h.srv.fail = true;
  await h.clk.advance(180000);
  h.srv.fail = false;
  const reqs = PENDING.requests.map((r) => ({ ...r, expires_at: 1000 + 100000 }));
  h.srv.pending = { requests: reqs, server_time: 1000 };
  let shown = null;
  for (let ms = 0; ms <= 8000; ms += 100) {
    await h.clk.advance(100);
    if (h.feed.pendingCount === reqs.length) { shown = ms + 100; break; }
  }
  assert.notEqual(shown, null);
  assert.ok(shown <= 5000, `${shown} ms after recovery (limit 5000)`);
  h.stop();
});
