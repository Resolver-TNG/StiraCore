/* Scheduled jobs page (#239): status, jobs, recent runs and health notifications.
 *
 * Read-only by default. Pause and cancel are always offered (they only narrow what
 * runs); resume asks for confirmation first (shared dialog, #282) and sends "confirm": true. Every change
 * goes through stiraFetch (CSRF token + Idempotency-Key) and the server re-checks
 * principal, confirmation and breaker level.
 */
(() => {
  "use strict";
  const $ = id => document.getElementById(id);
  const tr = (key, vars) => (typeof t === "function" ? t(key, vars) : key);
  const STATE_KEYS = {
    active: "schedules.state_active",
    paused: "schedules.state_paused",
    finished: "schedules.state_finished",
    running: "schedules.run_running",
    completed: "schedules.run_completed",
    failed: "schedules.run_failed",
    cancelled: "schedules.run_cancelled",
    awaiting_approval: "schedules.run_awaiting_approval",
  };
  let busy = false;

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function when(epoch) {
    if (typeof epoch !== "number") return "—";
    return new Date(epoch * 1000).toLocaleString();
  }

  function label(state) {
    return STATE_KEYS[state] ? tr(STATE_KEYS[state]) : String(state ?? "—");
  }

  // Same words as the sidebar chip (shell.*); unknown values are shown as they are.
  const BREAKER_KEYS = {
    normal: "shell.normal", warning: "shell.warning", critical: "shell.critical", coherence: "shell.coherence",
  };
  function breakerLabel(level) {
    if (!level) return "—";
    const name = String(level).toLowerCase();
    const key = Object.prototype.hasOwnProperty.call(BREAKER_KEYS, name) ? BREAKER_KEYS[name] : null;
    return key ? tr(key) : String(level);
  }

  // #279: the server sends codes (alerts, run reasons, notification codes) and, for
  // notifications, allowlisted variables (`params`); the page words them from the catalog.
  // A code it does not know is never shown bare or guessed: "Other (code: ...)" when it
  // has the shape of a code, plain "Other" when it does not.
  const NOTE_KEYS = {
    breaker_critical: "schedules.code_breaker_critical",
    heartbeat_stalled: "schedules.code_heartbeat_stalled",
    job_failing: "schedules.code_job_failing",
    log_recovered: "schedules.code_log_recovered",
    budget_exhausted: "schedules.code_budget_exhausted",
    approval_required: "schedules.code_approval_required",
    // ADR 0021 §10 (S1)
    heartbeat_required: "schedules.code_heartbeat_required",
    jobs_paused_at_start: "schedules.code_jobs_paused_at_start",
    // ADR 0024 phase 2 (S3)
    approval_waiting: "schedules.code_approval_waiting",
  };
  const REASON_KEYS = {
    interrupted: "schedules.reason_interrupted",
    shutdown: "schedules.reason_shutdown",
    breaker_critical: "schedules.reason_breaker_critical",
    operator: "schedules.reason_operator",
    approval_required: "schedules.reason_approval_required",
    budget_exceeded: "schedules.reason_budget_exceeded",
    budget: "schedules.reason_budget",
    dispatch_failed: "schedules.reason_dispatch_failed",
    action_unavailable: "schedules.reason_action_unavailable",
    agent_jobs_disabled: "schedules.reason_agent_jobs_disabled",
    invalid_outcome: "schedules.reason_invalid_outcome",
    log_unavailable: "schedules.reason_log_unavailable",
    job_updated: "schedules.reason_job_updated",
    unavailable: "schedules.reason_unavailable",
    recovering: "schedules.reason_recovering",
    paused: "schedules.reason_paused",
    cancel_backlog: "schedules.reason_cancel_backlog",
    memory_jobs_disabled: "schedules.reason_memory_jobs_disabled",
    model_call_not_allowed: "schedules.reason_model_call_not_allowed",
    heartbeat: "schedules.reason_heartbeat",
    approval_rejected: "schedules.reason_approval_rejected",
    approval_expired: "schedules.reason_approval_expired",
    approval_cancelled: "schedules.reason_approval_cancelled",
    approval_unavailable: "schedules.reason_approval_unavailable",
    binding_mismatch: "schedules.reason_binding_mismatch",
  };
  const BUDGET_KIND_KEYS = {
    max_runs_per_day: "schedules.budget_kind_max_runs_per_day",
    max_tokens_per_day: "schedules.budget_kind_max_tokens_per_day",
    daily_token_budget: "schedules.budget_kind_daily_token_budget",
    max_tokens_per_run: "schedules.budget_kind_max_tokens_per_run",
    agent_daily_token_budget: "schedules.budget_kind_agent_daily_token_budget",
  };
  const has = (table, code) => typeof code === "string" && Object.prototype.hasOwnProperty.call(table, code);
  const CODE_SHAPE = /^[A-Za-z][A-Za-z0-9_]{0,47}$/;
  const TOOL_SHAPE = /^[A-Za-z0-9_.-]{1,64}$/;
  const isCount = value => Number.isInteger(value) && value >= 0;
  const codeShaped = code => typeof code === "string" && CODE_SHAPE.test(code);
  function otherText(code) {
    return codeShaped(code) ? tr("schedules.other_code", { code }) : tr("schedules.other");
  }
  // #279: why a change was refused (scheduler_api STATUS_CODES) and why the system paused a job.
  const REFUSED_KEYS = {
    invalid: "schedules.refused_invalid",
    scope: "schedules.refused_scope",
    not_found: "schedules.refused_not_found",
    conflict: "schedules.refused_conflict",
    breaker: "schedules.refused_breaker",
    confirmation_required: "schedules.refused_confirmation_required",
    unavailable: "schedules.refused_unavailable",
    tool_call_running: "schedules.refused_tool_call_running",
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
  };
  const SYSTEM_PAUSE_KEYS = { "stored definition invalid": "schedules.paused_definition_invalid" };
  function pausedReason(job) {
    if (!job.paused_reason) return null;
    const reason = String(job.paused_reason);
    // Only the scheduler's own sentence is worded; an operator's reason is shown as typed.
    return job.paused_by === "system" && has(SYSTEM_PAUSE_KEYS, reason) ? tr(SYSTEM_PAUSE_KEYS[reason]) : reason;
  }
  let jobNames = {};
  const jobName = id => (id && jobNames[id]) || id || "—";
  // `params` are server variables from an allowlist; a value of the wrong shape is ignored
  // (the plain wording of the code is used) rather than shown.
  function noteText(code, jobId, params) {
    if (!has(NOTE_KEYS, code)) return null;
    const job = jobName(jobId);
    const p = params && typeof params === "object" ? params : {};
    if (code === "job_failing" && isCount(p.count)) {
      return tr("schedules.code_job_failing_count", { job, count: p.count });
    }
    if (code === "jobs_paused_at_start" && isCount(p.count)) {
      return tr("schedules.code_jobs_paused_at_start_count", { count: p.count });
    }
    if (code === "approval_required" && typeof p.tool === "string" && TOOL_SHAPE.test(p.tool)) {
      return tr("schedules.code_approval_required_tool", { job, tool: p.tool });
    }
    if (code === "approval_waiting" && typeof p.tool === "string" && TOOL_SHAPE.test(p.tool)) {
      return tr("schedules.code_approval_waiting_tool", { job, tool: p.tool });
    }
    if (code === "budget_exhausted" && has(BUDGET_KIND_KEYS, p.kind)) {
      const kind = tr(BUDGET_KIND_KEYS[p.kind]);
      return isCount(p.limit)
        ? tr("schedules.code_budget_exhausted_limit", { job, kind, limit: p.limit })
        : tr("schedules.code_budget_exhausted_kind", { job, kind });
    }
    return tr(NOTE_KEYS[code], { job });
  }
  // Alert keys are "<code>" or "<code>:<job_id>" (scheduler._alerts).
  function alertText(key) {
    const text = String(key);
    const cut = text.indexOf(":");
    const code = cut < 0 ? text : text.slice(0, cut);
    return noteText(code, cut < 0 ? null : text.slice(cut + 1)) || otherText(code);
  }
  function reasonCell(reason) {
    if (!reason) return el("td", "sched-mono", "—");
    if (!has(REASON_KEYS, reason)) {
      // An exception class name or a code from a newer server: said as "Other", never bare.
      const other = el("td", null, otherText(reason));
      if (codeShaped(reason)) other.dataset.code = reason;
      return other;
    }
    const cell = el("td", null, tr(REASON_KEYS[reason]));
    cell.title = reason;
    cell.dataset.code = reason;
    return cell;
  }

  function scheduleText(schedule) {
    if (!schedule) return "—";
    if (schedule.type === "cron") return `cron ${schedule.expr} (${schedule.timezone})`;
    if (schedule.type === "interval") return tr("schedules.every", { sec: schedule.every_sec });
    return tr("schedules.once_at", { time: when(schedule.at) });
  }

  function announce(text) {
    $("sched-live").textContent = text;
  }

  async function post(url, body, doneKey, vars) {
    if (busy) return;
    busy = true;
    try {
      const response = await stiraFetch(url, {
        method: "POST",
        idempotent: true,
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify(body || {}),
      });
      const data = await response.json().catch(() => ({}));
      const message = $("sched-message");
      if (response.ok) {
        message.textContent = tr(doneKey, vars);
      } else {
        // #279: the refusal code is worded; the server's sentence stays as the detail.
        const worded = has(REFUSED_KEYS, data.code) ? tr(REFUSED_KEYS[data.code]) : null;
        message.textContent = tr("schedules.refused", { reason: worded || data.error || response.status });
        // A refusal the shared fetch wrapper worded itself (`local`, ADR 0012 §8b) has no
        // server sentence: no detail line (it would repeat the wording, marked English).
        if (worded && data.error && data.local !== true) {
          const detail = el("span", "sched-note-detail", ` (${data.error})`);
          detail.lang = "en";
          message.append(detail);
        }
      }
      announce(message.textContent);
    } catch (error) {
      $("sched-message").textContent = tr("schedules.request_failed");
    } finally {
      busy = false;
      void refresh();
    }
  }

  // #282: resuming widens what runs, so it is confirmed in the shared dialog (what changes,
  // Cancel is the default, Esc cancels). Without the dialog nothing is sent (fail-closed).
  // Pause and cancel only narrow what runs and stay one click.
  async function confirmResume(job, name) {
    const kind = job.layer === "agent" ? tr("schedules.kind_agent") : `${tr("schedules.kind_gateway")}: ${job.action}`;
    const ok = typeof StiraConfirm !== "undefined" && await StiraConfirm.open({
      title: tr("schedules.resume_title", { name }),
      intro: tr("schedules.resume_intro"),
      items: [
        `${tr("schedules.col_name")}: ${name}${job.name && job.job_id ? ` (${job.job_id})` : ""}`,
        `${tr("schedules.col_schedule")}: ${scheduleText(job.schedule)}`,
        `${tr("schedules.col_kind")}: ${kind}`,
      ],
      confirmLabel: tr("schedules.resume"),
      cancelLabel: tr("hub.cancel"),
      tone: "primary",
      // Sent inside the dialog's busy window: no second dialog / POST until it settles.
      onConfirm: () => post(`/api/scheduler/jobs/${encodeURIComponent(job.job_id)}/resume`, { confirm: true },
        "schedules.resumed_done", { name }),
    });
    return ok;
  }

  function jobButtons(job) {
    const cell = el("td", "sched-actions");
    const name = job.name || job.job_id;
    if (job.state === "active") {
      const pause = el("button", "btn btn-secondary", tr("schedules.pause"));
      pause.type = "button";
      pause.setAttribute("aria-label", tr("schedules.pause_label", { name }));
      pause.addEventListener("click", () =>
        post(`/api/scheduler/jobs/${encodeURIComponent(job.job_id)}/pause`, {}, "schedules.paused_done", { name }));
      cell.append(pause);
    } else if (job.state === "paused") {
      const resume = el("button", "btn btn-primary", tr("schedules.resume"));
      resume.type = "button";
      resume.setAttribute("aria-label", tr("schedules.resume_label", { name }));
      resume.addEventListener("click", () => { void confirmResume(job, name); });
      cell.append(resume);
    } else {
      cell.textContent = "—";
    }
    return cell;
  }

  function renderJobs(jobs) {
    const body = $("sched-jobs");
    body.replaceChildren();
    if (!jobs.length) {
      const row = el("tr");
      const cell = el("td", "empty", tr("schedules.jobs_empty"));
      cell.colSpan = 7;
      row.append(cell);
      body.append(row);
      return;
    }
    for (const job of jobs) {
      const row = el("tr");
      const name = el("td");
      name.append(el("span", "sched-name", job.name), el("span", "sched-id", job.job_id));
      const kind = job.layer === "agent" ? tr("schedules.kind_agent") : `${tr("schedules.kind_gateway")}: ${job.action}`;
      const state = el("td", null, label(job.state));
      state.dataset.state = job.state;
      if (job.paused_reason) state.title = pausedReason(job);
      row.append(
        name,
        el("td", "sched-mono", scheduleText(job.schedule)),
        el("td", null, kind),
        state,
        el("td", null, job.held_since ? tr("schedules.held") : when(job.next_run_at)),
        el("td", "sched-num", job.consecutive_failures),
        jobButtons(job),
      );
      body.append(row);
    }
  }

  function renderRuns(runs, jobs) {
    const names = Object.fromEntries(jobs.map(job => [job.job_id, job.name]));
    const body = $("sched-runs");
    body.replaceChildren();
    if (!runs.length) {
      const row = el("tr");
      const cell = el("td", "empty", tr("schedules.runs_empty"));
      cell.colSpan = 6;
      row.append(cell);
      body.append(row);
      return;
    }
    for (const run of runs) {
      const row = el("tr");
      const status = el("td", null, label(run.status));
      status.dataset.state = run.status;
      const actions = el("td", "sched-actions");
      if (run.status === "running") {
        const cancel = el("button", "btn btn-danger", tr("schedules.cancel"));
        cancel.type = "button";
        cancel.setAttribute("aria-label", tr("schedules.cancel_label", { name: names[run.job_id] || run.job_id }));
        cancel.addEventListener("click", () =>
          post(`/api/scheduler/runs/${encodeURIComponent(run.run_id)}/cancel`, {}, "schedules.cancelled_done"));
        actions.append(cancel);
      } else {
        actions.textContent = "—";
      }
      row.append(
        el("td", null, when(run.started_at)),
        el("td", null, names[run.job_id] || run.job_id),
        status,
        reasonCell(run.reason),
        el("td", "sched-num", run.tokens || 0),
        actions,
      );
      body.append(row);
    }
  }

  function renderNotes(notes) {
    const list = $("sched-notes");
    list.replaceChildren();
    $("sched-notes-empty").hidden = notes.length > 0;
    for (const note of notes) {
      const item = el("li", "sched-note");
      item.dataset.severity = note.severity || "info";
      const worded = noteText(note.code, note.job_id, note.params);
      const headline = worded && note.state === "resolved" ? tr("schedules.note_resolved", { text: worded }) : worded;
      const code = el("span", "sched-note-code", headline || otherText(note.code));
      if (headline) code.title = note.code;
      // The server's own sentence stays as the detail (masked audit text, English).
      const message = el("span", "sched-note-text", note.message);
      message.lang = "en";
      if (headline) message.classList.add("sched-note-detail");
      item.append(el("span", "sched-note-time", when(note.at)), code, message);
      list.append(item);
    }
  }

  function renderStatus(status) {
    const enabled = !!status.enabled;
    $("sched-disabled").hidden = enabled;
    $("sched-facts").hidden = !enabled;
    const states = {
      running: "schedules.sched_running",
      stopped: "schedules.sched_stopped",
      stopping: "schedules.sched_stopping",
      unavailable: "schedules.sched_unavailable",
    };
    $("sched-state").textContent = enabled
      ? (states[status.state] ? tr(states[status.state]) : String(status.state))
      : tr("schedules.off");
    $("sched-breaker").textContent = breakerLabel(status.breaker_level);
    $("sched-last-tick").textContent = when(status.last_tick_at);
    $("sched-alerts").textContent = (status.alerts || []).map(alertText).join(", ") || tr("schedules.none");
    // ADR 0021 S2: the Agent layer (gateway agent_jobs) and its daily pool
    const agentLayer = $("sched-agent-layer");
    if (agentLayer) agentLayer.textContent = status.agent_jobs
      ? tr("schedules.agent_layer_on", { budget: Number(status.agent_daily_token_budget || 0).toLocaleString() })
      : tr("schedules.agent_layer_off");
  }

  let loaded = false;
  function failedRow(bodyId, columns) {
    const cell = el("td", "empty", tr("schedules.table_failed"));
    cell.colSpan = columns;
    const row = el("tr");
    row.append(cell);
    $(bodyId).replaceChildren(row);
  }

  // Resolves to whether the data differs from the last read, null when it could not be read.
  let lastKey = null;
  async function refresh() {
    try {
      const response = await fetch("/api/scheduler", { headers: { Accept: "application/json" } });
      if (!response.ok) throw new Error(String(response.status));
      const data = await response.json();
      jobNames = Object.fromEntries((data.jobs || []).map(job => [job.job_id, job.name || job.job_id]));
      renderStatus(data.status || {});
      renderJobs(data.jobs || []);
      renderRuns(data.runs || [], data.jobs || []);
      renderNotes(data.notifications || []);
      loaded = true;
      const key = JSON.stringify(data);
      const changed = key !== lastKey;
      lastKey = key;
      return changed;
    } catch (error) {
      $("sched-message").textContent = tr("schedules.load_failed");
      if (!loaded) {
        failedRow("sched-jobs", 7);
        failedRow("sched-runs", 6);
      }
      return null;
    }
  }

  // #369: not while the tab is hidden, slower while nothing changes, backed off on failure
  // (poll.js). A running action (busy) skips the poll. Without poll.js: a plain timer.
  if (typeof StiraPoll !== "undefined") {
    StiraPoll.keep(() => (busy ? true : refresh()), 5000, 20000, undefined, document);
  } else {
    void refresh();
    setInterval(() => { if (!document.hidden && !busy) void refresh(); }, 5000);
  }
})();
