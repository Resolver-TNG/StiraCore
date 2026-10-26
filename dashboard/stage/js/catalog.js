// Animation catalog (#341): declarative data + validation. No code in entries, ever.
//
// An entry is plain JSON: which channels it moves, with which whitelisted curve, for how
// long, and what starts it. The agent may only *select* entries of scope "gesture" by id;
// it cannot add code, and gestures cannot touch the channels that report real state
// (steam, prop lights, neon, shake). New entries come from data reviewed by a human
// (stage 3: hash-pinned packs), never from agent-supplied JavaScript.

import { GADGET_CHANNELS, GADGET_TRIGGERS } from './gadget_catalog.js';
import { AMBIENT_CHANNELS, AMBIENT_MIN_PERIOD } from './room.js';

export const NUMERIC_CHANNELS = {
  'head.x': [-3, 3],
  'head.y': [-3, 3],
  'body.breath': [-2, 2],
  'hair.sway': [-2, 2],
  'ear.l': [-3, 3],
  'ear.r': [-3, 3],
  'arm.y': [-3, 3],
  'cam.x': [-3, 3],
  'cam.y': [-3, 3],
  'steam.puff': [0, 3],
  'sparkle.burst': [0, 1],
  'neon.flicker': [0, 1],
  shake: [0, 4],
  'light.desk.pulse': [0, 1],
  'light.shelf.scan': [0, 1],
  'light.window.pulse': [0, 1],
  ...GADGET_CHANNELS, // props brought in by actions (#341 stage 2), state scope only
  ...AMBIENT_CHANNELS, // the room's few ambient motions (room.js), ambient scope only
};
export const ENUM_CHANNELS = {
  eyes: ['open', 'closed', 'half', 'up', 'happy', 'wide', 'down'],
  mouth: ['neutral', 'smile', 'grin', 'flat', 'frown', 'open'],
};
// Channels each scope may drive. "state" entries run only while their derived state is on.
// idle moves the character only (never the camera or the room: #341 layer separation);
// ambient moves only the listed room channels, slowly (room.js).
export const SCOPE_CHANNELS = {
  idle: ['body.breath', 'hair.sway', 'ear.l', 'ear.r', 'eyes', 'head.x', 'head.y'],
  ambient: Object.keys(AMBIENT_CHANNELS),
  gesture: ['head.x', 'head.y', 'ear.l', 'ear.r', 'arm.y', 'hair.sway', 'eyes', 'mouth'],
  state: Object.keys(NUMERIC_CHANNELS).filter((c) => !(c in AMBIENT_CHANNELS)),
};
export const STATE_TRIGGERS = ['steam', 'light.desk', 'light.shelf', 'light.window', 'shake', 'sparkle', 'choice', ...GADGET_TRIGGERS];
const TRIGGER_VALUE_RE = /^[a-z][a-z0-9-]{0,15}$/; // `is`: run only while the channel has this value
// Derived numbers an idle entry may use as its playback rate (state sets the tempo only).
export const RATE_SOURCES = ['breathRate'];
export const POSE_CONDITIONS = { eyes: ENUM_CHANNELS.eyes, emotion: ['calm', 'curious', 'joy', 'worried', 'tense'] };
const CURVES = ['const', 'sine', 'pulse', 'keys'];
const EASES = ['linear', 'smooth', 'step'];
const ID_RE = /^[a-z][a-z0-9._-]{0,47}$/;
const MAX_KEYS = 32;

const num = (v, lo, hi, dflt) => (typeof v === 'number' && Number.isFinite(v) ? Math.min(hi, Math.max(lo, v)) : dflt);

function validCurve(channel, c) {
  if (!c || typeof c !== 'object' || Array.isArray(c) || !CURVES.includes(c.fn)) return null;
  const isEnum = channel in ENUM_CHANNELS;
  const [lo, hi] = isEnum ? [0, 0] : NUMERIC_CHANNELS[channel];
  const span = Math.max(Math.abs(lo), Math.abs(hi));
  const value = (v) => (isEnum ? (ENUM_CHANNELS[channel].includes(v) ? v : null) : typeof v === 'number' && Number.isFinite(v) ? Math.min(hi, Math.max(lo, v)) : null);
  if (c.fn === 'const') {
    const v = value(c.value);
    return v === null ? null : { fn: 'const', value: v };
  }
  if (isEnum && c.fn !== 'keys') return null;
  if (c.fn === 'sine') return { fn: 'sine', amp: num(c.amp, -span, span, 0), period: num(c.period, 0.05, 60, 1), phase: num(c.phase, 0, 1, 0), offset: num(c.offset, lo, hi, 0) };
  if (c.fn === 'pulse') return { fn: 'pulse', amp: num(c.amp, -span, span, 0), period: num(c.period, 0.05, 60, 1), duty: num(c.duty, 0.01, 1, 0.5) };
  if (!Array.isArray(c.keys) || c.keys.length === 0 || c.keys.length > MAX_KEYS) return null;
  const keys = [];
  for (const k of c.keys) {
    if (!Array.isArray(k) || k.length !== 2) return null;
    const t = num(k[0], 0, 60, null);
    const v = value(k[1]);
    if (t === null || v === null) return null;
    keys.push([t, v]);
  }
  keys.sort((a, b) => a[0] - b[0]);
  const ease = isEnum ? 'step' : EASES.includes(c.ease) ? c.ease : 'linear';
  return { fn: 'keys', keys, ease };
}

