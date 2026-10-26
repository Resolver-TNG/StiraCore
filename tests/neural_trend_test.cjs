// Collapsible Neural State trend on /chat (#303): node --test tests/neural_trend_test.cjs
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { resolve } = require('node:path');
const vm = require('node:vm');
const { shimSource } = require('./ui_shim.cjs');
const { Element, settle, setup } = require('./chat_dom_mock.cjs');

const source = readFileSync(resolve(__dirname, '../dashboard/static/js/neural_trend.js'), 'utf8');
const KEY = 'stira.chat.neuralTrend';

function memoryStorage(initial = {}) {
  const data = new Map(Object.entries(initial));
  return {
    data,
    getItem: key => (data.has(key) ? data.get(key) : null),
    setItem: (key, value) => { data.set(key, String(value)); },
  };
}

// A fresh page: new DOM nodes (server markup is collapsed) + the given storage.
function page(storage) {
  const doc = {
    nodes: new Map(),
    getElementById(id) {
      if (!this.nodes.has(id)) { const node = new Element(this); node.id = id; this.nodes.set(id, node); }
      return this.nodes.get(id);
    },
    createElement: tag => new Element(doc, tag),
  };
  doc.getElementById('neural-toggle').setAttribute('aria-expanded', 'false');
  doc.getElementById('neural-body').hidden = true;
  const context = {};
  vm.runInNewContext(`${shimSource()}${source}`, context);
  const view = context.StiraTrend.create({ document: doc, storage, t: context.t });
  const get = id => doc.getElementById(id);
  return { view, get, toggle: get('neural-toggle'), body: get('neural-body'), section: get('neural-trend') };
}

test('collapsed by default with no stored preference', () => {
  const { view, toggle, body, section } = page(memoryStorage());
  assert.equal(toggle.getAttribute('aria-expanded'), 'false');
  assert.equal(body.hidden, true, 'disclaimer + sparklines hidden when collapsed');
  assert.equal(section.dataset.expanded, 'false');
  assert.equal(view.isOpen(), false);
});

test('toggle updates aria-expanded, hides/shows the body and persists the choice', () => {
  const storage = memoryStorage();
  const { toggle, body } = page(storage);
  toggle.click();
  assert.equal(toggle.getAttribute('aria-expanded'), 'true');
  assert.equal(body.hidden, false);
  assert.equal(storage.data.get(KEY), 'expanded');
  toggle.click();
  assert.equal(toggle.getAttribute('aria-expanded'), 'false');
  assert.equal(body.hidden, true);
  assert.equal(storage.data.get(KEY), 'collapsed');
});

test('open/closed state survives a reload', () => {
  const storage = memoryStorage();
  page(storage).toggle.click();
  const reloaded = page(storage);
  assert.equal(reloaded.toggle.getAttribute('aria-expanded'), 'true');
  assert.equal(reloaded.body.hidden, false);
  reloaded.toggle.click();
  const again = page(storage);
  assert.equal(again.toggle.getAttribute('aria-expanded'), 'false');
  assert.equal(again.body.hidden, true);
});

test('unknown stored values and broken storage fall back to collapsed without throwing', () => {
  for (const value of ['true', '1', 'EXPANDED', '', 'open']) {
    const { toggle, body } = page(memoryStorage({ [KEY]: value }));
    assert.equal(toggle.getAttribute('aria-expanded'), 'false', `stored ${JSON.stringify(value)}`);
    assert.equal(body.hidden, true);
  }
  const broken = { getItem() { throw new Error('denied'); }, setItem() { throw new Error('denied'); } };
  const { toggle, body } = page(broken);
  assert.equal(toggle.getAttribute('aria-expanded'), 'false');
  toggle.click();
  assert.equal(toggle.getAttribute('aria-expanded'), 'true', 'still usable when storage is blocked');
  assert.equal(body.hidden, false);
  const none = page(null);
  none.toggle.click();
  assert.equal(none.toggle.getAttribute('aria-expanded'), 'true');
});

test('summary shows stability and a text badge only for non-normal breaker levels', () => {
  const { view, get } = page(memoryStorage());
  const value = get('neural-summary-stability');
  const badge = get('neural-summary-badge');
  view.update({ stability: 0.724 }, 'normal');
  assert.equal(value.textContent, '72%');
  assert.equal(badge.hidden, true);
  assert.equal(badge.textContent, '');
  const cases = [
    ['warning', '▲警告'], ['WARNING', '▲警告'], ['critical', '■危険'], ['coherence', '◆一貫性低下'],
  ];
  for (const [level, text] of cases) {
    view.update({ stability: 0.25 }, level);
    assert.equal(badge.hidden, false, level);
    assert.equal(badge.textContent, text, level);
    assert.equal(badge.dataset.level, level.toLowerCase());
    // The glyph is decorative; the label carries the meaning (not colour alone).
    assert.equal(badge.children[0].getAttribute('aria-hidden'), 'true');
    assert.equal(badge.children[1].getAttribute('aria-hidden'), null);
  }
  for (const level of ['unknown', 'halt', '', null, undefined, 3, { level: 'critical' }, '__proto__']) {
    view.update({ stability: 0.5 }, level);
    assert.equal(badge.hidden, true, `level ${String(level)}`);
    assert.equal(badge.textContent, '');
  }
});

test('missing or non-finite stability is shown as unknown, never as 0', () => {
  const { view, get } = page(memoryStorage());
  for (const state of [null, undefined, {}, { stability: NaN }, { stability: '0.5' }, { stability: Infinity }]) {
    view.update(state, 'normal');
    assert.equal(get('neural-summary-stability').textContent, '—');
  }
});

test('WARNING/CRITICAL never auto-expands the panel', () => {
  const { view, toggle, body } = page(memoryStorage());
  view.update({ stability: 0.1 }, 'critical');
  view.update({ stability: 0.4 }, 'warning');
  assert.equal(toggle.getAttribute('aria-expanded'), 'false');
  assert.equal(body.hidden, true);
});

test('chat.js feeds the live gateway status into the summary', async () => {
  const ctx = setup({
    scripts: ['turn_ribbon.js', 'neural_trend.js', 'chat_core.js', 'chat.js'],
    status: { neural_state: { stability: 0.42 }, breaker: { level: 'warning' } },
  });
  await settle();
  assert.equal(ctx.get('neural-summary-stability').textContent, '42%');
  assert.equal(ctx.get('neural-summary-badge').hidden, false);
  assert.equal(ctx.get('neural-summary-badge').textContent, '▲警告');
});
