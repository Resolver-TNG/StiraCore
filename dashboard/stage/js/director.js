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

// --- the goal grant's life (ADR 0011 stage C; docs/stage.md, "The goal grant in the demo") ---
// Each step carries `activity`: one dummy answer of GET /api/activity in the server's shape
// (observability/activity.py grant_view and rounds_view: codes, counts and tool names only),
// which the page draws on the board with activity.js exactly as it draws a live answer. No
// request is made. Key `G` plays the next of them; they are not part of the autoplay.
export const DEMO_TIME = 1792416000; // the dummy "server time": the same picture on every run
const round = (n, progress, writes, files, end = 'done') => ({ round: n, of_max: 12, wall_ms: 41000, model_calls: 3, writes, files_changed: files, tokens_in: 14000, tokens_out: 2100, origin: 'tick', end_code: end, progress });
const outcome = (n, what, tools, sec = 41) => ({ what, number: n, duration_ms: sec * 1000, tick_used: 16100, ts: DEMO_TIME - (8 - n) * 600, tools });
const ROUNDS = [round(1, 'new', 2, 2), round(2, 'new', 1, 1), round(3, 'repeat', 1, 0), round(4, 'none', 0, 0, 'yielded_user_turn'), round(5, 'new', 3, 2), round(6, 'new', 1, 1, 'claimed')];
const OUTCOMES = [
  outcome(1, 'done', ['file_read', 'editor']), outcome(2, 'done', ['file_read', 'editor', 'run_checks']), outcome(3, 'done', ['editor', 'run_checks']),
  outcome(4, 'yielded_user_turn', ['file_read'], 9), outcome(5, 'done', ['editor', 'run_checks']), outcome(6, 'claimed', ['run_checks'], 22),
];
const CLAIM = {
  claimed_at: DEMO_TIME - 540, iteration: 6, max_iterations: 12, used_tokens: 96600, budget_left: 303400, ttl_left_sec: 21600,
  checks: 'pass', check_rows: [{ kind: 'pytest', status: 'passed', counts: { passed: 42, failed: 0, errors: 0, skipped: 1, findings: 0 } }, { kind: 'ruff', status: 'passed', counts: { passed: 0, failed: 0, errors: 0, skipped: 0, findings: 0 } }],
  workspace: 'changed', progress: 'new', waiting_sec: 540, stale_after_sec: 1800, stale: false,
};
// after n iterations: the counters, the rounds and the outcomes so far
const after = (n, over = {}) => ({
  id: '7c41e09d', state: 'active', live: true, headline: { subject: 'grant', state: 'active' }, goal_state: 'active',
  iterations: n, max_iterations: 12, ttl_sec: 28800, ttl_left_sec: 28800 - n * 1200, budget_total: 400000, budget_left: 400000 - n * 16100, budget_per_tick: 60000,
  awaiting_verification: false, cards: 0, card: false, iteration: n ? { run: 'idle', number: n, end: 'done', tick_used: 16100 } : { run: 'idle' },
  outcomes: OUTCOMES.slice(0, n).slice(-5), ...(n ? { rounds: ROUNDS.slice(0, n).slice(-5), ledger: 'ok' } : {}), ...over,
});
const activity = (grant) => ({
  server_time: DEMO_TIME, cursor: 0, reset: true, items: [], stall_sec: 120, state: { code: 'idle', since: null, last_event: null, running: false }, yolo: null,
  totals: { turns: 0, input: 0, output: 0, cache_read: 0, cache_write: 0, context_tokens: null, cost: null }, grant,
});
const grantStep = (id, label, events, say, grant) => ({ key: 'G', id: `grant-${id}`, label, events, say, activity: activity(grant) });
const T = { type: 'turn.start' };
const UNMET = { goal_state: 'unmet', live: false, iteration: { run: 'idle', number: 3, end: 'stopped', tick_used: 4200 } };

