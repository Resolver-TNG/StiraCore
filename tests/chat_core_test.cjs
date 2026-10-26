// #350: chat_core.js — the send / stream / stop / re-send / image / IME logic that the chat
// page (chat.js) and the Stage (dashboard/stage/js/talk.js) share.
// node --test tests/chat_core_test.cjs
const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');

const ROOT = path.join(__dirname, '..');
const core = require('../dashboard/static/js/chat_core.js');
const t = (key, vars) => (vars ? `${key}(${JSON.stringify(vars)})` : key);

function sse(events, { chunk = 0 } = {}) {
  const text = events.map(([event, data]) => `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`).join('');
  const bytes = new TextEncoder().encode(text);
  const body = new ReadableStream({
    start(c) {
      if (!chunk) c.enqueue(bytes);
      else for (let i = 0; i < bytes.length; i += chunk) c.enqueue(bytes.slice(i, i + chunk));
      c.close();
    },
  });
  return { ok: true, status: 200, headers: { get: () => 'text/event-stream' }, body };
}
const refusal = (status, body = {}) => ({ ok: false, status, json: async () => body });

function fakeFetch(outcomes) {
  const calls = [];
  const fetch = async (url, init) => {
    calls.push({ url, init });
    const next = outcomes.shift();
    if (typeof next === 'function') return next(init);
    if (next instanceof Error) throw next;
    return next;
  };
  return { fetch, calls };
}

let n = 0;
const makeKey = () => `key-${(n += 1)}`;

test('IME: Enter while composing never sends; Enter sends; Shift+Enter is a new line', () => {
  const k = (o) => ({ key: 'Enter', shiftKey: false, isComposing: false, keyCode: 13, ...o });
  assert.equal(core.isSendKey(k({}), false), true);
  assert.equal(core.isSendKey(k({ shiftKey: true }), false), false);
  assert.equal(core.isSendKey(k({ isComposing: true }), false), false); // KeyboardEvent.isComposing
  assert.equal(core.isSendKey(k({ keyCode: 229 }), false), false); // Safari/Chrome confirm Enter
  assert.equal(core.isSendKey(k({}), true), false); // between compositionstart and compositionend
  assert.equal(core.isSendKey({ key: 'a', keyCode: 65 }, false), false);
});

test('image rules: 5 images / 10 MiB together, PNG/JPEG/GIF/WebP, not empty', () => {
  const img = (type = 'image/png', size = 10) => ({ type, size, name: 'x' });
  assert.equal(core.imageError([], [img()]), null);
  assert.equal(core.imageError([img(), img(), img()], [img(), img(), img()]), 'chat.up_to_5_images_10_mib_total');
  assert.equal(core.imageError([], [img('image/png', 10 * 1024 * 1024 + 1)]), 'chat.up_to_5_images_10_mib_total');
  assert.equal(core.imageError([], [img('image/svg+xml')]), 'chat.supported_images_only');
  assert.equal(core.imageError([], [img('image/png', 0)]), 'chat.supported_images_only');
});

test('readImages builds the API image block and a data: URL preview', async () => {
  class FileReader {
    readAsDataURL(file) {
      this.result = `data:${file.type};base64,QUJD`;
      this.onload();
    }
  }
  const [a] = await core.readImages([{ name: 'a.png', size: 3, type: 'image/png' }], { FileReader, t });
  assert.deepEqual(a.block, { type: 'image', source: { type: 'base64', media_type: 'image/png', data: 'QUJD' } });
  assert.equal(a.url, 'data:image/png;base64,QUJD');
  assert.equal(core.payloadOf('hi', [a]), JSON.stringify({ message: 'hi', images: [a.block] }));
  assert.equal(core.payloadOf('hi', []), JSON.stringify({ message: 'hi' }));
});

test('readSSE splits events across chunk boundaries and CRLF', async () => {
  const seen = [];
  await core.readSSE(sse([['delta', { text: 'こん' }], ['delta', { text: 'にちは' }], ['turn', { turn_id: 't1' }]], { chunk: 5 }), (e, d) => seen.push([e, d]), t);
  assert.deepEqual(seen, [['delta', { text: 'こん' }], ['delta', { text: 'にちは' }], ['turn', { turn_id: 't1' }]]);
  await assert.rejects(core.readSSE({ ok: true, headers: { get: () => 'application/json' }, body: {} }, () => {}, t), /chat.missing_event_stream/);
});

