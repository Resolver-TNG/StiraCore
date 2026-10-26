// Settings hub cost section (#470) via jsdom: automatic cap recommended, model-limit warning,
// field errors tied to inputs, env pins through the shared settings-surface part (#469: input
// off + the variable's name, never its value; the location card), the save body, axe.
// usage: node settings_cost_dom.cjs <settings.html> <hub.json> <no-runtime.json> <big-cap.json> <marker>
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

// One page load in jsdom with ``hub`` as /api/config/hub; ``rollback`` answers the history's
// rollback POST, ``history`` the history list.
async function openPage(html, hub, { history = [], rollback = null } = {}) {
  const dom = new JSDOM(html, { runScripts: 'outside-only', pretendToBeVisual: true, url: 'http://127.0.0.1/settings' });
  const { window } = dom;
  polyfillDialog(window);
  const posts = [];
  window.fetch = async (url, options = {}) => {
    const json = body => ({ ok: true, status: 200, json: async () => body });
    if (options.method === 'POST') {
      posts.push({ url, body: JSON.parse(options.body) });
      if (rollback && url.includes('/rollback')) return rollback;
      return { ok: false, status: 409, json: async () => ({}) };
    }
    if (url.startsWith('/api/config/history')) return json({ entries: history });
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
  const type = async (id, value) => {
    $(id).value = value;
    $(id).dispatchEvent(new window.Event('input'));
    await settle();
  };
  const badge = group => doc.querySelector(`.timing-badge[data-group="${group}"]`).textContent;
  return { window, doc, $, type, posts, badge };
}

async function main() {
  const html = fs.readFileSync(process.argv[2], 'utf8');
  const hub = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
  const noRuntime = JSON.parse(fs.readFileSync(process.argv[4], 'utf8'));
  const bigCap = JSON.parse(fs.readFileSync(process.argv[5], 'utf8'));
  const marker = process.argv[6];
  assert.ok(marker && marker.length > 8, 'marker value');
  // The running agent uses the env value (the opposite of the saved one).
  assert.equal(hub.apply.cache.running.enabled, !hub.values.cache.enabled);
  const { window, doc, $, type, posts, badge } = await openPage(html, hub);

  // Apply-timing badges in words (M1): the cap from the next turn, cache/context at restart.
  assert.equal(hub.apply.output.applies, 'next_turn');
  assert.equal(hub.apply.cache.applies, 'restart');
  assert.equal(badge('output'), window.t('hub.timing_next_turn'));
  assert.equal(badge('cache'), window.t('hub.timing_restart'));
  assert.equal(badge('context'), window.t('hub.timing_restart'));
  assert.notEqual(window.t('hub.timing_restart'), 'hub.timing_restart');
  const info = hub.cost.model;

  // Automatic is the default and recommended; the number box shows the automatic value.
  assert.equal($('cost-output-auto').checked, true);
  assert.equal($('cost-max-output').disabled, true);
  assert.equal($('cost-max-output').value, String(info.auto_max_output_tokens));
  assert.match($('cost-output-auto-desc').textContent, /32[,.]?000/);
  assert.equal($('cost-max-output-warn').hidden, true);
  // #416: the fixture's model thinks within the cap: the description names the reserve.
  const fmt = n => n.toLocaleString(doc.documentElement.lang || undefined);
  assert.equal(info.auto_max_output_source, 'model');
  assert.equal(info.thinking_reserve, 32000);
  assert.equal($('cost-output-auto-desc').textContent,
    window.t('cost.output_auto_desc_thinking', { n: fmt(info.auto_max_output_tokens), reserve: fmt(info.thinking_reserve) }));
  assert.equal($('hub-save').disabled, true, 'nothing changed yet');

  // "Set a number": prefilled, focused, savable.
  $('cost-output-fixed').checked = true;
  $('cost-output-fixed').dispatchEvent(new window.Event('change'));
  await settle();
  assert.equal($('cost-max-output').disabled, false);
  assert.equal(doc.activeElement, $('cost-max-output'));
  // Above the model's documented limit: a warning, not an error.
  await type('cost-max-output', String(info.model_max_output + 1));
  assert.equal($('cost-max-output-warn').hidden, false, 'warning above the model limit');
  assert.equal($('hub-save').disabled, false);
  // Out of range: a field error on the input, save off.
  await type('cost-max-output', '0');
  assert.equal($('err-output-max_output_tokens').hidden, false);
  assert.equal($('cost-max-output').getAttribute('aria-invalid'), 'true');
  assert.equal($('hub-save').disabled, true);
  await type('cost-max-output', '1.5');
  assert.equal($('err-output-max_output_tokens').textContent, window.t('hub.err_not_integer'));
  await type('cost-max-output', '64000');
  assert.equal($('err-output-max_output_tokens').hidden, true);
  assert.equal($('cost-max-output').hasAttribute('aria-invalid'), false);
  assert.equal($('cost-max-output-warn').hidden, true);

  // Context option errors stay on their own field (preview must be below the offload size).
  await type('cost-ctx-preview_tokens', '5000');
  assert.equal($('err-context-preview_tokens').hidden, false);
  assert.equal($('err-context-offload_threshold_tokens').hidden, true);
  assert.equal($('hub-save').disabled, true);
  await type('cost-ctx-preview_tokens', '500');
  // Empty context window = automatic (null), not an error.
  await type('cost-ctx-context_window_tokens', '');
  assert.equal($('err-context-context_window_tokens').hidden, true);

  // The env-pinned switches go through the shared surface part (settings_surface.js applyPins):
  // off, marked, and its note names the variable; the cost section only adds which value it shows.
  for (const [id, surfaceId, name] of [
    ['cost-cache-enabled', 'gateway.prompt_cache', 'STIRA_PROMPT_CACHE'],
    ['cost-context-enabled', 'gateway.context_management', 'STIRA_CONTEXT_MANAGEMENT'],
  ]) {
    const input = $(id);
    assert.equal(input.getAttribute('data-surface-field'), surfaceId, id);
    assert.equal(input.disabled, true, `${id} disabled`);
    assert.equal(input.getAttribute('data-surface-pinned'), 'true', `${id} marked by applyPins`);
    const note = $(`pin-${surfaceId.replace('.', '-')}`);
    assert.equal(note.hidden, false, `${id} note shown`);
    assert.equal(note.textContent, window.t('surface.pinned_by', { name }), `${id}: the variable's name`);
    assert.ok(input.getAttribute('aria-describedby').split(' ').includes(note.id), `${id} described by the note`);
    assert.equal($(`${id}-live`).hidden, false, `${id}: which value it shows`);
    assert.equal($(`${id}-live`).textContent, window.t('cost.pinned_live'));
  }
  assert.equal($('cost-cache-enabled').checked, !hub.values.cache.enabled, 'a pinned switch shows the running value');
  assert.equal($('cost-context-enabled').checked, hub.apply.context.running.enabled, 'the running value');
  // Exactly one note per pinned input (the section does not add its own).
  assert.equal(doc.querySelectorAll('#sec-cost .pin-note:not([hidden])').length, 2);
  // Not pinned: editable, no note, no data-surface-pinned.
  assert.equal($('cost-cache-ttl').disabled, false);
  assert.equal($('cost-cache-ttl').hasAttribute('data-surface-pinned'), false);
  assert.equal($('pin-gateway-prompt_cache_ttl').hidden, true);
  assert.equal($('cost-cache-ttl-live').hidden, true);
  // Where it comes from and when it applies: the location card lists both variables by name,
  // with the field and "after restart" (the shared part), never the value.
  const pinnedRows = [...doc.querySelectorAll('#pinned-list li')].map(li => li.textContent);
  const cacheRow = pinnedRows.find(row => row.startsWith('STIRA_PROMPT_CACHE '));
  assert.ok(cacheRow && cacheRow.includes(window.t('surface.grp_prompt_cache')), pinnedRows.join(' | '));
  assert.ok(cacheRow.includes(window.t('hub.timing_restart')), cacheRow);
  const ctxRow = pinnedRows.find(row => row.startsWith('STIRA_CONTEXT_MANAGEMENT '));
  assert.ok(ctxRow && ctxRow.includes(window.t('surface.grp_context_management')), pinnedRows.join(' | '));
  assert.equal(doc.documentElement.outerHTML.includes(marker), false, 'the env value is never on the page');
  // Freed (the variable removed, the page polled): the shared part turns the input back on.
  {
    const freed = JSON.parse(JSON.stringify(hub.surface));
    freed.fields['gateway.context_management'].pinned_by = null;
    window.StiraSettingsSurface.applyPins(doc, freed, $('sec-cost'));
    assert.equal($('cost-context-enabled').disabled, false);
    assert.equal($('pin-gateway-context_management').hidden, true);
    window.StiraSettingsSurface.applyPins(doc, hub.surface, $('sec-cost'));
    assert.equal($('cost-context-enabled').disabled, true);
  }
  $('cost-cache-ttl').value = '1h';
  $('cost-cache-ttl').dispatchEvent(new window.Event('change'));
  await settle();

  // The footer lists the changes in words.
  const diff = [...doc.querySelectorAll('#hub-diff-list li')].map(li => li.textContent).join('\n');
  assert.match(diff, new RegExp(window.t('cost.output_title').replace(/[()（）]/g, '.')));
  assert.match(diff, /64[,.]?000/);

  // axe over the section (jsdom: no color-contrast), details open so every input is checked.
  $('cost-context-details').open = true;
  window.eval(axeSource);
  const result = await window.axe.run($('sec-cost'), { rules: { 'color-contrast': { enabled: false } } });
  if (result.violations.length) {
    for (const v of result.violations) console.log(`[${v.impact}] ${v.id}: ${v.help} (${v.nodes.length})`);
    process.exit(1);
  }

  // Save: only the changed groups go out, with the typed values.
  $('hub-save').click();
  await settle();
  assert.equal(posts.length, 1, 'posted once, no confirmation needed');
  const { changes } = posts[0].body;
  assert.deepEqual(Object.keys(changes).sort(), ['cache', 'context', 'output']);
  assert.equal(changes.output.max_output_tokens, 64000);
  assert.equal(changes.cache.ttl, '1h');
  assert.equal(changes.cache.enabled, hub.values.cache.enabled, 'the pinned switch is sent unchanged');
  assert.equal(changes.context.enabled, hub.values.context.enabled, 'the pinned switch is sent unchanged');
  assert.equal(changes.context.preview_tokens, 500);
  assert.equal(changes.context.context_window_tokens, null);

  // Back to automatic sends null again (the typed number is remembered for the radio).
  $('cost-output-auto').checked = true;
  $('cost-output-auto').dispatchEvent(new window.Event('change'));
  await settle();
  assert.equal($('cost-max-output').disabled, true);
  $('cost-output-fixed').checked = true;
  $('cost-output-fixed').dispatchEvent(new window.Event('change'));
  await settle();
  assert.equal($('cost-max-output').value, '64000', 'the earlier number comes back');
  window.close();

  // No running agent: a pinned switch does not show the saved value (it may be the opposite of
  // the env value, which is never sent); the badges say "at next start".
  {
    const entry = {
      id: 'e1', at: '2026-10-19T00:00:00Z', source: 'manual',
      changes: [{ field: 'cache.enabled', from: false, to: true }],
    };
    const refused = {
      ok: false, status: 400,
      json: async () => ({
        error: 'Cannot roll back.',
        errors: [{ field: 'cache.enabled', code: 'env_pinned', params: { env: 'STIRA_PROMPT_CACHE' } }],
      }),
    };
    const page = await openPage(html, noRuntime, { history: [entry], rollback: refused });
    assert.equal(noRuntime.apply.cache.running, undefined);
    assert.equal(page.badge('cache'), page.window.t('hub.timing_next_start'));
    assert.equal(page.$('cost-cache-enabled').disabled, true);
    assert.equal(page.$('cost-cache-enabled').indeterminate, true, 'unknown here, not the saved value');
    assert.equal(page.$('cost-cache-enabled').checked, false);
    assert.equal(page.$('cost-cache-enabled-live').hidden, false);
    assert.equal(page.$('cost-cache-enabled-live').textContent, page.window.t('cost.pinned_unknown'));
    assert.equal(page.$('pin-gateway-prompt_cache').textContent,
      page.window.t('surface.pinned_by', { name: 'STIRA_PROMPT_CACHE' }));
    assert.equal(page.$('cost-context-enabled').indeterminate, true, 'pinned too: unknown here');
    assert.notEqual(page.$('cost-cache-ttl').selectedIndex, -1, 'not pinned: the saved value');
    assert.equal(page.doc.documentElement.outerHTML.includes(marker), false);
    // A rollback refused because the switch is now pinned says why in the history.
    const button = page.doc.querySelector('#history-list button');
    assert.ok(button && !button.disabled, 'rollback button');
    button.click();
    await settle();
    assert.equal(page.posts.length, 1);
    const status = page.$('history-status').textContent;
    assert.match(status, /STIRA_PROMPT_CACHE/);
    assert.ok(status.includes(page.window.t('cost.cache_title')), status);
    page.window.close();
  }

  // #416: a model whose limit is not recorded: automatic = the provider's default, and the page
  // says so in automatic mode too (a reasoning model can use it up on thinking alone).
  {
    const unknown = JSON.parse(JSON.stringify(hub));
    unknown.cost.model = { ...unknown.cost.model, auto_max_output_tokens: 8192, model_max_output: null,
      auto_max_output_source: 'provider_default', thinking_reserve: null };
    const page = await openPage(html, unknown);
    const fmtU = n => n.toLocaleString(page.doc.documentElement.lang || undefined);
    assert.equal(page.$('cost-output-auto').checked, true);
    assert.equal(page.$('cost-output-auto-desc').textContent, page.window.t('cost.output_auto_desc_unknown', { n: fmtU(8192) }));
    assert.equal(page.$('cost-max-output-warn').hidden, false, 'shown in automatic mode');
    assert.equal(page.$('cost-max-output-warn').textContent, page.window.t('cost.output_auto_warn_unknown'));
    assert.notEqual(page.window.t('cost.output_auto_warn_unknown'), 'cost.output_auto_warn_unknown');
    // A number for it keeps the existing "limit unknown" warning.
    page.$('cost-output-fixed').checked = true;
    page.$('cost-output-fixed').dispatchEvent(new page.window.Event('change'));
    await settle();
    assert.equal(page.$('cost-max-output-warn').textContent,
      page.window.t('cost.output_warn_unknown', { auto: fmtU(8192) }));
    page.window.close();
  }

  // A cap in the file above the sanity bound (the chat server accepts it) shows no error and
  // does not block saving another change (M2, mirrored on the page).
  {
    const page = await openPage(html, bigCap);
    assert.equal(bigCap.values.output.max_output_tokens, 2000000);
    assert.equal(page.$('cost-output-fixed').checked, true);
    assert.equal(page.$('err-output-max_output_tokens').hidden, true);
    page.$('cost-cache-ttl').value = '1h';
    page.$('cost-cache-ttl').dispatchEvent(new page.window.Event('change'));
    await settle();
    assert.equal(page.$('hub-save').disabled, false, 'unrelated change can be saved');
    page.$('hub-save').click();
    await settle();
    assert.equal(page.posts.length, 1);
    assert.equal(page.posts[0].body.changes.cache.ttl, '1h');
    // Changing the cap itself is still checked.
    await page.type('cost-max-output', '3000000');
    assert.equal(page.$('err-output-max_output_tokens').hidden, false);
    assert.equal(page.$('hub-save').disabled, true);
    page.window.close();
  }
  console.log('ok: cost section (auto/fixed, warnings, field errors, env pin, badges, rollback reason, save body, axe)');
}

main().catch((err) => { console.error(err); process.exit(1); });
