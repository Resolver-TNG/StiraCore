// The board's activity mode (#446): what she is doing now, what she did last, the YOLO window.
//
// One read-only, same-origin source: GET /api/activity (observability/activity.py). The first
// read (no cursor) brings the last 20 rows from turns.db, so a reload restores the list; later
// reads ask only for rows after the cursor (after_id). The server sends codes, counts, times
// and tool names only; nothing here reads or shows an argument, an output, a file, a command or
// a secret, and everything is drawn with createElement + textContent.
//
// Polling (#369, static/js/poll.js): only while the activity is on the board, 4 s while
// something runs (a turn, an approval), widened up to 20 s while nothing changes, paused while
// the tab is hidden, backed off when the server fails. The elapsed seconds tick on the page
// between reads (server clock corrected), not by asking again.
//
// Pure helpers first (tested in Node: tests/stage_activity_test.cjs), then the DOM.

export const POLL = { base: 4, max: 20, backoffMax: 60 }; // seconds
export const RECENT = 20;
export const STALL_SEC = 120; // the server's value wins (stall_sec)
// "show the activity on the board while she waits" — this browser's choice (default off)
export const IDLE_KEY = 'stira.stage.board_idle';
const BUSY = new Set(['generating', 'tool', 'working', 'approval']);
const TRIGGER_KEY = { time: 'stage.act_trigger_time', tools: 'stage.act_trigger_tools', tokens: 'stage.act_trigger_tokens', declared: 'stage.act_trigger_declared' };
function triggerText(item, t) {
  const key = TRIGGER_KEY[code(item.count) || code(item.trigger)] || null;
  return key ? t(key) : '';
}
const CODE = /^[A-Za-z0-9_.:-]{1,64}$/;
const code = (v) => (typeof v === 'string' && CODE.test(v) ? v : null);
const int = (v) => (Number.isInteger(v) && v >= 0 ? v : null);
const num = (v) => (typeof v === 'number' && Number.isFinite(v) ? v : null);

// The list after one answer: the first read replaces it, a diff appends (by row id, once).
export function mergeItems(prev, body, limit = RECENT) {
  const fresh = Array.isArray(body && body.items) ? body.items.filter((i) => i && Number.isInteger(i.id)) : [];
  const base = body && body.reset ? [] : Array.isArray(prev) ? prev : [];
  const seen = new Set(base.map((i) => i.id));
  const out = base.slice();
  // one row per id, also inside one answer (W5b audit D1)
  for (const i of fresh) {
    if (seen.has(i.id)) continue;
    seen.add(i.id);
    out.push(i);
  }
  out.sort((a, b) => a.id - b.id);
  return out.slice(-limit);
}

export function elapsed(since, now) {
  const s = num(since);
  const n = num(now);
  return s === null || n === null ? null : Math.max(0, Math.floor(n - s));
}

export function duration(sec, t) {
  if (sec === null || sec === undefined) return '';
  if (sec < 60) return t('stage.act_sec', { n: sec });
  if (sec < 3600) return t('stage.act_min_sec', { m: Math.floor(sec / 60), s: sec % 60 });
  return t('stage.act_hour_min', { h: Math.floor(sec / 3600), m: Math.floor((sec % 3600) / 60) });
}

export function clock(ts) {
  const v = num(ts);
  if (v === null) return '--:--:--';
  const d = new Date(v * 1000);
  const p = (x) => String(x).padStart(2, '0');
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}

// -> { code, text, stall (bool), stallText }. now: server seconds.
export function stateText(state, now, t, stallSec = STALL_SEC, offline = false) {
  if (offline) return { code: 'offline', text: t('stage.act_state_offline'), stall: false, stallText: '' };
  const s = state && typeof state === 'object' ? state : {};
  const c = code(s.code) || 'idle';
  const since = elapsed(s.since, now);
  const d = since === null ? '' : duration(since, t);
  const tool = code(s.tool) || 'other';
  let text;
  if (c === 'generating') text = t('stage.act_state_generating', { time: d });
  else if (c === 'tool') text = t('stage.act_state_tool', { tool, time: d });
  else if (c === 'working') text = t('stage.act_state_working', { time: d });
  else if (c === 'approval') text = t('stage.act_state_approval', { tool, count: int(s.waiting) || 1 });
  else if (c === 'report') text = t('stage.act_state_report', { trigger: triggerText(s, t), time: d });
  else if (c === 'paused') text = t('stage.act_state_paused', { reason: reasonText(s.reason, t) });
  else if (c === 'stopped') text = t('stage.act_state_stopped', { reason: reasonText(s.reason, t) });
  else text = t('stage.act_state_idle');
  const quiet = BUSY.has(c) && c !== 'approval' ? elapsed(s.last_event, now) : null;
  const stall = quiet !== null && quiet >= stallSec;
  return { code: c, text, stall, stallText: stall ? t('stage.act_stall', { time: duration(quiet, t) }) : '' };
}

function reasonText(reason, t) {
  const r = code(reason);
  if (r === 'breaker' || r === 'breaker_critical') return t('stage.act_reason_breaker');
  if (r === 'latched') return t('stage.act_reason_latched');
  return r ? t('stage.act_reason_other', { code: r }) : '';
}

