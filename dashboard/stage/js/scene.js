// Stage renderer (#341): three.js, rendered at a low internal resolution into a
// NearestFilter render target and blitted to the canvas at an integer scale.

import * as THREE from '../vendor/three.stage.min.js';
import { buildGadgets, GADGETS } from './gadget_catalog.js';
import { GadgetLayer } from './gadget_scene.js';
import { layersFor } from './bg_layers.js';
import { layerOffset, roomFrame, STEAM } from './room.js';

const WORLD_W = 640;
const WORLD_H = 340;
const VIEW_TOP = 330; // world y of the top edge of the view
const CHAR_POS = [181, 20]; // image coords of the character sheet's top-left

const BAYER = `
float bayer4(vec2 p){
  vec2 q = mod(floor(p), 4.0);
  int i = int(q.x + q.y * 4.0);
  float m[16];
  m[0]=0.0;m[1]=8.0;m[2]=2.0;m[3]=10.0;m[4]=12.0;m[5]=4.0;m[6]=14.0;m[7]=6.0;
  m[8]=3.0;m[9]=11.0;m[10]=1.0;m[11]=9.0;m[12]=15.0;m[13]=7.0;m[14]=13.0;m[15]=5.0;
  for (int k = 0; k < 16; k++) { if (k == i) return (m[k] + 0.5) / 16.0; }
  return 0.5;
}`;

const GRADE = `
uniform float uDim; uniform float uDesat; uniform vec3 uAmbient;
vec3 grade(vec3 c){
  float l = dot(c, vec3(0.299, 0.587, 0.114));
  c = mix(c, vec3(l) * vec3(0.92, 0.95, 1.08), uDesat);
  return c * uAmbient * (1.0 - uDim);
}`;

const VERT = `
varying vec2 vUv;
void main(){ vUv = uv; gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0); }`;

const LAYER_FRAG = `
uniform sampler2D map; uniform vec2 uSize; uniform float uTwinkle; uniform vec4 uTint; uniform vec4 uRect;
varying vec2 vUv;
${GRADE}
void main(){
  vec2 ip = uRect.xy + vec2(floor(vUv.x * uRect.z), floor((1.0 - vUv.y) * uRect.w)); // room pixel
  vec4 c = texture2D(map, (ip + 0.5) / uSize);
  if (c.a < 0.5) discard;
  // ambient (room.js, city layer only): a few lit windows off, the set changes every few seconds
  if (uTwinkle >= 0.0 && ip.y > 60.0 && dot(c.rgb, vec3(0.299, 0.587, 0.114)) > 0.42) {
    float h = fract(sin(dot(ip + floor(uTwinkle * 9.0 + 0.5) * vec2(3.1, 7.7), vec2(12.9898, 78.233))) * 43758.5453);
    if (h < 0.07) c.rgb *= 0.3;
  }
  gl_FragColor = vec4(mix(grade(c.rgb), uTint.rgb, uTint.a), 1.0); // uTint: ?layers=1 band colour
}`;

// Rain past the window (bg_layers.js 'rain', rain sky only): slanted 1 px streaks, phase from
// the listed ambient entry (room.js). One period of the pattern is 64 rows; a phase of 0..1
// moves it 640 rows, so the loop is seamless.
const RAIN_FRAG = `
uniform vec2 uSize; uniform vec4 uWin; uniform float uPhase; uniform vec4 uRect;
varying vec2 vUv;
${GRADE}
float h1(float n){ return fract(sin(n * 12.9898) * 43758.5453); }
void main(){
  vec2 ip = uRect.xy + vec2(floor(vUv.x * uRect.z), floor((1.0 - vUv.y) * uRect.w)); // room pixel
  if (ip.x < uWin.x || ip.y < uWin.y || ip.x >= uWin.x + uWin.z || ip.y >= uWin.y + uWin.w) discard;
  float s = ip.y - floor(uPhase * 640.0 + 0.5);
  float col = ip.x + floor(s / 8.0);
  float r = h1(col);
  if (r > 0.3) discard;
  float len = 3.0 + floor(h1(col + 7.0) * 4.0);
  float m = mod(s + floor(r * 640.0), 64.0);
  if (m >= len) discard;
  vec3 c = m > len - 1.5 ? vec3(0.42, 0.47, 0.64) : vec3(0.25, 0.29, 0.43);
  gl_FragColor = vec4(grade(c), 1.0);
}`;

// Additive light layer. Fractional levels grow as ordered dither, never as a smooth haze.
const GLOW_FRAG = `
uniform sampler2D map; uniform vec2 uSize; uniform float uLevel; uniform float uTime; uniform vec4 uRect;
uniform vec4 uScan; uniform vec4 uScope; uniform float uScopeOn;
varying vec2 vUv;
${BAYER}
void main(){
  vec2 ip = uRect.xy + vec2(floor(vUv.x * uRect.z), floor((1.0 - vUv.y) * uRect.w)); // room pixel
  vec3 e = texture2D(map, (ip + 0.5) / uSize).rgb;
  float b = bayer4(ip);
  float lvl = uLevel;
  if (uScan.w > 0.0) {
    float y = mix(uScan.x, uScan.y, uScan.z);
    lvl = uLevel * (0.55 + 0.9 * smoothstep(18.0, 0.0, abs(ip.y - y)));
  }
  vec3 col = e * step(b, lvl) * min(lvl, 1.4);
  if (uScopeOn > 0.5 && ip.x >= uScope.x && ip.x < uScope.x + uScope.z && ip.y >= uScope.y && ip.y < uScope.y + uScope.w) {
    float lx = ip.x - uScope.x;
    float wy = uScope.y + uScope.w * 0.5 + floor(sin(lx * 0.42 - uTime * 9.0) * 5.0 * (0.6 + 0.4 * sin(uTime * 1.7)) + 0.5);
    if (abs(ip.y - wy) < 0.5) col += vec3(0.62, 0.94, 0.54);
    else if (abs(ip.y - wy) < 1.5 && b < 0.4) col += vec3(0.14, 0.36, 0.2);
  }
  if (dot(col, col) < 1e-5) discard;
  gl_FragColor = vec4(col, 1.0);
}`;

