// Stage stage 2 (#341): event adapter, live feed, approval client and served-page rules.
// node --test tests/stage_adapter_test.cjs
//
// The fixtures are measured: tests/fixtures/stage_turn_events.json holds the turn-contract
// events (/api/turns/<id>/events) of five real chat server turns (offline fake Bedrock): a plain
// turn, a breaker transition, memory_search, a failing file_read and an approved file_write.
// tests/fixtures/stage_hitl_pending.json is a real /api/hitl/pending answer whose arguments
// hold a fake key (masked by the server).
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { pathToFileURL } = require('node:url');

const ROOT = path.join(__dirname, '..');
const STAGE = path.join(ROOT, 'dashboard', 'stage', 'js');
const load = (name) => import(pathToFileURL(path.join(STAGE, name)).href);
const TURNS = JSON.parse(fs.readFileSync(path.join(__dirname, 'fixtures', 'stage_turn_events.json'), 'utf8'));
const PENDING = JSON.parse(fs.readFileSync(path.join(__dirname, 'fixtures', 'stage_hitl_pending.json'), 'utf8'));
const turnWith = (name) => TURNS.find((t) => t.events.some((e) => e.data && e.data.name === name));
const turnOfType = (type) => TURNS.find((t) => t.events.some((e) => e.type === type));

test('tool names map to the Stage kinds (shelf / window / desk) and gadget categories', async () => {
  const { toolKind, toolCategory, memoryOp } = await load('adapter.js');
  assert.equal(toolKind('memory_search'), 'memory');
  assert.equal(toolKind('memory_write'), 'memory');
  assert.equal(toolKind('retrieve_offloaded_content'), 'memory');
  assert.equal(toolKind('web_fetch'), 'web');
  assert.equal(toolKind('http_request'), 'web');
  assert.equal(toolKind('perplexity_search'), 'web');
  assert.equal(toolKind('file_write'), 'tool');
  assert.equal(toolKind(''), 'tool');
  assert.equal(toolKind(undefined), 'tool');
  assert.equal(toolCategory('lsp_diagnostics'), 'code');
  assert.equal(toolCategory('file_read'), 'code');
  assert.equal(toolCategory('use_agent'), 'subagent');
  assert.equal(toolCategory('webhook_send'), 'other');
  assert.equal(memoryOp('memory_write'), 'write');
  assert.equal(memoryOp('memory_search'), 'read');
  assert.equal(memoryOp('file_write'), null);
});

test('breaker levels: three looks; COHERENCE uses the WARNING look and keeps its name', async () => {
  const { breakerLevel } = await load('adapter.js');
  assert.deepEqual(breakerLevel('normal'), { level: 'NORMAL', raw: 'normal' });
  assert.deepEqual(breakerLevel({ level: 'critical', last_changed: 'x' }), { level: 'CRITICAL', raw: 'critical' });
  assert.deepEqual(breakerLevel('COHERENCE'), { level: 'WARNING', raw: 'coherence' });
  assert.equal(breakerLevel('halt'), null);
  assert.equal(breakerLevel(null), null);
});

test('real memory_search turn -> turn.start, tool.start(memory), tool.end, neural, turn.end', async () => {
  const { fromTurnEvent } = await load('adapter.js');
  const out = turnWith('memory_search').events.flatMap(fromTurnEvent);
  assert.deepEqual(out.map((e) => e.type), ['turn.start', 'tool.start', 'tool.end', 'neural', 'turn.end']);
  const start = out[1];
  assert.equal(start.kind, 'memory');
  assert.equal(start.name, 'memory_search');
  assert.equal(start.op, 'read');
  assert.equal(out[2].id, start.id);
  assert.equal(out[2].status, 'done');
  assert.equal(out[4].ok, true);
  assert.equal(typeof out[3].values.stability, 'number');
});

test('real breaker transition and a failing tool map to breaker / tool.end(done)', async () => {
  const { fromTurnEvent } = await load('adapter.js');
  const tr = turnOfType('breaker.transition');
  const b = tr.events.flatMap(fromTurnEvent).find((e) => e.type === 'breaker');
  const to = tr.events.find((e) => e.type === 'breaker.transition').data.to;
  assert.equal(b.level, to.toUpperCase());
  // file_read of a missing file still *completed* (the result says so; status done)
  const fr = turnWith('file_read').events.flatMap(fromTurnEvent);
  assert.deepEqual(fr.map((e) => e.type), ['turn.start', 'tool.start', 'tool.end', 'neural', 'turn.end']);
  assert.equal(fr[1].kind, 'tool');
  assert.equal(fr[1].category, 'code');
});

test('approval events in a turn are not used (the pending queue is the source of truth)', async () => {
  const { fromTurnEvent } = await load('adapter.js');
  const t = turnOfType('approval.requested');
  const out = t.events.flatMap(fromTurnEvent);
  assert.ok(!out.some((e) => e.type.startsWith('approval')));
  // the tool is created only after the decision: no desk light while waiting
  const types = t.events.map((e) => e.type);
  assert.ok(types.indexOf('approval.resolved') < types.indexOf('tool_call.created'));
});