// One row: { time, kind, text, ok (true | false | null), href (artifacts of the turn) | null,
// artifacts (their count, with href) | null }
const WRITES = new Set(['file_write', 'editor']);
export function rowView(item, t) {
  const i = item && typeof item === 'object' ? item : {};
  const name = code(i.name) || 'other';
  const what = code(i.what) || 'other';
  const ms = int(i.duration_ms);
  const took = ms === null ? '' : duration(Math.round(ms / 1000), t);
  const ok = typeof i.ok === 'boolean' ? i.ok : null;
  let text;
  let href = null;
  switch (i.kind) {
    case 'turn':
      if (what === 'started') text = t(i.auto ? 'stage.act_row_turn_auto' : 'stage.act_row_turn_started');
      else {
        const stop = code(i.stop);
        const shown = stop && stop !== 'completed' ? `(${stop})` : '';
        text = t(ok === false ? 'stage.act_row_turn_failed' : 'stage.act_row_turn_done', { stop: shown }).trim();
      }
      break;
    case 'tool':
      // #545: a declared checkpoint's own call was cancelled on purpose (ok: null, not refused)
      if (what === 'declared') text = t('stage.act_row_tool_declared', { tool: name });
      else text = t(ok ? 'stage.act_row_tool_done' : what === 'denied' ? 'stage.act_row_tool_denied' : 'stage.act_row_tool_failed', { tool: name, time: took });
      break;
    case 'model':
      text = t(ok ? 'stage.act_row_model_done' : 'stage.act_row_model_failed', { time: took });
      break;
    case 'approval':
      text = what === 'requested' ? t('stage.act_row_approval_asked', { tool: name }) : t('stage.act_row_approval_decided', { decision: what });
      break;
    case 'breaker':
      text = t('stage.act_row_breaker', { level: what });
      break;
    case 'progress':
      text = what === 'report' ? t('stage.act_row_report', { trigger: triggerText(i, t) }).trim() : t('stage.act_row_reply', { answer: what });
      break;
    default:
      text = what;
  }
  // #535: the turn wrote artifacts (the server's count, > 0): its end row and a done
  // file_write / editor row link to the artifacts page filtered to that turn (same origin).
  const artifacts = int(i.artifacts);
  const linked = i.kind === 'turn' ? what === 'completed' : i.kind === 'tool' && ok === true && WRITES.has(name);
  if (artifacts && linked && code(i.turn)) href = `/artifacts?turn=${encodeURIComponent(i.turn)}`;
  return { time: clock(i.ts), kind: code(i.kind) || 'other', text, ok, href, artifacts: href ? artifacts : null };
}

// The YOLO window's lines (null: no window).
export function yoloView(yolo, t) {
  if (!yolo || typeof yolo !== 'object') return null;
  const lines = [];
  const state = code(yolo.state) || 'ended';
  lines.push(t('stage.act_yolo_state', { state, tier: code(yolo.tier) || '' }));
  if (int(yolo.remaining_sec) !== null && state !== 'ended') lines.push(t('stage.act_yolo_left', { time: duration(yolo.remaining_sec, t) }));
  if (int(yolo.limit) !== null) lines.push(t('stage.act_yolo_auto', { n: int(yolo.used) || 0, limit: yolo.limit }));
  if (int(yolo.external_limit)) lines.push(t('stage.act_yolo_external', { n: int(yolo.external_used) || 0, limit: yolo.external_limit }));
  if (int(yolo.exec_limit)) lines.push(t('stage.act_yolo_exec', { n: int(yolo.exec_used) || 0, limit: yolo.exec_limit }));
  if (state === 'paused') {
    const reasons = Array.isArray(yolo.pause_breaker_reasons) ? yolo.pause_breaker_reasons.map(code).filter(Boolean) : [];
    lines.push(t('stage.act_yolo_paused', { reason: code(yolo.pause_reason) || '', detail: reasons.join(', ') }));
  }
  return lines;
}

export function totalsView(totals, t) {
  const s = totals && typeof totals === 'object' ? totals : {};
  const n = (v) => (int(v) === null ? '–' : v.toLocaleString('en-US'));
  const lines = [
    t('stage.act_totals_turns', { n: n(s.turns) }),
    t('stage.act_totals_tokens', { input: n(s.input), output: n(s.output), read: n(s.cache_read), write: n(s.cache_write) }),
  ];
  if (int(s.context_tokens) !== null) {
    const pct = int(s.context_window) ? Math.round((s.context_tokens / s.context_window) * 100) : null;
    lines.push(pct === null ? t('stage.act_totals_context', { n: n(s.context_tokens) }) : t('stage.act_totals_context_pct', { n: n(s.context_tokens), percent: pct }));
  }
  const cost = num(s.cost);
  // #451: the server prices each turn by its model's configured price; a partial sum says so.
  const key = cost === null ? 'stage.act_totals_cost_unknown' : s.cost_partial === true ? 'stage.act_totals_cost_partial' : 'stage.act_totals_cost';
  lines.push(cost === null ? t(key) : t(key, { cost: cost.toFixed(4) }));
  return lines;
}

// --- the goal grant (ADR 0011 stage C, W5b of #296) ------------------------------------------
// GET /api/activity carries `grant` only with autonomy.stage_c on (without it the key is absent
// and nothing below draws anything): null (no grant to show: nothing is drawn either), an error
// code, or the allow-listed block of activity.grant_view — codes, counts and tool names. The
// goal, the card's content and the tool arguments never reach the page. Display only: the
// controls stay in Settings → Goal grants (a plain link, no action from the board).
export const GRANTS_LINK = '/settings#sec-grants';
export const APPROVALS_LINK = '/approvals';
const GRANT_STATE_KEY = {
  active: 'stage.grant_state_active', paused: 'stage.grant_state_paused', halted: 'stage.grant_state_halted',
  revoked: 'stage.grant_state_revoked', expired: 'stage.grant_state_expired', ended: 'stage.grant_state_ended',
};
const GOAL_STATE_KEY = {
  active: 'stage.grant_goal_active', paused: 'stage.grant_goal_paused', blocked: 'stage.grant_goal_blocked',
  budget_limited: 'stage.grant_goal_budget_limited', awaiting_verification: 'stage.grant_goal_awaiting',
  complete: 'stage.grant_goal_complete', unmet: 'stage.grant_goal_unmet',
};
const RUN_KEY = {
  idle: 'stage.grant_run_idle', running: 'stage.grant_run_running', yielded: 'stage.grant_run_yielded', abandoned: 'stage.grant_run_abandoned',
};
const END_KEY = {
  done: 'stage.grant_end_done', claimed: 'stage.grant_end_claimed', reported_blocked: 'stage.grant_end_reported_blocked',
  yielded_user_turn: 'stage.grant_end_yielded', permit_stale: 'stage.grant_end_permit_stale', budget_refused: 'stage.grant_end_budget',
  token_count_unavailable: 'stage.grant_end_token_count', approval_timeout: 'stage.grant_end_approval_timeout',
  run_checks_unavailable: 'stage.grant_end_run_checks', timeout: 'stage.grant_end_timeout', model_error: 'stage.grant_end_model_error',
  tool_refused: 'stage.grant_end_tool_refused', stopped: 'stage.grant_end_stopped', shutdown: 'stage.grant_end_shutdown',
  failed: 'stage.grant_end_failed', workspace_changed: 'stage.grant_end_workspace_changed',
  no_action: 'stage.grant_end_no_action',
};
// ✓ / ✗ / · of an iteration's end (as the rows): finished as planned, failed, or stopped
const END_OK = new Set(['done', 'claimed']);
const END_FAILED = new Set(['failed', 'model_error', 'timeout', 'tool_refused', 'budget_refused', 'token_count_unavailable', 'approval_timeout', 'run_checks_unavailable', 'workspace_changed', 'permit_stale']);

