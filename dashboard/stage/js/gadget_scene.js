// Gadget layer of the Stage renderer (#341, stage 2). Draws the props listed in
// gadget_catalog.js from the sprite atlas built by tools/build_gadgets.py.
//
// What is shown comes only from the derived pose (model.derive -> gadget_model.deriveGadgets);
// how it moves comes from catalog channels (anim.js) plus the small built-in programs below
// (human-written, picked by name from the data). Same pixel rules as the room: integer
// positions, the room's colour grade on the props, ordered dither for light and for the
// materialise / dissolve transition, so a prop appears as pixels, never as a blur.

import * as THREE from '../vendor/three.stage.min.js';
import { gadgetCondition } from './gadget_catalog.js';
import { layerOffset } from './room.js';

// #350: which props the whiteboard hides while it is down (pure; tests/stage_board_test.cjs):
// the front-layer props standing on the text window's edge (floor), which would stand over the
// board's lower right. Drones (around her head) and the room's own props stay.
// Where a drone of `slot` lands when it reports (pure): beside her on the slot's side
// (docks: [left, right], split at centerX), so it does not cross or cover her chest and hands;
// the single `dock` when no docks are given.
export function droneDock(slot, dock, docks, centerX = 320) {
  if (!Array.isArray(docks) || docks.length !== 2) return dock;
  return slot[0] + 12 < centerX ? docks[0] : docks[1];
}

export function boardHides(g, boardDown) {
  return Boolean(boardDown && g && g.layer === 'front' && g.floor === true && g.program !== 'drones');
}

const WORLD_H = 340;

const BAYER = `
float bayer4(vec2 p){
  vec2 q = mod(floor(p), 4.0);
  int i = int(q.x + q.y * 4.0);
  float m[16];
  m[0]=0.0;m[1]=8.0;m[2]=2.0;m[3]=10.0;m[4]=12.0;m[5]=4.0;m[6]=14.0;m[7]=6.0;
  m[8]=3.0;m[9]=11.0;m[10]=1.0;m[11]=9.0;m[12]=15.0;m[13]=7.0;m[14]=13.0;m[15]=5.0;
  for (int k = 0; k < 16; k++) { if (k == i) return (m[k] + 0.5) / 16.0; }
  return 0.5;
}
float hash(vec2 p){ return fract(sin(dot(p, vec2(12.9898, 78.233))) * 43758.5453); }`;

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

