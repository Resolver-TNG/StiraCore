// Artifacts page: answers that arrive late (#601 E2-E4): node --test tests/artifacts_races_test.cjs
// Needs jsdom (npm install --no-save jsdom axe-core); skipped without it.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { resolve } = require('node:path');

let JSDOM = null;
try { ({ JSDOM } = require('jsdom')); } catch (err) { /* skipped below */ }
const skip = JSDOM ? false : 'jsdom not installed';
const SRC = readFileSync(resolve(__dirname, '../dashboard/static/js/artifacts.js'), 'utf8');
const tick = () => new Promise((r) => setTimeout(r, 0));
async function settle() { for (let i = 0; i < 40; i += 1) await tick(); }

const A = `a${'1'.repeat(20)}`;
const B = `a${'2'.repeat(20)}`;
const version = (n) => ({ version: n, turn_id: 't', op: n === 1 ? 'create' : 'update', size: 3, stored: true, at: 1 });
const detailOf = (id, path, labels = []) => ({
  id, path, kind: 'text', version: 2, updated_at: 1, labels, label_history: [], checks: [],
  versions: [version(2), version(1)],
});

// A page whose reads wait until the test lets them through (``hold``), one gate per URL.
function page() {
  const dom = new JSDOM(`<!doctype html><html><body>
    <select id="filter-kind"><option value=""></option></select><input id="filter-turn"><input id="filter-q">
    <p id="list-warnings"></p><ul id="artifact-list"></ul><p id="list-empty"></p>
    <section id="detail" hidden><h2 id="detail-title"></h2><p id="detail-meta"></p>
      <label><input type="checkbox" name="label" value="keep"></label>
      <label><input type="checkbox" name="label" value="discard"></label>
      <button id="save-labels"></button><p id="label-status"></p><div id="checks"></div>
      <table><tbody id="version-rows"></tbody></table>
      <h3 id="preview-title"></h3><div id="preview"></div><h3 id="diff-title"></h3><div id="diff"></div>
    </section></body></html>`, { runScripts: 'outside-only', url: 'http://127.0.0.1/artifacts' });
  const w = dom.window;
  const gates = new Map();
  const details = { [A]: detailOf(A, 'a.txt'), [B]: detailOf(B, 'b.txt') };
  const hold = (url) => { let open; const wait = new Promise((r) => { open = r; }); gates.set(url, { wait, open }); return open; };
  const answer = (url) => {
    if (url.startsWith('/api/artifacts?')) {
      return { artifacts: Object.values(details).map((d) => ({ id: d.id, path: d.path, kind: d.kind, version: 2, labels: d.labels, updated_at: 1 })) };
    }
    const m = /^\/api\/artifacts\/(a[0-9a-f]{20})(.*)$/.exec(url);
    const d = details[m[1]];
    if (!m[2]) return d;
    if (m[2].startsWith('/versions/')) return { text: `${d.path} v${m[2].split('/')[2]}` };
    if (m[2].startsWith('/diff')) return { available: true, added: 1, removed: 0, lines: [`+${d.path} ${m[2]}`], truncated: false };
    return {};
  };
  w.fetch = async (url) => {
    const gate = gates.get(url);
    if (gate) { gates.delete(url); await gate.wait; }
    return { ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(answer(url))) };
  };
  w.stiraFetch = async () => ({ ok: true, status: 200, json: async () => ({}) });
  w.t = (key, vars) => (vars ? `${key}:${JSON.stringify(vars)}` : key);
  const $ = (id) => w.document.getElementById(id);
  return { w, $, hold, details };
}

const showButtons = (env) => [...env.$('version-rows').querySelectorAll('button')];

test('a slow preview of v1 is not appended after the v2 the person asked for next (#601 E2/E4)', { skip }, async () => {
  const env = page();
  env.w.history.replaceState(null, '', `/artifacts#${A}`);
  env.w.eval(SRC);
  await settle();
  assert.equal(env.$('detail-title').textContent, 'a.txt');
  const [showV2, , showV1] = showButtons(env); // v2: show, diff; v1: show
  const openV1 = env.hold(`/api/artifacts/${A}/versions/1`);
  showV1.click();
  await settle();
  showV2.click();
  await settle();
  openV1();
  await settle();
  const texts = [...env.$('preview').querySelectorAll('pre')].map((p) => p.textContent);
  assert.deepEqual(texts, ['a.txt v2'], 'only the version asked for last is shown');
});

