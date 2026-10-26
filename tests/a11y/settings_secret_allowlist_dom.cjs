// #270: a saved allowlist entry holding a secret is shown masked and marked, and the
// removal button outside the (disabled) shell editor removes it; the save posts the
// allowlist without it. Via jsdom, with a real /api/config/hub state.
// usage: node settings_secret_allowlist_dom.cjs <settings.html> <hub.json> <secret marker>
// Exit codes: 0 = ok, 1 = failure, 77 = deps missing (skip).
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const { shimSource } = require('../ui_shim.cjs');
const { polyfillDialog } = require('../dialog_polyfill.cjs');

let JSDOM;
let axeSource;
try {
  ({ JSDOM } = require('jsdom'));
  axeSource = require('axe-core').source;
} catch (err) {
  console.log(`SKIP: jsdom/axe-core not available (${err.message})`);
  process.exit(77);
}

const JS = path.join(__dirname, '..', '..', 'dashboard', 'static', 'js');
const tick = () => new Promise(r => setTimeout(r, 0));
async function settle() { for (let i = 0; i < 30; i += 1) await tick(); }

async function main() {
  const html = fs.readFileSync(process.argv[2], 'utf8');
  const hub = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
  const marker = process.argv[4];
  const dom = new JSDOM(html, { runScripts: 'outside-only', pretendToBeVisual: true, url: 'http://127.0.0.1/settings' });
  const { window } = dom;
  polyfillDialog(window);
  if (!window.CSS) window.CSS = {};
  if (!window.CSS.escape) window.CSS.escape = s => String(s).replace(/[^a-zA-Z0-9_-]/g, c => `\\${c}`);
  const posts = [];
  window.fetch = async (url, options = {}) => {
    const json = body => ({ ok: true, status: 200, json: async () => body });
    if (options.method === 'POST') { posts.push([url, JSON.parse(options.body)]); return { ok: false, status: 409, json: async () => ({}) }; }
    if (url.startsWith('/api/config/history')) return json({ entries: [] });
    if (url.startsWith('/api/config/hub')) return json(hub);
    return json({});
  };
  window.setInterval = () => 0;
  window.eval(shimSource());
  window.eval(fs.readFileSync(path.join(JS, 'confirm_dialog.js'), 'utf8'));
  // Same order as the page: the breaker pair helpers (#471) load before the hub.
  window.eval(fs.readFileSync(path.join(JS, 'breaker_pairs.js'), 'utf8'));
  window.eval(fs.readFileSync(path.join(JS, 'settings_hub.js'), 'utf8'));
  await settle();
  const doc = window.document;
  const visibleText = () => doc.getElementById('sec-tools').textContent;
  const labels = () => [...doc.querySelectorAll('[aria-label]')].map(el => el.getAttribute('aria-label')).join('\n');

  assert.equal(hub.secret_allowlist.length, 1, 'fixture: one saved secret entry');
  const banner = doc.getElementById('allowlist-secrets');
  assert.equal(banner.hidden, false, 'the notice is shown');
  assert.equal(doc.getElementById('allowlist-secrets-text').textContent, window.t('policy.secret_saved_notice', { count: 1 }));
  assert.equal(doc.getElementById('shell-editor').disabled, true, 'fixture: shell off, the editor is disabled');
  assert.equal(banner.closest('fieldset[disabled]'), null, 'the removal works while the editor is disabled');
  const items = [...doc.querySelectorAll('#allowlist li')];
  assert.equal(items.length, hub.values.tools.shell_allowlist.length);
  const marked = items.filter(li => li.querySelector('.badge'));
  assert.equal(marked.length, 1, 'one entry marked');
  assert.equal(marked[0].querySelector('code').textContent, hub.secret_allowlist[0].masked, 'shown masked');
  assert.ok(!visibleText().includes(marker), 'the secret is not on screen');
  assert.ok(!labels().includes(marker), 'nor in an accessible name');

  window.eval(axeSource);
  const result = await window.axe.run(doc.getElementById('sec-tools'), { rules: { 'color-contrast': { enabled: false } } });
  if (result.violations.length) {
    for (const v of result.violations) console.log(`[${v.impact}] ${v.id}: ${v.help} (${v.nodes.length})`);
    process.exit(1);
  }

  doc.getElementById('allowlist-secrets-remove').click();
  await settle();
  assert.equal(banner.hidden, true, 'removed: the notice goes away');
  const left = [...doc.querySelectorAll('#allowlist li code')].map(c => c.textContent);
  const expected = hub.values.tools.shell_allowlist.filter((_, i) => !hub.secret_allowlist.some(m => m.index === i));
  assert.deepEqual(left, expected);
  assert.ok(!visibleText().includes(marker), 'the pending-change summary does not show it either');
  doc.getElementById('hub-save').click();
  await settle();
  assert.equal(posts.length, 1, 'a removal is a tightening: saved without a confirmation');
  assert.equal(posts[0][0], '/api/config/hub');
  assert.deepEqual(posts[0][1].changes.tools.shell_allowlist, expected);
  console.log(`ok: masked, marked, removed (${expected.length} left)`);
  window.close();
}

main().catch((err) => { console.error(err); process.exit(1); });
