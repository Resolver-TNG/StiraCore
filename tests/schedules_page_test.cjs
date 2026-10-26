// /schedules page states (#239 UI audit): node --test tests/schedules_page_test.cjs
const { test } = require('node:test');
const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const { resolve } = require('node:path');
const vm = require('node:vm');
const { shimSource } = require('./ui_shim.cjs');
const { Element, settle } = require('./chat_dom_mock.cjs');

const source = readFileSync(resolve(__dirname, '../dashboard/static/js/schedules_page.js'), 'utf8');

function harness(respond, extra = {}) {
  const doc = {
    hidden: false,
    nodes: new Map(),
    getElementById(id) {
      if (!this.nodes.has(id)) { const node = new Element(this); node.id = id; this.nodes.set(id, node); }
      return this.nodes.get(id);
    },
    createElement: tag => new Element(doc, tag),
  };
  const state = { respond };
  const context = {
    document: doc, JSON, Math, Date, String, Number, Array, Object, Promise,
    setInterval: () => 0,
    fetch: async () => state.respond(),
    ...extra,
  };
  context.window = context;
  vm.runInNewContext(shimSource() + source, context);
  const $ = id => doc.getElementById(id);
  return { $, state };
}

const ok = data => ({ ok: true, status: 200, json: async () => data });

test('breaker level uses the sidebar words, not the raw enum', async () => {
  const h = harness(() => ok({
    status: { enabled: true, state: 'running', breaker_level: 'warning', alerts: [] },
    jobs: [], runs: [], notifications: [],
  }));
  await settle();
  assert.equal(h.$('sched-breaker').textContent, '警告');
  assert.equal(h.$('sched-facts').hidden, false);
  assert.equal(h.$('sched-disabled').hidden, true);
});

test('unknown breaker values are shown as they are; missing is a dash', async () => {
  const h = harness(() => ok({ status: { enabled: true, breaker_level: 'strange' } }));
  await settle();
  assert.equal(h.$('sched-breaker').textContent, 'strange');
  const off = harness(() => ok({ status: { enabled: false } }));
  await settle();
  assert.equal(off.$('sched-breaker').textContent, '—');
  assert.equal(off.$('sched-facts').hidden, true, 'facts are hidden while the scheduler is off');
});

test('a failed first load replaces the "loading" rows with an error row', async () => {
  const h = harness(() => ({ ok: false, status: 500, json: async () => ({}) }));
  await settle();
  assert.equal(h.$('sched-message').textContent, '状態を読み込めませんでした');
  for (const [id, columns] of [['sched-jobs', 7], ['sched-runs', 6]]) {
    const [row] = h.$(id).children;
    assert.equal(row.children[0].textContent, '読み込めませんでした（5 秒ごとに再試行します）');
    assert.equal(row.children[0].colSpan, columns);
  }
});

test('alert keys, run reasons and notification codes are worded from the catalog (#279)', async () => {
  const h = harness(() => ok({
    status: { enabled: true, state: 'running', breaker_level: 'normal',
      alerts: ['breaker_critical', 'job_failing:j1', 'mystery_alert'] },
    jobs: [{ job_id: 'j1', name: 'digest', layer: 'gateway', action: 'memory_digest', state: 'active',
      schedule: { type: 'interval', every_sec: 60 }, consecutive_failures: 3 }],
    runs: [
      { run_id: 'r1', job_id: 'j1', status: 'failed', reason: 'approval_required', tokens: 0, started_at: 1 },
      { run_id: 'r2', job_id: 'j1', status: 'failed', reason: 'ValueError', tokens: 0, started_at: 2 },
      { run_id: 'r3', job_id: 'j1', status: 'completed', reason: null, tokens: 5, started_at: 3 },
    ],
    notifications: [
      { id: 1, at: 1, code: 'job_failing', state: 'raised', severity: 'warning', job_id: 'j1',
        message: 'job j1 failed 3 times in a row' },
      { id: 2, at: 2, code: 'job_failing', state: 'resolved', severity: 'info', job_id: 'j1',
        message: 'resolved: job j1 succeeded' },
      { id: 3, at: 3, code: 'brand_new', state: 'event', severity: 'info', job_id: null, message: 'server text' },
    ],
  }));
  await settle();
  assert.equal(h.$('sched-alerts').textContent,
    'ブレーカーが「危険」（ジョブは開始されず、実行中のものは取り消されます）, digest が続けて失敗しています, その他（コード: mystery_alert）');
  const reasons = h.$('sched-runs').children.map(row => row.children[3]);
  assert.equal(reasons[0].textContent, '承認が必要な操作に当たった');
  assert.equal(reasons[0].title, 'approval_required', 'the raw code stays available');
  assert.equal(reasons[1].textContent, 'その他（コード: ValueError）', 'unknown codes are never shown bare');
  assert.equal(reasons[2].textContent, '—');
  const notes = h.$('sched-notes').children.map(item => item.children.map(c => c.textContent));
  assert.deepEqual(notes.map(n => n[1]), [
    'digest が続けて失敗しています', '解決: digest が続けて失敗しています', 'その他（コード: brand_new）']);
  // The server's sentence is kept as the detail line.
  assert.deepEqual(notes.map(n => n[2]), ['job j1 failed 3 times in a row', 'resolved: job j1 succeeded', 'server text']);
  assert.equal(h.$('sched-notes').children[0].children[2].lang, 'en');
});

