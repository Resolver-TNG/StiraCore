// Character compositor for Stage (#341). Pure: no DOM, no WebGL; tested in Node.
//
// Builds the character sheet of one frame from the layered parts made offline by
// tools/char_parts.py (see parts.json "layers" / "groups"): turtleneck, coat, neck, face, ears,
// eyes / mouth / blush, the hair (slot), brows, the mug arm (or an arm sprite), the desk mug,
// the free arm, effects — back to front. A pose (frame.sheet) picks each part's variant drawn
// for that pose (falling back to the rest pose). Everything is a copy of whole pixels: no
// filtering, no new colours. Nothing here touches the room (background) layers.

export const NEUTRAL_FRAME = Object.freeze({
  sheet: 'base', breath: 0, eyes: 'open', gaze: 'c', brows: 'neutral', mouth: 'neutral',
  earL: 0, earR: 0, armR: 'mug', armL: 'none', blush: 0, sweat: 0, fx: 'none',
  ears: 'angle', tail: 'c', xfx: '',
});

// The back view has three tail poses (extras.json tail.back): the front's others map to them.
export const TAIL_BACK = Object.freeze({ l2: 'l1', l1: 'l1', c: 'c', r1: 'r1', r2: 'r1', up: 'c', down: 'c' });

// Layers whose rows above the breath line rise with the chest (the others move rigidly).
const BREATHING = new Set(['body_underlay', 'turtleneck', 'coat', 'neck']);
// For the hair layer's depth test (mask: R = a pixel of her, G = arm / hand / mug in front).
const ARM_LAYERS = new Set(['forearm_r', 'hand_r', 'mug_desk', 'arm_l']);
const FACE_LAYERS = new Set(['ear_l', 'ear_r', 'eyes', 'mouth', 'blush', 'brows']);
const TINT = {
  hair_back: [80, 220, 230], body_underlay: [120, 120, 140], head_underlay: [200, 140, 200], turtleneck: [60, 80, 220], coat: [230, 200, 90], neck: [240, 140, 120], face: [250, 120, 160],
  ear_l: [230, 80, 160], ear_r: [230, 80, 160], eyes: [255, 240, 120], mouth: [255, 160, 60], blush: [255, 160, 60],
  hair_front: [80, 220, 230], brows: [200, 240, 80], forearm_r: [90, 220, 120], hand_r: [40, 160, 80], mug_desk: [40, 160, 80],
  arm_l: [40, 200, 160], hair_tips: [80, 220, 230], fx: [255, 255, 255],
};

const round = Math.round;

