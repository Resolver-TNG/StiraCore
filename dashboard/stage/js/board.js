// The whiteboard (#350 phase 2): a projection screen lowered from the ceiling, behind the
// character (who steps to the right edge), for replies that do not fit the text window
// (markdown.classify: code, tables, headings, lists of 3+, quotes, more than 140 characters,
// 3 paragraphs or 4 rows). docs/stage-main-ui.md §2, §4, §5.
//
// It shows the conversation's reply (the text of /api/chat and /api/chat/history), rendered by
// md_render.js (createElement + textContent), or — in its activity mode (#446, activity.js) —
// what she is doing now and did last, drawn from codes and tool names only. A reply always
// takes the board from the activity (unless the person pinned it). Pure helpers first (tested
// in Node), then the DOM controller.

import { renderBlocks } from './md_render.js';
import { spanText } from './markdown.js';

export const OPEN_S = 0.27; // seconds for the screen to come down (and the character to step aside)
export const easeOut = (x) => 1 - (1 - Math.min(1, Math.max(0, x))) ** 3;

// Progress 0..1 toward target; reduced motion jumps.
export function stepProgress(p, target, dt, reduced = false) {
  if (reduced) return target;
  const d = Math.max(0, dt) / OPEN_S;
  return target > p ? Math.min(target, p + d) : Math.max(target, p - d);
}

// Where the board and the character go (CSS px inside the stage, art pixels for the scene).
// in: { width (canvas CSS px), height (the stage's CSS px), scale (CSS px per art pixel), viewW
//   (art px), tbTop (CSS px from the stage top to what the board must stay above: the text
//   window; on a phone the bottom of the scene), portrait, faceCol (art column of her face) }
// out: { shift (art px her face moves right), pan (art px the room pans), aside (art px for the
//   character layer = shift + pan, as the pan also reaches her depth), front (board in front
//   of her: phone, or too little room), left, width, top, bottom (CSS px) }
export const FACE_COL = 320; // scene.js CHAR_POS[0] + 139
// her body's left edge beside the board, art px left of the face (the coat near the text window
// reaches further than the head: 92 keeps the board clear of it, as in the phase-0 mock picture)
export const FACE_HALF = 92;
export const MIN_BOARD_W = 360;
export function boardGeometry({ width, height, scale, viewW, tbTop, portrait, faceCol = FACE_COL }) {
  const s = scale > 0 ? scale : 1;
  const camLeft = Math.round(faceCol - viewW / 2);
  const faceX = (faceCol - camLeft) * s;
  const pan = portrait ? 8 : 12;
  const target = portrait ? width - 10 : width - 120;
  const shift = Math.max(0, Math.round((target - faceX) / s));
  const bottom = Math.max(0, Math.round(height - tbTop + (portrait ? 8 : 12)));
  const besideW = Math.round(target - FACE_HALF * s - 16 - 24);
  if (portrait || besideW < MIN_BOARD_W) {
    return { shift, pan, aside: shift + pan, front: true, left: 6, width: Math.max(160, Math.round(width - 6 - 56)), top: 0, bottom };
  }
  return { shift, pan, aside: shift + pan, front: false, left: 24, width: besideW, top: 0, bottom };
}

// The board's title: the reply's first heading, else a neutral label.
export function boardTitle(c, t) {
  const h = c.blocks.find((b) => b.t === 'h');
  const text = h ? spanText(h.inl).trim() : '';
  return text || t('stage.board_title');
}

// Footer: what the board holds ("見出し 4 · コード 3 · 表 1 · 1716 字"), from the classifier.
export function boardStats(c, t) {
  const parts = [];
  if (c.counts.heading) parts.push(t('stage.board_n_headings', { count: c.counts.heading }));
  if (c.counts.code) parts.push(t('stage.board_n_code', { count: c.counts.code }));
  if (c.counts.table) parts.push(t('stage.board_n_tables', { count: c.counts.table }));
  parts.push(t('stage.board_n_chars', { count: c.counts.visible }));
  return parts.join(' · ');
}

// The reply has ended (#350 review): its final text is classified once more, from scratch, as
// the log and a reload do. If that says "line" while this reply's board is down (a stream
// can look like a board for a moment, e.g. "|-" before "|-x|"), the board rolls up — unless
// the person pinned it. -> 'close' | 'keep'
export function finalBoardAction({ finalMode, boardKey, key, open, pinned }) {
  if (finalMode === 'board' || !open || boardKey !== key || pinned) return 'keep';
  return 'close';
}

// What happens to a new board reply. pinned: the board keeps what it shows (the new one waits
// in the log); same: the reply on the board is still arriving (update in place).
export function boardAction({ pinned, open, currentKey, key, explicit }) {
  if (currentKey === key) return open || explicit ? 'update' : 'keep';
  if (pinned && open && !explicit) return 'held';
  return open ? 'replace' : 'open';
}