// One shader for every prop; the fx map (blue = region id) says which pixels are special.
const SPRITE_FRAG = `
uniform sampler2D tCol; uniform sampler2D tFx; uniform vec2 uAtlas; uniform vec4 uRect;
uniform float uTime; uniform float uReveal; uniform float uGlitch;
uniform vec4 uP; uniform vec4 uQ; uniform vec3 uTint; uniform vec3 uRim;
varying vec2 vUv;
${BAYER}
${GRADE}
vec3 codeColour(float k){
  if (k < 0.22) return vec3(0.94, 0.61, 0.88);   // keyword
  if (k < 0.5) return vec3(0.86, 0.85, 0.93);    // identifier
  if (k < 0.66) return vec3(0.62, 0.94, 0.54);   // string
  if (k < 0.78) return vec3(0.95, 0.77, 0.42);   // number
  if (k < 0.9) return vec3(0.32, 0.9, 1.0);      // call
  return vec3(0.36, 0.4, 0.6);                   // comment
}
vec3 screen(float u, float v){
  // uP.x typed time (s), uP.y lamp (0 off, 1 run, 2 pass, 3 fail), uQ.y lamp blink
  vec3 bg = mix(vec3(0.04, 0.07, 0.14), vec3(0.03, 0.05, 0.1), v);
  if (v > 0.86) {
    vec3 bar = vec3(0.07, 0.24, 0.3);
    if (uP.y > 2.5) bar = vec3(0.85, 0.2, 0.26);
    else if (uP.y > 1.5) bar = vec3(0.28, 0.78, 0.4);
    else if (uP.y > 0.5) {
      float seg = fract(u * 3.0 - uTime * 0.8);
      bar = seg < 0.4 ? vec3(0.95, 0.68, 0.25) : vec3(0.35, 0.22, 0.1);
    }
    return v > 0.88 ? bar : bg;
  }
  float rows = 12.0;
  float li = floor(v / 0.86 * rows);
  float lv = fract(v / 0.86 * rows);
  float cols = 34.0;
  float c = floor(u * cols);
  if (c < 2.0) return (lv < 0.5 && c < 1.0) ? vec3(0.2, 0.22, 0.36) : bg; // line-number gutter
  float dur = 1.15;
  float n = floor(uP.x / dur);
  float prog = fract(uP.x / dur);
  float top = max(0.0, n - (rows - 1.0));
  float L = top + li;
  if (L > n) return bg;
  float indent = floor(hash(vec2(L, 3.1)) * 4.0) * 2.0;
  float len = 6.0 + floor(hash(vec2(L, 7.7)) * 22.0);
  float x = c - 2.0 - indent;
  float shown = L < n ? len : floor(prog * len);
  bool cursor = L == n && x == shown && fract(uTime * 2.2) < 0.6;
  if (cursor && lv < 0.9) return vec3(0.77, 0.97, 1.0);
  if (x < 0.0 || x >= shown || lv >= 0.5) return bg;
  if (hash(vec2(L * 13.0 + x, 1.3)) < 0.16) return bg; // spaces between tokens
  float tok = floor((x + floor(hash(vec2(L, 9.2)) * 3.0)) / 4.0);
  vec3 col = codeColour(hash(vec2(L * 7.0 + tok, 4.4)));
  if (x < 3.0 && hash(vec2(L, 5.0)) < 0.5) col = codeColour(0.1);
  if (hash(vec2(L, 2.0)) > 0.86) col = codeColour(0.95);
  return col;
}
void main(){
  vec2 ip = vec2(floor(vUv.x * uRect.z), floor((1.0 - vUv.y) * uRect.w));
  if (uGlitch > 0.01) {
    float h = hash(vec2(ip.y, floor(uTime * 30.0)));
    if (h < 0.4 * uGlitch) ip.x += h < 0.2 * uGlitch ? 2.0 : -2.0;
  }
  if (ip.x < 0.0 || ip.x >= uRect.z) discard;
  vec2 ap = uRect.xy + ip;
  vec4 c = texture2D(tCol, (ap + 0.5) / uAtlas);
  if (c.a < 0.5) discard;
  if (bayer4(ap) >= uReveal) discard;
  vec4 fx = texture2D(tFx, (ap + 0.5) / uAtlas);
  float region = floor(fx.b * 255.0 + 0.5);
  vec3 col = grade(c.rgb);
  if (region == 1.0) {
    col = uQ.w > 0.5 ? screen(fx.r, fx.g) : vec3(0.03, 0.04, 0.08);
  } else if (region == 2.0) {
    float key = floor(fx.r * 255.0 / 16.0 + 0.5) + 13.0 * floor(fx.g * 255.0 / 32.0 + 0.5);
    float tick = floor(uTime * 15.0);
    if (uQ.x > 0.5 && hash(vec2(key, tick)) < 0.11) col = vec3(0.45, 0.85, 0.95);
    else if (uQ.w > 0.5) col += vec3(0.02, 0.05, 0.07);
  } else if (region >= 3.0 && region <= 5.0) {
    // signal tower: 3 red (fail) / 4 amber (run, blinking) / 5 green (pass)
    bool lit = (region == 3.0 && uP.y > 2.5) || (region == 4.0 && uP.y > 0.5 && uP.y < 1.5 && uQ.y > 0.5)
      || (region == 5.0 && uP.y > 1.5 && uP.y < 2.5);
    vec3 lamp = region == 3.0 ? vec3(1.0, 0.25, 0.3) : region == 4.0 ? vec3(1.0, 0.7, 0.25) : vec3(0.45, 1.0, 0.5);
    float hi = 1.0 - fx.r;
    if (lit) col = mix(lamp, vec3(1.0), hi * hi * 0.55 + (fx.g > 0.3 && fx.g < 0.5 ? 0.2 : 0.0));
  } else if (region == 6.0) {
    // drone visor: eye pixels take the status colour, the visor a dim tint of it
    float on = uQ.z;
    col = fx.r > 0.5 ? mix(vec3(0.05), uTint, on) : mix(vec3(0.03, 0.03, 0.08), uTint * 0.22, on);
  } else if (region == 7.0) {
    // beacon lens: uP.x spin phase (critical), uP.y glow level
    float d = abs(fract(fx.r - uP.x + 0.5) - 0.5);
    float band = uP.z > 0.5 ? smoothstep(0.32, 0.0, d) : 0.0;
    float lvl = clamp(uP.y * 0.65 + band * 0.9, 0.0, 1.3);
    vec3 lit = uTint * lvl + vec3(1.0) * band * band * 0.35;
    col = bayer4(ap) < lvl ? lit : mix(col, uTint * 0.35, uP.y);
  } else if (region == 8.0) {
    // glass: half the pixels let the room through (dithered transparency), glint stays solid
    float glint = abs(fx.r - fx.g) < 0.07 && fx.r > 0.2 && fx.r < 0.5 ? 1.0 : 0.0;
    if (glint < 0.5 && bayer4(ap + floor(uTime * 2.0)) < 0.45) discard;
    col = glint > 0.5 ? vec3(0.85, 0.97, 1.0) : mix(col, uTint, 0.4 + 0.12 * sin(uTime * 3.0));
  } else if (region == 9.0 || region == 10.0) {
    float t = uP.x; // 0 full top .. 1 full bottom
    float v = fx.r;
    bool sand = region == 9.0 ? v > t * 0.98 + 0.02 : v > 1.0 - t;
    if (region == 10.0 && fx.g > 0.0) sand = false;
    if (fx.r > 0.49 && fx.r < 0.51 && region == 10.0) sand = mod(floor(uTime * 8.0 + ip.y), 2.0) < 1.0;
    if (sand) col = grade(mix(vec3(0.79, 0.54, 0.23), vec3(0.95, 0.77, 0.42), hash(ap) * 0.6));
  } else if (region == 11.0) {
    col = mix(col, uTint, 0.75 * uQ.z * (0.7 + 0.3 * step(0.5, fract(uTime * 9.0))));
  } else if (region == 12.0) {
    if (uQ.w > 0.5) col = uTint;
  }
  if (uGlitch > 0.01) col = mix(col, vec3(1.0, 0.24, 0.32), 0.35 * uGlitch) + vec3(0.0, 0.05, 0.08) * uGlitch;
  gl_FragColor = vec4(col, 1.0);
}`;