export class CharRig {
  // meta: parts.json; atlas: { width, height, data } (RGBA bytes); extras (optional, round A's
  // extras.json + extras.png, "Asset contract: extras"): { meta, atlas } — ear variants, tail
  // faceFx (optional, round A's fx.json + fx.png, "Asset contract: face fx"): { meta, atlas }
  // poses (optional, round A's poses.json + poses.png, "Asset contract: pose arms"): { meta,
  // atlas } — their arms join meta.arms (a copy; the entries keep a mark to blit from poses.png)
  constructor(meta, atlas, extras = null, faceFx = null, poses = null) {
    this.poses = poses && poses.meta && poses.atlas && poses.meta.arms ? poses : null;
    if (this.poses) {
      const pa = this.poses.atlas;
      this.p32 = new Uint32Array(pa.data.buffer, pa.data.byteOffset, pa.width * pa.height);
      const mark = (set) => Object.fromEntries(Object.entries(set || {}).map(([k, v]) => [k, { ...v, poses: true }]));
      meta = { ...meta, arms: { r: { ...meta.arms.r, ...mark(this.poses.meta.arms.r) }, l: { ...meta.arms.l, ...mark(this.poses.meta.arms.l) } } };
    }
    this.meta = meta;
    this.W = meta.size[0];
    this.H = meta.size[1];
    this.atlas = atlas;
    this.a32 = new Uint32Array(atlas.data.buffer, atlas.data.byteOffset, atlas.width * atlas.height);
    this.out = new Uint8ClampedArray(this.W * this.H * 4);
    this.o32 = new Uint32Array(this.out.buffer);
    this.layer = new Uint8Array(this.W * this.H);
    // the face effects' pixels (fx.json): alpha 255 here where an effect is on top — the
    // character shader leaves them out of the room's grading (a red CRITICAL light must not
    // hide the blue lines or the sweat); RGB stays 0 (the old shader's offset channels)
    this.fxMask = new Uint8ClampedArray(this.W * this.H * 4);
    this.fxl = new Uint8Array(this.W * this.H);
    // the cheek layer's pixels (parts.json `cheek`, overHair): the face she shows at rest, in
    // front of the side locks — also where the mouth, the blush or the eyes are drawn over it
    this.cheekAt = new Uint8Array(this.W * this.H);
    this.order = meta.layers.map((l) => l.id);
    this.mask = new Uint8ClampedArray(this.W * this.H * 4);
    this.armLayer = new Uint8Array(this.order.length + 1);
    // mask G: the hair layer is not drawn over these (the arm and the mug, as in the icon; and the
    // layers marked overHair in parts.json — the eyes and brows in front of the bangs)
    meta.layers.forEach((l, i) => {
      this.armLayer[i + 1] = ARM_LAYERS.has(l.id) || l.overHair === true ? 1 : 0;
    });
    this.faceOver = new Uint8Array(this.order.length + 1);
    for (const id of ['cheek', 'eyes', 'mouth', 'blush']) if (this.order.includes(id)) this.faceOver[this.order.indexOf(id) + 1] = 1;
    this.key = null;
    this.ids = partIds(meta);
    this.extras = extras && extras.meta && extras.atlas ? extras : null;
    if (this.extras) {
      const xa = this.extras.atlas;
      this.x32 = new Uint32Array(xa.data.buffer, xa.data.byteOffset, xa.width * xa.height);
    }
    this.faceFx = faceFx && faceFx.meta && faceFx.atlas ? faceFx : null;
    if (this.faceFx) {
      const fa = this.faceFx.atlas;
      this.f32 = new Uint32Array(fa.data.buffer, fa.data.byteOffset, fa.width * fa.height);
    }
  }

  // A sprite of the extras atlas (whole pixels, alpha 255 only), its top-left at (x, y).
  blitX(part, x, y, li, from = null) {
    const set = from || (this.extras ? { a32: this.x32, width: this.extras.atlas.width } : null);
    if (!part || !part.w || !set) return;
    const { W, H, o32, layer } = this;
    const x32 = set.a32;
    const aw = set.width;
    for (let j = 0; j < part.h; j += 1) {
      const ty = y + j;
      if (ty < 0 || ty >= H) continue;
      const src = (part.ay + j) * aw + part.ax;
      for (let i = 0; i < part.w; i += 1) {
        const tx = x + i;
        if (tx < 0 || tx >= W) continue;
        const v = x32[src + i];
        if (v >>> 24 === 255) {
          o32[ty * W + tx] = v;
          layer[ty * W + tx] = li;
          this.fxl[ty * W + tx] = from && from.a32 === this.f32 ? (from.slot === 'fx' ? 2 : 1) : 0;
        }
      }
    }
  }

  // Face effects of a slot (fx.json, "Asset contract: face fx"): the frame's xfx names
  // ('+'-joined; a name with .l / .r sprites draws both), at their anchors, whole pixels.
  faceEffects(slot, f, A, at, li) {
    if (!this.faceFx || !f.xfx) return;
    const all = this.faceFx.meta.fx;
    const from = { a32: this.f32, width: this.faceFx.atlas.width, slot };
    for (const name of f.xfx.split('+')) {
      for (const id of all[name] ? [name] : [`${name}.l`, `${name}.r`]) {
        const part = all[id];
        if (!part || part.slot !== slot || !A[part.anchor]) continue;
        const [x, y] = at(A[part.anchor]);
        this.blitX(part, x + part.ox, y + part.oy, li, from);
      }
    }
  }

