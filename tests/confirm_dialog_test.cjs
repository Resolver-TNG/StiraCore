// Shared confirmation dialog (#282): node --test tests/confirm_dialog_test.cjs
// Needs jsdom (npm install --no-save jsdom axe-core); skipped when it is missing.
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { resolve } = require('node:path');
const { shimSource } = require('./ui_shim.cjs');
const { polyfillDialog } = require('./dialog_polyfill.cjs');

let JSDOM = null;
try { ({ JSDOM } = require('jsdom')); } catch (err) { /* skipped below */ }
const skip = JSDOM ? false : 'jsdom not installed';
const JS = resolve(__dirname, '../dashboard/static/js');
const read = name => readFileSync(resolve(JS, name), 'utf8');
const tick = () => new Promise(r => setTimeout(r, 0));
async function settle() { for (let i = 0; i < 20; i += 1) await tick(); }

function page(body = '<button id="opener">open</button>', scripts = ['confirm_dialog.js']) {
  const dom = new JSDOM(`<!doctype html><html lang="ja"><body>${body}</body></html>`,
    { runScripts: 'outside-only', pretendToBeVisual: true });
  polyfillDialog(dom.window);
  dom.window.eval(shimSource());
  for (const name of scripts) dom.window.eval(read(name));
  return dom.window;
}
const esc = win => win.document.dispatchEvent(new win.KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));

test('lists what changes, focuses Cancel, Esc cancels and focus returns to the opener', { skip }, async () => {
  const win = page();
  const doc = win.document;
  doc.getElementById('opener').focus();
  const result = win.StiraConfirm.open({ title: 'T', intro: 'I', items: ['a', 'b'], confirmLabel: 'Go' });
  const dialog = doc.getElementById('stira-confirm');
  assert.equal(dialog.open, true);
  assert.deepEqual([...dialog.querySelectorAll('li')].map(li => li.textContent), ['a', 'b']);
  assert.equal(doc.activeElement, doc.getElementById('stira-confirm-cancel'));
  assert.equal(doc.getElementById('stira-confirm-cancel').textContent, 'キャンセル');
  assert.equal(doc.getElementById('stira-confirm-ok').textContent, 'Go');
  // Cancel comes first, so Enter (implicit submission) picks Cancel.
  const buttons = [...dialog.querySelectorAll('button[type="submit"]')];
  assert.equal(buttons[0].value, 'cancel');
  assert.equal(dialog.getAttribute('aria-labelledby'), 'stira-confirm-title');
  esc(win);
  assert.equal(await result, false);
  assert.equal(dialog.open, false);
  assert.equal(doc.activeElement, doc.getElementById('opener'));
});

test('confirm resolves true; a second open while showing resolves false', { skip }, async () => {
  const win = page();
  const first = win.StiraConfirm.open({ title: 'T', items: ['x'] });
  assert.equal(await win.StiraConfirm.open({ title: 'other' }), false);
  win.document.getElementById('stira-confirm-ok').click();
  assert.equal(await first, true);
  const again = win.StiraConfirm.open({ title: 'T' });
  win.document.getElementById('stira-confirm-cancel').click();
  assert.equal(await again, false);
});

test('typed word and tool names gate the confirm button and are re-checked on close', { skip }, async () => {
  const win = page();
  const doc = win.document;
  const result = win.StiraConfirm.open({
    title: 'T', items: ['loosen'], typed: { word: 'ALLOW', prompt: 'type ALLOW' },
    names: { expected: ['file_write'], prompt: 'names', hint: 'hint' },
  });
  const ok = doc.getElementById('stira-confirm-ok');
  const input = doc.getElementById('stira-confirm-input');
  const names = doc.getElementById('stira-confirm-names');
  assert.equal(doc.activeElement, input, 'the word field takes focus when a word is required');
  assert.equal(ok.disabled, true);
  input.value = 'ALLOW'; input.dispatchEvent(new win.Event('input'));
  assert.equal(ok.disabled, true, 'names still missing');
  names.value = 'shell'; names.dispatchEvent(new win.Event('input'));
  assert.equal(ok.disabled, true);
  names.value = 'file_write'; names.dispatchEvent(new win.Event('input'));
  assert.equal(ok.disabled, false);
  // Changing the field after enabling (without an input event) must not slip through.
  names.value = 'shell';
  win.document.getElementById('stira-confirm').close('confirm');
  assert.equal(await result, false);
});

