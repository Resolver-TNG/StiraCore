// #185: a chat message whose outcome is unknown is never re-sent automatically. It keeps
// its Idempotency-Key until re-sent (same key: the server answers 409) or discarded, and
// no other message can be sent meanwhile. Only no-effect refusals drop the key.
// node --test tests/chat_resend_test.cjs
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { resolve } = require('node:path');
const vm = require('node:vm');
// chat.js uses the shared core (#350), loaded first as on the page.
const source = ['chat_core.js', 'chat.js']
  .map(name => readFileSync(resolve(__dirname, '../dashboard/static/js', name), 'utf8')).join('\n');

class Element {
  constructor() {
    this.children = []; this.attributes = {}; this.listeners = {}; this.dataset = {};
    this.classList = { add() {}, remove() {} }; this.textContent = ''; this.value = '';
  }
  append(...nodes) { for (const node of nodes) { node.parent = this; this.children.push(node); } }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter(n => n !== this); }
  setAttribute(key, value) { this.attributes[key] = value; }
  removeAttribute(key) { delete this.attributes[key]; }
  addEventListener(key, fn) { this.listeners[key] = fn; }
  focus() {}
}

function sse(events) {
  const text = events.map(([event, data]) => `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`).join('');
  const body = new ReadableStream({
    start(c) { c.enqueue(new TextEncoder().encode(text)); c.close(); }
  });
  return { ok: true, status: 200, headers: { get: () => 'text/event-stream' }, body };
}

// Each /api/chat call takes the next scripted outcome; every call is recorded.
async function setup(outcomes) {
  const nodes = new Map();
  const get = id => { if (!nodes.has(id)) nodes.set(id, new Element()); return nodes.get(id); };
  const posts = [];
  let uuid = 0;
  vm.runInNewContext(require('./ui_shim.cjs').shimSource() + source, {
    document: { getElementById: get, createElement: () => new Element(),
      createTextNode: text => Object.assign(new Element(), { textContent: text }),
      querySelectorAll: () => [] },
    fetch: async (url, init = {}) => {
      if (url !== '/api/chat') return { ok: true, json: async () => url.includes('history') ? { turns: [] } : {} };
      posts.push({ key: init.headers['Idempotency-Key'], body: init.body });
      const next = outcomes.shift();
      if (next instanceof Error) throw next;
      return next;
    },
    crypto: { randomUUID: () => `key-${++uuid}` },
    AbortSignal, TextDecoder, setInterval() {}, setTimeout
  });
  await new Promise(setImmediate);
  const send = async text => {
    get('message').value = text;
    await get('chat-form').listeners.submit({ preventDefault() {} });
    await new Promise(setImmediate);
  };
  return { get, posts, send };
}

test('network failure: no automatic re-send; same message reuses the key', async () => {
  const ctx = await setup([new Error('offline'), sse([['turn', {}]])]);
  await ctx.send('hello');
  await new Promise(resolve => setTimeout(resolve, 20));
  assert.equal(ctx.posts.length, 1); // nothing re-sent on its own
  assert.match(ctx.get('connection-status').textContent, /届いたか不明/);
  assert.equal(ctx.get('message').value, 'hello'); // offered back, not re-sent
  await ctx.send('hello');
  assert.equal(ctx.posts.length, 2);
  assert.equal(ctx.posts[1].key, ctx.posts[0].key);
  assert.match(ctx.get('connection-status').textContent, /準備完了/);
});

test('stream cut before the turn ended counts as unknown delivery', async () => {
  const ctx = await setup([sse([['delta', { text: 'par' }]]), sse([['turn', {}]])]);
  await ctx.send('hello');
  assert.match(ctx.get('connection-status').textContent, /届いたか不明/);
  await ctx.send('hello');
  assert.equal(ctx.posts[1].key, ctx.posts[0].key);
});

test('duplicate answer: says it was already delivered and does not offer a new send', async () => {
  const duplicate = { ok: false, status: 409, json: async () => ({ error: 'duplicate request', code: 'duplicate_request' }) };
  const ctx = await setup([new Error('offline'), duplicate, sse([['turn', {}]])]);
  await ctx.send('hello');
  await ctx.send('hello');
  assert.equal(ctx.posts[1].key, ctx.posts[0].key);
  assert.match(ctx.get('connection-status').textContent, /二重送信を防止/);
  assert.equal(ctx.get('message').value, '');
  await ctx.send('hello'); // a deliberate new send afterwards is a new message
  assert.notEqual(ctx.posts[2].key, ctx.posts[0].key);
});