const NEON_FRAG = `
uniform sampler2D map; uniform vec2 uSize; uniform vec3 uColor; uniform float uLevel; uniform vec4 uRect;
uniform float uFlicker; uniform float uTime; uniform float uCore;
varying vec2 vUv;
${BAYER}
void main(){
  vec2 ip = uRect.xy + vec2(floor(vUv.x * uRect.z), floor((1.0 - vUv.y) * uRect.w)); // room pixel
  vec3 d = texture2D(map, (ip + 0.5) / uSize).rgb;
  float lvl = uLevel;
  if (d.b > 0.75) lvl *= 1.0 - uFlicker * 0.85;
  float b = bayer4(ip);
  vec3 col = vec3(0.0);
  if (d.r > 0.9) col = mix(uColor, vec3(1.0), 0.5) * lvl * uCore;
  else if (d.r > 0.4) col = uColor * 0.95 * lvl * uCore;
  else {
    float g = d.g;
    if (b < g * g * 0.95 * lvl) col = uColor * 0.42;
    else if (b < g * 0.45 * lvl) col = uColor * 0.13;
  }
  if (dot(col, col) < 1e-5) discard;
  gl_FragColor = vec4(col, 1.0);
}`;

const CHAR_FRAG = `
uniform sampler2D tBase; uniform sampler2D tRig; uniform sampler2D tRig2; uniform sampler2D tFace;
uniform vec2 uSize; uniform vec2 uFaceSize;
uniform vec2 uHead; uniform float uBreath; uniform float uSway; uniform float uEarL; uniform float uEarR;
uniform float uArm;
uniform vec4 uEyeBox; uniform vec4 uMouthBox; uniform vec4 uBlushBox; uniform vec4 uSweatBox;
uniform float uEyes; uniform float uMouth; uniform float uBlush; uniform float uSweat;
uniform float uMouthRow; uniform float uBlushRow; uniform float uSweatRow;
uniform vec3 uRimColor; uniform float uRim; uniform float uLayers;
varying vec2 vUv;
${GRADE}
vec4 tx(sampler2D t, vec2 ip, vec2 sz){ return texture2D(t, (ip + 0.5) / sz); }
bool inBox(vec2 p, vec4 b){ return p.x >= b.x && p.y >= b.y && p.x < b.x + b.z && p.y < b.y + b.w; }
float alphaAt(vec2 p){ if (p.x < 0.0 || p.y < 0.0 || p.x >= uSize.x || p.y >= uSize.y) return 0.0; return tx(tBase, p, uSize).a; }
void main(){
  vec2 ip = vec2(floor(vUv.x * uSize.x), floor((1.0 - vUv.y) * uSize.y));
  vec4 r1 = tx(tRig, ip, uSize);
  vec4 r2 = tx(tRig2, ip, uSize);
  vec2 off = uHead * r1.r + vec2(0.0, uBreath) * r1.b + vec2(uSway * r1.g, 0.0)
    + vec2(0.6 * uEarL, 0.8 * uEarL) * r2.r + vec2(-0.6 * uEarR, 0.8 * uEarR) * r2.g
    + vec2(0.0, uArm) * r2.b;
  vec2 sp = ip - floor(off + 0.5);
  vec4 c = vec4(0.0);
  if (sp.x >= 0.0 && sp.y >= 0.0 && sp.x < uSize.x && sp.y < uSize.y) c = tx(tBase, sp, uSize);
  if (c.a > 0.5) {
    if (inBox(sp, uEyeBox)) {
      vec4 f = tx(tFace, sp - uEyeBox.xy + vec2(0.0, uEyes * uEyeBox.w), uFaceSize);
      if (f.a > 0.5) c = f;
    }
    if (inBox(sp, uMouthBox)) {
      vec4 f = tx(tFace, sp - uMouthBox.xy + vec2(0.0, uMouthRow + uMouth * uMouthBox.w), uFaceSize);
      if (f.a > 0.5) c = f;
    }
    if (uBlush > 0.5 && inBox(sp, uBlushBox)) {
      vec4 f = tx(tFace, sp - uBlushBox.xy + vec2(0.0, uBlushRow), uFaceSize);
      if (f.a > 0.5) c.rgb = mix(c.rgb, f.rgb, 0.75);
    }
    // rim light from the neon behind: the pixel just inside the outline
    bool edge2 = alphaAt(sp + vec2(2.0, 0.0)) < 0.5 || alphaAt(sp - vec2(2.0, 0.0)) < 0.5 || alphaAt(sp - vec2(0.0, 2.0)) < 0.5;
    bool edge1 = alphaAt(sp + vec2(1.0, 0.0)) < 0.5 || alphaAt(sp - vec2(1.0, 0.0)) < 0.5 || alphaAt(sp - vec2(0.0, 1.0)) < 0.5;
    // the face effects (tRig2 alpha, CharRig.fxMask) keep their own colours: drawn after the
    // room's grading, so a red CRITICAL light does not hide the blue lines or the sweat
    bool fxPx = r2.a > 0.5;
    if (!fxPx) c.rgb = grade(c.rgb);
    if (edge2 && !edge1 && sp.y < 205.0 && !fxPx) c.rgb = mix(c.rgb, uRimColor, 0.55 * uRim);
  }
  if (uSweat > 0.5 && inBox(sp, uSweatBox)) {
    vec4 f = tx(tFace, sp - uSweatBox.xy + vec2(0.0, uSweatRow), uFaceSize);
    if (f.a > 0.5) c = vec4(f.rgb, 1.0);
  }
  if (c.a < 0.5) discard;
  if (uLayers > 0.5) {
    vec3 tint = vec3(0.35, 0.4, 0.9);
    if (r1.r > 0.5) tint = vec3(0.95, 0.75, 0.3);
    if (r2.r > 0.0 || r2.g > 0.0) tint = vec3(0.9, 0.3, 0.6);
    if (r2.b > 0.5) tint = vec3(0.3, 0.9, 0.5);
    if (r1.g > 0.3 && r1.r < 0.5) tint = vec3(0.4, 0.85, 0.95);
    c.rgb = mix(c.rgb, tint, 0.45);
  }
  gl_FragColor = vec4(c.rgb, 1.0);
}`;

