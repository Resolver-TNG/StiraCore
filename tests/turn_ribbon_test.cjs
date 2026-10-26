// Offline turn ribbon + Inspector tests (#187): node --test tests/turn_ribbon_test.cjs
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { resolve } = require('node:path');
const { catalog } = require('./ui_shim.cjs');
const { byClass, settle, SUMMARY, ev, setup } = require('./chat_dom_mock.cjs');

const ribbonSource = readFileSync(resolve(__dirname, '../dashboard/static/js/turn_ribbon.js'), 'utf8');
const enCatalog = JSON.parse(readFileSync(
  resolve(__dirname, '../dashboard/static/i18n/catalog.en.json'), 'utf8'));

test('live ui_event stream renders a ribbon with glyphs, confidence and breaker', async () => {
  const id = 'aaaa1111';
  const ctx = setup({ sse: [
    ['ui_event', ev(id, 1, 'turn.started', { prompt_chars: 4 })],
    ['delta', { text: 'ok' }],
    ['ui_event', ev(id, 2, 'turn.summary', SUMMARY)],
    ['ui_event', ev(id, 3, 'turn.completed', { usage: { input_tokens: 12, output_tokens: 3 }, error: false })],
    ['turn', { turn_id: id, input_tokens: 12, output_tokens: 3, turn_summary: { category: 'threat', deltas: SUMMARY.deltas, breaker: 'normal' } }],
  ] });
  await settle();
  ctx.get('message').value = 'the tool failed';
  await ctx.get('chat-form').listeners.submit[0]({ preventDefault() {} });
  const [ribbon] = ctx.ribbons();
  assert.ok(ribbon, 'ribbon rendered');
  assert.equal(ribbon.tagName, 'button');
  assert.equal(ribbon.dataset.turnId, id);
  assert.equal(ribbon.parent.classList.contains('has-ribbon'), true);
  const dims = ribbon.findAll(byClass('ribbon-dim'));
  assert.deepEqual(dims.map(d => d.textContent), [
    '信頼 ・0', '好奇心 ・0', '警戒 ▲+0.08', '愛着 ・0', '安定 ▼−0.02', '満足 ・0',
  ]);
  assert.deepEqual(dims.map(d => d.dataset.trend), ['flat', 'flat', 'up', 'flat', 'down', 'flat']);
  assert.equal(ribbon.find(byClass('ribbon-stimulus')).textContent, '⚡ threat');
  assert.equal(ribbon.find(byClass('ribbon-confidence')).textContent, '分類信頼度 0.82');
  assert.equal(ribbon.find(byClass('ribbon-breaker')).textContent, '🟢 正常');
  assert.equal(ribbon.find(byClass('ribbon-breaker')).dataset.level, 'normal');
  const label = ribbon.attributes['aria-label'];
  assert.match(label, /刺激: threat/);
  assert.match(label, /分類信頼度: 0\.82/);
  assert.match(label, /ブレーカー: 正常/);
  assert.match(label, /警戒 \+0\.08/);
  assert.match(label, /信頼 変化なし/);
  assert.equal(ribbon.attributes['aria-controls'], 'turn-inspector');
});

test('missing data shows "not recorded" and is never zero-filled', async () => {
  const ctx = setup({ history: [{ message: 'hi', response: 'yo' }] });
  await settle();
  const [ribbon] = ctx.ribbons();
  assert.equal(ribbon.dataset.state, 'missing');
  assert.equal(ribbon.find(byClass('ribbon-stimulus')).textContent, '未記録');
  assert.equal(ribbon.find(byClass('ribbon-confidence')).textContent, '分類信頼度 未記録');
  assert.equal(ribbon.find(byClass('ribbon-breaker')).textContent, '⚪ 未記録');
  for (const dim of ribbon.findAll(byClass('ribbon-dim'))) {
    assert.equal(dim.dataset.trend, 'missing');
    assert.match(dim.textContent, /\?未記録$/);
    assert.doesNotMatch(dim.textContent, /0/);
  }
  ribbon.click();
  await settle();
  const pretty = ctx.get('inspector-pretty');
  const cells = pretty.findAll(node => node.tagName === 'td');
  assert.equal(cells.length, 6 * 4);
  assert.ok(cells.every(cell => cell.textContent === '未記録'), 'no zero-filled cells');
});

