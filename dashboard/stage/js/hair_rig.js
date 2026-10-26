// Hair rig (#341): secondary motion of the hair parts. Pure functions, no DOM, no randomness.
//
// The hair is cut into parts and segments by tools/build_hair.py (assets/char/hair.json:
// parts, chains, segments with their parent). Each segment hangs from its parent (a root
// segment from the head) on a damped spring that sees its anchor a little late, so the hair
// lags behind the head (follow-through), more toward the tips, and springs back with a small
// overshoot. Offsets snap to whole pixels.
//
// Inputs per frame: the head offset the character layer applied (px), the breath offset (px),
// the idle `hair.sway` channel and the derived pose (model.derive). Same input sequence, same
// offsets: the step is fixed (1/240 s), the input is interpolated inside a frame, and nothing is
// random. The look per state (HAIR_STYLES) comes only from the derived pose.
//
// In phase (#341, night round): only a change of the head pose goes through the springs. The
// breath and the idle sway are added after them as one whole-pixel offset shared by every part
// that takes it, so the hair rises with the chest and sways as one piece in the same frame,
// instead of row bands switching one after the other (it read as a wave running down her). The
// hair over the face (the bangs, the side locks beside the cheeks) follows the head exactly;
// only the free tips trail a head move, a pixel per segment, without swinging past.
//
// Tuning is data (HAIR_RIG, HAIR_STYLES): validated and clamped, never code.

import { EXPRESSIONS } from './model.js';

export const STEP = 1 / 240;
const MAX_STEPS = 60; // a long frame (tab in background) is not replayed in full
const HISTORY = 128; // substeps of anchor history (0.53 s): the longest delay

// Per part; arrays are per segment depth (root, then toward the tip; the last value repeats).
//   follow    share of the parent's motion the segment keeps (the rest stays with the body)
//   delay     s the segment sees its anchor late (phase lag, grows toward the tip)
//   maxAmp    px the segment may be away from where it hangs (the cap of the swing)
//   stiffness 1/s^2 of the spring that pulls it back; damping ratio per depth (< 1 bounces)
//   sway      > 0: the segment takes the shared idle sway (whole pixels, same frame for all)
//   overArm   drawn in front of the arm and the mug (default: behind them, as in the icon)
export const HAIR_RIG = {
  // over the face: exactly with the head (no lag, no sway), so the fringe never slips off the
  // face it was cut from when the head pose changes (the thinking pose, a tilt)
  bangs: { follow: [1, 1, 1], delay: [0, 0, 0], maxAmp: [0, 0, 0], stiffness: 260, damping: [1, 1, 1], sway: [0, 0, 0] },
  // beside the cheeks: root and the lock along the face with the head; the two lower segments
  // trail a head move by a pixel (critically damped: no swing past) and take the idle sway
  side_l: { follow: [1, 1, 1, 0.95], delay: [0, 0, 0.03, 0.06], maxAmp: [0, 0, 1, 1], stiffness: 170, damping: [1, 1, 0.9, 0.8], sway: [0, 0, 1, 1] },
  side_r: { follow: [1, 1, 1, 0.95], delay: [0, 0, 0.03, 0.06], maxAmp: [0, 0, 1, 1], stiffness: 170, damping: [1, 1, 0.9, 0.8], sway: [0, 0, 1, 1] },
  // behind the shoulders: keeps part of a head move (the rest hangs with the body)
  back: { follow: [0.8, 0.7, 0.6], delay: [0.02, 0.04, 0.06], maxAmp: [1, 1, 1], stiffness: 90, damping: [1, 0.9, 0.8], sway: [0, 1, 1] },
  wisps: { follow: [1, 1], delay: [0, 0.03], maxAmp: [0, 1], stiffness: 320, damping: [1, 0.8], sway: [0, 1] },
  // back view set (hair_back.json, tools/char_back.py): the locks down her back; the lower
  // segments trail the head by a pixel as she types and sway with the rest
  locks: { follow: [1, 1, 0.95, 0.9], delay: [0, 0, 0.04, 0.07], maxAmp: [0, 0, 1, 1], stiffness: 140, damping: [1, 1, 0.9, 0.8], sway: [0, 0, 1, 1] },
  locks_side: { follow: [1, 1, 0.95, 0.9], delay: [0, 0, 0.04, 0.07], maxAmp: [0, 0, 1, 1], stiffness: 160, damping: [1, 1, 0.9, 0.8], sway: [0, 0, 1, 1] },
};

