(() => {
  "use strict";

  /*
   * SSE contract (JSON in data; ordinary messages or the named events below):
   * {
   *   id, session_id, timestamp: ISO8601 | epoch_ms,
   *   type: "model_call_start" | "model_call_end" | "tool_start" | "tool_end"
   *       | "pre_turn_start" | "pre_turn_end" | "post_turn_start" | "post_turn_end",
   *   turn_id, span_id, correlation_id, severity, model, tool, message,
   *   start_time, end_time, duration_ms, status, details,
   *   usage: {input_tokens, output_tokens, is_final: true},
   *   usage_id, estimated_cost_usd,
   *   pricing: {input_per_million, output_per_million}
   * }
   * A completed event can supply start_time + duration_ms without a start event.
   * Use unique span_id/call_id for concurrent calls, especially identical tools.
   * Final usage is cumulative PER CALL, not cumulative per session. Revisions
   * replace usage with the same usage_id/span_id, rather than adding it again.
   * Usage deltas must have distinct usage_ids. Partial usage must set is_final:false.
   * Event IDs must be stable for replay deduplication. Event data is never HTML.
   * Rates can instead be configured in observability-pricing.pricing[model].
   */
  const $ = id => document.getElementById(id);
  const MAX_ROWS = 200;
  const MAX_TURNS = 100;
  const MAX_SPANS = 300;
  const numbers = new Intl.NumberFormat("ja-JP");
  const money = new Intl.NumberFormat("en-US", {
    style: "currency", currency: "USD", minimumFractionDigits: 2
  });
  const severityLabels = {
    info: t("observability.info"), warning: t("shell.warning"), error: t("observability.error")
  };
  const categoryLabels = {
    model: t("observability.model_error"),
    tool: t("observability.tool_error_exception"),
    breaker: t("observability.breaker_intervention"),
    approval: t("observability.approval_request"),
    denied: t("observability.denied"),
    recovery: t("observability.retry_recovery")
  };
  const phaseLabels = {
    pre_turn: t("observability.pre_turn"),
    model_call: t("observability.model_call"),
    tool: t("observability.tool"),
    post_turn: t("observability.post_turn")
  };
  // The chat server's turns.db events (#286) name their own operation; tool_* use the phase label.
  const typeLabels = {
    turn_started: t("observability.op_turn_started"),
    turn_summary: t("observability.op_turn_summary"),
    turn_end: t("observability.op_turn_end"),
    approval_requested: t("observability.op_approval_requested"),
    approval_resolved: t("observability.op_approval_resolved"),
    breaker_transition: t("observability.op_breaker_transition")
  };
  let configuredRates = {};
  try {
    configuredRates = JSON.parse($("observability-pricing").textContent).pricing || {};
  } catch (_) {
    // Missing configuration means unpriced usage, never an assumed free model.
  }

  const state = {
    events: [], errors: [], turns: new Map(), currentTurn: null,
    selectedTurn: null, followTurn: true, filter: null,
    paused: false, pending: 0, invalid: 0, dirty: true,
    seen: new Set(), usage: new Map(), models: new Map(), turnUsage: new Map(),
    total: aggregate(), sequence: 0, openDetails: new Set()
  };

  function aggregate() {
    return { input: 0, output: 0, cost: 0, unknown: 0, calls: 0 };
  }
  function object(value) {
    return value && typeof value === "object" && !Array.isArray(value) ? value : {};
  }
  function text(value) {
    if (value === null || value === undefined) return "";
    if (typeof value === "object") {
      try { return JSON.stringify(value); } catch (_) { return ""; }
    }
    return String(value);
  }
  // #455: a breaker transition with its reasons reads "WARNING: 安定度 0.31 < 0.35"
  // (codes from the server, worded by breaker_reasons.js; never the code itself).
  function breakerMessage(data) {
    const words = window.StiraBreakerReasons;
    if (!words || data.type !== "breaker_transition" || !Array.isArray(data.reasons)) return "";
    const line = words.line(data.to_level, data.reasons, object(data.inputs), t);
    return line ? `${text(data.message)} · ${line}` : "";
  }
  function numeric(value) {
    if (value === null || value === undefined || value === "") return null;
    const n = Number(value);
    return Number.isFinite(n) && n >= 0 ? n : null;
  }
  function timestamp(value) {
    if (value === null || value === undefined || value === "") return null;
    if (typeof value === "number") {
      const ms = value < 100000000000 ? value * 1000 : value;
      return Number.isFinite(ms) && Math.abs(ms) <= 8640000000000000 ? ms : null;
    }
    const ms = Date.parse(value);
    return Number.isFinite(ms) ? ms : null;
  }
  function node(tag, className, content) {
    const el = document.createElement(tag);
    if (className) el.className = className;
    if (content !== undefined) el.textContent = content;
    return el;
  }
  function cell(row, content, className) {
    const el = node("td", className, content);
    row.append(el);
    return el;
  }
  function emptyRow(columns, label) {
    const tr = node("tr");
    const td = cell(tr, label, "empty");
    td.colSpan = columns;
    return tr;
  }
  function clock(ms) {
    return new Date(ms).toLocaleTimeString("ja-JP", { hour12: false }) +
      "." + String(new Date(ms).getMilliseconds()).padStart(3, "0");
  }
  function seconds(ms) {
    return t("observability.seconds", { n: (Math.max(0, ms) / 1000).toFixed(2) });
  }
  function tokenText(input, output) {
    return t("observability.tokens_in_out", {
      input: numbers.format(input), output: numbers.format(output)
    });
  }

  function normalize(raw, message) {
    const outer = object(raw);
    const data = outer.data && typeof outer.data === "object"
      ? { ...outer, ...object(outer.data) } : outer;
    const type = text(data.type || data.event_type || data.event ||
      (message.type === "message" ? "event" : message.type)).toLowerCase().replace(/[.\s-]/g, "_");
    const status = text(data.status).toLowerCase();
    const usage = object(data.usage || data.token_usage || data.tokens);
    let phase = text(data.phase).toLowerCase();
    if (!phase) {
      if (/pre_turn/.test(type)) phase = "pre_turn";
      else if (/post_turn/.test(type)) phase = "post_turn";
      else if (/model|llm/.test(type)) phase = "model_call";
      else if (/tool/.test(type)) phase = "tool";
    }
    if (phase === "model" || phase === "llm") phase = "model_call";
    const duration = numeric(data.duration_ms ?? data.latency_ms);
    const start = timestamp(data.start_time ?? data.started_at);
    const end = timestamp(data.end_time ?? data.ended_at);
    const at = timestamp(data.timestamp ?? data.time) ?? end ?? start ?? Date.now();
    const isStart = /(?:^|_)(start|started|begin)$/.test(type) || /^(start|started|running)$/.test(status);
    const isEnd = /(?:^|_)(end|ended|complete|completed|finish|finished|error|exception|failed|failure)$/.test(type) ||
      /^(end|completed|success|error|failed|failure|denied)$/.test(status) || duration !== null || end !== null;
    let severity = text(data.severity || data.level || "info").toLowerCase();
    if (severity === "warn") severity = "warning";
    if (severity === "fatal" || /error|exception|failed|failure/.test(`${type} ${status}`)) severity = "error";
    if (!severityLabels[severity]) severity = "info";
    const tool = text(data.tool ?? data.tool_name);
    const model = text(data.model ?? data.model_id ?? data.model_name);
    const categorySource = `${type} ${text(data.category)} ${status} ${text(data.action)}`.toLowerCase();
    let category = null;
    if (type === "approval_resolved" && /^(deny|reject)/.test(text(data.decision).toLowerCase())) {
      // A refusal (a person's or the system's), not a breaker transition.
      category = "denied";
      if (severity === "info") severity = "warning";
    } else if (/^approval_/.test(type)) {
      category = "approval";
    } else if (status === "denied") {
      category = "denied";
    } else if (/breaker|intervention|(?:^|[\s_])(hold|block|blocked)(?:$|[\s_])/.test(categorySource)) {
      category = "breaker";
      if (severity === "info") severity = "warning";
    } else if (/retry|recover/.test(categorySource)) category = "recovery";
    else if (severity === "error") category = tool || phase === "tool" ? "tool" : "model";

    const turn = data.turn_id ?? data.turn;
    return {
      key: ++state.sequence,
      id: text(data.id ?? data.event_id ?? message.lastEventId),
      session: text(data.session_id),
      turn: turn === undefined || turn === null ? null : text(turn),
      correlation: text(data.correlation_id ?? data.trace_id),
      span: text(data.span_id ?? data.call_id),
      usageId: text(data.usage_id ?? usage.id),
      at, start, end, duration, type, status, phase, isStart, isEnd,
      severity, category, tool, model,
      operation: text(data.operation || typeLabels[type] || phaseLabels[phase] || t("observability.event")),
      message: breakerMessage(data) || text(data.message ?? data.summary ?? data.error),
      input: numeric(usage.input_tokens ?? usage.input ?? usage.prompt_tokens ?? data.input_tokens),
      output: numeric(usage.output_tokens ?? usage.output ?? usage.completion_tokens ?? data.output_tokens),
      finalUsage: usage.is_final !== false && data.usage_final !== false && !isStart,
      cost: numeric(data.estimated_cost_usd ?? data.cost_usd ?? usage.estimated_cost_usd),
      pricing: object(data.pricing),
      raw: data
    };
  }

  // A turn id from turns.db is a 32-digit hex uuid: show its first 8 digits (full id in the title).
  function shortId(id) {
    return /^[0-9a-f]{16,}$/i.test(id) ? id.slice(0, 8) : id;
  }
  function turnKey(event) {
    return JSON.stringify([event.session, event.turn]);
  }
  function updateCosts(event) {
    if (!event.finalUsage || (event.input === null && event.output === null && event.cost === null)) return;
    const identity = event.usageId || event.span || event.id || `local:${event.key}`;
    const key = JSON.stringify([event.session, event.turn, identity]);
    const old = state.usage.get(key);
    const model = event.model || old?.model || t("base.unknown");
    const input = event.input ?? old?.input ?? 0;
    const output = event.output ?? old?.output ?? 0;
    const rates = Object.keys(event.pricing).length ? event.pricing : object(configuredRates[model]);
    const inputRate = numeric(rates.input_per_million);
    const outputRate = numeric(rates.output_per_million);
    const calculated = inputRate !== null && outputRate !== null
      ? (input * inputRate + output * outputRate) / 1000000 : null;
    const cost = event.cost ?? calculated ??
      (old && input === old.input && output === old.output ? old.cost : null);
    const entry = {
      model, turn: event.turn === null ? null : turnKey(event),
      input, output, cost: Number.isFinite(cost) ? cost : null
    };
    function apply(item, sign) {
      if (!state.models.has(item.model)) state.models.set(item.model, aggregate());
      const destinations = [state.total, state.models.get(item.model)];
      if (item.turn !== null) {
        if (!state.turnUsage.has(item.turn)) state.turnUsage.set(item.turn, aggregate());
        destinations.push(state.turnUsage.get(item.turn));
      }
      for (const total of destinations) {
        total.input += sign * item.input;
        total.output += sign * item.output;
        total.cost += sign * (item.cost ?? 0);
        total.unknown += sign * (item.cost === null ? 1 : 0);
        total.calls += sign;
      }
    }
    if (old) apply(old, -1);
    apply(entry, 1);
    state.usage.set(key, entry);
  }

  function updateTimeline(event) {
    if (event.turn === null) return;
    const key = turnKey(event);
    let turn = state.turns.get(key);
    if (!turn) {
      turn = {
        key, id: event.turn, session: event.session, start: event.start ?? event.at,
        end: null, last: event.at, spans: [], dropped: 0
      };
      state.turns.set(key, turn);
      state.currentTurn = key;
      if (state.followTurn) state.selectedTurn = key;
      if (state.turns.size > MAX_TURNS) {
        const oldest = state.turns.keys().next().value;
        state.turns.delete(oldest);
        if (state.selectedTurn === oldest) {
          state.followTurn = true;
          state.selectedTurn = state.currentTurn;
        }
      }
    }
    turn.last = Math.max(turn.last, event.end ?? event.at);
    turn.start = Math.min(turn.start, event.start ?? event.at);
    if (/^turn_(end|ended|complete|completed)$/.test(event.type) ||
        (event.phase === "post_turn" && event.isEnd)) {
      turn.end = event.end ?? event.at;
    }
    if (!phaseLabels[event.phase] || (!event.isStart && !event.isEnd)) return;
    let span = event.span
      ? turn.spans.find(s => s.id === event.span)
      : (!event.isStart ? [...turn.spans].reverse().find(s =>
        s.end === null && s.phase === event.phase &&
        s.target === (event.tool || event.model)) : null);
    if (!span) {
      const finish = event.end ?? event.at;
      span = {
        id: event.span || `local:${event.key}`, phase: event.phase,
        target: event.tool || event.model,
        start: event.start ?? (event.isEnd && event.duration !== null ? finish - event.duration : event.at),
        end: null, error: false
      };
      turn.spans.push(span);
      if (turn.spans.length > MAX_SPANS) {
        turn.spans.shift();
        turn.dropped++;
      }
    } else if (event.start !== null || event.isStart) {
      span.start = Math.min(span.start, event.start ?? event.at);
    }
    if (event.isEnd) {
      span.end = Math.max(span.start, event.end ??
        (event.duration !== null ? span.start + event.duration : event.at));
      if (event.duration === null) event.duration = span.end - span.start;
    }
    span.error = span.error || event.severity === "error";
    turn.start = Math.min(turn.start, span.start);
    turn.last = Math.max(turn.last, span.end ?? event.at);
  }

  function ingest(message) {
    try {
      const raw = JSON.parse(message.data);
      if (!raw || typeof raw !== "object" || Array.isArray(raw)) throw new Error("Invalid event");
      const event = normalize(raw, message);
      if (/^(heartbeat|ping)$/.test(event.type)) return;
      if (event.id) {
        const id = JSON.stringify([event.session, event.id]);
        if (state.seen.has(id)) return;
        state.seen.add(id);
        // Retain replay IDs for this page's session.
      }
      updateTimeline(event);
      updateCosts(event);
      state.events.push(event);
      if (state.events.length > MAX_ROWS) state.events.shift();
      if (event.category) {
        state.errors.push(event);
        if (state.errors.length > MAX_ROWS) {
          state.openDetails.delete(state.errors.shift().key);
        }
      }
      state.dirty = true;
      if (state.paused) {
        state.pending++;
        updatePauseNotice();
      }
    } catch (_) {
      state.invalid++;
      updateConnection();
    }
  }

  function relatedFilter(event) {
    if (event.turn !== null) {
      return { kind: "turn", value: event.turn, session: event.session };
    }
    if (event.correlation) {
      return { kind: "correlation", value: event.correlation, session: event.session };
    }
    return null;
  }
  function matchesFilter(event) {
    const f = state.filter;
    return !f || (event.session === f.session &&
      (f.kind === "turn" ? event.turn : event.correlation) === f.value);
  }
  function selectRelated(event) {
    state.filter = relatedFilter(event);
    renderLog();
  }
  function renderLog() {
    const scroll = $("log-scroll");
    const previousScroll = scroll.scrollTop;
    const fragment = document.createDocumentFragment();
    const events = state.events.filter(matchesFilter);
    for (const event of events) {
      const tr = node("tr", `severity-${event.severity}`);
      if (relatedFilter(event)) {
        tr.tabIndex = 0;
        tr.classList.add("related-row");
        tr.setAttribute("aria-label", t("observability.show_related_events"));
        tr.addEventListener("click", () => selectRelated(event));
        tr.addEventListener("keydown", e => {
          if (e.key === "Enter" || e.key === " ") {
            e.preventDefault();
            selectRelated(event);
          }
        });
      }
      const timeCell = cell(tr, clock(event.at), "nowrap");
      timeCell.title = new Date(event.at).toISOString();
      const turnCell = cell(tr, event.turn === null ? "—" : shortId(event.turn), "nowrap");
      if (event.turn !== null) turnCell.title = event.turn;
      cell(tr, severityLabels[event.severity], "severity-label nowrap");
      const operationCell = cell(tr, event.operation);
      operationCell.append(node("small", "event-type", event.type));
      cell(tr, [event.model, event.tool].filter(Boolean).join(" / ") || "—", "break-word");
      cell(tr, event.duration === null ? "—" : seconds(event.duration), "nowrap");
      cell(tr, event.input === null && event.output === null
        ? "—" : tokenText(event.input ?? 0, event.output ?? 0), "nowrap");
      cell(tr, event.message || "—", "message-cell");
      fragment.append(tr);
    }
    if (!events.length) fragment.append(emptyRow(8, t("observability.no_matching_events")));
    $("log-body").replaceChildren(fragment);
    const filter = state.filter;
    $("log-filter").textContent = filter
      ? `${filter.kind === "turn" ? t("observability.turn") : t("observability.correlation_id")}: ${filter.value}`
      : t("observability.all_events");
    $("clear-filter").hidden = !filter;
    $("log-count").textContent = t("observability.log_count", {
      shown: events.length, total: state.events.length
    });
    scroll.scrollTop = $("follow-latest").checked ? scroll.scrollHeight : previousScroll;
  }

  function renderHistory() {
    const fragment = document.createDocumentFragment();
    for (const turn of [...state.turns.values()].reverse()) {
      const button = node("button", "turn-button", t("observability.turn_n", { id: shortId(turn.id) }));
      button.type = "button";
      button.setAttribute("aria-pressed", String(state.selectedTurn === turn.key));
      button.title = turn.session
        ? t("observability.session_id", { session: turn.session }) : turn.id;
      button.addEventListener("click", () => {
        state.selectedTurn = turn.key;
        state.followTurn = false;
        renderHistory();
        renderTimeline();
      });
      fragment.append(button);
    }
    $("turn-history").replaceChildren(fragment);
  }

  function renderTimeline() {
    const turn = state.turns.get(state.selectedTurn);
    if (!turn) return;
    $("timeline-title").textContent = t("observability.turn_n", { id: shortId(turn.id) });
    const now = Date.now();
    const hasOpen = turn.spans.some(span => span.end === null);
    const end = Math.max(turn.start + 1, turn.last, turn.end ?? turn.start,
      hasOpen && turn.end === null ? now : turn.start);
    const range = end - turn.start;
    $("timeline-duration").textContent = seconds(range) +
      (hasOpen && turn.end === null ? " · " + t("observability.running") : "");
    const fragment = document.createDocumentFragment();
    const axis = node("div", "timeline-row timeline-axis");
    axis.append(node("span", "", t("observability.elapsed")));
    const ticks = node("div", "axis-ticks");
    for (const fraction of [0, 0.5, 1]) ticks.append(node("span", "", seconds(range * fraction)));
    axis.append(ticks);
    fragment.append(axis);
    for (const span of [...turn.spans].sort((a, b) => a.start - b.start)) {
      const row = node("div", "timeline-row");
      const label = `${phaseLabels[span.phase]}${span.target ? ` · ${span.target}` : ""}`;
      row.append(node("div", "lane-label", label));
      const track = node("div", "lane-track");
      const stop = Math.max(span.start, span.end ?? turn.end ?? end);
      const left = Math.max(0, Math.min(100, (span.start - turn.start) / range * 100));
      const width = Math.max(0, Math.min(100 - left, (stop - span.start) / range * 100));
      const kind = span.error ? "error" : span.phase === "tool" ? "tool" :
        span.phase === "model_call" ? "model" : "phase";
      const bar = node("div", `timeline-bar ${kind}${span.end === null ? " unfinished" : ""}`);
      bar.style.left = `${left}%`;
      bar.style.width = `${width}%`;
      const description = `${label} · ${seconds(stop - span.start)}` +
        (span.error ? " · " + t("observability.error") : "") +
        (span.end === null ? " · " + t("observability.end_not_received") : "");
      bar.title = description;
      bar.tabIndex = 0;
      bar.setAttribute("role", "img");
      bar.setAttribute("aria-label", description);
      bar.append(node("span", "", seconds(stop - span.start)));
      track.append(bar);
      row.append(track);
      fragment.append(row);
    }
    if (!turn.spans.length) fragment.append(node("p", "empty", t("observability.no_phase_data")));
    if (turn.dropped) fragment.append(node("p", "hint",
      t("observability.dropped_older_lanes_omitted", { dropped: turn.dropped })));
    $("timeline").replaceChildren(fragment);
  }

  function costLabel(total) {
    return money.format(Math.max(0, total.cost)) + (total.unknown > 0 ? " *" : "");
  }
  function renderCosts() {
    const turn = state.turns.get(state.currentTurn);
    const current = state.turnUsage.get(state.currentTurn) || aggregate();
    $("cost-turn").textContent = turn ? t("observability.turn_n", { id: shortId(turn.id) }) : "—";
    $("current-tokens").textContent = tokenText(current.input, current.output);
    $("session-tokens").textContent = tokenText(state.total.input, state.total.output);
    $("estimated-cost").textContent = costLabel(state.total);
    const fragment = document.createDocumentFragment();
    for (const [model, total] of [...state.models.entries()].sort(([a], [b]) => a.localeCompare(b))) {
      if (!total.calls) continue;
      const tr = node("tr");
      cell(tr, model, "break-word");
      cell(tr, numbers.format(total.input), "numeric");
      cell(tr, numbers.format(total.output), "numeric");
      cell(tr, costLabel(total), "numeric");
      fragment.append(tr);
    }
    if (!fragment.childNodes.length) fragment.append(emptyRow(4, t("observability.no_usage_yet")));
    $("model-cost-body").replaceChildren(fragment);
    $("pricing-notice").textContent = state.total.unknown
      ? t("observability.subtotal_excludes_unpriced", { unknown: state.total.unknown })
      : t("observability.calculated_in_usd_from_reported_costs_or");
  }

  function renderErrors() {
    const category = $("error-category").value;
    const fragment = document.createDocumentFragment();
    for (const event of [...state.errors].reverse()) {
      if (category !== "all" && event.category !== category) continue;
      const tr = node("tr", `category-${event.category}`);
      const timeCell = cell(tr, clock(event.at), "nowrap");
      timeCell.title = new Date(event.at).toISOString();
      cell(tr, categoryLabels[event.category]);
      const turnCell = cell(tr, event.turn === null ? "—" : shortId(event.turn), "nowrap");
      if (event.turn !== null) turnCell.title = event.turn;
      cell(tr, event.tool || event.model || "—", "break-word");
      const content = cell(tr, "");
      content.append(node("div", "error-summary", event.message || event.operation));
      const details = node("details");
      details.open = state.openDetails.has(event.key);
      details.append(node("summary", "", t("observability.show_hide_details")));
      details.append(node("pre", "", JSON.stringify(event.raw, null, 2)));
      details.addEventListener("toggle", () => {
        if (!details.isConnected) return;
        if (details.open) state.openDetails.add(event.key);
        else state.openDetails.delete(event.key);
      });
      content.append(details);
      fragment.append(tr);
    }
    if (!fragment.childNodes.length) fragment.append(emptyRow(5, t("observability.no_errors_or_interventions")));
    $("error-body").replaceChildren(fragment);
  }

  function renderAll() {
    renderLog();
    renderHistory();
    renderTimeline();
    renderCosts();
    renderErrors();
    state.dirty = false;
  }
  function updatePauseNotice() {
    $("pause-notice").hidden = !state.paused;
    $("pause-notice").textContent = t("observability.display_paused", { pending: state.pending });
  }
  $("pause-button").addEventListener("click", () => {
    state.paused = !state.paused;
    $("pause-button").textContent = state.paused ? t("observability.resume") : t("observability.pause");
    $("pause-button").setAttribute("aria-pressed", String(state.paused));
    if (!state.paused) {
      state.pending = 0;
      renderAll();
    }
    updatePauseNotice();
  });
  $("clear-filter").addEventListener("click", () => {
    state.filter = null;
    renderLog();
  });
  $("follow-latest").addEventListener("change", () => {
    if ($("follow-latest").checked) $("log-scroll").scrollTop = $("log-scroll").scrollHeight;
  });
  $("latest-turn").addEventListener("click", () => {
    state.followTurn = true;
    state.selectedTurn = state.currentTurn;
    renderHistory();
    renderTimeline();
  });
  $("error-category").addEventListener("change", renderErrors);

  let source = null;
  let connectionState = "connecting";
  function updateConnection() {
    const labels = {
      connecting: t("observability.connecting"),
      connected: t("observability.connected"),
      reconnecting: t("observability.reconnecting"),
      closed: t("observability.closed_reload_to_reconnect"),
      unsupported: t("observability.browser_does_not_support_sse"),
      absent: t("observability.no_stream")
    };
    $("connection-dot").className = `connection-dot ${connectionState}`;
    $("connection-text").textContent = labels[connectionState] +
      (state.invalid ? " · " + t("observability.invalid_ignored", { invalid: state.invalid }) : "");
  }
  // #278: a server without the stream says so (data-event-source="absent"); the page
  // then never connects and never asks for a reload that cannot help.
  const eventSource = $("connection")?.dataset.eventSource || "live";
  function connect() {
    if (eventSource === "absent") {
      connectionState = "absent";
      updateConnection();
      return;
    }
    if (!("EventSource" in window)) {
      connectionState = "unsupported";
      updateConnection();
      return;
    }
    source = new EventSource("/api/observability/events");
    source.onopen = () => {
      connectionState = "connected";
      updateConnection();
    };
    source.onmessage = ingest;
    const namedEvents = [
      "observability", "strands", "event",
      "turn_start", "turn_end", "pre_turn", "pre_turn_start", "pre_turn_end",
      "post_turn", "post_turn_start", "post_turn_end",
      "model_call", "model_call_start", "model_call_end", "model_error",
      "tool_call", "tool_call_start", "tool_call_end", "tool_start", "tool_end",
      "tool_error", "tool_exception", "breaker", "breaker_hold", "breaker_block",
      "breaker_intervention", "retry", "recovery", "usage", "token_usage", "error"
    ];
    for (const name of namedEvents) {
      source.addEventListener(name, event => {
        // A transport error is an Event, not a MessageEvent with JSON data.
        if (typeof event.data === "string") ingest(event);
      });
    }
    source.onerror = event => {
      if (typeof event.data === "string") return;
      connectionState = source.readyState === EventSource.CLOSED ? "closed" : "reconnecting";
      updateConnection();
      // EventSource handles retry and Last-Event-ID automatically.
    };
  }

  const renderTimer = window.setInterval(() => {
    if (!state.paused && state.dirty) renderAll();
  }, 150);
  const timelineTimer = window.setInterval(() => {
    if (!state.paused && state.turns.get(state.selectedTurn)?.spans.some(s => s.end === null)) {
      renderTimeline();
    }
  }, 1000);
  window.addEventListener("pagehide", () => source?.close());
  window.addEventListener("pageshow", event => {
    if (event.persisted) {
      source?.close();
      connectionState = "connecting";
      updateConnection();
      connect();
    }
  });
  // Timer IDs are retained for embedders that adapt this standalone page.
  void renderTimer;
  void timelineTimer;
  renderAll();
  connect();
})();
