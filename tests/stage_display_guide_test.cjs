// Display guide (#366): the fixed text that tells the model how the Stage splits a reply into
// the spoken line and the whiteboard (docs/stage-main-ui.md §5.5). Its numbers and names come
// from the constants the screen uses; this test fails when they drift apart.
// node --test tests/stage_display_guide_test.cjs
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { pathToFileURL } = require('node:url');

const ROOT = path.join(__dirname, '..');
const load = (name) => import(pathToFileURL(path.join(ROOT, 'dashboard', 'stage', 'js', name)).href);

function guide() {
  const md = fs.readFileSync(path.join(ROOT, 'docs', 'stage-main-ui.md'), 'utf8').replace(/\r\n/g, '\n');
  const parts = md.split('<!-- stage-display-guide:begin -->');
  assert.equal(parts.length, 2, 'one guide block');
  const body = parts[1].split('<!-- stage-display-guide:end -->')[0].trim();
  assert.ok(body.startsWith('```text\n') && body.endsWith('\n```'));
  return body.slice('```text\n'.length, -'\n```'.length);
}
const flat = (s) => s.replace(/\s+/g, ' ');

test('the guide states the limits of classify (LIMITS), not its own numbers', async () => {
  const { LIMITS } = await load('markdown.js');
  const g = flat(guide());
  assert.ok(g.includes(`about ${LIMITS.lineChars} characters at most, no more than ${LIMITS.linePages} paragraphs of up to ${LIMITS.pageRows} lines each`));
  assert.ok(g.includes(`a list of ${LIMITS.listItems} or more items`));
  assert.ok(g.includes(`about ${LIMITS.leadChars} characters at most, up to ${LIMITS.pageRows} lines`));
  // every number in the text is one of the constants (nothing defined twice by hand)
  const { SVG_LIMITS } = await load('svg_safe.js');
  const known = new Set([LIMITS.lineChars, LIMITS.linePages, LIMITS.pageRows, LIMITS.listItems, LIMITS.leadChars, SVG_LIMITS.perReply, SVG_LIMITS.bytes / 1024]);
  for (const n of g.match(/\d+/g)) assert.ok(known.has(Number(n)), `number ${n} is not a display constant`);
});

test('the guide states the SVG rules of the board (elements, limits, what stays text)', async () => {
  const { ELEMENTS, SVG_LIMITS, svgImage } = await load('svg_safe.js');
  const { classify } = await load('markdown.js');
  const g = flat(guide());
  assert.ok(g.includes(`Allowed elements: ${ELEMENTS.join(', ')}.`));
  assert.ok(g.includes(`at most ${SVG_LIMITS.perReply} per reply, ${SVG_LIMITS.bytes / 1024} KB each`));
  assert.ok(g.includes('only a closed ```svg code block is drawn'));
  // what the text says is what the code does
  assert.ok(!ELEMENTS.includes('filter') && !ELEMENTS.includes('feGaussianBlur'));
  for (const name of ['script', 'style', 'image']) assert.ok(!ELEMENTS.includes(name), name);
  const pic = svgImage('<svg xmlns="http://www.w3.org/2000/svg" width="20" height="20"><filter id="f"><feGaussianBlur stdDeviation="2"/></filter><rect width="10" height="10" fill="#f00"/></svg>');
  assert.ok(!pic.ok || !/filter|feGaussianBlur/.test(pic.svg), 'a filter is never drawn');
  const inlineSvg = classify('<svg width="10" height="10"><rect width="10" height="10"/></svg>');
  assert.ok(inlineSvg.blocks.every((b) => b.t === 'p'), 'an inline <svg> is a paragraph of text');
  assert.ok(classify('![chart](x.svg)').blocks.every((b) => b.t === 'p'), 'an image reference is text');
});

test('a reply written as the guide says: the first paragraph is spoken, the rest is the board', async () => {
  const { classify, LIMITS } = await load('markdown.js');
  const short = classify('了解。すぐ見るよ。');
  assert.equal(short.mode, 'line');
  const lead = '結論から言うと、原因は設定の読み込み順です。';
  const long = classify(`${lead}\n\n## 手順\n\n1. 設定を開く\n2. 順番を直す\n3. 再起動する\n\n\`\`\`sh\nstira restart\n\`\`\``);
  assert.equal(long.mode, 'board');
  assert.equal(long.lead, lead);
  assert.ok(long.blocks.length >= 3 && long.blocks[0].t === 'h');
  // a first paragraph over the lead limit is not spoken (the whole reply is on the board)
  assert.equal(classify(`${'あ'.repeat(LIMITS.leadChars + 1)}\n\n# 見出し`).lead, null);
});

test('the guide is one fixed text: no per-screen wording, no state, no marker syntax, bounded size', () => {
  const g = guide();
  assert.ok(g.length < 1800, `guide is ${g.length} characters`);
  assert.doesNotMatch(g, /\{\{|\$\{|%s|<say>/);
  assert.doesNotMatch(g, /\b(if you are on|when on the stage|chat page)\b/i);
  assert.doesNotMatch(g, /setting is (on|off) now|currently (on|off)/i);
});