// The shared idle sway: px per unit of the `hair.sway` channel (whole pixels, with hysteresis).
export const SWAY_PX = 1;

// Looks per derived state. lift: per part, per depth [outward, down] px relative to the parent
// (so it adds up toward the tip; outward follows the chain's side, the bangs have no side). kick: px/s given once to the tips when the style
// starts. stiffness / damping: multipliers.
export const HAIR_STYLES = {
  calm: { description: 'Rest.', lift: {}, kick: [0, 0], stiffness: 1, damping: 1 },
  perk: { description: 'Curious: the wisps lift a pixel.', lift: { wisps: [[0, 0], [0, -1]] }, kick: [0, 0], stiffness: 1.1, damping: 1 },
  bounce: { description: 'Joy: the hair hops once and the wisps stay up.', lift: { wisps: [[0, -1], [0, 0]] }, kick: [0, -55], stiffness: 1, damping: 0.9 },
  droop: {
    description: 'Worried: tips hang down and in, the wisps fall, the hair is heavier.',
    lift: { wisps: [[-1, 1], [-1, 1]], side_l: [[0, 0], [0, 0], [-1, 1], [0, 0]], side_r: [[0, 0], [0, 0], [-1, 1], [0, 0]], back: [[0, 0], [0, 1], [0, 0]] },
    kick: [0, 0],
    stiffness: 0.7,
    damping: 1.2,
  },
  alert: { description: 'Vigilant: the wisps stand up and out.', lift: { wisps: [[1, -1], [1, -1]] }, kick: [0, -25], stiffness: 1.4, damping: 1 },
  bristle: {
    description: 'Tense: the wisps stand, the tips flare out, the hair is stiff and quivers.',
    lift: { wisps: [[1, -1], [1, -1]], side_l: [[0, 0], [0, 0], [1, 0], [0, 0]], side_r: [[0, 0], [0, 0], [1, 0], [0, 0]] },
    kick: [0, -30],
    stiffness: 1.8,
    damping: 0.6,
  },
};

const vigilant = (pose) => {
  const ex = EXPRESSIONS[pose.emotion];
  return Boolean(ex) && typeof pose.ears === 'number' && pose.ears < ex.ears;
};

// Hair style from the derived pose (first match wins). Documented in docs/stage.md.
export const HAIR_STYLE_RULES = [
  { style: 'bristle', when: 'emotion is tense', test: (p) => p.emotion === 'tense' },
  { style: 'droop', when: 'emotion is worried', test: (p) => p.emotion === 'worried' },
  { style: 'alert', when: 'ears folded back by vigilance', test: vigilant },
  { style: 'bounce', when: 'emotion is joy', test: (p) => p.emotion === 'joy' },
  { style: 'perk', when: 'emotion is curious', test: (p) => p.emotion === 'curious' },
  { style: 'calm', when: 'otherwise', test: () => true },
];

export function hairStyle(pose) {
  const rule = HAIR_STYLE_RULES.find((r) => r.test(pose || {}));
  return { style: rule.style, cause: rule.when };
}

// derive() result + the hair style as one more derived channel (so the drift monitor and the
// raw view see it, with its cause).
export function withHairStyle(derived) {
  const { style, cause } = hairStyle(derived.pose);
  const why = derived.causes.find((c) => c.channel === 'emotion');
  return {
    ...derived,
    pose: { ...derived.pose, hair: style },
    causes: [...derived.causes, { channel: 'hair', value: style, cause: `${cause}${why ? ` ← ${why.cause}` : ''}` }],
  };
}

// --- validation ---------------------------------------------------------------------------

