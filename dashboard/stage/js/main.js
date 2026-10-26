// Stage entry point (#341). Wires: events -> model -> animator -> scene / UI.
// Demo (standalone page, or ?demo=1): scripted dummy events. Live (served by the chat server with
// STIRA_STAGE=1, data-stage="live"): Stira's APIs through live_ui.js; inside the dashboard
// shell (#350) the text window also holds the composer (talk.js, the chat's send logic).

import { Animator } from './anim.js';
import { boardGeometry, createBoard } from './board.js';
import { svgImage } from './svg_safe.js';
import { IDLE_KEY, startActivityController } from './activity.js';
import { buildCatalog, CATALOG } from './catalog.js';
import { CharLayer, loadCharRig } from './char_bridge.js';
import { partIds } from './char_rig.js';
import { MOTION_STEPS } from './demo_motions.js';
import { MotionPlayer } from './motion.js';
import { buildMotions, MOTIONS } from './motion_catalog.js';
import { FaceHolder } from './motion_model.js';
import { GADGET_STEPS } from './demo_gadgets.js';
import { AUTOPLAY_SECONDS, STEPS as BASE_STEPS } from './director.js';
import { GADGET_ANIMS } from './gadget_catalog.js';
import { DeskSeat, deskLiftFor } from './desk_seat.js';
import { HAIR_VIEWS, HairLayer } from './hair_layer.js';
import { withHairStyle } from './hair_rig.js';
import { buildStrings, loadCatalog, pickLang, storedLang, storeLang } from './i18n.js';
import { startLive } from './live_ui.js';
import { AMBIENT_ANIMS } from './room.js';
import { pickSky } from './bg_layers.js';
import { derive, DriftMonitor, initialState, reduce } from './model.js';
import { BOARD_FPS, createPacer, FPS_KEY, pickFps } from './pacer.js';
import { NEON_COLORS, StageScene, VIEW_TOP_ROW } from './scene.js';
import { StillScene } from './still_scene.js';
import { startTalk } from './talk.js';
import { StageUI } from './ui.js';

const params = new URLSearchParams(location.search);
const appEl = document.getElementById('app');
const live = (document.documentElement.dataset.stage === 'live' || appEl.dataset.stage === 'live') && params.get('demo') !== '1';
// #350: the page sits in the dashboard shell (side menu); the shell owns the language.
const shellI18n = document.getElementById('app-shell') && window.i18n ? window.i18n : null;
let lang = shellI18n ? shellI18n.lang : pickLang(location.search, navigator.language, storedLang());
let strings = { ja: {}, en: {} }; // the dashboard's i18n catalog
let S = buildStrings(strings, lang);
const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
const ui = new StageUI(S, { instant: reducedMotion.matches });
if (reducedMotion.addEventListener) reducedMotion.addEventListener('change', () => ui.setInstant(reducedMotion.matches));
document.documentElement.lang = lang;
let liveCtl = null;
let talk = null;

// ?ambient=0 turns the room's ambient motion off (layer-separation check, docs/stage.md)
const ambient = params.get('ambient') === '0' ? [] : AMBIENT_ANIMS;
const { map: catalog, rejected } = buildCatalog([...CATALOG, ...GADGET_ANIMS, ...ambient]);
const STEPS = [...BASE_STEPS, ...GADGET_STEPS, ...MOTION_STEPS];
if (rejected.length) console.warn('stage: rejected catalog entries', rejected);
const animator = new Animator(catalog, Number(params.get('seed')) || 341);
const drift = new DriftMonitor();
let state = initialState();
let derived = withHairStyle(derive(state)); // + the hair look, derived from the pose
let hair = null; // HairLayer (hair parts + follow-through), after the scene has loaded
let desk = null; // DeskSeat (she at the room desk, #341 night round B), after the scene has loaded
const DESK_PAN = 40; // art px the room pans while she is at the room desk
const DESK_PAN_S = 0.6; // s the pan takes (about her walk)
let deskE = 0;
const face = new FaceHolder(); // the state's face over time (#341 night round B)
let deskLift = 0;
const log = [];
let stepIndex = 0;
let autoplay = !live && params.get('autoplay') !== '0' && !params.has('state');
let stepClock = 0;
let now = 0;

