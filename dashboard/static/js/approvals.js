/* Approval cards, queue and decision history (#189).
 *
 * StiraApprovals.create({ list, panel, live, history, page }) renders every pending
 * /api/hitl/pending request as a card built from the shared tool-card parts (#188),
 * and (on the approvals page) the /api/hitl/history table. Chat cards, the nav chip
 * and the approvals page all read the same server state, so a reload restores them.
 *
 * Basic operations are "approve this once" and "reject (with a reason)". Edit and
 * respond stay available under "other operations"; an edit is re-reviewed by the
 * server as a new request (#146). ADR 0025: two more buttons — "approve, and allow the
 * same for this session" and "approve, and set this tool to Always" — are drawn only
 * where the server's view (request.allow_again) offers them, and only while
 * ALLOW_AGAIN_ON is set (a code constant, not a setting; PR 3 turns it on together with
 * the server). A card the server does not offer them for says why in one line. The page
 * computes no class: the text comes from the view. Every decision quotes the displayed args_hash,
 * carries a per-attempt Idempotency-Key (reused on retry) and is refused by the
 * server when stale, expired, already decided or raced by another tab.
 */
(function (root) {
  "use strict";
  const ERRORS = {
    binding_mismatch: "approvals.error_binding_mismatch",
    already_decided: "approvals.error_already_decided",
    expired: "approvals.error_expired",
    not_found: "approvals.error_not_found",
    store_unavailable: "approvals.error_store_unavailable",
    // #350: a new conversation held the shared lock; nothing recorded, still pending.
    busy: "approvals.error_busy",
    unsupported: "approvals.error_unsupported",
    // ADR 0024 §3: approve / edit wait for the breaker (nothing recorded, still pending).
    breaker: "approvals.error_breaker",
    // The same decision was already received (Idempotency-Key replay, #185).
    duplicate_request: "approvals.error_duplicate",
    // ADR 0012 §8b (only with STIRA_YOLO_ALLOW): the host confirmation's outcome.
    host_confirmation_required: "host.code_host_confirmation_required",
    stale_operation: "host.code_stale_operation",
    host_operation_expired: "host.code_host_operation_expired",
    host_operation_denied: "host.code_host_operation_denied",
    host_operation_cancelled: "host.code_host_operation_cancelled",
    host_operation_unknown: "host.code_host_operation_unknown",
    host_operation_pending: "host.code_host_operation_pending",
    host_operation_used: "host.code_host_operation_used",
    host_operation_mismatch: "host.code_host_operation_mismatch",
    host_rate_limited: "host.code_host_rate_limited",
    host_channel_unavailable: "host.code_host_channel_unavailable",
    host_not_acknowledged: "host.code_host_not_acknowledged",
    host_browser_refused: "host.code_host_browser_refused",
    host_confirm_on_host: "host.code_host_confirm_on_host",
    host_operation_incarnation: "host.code_host_operation_incarnation",
    tool_call_running: "host.code_tool_call_running",
  };
  // ADR 0025 "Implementation order": PR 3 turns both buttons on together with the server's
  // entries (one code constant, not a setting; false hides both again).
  const ALLOW_AGAIN_ON = true;
  // Decision refusals of the two buttons (nothing was approved; the card re-reads its view).
  Object.assign(ERRORS, {
    stale_view: "approvals.error_stale_view",
    class_changed: "approvals.error_stale_view",
    op_token_used: "approvals.error_stale_view",
    list_full: "approvals.allow_list_full",
    audit_failed: "approvals.error_allow_audit",
    not_enabled: "approvals.error_allow_not_enabled",
    card_not_pending: "approvals.error_already_decided",
  });
  // Why a card offers only "Approve once" (ADR 0025 §3): the reason code -> its words.
  const NOT_OFFERED = {
    exec: "approvals.allow_reason_exec",
    person_only: "approvals.allow_reason_person_only",
    unclassified: "approvals.allow_reason_unclassified",
    scope_outside: "approvals.allow_reason_scope_outside",
    scope_unknown: "approvals.allow_reason_scope_unknown",
    read_unbound: "approvals.allow_reason_read_unbound",
    secret_in_args: "approvals.allow_reason_secret_in_args",
    credential_by_reference: "approvals.allow_reason_secret_in_args",
    destination_unknown: "approvals.allow_reason_destination_unknown",
    sends_data: "approvals.allow_reason_sends_data",
    breaker: "approvals.allow_reason_breaker",
    not_this_session: "approvals.allow_reason_not_this_session",
    edited: "approvals.allow_reason_edited",
    rule_not_ask: "approvals.allow_reason_rule_not_ask",
    args_unknown: "approvals.allow_reason_unclassified",
    read_failed: "approvals.allow_reason_read_failed",
    audit_failed: "approvals.allow_reason_audit_failed",
  };
  function allowClassText(tool, view) {
    const shared = root.StiraAllow && typeof root.StiraAllow.classText === "function" ? root.StiraAllow : null;
    if (shared) return shared.classText(tool, view, t);
    return `${tool} · ${String(view && view.label || "")}`;
  }
  // What the card shows for request.allow_again (pure): { buttons, session, always,
  // classText, full, reason } — nothing at all while ALLOW_AGAIN_ON is false or the server
  // does not offer the feature (not_enabled).
  function allowView(request, on = ALLOW_AGAIN_ON) {
    const view = request && request.allow_again && typeof request.allow_again === "object" ? request.allow_again : null;
    if (!on || !view || view.reason === "not_enabled") return { buttons: false, reason: null };
    if (view.offered === true && typeof view.token === "string" && view.token) {
      return { buttons: true, full: view.list_full === true, classText: allowClassText(request.tool_name || "?", view), reason: null };
    }
    const code = typeof view.reason === "string" ? view.reason : "";
    const words = NOT_OFFERED[code] ? t(NOT_OFFERED[code]) : (REASON_CODE_SHAPE.test(code) ? code : t("approvals.reason_other"));
    return { buttons: false, reason: t("approvals.allow_not_offered", { reason: words }) };
  }
  // #279: server-owned HITL reasons (core/hitl_reasons.py) -> catalog keys.
  const REASON_KEYS = {
    breaker_critical_halt: "approvals.reason_breaker_critical_halt",
    rule_ask: "approvals.reason_rule_ask",
  };
  // The exact server sentence each code stands for: a reason that does not match is
  // shown as sent (unknown code, edited or older server), never replaced.
  const REASON_TEXT = {
    breaker_critical_halt: "CRITICAL HALT requires human approval for this write operation.",
    rule_ask: "Tool policy is Ask: every call of this tool needs a human approval.",
  };
  // #279: sentences the server closes a request with (history), by
  // resolution_reason_code. Same rule: worded only when the sentence matches exactly.
  const RESOLUTION_KEYS = {
    expired: "approvals.resolution_expired",
    closed: "approvals.resolution_closed",
    replaced: "approvals.resolution_replaced",
    binding_mismatch: "approvals.resolution_binding_mismatch",
    live_only: "approvals.resolution_live_only",
    rules_changed: "approvals.resolution_rules_changed",
    breaker_stricter: "approvals.resolution_breaker_stricter",
    job_restart: "approvals.resolution_job_restart",
    grant_stopped: "approvals.resolution_grant_stopped",
  };
  const RESOLUTION_TEXT = {
    expired: ["approval expired", "expired"],
    closed: ["approval closed"],
    replaced: ["replaced by a new request"],
    binding_mismatch: ["binding mismatch"],
    live_only: ["approved live only (secret arguments); not restorable"],
    rules_changed: ["tool rules changed (#190)"],
    breaker_stricter: ["circuit breaker stricter than at request time"],
    job_restart: ["job runs do not survive a restart"],
    grant_stopped: ["the grant iteration stopped"],
  };
  function resolutionText(item) {
    const sentence = typeof item.resolution_reason === "string" ? item.resolution_reason : null;
    if (!sentence) return null;
    const code = typeof item.resolution_reason_code === "string" ? item.resolution_reason_code : null;
    const known = code && item.resolver === "system"
      && Object.prototype.hasOwnProperty.call(RESOLUTION_KEYS, code)
      && RESOLUTION_TEXT[code].includes(sentence);
    return known ? t(RESOLUTION_KEYS[code]) : sentence;
  }
  // Breaker level words (#284), shown in front of every reason, translated or not.
  const LEVEL_KEYS = {
    normal: "shell.normal", warning: "shell.warning", critical: "shell.critical", coherence: "shell.coherence",
  };
  const REASON_CODE_SHAPE = /^[A-Za-z][A-Za-z0-9_]{0,47}$/;
  // A code this page has no wording for (a newer server): never the bare code or a
  // guessed sentence, but "Other reason (code: ...)" when it has the shape of a code.
  function otherReason(code) {
    return REASON_CODE_SHAPE.test(code) ? t("approvals.reason_other_code", { code }) : t("approvals.reason_other");
  }
  function whyText(request) {
    // `reason_params.level` (server variable, #279) wins over the older `break_level`.
    const params = request.reason_params && typeof request.reason_params === "object" ? request.reason_params : {};
    const given = typeof params.level === "string" ? params.level : request.break_level;
    const raw = given ? String(given) : null;
    const key = raw && Object.prototype.hasOwnProperty.call(LEVEL_KEYS, raw.toLowerCase())
      ? LEVEL_KEYS[raw.toLowerCase()] : null;
    const level = raw ? (key ? t(key) : raw.toUpperCase()) : null;
    const reasonText = typeof request.reason === "string" ? request.reason : null;
    const code = typeof request.reason_code === "string" ? request.reason_code : null;
    const coded = code && Object.prototype.hasOwnProperty.call(REASON_KEYS, code);
    let reason = reasonText;
    if (coded && REASON_TEXT[code] === reasonText) reason = t(REASON_KEYS[code]);
    else if (code && !coded) reason = otherReason(code);
    if (!level) return reason;
    return reason ? t("approvals.why_level", { level, reason }) : level;
  }
  const STATUS_KEYS = {
    approved: "approvals.status_approved",
    rejected: "approvals.status_rejected",
    edited: "approvals.status_edited",
    responded: "approvals.status_responded",
    expired: "approvals.status_expired",
    closed: "approvals.status_closed",
    cancelled: "approvals.status_cancelled",
  };
  const STATUS_STATE = {
    approved: "ok", rejected: "crit", edited: "info", responded: "info", expired: "warn", closed: "warn",
    cancelled: "warn",
  };
  const finite = value => typeof value === "number" && Number.isFinite(value);

  function make(doc, tag, className, content) {
    const node = doc.createElement(tag);
    if (className) node.className = className;
    if (content !== undefined && content !== null) node.textContent = content;
    return node;
  }

  function pretty(request) {
    const raw = typeof request.normalized_input === "string" ? request.normalized_input : null;
    if (raw === null) return JSON.stringify(request.tool_input ?? {}, null, 2);
    try { return JSON.stringify(JSON.parse(raw), null, 2); } catch { return raw; }
  }

  function oneLine(text, limit = 80) {
    const line = String(text ?? "").replace(/\s+/g, " ").trim();
    return line.length > limit ? `${line.slice(0, limit - 1)}…` : line;
  }

  function clock(seconds) {
    if (!finite(seconds)) return null;
    const date = new Date(seconds * 1000);
    const pad = n => String(n).padStart(2, "0");
    return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
  }

  function remainingText(seconds) {
    if (seconds <= 0) return t("approvals.expired_now");
    const whole = Math.ceil(seconds);
    const minutes = Math.floor(whole / 60), rest = whole % 60;
    return t("approvals.remaining", { time: minutes ? `${minutes}:${String(rest).padStart(2, "0")}` : `${rest}s` });
  }

  function randomId() {
    const crypto = root.crypto;
    if (crypto?.randomUUID) return crypto.randomUUID();
    return `k${Date.now().toString(36)}${Math.random().toString(36).slice(2, 10)}`;
  }

  function create(options = {}) {
    const doc = options.document || root.document;
    const now = options.now || (() => Date.now() / 1000);
    const page = Boolean(options.page);
    // Card heading level follows the surrounding outline (chat panel h3 -> h4, page h2 -> h3).
    const headingLevel = Number.isInteger(options.headingLevel) ? options.headingLevel : 4;
    const list = options.list, panel = options.panel, live = options.live;
    const historyBody = options.history || null;
    const cards = root.StiraCards;
    const clientId = options.clientId || `tab-${randomId().slice(0, 8)}`;
    const getJSON = options.getJSON || (async url => {
      const response = await fetch(url, { cache: "no-store", headers: { Accept: "application/json" } });
      if (!response.ok) throw new Error(String(response.status));
      return response.json();
    });
    const post = options.post || ((url, body, key) => stiraFetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", "Idempotency-Key": key },
      body: JSON.stringify(body),
    }));
    const el = (tag, className, content) => make(doc, tag, className, content);
    const entries = new Map(); // id -> { request, card, parts }
    let skew = 0, loading = false, lastCount = null, lastIds = null, historyAt = 0;
    const HISTORY_REFRESH_MS = 30000;
    const HISTORY_RETRY_MS = 10000;
    const HISTORY_TIMEOUT_MS = 10000;
    // #601 E1: the queue's read has a time limit too; one that never answers would otherwise
    // keep ``loading`` set and every later refresh (polls, after a decision) would be skipped.
    const PENDING_TIMEOUT_MS = 10000;
    let historyBusy = false;

    function announce(text) {
      if (!live) return;
      live.textContent = "";
      live.textContent = text;
    }

    function publishCount(count) {
      if (count === lastCount) return;
      lastCount = count;
      const target = doc.dispatchEvent ? doc : null;
      if (target && typeof root.CustomEvent === "function") {
        target.dispatchEvent(new root.CustomEvent("stira:approvals", { detail: { count } }));
      }
      options.onCount?.(count);
    }

    function turnLink(turnId) {
      if (!turnId) return null;
      // The chat page opens the Turn Inspector for ?turn= (#187).
      const link = el("a", "approval-turn", String(turnId).slice(0, 8));
      link.setAttribute("href", `/chat?turn=${encodeURIComponent(turnId)}`);
      link.setAttribute("title", String(turnId));
      return link;
    }

    function fact(dl, label, value, extra) {
      const row = el("div");
      const node = value !== null && typeof value === "object";
      const dd = el("dd", extra?.className || null, node ? null : (value ?? t("approvals.none")));
      if (node) dd.append(value);
      if (value === null || value === undefined) dd.dataset.trend = "missing";
      row.append(el("dt", null, label), dd);
      dl.append(row);
      return dd;
    }

    function button(label, action, variant) {
      const node = el("button", `btn ${variant}`, label);
      node.type = "button";
      node.dataset.approvalAction = action;
      return node;
    }

    function field(id, label, rows, value) {
      const wrap = el("div", "approval-field");
      const lab = el("label", null, label);
      lab.setAttribute("for", id);
      const area = el("textarea", "textarea");
      area.id = id;
      area.rows = rows;
      area.spellcheck = false;
      area.value = value ?? "";
      wrap.append(lab, area);
      return { wrap, area };
    }

    function buildCard(request) {
      const id = request.id;
      const safe = String(id).replace(/[^\w-]/g, "");
      const spec = { kind: "approval", title: request.tool_name || "?", status: "pending",
        brief: oneLine(request.normalized_input ?? ""), meta: null };
      const box = cards ? cards.card(doc, spec) : el("details", "stira-card");
      if (!cards) { box.parts = { summary: el("summary", "stira-card-summary"), body: el("div", "stira-card-body") }; box.append(box.parts.summary, box.parts.body); }
      box.className = "stira-card approval-card";
      box.open = true;
      box.dataset.requestId = id;
      box.setAttribute("aria-labelledby", `approval-title-${safe}`);
      const body = box.parts.body;
      const heading = el(`h${headingLevel}`, "approval-heading", t("approvals.card_title", { tool: request.tool_name || "?" }));
      heading.id = `approval-title-${safe}`;
      const dl = el("dl", "facts approval-facts");
      fact(dl, t("approvals.target"), [request.tool_name, request.agent_id].filter(Boolean).join(" · ") || null);
      fact(dl, t("approvals.cwd"), request.cwd || null);
      fact(dl, t("approvals.why"), whyText(request) || null);
      const expiry = fact(dl, t("approvals.expires"), null, { className: "approval-expiry" });
      if (request.supersedes) fact(dl, t("approvals.supersedes"), t("approvals.supersedes_value", { id: String(request.supersedes).slice(0, 8) }));
      fact(dl, t("approvals.origin_turn"), turnLink(request.turn_id));
      // Restored after a restart (#240): approving resumes the paused turn.
      if (request.restored) fact(dl, t("approvals.restored"), t(request.resumable ? "approvals.restored_resumes" : "approvals.restored_no_resume"));
      // ADR 0024 §5: what the resume does with the other calls of the paused batch (counts).
      const replay = request.restored && request.replay && typeof request.replay === "object" ? request.replay : null;
      if (replay && Number.isInteger(replay.from_ledger) && replay.from_ledger > 0) {
        fact(dl, t("approvals.replay"), t("approvals.replay_from_ledger", { count: replay.from_ledger }));
      }
      if (replay && Number.isInteger(replay.unknown) && replay.unknown > 0) {
        fact(dl, t("approvals.replay"), t("approvals.replay_unknown", { count: replay.unknown }));
      }
      fact(dl, t("approvals.request_id"), String(id).slice(0, 8));
      fact(dl, t("approvals.args_hash"), request.args_hash ? String(request.args_hash).slice(0, 12) : null);
      const argsTitle = el(`h${Math.min(headingLevel + 1, 6)}`, "approval-subheading", t("approvals.normalized_args"));
      const args = el("pre", "tool-payload-text approval-args", pretty(request));
      args.setAttribute("tabindex", "0");
      args.setAttribute("aria-label", t("approvals.normalized_args"));
      const reason = field(`approval-reason-${safe}`, t("approvals.reject_reason"), 2, "");
      const actions = el("div", "approval-actions");
      const approve = button(t("approvals.approve_once"), "approve", "btn-primary");
      const reject = button(t("approvals.reject"), "reject", "btn-danger");
      actions.append(approve, reject);
      // ADR 0025: the two "allow again" buttons, their class line and the not-offered line.
      const allow = el("div", "approval-allow");
      allow.hidden = true;
      const allowActions = el("div", "approval-actions approval-allow-actions");
      const allowSession = button(t("approvals.allow_session"), "allow_session", "btn-secondary");
      const allowAlways = button(t("approvals.allow_always"), "allow_always", "btn-secondary");
      allowActions.append(allowSession, allowAlways);
      const allowClass = el("p", "tool-payload-note approval-allow-class");
      allowClass.id = `approval-allow-class-${safe}`;
      allowSession.setAttribute("aria-describedby", allowClass.id);
      const allowFull = el("p", "tool-payload-note approval-allow-full", t("approvals.allow_list_full"));
      allowFull.hidden = true;
      allow.append(allowActions, allowClass, allowFull);
      const notOffered = el("p", "tool-payload-note approval-allow-reason");
      notOffered.hidden = true;
      const more = el("details", "approval-more");
      const moreSummary = el("summary", null, t("approvals.other_operations"));
      const edit = field(`approval-edit-${safe}`, t("approvals.edit_args"), 4, pretty(request));
      const editNote = el("p", "tool-payload-note", t("approvals.edit_note"));
      const editButton = button(t("approvals.edit_resubmit"), "edit", "btn-secondary");
      const respond = field(`approval-respond-${safe}`, t("approvals.respond_text"), 2, "");
      const respondButton = button(t("approvals.respond"), "respond", "btn-secondary");
      more.append(moreSummary, edit.wrap, editNote, editButton, respond.wrap, respondButton);
      // Restored: no live turn to answer. Masked arguments (restored, or a live request
      // whose secrets the server masked, #185) cannot be edited; the server agrees.
      if (request.restored) respond.wrap.hidden = respondButton.hidden = true;
      if (request.editable === false) {
        edit.wrap.hidden = editButton.hidden = true;
        editNote.textContent = t(request.restored ? "approvals.edit_masked" : "approvals.edit_masked_live");
      }
      const error = el("p", "error approval-error");
      error.setAttribute("role", "alert");
      // ADR 0024 §3: while the breaker is stricter than at request time, approve / edit
      // wait (the server refuses them too); reject stays possible, the expiry is not extended.
      const hold = el("p", "tool-payload-note approval-hold", t("approvals.breaker_hold"));
      hold.hidden = true;
      body.append(heading, dl, hold, argsTitle, args, reason.wrap, actions, allow, notOffered, more, error);
      const parts = { approve, reject, editButton, respondButton, reason: reason.area,
        edit: edit.area, respond: respond.area, expiry, error, hold, allow, allowSession, allowAlways,
        allowClass, allowFull, notOffered,
        buttons: [approve, reject, editButton, respondButton, allowSession, allowAlways] };
      for (const node of parts.buttons) {
        node.addEventListener("click", () => void decide(id, node.dataset.approvalAction));
      }
      return { box, parts };
    }

    function tickExpiry(entry) {
      const left = entry.request.expires_at - (now() + skew);
      const expired = finite(left) && left <= 0;
      const text = finite(entry.request.expires_at)
        ? `${clock(entry.request.expires_at - skew) ?? "?"} (${remainingText(left)})` : t("approvals.none");
      entry.parts.expiry.textContent = text;
      delete entry.parts.expiry.dataset.trend;
      if (cards) {
        cards.update(doc, entry.card, { kind: "approval", title: entry.request.tool_name || "?",
          status: expired ? "timeout" : "pending", brief: oneLine(entry.request.normalized_input ?? ""),
          meta: finite(left) ? remainingText(left) : null });
      }
      entry.card.dataset.expired = expired ? "true" : "false";
      // A request the client sees as expired can never be approved (the server agrees).
      const held = entry.request.breaker_hold === true;
      entry.parts.hold.hidden = !held;
      entry.card.dataset.breakerHold = held ? "true" : "false";
      for (const node of [entry.parts.approve, entry.parts.editButton]) {
        node.disabled = expired || entry.busy || held;
      }
      const allow = allowView(entry.request);
      entry.parts.allow.hidden = !allow.buttons;
      entry.parts.notOffered.hidden = !allow.reason;
      if (entry.parts.notOffered.textContent !== (allow.reason || "")) entry.parts.notOffered.textContent = allow.reason || "";
      if (allow.buttons) {
        if (entry.parts.allowClass.textContent !== allow.classText) entry.parts.allowClass.textContent = allow.classText;
        entry.parts.allowFull.hidden = !allow.full;
        entry.parts.allowSession.disabled = expired || entry.busy || held || allow.full;
        entry.parts.allowAlways.disabled = expired || entry.busy || held;
      }
      entry.parts.respondButton.disabled = expired || entry.busy;
      entry.parts.reject.disabled = entry.busy;
      if (expired && !entry.expiredShown) {
        entry.expiredShown = true;
        entry.parts.error.textContent = t("approvals.error_expired");
      }
    }

    function focusAfterRemoval(index) {
      const remaining = [...entries.values()];
      const next = remaining[Math.min(index, remaining.length - 1)];
      if (next) next.parts.approve.disabled ? next.parts.reject.focus() : next.parts.approve.focus();
      else options.onEmpty?.();
    }

    function removeEntry(id) {
      const entry = entries.get(id);
      if (!entry) return -1;
      const index = [...entries.keys()].indexOf(id);
      entry.card.remove();
      entries.delete(id);
      return index;
    }

    function syncPanel() {
      if (panel) panel.hidden = entries.size === 0;
      publishCount(entries.size);
      if (options.empty) options.empty.hidden = entries.size !== 0;
    }

    function render(requests, serverTime) {
      if (finite(serverTime)) skew = serverTime - now();
      const seen = new Set();
      for (const request of requests || []) {
        if (!request || typeof request.id !== "string") continue;
        seen.add(request.id);
        if (entries.has(request.id)) { entries.get(request.id).request = request; continue; }
        const { box, parts } = buildCard(request);
        const entry = { request, card: box, parts, busy: false, key: null };
        entries.set(request.id, entry);
        list.append(box);
        if (request.supersedes) announce(t("approvals.announce_resubmitted", { tool: request.tool_name || "?" }));
        else announce(t("approvals.announce_new", { tool: request.tool_name || "?" }));
      }
      for (const id of [...entries.keys()]) {
        if (!seen.has(id) && !entries.get(id).busy) {
          const focused = entries.get(id).parts.buttons.includes(doc.activeElement);
          const index = removeEntry(id);
          announce(t("approvals.announce_resolved_elsewhere"));
          if (focused) focusAfterRemoval(index);
        }
      }
      for (const entry of entries.values()) tickExpiry(entry);
      syncPanel();
    }

    async function decide(id, action) {
      const entry = entries.get(id);
      if (!entry || entry.busy) return false;
      const { parts, request } = entry;
      parts.error.textContent = "";
      const allowing = action === "allow_session" || action === "allow_always";
      const payload = { action: allowing ? "approve" : action, args_hash: request.args_hash, client_id: clientId };
      if (allowing) {
        // ADR 0025 §4a: the press carries only the view's one-time token (the server
        // recomputes the class from the stored request).
        const view = allowView(request);
        if (!view.buttons) return false;
        payload.allow_again = action === "allow_session" ? "session" : "always";
        payload.view_token = request.allow_again.token;
      }
      if (action === "reject") {
        const reason = parts.reason.value.trim();
        if (reason) payload.reason = reason;
      } else if (action === "edit") {
        let edited;
        try { edited = JSON.parse(parts.edit.value); } catch {
          parts.error.textContent = t("chat.tool_input_must_be_json");
          return false;
        }
        if (!edited || Array.isArray(edited) || typeof edited !== "object") {
          parts.error.textContent = t("chat.tool_input_must_be_an_object");
          return false;
        }
        payload.edited_input = edited;
      } else if (action === "respond") {
        payload.response = parts.respond.value;
      }
      // One key per attempted decision; a retry after a network error reuses it.
      const signature = JSON.stringify(payload);
      if (!entry.key || entry.keyFor !== signature) { entry.key = randomId(); entry.keyFor = signature; }
      entry.busy = true;
      tickExpiry(entry);
      let ok = false, allowed = null;
      try {
        const response = await post(`/api/hitl/${encodeURIComponent(id)}/decision`, payload, entry.key);
        const data = await response.json().catch(() => ({}));
        if (response.ok) {
          ok = true;
          allowed = allowing ? (data && typeof data.allow === "object" ? data.allow : {}) : null;
        } else {
          parts.error.textContent = ERRORS[data.code] ? t(ERRORS[data.code]) : (data.error || t("chat.decision_failed"));
          if (data.code === "binding_mismatch") entry.key = null;
        }
      } catch {
        parts.error.textContent = t("approvals.error_network");
      } finally {
        entry.busy = false;
      }
      if (ok) {
        const focused = parts.buttons.includes(doc.activeElement) || page;
        const index = removeEntry(id);
        announce(t(`approvals.announce_${allowing ? "approve" : action}`, { tool: request.tool_name || "?" }));
        syncPanel();
        if (focused) focusAfterRemoval(index);
        if (allowing) allowResult(request, action, allowed);
      } else if (entries.has(id)) {
        tickExpiry(entry);
      }
      void refresh();
      return ok;
    }

    // ADR 0025: the result line of a press (§4a step 3, §9), to the chat log and the badge.
    function allowResult(request, action, result) {
      const tool = request.tool_name || "?";
      const status = result && typeof result.status === "string" ? result.status : "";
      let text;
      if (action === "allow_session") {
        text = status === "allowed"
          ? t("approvals.allow_result_allowed", { what: allowClassText(tool, request.allow_again) })
          : t("approvals.allow_result_once");
      } else {
        text = t("approvals.allow_result_always_pending", { tool });
      }
      announce(text);
      try {
        doc.dispatchEvent(new root.CustomEvent("stira:allow-result", { detail: { text, action, request_id: request.id, tool } }));
      } catch (e) { /* no CustomEvent: the announcement is enough */ }
      if (action === "allow_always" && root.StiraAllow && typeof root.StiraAllow.makeAlways === "function") {
        root.StiraAllow.makeAlways({ requestId: request.id, tool });
      }
    }

    function historyRow(item) {
      const row = el("tr");
      const status = STATUS_KEYS[item.status] ? item.status : null;
      const badge = el("span", "badge", status ? t(STATUS_KEYS[status]) : String(item.status ?? "?"));
      badge.dataset.state = STATUS_STATE[status] || "neutral";
      const resolver = item.resolver === "system" ? t("approvals.resolver_system")
        : item.resolver === "operator"
          ? (item.client_id === clientId ? t("approvals.resolver_this_tab") : t("approvals.resolver_operator"))
          : (item.resolver ?? null);
      const cells = [
        clock(item.resolved_at),
        null, // tool (code)
        null, // status badge
        resolutionText(item),
        turnLink(item.turn_id),
        resolver,
        clock(item.expires_at),
        item.args_hash ? String(item.args_hash).slice(0, 12) : null,
      ];
      cells.forEach((value, index) => {
        const td = el("td");
        if (index === 1) td.append(el("code", null, item.tool_name || "?"));
        else if (value !== null && typeof value === "object") td.append(value);
        else if (index === 2) {
          td.append(badge);
          if (item.superseded_by) td.append(el("span", "approval-note", ` → ${String(item.superseded_by).slice(0, 8)}`));
        } else {
          td.textContent = value ?? "—";
          if (value === null) td.dataset.trend = "missing";
        }
        row.append(td);
      });
      return row;
    }

    let historyLoaded = false;
    function renderHistoryError() {
      const row = el("tr");
      const td = el("td", "empty", t("approvals.history_failed"));
      td.setAttribute("colspan", "8");
      row.append(td);
      historyBody.replaceChildren(row);
    }

    function renderHistory(items) {
      if (!historyBody) return;
      historyLoaded = true;
      const rows = (items || []).map(historyRow);
      if (!rows.length) {
        const row = el("tr");
        const td = el("td", "empty", t("approvals.history_empty"));
        td.setAttribute("colspan", "8");
        row.append(td);
        rows.push(row);
      }
      historyBody.replaceChildren(...rows);
    }

    function withTimeout(promise, ms) {
      if (typeof root.setTimeout !== "function") return promise;
      let timer;
      const limit = new Promise((_, reject) => { timer = root.setTimeout(() => reject(new Error("timeout")), ms); });
      return Promise.race([promise, limit]).finally(() => root.clearTimeout?.(timer));
    }

    async function loadHistory() {
      if (!historyBody || historyBusy) return;
      historyBusy = true;
      historyAt = Date.now();
      try {
        const past = await withTimeout(getJSON("/api/hitl/history?limit=50"), HISTORY_TIMEOUT_MS);
        renderHistory(past?.history);
      } catch {
        if (!historyLoaded) renderHistoryError();
        if (options.status) options.status.textContent = t("chat.approval_refresh_failed");
        historyAt = Date.now() - HISTORY_REFRESH_MS + HISTORY_RETRY_MS; // try again soon
      } finally {
        historyBusy = false;
      }
    }

    // Resolves to null when the queue could not be read, otherwise to whether something
    // needs a quick look again (the queue changed or still holds a request): the pollers
    // (poll.js, #369) slow down only on false. The history is re-read when the queue changed
    // (a decision, an expiry) and at least every HISTORY_REFRESH_MS, not on every poll.
    async function refresh() {
      if (loading) return undefined;
      loading = true;
      try {
        const data = await withTimeout(getJSON("/api/hitl/pending"), PENDING_TIMEOUT_MS);
        const requests = Array.isArray(data?.requests) ? data.requests : [];
        render(requests, data?.server_time);
        if (options.status) options.status.textContent = "";
        const ids = requests.map(request => String(request?.id)).sort().join("|");
        const changed = ids !== lastIds;
        lastIds = ids;
        // The history is its own read, with its own guard and timeout: it never keeps this
        // refresh (and so the queue's next poll) waiting, and its failure is not the queue's.
        if (historyBody && (changed || Date.now() - historyAt >= HISTORY_REFRESH_MS)) void loadHistory();
        return changed || requests.length > 0;
      } catch {
        if (options.status) options.status.textContent = t("chat.approval_refresh_failed");
        for (const entry of entries.values()) entry.parts.error.textContent = t("chat.approval_refresh_failed");
        // Unknown is not "none pending": hide the empty-state text until a refresh succeeds.
        if (options.empty) options.empty.hidden = true;
        if (historyBody && !historyLoaded) renderHistoryError();
        return null;
      } finally {
        loading = false;
      }
    }

    function tick() { for (const entry of entries.values()) tickExpiry(entry); }

    return { refresh, render, renderHistory, decide, tick, entries, clientId };
  }

  root.StiraApprovals = { create, pretty, allowView, ALLOW_AGAIN_ON };
})(typeof window !== "undefined" ? window : globalThis);
