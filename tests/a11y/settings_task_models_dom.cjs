// Settings hub "Model per task" (docs/task-models.md) via jsdom: with a running server the list
// shows the running models, and a saved setting that would refuse the next start is named with
// its code (review r588 #4); without a running server the rows come from the next start.
// usage: node settings_task_models_dom.cjs <settings.html> <running.json> <standalone.json>
// Exit codes: 0 = ok, 1 = failure, 77 = deps missing (skip).
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const { shimSource } = require('../ui_shim.cjs');
const { polyfillDialog } = require('../dialog_polyfill.cjs');

let JSDOM;
try {
  ({ JSDOM } = require('jsdom'));
} catch (err) {
  console.log(`SKIP: jsdom not available (${err.message})`);
  process.exit(77);
}

const JS = path.join(__dirname, '..', '..', 'dashboard', 'static', 'js');
const tick = () => new Promise(r => setTimeout(r, 0));
async function settle() { for (let i = 0; i < 30; i += 1) await tick(); }

async function openPage(html, hub) {
  const dom = new JSDOM(html, { runScripts: 'outside-only', pretendToBeVisual: true, url: 'http://127.0.0.1/settings' });
  const { window } = dom;
  polyfillDialog(window);
  window.fetch = async (url) => {
    const json = body => ({ ok: true, status: 200, json: async () => body });
    if (url.startsWith('/api/config/history')) return json({ entries: [] });
    if (url.startsWith('/api/config/hub')) return json(hub);
    return json({});
  };
  window.setInterval = () => 0;
  window.eval(shimSource());
  for (const name of ['confirm_dialog.js', 'breaker_pairs.js', 'settings_surface.js', 'settings_hub.js']) {
    window.eval(fs.readFileSync(path.join(JS, name), 'utf8'));
  }
  await settle();
  return { window, $: id => window.document.getElementById(id) };
}

async function main() {
  const html = fs.readFileSync(process.argv[2], 'utf8');
  const running = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
  const standalone = JSON.parse(fs.readFileSync(process.argv[4], 'utf8'));

  // Running fine, the saved file would refuse the next start: both are on the page.
  let { window, $ } = await openPage(html, running);
  const items = [...$('cost-task-models-list').querySelectorAll('li')].map(li => li.textContent);
  assert.equal(items.length, 2, 'one row per task');
  assert.ok(items.every(text => !text.includes('unknown_model')), 'the running rows are fine');
  assert.equal($('cost-task-models-next').hidden, false, 'the next-start problem is shown');
  const next = $('cost-task-models-next').textContent;
  assert.equal(next, window.t('cost.task_model_next_error', { task: 'tool_summary', code: 'unknown_model' }));
  assert.notEqual(window.t('cost.task_model_next_error'), 'cost.task_model_next_error');

  // No running server: the rows are the next start's, the error inline.
  ({ window, $ } = await openPage(html, standalone));
  const rows = [...$('cost-task-models-list').querySelectorAll('li')].map(li => li.textContent);
  assert.ok(rows.includes(window.t('cost.task_model_error', { task: 'tool_summary', code: 'unknown_model' })), rows.join(' | '));
  assert.equal($('cost-task-models-next').hidden, true);
  console.log('ok: settings task models');
  process.exit(0);
}

main().catch(err => { console.error(err); process.exit(1); });
