// Approval cards, queue and history (#189): node --test tests/approvals_test.cjs
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { resolve } = require('node:path');
const vm = require('node:vm');
const { shimSource } = require('./ui_shim.cjs');
const { Element, byClass, settle } = require('./chat_dom_mock.cjs');

const read = name => readFileSync(resolve(__dirname, '../dashboard/static/js', name), 'utf8');
const NOW = 1_800_000_000;

const request = (id, extra = {}) => ({
  id, agent_id: 'stira', tool_name: 'memory_write', tool_input: { content: 'note', layer: 'episodic' },
  normalized_input: '{"content":"note","layer":"episodic"}', args_hash: `hash-${id}`.padEnd(64, '0'),
  break_level: 'critical', reason: 'CRITICAL HALT requires human approval for this write operation.',
  reason_code: 'breaker_critical_halt', tool_use_id: `use-${id}`, supersedes: null, cwd: null, turn_id: 'turn-abcdef123', created_at: NOW - 5,
  expires_at: NOW + 90, status: 'pending', ...extra,
});

function harness({ pending = [request('r1')], history = [], post } = {}) {
  const doc = {
    activeElement: null, events: [],
    createElement: tag => new Element(doc, tag),
    dispatchEvent(event) { this.events.push(event); return true; },
  };
  class CustomEvent { constructor(type, init) { this.type = type; this.detail = init?.detail; } }
  const context = { document: doc, CustomEvent, JSON, Math, Date, String, Number, Array, Set, Map, Promise,
    crypto: { randomUUID: (() => { let n = 0; return () => `uuid-${++n}`; })() } };
  context.window = context;
  vm.runInNewContext(shimSource() + read('tool_cards.js') + read('approvals.js'), context);
  const list = new Element(doc), panel = new Element(doc), live = new Element(doc), table = new Element(doc);
  const empty = new Element(doc), status = new Element(doc);
  panel.hidden = true;
  const state = { pending, history, now: NOW, posts: [], fail: null };
  const queue = context.StiraApprovals.create({
    document: doc, list, panel, live, history: table, empty, status, now: () => state.now, clientId: 'tab-me',
    getJSON: async url => {
      if (state.fail && url.startsWith(state.fail)) throw new Error('HTTP 503');
      if (state.hang && url.startsWith(state.hang)) return new Promise(() => {});
      return url.startsWith('/api/hitl/pending')
        ? { requests: state.pending, server_time: state.now }
        : { history: state.history, server_time: state.now };
    },
    post: async (url, body, key) => {
      state.posts.push({ url, body, key });
      if (post) return post(url, body, key, state);
      state.pending = state.pending.filter(item => !url.includes(item.id));
      return { ok: true, status: 202, json: async () => ({ status: 'accepted' }) };
    },
  });
  // refresh() no longer waits for the history (#369); the tests that look at the history let
  // it settle. rawRefresh is the real one.
  const rawRefresh = queue.refresh;
  queue.rawRefresh = rawRefresh;
  queue.refresh = async () => { const result = await rawRefresh(); await settle(); return result; };
  const cards = () => list.findAll(byClass('approval-card'));
  const buttons = card => Object.fromEntries(card.findAll(node => node.tagName === 'button')
    .map(node => [node.dataset.approvalAction, node]));
  return { doc, queue, list, panel, live, table, empty, status, state, cards, buttons };
}

const facts = card => Object.fromEntries(card.find(byClass('approval-facts')).children
  .map(row => [row.children[0].textContent, row.children[1].textContent]));

