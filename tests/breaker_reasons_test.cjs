// #455: breaker reasons in words (breaker_reasons.js), the Stage adapter and the catalogs.
// node --test tests/breaker_reasons_test.cjs
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const ROOT = path.join(__dirname, '..');
const words = require(path.join(ROOT, 'dashboard', 'static', 'js', 'breaker_reasons.js'));
const catalog = (lang) =>
  JSON.parse(fs.readFileSync(path.join(ROOT, 'dashboard', 'static', 'i18n', `catalog.${lang}.json`), 'utf8'));
const translator = (lang) => {
  const c = catalog(lang);
  return (key, vars) => {
    assert.ok(Object.hasOwn(c, key), `missing ${lang} key ${key}`);
    return c[key].replace(/\{(\w+)\}/g, (m, k) => (vars && Object.hasOwn(vars, k) ? String(vars[k]) : m));
  };
};
const CODES = [
  'stability_below_critical', 'stability_below_warning', 'bc_score_below_coherence',
  'hysteresis_hold', 'cooldown_active', 'stability_above_critical_exit', 'cooldown_served',
  'stability_above_warning_exit', 'bc_score_above_coherence_exit', 'within_thresholds',
];

test('the issue example reads as words with the numbers', () => {
  const inputs = { stability: 0.31, stability_warning: 0.35 };
  assert.equal(words.line('warning', ['stability_below_warning'], inputs, translator('ja')), 'WARNING: 安定度 0.31 < 0.35');
  assert.equal(words.line('WARNING', ['stability_below_warning'], inputs, translator('en')), 'WARNING: stability 0.31 < 0.35');
});

test('holds, cooldown and recovery are worded per level', () => {
  const ja = translator('ja');
  assert.equal(
    words.describe('critical', ['hysteresis_hold', 'cooldown_active'],
      { stability: 0.4, stability_critical_exit: 0.45, critical_turns: 1, critical_cooldown_turns: 3 }, ja),
    '安定度 0.40 ≤ 回復の基準 0.45(戻りの幅で保持)、保護ターン 1/3');
  assert.equal(words.describe('coherence', ['hysteresis_hold'], { bc_score: 0.65, bc_exit_threshold: 0.7 }, translator('en')),
    'coherence score 0.65 ≤ recovery threshold 0.70 (held by hysteresis)');
  assert.equal(words.line('warning', ['stability_above_critical_exit', 'cooldown_served', 'hysteresis_hold'],
    { stability: 0.5, stability_critical_exit: 0.45, critical_cooldown_turns: 3, stability_warning_exit: 0.6 }, translator('en')),
  'WARNING: stability 0.50 > recovery threshold 0.45; 3 protected turns served; stability 0.50 ≤ recovery threshold 0.60 (held by hysteresis)');
});

test('no code reaches the screen, in either language', () => {
  for (const lang of ['ja', 'en']) {
    const t = translator(lang);
    for (const level of ['normal', 'coherence', 'warning', 'critical', 'unknown']) {
      for (const code of [...CODES, 'made_up_code', '<b>x</b>']) {
        const out = words.line(level, [code], { stability: 0.2, bc_score: 0.5 }, t);
        assert.ok(out.length > 0);
        for (const c of [...CODES, 'made_up_code', '<b>']) assert.ok(!out.includes(c), `${lang} ${level} ${code}: ${out}`);
        assert.ok(!out.includes('breaker_reason.'));
      }
    }
  }
  assert.equal(words.describe('warning', [], {}, translator('ja')), '');
  assert.equal(words.describe('warning', 'stability_below_warning', {}, translator('ja')), '');
});

test('the Stage adapter forwards codes and numbers only', async () => {
  const { fromTurnEvent } = await import(path.join(ROOT, 'dashboard', 'stage', 'js', 'adapter.js'));
  const [ev] = fromTurnEvent({
    type: 'breaker.transition', turn_id: 't', seq: 2,
    data: { from: 'normal', to: 'warning', reasons: ['stability_below_warning', 'Has Text!'],
      inputs: { stability: 0.31, stability_warning: 0.35, note: 'x', 'Bad Key': 1 } },
  });
  assert.deepEqual(ev.reasons, ['stability_below_warning']);
  assert.deepEqual(ev.inputs, { stability: 0.31, stability_warning: 0.35 });
  const [old] = fromTurnEvent({ type: 'breaker.transition', turn_id: 't', seq: 3, data: { from: 'normal', to: 'critical' } });
  assert.deepEqual(old, { type: 'breaker', level: 'CRITICAL', raw: 'critical', turn: 't', seq: 3 });
});

test('the YOLO pause names the breaker reason in words', () => {
  const yolo = require(path.join(ROOT, 'dashboard', 'static', 'js', 'yolo_ui.js'));
  const t = translator('ja');
  const v = { pause: 'breaker_warning', pauseWhy: { reasons: ['stability_below_warning'], inputs: { stability: 0.31, stability_warning: 0.35 } } };
  assert.equal(yolo.pauseWhyText(v, t), ' (安定度 0.31 < 0.35)');
  assert.equal(yolo.pauseWhyText({ pause: 'breaker_warning', pauseWhy: null }, t), '');
  const view = yolo.viewOf({ window: { state: 'paused', tier: 'internal', pause_reason: 'breaker_critical',
    pause_breaker_reasons: ['stability_below_critical', 'NOT A CODE'], pause_breaker_inputs: { stability: 0.2 } } }, {});
  assert.equal(view.pause, 'breaker_critical');
  assert.deepEqual(view.pauseWhy.reasons, ['stability_below_critical']);
});
