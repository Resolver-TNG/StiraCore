// Settings hub tool list (#281) + shared confirm dialog on save (#282), via jsdom.
// usage: node settings_tools_dom.cjs <settings.html> <hub.json>
// Exit codes: 0 = ok, 1 = failure, 77 = deps missing (skip).
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
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
const tick = () => new Promise(r => setTimeout(r, 0));
async function settle() { for (let i = 0; i < 30; i += 1) await tick(); }

async function main() {
  const html = fs.readFileSync(process.argv[2], 'utf8');
  const hub = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
  const dom = new JSDOM(html, { runScripts: 'outside-only', pretendToBeVisual: true, url: 'http://127.0.0.1/settings' });
  const { window } = dom;
  polyfillDialog(window);
  if (!window.CSS) window.CSS = {};
  if (!window.CSS.escape) window.CSS.escape = s => String(s).replace(/[^a-zA-Z0-9_-]/g, c => `\\${c}`);
  const posts = [];
  window.fetch = async (url, options = {}) => {
    const json = body => ({ ok: true, status: 200, json: async () => body });
    if (options.method === 'POST') { posts.push(url); return { ok: false, status: 409, json: async () => ({}) }; }
    if (url.startsWith('/api/config/history')) return json({ entries: [] });
    if (url.startsWith('/api/config/hub')) return json(hub);
    return json({});
  };
  window.setInterval = () => 0;
  window.eval(shimSource());
  window.eval(fs.readFileSync(path.join(JS, 'confirm_dialog.js'), 'utf8'));
  // Same order as the page: the breaker pair helpers (#471) load before the hub.
  window.eval(fs.readFileSync(path.join(JS, 'breaker_pairs.js'), 'utf8'));
  window.eval(fs.readFileSync(path.join(JS, 'settings_hub.js'), 'utf8'));
  await settle();
  const doc = window.document;

  // --- #281: one <details> per tool, closed unless stricter than the saved rule.
  const details = [...doc.querySelectorAll('.tool-item details.policy-details')];
  assert.equal(details.length, hub.tools.length, 'one details per tool');
  const rank = { allow: 0, prompt: 1, forbidden: 2 };
  const rule = { always: 'allow', ask: 'prompt', never: 'forbidden' };
  let opened = 0;
  for (const row of hub.tools) {
    const el = doc.querySelector(`details.policy-details[data-tool="${row.name}"]`);
    const stricter = rank[row.effective.decision] > rank[rule[row.policy]];
    assert.equal(el.open, stricter, `${row.name}: open only when stricter`);
    assert.equal(el.dataset.stricter === 'true', stricter, `${row.name}: marked`);
    if (stricter) opened += 1;
    // The effective decision stays visible in the summary while closed.
    const summary = el.querySelector('summary');
    assert.ok(summary.querySelector('.badge.effective'), `${row.name}: summary shows the effective decision`);
    // The three facts are still there (inside the details).
    assert.ok(el.querySelectorAll('.policy-facts > div').length >= 2, `${row.name}: facts kept`);
  }
  assert.ok(opened > 0, 'fixture has stricter tools (breaker = warning)');
  // Grant text once at the top; per row only the short pointer.
  const common = doc.getElementById('policy-grant-common');
  assert.match(common.textContent, /自動失効なし/);
  const longGrant = [...doc.querySelectorAll('.policy-facts dd')].filter(dd => /自動失効なし/.test(dd.textContent));
  assert.equal(longGrant.length, 0, 'the long grant text is not repeated per tool');
  const shortGrant = [...doc.querySelectorAll('.policy-facts dd')].filter(dd => dd.textContent === window.t('policy.grant_short'));
  assert.equal(shortGrant.length, hub.tools.filter(r => r.grant).length);
  // A user-opened row stays open after a full re-render (radio change elsewhere).
  const closed = details.find(el => !el.open);
  closed.open = true;
  closed.dispatchEvent(new window.Event('toggle'));

  // Transitions (review of #314): stricter always opens over a remembered "closed";
  // when it is no longer stricter the remembered state comes back.
  const reload = async () => { doc.dispatchEvent(new window.Event('stira:model-saved')); await settle(); };
  const box = name => doc.querySelector(`details.policy-details[data-tool="${name}"]`);
  const toggle = (name, open) => { const el = box(name); el.open = open; el.dispatchEvent(new window.Event('toggle')); };
  const reads = hub.tools.filter(r => r.policy === 'always' && r.effective.decision === 'allow').map(r => r.name);
  const [shut, kept] = reads.filter(name => name !== closed.dataset.tool);
  toggle(shut, true); toggle(shut, false);   // opened, then closed: remembered closed
  toggle(kept, true);                         // remembered open
  const rowOf = name => hub.tools.find(r => r.name === name);
  const allow = clone => JSON.parse(JSON.stringify(clone));
  const saved = { shut: allow(rowOf(shut).effective), kept: allow(rowOf(kept).effective) };
  for (const name of [shut, kept]) rowOf(name).effective = { decision: 'forbidden', decided_by: ['breaker'], reasons: ['breaker_critical'] };
  await reload();
  assert.equal(box(shut).open, true, 'stricter opens even though the user closed it');
  assert.equal(box(shut).dataset.stricter, 'true');
  assert.equal(box(kept).open, true);
  toggle(shut, false);                        // closing a forced-open row is not remembered
  await reload();
  assert.equal(box(shut).open, true, 'still stricter: open again');
  rowOf(shut).effective = saved.shut; rowOf(kept).effective = saved.kept;
  await reload();
  assert.equal(box(shut).open, false, 'no longer stricter: back to the remembered closed');
  assert.equal(box(kept).open, true, 'no longer stricter: back to the remembered open');
  assert.equal(box(shut).dataset.stricter, undefined);
  // The comparison uses the current choice: choosing Never for a stopped Ask tool is not "stricter".
  const askStopped = hub.tools.find(r => r.policy === 'ask' && r.effective.decision === 'forbidden');
  assert.equal(box(askStopped.name).open, true);
  const neverRadio = doc.querySelector(`input[name="policy-${askStopped.name}"][value="never"]`);
  neverRadio.checked = true;
  neverRadio.dispatchEvent(new window.Event("change"));
  await settle();
  assert.equal(box(askStopped.name).open, false, 'Never chosen: effective is not stricter than the choice');
  const askBack = doc.querySelector(`input[name="policy-${askStopped.name}"][value="ask"]`);
  askBack.checked = true;
  askBack.dispatchEvent(new window.Event('change'));
  await settle();
  assert.equal(box(askStopped.name).open, true, 'Ask again: stricter, open');

  // axe over the tools subsection (jsdom: no color-contrast).
  window.eval(axeSource);
  const result = await window.axe.run(doc.getElementById('sec-tools'), { rules: { 'color-contrast': { enabled: false } } });
  if (result.violations.length) {
    for (const v of result.violations) console.log(`[${v.impact}] ${v.id}: ${v.help} (${v.nodes.length})`);
    process.exit(1);
  }

  // --- #282: a loosening save opens the shared dialog; Esc cancels and nothing is posted.
  const never = hub.tools.find(r => r.policy === 'never' && r.exposed !== false && r.category === 'write');
  const radio = doc.querySelector(`input[name="policy-${never.name}"][value="ask"]`);
  radio.checked = true;
  radio.dispatchEvent(new window.Event('change'));
  await settle();
  assert.equal(doc.querySelector(`details.policy-details[data-tool="${closed.dataset.tool}"]`).open, true,
    'user-opened details survive the re-render');
  doc.getElementById('hub-save').click();
  await settle();
  const dialog = doc.getElementById('stira-confirm');
  assert.ok(dialog && dialog.open, 'shared dialog opened');
  assert.equal(doc.getElementById('stira-confirm-title').textContent, window.t('hub.danger_title'));
  assert.ok(dialog.querySelectorAll('li').length >= 1, 'lists the dangers');
  assert.equal(doc.activeElement, doc.getElementById('stira-confirm-input'));
  assert.equal(doc.getElementById('stira-confirm-ok').disabled, true);
  const dialogAxe = await window.axe.run(dialog, { rules: { 'color-contrast': { enabled: false } } });
  if (dialogAxe.violations.length) {
    for (const v of dialogAxe.violations) console.log(`[dialog ${v.impact}] ${v.id}: ${v.help}`);
    process.exit(1);
  }
  doc.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
  await settle();
  assert.equal(dialog.open, false);
  assert.deepEqual(posts, [], 'Esc: nothing saved');
  // Typing the word and confirming does post (the server still decides).
  doc.getElementById('hub-save').click();
  await settle();
  const input = doc.getElementById('stira-confirm-input');
  input.value = window.t('hub.confirm_word');
  input.dispatchEvent(new window.Event('input'));
  assert.equal(doc.getElementById('stira-confirm-ok').disabled, false);
  doc.getElementById('stira-confirm-ok').click();
  await settle();
  assert.deepEqual(posts, ['/api/config/hub']);
  console.log(`ok: ${details.length} tools, ${opened} open (stricter), dialog flow ok`);
  window.close();
}

main().catch((err) => { console.error(err); process.exit(1); });