test('send: POST /api/chat with JSON, SSE accept and an Idempotency-Key; success settles', async () => {
  const { fetch, calls } = fakeFetch([sse([['ui_event', { turn_id: 't1', seq: 1 }], ['delta', { text: 'ok' }], ['turn', { turn_id: 't1' }]])]);
  const s = core.createSender({ fetch, t, makeKey });
  const got = [];
  const r = await s.send({ message: 'hi' }, { delta: (d) => got.push(['delta', d.text]), uiEvent: (d) => got.push(['ui', d.seq]), turn: (d) => got.push(['turn', d.turn_id]) });
  assert.equal(r.ok, true);
  assert.deepEqual(got, [['ui', 1], ['delta', 'ok'], ['turn', 't1']]);
  assert.equal(calls[0].url, '/api/chat');
  assert.equal(calls[0].init.method, 'POST');
  assert.equal(calls[0].init.headers['Content-Type'], 'application/json');
  assert.equal(calls[0].init.headers.Accept, 'text/event-stream');
  assert.match(calls[0].init.headers['Idempotency-Key'], /^key-/);
  assert.equal(calls[0].init.body, JSON.stringify({ message: 'hi' }));
  assert.equal(s.unsettled, null);
});

test('unknown outcome keeps the key: same payload reuses it, another payload is blocked', async () => {
  const { fetch, calls } = fakeFetch([new Error('offline'), sse([['delta', { text: 'cut' }]]), sse([['turn', {}]])]);
  const s = core.createSender({ fetch, t, makeKey });
  const r1 = await s.send({ message: 'hello' });
  assert.equal(r1.ok, false);
  assert.equal(r1.delivery, 'unknown');
  assert.ok(s.unsettled);
  assert.equal(s.blocked(core.payloadOf('other', [])), true);
  assert.equal(s.blocked(core.payloadOf('hello', [])), false);
  const r2 = await s.send({ message: 'hello' }); // stream cut before the turn: still unknown
  assert.equal(r2.error.message, 'chat.response_interrupted');
  assert.equal(calls[1].init.headers['Idempotency-Key'], calls[0].init.headers['Idempotency-Key']);
  const r3 = await s.send({ message: 'hello' });
  assert.equal(r3.ok, true);
  assert.equal(calls[2].init.headers['Idempotency-Key'], calls[0].init.headers['Idempotency-Key']);
  assert.equal(s.unsettled, null);
});

test('a turn cut off at the output limit says why and settles the send (#363)', async () => {
  const { fetch } = fakeFetch([sse([['error', { error: 'server text', code: 'max_tokens' }]])]);
  const s = core.createSender({ fetch, t, makeKey });
  const r = await s.send({ message: 'write a long game' });
  assert.equal(r.ok, false);
  assert.equal(r.error.message, 'chat.max_tokens_cut_off');
  assert.equal(r.error.code, 'max_tokens');
  assert.equal(s.unsettled, null, 'the turn ran and ended: nothing is left to re-send or discard');
  // any other error event keeps its own text and its key
  const other = fakeFetch([sse([['error', { error: 'boom' }]])]);
  const s2 = core.createSender({ fetch: other.fetch, t, makeKey });
  const r2 = await s2.send({ message: 'x' });
  assert.equal(r2.error.message, 'boom');
  assert.ok(s2.unsettled);
});

test('a cut before the reply began names the limit; without a limit the generic reason (#416)', async () => {
  const calls = [];
  const tv = (key, vars) => { calls.push([key, vars]); return key; };
  const { fetch } = fakeFetch([sse([['error', { error: 'server text', code: 'max_tokens', cut: 'before_reply', limit: 64000 }]])]);
  const s = core.createSender({ fetch, t: tv, makeKey });
  const r = await s.send({ message: 'write a long game' });
  assert.equal(r.error.message, 'chat.max_tokens_before_reply');
  assert.equal(r.error.code, 'max_tokens');
  assert.equal(r.error.cut, 'before_reply');
  assert.deepEqual(calls.find(([key]) => key === 'chat.max_tokens_before_reply')[1], { limit: (64000).toLocaleString() });
  assert.equal(s.unsettled, null);
  for (const data of [{ code: 'max_tokens', cut: 'before_reply' }, { code: 'max_tokens', limit: 8192 }]) {
    const other = fakeFetch([sse([['error', { error: 'x', ...data }]])]);
    const r2 = await core.createSender({ fetch: other.fetch, t, makeKey }).send({ message: 'x' });
    assert.equal(r2.error.message, 'chat.max_tokens_cut_off');
    assert.equal(r2.error.cut, undefined);
  }
});

