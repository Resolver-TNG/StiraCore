// Stage desk shot (#341, night round B): she walks to the room desk and types there with her back
// to us. node --test tests/stage_desk_test.cjs
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const { decodePng } = require('./stage_png.cjs');

const STAGE = path.join(__dirname, '..', 'dashboard', 'stage');
const load = (name) => import(pathToFileURL(path.join(STAGE, 'js', name)).href);
const META = JSON.parse(fs.readFileSync(path.join(STAGE, 'assets', 'char', 'desk.json'), 'utf8'));
const ATLAS = decodePng(fs.readFileSync(path.join(STAGE, 'assets', 'char', 'desk.png')));
const PARTS = JSON.parse(fs.readFileSync(path.join(STAGE, 'assets', 'char', 'parts.json'), 'utf8'));
// round A's back tail at the seated scale, as desk_seat.js builds it in the browser
async function deskTailsFromFiles() {
  const { deskTails } = await load('desk_seat.js');
  const XM = JSON.parse(fs.readFileSync(path.join(STAGE, 'assets', 'char', 'extras.json'), 'utf8'));
  const XA = decodePng(fs.readFileSync(path.join(STAGE, 'assets', 'char', 'extras.png')));
  const pal = ['hair5', 'hair4', 'hair3', 'hair2', 'hair1', 'hair0'].map((k) => [1, 3, 5].map((i) => parseInt(PARTS.palette[k].slice(i, i + 2), 16)));
  return deskTails(XM, XA, pal);
}
const CALM = { emotion: 'calm', eyes: 'open', mouth: 'neutral', headY: 0, ears: 0, blush: 0, sweat: 0 };

test('desk.json and the motion catalog name the same frames; every frame lies inside the atlas', async () => {
  const { DESK_IDS } = await load('motion_catalog.js');
  assert.deepEqual([...DESK_IDS.desk].sort(), ['none', ...META.body].sort());
  // the tail ids are round A's (extras.json tail.front; the back view takes l1 / c / r1)
  const XM = JSON.parse(fs.readFileSync(path.join(STAGE, 'assets', 'char', 'extras.json'), 'utf8'));
  assert.deepEqual([...DESK_IDS.tail].sort(), ['none', ...Object.keys(XM.tail.front)].sort());
  const { TAIL_BACK } = await load('char_rig.js');
  for (const n of Object.keys(XM.tail.front)) assert.ok(XM.tail.back[TAIL_BACK[n]], `${n} -> back ${TAIL_BACK[n]}`);
  assert.deepEqual([...DESK_IDS.ears].sort(), ['angle', 'droop', 'red', 'state', 'twitch1', 'twitch2']);
  for (const [id, f] of Object.entries(META.frames)) {
    const [x, y, w, h] = f.rect;
    assert.ok(x >= 0 && y >= 0 && x + w <= ATLAS.width && y + h <= ATLAS.height, id);
    let n = 0;
    for (let j = y; j < y + h; j += 1) for (let i = x; i < x + w; i += 1) if (ATLAS.data[(j * ATLAS.width + i) * 4 + 3] > 127) n += 1;
    assert.ok(n > 20, `${id} has pixels`);
  }
});

// The art lint's rules (tests/stage_art_lint_test.cjs) for the desk shot's own atlas.
const PALETTE = Object.values(PARTS.palette);
const key = (d, i) => (d[i] << 16) | (d[i + 1] << 8) | d[i + 2];
const keyHex = (h) => parseInt(h.slice(1), 16);
const luma = (k) => (299 * (k >> 16) + 587 * ((k >> 8) & 255) + 114 * (k & 255)) / 1000;

