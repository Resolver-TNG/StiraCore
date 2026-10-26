/* YOLO button, badge and log lines (ADR 0012 §1a, §8b, §12; phase 5b).
 *
 * Only on a server started with STIRA_YOLO_ALLOW: the templates then carry one
 * <div class="yolo-slot"> (chat header, Stage dialogue box edge) and this script. Without it
 * there is no slot, no script and no request.
 *
 * Everything shown comes from GET /api/yolo/status (the browser holds no policy):
 *   offered            -> "Start YOLO" (the tier dialog, then POST /api/yolo/request, then
 *                         the same page's confirmation: StiraHostConfirm.yolo, checkboxes)
 *   request pending    -> "waiting for the confirmation" (host channel: the countdown) + cancel
 *   window active      -> tier, time left, automatic approvals used/limit, Stop; phase 7:
 *                         "Includes exec" and exec used/limit when the press chose exec
 *   window paused      -> "Paused (WARNING)" + when it resumes, Stop
 *   last_end           -> the end reason until acknowledged (OK)
 *   latched            -> why, and Unlatch (POST /api/yolo/unlatch: a confirmed operation,
 *                         the shared dialog through stiraFetch's 428 handling)
 * The tier dialog (#450) also says, for the chosen scope, how many tools (and which) a window
 * could approve — status.targets, computed by the server from the saved Ask rules — and warns
 * when that is none (the window still starts; the warning links to the tool settings).
 * Stop (POST /api/yolo/stop) is in every state where something is current (pending,
 * issuing, active, paused) and also when the status could not be read: it needs no
 * confirmation, no status read and no dialog.
 *
 * Polling: only while something is current (pending / issuing: 1 s, for the countdown;
 * active / paused: 3 s, widening to 15 s while nothing changes); never in a hidden tab;
 * once at load, on return to the tab and after this page's own actions. Idle (no window,
 * no request) is not polled. poke() reads now (the chat / Stage call it after a turn).
 *
 * Log lines: each new status.auto_approvals item of the current window becomes one line
 * "Auto-approved (YOLO internal): file_write #a3f2 — 37/2000" — appended to the chat log
 * (#conversation) and announced as a "stira:yolo-line" event (the Stage puts it in its
 * backlog and raw view). Lines from before this page loaded are not repeated: one line says
 * how many there were; more than the server keeps (20) between two reads -> one "N more" line.
 * Order of answers: a read is drawn only if it was sent after the drawn answer arrived; Stop's
 * answer is drawn unless the drawn answer was sent after Stop's arrived (a narrowing wins a
 * tie). Only an answer that is drawn moves the mark (a read whose body is still on the way, a
 * failed read: nothing); so a slow read never undoes Stop, and Stop never waits for one.
 *
 * Re-rendering touches the DOM only where a text changed (no flicker); a new structure
 * (another state) keeps the keyboard focus inside the badge.
 */
