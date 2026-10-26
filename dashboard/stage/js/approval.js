// Approval decisions from the choice window (#341, stage 2), through the existing API only:
// POST /api/hitl/<id>/decision with the CSRF token, the displayed args_hash, this tab's
// client_id and an Idempotency-Key (one per attempted decision, reused when the same
// decision is retried after a network error). The server stays the authority: it refuses
// stale (binding_mismatch), expired, already decided or raced requests.
//
// Stage offers "approve once" and "reject" — and (ADR 0025) the two "allow again" choices
// where the server's view offers them, while ALLOW_AGAIN_ON is set (a code constant, not a
// setting; PR 3 turns it on with the server). Approve needs the person to have opened the
// (masked) arguments of that very request first; an expired request cannot be approved.
// Edit, respond and reject-with-reason are on the approvals page.

// ADR 0025 "Implementation order": PR 3 turns both choices on with the server (one constant).
export const ALLOW_AGAIN_ON = true;

const HOST_ID = /^[0-9a-f]{32}$/;
const SHORT_HEX = /^[0-9a-f]{8}$/;

// Server error codes -> catalog keys (the approvals page's wording, #189).
export const ERROR_KEYS = {
  binding_mismatch: 'approvals.error_binding_mismatch',
  already_decided: 'approvals.error_already_decided',
  expired: 'approvals.error_expired',
  not_found: 'approvals.error_not_found',
  store_unavailable: 'approvals.error_store_unavailable',
  busy: 'approvals.error_busy',
  unsupported: 'approvals.error_unsupported',
  duplicate_request: 'approvals.error_duplicate',
  // ADR 0012 §8b (only with STIRA_YOLO_ALLOW): the host confirmation's outcome.
  host_confirmation_required: 'host.code_host_confirmation_required',
  stale_operation: 'host.code_stale_operation',
  host_operation_expired: 'host.code_host_operation_expired',
  host_operation_denied: 'host.code_host_operation_denied',
  host_operation_cancelled: 'host.code_host_operation_cancelled',
  host_operation_unknown: 'host.code_host_operation_unknown',
  host_operation_pending: 'host.code_host_operation_pending',
  host_operation_used: 'host.code_host_operation_used',
  host_operation_mismatch: 'host.code_host_operation_mismatch',
  host_rate_limited: 'host.code_host_rate_limited',
  host_channel_unavailable: 'host.code_host_channel_unavailable',
  host_not_acknowledged: 'host.code_host_not_acknowledged',
  host_browser_refused: 'host.code_host_browser_refused',
  host_confirm_on_host: 'host.code_host_confirm_on_host',
  host_operation_incarnation: 'host.code_host_operation_incarnation',
  tool_call_running: 'host.code_tool_call_running',
  // ADR 0025: refusals of the two "allow again" choices (nothing was approved).
  stale_view: 'approvals.error_stale_view',
  class_changed: 'approvals.error_stale_view',
  op_token_used: 'approvals.error_stale_view',
  list_full: 'approvals.allow_list_full',
  audit_failed: 'approvals.error_allow_audit',
  not_enabled: 'approvals.error_allow_not_enabled',
  card_not_pending: 'approvals.error_already_decided',
};

// Why a card offers only "approve once" (ADR 0025 §3): the same words as the chat card.
export const NOT_OFFERED_KEYS = {
  exec: 'approvals.allow_reason_exec',
  person_only: 'approvals.allow_reason_person_only',
  unclassified: 'approvals.allow_reason_unclassified',
  scope_outside: 'approvals.allow_reason_scope_outside',
  scope_unknown: 'approvals.allow_reason_scope_unknown',
  read_unbound: 'approvals.allow_reason_read_unbound',
  secret_in_args: 'approvals.allow_reason_secret_in_args',
  credential_by_reference: 'approvals.allow_reason_secret_in_args',
  destination_unknown: 'approvals.allow_reason_destination_unknown',
  sends_data: 'approvals.allow_reason_sends_data',
  breaker: 'approvals.allow_reason_breaker',
  not_this_session: 'approvals.allow_reason_not_this_session',
  edited: 'approvals.allow_reason_edited',
  rule_not_ask: 'approvals.allow_reason_rule_not_ask',
  args_unknown: 'approvals.allow_reason_unclassified',
  read_failed: 'approvals.allow_reason_read_failed',
  audit_failed: 'approvals.allow_reason_audit_failed',
};

// The not-offered line for a record, or null (nothing while ALLOW_AGAIN_ON is off).
export function allowReasonText(record, t, on = ALLOW_AGAIN_ON) {
  const v = record && record.allowAgain;
  if (!on || !v || v.offered || !v.reason || v.reason === 'not_enabled') return null;
  const words = has(NOT_OFFERED_KEYS, v.reason) ? t(NOT_OFFERED_KEYS[v.reason]) : t('approvals.reason_other');
  return t('approvals.allow_not_offered', { reason: words });
}

