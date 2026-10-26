// Progress report panel (ADR 0014 phase 1, #451): node --test tests/progress_ui_test.cjs
// The pure parts run everywhere; the DOM parts need jsdom (npm install --no-save jsdom axe-core).
const { test, after } = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { resolve } = require('node:path');

let JSDOM = null;
try { ({ JSDOM } = require('jsdom')); } catch (err) { /* DOM tests skipped below */ }
const skip = JSDOM ? false : 'jsdom not installed';
const ROOT = resolve(__dirname, '..');
const SRC = readFileSync(resolve(ROOT, 'dashboard/static/js/progress_ui.js'), 'utf8');
const CAT = {
  ja: JSON.parse(readFileSync(resolve(ROOT, 'dashboard/static/i18n/catalog.ja.json'), 'utf8')),
  en: JSON.parse(readFileSync(resolve(ROOT, 'dashboard/static/i18n/catalog.en.json'), 'utf8')),
};
const P = require('../dashboard/static/js/progress_ui.js');
const tick = () => new Promise((r) => setTimeout(r, 0));
async function settle() { for (let i = 0; i < 40; i += 1) await tick(); }

const RID = 'ab'.repeat(16);
const report = (over = {}) => ({
  report_id: RID, turn_id: 'cd'.repeat(16), trigger: 'time', end: 'soft', ts: 1792416000,
  elapsed_sec: 1860, tools: { file_write: [2, 1], current_time: [1, 0] },
  usage: { input: 1200, output: 340 }, yolo: { tier: 'internal', remaining_sec: 17940, used: 2, limit: 2000 },
  note: 'Done: a.md\nNext: b.md', pending: true, answer: null,
  auto: { due_wall: 2000 + 600, n: 1, max: 3 }, ...over,
});
const body = (over = {}, rep = report()) => ({
  report: rep, window_open: true, reports_on: true, consecutive_auto: 0,
  settings: { enabled: false, interval_min: 30, continue_after_min: 10, max_auto_continues: 3 }, ...over,
});

// --- pure ------------------------------------------------------------------------------------

test('the facts are numbers and tool names only', () => {
  global.t = (key, vars) => {
    let text = CAT.en[key] ?? `!${key}`;
    if (vars) text = text.replace(/\{(\w+)\}/g, (m, n) => (n in vars ? String(vars[n]) : m));
    return text;
  };
  const rows = P.factRows(report());
  assert.deepEqual(rows, [
    ['Elapsed', '31 min'],
    ['Tool calls', '3, 1 failed (file_write, current_time)'],
    ['Tokens', 'in 1200 / out 340'],
    ['YOLO', '4 h 59 min left · auto-approved 2/2000'],
  ]);
  delete global.t;
});

test('what happens without an answer: the countdown, the row limit, or waiting', () => {
  assert.equal(P.autoState(body()).key, 'progress.auto_in');
  assert.equal(P.autoState(body({ consecutive_auto: 3 }, report({ auto: null }))).key, 'progress.auto_limit');
  assert.equal(P.autoState(body({}, report({ auto: null }))).key, 'progress.waiting');
  assert.equal(P.autoState(body({}, report({ pending: false }))), null);
  assert.equal(P.clockText(599.2), '10:00');
  assert.equal(P.clockText(61), '1:01');
});

// --- DOM ---------------------------------------------------------------------------------------

const windows = [];
after(() => { for (const w of windows) w.close(); });

function page(lang = 'ja', state = body()) {
  const dom = new JSDOM(`<!doctype html><html lang="${lang}"><head><title>Chat</title></head><body>
    <section id="progress-slot" class="progress-panel" aria-labelledby="progress-title" hidden></section></body></html>`,
  { runScripts: 'outside-only', url: 'http://localhost/chat' });
  const w = dom.window;
  windows.push(w);
  w.t = (key, vars) => {
    let text = Object.hasOwn(CAT[lang], key) ? CAT[lang][key] : `!${key}`;
    if (vars) text = text.replace(/\{(\w+)\}/g, (m, n) => (n in vars ? String(vars[n]) : m));
    return text;
  };
  const calls = [];
  w.STIRA_PROGRESS_NO_AUTOSTART = true;
  w.eval(SRC);
  const current = { state };
  const instance = w.StiraProgress.create({
    poll: false,
    now: () => 2000,
    fetch: async () => ({ ok: true, json: async () => JSON.parse(JSON.stringify(current.state)) }),
    post: async (url, payload) => { calls.push({ url, payload }); return { ok: true, json: async () => ({ yolo_stopped: true }), text: async () => '' }; },
  });
  return { w, doc: w.document, instance, calls, current };
}

test('a pending report draws the note, the facts, the countdown and three answers', { skip }, async () => {
  const { doc, instance } = page('ja');
  await instance.refresh();
  const slot = doc.getElementById('progress-slot');
  assert.equal(slot.hidden, false);
  assert.equal(doc.getElementById('progress-title').textContent, '中間報告');
  assert.equal(slot.querySelector('.progress-note').textContent, 'Done: a.md\nNext: b.md');
  assert.match(slot.querySelector('.progress-auto').textContent, /10:00 後に自動で続けます（連続 1\/3 回目）/);
  assert.deepEqual([...slot.querySelectorAll('.progress-actions button')].map((b) => b.textContent),
    ['続ける', '方向を直す', 'ここで止める']);
  assert.equal(doc.title, '● Chat');
  // The note is text, never markup.
  instance.state.body.report.note = '<img src=x onerror=alert(1)>';
  instance.render(true);
  assert.equal(slot.querySelector('img'), null);
});

