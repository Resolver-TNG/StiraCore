// Settings hub "Memory retention limits" section (#474) via jsdom: the inputs built from the
// group's JSON Schema, the change list, the save body (no confirmation), the page's own range
// check, the read-only dry run (counts only, "apply is command line only"), axe.
// usage: node settings_retention_dom.cjs <settings.html> <hub.json> <dry-run.json> <marker>
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
const DRY_RUN = '/api/config/memory-retention/dry-run';
const tick = () => new Promise(r => setTimeout(r, 0));
async function settle() { for (let i = 0; i < 30; i += 1) await tick(); }

async function openPage(html, hub, dry) {
  const dom = new JSDOM(html, { runScripts: 'outside-only', pretendToBeVisual: true, url: 'http://127.0.0.1/settings' });
  const { window } = dom;
  polyfillDialog(window);
  const posts = [];
  const gets = [];
  window.fetch = async (url, options = {}) => {
    const json = body => ({ ok: true, status: 200, json: async () => body });
    if (options.method === 'POST') {
      posts.push({ url, body: JSON.parse(options.body) });
      return { ok: false, status: 409, json: async () => ({}) };
    }
    gets.push(url);
    if (url.startsWith('/api/config/history')) return json({ entries: [] });
    if (url.startsWith('/api/config/hub')) return json(hub);
    if (url.startsWith(DRY_RUN)) return json(dry);
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
  const type = (id, text) => {
    $(id).value = text;
    $(id).dispatchEvent(new window.Event('input'));
  };
  return { window, doc, $, posts, gets, type };
}

async function main() {
  const html = fs.readFileSync(process.argv[2], 'utf8');
  const hub = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
  const dry = JSON.parse(fs.readFileSync(process.argv[4], 'utf8'));
  const marker = process.argv[5];
  assert.ok(marker && marker.length > 8, 'marker value');
  const rules = hub.schemas.memory_retention.properties;
  const saved = hub.values.memory_retention;

  {
    const { window, doc, $, posts, gets, type } = await openPage(html, hub, dry);
    const t = window.t;
    assert.notEqual(t('ret.sec'), 'ret.sec', 'catalog loaded');
    const section = $('sec-memory-retention');
    assert.ok(doc.querySelector('.hub-nav a[href="#sec-memory-retention"]'), 'in the section list');
    const badge = kind => doc.querySelector(`.${kind}[data-group="memory_retention"]`).textContent;
    assert.equal(badge('timing-badge'), t('surface.applies_on_run'));
    assert.equal(badge('apply-status'), t('hub.status_saved'));
    // "Applying is command line only" is on the page before anything is pressed.
    assert.equal($('ret-apply-cli').textContent.trim(), t('ret.apply_cli'));
    assert.ok($('ret-apply-cli').textContent.includes('--apply'));

    // One input per schema property, with the schema's range and the saved value.
    for (const [key, rule] of Object.entries(rules)) {
      const input = $(`ret-${key}`);
      assert.ok(input, key);
      assert.ok(section.contains(input), key);
      assert.ok($(`err-memory_retention-${key}`), key);
      assert.ok(doc.querySelector(`label[for="ret-${key}"]`).textContent.trim(), `${key}: label`);
      if (rule.type === 'boolean') {
        assert.equal(input.type, 'checkbox', key);
        assert.equal(input.checked, saved[key], key);
      } else {
        assert.equal(input.type, 'number', key);
        assert.equal(input.min, String(rule.minimum), key);
        assert.equal(input.max, String(rule.maximum), key);
        assert.equal(input.value, saved[key] === null ? '' : String(saved[key]), key);
      }
    }
    assert.equal($('ret-archive__warn_bytes').max, String(10 ** 15));
    assert.equal($('ret-episodic__max_age_days').placeholder, t('ret.no_limit'));
    assert.equal($('ret-memory_plans__max_age_days').placeholder, '', 'has a default: never "no limit"');
    assert.equal(section.querySelectorAll('fieldset.ret-store').length,
      new Set(Object.values(rules).map(rule => rule['x-section'])).size);
    assert.equal($('hub-save').disabled, true, 'nothing changed yet');
    assert.equal(gets.some(url => url.startsWith(DRY_RUN)), false, 'the dry run is not run by itself');

    // Out of range / not a whole number: named on the field, the save is off.
    type('ret-episodic__max_age_days', '0');
    await settle();
    const error = $('err-memory_retention-episodic__max_age_days');
    assert.equal(error.hidden, false);
    assert.equal(error.textContent, t('hub.err_out_of_range', { min: 1, max: 1000000 }));
    assert.equal($('ret-episodic__max_age_days').getAttribute('aria-invalid'), 'true');
    assert.equal($('hub-save').disabled, true);
    type('ret-episodic__max_age_days', '1.5');
    await settle();
    assert.equal(error.textContent, t('hub.err_not_integer'));
    type('ret-archive__warn_bytes', String(10 ** 15 + 1));
    await settle();
    assert.equal($('err-memory_retention-archive__warn_bytes').hidden, false);
    type('ret-archive__warn_bytes', '');
    // A setting with a default cannot be emptied.
    type('ret-memory_plans__max_age_days', '');
    await settle();
    assert.equal($('err-memory_retention-archive__warn_bytes').hidden, true, 'empty = no limit');
    assert.equal($('err-memory_retention-memory_plans__max_age_days').hidden, false);
    type('ret-memory_plans__max_age_days', String(saved.memory_plans__max_age_days));

    // Valid values: the change list in the page's words, then the save body.
    type('ret-episodic__max_age_days', '30');
    type('ret-archive__warn_bytes', '2000000000');
    $('ret-episodic__keep_unharvested').click();
    await settle();
    assert.equal(error.hidden, true);
    assert.equal($('hub-save').disabled, false);
    const diff = [...doc.querySelectorAll('#hub-diff-list li')].map(li => li.textContent);
    assert.equal(diff.length, 3, diff.join('\n'));
    assert.ok(diff.some(line => line.includes(t('ret.store_episodic')) && line.includes(t('ret.f_max_age_days'))
      && line.includes(t('ret.no_limit')) && line.includes('30')), diff.join('\n'));
    assert.ok(diff.some(line => line.includes(t('ret.f_keep_unharvested')) && line.includes(t('hub.value_off'))), diff.join('\n'));
    assert.ok(diff.every(line => !line.includes('memory_retention.')), diff.join('\n'));

    // The dry run: one GET, counts only, and the words "apply is command line only".
    assert.equal($('ret-dry-facts').hidden, true);
    $('ret-dry-run').click();
    await settle();
    assert.equal(gets.filter(url => url.startsWith(DRY_RUN)).length, 1);
    assert.equal(posts.length, 0, 'the dry run posts nothing');
    const facts = $('ret-dry-facts');
    assert.equal(facts.hidden, false);
    const rows = [...facts.querySelectorAll('div')].map(row => [row.querySelector('dt').textContent, row.querySelector('dd').textContent]);
    assert.equal(rows.length, Object.keys(dry.will_remove).length);
    assert.deepEqual(rows[0], [t('ret.count_episodic'), t('ret.count_n', { n: String(dry.will_remove.episodic) })]);
    const status = $('ret-dry-status').textContent;
    assert.ok(status.includes(t('ret.dry_apply_cli')), status);
    assert.ok(status.includes(t('ret.dry_unsaved')), status);
    assert.equal(section.textContent.includes(marker), false, 'no memory text');
    assert.equal(section.textContent.includes('canary-key'), false, 'no memory key');

    // axe over the section (jsdom: no color-contrast).
    window.eval(axeSource);
    const result = await window.axe.run(section, { rules: { 'color-contrast': { enabled: false } } });
    if (result.violations.length) {
      for (const v of result.violations) console.log(`[${v.impact}] ${v.id}: ${v.help} (${v.nodes.length})`);
      process.exit(1);
    }

    // Save: only the memory_retention group (the whole group, as the hub takes it), no confirmation.
    $('hub-save').click();
    await settle();
    assert.equal(posts.length, 1, 'posted once, no confirmation needed');
    const { changes, acknowledged_dangers: acknowledged } = posts[0].body;
    assert.deepEqual(Object.keys(changes), ['memory_retention']);
    assert.deepEqual(changes.memory_retention, {
      ...saved, episodic__max_age_days: 30, archive__warn_bytes: 2000000000, episodic__keep_unharvested: false,
    });
    assert.ok(!acknowledged || acknowledged.length === 0, 'nothing to confirm');
    window.close();
  }

  // --- A count the report did not give is "unknown", never 0; an unavailable dry run says why
  {
    const unknown = { ...dry, will_remove: { ...dry.will_remove, checkpoints: null }, not_inspected: 2 };
    const page = await openPage(html, hub, unknown);
    const t = page.window.t;
    page.$('ret-dry-run').click();
    await settle();
    const cells = [...page.$('ret-dry-facts').querySelectorAll('dd')].map(dd => dd.textContent);
    assert.ok(cells.includes(t('ret.count_unknown')), cells.join(','));
    assert.ok(page.$('ret-dry-status').textContent.includes(t('ret.dry_not_inspected', { n: 2 })));
    page.window.close();
  }
  {
    const page = await openPage(html, hub, { available: false, apply: 'cli', reason: 'no_data_directory' });
    page.$('ret-dry-run').click();
    await settle();
    assert.equal(page.$('ret-dry-status').textContent, page.window.t('ret.dry_no_data_directory'));
    assert.equal(page.$('ret-dry-facts').hidden, true);
    page.window.close();
  }

  console.log('ok: memory retention section');
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
