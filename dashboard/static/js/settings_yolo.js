/* Settings page: the YOLO section (ADR 0012 phase 7c).
 *
 * GET /api/yolo/settings → every value with what is saved, what a start would use and from
 * where (env / config / default), its default, the variable that pins it and when a save
 * is used (restart / next_window). The section has its own save button (as the model
 * section): POST /api/yolo/settings with the changed values only.
 *
 * A save that loosens is confirmed: without the allowance the server answers 409
 * needs_confirmation and this page asks in the shared dialog (one box), then sends
 * confirm + acknowledged; with the allowance the server answers 428 and api.js's
 * stiraFetch shows the host confirmation (browser checkbox or the host) and re-sends.
 * One dialog either way. Inputs are checked as they are typed (same ranges as the
 * server); the server decides.
 *
 * StiraYoloSettings.create({ document, elements, getJSON, post, confirm }) for tests;
 * StiraYoloSettings.validate / changedValues are pure.
 */
(function (root) {
  "use strict";
  const FIELDS = ["allow", "offer_exec", "max_auto_approvals", "max_external", "max_exec", "host_confirm"];
  const CAPS = ["max_auto_approvals", "max_external", "max_exec"];
  const CAP_MAX = 10000;
  const LOW = { max_auto_approvals: 1, max_external: 0, max_exec: 0 };
  const CHOICES = { allow: ["off", "internal", "external"], host_confirm: ["browser", "auto", "command", "dialog"] };
  const INPUT_ID = {
    allow: "yset-allow", offer_exec: "yset-offer-exec", max_auto_approvals: "yset-cap",
    max_external: "yset-external", max_exec: "yset-exec", host_confirm: "yset-confirm",
  };
  const PIN_ID = {
    allow: "yset-allow-pin", max_auto_approvals: "yset-cap-pin", max_external: "yset-external-pin",
    max_exec: "yset-exec-pin", host_confirm: "yset-confirm-pin",
  };

  // Literal t() calls so `i18n_extract.py --check` sees every key.
  const FIELD_TEXT = {
    allow: () => t("yset.allow_label"),
    offer_exec: () => t("yset.offer_exec_label"),
    max_auto_approvals: () => t("yset.cap_label"),
    max_external: () => t("yset.external_label"),
    max_exec: () => t("yset.exec_label"),
    host_confirm: () => t("yset.confirm_label"),
  };
  const VALUE_TEXT = {
    off: () => t("yset.allow_off"),
    internal: () => t("yset.allow_internal"),
    external: () => t("yset.allow_external"),
    browser: () => t("yset.confirm_browser"),
    auto: () => t("yset.confirm_auto"),
    command: () => t("yset.confirm_command"),
    dialog: () => t("yset.confirm_dialog"),
    true: () => t("yset.on"),
    false: () => t("yset.off"),
  };
  const SOURCE_TEXT = {
    env: () => t("yset.source_env"),
    config: () => t("yset.source_config"),
    default: () => t("yset.source_default"),
  };
  const ERROR_TEXT = {
    out_of_range: (p) => t("yset.err_out_of_range", { min: p.min, max: p.max }),
    exceeds_cap: (p) => t("yset.err_exceeds_cap", { max: p.max }),
    not_integer: (p) => t("yset.err_out_of_range", { min: p.min, max: p.max }),
    invalid_choice: () => t("yset.err_invalid_choice"),
    not_boolean: () => t("yset.err_invalid_choice"),
    pinned_by_env: (p) => t("yset.err_pinned", { env: p.env }),
    wildcard_bind: () => t("yset.err_wildcard_bind"),
    env_needs_allow: (p) => t("yset.err_env_needs_allow", { env: (p.env || []).join(", ") }),
  };
  const CODE_TEXT = {
    turn_running: () => t("yset.code_turn_running"),
    tool_call_running: () => t("yset.code_tool_call_running"),
    yolo_busy: () => t("yset.busy"),
    version_conflict: () => t("yset.code_version_conflict"),
    config_unavailable: () => t("yset.code_config_unavailable"),
    save_failed: () => t("yset.code_save_failed"),
    audit_failed: () => t("yset.code_audit_failed"),
    env_invalid: () => t("yset.code_config_unavailable"),
    invalid_request: () => t("yset.code_save_failed"),
  };

  function valueText(field, value) {
    if (CAPS.includes(field)) return String(value);
    const fn = VALUE_TEXT[String(value)];
    return fn ? fn() : String(value);
  }

  // The draft's field errors: { field: { code, params } } (the server's ranges).
  function validate(draft, fields) {
    const errors = {};
    const cap = fields && fields.max_auto_approvals && fields.max_auto_approvals.pinned
      ? fields.max_auto_approvals.value : draft.max_auto_approvals;
    for (const name of CAPS) {
      const value = draft[name];
      if (!Number.isInteger(value) || value < LOW[name] || value > CAP_MAX) {
        errors[name] = { code: "out_of_range", params: { min: LOW[name], max: CAP_MAX } };
      }
    }
    if (!errors.max_auto_approvals || (fields && fields.max_auto_approvals && fields.max_auto_approvals.pinned)) {
      for (const name of ["max_external", "max_exec"]) {
        // A sub-cap left at its default follows a smaller cap (as on the server).
        const field = fields && fields[name];
        const followsCap = field && field.source === "default" && draft[name] === field.value;
        if (!errors[name] && !followsCap && Number.isInteger(cap) && draft[name] > cap) {
          errors[name] = { code: "exceeds_cap", params: { max: cap } };
        }
      }
    }
    for (const name of ["allow", "host_confirm"]) {
      if (!CHOICES[name].includes(draft[name])) errors[name] = { code: "invalid_choice", params: {} };
    }
    if (typeof draft.offer_exec !== "boolean") errors.offer_exec = { code: "not_boolean", params: {} };
    return errors;
  }

  // Only what differs from the value a start would use now (pinned values never).
  function changedValues(draft, fields) {
    const out = {};
    for (const name of FIELDS) {
      const field = fields[name];
      if (!field || field.pinned) continue;
      if (draft[name] !== field.value) out[name] = draft[name];
    }
    return out;
  }

  function create({ document: doc, elements: els, getJSON, post, confirm }) {
    let state = null; // last GET / POST answer
    let draft = null;
    let saving = false;
    let lastSaved = null; // { changed, caps_applied } of the last successful save

    const input = (name) => els.inputs[name];

    function readDraft() {
      const next = {};
      for (const name of FIELDS) {
        const el = input(name);
        if (name === "offer_exec") next[name] = el.checked;
        else if (CAPS.includes(name)) {
          const text = String(el.value).trim();
          next[name] = /^[0-9]{1,6}$/.test(text) ? Number(text) : NaN;
        } else next[name] = el.value;
      }
      return next;
    }

    function setText(el, text) {
      if (!el) return;
      el.textContent = text || "";
      el.hidden = !text;
    }

    function showErrors(errors) {
      for (const name of FIELDS) {
        const box = els.errors[name], el = input(name);
        const error = errors[name];
        const text = error ? (ERROR_TEXT[error.code] || (() => error.code))(error.params || {}) : "";
        setText(box, text);
        if (el) {
          if (error) el.setAttribute("aria-invalid", "true");
          else el.removeAttribute("aria-invalid");
        }
      }
    }

    function sync() {
      if (!state) return;
      draft = readDraft();
      const errors = validate(draft, state.fields);
      showErrors(errors);
      const dirty = Object.keys(changedValues(draft, state.fields)).length > 0;
      const valid = Object.keys(errors).length === 0;
      els.save.disabled = saving || !dirty || !valid || state.busy;
      els.discard.disabled = saving || !dirty;
      // The page's common Save does not include this section: say so next to it.
      if (els.footerNote) els.footerNote.hidden = !dirty;
      if (dirty && !saving) setText(els.status, valid ? t("yset.unsaved") : t("yset.fix_errors"));
      else if (!saving && !lastSaved) setText(els.status, "");
      // Sub-cap inputs follow the cap's range as typed.
      const cap = state.fields.max_auto_approvals.pinned ? state.fields.max_auto_approvals.value : draft.max_auto_approvals;
      if (Number.isInteger(cap)) for (const name of ["max_external", "max_exec"]) input(name).max = String(Math.min(cap, CAP_MAX));
    }

    function render(body) {
      state = body;
      const fields = body.fields;
      for (const name of FIELDS) {
        const field = fields[name];
        const el = input(name);
        if (name === "offer_exec") el.checked = field.value === true;
        else el.value = String(field.value);
        el.disabled = field.pinned || body.busy;
        const pin = els.pins[name];
        if (pin) setText(pin, field.pinned ? t("yset.pinned", { env: field.env }) : "");
        const badge = els.sources[name];
        if (badge) {
          const fn = SOURCE_TEXT[field.source];
          badge.textContent = fn ? fn() : field.source;
          badge.dataset.state = field.source === "env" ? "warn" : field.source === "config" ? "ok" : "info";
          badge.hidden = false;
        }
      }
      setText(els.execTools, body.exec_tools
        ? t("yset.exec_tools_on", { env: body.exec_tools_env })
        : t("yset.exec_tools_off", { env: body.exec_tools_env }));
      const pending = body.restart_pending || [];
      setText(els.restart, pending.length
        ? t("yset.restart_pending", { fields: pending.map((name) => FIELD_TEXT[name]()).join(" / ") })
        : "");
      els.busy.hidden = !body.busy;
      const warnings = body.warnings || [];
      setText(els.warnings, warnings.length ? t("yset.warnings", { codes: warnings.join(", ") }) : "");
      const running = fields.allow.running;
      els.stateBadge.textContent = running === "off" ? t("yset.state_off") : t("yset.state_on", { tier: valueText("allow", running) });
      els.stateBadge.dataset.state = running === "off" ? "info" : "warn";
      els.stateBadge.hidden = false;
      if (els.summary) els.summary.textContent = els.stateBadge.textContent + (pending.length ? ` · ${t("yset.sum_pending")}` : "");
      els.fields.hidden = false;
      setText(els.load, "");
      sync();
    }

    async function load() {
      try {
        const { ok, data } = await getJSON("/api/yolo/settings");
        if (!ok) throw new Error("load");
        render(data);
      } catch (e) {
        setText(els.load, t("yset.load_failed"));
      }
    }

    function savedMessage(body) {
      const changed = body.changed || [];
      const restart = changed.filter((name) => body.fields[name].applies === "restart");
      const caps = changed.filter((name) => body.fields[name].applies === "next_window");
      const parts = [t("yset.saved")];
      if (restart.length) parts.push(t("yset.saved_restart"));
      if (caps.length) parts.push(body.caps_applied === false ? t("yset.saved_caps_later") : t("yset.saved_next_window"));
      return parts.join(" ");
    }

    function confirmItems(values, codes) {
      const items = [];
      for (const name of FIELDS) {
        if (!(name in values)) continue;
        items.push(t("yset.confirm_item", {
          field: FIELD_TEXT[name](),
          from: valueText(name, state.fields[name].value),
          to: valueText(name, values[name]),
        }));
      }
      if (codes.includes("yolo_allow")) items.push(t("yset.confirm_allow_note"));
      // Hand-edited invalid values are rounded or dropped by this save (PR #444 m1).
      const warnings = state.warnings || [];
      if (warnings.length) items.push(t("yset.confirm_rounds_note", { codes: warnings.join(", ") }));
      return items;
    }

    // One request (and its confirmation) → { message, errors } for the status line.
    async function attempt(values) {
      let body = { values, base_version: state.version };
      let result = await post("/api/yolo/settings", body);
      if (result.status === 409 && result.data && result.data.code === "needs_confirmation") {
        const codes = Array.isArray(result.data.needs_confirmation) ? result.data.needs_confirmation : [];
        const ok = await confirm({
          title: t("yset.confirm_title"),
          intro: t("yset.confirm_intro"),
          items: confirmItems(values, codes),
          checks: [t("host.browser_check")],
          confirmLabel: t("hub.confirm_save"),
          cancelLabel: t("hub.cancel"),
          tone: "danger",
        });
        if (!ok) return { message: t("yset.cancelled") };
        body = { ...body, confirm: true, acknowledged: codes };
        result = await post("/api/yolo/settings", body);
      }
      const data = result.data || {};
      if (result.ok && data.saved) {
        lastSaved = data;
        render(data);
        return { message: savedMessage(data) };
      }
      if (Array.isArray(data.errors) && data.errors.length) {
        const errors = {};
        for (const error of data.errors) if (FIELDS.includes(error.field)) errors[error.field] = error;
        return { message: t("yset.fix_errors"), errors };
      }
      if (data.code === "version_conflict") await load();
      const fn = CODE_TEXT[data.code];
      // A host-confirmation outcome from api.js is already worded by this page (local).
      return { message: fn ? fn() : (data.local && data.error) || t("yset.code_save_failed") };
    }

    async function save() {
      if (!state || saving) return;
      draft = readDraft();
      if (Object.keys(validate(draft, state.fields)).length) return sync();
      const values = changedValues(draft, state.fields);
      if (!Object.keys(values).length) return sync();
      saving = true;
      lastSaved = null;
      els.save.disabled = true;
      els.discard.disabled = true;
      setText(els.status, t("yset.saving"));
      let outcome;
      try {
        outcome = await attempt(values);
      } catch (e) {
        outcome = { message: t("yset.code_save_failed") };
      }
      saving = false;
      sync();
      if (outcome.errors) showErrors(outcome.errors);
      setText(els.status, outcome.message);
    }

    function discard() {
      if (state) render(state);
      setText(els.status, "");
    }

    for (const name of FIELDS) {
      const el = input(name);
      el.addEventListener(el.type === "checkbox" || el.tagName === "SELECT" ? "change" : "input", () => {
        lastSaved = null;
        sync();
      });
    }
    els.save.addEventListener("click", save);
    els.discard.addEventListener("click", discard);
    return { load, save, render, sync, state: () => state };
  }

  async function getJSON(url) {
    const response = await root.fetch(url, { cache: "no-store", credentials: "same-origin", headers: { Accept: "application/json" } });
    const data = await response.json().catch(() => ({}));
    return { ok: response.ok, status: response.status, data };
  }

  async function post(url, body) {
    const response = await root.stiraFetch(url, {
      method: "POST", credentials: "same-origin",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify(body),
    });
    const data = await response.json().catch(() => ({}));
    return { ok: response.ok, status: response.status, data };
  }

  function init() {
    const doc = root.document;
    const id = (name) => doc.getElementById(name);
    if (!id("sec-yolo")) return;
    const inputs = {}, errors = {}, pins = {}, sources = {};
    for (const name of FIELDS) {
      inputs[name] = id(INPUT_ID[name]);
      errors[name] = id(`err-yset-${name}`);
      pins[name] = PIN_ID[name] ? id(PIN_ID[name]) : null;
      sources[name] = doc.querySelector(`.yset-source[data-field="${name}"]`);
    }
    const section = create({
      document: doc,
      elements: {
        inputs, errors, pins, sources,
        fields: id("yset-fields"), load: id("yset-load"), restart: id("yset-restart"), busy: id("yset-busy"),
        warnings: id("yset-warnings"), execTools: id("yset-exec-tools"), stateBadge: id("yset-state"),
        summary: id("sum-yolo"), save: id("yset-save"), discard: id("yset-discard"), status: id("yset-status"),
        footerNote: id("hub-yolo-unsaved"),
      },
      getJSON,
      post,
      confirm: (options) => (root.StiraConfirm ? root.StiraConfirm.open(options) : Promise.resolve(false)),
    });
    section.load();
  }

  root.StiraYoloSettings = { create, validate, changedValues, FIELDS };
  if (root.document && typeof root.document.addEventListener === "function" && !root.__STIRA_NO_AUTOINIT__) {
    if (root.document.readyState === "loading") root.document.addEventListener("DOMContentLoaded", init);
    else init();
  }
})(typeof window !== "undefined" ? window : globalThis);