test('no preview, path or body ever leaves the adapter', async () => {
  const { fromTurnEvent, describeSource } = await load('adapter.js');
  for (const t of TURNS) {
    for (const ev of t.events) {
      const mapped = fromTurnEvent(ev);
      const text = JSON.stringify(mapped) + describeSource(ev, mapped);
      assert.doesNotMatch(text, /\/private\/|\/tmp\/|preview|File read|File write|plan v|prompt_cache|_hash/, ev.type);
      const allowed = new Set(['type', 'turn', 'seq', 'id', 'name', 'kind', 'category', 'op', 'status', 'level', 'raw', 'values', 'ok', 'stop', 'cut']);
      for (const m of mapped) for (const k of Object.keys(m)) assert.ok(allowed.has(k), `${ev.type}: ${k}`);
    }
  }
});

test('describeSource drops free text and path-like values from the debug log', async () => {
  const { describeSource } = await load('adapter.js');
  const ev = { type: 'tool_call.created', turn_id: 'abcdef012345', seq: 3, data: { name: 'SECRET /private/path body', status: 'ok', to: 'x'.repeat(200) } };
  const text = describeSource(ev, []);
  assert.doesNotMatch(text, /SECRET|private|xxxx/);
  assert.match(text, /tool_call\.created ok/);
});

test('mapping is deterministic: same events, same Stage events and the same pose', async () => {
  const { fromTurnEvent } = await load('adapter.js');
  const { initialState, reduce, derive, poseSignature } = await load('model.js');
  for (const t of TURNS) {
    const a = t.events.flatMap(fromTurnEvent);
    const b = structuredClone(t.events).flatMap(fromTurnEvent);
    assert.deepEqual(a, b);
    assert.equal(poseSignature(derive(a.reduce(reduce, initialState())).pose), poseSignature(derive(b.reduce(reduce, initialState())).pose));
    // a finished turn leaves no busy look behind
    const pose = derive(a.reduce(reduce, initialState())).pose;
    for (const k of ['steam', 'light.desk', 'light.shelf', 'light.window', 'choice']) assert.equal(pose[k], 0, k);
  }
});

test('malformed events are ignored', async () => {
  const { fromTurnEvent } = await load('adapter.js');
  for (const bad of [null, 1, 'x', {}, { type: 7 }, { type: 'tool_call.created', data: {} }, { type: 'tool_call.updated', data: { tool_use_id: 'a', status: 'running' } }, { type: 'breaker.transition', data: { to: 'meltdown' } }, { type: 'memory.injected', data: {} }]) {
    assert.deepEqual(fromTurnEvent(bad), []);
  }
  // a call refused at the gate is created as denied: start and end at once
  const denied = fromTurnEvent({ type: 'tool_call.created', turn_id: 't', seq: 2, data: { tool_use_id: 'u', name: 'file_write', status: 'denied' } });
  assert.deepEqual(denied.map((e) => e.type), ['tool.start', 'tool.end']);
});

test('status snapshots emit neural / breaker only when they change', async () => {
  const { fromStatus } = await load('adapter.js');
  const s = { neural_state: { trust: 0.5, curiosity: 0.5, vigilance: 0.1, attachment: 0.5, stability: 0.7, satisfaction: 0.5 }, breaker: { level: 'normal' }, soul: { path: '/secret/soul.md' } };
  const a = fromStatus(s, {});
  assert.deepEqual(a.events.map((e) => e.type), ['neural', 'breaker']);
  assert.doesNotMatch(JSON.stringify(a.events), /secret|soul/);
  assert.deepEqual(fromStatus(s, a.memo).events, []);
  const b = fromStatus({ ...s, breaker: { level: 'coherence' } }, a.memo);
  assert.deepEqual(b.events, [{ type: 'breaker', level: 'WARNING', raw: 'coherence', source: 'status' }]);
  assert.equal(b.notes[0].key, 'stage.log_coherence');
});

test('pending queue -> one approval at a time, oldest first, masked record only', async () => {
  const { fromPending } = await load('adapter.js');
  const first = fromPending(PENDING, null);
  assert.deepEqual(first.events.map((e) => e.type), ['approval.request']);
  assert.equal(first.events[0].summary, 'file_write');
  assert.equal(first.current.tool, 'file_write');
  assert.equal(first.current.masked, true);
  assert.match(first.current.args, /sk-\*\*\*/); // the server's mask, kept as is
  assert.doesNotMatch(JSON.stringify(first.events), /notes\/|sk-/); // the scene gets the tool name only
  assert.deepEqual(fromPending(PENDING, first.current.id).events, []);
  const gone = fromPending({ requests: [] }, first.current.id);
  assert.deepEqual(gone.events, [{ type: 'approval.resolved', id: first.current.id, source: 'pending' }]);
  const two = { requests: [{ ...PENDING.requests[0], id: 'b', created_at: 20 }, { ...PENDING.requests[0], id: 'a', created_at: 10 }] };
  const r = fromPending(two, null);
  assert.equal(r.current.id, 'a');
  assert.equal(r.count, 2);
});