// The fixed code sets of the grant state machine and the executor (W5b fix round 1: an
// allow-list of values, not of shapes; tests/test_activity_grant.py keeps them equal to the
// server's). A value outside its set is never drawn: a state or an end is "other", a reason
// or a detail is left out, a tool name is "other".
const GRANT_REASONS = new Set(['breaker_critical', 'breaker_warning', 'clock_anomaly', 'complete', 'goal_blocked', 'goal_budget_limited', 'goal_paused', 'kill_switch', 'restart', 'session_reset', 'ttl', 'unhealthy', 'user']);
const GOAL_REASONS = new Set(['approval_timeout', 'expired_ttl', 'iteration_timeout', 'loop_detected', 'max_iterations', 'missing_input', 'needs_decision', 'no_progress', 'out_of_scope', 'revoked_clock_anomaly', 'revoked_kill_switch', 'revoked_restart', 'revoked_session_reset', 'revoked_unhealthy', 'revoked_user', 'run_checks_unavailable', 'same_blocker', 'user', 'verification', 'verified', 'workspace_changed']);
const DETAIL_CODES = new Set(['authority_unhealthy', 'breaker', 'child_error', 'clock', 'containment_unverified', 'count_failed', 'disabled', 'goal_not_active', 'grant_not_active', 'grant_total', 'in_flight_cancelled', 'killed', 'max_tokens_unknown', 'no_counter', 'no_formatter', 'not_contained', 'not_in_scope', 'outside_subtree', 'path_invalid', 'per_tick', 'permit_refused', 'pool', 'provider_error', 'provider_unsupported', 'report_refused', 'setting_off', 'store_unhealthy', 'tick_stale', 'ttl']);
const SCOPE_TOOLS = new Set(['current_time', 'editor', 'file_read', 'memory_list', 'memory_read', 'memory_search', 'run_checks']);
// W6c: the claim evidence's codes (the same sets as the server's: grants/schema.py)
const CLAIM_CHECKS = new Set(['fail', 'not_run', 'pass', 'unknown']);
const CLAIM_WORKSPACE = new Set(['changed', 'unchanged', 'unknown']);
const CLAIM_PROGRESS = new Set(['new', 'none', 'repeat', 'unknown']);
const CHECK_KINDS = new Set(['compileall', 'node_test', 'other', 'pytest', 'ruff', 'unittest']);
const CHECK_STATUSES = new Set(['error', 'failed', 'limited', 'passed']);
const CHECK_COUNT_KEYS = ['passed', 'failed', 'errors', 'skipped', 'findings'];
const CLAIM_CHECKS_KEY = {
  pass: 'stage.grant_claim_checks_pass', fail: 'stage.grant_claim_checks_fail', not_run: 'stage.grant_claim_checks_not_run', unknown: 'stage.grant_claim_checks_unknown',
};
const CLAIM_WORKSPACE_KEY = {
  changed: 'stage.grant_claim_workspace_changed', unchanged: 'stage.grant_claim_workspace_unchanged', unknown: 'stage.grant_claim_workspace_unknown',
};
const CLAIM_PROGRESS_KEY = {
  new: 'stage.grant_claim_progress_new', repeat: 'stage.grant_claim_progress_repeat', none: 'stage.grant_claim_progress_none', unknown: 'stage.grant_claim_progress_unknown',
};
const CHECK_STATUS_KEY = {
  passed: 'stage.grant_check_passed', failed: 'stage.grant_check_failed', error: 'stage.grant_check_error', limited: 'stage.grant_check_limited',
};
// W6d: the heartbeat start's skip reasons (the server's: scheduler/grant_tick.py SKIP_REASONS)
const TICK_SKIPS = new Set(['abandoned_call', 'authority_unhealthy', 'awaiting_verification', 'breaker', 'budget', 'clock', 'executor_refused', 'executor_unavailable', 'flood', 'goal_state', 'grant_state', 'heartbeat_unhealthy', 'iteration_running', 'kill', 'max_iterations', 'min_spacing', 'narrowed', 'no_action', 'no_grant', 'not_enabled', 'repeated_failure', 'scheduler_stopped', 'shutdown', 'store_unhealthy', 'ttl', 'turn_busy']);
const own = (map, c) => Object.prototype.hasOwnProperty.call(map, c);