test('5xx and an SSE error event keep the key: the outcome is unknown', async () => {
  const fail = { ok: false, status: 503, json: async () => ({ error: 'unavailable' }) };
  for (const first of [fail, sse([['error', { error: 'max tokens' }]])]) {
    const ctx = await setup([first, sse([['turn', {}]])]);
    await ctx.send('hello');
    assert.match(ctx.get('connection-status').textContent, /届いたか不明/);
    assert.equal(ctx.get('unsettled-send').hidden, false);
    await ctx.send('hello');
    assert.equal(ctx.posts[1].key, ctx.posts[0].key);
    assert.equal(ctx.get('unsettled-send').hidden, true);
  }
});

test('only refusals that guarantee nothing ran (4xx validation/403/413/422, key reuse) drop the key', async () => {
  for (const [status, code] of [[400], [403], [413], [422], [409, 'idempotency_key_reused']]) {
    const refused = { ok: false, status, json: async () => ({ error: 'no', code }) };
    const ctx = await setup([refused, sse([['turn', {}]])]);
    await ctx.send('hello');
    assert.match(ctx.get('connection-status').textContent, /自動再送なし/, String(status));
    assert.equal(ctx.get('unsettled-send').hidden, true);
    await ctx.send('hello');
    assert.notEqual(ctx.posts[1].key, ctx.posts[0].key, String(status));
  }
  // Another 409 or a 429 is not a no-effect guarantee: the key stays.
  for (const [status, code] of [[409, 'other'], [429]]) {
    const odd = { ok: false, status, json: async () => ({ error: 'no', code }) };
    const ctx = await setup([odd, sse([['turn', {}]])]);
    await ctx.send('hello');
    await ctx.send('hello');
    assert.equal(ctx.posts[1].key, ctx.posts[0].key, String(status));
  }
});

test('a different message while one is unsettled is not sent; the panel asks to re-send or discard', async () => {
  const ctx = await setup([new Error('offline'), sse([['turn', {}]]), sse([['turn', {}]])]);
  await ctx.send('A');
  await ctx.send('B'); // blocked: A's outcome is unknown
  assert.equal(ctx.posts.length, 1);
  assert.equal(ctx.get('message').value, 'B'); // what the operator typed stays
  assert.match(ctx.get('connection-status').textContent, /結果が不明なため送信しませんでした/);
  assert.equal(ctx.get('unsettled-send').hidden, false);
  assert.match(ctx.get('unsettled-preview').textContent, /^A$/);
  // Re-send from the panel: A again, with A's key; the composer keeps B.
  await ctx.get('unsettled-resend').listeners.click();
  await new Promise(setImmediate);
  assert.equal(ctx.posts.length, 2);
  assert.equal(ctx.posts[1].key, ctx.posts[0].key);
  assert.equal(JSON.parse(ctx.posts[1].body).message, 'A');
  assert.equal(ctx.get('message').value, 'B');
  assert.equal(ctx.get('unsettled-send').hidden, true);
  await ctx.send('B'); // now allowed, as a new message
  assert.notEqual(ctx.posts[2].key, ctx.posts[0].key);
});

test('discard is the only way to drop an unknown outcome\'s key', async () => {
  const ctx = await setup([new Error('offline'), sse([['turn', {}]])]);
  await ctx.send('A');
  ctx.get('message').value = 'B';
  ctx.get('unsettled-discard').listeners.click();
  assert.equal(ctx.get('unsettled-send').hidden, true);
  assert.match(ctx.get('connection-status').textContent, /破棄しました/);
  assert.equal(ctx.get('message').value, 'B');
  await ctx.send('B');
  assert.equal(ctx.posts.length, 2);
  assert.notEqual(ctx.posts[1].key, ctx.posts[0].key);
});

test('a completed turn settles the send: the next one (even the same text) is new', async () => {
  const ctx = await setup([sse([['turn', {}]]), sse([['turn', {}]])]);
  await ctx.send('hello');
  await ctx.send('hello');
  assert.notEqual(ctx.posts[1].key, ctx.posts[0].key);
});