test('turns cut by a restart are not shown as open', async () => {
  const { turnIsOpen, latestTurn } = await load('adapter.js');
  assert.equal(turnIsOpen({ turn_id: 'a', completed: true, started_at: 100 }, 50), false);
  assert.equal(turnIsOpen({ turn_id: 'a', completed: false, started_at: 100 }, 50), true);
  assert.equal(turnIsOpen({ turn_id: 'a', completed: false, started_at: 100 }, 200), false);
  assert.equal(latestTurn({ turns: [{ turn_id: 'n' }, { turn_id: 'o' }] }).turn_id, 'n');
  assert.equal(latestTurn({ turns: [] }), null);
});

test('pacer: a fast tool stays visible MIN_SHOW s, order is kept, backlogs flush', async () => {
  const { Pacer, MIN_SHOW, MIN_TURN, BACKLOG_FLUSH } = await load('adapter.js');
  const p = new Pacer();
  p.push([{ type: 'turn.start' }, { type: 'tool.start', id: 'x' }, { type: 'tool.end', id: 'x' }, { type: 'neural', values: {} }, { type: 'turn.end' }]);
  assert.deepEqual(p.take(0).map((e) => e.type), ['turn.start', 'tool.start']);
  assert.deepEqual(p.take(MIN_SHOW - 0.1), []);
  assert.deepEqual(p.take(MIN_SHOW).map((e) => e.type), ['tool.end', 'neural']);
  assert.deepEqual(p.take(MIN_TURN - 0.1), []);
  assert.deepEqual(p.take(MIN_TURN).map((e) => e.type), ['turn.end']);
  const q = new Pacer();
  const many = [];
  for (let i = 0; i <= BACKLOG_FLUSH; i += 1) many.push({ type: 'tool.start', id: `t${i}` }, { type: 'tool.end', id: `t${i}` });
  q.push(many);
  assert.equal(q.take(0).length, many.length);
});

test('narration names the state, never a body; offline wins', async () => {
  const { narrate } = await load('adapter.js');
  const { initialState, reduce } = await load('model.js');
  let s = initialState();
  assert.equal(narrate(s).key, 'stage.say_idle');
  s = reduce(s, { type: 'turn.start' });
  assert.equal(narrate(s).key, 'stage.say_thinking');
  s = reduce(s, { type: 'tool.start', id: 'm', kind: 'memory', name: 'memory_write' });
  assert.deepEqual(narrate(s, { memoryWrite: new Set(['m']) }), { key: 'stage.say_memory_write', vars: { name: 'memory_write' } });
  s = reduce(s, { type: 'approval.request', id: 'a', summary: 'file_write' });
  assert.deepEqual(narrate(s, { pendingCount: 3 }), { key: 'stage.say_approval_more', vars: { tool: 'file_write', more: 2 } });
  assert.equal(narrate(s, { offline: true }).key, 'stage.say_offline');
  s = reduce(reduce(initialState(), { type: 'breaker', level: 'WARNING' }), { type: 'turn.end', ok: false });
  assert.equal(narrate(s, { breakerRaw: 'coherence' }).key, 'stage.say_coherence');
  assert.equal(narrate(reduce(initialState(), { type: 'turn.end', ok: false })).key, 'stage.say_done_error');
  // a turn cut off at the output limit says so (#363); the next turn clears it
  const cut = reduce(initialState(), { type: 'turn.end', ok: false, stop: 'max_tokens' });
  assert.equal(narrate(cut).key, 'stage.say_done_max_tokens');
  assert.equal(narrate(reduce(cut, { type: 'turn.start' })).key, 'stage.say_thinking');
  assert.equal(narrate(reduce(reduce(cut, { type: 'turn.start' }), { type: 'turn.end', ok: false, stop: 'error' })).key, 'stage.say_done_error');
});

test('turn.completed with stop=max_tokens reaches the Stage as a failed, cut-off turn (#363)', async () => {
  const { fromTurnEvent } = await load('adapter.js');
  const [end] = fromTurnEvent({ type: 'turn.completed', turn_id: 't1', seq: 9, data: { error: true, stop: 'max_tokens', usage: { input_tokens: 1, output_tokens: 2 } } });
  assert.equal(end.type, 'turn.end');
  assert.equal(end.ok, false);
  assert.equal(end.stop, 'max_tokens');
  assert.equal(end.cut, undefined);
});

test('a cut before the reply began has its own narration; other cut values are dropped (#416)', async () => {
  const { fromTurnEvent, narrate } = await load('adapter.js');
  const { reduce, initialState } = await load('model.js');
  const [end] = fromTurnEvent({ type: 'turn.completed', turn_id: 't1', seq: 9, data: { error: true, stop: 'max_tokens', cut: 'before_reply' } });
  assert.equal(end.cut, 'before_reply');
  assert.equal(narrate(reduce(initialState(), end)).key, 'stage.say_done_max_tokens_before_reply');
  const [odd] = fromTurnEvent({ type: 'turn.completed', turn_id: 't2', seq: 10, data: { error: true, stop: 'max_tokens', cut: '<b>x</b>' } });
  assert.equal(odd.cut, undefined);
  assert.equal(narrate(reduce(initialState(), odd)).key, 'stage.say_done_max_tokens');
  // cut without stop=max_tokens means nothing
  assert.equal(narrate(reduce(initialState(), { type: 'turn.end', ok: false, stop: 'error', cut: 'before_reply' })).key, 'stage.say_done_error');
});