test('a turn without a change reads "no change" (the reason is on the Gateway card, #367)', async () => {
  // The summary does not carry why nothing moved (abstain, classifier error, unknown
  // stimulus, ...), so the ribbon states only the fact; it never asserts "no stimulus".
  const ctx = setup({ history: [{ message: 'ok', response: 'ok', turn_id: 'abst', turn_summary: { category: null, deltas: {}, emotion_transcription: null, breaker: 'normal' } }] });
  await settle();
  const [ribbon] = ctx.ribbons();
  assert.equal(ribbon.find(byClass('ribbon-stimulus')).textContent, '変化なし');
  assert.ok(ribbon.findAll(byClass('ribbon-dim')).every(d => d.dataset.trend === 'flat'));
});

test('inspector opens from the ribbon, keeps turn id in URL, toggles JSON and returns focus on Esc', async () => {
  const id = 'bbbb2222';
  const history = [{ message: 'q', response: 'a', turn_id: id, turn_summary: { category: 'threat', deltas: SUMMARY.deltas, breaker: 'normal' } }];
  const events = { [id]: [
    ev(id, 1, 'turn.started'), ev(id, 2, 'tool_call.created', { tool_use_id: 'u1', name: 'current_time', status: 'running' }),
    ev(id, 3, 'tool_call.updated', { tool_use_id: 'u1', name: 'current_time', status: 'done', duration_ms: 4.2 }),
    ev(id, 4, 'turn.summary', SUMMARY), ev(id, 5, 'breaker.transition', { from: 'warning', to: 'normal' }),
    ev(id, 6, 'turn.completed', { usage: { input_tokens: 1200, output_tokens: 34 }, error: false }),
  ] };
  const ctx = setup({ history, events, turns: [{ turn_id: id, summary: SUMMARY, started_at: 1760246401, ended_at: 1760246406, completed: true }],
    status: { neural_state: { ...SUMMARY.after, stability: 0.69 }, breaker: { level: 'normal' } } });
  await settle();
  const [ribbon] = ctx.ribbons();
  ribbon.focus();
  ribbon.click();
  await settle();
  const drawer = ctx.get('turn-inspector');
  assert.equal(drawer.hidden, false);
  assert.equal(ribbon.attributes['aria-expanded'], 'true');
  assert.equal(ctx.location.search, `?turn=${id}`);
  assert.equal(ctx.doc.activeElement, ctx.get('inspector-close'));
  assert.ok(ctx.requests.includes(`/api/turns/${id}/events?after_seq=0`));

  const pretty = ctx.get('inspector-pretty');
  const rows = pretty.find(node => node.tagName === 'tbody').children.map(row => row.children.map(c => c.textContent));
  assert.deepEqual(rows[4], ['安定', '0.700', '0.680', '−0.02', '0.690']);
  assert.deepEqual(rows[2], ['警戒', '0.100', '0.180', '+0.08', '0.180']);
  const text = pretty.textContent;
  assert.match(text, /current_time — 成功 · 4 ms/);
  assert.match(text, /🟡 警告 → 🟢 正常/);
  assert.match(text, /1,200/);
  assert.match(ctx.get('inspector-meta').textContent, /最新ターン/);
  assert.equal(ctx.get('inspector-json').hidden, true);

  ctx.get('inspector-view-json').click();
  assert.equal(pretty.hidden, true);
  assert.equal(ctx.get('inspector-json').hidden, false);
  assert.equal(ctx.get('inspector-view-json').attributes['aria-pressed'], 'true');
  const raw = JSON.parse(ctx.get('inspector-json').textContent);
  assert.equal(raw.turn_id, id);
  assert.equal(raw.events.length, 6);
  ctx.get('inspector-view-pretty').click();
  assert.equal(pretty.hidden, false);

  ctx.press('Escape');
  assert.equal(drawer.hidden, true);
  assert.equal(ctx.doc.activeElement, ribbon);
  assert.equal(ribbon.attributes['aria-expanded'], 'false');
  assert.equal(ctx.location.search, '');
});

