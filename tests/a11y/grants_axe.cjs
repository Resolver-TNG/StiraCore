// axe-core over the live-rendered goal grants panel (ADR 0011 W4 of #296), via jsdom.
// usage: node grants_axe.cjs <settings_grants.html>
// Checks: the panel renders every grant / goal state, shows no goal text (only the
// issuer-only view after the typed SHOW), buttons are type=button, the issue goes through
// the 428 → confirm → re-send flow with the operation header, and axe finds nothing.
// Exit codes: 0 = ok, 1 = violations/error, 77 = deps missing (skip).
const fs = require('node:fs');
const path = require('node:path');
const { shimSource } = require('../ui_shim.cjs');

let JSDOM;
let axeSource;
try {
  ({ JSDOM } = require('jsdom'));
  axeSource = require('axe-core').source;
} catch (err) {
  console.log(`SKIP: jsdom/axe-core not available (${err.message})`);
  process.exit(77);
}

const JS = path.join(__dirname, '..', '..', 'dashboard', 'static', 'js');
const GOAL = 'goal-marker-axe-41';
const ids = ['a', 'b', 'c', 'd', 'e', '1'].map((c) => c.repeat(32));
const base = {
  kind: 'goal', tools: ['editor', 'file_read'], ttl_sec: 7200, ttl_left_sec: 3600, budget_total: 1000,
  budget_left: 400, budget_used: 600, iterations: 2, max_iterations: 10, generation: 3, late_verification: '',
};
// W6c: the evidence of a completion claim (codes and counts; a marker in an unknown field
// is never drawn).
const row = (kind, status, failed) => ({ kind, status, counts: { passed: 3, failed, errors: 0, skipped: 0, findings: 0 } });
const evidence = (over) => ({ claimed_at: 1792416000, iteration: 2, max_iterations: 10, used_tokens: 600, budget_left: 400,
  ttl_left_sec: 3600, checks: 'fail', check_rows: [row('pytest', 'failed', 2)], workspace: 'changed', progress: 'new',
  stale_after_sec: 1800, waiting_sec: 1900, stale: true, note: GOAL, ...over });
const grants = [
  { ...base, grant_id: ids[0], state: 'active', reason: '', goal_state: 'awaiting_verification', goal_reason: '',
    headline: { subject: 'goal', state: 'awaiting_verification', reason: '' }, claim_evidence: evidence({}) },
  { ...base, grant_id: ids[1], state: 'halted', reason: 'breaker_warning', goal_state: 'active', goal_reason: '',
    headline: { subject: 'grant', state: 'halted', reason: 'breaker_warning' } },
  { ...base, grant_id: ids[2], state: 'paused', reason: 'goal_blocked', goal_state: 'blocked', goal_reason: 'no_progress',
    headline: { subject: 'goal', state: 'blocked', reason: 'no_progress' },
    iteration: { state: 'idle', number: 2, end: 'approval_timeout', detail: '', card: 'waiting' } },
  { ...base, grant_id: ids[3], state: 'revoked', reason: 'kill_switch', goal_state: 'unmet', goal_reason: 'revoked_kill_switch',
    late_verification: 'pass', headline: { subject: 'grant', state: 'revoked', reason: 'kill_switch' } },
  { ...base, grant_id: ids[4], state: 'ended', reason: 'complete', goal_state: 'complete', goal_reason: 'verified',
    headline: { subject: 'grant', state: 'ended', reason: 'complete' },
    claim_evidence: evidence({ checks: 'pass', check_rows: [row('ruff', 'passed', 0)], waiting_sec: undefined, stale: undefined }) },
  // W5: the executor is on — an active goal offers "Run one iteration".
  { ...base, grant_id: ids[5], state: 'active', reason: '', goal_state: 'active', goal_reason: '',
    headline: { subject: 'grant', state: 'active', reason: '' },
    iteration: { state: 'yielded', number: 1, end: 'yielded_user_turn', detail: '', card: 'none' },
    // W6d: issued with the heartbeat start — its rows (codes and numbers; a marker in an
    // unknown skip reason is never drawn)
    heartbeat_start: true,
    tick: { started: 1, spacing_sec: 300, last: 'min_spacing', skipped: { min_spacing: 2, [GOAL]: 1 },
      costs: [{ number: 1, model_calls: 2, tokens_in: 40, tokens_out: 6, wall_ms: 3000 }] } },
];
const list = { enabled: true, healthy: true, unhealthy_reason: null, authority_killed: false,
  tools: ['current_time', 'editor', 'file_read', 'memory_list', 'memory_read', 'memory_search', 'run_checks'], grants };

