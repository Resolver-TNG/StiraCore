// Character layer bridge (#341): puts the composited character (char_rig.js) into the
// renderer's existing character layer, without changing scene.js.
//
// The scene draws the character as one sheet texture with weight-map offsets and expression
// overlays (the spike's shader). With the motion parts, the whole sheet is composited on the
// CPU instead (whole pixels, only when the frame changes) and uploaded as that texture; the
// weight maps are set to zero and the overlay boxes to empty, so the shader only adds the rim
// light from the neon. The scene places the layer as before (parallax at the character's own
// depth); nothing of the character's motion reaches the room layers, which roomFrame() places
// from room channels only. `mugTop` is updated so the (state-driven) steam rises from the mug
// wherever the hand holds it.

import * as THREE from '../vendor/three.stage.min.js';
import { CharRig, poseDisplacement } from './char_rig.js';

function loadImage(url) {
  return new Promise((resolve, reject) => {
    const im = new Image();
    im.onload = () => resolve(im);
    im.onerror = () => reject(new Error(url));
    im.src = url;
  });
}

function pixels(img) {
  const cv = document.createElement('canvas');
  cv.width = img.width;
  cv.height = img.height;
  const ctx = cv.getContext('2d', { willReadFrequently: true });
  ctx.drawImage(img, 0, 0);
  const data = ctx.getImageData(0, 0, img.width, img.height);
  return { width: data.width, height: data.height, data: data.data };
}

// The parts, and round A's extras (ear variants, tail; "Asset contract: extras") and face
// effects ("Asset contract: face fx") when present.
export async function loadCharRig(base) {
  const [meta, img] = await Promise.all([fetch(`${base}/char/parts.json`).then((r) => r.json()), loadImage(`${base}/char/parts.png`)]);
  let extras = null;
  try {
    const [xm, ximg] = await Promise.all([fetch(`${base}/char/extras.json`).then((r) => (r.ok ? r.json() : null)), loadImage(`${base}/char/extras.png`)]);
    if (xm) extras = { meta: xm, atlas: pixels(ximg) };
  } catch (e) {
    extras = null; // without them she has no tail and the ears keep their angle sprites
  }
  let faceFx = null;
  try {
    const [fm, fimg] = await Promise.all([fetch(`${base}/char/fx.json`).then((r) => (r.ok ? r.json() : null)), loadImage(`${base}/char/fx.png`)]);
    if (fm) faceFx = { meta: fm, atlas: pixels(fimg) };
  } catch (e) {
    faceFx = null; // without them the face effects of the expressions are not drawn
  }
  let poses = null;
  try {
    const [pm, pimg] = await Promise.all([fetch(`${base}/char/poses.json`).then((r) => (r.ok ? r.json() : null)), loadImage(`${base}/char/poses.png`)]);
    if (pm) poses = { meta: pm, atlas: pixels(pimg) };
  } catch (e) {
    poses = null; // without them the motions that cross the arms keep the mug arm (refused ids)
  }
  return new CharRig(meta, pixels(img), extras, faceFx, poses);
}

// The vendored three.js subset has no DataTexture: the pixels go through a canvas.
function canvasTexture(w, h) {
  const cv = document.createElement('canvas');
  cv.width = w;
  cv.height = h;
  const t = new THREE.Texture(cv);
  t.magFilter = THREE.NearestFilter;
  t.minFilter = THREE.NearestFilter;
  t.generateMipmaps = false;
  t.flipY = false;
  t.premultiplyAlpha = false;
  t.colorSpace = THREE.NoColorSpace;
  t.wrapS = THREE.ClampToEdgeWrapping;
  t.wrapT = THREE.ClampToEdgeWrapping;
  t.needsUpdate = true;
  return { tex: t, ctx: cv.getContext('2d') };
}

export class CharLayer {
  constructor(scene, rig, opts = {}) {
    this.scene = scene;
    this.rig = rig;
    this.tint = Boolean(opts.tint);
    const u = scene.charMat.uniforms;
    const { tex, ctx } = canvasTexture(rig.W, rig.H);
    this.tex = tex;
    this.ctx = ctx;
    this.img = new ImageData(rig.out, rig.W, rig.H);
    const zero = canvasTexture(1, 1).tex;
    u.tBase.value = this.tex;
    u.tRig.value = zero;
    // tRig2 (the old shader's ear / arm offsets, unused by the rig): its alpha marks the face
    // effects, which the shader does not grade (CharRig.fxMask)
    const fxm = canvasTexture(rig.W, rig.H);
    this.fxTex = fxm.tex;
    this.fxCtx = fxm.ctx;
    this.fxImg = new ImageData(rig.fxMask, rig.W, rig.H);
    u.tRig2.value = rig.faceFx ? this.fxTex : zero;
    for (const k of ['uEyeBox', 'uMouthBox', 'uBlushBox', 'uSweatBox']) u[k].value.set(0, 0, 0, 0);
    // ?layers=1: the parts are tinted here; the shared flag also tints the hair layer's parts
    u.uLayers.value = this.tint ? 1 : 0;
    this.mugRest = [...scene.charMeta.mugTop];
    this.head = null;
    this.key = null;
    // the hair layer (hair_layer.js) reads where she is drawn and how her head is posed from
    // here instead of re-deriving the old shader's offsets
    this.hair = opts.hair || null;
    if (this.hair) {
      const m = canvasTexture(rig.W, rig.H);
      this.maskTex = m.tex;
      this.maskCtx = m.ctx;
      this.maskImg = new ImageData(rig.mask, rig.W, rig.H);
      this.hair.attach({ mask: this.maskTex });
    }
  }

  // Input for the hair rig: the head offset at the crown, the breath, the displacement of any
  // point of the head (tilt), and the view (front / back / turn).
  hairInput() {
    const pose = this.rig.meta.sheets[this.head ? this.head.sheet : 'base'];
    const crown = poseDisplacement(pose, 139, 60);
    const br = this.head ? this.head.breath : 0;
    return {
      head: [Math.round(crown[0]), Math.round(crown[1])],
      breath: -br,
      at: (x, y) => poseDisplacement(pose, x, y),
      view: this.head ? this.head.view : 'front',
    };
  }

  // frame: MotionPlayer frame. Returns true when the texture was re-uploaded.
  update(frame) {
    const r = this.rig.compose(frame, { tint: this.tint });
    this.head = r.head;
    this.key = r.key;
    // no mug drawn (a pose arm put it away): no steam either
    this.noMug = !r.mug;
    if (r.mug) {
      // the scene adds the breath itself (uBreath), so the anchor goes without it
      this.scene.charMeta.mugTop[0] = r.mug[0];
      this.scene.charMeta.mugTop[1] = r.mug[1] + (frame.breath ? 1 : 0);
    }
    if (r.changed) {
      this.ctx.putImageData(this.img, 0, 0);
      this.tex.needsUpdate = true;
      if (this.rig.faceFx) {
        this.fxCtx.putImageData(this.fxImg, 0, 0);
        this.fxTex.needsUpdate = true;
      }
      if (this.maskTex) {
        this.maskCtx.putImageData(this.maskImg, 0, 0);
        this.maskTex.needsUpdate = true;
      }
    }
    return r.changed;
  }
}
