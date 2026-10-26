// Desk shot (#341, night round B): she walks to the room desk, sits on the chair with her back
// to us and types there, then walks back. The frames are new parts (assets/char/desk.png,
// desk.json, built by tools/char_desk.py from the back view); which one shows is motion data:
// the `desk` and `tail` tracks of the motion catalog (typing_back, watch_back). While a desk
// frame is on, the character layer at the front (and its hair layer) is not drawn.
//
// The sprites stand in the room at the desk's depth (0.8, like the laptop on it): the room's
// parallax moves them with the desk, never the character's motion. Drawn with the room's
// grading (dim / desaturation / ambient), so CRITICAL greys her as it greys the room.

import * as THREE from '../vendor/three.stage.min.js';
import { TAIL_BACK } from './char_rig.js';
import { layerOffset } from './room.js';

export const SIT_SCALE = 0.4; // the seated figure's scale (tools/char_desk.py)
const SHEET = 278;

const WORLD_H = 340;
export const DESK_DEPTH = 0.8;
// render order: after the laptop on the room desk (gadgets 'desk' 9.6x), before the character
// layer (10) and the props in front (10.3)
const ORDER = { monitor: 9.605, mug: 9.612, body: 9.62, chair: 9.63, tail: 9.64 }; // monitor: behind the laptop
const CHAIR_BEHIND = 9.615; // while she walks on our side of it, the chair is behind her

const VERT = `
varying vec2 vUv;
void main(){ vUv = uv; gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0); }`;

const FRAG = `
uniform sampler2D map; uniform vec2 uAtlas; uniform vec4 uRect;
uniform float uDim; uniform float uDesat; uniform vec3 uAmbient;
uniform vec3 uRimColor; uniform float uRim;
varying vec2 vUv;
vec3 grade(vec3 c){
  float l = dot(c, vec3(0.299, 0.587, 0.114));
  c = mix(c, vec3(l) * vec3(0.92, 0.95, 1.08), uDesat);
  return c * uAmbient * (1.0 - uDim);
}
float alphaAt(vec2 p){
  if (p.x < 0.0 || p.y < 0.0 || p.x >= uRect.z || p.y >= uRect.w) return 0.0;
  return texture2D(map, (uRect.xy + p + 0.5) / uAtlas).a;
}
void main(){
  vec2 ip = vec2(floor(vUv.x * uRect.z), floor((1.0 - vUv.y) * uRect.w));
  vec4 c = texture2D(map, (uRect.xy + ip + 0.5) / uAtlas);
  if (c.a < 0.5) discard;
  vec3 col = grade(c.rgb);
  // the laptop's light on her outline (the pixel just inside the edge, top and sides)
  bool edge = alphaAt(ip + vec2(1.0, 0.0)) < 0.5 || alphaAt(ip - vec2(1.0, 0.0)) < 0.5 || alphaAt(ip - vec2(0.0, 1.0)) < 0.5;
  if (edge) col = mix(col, uRimColor, 0.35 * uRim);
  gl_FragColor = vec4(col, 1.0);
}`;

function loadTexture(url) {
  return new Promise((resolve, reject) => {
    new THREE.TextureLoader().load(
      url,
      (t) => {
        t.magFilter = THREE.NearestFilter;
        t.minFilter = THREE.NearestFilter;
        t.generateMipmaps = false;
        t.flipY = false;
        t.colorSpace = THREE.NoColorSpace;
        resolve(t);
      },
      undefined,
      () => reject(new Error(`texture ${url}`)),
    );
  });
}

// Pure: which sprites show for a motion frame ({ desk, tail }) and where (image coords, top-left).
// meta: desk.json. narrow: the phone layout (the desk props shift left with the laptop).
// Box average with premultiplied alpha to round(size * s), snapped to the nearest palette
// colour (the same as tools/char_desk.py downscale): whole pixels, alpha 0 / 255.
// src: { width, height, data } RGBA; part: { ax, ay, w, h }; palette: [[r, g, b], ...].
export function downscaleSprite(src, part, s, palette) {
  const { w, h } = part;
  const nw = Math.max(1, Math.round(w * s));
  const nh = Math.max(1, Math.round(h * s));
  const acc = new Float64Array(nw * nh * 4);
  const cnt = new Float64Array(nw * nh);
  for (let y = 0; y < h; y += 1) {
    const Y = Math.floor((y * nh) / h);
    for (let x = 0; x < w; x += 1) {
      const X = Math.floor((x * nw) / w);
      const q = ((part.ay + y) * src.width + part.ax + x) * 4;
      const a = src.data[q + 3] / 255;
      const k = Y * nw + X;
      acc[k * 4] += src.data[q] * a;
      acc[k * 4 + 1] += src.data[q + 1] * a;
      acc[k * 4 + 2] += src.data[q + 2] * a;
      acc[k * 4 + 3] += a;
      cnt[k] += 1;
    }
  }
  const data = new Uint8ClampedArray(nw * nh * 4);
  for (let k = 0; k < nw * nh; k += 1) {
    if (!(acc[k * 4 + 3] / cnt[k] > 0.5)) continue;
    const c = [0, 1, 2].map((i) => acc[k * 4 + i] / acc[k * 4 + 3]);
    let best = 0;
    let bd = Infinity;
    palette.forEach((p, i) => {
      const d = (p[0] - c[0]) ** 2 + (p[1] - c[1]) ** 2 + (p[2] - c[2]) ** 2;
      if (d < bd) {
        bd = d;
        best = i;
      }
    });
    data.set([...palette[best], 255], k * 4);
  }
  return { w: nw, h: nh, data };
}

