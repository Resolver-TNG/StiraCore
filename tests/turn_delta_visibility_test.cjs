// Offline turn-delta DOM/SSE regression tests.
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
    this.classList = { add() {}, remove() {} }; this._textContent = ''; this.value = '';
  }
  get textContent() { return this._textContent; }
  set textContent(value) { this._textContent = String(value); this.children = []; }
  append(...nodes) { for (const node of nodes) { node.parent = this; this.children.push(node); } }
  replaceChildren(...nodes) { this.children = []; this.append(...nodes); }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter(node => node !== this); }
  setAttribute(key, value) { this.attributes[key] = value; }
  removeAttribute(key) { delete this.attributes[key]; }
  addEventListener(key, fn) { this.listeners[key] = fn; }
  click() {}
  focus() {}
  scrollIntoView() {}
}

const tick = () => new Promise(setImmediate);
const detected = {
  category: 'threat',
  deltas: { vigilance: 0.08, stability: -0.02, satisfaction: -0.03 },
  emotion_transcription: 'rule: threat (tool error)',
  breaker: 'normal'
};

async function setup({ history = [], turnSummary = detected } = {}) {
  const nodes = new Map();
  const get = id => { if (!nodes.has(id)) nodes.set(id, new Element()); return nodes.get(id); };
  const hitlButtons = ['approve', 'edit', 'reject', 'respond'].map(hitlAction => {
    const button = new Element(); button.dataset.hitlAction = hitlAction; return button;
  });
  vm.runInNewContext(require('./ui_shim.cjs').shimSource() + source, {
    document: {
      hidden: false,
      getElementById: get,
      createElement: () => new Element(),
      createTextNode: text => Object.assign(new Element(), { textContent: text }),
      querySelectorAll: selector => selector === '[data-hitl-action]' ? hitlButtons : []
    },
    fetch: async (url) => {
      if (url === '/api/chat') return {
        ok: true,
        headers: { get: () => 'text/event-stream' },
        body: new ReadableStream({ start(controller) {
          controller.enqueue(new TextEncoder().encode(
            `event: turn\ndata: ${JSON.stringify({ turn_summary: turnSummary })}\n\n`
          ));
          controller.close();
        } })
      };
      return { ok: true, json: async () => url.includes('history') ? { turns: history } : {} };
    },
    AbortSignal, TextDecoder, setInterval() {}
  });
  await tick();
  return { get };
}

function agentSummaries(conversation) {
  return conversation.children
    .filter(article => article.className === 'message agent')
    .map(article => article.children[2]);
}

test('detected stimulus renders signed deltas, breaker, and rationale on live turn', async () => {
  const { get } = await setup();
  get('message').value = 'the tool failed';

  await get('chat-form').listeners.submit({ preventDefault() {} });

  const [summary] = agentSummaries(get('conversation'));
  assert.equal(summary.hidden, false);
  assert.equal(
    summary.children[0].textContent,
    '⚡ threat 検知 → vigilance +0.08, stability -0.02, satisfaction -0.03 [breaker: normal]'
  );
  assert.equal(summary.children[1].textContent, 'rule: threat (tool error)');
  assert.equal(summary.children[0].className, 'turn-summary-line');
});

test('abstain or decay-only summary stays hidden', async () => {
  const { get } = await setup({
    turnSummary: { category: null, deltas: {}, emotion_transcription: null, breaker: 'normal' }
  });
  get('message').value = 'ok';

  await get('chat-form').listeners.submit({ preventDefault() {} });

  const [summary] = agentSummaries(get('conversation'));
  assert.equal(summary.hidden, true);
  assert.equal(summary.children.length, 0);
});

test('history restores turn summary on its matching agent bubble', async () => {
  const { get } = await setup({
    history: [{ message: 'problem', response: 'checked', turn_summary: detected }]
  });

  const [summary] = agentSummaries(get('conversation'));
  assert.equal(summary.hidden, false);
  assert.match(summary.children[0].textContent, /vigilance \+0\.08/);
  assert.equal(summary.children[1].textContent, detected.emotion_transcription);
});
