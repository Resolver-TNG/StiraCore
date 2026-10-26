// Event adapter (#341, stage 2): Stira's read-only APIs -> Stage events. Pure functions only
// (no DOM, no fetch, no clock): the same inputs always give the same events, so the mapping
// is tested in Node (tests/stage_adapter_test.cjs).
//
// Inputs, all same-origin and already masked by the server:
//   GET /api/gateway/status          -> neural_state (6 numbers), breaker.level
//   GET /api/turns?limit=N           -> turn_id, started_at, completed, error
//   GET /api/turns/<id>/events       -> the turn contract (#186): turn.started,
//                                       tool_call.created/updated, approval.*,
//                                       breaker.transition, turn.summary, turn.completed
//   GET /api/hitl/pending            -> pending approvals (masked arguments)
// Only whitelisted fields are read. Tool previews, prompt/response text, paths, the SOUL
// path and credentials are never copied into a Stage event: the scene and the narration
// can show the tool *name* only. The masked arguments of an approval stay in the approval
// record and are shown only when the person asks for them (choice window).

export const DIMS = ['trust', 'curiosity', 'vigilance', 'attachment', 'stability', 'satisfaction'];
const NAME_MAX = 40;

const clip = (v, n = NAME_MAX) => (typeof v === 'string' ? v.slice(0, n) : '');
const num = (v) => typeof v === 'number' && Number.isFinite(v);

// --- tool names -> Stage kinds ---------------------------------------------------------------
// Stage kinds (model.TOOL_KINDS): memory = the shelf, web = the window, tool = the desk.
const MEMORY_TOOLS = new Set(['memory_read', 'memory_write', 'memory_search', 'memory_list', 'memory_delete', 'retrieve_offloaded_content']);
const WEB_TOOLS = new Set(['web_search', 'web_fetch', 'perplexity_search', 'http_request']);
const MEMORY_WRITES = new Set(['memory_write', 'memory_delete']);
// Finer category for gadgets (hint only; the model reads `kind`).
const CODE_TOOLS = new Set(['file_read', 'file_write', 'file_edit', 'editor', 'lsp_diagnostics', 'lsp_hover', 'python_repl', 'shell', 'shell_allowlist', 'batch']);
const AGENT_TOOLS = new Set(['use_agent', 'swarm', 'graph', 'sessions_spawn']);

export function toolKind(name) {
  const n = clip(name);
  if (MEMORY_TOOLS.has(n) || n.startsWith('memory_')) return 'memory';
  if (WEB_TOOLS.has(n) || n.startsWith('web_')) return 'web';
  return 'tool';
}

export function toolCategory(name) {
  const n = clip(name);
  const kind = toolKind(n);
  if (kind !== 'tool') return kind;
  if (CODE_TOOLS.has(n) || n.startsWith('lsp_') || n.startsWith('file_')) return 'code';
  if (AGENT_TOOLS.has(n)) return 'subagent';
  return 'other';
}

export function memoryOp(name) {
  if (toolKind(name) !== 'memory') return null;
  return MEMORY_WRITES.has(clip(name)) ? 'write' : 'read';
}

// --- breaker -------------------------------------------------------------------------------
// Stage draws three looks. COHERENCE (bcScore low) restricts like WARNING and is drawn with
// the WARNING look; the raw level is kept for the raw view and the narration.
// #455: why the level changed — codes (short identifiers, never shown as is) and numbers.
const REASON = /^[a-z_]{1,48}$/;
export function breakerReasons(d) {
  const reasons = Array.isArray(d.reasons) ? d.reasons.filter((c) => typeof c === 'string' && REASON.test(c)).slice(0, 8) : [];
  if (!reasons.length) return {};
  const inputs = {};
  if (d.inputs && typeof d.inputs === 'object') {
    for (const [k, v] of Object.entries(d.inputs)) if (REASON.test(k) && num(v)) inputs[k] = v;
  }
  return { reasons, inputs };
}

export function breakerLevel(raw) {
  const value = raw && typeof raw === 'object' ? raw.level ?? raw.value : raw;
  if (typeof value !== 'string') return null;
  const v = value.toLowerCase();
  if (v === 'normal') return { level: 'NORMAL', raw: 'normal' };
  if (v === 'warning') return { level: 'WARNING', raw: 'warning' };
  if (v === 'critical') return { level: 'CRITICAL', raw: 'critical' };
  if (v === 'coherence') return { level: 'WARNING', raw: 'coherence' };
  return null;
}

