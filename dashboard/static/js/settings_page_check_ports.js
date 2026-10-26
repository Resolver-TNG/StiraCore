/* Settings page: page_check's localhost ports (ADR 0015 Addendum rev 4, #449).
 *
 * GET /api/tools/page-check-ports → { ports, locked, max_ports, min_port, refused_ports }.
 * Adding a port is a loosening: the shared dialog asks the person to type the port, then
 * POST { ports, confirm: true, confirm_ports: [port] } (with the YOLO allowance the server
 * answers 428 and api.js's stiraFetch shows the host confirmation and re-sends). Removing
 * a port sends the shorter list (no confirmation: it only narrows). The server decides;
 * the checks here only explain early.
 *
 * StiraPageCheckPorts.create({ document, getJSON, post, confirm }) for tests;
 * StiraPageCheckPorts.validate(text, state) is pure.
 */
(function (root) {
  "use strict";
  const tr = (key, vars) => (typeof t === "function" ? t(key, vars) : key);
  const ROUTE = "/api/tools/page-check-ports";

  // Literal t() calls so `i18n_extract.py --check` sees every key.
  const ERROR_TEXT = {
    not_a_port: () => tr("pcports.err_not_a_port"),
    port_refused: () => tr("pcports.err_port_refused"),
    own_dashboard: () => tr("pcports.err_own_dashboard"),
    already: () => tr("pcports.err_already"),
    too_many: () => tr("pcports.err_too_many"),
    breaker_active: () => tr("pcports.err_breaker_active"),
    needs_confirmation: () => tr("pcports.err_needs_confirmation"),
    tool_rules_locked: () => tr("pcports.err_locked"),
    tool_call_running: () => tr("pcports.err_tool_call_running"),
    failed: () => tr("pcports.err_failed"),
  };

  function validate(text, state) {
    const value = String(text || "").trim();
    if (!/^[0-9]{1,5}$/.test(value)) return { error: "not_a_port" };
    const port = Number(value);
    if (port > 65535) return { error: "not_a_port" };
    if (port < (state.min_port || 1024)) return { error: "port_refused" };
    if ((state.refused_ports || []).includes(port)) return { error: "own_dashboard" };
    if ((state.ports || []).includes(port)) return { error: "already" };
    if ((state.ports || []).length >= (state.max_ports || 8)) return { error: "too_many" };
    return { port };
  }

  function create(options) {
    const doc = options.document;
    const el = (id) => doc.getElementById(id);
    const input = el("pcports-new");
    const add = el("pcports-add");
    const list = el("pcports-list");
    const error = el("err-pcports");
    const status = el("pcports-status");
    const locked = el("pcports-locked");
    let state = { ports: [], locked: false, max_ports: 8, min_port: 1024, refused_ports: [] };

    function showError(code) {
      const text = (ERROR_TEXT[code] || ERROR_TEXT.failed)();
      error.textContent = text;
      error.hidden = false;
      input.setAttribute("aria-invalid", "true");
    }

    function clearError() {
      error.textContent = "";
      error.hidden = true;
      input.removeAttribute("aria-invalid");
    }

    function render() {
      list.textContent = "";
      locked.hidden = !state.locked;
      locked.textContent = state.locked ? tr("pcports.locked") : "";
      input.disabled = add.disabled = state.locked;
      if (!state.ports.length) {
        const item = doc.createElement("li");
        item.className = "hint";
        item.textContent = tr("pcports.none");
        list.appendChild(item);
        return;
      }
      for (const port of state.ports) {
        const item = doc.createElement("li");
        const code = doc.createElement("code");
        code.textContent = `http://localhost:${port}/`;
        const remove = doc.createElement("button");
        remove.type = "button";
        remove.className = "btn btn-secondary";
        remove.textContent = tr("pcports.remove");
        remove.setAttribute("aria-label", tr("pcports.remove_port", { port }));
        remove.addEventListener("click", () => save(state.ports.filter((p) => p !== port), null));
        item.append(code, " ", remove);
        list.appendChild(item);
      }
    }

    async function load() {
      try {
        const data = await options.getJSON(ROUTE);
        if (data && Array.isArray(data.ports)) state = { ...state, ...data };
      } catch (e) {
        status.textContent = tr("pcports.err_failed");
      }
      render();
    }

    async function save(ports, added) {
      clearError();
      const body = { ports };
      if (added !== null) {
        body.confirm = true;
        body.confirm_ports = [added];
      }
      let answer;
      try {
        answer = await options.post(ROUTE, body);
      } catch (e) {
        showError("failed");
        return false;
      }
      const data = (answer && answer.data) || {};
      if (!answer || !answer.ok) {
        showError(typeof data.error_code === "string" ? data.error_code : data.code || "failed");
        return false;
      }
      state.ports = Array.isArray(data.ports) ? data.ports : ports;
      status.textContent = tr("pcports.saved");
      render();
      return true;
    }

    async function onAdd() {
      clearError();
      const checked = validate(input.value, state);
      if (checked.error) {
        showError(checked.error);
        return;
      }
      const port = checked.port;
      const ok = await options.confirm({
        title: tr("pcports.confirm_title"),
        intro: tr("pcports.confirm_intro"),
        items: [`http://localhost:${port}/`, tr("pcports.confirm_item_forward", { port })],
        typed: { word: String(port), prompt: tr("pcports.confirm_prompt", { port }) },
        confirmLabel: tr("pcports.confirm_button"),
        cancelLabel: tr("hub.cancel"),
        tone: "danger",
      });
      if (!ok) {
        status.textContent = tr("pcports.cancelled");
        return;
      }
      if (await save([...state.ports, port], port)) input.value = "";
    }

    add.addEventListener("click", onAdd);
    return { load, save, state: () => state };
  }

  async function getJSON(url) {
    const response = await root.fetch(url, { headers: { Accept: "application/json" } });
    return response.json();
  }

  async function post(url, body) {
    const send = root.stiraFetch || root.fetch;
    const response = await send(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify(body),
    });
    return { ok: response.ok, status: response.status, data: await response.json().catch(() => ({})) };
  }

  function start() {
    const doc = root.document;
    if (!doc || !doc.getElementById("pcports")) return;
    const dialog = root.StiraConfirm;
    const ui = create({
      document: doc,
      getJSON,
      post,
      confirm: (spec) => (dialog && typeof dialog.open === "function" ? dialog.open(spec) : Promise.resolve(false)),
    });
    ui.load();
  }

  root.StiraPageCheckPorts = { create, validate };
  if (root.document && root.document.readyState === "loading") {
    root.document.addEventListener("DOMContentLoaded", start);
  } else {
    start();
  }
})(typeof window !== "undefined" ? window : globalThis);