// Additive pool of light, quantised by ordered dither like the room's glow layers.
// uMode 1: two rotating beams (beacon at CRITICAL).
const LIGHT_FRAG = `
uniform vec3 uColor; uniform float uLevel; uniform vec2 uSize; uniform float uMode; uniform float uAngle;
varying vec2 vUv;
${BAYER}
void main(){
  vec2 ip = floor(vec2(vUv.x, 1.0 - vUv.y) * uSize);
  vec2 d = (ip + 0.5) / uSize * 2.0 - 1.0;
  float r = length(d);
  float f = clamp(1.0 - r, 0.0, 1.0);
  f = f * f;
  if (uMode > 0.5) {
    // two opposite beams sweeping round (the wall is seen flat, so the sweep is squashed)
    float a = atan(d.y * 1.8, d.x);
    float da = abs(mod(a - uAngle + 1.5708, 3.14159) - 1.5708);
    float beam = smoothstep(0.42, 0.06, da) * pow(clamp(1.0 - r, 0.0, 1.0), 0.6);
    f = beam * 1.1 + f * f * 0.9;
  }
  float lvl = f * uLevel;
  float b = bayer4(gl_FragCoord.xy);
  vec3 col = vec3(0.0);
  if (b < lvl * lvl * 1.4) col = uColor * 0.36;
  else if (b < lvl * 0.9) col = uColor * 0.13;
  if (dot(col, col) < 1e-5) discard;
  gl_FragColor = vec4(col, 1.0);
}`;

export const LIGHT_RGB = {
  cyan: [0.32, 0.9, 1.0],
  amber: [1.0, 0.68, 0.22],
  green: [0.4, 1.0, 0.5],
  red: [1.0, 0.22, 0.28],
  violet: [0.7, 0.5, 1.0],
  paper: [0.9, 0.9, 1.0],
};
const ORDER = { back: 5.5, shelf: 7.5, desk: 9.6, front: 10.3 };
const STATUS_TINT = { run: LIGHT_RGB.cyan, done: LIGHT_RGB.green, err: LIGHT_RGB.red };
const REVEAL_IN = 0.35;
const REVEAL_OUT = 0.3;
const smooth = (x) => x * x * (3 - 2 * x);
const clamp01 = (x) => Math.min(1, Math.max(0, x));
const isOn = (v) => Boolean(v) && v !== 'off' && v !== 'none';