// --- /api/gateway/status -> neural + breaker (only on change) --------------------------------
export function neuralValues(source) {
  if (!source || typeof source !== 'object') return null;
  const out = {};
  for (const d of DIMS) if (num(source[d])) out[d] = Math.round(source[d] * 1000) / 1000;
  return Object.keys(out).length ? out : null;
}

// prev: { neuralKey, breakerRaw } from the last call. Returns { events, memo, notes }.
export function neuralKey(values) {
  return DIMS.map((d) => (values && d in values ? values[d].toFixed(3) : '-')).join(',');
}

export function fromStatus(status, prev = {}) {
  const events = [];
  const notes = [];
  const memo = { ...prev };
  if (!status || typeof status !== 'object') return { events, memo, notes };
  const values = neuralValues(status.neural_state);
  if (values) {
    const key = neuralKey(values);
    if (key !== prev.neuralKey) {
      events.push({ type: 'neural', values, source: 'status' });
      memo.neuralKey = key;
    }
  }
  const b = breakerLevel(status.breaker);
  if (b && b.raw !== prev.breakerRaw) {
    events.push({ type: 'breaker', level: b.level, raw: b.raw, source: 'status' });
    if (b.raw === 'coherence') notes.push({ key: 'stage.log_coherence' });
    memo.breakerRaw = b.raw;
  }
  return { events, memo, notes };
}

// --- one turn-contract event -> Stage events ------------------------------------------------
const ENDED = new Set(['done', 'error', 'denied', 'timeout']);

export function fromTurnEvent(ev) {
  if (!ev || typeof ev !== 'object' || typeof ev.type !== 'string') return [];
  const d = ev.data && typeof ev.data === 'object' ? ev.data : {};
  const turn = clip(ev.turn_id, 64);
  const seq = Number.isInteger(ev.seq) ? ev.seq : null;
  const at = { turn, seq };
  switch (ev.type) {
    case 'turn.started':
      return [{ type: 'turn.start', ...at }];
    case 'tool_call.created': {
      const id = clip(d.tool_use_id, 64);
      const name = clip(d.name);
      if (!id) return [];
      const tool = { id, name, kind: toolKind(name), category: toolCategory(name), op: memoryOp(name) };
      // A call refused at the gate is created as "denied": it never runs.
      if (d.status === 'denied') return [{ type: 'tool.start', ...tool, ...at }, { type: 'tool.end', id, status: 'denied', ...at }];
      return [{ type: 'tool.start', ...tool, ...at }];
    }
    case 'tool_call.updated': {
      const id = clip(d.tool_use_id, 64);
      if (!id || !ENDED.has(d.status)) return [];
      return [{ type: 'tool.end', id, status: d.status, ...at }];
    }
    case 'breaker.transition': {
      const b = breakerLevel(d.to);
      if (!b) return [];
      const why = breakerReasons(d);
      return [{ type: 'breaker', level: b.level, raw: b.raw, ...why, ...at }];
    }
    case 'turn.summary': {
      const values = neuralValues(d.after);
      return values ? [{ type: 'neural', values, ...at }] : [];
    }
    case 'turn.completed':
      // #416: `cut` only as the one fixed value (a cut before the reply began), never free text.
      return [{ type: 'turn.end', ok: !d.error, stop: clip(d.stop, 20), ...(d.cut === 'before_reply' ? { cut: 'before_reply' } : {}), ...at }];
    // approval.requested / resolved: the pending queue (/api/hitl/pending) is the source of
    // truth for the choice window (it also holds requests restored after a restart).
    default:
      return [];
  }
}

// --- /api/turns -> which turn to follow ----------------------------------------------------
// processStart: server time when this server process started (server_time - uptime). A turn
// that started before it and never completed was cut by the restart: it is not open.
export function turnIsOpen(turn, processStart) {
  if (!turn || typeof turn !== 'object' || turn.completed) return false;
  if (num(processStart) && num(turn.started_at) && turn.started_at < processStart - 2) return false;
  return true;
}

export function latestTurn(list) {
  const turns = Array.isArray(list) ? list : list && Array.isArray(list.turns) ? list.turns : [];
  const t = turns.find((x) => x && typeof x.turn_id === 'string');
  return t || null;
}

