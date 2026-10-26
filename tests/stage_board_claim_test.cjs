// W6c (#296): a completion claim's evidence and its waiting state on the Stage board —
// node --test tests/stage_board_claim_test.cjs (the DOM / axe parts need jsdom + axe-core:
// npm install --no-save jsdom axe-core).
//
// The board shows what the harness measured at the claim (codes and observed counts, no
// percentage, never model text): "claimed at iteration k/max · checks · workspace · progress",
// the per-check rows, the tokens / TTL at the claim, "since <time>" while it waits and — past the
// server's threshold, counted on from server_time between reads like the TTL (no timer of its
// own) — "still waiting for you". Failing checks are amber with a "⚠" line in words.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const { execFileSync } = require('node:child_process');

let JSDOM = null;
let axeSource = null;
try {
  ({ JSDOM } = require('jsdom'));
  axeSource = require('axe-core').source;
} catch {
  /* DOM tests skipped below */
}
const ROOT = path.join(__dirname, '..');
const STAGE = path.join(ROOT, 'dashboard', 'stage', 'js');
const load = (name) => import(path.join(STAGE, name));
const CAT = {
  ja: JSON.parse(fs.readFileSync(path.join(ROOT, 'dashboard/static/i18n/catalog.ja.json'), 'utf8')),
  en: JSON.parse(fs.readFileSync(path.join(ROOT, 'dashboard/static/i18n/catalog.en.json'), 'utf8')),
};
const tr = (lang) => (key, vars = {}) => {
  const text = CAT[lang][key];
  if (text === undefined) throw new Error(`missing key ${key}`);
  return text.replace(/\{(\w+)\}/g, (m, k) => (k in vars ? String(vars[k]) : m));
};
const MARKER = 'w6cMarker-3b9d17e05c2a48f1';
const NOW = 1792416000;
const row = (kind = 'pytest', status = 'passed', failed = 0) => ({ kind, status, counts: { passed: 4, failed, errors: 0, skipped: 1, findings: 0 } });
const claim = (over = {}) => ({
  claimed_at: NOW - 600, iteration: 3, max_iterations: 10, used_tokens: 1200, budget_left: 8800, ttl_left_sec: 3000,
  checks: 'pass', check_rows: [row()], workspace: 'changed', progress: 'new',
  waiting_sec: 600, stale_after_sec: 1800, stale: false, ...over,
});
const grant = (over = {}) => ({
  id: 'abababab', state: 'active', live: true, headline: { subject: 'goal', state: 'awaiting_verification' },
  goal_state: 'awaiting_verification', iterations: 3, max_iterations: 10, ttl_sec: 7200, ttl_left_sec: 2400,
  budget_total: 10000, budget_left: 8800, budget_per_tick: 2000, awaiting_verification: true, cards: 0, card: false,
  iteration: { run: 'idle', number: 3, end: 'claimed', tick_used: 300 }, outcomes: [], claim: claim(), ...over,
});
const answer = (over = {}) => ({
  server_time: NOW, cursor: 9, reset: true, items: [], stall_sec: 120,
  state: { code: 'idle', since: null, last_event: null, running: false },
  yolo: null,
  totals: { turns: 1, input: 1, output: 2, cache_read: 0, cache_write: 0, context_tokens: null, cost: null },
  ...over,
});

test('the evidence line: iteration k/max, checks, workspace, progress — observed codes, no percentage', async () => {
  const A = await load('activity.js');
  for (const lang of ['ja', 'en']) {
    const t = tr(lang);
    const g = A.grantView(grant(), NOW, NOW, t);
    const c = g.claim;
    assert.equal(c.lines[0], t('stage.grant_claim_line', {
      n: 3, max: 10, checks: t('stage.grant_claim_checks_pass'), workspace: t('stage.grant_claim_workspace_changed'), progress: t('stage.grant_claim_progress_new'),
    }));
    assert.equal(c.lines[1], t('stage.grant_claim_at', { used: '1,200', left: '8,800', time: A.duration(3000, t) }));
    assert.deepEqual(c.rows, [t('stage.grant_claim_row', { kind: 'pytest', status: t('stage.grant_check_passed'), passed: 4, failed: 0, errors: 0, skipped: 1, findings: 0 })]);
    assert.equal(c.since, t('stage.grant_claim_since_for', { time: A.clock(NOW - 600), wait: A.duration(600, t) }));
    assert.equal(c.title, t('stage.grant_claim_h'));
    assert.equal(c.amber, false);
    assert.equal(c.stale, false);
    assert.equal(g.cue.kind, 'verify');
    assert.equal(/%/.test(JSON.stringify(c)), false, 'no percentage');
  }
});

