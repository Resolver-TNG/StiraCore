// Offline in-chat tool card tests (#188): node --test tests/tool_cards_test.cjs
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { resolve } = require('node:path');
const { catalog } = require('./ui_shim.cjs');
const { byClass, settle, SUMMARY, ev, setup } = require('./chat_dom_mock.cjs');

const SCRIPTS = ['tool_cards.js', 'turn_ribbon.js', 'chat_core.js', 'chat.js'];
const cardsSource = readFileSync(resolve(__dirname, '../dashboard/static/js/tool_cards.js'), 'utf8');
const enCatalog = JSON.parse(readFileSync(
  resolve(__dirname, '../dashboard/static/i18n/catalog.en.json'), 'utf8'));

const created = (id, seq, use, extra = {}) => ev(id, seq, 'tool_call.created', {
  tool_use_id: use, name: 'current_time', status: 'running', duration_ms: null,
  input_preview: '{\n  "timezone": "Asia/Tokyo"\n}', input_chars: 30, input_lines: 3, input_truncated: false,
  output_preview: null, ...extra,
});
const updated = (id, seq, use, extra = {}) => ev(id, seq, 'tool_call.updated', {
  tool_use_id: use, name: 'current_time', status: 'done', duration_ms: 4.4,
  input_preview: null, output_preview: '2026-10-12T14:00:00+09:00', output_chars: 25, output_lines: 1,
  output_truncated: false, result_status: 'success', exception_type: null, ...extra,
});

const article = ctx => ctx.get('conversation').children.find(n => n.className === 'message agent');
const groupOf = ctx => article(ctx).find(byClass('tool-group'));
const cardsOf = ctx => groupOf(ctx).findAll(byClass('stira-card'));
const badge = card => card.find(byClass('badge'));

test('live tool call renders a card before the reply text and folds to "N calls · M failed"', async () => {
  const id = 'live0001';
  const ctx = setup({ scripts: SCRIPTS, sse: [
    ['ui_event', ev(id, 1, 'turn.started')],
    ['ui_event', created(id, 2, 'u1')],
    ['ui_event', updated(id, 3, 'u1')],
    ['delta', { text: 'It is 14:00.' }],
    ['ui_event', ev(id, 4, 'turn.summary', SUMMARY)],
    ['ui_event', ev(id, 5, 'turn.completed', { usage: { input_tokens: 1, output_tokens: 2 } })],
    ['turn', { turn_id: id, input_tokens: 1, output_tokens: 2 }],
  ] });
  await settle();
  ctx.get('message').value = 'what time is it?';
  await ctx.get('chat-form').listeners.submit[0]({ preventDefault() {} });
  const reply = article(ctx);
  const group = groupOf(ctx);
  assert.ok(group, 'tool group rendered');
  const order = reply.children.map(n => n.className);
  assert.ok(order.indexOf('tool-group') < order.indexOf('message-text'), order.join(','));
  assert.equal(group.find(byClass('tool-group-summary')).textContent, 'ツール 1 件・失敗 0');
  assert.equal(group.open, false, 'folded at turn completion');
  const [card] = cardsOf(ctx);
  assert.equal(card.tagName, 'details');
  assert.equal(badge(card).textContent, '成功');
  assert.equal(badge(card).dataset.state, 'ok');
  assert.equal(card.find(byClass('stira-card-title')).textContent, 'current_time');
  assert.equal(card.find(byClass('stira-card-brief')).textContent, '{ "timezone": "Asia/Tokyo" }');
  assert.equal(card.find(byClass('stira-card-meta')).textContent, '4 ms');
  const [input, output] = card.findAll(byClass('tool-payload-text')).map(n => n.textContent);
  assert.equal(input, '{\n  "timezone": "Asia/Tokyo"\n}', 'created input survives updated input_preview: null');
  assert.equal(output, '2026-10-12T14:00:00+09:00');
  assert.match(card.textContent, /終了状態success/);
});

