// Hair layer (#341): draws the hair parts of the character (tools/build_hair.py) at the
// offsets of the hair rig (hair_rig.js), inside the character layer of scene.js.
//
// Connection to the character renderer (data, read-only): the segment array of hair.json
// (box + atlas position + parent + draw order) and, per frame, one whole-pixel offset per
// segment from hairOffsets(). This layer reads the character layer's applied uniforms (head,
// breath, ears, arm, sweat, rim, grade: the same uniform objects, shared) so that the hair
// hangs exactly where the head was drawn, and it writes only its own mesh, which it places at
// the character layer's position. It never touches the room layers, the camera or props.
//
// Depth without a depth buffer: front parts are drawn over the body except where the arm and
// mug are (unless the segment is overArm); back parts only where no body pixel is
// (hair_mask.png). The order contract is hair.json `layerOrder` / part `slot`. The body pixel under a
// screen pixel is found with the same offset formula as scene.js CHAR_FRAG (keep in sync).

import * as THREE from '../vendor/three.stage.min.js';
import { createHairRig, hairOffsets, hairStyle, initHairState, stepHair } from './hair_rig.js';

export const HAIR_ORDER = 10.1; // right after the character (10): before front props (10.3), steam (11)
const SHARED = ['tBase', 'tRig', 'tRig2', 'tFace', 'uSize', 'uFaceSize', 'uHead', 'uBreath', 'uSway', 'uEarL', 'uEarR', 'uArm', 'uSweatBox', 'uSweat', 'uSweatRow', 'uRimColor', 'uRim', 'uLayers'];

const VERT = `
varying vec2 vUv;
void main(){ vUv = uv; gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0); }`;

const frag = (n) => `
#define NSEG ${n}
uniform sampler2D tHair; uniform sampler2D tHairFx; uniform sampler2D tMask; uniform vec2 uHairSize;
uniform sampler2D tBase; uniform sampler2D tRig; uniform sampler2D tRig2; uniform sampler2D tFace;
uniform vec2 uSize; uniform vec2 uFaceSize;
uniform vec2 uHead; uniform float uBreath; uniform float uSway; uniform float uEarL; uniform float uEarR; uniform float uArm;
uniform vec4 uSweatBox; uniform float uSweat; uniform float uSweatRow;
uniform vec3 uRimColor; uniform float uRim; uniform float uLayers; uniform float uDirect;
uniform float uDim; uniform float uDesat; uniform vec3 uAmbient;
uniform vec4 uBox[NSEG]; uniform vec2 uAt[NSEG]; uniform vec2 uOff[NSEG]; uniform float uBack[NSEG]; uniform float uOverArm[NSEG]; uniform vec3 uTint[NSEG];
varying vec2 vUv;
vec3 grade(vec3 c){
  float l = dot(c, vec3(0.299, 0.587, 0.114));
  c = mix(c, vec3(l) * vec3(0.92, 0.95, 1.08), uDesat);
  return c * uAmbient * (1.0 - uDim);
}
vec4 tx(sampler2D t, vec2 ip, vec2 sz){ return texture2D(t, (ip + 0.5) / sz); }
bool inBox(vec2 p, vec4 b){ return p.x >= b.x && p.y >= b.y && p.x < b.x + b.z && p.y < b.y + b.w; }
void main(){
  vec2 ip = vec2(floor(vUv.x * uSize.x), floor((1.0 - vUv.y) * uSize.y));
  // the body pixel the character layer shows here (same formula as CHAR_FRAG)
  vec4 r1 = tx(tRig, ip, uSize);
  vec4 r2 = tx(tRig2, ip, uSize);
  vec2 off = uHead * r1.r + vec2(0.0, uBreath) * r1.b + vec2(uSway * r1.g, 0.0)
    + vec2(0.6 * uEarL, 0.8 * uEarL) * r2.r + vec2(-0.6 * uEarR, 0.8 * uEarR) * r2.g
    + vec2(0.0, uArm) * r2.b;
  // uDirect: the character layer composites her itself and gives the mask of the frame
  vec2 sp = uDirect > 0.5 ? ip : ip - floor(off + 0.5);
  bool inside = sp.x >= 0.0 && sp.y >= 0.0 && sp.x < uSize.x && sp.y < uSize.y;
  vec4 m = inside ? tx(tMask, sp, uSize) : vec4(0.0);
  bool arm = m.g > 0.5; // arm and mug: in front of the hair unless a segment is overArm
  if (uSweat > 0.5 && inBox(sp, uSweatBox) && tx(tFace, sp - uSweatBox.xy + vec2(0.0, uSweatRow), uFaceSize).a > 0.5) discard;
  bool body = m.r > 0.5;
  vec4 c = vec4(0.0);
  float rim = 0.0;
  vec3 tint = vec3(0.0);
  for (int i = 0; i < NSEG; i++) {
    if (uBack[i] > 0.5 && body) continue;
    if (arm && uOverArm[i] < 0.5) continue;
    vec2 p = ip - uOff[i];
    if (!inBox(p, uBox[i])) continue;
    vec2 q = p - uBox[i].xy + uAt[i];
    vec4 h = tx(tHair, q, uHairSize);
    if (h.a > 0.5) { c = h; rim = tx(tHairFx, q, uHairSize).r; tint = uTint[i]; break; }
  }
  if (c.a < 0.5) discard;
  c.rgb = grade(c.rgb);
  if (rim > 0.5) c.rgb = mix(c.rgb, uRimColor, 0.55 * uRim);
  if (uLayers > 0.5) c.rgb = mix(c.rgb, tint, 0.6);
  gl_FragColor = vec4(c.rgb, 1.0);
}`;

