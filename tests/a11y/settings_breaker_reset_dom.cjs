// #512: a breaker set reset on load, on the settings page, via jsdom.
// usage: node settings_breaker_reset_dom.cjs <settings.html> <hub.json>
// The hub state has breaker_pairs_reset: the file's warning pair is 0.5 / 0.4 (inverted).
// Exit codes: 0 = ok, 1 = failure, 77 = deps missing (skip).
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const { shimSource } = require('../ui_shim.cjs');
const { polyfillDialog } = require('../dialog_polyfill.cjs');

let JSDOM;
try {
  ({ JSDOM } = require('jsdom'));
} catch (err) {
  console.log(`SKIP: jsdom not available (${err.message})`);
  process.exit(77);
}

const JS = path.join(__dirname, '..', '..', 'dashboard', 'static', 'js');
const tick = () => new Promise(r => setTimeout(r, 0));
async function settle() { for (let i = 0; i < 30; i += 1) await tick(); }

async function main() {
  const html = fs.readFileSync(process.argv[2], 'utf8');
  const hub = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
  const dom = new JSDOM(html, { runScripts: 'outside-only', pretendToBeVisual: true, url: 'http://127.0.0.1/settings' });
  const { window } = dom;
  polyfillDialog(window);
  if (!window.CSS) window.CSS = {};
  if (!window.CSS.escape) window.CSS.escape = s => String(s).replace(/[^a-zA-Z0-9_-]/g, c => `\\${c}`);
  window.fetch = async (url, options = {}) => {
    const json = body => ({ ok: true, status: 200, json: async () => body });
    if (options.method === 'POST') return { ok: false, status: 409, json: async () => ({}) };
    if (url.startsWith('/api/config/history')) return json({ entries: [] });
    if (url.startsWith('/api/config/hub')) return json(hub);
    return json({});
  };
  window.setInterval = () => 0;
  window.eval(shimSource());
  window.eval(fs.readFileSync(path.join(JS, 'confirm_dialog.js'), 'utf8'));
  window.eval(fs.readFileSync(path.join(JS, 'breaker_pairs.js'), 'utf8'));
  window.eval(fs.readFileSync(path.join(JS, 'settings_hub.js'), 'utf8'));
  await settle();
  const doc = window.document;
  const $ = id => doc.getElementById(id);
  const t = (key, params) => window.t(key, params);
  const num = id => Number($(id).value);

  // The note on the breaker card, visible, in the page's language.
  const note = $('breaker-reset-note');
  assert.equal(note.hidden, false);
  assert.equal(note.textContent, t('hub.note_breaker_pairs_reset'));
  assert.ok(note.textContent.length > 20);
  // The apply status names the reason in words, not the code.
  const status = doc.querySelector('.apply-status[data-group="breaker"]');
  assert.ok(status.textContent.includes(t('hub.status_reason_breaker_pairs_reset')), status.textContent);
  assert.ok(!status.textContent.includes('breaker_pairs_reset'), status.textContent);
  // The sliders show the file's values (so entering any valid set is a change).
  assert.equal(num('stability-warning'), 0.5);
  assert.equal(num('stability-warning-exit'), 0.4);
  // No field error before a threshold moves, so another section can still be saved.
  assert.equal(doc.querySelectorAll('#sec-breaker .field-error:not([hidden])').length, 0);
  $('hb-enabled').checked = true;
  $('hb-enabled').dispatchEvent(new window.Event('change'));
  await settle();
  assert.equal($('hub-save').disabled, false, 'an unrelated change can be saved');
  $('hb-enabled').checked = false;
  $('hb-enabled').dispatchEvent(new window.Event('change'));
  await settle();
  // Moving the exit makes the pair valid on the safe side (the exit goes above the entry).
  $('stability-warning-exit').value = '0.45';
  $('stability-warning-exit').dispatchEvent(new window.Event('input'));
  await settle();
  assert.ok(num('stability-warning-exit') > num('stability-warning'));
  assert.equal($('hub-save').disabled, false);
  console.log('ok: breaker reset note, status reason, file values, safe-side slider');
  window.close();
}

main().catch((err) => { console.error(err); process.exit(1); });
