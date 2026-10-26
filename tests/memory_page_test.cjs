// Memory browser page (#601 E6): node --test tests/memory_page_test.cjs
// Needs jsdom (npm install --no-save jsdom axe-core); skipped without it.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { resolve } = require('node:path');

let JSDOM = null;
try { ({ JSDOM } = require('jsdom')); } catch (err) { /* skipped below */ }
const skip = JSDOM ? false : 'jsdom not installed';
const SRC = readFileSync(resolve(__dirname, '../dashboard/static/js/memory.js'), 'utf8');
const tick = () => new Promise((r) => setTimeout(r, 0));
async function settle() { for (let i = 0; i < 20; i += 1) await tick(); }

function page(answer) {
  const dom = new JSDOM(`<!doctype html><html><body>
    <button class="tab-btn active" data-layer="episodic"></button><input id="search">
    <div id="entry-list"></div></body></html>`, { runScripts: 'outside-only', url: 'http://127.0.0.1/memory' });
  const w = dom.window;
  w.t = (key) => key;
  w.fetch = async () => answer();
  w.setInterval = () => 0;
  w.eval(SRC);
  return w;
}

test('an error answer with a JSON body says "could not load", not "no entries" (#601 E6)', { skip }, async () => {
  const w = page(() => ({ ok: false, status: 400, json: async () => ({ error: 'invalid layer: x' }) }));
  await settle();
  assert.equal(w.document.getElementById('entry-list').textContent.trim(), 'memory.load_error');
});

test('a list answer is drawn; an empty list says "no entries"', { skip }, async () => {
  const w = page(() => ({ ok: true, status: 200, json: async () => [{ key: 'k1', content: 'hello', metadata: null }] }));
  await settle();
  assert.match(w.document.getElementById('entry-list').textContent, /k1[\s\S]*hello/);
  const empty = page(() => ({ ok: true, status: 200, json: async () => [] }));
  await settle();
  assert.equal(empty.document.getElementById('entry-list').textContent.trim(), 'memory.no_entries');
});

test('a 200 answer that is not a list is a failure too (#601 E6)', { skip }, async () => {
  const w = page(() => ({ ok: true, status: 200, json: async () => ({ detail: 'not a list' }) }));
  await settle();
  assert.equal(w.document.getElementById('entry-list').textContent.trim(), 'memory.load_error');
});
