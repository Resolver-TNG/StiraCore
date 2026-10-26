// W6d (#296): the heartbeat start on the Stage board and in the settings panel —
// node --test tests/stage_board_tick_test.cjs. Codes and counts only; nothing announced.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { pathToFileURL } = require('node:url');
const vm = require('node:vm');

const ROOT = path.join(__dirname, '..');
const load = (name) => import(pathToFileURL(path.join(ROOT, 'dashboard', 'stage', 'js', name)).href);
const CAT = {
  ja: JSON.parse(fs.readFileSync(path.join(ROOT, 'dashboard/static/i18n/catalog.ja.json'), 'utf8')),
  en: JSON.parse(fs.readFileSync(path.join(ROOT, 'dashboard/static/i18n/catalog.en.json'), 'utf8')),
};
const tr = (lang) => (key, vars = {}) => {
  const text = CAT[lang][key];
  if (text === undefined) throw new Error(`missing key ${key}`);
  return text.replace(/\{(\w+)\}/g, (m, k) => (k in vars ? String(vars[k]) : m));
};
const MARKER = 'w6dMarker-5e1c9a7d3b204f68';
const grant = (over = {}) => ({
  id: 'abababab', state: 'active', live: true, headline: { subject: 'grant', state: 'active' },
  goal_state: 'active', iterations: 1, max_iterations: 10, ttl_sec: 7200, ttl_left_sec: 3600,
  budget_total: 10000, budget_left: 9000, budget_per_tick: 2000, awaiting_verification: false,
  cards: 0, card: false, iteration: { run: 'idle', number: 1, end: 'done', tick_used: 300 }, outcomes: [], ...over,
});
const tick = (over = {}) => ({
  on: true, started: 2, spacing_sec: 300, last: 'min_spacing',
  skipped: { min_spacing: 7, turn_busy: 2, heartbeat_unhealthy: 1 },
  cost: { number: 2, model_calls: 3, tokens_in: 1200, tokens_out: 90, wall_ms: 64000 }, ...over,
});

for (const lang of ['ja', 'en']) {
  test(`the board's heartbeat-start lines (${lang})`, async () => {
    const A = await load('activity.js');
    const t = tr(lang);
    const lines = A.tickLines(tick(), t);
    assert.equal(lines.length, 3);
    assert.match(lines[0], /2/);
    assert.ok(lines[0].includes(t('grants.tick_skip_min_spacing')));
    assert.ok(lines[1].startsWith(t('stage.grant_tick_skipped', { list: '' }).trim()));
    assert.ok(lines[1].indexOf(t('grants.tick_skip_min_spacing')) < lines[1].indexOf(t('grants.tick_skip_turn_busy')));
    assert.ok(lines[2].includes('1200') && lines[2].includes('90'));
    const cached = A.tickLines(tick({ cost: { tokens_in: 1, tokens_out: 2, cache_read: 40, cache_write: 5 } }), t);
    assert.ok(cached[cached.length - 1].includes('40') && cached[cached.length - 1].includes('5'));
    const off = A.tickLines(tick({ unavailable: 'heartbeat_off' }), t);
    assert.ok(off.includes(t('grants.tick_unavailable_heartbeat_off')));
    assert.ok(A.tickLines(tick({ unavailable: 'scheduler_off' }), t).includes(t('grants.tick_unavailable_scheduler_off')));
    assert.ok(!lines.includes(t('grants.tick_unavailable_heartbeat_off')));
    const view = A.grantView(grant({ tick: tick() }), 0, 0, t);
    assert.ok(view.lines.some((l) => l === lines[0]));
    assert.ok(!view.announce.includes(lines[0])); // never announced
  });
}

test('unknown codes and shapes are never drawn; no tick block: as before', async () => {
  const A = await load('activity.js');
  const t = tr('en');
  assert.deepEqual(A.tickLines(undefined, t), []);
  assert.deepEqual(A.tickLines({ started: 3 }, t), []); // not "on"
  const lenient = (key, vars = {}) => (CAT.en[key] === undefined ? key : t(key, vars));
  const odd = A.tickLines(tick({ last: MARKER, skipped: { [MARKER]: 3, turn_busy: 'x' }, cost: { tokens_in: MARKER } }), lenient);
  assert.ok(!odd.join(' ').includes(MARKER));
  assert.equal(odd.length, 1);
  const before = A.grantView(grant(), 0, 0, t);
  const after = A.grantView(grant({ tick: undefined }), 0, 0, t);
  assert.deepEqual(after, before);
});

test('the settings panel rows: starts, last tick, skips, last cost', () => {
  const ctx = { window: {}, document: undefined, t: tr('en') };
  ctx.globalThis = ctx.window;
  vm.createContext(ctx);
  vm.runInContext(fs.readFileSync(path.join(ROOT, 'dashboard/static/js/settings_grants.js'), 'utf8').replace('(typeof window !== "undefined" ? window : globalThis)', '(window)'), ctx);
  const rows = ctx.window.StiraGrants.tickRows({
    heartbeat_start: true,
    tick: { started: 1, spacing_sec: 300, last: 'no_action', skipped: { no_action: 2, [MARKER]: 1 }, costs: [{ model_calls: 2, tokens_in: 10, tokens_out: 3, wall_ms: 2000, cache_read: 7 }] },
  });
  const text = JSON.stringify(rows);
  assert.equal(rows.length, 4);
  assert.ok(text.includes(CAT.en['grants.tick_skip_no_action']));
  assert.ok(text.includes('cache read 7'));
  assert.ok(!text.includes(MARKER));
  assert.equal(ctx.window.StiraGrants.tickRows({ tick: { started: 1 } }).length, 0); // not flagged
});
