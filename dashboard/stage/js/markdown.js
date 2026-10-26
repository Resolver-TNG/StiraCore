// Markdown subset for the Stage (#350 phase 2): parser, the line / board rule and a small
// code highlighter. Pure functions, no DOM: the output is plain data that md_render.js turns
// into elements with createElement / textContent only (docs/stage-main-ui.md §5, §6).
//
// Blocks: ATX heading (# .. ######), paragraph, fenced code (``` or ~~~; the info word is the
// language; an unclosed fence runs to the end), GFM table (header row + delimiter row), bullet
// and ordered list (one level), blockquote, thematic break. Inline: `code`, **strong** /
// __strong__, *em* / _em_, [text](http(s) URL). Everything else is text: raw HTML, images,
// footnotes, nested lists and links with any other scheme stay literal characters.

export const LIMITS = Object.freeze({
  lineChars: 140, // visible characters a spoken line may have (more = board)
  linePages: 3, // paragraphs (typewriter pages) a spoken line may have
  pageRows: 4, // line breaks inside one paragraph
  listItems: 3, // a list this long is a board
  leadChars: 120, // the first paragraph spoken as the lead of a board
  boardBytes: 64 * 1024, // longer replies: the head only, "the whole text is on the chat page"
  tableCols: 24, // wider tables keep their first columns ("columns left out")
  tableRows: 400, // longer tables keep their first rows ("rows left out")
  blocks: 2000, // a reply never yields more blocks than this (the rest is left out)
});

// The parser works on the head of the text (LIMITS.boardBytes, UTF-8, never splitting a
// character) on every path — the board, the log and a stream — and in time linear in its
// length: lines are scanned by hand (no regular expression with nested or overlapping
// quantifiers), and every step of the line loop consumes at least one line.

const isSpace = (c) => c === ' ' || c === '\t';
// up to 3 leading spaces (CommonMark's block indent); -1 when there are 4 or more
function indentOf(line) {
  let i = 0;
  while (i < line.length && line[i] === ' ') i += 1;
  return i;
}