(function (root) {
  "use strict";
  const HEX32 = /^[0-9a-f]{32}$/;
  const SHORT = /^[0-9a-f]{8}$/;
  const REF = /^[0-9a-f]{4}$/;
  const TOOL = /^(?:[a-z][a-z0-9_]{0,47}|mcp:[0-9a-f]{8})$/;
  const CODE = /^[a-z0-9_]{1,48}$/;
  const COMMAND = /^stira yolo confirm [0-9a-f]{8}$/;
  const ENDS = new Set([
    "expired_time", "expired_sleep", "expired_cap", "revoked_stop", "revoked_new_session",
    "revoked_flapping", "issue_aborted_stop", "issue_aborted_new_session", "issue_aborted_cancel",
    "issue_aborted_epoch", "revoked_audit_failed", "revoked_clock_anomaly", "aborted_issue_failed",
  ]);
  const PAUSES = new Set(["breaker_warning", "breaker_coherence", "breaker_critical", "breaker_unknown"]);
  const REFUSALS = new Set([
    "yolo_rate_limited", "yolo_active", "yolo_latched", "yolo_request_pending", "yolo_tier_not_allowed",
    "yolo_clock_failed", "yolo_audit_failed", "yolo_busy", "yolo_closing", "yolo_not_allowed",
    "clock_not_measured", "tool_call_running", "yolo_exec_not_allowed",
  ]);
  const LATCH_SUBJECTS = new Set(["window", "orphan_marker", "unreadable"]);
  const ACK_KEY = "stira.yolo.ack";
  const SOON_SEC = 600;
  const POLL = { pending: 1000, active: 3000, activeMax: 15000, backoffMax: 30000 };

  const isObj = (v) => v !== null && typeof v === "object" && !Array.isArray(v);
  const int = (v) => Number.isInteger(v) && v >= 0;
  const code = (v) => (typeof v === "string" && CODE.test(v) ? v : "unknown");
  const TOOLS_URL = "/settings#sec-tools";
  const TARGET_NAMES_SHOWN = 12;

  // #450: what the tier dialog says about status.targets for one scope. ``undefined`` (a
  // server without it): nothing; ``null`` (the rules could not be read): unknown. Only
  // names that look like tool names are shown (anything else: "?").
  function targetsView(targets, tier) {
    if (targets === undefined) return null;
    const scope = tier === "exec" ? "exec" : tier === "external" ? "external" : "internal";
    const entry = isObj(targets) && isObj(targets.scopes) ? targets.scopes[scope] : null;
    if (!isObj(entry) || !int(entry.count) || !Array.isArray(entry.tools)) return { kind: "unknown" };
    const name = (n) => (typeof n === "string" && TOOL.test(n) ? n : "?");
    // An Ask tool no window approves (a person's card), and an exec tool that is not
    // offered at all (STIRA_ENABLE_EXEC_TOOLS unset) — said apart.
    const rest = Array.isArray(targets.excluded) ? targets.excluded.filter(isObj) : [];
    const excluded = rest.filter((e) => e.reason !== "exec_not_exposed").map((e) => name(e.name));
    const hidden = rest.filter((e) => e.reason === "exec_not_exposed").map((e) => name(e.name));
    if (entry.count === 0) return { kind: "none", excluded, hidden };
    const tools = entry.tools.map(name);
    return {
      kind: "some",
      count: entry.count,
      shown: tools.slice(0, TARGET_NAMES_SHOWN),
      more: Math.max(0, entry.count - TARGET_NAMES_SHOWN),
      excluded,
      hidden,
    };
  }

  // --- pure parts (tests/yolo_ui_test.cjs) ---------------------------------------------

  function clock(epoch) {
    if (!Number.isFinite(epoch)) return "--:--";
    const d = new Date(epoch * 1000);
    return `${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
  }

  function remainingText(sec, t) {
    if (!int(sec)) return "";
    // Rounded up to the minute first, so 3599 s and 3600 s both read "1 h"; the unit is
    // always written out ("6:00" read as either hours or minutes).
    const minutes = Math.ceil(sec / 60);
    if (minutes < 60) return t("yolo.remaining_min", { m: minutes });
    const h = Math.floor(minutes / 60), m = minutes % 60;
    return m === 0 ? t("yolo.remaining_h", { h }) : t("yolo.remaining_hm", { h, m });
  }

  // External tools ask again only when the status says so: a sub-cap that is used up (or 0).
  // A window with no ``external_limit`` in the answer says nothing either way.
  function backToAsk(ext) {
    return Boolean(ext) && ext.used >= ext.limit;
  }

  function lineText(item, t) {
    if (!isObj(item) || !int(item.n) || !int(item.limit)) return null;
    const tool = typeof item.tool === "string" && TOOL.test(item.tool) ? item.tool : "?";
    const ref = typeof item.ref === "string" && REF.test(item.ref) ? item.ref : "????";
    // Phase 7: an exec tool's line says exec and counts the exec sub-cap; the tool name and
    // #ref only, never the command (the server does not send one).
    if (item.tier === "exec" && int(item.exec_n) && int(item.exec_limit)) {
      return t("yolo.auto_approved_line_exec", {
        tool, id: ref, used: item.n, limit: item.limit, exec: item.exec_n, exec_limit: item.exec_limit,
      });
    }
    // Phase 6: an external tool's line says so and adds the external sub-cap's count; it
    // never names the destination (the server does not send one).
    if (item.tier === "external" && int(item.external_n) && int(item.external_limit)) {
      return t("yolo.auto_approved_line_external", {
        tool, id: ref, used: item.n, limit: item.limit, ext: item.external_n, ext_limit: item.external_limit,
      });
    }
    return t("yolo.auto_approved_line", { tool, id: ref, used: item.n, limit: item.limit });
  }

  // #448: what ran in the window (counts only): every tool call by decision. ``null`` when
  // the answer has no breakdown (an older server) — then nothing is claimed.
  function toolsOf(calls) {
    if (!isObj(calls) || !int(calls.total) || !isObj(calls.by_decision)) return null;
    const d = calls.by_decision;
    const n = (v) => (int(v) ? v : 0);
    return {
      total: calls.total, auto: n(d.auto_approved), always: n(d.always), manual: n(d.manual),
      denied: n(d.denied), failed: n(calls.failed),
      // ADR 0025 §7: answered by a session allowance (counts only; absent on older servers).
      again: n(d.allowed_again),
    };
  }

  function toolsText(tools, t) {
    if (!tools) return "";
    const text = t("yolo.tools", {
      total: tools.total, auto: tools.auto, always: tools.always, manual: tools.manual, denied: tools.denied,
    });
    const again = tools.again > 0 ? `${text} · ${t("yolo.tools_allowed_again", { again: tools.again })}` : text;
    return tools.failed > 0 ? `${again} · ${t("yolo.tools_failed", { failed: tools.failed })}` : again;
  }

  function endKey(end) {
    return isObj(end) ? `${typeof end.id === "string" ? end.id : "?"}:${Number.isFinite(end.at) ? end.at : "?"}` : null;
  }

  // status (or null) + local page state -> what to draw. Pure.
  // #601 E13: a failed read outranks an older answer that shows no Stop (offered, ended,
  // latched, unavailable): the window may have started elsewhere since, so the badge says the
  // status could not be read and keeps Stop; an older answer that already shows Stop (window,
  // pending, issuing) is kept as drawn.
  function viewOf(status, local = {}) {
    const v = viewOfStatus(status, local);
    if (local.failed && v.mode !== "window" && v.mode !== "pending" && v.mode !== "issuing") {
      return { mode: "unreadable", notice: local.notice || null };
    }
    return v;
  }

  function viewOfStatus(status, local = {}) {
    if (!isObj(status)) return { mode: local.failed ? "unreadable" : "none", notice: local.notice || null };
    const notice = local.notice || null;
    const request = isObj(status.request) ? status.request : null;
    const win = isObj(status.window) ? status.window : null;
    const current = request && (request.state === "pending" || request.state === "issuing") ? request : null;
    const lastEnd = isObj(status.last_end) ? status.last_end : null;
    const ended = lastEnd && endKey(lastEnd) !== local.acked
      ? { reason: ENDS.has(lastEnd.reason) ? lastEnd.reason : "unknown", abnormal: lastEnd.class !== "normal", key: endKey(lastEnd) }
      : null;
    if (win && (win.state === "active" || win.state === "paused")) {
      const paused = win.state === "paused";
      return {
        mode: "window", notice, paused,
        tier: win.tier === "external" ? "external" : "internal",
        remaining: int(win.remaining_sec) ? win.remaining_sec : null,
        endsAt: Number.isFinite(win.ends_at) ? win.ends_at : null,
        used: int(win.used) ? win.used : 0,
        limit: int(win.limit) ? win.limit : int(win.cap) ? win.cap : null,
        pause: paused ? (PAUSES.has(win.pause_reason) ? win.pause_reason : "breaker_unknown") : null,
        // #455: the breaker's codes and numbers behind the pause (worded, never shown raw)
        pauseWhy: paused && Array.isArray(win.pause_breaker_reasons) && win.pause_breaker_reasons.length
          ? { reasons: win.pause_breaker_reasons.filter((c) => typeof c === "string" && CODE.test(c)).slice(0, 8),
              inputs: isObj(win.pause_breaker_inputs) ? win.pause_breaker_inputs : {} }
          : null,
        resumeIn: paused && int(win.resume_in_sec) ? win.resume_in_sec : null,
        // Phase 6: a window of tier external shows its sub-cap; used up (or 0) → external
        // tools ask again while the window goes on.
        ext: win.tier === "external" && int(win.external_limit)
          ? { used: int(win.external_used) ? win.external_used : 0, limit: win.external_limit }
          : null,
        // Phase 7: the press chose "Include exec" (tier external only); its sub-cap, and
        // used up (or 0) → exec tools ask again while the window goes on.
        exec: win.exec === true && win.tier === "external",
        execCap: win.exec === true && win.tier === "external" && int(win.exec_limit)
          ? { used: int(win.exec_used) ? win.exec_used : 0, limit: win.exec_limit }
          : null,
        soon: Boolean(int(win.remaining_sec) && win.remaining_sec <= SOON_SEC),
        id: typeof win.id === "string" ? win.id : null,
        tools: toolsOf(win.tool_calls),
      };
    }
    if (current && current.state === "pending") {
      const channel = current.channel === "browser" ? "browser" : "host";
      return {
        mode: "pending", notice, channel,
        hostKind: current.channel === "command" ? "command" : "dialog",
        id: HEX32.test(current.id) ? current.id : null,
        short: SHORT.test(current.short) ? current.short : "?",
        left: int(current.expires_in) ? current.expires_in : null,
        own: Boolean(local.own && local.own === current.id),
        command: typeof local.command === "string" && COMMAND.test(local.command) ? local.command : null,
      };
    }
    if (current) return { mode: "issuing", notice };
    // #594: an abnormal end keeps the window as "ended" and latches (ADR 0012 §11), so the
    // latch is read before the ended window; still closing (not latched yet) keeps Stop below.
    if (status.latched && !(win && (win.state === "active" || win.state === "paused"))) {
      const latches = Array.isArray(status.latches) ? status.latches.filter(isObj) : [];
      const target = latches.find((l) => HEX32.test(l.window_id)) || null;
      const first = target || latches[0] || null;
      return {
        mode: "latched", notice, ended,
        subject: first && LATCH_SUBJECTS.has(first.subject) ? first.subject : "unreadable",
        reason: code(first ? first.reason : status.latch_reason),
        windowId: target ? target.window_id : null,
        more: Math.max(0, latches.length - 1),
      };
    }
    if (win && win.state === "ended") return { mode: "issuing", notice };
    // Phase 7: "Include exec" only when the server offers it (exec tools + allowance external).
    const tiers = status.allowance === "external"
      ? (status.exec_offered === true ? ["internal", "external", "exec"] : ["internal", "external"])
      : ["internal"];
    if (ended) return { mode: "ended", notice, ended, offered: status.offered === true, tiers, tools: toolsOf(lastEnd.tool_calls) };
    if (status.offered === true) return { mode: "offered", notice, tiers };
    const reasons = Array.isArray(status.unavailable) ? status.unavailable : [];
    return { mode: "unavailable", notice, reason: code(reasons[0]) };
  }

  // --- the badge -----------------------------------------------------------------------

  function create(options = {}) {
    const doc = options.doc || root.document;
    const win = options.win || root;
    const tr = options.t || ((key, vars) => (typeof root.t === "function" ? root.t(key, vars) : key));
    const getJSON = options.fetch || ((url) => root.fetch(url, { headers: { Accept: "application/json" }, cache: "no-store" }));
    const post = options.post || ((url, body) => {
      const send = typeof root.stiraFetch === "function" ? root.stiraFetch : root.fetch;
      return send(url, { method: "POST", headers: { "Content-Type": "application/json", Accept: "application/json" }, body: JSON.stringify(body || {}) });
    });
    const setTimer = options.setTimer || ((fn, ms) => win.setTimeout(fn, ms));
    const clearTimer = options.clearTimer || ((id) => win.clearTimeout(id));
    const storage = options.storage !== undefined ? options.storage : (() => { try { return win.localStorage; } catch (e) { return null; } })();
    const slots = options.slots || [...doc.querySelectorAll(".yolo-slot")];
    const confirmYolo = options.confirmYolo || ((answer) => (root.StiraHostConfirm ? root.StiraHostConfirm.yolo(answer) : Promise.resolve({ code: "host_channel_unavailable" })));
    const chooseTier = options.chooseTier || ((tiers, channel, found) => chooser(doc, tr, tiers, channel, found));
    const onLine = options.onLine || null;

    let status = null, failed = false, fails = 0, idle = 0, timer = null, reading = false, again = false;
    let targets; // #450: the last status.targets read (kept across an answer without it)
    let busy = false, stopping = false, notice = null, noticeTimer = null, own = null, command = null;
    let acked = null, lastSig = "", seen = null, structure = "", announced = "", watched = null;
    // One counter for sending and arriving (headers: the moment the server answered). The mark is
    // the drawn answer's pair; it moves only when an answer is drawn (see the header comment).
    let order = 0, drawnSent = 0, drawnArrived = 0;
    const newerRead = (sentAt) => sentAt > drawnArrived;
    const newerStop = (arrivedAt) => !(drawnSent > arrivedAt);
    function mark(sentAt, arrivedAt) { drawnSent = sentAt; drawnArrived = arrivedAt; }
    try { acked = storage ? storage.getItem(ACK_KEY) : null; } catch (e) { acked = null; }
    const parts = slots.map((slot) => ({ slot, nodes: null }));

    function setNotice(key, vars, sticky) {
      notice = key ? { key, vars: vars || null } : null;
      if (noticeTimer !== null) { clearTimer(noticeTimer); noticeTimer = null; }
      if (notice && !sticky) noticeTimer = setTimer(() => { notice = null; noticeTimer = null; draw(); }, 8000);
    }

    function local() {
      return { failed, notice: notice ? tr(notice.key, notice.vars || undefined) : null, own, command, acked };
    }

    // --- DOM -----------------------------------------------------------------------
    function el(tag, cls, text) {
      const n = doc.createElement(tag);
      if (cls) n.className = cls;
      if (text !== undefined && text !== null) n.textContent = text;
      return n;
    }
    function button(cls, label, onClick, aria) {
      const b = el("button", `btn yolo-btn ${cls}`, label);
      b.type = "button";
      if (aria) b.setAttribute("aria-label", aria);
      b.addEventListener("click", onClick);
      return b;
    }
    const setText = (node, text) => { if (node && node.textContent !== text) node.textContent = text; };
    const setAttr = (node, name, value) => { if (node && node.getAttribute(name) !== value) node.setAttribute(name, value); };

    function structureOf(v) {
      switch (v.mode) {
        case "window": return `window:${v.paused}:${v.tier}:${Boolean(v.soon)}:${v.resumeIn !== null}:${Boolean(v.ext)}:${v.exec}:${Boolean(v.execCap)}:${Boolean(v.tools)}`;
        case "pending": return `pending:${v.channel}:${v.own}:${Boolean(v.command)}`;
        case "latched": return `latched:${Boolean(v.windowId)}:${Boolean(v.ended)}`;
        case "ended": return `ended:${v.ended.abnormal}:${Boolean(v.tools)}`;
        default: return v.mode;
      }
    }

    function build(v) {
      const box = el("div", "yolo-badge");
      box.dataset.mode = v.mode;
      const n = { box };
      const row = el("div", "yolo-row");
      box.append(row);
      const stopButton = () => {
        n.stop = button("btn-danger yolo-stop", tr("yolo.stop"), stop);
        n.stop.dataset.focus = "1";
        return n.stop;
      };
      switch (v.mode) {
        case "offered":
          n.start = button("btn-secondary yolo-start", tr("yolo.start"), press);
          n.start.dataset.focus = "1";
          row.append(n.start);
          break;
        case "window": {
          box.dataset.state = v.paused ? "paused" : "active";
          n.tier = el("span", "yolo-tag");
          n.time = el("span", "yolo-time");
          n.count = el("span", "yolo-count");
          if (v.ext) n.ext = el("span", "yolo-count yolo-ext");
          if (v.exec) n.execTag = el("span", "yolo-tag yolo-exec-tag");
          if (v.execCap) n.execCount = el("span", "yolo-count yolo-exec");
          row.append(...[n.tier, n.execTag, n.time, n.count, n.ext, n.execCount].filter(Boolean), stopButton());
          if (v.paused) { n.pause = el("p", "yolo-sub yolo-pause"); box.append(n.pause); }
          // #448: every tool call of the window, and that the cap counts automatic ones only.
          if (v.tools) { n.tools = el("p", "yolo-sub yolo-tools"); box.append(n.tools); }
          if (v.tier === "external") {
            n.note = el("p", "yolo-sub yolo-note");
            n.note.hidden = true;
            box.append(n.note);
          }
          if (v.exec) {
            n.execNote = el("p", "yolo-sub yolo-exec-note");
            n.execNote.hidden = true;
            box.append(n.execNote);
          }
          if (v.soon) { n.soon = el("p", "yolo-sub yolo-soon"); box.append(n.soon); }
          break;
        }
        case "pending":
          n.text = el("span", "yolo-text");
          row.append(n.text);
          if (!v.own) {
            n.cancel = button("btn-secondary yolo-cancel", tr("yolo.cancel_request"), cancelRequest);
            row.append(n.cancel);
          }
          row.append(stopButton());
          if (v.command) { n.command = el("p", "yolo-sub yolo-command"); box.append(n.command); }
          break;
        case "issuing":
          row.append(el("span", "yolo-text", tr("yolo.issuing")), stopButton());
          break;
        case "unreadable":
          row.append(el("span", "yolo-text", tr("yolo.status_unreadable")), stopButton());
          break;
        case "latched":
          box.dataset.state = "latched";
          n.text = el("span", "yolo-text");
          row.append(n.text);
          n.reason = el("p", "yolo-sub yolo-reason");
          if (v.windowId) {
            n.unlatch = button("btn-secondary yolo-unlatch", tr("yolo.unlatch"), unlatch);
            n.unlatch.dataset.focus = "1";
            row.append(n.unlatch);
          } else box.append(el("p", "yolo-sub", tr("yolo.unlatch_files")));
          box.append(n.reason);
          if (v.ended) {
            n.ended = el("p", "yolo-sub yolo-ended");
            box.append(n.ended);
          }
          break;
        case "ended":
          box.dataset.state = v.ended.abnormal ? "abnormal" : "normal";
          n.ended = el("span", "yolo-text yolo-ended");
          // The visible word is the name (axe label-content-name-mismatch); the end reason
          // next to it says what is acknowledged.
          n.ack = button("btn-secondary yolo-ack", tr("yolo.ack"), ack);
          n.ack.dataset.focus = "1";
          row.append(n.ended, n.ack);
          if (v.tools) { n.tools = el("p", "yolo-sub yolo-tools"); box.append(n.tools); }
          break;
        case "unavailable":
          n.text = el("span", "yolo-text yolo-muted");
          row.append(n.text);
          break;
        default:
          break;
      }
      n.notice = el("p", "yolo-sub yolo-notice");
      n.notice.hidden = true;
      box.append(n.notice);
      return n;
    }

    function fill(n, v) {
      if (n.notice) {
        setText(n.notice, v.notice || "");
        if (n.notice.hidden !== !v.notice) n.notice.hidden = !v.notice;
      }
      switch (v.mode) {
        case "window": {
          setText(n.tier, tr("yolo.badge_tier", { tier: tr(`yolo.tier_${v.tier}`) }));
          setText(n.time, v.remaining === null ? "" : remainingText(v.remaining, tr));
          setAttr(n.time, "title", tr("yolo.until", { time: clock(v.endsAt) }));
          setText(n.count, v.limit === null ? "" : tr("yolo.count", { used: v.used, limit: v.limit }));
          if (n.ext) setText(n.ext, tr("yolo.count_external", { used: v.ext.used, limit: v.ext.limit }));
          if (n.note) {
            // Shown only once external tools ask again (sub-cap used up, or 0). No sub-cap
            // in the status answer: nothing is known, so nothing is claimed.
            const back = backToAsk(v.ext);
            setText(n.note, back ? tr("yolo.external_back_to_ask") : "");
            if (n.note.hidden !== !back) n.note.hidden = !back;
          }
          if (n.tools) setText(n.tools, `${toolsText(v.tools, tr)} · ${tr("yolo.tools_cap_note")}`);
          if (n.execTag) setText(n.execTag, tr("yolo.badge_exec"));
          if (n.execCount) setText(n.execCount, tr("yolo.count_exec", { used: v.execCap.used, limit: v.execCap.limit }));
          if (n.execNote) {
            const back = backToAsk(v.execCap);
            setText(n.execNote, back ? tr("yolo.exec_back_to_ask") : "");
            if (n.execNote.hidden !== !back) n.execNote.hidden = !back;
          }
          if (n.pause) setText(n.pause, `${tr(`yolo.pause_${v.pause}`)}${pauseWhyText(v, tr)} · ${v.resumeIn !== null
            ? tr("yolo.resume_in", { sec: v.resumeIn }) : tr("yolo.resume_wait")}`);
          if (n.soon) setText(n.soon, tr("yolo.ending_soon", { m: Math.max(1, Math.ceil((v.remaining || 0) / 60)) }));
          break;
        }
        case "pending":
          setText(n.text, v.channel === "browser"
            ? tr("yolo.pending_browser", { short: v.short })
            : tr(v.hostKind === "command" ? "yolo.pending_command" : "yolo.pending_host", { short: v.short, s: v.left === null ? "?" : v.left }));
          if (n.command) setText(n.command, v.command);
          break;
        case "latched":
          setText(n.text, tr(`yolo.latch_${v.subject}`));
          setText(n.reason, `${tr("yolo.reason", { code: v.reason })}${v.more ? ` (+${v.more})` : ""}`);
          if (n.ended) setText(n.ended, tr("yolo.ended", { reason: tr(`yolo.end_${v.ended.reason}`) }));
          break;
        case "ended":
          setText(n.ended, tr("yolo.ended", { reason: tr(`yolo.end_${v.ended.reason}`) }));
          if (n.tools) setText(n.tools, toolsText(v.tools, tr));
          break;
        case "unavailable":
          setText(n.text, tr("yolo.unavailable", { reason: tr(`yolo.unavailable_${v.reason === "clock_not_measured" || v.reason === "host_channel_unavailable" ? v.reason : "other"}`) }));
          break;
        default:
          break;
      }
      if (n.stop) {
        setText(n.stop, stopping ? tr("yolo.stopping") : tr("yolo.stop"));
        setAttr(n.stop, "aria-busy", stopping ? "true" : "false");
      }
      // The start button is never disabled while its dialog is open (focus returns to it);
      // a second press meanwhile is ignored (``busy``).
    }

    function announce(v) {
      // Transitions only (the remaining time is not read out every poll).
      const key = v.mode === "window" ? `w:${v.id}:${v.paused}:${Boolean(v.soon)}` : `${v.mode}:${v.ended ? v.ended.key : ""}`;
      if (key === announced) return;
      const first = announced === "";
      announced = key;
      if (first) return;
      let text = "";
      if (v.mode === "window" && v.paused) text = `${tr(`yolo.pause_${v.pause}`)}${pauseWhyText(v, tr)}`;
      else if (v.mode === "window" && v.soon) text = tr("yolo.ending_soon", { m: Math.max(1, Math.ceil((v.remaining || 0) / 60)) });
      else if (v.mode === "window") text = tr("yolo.started", { tier: tr(v.exec ? "yolo.tier_exec" : `yolo.tier_${v.tier}`), time: clock(v.endsAt) });
      else if (v.ended) text = tr("yolo.ended", { reason: tr(`yolo.end_${v.ended.reason}`) });
      if (!text) return;
      for (const p of parts) {
        let live = p.slot.querySelector(".yolo-live");
        if (!live) {
          live = el("p", "visually-hidden yolo-live");
          live.setAttribute("role", "status");
          live.setAttribute("aria-live", "polite");
          p.slot.append(live);
        }
        live.textContent = text;
      }
    }

    function draw() {
      const v = viewOf(status, local());
      const shape = structureOf(v);
      for (const p of parts) {
        if (v.mode === "none") {
          if (p.nodes) { p.nodes.box.remove(); p.nodes = null; }
          continue;
        }
        if (!p.nodes || shape !== structure) {
          const active = doc.activeElement;
          const hadFocus = Boolean(active && (p.slot.contains(active) || active === doc.body));
          const ownFocus = Boolean(active && p.slot.contains(active));
          const next = build(v);
          if (p.nodes) p.nodes.box.replaceWith(next.box); else p.slot.prepend(next.box);
          p.nodes = next;
          fill(next, v);
          const target = next.box.querySelector("[data-focus]") || next.box.querySelector("button");
          if (target && (ownFocus || (hadFocus && p.wantFocus))) target.focus();
          p.wantFocus = false;
        } else fill(p.nodes, v);
        if (p.slot.hidden) p.slot.hidden = false;
      }
      structure = shape;
      if (v.mode !== "none") announce(v);
      return v;
    }

    // --- log lines -------------------------------------------------------------------
    function lines(data) {
      const block = isObj(data.auto_approvals) ? data.auto_approvals : null;
      const items = block && Array.isArray(block.items) ? block.items : [];
      const id = block && typeof block.window === "string" ? block.window : null;
      const top = items.reduce((m, i) => (isObj(i) && int(i.n) ? Math.max(m, i.n) : m), 0);
      endSummary(data);
      if (seen === null) {
        // The first read: no repeats; one line says how many came before (the turn events
        // keep no line, so a reload would otherwise look as if nothing had been approved).
        seen = { id, n: top };
        const w = isObj(data.window) ? data.window : null;
        if (top > 0 && w && (w.state === "active" || w.state === "paused")) {
          emit(tr("yolo.lines_before", { count: top }), { n: top }, true);
        }
        return;
      }
      if (id !== seen.id) seen = { id, n: 0 };
      const fresh = items.filter((i) => isObj(i) && int(i.n) && i.n > seen.n).sort((a, b) => a.n - b.n);
      for (const item of fresh) {
        const text = lineText(item, tr);
        if (!text) continue;
        // The server keeps the latest 20: more between two reads leaves a gap; say so once.
        if (item.n > seen.n + 1) emit(tr("yolo.lines_skipped", { count: item.n - seen.n - 1 }), { n: item.n - 1 }, true);
        seen.n = item.n;
        emit(text, item);
      }
    }

    // #448: when a window this page watched ends, one line says what ran in it (counts only).
    function endSummary(data) {
      const w = isObj(data.window) ? data.window : null;
      if (w && (w.state === "active" || w.state === "paused") && typeof w.id === "string") watched = w.id;
      const end = isObj(data.last_end) ? data.last_end : null;
      if (!end || !watched || end.id !== watched) return;
      watched = null;
      const tools = toolsOf(end.tool_calls);
      if (!tools) return;
      const id = SHORT.test(end.id) ? end.id : "?";
      const turns = int(end.turns) ? end.turns : 0;
      emit(tr("yolo.tools_line", { id, turns, tools: toolsText(tools, tr) }), { n: 0 }, true);
    }

    function emit(text, item, summary) {
      const log = doc.getElementById("conversation");
      if (log && log.getAttribute("role") === "log") {
        const nearBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 80;
        const line = el("p", summary ? "yolo-line yolo-line-summary" : "yolo-line", text);
        line.dataset.yoloN = String(item.n);
        log.append(line);
        if (nearBottom) log.scrollTop = log.scrollHeight;
      }
      if (typeof onLine === "function") onLine(text, item);
      try {
        doc.dispatchEvent(new win.CustomEvent("stira:yolo-line", { detail: { text, n: item.n } }));
      } catch (e) { /* no CustomEvent: the chat log line is enough */ }
    }

    // --- reading / polling -----------------------------------------------------------
    function needsPoll() {
      // #601 E13: a failed read is retried (with the back-off) whatever the older answer said.
      if (failed) return true;
      if (!status) return false;
      const v = viewOf(status, local());
      return v.mode === "window" || v.mode === "pending" || v.mode === "issuing";
    }
    function delay() {
      if (fails > 0) return Math.min(POLL.active * 2 ** Math.min(fails, 8), POLL.backoffMax);
      const v = viewOf(status, local());
      if (v.mode === "pending" || v.mode === "issuing") return POLL.pending;
      return Math.min(POLL.active * 2 ** Math.floor(idle / 3), POLL.activeMax);
    }
    function schedule() {
      if (timer !== null) { clearTimer(timer); timer = null; }
      if (doc.hidden || !needsPoll()) return;
      timer = setTimer(() => { timer = null; void read(); }, delay());
    }

    // The clocks (remaining_sec, resume_in_sec, expires_in) change on every read: they are
    // not a change of state, or the poll would never widen to 15 s.
    const still = (o, drop) => (isObj(o) ? Object.fromEntries(Object.entries(o).filter(([k]) => !drop.includes(k))) : o);
    function accept(data) {
      const items = data.auto_approvals && Array.isArray(data.auto_approvals.items) ? data.auto_approvals.items : [];
      const last = items.length && isObj(items[items.length - 1]) ? items[items.length - 1].n : null;
      const sig = JSON.stringify([still(data.window, ["remaining_sec", "resume_in_sec"]),
        still(data.request, ["expires_in"]), data.latched, data.last_end, data.offered, items.length, last]);
      idle = sig === lastSig ? idle + 1 : 0;
      lastSig = sig;
      status = data;
      if (Object.prototype.hasOwnProperty.call(data, "targets")) targets = data.targets;
      failed = false;
      fails = 0;
      lines(data);
      const req = isObj(data.request) ? data.request : null;
      if (own && (!req || req.id !== own || req.state !== "pending")) own = null;
      if (command && (!req || req.state !== "pending")) command = null;
    }

    async function read() {
      if (reading) { again = true; return status; }
      reading = true;
      const sentAt = ++order;
      try {
        const response = await getJSON("/api/yolo/status");
        const arrivedAt = ++order;
        if (!newerRead(sentAt)) return status; // answered before an answer already drawn (Stop's)
        if (response.status === 404) { mark(sentAt, arrivedAt); status = null; failed = false; return null; } // no allowance
        if (!response.ok) throw new Error(String(response.status));
        const data = await response.json();
        if (!newerRead(sentAt)) return status; // Stop was drawn while the body was on the way
        mark(sentAt, arrivedAt);
        accept(data);
      } catch (e) {
        if (!newerRead(sentAt)) return status;
        fails += 1;
        failed = true;
      } finally {
        reading = false;
        draw();
        if (again) { again = false; void read(); } else schedule();
      }
      return status;
    }

    function poke() {
      idle = 0;
      if (doc.hidden) return;
      void read();
    }

    // --- actions ---------------------------------------------------------------------
    async function stop() {
      // A narrowing: nothing to confirm, nothing to read first; pressed again it is sent again.
      stopping = true;
      draw();
      const sentAt = ++order;
      try {
        const response = await post("/api/yolo/stop", {});
        const arrivedAt = ++order;
        const data = await response.json().catch(() => null);
        if (response.ok && isObj(data)) {
          if (newerStop(arrivedAt)) { mark(sentAt, arrivedAt); accept(data); }
          setNotice(null);
        }
        else setNotice("yolo.stop_failed", null, true);
      } catch (e) {
        setNotice("yolo.stop_failed", null, true);
      } finally {
        stopping = false;
        for (const p of parts) p.wantFocus = true;
        draw();
        schedule();
      }
    }

    async function press() {
      if (busy) return;
      const v = viewOf(status, local());
      if (v.mode !== "offered") return;
      busy = true;
      setNotice(null);
      draw();
      let tier = null;
      try {
        tier = await chooseTier(v.tiers, status && status.host_channel === "browser" ? "browser" : "host", targets);
      } catch (e) { tier = null; }
      if (tier !== "internal" && tier !== "external" && tier !== "exec") {
        busy = false;
        for (const p of parts) p.wantFocus = true;
        draw();
        return;
      }
      try {
        // Phase 7: "Include exec" is tier external plus ``exec: true`` (only sent then).
        const body = tier === "exec" ? { tier: "external", exec: true } : { tier };
        // ADR 0014 phase 1 (#451): the dialog's 「30 分ごとに中間報告する」 tick (only sent when
        // the dialog offered it).
        if (typeof chooserProgress === "boolean") body.progress = chooserProgress;
        const response = await post("/api/yolo/request", body);
        const data = await response.json().catch(() => ({}));
        if (response.status !== 202 || !isObj(data) || !isObj(data.request)) {
          const why = code(data && data.code);
          setNotice(REFUSALS.has(why) ? `yolo.code_${why}` : "yolo.code_other");
          return;
        }
        if (isObj(data.confirm)) {
          own = HEX32.test(data.request.id) ? data.request.id : null;
          void read();
          const outcome = await confirmYolo(data);
          own = null;
          if (outcome === "cancelled") setNotice("yolo.cancelled");
          else if (outcome !== "confirmed") {
            const why = isObj(outcome) ? code(outcome.code) : "host_operation_unknown";
            setNotice(`host.code_${why}`, null, true);
          }
        } else {
          command = typeof data.command === "string" && COMMAND.test(data.command) ? data.command : null;
        }
      } catch (e) {
        setNotice("yolo.code_other");
      } finally {
        busy = false;
        for (const p of parts) p.wantFocus = true;
        await read();
      }
    }

    async function cancelRequest() {
      const req = status && isObj(status.request) ? status.request : null;
      try {
        await post("/api/yolo/request/cancel", req && HEX32.test(req.id) ? { request_id: req.id } : {});
        setNotice("yolo.cancelled");
      } catch (e) { /* the request also ends by its expiry */ }
      for (const p of parts) p.wantFocus = true;
      await read();
    }

    async function unlatch() {
      const v = viewOf(status, local());
      if (v.mode !== "latched" || !v.windowId) return;
      // #594: the end shown next to Unlatch is acknowledged by a successful Unlatch (the person
      // confirmed it), so the start button comes back at once instead of one more OK.
      const endShown = v.ended ? v.ended.key : null;
      try {
        // A confirmed operation: stiraFetch answers the 428 with the shared dialog (or the
        // host channel's notice) and re-sends once; the outcome is in the response.
        const response = await post("/api/yolo/unlatch", { window_id: v.windowId });
        const data = await response.json().catch(() => ({}));
        if (response.ok) {
          setNotice("yolo.unlatched");
          if (endShown) {
            acked = endShown;
            try { if (storage) storage.setItem(ACK_KEY, endShown); } catch (e) { /* this page only */ }
          }
        } else {
          const why = code(data && data.code);
          const known = why === "stale_operation" ? "yolo.code_stale_unlatch"
            : why === "yolo_not_latched" || why === "yolo_busy" ? `yolo.code_${why}`
              : why.startsWith("host_") || why === "tool_call_running" ? `host.code_${why}` : "yolo.code_other";
          setNotice(known, null, true);
        }
      } catch (e) {
        setNotice("yolo.code_other", null, true);
      }
      for (const p of parts) p.wantFocus = true;
      await read();
    }

    function ack() {
      const v = viewOf(status, local());
      const key = v.ended ? v.ended.key : null;
      if (!key) return;
      acked = key;
      try { if (storage) storage.setItem(ACK_KEY, key); } catch (e) { /* this page only */ }
      for (const p of parts) p.wantFocus = true;
      draw();
    }

    function start() {
      if (doc.addEventListener) {
        doc.addEventListener("visibilitychange", () => {
          if (doc.hidden) { if (timer !== null) { clearTimer(timer); timer = null; } } else poke();
        });
      }
      return read();
    }

    return { start, read, poke, stop, press, ack, unlatch, cancelRequest, view: () => viewOf(status, local()) };
  }

  // The tier dialog of §1a step 1: the §12 text, the tier choice, Cancel (the default) and
  // "Next". -> "internal" | "external" | "exec" | null. The confirmation itself is the shared
  // dialog (phase 7: a third checkbox for exec).
  let chooserDialog = null;
  let chooserProgress = null; // the last dialog's progress-report tick (null: not offered)
  function chooser(doc, tr, tiers, channel, targets) {
    chooserProgress = null;
    if (chooserDialog && chooserDialog.open) return Promise.resolve(null);
    const node = (tag, attrs = {}, text) => {
      const n = doc.createElement(tag);
      for (const [k, v] of Object.entries(attrs)) { if (k === "className") n.className = v; else n.setAttribute(k, v); }
      if (text !== undefined) n.textContent = text;
      return n;
    };
    if (chooserDialog) chooserDialog.remove();
    const dialog = node("dialog", { id: "yolo-start-dialog", className: "stira-confirm yolo-dialog", "aria-labelledby": "yolo-start-title", "aria-describedby": "yolo-start-text" });
    const form = node("form", { method: "dialog" });
    const title = node("h2", { id: "yolo-start-title" }, tr("yolo.start_title"));
    const text = node("p", { id: "yolo-start-text", className: "stira-confirm-intro" },
      tr(channel === "browser" ? "yolo.text_internal_browser" : "yolo.text_internal_host"));
    const extra = node("p", { id: "yolo-start-external", className: "stira-confirm-intro yolo-external-text" }, tr("yolo.text_external"));
    extra.hidden = true;
    // Phase 7: exec includes external (both texts are shown) and says what commands can do.
    const execText = node("p", { id: "yolo-start-exec", className: "stira-confirm-intro yolo-exec-text" }, tr("yolo.text_exec"));
    execText.hidden = true;
    const field = node("fieldset", { className: "yolo-tiers" });
    field.append(node("legend", {}, tr("yolo.tier_label")));
    const radios = tiers.map((tier, index) => {
      const id = `yolo-tier-${tier}`;
      const input = node("input", { type: "radio", name: "yolo-tier", id, value: tier });
      if (index === 0) input.checked = true;
      const label = node("label", { for: id, className: "stira-confirm-check" });
      label.append(input, node("span", {}, tr(`yolo.tier_${tier}`)));
      field.append(label);
      return input;
    });
    // Enter on a choice must not submit the form (implicit submission = Cancel).
    field.addEventListener("keydown", (event) => { if (event.key === "Enter") event.preventDefault(); });
    // #450: the tools this scope could approve (a warning when none; the press still works).
    const found = node("div", { id: "yolo-start-targets", className: "yolo-targets", "aria-live": "polite" });
    const drawTargets = (tier) => {
      const view = targetsView(targets, tier);
      found.replaceChildren();
      found.hidden = view === null;
      if (view === null) return;
      found.dataset.state = view.kind;
      const link = () => node("a", { href: TOOLS_URL, className: "yolo-targets-link" }, tr("yolo.targets_open_settings"));
      if (view.kind === "unknown") {
        found.append(node("p", { className: "yolo-targets-text" }, tr("yolo.targets_unknown")), link());
        return;
      }
      if (view.kind === "none") {
        found.append(node("p", { className: "yolo-targets-warn" }, tr("yolo.targets_none")), link());
      } else {
        const text = tr("yolo.targets_some", { n: view.count, tools: view.shown.join(", ") })
          + (view.more ? tr("yolo.targets_more", { n: view.more }) : "");
        found.append(node("p", { className: "yolo-targets-text" }, text));
      }
      if (view.excluded.length) {
        found.append(node("p", { className: "yolo-targets-excluded" },
          tr("yolo.targets_excluded", { tools: view.excluded.join(", ") })));
      }
      if (view.hidden.length) {
        found.append(node("p", { className: "yolo-targets-excluded yolo-targets-hidden" },
          tr("yolo.targets_hidden", { tools: view.hidden.join(", ") })));
      }
    };
    const sync = () => {
      const chosen = (radios.find((r) => r.checked) || {}).value;
      extra.hidden = chosen !== "external" && chosen !== "exec";
      execText.hidden = chosen !== "exec";
      drawTargets(chosen);
    };
    field.addEventListener("change", sync);
    // ADR 0014 phase 1 (#451): progress reports for this window (only with the report panel
    // loaded; its default is the settings' ``progress.enabled``, off unless set).
    const reports = (doc.defaultView && doc.defaultView.StiraProgress) || root.StiraProgress;
    let progressBox = null;
    let progressRow = null;
    if (reports) {
      progressRow = node("label", { for: "yolo-progress", className: "stira-confirm-check yolo-progress" });
      progressBox = node("input", { type: "checkbox", id: "yolo-progress", "aria-describedby": "yolo-progress-help" });
      progressBox.checked = reports.defaultOn === true;
      progressRow.append(progressBox, node("span", {}, tr("yolo.progress_check", { min: reports.intervalMin || 30 })));
      progressRow.append(node("small", { id: "yolo-progress-help", className: "yolo-progress-help" }, tr("yolo.progress_help", { wait: reports.continueAfterMin || 10, max: reports.maxAuto ?? 3 })));
    }
    const actions = node("div", { className: "dialog-actions" });
    const cancel = node("button", { type: "submit", value: "cancel", className: "btn btn-secondary", id: "yolo-start-cancel" }, tr("yolo.cancel"));
    const next = node("button", { type: "submit", value: "next", className: "btn btn-danger", id: "yolo-start-next" }, tr("yolo.next"));
    actions.append(cancel, next);
    form.append(title, text, extra, execText, field, found, ...(progressRow ? [progressRow] : []), actions);
    sync();
    dialog.append(form);
    dialog.addEventListener("keydown", (event) => {
      if (event.repeat && (event.key === "Enter" || event.key === " ")) event.preventDefault();
    });
    doc.body.append(dialog);
    chooserDialog = dialog;
    const opener = doc.activeElement;
    return new Promise((resolve) => {
      dialog.addEventListener("close", () => {
        const choice = radios.find((r) => r.checked);
        const value = dialog.returnValue === "next" && choice ? choice.value : null;
        chooserProgress = progressBox ? progressBox.checked === true : null;
        if (opener && typeof opener.focus === "function" && opener.isConnected !== false) opener.focus();
        resolve(value === "internal" || value === "external" || value === "exec" ? value : null);
      }, { once: true });
      dialog.returnValue = "";
      try { dialog.showModal(); } catch (e) { resolve(null); return; }
      (radios[0] || cancel).focus();
    });
  }

  // #455: " (安定度 0.31 < 0.35)" after the pause reason, when the server said why.
  function pauseWhyText(v, tr) {
    const words = root.StiraBreakerReasons;
    if (!v.pauseWhy || !words) return "";
    const why = words.describe(String(v.pause || "").replace(/^breaker_/, ""), v.pauseWhy.reasons, v.pauseWhy.inputs, tr);
    return why ? ` (${why})` : "";
  }

  const api = { viewOf, pauseWhyText, lineText, backToAsk, remainingText, targetsView, toolsOf, toolsText, create, clock, instance: null };
  api.poke = () => { if (api.instance) api.instance.poke(); };
  root.StiraYolo = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;

  // Auto-start on a page that has a slot (none without STIRA_YOLO_ALLOW).
  const doc = root.document;
  if (doc && typeof doc.querySelectorAll === "function" && !(typeof module !== "undefined" && module.exports)) {
    const boot = () => {
      if (!doc.querySelector(".yolo-slot") || api.instance) return;
      api.instance = create();
      void api.instance.start();
    };
    if (doc.readyState === "loading") doc.addEventListener("DOMContentLoaded", boot); else boot();
  }
})(typeof globalThis !== "undefined" ? globalThis : this);