test('inspector shows prompt-cache read/write/hit rate only when the provider reported them (#254)', async () => {
  const cached = 'dddd0001', plain = 'dddd0002';
  const history = [
    { message: 'a', response: 'b', turn_id: cached, turn_summary: { category: 'threat', deltas: SUMMARY.deltas, breaker: 'normal' } },
    { message: 'c', response: 'd', turn_id: plain, turn_summary: { category: 'threat', deltas: SUMMARY.deltas, breaker: 'normal' } },
  ];
  const events = {
    [cached]: [ev(cached, 1, 'turn.started'), ev(cached, 2, 'turn.completed', {
      usage: { input_tokens: 559, output_tokens: 20, cache_read_tokens: 10829, cache_write_tokens: 796, cache_hit_ratio: 0.8893 }, error: false })],
    [plain]: [ev(plain, 1, 'turn.started'), ev(plain, 2, 'turn.completed', { usage: { input_tokens: 9, output_tokens: 2 }, error: false })],
  };
  const ctx = setup({ history, events, search: `?turn=${cached}` });
  await settle();
  const text = ctx.get('inspector-pretty').textContent;
  assert.match(text, /キャッシュ読み取り/);
  assert.match(text, /10,829/);
  assert.match(text, /796/);
  assert.match(text, /88\.9%/);
  ctx.press('Escape');
  ctx.ribbons()[1].click();
  await settle();
  assert.doesNotMatch(ctx.get('inspector-pretty').textContent, /キャッシュ/);
});

test('reload restores ribbons from /api/turns and reopens the selected past turn from the URL', async () => {
  const old = 'cccc0001', latest = 'cccc0002';
  const history = [
    { message: 'a', response: 'b', turn_id: old, turn_summary: { category: 'threat', deltas: SUMMARY.deltas, breaker: 'normal' } },
    { message: 'c', response: 'd', turn_id: latest, turn_summary: { category: null, deltas: {}, breaker: 'normal' } },
  ];
  const ctx = setup({ history, search: `?turn=${old}`,
    turns: [
      { turn_id: latest, summary: { category: null, deltas: {}, breaker: 'normal' }, completed: true, ended_at: 1760246500 },
      { turn_id: old, summary: SUMMARY, completed: true, started_at: 1760246401, ended_at: 1760246406, usage: { input_tokens: 5, output_tokens: 6 } },
    ],
    events: { [old]: [ev(old, 1, 'turn.started'), ev(old, 2, 'turn.summary', SUMMARY), ev(old, 3, 'turn.completed', { usage: { input_tokens: 5, output_tokens: 6 } })] } });
  await settle(10);
  const [first] = ctx.ribbons();
  assert.equal(first.find(byClass('ribbon-confidence')).textContent, '分類信頼度 0.82', 'confidence restored from /api/turns');
  assert.ok(ctx.requests.includes('/api/turns?limit=200'));
  assert.equal(ctx.get('turn-inspector').hidden, false, 'URL-selected turn reopened');
  const meta = ctx.get('inspector-meta').textContent;
  assert.match(meta, /過去/);
  assert.match(meta, /記録時刻 /);
  assert.match(meta, new RegExp(old));
  ctx.press('Escape');
  assert.equal(ctx.doc.activeElement, ctx.get('conversation'));
});

test('200-turn history renders with ribbons in under 1 second', async () => {
  const history = [], turns = [];
  for (let i = 0; i < 200; i++) {
    const id = `t${String(i).padStart(4, '0')}`;
    history.push({ message: `m${i}`, response: `r${i}`.repeat(20), turn_id: id, input_tokens: 10, output_tokens: 20,
      turn_summary: { category: 'joy', deltas: { trust: 0.01, satisfaction: 0.02 }, breaker: 'normal' } });
    turns.push({ turn_id: id, summary: SUMMARY, completed: true, ended_at: 1760246400 + i });
  }
  const started = performance.now();
  const ctx = setup({ history, turns });
  await settle(10);
  const elapsed = performance.now() - started;
  assert.equal(ctx.ribbons().length, 200);
  assert.equal(ctx.ribbons()[199].find(byClass('ribbon-confidence')).textContent, '分類信頼度 0.82');
  console.log(`# 200-turn initial render: ${elapsed.toFixed(1)} ms`);
  assert.ok(elapsed < 1000, `took ${elapsed} ms`);
});

