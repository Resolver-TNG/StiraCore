// axe-core over the YOLO button / badge states (ADR 0012 phase 5b) on the chat and Stage
// pages as rendered with STIRA_YOLO_ALLOW, via jsdom. Colour contrast is not computable in
// jsdom (as in the other a11y checks here); it is measured in a real browser for the PR.
// usage: node yolo_axe.cjs <chat_yolo.html> <stage_yolo.html>
// Exit codes: 0 = ok, 1 = violations/error, 77 = deps missing (skip).
const fs = require('node:fs');
const path = require('node:path');
const { shimSource } = require('../ui_shim.cjs');
const { polyfillDialog } = require('../dialog_polyfill.cjs');

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
const W = 'ab'.repeat(16);
const R = 'cd'.repeat(16);
const base = (over = {}) => ({
  allowance: 'external', offered: true, unavailable: [], clock_supported: true, window: null, request: null,
  last_end: null, latched: false, latch_reason: null, latches: [], host_channel: 'browser',
  auto_approvals: { window: null, items: [] }, ...over,
});
const win = (over = {}) => ({
  id: W.slice(0, 8), state: 'active', tier: 'internal', t0: 1, ends_at: 21601, remaining_sec: 21000, cap: 2000,
  used: 3, reserved: 0, limit: 2000, auto_approved: 3, paused: false, pause_reason: null, resume_in_sec: null, ...over,
});
const STATES = {
  offered: base(),
  pending_host: base({ offered: false, host_channel: 'dialog', request: { id: R, short: R.slice(0, 8), state: 'pending', channel: 'dialog', expires_in: 80 } }),
  issuing: base({ offered: false, request: { id: R, short: R.slice(0, 8), state: 'issuing', channel: 'browser' } }),
  active: base({ offered: false, window: win() }),
  // the chat log gets the "before this page" line (the first read of a window with approvals)
  active_lines: base({ offered: false, window: win(), auto_approvals: { window: W.slice(0, 8),
    items: [{ n: 3, tool: 'file_write', ref: 'a3f2', limit: 2000, tier: 'internal' }] } }),
  active_external_soon: base({ offered: false, window: win({ tier: 'external', remaining_sec: 300,
    external_used: 4, external_limit: 200 }), auto_approvals: { window: W.slice(0, 8),
    items: [{ n: 3, tool: 'web_fetch', ref: 'b4c1', limit: 2000, tier: 'external', external_n: 4, external_limit: 200 }] } }),
  // phase 6: the external sub-cap used up — the note says external tools ask again
  active_external_used_up: base({ offered: false, window: win({ tier: 'external', external_used: 200,
    external_limit: 200 }) }),
  // phase 7: a window whose press chose "Include exec" (its tag, the exec count, a line)
  active_exec: base({ offered: false, window: win({ tier: 'external', external_used: 1, external_limit: 200,
    exec: true, exec_used: 2, exec_limit: 200 }), auto_approvals: { window: W.slice(0, 8),
    items: [{ n: 3, tool: 'shell', ref: 'c0de', limit: 2000, tier: 'exec', exec_n: 2, exec_limit: 200 }] } }),
  active_exec_used_up: base({ offered: false, window: win({ tier: 'external', external_limit: 200,
    exec: true, exec_used: 200, exec_limit: 200 }) }),
  paused: base({ offered: false, window: win({ state: 'paused', paused: true, pause_reason: 'breaker_warning', resume_in_sec: 90 }) }),
  ended: base({ last_end: { id: W.slice(0, 8), reason: 'expired_cap', class: 'normal', at: 2 } }),
  latched: base({ offered: false, latched: true, latches: [{ subject: 'window', window_id: W, reason: 'revoked_clock_anomaly' }],
    last_end: { id: W.slice(0, 8), reason: 'revoked_clock_anomaly', class: 'abnormal', at: 3 } }),
  // #594: the abnormal end's window stays "ended" in the status until Unlatch
  latched_ended_window: base({ offered: false, latched: true, latches: [{ subject: 'window', window_id: W, reason: 'revoked_clock_anomaly' }],
    window: win({ state: 'ended', reason: 'revoked_clock_anomaly', end_class: 'abnormal', remaining_sec: 0 }),
    last_end: { id: W.slice(0, 8), reason: 'revoked_clock_anomaly', class: 'abnormal', at: 3 } }),
  unavailable: base({ offered: false, unavailable: ['clock_not_measured'] }),
};