const STEAM_VERT = `
attribute float aSize; attribute float aAlpha;
varying float vAlpha;
void main(){ vAlpha = aAlpha; gl_PointSize = aSize; gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0); }`;

// Steam is drawn in two passes: particles add density into their own target, then one
// composite pass turns density into 3 flat tones + ordered dither (pixel smoke, no blur).
const STEAM_FRAG = `
varying float vAlpha;
void main(){
  vec2 q = gl_PointCoord * 2.0 - 1.0;
  float r2 = dot(q, q);
  if (r2 > 1.0) discard;
  gl_FragColor = vec4(vec3((1.0 - r2) * vAlpha * 0.42), 1.0);
}`;

const STEAM_COMP_FRAG = `
uniform sampler2D tDensity; uniform vec2 uRes;
uniform vec3 uLight; uniform vec3 uMid; uniform vec3 uShade;
${BAYER}
void main(){
  vec2 ip = floor(gl_FragCoord.xy);
  float d = texture2D(tDensity, (ip + 0.5) / uRes).r;
  float b = bayer4(ip);
  vec3 c;
  if (d > 0.7) c = uLight;
  else if (d > 0.36) c = (d > 0.36 + b * 0.26) ? uLight : uMid;
  else if (d > 0.12) c = (d > 0.12 + b * 0.24) ? uMid : uShade;
  else if (d > b * 0.12 + 0.02) c = uShade;
  else discard;
  gl_FragColor = vec4(c, 1.0);
}`;

const SPARK_FRAG = `
varying float vAlpha;
void main(){
  vec2 q = floor(gl_PointCoord * 11.0) - 5.0;
  float arm = floor(5.0 * vAlpha + 0.5);
  bool plus = (q.x == 0.0 && abs(q.y) <= arm) || (q.y == 0.0 && abs(q.x) <= arm);
  bool diag = abs(q.x) == 1.0 && abs(q.y) == 1.0 && arm >= 4.0;
  if (!plus && !diag) discard;
  float r = abs(q.x) + abs(q.y);
  gl_FragColor = vec4(r < 1.0 ? vec3(1.0) : r < 3.0 && !diag ? vec3(1.0, 0.95, 0.78) : vec3(1.0, 0.78, 0.36), 1.0);
}`;

// uKeep (#350, the character pass of the two-canvas view): keep the target's alpha, so the
// canvas is transparent where nothing of her is drawn (premultiplied: a light drawn there has
// alpha 0 and adds to what is behind, as it would in one pass).
const BLIT_FRAG = `
uniform sampler2D tScene; uniform vec2 uRes; uniform float uVignette; uniform float uKeep;
varying vec2 vUv;
${BAYER}
void main(){
  vec2 ip = floor(vUv * uRes);
  vec4 s = texture2D(tScene, (ip + 0.5) / uRes);
  vec3 c = s.rgb;
  vec2 d = (ip + 0.5) / uRes - 0.5;
  float v = dot(d * vec2(1.0, 1.25), d * vec2(1.0, 1.25));
  float k = smoothstep(0.12, 0.42, v) * uVignette;
  if (bayer4(ip) < k) c *= 0.55;
  gl_FragColor = vec4(c, uKeep > 0.5 ? s.a : 1.0);
}`;

// Two canvases (#350 phase 2, docs/stage-main-ui.md §4): while the whiteboard is shown, the
// room is drawn on a back canvas and the character group on the front one, so the board (DOM)
// sits between them. Layer 1 = the character group: her, the hair, the steam from her mug,
// the sparkles and the props at her depth or in front (laptop, lamp, drones).
export const CHAR_LAYER = 1;
// three.js constants that the bundled subset does not export (r186 values, checked at build)
const CUSTOM_BLENDING = 5;
const ADD_EQUATION = 100;
const ZERO_FACTOR = 200;
const ONE_FACTOR = 201;
const SRC_ALPHA_FACTOR = 204;

// ?layers=1: background bands tinted (far blue, mid green, near orange, fore magenta)
const BAND_TINT = { far: [0.25, 0.4, 1.0], mid: [0.3, 0.9, 0.45], near: [1.0, 0.65, 0.2], fore: [1.0, 0.3, 0.8] };

export const NEON_COLORS = {
  cyan: [0.32, 0.9, 1.0],
  amber: [1.0, 0.68, 0.22],
  violet: [0.7, 0.5, 1.0],
  red: [1.0, 0.2, 0.26],
  green: [0.4, 1.0, 0.55],
};

function loadTexture(loader, url) {
  return new Promise((resolve, reject) => {
    loader.load(
      url,
      (t) => {
        t.magFilter = THREE.NearestFilter;
        t.minFilter = THREE.NearestFilter;
        t.generateMipmaps = false;
        t.flipY = false;
        t.colorSpace = THREE.NoColorSpace;
        t.wrapS = THREE.ClampToEdgeWrapping;
        t.wrapT = THREE.ClampToEdgeWrapping;
        resolve(t);
      },
      undefined,
      () => reject(new Error(`texture ${url}`)),
    );
  });
}

