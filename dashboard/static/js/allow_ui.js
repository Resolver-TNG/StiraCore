/* Session allowances: badge, list, revoke and log lines (ADR 0025 §5, §7; PR 2).
 *
 * The chat header and the Stage dialogue edge carry one <div class="allow-slot">. Everything
 * shown comes from GET /api/allowances (the browser holds no policy and computes no class):
 *   allowances -> 「継続許可 N件」 / "N allowances" (a toggle) and its list: tool, class,
 *                 time left, uses, 「取り消し」 per row and 「すべて取り消し」
 *   uses       -> one log line per automatic answer ("↻ 継続許可で承認: file_write · game/ 配下"),
 *                 appended to the chat log (#conversation) and announced as "stira:allow-line"
 *                 (the Stage puts it in its backlog). Lines from before this page loaded are
 *                 not repeated. The class text comes from the live list; an ended allowance's
 *                 line says 「(終了した継続許可)」.
 * The badge is hidden while there is none. Revoke (POST /api/allowances/<id>/revoke) and
 * "Revoke all" (POST /api/allowances/revoke-all) are narrowings: no confirmation.
 *
 * Polling: once at load; while the server offers the buttons (status.enabled) every 15 s when
 * idle and every 3 s while an allowance exists; never in a hidden tab; after a card decision
 * ("stira:approvals"), after a turn (poke) and after this page's own actions. A server that
 * does not offer them (PR 2: always) is read once and never polled again.
 *
 * classText() is shared with the approval cards (approvals.js, Stage approval.js): the same
 * words on the button, in the list and in the log line.
 */
