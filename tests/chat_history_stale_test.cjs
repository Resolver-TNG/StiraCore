// #350 final round: after a successful reset whose new history cannot be read, the chat page
// keeps sending off until a re-read succeeds (and the Stage applies the same rule).
// node --test tests/chat_history_stale_test.cjs
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { shimSource } = require('./ui_shim.cjs');
const { Element, settle } = require('./chat_dom_mock.cjs');

Element.prototype.prepend = function prepend(...nodes) {
  for (const node of nodes.reverse()) { node.remove(); node.parent = this; this.children.unshift(node); }
};
const read = (rel) => fs.readFileSync(path.join(__dirname, '..', rel), 'utf8');

function chatPage() {
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
  doc.getElementById('history-stale').hidden = true;
  const state = { historyFails: false, posts: [], holds: [], turns: [] };
  // gate(): the next history read (in order) waits until the returned function is called;
  // gate(turns): that read then answers with these turns.
  const gate = (turns) => {
    let open;
    state.holds.push({ wait: new Promise((r) => { open = r; }), turns });
    return () => open();
  };
  const json = (body, status = 200) => ({ ok: status < 300, status, json: async () => body });
  const fetch = async (url, init = {}) => {
    if (url === '/api/chat' && init.method === 'POST') {
      state.posts.push(url);
      return {
        ok: true, headers: { get: () => 'text/event-stream' },
        body: new ReadableStream({ start(c) { c.enqueue(new TextEncoder().encode('event: turn\ndata: {}\n\n')); c.close(); } }),
      };
    }
    if (url === '/api/chat/session/new') {
      state.posts.push(url);
      return json({ status: 'started', session: { id: 's2', reason: 'new' } });
    }
    if (url.startsWith('/api/chat/history')) {
      if (state.historyFails) throw new TypeError('network (test)');
      const hold = state.holds.shift();
      if (hold) await hold.wait;
      return json({ turns: hold?.turns || state.turns, session: { id: 's2', reason: 'new', started_at: 1, request: null } });
    }
    return json({});
  };
  const context = {
    document: doc, fetch, AbortSignal, TextDecoder, TextEncoder, ReadableStream, setInterval() {},
    crypto: { randomUUID: (() => { let n = 0; return () => `k-${++n}`; })() },
  };
  context.window = context;
  context.StiraConfirm = { open: async () => true };
  vm.runInNewContext(shimSource() + read('dashboard/static/js/chat_core.js') + read('dashboard/static/js/chat.js'), context);
  const get = (id) => doc.getElementById(id);
  return { get, state, gate };
}

test('chat: reset ok, history re-read fails -> sending stays off; a successful reload lifts it', async () => {
  const { get, state } = chatPage();
  await settle(10);
  state.historyFails = true;
  get('new-session').click();
  await settle(20);
  assert.deepEqual(state.posts, ['/api/chat/session/new']);
  assert.equal(get('history-stale').hidden, false, 'the reload notice is shown');
  assert.equal(get('send-button').disabled, true);
  assert.equal(get('connection-status').textContent, '新しい履歴を読み込めませんでした。再読み込みしてください。');
  // a send now goes nowhere (also via Enter / requestSubmit)
  get('message').value = 'hello';
  for (const fn of get('chat-form').listeners.submit) await fn({ preventDefault() {} });
  await settle(10);
  assert.deepEqual(state.posts, ['/api/chat/session/new'], 'no POST /api/chat while the history is stale');
  // the reload fails again: still off
  get('history-reload').click();
  await settle(10);
  assert.equal(get('send-button').disabled, true);
  assert.equal(get('history-stale').hidden, false);
  // the reload works: on again, and a send goes out
  state.historyFails = false;
  get('history-reload').click();
  await settle(10);
  assert.equal(get('history-stale').hidden, true);
  assert.equal(get('send-button').disabled, false);
  get('message').value = 'hello';
  for (const fn of get('chat-form').listeners.submit) await fn({ preventDefault() {} });
  await settle(10);
  assert.deepEqual(state.posts, ['/api/chat/session/new', '/api/chat']);
});

