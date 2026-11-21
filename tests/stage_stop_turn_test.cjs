// #680: the Stage's "■ Stop the turn" — it calls the chat page's own stop route
// (POST /api/chat/cancel, no new route) and is shown only when there is a turn it can stop.
// node --test tests/stage_stop_turn_test.cjs
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { pathToFileURL } = require('node:url');

const ROOT = path.join(__dirname, '..');
const read = (rel) => fs.readFileSync(path.join(ROOT, rel), 'utf8');
const loadTalk = () => import(pathToFileURL(path.join(ROOT, 'dashboard', 'stage', 'js', 'talk.js')).href);
const core = require('../dashboard/static/js/chat_core.js');

function fakeFetch(status = 202, body = { status: 'cancelling' }) {
  const calls = [];
  const fetch = async (url, init = {}) => {
    calls.push({ url, method: init.method, body: init.body ? JSON.parse(init.body) : null });
    return { ok: status < 300, status, json: async () => body };
  };
  return { fetch, calls };
}

test('no turn: no button target', async () => {
  const { stopTurnTarget } = await loadTalk();
  assert.equal(stopTurnTarget({ busy: false, ownTurnId: null, live: null }), null);
  assert.equal(stopTurnTarget({ busy: false, ownTurnId: null, live: { turnOpen: false, turnStoppable: true, turnId: 't1' } }), null);
  // a turn the server does not list as stoppable (e.g. resumed after an approval)
  assert.equal(stopTurnTarget({ busy: false, ownTurnId: null, live: { turnOpen: true, turnStoppable: false, turnId: 't1' } }), null);
  // this page's own turn before its id is known
  assert.equal(stopTurnTarget({ busy: true, ownTurnId: null, live: null }), null);
});

test('a running turn: the target is its id (own turn first, else the stoppable live turn)', async () => {
  const { stopTurnTarget } = await loadTalk();
  assert.deepEqual(stopTurnTarget({ busy: true, ownTurnId: 'own', live: { turnOpen: true, turnStoppable: true, turnId: 'other' } }), { turnId: 'own' });
  assert.deepEqual(stopTurnTarget({ busy: false, ownTurnId: null, live: { turnOpen: true, turnStoppable: true, turnId: 't9' } }), { turnId: 't9' });
});

test('one press calls POST /api/chat/cancel with the turn id', async () => {
  const { requestStopTurn } = await loadTalk();
  const { fetch, calls } = fakeFetch();
  const result = await requestStopTurn(core, fetch, { turnId: 't9' }, null);
  assert.equal(result.ok, true);
  assert.deepEqual(calls, [{ url: '/api/chat/cancel', method: 'POST', body: { turn_id: 't9' } }]);
});

test('no target: nothing is sent', async () => {
  const { requestStopTurn } = await loadTalk();
  const { fetch, calls } = fakeFetch();
  const result = await requestStopTurn(core, fetch, null, null);
  assert.equal(result.ok, false);
  assert.equal(calls.length, 0);
});

test('a refused stop is reported, not hidden', async () => {
  const { requestStopTurn } = await loadTalk();
  const { fetch } = fakeFetch(409, { code: 'no_turn', error: 'no turn is running' });
  const result = await requestStopTurn(core, fetch, { turnId: 't9' }, null);
  assert.equal(result.ok, false);
  assert.equal(result.code, 'no_turn');
});

test('the button: hidden by default, labelled in both languages, wired to the stop', () => {
  const html = read('dashboard/templates/stage.html');
  const button = html.match(/<button id="stage-stop-turn"[^>]*>/);
  assert.ok(button, 'the Stage has the stop-the-turn button');
  assert.match(button[0], /\bhidden\b/);
  assert.match(button[0], /type="button"/);
  assert.match(button[0], /aria-label="[^"]+"/);
  assert.match(button[0], /data-i18n-aria-label="stage\.stop_turn_label"/);
  for (const lang of ['ja', 'en']) {
    const catalog = JSON.parse(read(`dashboard/static/i18n/catalog.${lang}.json`));
    assert.ok(catalog['stage.stop_turn'] && catalog['stage.stop_turn_label'], lang);
  }
  const talk = read('dashboard/stage/js/talk.js');
  assert.match(talk, /el\.stopTurn\.hidden = !target/);
  assert.match(talk, /requestStopTurn\(core, ctx\.fetch, target, t\)/);
  // no new route: only the existing stop
  assert.doesNotMatch(talk, /\/api\/chat\/stop/);
});