test('checks (ADR 0012 phase 2c): every box gates the confirm button; focus order box -> Cancel -> confirm', { skip }, async () => {
  const win = page();
  const doc = win.document;
  doc.getElementById('opener').focus();
  const result = win.StiraConfirm.open({ title: 'T', items: ['x'], checks: ['I checked it', 'Outside is possible'] });
  const dialog = doc.getElementById('stira-confirm');
  const ok = doc.getElementById('stira-confirm-ok');
  const boxes = [...dialog.querySelectorAll('input[type="checkbox"]')];
  assert.equal(boxes.length, 2);
  assert.deepEqual([...dialog.querySelectorAll('.stira-confirm-check')].map(l => l.textContent),
    ['I checked it', 'Outside is possible']);
  assert.equal(boxes[0].labels[0].textContent, 'I checked it', 'each box has its label (clicking the text ticks it)');
  assert.equal(doc.activeElement, boxes[0], 'the first box takes focus');
  // DOM (= Tab) order: the boxes, then Cancel, then the confirm button.
  const order = [...dialog.querySelectorAll('input[type="checkbox"], button')];
  assert.deepEqual(order.map(n => n.id || n.type),
    ['stira-confirm-check-0', 'stira-confirm-check-1', 'stira-confirm-cancel', 'stira-confirm-ok']);
  assert.equal(ok.disabled, true);
  boxes[0].click();
  assert.equal(ok.disabled, true, 'the second box is still missing');
  // Enter on a box neither confirms nor cancels (no implicit submission).
  const enter = new win.KeyboardEvent('keydown', { key: 'Enter', bubbles: true, cancelable: true });
  boxes[1].dispatchEvent(enter);
  assert.equal(enter.defaultPrevented, true);
  assert.equal(dialog.open, true);
  boxes[1].click();
  assert.equal(ok.disabled, false);
  boxes[1].click();
  assert.equal(ok.disabled, true, 'unticking disables it again');
  boxes[1].click();
  ok.click();
  assert.equal(await result, true);
  assert.equal(doc.activeElement, doc.getElementById('opener'));
});

test('checks: axe finds no violation in the open dialog with two boxes', { skip }, async () => {
  let axeSource = null;
  try { axeSource = require('axe-core').source; } catch (err) { /* jsdom without axe */ }
  if (!axeSource) return;
  const win = page();
  const result = win.StiraConfirm.open({ title: 'T', intro: 'I', items: ['x'], checks: ['one', 'two'], confirmLabel: 'Go' });
  win.eval(axeSource);
  const report = await win.axe.run(win.document.getElementById('stira-confirm'), { rules: { 'color-contrast': { enabled: false } } });
  assert.equal(report.violations.map(v => v.id).join(','), '');
  assert.ok(report.passes.length > 0, 'axe ran its rules');
  esc(win);
  assert.equal(await result, false);
});

test('checks: Esc cancels at once; an untick without its change event still does not slip through', { skip }, async () => {
  const win = page();
  const doc = win.document;
  const first = win.StiraConfirm.open({ title: 'T', checks: ['I checked it'] });
  esc(win);
  assert.equal(await first, false);
  const second = win.StiraConfirm.open({ title: 'T', checks: ['I checked it'] });
  const box = doc.querySelector('#stira-confirm input[type="checkbox"]');
  assert.equal(box.checked, false, 'a new dialog starts unticked');
  box.click();
  box.checked = false; // no change event
  doc.getElementById('stira-confirm').close('confirm');
  assert.equal(await second, false);
  // Without checks the area is hidden and Cancel has the focus (as before).
  const plain = win.StiraConfirm.open({ title: 'T' });
  assert.equal(doc.querySelector('.stira-confirm-checks').hidden, true);
  assert.equal(doc.activeElement, doc.getElementById('stira-confirm-cancel'));
  esc(win);
  assert.equal(await plain, false);
});