  // The tail of a view (extras tail.front / tail.back, anchored at the sheet's top left).
  tail(view, name, li) {
    if (!this.extras || !name || name === 'none') return;
    const set = this.extras.meta.tail && this.extras.meta.tail[view];
    if (!set) return;
    const part = set[view === 'back' ? TAIL_BACK[name] || 'c' : name] || set.c;
    this.blitX(part, part.ox, part.oy, li);
  }

  // Copies a part with its top-left at (x, y). alpha 255: set; alpha 1: erase; 0: keep.
  // breath (0/1): the part's rows above `row` are drawn one pixel higher (row - 1 doubles).
  blit(part, x, y, li, breath = 0, row = 0) {
    if (!part || !part.w) return;
    // a pose arm (poses.png): whole pixels of its own atlas
    if (part.poses && this.poses) {
      this.blitX(part, x, y, li, { a32: this.p32, width: this.poses.atlas.width });
      return;
    }
    const { W, H, a32, o32, layer } = this;
    const aw = this.atlas.width;
    for (let j = 0; j < part.h; j += 1) {
      const sy = y + j;
      const targets = breath && sy < row ? (sy === row - 1 ? [sy - 1, sy] : [sy - 1]) : [sy];
      const src = (part.ay + j) * aw + part.ax;
      for (const ty of targets) {
        if (ty < 0 || ty >= H) continue;
        const dst = ty * W;
        for (let i = 0; i < part.w; i += 1) {
          const tx = x + i;
          if (tx < 0 || tx >= W) continue;
          const v = a32[src + i];
          const a = v >>> 24;
          if (a === 255) {
            o32[dst + tx] = v;
            layer[dst + tx] = li;
            this.fxl[dst + tx] = 0;
          } else if (a === 1) {
            o32[dst + tx] = 0;
            layer[dst + tx] = 0;
            this.fxl[dst + tx] = 0;
          }
        }
      }
    }
  }

  // The variant of an icon part for this pose: `<pose>` (or `<pose>@free` for the coat when
  // the mug arm is away), else the rest pose's.
  variant(id, pose, free) {
    const p = this.meta.parts[id];
    if (!p) return null;
    if (free && p[`${pose}@free`]) return p[`${pose}@free`];
    if (p[pose]) return p[pose];
    if (free && p['base@free']) return p['base@free'];
    return p.base || null;
  }

