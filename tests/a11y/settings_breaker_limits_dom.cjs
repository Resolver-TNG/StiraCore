// Upper limits of the breaker details on the settings page (#471 review M-1), via jsdom.
// usage: node settings_breaker_limits_dom.cjs <settings.html> <hub.json>
// The saved state holds bc_window_size = 10**9 (saved before the limit existed).
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
  const posts = [];
  let postReply = { ok: false, status: 409, json: async () => ({}) };
  window.fetch = async (url, options = {}) => {
    const json = body => ({ ok: true, status: 200, json: async () => body });
    if (options.method === 'POST') {
      posts.push({ url, body: JSON.parse(options.body) });
      return postReply;
    }
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
  const type = async (id, value) => {
    $(id).value = String(value);
    $(id).dispatchEvent(new window.Event('change'));
    await settle();
  };
  const rules = hub.constraints.breaker_details;
  const HUGE = hub.values.breaker_details.bc_window_size;
  assert.equal(HUGE, 10 ** 9);

  // The inputs carry the server's limits.
  const inputs = {
    bc_window_size: 'bc-window-size',
    sycophancy_window_size: 'sycophancy-window-size',
    critical_cooldown_turns: 'critical-cooldown-turns',
    hitl_max_edit_rounds: 'hitl-max-edit-rounds',
  };
  for (const [key, id] of Object.entries(inputs)) {
    assert.equal($(id).max, String(rules[key].max), `${key} input max`);
  }
  // The saved value above the limit is shown as it is, with a note, never as an error.
  assert.equal($('bc-window-size').value, String(HUGE));
  assert.equal($('note-bc-window-size').hidden, false);
  assert.equal($('note-bc-window-size').textContent,
    t('hub.note_saved_above_limit', { value: String(HUGE), max: '200' }));
  assert.equal($('err-breaker_details-bc_window_size').hidden, true);
  assert.equal($('note-sycophancy-window-size').hidden, true, 'a value inside its limit has no note');

  // An unrelated change saves (the kept value is not flagged by the page's check).
  await type('hitl-timeout-sec', '60');
  assert.equal(doc.querySelectorAll('#sec-breaker .field-error:not([hidden])').length, 0);
  $('hub-save').click();
  await settle();
  assert.equal(posts.length, 1, 'the unrelated change was posted');
  assert.equal(posts[0].body.changes.breaker_details.hitl_timeout_sec, 60);
  assert.equal(posts[0].body.changes.breaker_details.bc_window_size, HUGE, 'kept as saved');

  // Typing a huge value rounds it to the limit, with the "rounded" note (the page's rule).
  $('hub-discard').click();
  await settle();
  await type('sycophancy-window-size', String(10 ** 9));
  assert.equal($('sycophancy-window-size').value, '200');
  assert.equal($('note-sycophancy-window-size').textContent,
    t('hub.note_rounded', { from: String(10 ** 9), to: '200' }));
  // Moving the saved value: the limit applies (rounded into it), the note changes.
  await type('bc-window-size', String(HUGE - 1));
  assert.equal($('bc-window-size').value, '200');
  assert.equal($('note-bc-window-size').textContent,
    t('hub.note_rounded', { from: String(HUGE - 1), to: '200' }));

  // A server refusal (the server checks the limit itself) shows on its field.
  $('hub-discard').click();
  await settle();
  const refusal = { field: 'breaker_details.critical_cooldown_turns', code: 'out_of_range', params: { min: 3, max: 50 } };
  postReply = { ok: false, status: 400, json: async () => ({ error: 'Invalid settings.', errors: [refusal] }) };
  await type('critical-cooldown-turns', '7');
  $('hub-save').click();
  await settle();
  assert.equal(posts.length, 2);
  const shown = $('err-breaker_details-critical_cooldown_turns');
  assert.equal(shown.hidden, false);
  assert.ok(shown.textContent.includes(t('hub.err_out_of_range', { min: 3, max: 50 })), shown.textContent);
  console.log('ok: detail upper limits — input max, kept saved value, rounding, server refusal');
  window.close();
}

main().catch((err) => { console.error(err); process.exit(1); });
