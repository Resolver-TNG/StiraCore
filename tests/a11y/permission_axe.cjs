// axe-core over live-rendered permission cards (#244) on the chat page, via jsdom.
// usage: node permission_axe.cjs <chat.html>
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
const card = (id, extra = {}) => ({
  id, tool: 'file_write', operation: 'write', path: '~/Documents/x.txt', category: 'protected_by_os',
  remedy: 'tcc', errno: 'EPERM', evidence: ['tcc:~/Documents', 'errno:EPERM'],
  principal: { kind: 'terminal', name: 'Terminal', executable: '/usr/bin/python3' },
  deep_link: 'x-apple.systempreferences:com.apple.preference.security?Privacy_FilesAndFolders',
  message: "[Errno 1] Operation not permitted: '~/Documents/x.txt'", retry: 'available', recheck: null,
  ...extra,
});

async function main() {
  const html = fs.readFileSync(process.argv[2], 'utf8');
  const dom = new JSDOM(html, { runScripts: 'outside-only', pretendToBeVisual: true });
  const { window } = dom;
  window.eval(shimSource());
  window.eval(fs.readFileSync(path.join(JS, 'tool_cards.js'), 'utf8'));
  window.eval(fs.readFileSync(path.join(JS, 'permission_cards.js'), 'utf8'));
  const doc = window.document;
  const queue = window.StiraPermissions.create({
    document: doc, list: doc.getElementById('permission-list'), panel: doc.getElementById('permission-panel'),
    live: doc.getElementById('permission-live'),
    getJSON: async () => ({ cards: [card('a1'), card('a2', { category: 'locked_flag', remedy: 'locked_flag',
      deep_link: null, evidence: ['flag:uchg(file)'] })] }),
    post: async () => ({ ok: true, status: 200, json: async () => ({}) }),
  });
  await queue.refresh();
  const problems = [];
  const cards = doc.querySelectorAll('.permission-card');
  if (cards.length !== 2) problems.push(`expected 2 cards, got ${cards.length}`);
  if (doc.getElementById('permission-panel').hidden) problems.push('panel stayed hidden');
  for (const node of doc.querySelectorAll('[data-permission-action]')) {
    if (node.tagName !== 'BUTTON' || node.type !== 'button') problems.push(`non-native control ${node.outerHTML.slice(0, 80)}`);
  }
  for (const node of doc.querySelectorAll('[tabindex]')) {
    if (Number(node.getAttribute('tabindex')) > 0) problems.push(`positive tabindex: ${node.outerHTML.slice(0, 80)}`);
  }
  window.eval(axeSource);
  const result = await window.axe.run(doc, { rules: { 'color-contrast': { enabled: false } } });
  for (const v of result.violations) {
    problems.push(`[${v.impact}] ${v.id}: ${v.help}\n    ${v.nodes.map(n => n.html).join('\n    ')}`);
  }
  console.log(`permission cards: ${result.passes.length} axe passes, ${result.violations.length} violations`);
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