function loadTexture(loader, url) {
  return new Promise((resolve, reject) => {
    loader.load(url, (t) => {
      t.magFilter = THREE.NearestFilter;
      t.minFilter = THREE.NearestFilter;
      t.generateMipmaps = false;
      t.flipY = false;
      t.colorSpace = THREE.NoColorSpace;
      resolve(t);
    }, undefined, () => reject(new Error(`texture ${url}`)));
  });
}

const hexRgb = (h) => [1, 3, 5].map((i) => parseInt(h.slice(i, i + 2), 16) / 255);

// Hair part sets per view: view id -> file stem in assets/char/ (<stem>.json, <stem>.png,
// <stem>_fx.png, <stem>_mask.png, all made by tools/build_hair.py). A view drawn from another
// sheet (e.g. the character seen from behind, where the back hair leads) adds one entry here
// and its part ids to HAIR_RIG; nothing else changes. setView() swaps the set at run time.
export const HAIR_VIEWS = { front: 'hair', back: 'hair_back' };

async function loadSet(base, stem) {
  const meta = await fetch(`${base}/char/${stem}.json`).then((r) => r.json());
  const loader = new THREE.TextureLoader();
  const [hair, fx, mask] = await Promise.all(['', '_fx', '_mask'].map((n) => loadTexture(loader, `${base}/char/${stem}${n}.png`)));
  return { meta, tex: { hair, fx, mask } };
}

export class HairLayer {
  // stage: a StageScene after load(). Resolves to a HairLayer whose mesh is in stage.scene.
  static async load(stage, base, opts = {}) {
    const view = opts.view && HAIR_VIEWS[opts.view] ? opts.view : 'front';
    const layer = new HairLayer(stage, await loadSet(base, HAIR_VIEWS[view]), { ...opts, view });
    layer.base = base;
    return layer;
  }

  constructor(stage, set, opts = {}) {
    this.stage = stage;
    this.opts = opts;
    this.sets = {};
    this.frozen = Boolean(opts.frozen);
    this.style = hairStyle({});
    this.mesh = null;
    this.ext = {};
    this.use(set, opts.view || 'front');
  }

  // Load another view's part set ahead of time (so the first switch shows no frame without it).
  async preload(view) {
    if (!Object.prototype.hasOwnProperty.call(HAIR_VIEWS, view) || this.sets[view]) return Boolean(this.sets[view]);
    this.sets[view] = await loadSet(this.base, HAIR_VIEWS[view]);
    return true;
  }

  // Switch to another view's part set (loaded once, then cached). Unknown views are refused.
  async setView(view) {
    if (!Object.prototype.hasOwnProperty.call(HAIR_VIEWS, view)) return false;
    if (view === this.view) return true;
    const set = this.sets[view] || (await loadSet(this.base, HAIR_VIEWS[view]));
    this.use(set, view);
    return true;
  }

