// Stage raw view (#367): node --test tests/stage_emotion_rows_test.cjs
//
// The raw view names the stimulus classifier (rule-only fallback said in words) and how many
// turns moved Neural State, with the chat page's own wording (chat_core.js).
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const core = require('../dashboard/static/js/chat_core.js');

const STAGE = path.join(__dirname, '..', 'dashboard', 'stage', 'js');
// A file URL, not a path: import('C:\\...') is read as a URL scheme on Windows.
const load = (name) => import(pathToFileURL(path.join(STAGE, name)).href);
const CATALOG = {
  ja: JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'dashboard', 'static', 'i18n', 'catalog.ja.json'), 'utf8')),
  en: JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'dashboard', 'static', 'i18n', 'catalog.en.json'), 'utf8')),
};

async function strings(lang) {
  const { buildStrings } = await load('i18n.js');
  return buildStrings(CATALOG, lang);
}

const RULE_FALLBACK = {
  classifier: { name: 'RuleBasedClassifier', is_jev: false, model: null, mode: 'rule_only', fallback: true },
  emotion_updates: { turns: 12, updated: 4, not_updated: 8, reasons: { no_stimulus: 8 }, last: { updated: false, reason: 'no_stimulus' } },
};

test('the raw view names a rule-only fallback and the turns that moved Neural State', async () => {
  const { emotionRows } = await load('live_ui.js');
  for (const lang of ['ja', 'en']) {
    const S = await strings(lang);
    const rows = emotionRows(RULE_FALLBACK, S, core);
    assert.deepEqual(rows.map(([label]) => label), [CATALOG[lang]['chat.stimulus_classifier'], CATALOG[lang]['chat.emotion_updates']]);
    assert.equal(rows[0][1], core.classifierText(RULE_FALLBACK.classifier, S.t).text);
    assert.equal(rows[0][1], CATALOG[lang]['chat.rule_only_fallback']);
    assert.equal(rows[1][1], core.emotionUpdatesText(RULE_FALLBACK.emotion_updates, S.t).text);
    assert.match(rows[1][1], /4/);
    assert.match(rows[1][1], /12/);
  }
});

test('nothing before the first status or without chat_core', async () => {
  const { emotionRows } = await load('live_ui.js');
  const S = await strings('ja');
  assert.deepEqual(emotionRows(null, S, core), []);
  assert.deepEqual(emotionRows(RULE_FALLBACK, S, null), []);
});