test('whole pixels in her palette (parts.json), no near-black hair line, black only on the outline, no dark specks', () => {
  const allowed = new Set(PALETTE.map(keyHex));
  const hairs = new Set(['hair0', 'hair1', 'hair2', 'hair3', 'hair4', 'hair5'].map((k) => keyHex(PARTS.palette[k])));
  const blacks = new Set([keyHex(PARTS.palette.outline), keyHex(PARTS.palette.line_hair)]);
  const bad = {};
  const report = {};
  for (const [id, f] of Object.entries(META.frames)) {
    const [x0, y0, w, h] = f.rect;
    const at = (x, y) => (x < 0 || y < 0 || x >= w || y >= h ? -1 : ATLAS.data[((y0 + y) * ATLAS.width + x0 + x) * 4 + 3] === 255 ? key(ATLAS.data, ((y0 + y) * ATLAS.width + x0 + x) * 4) : -1);
    let inside = 0;
    let specks = 0;
    for (let y = 0; y < h; y += 1) {
      for (let x = 0; x < w; x += 1) {
        const q = ((y0 + y) * ATLAS.width + x0 + x) * 4;
        const a = ATLAS.data[q + 3];
        if (a !== 0 && a !== 255) bad[`${id} alpha ${a}`] = 1;
        if (a !== 255) continue;
        const k = key(ATLAS.data, q);
        if (!allowed.has(k)) bad[`${id} #${k.toString(16).padStart(6, '0')}`] = (bad[`${id} #${k.toString(16).padStart(6, '0')}`] || 0) + 1;
        if (k === keyHex(PARTS.palette.line_hair)) bad[`${id} near-black hair line`] = (bad[`${id} near-black hair line`] || 0) + 1;
        const nb = [];
        for (const [dx, dy] of [[-1, -1], [0, -1], [1, -1], [-1, 0], [1, 0], [-1, 1], [0, 1], [1, 1]]) nb.push(at(x + dx, y + dy));
        if (blacks.has(k) && !nb.includes(-1) && nb.filter((n) => hairs.has(n)).length >= 5) inside += 1;
        if (!nb.includes(-1) && nb.every((n) => luma(n) - luma(k) >= 60)) specks += 1;
      }
    }
    report[id] = [inside, specks];
  }
  assert.deepEqual(bad, {}, 'colours outside the palette');
  // budgets of the art lint per frame: black inside the hair <= 8, isolated dark specks <= 11
  const over = Object.entries(report).filter(([, [i, s]]) => i > 8 || s > 11).map(([id, v]) => `${id}: ${v}`);
  assert.deepEqual(over, [], 'black inside the hair / dark specks');
});

test('the camera is farther: seated she is 0.40 of her front size; walking she gets smaller with every step', () => {
  const H = (id) => META.frames[id].rect[3];
  assert.ok(H('sit') <= 0.42 * 278, `seated ${H('sit')} px`);
  const walk = ['walk1', 'walk2', 'walk3', 'walk4', 'walk5', 'walk6', 'walk7', 'stand'];
  const w = walk.map((id) => META.frames[id].rect[3]);
  for (let i = 1; i < w.length; i += 1) assert.ok(w[i] < w[i - 1], `walk heights ${w}`);
  // smooth: from her full size (278 px bust = 1.0) every step shrinks by about the same factor
  // (at most 12 % between two frames), and the first step is close to her size at the front
  const ratios = w.slice(1).map((h, i) => h / w[i]);
  for (const r of ratios) assert.ok(r > 0.86 && r < 0.95, `step ratio ${r.toFixed(3)}`);
  assert.ok(META.frames.walk1.rect[3] / 470 > 0.85, 'the first step is near her full size (the whole figure is 470 px there)');
  // and walking away she goes up the floor toward the desk (farther = higher), coming back down
  const top = walk.map((id) => META.frames[id].at[1]);
  for (let i = 1; i < top.length; i += 1) assert.ok(top[i] >= top[i - 1] - 1, `tops ${top}`);
  const back = ['back1', 'back2', 'back3', 'back4', 'back5', 'back6', 'back7'];
  assert.deepEqual(back.map((id) => META.frames[id].rect[3]), walk.slice(0, 7).map((id) => META.frames[id].rect[3]));
});

