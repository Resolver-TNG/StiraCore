// Tool profiles on the settings page (#450), via jsdom: the cards, the preview (three rule
// groups, "needs STIRA_ENABLE_EXEC_TOOLS", the YOLO line), axe over the tools subsection with
// the preview open, and "Put in draft" -> Save -> the shared confirmation -> one POST whose
// source is "profile:<id>".
// usage: node settings_profiles_dom.cjs <settings.html> <hub.json> <ja|en>
// Exit codes: 0 = ok, 1 = failure, 77 = deps missing (skip).
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
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

const ROOT = path.join(__dirname, '..', '..');
const JS = path.join(ROOT, 'dashboard', 'static', 'js');
const tick = () => new Promise(r => setTimeout(r, 0));
async function settle() { for (let i = 0; i < 30; i += 1) await tick(); }

const TEXT = {
  ja: { env: '環境変数 STIRA_ENABLE_EXEC_TOOLS が必要', was: '（今: 常に許可）', none: /YOLO の自動承認は働きません/, dev: '開発', yolo: /内部のみ \d+ 個/ },
  en: { env: 'needs STIRA_ENABLE_EXEC_TOOLS', was: '(now: Always)', none: /YOLO approves nothing/, dev: 'Development', yolo: /Internal only \d+/ },
};

