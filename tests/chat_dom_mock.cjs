// Shared mock DOM + chat.js harness for vm tests (#187/#188).
const { readFileSync } = require('node:fs');
const { resolve } = require('node:path');
const vm = require('node:vm');
const { shimSource } = require('./ui_shim.cjs');

const read = name => readFileSync(resolve(__dirname, '../dashboard/static/js', name), 'utf8');

class Element {
  constructor(doc, tag = 'div') {
    this.doc = doc; this.tagName = tag; this.children = []; this.attributes = {};
    this.listeners = {}; this.dataset = {}; this._text = ''; this.value = ''; this.hidden = false;
    this.className = ''; this.parent = null;
    const classes = new Set();
    this.classList = { add: c => classes.add(c), remove: c => classes.delete(c), contains: c => classes.has(c) };
  }
  get textContent() { return this._text + this.children.map(node => node.textContent).join(''); }
  set textContent(value) { this._text = String(value); this.children = []; }
  append(...nodes) { for (const node of nodes) { node.remove(); node.parent = this; this.children.push(node); } }
  insertBefore(node, ref) {
    node.remove();
    const index = this.children.indexOf(ref);
    node.parent = this;
    if (index < 0) this.children.push(node); else this.children.splice(index, 0, node);
  }
  replaceChildren(...nodes) { this.children = []; this._text = ''; this.append(...nodes); }
  remove() { if (this.parent) this.parent.children = this.parent.children.filter(n => n !== this); this.parent = null; }
  setAttribute(key, value) { this.attributes[key] = String(value); }
  getAttribute(key) { return this.attributes[key] ?? null; }
  removeAttribute(key) { delete this.attributes[key]; }
  addEventListener(key, fn) { (this.listeners[key] ||= []).push(fn); }
  dispatch(key, event = {}) { for (const fn of this.listeners[key] || []) fn(event); }
  click() { this.dispatch('click', { preventDefault() {} }); }
  focus() { this.doc.activeElement = this; }
  scrollIntoView() {}
  find(predicate) {
    for (const child of this.children) {
      if (predicate(child)) return child;
      const found = child.find(predicate);
      if (found) return found;
    }
    return null;
  }
  findAll(predicate, out = []) {
    for (const child of this.children) { if (predicate(child)) out.push(child); child.findAll(predicate, out); }
    return out;
  }
}

const byClass = name => node => node.className.split(' ').includes(name);
const tick = () => new Promise(setImmediate);
const settle = async (n = 6) => { for (let i = 0; i < n; i++) await tick(); };

const SUMMARY = {
  category: 'threat', intensity: 1.0, confidence: 0.82, classifier: 'RuleBasedClassifier',
  emotion_transcription: 'rule: threat (tool error)',
  deltas: { vigilance: 0.08, stability: -0.02 },
  before: { trust: 0.5, curiosity: 0.5, vigilance: 0.1, attachment: 0.5, stability: 0.7, satisfaction: 0.5 },
  after: { trust: 0.5, curiosity: 0.5, vigilance: 0.18, attachment: 0.5, stability: 0.68, satisfaction: 0.5 },
  breaker: 'normal',
};
const ev = (turnId, seq, type, data = {}) => ({
  schema_version: 1, event_id: `e${seq}`, turn_id: turnId, seq, ts: 1760246400 + seq, type, data,
});

function setup({ history = [], turns = [], events = {}, search = '', sse = [], status = {}, payloads = {},
  scripts = ['turn_ribbon.js', 'chat_core.js', 'chat.js'] } = {}) {
  const doc = {
    hidden: false, activeElement: null, keydown: [],
    getElementById(id) {
      if (!this.nodes.has(id)) { const node = new Element(this); node.id = id; this.nodes.set(id, node); }
      return this.nodes.get(id);
    },
    createElement: tag => new Element(doc, tag),
    createTextNode: text => Object.assign(new Element(doc, '#text'), { _text: String(text) }),
    querySelectorAll: () => [],
    addEventListener(type, fn) { if (type === 'keydown') this.keydown.push(fn); },
    nodes: new Map(),
  };
  doc.getElementById('turn-inspector').hidden = true;
  const requests = [];
  const location = { search, href: `http://127.0.0.1/chat${search}` };
  const historyApi = {
    state: null,
    replaceState(_state, _title, url) {
      const parsed = new URL(url, 'http://127.0.0.1');
      location.search = parsed.search; location.href = parsed.href;
    },
  };
  const json = body => ({ ok: true, json: async () => body });
  const context = {
    document: doc, location, history: historyApi, URL, URLSearchParams,
    fetch: async url => {
      requests.push(url);
      if (url === '/api/chat') return {
        ok: true, headers: { get: () => 'text/event-stream' },
        body: new ReadableStream({ start(controller) {
          for (const [name, data] of sse)
            controller.enqueue(new TextEncoder().encode(`event: ${name}\ndata: ${JSON.stringify(data)}\n\n`));
          controller.close();
        } }),
      };
      if (url.startsWith('/api/chat/history')) return json({ turns: history });
      if (url.startsWith('/api/turns?')) return json({ turns });
      const payload = /^\/api\/turns\/([^/]+)\/tools\/([^/]+)\/(input|output)\?offset=(\d+)/.exec(url);
      if (payload) {
        const page = payloads[`${payload[2]}/${payload[3]}/${payload[4]}`];
        return page ? json(page) : { ok: false, status: 404, json: async () => ({}) };
      }
      const match = /^\/api\/turns\/([^/]+)\/events/.exec(url);
      if (match) return json({ turn_id: match[1], events: events[match[1]] || [] });
      if (url.startsWith('/api/gateway/status')) return json(status);
      return json({});
    },
    AbortSignal, TextDecoder, setInterval() {},
  };
  vm.runInNewContext(shimSource() + scripts.map(read).join('\n'), context);
  const get = id => doc.getElementById(id);
  const ribbons = () => get('conversation').findAll(byClass('turn-ribbon'));
  const press = key => { const event = { key, preventDefault() {} }; for (const fn of doc.keydown) fn(event); };
  return { doc, get, ribbons, requests, location, press, context };
}


module.exports = { Element, byClass, tick, settle, SUMMARY, ev, setup };
