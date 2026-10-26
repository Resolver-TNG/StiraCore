/* Turn ribbon + Turn Inspector drawer (#187).
 * Loaded before chat.js. chat.js calls StiraTurns.create() and feeds it:
 *   attach(reply, turnId, summary, opts)  - add/refresh the ribbon on an agent bubble
 *   ingest(event, reply)                  - live `ui_event` SSE payloads (#186 contract)
 *   restore()                             - GET /api/turns to rehydrate after reload
 *   setCurrent(state, level)              - live gateway values for the "now" column
 * Missing values render as "not recorded" and are never filled with 0.
 */
(function (root) {
  "use strict";
  const DIMS = ["trust", "curiosity", "vigilance", "attachment", "stability", "satisfaction"];
  // #310: coherence uses the #284 word ("shell.coherence"), same as chat.js and the nav chip.
  const GLYPH = { normal: "🟢", warning: "🟡", coherence: "🟣", critical: "🔴" };
  const LEVEL_KEY = {
    normal: "shell.normal", warning: "shell.warning", coherence: "shell.coherence", critical: "shell.critical",
  };
  const finite = value => typeof value === "number" && Number.isFinite(value);
  const isObject = value => value !== null && typeof value === "object" && !Array.isArray(value);
  const text = value => (typeof value === "string" && value.trim() ? value.trim() : null);

  function signed(value) {
    if (value === 0) return "0";
    const abs = Math.abs(value);
    let out = abs.toFixed(3).replace(/0$/, "");
    if (out === "0.00") out = abs.toPrecision(1);
    return `${value > 0 ? "+" : "−"}${out}`;
  }
  const fixed = (value, digits = 2) => (finite(value) ? value.toFixed(digits) : null);

  function levelOf(value) {
    const raw = isObject(value) ? value.level ?? value.value : value;
    const level = typeof raw === "string" ? raw.toLowerCase() : null;
    return Object.hasOwn(GLYPH, level) ? level : null;
  }

  /** Normalize turn.summary / recent_turns summary / legacy history turn_summary. */
  function normalizeSummary(raw) {
    if (!isObject(raw)) return null;
    const snapshot = value => (isObject(value)
      ? Object.fromEntries(DIMS.map(dim => [dim, finite(value[dim]) ? value[dim] : null]))
      : null);
    return {
      category: text(raw.category),
      intensity: finite(raw.intensity) ? raw.intensity : null,
      confidence: finite(raw.confidence) ? raw.confidence : null,
      classifier: text(raw.classifier),
      rationale: text(raw.emotion_transcription),
      deltas: isObject(raw.deltas) ? raw.deltas : null,
      breaker: levelOf(raw.breaker),
      before: snapshot(raw.before),
      after: snapshot(raw.after),
    };
  }

  /** Per-dimension delta: {value, trend} with trend up|down|flat|missing. */
  function deltaOf(summary, dim) {
    if (!summary || !summary.deltas) return { value: null, trend: "missing" };
    if (!Object.hasOwn(summary.deltas, dim)) return { value: 0, trend: "flat" };
    const value = summary.deltas[dim];
    if (!finite(value)) return { value: null, trend: "missing" };
    return { value, trend: value > 0 ? "up" : value < 0 ? "down" : "flat" };
  }

  function create(options = {}) {
    const doc = options.document || root.document;
    const $ = id => doc.getElementById(id);
    const fetchJSON = options.fetchJSON || (async url => {
      const response = await fetch(url, { cache: "no-store" });
      if (!response.ok) throw new Error(String(response.status));
      return response.json();
    });
    const records = new Map();   // turn_id -> record
    const ribbons = new Map();   // turn_id -> ribbon button
    const local = new WeakMap(); // ribbon -> record for turns without an id
    let latestId = null, openRecord = null, trigger = null, view = "pretty";
    const current = { state: null, level: null };
    // #188: tool cards inside the reply (tool_cards.js); optional.
    const cards = typeof root.StiraCards !== "undefined"
      ? root.StiraCards.create({ document: doc, fetchJSON }) : null;
    const renderCards = rec => { if (cards && rec.reply) cards.renderTurn(rec, rec.reply); };

    const el = (tag, className, content) => {
      const node = doc.createElement(tag);
      if (className) node.className = className;
      if (content !== undefined && content !== null) node.textContent = content;
      return node;
    };
    const dimLabel = dim => t(`chat.${dim}`);
    // #455: " — 安定度 0.31 < 0.35" after a level change, when the event says why.
    const reasonSuffix = (transition) => {
      const words = globalThis.StiraBreakerReasons;
      const why = words ? words.describe(transition.to, transition.reasons, transition.inputs, t) : "";
      return why ? ` — ${why}` : "";
    };
    const missing = () => t("turns.not_recorded");

    function record(turnId) {
      if (!turnId) return { id: null, events: [], summary: null, tools: new Map(), approvals: [],
        transitions: [], usage: null, startedAt: null, endedAt: null, completed: false };
      if (!records.has(turnId)) {
        records.set(turnId, { id: turnId, events: [], summary: null, tools: new Map(),
          approvals: [], transitions: [], usage: null, startedAt: null, endedAt: null,
          completed: false, eventsLoaded: false });
      }
      return records.get(turnId);
    }

    function applyEvent(rec, event) {
      if (!isObject(event) || !isObject(event.data)) return;
      if (rec.events.some(known => known.seq === event.seq)) return;
      // #601 E12: the history read and the live stream can deliver an older event after a
      // newer one; the tools / transitions / approvals are then folded again in seq order
      // (a late "created: running" must not undo an "updated: done").
      const late = finite(event.seq) && rec.events.some(known => finite(known.seq) && known.seq > event.seq);
      rec.events.push(event);
      rec.events.sort((a, b) => a.seq - b.seq);
      applyScalar(rec, event);
      if (!late) { applyList(rec, event); return; }
      rec.tools.clear();
      rec.transitions.length = 0;
      rec.approvals.length = 0;
      for (const known of rec.events) applyList(rec, known);
    }

    // One per turn (start, summary, end): the order of arrival does not matter.
    function applyScalar(rec, event) {
      const data = event.data;
      switch (event.type) {
        case "turn.started": rec.startedAt = event.ts; break;
        case "turn.summary": rec.summary = normalizeSummary(data); break;
        case "turn.completed":
          rec.completed = true;
          rec.endedAt = event.ts;
          rec.usage = isObject(data.usage) ? data.usage : null;
          rec.error = data.error === true;
          break;
        default: break;
      }
    }

    // Folded in order: later events refine earlier ones.
    function applyList(rec, event) {
      const data = event.data;
      switch (event.type) {
        case "breaker.transition": rec.transitions.push({
          from: levelOf(data.from), to: levelOf(data.to),
          // #455: why (codes and numbers; worded by breaker_reasons.js, never shown raw)
          reasons: Array.isArray(data.reasons) ? data.reasons : [],
          inputs: isObject(data.inputs) ? data.inputs : {},
        }); break;
        case "tool_call.created":
        case "tool_call.updated": {
          const key = data.tool_use_id || `${data.name}#${event.seq}`;
          const merged = { ...(rec.tools.get(key) || {}) };
          // tool_call.updated repeats input_preview: null; keep what created recorded.
          for (const [name, value] of Object.entries(data)) if (value !== null && value !== undefined) merged[name] = value;
          rec.tools.set(key, merged);
          break;
        }
        case "approval.requested":
        case "approval.resolved": {
          const known = rec.approvals.find(item => item.request_id === data.request_id);
          if (known) Object.assign(known, data); else rec.approvals.push({ ...data });
          break;
        }
        default: break;
      }
    }

    function stimulusText(rec) {
      const summary = rec.summary;
      if (!summary) return rec.completed || rec.restored ? missing() : t("turns.recording");
      if (summary.category) return `⚡ ${summary.category}`;
      return summary.deltas ? t("turns.no_update") : missing();
    }

    function breakerText(level) {
      return level ? `${GLYPH[level]} ${t(LEVEL_KEY[level])}` : `⚪ ${missing()}`;
    }

    function renderRibbon(ribbon, rec) {
      const summary = rec.summary;
      ribbon.replaceChildren();
      ribbon.dataset.state = summary ? "recorded" : "missing";
      const stimulus = el("span", "ribbon-stimulus", stimulusText(rec));
      const confidenceValue = summary ? fixed(summary.confidence) : null;
      const confidence = el("span", "ribbon-confidence",
        t("turns.confidence_value", { value: confidenceValue ?? missing() }));
      if (confidenceValue === null) confidence.dataset.trend = "missing";
      const breaker = el("span", "ribbon-breaker", breakerText(summary?.breaker ?? null));
      breaker.dataset.level = summary?.breaker ?? "unknown";
      const dims = el("span", "ribbon-dims");
      const spoken = [];
      for (const dim of DIMS) {
        const { value, trend } = deltaOf(summary, dim);
        const glyph = { up: "▲", down: "▼", flat: "・", missing: "?" }[trend];
        const shown = value === null ? missing() : signed(value);
        const cell = el("span", "ribbon-dim", `${dimLabel(dim)} ${glyph}${shown}`);
        cell.dataset.trend = trend;
        cell.dataset.dim = dim;
        dims.append(cell);
        spoken.push(`${dimLabel(dim)} ${trend === "flat" ? t("turns.no_change") : shown}`);
      }
      ribbon.append(stimulus, confidence, breaker, dims);
      if (summary?.rationale) ribbon.append(el("span", "ribbon-rationale", summary.rationale));
      ribbon.setAttribute("aria-label", t("turns.ribbon_label", {
        stimulus: stimulusText(rec).replace(/^⚡ /, ""),
        confidence: confidenceValue ?? missing(),
        breaker: summary?.breaker ? t(LEVEL_KEY[summary.breaker]) : missing(),
        deltas: spoken.join(", "),
      }));
    }

    function ribbonFor(reply) {
      if (reply.ribbon) return reply.ribbon;
      const ribbon = el("button", "turn-ribbon");
      ribbon.type = "button";
      ribbon.setAttribute("aria-haspopup", "dialog");
      ribbon.setAttribute("aria-controls", "turn-inspector");
      ribbon.setAttribute("aria-expanded", "false");
      ribbon.addEventListener("click", () => void open(ribbon.dataset.turnId || null, ribbon));
      if (reply.turnSummary && reply.article.insertBefore) reply.article.insertBefore(ribbon, reply.turnSummary);
      else reply.article.append(ribbon);
      reply.article.classList.add("has-ribbon");
      reply.ribbon = ribbon;
      return ribbon;
    }

    function attach(reply, turnId, rawSummary, opts = {}) {
      if (!reply?.article) return null;
      const rec = turnId ? record(turnId) : (reply.ribbon && local.get(reply.ribbon)) || record(null);
      if (!rec.summary && rawSummary) rec.summary = normalizeSummary(rawSummary);
      if (opts.past) rec.restored = true;
      if (opts.completed) rec.completed = true;
      const ribbon = ribbonFor(reply);
      rec.reply = reply;
      if (turnId) {
        ribbon.dataset.turnId = turnId;
        ribbons.set(turnId, ribbon);
        latestId = turnId;
      } else {
        local.set(ribbon, rec);
      }
      renderRibbon(ribbon, rec);
      renderCards(rec);
      if (openRecord === rec) renderInspector();
      return ribbon;
    }

    function ingest(event, reply) {
      if (!isObject(event) || typeof event.turn_id !== "string") return;
      const rec = record(event.turn_id);
      applyEvent(rec, event);
      rec.eventsLoaded = true;
      if (reply) attach(reply, event.turn_id, null);
      else if (ribbons.has(event.turn_id)) { renderRibbon(ribbons.get(event.turn_id), rec); renderCards(rec); }
      if (openRecord === rec) renderInspector();
    }

    async function restore(limit = 200) {
      let list = [];
      try {
        const data = await fetchJSON(`/api/turns?limit=${limit}`);
        list = Array.isArray(data?.turns) ? data.turns : [];
      } catch { list = []; }
      for (const item of list) {
        if (!isObject(item) || typeof item.turn_id !== "string") continue;
        const rec = record(item.turn_id);
        rec.restored = true;
        if (item.summary) rec.summary = normalizeSummary(item.summary) || rec.summary;
        if (finite(item.started_at)) rec.startedAt = item.started_at;
        if (finite(item.ended_at)) rec.endedAt = item.ended_at;
        if (item.completed) rec.completed = true;
        if (isObject(item.usage)) rec.usage = item.usage;
        if (ribbons.has(item.turn_id)) renderRibbon(ribbons.get(item.turn_id), rec);
      }
      // #188: past tool calls need their events; fetch only turns that had tools.
      for (const item of list) {
        const rec = records.get(item?.turn_id);
        if (!rec || !rec.reply || rec.eventsLoaded || !(item.tool_count > 0)) continue;
        await loadEvents(rec);
        renderCards(rec);
      }
      if (!latestId && typeof list[0]?.turn_id === "string") latestId = list[0].turn_id;
      const wanted = selectedFromURL();
      if (wanted) await open(wanted, null);
      return list.length;
    }

    function setCurrent(state, level) {
      current.state = isObject(state) ? state : null;
      current.level = levelOf(level);
      if (openRecord) renderInspector();
    }

    // ---------- URL (selected turn) ----------
    function selectedFromURL() {
      try {
        const id = new URLSearchParams(root.location.search).get("turn");
        return id && /^[\w-]{1,64}$/.test(id) ? id : null;
      } catch { return null; }
    }
    function writeURL(turnId) {
      try {
        const url = new URL(root.location.href);
        if (turnId) url.searchParams.set("turn", turnId); else url.searchParams.delete("turn");
        root.history.replaceState(root.history.state, "", url.pathname + url.search + url.hash);
      } catch { /* no history API (tests, file://) */ }
    }

    // ---------- Inspector ----------
    async function open(turnId, from) {
      const drawer = $("turn-inspector");
      if (!drawer) return;
      const ribbon = from || null;
      const rec = turnId ? record(turnId) : (ribbon && local.get(ribbon)) || record(null);
      if (trigger && trigger !== ribbon) trigger.setAttribute?.("aria-expanded", "false");
      trigger = ribbon || trigger;
      openRecord = rec;
      drawer.hidden = false;
      ribbon?.setAttribute("aria-expanded", "true");
      writeURL(rec.id);
      renderInspector();
      $("inspector-close")?.focus();
      if (rec.id && !rec.eventsLoaded) {
        await loadEvents(rec);
        renderCards(rec);
        if (openRecord === rec) renderInspector();
      }
    }

    async function loadEvents(rec) {
      try {
        const data = await fetchJSON(`/api/turns/${encodeURIComponent(rec.id)}/events?after_seq=0`);
        for (const event of Array.isArray(data?.events) ? data.events : []) applyEvent(rec, event);
        rec.eventsLoaded = true;
        rec.loadError = false;
      } catch { rec.loadError = true; }
    }

    function close() {
      const drawer = $("turn-inspector");
      if (!drawer || drawer.hidden) return;
      drawer.hidden = true;
      openRecord = null;
      writeURL(null);
      const back = trigger;
      trigger = null;
      if (back) {
        back.setAttribute("aria-expanded", "false");
        back.focus();
      } else {
        $("conversation")?.focus();
      }
    }

    function timeText(ts) {
      if (!finite(ts)) return missing();
      const date = new Date(ts * 1000);
      return Number.isNaN(date.getTime()) ? missing() : date.toLocaleString(root.i18n?.lang === "en" ? "en-US" : "ja-JP");
    }

    function section(titleKey) {
      const box = el("section", "inspector-section");
      box.append(el("h3", null, t(titleKey)));
      return box;
    }

    function factList(rows) {
      const dl = el("dl", "facts");
      for (const [label, value] of rows) {
        const row = el("div");
        const dd = el("dd", null, value ?? missing());
        if (value === null || value === undefined) dd.dataset.trend = "missing";
        row.append(el("dt", null, label), dd);
        dl.append(row);
      }
      return dl;
    }

    function stateTable(rec) {
      const table = el("table", "table inspector-table");
      const caption = el("caption", "sr-only", t("turns.state_table_caption"));
      const head = el("thead");
      const headRow = el("tr");
      for (const key of ["turns.dimension", "turns.before", "turns.after", "turns.delta", "turns.now"]) {
        const th = el("th", null, t(key));
        th.setAttribute("scope", "col");
        headRow.append(th);
      }
      head.append(headRow);
      const body = el("tbody");
      const summary = rec.summary;
      for (const dim of DIMS) {
        const row = el("tr");
        const th = el("th", null, dimLabel(dim));
        th.setAttribute("scope", "row");
        const cell = value => {
          const td = el("td", null, value ?? missing());
          if (value === null || value === undefined) td.dataset.trend = "missing";
          return td;
        };
        const { value, trend } = deltaOf(summary, dim);
        const delta = cell(value === null ? null : signed(value));
        delta.dataset.trend = trend;
        // 3 decimals: deltas are often below 0.01 and must reconcile with before/after.
        row.append(th, cell(fixed(summary?.before?.[dim], 3)), cell(fixed(summary?.after?.[dim], 3)),
          delta, cell(fixed(current.state?.[dim], 3)));
        body.append(row);
      }
      table.append(caption, head, body);
      return table;
    }

    const TOOL_STATUS = {
      pending: "turns.tool_status_pending", running: "turns.tool_status_running",
      done: "turns.tool_status_done", error: "turns.tool_status_error",
      denied: "turns.tool_status_denied", timeout: "turns.tool_status_timeout",
    };
    const toolStatus = status => t(TOOL_STATUS[status] || "turns.tool_status_unknown");

    function toolList(rec) {
      const box = section("turns.tools_and_approvals");
      if (!rec.tools.size && !rec.approvals.length) {
        box.append(el("p", "inspector-empty", rec.eventsLoaded ? t("turns.no_tools") : missing()));
        return box;
      }
      const list = el("ul", "inspector-list");
      for (const tool of rec.tools.values()) {
        const duration = finite(tool.duration_ms) ? `${Math.round(tool.duration_ms)} ms` : missing();
        list.append(el("li", null, t("turns.tool_line", {
          name: tool.name || "?", status: toolStatus(tool.status),
          duration,
        })));
      }
      for (const approval of rec.approvals) {
        list.append(el("li", null, t("turns.approval_line", {
          name: approval.tool_name || "?", decision: approval.decision || t("turns.pending"),
        })));
      }
      box.append(list);
      return box;
    }

    function renderInspector() {
      const rec = openRecord;
      const pretty = $("inspector-pretty"), json = $("inspector-json"), meta = $("inspector-meta");
      if (!rec || !pretty || !json || !meta) return;
      const isLatest = rec.id !== null && rec.id === latestId;
      meta.replaceChildren();
      const badge = el("span", "badge", isLatest ? t("turns.latest") : t("turns.past"));
      badge.dataset.state = isLatest ? "info" : "warn";
      meta.append(badge, el("span", "inspector-time",
        t("turns.recorded_at", { time: timeText(rec.endedAt ?? rec.startedAt) })));
      meta.append(el("code", "inspector-turn-id", rec.id ?? missing()));

      pretty.replaceChildren();
      const summary = rec.summary;
      const stim = section("turns.stimulus");
      stim.append(factList([
        [t("turns.category"), summary ? (summary.category ?? (summary.deltas ? t("turns.no_update") : null)) : null],
        [t("turns.intensity"), fixed(summary?.intensity)],
        [t("turns.confidence"), fixed(summary?.confidence)],
        [t("turns.classifier"), summary?.classifier ?? null],
        [t("turns.rationale"), summary?.rationale ?? null],
      ]));
      const state = section("turns.state_before_after");
      state.append(el("p", "inspector-hint", t("turns.state_hint")), stateTable(rec));
      const breaker = section("turns.breaker");
      const transition = rec.transitions.at?.(-1) ?? rec.transitions[rec.transitions.length - 1];
      breaker.append(factList([
        [t("turns.breaker_at_turn"), summary?.breaker ? breakerText(summary.breaker) : null],
        [t("turns.breaker_transition"), transition
          ? `${breakerText(transition.from)} → ${breakerText(transition.to)}${reasonSuffix(transition)}`
          : (rec.eventsLoaded ? t("turns.no_transition") : null)],
        [t("turns.breaker_now"), current.level ? breakerText(current.level) : null],
      ]));
      const tokens = section("turns.tokens");
      const usage = rec.usage;
      const tokenRows = [
        [t("chat.input_tokens"), finite(usage?.input_tokens) ? usage.input_tokens.toLocaleString() : null],
        [t("chat.output_tokens"), finite(usage?.output_tokens) ? usage.output_tokens.toLocaleString() : null],
      ];
      // Prompt-cache buckets only when the provider reported them (#254).
      if (finite(usage?.cache_read_tokens) || finite(usage?.cache_write_tokens)) {
        tokenRows.push(
          [t("turns.cache_read"), finite(usage.cache_read_tokens) ? usage.cache_read_tokens.toLocaleString() : null],
          [t("turns.cache_write"), finite(usage.cache_write_tokens) ? usage.cache_write_tokens.toLocaleString() : null],
          [t("turns.cache_hit_rate"), finite(usage.cache_hit_ratio) ? `${(usage.cache_hit_ratio * 100).toFixed(1)}%` : null],
        );
      }
      tokens.append(factList(tokenRows));
      pretty.append(stim, state, breaker, toolList(rec), tokens);
      if (rec.loadError) pretty.append(el("p", "error", t("turns.events_unavailable")));

      json.textContent = JSON.stringify({ turn_id: rec.id, events: rec.events,
        ...(rec.events.length ? {} : { summary: rec.summary }) }, null, 2);
      pretty.hidden = view !== "pretty";
      json.hidden = view !== "json";
      $("inspector-view-pretty")?.setAttribute("aria-pressed", String(view === "pretty"));
      $("inspector-view-json")?.setAttribute("aria-pressed", String(view === "json"));
    }

    function setView(next) {
      view = next === "json" ? "json" : "pretty";
      renderInspector();
    }

    $("inspector-close")?.addEventListener("click", close);
    $("inspector-view-pretty")?.addEventListener("click", () => setView("pretty"));
    $("inspector-view-json")?.addEventListener("click", () => setView("json"));
    doc.addEventListener?.("keydown", event => {
      if (event.key === "Escape" && openRecord) {
        event.preventDefault?.();
        close();
      }
    });

    return { attach, ingest, restore, setCurrent, open, close, setView,
      get selected() { return openRecord?.id ?? null; }, records };
  }

  root.StiraTurns = { create, normalizeSummary, deltaOf, signed, DIMS };
})(typeof window !== "undefined" ? window : globalThis);
