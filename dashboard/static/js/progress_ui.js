/*
 * Progress reports (ADR 0014 phase 1, #451) — the report panel on the chat page and the Stage.
 *
 * Only with STIRA_YOLO_ALLOW (the templates include it with yolo_ui). Reads
 * GET /api/progress/current (the server builds it from the turn events, so a reload restores a
 * pending report) and draws it into #progress-slot: when it was made, the agent's short note
 * (text only, already masked and cut by the server), the harness facts (time, tool calls,
 * tokens, the YOLO window's time and count) and three answers:
 *   続ける / Continue      -> the page sends "続けてください。" with progress_reply (a new turn)
 *   方向を直す / Redirect  -> a one-line field (<= 300); sends "方向修正: …" with progress_reply
 *   ここで止める / Stop    -> POST /api/progress/<id>/stop (the YOLO window ends; never refused)
 * and, while the server will continue by itself, the countdown with 自動で続けない / Don't
 * continue automatically (POST /api/progress/<id>/hold). The page's own sender
 * (window.StiraProgressSend, set by chat.js / talk.js) sends the answers so its log shows the
 * turn; without one the answer is posted directly.
 *
 * Polling: 3 s while a report waits (the countdown itself ticks every second locally), 15 s
 * otherwise, paused in a hidden tab (poll.js). The tab title gets "● " while a report waits.
 */