// A 1 px edge for a downscaled sprite: light pixels on the silhouette (a transparent
// 4-neighbour) take `edge` — at 0.4 the fur's own soft edge reads as a blur.
export function edgeSprite(sp, edge, minLuma = 150) {
  const { w, h, data } = sp;
  const out = new Uint8ClampedArray(data);
  const opaque = (x, y) => x >= 0 && y >= 0 && x < w && y < h && data[(y * w + x) * 4 + 3] === 255;
  for (let y = 0; y < h; y += 1) {
    for (let x = 0; x < w; x += 1) {
      const q = (y * w + x) * 4;
      if (data[q + 3] !== 255) continue;
      if (opaque(x - 1, y) && opaque(x + 1, y) && opaque(x, y - 1) && opaque(x, y + 1)) continue;
      const l = (299 * data[q] + 587 * data[q + 1] + 114 * data[q + 2]) / 1000;
      if (l > minLuma) out.set(edge, q);
    }
  }
  return { w, h, data: out };
}

// The desk shot's tail: round A's back-view tail (extras.json tail.back l1 / c / r1) at the
// seated figure's scale, placed where it hangs on the back sheet (anchor: the sheet's top left)
// relative to the seated figure. palette: the fur ramp it snaps to (light to dark; the second
// darkest is its edge). -> { size, frames: { name: { rect, off } }, data } (one strip).
export function deskTails(extrasMeta, extrasAtlas, palette) {
  const set = extrasMeta && extrasMeta.tail && extrasMeta.tail.back;
  if (!set) return null;
  const names = ['l1', 'c', 'r1'].filter((n) => set[n]);
  const edge = palette[Math.max(0, palette.length - 3)];
  const sprites = names.map((n) => edgeSprite(downscaleSprite(extrasAtlas, set[n], SIT_SCALE, palette), edge));
  const W = sprites.reduce((a, sp) => a + sp.w + 1, 0);
  const H = Math.max(...sprites.map((sp) => sp.h));
  const data = new Uint8ClampedArray(W * H * 4);
  const frames = {};
  let x = 0;
  names.forEach((n, i) => {
    const sp = sprites[i];
    for (let y = 0; y < sp.h; y += 1) data.set(sp.data.subarray(y * sp.w * 4, (y + 1) * sp.w * 4), (y * W + x) * 4);
    frames[n] = { rect: [x, 0, sp.w, sp.h], off: [Math.round(set[n].ox * SIT_SCALE), Math.round(set[n].oy * SIT_SCALE)] };
    x += sp.w + 1;
  });
  return { size: [W, H], frames, data };
}

// tails: deskTails() (or null: no tail).
export function deskSprites(meta, frame, narrow = false, tails = null) {
  const body = frame && typeof frame.desk === 'string' && frame.desk !== 'none' && meta.frames[frame.desk] ? frame.desk : null;
  if (!body) return [];
  const dx = narrow ? meta.narrowDx || 0 : 0;
  const at = (id) => [meta.frames[id].at[0] + dx, meta.frames[id].at[1]];
  const out = [{ part: 'body', id: body, at: at(body) }];
  // the chair stands at the desk; she walks on our side of it (it is behind her) until she reaches it
  const atChair = body === 'stand' || body === 'turn' || meta.seated.includes(body);
  out.push({ part: 'chair', id: 'chair', at: at('chair'), behind: !atChair });
  out.push({ part: 'monitor', id: 'monitor', at: at('monitor') });
  if (meta.seated.includes(body)) {
    if (tails) {
      // the seated frame is the back sheet at SIT_SCALE with a pad on both sides (her arms)
      const r = meta.frames[body].rect;
      const pad = Math.round((r[2] - Math.round(SHEET * SIT_SCALE)) / 2);
      const name = TAIL_BACK[frame.tail] || 'c';
      const t = tails.frames[name] || tails.frames.c;
      const [bx, by] = at(body);
      out.push({ part: 'tail', id: name, at: [bx + pad + t.off[0], by + t.off[1]], rect: t.rect });
    }
    out.push({ part: 'mug', id: 'mug', at: at('mug') });
  }
  return out;
}

