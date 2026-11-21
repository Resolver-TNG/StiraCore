// Memory table on /memory (#652): node --test tests/memory_table_test.cjs
// Needs jsdom (npm install --no-save jsdom axe-core); skipped when it is missing.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { resolve } = require('node:path');
const { shimSource } = require('./ui_shim.cjs');

let JSDOM = null;
try { ({ JSDOM } = require('jsdom')); } catch (err) { /* skipped below */ }
const skip = JSDOM ? false : 'jsdom not installed';
const SRC = readFileSync(resolve(__dirname, '../dashboard/static/js/memory_table.js'), 'utf8');
const tick = () => new Promise(r => setTimeout(r, 0));
async function settle() { for (let i = 0; i < 30; i += 1) await tick(); }

const BODY = `
<button id="refresh" type="button">r</button>
<p id="memtable-status" role="status"></p>
<table id="memtable" hidden><tbody id="memtable-body"></tbody></table>`;
const INJECTED = '<img src=x onerror="window.__xss=1">';
const retention = (kind, state, days = null) => ({ kind, half_life_days: days, state });
const row = (key, importance, importanceState, ret) => ({
  key, importance, importance_state: importanceState, retention: ret,
});

function page(answer) {
  const dom = new JSDOM(`<!doctype html><html lang="ja"><body>${BODY}</body></html>`,
    { runScripts: 'outside-only', url: 'http://localhost/memory' });
  const win = dom.window;
  win.eval(shimSource());
  const urls = [];
  win.fetch = async (url, init = {}) => {
    urls.push({ url, method: init.method || 'GET' });
    return answer(new URL(url, 'http://localhost').searchParams.get('layer'));
  };
  win.eval(SRC);
  return { win, urls };
}
const ok = payload => ({ ok: true, status: 200, json: async () => payload });
const cells = win => [...win.document.querySelectorAll('#memtable-body tr')]
  .map(tr => [...tr.children].map(td => td.textContent));

test('one row per entry: layer, key, importance, retention; unverified reads 未確認', { skip }, async () => {
  const layers = {
    episodic: { entries: [
      row('2026-11-01', 0.46, 'verified', retention('invariant', 'verified')),
      row('claimed', null, 'unverified', retention('derived', 'unverified')),
      row('plain', null, 'none', retention('derived', 'default')),
      row('edited', null, 'none', retention('invariant', 'changed')),
    ], total: 4, limit: 50 },
    semantic: { entries: [
      row('knowledge/rule', 0.8, 'recorded', retention('derived', 'default', 30)),
      row('fades', null, 'none', retention('derived', 'tagged', 7)),
      row('bad', null, 'none', retention('derived', 'invalid', 30)),
    ], total: 3, limit: 50 },
    procedural: { entries: [row('style', null, 'none', retention('derived', 'default'))], total: 1, limit: 50 },
  };
  const { win, urls } = page(layer => ok(layers[layer]));
  await settle();
  assert.deepEqual(urls.map(u => u.url), ['episodic', 'semantic', 'procedural'].map(l => `/api/memory/entries?layer=${l}`));
  assert.ok(urls.every(u => u.method === 'GET'));
  assert.deepEqual(cells(win), [
    ['エピソード記憶', '2026-11-01', '0.46', '忘れない（invariant）'],
    ['エピソード記憶', 'claimed', '未確認', '未確認'],
    ['エピソード記憶', 'plain', '—', '既定（タグなし）'],
    ['エピソード記憶', 'edited', '—', '忘れない（invariant。タグを付けた後に本文が変わっています）'],
    ['意味記憶', 'knowledge/rule', '0.80', '既定（タグなし、半減期 30 日）'],
    ['意味記憶', 'fades', '—', '半減期 7 日'],
    ['意味記憶', 'bad', '—', '既定（タグの値が不正なため無視）'],
    ['手続き記憶', 'style', '—', '既定（タグなし）'],
  ]);
  assert.equal(win.document.getElementById('memtable').hidden, false);
  assert.match(win.document.getElementById('memtable-status').textContent, /8 件/);
});

test('only the allow-listed fields are drawn, as text: no body, no markup, no unknown values', { skip }, async () => {
  const entry = {
    ...row(`k ${INJECTED}`, 0.99, 'unverified', retention(INJECTED, INJECTED, INJECTED)),
    content: 'BODY-TEXT-must-not-show', metadata: { note: 'FREE-TEXT' }, summary: 'BODY-TEXT-must-not-show',
  };
  const odd = row('odd', '0.5', INJECTED, { kind: 'derived', half_life_days: '9', state: 'tagged' });
  const { win } = page(layer => ok({ entries: layer === 'episodic' ? [entry, odd, { key: 7 }] : [], total: 120, limit: 50 }));
  await settle();
  const doc = win.document;
  assert.deepEqual(cells(win), [
    ['エピソード記憶', `k ${INJECTED}`, '未確認', '—'],
    ['エピソード記憶', 'odd', '—', '忘れてよい（derived）'],
  ]);
  assert.equal(doc.querySelector('#memtable-body img'), null);
  assert.equal(win.__xss, undefined);
  assert.doesNotMatch(doc.body.textContent, /BODY-TEXT|FREE-TEXT|0\.99/);
  // The total of the layers is named when the list is cut.
  assert.match(doc.getElementById('memtable-status').textContent, /2 件.*360 件/);
});

test('empty: the table stays hidden; a failure clears the rows and says so; refresh reloads', { skip }, async () => {
  let mode = 'empty';
  const { win } = page(() => {
    if (mode === 'fail') return { ok: false, status: 500, json: async () => ({ error: 'x' }) };
    if (mode === 'odd') return ok({ detail: 'not a list' });
    return ok({ entries: mode === 'rows' ? [row('a', null, 'none', retention('derived', 'default'))] : [], total: 0, limit: 50 });
  });
  const doc = win.document;
  const status = doc.getElementById('memtable-status');
  await settle();
  assert.equal(doc.getElementById('memtable').hidden, true);
  assert.equal(status.textContent, 'まだ記憶はありません');
  mode = 'rows';
  doc.getElementById('refresh').click();
  await settle();
  assert.equal(cells(win).length, 3);
  for (const failing of ['fail', 'odd']) {
    mode = failing;
    doc.getElementById('refresh').click();
    await settle();
    assert.equal(cells(win).length, 0, failing);
    assert.equal(doc.getElementById('memtable').hidden, true, failing);
    assert.match(status.textContent, /読み込めませんでした/, failing);
  }
});
