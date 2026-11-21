// #655: after a server restart the chat page is empty although turns.db and memory keep the
// earlier turns. The empty state then says why (a restart starts a new conversation);
// a first start, a conversation started with /new and a page with turns keep the plain text.
// node --test tests/chat_restart_note_test.cjs
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { shimSource } = require('./ui_shim.cjs');
const { Element, settle } = require('./chat_dom_mock.cjs');
const core = require('../dashboard/static/js/chat_core.js');

Element.prototype.prepend = function prepend(...nodes) {
  for (const node of nodes.reverse()) { node.remove(); node.parent = this; this.children.unshift(node); }
};
const read = (rel) => fs.readFileSync(path.join(__dirname, '..', rel), 'utf8');
const PLAIN = 'メッセージを送信してください';
const RESTARTED = JSON.parse(read('dashboard/static/i18n/catalog.ja.json'))['chat.restarted_empty'];
const START = { id: 's1', reason: 'start', started_at: 1000 };
const OLD_TURN = { turn_id: 't-old', started_at: 900, completed: true };

// The page as the template ships it: #empty-history inside #conversation.
function chatPage({ history = [], session = START, turns = [], turnsStatus = 200 }) {
  const doc = {
    hidden: false, activeElement: null, documentElement: { lang: 'ja' }, nodes: new Map(),
    getElementById(id) {
      if (!this.nodes.has(id)) { const node = new Element(this); node.id = id; this.nodes.set(id, node); }
      return this.nodes.get(id);
    },
    createElement: (tag) => new Element(doc, tag),
    createTextNode: (text) => Object.assign(new Element(doc, '#text'), { _text: String(text) }),
    querySelectorAll: () => [],
    addEventListener() {},
  };
  const empty = doc.getElementById('empty-history');
  empty.textContent = PLAIN;
  doc.getElementById('conversation').append(empty);
  // A removed node is gone from the document (the mock would hand out the old one).
  const remove = empty.remove.bind(empty);
  empty.remove = () => { remove(); doc.nodes.delete('empty-history'); };
  const lookup = doc.getElementById.bind(doc);
  doc.getElementById = (id) => (id === 'empty-history' && !doc.nodes.has(id) ? null : lookup(id));
  const json = (body, status = 200) => ({ ok: status < 300, status, json: async () => body });
  const fetch = async (url) => {
    if (url.startsWith('/api/chat/history')) return json({ turns: history, session });
    if (url.startsWith('/api/turns?')) return json({ turns }, turnsStatus);
    return json({});
  };
  const context = { document: doc, fetch, AbortSignal, TextDecoder, TextEncoder, ReadableStream, setInterval() {} };
  context.window = context;
  vm.runInNewContext(shimSource() + read('dashboard/static/js/chat_core.js') + read('dashboard/static/js/chat.js'), context);
  return { text: () => lookup('conversation').textContent };
}

test('core: restartedEmpty needs a process-start session, no turn and an earlier recorded turn', () => {
  const session = { id: 's1', reason: 'start', startedAt: 1000 };
  assert.equal(core.restartedEmpty(session, 0, OLD_TURN), true);
  assert.equal(core.restartedEmpty(session, 1, OLD_TURN), false, 'turns are on screen');
  assert.equal(core.restartedEmpty({ ...session, reason: 'new' }, 0, OLD_TURN), false, '/new has its own line');
  assert.equal(core.restartedEmpty(session, 0, null), false, 'a first start');
  assert.equal(core.restartedEmpty(session, 0, { turn_id: 't', started_at: 1001 }), false, 'a turn of this process');
  assert.equal(core.restartedEmpty(session, 0, { turn_id: 't', started_at: null }), false);
  assert.equal(core.restartedEmpty({ ...session, startedAt: null }, 0, OLD_TURN), false);
  assert.equal(core.restartedEmpty(null, 0, OLD_TURN), false, 'an older server: no session');
});

test('chat: empty history after a restart (turns.db has an earlier turn) says why', async () => {
  const page = chatPage({ turns: [OLD_TURN] });
  await settle(20);
  assert.ok(RESTARTED, 'the catalog has chat.restarted_empty');
  assert.equal(page.text(), RESTARTED);
});

test('chat: a first start (no recorded turn) keeps the plain empty text', async () => {
  const page = chatPage({ turns: [] });
  await settle(20);
  assert.equal(page.text(), PLAIN);
});

test('chat: without turn events (404) the plain empty text stays', async () => {
  const page = chatPage({ turns: [OLD_TURN], turnsStatus: 404 });
  await settle(20);
  assert.equal(page.text(), PLAIN);
});

test('chat: a conversation with turns shows them and no restart note', async () => {
  const page = chatPage({
    history: [{ turn_id: 't1', message: 'hello', response: 'hi', input_tokens: 1, output_tokens: 1 }],
    turns: [OLD_TURN],
  });
  await settle(20);
  assert.ok(page.text().includes('hello') && page.text().includes('hi'));
  assert.ok(!page.text().includes(RESTARTED) && !page.text().includes(PLAIN));
});

test('both catalogs carry the note, and it names where the record is', () => {
  const en = JSON.parse(read('dashboard/static/i18n/catalog.en.json'))['chat.restarted_empty'];
  assert.match(en, /restart/i);
  assert.match(en, /Observability/);
  assert.match(RESTARTED, /再起動/);
  assert.match(RESTARTED, /観測/);
});
