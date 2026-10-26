// #350 phase 2: the whiteboard — the line / board rule (deterministic), the Markdown renderer
// (sanitised: no HTML, no scripts, no handlers, safe links only), the highlighter, the board's
// geometry and progress, the character's step aside (room layers independent of the board),
// the context gauge and the `/new` command. node --test tests/stage_board_test.cjs
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const ROOT = path.join(__dirname, '..');
const STAGE = path.join(ROOT, 'dashboard', 'stage', 'js');
const load = (name) => import(path.join(STAGE, name));
const core = require('../dashboard/static/js/chat_core.js');
const SAMPLES = JSON.parse(fs.readFileSync(path.join(ROOT, 'docs/research/stage-main-ui-2026-10-17/samples-real.json'), 'utf8')).samples;
const ja = JSON.parse(fs.readFileSync(path.join(ROOT, 'dashboard/static/i18n/catalog.ja.json'), 'utf8'));
const en = JSON.parse(fs.readFileSync(path.join(ROOT, 'dashboard/static/i18n/catalog.en.json'), 'utf8'));
const t = (key, vars = {}) => String(ja[key] ?? key).replace(/\{(\w+)\}/g, (_, k) => String(vars[k]));

// --- the rule ---------------------------------------------------------------------------------
test('classify: the design table (docs/stage-main-ui.md §5.2), real and synthetic replies', async () => {
  const { classify } = await load('markdown.js');
  const real = SAMPLES.find((s) => s.id === 'real-1');
  const c = classify(real.text);
  assert.equal(c.mode, 'board');
  assert.deepEqual(c.counts.code, 3);
  assert.deepEqual(c.counts.table, 1);
  assert.deepEqual(c.counts.heading, 4);
  assert.ok(c.lead && c.lead.startsWith('コーヒーのついでに'), 'the first short paragraph is the lead');
  assert.ok(c.blocks.length && c.blocks[0].t !== 'p' || c.blocks[0].rows, 'the board holds the rest');
  const short = SAMPLES.find((s) => s.id === 'real-2');
  if (short) assert.equal(classify(short.text).mode, 'line', short.text);
  const cases = [
    ['こんにちは。今日もよろしくお願いします。', 'line', []],
    ['詳しくは https://example.com を見てください。', 'line', []],
    ['`pip install x` で入ります。', 'line', []],
    ['- ひとつ\n- ふたつ', 'line', []],
    ['- ひとつ\n- ふたつ\n- みっつ', 'board', ['list']],
    ['あ'.repeat(141), 'board', ['chars']],
    ['あ'.repeat(140), 'line', []],
    ['一。\n\n二。\n\n三。', 'line', []],
    ['一。\n\n二。\n\n三。\n\n四。', 'board', ['paragraphs']],
    ['1\n2\n3\n4', 'line', []],
    ['1\n2\n3\n4\n5', 'board', ['rows']],
    ['出力が長すぎるため途中で停止しました。続きが必要なら「続けて」と送信してください', 'line', []],
    ['<img src=x onerror=alert(1)>', 'line', []],
    ['# 見出しだけ', 'board', ['heading']],
    ['> 引用です', 'board', ['quote']],
    ['```python\nprint(1)', 'board', ['code']],
    ['| a | b |\n|---|---|\n| 1 | 2 |', 'board', ['table']],
  ];
  for (const [text, mode, reasons] of cases) {
    const r = classify(text);
    assert.equal(r.mode, mode, text);
    for (const why of reasons) assert.ok(r.reasons.includes(why), `${text}: ${r.reasons}`);
  }
});

test('classify is deterministic and independent of anything but the text', async () => {
  const { classify } = await load('markdown.js');
  for (const s of SAMPLES) {
    const a = JSON.stringify(classify(s.text));
    for (let i = 0; i < 3; i += 1) assert.equal(JSON.stringify(classify(s.text)), a);
  }
  // a seeded random corpus: the same input, the same answer
  let seed = 7;
  const rnd = () => ((seed = (seed * 1103515245 + 12345) % 2 ** 31) / 2 ** 31);
  const bits = ['# h', '- a', '1. b', '> q', '```', '|a|b|', '|-|-|', 'text', '', '**s**', '[l](https://x.y)', 'あいうえお'];
  for (let n = 0; n < 200; n += 1) {
    const text = Array.from({ length: 1 + Math.floor(rnd() * 12) }, () => bits[Math.floor(rnd() * bits.length)]).join('\n');
    assert.equal(JSON.stringify(classify(text)), JSON.stringify(classify(text)));
  }
});

test('lead: only a short first paragraph without code is spoken; else the fixed line', async () => {
  const { classify } = await load('markdown.js');
  const { replyView } = await load('talk.js');
  assert.equal(classify(`${'あ'.repeat(121)}\n\n## h`).lead, null);
  assert.equal(classify('`x` を使います。\n\n## h').lead, null);
  assert.equal(classify('はい。\n\n## h').lead, 'はい。');
  assert.equal(replyView('## h\n\ntext', null, t).say, ja['stage.say_board']);
  assert.equal(replyView('はい。\n\n## h', null, t).say, 'はい。');
  const line = replyView('**大事**なことです。', null, t);
  assert.equal(line.mode, 'line');
  assert.equal(line.say, '大事なことです。', 'marks are not spoken');
});

test('a stream is promoted to the board and never demoted', async () => {
  const { classifyStream } = await load('markdown.js');
  const full = 'はい。\n\n```py\nprint(1)\n```';
  assert.equal(classifyStream('はい。', null).mode, 'line');
  assert.equal(classifyStream(full, 'line').mode, 'board');
  assert.equal(classifyStream('はい。', 'board').mode, 'board');
});

test('size: replies over 64 KiB keep the head (never splitting a character) and say so', async () => {
  const { classify, headBytes, LIMITS } = await load('markdown.js');
  const big = '漢'.repeat(30000); // 90,000 bytes
  const c = classify(big);
  assert.equal(c.cut, true);
  assert.ok(new TextEncoder().encode(c.source).length <= LIMITS.boardBytes);
  assert.ok(c.reasons.includes('size'));
  const emoji = headBytes('👍'.repeat(10), 9);
  assert.equal(emoji.text, '👍👍');
  assert.equal(emoji.cut, true);
});