// On a wide screen the text window covers the room below the desk top (a phone has it under
// the view). While she sits at the desk the camera looks lower — the room moves up by `lift`
// art px (x the desk's depth at the desk) — until `showRow` (the chair's seat) is above the
// window; at most `max` px, so the world's bottom edge stays in the view.
// rowsAbove: image rows of the view above the text window's top; viewTop: the view's first row.
export const DESK_SHOW_ROW = 258;
export const DESK_LIFT_MAX = 36;
export function deskLiftFor({ portrait, rowsAbove, viewTop }) {
  if (portrait || !Number.isFinite(rowsAbove)) return 0;
  return Math.max(0, Math.min(DESK_LIFT_MAX, Math.ceil((DESK_SHOW_ROW - (viewTop + rowsAbove)) / DESK_DEPTH)));
}

export class DeskSeat {
  // rig: the CharRig (its parts.json palette and round A's extras, for the tail)
  static async load(scene, base, rig = null) {
    const [meta, tex] = await Promise.all([fetch(`${base}/char/desk.json`).then((r) => r.json()), loadTexture(`${base}/char/desk.png`)]);
    let tails = null;
    if (rig && rig.extras && rig.meta.palette) {
      // the fur only: the hair ramp (no skin or coat tones from averaging grey with dark)
      const palette = ['hair5', 'hair4', 'hair3', 'hair2', 'hair1', 'hair0']
        .filter((k) => rig.meta.palette[k])
        .map((k) => [1, 3, 5].map((i) => parseInt(rig.meta.palette[k].slice(i, i + 2), 16)));
      tails = deskTails(rig.extras.meta, rig.extras.atlas, palette);
    }
    return new DeskSeat(scene, meta, tex, tails);
  }

  constructor(scene, meta, tex, tails = null) {
    this.stage = scene;
    this.meta = meta;
    this.tails = tails;
    let tailTex = null;
    if (tails) {
      const cv = document.createElement('canvas');
      cv.width = tails.size[0];
      cv.height = tails.size[1];
      cv.getContext('2d').putImageData(new ImageData(tails.data, tails.size[0], tails.size[1]), 0, 0);
      tailTex = new THREE.Texture(cv);
      Object.assign(tailTex, { magFilter: THREE.NearestFilter, minFilter: THREE.NearestFilter, generateMipmaps: false, flipY: false, premultiplyAlpha: false, colorSpace: THREE.NoColorSpace, needsUpdate: true });
    }
    this.active = false;
    this.parts = {};
    for (const part of Object.keys(ORDER)) {
      const mat = new THREE.ShaderMaterial({
        uniforms: {
          map: { value: part === 'tail' && tailTex ? tailTex : tex },
          uAtlas: { value: part === 'tail' && tails ? new THREE.Vector2(tails.size[0], tails.size[1]) : new THREE.Vector2(meta.size[0], meta.size[1]) },
          uRect: { value: new THREE.Vector4(0, 0, 1, 1) },
          uRimColor: { value: new THREE.Vector3(0.32, 0.9, 1.0) },
          uRim: { value: 0 },
          ...scene.grade,
        },
        vertexShader: VERT,
        fragmentShader: FRAG,
        depthTest: false,
        depthWrite: false,
      });
      const mesh = new THREE.Mesh(new THREE.PlaneGeometry(1, 1), mat);
      mesh.renderOrder = ORDER[part];
      mesh.frustumCulled = false;
      mesh.visible = false;
      scene.scene.add(mesh);
      this.parts[part] = { mesh, id: null };
    }
  }

  // Call after scene.update(); frame: the motion player's frame. Returns true while she is at
  // (or on the way to) the desk: the caller then hides the character layer and its hair.
  update(frame) {
    const narrow = Boolean(this.stage.gadgets && this.stage.gadgets.narrow);
    const list = deskSprites(this.meta, frame, narrow, this.tails);
    const room = this.stage.roomState;
    const view = room ? { ...room, shake: null, aside: null } : null;
    const [ox, oy] = view ? layerOffset(view, DESK_DEPTH) : [0, 0];
    const shown = new Set();
    for (const s of list) {
      const p = this.parts[s.part];
      const r = s.rect || this.meta.frames[s.id].rect;
      const u = p.mesh.material.uniforms;
      if (p.id !== s.id) {
        u.uRect.value.set(...r);
        p.mesh.scale.set(r[2], r[3], 1);
        p.id = s.id;
      }
      u.uRim.value = s.part === 'body' || s.part === 'tail' ? 1 : s.part === 'monitor' ? 0 : 0.5;
      if (s.part === 'chair') p.mesh.renderOrder = s.behind ? CHAIR_BEHIND : ORDER.chair;
      p.mesh.position.set(s.at[0] + r[2] / 2 + ox, WORLD_H - (s.at[1] + r[3] / 2) - oy, 0);
      p.mesh.visible = true;
      shown.add(s.part);
    }
    for (const [k, p] of Object.entries(this.parts)) if (!shown.has(k)) p.mesh.visible = false;
    this.active = list.length > 0;
    return this.active;
  }
}
