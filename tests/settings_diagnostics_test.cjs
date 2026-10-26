// Settings diagnostics section (#267): node --test tests/settings_diagnostics_test.cjs
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { resolve } = require('node:path');
const vm = require('node:vm');
const { shimSource, catalog } = require('./ui_shim.cjs');
const { Element, byClass } = require('./chat_dom_mock.cjs');

const source = readFileSync(resolve(__dirname, '../dashboard/static/js/settings_diagnostics.js'), 'utf8');

const root = (path, extra = {}) => ({
  root: path, path, status: 'ok', findings: [], write_test: { ok: true }, ...extra,
});
const finding = (code, level, extra = {}) => ({ code, level, message: `server says ${code}`, ...extra });
const report = (extra = {}) => ({
  version: 1, generated_at: '2026-10-18T15:00:00+0900', platform: 'darwin', ok: true, config_error: null,
  counts: { ok: 1, warning: 0, error: 0 }, workspace: '@workspace',
  principal: { kind: 'terminal', name: 'Terminal', executable: '/usr/bin/python3' },
  roots: [root('@workspace')], checks: [], notes: [], ...extra,
});

function harness({ reply, posted } = {}) {
  const doc = { createElement: tag => new Element(doc, tag) };
  const context = { document: doc, JSON, Math, Date, String, Number, Array, Set, Map, Promise };
  context.window = context;
  vm.runInNewContext(shimSource() + source, context);
  const els = {};
  for (const name of ['status', 'facts', 'checks', 'roots', 'notes', 'checksBox', 'rootsBox', 'notesBox', 'rerun']) {
    els[name] = new Element(doc, name === 'rerun' ? 'button' : 'div');
  }
  const calls = [];
  const tab = context.StiraDiagnosticsTab.create({
    document: doc, elements: els,
    getJSON: async url => { calls.push(['GET', url]); return typeof reply === 'function' ? reply() : reply; },
    post: async url => { calls.push(['POST', url]); return posted; },
  });
  return { tab, els, calls };
}

test('a healthy report shows the verdict, facts and every root with a plain "ok"', async () => {
  const h = harness({ reply: report() });
  await h.tab.refresh();
  assert.deepEqual(h.calls, [['GET', '/api/diagnostics/doctor']]);
  assert.equal(h.els.status.textContent, catalog['diagnostics.tab_verdict_ok'].replace('{roots}', '1'));
  const facts = h.els.facts.children.map(row => [row.children[0].textContent, row.children[1].textContent]);
  assert.deepEqual(facts[0], [catalog['diagnostics.tab_workspace'], '@workspace']);
  assert.match(facts[2][1], /Terminal/);
  assert.equal(h.els.rootsBox.hidden, false);
  assert.equal(h.els.checksBox.hidden, true);
  assert.ok(h.els.roots.textContent.includes(catalog['diagnostics.tab_write_ok']));
});

test('findings are worded from the catalog by code, with the level as text and a human-only fix', async () => {
  const entry = root('~/Documents', {
    status: 'warning', write_test: null,
    findings: [finding('tcc_zone', 'warning', { zone: '~/Documents', remedy: 'tcc' })],
  });
  const h = harness({ reply: report({ roots: [entry] }) });
  await h.tab.refresh();
  const item = h.els.roots.find(n => n.dataset.code === 'tcc_zone');
  assert.equal(item.dataset.level, 'warning');
  assert.equal(item.find(byClass('diag-level')).textContent, catalog['diagnostics.level_warning']);
  assert.equal(item.textContent.includes('~/Documents'), true);
  assert.equal(item.textContent.includes('server says'), false);
  assert.equal(item.textContent.includes(catalog['diagnostics.fix_tcc']), true);
  assert.equal(
    h.els.status.textContent,
    catalog['diagnostics.tab_verdict_warnings'].replace('{warnings}', '1').replace('{roots}', '1'));
});