test('card shows target, cwd, normalized args, expiry and why; only once/reject as basic actions', async () => {
  const h = harness({ pending: [request('r1', { cwd: '/srv/app' })] });
  await h.queue.refresh();
  const [card] = h.cards();
  assert.equal(h.panel.hidden, false);
  assert.equal(card.open, true);
  assert.equal(card.dataset.requestId, 'r1');
  const f = facts(card);
  assert.equal(f['実行対象'], 'memory_write · stira');
  assert.equal(f['作業ディレクトリ (cwd)'], '/srv/app');
  // #279: the server's reason_code is worded from the catalog (ja UI shows Japanese).
  assert.equal(f['なぜ聞かれているか'], '危険: halt モードのため、書き込み・実行系の操作は 1 回ごとに人の承認が必要です');
  assert.equal(f['要求ID'], 'r1');
  assert.match(f['期限'], /\(残り 1:30\)$/);
  assert.equal(f['由来ターン'], 'turn-abc');
  assert.equal(card.find(byClass('approval-args')).textContent,
    '{\n  "content": "note",\n  "layer": "episodic"\n}');
  const basic = card.find(byClass('approval-actions')).children.map(node => node.textContent);
  assert.deepEqual(basic, ['今回のみ承認', '拒否']);
  // ADR 0025: the two "allow again" buttons are hidden where the server offers no view.
  const allowBox = card.find(byClass('approval-allow'));
  assert.equal(allowBox.hidden, true, 'no allow-again buttons without a server view');
  assert.equal(card.find(byClass('approval-allow-reason')).hidden, true);
  const shown = card.findAll(node => node.tagName === 'button' && !allowBox.findAll(n => n === node).length)
    .map(node => node.textContent).join(' ');
  assert.doesNotMatch(shown, /常に|セッション|always|session/i, 'no session/permanent grants shown without server support');
  const more = card.find(byClass('approval-more'));
  assert.equal(more.tagName, 'details');
  assert.deepEqual(more.findAll(node => node.tagName === 'button').map(n => n.dataset.approvalAction), ['edit', 'respond']);
  // Every control has a programmatic label (keyboard + screen reader).
  for (const area of card.findAll(node => node.tagName === 'textarea')) {
    const label = card.find(node => node.tagName === 'label' && node.getAttribute('for') === area.id);
    assert.ok(label && label.textContent, `label for ${area.id}`);
  }
  assert.equal(h.doc.events.at(-1).detail.count, 1, 'nav chip receives the same queue size');
  assert.match(h.live.textContent, /memory_write の承認要求が届きました/);
});

test('approve once quotes the displayed args hash with an idempotency key, then focuses the next card', async () => {
  const h = harness({ pending: [request('r1'), request('r2')] });
  await h.queue.refresh();
  const [first, second] = h.cards();
  h.buttons(first).approve.focus();
  h.buttons(first).approve.click();
  await settle();
  assert.equal(h.state.posts.length, 1);
  const [sent] = h.state.posts;
  assert.equal(sent.url, '/api/hitl/r1/decision');
  assert.deepEqual(JSON.parse(JSON.stringify(sent.body)), { action: 'approve', args_hash: request('r1').args_hash, client_id: 'tab-me' });
  assert.equal(sent.key, 'uuid-1');
  assert.deepEqual(h.cards().map(card => card.dataset.requestId), ['r2']);
  assert.equal(h.doc.activeElement, h.buttons(second).approve, 'keyboard focus moves to the next request');
  assert.match(h.live.textContent, /今回のみ承認しました/);
  assert.equal(h.doc.events.at(-1).detail.count, 1);
});

test('reject carries the operator reason; the last decision empties and hides the queue', async () => {
  const h = harness();
  await h.queue.refresh();
  const [card] = h.cards();
  card.findAll(node => node.tagName === 'textarea')[0].value = '  対象が違う  ';
  h.buttons(card).reject.click();
  await settle();
  assert.deepEqual(JSON.parse(JSON.stringify(h.state.posts[0].body)),
    { action: 'reject', args_hash: request('r1').args_hash, client_id: 'tab-me', reason: '対象が違う' });
  assert.equal(h.cards().length, 0);
  assert.equal(h.panel.hidden, true);
  assert.equal(h.doc.events.at(-1).detail.count, 0);
});

test('server refusals are explained and keep the card; network retries reuse the same key', async () => {
  const replies = [
    () => { throw new Error('offline'); },
    () => ({ ok: false, status: 409, json: async () => ({ code: 'already_decided', error: 'x' }) }),
  ];
  const h = harness({ post: () => replies.shift()() });
  await h.queue.refresh();
  const [card] = h.cards();
  h.buttons(card).approve.click();
  await settle();
  assert.match(card.find(byClass('approval-error')).textContent, /もう一度押すと同じ決定を再送/);
  h.buttons(card).approve.click();
  await settle();
  assert.equal(h.state.posts[0].key, h.state.posts[1].key, 'retransmission is deduplicated by the server');
  assert.match(card.find(byClass('approval-error')).textContent, /既に決定済み/);
  assert.equal(h.cards().length, 1, 'card stays until the server state says otherwise');
});

