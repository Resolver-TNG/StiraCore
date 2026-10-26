// #453: static SVG on the board — the strict reader, the allow-list rebuild, the audit and the
// board renderer's use of it. The attack cases are fixed here; every picture that is shown is
// read back by an independent scanner in this file (not svg_safe's own reader) and must hold
// nothing outside the allow-list. node --test tests/stage_svg_test.cjs
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const ROOT = path.join(__dirname, '..');
const STAGE = path.join(ROOT, 'dashboard', 'stage', 'js');
const load = (name) => import(path.join(STAGE, name));
const ja = JSON.parse(fs.readFileSync(path.join(ROOT, 'dashboard/static/i18n/catalog.ja.json'), 'utf8'));
const en = JSON.parse(fs.readFileSync(path.join(ROOT, 'dashboard/static/i18n/catalog.en.json'), 'utf8'));
const t = (key, vars = {}) => String(ja[key] ?? key).replace(/\{(\w+)\}/g, (_, k) => String(vars[k]));

const NS = 'xmlns="http://www.w3.org/2000/svg"';
const svg = (body, attrs = '') => `<svg ${NS} viewBox="0 0 100 100"${attrs ? ` ${attrs}` : ''}>${body}</svg>`;

// --- an independent reading of what the <img> holds -------------------------------------------
// Written for this test only (no code shared with svg_safe.js): the allow-list as the issue
// states it (plus the few keyword attributes svg_safe adds), every tag and attribute of the
// output, and the strings that must never appear.
const ALLOWED_ELEMENTS = new Set(['svg', 'g', 'defs', 'path', 'rect', 'circle', 'ellipse', 'line', 'polyline', 'polygon', 'text', 'tspan', 'linearGradient', 'radialGradient', 'stop', 'title', 'desc']);
const ALLOWED_ATTRS = new Set(['xmlns', 'id', 'd', 'x', 'y', 'dx', 'dy', 'cx', 'cy', 'r', 'rx', 'ry', 'x1', 'y1', 'x2', 'y2', 'width', 'height', 'points', 'viewBox', 'fill', 'stroke', 'stroke-width', 'stroke-linecap', 'stroke-linejoin', 'stroke-dasharray', 'opacity', 'fill-opacity', 'stroke-opacity', 'fill-rule', 'stop-color', 'stop-opacity', 'offset', 'gradientUnits', 'transform', 'text-anchor', 'dominant-baseline', 'font-size', 'font-family', 'font-weight', 'font-style', 'aria-label', 'role']);
function independentScan(out) {
  const problems = [];
  // only text between tags; inside a tag only name="value" pairs
  const tag = /<(\/?)([^\s/>]*)([^>]*)>/g;
  let m;
  let last = 0;
  while ((m = tag.exec(out))) {
    const between = out.slice(last, m.index);
    if (/[<>]/.test(between)) problems.push(`raw markup in text: ${between}`);
    last = tag.lastIndex;
    const [, close, name, rest] = m;
    if (!ALLOWED_ELEMENTS.has(name)) problems.push(`element ${name}`);
    if (close) {
      if (rest.trim()) problems.push(`attributes on a closing tag ${name}`);
      continue;
    }
    const body = rest.endsWith('/') ? rest.slice(0, -1) : rest;
    const attr = /\s([^\s="]+)="([^"]*)"/g;
    let a;
    let consumed = '';
    while ((a = attr.exec(body))) {
      consumed += a[0];
      const [, an, av] = a;
      if (!ALLOWED_ATTRS.has(an)) problems.push(`attribute ${name}.${an}`);
      if (an === 'xmlns' && (name !== 'svg' || av !== 'http://www.w3.org/2000/svg')) problems.push(`xmlns ${av}`);
      if (/url\(/i.test(av) && !/^url\(#[A-Za-z][A-Za-z0-9-]*\)$/.test(av)) problems.push(`url ${an}=${av}`);
      if (/[<>]/.test(av)) problems.push(`raw < > in ${an}`);
    }
    if (consumed.length !== body.length) problems.push(`unparsed tag text in ${name}: ${body.slice(consumed.length)}`);
  }
  if (/[<>]/.test(out.slice(last))) problems.push('raw markup after the root');
  // the markup itself (tags and attribute names: values and text blanked) never names anything
  // that runs, links or loads; values other than the free-text label never hold a scheme
  const skeleton = out.replace(/"[^"]*"/g, '""').replace(/>[^<]*</g, '><').toLowerCase();
  for (const bad of ['script', 'href', 'on', 'style', 'foreignobject', '<!', '<?', 'xmlns:', 'xlink', 'cdata', 'use', 'image', 'animate', 'set']) {
    const words = skeleton.match(/[a-z!?:-]+/g) || [];
    if (bad === 'on' ? words.some((w) => w.startsWith('on')) : words.some((w) => w === bad || (bad.length > 5 && w.includes(bad))) || (bad.startsWith('<') && skeleton.includes(bad))) problems.push(`markup names ${bad}`);
  }
  for (const [, an, av] of out.matchAll(/\s([^\s="]+)="([^"]*)"/g)) {
    if (an === 'aria-label' || an === 'xmlns') continue;
    if (/javascript:|data:|https?:|\/\/|@import|expression\(/i.test(av)) problems.push(`scheme in ${an}=${av}`);
  }
  return problems;
}

async function draw(text, opts) {
  const { svgImage, auditSvg } = await load('svg_safe.js');
  const r = svgImage(text, opts);
  if (r.ok) {
    assert.deepEqual(independentScan(r.svg), [], `independent scan of ${r.svg}`);
    assert.deepEqual(auditSvg(r.svg), [], `audit of ${r.svg}`);
    assert.ok(r.src.startsWith('data:image/svg+xml;charset=utf-8,'));
    assert.equal(decodeURIComponent(r.src.slice('data:image/svg+xml;charset=utf-8,'.length)), r.svg, 'the <img> shows exactly the audited SVG');
  }
  return r;
}

// [name, input, expected]: 'refused:<reason>' (stays code) or { removed: [...], has: [...], not: [...] }
const ATTACKS = [
  ['script element', svg('<script>alert(1)</script><rect width="10" height="10"/>'), { removed: ['script'], not: ['alert'] }],
  ['script, upper case (not an SVG element)', svg('<SCRIPT>alert(1)</SCRIPT><rect width="1" height="1"/>'), { removed: ['SCRIPT'], not: ['alert'] }],
  ['onload on the root and an element', `<svg ${NS} onload="alert(1)" viewBox="0 0 9 9"><rect onclick="x()" OnMouseOver="y()" width="9" height="9"/></svg>`, { removed: ['OnMouseOver', 'onclick', 'onload'] }],
  ['foreignObject with HTML', svg('<foreignObject width="9" height="9"><iframe xmlns="http://www.w3.org/1999/xhtml" src="https://evil.example/"></iframe></foreignObject><circle r="3"/>'), { removed: ['foreignObject'], not: ['iframe', 'evil'] }],
  ['use href to a fragment', svg('<defs><g id="a"><rect width="1" height="1"/></g></defs><use href="#a"/><use xlink:href="#a"/>'), { removed: ['use'] }],
  ['image with an external href', svg('<image href="https://evil.example/x.png" width="9" height="9"/><image xlink:href="data:image/png;base64,AAAA"/><rect width="1" height="1"/>'), { removed: ['image'], not: ['evil'] }],
  ['a link', svg('<a href="javascript:alert(1)"><text x="1" y="9">click</text></a><rect width="1" height="1"/>'), { removed: ['a'], not: ['click'] }],
  ['href / xlink:href on allowed elements', svg('<rect href="javascript:alert(1)" xlink:href="https://evil.example/" width="1" height="1"/>', 'xmlns:xlink="http://www.w3.org/1999/xlink"'), { removed: ['href', 'xlink:href'] }],
  ['style element with url() and @import', svg('<style>@import url(https://evil.example/a.css); rect { fill: url(https://evil.example/p.svg#x) }</style><rect width="1" height="1"/>'), { removed: ['style'], not: ['evil', '@import'] }],
  ['style attribute with url()', svg('<rect style="fill:url(https://evil.example/#a);background:url(javascript:alert(1))" width="1" height="1"/>'), { removed: ['style'] }],
  ['class attribute', svg('<rect class="x" width="1" height="1"/>'), { removed: ['class'] }],
  ['javascript: in a paint', svg('<rect fill="javascript:alert(1)" stroke="url(javascript:alert(1))" width="1" height="1"/>'), { removed: ['fill', 'stroke'] }],
  ['external url() in a paint', svg('<rect fill="url(https://evil.example/p.svg#g)" width="1" height="1"/>'), { removed: ['fill'] }],
  ['url() to an id that is not a gradient here', svg('<defs><g id="g1"><rect width="1" height="1"/></g></defs><rect fill="url(#g1)" stroke="url(#nowhere)" width="1" height="1"/>'), { removed: ['fill', 'stroke'] }],
  ['url( obfuscated: spaces, quotes, case, CSS escape, entity-built scheme', svg('<linearGradient id="a"><stop offset="0" stop-color="red"/></linearGradient><rect fill="url( #a)" width="1" height="1"/><rect fill="URL(#a)" width="1" height="1"/><rect fill="url(\'#a\')" width="1" height="1"/><rect fill="url(\\23 a)" width="1" height="1"/><rect fill="u&#114;l(http://evil.example/#a)" width="1" height="1"/><rect fill="url(#a) " width="1" height="1"/>'), { removed: ['fill'], has: ['fill="url(#a)"'], not: ['evil'] }],
  ['url(&#x23;a): a numeric reference decodes to an allowed same-picture reference', svg('<radialGradient id="a"><stop offset="1" stop-color="#fff"/></radialGradient><circle r="5" fill="url(&#x23;a)"/>'), { removed: [], has: ['fill="url(#a)"'] }],
  ['external font: @font-face, a font url, a named font', svg('<style>@font-face{font-family:x;src:url(https://evil.example/f.woff)}</style><text x="1" y="9" font-family="url(https://evil.example/f.woff)">a</text><text x="1" y="9" font-family="Evil Font">b</text><text x="1" y="9" font-family="Arial, \'sans-serif\'">c</text>'), { removed: ['font-family', 'style'], has: ['font-family="sans-serif"'], not: ['evil'] }],
  ['SMIL: set / animate rewriting a link or a paint', svg('<rect width="9" height="9"><set attributeName="fill" to="url(https://evil.example/#x)"/><animate attributeName="href" to="javascript:alert(1)" begin="0s"/><animateTransform attributeName="transform" type="rotate"/><animateMotion path="M0 0"/></rect>'), { removed: ['animate', 'animateMotion', 'animateTransform', 'set'], not: ['evil'] }],
  ['filter, mask, clipPath, pattern, marker, switch, iframe, object, embed', svg('<filter id="f"><feImage href="https://evil.example/"/></filter><mask id="m"/><clipPath id="c"/><pattern id="p"/><marker id="k"/><switch><rect/></switch><iframe/><object/><embed/><rect width="1" height="1"/>'), { removed: ['clipPath', 'embed', 'filter', 'iframe', 'marker', 'mask', 'object', 'pattern', 'switch'], not: ['evil'] }],
  ['DOCTYPE with an ENTITY (XXE)', `<!DOCTYPE svg [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>${svg('<text x="1" y="9">&xxe;</text>')}`, 'refused:markup'],
  ['billion laughs', `<?xml version="1.0"?><!DOCTYPE l [<!ENTITY a "aaaa"><!ENTITY b "&a;&a;&a;&a;">]>${svg('<text>&b;</text>')}`, 'refused:markup'],
  ['an undeclared named entity', svg('<text x="1" y="9">&nbsp;</text>'), 'refused:markup'],
  ['CDATA', svg('<text><![CDATA[<script>alert(1)</script>]]></text>'), 'refused:markup'],
  ['a processing instruction (stylesheet)', `<?xml-stylesheet href="https://evil.example/a.css"?>${svg('<rect/>')}`, 'refused:markup'],
  ['a PI inside the root', svg('<?php echo 1 ?><rect/>'), 'refused:markup'],
  ['the XML declaration alone is fine', `<?xml version="1.0" encoding="UTF-8"?>\n${svg('<rect width="1" height="1"/>')}`, { removed: [] }],
  ['nesting deeper than 8', svg('<g><g><g><g><g><g><g><g><rect/></g></g></g></g></g></g></g></g>'), 'refused:too_deep'],
  ['nesting of exactly 8 is fine', svg('<g><g><g><g><g><g><rect width="1" height="1"/></g></g></g></g></g></g>'), { removed: [] }],
  ['more than 500 elements', svg('<rect width="1" height="1"/>'.repeat(500)), 'refused:too_many_elements'],
  ['more than 40 attributes on one element: its own reason', svg(`<rect ${Array.from({ length: 41 }, (_, k) => `a${k}="1"`).join(' ')}/>`), 'refused:too_many_attrs'],
  ['40 attributes on one element are read (the unknown ones are left out)', svg(`<rect width="1" height="1" ${Array.from({ length: 38 }, (_, k) => `a${k}="1"`).join(' ')}/>`), { removed: Array.from({ length: 38 }, (_, k) => `a${k}`) }],
  ['a carriage return in text (&#13;) is written back as &#13;, not refused', svg('<text x="1" y="9">a&#13;b&#13;&#10;c</text>'), { removed: [], has: ['>a&#13;b&#13;\nc</text>'] }],
  ['more than 32 KB', svg(`<desc>${'x'.repeat(33 * 1024)}</desc><rect/>`), 'refused:too_long'],
  ['a very long text run', svg(`<text x="1" y="9">${'あ'.repeat(501)}</text>`), 'refused:too_much_text'],
  ['a very long path', svg(`<path d="M0 0${' L1 1'.repeat(1700)}"/>`), 'refused:path_too_long'],
  ['too many numbers in one path', svg(`<path d="M0 0${' 1 1'.repeat(2001)}"/>`), 'refused:path_too_long'],
  ['too large to show', `<svg ${NS} width="9000" height="10"><rect width="1" height="1"/></svg>`, 'refused:too_big'],
  ['a viewBox too large', `<svg ${NS} viewBox="0 0 200000 10"><rect width="1" height="1"/></svg>`, 'refused:too_big'],
  ['another default namespace on the root', '<svg xmlns="http://www.w3.org/1999/xhtml"><rect width="1" height="1"/></svg>', 'refused:not_svg'],
  ['a namespace added for prefixed elements and attributes', `<svg ${NS} xmlns:x="https://evil.example/ns" xmlns:xlink="http://www.w3.org/1999/xlink"><x:script>alert(1)</x:script><rect x:onload="alert(1)" xml:space="preserve" width="1" height="1"/></svg>`, { removed: ['x:onload', 'x:script', 'xml:space', 'xmlns:x'], not: ['alert'] }],
  ['a default namespace switched on a child', svg('<g xmlns="http://www.w3.org/1999/xhtml"><script>alert(1)</script></g><rect width="1" height="1"/>'), { removed: ['g'], not: ['alert'] }],
  ['svg prefixed (svg:script)', `<svg ${NS} xmlns:svg="http://www.w3.org/2000/svg"><svg:script>alert(1)</svg:script><rect width="1" height="1"/></svg>`, { removed: ['svg:script', 'xmlns:svg'], not: ['alert'] }],
  ['not an svg root', '<html><svg/></html>', 'refused:not_svg'],
  ['Svg (case) is not svg', '<Svg xmlns="http://www.w3.org/2000/svg"><rect/></Svg>', 'refused:not_svg'],
  ['two roots', `${svg('<rect/>')}${svg('<rect/>')}`, 'refused:syntax'],
  ['unclosed', `<svg ${NS}><rect>`, 'refused:syntax'],
  ['text before the root', `hello ${svg('<rect/>')}`, 'refused:syntax'],
  ['an unquoted attribute', `<svg ${NS}><rect width=1/></svg>`, 'refused:syntax'],
  ['a duplicate attribute', `<svg ${NS}><rect width="1" width="2"/></svg>`, 'refused:syntax'],
  ['< inside an attribute value', `<svg ${NS}><rect aria-label="<script>"/></svg>`, 'refused:syntax'],
  ['a NUL in the input', svg('<rect width="1" height="1"/>\u0000'), 'refused:char'],
  ['a control character in an element name', `<svg ${NS}><scr\u0001ipt/></svg>`, 'refused:char'],
  ['a C1 control character', svg('<text>\u0085</text>'), 'refused:char'],
  ['a lone surrogate', svg('<text>\ud800</text>'), 'refused:char'],
  ['a numeric reference to a control character', svg('<text>&#1;</text>'), 'refused:char'],
  ['full-width letters in a tag name (no Unicode folding)', `<svg ${NS}><ｓｃｒｉｐｔ/></svg>`, 'refused:syntax'],
  ['NFKC look-alikes in an attribute name', svg('<rect ｏｎload="alert(1)" width="1" height="1"/>'), 'refused:syntax'],
  ['a zero-width space in an attribute name', svg('<rect on\u200bload="x" width="1" height="1"/>'), 'refused:syntax'],
  ['attribute escape: quotes and & in aria-label, < > & " in text', svg('<title>a "b" &amp; &lt;script&gt;</title><rect aria-label="x&quot; onload=&quot;alert(1)&quot; &amp;lt;" width="1" height="1"/><text x="1" y="9">&lt;/text&gt;&lt;script&gt;alert(1)&lt;/script&gt;</text>'), { removed: [], has: ['aria-label="x&quot; onload=&quot;alert(1)&quot; &amp;lt;"', '&lt;/text&gt;&lt;script&gt;'] }],
  ['attribute escape via newlines and tabs', svg('<rect aria-label="a&#10;b&#9;c" width="1" height="1"/>'), { removed: [], has: ['aria-label="a b c"'] }],
  ['path data: a scheme, a huge number, an odd count, a stray comma, an unknown command', svg('<path d="M0 0 L10 10 javascript:alert(1)"/><path d="M0 0 L 1e999 0"/><path d="M0 0 L10"/><path d="M0,0,L1,1"/><path d="M0 0 X1 1"/><path d="L1 1"/><path d="M0 0 L1 1"/>'), { removed: ['d'], has: ['d="M 0 0 L 1 1"'] }],
  ['path data written out again, not copied (arc flags packed)', svg('<path d="M10,10a5 5 0 011 1h-.5e1Z"/>'), { removed: [], has: ['d="M 10 10 a 5 5 0 0 1 1 1 h -5 Z"'] }],
  ['transform: only translate / scale / rotate with numbers', svg('<g transform="translate(10,20) rotate(45 50 50)"><rect width="1" height="1"/></g><g transform="matrix(1 0 0 1 0 0)"><rect width="1" height="1"/></g><g transform="scale(2) skewX(3)"><rect width="1" height="1"/></g><g transform="translate(1e7 0)"><rect width="1" height="1"/></g><g transform="translate(javascript:1)"><rect width="1" height="1"/></g>'), { removed: ['transform'], has: ['transform="translate(10 20) rotate(45 50 50)"'] }],
  ['colours: exact grammar only', svg('<rect fill="#ABC" width="1" height="1"/><rect fill="RGB( 1, 2 ,255 )" width="1" height="1"/><rect fill="rgb(300,0,0)" width="1" height="1"/><rect fill="#abcd" width="1" height="1"/><rect fill="hsl(0,0%,0%)" width="1" height="1"/><rect fill="Red" width="1" height="1"/><rect fill="currentColor" width="1" height="1"/><rect fill="r\u0435d" width="1" height="1"/>'), { removed: ['fill'], has: ['fill="#abc"', 'fill="rgb(1,2,255)"', 'fill="red"'] }],
  ['ids: letters, digits and hyphens; duplicates dropped', svg('<linearGradient id="ok-1"/><linearGradient id="a b"/><linearGradient id="ok-1"/><linearGradient id="1x"/><rect width="1" height="1"/>'), { removed: ['id'], has: ['id="ok-1"'] }],
  ['role other than img, an over-long label', svg(`<rect role="button" aria-label="${'x'.repeat(201)}" width="1" height="1"/>`), { removed: ['aria-label', 'role'] }],
  ['path data: one stray token', svg('<path d="M0 0 L10 10 x"/><rect width="1" height="1"/>'), { removed: ['d'] }],
  ['a duplicate gradient id', svg('<linearGradient id="g"/><radialGradient id="g"/><rect fill="url(#g)" width="1" height="1"/>'), { removed: ['id'], has: ['<linearGradient id="g"/>', '<radialGradient/>'] }],
  ['only forbidden content: nothing left to draw', svg('<script>1</script><title>x</title>'), 'refused:empty'],
  ['a comment is skipped, "--" inside it is not XML', svg('<!-- hi --><rect width="1" height="1"/>'), { removed: [] }],
  ['a comment with -- inside', svg('<!-- a -- b --><rect/>'), 'refused:syntax'],
];

test('attack cases: each is refused (stays code) or drawn without the dangerous parts', async () => {
  for (const [name, input, want] of ATTACKS) {
    const r = await draw(input);
    if (typeof want === 'string') {
      assert.equal(r.ok, false, `${name}: should be refused, got ${r.svg}`);
      assert.equal(`refused:${r.reason}`, want, name);
      continue;
    }
    assert.equal(r.ok, true, `${name}: ${r.reason}`);
    assert.deepEqual(r.removed, [...want.removed].sort(), `${name}: removed ${r.removed}`);
    for (const s of want.has || []) assert.ok(r.svg.includes(s), `${name}: ${s} in ${r.svg}`);
    for (const s of want.not || []) assert.ok(!r.svg.includes(s), `${name}: no ${s} in ${r.svg}`);
  }
  assert.ok(ATTACKS.length >= 60);
});

test('value grammars, one by one (whole-value matches; written out canonically)', async () => {
  const { attributeValue: v } = await load('svg_safe.js');
  const table = [
    ['fill', 'url(#a)', 'url(#a)'], ['fill', 'url(#a-1)', 'url(#a-1)'], ['fill', 'url( #a)', null], ['fill', 'url(#a b)', null], ['fill', 'url(#1a)', null], ['fill', 'url(a)', null], ['fill', 'url(https://e/#a)', null], ['fill', 'url("#a")', null], ['fill', 'url(#a)x', null], ['stop-color', 'url(#a)', null],
    ['fill', 'rgb(255,255,255)', 'rgb(255,255,255)'], ['fill', 'rgb(256,0,0)', null], ['fill', 'rgb(1,2)', null], ['fill', 'rgb(1,2,3,4)', null], ['fill', 'rgba(1,2,3,1)', null], ['fill', '#ABCDEF', '#abcdef'], ['fill', '#abcdeg', null], ['fill', 'none', 'none'], ['fill', 'expression(alert(1))', null], ['fill', 'red;x', null],
    ['transform', 'translate(1)', 'translate(1)'], ['transform', 'rotate(1 2 3)', 'rotate(1 2 3)'], ['transform', 'rotate(1 2)', null], ['transform', 'matrix(1 0 0 1 0 0)', null], ['transform', 'skewX(1)', null], ['transform', 'translate(1,)', null], ['transform', 'Translate(1)', null], ['transform', 'translate(1) ,', null],
    ['d', 'M0 0L1 1z', 'M 0 0 L 1 1 z'], ['d', 'M0 0 L1 1 x', null], ['d', 'M0 0 L1', null], ['d', 'm1-2-3-4', 'm 1 -2 -3 -4'], ['d', 'M0 0 A1 1 0 2 1 1 1', null], ['d', 'M 0 0 L 1 1,', null], ['d', '', null],
    ['points', '1,2 3,4', '1 2 3 4'], ['points', '1,2 3', null], ['viewBox', '0 0 10 10', '0 0 10 10'], ['viewBox', '0 0 -1 10', null], ['viewBox', '0 0 10', null],
    ['x', '10px', '10'], ['x', '50%', '50%'], ['x', '1em', null], ['r', '-1', null], ['font-size', '12', '12'], ['font-size', '50%', null], ['opacity', '50%', '0.5'], ['opacity', '1.5', null], ['offset', '100%', '1'],
    ['font-family', 'Arial, "Sans-Serif", monospace', 'sans-serif, monospace'], ['font-family', 'Evil', null], ['id', 'a-1', 'a-1'], ['id', 'a_1', null], ['role', 'img', 'img'], ['role', 'IMG', null], ['stroke-dasharray', '4 3', '4 3'], ['stroke-dasharray', '-1', null],
    ['onload', 'x', null], ['href', '#a', null], ['style', 'fill:red', null], ['xlink:href', '#a', null], ['constructor', 'x', null], ['__proto__', 'x', null],
  ];
  for (const [name, value, want] of table) assert.equal(v(name, value), want, `${name}="${value}"`);
});

test('the audit reads the output back: anything outside the allow-list is reported', async () => {
  const { auditSvg } = await load('svg_safe.js');
  const clean = `<svg ${NS} width="10" height="10"><rect width="1" height="1"/></svg>`;
  assert.deepEqual(auditSvg(clean), []);
  for (const [bad, problem] of [
    [`<svg ${NS}><script/></svg>`, 'element:script'],
    [`<svg ${NS}><rect onload="x"/></svg>`, 'attr:rect.onload'],
    [`<svg ${NS}><rect fill="url(https://e/#a)"/></svg>`, 'value:rect.fill'],
    [`<svg ${NS}><rect d="M0 0 javascript:1"/></svg>`, 'value:rect.d'],
    [`<svg ${NS}><rect width="1px"/></svg>`, 'value:rect.width'], // not the canonical form written
    [`<svg ${NS}><g xmlns="${'http://www.w3.org/2000/svg'}"/></svg>`, 'xmlns:http://www.w3.org/2000/svg'],
    [`<svg ${NS}><g>text</g></svg>`, 'text:g'],
    ['<svg><rect/></svg>', 'root'],
    [`<!DOCTYPE x><svg ${NS}/>`, 'unreadable:markup'],
  ]) {
    assert.ok(auditSvg(bad).includes(problem), `${bad}: ${auditSvg(bad)}`);
  }
  // the independent scanner agrees on the same cases
  assert.ok(independentScan(`<svg ${NS}><rect onload="x"/></svg>`).length);
  assert.ok(independentScan(`<svg ${NS}><rect fill="url(https://e/#a)"/></svg>`).length);
  assert.ok(independentScan(`<svg ${NS}><text>a<b</text></svg>`).length);
  assert.ok(independentScan(`<svg ${NS}><rect style="fill:red"/></svg>`).length);
  assert.ok(independentScan(`<svg ${NS}><use/></svg>`).length);
  assert.ok(independentScan(`<svg ${NS}><rect fill="javascript:x"/></svg>`).length);
  assert.deepEqual(independentScan(`<svg ${NS}><text font-style="italic" aria-label="see https://example.com">onload &lt;script&gt;</text></svg>`), []);
});

test('the browser check: a DOMParser that reads something else, or an error, refuses the picture', async () => {
  const { svgImage, SVG_NS } = await load('svg_safe.js');
  const input = svg('<rect width="1" height="1" fill="red"/>');
  // a minimal DOM as a DOMParser would give back
  const el = (name, attrs, kids = [], ns = SVG_NS) => ({ nodeType: 1, namespaceURI: ns, prefix: null, localName: name, attributes: attrs.map(([n, v]) => ({ name: n, value: v, namespaceURI: n === 'xmlns' ? 'http://www.w3.org/2000/xmlns/' : null, prefix: null })), childNodes: kids });
  const parserGiving = (root, errors = 0) => class {
    parseFromString(text, type) {
      assert.equal(type, 'image/svg+xml');
      return { doctype: null, documentElement: root(text), getElementsByTagName: () => ({ length: errors }), getElementsByTagNameNS: () => ({ length: 0 }) };
    }
  };
  const good = () => el('svg', [['xmlns', SVG_NS], ['width', '100'], ['height', '100'], ['viewBox', '0 0 100 100']], [el('rect', [['width', '1'], ['height', '1'], ['fill', 'red']])]);
  assert.equal(svgImage(input, { domParser: parserGiving(good) }).ok, true);
  assert.deepEqual(svgImage(input, { domParser: parserGiving(good, 1) }), { ok: false, reason: 'check_failed' });
  const extraAttr = () => el('svg', [['xmlns', SVG_NS], ['width', '100'], ['height', '100'], ['viewBox', '0 0 100 100']], [el('rect', [['width', '1'], ['height', '1'], ['fill', 'red'], ['onload', 'x']])]);
  assert.deepEqual(svgImage(input, { domParser: parserGiving(extraAttr) }), { ok: false, reason: 'check_failed' });
  const otherNs = () => el('svg', [['xmlns', SVG_NS], ['width', '100'], ['height', '100'], ['viewBox', '0 0 100 100']], [el('rect', [['width', '1'], ['height', '1'], ['fill', 'red']], [], 'http://www.w3.org/1999/xhtml')]);
  assert.deepEqual(svgImage(input, { domParser: parserGiving(otherNs) }), { ok: false, reason: 'check_failed' });
  const comment = () => el('svg', [['xmlns', SVG_NS], ['width', '100'], ['height', '100'], ['viewBox', '0 0 100 100']], [{ nodeType: 8, data: 'x' }, el('rect', [['width', '1'], ['height', '1'], ['fill', 'red']])]);
  assert.deepEqual(svgImage(input, { domParser: parserGiving(comment) }), { ok: false, reason: 'check_failed' });
  const throwing = class { parseFromString() { throw new Error('x'); } };
  assert.deepEqual(svgImage(input, { domParser: throwing }), { ok: false, reason: 'check_failed' });
});

// --- normal pictures ---------------------------------------------------------------------------
const SAMPLES = {
  coffee: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 200 200" width="200" height="200">
  <title>珈琲カップ</title>
  <defs><linearGradient id="coffee" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="#8B5A2B"/><stop offset="100%" stop-color="#4a2c0f"/></linearGradient></defs>
  <ellipse cx="100" cy="172" rx="72" ry="10" fill="#d9d4c7"/>
  <path d="M50 80 L150 80 L140 160 Q100 176 60 160 Z" fill="#ffffff" stroke="#333" stroke-width="4"/>
  <ellipse cx="100" cy="84" rx="48" ry="8" fill="url(#coffee)"/>
  <path d="M150 96 a22 22 0 0 1 0 44" fill="none" stroke="#333" stroke-width="7" stroke-linecap="round"/>
  <path d="M80 60 q10 -15 0 -30 M100 60 q10-15 0-30 M120 60 q10 -15 0 -30" stroke="#999" stroke-width="3" fill="none" stroke-linecap="round"/>
</svg>`,
  bars: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 320 200" role="img" aria-label="月別の件数">
  <line x1="40" y1="170" x2="300" y2="170" stroke="#444"/>
  <line x1="40" y1="20" x2="40" y2="170" stroke="#444"/>
  <g fill="#3b82f6"><rect x="60" y="90" width="40" height="80"/><rect x="120" y="50" width="40" height="120"/><rect x="180" y="110" width="40" height="60"/><rect x="240" y="30" width="40" height="140" fill="#f97316"/></g>
  <g font-family="sans-serif" font-size="12" text-anchor="middle" fill="#222"><text x="80" y="188">7月</text><text x="140" y="188">8月</text><text x="200" y="188">9月</text><text x="260" y="188">10月</text></g>
</svg>`,
  diagram: `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 360 140">
  <title>構成図: ブラウザ → サーバー → モデル</title>
  <g font-family="sans-serif" font-size="13" text-anchor="middle" dominant-baseline="middle">
    <rect x="10" y="45" width="90" height="50" rx="8" fill="#e0f2fe" stroke="#0369a1" stroke-width="2"/><text x="55" y="70">ブラウザ</text>
    <rect x="135" y="45" width="90" height="50" rx="8" fill="#dcfce7" stroke="#15803d" stroke-width="2"/><text x="180" y="70">サーバー</text>
    <rect x="260" y="45" width="90" height="50" rx="8" fill="#fef3c7" stroke="#b45309" stroke-width="2"/><text x="305" y="70">モデル</text>
  </g>
  <g stroke="#333" stroke-width="2" fill="#333"><line x1="100" y1="70" x2="128" y2="70"/><polygon points="135,70 126,65 126,75"/><line x1="225" y1="70" x2="253" y2="70" stroke-dasharray="4 3"/><polygon points="260,70 251,65 251,75"/></g>
</svg>`,
};

test('normal pictures (coffee cup, bar chart, diagram) are drawn whole, with their alt text', async () => {
  for (const [name, text] of Object.entries(SAMPLES)) {
    const r = await draw(text);
    assert.equal(r.ok, true, `${name}: ${r.reason}`);
    assert.deepEqual(r.removed, [], name);
    const count = (s) => (s.match(/<[a-zA-Z]/g) || []).length;
    assert.equal(count(r.svg), count(text), `${name}: every element kept`);
  }
  assert.equal((await draw(SAMPLES.coffee)).alt, '珈琲カップ');
  assert.equal((await draw(SAMPLES.bars)).alt, '月別の件数');
  const d = await draw(SAMPLES.diagram);
  assert.equal(d.alt, '構成図: ブラウザ → サーバー → モデル');
  assert.deepEqual([d.width, d.height], [360, 140], 'the size comes from the viewBox');
  assert.deepEqual([(await draw(SAMPLES.coffee)).width, (await draw(SAMPLES.coffee)).height], [200, 200]);
  // a huge viewBox alone is shown at most SVG_LIMITS.shown on its long side
  const big = await draw(`<svg ${NS} viewBox="0 0 5000 2500"><rect width="1" height="1"/></svg>`);
  assert.deepEqual([big.width, big.height], [1024, 512]);
  const plain = await draw(`<svg ${NS}><rect width="1" height="1"/></svg>`);
  assert.deepEqual([plain.width, plain.height, plain.alt], [300, 150, null]);
});

test('the sanitiser is linear: a 32 KB pathological input is answered quickly', async () => {
  const { svgImage } = await load('svg_safe.js');
  const inputs = [
    svg(`<path d="${'1'.repeat(7000)}"/>`),
    svg(`<path d="M${' 1,'.repeat(2600)}"/>`),
    svg(`<rect fill="${'rgb('.repeat(2000)}"/>`),
    svg(`<g transform="${'translate('.repeat(3000)}"/>`),
    `<svg ${NS}>${'<!--'.repeat(5000)}`,
    `<svg ${NS} ${'a="1" '.repeat(5000)}/>`,
    svg('&amp;'.repeat(6000)),
  ];
  for (const input of inputs) {
    const t0 = process.hrtime.bigint();
    svgImage(input);
    const ms = Number(process.hrtime.bigint() - t0) / 1e6;
    assert.ok(ms < 200, `${ms} ms for ${input.slice(0, 40)}`);
  }
});

// --- the board renderer ------------------------------------------------------------------------
function fakeDoc() {
  const made = [];
  class Text {
    constructor(v) {
      this.nodeType = 3;
      this.data = String(v);
    }
    get textContent() {
      return this.data;
    }
  }
  class El {
    constructor(tag) {
      this.tagName = tag;
      this.children = [];
      this._text = '';
      this.className = '';
      this.attrs = {};
      this.listeners = {};
      made.push(tag);
    }
    set textContent(v) {
      this._text = String(v);
      this.children = [];
    }
    get textContent() {
      return this._text + this.children.map((c) => c.textContent).join('');
    }
    set innerHTML(_v) {
      throw new Error('innerHTML used');
    }
    setAttribute(k, v) {
      if (/^on/i.test(k) || k === 'href' || k === 'src' || k === 'style') throw new Error(`attribute ${k} set`);
      this.attrs[k] = String(v);
    }
    addEventListener(type, fn) {
      this.listeners[type] = fn;
    }
    append(...n) {
      this.children.push(...n);
    }
  }
  const all = (node, out = []) => {
    if (node instanceof El) {
      out.push(node);
      node.children.forEach((c) => all(c, out));
    }
    return out;
  };
  return { made, all, createElement: (tag) => new El(tag), createTextNode: (v) => new Text(v) };
}
const fence = (body, lang = 'svg') => `\`\`\`${lang}\n${body}\n\`\`\``;

test('board: with the setting a ```svg block is one <img> of the audited data: URL; off = code as before', async () => {
  const { parse } = await load('markdown.js');
  const { renderBlocks } = await load('md_render.js');
  const { svgImage } = await load('svg_safe.js');
  const reply = `図です。\n\n${fence(SAMPLES.coffee)}\n\nインラインの <svg onload="x"><rect/></svg> は文字のまま。`;
  // off: the code block, exactly as before (no img made)
  const off = fakeDoc();
  const offNodes = renderBlocks(off, parse(reply), { t, copy: async () => true });
  assert.ok(!off.made.includes('img'));
  assert.ok(offNodes.some((n) => n.className === 'md-code'));
  // on
  const doc = fakeDoc();
  let copied = null;
  const nodes = renderBlocks(doc, parse(reply), { t, copy: async (x) => { copied = x; return true; }, svg: (text) => svgImage(text) });
  const els = nodes.flatMap((n) => doc.all(n));
  const imgs = els.filter((e) => e.tagName === 'img');
  assert.equal(imgs.length, 1);
  const [img] = imgs;
  assert.ok(img.src.startsWith('data:image/svg+xml;charset=utf-8,'));
  const shown = decodeURIComponent(img.src.split(',').slice(1).join(','));
  assert.deepEqual(independentScan(shown), []);
  assert.equal(img.alt, '珈琲カップ');
  assert.deepEqual([img.width, img.height], [200, 200]);
  assert.equal(img.referrerPolicy, 'no-referrer');
  assert.deepEqual(Object.keys(img.attrs), [], 'nothing set through setAttribute on the image');
  assert.deepEqual(Object.keys(img.listeners), []);
  // the source folds away under the picture; copy copies the block's own text
  const details = els.find((e) => e.tagName === 'details');
  assert.ok(details && details.textContent.includes('<title>珈琲カップ</title>'));
  await els.find((e) => e.tagName === 'button').listeners.click();
  assert.equal(copied, SAMPLES.coffee);
  // inline SVG in a paragraph stays text
  assert.ok(nodes.map((n) => n.textContent).join('\n').includes('<svg onload="x"><rect/></svg>'));
  for (const tag of new Set(doc.made)) assert.ok(['p', 'div', 'span', 'button', 'img', 'details', 'summary', 'pre', 'code'].includes(tag), tag);
});

test('board: a refused SVG stays code with one line of reason; removed parts are named', async () => {
  const { parse } = await load('markdown.js');
  const { renderBlocks } = await load('md_render.js');
  const { svgImage } = await load('svg_safe.js');
  const doc = fakeDoc();
  const nodes = renderBlocks(doc, parse(fence(`<!DOCTYPE svg [<!ENTITY x "y">]>${svg('<rect/>')}`)), { t, svg: (x) => svgImage(x) });
  assert.ok(!doc.made.includes('img'));
  const text = nodes.map((n) => n.textContent).join('\n');
  assert.ok(text.includes(t('stage.board_svg_refused', { reason: t('stage.board_svg_r_markup') })), text);
  assert.ok(text.includes('<!DOCTYPE svg'), 'the code is shown as text');
  // too many attributes says so (not "too many elements"), with the limit
  const attrs = renderBlocks(fakeDoc(), parse(fence(svg(`<rect ${Array.from({ length: 41 }, (_, k) => `a${k}="1"`).join(' ')}/>`))), { t, svg: (x) => svgImage(x) });
  const attrsText = attrs.map((n) => n.textContent).join('\n');
  assert.ok(attrsText.includes(t('stage.board_svg_refused', { reason: t('stage.board_svg_r_too_many_attrs', { max: 40 }) })), attrsText);
  assert.ok(!attrsText.includes(t('stage.board_svg_r_too_many_elements')), attrsText);
  // every reason svg_safe.js can give has its own line in md_render.js
  const safeSrc = fs.readFileSync(path.join(STAGE, 'svg_safe.js'), 'utf8');
  const renderSrc = fs.readFileSync(path.join(STAGE, 'md_render.js'), 'utf8');
  const reasons = new Set([...safeSrc.matchAll(/refuse\('(\w+)'\)|reason: '(\w+)'/g)].map((m) => m[1] || m[2]));
  assert.ok(reasons.has('too_many_attrs') && reasons.size >= 10, [...reasons].join(' '));
  for (const reason of reasons) assert.ok(renderSrc.includes(`${reason}: () => t('stage.board_svg_r_${reason}'`), reason);
  const doc2 = fakeDoc();
  const nodes2 = renderBlocks(doc2, parse(fence(svg('<script>1</script><rect onload="x" width="1" height="1"/>'))), { t, svg: (x) => svgImage(x) });
  assert.ok(doc2.made.includes('img'));
  assert.ok(nodes2.map((n) => n.textContent).join('\n').includes(t('stage.board_svg_removed', { names: 'onload, script' })));
  // a thrown error or a foreign result is never drawn
  for (const bad of [() => { throw new Error('x'); }, () => ({ ok: true, src: 'https://evil.example/x.svg', removed: [] }), () => ({ ok: true, src: 'data:text/html,<script>', removed: [] }), () => null]) {
    const d = fakeDoc();
    renderBlocks(d, parse(fence(svg('<rect/>'))), { t, svg: bad });
    assert.ok(!d.made.includes('img'));
  }
});

test('board: at most 4 pictures per reply; an unclosed block (still streaming) stays code without a note; the node budget holds', async () => {
  const { parse } = await load('markdown.js');
  const { renderBlocks, MAX_NODES } = await load('md_render.js');
  const { svgImage, SVG_LIMITS } = await load('svg_safe.js');
  assert.equal(SVG_LIMITS.perReply, 4);
  const pic = fence(svg('<rect width="1" height="1"/>'));
  const doc = fakeDoc();
  const nodes = renderBlocks(doc, parse(Array(5).fill(pic).join('\n\n')), { t, svg: (x) => svgImage(x) });
  assert.equal(doc.made.filter((x) => x === 'img').length, 4);
  assert.ok(nodes.map((n) => n.textContent).join('\n').includes(t('stage.board_svg_r_too_many_svgs', { count: 4 })));
  // a refused block does not take a place: 4 pictures still drawn after it
  const bad = fence('<svg><rect>');
  const d2 = fakeDoc();
  renderBlocks(d2, parse([bad, pic, pic, pic, pic, pic].join('\n\n')), { t, svg: (x) => svgImage(x) });
  assert.equal(d2.made.filter((x) => x === 'img').length, 4);
  const streaming = fakeDoc();
  const sNodes = renderBlocks(streaming, parse(`\`\`\`svg\n${svg('<rect/>')}`), { t, svg: (x) => svgImage(x) });
  assert.ok(!streaming.made.includes('img'));
  assert.ok(!sNodes.map((n) => n.textContent).join('\n').includes(t('stage.board_svg_refused', { reason: '' }).slice(0, 8)));
  // a small budget: the elements stay within it, the rest is plain text
  for (const maxNodes of [5, 8, 12, 30]) {
    const d = fakeDoc();
    renderBlocks(d, parse(`# a\n\n${pic}\n\n${pic}`), { t, svg: (x) => svgImage(x), maxNodes });
    assert.ok(d.made.length <= maxNodes, `${d.made.length} > ${maxNodes}`);
  }
  assert.equal(MAX_NODES, 20000, 'the board budget is unchanged');
});

test('i18n: the SVG keys exist in both catalogs', () => {
  const src = ['md_render.js', 'svg_safe.js'].map((f) => fs.readFileSync(path.join(STAGE, f), 'utf8')).join('\n');
  const keys = [...src.matchAll(/'(stage\.board_svg[a-z_]*)'/g)].map((m) => m[1]);
  assert.ok(keys.length >= 16, keys.join(' '));
  for (const k of keys) {
    assert.ok(k in ja, `ja ${k}`);
    assert.ok(k in en, `en ${k}`);
  }
});

test('main.js: pictures only with data-board-svg="on", checked again by the browser DOMParser', () => {
  const main = fs.readFileSync(path.join(STAGE, 'main.js'), 'utf8');
  assert.match(main, /appEl\.dataset\.boardSvg === 'on' \? \(text\) => svgImage\(text, \{ domParser: window\.DOMParser \}\) : null/);
  // the log (talk.js) never draws them: checked in the page by scripts/check_board_svg_browser.mjs
  // (tests/test_board_svg_browser.py, whenever a Chrome / Chromium is found)
  const html = fs.readFileSync(path.join(ROOT, 'dashboard/templates/stage.html'), 'utf8');
  assert.ok(html.includes('{% if board_svg %} data-board-svg="on"{% endif %}'));
});

test('the browser check finds Chrome for this platform (CHROME first, then the usual places)', async () => {
  const { findChrome } = await import(path.join(ROOT, 'scripts', 'find_chrome.mjs'));
  const os = require('node:os');
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'find-chrome-'));
  const home = fs.mkdtempSync(path.join(os.tmpdir(), 'find-chrome-home-'));
  try {
    const bin = path.join(dir, 'chromium');
    fs.writeFileSync(bin, '');
    assert.equal(findChrome({ PATH: dir }, 'linux', home), bin);
    assert.equal(findChrome({ PATH: `${path.join(dir, 'none')}${path.delimiter}${dir}` }, 'freebsd', home), bin);
    assert.equal(findChrome({ CHROME: bin, PATH: '' }, 'linux', home), bin);
    assert.equal(findChrome({ CHROME: path.join(dir, 'missing'), PATH: dir }, 'linux', home), null, 'a CHROME that does not exist is not replaced by a guess');
    assert.equal(findChrome({ PATH: path.join(dir, 'none') }, 'linux', home), null);
    fs.mkdirSync(path.join(dir, 'chromium-dir'));
    assert.equal(findChrome({ PATH: path.join(dir, 'chromium-dir') }, 'linux', home), null, 'a directory is not a browser');
    const app = path.join(home, 'Applications/Chromium.app/Contents/MacOS');
    fs.mkdirSync(app, { recursive: true });
    fs.writeFileSync(path.join(app, 'Chromium'), '');
    const mac = findChrome({ PATH: '' }, 'darwin', home);
    assert.ok(mac === path.join(app, 'Chromium') || mac.startsWith('/Applications/'), mac);
    const pf = fs.mkdtempSync(path.join(os.tmpdir(), 'find-chrome-pf-'));
    fs.mkdirSync(path.join(pf, 'Google', 'Chrome', 'Application'), { recursive: true });
    fs.writeFileSync(path.join(pf, 'Google', 'Chrome', 'Application', 'chrome.exe'), '');
    assert.equal(findChrome({ PROGRAMFILES: pf }, 'win32', home), path.join(pf, 'Google', 'Chrome', 'Application', 'chrome.exe'));
    fs.rmSync(pf, { recursive: true, force: true });
  } finally {
    fs.rmSync(dir, { recursive: true, force: true });
    fs.rmSync(home, { recursive: true, force: true });
  }
});
