// Settings page: the model section's region / organization-variable fields (#476) and the
// usage card (#195) via jsdom, with real API answers as fixtures.
// usage: node settings_model_usage_dom.cjs <model|usage> <settings.html> <fixture.json> <marker>
// Exit codes: 0 = ok, 1 = failure, 77 = deps missing (skip).
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const { shimSource } = require('../ui_shim.cjs');

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
const answer = (status, body) => ({ ok: status < 400, status, json: async () => body });

// One page load with settings.js only. ``routes``: GET url prefix -> () => answer;
// ``posts``: answers of the POSTs, in order.
async function openPage(html, routes, posts = []) {
  const dom = new JSDOM(html, { runScripts: 'outside-only', pretendToBeVisual: true, url: 'http://127.0.0.1/settings' });
  const { window } = dom;
  const sent = [];
  const gets = [];
  window.fetch = async (url, options = {}) => {
    if (options.method === 'POST') {
      sent.push({ url, body: JSON.parse(options.body) });
      return posts.shift() || answer(500, {});
    }
    gets.push(url);
    const prefix = Object.keys(routes).find(key => url.startsWith(key));
    return prefix ? routes[prefix]() : answer(404, {});
  };
  window.eval(shimSource());
  window.eval(fs.readFileSync(path.join(JS, 'settings.js'), 'utf8'));
  await settle();
  const doc = window.document;
  const $ = id => doc.getElementById(id);
  const fire = async (id, kind) => { $(id).dispatchEvent(new window.Event(kind, { bubbles: true })); await settle(); };
  const type = async (id, value) => { $(id).value = value; await fire(id, 'input'); await fire(id, 'change'); };
  const click = async (id) => { $(id).click(); await settle(); };
  return { window, doc, $, type, fire, click, sent, gets };
}

const soul = () => answer(200, { path: '', exists: false, preview: '' });

