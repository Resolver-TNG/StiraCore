// Settings hub "Screens" section (#477 / #809 N1) via jsdom: the Stage switch, the page "/"
// opens (the Stage is not selectable while the Stage is off), the env pin through the shared
// settings-surface part (input off + the variable's name, never its value), the change list,
// the save body (no confirmation), the frame rate kept in this browser only, axe.
// usage: node settings_screens_dom.cjs <settings.html> <off.json> <pinned.json> <kept.json> <marker> <defaults.json>
// (<off.json>: the Stage saved off; <defaults.json>: no file, the Stage on by default)
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
const FPS_KEY = 'stira.stage.fps';
const tick = () => new Promise(r => setTimeout(r, 0));
async function settle() { for (let i = 0; i < 30; i += 1) await tick(); }

async function openPage(html, hub) {
  const dom = new JSDOM(html, { runScripts: 'outside-only', pretendToBeVisual: true, url: 'http://127.0.0.1/settings' });
  const { window } = dom;
  polyfillDialog(window);
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
  for (const name of ['confirm_dialog.js', 'breaker_pairs.js', 'settings_surface.js', 'settings_hub.js']) {
    window.eval(fs.readFileSync(path.join(JS, name), 'utf8'));
  }
  await settle();
  const doc = window.document;
  const $ = id => doc.getElementById(id);
  const badge = (kind, group) => doc.querySelector(`.${kind}[data-group="${group}"]`).textContent;
  return { window, doc, $, posts, badge };
}

