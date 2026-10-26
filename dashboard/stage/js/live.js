// Live feed (#341, stage 2): polls Stira's read-only, same-origin APIs and turns what changed
// into Stage events through adapter.js. No new server route: the page reads what the chat and
// approvals pages read. Network access is injected (getJSON) so the feed runs in Node tests.

import { describeSource, fromPending, fromStatus, fromTurnEvent, generationFrom, generationSeconds, latestTurn, neuralKey, Pacer, turnIsOpen } from './adapter.js';

export const INTERVALS = {
  status: 2.0, // s; neural state + breaker
  turns: 1.0, // newest turn + its new events
  pending: 1.5, // approval queue
  hiddenFactor: 5, // without poll.js: slower while the tab is hidden
};
// #369: with poll.js (static/js/poll.js) these are the shortest intervals; while nothing
// changes each is widened step by step up to the value here, and it snaps back at once when
// something happens (a turn runs, an approval waits, an event arrives on this page's stream).
// A hidden tab does not poll at all and polls once when it returns.
export const IDLE_MAX = {
  status: 15.0,
  turns: 10.0,
  pending: 5.0, // an approval request from another tab / channel is seen within this
};
const DECIDED_TTL = 30; // s; a request decided here is ignored if a poll still lists it

export class LiveFeed {
  // getJSON(url) -> Promise<object> (throws on HTTP error); clock() -> seconds; wall() -> unix s
  constructor({ getJSON, clock, wall, onNote = () => {} }) {
    this.getJSON = getJSON;
    this.clock = clock;
    this.wall = wall;
    this.onNote = onNote;
    this.onActivity = () => {}; // set by schedule(): an event arrived on this page's own stream
    this.statusKey = null;
    this.pendingKey = null;
    this.pacer = new Pacer();
    this.memo = {};
    this.turn = null; // { id, seq, done }
    this.booted = false;
    this.skew = 0; // server_time - wall()
    this.processStart = null;
    this.approval = null; // current approval record (masked)
    this.pendingCount = 0;
    this.decided = new Map(); // id -> wall time
    this.memoryWrite = new Set(); // ids of running memory writes (narration)
    this.breakerRaw = null;
    this.generation = null; // #445: the running model call { callId, since } (server s)
    this.lastStatus = null;
    this.lastStatusAt = null;
    this.eventsAt = null; // wall time of the last fetch that returned new turn events
    this.sources = {
      status: { url: '/api/gateway/status', ok: null, at: null, fails: 0 },
      turns: { url: '/api/turns', ok: null, at: null, fails: 0 },
      events: { url: '/api/turns/<id>/events', ok: null, at: null, fails: 0 },
      pending: { url: '/api/hitl/pending', ok: null, at: null, fails: 0 },
    };
  }

  get offline() {
    return this.sources.status.fails >= 2;
  }

  serverNow() {
    return this.wall() + this.skew;
  }

  async fetchSource(name, url) {
    const src = this.sources[name];
    try {
      const data = await this.getJSON(url);
      src.ok = true;
      src.at = this.clock();
      src.fails = 0;
      return data;
    } catch (err) {
      src.ok = false;
      src.fails += 1;
      src.error = String((err && err.message) || err).slice(0, 60);
      return null;
    }
  }

  async pollStatus() {
    const status = await this.fetchSource('status', '/api/gateway/status');
    if (!status) return null; // null: could not be read (the scheduler backs off)
    if (typeof status.uptime_seconds === 'number') this.processStart = this.serverNow() - status.uptime_seconds;
    // Applied after the turn events of the same moment (applyStatus): a snapshot taken after
    // a turn must not show its breaker change before the turn itself.
    this.lastStatus = status;
    this.lastStatusAt = this.wall();
    // Whether anything but the uptime moved since the last read.
    const key = JSON.stringify(status, (name, value) => (name === 'uptime_seconds' ? undefined : value));
    const changed = key !== this.statusKey;
    this.statusKey = key;
    return changed;
  }

  applyStatus() {
    if (!this.lastStatus) return;
    // While a turn is open its own events carry neural/breaker; a snapshot fetched before the
    // last batch of turn events may predate them (stale): wait for the next one.
    // The very first snapshot is always applied (the state on page load).
    const first = this.memo.neuralKey === undefined && this.memo.breakerRaw === undefined;
    if (!first && this.turn && !this.turn.done) return;
    if (!first && this.eventsAt !== null && this.lastStatusAt <= this.eventsAt) return;
    const { events, memo, notes } = fromStatus(this.lastStatus, this.memo);
    this.memo = memo;
    for (const n of notes) this.onNote(n);
    for (const e of events) if (e.type === 'breaker') this.breakerRaw = e.raw;
    this.pacer.push(events);
  }