  // frame: see NEUTRAL_FRAME (unknown ids fall back to neutral parts). Returns
  // { pixels, changed, mug: [x, y], head, key }.
  compose(frame, opts = {}) {
    const f = { ...NEUTRAL_FRAME, ...frame };
    const key = frameKey(f) + (opts.tint ? '|tint' : '');
    if (key === this.key) return { pixels: this.out, changed: false, mug: this.mug, head: this.head, key };
    this.key = key;
    const m = this.meta;
    this.o32.fill(0);
    this.layer.fill(0);
    this.fxl.fill(0);
    this.cheekAt.fill(0);
    const poseName = m.sheets[f.sheet] ? f.sheet : 'base';
    this.poseName = poseName;
    const pose = m.sheets[poseName];
    const front = pose.view === 'front';
    const A = pose.anchors;
    const B = m.sheets.base.anchors;
    const br = f.breath ? 1 : 0;
    const R = m.breathRow;
    const at = (p, dx = 0, dy = 0) => [round(p[0]) + dx, round(p[1]) - br + dy];
    const armAny = f.armR !== 'mug' ? m.arms.r[f.armR] : null;
    // an arm drawn for another view (the typing arm seen from behind) is the mug arm in the front
    const armR = armAny && armAny.view && front ? null : armAny;
    const chest = [round(A.chest[0] - B.chest[0]), round(A.chest[1] - B.chest[1])];
    let mug = null;
    this.order.forEach((id, idx) => {
      const li = idx + 1;
      const icon = m.parts[id];
      // the tail behind her in the front view (extras contract: after hair_back, before
      // body_underlay; it does not breathe or follow the head)
      if (front && id === 'body_underlay') this.tail('front', f.tail, li);
      if (!front) {
        // back view / turn / profile: only the layers drawn for that view, the mug set down;
        // the head moves with the breath as in the front (the hair layer's roots follow it)
        if (icon && icon[poseName]) {
          const part = icon[poseName];
          const breathes = BREATHING.has(id);
          this.blit(part, part.ox, part.oy - (breathes ? 0 : br), li, breathes ? br : 0, R);
        }
        // the tail in the back view: in front of the coat, before the typing arm
        if (id === 'hand_r' && pose.view === 'back') this.tail('back', f.tail, li);
        // the typing arm seen from behind (arm sprites marked view: back), at the right shoulder
        if (id === 'hand_r' && armR && armR.view === pose.view) {
          const sh = pose.armShift || [0, 0];
          this.blit(armR, armR.ox + sh[0], armR.oy + sh[1] - br, li);
        }
        if (id === 'mug_desk') mug = this.desk(li);
        return;
      }
      if (icon) {
        if ((id === 'forearm_r' || id === 'hand_r') && armR) {
          if (id === 'hand_r') {
            this.blit(armR, armR.ox + chest[0], armR.oy + chest[1] - br, li);
            if (armR.mug) mug = [round(armR.mug[0]) + chest[0], round(armR.mug[1]) + chest[1] - br];
          }
          return;
        }
        const part = this.variant(id, poseName, Boolean(armR) && id === 'coat');
        if (!part) return;
        const breathes = BREATHING.has(id);
        this.blit(part, part.ox, part.oy - (breathes ? 0 : br), li, breathes ? br : 0, R);
        if (id === 'cheek') this.mark(part, part.ox, part.oy - br, this.cheekAt);
        if (id === 'hand_r') mug = [m.mugRest[0] + chest[0], m.mugRest[1] + chest[1] - br];
        return;
      }
      if (FACE_LAYERS.has(id)) this.face(id, f, pose, A, at, li);
      else if (id === 'mug_desk') {
        // a free arm (typing, a pose arm) has put the mug away: no mug is drawn — on the desk
        // edge (props.mug_desk) it floats over her coat in this framing (the desk shot, paused,
        // draws its own desk mug)
      } else if (id === 'arm_l') {
        const part = m.arms.l[f.armL];
        if (part) {
          const sh = [round(A.shoulder_l[0] - B.shoulder_l[0]), round(A.shoulder_l[1] - B.shoulder_l[1])];
          this.blit(part, part.ox + sh[0], part.oy + sh[1] - br, li);
        }
      } else if (id === 'fx') {
        if (f.sweat) {
          const [x, y] = at(A.eye_r, 11, -18);
          this.blit(m.fx.sweat, x, y, li);
        }
        if (f.fx === 'shock') {
          const [x, y] = at(A.eye_l, -54, -46);
          this.blit(m.fx.shock, x, y, li);
        }
        this.faceEffects('fx', f, A, at, li);
      }
      // hair_back / hair_front / hair_tips without a part: slots of the hair layer
    });
    this.buildMask();
    for (let i = 0; i < this.fxl.length; i += 1) this.fxMask[i * 4 + 3] = this.fxl[i] ? 255 : 0;
    if (opts.tint) this.tint();
    // null: no mug is drawn (a free arm in the front view put it away)
    this.mug = front && armR && !mug ? null : mug || [m.mugRest[0], m.mugRest[1]];
    this.head = headPose(poseName, pose, br);
    return { pixels: this.out, changed: true, mug: this.mug, head: this.head, key };
  }

  // The opaque pixels of a part placed with its top-left at (x, y), set to 1 in `into`.
  mark(part, x, y, into) {
    if (!part || !part.w || part.poses) return;
    const { W, H, a32 } = this;
    const aw = this.atlas.width;
    for (let j = 0; j < part.h; j += 1) {
      const ty = y + j;
      if (ty < 0 || ty >= H) continue;
      const src = (part.ay + j) * aw + part.ax;
      for (let i = 0; i < part.w; i += 1) {
        const tx = x + i;
        if (tx >= 0 && tx < W && a32[src + i] >>> 24 === 255) into[ty * W + tx] = 1;
      }
    }
  }