test('notification variables build the sentence; wrong-shaped variables are ignored (#279)', async () => {
  const note = (id, code, params, extra = {}) => ({ id, at: id, code, state: 'event', severity: 'warning',
    job_id: 'j1', message: `server sentence ${id}`, params, ...extra });
  const h = harness(() => ok({
    status: { enabled: true, state: 'running', breaker_level: 'normal', alerts: [] },
    jobs: [{ job_id: 'j1', name: 'digest', layer: 'gateway', action: 'memory_digest', state: 'active',
      schedule: { type: 'interval', every_sec: 60 }, consecutive_failures: 0 }],
    runs: [],
    notifications: [
      note(1, 'job_failing', { count: 5 }),
      note(2, 'approval_required', { tool: 'file_write' }),
      note(3, 'budget_exhausted', { kind: 'max_runs_per_day', limit: 20 }),
      note(4, 'budget_exhausted', { kind: 'max_tokens_per_run' }),
      // Wrong shapes: the plain wording of the code, and nothing of the value shown.
      note(5, 'job_failing', { count: 'many' }),
      note(6, 'approval_required', { tool: 'sk-' + 'a'.repeat(40) + ' rm -rf /' }),
      note(7, 'budget_exhausted', { kind: 'free text', limit: -1 }),
      note(8, 'job_failing', undefined),
      note(9, 'job_failing', null),
      // A resolved alert wraps the same wording.
      note(10, 'job_failing', { count: 5 }, { state: 'resolved' }),
      // Odd code shapes never appear bare.
      note(11, 'sk-' + 'b'.repeat(40) + ' text', {}),
      note(12, 'x'.repeat(80), {}),
      note(13, 'job_notice', {}),
    ],
  }));
  await settle();
  const texts = h.$('sched-notes').children.map(item => item.children[1].textContent);
  assert.deepEqual(texts, [
    'digest が 5 回続けて失敗しています',
    'digest が承認の必要な操作（file_write）に当たり、実行を止めました',
    'digest を予算の上限（1 日の実行回数: 20）で一時停止しました（再開は人が行います）',
    'digest を予算の上限（1 回あたりのトークン数）で一時停止しました（再開は人が行います）',
    'digest が続けて失敗しています',
    'digest が承認の必要な操作に当たり、実行を止めました',
    'digest を予算の上限で一時停止しました（再開は人が行います）',
    'digest が続けて失敗しています',
    'digest が続けて失敗しています',
    '解決: digest が 5 回続けて失敗しています',
    'その他',
    'その他',
    'その他（コード: job_notice）',
  ]);
  assert.ok(!texts.join('').includes('sk-'));
  // The server sentence stays as the (English, muted) detail.
  assert.equal(h.$('sched-notes').children[0].children[2].textContent, 'server sentence 1');
  assert.equal(h.$('sched-notes').children[0].children[2].lang, 'en');
});

test('a run reason or alert that is not code-shaped is plain "other"; the value is not shown (#279)', async () => {
  const secret = 'sk-' + 'c'.repeat(40);
  const h = harness(() => ok({
    status: { enabled: true, state: 'running', breaker_level: 'normal', alerts: [`${secret} free text`] },
    jobs: [],
    runs: [{ run_id: 'r1', job_id: 'j1', status: 'failed', reason: `HTTP 500 ${secret}`, tokens: 0, started_at: 1 }],
    notifications: [],
  }));
  await settle();
  assert.equal(h.$('sched-alerts').textContent, 'その他');
  const cell = h.$('sched-runs').children[0].children[3];
  assert.equal(cell.textContent, 'その他');
  assert.equal(cell.dataset.code, undefined);
  assert.ok(!JSON.stringify([cell.textContent, cell.title, h.$('sched-alerts').textContent]).includes('sk-'));
});