function summary(ev) {
  switch (ev.type) {
    case 'neural':
      return Object.entries(ev.values).map(([k, v]) => `${k}=${v}`).join(' ');
    case 'breaker':
      return ev.level;
    case 'tool.start':
      return `${ev.kind} ${ev.name}${ev.write ? ' (write)' : ''}${ev.layer ? ` @${ev.layer}` : ''}`;
    case 'tool.end':
      return `${ev.id}${ev.ok === false ? ' failed' : ' ok'}`;
    case 'approval.request':
      return ev.summary;
    case 'approval.resolved':
      return ev.decision || '';
    case 'job.start':
      return ev.name || ev.id;
    case 'memory.forget':
    case 'memory.restore':
      return String(ev.count);
    case 'anim':
      return ev.id;
    case 'turn.end':
      return ev.ok === false ? 'error' : 'ok';
    default:
      return '';
  }
}

function note(line) {
  log.push(`${now.toFixed(1)}s ${line}`);
  if (log.length > 200) log.shift();
}

// the character's motions (motion_catalog.js), ready once the parts atlas is loaded
let motion = null;
let charLayer = null;

function dispatch(ev) {
  if (ev.type === 'anim') {
    const r = motion && motion.has(ev.id) ? motion.request(ev.id, now, derived.pose) : animator.request(ev.id, now);
    log.push(`${now.toFixed(1)}s anim ${summary(ev)}${r.ok ? '' : ` — ignored (${r.reason})`}`);
    return;
  }
  const next = reduce(state, ev);
  log.push(`${now.toFixed(1)}s #${next.seq} ${ev.type} ${summary(ev)}${next === state ? ' — ignored' : ''}`);
  if (log.length > 200) log.shift();
  state = next;
  derived = withHairStyle(derive(state));
  drift.record(state, derived.pose);
  if (motion) motion.select(derived.pose.motion, derived.causes.find((c) => c.channel === 'motion').cause);
  if (ev.type === 'breaker' && Array.isArray(ev.reasons)) breakerLine(ev);
}

// #455: a level change with its reasons is one line in the backlog and the raw view, worded
// by breaker_reasons.js ("WARNING: 安定度 0.31 < 0.35"); the code itself is never shown.
function breakerLine(ev) {
  const words = window.StiraBreakerReasons;
  if (!words || typeof S.t !== 'function') return;
  const text = words.line(ev.raw, ev.reasons, ev.inputs, S.t);
  if (!text) return;
  note(text);
  if (talk) talk.systemLine(text);
}

function playStep(i) {
  stepIndex = (i + STEPS.length) % STEPS.length;
  const step = STEPS[stepIndex];
  dispatch({ type: 'reset' });
  for (const ev of step.events) dispatch(ev);
  ui.say(step.say[lang]);
  ui.showChoices(step.choices, lang);
  stepClock = 0;
  document.documentElement.dataset.step = step.id;
  if (step.choices) ui.focusChoice();
}

ui.onChoice = (c) => {
  if (liveCtl) {
    liveCtl.onChoice(c);
    return;
  }
  const label = c[lang];
  ui.hideChoices();
  if (c.id === 'approve' || c.id === 'deny') dispatch({ type: 'approval.resolved', id: state.approval && state.approval.id, decision: c.id });
  ui.say(`${S.chose}: ${label} ${S.choice_note}`);
};

