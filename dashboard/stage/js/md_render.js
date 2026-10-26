// Renderer for the Markdown subset (#350 phase 2): markdown.js blocks -> DOM elements, built
// with doc.createElement / createTextNode / textContent only. No HTML string API or parsing,
// no style attribute, nothing loaded: raw HTML, images and unknown link schemes arrive here
// as plain text spans and stay text. Code is coloured, never run.
//
// Links (http/https only, see markdown.safeHref) never get an href: nothing can be followed
// before the person confirmed it (middle click, the context menu, drag or a screen reader's
// link list have nothing to open). With opts.openLink a link is a button that asks the page
// (the confirmation dialog shows the real address next to the link text) and only then opens
// it with noopener / noreferrer; without opts.openLink it is plain text with its address.
//
// The amount drawn is bounded (opts.maxNodes, default MAX_NODES, counted in one place): past
// the budget no element is made — the rest is plain text under one note.
//
// Static SVG (#453, opt-in): with opts.svg (svg_safe.svgImage, given only when the setting is
// on) a closed ```svg block becomes one <img> whose src is the data: URL of the SVG that
// svg_safe.js rebuilt from its allow-list (an SVG image runs no script and loads nothing).
// At most SVG_LIMITS.perReply pictures per reply; a refused block stays code with its reason
// (and does not take a place).
// Inline <svg> in the text stays text, and without opts.svg every block is code, as before.

import { highlight, langOf } from './markdown.js';
import { SVG_LIMITS, SVG_SRC_PREFIX } from './svg_safe.js';

export const MAX_NODES = 20000; // elements per render (board); the log uses a smaller budget

// Board headings start below the board's own title (h2): the levels a reply uses, in order,
// become h3, h4, … (deterministic per reply; never skips a level).
export function headingLevels(blocks) {
  const used = [...new Set(blocks.filter((b) => b.t === 'h').map((b) => b.level))].sort((a, b) => a - b);
  return new Map(used.map((lvl, i) => [lvl, Math.min(6, 3 + i)]));
}

