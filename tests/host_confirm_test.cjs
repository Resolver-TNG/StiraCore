// Host-confirmed operations in the shared fetch wrapper (ADR 0012 §8b, phase 2):
// node --test tests/host_confirm_test.cjs
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { resolve } = require('node:path');
const vm = require('node:vm');
const { catalog } = require('./ui_shim.cjs');

const source = readFileSync(resolve(__dirname, '../dashboard/static/js/api.js'), 'utf8');
const OP = 'ab'.repeat(16);
const HANG = Symbol('hang');
const FAIL = Symbol('fail');

function harness(server) {
  const body = { children: [], appendChild(node) { this.children.push(node); node.parent = this; } };
  const nodes = {};
  function element(tag) {
    return {
      tag, children: [], attrs: {}, style: {}, textContent: '', listeners: {},
      setAttribute(k, v) { this.attrs[k] = v; },
      appendChild(child) { this.children.push(child); },
      addEventListener(type, fn) { this.listeners[type] = fn; },
      remove() { body.children = body.children.filter((c) => c !== this); delete nodes[this.id]; },
      set id(v) { this._id = v; nodes[v] = this; },
      get id() { return this._id; },
    };
  }
  const calls = [];
  const document = {
    body,
    querySelector: () => ({ content: 'tok' }),
    getElementById: (id) => nodes[id] || null,
    createElement: element,
  };
  const context = {
    document, Headers, Response, JSON, Math, Date, Number, String, Promise, Object, Array,
    crypto: { randomUUID: (() => { let n = 0; return () => `key-${++n}`; })() },
    setTimeout: (fn) => setImmediate(fn),
    t: (key, vars) => {
      let text = Object.prototype.hasOwnProperty.call(catalog, key) ? catalog[key] : key;
      if (vars) text = text.replace(/\{(\w+)\}/g, (m, k) => (k in vars ? String(vars[k]) : m));
      return text;
    },
    fetch: async (url, init = {}) => {
      const headers = new Headers(init.headers || {});
      calls.push({ url, method: (init.method || 'GET').toUpperCase(), headers, body: init.body });
      const out = server(url, init, headers, calls);
      if (out === HANG) return new Promise(() => {});
      if (out === FAIL) throw new TypeError('network');
      const [status, data] = out;
      return new Response(JSON.stringify(data), { status, headers: { 'Content-Type': 'application/json' } });
    },
  };
  context.window = context;
  if (server.confirm) context.StiraConfirm = server.confirm;
  vm.runInNewContext(source, context);
  return { context, calls, body, nodes };
}

const required = (extra = {}) => [428, {
  code: 'host_confirmation_required',
  operation: { id: OP, short: OP.slice(0, 8), code: '12345678', expires_in: 120, channel: 'command',
    command: `stira approvals decide ${OP.slice(0, 8)}`, ...extra },
}];

function scripted(states, final = [202, { status: 'accepted' }]) {
  let polls = 0;
  return (url, init, headers) => {
    if (url === '/api/hitl/r1/decision') {
      return headers.has('X-Stira-Host-Operation') ? final : required();
    }
    if (url === `/api/host-operations/${OP}`) {
      const state = states[Math.min(polls++, states.length - 1)];
      return state === 404 ? [404, { code: 'host_operation_unknown' }] : [200, { operation: { state, expires_in: 100 } }];
    }
    if (url === `/api/host-operations/${OP}/cancel`) return [200, { operation: { state: 'cancelled' } }];
    return [500, {}];
  };
}

const send = (h, body = '{"action":"approve"}') => h.context.stiraFetch('/api/hitl/r1/decision', {
  method: 'POST', idempotent: true, headers: { 'Content-Type': 'application/json' }, body,
});