test('binding mismatch and expiry messages; an expired request cannot be approved but can be rejected', async () => {
  const h = harness({ post: () => ({ ok: false, status: 409, json: async () => ({ code: 'binding_mismatch' }) }) });
  await h.queue.refresh();
  const [card] = h.cards();
  h.buttons(card).approve.click();
  await settle();
  assert.match(card.find(byClass('approval-error')).textContent, /表示中の内容が古くなっています/);
  h.state.now = NOW + 91;
  h.queue.tick();
  const b = h.buttons(card);
  assert.equal(b.approve.disabled, true);
  assert.equal(b.edit.disabled, true);
  assert.equal(b.respond.disabled, true);
  assert.equal(b.reject.disabled, false);
  assert.equal(card.dataset.expired, 'true');
  assert.equal(card.find(byClass('badge')).textContent, '期限切れ');
  assert.match(card.find(byClass('approval-error')).textContent, /期限切れのため承認できません/);
});

test('edit validates JSON locally and is submitted for re-review, not executed', async () => {
  const h = harness();
  await h.queue.refresh();
  const [card] = h.cards();
  const editArea = card.findAll(node => node.tagName === 'textarea').find(node => node.id.startsWith('approval-edit-'));
  editArea.value = '{bad';
  h.buttons(card).edit.click();
  await settle();
  assert.equal(h.state.posts.length, 0);
  editArea.value = '{"content": "edited", "layer": "episodic"}';
  h.buttons(card).edit.click();
  await settle();
  const body = JSON.parse(JSON.stringify(h.state.posts[0].body));
  assert.deepEqual(body.edited_input, { content: 'edited', layer: 'episodic' });
  assert.equal(body.args_hash, request('r1').args_hash);
  // The server answers with a new request superseding the old one.
  h.state.pending = [request('r1b', { supersedes: 'r1', normalized_input: '{"content":"edited","layer":"episodic"}' })];
  await h.queue.refresh();
  const [fresh] = h.cards();
  assert.equal(fresh.dataset.requestId, 'r1b');
  assert.equal(facts(fresh)['再審査'], '編集された要求 r1 の置き換え');
});

test('state comes from the server: reload restores cards, resolution elsewhere removes them', async () => {
  const pending = [request('r1'), request('r2')];
  const a = harness({ pending });
  const b = harness({ pending });
  const c = harness({ pending: [request('w1', { break_level: 'warning', reason: 'Operator confirmation required.', reason_code: null })] });
  await c.queue.refresh();
  assert.equal(facts(c.cards()[0])['なぜ聞かれているか'], '警告: Operator confirmation required.');
  await a.queue.refresh();
  await b.queue.refresh();
  assert.deepEqual(a.cards().map(c => c.dataset.requestId), b.cards().map(c => c.dataset.requestId));
  b.state.pending = [request('r2')];
  await b.queue.refresh();
  assert.deepEqual(b.cards().map(c => c.dataset.requestId), ['r2']);
  assert.match(b.live.textContent, /別の場所で解決されました/);
});

test('history table lists decision, reason, origin turn, resolver and expiry', async () => {
  const history = [
    { id: 'h1', tool_name: 'memory_write', status: 'rejected', resolution_reason: '不要', turn_id: 'turn-0011223344',
      resolver: 'operator', client_id: 'tab-me', resolved_at: NOW - 10, expires_at: NOW + 60, args_hash: 'a'.repeat(64) },
    { id: 'h2', tool_name: 'memory_write', status: 'expired', resolution_reason: 'approval expired', turn_id: null,
      resolver: 'system', resolved_at: NOW - 20, expires_at: NOW - 20, args_hash: 'b'.repeat(64) },
    { id: 'h3', tool_name: 'memory_write', status: 'edited', superseded_by: 'h4abcdef99', resolver: 'operator',
      client_id: 'tab-other', resolved_at: NOW - 30, expires_at: NOW, args_hash: 'c'.repeat(64) },
  ];
  const h = harness({ pending: [], history });
  await h.queue.refresh();
  const rows = h.table.children.map(row => row.children.map(cell => cell.textContent));
  assert.equal(rows.length, 3);
  assert.deepEqual(rows[0].slice(1, 6), ['memory_write', '拒否', '不要', 'turn-001', '操作者（このタブ）']);
  assert.deepEqual(rows[1].slice(2, 6), ['期限切れ（拒否）', 'approval expired', '—', 'システム（無応答）']);
  assert.equal(rows[2][2], '編集→再審査 → h4abcdef');
  assert.equal(rows[2][5], '操作者');
  assert.equal(h.table.children[0].children[2].find(byClass('badge')).dataset.state, 'crit');
  const link = h.table.children[0].children[4].find(node => node.tagName === 'a');
  assert.equal(link.getAttribute('href'), '/chat?turn=turn-0011223344', 'origin turn opens the Turn Inspector');
  assert.equal(h.panel.hidden, true);
});

