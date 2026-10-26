// Motion catalog of the Stage character (#341): declarative data + validation. No code in
// entries, ever.
//
// A motion is a list of keyed *tracks*: which pose sheet (head tilt / nod / posture, drawn
// offline from the owner's icon), which eyes / gaze / brows / mouth, the ear angles, which arm
// sprites, blush / sweat / effect, at which second. Values are ids of parts that exist in
// assets/char/parts.json (checked by validateMotion), so a motion can only recombine drawn
// frames; it cannot draw, load or run anything. Adding a motion = adding one entry below (or,
// later, in a human-approved pack). A new *frame* needs tools/char_parts.py and a rebuild.
//
// Which motion plays is decided by MOTION_RULES (data, evaluated top to bottom by
// motion_model.js) from the state that real events left — the same state always selects the
// same motion. The agent may only *request* motions marked `selectable`, by id; those never
// change a state channel and give way to state-selected motions.

// Free hands are paused (the commander, 2026-10-18): only the mug hand is drawn. These arm
// frames stay in the assets (parts.json, poses.json) but no motion uses them.
export const PAUSED_ARMS = Object.freeze({
  r: Object.freeze(['type1', 'type2', 'cross', 'ponder_cross']),
  l: Object.freeze(['point', 'wave1', 'wave2', 'raise', 'chin', 'chin_in', 'pen', 'cross', 'ponder_cross']),
});
// The desk shot (typing_back / watch_back) is paused too: kept, chosen by no rule.
export const PAUSED_MOTIONS = Object.freeze(['typing_back', 'watch_back']);

// Ids the gadgets refer to (gadget_catalog.MOTION_IDS) — they must exist here.
export const CONTRACT_IDS = ['typing', 'look_screen', 'delegate_point', 'receive_report', 'ponder', 'head_tilt', 'sip', 'alert', 'relief', 'slump'];

// Track channels. Enum tracks hold a part id (step keys); number tracks are interpolated
// linearly and the rig picks the nearest drawn frame (ears: degrees, + = folds back).
export const ENUM_TRACKS = ['sheet', 'eyes', 'gaze', 'brows', 'mouth', 'armR', 'armL', 'fx', 'desk', 'tail', 'ears'];
// The desk shot (#341 night round B, js/desk_seat.js): frames of assets/char/desk.json (built by
// tools/char_desk.py; a test keeps this list and the file in step). `desk` = her figure in the
// room (walking there, at the chair, seated, walking back), `tail` = the tail while she sits.
export const DESK_IDS = Object.freeze({
  desk: Object.freeze(['none', 'walk1', 'walk2', 'walk3', 'walk4', 'walk5', 'walk6', 'walk7', 'stand', 'sit', 'sit_l', 'sit_r', 'sit_bob', 'sit_tilt', 'turn', 'back1', 'back2', 'back3', 'back4', 'back5', 'back6', 'back7']),
  // the tail (round A's extras.json `tail.front`; the back view and the desk shot take the
  // nearest of `tail.back` l1 / c / r1): set by a motion track, else by TAIL_MOOD
  tail: Object.freeze(['none', 'l2', 'l1', 'c', 'r1', 'r2', 'up', 'down']),
  // the ears (extras.json `ears.l` / `ears.r`): angle = the parts' angle sprites (earL / earR);
  // droop, twitch1 / twitch2 and red (red.<angle>) are round A's drawings
  ears: Object.freeze(['angle', 'droop', 'twitch1', 'twitch2', 'red', 'state']), // state: back to the state's ears (a track's end)
});

