// ADR 0022 phase 0 (#458): the settings card of the taint observation (read only).
// node --test tests/settings_taint_test.cjs
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const ROOT = path.join(__dirname, '..');
require(path.join(ROOT, 'dashboard', 'static', 'js', 'settings_taint.js'));
const card = globalThis.StiraTaint;
const catalog = (lang) =>
  JSON.parse(fs.readFileSync(path.join(ROOT, 'dashboard', 'static', 'i18n', `catalog.${lang}.json`), 'utf8'));
const use = (lang) => {
  const c = catalog(lang);
  globalThis.t = (key, vars) => {
    assert.ok(Object.hasOwn(c, key), `missing ${lang} key ${key}`);
    return c[key].replace(/\{(\w+)\}/g, (m, k) => (vars && Object.hasOwn(vars, k) ? String(vars[k]) : m));
  };
};

test('off, forced off and unreadable read as one line each', () => {
  use('en');
  assert.deepEqual(card.lines({ enabled: false }), ['Off (not counting).']);
  assert.deepEqual(card.lines({ enabled: false, forced_off: true }), ['Off (forced off by STIRA_TAINT_OBSERVE).']);
  assert.deepEqual(card.lines(null), ['The observation could not be read.']);
});

test('on: totals and would-hold codes in both languages', () => {
  const data = {
    enabled: true,
    observation: {
      last_turn: null,
      totals: { turns: 3, 'trust.untrusted': 2, sensitive: 0, untrusted_recall_only: 1, 'flow.untrusted_send': 2 },
    },
  };
  use('en');
  assert.deepEqual(card.lines(data), [
    'On, observe only. Nothing is enforced.',
    'Since start: 3 turns, 2 untrusted, 0 sensitive, 1 untrusted only because of recalled memory.',
    'Would have asked a person: untrusted_send 2.',
  ]);
  use('ja');
  assert.equal(card.lines({ enabled: true, observation: { totals: {} } })[2], '保留されていたはずの外部送信はありません。');
});