// --- live feed against a fake server built from the fixtures --------------------------------
function fakeServer() {
  const turn = turnWith('memory_search');
  const st = { uptime_seconds: 100, neural_state: { trust: 0.5, curiosity: 0.5, vigilance: 0.1, attachment: 0.5, stability: 0.7, satisfaction: 0.5 }, breaker: { level: 'normal' } };
  const srv = { visible: 0, turns: [], status: st, pending: { requests: [], server_time: 1000 }, calls: [] };
  srv.getJSON = async (url) => {
    srv.calls.push(url);
    if (url === '/api/gateway/status') return srv.status;
    if (url === '/api/hitl/pending') return srv.pending;
    if (url.startsWith('/api/turns?')) return { turns: srv.turns };
    const m = url.match(/^\/api\/turns\/([^/]+)\/events\?after_seq=(\d+)$/);
    if (m) return { turn_id: m[1], events: turn.events.slice(0, srv.visible).filter((e) => e.seq > Number(m[2])) };
    throw new Error('404');
  };
  srv.turn = turn;
  return srv;
}

test('live feed: a completed turn found on page load is history (not replayed)', async () => {
  const { LiveFeed } = await load('live.js');
  const srv = fakeServer();
  srv.turns = [{ ...srv.turn.turn, completed: true }];
  srv.visible = srv.turn.events.length;
  const feed = new LiveFeed({ getJSON: srv.getJSON, clock: () => 0, wall: () => 1000 });
  await feed.boot();
  const ev = feed.drain(0);
  assert.deepEqual(ev.map((e) => e.type), ['neural', 'breaker']);
  assert.ok(!srv.calls.some((u) => u.includes('/events')));
});

test('live feed: follows a new turn through the events API, in order, with pacing', async () => {
  const { LiveFeed } = await load('live.js');
  const { MIN_TURN } = await load('adapter.js');
  const srv = fakeServer();
  let wall = 1000;
  const feed = new LiveFeed({ getJSON: srv.getJSON, clock: () => 0, wall: () => wall });
  await feed.boot();
  assert.deepEqual(feed.drain(0).map((e) => e.type), ['neural', 'breaker']);
  // a turn starts: only turn.started is recorded so far
  srv.turns = [{ ...srv.turn.turn, completed: false, started_at: 1000 }];
  srv.visible = 1;
  wall += 1;
  await feed.pollTurns();
  assert.deepEqual(feed.drain(10).map((e) => e.type), ['turn.start']);
  // the rest arrives in one batch
  srv.visible = srv.turn.events.length;
  srv.turns = [{ ...srv.turn.turn, completed: true, started_at: 1000 }];
  wall += 1;
  await feed.pollTurns();
  assert.deepEqual(feed.drain(10).map((e) => e.type), ['tool.start']);
  assert.deepEqual(feed.drain(10 + MIN_TURN + 0.01).map((e) => e.type), ['tool.end', 'neural', 'turn.end']);
  assert.equal(feed.turn.done, true);
  // a status snapshot taken before that batch is stale: not applied
  // (same Neural State as the turn's summary: only the breaker differs)
  const after = srv.turn.events.find((e) => e.type === 'turn.summary').data.after;
  srv.status = { ...srv.status, neural_state: after, breaker: { level: 'warning' } };
  feed.lastStatus = srv.status;
  feed.lastStatusAt = wall - 0.5;
  feed.applyStatus();
  assert.deepEqual(feed.drain(20), []);
  wall += 1; // the next snapshot is newer than the batch
  await feed.pollStatus();
  feed.applyStatus();
  assert.deepEqual(feed.drain(20).map((e) => e.type), ['breaker']);
});

test('live feed: a turn started before the server process is not followed', async () => {
  const { LiveFeed } = await load('live.js');
  const srv = fakeServer();
  srv.turns = [{ ...srv.turn.turn, completed: false, started_at: 800 }]; // uptime 100 -> start 900
  const notes = [];
  const feed = new LiveFeed({ getJSON: srv.getJSON, clock: () => 0, wall: () => 1000, onNote: (n) => notes.push(n) });
  await feed.boot();
  assert.deepEqual(feed.drain(0).map((e) => e.type), ['neural', 'breaker']);
  assert.equal(notes[0].key, 'stage.log_interrupted');
});

