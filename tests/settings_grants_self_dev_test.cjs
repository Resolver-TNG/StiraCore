// #791 (ADR 0029 B6b): the issue form's separate "self-development" tick, the refusal
// texts and the loosening line of the confirmation — node --test tests/settings_grants_self_dev_test.cjs
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const ROOT = path.join(__dirname, '..');
const CAT = {
  ja: JSON.parse(fs.readFileSync(path.join(ROOT, 'dashboard/static/i18n/catalog.ja.json'), 'utf8')),
  en: JSON.parse(fs.readFileSync(path.join(ROOT, 'dashboard/static/i18n/catalog.en.json'), 'utf8')),
};
const tr = (lang) => (key, vars = {}) => {
  const text = CAT[lang][key];
  if (text === undefined) throw new Error(`missing key ${key}`);
  return text.replace(/\{(\w+)\}/g, (m, k) => (k in vars ? String(vars[k]) : m));
};
const MARKER = 'b6cMarker_7d21';
const SOURCE = fs.readFileSync(path.join(ROOT, 'dashboard/static/js/settings_grants.js'), 'utf8')
  .replace('(typeof window !== "undefined" ? window : globalThis)', '(window)');

function panel(lang, extra = {}) {
  const ctx = { window: { ...extra }, document: undefined, t: tr(lang), setTimeout };
  vm.createContext(ctx);
  vm.runInContext(SOURCE, ctx);
  return ctx.window.StiraGrants;
}

function form(elements = {}) {
  const els = {
    'grants-goal': { value: 'tidy' }, 'grants-subtree': { value: ' /ws/stira ' }, 'grants-total': { value: '1000' },
    'grants-tick': { value: '100' }, 'grants-ttl': { value: '10' }, 'grants-iterations': { value: '2' },
    'grants-confirm': { value: 'ISSUE' }, 'grants-tool-file_read': { checked: true }, ...elements,
  };
  return { getElementById: (id) => els[id] || null, els };
}

test('the tick is sent only when the box is on the page and ticked', () => {
  const G = panel('en');
  assert.equal('self_development' in G.issueBody(form()), false); // the setting is off: no box
  assert.equal('self_development' in G.issueBody(form({ 'grants-self-development': { checked: false } })), false);
  for (const odd of [1, 'true', 'on', undefined]) {
    assert.equal('self_development' in G.issueBody(form({ 'grants-self-development': { checked: odd } })), false);
  }
  const body = G.issueBody(form({ 'grants-self-development': { checked: true } }));
  assert.equal(body.self_development, true);
  assert.equal(body.scope.workspace_subtree, '/ws/stira');
});

for (const lang of ['en', 'ja']) {
  test(`the refusals are shown in words (${lang})`, () => {
    const G = panel(lang);
    const box = { textContent: '', hidden: true };
    const doc = { getElementById: (id) => (id === 'grants-error' ? box : null) };
    for (const code of ['grant_subtree_protected', 'grant_subtree_running_checkout', 'grant_self_development_off']) {
      G.showError(doc, { code });
      assert.equal(box.textContent, CAT[lang][`grants.code_${code}`]);
      assert.equal(box.hidden, false);
      assert.ok(box.textContent.includes('autonomy.grant_self_development'));
    }
    G.showError(doc, { code: 'grant_field_invalid', reason: 'ttl_sec' }); // every other code: as before
    assert.equal(box.textContent, tr(lang)('grants.refused', { code: 'grant_field_invalid: ttl_sec' }));
    G.showError(doc, null);
    assert.equal(box.hidden, true);
  });

  test(`a self-development grant has the badge in words, drawn as text (${lang})`, () => {
    const G = panel(lang);
    const make = (tag) => ({
      tag, attrs: {}, children: [], dataset: {}, textContent: '', className: '',
      setAttribute(k, v) { this.attrs[k] = v; },
      appendChild(c) { this.children.push(c); return c; },
    });
    const els = { 'grants-status': make('p'), 'grants-list': make('div') };
    const doc = { createElement: make, getElementById: (id) => els[id] || null };
    const badges = (over) => {
      els['grants-list'].children = [];
      G.render(doc, { healthy: true, grants: [{ grant_id: 'a'.repeat(32), state: 'active', goal_state: 'active', headline: { subject: 'grant', state: 'active' }, ...over }] });
      assert.equal(els['grants-list'].children.length, 1);
      const found = [];
      const walk = (n) => { if (/\bgrant-self-development\b/.test(n.className)) found.push(n); n.children.forEach(walk); };
      els['grants-list'].children.forEach(walk);
      return found;
    };
    const found = badges({ self_development: true });
    assert.equal(found.length, 1);
    assert.equal(found[0].textContent, CAT[lang]['grants.self_development_badge']);
    assert.equal(found[0].attrs.title, CAT[lang]['grants.self_development_badge_title']);
    assert.ok(found[0].attrs.title.includes('docs/'));
    for (const odd of [undefined, false, 1, 'true', MARKER, { on: true }]) assert.equal(badges({ self_development: odd }).length, 0);
  });

  test(`the confirmation names the loosening (${lang})`, async () => {
    const operation = (changes) => ({
      id: '9'.repeat(32), confirm_token: '8'.repeat(32), instance: '7'.repeat(16), kind: 'grant_issue',
      summary: { action: 'grant_issue', tool: ['file_read'], count: 10, ...(changes ? { changes } : {}) },
    });
    const shown = [];
    const run = async (changes) => {
      const G = panel(lang, {
        stiraFetch: async (url, init) => {
          const first = url === '/api/grants/issue' && !init.headers['X-Stira-Host-Operation'];
          return first
            ? { ok: false, status: 428, json: async () => ({ code: 'grant_confirmation_required', operation: operation(changes) }) }
            : { ok: true, status: 200, json: async () => ({}) };
        },
        StiraConfirm: { open: async (options) => { shown.push(options); return true; } },
      });
      const done = await G.gated('/api/grants/issue', {}, 'issue', form());
      assert.equal(done.response.ok, true);
      return shown[shown.length - 1].items.join('\n');
    };
    const line = CAT[lang]['grants.confirm_self_development'];
    assert.ok((await run(['report_files:1', 'loosen:self_development'])).includes(line));
    assert.ok(!(await run(undefined)).includes(line));
    assert.ok(!(await run(['report_files:1'])).includes(line));
    const odd = await run([`loosen:${MARKER}`, MARKER, { loosen: 'self_development' }, 'loosen:self_development_x']);
    assert.ok(!odd.includes(MARKER) && !odd.includes(line)); // a value outside the list is never drawn
    assert.ok(!(await run('loosen:self_development')).includes(line)); // not a list
    // #683: the declared completion checks are drawn as their codes, only from the list
    const declared = await run(['done_when:pytest', 'done_when:ruff', `done_when:${MARKER}`]);
    assert.ok(declared.includes('done_when:pytest') && declared.includes('done_when:ruff'));
    assert.ok(!declared.includes(MARKER));
  });
}