test('a request restored after a restart says approving resumes the turn; no respond, no masked edit (#240)', async () => {
  const h = harness({ pending: [
    request('s1', { restored: true, resumable: true, editable: true }),
    request('s2', { restored: true, resumable: false, editable: false }),
  ] });
  await h.queue.refresh();
  const [open, masked] = h.cards();
  assert.equal(facts(open)['復元'], '再起動前の要求です。承認すると中断したターンを再開します。');
  assert.equal(facts(masked)['復元'], '再起動前の要求です。チェックポイントがないため、承認しても再開はされません。');
  assert.equal(h.buttons(open).respond.hidden, true);
  assert.equal(h.buttons(open).edit.hidden, false);
  assert.equal(h.buttons(masked).edit.hidden, true);
  assert.equal(h.buttons(masked).approve.hidden, false);
  // A live request is unchanged.
  const live = harness();
  await live.queue.refresh();
  assert.equal(facts(live.cards()[0])['復元'], undefined);
  assert.notEqual(live.buttons(live.cards()[0]).respond.hidden, true);
});

test('a card held by the breaker cannot be approved or edited until it recovers; reject stays (ADR 0024)', async () => {
  const h = harness({ pending: [request('b1', { break_level: 'normal', breaker_hold: true })] });
  await h.queue.refresh();
  const [card] = h.cards();
  const hold = card.findAll(byClass('approval-hold'))[0];
  assert.equal(hold.hidden, false);
  assert.match(hold.textContent, /ブレーカー/);
  assert.equal(card.dataset.breakerHold, 'true');
  assert.equal(h.buttons(card).approve.disabled, true);
  assert.equal(h.buttons(card).edit.disabled, true);
  assert.equal(h.buttons(card).reject.disabled, false);
  assert.equal(h.buttons(card).respond.disabled, false);
  // Recovery: the next poll lifts the hold on the same card.
  h.state.pending = [request('b1', { break_level: 'normal', breaker_hold: false })];
  await h.queue.refresh();
  assert.equal(hold.hidden, true);
  assert.equal(h.buttons(card).approve.disabled, false);
});

test('a refused approval because of the breaker is explained with its catalog sentence (ADR 0024)', async () => {
  const h = harness({ post: async () => ({ ok: false, status: 409, json: async () => ({ code: 'breaker', error: 'raw' }) }) });
  await h.queue.refresh();
  const [card] = h.cards();
  h.buttons(card).approve.click();
  await settle();
  const error = card.findAll(byClass('approval-error'))[0];
  assert.match(error.textContent, /保留中/);
  assert.equal(h.cards().length, 1);
});

test('a restored card counts the other calls of its batch a resume answers from the ledger (ADR 0024 §5)', async () => {
  const h = harness({ pending: [
    request('p1', { restored: true, resumable: true, replay: { from_ledger: 2, unknown: 1 } }),
    request('p2', { restored: true, resumable: true, replay: { from_ledger: 0, unknown: 0 } }),
  ] });
  await h.queue.refresh();
  const [counted, quiet] = h.cards();
  const rows = counted.findAll(node => node.tagName === 'dd').map(node => node.textContent);
  assert.ok(rows.some(text => /2 件は実行済み/.test(text)), rows.join(' | '));
  assert.ok(rows.some(text => /1 件は実行済みの可能性/.test(text)), rows.join(' | '));
  assert.equal(facts(quiet)['再開時'], undefined);
});

test('a live request with masked secrets: approve/reject/respond stay, edit is hidden with a note (#185)', async () => {
  const h = harness({ pending: [request('m1', {
    tool_input: { url: 'https://***@example.test/' }, normalized_input: '{"url":"https://***@example.test/"}',
    editable: false, masked: true,
  })] });
  await h.queue.refresh();
  const [card] = h.cards();
  const b = h.buttons(card);
  assert.equal(b.edit.hidden, true);
  assert.equal(b.approve.hidden, false);
  assert.notEqual(b.respond.hidden, true);
  const notes = card.findAll(byClass('tool-payload-note')).map(n => n.textContent).join(' ');
  assert.match(notes, /秘密情報は伏せ字/);
  b.approve.click();
  await settle();
  assert.equal(h.state.posts[0].body.args_hash, 'hash-m1'.padEnd(64, '0'));
});