// --- expressions from the state (#341 night round B, the reference sheet 05 / 06) -----------
// The face (eyes, mouth, brows), the face effects (round A's fx.json), and the ears (extras
// ears) follow the state through FACE_RULES (first match), held by motion_model.FaceHolder:
// - `for` (s): the row shows for this long after its facts first hold, then the next match
//   (a reaction to an event, not a mask for the rest of the turn);
// - a new face waits until the current one has shown FACE_MIN_HOLD s, unless it is `urgent`
//   (the breaker), so the face does not flicker from event to event.
// Each row names the Stira emotion category it shows; no emotion is defined here (the pose's
// emotion comes from model.js EMOTION_RULES, which this does not change). Neural State values
// are only read.
// when (all must hold): breaker, cue (breaker.rise / breaker.calm), vigilant (vigilance >=
// 0.6), toolError (a tool failed in the open turn), outcome, test (the turn's last test),
// stamp (approval decided in the turn), fond (attachment >= 0.7 and trust >= 0.6, the pose's
// own blush rule), elated (satisfaction >= 0.85: a strong joy), emotion (the pose's).
// The eyes / mouths are parts.json names, xfx fx.json names (a name with .l / .r sprites draws
// both; `sparkle` twinkles 1 2 3 2), ears extras names.
export const FACE_MIN_HOLD = 1.5;
export const FACE_RULES = Object.freeze([
  { id: 'panic', when: { breaker: ['CRITICAL'], cue: ['breaker.rise'] }, eyes: 'panic', mouth: 'gasp', xfx: ['gloom', 'sweat.fly'], ears: 'droop', urgent: true, category: 'threat', why: 'the breaker just tripped to CRITICAL (焦り)' },
  { id: 'alarm', when: { breaker: ['CRITICAL'], vigilant: [true] }, eyes: 'sharp', mouth: 'mutto', brows: 'angry', xfx: ['anger'], ears: 'droop', urgent: true, category: 'threat', why: 'CRITICAL with vigilance >= 0.6: a strong threat (警戒・怒り)' },
  { id: 'wary', when: { breaker: ['CRITICAL'] }, eyes: 'sharp', mouth: 'mutto', ears: 'droop', urgent: true, category: 'threat', why: 'breaker CRITICAL (警戒)' },
  { id: 'puzzled', when: { breaker: ['WARNING'] }, eyes: 'puzzled', xfx: ['sweat.drop'], ears: 'droop', urgent: true, category: 'uncertainty', why: 'breaker WARNING (困惑)' },
  { id: 'stumble', when: { thinking: [true], toolError: [true] }, eyes: 'puzzled', mouth: 'wavy', xfx: ['sweat.drop'], ears: 'droop', for: 3, category: 'uncertainty', why: 'a tool failed in the open turn (戸惑い)' },
  { id: 'teary', when: { outcome: ['error'] }, eyes: 'teary', mouth: 'hen', xfx: ['tear'], ears: 'droop', for: 5, category: 'loss', why: 'the turn ended with an error (悲しみ)' },
  { id: 'tired', when: { outcome: ['ok'], test: ['fail'] }, eyes: 'jito', mouth: 'hen', for: 5, category: 'loss', why: 'the turn ended, but a test failed in it (お疲れ・ジト目)' },
  { id: 'triumph', when: { outcome: ['ok'], test: ['pass'], elated: [true] }, eyes: 'arc', mouth: 'laugh', xfx: ['sparkle'], for: 4, category: 'achievement', why: 'the tests passed and satisfaction >= 0.85: a strong joy (大笑い)' },
  { id: 'smug', when: { outcome: ['ok'], test: ['pass'] }, eyes: 'arc', mouth: 'smirk', xfx: ['sparkle'], for: 4, category: 'achievement', why: 'the turn ended and its tests passed (ドヤ)' },
  { id: 'shy_strong', when: { stamp: ['approve'], fond: [true] }, eyes: 'shy', gaze: 'c', mouth: 'small_open', xfx: ['blush.strong', 'blush.nose'], ears: 'red', for: 4, category: 'connection', why: 'approved, with attachment and trust high (照れ, strong)' },
  { id: 'shy', when: { stamp: ['approve'] }, eyes: 'shy', gaze: 'c', xfx: ['blush.soft'], ears: 'red', for: 4, category: 'connection', why: 'the commander approved her request (照れ)' },
  { id: 'content', when: { outcome: ['ok'], test: [null] }, mouth: 'niko', for: 3, category: 'achievement', why: 'the turn ended well, no test in it (ニコニコ)' },
  { id: 'fond', when: { fond: [true] }, xfx: ['blush.soft'], ears: 'red', category: 'connection', why: 'attachment >= 0.7 and trust >= 0.6 (照れ, quiet: cheeks and ears only)' },
  { id: 'curious', when: { emotion: ['curious'] }, mouth: 'small_open', category: 'uncertainty', why: 'curiosity >= 0.70 (a small open mouth)' },
].map((r) => Object.freeze({ ...r, xfx: Object.freeze(r.xfx || []) })));
export const SPARKLE = Object.freeze({ frames: Object.freeze(['sparkle.1', 'sparkle.2', 'sparkle.3', 'sparkle.2']), hold: 0.2 });

// The tail per emotion: frames in turn, each held `hold` s, on the clock (the same state and
// time give the same tail). A sway is l1 c r1 c or wider (extras contract: 4-8 frames a step).
export const TAIL_MOOD = Object.freeze({
  calm: Object.freeze({ frames: Object.freeze(['c', 'c', 'c', 'r1', 'c', 'l1']), hold: 0.26, category: '(none: at rest)', why: 'a slow sway now and then' }),
  curious: Object.freeze({ frames: Object.freeze(['l1', 'c', 'r1', 'c']), hold: 0.2, category: 'uncertainty (curiosity)', why: 'curiosity >= 0.70: swings side to side' }),
  joy: Object.freeze({ frames: Object.freeze(['l1', 'c', 'r1', 'r2', 'r1', 'c']), hold: 0.14, category: 'joy', why: 'satisfaction >= 0.72: a wide, quick wag' }),
  worried: Object.freeze({ frames: Object.freeze(['down']), hold: 1, category: 'uncertainty', why: 'breaker WARNING: lowered, still' }),
  tense: Object.freeze({ frames: Object.freeze(['down']), hold: 1, category: 'threat', why: 'breaker CRITICAL: lowered, still' }),
});
export function tailFor(emotion, t) {
  const m = TAIL_MOOD[emotion] || TAIL_MOOD.calm;
  return m.frames[Math.floor(Math.max(0, t) / m.hold) % m.frames.length];
}
export const NUMBER_TRACKS = { earL: [-18, 18], earR: [-18, 18], blush: [0, 1], sweat: [0, 1] };
// Face tracks: an agent-requested motion does not override them while worried or tense.
export const FACE_TRACKS = ['eyes', 'gaze', 'brows', 'mouth'];

const ID_RE = /^[a-z][a-z0-9_]{0,31}$/;
const MAX_KEYS = 40;
const num = (v, lo, hi) => (typeof v === 'number' && Number.isFinite(v) ? Math.min(hi, Math.max(lo, v)) : null);