export const GRANT_STEPS = [
  grantStep('issued', { ja: 'グラント: 発行直後', en: 'Grant: just issued' }, [N(BASE)],
    { ja: '目標グラントを受け取った。ここからは、ひとりで進める。', en: 'Goal grant received. From here I work on my own.' }, after(0)),
  grantStep('running', { ja: 'グラント: 反復を実行中', en: 'Grant: an iteration runs' }, [N(BASE), T, { type: 'tool.start', id: 'g1', kind: 'code', name: 'editor', write: true }],
    { ja: '三つめの反復。直して、確かめて、次へ。', en: 'Third iteration. Fix, check, next.' },
    after(2, { iteration: { run: 'running', number: 3, tick_used: 18200 } })),
  grantStep('approval', { ja: 'グラント: 承認カード待ち', en: 'Grant: waiting for an approval card' }, [N(BASE), T],
    { ja: 'ここから先は、あなたの承認が要る。カードを見て。', en: 'From here I need your approval. Look at the card.' },
    after(3, { card: true, cards: 1, iteration: { run: 'running', number: 4, tick_used: 9100 } })),
  grantStep('claim', { ja: 'グラント: 完了の申告（確認待ち）', en: 'Grant: completion claimed (awaiting verification)' }, [N({ ...BASE, satisfaction: 0.64 })],
    { ja: 'できたと思う。確かめて、あなたが決めて。', en: 'I think it is done. Check it; the decision is yours.' },
    after(6, { headline: { subject: 'goal', state: 'awaiting_verification' }, goal_state: 'awaiting_verification', awaiting_verification: true, iteration: { run: 'idle', number: 6, end: 'claimed', tick_used: 16100 }, claim: CLAIM })),
  grantStep('complete', { ja: 'グラント: 人が確定して終了', en: 'Grant: confirmed by the person, ended' }, [N({ ...BASE, satisfaction: 0.82, trust: 0.64 }), T, { type: 'turn.end', ok: true }],
    { ja: '確認、ありがとう。この目標はここまで。', en: 'Thanks for confirming. This goal ends here.' },
    after(6, { state: 'ended', live: false, reason: 'complete', headline: { subject: 'grant', state: 'ended', reason: 'complete' }, goal_state: 'complete', goal_reason: 'verified', iteration: { run: 'idle', number: 6, end: 'claimed', tick_used: 16100 }, claim: CLAIM })),
  grantStep('killed', { ja: 'グラント: キルスイッチで取り消し', en: 'Grant: revoked by the kill switch' }, [N(BASE)],
    { ja: '止められた。手を離す。', en: 'I was stopped. Hands off.' },
    after(3, { ...UNMET, state: 'revoked', reason: 'kill_switch', headline: { subject: 'grant', state: 'revoked', reason: 'kill_switch' }, goal_reason: 'revoked_kill_switch' })),
  grantStep('expired', { ja: 'グラント: 期限切れ（TTL）', en: 'Grant: expired (TTL)' }, [N(BASE)],
    { ja: '時間切れ。続きは、新しいグラントで。', en: 'Out of time. The rest needs a new grant.' },
    after(5, { ...UNMET, state: 'expired', reason: 'ttl', ttl_left_sec: 0, headline: { subject: 'grant', state: 'expired', reason: 'ttl' }, goal_reason: 'expired_ttl', iteration: { run: 'idle', number: 5, end: 'done', tick_used: 16100 } })),
  grantStep('halted', { ja: 'グラント: ブレーカーで停止', en: 'Grant: halted by the breaker' }, [N({ ...BASE, stability: 0.24, vigilance: 0.62 }), { type: 'breaker', level: 'CRITICAL' }],
    { ja: 'ブレーカー作動。落ち着くまで、反復は始めない。', en: 'Breaker tripped. No iteration until I settle.' },
    after(3, { state: 'halted', reason: 'breaker_critical', headline: { subject: 'grant', state: 'halted', reason: 'breaker_critical' } })),
  grantStep('budget', { ja: 'グラント: 予算切れ', en: 'Grant: out of budget' }, [N(BASE)],
    { ja: '予算を使い切った。ここで止まる。', en: 'The budget is spent. I stop here.' },
    after(5, { state: 'paused', reason: 'goal_budget_limited', headline: { subject: 'goal', state: 'budget_limited' }, goal_state: 'budget_limited', budget_left: 9000, iteration: { run: 'idle', number: 6, end: 'budget_refused', tick_used: 0 } })),
  grantStep('blocked', { ja: 'グラント: 無進展で停止', en: 'Grant: stopped, no progress' }, [N({ ...BASE, satisfaction: 0.38 })],
    { ja: '同じところを回ってる。進んでいないから、止まる。', en: 'I keep circling. No progress, so I stop.' },
    after(5, { state: 'paused', reason: 'goal_blocked', headline: { subject: 'goal', state: 'blocked', reason: 'no_progress' }, goal_state: 'blocked', goal_reason: 'no_progress', rounds: [round(1, 'new', 2, 2), round(2, 'repeat', 1, 0), round(3, 'repeat', 1, 0), round(4, 'none', 0, 0), round(5, 'none', 0, 0)], ledger: 'ok' })),
];