async function main() {
  const html = fs.readFileSync(process.argv[2], 'utf8');
  const hub = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
  const lang = process.argv[4] === 'en' ? 'en' : 'ja';
  const text = TEXT[lang];
  const catalog = JSON.parse(fs.readFileSync(path.join(ROOT, 'dashboard', 'static', 'i18n', `catalog.${lang}.json`), 'utf8'));
  const dom = new JSDOM(html, { runScripts: 'outside-only', pretendToBeVisual: true, url: 'http://127.0.0.1/settings' });
  const { window } = dom;
  polyfillDialog(window);
  if (!window.CSS) window.CSS = {};
  if (!window.CSS.escape) window.CSS.escape = s => String(s).replace(/[^a-zA-Z0-9_-]/g, c => `\\${c}`);
  const posts = [];
  window.fetch = async (url, options = {}) => {
    const json = body => ({ ok: true, status: 200, json: async () => body });
    if (options.method === 'POST') { posts.push([url, JSON.parse(options.body || '{}')]); return { ok: false, status: 409, json: async () => ({}) }; }
    if (url.startsWith('/api/config/history')) return json({ entries: [] });
    if (url.startsWith('/api/config/hub')) return json(hub);
    return json({});
  };
  window.setInterval = () => 0;
  window.eval(`var stiraFetch = (...args) => fetch(...args);
var __catalog = ${JSON.stringify(catalog)};
var t = (key, vars) => {
  let text = Object.prototype.hasOwnProperty.call(__catalog, key) ? __catalog[key] : key;
  if (vars) text = text.replace(/\\{(\\w+)\\}/g, (m, n) => (n in vars ? String(vars[n]) : m));
  return text;
};`);
  window.eval(fs.readFileSync(path.join(JS, 'confirm_dialog.js'), 'utf8'));
  // The settings page loads these before settings_hub.js (as in main's other settings harnesses).
  window.eval(fs.readFileSync(path.join(JS, 'breaker_pairs.js'), 'utf8'));
  window.eval(fs.readFileSync(path.join(JS, 'settings_surface.js'), 'utf8'));
  window.eval(fs.readFileSync(path.join(JS, 'settings_hub.js'), 'utf8'));
  await settle();
  const doc = window.document;

  // Three cards, named, none pressed (the saved rules match none).
  const cards = [...doc.querySelectorAll('#profile-list .profile-card')];
  assert.deepEqual(cards.map(c => c.dataset.profile), ['minimal', 'dev', 'research']);
  assert.ok(cards.every(c => c.getAttribute('aria-pressed') === 'false'));
  assert.equal(cards[1].querySelector('strong').textContent, text.dev);
  // The YOLO rule of thumb is on the page (#450 req. 3).
  assert.equal(doc.getElementById('policy-yolo-help').dataset.i18n, 'policy.yolo_help');

  // Minimal: no Ask tool -> the YOLO line warns.
  cards[0].click();
  await settle();
  const preview = doc.getElementById('profile-preview');
  assert.equal(preview.hidden, false);
  assert.equal(doc.getElementById('profile-preview-yolo').dataset.state, 'warn');
  assert.match(doc.getElementById('profile-preview-yolo').textContent, text.none);
  doc.getElementById('profile-cancel').click();
  await settle();
  assert.equal(preview.hidden, true);
  assert.equal(doc.activeElement, cards[0], 'focus back on the card');

  // Dev: the three groups, what changes, the exec variable, the YOLO counts.
  doc.querySelector('.profile-card[data-profile="dev"]').click();
  await settle();
  const groups = [...preview.querySelectorAll('.profile-group')];
  assert.deepEqual(groups.map(g => g.dataset.policy), ['always', 'ask', 'never']);
  const dev = hub.tool_profiles.find(p => p.id === 'dev');
  for (const group of groups) {
    const want = dev.rows.filter(r => r.policy === group.dataset.policy).map(r => r.name).sort();
    const got = [...group.querySelectorAll('li[data-tool]')].map(li => li.dataset.tool).sort();
    assert.deepEqual(got, want, group.dataset.policy);
  }
  const shell = preview.querySelector('.profile-group[data-policy="ask"] li[data-tool="shell"]');
  assert.equal(shell.querySelector('.badge.env').textContent, text.env);
  const write = preview.querySelector('.profile-group[data-policy="ask"] li[data-tool="file_write"]');
  assert.equal(write.querySelector('.profile-was').textContent, text.was);
  assert.equal(doc.getElementById('profile-preview-yolo').dataset.state, 'info');
  assert.match(doc.getElementById('profile-preview-yolo').textContent, text.yolo);

  // axe over the tools subsection with the preview open (jsdom: no color-contrast).
  window.eval(axeSource);
  const result = await window.axe.run(doc.getElementById('sec-tools'), { rules: { 'color-contrast': { enabled: false } } });
  if (result.violations.length) {
    for (const v of result.violations) console.log(`[${v.impact}] ${v.id}: ${v.help} (${v.nodes.map(n => n.html).join(' | ')})`);
    process.exit(1);
  }

  // Put in draft: nothing is posted yet; the rule radios follow the draft.
  doc.getElementById('profile-apply').click();
  await settle();
  assert.equal(preview.hidden, true);
  assert.deepEqual(posts, [], 'putting in the draft saves nothing');
  assert.equal(doc.querySelector('input[name="policy-file_write"][value="ask"]').checked, true);
  assert.equal(doc.querySelector('.profile-card[data-profile="dev"]').getAttribute('aria-pressed'), 'true');
  assert.equal(doc.activeElement, doc.getElementById('hub-save'));

  // Save: a loosening -> the shared confirmation; Esc posts nothing.
  doc.getElementById('hub-save').click();
  await settle();
  const dialog = doc.getElementById('stira-confirm');
  assert.ok(dialog && dialog.open, 'the shared confirmation opened');
  doc.dispatchEvent(new window.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
  await settle();
  assert.deepEqual(posts, []);
  doc.getElementById('hub-save').click();
  await settle();
  const input = doc.getElementById('stira-confirm-input');
  input.value = window.t('hub.confirm_word');
  input.dispatchEvent(new window.Event('input'));
  doc.getElementById('stira-confirm-ok').click();
  await settle();
  assert.equal(posts.length, 1);
  const [url, body] = posts[0];
  assert.equal(url, '/api/config/hub');
  assert.equal(body.source, 'profile:dev');
  assert.ok(body.acknowledged_dangers.includes('tool_policy_loosened'), JSON.stringify(body.acknowledged_dangers));
  assert.deepEqual([...body.changes.tools.ask_tools].sort(), [...dev.tools.ask_tools].sort());
  assert.deepEqual([...body.changes.tools.enabled_tools].sort(), [...dev.tools.enabled_tools].sort());
  console.log(`ok (${lang}): 3 profiles, preview + axe, confirmed save as profile:dev`);
  window.close();
}

main().catch((err) => { console.error(err); process.exit(1); });