  async pollPending() {
    const data = await this.fetchSource('pending', '/api/hitl/pending');
    if (!data) return null;
    if (typeof data.server_time === 'number') this.skew = data.server_time - this.wall();
    const now = this.wall();
    for (const [id, at] of this.decided) if (now - at > DECIDED_TTL) this.decided.delete(id);
    const requests = (Array.isArray(data.requests) ? data.requests : []).filter((r) => r && !this.decided.has(r.id));
    const { events, current, count } = fromPending({ requests }, this.approval && this.approval.id);
    this.approval = current;
    this.pendingCount = count;
    this.apply(events);
    // A waiting request keeps the short interval (its window has a countdown); so does a change.
    const key = requests.map((r) => String(r.id)).sort().join('|');
    const changed = key !== this.pendingKey;
    this.pendingKey = key;
    return changed || count > 0;
  }

  // A decision accepted by the server (202): the window closes now, the poll confirms.
  markDecided(id, decision) {
    this.decided.set(id, this.wall());
    if (this.approval && this.approval.id === id) {
      this.approval = null;
      // decision (approve / reject) lets the gadgets show the seal (#341)
      this.apply([{ type: 'approval.resolved', id, source: 'decision', decision }]);
    }
  }

  async pullEvents() {
    const t = this.turn;
    const url = `/api/turns/${encodeURIComponent(t.id)}/events?after_seq=${t.seq}`;
    const data = await this.fetchSource('events', url);
    if (!data || !Array.isArray(data.events)) return;
    if (data.events.some((ev) => ev && Number.isInteger(ev.seq) && ev.seq > t.seq)) this.eventsAt = this.wall();
    for (const ev of data.events) this.acceptEvent(t, ev);
  }

  // One turn-contract event of the followed turn, once (by seq).
  acceptEvent(t, ev) {
    if (!ev || !Number.isInteger(ev.seq) || ev.seq <= t.seq) return false;
    t.seq = ev.seq;
    this.generation = generationFrom(this.generation, ev, this.serverNow());
    const mapped = fromTurnEvent(ev);
    for (const m of mapped) {
      if (m.type === 'tool.start' && m.op === 'write') this.memoryWrite.add(m.id);
      if (m.type === 'tool.end') this.memoryWrite.delete(m.id);
      if (m.type === 'neural') this.memo = { ...this.memo, neuralKey: neuralKey(m.values) };
      if (m.type === 'breaker') {
        this.breakerRaw = m.raw;
        this.memo = { ...this.memo, breakerRaw: m.raw };
      }
    }
    this.onNote({ text: describeSource(ev, mapped) });
    this.pacer.push(mapped);
    if (ev.type === 'turn.completed') t.done = true;
    return true;
  }

  // #350: a turn event that arrived on this page's own chat stream (SSE `ui_event`, the same
  // turn contract the poll reads). It is shown at once instead of on the next poll; the poll
  // skips it afterwards (same turn, same seq), so nothing is shown twice.
  ingestTurnEvent(ev) {
    if (!ev || typeof ev !== 'object' || typeof ev.turn_id !== 'string' || !ev.turn_id) return false;
    if (!this.turn || this.turn.id !== ev.turn_id) {
      if (this.turn && !this.turn.done) {
        // Turns are serialised on the server: a newer turn means the followed one is over.
        this.onNote({ text: `turn ${this.turn.id.slice(0, 8)} superseded without turn.completed` });
        this.pacer.push([{ type: 'turn.end', ok: false, stop: 'superseded', turn: this.turn.id }]);
      }
      this.turn = { id: ev.turn_id, seq: 0, done: false };
    }
    this.booted = true;
    const ok = this.acceptEvent(this.turn, ev);
    if (ok) {
      this.eventsAt = this.wall();
      this.onActivity(ev);
    }
    return ok;
  }

  // #445: whole seconds the running model call has taken, null when none (or no open turn).
  generatingSec() {
    if (!this.turnOpen) return null;
    return generationSeconds(this.generation, this.serverNow());
  }

  get turnOpen() {
    return Boolean(this.turn && !this.turn.done);
  }

  // Resolves to whether the followed turn moved (or is still open), null when unreadable.
  async pollTurns() {
    try {
      return await this.followTurns();
    } finally {
      this.applyStatus();
    }
  }

  async followTurns() {
    const data = await this.fetchSource('turns', '/api/turns?limit=1');
    if (!data) return null;
    const before = this.turnKey();
    const latest = latestTurn(data);
    if (latest && (!this.turn || latest.turn_id !== this.turn.id)) {
      if (this.turn && !this.turn.done) {
        await this.pullEvents(); // read the old turn's completion first
        if (!this.turn.done) {
          // Turns are serialised on the server: a newer turn means this one is over.
          this.onNote({ text: `turn ${this.turn.id.slice(0, 8)} superseded without turn.completed` });
          this.pacer.push([{ type: 'turn.end', ok: false, stop: 'superseded', turn: this.turn.id }]);
        }
      }
      const first = !this.booted;
      this.turn = { id: latest.turn_id, seq: 0, done: false };
      // On page load a finished (or restart-cut) turn is history, not something to replay.
      if (first && !turnIsOpen(latest, this.processStart)) {
        this.turn.done = true;
        if (!latest.completed) this.onNote({ key: 'stage.log_interrupted', vars: { turn: latest.turn_id.slice(0, 8) } });
      }
    }
    this.booted = true;
    if (this.turn && !this.turn.done) await this.pullEvents();
    return this.turnKey() !== before || this.turnOpen;
  }

