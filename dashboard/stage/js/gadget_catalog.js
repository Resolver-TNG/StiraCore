// Gadget catalog (#341, stage 2): declarative data for the props that actions bring in.
//
// Two kinds of entries, both plain data, validated and frozen before use:
//   GADGET_ANIMS — animation entries for the existing interpreter (scope "state" only:
//                  they run while a derived gadget channel is on, never on agent request).
//   GADGETS      — where a prop stands, which derived channel shows it, its parallax depth,
//                  its light pool, and which built-in program draws it (a whitelisted name;
//                  the programs are human-written code in gadget_scene.js).
// Nothing in here is ever evaluated. Adding a prop = adding data (and, for a new look, a
// sprite in tools/build_gadgets.py). Agent-supplied JavaScript is never run (docs/stage.md).

// Numeric channels the gadget animations may drive (merged into catalog.NUMERIC_CHANNELS).
export const GADGET_CHANNELS = {
  'laptop.type': [0, 1],
  'lamp.blink': [0, 1],
  'drone.bob': [-2, 2],
  'beacon.spin': [0, 1],
  'beacon.pulse': [0, 1],
  'gadget.glitch': [0, 1],
  'stamp.y': [-16, 0],
  'stamp.seal': [0, 1],
  'bell.ring': [-2, 2],
  'sand.t': [0, 1],
  'box.slide': [0, 16],
  'magnifier.x': [-4, 4],
  'magnifier.y': [-4, 4],
  'folder.out': [0, 8],
  'note.press': [0, 1],
};

// Derived channels (gadget_model.js) that may start a gadget animation or show a gadget.
export const GADGET_TRIGGERS = [
  'gadget.laptop', 'gadget.typing', 'gadget.lamp', 'gadget.drones', 'gadget.drones.running',
  'gadget.folder', 'gadget.note', 'gadget.shelf.row', 'gadget.magnifier', 'gadget.stamp',
  'gadget.beacon', 'gadget.hourglass', 'gadget.box', 'gadget.glitch', 'gadget.drones.cue',
  'gadget.motion',
];

// --- motions that come with the props (contract with the motion catalog) -----------------------
// The character's motions are defined in the motion catalog (a separate part of stage 2). The
// gadgets only *name* the motion that belongs with them, by id; deriveGadgets() sets
// `gadget.motion` to the first cue below that matches, and a motion entry connects with
// `trigger: { state: 'gadget.motion', is: '<id>' }`. An id that no entry uses plays nothing,
// so she keeps her current pose (fallback) until that motion exists.
export const MOTION_IDS = ['typing', 'look_screen', 'delegate_point', 'receive_report', 'ponder', 'head_tilt', 'sip', 'alert', 'relief', 'slump'];
const CUE_LIST = [
  { when: 'gadget.beacon', motion: 'alert' }, // breaker WARNING / CRITICAL
  { when: 'gadget.glitch', motion: 'slump' }, // a tool (or test) failed: its prop flickers
  { when: 'gadget.drones.cue', is: 'err', motion: 'slump' }, // a subagent failed
  { when: 'gadget.stamp', is: 'deny', motion: 'slump' },
  { when: 'gadget.typing', motion: 'typing' }, // code tool running: laptop + hand on the keys
  { when: 'gadget.lamp', is: 'run', motion: 'look_screen' }, // tests running
  { when: 'gadget.drones.cue', is: 'spawn', motion: 'delegate_point' }, // a drone was sent
  { when: 'gadget.drones.cue', is: 'done', motion: 'receive_report' }, // a drone came back
  { when: 'gadget.lamp', is: 'pass', motion: 'relief' },
  { when: 'gadget.stamp', is: 'approve', motion: 'relief' },
  { when: 'gadget.laptop', motion: 'look_screen' },
  { when: 'gadget.magnifier', motion: 'head_tilt' },
];
const CUE_VALUE_RE = /^[a-z][a-z0-9-]{0,15}$/;
export function validateCue(c) {
  if (!c || typeof c !== 'object' || !GADGET_TRIGGERS.includes(c.when) || c.when === 'gadget.motion') return null;
  if (!MOTION_IDS.includes(c.motion)) return null;
  if (c.is !== undefined && !(typeof c.is === 'string' && CUE_VALUE_RE.test(c.is))) return null;
  return Object.freeze(c.is === undefined ? { when: c.when, motion: c.motion } : { when: c.when, is: c.is, motion: c.motion });
}
export const MOTION_CUES = Object.freeze(CUE_LIST.map(validateCue).filter(Boolean));