function validCondition(cond) {
  if (cond === undefined) return {};
  if (!cond || typeof cond !== 'object' || Array.isArray(cond)) return null;
  const out = {};
  for (const [k, list] of Object.entries(cond)) {
    if (!(k in POSE_CONDITIONS) || !Array.isArray(list)) return null;
    const vals = list.filter((v) => POSE_CONDITIONS[k].includes(v));
    if (vals.length !== list.length) return null;
    out[k] = vals;
  }
  return out;
}

// Returns a frozen, sanitised copy, or null when the entry is not acceptable as data.
export function validateEntry(raw) {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null;
  if (typeof raw.id !== 'string' || !ID_RE.test(raw.id)) return null;
  if (!(raw.scope in SCOPE_CHANNELS)) return null;
  if (!raw.channels || typeof raw.channels !== 'object' || Array.isArray(raw.channels)) return null;
  const channels = {};
  for (const [ch, curve] of Object.entries(raw.channels)) {
    if (!SCOPE_CHANNELS[raw.scope].includes(ch)) return null;
    const c = validCurve(ch, curve);
    if (!c) return null;
    channels[ch] = c;
  }
  if (Object.keys(channels).length === 0) return null;
  let trigger = null;
  if (raw.trigger !== undefined) {
    const t = raw.trigger;
    if (t && Array.isArray(t.every) && t.every.length === 2 && raw.scope === 'idle') {
      const a = num(t.every[0], 0.2, 120, null);
      const b = num(t.every[1], 0.2, 120, null);
      if (a === null || b === null) return null;
      trigger = { every: [Math.min(a, b), Math.max(a, b)] };
    } else if (t && t.always === true && (raw.scope === 'idle' || raw.scope === 'ambient')) {
      trigger = { always: true };
    } else if (t && STATE_TRIGGERS.includes(t.state) && raw.scope === 'state') {
      if (t.is !== undefined && !(typeof t.is === 'string' && TRIGGER_VALUE_RE.test(t.is))) return null;
      trigger = t.is === undefined ? { state: t.state } : { state: t.state, is: t.is };
    } else {
      return null;
    }
  }
  if (raw.scope !== 'gesture' && !trigger) return null;
  if (raw.scope === 'ambient') {
    // ambient: always on, looping, slow; no pulse (a hard on/off reads as an event)
    if (!trigger.always || raw.loop !== true || num(raw.duration, 0, 600, 0) < AMBIENT_MIN_PERIOD) return null;
    for (const c of Object.values(channels)) {
      if (c.fn === 'pulse' || (c.fn === 'sine' && c.period < AMBIENT_MIN_PERIOD)) return null;
    }
  }
  const onlyIf = validCondition(raw.onlyIf);
  if (onlyIf === null) return null;
  if (raw.rateFrom !== undefined && !(RATE_SOURCES.includes(raw.rateFrom) && trigger && trigger.always)) return null;
  const entry = {
    id: raw.id,
    scope: raw.scope,
    description: typeof raw.description === 'string' ? raw.description.slice(0, 160) : '',
    channels,
    duration: num(raw.duration, 0.05, 30, 1),
    loop: raw.loop === true,
    fade: num(raw.fade, 0, 5, 0.25),
    intensity: num(raw.intensity, 0, 1, 1),
    trigger,
    onlyIf,
    rateFrom: raw.rateFrom || null,
  };
  return deepFreeze(entry);
}

function deepFreeze(o) {
  for (const v of Object.values(o)) if (v && typeof v === 'object') deepFreeze(v);
  return Object.freeze(o);
}

export function buildCatalog(entries) {
  const map = new Map();
  const rejected = [];
  for (const raw of entries) {
    const e = validateEntry(raw);
    if (e && !map.has(e.id)) map.set(e.id, e);
    else rejected.push(raw && typeof raw.id === 'string' ? raw.id.slice(0, 48) : '?');
  }
  return { map, rejected };
}