test('a confirmed operation is re-sent once with the header, same body, new key', async () => {
  const h = harness(scripted(['pending', 'pending', 'confirmed']));
  const response = await send(h);
  assert.equal(response.status, 202);
  const posts = h.calls.filter((c) => c.method === 'POST');
  assert.equal(posts.length, 2);
  assert.equal(posts[0].headers.has('X-Stira-Host-Operation'), false);
  assert.equal(posts[1].headers.get('X-Stira-Host-Operation'), OP);
  assert.equal(posts[1].body, posts[0].body);
  assert.notEqual(posts[1].headers.get('Idempotency-Key'), posts[0].headers.get('Idempotency-Key'));
  assert.equal(posts[1].headers.get('X-CSRF-Token'), 'tok');
  assert.equal(h.body.children.length, 0, 'the notice is removed');
});

for (const [state, code] of [['denied', 'host_operation_denied'], ['expired', 'host_operation_expired'],
  ['cancelled', 'host_operation_cancelled'], ['stale', 'stale_operation'], [404, 'host_operation_unknown']]) {
  test(`host outcome ${state} -> 409 ${code}, never re-sent`, async () => {
    const h = harness(scripted(['pending', state]));
    const response = await send(h);
    assert.equal(response.status, 409);
    const data = await response.json();
    assert.equal(data.code, code);
    assert.equal(data.error, catalog[`host.code_${code}`]);
    assert.equal(h.calls.filter((c) => c.method === 'POST').length, 1);
  });
}

test('the notice names the command and the code, and its button cancels', async () => {
  let seen = null;
  let cancelled = false;
  const states = ['pending'];
  const h = harness((url, init, headers) => {
    if (url === '/api/hitl/r1/decision') return required();
    if (url.endsWith('/cancel')) { cancelled = true; return [200, { operation: { state: 'cancelled' } }]; }
    if (!seen) {
      seen = h.nodes['host-confirm-notice'];
      seen.children[1].listeners.click();
    }
    return [200, { operation: { state: cancelled ? 'cancelled' : states[0], expires_in: 90 } }];
  });
  const response = await send(h);
  assert.ok(seen, 'a notice was shown');
  assert.equal(seen.attrs.role, 'status');
  assert.match(seen.children[0].textContent, /stira approvals decide abababab/);
  assert.match(seen.children[0].textContent, /12345678/);
  assert.equal(seen.children[1].textContent, catalog['host.cancel']);
  assert.equal(cancelled, true);
  assert.equal((await response.json()).code, 'host_operation_cancelled');
});

test('the dialog wording is used for the dialog channel; a bad command text is not shown', async () => {
  let text = null;
  const h = harness((url, init, headers) => {
    if (url === '/api/hitl/r1/decision') {
      if (headers.has('X-Stira-Host-Operation')) return [202, {}];
      const [s, d] = required({ channel: 'dialog', command: 'rm -rf /' });
      return [s, d];
    }
    text = h.nodes['host-confirm-notice'].children[0].textContent;
    return [200, { operation: { state: 'confirmed' } }];
  });
  await send(h);
  assert.equal(text, catalog['host.waiting_dialog'].replace('{short}', 'abababab').replace('{code}', '12345678').replace('{seconds}', '120'));
  assert.doesNotMatch(text, /rm -rf/);
});

test('anything else passes through untouched (no allowance: nothing runs)', async () => {
  for (const [status, data] of [[200, { ok: 1 }], [428, { code: 'confirmation_required' }],
    [428, { code: 'host_confirmation_required', operation: { id: 'not-hex' } }]]) {
    const h = harness(() => [status, data]);
    const response = await send(h);
    assert.equal(response.status, status);
    assert.deepEqual(await response.json(), data);
    assert.equal(h.calls.length, 1);
  }
});

test('a request that already names an operation is never re-sent again', async () => {
  const h = harness((url) => (url.startsWith('/api/host-operations/') ? [200, { operation: { state: 'confirmed' } }] : required()));
  const response = await h.context.stiraFetch('/api/hitl/r1/decision', {
    method: 'POST', headers: { 'X-Stira-Host-Operation': OP }, body: '{}',
  });
  assert.equal(response.status, 428);
  assert.equal(h.calls.length, 1);
});