// --- view toggles --------------------------------------------------------------------------
const raw = document.getElementById('raw');
const debug = document.getElementById('debug');
function setRaw(on) {
  raw.hidden = !on;
  document.body.classList.toggle('raw-on', on);
  document.getElementById('btn-raw').setAttribute('aria-pressed', String(on));
}
function setLang(l) {
  if (shellI18n) {
    shellI18n.setLang(l); // reloads, so the menu and the Stage stay in one language
    return;
  }
  lang = l;
  storeLang(l);
  document.documentElement.lang = l;
  S = buildStrings(strings, l);
  ui.setStrings(S, live);
  if (talk) talk.relabel();
  if (liveCtl) {
    liveCtl.relabel();
    return;
  }
  const step = STEPS[stepIndex];
  ui.say(step.say[l]);
  if (step.choices && state.approval) ui.showChoices(step.choices, l);
}
setRaw(params.get('raw') === '1');
debug.hidden = params.get('debug') !== '1';
document.getElementById('btn-raw').addEventListener('click', () => setRaw(raw.hidden));
const btnLang = document.getElementById('btn-lang');
if (btnLang) btnLang.addEventListener('click', () => setLang(lang === 'ja' ? 'en' : 'ja'));

// Keys typed into the composer, the menu or any control are theirs (#350): the page keys
// (Tab raw view, Enter/Space finish the line, D, L) apply only when the page itself has focus.
const OWN_KEYS = 'input, textarea, select, button, a[href], [contenteditable], [tabindex]:not([tabindex="-1"]), .app-sidebar, #backlog';
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && !raw.hidden) {
    // the raw view covers the text window (and its button): Esc always closes it
    setRaw(false);
    document.getElementById('btn-raw').focus({ preventScroll: true });
    return;
  }
  // #350: Esc rolls the whiteboard up (the log, when open, takes Esc first: talk.js)
  if (e.key === 'Escape' && board && board.open && !(talk && talk.logOpen)) {
    e.preventDefault();
    board.close();
    return;
  }
  const inChoice = e.target instanceof HTMLElement && e.target.closest('#choices');
  if (!inChoice && e.target instanceof HTMLElement && e.target.closest(OWN_KEYS)) return;
  if (e.key === 'Tab' && !inChoice && !(e.target instanceof HTMLButtonElement)) {
    e.preventDefault();
    setRaw(raw.hidden);
    return;
  }
  if (inChoice || e.ctrlKey || e.metaKey || e.altKey) return;
  if (live && (/^[0-9]$/.test(e.key) || e.key === 'Enter' || e.key === ' ')) {
    // live: no dummy states; Enter only finishes the typed line
    if (e.key === 'Enter' || e.key === ' ') ui.finishLine();
  } else if (/^[1-9]$/.test(e.key)) {
    autoplay = false;
    playStep(Number(e.key) - 1);
  } else if (/^[a-z]$/.test(e.key) && STEPS.some((s) => s.key === e.key)) {
    autoplay = false;
    playStep(STEPS.findIndex((s) => s.key === e.key));
  } else if (e.key === '0') {
    autoplay = true;
    playStep(stepIndex + 1);
  } else if (e.key === 'Enter' || e.key === ' ') {
    if (!ui.finishLine()) playStep(stepIndex + 1);
    e.preventDefault();
  } else if (e.key === 'd' || e.key === 'D') {
    debug.hidden = !debug.hidden;
  } else if (e.key === 'l' || e.key === 'L') {
    setLang(lang === 'ja' ? 'en' : 'ja');
  }
});

// --- scene ---------------------------------------------------------------------------------
const canvas = document.getElementById('view');
// ?sky=night|dawn|rain|auto: the window's sky variant (bg_layers.js); default night
const sky = pickSky(params.get('sky'), new Date().getHours());
// WebGL unavailable (or ?webgl=0): a still picture; the conversation works the same (#350)
let scene = null;
if (params.get('webgl') !== '0') {
  try {
    scene = new StageScene(canvas, { layers: params.get('layers') === '1', preserve: params.get('capture') === '1', sky, roomCanvas: document.getElementById('view-room') });
  } catch (err) {
    console.warn('stage: WebGL unavailable, showing a still picture', err);
  }
}
const still = !scene;
if (still) scene = new StillScene(canvas, { sky });
document.documentElement.dataset.render = still ? 'still' : 'webgl';
const stillNote = document.getElementById('stage-still');
if (stillNote) stillNote.hidden = !still;
const stage = document.getElementById('stage');