async function modelMode(html, fixture, marker) {
  const { providers, pinned } = fixture;
  const posts = [
    answer(409, { error: 'The model selection changed since this page loaded.', error_code: 'conflict', version: 'other' }),
    answer(400, { error: 'region must be an AWS region name such as ap-northeast-1.', error_code: 'invalid_region', field: 'region' }),
    answer(500, { error: `Traceback ${marker}` }),
    answer(409, { error: 'An environment variable pins this field.', error_code: 'pinned_by_env', field: 'region', env: 'AWS_REGION' }),
    answer(400, { error: 'A required environment variable is not set.', error_code: 'missing_credential', reason: 'missing_credential', env: 'STIRA_TEST_ORG' }),
  ];
  const routes = { '/api/config/providers': () => answer(200, providers), '/api/config/soul': soul };
  const { window, doc, $, type, fire, click, sent, gets } = await openPage(html, routes, posts);
  const t = window.t;
  const shown = () => $('model-error').hidden ? '' : $('model-error').textContent;
  const loads = () => gets.filter(url => url.startsWith('/api/config/providers')).length;

  // Bedrock: the region as saved; no organization field. Nothing is dirty yet.
  assert.equal($('provider-select').value, 'bedrock');
  assert.equal($('region-row').hidden, false);
  assert.equal($('custom-region').value, providers.region);
  assert.equal($('custom-region').disabled, false);
  assert.equal($('organization-env-row').hidden, true);
  assert.equal($('model-error').hidden, true);
  assert.equal(window.StiraModelSection.dirty(), false);
  for (const id of ['custom-region', 'custom-organization-env']) {
    assert.ok(doc.querySelector(`label[for="${id}"]`).textContent.trim(), `${id} has a label`);
    assert.ok($($(id).getAttribute('aria-describedby')), `${id} has a hint`);
  }

  // A new region: the save carries it and the version the page loaded.
  await type('custom-region', 'us-west-2');
  assert.equal(window.StiraModelSection.dirty(), true);
  const before = loads();
  await click('model-use');
  assert.equal(sent.length, 1);
  assert.equal(sent[0].url, '/api/config/models');
  assert.equal(sent[0].body.region, 'us-west-2');
  assert.equal(sent[0].body.base_version, providers.version);
  assert.ok(!('organization_env' in sent[0].body));
  // 1) Saved in another tab: said in the page's language, and the page loads the new state.
  assert.equal(shown(), t('mset.err_conflict'));
  assert.notEqual(t('mset.err_conflict'), 'mset.err_conflict');
  assert.equal(loads(), before + 1, 'reloaded after the conflict');
  assert.equal($('custom-region').value, providers.region, 'the newer saved value is shown');

  // 2) A coded field refusal: the page's own sentence, tied to the input.
  await type('custom-region', 'nowhere');
  await click('model-use');
  assert.equal(shown(), t('mset.err_invalid_region'));
  assert.equal($('custom-region').getAttribute('aria-invalid'), 'true');
  assert.ok(!doc.body.textContent.includes('region must be an AWS region name'), 'no server sentence');

  // 3) No code (an unexpected answer): a fixed sentence with the status; no server text.
  await click('model-use');
  assert.equal(shown(), t('mset.err_unknown', { status: 500 }));
  assert.ok(!doc.body.textContent.includes(marker), 'exception text is not shown');
  assert.ok(!doc.body.textContent.includes('Traceback'));

  // 4) Pinned while the page was open: names the variable.
  await click('model-use');
  assert.equal(shown(), t('mset.err_pinned_by_env', { env: 'AWS_REGION' }));

  // OpenAI: the organization variable NAME; no region.
  $('provider-select').value = 'openai';
  await fire('provider-select', 'change');
  assert.equal($('region-row').hidden, true);
  assert.equal($('organization-env-row').hidden, false);
  assert.equal($('custom-organization-env').value, '');
  assert.equal($('custom-region').getAttribute('aria-invalid'), null, 'the old field error is cleared');
  await type('custom-organization-env', 'STIRA_TEST_ORG');
  await click('model-use');
  const last = sent[sent.length - 1].body;
  assert.equal(last.provider, 'openai');
  assert.equal(last.organization_env, 'STIRA_TEST_ORG');
  assert.ok(!('region' in last), 'no region for a provider that does not use it');
  assert.equal(shown(), t('mset.err_missing_env', { env: 'STIRA_TEST_ORG' }));
  assert.ok(!doc.body.textContent.includes('A required environment variable is not set'));

  // The connection test sends the same fields, without the save keys.
  await click('conn-test');
  const probe = sent[sent.length - 1];
  assert.equal(probe.url, '/api/config/models/test');
  assert.equal(probe.body.organization_env, 'STIRA_TEST_ORG');
  assert.ok(!('base_version' in probe.body));

  // AWS_REGION set: the input is off and the note names the variable (never its value).
  const second = await openPage(html, { '/api/config/providers': () => answer(200, pinned), '/api/config/soul': soul });
  assert.equal(second.$('custom-region').disabled, true);
  assert.equal(second.$('custom-region').value, pinned.region);
  assert.equal(second.$('region-pinned').hidden, false);
  assert.equal(second.$('region-pinned').textContent, second.window.t('mset.region_pinned', { env: 'AWS_REGION' }));
  assert.equal(second.window.StiraModelSection.dirty(), false);

  // The configuration cannot be read: a fixed sentence, not the server's.
  const broken = await openPage(html, {
    '/api/config/providers': () => answer(503, { error: 'Config unavailable.', error_code: 'config_unavailable' }),
    '/api/config/soul': soul,
  });
  assert.match(broken.$('toast').textContent, new RegExp(broken.window.t('mset.err_config_unavailable').slice(0, 12)));
  assert.ok(!broken.$('toast').textContent.includes('Config unavailable.'));
}

