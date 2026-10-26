/* Settings page: the goal grants panel (ADR 0011 §8 / §12; W4 of #296). Shown only when the
 * server runs with gateway autonomy.stage_c (the template renders the section then).
 *
 * GET /api/grants → { healthy, unhealthy_reason, authority_killed, tools, instance,
 *   grants: [ { grant_id, short, state, reason, goal_state, goal_reason, headline,
 *   ttl_left_sec, budget_left, budget_total, iterations, max_iterations, ... } ] }
 * Structured state only: the goal text is never in that answer and never rendered here,
 * except in the issuer-only view (the one-time token the issue answer gave, kept in this
 * tab's sessionStorage, and the typed word SHOW).
 *
 * Issue, resume and verify need a host-confirmed operation (428
 * grant_confirmation_required): the shared confirmation dialog (one tick), POST
 * /api/grants/operations/<id>/confirm, then the same request again with
 * X-Stira-Host-Operation — or, with the host command / dialog channel, a notice and a poll
 * of GET /api/grants/operations/<id> until the person decided on the host. Revoke, pause
 * and the kill switch are one press. Nothing here runs an iteration by itself: with gateway
 * autonomy.stage_c_exec the "Run one iteration" button posts /api/grants/<id>/step (one
 * iteration per press; W6d heartbeat starts are the scheduler's, opt-in at issue).
 *
 * StiraGrants.render(doc, data) is pure (tests); StiraGrants.load(doc).
 */