// Internal resolution (shared with the still fallback, still_scene.js): keep ~270-320 rows,
// integer scale, never wider than the world. opts.minViewW (portrait): pick the scale in
// *device* pixels so that at least minViewW world pixels fit across (the CSS scale may then be
// fractional, e.g. 4/3 at dpr 3, but every art pixel is still a whole number of device pixels);
// the height is capped to the world (or opts.maxViewH) so the view never shows past it.
export function viewFor(cssW, cssH, dpr, opts = {}) {
  let s;
  let vw;
  let vh;
  let dev;
  if (opts.minViewW) {
    const d = Math.max(1, Math.floor((cssW * dpr) / opts.minViewW), Math.ceil((cssW * dpr) / WORLD_W));
    s = d / dpr;
    vw = Math.max(2, Math.floor((cssW * dpr) / d));
    vh = Math.max(2, Math.min(opts.maxViewH || WORLD_H - 10, Math.floor((cssH * dpr) / d)));
    dev = d;
  } else {
    s = Math.max(1, Math.round(cssH / 290));
    s = Math.max(s, Math.ceil(cssW / WORLD_W), Math.ceil(cssH / (WORLD_H - 10)));
    vw = Math.max(2, Math.floor(cssW / s));
    vh = Math.max(2, Math.floor(cssH / s));
    dev = Math.max(1, Math.round(s * dpr));
  }
  return { s, vw, vh, dev };
}

// the view's world origin: left column and top row of the image (shared with still_scene.js)
export const viewLeft = (vw) => Math.round(320 - vw / 2);
export const VIEW_TOP_ROW = WORLD_H - VIEW_TOP;
export { CHAR_POS };

export class StageScene {
  constructor(canvas, opts = {}) {
    this.canvas = canvas;
    this.opts = opts;
    // alpha: the front canvas of the two-canvas view is transparent around her (one pass:
    // every pixel is written with alpha 1, the same picture as before)
    this.renderer = new THREE.WebGLRenderer({ canvas, antialias: false, alpha: true, powerPreference: 'low-power', preserveDrawingBuffer: Boolean(opts.preserve) });
    this.renderer.outputColorSpace = THREE.LinearSRGBColorSpace;
    this.renderer.setClearColor(0x05050c, 1);
    this.scene = new THREE.Scene();
    this.camera = new THREE.OrthographicCamera(0, 1, 1, 0, -10, 10);
    this.grade = { uDim: { value: 0 }, uDesat: { value: 0 }, uAmbient: { value: new THREE.Vector3(1, 1, 1) } };
    this.layers = [];
    this.time = 0;
    this.particles = [];
    this.sparks = [];
    this.parallax = { x: 0, y: 0 };
    // #350: the room canvas behind the board (2D copy of the room pass) and the aside input
    this.roomCanvas = opts.roomCanvas || null;
    this.roomCtx = null;
    this.aside = { e: 0, aside: 0, pan: 0 };
    this.split = false;
  }

  // e: 0..1 (board closed .. down); geo: board.boardGeometry() (art px for her and the room)
  setAside(e, geo) {
    this.aside = { e, aside: geo ? geo.aside : 0, pan: geo ? geo.pan : 0, lift: geo && geo.lift ? geo.lift : 0 };
  }

  // Two canvases while the board is shown (room behind, character in front); one otherwise.
  setSplit(on) {
    const want = Boolean(on && this.roomCanvas);
    if (want === this.split) return;
    this.split = want;
    if (this.roomCanvas) this.roomCanvas.hidden = !want;
    this.canvas.classList.toggle('front-layer', want);
  }

  // After the hair layer is in the scene: put the character group on its own render layer, so
  // the two passes split the scene. Additive lights of that group keep the target's alpha
  // (same colour as AdditiveBlending), so over a transparent pixel they stay additive.
  assignGroups() {
    const charObjs = new Set([this.char.mesh, this.steamComp, this.sparkPts]);
    this.scene.traverse((o) => {
      if (o.name === 'hair') charObjs.add(o);
    });
    for (const item of this.gadgets.items.values()) {
      if (!(item.g.depth >= 1)) continue;
      const parts = [...Object.values(item.parts || {}), item.light, ...(item.slots || []).flatMap((x) => [x.body, x.bubble, x.light]), ...(item.badge || [])];
      for (const p of parts) if (p && p.mesh) charObjs.add(p.mesh);
    }
    for (const o of charObjs) {
      o.layers.set(CHAR_LAYER);
      const m = o.material;
      if (m && m.blending === THREE.AdditiveBlending) {
        Object.assign(m, { blending: CUSTOM_BLENDING, blendEquation: ADD_EQUATION, blendSrc: SRC_ALPHA_FACTOR, blendDst: ONE_FACTOR, blendEquationAlpha: ADD_EQUATION, blendSrcAlpha: ZERO_FACTOR, blendDstAlpha: ONE_FACTOR });
        m.needsUpdate = true;
      }
    }
    this.camera.layers.enable(0);
    this.camera.layers.enable(CHAR_LAYER);
    this.charGroup = charObjs;
    return charObjs.size;
  }