  turnKey() {
    const t = this.turn;
    return t ? `${t.id}:${t.seq}:${t.done}` : '';
  }

  // Boot in order: status (clock, uptime), queue, turns.
  async boot() {
    await this.pollStatus();
    await this.pollPending();
    await this.pollTurns(); // also applies the first status snapshot
  }

  apply(events) {
    this.pacer.push(events);
  }

  // Events due for display at `now` (clock seconds); called every frame.
  drain(now) {
    return this.pacer.take(now);
  }

  info() {
    const now = this.clock();
    return Object.entries(this.sources).map(([name, s]) => ({
      name,
      url: s.url,
      state: s.ok === null ? 'pending' : s.ok ? 'ok' : `error${s.error ? ` (${s.error})` : ''}`,
      age: s.at === null ? null : Math.max(0, now - s.at),
    }));
  }
}

// Runs the three polls on their own timers (browser only). With poll.js (the page loads it
// from base.html; ``poll`` is injected in tests) each poll is paused while the tab is hidden,
// backed off when the server fails, widened while nothing changes and short again at once
// when something does (#369). Without it: the plain timers of before, 5x slower when hidden.
export function schedule(feed, doc, { setTimer, clearTimer, now, poll = globalThis.StiraPoll } = {}) {
  if (!poll) return scheduleFixed(feed, doc, setTimer || setTimeout);
  const timing = {};
  if (setTimer) timing.setTimer = setTimer;
  if (clearTimer) timing.clearTimer = clearTimer;
  if (now) timing.now = now;
  const ms = (s) => Math.round(s * 1000);
  const read = (fn) => async () => {
    const result = await fn();
    if (result === null) throw new Error('poll failed'); // -> backoff
    return result;
  };
  const mk = (fn, base, max, hold, backoffMax) =>
    poll.create({ run: read(fn), base: ms(base), max: ms(max), hold, doc, ...(backoffMax ? { backoffMax: ms(backoffMax) } : {}), ...timing });
  let pending = null;
  let status = null;
  const turns = mk(
    async () => {
      const result = await feed.pollTurns();
      // A turn is running (here or elsewhere): approvals and the neural state move now.
      if (feed.turnOpen) {
        if (pending) pending.boost();
        if (status) status.boost();
      }
      return result;
    },
    INTERVALS.turns,
    IDLE_MAX.turns,
    () => feed.turnOpen,
  );
  pending = mk(() => feed.pollPending(), INTERVALS.pending, IDLE_MAX.pending, () => feed.pendingCount > 0 || feed.turnOpen, IDLE_MAX.pending);
  status = mk(() => feed.pollStatus(), INTERVALS.status, IDLE_MAX.status, () => feed.turnOpen);
  const all = [status, turns, pending];
  // This page's own chat stream: back to the short intervals; an approval request (or its
  // resolution) is read at once instead of on the next poll.
  feed.onActivity = (ev) => {
    for (const p of all) p.boost();
    if (ev && (ev.type === 'approval.requested' || ev.type === 'approval.resolved')) pending.poke();
  };
  let stopped = false;
  let began = false;
  const begin = () => {
    if (began || stopped) return;
    began = true;
    feed.boot().finally(() => {
      if (stopped) return;
      for (const p of all) p.start({ wait: true }); // boot has just read them all
    });
  };
  // The first read is under the same rule as the polls: a tab that is hidden at load sends
  // nothing until it is shown.
  if (doc.hidden && doc.addEventListener) {
    doc.addEventListener('visibilitychange', () => {
      if (!doc.hidden) begin();
    });
  } else {
    begin();
  }
  return () => {
    stopped = true;
    feed.onActivity = () => {};
    for (const p of all) p.stop();
  };
}

function scheduleFixed(feed, doc, setTimer) {
  let stopped = false;
  const loop = (name, fn, base) => {
    const run = async () => {
      if (stopped) return;
      try {
        await fn();
      } finally {
        const factor = doc.hidden ? INTERVALS.hiddenFactor : 1;
        setTimer(run, base * factor * 1000);
      }
    };
    return run;
  };
  feed.boot().finally(() => {
    loop('status', () => feed.pollStatus(), INTERVALS.status)();
    loop('turns', () => feed.pollTurns(), INTERVALS.turns)();
    loop('pending', () => feed.pollPending(), INTERVALS.pending)();
  });
  return () => {
    stopped = true;
  };
}