test('seated she is at the room desk, in front of the laptop and the display, in the wide and the phone view', async () => {
  const { GADGETS } = await load('gadget_catalog.js');
  const mon = GADGETS.find((g) => g.id === 'monitor');
  const sit = META.frames.sit;
  for (const [narrow, laptopX, view] of [[false, mon.at[0], [104, 537]], [true, mon.narrow.at[0], [155, 485]]]) {
    const dx = narrow ? META.narrowDx : 0;
    const x0 = sit.at[0] + dx;
    const x1 = x0 + sit.rect[2];
    const cx = (x0 + x1) / 2;
    assert.ok(cx >= 388 + dx, 'her centre is at the desk (it starts at x 388)');
    assert.ok(Math.abs(cx - (laptopX + 81 / 2)) <= 3, `in front of the laptop (${cx} vs ${laptopX + 40.5})`);
    // the display behind it is wider than her head and taller: its code shows around her head
    const mon = META.frames.monitor;
    const mx = mon.at[0] + dx;
    assert.ok(Math.abs(mx + mon.rect[2] / 2 - cx) <= 3, 'the display is centred on her');
    assert.ok(mon.at[1] < sit.at[1] - 8, 'its top is above her ears');
    assert.ok(mon.at[1] + mon.rect[3] <= 224, 'it stands on the desk (top at y 224)');
    // inside the view (with the 40 px pan while she is there: depth 0.8 -> 32 px)
    assert.ok(x0 - 32 >= view[0] && x1 - 32 <= view[1], `${narrow ? 'phone' : 'wide'}: in view`);
  }
  // the chair is under her: its back covers her lower back, the tail starts at its left edge
  const ch = META.frames.chair;
  assert.ok(ch.at[1] > sit.at[1] + sit.rect[3] * 0.5 && ch.at[1] < sit.at[1] + sit.rect[3]);
  for (const t of META.tails) assert.ok(META.frames[t].at[0] < ch.at[0] + ch.rect[2] / 2, `${t} at the chair's left`);
});

test('which sprites show is a pure function of the motion frame: front layer off only while a desk frame is on', async () => {
  const { deskSprites } = await load('desk_seat.js');
  assert.deepEqual(deskSprites(META, { desk: 'none' }), []);
  assert.deepEqual(deskSprites(META, {}), []);
  assert.deepEqual(deskSprites(META, { desk: 'dance' }), [], 'an unknown frame shows nothing');
  const walk = deskSprites(META, { desk: 'walk1' });
  assert.deepEqual(walk.map((s) => s.part), ['body', 'chair', 'monitor']);
  assert.equal(walk[1].behind, true, 'walking on our side of the chair, it is behind her');
  // the tail: round A's back-view tail at the seated scale (deskTails), the front names mapped
  const tails = await deskTailsFromFiles();
  const sit = deskSprites(META, { desk: 'sit_l', tail: 'up' }, false, tails);
  assert.deepEqual(sit.map((s) => [s.part, s.id]), [['body', 'sit_l'], ['chair', 'chair'], ['monitor', 'monitor'], ['tail', 'c'], ['mug', 'mug']]);
  assert.equal(deskSprites(META, { desk: 'sit', tail: 'r2' }, false, tails)[3].id, 'r1');
  assert.deepEqual(deskSprites(META, { desk: 'sit', tail: 'c' }).map((s) => s.part), ['body', 'chair', 'monitor', 'mug'], 'without the extras: no tail');
  assert.equal(sit[1].behind, false);
  const phone = deskSprites(META, { desk: 'sit' }, true);
  assert.equal(phone[0].at[0], META.frames.sit.at[0] + META.narrowDx);
  assert.deepEqual(deskSprites(META, { desk: 'sit' }), deskSprites(META, { desk: 'sit' }), 'deterministic');
  // main.js hides the front layer and its hair while a desk frame is on
  const main = fs.readFileSync(path.join(STAGE, 'js', 'main.js'), 'utf8');
  assert.match(main, /scene\.char\.mesh\.visible = !atDesk/);
  assert.match(main, /if \(hair && atDesk\) hair\.mesh\.visible = false/);
});

