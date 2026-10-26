// Demo steps for the gadgets (#341, stage 2): scripted *events* only, like director.js.
// Every prop on screen comes from what model.reduce/derive make of these events.

const N = (values) => ({ type: 'neural', values });
const BASE = { trust: 0.5, curiosity: 0.5, vigilance: 0.1, attachment: 0.5, stability: 0.7, satisfaction: 0.5 };
const T = { type: 'turn.start' };
const start = (id, kind, name, extra = {}) => ({ type: 'tool.start', id, kind, name, ...extra });
const end = (id, ok = true) => ({ type: 'tool.end', id, ok });

export const GADGET_STEPS = [
  {
    key: 'c',
    id: 'coding',
    label: { ja: 'コーディング', en: 'Coding' },
    events: [N(BASE), T, start('e1', 'code', 'editor', { write: true })],
    say: { ja: '書いてる。……この関数、もう少しで形になる。', en: 'Writing. ...This function is almost there.' },
  },
  {
    key: 't',
    id: 'tests-run',
    label: { ja: 'テスト実行中', en: 'Tests running' },
    events: [N(BASE), T, start('e1', 'code', 'editor', { write: true }), end('e1'), start('t1', 'test', 'run_tests')],
    say: { ja: 'テストを流してる。ランプが変わるまで待って。', en: 'Running the tests. Wait for the lamp.' },
  },
  {
    key: 'y',
    id: 'tests-pass',
    label: { ja: 'テスト成功', en: 'Tests passed' },
    events: [N({ ...BASE, satisfaction: 0.64 }), T, start('e1', 'code', 'editor', { write: true }), end('e1'), start('t1', 'test', 'run_tests'), end('t1', true)],
    say: { ja: '緑。全部通った。', en: 'Green. Everything passed.' },
  },
  {
    key: 'u',
    id: 'tests-fail',
    label: { ja: 'テスト失敗', en: 'Tests failed' },
    events: [N(BASE), T, start('e1', 'code', 'editor', { write: true }), end('e1'), start('t1', 'test', 'run_tests'), end('t1', false)],
    say: { ja: '……赤。どこで落ちたか見る。', en: '...Red. Let me see where it broke.' },
  },
  {
    key: 's',
    id: 'delegate',
    label: { ja: 'サブエージェント3体', en: 'Three subagents' },
    events: [N(BASE), T, start('a1', 'subagent', 'research'), start('a2', 'subagent', 'review'), start('a3', 'subagent', 'tests')],
    say: { ja: '三つに分けて任せた。戻ってきたら聞くね。', en: 'Split it three ways. I\u2019ll hear them out when they\u2019re back.' },
  },
  {
    key: 'r',
    id: 'report',
    label: { ja: '報告（完了1・失敗1・稼働1）', en: 'Reports (1 done, 1 failed, 1 running)' },
    events: [N(BASE), T, start('a1', 'subagent', 'research'), start('a2', 'subagent', 'review'), start('a3', 'subagent', 'tests'), end('a1', true), end('a2', false)],
    say: { ja: '一つ戻った。……一つは失敗して止まってる。', en: 'One is back. ...One failed and is standing by.' },
  },
  {
    key: 'g',
    id: 'swarm',
    label: { ja: 'サブエージェント9体', en: 'Nine subagents' },
    events: [N(BASE), T, ...['a1', 'a2', 'a3', 'a4', 'a5', 'a6', 'a7', 'a8', 'a9'].map((id) => start(id, 'subagent', `worker ${id}`))],
    say: { ja: '九つ並行。表示は六つ分、残りは数で。', en: 'Nine in parallel. Six shown, the rest as a count.' },
  },
  {
    key: 'm',
    id: 'memory-write',
    label: { ja: '記憶の書き込み（意味記憶）', en: 'Memory write (semantic)' },
    events: [N(BASE), T, start('w1', 'memory', 'memory_write', { write: true, layer: 'semantic' })],
    say: { ja: '覚えておく。付箋を貼っておくね。', en: 'Noting this down. A sticky note on the shelf.' },
  },
  {
    key: 'k',
    id: 'forget',
    label: { ja: '忘却（箱に詰めて棚の奥へ）', en: 'Forgetting (boxed, restorable)' },
    events: [N(BASE), { type: 'memory.forget', count: 12 }],
    say: { ja: '古い記憶を箱に詰めて奥へ。消してはいない、戻せる。', en: 'Old memories boxed up and put away. Not deleted; restorable.' },
  },
  {
    key: 'h',
    id: 'scheduler',
    label: { ja: 'スケジュール実行', en: 'Scheduled job' },
    events: [N(BASE), { type: 'job.start', id: 'j1', name: 'heartbeat' }],
    say: { ja: '定刻。ハートビートを回してる。', en: 'On the hour. Running the heartbeat.' },
  },
  {
    key: 'x',
    id: 'tool-fail',
    label: { ja: 'ツール失敗（Web）', en: 'Tool failed (web)' },
    events: [N({ ...BASE, curiosity: 0.6 }), T, start('f1', 'web', 'web_fetch'), end('f1', false)],
    say: { ja: '取得に失敗。……もう一度やってみる。', en: 'The fetch failed. ...I\u2019ll try again.' },
  },
  {
    key: 'a',
    id: 'approved',
    label: { ja: '承認（判子）', en: 'Approved (seal)' },
    events: [N(BASE), T, { type: 'approval.request', id: 'a1', summary: 'write_file: notes/plan.md' }, { type: 'approval.resolved', id: 'a1', decision: 'approve' }],
    say: { ja: '許可、ありがとう。書くね。', en: 'Thanks for the go-ahead. Writing it.' },
  },
  {
    key: 'n',
    id: 'denied',
    label: { ja: '却下（判子）', en: 'Denied (seal)' },
    events: [N(BASE), T, { type: 'approval.request', id: 'a1', summary: 'write_file: notes/plan.md' }, { type: 'approval.resolved', id: 'a1', decision: 'deny' }],
    say: { ja: 'わかった、今回は書かない。', en: 'Understood. I won\u2019t write it this time.' },
  },
];