const num = (v, lo, hi, dflt) => (typeof v === 'number' && Number.isFinite(v) ? Math.min(hi, Math.max(lo, v)) : dflt);
const list = (v, lo, hi, dflt, n, int = false) => {
  const src = Array.isArray(v) && v.length ? v : [dflt];
  return Array.from({ length: n }, (_, i) => {
    const x = num(src[Math.min(i, src.length - 1)], lo, hi, dflt);
    return int ? Math.round(x) : x;
  });
};
const pair = (v, lo, hi) => (Array.isArray(v) && v.length === 2 ? [num(v[0], lo, hi, 0), num(v[1], lo, hi, 0)] : [0, 0]);
const deepFreeze = (o) => {
  for (const v of Object.values(o)) if (v && typeof v === 'object') deepFreeze(v);
  return Object.freeze(o);
};

function validStyles(raw, partIds) {
  const out = {};
  for (const [id, s] of Object.entries(raw || {})) {
    if (!/^[a-z][a-z0-9_-]{0,23}$/.test(id) || !s || typeof s !== 'object' || Array.isArray(s)) continue;
    const lift = {};
    for (const [p, per] of Object.entries(s.lift || {})) if (partIds.includes(p) && Array.isArray(per)) lift[p] = per.slice(0, 8).map((d) => pair(d, -3, 3).map(Math.round));
    out[id] = {
      description: typeof s.description === 'string' ? s.description.slice(0, 160) : '',
      lift,
      kick: pair(s.kick, -120, 120),
      stiffness: num(s.stiffness, 0.3, 3, 1),
      damping: num(s.damping, 0.3, 3, 1),
    };
  }
  if (!out.calm) out.calm = { description: '', lift: {}, kick: [0, 0], stiffness: 1, damping: 1 };
  return out;
}

// geometry: hair.json. Returns the frozen rig: segments in update order (parents first).
export function createHairRig(geometry, tuning = HAIR_RIG, styles = HAIR_STYLES) {
  const segs = Array.isArray(geometry && geometry.segments) ? geometry.segments : [];
  const parts = Array.isArray(geometry && geometry.parts) ? geometry.parts.map((p) => p.id) : [];
  const ids = new Set(segs.map((s) => s.id));
  const rejected = [];
  const depthCount = new Map();
  for (const s of segs) depthCount.set(s.chain, Math.max(depthCount.get(s.chain) || 0, s.depth + 1));
  const out = [];
  for (const s of segs) {
    const t = tuning[s.part];
    if (!t || typeof t !== 'object' || (s.parent !== 'head' && !ids.has(s.parent))) {
      rejected.push(s.id);
      continue;
    }
    const n = depthCount.get(s.chain);
    const d = s.depth;
    out.push({
      id: s.id,
      part: s.part,
      parent: s.parent,
      depth: d,
      side: num(s.side, -1, 1, 0),
      breath: num(s.breath, 0, 1, 1),
      follow: list(t.follow, 0, 1, 1, n)[d],
      delay: list(t.delay, 0, (HISTORY - 2) * STEP, 0, n)[d],
      maxAmp: list(t.maxAmp, 0, 4, 1, n, true)[d],
      sway: list(t.sway, -2, 2, 0, n)[d],
      stiffness: num(t.stiffness, 5, 600, 80),
      damping: list(t.damping, 0.05, 2, 0.4, n)[d],
      tip: d === n - 1,
      overArm: Array.isArray(t.overArm) ? t.overArm[Math.min(d, t.overArm.length - 1)] === true : false,
    });
  }
  // parents before children (stable for the same geometry)
  const order = [];
  const placed = new Set(['head']);
  let guard = 0;
  while (order.length < out.length && guard < 64) {
    for (const s of out) if (!placed.has(s.id) && placed.has(s.parent)) {
      order.push(s);
      placed.add(s.id);
    }
    guard += 1;
  }
  return deepFreeze({ segments: order, index: Object.fromEntries(order.map((s, i) => [s.id, i])), styles: validStyles(styles, parts), rejected });
}

// --- dynamics -----------------------------------------------------------------------------

// the springs see the head only; the breath and the sway are added afterwards, in phase
function anchorOf(rig, s, head, pos) {
  const parent = s.parent === 'head' ? head : pos[rig.index[s.parent]];
  return [s.follow * parent[0], s.follow * parent[1]];
}

