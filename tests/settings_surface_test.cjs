// Settings page: source / applies / restart pending (#469): node --test tests/settings_surface_test.cjs
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { resolve } = require('node:path');
const vm = require('node:vm');
const { shimSource, catalog } = require('./ui_shim.cjs');

const source = readFileSync(resolve(__dirname, '../dashboard/static/js/settings_surface.js'), 'utf8');

// A small DOM: enough for createElement / getElementById / querySelectorAll('[data-surface-field]').
class Node {
  constructor(doc, tag) {
    this.doc = doc; this.tagName = tag.toUpperCase(); this.children = []; this.parent = null;
    this.attributes = {}; this.dataset = {}; this.hidden = false; this.disabled = false; this._text = ''; this.className = '';
  }
  get textContent() { return this._text + this.children.map((n) => n.textContent).join(''); }
  set textContent(value) { this._text = String(value); this.children = []; }
  get id() { return this.attributes.id || ''; }
  set id(value) { this.attributes.id = String(value); }
  appendChild(node) { node.parent = this; this.children.push(node); return node; }
  insertAdjacentElement(where, node) {
    assert.equal(where, 'afterend');
    const siblings = this.parent.children;
    siblings.splice(siblings.indexOf(this) + 1, 0, node);
    node.parent = this.parent;
    return node;
  }
  setAttribute(key, value) { this.attributes[key] = String(value); }
  getAttribute(key) { return Object.prototype.hasOwnProperty.call(this.attributes, key) ? this.attributes[key] : null; }
  removeAttribute(key) { delete this.attributes[key]; }
  walk(out = []) { for (const c of this.children) { out.push(c); c.walk(out); } return out; }
  querySelectorAll(selector) {
    assert.equal(selector, '[data-surface-field]');
    return this.walk().filter((n) => n.getAttribute('data-surface-field') !== null);
  }
}

function makeDoc() {
  const doc = {
    body: null,
    createElement: (tag) => new Node(doc, tag),
    createTextNode: (text) => { const n = new Node(doc, '#text'); n._text = String(text); return n; },
    getElementById: (id) => doc.body.walk().find((n) => n.id === id) || null,
    querySelectorAll: (selector) => doc.body.querySelectorAll(selector),
    addEventListener: (name, fn) => { (doc.listeners[name] ||= []).push(fn); },
    listeners: {},
  };
  doc.body = new Node(doc, 'body');
  for (const id of ['restart-banner', 'location-facts', 'pinned-list', 'pinned-empty', 'restart-unknown']) {
    const el = doc.createElement(id === 'location-facts' ? 'dl' : 'p');
    el.id = id;
    doc.body.appendChild(el);
  }
  return doc;
}

function load(doc) {
  const sandbox = { document: doc, console };
  sandbox.window = sandbox;
  vm.createContext(sandbox);
  vm.runInContext(shimSource(), sandbox);
  vm.runInContext(source, sandbox);
  return sandbox;
}

const SECRET = 'value-that-never-reaches-the-page';

function surface(extra = {}) {
  return {
    fields: {
      'gateway.region': { applies: 'next_turn', env: ['AWS_REGION'], pinned_by: 'AWS_REGION', source: 'env', pending_restart: false },
      'gateway.hb_enabled': { applies: 'restart', env: ['STIRA_HEARTBEAT'], pinned_by: null, source: 'gateway', pending_restart: true, group: 'heartbeat' },
    },
    pinned: [{ name: 'AWS_REGION', fields: ['gateway.region'], applies: 'restart', class: 'A' }],
    restart_pending: ['gateway.hb_enabled', 'gateway.jev_enabled'],
    restart_pending_groups: ['heartbeat', 'classifier'],
    restart_known: true,
    location: {
      files: ['gateway-config.json', 'agent-config.json', 'tool-config.json'], same_directory: true,
      matches_data_dir: false, data_dir: 'default', anchor_env: 'STIRA_TOOL_RULES_ANCHOR_DIR',
      anchor_configured: false, tools_locked: false,
    },
    ...extra,
  };
}

test('the restart banner counts one entry per change and names them', () => {
  const doc = makeDoc();
  const box = load(doc);
  box.StiraSettingsSurface.render(doc, surface());
  const banner = doc.getElementById('restart-banner');
  assert.equal(banner.hidden, false);
  const expected = catalog['surface.restart_pending']
    .replace('{count}', '2')
    .replace('{fields}', `${catalog['surface.grp_heartbeat']} / ${catalog['hub.sec_classifier']}`);
  assert.ok(banner.textContent.includes(expected), banner.textContent);
  box.StiraSettingsSurface.render(doc, surface({ restart_pending: [], restart_pending_groups: [] }));
  assert.equal(banner.hidden, true);
});

