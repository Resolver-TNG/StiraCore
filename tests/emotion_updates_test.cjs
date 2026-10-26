// #367: the chat sidebar says which classifier drives Neural State (rules only = fallback)
// and how many turns moved it, with the fixed reason of the last one that did not.
// node --test tests/emotion_updates_test.cjs
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const ROOT = path.join(__dirname, '..');
const core = require('../dashboard/static/js/chat_core.js');
const t = (key, vars) => (vars ? `${key}(${JSON.stringify(vars)})` : key);
const catalog = (lang) =>
  JSON.parse(fs.readFileSync(path.join(ROOT, 'dashboard/static/i18n', `catalog.${lang}.json`), 'utf8'));

const REASONS = ['no_stimulus', 'classifier_error', 'unknown_stimulus', 'no_change', 'turn_incomplete'];
const zero = () => Object.fromEntries(REASONS.map((r) => [r, 0]));

test('rules running as a fallback are said so, with the install hint as the title', () => {
  const view = core.classifierText(
    { name: 'RuleBasedClassifier', is_jev: false, model: null, mode: 'rule_only', fallback: true }, t);
  assert.deepEqual(view, {
    text: 'chat.rule_only_fallback', title: 'chat.rule_only_fallback_title', state: 'rule', fallback: true,
  });
});

test('an explicit rule choice keeps the plain label', () => {
  const view = core.classifierText({ name: 'RuleBasedClassifier', mode: 'rule_only', fallback: false }, t);
  assert.deepEqual(view, { text: 'chat.rule_fallback', title: '', state: 'rule', fallback: false });
});

test('sokudan and unresolved keep their labels; an older server (no mode) still works', () => {
  assert.equal(core.classifierText({ name: 'SokudanClassifier', is_jev: true, model: 'm', mode: 'sokudan' }, t).text,
    'chat.sokudan_model({"model":" (m)"})');
  assert.equal(core.classifierText({ name: 'unresolved', mode: 'unknown', fallback: null }, t).state, 'unresolved');
  assert.equal(core.classifierText({ name: 'RuleBasedClassifier', is_jev: false }, t).text, 'chat.rule_fallback');
  assert.equal(core.classifierText(undefined, t).text, 'chat.unresolved');
});

test('counts and the last reason', () => {
  const view = core.emotionUpdatesText({
    turns: 19, updated: 7, not_updated: 12,
    reasons: { ...zero(), no_stimulus: 12 }, last: { updated: false, reason: 'no_stimulus' },
  }, t);
  assert.equal(view.text,
    'chat.emotion_updates_count({"updated":7,"turns":19}) · chat.emotion_last_reason({"reason":"chat.no_update_no_stimulus"})');
  assert.match(view.title, /^chat\.emotion_updates_title/);
  assert.match(view.title, /chat\.no_update_no_stimulus 12/);
  assert.equal(view.reason, 'no_stimulus');
});

test('a last turn that changed, no turn yet, no field', () => {
  const changed = core.emotionUpdatesText(
    { turns: 2, updated: 2, not_updated: 0, reasons: zero(), last: { updated: true, reason: null } }, t);
  assert.equal(changed.text, 'chat.emotion_updates_count({"updated":2,"turns":2}) · chat.emotion_last_updated');
  assert.equal(changed.reason, null);
  assert.equal(core.emotionUpdatesText({ turns: 0, updated: 0, reasons: zero(), last: null }, t).text,
    'chat.emotion_updates_none');
  assert.equal(core.emotionUpdatesText(null, t).text, '—');
  assert.equal(core.emotionUpdatesText(undefined, t).text, '—');
});

test('a code outside the vocabulary reads unknown (never echoed)', () => {
  const view = core.emotionUpdatesText(
    { turns: 1, updated: 0, reasons: zero(), last: { updated: false, reason: '<img onerror=x>' } }, t);
  assert.equal(view.reason, 'unknown');
  assert.ok(!view.text.includes('<img'));
});

test('every key exists in both catalogs', () => {
  const keys = ['chat.rule_only_fallback', 'chat.rule_only_fallback_title', 'chat.emotion_updates',
    'chat.emotion_updates_count', 'chat.emotion_updates_none', 'chat.emotion_last_updated',
    'chat.emotion_last_reason', 'chat.emotion_updates_title',
    ...[...REASONS, 'unknown'].map((r) => `chat.no_update_${r}`)];
  for (const lang of ['ja', 'en']) {
    const cat = catalog(lang);
    for (const key of keys) assert.ok(cat[key], `${lang}: ${key}`);
  }
  assert.deepEqual([...core.NO_UPDATE_REASONS], REASONS);
});

test('the chat page has the row and renders it from the status', () => {
  const html = fs.readFileSync(path.join(ROOT, 'dashboard/templates/chat.html'), 'utf8');
  assert.match(html, /data-i18n="chat\.emotion_updates"/);
  assert.match(html, /id="emotion-updates"/);
  const js = fs.readFileSync(path.join(ROOT, 'dashboard/static/js/chat.js'), 'utf8');
  assert.match(js, /emotionUpdatesText\(status\.emotion_updates/);
  assert.match(js, /classifierText\(/);
});
