// axe-core over the settings page's restart banner and "Where settings live" card (#469),
// rendered by settings_surface.js with a pinned variable and a pending restart, via jsdom.
// usage: node settings_surface_axe.cjs <settings.html>
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
const surface = {
  fields: {
    'gateway.region': { applies: 'next_turn', env: ['AWS_REGION'], pinned_by: 'AWS_REGION', source: 'env' },
  },
  pinned: [
    { name: 'AWS_REGION', fields: ['gateway.region'], applies: 'restart', class: 'A' },
    { name: 'STIRA_STAGE', fields: [], applies: 'restart', class: 'A' },
  ],
  restart_pending: ['gateway.jev_enabled'],
  restart_pending_groups: ['classifier'],
  restart_known: true,
  location: {
    files: ['gateway-config.json', 'agent-config.json', 'tool-config.json'], same_directory: true,
    matches_data_dir: true, data_dir: 'STIRA_DATA_DIR', anchor_env: 'STIRA_TOOL_RULES_ANCHOR_DIR',
    anchor_configured: true, tools_locked: true,
  },
};

async function main() {
  const html = fs.readFileSync(process.argv[2], 'utf8');
  const dom = new JSDOM(html, { runScripts: 'outside-only', pretendToBeVisual: true });
  const { window } = dom;
  window.eval(shimSource());
  window.eval(fs.readFileSync(path.join(JS, 'settings_surface.js'), 'utf8'));
  const doc = window.document;
  // An input a later section marks, pinned by a variable.
  const input = doc.createElement('input');
  input.id = 'probe-region';
  input.setAttribute('data-surface-field', 'gateway.region');
  const label = doc.createElement('label');
  label.htmlFor = 'probe-region';
  label.textContent = 'Region';
  const card = doc.getElementById('sec-location');
  card.append(label, input);
  doc.dispatchEvent(new window.CustomEvent('stira:hub-state', { detail: { surface } }));
  const problems = [];
  const banner = doc.getElementById('restart-banner');
  if (banner.hidden) problems.push('restart banner not shown');
  if (doc.getElementById('pinned-list').hidden) problems.push('pinned list not shown');
  if (!input.disabled) problems.push('pinned input is editable');
  if (!(input.getAttribute('aria-describedby') || '').includes('pin-gateway-region')) {
    problems.push('pinned input does not reference its reason');
  }
  window.eval(axeSource);
  const rules = { rules: { 'color-contrast': { enabled: false } } };
  let passes = 0;
  for (const scope of [card, doc.getElementById('hub-summary')]) {
    const result = await window.axe.run(scope, rules);
    passes += result.passes.length;
    for (const v of result.violations) {
      problems.push(`[${v.impact}] ${v.id}: ${v.help}\n    ${v.nodes.map(n => n.html).join('\n    ')}`);
    }
  }
  console.log(`settings surface: ${passes} axe passes, ${problems.length} problems`);
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
