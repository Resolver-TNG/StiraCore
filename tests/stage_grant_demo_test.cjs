// The denser goal-grant panel and its demo (Stage, front only) — node --test tests/stage_grant_demo_test.cjs
// The state chip (a mark and words), the reasons in words, the iterations meter, "running
// low", the rounds strip, and the demo's dummy answers (director.js GRANT_STEPS) drawn by the
// same activity.js as a live answer. The DOM part needs jsdom (npm install --no-save jsdom).
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { pathToFileURL } = require('node:url');

let JSDOM = null;
try {
  ({ JSDOM } = require('jsdom'));
} catch {
  /* DOM tests skipped below */
}
const ROOT = path.join(__dirname, '..');
const STAGE = path.join(ROOT, 'dashboard', 'stage', 'js');
const load = (name) => import(pathToFileURL(path.join(STAGE, name)).href);
const CAT = {
  ja: JSON.parse(fs.readFileSync(path.join(ROOT, 'dashboard/static/i18n/catalog.ja.json'), 'utf8')),
  en: JSON.parse(fs.readFileSync(path.join(ROOT, 'dashboard/static/i18n/catalog.en.json'), 'utf8')),
};
const tr = (lang) => (key, vars = {}) => {
  const text = CAT[lang][key];
  if (text === undefined) throw new Error(`missing key ${key}`);
  return text.replace(/\{(\w+)\}/g, (m, k) => (k in vars ? String(vars[k]) : m));
};
const NOW = 1792416000;
const SRC = fs.readFileSync(path.join(STAGE, 'activity.js'), 'utf8');
const pageSet = (name) => [...SRC.match(new RegExp(`const ${name} = new Set\\(\\[([^\\]]*)\\]\\)`))[1].matchAll(/'([a-z_]+)'/g)].map((m) => m[1]);
const grant = (over = {}) => ({
  id: 'abababab', state: 'active', live: true, headline: { subject: 'grant', state: 'active' }, goal_state: 'active',
  iterations: 2, max_iterations: 10, ttl_sec: 7200, ttl_left_sec: 3600, budget_total: 1000, budget_left: 750, budget_per_tick: 100,
  awaiting_verification: false, cards: 0, card: false, iteration: { run: 'idle', number: 2, end: 'done', tick_used: 60 }, outcomes: [],
  ...over,
});
const round = (over = {}) => ({ round: 3, of_max: 10, wall_ms: 1000, model_calls: 2, writes: 1, files_changed: 1, tokens_in: 5, tokens_out: 6, origin: 'tick', end_code: 'done', progress: 'new', ...over });

test('every reason of the page\'s fixed sets is drawn in words (ja / en), never as its code', async () => {
  const A = await load('activity.js');
  const grantReasons = pageSet('GRANT_REASONS');
  const goalReasons = pageSet('GOAL_REASONS');
  assert.ok(grantReasons.length >= 13 && goalReasons.length >= 20);
  for (const lang of ['ja', 'en']) {
    const t = tr(lang);
    for (const r of grantReasons) {
      const g = A.grantView(grant({ state: 'revoked', live: false, headline: { subject: 'grant', state: 'revoked', reason: r } }), NOW, NOW, t);
      assert.match(g.headline, /\(.+\)$/, `${lang} grant ${r}: words`);
      assert.equal(g.headline.includes(`(${r})`), false, `${lang} grant ${r}: not the code`);
      assert.equal(g.announce.includes(`(${r})`), false);
    }
    for (const r of goalReasons) {
      const g = A.grantView(grant({ headline: { subject: 'goal', state: 'blocked', reason: r }, goal_state: 'blocked', goal_reason: r }), NOW, NOW, t);
      assert.match(g.headline, /\(.+\)$/, `${lang} goal ${r}: words`);
      assert.equal(g.headline.includes(`(${r})`), false, `${lang} goal ${r}: not the code`);
      assert.equal(g.lines[0], t('stage.grant_goal', { state: t('stage.grant_goal_blocked') }), 'the goal line does not say the headline\'s reason again');
      // under a grant headline the goal line carries the goal's reason, in words
      const under = A.grantView(grant({ goal_state: 'blocked', goal_reason: r }), NOW, NOW, t);
      assert.match(under.lines[0], /\(.+\)$/);
      assert.equal(under.lines[0].includes(`(${r})`), false);
    }
  }
  const t = tr('en');
  // a reason outside the sets is left out (no words, no code)
  const odd = A.grantView(grant({ state: 'paused', headline: { subject: 'grant', state: 'paused', reason: 'made_up' } }), NOW, NOW, t);
  assert.equal(odd.headline, t('stage.grant_state_paused'));
  const unmet = A.grantView(grant({ goal_state: 'unmet', goal_reason: 'revoked_kill_switch' }), NOW, NOW, t);
  assert.equal(unmet.lines[0], t('stage.grant_goal', { state: `${t('stage.grant_goal_unmet')} (${t('stage.grant_state_revoked')}: ${t('stage.grant_reason_kill_switch')})` }));
});