  async load(base) {
    const loader = new THREE.TextureLoader();
    const [charMeta, bgMeta] = await Promise.all([
      fetch(`${base}/char/char.json`).then((r) => r.json()),
      fetch(`${base}/bg/bg.json`).then((r) => r.json()),
    ]);
    this.charMeta = charMeta;
    this.bgMeta = bgMeta;
    // background: the declarative layer list for this sky (bg_layers.js)
    this.bg = layersFor(this.opts.sky);
    if (this.bg.rejected.length) console.warn('stage: rejected background layers', this.bg.rejected);
    const names = this.bg.list.map((e) => e.file).filter(Boolean);
    const tex = {};
    await Promise.all(names.map(async (n) => {
      tex[n] = await loadTexture(loader, `${base}/bg/${n}.png`);
    }));
    for (const n of ['base', 'rig', 'rig2', 'face']) tex[`char_${n}`] = await loadTexture(loader, `${base}/char/${n}.png`);
    // gadgets (#341 stage 2): atlas + effect map + sprite rectangles
    this.gadgetMeta = await fetch(`${base}/gadgets/gadgets.json`).then((r) => r.json());
    tex.gadget_col = await loadTexture(loader, `${base}/gadgets/gadgets.png`);
    tex.gadget_fx = await loadTexture(loader, `${base}/gadgets/gadgets_fx.png`);
    this.tex = tex;
    this.build();
    // compile the props' shaders now, not on the frame a prop first appears (no hitch)
    const hidden = [];
    this.scene.traverse((o) => {
      if (o.isMesh && !o.visible) {
        o.visible = true;
        hidden.push(o);
      }
    });
    this.renderer.compile(this.scene, this.camera);
    for (const o of hidden) o.visible = false;
  }

  // Layers are grouped: 'room' (background) and 'char'; props live in the gadget layer. Each
  // group is placed from its own inputs: the room from roomFrame() (room channels + parallax),
  // the character from the parallax at its own depth, its motion only inside its shader.
  addLayer(name, material, w, h, imgX, imgY, depth, order, group = 'room') {
    const mesh = new THREE.Mesh(new THREE.PlaneGeometry(w, h), material);
    mesh.renderOrder = order;
    mesh.frustumCulled = false;
    const layer = { name, mesh, w, h, imgX, imgY, depth, group };
    this.layers.push(layer);
    this.scene.add(mesh);
    return layer;
  }

  layerMat(map) {
    return new THREE.ShaderMaterial({
      uniforms: { map: { value: map }, uSize: { value: new THREE.Vector2(WORLD_W, WORLD_H) }, uRect: { value: new THREE.Vector4(0, 0, WORLD_W, WORLD_H) }, uTwinkle: { value: -1 }, uTint: { value: new THREE.Vector4(0, 0, 0, 0) }, ...this.grade },
      vertexShader: VERT,
      fragmentShader: LAYER_FRAG,
      depthTest: false,
      depthWrite: false,
    });
  }

  glowMat(map, extra = {}) {
    return new THREE.ShaderMaterial({
      uniforms: {
        map: { value: map },
        uSize: { value: new THREE.Vector2(WORLD_W, WORLD_H) },
        uRect: { value: new THREE.Vector4(0, 0, WORLD_W, WORLD_H) },
        uLevel: { value: 0 },
        uTime: { value: 0 },
        uScan: { value: new THREE.Vector4(0, 0, 0, 0) },
        uScope: { value: new THREE.Vector4(0, 0, 0, 0) },
        uScopeOn: { value: 0 },
        ...extra,
      },
      vertexShader: VERT,
      fragmentShader: GLOW_FRAG,
      blending: THREE.AdditiveBlending,
      transparent: false, // keep renderOrder: transparent objects would be drawn after the character
      depthTest: false,
      depthWrite: false,
    });
  }

  neonMat(map) {
    return new THREE.ShaderMaterial({
      uniforms: {
        map: { value: map },
        uSize: { value: new THREE.Vector2(WORLD_W, WORLD_H) },
        uRect: { value: new THREE.Vector4(0, 0, WORLD_W, WORLD_H) },
        uColor: { value: new THREE.Vector3(...NEON_COLORS.cyan) },
        uLevel: { value: 1 },
        uFlicker: { value: 0 },
        uTime: { value: 0 },
        uCore: { value: 1 },
      },
      vertexShader: VERT,
      fragmentShader: NEON_FRAG,
      blending: THREE.AdditiveBlending,
      transparent: false, // keep renderOrder: transparent objects would be drawn after the character
      depthTest: false,
      depthWrite: false,
    });
  }

  rainMat() {
    return new THREE.ShaderMaterial({
      uniforms: { uSize: { value: new THREE.Vector2(WORLD_W, WORLD_H) }, uRect: { value: new THREE.Vector4(0, 0, WORLD_W, WORLD_H) }, uWin: { value: new THREE.Vector4(...this.bgMeta.window) }, uPhase: { value: 0 }, ...this.grade },
      vertexShader: VERT,
      fragmentShader: RAIN_FRAG,
      depthTest: false,
      depthWrite: false,
    });
  }