test('every status is distinguished by text, including approval and unknown outcomes', async () => {
  const ctx = setup({ scripts: SCRIPTS });
  await settle();
  const { StiraCards } = ctx.context;
  const texts = Object.keys(StiraCards.STATUS).map(status =>
    StiraCards.card(ctx.doc, { title: 'x', status }).find(byClass('badge')).textContent);
  assert.deepEqual(texts, ['承認待ち', '実行中', '成功', '失敗', '拒否', '期限切れ', '状態不明']);
  assert.equal(new Set(texts).size, texts.length);
  assert.equal(StiraCards.card(ctx.doc, { title: 'x', status: 'weird' }).dataset.status, 'unknown');

  const cards = StiraCards.create({ document: ctx.doc });
  const tool = { tool_use_id: 'u1', name: 'memory_write', status: 'running' };
  const rec = { approvals: [{ request_id: 'r', tool_use_id: 'u1' }], completed: false };
  assert.equal(cards.effectiveStatus(rec, tool), 'pending');
  rec.approvals[0].decision = 'deny';
  assert.equal(cards.effectiveStatus(rec, tool), 'denied');
  assert.equal(cards.effectiveStatus({ approvals: [], completed: true }, tool), 'unknown');
  assert.equal(cards.effectiveStatus({ approvals: [], completed: false }, { status: 'timeout' }), 'timeout');
});

test('failures are counted and missing payloads show "not recorded"', async () => {
  const id = 'fail0001';
  const ctx = setup({ scripts: SCRIPTS, sse: [
    ['ui_event', ev(id, 1, 'turn.started')],
    ['ui_event', created(id, 2, 'a', { input_preview: null })],
    ['ui_event', updated(id, 3, 'a', { status: 'error', output_preview: null, result_status: 'error', result_code: 'redirect', exception_type: 'ValueError' })],
    ['ui_event', created(id, 4, 'b')],
    ['ui_event', updated(id, 5, 'b')],
    ['ui_event', ev(id, 6, 'turn.completed', {})],
    ['turn', { turn_id: id }],
  ] });
  await settle();
  ctx.get('message').value = 'x';
  await ctx.get('chat-form').listeners.submit[0]({ preventDefault() {} });
  const group = groupOf(ctx);
  assert.equal(group.find(byClass('tool-group-summary')).textContent, 'ツール 2 件・失敗 1');
  assert.equal(group.dataset.failed, '1');
  const [failed] = cardsOf(ctx);
  assert.equal(badge(failed).textContent, '失敗');
  assert.equal(failed.dataset.status, 'error');
  assert.match(failed.textContent, /error · redirect · ValueError/);
  const notes = failed.findAll(byClass('tool-payload-note')).map(n => n.textContent);
  assert.deepEqual(notes, ['未記録', '未記録']);
});

test('truncated output states the cut and pages more masked text on demand', async () => {
  const id = 'page0001';
  const lines = n => Array.from({ length: n }, (_, i) => `l${i}`).join('\n');
  const ctx = setup({ scripts: SCRIPTS,
    payloads: {
      'u1/output/0': { text: lines(200), next_offset: 200, total_lines: 250, retained_truncated: false },
      'u1/output/200': { text: lines(50), next_offset: null, total_lines: 250, retained_truncated: false },
    },
    sse: [
      ['ui_event', ev(id, 1, 'turn.started')],
      ['ui_event', created(id, 2, 'u1')],
      ['ui_event', updated(id, 3, 'u1', { output_preview: 'l0\nl1', output_truncated: true, output_stored: true, output_lines: 250, output_chars: 1200 })],
      ['ui_event', ev(id, 4, 'turn.completed', {})],
      ['turn', { turn_id: id }],
    ] });
  await settle();
  ctx.get('message').value = 'x';
  await ctx.get('chat-form').listeners.submit[0]({ preventDefault() {} });
  const [card] = cardsOf(ctx);
  const output = card.findAll(byClass('tool-payload')).find(n => n.dataset.kind === 'output');
  const note = output.find(byClass('tool-payload-note'));
  assert.equal(note.textContent, '先頭のみ表示（切り詰め）。記録: 全 250 行・1,200 文字');
  const more = output.find(byClass('tool-payload-more'));
  more.click();
  await settle();
  assert.ok(ctx.requests.includes(`/api/turns/${id}/tools/u1/output?offset=0`));
  assert.equal(output.find(byClass('tool-payload-text')).textContent.split('\n').length, 200);
  assert.match(note.textContent, /200 \/ 250 行を表示（秘匿値はマスク済み）/);
  assert.equal(more.textContent, 'さらに読み込む');
  more.click();
  await settle();
  assert.ok(ctx.requests.includes(`/api/turns/${id}/tools/u1/output?offset=200`));
  assert.equal(output.find(byClass('tool-payload-text')).textContent.split('\n').length, 250);
  assert.match(note.textContent, /記録分をすべて表示しました/);
  assert.equal(output.find(byClass('tool-payload-more')), null);
});