async function main() {
  const html = fs.readFileSync(process.argv[2], 'utf8');
  const dom = new JSDOM(html, { runScripts: 'outside-only', pretendToBeVisual: true, url: 'http://localhost/settings' });
  const { window } = dom;
  window.eval(shimSource());
  window.StiraGrantsNoPoll = true;
  window.StiraGrantsPollMs = 5;
  const posted = [];
  window.fetch = async (url, init = {}) => {
    const headers = new window.Headers(init.headers || {});
    if (url === '/api/grants') return { ok: true, status: 200, json: async () => list };
    if (url.startsWith(`/api/grants/${ids[0]}/goal`)) {
      const ok = url.endsWith('confirm=SHOW') && headers.get('X-Stira-Goal-View') === 'f'.repeat(32);
      return ok ? { ok: true, status: 200, json: async () => ({ grant_id: ids[0], goal: GOAL }) }
        : { ok: false, status: 403, json: async () => ({ code: 'grant_view_refused' }) };
    }
    if (url === `/api/grants/operations/${'6'.repeat(32)}`) {
      return { ok: true, status: 200, json: async () => ({ operation: { state: 'confirmed' } }) };
    }
    posted.push({ url, op: headers.get('X-Stira-Host-Operation'), body: init.body ? JSON.parse(init.body) : null });
    if (url === `/api/grants/${ids[0]}/verify` && !headers.get('X-Stira-Host-Operation')) {
      return { ok: false, status: 428, json: async () => ({ code: 'grant_confirmation_required', operation: {
        id: '6'.repeat(32), short: '66666666', channel: 'command', expires_in: 120, kind: 'grant_verify' } }) };
    }
    if (url === '/api/grants/issue' && !headers.get('X-Stira-Host-Operation')) {
      return { ok: false, status: 428, json: async () => ({ code: 'grant_confirmation_required', operation: {
        id: '9'.repeat(32), confirm_token: '8'.repeat(32), instance: '7'.repeat(16), kind: 'grant_issue',
        summary: { action: 'grant_issue', tool: ['file_read'], count: 120 } } }) };
    }
    if (url === '/api/grants/issue') {
      return { ok: true, status: 200, json: async () => ({ grant: { grant_id: ids[0] }, goal_view_token: 'f'.repeat(32) }) };
    }
    return { ok: true, status: 200, json: async () => ({ operation: { state: 'confirmed' } }) };
  };
  window.StiraConfirm = { open: async () => true };
  window.eval(fs.readFileSync(path.join(JS, 'settings_grants.js'), 'utf8'));
  const doc = window.document;
  const wait = async (check) => { for (let i = 0; i < 50 && !check(); i += 1) await new Promise((r) => setTimeout(r, 10)); };
  await wait(() => doc.querySelectorAll('.grant-card').length === grants.length);
  const problems = [];
  const section = doc.getElementById('sec-grants');
  if (!section) problems.push('no #sec-grants');
  if (doc.querySelectorAll('.grant-card').length !== grants.length) problems.push('not every grant rendered');
  if (!doc.getElementById(`grant-${ids[1].slice(0, 8)}-word`)) problems.push('no RESUME input on the halted grant');
  if (!doc.getElementById(`grant-${ids[0].slice(0, 8)}-confirm`)) problems.push('no verify on the awaiting goal');
  if (doc.getElementById(`grant-${ids[3].slice(0, 8)}-revoke`)) problems.push('a revoke on a terminal grant');
  if (section.textContent.includes(GOAL)) problems.push('goal text rendered before the issuer view');
  const step = doc.getElementById(`grant-${ids[5].slice(0, 8)}-step`);
  if (!step) problems.push('no "Run one iteration" on the active goal');
  if (doc.getElementById(`grant-${ids[2].slice(0, 8)}-step`)) problems.push('a step on a paused grant');
  if (doc.getElementById(`grant-${ids[0].slice(0, 8)}-step`)) problems.push('a step while the goal awaits verification');
  if (!section.querySelector('a[href="/approvals"]')) problems.push('no link to the waiting card');
  // W6c: the awaiting claim shows its evidence; failing checks are amber with a "⚠" notice in
  // words (not colour alone); past the threshold "still waiting for you"; the confirmed claim
  // is shown read-only with no waiting notice.
  const { catalog } = require('../ui_shim.cjs');
  const awaiting = doc.querySelector(`[aria-labelledby="grant-${ids[0].slice(0, 8)}-title"]`);
  const verified = doc.querySelector(`[aria-labelledby="grant-${ids[4].slice(0, 8)}-title"]`);
  if (!awaiting || awaiting.dataset.claim !== 'fail') problems.push('the failed claim is not marked');
  const notes = awaiting ? [...awaiting.querySelectorAll('.grant-claim-note')].map((n) => n.textContent) : [];
  if (!notes.includes(catalog['grants.claim_fail']) || !catalog['grants.claim_fail'].startsWith('⚠')) problems.push('no ⚠ notice for the failed checks');
  if (!notes.includes(catalog['grants.claim_stale'])) problems.push('no "still waiting" notice');
  const text = awaiting ? awaiting.textContent : '';
  const minutes = catalog['grants.claim_since'].split('{time}')[1].replace('{n}', '31'); // waited 1900 s
  for (const want of [catalog['grants.claim_checks_fail'], catalog['grants.claim_workspace_changed'], catalog['grants.claim_progress_new'], 'pytest', minutes]) {
    if (!text.includes(want)) problems.push(`the evidence misses ${want}`);
  }
  if (!verified || verified.dataset.claim || verified.querySelector('.grant-claim-note')) problems.push('the verified claim is not read-only');
  // W6d: the flagged grant shows its heartbeat-start rows (known codes only).
  const ticking = doc.querySelector(`[aria-labelledby="grant-${ids[5].slice(0, 8)}-title"]`);
  for (const want of [catalog['grants.field_tick'], catalog['grants.tick_skip_min_spacing'], catalog['grants.field_tick_cost']]) {
    if (!ticking || !ticking.textContent.includes(want)) problems.push(`the heartbeat-start rows miss ${want}`);
  }
  const tickBox = doc.getElementById('grants-heartbeat-start');
  if (tickBox) tickBox.checked = true; // W6d page: the issue below carries the flag
  if (verified && !verified.textContent.includes(catalog['grants.field_claim_audit'])) problems.push('the verified claim is not shown');
  for (const node of section.querySelectorAll('button')) {
    if (node.type !== 'button' && node.id !== 'grants-issue') problems.push(`button without type=button: ${node.outerHTML.slice(0, 80)}`);
  }
  window.eval(axeSource);
  let result = await window.axe.run(section, { rules: { 'color-contrast': { enabled: false } } });
  const report = (r) => {
    for (const v of r.violations) problems.push(`[${v.impact}] ${v.id}: ${v.help}\n    ${v.nodes.map((n) => n.html).join('\n    ')}`);
  };
  report(result);
  // W5: one press of "Run one iteration" posts the step route once, with an empty body.
  if (step) {
    step.click();
    await wait(() => posted.some((p) => p.url === `/api/grants/${ids[5]}/step`));
    const steps = posted.filter((p) => p.url === `/api/grants/${ids[5]}/step`);
    if (steps.length !== 1 || JSON.stringify(steps[0].body) !== '{}') problems.push('step post');
    posted.length = 0;
  }
  // The issue: 428 → the dialog → confirm → re-send with the operation header.
  doc.getElementById('grants-goal').value = 'x';
  doc.getElementById('grants-subtree').value = '/tmp/ws';
  doc.getElementById('grants-confirm').value = 'ISSUE';
  doc.getElementById('grants-issue-form').dispatchEvent(new window.Event('submit', { cancelable: true }));
  await wait(() => posted.some((p) => p.url === '/api/grants/issue' && p.op));
  const flow = posted.map((p) => (p.op ? `${p.url}+op` : p.url));
  const expected = ['/api/grants/issue', `/api/grants/operations/${'9'.repeat(32)}/confirm`, '/api/grants/issue+op'];
  if (JSON.stringify(flow.slice(0, 3)) !== JSON.stringify(expected)) problems.push(`issue flow: ${flow.join(', ')}`);
  const issued = posted.find((p) => p.url === '/api/grants/issue');
  if (tickBox ? !issued || issued.body.heartbeat_start !== true : issued && 'heartbeat_start' in issued.body) problems.push('the heartbeat-start flag of the issue');
  const confirm = posted.find((p) => p.url.endsWith('/confirm'));
  if (!confirm || confirm.body.acknowledged !== true || confirm.body.token !== '8'.repeat(32)) problems.push('confirm body');
  // The issuer-only view: the token kept in this tab, the typed word, then the goal.
  await wait(() => doc.getElementById(`grant-${ids[0].slice(0, 8)}-show`));
  const show = doc.getElementById(`grant-${ids[0].slice(0, 8)}-show`);
  if (!show) problems.push('no issuer view control after the issue');
  else {
    show.value = 'show';  // mistyped: refused, the token must stay for a retry
    doc.getElementById(`grant-${ids[0].slice(0, 8)}-goal`).click();
    await new Promise((r) => setTimeout(r, 50));
    if (!window.sessionStorage.getItem(`stira.grant.view.${ids[0]}`)) problems.push('a refused view dropped the token');
    show.value = 'SHOW';
    doc.getElementById(`grant-${ids[0].slice(0, 8)}-goal`).click();
    await wait(() => section.textContent.includes(GOAL));
    if (!doc.getElementById(`grant-${ids[0].slice(0, 8)}-goal-view`).textContent.includes(GOAL)) problems.push('the issuer view shows no goal');
  }
  // Verify with the host command channel: poll, then re-send with the operation header.
  await wait(() => doc.getElementById(`grant-${ids[0].slice(0, 8)}-confirm`));
  doc.getElementById(`grant-${ids[0].slice(0, 8)}-confirm`).click();
  await wait(() => posted.some((p) => p.url === `/api/grants/${ids[0]}/verify` && p.op === '6'.repeat(32)));
  if (!posted.some((p) => p.url === `/api/grants/${ids[0]}/verify` && p.op === '6'.repeat(32))) problems.push('verify on the host channel was not re-sent');
  if (section.querySelector(`#grant-${ids[0].slice(0, 8)}-goal`)) problems.push('the one-time view is still offered');
  result = await window.axe.run(section, { rules: { 'color-contrast': { enabled: false } } });
  report(result);
  console.log(`grants panel: ${result.passes.length} axe passes, ${result.violations.length} violations`);
  window.close();
  if (problems.length) {
    console.log(`FAIL:\n  ${problems.join('\n  ')}`);
    process.exit(1);
  }
  console.log('OK');
  process.exit(0);
}

main().catch((err) => {
  console.log(`ERROR: ${err && err.stack ? err.stack : err}`);
  process.exit(1);
});
