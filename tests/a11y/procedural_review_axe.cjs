// axe-core over the live-rendered procedural proposals panel on /memory (#463), via jsdom.
// usage: node procedural_review_axe.cjs <forest.html>
// Also checks: bodies/rationales/diffs are text (an injected <img> stays text), every
// control is a native button/textarea with a label, approve goes through the shared
// dialog (no POST without it), a reason being typed survives a re-render, and a breaker
// other than NORMAL shows its notice and disables Approve only (Reject stays usable).
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
const INJECTED = '<img src=x onerror="window.__xss=1">';
const proposal = (key, extra = {}) => ({
  key, body: `1. Read first.\n2. ${INJECTED}`, sha256: 'a'.repeat(64), length: 30,
  rationale: `seen twice ${INJECTED}`, source_turn: 'turn-1234', proposed_at: '2026-10-20T06:00:00+00:00',
  proposed_via: 'propose_procedure', legacy: false, revision: false, existing: null, diff: [],
  approvable: true, problem: null, ...extra,
});

async function main() {
  const file = process.argv[2];
  const html = fs.readFileSync(file, 'utf8');
  const dom = new JSDOM(html, { runScripts: 'outside-only', pretendToBeVisual: true, url: 'http://localhost/memory' });
  const { window } = dom;
  window.eval(shimSource());
  const posts = [];
  let state = {
    proposals: [
      proposal('safe_artifact_editing'),
      proposal('reply_style', {
        revision: true,
        existing: { sha256: 'b'.repeat(64), approved_by: 'local:operator', approved_at: '2026-10-19T00:00:00+00:00', approved: true },
        diff: [{ op: '@', text: '@@ -1,2 +1,2 @@' }, { op: ' ', text: '1. Read first.' }, { op: '-', text: '2. old' }, { op: '+', text: `2. ${INJECTED}` }],
      }),
    ],
    rules: [], reviews: [{ action: 'rejected', key: 'old', sha256: 'c'.repeat(64), by: 'local:operator', at: '2026-10-19T00:00:00+00:00', reason: 'too vague' }],
    limits: {}, prompt_limit: 5, effective: 'next_turn',
    breaker: { level: 'normal', approve_allowed: true },
  };
  window.fetch = async (url, init = {}) => {
    if ((init.method || 'GET') !== 'GET') posts.push(url);
    return { ok: true, status: 200, json: async () => JSON.parse(JSON.stringify(state)) };
  };
  window.stiraFetch = window.fetch;
  window.eval(fs.readFileSync(path.join(JS, 'procedural_review.js'), 'utf8'));
  await new Promise(resolve => setTimeout(resolve, 50));
  const doc = window.document;
  const problems = [];
  const cards = doc.querySelectorAll('article.proposal');
  if (cards.length !== 2) problems.push(`expected 2 proposals, got ${cards.length}`);
  if (doc.querySelector('#proposals-list img')) problems.push('a body/rationale/diff was rendered as HTML');
  if (window.__xss) problems.push('injected handler ran');
  if (!doc.querySelector('.proposal-diff .diff-add')) problems.push('no diff lines rendered');
  if (!doc.getElementById('proposals-effective').textContent.includes('5')) problems.push('effective note missing');
  for (const node of doc.querySelectorAll('#procedural-review button')) {
    if (node.type !== 'button') problems.push(`non-button control ${node.outerHTML.slice(0, 80)}`);
  }
  for (const node of doc.querySelectorAll('#procedural-review textarea')) {
    if (!doc.querySelector(`label[for="${node.id}"]`)) problems.push(`textarea without label ${node.id}`);
  }
  // Approve without the shared dialog (not loaded here): nothing is sent (fail closed).
  cards[0].querySelector('.btn-primary').click();
  await new Promise(resolve => setTimeout(resolve, 20));
  if (posts.length) problems.push(`approve sent without a confirmation: ${posts}`);
  // A typed reason survives a re-render caused by a new proposal.
  cards[1].querySelector('textarea').value = 'needs a source';
  state = { ...state, proposals: [...state.proposals, proposal('third')] };
  doc.getElementById('refresh').click();
  await new Promise(resolve => setTimeout(resolve, 50));
  const kept = doc.querySelector('article.proposal[data-key="reply_style"] textarea');
  if (!kept || kept.value !== 'needs a source') problems.push('typed reason lost on re-render');
  const note = doc.getElementById('proposals-breaker');
  if (!note || !note.hidden) problems.push('breaker notice shown at NORMAL');
  if (doc.querySelector('article.proposal .btn-primary').disabled) problems.push('approve disabled at NORMAL');
  window.eval(axeSource);
  const axeRun = async (label) => {
    const result = await window.axe.run(doc, { rules: { 'color-contrast': { enabled: false } } });
    for (const v of result.violations) {
      problems.push(`[${label}] [${v.impact}] ${v.id}: ${v.help}\n    ${v.nodes.map(n => n.html).join('\n    ')}`);
    }
    console.log(`procedural review (${label}): ${result.passes.length} axe passes, ${result.violations.length} violations`);
  };
  await axeRun('normal');
  // Breaker at WARNING: the notice is shown, Approve is disabled, Reject is not.
  state = { ...state, breaker: { level: 'warning', approve_allowed: false } };
  doc.getElementById('refresh').click();
  await new Promise(resolve => setTimeout(resolve, 50));
  if (note.hidden || !note.textContent.includes('WARNING')) problems.push('breaker notice missing at WARNING');
  for (const card of doc.querySelectorAll('article.proposal')) {
    if (!card.querySelector('.btn-primary').disabled) problems.push('approve enabled at WARNING');
    if (card.querySelector('.btn-secondary').disabled) problems.push('reject disabled at WARNING');
  }
  await axeRun('warning');
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