test('schedules: resume asks in the shared dialog; Cancel and Esc send nothing, confirm posts confirm:true', { skip }, async () => {
  const posts = [];
  const body = `
    <span id="sched-message"></span><p id="sched-disabled"></p><dl id="sched-facts"></dl>
    <dd id="sched-state"></dd><dd id="sched-breaker"></dd><dd id="sched-last-tick"></dd><dd id="sched-alerts"></dd>
    <table><tbody id="sched-jobs"></tbody></table><table><tbody id="sched-runs"></tbody></table>
    <ul id="sched-notes"></ul><p id="sched-notes-empty"></p><p id="sched-live"></p>`;
  const dom = new JSDOM(`<!doctype html><html lang="ja"><body>${body}</body></html>`,
    { runScripts: 'outside-only', pretendToBeVisual: true });
  const win = dom.window;
  polyfillDialog(win);
  win.fetch = async (url, options = {}) => {
    if (options.method === 'POST') { posts.push([url, JSON.parse(options.body)]); return { ok: true, status: 200, json: async () => ({}) }; }
    return { ok: true, status: 200, json: async () => ({
      status: { enabled: true, state: 'running', breaker_level: 'normal', alerts: [] },
      jobs: [{ job_id: 'j1', name: 'digest', layer: 'gateway', action: 'memory_digest', state: 'paused',
        schedule: { type: 'interval', every_sec: 600 }, consecutive_failures: 0 }],
      runs: [], notifications: [] }) };
  };
  win.setInterval = () => 0;
  win.eval(shimSource());
  win.eval(read('confirm_dialog.js'));
  win.eval(read('schedules_page.js'));
  await settle();
  const resume = () => win.document.querySelector('#sched-jobs button');
  assert.equal(resume().textContent, '再開');
  resume().click();
  await settle();
  const dialog = win.document.getElementById('stira-confirm');
  assert.equal(dialog.open, true);
  assert.equal(win.document.getElementById('stira-confirm-title').textContent, 'digest を再開しますか');
  const items = [...dialog.querySelectorAll('li')].map(li => li.textContent);
  assert.deepEqual(items, ['名前: digest (j1)', 'スケジュール: 600 秒ごと', '種類: ゲートウェイ: memory_digest']);
  assert.equal(win.document.activeElement, win.document.getElementById('stira-confirm-cancel'));
  win.document.getElementById('stira-confirm-cancel').click();
  await settle();
  esc(win);
  resume().click();
  await settle();
  esc(win);
  await settle();
  assert.deepEqual(posts, [], 'cancel / Esc send nothing');
  resume().click();
  await settle();
  win.document.getElementById('stira-confirm-ok').click();
  await settle();
  assert.deepEqual(posts, [['/api/scheduler/jobs/j1/resume', { confirm: true }]]);
});

test('schedules: without the dialog script, resume sends nothing (fail-closed)', { skip }, async () => {
  const posts = [];
  const dom = new JSDOM('<!doctype html><body><span id="sched-message"></span><p id="sched-disabled"></p><dl id="sched-facts"></dl><dd id="sched-state"></dd><dd id="sched-breaker"></dd><dd id="sched-last-tick"></dd><dd id="sched-alerts"></dd><table><tbody id="sched-jobs"></tbody></table><table><tbody id="sched-runs"></tbody></table><ul id="sched-notes"></ul><p id="sched-notes-empty"></p><p id="sched-live"></p></body>',
    { runScripts: 'outside-only', pretendToBeVisual: true });
  const win = dom.window;
  win.fetch = async (url, options = {}) => {
    if (options.method === 'POST') posts.push(url);
    return { ok: true, status: 200, json: async () => ({ status: { enabled: true }, jobs: [{ job_id: 'j1', name: 'n', layer: 'agent', state: 'paused', schedule: { type: 'interval', every_sec: 60 } }], runs: [], notifications: [] }) };
  };
  win.setInterval = () => 0;
  win.eval(shimSource());
  win.eval(read('schedules_page.js'));
  await settle();
  win.document.querySelector('#sched-jobs button').click();
  await settle();
  assert.deepEqual(posts, []);
});