// W6d: a grant issued with the heartbeat start — how many iterations the scheduler's
// model-free tick started, the last tick's outcome, the skips by reason (the most frequent
// first, at most four) and the last heartbeat-started iteration's tokens and wall time
// (cache counters only when the provider reported them). Codes and counts only.
export function tickLines(tick, t) {
  if (!tick || typeof tick !== 'object' || tick.on !== true) return [];
  const lines = [];
  const started = int(tick.started);
  let last = t('stage.grant_tick_none');
  if (tick.last === 'started') last = t('stage.grant_tick_started');
  else if (TICK_SKIPS.has(code(tick.last))) last = t(`grants.tick_skip_${tick.last}`);
  else if (code(tick.last)) last = t('stage.act_reason_other', { code: 'other' });
  lines.push(t('stage.grant_tick', { n: started === null ? 0 : started, last }));
  if (tick.unavailable === 'heartbeat_off' || tick.unavailable === 'scheduler_off') lines.push(t(`grants.tick_unavailable_${tick.unavailable}`));
  const skipped = tick.skipped && typeof tick.skipped === 'object' ? tick.skipped : {};
  const rows = Object.keys(skipped).filter((r) => TICK_SKIPS.has(r) && int(skipped[r]) !== null)
    .sort((a, b) => skipped[b] - skipped[a] || (a < b ? -1 : 1)).slice(0, 4)
    .map((r) => `${t(`grants.tick_skip_${r}`)} ×${skipped[r]}`);
  if (rows.length) lines.push(t('stage.grant_tick_skipped', { list: rows.join(', ') }));
  const cost = tick.cost && typeof tick.cost === 'object' ? tick.cost : null;
  if (cost && int(cost.tokens_in) !== null && int(cost.tokens_out) !== null) {
    const ms = int(cost.wall_ms);
    const time = ms === null ? '–' : duration(Math.round(ms / 1000), t);
    lines.push(t('stage.grant_tick_cost', { in: cost.tokens_in, out: cost.tokens_out, time }));
    if (int(cost.cache_read) !== null || int(cost.cache_write) !== null) {
      lines.push(t('stage.grant_tick_cache', { read: int(cost.cache_read) ?? 0, write: int(cost.cache_write) ?? 0 }));
    }
  }
  return lines;
}

function codeText(map, value, t) {
  const c = code(value);
  if (!c) return '';
  return own(map, c) ? t(map[c]) : t('stage.act_reason_other', { code: 'other' });
}

function withReason(text, reason, known) {
  const r = code(reason);
  return r && known.has(r) ? `${text} (${r})` : text;
}

export function endText(end, t) {
  return codeText(END_KEY, end, t);
}

// -> null (nothing to draw) | { code, unknown, title, headline, lines, ttl, budget, cue, outcomes, announce }
// now: server seconds (the TTL counts down between reads from the answer's server_time).
// stale (W5b round 2): ``grant`` is the last readable block, shown because this answer was
// grant_busy (a change was being written): the same panel, marked as the last read's values
// — its announcement is the one already made, so a single busy read says nothing new.
export function grantView(grant, now, serverTime, t, offline = false, stale = false) {
  if (grant === null || grant === undefined) return null;
  const title = t('stage.grant_h');
  if (offline || typeof grant !== 'object' || code(grant.error)) {
    const text = t(offline ? 'stage.grant_unknown_offline' : 'stage.grant_unknown');
    return { code: 'unknown', unknown: true, title, headline: text, lines: [], ttl: null, budget: null, cue: null, outcomes: [], announce: text };
  }
  const id = typeof grant.id === 'string' && /^[0-9a-f]{8}$/.test(grant.id) ? grant.id : null;
  const head = grant.headline && typeof grant.headline === 'object' ? grant.headline : {};
  const state = own(GRANT_STATE_KEY, code(grant.state)) ? grant.state : 'other';
  let headline;
  if (head.subject === 'goal') headline = withReason(codeText(GOAL_STATE_KEY, head.state, t), head.reason, GOAL_REASONS);
  else headline = withReason(codeText(GRANT_STATE_KEY, head.state || state, t), head.reason || grant.reason, GRANT_REASONS);
  const lines = [];
  const goal = codeText(GOAL_STATE_KEY, grant.goal_state, t);
  if (goal) lines.push(t('stage.grant_goal', { state: withReason(goal, grant.goal_reason, GOAL_REASONS) }));
  const it = grant.iteration && typeof grant.iteration === 'object' ? grant.iteration : null;
  const run = it ? (own(RUN_KEY, code(it.run)) ? it.run : 'other') : null;
  // while one runs, its own number (the grant counts an iteration at its end)
  const done = run === 'running' && int(it.number) ? it.number : int(grant.iterations);
  const max = int(grant.max_iterations);
  if (max !== null) {
    const count = t('stage.grant_iterations', { n: done === null ? '–' : done, max });
    lines.push(run ? `${count} · ${codeText(RUN_KEY, run, t)}` : count);
  }
  if (it && run !== 'running' && code(it.end)) lines.push(t('stage.grant_last_stop', { reason: withReason(endText(it.end, t), it.detail, DETAIL_CODES) }));
  lines.push(...tickLines(grant.tick, t));
  const live = grant.live === true;
  let ttl = null;
  const left0 = int(grant.ttl_left_sec);
  const total = int(grant.ttl_sec);
  if (live && left0 !== null) {
    const passed = elapsed(serverTime, now) || 0;
    const left = Math.max(0, left0 - passed);
    ttl = { left, total, text: t('stage.grant_ttl', { time: duration(left, t) }) };
  }
  let budget = null;
  const bLeft = int(grant.budget_left);
  const bTotal = int(grant.budget_total);
  if (bLeft !== null && bTotal !== null) {
    const n = (v) => v.toLocaleString('en-US');
    budget = { left: bLeft, total: bTotal, text: t('stage.grant_budget', { n: n(bLeft), total: n(bTotal) }) };
    const tick = it ? int(it.tick_used) : null;
    const per = int(grant.budget_per_tick);
    if (tick !== null && per !== null) budget.tick = t('stage.grant_budget_tick', { n: n(tick), total: n(per) });
  }
  const waiting = live && grant.awaiting_verification === true;
  const claim = claimView(grant.claim, waiting, elapsed(serverTime, now) || 0, t);
  let cue = null;
  if (live && grant.card === true) cue = { kind: 'approval', text: t('stage.grant_cue_approval'), link: t('stage.grant_cue_approval_link'), href: APPROVALS_LINK };
  // (no seconds in a cue: the cue is announced, the seconds never are — they are in the since line)
  else if (waiting && claim && claim.stale) cue = { kind: 'verify_stale', text: t('stage.grant_cue_verify_stale'), link: t('stage.grant_link'), href: GRANTS_LINK };
  else if (waiting) cue = { kind: 'verify', text: t('stage.grant_cue_verify'), link: t('stage.grant_link'), href: GRANTS_LINK };
  const outcomes = (Array.isArray(grant.outcomes) ? grant.outcomes : []).slice(-5).reverse().map((o) => outcomeView(o, t));
  const runText = run === 'running' ? codeText(RUN_KEY, run, t) : '';
  const announce = [headline, runText, cue ? cue.text : '', waiting && claim && claim.amber ? claim.warn : ''].filter(Boolean).join(' · ');
  if (stale) lines.push(t('stage.grant_stale'));
  return { code: run === 'running' ? 'running' : state, unknown: false, stale, title: id ? `${title} ${id}` : title, headline, lines, ttl, budget, cue, claim, outcomes, announce };
}

