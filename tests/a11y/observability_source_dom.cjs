// /observability says where its events come from (#278 / #286), via jsdom + axe.
// usage: node observability_source_dom.cjs <page.html> <absent|turns|live>
// Exit codes: 0 = ok, 1 = failure, 77 = deps missing (skip).
const fs = require('node:fs');
const path = require('node:path');
const assert = require('node:assert/strict');
const { shimSource, catalog } = require('../ui_shim.cjs');

let JSDOM;
let axeSource;
try {
  ({ JSDOM } = require('jsdom'));
  axeSource = require('axe-core').source;
} catch (err) {
  console.log(`SKIP: jsdom/axe-core not available (${err.message})`);
  process.exit(77);
}

const JS = path.join(__dirname, '..', '..', 'dashboard', 'static', 'js');

async function main() {
  const html = fs.readFileSync(process.argv[2], 'utf8');
  const expected = process.argv[3];
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
  window.setInterval = () => 0;
  window.eval(shimSource());
  window.eval(fs.readFileSync(path.join(JS, 'observability.js'), 'utf8'));
  const doc = window.document;
  const box = doc.getElementById('connection');
  assert.equal(box.dataset.eventSource, expected, 'the server-rendered source');
  const text = doc.getElementById('connection-text').textContent;
  const dot = doc.getElementById('connection-dot').className;
  const notice = doc.getElementById('event-source-notice');

  if (expected === 'absent') {
    // No stream on this server: never connect, never ask for a reload.
    assert.equal(opened.length, 0, 'no EventSource is opened');
    assert.equal(text, catalog['observability.no_stream']);
    assert.ok(!text.includes(catalog['observability.closed_reload_to_reconnect']));
    assert.match(dot, /\babsent\b/);
    assert.ok(notice && !notice.hidden, 'the notice is shown');
    assert.equal(notice.textContent.trim(), catalog['observability.source_absent_notice']);
  } else {
    assert.equal(opened.length, 1, 'the stream is opened');
    assert.equal(opened[0].url, '/api/observability/events');
    opened[0].onopen();
    assert.equal(doc.getElementById('connection-text').textContent, catalog['observability.connected']);
    if (expected === 'turns') {
      assert.ok(notice, 'the turns.db notice is shown');
      assert.equal(notice.textContent.trim(), catalog['observability.source_turns_notice']);
    } else {
      assert.equal(notice, null, 'no notice when the stream has a writer');
    }
  }

  window.eval(axeSource);
  const result = await window.axe.run(doc, { rules: { 'color-contrast': { enabled: false } } });
  for (const v of result.violations) {
    console.log(`  [${v.impact}] ${v.id}: ${v.help}`);
    for (const n of v.nodes) console.log(`    - ${n.html}`);
  }
  assert.equal(result.violations.length, 0, 'axe violations');
  console.log(`OK: ${expected}`);
}

main().then(() => process.exit(0), err => {
  console.log(`FAIL: ${err && err.stack ? err.stack : err}`);
  process.exit(1);
});