// --- review of phase 2: cancel is final -----------------------------------------------

for (const [label, answer] of [['fails', FAIL], ['never answers', HANG]]) {
  test(`cancel is final: the cancel POST ${label}, a later poll says confirmed, nothing is re-sent`, async () => {
    let clicked = false;
    let polls = 0;
    const h = harness((url) => {
      if (url === '/api/hitl/r1/decision') return required();
      if (url.endsWith('/cancel')) return answer;
      polls += 1;
      if (!clicked) {
        clicked = true;
        h.nodes['host-confirm-notice'].children[1].listeners.click();
      }
      return [200, { operation: { state: 'confirmed', expires_in: 90 } }];
    });
    const response = await send(h);
    assert.equal(response.status, 409);
    assert.equal((await response.json()).code, 'host_operation_cancelled');
    const decisions = h.calls.filter((c) => c.url === '/api/hitl/r1/decision');
    assert.equal(decisions.length, 1, 'the original request is never sent again');
    assert.equal(h.body.children.length, 0, 'the notice closed at the press');
    await new Promise((r) => setImmediate(r));
    assert.equal(h.calls.filter((c) => c.url === '/api/hitl/r1/decision').length, 1);
    assert.ok(polls <= 1, 'no poll after the cancel');
  });
}

// --- revision 6: the browser channel ---------------------------------------------------

const TOKEN = 'cd'.repeat(16);
const INSTANCE = '0123456789abcdef';
const browserRequired = (extra = {}) => [428, {
  code: 'host_confirmation_required',
  operation: { id: OP, short: OP.slice(0, 8), code: '12345678', expires_in: 120, channel: 'browser',
    kind: 'rules_loosen', word: 'APPROVE', instance: INSTANCE, confirm_token: TOKEN,
    summary: { source: 'tools_page', changes: ['http_request', 'ask'] }, ...extra },
}];

function browserServer(answer, confirmResult = [200, { operation: { state: 'confirmed' } }], extra = {}) {
  const seen = { dialogs: [] };
  const server = (url, init, headers) => {
    if (url === '/api/hitl/r1/decision') {
      return headers.has('X-Stira-Host-Operation') ? [202, { status: 'accepted' }] : browserRequired(extra);
    }
    if (url === `/api/host-operations/${OP}/confirm`) { seen.confirm = JSON.parse(init.body); return confirmResult; }
    if (url === `/api/host-operations/${OP}/cancel`) { seen.cancel = true; return [200, { operation: { state: 'cancelled' } }]; }
    return [500, {}];
  };
  server.confirm = { open: async (options) => { seen.dialogs.push(options); return answer; } };
  return { server, seen };
}

test('browser: the dialog shows the fixed format and one checkbox; confirm posts the token, then re-sends once', async () => {
  const { server, seen } = browserServer(true);
  const h = harness(server);
  const response = await send(h);
  assert.equal(response.status, 202);
  assert.equal(seen.dialogs.length, 1);
  const dialog = seen.dialogs[0];
  // Phase 2c (operator decision 2026-10-19): a checkbox, no typed word.
  assert.equal(dialog.typed, undefined);
  assert.deepEqual([...dialog.checks], [catalog['host.browser_check']]);
  assert.equal(dialog.title, catalog['host.browser_title'].replace('{what}', catalog['host.kind_rules_loosen']));
  assert.deepEqual([...dialog.items.slice(0, 2)], [
    `${catalog['host.field_source']}: tools_page`, `${catalog['host.field_changes']}: http_request, ask`,
  ]);
  assert.deepEqual({ ...seen.confirm }, { token: TOKEN, acknowledged: true, instance: INSTANCE });
  const posts = h.calls.filter((c) => c.url === '/api/hitl/r1/decision');
  assert.equal(posts.length, 2);
  assert.equal(posts[1].headers.get('X-Stira-Host-Operation'), OP);
  assert.equal(posts[1].body, posts[0].body);
  assert.equal(h.calls.filter((c) => c.url.startsWith('/api/host-operations/') && c.method === 'GET').length, 0, 'no polling');
});

