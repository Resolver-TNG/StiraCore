// Tiny interpreter for the declarative animation catalog (#341). No eval, no callbacks:
// it only reads numbers and enum strings from validated, frozen entries.

import { ENUM_CHANNELS } from './catalog.js';

const smooth = (x) => x * x * (3 - 2 * x);

export function evalCurve(c, t) {
  switch (c.fn) {
    case 'const':
      return c.value;
    case 'sine':
      return c.offset + c.amp * Math.sin(2 * Math.PI * (t / c.period + c.phase));
    case 'pulse':
      return (t % c.period) / c.period < c.duty ? c.amp : 0;
    case 'keys': {
      const k = c.keys;
      if (c.ease === 'step') {
        let v = null;
        for (const [kt, kv] of k) if (kt <= t) v = kv;
        return v;
      }
      if (t <= k[0][0]) return k[0][1];
      for (let i = 1; i < k.length; i += 1) {
        if (t <= k[i][0]) {
          const [t0, v0] = k[i - 1];
          const [t1, v1] = k[i];
          const x = t1 > t0 ? (t - t0) / (t1 - t0) : 1;
          return v0 + (v1 - v0) * (c.ease === 'smooth' ? smooth(x) : x);
        }
      }
      return k[k.length - 1][1];
    }
    default:
      return null;
  }
}

// Seeded PRNG (mulberry32) so idle timing is reproducible in screenshots and tests.
export function rng(seed) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

const GESTURE_LIMIT = 3;
const GESTURE_COOLDOWN = 2;

export class Animator {
  constructor(catalog, seed = 341) {
    this.catalog = catalog;
    this.random = rng(seed);
    this.active = []; // {entry, start, end, fadeOutAt}
    this.nextIdle = new Map();
    this.lastGesture = new Map();
    this.ignored = [];
    this.clocks = new Map(); // always-on entries: own clock, so a tempo change never jumps
    this.lastNow = null;
  }

  // The agent-facing entry point: select a gesture by id. Anything else is ignored.
  request(id, now) {
    const e = typeof id === 'string' ? this.catalog.get(id) : undefined;
    let reason = null;
    if (!e) reason = 'unknown id';
    else if (e.scope !== 'gesture') reason = `scope ${e.scope} is not selectable`;
    else if (now - (this.lastGesture.get(id) ?? -Infinity) < GESTURE_COOLDOWN) reason = 'cooldown';
    else if (this.active.filter((a) => a.entry.scope === 'gesture').length >= GESTURE_LIMIT) reason = 'too many gestures';
    if (reason) {
      this.ignored.push({ id: typeof id === 'string' ? id.slice(0, 48) : String(typeof id), reason });
      if (this.ignored.length > 20) this.ignored.shift();
      return { ok: false, reason };
    }
    this.lastGesture.set(id, now);
    this.active.push({ entry: e, start: now, end: now + e.duration });
    return { ok: true };
  }

  // pose: derived state (model.derive().pose). Returns channel values for this frame.
  tick(now, pose) {
    const numeric = {};
    const enums = {};
    for (const e of this.catalog.values()) {
      if (e.scope === 'idle' && e.trigger.every) {
        if (!this.nextIdle.has(e.id)) this.nextIdle.set(e.id, now + this.delay(e));
        if (now >= this.nextIdle.get(e.id)) {
          this.nextIdle.set(e.id, now + e.duration + this.delay(e));
          this.active.push({ entry: e, start: now, end: now + e.duration });
        }
      }
      if (e.scope === 'state') {
        const v = pose[e.trigger.state];
        // gadget channels use 'off' / 'none' for "not shown"; `is` narrows to one value
        const on = e.trigger.is !== undefined ? v === e.trigger.is : Boolean(v) && v !== 'off' && v !== 'none';
        const run = this.active.find((a) => a.entry === e && a.fadeOutAt === undefined);
        if (on && !run) this.active.push({ entry: e, start: now, end: Infinity, value: v });
        if (!on && run) run.fadeOutAt = now;
        if (on && run && run.value !== v) {
          // same trigger, new value (e.g. another prop failed): play it again from the start
          run.fadeOutAt = now;
          this.active.push({ entry: e, start: now, end: Infinity, value: v });
        }
      }
    }
    const step = this.lastNow === null ? now : Math.max(0, now - this.lastNow);
    this.lastNow = now;
    const always = [...this.catalog.values()].filter((e) => (e.scope === 'idle' || e.scope === 'ambient') && e.trigger.always);
    for (const e of always) {
      const rate = e.rateFrom && typeof pose[e.rateFrom] === 'number' ? Math.min(3, Math.max(0.2, pose[e.rateFrom])) : 1;
      this.clocks.set(e.id, (this.clocks.get(e.id) || 0) + step * rate);
    }
    const runs = [...always.map((e) => ({ entry: e, start: now - this.clocks.get(e.id), end: Infinity })), ...this.active];
    const stateWins = pose.emotion === 'worried' || pose.emotion === 'tense';
    for (const a of runs) {
      const e = a.entry;
      if (!this.allowed(e, pose)) continue;
      let t = now - a.start;
      if (e.loop) t %= e.duration;
      let env = e.fade > 0 ? Math.min(1, (now - a.start) / e.fade) : 1;
      if (a.fadeOutAt !== undefined) env *= Math.max(0, 1 - (now - a.fadeOutAt) / Math.max(e.fade, 0.01));
      for (const [ch, curve] of Object.entries(e.channels)) {
        const v = evalCurve(curve, t);
        if (v === null) continue;
        if (ch in ENUM_CHANNELS) {
          if (e.scope === 'gesture' && stateWins) continue;
          enums[ch] = v;
        } else {
          numeric[ch] = (numeric[ch] || 0) + v * e.intensity * (e.scope === 'idle' || e.scope === 'ambient' ? 1 : env);
        }
      }
    }
    this.active = this.active.filter((a) => {
      if (a.fadeOutAt !== undefined) return now - a.fadeOutAt < a.entry.fade;
      return now < a.end;
    });
    return { numeric, enums };
  }

  allowed(e, pose) {
    for (const [k, vals] of Object.entries(e.onlyIf || {})) if (!vals.includes(pose[k])) return false;
    return true;
  }

  delay(e) {
    const [a, b] = e.trigger.every;
    return a + (b - a) * this.random();
  }

  activeIds() {
    return this.active.map((a) => a.entry.id);
  }
}