// Motions in which she faces the desk behind her (motion_catalog.js): the laptop moves there.
const BACK_VIEW = ['typing_back', 'watch_back'];
const COND_KEYS = ['motion'];
const COND_RE = /^[a-z][a-z0-9_]{0,31}$/;
function validCond(c) {
  if (!c || typeof c !== 'object' || Array.isArray(c)) return null;
  const out = {};
  for (const [k, list] of Object.entries(c)) {
    if (!COND_KEYS.includes(k) || !Array.isArray(list) || list.length > 16 || !list.every((v) => typeof v === 'string' && COND_RE.test(v))) return null;
    out[k] = [...list];
  }
  return out;
}
// `when` / `unless` of an entry against the derived pose (data: { motion: [ids] }).
export function gadgetCondition(g, pose) {
  const hit = (c) => Object.entries(c).every(([k, list]) => list.includes(pose[k]));
  return (!g.when || hit(g.when)) && !(g.unless && hit(g.unless));
}

// Built-in drawing programs (gadget_scene.js). Data picks one by name; unknown names are refused.
export const GADGET_PROGRAMS = ['laptop', 'stacklight', 'drones', 'beacon', 'magnifier', 'folder', 'note', 'stamp', 'hourglass', 'bell', 'box'];
export const GADGET_LAYERS = ['back', 'shelf', 'desk', 'front'];
export const LIGHT_COLORS = ['screen', 'lamp', 'beacon', 'cyan', 'amber', 'green', 'red', 'violet', 'paper'];

// --- animations (data for anim.js; scope "state") -----------------------------------------------

const K = (keys, ease = 'linear') => ({ fn: 'keys', keys, ease });

