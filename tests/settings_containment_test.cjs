// ADR 0023 phase 0 (#460-0): the settings card of the containment observation (read only).
// node --test tests/settings_containment_test.cjs
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const ROOT = path.join(__dirname, '..');
require(path.join(ROOT, 'dashboard', 'static', 'js', 'settings_containment.js'));
const card = globalThis.StiraContainment;
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
  assert.deepEqual(card.lines({ enabled: false, forced_off: true }), ['Off (forced off by STIRA_CONTAINMENT_OBSERVE).']);
  assert.deepEqual(card.lines(null), ['The observation could not be read.']);
  assert.deepEqual(card.lines({ error: 'x' }), ['The observation could not be read.']);
});

test('on: window, would-be stage, peak and counts in both languages', () => {
  const data = {
    enabled: true,
    window_seconds: 600,
    window: { score: 4.5, would_be: 'restricted', peak_would_be: 'closed', signals: { G1: 2, G6: 1, T: 0 } },
  };
  use('en');
  assert.deepEqual(card.lines(data), [
    'On, observe only: a 10-minute window. Nothing is enforced.',
    'Would be now: restricted (score 4.5).',
    'Highest since start: closed.',
    'secret probing 2, outside the roots 1',
  ]);
  use('ja');
  const ja = card.lines(data);
  assert.equal(ja[1], 'いまの仮の段階: restricted（制限）（スコア 4.5）。');
  assert.equal(ja[3], '秘密の探索 2、範囲外 1');
  assert.equal(card.lines({ ...data, window: { score: 0, would_be: 'open', signals: {} } })[3], '窓の中にシグナルはありません。');
});

test('an unknown stage is not echoed', () => {
  use('en');
  const line = card.lines({ enabled: true, window: { would_be: '<b>x</b>', score: 0 } })[1];
  assert.equal(line, 'Would be now: unknown (score 0).');
});

test('the catalogs carry no URL in the containment keys', () => {
  for (const lang of ['en', 'ja']) {
    for (const [key, value] of Object.entries(catalog(lang))) {
      if (key.startsWith('contain.')) assert.ok(!/https?:\/\/|\/api\//.test(value), `${lang} ${key}`);
    }
  }
});