(function (root) {
  "use strict";
  const tr = (key, vars) => (typeof t === "function" ? t(key, vars) : key);
  const ROUTE = "/api/grants";
  const HOST_HEADER = "X-Stira-Host-Operation";
  const VIEW_HEADER = "X-Stira-Goal-View";
  const HEX32 = /^[0-9a-f]{32}$/;
  const INSTANCE = /^[0-9a-f]{16}$/;
  const CODE = /^[a-z][a-z0-9_]{0,47}$/;
  const STATES = ["active", "paused", "halted", "revoked", "expired", "ended"];
  const GOALS = ["active", "paused", "blocked", "budget_limited", "awaiting_verification", "complete", "unmet"];
  const TERMINAL = ["revoked", "expired", "ended"];
  const TOOLS = ["file_read", "editor", "run_checks", "memory_read", "memory_search", "memory_list", "current_time"];
  const ITERATION_STATES = ["running", "yielded", "idle"];
  // W6c: the claim evidence's codes (grants/schema.py; kept equal by a test)
  const CLAIM_CHECKS = ["pass", "fail", "not_run", "unknown"];
  const CLAIM_WORKSPACE = ["changed", "unchanged", "unknown"];
  const CLAIM_PROGRESS = ["new", "repeat", "none", "unknown"];
  const CHECK_KINDS = ["pytest", "unittest", "node_test", "ruff", "compileall"];
  const CHECK_STATUSES = ["passed", "failed", "error", "limited"];
  const CHECK_COUNT_KEYS = ["passed", "failed", "errors", "skipped", "findings"];
  // W6d: the heartbeat start's skip reasons (scheduler/grant_tick.py SKIP_REASONS; a test
  // keeps them equal)
  const TICK_SKIPS = ["not_enabled", "no_grant", "grant_state", "goal_state", "awaiting_verification", "heartbeat_unhealthy", "breaker", "kill", "authority_unhealthy", "store_unhealthy", "clock", "ttl", "max_iterations", "budget", "turn_busy", "iteration_running", "abandoned_call", "min_spacing", "no_action", "flood", "executor_unavailable", "executor_refused", "narrowed", "scheduler_stopped", "shutdown", "repeated_failure"];
  const n = (value) => (Number.isFinite(Number(value)) ? Number(value) : 0);
  const code = (value) => (typeof value === "string" && CODE.test(value) ? value : "");

  function node(doc, tag, attrs, text) {
    const el = doc.createElement(tag);
    for (const [name, value] of Object.entries(attrs || {})) {
      if (name === "className") el.className = value;
      else el.setAttribute(name, value);
    }
    if (text !== undefined) el.textContent = text;
    return el;
  }

  function stateLabel(kind, value) {
    const list = kind === "goal" ? GOALS : STATES;
    return list.includes(value) ? tr(`grants.${kind}_${value}`) : "?";
  }

  function viewToken(grantId) {
    try {
      const value = root.sessionStorage && root.sessionStorage.getItem(`stira.grant.view.${grantId}`);
      return typeof value === "string" && HEX32.test(value) ? value : null;
    } catch (_e) {
      return null;
    }
  }

  function keepToken(grantId, token) {
    try {
      if (HEX32.test(grantId) && HEX32.test(token)) root.sessionStorage.setItem(`stira.grant.view.${grantId}`, token);
    } catch (_e) { /* a private window: no issuer view later */ }
  }

  function headlineText(grant) {
    const head = grant.headline || {};
    const label = stateLabel(head.subject === "goal" ? "goal" : "grant", head.state);
    const reason = code(head.reason);
    return reason ? `${label} (${reason})` : label;
  }

  function row(doc, list, label, value) {
    list.appendChild(node(doc, "dt", {}, label));
    list.appendChild(node(doc, "dd", {}, value));
  }

  const count = (value) => (Number.isInteger(value) && value >= 0 ? value : null);
  function hhmm(seconds) {
    if (!Number.isFinite(seconds)) return "--:--";
    const d = new Date(seconds * 1000);
    const p = (x) => String(x).padStart(2, "0");
    return `${p(d.getHours())}:${p(d.getMinutes())}`;
  }

  // W6c: the evidence of the last completion claim — codes and observed counts the harness
  // measured (never model text): while it waits for the person (since <time>, "still waiting
  // for you" past autonomy.claim_stale_sec) and, read-only, after it was confirmed. A claim
  // whose checks failed gets a "⚠" notice in words. -> null | { rows: [[label, value]], notes }
  function claimEvidence(grant) {
    const c = grant.claim_evidence;
    if (!c || typeof c !== "object") return null;
    const waiting = grant.goal_state === "awaiting_verification" && !TERMINAL.includes(grant.state);
    if (!waiting && grant.goal_state !== "complete") return null;
    let checks = CLAIM_CHECKS.includes(c.checks) ? c.checks : "unknown";
    const raw = Array.isArray(c.check_rows) ? c.check_rows : [];
    let checkRows = [];
    if (checks === "pass" || checks === "fail") {
      const ok = raw.length > 0 && raw.length <= 16 && raw.every((r) => r && typeof r === "object" && CHECK_STATUSES.includes(r.status)
        && r.counts && typeof r.counts === "object" && CHECK_COUNT_KEYS.every((k) => count(r.counts[k]) !== null));
      // unreadable, or a "pass" with a row that did not pass: unknown, never pass
      if (!ok || (checks === "pass") !== raw.every((r) => r.status === "passed")) checks = "unknown";
      else checkRows = raw.map((r) => tr("grants.claim_row", {
        kind: CHECK_KINDS.includes(r.kind) ? r.kind : "other", status: tr(`grants.check_${r.status}`),
        passed: r.counts.passed, failed: r.counts.failed, errors: r.counts.errors, skipped: r.counts.skipped, findings: r.counts.findings,
      }));
    }
    const workspace = CLAIM_WORKSPACE.includes(c.workspace) ? c.workspace : "unknown";
    const progress = CLAIM_PROGRESS.includes(c.progress) ? c.progress : "unknown";
    const k = count(c.iteration);
    const max = count(c.max_iterations);
    const rows = [];
    if (waiting) {
      const wait = count(c.waiting_sec);
      rows.push([tr("grants.field_claim_since"), wait === null ? hhmm(c.claimed_at) : tr("grants.claim_since", { time: hhmm(c.claimed_at), n: Math.floor(wait / 60) })]);
    } else rows.push([tr("grants.field_claim_audit"), hhmm(c.claimed_at)]);
    rows.push([tr("grants.field_claim_iteration"), tr("grants.of", { a: k === null ? "-" : k, b: max === null ? "-" : max })]);
    rows.push([tr("grants.field_claim_checks"), [tr(`grants.claim_checks_${checks}`), ...checkRows].join(" · ")]);
    rows.push([tr("grants.field_claim_workspace"), tr(`grants.claim_workspace_${workspace}`)]);
    rows.push([tr("grants.field_claim_progress"), tr(`grants.claim_progress_${progress}`)]);
    const used = count(c.used_tokens);
    const left = count(c.budget_left);
    const ttl = count(c.ttl_left_sec);
    if (used !== null && left !== null && ttl !== null) {
      rows.push([tr("grants.field_claim_at"), tr("grants.claim_at", { used, left, n: Math.ceil(ttl / 60) })]);
    }
    const notes = [];
    if (checks === "fail") notes.push(tr("grants.claim_fail"));
    if (waiting && c.stale === true) notes.push(tr("grants.claim_stale"));
    return { rows, notes, amber: checks === "fail", stale: waiting && c.stale === true };
  }

  function button(doc, id, label, action, grantId) {
    const el = node(doc, "button", { type: "button", id, className: "btn btn-secondary" }, label);
    el.dataset.action = action;
    el.dataset.grant = grantId;
    return el;
  }

  // W6d: a grant issued with the heartbeat start — its starts, the last tick, the skips by
  // reason and the last heartbeat-started iteration's tokens and wall time (codes, numbers).
  function tickRows(grant) {
    const tick = grant.heartbeat_start === true && grant.tick && typeof grant.tick === "object" ? grant.tick : null;
    if (!tick) return [];
    const rows = [[tr("grants.field_tick"), tr("grants.tick_on", { n: n(tick.started), min: Math.round(n(tick.spacing_sec) / 60) })]];
    if (tick.unavailable === "heartbeat_off" || tick.unavailable === "scheduler_off") rows.push([tr("grants.field_tick_unavailable"), tr(`grants.tick_unavailable_${tick.unavailable}`)]);
    const last = code(tick.last);
    if (last) rows.push([tr("grants.field_tick_last"), last === "started" ? tr("grants.tick_started") : TICK_SKIPS.includes(last) ? tr(`grants.tick_skip_${last}`) : "?"]);
    const skipped = tick.skipped && typeof tick.skipped === "object" ? tick.skipped : {};
    const parts = TICK_SKIPS.filter((r) => Number.isInteger(skipped[r]) && skipped[r] > 0).map((r) => `${tr(`grants.tick_skip_${r}`)} ×${skipped[r]}`);
    if (parts.length) rows.push([tr("grants.field_tick_skipped"), parts.join(", ")]);
    const costs = Array.isArray(tick.costs) ? tick.costs : [];
    const cost = costs.length && costs[costs.length - 1] && typeof costs[costs.length - 1] === "object" ? costs[costs.length - 1] : null;
    if (cost) {
      let text = tr("grants.tick_cost", { calls: n(cost.model_calls), in: n(cost.tokens_in), out: n(cost.tokens_out), sec: Math.round(n(cost.wall_ms) / 1000) });
      if (Number.isInteger(cost.cache_read) || Number.isInteger(cost.cache_write)) text += ` · ${tr("grants.tick_cache", { read: n(cost.cache_read), write: n(cost.cache_write) })}`;
      rows.push([tr("grants.field_tick_cost"), text]);
    }
    return rows;
  }

  function card(doc, grant) {
    const id = HEX32.test(grant.grant_id) ? grant.grant_id : null;
    if (!id) return null;
    const short = id.slice(0, 8);
    const box = node(doc, "article", { className: "grant-card", "aria-labelledby": `grant-${short}-title` });
    box.appendChild(node(doc, "h3", { id: `grant-${short}-title` }, tr("grants.card_title", { id: short, state: headlineText(grant) })));
    const list = node(doc, "dl", { className: "summary-grid" });
    const reason = code(grant.reason);
    row(doc, list, tr("grants.field_state"), reason ? `${stateLabel("grant", grant.state)} (${reason})` : stateLabel("grant", grant.state));
    const goalReason = code(grant.goal_reason);
    row(doc, list, tr("grants.field_goal_state"), goalReason ? `${stateLabel("goal", grant.goal_state)} (${goalReason})` : stateLabel("goal", grant.goal_state));
    row(doc, list, tr("grants.field_ttl"), tr("grants.minutes", { n: Math.ceil(n(grant.ttl_left_sec) / 60) }));
    row(doc, list, tr("grants.field_budget"), tr("grants.of", { a: n(grant.budget_left), b: n(grant.budget_total) }));
    row(doc, list, tr("grants.field_iterations"), tr("grants.of", { a: n(grant.iterations), b: n(grant.max_iterations) }));
    row(doc, list, tr("grants.field_stop"), grant.state !== "active" ? (reason || "-") : "-");
    const it = grant.iteration && typeof grant.iteration === "object" ? grant.iteration : null;
    if (it) {
      // W5: the iteration's state, its stop reason and a waiting card (codes only).
      const itState = ITERATION_STATES.includes(it.state) ? tr(`grants.iteration_${it.state}`) : "?";
      const end = code(it.end);
      const detail = code(it.detail);
      const shown = end ? `${itState} (${detail ? `${end}: ${detail}` : end})` : itState;
      row(doc, list, tr("grants.field_iteration"), `${shown} #${n(it.number)}`);
      if (it.card === "waiting") {
        list.appendChild(node(doc, "dt", {}, tr("grants.field_card")));
        const dd = node(doc, "dd", {});
        dd.appendChild(node(doc, "a", { href: "/approvals" }, tr("grants.card_waiting")));
        list.appendChild(dd);
      }
    }
    if (grant.late_verification === "pass" || grant.late_verification === "fail") {
      row(doc, list, tr("grants.field_late"), tr(`grants.late_${grant.late_verification}`));
    }
    for (const [label, value] of tickRows(grant)) row(doc, list, label, value);
    const claim = claimEvidence(grant);
    if (claim) for (const [label, value] of claim.rows) row(doc, list, label, value);
    box.appendChild(list);
    if (claim) {
      if (claim.amber) box.dataset.claim = "fail";
      if (claim.stale) box.dataset.claimStale = "1";
      for (const note of claim.notes) box.appendChild(node(doc, "p", { className: "notice warn grant-claim-note" }, note));
    }
    const actions = node(doc, "p", { className: "grant-actions" });
    const live = !TERMINAL.includes(grant.state);
    if (live) actions.appendChild(button(doc, `grant-${short}-revoke`, tr("grants.revoke"), "revoke", id));
    if (it && grant.state === "active" && grant.goal_state === "active" && it.state !== "running") {
      actions.appendChild(button(doc, `grant-${short}-step`, tr("grants.step"), "step", id));
    }
    if (grant.state === "active") actions.appendChild(button(doc, `grant-${short}-pause`, tr("grants.pause"), "pause", id));
    if (grant.state === "halted") {
      const label = node(doc, "label", { for: `grant-${short}-word` }, tr("grants.resume_word_label"));
      actions.appendChild(label);
      actions.appendChild(node(doc, "input", { id: `grant-${short}-word`, type: "text", autocomplete: "off", spellcheck: "false" }));
    }
    if (grant.state === "paused" || grant.state === "halted") {
      actions.appendChild(button(doc, `grant-${short}-resume`, tr("grants.resume"), "resume", id));
    }
    if (grant.goal_state === "awaiting_verification" && (grant.state === "active" || !live)) {
      actions.appendChild(button(doc, `grant-${short}-confirm`, tr("grants.verify_confirm"), "verify-confirm", id));
      actions.appendChild(button(doc, `grant-${short}-reject`, tr("grants.verify_reject"), "verify-reject", id));
    }
    if (viewToken(id)) {
      actions.appendChild(node(doc, "label", { for: `grant-${short}-show` }, tr("grants.show_word_label")));
      actions.appendChild(node(doc, "input", { id: `grant-${short}-show`, type: "text", autocomplete: "off", spellcheck: "false" }));
      actions.appendChild(button(doc, `grant-${short}-goal`, tr("grants.show_goal"), "show-goal", id));
    }
    box.appendChild(actions);
    box.appendChild(node(doc, "div", { id: `grant-${short}-goal-view`, className: "grant-goal-view" }));
    return box;
  }

  function toolChoices(doc, tools) {
    const box = doc.getElementById("grants-tools");
    if (!box || box.childElementCount) return;
    for (const tool of tools.filter((name) => TOOLS.includes(name))) {
      const id = `grants-tool-${tool}`;
      const label = node(doc, "label", { for: id, className: "choice" });
      const input = node(doc, "input", { id, type: "checkbox", value: tool });
      if (tool === "file_read") input.checked = true;
      label.appendChild(input);
      label.appendChild(node(doc, "span", {}, tool));
      box.appendChild(label);
    }
  }

  function render(doc, data) {
    const status = doc.getElementById("grants-status");
    const list = doc.getElementById("grants-list");
    if (!status || !list) return;
    list.textContent = "";
    if (!data || typeof data !== "object" || !Array.isArray(data.grants)) {
      status.textContent = tr("grants.unavailable");
      return;
    }
    const parts = [data.healthy ? tr("grants.healthy") : tr("grants.unhealthy", { code: code(data.unhealthy_reason) || "?" })];
    if (data.authority_killed) parts.push(tr("grants.killed"));
    if (!data.grants.length) parts.push(tr("grants.none"));
    status.textContent = parts.join(" ");
    for (const grant of data.grants) {
      const box = card(doc, grant);
      if (box) list.appendChild(box);
    }
    toolChoices(doc, Array.isArray(data.tools) ? data.tools : TOOLS);
  }

  async function getJSON(url) {
    const response = await root.fetch(url, { headers: { Accept: "application/json" }, cache: "no-store" });
    return response.json();
  }

  function showError(doc, data) {
    const box = doc.getElementById("grants-error");
    if (!box) return;
    const value = data && (code(data.host_code) || code(data.code));
    const reason = data && code(data.reason);
    box.textContent = !value ? "" : value === "host_channel_required"
      ? tr("grants.host_channel_required")
      : tr("grants.refused", { code: reason ? `${value}: ${reason}` : value });
    box.hidden = !value;
  }

  async function post(url, body, headers) {
    const response = await root.stiraFetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json", ...(headers || {}) },
      body: JSON.stringify(body || {}),
    });
    const data = await response.json().catch(() => ({}));
    return { response, data };
  }

  const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  // The host command / dialog channel (STIRA_YOLO_HOST_CONFIRM): the person decides on the
  // host; the page polls the operation and re-sends only when it was confirmed.
  async function waitOnHost(doc, url, body, op) {
    const status = doc.getElementById("grants-status");
    const short = /^[0-9a-f]{8}$/.test(op.short || "") ? op.short : "?";
    if (status) status.textContent = tr("grants.confirm_on_host", { short });
    const limit = Date.now() + ((Number(op.expires_in) || 120) + 30) * 1000;
    while (Date.now() < limit) {
      await pause(root.StiraGrantsPollMs || 1500);
      let state = null;
      try {
        const response = await root.fetch(`/api/grants/operations/${op.id}`, { headers: { Accept: "application/json" }, cache: "no-store" });
        const data = await response.json();
        state = data && data.operation && data.operation.state;
      } catch (_e) {
        continue;
      }
      if (state === "pending") continue;
      if (state === "confirmed") return post(url, body, { [HOST_HEADER]: op.id });
      return { response: { ok: false, status: 409 }, data: { code: "grant_operation_refused", host_code: `host_operation_${code(state) || "unknown"}` } };
    }
    return { response: { ok: false, status: 409 }, data: { code: "grant_operation_refused", host_code: "host_operation_expired" } };
  }

  // Issue / resume / verify: the host-confirmed operation (one tick in the shared dialog,
  // or the host command / dialog when the server is configured so).
  async function gated(url, body, kind, doc) {
    const first = await post(url, body);
    if (first.response.status !== 428 || first.data.code !== "grant_confirmation_required") return first;
    const op = first.data.operation || {};
    if (HEX32.test(op.id || "") && (op.channel === "command" || op.channel === "dialog")) {
      return waitOnHost(doc || root.document, url, body, op);
    }
    if (!HEX32.test(op.id) || !HEX32.test(op.confirm_token || "") || !INSTANCE.test(op.instance || "")
        || !root.StiraConfirm || typeof root.StiraConfirm.open !== "function") {
      return { response: first.response, data: { code: "grant_operation_refused", host_code: "host_channel_unavailable" } };
    }
    const summary = op.summary && typeof op.summary === "object" ? op.summary : {};
    const items = [];
    if (Array.isArray(summary.tool)) items.push(tr("grants.confirm_tools", { list: summary.tool.filter((x) => TOOLS.includes(x)).join(", ") }));
    if (Number.isInteger(summary.count)) items.push(tr("grants.confirm_minutes", { n: summary.count }));
    if (summary.source === "heartbeat_start") items.push(tr("grants.confirm_heartbeat_start"));
    if (typeof summary.operation === "string" && CODE.test(summary.operation.replace("#", "_"))) items.push(summary.operation);
    const ok = await root.StiraConfirm.open({
      title: tr(`grants.confirm_${kind}`),
      intro: tr("grants.confirm_intro"),
      items,
      checks: [tr("grants.confirm_check")],
      confirmLabel: tr(kind === "verify" ? "grants.verify_ok" : `grants.${kind}`),
      cancelLabel: tr("hub.cancel"),
      tone: "danger",
    });
    if (!ok) {
      post(`/api/grants/operations/${op.id}/cancel`, {}).catch(() => {});
      return { response: first.response, data: { code: "grant_operation_refused", host_code: "host_operation_cancelled" } };
    }
    const confirmed = await post(`/api/grants/operations/${op.id}/confirm`, { token: op.confirm_token, acknowledged: true, instance: op.instance });
    if (!confirmed.response.ok) return confirmed;
    return post(url, body, { [HOST_HEADER]: op.id });
  }

  function issueBody(doc) {
    const value = (id) => (doc.getElementById(id) || {}).value || "";
    const tools = TOOLS.filter((tool) => {
      const box = doc.getElementById(`grants-tool-${tool}`);
      return box && box.checked;
    });
    return {
      goal: value("grants-goal"),
      scope: { tools, workspace_subtree: value("grants-subtree").trim() },
      budget: { total_tokens: Math.trunc(n(value("grants-total"))), per_tick_tokens: Math.trunc(n(value("grants-tick"))) },
      ttl_sec: Math.trunc(n(value("grants-ttl"))) * 60,
      max_iterations: Math.trunc(n(value("grants-iterations"))),
      verifier: "user",
      confirm: value("grants-confirm"),
      // W6d: only when the switch is on (the box is rendered) and checked
      ...((doc.getElementById("grants-heartbeat-start") || {}).checked === true ? { heartbeat_start: true } : {}),
    };
  }

  async function act(doc, action, grantId) {
    const short = grantId.slice(0, 8);
    let result = null;
    if (action === "revoke" || action === "pause" || action === "step") {
      result = await post(`/api/grants/${grantId}/${action}`, {});
    } else if (action === "resume") {
      const word = doc.getElementById(`grant-${short}-word`);
      result = await gated(`/api/grants/${grantId}/resume`, word ? { confirm: word.value } : {}, "resume", doc);
    } else if (action === "verify-confirm" || action === "verify-reject") {
      result = await gated(`/api/grants/${grantId}/verify`, { decision: action === "verify-confirm" ? "confirm" : "reject" }, "verify", doc);
    } else if (action === "show-goal") {
      const word = (doc.getElementById(`grant-${short}-show`) || {}).value || "";
      const token = viewToken(grantId);
      const response = await root.fetch(`/api/grants/${grantId}/goal?confirm=${encodeURIComponent(word)}`, {
        headers: { Accept: "application/json", [VIEW_HEADER]: token || "" }, cache: "no-store",
      });
      const data = await response.json().catch(() => ({}));
      const box = doc.getElementById(`grant-${short}-goal-view`);
      if (box) {
        box.textContent = "";
        if (response.ok && typeof data.goal === "string") box.appendChild(node(doc, "pre", { className: "grant-goal" }, data.goal));
      }
      // One time: only a successful view spent the token on the server; a refused one
      // (a mistyped SHOW) keeps it, so the person can try again.
      if (response.ok) {
        try { root.sessionStorage.removeItem(`stira.grant.view.${grantId}`); } catch (_e) { /* none kept */ }
      }
      showError(doc, response.ok ? null : data);
      return;
    }
    showError(doc, result && !result.response.ok ? result.data : null);
    await load(doc);
  }

  async function load(doc, get) {
    let data = null;
    try {
      data = await (get || getJSON)(ROUTE);
    } catch (_error) {
      data = null;
    }
    render(doc, data);
  }

  function wire(doc) {
    const list = doc.getElementById("grants-list");
    if (list) {
      list.addEventListener("click", (event) => {
        const target = event.target.closest("button[data-action]");
        if (target && HEX32.test(target.dataset.grant || "")) act(doc, target.dataset.action, target.dataset.grant);
      });
    }
    const kill = doc.getElementById("grants-kill");
    if (kill) {
      kill.addEventListener("click", async () => {
        const result = await post("/api/authority/kill", {});
        showError(doc, result.response.ok ? null : result.data);
        await load(doc);
      });
    }
    const form = doc.getElementById("grants-issue-form");
    if (form) {
      form.addEventListener("submit", async (event) => {
        event.preventDefault();
        const result = await gated("/api/grants/issue", issueBody(doc), "issue", doc);
        if (result.response.ok && result.data && result.data.grant) {
          keepToken(result.data.grant.grant_id, result.data.goal_view_token || "");
          form.reset();
        }
        showError(doc, result.response.ok ? null : result.data);
        await load(doc);
      });
    }
  }

  function start() {
    const doc = root.document;
    if (!doc || !doc.getElementById("grants-list")) return;
    wire(doc);
    load(doc);
    if (!root.StiraGrantsNoPoll) root.setInterval(() => load(doc), 15000);
  }

  root.StiraGrants = { render, load, issueBody, gated, claimEvidence, tickRows };
  if (root.document && root.document.readyState === "loading") {
    root.document.addEventListener("DOMContentLoaded", start);
  } else if (root.document) {
    start();
  }
})(typeof window !== "undefined" ? window : globalThis);