// W6c: the evidence of the agent's completion claim (codes and observed counts only, no
// percentage): "claimed at iteration k/max · checks · workspace · progress", the per-check
// rows, the tokens and TTL left at the claim; while it waits (live, awaiting verification)
// "since <time>" and — past the server's threshold (autonomy.claim_stale_sec), counted on
// between reads from server_time like the TTL, no timer of its own — "still waiting for you".
// A claim whose checks failed is amber with a "⚠" line in words (not colour alone). After
// the person confirmed it is shown read-only (the audit trail). -> null | { ... }
export function claimView(c, waiting, passed, t) {
  if (!c || typeof c !== 'object') return null;
  const pick = (set, v) => (typeof v === 'string' && set.has(v) ? v : 'unknown');
  let checks = pick(CLAIM_CHECKS, c.checks);
  let rows = [];
  if (checks === 'pass' || checks === 'fail') {
    const raw = Array.isArray(c.check_rows) ? c.check_rows : [];
    const ok = raw.length > 0 && raw.length <= 16 && raw.every((r) => r && typeof r === 'object' && CHECK_STATUSES.has(r.status)
      && r.counts && typeof r.counts === 'object' && CHECK_COUNT_KEYS.every((k) => int(r.counts[k]) !== null));
    // a row that cannot be read, or a "pass" with a row that did not pass: unknown, never pass
    if (!ok || (checks === 'pass') !== raw.every((r) => r.status === 'passed')) checks = 'unknown';
    else rows = raw.map((r) => t('stage.grant_claim_row', {
      kind: CHECK_KINDS.has(r.kind) ? r.kind : 'other', status: t(CHECK_STATUS_KEY[r.status]),
      passed: r.counts.passed, failed: r.counts.failed, errors: r.counts.errors, skipped: r.counts.skipped, findings: r.counts.findings,
    }));
  }
  const workspace = pick(CLAIM_WORKSPACE, c.workspace);
  const progress = pick(CLAIM_PROGRESS, c.progress);
  const k = int(c.iteration);
  const max = int(c.max_iterations);
  const lines = [];
  lines.push(t('stage.grant_claim_line', {
    n: k === null ? '–' : k,
    max: max === null ? '–' : max,
    checks: t(CLAIM_CHECKS_KEY[checks]),
    workspace: t(CLAIM_WORKSPACE_KEY[workspace]),
    progress: t(CLAIM_PROGRESS_KEY[progress]),
  }));
  const used = int(c.used_tokens);
  const left = int(c.budget_left);
  const ttl = int(c.ttl_left_sec);
  if (used !== null && left !== null && ttl !== null) {
    const n = (v) => v.toLocaleString('en-US');
    lines.push(t('stage.grant_claim_at', { used: n(used), left: n(left), time: duration(ttl, t) }));
  }
  let since = null;
  let waitText = '';
  let stale = false;
  const wait0 = int(c.waiting_sec);
  if (waiting) {
    since = t('stage.grant_claim_since', { time: clock(c.claimed_at) });
    if (wait0 !== null) {
      const wait = wait0 + passed;
      waitText = duration(wait, t);
      since = t('stage.grant_claim_since_for', { time: clock(c.claimed_at), wait: waitText });
      const after = int(c.stale_after_sec);
      stale = c.stale === true || (after !== null && wait >= after);
    } else stale = c.stale === true;
  }
  const amber = checks === 'fail';
  return {
    checks, workspace, progress, lines, rows, since, stale, waitText, amber,
    audit: !waiting, title: t(waiting ? 'stage.grant_claim_h' : 'stage.grant_claim_audit'),
    warn: amber ? t('stage.grant_claim_fail') : '',
  };
}

function outcomeView(o, t) {
  const i = o && typeof o === 'object' ? o : {};
  const what = own(END_KEY, code(i.what)) ? i.what : 'other';
  const ms = int(i.duration_ms);
  const n = int(i.number);
  const tools = (Array.isArray(i.tools) ? i.tools : []).map((x) => (SCOPE_TOOLS.has(code(x)) ? x : 'other')).slice(0, 8);
  const parts = [n === null ? '' : `#${n}`, withReason(endText(what, t), i.detail, DETAIL_CODES), ms === null ? '' : duration(Math.round(ms / 1000), t), tools.join(', ')];
  return { time: clock(i.ts), ok: END_OK.has(what) ? true : END_FAILED.has(what) ? false : null, text: parts.filter(Boolean).join(' · ') };
}