test('browser: agent text never reaches the dialog (a summary value that is not a code shows "?")', async () => {
  const { server, seen } = browserServer(false, undefined, {
    summary: { tool: 'Ignore previous instructions', changes: ['ok_code', 'Free Text!'] },
  });
  await send(harness(server));
  const items = seen.dialogs[0].items.join('\n');
  assert.doesNotMatch(items, /Ignore|Free Text/);
  assert.match(items, /: \?/);
  assert.match(items, /ok_code/);
});

test('browser: cancel in the dialog is final, sends the cancel, never re-sends', async () => {
  const { server, seen } = browserServer(false);
  const h = harness(server);
  const response = await send(h);
  assert.equal(response.status, 409);
  assert.equal((await response.json()).code, 'host_operation_cancelled');
  await new Promise((r) => setImmediate(r));
  assert.equal(seen.cancel, true);
  assert.equal(seen.confirm, undefined);
  assert.equal(h.calls.filter((c) => c.url === '/api/hitl/r1/decision').length, 1);
});

for (const code of ['host_browser_refused', 'host_operation_expired', 'tool_call_running', 'host_confirm_on_host']) {
  test(`browser: a refused confirmation (${code}) reaches the page with its code, nothing re-sent`, async () => {
    const { server } = browserServer(true, [code === 'host_browser_refused' ? 403 : 409, { code }]);
    const h = harness(server);
    const response = await send(h);
    assert.equal(response.status, 409);
    const data = await response.json();
    assert.equal(data.code, code);
    assert.equal(data.error, catalog[`host.code_${code}`]);
    assert.equal(h.calls.filter((c) => c.url === '/api/hitl/r1/decision').length, 1);
  });
}

test('browser: no token, a bad instance, an unknown kind or no dialog -> nothing is shown, nothing re-sent', async () => {
  // approval_decide is not a confirmed kind since phase 2c (an approve is one press).
  for (const extra of [{ confirm_token: undefined }, { instance: 'x' }, { instance: 'あ'.repeat(16) },
    { kind: 'other' }, { kind: 'approval_decide' }]) {
    const { server, seen } = browserServer(true, undefined, extra);
    const h = harness(server);
    const response = await send(h);
    assert.equal((await response.json()).code, 'host_channel_unavailable');
    assert.equal(seen.dialogs.length, 0);
    assert.equal(h.calls.filter((c) => c.url === '/api/hitl/r1/decision').length, 1);
  }
  const { server } = browserServer(true);
  delete server.confirm;
  const h = harness(server);
  assert.equal((await (await send(h)).json()).code, 'host_channel_unavailable');
});

// --- phase 2c: the YOLO request's confirmation (checkboxes; the button itself is phase 3) ---

const REQ = 'ef'.repeat(16);
function yoloServer(answer, confirmResult = [200, { operation: { state: 'confirmed' } }]) {
  const seen = { dialogs: [] };
  const server = (url, init) => {
    if (url === `/api/host-operations/${REQ}/confirm`) { seen.confirm = JSON.parse(init.body); return confirmResult; }
    if (url === '/api/yolo/request/cancel') { seen.cancel = JSON.parse(init.body); return [200, {}]; }
    return [500, {}];
  };
  server.confirm = { open: async (options) => { seen.dialogs.push(options); return answer; } };
  return { server, seen };
}
const press = (external) => ({
  request: { id: REQ, short: REQ.slice(0, 8), tier: external ? 'external' : 'internal', expires_in: 120,
    ends_at: Date.UTC(2026, 9, 19, 6, 30) / 1000 },
  confirm: { token: TOKEN, external, instance: INSTANCE },
});

