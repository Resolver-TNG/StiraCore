// Settings page: page_check's localhost ports (ADR 0015 Addendum rev 4, #449).
// node --test tests/settings_page_check_ports_test.cjs  (skipped without jsdom)
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { resolve } = require('node:path');
const { shimSource } = require('./ui_shim.cjs');

let JSDOM = null;
try { ({ JSDOM } = require('jsdom')); } catch (e) { JSDOM = null; }
const source = readFileSync(resolve(__dirname, '../dashboard/static/js/settings_page_check_ports.js'), 'utf8');
const HTML = `<fieldset id="pcports"><legend>x</legend><p id="pcports-help">h</p>
<p id="pcports-locked" hidden></p><label for="pcports-new">p</label>
<input id="pcports-new" type="number"><button id="pcports-add" type="button">Add</button>
<p id="err-pcports" hidden></p><p id="pcports-status"></p><ul id="pcports-list"></ul></fieldset>`;

function harness({ state = {}, answers = [], confirmResult = true } = {}) {
  const dom = new JSDOM(HTML, { runScripts: 'outside-only' });
  const { window } = dom;
  window.eval(shimSource());
  window.eval(source.replace(/\n\s*root\.document\.addEventListener\("DOMContentLoaded", start\);|\n\s*start\(\);/g, ''));
  const posts = [];
  const confirms = [];
  const ui = window.StiraPageCheckPorts.create({
    document: window.document,
    getJSON: async () => ({ ports: [], locked: false, max_ports: 8, min_port: 1024, refused_ports: [5555], ...state }),
    post: async (url, body) => {
      posts.push({ url, body });
      return answers.shift() || { ok: true, status: 200, data: { ports: body.ports, saved: true } };
    },
    confirm: async (spec) => { confirms.push(spec); return confirmResult; },
  });
  return { window, ui, posts, confirms, el: (id) => window.document.getElementById(id) };
}

const plain = (v) => JSON.parse(JSON.stringify(v));
const tick = () => new Promise((r) => setTimeout(r, 0));
const maybe = JSDOM ? test : test.skip;

maybe('validate explains early; the server still decides', () => {
  const h = harness();
  const v = h.window.StiraPageCheckPorts.validate;
  const s = { ports: [8080], max_ports: 2, min_port: 1024, refused_ports: [5555] };
  assert.deepEqual(plain(v('9090', s)), { port: 9090 });
  for (const [text, code] of [['', 'not_a_port'], ['80', 'port_refused'], ['70000', 'not_a_port'], ['8.5', 'not_a_port'],
    ['5555', 'own_dashboard'], ['8080', 'already'], ['-1', 'not_a_port']]) {
    assert.equal(v(text, s).error, code, text);
  }
  assert.equal(v('9091', { ...s, ports: [1, 2] }).error, 'too_many');
});

maybe('adding asks for the typed port, then sends confirm and confirm_ports', async () => {
  const h = harness({ state: { ports: [8080] } });
  await h.ui.load();
  assert.match(h.el('pcports-list').textContent, /http:\/\/localhost:8080\//);
  h.el('pcports-new').value = '9090';
  h.el('pcports-add').click();
  await tick(); await tick();
  assert.equal(h.confirms.length, 1);
  assert.equal(h.confirms[0].typed.word, '9090');
  assert.equal(h.confirms[0].tone, 'danger');
  assert.deepEqual(plain(h.posts[0].body), { ports: [8080, 9090], confirm: true, confirm_ports: [9090] });
  assert.match(h.el('pcports-list').textContent, /localhost:9090/);
  assert.equal(h.el('pcports-new').value, '');
});

maybe('a cancelled confirmation posts nothing', async () => {
  const h = harness({ confirmResult: false });
  await h.ui.load();
  h.el('pcports-new').value = '9090';
  h.el('pcports-add').click();
  await tick(); await tick();
  assert.equal(h.posts.length, 0);
});

maybe('an invalid port shows an inline error and never asks', async () => {
  const h = harness();
  await h.ui.load();
  h.el('pcports-new').value = '5555';
  h.el('pcports-add').click();
  await tick();
  assert.equal(h.confirms.length, 0);
  assert.equal(h.el('err-pcports').hidden, false);
  assert.equal(h.el('pcports-new').getAttribute('aria-invalid'), 'true');
});

maybe('removing sends the shorter list without a confirmation', async () => {
  const h = harness({ state: { ports: [8080, 9090] } });
  await h.ui.load();
  const remove = h.el('pcports-list').querySelectorAll('button')[0];
  assert.equal(remove.getAttribute('type'), 'button');
  assert.match(remove.getAttribute('aria-label'), /8080/);
  remove.click();
  await tick();
  assert.equal(h.confirms.length, 0);
  assert.deepEqual(plain(h.posts[0].body), { ports: [9090] });
});

maybe('a refusal from the server is shown and the list stays', async () => {
  const h = harness({ answers: [{ ok: false, status: 403, data: { error_code: 'breaker_active' } }] });
  await h.ui.load();
  h.el('pcports-new').value = '9090';
  h.el('pcports-add').click();
  await tick(); await tick();
  assert.equal(h.el('err-pcports').hidden, false);
  assert.ok(h.el('err-pcports').textContent.length > 0);
  assert.doesNotMatch(h.el('pcports-list').textContent, /9090/);
});

maybe('locked rules disable the controls', async () => {
  const h = harness({ state: { locked: true } });
  await h.ui.load();
  assert.equal(h.el('pcports-locked').hidden, false);
  assert.equal(h.el('pcports-add').disabled, true);
  assert.equal(h.el('pcports-new').disabled, true);
});