// doc: a Document; blocks: markdown.parse()/classify().blocks.
// opts: { t(key, vars), openLink(url, text), copy(text) -> Promise<boolean>, maxNodes,
//         svg(text) -> svgImage result (#453; absent = SVG blocks stay code) }
// Returns the list of top-level nodes (append them with replaceChildren(...nodes)).
//
// The element budget is enforced in one place, make(): once it is spent no element is created
// any more; the rest of the paragraph / cell / item becomes one text node, and everything after
// it one plain-text paragraph under a "shown without formatting" note. The note and that
// paragraph come from a small reserve, so the total stays <= maxNodes.
const RESERVE = 3;
export function renderBlocks(doc, blocks, opts = {}) {
  const t = typeof opts.t === 'function' ? opts.t : (k) => k;
  const max = Number.isFinite(opts.maxNodes) ? Math.max(RESERVE + 1, Math.floor(opts.maxNodes)) : MAX_NODES;
  const budget = { left: max - RESERVE, reserve: RESERVE, spent: false };
  const rest = []; // plain text of what could not be drawn as elements
  // the only place elements are made (null when the budget is spent)
  const make = (tag, cls, text) => {
    if (budget.left <= 0) {
      budget.spent = true;
      return null;
    }
    budget.left -= 1;
    const n = doc.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  };
  const fromReserve = (tag, cls, text) => {
    if (budget.reserve <= 0) return null;
    budget.reserve -= 1;
    const n = doc.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  };
  const txt = (v) => doc.createTextNode(String(v ?? ''));
  const spanPlain = (x) => (x.t === 'link' && x.v !== x.href ? `${x.v} (${x.href})` : String(x.v ?? ''));
  const plainSpans = (list) => (list || []).map(spanPlain).join('');
  const plainRows = (list) => (list || []).map(plainSpans).join('\n');
  // inline spans into parent; false when the budget ran out (the rest is one text node)
  const spans = (parent, list) => {
    const l = list || [];
    for (let k = 0; k < l.length; k += 1) {
      const x = l[k];
      if (x.t === 'text' || (x.t === 'link' && typeof opts.openLink !== 'function')) {
        parent.append(txt(spanPlain(x)));
        continue;
      }
      let e = null;
      if (x.t === 'code') e = make('code', 'md-ic', x.v);
      else if (x.t === 'strong') e = make('strong', null, x.v);
      else if (x.t === 'em') e = make('em', null, x.v);
      else if (x.t === 'link' && typeof x.href === 'string') {
        e = make('button', 'md-link', x.v);
        if (e) {
          e.type = 'button';
          e.title = x.href;
          e.addEventListener('click', () => opts.openLink(x.href, x.v));
        }
      } else {
        parent.append(txt(spanPlain(x)));
        continue;
      }
      if (!e) {
        parent.append(txt(plainSpans(l.slice(k))));
        return false;
      }
      parent.append(e);
    }
    return true;
  };
  const rows = (parent, list) => {
    const l = list || [];
    for (let i = 0; i < l.length; i += 1) {
      if (i) {
        const br = make('br');
        if (!br) {
          parent.append(txt(` ${plainRows(l.slice(i))}`));
          return false;
        }
        parent.append(br);
      }
      if (!spans(parent, l[i])) {
        if (i + 1 < l.length) parent.append(txt(` ${plainRows(l.slice(i + 1))}`));
        return false;
      }
    }
    return true;
  };
  // the reason a ```svg block stays code (literal keys for i18n_extract)
  const SVG_REASON = {
    too_long: () => t('stage.board_svg_r_too_long'),
    char: () => t('stage.board_svg_r_char'),
    markup: () => t('stage.board_svg_r_markup'),
    syntax: () => t('stage.board_svg_r_syntax'),
    not_svg: () => t('stage.board_svg_r_not_svg'),
    too_many_elements: () => t('stage.board_svg_r_too_many_elements'),
    too_many_attrs: () => t('stage.board_svg_r_too_many_attrs', { max: SVG_LIMITS.attrs }),
    too_deep: () => t('stage.board_svg_r_too_deep'),
    too_much_text: () => t('stage.board_svg_r_too_much_text'),
    path_too_long: () => t('stage.board_svg_r_path_too_long'),
    too_big: () => t('stage.board_svg_r_too_big'),
    empty: () => t('stage.board_svg_r_empty'),
    too_many_svgs: () => t('stage.board_svg_r_too_many_svgs', { count: SVG_LIMITS.perReply }),
    check_failed: () => t('stage.board_svg_r_check_failed'),
  };
  let svgCount = 0;
  // a code block (with an optional one-line note under it); false when the budget ran out
  const codeBlock = (b, note) => {
    const fig = make('div', 'md-code');
    const head = fig && make('div', 'md-code-head');
    const lang = head && make('span', 'md-lang', b.lang || t('stage.board_code'));
    const pre = lang && make('pre', 'md-pre');
    const code = pre && make('code', langOf(b.lang) ? `lang-${langOf(b.lang)}` : null);
    if (!code) return false;
    head.append(lang);
    const btn = copyButton(b);
    if (btn) head.append(btn);
    pre.tabIndex = 0;
    // coloured while the budget lasts; past it the code stays one plain text node
    const toks = highlight(b.text, b.lang);
    const coloured = toks.filter((x) => x.k !== 'text').length;
    if (coloured && coloured <= budget.left) {
      for (const tok of toks) code.append(tok.k === 'text' ? txt(tok.v) : make('span', `tok-${tok.k}`, tok.v));
    } else code.append(txt(b.text));
    pre.append(code);
    fig.append(head);
    if (note) {
      const n = make('p', 'md-note md-svg-note', note);
      if (n) fig.append(n);
    }
    fig.append(pre);
    out.push(fig);
    return true;
  };
  const copyButton = (b) => {
    const btn = typeof opts.copy === 'function' ? make('button', 'mini md-copy', t('stage.board_copy_code')) : null;
    if (!btn) return null;
    btn.type = 'button';
    btn.setAttribute('aria-label', t('stage.board_copy_code_label', { lang: b.lang || t('stage.board_code') }));
    btn.addEventListener('click', async () => {
      let ok = false;
      try {
        ok = (await opts.copy(b.text)) === true;
      } catch {
        ok = false;
      }
      btn.textContent = t(ok ? 'stage.board_copied' : 'stage.board_copy_failed');
    });
    return btn;
  };
  // a ```svg block as a picture: the image, what was left out, the source folded away
  const svgFigure = (b, r) => {
    const fig = make('div', 'md-code md-svg');
    const head = fig && make('div', 'md-code-head');
    const lang = head && make('span', 'md-lang', b.lang);
    const view = lang && make('div', 'md-svg-view');
    const img = view && make('img', 'md-svg-img');
    const details = img && make('details', 'md-svg-src');
    const summary = details && make('summary', null, t('stage.board_svg_source'));
    const pre = summary && make('pre', 'md-pre');
    const code = pre && make('code', null, b.text);
    if (!code) return false;
    img.alt = r.alt || t('stage.board_svg_alt');
    img.width = r.width;
    img.height = r.height;
    img.decoding = 'async';
    img.referrerPolicy = 'no-referrer';
    img.src = r.src;
    head.append(lang);
    const btn = copyButton(b);
    if (btn) head.append(btn);
    view.append(img);
    fig.append(head, view);
    if (r.removed.length) {
      const shown = r.removed.slice(0, 8).join(', ') + (r.removed.length > 8 ? ', …' : '');
      const n = make('p', 'md-note md-svg-note', t('stage.board_svg_removed', { names: shown }));
      if (n) fig.append(n);
    }
    pre.tabIndex = 0;
    pre.append(code);
    details.append(summary, pre);
    fig.append(details);
    out.push(fig);
    return true;
  };
  const levels = headingLevels(blocks || []);
  const out = [];
  const list = blocks || [];
  let omitted = false;
  let k = 0;
  for (; k < list.length && !budget.spent; k += 1) {
    const b = list[k];
    if (b.t === 'omitted') {
      omitted = true;
      break;
    }
    if (b.t === 'p' || b.t === 'quote') {
      const e = make(b.t === 'p' ? 'p' : 'blockquote', b.t === 'p' ? 'md-p' : 'md-quote');
      if (!e) {
        rest.push(plainRows(b.rows));
        continue;
      }
      rows(e, b.rows);
      out.push(e);
    } else if (b.t === 'h') {
      const e = make(`h${levels.get(b.level) || 6}`, `md-h md-h${b.level}`);
      if (!e) {
        rest.push(plainSpans(b.inl));
        continue;
      }
      spans(e, b.inl);
      out.push(e);
    } else if (b.t === 'hr') {
      const e = make('hr', 'md-hr');
      if (e) out.push(e);
    } else if (b.t === 'ul' || b.t === 'ol') {
      const e = make(b.t, 'md-list');
      if (!e) {
        rest.push(b.items.map(plainSpans).join('\n'));
        continue;
      }
      if (b.t === 'ol' && b.start !== 1) e.start = b.start;
      for (let i = 0; i < b.items.length; i += 1) {
        const li = make('li');
        if (!li) {
          rest.push(b.items.slice(i).map(plainSpans).join('\n'));
          break;
        }
        e.append(li);
        if (!spans(li, b.items[i])) {
          if (i + 1 < b.items.length) rest.push(b.items.slice(i + 1).map(plainSpans).join('\n'));
          break;
        }
      }
      out.push(e);
    } else if (b.t === 'table') {
      const tableText = () => [b.head, ...b.rows].map((r) => r.map(plainSpans).join(' | ')).join('\n');
      // the table scrolls sideways inside its own box (focusable for the keyboard)
      const box = make('div', 'md-table');
      const table = box && make('table');
      const thead = table && make('thead');
      const hr = thead && make('tr');
      const tbody = hr && make('tbody');
      if (!tbody) {
        rest.push(tableText());
        continue;
      }
      box.tabIndex = 0;
      box.setAttribute('role', 'group');
      box.setAttribute('aria-label', t('stage.board_table'));
      const align = (i) => (b.align[i] === 'center' ? 'al-c' : b.align[i] === 'right' ? 'al-r' : null);
      let ok = true;
      for (let i = 0; i < b.head.length && ok; i += 1) {
        const th = make('th', align(i));
        if (!th) {
          hr.append(txt(b.head.slice(i).map(plainSpans).join(' | ')));
          ok = false;
          break;
        }
        th.scope = 'col';
        hr.append(th);
        ok = spans(th, b.head[i]);
      }
      thead.append(hr);
      let shown = 0;
      for (let ri = 0; ri < b.rows.length && ok; ri += 1) {
        const r = b.rows[ri];
        const tr = make('tr');
        if (!tr) {
          ok = false;
          break;
        }
        shown += 1;
        tbody.append(tr);
        for (let i = 0; i < Math.max(b.head.length, r.length); i += 1) {
          const td = make('td', align(i));
          if (!td) {
            tr.append(txt(r.slice(i).map(plainSpans).join(' | ')));
            ok = false;
            break;
          }
          tr.append(td);
          if (!spans(td, r[i] || [])) {
            ok = false;
            break;
          }
        }
      }
      if (shown < b.rows.length) rest.push(b.rows.slice(shown).map((r) => r.map(plainSpans).join(' | ')).join('\n'));
      table.append(thead, tbody);
      box.append(table);
      if (b.cutCols) {
        const n = make('p', 'md-note', t('stage.board_table_cols_cut'));
        if (n) box.append(n);
      }
      if (b.cutRows) {
        const n = make('p', 'md-note', t('stage.board_table_rows_cut', { count: b.cutRows }));
        if (n) box.append(n);
      }
      out.push(box);
    } else if (b.t === 'code') {
      // #453: a closed ```svg block, with the setting on, is offered to svg_safe (at most perReply)
      if (b.lang === 'svg' && b.closed && typeof opts.svg === 'function') {
        // only pictures that are drawn count (a refused block does not take a place)
        let r = svgCount >= SVG_LIMITS.perReply ? { ok: false, reason: 'too_many_svgs' } : null;
        if (!r) {
          try {
            r = opts.svg(b.text);
          } catch {
            r = { ok: false, reason: 'check_failed' };
          }
        }
        // only our own data: URL (svg_safe writes it); anything else stays code
        if (r && r.ok === true && typeof r.src === 'string' && r.src.startsWith(SVG_SRC_PREFIX) && Array.isArray(r.removed)) {
          svgCount += 1;
          if (!svgFigure(b, r)) rest.push(b.text);
          continue;
        }
        const why = (SVG_REASON[r && r.reason] || SVG_REASON.check_failed)();
        if (!codeBlock(b, t('stage.board_svg_refused', { reason: why }))) rest.push(b.text);
        continue;
      }
      if (!codeBlock(b)) rest.push(b.text);
    }
  }
  // what could not be drawn: one note and one plain-text paragraph (from the reserve)
  for (; k < list.length; k += 1) {
    const b = list[k];
    if (b.t === 'omitted') omitted = true;
    else if (b.t === 'p' || b.t === 'quote') rest.push(plainRows(b.rows));
    else if (b.t === 'h') rest.push(plainSpans(b.inl));
    else if (b.t === 'ul' || b.t === 'ol') rest.push(b.items.map(plainSpans).join('\n'));
    else if (b.t === 'table') rest.push([b.head, ...b.rows].map((r) => r.map(plainSpans).join(' | ')).join('\n'));
    else if (b.t === 'code') rest.push(b.text);
  }
  if (budget.spent || rest.length) {
    const note = fromReserve('p', 'md-note', t('stage.board_plain_rest'));
    if (note) out.push(note);
    const text = rest.filter(Boolean).join('\n\n');
    if (text) {
      const plain = fromReserve('p', 'md-plain', text);
      if (plain) out.push(plain);
    }
  }
  if (omitted) {
    const note = fromReserve('p', 'md-note', t('stage.board_omitted'));
    if (note) out.push(note);
  }
  return out;
}
