// The breaker section on the #490 settings surface (#471), via jsdom on the real page.
// usage: node settings_breaker_surface_dom.cjs <settings.html> <hub.json> <secret-value>
// The hub answer has STIRA_PROBE_SYCOPHANCY pinning stira.sycophancy_threshold (its value is
// <secret-value>, which must never reach the page), a saved detail waiting for a restart and
// a saved exit (next turn). Exit codes: 0 = ok, 1 = failure, 77 = deps missing (skip).
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

const BREAKER_INPUTS = [
  'stability-warning', 'stability-warning-exit', 'stability-critical', 'stability-critical-exit',
  'bc-threshold', 'bc-threshold-exit', 'bc-window-size', 'critical-cooldown-turns',
  'decay-min-interval-sec', 'sycophancy-threshold', 'sycophancy-window-size',
  'hitl-timeout-sec', 'hitl-max-edit-rounds',
];

async function main() {
  const html = fs.readFileSync(process.argv[2], 'utf8');
  const hub = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
  const secret = process.argv[4];
  assert.ok(secret && secret.length > 8, 'the secret marker is given');
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
  // Same order as the page: dialog, breaker pairs, the surface, then the hub.
  window.eval(fs.readFileSync(path.join(JS, 'confirm_dialog.js'), 'utf8'));
  window.eval(fs.readFileSync(path.join(JS, 'breaker_pairs.js'), 'utf8'));
  window.eval(fs.readFileSync(path.join(JS, 'settings_surface.js'), 'utf8'));
  window.eval(fs.readFileSync(path.join(JS, 'settings_hub.js'), 'utf8'));
  await settle();
  const doc = window.document;
  const $ = id => doc.getElementById(id);
  const t = (key, params) => window.t(key, params);
  const NAME = 'STIRA_PROBE_SYCOPHANCY';
  const NOTE_ID = 'pin-stira-sycophancy_threshold';

  const checkPinned = (when) => {
    const input = $('sycophancy-threshold');
    assert.equal(input.disabled, true, `${when}: the pinned field is not editable`);
    assert.equal(input.getAttribute('data-surface-pinned'), 'true', when);
    const notes = doc.querySelectorAll(`#${NOTE_ID}`);
    assert.equal(notes.length, 1, `${when}: one reason, not one per render`);
    assert.equal(notes[0].hidden, false, when);
    assert.equal(notes[0].textContent, t('surface.pinned_by', { name: NAME }), `${when}: the name`);
    assert.ok(notes[0].closest('.detail-row'), `${when}: the reason sits in the field's row`);
    const described = (input.getAttribute('aria-describedby') || '').split(/\s+/);
    assert.ok(described.includes(NOTE_ID), `${when}: the reason is linked`);
    for (const id of ['desc-sycophancy-threshold', 'err-breaker_details-sycophancy_threshold']) {
      assert.ok(described.includes(id), `${when}: keeps ${id}`);
    }
    for (const id of BREAKER_INPUTS.filter(id => id !== 'sycophancy-threshold')) {
      assert.equal($(id).disabled, false, `${when}: ${id} stays editable`);
      assert.equal($(id).hasAttribute('data-surface-pinned'), false, `${when}: ${id} not pinned`);
    }
    assert.ok(!doc.documentElement.outerHTML.includes(secret), `${when}: the value never reaches the page`);
    assert.ok(!doc.documentElement.outerHTML.includes('MARKER'), `${when}: no part of the value`);
  };

  // Every breaker input carries its manifest id.
  for (const id of BREAKER_INPUTS) {
    assert.equal($(id).getAttribute('data-surface-field'), `stira.${$(id).name}`, id);
  }
  checkPinned('after load');
  // The value shown is the engine's/saved one, not the variable's (the page never sees it).
  assert.equal(Number($('sycophancy-threshold').value), hub.values.breaker_details.sycophancy_threshold);

  // Restart banner: the saved detail is one change "Breaker details"; the exit (next turn) not.
  const banner = $('restart-banner');
  assert.equal(banner.hidden, false, 'the restart banner shows');
  assert.ok(banner.textContent.includes(t('surface.grp_breaker_details')), banner.textContent);
  assert.ok(!banner.textContent.includes(t('hub.breaker_title')), 'an exit does not wait for a restart');
  // "Where settings live": the variable by name, with the field's own label.
  // (Other variables of the machine running the tests may be listed too.)
  const pinned = [...$('pinned-list').querySelectorAll('li')].map(li => li.textContent)
    .filter(text => text.startsWith(NAME));
  assert.equal(pinned.length, 1);
  assert.ok(pinned[0].startsWith(NAME), pinned[0]);
  assert.ok(pinned[0].includes(t('hub.det_syc')), `labelled: ${pinned[0]}`);

  // A full re-render (moving a slider) keeps the pin and adds no second reason.
  $('stability-critical').value = '0.35';
  $('stability-critical').dispatchEvent(new window.Event('input'));
  await settle();
  checkPinned('after a slider move');
  // Saving sends the moved slider only: nothing for the pinned field.
  $('hub-save').click();
  await settle();
  assert.equal(posts.length, 1, 'saved without a question');
  assert.deepEqual(Object.keys(posts[0].body.changes), ['breaker']);
  assert.ok(!JSON.stringify(posts[0].body).includes('sycophancy_threshold'));

  // A later answer without the variable gives the field back.
  const freed = JSON.parse(JSON.stringify(hub));
  freed.surface.fields['stira.sycophancy_threshold'].pinned_by = null;
  freed.surface.pinned = [];
  window.StiraSettingsSurface.applyPins(doc, freed.surface);
  assert.equal($('sycophancy-threshold').disabled, false, 'editable again');
  assert.equal($(NOTE_ID).hidden, true, 'the reason is hidden');
  window.StiraSettingsSurface.applyPins(doc, hub.surface);
  checkPinned('pinned again');

  // axe over the breaker section (details open) and the location card.
  $('breaker-details').open = true;
  window.eval(axeSource);
  const rules = { rules: { 'color-contrast': { enabled: false } } };
  const problems = [];
  let passes = 0;
  for (const scope of [$('sec-breaker'), $('sec-location')]) {
    const result = await window.axe.run(scope, rules);
    passes += result.passes.length;
    for (const v of result.violations) {
      problems.push(`[${v.impact}] ${v.id}: ${v.help}\n    ${v.nodes.map(n => n.html).join('\n    ')}`);
    }
  }
  assert.deepEqual(problems, [], 'axe');
  console.log(`ok: breaker surface pin/banner/label, ${passes} axe passes`);
  window.close();
}

main().catch((err) => { console.error(err); process.exit(1); });
