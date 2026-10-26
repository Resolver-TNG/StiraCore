// Demo director (#341, stage 1): a scripted list of *events* (not looks). The scene only ever
// sees what model.reduce/derive make of these events, exactly as it will with Stira's SSE.

const N = (values) => ({ type: 'neural', values });
const BASE = { trust: 0.5, curiosity: 0.5, vigilance: 0.1, attachment: 0.5, stability: 0.7, satisfaction: 0.5 };

// Each step: key, label, events to apply from a fresh state, line to say (ja/en).
export const STEPS = [
  {
    key: '1',
    id: 'calm',
    // the agent picks a gesture; an unknown id is ignored (see the debug panel)
    events: [N(BASE), { type: 'breaker', level: 'NORMAL' }, { type: 'anim', id: 'gesture.ear-perk' }, { type: 'anim', id: 'dance.backflip' }],
    say: { ja: 'こんばんは。ラボは静か。何から片付けようか。', en: 'Evening. The lab is quiet. What shall we tackle first?' },
  },
  {
    key: '2',
    id: 'thinking',
    // selecting a state animation is refused: the agent cannot fake 'thinking' steam
    events: [N(BASE), { type: 'turn.start' }, { type: 'anim', id: 'state.steam' }],
    say: { ja: '少し考えさせて。……前提を並べ直してるところ。', en: 'Let me think. ...Re-ordering the premises.' },
  },
  {
    key: '3',
    id: 'tool',
    events: [N(BASE), { type: 'turn.start' }, { type: 'tool.start', id: 'c1', kind: 'tool', name: 'run_tests' }],
    say: { ja: 'テストを回してる。計器の波形が落ち着くまで待って。', en: 'Running the tests. Wait for the scope to settle.' },
  },
  {
    key: '4',
    id: 'memory',
    events: [N(BASE), { type: 'turn.start' }, { type: 'tool.start', id: 'c2', kind: 'memory', name: 'memory_search' }],
    say: { ja: '棚の記憶を探してる。先週の決定、どこに置いたかな。', en: 'Searching the shelf. Where did I file last week\u2019s decision?' },
  },
  {
    key: '5',
    id: 'approval',
    events: [N(BASE), { type: 'turn.start' }, { type: 'approval.request', id: 'a1', summary: 'write_file: notes/plan.md' }],
    say: { ja: 'ファイルを書き換えたい。許可をもらえる？', en: 'I want to write a file. May I?' },
    choices: [
      { id: 'approve', ja: '許可する（notes/plan.md）', en: 'Approve (notes/plan.md)' },
      { id: 'deny', ja: '今回は見送る', en: 'Not this time' },
      // live mode offers what the approval API supports: the masked arguments, not a diff
      { id: 'details', ja: '引数を先に見せて', en: 'Show me the arguments first' },
    ],
  },
  {
    key: '6',
    id: 'achievement',
    events: [N({ ...BASE, satisfaction: 0.82, trust: 0.64 }), { type: 'turn.start' }, { type: 'turn.end', ok: true }, { type: 'anim', id: 'gesture.nod' }],
    say: { ja: '通った！全部グリーン。……ちょっと嬉しい。', en: 'It passed! All green. ...I\u2019m a little proud.' },
  },
  {
    key: '7',
    id: 'curious',
    events: [N({ ...BASE, curiosity: 0.82 }), { type: 'turn.start' }, { type: 'tool.start', id: 'c3', kind: 'web', name: 'web_search' }],
    say: { ja: '外の資料を見に行ってる。これ、面白いかも。', en: 'Looking outside for sources. This might be interesting.' },
  },
  {
    key: '8',
    id: 'worried',
    events: [N({ ...BASE, stability: 0.42, vigilance: 0.38 }), { type: 'breaker', level: 'WARNING' }],
    say: { ja: '……ごめん、少し揺らいでる。書き込みは控えるね。', en: '...Sorry, I\u2019m a bit unsteady. I\u2019ll hold off on writes.' },
  },
  {
    key: '9',
    id: 'critical',
    events: [N({ ...BASE, stability: 0.24, vigilance: 0.62 }), { type: 'breaker', level: 'CRITICAL' }],
    say: { ja: 'ブレーカー作動。読み取りだけにする。落ち着くまで待って。', en: 'Breaker tripped. Read-only now. Give me time to settle.' },
  },
];

export const AUTOPLAY_SECONDS = 7;
