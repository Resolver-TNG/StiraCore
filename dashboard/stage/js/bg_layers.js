// Background layers of the Stage room (#341): declarative data, validated and frozen.
//
// The room behind the character is a stack of flat pixel-art images (tools/build_bg.py), each
// with its own parallax depth (1 = the character). Bands, back to front:
//   far  — the world outside the window: sky, the city, rain, the window's light
//   mid  — the room's shell: window glass, wall (frame, blind, curtains, posters), neon, pipes
//   near — furniture: the shelf (memory) and the desk (tools) with their light layers
//   fore — dark shapes nearest the viewer, never over the character (checked when built)
// The renderer builds one mesh per entry; nothing here is evaluated. A `role` names the layer
// the renderer drives from *events* (room.js: window = web, shelf = memory, desk = tools,
// neon = breaker / approval, city / rain = the listed ambient). Sky variants (night, dawn,
// rain) swap the far band's pictures; they never change what an event shows.

export const BG_BANDS = ['far', 'mid', 'near', 'fore'];
export const BG_KINDS = ['color', 'glow', 'neon', 'rain'];
export const BG_ROLES = ['city', 'rain', 'window', 'neon', 'shelf', 'desk'];
// Depth range per band (the character is 1.0; gadgets in front are 1.18-1.3).
export const BAND_DEPTH = { far: [0, 0.2], mid: [0.2, 0.5], near: [0.5, 0.95], fore: [1.05, 1.15] };
// Render order bounds: the character is 10, gadgets in front 10.3, steam 11.
export const BAND_ORDER = { far: [-2, 1.9], mid: [2, 5.9], near: [6, 9.9], fore: [10.1, 10.25] };

export const SKIES = {
  night: { description: 'Night (default): moon and stars, lit windows, neon signs.' },
  dawn: { description: 'Dawn: warm horizon, a few windows still lit, thin clouds.' },
  rain: { description: 'Rainy night: low clouds, haze, rain falling past the window, drops on the glass.' },
};
export const DEFAULT_SKY = 'night';

// `{sky}` in a file name is replaced by the sky variant. `skies` limits a layer to variants.
export const BG_LAYERS = [
  { id: 'sky', band: 'far', kind: 'color', file: 'sky_{sky}', depth: 0.06, order: -1, description: 'Sky, moon or clouds, the far skyline.' },
  { id: 'city', band: 'far', kind: 'color', file: 'city_{sky}', depth: 0.12, order: 0, role: 'city', description: 'Nearer blocks: lit windows, rooftop gear, signs, the elevated line.' },
  { id: 'rain', band: 'far', kind: 'rain', depth: 0.16, order: 0.5, role: 'rain', skies: ['rain'], description: 'Rain streaks falling past the window (ambient.rain).' },
  { id: 'window_glow', band: 'far', kind: 'glow', file: 'window_glow_{sky}', depth: 0.12, order: 1, role: 'window', description: 'Light from the window while the web is used.' },
  { id: 'glass', band: 'mid', kind: 'color', file: 'glass', depth: 0.3, order: 2, skies: ['night', 'dawn'], description: 'Reflections on the pane.' },
  { id: 'glass_rain', band: 'mid', kind: 'color', file: 'glass_rain', depth: 0.3, order: 2, skies: ['rain'], description: 'Drops and trails on the pane.' },
  { id: 'wall', band: 'mid', kind: 'color', file: 'wall', depth: 0.3, order: 3, description: 'Wall, ceiling, window frame, blind, curtains, posters, conduit, vent.' },
  { id: 'neon', band: 'mid', kind: 'neon', file: 'neon', depth: 0.34, order: 4, role: 'neon', description: 'Neon orbits: breaker level / approval colour.' },
  { id: 'pipes', band: 'mid', kind: 'color', file: 'pipes', depth: 0.42, order: 5, description: 'Pipes, straps, cables, valve, gauge, junction box.' },
  { id: 'shelf', band: 'near', kind: 'color', file: 'shelf', depth: 0.55, order: 6, description: 'Shelf = memory: procedural / semantic / episodic compartments and the archive row.' },
  { id: 'shelf_glow', band: 'near', kind: 'glow', file: 'shelf_glow', depth: 0.55, order: 7, role: 'shelf', description: 'Shelf LEDs and liquids while memory is used.' },
  { id: 'desk', band: 'near', kind: 'color', file: 'desk', depth: 0.8, order: 8, description: 'Desk = tools: oscilloscope, rack with patch cables, monitor, lamp.' },
  { id: 'desk_glow', band: 'near', kind: 'glow', file: 'desk_glow', depth: 0.8, order: 9, role: 'desk', description: 'Scope, LEDs, monitor lines while a tool runs.' },
  { id: 'fore', band: 'fore', kind: 'color', file: 'fore', depth: 1.1, order: 10.2, description: 'Pendant lamp and leaves in the corners, off the character.' },
];