test('a replayed decision (duplicate_request) is explained, not shown as a raw error (#185)', async () => {
  const replies = [
    () => { throw new Error('offline'); },
    () => ({ ok: false, status: 409, json: async () => ({ code: 'duplicate_request', error: 'duplicate request' }) }),
  ];
  const h = harness({ post: () => replies.shift()() });
  await h.queue.refresh();
  const [card] = h.cards();
  h.buttons(card).approve.click();
  await settle();
  h.buttons(card).approve.click();
  await settle();
  assert.equal(h.state.posts.length, 2, 'only the operator presses re-send; nothing is automatic');
  assert.equal(h.state.posts[0].key, h.state.posts[1].key);
  assert.match(card.find(byClass('approval-error')).textContent, /同じ決定をすでに受け付けました/);
});

test('a failed load never says "no pending requests" and history stops saying "loading"', async () => {
  const h = harness({ pending: [], history: [] });
  h.state.fail = '/api/hitl/pending';
  await h.queue.refresh();
  assert.equal(h.empty.hidden, true, 'unknown is not the same as none pending');
  assert.equal(h.status.textContent, '承認要求の更新に失敗');
  assert.equal(h.table.children.length, 1);
  assert.equal(h.table.children[0].children[0].textContent, '決定履歴を読み込めませんでした（自動で再試行します）');
  // The next successful refresh restores the real state.
  h.state.fail = null;
  await h.queue.refresh();
  assert.equal(h.empty.hidden, false);
  assert.equal(h.status.textContent, '');
  assert.equal(h.table.children[0].children[0].textContent, 'まだ決定はありません');
  // A later failure keeps the last history instead of replacing it with the error row.
  h.state.fail = '/api/hitl/history';
  await h.queue.refresh();
  assert.equal(h.table.children[0].children[0].textContent, 'まだ決定はありません');
});

test('reason without a known reason_code falls back to the server sentence; the level always stays (#279)', async () => {
  const legacy = request('r1', { reason_code: undefined });
  const unknown = request('r2', { reason_code: 'something_new', break_level: 'warning', reason: 'Operator confirmation required.' });
  const ask = request('r3', { reason_code: 'rule_ask', break_level: 'normal', reason: 'Tool policy is Ask: every call of this tool needs a human approval.' });
  // Known code but a different sentence (edited / older server): the sentence is shown.
  const mismatch = request('r4', { reason_code: 'rule_ask', break_level: 'critical', reason: 'Edited elsewhere.' });
  const odd = request('r5', { reason_code: 'rule_ask', break_level: 'strange',
    reason: 'Tool policy is Ask: every call of this tool needs a human approval.' });
  const h = harness({ pending: [legacy, unknown, ask, mismatch, odd] });
  await h.queue.refresh();
  const [a, b, c, d, e] = h.cards().map(facts);
  assert.equal(a['なぜ聞かれているか'], '危険: CRITICAL HALT requires human approval for this write operation.');
  // #279: a code this page cannot word is "Other reason (code: ...)", never the bare code
  // and never the server's English sentence.
  assert.equal(b['なぜ聞かれているか'], '警告: その他の理由（コード: something_new）');
  assert.equal(c['なぜ聞かれているか'], '正常: このツールの許可は「毎回確認」です。呼び出しごとに人の承認が必要です');
  assert.equal(d['なぜ聞かれているか'], '危険: Edited elsewhere.');
  assert.equal(e['なぜ聞かれているか'], 'STRANGE: このツールの許可は「毎回確認」です。呼び出しごとに人の承認が必要です');
  // What is being approved stays exact: tool, request id, args hash.
  for (const [f, id] of [[a, 'r1'], [b, 'r2'], [c, 'r3'], [d, 'r4'], [e, 'r5']]) {
    assert.equal(f['実行対象'], 'memory_write · stira');
    assert.equal(f['要求ID'], id);
    assert.equal(f['引数ハッシュ'], `hash-${id}`.padEnd(64, '0').slice(0, 12));
  }
});

test('an unknown reason_code is said as "other"; a code-shaped value is shown only inside the sentence (#279)', async () => {
  const requests = [
    request('o1', { reason_code: 'future_reason', reason_params: { level: 'warning' }, reason: 'A new server sentence.' }),
    // Not shaped like a code (free text, a secret-looking value): no code at all in the text.
    request('o2', { reason_code: 'sk-' + 'a'.repeat(40) + ' extra words', break_level: 'critical', reason: 'x' }),
    request('o3', { reason_code: 'bad code!', break_level: 'critical', reason: 'x' }),
    request('o4', { reason_code: 'x'.repeat(80), break_level: 'critical', reason: 'x' }),
  ];
  const h = harness({ pending: requests });
  await h.queue.refresh();
  const [a, b, c, d] = h.cards().map(facts);
  assert.equal(a['なぜ聞かれているか'], '警告: その他の理由（コード: future_reason）');
  for (const f of [b, c, d]) assert.equal(f['なぜ聞かれているか'], '危険: その他の理由');
  assert.ok(!h.cards().map(card => JSON.stringify(facts(card))).join('').includes('sk-'));
});

