// #651: the real observability.js (Cost Tracker) and the real chat.js (Session panel) on
// the mock DOM of chat_dom_mock.cjs (no jsdom). Driven by tests/test_observability_cost_page.py.
// usage: node observability_cost_dom.cjs <case.json>
// case.json: {"feeds": {name: [page-contract events, in stream order]}, "status": gateway status}
// stdout: {"feeds": {name: {cost, notice, models: [[model, cost]], log_rows}}, "chat": text,
//          "notices": {priced, unpriced: the catalog texts with {unknown} left in}}
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const { shimSource, catalog } = require('./ui_shim.cjs');
const { Element, settle, setup } = require('./chat_dom_mock.cjs');

const SOURCE = fs.readFileSync(
  path.join(__dirname, '..', 'dashboard', 'static', 'js', 'observability.js'), 'utf8');

// What observability.js uses beyond the chat mock: a style object, fragments, childNodes.
class Node extends Element {
  constructor(doc, tag) { super(doc, tag); this.style = {}; }
  get childNodes() { return this.children; }
}

// One page load: the script runs, opens its EventSource, and gets `events` as messages.
function page(events) {
  const doc = {
    nodes: new Map(),
    getElementById(id) {
      if (!this.nodes.has(id)) { const node = new Node(this); node.id = id; this.nodes.set(id, node); }
      return this.nodes.get(id);
    },
    createElement: tag => new Node(doc, tag),
    createDocumentFragment: () => new Node(doc, '#fragment'),
  };
  // As the template ships them (observability.html).
  doc.getElementById('observability-pricing').textContent = '{"pricing":{}}';
  doc.getElementById('error-category').value = 'all';
  const opened = [];
  class FakeEventSource {
    constructor(url) { this.url = url; this.readyState = 0; opened.push(this); }
    addEventListener() {}
    close() { this.readyState = 2; }
  }
  FakeEventSource.CLOSED = 2;
  const timers = [];
  const context = {
    document: doc, EventSource: FakeEventSource,
    setInterval: fn => { timers.push(fn); return timers.length; },
    addEventListener() {},
  };
  context.window = context;
  vm.runInNewContext(shimSource() + SOURCE, context);
  if (opened.length !== 1 || opened[0].url !== '/api/observability/events') {
    throw new Error('the page did not open its event stream');
  }
  opened[0].onopen();
  for (const event of events) {
    opened[0].onmessage({ type: 'message', data: JSON.stringify(event), lastEventId: event.id });
  }
  timers[0](); // the render timer
  const rows = id => doc.getElementById(id).findAll(node => node.tagName === 'tr');
  return {
    cost: doc.getElementById('estimated-cost').textContent,
    notice: doc.getElementById('pricing-notice').textContent,
    models: rows('model-cost-body')
      .filter(row => row.children.length === 4)
      .map(row => [row.children[0].textContent, row.children[3].textContent]),
    log_rows: rows('log-body').length,
  };
}

async function main() {
  const input = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'));
  const feeds = {};
  for (const [name, events] of Object.entries(input.feeds)) feeds[name] = page(events);
  const chat = setup({ status: input.status });
  await settle();
  process.stdout.write(JSON.stringify({
    feeds,
    chat: chat.get('estimated-cost').textContent,
    notices: {
      priced: catalog['observability.calculated_in_usd_from_reported_costs_or'],
      unpriced: catalog['observability.subtotal_excludes_unpriced'],
    },
  }));
}

main().then(() => process.exit(0), err => {
  console.error(err && err.stack ? err.stack : err);
  process.exit(1);
});