// ids: { sheet: [...], eyes: [...], gaze, brows, mouth, armR, armL, fx } (char_rig.partIds).
// Returns a frozen, sanitised copy, or null when the entry is not acceptable as data.
export function validateMotion(raw, ids) {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null;
  if (typeof raw.id !== 'string' || !ID_RE.test(raw.id)) return null;
  const duration = num(raw.duration, 0.1, 20);
  if (duration === null) return null;
  const tracks = validTracks(raw.tracks, ids, duration);
  if (!tracks) return null;
  const loop = raw.loop === true;
  const intro = loop ? num(raw.intro ?? 0, 0, duration - 0.05) : 0;
  if (intro === null) return null;
  let exit = null;
  if (raw.exit !== undefined) {
    if (!raw.exit || typeof raw.exit !== 'object') return null;
    const d = num(raw.exit.duration, 0.05, 3);
    const t = d === null ? null : validTracks(raw.exit.tracks, ids, d);
    if (!t) return null;
    exit = { duration: d, tracks: t };
  }
  if (raw.then !== undefined && (typeof raw.then !== 'string' || !ID_RE.test(raw.then))) return null;
  if (raw.group !== undefined && (typeof raw.group !== 'string' || !ID_RE.test(raw.group))) return null;
  return deepFreeze({
    id: raw.id,
    description: typeof raw.description === 'string' ? raw.description.slice(0, 200) : '',
    duration,
    loop,
    intro,
    tracks,
    exit,
    then: raw.then || null,
    // motions of one group pass to each other without their exit and intro (e.g. she stays
    // turned round from typing to watching the screen)
    group: raw.group || null,
    selectable: raw.selectable === true,
  });
}

function validTracks(raw, ids, duration) {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null;
  const out = {};
  for (const [ch, keys] of Object.entries(raw)) {
    const isEnum = ENUM_TRACKS.includes(ch);
    if (!isEnum && !(ch in NUMBER_TRACKS)) return null;
    if (!Array.isArray(keys) || keys.length === 0 || keys.length > MAX_KEYS) return null;
    const list = [];
    for (const k of keys) {
      if (!Array.isArray(k) || k.length !== 2) return null;
      const t = num(k[0], 0, duration);
      if (t === null) return null;
      let v;
      if (isEnum) {
        if (typeof k[1] !== 'string' || !(ids[ch] || DESK_IDS[ch] || []).includes(k[1])) return null;
        v = k[1];
      } else {
        v = num(k[1], ...NUMBER_TRACKS[ch]);
        if (v === null) return null;
      }
      list.push([t, v]);
    }
    list.sort((a, b) => a[0] - b[0]);
    out[ch] = list;
  }
  return Object.keys(out).length ? out : null;
}

function deepFreeze(o) {
  for (const v of Object.values(o)) if (v && typeof v === 'object') deepFreeze(v);
  return Object.freeze(o);
}

export function buildMotions(entries, ids) {
  const map = new Map();
  const rejected = [];
  for (const raw of entries) {
    const m = validateMotion(raw, ids);
    if (m && !map.has(m.id)) map.set(m.id, m);
    else rejected.push(raw && typeof raw.id === 'string' ? raw.id.slice(0, 32) : '?');
  }
  // `then` must name another motion of the catalog
  for (const [id, m] of map) if (m.then && !map.has(m.then)) {
    map.delete(id);
    rejected.push(id);
  }
  return { map, rejected };
}

// Value of a track at time t (step for ids, linear for numbers).
export function trackAt(keys, t, isEnum) {
  if (t <= keys[0][0]) return keys[0][1];
  for (let i = 1; i < keys.length; i += 1) {
    if (t < keys[i][0]) {
      if (isEnum) return keys[i - 1][1];
      const [t0, v0] = keys[i - 1];
      const [t1, v1] = keys[i];
      return v0 + ((v1 - v0) * (t - t0)) / Math.max(1e-6, t1 - t0);
    }
  }
  return keys[keys.length - 1][1];
}

// --- the face of each emotion (from model.derive) ---------------------------------------------
// model.derive's eye ids -> drawn eye shape + gaze.
export const EYE_MAP = {
  open: ['open', 'c'], up: ['open', 'u'], half: ['half', 'c'], down: ['down', 'c'], wide: ['wide', 'c'],
  happy: ['happy', 'c'], closed: ['closed', 'c'], side: ['open', 'dl'],
};
// Posture, brows and ears that come with an emotion (the eyes and mouth come from derive).
export const EMOTION_POSE = {
  calm: { sheet: 'base', brows: 'neutral', earL: 0, earR: 0 },
  curious: { sheet: 'up1', brows: 'up', earL: -6, earR: -6 },
  joy: { sheet: 'up1', brows: 'soft', earL: -6, earR: -6 },
  worried: { sheet: 'down1', brows: 'worried', earL: 8, earR: 8 },
  tense: { sheet: 'down1', brows: 'angry', earL: 14, earR: 14 },
};