  // mask for the hair layer: R where she is drawn, G where an arm / the mug is (in front of
  // the hair unless a hair segment is overArm)
  buildMask() {
    const { layer, mask, armLayer, cheekAt, faceOver } = this;
    for (let i = 0; i < layer.length; i += 1) {
      const l = layer[i];
      const k = i * 4;
      mask[k] = l ? 255 : 0;
      // G: the hair layer is not drawn over these — the arm / mug / overHair layers and the
      // face effects of the `fx` slot ("on top", the contract); `blush`-slot ones stay under the
      // bangs; the face sprites drawn over the cheek layer keep it in front of the side locks
      mask[k + 1] = (l && armLayer[l]) || this.fxl[i] === 2 || (cheekAt[i] && faceOver[l]) ? 255 : 0;
      mask[k + 2] = 0;
      mask[k + 3] = 255;
    }
  }

  desk(li) {
    const d = this.meta.props.mug_desk;
    this.blit(d, d.ox, d.oy, li);
    return [d.top[0], d.top[1]];
  }

  face(id, f, pose, A, at, li) {
    const m = this.meta;
    if (id === 'ear_l' || id === 'ear_r') {
      const side = id === 'ear_l' ? 'l' : 'r';
      const e = m.ears[side];
      // earL / earR: + = the ear folds outward / back, - = it perks up (rotation sense per side)
      const want = (pose.tilt || 0) + (side === 'l' ? -f.earL : f.earR);
      const ang = e.angles.reduce((best, a) => (Math.abs(a - want) < Math.abs(best - want) ? a : best), e.angles[0]);
      const [x, y] = at(A[`ear_${side}`]);
      // round A's ear drawings (extras ears.<side>): droop and the flick replace the angle
      // sprite (their angle is in the drawing), red is the angle sprite's pink twin
      const xe = this.extras && f.ears && f.ears !== 'angle' ? this.extras.meta.ears[side] : null;
      // droop: one drawing per front sheet where it differs (its top of the head and the crown it
      // meets move with the head's field), else the rest sheet's
      const name = f.ears === 'red' ? `red.${ang}` : f.ears === 'droop' && xe && xe[`droop.${this.poseName}`] ? `droop.${this.poseName}` : f.ears;
      const xpart = xe ? xe[name] : null;
      if (xpart) {
        this.blitX(xpart, x + xpart.ox, y + xpart.oy, li);
        return;
      }
      const part = e.frames[String(ang)];
      this.blit(part, x + part.ox, y + part.oy, li);
    } else if (id === 'eyes') {
      for (const side of ['l', 'r']) {
        const set = m.eyes[side];
        const part = set[`${f.eyes}.${f.gaze}`] || set[`${f.eyes}.c`] || set['open.c'];
        const [x, y] = at(A[`eye_${side}`]);
        this.blit(part, x + part.ox, y + part.oy, li);
      }
    } else if (id === 'mouth') {
      const part = m.mouths[f.mouth] || m.mouths.neutral;
      const [x, y] = at(A.mouth);
      this.blit(part, x + part.ox, y + part.oy, li);
    } else if (id === 'blush') {
      if (f.blush) {
        for (const [side, dx] of [['l', -7], ['r', -6]]) {
          const [x, y] = at(A[`eye_${side}`], dx, 7);
          this.blit(m.fx.blush, x, y, li);
        }
      }
      this.faceEffects('blush', f, A, at, li);
    } else if (id === 'brows') {
      for (const side of ['l', 'r']) {
        const part = m.brows[side][f.brows] || m.brows[side].neutral;
        const [x, y] = at(A[`brow_${side}`]);
        this.blit(part, x + part.ox, y + part.oy, li);
      }
    }
  }

