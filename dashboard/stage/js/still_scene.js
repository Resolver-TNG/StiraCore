// Still picture for the Stage when WebGL is not available (#350 phase 2, docs/stage-main-ui.md
// §7): the room's colour layers and the character's rest sheet drawn once on a 2D canvas at the
// same view as the live scene (whole pixels, no smoothing). Nothing moves; the text window,
// the composer, the log, the board and the approvals work as usual. Same interface as the parts
// of StageScene that main.js uses.

import { layersFor } from './bg_layers.js';
import { loadCharRig } from './char_bridge.js';
import { createHairRig } from './hair_rig.js';
import { CHAR_POS, VIEW_TOP_ROW, viewFor, viewLeft } from './scene.js';

// Her rest pose on the CPU: the composited parts (char_rig.js, the same as the live layer at
// rest) and the hair segments at rest, with the live hair shader's rule (hair_layer.js): back
// parts only where no body pixel is, front parts not over the arm / mug unless overArm, the
// first segment in draw order that has a pixel wins.
export function restCharacter(rig, hairMeta, hairPx) {
  rig.compose({});
  const W = rig.W;
  const H = rig.H;
  const out = new Uint8ClampedArray(rig.out);
  const mask = rig.mask;
  const hr = createHairRig(hairMeta);
  const segs = hairMeta.segments.filter((sg) => hr.index[sg.id] !== undefined).map((sg) => ({ ...sg, overArm: hr.segments[hr.index[sg.id]].overArm }));
  const [aw] = hairMeta.atlas;
  for (let y = 0; y < H; y += 1) {
    for (let x = 0; x < W; x += 1) {
      const i = (y * W + x) * 4;
      const body = mask[i] > 127;
      const arm = mask[i + 1] > 127;
      for (const sg of segs) {
        if (sg.z === 'back' && body) continue;
        if (arm && !sg.overArm) continue;
        const [bx, by, bw, bh] = sg.box;
        if (x < bx || y < by || x >= bx + bw || y >= by + bh) continue;
        const j = ((y - by + sg.at[1]) * aw + (x - bx + sg.at[0])) * 4;
        if (hairPx[j + 3] > 127) {
          out[i] = hairPx[j];
          out[i + 1] = hairPx[j + 1];
          out[i + 2] = hairPx[j + 2];
          out[i + 3] = 255;
          break;
        }
      }
    }
  }
  return { width: W, height: H, data: out };
}

const image = (url) => new Promise((resolve, reject) => {
  const im = new Image();
  im.onload = () => resolve(im);
  im.onerror = () => reject(new Error(`still: ${url}`));
  im.src = url;
});

export class StillScene {
  constructor(canvas, opts = {}) {
    this.canvas = canvas;
    this.opts = opts;
    this.still = true;
    this.parallax = { x: 0, y: 0 };
    this.scale = 1;
    this.view = [2, 2];
    this.images = [];
    this.gadgets = { visibleIds: () => [] };
  }

  async load(base) {
    const { list } = layersFor(this.opts.sky);
    const files = list.filter((e) => e.kind === 'color' && e.band !== 'fore').map((e) => `${base}/bg/${e.file}.png`);
    const fore = list.filter((e) => e.kind === 'color' && e.band === 'fore').map((e) => `${base}/bg/${e.file}.png`);
    const [room, rig, hairMeta, hairImg, front] = await Promise.all([Promise.all(files.map(image)), loadCharRig(base), fetch(`${base}/char/hair.json`).then((r) => r.json()), image(`${base}/char/hair.png`), Promise.all(fore.map(image))]);
    const hc = document.createElement('canvas');
    hc.width = hairImg.width;
    hc.height = hairImg.height;
    const hctx = hc.getContext('2d', { willReadFrequently: true });
    hctx.drawImage(hairImg, 0, 0);
    const still = restCharacter(rig, hairMeta, hctx.getImageData(0, 0, hc.width, hc.height).data);
    const char = document.createElement('canvas');
    char.width = still.width;
    char.height = still.height;
    char.getContext('2d').putImageData(new ImageData(still.data, still.width, still.height), 0, 0);
    // back to front: the room, her rest sheet, the foreground (as in the live scene)
    this.images = [...room.map((im) => [im, 0, 0]), [char, CHAR_POS[0], CHAR_POS[1]], ...front.map((im) => [im, 0, 0])];
  }

  resize(cssW, cssH, dpr, opts = {}) {
    const { s, vw, vh, dev } = viewFor(cssW, cssH, dpr, opts);
    this.scale = s;
    this.view = [vw, vh];
    this.canvas.width = vw * dev;
    this.canvas.height = vh * dev;
    this.canvas.style.width = `${vw * s}px`;
    this.canvas.style.height = `${vh * s}px`;
    this.dev = dev;
    this.draw();
    return { scale: s, view: this.view };
  }

  draw() {
    const ctx = this.canvas.getContext('2d');
    if (!ctx) return;
    const [vw] = this.view;
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.fillStyle = getComputedStyle(this.canvas).getPropertyValue('--deep').trim() || 'black';
    ctx.fillRect(0, 0, this.canvas.width, this.canvas.height);
    ctx.imageSmoothingEnabled = false;
    ctx.setTransform(this.dev, 0, 0, this.dev, -viewLeft(vw) * this.dev, -VIEW_TOP_ROW * this.dev);
    for (const [im, x, y] of this.images) ctx.drawImage(im, x, y);
    ctx.setTransform(1, 0, 0, 1, 0, 0);
  }

  setFloor() {}
  setAside() {}
  setSplit() {}
  update() {}
  render() {}
}