export const GADGET_ANIMS = [
  { id: 'state.laptop-type', scope: 'state', trigger: { state: 'gadget.typing' }, loop: true, duration: 0.6, fade: 0.05, channels: { 'laptop.type': { fn: 'pulse', amp: 1, period: 0.6, duty: 0.8 } }, description: 'Keys clatter and code is typed while a code tool runs.' },
  { id: 'state.lamp-run', scope: 'state', trigger: { state: 'gadget.lamp', is: 'run' }, loop: true, duration: 0.7, fade: 0.01, channels: { 'lamp.blink': { fn: 'pulse', amp: 1, period: 0.7, duty: 0.55 } }, description: 'Amber lamp blinks while tests run.' },
  { id: 'state.drone-bob', scope: 'state', trigger: { state: 'gadget.drones.running' }, loop: true, duration: 1.3, fade: 0.2, channels: { 'drone.bob': { fn: 'sine', amp: 1, period: 1.3 } }, description: 'Running drones hover.' },
  { id: 'state.beacon-spin', scope: 'state', trigger: { state: 'gadget.beacon', is: 'critical' }, loop: true, duration: 0.9, fade: 0.01, channels: { 'beacon.spin': K([[0, 0], [0.9, 1]]) }, description: 'Red beacon turns at CRITICAL.' },
  { id: 'state.beacon-pulse', scope: 'state', trigger: { state: 'gadget.beacon', is: 'warning' }, loop: true, duration: 2.4, fade: 0.3, channels: { 'beacon.pulse': { fn: 'sine', amp: 0.5, period: 2.4, offset: 0.5 } }, description: 'Beacon glows slowly at WARNING.' },
  { id: 'state.glitch', scope: 'state', trigger: { state: 'gadget.glitch' }, duration: 0.9, fade: 0.01, channels: { 'gadget.glitch': K([[0, 1], [0.07, 0], [0.14, 1], [0.24, 0.5], [0.34, 0], [0.5, 1], [0.58, 0], [0.9, 0]], 'step') }, description: 'The prop of a failed tool flickers once.' },
  { id: 'state.stamp', scope: 'state', trigger: { state: 'gadget.stamp' }, duration: 1.3, fade: 0.01, channels: { 'stamp.y': K([[0, -13], [0.32, -13], [0.44, 0], [0.7, 0], [1.0, -14]], 'smooth'), 'stamp.seal': K([[0, 0], [0.44, 1]], 'step') }, description: 'The seal comes down once on the slip and stays.' },
  { id: 'state.bell', scope: 'state', trigger: { state: 'gadget.hourglass' }, duration: 1.0, fade: 0.01, channels: { 'bell.ring': K([[0, 0], [0.08, -1], [0.16, 1], [0.24, -1], [0.32, 1], [0.44, -1], [0.56, 0]], 'step') }, description: 'The bell rings once when a scheduled job starts.' },
  { id: 'state.sand', scope: 'state', trigger: { state: 'gadget.hourglass' }, loop: true, duration: 8, fade: 0.01, channels: { 'sand.t': K([[0, 0], [8, 1]]) }, description: 'Sand runs while the job runs.' },
  { id: 'state.box', scope: 'state', trigger: { state: 'gadget.box' }, duration: 1.4, fade: 0.01, channels: { 'box.slide': K([[0, 14], [0.2, 14], [1.4, 0]], 'smooth') }, description: 'The archive box slides to the back of the shelf.' },
  { id: 'state.magnifier', scope: 'state', trigger: { state: 'gadget.magnifier' }, loop: true, duration: 3.2, fade: 0.3, channels: { 'magnifier.x': { fn: 'sine', amp: 3, period: 3.2 }, 'magnifier.y': { fn: 'sine', amp: 2, period: 3.2, phase: 0.25 } }, description: 'The magnifier scans the window.' },
  { id: 'state.folder', scope: 'state', trigger: { state: 'gadget.folder' }, duration: 0.6, fade: 0.01, channels: { 'folder.out': K([[0, 0], [0.6, 7]], 'smooth') }, description: 'A binder slides out of the shelf.' },
  { id: 'state.note', scope: 'state', trigger: { state: 'gadget.note' }, loop: true, duration: 1.6, fade: 0.01, channels: { 'note.press': K([[0, 0], [0.5, 0], [0.62, 1], [1.1, 1], [1.2, 0]], 'step') }, description: 'A sticky note is pressed on while a write runs.' },
];

// --- props (data for gadget_scene.js) -----------------------------------------------------------
// at: image coordinates of the sprite's top-left (640x340 room). floor: true puts the sprite's
// bottom `at[1]` pixels below the visible floor line (text window top, or the view bottom).
// depth: parallax factor (1 = the character). layer: back (behind her), shelf, front.
// narrow: other placement (or hide) when the view is narrower than the room's props (phone
// portrait shows about 330 of the 640 columns: the window sill and the right wall are cut).

