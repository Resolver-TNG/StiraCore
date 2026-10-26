/* Settings page: where each value comes from and when it applies (#469).
 *
 * Reads `state.surface` of GET /api/config/hub (settings_hub.js dispatches
 * `stira:hub-state` after each render) and shows, read-only:
 *   - a banner "N changes wait for a restart" (restart fields saved since the server started),
 *   - the "Where settings live" card: file names, data-directory match, tool-rule anchor and
 *     lock, and every environment variable that pins a setting (its NAME; values never reach
 *     the page),
 *   - for any input marked `data-surface-field="<kind>.<name>"`: disabled while a variable
 *     pins it, with the variable's name as the reason (sections #470–#479 mark theirs).
 * It saves nothing and adds no route. StiraSettingsSurface.applyPins(doc, surface, scope)
 * can be called by a section that renders its inputs later.
 */
(function (root) {
  "use strict";

  // Literal t() calls so `i18n_extract.py --check` sees every key.
  const GROUP_LABEL = {
    classifier: () => t("hub.sec_classifier"),
    write: () => t("hub.write_title"),
    model: () => t("hub.sec_model"),
    tools: () => t("hub.tools_title"),
    breaker: () => t("hub.breaker_title"),
    breaker_details: () => t("surface.grp_breaker_details"),
    heartbeat: () => t("surface.grp_heartbeat"),
    prompt_cache: () => t("surface.grp_prompt_cache"),
    context_management: () => t("surface.grp_context_management"),
    distiller: () => t("surface.grp_distiller"),
  };
  const FIELD_LABEL = {
    "gateway.model_id": () => t("settings.model"),
    "agent.model_override": () => t("settings.model"),
    "gateway.provider": () => t("settings.provider"),
    "agent.provider_override": () => t("settings.provider"),
    "gateway.base_url": () => t("surface.fld_base_url"),
    "gateway.api_key_env": () => t("surface.fld_api_key_env"),
    "gateway.region": () => t("surface.fld_region"),
    "gateway.port": () => t("surface.fld_port"),
    "gateway.state_base_path": () => t("surface.fld_state_path"),
    "gateway.hb_enabled": () => t("surface.grp_heartbeat"),
    "gateway.prompt_cache": () => t("surface.grp_prompt_cache"),
    "gateway.prompt_cache_ttl": () => t("surface.fld_prompt_cache_ttl"),
    "gateway.context_management": () => t("surface.grp_context_management"),
    "agent.episodic_emotion_snapshot": () => t("surface.fld_episodic_emotion"),
    "gateway.yolo": () => t("yset.sum"),
    // #471: the breaker set and its details (same words as the hub's change list).
    "stira.stability_warning": () => t("hub.warn_label"),
    "stira.stability_critical": () => t("hub.crit_label"),
    "stira.bc_threshold": () => t("hub.bc_label"),
    "stira.stability_warning_exit": () => t("hub.warn_exit_label"),
    "stira.stability_critical_exit": () => t("hub.crit_exit_label"),
    "stira.bc_exit_threshold": () => t("hub.bc_exit_label"),
    "stira.bc_window_size": () => t("hub.det_bc_window"),
    "stira.critical_cooldown_turns": () => t("hub.det_cooldown"),
    "stira.decay_min_interval_sec": () => t("hub.det_decay"),
    "stira.sycophancy_threshold": () => t("hub.det_syc"),
    "stira.sycophancy_window_size": () => t("hub.det_syc_window"),
    "stira.hitl_timeout_sec": () => t("hub.det_hitl_timeout"),
    "stira.hitl_max_edit_rounds": () => t("hub.det_hitl_rounds"),
  };
  const APPLIES_TEXT = {
    next_turn: () => t("hub.timing_next_turn"),
    restart: () => t("hub.timing_restart"),
    reload: () => t("hub.timing_reload"),
    immediate: () => t("surface.applies_immediate"),
    on_run: () => t("surface.applies_on_run"),
    per_field: () => t("surface.applies_per_field"),
  };
  const DATA_DIR_TEXT = {
    STIRA_DATA_DIR: () => t("surface.data_dir_env"),
    default: () => t("surface.data_dir_default"),
  };

  function make(doc, tag, className, content) {
    const node = doc.createElement(tag);
    if (className) node.className = className;
    if (content !== undefined && content !== null) node.textContent = content;
    return node;
  }

  function code(doc, text) {
    return make(doc, "code", null, text);
  }

  function shortName(id) {
    const dot = String(id).indexOf(".");
    return dot < 0 ? String(id) : String(id).slice(dot + 1);
  }

  function fieldLabel(id) {
    return FIELD_LABEL[id] ? FIELD_LABEL[id]() : null;
  }

  function groupLabel(key) {
    if (GROUP_LABEL[key]) return GROUP_LABEL[key]();
    return fieldLabel(key) || shortName(key);
  }

  function appliesText(applies) {
    return APPLIES_TEXT[applies] ? APPLIES_TEXT[applies]() : null;
  }

  /** {count, text} for the restart banner, or null when nothing waits. */
  function pendingSummary(surface) {
    const groups = (surface && surface.restart_pending_groups) || [];
    if (!groups.length) return null;
    return {
      count: groups.length,
      text: t("surface.restart_pending", { count: groups.length, fields: groups.map(groupLabel).join(" / ") }),
    };
  }

  /** The reason an input is disabled, or null: "Set by the environment variable X". */
  function pinNote(surface, fieldId) {
    const field = surface && surface.fields && surface.fields[fieldId];
    if (!field || !field.pinned_by) return null;
    return t("surface.pinned_by", { name: field.pinned_by });
  }

  function renderBanner(doc, el, surface) {
    if (!el) return;
    const summary = pendingSummary(surface);
    el.textContent = "";
    el.hidden = !summary;
    if (!summary) return;
    el.appendChild(make(doc, "strong", null, summary.text));
    el.appendChild(doc.createTextNode(" "));
    el.appendChild(make(doc, "span", null, t("surface.restart_how")));
  }

  function fact(doc, list, label, value, stateName) {
    const row = make(doc, "div");
    row.appendChild(make(doc, "dt", null, label));
    const dd = make(doc, "dd");
    if (typeof value === "string") dd.textContent = value;
    else dd.appendChild(value);
    if (stateName) dd.dataset.state = stateName;
    row.appendChild(dd);
    list.appendChild(row);
  }

  function renderLocation(doc, elements, surface) {
    const facts = elements.facts;
    const list = elements.pinned;
    if (!facts || !list) return;
    facts.textContent = "";
    list.textContent = "";
    if (!surface) return;
    if (surface.available === false) {
      // The server could not build these facts (#490 review): say so, saving still works.
      fact(doc, facts, t("surface.where"), t("surface.unavailable"), "warn");
      if (elements.empty) elements.empty.hidden = true;
      list.hidden = true;
      if (elements.restartUnknown) elements.restartUnknown.hidden = true;
      return;
    }
    const loc = surface.location || {};
    const files = make(doc, "span");
    (loc.files || []).forEach((name, index) => {
      if (index) files.appendChild(doc.createTextNode(", "));
      files.appendChild(code(doc, name));
    });
    fact(doc, facts, t("surface.files"), files);
    let where;
    let whereState;
    if (loc.matches_data_dir === true) { where = t("surface.in_data_dir"); whereState = "ok"; }
    else if (loc.matches_data_dir === false) { where = t("surface.not_in_data_dir"); whereState = "warn"; }
    else if (loc.same_directory === false) { where = t("surface.split_dirs"); whereState = "warn"; }
    else { where = t("surface.location_unknown"); }
    fact(doc, facts, t("surface.where"), where, whereState);
    if (loc.data_dir && DATA_DIR_TEXT[loc.data_dir]) {
      fact(doc, facts, t("surface.data_dir"), DATA_DIR_TEXT[loc.data_dir]());
    }
    const anchor = make(doc, "span", null, loc.anchor_configured ? t("surface.anchor_on") : t("surface.anchor_off"));
    anchor.appendChild(doc.createTextNode(" "));
    anchor.appendChild(code(doc, loc.anchor_env || "STIRA_TOOL_RULES_ANCHOR_DIR"));
    fact(doc, facts, t("surface.anchor"), anchor);
    fact(
      doc, facts, t("surface.tools_lock"),
      loc.tools_locked ? t("surface.tools_locked") : t("surface.tools_unlocked"),
      loc.tools_locked ? "crit" : "ok",
    );
    const pinned = surface.pinned || [];
    if (elements.empty) elements.empty.hidden = pinned.length > 0;
    list.hidden = pinned.length === 0;
    for (const entry of pinned) {
      const item = make(doc, "li");
      item.appendChild(code(doc, entry.name));
      const targets = (entry.fields || []).map((id) => fieldLabel(id) || shortName(id));
      const text = targets.length ? targets.join(" / ") : t("surface.pinned_switch");
      item.appendChild(make(doc, "span", null, ` → ${text}`));
      const when = appliesText(entry.applies);
      if (when) item.appendChild(make(doc, "span", "hint", ` (${when})`));
      list.appendChild(item);
    }
    if (elements.restartUnknown) elements.restartUnknown.hidden = surface.restart_known !== false;
  }

  /** Disable every `[data-surface-field]` input a variable pins; give it the reason. */
  function applyPins(doc, surface, scope) {
    const base = scope || doc;
    if (!base || typeof base.querySelectorAll !== "function") return 0;
    let pinnedCount = 0;
    base.querySelectorAll("[data-surface-field]").forEach((input) => {
      const id = input.getAttribute("data-surface-field");
      const note = pinNote(surface, id);
      const noteId = `pin-${id.replace(/[^A-Za-z0-9_-]/g, "-")}`;
      let hint = doc.getElementById(noteId);
      if (note) {
        pinnedCount += 1;
        input.disabled = true;
        input.setAttribute("data-surface-pinned", "true");
        if (!hint) {
          hint = make(doc, "p", "hint pin-note");
          hint.id = noteId;
          input.insertAdjacentElement("afterend", hint);
        }
        hint.textContent = note;
        hint.hidden = false;
        const described = (input.getAttribute("aria-describedby") || "").split(/\s+/).filter(Boolean);
        if (!described.includes(noteId)) input.setAttribute("aria-describedby", [...described, noteId].join(" "));
      } else if (input.getAttribute("data-surface-pinned") === "true") {
        input.disabled = false;
        input.removeAttribute("data-surface-pinned");
        if (hint) hint.hidden = true;
      }
    });
    return pinnedCount;
  }

  let lastSurface = null;

  function render(doc, surface) {
    lastSurface = surface || null;
    renderBanner(doc, doc.getElementById("restart-banner"), surface);
    renderLocation(doc, {
      facts: doc.getElementById("location-facts"),
      pinned: doc.getElementById("pinned-list"),
      empty: doc.getElementById("pinned-empty"),
      restartUnknown: doc.getElementById("restart-unknown"),
    }, surface);
    applyPins(doc, surface);
  }

  const api = {
    applyPins,
    appliesText,
    groupLabel,
    pendingSummary,
    pinNote,
    render,
    renderBanner,
    renderLocation,
    current: () => lastSurface,
    field: (id) => (lastSurface && lastSurface.fields && lastSurface.fields[id]) || null,
  };
  root.StiraSettingsSurface = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
  if (root.document && typeof root.document.addEventListener === "function") {
    root.document.addEventListener("stira:hub-state", (event) => {
      const state = event && event.detail;
      render(root.document, state && state.surface);
    });
  }
})(typeof window !== "undefined" ? window : globalThis);