test('live feed: offline after two failed status reads; decided requests stay closed', async () => {
  const { LiveFeed } = await load('live.js');
  const srv = fakeServer();
  srv.pending = PENDING;
  const feed = new LiveFeed({ getJSON: srv.getJSON, clock: () => 0, wall: () => 1000 });
  await feed.boot();
  assert.ok(feed.drain(0).some((e) => e.type === 'approval.request'));
  feed.markDecided(PENDING.requests[0].id);
  assert.deepEqual(feed.drain(0).map((e) => e.type), ['approval.resolved']);
  await feed.pollPending(); // the queue may still list it for a moment
  assert.deepEqual(feed.drain(0), []);
  assert.equal(feed.approval, null);
  const broken = new LiveFeed({ getJSON: async () => { throw new Error('502'); }, clock: () => 0, wall: () => 0 });
  await broken.pollStatus();
  assert.equal(broken.offline, false);
  await broken.pollStatus();
  assert.equal(broken.offline, true);
});

// --- approval client -------------------------------------------------------------------------
function fakePost(responses) {
  const calls = [];
  const post = async (url, body, headers) => {
    calls.push({ url, body: JSON.parse(body), headers });
    const r = responses.shift();
    if (r instanceof Error) throw r;
    return { ok: r.status >= 200 && r.status < 300, status: r.status, json: async () => r.body };
  };
  return { post, calls };
}

test('approval: approve needs the arguments shown, is not offered when expired', async () => {
  const { approvalChoices } = await load('approval.js');
  const { approvalRecord } = await load('adapter.js');
  const rec = approvalRecord(PENDING.requests[0]);
  const ids = (cs) => cs.map((c) => `${c.id}:${c.disabled ? 'off' : 'on'}`);
  assert.deepEqual(ids(approvalChoices(rec, { argsSeen: false, expired: false })), ['args:on', 'approve:off', 'reject:on', 'page:on']);
  assert.deepEqual(ids(approvalChoices(rec, { argsSeen: true, expired: false })), ['args:on', 'approve:on', 'reject:on', 'page:on']);
  assert.deepEqual(ids(approvalChoices(rec, { argsSeen: true, expired: true })), ['args:on', 'approve:off', 'reject:on', 'page:on']);
  assert.equal(approvalChoices(rec, { argsSeen: true, expired: true })[1].key, 'stage.choice_approve_expired');
  // no session/permanent permission, no edit here (approvals page)
  assert.ok(!approvalChoices(rec, { argsSeen: true }).some((c) => /always|session|edit/.test(c.id)));
});

test('approval: the two allow-again choices are drawn only where offered (one constant)', async () => {
  const { approvalChoices, ALLOW_AGAIN_ON, allowReasonText } = await load('approval.js');
  const { approvalRecord } = await load('adapter.js');
  assert.equal(ALLOW_AGAIN_ON, true);
  const offered = { offered: true, token: 'tk', class_kind: 'dir_subtree', label: 'game/', whole_root: false, max_hours: 2 };
  const rec = approvalRecord({ ...PENDING.requests[0], allow_again: offered });
  assert.deepEqual(rec.allowAgain, { ...offered, listFull: false });
  const ids = (cs) => cs.map((c) => `${c.id}:${c.disabled ? 'off' : 'on'}`);
  assert.deepEqual(ids(approvalChoices(rec, { argsSeen: true, allowOn: false })), ['args:on', 'approve:on', 'reject:on', 'page:on']);
  // As PR 3 draws them: next to approve, locked like approve until the arguments were shown.
  assert.deepEqual(ids(approvalChoices(rec, { argsSeen: false, allowOn: true })),
    ['args:on', 'approve:off', 'allow_session:off', 'allow_always:off', 'reject:on', 'page:on']);
  assert.deepEqual(ids(approvalChoices(rec, { argsSeen: true, allowOn: true })),
    ['args:on', 'approve:on', 'allow_session:on', 'allow_always:on', 'reject:on', 'page:on']);
  const full = approvalRecord({ ...PENDING.requests[0], allow_again: { ...offered, list_full: true } });
  assert.deepEqual(ids(approvalChoices(full, { argsSeen: true, allowOn: true })).slice(2, 4), ['allow_session:off', 'allow_always:on']);
  const exec = approvalRecord({ ...PENDING.requests[0], allow_again: { offered: false, reason: 'exec' } });
  assert.ok(!approvalChoices(exec, { argsSeen: true, allowOn: true }).some((c) => c.id.startsWith('allow_')));
  const t = (key, vars) => `${key}${vars ? JSON.stringify(vars) : ''}`;
  assert.equal(allowReasonText(exec, t, false), null, 'the constant off: no line either');
  assert.equal(allowReasonText(exec, t, true), 'approvals.allow_not_offered{"reason":"approvals.allow_reason_exec"}');
});

test('approval: an allow-again press sends approve + the view token only', async () => {
  const { ApprovalClient } = await load('approval.js');
  const { approvalRecord } = await load('adapter.js');
  const rec = approvalRecord({ ...PENDING.requests[0], allow_again: { offered: true, token: 'view-tk', class_kind: 'host', label: 'a.example' } });
  const { post, calls } = fakePost([{ status: 202, body: { status: 'accepted', allow: { status: 'allowed' } } }]);
  const c = new ApprovalClient({ post, token: async () => 'tok', clientId: 'stage-test', newKey: () => 'k1' });
  assert.equal((await c.decide(rec, 'allow_session', { argsSeen: false })).ok, false, 'never unseen');
  const result = await c.decide(rec, 'allow_session', { argsSeen: true });
  assert.deepEqual(result, { ok: true, allow: { status: 'allowed' } });
  assert.deepEqual(calls[0].body, { action: 'approve', client_id: 'stage-test', args_hash: rec.argsHash, allow_again: 'session', view_token: 'view-tk' });
  const none = approvalRecord(PENDING.requests[0]);
  assert.equal((await c.decide(none, 'allow_always', { argsSeen: true })).errorKey, 'approvals.error_stale_view');
});