export class GadgetLayer {
  constructor(scene, grade, tex, meta, gadgets) {
    this.scene = scene;
    this.grade = grade;
    this.tex = tex;
    this.meta = meta;
    this.gadgets = gadgets;
    this.floor = 223;
    this.narrow = false; // phone-narrow view: entries use their `narrow` placement
    this.time = 0;
    this.items = new Map(); // gadget id -> runtime record
    this.drones = []; // per slot: runtime record
    this.typeT = 9.5; // typed time of the laptop screen (starts with a few lines on it)
    for (const g of gadgets) this.items.set(g.id, this.makeItem(g));
  }

  spriteMat(name) {
    const r = this.meta.sprites[name];
    return new THREE.ShaderMaterial({
      uniforms: {
        tCol: { value: this.tex.col },
        tFx: { value: this.tex.fx },
        uAtlas: { value: new THREE.Vector2(this.meta.size[0], this.meta.size[1]) },
        uRect: { value: new THREE.Vector4(...r) },
        uTime: { value: 0 },
        uReveal: { value: 0 },
        uGlitch: { value: 0 },
        uP: { value: new THREE.Vector4(0, 0, 0, 0) },
        uQ: { value: new THREE.Vector4(0, 0, 0, 0) },
        uTint: { value: new THREE.Vector3(...LIGHT_RGB.cyan) },
        uRim: { value: new THREE.Vector3(...LIGHT_RGB.cyan) },
        ...this.grade,
      },
      vertexShader: VERT,
      fragmentShader: SPRITE_FRAG,
      depthTest: false,
      depthWrite: false,
    });
  }

  sprite(name, order, size = null) {
    const [, , w, h] = size ? [0, 0, ...size] : this.meta.sprites[name];
    const mesh = new THREE.Mesh(new THREE.PlaneGeometry(w, h), this.spriteMat(name));
    mesh.renderOrder = order;
    mesh.frustumCulled = false;
    mesh.visible = false;
    this.scene.add(mesh);
    return { mesh, name, w, h, u: mesh.material.uniforms };
  }

  setSprite(part, name) {
    if (part.name === name) return;
    const r = this.meta.sprites[name];
    if (r[2] !== part.w || r[3] !== part.h) return; // frames of one prop share a size
    part.u.uRect.value.set(...r);
    part.name = name;
  }

  light(order, r) {
    const size = Math.round(r * 2);
    const mesh = new THREE.Mesh(new THREE.PlaneGeometry(size, size), new THREE.ShaderMaterial({
      uniforms: {
        uColor: { value: new THREE.Vector3(1, 1, 1) },
        uLevel: { value: 0 },
        uSize: { value: new THREE.Vector2(size, size) },
        uMode: { value: 0 },
        uAngle: { value: 0 },
      },
      vertexShader: VERT,
      fragmentShader: LIGHT_FRAG,
      blending: THREE.AdditiveBlending,
      transparent: false,
      depthTest: false,
      depthWrite: false,
    }));
    mesh.renderOrder = order;
    mesh.frustumCulled = false;
    mesh.visible = false;
    this.scene.add(mesh);
    return { mesh, size, u: mesh.material.uniforms };
  }

  makeItem(g) {
    const order = ORDER[g.layer];
    const item = { g, on: false, reveal: 0, parts: {}, light: null };
    if (g.program === 'drones') {
      item.slots = g.slots.map((_, i) => ({
        body: this.sprite('drone.0', 11.5 + i * 0.01),
        bubble: this.sprite('bubble.ok', 11.6 + i * 0.01),
        light: this.light(11.4, g.light ? g.light.r : 8),
        status: null, phase: 'gone', t0: 0, from: [0, 0], to: [0, 0], pos: [0, 0], reveal: 0,
      }));
      item.badge = [this.sprite('digits', 11.7, [5, 7]), this.sprite('digits', 11.71, [5, 7])];
      return item;
    }
    item.parts.main = this.sprite(g.sprite, order + 0.01);
    if (g.program === 'stamp') {
      item.parts.seal = this.sprite('seal.approve', order + 0.02);
      item.parts.stamper = this.sprite('stamper', order + 0.03);
    }
    if (g.light) item.light = this.light(order, g.light.r);
    return item;
  }

  // Place a sprite with its top-left at image (x, y) and the parallax of `depth`.
  place(part, x, y, depth, view) {
    const [ox, oy] = layerOffset(view, depth);
    part.mesh.position.set(Math.round(x) + part.w / 2 + ox, WORLD_H - (Math.round(y) + part.h / 2) - oy, 0);
  }

