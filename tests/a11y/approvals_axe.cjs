// axe-core over live-rendered approval cards + history (#189), via jsdom.
// usage: node approvals_axe.cjs <approvals.html>
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
const NOW = 1_800_000_000;
const request = (id, extra = {}) => ({
  id, agent_id: 'stira', tool_name: 'memory_write', tool_input: { content: 'note' },
  normalized_input: '{"content":"note"}', args_hash: 'f'.repeat(64), break_level: 'critical',
  reason: 'CRITICAL HALT requires human approval for this write operation.', reason_code: 'breaker_critical_halt', tool_use_id: `u-${id}`,
  supersedes: null, cwd: '/srv/app', turn_id: 'turn-1234567890', created_at: NOW, expires_at: NOW + 90,
  ...extra,
});

async function main() {
  const file = process.argv[2];
  const html = fs.readFileSync(file, 'utf8');
  const dom = new JSDOM(html, { runScripts: 'outside-only', pretendToBeVisual: true });
  const { window } = dom;
  window.eval(shimSource());
  window.eval(fs.readFileSync(path.join(JS, 'tool_cards.js'), 'utf8'));
  window.eval(fs.readFileSync(path.join(JS, 'approvals.js'), 'utf8'));
  const doc = window.document;
  let pending = [request('r1'), request('r2', { supersedes: 'r0', cwd: null }),
    // ADR 0024: a restored card on breaker hold, with the replay counts of its batch.
    request('r3', { restored: true, resumable: true, break_level: 'normal', breaker_hold: true,
      replay: { from_ledger: 1, unknown: 1 } })];
  const history = [
    { id: 'h1', tool_name: 'memory_write', status: 'rejected', resolution_reason: 'no', turn_id: 'turn-1',
      resolver: 'operator', client_id: 'tab-x', resolved_at: NOW - 5, expires_at: NOW + 10, args_hash: 'a'.repeat(64) },
    { id: 'h2', tool_name: 'memory_write', status: 'expired', resolution_reason: 'approval expired', turn_id: null,
      resolver: 'system', resolved_at: NOW - 9, expires_at: NOW - 9, args_hash: 'b'.repeat(64) },
  ];
  const queue = window.StiraApprovals.create({
    document: doc, page: true, headingLevel: 3, now: () => NOW,
    list: doc.getElementById('approval-list'), live: doc.getElementById('approval-live'),
    history: doc.getElementById('approval-history'), empty: doc.getElementById('approvals-empty'),
    status: doc.getElementById('approvals-status'),
    getJSON: async url => (url.startsWith('/api/hitl/pending')
      ? { requests: pending, server_time: NOW } : { history, server_time: NOW }),
    post: async url => {
      pending = pending.filter(item => !url.includes(`/${item.id}/`));
      return { ok: true, status: 202, json: async () => ({}) };
    },
  });
  await queue.refresh();
  const problems = [];
  const cards = doc.querySelectorAll('.approval-card');
  if (cards.length !== 3) problems.push(`expected 3 cards, got ${cards.length}`);
  const held = cards[2].querySelector('.approval-hold');
  if (!held || held.hidden) problems.push('the held card does not show its hold note');
  if (!cards[2].querySelector('[data-approval-action="approve"]').disabled) problems.push('held approve is enabled');
  // Keyboard-only operation: native controls, no positive tabindex, decision moves focus.
  for (const node of doc.querySelectorAll('[tabindex]')) {
    if (Number(node.getAttribute('tabindex')) > 0) problems.push(`positive tabindex: ${node.outerHTML.slice(0, 80)}`);
  }
  for (const node of doc.querySelectorAll('[data-approval-action]')) {
    if (node.tagName !== 'BUTTON' || node.type !== 'button') problems.push(`non-native control ${node.outerHTML.slice(0, 80)}`);
  }
  const firstApprove = cards[0].querySelector('[data-approval-action="approve"]');
  firstApprove.focus();
  firstApprove.click();
  await new Promise(resolve => setTimeout(resolve, 20));
  const next = doc.querySelector('.approval-card [data-approval-action="approve"]');
  if (doc.activeElement !== next) problems.push(`focus after approve: ${doc.activeElement?.outerHTML?.slice(0, 80)}`);
  window.eval(axeSource);
  const result = await window.axe.run(doc, { rules: { 'color-contrast': { enabled: false } } });
  for (const v of result.violations) {
    problems.push(`[${v.impact}] ${v.id}: ${v.help}\n    ${v.nodes.map(n => n.html).join('\n    ')}`);
  }
  console.log(`approval cards: ${result.passes.length} axe passes, ${result.violations.length} violations`);
  window.close();
  if (problems.length) {
    console.log(`FAIL:\n  ${problems.join('\n  ')}`);
    process.exit(1);
  }
  console.log('OK');
  process.exit(0);
}

main().catch(err => {
  console.log(`ERROR: ${err && err.stack ? err.stack : err}`);
  process.exit(1);
});
