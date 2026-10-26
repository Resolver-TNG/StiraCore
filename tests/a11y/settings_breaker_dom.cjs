// Breaker pairs and details on the settings page (#471 review), via jsdom.
// usage: node settings_breaker_dom.cjs <settings.html> <hub.json>
// The saved state has warning 0.7 / 0.8, critical 0.3 / 0.45 and the details changed.
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
  window.fetch = async (url, options = {}) => {
    const json = body => ({ ok: true, status: 200, json: async () => body });
    if (options.method === 'POST') {
      posts.push({ url, body: JSON.parse(options.body) });
      return { ok: false, status: 409, json: async () => ({}) };
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
  const slide = async (id, value) => {
    $(id).value = String(value);
    $(id).dispatchEvent(new window.Event('input'));
    await settle();
  };
  const type = async (id, value) => {
    $(id).value = String(value);
    $(id).dispatchEvent(new window.Event('change'));
    await settle();
  };
  const num = id => Number($(id).value);
  const dialog = () => $('stira-confirm');
  const escape = async () => {
    doc.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
    await settle();
  };
  const saved = hub.values.breaker;
  assert.equal(num('stability-warning'), saved.stability_warning);
  assert.equal(num('stability-warning-exit'), saved.stability_warning_exit);

  // --- moveSlider + orderExits: the critical exit pushes the warning exit up -------------
  await slide('stability-critical-exit', saved.stability_warning_exit);
  assert.equal(num('stability-critical-exit'), saved.stability_warning_exit);
  assert.equal(num('stability-warning-exit'), Math.round((saved.stability_warning_exit + 0.01) * 100) / 100);
  assert.equal($('note-stability-warning').hidden, false);
  assert.equal($('note-stability-warning').textContent,
    t('hub.note_warning_exit_raised', { exit: $('stability-warning-exit-value').textContent }));
  // Lowering the warning exit under the critical exit comes back above it (the safe side).
  const critExit = num('stability-critical-exit');
  await slide('stability-warning-exit', critExit - 0.05);
  assert.equal(num('stability-warning-exit'), Math.round((critExit + 0.01) * 100) / 100,
    'the warning exit returns above the critical exit');
  assert.equal($('note-stability-warning').textContent,
    t('hub.note_warning_exit_raised', { exit: $('stability-warning-exit-value').textContent }));
  // Never an error shown for a pair the page itself kept valid.
  assert.equal($('err-breaker-stability_critical_exit').hidden, true);
  assert.equal($('err-breaker-stability_warning_exit').hidden, true);
  // Lowering it while it is still above the critical exit is just a move (no note).
  await slide('stability-critical-exit', 0.45);
  await slide('stability-warning-exit', 0.75);
  assert.equal(num('stability-warning-exit'), 0.75);
  assert.equal($('note-stability-warning').hidden, true);
  $('hub-discard').click();
  await settle();
  assert.equal(num('stability-warning-exit'), saved.stability_warning_exit);

  // --- m-2: the sycophancy threshold is not rounded to its dull end ------------------------
  await type('sycophancy-threshold', '1.5');
  assert.equal($('sycophancy-threshold').value, '1.5', 'kept as typed, not rounded to 1');
  assert.equal($('err-breaker_details-sycophancy_threshold').hidden, false, 'shown as an error');
  assert.equal($('note-sycophancy-threshold').hidden, true, 'no "rounded" note');
  $('hub-save').click();
  await settle();
  assert.ok(!dialog() || !dialog().open, 'an invalid draft opens no dialog');
  assert.deepEqual(posts, [], 'an invalid draft is not posted');
  // Other details still round into their valves (the safe side), with a note.
  await type('critical-cooldown-turns', '1');
  assert.equal($('critical-cooldown-turns').value, '3');
  assert.equal($('note-critical-cooldown-turns').hidden, false);

  // --- M-1: switching the alert practically off asks the typed confirmation ----------------
  $('hub-discard').click();
  await settle();
  await type('sycophancy-threshold', '1');
  assert.equal($('err-breaker_details-sycophancy_threshold').hidden, true);
  assert.match($('danger-banner').textContent, new RegExp(t('hub.danger_sycophancy_off').slice(0, 12)));
  $('hub-save').click();
  await settle();
  assert.ok(dialog() && dialog().open, 'the danger dialog opened');
  const items = [...dialog().querySelectorAll('li')].map(li => li.textContent);
  assert.deepEqual(items, [t('hub.danger_sycophancy_off')]);
  assert.equal($('stira-confirm-ok').disabled, true, 'needs the typed word');
  await escape();
  assert.deepEqual(posts, [], 'Esc: nothing saved');
  $('hub-save').click();
  await settle();
  $('stira-confirm-input').value = t('hub.confirm_word');
  $('stira-confirm-input').dispatchEvent(new window.Event('input'));
  $('stira-confirm-ok').click();
  await settle();
  assert.equal(posts.length, 1);
  assert.deepEqual(posts[0].body.acknowledged_dangers, ['sycophancy_off']);
  assert.equal(posts[0].body.changes.breaker_details.sycophancy_threshold, 1);
  // A short window is the same danger.
  $('hub-discard').click();
  await settle();
  await type('sycophancy-window-size', '9');
  $('hub-save').click();
  await settle();
  assert.deepEqual([...dialog().querySelectorAll('li')].map(li => li.textContent), [t('hub.danger_sycophancy_off')]);
  await escape();
  // 0.94 is a normal value: no question.
  $('hub-discard').click();
  await settle();
  await type('sycophancy-threshold', '0.94');
  $('hub-save').click();
  await settle();
  assert.ok(!dialog().open, '0.94 saves without a dialog');
  assert.equal(posts.length, 2);
  assert.deepEqual(posts[1].body.acknowledged_dangers, []);

  // --- "Reset to defaults": the whole breaker set and the details, never empty -----------
  $('hub-discard').click();
  await settle();
  const defaults = hub.defaults;
  assert.equal($('hub-defaults').disabled, false);
  $('hub-defaults').click();
  await settle();
  const sliders = {
    stability_warning: 'stability-warning',
    stability_warning_exit: 'stability-warning-exit',
    stability_critical: 'stability-critical',
    stability_critical_exit: 'stability-critical-exit',
    bc_threshold: 'bc-threshold',
    bc_exit_threshold: 'bc-threshold-exit',
  };
  assert.deepEqual(Object.keys(defaults.breaker).sort(), Object.keys(sliders).sort(), 'defaults hold all six');
  for (const [key, id] of Object.entries(sliders)) {
    assert.equal(num(id), defaults.breaker[key], `${key} slider at its default`);
    const shown = $(`${id}-value`).textContent;
    assert.ok(shown && !/NaN|undefined/.test(shown), `${key} label shows a number (${shown})`);
  }
  for (const id of ['stability-warning-band', 'stability-critical-band', 'bc-threshold-band']) {
    assert.ok(!/NaN/.test($(id).style.width) && $(id).style.width !== '', `${id} has a width`);
  }
  assert.ok(!/NaN|undefined/.test($('pair-sum-stability-critical').textContent));
  const details = {
    bc_window_size: 'bc-window-size',
    critical_cooldown_turns: 'critical-cooldown-turns',
    sycophancy_threshold: 'sycophancy-threshold',
    sycophancy_window_size: 'sycophancy-window-size',
    hitl_timeout_sec: 'hitl-timeout-sec',
  };
  for (const [key, id] of Object.entries(details)) {
    assert.equal(num(id), defaults.breaker_details[key], `${key} at its default`);
  }
  assert.equal(doc.querySelectorAll('#sec-breaker .field-error:not([hidden])').length, 0,
    'no error after reset');
  assert.equal($('hub-save-status').textContent, t('hub.defaults_staged'));
  console.log('ok: pair ordering, sycophancy confirmation/no rounding, reset to defaults');
  window.close();
}

main().catch((err) => { console.error(err); process.exit(1); });