  tint() {
    const { out, layer } = this;
    for (let i = 0; i < layer.length; i += 1) {
      const l = layer[i];
      if (!l) continue;
      const t = TINT[this.order[l - 1]] || [255, 255, 255];
      const k = i * 4;
      out[k] = (out[k] + t[0]) >> 1;
      out[k + 1] = (out[k + 1] + t[1]) >> 1;
      out[k + 2] = (out[k + 2] + t[2]) >> 1;
    }
  }
}

// The head pose of a frame, for layers that follow the head (e.g. hair follow-through):
// view (front / back / turn), tilt in degrees (+ = the top of the head to the right of the
// picture), pitch in pixels at the eyes (+ = nod down), head shift, shoulder drop, breath
// (1 = the upper body is 1 px up) and the neck pivot in sheet pixels.
export function headPose(name, pose, br) {
  const s = pose.spec || {};
  const n = pose.anchors.neck;
  return Object.freeze({
    sheet: name,
    view: pose.view || 'front',
    tilt: s.tilt || 0,
    pitch: s.pitch || 0,
    dx: s.hx || s.head_dx || 0,
    dy: s.hy || s.head_dy || 0,
    shoulders: s.shoulders || 0,
    breath: br,
    neck: [n[0], n[1] - br],
  });
}

// Displacement (px) of a point of the head for a pose: the same field as tools/char_parts.py
// (tilt about the neck, head shift, pitch stronger at the eyes than at the chin, shoulders),
// for layers that follow the head (the hair rig).
export function poseDisplacement(pose, x, y) {
  const s = (pose && pose.spec) || {};
  const [nx, ny] = [139, 142];
  const t = ((s.tilt || 0) * Math.PI) / 180;
  const ox = x - nx;
  const oy = y - ny;
  let dx = Math.cos(t) * ox - Math.sin(t) * oy - ox + (s.hx || 0) + (s.head_dx || 0) - 8 * (s.turn || 0);
  let dy = Math.sin(t) * ox + Math.cos(t) * oy - oy + (s.hy || 0) + (s.head_dy || 0) - ((s.shoulder_l || 0) + (s.shoulder_r || 0)) / 2;
  if (s.pitch) dy += s.pitch * Math.min(1, Math.max(0.4, 1 - (Math.max(0, y - 95) / 43) * 0.6));
  if (s.shoulders) dy += s.shoulders * Math.min(1, Math.max(0, (262 - y) / 110));
  return [dx, dy];
}

export function frameKey(f) {
  return `${f.sheet}|${f.breath}|${f.eyes}.${f.gaze}|${f.brows}|${f.mouth}|${f.earL},${f.earR}|${f.armR}|${f.armL}|${f.blush}${f.sweat}|${f.fx}|${f.ears || 'angle'}|${f.tail || 'c'}${f.xfx ? `|${f.xfx}` : ''}`;
}

// Part ids by kind, as the motion catalog may name them (from parts.json).
// The pose arms of round A's contract ("Asset contract: pose arms", poses.json): valid arm ids
// for the motions; without poses.json the rig draws the mug arm / no free arm for them.
export const POSE_ARM_IDS = Object.freeze(['cross', 'ponder_cross']);

export function partIds(meta) {
  const eyeShapes = new Set();
  const gazes = new Set();
  for (const k of Object.keys(meta.eyes.l)) {
    const [s, g] = k.split('.');
    eyeShapes.add(s);
    gazes.add(g);
  }
  return {
    sheet: Object.keys(meta.sheets),
    eyes: [...eyeShapes],
    gaze: [...gazes],
    brows: Object.keys(meta.brows.l),
    mouth: Object.keys(meta.mouths),
    armR: [...new Set(['mug', ...Object.keys(meta.arms.r), ...POSE_ARM_IDS])],
    armL: [...new Set(['none', ...Object.keys(meta.arms.l), ...POSE_ARM_IDS])],
    fx: ['none', ...Object.keys(meta.fx).filter((k) => k !== 'blush' && k !== 'sweat')],
  };
}