  use(set, view) {
    const { stage } = this;
    const { meta, tex } = set;
    this.sets[view] = set;
    this.view = view;
    this.meta = meta;
    this.rig = createHairRig(meta, this.opts.tuning, this.opts.styles);
    if (this.rig.rejected.length) console.warn('stage: rejected hair segments', this.rig.rejected);
    const cu = stage.charMat.uniforms;
    this.state = initHairState(this.rig, { head: [cu.uHead.value.x, cu.uHead.value.y], breath: cu.uBreath.value, style: this.style.style });
    this.drawn = meta.segments.filter((s) => this.rig.index[s.id] !== undefined); // draw order
    // pivot of each segment's chain root (where it hangs from the head)
    const byId = Object.fromEntries(meta.segments.map((s) => [s.id, s]));
    this.rootPivot = this.drawn.map((s) => {
      let r = s;
      while (r.parent !== 'head' && byId[r.parent]) r = byId[r.parent];
      return r.pivot || [139, 60];
    });
    const segs = this.drawn;
    const tints = Object.fromEntries(meta.parts.map((p) => [p.id, hexRgb(p.tint)]));
    const shared = {};
    for (const k of SHARED) shared[k] = cu[k];
    this.offsets = segs.map(() => new THREE.Vector2(0, 0));
    const material = new THREE.ShaderMaterial({
      uniforms: {
        ...shared,
        ...stage.grade,
        tHair: { value: tex.hair },
        tHairFx: { value: tex.fx },
        tMask: { value: this.ext.mask || tex.mask },
        uDirect: { value: this.ext.mask ? 1 : 0 },
        uHairSize: { value: new THREE.Vector2(...meta.atlas) },
        uBox: { value: segs.map((s) => new THREE.Vector4(...s.box)) },
        uAt: { value: segs.map((s) => new THREE.Vector2(...s.at)) },
        uOff: { value: this.offsets },
        uBack: { value: segs.map((s) => (s.z === 'back' ? 1 : 0)) },
        uOverArm: { value: segs.map((s) => (this.rig.segments[this.rig.index[s.id]].overArm ? 1 : 0)) },
        uTint: { value: segs.map((s) => new THREE.Vector3(...tints[s.part].map((v) => v * (s.depth % 2 ? 0.6 : 1)))) },
      },
      vertexShader: VERT,
      fragmentShader: frag(segs.length),
      depthTest: false,
      depthWrite: false,
    });
    if (this.mesh) {
      this.mesh.material.dispose();
      this.mesh.material = material;
    } else {
      const [w, h] = meta.size;
      this.mesh = new THREE.Mesh(new THREE.PlaneGeometry(w, h), material);
      this.mesh.renderOrder = HAIR_ORDER;
      this.mesh.frustumCulled = false;
      this.mesh.name = 'hair';
      stage.scene.add(this.mesh);
    }
    this.material = material;
  }

  // The character layer (char_bridge.js) gives the mask of the composited frame: where she is
  // drawn and where an arm is, in sheet pixels (no offset formula needed).
  attach(ext) {
    this.ext = { ...ext };
    if (this.material) {
      this.material.uniforms.tMask.value = ext.mask;
      this.material.uniforms.uDirect.value = 1;
    }
  }

  // Call after stage.update(frame), before stage.render(). frame: { pose, ch, dt, head? }.
  // head (from the character layer): { head: [x, y], breath, at(x, y), view }; without it
  // the head offset is read from the character shader's uniforms.
  update(frame) {
    const cu = this.stage.charMat.uniforms;
    const ext = frame.head || null;
    // seen from behind, the back hair is part of the character's back view
    // a view without its own part set (the turn, the profile) has its hair in the character sheet
    this.mesh.visible = !ext || ext.view === this.view;
    const ch = frame.ch || {};
    const extra = {};
    for (const k of Object.keys(ch)) if (/^hair\.[a-z_]+\.[xy]$/.test(k)) extra[k] = ch[k];
    this.style = frame.pose && frame.pose.hair ? { style: frame.pose.hair } : hairStyle(frame.pose);
    const input = { head: ext ? ext.head : [cu.uHead.value.x, cu.uHead.value.y], breath: ext ? ext.breath : cu.uBreath.value, sway: ch['hair.sway'] || 0, style: this.style.style, extra };
    if (!this.frozen) this.state = stepHair(this.rig, this.state, input, frame.dt || 0);
    const off = hairOffsets(this.rig, this.state);
    // ?hair=rigid (before/after captures): every part moves with the head, no follow-through
    this.drawn.forEach((s, i) => (this.frozen ? this.offsets[i].set(input.head[0], input.head[1] + input.breath) : this.offsets[i].set(off[s.id][0], off[s.id][1])));
    if (ext && ext.at) {
      // a tilted head: each chain hangs from its own point of the head, not from the crown
      this.drawn.forEach((s, i) => {
        const [x, y] = ext.at(...this.rootPivot[i]);
        this.offsets[i].x += Math.round(x - input.head[0]);
        this.offsets[i].y += Math.round(y - input.head[1]);
      });
    }
    this.mesh.position.copy(this.stage.char.mesh.position);
    return off;
  }

  info() {
    return { view: this.view, style: this.style.style, offsets: hairOffsets(this.rig, this.state) };
  }
}