const isBusy = (g) => Boolean(g && typeof g === 'object' && g.error === 'grant_busy');
// W5b round 3: a grant block that says nothing readable — grant_busy, grant_unreadable, any
// error (null = no grant is readable; no key = stage C off). Such an answer never edits the
// watch's history (no start, no end) and never resets the busy-retention counters.
export function isUnreadableGrant(g) {
  return g !== null && g !== undefined && (typeof g !== 'object' || g.error !== undefined);
}
// How long a busy answer may keep showing the last readable panel ("Updating"): at most this
// many unreadable answers in a row, and this many seconds (poll clock) since the last readable
// block — whichever comes first; then the panel is "unknown" (no numbers, not running).
export const BUSY_KEEP_READS = 3;
export const BUSY_KEEP_SEC = 20;

// A4: may a goal iteration lower the board by itself? Only at its start (the edge), and only
// onto a board that is up for nothing: a reply (pinned or not), the activity, a board still
// rolling, the person's own turn, the open log or a hidden tab all mean "no". A pinned board
// (the pin is board-level and survives a roll-up) is never taken either: the person's next
// reply must not find the board held by the activity. Never focus, never the log, never a
// pin. -> 'open' | 'none' (docs/stage.md, "Activity on the board").
export function attention({ running, wasRunning, boardVisible, pinned, turnOpen, logOpen, hidden }) {
  if (running !== true || wasRunning === true) return 'none';
  if (hidden || boardVisible || pinned !== false || turnOpen || logOpen) return 'none';
  return 'open';
}

// A5: what one watch read tells the poll loop (poll.js: false widens the interval). The board
// shows the activity: the feed's own answer; the board is closed (watch only): "something
// changed" only while an iteration runs, so a closed board is read at the idle cadence.
export function watchResult(result, { showing, running }) {
  if (result === null || showing) return result;
  return running === true;
}

// Everything the board draws, from one snapshot. snap: { body (last answer), items, offline }
export function activityModel(snap, now, t) {
  const body = (snap && snap.body) || {};
  const stallSec = int(body.stall_sec) || STALL_SEC;
  const model = {
    state: stateText(body.state, now, t, stallSec, Boolean(snap && snap.offline)),
    rows: (snap && Array.isArray(snap.items) ? snap.items : []).slice().reverse().map((i) => rowView(i, t)),
    yolo: yoloView(body.yolo, t),
    totals: totalsView(body.totals, t),
  };
  // W5b: only an answer that has the key (stage C on); off, the model is the one of before
  if (Object.prototype.hasOwnProperty.call(body, 'grant')) {
    const last = snap && snap.lastGrant;
    const offline = Boolean(snap && snap.offline);
    if (snap && snap.retained && last) model.grant = grantView(last.grant, now, last.server_time, t, false, true);
    else model.grant = grantView(body.grant, now, body.server_time, t, offline);
  }
  return model;
}

export class ActivityFeed {
  // getJSON(url) -> Promise<object> (throws on an HTTP error); wall() -> unix seconds
  constructor({ getJSON, wall }) {
    this.getJSON = getJSON;
    this.wall = wall;
    this.body = null;
    this.items = [];
    this.cursor = 0;
    this.skew = 0;
    this.fails = 0;
    this.key = '';
    this.lastGrant = null; // W5b: { grant, server_time, at (poll clock), unreadable (answers since) }
  }

  get offline() {
    return this.fails >= 2;
  }

  get busy() {
    const c = this.body && this.body.state && this.body.state.code;
    return BUSY.has(c) || this.iterationRunning;
  }

  // W5b: a goal iteration runs (the answer's grant block — after a busy one the last
  // readable block; never while offline)
  get iterationRunning() {
    let g = this.body && this.body.grant;
    if (isUnreadableGrant(g)) g = this.retained ? this.lastGrant.grant : null;
    return !this.offline && Boolean(g && g.iteration && g.iteration.run === 'running');
  }

  // W5b round 3: the last readable panel stands in for a busy answer — within the limits
  get retained() {
    const g = this.body && this.body.grant;
    const last = this.lastGrant;
    if (!isBusy(g) || !last || this.offline) return false;
    return last.unreadable <= BUSY_KEEP_READS && this.wall() - last.at <= BUSY_KEEP_SEC;
  }

  serverNow() {
    return this.wall() + this.skew;
  }

  // -> true (something changed), false (nothing new), null (could not be read: backoff)
  async poll() {
    let body;
    try {
      body = await this.getJSON(`/api/activity?after_id=${this.cursor}`);
    } catch {
      this.fails += 1;
      return null;
    }
    if (!body || typeof body !== 'object') {
      this.fails += 1;
      return null;
    }
    this.fails = 0;
    const prev = this.body;
    if (!body.reset && prev && num(prev.started_at) !== null && num(body.started_at) !== null && body.started_at !== prev.started_at) {
      // W5b audit D3: the server restarted (perhaps on another data dir): the rows after this
      // cursor may be another store's, so the list is read from scratch (one more read).
      this.cursor = 0;
      this.items = [];
      this.body = null;
      return this.poll();
    }
    if (num(body.server_time) !== null) this.skew = body.server_time - this.wall();
    this.items = mergeItems(this.items, body);
    if (Number.isInteger(body.cursor) && body.cursor >= 0) this.cursor = body.cursor;
    this.body = body;
    if ('grant' in body) {
      const g = body.grant;
      // only a readable block resets the counters; an unreadable answer only counts
      if (!isUnreadableGrant(g)) this.lastGrant = g ? { grant: g, server_time: body.server_time, at: this.wall(), unreadable: 0 } : null;
      else if (this.lastGrant) this.lastGrant.unreadable += 1;
    }
    const parts = [body.state, body.yolo && { ...body.yolo, remaining_sec: undefined }, body.totals, this.items.length && this.items[this.items.length - 1].id];
    // W5b: the grant block (stage C only; its TTL moves on every read, like the window's —
    // and, W6c, a waiting claim's seconds: neither is news that keeps the poll fast)
    if ('grant' in body) parts.push(body.grant && { ...body.grant, ttl_left_sec: undefined, claim: body.grant.claim && { ...body.grant.claim, waiting_sec: undefined } });
    const key = JSON.stringify(parts);
    const changed = key !== this.key || (body.items && body.items.length > 0);
    this.key = key;
    return changed || this.busy;
  }