test('approval: one POST with CSRF, args_hash, client_id, Idempotency-Key; never unseen', async () => {
  const { ApprovalClient } = await load('approval.js');
  const { approvalRecord } = await load('adapter.js');
  const rec = approvalRecord(PENDING.requests[0]);
  const { post, calls } = fakePost([{ status: 202, body: { status: 'accepted' } }]);
  let n = 0;
  const c = new ApprovalClient({ post, token: async () => 'tok', clientId: 'stage-test', newKey: () => `k${(n += 1)}` });
  assert.equal((await c.decide(rec, 'approve', { argsSeen: false })).ok, false);
  assert.equal((await c.decide(rec, 'approve', { argsSeen: true, expired: true })).ok, false);
  assert.equal(calls.length, 0);
  assert.deepEqual(await c.decide(rec, 'approve', { argsSeen: true }), { ok: true });
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, `/api/hitl/${rec.id}/decision`);
  assert.deepEqual(calls[0].body, { action: 'approve', client_id: 'stage-test', args_hash: rec.argsHash });
  assert.equal(calls[0].headers['X-CSRF-Token'], 'tok');
  assert.ok(calls[0].headers['Idempotency-Key']);
  assert.equal((await c.decide(rec, 'edit', { argsSeen: true })).ok, false);
});

test('approval: a retry after a network error reuses the key; server refusals are worded', async () => {
  const { ApprovalClient } = await load('approval.js');
  const { approvalRecord } = await load('adapter.js');
  const rec = approvalRecord(PENDING.requests[0]);
  const { post, calls } = fakePost([new Error('net'), { status: 409, body: { code: 'duplicate_request' } }, { status: 409, body: { code: 'binding_mismatch' } }, { status: 202, body: {} }]);
  let n = 0;
  const c = new ApprovalClient({ post, token: async () => 'tok', newKey: () => `k${(n += 1)}` });
  assert.equal((await c.decide(rec, 'reject')).errorKey, 'approvals.error_network');
  assert.equal((await c.decide(rec, 'reject')).errorKey, 'approvals.error_duplicate');
  assert.equal(calls[0].headers['Idempotency-Key'], calls[1].headers['Idempotency-Key']);
  assert.equal((await c.decide(rec, 'reject')).errorKey, 'approvals.error_binding_mismatch');
  await c.decide(rec, 'reject');
  assert.notEqual(calls[3].headers['Idempotency-Key'], calls[2].headers['Idempotency-Key']); // stale binding: new key
  // a different decision is a different key
  const d = fakePost([new Error('net'), { status: 202, body: {} }]);
  const c2 = new ApprovalClient({ post: d.post, token: async () => 'tok', newKey: () => `k${(n += 1)}` });
  await c2.decide(rec, 'reject');
  await c2.decide(rec, 'approve', { argsSeen: true });
  assert.notEqual(d.calls[0].headers['Idempotency-Key'], d.calls[1].headers['Idempotency-Key']);
});

test('approval: a host confirmation (ADR 0012 §8b) is named on the next press, then dropped', async () => {
  const { ApprovalClient, ERROR_KEYS } = await load('approval.js');
  const { approvalRecord } = await load('adapter.js');
  const rec = approvalRecord(PENDING.requests[0]);
  const OP = 'cd'.repeat(16);
  const host = { code: 'host_confirmation_required', operation: { id: OP, short: OP.slice(0, 8), code: '1234abcd' } };
  const { post, calls } = fakePost([
    { status: 428, body: host },
    { status: 409, body: { code: 'host_operation_pending' } },
    { status: 409, body: { code: 'host_operation_denied' } },
    { status: 409, body: { code: 'binding_mismatch' } },
  ]);
  let n = 0;
  const c = new ApprovalClient({ post, token: async () => 'tok', newKey: () => `k${(n += 1)}` });
  const first = await c.decide(rec, 'approve', { argsSeen: true });
  assert.deepEqual(first, { ok: false, errorKey: 'stage.host_confirm_then_retry',
    errorVars: { short: 'cdcdcdcd', code: '1234abcd' }, code: 'host_confirmation_required' });
  assert.equal(calls[0].headers['X-Stira-Host-Operation'], undefined);
  const second = await c.decide(rec, 'approve', { argsSeen: true });
  assert.equal(calls[1].headers['X-Stira-Host-Operation'], OP);
  assert.notEqual(calls[1].headers['Idempotency-Key'], calls[0].headers['Idempotency-Key']);
  assert.equal(second.errorKey, ERROR_KEYS.host_operation_pending); // kept: not confirmed yet
  const third = await c.decide(rec, 'approve', { argsSeen: true });
  assert.equal(calls[2].headers['X-Stira-Host-Operation'], OP);
  assert.equal(third.errorKey, 'host.code_host_operation_denied');
  await c.decide(rec, 'approve', { argsSeen: true });
  assert.equal(calls[3].headers['X-Stira-Host-Operation'], undefined); // dropped after the outcome
  // a 428 without a well-formed operation is not followed
  const bad = fakePost([{ status: 428, body: { code: 'host_confirmation_required', operation: { id: 'x' } } }, { status: 202, body: {} }]);
  const c2 = new ApprovalClient({ post: bad.post, token: async () => 'tok', newKey: () => `k${(n += 1)}` });
  assert.equal((await c2.decide(rec, 'approve', { argsSeen: true })).errorKey, 'host.code_host_confirmation_required');
  await c2.decide(rec, 'approve', { argsSeen: true });
  assert.equal(bad.calls[1].headers['X-Stira-Host-Operation'], undefined);
});