test('the state chip: a mark and a tone for every grant and goal state; unknown is muted', async () => {
  const A = await load('activity.js');
  const t = tr('en');
  const chip = (headline, over = {}) => A.grantView(grant({ headline, ...over }), NOW, NOW, t).chip;
  const grantStates = { active: 'go', paused: 'hold', halted: 'stop', revoked: 'stop', expired: 'stop', ended: 'done' };
  const goalStates = { active: 'go', paused: 'hold', blocked: 'stop', budget_limited: 'stop', awaiting_verification: 'hold', complete: 'done', unmet: 'stop' };
  const marks = new Set();
  for (const [state, tone] of Object.entries(grantStates)) {
    const c = chip({ subject: 'grant', state });
    assert.equal(c.tone, tone, state);
    assert.ok(c.mark.length === 1 && c.mark !== '·', state);
    marks.add(c.mark);
  }
  assert.equal(marks.size, Object.keys(grantStates).length, 'one mark per grant state: told without the colour');
  for (const [state, tone] of Object.entries(goalStates)) assert.equal(chip({ subject: 'goal', state }).tone, tone, state);
  assert.deepEqual(chip({ subject: 'grant', state: '<b>' }, { state: 'x y' }), { mark: '·', tone: 'muted' });
  assert.equal(A.grantView({ error: 'grant_busy' }, NOW, NOW, t).chip, null, 'unknown: no chip');
});

test('the iterations meter and "running low" (a tenth of the TTL or the budget)', async () => {
  const A = await load('activity.js');
  const t = tr('ja');
  const g = A.grantView(grant(), NOW, NOW, t);
  assert.deepEqual(g.iter, { line: 1, value: 2, max: 10 });
  assert.equal(g.lines[g.iter.line].startsWith(t('stage.grant_iterations', { n: 2, max: 10 })), true);
  assert.deepEqual([g.ttl.low, g.budget.low], [false, false]);
  assert.equal(A.grantView(grant({ iterations: 99 }), NOW, NOW, t).iter.value, 10, 'never past the max');
  assert.equal(A.grantView(grant({ max_iterations: 0 }), NOW, NOW, t).iter, null);
  assert.equal(A.grantView(grant({ iterations: undefined, iteration: undefined }), NOW, NOW, t).iter, null, 'no count: no bar');
  const low = A.grantView(grant({ ttl_left_sec: 720, budget_left: 100 }), NOW, NOW, t);
  assert.deepEqual([low.ttl.low, low.budget.low], [true, true]);
  assert.equal(A.grantView(grant({ ttl_left_sec: 800 }), NOW + 100, NOW, t).ttl.low, true, 'the TTL gets low between reads');
  assert.equal(A.grantView(grant({ ttl_sec: undefined, ttl_left_sec: 1 }), NOW, NOW, t).ttl.low, false, 'no total: not judged');
});

