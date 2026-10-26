// DOM side of the Stage spike: text window, choices, raw view, debug panel.
// All text goes through textContent; nothing from events is ever parsed as HTML.

import { Typewriter } from './typewriter.js';

const $ = (id) => document.getElementById(id);
const setText = (id, text) => {
  const n = $(id);
  if (n && text !== undefined) n.textContent = text;
};

function el(tag, cls, text) {
  const n = document.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined) n.textContent = text;
  return n;
}

export class StageUI {
  // instant: prefers-reduced-motion (each line is shown at once).
  constructor(strings, { instant = false } = {}) {
    this.S = strings;
    this.line = $('line');
    this.live = $('line-live'); // screen readers hear each finished line once
    this.tw = new Typewriter({ instant });
    this.spoken = '';
    this.choicesEl = $('choices');
    this.onChoice = null;
  }

  setInstant(on) {
    this.tw.instant = on;
    if (on && this.tw.skip()) this.render();
  }

  // live: the page reads Stira's APIs (no demo keys in the hint, an exit link).
  setStrings(strings, live = false) {
    this.S = strings;
    setText('nameplate', strings.name);
    // in the shell (#350) the hint names the composer keys
    setText('hint', $('stage-form') ? strings.hint_talk : live ? strings.hint_live : strings.hint);
    setText('btn-raw', strings.raw_toggle);
    setText('btn-lang', strings.lang_toggle);
    setText('btn-exit', strings.exit);
    const view = $('view');
    if (view && strings.canvas_label) view.setAttribute('aria-label', strings.canvas_label);
    const choices = $('choices');
    if (choices && strings.choices_label) choices.setAttribute('aria-label', strings.choices_label);
    document.title = strings.title;
  }

  say(text) {
    this.tw.reset(text);
    this.render();
  }

  // A line that arrives as a stream (#350): the whole text so far; open = more may come.
  stream(text, { open = true } = {}) {
    this.tw.receive(text, { open });
    this.render();
  }

  get typing() {
    return this.tw.typing;
  }

  render() {
    this.line.textContent = this.tw.text;
    this.line.dataset.done = this.tw.done ? '1' : '0';
    if (this.tw.done && this.live && this.spoken !== this.tw.source) {
      this.spoken = this.tw.source;
      this.live.textContent = this.tw.source;
    }
  }

  // typewriter (typewriter.js): ~38 characters per second, faster for a long backlog
  tick(dt) {
    if (this.tw.tick(dt)) this.render();
    else if (this.line.dataset.done !== (this.tw.done ? '1' : '0')) this.render();
  }

  finishLine() {
    if (!this.tw.skip()) return false;
    this.render();
    return true;
  }

