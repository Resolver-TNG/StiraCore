// Room (background) frame for the Stage renderer (#341). Pure function, no DOM / WebGL.
//
// Rule: the room and the character are separate layers. Breathing, blinking, posture and the
// character's motions never reach the room: roomFrame() reads only the room channels below
// (state lights, steam, shake, gadget flicker, the listed ambient channels) and the pointer
// parallax, never a character channel. The room may move only for
//   1. events (state channels: lights, scope trace, neon colour, CRITICAL shake, ...),
//   2. the pointer parallax (each layer by its own depth, the character by its own),
//   3. the few ambient entries listed in AMBIENT_ANIMS (slow, small, never a fast flicker),
//   4. (#350) the whiteboard: while it is down the room pans a few pixels (`cam`) and the
//      character group steps aside (`aside`, depth >= SHAKE_MIN_DEPTH only). A UI input with
//      its own progress, not a character channel; the board's content never reaches here.

export const ROOM_CHANNELS = [
  'shake', 'neon.flicker', 'light.desk.pulse', 'light.shelf.scan', 'light.window.pulse',
  'steam.puff', 'sparkle.burst', 'gadget.glitch', 'neon.ambient', 'city.twinkle', 'rain.fall',
];
// Channels the ambient scope may drive, with their limits (catalog.js enforces them).
export const AMBIENT_CHANNELS = { 'neon.ambient': [-0.08, 0.08], 'city.twinkle': [0, 1], 'rain.fall': [0, 1] };
export const AMBIENT_MIN_PERIOD = 4; // seconds: no fast flicker

// The only ambient motion of the room (data for anim.js, scope "ambient").
export const AMBIENT_ANIMS = [
  { id: 'ambient.neon', scope: 'ambient', trigger: { always: true }, loop: true, duration: 7.5, channels: { 'neon.ambient': { fn: 'sine', amp: 0.05, period: 7.5 } }, description: 'Neon rings breathe very slightly (±5 %, 7.5 s).' },
  { id: 'ambient.city', scope: 'ambient', trigger: { always: true }, loop: true, duration: 36, channels: { 'city.twinkle': { fn: 'keys', keys: [[0, 0], [36, 1]] } }, description: 'A few lit windows of the night city go on or off, one change every ~4 s.' },
  { id: 'ambient.rain', scope: 'ambient', trigger: { always: true }, loop: true, duration: 4, channels: { 'rain.fall': { fn: 'keys', keys: [[0, 0], [4, 1]] } }, description: 'Rain falls past the window (rain sky only; other skies draw no rain layer).' },
];

const AMBER_RATE = 4;

// CRITICAL shake is "something happens to her": it moves the layers at her depth or in front
// (the character and the props in front of her: laptop, lamp, drones), never the room or the
// props fixed in it (shelf, beacon, slip). 1 px, the same for all of them.
export const SHAKE_MIN_DEPTH = 1.0;

// Mug steam (while a turn is open): one or two thin wisps that rise a short way, curl a little,
// thin out upward and are drawn with ordered dither (half-transparent), so her face and hair
// stay visible. Particles per second = rate x steam.puff (state.steam in the catalog).
export const STEAM = {
  rate: 9, // dense enough that the particles join into a thread
  wisps: [4, 10], // x offsets from the mug's top edge (art pixels); particles alternate
  life: [2.2, 0.4], // seconds: base + spread
  rise: 26, // art pixels over a life (fades out below her chin)
  drift: 3, // drifts this far left (away from her face) by the end
  sway: [0.8, 1.6], // curl amplitude at the start / added toward the end
  size: [3, 4], // point size: start, growth
  alpha: [0.8, 1.4], // peak opacity, fade-out exponent (higher = thins sooner)
};

// pose: model.derive().pose; ch: numeric channels of this frame (anything not listed above is
// ignored); time: seconds; parallax: {x, y} pointer parallax; board (#350): { e (0..1 eased
// progress of the board), aside (art px the character group moves right at e = 1), pan (art
// px the room pans), lift (art px the room moves up: the camera looks lower; the desk shot on a
// wide screen) }. Returns plain numbers only.
export function roomFrame(pose, ch, time, parallax, board = null) {
  const v = (k) => (ROOM_CHANNELS.includes(k) && typeof ch[k] === 'number' ? ch[k] : 0);
  const shake = v('shake') > 0.5 ? (Math.floor(time * 30) % 2) * 2 - 1 : 0;
  const amber = pose.neon === 'amber';
  const desk = pose['light.desk'] ? 1 : 0;
  const shelf = pose['light.shelf'] ? 1 : 0;
  const row = { procedural: 0.13, semantic: 0.38, episodic: 0.63 }[pose['gadget.shelf.row']];
  let deskLevel = desk ? 0.65 + 0.5 * v('light.desk.pulse') : 0.12;
  if (pose['gadget.glitch'] === 'scope' && v('gadget.glitch') > 0.5) deskLevel *= 0.15; // generic tool failed
  const e = board && Number.isFinite(board.e) ? Math.min(1, Math.max(0, board.e)) : 0;
  const px = (k) => (board && Number.isFinite(board[k]) ? board[k] : 0);
  return {
    // the board pans the room left (the deeper a layer, the less it moves: cam × depth)
    cam: [-Math.round(e * px('pan')), -Math.round(e * px('lift')) || 0],
    aside: [Math.round(e * px('aside')), 0], // the character group only (layerOffset)
    shake: [shake, 0], // applied to depth >= SHAKE_MIN_DEPTH only (layerOffset)
    parallax: [parallax.x, parallax.y],
    neon: {
      colour: pose.neon,
      level: pose.neonLevel * (amber ? 0.85 + 0.15 * Math.sin(time * AMBER_RATE) : 1),
      core: 1 + v('neon.ambient'), // ambient brightens / dims the tube cores only, not the dithered halo
      flicker: v('neon.flicker'),
    },
    grade: { dim: pose.dim, desat: pose.desat, ambient: pose.neon === 'red' ? [1.04, 0.88, 0.9] : amber ? [1.0, 0.97, 0.93] : [0.96, 0.98, 1.04] },
    desk: { level: deskLevel, scope: desk, scopeTime: desk ? time : 0 },
    shelf: { level: shelf ? 0.8 : 0, scan: row !== undefined && shelf ? row + 0.03 * Math.sin(time * 3) : v('light.shelf.scan'), on: shelf },
    window: pose['light.window'] ? 0.55 + 0.6 * v('light.window.pulse') : 0.18,
    city: Math.floor(v('city.twinkle') * 9) / 9, // stepped: a change every duration / 9
    rain: v('rain.fall'), // phase of the rain streaks (drawn only by the rain sky's layer)
    steam: pose.steam ? v('steam.puff') : 0,
    sparkle: pose.sparkle ? v('sparkle.burst') : 0,
  };
}

// Offset of a layer of `depth` for this frame (the same formula for room, props and character).
export function layerOffset(frame, depth) {
  const [sx, sy] = depth >= SHAKE_MIN_DEPTH && frame.shake ? frame.shake : [0, 0];
  const ax = depth >= SHAKE_MIN_DEPTH && frame.aside ? frame.aside[0] : 0;
  return [
    Math.round(-frame.parallax[0] * 6 * depth + frame.cam[0] * depth) + sx + ax,
    Math.round(-frame.parallax[1] * 3 * depth + frame.cam[1] * depth) + sy,
  ];
}