test('onConfirm runs in a busy window: no second dialog while it is pending (review of #314)', { skip }, async () => {
  const win = page();
  let release;
  const sent = [];
  const first = win.StiraConfirm.open({ title: 'T', onConfirm: () => new Promise((r) => { sent.push(1); release = r; }) });
  win.document.getElementById('stira-confirm-ok').click();
  await settle();
  assert.equal(sent.length, 1);
  assert.equal(win.StiraConfirm.isOpen(), true, 'busy while the request is pending');
  assert.equal(await win.StiraConfirm.open({ title: 'again', onConfirm: () => sent.push(2) }), false);
  release();
  assert.equal(await first, true);
  assert.equal(win.StiraConfirm.isOpen(), false);
  assert.deepEqual(sent, [1]);
  // A failing request still frees the dialog.
  const failing = win.StiraConfirm.open({ title: 'T', onConfirm: () => Promise.reject(new Error('x')) });
  win.document.getElementById('stira-confirm-ok').click();
  assert.equal(await failing, true);
  assert.equal(win.StiraConfirm.isOpen(), false);
  // Cancel never calls onConfirm.
  const cancelled = win.StiraConfirm.open({ title: 'T', onConfirm: () => sent.push(3) });
  win.document.getElementById('stira-confirm-cancel').click();
  assert.equal(await cancelled, false);
  assert.deepEqual(sent, [1]);
});

test('showModal() throwing confirms nothing and leaves the dialog usable', { skip }, async () => {
  const win = page();
  const proto = win.HTMLDialogElement.prototype;
  const real = proto.showModal;
  proto.showModal = () => { throw new win.DOMException('nope', 'InvalidStateError'); };
  let called = false;
  assert.equal(await win.StiraConfirm.open({ title: 'T', onConfirm: () => { called = true; } }), false);
  assert.equal(win.StiraConfirm.isOpen(), false);
  assert.equal(called, false);
  proto.showModal = real;
  const next = win.StiraConfirm.open({ title: 'T' });
  assert.equal(win.document.getElementById('stira-confirm').open, true);
  win.document.getElementById('stira-confirm-ok').click();
  assert.equal(await next, true);
});

test('schedules: double-clicking resume while the confirmed POST is pending sends one POST', { skip }, async () => {
  const posts = [];
  let release;
  const dom = new JSDOM('<!doctype html><body><span id="sched-message"></span><p id="sched-disabled"></p><dl id="sched-facts"></dl><dd id="sched-state"></dd><dd id="sched-breaker"></dd><dd id="sched-last-tick"></dd><dd id="sched-alerts"></dd><table><tbody id="sched-jobs"></tbody></table><table><tbody id="sched-runs"></tbody></table><ul id="sched-notes"></ul><p id="sched-notes-empty"></p><p id="sched-live"></p></body>',
    { runScripts: 'outside-only', pretendToBeVisual: true });
  const win = dom.window;
  polyfillDialog(win);
  win.fetch = async (url, options = {}) => {
    if (options.method === 'POST') {
      posts.push(url);
      await new Promise((r) => { release = r; });
      return { ok: true, status: 200, json: async () => ({}) };
    }
    return { ok: true, status: 200, json: async () => ({ status: { enabled: true }, jobs: [{ job_id: 'j1', name: 'n', layer: 'agent', state: 'paused', schedule: { type: 'interval', every_sec: 60 } }], runs: [], notifications: [] }) };
  };
  win.setInterval = () => 0;
  win.eval(shimSource());
  win.eval(read('confirm_dialog.js'));
  win.eval(read('schedules_page.js'));
  await settle();
  const resume = win.document.querySelector('#sched-jobs button');
  resume.click();
  await settle();
  win.document.getElementById('stira-confirm-ok').click();
  await settle();
  resume.click();   // the row has not refreshed yet: same button
  await settle();
  assert.equal(win.document.getElementById('stira-confirm').open, false, 'no second dialog while sending');
  assert.deepEqual(posts, ['/api/scheduler/jobs/j1/resume']);
  release();
  await settle();
});

