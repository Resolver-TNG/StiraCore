// axe-core over the live-rendered settings YOLO section (ADR 0012 phase 7c), via jsdom.
// usage: node yolo_settings_axe.cjs <settings.html>
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
const FIELDS = ['allow', 'offer_exec', 'max_auto_approvals', 'max_external', 'max_exec', 'host_confirm'];
const APPLIES = { allow: 'restart', offer_exec: 'restart', host_confirm: 'restart' };
const VALUES = { allow: 'external', offer_exec: true, max_auto_approvals: 300, max_external: 200, max_exec: 50, host_confirm: 'browser' };
const fields = {};
for (const name of FIELDS) {
  fields[name] = {
    saved: null, value: VALUES[name], source: name === 'max_auto_approvals' ? 'env' : name === 'allow' ? 'config' : 'default',
    running: name === 'allow' ? 'off' : VALUES[name], default: VALUES[name], env: name === 'offer_exec' ? null : `STIRA_YOLO_${name}`,
    pinned: name === 'max_auto_approvals', applies: APPLIES[name] || 'next_window', pending_restart: name === 'allow',
  };
}
const body = {
  fields, version: 'v1', enabled: false, exec_tools: false, exec_tools_env: 'STIRA_ENABLE_EXEC_TOOLS', busy: false,
  wildcard_bind: false, restart_pending: ['allow'], warnings: ['max_exec_rounded'], file: 'gateway-config.json',
};

async function main() {
  const html = fs.readFileSync(process.argv[2], 'utf8');
  const dom = new JSDOM(html, { runScripts: 'outside-only', pretendToBeVisual: true });
  const { window } = dom;
  window.eval(shimSource());
  window.fetch = async () => ({ ok: true, status: 200, json: async () => body });
  window.eval(fs.readFileSync(path.join(JS, 'settings_yolo.js'), 'utf8'));
  const doc = window.document;
  for (let i = 0; i < 20 && doc.getElementById('yset-fields').hidden; i += 1) await new Promise(r => setTimeout(r, 10));
  const problems = [];
  if (doc.getElementById('yset-fields').hidden) problems.push('fields stayed hidden');
  if (doc.getElementById('yset-restart').hidden) problems.push('restart notice not shown');
  if (!doc.getElementById('yset-cap').disabled) problems.push('pinned cap is editable');
  // An inline error, as typed.
  const external = doc.getElementById('yset-external');
  external.value = '9999';
  external.dispatchEvent(new window.Event('input'));
  if (doc.getElementById('err-yset-max_external').hidden) problems.push('no inline error');
  const section = doc.getElementById('sec-yolo');
  for (const node of section.querySelectorAll('button')) {
    if (node.type !== 'button') problems.push(`button without type=button: ${node.outerHTML.slice(0, 80)}`);
  }
  window.eval(axeSource);
  const result = await window.axe.run(section, { rules: { 'color-contrast': { enabled: false } } });
  for (const v of result.violations) {
    problems.push(`[${v.impact}] ${v.id}: ${v.help}\n    ${v.nodes.map(n => n.html).join('\n    ')}`);
  }
  console.log(`yolo settings section: ${result.passes.length} axe passes, ${result.violations.length} violations`);
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