test('approval: the browser confirmation (ADR 0012 §8b rev 6) confirms in the dialog, then re-sends once', async () => {
  const { ApprovalClient } = await load('approval.js');
  const { approvalRecord } = await load('adapter.js');
  const rec = approvalRecord(PENDING.requests[0]);
  const OP = 'cd'.repeat(16);
  const host = { code: 'host_confirmation_required', operation: { id: OP, short: OP.slice(0, 8), code: '1234abcd', channel: 'browser' } };
  let n = 0;
  const seen = [];
  const ok = fakePost([{ status: 428, body: host }, { status: 202, body: {} }]);
  const c = new ApprovalClient({ post: ok.post, token: async () => 'tok', newKey: () => `k${(n += 1)}`,
    hostConfirm: async (operation) => { seen.push(operation.id); return 'confirmed'; } });
  assert.deepEqual(await c.decide(rec, 'approve', { argsSeen: true }), { ok: true });
  assert.deepEqual(seen, [OP]);
  assert.equal(ok.calls.length, 2);
  assert.equal(ok.calls[0].headers['X-Stira-Host-Operation'], undefined);
  assert.equal(ok.calls[1].headers['X-Stira-Host-Operation'], OP);
  assert.deepEqual(ok.calls[1].body, ok.calls[0].body);
  assert.notEqual(ok.calls[1].headers['Idempotency-Key'], ok.calls[0].headers['Idempotency-Key']);
  // cancel in the dialog: final, one POST only, the panel says it was cancelled
  const no = fakePost([{ status: 428, body: host }, { status: 202, body: {} }]);
  const c2 = new ApprovalClient({ post: no.post, token: async () => 'tok', newKey: () => `k${(n += 1)}`,
    hostConfirm: async () => 'cancelled' });
  const cancelled = await c2.decide(rec, 'approve', { argsSeen: true });
  assert.equal(cancelled.errorKey, 'host.code_host_operation_cancelled');
  assert.equal(no.calls.length, 1);
  // a refused confirmation keeps its code; the next press starts a new operation
  const bad = fakePost([{ status: 428, body: host }, { status: 428, body: host }]);
  const c3 = new ApprovalClient({ post: bad.post, token: async () => 'tok', newKey: () => `k${(n += 1)}`,
    hostConfirm: async () => ({ code: 'host_browser_refused' }) });
  assert.equal((await c3.decide(rec, 'approve', { argsSeen: true })).errorKey, 'host.code_host_browser_refused');
  await c3.decide(rec, 'approve', { argsSeen: true });
  assert.equal(bad.calls[1].headers['X-Stira-Host-Operation'], undefined);
});

test('approval reasons: translated only for a known code with its exact sentence', async () => {
  const { whyText } = await load('approval.js');
  const t = (k, v) => (v ? `${k}(${Object.values(v).join('|')})` : k);
  const rec = { breakLevel: 'normal', reasonCode: 'rule_ask', reason: 'Tool policy is Ask: every call of this tool needs a human approval.' };
  assert.equal(whyText(rec, t), 'approvals.why_level(shell.normal|approvals.reason_rule_ask)');
  assert.equal(whyText({ ...rec, reason: 'edited text' }, t), 'approvals.why_level(shell.normal|edited text)');
  // #279: a code the page cannot word is "other", never the bare code or the English sentence.
  assert.equal(whyText({ ...rec, reasonCode: 'future_code', reason: 'x' }, t),
    'approvals.why_level(shell.normal|approvals.reason_other_code(future_code))');
  assert.equal(whyText({ ...rec, reasonCode: 'not a code!', reason: 'x' }, t),
    'approvals.why_level(shell.normal|approvals.reason_other)');
  assert.equal(whyText({ ...rec, reasonCode: null, reason: 'plain sentence' }, t),
    'approvals.why_level(shell.normal|plain sentence)');
});