// --- which motion (data; motion_model.selectMotion evaluates it) -------------------------------
// `when`: every key must match. breaker / emotion / outcome / cue: one of the listed values;
// approval / thinking: true or false; kinds: any running tool of these kinds; gadget: the
// motion that the props ask for (gadget.motion) is one of the listed ids, or '*' for any id
// of this catalog. `motion: '$gadget'` plays the motion the props named.
export const MOTION_RULES = [
  { motion: 'surprised', when: { cue: ['breaker.rise'], breaker: ['CRITICAL'] }, why: 'the breaker just tripped to CRITICAL' },
  { motion: 'alert', when: { breaker: ['CRITICAL', 'WARNING'] }, why: 'breaker above NORMAL' },
  // asking, then waiting for the answer with the mug (reference 08: 解析待ち・休息)
  { motion: 'await_mug', when: { approval: [true] }, why: 'asking for approval, then waiting for the answer' },
  { motion: 'relief', when: { cue: ['breaker.calm'] }, why: 'the breaker went back to NORMAL' },
  // coding (the commander, 2026-10-18): she stays facing us and the laptop comes out in front
  // of her — no walk to the room desk, no chair, no back view. typing_back / watch_back (the
  // desk shot) are paused: kept in the catalog, not chosen by any rule (docs/stage.md).
  { motion: 'typing', when: { gadget: ['typing'] }, why: 'a code tool is running: the laptop out in front of her, typing' },
  { motion: 'look_screen', when: { gadget: ['look_screen'] }, why: 'code / tests on the laptop in this turn: watching its screen' },
  // the web (the magnifier at the window): she leans in to read (before the props' head tilt)
  { motion: 'peer', when: { kinds: ['web'] }, why: 'the web is searched or fetched' },
  { motion: '$gadget', when: { gadget: ['*'] }, why: 'the props ask for it' },
  { motion: 'look_shelf', when: { kinds: ['memory', 'file'] }, why: 'reading the shelf (memory / files)' },
  { motion: 'look_desk', when: { kinds: ['tool'] }, why: 'a desk tool is running' },
  { motion: 'ponder', when: { thinking: [true] }, why: 'a turn is open' },
  // how the turn ended for her, by its tests (reference sheets 05 / 01): achievement -> a smug
  // look (ドヤ顔); a test failed but the turn still ended -> a tired sigh (お疲れ, loss)
  { motion: 'smug', when: { outcome: ['ok'], test: ['pass'] }, why: 'the turn ended well and its tests passed (achievement)' },
  { motion: 'sigh', when: { outcome: ['ok'], test: ['fail'] }, why: 'the turn ended, but a test failed in it (loss)' },
  { motion: 'stretch', when: { outcome: ['ok'], emotion: ['joy'] }, why: 'the turn ended well (satisfied)' },
  { motion: 'sip', when: { outcome: ['ok'] }, why: 'the turn ended' },
  { motion: 'slump', when: { outcome: ['error'] }, why: 'the turn ended with an error' },
];
export const RULE_KEYS = ['breaker', 'emotion', 'outcome', 'cue', 'approval', 'thinking', 'kinds', 'gadget', 'test'];

// --- the motions ---------------------------------------------------------------------------------
// Times in seconds. Pixel-art timing: a pose holds for 2-10 frames (at 60 fps) before the next.