// The whiteboard (#350 phase 2): only in the shell page (the demo page has none).
const boardEl = document.getElementById('board');
async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}
// A link in a reply opens only after the person confirmed the address (the shell's dialog):
// the dialog names the real address and, when it differs, the text the reply showed for it.
// Without the dialog nothing opens (fail closed).
async function openLink(url, label) {
  const confirm = window.StiraConfirm;
  if (!confirm || typeof confirm.open !== 'function') return false;
  const items = [S.t('stage.link_to', { url })];
  const shown = String(label || '').trim();
  if (shown && shown !== url) items.push(S.t('stage.link_text', { text: shown }));
  const ok = await confirm.open({ title: S.t('stage.link_title'), intro: S.t('stage.link_intro'), items, confirmLabel: S.t('stage.link_open'), cancelLabel: S.t('hub.cancel') }).catch(() => false);
  if (ok !== true) return false;
  window.open(url, '_blank', 'noopener,noreferrer');
  return true;
}
// #453: static SVG on the board unless the settings page turned it off (on by default since
// the ADR 0012 addendum of 2026-10-24; the server writes data-board-svg="on" on #app); the
// browser's DOMParser checks the rebuilt picture once more.
const boardSvg = appEl && appEl.dataset.boardSvg === 'on' ? (text) => svgImage(text, { domParser: window.DOMParser }) : null;
// #446: "show the activity on the board while she waits" is this browser's choice (default off)
const idleSetting = {
  get: () => {
    try {
      return localStorage.getItem(IDLE_KEY) === 'on';
    } catch {
      return false;
    }
  },
  set: (on) => {
    try {
      localStorage.setItem(IDLE_KEY, on ? 'on' : 'off');
    } catch {
      /* storage refused: the choice lasts for this page only */
    }
  },
};
const board = boardEl ? createBoard({ root: boardEl, t: (k, v) => S.t(k, v), reduced: () => reducedMotion.matches, copy: copyText, openLink, onClose: () => talk && talk.boardClosed(), svg: boardSvg, idle: live ? idleSetting : null }) : null;

// The board's activity mode (#446, activity.js): one read-only source, polled only while the
// activity is on the board (poll.js: paused in a hidden tab, backed off on failure, widened
// while nothing changes). The seconds tick here between reads.
function startActivity() {
  const btn = document.getElementById('btn-activity');
  if (!board || !btn || !window.StiraPoll) {
    if (btn) btn.hidden = true;
    return;
  }
  const getJSON = async (url) => {
    const r = await fetch(url, { cache: 'no-store', headers: { Accept: 'application/json' }, redirect: 'error' });
    if (!r.ok) throw new Error(String(r.status));
    return r.json();
  };
  // activity.js startActivityController (tested in Node with jsdom: tests/stage_activity_ctl_test.cjs, #624)
  startActivityController({
    board,
    btn,
    doc: document,
    poll: window.StiraPoll,
    getJSON,
    wall: () => Date.now() / 1000,
    t: (k, v) => S.t(k, v),
    // W5b: data-grants="exec" (the goal iteration executor is on): the watch
    watch: Boolean(appEl && appEl.dataset.grants === 'exec'),
    atLoad: params.get('board') === 'activity',
    status: () => (liveCtl ? liveCtl.status() : null),
    logOpen: () => Boolean(talk && talk.logOpen),
    idle: idleSetting,
    setInterval: (fn, ms) => setInterval(fn, ms),
    setTimeout: (fn, ms) => setTimeout(fn, ms),
  });
}
let boardGeo = null;
const forceSplit = params.get('split') === '1';
let pointer = { x: 0, y: 0 };
stage.addEventListener('pointermove', (e) => {
  const r = stage.getBoundingClientRect();
  pointer = { x: ((e.clientX - r.left) / r.width) * 2 - 1, y: ((e.clientY - r.top) / r.height) * 2 - 1 };
});
stage.addEventListener('pointerleave', () => {
  pointer = { x: 0, y: 0 };
});

