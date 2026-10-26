(() => {
  "use strict";
  const $ = id => document.getElementById(id);
  const dimensions = [
    ["trust", t("chat.trust")], ["curiosity", t("chat.curiosity")],
    ["vigilance", t("chat.vigilance")], ["attachment", t("chat.attachment")],
    ["stability", t("chat.stability")], ["satisfaction", t("chat.satisfaction")]
  ];
  const levels = {
    normal: ["🟢", t("shell.normal")], warning: ["🟡", t("shell.warning")],
    // #310: same word as the nav chip and the settings hub (#284).
    coherence: ["🟣", t("shell.coherence")],
    critical: ["🔴", t("chat.critical")], unknown: ["⚪", t("base.unknown")]
  };
  let busy = false, polling = false, composing = false;
  let attachments = [], readingImages = false, ready = false;
  // #350: send / stream / re-send / images / IME guard are shared with the Stage
  // (chat_core.js). #185: a message whose outcome is unknown is never re-sent
  // automatically; it keeps its Idempotency-Key (sender.unsettled) until the operator
  // re-sends it (same key) or discards it, and no other message can be sent meanwhile.
  const core = StiraChatCore;
  const sender = core.createSender({ fetch: (url, init) => stiraFetch(url, init), t });
  // #350 phase 3: "new conversation" (POST /api/chat/session/new after confirmation).
  const starter = core.createSessionStarter ? core.createSessionStarter({ fetch: (url, init) => stiraFetch(url, init), t }) : null;
  let approvalCount = 0, starting = false, session = null;
  // The server reset the conversation but its new history is not on screen (the re-read
  // failed): nothing is sent until a re-read succeeds ("reload" in the notice).
  let historyStale = false;
  // #601 E5: a history re-read is on the way; its answer replaces the conversation, so a
  // message sent meanwhile would have its bubbles dropped. Sending waits for it (short).
  // Counted (two re-reads can overlap: another tab's turn ended, the reload button): sending
  // comes back only when the last one ended; only the latest started re-read is drawn.
  let reloading = 0, reloadSeq = 0;
  // #187: turn ribbon + Inspector (turn_ribbon.js); absent in isolated tests.
  const turnView = typeof StiraTurns !== "undefined" ? StiraTurns.create() : null;
  // #303: collapsible trend + one-line summary (neural_trend.js); absent in isolated tests.
  const trendView = typeof StiraTrend !== "undefined" ? StiraTrend.create() : null;
  const MAX_SPARK = 30;
  const sparkHistory = Object.fromEntries(dimensions.map(([k]) => [k, []]));
  const finite = value => typeof value === "number" && Number.isFinite(value);
  const count = value => finite(value) ? value.toLocaleString() : "—";
  const set = (id, value) => { $(id).textContent = value ?? "—"; };
  const scroll = () => { $("conversation").scrollTop = $("conversation").scrollHeight; };

  function buildBars(id) {
    for (const [key, label] of dimensions) {
      const row = document.createElement("div");
      row.className = `neural-row ${key}`;
      const caption = document.createElement("label");
      caption.htmlFor = `${id}-${key}`;
      caption.textContent = label;
      const value = document.createElement("span");
      value.id = `${id}-${key}-value`;
      value.textContent = "—";
      const bar = document.createElement("progress");
      bar.id = `${id}-${key}`;
      bar.max = 1;
      bar.value = 0;
      bar.setAttribute("aria-valuetext", t("base.unknown"));
      row.append(caption, value, bar);
      $(id).append(row);
    }
  }

  function renderBars(state = {}) {
    // Sidebar bars only (gateway-neural)
    for (const [key] of dimensions) {
      const raw = state?.[key];
      const value = finite(raw) ? Math.min(1, Math.max(0, raw)) : null;
      const text = value === null ? "—" : `${Math.round(value * 100)}%`;
      const bar = $(`gateway-neural-${key}`);
      const valEl = $(`gateway-neural-${key}-value`);
      if (bar) { bar.value = value ?? 0; bar.setAttribute("aria-valuetext", text); }
      if (valEl) { valEl.textContent = text; }
    }
    // Sparklines (turn-neural)
    updateSparklines(state);
  }

  function updateSparklines(state = {}) {
    if (typeof renderSparkline !== "function") return;
    for (const [key] of dimensions) {
      const raw = state?.[key];
      if (!finite(raw)) continue;
      const hist = sparkHistory[key];
      hist.push(raw);
      if (hist.length > MAX_SPARK) hist.shift();
      const container = $(`spark-${key}`);
      if (!container) continue;
      const opts = key === "stability"
        ? { width: 200, height: 32, className: `dim-${key}`, thresholds: typeof STABILITY_THRESHOLDS !== "undefined" ? STABILITY_THRESHOLDS : [] }
        : { width: 120, height: 32, className: `dim-${key}` };
      renderSparkline(container, hist, opts);
      const valEl = $(`sv-${key}`);
      if (valEl) valEl.textContent = `${Math.round(raw * 100)}%`;
    }
  }

  function renderClassifier(info) {
    const el = $("classifier");
    if (!el) return;
    // #367 (chat_core.classifierText): rules running as a fallback are said in words.
    // Before the first turn ("auto" resolves lazily) it reads unresolved.
    const view = core.classifierText(info, t);
    el.textContent = view.text;
    el.dataset.classifier = view.state;
    el.dataset.fallback = String(view.fallback);
    if (view.title) el.title = view.title; else el.removeAttribute("title");
  }

  // #367: how many turns since start-up moved Neural State, and why the last one did not.
  function renderEmotionUpdates(status) {
    const el = $("emotion-updates");
    if (!el) return;
    const view = core.emotionUpdatesText(status.emotion_updates, t);
    el.textContent = view.text;
    el.dataset.reason = view.reason ?? "";
    if (view.title) el.title = view.title; else el.removeAttribute("title");
  }

  // Context gauge (#350, chat_core.contextGauge, shared with the Stage): the last turn's
  // input (uncached + cache read + cache write) against the context window when the status
  // carries one; the token count otherwise. Read-only.
  let gaugeTurn = null;
  let gaugeStatus = null;
  function renderGauge() {
    const el = $("ctx-gauge");
    if (!el || !core.contextGauge) return;
    const g = core.contextGauge({ turn: gaugeTurn, ...core.contextOf(gaugeStatus) });
    el.hidden = !g;
    if (!g) return;
    const view = core.gaugeText(g, t);
    el.dataset.level = g.level;
    el.textContent = view.text;
    el.title = view.title;
  }

  // #337: 1.0 is both "fewer than the minimum samples" and "measured, no movement".
  function bcText(status) {
    if (!finite(status.bc_score)) return "—";
    if (status.bc_measured === false)
      return t("chat.bc_unmeasured", { count: count(status.bc_samples), min: count(status.bc_min_samples) });
    const text = status.bc_score.toFixed(3);
    return status.bc_measured === true && status.bc_score >= 1 ? `${text} (${t("chat.bc_stable")})` : text;
  }

  function renderStatus(status) {
    gaugeStatus = status;
    renderGauge();
    set("agent-id", status.agent_id);
    set("model", status.model);
    renderClassifier(status.classifier);
    renderEmotionUpdates(status);
    renderBars(status.neural_state);
    const level = Object.hasOwn(levels, status.breaker?.level) ? status.breaker.level : "unknown";
    turnView?.setCurrent(status.neural_state, level);
    trendView?.update(status.neural_state, status.breaker?.level);
    for (const id of ["breaker-indicator", "breaker-level"]) {
      $(id).dataset.level = level;
      set(id, levels[level].join(" "));
    }
    const changed = status.breaker?.last_changed;
    const date = changed ? new Date(changed) : null;
    set("breaker-changed", date && !Number.isNaN(date.getTime()) ? date.toLocaleString("ja-JP") : changed);
    // #455: why the level is what it is (codes and numbers from the server, in words).
    const why = globalThis.StiraBreakerReasons?.describe(level, status.breaker?.reasons, status.breaker?.inputs, t);
    set("breaker-reason", why || t("breaker_reason.none"));
    for (const type of ["episodic", "semantic", "procedural"])
      set(`${type}-count`, count(status.memory?.[`${type}_count`]));
    const soul = status.soul || {};
    // #291: the real read state (an empty or unreadable file is not "loaded") and where it
    // is set; the server never sends the path.
    const soulStates = { empty: "chat.soul_empty", unreadable: "chat.soul_unreadable" };
    set("soul-status", soul.loaded === true ? t("chat.loaded") :
      soulStates[soul.state] ? t(soulStates[soul.state]) :
      soul.loaded === false ? t("chat.headless") : t("base.unknown"));
    set("soul-path", soul.source === "configured" ? t("chat.soul_source_configured") :
      soul.source === "default" ? t("chat.soul_source_default") : soul.path);
    set("bc-score", bcText(status));
    set("input-tokens", count(status.session?.total_input_tokens));
    set("output-tokens", count(status.session?.total_output_tokens));
    const cost = status.session?.estimated_cost;
    set("estimated-cost", finite(cost) ? `$${cost.toFixed(6)}` : "—");
    renderDiagnostics(status.diagnostics);
    const seconds = status.uptime_seconds;
    set("uptime", finite(seconds) ?
      `${Math.floor(seconds / 3600)}h ${Math.floor(seconds % 3600 / 60)}m ${Math.floor(seconds % 60)}s` : "—");
    set("gateway-health", t("chat.connected"));
    $("gateway-health").classList.remove("error");
  }

  // #244: doctor summary + open permission cards, from /api/gateway/status.
  function renderDiagnostics(diagnostics) {
    if (!$("doctor-status")) return;
    const doctor = diagnostics?.doctor;
    if (!doctor?.ran) set("doctor-status", doctor ? t("diagnostics.doctor_not_run") : "—");
    else if (doctor.error) set("doctor-status", t("diagnostics.doctor_failed"));
    else set("doctor-status", t(doctor.errors ? "diagnostics.doctor_errors" : doctor.warnings ? "diagnostics.doctor_warnings" : "diagnostics.doctor_ok",
      { errors: count(doctor.errors), warnings: count(doctor.warnings), roots: count(doctor.roots) }));
    const open = diagnostics?.write_failures?.open;
    set("write-failure-count", finite(open) ? count(open) : "—");
  }

  // Resolves to whether the status differs from the last one read (uptime aside), null
  // when it could not be read: the poller (poll.js, #369) slows down only on false.
  let lastStatusKey = null;
  async function pollStatus() {
    if (polling) return undefined;
    polling = true;
    try {
      const response = await fetch("/api/gateway/status", {
        cache: "no-store", signal: AbortSignal.timeout(4500)
      });
      if (!response.ok) throw new Error();
      const status = await response.json();
      renderStatus(status);
      await pollRunningTurn();
      const key = JSON.stringify(status, (name, value) => name === "uptime_seconds" ? undefined : value);
      const changed = key !== lastStatusKey;
      lastStatusKey = key;
      return changed;
    } catch {
      set("gateway-health", t("chat.update_failed_stale_data"));
      $("gateway-health").classList.add("error");
      return null;
    } finally { polling = false; }
  }

  // #452: a turn this page did not send (another tab, or this page before a reload) may still
  // run on the server — closing a page never stops it. The page says so, offers ■ Stop for it
  // and reads the history when it has ended. The server still refuses a second send ("busy").
  let elsewhereTurn = null;
  // Only the server's running chat turn can be stopped from here (``stoppable``); a turn
  // resumed after an approval is shown as running, without ■ Stop.
  let elsewhereStoppable = false;
  // #555: that turn's progress (its events after ``seq``), so the status line counts its
  // seconds like this page's own turn ("生成中 N 秒", between calls the time since it began).
  let elsewhereProgress = null;
  let elsewhereShown = null; // the status text last written for it (a 1 s timer refreshes it)
  let elsewhereTimer = null; // only while such a turn is shown (not a poll: no request)
  function showElsewhere() {
    if (busy || elsewhereTurn === null) return;
    const status = $("connection-status");
    // Another message written meanwhile (stopping, a failure) is not overwritten.
    if (elsewhereShown !== null && status.textContent !== elsewhereShown) return;
    elsewhereShown = core.elsewhereText(elsewhereProgress, Date.now(), t);
    set("connection-status", elsewhereShown);
  }
  async function readElsewhereEvents(turnId) {
    const after = elsewhereProgress?.turnId === turnId ? elsewhereProgress.seq : 0;
    try {
      const response = await fetch(`/api/turns/${encodeURIComponent(turnId)}/events?after_seq=${after}`,
        { cache: "no-store", signal: AbortSignal.timeout(4500) });
      if (!response.ok) return;
      const body = await response.json();
      if (elsewhereTurn !== turnId) return; // it ended meanwhile
      elsewhereProgress = core.turnProgressOf(elsewhereProgress, turnId, body?.events);
    } catch {
      // the count waits for the next poll
    }
  }
  async function pollRunningTurn() {
    if (busy) return;
    let latest = null;
    try {
      const response = await fetch("/api/turns?limit=1", { cache: "no-store", signal: AbortSignal.timeout(4500) });
      if (!response.ok) return;
      const body = await response.json();
      latest = Array.isArray(body?.turns) ? body.turns[0] || null : null;
    } catch {
      return;
    }
    if (busy) return; // this page sent a message meanwhile: that turn is its own
    const running = latest && latest.completed === false && typeof latest.turn_id === "string"
      ? latest.turn_id : null;
    const stoppable = Boolean(running && latest.stoppable === true);
    if (running === elsewhereTurn && stoppable === elsewhereStoppable) {
      if (running) {
        await readElsewhereEvents(running);
        showElsewhere();
      }
      return;
    }
    const ended = elsewhereTurn !== null && running === null;
    elsewhereTurn = running;
    elsewhereStoppable = stoppable;
    if (running !== elsewhereProgress?.turnId) elsewhereProgress = null;
    elsewhereShown = null;
    if (running && elsewhereTimer === null) elsewhereTimer = setInterval(showElsewhere, 1000);
    if (!running && elsewhereTimer !== null) {
      globalThis.clearInterval?.(elsewhereTimer);
      elsewhereTimer = null;
    }
    updateAttachmentControls();
    if (running) {
      // Owned from the moment it is written: a stop pressed while the first read is out
      // ("止めています") is someone else's text, never adopted after the await.
      elsewhereShown = t("chat.turn_running_elsewhere");
      set("connection-status", elsewhereShown);
      await readElsewhereEvents(running);
      showElsewhere();
    } else if (ended && ready) {
      await reloadHistory();
      globalThis.StiraProgress?.poke(); // e.g. an automatic continue ended at a report
    }
  }

  // #369: every poll below goes through poll.js: paused while the tab is hidden, backed off
  // when the server fails, slower while nothing changes, short again at once when something
  // does (``hold``: a turn runs / an approval waits). ``read`` returns null on failure.
  // Without poll.js (isolated tests) it is the plain timer.
  function keepPolling(read, base, max, hold = () => false, backoffMax) {
    const run = async () => {
      const result = await read();
      if (result === null) throw new Error("read failed");
      return result;
    };
    if (typeof StiraPoll === "undefined") {
      void read();
      setInterval(() => { if (!document.hidden) void read(); }, base);
      return null;
    }
    const poller = StiraPoll.create({ run, base, max, hold, doc: document, ...(backoffMax ? { backoffMax } : {}) });
    poller.start();
    return poller;
  }

  // #189: approval cards (approvals.js) — the same /api/hitl state as the nav chip and
  // the approvals page; absent in isolated tests.
  const approvalQueue = typeof StiraApprovals !== "undefined"
    ? StiraApprovals.create({ list: $("approval-list"), panel: $("hitl-panel"), live: $("approval-live"),
      onEmpty: () => $("message").focus(),
      onCount: count => { approvalCount = count; updateNewSession(); } })
    : null;
  // #244: permission cards for failed writes. "Fixed it -> retry once" sends one ordinary
  // chat message (every gate applies); while a turn runs it only fills the composer.
  const permissionQueue = typeof StiraPermissions !== "undefined" && $("permission-list")
    ? StiraPermissions.create({ list: $("permission-list"), panel: $("permission-panel"), live: $("permission-live"),
      onRetry: prompt => {
        $("message").value = prompt;
        if (!busy && ready) $("chat-form").requestSubmit();
        else $("message").focus();
      } })
    : null;

  function bubble(role, text = "", images = []) {
    $("empty-history")?.remove();
    const article = document.createElement("article");
    article.className = `message ${role}`;
    const label = document.createElement("header");
    label.textContent = role === "user" ? t("chat.you") : t("chat.agent");
    const body = document.createElement("div");
    body.className = "message-text";
    body.textContent = text;
    for (const attachment of images) {
      const image = document.createElement("img");
      image.className = "message-image";
      image.src = attachment.url;
      image.alt = attachment.name;
      body.append(image);
    }
    const turnSummary = document.createElement("aside");
    turnSummary.className = "turn-summary";
    turnSummary.setAttribute("aria-label", t("chat.turn_neural_state_changes"));
    turnSummary.hidden = true;
    const meta = document.createElement("small");
    meta.className = "message-meta";
    article.append(label, body, turnSummary, meta);
    $("conversation").append(article);
    scroll();
    return { article, body, turnSummary, meta };
  }

  function tokenText(turn) {
    return t("chat.in_count_out_count2", { count: count(turn.input_tokens), count2: count(turn.output_tokens) });
  }

  function renderTurnSummary(target, summary) {
    target.textContent = "";
    target.hidden = true;
    const category = typeof summary?.category === "string" ? summary.category.trim() : "";
    const deltas = summary?.deltas && typeof summary.deltas === "object"
      ? Object.entries(summary.deltas).filter(([, value]) => finite(value) && value !== 0)
      : [];
    if (!category || !deltas.length) return false;

    const signed = value => `${value > 0 ? "+" : ""}${value.toFixed(4)}`
      .replace(/(\.\d*?[1-9])0+$/, "$1").replace(/\.0+$/, ".00");
    const breaker = typeof summary.breaker === "string" && summary.breaker
      ? summary.breaker : "unknown";
    const headline = document.createElement("p");
    headline.className = "turn-summary-line";
    headline.textContent = t("chat.turn_detected", {
      category,
      deltas: deltas.map(([key, value]) => `${key} ${signed(value)}`).join(", "),
      breaker,
    });
    target.append(headline);

    if (typeof summary.emotion_transcription === "string" && summary.emotion_transcription) {
      const rationale = document.createElement("small");
      rationale.className = "turn-summary-rationale";
      rationale.textContent = summary.emotion_transcription;
      target.append(rationale);
    }
    target.hidden = false;
    return true;
  }

  // The boundary of a conversation started with /new ("New conversation (HH:MM)").
  function boundary(current) {
    const text = core.sessionNote ? core.sessionNote(current, t, document.documentElement?.lang) : null;
    if (!text) return null;
    const line = document.createElement("p");
    line.className = "session-boundary";
    line.setAttribute("role", "note");
    line.textContent = text;
    return line;
  }

  // The server's conversation: its boundary line (a session started with /new), then its turns.
  // replace: drop what the page shows first (after a new conversation started).
  function renderConversation(conversation, replace) {
    const turns = conversation.turns;
    session = conversation.session;
    if (replace) {
      const empty = document.createElement("p");
      empty.id = "empty-history";
      empty.className = "empty";
      empty.textContent = t("chat.send_a_message_to_begin");
      $("conversation").replaceChildren(empty);
      gaugeTurn = null;
      set("turn-tokens", t("chat.turn_tokens"));
    }
    const line = boundary(session);
    if (line) $("conversation").prepend(line);
    gaugeTurn = turns.length ? turns[turns.length - 1] : gaugeTurn;
    renderGauge();
    for (const turn of turns) {
      bubble("user", turn.message + (turn.image_count ? `\n${t("chat.images_not_retained", { count: turn.image_count })}` : ""));
      const reply = bubble("agent", turn.response);
      renderTurnSummary(reply.turnSummary, turn.turn_summary);
      reply.meta.textContent = tokenText(turn);
      turnView?.attach(reply, turn.turn_id, turn.turn_summary, { past: true, completed: true });
    }
  }

  async function loadHistory() {
    try {
      const conversation = core.fetchConversation
        ? await core.fetchConversation((url, init) => fetch(url, init))
        : { turns: await core.fetchHistory((url, init) => fetch(url, init)), session: null };
      renderConversation(conversation, false);
      set("connection-status", t("chat.ready"));
    } catch {
      set("connection-status", t("chat.history_unavailable"));
    } finally {
      void turnView?.restore();
      $("message").disabled = false;
      ready = true;
      updateAttachmentControls();
      scroll();
    }
  }

  function updateAttachmentControls() {
    $("send-button").disabled = !ready || busy || readingImages || starting || historyStale || reloading > 0;
    $("attach-button").disabled = !ready || busy || readingImages;
    $("image-input").disabled = !ready || busy || readingImages;
    $("unsettled-resend").disabled = !ready || busy;
    $("unsettled-discard").disabled = busy;
    // #452: while this page's turn runs, the send button's neighbour stops it on the server.
    const running = busy || (elsewhereTurn !== null && elsewhereStoppable);
    const stopButton = $("stop-turn-button");
    if (stopButton) {
      stopButton.hidden = !running;
      if (!running) stopButton.disabled = false;
    }
    const stopHelp = $("stop-turn-help");
    if (stopHelp) stopHelp.hidden = !running;
    updateNewSession();
  }

  // Disabled with the reason (chat_core.newSessionBlock) while a turn runs here, an approval
  // waits or a send's outcome is unknown; the server refuses the same cases (and a turn of
  // another page) with 409.
  function newSessionBlock() {
    if (!starter || !core.newSessionBlock) return "chat.new_blocked_loading";
    return core.newSessionBlock({ ready, busy, approval: approvalCount > 0, unsettled: Boolean(sender.unsettled),
      starting: starting || starter.running });
  }
  function updateNewSession() {
    const button = $("new-session");
    if (!button) return;
    const reason = newSessionBlock();
    button.disabled = Boolean(reason);
    const why = reason ? t(reason) : t("chat.new_hint");
    button.title = why;
    set("new-session-reason", why);
  }

  // After a reset (this request's, or this key's earlier one) the page shows the server's
  // conversation as it is now — never an empty page it only assumes.
  // -> true when the server's history is on screen. On failure the old conversation is
  // cleared from the page (it is gone on the server) and sending stays off until a reload.
  async function showServerConversation(result) {
    let shown = false;
    try {
      renderConversation(await core.fetchConversation((url, init) => fetch(url, init)), true);
      shown = true;
    } catch {
      $("conversation").replaceChildren(boundary({ reason: "new", startedAt: Date.now() / 1000 }));
      gaugeTurn = null;
      renderGauge();
    }
    setHistoryStale(!shown);
    set("connection-status", t(!shown ? "chat.new_history_failed"
      : result.outcome === "already" && !result.session?.current ? "chat.new_already_superseded"
        : result.outcome === "already" ? "chat.new_already" : "chat.new_started"));
    return shown;
  }

  function setHistoryStale(stale) {
    historyStale = stale;
    const notice = $("history-stale");
    if (notice) notice.hidden = !stale;
    updateAttachmentControls();
  }

  async function reloadHistory() {
    const button = $("history-reload");
    if (button) button.disabled = true;
    reloading += 1;
    const mine = ++reloadSeq;
    updateAttachmentControls();
    try {
      const conversation = await core.fetchConversation((url, init) => fetch(url, init));
      if (mine === reloadSeq) {
        renderConversation(conversation, true);
        setHistoryStale(false);
        set("connection-status", t("chat.ready"));
      }
    } catch {
      if (mine === reloadSeq) set("connection-status", t("chat.new_history_failed"));
    } finally {
      reloading -= 1;
      if (button) button.disabled = reloading > 0;
      updateAttachmentControls();
    }
  }

  async function newConversation(fromComposer) {
    const blocked = newSessionBlock();
    if (blocked) {
      set("connection-status", t(blocked));
      return;
    }
    const ok = await core.confirmNewSession(window.StiraConfirm, t);
    if (!ok) {
      set("connection-status", t("chat.new_cancelled"));
      return;
    }
    const again = newSessionBlock();
    if (again) {
      set("connection-status", t(again));
      return;
    }
    starting = true;
    updateAttachmentControls();
    let result;
    try {
      result = await starter.start();
      if (!result.ok && result.outcome === "unknown" && starter.resolve) {
        // only this key's digest in the history resolves it (not another tab's reset)
        try {
          result = (await starter.resolve(await core.fetchConversation((url, init) => fetch(url, init)))) || result;
        } catch { /* stays unknown */ }
      }
      // Sending stays off until the server's conversation is on screen: a reply arriving
      // before the re-read history would be overwritten by it.
      if (result.ok) await showServerConversation(result);
    } finally {
      starting = false;
    }
    if (result.ok) {
      if (fromComposer && core.commandOf($("message").value) === "new") $("message").value = "";
      void pollStatus();
    } else {
      set("connection-status", result.outcome === "unknown"
        ? `${t("chat.new_unknown")} ${result.error?.message || ""}`.trim() : result.error.message);
    }
    updateAttachmentControls();
    $("message").focus();
  }

  function showUnsettled() {
    const unsettled = sender.unsettled;
    $("unsettled-send").hidden = !unsettled;
    if (!unsettled) return;
    const line = unsettled.message.replace(/\s+/g, " ").trim();
    const shown = line.length > 120 ? `${line.slice(0, 119)}…` : line;
    $("unsettled-preview").textContent = unsettled.images.length
      ? `${shown} ${t("chat.unsettled_images", { count: unsettled.images.length })}`.trim() : shown;
  }

  function renderAttachments() {
    const preview = $("image-preview");
    preview.replaceChildren();
    preview.hidden = attachments.length === 0;
    attachments.forEach((attachment, index) => {
      const item = document.createElement("div");
      item.className = "attachment";
      const image = document.createElement("img");
      image.src = attachment.url;
      image.alt = attachment.name;
      const remove = document.createElement("button");
      remove.type = "button";
      remove.textContent = `× ${attachment.name}`;
      remove.setAttribute("aria-label", t("chat.remove_name", { name: attachment.name }));
      remove.addEventListener("click", () => {
        if (busy || readingImages) return;
        attachments.splice(index, 1);
        renderAttachments();
      });
      item.append(image, remove);
      preview.append(item);
    });
  }

  async function addImages(files) {
    if (!ready || busy || readingImages || !files.length) return;
    set("image-error", "");
    const refused = core.imageError(attachments, files);
    if (refused) {
      set("image-error", t(refused));
      return;
    }
    readingImages = true;
    updateAttachmentControls();
    try {
      const added = await core.readImages(files, { FileReader, t });
      attachments.push(...added);
      renderAttachments();
    } catch (error) {
      set("image-error", error.message);
    } finally {
      readingImages = false;
      updateAttachmentControls();
    }
  }

  $("attach-button").addEventListener("click", () => $("image-input").click());
  $("image-input").addEventListener("change", async event => {
    await addImages(Array.from(event.target.files));
    event.target.value = "";
  });
  $("message").addEventListener("paste", event => {
    const files = Array.from(event.clipboardData?.items || [])
      .filter(item => item.kind === "file").map(item => item.getAsFile()).filter(Boolean);
    if (files.length) {
      event.preventDefault();
      void addImages(files);
    }
  });

  $("chat-form").addEventListener("submit", async event => {
    event.preventDefault();
    const message = $("message").value.trim();
    // `/new` is the page's command, never a message for the model (with images too): it opens
    // the new-conversation confirmation. Fail closed without core.commandOf; the shared sender
    // refuses it as well.
    if (/^\/new$/i.test(message) || (typeof core.commandOf === "function" && core.commandOf(message) === "new")) {
      await newConversation(true);
      return;
    }
    if (!ready || busy || readingImages || starting || reloading > 0 || (!message && !attachments.length)) return;
    if (historyStale) {
      set("connection-status", t("chat.new_history_failed"));
      return;
    }
    if (starter?.pendingKey) {
      // a reset whose outcome is unknown: this message could land in the conversation that is
      // about to be dropped. Press "new conversation" again (same key) first.
      set("connection-status", t("chat.new_unknown_blocks_send"));
      return;
    }
    const payload = core.payloadOf(message, attachments);
    if (sender.blocked(payload)) {
      // Another message now would hide the unsettled one: resend or discard it first.
      set("connection-status", t("chat.unsettled_blocked"));
      showUnsettled();
      $("unsettled-resend").focus();
      return;
    }
    await deliver(message, attachments.slice(), payload, false);
  });

  $("new-session")?.addEventListener("click", () => { void newConversation(false); });
  $("history-reload")?.addEventListener("click", () => { void reloadHistory(); });
  $("unsettled-resend").addEventListener("click", () => {
    const unsettled = sender.unsettled;
    if (!unsettled || !ready || busy || reloading > 0) return;
    void deliver(unsettled.message, unsettled.images, unsettled.body, true);
  });
  $("unsettled-discard").addEventListener("click", () => {
    if (busy) return;
    sender.discard(); // only an explicit discard drops an unknown outcome's key
    showUnsettled();
    updateNewSession();
    set("connection-status", t("chat.unsettled_discarded"));
    $("message").focus();
  });

  // fromPanel: re-sending the unsettled message; the composer keeps what is typed now.
  async function deliver(message, sentImages, payload, fromPanel) {
    busy = true;
    for (const poller of [statusPoller, approvalPoller, permissionPoller]) poller?.boost();
    updateAttachmentControls();
    $("message").disabled = true;
    if (!fromPanel) $("message").value = "";
    bubble("user", message, sentImages);
    const reply = bubble("agent");
    reply.article.setAttribute("aria-busy", "true");
    // #86: show a thinking indicator until the first delta (or an error) arrives,
    // so the model's "thinking" time is visible instead of an empty bubble.
    const thinking = document.createElement("span");
    thinking.className = "thinking-indicator";
    thinking.setAttribute("role", "img");
    thinking.setAttribute("aria-label", t("chat.thinking"));
    thinking.append(
      document.createElement("span"),
      document.createElement("span"),
      document.createElement("span"),
    );
    reply.body.append(thinking);
    let thinkingCleared = false;
    const clearThinking = () => {
      if (!thinkingCleared) {
        thinking.remove();
        thinkingCleared = true;
      }
    };
    set("connection-status", t("chat.thinking"));
    set("turn-tokens", t("chat.turn_tokens_2"));
    // #445: "生成中 N 秒" under the reply while a model call runs (model.* turn events; the
    // count goes on between them). Past LONG_GENERATION_SEC the status line says it once.
    let generation = null;
    let longAnnounced = false;
    const showGeneration = () => {
      const text = core.generationText(generation, Date.now(), t);
      if (!text && reply.meta.dataset.generating !== "1") return;
      reply.meta.textContent = text;
      if (text) reply.meta.dataset.generating = "1"; else delete reply.meta.dataset.generating;
      if (text && !longAnnounced && Date.now() - generation.since >= core.LONG_GENERATION_SEC * 1000) {
        longAnnounced = true;
        set("connection-status", t("chat.generating_long"));
      }
    };
    const generationTimer = setInterval(showGeneration, 1000);
    let stoppedTurn = false;
    const result = await sender.send({ message, images: sentImages, payload }, {
      delta(data) {
        clearThinking();
        set("connection-status", t("chat.responding"));
        reply.body.append(document.createTextNode(data.text || ""));
        scroll();
      },
      turn(data) {
        clearThinking();
        generation = null;
        showGeneration();
        stoppedTurn = data.stop === "cancelled";
        renderTurnSummary(reply.turnSummary, data.turn_summary);
        reply.meta.textContent = tokenText(data);
        set("turn-tokens", t("chat.turn_tokens_tokentext", { tokenText: tokenText(data) }));
        gaugeTurn = data;
        renderGauge();
        if (data.status) renderStatus(data.status);
        turnView?.attach(reply, data.turn_id, data.turn_summary, { completed: true });
      },
      uiEvent(data) {
        generation = core.generationOf(generation, data, Date.now());
        showGeneration();
        turnView?.ingest(data, reply);
        if (data?.type === "approval.requested" || data?.type === "approval.resolved") approvalPoller?.poke();
        // ADR 0012 phase 5b: a finished tool call may have been approved automatically; the
        // YOLO badge reads its status now (no-op without STIRA_YOLO_ALLOW: no instance).
        if (data?.type === "tool_call.updated") globalThis.StiraYolo?.poke();
      },
    });
    try {
      if (result.ok) {
        if (!fromPanel) {
          attachments = [];
          if (sentImages.length) renderAttachments();
        }
        set("connection-status", t(stoppedTurn ? "chat.turn_stopped" : "chat.ready"));
        if (stoppedTurn) reply.meta.textContent = `${t("chat.turn_stopped")} · ${reply.meta.textContent}`;
      } else {
        const { error, delivery, completed } = result;
        clearThinking();
        reply.meta.textContent = error.message || t("chat.connection_failed");
        reply.meta.classList.add("error");
        if (error.code === "max_tokens") {
          // #470: the cut-off note says "raise the limit": take the operator straight there.
          const link = document.createElement("a");
          link.href = "/settings#sec-cost-output";
          link.className = "cut-off-settings-link";
          link.textContent = t("chat.max_tokens_settings_link");
          reply.meta.append(" ", link);
        }
        set("connection-status", t(delivery === "duplicate" ? "chat.already_delivered_status"
          : error.code === "max_tokens" ? "chat.max_tokens_status"
          : error.code === "model_failure" ? "chat.model_failure_status"
          : delivery === "unknown" && !completed ? "chat.delivery_unknown"
            : "chat.failed_not_automatically_retried"));
        // Already received: do not offer the same text for a second, new send.
        if (delivery !== "duplicate" && !fromPanel) $("message").value = message;
        if (!completed) set("turn-tokens", t("chat.turn_tokens"));
      }
    } finally {
      globalThis.clearInterval?.(generationTimer);
      delete reply.meta.dataset.generating; // an error line written meanwhile stays
      reply.article.removeAttribute("aria-busy");
      busy = false;
      showUnsettled();
      updateAttachmentControls();
      $("message").disabled = false;
      $("message").focus();
      scroll();
      void pollStatus();
      permissionPoller?.poke(); // a failed write of this turn shows its card now
      globalThis.StiraProgress?.poke(); // ADR 0014: the turn may have ended at a checkpoint
    }
  }

  // ADR 0014 (#451): the progress report's Continue / Redirect are ordinary messages of this
  // conversation (progress_reply names the report). -> false when it cannot send now.
  globalThis.StiraProgressSend = async (message, reply) => {
    if (busy || !ready || elsewhereTurn !== null) return false;
    const payload = JSON.stringify({ message, progress_reply: reply });
    if (sender.blocked(payload)) return false;
    await deliver(message, [], payload, true);
    return true;
  };

  // #452: the explicit stop. One press (a stop only narrows: nothing runs or is approved by
  // it); the reply so far stays, and the same message can be sent again afterwards.
  $("stop-turn-button")?.addEventListener("click", async () => {
    const button = $("stop-turn-button");
    if ((!busy && (elsewhereTurn === null || !elsewhereStoppable)) || button.disabled) return;
    button.disabled = true;
    set("connection-status", t("chat.stopping_turn"));
    // Another page's turn is named, so a stop never lands on a turn started meanwhile.
    const turnId = busy ? null : elsewhereTurn;
    const outcome = await core.cancelTurn((url, init) => stiraFetch(url, init), turnId, t);
    if (!outcome.ok && outcome.code !== "no_turn") {
      button.disabled = false;
      set("connection-status", t("chat.stop_failed", { error: outcome.error?.message || "" }));
    }
  });

  $("message").addEventListener("compositionstart", () => { composing = true; });
  $("message").addEventListener("compositionend", () => { composing = false; });
  $("message").addEventListener("keydown", event => {
    if (core.isSendKey(event, composing)) {
      event.preventDefault();
      $("chat-form").requestSubmit();
    }
  });
  buildBars("gateway-neural");
  void loadHistory();
  // A turn on this page or a waiting approval keeps the pollers short; an approval request
  // that arrives on this page's own stream (ui_event) is read at once, not on the next poll.
  const statusPoller = keepPolling(pollStatus, 5000, 20000, () => busy || elsewhereTurn !== null);
  const approvalPoller = approvalQueue
    ? keepPolling(() => approvalQueue.refresh(), 1000, 5000, () => busy || approvalCount > 0, 5000) : null;
  let lastPermissionCount = null;
  const permissionPoller = permissionQueue
    ? keepPolling(async () => {
      const count = await permissionQueue.refresh();
      if (count === null || count === undefined) return count;
      const changed = count !== lastPermissionCount || count > 0;
      lastPermissionCount = count;
      return changed;
    }, 3000, 20000, () => busy) : null;
  $("doctor-rerun")?.addEventListener("click", async () => {
    const rerun = $("doctor-rerun");
    rerun.disabled = true;
    try {
      const response = await stiraFetch("/api/diagnostics/doctor", { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
      if (!response.ok) throw new Error(String(response.status));
      await pollStatus();
    } catch {
      set("doctor-status", t("diagnostics.doctor_failed"));
    } finally { rerun.disabled = false; }
  });
})();