// #650: Resume on /schedules sends its POST from the resume dialog's onConfirm; with the
// browser channel (the default) the POST is answered 428 and api.js opens the checkbox
// confirmation for that very operation. That follow-up must open (it used to resolve false
// inside the busy window: "Cancelled; nothing was applied"), and the busy window still
// keeps the page's own second Resume dialog / POST out.
const HOST_OP = 'cd'.repeat(16);
function resumePage(channel, { confirmState = 'confirmed' } = {}) {
  const calls = [];
  const dom = new JSDOM('<!doctype html><head><meta name="csrf-token" content="tok"></head><body><span id="sched-message"></span><p id="sched-disabled"></p><dl id="sched-facts"></dl><dd id="sched-state"></dd><dd id="sched-breaker"></dd><dd id="sched-last-tick"></dd><dd id="sched-alerts"></dd><table><tbody id="sched-jobs"></tbody></table><table><tbody id="sched-runs"></tbody></table><ul id="sched-notes"></ul><p id="sched-notes-empty"></p><p id="sched-live"></p></body>',
    { runScripts: 'outside-only', pretendToBeVisual: true });
  const win = dom.window;
  polyfillDialog(win);
  const reply = (status, data) => new win.Response(JSON.stringify(data), { status, headers: { 'Content-Type': 'application/json' } });
  const job = { job_id: 'j1', name: 'hourly health', layer: 'gateway', action: 'health_check', state: 'paused', schedule: { type: 'interval', every_sec: 3600 } };
  win.Response = Response;
  win.Headers = Headers;
  if (!win.crypto || typeof win.crypto.randomUUID !== 'function') {
    Object.defineProperty(win, 'crypto', { configurable: true, value: { randomUUID: (() => { let n = 0; return () => `key-${++n}`; })() } });
  }
  win.fetch = async (url, options = {}) => {
    const method = (options.method || 'GET').toUpperCase();
    const headers = new Headers(options.headers || {});
    calls.push({ url, method, header: headers.get('X-Stira-Host-Operation'), body: options.body });
    if (url === '/api/scheduler/jobs/j1/resume') {
      if (headers.get('X-Stira-Host-Operation') === HOST_OP) {
        job.state = 'active';
        return reply(200, { job });
      }
      return reply(428, { code: 'host_confirmation_required', operation: {
        id: HOST_OP, short: HOST_OP.slice(0, 8), kind: 'scheduler_loosen', code: '12345678', expires_in: 120,
        channel, instance: 'ef'.repeat(8), word: null,
        command: channel === 'command' ? `stira approvals decide ${HOST_OP.slice(0, 8)}` : null,
        ...(channel === 'browser' ? { summary: { action: 'resume', job: 'j1' }, confirm_token: 'aa'.repeat(16) } : {}),
      } });
    }
    if (url === `/api/host-operations/${HOST_OP}/confirm`) return reply(200, { operation: { state: 'confirmed' } });
    if (url === `/api/host-operations/${HOST_OP}/cancel`) return reply(200, { operation: { state: 'cancelled' } });
    if (url === `/api/host-operations/${HOST_OP}`) return reply(200, { operation: { state: confirmState, expires_in: 100 } });
    return reply(200, { status: { enabled: true }, jobs: [job], runs: [], notifications: [] });
  };
  win.setInterval = () => 0;
  win.setTimeout = (fn) => setImmediate(fn);   // the command channel's poll pause
  win.eval(shimSource());
  for (const name of ['api.js', 'confirm_dialog.js', 'schedules_page.js']) win.eval(read(name));
  const posts = () => calls.filter((c) => c.method === 'POST').map((c) => (c.header ? `${c.url} +op` : c.url));
  return { win, doc: win.document, calls, posts, job };
}

async function pressResume(p) {
  await settle();
  p.doc.querySelector('#sched-jobs button').click();
  await settle();
  assert.equal(p.doc.getElementById('stira-confirm-title').textContent.includes('hourly health'), true, 'the resume dialog');
  p.doc.getElementById('stira-confirm-ok').click();
  await settle();
}