export const MOTIONS = [
  {
    id: 'typing_back',
    group: 'back',
    description: 'Coding: turns round through a side profile, walks to the room desk (the camera stays: she gets smaller), sits on the chair with her back to us and types, shoulders and head with the keys, the tail over the seat. Walks back facing us when it ends.',
    loop: true,
    intro: 1.42,
    duration: 3.54,
    tracks: {
      sheet: [[0, 'base'], [0.24, 'yaw_r'], [0.34, 'profile'], [0.44, 'turn1'], [0.54, 'turn'], [0.64, 'back']],
      armR: [[0, 'mug_low'], [0.12, 'mug']],
      gaze: [[0, 'dl'], [0.24, 'r']],
      eyes: [[0, 'open']],
      earL: [[0, 0], [0.3, -6]],
      earR: [[0, 0], [0.3, -6]],
      desk: [[0, 'none'], [0.7, 'walk1'], [0.78, 'walk2'], [0.86, 'walk3'], [0.94, 'walk4'], [1.02, 'walk5'], [1.1, 'walk6'], [1.18, 'walk7'], [1.26, 'stand'], [1.42, 'sit'], [1.5, 'sit_l'], [1.62, 'sit'], [1.74, 'sit_r'], [1.88, 'sit'], [2.02, 'sit_bob'], [2.14, 'sit'], [2.3, 'sit_l'], [2.4, 'sit'], [2.54, 'sit_r'], [2.66, 'sit'], [2.84, 'sit_tilt'], [3.14, 'sit'], [3.24, 'sit_l'], [3.36, 'sit']],
      // the tail follows TAIL_MOOD while she sits (no track)
    },
    exit: {
      duration: 1.3,
      tracks: {
        desk: [[0, 'stand'], [0.12, 'turn'], [0.26, 'back7'], [0.34, 'back6'], [0.42, 'back5'], [0.5, 'back4'], [0.58, 'back3'], [0.66, 'back2'], [0.74, 'back1'], [0.82, 'none']],
        sheet: [[0, 'base']],
        armR: [[0, 'mug']],
        gaze: [[0, 'c']],
        earL: [[0, -2], [0.8, 0]],
        earR: [[0, -2], [0.8, 0]],
      },
    },
  },
  {
    id: 'watch_back',
    group: 'back',
    description: 'At the room desk (code was written in this turn, tests run): seated with her back to us, reads, the head tilts now and then, a key now and then. Comes and goes like typing_back.',
    loop: true,
    intro: 1.42,
    duration: 5.42,
    tracks: {
      sheet: [[0, 'base'], [0.24, 'yaw_r'], [0.34, 'profile'], [0.44, 'turn1'], [0.54, 'turn'], [0.64, 'back']],
      armR: [[0, 'mug_low'], [0.12, 'mug']],
      gaze: [[0, 'dl'], [0.24, 'r']],
      eyes: [[0, 'open']],
      earL: [[0, 0], [0.3, -6]],
      earR: [[0, 0], [0.3, -6]],
      desk: [[0, 'none'], [0.7, 'walk1'], [0.78, 'walk2'], [0.86, 'walk3'], [0.94, 'walk4'], [1.02, 'walk5'], [1.1, 'walk6'], [1.18, 'walk7'], [1.26, 'stand'], [1.42, 'sit'], [2.6, 'sit_bob'], [2.8, 'sit'], [3.8, 'sit_tilt'], [4.7, 'sit'], [5.0, 'sit_r'], [5.12, 'sit']],
      // the tail follows TAIL_MOOD while she sits (no track)
    },
    exit: {
      duration: 1.3,
      tracks: {
        desk: [[0, 'stand'], [0.12, 'turn'], [0.26, 'back7'], [0.34, 'back6'], [0.42, 'back5'], [0.5, 'back4'], [0.58, 'back3'], [0.66, 'back2'], [0.74, 'back1'], [0.82, 'none']],
        sheet: [[0, 'base']],
        armR: [[0, 'mug']],
        gaze: [[0, 'c']],
        earL: [[0, -2], [0.8, 0]],
        earR: [[0, -2], [0.8, 0]],
      },
    },
  },

  {
    id: 'typing',
    description: 'Coding: the laptop comes out in front of her (the prop fades in, its keys light and its screen scrolls); she stays facing us with the mug lowered in her hand, eyes on its screen, and the head and shoulders bob a little with the typing — no hand on the keys (free hands are paused).',
    loop: true,
    intro: 0.3,
    duration: 1.5,
    tracks: {
      sheet: [[0, 'down1'], [0.3, 'type'], [0.5, 'down1'], [0.62, 'type'], [0.94, 'down1'], [1.04, 'type'], [1.3, 'down1'], [1.38, 'type']],
      armR: [[0, 'mug'], [0.16, 'mug_low']],
      eyes: [[0, 'open']],
      gaze: [[0, 'dl'], [0.9, 'd'], [1.2, 'dl']],
      brows: [[0, 'focus']],
      mouth: [[0, 'flat']],
      earL: [[0, -4]],
      earR: [[0, -4]],
    },
    exit: { duration: 0.3, tracks: { armR: [[0, 'mug_low'], [0.16, 'mug']], sheet: [[0, 'down1'], [0.2, 'base']] } },
  },
  {
    id: 'look_screen',
    description: 'Code was written in this turn / tests run: watches the laptop screen in front of her, head and eyes to the lower left, the mug in hand.',
    loop: true,
    intro: 0.2,
    duration: 3.2,
    tracks: {
      sheet: [[0, 'yaw_l'], [0.2, 'type']],
      eyes: [[0, 'open'], [2.3, 'half'], [2.6, 'open']],
      gaze: [[0, 'l'], [0.2, 'dl'], [1.6, 'd'], [2.0, 'dl']],
      brows: [[0, 'focus']],
      earL: [[0, -4], [0.2, -6]],
      earR: [[0, -2]],
    },
    exit: { duration: 0.2, tracks: { sheet: [[0, 'yaw_l'], [0.12, 'base']], gaze: [[0, 'l'], [0.12, 'c']] } },
  },
  {
    id: 'look_desk',
    description: 'Watches the instrument on the desk (a tool is running): head and eyes to the right.',
    loop: true,
    intro: 0.2,
    duration: 3.0,
    tracks: {
      sheet: [[0, 'yaw_r']],
      eyes: [[0, 'open'], [2.2, 'half'], [2.5, 'open']],
      gaze: [[0, 'r'], [1.4, 'dr'], [2.0, 'r']],
      brows: [[0, 'focus']],
      earR: [[0, -6]],
    },
  },
  {
    id: 'look_shelf',
    description: 'Memory or files are read: looks up at the shelf on the left, then reads what came down (reference 08: 資料探索) — head lowered, eyes down the page line by line, a small nod per line; then up to the shelf again.',
    loop: true,
    intro: 0.2,
    duration: 5.6,
    tracks: {
      sheet: [[0, 'yaw_l'], [0.2, 'tilt_l1'], [2.0, 'down1'], [4.8, 'tilt_l1']],
      eyes: [[0, 'open'], [2.0, 'half'], [4.8, 'open']],
      gaze: [[0, 'l'], [0.2, 'ul'], [1.5, 'l'], [2.0, 'dl'], [2.6, 'd'], [3.2, 'dl'], [3.8, 'd'], [4.4, 'dl'], [4.8, 'ul']],
      brows: [[0, 'up'], [2.0, 'focus'], [4.8, 'up']],
      earL: [[0, -8]],
      earR: [[0, -2]],
    },
    exit: { duration: 0.16, tracks: { sheet: [[0, 'yaw_l'], [0.1, 'base']] } },
  },
  {
    id: 'delegate_point',
    description: 'Sends a subagent: turns her head and eyes up and out after the drone, says a word, looks back (no pointing hand: free hands are paused; the drone shows where it goes).',
    duration: 2.3,
    tracks: {
      sheet: [[0, 'base'], [0.12, 'tilt_r1'], [0.3, 'up1'], [1.4, 'tilt_r1'], [1.7, 'base']],
      eyes: [[0, 'open'], [0.12, 'sharp'], [0.9, 'open']],
      gaze: [[0, 'r'], [0.12, 'ur'], [1.7, 'c']],
      brows: [[0, 'focus'], [1.7, 'neutral']],
      mouth: [[0, 'neutral'], [0.2, 'open'], [0.42, 'talk'], [0.6, 'open'], [0.8, 'neutral']],
      earL: [[0, 0], [0.12, -10], [1.7, 0]],
      earR: [[0, 0], [0.12, -10], [1.7, 0]],
    },
  },
  {
    id: 'receive_report',
    description: 'A subagent reports back: two small nods with soft eyes and a smile, ears up.',
    duration: 2.0,
    tracks: {
      sheet: [[0, 'down1'], [0.18, 'base'], [0.36, 'down1'], [0.54, 'base']],
      eyes: [[0, 'open'], [0.1, 'soft'], [1.6, 'open']],
      mouth: [[0, 'neutral'], [0.1, 'smile'], [1.7, 'neutral']],
      brows: [[0, 'soft'], [1.7, 'neutral']],
      earL: [[0, 0], [0.1, -10], [1.4, -6], [1.9, 0]],
      earR: [[0, 0], [0.1, -10], [1.4, -6], [1.9, 0]],
      armR: [[0, 'mug_low'], [1.6, 'mug']],
    },
  },
  {
    id: 'ponder',
    description: 'Thinks (reference 01 考え事): the mug stays in hand; the head turns and tilts through two in-betweens, eyes up to the side, now and then narrowed, the mouth pursed. Back the same way (no hand at the chin: free hands are paused).',
    loop: true,
    intro: 0.5,
    duration: 4.3,
    tracks: {
      // the head turns through two in-betweens; the hand comes up to the chin in two steps with
      // them (chin_in: the hand on its way, drawn by round A; the arm's 'raise' frame showed the
      // hand outside her silhouette at the shoulder)
      sheet: [[0, 'yaw_r'], [0.08, 'tilt_r1'], [0.16, 'ponder']],
      eyes: [[0, 'open'], [1.2, 'half'], [1.6, 'open'], [2.9, 'half'], [3.4, 'open']],
      gaze: [[0, 'u'], [0.16, 'ul'], [2.0, 'u'], [2.6, 'ul']],
      brows: [[0, 'focus']],
      mouth: [[0, 'flat'], [3.6, 'pout'], [4.0, 'flat']],
      earL: [[0, -2], [0.5, -6]],
      earR: [[0, 2]],
    },
    exit: { duration: 0.3, tracks: { sheet: [[0, 'tilt_r1'], [0.1, 'yaw_r'], [0.2, 'base']] } },
  },
  {
    id: 'await_mug',
    description: 'Waiting for the commander\'s answer (an approval is pending): asks with a tilt of the head, then rests with the mug lowered in her hands (reference 08: 解析待ち・休息) — soft eyes on us, a glance at the mug, now and then a small sip.',
    loop: true,
    intro: 2.0,
    duration: 6.0,
    tracks: {
      sheet: [[0, 'tilt_r1'], [0.14, 'tilt_r2'], [1.6, 'tilt_r1'], [1.8, 'relax'], [4.8, 'sip'], [5.4, 'relax']],
      eyes: [[0, 'open'], [0.14, 'wide'], [1.5, 'open'], [2.0, 'soft'], [3.6, 'half'], [4.0, 'soft'], [4.8, 'closed'], [5.4, 'soft']],
      gaze: [[0, 'c'], [0.14, 'ur'], [1.2, 'c'], [3.0, 'dl'], [4.4, 'c']],
      brows: [[0, 'up'], [2.0, 'soft']],
      mouth: [[0, 'neutral'], [0.14, 'small_o'], [0.9, 'neutral'], [5.4, 'exhale'], [5.7, 'neutral']],
      armR: [[0, 'mug'], [1.8, 'mug_low'], [4.6, 'mug_lift'], [4.8, 'mug_sip'], [5.4, 'mug_lift'], [5.6, 'mug_low']],
      earL: [[0, -6]],
      earR: [[0, 6]],
    },
    exit: { duration: 0.3, tracks: { sheet: [[0, 'relax'], [0.15, 'base']], armR: [[0, 'mug_low'], [0.15, 'mug']] } },
  },
  {
    id: 'peer',
    description: 'The web is searched or fetched (the magnifier at the window): turns toward it and leans in to read, eyes narrowed, the mouth a little open; now and then she squints closer and pulls back (reference 07 / 08).',
    loop: true,
    intro: 0.3,
    duration: 3.6,
    tracks: {
      sheet: [[0, 'yaw_r'], [0.16, 'tilt_r1'], [1.4, 'yaw_r'], [1.7, 'tilt_r1'], [2.8, 'yaw_r'], [3.0, 'tilt_r1']],
      eyes: [[0, 'open'], [0.3, 'sharp'], [1.4, 'half'], [1.7, 'sharp'], [2.8, 'half'], [3.0, 'sharp']],
      gaze: [[0, 'r'], [0.3, 'ur'], [1.6, 'r'], [2.4, 'ur']],
      brows: [[0, 'focus']],
      mouth: [[0, 'neutral'], [0.3, 'small_open'], [2.0, 'neutral']],
      earL: [[0, -6]],
      earR: [[0, -6]],
    },
    exit: { duration: 0.2, tracks: { sheet: [[0, 'yaw_r'], [0.12, 'base']] } },
  },
  {
    id: 'head_tilt',
    description: 'Tilts the head (asking, or curious about something outside); ears uneven.',
    loop: true,
    intro: 0.3,
    duration: 2.6,
    selectable: true,
    tracks: {
      sheet: [[0, 'tilt_r1'], [0.14, 'tilt_r2']],
      eyes: [[0, 'open'], [0.14, 'wide'], [1.5, 'open']],
      gaze: [[0, 'c'], [0.14, 'ur'], [1.2, 'c']],
      brows: [[0, 'up']],
      mouth: [[0, 'neutral'], [0.14, 'small_o'], [0.9, 'neutral']],
      earL: [[0, -4], [0.14, -10]],
      earR: [[0, 4], [0.14, 10]],
    },
    exit: { duration: 0.2, tracks: { sheet: [[0, 'tilt_r1'], [0.12, 'base']] } },
  },
  {
    id: 'sip',
    description: 'Brings the mug to the mouth, tilts it and drinks with closed eyes, lowers it, breathes out.',
    duration: 2.7,
    selectable: true,
    tracks: {
      armR: [[0, 'mug_up'], [0.16, 'mug_lift'], [0.32, 'mug_sip'], [1.5, 'mug_lift'], [1.68, 'mug_up'], [1.86, 'mug']],
      sheet: [[0, 'base'], [0.3, 'sip'], [1.55, 'base']],
      eyes: [[0, 'open'], [0.26, 'half'], [0.4, 'closed'], [1.44, 'half'], [1.62, 'soft'], [2.3, 'open']],
      mouth: [[0, 'neutral'], [1.7, 'exhale'], [2.2, 'neutral']],
      brows: [[0, 'neutral'], [0.4, 'soft'], [2.3, 'neutral']],
      earL: [[0, 0], [0.4, 6], [1.8, 0]],
      earR: [[0, 0], [0.4, 6], [1.8, 0]],
    },
  },
  {
    id: 'alert',
    description: 'Straightens up, eyes sharp, ears up; scans left and right while the breaker is up.',
    loop: true,
    intro: 0.6,
    duration: 3.6,
    tracks: {
      sheet: [[0, 'lift'], [0.16, 'straight']],
      eyes: [[0, 'wide'], [0.26, 'sharp']],
      gaze: [[0, 'c'], [0.9, 'r'], [1.7, 'c'], [2.4, 'l'], [3.1, 'c']],
      brows: [[0, 'up'], [0.26, 'angry']],
      mouth: [[0, 'flat']],
      earL: [[0, -16], [0.3, -12]],
      earR: [[0, -16], [0.3, -12]],
    },
    exit: { duration: 0.3, tracks: { sheet: [[0, 'relax'], [0.2, 'base']] } },
  },
  {
    id: 'relief',
    description: 'Breathes in, then the shoulders drop and relax; eyes close and open soft; a small smile.',
    duration: 2.6,
    tracks: {
      sheet: [[0, 'lift'], [0.3, 'relax'], [2.0, 'base']],
      eyes: [[0, 'open'], [0.3, 'closed'], [1.1, 'soft'], [2.2, 'open']],
      mouth: [[0, 'neutral'], [0.3, 'exhale'], [1.1, 'smile'], [2.3, 'neutral']],
      brows: [[0, 'up'], [0.3, 'soft'], [2.2, 'neutral']],
      earL: [[0, -8], [0.3, 10], [1.6, 4], [2.3, 0]],
      earR: [[0, -8], [0.3, 10], [1.6, 4], [2.3, 0]],
      armR: [[0, 'mug'], [0.3, 'mug_low'], [2.0, 'mug']],
    },
  },
  {
    id: 'slump',
    description: 'Disappointed: the head drops, shoulders fall, eyes downcast, ears flat; recovers slowly.',
    duration: 3.4,
    tracks: {
      sheet: [[0, 'down1'], [0.14, 'down2'], [0.3, 'slump'], [2.8, 'down2'], [3.0, 'down1'], [3.2, 'base']],
      eyes: [[0, 'half'], [0.3, 'down'], [2.9, 'half'], [3.2, 'open']],
      brows: [[0, 'worried']],
      mouth: [[0, 'flat'], [0.3, 'frown'], [3.0, 'flat']],
      earL: [[0, 8], [0.3, 18], [2.8, 10], [3.2, 0]],
      earR: [[0, 8], [0.3, 18], [2.8, 10], [3.2, 0]],
      armR: [[0.3, 'mug_low'], [2.9, 'mug']],
      ears: [[0, 'state'], [0.3, 'droop'], [2.8, 'state']], // loss: flattened to the side
      tail: [[0, 'c'], [0.3, 'down'], [3.0, 'c']],
    },
  },
  {
    id: 'surprised',
    description: 'Startled: jumps up a little, eyes wide with small pupils, ears straight up, a shock mark.',
    duration: 1.4,
    then: 'alert',
    tracks: {
      sheet: [[0, 'lift'], [0.5, 'up1']],
      eyes: [[0, 'surprised'], [1.0, 'wide']],
      brows: [[0, 'up']],
      mouth: [[0, 'small_o'], [0.3, 'open'], [1.0, 'flat']],
      earL: [[0, -18]],
      earR: [[0, -18]],
      fx: [[0, 'shock'], [0.8, 'none']],
      // a flick at the peak (extras contract: std -> twitch1 -> twitch2 -> std, 2 + 2 frames at 30 fps)
      ears: [[0, 'twitch1'], [0.067, 'twitch2'], [0.134, 'state']], // then the state's ears again
      tail: [[0, 'up']],
    },
  },
  {
    id: 'smug',
    description: 'Tests passed and the turn is done: chest out and chin up (`straight`, `up1`) with closed eyes and a small smile, then a side glance under half-closed lids, ears perked, the tail up, the mug in hand (ドヤ顔, reference 05; the crossed arms are paused).',
    duration: 2.6,
    then: 'sip',
    tracks: {
      sheet: [[0, 'straight'], [0.12, 'up1'], [1.7, 'yaw_l'], [2.2, 'base']],
      eyes: [[0, 'closed'], [0.9, 'half']],
      gaze: [[0, 'c'], [0.9, 'l'], [1.7, 'dl'], [2.2, 'c']],
      mouth: [[0, 'neutral'], [0.12, 'smile'], [2.2, 'neutral']],
      brows: [[0, 'up'], [0.9, 'soft']],
      earL: [[0, -8]],
      earR: [[0, -8]],
      tail: [[0, 'c'], [0.12, 'up'], [2.2, 'c']],
    },
  },
  {
    id: 'sigh',
    description: 'The turn is done but a test failed in it: eyes close, a long breath out with the head down, the ears flattened and the tail low, then she looks up again (お疲れ・ため息).',
    duration: 2.4,
    then: 'sip',
    tracks: {
      sheet: [[0, 'base'], [0.1, 'down1'], [0.5, 'down2'], [1.6, 'down1'], [2.0, 'base']],
      eyes: [[0, 'half'], [0.1, 'closed'], [1.6, 'half'], [2.0, 'open']],
      mouth: [[0, 'neutral'], [0.5, 'exhale'], [1.6, 'flat'], [2.0, 'neutral']],
      brows: [[0, 'worried'], [2.0, 'neutral']],
      ears: [[0, 'state'], [0.5, 'droop'], [1.8, 'state']],
      tail: [[0, 'c'], [0.5, 'down'], [2.0, 'c']],
    },
  },
  {
    id: 'scheme',
    description: 'A little plan (企み): narrowed eyes looking aside and a one-sided smile, the head tilted, then back.',
    duration: 1.8,
    selectable: true,
    tracks: {
      sheet: [[0, 'yaw_r'], [0.1, 'tilt_r1'], [1.5, 'yaw_r'], [1.7, 'base']],
      eyes: [[0, 'half'], [0.1, 'scheme'], [1.5, 'half'], [1.7, 'open']],
      mouth: [[0, 'neutral'], [0.1, 'smirk'], [1.6, 'neutral']],
      brows: [[0, 'soft']],
      earL: [[0, -4]],
      earR: [[0, 6]],
    },
  },
  {
    id: 'stretch',
    description: 'Satisfied: shoulders up in a stretch with closed eyes, then a happy look.',
    duration: 2.6,
    selectable: true,
    tracks: {
      sheet: [[0, 'straight'], [0.16, 'lift'], [0.9, 'tilt_l1'], [1.3, 'relax'], [2.0, 'base']],
      eyes: [[0, 'closed'], [1.3, 'happy'], [2.2, 'open']],
      mouth: [[0, 'neutral'], [0.16, 'exhale'], [1.3, 'grin'], [2.2, 'smile']],
      brows: [[0, 'soft']],
      earL: [[0, -12], [1.3, 0]],
      earR: [[0, -12], [1.3, 0]],
      armR: [[1.3, 'mug_low'], [2.0, 'mug']],
    },
  },
  {
    id: 'wave',
    description: 'Greets: a tilt of the head with happy eyes and a grin, the ears flick (the hand wave is paused: free hands are not drawn).',
    duration: 1.9,
    selectable: true,
    tracks: {
      sheet: [[0, 'base'], [0.1, 'tilt_r1'], [0.7, 'tilt_r2'], [1.0, 'tilt_r1'], [1.32, 'base']],
      eyes: [[0, 'open'], [0.1, 'happy'], [1.4, 'open']],
      mouth: [[0, 'smile'], [0.3, 'grin'], [1.2, 'smile']],
      earL: [[0, -6]],
      earR: [[0, -6]],
    },
  },
  {
    id: 'nod',
    description: 'A clear nod (yes).',
    duration: 0.9,
    selectable: true,
    tracks: {
      sheet: [[0, 'down1'], [0.1, 'down2'], [0.26, 'down1'], [0.38, 'base'], [0.5, 'down1'], [0.62, 'base']],
      eyes: [[0, 'half'], [0.62, 'open']],
      earL: [[0, 2], [0.62, 0]],
      earR: [[0, 2], [0.62, 0]],
    },
  },
  {
    id: 'shake_head',
    description: 'Shakes the head (no), eyes closed.',
    duration: 1.2,
    selectable: true,
    tracks: {
      sheet: [[0, 'yaw_l'], [0.16, 'yaw_r'], [0.32, 'yaw_l'], [0.48, 'yaw_r'], [0.64, 'base']],
      eyes: [[0, 'closed'], [0.7, 'open']],
      mouth: [[0, 'flat']],
      brows: [[0, 'worried'], [0.8, 'neutral']],
      earL: [[0, 4], [0.16, -4], [0.32, 4], [0.48, -4], [0.64, 0]],
      earR: [[0, -4], [0.16, 4], [0.32, -4], [0.48, 4], [0.64, 0]],
    },
  },
];
