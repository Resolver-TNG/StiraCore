// Settings hub: answers that arrive while something else is on the way (#601 E7 / E9 / E10).
// usage: node settings_hub_races_dom.cjs <settings.html> <hub.json> [case]
// Each case runs on its own (tests/test_settings_hub_races.py runs them one by one).
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
const clone = v => JSON.parse(JSON.stringify(v));

// A hub state after ``changes`` were saved (the server's answer, enough for the page).
function savedState(base, changes) {
  const next = clone(base);
  for (const [group, values] of Object.entries(changes || {})) {
    next.values[group] = { ...next.values[group], ...values };
  }
  next.version = `${base.version}-next`;
  return next;
}

async function page(hub, route) {
  const html = fs.readFileSync(process.argv[2], 'utf8');
  const dom = new JSDOM(html, { runScripts: 'outside-only', pretendToBeVisual: true, url: 'http://127.0.0.1/settings' });
  const { window } = dom;
  polyfillDialog(window);
  if (!window.CSS) window.CSS = {};
  if (!window.CSS.escape) window.CSS.escape = s => String(s).replace(/[^a-zA-Z0-9_-]/g, c => `\\${c}`);
  const json = (body, status = 200) => ({ ok: status >= 200 && status < 300, status, json: async () => body });
  window.fetch = async (url, options = {}) => {
    const answer = await route(url, options, hub);
    if (answer) return json(answer.body, answer.status || 200);
    if (url.startsWith('/api/config/history')) return json({ entries: [] });
    if (url.startsWith('/api/config/hub')) return json(hub);
    return json({});
  };
  window.setInterval = () => 0;
  window.eval(shimSource());
  window.eval(fs.readFileSync(path.join(JS, 'confirm_dialog.js'), 'utf8'));
  window.eval(fs.readFileSync(path.join(JS, 'breaker_pairs.js'), 'utf8'));
  return window;
}
const boot = async (window) => {
  window.eval(fs.readFileSync(path.join(JS, 'settings_hub.js'), 'utf8'));
  await settle();
};

function gate() {
  let open;
  const wait = new Promise(r => { open = r; });
  return { wait, open: () => open() };
}

const CASES = {};

// --- E9: a value changed while the save is on the way is kept -----------------------------
CASES.e9_values = async (hub) => {
  {
    const post = gate();
    let sent = null;
    const window = await page(hub, async (url, options, base) => {
      if (url === '/api/config/hub' && options.method === 'POST') {
        sent = JSON.parse(options.body);
        await post.wait;
        return { body: { state: savedState(base, sent.changes) } };
      }
      return null;
    });
    await boot(window);
    const $ = id => window.document.getElementById(id);
    const warning = hub.values.breaker.stability_warning;
    const next = Math.round((warning - 0.05) * 100) / 100;
    $('stability-warning').value = String(next);
    $('stability-warning').dispatchEvent(new window.Event('input'));
    await settle();
    $('hub-save').click();
    await settle();
    assert.ok(sent, 'the save was sent');
    assert.deepEqual(Object.keys(sent.changes), ['breaker']);
    // typed while the POST is on the way
    const typed = hub.values.breaker_details.bc_window_size + 5;
    $('bc-window-size').value = String(typed);
    $('bc-window-size').dispatchEvent(new window.Event('change'));
    await settle();
    post.open();
    await settle();
    assert.equal($('bc-window-size').value, String(typed), 'the value typed during the save is kept');
    assert.equal(Number($('stability-warning').value), next, 'the saved value stays');
    assert.equal($('hub-save').disabled, false, 'the kept edit can be saved next');
    assert.match($('hub-dirty-count').textContent, /1/, 'one unsaved change: the one typed during the save');
    window.close();
  }
};

// --- E7: the model section's "saved" event during the hub save does not read the older state
CASES.e7_model_event = async (hub) => {
  {
    const post = gate();
    let gets = 0;
    let saved = null;
    const window = await page(hub, async (url, options, base) => {
      if (url === '/api/config/hub' && options.method === 'POST') {
        saved = savedState(base, JSON.parse(options.body).changes);
        const answer = { body: { state: saved } };
        post.open();
        return answer;
      }
      if (url === '/api/config/hub' && gets++ > 0) {
        await post.wait; // the older state, answered after the POST
        await settle();
        return { body: clone(base) };
      }
      return null;
    });
    let modelDirty = true;
    window.StiraModelSection = {
      dirty: () => modelDirty,
      label: () => 'local / model',
      reset() {},
      save: async () => {
        modelDirty = false;
        window.document.dispatchEvent(new window.CustomEvent('stira:model-saved'));
        return true;
      },
    };
    await boot(window);
    const $ = id => window.document.getElementById(id);
    const next = Math.round((hub.values.breaker.stability_warning - 0.05) * 100) / 100;
    $('stability-warning').value = String(next);
    $('stability-warning').dispatchEvent(new window.Event('input'));
    await settle();
    $('hub-save').click();
    for (let i = 0; i < 4; i += 1) await settle();
    assert.ok(saved, 'the hub change was posted');
    assert.equal(Number($('stability-warning').value), next);
    assert.equal($('hub-save').disabled, true, 'nothing unsaved after the save (the older GET did not come back)');
    window.close();
  }
};