  snapshot() {
    return { body: this.body, items: this.items, offline: this.offline, lastGrant: this.lastGrant, retained: this.retained };
  }
}

// --- the controller (#446; W5b of #296) ------------------------------------------------------
// The board's activity mode, wired: main.js passes the page's objects in (tested in Node with
// jsdom: tests/stage_activity_ctl_test.cjs, #624). Kept in this module so that the page loads
// the same module graph as before W5b (no extra request with the switches off).
//
// One read-only source (GET /api/activity, ActivityFeed), polled through poll.js only while
// the activity is on the board — or, with the watch (data-grants="exec": the goal iteration
// executor is on), also while the board is closed, at the idle cadence (4 s while an
// iteration runs), so the start of an iteration can lower the board (attention).
// Without the watch nothing reads with the board closed and nothing extra is drawn. The
// seconds tick once a second without a request.

// ctx: { board (board.js), btn (the Activity button), doc, poll (StiraPoll), getJSON(url),
//   wall() -> unix s, t(key, vars), watch (bool), atLoad (?board=activity), status() -> the
//   live feed's status | null, logOpen() -> bool, idle: { get() -> bool },
//   setInterval, setTimeout }. Started once per page and never stopped (the page's life is
//   the controller's), so it returns nothing.
export function startActivityController(ctx) {
  const { board, btn, doc, t, watch } = ctx;
  const feed = new ActivityFeed({ getJSON: ctx.getJSON, wall: ctx.wall });
  let poller = null;
  let wasOpen = null; // the followed turn was open at the last check (idle default)
  let wasRunning = false; // a goal iteration ran at the last read (watch only)
  const model = () => activityModel(feed.snapshot(), feed.serverNow(), t);
  const announce = (m) => (m.grant ? m.grant.announce : undefined);
  const foot = () => (feed.body ? t('stage.act_updated', { time: clock(feed.body.server_time) }) : t('stage.act_loading'));
  const sync = () => btn.setAttribute('aria-pressed', String(board.showingActivity));
  const stop = () => {
    if (poller) poller.stop();
    poller = null;
    sync();
  };
  const draw = () => {
    if (!board.showingActivity) return watch ? sync() : stop();
    if (doc.hidden) return;
    const m = model();
    board.updateActivity(renderActivity(doc, m, t), foot(), announce(m));
  };
  const show = (explicit) => {
    const m = model();
    return board.showActivity(renderActivity(doc, m, t), { explicit, foot: foot(), announce: announce(m) });
  };
  // after each watch read: the start of an iteration may lower the board (never focus, the
  // log or a pin; never onto a board that is up or pinned: docs/stage.md, the A4 table)
  const notice = () => {
    // an unreadable grant block (busy, unreadable, any error) tells nothing: the watch's
    // history stays exactly as it was — no start, no end, no attention
    if (isUnreadableGrant(feed.body && feed.body.grant)) return wasRunning;
    const running = feed.iterationRunning;
    const status = ctx.status();
    const verdict = attention({ running, wasRunning, boardVisible: board.visible, pinned: board.pinned, turnOpen: Boolean(status && status.turnOpen), logOpen: ctx.logOpen(), hidden: doc.hidden });
    wasRunning = running;
    if (verdict === 'open' && show(false) !== 'held') sync();
    return running;
  };
  const ensure = () => {
    if (poller) return false;
    poller = ctx.poll.create({
      run: async () => {
        const result = await feed.poll();
        const running = watch && result !== null ? notice() : false;
        draw();
        if (result === null) throw new Error('activity: not read'); // -> backoff
        return watch ? watchResult(result, { showing: board.showingActivity, running }) : result;
      },
      base: POLL.base * 1000,
      max: POLL.max * 1000,
      backoffMax: POLL.backoffMax * 1000,
      hold: () => (watch && !board.showingActivity ? feed.iterationRunning : feed.busy),
      doc,
    });
    poller.start();
    return true;
  };
  const open = (explicit) => {
    if (show(explicit) === 'held') return;
    sync();
    if (!ensure()) poller.poke();
  };
  if (watch) ensure();
  btn.addEventListener('click', () => {
    if (board.showingActivity) board.close();
    else open(true);
  });
  // ?board=activity (captures and checks, like ?log=1): the activity on the board at load
  if (ctx.atLoad) ctx.setTimeout(() => open(false), 300);
  // once a second, no request: the elapsed seconds, the button, and the idle default
  ctx.setInterval(() => {
    if (poller) draw();
    else sync();
    const status = ctx.status();
    if (!status || doc.hidden) return;
    const turnOpen = Boolean(status.turnOpen);
    if (!turnOpen && wasOpen !== false && ctx.idle.get() && !board.visible) open(false);
    wasOpen = turnOpen;
  }, 1000);
}

// --- DOM -------------------------------------------------------------------------------------

function el(doc, tag, cls, text) {
  const n = doc.createElement(tag);
  if (cls) n.className = cls;
  if (text !== undefined && text !== null) n.textContent = text;
  return n;
}