test('every turns.* key used by turn_ribbon.js exists in both catalogs', () => {
  const keys = new Set(ribbonSource.match(/"turns\.[a-z_]+"|`turns\.[a-z_]+`/g).map(k => k.slice(1, -1)));
  for (const key of ['chat.trust', 'shell.normal', 'shell.warning', 'shell.critical', 'chat.input_tokens']) keys.add(key);
  for (const key of keys) {
    assert.ok(Object.hasOwn(catalog, key), `ja missing ${key}`);
    assert.ok(Object.hasOwn(enCatalog, key), `en missing ${key}`);
  }
});

test('coherence breaker is shown as 一貫性低下, not 不明/未記録 (#310)', async () => {
  const id = 'cccc3333';
  const summary = { ...SUMMARY, breaker: 'coherence' };
  const history = [{ message: 'q', response: 'a', turn_id: id, turn_summary: summary }];
  const events = { [id]: [
    ev(id, 1, 'turn.started'), ev(id, 2, 'turn.summary', summary),
    ev(id, 3, 'breaker.transition', { from: 'normal', to: 'coherence' }),
    ev(id, 4, 'turn.completed', { usage: { input_tokens: 1, output_tokens: 1 }, error: false }),
  ] };
  const ctx = setup({ history, events, turns: [{ turn_id: id, summary, completed: true }],
    status: { neural_state: SUMMARY.after, breaker: { level: 'coherence' } } });
  await settle();
  for (const name of ['breaker-indicator', 'breaker-level']) {
    assert.equal(ctx.get(name).textContent, '🟣 一貫性低下');
    assert.equal(ctx.get(name).dataset.level, 'coherence');
  }
  const [ribbon] = ctx.ribbons();
  const cell = ribbon.find(byClass('ribbon-breaker'));
  assert.equal(cell.textContent, '🟣 一貫性低下');
  assert.equal(cell.dataset.level, 'coherence');
  assert.match(ribbon.attributes['aria-label'], /一貫性低下/);
  ribbon.click();
  await settle();
  assert.match(ctx.get('inspector-pretty').textContent, /🟢 正常 → 🟣 一貫性低下/);
  assert.equal(enCatalog['shell.coherence'], 'Coherence');
});

test('an unknown breaker level still falls back to 不明 (#310 keeps the fallback)', async () => {
  const ctx = setup({ status: { neural_state: SUMMARY.after, breaker: { level: 'strange' } } });
  await settle();
  assert.equal(ctx.get('breaker-level').textContent, '⚪ 不明');
  assert.equal(ctx.get('breaker-level').dataset.level, 'unknown');
});

test('#455: the inspector words the breaker reasons of a transition (never the code)', async () => {
  const id = 'eeee5555';
  const history = [{ message: 'q', response: 'a', turn_id: id, turn_summary: { category: 'loss', deltas: SUMMARY.deltas, breaker: 'warning' } }];
  const events = { [id]: [
    ev(id, 1, 'turn.started'), ev(id, 2, 'turn.summary', SUMMARY),
    ev(id, 3, 'breaker.transition', { from: 'normal', to: 'warning', reasons: ['stability_below_warning'],
      inputs: { stability: 0.31, stability_warning: 0.35 } }),
    ev(id, 4, 'turn.completed', { usage: { input_tokens: 1, output_tokens: 1 }, error: false }),
  ] };
  const ctx = setup({ history, events, turns: [{ turn_id: id, summary: SUMMARY, completed: true }],
    status: { neural_state: SUMMARY.after, breaker: { level: 'warning', reasons: ['stability_below_warning'],
      inputs: { stability: 0.31, stability_warning: 0.35 } } },
    scripts: ['breaker_reasons.js', 'turn_ribbon.js', 'chat_core.js', 'chat.js'] });
  await settle();
  const [ribbon] = ctx.ribbons();
  ribbon.click();
  await settle();
  const text = ctx.get('inspector-pretty').textContent;
  assert.match(text, /🟢 正常 → 🟡 警告 — 安定度 0\.31 < 0\.35/);
  assert.doesNotMatch(text, /stability_below_warning/);
  assert.equal(ctx.get('breaker-reason').textContent, '安定度 0.31 < 0.35');
});