test('the desk shot is paused (2026-10-18): kept in the catalog, no rule starts it; the player still walks there and back when it is selected', async () => {
  const { buildMotions, MOTIONS, MOTION_RULES } = await load('motion_catalog.js');
  const { partIds } = await load('char_rig.js');
  const { MotionPlayer } = await load('motion.js');
  const { map, rejected } = buildMotions(MOTIONS, partIds(PARTS));
  assert.deepEqual(rejected, []);
  const withDesk = [...map.values()].filter((m) => m.tracks.desk).map((m) => m.id);
  assert.deepEqual(withDesk.sort(), ['typing_back', 'watch_back']);
  for (const id of withDesk) assert.ok(!map.get(id).selectable, `${id}: never on agent request`);
  // paused: no rule selects the desk shot (coding is the laptop out in front of her)
  assert.deepEqual(MOTION_RULES.filter((r) => withDesk.includes(r.motion)), []);
  const coding = MOTION_RULES.filter((r) => r.when.gadget && (r.when.gadget.includes('typing') || r.when.gadget.includes('look_screen'))).map((r) => r.motion);
  assert.deepEqual(coding, ['typing', 'look_screen']);
  const p = new MotionPlayer(map, 3);
  p.select('typing_back', 'code');
  const seq = [];
  for (let t = 0; t < 3; t += 1 / 30) seq.push(p.tick(t, CALM).frame.desk);
  const order = [...new Set(seq)];
  assert.deepEqual(order.slice(0, 10), ['none', 'walk1', 'walk2', 'walk3', 'walk4', 'walk5', 'walk6', 'walk7', 'stand', 'sit']);
  p.select('none', 'turn ended');
  const back = [];
  for (let t = 3; t < 4.5; t += 1 / 30) back.push(p.tick(t, CALM).frame);
  assert.deepEqual([...new Set(back.map((f) => f.desk))], ['stand', 'turn', 'back7', 'back6', 'back5', 'back4', 'back3', 'back2', 'back1', 'none']);
  assert.equal(back.at(-1).sheet, 'base');
});

test('on a wide screen the camera looks lower at the desk until the seat is above the text window; not on a phone', async () => {
  const { deskLiftFor, DESK_DEPTH, DESK_SHOW_ROW, DESK_LIFT_MAX } = await load('desk_seat.js');
  // 1300 x 860: the text window starts about 220 rows below the view's top row (10)
  const lift = deskLiftFor({ portrait: false, rowsAbove: 220, viewTop: 10 });
  assert.ok(lift > 0 && lift <= DESK_LIFT_MAX);
  assert.ok(10 + 220 + lift * DESK_DEPTH >= Math.min(DESK_SHOW_ROW, 10 + 220 + DESK_LIFT_MAX * DESK_DEPTH) - 1, 'the seat row is above the window (or the lift is at its cap)');
  assert.equal(deskLiftFor({ portrait: true, rowsAbove: 220, viewTop: 10 }), 0, 'phone: unchanged');
  assert.equal(deskLiftFor({ portrait: false, rowsAbove: 280, viewTop: 10 }), 0, 'the seat already shows');
  assert.equal(deskLiftFor({ portrait: false, rowsAbove: 100, viewTop: 10 }), DESK_LIFT_MAX, 'capped');
  // the room frame moves up by it (cam y), the board's pan untouched
  const { roomFrame } = await load('room.js');
  const pose = { neon: 'cyan', neonLevel: 1, dim: 0, desat: 0 };
  const f = roomFrame(pose, {}, 0, { x: 0, y: 0 }, { e: 1, aside: 0, pan: 40, lift });
  assert.deepEqual(f.cam, [-40, -lift]);
  assert.deepEqual(roomFrame(pose, {}, 0, { x: 0, y: 0 }, { e: 1, aside: 0, pan: 40 }).cam, [-40, 0]);
});