// ``` or ~~~ (3+), info string; a backtick fence's info may not hold a backtick.
// -> { ch, len, lang } | null
function fenceOpen(line) {
  const ind = indentOf(line);
  if (ind > 3) return null;
  const ch = line[ind];
  if (ch !== '`' && ch !== '~') return null;
  let j = ind;
  while (j < line.length && line[j] === ch) j += 1;
  const len = j - ind;
  if (len < 3) return null;
  const info = line.slice(j);
  if (ch === '`' && info.includes('`')) return null;
  const t = info.trim();
  let k = 0;
  while (k < t.length && /[A-Za-z0-9_+.#-]/.test(t[k])) k += 1;
  return { ch, len, lang: t.slice(0, k).toLowerCase() };
}
function fenceClose(line, open) {
  const t = line.trim();
  if (t.length < open.len) return false;
  for (let i = 0; i < t.length; i += 1) if (t[i] !== open.ch) return false;
  return true;
}
function heading(line) {
  const ind = indentOf(line);
  if (ind > 3) return null;
  let j = ind;
  while (j < line.length && line[j] === '#') j += 1;
  const level = j - ind;
  if (level < 1 || level > 6 || (j < line.length && !isSpace(line[j]))) return null;
  let text = line.slice(j).trim();
  // closing hashes: " ##" at the end
  let e = text.length;
  while (e > 0 && text[e - 1] === '#') e -= 1;
  if (e < text.length && (e === 0 || isSpace(text[e - 1]))) text = text.slice(0, e).trim();
  return { level, text };
}
function isRule(line) {
  const t = line.trim();
  if (indentOf(line) > 3 || !t) return false;
  const ch = t[0];
  if (ch !== '-' && ch !== '*' && ch !== '_') return false;
  let n = 0;
  for (const c of t) {
    if (c === ch) n += 1;
    else if (!isSpace(c)) return false;
  }
  return n >= 3;
}
// list item marker -> { ordered, start, indent, rest } | null
function listItem(line) {
  const ind = indentOf(line);
  const c = line[ind];
  if ((c === '-' || c === '*' || c === '+') && isSpace(line[ind + 1] || '')) return { ordered: false, start: 1, indent: ind, rest: line.slice(ind + 2).trim() };
  let j = ind;
  while (j < line.length && j - ind < 9 && line[j] >= '0' && line[j] <= '9') j += 1;
  if (j > ind && (line[j] === '.' || line[j] === ')') && isSpace(line[j + 1] || '')) {
    return { ordered: true, start: Number(line.slice(ind, j)), indent: ind, rest: line.slice(j + 2).trim() };
  }
  return null;
}
function quoteLine(line) {
  const ind = indentOf(line);
  if (ind > 3 || line[ind] !== '>') return null;
  return line.slice(ind + (line[ind + 1] === ' ' ? 2 : 1));
}

function cells(line) {
  let s = line.trim();
  if (s.startsWith('|')) s = s.slice(1);
  if (s.endsWith('|') && !s.endsWith('\\|')) s = s.slice(0, -1);
  const out = [];
  let cur = '';
  let inCode = false;
  for (let i = 0; i < s.length; i += 1) {
    const c = s[i];
    if (c === '`') inCode = !inCode;
    if (c === '\\' && s[i + 1] === '|') {
      cur += '|';
      i += 1;
      continue;
    }
    if (c === '|' && !inCode) {
      out.push(cur.trim());
      cur = '';
      continue;
    }
    cur += c;
  }
  out.push(cur.trim());
  return out;
}
// the delimiter row of a GFM table: cells of ":?-+:?" between pipes (at least one pipe)
function tableSep(line) {
  if (typeof line !== 'string' || !line.includes('|') || !line.includes('-') || indentOf(line) > 3) return null;
  const parts = cells(line);
  for (const p of parts) {
    let a = 0;
    let b = p.length;
    if (p[a] === ':') a += 1;
    if (b > a && p[b - 1] === ':') b -= 1;
    if (b <= a) return null;
    for (let i = a; i < b; i += 1) if (p[i] !== '-') return null;
  }
  return parts.map((c) => (c.startsWith(':') && c.endsWith(':') ? 'center' : c.endsWith(':') ? 'right' : 'left'));
}

// text -> blocks: { t: 'p', rows: [spans] } | { t: 'h', level, inl } | { t: 'code', lang, text,
// closed } | { t: 'table', align, head, rows, cutCols, cutRows } | { t: 'ul' | 'ol', start,
// items } | { t: 'quote', rows } | { t: 'hr' } | { t: 'omitted' } (more blocks than the limit)
export function parse(text, limits = LIMITS) {
  const src = headBytes(typeof text === 'string' ? text : '', limits.boardBytes).text.replace(/\r\n?/g, '\n');
  const lines = src.split('\n');
  const n = lines.length;
  const blocks = [];
  let para = [];
  let over = false;
  // every block goes through add(): past the limit nothing more is kept (one "omitted" at the end)
  const add = (b) => {
    if (blocks.length >= limits.blocks) over = true;
    else blocks.push(b);
  };
  const flush = () => {
    if (para.length) add({ t: 'p', rows: para.map((l) => inline(l)) });
    para = [];
  };
  let i = 0;
  let guard = 0;
  while (i < n) {
    guard += 1;
    if (guard > n + 1) break; // never reached: every step below consumes a line
    if (over) break;
    const start = i;
    const line = lines[i];
    let m;
    if ((m = fenceOpen(line))) {
      flush();
      const body = [];
      let closed = false;
      for (i += 1; i < n; i += 1) {
        if (fenceClose(lines[i], m)) {
          closed = true;
          i += 1;
          break;
        }
        body.push(lines[i]);
      }
      add({ t: 'code', lang: m.lang, text: body.join('\n'), closed });
    } else if (!line.trim()) {
      flush();
      i += 1;
    } else if ((m = heading(line))) {
      flush();
      add({ t: 'h', level: m.level, inl: inline(m.text) });
      i += 1;
    } else if (!para.length && isRule(line)) {
      add({ t: 'hr' });
      i += 1;
    } else if (line.includes('|') && (m = tableSep(lines[i + 1]))) {
      flush();
      const headCells = cells(line);
      const cols = Math.min(limits.tableCols, Math.max(headCells.length, m.length));
      const rows = [];
      let cutRows = 0;
      let wide = headCells.length > cols;
      for (i += 2; i < n && lines[i].includes('|') && lines[i].trim(); i += 1) {
        if (rows.length >= limits.tableRows) {
          cutRows += 1; // consumed, not kept
          continue;
        }
        const r = cells(lines[i]);
        if (r.length > cols) wide = true;
        rows.push(r.slice(0, cols).map((c) => inline(c)));
      }
      add({ t: 'table', align: m.slice(0, cols), head: headCells.slice(0, cols).map((c) => inline(c)), rows, cutCols: wide, cutRows });
    } else if ((m = listItem(line)) && m.indent <= 3) {
      flush();
      const first = m;
      const items = [inline(first.rest)];
      for (i += 1; i < n; i += 1) {
        const l = lines[i];
        if (!l.trim()) break;
        const it = listItem(l);
        // a deeper line (nested list, continuation) stays text of the item above
        if (indentOf(l) >= first.indent + 2) items[items.length - 1].push({ t: 'text', v: ` ${l.trim()}` });
        else if (it && it.ordered === first.ordered) items.push(inline(it.rest));
        else break;
      }
      add({ t: first.ordered ? 'ol' : 'ul', start: Math.min(first.start, 1e9), items });
    } else if ((m = quoteLine(line)) !== null) {
      flush();
      const rows = [inline(m)];
      for (i += 1; i < n && (m = quoteLine(lines[i])) !== null; i += 1) rows.push(inline(m));
      add({ t: 'quote', rows });
    } else {
      para.push(line);
      i += 1;
    }
    if (i <= start) {
      // a branch that did not consume its line: the line is text (keeps the scan moving)
      para.push(line);
      i = start + 1;
    }
  }
  if (!over) flush();
  if (over) blocks.push({ t: 'omitted' });
  return blocks;
}

// A link target the board may offer: an absolute http(s) URL without credentials, spaces or
// quotes. Anything else (javascript:, data:, relative paths, …) stays text.
export function safeHref(url) {
  const s = String(url || '');
  if (s.length > 2048 || !/^https?:\/\/[^\s<>"'`\\]+$/i.test(s)) return null;
  let u;
  try {
    u = new URL(s);
  } catch {
    return null;
  }
  if ((u.protocol !== 'http:' && u.protocol !== 'https:') || u.username || u.password || !u.hostname) return null;
  return u.href;
}

// Inline spans: { t: 'text' | 'code' | 'strong' | 'em', v } and { t: 'link', v, href }.
// An image (![alt](src)) is text: nothing is ever loaded. Linear: one pass with lookup
// tables for the next ']' and the next ')'/space, code spans matched by run length.
const isWordChar = (c) => /[A-Za-z0-9]/.test(c || '');
export function inline(s) {
  const str = String(s ?? '');
  const n = str.length;
  const out = [];
  const push = (t, v, extra) => {
    if (!v && t === 'text') return;
    if (t === 'text' && out.length && out[out.length - 1].t === 'text') out[out.length - 1].v += v;
    else out.push({ t, v, ...extra });
  };
  // next index (>= i) of ']' / of ')' / of ')' or whitespace, -1 if none
  const nextBracket = new Int32Array(n + 1).fill(-1);
  const nextParen = new Int32Array(n + 1).fill(-1);
  const nextStop = new Int32Array(n + 1).fill(-1);
  for (let i = n - 1; i >= 0; i -= 1) {
    const c = str[i];
    nextBracket[i] = c === ']' ? i : nextBracket[i + 1];
    nextParen[i] = c === ')' ? i : nextParen[i + 1];
    nextStop[i] = c === ')' || /\s/.test(c) ? i : nextStop[i + 1];
  }
  // backtick runs, and for each the next run of the same length
  const runs = [];
  for (let i = 0; i < n;) {
    if (str[i] !== '`') {
      i += 1;
      continue;
    }
    let j = i;
    while (j < n && str[j] === '`') j += 1;
    runs.push({ at: i, len: j - i, close: -1 });
    i = j;
  }
  const lastByLen = new Map();
  for (let k = runs.length - 1; k >= 0; k -= 1) {
    runs[k].close = lastByLen.has(runs[k].len) ? lastByLen.get(runs[k].len) : -1;
    lastByLen.set(runs[k].len, k);
  }
  const runAt = new Map(runs.map((r, k) => [r.at, k]));
  let i = 0;
  let text = '';
  const emit = (t, v, extra) => {
    if (text) push('text', text);
    text = '';
    push(t, v, extra);
  };
  while (i < n) {
    const c = str[i];
    if (c === '`') {
      const k = runAt.get(i);
      if (k === undefined) {
        text += c;
        i += 1;
        continue;
      }
      const r = runs[k];
      const close = r.close >= 0 ? runs[r.close] : null;
      if (close && close.at > r.at + r.len) {
        emit('code', str.slice(r.at + r.len, close.at));
        i = close.at + close.len;
        // the closing run is used: later runs of that length pair among themselves
      } else {
        text += str.slice(i, i + r.len);
        i += r.len;
      }
      continue;
    }
    if ((c === '!' && str[i + 1] === '[') || c === '[') {
      const o = c === '!' ? i + 1 : i;
      const cb = nextBracket[o + 1];
      if (cb > o + (c === '!' ? 0 : 1) && str[cb + 1] === '(') {
        if (c === '!') {
          const cp = nextParen[cb + 2];
          if (cp >= 0) {
            text += str.slice(i, cp + 1); // an image stays text
            i = cp + 1;
            continue;
          }
        } else {
          const stop = nextStop[cb + 2];
          if (stop > cb + 2 && str[stop] === ')') {
            const href = safeHref(str.slice(cb + 2, stop));
            if (href) emit('link', str.slice(o + 1, cb), { href });
            else text += str.slice(i, stop + 1);
            i = stop + 1;
            continue;
          }
        }
      }
      text += c;
      i += 1;
      continue;
    }
    if ((c === '*' || c === '_') && str[i + 1] === c) {
      // **strong** / __strong__: content without the marker character, not starting with space
      const end = str.indexOf(c, i + 2);
      if (end > i + 2 && str[end + 1] === c && !/\s/.test(str[i + 2]) && (c === '*' || !isWordChar(str[i - 1]))) {
        emit('strong', str.slice(i + 2, end));
        i = end + 2;
        continue;
      }
      text += c + c;
      i += 2;
      continue;
    }
    if (c === '*' || (c === '_' && !isWordChar(str[i - 1]))) {
      const end = str.indexOf(c, i + 1);
      if (end > i + 1 && !/\s/.test(str[i + 1]) && (c === '*' || !isWordChar(str[end + 1]))) {
        emit('em', str.slice(i + 1, end));
        i = end + 1;
        continue;
      }
    }
    text += c;
    i += 1;
  }
  if (text) push('text', text);
  return out;
}

export const spanText = (spans) => spans.map((x) => x.v).join('');
const chars = (s) => [...s].length;

export function plainText(blocks) {
  const parts = [];
  for (const b of blocks) {
    if (b.t === 'p' || b.t === 'quote') parts.push(b.rows.map(spanText).join('\n'));
    else if (b.t === 'h') parts.push(spanText(b.inl));
    else if (b.t === 'code') parts.push(b.text);
    else if (b.t === 'ul' || b.t === 'ol') parts.push(b.items.map(spanText).join('\n'));
    else if (b.t === 'table') parts.push([b.head, ...b.rows].map((r) => r.map(spanText).join(' ')).join('\n'));
  }
  return parts.join('\n\n');
}

// The head of a text that fits in maxBytes of UTF-8 (never splitting a character).
export function headBytes(text, maxBytes) {
  const all = String(text ?? '');
  if (all.length * 3 <= maxBytes) return { text: all, cut: false };
  // a head of maxBytes UTF-8 bytes has at most maxBytes UTF-16 units: search only those
  const s = all.length > maxBytes ? all.slice(0, maxBytes + 1) : all;
  const enc = new TextEncoder();
  if (s === all && enc.encode(s).length <= maxBytes) return { text: s, cut: false };
  let lo = 0;
  let hi = s.length;
  while (lo < hi) {
    const mid = Math.ceil((lo + hi) / 2);
    if (enc.encode(s.slice(0, mid)).length <= maxBytes) lo = mid;
    else hi = mid - 1;
  }
  if (lo > 0 && /[\uD800-\uDBFF]/.test(s[lo - 1])) lo -= 1;
  return { text: s.slice(0, lo), cut: true };
}

// Line or board? Deterministic: the same text always gives the same answer, whatever the
// window size, so the log and the board agree with what was shown.
// -> { mode: 'line' | 'board', reasons: [codes], counts, lead (string | null), line (string,
//      the spoken text in line mode), blocks (what the board shows), cut, source }
export function classify(text, limits = LIMITS) {
  const head = headBytes(typeof text === 'string' ? text : '', limits.boardBytes);
  const blocks = parse(head.text, limits);
  const count = (t) => blocks.filter((b) => b.t === t).length;
  const paras = blocks.filter((b) => b.t === 'p');
  const counts = {
    code: count('code'),
    table: count('table'),
    heading: count('h'),
    quote: count('quote'),
    listItems: blocks.filter((b) => b.t === 'ul' || b.t === 'ol').reduce((n, b) => n + b.items.length, 0),
    visible: chars(plainText(blocks).replace(/\s+/g, '')),
    paragraphs: paras.length,
    rows: Math.max(0, ...paras.map((p) => p.rows.length)),
  };
  const reasons = [];
  if (counts.code) reasons.push('code');
  if (counts.table) reasons.push('table');
  if (counts.heading) reasons.push('heading');
  if (counts.listItems >= limits.listItems) reasons.push('list');
  if (counts.quote) reasons.push('quote');
  if (counts.visible > limits.lineChars) reasons.push('chars');
  if (counts.paragraphs > limits.linePages) reasons.push('paragraphs');
  if (counts.rows > limits.pageRows) reasons.push('rows');
  if (head.cut) reasons.push('size');
  const base = { reasons, counts, cut: head.cut, source: head.text };
  if (!reasons.length) {
    // spoken: one row per paragraph row; a short list becomes "・ item" rows; marks dropped
    const rows = [];
    for (const b of blocks) {
      if (b.t === 'p') rows.push(...b.rows.map(spanText));
      else if (b.t === 'ul' || b.t === 'ol') rows.push(...b.items.map((it, i) => `${b.t === 'ul' ? '・' : `${b.start + i}. `}${spanText(it)}`));
    }
    return { ...base, mode: 'line', lead: null, line: rows.join('\n'), blocks };
  }
  const first = blocks[0];
  const leadOk = Boolean(first && first.t === 'p' && first.rows.length <= limits.pageRows
    && chars(first.rows.map(spanText).join('')) <= limits.leadChars && first.rows.every((r) => r.every((x) => x.t !== 'code')));
  return { ...base, mode: 'board', lead: leadOk ? first.rows.map(spanText).join('\n') : null, line: '', blocks: leadOk ? blocks.slice(1) : blocks };
}

// A stream is re-classified on every delta; once a board, it stays a board (no demotion).
export function classifyStream(text, previousMode, limits = LIMITS) {
  const c = classify(text, limits);
  if (previousMode === 'board' && c.mode === 'line') {
    return { ...c, mode: 'board', reasons: ['kept'], lead: null, line: '', blocks: c.blocks };
  }
  return c;
}

// --- highlighter -----------------------------------------------------------------------------
// Splits code into [{ k, v }] tokens (k: kw, type, fn, str, com, num, punc, text). Data only:
// the renderer makes one <span class="tok-k"> per token with textContent. Joining the token
// values always gives the code back (tests/stage_board_test.cjs). Code is never run.
const LANGS = {
  python: { kw: 'and as assert async await break class continue def del elif else except finally for from global if import in is lambda nonlocal not or pass raise return try while with yield None True False match case', type: 'int float str bool dict list tuple set bytes object Exception', com: ['#'], str3: true },
  js: { kw: 'async await break case catch class const continue default delete do else export extends finally for from function if import in instanceof let new of return static super switch this throw try typeof var void while yield null undefined true false interface type enum implements', type: 'Array Object String Number Boolean Map Set Promise Error', com: ['//'], block: true },
  sh: { kw: 'if then else elif fi for in do done while until case esac function return export local set unset', type: '', com: ['#'] },
  json: { kw: 'true false null', type: '', com: [] },
  css: { kw: 'important', type: '', com: [], block: true },
  sql: { kw: 'select from where and or not insert into values update set delete create table index join left right inner outer on group by order having limit as null is in like distinct union all', type: 'int integer text varchar boolean real', com: ['--'], block: true, ci: true },
};
const ALIAS = { py: 'python', python3: 'python', javascript: 'js', mjs: 'js', cjs: 'js', ts: 'js', typescript: 'js', jsx: 'js', tsx: 'js', node: 'js', bash: 'sh', zsh: 'sh', shell: 'sh', console: 'sh', sh: 'sh', jsonc: 'json', scss: 'css' };

// own properties only: a fence named `constructor`, `__proto__` or `toString` is no language
const own = (obj, key) => Object.prototype.hasOwnProperty.call(obj, key);
export const langOf = (lang) => {
  const l = String(lang || '').toLowerCase();
  if (own(LANGS, l)) return l;
  return own(ALIAS, l) ? ALIAS[l] : null;
};

export function highlight(code, lang) {
  const s = String(code ?? '');
  const id = langOf(lang);
  const L = id && own(LANGS, id) ? LANGS[id] : null;
  if (!L) return s ? [{ k: 'text', v: s }] : [];
  const kw = new Set(L.kw.split(' ').filter(Boolean));
  const ty = new Set(L.type.split(' ').filter(Boolean));
  const out = [];
  const push = (k, v) => {
    if (!v) return;
    if (out.length && out[out.length - 1].k === k) out[out.length - 1].v += v;
    else out.push({ k, v });
  };
  const sticky = (src) => new RegExp(src, 'y');
  const COM = L.com.map((c) => sticky(`${c.replace(/[/\-]/g, '\\$&')}[^\\n]*`));
  const BLOCK = L.block ? sticky('/\\*[\\s\\S]*?(?:\\*/|$)') : null;
  const STR = sticky(`${L.str3 ? '"""[\\s\\S]*?(?:"""|$)|\'\'\'[\\s\\S]*?(?:\'\'\'|$)|' : ''}"(?:\\\\.|[^"\\\\\\n])*"?|'(?:\\\\.|[^'\\\\\\n])*'?${id === 'js' || id === 'sh' ? '|`(?:\\\\.|[^`\\\\])*`?' : ''}`);
  const NUM = sticky('\\d[\\d_]*(?:\\.\\d+)?(?:[eE][+-]?\\d+)?');
  const WORD = sticky('[A-Za-z_$][A-Za-z0-9_$]*');
  const PUNC = sticky('[()[\\]{}.,:;=+\\-*/<>!&|%^~@?]+');
  const SPACE = sticky('\\s+');
  let i = 0;
  let prev = '';
  const at = (re) => {
    re.lastIndex = i;
    const m = re.exec(s);
    return m && m[0] ? m[0] : null;
  };
  while (i < s.length) {
    let v;
    if ((v = COM.map(at).find(Boolean))) push('com', v);
    else if (BLOCK && (v = at(BLOCK))) push('com', v);
    else if ((v = at(STR))) push('str', v);
    else if ((v = at(NUM)) && !/[A-Za-z0-9_$]/.test(s[i - 1] || '')) push('num', v);
    else if ((v = at(WORD))) {
      const w = L.ci ? v.toLowerCase() : v;
      if (kw.has(w)) push('kw', v);
      else if (ty.has(w)) push('type', v);
      else if (['def', 'function', 'class'].includes(prev) || /^\s*\(/.test(s.slice(i + v.length, i + v.length + 8))) push('fn', v);
      else push('text', v);
      prev = v;
      i += v.length;
      continue;
    } else if ((v = at(PUNC))) push('punc', v);
    else if ((v = at(SPACE))) {
      push('text', v);
      i += v.length;
      continue;
    } else {
      v = s[i];
      push('text', v);
    }
    prev = v;
    i += v.length;
  }
  return out;
}