(function (root) {
  "use strict";
  const doc = root.document;
  const ID = /^[0-9a-f]{32}$/;
  const tr = (key, vars) => (typeof root.t === "function" ? root.t(key, vars) : key);
  const lang = () => (doc.documentElement.getAttribute("lang") === "en" ? "en" : "ja");

  function duration(sec) {
    const s = Math.max(0, Math.round(Number(sec) || 0));
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
    if (h) return tr("progress.dur_hm", { h, m });
    return tr("progress.dur_m", { m: Math.max(m, s > 0 && m === 0 ? 1 : m) });
  }

  function clockText(sec) {
    const s = Math.max(0, Math.ceil(sec));
    return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
  }

  function timeOf(ts) {
    const date = new Date(Number(ts) * 1000);
    if (Number.isNaN(date.getTime())) return "";
    return `${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}`;
  }

  // -> [label, value] rows; numbers only, names only (the server allow-lists them).
  function factRows(report) {
    const rows = [[tr("progress.fact_elapsed"), duration(report.elapsed_sec)]];
    const tools = report.tools && typeof report.tools === "object" ? report.tools : {};
    let calls = 0, failed = 0;
    for (const value of Object.values(tools)) {
      if (Array.isArray(value)) { calls += Number(value[0]) || 0; failed += Number(value[1]) || 0; }
    }
    const names = Object.keys(tools).slice(0, 4).join(", ");
    rows.push([tr("progress.fact_tools"), tr(failed ? "progress.tools_failed" : "progress.tools", { n: calls, failed, names: names || "—" })]);
    const usage = report.usage || {};
    if (Number.isFinite(usage.input) || Number.isFinite(usage.output)) {
      rows.push([tr("progress.fact_tokens"), tr("progress.tokens", { input: usage.input ?? "—", output: usage.output ?? "—" })]);
    }
    const yolo = report.yolo;
    if (yolo && Number.isFinite(yolo.remaining_sec)) {
      const count = Number.isFinite(yolo.used) && Number.isFinite(yolo.limit) ? `${yolo.used}/${yolo.limit}` : "—";
      rows.push([tr("progress.fact_yolo"), tr("progress.yolo", { left: duration(yolo.remaining_sec), count })]);
    }
    return rows;
  }

  // What the server will do without an answer. -> { key, vars, hold } | null
  // #535: the report's turn wrote artifacts (the server's count) -> the artifacts page
  // filtered to that turn (a same-origin path with the opaque turn id only), else null.
  function artifactLink(report) {
    const n = report && report.artifacts;
    if (!Number.isInteger(n) || n <= 0 || typeof report.turn_id !== "string" || !ID.test(report.turn_id)) return null;
    return { href: `/artifacts?turn=${report.turn_id}`, n };
  }

  function autoState(body) {
    const report = body && body.report;
    if (!report || !report.pending) return null;
    const max = body.settings && Number(body.settings.max_auto_continues);
    if (report.auto && Number.isFinite(report.auto.due_wall)) {
      return { key: "progress.auto_in", due: report.auto.due_wall, vars: { n: report.auto.n, max: report.auto.max }, hold: true };
    }
    if (max > 0 && body.window_open && Number(body.consecutive_auto) >= max) {
      return { key: "progress.auto_limit", vars: { max }, hold: false };
    }
    return { key: "progress.waiting", vars: {}, hold: false };
  }

  function create(options = {}) {
    const slot = options.slot || doc.getElementById("progress-slot");
    if (!slot) return null;
    const fetchJSON = options.fetch || ((url) => root.fetch(url, { cache: "no-store", headers: { Accept: "application/json" } }));
    const post = options.post || ((url, body, idempotent) => (root.stiraFetch || root.fetch)(url, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}),
      ...(idempotent ? { idempotent: true } : {}),
    }));
    const now = options.now || (() => Date.now() / 1000);
    const baseTitle = doc.title.replace(/^● /, "");
    const view = { body: null, sig: "", busy: false, error: "", redirect: false, seen: "" };
    let ticker = null;

    function node(tag, cls, text) {
      const n = doc.createElement(tag);
      if (cls) n.className = cls;
      if (text !== undefined) n.textContent = text;
      return n;
    }

    function button(id, key, cls, onClick) {
      const b = node("button", `btn ${cls}`, tr(key));
      b.type = "button";
      b.id = id;
      b.disabled = view.busy;
      b.addEventListener("click", onClick);
      return b;
    }

    async function send(answer, text) {
      const report = view.body && view.body.report;
      if (!report || view.busy) return;
      const message = answer === "continue" ? tr("progress.continue_message") : `${tr("progress.redirect_prefix")}${text}`;
      const reply = { report_id: report.report_id, answer };
      view.busy = true;
      view.error = "";
      render(true);
      try {
        const sender = root.StiraProgressSend;
        if (typeof sender === "function") {
          const outcome = await sender(message, reply);
          if (outcome === false) throw new Error(tr("progress.send_busy"));
        } else {
          const response = await post("/api/chat", { message, progress_reply: reply }, true);
          if (!response.ok) throw new Error(String(response.status));
          void response.text().catch(() => ""); // the turn runs on; the page polls it
        }
        view.redirect = false;
      } catch (error) {
        view.error = tr("progress.send_failed", { error: (error && error.message) || "" });
      } finally {
        view.busy = false;
        await refresh();
      }
    }

    async function action(kind) {
      const report = view.body && view.body.report;
      if (!report || view.busy) return;
      view.busy = true;
      view.error = "";
      render(true);
      try {
        const response = await post(`/api/progress/${report.report_id}/${kind}`, {});
        if (!response.ok) throw new Error(String(response.status));
        const data = await response.json().catch(() => ({}));
        if (kind === "stop") {
          if (root.StiraYolo && typeof root.StiraYolo.poke === "function") root.StiraYolo.poke();
          if (data.yolo_stopped === false) view.error = tr("progress.stop_window_failed");
        }
      } catch (error) {
        view.error = tr("progress.action_failed", { error: (error && error.message) || "" });
      } finally {
        view.busy = false;
        await refresh();
      }
    }

    // The countdown ticks every second: only its text changes (the button and the keyboard
    // focus stay where they are).
    function renderAuto(box, body) {
      const state = autoState(body);
      const text = !state ? "" : state.due !== undefined
        ? tr(state.key, { ...state.vars, time: clockText(state.due - now()) })
        : tr(state.key, state.vars);
      let span = box.querySelector(".progress-auto-text");
      if (!state) { box.replaceChildren(); return; }
      if (!span) { span = node("span", "progress-auto-text"); box.prepend(span); }
      if (span.textContent !== text) span.textContent = text;
      const hold = box.querySelector("#progress-hold");
      if (state.hold && !hold) box.append(button("progress-hold", "progress.hold", "btn-secondary progress-hold", () => action("hold")));
      else if (!state.hold && hold) hold.remove();
    }

    function render(force) {
      const body = view.body;
      const report = body && body.report;
      const pending = Boolean(report && report.pending && typeof report.report_id === "string" && ID.test(report.report_id));
      doc.title = pending ? `● ${baseTitle}` : baseTitle;
      if (!pending) {
        slot.hidden = true;
        slot.replaceChildren();
        view.sig = "";
        stopTicker();
        return;
      }
      const sig = JSON.stringify([report.report_id, report.note, report.end, report.auto, report.artifacts, body.consecutive_auto,
        view.busy, view.error, view.redirect, lang()]);
      const autoBox = slot.querySelector(".progress-auto");
      if (!force && sig === view.sig) {
        if (autoBox) renderAuto(autoBox, body);
        return;
      }
      const focusedId = doc.activeElement && slot.contains(doc.activeElement) ? doc.activeElement.id : null;
      const typed = slot.querySelector("#progress-redirect-input");
      const draft = typed ? typed.value : "";
      view.sig = sig;
      slot.replaceChildren();
      const head = node("div", "progress-head");
      const title = node("h2", "progress-title", tr("progress.title"));
      title.id = "progress-title";
      head.append(title, node("span", "progress-time", tr("progress.made_at", { time: timeOf(report.ts) })));
      slot.append(head);
      const note = node("p", "progress-note", report.note || tr(report.end === "hard" ? "progress.no_note_hard" : "progress.no_note"));
      if (!report.note) note.classList.add("muted");
      slot.append(note);
      const facts = node("dl", "progress-facts");
      for (const [label, value] of factRows(report)) {
        const row = node("div");
        row.append(node("dt", null, label), node("dd", null, value));
        facts.append(row);
      }
      slot.append(facts);
      const link = artifactLink(report);
      if (link) {
        const p = node("p", "progress-artifacts");
        const a = node("a", null, tr("progress.artifacts", { n: link.n }));
        a.href = link.href;
        p.append(a);
        slot.append(p);
      }
      const auto = node("p", "progress-auto");
      auto.setAttribute("role", "status");
      renderAuto(auto, body);
      slot.append(auto);
      const actions = node("div", "progress-actions");
      actions.append(
        button("progress-continue", "progress.continue", "btn-primary", () => send("continue")),
        button("progress-redirect", "progress.redirect", "btn-secondary", () => { view.redirect = true; render(true); doc.getElementById("progress-redirect-input")?.focus(); }),
        button("progress-stop", "progress.stop", "btn-danger", () => action("stop")),
      );
      slot.append(actions);
      slot.append(node("p", "progress-help", tr("progress.stop_hint")));
      if (view.redirect) {
        const form = node("form", "progress-redirect");
        const label = node("label", null, tr("progress.redirect_label"));
        label.setAttribute("for", "progress-redirect-input");
        const input = node("input");
        input.id = "progress-redirect-input";
        input.type = "text";
        input.maxLength = 300;
        input.value = draft;
        input.setAttribute("aria-describedby", "progress-redirect-help");
        const help = node("small", "progress-help", tr("progress.redirect_help"));
        help.id = "progress-redirect-help";
        const go = button("progress-redirect-send", "progress.redirect_send", "btn-primary", () => {});
        go.type = "submit";
        const cancel = button("progress-redirect-cancel", "progress.redirect_cancel", "btn-secondary", () => close());
        const close = () => { view.redirect = false; render(true); doc.getElementById("progress-redirect")?.focus(); };
        form.addEventListener("submit", (event) => {
          event.preventDefault();
          const text = input.value.trim();
          if (!text) { input.focus(); return; }
          void send("redirect", text);
        });
        input.addEventListener("keydown", (event) => { if (event.key === "Escape") { event.preventDefault(); close(); } });
        const row = node("div", "progress-redirect-row");
        row.append(input, go, cancel);
        form.append(label, row, help);
        slot.append(form);
      }
      const error = node("p", "progress-error", view.error);
      error.setAttribute("role", "alert");
      slot.append(error);
      slot.hidden = false;
      if (focusedId && doc.getElementById(focusedId)) doc.getElementById(focusedId).focus({ preventScroll: true });
      if (report.report_id !== view.seen) {
        view.seen = report.report_id;
        if (options.onReport) options.onReport(report);
      }
      startTicker();
    }

    function startTicker() {
      if (ticker || !root.setInterval) return;
      ticker = root.setInterval(() => {
        const box = slot.querySelector(".progress-auto");
        if (box && view.body) renderAuto(box, view.body);
      }, 1000);
    }

    function stopTicker() {
      if (ticker) root.clearInterval(ticker);
      ticker = null;
    }

    async function refresh() {
      try {
        const response = await fetchJSON("/api/progress/current");
        if (!response.ok) return null;
        const body = await response.json();
        const before = view.body && view.body.report && view.body.report.pending ? view.body.report.report_id : null;
        view.body = body;
        // The YOLO start dialog's tick takes its default and its numbers from here.
        const settings = body && body.settings;
        if (settings) {
          api.defaultOn = settings.enabled === true;
          api.intervalMin = Number(settings.interval_min) || 30;
          api.continueAfterMin = Number(settings.continue_after_min) || 10;
          api.maxAuto = Number.isFinite(settings.max_auto_continues) ? settings.max_auto_continues : 3;
        }
        render(false);
        const after = body.report && body.report.pending ? body.report.report_id : null;
        return before !== after;
      } catch {
        return null;
      }
    }

    const pendingNow = () => Boolean(view.body && view.body.report && view.body.report.pending);
    let poller = null;
    if (options.poll !== false) {
      if (root.StiraPoll) {
        poller = root.StiraPoll.create({ run: async () => { const r = await refresh(); if (r === null) throw new Error("read failed"); return r; }, base: 3000, max: 15000, hold: pendingNow, doc });
        poller.start();
      } else {
        void refresh();
      }
    }
    return { refresh, render, poke: () => (poller && poller.boost ? poller.boost() : refresh()), state: view };
  }

  const api = { create, factRows, artifactLink, autoState, duration, clockText, instance: null };
  api.poke = () => { if (api.instance) api.instance.poke(); };
  root.StiraProgress = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (doc && doc.getElementById && !root.STIRA_PROGRESS_NO_AUTOSTART) {
    const start = () => { if (!api.instance) api.instance = create(); };
    if (doc.readyState === "loading") doc.addEventListener("DOMContentLoaded", start);
    else start();
  }
})(typeof window !== "undefined" ? window : globalThis);
