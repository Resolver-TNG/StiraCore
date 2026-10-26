// Offline DOM/SSE regression tests: node --test tests/chat_indicator_test.cjs
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

async function setup() {
  const nodes = new Map();
  const get = id => { if (!nodes.has(id)) nodes.set(id, new Element()); return nodes.get(id); };
  const hitlButtons = ['approve', 'edit', 'reject', 'respond'].map(hitlAction => {
    const button = new Element(); button.dataset.hitlAction = hitlAction; return button;
  });
  let resolveRequest, rejectRequest;
  const request = new Promise((yes, no) => { resolveRequest = yes; rejectRequest = no; });
  vm.runInNewContext(require('./ui_shim.cjs').shimSource() + source, {
    document: { getElementById: get, createElement: () => new Element(),
      createTextNode: text => Object.assign(new Element(), { textContent: text }),
      querySelectorAll: selector => selector === '[data-hitl-action]' ? hitlButtons : [] },
    fetch: async url => url === '/api/chat' ? request : {
      ok: true, json: async () => url.includes('history') ? { turns: [] } : {}
    },
    AbortSignal, TextDecoder, setInterval() {}
  });
  await new Promise(setImmediate); // Finish initial history/status requests.
  get('message').value = 'hello';
  const completed = get('chat-form').listeners.submit({ preventDefault() {} });
  const article = get('conversation').children.at(-1);
  const body = article.children[1];
  assert.equal(body.children[0].className, 'thinking-indicator');
  assert.equal(body.children[0].attributes.role, 'img');
  assert.equal(body.children[0].attributes['aria-label'], '考え中');
  assert.equal(body.children[0].children.length, 3);
  assert.match(get('connection-status').textContent, /考え中/);
  assert.equal(get('send-button').disabled, true);
  return { get, body, article, completed, resolveRequest, rejectRequest };
}

function stream(ctx) {
  let controller;
  const body = new ReadableStream({ start(c) { controller = c; } });
  ctx.resolveRequest({ ok: true, headers: { get: () => 'text/event-stream' }, body });
  return {
    async emit(event, data) {
      controller.enqueue(new TextEncoder().encode(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`));
      await new Promise(setImmediate);
    },
    close() { controller.close(); }
  };
}

function checkDone(ctx) {
  assert.equal(ctx.body.children.some(n => n.className === 'thinking-indicator'), false);
  assert.equal(ctx.article.attributes['aria-busy'], undefined);
  assert.equal(ctx.get('send-button').disabled, false);
  assert.equal(ctx.get('message').disabled, false);
}

test('thinking -> responding -> ready; text survives indicator cleanup', async () => {
  const ctx = await setup(); const s = stream(ctx);
  await s.emit('delta', { text: 'hello' });
  assert.match(ctx.get('connection-status').textContent, /応答中/);
  assert.equal(ctx.body.children.map(n => n.textContent).join(''), 'hello');
  await s.emit('turn', {}); s.close(); await ctx.completed;
  checkDone(ctx); assert.match(ctx.get('connection-status').textContent, /準備完了/);
});

test('turn without delta clears indicator', async () => {
  const ctx = await setup(); const s = stream(ctx);
  await s.emit('turn', {}); s.close(); await ctx.completed; checkDone(ctx);
});

for (const mode of ['sse-error', 'network', 'http', 'interrupted', 'malformed']) {
  test(`${mode} clears indicator and restores input`, async () => {
    const ctx = await setup();
    if (mode === 'network') ctx.rejectRequest(new Error('offline'));
    else if (mode === 'http') ctx.resolveRequest({ ok: false, status: 503, json: async () => ({}) });
    else if (mode === 'malformed') ctx.resolveRequest({ ok: true, headers: { get: () => 'application/json' } });
    else {
      const s = stream(ctx);
      if (mode === 'sse-error') await s.emit('error', { error: 'max tokens' });
      else s.close();
    }
    await ctx.completed; checkDone(ctx);
    assert.match(ctx.get('connection-status').textContent, /失敗/);
  });
}

test('reduced-motion override follows the base animation rule', () => {
  const css = readFileSync(resolve(__dirname, '../dashboard/static/css/chat.css'), 'utf8');
  const override = css.lastIndexOf('@media (prefers-reduced-motion: reduce)');
  assert.ok(override > css.indexOf('animation: thinking-pulse'));
  assert.match(css.slice(override), /\.thinking-indicator span\s*\{\s*animation: none/);
});