test('reload rebuilds past tool cards from replayed events (collapsed)', async () => {
  const id = 'past0001';
  const ctx = setup({ scripts: SCRIPTS,
    history: [{ message: 'q', response: 'a', turn_id: id }],
    turns: [{ turn_id: id, completed: true, tool_count: 1, summary: SUMMARY }],
    events: { [id]: [ev(id, 1, 'turn.started'), created(id, 2, 'u1'), updated(id, 3, 'u1'), ev(id, 4, 'turn.completed', {})] } });
  await settle(12);
  assert.ok(ctx.requests.includes(`/api/turns/${id}/events?after_seq=0`));
  const group = groupOf(ctx);
  assert.equal(group.open, false);
  assert.equal(group.find(byClass('tool-group-summary')).textContent, 'ツール 1 件・失敗 0');
  assert.equal(badge(cardsOf(ctx)[0]).textContent, '成功');
});

test('every key used by tool_cards.js exists in both catalogs', () => {
  const keys = new Set(cardsSource.match(/"(?:turns|cards)\.[a-z_]+"/g).map(k => k.slice(1, -1)));
  for (const key of keys) {
    assert.ok(Object.hasOwn(catalog, key), `ja missing ${key}`);
    assert.ok(Object.hasOwn(enCatalog, key), `en missing ${key}`);
  }
});

test('#601 E11: a status update keeps the input pages already loaded (same payload, same block)', async () => {
  const vm = require('node:vm');
  const { shimSource } = require('./ui_shim.cjs');
  const { Element } = require('./chat_dom_mock.cjs');
  const doc = { createElement: tag => new Element(doc, tag) };
  const context = { document: doc, JSON, Math, Date, String, Number, Array, Set, Map, Promise, Object };
  context.window = context;
  vm.runInNewContext(shimSource() + cardsSource, context);
  let release;
  const reads = [];
  const cards = context.StiraCards.create({ document: doc, fetchJSON: async (url) => {
    reads.push(url);
    await new Promise((r) => { release = r; });
    return { text: 'all\nof\nthe\ninput', next_offset: null, total_lines: 4 };
  } });
  const tool = { tool_use_id: 'u1', name: 'file_write', status: 'running', input_preview: 'all',
    input_truncated: true, input_stored: true, input_lines: 4, input_chars: 15, output_preview: null };
  const rec = { id: 'turn0001', tools: new Map([['u1', tool]]), approvals: [], completed: false };
  const reply = { article: new Element(doc), body: null };
  cards.renderTurn(rec, reply);
  const input = () => reply.article.find(byClass('stira-card')).findAll(byClass('tool-payload')).find(n => n.dataset.kind === 'input');
  const before = input();
  before.find(byClass('tool-payload-more')).click();
  await settle();
  // the status changes while the page is on the way, and once more after it landed
  rec.tools.set('u1', { ...tool, status: 'done', duration_ms: 3, output_preview: 'ok' });
  cards.renderTurn(rec, reply);
  release();
  await settle();
  assert.equal(input(), before, 'the same block is kept');
  assert.equal(input().find(byClass('tool-payload-text')).textContent, 'all\nof\nthe\ninput', 'the loaded text is on screen');
  rec.tools.set('u1', { ...tool, status: 'done', duration_ms: 4, output_preview: 'ok!' });
  cards.renderTurn(rec, reply);
  assert.equal(input().find(byClass('tool-payload-text')).textContent, 'all\nof\nthe\ninput');
  assert.equal(reply.article.find(byClass('stira-card')).dataset.status, 'done');
  assert.equal(reads.length, 1);
});