test('seated she types with her arms: sleeves from the shoulders, navy cuffs and pale hands at the keys, a hand presses', () => {
  const pal = PARTS.palette;
  const count = (id, hex) => {
    const [x0, y0, w, h] = META.frames[id].rect;
    const k = parseInt(hex.slice(1), 16);
    const xs = [];
    for (let y = 0; y < h; y += 1) for (let x = 0; x < w; x += 1) {
      const q = ((y0 + y) * ATLAS.width + x0 + x) * 4;
      if (ATLAS.data[q + 3] === 255 && key(ATLAS.data, q) === k) xs.push([x, y]);
    }
    return xs;
  };
  for (const id of META.seated) {
    const w = META.frames[id].rect[2];
    const skin = count(id, pal.skin3);
    const cuff = count(id, pal.navy2);
    // a hand and a cuff on each side
    assert.ok(skin.some(([x]) => x < w / 2) && skin.some(([x]) => x > w / 2), `${id}: both hands`);
    assert.ok(cuff.some(([x]) => x < w / 2) && cuff.some(([x]) => x > w / 2), `${id}: both cuffs`);
    // no black next to the skin (round A's hands: the contour is the skin line)
    const black = new Set(count(id, pal.outline).map(([x, y]) => `${x},${y}`));
    for (const [x, y] of skin) for (const [dx, dy] of [[1, 0], [-1, 0], [0, 1], [0, -1]]) assert.ok(!black.has(`${x + dx},${y + dy}`), `${id}: black next to the hand at ${x},${y}`);
  }
  // sit_l presses with the left hand: its hand is lower than in sit, the right one is not
  const low = (id, left) => Math.max(...count(id, pal.skin3).filter(([x]) => (left ? x < 40 : x > 80)).map(([, y]) => y));
  assert.equal(low('sit_l', true), low('sit', true) + 1);
  assert.equal(low('sit_l', false), low('sit', false));
});

test('the desk shot\'s tail is round A\'s back tail at the seated scale: palette, whole pixels, grey-white fur, at the chair', async () => {
  const tails = await deskTailsFromFiles();
  assert.deepEqual(Object.keys(tails.frames), ['l1', 'c', 'r1']);
  const pal = new Set(Object.values(PARTS.palette).map((h) => parseInt(h.slice(1), 16)));
  const hair = new Set(['hair3', 'hair4', 'hair5'].map((k) => parseInt(PARTS.palette[k].slice(1), 16)));
  const pics = new Set();
  for (const [n, f] of Object.entries(tails.frames)) {
    const [x0, , w, h] = f.rect;
    let light = 0;
    let all = 0;
    let sig = '';
    for (let y = 0; y < h; y += 1) for (let x = 0; x < w; x += 1) {
      const q = (y * tails.size[0] + x0 + x) * 4;
      const a = tails.data[q + 3];
      assert.ok(a === 0 || a === 255);
      if (!a) continue;
      const k = (tails.data[q] << 16) | (tails.data[q + 1] << 8) | tails.data[q + 2];
      assert.ok(pal.has(k), `${n}: #${k.toString(16)}`);
      all += 1;
      if (hair.has(k)) light += 1;
      sig += `${x},${y};`;
    }
    assert.ok(all > 60 && light / all > 0.5, `${n}: grey-white (${light}/${all})`);
    pics.add(sig);
    // about 0.4 of the portrait tail
    assert.ok(w <= 40 && h <= 30, `${n}: ${w} x ${h}`); // round A4's S-curve tail is a little wider
  }
  assert.equal(pics.size, 3, 'three different poses');
  // placed beside the chair, below her shoulders, over the seat
  const { deskSprites } = await load('desk_seat.js');
  const s = deskSprites(META, { desk: 'sit', tail: 'c' }, false, tails).find((x) => x.part === 'tail');
  const ch = META.frames.chair;
  assert.ok(s.at[1] > META.frames.sit.at[1] + 60 && s.at[1] < ch.at[1] + ch.rect[3], `y ${s.at[1]}`);
  assert.ok(s.at[0] + s.rect[2] > ch.at[0] + ch.rect[2] * 0.6, 'at the chair\'s right (the tail hangs past it, reference 01)');
});