const inShell = appEl.classList.contains('stage-shell');
function layout() {
  // In the shell (#350) the Stage fills the content area right of the side menu (its width
  // changes when the menu opens or closes); standalone it fills the window.
  const box = inShell ? appEl.getBoundingClientRect() : null;
  const w = inShell ? Math.max(2, Math.floor(box.width)) : window.innerWidth;
  const h = inShell ? Math.max(2, Math.floor(box.height)) : window.innerHeight;
  const portrait = w / h < 0.9;
  document.body.classList.toggle('portrait', portrait);
  // phone keyboard up (#350): the visual viewport shrinks; the scene gives up its height so the
  // text window and the composer stay in view
  const vv = window.visualViewport;
  const kbd = Boolean(portrait && inShell && vv && vv.height < window.innerHeight * 0.8);
  document.body.classList.toggle('kbd', kbd);
  // Portrait (phones): a wider view at a device-pixel integer scale, so the shelf (memory)
  // and the desk (tools) stay on screen; the text window goes below the scene.
  const minViewW = inShell ? SHELL_PORTRAIT_VIEW_W : PORTRAIT_VIEW_W;
  const tbH = document.getElementById('textbox').getBoundingClientRect().height;
  const sceneH = kbd ? Math.max(120, Math.round(vv.height - tbH - 24 - (box ? box.top : 0))) : Math.round(h * 0.6);
  const info = portrait
    ? scene.resize(w, sceneH, window.devicePixelRatio || 1, { minViewW, maxViewH: PORTRAIT_VIEW_H })
    : scene.resize(w, h, window.devicePixelRatio || 1);
  document.documentElement.style.setProperty('--px', `${info.scale}px`);
  stage.style.width = `${info.view[0] * info.scale}px`;
  stage.style.height = `${info.view[1] * info.scale}px`;
  // props stand on the text window's top edge (landscape) or the bottom of the view (portrait)
  const tb = document.getElementById('textbox').getBoundingClientRect();
  const cv = canvas.getBoundingClientRect();
  // the choices and the log sit above the text window, which grows with the composer
  stage.style.setProperty('--tb-h', `${Math.round(tb.height)}px`);
  const rows = Math.min(info.view[1], Math.max(0, (tb.top - cv.top) / info.scale));
  // the desk shot: how far the camera looks lower so the seat shows above the text window
  // (portrait: the text window is under the view, nothing to lift)
  deskLift = deskLiftFor({ portrait, rowsAbove: rows, viewTop: VIEW_TOP_ROW });
  scene.setFloor(10 + (rows > info.view[1] * 0.5 ? rows : info.view[1]));
  // the canvas height, for what overlays the scene on a phone (the approval choices)
  stage.style.setProperty('--view-h', `${Math.round(cv.height)}px`);
  if (board) {
    // where the board hangs and how far she steps aside (board.js boardGeometry)
    const st = stage.getBoundingClientRect();
    boardGeo = boardGeometry({ width: cv.width, height: st.height, scale: info.scale, viewW: info.view[0], tbTop: portrait ? cv.bottom - st.top : tb.top - st.top, portrait });
    if (still) boardGeo = { ...boardGeo, front: true, aside: 0, pan: 0 };
    board.setGeometry(boardGeo);
  }
}

