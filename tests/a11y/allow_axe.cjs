// axe-core over the session-allowance badge, its list and the approval card's allow-again
// buttons (ADR 0025 PR 2) on the chat and Stage pages, via jsdom. Colour contrast is not
// computable in jsdom (as in the other a11y checks here); it is checked in a real browser.
// usage: node allow_axe.cjs <chat.html> <stage.html>
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
const ID = 'ab'.repeat(16);
const row = (over = {}) => ({
  allow_id: ID, ref: ID.slice(0, 8), tool: 'file_write', class_kind: 'dir_subtree', label: 'projects/game/',
  whole_root: false, expires_at: 9000, remaining_sec: 7140, uses: 3, ...over,
});
const STATES = {
  none: { enabled: true, allowances: [], uses: [], seq: 0 },
  one: { enabled: true, allowances: [row()], uses: [], seq: 0 },
  many: { enabled: true, allowances: [row(), row({ allow_id: 'cd'.repeat(16), tool: 'web_fetch', class_kind: 'host', label: 'docs.python.org', uses: 0 }),
    row({ allow_id: 'ef'.repeat(16), tool: 'editor', whole_root: true, label: '' })], uses: [], seq: 0 },
};

async function check(file, label, problems) {
  const html = fs.readFileSync(file, 'utf8');
  const dom = new JSDOM(html, { runScripts: 'outside-only', pretendToBeVisual: true });
  const { window } = dom;
  window.fetch = async () => ({ status: 404, ok: false, json: async () => ({}) });
  window.eval(shimSource());
  window.eval(fs.readFileSync(path.join(JS, 'allow_ui.js'), 'utf8'));
  window.eval(axeSource);
  const doc = window.document;
  const slots = [...doc.querySelectorAll('.allow-slot')];
  if (slots.length !== 1) problems.push(`${label}: expected one .allow-slot, got ${slots.length}`);
  for (const [state, status] of Object.entries(STATES)) {
    for (const slot of slots) slot.replaceChildren();
    const badge = window.StiraAllow.create({
      document: doc, window, slots,
      getJSON: async () => JSON.parse(JSON.stringify(status)),
      post: async () => ({ json: async () => status }),
    });
    await badge.start();
    badge.stop();
    if (status.allowances.length) {
      slots[0].querySelector('.allow-toggle').click(); // the list open
      if (slots[0].querySelector('.allow-panel').hidden) problems.push(`${label}/${state}: the list did not open`);
    } else if (!slots[0].hidden) problems.push(`${label}/${state}: an empty badge is shown`);
    for (const node of slots[0].querySelectorAll('button')) {
      if (node.type !== 'button' || !node.textContent.trim()) problems.push(`${label}/${state}: unnamed or non-button control`);
    }
    const result = await window.axe.run(doc, { rules: { 'color-contrast': { enabled: false } } });
    for (const v of result.violations) {
      problems.push(`${label}/${state} [${v.impact}] ${v.id}: ${v.help}\n    ${v.nodes.map((n) => n.html).join('\n    ')}`);
    }
  }
  // ADR 0025 PR 3: the approval card with both buttons, the class line, and a card that says
  // why only "Approve once" is offered (chat page only: the Stage has its choice window).
  const list = doc.getElementById('approval-list');
  if (list) {
    window.eval(fs.readFileSync(path.join(JS, 'tool_cards.js'), 'utf8'));
    window.eval(fs.readFileSync(path.join(JS, 'approvals.js'), 'utf8'));
    const panel = doc.getElementById('hitl-panel');
    const queue = window.StiraApprovals.create({ document: doc, list, panel, live: doc.getElementById('approval-live'), getJSON: async () => ({ requests: [] }) });
    const base = (id, view) => ({
      id, agent_id: 'stira', tool_name: 'file_write', tool_input: { path: 'game/a.py', content: 'x' },
      normalized_input: '{"content":"x","path":"game/a.py"}', args_hash: 'a'.repeat(64), break_level: 'normal',
      reason: 'Tool policy is Ask: every call of this tool needs a human approval.', reason_code: 'rule_ask',
      created_at: 1, expires_at: 4102444800, status: 'pending', allow_again: view,
    });
    queue.render([
      base('r1', { offered: true, token: 'tk', class_kind: 'dir_subtree', label: 'projects/game/', max_hours: 2 }),
      base('r2', { offered: true, token: 'tk2', class_kind: 'host', label: 'docs.python.org', max_hours: 2, list_full: true }),
      base('r3', { offered: false, reason: 'exec' }),
    ], 1);
    const shown = [...list.querySelectorAll('.approval-allow')].filter((n) => !n.hidden).length;
    if (shown !== 2) problems.push(`${label}/card: expected two cards with the buttons, got ${shown}`);
    if (!list.querySelector('.approval-allow-reason:not([hidden])')) problems.push(`${label}/card: no not-offered line`);
    const result = await window.axe.run(doc, { rules: { 'color-contrast': { enabled: false } } });
    for (const v of result.violations) {
      problems.push(`${label}/card [${v.impact}] ${v.id}: ${v.help}\n    ${v.nodes.map((n) => n.html).join('\n    ')}`);
    }
  }
  window.close();
}

async function main() {
  const problems = [];
  await check(process.argv[2], 'chat', problems);
  await check(process.argv[3], 'stage', problems);
  if (problems.length) {
    console.log(`FAIL:\n  ${problems.join('\n  ')}`);
    process.exit(1);
  }
  console.log(`OK: ${Object.keys(STATES).length} states on chat and Stage`);
  process.exit(0);
}

main().catch((err) => {
  console.log(`ERROR: ${err && err.stack ? err.stack : err}`);
  process.exit(1);
});