// Built-in catalog. Adding an animation = adding one more object here (or, in stage 3, in a
// human-approved pack). See docs/stage.md "Adding an animation as data".
export const CATALOG = [
  // idle: always-on life signs, they do not encode any state
  { id: 'idle.breath', scope: 'idle', trigger: { always: true }, rateFrom: 'breathRate', loop: true, duration: 4.2, channels: { 'body.breath': { fn: 'sine', amp: 1, period: 4.2 } }, description: 'Chest and shoulders rise by one pixel.' },
  { id: 'idle.hair', scope: 'idle', trigger: { always: true }, loop: true, duration: 5.3, channels: { 'hair.sway': { fn: 'sine', amp: 1, period: 5.3, phase: 0.2 } }, description: 'Hair tips sway.' },
  { id: 'idle.blink', scope: 'idle', trigger: { every: [2.4, 5.6] }, duration: 0.16, onlyIf: { eyes: ['open', 'wide', 'up', 'half', 'down'] }, channels: { eyes: { fn: 'keys', keys: [[0, 'closed']] } }, description: 'Blink.' },
  { id: 'idle.ear-twitch', scope: 'idle', trigger: { every: [5, 11] }, duration: 0.45, channels: { 'ear.r': { fn: 'keys', keys: [[0, 0], [0.08, 2], [0.2, -1], [0.45, 0]], ease: 'smooth' } }, description: 'Right ear flicks.' },
  // state: run only while the derived state is on (fed by real events)
  { id: 'state.steam', scope: 'state', trigger: { state: 'steam' }, loop: true, duration: 2, fade: 0.6, channels: { 'steam.puff': { fn: 'sine', amp: 0.3, period: 2, offset: 1 } }, description: 'Thin mug steam while a turn is open.' },
  { id: 'state.desk', scope: 'state', trigger: { state: 'light.desk' }, loop: true, duration: 0.9, channels: { 'light.desk.pulse': { fn: 'pulse', amp: 1, period: 0.9, duty: 0.55 } }, description: 'Instrument pulse while a tool runs.' },
  { id: 'state.shelf', scope: 'state', trigger: { state: 'light.shelf' }, loop: true, duration: 1.6, channels: { 'light.shelf.scan': { fn: 'keys', keys: [[0, 0], [1.6, 1]] } }, description: 'Shelf scan while memory is read.' },
  { id: 'state.window', scope: 'state', trigger: { state: 'light.window' }, loop: true, duration: 1.2, channels: { 'light.window.pulse': { fn: 'sine', amp: 0.5, period: 1.2, offset: 0.5 } }, description: 'Window glow while the outside world is fetched.' },
  { id: 'state.shake', scope: 'state', trigger: { state: 'shake' }, loop: true, duration: 0.5, fade: 0.1, channels: { shake: { fn: 'pulse', amp: 1, period: 0.5, duty: 0.4 }, 'neon.flicker': { fn: 'pulse', amp: 1, period: 1.7, duty: 0.08 } }, description: 'Small screen shake and neon flicker at CRITICAL.' },
  { id: 'state.sparkle', scope: 'state', trigger: { state: 'sparkle' }, loop: true, duration: 1.4, channels: { 'sparkle.burst': { fn: 'keys', keys: [[0, 0], [0.2, 1], [1.4, 0]], ease: 'smooth' } }, description: 'Sparkles after a turn ended well.' },
  // gesture: the agent may pick these by id
  { id: 'gesture.nod', scope: 'gesture', duration: 0.7, channels: { 'head.y': { fn: 'keys', keys: [[0, 0], [0.18, 2], [0.4, 0], [0.55, 1], [0.7, 0]], ease: 'smooth' } }, description: 'Small nod.' },
  { id: 'gesture.ear-perk', scope: 'gesture', duration: 0.8, channels: { 'ear.l': { fn: 'keys', keys: [[0, 0], [0.12, -2], [0.6, -2], [0.8, 0]], ease: 'smooth' }, 'ear.r': { fn: 'keys', keys: [[0, 0], [0.12, -2], [0.6, -2], [0.8, 0]], ease: 'smooth' } }, description: 'Both ears perk up.' },
  { id: 'gesture.sip', scope: 'gesture', duration: 1.6, channels: { 'arm.y': { fn: 'keys', keys: [[0, 0], [0.4, -3], [1.2, -3], [1.6, 0]], ease: 'smooth' }, eyes: { fn: 'keys', keys: [[0.4, 'closed'], [1.2, 'open']] } }, description: 'Lift the mug for a sip.' },
  { id: 'gesture.glance-up', scope: 'gesture', duration: 1.2, channels: { eyes: { fn: 'keys', keys: [[0, 'up'], [1.2, 'open']] }, 'head.y': { fn: 'keys', keys: [[0, 0], [0.2, -1], [1, -1], [1.2, 0]], ease: 'smooth' } }, description: 'Look up for a moment.' },
];