// The class line of the offered choices ("file_write · game/ 配下 · 最長2時間").
export function allowClassText(record, t) {
  const shared = globalThis.StiraAllow;
  const v = record && record.allowAgain;
  if (shared && typeof shared.classText === 'function') return shared.classText(record.tool, v, t);
  return `${record.tool} · ${v && v.label ? v.label : ''}`;
}

// The approvals page's rule (#279/#284): a server reason is translated only when its code
// and its exact sentence match; a code it does not know is said as "Other reason (code: ...)"
// (never bare, never a guessed sentence); a reason without a code is shown as sent. The level
// comes first.
const REASON_KEYS = {
  breaker_critical_halt: ['approvals.reason_breaker_critical_halt', 'CRITICAL HALT requires human approval for this write operation.'],
  rule_ask: ['approvals.reason_rule_ask', 'Tool policy is Ask: every call of this tool needs a human approval.'],
};
const LEVEL_KEYS = { normal: 'shell.normal', warning: 'shell.warning', critical: 'shell.critical', coherence: 'shell.coherence' };

const CODE_SHAPE = /^[A-Za-z][A-Za-z0-9_]{0,47}$/;
const has = (table, key) => typeof key === 'string' && Object.prototype.hasOwnProperty.call(table, key);

export function whyText(record, t) {
  const raw = record.breakLevel ? String(record.breakLevel).toLowerCase() : null;
  const level = raw ? (has(LEVEL_KEYS, raw) ? t(LEVEL_KEYS[raw]) : raw.toUpperCase()) : null;
  const code = typeof record.reasonCode === 'string' ? record.reasonCode : null;
  let reason = record.reason;
  if (has(REASON_KEYS, code)) {
    if (REASON_KEYS[code][1] === record.reason) reason = t(REASON_KEYS[code][0]);
  } else if (code) {
    reason = CODE_SHAPE.test(code) ? t('approvals.reason_other_code', { code }) : t('approvals.reason_other');
  }
  if (!level) return reason || null;
  return reason ? t('approvals.why_level', { level, reason }) : level;
}

export function remainingText(seconds, t) {
  if (seconds <= 0) return t('approvals.expired_now');
  const whole = Math.ceil(seconds);
  const minutes = Math.floor(whole / 60);
  const rest = whole % 60;
  return t('approvals.remaining', { time: minutes ? `${minutes}:${String(rest).padStart(2, '0')}` : `${rest}s` });
}

export function randomId() {
  const c = globalThis.crypto;
  if (c && typeof c.randomUUID === 'function') return c.randomUUID();
  const bytes = new Uint8Array(16);
  c.getRandomValues(bytes);
  return [...bytes].map((b) => b.toString(16).padStart(2, '0')).join('');
}

// What the choice window may offer for a request right now (pure).
// argsSeen: the arguments of this request were shown here; argsOpen: the panel is open now.
export function approvalChoices(record, { argsSeen, argsOpen = false, expired, busy, allowOn = ALLOW_AGAIN_ON }) {
  if (!record) return [];
  const approve = expired
    ? { id: 'approve', key: 'stage.choice_approve_expired', disabled: true }
    : argsSeen
      ? { id: 'approve', key: 'stage.choice_approve', disabled: Boolean(busy) }
      : { id: 'approve', key: 'stage.choice_approve_locked', disabled: true };
  const choices = [
    { id: 'args', key: argsOpen ? 'stage.choice_hide_args' : 'stage.choice_show_args', disabled: false },
    approve,
  ];
  const v = record.allowAgain;
  if (allowOn && v && v.offered) {
    // ADR 0025: the same lock as approve (the arguments shown first, not expired).
    const locked = !argsSeen || Boolean(expired) || Boolean(busy);
    choices.push(
      { id: 'allow_session', key: 'stage.choice_allow_session', disabled: locked || v.listFull === true },
      { id: 'allow_always', key: 'stage.choice_allow_always', disabled: locked },
    );
  }
  choices.push(
    { id: 'reject', key: 'stage.choice_reject', disabled: Boolean(busy) },
    { id: 'page', key: 'stage.choice_open_page', disabled: false },
  );
  return choices;
}

export class ApprovalClient {
  // post(url, body, headers) -> Promise<{ ok, status, json() }>; token() -> Promise<string>
  // hostConfirm(operation) -> Promise<"confirmed" | "cancelled" | { code }>: the browser
  // confirmation dialog (api.js StiraHostConfirm.browser; ADR 0012 §8b revision 6).
  constructor({ post, token, clientId, newKey = randomId, hostConfirm = null }) {
    this.post = post;
    this.token = token;
    this.hostConfirm = typeof hostConfirm === 'function' ? hostConfirm : null;
    this.clientId = clientId || `stage-${newKey().slice(0, 8)}`;
    this.newKey = newKey;
    this.keys = new Map(); // request id -> { signature, key }
    this.busy = new Set();
  }