test('Continue and Redirect go through the page sender with progress_reply', { skip }, async () => {
  const { w, doc, instance } = page('en');
  const sent = [];
  w.StiraProgressSend = async (message, reply) => { sent.push({ message, reply }); return true; };
  await instance.refresh();
  doc.getElementById('progress-continue').click();
  await settle();
  assert.deepEqual(JSON.parse(JSON.stringify(sent[0])), { message: 'Please continue.', reply: { report_id: RID, answer: 'continue' } });
  doc.getElementById('progress-redirect').click();
  const input = doc.getElementById('progress-redirect-input');
  assert.equal(doc.activeElement, input);
  assert.equal(input.maxLength, 300);
  input.value = 'finish b.md first';
  input.form.dispatchEvent(new w.Event('submit', { cancelable: true }));
  await settle();
  assert.deepEqual(JSON.parse(JSON.stringify(sent[1])), { message: 'Change of direction: finish b.md first', reply: { report_id: RID, answer: 'redirect' } });
});

test('Escape closes the redirect field and gives the focus back; an empty line is not sent', { skip }, async () => {
  const { w, doc, instance } = page('en');
  const sent = [];
  w.StiraProgressSend = async (message, reply) => { sent.push({ message, reply }); return true; };
  await instance.refresh();
  doc.getElementById('progress-redirect').click();
  const input = doc.getElementById('progress-redirect-input');
  input.form.dispatchEvent(new w.Event('submit', { cancelable: true }));
  await settle();
  assert.equal(sent.length, 0);
  input.dispatchEvent(new w.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
  assert.equal(doc.getElementById('progress-redirect-input'), null);
  assert.equal(doc.activeElement, doc.getElementById('progress-redirect'));
});

test('Stop and the hold post to the report and say when the window stop failed', { skip }, async () => {
  const { doc, instance, calls, current } = page('ja');
  await instance.refresh();
  doc.getElementById('progress-hold').click();
  await settle();
  assert.equal(calls[0].url, `/api/progress/${RID}/hold`);
  doc.getElementById('progress-stop').click();
  await settle();
  assert.equal(calls[1].url, `/api/progress/${RID}/stop`);
  current.state = body({}, report({ pending: false, answer: { answer: 'stop' } }));
  await instance.refresh();
  assert.equal(doc.getElementById('progress-slot').hidden, true);
  assert.equal(doc.title, 'Chat');
});

test('a hard end says there is no note; the row limit is said in words', { skip }, async () => {
  const { doc, instance } = page('en', body({ consecutive_auto: 3 }, report({ end: 'hard', note: null, auto: null })));
  await instance.refresh();
  const slot = doc.getElementById('progress-slot');
  assert.equal(slot.querySelector('.progress-note').textContent, 'Work stopped at the checkpoint (no note from the agent).');
  assert.equal(slot.querySelector('.progress-auto').textContent, 'It continues by itself at most 3 times in a row. Waiting for your answer.');
  assert.equal(doc.getElementById('progress-hold'), null);
});

test('a report id that is not 32 hex characters is never drawn', { skip }, async () => {
  const { doc, instance } = page('en', body({}, report({ report_id: '../x' })));
  await instance.refresh();
  assert.equal(doc.getElementById('progress-slot').hidden, true);
});

test('every string is in both catalogs', () => {
  const keys = [...new Set([...SRC.matchAll(/"(progress\.[a-z_]+)"/g)].map((m) => m[1]))];
  assert.ok(keys.length > 20);
  for (const key of keys) {
    assert.ok(Object.hasOwn(CAT.ja, key), `ja ${key}`);
    assert.ok(Object.hasOwn(CAT.en, key), `en ${key}`);
  }
});

test('the countdown ticks without moving the keyboard focus off its button', { skip }, async () => {
  const { w, doc, instance } = page('en');
  let clock = 2000;
  instance.state.body = null;
  const inst = w.StiraProgress.create({
    poll: false,
    now: () => clock,
    fetch: async () => ({ ok: true, json: async () => body() }),
    post: async () => ({ ok: true, json: async () => ({}) }),
  });
  await inst.refresh();
  const hold = doc.getElementById('progress-hold');
  hold.focus();
  clock += 5;
  inst.render(false);
  assert.equal(doc.activeElement, hold);
  assert.match(doc.querySelector('.progress-auto-text').textContent, /9:55/);
});

test('#535: the report links to the artifacts of its turn, only when it wrote some', { skip }, async () => {
  const TID = 'cd'.repeat(16);
  for (const [lang, text] of [['ja', '成果物 2 件（このターン）'], ['en', '2 artifacts (this turn)']]) {
    const { doc, instance } = page(lang, body({}, report({ artifacts: 2 })));
    await instance.refresh();
    const a = doc.querySelector('#progress-slot .progress-artifacts a');
    assert.equal(a.getAttribute('href'), `/artifacts?turn=${TID}`);
    assert.equal(a.textContent, text);
    assert.equal(a.getAttribute('target'), null, 'same tab, same origin');
  }
  for (const over of [{}, { artifacts: 0 }, { artifacts: 2, turn_id: 'http://x/y' }, { artifacts: '2' }]) {
    const { doc, instance } = page('en', body({}, report(over)));
    await instance.refresh();
    assert.equal(doc.querySelector('#progress-slot .progress-artifacts'), null, JSON.stringify(over));
  }
});