async function main() {
  const html = fs.readFileSync(process.argv[2], 'utf8');
  const standalone = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
  const pinned = JSON.parse(fs.readFileSync(process.argv[4], 'utf8'));
  const kept = JSON.parse(fs.readFileSync(process.argv[5], 'utf8'));
  const marker = process.argv[6];
  const defaults = JSON.parse(fs.readFileSync(process.argv[7], 'utf8'));
  assert.ok(marker && marker.length > 8, 'marker value');

  // --- Defaults: the Stage is on, "/" is the chat; the Stage can be chosen for "/" ---------
  {
    const { $ } = await openPage(html, defaults);
    assert.equal(defaults.values.screens.stage, true, 'on by default');
    assert.equal(defaults.values.screens.home, 'chat', '"/" stays the chat');
    assert.equal($('screens-stage').checked, true);
    assert.equal($('screens-stage').disabled, false);
    assert.equal($('screens-home-chat').checked, true);
    assert.equal($('screens-home-stage').disabled, false);
    assert.equal($('screens-home-note').hidden, true);
    assert.equal($('hub-save').disabled, true, 'nothing changed yet');
  }

  // --- Saved off: "/" is the chat; turn on, choose the Stage, save -------------------------
  {
    const { window, doc, $, posts, badge } = await openPage(html, standalone);
    const t = window.t;
    assert.notEqual(t('screens.sec'), 'screens.sec', 'catalog loaded');
    assert.equal(badge('timing-badge', 'screens'), t('hub.timing_next_start'));
    assert.equal($('screens-stage').checked, false);
    assert.equal($('screens-stage').disabled, false);
    assert.equal($('screens-home-chat').checked, true);
    // Stage off: the Stage cannot be chosen for "/", and the page says why.
    assert.equal($('screens-home-stage').disabled, true);
    assert.equal($('screens-home-note').hidden, false);
    assert.equal($('screens-home-note').textContent, t('screens.home_stage_off'));
    assert.equal($('screens-restart-note').textContent.trim(), t('screens.restart_note'));
    assert.equal($('hub-save').disabled, true, 'nothing changed yet');

    $('screens-stage').click();
    await settle();
    assert.equal($('screens-stage').checked, true);
    assert.equal($('screens-home-stage').disabled, false);
    assert.equal($('screens-home-note').hidden, true);
    assert.equal($('hub-save').disabled, false);
    $('screens-home-stage').click();
    await settle();
    assert.equal($('screens-home-stage').checked, true);

    // The change list in the page's words.
    const diff = [...doc.querySelectorAll('#hub-diff-list li')].map(li => li.textContent);
    assert.equal(diff.length, 2, diff.join('\n'));
    assert.ok(diff.some(line => line.startsWith(t('screens.stage')) && line.includes(t('hub.value_on'))), diff.join('\n'));
    assert.ok(diff.some(line => line.startsWith(t('screens.home')) && line.includes(t('base.stage'))), diff.join('\n'));

    // Switching the Stage off again with "/" moved to it: refused on the page, as the server does.
    $('screens-stage').click();
    await settle();
    assert.equal($('err-screens-home').hidden, false);
    assert.equal($('err-screens-home').textContent, t('screens.home_stage_off'));
    assert.equal($('hub-save').disabled, true);
    assert.ok($('screens-home').getAttribute('aria-describedby').split(' ').includes('err-screens-home'));
    $('screens-stage').click();
    await settle();
    assert.equal($('err-screens-home').hidden, true);

    // The frame rate: this browser's localStorage, stored as the slider moves, never posted.
    assert.equal(window.localStorage.getItem(FPS_KEY), null);
    assert.equal($('screens-fps').value, '30');
    assert.equal($('screens-fps-value').textContent, t('screens.fps_value', { n: '30' }));
    assert.equal($('screens-fps-reset').disabled, true);
    $('screens-fps').value = '60';
    $('screens-fps').dispatchEvent(new window.Event('input'));
    await settle();
    assert.equal(window.localStorage.getItem(FPS_KEY), '60');
    assert.equal($('screens-fps-value').textContent, t('screens.fps_value', { n: '60' }));
    assert.equal($('screens-fps-note').textContent, t('screens.fps_saved'));
    assert.equal($('screens-fps-reset').disabled, false);
    assert.equal(doc.querySelectorAll('#hub-diff-list li').length, 2, 'not part of the draft');
    $('screens-fps-reset').click();
    await settle();
    assert.equal(window.localStorage.getItem(FPS_KEY), null);
    assert.equal($('screens-fps').value, '30');
    assert.equal(posts.length, 0, 'nothing sent to the server');

    // axe over the section (jsdom: no color-contrast).
    window.eval(axeSource);
    const result = await window.axe.run($('sec-screens'), { rules: { 'color-contrast': { enabled: false } } });
    if (result.violations.length) {
      for (const v of result.violations) console.log(`[${v.impact}] ${v.id}: ${v.help} (${v.nodes.length})`);
      process.exit(1);
    }

    // Save: only the screens group, with no confirmation.
    $('hub-save').click();
    await settle();
    assert.equal(posts.length, 1, 'posted once, no confirmation needed');
    const { changes, acknowledged_dangers: acknowledged } = posts[0].body;
    assert.deepEqual(changes, { screens: { stage: true, home: 'stage' } });
    assert.ok(!acknowledged || acknowledged.length === 0, 'nothing to confirm');
    window.close();
  }

  // --- A stored frame rate is shown; a bad one is the default ------------------------------
  {
    const page = await openPage(html, standalone);
    page.window.localStorage.setItem(FPS_KEY, '500');
    page.$('screens-stage').click(); // any re-render
    await settle();
    assert.equal(page.$('screens-fps').value, '30', 'out of range = the default, as the Stage reads it');
    assert.equal(page.$('screens-fps-reset').disabled, true);
    page.window.close();
  }

  // --- STIRA_STAGE set: the switch is pinned and shows what the variable gives -------------
  {
    const { window, doc, $ } = await openPage(html, pinned);
    const t = window.t;
    assert.equal(pinned.values.screens.stage, true, 'saved on');
    assert.equal(pinned.stage_enabled, false, 'the variable turns it off');
    const input = $('screens-stage');
    assert.equal(input.getAttribute('data-surface-field'), 'gateway.stage_enabled');
    assert.equal(input.disabled, true);
    assert.equal(input.getAttribute('data-surface-pinned'), 'true');
    assert.equal(input.checked, false, 'what the next start uses, not the saved value');
    const note = $('pin-gateway-stage_enabled');
    assert.equal(note.hidden, false);
    assert.equal(note.textContent, t('surface.pinned_by', { name: 'STIRA_STAGE' }));
    assert.ok(input.getAttribute('aria-describedby').split(' ').includes(note.id));
    assert.equal($('screens-stage-live').textContent, t('screens.pinned_live'));
    assert.equal($('screens-home-stage').disabled, true, 'the Stage is off: not selectable');
    assert.equal(doc.documentElement.outerHTML.includes(marker), false, 'never the value');
    window.close();
  }

  // --- A pair already in the file (home = stage, Stage off) is shown and blocks nothing ----
  {
    const { window, $ } = await openPage(html, kept);
    const t = window.t;
    assert.equal($('screens-stage').checked, false);
    assert.equal($('screens-home-stage').checked, true);
    assert.equal($('screens-home-stage').disabled, false);
    assert.equal($('screens-home-note').textContent, t('screens.home_falls_back'));
    assert.equal($('err-screens-home').hidden, true);
    $('hb-enabled').click();
    await settle();
    assert.equal($('hub-save').disabled, false, 'an unrelated change can be saved');
    window.close();
  }

  console.log('ok: screens section');
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