test('no-effect refusals and duplicates settle; 5xx and SSE errors keep the key', async () => {
  for (const [res, delivery, kept] of [
    [refusal(400, { error: 'bad' }), 'refused', false],
    [refusal(403), 'refused', false],
    [refusal(409, { code: 'idempotency_key_reused' }), 'refused', false],
    [refusal(409, { code: 'duplicate_request' }), 'duplicate', false],
    [refusal(503), 'unknown', true],
    [sse([['error', { error: '処理中です / Agent is busy' }]]), 'unknown', true],
  ]) {
    const { fetch } = fakeFetch([res]);
    const s = core.createSender({ fetch, t, makeKey });
    const r = await s.send({ message: 'x' });
    assert.equal(r.ok, false);
    assert.equal(r.delivery, delivery, JSON.stringify(res.status));
    assert.equal(Boolean(s.unsettled), kept, `${res.status} keeps the key: ${kept}`);
  }
  const { fetch } = fakeFetch([refusal(409, { code: 'duplicate_request' })]);
  const r = await core.createSender({ fetch, t, makeKey }).send({ message: 'x' });
  assert.equal(r.error.message, 'chat.already_delivered');
});

test('stop() aborts the stream: stopped, key kept until the caller discards it', async () => {
  let release;
  const { fetch, calls } = fakeFetch([
    (init) =>
      new Promise((resolve, reject) => {
        init.signal.addEventListener('abort', () => reject(Object.assign(new Error('aborted'), { name: 'AbortError' })));
        release = resolve;
      }),
  ]);
  const s = core.createSender({ fetch, t, makeKey });
  assert.equal(s.stop(), false); // nothing to stop
  const pending = s.send({ message: 'slow' });
  await new Promise(setImmediate);
  assert.equal(s.busy, true);
  assert.ok(calls[0].init.signal, 'the request carries an AbortSignal');
  assert.equal(s.stop(), true);
  const r = await pending;
  assert.equal(r.ok, false);
  assert.equal(r.stopped, true);
  assert.equal(r.completed, false);
  assert.equal(s.busy, false);
  assert.ok(s.unsettled, 'stop alone does not drop the key');
  s.discard();
  assert.equal(s.unsettled, null);
  assert.equal(typeof release, 'function');
});

test('after stop the outcome is unknown: the same text re-sent keeps the key, the server deduplicates', async () => {
  const hanging = (init) =>
    new Promise((_resolve, reject) => {
      init.signal.addEventListener('abort', () => reject(Object.assign(new Error('aborted'), { name: 'AbortError' })));
    });
  const { fetch, calls } = fakeFetch([hanging, refusal(409, { code: 'duplicate_request' })]);
  const s = core.createSender({ fetch, t, makeKey });
  const first = s.send({ message: 'slow' });
  await new Promise(setImmediate);
  s.stop();
  const r1 = await first;
  assert.equal(r1.stopped, true);
  assert.equal(r1.delivery, 'unknown');
  assert.ok(s.unsettled, 'Stop is not a confirmed cancel: the key stays');
  assert.equal(s.blocked(core.payloadOf('slow', [])), false); // the same text may go again
  const r2 = await s.send({ message: 'slow' }); // e.g. the text put back into the composer
  assert.equal(calls[1].init.headers['Idempotency-Key'], calls[0].init.headers['Idempotency-Key']);
  assert.equal(r1.key, calls[0].init.headers['Idempotency-Key']); // results name their key
  assert.equal(r2.key, r1.key);
  assert.equal(r2.delivery, 'duplicate'); // the server already has that turn: not run twice
  assert.equal(s.unsettled, null);
});