test('#601 E12: an older event that arrives after a newer one does not roll the tool state back', async () => {
  const vm = require('node:vm');
  const { shimSource } = require('./ui_shim.cjs');
  const { Element } = require('./chat_dom_mock.cjs');
  const doc = { nodes: new Map(), getElementById(id) { if (!this.nodes.has(id)) this.nodes.set(id, new Element(this)); return this.nodes.get(id); },
    createElement: tag => new Element(doc, tag), addEventListener() {}, querySelectorAll: () => [] };
  const context = { document: doc, JSON, Math, Date, String, Number, Array, Set, Map, Promise, Object, WeakMap };
  context.window = context;
  vm.runInNewContext(shimSource() + ribbonSource, context);
  const turns = context.StiraTurns.create({ document: doc, fetchJSON: async () => ({}) });
  const id = 'ffff6666';
  const tool = (status, extra = {}) => ({ tool_use_id: 'u1', name: 'file_write', status, input_preview: null, ...extra });
  // live: the newer events first (the history read is still on the way)
  turns.ingest(ev(id, 4, 'tool_call.updated', tool('done', { duration_ms: 12 })));
  turns.ingest(ev(id, 6, 'breaker.transition', { from: 'warning', to: 'normal' }));
  // the history then delivers the older ones
  turns.ingest(ev(id, 2, 'tool_call.created', tool('running', { input_preview: '{"path":"a"}' })));
  turns.ingest(ev(id, 3, 'breaker.transition', { from: 'normal', to: 'warning' }));
  const rec = turns.records.get(id);
  assert.deepEqual(Array.from(rec.events, e => e.seq), [2, 3, 4, 6]);
  const merged = rec.tools.get('u1');
  assert.equal(merged.status, 'done', 'the newer status stays');
  assert.equal(merged.duration_ms, 12);
  assert.equal(merged.input_preview, '{"path":"a"}', 'the older input is still kept');
  assert.deepEqual(Array.from(rec.transitions, x => `${x.from}>${x.to}`), ['normal>warning', 'warning>normal'],
    'the transitions are in seq order (the last one is the latest)');
});

test('#601 E12: a late approval.requested does not undo the approval.resolved that came first', async () => {
  const vm = require('node:vm');
  const { shimSource } = require('./ui_shim.cjs');
  const { Element } = require('./chat_dom_mock.cjs');
  const doc = { nodes: new Map(), getElementById(id) { if (!this.nodes.has(id)) this.nodes.set(id, new Element(this)); return this.nodes.get(id); },
    createElement: tag => new Element(doc, tag), addEventListener() {}, querySelectorAll: () => [] };
  const context = { document: doc, JSON, Math, Date, String, Number, Array, Set, Map, Promise, Object, WeakMap };
  context.window = context;
  vm.runInNewContext(shimSource() + ribbonSource, context);
  const turns = context.StiraTurns.create({ document: doc, fetchJSON: async () => ({}) });
  const id = 'abab7777';
  turns.ingest(ev(id, 5, 'approval.resolved', { request_id: 'r1', tool_use_id: 'u1', status: 'approved', decision: 'once' }));
  turns.ingest(ev(id, 3, 'approval.requested', { request_id: 'r1', tool_use_id: 'u1', status: 'pending', decision: null }));
  const rec = turns.records.get(id);
  assert.equal(rec.approvals.length, 1);
  assert.equal(rec.approvals[0].status, 'approved', 'the later decision stays');
  assert.equal(rec.approvals[0].decision, 'once');
});