const PORTRAIT_VIEW_W = 330; // world pixels: shelf, character and desk instrument
// In the shell on a phone the composer needs the room: a closer view (3 device px per art
// pixel on a 390 px DPR 2 phone, as in the #350 mocks); the shelf and the desk stay at the edges.
const SHELL_PORTRAIT_VIEW_W = 256;
const PORTRAIT_VIEW_H = 280; // down to the character's waist (the sheet's bottom rows are a flat cut)

const frames = [];
let rawTimer = 0;
// ?frames=N (captures and the distinctness check): a fixed 1/60 s step, frozen after N frames,
// so the same state gives the same picture regardless of the machine's frame rate. It is not
// paced: every rAF is one step, whatever ?fps= says.
const fixedFrames = Number(params.get('frames')) || 0;
let frameNo = 0;
// Frame pacing (pacer.js): the scene updates and renders at the target rate (default 30 fps;
// ?fps=15..120 or localStorage 'stira.stage.fps'), not at the display's. All motion is driven by
// real time (dt), so only how often the picture refreshes changes. The whiteboard's 270 ms
// roll (and her step aside) runs at 60 while it moves: it reads smooth and ends on a still 30.
const targetFps = pickFps(location.search, () => localStorage.getItem(FPS_KEY));
const pacer = createPacer({ fps: targetFps });
document.documentElement.dataset.fpsTarget = String(targetFps);
let rafPending = false;
function boardMoving() {
  return Boolean(board && board.visible && (!board.open || boardEl.dataset.state !== 'open'));
}
// A hidden tab draws nothing and does not keep the loop alive (rAF is paused there anyway);
// coming back starts a fresh first frame (dt 0), so the time spent away is not played at once.
function next() {
  if (rafPending) return;
  if (!fixedFrames && document.hidden) {
    pacer.reset();
    return;
  }
  rafPending = true;
  requestAnimationFrame(loop);
}
document.addEventListener('visibilitychange', () => {
  if (document.hidden) pacer.reset();
  else if (document.documentElement.dataset.ready === '1') next();
});
function loop(t) {
  rafPending = false;
  let dt = 1 / 60;
  if (!fixedFrames) {
    const p = pacer.frame(t, { minFps: boardMoving() ? BOARD_FPS : 0 });
    if (!p.run) {
      next();
      return;
    }
    dt = p.dt;
  }
  if (fixedFrames && frameNo >= fixedFrames) {
    document.documentElement.dataset.frozen = '1';
    next();
    return;
  }
  frameNo += 1;
  now += dt;
  if (dt > 0) frames.push(dt);
  if (frames.length > 90) frames.shift();
  if (liveCtl) liveCtl.tick(now);
  if (talk) talk.tick();
  // the board's progress drives the screen (CSS) and her step aside (the scene's aside input)
  const boardE = board ? board.tick(dt) : 0;
  if (still) {
    ui.tick(dt);
    rawTimer += dt;
    if (rawTimer > 0.25) {
      rawTimer = 0;
      if (!raw.hidden) ui.renderRaw(state, derived, log, drift, liveCtl ? liveCtl.rawRows() : [[S.mode, S.mode_demo]]);
    }
    next();
    return;
  }
  scene.setAside(boardE, boardGeo);
  // the props on the text window's edge wait while the board is down (gadget_scene.boardHides)
  if (scene.gadgets) scene.gadgets.boardDown = Boolean(board && board.visible);
  // ?split=1 (checks): two canvases even with the board closed, to compare with one canvas
  scene.setSplit(forceSplit || Boolean(board && board.visible && boardGeo && !boardGeo.front));
  if (autoplay) {
    stepClock += dt;
    if (stepClock > AUTOPLAY_SECONDS && !state.approval) playStep(stepIndex + 1);
    if (stepClock > AUTOPLAY_SECONDS * 1.6) playStep(stepIndex + 1);
  }
  // parallax follows the pointer only; once it has caught up it is exactly still
  for (const k of ['x', 'y']) {
    const d = pointer[k] - scene.parallax[k];
    scene.parallax[k] = Math.abs(d) < 0.002 ? pointer[k] : scene.parallax[k] + d * Math.min(1, dt * 4);
  }
  const { numeric, enums } = animator.tick(now, derived.pose);
  if (reducedMotion.matches) for (const k of ['shake', 'cam.x', 'cam.y', 'drone.bob', 'gadget.glitch', 'bell.ring', 'neon.ambient', 'city.twinkle', 'rain.fall']) delete numeric[k];
  const gesture = animator.active.some((a) => a.entry.scope === 'gesture');
  // the face the state asks for (FACE_RULES, held: FaceHolder); the tail follows the emotion in
  // the player (TAIL_MOOD)
  const expr = face.update(now, state, derived.pose);
  document.documentElement.dataset.charFace = expr.id; // for captures and checks
  const mf = motion.tick(now, derived.pose, { numeric, enums, gesture }, expr);
  delete numeric['arm.y']; // the arm is drawn by the motion parts now (the steam follows the mug)
  if (raw.hidden) {
    charLayer.update(mf.frame);
    document.documentElement.dataset.charFrame = charLayer.key; // for captures and checks
    // the mug is in her hand only in the front view: no steam while she is turned away (the
    // mug stands on the desk), and what was rising fades fast
    const atDesk = Boolean(mf.frame && mf.frame.desk && mf.frame.desk !== 'none');
    // the camera follows her to the room desk: the room pans like it does for the board (the
    // board wins when both), eased over the walk
    deskE = Math.min(1, Math.max(0, deskE + (atDesk ? dt : -dt) / DESK_PAN_S));
    if (!(boardE > 0) && deskE > 0) scene.setAside(deskE * deskE * (3 - 2 * deskE), { aside: 0, pan: DESK_PAN, lift: deskLift });
    const away = atDesk || charLayer.noMug || (mf.frame && charLayer.head && charLayer.head.view !== 'front');
    scene.steamFade = away ? 6 : 1;
    scene.update({ pose: away ? { ...derived.pose, steam: 0 } : derived.pose, ch: numeric, en: enums, dt });
    if (hair) {
      const hin = charLayer.hairInput();
      // the hair layer's part set follows the character's view (front, back); loaded once
      if (hin.view !== hair.view && HAIR_VIEWS[hin.view] && !hair.switching) {
        hair.switching = true;
        hair.setView(hin.view).finally(() => { hair.switching = false; });
      }
      hair.update({ pose: derived.pose, ch: numeric, dt, head: hin });
    }
    // at the room desk (or on the way): her figure there, nothing of her at the front
    if (desk) desk.update(mf.frame);
    scene.char.mesh.visible = !atDesk;
    if (hair && atDesk) hair.mesh.visible = false;
    scene.render();
  }
  ui.tick(dt);
  ui.setAccent(NEON_COLORS[derived.pose.neon] || NEON_COLORS.cyan);
  rawTimer += dt;
  if (rawTimer > 0.25) {
    rawTimer = 0;
    const fps = frames.length / frames.reduce((a, b) => a + b, 0.0001);
    document.documentElement.dataset.fps = fps.toFixed(1);
    if (!raw.hidden) ui.renderRaw(state, derived, log, drift, liveCtl ? liveCtl.rawRows() : [[S.mode, S.mode_demo]]);
    if (!debug.hidden) {
      const common = { fps, target: targetFps, view: scene.view, scale: scene.scale, causes: derived.causes, active: [...motion.activeIds(), ...animator.activeIds(), `frame ${charLayer.key}`], ignored: [...motion.ignored, ...animator.ignored], gadgets: scene.gadgets.visibleIds(), boardHidden: scene.gadgets.boardHiddenIds(), lang };
      ui.renderDebug(liveCtl ? { ...common, sources: liveCtl.sources(), steps: [] } : { ...common, steps: STEPS, current: STEPS[stepIndex].id, autoplay });
    }
  }
  next();
}