test('a slow diff is not appended after the next diff (#601 E2)', { skip }, async () => {
  const env = page();
  env.w.history.replaceState(null, '', `/artifacts#${A}`);
  env.w.eval(SRC);
  await settle();
  const diff = showButtons(env)[1];
  const first = env.hold(`/api/artifacts/${A}/diff?from=1&to=2`);
  diff.click();
  await settle();
  diff.click();
  await settle();
  first();
  await settle();
  assert.equal(env.$('diff').querySelectorAll('pre').length, 1, 'one diff, not two');
});

test('saving the labels of A while B is chosen leaves B on screen (#601 E3)', { skip }, async () => {
  const env = page();
  env.w.history.replaceState(null, '', `/artifacts#${A}`);
  env.w.eval(SRC);
  await settle();
  const item = (id) => env.w.document.querySelector(`.artifact-item[data-id="${id}"]`);
  assert.ok(item(B), 'B is in the list');
  env.details[A].labels = ['keep'];
  env.details[B].labels = ['discard'];
  const list = env.hold('/api/artifacts?limit=200'); // the refresh after the POST waits
  env.$('save-labels').click();
  await settle();
  item(B).click(); // the person picks B meanwhile
  await settle();
  assert.equal(env.$('detail-title').textContent, 'b.txt');
  list();
  await settle();
  const checked = [...env.w.document.querySelectorAll('input[name="label"]:checked')].map((b) => b.value);
  assert.equal(env.$('detail-title').textContent, 'b.txt');
  assert.deepEqual(checked, ['discard'], "B's labels stay; A's save does not draw A's labels over B");
  // the next save is B's
  const posts = [];
  env.w.stiraFetch = async (url) => { posts.push(url); return { ok: true, status: 200, json: async () => ({}) }; };
  env.$('save-labels').click();
  await settle();
  assert.deepEqual(posts, [`/api/artifacts/${B}/labels`]);
});

test('choosing A then B: a slow answer for A does not replace B (#601 E4)', { skip }, async () => {
  const env = page();
  env.w.eval(SRC);
  await settle();
  const item = (id) => env.w.document.querySelector(`.artifact-item[data-id="${id}"]`);
  const slowA = env.hold(`/api/artifacts/${A}`);
  item(A).click();
  await settle();
  item(B).click();
  await settle();
  slowA();
  await settle();
  assert.equal(env.$('detail-title').textContent, 'b.txt');
  assert.deepEqual([...env.$('preview').querySelectorAll('pre')].map((p) => p.textContent), ['b.txt v2']);
});

test('a label save of A that fails after B was chosen does not write its failure under B (#601 E3)', { skip }, async () => {
  const env = page();
  env.w.history.replaceState(null, '', `/artifacts#${A}`);
  env.w.eval(SRC);
  await settle();
  const item = (id) => env.w.document.querySelector(`.artifact-item[data-id="${id}"]`);
  let fail;
  env.w.stiraFetch = () => new Promise((r) => { fail = () => r({ ok: false, status: 500, json: async () => ({ error: 'x' }) }); });
  env.$('save-labels').click();
  await settle();
  item(B).click();
  await settle();
  assert.equal(env.$('detail-title').textContent, 'b.txt');
  const before = env.$('label-status').textContent;
  fail();
  await settle();
  assert.equal(env.$('label-status').textContent, before, "B's status line is not A's failure");
  // the same failure while A is still shown is said
  env.w.stiraFetch = async () => ({ ok: false, status: 500, json: async () => ({ error: 'x' }) });
  env.$('save-labels').click();
  await settle();
  assert.equal(env.$('label-status').textContent, 'artifacts.label_failed');
});