  placeLight(l, cx, cy, depth, view) {
    const [ox, oy] = layerOffset(view, depth);
    l.mesh.position.set(Math.round(cx) + ox, WORLD_H - Math.round(cy) - oy, 0);
  }

  // Placement of an entry for the current view (wide, or its `narrow` override).
  spot(g) {
    const n = this.narrow && g.narrow ? g.narrow : null;
    return {
      hide: Boolean(n && n.hide),
      at: (n && n.at) || g.at,
      slots: (n && n.slots) || g.slots,
      dock: (n && n.dock) || g.dock,
      docks: (n && n.docks) || g.docks || null,
    };
  }

  origin(g) {
    const [, , , h] = this.meta.sprites[g.sprite];
    const { at } = this.spot(g);
    return [at[0], g.floor ? this.floor + at[1] - h : at[1]];
  }

  // frame: { pose, ch (numeric channels), dt, room (room.js frame: camera + parallax) }.
  // Props have their own depth and animation; the character's motion does not move them.
  update(frame) {
    const { pose, ch, dt, room: view } = frame;
    this.time += dt;
    const v = (k) => ch[k] || 0;
    if (v('laptop.type') > 0.5) this.typeT += dt;
    for (const item of this.items.values()) {
      const g = item.g;
      if (g.program === 'drones') {
        this.updateDrones(item, pose, v, dt, view);
        continue;
      }
      const shown = isOn(pose[g.show]) && !this.spot(g).hide && gadgetCondition(g, pose);
      // #350: while the whiteboard is down, the props standing on the text window in front of
      // her (laptop, signal lamp) would cover the board: they wait (still caused by the event,
      // back when the board rolls up; the debug panel lists them as hidden for the board)
      const byBoard = shown && boardHides(g, this.boardDown);
      item.boardHidden = byBoard;
      const want = shown && !byBoard;
      item.reveal = clamp01(item.reveal + (want ? dt / REVEAL_IN : -dt / REVEAL_OUT));
      const visible = item.reveal > 0;
      for (const p of Object.values(item.parts)) p.mesh.visible = visible;
      if (item.light) item.light.mesh.visible = visible;
      if (!visible) continue;
      const [x, y] = this.origin(g);
      const main = item.parts.main;
      const u = main.u;
      u.uTime.value = this.time;
      u.uReveal.value = smooth(item.reveal) * 1.001;
      u.uGlitch.value = pose['gadget.glitch'] === g.id ? v('gadget.glitch') : 0;
      let dx = 0;
      let dy = 0;
      let light = item.light ? item.light.u : null;
      let lightLevel = g.light ? g.light.level : 0;
      let lightRGB = g.light ? LIGHT_RGB[g.light.color] || LIGHT_RGB.cyan : null;
      const lamp = { off: 0, run: 1, pass: 2, fail: 3 }[pose['gadget.lamp']] || 0;
      switch (g.program) {
        case 'laptop':
          u.uP.value.set(this.typeT, lamp, 0, 0);
          u.uQ.value.set(v('laptop.type') > 0.5 ? 1 : 0, v('lamp.blink'), 0, 1);
          u.uTint.value.set(...(lamp === 3 ? LIGHT_RGB.red : LIGHT_RGB.green));
          lightRGB = lamp === 3 ? [0.9, 0.35, 0.45] : lamp === 2 ? [0.45, 0.95, 0.6] : [0.4, 0.75, 1.0];
          lightLevel *= 0.85 + 0.15 * v('laptop.type');
          break;
        case 'stacklight':
          u.uP.value.set(0, lamp, 0, 0);
          u.uQ.value.set(0, v('lamp.blink'), 0, 0);
          lightRGB = lamp === 3 ? LIGHT_RGB.red : lamp === 2 ? LIGHT_RGB.green : LIGHT_RGB.amber;
          lightLevel = lamp === 0 || (lamp === 1 && v('lamp.blink') < 0.5) ? 0 : lightLevel;
          break;
        case 'beacon': {
          const crit = pose['gadget.beacon'] === 'critical';
          const tint = crit ? LIGHT_RGB.red : LIGHT_RGB.violet;
          const level = crit ? 0.6 : 0.45 + 0.5 * v('beacon.pulse');
          u.uTint.value.set(...tint);
          u.uP.value.set(v('beacon.spin'), level, crit ? 1 : 0, 0);
          lightRGB = tint;
          lightLevel *= crit ? 1 : level;
          if (light) {
            light.uMode.value = crit ? 1 : 0;
            light.uAngle.value = v('beacon.spin') * Math.PI * 2;
          }
          break;
        }
        case 'magnifier':
          dx = Math.round(v('magnifier.x'));
          dy = Math.round(v('magnifier.y'));
          u.uTint.value.set(...LIGHT_RGB.cyan);
          break;
        case 'folder':
          dx = Math.round(v('folder.out'));
          break;
        case 'note':
          dy = v('note.press') > 0.5 ? 0 : -1;
          break;
        case 'box':
          dx = Math.round(v('box.slide'));
          break;
        case 'bell':
          dx = Math.round(v('bell.ring'));
          break;
        case 'hourglass':
          u.uP.value.set(v('sand.t'), 0, 0, 0);
          break;
        case 'stamp': {
          const kind = pose['gadget.stamp'] === 'deny' ? 'deny' : 'approve';
          const seal = item.parts.seal;
          const st = item.parts.stamper;
          this.setSprite(seal, `seal.${kind}`);
          seal.mesh.visible = v('stamp.seal') > 0.5;
          seal.u.uReveal.value = 1.001;
          seal.u.uTime.value = this.time;
          this.place(seal, x + 4, y + 11, g.depth, view);
          const sy = v('stamp.y');
          st.mesh.visible = sy > -13.5;
          st.u.uReveal.value = 1.001;
          this.place(st, x + 7, y + 12 - 14 + Math.round(sy), g.depth, view);
          break;
        }
        default:
          break;
      }
      this.place(main, x + dx, y + dy, g.depth, view);
      if (item.light && g.light) {
        light.uColor.value.set(...lightRGB);
        light.uLevel.value = lightLevel * smooth(item.reveal);
        item.light.mesh.visible = light.uLevel.value > 0.01;
        this.placeLight(item.light, x + dx + g.light.at[0], y + dy + g.light.at[1], g.depth, view);
      }
    }
  }