async function usageMode(html, fixture, marker) {
  const { usage } = fixture;
  const routes = { '/api/config/providers': () => answer(503, {}), '/api/config/soul': soul };
  const page = await openPage(html, { ...routes, '/api/observability/usage': () => answer(200, usage) });
  const { window, doc, $ } = page;
  const t = window.t;
  const fmt = n => n.toLocaleString(doc.documentElement.lang || undefined);
  assert.ok(doc.querySelector('.hub-nav a[href="#sec-usage"]'), 'in the section list');
  assert.equal($('usage-table').hidden, false);
  const rows = [...$('usage-rows').querySelectorAll('tr')];
  assert.equal(rows.length, 1 + usage.by_model.length, 'the total and one row per model');
  const cells = row => [...row.children].map(cell => cell.textContent);
  // The total first: the sums of what was reported; a count nobody reported is "unknown".
  const total = cells(rows[0]);
  assert.equal(total[0], t('usage.total'));
  assert.equal(total[1], fmt(usage.total.turns));
  assert.equal(total[2], fmt(usage.total.input_tokens));
  assert.equal(total[3], fmt(usage.total.output_tokens));
  assert.equal(usage.total.cache_read_tokens, null);
  assert.equal(total[4], t('usage.unknown'));
  assert.equal(rows[0].children[0].tagName, 'TH');
  // A model that reported nothing: unknown, never 0.
  const index = usage.by_model.findIndex(row => row.model === 'local-model');
  const local = cells(rows[1 + index]);
  assert.equal(local[0], 'local-model');
  assert.deepEqual(local.slice(2), Array(4).fill(t('usage.unknown')));
  assert.ok(!local.slice(2).includes('0'));
  // A reported 0 stays 0; a turn without a model id has its own row.
  const none = cells(rows[1 + usage.by_model.findIndex(row => row.model === null)]);
  assert.equal(none[0], t('usage.model_unknown'));
  assert.equal(none[3], '0');
  assert.equal($('usage-note').hidden, false);
  assert.equal($('usage-note').textContent, t('usage.without_usage', { n: fmt(usage.total.turns_without_usage) }));
  assert.notEqual(t('usage.unknown'), 'usage.unknown');
  assert.equal($('usage-status').textContent, '');

  // "Refresh" reads again.
  const before = page.gets.filter(url => url.startsWith('/api/observability/usage')).length;
  await page.click('usage-refresh');
  assert.equal(page.gets.filter(url => url.startsWith('/api/observability/usage')).length, before + 1);

  // No turn yet: said so; nothing is shown as 0.
  const empty = await openPage(html, {
    ...routes,
    '/api/observability/usage': () => answer(200, {
      source: 'turns.db', truncated: false, by_model: [],
      total: { turns: 0, turns_without_usage: 0, input_tokens: null, output_tokens: null, cache_read_tokens: null, cache_write_tokens: null },
    }),
  });
  assert.equal(empty.$('usage-table').hidden, true);
  assert.equal(empty.$('usage-status').textContent, empty.window.t('usage.empty'));

  // A server without turn records (the observation console): said so, no table.
  const missing = await openPage(html, routes);
  assert.equal(missing.$('usage-table').hidden, true);
  assert.equal(missing.$('usage-status').textContent, missing.window.t('usage.unavailable'));

  // A failure: the status only; no server text.
  const failed = await openPage(html, { ...routes, '/api/observability/usage': () => answer(500, { error: `boom ${marker}` }) });
  assert.equal(failed.$('usage-table').hidden, true);
  assert.equal(failed.$('usage-status').textContent, failed.window.t('usage.load_failed', { status: 500 }));
  assert.ok(!failed.doc.body.textContent.includes(marker));
}

async function main() {
  const [mode, page, data, marker] = process.argv.slice(2);
  const html = fs.readFileSync(page, 'utf8');
  const fixture = JSON.parse(fs.readFileSync(data, 'utf8'));
  assert.ok(marker && marker.length > 8, 'marker value');
  await (mode === 'usage' ? usageMode : modelMode)(html, fixture, marker);
  console.log(`ok: settings ${mode}`);
}

main().catch((err) => { console.error(err); process.exit(1); });