const ID_RE = /^[a-z][a-z0-9_]{0,23}$/;
const FILE_RE = /^[a-z][a-z0-9_]{0,31}$/; // also within the page's asset allow-list (stage.py)
const inRange = (v, [lo, hi]) => typeof v === 'number' && Number.isFinite(v) && v >= lo && v <= hi;

// Returns a frozen copy of the entry for this sky, or null when it is not acceptable as data
// (unknown band / kind / role, depth or order outside the band, bad id or file name).
export function validateLayer(raw, sky = DEFAULT_SKY) {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return null;
  if (!ID_RE.test(raw.id) || !BG_BANDS.includes(raw.band) || !BG_KINDS.includes(raw.kind)) return null;
  if (!inRange(raw.depth, BAND_DEPTH[raw.band]) || !inRange(raw.order, BAND_ORDER[raw.band])) return null;
  if (raw.role !== undefined && !BG_ROLES.includes(raw.role)) return null;
  if (raw.skies !== undefined && !(Array.isArray(raw.skies) && raw.skies.length && raw.skies.every((s) => Object.hasOwn(SKIES, s)))) return null;
  let file = null;
  if (raw.kind !== 'rain') {
    if (typeof raw.file !== 'string') return null;
    file = raw.file.replace('{sky}', sky);
    if (!FILE_RE.test(file)) return null;
  } else if (raw.file !== undefined) return null; // rain is drawn by the shader
  const out = { id: raw.id, band: raw.band, kind: raw.kind, file, depth: raw.depth, order: raw.order };
  if (raw.role) out.role = raw.role;
  if (raw.skies) out.skies = Object.freeze([...raw.skies]);
  return Object.freeze(out);
}

// The layers to draw for a sky variant, back to front. Unknown skies fall back to the default.
// Returns { list, rejected }: rejected entries are not drawn (the page logs them).
export function layersFor(sky, entries = BG_LAYERS) {
  const s = Object.hasOwn(SKIES, sky) ? sky : DEFAULT_SKY;
  const list = [];
  const rejected = [];
  const roles = new Set();
  for (const raw of entries) {
    if (raw && Array.isArray(raw.skies) && !raw.skies.includes(s)) continue;
    const e = validateLayer(raw, s);
    if (!e || list.some((x) => x.id === e.id) || (e.role && roles.has(e.role))) {
      rejected.push(raw && raw.id);
      continue;
    }
    if (e.role) roles.add(e.role);
    list.push(e);
  }
  list.sort((a, b) => a.order - b.order);
  return { sky: s, list: Object.freeze(list), rejected };
}

// Sky from the page settings: `?sky=night|dawn|rain`, or `?sky=auto` = from the local clock
// (05:00-06:59 dawn, otherwise night; the clock cannot tell rain). Anything else: the default.
export function pickSky(param, hour) {
  if (typeof param === 'string' && Object.hasOwn(SKIES, param)) return param;
  if (param === 'auto' && Number.isInteger(hour)) return hour >= 5 && hour < 7 ? 'dawn' : 'night';
  return DEFAULT_SKY;
}