// The board body's dynamic part (replaced on every refresh).
export function renderActivity(doc, model, t) {
  const out = [];
  const now = el(doc, 'section', 'act-now');
  now.dataset.state = model.state.code;
  now.append(el(doc, 'h3', 'act-h', t('stage.act_now')), el(doc, 'p', 'act-state', model.state.text));
  if (model.state.stall) {
    const warn = el(doc, 'p', 'act-stall', model.state.stallText);
    now.append(warn);
  }
  out.push(now);
  if (model.grant) out.push(renderGrant(doc, model.grant, t));
  const recent = el(doc, 'section', 'act-recent');
  recent.append(el(doc, 'h3', 'act-h', t('stage.act_recent')));
  if (!model.rows.length) recent.append(el(doc, 'p', 'act-empty', t('stage.act_none')));
  else {
    const ol = el(doc, 'ol', 'act-list');
    for (const r of model.rows) {
      const li = el(doc, 'li', 'act-row');
      li.dataset.kind = r.kind;
      li.dataset.ok = r.ok === null ? '' : String(r.ok);
      const mark = r.ok === null ? '·' : r.ok ? '✓' : '✗';
      li.append(el(doc, 'span', 'act-time', r.time), el(doc, 'span', 'act-mark', mark), el(doc, 'span', 'act-text', r.text));
      if (r.href) {
        const a = el(doc, 'a', 'act-link', t('stage.act_artifacts_n', { n: r.artifacts }));
        a.href = r.href;
        a.target = '_blank';
        a.rel = 'noopener noreferrer';
        li.append(a);
      }
      ol.append(li);
    }
    recent.append(ol);
  }
  out.push(recent);
  const side = el(doc, 'section', 'act-side');
  const yolo = el(doc, 'div', 'act-group');
  yolo.append(el(doc, 'h3', 'act-h', t('stage.act_yolo')));
  if (model.yolo) for (const line of model.yolo) yolo.append(el(doc, 'p', 'act-line', line));
  else yolo.append(el(doc, 'p', 'act-line act-muted', t('stage.act_yolo_none')));
  const totals = el(doc, 'div', 'act-group');
  totals.append(el(doc, 'h3', 'act-h', t('stage.act_totals')));
  for (const line of model.totals) totals.append(el(doc, 'p', 'act-line', line));
  side.append(yolo, totals);
  out.push(side);
  return out;
}

function link(doc, text, href) {
  const a = el(doc, 'a', 'act-link', text);
  a.href = href; // same origin, a fixed path (no action from the board)
  return a;
}

// W6c: the claim evidence (a group in the grant panel; read-only, no control). Amber with a
// "⚠" line in words when its checks failed; "still waiting for you" past the threshold.
export function renderClaim(doc, c) {
  const box = el(doc, 'div', 'act-claim');
  box.dataset.checks = c.checks;
  if (c.amber) box.dataset.tone = 'amber';
  if (c.stale) box.dataset.stale = '1';
  if (c.audit) box.dataset.audit = '1';
  box.append(el(doc, 'p', 'act-line act-claim-h', c.title));
  if (c.since) box.append(el(doc, 'p', 'act-line', c.since));
  for (const line of c.lines) box.append(el(doc, 'p', 'act-line', line));
  if (c.warn) box.append(el(doc, 'p', 'act-line act-claim-warn', c.warn));
  if (c.rows.length) {
    const ul = el(doc, 'ul', 'act-checks');
    for (const row of c.rows) ul.append(el(doc, 'li', 'act-line', row));
    box.append(ul);
  }
  return box;
}

// The grant panel (W5b): a labelled region; its status changes are announced by the board's
// one polite live region (board.js), not by this node, which is rebuilt on every refresh.
export function renderGrant(doc, g, t) {
  const box = el(doc, 'section', 'act-grant');
  box.dataset.state = g.code;
  if (g.stale) box.dataset.stale = '1';
  box.setAttribute('aria-labelledby', 'act-grant-h');
  const h = el(doc, 'h3', 'act-h', g.title);
  h.id = 'act-grant-h';
  box.append(h, el(doc, 'p', 'act-state', g.headline));
  for (const line of g.lines) box.append(el(doc, 'p', 'act-line', line));
  const bar = (cls, label, value, max, text) => {
    const p = el(doc, 'p', `act-line ${cls}`);
    if (max) {
      const m = el(doc, 'meter', 'act-meter');
      m.setAttribute('min', '0');
      m.setAttribute('max', String(max));
      m.setAttribute('value', String(Math.min(value, max)));
      m.setAttribute('aria-label', label);
      p.append(m, doc.createTextNode(' '));
    }
    p.append(doc.createTextNode(text));
    return p;
  };
  if (g.ttl) box.append(bar('act-ttl', t('stage.grant_ttl_label'), g.ttl.left, g.ttl.total, g.ttl.text));
  if (g.budget) {
    box.append(bar('act-budget', t('stage.grant_budget_label'), g.budget.left, g.budget.total, g.budget.text));
    if (g.budget.tick) box.append(el(doc, 'p', 'act-line act-muted', g.budget.tick));
  }
  if (g.cue) {
    const cue = el(doc, 'p', 'act-cue', g.cue.text);
    cue.dataset.cue = g.cue.kind;
    cue.append(doc.createTextNode(' '), link(doc, g.cue.link, g.cue.href));
    box.append(cue);
  }
  if (g.claim) box.append(renderClaim(doc, g.claim));
  if (g.outcomes.length) {
    const ol = el(doc, 'ol', 'act-list act-outcomes');
    ol.setAttribute('aria-label', t('stage.grant_outcomes'));
    for (const o of g.outcomes) {
      const li = el(doc, 'li', 'act-row');
      li.dataset.kind = 'iteration';
      li.dataset.ok = o.ok === null ? '' : String(o.ok);
      li.append(el(doc, 'span', 'act-time', o.time), el(doc, 'span', 'act-mark', o.ok === null ? '·' : o.ok ? '✓' : '✗'), el(doc, 'span', 'act-text', o.text));
      ol.append(li);
    }
    box.append(ol);
  }
  if (!g.unknown && !(g.cue && g.cue.href === GRANTS_LINK)) {
    const p = el(doc, 'p', 'act-line act-muted');
    p.append(link(doc, t('stage.grant_link'), GRANTS_LINK));
    box.append(p);
  }
  return box;
}
