// axe-core over the live-rendered settings Diagnostics section (#267), via jsdom.
// usage: node diagnostics_axe.cjs <settings.html>
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
const finding = (code, level, extra = {}) => ({ code, level, message: `server says ${code}`, ...extra });
const report = {
  version: 1, generated_at: '2026-10-18T15:00:00+0900', platform: 'darwin', ok: false,
  config_error: null, workspace: '@workspace',
  principal: { kind: 'terminal', name: 'Terminal', executable: '/usr/bin/python3' },
  roots: [
    { root: '@workspace', path: '@workspace', status: 'ok', findings: [], write_test: { ok: true } },
    { root: '~/Documents', path: '~/Documents', status: 'warning', write_test: null,
      findings: [finding('tcc_zone', 'warning', { zone: '~/Documents', remedy: 'tcc' })] },
    { root: '/ro', path: '/ro', status: 'error', write_test: null,
      findings: [finding('readonly_volume', 'error', { remedy: 'readonly_volume' })] },
  ],
  checks: [finding('classifier_rule_only', 'warning', { mode: 'rule_only', remedy: 'install_jev' })],
  notes: [finding('cwd_root', 'info')],
};

async function main() {
  const html = fs.readFileSync(process.argv[2], 'utf8');
  const dom = new JSDOM(html, { runScripts: 'outside-only', pretendToBeVisual: true });
  const { window } = dom;
  window.eval(shimSource());
  window.fetch = async () => ({ ok: true, status: 200, json: async () => report });
  window.eval(fs.readFileSync(path.join(JS, 'settings_diagnostics.js'), 'utf8'));
  const doc = window.document;
  // The script auto-initialises when loaded after the DOM; wait for its fetch to render.
  for (let i = 0; i < 20 && !doc.querySelector('#diag-roots li'); i += 1) await new Promise(r => setTimeout(r, 10));
  const problems = [];
  if (doc.querySelectorAll('#diag-roots > li').length !== 3) problems.push('expected 3 root entries');
  if (doc.getElementById('diag-checks-box').hidden) problems.push('checks box stayed hidden');
  const section = doc.getElementById('sec-diagnostics');
  for (const node of section.querySelectorAll('button')) {
    if (node.type !== 'button') problems.push(`button without type=button: ${node.outerHTML.slice(0, 80)}`);
  }
  if (section.querySelector('input, select, textarea, form')) problems.push('the section must have no form controls');
  window.eval(axeSource);
  const result = await window.axe.run(section, { rules: { 'color-contrast': { enabled: false } } });
  for (const v of result.violations) {
    problems.push(`[${v.impact}] ${v.id}: ${v.help}\n    ${v.nodes.map(n => n.html).join('\n    ')}`);
  }
  console.log(`diagnostics section: ${result.passes.length} axe passes, ${result.violations.length} violations`);
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