test('a refused change is worded by its code; the server sentence stays as the detail (#279)', async () => {
  const data = {
    status: { enabled: true, state: 'running', breaker_level: 'warning', alerts: [] },
    jobs: [{ job_id: 'j1', name: 'digest', layer: 'gateway', action: 'health_check', state: 'active',
      schedule: { type: 'interval', every_sec: 60 }, consecutive_failures: 0 }],
    runs: [], notifications: [],
  };
  const h = harness(() => ok(data));
  await settle();
  const pause = () => h.$('sched-jobs').children[0].children[6]
    .find(node => node.tagName === 'button');
  const refuse = body => {
    let once = true;
    h.state.respond = () => {
      if (!once) return ok(data);
      once = false;
      return { ok: false, status: 409, json: async () => body };
    };
  };
  refuse({ code: 'breaker', error: 'schedule changes are refused while the breaker is warning' });
  pause().click();
  await settle();
  const message = h.$('sched-message');
  assert.equal(message.textContent,
    '拒否されました: ブレーカーが「正常」でない間は変更できません (schedule changes are refused while the breaker is warning)');
  assert.equal(message.children[0].lang, 'en', 'the server sentence is marked English');
  // Unknown code: the server sentence as before (never guessed).
  refuse({ code: 'brand_new', error: 'something else' });
  pause().click();
  await settle();
  assert.equal(message.textContent, '拒否されました: something else');
  assert.equal(message.children.length, 0);
});

test('a refusal the fetch wrapper worded itself (ADR 0012 §8b) has no repeated detail line (review of #432)', async () => {
  const data = {
    status: { enabled: true, state: 'running', breaker_level: 'normal', alerts: [] },
    jobs: [{ job_id: 'j1', name: 'digest', layer: 'gateway', action: 'health_check', state: 'active',
      schedule: { type: 'interval', every_sec: 60 }, consecutive_failures: 0 }],
    runs: [], notifications: [],
  };
  const h = harness(() => ok(data));
  await settle();
  const pause = () => h.$('sched-jobs').children[0].children[6].find(node => node.tagName === 'button');
  const message = h.$('sched-message');
  for (const [code, words] of [
    ['tool_call_running', 'エージェントのツール呼び出しの実行中です。終わってからもう一度試してください'],
    ['host_not_acknowledged', '確認のチェックが入っていません。チェックしてからもう一度押してください(まだ何も適用していません)'],
  ]) {
    let once = true;
    h.state.respond = () => {
      if (!once) return ok(data);
      once = false;
      return { ok: false, status: 409, json: async () => ({ code, error: 'page wording', local: true }) };
    };
    pause().click();
    await settle();
    assert.equal(message.textContent, `拒否されました: ${words}`, code);
    assert.equal(message.children.length, 0, `${code}: no detail line`);
  }
});

test('a job paused by the system is explained; an operator pause reason stays as typed (#279)', async () => {
  const job = (id, by, reason) => ({ job_id: id, name: id, layer: 'gateway', action: 'health_check', state: 'paused',
    paused_by: by, paused_reason: reason, schedule: { type: 'interval', every_sec: 60 }, consecutive_failures: 0 });
  const h = harness(() => ok({
    status: { enabled: true, state: 'running', breaker_level: 'normal', alerts: [] },
    jobs: [job('j1', 'system', 'stored definition invalid'), job('j2', 'local:operator', 'stored definition invalid'),
      job('j3', 'local:operator', 'メンテナンス中')],
    runs: [], notifications: [],
  }));
  await settle();
  const titles = h.$('sched-jobs').children.map(row => row.children[3].title);
  assert.deepEqual(titles, [
    '保存された定義が読めないため、システムが一時停止しました', 'stored definition invalid', 'メンテナンス中']);
});

test('every refusal code of the scheduler API has words (#279)', () => {
  const py = readFileSync(resolve(__dirname, '../src/stiracore/observability/scheduler_api.py'), 'utf8');
  const block = /STATUS_CODES = \{([\s\S]*?)\n\}/.exec(py)[1];
  const codes = [...block.matchAll(/"(\w+)":/g)].map(m => m[1]);
  assert.ok(codes.length >= 7);
  for (const code of codes) {
    assert.ok(source.includes(`${code}: "schedules.refused_${code}"`), `${code} is worded`);
  }
});

// --- #369: the page's poll goes through poll.js -------------------------------------------------
test('the poll is paused when hidden, backs off on failure and widens when nothing changes (#369)', async () => {
  const keeps = [];
  const StiraPoll = { keep: (read, base, max, hold, doc) => { keeps.push({ read, base, max, doc }); return {}; } };
  const jobs = { status: { enabled: true, state: 'running' }, jobs: [], runs: [], notifications: [] };
  const h = harness(() => ok(jobs), { StiraPoll });
  await settle();
  assert.equal(keeps.length, 1);
  const [{ read, base, max, doc }] = keeps;
  assert.equal(base, 5000);
  assert.equal(max, 20000);
  assert.ok(doc, 'the document is passed so a hidden tab is not polled');
  assert.equal(await read(), true, 'the first read: new');
  assert.equal(await read(), false, 'unchanged: may widen');
  jobs.runs = [{ run_id: 'r1', job_id: 'j', status: 'ok' }];
  assert.equal(await read(), true, 'a run appeared');
  h.state.respond = () => ({ ok: false, status: 503, json: async () => ({}) });
  assert.equal(await read(), null, 'unreadable: a failure (backoff)');
  assert.ok(h.$('sched-message').textContent.length > 0);
});
