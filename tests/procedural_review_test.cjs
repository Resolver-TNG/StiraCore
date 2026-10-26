// Procedural proposals panel (#463): node --test tests/procedural_review_test.cjs
// Needs jsdom (npm install --no-save jsdom axe-core); skipped when it is missing.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { resolve } = require('node:path');
const { shimSource } = require('./ui_shim.cjs');
const { polyfillDialog } = require('./dialog_polyfill.cjs');

let JSDOM = null;
try { ({ JSDOM } = require('jsdom')); } catch (err) { /* skipped below */ }
const skip = JSDOM ? false : 'jsdom not installed';
const JS = resolve(__dirname, '../dashboard/static/js');
const tick = () => new Promise(r => setTimeout(r, 0));
async function settle() { for (let i = 0; i < 30; i += 1) await tick(); }

const BODY = `
<button id="refresh" type="button">r</button>
<section id="procedural-review">
  <span id="proposals-count"></span><p id="proposals-effective"></p><p id="proposals-breaker" hidden></p>
  <p id="proposals-status" role="status"></p><div id="proposals-list"></div>
  <ul id="proposals-history"></ul>
</section>`;
const TURN = 'a'.repeat(32);
const NORMAL = { level: 'normal', approve_allowed: true };
const proposal = (key, extra = {}) => ({
  key, body: 'step', sha256: 'f'.repeat(64), length: 4, rationale: 'r', source_turn: TURN,
  proposed_at: null, proposed_via: 'propose_procedure', revision: false, existing: null, diff: [],
  approvable: true, problem: null, ...extra,
});

function page(state) {
  const dom = new JSDOM(`<!doctype html><html lang="ja"><body>${BODY}</body></html>`,
    { runScripts: 'outside-only', pretendToBeVisual: true, url: 'http://localhost/memory' });
  const win = dom.window;
  polyfillDialog(win);
  win.eval(shimSource());
  const posts = [];
  win.fetch = async (url, init = {}) => {
    if ((init.method || 'GET') !== 'GET') {
      posts.push({ url, body: JSON.parse(init.body) });
      return { ok: true, status: 200, json: async () => ({}) };
    }
    return { ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(state.data)) };
  };
  win.stiraFetch = win.fetch;
  win.eval(readFileSync(resolve(JS, 'confirm_dialog.js'), 'utf8'));
  win.eval(readFileSync(resolve(JS, 'procedural_review.js'), 'utf8'));
  return { win, posts };
}

test('approve: the dialog needs its tick box and sends the shown hash and replace', { skip }, async () => {
  const state = { data: { proposals: [proposal('style', { revision: true, existing: { sha256: 'e'.repeat(64) } })], reviews: [], prompt_limit: 5, effective: 'next_turn', breaker: NORMAL } };
  const { win, posts } = page(state);
  await settle();
  const doc = win.document;
  doc.querySelector('article.proposal .btn-primary').click();
  await settle();
  const dialog = doc.getElementById('stira-confirm');
  assert.equal(dialog.open, true);
  const items = [...dialog.querySelectorAll('#stira-confirm-items li')].map(li => li.textContent);
  assert.ok(items.some(text => text.includes('置き換え')), items);
  const ok = doc.getElementById('stira-confirm-ok');
  assert.equal(ok.disabled, true, 'not before the box is ticked');
  dialog.querySelector('input[type="checkbox"]').click();
  ok.click();
  await settle();
  assert.deepEqual(posts, [{ url: '/api/memory/procedural/proposals/style/approve', body: { sha256: 'f'.repeat(64), confirm: true, replace: true } }]);
});

test('cancel sends nothing; reject sends the typed reason; a seed turn is not a link', { skip }, async () => {
  const state = { data: { proposals: [proposal('seedy', { source_turn: 'seed:x', proposed_via: 'operator' })], reviews: [], prompt_limit: 5, effective: 'next_start', breaker: NORMAL } };
  const { win, posts } = page(state);
  await settle();
  const doc = win.document;
  assert.equal(doc.querySelector('.proposal-meta a'), null);
  doc.querySelector('article.proposal .btn-primary').click();
  await settle();
  doc.getElementById('stira-confirm-cancel').click();
  await settle();
  assert.deepEqual(posts, []);
  doc.querySelector('article.proposal textarea').value = '  too vague ';
  doc.querySelector('article.proposal .btn-secondary').click();
  await settle();
  doc.getElementById('stira-confirm-ok').click();
  await settle();
  assert.deepEqual(posts, [{ url: '/api/memory/procedural/proposals/seedy/reject', body: { sha256: 'f'.repeat(64), confirm: true, reason: 'too vague' } }]);
  assert.match(doc.getElementById('proposals-effective').textContent, /再起動/);
});

test('a proposal that cannot be approved has its approve button disabled and says why', { skip }, async () => {
  const state = { data: { proposals: [proposal('old', { approvable: false, problem: 'E_REVISION_STALE', existing: { sha256: 'e'.repeat(64) } })], reviews: [], effective: 'next_turn', breaker: NORMAL } };
  const { win } = page(state);
  await settle();
  const doc = win.document;
  assert.equal(doc.querySelector('article.proposal .btn-primary').disabled, true);
  assert.match(doc.querySelector('.proposal-problem').textContent, /変わりました/);
});

test('breaker not NORMAL: approve disabled with a notice, reject still sends; a refusal reads as the breaker message', { skip }, async () => {
  const state = { data: { proposals: [proposal('style')], reviews: [], prompt_limit: 5, effective: 'next_turn', breaker: { level: 'critical', approve_allowed: false } } };
  const { win, posts } = page(state);
  await settle();
  const doc = win.document;
  const note = doc.getElementById('proposals-breaker');
  assert.equal(note.hidden, false);
  assert.match(note.textContent, /CRITICAL/);
  assert.match(note.textContent, /NORMAL/);
  assert.equal(doc.querySelector('article.proposal .btn-primary').disabled, true);
  doc.querySelector('article.proposal .btn-secondary').click();
  await settle();
  doc.getElementById('stira-confirm-ok').click();
  await settle();
  assert.equal(posts.length, 1);
  assert.equal(posts[0].url, '/api/memory/procedural/proposals/style/reject');
  // A field without the breaker view (an older server) fails closed: approve disabled.
  state.data = { proposals: [proposal('style')], reviews: [], prompt_limit: 5, effective: 'next_turn' };
  doc.getElementById('refresh').click();
  await settle();
  assert.equal(doc.querySelector('article.proposal .btn-primary').disabled, true);
});

test('the server refusing an approval for the breaker shows the breaker message (ja)', { skip }, async () => {
  const state = { data: { proposals: [proposal('style')], reviews: [], prompt_limit: 5, effective: 'next_turn', breaker: NORMAL } };
  const { win } = page(state);
  // The level dropped between the poll and the click: the server answers 409.
  win.stiraFetch = async () => ({ ok: false, status: 409, json: async () => ({ code: 'E_BREAKER_NOT_NORMAL' }) });
  await settle();
  const doc = win.document;
  doc.querySelector('article.proposal .btn-primary').click();
  await settle();
  doc.getElementById('stira-confirm').querySelector('input[type="checkbox"]').click();
  doc.getElementById('stira-confirm-ok').click();
  await settle();
  const text = doc.getElementById('proposals-status').textContent;
  assert.match(text, /NORMAL/);
  assert.match(text, /E_BREAKER_NOT_NORMAL/);
  assert.match(text, /承認されませんでした/);
});