test('the rounds strip: only with the key, oldest first, at most five, words and counts the server sent', async () => {
  const A = await load('activity.js');
  for (const lang of ['ja', 'en']) {
    const t = tr(lang);
    const none = A.grantView(grant(), NOW, NOW, t);
    assert.deepEqual([none.rounds, none.ledger], [[], ''], 'no key: nothing');
    assert.deepEqual(A.roundView(round(), t), { kind: 'new', mark: '●', text: `#3 · ${t('stage.grant_claim_progress_new')} · ${t('stage.grant_round_writes', { n: 1 })} · ${t('stage.grant_round_files', { n: 1 })}` });
    assert.equal(A.roundView(round({ progress: 'repeat' }), t).mark, '○');
    assert.equal(A.roundView(round({ progress: 'none' }), t).mark, '–');
    assert.equal(A.roundView(round({ progress: 'other' }), t).kind, 'unknown');
    for (const end of ['yielded_user_turn', 'stopped', 'shutdown']) {
      const cut = A.roundView(round({ end_code: end, progress: 'none' }), t);
      assert.equal(cut.kind, 'interrupted');
      assert.ok(cut.text.includes(t('stage.grant_round_interrupted')));
    }
    // a count the server did not send is not drawn
    assert.equal(A.roundView({ round: 1, progress: 'new' }, t).text, `#1 · ${t('stage.grant_claim_progress_new')}`);
    const many = A.grantView(grant({ rounds: [1, 2, 3, 4, 5, 6, 7].map((n) => round({ round: n })), ledger: 'ok' }), NOW, NOW, t);
    assert.deepEqual(many.rounds.map((r) => r.text.split(' ')[0]), ['#3', '#4', '#5', '#6', '#7']);
    assert.equal(many.ledger, '');
    for (const state of ['round_ledger_torn', 'other']) assert.equal(A.grantView(grant({ rounds: [], ledger: state }), NOW, NOW, t).ledger, t('stage.grant_ledger_warn'));
  }
  const t = tr('en');
  const hostile = A.grantView(grant({ rounds: [{ round: '<b>', progress: '<i>', end_code: 'a b', writes: -1, files_changed: 'x' }, null, 'x'], ledger: 7 }), NOW, NOW, t);
  assert.equal(/[<>]/.test(JSON.stringify(hostile.rounds)), false);
  assert.deepEqual(hostile.rounds.map((r) => r.kind), ['unknown', 'unknown', 'unknown']);
  assert.equal(hostile.ledger, '');
});

// --- the demo ---------------------------------------------------------------------------------

// what activity.grant_view may send (observability/activity.py): the demo sends nothing else
const GRANT_KEYS = new Set(['id', 'state', 'live', 'reason', 'goal_state', 'goal_reason', 'headline', 'iterations', 'max_iterations', 'ttl_sec', 'ttl_left_sec', 'budget_total', 'budget_left', 'budget_per_tick', 'awaiting_verification', 'claim', 'cards', 'iteration', 'card', 'outcomes', 'rounds', 'ledger', 'tick']);
const ROUND_KEYS = new Set(['round', 'of_max', 'wall_ms', 'model_calls', 'writes', 'files_changed', 'tokens_in', 'tokens_out', 'origin', 'end_code', 'progress', 'stop']);
const DEMO_IDS = ['issued', 'running', 'approval', 'claim', 'complete', 'killed', 'expired', 'halted', 'budget', 'blocked'];

test('the demo: the grant\'s life in the server\'s shape, every state readable in ja and en', async () => {
  const A = await load('activity.js');
  const { GRANT_STEPS, STEPS, DEMO_TIME } = await load('director.js');
  assert.deepEqual(GRANT_STEPS.map((s) => s.id), DEMO_IDS.map((id) => `grant-${id}`));
  assert.equal(STEPS.some((s) => s.activity), false, 'the steps of before carry no board');
  const seen = {};
  for (const step of GRANT_STEPS) {
    assert.equal(step.key, 'G');
    for (const lang of ['ja', 'en']) assert.ok(step.say[lang] && step.label[lang], step.id);
    const body = step.activity;
    assert.equal(body.server_time, DEMO_TIME);
    for (const k of Object.keys(body.grant)) assert.ok(GRANT_KEYS.has(k), `${step.id}: ${k} is not in grant_view`);
    for (const r of body.grant.rounds || []) for (const k of Object.keys(r)) assert.ok(ROUND_KEYS.has(k), `${step.id}: ${k} is not in rounds_view`);
    for (const lang of ['ja', 'en']) {
      const m = A.activityModel({ body, items: body.items, offline: false }, body.server_time, tr(lang));
      assert.equal(m.grant.unknown, false, step.id);
      // nothing falls back to "(other)" and no reason code is spoken
      const text = JSON.stringify([m.grant.headline, m.grant.lines, m.grant.rounds, m.grant.outcomes, m.grant.announce]);
      assert.equal(text.includes('(other)'), false, `${step.id} ${lang}: ${text}`);
      assert.equal(/\([a-z]+_[a-z_]+\)/.test(text), false, `${step.id} ${lang}: a code in brackets`);
      seen[step.id] = m.grant;
    }
  }
  assert.equal(seen['grant-issued'].rounds.length, 0, 'before a round: no strip');
  assert.equal(seen['grant-running'].code, 'running');
  assert.equal(seen['grant-approval'].cue.kind, 'approval');
  assert.equal(seen['grant-claim'].cue.kind, 'verify');
  assert.equal(seen['grant-claim'].claim.audit, false);
  assert.equal(seen['grant-complete'].claim.audit, true);
  assert.deepEqual([seen['grant-complete'].cue, seen['grant-complete'].ttl], [null, null]);
  assert.deepEqual(['killed', 'expired', 'halted', 'budget', 'blocked'].map((id) => seen[`grant-${id}`].chip.tone), ['stop', 'stop', 'stop', 'stop', 'stop']);
  assert.equal(seen['grant-budget'].budget.low, true);
  assert.deepEqual(seen['grant-blocked'].rounds.map((r) => r.kind), ['new', 'repeat', 'repeat', 'none', 'none']);
  assert.ok(seen['grant-expired'].rounds.some((r) => r.kind === 'interrupted'));
});

