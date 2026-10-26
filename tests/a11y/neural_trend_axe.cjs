// axe-core + keyboard contract for the collapsible Neural State trend (#303), via jsdom.
// usage: node neural_trend_axe.cjs <chat.html>
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
const html = fs.readFileSync(process.argv[2], 'utf8');
const source = fs.readFileSync(path.join(JS, 'neural_trend.js'), 'utf8');
const problems = [];

function load(storageSeed) {
  const dom = new JSDOM(html, { runScripts: 'outside-only', pretendToBeVisual: true, url: 'http://127.0.0.1/chat' });
  const { window } = dom;
  if (storageSeed) for (const [k, v] of Object.entries(storageSeed)) window.localStorage.setItem(k, v);
  window.eval(shimSource());
  window.eval(source);
  const view = window.StiraTrend.create();
  return { dom, window, doc: window.document, view };
}

async function axe(window, label) {
  window.eval(axeSource);
  const result = await window.axe.run(window.document, { rules: { 'color-contrast': { enabled: false } } });
  for (const v of result.violations) {
    problems.push(`${label}: [${v.impact}] ${v.id}: ${v.help}\n    ${v.nodes.map(n => n.html).join('\n    ')}`);
  }
  console.log(`${label}: ${result.passes.length} axe passes, ${result.violations.length} violations`);
}

async function main() {
  const first = load();
  const { doc, window, view } = first;
  const toggle = doc.getElementById('neural-toggle');
  const body = doc.getElementById('neural-body');
  // Native button: Enter/Space activate it without custom key handlers.
  if (toggle.tagName !== 'BUTTON' || toggle.type !== 'button') problems.push(`toggle is not a native button: ${toggle.outerHTML.slice(0, 80)}`);
  if (toggle.closest('h3')?.id !== 'neural-title') problems.push('toggle is not inside the section heading');
  if (toggle.getAttribute('aria-controls') !== 'neural-body') problems.push('aria-controls must point at neural-body');
  if (toggle.getAttribute('aria-expanded') !== 'false') problems.push('initial aria-expanded must be false');
  if (!body.hidden) problems.push('body must be hidden by default');
  if (!body.contains(doc.getElementById('neural-disclaimer'))) problems.push('disclaimer must live inside the collapsible body');
  if (body.contains(doc.getElementById('neural-summary'))) problems.push('summary must stay outside the collapsible body');
  if (toggle.hasAttribute('tabindex')) problems.push('toggle must keep the native tab order');
  view.update({ stability: 0.21 }, 'critical');
  const badge = doc.getElementById('neural-summary-badge');
  if (badge.hidden || !/危険/.test(badge.textContent)) problems.push(`critical badge missing: ${badge.outerHTML}`);
  if (toggle.getAttribute('aria-expanded') !== 'false') problems.push('critical must not auto-expand');
  await axe(window, 'collapsed+critical');
  toggle.focus();
  toggle.click();
  if (doc.activeElement !== toggle) problems.push('focus must stay on the toggle');
  if (toggle.getAttribute('aria-expanded') !== 'true' || body.hidden) problems.push('click did not expand');
  if (window.localStorage.getItem('stira.chat.neuralTrend') !== 'expanded') problems.push('expanded state not stored');
  await axe(window, 'expanded');
  window.close();

  const second = load({ 'stira.chat.neuralTrend': 'expanded' });
  const t2 = second.doc.getElementById('neural-toggle');
  if (t2.getAttribute('aria-expanded') !== 'true' || second.doc.getElementById('neural-body').hidden) {
    problems.push('stored expanded state not restored after reload');
  }
  second.window.close();

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