export const GADGETS = [
  { id: 'laptop', show: 'gadget.laptop', unless: { motion: BACK_VIEW }, program: 'laptop', sprite: 'laptop', at: [188, 4], floor: true, depth: 1.18, layer: 'front', light: { at: [46, 22], r: 42, color: 'screen', level: 0.9 }, description: 'Laptop on the desk while code or tests run in the turn.' },
  { id: 'stacklight', show: 'gadget.laptop', unless: { motion: BACK_VIEW }, program: 'stacklight', sprite: 'stacklight', at: [174, 1], floor: true, depth: 1.18, layer: 'front', light: { at: [5, 13], r: 20, color: 'lamp', level: 0.9 }, description: 'Signal lamp: amber while tests run, green pass, red fail.' },
  // while she has turned to work at the desk behind her (motion typing_back / watch_back), the
  // same laptop and lamp stand on the room's desk, screen toward her (and us)
  { id: 'monitor', show: 'gadget.laptop', when: { motion: BACK_VIEW }, program: 'laptop', sprite: 'laptop', at: [412, 148], depth: 0.8, layer: 'desk', narrow: { at: [398, 148] }, light: { at: [46, 22], r: 42, color: 'screen', level: 0.9 }, description: 'The laptop on the room desk, facing her, while she types with her back to us.' },
  { id: 'stacklight_back', show: 'gadget.laptop', when: { motion: BACK_VIEW }, program: 'stacklight', sprite: 'stacklight', at: [516, 184], depth: 0.8, layer: 'desk', narrow: { at: [490, 184] }, light: { at: [5, 13], r: 20, color: 'lamp', level: 0.9 }, description: 'The signal lamp next to it.' },
  { id: 'drones', show: 'gadget.drones', program: 'drones', sprite: 'drone.0', at: [318, 166], depth: 1.3, layer: 'front', slots: [[228, 94], [392, 88], [244, 46], [378, 40], [222, 140], [392, 134]], dock: [362, 148], docks: [[196, 136], [414, 136]], centerX: 320, light: { at: [12, 20], r: 11, color: 'cyan', level: 0.8 }, description: 'One drone per subagent; returns and reports, stays and warns on failure.' },
  { id: 'beacon', show: 'gadget.beacon', program: 'beacon', sprite: 'beacon', at: [388, 16], depth: 0.36, layer: 'back', light: { at: [8, 9], r: 64, color: 'beacon', level: 0.85 }, description: 'Warning beacon on the pipe at WARNING / CRITICAL.' },
  { id: 'magnifier', show: 'gadget.magnifier', program: 'magnifier', sprite: 'magnifier', at: [398, 72], depth: 0.6, layer: 'back', light: { at: [11, 11], r: 20, color: 'cyan', level: 0.6 }, description: 'Magnifier at the window while the web is searched or fetched.' },
  { id: 'folder', show: 'gadget.folder', program: 'folder', sprite: 'folder', at: [188, 190], depth: 0.55, layer: 'shelf', description: 'A binder pulled from the shelf while memory or files are read.' },
  { id: 'note', show: 'gadget.note', program: 'note', sprite: 'note', at: [196, 98], depth: 0.55, layer: 'shelf', description: 'Sticky note pressed on the shelf while memory or files are written.' },
  { id: 'stamp', show: 'gadget.stamp', program: 'stamp', sprite: 'slip', at: [486, 152], depth: 0.5, layer: 'back', narrow: { at: [452, 142] }, description: 'Approval slip pinned on the wall, sealed approve / deny, for the rest of the turn.' },
  { id: 'hourglass', show: 'gadget.hourglass', program: 'hourglass', sprite: 'hourglass', at: [508, 123], depth: 0.42, layer: 'back', narrow: { at: [462, 123] }, description: 'Hourglass on the window sill while a scheduled job runs.' },
  { id: 'bell', show: 'gadget.hourglass', program: 'bell', sprite: 'bell', at: [490, 134], depth: 0.42, layer: 'back', narrow: { at: [446, 134] }, description: 'Desk bell; rings once when the job starts.' },
  { id: 'box', show: 'gadget.box', program: 'box', sprite: 'box', at: [140, 18], depth: 0.55, layer: 'shelf', narrow: { at: [160, 18] }, description: 'Archive box at the back of the shelf: forgotten, restorable.' },
];

const ID_RE = /^[a-z][a-z0-9._-]{0,31}$/;
const SPRITE_RE = /^[a-z][a-z0-9.]{0,23}$/;
const num = (v, lo, hi) => (typeof v === 'number' && Number.isFinite(v) ? Math.min(hi, Math.max(lo, v)) : null);
const pt = (p, lo = [-64, -64], hi = [704, 404]) => (Array.isArray(p) && p.length === 2 && num(p[0], lo[0], hi[0]) !== null && num(p[1], lo[1], hi[1]) !== null ? [num(p[0], lo[0], hi[0]), num(p[1], lo[1], hi[1])] : null);