test('after stop an edited text is held back; only an explicit discard gives a new key', async () => {
  const hanging = (init) =>
    new Promise((_resolve, reject) => {
      init.signal.addEventListener('abort', () => reject(Object.assign(new Error('aborted'), { name: 'AbortError' })));
    });
  const { fetch, calls } = fakeFetch([hanging, sse([['turn', {}]])]);
  const s = core.createSender({ fetch, t, makeKey });
  const first = s.send({ message: 'draft' });
  await new Promise(setImmediate);
  s.stop();
  await first;
  const edited = core.payloadOf('draft, edited', []);
  assert.equal(s.blocked(edited), true, 'an edited text would hide the unknown send');
  s.discard();
  assert.equal(s.blocked(edited), false);
  const r = await s.send({ message: 'draft, edited' });
  assert.equal(r.ok, true);
  assert.notEqual(calls[1].init.headers['Idempotency-Key'], calls[0].init.headers['Idempotency-Key']);
});

test('a turn that completed before the stream broke is settled, not unknown', async () => {
  let pulls = 0;
  const body = new ReadableStream({
    pull(c) {
      pulls += 1;
      if (pulls === 1) c.enqueue(new TextEncoder().encode('event: turn\ndata: {}\n\n'));
      else c.error(new Error('reset')); // connection reset after the turn event
    },
  });
  const { fetch } = fakeFetch([{ ok: true, status: 200, headers: { get: () => 'text/event-stream' }, body }]);
  const s = core.createSender({ fetch, t, makeKey });
  const r = await s.send({ message: 'x' });
  assert.equal(r.ok, false);
  assert.equal(r.completed, true);
  assert.equal(s.unsettled, null);
});

test('fetchHistory reads GET /api/chat/history (no-store)', async () => {
  const calls = [];
  const turns = await core.fetchHistory(async (url, init) => {
    calls.push([url, init.cache]);
    return { ok: true, json: async () => ({ turns: [{ message: 'a', response: 'b' }] }) };
  });
  assert.deepEqual(calls, [['/api/chat/history', 'no-store']]);
  assert.equal(turns.length, 1);
  await assert.rejects(core.fetchHistory(async () => ({ ok: false, status: 503 })));
});

