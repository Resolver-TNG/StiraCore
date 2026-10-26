// Demo steps for the character's motions (#341): scripted *events* only, like director.js.
// Each motion on screen is what model.derive (MOTION_RULES) selects from these events, or —
// for the `anim` steps — a motion the agent requests by id (only `selectable` ones).
// The spike's and the gadgets' steps already show: thinking -> ponder, tool -> look_desk,
// memory -> look_shelf, approval -> head_tilt, achievement -> stretch, curious + web ->
// head_tilt, WARNING -> alert, CRITICAL -> surprised then alert, coding -> typing, tests ->
// look_screen / relief / slump, delegate -> delegate_point, report -> receive_report.

const N = (values) => ({ type: 'neural', values });
const BASE = { trust: 0.5, curiosity: 0.5, vigilance: 0.1, attachment: 0.5, stability: 0.7, satisfaction: 0.5 };

export const MOTION_STEPS = [
  // #341 night round B: how the turn ended by its tests, and the face (FaceHolder)
  {
    key: 'e',
    id: 'tests-done',
    label: { ja: 'テスト通過で完了（ドヤ顔）', en: 'Done, tests passed (smug)' },
    events: [N(BASE), { type: 'turn.start' }, { type: 'tool.start', id: 'e1', kind: 'tool', category: 'other', name: 'run_tests' }, { type: 'tool.end', id: 'e1', ok: true }, { type: 'turn.end', ok: true }],
    say: { ja: 'ふふん。全部通った。', en: 'Hmph. All of them passed.' },
  },
  {
    key: 'o',
    id: 'tests-elated',
    label: { ja: 'テスト通過・満足度が高い（大笑い）', en: 'Tests passed, satisfaction high (laugh)' },
    events: [N({ ...BASE, satisfaction: 0.9 }), { type: 'turn.start' }, { type: 'tool.start', id: 'o1', kind: 'tool', category: 'other', name: 'run_tests' }, { type: 'tool.end', id: 'o1', ok: true }, { type: 'turn.end', ok: true }],
    say: { ja: 'あはは、全部通った！', en: 'Ha ha, every one of them passed!' },
  },
  {
    key: 'i',
    id: 'tests-tired',
    label: { ja: 'テスト失敗のまま完了（ため息）', en: 'Done, a test still failing (sigh)' },
    events: [N(BASE), { type: 'turn.start' }, { type: 'tool.start', id: 'i1', kind: 'tool', category: 'other', name: 'run_tests' }, { type: 'tool.end', id: 'i1', ok: false }, { type: 'turn.end', ok: true }],
    say: { ja: '……はあ。一本だけ、まだ赤い。', en: '...Sigh. One is still red.' },
  },
  {
    key: 'p',
    id: 'fond',
    label: { ja: '信頼と愛着が高い（照れ・赤耳）', en: 'High trust and attachment (shy, pink ears)' },
    events: [N({ ...BASE, attachment: 0.8, trust: 0.7 })],
    say: { ja: '……なに。じっと見ないで。', en: '...What? Don’t stare.' },
  },
  {
    key: 'b',
    id: 'recovered',
    label: { ja: 'ブレーカー復帰（安心）', en: 'Breaker back to NORMAL (relief)' },
    events: [N({ ...BASE, stability: 0.42 }), { type: 'breaker', level: 'WARNING' }, N(BASE), { type: 'breaker', level: 'NORMAL' }],
    say: { ja: '……戻った。ふう、肩の力が抜けた。', en: '...Back to normal. Phew, I can breathe again.' },
  },
  {
    key: 'f',
    id: 'turn-done',
    label: { ja: 'ターン終了（ひと口）', en: 'Turn finished (a sip)' },
    events: [N(BASE), { type: 'turn.start' }, { type: 'turn.end', ok: true }],
    say: { ja: '終わった。冷めないうちに、ひと口。', en: 'Done. A sip before it gets cold.' },
  },
  {
    key: 'j',
    id: 'turn-error',
    label: { ja: 'ターン失敗（がっかり）', en: 'Turn failed (slump)' },
    events: [N(BASE), { type: 'turn.start' }, { type: 'turn.end', ok: false }],
    say: { ja: '……だめだった。もう一度考える。', en: '...That didn\u2019t work. Let me think again.' },
  },
  {
    key: 'z',
    id: 'agent-back',
    label: { ja: 'サブエージェント完了（報告を受ける）', en: 'Subagent done (receives the report)' },
    events: [N(BASE), { type: 'turn.start' }, { type: 'tool.start', id: 'a1', kind: 'subagent', name: 'research' }, { type: 'tool.end', id: 'a1', ok: true }],
    say: { ja: 'おかえり。報告、受け取った。', en: 'Welcome back. Report received.' },
  },
  {
    key: 'w',
    id: 'agent-wave',
    label: { ja: 'エージェントが選択: wave', en: 'Agent picks: wave' },
    events: [N(BASE), { type: 'anim', id: 'wave' }],
    say: { ja: 'おかえり。こっちは準備できてる。', en: 'Welcome back. I\u2019m ready here.' },
  },
  {
    key: 'q',
    id: 'agent-nod',
    label: { ja: 'エージェントが選択: nod', en: 'Agent picks: nod' },
    events: [N(BASE), { type: 'anim', id: 'nod' }],
    say: { ja: 'うん、その方針でいこう。', en: 'Yes, let\u2019s go with that.' },
  },
  {
    key: 'v',
    id: 'agent-shake',
    label: { ja: 'エージェントが選択: shake_head', en: 'Agent picks: shake_head' },
    // the second request is refused (cooldown) and listed in the debug panel
    events: [N(BASE), { type: 'anim', id: 'shake_head' }, { type: 'anim', id: 'typing' }],
    say: { ja: 'それは違うと思う。', en: 'I don\u2019t think that\u2019s right.' },
  },
];
