// Static SVG on the board (#453): a ```svg block becomes a picture only when it can be rebuilt
// from an allow-list. Pure functions (no DOM needed; tested in Node); the page uses DOMParser,
// when it has one, only as a second check of what this module wrote.
//
// Pipeline (every step fails closed: the block stays code, with a one-line reason):
//   1. parseXml: a small, strict XML reader (linear, index based). No DOCTYPE, ENTITY, CDATA or
//      processing instruction (only a leading `<?xml …?>` declaration); only the five XML
//      entities and numeric references; no control characters; limits on length, elements,
//      depth and text. It only produces a tree of names, attribute strings and text.
//   2. rebuild: a *new* tree from the allowed elements and attributes only. Every attribute
//      value is re-written from a strict grammar (numbers, path commands, the three transforms,
//      colours, keywords; `url(#id)` only to a gradient defined in the same picture), so what
//      is written out is generated here, never copied. Anything else (and its subtree, for an
//      element) is left out and named in a note.
//   3. serialize with escaping, then audit: the output is read again by parseXml and every
//      element / attribute / value is checked against the allow-list (re-validating a value must
//      give the same value). With opts.domParser (the page passes the browser's) the output is
//      also parsed as image/svg+xml and must give exactly the same tree (no parser error, all
//      SVG namespace, the same attributes and text).
//   4. The picture is shown as <img src="data:image/svg+xml,…"> (md_render.js): an SVG image
//      runs no script and loads nothing; the CSP (img-src 'self' data: blob:) is unchanged.

export const SVG_NS = 'http://www.w3.org/2000/svg';
const XLINK_NS = 'http://www.w3.org/1999/xlink';
export const SVG_SRC_PREFIX = 'data:image/svg+xml;charset=utf-8,';

export const SVG_LIMITS = Object.freeze({
  bytes: 32 * 1024, // input, UTF-8
  elements: 500, // every element read, allowed or not
  depth: 8, // nesting, the root is 1
  attrs: 40, // attributes on one element
  pathChars: 8000, // one `d`
  pathNumbers: 4000, // numbers in one `d`
  points: 2000, // numbers in one `points`
  textChars: 500, // one text run
  totalText: 4000, // all text of the picture
  perReply: 4, // pictures in one reply (md_render.js counts)
  size: 4096, // width / height in px
  viewBox: 100000, // viewBox width / height
  coord: 1e6, // |any number|
  shown: 1024, // longest side of the picture's size when it comes from the viewBox only
});

// --- 1. the XML reader -----------------------------------------------------------------------

class Refused extends Error {
  constructor(reason) {
    super(reason);
    this.reason = reason;
  }
}
const refuse = (reason) => {
  throw new Refused(reason);
};

const NAME = /[A-Za-z_][A-Za-z0-9_.-]*(?::[A-Za-z_][A-Za-z0-9_.-]*)?/y;
const XML_DECL = /<\?xml[ \t\n]+version=(["'])1\.[01]\1(?:[ \t\n]+encoding=(["'])[A-Za-z][A-Za-z0-9._-]{0,39}\2)?(?:[ \t\n]+standalone=(["'])(?:yes|no)\3)?[ \t\n]*\?>/y;
const isWs = (c) => c === ' ' || c === '\n' || c === '\t';

// a code point allowed in the input (stricter than XML 1.0: no C1 controls either)
function allowedChar(cp) {
  if (cp === 0x9 || cp === 0xa || cp === 0xd) return true;
  if (cp < 0x20 || (cp >= 0x7f && cp <= 0x9f)) return false;
  if (cp >= 0xd800 && cp <= 0xdfff) return false;
  if (cp === 0xfffe || cp === 0xffff) return false;
  return cp <= 0x10ffff;
}

function utf8Length(s) {
  let n = 0;
  for (let i = 0; i < s.length; i += 1) {
    const c = s.charCodeAt(i);
    if (c < 0x80) n += 1;
    else if (c < 0x800) n += 2;
    else if (c >= 0xd800 && c <= 0xdbff) {
      n += 4;
      i += 1;
    } else n += 3;
  }
  return n;
}