test('both pages load the shared core before their own script', () => {
  const chat = fs.readFileSync(path.join(ROOT, 'dashboard/templates/chat.html'), 'utf8');
  assert.ok(chat.indexOf('js/chat_core.js') > 0 && chat.indexOf('js/chat_core.js') < chat.indexOf('js/chat.js'));
  const stage = fs.readFileSync(path.join(ROOT, 'dashboard/templates/stage.html'), 'utf8');
  assert.ok(stage.indexOf('js/chat_core.js') > 0 && stage.indexOf('js/chat_core.js') < stage.indexOf('js/main.js'));
  const chatJs = fs.readFileSync(path.join(ROOT, 'dashboard/static/js/chat.js'), 'utf8');
  for (const own of ['function readSSE', 'NO_EFFECT_STATUS = new Set', 'new FileReader', 'keyCode !== 229']) {
    assert.ok(!chatJs.includes(own), `chat.js still has its own copy of: ${own}`);
  }
  const src = fs.readFileSync(path.join(ROOT, 'dashboard/static/js/chat_core.js'), 'utf8');
  for (const m of src.matchAll(/["'`](\/api\/[^"'`]*)["'`]/g)) assert.match(m[1], /^\/api\/chat(\/history|\/session\/new|\/cancel)?$/);
  assert.doesNotMatch(src, /innerHTML|insertAdjacentHTML|document\.write|eval\(|new Function/);
});

// #452: the explicit stop is its own request; closing the stream (sender.stop) never sends it.
test('cancelTurn posts /api/chat/cancel and reports the refusal code', async () => {
  const ok = { ok: true, status: 202, json: async () => ({ status: 'cancelling', turn_id: 'abc' }) };
  const { fetch, calls } = fakeFetch([ok, refusal(409, { code: 'no_turn', error: 'No turn is running' }), new Error('offline')]);
  assert.deepEqual(await core.cancelTurn(fetch, 'abc', t), { ok: true, code: null, error: null });
  assert.equal(calls[0].url, '/api/chat/cancel');
  assert.equal(calls[0].init.method, 'POST');
  assert.deepEqual(JSON.parse(calls[0].init.body), { turn_id: 'abc' });
  const refused = await core.cancelTurn(fetch, null, t);
  assert.equal(refused.ok, false);
  assert.equal(refused.code, 'no_turn');
  assert.deepEqual(JSON.parse(calls[1].init.body), {});
  const failed = await core.cancelTurn(fetch, null, t);
  assert.equal(failed.ok, false);
  assert.equal(failed.error.message, 'offline');
});

test('stopping the reading does not ask the server to cancel', async () => {
  let release;
  const stream = new ReadableStream({ start(c) { release = () => c.close(); } });
  const { fetch, calls } = fakeFetch([{ ok: true, status: 200, headers: { get: () => 'text/event-stream' }, body: stream }]);
  const sender = core.createSender({ fetch, t, makeKey });
  const pending = sender.send({ message: 'hi' });
  await new Promise((r) => setTimeout(r, 0));
  assert.equal(sender.stop(), true);
  release();
  await pending;
  assert.deepEqual(calls.map((c) => c.url), ['/api/chat']);
});

test('#445: the running model call from model.* events, and its "generating N s" text', () => {
  const id = 'a'.repeat(32);
  const ev = (type, data) => ({ type, data });
  let g = core.generationOf(null, ev('model.started', { call_id: id }), 1000);
  assert.deepEqual(g, { callId: id, since: 1000 });
  // a progress read first (sent at once) fixes the start; a later queued start keeps it
  g = core.generationOf(null, ev('model.progress', { call_id: id, elapsed_ms: 20000 }), 50000);
  assert.deepEqual(g, { callId: id, since: 30000 });
  assert.equal(core.generationOf(g, ev('model.started', { call_id: id }), 51000), g);
  assert.equal(core.generationText(g, 42500, t), 'chat.generating({"s":12})');
  assert.equal(core.generationText(g, 30000 + 61000, t),
    'chat.generating({"s":61}) — chat.generating_long');
  assert.equal(core.generationOf(g, ev('model.completed', { call_id: id }), 60000), null);
  assert.equal(core.generationOf(g, ev('model.completed', { call_id: 'b'.repeat(32) }), 60000), g);
  assert.equal(core.generationOf(g, ev('turn.completed', {}), 60000), null);
  assert.equal(core.generationOf(null, ev('model.started', { call_id: 'not-an-id' }), 1), null);
  assert.equal(core.generationText(null, 1, t), '');
  // the catalogs word both lines
  for (const lang of ['ja', 'en']) {
    const cat = JSON.parse(fs.readFileSync(path.join(ROOT, 'dashboard/static/i18n', `catalog.${lang}.json`), 'utf8'));
    for (const key of ['chat.generating', 'chat.generating_long', 'stage.say_generating', 'stage.say_generating_long'])
      assert.ok(cat[key], `${lang} ${key}`);
    assert.match(cat['chat.generating'], /\{s\}/);
  }
});

test('a failed model call names the kind in the page language and settles the send', async () => {
  const catalog = JSON.parse(fs.readFileSync(path.join(ROOT, 'dashboard/static/i18n/catalog.en.json'), 'utf8'));
  const tc = (key, vars) => (key in catalog ? catalog[key].replace(/\{(\w+)\}/g, (m, k) => (vars && k in vars ? vars[k] : m)) : key);
  const data = { error: 'server text', code: 'model_failure', reason: 'refused', provider: 'llamacpp', host: '127.0.0.1:5599' };
  const { fetch } = fakeFetch([sse([['error', data]])]);
  const s = core.createSender({ fetch, t: tc, makeKey });
  const r = await s.send({ message: 'hi' });
  assert.equal(r.error.code, 'model_failure');
  assert.equal(r.error.message, `${catalog['chat.model_failure_refused']} (llamacpp · 127.0.0.1:5599)`);
  assert.equal(s.unsettled, null);
  // an unknown reason (or no catalog entry) keeps the server's own text
  const other = fakeFetch([sse([['error', { ...data, reason: 'surprise' }]])]);
  const r2 = await core.createSender({ fetch: other.fetch, t: tc, makeKey }).send({ message: 'x' });
  assert.equal(r2.error.message, 'server text');
  // #256: a conversation that ended with the model's own reply (tool call written as text)
  const bedrock = { ...data, reason: 'ends_with_assistant', provider: 'bedrock', host: undefined };
  const third = fakeFetch([sse([['error', bedrock]])]);
  const r3 = await core.createSender({ fetch: third.fetch, t: tc, makeKey }).send({ message: 'y' });
  assert.equal(r3.error.message, `${catalog['chat.model_failure_ends_with_assistant']} (bedrock)`);
});