// --- /api/hitl/pending -> approval events ---------------------------------------------------
// The choice window shows one request at a time: the oldest pending one.
export function approvalRecord(r) {
  if (!r || typeof r !== 'object' || typeof r.id !== 'string') return null;
  return {
    id: clip(r.id, 80),
    tool: clip(r.tool_name) || '?',
    argsHash: typeof r.args_hash === 'string' ? r.args_hash : null,
    // masked by the server (mask_secrets); shown only on request
    args: typeof r.normalized_input === 'string' ? r.normalized_input : '',
    reason: typeof r.reason === 'string' ? r.reason.slice(0, 300) : null,
    reasonCode: typeof r.reason_code === 'string' ? r.reason_code : null,
    breakLevel: typeof r.break_level === 'string' ? r.break_level : null,
    expiresAt: num(r.expires_at) ? r.expires_at : null,
    createdAt: num(r.created_at) ? r.created_at : null,
    masked: r.masked === true,
    restored: r.restored === true,
    allowAgain: allowAgainView(r.allow_again),
  };
}

// ADR 0025: the server's allow_again view of a card, reduced to what the choice window uses
// (the class text is the server's; the page computes none).
const ALLOW_KINDS = new Set(['dir_subtree', 'host', 'host_read', 'layer', 'tool']);
export function allowAgainView(v) {
  if (!v || typeof v !== 'object') return null;
  if (v.offered === true && typeof v.token === 'string' && v.token && ALLOW_KINDS.has(v.class_kind)) {
    return {
      offered: true,
      token: v.token.slice(0, 128),
      class_kind: v.class_kind,
      label: typeof v.label === 'string' ? v.label.slice(0, 200) : '',
      whole_root: v.whole_root === true,
      max_hours: Number.isInteger(v.max_hours) ? v.max_hours : 2,
      listFull: v.list_full === true,
    };
  }
  return { offered: false, reason: typeof v.reason === 'string' ? v.reason.slice(0, 48) : '' };
}

// prevId: the request currently shown (or null). Returns { events, current, count }.
export function fromPending(data, prevId) {
  const list = (data && Array.isArray(data.requests) ? data.requests : []).map(approvalRecord).filter(Boolean);
  list.sort((a, b) => (a.createdAt ?? 0) - (b.createdAt ?? 0) || (a.id < b.id ? -1 : 1));
  const current = list.find((r) => r.id === prevId) || list[0] || null;
  const events = [];
  if (prevId && (!current || current.id !== prevId)) events.push({ type: 'approval.resolved', id: prevId, source: 'pending' });
  if (current && current.id !== prevId) events.push({ type: 'approval.request', id: current.id, summary: current.tool, source: 'pending' });
  return { events, current, count: list.length };
}

// --- pacing --------------------------------------------------------------------------------
// Polling sees a fast tool's start and end in the same batch. To make every real tool call
// visible, an end is held until its start has been on screen for MIN_SHOW seconds (and a
// turn end until the turn was open MIN_TURN). Order is kept: later events wait behind a held
// one. A backlog (e.g. a hidden tab catching up) is flushed without holds. The raw view lists
// the real event times; only the display is delayed.
export const MIN_SHOW = 1.2;
export const MIN_TURN = 1.6;
export const BACKLOG_FLUSH = 24;

export class Pacer {
  constructor() {
    this.queue = [];
    this.shownAt = new Map(); // tool id / 'turn' -> time its start was released
  }

  push(events) {
    for (const e of events) this.queue.push(e);
  }

  // Returns the events to dispatch at time `now` (seconds, any monotonic clock).
  take(now) {
    const out = [];
    const flush = this.queue.length > BACKLOG_FLUSH;
    while (this.queue.length) {
      const e = this.queue[0];
      if (!flush) {
        const since = (k) => (this.shownAt.has(k) ? now - this.shownAt.get(k) : Infinity);
        if (e.type === 'tool.end' && since(e.id) < MIN_SHOW) break;
        if (e.type === 'turn.end' && (since('turn') < MIN_TURN || [...this.shownAt.keys()].some((k) => k !== 'turn' && since(k) < MIN_SHOW))) break;
      }
      this.queue.shift();
      if (e.type === 'tool.start') this.shownAt.set(e.id, now);
      if (e.type === 'tool.end') this.shownAt.delete(e.id);
      if (e.type === 'turn.start') this.shownAt.set('turn', now);
      if (e.type === 'turn.end') this.shownAt.clear();
      out.push(e);
    }
    return out;
  }
}