const ENTITIES = { amp: '&', lt: '<', gt: '>', quot: '"', apos: "'" };
// &amp; &lt; &gt; &quot; &apos; &#N; &#xH; — anything else refuses the picture
function decode(raw) {
  if (!raw.includes('&')) return raw;
  let out = '';
  let i = 0;
  while (i < raw.length) {
    const amp = raw.indexOf('&', i);
    if (amp < 0) {
      out += raw.slice(i);
      break;
    }
    out += raw.slice(i, amp);
    const semi = raw.indexOf(';', amp);
    if (semi < 0 || semi - amp > 10) refuse('syntax');
    const body = raw.slice(amp + 1, semi);
    if (Object.prototype.hasOwnProperty.call(ENTITIES, body)) out += ENTITIES[body];
    else {
      let cp = NaN;
      if (/^#[0-9]{1,7}$/.test(body)) cp = Number(body.slice(1));
      else if (/^#x[0-9A-Fa-f]{1,6}$/.test(body)) cp = parseInt(body.slice(2), 16);
      else refuse('markup'); // a named entity (or a DTD's) is never expanded
      if (!allowedChar(cp)) refuse('char');
      out += String.fromCodePoint(cp);
    }
    i = semi + 1;
  }
  return out;
}

// text -> { name, attrs: [[name, value]], kids: [node | string] } (the root element)
export function parseXml(text, L = SVG_LIMITS) {
  if (typeof text !== 'string') refuse('syntax');
  if (utf8Length(text) > L.bytes) refuse('too_long');
  let s = text.charCodeAt(0) === 0xfeff ? text.slice(1) : text;
  for (let i = 0; i < s.length; i += 1) {
    const c = s.charCodeAt(i);
    if (c >= 0xd800 && c <= 0xdbff) {
      const d = s.charCodeAt(i + 1);
      if (!(d >= 0xdc00 && d <= 0xdfff)) refuse('char');
      i += 1;
    } else if (!allowedChar(c)) refuse('char');
  }
  s = s.replace(/\r\n?/g, '\n');
  const n = s.length;
  let i = 0;
  const skipWs = () => {
    const from = i;
    while (i < n && isWs(s[i])) i += 1;
    return i > from;
  };
  const readName = () => {
    NAME.lastIndex = i;
    const m = NAME.exec(s);
    if (!m || m[0].length > 64) refuse('syntax');
    i += m[0].length;
    return m[0];
  };
  skipWs();
  if (s.startsWith('<?xml', i)) {
    XML_DECL.lastIndex = i;
    const m = XML_DECL.exec(s);
    if (!m) refuse('markup');
    i += m[0].length;
  }
  const stack = [];
  let root = null;
  let elements = 0;
  let textTotal = 0;
  while (i < n) {
    const lt = s.indexOf('<', i);
    const end = lt < 0 ? n : lt;
    if (end > i) {
      const raw = s.slice(i, end);
      i = end;
      if (!stack.length) {
        if (raw.trim()) refuse('syntax');
        continue;
      }
      if (raw.includes('>') && raw.includes(']]>')) refuse('syntax');
      const value = decode(raw);
      const visible = value.trim().length;
      if (visible > L.textChars) refuse('too_much_text');
      textTotal += visible;
      if (textTotal > L.totalText) refuse('too_much_text');
      stack[stack.length - 1].kids.push(value);
      continue;
    }
    if (s.startsWith('<!--', i)) {
      const close = s.indexOf('-->', i + 4);
      if (close < 0 || s.slice(i + 4, close).includes('--')) refuse('syntax');
      i = close + 3;
      continue;
    }
    if (s[i + 1] === '!' || s[i + 1] === '?') refuse('markup'); // DOCTYPE, ENTITY, CDATA, PI
    if (s[i + 1] === '/') {
      i += 2;
      const name = readName();
      skipWs();
      if (s[i] !== '>') refuse('syntax');
      i += 1;
      const top = stack.pop();
      if (!top || top.name !== name) refuse('syntax');
      if (!stack.length) root = top;
      continue;
    }
    if (root) refuse('syntax'); // a second root element
    i += 1;
    const el = { name: readName(), attrs: [], kids: [] };
    elements += 1;
    if (elements > L.elements) refuse('too_many_elements');
    if (stack.length + 1 > L.depth) refuse('too_deep');
    const seen = new Set();
    let closed = false;
    for (;;) {
      const spaced = skipWs();
      if (s[i] === '>') {
        i += 1;
        break;
      }
      if (s[i] === '/' && s[i + 1] === '>') {
        i += 2;
        closed = true;
        break;
      }
      if (!spaced || i >= n) refuse('syntax');
      const an = readName();
      skipWs();
      if (s[i] !== '=') refuse('syntax');
      i += 1;
      skipWs();
      const q = s[i];
      if (q !== '"' && q !== "'") refuse('syntax');
      const close = s.indexOf(q, i + 1);
      if (close < 0) refuse('syntax');
      const raw = s.slice(i + 1, close);
      if (raw.includes('<')) refuse('syntax');
      if (seen.has(an)) refuse('syntax');
      seen.add(an);
      if (el.attrs.length >= L.attrs) refuse('too_many_attrs');
      el.attrs.push([an, decode(raw)]);
      i = close + 1;
    }
    if (stack.length) stack[stack.length - 1].kids.push(el);
    if (closed) {
      if (!stack.length) root = el;
    } else stack.push(el);
  }
  if (stack.length || !root) refuse('syntax');
  return root;
}

// --- 2. the allow-list -----------------------------------------------------------------------

export const ELEMENTS = Object.freeze(['svg', 'g', 'defs', 'path', 'rect', 'circle', 'ellipse', 'line', 'polyline', 'polygon', 'text', 'tspan', 'linearGradient', 'radialGradient', 'stop', 'title', 'desc']);
const ELEMENT_SET = new Set(ELEMENTS);
const TEXT_PARENTS = new Set(['text', 'tspan', 'title', 'desc']);
const GRADIENTS = new Set(['linearGradient', 'radialGradient']);
const ID = /^[A-Za-z][A-Za-z0-9-]{0,63}$/;

// numbers: one hand-written scanner shared by every list grammar (linear, no backtracking).
// Separators are whitespace and at most one comma between two numbers.
function scanner(v, L, max) {
  const n = v.length;
  const sc = { i: 0, n, count: 0 };
  sc.ws = () => {
    while (sc.i < n && (v[sc.i] === ' ' || v[sc.i] === '\t' || v[sc.i] === '\n' || v[sc.i] === '\r' || v[sc.i] === '\f')) sc.i += 1;
  };
  sc.sep = () => {
    sc.ws();
    if (v[sc.i] !== ',') return false;
    sc.i += 1;
    sc.ws();
    return true;
  };
  sc.startsNumber = () => {
    const c = v[sc.i];
    return c === '-' || c === '+' || c === '.' || (c >= '0' && c <= '9');
  };
  // the next number, as written out (fmtNum), or null (nothing to read / too long / too large)
  sc.num = () => {
    const start = sc.i;
    let i = sc.i;
    if (v[i] === '+' || v[i] === '-') i += 1;
    let digits = 0;
    while (i < n && v[i] >= '0' && v[i] <= '9') {
      i += 1;
      digits += 1;
    }
    if (v[i] === '.') {
      i += 1;
      while (i < n && v[i] >= '0' && v[i] <= '9') {
        i += 1;
        digits += 1;
      }
    }
    if (!digits) return null;
    if (v[i] === 'e' || v[i] === 'E') {
      let k = i + 1;
      if (v[k] === '+' || v[k] === '-') k += 1;
      const e0 = k;
      while (k < n && v[k] >= '0' && v[k] <= '9') k += 1;
      if (k > e0) i = k;
    }
    if (i - start > 32) return null;
    const x = Number(v.slice(start, i));
    if (!Number.isFinite(x) || Math.abs(x) > L.coord) return null;
    sc.count += 1;
    if (sc.count > max) return null;
    sc.i = i;
    return x;
  };
  return sc;
}

export function fmtNum(x) {
  if (x === 0) return '0';
  const r = Math.round(x * 1e6) / 1e6;
  return String(r === 0 ? x : r);
}

// "1 2,3" -> [1, 2, 3] | null (only numbers and separators; no trailing comma)
function numberList(v, max, L) {
  const sc = scanner(v, L, max);
  const out = [];
  sc.ws();
  if (sc.i >= sc.n) return null;
  for (;;) {
    const x = sc.num();
    if (x === null) return null;
    out.push(x);
    const comma = sc.sep();
    if (sc.i >= sc.n) return comma ? null : out;
  }
}

function number(v, L) {
  const list = numberList(v, 1, L);
  return list ? list[0] : null;
}

// length: a number, optionally px or % (min: lower bound, null = none)
function length(v, L, { min = null, percent = true } = {}) {
  let t = v.trim();
  let unit = '';
  if (t.endsWith('px')) t = t.slice(0, -2);
  else if (percent && t.endsWith('%')) {
    unit = '%';
    t = t.slice(0, -1);
  }
  if (!t || t !== t.trim()) return null;
  const x = number(t, L);
  if (x === null || (min !== null && x < min)) return null;
  return fmtNum(x) + unit;
}

// path data: commands with their argument counts, arc flags as a single 0 / 1. Written out
// again from what was read ("M 10 10 L 20 20 Z"), never copied.
const ARITY = { M: 2, L: 2, T: 2, H: 1, V: 1, C: 6, S: 4, Q: 4, A: 7, Z: 0 };
const COMMANDS = 'MLTHVCSQAZmlthvcsqaz';
function pathData(v, L) {
  if (v.length > L.pathChars) refuse('path_too_long');
  const sc = scanner(v, L, Infinity);
  const parts = [];
  const flag = () => {
    const f = v[sc.i];
    if (f !== '0' && f !== '1') return null;
    sc.i += 1;
    return f;
  };
  sc.ws();
  if (sc.i >= sc.n) return null;
  while (sc.i < sc.n) {
    const c = v[sc.i];
    if (!COMMANDS.includes(c)) return null;
    const up = c.toUpperCase();
    if (!parts.length && up !== 'M') return null;
    sc.i += 1;
    sc.ws();
    const arity = ARITY[up];
    if (!arity) {
      parts.push(c);
      continue;
    }
    let groups = 0;
    for (;;) {
      const args = [];
      for (let k = 0; k < arity; k += 1) {
        if (k) sc.sep();
        let a;
        if (up === 'A' && (k === 3 || k === 4)) a = flag();
        else {
          const x = sc.num();
          a = x === null ? null : fmtNum(x);
        }
        if (a === null) return null;
        args.push(a);
      }
      if (sc.count > L.pathNumbers) refuse('path_too_long');
      parts.push(groups ? args.join(' ') : `${c} ${args.join(' ')}`);
      groups += 1;
      const comma = sc.sep();
      if (sc.i < sc.n && sc.startsNumber()) continue;
      if (comma) return null;
      break;
    }
  }
  return parts.join(' ');
}

// translate(x [y]) scale(x [y]) rotate(a [cx cy]) — nothing else
const TRANSFORMS = { translate: [1, 2], scale: [1, 2], rotate: [1, 3] };
function transform(v, L) {
  const out = [];
  let i = 0;
  const n = v.length;
  const ws = () => {
    while (i < n && (v[i] === ' ' || v[i] === '\t' || v[i] === '\n' || v[i] === '\r')) i += 1;
  };
  ws();
  while (i < n) {
    if (out.length >= 16) return null;
    let j = i;
    while (j < n && v[j] >= 'a' && v[j] <= 'z') j += 1;
    const name = v.slice(i, j);
    if (!Object.prototype.hasOwnProperty.call(TRANSFORMS, name)) return null;
    i = j;
    ws();
    if (v[i] !== '(') return null;
    const close = v.indexOf(')', i);
    if (close < 0) return null;
    const args = numberList(v.slice(i + 1, close), 3, L);
    if (!args || !TRANSFORMS[name].includes(args.length)) return null;
    out.push(`${name}(${args.map(fmtNum).join(' ')})`);
    i = close + 1;
    ws();
    if (v[i] === ',') {
      i += 1;
      ws();
      if (i >= n) return null;
    }
  }
  return out.length ? out.join(' ') : null;
}

// CSS named colours (CSS Color 4), lower case
const NAMED = new Set(('aliceblue antiquewhite aqua aquamarine azure beige bisque black blanchedalmond blue blueviolet brown burlywood cadetblue chartreuse chocolate coral cornflowerblue cornsilk crimson cyan darkblue darkcyan darkgoldenrod darkgray darkgreen darkgrey darkkhaki darkmagenta darkolivegreen darkorange darkorchid darkred darksalmon darkseagreen darkslateblue darkslategray darkslategrey darkturquoise darkviolet deeppink deepskyblue dimgray dimgrey dodgerblue firebrick floralwhite forestgreen fuchsia gainsboro ghostwhite gold goldenrod gray green greenyellow grey honeydew hotpink indianred indigo ivory khaki lavender lavenderblush lawngreen lemonchiffon lightblue lightcoral lightcyan lightgoldenrodyellow lightgray lightgreen lightgrey lightpink lightsalmon lightseagreen lightskyblue lightslategray lightslategrey lightsteelblue lightyellow lime limegreen linen magenta maroon mediumaquamarine mediumblue mediumorchid mediumpurple mediumseagreen mediumslateblue mediumspringgreen mediumturquoise mediumvioletred midnightblue mintcream mistyrose moccasin navajowhite navy oldlace olive olivedrab orange orangered orchid palegoldenrod palegreen paleturquoise palevioletred papayawhip peachpuff peru pink plum powderblue purple rebeccapurple red rosybrown royalblue saddlebrown salmon sandybrown seagreen seashell sienna silver skyblue slateblue slategray slategrey snow springgreen steelblue tan teal thistle tomato turquoise violet wheat white whitesmoke yellow yellowgreen').split(' '));
const lower = (s) => s.replace(/[A-Z]/g, (c) => c.toLowerCase()); // ASCII only (no Unicode folding)
const RGB = /^rgb\( *(\d{1,3}) *, *(\d{1,3}) *, *(\d{1,3}) *\)$/;
function color(v) {
  const t = lower(v.trim());
  if (t === 'none' || t === 'transparent' || NAMED.has(t)) return t;
  if (/^#[0-9a-f]{3}$/.test(t) || /^#[0-9a-f]{6}$/.test(t)) return t;
  const m = RGB.exec(t);
  if (m && [m[1], m[2], m[3]].every((x) => Number(x) <= 255)) return `rgb(${Number(m[1])},${Number(m[2])},${Number(m[3])})`;
  return null;
}
// a paint: a colour or url(#id) (the id is checked against the picture's gradients later)
const URL_REF = /^url\(#([A-Za-z][A-Za-z0-9-]{0,63})\)$/;
function paint(v) {
  const m = URL_REF.exec(v.trim());
  if (m) return `url(#${m[1]})`;
  return color(v);
}
function fraction(v, L) {
  const t = v.trim();
  const pct = t.endsWith('%');
  const x = number(pct ? t.slice(0, -1) : t, L);
  if (x === null) return null;
  const f = pct ? x / 100 : x;
  return f >= 0 && f <= 1 ? fmtNum(f) : null;
}
const keyword = (...words) => {
  const set = new Set(words);
  return (v) => (set.has(v.trim()) ? v.trim() : null);
};
const GENERIC_FONTS = new Set(['serif', 'sans-serif', 'monospace', 'cursive', 'fantasy', 'system-ui']);
function fontFamily(v) {
  const kept = [];
  for (const part of v.split(',')) {
    let f = lower(part.trim());
    if ((f.startsWith('"') && f.endsWith('"')) || (f.startsWith("'") && f.endsWith("'"))) f = f.slice(1, -1).trim();
    if (GENERIC_FONTS.has(f) && !kept.includes(f)) kept.push(f);
  }
  return kept.length ? kept.join(', ') : null;
}
function label(v) {
  const t = v.replace(/\s+/g, ' ').trim();
  return t && t.length <= 200 ? t : null;
}
function viewBox(v, L) {
  const list = numberList(v, 4, L);
  if (!list || list.length !== 4) return null;
  if (!(list[2] > 0 && list[3] > 0)) return null;
  if (list[2] > L.viewBox || list[3] > L.viewBox) refuse('too_big');
  return list.map(fmtNum).join(' ');
}
function numbers(v, L, { max, even = false, min = null }) {
  const list = numberList(v, max, L);
  if (!list || (even && list.length % 2) || (min !== null && list.some((x) => x < min))) return null;
  return list.map(fmtNum).join(' ');
}

// attribute -> value grammar (returns the value to write, or null = left out)
const ATTRS = new Map([
  ['id', (v) => (ID.test(v.trim()) ? v.trim() : null)],
  ['d', pathData],
  ['x', (v, L) => length(v, L)],
  ['y', (v, L) => length(v, L)],
  ['dx', (v, L) => length(v, L)],
  ['dy', (v, L) => length(v, L)],
  ['cx', (v, L) => length(v, L)],
  ['cy', (v, L) => length(v, L)],
  ['r', (v, L) => length(v, L, { min: 0 })],
  ['rx', (v, L) => length(v, L, { min: 0 })],
  ['ry', (v, L) => length(v, L, { min: 0 })],
  ['x1', (v, L) => length(v, L)],
  ['y1', (v, L) => length(v, L)],
  ['x2', (v, L) => length(v, L)],
  ['y2', (v, L) => length(v, L)],
  ['width', (v, L) => length(v, L, { min: 0 })],
  ['height', (v, L) => length(v, L, { min: 0 })],
  ['points', (v, L) => numbers(v, L, { max: L.points, even: true })],
  ['viewBox', viewBox],
  ['fill', paint],
  ['stroke', paint],
  ['stroke-width', (v, L) => length(v, L, { min: 0 })],
  ['stroke-linecap', keyword('butt', 'round', 'square')],
  ['stroke-linejoin', keyword('miter', 'round', 'bevel')],
  ['stroke-dasharray', (v, L) => (v.trim() === 'none' ? 'none' : numbers(v, L, { max: 16, min: 0 }))],
  ['opacity', fraction],
  ['fill-opacity', fraction],
  ['stroke-opacity', fraction],
  ['fill-rule', keyword('nonzero', 'evenodd')],
  ['stop-color', color],
  ['stop-opacity', fraction],
  ['offset', fraction],
  ['gradientUnits', keyword('userSpaceOnUse', 'objectBoundingBox')],
  ['transform', transform],
  ['text-anchor', keyword('start', 'middle', 'end')],
  ['dominant-baseline', keyword('auto', 'middle', 'central', 'hanging', 'alphabetic', 'mathematical', 'ideographic', 'text-top', 'text-bottom')],
  ['font-size', (v, L) => length(v, L, { min: 0, percent: false })],
  ['font-family', fontFamily],
  ['font-weight', keyword('normal', 'bold', 'bolder', 'lighter', '100', '200', '300', '400', '500', '600', '700', '800', '900')],
  ['font-style', keyword('normal', 'italic', 'oblique')],
  ['aria-label', label],
  ['role', keyword('img')],
]);
export const ATTRIBUTES = Object.freeze([...ATTRS.keys()]);
// one attribute value through its grammar: the value written out, or null (left out)
export function attributeValue(name, value, L = SVG_LIMITS) {
  const spec = ATTRS.get(name);
  if (!spec) return null;
  try {
    const v = spec(String(value), L);
    return v === undefined ? null : v;
  } catch {
    return null;
  }
}
const PAINTS = new Set(['fill', 'stroke']);

// a name for the note (never markup: names come from the NAME grammar, cut short)
const shownName = (name) => String(name).slice(0, 40);

function rebuild(root, L) {
  if (root.name !== 'svg') refuse('not_svg');
  const removed = new Set();
  const ids = new Set();
  const gradients = new Set();
  const refs = []; // [node, index] of url(#id) values
  const nsOk = (el) => {
    const ns = el.attrs.find(([k]) => k === 'xmlns');
    return !ns || ns[1] === SVG_NS;
  };
  if (!nsOk(root)) refuse('not_svg');
  const build = (el) => {
    const out = { name: el.name, attrs: [], kids: [] };
    for (const [k, v] of el.attrs) {
      if (k === 'xmlns') continue; // checked by nsOk; the root's is written below
      if (k.startsWith('xmlns:')) {
        if (!(k === 'xmlns:xlink' && v === XLINK_NS)) removed.add(shownName(k));
        continue;
      }
      const spec = ATTRS.get(k);
      const value = spec ? spec(v, L) : null;
      if (value === null || value === undefined) {
        removed.add(shownName(k));
        continue;
      }
      if (k === 'viewBox' && el !== root && el.name !== 'svg') {
        removed.add(k);
        continue;
      }
      if (k === 'id') {
        if (ids.has(value)) {
          removed.add('id');
          continue;
        }
        ids.add(value);
        if (GRADIENTS.has(el.name)) gradients.add(value);
      }
      if (PAINTS.has(k) && value.startsWith('url(')) refs.push([out, out.attrs.length]);
      out.attrs.push([k, value]);
    }
    for (const kid of el.kids) {
      if (typeof kid === 'string') {
        if (!TEXT_PARENTS.has(el.name) || !kid) continue;
        const last = out.kids[out.kids.length - 1];
        if (typeof last === 'string') out.kids[out.kids.length - 1] = last + kid;
        else out.kids.push(kid);
        continue;
      }
      if (!ELEMENT_SET.has(kid.name) || !nsOk(kid)) {
        removed.add(shownName(kid.name));
        continue;
      }
      out.kids.push(build(kid));
    }
    return out;
  };
  const tree = build(root);
  // url(#id) only to a gradient of this picture
  const drop = new Map();
  for (const [node, index] of refs) {
    const id = node.attrs[index][1].slice(5, -1);
    if (!gradients.has(id)) {
      if (!drop.has(node)) drop.set(node, new Set());
      drop.get(node).add(index);
      removed.add(node.attrs[index][0]);
    }
  }
  for (const [node, set] of drop) node.attrs = node.attrs.filter((_, k) => !set.has(k));
  // the size: width / height in px, else from the viewBox
  const get = (k) => (tree.attrs.find(([a]) => a === k) || [])[1];
  const px = (v) => (v && !v.endsWith('%') ? Number(v) : null);
  const vb = get('viewBox') ? get('viewBox').split(' ').map(Number) : null;
  let w = px(get('width'));
  let h = px(get('height'));
  if (w !== null && w <= 0) w = null;
  if (h !== null && h <= 0) h = null;
  if (vb && w === null && h !== null) w = (h * vb[2]) / vb[3];
  if (vb && h === null && w !== null) h = (w * vb[3]) / vb[2];
  if (w === null || h === null) {
    if (vb) {
      const k = Math.min(1, L.shown / Math.max(vb[2], vb[3]));
      w = vb[2] * k;
      h = vb[3] * k;
    } else {
      w = 300;
      h = 150;
    }
  }
  if (w > L.size || h > L.size) refuse('too_big');
  w = Math.max(1, Math.round(w));
  h = Math.max(1, Math.round(h));
  tree.attrs = [['xmlns', SVG_NS], ['width', String(w)], ['height', String(h)], ...tree.attrs.filter(([a]) => a !== 'width' && a !== 'height')];
  return { tree, removed: [...removed].sort(), width: w, height: h };
}

// --- 3. writing out and the audit ------------------------------------------------------------

// \r as a reference: a raw CR would be read back as \n (XML line-end normalisation)
const escText = (s) => s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/\r/g, '&#13;');
const escAttr = (s) => escText(s).replace(/"/g, '&quot;').replace(/'/g, '&apos;').replace(/\t/g, '&#9;').replace(/\n/g, '&#10;').replace(/\r/g, '&#13;');

export function serialize(node) {
  if (typeof node === 'string') return escText(node);
  let out = `<${node.name}`;
  for (const [k, v] of node.attrs) out += ` ${k}="${escAttr(v)}"`;
  if (!node.kids.length) return `${out}/>`;
  return `${out}>${node.kids.map(serialize).join('')}</${node.name}>`;
}

const sameTree = (a, b) => {
  if (typeof a === 'string' || typeof b === 'string') return a === b;
  if (a.name !== b.name || a.attrs.length !== b.attrs.length || a.kids.length !== b.kids.length) return false;
  for (let k = 0; k < a.attrs.length; k += 1) if (a.attrs[k][0] !== b.attrs[k][0] || a.attrs[k][1] !== b.attrs[k][1]) return false;
  for (let k = 0; k < a.kids.length; k += 1) if (!sameTree(a.kids[k], b.kids[k])) return false;
  return true;
};

// svg text -> the list of what is not on the allow-list (empty = clean). Used on every picture
// before it is shown, and by the tests on what the <img> holds.
export function auditSvg(svg, L = SVG_LIMITS) {
  const problems = [];
  let root;
  try {
    root = parseXml(svg, L);
  } catch (e) {
    return [`unreadable:${e.reason || 'error'}`];
  }
  const walk = (el, isRoot) => {
    if (!ELEMENT_SET.has(el.name)) problems.push(`element:${el.name}`);
    for (const [k, v] of el.attrs) {
      if (k === 'xmlns') {
        if (!isRoot || v !== SVG_NS) problems.push(`xmlns:${v}`);
        continue;
      }
      const spec = ATTRS.get(k);
      if (!spec) {
        problems.push(`attr:${el.name}.${k}`);
        continue;
      }
      let again = null;
      try {
        again = spec(v, L);
      } catch {
        again = null;
      }
      if (again !== v) problems.push(`value:${el.name}.${k}`);
      if (PAINTS.has(k) && v.startsWith('url(') && !/^url\(#[A-Za-z][A-Za-z0-9-]{0,63}\)$/.test(v)) problems.push(`url:${el.name}.${k}`);
    }
    for (const kid of el.kids) {
      if (typeof kid === 'string') {
        if (!TEXT_PARENTS.has(el.name)) problems.push(`text:${el.name}`);
      } else walk(kid, false);
    }
  };
  walk(root, true);
  if (root.name !== 'svg' || !root.attrs.some(([k, v]) => k === 'xmlns' && v === SVG_NS)) problems.push('root');
  return problems;
}

// The browser's own reading of the output (DOMParser, image/svg+xml: a separate document that
// runs nothing and loads nothing) must be exactly our tree.
function domTree(el, isRoot) {
  if (el.namespaceURI !== SVG_NS || el.prefix) return null;
  const out = { name: el.localName, attrs: [], kids: [] };
  for (const a of Array.from(el.attributes)) {
    if (a.name === 'xmlns' && isRoot) {
      out.attrs.push(['xmlns', a.value]);
      continue;
    }
    if (a.namespaceURI !== null || a.prefix) return null;
    out.attrs.push([a.name, a.value]);
  }
  for (const node of Array.from(el.childNodes)) {
    if (node.nodeType === 3) {
      const last = out.kids[out.kids.length - 1];
      if (typeof last === 'string') out.kids[out.kids.length - 1] = last + node.data;
      else out.kids.push(node.data);
    } else if (node.nodeType === 1) {
      const kid = domTree(node, false);
      if (!kid) return null;
      out.kids.push(kid);
    } else return null; // comment, CDATA, processing instruction
  }
  return out;
}
export function domMatches(DOMParserCtor, svg, tree) {
  try {
    const doc = new DOMParserCtor().parseFromString(svg, 'image/svg+xml');
    if (doc.getElementsByTagName('parsererror').length) return false;
    if (doc.getElementsByTagNameNS('*', 'parsererror').length) return false;
    if (doc.doctype) return false;
    const got = domTree(doc.documentElement, true);
    return Boolean(got) && sameTree(got, tree);
  } catch {
    return false;
  }
}

// --- the entry point -------------------------------------------------------------------------

const firstTitle = (tree) => {
  const title = tree.kids.find((k) => typeof k !== 'string' && k.name === 'title');
  if (!title) return null;
  return label(title.kids.filter((k) => typeof k === 'string').join(''));
};

// text (a ```svg block) ->
//   { ok: true, svg, src, width, height, alt (null = none given), removed: [names] }
//   { ok: false, reason }   reason: too_long | char | markup | syntax | not_svg |
//     too_many_elements | too_many_attrs | too_deep | too_much_text | path_too_long | too_big | empty | check_failed
// opts: { domParser: the DOMParser constructor (the page's), limits }
export function svgImage(text, opts = {}) {
  const L = opts.limits || SVG_LIMITS;
  try {
    const root = parseXml(text, L);
    const { tree, removed, width, height } = rebuild(root, L);
    if (!tree.kids.some((k) => typeof k !== 'string' && k.name !== 'title' && k.name !== 'desc')) return { ok: false, reason: 'empty' };
    const svg = serialize(tree);
    // the audit: read back, nothing outside the allow-list, the same tree
    let back = null;
    try {
      back = parseXml(svg, { ...L, bytes: Math.max(L.bytes, svg.length * 4) });
    } catch {
      back = null;
    }
    if (!back || !sameTree(back, tree) || auditSvg(svg, { ...L, bytes: Math.max(L.bytes, svg.length * 4) }).length) return { ok: false, reason: 'check_failed' };
    if (opts.domParser && !domMatches(opts.domParser, svg, tree)) return { ok: false, reason: 'check_failed' };
    return { ok: true, svg, src: SVG_SRC_PREFIX + encodeURIComponent(svg), width, height, alt: firstTitle(tree) || (tree.attrs.find(([k]) => k === 'aria-label') || [])[1] || null, removed };
  } catch (e) {
    if (e instanceof Refused) return { ok: false, reason: e.reason };
    return { ok: false, reason: 'check_failed' };
  }
}