// whole-pixel shared offsets added to every segment that takes them (same frame for all)
// (the breath lifts her upper body and head by a pixel, so all of the hair rises with it)
function sharedOf(s, breathPx, swayPx) {
  return [s.sway > 0 ? swayPx : 0, breathPx];
}

function liftOf(style, s) {
  const per = style.lift[s.part];
  if (!per || !per.length) return [0, 0];
  const [o, dn] = per[Math.min(s.depth, per.length - 1)];
  return [o * (s.side || 0), dn];
}

export function initHairState(rig, input = {}) {
  const head = [num(input.head && input.head[0], -50, 50, 0), num(input.head && input.head[1], -50, 50, 0)];
  const breath = num(input.breath, -10, 10, 0);
  const sway = num(input.sway, -4, 4, 0);
  const pos = [];
  for (const s of rig.segments) pos.push(anchorOf(rig, s, head, pos));
  const swayPx = Math.round(sway * SWAY_PX);
  const breathPx = Math.round(breath);
  const dyn = pos.map((p) => [Math.round(p[0]), Math.round(p[1])]);
  return {
    acc: 0,
    style: input.style && rig.styles[input.style] ? input.style : 'calm',
    last: { head, breath, sway },
    pos,
    vel: rig.segments.map(() => [0, 0]),
    // anchor history: segment i, substep k -> hist[(i * HISTORY + k) * 2 + axis] (ring buffer at `at`)
    hist: Float64Array.from({ length: rig.segments.length * HISTORY * 2 }, (_, j) => pos[Math.floor(j / (HISTORY * 2))][j % 2]),
    at: 0,
    snap: dyn.map((p) => [...p]),
    dyn,
    swayPx,
    out: rig.segments.map((s, i) => {
      const sh = sharedOf(s, breathPx, swayPx);
      return [dyn[i][0] + sh[0], dyn[i][1] + sh[1]];
    }),
  };
}

const clone = (st) => ({
  acc: st.acc,
  style: st.style,
  last: { head: [...st.last.head], breath: st.last.breath, sway: st.last.sway },
  pos: st.pos.map((p) => [...p]),
  vel: st.vel.map((v) => [...v]),
  hist: st.hist.slice(),
  at: st.at,
  snap: st.snap.map((p) => [...p]),
  dyn: st.dyn.map((p) => [...p]),
  swayPx: st.swayPx,
  out: st.out.map((p) => [...p]),
});