  build() {
    const t = this.tex;
    // one mesh per background entry, covering only the part of the room its image draws
    // (bg.json rects, measured by the builder; less fill work); roles are the handles the
    // event-driven lights use
    const roles = {};
    const make = {
      color: (e) => this.layerMat(t[e.file]),
      glow: (e) => this.glowMat(t[e.file]),
      neon: (e) => this.neonMat(t[e.file]),
      rain: () => this.rainMat(),
    };
    for (const e of this.bg.list) {
      const rect = (e.kind === 'rain' ? this.bgMeta.window : this.bgMeta.rects && this.bgMeta.rects[e.file]) || [0, 0, WORLD_W, WORLD_H];
      const mat = make[e.kind](e);
      mat.uniforms.uRect.value.set(...rect);
      const layer = this.addLayer(e.id, mat, rect[2], rect[3], rect[0], rect[1], e.depth, e.order);
      layer.band = e.band;
      if (e.role) roles[e.role] = layer;
      if (this.opts.layers && e.kind === 'color') layer.mesh.material.uniforms.uTint.value.set(...BAND_TINT[e.band], 0.55);
    }
    this.city = roles.city;
    this.rain = roles.rain || null;
    this.glowWindow = roles.window;
    this.neon = roles.neon;
    this.glowShelf = roles.shelf;
    this.glowDesk = roles.desk;
    const [, sy, , sh] = this.bgMeta.shelf;
    this.glowShelf.mesh.material.uniforms.uScan.value.set(sy, sy + sh, 0, 0);
    this.glowDesk.mesh.material.uniforms.uScope.value.set(...this.bgMeta.scope);

    const m = this.charMeta;
    const [cw, ch] = m.size;
    const faceTex = t.char_face;
    const eyeRows = m.eyeVariants.length * m.eyeBox[3];
    const mouthRows = m.mouthVariants.length * m.mouthBox[3];
    this.charMat = new THREE.ShaderMaterial({
      uniforms: {
        tBase: { value: t.char_base },
        tRig: { value: t.char_rig },
        tRig2: { value: t.char_rig2 },
        tFace: { value: faceTex },
        uSize: { value: new THREE.Vector2(cw, ch) },
        uFaceSize: { value: new THREE.Vector2(faceTex.image.width, faceTex.image.height) },
        uHead: { value: new THREE.Vector2(0, 0) },
        uBreath: { value: 0 },
        uSway: { value: 0 },
        uEarL: { value: 0 },
        uEarR: { value: 0 },
        uArm: { value: 0 },
        uEyeBox: { value: new THREE.Vector4(...m.eyeBox) },
        uMouthBox: { value: new THREE.Vector4(...m.mouthBox) },
        uBlushBox: { value: new THREE.Vector4(...m.blushBox) },
        uSweatBox: { value: new THREE.Vector4(...m.sweatBox) },
        uEyes: { value: 0 },
        uMouth: { value: 0 },
        uBlush: { value: 0 },
        uSweat: { value: 0 },
        uMouthRow: { value: eyeRows },
        uBlushRow: { value: eyeRows + mouthRows },
        uSweatRow: { value: eyeRows + mouthRows + m.blushBox[3] },
        uRimColor: { value: new THREE.Vector3(...NEON_COLORS.cyan) },
        uRim: { value: 1 },
        uLayers: { value: this.opts.layers ? 1 : 0 },
        ...this.grade,
      },
      vertexShader: VERT,
      fragmentShader: CHAR_FRAG,
      depthTest: false,
      depthWrite: false,
    });
    this.char = this.addLayer('character', this.charMat, cw, ch, CHAR_POS[0], CHAR_POS[1], 1.0, 10, 'char');
    const { list, rejected } = buildGadgets(GADGETS, Object.keys(this.gadgetMeta.sprites));
    if (rejected.length) console.warn('stage: rejected gadget entries', rejected);
    this.gadgets = new GadgetLayer(this.scene, this.grade, { col: t.gadget_col, fx: t.gadget_fx }, this.gadgetMeta, list);

    const N = 48;
    this.steamGeo = new THREE.BufferGeometry();
    this.steamGeo.setAttribute('position', new THREE.BufferAttribute(new Float32Array(N * 3), 3));
    this.steamGeo.setAttribute('aSize', new THREE.BufferAttribute(new Float32Array(N), 1));
    this.steamGeo.setAttribute('aAlpha', new THREE.BufferAttribute(new Float32Array(N), 1));
    this.steamMat = new THREE.ShaderMaterial({
      vertexShader: STEAM_VERT,
      fragmentShader: STEAM_FRAG,
      blending: THREE.AdditiveBlending,
      depthTest: false,
      depthWrite: false,
    });
    this.steam = new THREE.Points(this.steamGeo, this.steamMat);
    this.steam.frustumCulled = false;
    this.steamScene = new THREE.Scene();
    this.steamScene.add(this.steam);
    this.steamComp = new THREE.Mesh(new THREE.PlaneGeometry(1, 1), new THREE.ShaderMaterial({
      uniforms: {
        tDensity: { value: null },
        uRes: { value: new THREE.Vector2(1, 1) },
        uLight: { value: new THREE.Vector3(0.93, 0.93, 0.98) },
        uMid: { value: new THREE.Vector3(0.7, 0.7, 0.84) },
        uShade: { value: new THREE.Vector3(0.45, 0.45, 0.62) },
      },
      vertexShader: VERT,
      fragmentShader: STEAM_COMP_FRAG,
      depthTest: false,
      depthWrite: false,
    }));
    this.steamComp.renderOrder = 11;
    this.steamComp.frustumCulled = false;
    this.scene.add(this.steamComp);
    for (let i = 0; i < N; i += 1) this.particles.push({ life: 0, age: 1, x: 0, y: 0, seed: i * 0.618 });

    const S = 10;
    this.sparkGeo = new THREE.BufferGeometry();
    this.sparkGeo.setAttribute('position', new THREE.BufferAttribute(new Float32Array(S * 3), 3));
    this.sparkGeo.setAttribute('aSize', new THREE.BufferAttribute(new Float32Array(S).fill(11), 1));
    this.sparkGeo.setAttribute('aAlpha', new THREE.BufferAttribute(new Float32Array(S), 1));
    this.sparkMat = new THREE.ShaderMaterial({ vertexShader: STEAM_VERT, fragmentShader: SPARK_FRAG, depthTest: false, depthWrite: false });
    this.sparkPts = new THREE.Points(this.sparkGeo, this.sparkMat);
    this.sparkPts.renderOrder = 12;
    this.sparkPts.frustumCulled = false;
    this.scene.add(this.sparkPts);
    const spots = [[-70, 30], [74, 22], [-52, 96], [86, 104], [-88, 150], [100, 160], [-24, -4], [44, -10], [-104, 70], [112, 56]];
    this.sparkSpots = spots;

    this.blitScene = new THREE.Scene();
    this.blitCam = new THREE.OrthographicCamera(-1, 1, 1, -1, 0, 1);
    this.blitMat = new THREE.ShaderMaterial({
      uniforms: { tScene: { value: null }, uRes: { value: new THREE.Vector2(1, 1) }, uVignette: { value: 0.5 }, uKeep: { value: 0 } },
      vertexShader: 'varying vec2 vUv; void main(){ vUv = uv; gl_Position = vec4(position.xy, 0.0, 1.0); }',
      fragmentShader: BLIT_FRAG,
      depthTest: false,
      depthWrite: false,
    });
    const quad = new THREE.Mesh(new THREE.PlaneGeometry(2, 2), this.blitMat);
    quad.frustumCulled = false;
    this.blitScene.add(quad);
  }