test('#650 browser channel: Resume opens the checkbox confirmation; confirming it resumes the job', { skip }, async () => {
  const p = resumePage('browser');
  await pressResume(p);
  const dialog = p.doc.getElementById('stira-confirm');
  assert.equal(dialog.open, true, 'the host confirmation dialog opens after the resume dialog');
  const box = p.doc.getElementById('stira-confirm-check-0');
  assert.ok(box, 'one checkbox to tick');
  const ok = p.doc.getElementById('stira-confirm-ok');
  assert.equal(ok.disabled, true, 'nothing is confirmed until the box is ticked');
  assert.deepEqual(p.posts(), ['/api/scheduler/jobs/j1/resume']);
  // The page's own Resume stays blocked while the request (and its confirmation) is pending.
  assert.equal(await p.win.StiraConfirm.open({ title: 'again' }), false);
  box.checked = true;
  box.dispatchEvent(new p.win.Event('change', { bubbles: true }));
  assert.equal(ok.disabled, false);
  ok.click();
  await settle();
  assert.deepEqual(p.posts(), ['/api/scheduler/jobs/j1/resume', `/api/host-operations/${HOST_OP}/confirm`,
    '/api/scheduler/jobs/j1/resume +op']);
  assert.equal(p.job.state, 'active');
  assert.equal(p.doc.getElementById('sched-message').textContent, 'hourly health を再開しました');
  assert.equal(p.win.StiraConfirm.isOpen(), false, 'the dialog is free again');
});

test('#650 browser channel: cancelling the checkbox confirmation sends /cancel and applies nothing', { skip }, async () => {
  const p = resumePage('browser');
  await pressResume(p);
  assert.equal(p.doc.getElementById('stira-confirm').open, true);
  p.doc.getElementById('stira-confirm-cancel').click();
  await settle();
  assert.deepEqual(p.posts(), ['/api/scheduler/jobs/j1/resume', `/api/host-operations/${HOST_OP}/cancel`]);
  assert.equal(p.job.state, 'paused');
  assert.match(p.doc.getElementById('sched-message').textContent, /^拒否されました: /);
  assert.equal(p.win.StiraConfirm.isOpen(), false);
});

test('#650 command channel: unchanged (host notice, poll, re-send once; no second dialog)', { skip }, async () => {
  const p = resumePage('command');
  await pressResume(p);
  await settle();
  assert.equal(p.doc.getElementById('stira-confirm').open, false, 'no browser dialog on the command channel');
  assert.deepEqual(p.posts(), ['/api/scheduler/jobs/j1/resume', '/api/scheduler/jobs/j1/resume +op']);
  assert.ok(p.calls.some((c) => c.method === 'GET' && c.url === `/api/host-operations/${HOST_OP}`), 'polled');
  assert.equal(p.job.state, 'active');
});

test('#650 followUp: only the pending request\'s own question opens in the busy window; the window holds after it', { skip }, async () => {
  const win = page();
  const doc = win.document;
  let release;
  let inner = null;
  const first = win.StiraConfirm.open({ title: 'T', onConfirm: () => new Promise((r) => { release = r; }) });
  doc.getElementById('stira-confirm-ok').click();
  await settle();
  assert.equal(await win.StiraConfirm.open({ title: 'plain' }), false, 'no plain dialog while pending');
  inner = win.StiraConfirm.open({ title: 'host', followUp: true });
  assert.equal(doc.getElementById('stira-confirm').open, true);
  assert.equal(doc.getElementById('stira-confirm-title').textContent, 'host');
  assert.equal(await win.StiraConfirm.open({ title: 'other', followUp: true }), false, 'one dialog at a time');
  doc.getElementById('stira-confirm-cancel').click();
  assert.equal(await inner, false, 'Cancel stays Cancel');
  assert.equal(win.StiraConfirm.isOpen(), true, 'still busy: the first request has not settled');
  assert.equal(await win.StiraConfirm.open({ title: 'plain' }), false);
  release();
  assert.equal(await first, true);
  assert.equal(win.StiraConfirm.isOpen(), false);
});