test('the location card shows names and states, the pinned list names only', () => {
  const doc = makeDoc();
  const box = load(doc);
  box.StiraSettingsSurface.render(doc, surface());
  const facts = doc.getElementById('location-facts').textContent;
  assert.ok(facts.includes('gateway-config.json'));
  assert.ok(facts.includes(catalog['surface.not_in_data_dir']));
  assert.ok(facts.includes('STIRA_TOOL_RULES_ANCHOR_DIR'));
  const list = doc.getElementById('pinned-list');
  assert.equal(list.hidden, false);
  assert.equal(doc.getElementById('pinned-empty').hidden, true);
  assert.ok(list.textContent.includes('AWS_REGION'));
  assert.ok(list.textContent.includes(catalog['surface.fld_region']));
  assert.ok(!list.textContent.includes(SECRET));
  box.StiraSettingsSurface.render(doc, surface({ pinned: [] }));
  assert.equal(doc.getElementById('pinned-empty').hidden, false);
  assert.equal(list.hidden, true);
});

test('a pinned input is disabled with the variable name, and enabled again when unpinned', () => {
  const doc = makeDoc();
  const box = load(doc);
  const input = doc.createElement('input');
  input.id = 'region';
  input.setAttribute('data-surface-field', 'gateway.region');
  input.setAttribute('aria-describedby', 'region-desc');
  doc.body.appendChild(input);
  const free = doc.createElement('input');
  free.setAttribute('data-surface-field', 'gateway.hb_enabled');
  doc.body.appendChild(free);
  assert.equal(box.StiraSettingsSurface.applyPins(doc, surface()), 1);
  assert.equal(input.disabled, true);
  assert.equal(free.disabled, false);
  const note = doc.getElementById('pin-gateway-region');
  assert.ok(note && note.textContent.includes('AWS_REGION'));
  assert.equal(input.getAttribute('aria-describedby'), 'region-desc pin-gateway-region');
  const unpinned = surface();
  unpinned.fields['gateway.region'].pinned_by = null;
  box.StiraSettingsSurface.applyPins(doc, unpinned);
  assert.equal(input.disabled, false);
  assert.equal(note.hidden, true);
});

test('an input the hub disabled itself is not re-enabled by the surface', () => {
  const doc = makeDoc();
  const box = load(doc);
  const input = doc.createElement('input');
  input.setAttribute('data-surface-field', 'gateway.hb_enabled');
  input.disabled = true; // e.g. the section's own rule
  doc.body.appendChild(input);
  box.StiraSettingsSurface.applyPins(doc, surface());
  assert.equal(input.disabled, true);
});

test('the hub-state event renders without any call from the page', () => {
  const doc = makeDoc();
  load(doc);
  for (const fn of doc.listeners['stira:hub-state']) fn({ detail: { surface: surface() } });
  assert.equal(doc.getElementById('restart-banner').hidden, false);
});

test('every surface key the script uses exists in both catalogs', () => {
  const ja = JSON.parse(readFileSync(resolve(__dirname, '../dashboard/static/i18n/catalog.ja.json'), 'utf8'));
  const en = JSON.parse(readFileSync(resolve(__dirname, '../dashboard/static/i18n/catalog.en.json'), 'utf8'));
  const keys = [...source.matchAll(/(?<![\w.])t\("([\w.]+)"/g)].map((m) => m[1]);
  assert.ok(keys.length > 20);
  for (const key of keys) {
    assert.ok(key in ja, `ja: ${key}`);
    assert.ok(key in en, `en: ${key}`);
  }
});

test('an unavailable surface says so and shows no stale facts (#490 review)', () => {
  const doc = makeDoc();
  const box = load(doc);
  box.StiraSettingsSurface.render(doc, surface());
  const input = doc.createElement('input');
  input.setAttribute('data-surface-field', 'gateway.region');
  doc.body.appendChild(input);
  const degraded = {
    available: false, fields: {}, pinned: [], restart_pending: [], restart_pending_groups: [],
    restart_known: false, location: {},
  };
  box.StiraSettingsSurface.render(doc, degraded);
  const facts = doc.getElementById('location-facts').textContent;
  assert.ok(facts.includes(catalog['surface.unavailable']), facts);
  assert.ok(!facts.includes(catalog['surface.tools_unlocked']));
  assert.ok(!facts.includes('gateway-config.json'));
  assert.equal(doc.getElementById('restart-banner').hidden, true);
  assert.equal(doc.getElementById('pinned-list').hidden, true);
  assert.equal(doc.getElementById('pinned-empty').hidden, true);
  assert.equal(input.disabled, false);
});