  // Internal resolution: viewFor() above.
  resize(cssW, cssH, dpr, opts = {}) {
    const { s, vw, vh, dev } = viewFor(cssW, cssH, dpr, opts);
    this.scale = s;
    this.view = [vw, vh];
    this.renderer.setPixelRatio(1);
    this.renderer.setSize(vw * dev, vh * dev, false);
    this.canvas.style.width = `${vw * s}px`;
    this.canvas.style.height = `${vh * s}px`;
    if (this.roomCanvas) {
      // (a same-size assignment would clear the copy until the next frame)
      if (this.roomCanvas.width !== vw * dev) this.roomCanvas.width = vw * dev;
      if (this.roomCanvas.height !== vh * dev) this.roomCanvas.height = vh * dev;
      this.roomCanvas.style.width = `${vw * s}px`;
      this.roomCanvas.style.height = `${vh * s}px`;
      this.roomCtx = this.roomCanvas.getContext('2d', { alpha: false });
      if (this.roomCtx) this.roomCtx.imageSmoothingEnabled = false;
    }
    if (this.rt) this.rt.dispose();
    this.rt = new THREE.WebGLRenderTarget(vw, vh, { minFilter: THREE.NearestFilter, magFilter: THREE.NearestFilter, depthBuffer: false, generateMipmaps: false });
    if (this.steamRT) this.steamRT.dispose();
    this.steamRT = new THREE.WebGLRenderTarget(vw, vh, { minFilter: THREE.NearestFilter, magFilter: THREE.NearestFilter, depthBuffer: false, generateMipmaps: false });
    const sc = this.steamComp.material.uniforms;
    sc.tDensity.value = this.steamRT.texture;
    sc.uRes.value.set(vw, vh);
    this.blitMat.uniforms.tScene.value = this.rt.texture;
    this.blitMat.uniforms.uRes.value.set(vw, vh);
    const left = viewLeft(vw);
    this.camera.left = left;
    this.camera.right = left + vw;
    this.camera.top = VIEW_TOP;
    this.camera.bottom = VIEW_TOP - vh;
    this.camera.updateProjectionMatrix();
    this.steamComp.scale.set(vw, vh, 1);
    this.steamComp.position.set(left + vw / 2, VIEW_TOP - vh / 2, 0);
    if (this.gadgets) this.gadgets.narrow = vw < 400; // phone portrait (about 330 columns)
    return { scale: s, view: this.view };
  }

  // Image row where props stand: the text window's top edge, or the bottom of the view.
  setFloor(imageY) {
    if (this.gadgets) this.gadgets.floor = Math.round(imageY);
  }

  placeLayer(layer, [ox, oy]) {
    layer.mesh.position.set(layer.imgX + layer.w / 2 + ox, WORLD_H - (layer.imgY + layer.h / 2) - oy, 0);
    return [ox, oy];
  }