test('reason_params.level words the breaker level before the older break_level (#279)', async () => {
  const h = harness({ pending: [
    request('p1', { break_level: 'critical', reason_params: { level: 'warning' } }),
    request('p2', { break_level: 'critical', reason_params: {} }),
    request('p3', { break_level: 'critical', reason_params: { level: 7 } }),
    request('p4', { break_level: 'critical', reason_params: null }),
  ] });
  await h.queue.refresh();
  const [a, b, c, d] = h.cards().map(facts);
  const why = f => f['なぜ聞かれているか'].split(':')[0];
  assert.deepEqual([a, b, c, d].map(why), ['警告', '危険', '危険', '危険']);
});

test('a known reason_code keeps the breaker level for every level, and the target/path/hash are rendered (#279 review)', async () => {
  const words = { normal: '正常', warning: '警告', critical: '危険', coherence: '一貫性低下' };
  const pending = Object.keys(words).flatMap(level => [
    request(`${level}-halt`, { break_level: level, tool_name: 'file_write',
      tool_input: { path: 'notes/todo.md', content: 'x' }, normalized_input: '{"content":"x","path":"notes/todo.md"}',
      cwd: '/srv/work' }),
    request(`${level}-ask`, { break_level: level.toUpperCase(), reason_code: 'rule_ask', tool_name: 'file_write',
      reason: 'Tool policy is Ask: every call of this tool needs a human approval.',
      tool_input: { path: 'notes/todo.md', content: 'x' }, normalized_input: '{"content":"x","path":"notes/todo.md"}',
      cwd: '/srv/work' }),
  ]);
  const h = harness({ pending });
  await h.queue.refresh();
  const cards = h.cards();
  assert.equal(cards.length, 8);
  for (const card of cards) {
    const id = card.dataset.requestId;
    const level = id.split('-')[0];
    const f = facts(card);
    assert.ok(f['なぜ聞かれているか'].startsWith(`${words[level]}: `), `${id}: ${f['なぜ聞かれているか']}`);
    assert.match(f['なぜ聞かれているか'], id.endsWith('halt') ? /halt モード/ : /毎回確認/);
    assert.equal(f['実行対象'], 'file_write · stira');
    assert.equal(f['作業ディレクトリ (cwd)'], '/srv/work');
    assert.equal(f['引数ハッシュ'], `hash-${id}`.padEnd(64, '0').slice(0, 12));
    assert.match(card.find(byClass('approval-args')).textContent, /"path": "notes\/todo.md"/);
  }
});

test('the client reason table matches the server constants (#279)', () => {
  const py = readFileSync(resolve(__dirname, '../src/stiracore/core/hitl_reasons.py'), 'utf8');
  const js = read('approvals.js');
  for (const name of ['CRITICAL_HALT', 'RULE_ASK']) {
    const sentence = new RegExp(`^${name} = "([^"]+)"`, 'm').exec(py)[1];
    assert.ok(js.includes(JSON.stringify(sentence)), `${name} sentence mirrored in approvals.js`);
  }
});

test('history words server closings by resolution_reason_code; an operator reason stays as typed (#279)', async () => {
  const row = (id, extra) => ({ id, tool_name: 'memory_write', status: 'expired', turn_id: null,
    resolved_at: NOW - 10, expires_at: NOW - 10, args_hash: id.repeat(64).slice(0, 64), ...extra });
  const history = [
    row('a', { resolution_reason: 'approval expired', resolution_reason_code: 'expired', resolver: 'system' }),
    row('b', { resolution_reason: 'expired', resolution_reason_code: 'expired', resolver: 'system' }),
    row('c', { status: 'cancelled', resolution_reason: 'tool rules changed (#190)', resolution_reason_code: 'rules_changed', resolver: 'system' }),
    row('d', { status: 'closed', resolution_reason: 'approval closed', resolution_reason_code: 'closed', resolver: 'system' }),
    // The operator typed the server's words: shown as typed.
    row('e', { status: 'rejected', resolution_reason: 'approval expired', resolution_reason_code: 'expired', resolver: 'operator' }),
    // A code whose sentence does not match (edited, older/newer server) is not trusted.
    row('f', { resolution_reason: 'approval expired soon', resolution_reason_code: 'expired', resolver: 'system' }),
    row('g', { resolution_reason: 'something new', resolution_reason_code: 'unknown_code', resolver: 'system' }),
    row('h', { status: 'rejected', resolution_reason: null, resolution_reason_code: null, resolver: 'operator' }),
  ];
  const h = harness({ pending: [], history });
  await h.queue.refresh();
  const reasons = h.table.children.map(r => r.children[3].textContent);
  assert.deepEqual(reasons, [
    '期限切れ', '期限切れ', 'ツールのルール変更で取り消し', 'サーバーの停止で終了',
    'approval expired', 'approval expired soon', 'something new', '—',
  ]);
});

