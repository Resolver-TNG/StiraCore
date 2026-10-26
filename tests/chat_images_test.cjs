// Offline attachment DOM and wire-format checks: node --test tests/chat_images_test.cjs
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
  replaceChildren(...nodes) { this.children = []; this.append(...nodes); }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter(n => n !== this); }
  setAttribute(key, value) { this.attributes[key] = value; }
  removeAttribute(key) { delete this.attributes[key]; }
  addEventListener(key, fn) { this.listeners[key] = fn; }
  click() { this.clicked = true; }
  focus() {}
}
const tick = () => new Promise(setImmediate);
async function setup({ fail = false } = {}) {
  const nodes = new Map(), requests = [];
  const get = id => { if (!nodes.has(id)) nodes.set(id, new Element()); return nodes.get(id); };
  const hitlButtons = ['approve', 'edit', 'reject', 'respond'].map(hitlAction => {
    const button = new Element(); button.dataset.hitlAction = hitlAction; return button;
  });
  class FileReader {
    readAsDataURL(file) {
      this.result = `data:${file.type};base64,UE5H`;
      queueMicrotask(() => file.fail ? this.onerror() : this.onload());
    }
  }
  vm.runInNewContext(require('./ui_shim.cjs').shimSource() + source, {
    document: { getElementById: get, createElement: () => new Element(),
      createTextNode: text => Object.assign(new Element(), { textContent: text }),
      querySelectorAll: selector => selector === '[data-hitl-action]' ? hitlButtons : [] },
    FileReader, AbortSignal, TextDecoder, setInterval() {},
    fetch: async (url, options) => {
      if (url !== '/api/chat') return { ok: true, json: async () => url.includes('history') ? { turns: [] } : {} };
      requests.push(JSON.parse(options.body));
      if (fail) return { ok: false, status: 400, json: async () => ({ error: 'Invalid image' }) };
      return { ok: true, headers: { get: () => 'text/event-stream' },
        body: new ReadableStream({ start(c) {
          c.enqueue(new TextEncoder().encode('event: turn\ndata: {}\n\n')); c.close();
        } }) };
    }
  });
  await tick();
  const select = files => get('image-input').listeners.change({ target: Object.assign(get('image-input'), { files }) });
  const submit = () => get('chat-form').listeners.submit({ preventDefault() {} });
  return { get, select, submit, requests };
}
const png = (extra = {}) => ({ name: 'screenshot.png', type: 'image/png', size: 3, ...extra });

test('file selection previews, image-only submit uses base64, success clears attachments', async () => {
  const c = await setup();
  c.get('attach-button').listeners.click(); assert.ok(c.get('image-input').clicked);
  const adding = c.select([png()]);
  assert.equal(c.get('send-button').disabled, true);
  await c.submit(); assert.equal(c.requests.length, 0);
  await adding;
  assert.equal(c.get('image-preview').children.length, 1);
  assert.equal(c.get('image-preview').hidden, false);
  await c.submit();
  assert.deepEqual(c.requests, [{ message: '', images: [{ type: 'image', source: {
    type: 'base64', media_type: 'image/png', data: 'UE5H'
  } }] }]);
  assert.equal(c.get('image-preview').hidden, true);
  const userBody = c.get('conversation').children[0].children[1];
  assert.equal(userBody.children[0].className, 'message-image');
});

test('clipboard paste and remove; empty submit sends nothing', async () => {
  const c = await setup(); let prevented = false;
  c.get('message').listeners.paste({ preventDefault() { prevented = true; },
    clipboardData: { items: [{ kind: 'file', getAsFile: () => png() }] } });
  await tick(); assert.ok(prevented);
  c.get('image-preview').children[0].children[1].listeners.click();
  assert.equal(c.get('image-preview').hidden, true);
  await c.submit(); assert.equal(c.requests.length, 0);
});

test('HTTP failure restores draft text and retains images without auto-retry', async () => {
  const c = await setup({ fail: true }); await c.select([png()]);
  c.get('message').value = 'describe'; await c.submit();
  assert.equal(c.get('message').value, 'describe');
  assert.equal(c.get('image-preview').children.length, 1);
  assert.equal(c.requests.length, 1);
  assert.equal(c.get('attach-button').disabled, false);
});

for (const [name, files] of [
  ['oversize', [png({ size: 10 * 1024 * 1024 + 1 })]],
  ['aggregate', [png({ size: 6 * 1024 * 1024 }), png({ size: 6 * 1024 * 1024 })]],
  ['unsupported', [png({ type: 'image/svg+xml' })]],
  ['count', Array.from({ length: 6 }, () => png())],
  ['reader failure', [png({ fail: true })]]
]) test(`${name} rejected before sending`, async () => {
  const c = await setup(); await c.select(files);
  assert.ok(c.get('image-error').textContent);
  assert.equal(c.get('image-preview').children.length, 0);
  await c.submit(); assert.equal(c.requests.length, 0);
  assert.equal(c.get('send-button').disabled, false);
});