// --- jsdom: the controller (startTalk) with the Stage's own markup -------------------------
let JSDOM = null;
try { ({ JSDOM } = require('jsdom')); } catch (err) { /* skipped below */ }
const skip = JSDOM ? false : 'jsdom not installed';

async function bootStage({ live, reply }) {
  const html = read('dashboard/templates/stage.html');
  const dom = new JSDOM(`<!doctype html><html><body>${html}</body></html>`);
  globalThis.window = dom.window;
  globalThis.document = dom.window.document;
  const calls = [];
  const pending = [];
  const fetch = (url, init = {}) => {
    if (url === '/api/chat/cancel') {
      calls.push({ url, method: init.method, body: init.body ? JSON.parse(init.body) : null });
      return new Promise((done) => pending.push(done)).then(() => reply());
    }
    return Promise.resolve({ ok: true, status: 200, json: async () => ({ turns: [], session: null }) });
  };
  const { startTalk } = await loadTalk();
  const ui = { say() {}, stream() {}, finishLine() {}, typing: false };
  const ctrl = startTalk({
    ui, core, fetch, live: { status: () => live.now },
    strings: () => ({ t: (key) => key, title: 't' }), stage: document.body,
    reducedMotion: true, confirm: null,
  });
  const flush = () => new Promise((done) => setTimeout(done, 0));
  await flush();
  ctrl.tick();
  return { button: document.getElementById('stage-stop-turn'), note: document.getElementById('stage-note'), ctrl, calls, pending, flush };
}

const running = { offline: false, approval: false, turnOpen: true, turnStoppable: true, turnId: 't9' };

test('jsdom: no stoppable turn -> the button is hidden; a running turn -> one press, one request', { skip }, async () => {
  const live = { now: { offline: false, approval: false, turnOpen: false, turnStoppable: false, turnId: null } };
  const accepted = () => ({ ok: true, status: 202, json: async () => ({ status: 'cancelling' }) });
  const s = await bootStage({ live, reply: accepted });
  assert.equal(s.button.hidden, true);
  live.now = running;
  s.ctrl.tick();
  assert.equal(s.button.hidden, false);
  assert.equal(s.button.disabled, false);
  s.button.click();
  s.button.click(); // a second press while the first is on its way sends nothing
  assert.equal(s.button.disabled, true);
  assert.deepEqual(s.calls, [{ url: '/api/chat/cancel', method: 'POST', body: { turn_id: 't9' } }]);
  s.pending.forEach((done) => done());
  await s.flush();
  assert.equal(s.calls.length, 1);
  assert.equal(s.button.disabled, true); // still stopping until the turn has ended
  live.now = { ...running, turnOpen: false, turnStoppable: false };
  s.ctrl.tick();
  assert.equal(s.button.hidden, true);
});

test('jsdom: "no_turn" says nothing; a failed stop gives the button back with its error', { skip }, async () => {
  const live = { now: running };
  let answer = { ok: false, status: 409, json: async () => ({ code: 'no_turn', error: 'no turn is running' }) };
  const s = await bootStage({ live, reply: () => answer });
  s.button.click();
  s.pending.splice(0).forEach((done) => done());
  await s.flush();
  assert.doesNotMatch(s.note.textContent, /no turn is running/);
  live.now = { ...running, turnId: 't10' }; // another stoppable turn: the button is offered again
  s.ctrl.tick();
  assert.equal(s.button.disabled, false);
  answer = { ok: false, status: 500, json: async () => ({ error: 'boom' }) };
  s.button.click();
  s.pending.splice(0).forEach((done) => done());
  await s.flush();
  assert.equal(s.button.disabled, false);
  assert.match(s.note.textContent, /boom/);
  assert.deepEqual(s.calls.map((c) => c.body.turn_id), ['t9', 't10']);
});