  // frame: { pose (state-derived), ch (numeric channels), en (enum channels), dt }
  update(frame) {
    const { pose, ch, en, dt } = frame;
    this.time += dt;
    const v = (k) => ch[k] || 0;
    // room: only room channels and the parallax (room.js); character channels never reach it
    const room = roomFrame(pose, ch, this.time, this.parallax, this.aside);
    this.roomState = room;
    let charOff = [0, 0];
    // background layers (bg_layers.js, they carry a band) never shake, the foreground included:
    // the CRITICAL shake is for her and the props in front of her (room.js SHAKE_MIN_DEPTH)
    // nor do they step aside with her (#350): only the room's small pan (cam) reaches them
    const still = { ...room, shake: null, aside: null };
    for (const layer of this.layers) {
      const o = this.placeLayer(layer, layerOffset(layer.band ? still : room, layer.depth));
      if (layer === this.char) charOff = o;
    }
    const neon = NEON_COLORS[room.neon.colour] || NEON_COLORS.cyan;
    this.grade.uDim.value = room.grade.dim;
    this.grade.uDesat.value = room.grade.desat;
    this.grade.uAmbient.value.set(...room.grade.ambient);
    const nu = this.neon.mesh.material.uniforms;
    nu.uColor.value.set(...neon);
    nu.uLevel.value = room.neon.level;
    nu.uFlicker.value = room.neon.flicker;
    nu.uCore.value = room.neon.core;
    this.city.mesh.material.uniforms.uTwinkle.value = room.city;
    if (this.rain) this.rain.mesh.material.uniforms.uPhase.value = room.rain;
    const gd = this.glowDesk.mesh.material.uniforms;
    gd.uLevel.value = room.desk.level;
    gd.uScopeOn.value = room.desk.scope;
    gd.uTime.value = room.desk.scopeTime;
    const gs = this.glowShelf.mesh.material.uniforms;
    gs.uLevel.value = room.shelf.level;
    gs.uScan.value.z = room.shelf.scan;
    gs.uScan.value.w = room.shelf.on;
    this.glowWindow.mesh.material.uniforms.uLevel.value = room.window;
    // character
    const cu = this.charMat.uniforms;
    const m = this.charMeta;
    const eyes = en.eyes || pose.eyes;
    const mouth = en.mouth || pose.mouth;
    cu.uEyes.value = Math.max(0, m.eyeVariants.indexOf(eyes));
    cu.uMouth.value = Math.max(0, m.mouthVariants.indexOf(mouth));
    cu.uBlush.value = pose.blush;
    cu.uSweat.value = pose.sweat;
    cu.uHead.value.set(Math.round(v('head.x') + this.parallax.x * 0.8), Math.round(pose.headY + v('head.y')));
    cu.uBreath.value = -Math.round(v('body.breath') * 0.5 + 0.5) + 0; // 0 or -1 px
    cu.uSway.value = v('hair.sway');
    cu.uEarL.value = v('ear.l') - pose.ears * 0.6;
    cu.uEarR.value = v('ear.r') - pose.ears * 0.6;
    cu.uArm.value = Math.round(v('arm.y'));
    cu.uRimColor.value.set(...neon);
    cu.uRim.value = pose.neonLevel;
    // steam from the mug: only while a turn is open (state), density from the state anim
    const mugX = CHAR_POS[0] + m.mugTop[0] + charOff[0];
    const mugY = WORLD_H - (CHAR_POS[1] + m.mugTop[1]) - charOff[1] - cu.uBreath.value - cu.uArm.value;
    const rate = room.steam * STEAM.rate;
    this.spawnAcc = (this.spawnAcc || 0) + rate * dt;
    const pos = this.steamGeo.attributes.position.array;
    const size = this.steamGeo.attributes.aSize.array;
    const alpha = this.steamGeo.attributes.aAlpha.array;
    this.particles.forEach((p, i) => {
      if (p.age >= 1 && this.spawnAcc >= 1) {
        this.spawnAcc -= 1;
        p.age = 0;
        p.life = STEAM.life[0] + (((i * 7) % 5) / 4) * STEAM.life[1];
        p.x = mugX + STEAM.wisps[i % STEAM.wisps.length];
        p.y = mugY;
        p.seed = (p.seed + 0.37) % 1;
      }
      if (p.age < 1) {
        p.age += (dt / p.life) * (this.steamFade || 1);
        const a = Math.min(p.age, 1);
        const sway = Math.sin(a * 5 + (i % STEAM.wisps.length) * 2.4 + p.seed * 0.8) * (STEAM.sway[0] + a * STEAM.sway[1]);
        pos[i * 3] = Math.round(p.x + sway - a * a * STEAM.drift);
        pos[i * 3 + 1] = Math.round(p.y + 1 + a * STEAM.rise);
        size[i] = Math.round(STEAM.size[0] + a * STEAM.size[1]);
        alpha[i] = Math.sin(Math.min(a * 5, 1) * 1.57) * Math.pow(1 - a, STEAM.alpha[1]) * STEAM.alpha[0];
      } else {
        alpha[i] = 0;
        size[i] = 0;
      }
    });
    if (this.spawnAcc > 2) this.spawnAcc = 2;
    this.steamGeo.attributes.position.needsUpdate = true;
    this.steamGeo.attributes.aSize.needsUpdate = true;
    this.steamGeo.attributes.aAlpha.needsUpdate = true;
    // sparkles
    const sp = this.sparkGeo.attributes.position.array;
    const sa = this.sparkGeo.attributes.aAlpha.array;
    const burst = room.sparkle;
    const cx = CHAR_POS[0] + 139 + charOff[0];
    const cy = WORLD_H - (CHAR_POS[1] + 90) - charOff[1];
    this.sparkSpots.forEach(([dx, dy], i) => {
      sp[i * 3] = cx + dx;
      sp[i * 3 + 1] = cy - dy + 40;
      const tw = (Math.sin(this.time * 6 + i * 1.7) + 1) / 2;
      sa[i] = burst > 0.05 ? Math.round(Math.min(1, burst * 1.3) * (0.34 + 0.66 * tw) * 3) / 3 : 0;
    });
    this.sparkGeo.attributes.position.needsUpdate = true;
    this.sparkGeo.attributes.aAlpha.needsUpdate = true;
    this.gadgets.update({ pose, ch, dt, room });
  }

  render() {
    const r = this.renderer;
    const cam = this.camera;
    r.setRenderTarget(this.steamRT);
    r.setClearColor(0x000000, 1);
    r.clear();
    r.render(this.steamScene, cam);
    if (!this.split || !this.roomCtx || !this.charGroup) {
      // one canvas: everything in one pass (the picture before #350 phase 2)
      r.setClearColor(0x05050c, 1);
      r.setRenderTarget(this.rt);
      r.clear();
      r.render(this.scene, cam);
      r.setRenderTarget(null);
      this.blitMat.uniforms.uKeep.value = 0;
      r.render(this.blitScene, this.blitCam);
      return;
    }
    // two canvases: the room pass, copied to the back canvas …
    cam.layers.set(0);
    r.setClearColor(0x05050c, 1);
    r.setRenderTarget(this.rt);
    r.clear();
    r.render(this.scene, cam);
    r.setRenderTarget(null);
    this.blitMat.uniforms.uKeep.value = 0;
    r.render(this.blitScene, this.blitCam);
    this.roomCtx.drawImage(this.canvas, 0, 0);
    // … then the character group on a transparent front canvas
    cam.layers.set(CHAR_LAYER);
    r.setClearColor(0x000000, 0);
    r.setRenderTarget(this.rt);
    r.clear();
    r.render(this.scene, cam);
    r.setRenderTarget(null);
    r.clear();
    this.blitMat.uniforms.uKeep.value = 1;
    r.render(this.blitScene, this.blitCam);
    cam.layers.enable(0);
    r.setClearColor(0x05050c, 1);
  }
}