  showChoices(choices, lang) {
    const box = this.choicesEl;
    box.replaceChildren();
    if (!choices || !choices.length) {
      box.hidden = true;
      return;
    }
    choices.forEach((c, i) => {
      // c.label: already translated (live choices); c[lang]: demo data
      const b = el('button', 'choice', c.label !== undefined ? c.label : c[lang]);
      b.type = 'button';
      b.dataset.id = c.id;
      if (c.disabled) b.setAttribute('aria-disabled', 'true');
      b.addEventListener('click', () => !c.disabled && this.onChoice && this.onChoice(c));
      b.addEventListener('keydown', (e) => {
        if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
          e.preventDefault();
          const all = [...box.querySelectorAll('button')];
          const j = (i + (e.key === 'ArrowDown' ? 1 : all.length - 1)) % all.length;
          all[j].focus();
        }
      });
      box.append(b);
    });
    box.hidden = false;
  }

  hideChoices() {
    this.choicesEl.hidden = true;
    this.choicesEl.replaceChildren();
  }

  focusChoice() {
    const b = this.choicesEl.querySelector('button');
    if (b && !this.choicesEl.hidden) b.focus({ preventScroll: true });
  }

  setAccent(rgb) {
    const [r, g, b] = rgb.map((v) => Math.round(Math.min(1, v) * 255));
    document.documentElement.style.setProperty('--neon', `rgb(${r} ${g} ${b})`);
  }

  // Raw view: the same events and state, as numbers and log lines only.
  // extraRows: [[label, value]] appended to the state table (live: mode, sources).
  renderRaw(state, derived, log, drift, extraRows = []) {
    const S = this.S;
    $('raw-title').textContent = S.raw_title;
    const st = $('raw-state');
    st.replaceChildren(el('h2', null, S.raw_state));
    const rows = [
      [S.breaker, state.breaker],
      [S.thinking, state.thinking ? S.yes : S.no],
      [S.tools, state.tools.length ? state.tools.map((t) => `${t.kind}:${t.name || t.id}`).join(', ') : S.none],
      [S.approval, state.approval ? state.approval.summary : S.none],
      [S.agents, state.agents.length ? state.agents.map((a) => `${a.name || a.id}:${a.status}`).join(', ') : S.none],
      [S.last_test, state.lastTest || S.none],
      [S.last_error, state.lastError ? `${state.lastError.kind}:${state.lastError.name}` : S.none],
      [S.stamp, state.stamp || S.none],
      [S.jobs, state.jobs.length ? state.jobs.map((j) => j.name || j.id).join(', ') : S.none],
      [S.archived, String(state.archived)],
      ...extraRows,
    ];
    const tbl = el('table', 'kv');
    for (const [k, v] of rows) {
      const tr = el('tr');
      tr.append(el('th', null, k), el('td', null, v));
      tbl.append(tr);
    }
    for (const [k, v] of Object.entries(state.neural)) {
      const tr = el('tr');
      const bar = el('span', 'bar');
      bar.style.setProperty('--v', String(v));
      const td = el('td', null, `${v.toFixed(2)} `);
      td.append(bar);
      tr.append(el('th', null, k), td);
      tbl.append(tr);
    }
    st.append(tbl);
    const pd = $('raw-pose');
    pd.replaceChildren(el('h2', null, S.raw_pose));
    const ul = el('ul', 'causes');
    for (const c of derived.causes) ul.append(el('li', null, `${c.channel} = ${c.value}  ← ${c.cause}`));
    pd.append(ul);
    const lg = $('raw-log');
    lg.replaceChildren(el('h2', null, S.raw_log));
    const ol = el('ol', 'log');
    for (const e of log.slice(-24).reverse()) ol.append(el('li', null, e));
    lg.append(ol);
    const dr = $('raw-drift');
    dr.replaceChildren(el('h2', null, S.raw_drift));
    const ok = drift.drifts.length === 0;
    const counts = S.t ? S.t('stage.drift_counts', { checks: drift.checks, keys: drift.seen.size }) : `${drift.checks} checks, ${drift.seen.size} input keys`;
    const p = el('p', ok ? 'ok' : 'bad', `${ok ? S.drift_ok : S.drift_bad} — ${counts}`);
    dr.append(p);
    for (const d of drift.drifts.slice(-3)) dr.append(el('pre', 'bad', `${d.key}\n- ${d.before}\n+ ${d.after}`));
  }

  renderDebug(info) {
    const S = this.S;
    const box = $('debug');
    box.replaceChildren(el('h2', null, `${S.debug_title} — ${info.fps.toFixed(0)}${info.target ? `/${info.target}` : ''} fps — ${info.view[0]}×${info.view[1]} ×${info.scale}`));
    if (info.sources) {
      // live: what is read, how it went, how long ago
      const ul = el('ul', 'causes');
      for (const s of info.sources) ul.append(el('li', null, `${s.url}  ${s.state}${s.age === null ? '' : `  ${s.age.toFixed(1)}s`}`));
      box.append(el('h3', null, S.debug_sources), ul);
    }
    if (info.steps && info.steps.length) {
      const steps = el('ol', 'steps');
      for (const s of info.steps) {
        const li = el('li', s.id === info.current ? 'cur' : null, `${s.key}  ${S.steps[s.id] || (s.label && s.label[info.lang]) || s.id}`);
        steps.append(li);
      }
      const a = el('li', info.autoplay ? 'cur' : null, `0  ${S.autoplay}`);
      steps.append(a);
      box.append(el('h3', null, S.debug_steps), steps);
    }
    box.append(el('h3', null, S.raw_pose));
    const ul = el('ul', 'causes');
    for (const c of info.causes) ul.append(el('li', null, `${c.channel}=${c.value} ← ${c.cause}`));
    box.append(ul);
    box.append(el('h3', null, S.debug_active), el('p', null, info.active.join(', ') || S.none));
    if (info.gadgets) box.append(el('h3', null, S.debug_gadgets), el('p', null, info.gadgets.join(', ') || S.none));
    // #350: props a pending event would show but the whiteboard keeps hidden
    if (info.boardHidden && info.boardHidden.length) box.append(el('p', null, `${S.debug_board_hidden}: ${info.boardHidden.join(', ')}`));
    box.append(el('h3', null, S.debug_ignored), el('p', null, info.ignored.map((x) => `${x.id} (${x.reason})`).join(', ') || S.none));
  }
}