test('stage: composerState refuses while the history is stale; only a successful re-read clears it', async () => {
  const { composerState } = await import('../dashboard/stage/js/talk.js');
  const base = { ready: true, busy: false, reading: false, offline: false, approval: false, turnOpen: false, unsettled: false };
  const c = composerState({ ...base, historyStale: true });
  assert.deepEqual([c.canSend, c.reason], [false, 'chat.new_history_failed']);
  assert.equal(composerState(base).canSend, true);
  const talk = read('dashboard/stage/js/talk.js');
  const refresh = talk.slice(talk.indexOf('async function refreshHistory('), talk.indexOf('function setLog('));
  const ok = refresh.indexOf('st.historyStale = false');
  assert.ok(ok > refresh.indexOf('fetchConversation(ctx.fetch)') && ok < refresh.indexOf('return true;', ok), 'cleared only after the history arrived');
  assert.doesNotMatch(refresh.slice(refresh.indexOf('catch')), /historyStale/);
  const show = talk.slice(talk.indexOf('async function showServerConversation('), talk.indexOf('async function startNew('));
  assert.match(show, /if \(!\(await refreshHistory\(\)\)\) \{[\s\S]{0,200}st\.historyStale = true;/);
  assert.match(talk, /historyStale: st\.historyStale, \.\.\.live/);
  assert.match(talk, /el\.historyReload\.hidden = !st\.historyStale/);
  assert.match(read('dashboard/templates/stage.html'), /id="stage-history-reload"[^>]*hidden/);
});

// #601 E5: a re-read of the history replaces the conversation; a message sent while it is on
// the way would have its bubbles dropped by the answer. Sending waits for the re-read.
test('chat: no send while a history re-read is on the way; on again when it lands (#601 E5)', async () => {
  const { get, state, gate } = chatPage();
  await settle(10);
  const open = gate();
  get('history-reload').click();
  await settle(5);
  assert.equal(get('send-button').disabled, true, 'send is off while the re-read is out');
  get('message').value = 'hello';
  for (const fn of get('chat-form').listeners.submit) await fn({ preventDefault() {} });
  await settle(5);
  assert.deepEqual(state.posts, [], 'nothing sent into a conversation about to be replaced');
  open();
  await settle(10);
  assert.equal(get('send-button').disabled, false);
  for (const fn of get('chat-form').listeners.submit) await fn({ preventDefault() {} });
  await settle(10);
  assert.deepEqual(state.posts, ['/api/chat']);
});

// #601 E5 review: two re-reads can overlap (another tab's turn ended + the reload button).
// The first one to end must not turn sending back on while the other is still out, and an
// older answer arriving last must not replace the newer one.
test('chat: overlapping history re-reads keep sending off until the last one ends (#601 E5)', async () => {
  const { get, state, gate } = chatPage();
  await settle(10);
  const turn = (message) => ({ message, response: `re: ${message}`, turn_id: null });
  const openA = gate([turn('old')]);
  const openB = gate([turn('old'), turn('newer')]);
  get('history-reload').click();
  get('history-reload').click();
  await settle(5);
  openB();
  await settle(10);
  assert.equal(get('send-button').disabled, true, 'A is still out: sending stays off');
  get('message').value = 'hello';
  for (const fn of get('chat-form').listeners.submit) await fn({ preventDefault() {} });
  await settle(5);
  assert.deepEqual(state.posts, [], 'nothing sent while a re-read is out');
  openA();
  await settle(10);
  assert.equal(get('send-button').disabled, false, 'on again when the last re-read ended');
  assert.match(get('conversation').textContent, /newer/, 'the older answer (A) did not replace B');
  for (const fn of get('chat-form').listeners.submit) await fn({ preventDefault() {} });
  await settle(10);
  assert.deepEqual(state.posts, ['/api/chat']);
});