Promise.all([
  still ? scene.load('assets') : scene.load('assets').then(() => HairLayer.load(scene, 'assets', { frozen: params.get('hair') === 'rigid' })).then(async (h) => {
    await h.preload('back'); // the back view's locks (tools/char_back.py)
    hair = h;
    scene.assignGroups(); // the character group's own render layer (two canvases, #350)
  }),
  loadCatalog(document, (url) => fetch(url)),
  still ? null : loadCharRig('assets'),
])
  .then(([, cat, rig]) => {
    if (!still) {
      const built = buildMotions(MOTIONS, partIds(rig.meta));
      if (built.rejected.length) console.warn('stage: rejected motions', built.rejected);
      motion = new MotionPlayer(built.map, Number(params.get('seed')) || 341);
      charLayer = new CharLayer(scene, rig, { tint: params.get('layers') === '1', hair });
      // the desk shot (its tail is round A's back-view tail at the seated scale)
      DeskSeat.load(scene, 'assets', rig).then((d) => {
        desk = d;
      });
    }
    strings = cat;
    S = buildStrings(strings, lang);
    ui.setStrings(S, live);
    const exit = document.getElementById('btn-exit');
    if (exit) exit.hidden = !live;
    layout();
    window.addEventListener('resize', layout);
    if (window.visualViewport) window.visualViewport.addEventListener('resize', layout);
    if (board) board.relabel();
    if (inShell && typeof ResizeObserver !== 'undefined') {
      // the side menu opening/closing and the composer growing resize the Stage
      let pending = false;
      const ro = new ResizeObserver(() => {
        if (pending) return;
        pending = true;
        requestAnimationFrame(() => {
          pending = false;
          layout();
        });
      });
      ro.observe(appEl);
      ro.observe(document.getElementById('textbox'));
    }
    if (live) {
      const talkHook = { yieldTo: (key) => (talk ? talk.yieldTo(key) : true) };
      liveCtl = startLive({ ui, strings: () => S, dispatch, state: () => state, note, clock: () => now, talk: talkHook });
      const core = window.StiraChatCore;
      if (inShell && core && document.getElementById('stage-form') && typeof window.stiraFetch === 'function') {
        talk = startTalk({ ui, core, fetch: (url, init) => window.stiraFetch(url, init), strings: () => S, live: liveCtl, stage, reducedMotion: reducedMotion.matches, confirm: window.StiraConfirm, board, openLink, copy: copyText });
        if (params.get('log') === '1') setTimeout(() => talk.openLog(), 300);
      }
      startActivity();
      document.documentElement.dataset.mode = 'live';
      // ADR 0012 phase 5b: an automatic-approval line goes to the backlog and the raw view
      // (the event comes only from yolo_ui.js, which exists only with STIRA_YOLO_ALLOW).
      document.addEventListener('stira:yolo-line', (event) => {
        const text = event.detail && typeof event.detail.text === 'string' ? event.detail.text : '';
        if (!text) return;
        note(text);
        if (talk) talk.systemLine(text);
      });
      // ADR 0025: a session allowance's line (an automatic answer, a press, a revoke) from
      // allow_ui.js goes to the same places.
      document.addEventListener('stira:allow-line', (event) => {
        const text = event.detail && typeof event.detail.text === 'string' ? event.detail.text : '';
        if (!text) return;
        note(text);
        if (talk) talk.systemLine(text);
      });
    } else {
      const conn = document.getElementById('conn');
      conn.dataset.state = 'demo';
      conn.textContent = S.conn_demo;
      document.documentElement.dataset.mode = 'demo';
      const start = STEPS.findIndex((s) => s.id === params.get('state'));
      playStep(start >= 0 ? start : 0);
    }
    if (params.get('instant') === '1') ui.finishLine();
    document.documentElement.dataset.ready = '1';
    next();
  })
  .catch((err) => {
    document.documentElement.dataset.ready = 'error';
    console.error('stage: failed to load', err);
  });