  // -> { ok: true } | { ok: false, errorKey?, error? }
  async decide(record, action, { argsSeen = false, expired = false } = {}) {
    if (!record || !record.id) return { ok: false, errorKey: 'approvals.error_not_found' };
    const allowing = action === 'allow_session' || action === 'allow_always';
    if (action !== 'approve' && action !== 'reject' && !allowing) return { ok: false, errorKey: 'approvals.error_unsupported' };
    // Never approve what was not shown, nor what the client already sees as expired.
    if ((action === 'approve' || allowing) && (!argsSeen || expired || !record.argsHash)) {
      return { ok: false, errorKey: expired ? 'approvals.error_expired' : 'approvals.error_binding_mismatch' };
    }
    if (allowing && !(record.allowAgain && record.allowAgain.offered && record.allowAgain.token)) {
      return { ok: false, errorKey: 'approvals.error_stale_view' };
    }
    if (this.busy.has(record.id)) return { ok: false, busy: true };
    const body = { action: allowing ? 'approve' : action, client_id: this.clientId };
    if (record.argsHash) body.args_hash = record.argsHash;
    if (allowing) {
      // ADR 0025 §4a: only the view's one-time token (the server recomputes the class).
      body.allow_again = action === 'allow_session' ? 'session' : 'always';
      body.view_token = record.allowAgain.token;
    }
    const signature = JSON.stringify(body);
    let entry = this.keys.get(record.id);
    if (!entry || entry.signature !== signature) {
      entry = { signature, key: this.newKey() };
      this.keys.set(record.id, entry);
    }
    this.busy.add(record.id);
    try {
      const headers = {
        'Content-Type': 'application/json',
        'X-CSRF-Token': await this.token(),
        'Idempotency-Key': entry.key,
      };
      // ADR 0012 §8b: the operation confirmed on the host for exactly this decision.
      if (entry.hostOp) headers['X-Stira-Host-Operation'] = entry.hostOp;
      const url = `/api/hitl/${encodeURIComponent(record.id)}/decision`;
      let response = await this.post(url, signature, headers);
      let data = {};
      try {
        data = await response.json();
      } catch {
        data = {};
      }
      let operation = data && data.operation;
      if (response.status === 428 && data.code === 'host_confirmation_required' && operation
          && HOST_ID.test(operation.id) && operation.channel === 'browser' && this.hostConfirm) {
        // The browser channel: confirm in the dialog now, then send this decision once
        // more bound to the operation (a new key: nothing was recorded). Cancel is final.
        const state = await this.hostConfirm(operation);
        if (state !== 'confirmed') {
          const code = state && typeof state === 'object' && typeof state.code === 'string'
            ? state.code : 'host_operation_cancelled';
          entry.key = this.newKey();
          return { ok: false, errorKey: ERROR_KEYS[code] || 'host.code_host_operation_unknown', code };
        }
        entry.key = this.newKey();
        response = await this.post(url, signature, {
          ...headers, 'Idempotency-Key': entry.key, 'X-Stira-Host-Operation': operation.id,
        });
        try {
          data = await response.json();
        } catch {
          data = {};
        }
        operation = null;
      }
      if (response.ok) {
        this.keys.delete(record.id);
        return allowing ? { ok: true, allow: data && typeof data.allow === 'object' ? data.allow : {} } : { ok: true };
      }
      if (response.status === 428 && data.code === 'host_confirmation_required' && operation
          && HOST_ID.test(operation.id)) {
        // Only with STIRA_YOLO_ALLOW: confirm on the host, then the next press re-sends
        // this decision bound to that operation (a new key: nothing was recorded).
        entry.hostOp = operation.id;
        entry.key = this.newKey();
        return {
          ok: false,
          errorKey: 'stage.host_confirm_then_retry',
          errorVars: { short: SHORT_HEX.test(operation.short) ? operation.short : '?',
            code: SHORT_HEX.test(operation.code) ? operation.code : '?' },
          code: data.code,
        };
      }
      if (data.code !== 'host_operation_pending' && typeof data.code === 'string'
          && (data.code.startsWith('host_') || data.code === 'stale_operation')) entry.hostOp = null;
      if (data.code === 'binding_mismatch') this.keys.delete(record.id);
      if (ERROR_KEYS[data.code]) return { ok: false, errorKey: ERROR_KEYS[data.code], code: data.code };
      return { ok: false, errorKey: 'chat.decision_failed', error: typeof data.error === 'string' ? data.error : null };
    } catch {
      // Same key on the next press: a retry of the same decision, not a second one.
      return { ok: false, errorKey: 'approvals.error_network' };
    } finally {
      this.busy.delete(record.id);
    }
  }
}