// --- narration -----------------------------------------------------------------------------
// The text window describes the current state in a neutral voice (catalog key + vars). It
// never quotes the agent's text: bodies are not read.
// #445: the model call that is running, from the turn's model.* events (ids and numbers only).
// prev / result: { callId, since } (since: server seconds when the call started) or null.
export const LONG_GENERATION_SEC = 60;
export function generationFrom(prev, ev, wallNow) {
  if (!ev || typeof ev !== 'object') return prev;
  const d = ev.data && typeof ev.data === 'object' ? ev.data : {};
  const id = typeof d.call_id === 'string' && /^[0-9a-f]{32}$/.test(d.call_id) ? d.call_id : null;
  switch (ev.type) {
    case 'model.started':
      if (!id) return prev;
      // a progress event read first (the page's own stream) already knows the start
      if (prev && prev.callId === id) return prev;
      return { callId: id, since: num(d.started_at) && d.started_at <= wallNow ? d.started_at : wallNow };
    case 'model.progress':
      if (!id || !num(d.elapsed_ms) || d.elapsed_ms < 0) return prev;
      return { callId: id, since: wallNow - d.elapsed_ms / 1000 };
    case 'model.completed':
      return prev && (!id || prev.callId === id) ? null : prev;
    case 'turn.completed':
      return null;
    default:
      return prev;
  }
}

// Whole seconds of a running call (null when none).
export function generationSeconds(gen, wallNow) {
  return gen && num(gen.since) ? Math.max(0, Math.floor(wallNow - gen.since)) : null;
}

export function narrate(state, ctx = {}) {
  if (ctx.offline) return { key: 'stage.say_offline' };
  if (state.approval) {
    const more = Math.max(0, (ctx.pendingCount || 1) - 1);
    return more ? { key: 'stage.say_approval_more', vars: { tool: state.approval.summary, more } } : { key: 'stage.say_approval', vars: { tool: state.approval.summary } };
  }
  if (state.breaker === 'CRITICAL') return { key: 'stage.say_critical' };
  if (state.breaker === 'WARNING') return { key: ctx.breakerRaw === 'coherence' ? 'stage.say_coherence' : 'stage.say_warning' };
  const tool = state.tools[state.tools.length - 1];
  if (tool) {
    const name = tool.name || tool.id;
    if (tool.kind === 'memory') return { key: ctx.memoryWrite && ctx.memoryWrite.has(tool.id) ? 'stage.say_memory_write' : 'stage.say_memory', vars: { name } };
    if (tool.kind === 'web') return { key: 'stage.say_web', vars: { name } };
    return { key: 'stage.say_tool', vars: { name } };
  }
  // shown from 5 s on, in steps of 5 s (the text window types each new line out)
  if (state.thinking && num(ctx.generatingSec) && ctx.generatingSec >= 5) {
    const s = Math.floor(ctx.generatingSec / 5) * 5;
    return { key: s >= LONG_GENERATION_SEC ? 'stage.say_generating_long' : 'stage.say_generating', vars: { s } };
  }
  if (state.thinking) return { key: 'stage.say_thinking' };
  if (state.lastOutcome === 'ok') return { key: 'stage.say_done_ok' };
  if (state.lastOutcome === 'error') {
    const key = state.lastStop === 'max_tokens_before_reply' ? 'stage.say_done_max_tokens_before_reply'
      : state.lastStop === 'max_tokens' ? 'stage.say_done_max_tokens' : 'stage.say_done_error';
    return { key };
  }
  return { key: 'stage.say_idle' };
}

// One raw-view log line for a source event and what it became (no previews, no bodies).
export function describeSource(ev, mapped) {
  const d = (ev && ev.data) || {};
  // Only short identifier-like tokens reach the debug/raw log (tool names, statuses,
  // states); anything else is dropped so free text can never be echoed here.
  const token = (x) => (typeof x === 'string' && /^[A-Za-z0-9_.:-]{1,64}$/.test(x) ? x : '');
  const what = [ev.type, d.name, d.status, d.to].map(token).filter(Boolean).join(' ');
  const turn = typeof ev.turn_id === 'string' ? ev.turn_id.slice(0, 8) : '?';
  const out = mapped.length ? mapped.map((m) => `${m.type}${m.kind ? ` ${m.kind}` : ''}`).join(', ') : '—';
  return `turn ${turn} #${ev.seq ?? '?'} ${what} → ${out}`;
}