test('failing checks: amber, with a "⚠" line in words (not colour alone), announced with the cue', async () => {
  const A = await load('activity.js');
  const t = tr('en');
  const g = A.grantView(grant({ claim: claim({ checks: 'fail', check_rows: [row('pytest', 'failed', 2), row('ruff')] }) }), NOW, NOW, t);
  assert.equal(g.claim.amber, true);
  assert.equal(g.claim.checks, 'fail');
  assert.equal(g.claim.warn, t('stage.grant_claim_fail'));
  assert.ok(t('stage.grant_claim_fail').startsWith('⚠') && tr('ja')('stage.grant_claim_fail').startsWith('⚠'));
  assert.ok(g.announce.includes(t('stage.grant_claim_fail')));
  assert.equal(g.claim.rows.length, 2);
  for (const checks of ['pass', 'not_run', 'unknown']) {
    const other = A.grantView(grant({ claim: claim({ checks, check_rows: checks === 'pass' ? [row()] : [] }) }), NOW, NOW, t);
    assert.equal(other.claim.amber, false, checks);
    assert.equal(other.claim.warn, '', checks);
  }
});

test('"still waiting for you" past the threshold — counted on from server_time, no timer', async () => {
  const A = await load('activity.js');
  const t = tr('ja');
  const near = grant({ claim: claim({ waiting_sec: 1790 }) });
  assert.equal(A.grantView(near, NOW + 9, NOW, t).cue.kind, 'verify', '1799 s: not yet');
  const past = A.grantView(near, NOW + 10, NOW, t);
  assert.equal(past.cue.kind, 'verify_stale', '1800 s: still waiting');
  assert.equal(past.cue.text, t('stage.grant_cue_verify_stale'));
  assert.ok(past.claim.since.includes(A.duration(1800, t)), 'the seconds are in the since line');
  // the announcement never carries seconds: the same at 1800 s and at 1801 s
  assert.equal(A.grantView(near, NOW + 11, NOW, t).announce, past.announce);
  assert.equal(/\{\w+\}/.test(CAT.ja['stage.grant_cue_verify_stale'] + CAT.en['stage.grant_cue_verify_stale']), false);
  assert.equal(past.cue.href, '/settings#sec-grants');
  assert.equal(past.claim.stale, true);
  assert.equal(A.grantView(grant({ claim: claim({ stale: true, waiting_sec: 5 }) }), NOW, NOW, t).cue.kind, 'verify_stale', "the server's flag");
  // a waiting card still wins (the person has to answer it first)
  assert.equal(A.grantView({ ...near, card: true, cards: 1 }, NOW + 10, NOW, t).cue.kind, 'approval');
  // the module adds no timer: still one interval and one timeout in the controller
  const src = fs.readFileSync(path.join(STAGE, 'activity.js'), 'utf8').replace(/\/\/.*$/gm, '');
  assert.equal((src.match(/setInterval\(/g) || []).length, 1);
  assert.equal((src.match(/setTimeout\(/g) || []).length, 1);
  assert.equal(/requestAnimationFrame|new Date\(\)|Date\.now/.test(src), false, 'no clock of its own');
});

test('a waiting claim\'s seconds are no news for the poll (like the TTL); a changed code is', async () => {
  const A = await load('activity.js');
  let body = answer({ grant: grant() });
  const feed = new A.ActivityFeed({ getJSON: async () => body, wall: () => NOW });
  assert.equal(await feed.poll(), true, 'the first read');
  body = answer({ grant: grant({ ttl_left_sec: 2390, claim: claim({ waiting_sec: 610 }) }), reset: false });
  assert.equal(await feed.poll(), false, 'only the TTL and the waiting seconds moved');
  body = answer({ grant: grant({ claim: claim({ waiting_sec: 620, stale: true }) }), reset: false });
  assert.equal(await feed.poll(), true, 'stale is news');
  body = answer({ grant: grant({ claim: claim({ waiting_sec: 630, stale: true, checks: 'fail', check_rows: [row('pytest', 'failed', 1)] }) }), reset: false });
  assert.equal(await feed.poll(), true, 'a measured code is news');
});

test('after the confirmation: read-only (the audit trail) — no since, no waiting cue', async () => {
  const A = await load('activity.js');
  const t = tr('en');
  const done = grant({
    state: 'ended', live: false, goal_state: 'complete', awaiting_verification: false,
    headline: { subject: 'grant', state: 'ended', reason: 'complete' }, claim: claim({ waiting_sec: undefined, stale: undefined }),
  });
  const g = A.grantView(done, NOW, NOW, t);
  assert.equal(g.claim.audit, true);
  assert.equal(g.claim.title, t('stage.grant_claim_audit'));
  assert.equal(g.claim.since, null);
  assert.equal(g.claim.stale, false);
  assert.equal(g.cue, null);
  // a stale flag on an ended grant is never a cue
  assert.equal(A.grantView({ ...done, claim: claim({ stale: true }) }, NOW, NOW, t).cue, null);
});

test('hostile values: unknown codes are "unknown" (never pass), a bad row makes the checks unknown', async () => {
  const A = await load('activity.js');
  const t = tr('en');
  const cases = [
    { checks: MARKER },
    { checks: 'pass', check_rows: [] },
    { checks: 'pass', check_rows: [row('pytest', 'failed', 1)] },
    { checks: 'fail', check_rows: [row()] },
    { checks: 'pass', check_rows: [{ ...row(), status: '<b>' }] },
    { checks: 'pass', check_rows: [{ ...row(), counts: { ...row().counts, passed: '4' } }] },
    { checks: 'pass', check_rows: Array(17).fill(row()) },
    { checks: 'pass', check_rows: 'lots' },
  ];
  for (const bad of cases) {
    const c = A.grantView(grant({ claim: claim(bad) }), NOW, NOW, t).claim;
    assert.equal(c.checks, 'unknown', JSON.stringify(bad).slice(0, 60));
    assert.deepEqual(c.rows, []);
  }
  const odd = A.grantView(grant({ claim: claim({ workspace: MARKER, progress: '<i>', iteration: -1, check_rows: [{ ...row(), kind: `${MARKER}<img>` }] }) }), NOW, NOW, t);
  const text = JSON.stringify(odd);
  assert.equal(text.includes(MARKER), false);
  assert.equal(/[<>]/.test(text), false);
  assert.equal(odd.claim.workspace, 'unknown');
  assert.equal(odd.claim.progress, 'unknown');
  assert.ok(odd.claim.lines[0].includes('–/10'));
  assert.ok(odd.claim.rows[0].startsWith('other:'));
  assert.equal(A.grantView(grant({ claim: 'x' }), NOW, NOW, t).claim, null);
  assert.equal(A.grantView(grant({ claim: undefined }), NOW, NOW, t).claim, null);
});

test('every claim key is in both catalogs (JA/EN), used literally', async () => {
  const src = fs.readFileSync(path.join(STAGE, 'activity.js'), 'utf8');
  for (const k of ['stage.grant_claim_h', 'stage.grant_claim_audit', 'stage.grant_claim_line', 'stage.grant_claim_fail', 'stage.grant_cue_verify_stale', 'stage.grant_check_limited']) {
    assert.ok(src.includes(`'${k}'`), k);
    for (const lang of ['ja', 'en']) assert.equal(typeof CAT[lang][k], 'string', `${lang} ${k}`);
  }
  assert.equal(/`stage\.grant_/.test(src), false, 'no template-built key');
});

// --- the DOM ---------------------------------------------------------------------------------------

const BOARD_HTML = '<!doctype html><html lang="ja"><body><div id="board"><div class="board-head"><h2 class="board-title" id="board-title"></h2></div><div class="board-body" id="board-body"></div><div class="board-foot"></div></div></body></html>';

test('the panel: the claim group, amber + "⚠" in words, textContent only, no control', { skip: JSDOM ? false : 'jsdom not installed' }, async () => {
  const dom = new JSDOM(BOARD_HTML);
  const { document } = dom.window;
  const A = await load('activity.js');
  const t = tr('ja');
  const body = answer({ grant: grant({ claim: claim({ checks: 'fail', check_rows: [row('pytest', 'failed', 2)], waiting_sec: 1900, stale: true }) }) });
  const box = A.renderActivity(document, A.activityModel({ body, items: [] }, NOW, t), t)[1];
  const group = box.querySelector('.act-claim');
  assert.ok(group);
  assert.equal(group.dataset.tone, 'amber');
  assert.equal(group.dataset.checks, 'fail');
  assert.equal(group.dataset.stale, '1');
  const warn = group.querySelector('.act-claim-warn');
  assert.ok(warn, 'a "⚠" line in words');
  assert.equal(warn.textContent, t('stage.grant_claim_fail'));
  assert.equal(group.querySelectorAll('.act-checks li').length, 1);
  assert.equal(box.querySelector('.act-cue').dataset.cue, 'verify_stale');
  assert.equal(box.querySelectorAll('button, input, form').length, 0, 'display only');
  const hostile = A.renderActivity(document, A.activityModel({ body: answer({ grant: grant({ claim: claim({ check_rows: [{ ...row(), kind: '<img src=x onerror=1>' }] }) }) }), items: [] }, NOW, t), t)[1];
  assert.equal(hostile.querySelector('img'), null);
  dom.window.close();
});

test('axe: the panel with a waiting, failed, stale claim and with a verified one', { skip: JSDOM && axeSource ? false : 'jsdom/axe-core not installed' }, async () => {
  const A = await load('activity.js');
  for (const lang of ['ja', 'en']) {
    const t = tr(lang);
    for (const g of [
      grant({ claim: claim({ checks: 'fail', check_rows: [row('pytest', 'failed', 2), row('ruff')], stale: true }) }),
      grant({ state: 'ended', live: false, goal_state: 'complete', awaiting_verification: false, headline: { subject: 'grant', state: 'ended', reason: 'complete' } }),
    ]) {
      const dom = new JSDOM(BOARD_HTML, { runScripts: 'outside-only', pretendToBeVisual: true });
      const { document } = dom.window;
      const nodes = A.renderActivity(document, A.activityModel({ body: answer({ grant: g }), items: [] }, NOW, t), t);
      document.getElementById('board-body').append(...nodes);
      dom.window.eval(axeSource);
      const result = await dom.window.axe.run(document.getElementById('board-body'), { rules: { 'color-contrast': { enabled: false } } });
      assert.deepEqual(Array.from(result.violations, (v) => `${v.id}: ${Array.from(v.nodes, (n) => n.html).join(' | ')}`), [], lang);
      assert.ok(result.passes.length > 0);
      dom.window.close();
    }
  }
});

// The panel of a grant with no claim to show is W5b's, node for node (main's activity.js, c1b512f).
test('no claim: the grant panel is the one of before W6c', { skip: JSDOM ? false : 'jsdom not installed' }, async (t0) => {
  let before;
  try {
    before = execFileSync('git', ['-C', ROOT, 'show', 'c1b512f9:dashboard/stage/js/activity.js'], { encoding: 'utf8' });
  } catch {
    t0.skip('main c1b512f9 not in this clone');
    return;
  }
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'w6c-main-'));
  const old = path.join(dir, 'activity.mjs');
  fs.writeFileSync(old, before);
  for (const dep of fs.readdirSync(STAGE)) if (dep !== 'activity.js') fs.copyFileSync(path.join(STAGE, dep), path.join(dir, dep));
  const A0 = await import(old);
  const A = await load('activity.js');
  const t = tr('ja');
  const dom = new JSDOM(BOARD_HTML);
  const { document } = dom.window;
  const html = (mod, g) => {
    const wrap = document.createElement('div');
    wrap.append(...mod.renderActivity(document, mod.activityModel({ body: answer({ grant: g }), items: [] }, NOW, t), t));
    return wrap.innerHTML;
  };
  const variants = [
    grant({ claim: undefined }),
    grant({ claim: undefined, goal_state: 'active', awaiting_verification: false, headline: { subject: 'grant', state: 'active' } }),
    grant({ claim: undefined, card: true, cards: 1 }),
    grant({ claim: undefined, state: 'revoked', live: false, headline: { subject: 'grant', state: 'revoked', reason: 'user' } }),
  ];
  for (const g of variants) assert.equal(html(A, g), html(A0, g));
  assert.equal(html(A, null), html(A0, null));
  dom.window.close();
  fs.rmSync(dir, { recursive: true, force: true });
});