async function check(file, label, problems) {
  const html = fs.readFileSync(file, 'utf8');
  const dom = new JSDOM(html, { runScripts: 'outside-only', pretendToBeVisual: true });
  const { window } = dom;
  polyfillDialog(window);
  window.fetch = async () => ({ status: 404, ok: false, json: async () => ({}) }); // the auto instance: nothing
  window.eval(shimSource());
  window.eval(fs.readFileSync(path.join(JS, 'confirm_dialog.js'), 'utf8'));
  window.eval(fs.readFileSync(path.join(JS, 'yolo_ui.js'), 'utf8'));
  window.eval(axeSource);
  const doc = window.document;
  const slots = [...doc.querySelectorAll('.yolo-slot')];
  if (slots.length !== 1) problems.push(`${label}: expected one .yolo-slot, got ${slots.length}`);
  for (const [state, status] of Object.entries(STATES)) {
    for (const slot of slots) slot.replaceChildren();
    const badge = window.StiraYolo.create({
      doc, win: window, slots, storage: null,
      fetch: async () => ({ status: 200, ok: true, json: async () => JSON.parse(JSON.stringify(status)) }),
      post: async () => ({ status: 200, ok: true, json: async () => status }),
      setTimer: () => 0, clearTimer: () => {},
    });
    await badge.start();
    if (!slots[0].querySelector('.yolo-badge')) problems.push(`${label}/${state}: nothing drawn`);
    for (const node of slots[0].querySelectorAll('button')) {
      if (node.type !== 'button' || !node.textContent.trim()) problems.push(`${label}/${state}: unnamed or non-button control`);
    }
    const result = await window.axe.run(doc, { rules: { 'color-contrast': { enabled: false } } });
    for (const v of result.violations) {
      problems.push(`${label}/${state} [${v.impact}] ${v.id}: ${v.help}\n    ${v.nodes.map((n) => n.html).join('\n    ')}`);
    }
  }
  // The tier dialog (every choice offered, phase 7: "Include exec" too) passes too.
  for (const slot of slots) slot.replaceChildren();
  const badge = window.StiraYolo.create({
    doc, win: window, slots, storage: null,
    // #450: no tool for internal / external (the warning and its link), shell for exec.
    fetch: async () => ({ status: 200, ok: true, json: async () => base({ exec_offered: true, targets: {
      ask_total: 2, locked: false, excluded: [{ name: 'cron', reason: 'exec_runs_later' }],
      scopes: { internal: { count: 0, tools: [] }, external: { count: 0, tools: [] }, exec: { count: 1, tools: ['shell'] } },
    } }) }),
    post: async () => ({ status: 409, ok: false, json: async () => ({ code: 'yolo_busy' }) }),
    setTimer: () => 0, clearTimer: () => {},
  });
  await badge.start();
  slots[0].querySelector('.yolo-start').click();
  await new Promise((r) => setTimeout(r, 10));
  if (!doc.querySelector('#yolo-start-dialog[open]')) problems.push(`${label}: the tier dialog did not open`);
  if (!doc.querySelector('#yolo-start-targets .yolo-targets-warn')) problems.push(`${label}: no "no tool" warning`);
  const warned = await window.axe.run(doc, { rules: { 'color-contrast': { enabled: false } } });
  for (const v of warned.violations) problems.push(`${label}/dialog-none [${v.impact}] ${v.id}: ${v.help}`);
  // Phase 7: three choices; with "Include exec" chosen both warning texts are shown.
  const exec = doc.querySelector('#yolo-tier-exec');
  if (!exec) problems.push(`${label}: no "Include exec" choice`);
  else {
    exec.checked = true;
    exec.dispatchEvent(new window.Event('change', { bubbles: true }));
  }
  const result = await window.axe.run(doc, { rules: { 'color-contrast': { enabled: false } } });
  for (const v of result.violations) problems.push(`${label}/dialog [${v.impact}] ${v.id}: ${v.help}`);
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
  console.log(`OK: ${Object.keys(STATES).length} states + the tier dialog on chat and Stage`);
  process.exit(0);
}

main().catch((err) => {
  console.log(`ERROR: ${err && err.stack ? err.stack : err}`);
  process.exit(1);
});