// Returns a frozen copy, or null when the entry is not acceptable as data.
// `sprites`: names present in the atlas (gadgets.json), so a prop cannot point anywhere else.
export function validateGadget(raw, sprites) {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null;
  if (typeof raw.id !== 'string' || !ID_RE.test(raw.id)) return null;
  if (!GADGET_TRIGGERS.includes(raw.show)) return null;
  if (!GADGET_PROGRAMS.includes(raw.program)) return null;
  if (typeof raw.sprite !== 'string' || !SPRITE_RE.test(raw.sprite) || (sprites && !sprites.includes(raw.sprite))) return null;
  if (!GADGET_LAYERS.includes(raw.layer)) return null;
  const at = pt(raw.at);
  if (!at) return null;
  const depth = num(raw.depth, 0, 1.6);
  if (depth === null) return null;
  const out = { id: raw.id, show: raw.show, program: raw.program, sprite: raw.sprite, layer: raw.layer, at, depth, floor: raw.floor === true, description: typeof raw.description === 'string' ? raw.description.slice(0, 160) : '' };
  for (const k of ['when', 'unless']) {
    if (raw[k] === undefined) continue;
    const c = validCond(raw[k]);
    if (!c) return null;
    out[k] = c;
  }
  if (raw.light !== undefined) {
    const l = raw.light;
    if (!l || typeof l !== 'object' || !pt(l.at, [-64, -64], [128, 128]) || !LIGHT_COLORS.includes(l.color)) return null;
    const r = num(l.r, 2, 64);
    const level = num(l.level, 0, 1);
    if (r === null || level === null) return null;
    out.light = { at: pt(l.at, [-64, -64], [128, 128]), r, color: l.color, level };
  }
  if (raw.slots !== undefined) {
    if (!Array.isArray(raw.slots) || raw.slots.length === 0 || raw.slots.length > 8) return null;
    const slots = raw.slots.map((p) => pt(p));
    if (slots.some((p) => !p)) return null;
    out.slots = slots;
  }
  if (raw.dock !== undefined) {
    out.dock = pt(raw.dock);
    if (!out.dock) return null;
  }
  // docks: [left, right] — a drone launches from and lands beside her on its slot's side
  // (split at centerX, image x), so it does not cross her face or cover her chest and hands
  if (raw.docks !== undefined) {
    if (!Array.isArray(raw.docks) || raw.docks.length !== 2) return null;
    out.docks = raw.docks.map((p) => pt(p));
    if (out.docks.some((p) => !p)) return null;
    out.centerX = num(raw.centerX === undefined ? 320 : raw.centerX, 0, 1024);
    if (out.centerX === null) return null;
  }
  if (raw.narrow !== undefined) {
    const n = raw.narrow;
    if (!n || typeof n !== 'object' || Array.isArray(n)) return null;
    const nw = { hide: n.hide === true };
    if (n.at !== undefined && !(nw.at = pt(n.at))) return null;
    if (n.dock !== undefined && !(nw.dock = pt(n.dock))) return null;
    if (n.slots !== undefined) {
      if (!Array.isArray(n.slots) || !out.slots || n.slots.length !== out.slots.length) return null;
      nw.slots = n.slots.map((p) => pt(p));
      if (nw.slots.some((p) => !p)) return null;
    }
    out.narrow = nw;
  }
  return deepFreeze(out);
}

function deepFreeze(o) {
  for (const v of Object.values(o)) if (v && typeof v === 'object') deepFreeze(v);
  return Object.freeze(o);
}

export function buildGadgets(entries, sprites) {
  const list = [];
  const rejected = [];
  const seen = new Set();
  for (const raw of entries) {
    const g = validateGadget(raw, sprites);
    if (g && !seen.has(g.id)) {
      seen.add(g.id);
      list.push(g);
    } else rejected.push(raw && typeof raw.id === 'string' ? raw.id.slice(0, 32) : '?');
  }
  return { list, rejected };
}
