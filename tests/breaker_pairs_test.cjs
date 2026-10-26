// Breaker pair/detail rounding (#471): node --test tests/breaker_pairs_test.cjs
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { resolve } = require('node:path');
const Pairs = require('../dashboard/static/js/breaker_pairs.js');

test('raising an entry to or past its exit raises the exit with it (keeps the gap)', () => {
  // The defect: warning 0.7 with the old exit 0.6 was refused, and the exit had no control.
  assert.deepEqual(Pairs.roundPair(0.7, 0.6, 'entry', 0.1), { entry: 0.7, exit: 0.8, note: 'exit_raised' });
  assert.deepEqual(Pairs.roundPair(0.95, 0.6, 'entry', 0.1), { entry: 0.95, exit: 1, note: 'exit_raised' });
  assert.deepEqual(Pairs.roundPair(1, 0.6, 'entry', 0.1), { entry: 0.99, exit: 1, note: 'exit_raised' });
  assert.deepEqual(Pairs.roundPair(0.55, 0.6, 'entry', 0.1), { entry: 0.55, exit: 0.6, note: null });
  // no saved gap: the smallest step
  assert.deepEqual(Pairs.roundPair(0.6, 0.6, 'entry', 0), { entry: 0.6, exit: 0.61, note: 'exit_raised' });
});

test('an exit moved under its entry stops just above it; the entry is never lowered', () => {
  assert.deepEqual(Pairs.roundPair(0.5, 0.4, 'exit', 0.1), { entry: 0.5, exit: 0.51, note: 'exit_clamped' });
  assert.deepEqual(Pairs.roundPair(0.5, 0.7, 'exit', 0.1), { entry: 0.5, exit: 0.7, note: null });
  assert.deepEqual(Pairs.roundPair(0.3, 0, 'exit', 0.1).entry, 0.3);
});

test('the critical exit stays below the warning exit by raising the warning exit', () => {
  assert.deepEqual(Pairs.orderExits(0.6, 0.45), { warningExit: 0.6, note: null });
  assert.deepEqual(Pairs.orderExits(0.6, 0.6), { warningExit: 0.61, note: 'warning_exit_raised' });
  assert.deepEqual(Pairs.orderExits(0.6, 1), { warningExit: 0.6, note: null }); // left to validation
});

test('typed details are rounded into the valves, and say so', () => {
  const windowRule = { integer: true, min: 5, max: null };
  assert.deepEqual(Pairs.roundDetail('3', windowRule), { value: 5, note: 'rounded' });
  assert.deepEqual(Pairs.roundDetail('7.4', windowRule), { value: 7, note: 'rounded' });
  assert.deepEqual(Pairs.roundDetail('30', windowRule), { value: 30, note: null });
  assert.deepEqual(Pairs.roundDetail('', windowRule), { value: null, note: 'not_number' });
  assert.deepEqual(Pairs.roundDetail('abc', windowRule), { value: null, note: 'not_number' });
  const timeout = { integer: false, min: 0, min_exclusive: true, max: 86400 };
  assert.deepEqual(Pairs.roundDetail('0', timeout), { value: 1, note: 'rounded' });
  assert.deepEqual(Pairs.roundDetail('100000', timeout), { value: 86400, note: 'rounded' });
  assert.deepEqual(Pairs.roundDetail('600', timeout), { value: 600, note: null });
  assert.deepEqual(Pairs.roundDetail('1.5', { integer: false, min: 0, max: 1 }), { value: 1, note: 'rounded' });
});

test('the sycophancy threshold is never rounded to the dull end: kept as typed (m-2)', () => {
  // Rounding 1.5 to 1 would pick the value at which the alert never fires.
  const threshold = { integer: false, min: 0, max: 1, clamp: false };
  assert.deepEqual(Pairs.roundDetail('1.5', threshold), { value: 1.5, note: null });
  assert.deepEqual(Pairs.roundDetail('-0.2', threshold), { value: -0.2, note: null });
  assert.deepEqual(Pairs.roundDetail('0.8', threshold), { value: 0.8, note: null });
  assert.deepEqual(Pairs.roundDetail('x', threshold), { value: null, note: 'not_number' });
});

test('the band spans entry..exit in percent', () => {
  assert.deepEqual(Pairs.band(0.5, 0.6), { left: 50, width: 10 });
  assert.deepEqual(Pairs.band(0.7, 0.5), { left: 70, width: 0 });
});

test('the hub page loads the helpers before settings_hub.js and the server publishes the same limits', () => {
  const html = readFileSync(resolve(__dirname, '../dashboard/templates/settings.html'), 'utf8');
  const helper = html.indexOf('js/breaker_pairs.js');
  assert.ok(helper > 0 && helper < html.indexOf('js/settings_hub.js'));
  const hub = readFileSync(resolve(__dirname, '../src/stiracore/observability/settings_hub.py'), 'utf8');
  assert.match(hub, /"bc_window_size": \{\s*"integer": True,\s*"min": BC_MIN_SAMPLES,\s*"max": DETAIL_LIMITS\["bc_window_size"\]/);
  assert.match(hub, /"critical_cooldown_turns": \{\s*"integer": True,\s*"min": 3,\s*"max": DETAIL_LIMITS\["critical_cooldown_turns"\]/);
  // #471 review M-1: a huge typed window is rounded to the server's upper limit.
  assert.deepEqual(Pairs.roundDetail('1000000000', { integer: true, min: 5, max: 200 }), { value: 200, note: 'rounded' });
});