// --- i18n and the served page ---------------------------------------------------------------
test('i18n: every stage.* key used in the Stage sources exists in both catalogs', async () => {
  const ja = JSON.parse(fs.readFileSync(path.join(ROOT, 'dashboard/static/i18n/catalog.ja.json'), 'utf8'));
  const en = JSON.parse(fs.readFileSync(path.join(ROOT, 'dashboard/static/i18n/catalog.en.json'), 'utf8'));
  const used = new Set();
  for (const f of fs.readdirSync(STAGE)) {
    const src = fs.readFileSync(path.join(STAGE, f), 'utf8');
    for (const m of src.matchAll(/'((?:stage|approvals|shell|chat)\.[a-z0-9_]+)'/g)) used.add(m[1]);
  }
  const { buildStrings } = await load('i18n.js');
  const S = buildStrings({ ja, en }, 'en');
  for (const k of ['title', 'name', 'hint', 'raw_title', 'drift_ok', 'debug_title', 'breaker', 'none', 'chose', 'choice_note', 'raw_toggle', 'lang_toggle', 'autoplay']) used.add(`stage.${k}`);
  for (const k of used) {
    assert.ok(k in ja, `ja: ${k}`);
    assert.ok(k in en, `en: ${k}`);
  }
  assert.equal(S.steps.critical, en['stage.step_critical']);
  assert.equal(S.t('stage.say_tool', { name: 'x' }), 'Running a tool: x');
});

test('served template keeps the standalone page\'s scene DOM ids, no inline code', () => {
  const ids = (html) => [...html.matchAll(/\sid="([^"]+)"/g)].map((m) => m[1]).filter((id) => id !== 'i18n-catalog');
  const tpl = fs.readFileSync(path.join(ROOT, 'dashboard/templates/stage.html'), 'utf8');
  const page = fs.readFileSync(path.join(ROOT, 'dashboard/stage/index.html'), 'utf8');
  // #350: the served page sits in the dashboard shell, which owns the language switch and the
  // link to the chat; it adds the composer and the log to the same scene DOM.
  const shellOwned = new Set(['btn-lang', 'btn-exit']);
  for (const id of ids(page)) if (!shellOwned.has(id)) assert.ok(ids(tpl).includes(id), id);
  for (const id of ['stage-form', 'stage-input', 'stage-send', 'stage-stop', 'backlog', 'backlog-list', 'btn-log']) assert.ok(ids(tpl).includes(id), id);
  assert.match(tpl, /data-stage="live"/);
  assert.doesNotMatch(page, /data-stage=/);
  // the only <script> without src is the JSON catalog (data, not code)
  for (const m of tpl.matchAll(/<script(?![^>]*\bsrc=)([^>]*)>/g)) assert.match(m[1], /type="application\/json"/);
  assert.doesNotMatch(tpl, /\son[a-z]+=/i);
  assert.doesNotMatch(tpl, /\sstyle=/i);
});

test('live sources use relative same-origin URLs only, no credentials beyond the page', () => {
  for (const f of ['live.js', 'live_ui.js', 'approval.js', 'adapter.js']) {
    const src = fs.readFileSync(path.join(STAGE, f), 'utf8');
    assert.doesNotMatch(src, /https?:\/\//, f);
    assert.doesNotMatch(src, /credentials:\s*'include'|mode:\s*'cors'|EventSource\(/, f);
    for (const m of src.matchAll(/[`'](\/api\/[^`']*)[`']/g)) {
      assert.match(m[1], /^\/api\/(gateway\/status|turns|hitl|csrf)/, `${f}: ${m[1]}`);
    }
  }
});

test('#445: the Stage says "generating N s" from the model.* events of the open turn', async () => {
  const { generationFrom, generationSeconds, narrate } = await load('adapter.js');
  const id = 'c'.repeat(32);
  let g = generationFrom(null, { type: 'model.started', data: { call_id: id, started_at: 100 } }, 101);
  assert.deepEqual(g, { callId: id, since: 100 });
  g = generationFrom(g, { type: 'model.progress', data: { call_id: id, elapsed_ms: 30000 } }, 131);
  assert.equal(generationSeconds(g, 135), 34);
  const s = { approval: null, breaker: 'NORMAL', tools: [], thinking: true };
  assert.equal(narrate(s, { generatingSec: 3 }).key, 'stage.say_thinking');
  assert.deepEqual(narrate(s, { generatingSec: 34 }), { key: 'stage.say_generating', vars: { s: 30 } });
  assert.deepEqual(narrate(s, { generatingSec: 62 }), { key: 'stage.say_generating_long', vars: { s: 60 } });
  // a running tool, an approval or the breaker still come first
  assert.equal(narrate({ ...s, tools: [{ id: 't', name: 'file_write', kind: 'tool' }] }, { generatingSec: 62 }).key, 'stage.say_tool');
  assert.equal(generationFrom(g, { type: 'model.completed', data: { call_id: id } }, 140), null);
  assert.equal(generationSeconds(null, 140), null);
  // model.* events are not Stage events (the character does not react to them)
  const { fromTurnEvent } = await load('adapter.js');
  assert.deepEqual(fromTurnEvent({ type: 'model.progress', turn_id: 't', seq: 3, data: { call_id: id, elapsed_ms: 1 } }), []);
});
