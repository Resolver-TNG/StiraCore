// /observability fed with the chat server's turns.db events (#286), via jsdom.
// usage: node observability_turns_dom.cjs <page.html> <events.json>
// events.json: the page-contract events (`turns_view.page_event`), in stream order.
// Exit codes: 0 = ok, 1 = failure, 77 = jsdom missing (skip).
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const { shimSource, catalog } = require('../ui_shim.cjs');

let JSDOM;
try {
  ({ JSDOM } = require('jsdom'));
} catch (err) {
  console.log(`SKIP: jsdom not available (${err.message})`);
  process.exit(77);
}

const JS = path.join(__dirname, '..', '..', 'dashboard', 'static', 'js');
const fill = (key, vars) => catalog[key].replace(/\{(\w+)\}/g, (m, n) => String(vars[n]));

function main() {
  const html = fs.readFileSync(process.argv[2], 'utf8');
  const events = JSON.parse(fs.readFileSync(process.argv[3], 'utf8'));
  const dom = new JSDOM(html, { runScripts: 'outside-only', pretendToBeVisual: true, url: 'http://127.0.0.1/observability' });
  const { window } = dom;
  const opened = [];
  class FakeEventSource {
    constructor(url) { this.url = url; this.readyState = 0; opened.push(this); }
    addEventListener() {}
    close() { this.readyState = 2; }
  }
  FakeEventSource.CLOSED = 2;
  window.EventSource = FakeEventSource;
  const timers = [];
  window.setInterval = fn => { timers.push(fn); return timers.length; };
  window.eval(shimSource());
  window.eval(fs.readFileSync(path.join(JS, 'observability.js'), 'utf8'));
  const doc = window.document;
  assert.equal(opened.length, 1);
  assert.equal(opened[0].url, '/api/observability/events');
  opened[0].onopen();
  for (const event of events) {
    opened[0].onmessage({ data: JSON.stringify(event), lastEventId: event.id });
  }
  timers[0]();

  const rows = [...doc.querySelectorAll('#log-body tr')];
  assert.equal(rows.length, events.length, 'one log row per event');
  assert.equal(doc.getElementById('log-count').textContent,
    fill('observability.log_count', { shown: events.length, total: events.length }));
  const logText = rows.map(r => r.textContent).join('\n');
  for (const key of ['op_turn_started', 'op_turn_end', 'op_approval_requested',
    'op_approval_resolved', 'op_breaker_transition']) {
    assert.ok(logText.includes(catalog[`observability.${key}`]), `log shows ${key}`);
  }
  assert.ok(logText.includes('file_write'), 'the tool name is shown');

  const turns = [...doc.querySelectorAll('#turn-history button')];
  const turnIds = [...new Set(events.map(e => e.turn_id))];
  assert.equal(turns.length, turnIds.length, 'one history button per turn');
  // A 32-digit turn id is shown by its first 8 digits, the full id in the title.
  const first = turnIds[0];
  assert.equal(turns[turns.length - 1].textContent,
    fill('observability.turn_n', { id: first.slice(0, 8) }));
  assert.equal(turns[turns.length - 1].title, first);
  assert.ok(rows[0].children[1].title === first && rows[0].children[1].textContent === first.slice(0, 8));
  // History lists the newest first; the oldest turn is the one with the tool call.
  turns[turns.length - 1].click();
  const lanes = [...doc.querySelectorAll('#timeline .timeline-bar.tool')];
  assert.equal(lanes.length, 1, 'the tool call is one lane of its turn');
  assert.equal(lanes[0].classList.contains('unfinished'), false, 'its end was received');

  const ends = events.filter(e => e.type === 'turn_end' && e.usage);
  const input = ends.reduce((n, e) => n + e.usage.input_tokens, 0);
  const output = ends.reduce((n, e) => n + e.usage.output_tokens, 0);
  const nf = new window.Intl.NumberFormat('ja-JP');
  assert.equal(doc.getElementById('session-tokens').textContent,
    fill('observability.tokens_in_out', { input: nf.format(input), output: nf.format(output) }));
  const modelRows = [...doc.querySelectorAll('#model-cost-body tr')];
  assert.equal(modelRows.length, 1);
  assert.ok(modelRows[0].textContent.includes('m-1'), 'the model of the turns');
  assert.ok(doc.getElementById('estimated-cost').textContent.endsWith('*'),
    'no rates configured: the cost is marked unpriced, not free');

  const errors = [...doc.querySelectorAll('#error-body tr')].map(r => r.textContent);
  assert.ok(errors.some(t => t.includes(catalog['observability.approval_request'])),
    'an approval is filed in the intervention log');
  assert.ok(errors.some(t => t.includes(catalog['observability.breaker_intervention'])),
    'a breaker transition is filed in the intervention log');
  // A refusal is "denied", never a breaker intervention (a rejected approval and a denied call).
  const denied = errors.filter(t => t.includes(catalog['observability.denied']));
  assert.equal(denied.length, 2, 'the rejection and the denied call are filed as denied');
  assert.ok(denied.every(t => !t.includes(catalog['observability.breaker_intervention'])),
    'a refusal is not shown as a breaker intervention');
  assert.equal(errors.filter(t => t.includes(catalog['observability.breaker_intervention'])).length, 1,
    'only the breaker transition is a breaker intervention');
  console.log(`OK: ${events.length} events, ${turns.length} turns, ${input}/${output} tokens`);
}

try { main(); process.exit(0); } catch (err) {
  console.log(`FAIL: ${err && err.stack ? err.stack : err}`);
  process.exit(1);
}
