// Composites for tools/char_desk.py (#341, night round B): the character as the rig draws it
// (char_rig.js) with the hair layer of its view at rest over it (hair.json / hair_back.json), and
// without the mug on the front desk (she takes it to the room desk). Writes raw RGBA frames.
//   node char_desk_compose.mjs <out.raw> '<json list of frames>'
// Reads only the committed assets; nothing is changed.
import fs from 'node:fs';
import path from 'node:path';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

const require = createRequire(import.meta.url);
const HERE = path.dirname(fileURLToPath(import.meta.url));
const STAGE = path.join(HERE, '..');
const { decodePng } = require(path.join(STAGE, '..', '..', 'tests', 'stage_png.cjs'));
const { CharRig, NEUTRAL_FRAME, poseDisplacement } = await import(path.join(STAGE, 'js', 'char_rig.js'));

const meta = JSON.parse(fs.readFileSync(path.join(STAGE, 'assets', 'char', 'parts.json'), 'utf8'));
const atlas = decodePng(fs.readFileSync(path.join(STAGE, 'assets', 'char', 'parts.png')));
const hairSet = (stem) => ({
  geo: JSON.parse(fs.readFileSync(path.join(STAGE, 'assets', 'char', `${stem}.json`), 'utf8')),
  atlas: decodePng(fs.readFileSync(path.join(STAGE, 'assets', 'char', `${stem}.png`))),
});
// the hair layer's part set per view (hair_layer.js HAIR_VIEWS); the turn and the profile have
// their hair in the sheet
const HAIR = { front: hairSet('hair'), back: hairSet('hair_back') };
const rig = new CharRig({ ...meta, layers: meta.layers.filter((l) => l.id !== 'mug_desk') }, atlas);
const W = rig.W;
const H = rig.H;

function compose(f) {
  const r = rig.compose({ ...NEUTRAL_FRAME, ...f });
  const px = Uint8ClampedArray.from(r.pixels);
  const pose = meta.sheets[f.sheet];
  const set = HAIR[pose.view];
  if (!set) return px;
  const hb = set.geo;
  const hbAtlas = set.atlas;
  const offs = hb.segments.map((s) => poseDisplacement(pose, s.pivot[0], s.pivot[1]).map(Math.round));
  for (let y = 0; y < H; y += 1) {
    for (let x = 0; x < W; x += 1) {
      const i = (y * W + x) * 4;
      if (rig.mask[i + 1] > 127) continue;
      for (let k = 0; k < hb.segments.length; k += 1) {
        const s = hb.segments[k];
        if (s.z === 'back' && rig.mask[i] > 127) continue;
        const sx = x - offs[k][0] - s.box[0];
        const sy = y - offs[k][1] - s.box[1];
        if (sx < 0 || sy < 0 || sx >= s.box[2] || sy >= s.box[3]) continue;
        const q = ((s.at[1] + sy) * hbAtlas.width + s.at[0] + sx) * 4;
        if (hbAtlas.data[q + 3] > 127) {
          px[i] = hbAtlas.data[q];
          px[i + 1] = hbAtlas.data[q + 1];
          px[i + 2] = hbAtlas.data[q + 2];
          px[i + 3] = 255;
          break;
        }
      }
    }
  }
  return px;
}

const [out, list] = process.argv.slice(2);
const frames = JSON.parse(list);
const buf = Buffer.alloc(W * H * 4 * frames.length);
frames.forEach((f, k) => buf.set(compose(f), k * W * H * 4));
fs.writeFileSync(out, buf);
process.stdout.write(JSON.stringify({ w: W, h: H, n: frames.length }));