test('the client resolution table matches the server constants (#279)', () => {
  const py = readFileSync(resolve(__dirname, '../src/stiracore/core/hitl_reasons.py'), 'utf8');
  const js = read('approvals.js');
  const block = /_RESOLUTION_CODES = \{([\s\S]*?)\n\}/.exec(py)[1];
  const pairs = [...block.matchAll(/^\s+(\w+): "(\w+)",$/gm)].map(m => [m[1], m[2]]);
  assert.ok(pairs.length >= 7, 'server codes found');
  for (const [name, code] of pairs) {
    const sentence = new RegExp(`^${name} = "([^"]+)"`, 'm').exec(py)[1];
    assert.match(js, new RegExp(`${code}: \\[[^\\]]*${JSON.stringify(sentence).replace(/[()#]/g, '\\$&')}`),
      `${name} -> ${code} mirrored in approvals.js`);
    assert.ok(js.includes(`${code}: "approvals.resolution_${code}"`), `${code} has a catalog key`);
  }
});

test('a busy decision (503, #350) keeps the card with its catalog sentence; the retry reuses the key', async () => {
  const catalog = JSON.parse(readFileSync(resolve(__dirname, '../dashboard/static/i18n/catalog.ja.json'), 'utf8'));
  const en = JSON.parse(readFileSync(resolve(__dirname, '../dashboard/static/i18n/catalog.en.json'), 'utf8'));
  assert.ok(catalog['approvals.error_busy'] && en['approvals.error_busy']);
  const replies = [
    () => ({ ok: false, status: 503, json: async () => ({ code: 'busy', error: 'a new conversation is being started' }) }),
    (url, _b, _k, state) => {
      state.pending = state.pending.filter(item => !url.includes(item.id));
      return { ok: true, status: 202, json: async () => ({ status: 'accepted' }) };
    },
  ];
  const h = harness({ post: (url, body, key, state) => replies.shift()(url, body, key, state) });
  await h.queue.refresh();
  const [card] = h.cards();
  h.buttons(card).approve.click();
  await settle();
  assert.equal(card.find(byClass('approval-error')).textContent, catalog['approvals.error_busy']);
  assert.equal(h.cards().length, 1, 'the card stays: nothing was recorded');
  h.buttons(card).approve.click();
  await settle();
  assert.equal(h.state.posts.length, 2);
  assert.equal(h.state.posts[0].key, h.state.posts[1].key, 'the same key: the server runs it once');
  assert.equal(h.cards().length, 0);
  // the Stage's approval window maps the same code
  const stage = readFileSync(resolve(__dirname, '../dashboard/stage/js/approval.js'), 'utf8');
  assert.match(stage, /busy: 'approvals\.error_busy'/);
});

// --- #369: what refresh() tells the poller, and the history is not re-read on every poll ------
test('refresh resolves to whether the queue needs another quick look (#369)', async () => {
  const h = harness({ pending: [] });
  assert.equal(await h.queue.refresh(), true, 'first read: the queue is new');
  assert.equal(await h.queue.refresh(), false, 'empty and unchanged: the poller may widen');
  h.state.pending = [request('r1')];
  assert.equal(await h.queue.refresh(), true, 'a request arrived');
  assert.equal(await h.queue.refresh(), true, 'a waiting request keeps the poll short');
  h.state.pending = [];
  assert.equal(await h.queue.refresh(), true, 'it left the queue (decided / expired)');
  assert.equal(await h.queue.refresh(), false);
});

test('a failed read resolves to null (the poller backs off) and says so (#369)', async () => {
  const h = harness();
  h.state.fail = '/api/hitl/pending';
  assert.equal(await h.queue.refresh(), null);
  assert.match(h.status.textContent, /.+/);
  h.state.fail = null;
  assert.notEqual(await h.queue.refresh(), null);
  assert.equal(h.status.textContent, '');
});

