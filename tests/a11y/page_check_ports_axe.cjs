// axe-core over the settings page's page_check ports fieldset (ADR 0015 Addendum rev 4),
// rendered with two ports and then with an inline error, via jsdom.
// usage: node page_check_ports_axe.cjs <settings.html>
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
const body = { ports: [8080, 9000], locked: false, max_ports: 8, min_port: 1024, refused_ports: [5555] };

async function main() {
  const html = fs.readFileSync(process.argv[2], 'utf8');
  const dom = new JSDOM(html, { runScripts: 'outside-only', pretendToBeVisual: true });
  const { window } = dom;
  window.eval(shimSource());
  window.fetch = async () => ({ ok: true, status: 200, json: async () => body });
  window.eval(fs.readFileSync(path.join(JS, 'settings_page_check_ports.js'), 'utf8'));
  const doc = window.document;
  const list = doc.getElementById('pcports-list');
  for (let i = 0; i < 20 && !/9000/.test(list.textContent); i += 1) await new Promise(r => setTimeout(r, 10));
  const problems = [];
  if (!/localhost:9000/.test(list.textContent)) problems.push('ports not rendered');
  const input = doc.getElementById('pcports-new');
  input.value = '80';
  doc.getElementById('pcports-add').click();
  await new Promise(r => setTimeout(r, 10));
  if (doc.getElementById('err-pcports').hidden) problems.push('no inline error');
  const section = doc.getElementById('pcports');
  for (const node of section.querySelectorAll('button')) {
    if (node.type !== 'button') problems.push(`button without type=button: ${node.outerHTML.slice(0, 80)}`);
  }
  window.eval(axeSource);
  const result = await window.axe.run(section, { rules: { 'color-contrast': { enabled: false } } });
  for (const v of result.violations) {
    problems.push(`[${v.impact}] ${v.id}: ${v.help}\n    ${v.nodes.map(n => n.html).join('\n    ')}`);
  }
  console.log(`page_check ports fieldset: ${result.passes.length} axe passes, ${result.violations.length} violations`);
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