// --- E10: the rollback retried after the confirmation keeps the lock until it ends ---------
CASES.e10_rollback = async (hub) => {
  {
    const retry = gate();
    const posts = [];
    const entry = {
      id: 'h1', at: '2026-10-23T00:00:00Z', source: 'manual',
      changes: [{ field: 'breaker.stability_warning', from: 0.5, to: hub.values.breaker.stability_warning }],
    };
    const window = await page(hub, async (url, options, base) => {
      if (url.startsWith('/api/config/history') && !options.method) return { body: { entries: [entry] } };
      if (url.includes('/rollback')) {
        const body = JSON.parse(options.body);
        posts.push(body);
        if (!body.acknowledged_dangers.length) {
          return { status: 409, body: { error_code: 'needs_confirmation', needs_confirmation: ['breaker_loosened'] } };
        }
        await retry.wait;
        return { body: { state: savedState(base, { breaker: { stability_warning: 0.5 } }) } };
      }
      return null;
    });
    await boot(window);
    const $ = id => window.document.getElementById(id);
    const t = (key, params) => window.t(key, params);
    const button = () => $('history-list').querySelector('button');
    assert.ok(button(), 'the entry can be rolled back');
    button().click();
    await settle();
    assert.equal($('stira-confirm').open, true, 'the confirmation is asked');
    $('stira-confirm-input').value = t('hub.confirm_word');
    $('stira-confirm-input').dispatchEvent(new window.Event('input'));
    $('stira-confirm-ok').click();
    await settle();
    assert.equal(posts.length, 2, 'retried after the confirmation');
    assert.equal(button().disabled, true, 'no second rollback while the retried one is on the way');
    assert.equal($('hub-defaults').disabled, true, 'no save while the retried rollback is on the way');
    button().click();
    await settle();
    assert.equal(posts.length, 2, 'a click meanwhile sends nothing');
    retry.open();
    await settle();
    assert.equal($('history-status').textContent, t('hub.rollback_done'));
    assert.equal(button().disabled, false, 'the lock ends with the retried rollback');
    window.close();
  }
};

// A model section whose save waits for ``gate`` (the hub's model-only save path).
function modelSection(window, wait) {
  let dirty = true;
  window.StiraModelSection = {
    dirty: () => dirty,
    label: () => 'local / model',
    reset() {},
    save: async () => { await wait; dirty = false; return true; },
  };
}

// --- E9 (model only): a value typed while only the model is being saved is kept ----------
CASES.e9_model_only_values = async (hub) => {
  const model = gate();
  const window = await page(hub, async () => null);
  modelSection(window, model.wait);
  await boot(window);
  const $ = id => window.document.getElementById(id);
  assert.equal($('hub-save').disabled, false, 'the model choice is the unsaved change');
  $('hub-save').click();
  await settle();
  const typed = hub.values.breaker_details.bc_window_size + 5;
  $('bc-window-size').value = String(typed);
  $('bc-window-size').dispatchEvent(new window.Event('change'));
  await settle();
  model.open();
  await settle();
  assert.equal($('bc-window-size').value, String(typed), 'the value typed during the model save is kept');
  assert.equal($('hub-save').disabled, false, 'and can be saved next');
  window.close();
};

// --- E9 (scope): a scope radio picked while a save is on the way stays picked -------------
// "custom" has no preset: picking it changes the choice only (the roots stay), so the draft
// alone cannot tell that it was changed.
async function scopeCase(hub, modelOnly) {
  const post = gate();
  const window = await page(hub, async (url, options, base) => {
    if (url === '/api/config/hub' && options.method === 'POST') {
      const sent = JSON.parse(options.body);
      await post.wait;
      return { body: { state: savedState(base, sent.changes) } };
    }
    return null;
  });
  if (modelOnly) modelSection(window, post.wait);
  await boot(window);
  const doc = window.document;
  const $ = id => doc.getElementById(id);
  const radio = value => doc.querySelector(`input[name="write-scope"][value="${value}"]`);
  assert.equal(radio('custom').checked, false, 'a built-in scope at the start');
  if (!modelOnly) {
    const next = Math.round((hub.values.breaker.stability_warning - 0.05) * 100) / 100;
    $('stability-warning').value = String(next);
    $('stability-warning').dispatchEvent(new window.Event('input'));
    await settle();
  }
  $('hub-save').click();
  await settle();
  radio('custom').checked = true;
  radio('custom').dispatchEvent(new window.Event('change'));
  await settle();
  assert.equal($('write-roots').readOnly, false, 'custom: the roots can be edited');
  post.open();
  await settle();
  assert.equal(radio('custom').checked, true, 'the scope picked during the save is still picked');
  assert.equal($('write-roots').readOnly, false, 'and the roots stay editable');
  window.close();
}
CASES.e9_scope_save = (hub) => scopeCase(hub, false);
CASES.e9_scope_model_only = (hub) => scopeCase(hub, true);

async function main() {
  const hub = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
  const wanted = process.argv[4];
  const names = wanted ? [wanted] : Object.keys(CASES);
  for (const name of names) {
    if (!CASES[name]) throw new Error(`unknown case ${name}`);
    await CASES[name](hub);
  }
  console.log('OK');
}

main().catch((err) => {
  console.error(err && err.stack ? err.stack : err);
  process.exit(1);
});