test('the demo is read-only and asks nothing: no request in director.js, main.js draws it with activity.js', () => {
  const director = fs.readFileSync(path.join(STAGE, 'director.js'), 'utf8');
  assert.equal(/fetch\(|XMLHttpRequest|EventSource|import\s/.test(director), false);
  const main = fs.readFileSync(path.join(STAGE, 'main.js'), 'utf8');
  assert.match(main, /activityModel\(\{ body, items: body\.items, offline: false \}, body\.server_time, t\)/);
  assert.match(main, /e\.key === 'G'/);
  assert.match(main, /playStep\(\(stepIndex \+ 1\) % AUTO_STEPS\)/, 'the autoplay keeps to the steps of before');
  assert.equal((main.match(/fetch\(/g) || []).length, 2, 'the two requests of before (the activity feed, the catalog)');
  // the standalone page: the board's DOM (the served template's ids) and the colour tokens
  const page = fs.readFileSync(path.join(ROOT, 'dashboard/stage/index.html'), 'utf8');
  for (const id of ['board', 'board-head', 'board-title-text', 'board-body', 'board-stats', 'board-note', 'board-pin', 'board-copy', 'board-close']) assert.ok(page.includes(`id="${id}"`), id);
  assert.match(page, /<section id="board" class="board" hidden/);
  assert.ok(page.indexOf('static/ui/tokens.css') < page.indexOf('stage.css'), 'the tokens first: the Stage\'s own colours win');
});

test('the panel\'s DOM: marks are aria-hidden beside words, the strip is a labelled list, no control', { skip: JSDOM ? false : 'jsdom not installed' }, async () => {
  const dom = new JSDOM('<!doctype html><body></body>');
  const { document } = dom.window;
  const A = await load('activity.js');
  const { GRANT_STEPS } = await load('director.js');
  for (const lang of ['ja', 'en']) {
    const t = tr(lang);
    for (const step of GRANT_STEPS) {
      const body = step.activity;
      const box = A.renderActivity(document, A.activityModel({ body, items: body.items, offline: false }, body.server_time, t), t)[1];
      assert.equal(box.className, 'act-grant', step.id);
      assert.equal(box.querySelectorAll('button, input, form, select, textarea').length, 0, 'display only');
      for (const a of box.querySelectorAll('a')) assert.ok(['/approvals', '/settings#sec-grants'].includes(a.getAttribute('href')), step.id);
      const head = box.querySelector('.act-state');
      assert.ok(['go', 'hold', 'stop', 'done'].includes(head.dataset.tone), step.id);
      assert.equal(head.firstChild.className, 'act-ico');
      for (const ico of box.querySelectorAll('.act-ico')) {
        assert.equal(ico.getAttribute('aria-hidden'), 'true');
        assert.ok(ico.parentNode.textContent.replace(ico.textContent, '').trim().length > 1, 'words beside the mark');
      }
      const strip = box.querySelector('.act-rounds');
      if (body.grant.rounds) {
        assert.equal(strip.getAttribute('aria-label'), t('stage.grant_rounds'));
        assert.equal(strip.children.length, body.grant.rounds.length);
      } else assert.equal(strip, null);
    }
    const low = A.renderGrant(document, A.grantView(grant({ budget_left: 10, card: true, cards: 1 }), NOW, NOW, t), t);
    assert.equal(low.querySelector('.act-budget').dataset.low, '1');
    assert.equal(low.querySelector('.act-budget .act-low').textContent, t('stage.grant_low'));
    assert.equal(low.querySelector('.act-ttl .act-low'), null);
    assert.ok(low.querySelector('.act-cue').textContent.includes(t('stage.grant_cue_approval')));
  }
  dom.window.close();
});
