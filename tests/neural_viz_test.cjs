const test = require('node:test');
const assert = require('node:assert/strict');
const {correlation, timestamp, heartbeatText} = require('../dashboard/static/js/neural_viz.js');
test('Pearson heatmap handles positive, negative, constant and missing samples', () => {
  const rows = [1, 2, 3].map(v => ({state: {a: v, b: 4 - v, c: 1}}));
  assert.deepEqual(correlation(rows, ['a', 'b', 'c']), [[1, -1, null], [-1, 1, null], [null, null, null]]);
  assert.deepEqual(correlation([], ['a']), [[null]]);
  assert.deepEqual(correlation(rows.slice(0, 1), ['a']), [[null]]);
});
test('supports ISO observer and numeric live timestamps', () => {
  assert.equal(timestamp(1000), 1000000);
  assert.equal(timestamp('1970-01-01T00:16:40+00:00'), 1000000);
});
test('#238 heartbeat summary is read-only text for off, running, degraded and refused', () => {
  const t = (key, vars) => (vars ? `${key} ${JSON.stringify(vars)}` : key);
  const clock = v => `T${v}`;
  assert.equal(heartbeatText(undefined, t, clock), null);
  assert.equal(heartbeatText({enabled: false, running: false}, t, clock), 'neural.heartbeat_off');
  assert.equal(
    heartbeatText({enabled: true, running: false, last_error: 'HeartbeatLockError'}, t, clock),
    'neural.heartbeat_stopped {"error":"HeartbeatLockError"}');
  const on = {enabled: true, running: true, last_tick_at: 10, next_tick_at: 20, last_level: 'warning', consecutive_failures: 0};
  assert.equal(heartbeatText(on, t, clock),
    'neural.heartbeat_on {"last":"T10","next":"T20","breaker":"shell.warning","failures":0}');
  const bad = {...on, last_tick_at: null, last_level: null, degraded: true, consecutive_failures: 3};
  assert.equal(heartbeatText(bad, t, clock),
    'neural.heartbeat_degraded {"last":"—","next":"T20","breaker":"—","failures":3}');
});