test('parameters of locked flags, failed write tests and skipped write tests are filled in', async () => {
  const entries = [
    root('/a', { status: 'error', findings: [finding('locked_flag', 'error', { flags: ['uchg', 'schg'], remedy: 'locked_flag' })] }),
    root('/b', { status: 'error', write_test: { ok: false }, findings: [finding('write_test_failed', 'error', { step: 'create', errno: 'EACCES' })] }),
    root('/c', { status: 'warning', write_test: { ok: null, skipped: 'symlink_in_path' }, findings: [finding('write_test_skipped', 'warning')] }),
  ];
  const h = harness({ reply: report({ ok: false, roots: entries }) });
  await h.tab.refresh();
  const text = h.els.roots.textContent;
  assert.ok(text.includes('uchg, schg'));
  assert.ok(text.includes('create') && text.includes('EACCES'));
  assert.ok(text.includes(catalog['diagnostics.skip_symlink_in_path']));
  assert.match(h.els.status.textContent, /エラー 2 件/);
});

test('the classifier check shows the warning and the install fix', async () => {
  const check = finding('classifier_rule_only', 'warning', { mode: 'rule_only', remedy: 'install_jev' });
  const h = harness({ reply: report({ checks: [check] }) });
  await h.tab.refresh();
  assert.equal(h.els.checksBox.hidden, false);
  const text = h.els.checks.textContent;
  assert.equal(text.includes(catalog['diagnostics.finding_classifier_rule_only']), true);
  assert.equal(text.includes(catalog['diagnostics.fix_install_jev']), true);
  assert.equal(h.els.status.dataset.level, 'warning');
});

test('an info check (Sokudan installed) has no fix text', async () => {
  const h = harness({ reply: report({ checks: [finding('classifier_sokudan', 'info', { mode: 'sokudan' })] }) });
  await h.tab.refresh();
  assert.equal(h.els.checks.find(byClass('diag-fix')) ?? null, null);
});

test('a configuration error is named, not hidden behind a green verdict', async () => {
  const h = harness({ reply: report({ ok: false, config_error: 'policy_unbuildable' }) });
  await h.tab.refresh();
  assert.equal(h.els.status.textContent, catalog['diagnostics.tab_config_error']);
  assert.equal(h.els.status.dataset.level, 'error');
});

test('an unknown code falls back to the server message instead of a bare key', async () => {
  const h = harness({ reply: report({ notes: [finding('brand_new_code', 'info')] }) });
  await h.tab.refresh();
  assert.equal(h.els.notes.textContent.includes('server says brand_new_code'), true);
  assert.equal(h.els.notes.textContent.includes('diagnostics.finding_'), false);
});

test('not run (404) and a failed load say so', async () => {
  let h = harness({ reply: null });
  await h.tab.refresh();
  assert.equal(h.els.status.textContent, catalog['diagnostics.tab_not_run']);
  assert.equal(h.els.rootsBox.hidden, true);
  h = harness({ reply: () => { throw new Error('500'); } });
  await h.tab.refresh();
  assert.equal(h.els.status.textContent, catalog['diagnostics.tab_failed']);
});

test('"Run again" uses the existing POST route only and re-renders; nothing else is written', async () => {
  const h = harness({ reply: report(), posted: report({ checks: [finding('classifier_rule_only', 'warning')] }) });
  await h.tab.refresh();
  h.els.rerun.click();
  await new Promise(r => setImmediate(r));
  assert.deepEqual(h.calls, [['GET', '/api/diagnostics/doctor'], ['POST', '/api/diagnostics/doctor']]);
  assert.equal(h.els.rerun.disabled, false);
  assert.equal(h.els.checksBox.hidden, false);
});

test('the script writes no setting: no storage, no other URL, no write verb', () => {
  assert.equal(/localStorage|sessionStorage|\/api\/config|\/api\/tools|method:\s*"(PUT|PATCH|DELETE)"/.test(source), false);
  const urls = [...source.matchAll(/"(\/api\/[^"]*)"/g)].map(m => m[1]);
  assert.deepEqual([...new Set(urls)], ['/api/diagnostics/doctor']);
});

test('every catalog key the script builds exists in ja and en', () => {
  const en = JSON.parse(readFileSync(resolve(__dirname, '../dashboard/static/i18n/catalog.en.json'), 'utf8'));
  const keys = [...new Set([...source.matchAll(/"(diagnostics\.[a-z_]+)"/g)].map(m => m[1]))];
  assert.ok(keys.length > 10);
  for (const key of keys) {
    assert.ok(key in catalog, `ja: ${key}`);
    assert.ok(key in en, `en: ${key}`);
  }
});