// --- DOM controller ----------------------------------------------------------------------------
// ctx: { root (#board), t(key, vars), reduced() -> bool, openLink(url), copy(text) -> Promise<bool>,
//        onClose(), svg(text) -> svg_safe.svgImage result (#453: only when the setting is on) }
export const ACTIVITY_KEY = 'activity';
// ctx.idle (#446, optional): { get() -> bool, set(bool) } — "show the activity while she waits"
export function createBoard(ctx) {
  const doc = ctx.root.ownerDocument;
  const $ = (id) => doc.getElementById(id);
  const el = { root: ctx.root, title: $('board-title-text'), body: $('board-body'), stats: $('board-stats'), note: $('board-note'), pin: $('board-pin'), copy: $('board-copy'), close: $('board-close'), head: $('board-head') };
  const st = { p: 0, target: 0, pinned: false, current: null, geo: null, mode: 'reply', dyn: null, idleBox: null, live: null, note: '' };
  const t = (k, v) => ctx.t(k, v);

  // key: a catalog key or '' (kept, so a language switch re-says it: W5b audit D4)
  function setNote(key) {
    st.note = key || '';
    el.note.textContent = st.note ? t(st.note) : '';
  }

  function setMode(mode) {
    st.mode = mode;
    el.root.dataset.mode = mode;
    el.copy.hidden = mode === 'activity';
  }

  // W5b fix round 1: the once-a-second refresh replaces the dynamic part; a control in it
  // that had the focus (a link: Settings, the approvals page, an artifacts link) gets it back
  // on its new copy — the same tag, href and class, the same occurrence. Focus elsewhere is
  // never touched, and nothing is focused that was not.
  const focusKey = (n) => `${n.tagName}|${n.getAttribute('href') || ''}|${n.className}`;
  function focusInside(box) {
    const active = doc.activeElement;
    if (!box || !active || active === box || !box.contains(active)) return null;
    const key = focusKey(active);
    const same = Array.from(box.querySelectorAll(active.tagName)).filter((n) => focusKey(n) === key);
    return { key, tag: active.tagName, nth: same.indexOf(active) };
  }
  function refocus(box, keep) {
    const again = Array.from(box.querySelectorAll(keep.tag)).filter((n) => focusKey(n) === keep.key)[keep.nth];
    if (again && typeof again.focus === 'function') again.focus({ preventScroll: true });
  }

  // The activity's own body: a dynamic part (replaced on every refresh) and the setting.
  // announce (W5b, a string only with a goal grant): the one polite live region of the board,
  // kept across refreshes and written only when its text changes (no announcement per second).
  function activityBody(nodes, keepScroll, announce) {
    const top = el.body.scrollTop;
    if (!st.dyn || st.dyn.parentNode !== el.body) {
      st.dyn = doc.createElement('div');
      st.dyn.className = 'act-board';
      st.live = null;
      const parts = [st.dyn];
      if (ctx.idle) {
        const label = doc.createElement('label');
        label.className = 'act-setting';
        st.idleBox = doc.createElement('input');
        st.idleBox.type = 'checkbox';
        st.idleBox.id = 'board-idle-activity';
        st.idleBox.checked = ctx.idle.get() === true;
        st.idleBox.addEventListener('change', () => ctx.idle.set(st.idleBox.checked));
        const text = doc.createElement('span');
        text.className = 'act-setting-text';
        label.append(st.idleBox, text);
        parts.push(label);
      }
      el.body.replaceChildren(...parts);
    }
    if (st.idleBox) st.idleBox.nextSibling.textContent = t('stage.act_idle_setting');
    const keep = focusInside(st.dyn);
    st.dyn.replaceChildren(...nodes);
    if (keep) refocus(st.dyn, keep);
    if (typeof announce === 'string') {
      if (!st.live) {
        st.live = doc.createElement('p');
        st.live.className = 'act-live';
        st.live.setAttribute('role', 'status');
        st.live.setAttribute('aria-live', 'polite');
        el.body.append(st.live);
      }
      if (st.live.textContent !== announce) st.live.textContent = announce;
    } else if (st.live) {
      // the grant went away (a restart, no grant, stage C off): its last words go with it
      st.live.remove();
      st.live = null;
    }
    el.body.scrollTop = keepScroll ? top : 0;
  }

  function render(entry, keepScroll) {
    const top = el.body.scrollTop;
    st.dyn = null;
    const c = entry.c;
    el.title.textContent = boardTitle(c, t);
    el.body.replaceChildren(...renderBlocks(doc, c.blocks, { t, openLink: ctx.openLink, copy: ctx.copy, svg: ctx.svg }));
    if (c.cut) {
      const p = doc.createElement('p');
      p.className = 'md-note';
      p.textContent = t('stage.board_cut');
      el.body.append(p);
    }
    el.stats.textContent = boardStats(c, t);
    if (keepScroll) el.body.scrollTop = top;
    else el.body.scrollTop = 0;
  }

  function setPinned(on) {
    el.copy.title = t('stage.board_copy_title');
    st.pinned = on;
    el.pin.setAttribute('aria-pressed', String(on));
    el.pin.textContent = t(on ? 'stage.board_pinned' : 'stage.board_pin');
  }

  const api = {
    // entry: { key, text, c (classify result) }; explicit: the person asked for it (log button)
    // -> the action taken ('open' | 'replace' | 'update' | 'held' | 'keep')
    show(entry, { explicit = false } = {}) {
      const action = boardAction({ pinned: st.pinned, open: st.target === 1, currentKey: st.current && st.current.key, key: entry.key, explicit });
      if (action === 'held') {
        setNote('stage.board_held');
        return action;
      }
      if (action === 'keep') return action;
      const same = action === 'update';
      setMode('reply');
      st.current = entry;
      render(entry, same);
      if (!same) setNote('');
      st.target = 1;
      el.root.hidden = false;
      if (explicit) el.body.focus({ preventScroll: true });
      return action;
    },
    // #446: the activity on the board. nodes: activity.renderActivity(); foot: one line.
    // -> 'open' | 'update' | 'held' (a pinned reply keeps the board unless explicit)
    showActivity(nodes, { explicit = false, foot = '', announce } = {}) {
      const open = st.target === 1;
      if (open && st.pinned && st.mode === 'reply' && !explicit) return 'held';
      const same = open && st.mode === 'activity';
      setMode('activity');
      st.current = { key: ACTIVITY_KEY, text: '' };
      el.title.textContent = t('stage.act_title');
      activityBody(nodes, same, announce);
      el.stats.textContent = foot;
      if (!same) setNote('');
      st.target = 1;
      el.root.hidden = false;
      if (explicit) el.body.focus({ preventScroll: true });
      return same ? 'update' : 'open';
    },
    // a refresh of the activity in place (false: the board shows something else)
    updateActivity(nodes, foot = '', announce = undefined) {
      if (st.mode !== 'activity' || st.target !== 1) return false;
      activityBody(nodes, true, announce);
      el.stats.textContent = foot;
      return true;
    },
    close() {
      if (st.target === 0) return false;
      st.target = 0;
      setNote('');
      if (ctx.onClose) ctx.onClose();
      return true;
    },
    // every frame -> the eased progress (0 closed, 1 down and the character at the side)
    tick(dt) {
      st.p = stepProgress(st.p, st.target, dt, ctx.reduced());
      const e = easeOut(st.p);
      el.root.style.setProperty('--e', e.toFixed(4));
      el.root.dataset.state = st.p === 0 ? 'closed' : st.p === 1 ? 'open' : 'moving';
      if (st.p === 0 && st.target === 0 && !el.root.hidden) el.root.hidden = true;
      return e;
    },
    setGeometry(geo) {
      st.geo = geo;
      const s = el.root.style;
      s.setProperty('--board-left', `${geo.left}px`);
      s.setProperty('--board-w', `${geo.width}px`);
      s.setProperty('--board-top', `${geo.top}px`);
      s.setProperty('--board-bottom', `${geo.bottom}px`);
      el.root.classList.toggle('front', geo.front);
    },
    relabel() {
      setPinned(st.pinned);
      setNote(st.note);
      if (st.mode === 'activity') {
        el.title.textContent = t('stage.act_title');
        if (st.idleBox) st.idleBox.nextSibling.textContent = t('stage.act_idle_setting');
      } else if (st.current) render(st.current, true);
    },
    get mode() {
      return st.mode;
    },
    get showingActivity() {
      return st.mode === 'activity' && st.target === 1;
    },
    get open() {
      return st.target === 1;
    },
    get visible() {
      return st.p > 0 || st.target === 1;
    },
    get pinned() {
      return st.pinned;
    },
    get currentKey() {
      return st.current ? st.current.key : null;
    },
  };

  el.pin.addEventListener('click', () => setPinned(!st.pinned));
  el.close.addEventListener('click', () => api.close());
  // Copy: the reply's whole Markdown source (st.current.text), not only the part shown when a
  // long reply was cut. A refused clipboard (rejection or exception) says so.
  el.copy.addEventListener('click', async () => {
    if (!st.current || st.mode !== 'reply') return;
    let ok = false;
    try {
      ok = (await ctx.copy(st.current.text)) === true;
    } catch {
      ok = false;
    }
    setNote(ok ? 'stage.board_copied' : 'stage.board_copy_failed');
  });
  // phone: swipe the screen up to roll it back
  let y0 = null;
  el.head.addEventListener('pointerdown', (e) => {
    y0 = e.clientY;
  });
  el.head.addEventListener('pointerup', (e) => {
    if (y0 !== null && e.clientY - y0 < -40) api.close();
    y0 = null;
  });
  el.root.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      e.preventDefault();
      e.stopPropagation();
      api.close();
    }
  });
  setPinned(false);
  return api;
}