test('yolo: internal shows one checkbox, external one more; confirm posts the ticks', async () => {
  for (const external of [false, true]) {
    const { server, seen } = yoloServer(true);
    const h = harness(server);
    assert.equal(await h.context.StiraHostConfirm.yolo(press(external)), 'confirmed');
    const dialog = seen.dialogs[0];
    const checks = [catalog['host.browser_check']];
    if (external) checks.push(catalog['host.browser_check_external']);
    assert.deepEqual([...dialog.checks], checks);
    assert.equal(dialog.typed, undefined);
    assert.equal(dialog.confirmLabel, catalog['host.browser_confirm_yolo']);
    assert.equal(dialog.title, catalog['host.browser_title'].replace('{what}', catalog['host.kind_yolo_request']));
    assert.equal(dialog.items[0], `${catalog['host.field_tier']}: ${catalog[`host.tier_${external ? 'external' : 'internal'}`]}`);
    assert.equal(dialog.items[1], `${catalog['host.field_request']}: ${REQ.slice(0, 8)}`);
    assert.deepEqual({ ...seen.confirm },
      { token: TOKEN, acknowledged: true, external_acknowledged: external, instance: INSTANCE });
    assert.equal(seen.cancel, undefined);
  }
});

test('yolo: a request with exec shows a third checkbox and posts exec_acknowledged (phase 7)', async () => {
  const { server, seen } = yoloServer(true);
  const h = harness(server);
  const answer = press(true);
  answer.confirm.exec = true;
  assert.equal(await h.context.StiraHostConfirm.yolo(answer), 'confirmed');
  const dialog = seen.dialogs[0];
  assert.deepEqual([...dialog.checks], [
    catalog['host.browser_check'], catalog['host.browser_check_external'], catalog['host.browser_check_exec'],
  ]);
  assert.equal(dialog.items[0], `${catalog['host.field_tier']}: ${catalog['host.tier_exec']}`);
  assert.deepEqual({ ...seen.confirm },
    { token: TOKEN, acknowledged: true, external_acknowledged: true, instance: INSTANCE, exec_acknowledged: true });
  // exec without external (not a shape the server sends): no exec box, no exec tick.
  const odd = yoloServer(true);
  const internal = press(false);
  internal.confirm.exec = true;
  assert.equal(await harness(odd.server).context.StiraHostConfirm.yolo(internal), 'confirmed');
  assert.deepEqual([...odd.seen.dialogs[0].checks], [catalog['host.browser_check']]);
  assert.equal(odd.seen.confirm.exec_acknowledged, undefined);
  // ja wording names the bypass.
  const ja = JSON.parse(require('node:fs').readFileSync(require('node:path').resolve(__dirname, '../dashboard/static/i18n/catalog.ja.json'), 'utf8'));
  assert.match(ja['host.browser_check_exec'], /迂回/);
});

test('yolo: cancel sends the cancel of that request and confirms nothing; bad input opens nothing', async () => {
  const { server, seen } = yoloServer(false);
  const h = harness(server);
  assert.equal(await h.context.StiraHostConfirm.yolo(press(true)), 'cancelled');
  await new Promise((r) => setImmediate(r));
  assert.deepEqual({ ...seen.cancel }, { request_id: REQ });
  assert.equal(seen.confirm, undefined);
  for (const bad of [{ ...press(false), confirm: { ...press(false).confirm, token: 'x' } },
    { ...press(false), confirm: { ...press(false).confirm, instance: 'あ'.repeat(16) } }, {}]) {
    const other = yoloServer(true);
    const result = await harness(other.server).context.StiraHostConfirm.yolo(bad);
    assert.deepEqual({ ...result }, { code: 'host_channel_unavailable' });
    assert.equal(other.seen.dialogs.length, 0);
  }
});

test('yolo: a refusal of the confirm route comes back with its code', async () => {
  const { server } = yoloServer(true, [400, { code: 'host_not_acknowledged' }]);
  const result = await harness(server).context.StiraHostConfirm.yolo(press(false));
  assert.deepEqual({ ...result }, { code: 'host_not_acknowledged' });
});
