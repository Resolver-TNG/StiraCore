// Which motion plays (#341): a pure function of the state that events left. No DOM.
//
// selectMotion() walks MOTION_RULES (data in motion_catalog.js) top to bottom and returns the
// first match with its cause, so the same state always selects the same motion (checked by the
// drift monitor through the `motion` pose channel). nextCue() keeps the one event fact the rules
// need beyond the state itself: that the breaker just rose or just went back to NORMAL.

import { FACE_MIN_HOLD, FACE_RULES, MOTION_RULES, MOTIONS, SPARKLE } from './motion_catalog.js';

const IDS = new Set(MOTIONS.map((m) => m.id));
const RANK = { NORMAL: 0, WARNING: 1, CRITICAL: 2 };
// work events end a breaker cue (the reaction belongs to the moment the level changed)
const CUE_CLEARS = new Set(['turn.start', 'turn.end', 'tool.start', 'reset']);

export function motionCue(ev, state) {
  if (ev.type === 'breaker' && ev.level in RANK && ev.level !== state.breaker) {
    if (RANK[ev.level] > RANK[state.breaker]) return 'breaker.rise';
    if (ev.level === 'NORMAL') return 'breaker.calm';
  }
  return null;
}

// The cue after `ev` (called by model.reduce for every accepted event).
export function nextCue(state, ev, seq) {
  const kind = motionCue(ev, state);
  if (kind) return { kind, seq, type: ev.type };
  if (CUE_CLEARS.has(ev.type)) return null;
  return state.cue || null;
}

export function motionInput(state, pose) {
  return {
    breaker: state.breaker,
    emotion: pose.emotion,
    outcome: state.lastOutcome,
    cue: state.cue ? state.cue.kind : null,
    approval: Boolean(state.approval),
    thinking: Boolean(state.thinking),
    kinds: [...new Set(state.tools.map((t) => t.kind))].sort(),
    gadget: IDS.has(pose['gadget.motion']) ? pose['gadget.motion'] : null,
    test: state.lastTest || null, // pass | fail: the last test of the turn (kept until the next turn)
  };
}

function matches(when, mi) {
  for (const [k, list] of Object.entries(when)) {
    if (k === 'kinds') {
      if (!mi.kinds.some((x) => list.includes(x))) return false;
    } else if (k === 'gadget') {
      if (!mi.gadget || !(list.includes('*') || list.includes(mi.gadget))) return false;
    } else if (!list.includes(mi[k])) return false;
  }
  return true;
}

// -> { id, cause, rule }; id 'none' when no rule matches. causes: derive's causes (for the
// gadget's own reason), ref(field) -> "#seq type".
export function selectMotion(state, pose, causes, ref) {
  const mi = motionInput(state, pose);
  for (let i = 0; i < MOTION_RULES.length; i += 1) {
    const r = MOTION_RULES[i];
    if (!matches(r.when, mi)) continue;
    const id = r.motion === '$gadget' ? mi.gadget : r.motion;
    if (!IDS.has(id)) continue;
    return { id, cause: `${r.why}: ${evidence(r, state, causes, ref)}`, rule: i };
  }
  return { id: 'none', cause: 'no rule matched (rest pose)', rule: -1 };
}

function evidence(rule, state, causes, ref) {
  const keys = Object.keys(rule.when);
  const out = [];
  for (const k of keys) {
    if (k === 'gadget') {
      const c = causes.find((x) => x.channel === 'gadget.motion');
      out.push(c ? c.cause : 'gadget.motion');
    } else if (k === 'cue') out.push(state.cue ? `${state.cue.kind} (#${state.cue.seq} ${state.cue.type})` : 'cue');
    else if (k === 'breaker') out.push(`breaker ${state.breaker} (${ref('breaker')})`);
    else if (k === 'approval') out.push(`approval pending (${ref('approval')})`);
    else if (k === 'thinking') out.push(`turn open (${ref('thinking')})`);
    else if (k === 'kinds') out.push(`${state.tools.map((t) => `${t.kind}:${t.name || t.id}`).join(', ')} (${ref('tools')})`);
    else if (k === 'outcome') out.push(`turn ended ${state.lastOutcome} (${ref('lastOutcome')})`);
    else if (k === 'emotion') out.push(`emotion ${rule.when.emotion.join('/')}`);
    else if (k === 'test') out.push(`its last test ${state.lastTest} (${ref('lastTest')})`);
  }
  return out.join('; ');
}

// The facts FACE_RULES read (pure).
export function faceFacts(state, pose) {
  return {
    breaker: state.breaker,
    cue: state.cue ? state.cue.kind : null,
    vigilant: state.neural.vigilance >= 0.6,
    thinking: Boolean(state.thinking),
    toolError: Boolean(state.thinking && state.lastError),
    outcome: state.lastOutcome,
    test: state.lastTest || null,
    stamp: state.stamp || null,
    fond: state.neural.attachment >= 0.7 && state.neural.trust >= 0.6,
    elated: state.neural.satisfaction >= 0.85,
    emotion: pose.emotion,
  };
}

const NO_FACE = Object.freeze({ id: 'none', ears: 'angle', xfx: '', cause: 'no face rule (the emotion\'s own face)', category: null });

// The face over time (FACE_RULES with their `for`, FACE_MIN_HOLD, `urgent`). update() is called
// once per frame with the time; the same states at the same times give the same faces.
export class FaceHolder {
  constructor() {
    this.cur = null; // the row shown (null: none)
    this.since = -Infinity; // when it started
    this.first = new Map(); // row id -> when its facts began to hold
  }

  update(now, state, pose) {
    const facts = faceFacts(state, pose);
    let want = null;
    for (const r of FACE_RULES) {
      const ok = Object.entries(r.when).every(([k, list]) => list.includes(facts[k]));
      if (!ok) {
        this.first.delete(r.id);
        continue;
      }
      if (!this.first.has(r.id)) this.first.set(r.id, now);
      if (r.for && now - this.first.get(r.id) >= r.for) continue;
      if (!want) want = r;
    }
    const curId = this.cur ? this.cur.id : 'none';
    const wantId = want ? want.id : 'none';
    if (wantId !== curId && (now - this.since >= FACE_MIN_HOLD || (want && want.urgent) || this.since === -Infinity)) {
      this.cur = want;
      this.since = now;
    }
    return faceOf(this.cur, now - this.since);
  }
}

// The frame values of a row at `t` s into it (the sparkle twinkles).
export function faceOf(r, t = 0) {
  if (!r) return NO_FACE;
  const xfx = r.xfx.map((n) => (n === 'sparkle' ? SPARKLE.frames[Math.floor(Math.max(0, t) / SPARKLE.hold) % SPARKLE.frames.length] : n)).join('+');
  return { id: r.id, eyes: r.eyes || null, gaze: r.gaze || null, mouth: r.mouth || null, brows: r.brows || null, xfx, ears: r.ears || 'angle', cause: r.why, category: r.category };
}

// Part of the drift key that the motion depends on beyond the rest of the input key.
export function motionKey(state) {
  return `cue=${state.cue ? state.cue.kind : ''}|test=${state.lastTest || ''}`;
}