// One frame. input: { head: [x, y], breath, sway, pose | style, extra: { 'hair.<part>.x': px } }.
// Returns a new state (the given one is not modified).
export function stepHair(rig, state, input, dt) {
  const st = clone(state);
  const head = [num(input.head && input.head[0], -50, 50, 0), num(input.head && input.head[1], -50, 50, 0)];
  const breath = num(input.breath, -10, 10, 0);
  const sway = num(input.sway, -4, 4, 0);
  const styleId = input.style || (input.pose ? hairStyle(input.pose).style : 'calm');
  const style = rig.styles[styleId] || rig.styles.calm;
  const extra = input.extra || {};
  const segs = rig.segments;
  if (styleId !== st.style) {
    st.style = styleId;
    for (let i = 0; i < segs.length; i += 1) {
      if (!segs[i].tip) continue;
      st.vel[i][0] += style.kick[0] * (segs[i].side || 0);
      st.vel[i][1] += style.kick[1];
    }
  }
  const from = st.last;
  st.acc += num(dt, 0, 1, 0);
  let n = Math.floor(st.acc / STEP + 1e-9);
  st.acc -= n * STEP;
  if (n > MAX_STEPS) n = MAX_STEPS;
  for (let k = 1; k <= n; k += 1) {
    const f = k / n;
    st.at = (st.at + 1) % HISTORY;
    const h = [from.head[0] + (head[0] - from.head[0]) * f, from.head[1] + (head[1] - from.head[1]) * f];
    for (let i = 0; i < segs.length; i += 1) {
      const s = segs[i];
      const anchor = anchorOf(rig, s, h, st.pos);
      const row = i * HISTORY;
      st.hist[(row + st.at) * 2] = anchor[0];
      st.hist[(row + st.at) * 2 + 1] = anchor[1];
      const back = (row + ((st.at - Math.round(s.delay / STEP) + HISTORY * 2) % HISTORY)) * 2;
      const seen = [st.hist[back], st.hist[back + 1]];
      const lift = liftOf(style, s);
      const ex = [num(extra[`hair.${s.part}.x`], -3, 3, 0), num(extra[`hair.${s.part}.y`], -3, 3, 0)];
      const reach = s.tip ? 1 : s.depth / (s.depth + 1);
      const target = [seen[0] + lift[0] + ex[0] * reach, seen[1] + lift[1] + ex[1] * reach];
      const kk = s.stiffness * style.stiffness;
      const c = 2 * Math.min(2, s.damping * style.damping) * Math.sqrt(kk);
      const p = st.pos[i];
      const v = st.vel[i];
      for (const a of [0, 1]) {
        v[a] += (kk * (target[a] - p[a]) - c * v[a]) * STEP;
        p[a] += v[a] * STEP;
        // cap the swing around where it hangs now (lifts may add their own pixels)
        const cap = s.maxAmp + Math.abs(lift[a]) + Math.abs(ex[a]);
        const lo = anchor[a] - cap;
        const hi = anchor[a] + cap;
        if (p[a] < lo || p[a] > hi) {
          p[a] = Math.min(hi, Math.max(lo, p[a]));
          v[a] = 0;
        }
      }
    }
  }
  st.last = { head, breath, sway };
  // the shared sway: one whole pixel value for all, with hysteresis against flicker
  if (Math.abs(sway * SWAY_PX - st.swayPx) > 0.62) st.swayPx = Math.round(sway * SWAY_PX);
  const breathPx = Math.round(breath);
  // whole pixels: hysteresis against flicker, then at most 1 px from the parent per axis; a
  // segment that may not leave its anchor (maxAmp 0) is exactly there
  for (let i = 0; i < segs.length; i += 1) {
    const s = segs[i];
    const p = st.pos[i];
    const q = st.snap[i];
    if (s.maxAmp === 0) {
      const anchor = anchorOf(rig, s, head, st.pos);
      const lift = liftOf(style, s);
      const reach = s.tip ? 1 : s.depth / (s.depth + 1);
      const ex = [num(extra[`hair.${s.part}.x`], -3, 3, 0), num(extra[`hair.${s.part}.y`], -3, 3, 0)];
      q[0] = Math.round(anchor[0] + lift[0] + ex[0] * reach);
      q[1] = Math.round(anchor[1] + lift[1] + ex[1] * reach);
    } else for (const a of [0, 1]) if (Math.abs(p[a] - q[a]) > 0.62) q[a] = Math.round(p[a]);
    const par = s.parent === 'head' ? [Math.round(head[0]), Math.round(head[1])] : st.dyn[rig.index[s.parent]];
    st.dyn[i] = [Math.min(par[0] + 1, Math.max(par[0] - 1, q[0])) + 0, Math.min(par[1] + 1, Math.max(par[1] - 1, q[1])) + 0]; // + 0: no -0
    const sh = sharedOf(s, breathPx, st.swayPx);
    st.out[i] = [st.dyn[i][0] + sh[0] + 0, st.dyn[i][1] + sh[1] + 0];
  }
  return st;
}

// Whole-pixel offset of every segment, in sheet pixels (head motion included).
export function hairOffsets(rig, state) {
  const out = {};
  rig.segments.forEach((s, i) => {
    out[s.id] = [state.out[i][0], state.out[i][1]];
  });
  return out;
}

// Replay a whole input sequence from rest (tests, captures): returns the offsets per frame.
export function simulateHair(rig, frames, dt = 1 / 60) {
  let st = initHairState(rig, frames[0] || {});
  return frames.map((f) => {
    st = stepHair(rig, st, f, f.dt ?? dt);
    return hairOffsets(rig, st);
  });
}