test('the history is read when the queue changed or is old, not on every poll (#369)', async () => {
  const h = harness({ pending: [] });
  let reads = 0;
  const past = h.state.history;
  Object.defineProperty(h.state, 'history', { get() { reads += 1; return past; } });
  const realNow = Date.now;
  let t = 1_000_000;
  Date.now = () => t; // the vm context shares this Date
  try {
    await h.queue.refresh();
    assert.equal(reads, 1, 'first read loads it');
    await h.queue.refresh();
    await h.queue.refresh();
    assert.equal(reads, 1, 'unchanged queue: the history is not fetched again');
    h.state.pending = [request('r1')];
    await h.queue.refresh();
    assert.equal(reads, 2, 'the queue changed: re-read');
    await h.queue.refresh();
    assert.equal(reads, 2);
    t += 31_000;
    await h.queue.refresh();
    assert.equal(reads, 3, 'and at least every 30 s');
  } finally {
    Date.now = realNow;
  }
});

test('a failing history is not a failure of the queue: refresh still resolves a boolean (#369)', async () => {
  const h = harness({ pending: [request('r1')] });
  h.state.fail = '/api/hitl/history';
  const result = await h.queue.refresh();
  assert.equal(typeof result, 'boolean');
  assert.equal(h.cards().length, 1, 'the card is shown');
  assert.equal(h.table.children[0].children[0].textContent, '決定履歴を読み込めませんでした（自動で再試行します）');
});

test('refresh does not wait for the history: a hanging history cannot hold the next poll (#369)', async () => {
  const h = harness({ pending: [request('r1')] });
  h.state.hang = '/api/hitl/history';
  const result = await Promise.race([h.queue.rawRefresh(), new Promise(r => setTimeout(() => r('stuck'), 200))]);
  assert.notEqual(result, 'stuck', 'refresh resolved although the history never answered');
  assert.equal(h.cards().length, 1);
  // and the next refresh is not blocked either (the history read is guarded on its own)
  const again = await Promise.race([h.queue.rawRefresh(), new Promise(r => setTimeout(() => r('stuck'), 200))]);
  assert.notEqual(again, 'stuck');
});

test('a queue without a history table (chat) never reads the history (#369)', async () => {
  const urls = [];
  // the chat page creates the queue without `history`: no history request at all
  const { Element: El } = require('./chat_dom_mock.cjs');
  const ctxDoc = { activeElement: null, createElement: tag => new El(ctxDoc, tag), dispatchEvent() { return true; } };
  const c = { document: ctxDoc, JSON, Math, Date, String, Number, Array, Set, Map, Promise, crypto: { randomUUID: () => 'u' } };
  c.window = c;
  vm.runInNewContext(shimSource() + read('tool_cards.js') + read('approvals.js'), c);
  const q = c.StiraApprovals.create({
    document: ctxDoc, list: new El(ctxDoc), panel: new El(ctxDoc), live: new El(ctxDoc), now: () => NOW,
    getJSON: async url => { urls.push(url); return { requests: [request('r1')], server_time: NOW }; },
  });
  await q.refresh();
  await settle();
  assert.deepEqual(urls, ['/api/hitl/pending']);
});

test('a queue read that never answers times out: later refreshes are not skipped (#601 E1)', async () => {
  const timers = [];
  const ctxDoc = { activeElement: null, createElement: tag => new Element(ctxDoc, tag), dispatchEvent() { return true; } };
  const c = { document: ctxDoc, JSON, Math, Date, String, Number, Array, Set, Map, Promise, crypto: { randomUUID: () => 'u' },
    setTimeout: (fn, ms) => { timers.push({ fn, ms }); return timers.length; }, clearTimeout: () => {} };
  c.window = c;
  vm.runInNewContext(shimSource() + read('tool_cards.js') + read('approvals.js'), c);
  let hang = true;
  const status = new Element(ctxDoc);
  const q = c.StiraApprovals.create({
    document: ctxDoc, list: new Element(ctxDoc), panel: new Element(ctxDoc), live: new Element(ctxDoc), status, now: () => NOW,
    getJSON: async () => (hang ? new Promise(() => {}) : { requests: [request('r1')], server_time: NOW }),
  });
  const first = q.refresh();
  await settle();
  const limit = timers.find(t => t.ms === 10000);
  assert.ok(limit, 'the queue read has a time limit');
  limit.fn();
  assert.equal(await first, null, 'a read that timed out is a failure (the pollers back off)');
  hang = false;
  const next = await q.refresh();
  assert.equal(next, true, 'the next refresh reads the queue again');
});
