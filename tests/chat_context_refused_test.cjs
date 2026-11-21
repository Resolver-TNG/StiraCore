// #746: context management that was asked for but refused is said on the chat page, in
// words (chat_core.contextRefusedText, from /api/gateway/status `context_management`).
// node --test tests/chat_context_refused_test.cjs
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const ROOT = path.join(__dirname, '..');
const core = require('../dashboard/static/js/chat_core.js');
const catalogs = {
  ja: JSON.parse(fs.readFileSync(path.join(ROOT, 'dashboard/static/i18n/catalog.ja.json'), 'utf8')),
  en: JSON.parse(fs.readFileSync(path.join(ROOT, 'dashboard/static/i18n/catalog.en.json'), 'utf8')),
};
const translate = (lang) => (key, vars) => String(catalogs[lang][key] ?? key)
  .replace(/\{(\w+)\}/g, (_, k) => (vars && k in vars ? vars[k] : `{${k}}`));
const read = (rel) => fs.readFileSync(path.join(ROOT, rel), 'utf8').replace(/\r\n/g, '\n');
// The server's codes (stiracore.agent.context_management.REFUSAL_REASONS).
const SERVER_CODES = [...read('src/stiracore/agent/context_management.py')
  .split('REFUSAL_REASONS: dict[str, str] = {')[1].split('\n}')[0].matchAll(/^ {4}"(\w+)":/gm)].map((m) => m[1]);

test('only a refused mode of an enabled feature is said', () => {
  const t = translate('ja');
  assert.equal(core.contextRefusedText({ enabled: true, mode: 'on', reason: null }, t), null);
  assert.equal(core.contextRefusedText({ enabled: false, mode: 'off', reason: null }, t), null);
  assert.equal(core.contextRefusedText({ enabled: true, mode: null, reason: null }, t), null, 'before the first turn');
  assert.equal(core.contextRefusedText({ enabled: true, mode: 'unsupported', reason: null }, t), null);
  assert.equal(core.contextRefusedText({ enabled: false, mode: 'refused', reason: 'no_stash_dir' }, t), null);
  assert.equal(core.contextRefusedText(undefined, t), null, 'an older server');
  assert.equal(core.contextRefusedText('refused', t), null);
});

test('every server reason code has its own sentence in both languages; the sign is not colour alone', () => {
  assert.deepEqual([...core.CONTEXT_REFUSALS].sort(), [...SERVER_CODES].sort());
  assert.equal(SERVER_CODES.length, 4);
  for (const lang of ['ja', 'en']) {
    const seen = new Set();
    for (const code of SERVER_CODES) {
      const view = core.contextRefusedText({ enabled: true, mode: 'refused', reason: code }, translate(lang));
      assert.equal(view.reason, code);
      assert.ok(catalogs[lang][`chat.ctx_refused_${code}`], `${lang} ${code}`);
      assert.ok(view.text.startsWith('⚠ '), 'a sign and words, not colour alone');
      assert.ok(view.text.includes(catalogs[lang][`chat.ctx_refused_${code}`]), 'the reason is in the visible text');
      assert.doesNotMatch(view.text, /\{\w+\}|chat\.ctx_/, 'no unfilled placeholder, no bare key');
      assert.ok(!view.text.includes(code), 'words, not the code');
      seen.add(view.text);
    }
    assert.equal(seen.size, SERVER_CODES.length, 'one sentence per reason');
  }
  assert.match(core.contextRefusedText({ enabled: true, mode: 'refused', reason: 'stash_outside_write_roots' }, translate('ja')).text,
    /動いていません.*context-stash/);
});

test('a reason outside the list is never echoed: it reads "unknown"', () => {
  for (const reason of [null, undefined, 42, '/home/me/secret-project', 'sk-test-abc', '<b>x</b>']) {
    for (const lang of ['ja', 'en']) {
      const view = core.contextRefusedText({ enabled: true, mode: 'refused', reason }, translate(lang));
      assert.equal(view.reason, 'unknown');
      assert.ok(view.text.includes(catalogs[lang]['chat.ctx_refused_unknown']));
      assert.ok(!view.text.includes(String(reason)));
    }
  }
});

test('the chat page: a hidden status line by the gauge, written from the status with textContent', () => {
  const html = read('dashboard/templates/chat.html');
  assert.match(html, /<p id="ctx-refused" class="ctx-refused" role="status" hidden><\/p>/);
  const js = read('dashboard/static/js/chat.js');
  assert.match(js, /core\.contextRefusedText\?\.\(status\.context_management, t\)/);
  assert.match(js, /renderContextRefused\(status\)/);
  const body = js.split('function renderContextRefused(status) {')[1].split('\n  }')[0];
  assert.match(body, /el\.hidden = !view;/);
  assert.match(body, /el\.textContent = view \? view\.text : "";/);
  assert.doesNotMatch(body, /innerHTML/);
  const css = read('dashboard/static/css/chat.css');
  assert.match(css, /\.ctx-refused\[hidden\] \{ display: none; \}/);
  assert.match(css, /\.ctx-refused \{[^}]*border: 1px solid var\(--warn-border\)/, 'a border, not colour alone');
});