(function (root) {
  "use strict";
  const CODE = /^[a-z0-9_]{1,48}$/;
  const TOOL = /^[a-z][a-z0-9_]{0,47}$/;
  const REF = /^[0-9a-f]{8}$/;
  const ID = /^[0-9a-f]{16,64}$/;
  const KINDS = new Set(["dir_subtree", "host", "host_read", "layer", "tool"]);
  const int = v => (typeof v === "number" && Number.isInteger(v) && v >= 0 ? v : null);
  const str = (v, n = 300) => (typeof v === "string" ? v.slice(0, n) : "");
  const IDLE_MS = 15000, BUSY_MS = 3000;

  function trOf(options) {
    return options.t || ((key, vars) => (typeof root.t === "function" ? root.t(key, vars) : key));
  }

  // The class in words: "game/ 配下", "ワークスペース全体", "docs.python.org へのリクエスト" …
  function targetText(view, t) {
    if (!view || !KINDS.has(view.class_kind)) return t("allow.target_ended");
    const label = str(view.label, 200);
    switch (view.class_kind) {
      case "dir_subtree":
        return view.whole_root === true || !label ? t("allow.target_root") : t("allow.target_dir", { dir: label });
      case "host": return t("allow.target_host", { host: label });
      case "host_read": return t("allow.target_host_read", { host: label });
      case "layer": return t("allow.target_layer", { layer: label });
      default: return t("allow.target_tool");
    }
  }

  // The button's class line: "file_write · game/ 配下 · 最長2時間".
  function classText(tool, view, t) {
    const hours = int(view && view.max_hours) || 2;
    return t("allow.class_line", { tool: TOOL.test(tool) ? tool : "?", target: targetText(view, t), hours });
  }

  function remainingText(seconds, t) {
    const s = int(seconds) ?? 0;
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
    return t("allow.remaining", { time: h ? `${h}:${String(m).padStart(2, "0")}` : `${Math.max(1, m)}m` });
  }

  // GET /api/allowances -> what the badge and the list draw (pure).
  function viewOf(status, t) {
    const list = status && Array.isArray(status.allowances) ? status.allowances : [];
    const rows = [];
    for (const item of list) {
      if (!item || !ID.test(String(item.allow_id)) || !TOOL.test(String(item.tool))) continue;
      rows.push({
        id: item.allow_id,
        ref: item.allow_id.slice(0, 8),
        tool: item.tool,
        text: `${item.tool} · ${targetText(item, t)}`,
        left: remainingText(item.remaining_sec, t),
        uses: t("allow.uses", { n: int(item.uses) ?? 0 }),
      });
    }
    return { enabled: status && status.enabled === true, count: rows.length, rows };
  }

  // One automatic answer -> its log line (the class from the live list, if still there).
  function lineText(use, status, t) {
    const tool = TOOL.test(String(use && use.tool)) ? use.tool : "?";
    const live = (status && Array.isArray(status.allowances) ? status.allowances : [])
      .find(a => a && typeof a.allow_id === "string" && a.allow_id.slice(0, 8) === use.ref);
    const target = live ? targetText(live, t) : t("allow.target_ended");
    return t("allow.line", { tool, target });
  }

  function create(options = {}) {
    const doc = options.document || root.document;
    const win = options.window || root;
    const tr = trOf(options);
    const getJSON = options.getJSON || (async url => {
      const response = await fetch(url, { cache: "no-store", headers: { Accept: "application/json" } });
      if (!response.ok) throw new Error(String(response.status));
      return response.json();
    });
    const post = options.post || (url => {
      const send = typeof root.stiraFetch === "function" ? root.stiraFetch : root.fetch;
      return send(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: "{}" });
    });
    const slots = options.slots || [...doc.querySelectorAll(".allow-slot")];
    const onLine = options.onLine || null;
    let status = null, lastSeq = null, timer = null, busy = false, stopped = false;
    const open = new Set(); // slots whose list is open

    function el(tag, className, text) {
      const node = doc.createElement(tag);
      if (className) node.className = className;
      if (text !== undefined && text !== null) node.textContent = text;
      return node;
    }

    function emit(text, summary) {
      const log = doc.getElementById("conversation");
      if (log && log.getAttribute("role") === "log") {
        const nearBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 80;
        log.append(el("p", summary ? "allow-line allow-line-summary" : "allow-line", text));
        if (nearBottom) log.scrollTop = log.scrollHeight;
      }
      if (typeof onLine === "function") onLine(text);
      try {
        doc.dispatchEvent(new win.CustomEvent("stira:allow-line", { detail: { text } }));
      } catch (e) { /* no CustomEvent: the chat log line is enough */ }
    }

    function drawSlot(slot, index) {
      const view = viewOf(status, tr);
      slot.hidden = view.count === 0;
      if (view.count === 0) { slot.replaceChildren(); open.delete(slot); return; }
      const listId = `allow-list-${index}`;
      const box = el("div", "allow-badge");
      const toggle = el("button", "btn btn-secondary allow-toggle", tr("allow.badge", { n: view.count }));
      toggle.type = "button";
      toggle.setAttribute("aria-expanded", open.has(slot) ? "true" : "false");
      toggle.setAttribute("aria-controls", listId);
      toggle.addEventListener("click", () => {
        if (open.has(slot)) open.delete(slot); else open.add(slot);
        draw();
        slot.querySelector(".allow-toggle")?.focus();
      });
      const panel = el("div", "allow-panel");
      panel.id = listId;
      panel.hidden = !open.has(slot);
      const list = el("ul", "allow-list");
      for (const row of view.rows) {
        const li = el("li", "allow-row");
        li.dataset.allowRef = row.ref;
        const text = el("span", "allow-text", row.text);
        const meta = el("span", "allow-meta", `${row.left} · ${row.uses}`);
        const revoke = el("button", "btn btn-secondary allow-revoke", tr("allow.revoke"));
        revoke.type = "button";
        revoke.setAttribute("aria-label", tr("allow.revoke_label", { what: row.text }));
        revoke.addEventListener("click", () => void act(`/api/allowances/${encodeURIComponent(row.id)}/revoke`, "allow.revoked"));
        li.append(text, meta, revoke);
        list.append(li);
      }
      const all = el("button", "btn btn-danger allow-revoke-all", tr("allow.revoke_all"));
      all.type = "button";
      all.addEventListener("click", () => void act("/api/allowances/revoke-all", "allow.revoked_all"));
      const note = el("p", "allow-note", tr("allow.list_note"));
      panel.append(list, all, note);
      box.append(toggle, panel);
      slot.replaceChildren(box);
    }

    function draw() {
      slots.forEach((slot, index) => drawSlot(slot, index));
    }

    function lines(previous) {
      const uses = status && Array.isArray(status.uses) ? status.uses : [];
      const seq = int(status && status.seq);
      if (seq === null) return;
      if (lastSeq === null) { lastSeq = seq; return; } // not before this page loaded
      const fresh = uses.filter(u => u && int(u.seq) !== null && u.seq > lastSeq && REF.test(String(u.ref)));
      const missed = seq - lastSeq - fresh.length;
      if (missed > 0) emit(tr("allow.more_lines", { n: missed }), true);
      for (const use of fresh) emit(lineText(use, status.allowances?.length ? status : previous, tr), false);
      lastSeq = seq;
    }

    function schedule() {
      if (timer) root.clearTimeout?.(timer);
      timer = null;
      if (stopped || !status || status.enabled !== true) return; // PR 2: read once
      if (doc.visibilityState === "hidden") return;
      const busyNow = Array.isArray(status.allowances) && status.allowances.length > 0;
      timer = root.setTimeout?.(() => void read(), busyNow ? BUSY_MS : IDLE_MS) ?? null;
    }

    async function read() {
      if (busy) return status;
      busy = true;
      try {
        const next = await getJSON("/api/allowances");
        const previous = status;
        status = next && typeof next === "object" ? next : null;
        lines(previous);
        draw();
      } catch {
        /* the badge keeps its last drawing; the next poll retries */
      } finally {
        busy = false;
        schedule();
      }
      return status;
    }

    async function act(url, doneKey) {
      try {
        const response = await post(url);
        const data = await response.json().catch(() => null);
        if (data && typeof data === "object" && Array.isArray(data.allowances)) {
          const previous = status;
          status = data;
          lines(previous);
        }
        emit(tr(doneKey), true);
      } catch {
        emit(tr("allow.revoke_failed"), true);
      }
      draw();
      await read();
    }

    function start() {
      doc.addEventListener?.("stira:approvals", () => void read());
      doc.addEventListener?.("stira:allow-result", event => {
        const text = event && event.detail && typeof event.detail.text === "string" ? event.detail.text : "";
        if (text) emit(text, true);
        void read();
      });
      doc.addEventListener?.("visibilitychange", () => { if (doc.visibilityState !== "hidden") void read(); });
      return read();
    }

    return { start, read, line: text => emit(text, true), poke: () => void read(), stop() { stopped = true; if (timer) root.clearTimeout?.(timer); }, get status() { return status; } };
  }

  // Make Always (ADR 0025 §9): after the press, wait for the call's terminal result
  // (GET /api/config/hub/card/<id>: "waiting" -> "ready"), then the hub's own confirmation
  // (the typed tool name, Q7) in the shared dialog, then one card-origin hub save with the
  // one-time save token. A refusal (stale_confirmation …) is said and the person may confirm
  // anew (a new token). Closing the dialog saves nothing. Lines go to the chat log / Stage.
  const TERMINAL_KEYS = { committed: "allow.always_ran", failed: "allow.always_failed", void: "allow.always_void" };
  async function makeAlways(options = {}) {
    const doc = options.document || root.document;
    const tr = trOf(options);
    const tool = TOOL.test(String(options.tool)) ? options.tool : "?";
    const id = typeof options.requestId === "string" ? options.requestId : "";
    const getJSON = options.getJSON || (async url => {
      const response = await fetch(url, { cache: "no-store", headers: { Accept: "application/json" } });
      return response.json();
    });
    const send = options.post || ((url, body) => (typeof root.stiraFetch === "function" ? root.stiraFetch : root.fetch)(url, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    }));
    const confirm = options.confirm || (root.StiraConfirm && root.StiraConfirm.open);
    const wait = options.wait || (ms => new Promise(r => root.setTimeout(r, ms)));
    const say = options.say || (text => {
      if (api.instance && typeof api.instance.line === "function") api.instance.line(text);
      else if (doc) doc.dispatchEvent?.(new root.CustomEvent("stira:allow-result", { detail: { text } }));
    });
    const deadline = Date.now() + (options.maxWaitMs || 15 * 60 * 1000);
    let view = null;
    for (;;) {
      try { view = await getJSON(`/api/config/hub/card/${encodeURIComponent(id)}`); } catch { view = null; }
      if (view && view.state !== "waiting") break;
      if (Date.now() > deadline) return "timeout";
      await wait(options.pollMs || 1500);
    }
    if (!view || view.state === "gone") { say(tr("allow.always_gone")); return "gone"; }
    if (view.state === "not_ask") { say(tr("allow.always_not_ask", { tool })); return "not_ask"; }
    if (view.breaker_ok === false) { say(tr("allow.always_breaker")); return "breaker"; }
    if (typeof confirm !== "function") return "no_dialog";
    const names = Array.isArray(view.confirm_names) ? view.confirm_names.filter(n => TOOL.test(String(n))) : [];
    const dangers = Array.isArray(view.needs_confirmation) ? view.needs_confirmation.filter(c => CODE.test(String(c))) : [];
    const ok = await confirm({
      title: tr("allow.always_title", { tool }),
      intro: tr(TERMINAL_KEYS[view.terminal] || "allow.always_void"),
      items: [tr("allow.always_meaning")],
      confirmLabel: tr("allow.always_confirm"),
      cancelLabel: tr("allow.always_close"),
      tone: "warn",
      names: names.length ? { expected: names, prompt: tr("allow.always_type", { tool: names.join(", ") }), parse: text => text.split(/[\s,]+/).filter(Boolean).sort() } : undefined,
      checks: [tr("allow.always_ack")],
    });
    if (!ok) return "closed";
    let response, data = {};
    try {
      response = await send("/api/config/hub", {
        source: "approval_card", request_id: id, save_token: view.token, base_version: view.base_version,
        acknowledged_dangers: dangers, confirm_always: names,
      });
      data = await response.json().catch(() => ({}));
    } catch {
      say(tr("allow.always_failed_save", { error: "network" }));
      return "failed";
    }
    if (response.ok) { say(tr("allow.always_saved", { tool })); return "saved"; }
    if (data.code === "stale_confirmation" || data.code === "op_token_used" || data.error_code === "conflict") {
      say(tr("allow.always_stale"));
      return options.again === false ? "stale" : makeAlways({ ...options, again: false });
    }
    say(tr("allow.always_failed_save", { error: typeof data.error === "string" ? data.error.slice(0, 200) : String(response.status) }));
    return "failed";
  }

  const api = { classText, targetText, viewOf, lineText, remainingText, create, makeAlways, instance: null, CODE };
  api.poke = () => { if (api.instance) api.instance.poke(); };
  root.StiraAllow = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;

  const doc = root.document;
  if (doc && typeof doc.querySelectorAll === "function" && !(typeof module !== "undefined" && module.exports)) {
    const boot = () => {
      if (!doc.querySelector(".allow-slot") || api.instance) return;
      api.instance = create();
      void api.instance.start();
    };
    if (doc.readyState === "loading") doc.addEventListener("DOMContentLoaded", boot); else boot();
  }
})(typeof globalThis !== "undefined" ? globalThis : this);