// --- sanitised rendering ------------------------------------------------------------------------
// A DOM that records what is built and refuses every HTML-parsing entry point, attribute
// handlers, src and href attributes (links get the href property after safeHref).
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
    set outerHTML(_v) {
      throw new Error('outerHTML used');
    }
    insertAdjacentHTML() {
      throw new Error('insertAdjacentHTML used');
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
    replaceChildren(...n) {
      this.children = n;
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

const HOSTILE = [
  '## <img src=x onerror=alert(1)> title',
  '',
  '<script>alert("x")</script> and [click](javascript:alert(1)) and [ok](https://example.com/a?b=1)',
  '![img](https://example.com/p.png) [data](data:text/html,<script>1</script>) [rel](/api/chat) [cred](https://u:p@example.com/)',
  '',
  '```html',
  '<script>alert(2)</script><a href="javascript:x" onclick="y()">',
  '```',
  '',
  '| a | <b onclick=x>b</b> |',
  '|---|---|',
  '| `<i>` | [x](JaVaScRiPt:alert(3)) |',
  '',
  '- <svg onload=alert(4)>',
  '- **<iframe src=//evil>**',
  '- [x](vbscript:msgbox)',
].join('\n');

test('renderer: hostile Markdown stays text — no script/img/iframe/svg, no handlers, no followable link', async () => {
  const { parse } = await load('markdown.js');
  const { renderBlocks } = await load('md_render.js');
  const doc = fakeDoc();
  const opened = [];
  const nodes = renderBlocks(doc, parse(HOSTILE), { t, openLink: (u, label) => opened.push([u, label]), copy: async () => true });
  const els = nodes.flatMap((n) => doc.all(n));
  const tags = new Set(doc.made);
  for (const bad of ['script', 'img', 'iframe', 'svg', 'object', 'embed', 'style', 'link', 'meta', 'form', 'input', 'a']) assert.ok(!tags.has(bad), bad);
  const allowed = new Set(['p', 'br', 'h3', 'h4', 'h5', 'h6', 'strong', 'em', 'code', 'ul', 'ol', 'li', 'blockquote', 'hr', 'div', 'span', 'pre', 'button', 'table', 'thead', 'tbody', 'tr', 'th', 'td']);
  for (const tag of tags) assert.ok(allowed.has(tag), `unexpected element ${tag}`);
  // the one safe link is a button: no href (nothing to middle-click, open from the context
  // menu or drag), only a click handler that asks the page
  const links = els.filter((e) => e.className === 'md-link');
  assert.equal(links.length, 1);
  const [b] = links;
  assert.equal(b.tagName, 'button');
  assert.equal(b.type, 'button');
  assert.equal(b.href, undefined);
  assert.equal(b.attrs.href, undefined);
  assert.deepEqual(Object.keys(b.listeners), ['click']);
  assert.equal(b.title, 'https://example.com/a?b=1');
  b.listeners.click({});
  assert.deepEqual(opened, [['https://example.com/a?b=1', 'ok']]);
  // the hostile parts are visible as text
  const text = nodes.map((n) => n.textContent).join('\n');
  for (const s of ['<img src=x onerror=alert(1)>', '<script>alert("x")</script>', '[click](javascript:alert(1))', '![img](https://example.com/p.png)', '[x](JaVaScRiPt:alert(3))', '<svg onload=alert(4)>', '[cred](https://u:p@example.com/)', '[rel](/api/chat)']) {
    assert.ok(text.includes(s), s);
  }
  // code is coloured, never run: the highlighted spans join back to the source
  const code = els.find((e) => e.tagName === 'pre').children[0];
  assert.equal(code.textContent, '<script>alert(2)</script><a href="javascript:x" onclick="y()">');
});

test('renderer: without the confirmation hook a link is plain text with its address', async () => {
  const { parse } = await load('markdown.js');
  const { renderBlocks } = await load('md_render.js');
  const doc = fakeDoc();
  const nodes = renderBlocks(doc, parse('see [the docs](https://example.com/d) and [https://example.com/e](https://example.com/e)'), { t });
  assert.ok(!doc.made.includes('button') && !doc.made.includes('a'));
  assert.equal(nodes[0].textContent, 'see the docs (https://example.com/d) and https://example.com/e');
});

test('renderer: a refused clipboard says so (rejection and exception)', async () => {
  const { parse } = await load('markdown.js');
  const { renderBlocks } = await load('md_render.js');
  for (const copy of [async () => { throw new Error('denied'); }, () => { throw new Error('sync'); }, async () => false]) {
    const doc = fakeDoc();
    const nodes = renderBlocks(doc, parse('```py\nx = 1\n```'), { t, copy });
    const btn = nodes.flatMap((n) => doc.all(n)).find((e) => e.tagName === 'button');
    await btn.listeners.click();
    assert.equal(btn.textContent, ja['stage.board_copy_failed']);
  }
});

test('safeHref: absolute http(s) only, no credentials, no spaces or quotes', async () => {
  const { safeHref } = await load('markdown.js');
  assert.equal(safeHref('https://example.com/x?y=1#z'), 'https://example.com/x?y=1#z');
  assert.equal(safeHref('http://127.0.0.1:5555/'), 'http://127.0.0.1:5555/');
  for (const bad of ['javascript:alert(1)', 'JAVASCRIPT:alert(1)', 'data:text/html,x', 'vbscript:x', '/api/chat', '//evil.example', 'https://u:p@example.com/', 'https://example.com/"onmouseover=x', 'https://exa mple.com', 'file:///etc/passwd', 'https:/x', '']) {
    assert.equal(safeHref(bad), null, bad);
  }
});

test('renderer: tables ruled and focusable for sideways scroll; headings start at h3 without gaps', async () => {
  const { parse } = await load('markdown.js');
  const { renderBlocks, headingLevels } = await load('md_render.js');
  const doc = fakeDoc();
  const nodes = renderBlocks(doc, parse('## a\n\n#### b\n\n| x | y |\n|:-:|--:|\n| 1 | 2 |'), { t });
  assert.deepEqual(nodes.map((n) => n.tagName), ['h3', 'h4', 'div']);
  const box = nodes[2];
  assert.equal(box.className, 'md-table');
  assert.equal(box.tabIndex, 0);
  assert.equal(box.attrs.role, 'group');
  const cells = doc.all(box).filter((e) => e.tagName === 'th' || e.tagName === 'td');
  assert.deepEqual(cells.map((c) => c.className), ['al-c', 'al-r', 'al-c', 'al-r']);
  assert.deepEqual([...headingLevels([{ t: 'h', level: 6 }, { t: 'h', level: 2 }])], [[2, 3], [6, 4]]);
});

test('highlighter: tokens join back to the code, for every language and the real replies', async () => {
  const { highlight, parse } = await load('markdown.js');
  const codes = SAMPLES.flatMap((s) => parse(s.text).filter((b) => b.t === 'code'));
  assert.ok(codes.length >= 3);
  for (const b of codes) assert.equal(highlight(b.text, b.lang).map((x) => x.v).join(''), b.text);
  const py = highlight('def f(x):\n    return "a" + 1  # c', 'python');
  const kinds = Object.fromEntries(py.filter((x) => x.k !== 'text').map((x) => [x.v.trim(), x.k]));
  assert.equal(kinds.def, 'kw');
  assert.equal(kinds.f, 'fn');
  assert.equal(kinds['"a"'], 'str');
  assert.equal(kinds['1'], 'num');
  assert.equal(kinds['# c'], 'com');
  for (const lang of ['js', 'ts', 'sh', 'json', 'css', 'sql', 'unknown', '']) {
    for (const src of ['const a = `x${1}`; // c\n/* b */', "echo 'a' # c", '{"a": [1, true, null]}', 'SELECT * FROM t -- c', '"unterminated', "'''", '😀 漢字 \t\r\n', '']) {
      assert.equal(highlight(src, lang).map((x) => x.v).join(''), src, `${lang}: ${src}`);
    }
  }
});

// --- board geometry, progress and actions -------------------------------------------------------
test('board geometry: beside her at 1300 px (she steps ~140 art px aside), in front on a phone', async () => {
  const { boardGeometry } = await load('board.js');
  const wide = boardGeometry({ width: 1068, height: 858, scale: 3, viewW: 356, tbTop: 600, portrait: false });
  assert.equal(wide.front, false);
  assert.equal(wide.left, 24);
  assert.ok(wide.width >= 620 && wide.width <= 700, wide.width);
  assert.ok(wide.shift >= 130 && wide.shift <= 150, wide.shift);
  assert.equal(wide.pan, 12);
  assert.equal(wide.aside, wide.shift + wide.pan);
  assert.equal(wide.bottom, 858 - 600 + 12);
  const phone = boardGeometry({ width: 390, height: 700, scale: 1.5, viewW: 260, tbTop: 420, portrait: true });
  assert.equal(phone.front, true);
  assert.equal(phone.left, 6);
  assert.equal(phone.width, 390 - 62);
  assert.equal(phone.pan, 8);
  // too little room beside her: in front, like the phone
  assert.equal(boardGeometry({ width: 600, height: 600, scale: 2, viewW: 300, tbTop: 420, portrait: false }).front, true);
  for (const g of [wide, phone]) for (const k of ['shift', 'aside', 'pan']) assert.ok(Number.isInteger(g[k]), k);
});

test('board progress: 270 ms ease-out down and up; reduced motion jumps', async () => {
  const { stepProgress, easeOut, OPEN_S } = await load('board.js');
  let p = 0;
  const seen = [];
  for (let i = 0; i < 20; i += 1) {
    p = stepProgress(p, 1, 1 / 60);
    seen.push(p);
  }
  assert.equal(OPEN_S, 0.27);
  assert.equal(seen[15] < 1, true);
  assert.equal(seen[16], 1); // 17 frames at 60 fps
  assert.ok(easeOut(0.5) > 0.5 && easeOut(0) === 0 && easeOut(1) === 1);
  assert.equal(stepProgress(0, 1, 0.001, true), 1);
  assert.equal(stepProgress(1, 0, 0.001, true), 0);
  assert.equal(stepProgress(0.5, 0, 1), 0);
});

test('board actions: replace when not pinned, hold when pinned, update the reply in flight', async () => {
  const { boardAction, boardStats, boardTitle } = await load('board.js');
  const { classify } = await load('markdown.js');
  assert.equal(boardAction({ pinned: false, open: false, currentKey: null, key: 'a' }), 'open');
  assert.equal(boardAction({ pinned: false, open: true, currentKey: 'a', key: 'b' }), 'replace');
  assert.equal(boardAction({ pinned: true, open: true, currentKey: 'a', key: 'b' }), 'held');
  assert.equal(boardAction({ pinned: true, open: true, currentKey: 'a', key: 'b', explicit: true }), 'replace');
  assert.equal(boardAction({ pinned: true, open: true, currentKey: 'a', key: 'a' }), 'update');
  assert.equal(boardAction({ pinned: false, open: false, currentKey: 'a', key: 'a' }), 'keep');
  const c = classify(SAMPLES[0].text);
  assert.equal(boardStats(c, t), `見出し 4 · コード 3 · 表 1 · ${c.counts.visible} 字`);
  assert.equal(boardTitle(classify('## タイトル\n\n本文'), t), 'タイトル');
  assert.equal(boardTitle(classify('```\nx\n```'), t), ja['stage.board_title']);
});

// --- layers: she steps aside, the room only pans, the board never reaches the room --------------
test('aside: integer art pixels for the character group only; the room pans by cam × depth', async () => {
  const { layerOffset, roomFrame, SHAKE_MIN_DEPTH } = await load('room.js');
  const pose = { neon: 'cyan', neonLevel: 1, dim: 0, desat: 0, steam: false, sparkle: false };
  const at = (e) => roomFrame(pose, {}, 3, { x: 0, y: 0 }, { e, aside: 152, pan: 12 });
  for (const e of [0, 0.13, 0.5, 0.77, 1]) {
    const f = at(e);
    const char = layerOffset(f, 1.0);
    const laptop = layerOffset(f, 1.18);
    const bands = [0.06, 0.3, 0.55, 0.8].map((d) => layerOffset({ ...f, aside: null }, d));
    const fore = layerOffset({ ...f, shake: null, aside: null }, 1.1);
    for (const o of [char, laptop, ...bands, fore]) assert.ok(Number.isInteger(o[0]) && Number.isInteger(o[1]));
    assert.equal(char[0], Math.round(-Math.round(e * 12) * 1.0) + Math.round(e * 152));
    for (const b of bands) assert.ok(Math.abs(b[0]) <= 12 && b[1] === 0);
    assert.ok(Math.abs(fore[0]) <= 14, 'the foreground (room) does not step aside');
    assert.ok(SHAKE_MIN_DEPTH === 1.0);
  }
  assert.deepEqual(layerOffset(at(1), 1.0), [140, 0]);
  assert.deepEqual(layerOffset(at(0), 1.0), [0, 0]);
});

test('layer independence: the board changes no room channel (lights, neon, grade, ambient)', async () => {
  const { roomFrame } = await load('room.js');
  const pose = { neon: 'violet', neonLevel: 0.5, dim: 0.28, desat: 0.55, 'light.desk': true, 'light.shelf': true, 'light.window': true, steam: true, sparkle: false };
  const ch = { 'light.desk.pulse': 0.4, 'neon.ambient': 0.03, 'city.twinkle': 0.5, 'steam.puff': 1 };
  const strip = (f) => ({ ...f, cam: null, aside: null });
  const base = strip(roomFrame(pose, ch, 9.5, { x: 0.2, y: -0.1 }));
  for (const board of [{ e: 1, aside: 152, pan: 12 }, { e: 0.4, aside: 999, pan: 8 }, { e: 1, aside: 0, pan: 0 }, { e: NaN }, null]) {
    assert.deepEqual(strip(roomFrame(pose, ch, 9.5, { x: 0.2, y: -0.1 }, board)), base);
  }
});

test('scene.js: two passes split by the render layer; additive lights keep the target alpha', () => {
  const src = fs.readFileSync(path.join(STAGE, 'scene.js'), 'utf8');
  assert.match(src, /cam\.layers\.set\(0\)/);
  assert.match(src, /cam\.layers\.set\(CHAR_LAYER\)/);
  assert.match(src, /this\.roomCtx\.drawImage\(this\.canvas, 0, 0\)/);
  assert.match(src, /blendSrcAlpha: ZERO_FACTOR, blendDstAlpha: ONE_FACTOR/);
  assert.match(src, /alpha: true/);
  // one pass when the board is up only if the scene has no room canvas (still identical picture)
  assert.match(src, /if \(!this\.split \|\| !this\.roomCtx \|\| !this\.charGroup\)/);
});

// --- context gauge and /new (chat_core, shared with the chat page) -------------------------------
test('gauge: input + cache read + cache write per provider, ÷ window; 80 % warns; near the summary', () => {
  const turn = { input_tokens: 1200, cache_read_tokens: 30000, cache_write_tokens: 800 };
  assert.equal(core.promptTokens(turn, 'bedrock'), 32000);
  assert.equal(core.promptTokens(turn, 'anthropic'), 32000);
  assert.equal(core.promptTokens(turn, 'openai'), 1200, 'openai-style input already includes the cache');
  assert.equal(core.promptTokens({ input_tokens: null }, 'bedrock'), null);
  assert.equal(core.promptTokens({ input_tokens: 10 }, 'bedrock'), 10);
  assert.equal(core.promptTokens({ input_tokens: -1 }, 'bedrock'), null);
  assert.equal(core.contextGauge({ turn: {}, provider: 'bedrock' }), null);
  const unknown = core.contextGauge({ turn, provider: 'bedrock' });
  assert.deepEqual(unknown, { tokens: 32000, window: null, ratio: null, level: 'ok', near: false, managed: false, threshold: 0.85, source: null, lastCall: false });
  assert.equal(core.gaugeText(unknown, t).text, 'ctx 32.0k');
  const g = (tokens, managed = true, threshold = 0.85) => core.contextGauge({ turn: { input_tokens: tokens }, provider: 'bedrock', window: 200000, threshold, managed });
  assert.equal(g(76000).ratio, 0.38);
  assert.equal(core.gaugeText(g(76000), t).text, 'ctx 38%');
  assert.equal(g(159999).level, 'ok');
  assert.equal(g(160000).level, 'warn');
  assert.equal(g(161000).near, false);
  assert.equal(g(161500).near, true); // >= 95 % of 0.85
  assert.equal(g(161500, false).near, false, 'no automatic summary without context management');
  assert.match(core.gaugeText(g(170000), t).text, /ctx 85% · 自動要約が近い/);
  assert.match(core.gaugeText(g(76000), t).title, /76,000 \/ コンテキストウィンドウ 200,000/);
});

test('gauge: the window comes from the status only when it carries one', () => {
  const live = { provider: 'bedrock', context_management: { enabled: false, mode: null } };
  assert.deepEqual(core.contextOf(live), { provider: 'bedrock', window: null, source: null, threshold: 0.85, managed: false });
  assert.deepEqual(core.contextOf(null), { provider: null, window: null, source: null, threshold: 0.85, managed: false });
  const proposed = { ...live, context_management: { enabled: true }, context_window: { tokens: 200000, compression_threshold: 0.7, source: 'default' } };
  assert.deepEqual(core.contextOf(proposed), { provider: 'bedrock', window: 200000, source: 'default', threshold: 0.7, managed: true });
  assert.equal(core.contextOf({ context_window: { tokens: 1000, source: 'elsewhere' } }).source, null);
  assert.equal(core.contextOf({ context_window: { tokens: -5, compression_threshold: 3 } }).window, null);
  assert.equal(core.contextOf({ context_window: { tokens: 5, compression_threshold: 3 } }).threshold, 0.85);
  // the provider list matches the server's (stiracore/providers/usage.py)
  const py = fs.readFileSync(path.join(ROOT, 'src/stiracore/providers/usage.py'), 'utf8');
  const list = py.match(/INPUT_INCLUDES_CACHE = frozenset\(\{([^}]*)\}\)/)[1].match(/"([a-z_]+)"/g).map((s) => s.slice(1, -1)).sort();
  assert.deepEqual([...core.INPUT_INCLUDES_CACHE].sort(), list);
});

test('/new is the page command, never a message; anything around it is a message', () => {
  assert.equal(core.commandOf('/new'), 'new');
  assert.equal(core.commandOf('  /NEW \n'), 'new');
  for (const m of ['/new please', 'new', '/newx', 'a /new', '/ new', '', null]) assert.equal(core.commandOf(m), null, String(m));
});

test('i18n: every phase-2 key exists in both catalogs', () => {
  const files = ['board.js', 'md_render.js', 'talk.js', 'backlog.js', 'main.js'].map((f) => fs.readFileSync(path.join(STAGE, f), 'utf8'));
  files.push(fs.readFileSync(path.join(ROOT, 'dashboard/static/js/chat_core.js'), 'utf8'));
  files.push(fs.readFileSync(path.join(ROOT, 'dashboard/templates/stage.html'), 'utf8'));
  const keys = new Set(files.flatMap((src) => [...src.matchAll(/['"]((?:stage|chat)\.(?:board|ctx|link|still|say_board|line_board|log_open_board|new_)[a-z_]*)['"]/g)].map((m) => m[1])));
  assert.ok(keys.size >= 20, [...keys].join(' '));
  for (const k of keys) {
    assert.ok(k in ja, `ja ${k}`);
    assert.ok(k in en, `en ${k}`);
  }
});

test('log: replies through the same renderer (text only), a board reply offers "open on the board"', async () => {
  const { buildEntries, renderEntries } = await load('backlog.js');
  const { buildStrings } = await load('i18n.js');
  const { parse, classify } = await load('markdown.js');
  const { renderBlocks } = await load('md_render.js');
  const S = buildStrings({ ja, en: {} }, 'ja');
  const doc = fakeDoc();
  const evil = '<script>alert(1)</script><img src=x onerror="alert(2)"> [x](javascript:alert(3))';
  const opened = [];
  const items = renderEntries(doc, doc.createElement('ol'), buildEntries([
    { message: '<b onmouseover="y()">hi</b>', response: evil, turn_id: 't1' },
    { message: 'code', response: HOSTILE, turn_id: 't2' },
  ]), S, {
    renderReply: (text) => renderBlocks(doc, parse(text), { t }),
    boardOf: (e) => (classify(e.text).mode === 'board' ? { key: `turn-${e.turn}`, text: e.text } : null),
    openBoard: (entry) => opened.push(entry.key),
  });
  assert.equal(items.length, 4);
  assert.equal(items[1].children[1].textContent, evil, 'the reply, verbatim, as text');
  assert.equal(items[0].children[1].textContent, '<b onmouseover="y()">hi</b>');
  assert.equal(items[1].children.length, 2, 'a line reply has no board button');
  const btn = items[3].children[2];
  assert.equal(btn.tagName, 'button');
  btn.listeners.click();
  assert.deepEqual(opened, ['turn-t2']);
  for (const bad of ['script', 'img', 'iframe', 'svg']) assert.ok(!doc.made.includes(bad), bad);
});

// --- review round (PR #357): termination, bounded work, links, /new, languages, final rule ---------
const { Worker } = require('node:worker_threads');

// Runs `body` (source of an async function of `m`, the markdown module) in a worker: a parser
// that loops forever fails the test after `ms` instead of hanging the run.
function inWorker(body, ms = 20000) {
  const url = require('node:url').pathToFileURL(path.join(STAGE, 'markdown.js')).href;
  const src = `const { parentPort } = require('node:worker_threads');
    import(${JSON.stringify(url)}).then(async (m) => { const r = await (${body})(m); parentPort.postMessage({ ok: true, r }); })
      .catch((e) => parentPort.postMessage({ ok: false, e: String(e && e.stack || e) }));`;
  return new Promise((resolve, reject) => {
    const w = new Worker(src, { eval: true });
    const timer = setTimeout(() => {
      w.terminate();
      reject(new Error(`did not finish in ${ms} ms (endless loop?)`));
    }, ms);
    w.on('message', (msg) => {
      clearTimeout(timer);
      w.terminate();
      if (msg.ok) resolve(msg.r);
      else reject(new Error(msg.e));
    });
    w.on('error', (e) => {
      clearTimeout(timer);
      reject(e);
    });
  });
}

test('parse always ends: the reported cases and a generated corpus of block combinations', async () => {
  const r = await inWorker(`async (m) => {
    const reported = ['# h\\n  - x', '# h\\n   - x', '# h\\n  1. x', '# h\\n   2) x', '  - a\\n  - b', '   1. a\\n 2. b\\n    3. c', '> q\\n  - x', '| a |\\n|-|\\n  - x'];
    for (const s of reported) m.classify(s);
    const bits = ['', ' ', '  ', '   ', '    '];
    const heads = ['- x', '* x', '+ x', '1. x', '2) x', '10. x', '> q', '>', '# h', '###### h', '#', '\`\`\`', '\`\`\`py', '~~~', '| a | b |', '|-|-|', '|---|', '---', '***', '___', 'text', '**b**', '[l](https://x.y)', '-', '1.', '| x', '- - -', '\`'];
    let seed = 11;
    const rnd = (k) => ((seed = (seed * 1103515245 + 12345) % 2147483648) % k);
    let runs = 0;
    for (let n = 0; n < 6000; n += 1) {
      const lines = Array.from({ length: 1 + rnd(9) }, () => bits[rnd(bits.length)] + heads[rnd(heads.length)]);
      const blocks = m.parse(lines.join('\\n'));
      if (blocks.length > lines.length + 1) throw new Error('more blocks than lines: ' + JSON.stringify(lines));
      m.classify(lines.join('\\n'));
      runs += 1;
    }
    // every pair of line kinds, at every indent
    for (const a of heads) for (const b of heads) for (const ia of bits) for (const ib of bits) { m.parse(ia + a + '\\n' + ib + b); runs += 1; }
    return runs;
  }`);
  assert.ok(r > 6000);
});

// pathological inputs (about the size the parser keeps, 64 KiB): each must stay linear
const PATHOLOGICAL = {
  fenceInfo: (n) => '```' + 'a'.repeat(n) + '`',
  fenceTildeInfo: (n) => '~~~' + ' a'.repeat(n / 2),
  backtickRuns: (n) => Array.from({ length: Math.floor(Math.sqrt(n)) }, (_, k) => '`'.repeat(k + 1) + 'x').join(' ').slice(0, n),
  unclosedCode: (n) => '`a'.repeat(n / 2),
  brackets: (n) => '['.repeat(n),
  linkOpeners: (n) => '[a]('.repeat(n / 4),
  images: (n) => '![a]('.repeat(n / 5),
  stars: (n) => '**a'.repeat(n / 3),
  underscores: (n) => '_a'.repeat(n / 2),
  headingSpaces: (n) => '# ' + ' '.repeat(n) + 'x',
  ruleLike: (n) => '- '.repeat(n / 2) + 'x',
  tableSepLike: (n) => 'a | b\n' + '-' + ' '.repeat(n) + 'x',
  tableSepCells: (n) => 'a | b\n' + '|-'.repeat(n / 2) + '|x',
  quotes: (n) => '> '.repeat(n / 2),
  listLines: (n) => '- a\n'.repeat(n / 4),
  nested: (n) => '- a\n  - b\n'.repeat(n / 10),
};

test('parse and inline are linear on pathological inputs (measured: 4× the input, < 8× the time)', async () => {
  const r = await inWorker(`async (m) => {
    const gens = { ${Object.entries(PATHOLOGICAL).map(([k, f]) => `${k}: ${f.toString()}`).join(',\n')} };
    const time = (s) => { const t0 = process.hrtime.bigint(); m.classify(s); m.parse(s); return Number(process.hrtime.bigint() - t0) / 1e6; };
    const out = {};
    for (const [name, g] of Object.entries(gens)) {
      const small = g(15000);
      const big = g(60000);
      time(small); // warm up
      const ts = Math.min(time(small), time(small));
      const tb = Math.min(time(big), time(big));
      out[name] = [ts, tb];
    }
    return out;
  }`, 60000);
  for (const [name, [ts, tb]] of Object.entries(r)) {
    assert.ok(tb < ts * 8 + 30, `${name}: 15k ${ts.toFixed(1)} ms, 60k ${tb.toFixed(1)} ms`);
    assert.ok(tb < 1000, `${name}: ${tb.toFixed(1)} ms`);
  }
});

test('every path is bounded: parse keeps 64 KiB, tables keep 24 columns × 400 rows, the render budget', async () => {
  const { parse, LIMITS, plainText } = await load('markdown.js');
  const { renderBlocks, MAX_NODES } = await load('md_render.js');
  // the review's table: 10,000 columns × 10,000 rows from about 50 KB
  const evil = `${'|'.repeat(10001)}\n|-|\n${'|x|\n'.repeat(10000)}`;
  let t0 = Date.now();
  const blocks = parse(evil);
  const table = blocks.find((b) => b.t === 'table');
  assert.ok(table.head.length <= LIMITS.tableCols && table.align.length <= LIMITS.tableCols);
  assert.ok(table.rows.length <= LIMITS.tableRows && table.cutRows > 0 && table.cutCols);
  const doc = fakeDoc();
  const nodes = renderBlocks(doc, blocks, { t });
  assert.ok(doc.made.length <= MAX_NODES + 10, `${doc.made.length} elements`);
  assert.ok(nodes.flatMap((n) => doc.all(n)).some((e) => e.className === 'md-note'), 'says what was left out');
  assert.ok(Date.now() - t0 < 3000);
  // parse() itself keeps the head (the log calls it directly, without classify)
  const huge = `${'あ'.repeat(200000)}\n\n- x`;
  const kept = parse(huge);
  assert.ok(new TextEncoder().encode(plainText(kept)).length <= LIMITS.boardBytes);
  // many blocks: at most LIMITS.blocks, then one "omitted"
  const many = parse('# h\n'.repeat(5000));
  assert.ok(many.length <= LIMITS.blocks + 1);
  assert.equal(many[many.length - 1].t, 'omitted');
  // a small budget: past it everything is plain text under one note, nothing is lost
  const doc2 = fakeDoc();
  const src2 = `${'- item\n'.repeat(3000)}\n\n\`\`\`js\n${'a();\n'.repeat(5000)}\`\`\``;
  const nodes2 = renderBlocks(doc2, parse(src2), { t, maxNodes: 500 });
  assert.ok(doc2.made.length <= 500, `${doc2.made.length}`);
  const notes2 = nodes2.filter((n) => n.className === 'md-note');
  assert.equal(notes2.length, 1);
  assert.equal(notes2[0].textContent, ja['stage.board_plain_rest']);
  const all2 = nodes2.map((n) => n.textContent).join('\n');
  assert.equal((all2.match(/item/g) || []).length, 3000, 'every item is still there, as text');
  assert.ok(all2.includes('a();\n'.repeat(4999)), 'the code too');
  // code that would need more coloured tokens than the budget stays one plain text node
  const doc3 = fakeDoc();
  const code = renderBlocks(doc3, parse(`\`\`\`js\n${'a();\n'.repeat(5000)}\`\`\``), { t, maxNodes: 100 });
  const pre = code.flatMap((n) => doc3.all(n)).find((e) => e.tagName === 'pre');
  assert.equal(pre.children[0].textContent, 'a();\n'.repeat(5000).replace(/\n$/, ''));
  assert.ok(doc3.made.length <= 100);
});

test('languages: inherited names (constructor, __proto__, toString) are not languages and never throw', async () => {
  const { langOf, highlight, parse } = await load('markdown.js');
  const { renderBlocks } = await load('md_render.js');
  for (const name of ['constructor', '__proto__', 'toString', 'hasOwnProperty', 'valueOf', 'isPrototypeOf', '__defineGetter__']) {
    assert.equal(langOf(name), null, name);
    assert.deepEqual(highlight('x = 1', name), [{ k: 'text', v: 'x = 1' }]);
    const doc = fakeDoc();
    renderBlocks(doc, parse(`\`\`\`${name}\nx = 1\n\`\`\``), { t });
  }
  assert.equal(langOf('PY'), 'python');
  assert.equal(langOf('ts'), 'js');
});

test('the end of a reply is classified from scratch: the board rolls up when the final text is a line', async () => {
  const { classify, classifyStream } = await load('markdown.js');
  const { replyView } = await load('talk.js');
  const { finalBoardAction } = await load('board.js');
  // a stream can look like a table for a moment: "|-" is a delimiter row, "|-x|" is not
  const prefix = 'a | b\n|-';
  const final = 'a | b\n|-x|';
  assert.equal(classifyStream(prefix, null).mode, 'board');
  assert.equal(classifyStream(final, 'board').mode, 'board', 'while streaming: never demoted');
  // at the end: as the log and a reload (classify of the whole text)
  assert.equal(replyView(final, null, t).mode, classify(final).mode);
  assert.equal(classify(final).mode, 'line');
  assert.equal(finalBoardAction({ finalMode: 'line', boardKey: 'k', key: 'k', open: true, pinned: false }), 'close');
  assert.equal(finalBoardAction({ finalMode: 'line', boardKey: 'k', key: 'k', open: true, pinned: true }), 'keep');
  assert.equal(finalBoardAction({ finalMode: 'line', boardKey: 'other', key: 'k', open: true, pinned: false }), 'keep');
  assert.equal(finalBoardAction({ finalMode: 'board', boardKey: 'k', key: 'k', open: true, pinned: false }), 'keep');
  // for every real and synthetic reply the end result equals the log's
  for (const s of [...SAMPLES.map((x) => x.text), HOSTILE, prefix, final, '1\n2\n3\n4\n5', '- a\n- b']) {
    assert.equal(replyView(s, null, t).mode, classify(s).mode);
  }
});

test('/new: refused with images, any case, surrounding spaces; the shared sender refuses it too', async () => {
  const { newCommand } = await load('talk.js');
  for (const m of ['/new', ' /NEW ', '/New\n']) {
    assert.equal(newCommand(core, m), true, m);
    assert.equal(newCommand({}, m), true, `${m} without core.commandOf (fail closed)`);
    assert.equal(newCommand(null, m), true);
  }
  for (const m of ['/new please', 'new', 'a /new']) assert.equal(newCommand(core, m), false, m);
  let calls = 0;
  const sender = core.createSender({ fetch: async () => { calls += 1; throw new Error('must not be called'); }, t: (k) => k });
  const image = { name: 'a.png', size: 3, url: 'data:', block: { type: 'image', source: { type: 'base64', media_type: 'image/png', data: 'AAA' } } };
  for (const send of [{ message: '/new', images: [image] }, { message: ' /NEW ' }, { message: 'x', payload: core.payloadOf('/new', [image]) }]) {
    const r = await sender.send(send);
    assert.equal(r.ok, false);
    assert.equal(r.delivery, 'refused');
    assert.equal(r.command, 'new');
    assert.equal(r.key, null);
  }
  assert.equal(calls, 0, 'no request');
  assert.equal(sender.unsettled, null, 'nothing waits for re-send / discard');
});

test('the board shows the conversation reply only: deltas, not tool events; history message/response only', async () => {
  const { buildEntries } = await load('backlog.js');
  // the shared sender passes only `delta` text to the reply; ui_event (tool calls) go elsewhere
  const enc = new TextEncoder();
  const sse = [
    'event: delta\ndata: {"text": "Hello "}\n\n',
    'event: ui_event\ndata: {"type": "tool_call.updated", "preview": "FILE CONTENT", "output": "SECRET OUTPUT"}\n\n',
    'event: delta\ndata: {"text": "world"}\n\n',
    'event: turn\ndata: {"turn_id": "t1", "input_tokens": 1}\n\n',
  ];
  const body = new ReadableStream({ start(c) { for (const x of sse) c.enqueue(enc.encode(x)); c.close(); } });
  const fetch = async () => new Response(body, { status: 200, headers: { 'content-type': 'text/event-stream' } });
  const sender = core.createSender({ fetch, t: (k) => k, makeKey: () => 'k1' });
  let reply = '';
  const ui = [];
  const r = await sender.send({ message: 'hi' }, { delta: (d) => { reply += d.text; }, uiEvent: (e) => ui.push(e) });
  assert.equal(r.ok, true);
  assert.equal(reply, 'Hello world');
  assert.equal(ui.length, 1);
  // the log reads message / response only, whatever else a history record carries
  const entries = buildEntries([{ message: 'q', response: 'answer', turn_id: 't', tool_output: 'SECRET', turn_summary: { x: 'SECRET' }, preview: 'SECRET' }]);
  assert.ok(!JSON.stringify(entries).includes('SECRET'));
  assert.deepEqual(entries.map((e) => e.text), ['q', 'answer']);
});

test('render budget is enforced centrally: inline floods in every container stay within maxNodes', async () => {
  const { parse } = await load('markdown.js');
  const { renderBlocks, MAX_NODES } = await load('md_render.js');
  const flood = '*x*'.repeat(21000); // 63,000 bytes: 21,000 <em> in one paragraph
  const cases = {
    paragraph: flood,
    quote: `> ${flood}`,
    heading: `# ${flood}`,
    list: `- ${'**b**'.repeat(12000)}\n${'- *i*\n'.repeat(1000)}`,
    tableCell: `| a | b |\n|---|---|\n| ${'`c`'.repeat(10000)} | x |\n${'| *y* | *z* |\n'.repeat(300)}`,
    rows: `${'*a*\n'.repeat(15000)}`,
    links: '[l](https://example.com/) '.repeat(2400),
    mixed: `${'*e* **s** `c` '.repeat(3000)}\n\n${'| *p* | *q* |\n|---|---|\n'}${'| *r* | *s* |\n'.repeat(400)}`,
  };
  for (const [name, src] of Object.entries(cases)) {
    for (const maxNodes of [3000, MAX_NODES, 50]) {
      const doc = fakeDoc();
      const t0 = process.hrtime.bigint();
      const nodes = renderBlocks(doc, parse(src), { t, maxNodes, openLink: () => {}, copy: async () => true });
      const ms = Number(process.hrtime.bigint() - t0) / 1e6;
      assert.ok(doc.made.length <= maxNodes, `${name} @${maxNodes}: ${doc.made.length} elements`);
      assert.ok(ms < 1500, `${name} @${maxNodes}: ${ms.toFixed(1)} ms`);
      // the text is all still there (plain past the budget): no reply text is silently lost
      const text = nodes.map((n) => n.textContent).join('');
      if (name === 'links') assert.ok((text.match(/l/g) || []).length >= 2400, 'every link text kept');
      else {
        const marks = (src.match(/[xbicyzaesqrp]/g) || []).length;
        assert.ok((text.match(/[xbicyzaesqrp]/g) || []).length >= marks * 0.99, `${name}: text kept`);
      }
      // at most one "without formatting" note
      assert.ok(nodes.filter((n) => n.textContent === ja['stage.board_plain_rest']).length <= 1);
    }
  }
  // the review's exact case
  const doc = fakeDoc();
  const t0 = Date.now();
  renderBlocks(doc, parse('*x*'.repeat(21000)), { t, maxNodes: 3000 });
  assert.ok(doc.made.length <= 3000, `${doc.made.length}`);
  assert.ok(Date.now() - t0 < 1500);
});

test('parse: the block limit holds on every path that adds a block (flush included)', async () => {
  const { parse, LIMITS } = await load('markdown.js');
  const kinds = ['para\n# h', 'para\n```\nx\n```', 'para\n- a', 'para\n> q', 'para\n| a |\n|-|', 'para\n\n---', 'p\n\np'];
  for (const k of kinds) {
    const blocks = parse(`${k}\n`.repeat(3000), { ...LIMITS, blocks: 50 });
    assert.equal(blocks.filter((b) => b.t !== 'omitted').length, 50, k);
    assert.equal(blocks.length, 51, `${k}: ${blocks.length}`);
    assert.equal(blocks[blocks.length - 1].t, 'omitted', k);
  }
  const big = parse('a\n\n'.repeat(20000));
  assert.ok(big.length <= LIMITS.blocks + 1);
  // a paragraph pending when the limit is reached is not added after it (flush goes through add)
  const edge = parse('# h\n'.repeat(50) + 'tail para', { ...LIMITS, blocks: 50 });
  assert.deepEqual([edge.length, edge[50].t, edge[49].t], [51, 'omitted', 'h'], 'the pending paragraph is left out, not added');
  const exact = parse('# h\n'.repeat(50), { ...LIMITS, blocks: 50 });
  assert.equal(exact.length, 50, 'exactly the limit: nothing omitted');
  const edge2 = parse('# h\n'.repeat(49) + 'p1\np2\n# h2\nmore', { ...LIMITS, blocks: 50 });
  assert.ok(edge2.filter((b) => b.t !== 'omitted').length <= 50);
});

test('/new is the command only as ASCII "/new" alone (any case, spaces around); the sender refuses it', async () => {
  // not the command: ordinary messages, sent as they are (harmless either way: a refused one
  // keeps the text in the composer, a sent one is plain text for the model — no action exists)
  for (const m of ['/new please', '/new\u200b', '\u200b/new', '／ｎｅｗ', '/ｎｅｗ', '/new/', '//new', '/news', 'new', '/n e w']) {
    assert.equal(core.commandOf(m), null, JSON.stringify(m));
  }
  for (const m of ['/new', '/NEW', '/NeW', '  /new  ', '\t/New\n']) assert.equal(core.commandOf(m), 'new', JSON.stringify(m));
  let calls = 0;
  const sender = core.createSender({ fetch: async () => { calls += 1; throw new Error('must not be called'); }, t: (k) => k });
  const image = { name: 'a.png', size: 3, url: 'data:', block: { type: 'image', source: { type: 'base64', media_type: 'image/png', data: 'AAA' } } };
  const sends = [
    { message: '/NEW', images: [image] },
    { message: 'hello', payload: core.payloadOf('/NeW', []) }, // the body says /new: refused
    { message: 'hello', payload: core.payloadOf(' /new ', [image]) },
    { message: '/new', payload: core.payloadOf('hello', []) }, // the message says /new: refused
  ];
  for (const send of sends) {
    const r = await sender.send(send);
    assert.equal(r.delivery, 'refused', JSON.stringify(send.message));
  }
  assert.equal(calls, 0);
  assert.equal(sender.unsettled, null);
  // ordinary messages around it still go out (one request each)
  const fetched = [];
  const ok = core.createSender({ fetch: async (url, init) => { fetched.push(JSON.parse(init.body).message); return new Response('event: turn\ndata: {"turn_id":"t"}\n\n', { status: 200 }); }, t: (k) => k, makeKey: () => `k${fetched.length}` });
  for (const m of ['/new please', '/new\u200b', '／ｎｅｗ']) await ok.send({ message: m });
  assert.deepEqual(fetched, ['/new please', '/new\u200b', '／ｎｅｗ']);
});

test('while the board is down the props on the text window wait (laptop, lamp); drones and room props stay', async () => {
  const { boardHides } = await load('gadget_scene.js');
  const { GADGETS } = await load('gadget_catalog.js');
  const hidden = GADGETS.filter((g) => boardHides(g, true)).map((g) => g.id).sort();
  assert.deepEqual(hidden, ['laptop', 'stacklight']);
  assert.deepEqual(GADGETS.filter((g) => boardHides(g, false)), [], 'board up: nothing hidden');
  for (const id of ['drones', 'monitor', 'stacklight_back']) assert.equal(boardHides(GADGETS.find((g) => g.id === id), true), false, id);
  // the scene asks the model as before (the event still causes them) and only holds them back
  const src = fs.readFileSync(path.join(STAGE, 'gadget_scene.js'), 'utf8');
  assert.match(src, /const shown = isOn\(pose\[g\.show\]\) && !this\.spot\(g\)\.hide && gadgetCondition\(g, pose\);/);
  assert.match(src, /const want = shown && !byBoard;/);
  assert.match(fs.readFileSync(path.join(STAGE, 'main.js'), 'utf8'), /scene\.gadgets\.boardDown = Boolean\(board && board\.visible\)/);
  assert.ok(ja['stage.debug_board_hidden'] && en['stage.debug_board_hidden']);
});