  // Drones: one per subagent slot. run -> fly out to the slot and hover; done -> fly back to
  // the dock, show a check, dissolve; err -> stay, sink, blink red with a "!" (never vanish
  // silently). More agents than slots -> "+N" next to the last slot.
  updateDrones(item, pose, v, dt, view) {
    const g = item.g;
    const { slots, dock: dock0, docks } = this.spot(g);
    const list = isOn(pose['gadget.drones']) ? pose['gadget.drones'].split(',') : [];
    const nSlots = item.slots.length;
    const overflow = list.length > nSlots ? list.length - (nSlots - 1) : 0;
    const shown = overflow ? list.slice(0, nSlots - 1) : list;
    const t = this.time;
    item.slots.forEach((d, i) => {
      const status = shown[i] || null;
      const slot = slots[i];
      // a drone comes back beside her on its own side (docks [left, right]), not across her
      const dock = droneDock(slot, dock0, docks, g.centerX);
      if (status !== d.status) {
        const prev = d.status;
        d.status = status;
        d.t0 = t;
        if (status === 'run') {
          // launched from beside her on its side too (docks), not from her chest across her face
          d.from = prev === null || d.phase === 'gone' ? (docks ? [...dock] : [g.at[0], g.at[1]]) : [...d.pos];
          d.phase = 'out';
          d.to = slot;
          if (prev === null) d.reveal = 0;
        } else if (status === 'done') {
          d.phase = 'back';
          d.from = prev === null ? [...dock] : [...d.pos];
          d.to = dock;
          if (prev === null) d.reveal = 0;
        } else if (status === 'err') {
          d.phase = 'fail';
          d.from = prev === null ? [...slot] : [...d.pos];
          d.to = [slot[0], slot[1] + 3];
          if (prev === null) d.reveal = 0;
        } else {
          d.phase = 'leave';
        }
      }
      const age = t - d.t0;
      let pos = d.pos;
      let showBubble = null;
      let fadeIn = true;
      if (d.phase === 'out' && d.status === 'run') d.to = slot;
      if (d.phase === 'back') d.to = dock;
      if (d.phase === 'out') {
        const k = smooth(clamp01(age / 0.8));
        pos = [d.from[0] + (d.to[0] - d.from[0]) * k, d.from[1] + (d.to[1] - d.from[1]) * k - Math.sin(k * Math.PI) * 10];
        if (k >= 1) pos = [d.to[0], d.to[1] + Math.round(v('drone.bob') + (i % 2 ? 0.4 : -0.4))];
      } else if (d.phase === 'back') {
        const k = smooth(clamp01(age / 0.9));
        pos = [d.from[0] + (d.to[0] - d.from[0]) * k, d.from[1] + (d.to[1] - d.from[1]) * k - Math.sin(k * Math.PI) * 8];
        if (age > 0.9) showBubble = 'bubble.ok';
        if (age > 2.6) fadeIn = false;
      } else if (d.phase === 'fail') {
        const k = smooth(clamp01(age / 0.4));
        pos = [d.from[0] + (d.to[0] - d.from[0]) * k + (age < 0.5 ? (Math.floor(age * 30) % 2) * 2 - 1 : 0), d.from[1] + (d.to[1] - d.from[1]) * k];
        showBubble = 'bubble.err';
      } else {
        fadeIn = false;
      }
      d.pos = pos;
      d.reveal = clamp01(d.reveal + (fadeIn ? dt / REVEAL_IN : -dt / REVEAL_OUT));
      if (d.reveal <= 0 && !fadeIn) d.phase = d.phase === 'out' || d.phase === 'fail' ? d.phase : 'gone';
      const vis = d.reveal > 0;
      d.body.mesh.visible = vis;
      d.light.mesh.visible = vis;
      d.bubble.mesh.visible = vis && Boolean(showBubble);
      if (!vis) return;
      const st = d.status || 'done';
      const tint = STATUS_TINT[st];
      const rotorFps = st === 'err' ? 3 : 14;
      this.setSprite(d.body, Math.floor(t * rotorFps + i) % 2 ? 'drone.1' : 'drone.0');
      const bu = d.body.u;
      bu.uTime.value = t;
      bu.uReveal.value = smooth(d.reveal) * 1.001;
      bu.uTint.value.set(...tint);
      const blink = st === 'err' ? (Math.floor(t * 2.5) % 2 ? 1 : 0.25) : 1;
      bu.uQ.value.set(0, 0, blink, 0);
      this.place(d.body, pos[0], pos[1], g.depth, view);
      if (showBubble) {
        this.setSprite(d.bubble, showBubble);
        d.bubble.u.uReveal.value = 1.001;
        this.place(d.bubble, pos[0] + 12, pos[1] - 9, g.depth, view);
      }
      const lu = d.light.u;
      lu.uColor.value.set(...tint);
      lu.uLevel.value = (g.light ? g.light.level : 0.6) * smooth(d.reveal) * (st === 'err' ? blink : 1);
      this.placeLight(d.light, pos[0] + (g.light ? g.light.at[0] : 9), pos[1] + (g.light ? g.light.at[1] : 16), g.depth, view);
    });
    // "+N" badge (two digits at most; 99+ shows as +9 9)
    const [b1, b2] = item.badge;
    const n = overflow;
    b1.mesh.visible = n > 0;
    b2.mesh.visible = n > 9;
    if (n > 0) {
      const last = slots[nSlots - 1];
      const cell = (k) => [k * 5, 0, 5, 7];
      const r = this.meta.sprites.digits;
      const setCell = (part, k) => part.u.uRect.value.set(r[0] + cell(k)[0], r[1], 5, 7);
      // "+N" (N < 10) or "NN"
      setCell(b1, n > 9 ? Math.floor(Math.min(n, 99) / 10) : 10);
      setCell(b2, Math.min(n, 99) % 10);
      b2.mesh.visible = true;
      for (const b of [b1, b2]) {
        b.u.uReveal.value = 1.001;
        b.u.uTime.value = t;
      }
      this.place(b1, last[0] + 4, last[1] + 4, g.depth, view);
      this.place(b2, last[0] + 9, last[1] + 4, g.depth, view);
    }
  }

  // Ids of the props the board keeps hidden now (debug panel: "hidden for the board").
  boardHiddenIds() {
    return [...this.items.values()].filter((item) => item.boardHidden).map((item) => item.g.id);
  }

  // Ids of the props on screen (debug panel).
  visibleIds() {
    const out = [];
    for (const item of this.items.values()) {
      if (item.g.program === 'drones') {
        const n = item.slots.filter((d) => d.body.mesh.visible).length;
        if (n) out.push(`drones×${n}`);
      } else if (item.reveal > 0) out.push(item.g.id);
    }
    return out;
  }
}
