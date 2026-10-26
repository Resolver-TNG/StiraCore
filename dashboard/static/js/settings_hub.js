// Settings hub (#227): one draft for classifier / breaker / tools, a sticky footer
// (save / discard / reset to defaults), inline validation mirrored from the server,
// typed confirmation for settings that remove safety margins, and per-section
// apply status taken from the server (saved / pending / applied / failed).
(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  const clone = (value) => JSON.parse(JSON.stringify(value));
  const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
  const BREAKER = [
    ["stability_warning", "stability-warning"],
    ["stability_critical", "stability-critical"],
    ["bc_threshold", "bc-threshold"],
  ];
  // #471: each entry with its exit (one set), and the details fold (read at start-up).
  const EXIT_OF = {
    stability_warning: "stability_warning_exit",
    stability_critical: "stability_critical_exit",
    bc_threshold: "bc_exit_threshold",
  };
  const BREAKER_SET = [...BREAKER.map(([key]) => key), ...BREAKER.map(([key]) => EXIT_OF[key])];
  const INPUT_OF = Object.fromEntries(BREAKER.flatMap(([key, id]) => [[key, id], [EXIT_OF[key], `${id}-exit`]]));
  const DETAILS = [
    ["bc_window_size", "bc-window-size"],
    ["critical_cooldown_turns", "critical-cooldown-turns"],
    ["decay_min_interval_sec", "decay-min-interval-sec"],
    ["sycophancy_threshold", "sycophancy-threshold"],
    ["sycophancy_window_size", "sycophancy-window-size"],
    ["hitl_timeout_sec", "hitl-timeout-sec"],
    ["hitl_max_edit_rounds", "hitl-max-edit-rounds"],
  ];
  const Pairs = window.StiraBreakerPairs;
  const SHELL_OPERATORS = /[\r\n;&|<>`$()]/;
  const INTERNAL_KEY = "stira.settings.showInternal";
  const HUB_GROUPS = ["classifier", "tools", "breaker", "breaker_details", "write", "read", "board", "output", "cache", "context", "heartbeat", "delegation", "agent_jobs"];
  // #470: the cost groups. Context options in the order of the page (rules come from the server).
  const CONTEXT_OPTIONS = [
    "offload_threshold_tokens", "preview_tokens", "compression_threshold", "summary_ratio",
    "preserve_recent", "retention_hours", "context_window_tokens", "retrieval_max_tokens",
  ];
  const COST_INPUTS = {
    "output.max_output_tokens": "cost-max-output",
    "cache.enabled": "cost-cache-enabled",
    "cache.ttl": "cost-cache-ttl",
    "context.enabled": "cost-context-enabled",
    ...Object.fromEntries(CONTEXT_OPTIONS.map((name) => [`context.${name}`, `cost-ctx-${name}`])),
  };
  // The switches a variable can pin: input -> the note on the value it shows. Which variable
  // pins it comes from the settings surface (#469), keyed by the input's data-surface-field.
  const COST_PIN_TARGETS = {
    "cache.enabled": ["cost-cache-enabled", "cost-cache-enabled-live"],
    "cache.ttl": ["cost-cache-ttl", "cost-cache-ttl-live"],
    "context.enabled": ["cost-context-enabled", "cost-context-enabled-live"],
  };
  // #472: the heartbeat inputs (minutes on the page, seconds in the file).
  const HB_INPUTS = {
    "heartbeat.enabled": "hb-enabled",
    "heartbeat.interval_floor_sec": "hb-floor",
    "heartbeat.interval_ceil_sec": "hb-ceil",
  };
  // #486 (ADR 0016): the delegate tool's caps (number inputs); the server re-checks them.
  const DELEGATION_LIMITS = [
    ["max_tool_calls", "delegation-max-tool-calls"],
    ["max_seconds", "delegation-max-seconds"],
    ["max_children_per_turn", "delegation-max-children"],
  ];

  let state = null; // last server state (GET/POST /api/config/hub)
  let draft = null; // what the operator is editing (same shape as state.values)
  let serverErrors = []; // [{field, code, params}] from the last save attempt
  let saving = false;
  let pollTimer = null;
  let previewing = null; // preset id shown in the preview panel
  let profilePreviewing = null; // tool profile id shown in its preview panel (#450)
  let historyEntries = [];
  let draftSource = "manual"; // recorded in the change history: manual / defaults / preset:<id> / profile:<id>
  let lastFixedCap = null; // #470: the number typed before switching back to "automatic"
  let writeScopeChoice = null; // radio picked by the operator; null = derived from the roots (#243)
  let readScopeChoice = null; // the same for the read roots (#457)

  // Literal t() calls so `i18n_extract.py --check` sees every key.
  const TOOL_TEXT = {
    current_time: () => [t("hub.tool_current_time"), t("hub.tool_current_time_desc")],
    file_read: () => [t("hub.tool_file_read"), t("hub.tool_file_read_desc")],
    handoff_to_user: () => [t("hub.tool_handoff_to_user"), t("hub.tool_handoff_to_user_desc")],
    image_reader: () => [t("hub.tool_image_reader"), t("hub.tool_image_reader_desc")],
    perplexity_search: () => [t("hub.tool_perplexity_search"), t("hub.tool_perplexity_search_desc")],
    stop: () => [t("hub.tool_stop"), t("hub.tool_stop_desc")],
    view_image: () => [t("hub.tool_view_image"), t("hub.tool_view_image_desc")],
    batch: () => [t("hub.tool_batch"), t("hub.tool_batch_desc")],
    cron: () => [t("hub.tool_cron"), t("hub.tool_cron_desc")],
    editor: () => [t("hub.tool_editor"), t("hub.tool_editor_desc")],
    file_write: () => [t("hub.tool_file_write"), t("hub.tool_file_write_desc")],
    graph: () => [t("hub.tool_graph"), t("hub.tool_graph_desc")],
    http_request: () => [t("hub.tool_http_request"), t("hub.tool_http_request_desc")],
    journal: () => [t("hub.tool_journal"), t("hub.tool_journal_desc")],
    lsp_diagnostics: () => [t("hub.tool_lsp_diagnostics"), t("hub.tool_lsp_diagnostics_desc")],
    lsp_hover: () => [t("hub.tool_lsp_hover"), t("hub.tool_lsp_hover_desc")],
    oauth_api_call: () => [t("hub.tool_oauth_api_call"), t("hub.tool_oauth_api_call_desc")],
    shell: () => [t("hub.tool_shell"), t("hub.tool_shell_desc")],
    speak: () => [t("hub.tool_speak"), t("hub.tool_speak_desc")],
    swarm: () => [t("hub.tool_swarm"), t("hub.tool_swarm_desc")],
    think: () => [t("hub.tool_think"), t("hub.tool_think_desc")],
    use_agent: () => [t("hub.tool_use_agent"), t("hub.tool_use_agent_desc")],
    web_fetch: () => [t("hub.tool_web_fetch"), t("hub.tool_web_fetch_desc")],
    web_search: () => [t("hub.tool_web_search"), t("hub.tool_web_search_desc")],
    webhook_send: () => [t("hub.tool_webhook_send"), t("hub.tool_webhook_send_desc")],
    memory: () => [t("hub.tool_memory"), t("hub.tool_memory_desc")],
    rss: () => [t("hub.tool_rss"), t("hub.tool_rss_desc")],
    diagram: () => [t("hub.tool_diagram"), t("hub.tool_diagram_desc")],
  };
  // Tool profiles (#450): Always / Ask / Never tables by purpose.
  const PROFILE_TEXT = {
    minimal: () => [t("profile.minimal"), t("profile.minimal_desc")],
    dev: () => [t("profile.dev"), t("profile.dev_desc")],
    research: () => [t("profile.research"), t("profile.research_desc")],
  };
  // What a YOLO window does with an Ask tool (yolo.targets), by scope or by reason.
  const PROFILE_YOLO_TEXT = {
    internal: () => t("profile.yolo_internal"),
    external: () => t("profile.yolo_external"),
    exec: () => t("profile.yolo_exec"),
    exec_runs_later: () => t("profile.yolo_person"),
    exec_child_ungated: () => t("profile.yolo_person"),
    person_tool: () => t("profile.yolo_person"),
    mcp_tool: () => t("profile.yolo_person"),
    unclassified: () => t("profile.yolo_person"),
  };
  const CLASSIFIER_TEXT = {
    auto: () => [t("hub.cls_auto"), t("hub.cls_auto_desc")],
    rule: () => [t("hub.cls_rule"), t("hub.cls_rule_desc")],
    sokudan: () => [t("hub.cls_sokudan"), t("hub.cls_sokudan_desc")],
  };
  const CLASSIFIER_SHORT = {
    auto: () => t("hub.cls_auto_short"),
    rule: () => t("hub.cls_rule"),
    sokudan: () => t("hub.cls_sokudan_short"),
  };
  const CLASSIFIER_NAMES = {
    SokudanClassifier: () => t("hub.cls_sokudan_short"),
    RuleBasedClassifier: () => t("hub.cls_rule"),
    unresolved: () => t("hub.cls_unresolved"),
  };
  const ERROR_TEXT = {
    not_number: () => t("hub.err_not_number"),
    out_of_range: (p) => (p.max === null || p.max === undefined
      ? t("hub.err_at_least", { min: p.min }) : t("hub.err_out_of_range", { min: p.min, max: p.max })),
    must_be_below: (p) => t("hub.err_must_be_below", { limit: fmt(p.limit) }),
    must_be_below_field: (p) => (p.other === "stability_warning_exit"
      ? t("hub.err_must_be_below_warning_exit", { limit: fmt(p.limit) })
      : t("hub.err_must_be_below_warning", { limit: fmt(p.limit) })),
    must_be_above: (p) => t("hub.err_must_be_above", { limit: fmt(p.limit) }),
    not_integer: () => t("hub.err_not_integer"),
    unknown_choice: () => t("hub.err_unknown_choice"),
    not_installed: () => t("hub.err_not_installed"),
    unknown_tool: (p) => t("hub.err_unknown_tool", { names: (p.names || []).join(", ") }),
    invalid_command: () => t("tools.enter_a_literal_command_without_shell_op"),
    secret_command: () => t("policy.err_secret_command"),
    secret_command_saved: (p) => t("policy.err_secret_command_saved", { count: p.count }),
    invalid_root: (p) => t("hub.err_invalid_root", { roots: (p.roots || []).join(", ") }),
    too_many: (p) => t("hub.err_too_many_roots", { max: p.max }),
    broad_root: (p) => t("hub.err_broad_root", { roots: (p.roots || []).join(", ") }),
    empty: () => t("hub.err_empty_roots"),
    not_boolean: () => t("hub.err_invalid"),
    // #470: cost settings
    too_small: (p) => t("cost.err_too_small", { min: p.min }),
    out_of_range_open: (p) => t("cost.err_out_of_range_open", { min: p.min, max: p.max }),
    must_be_below_other: (p) => t("cost.err_preview_below_offload", { limit: p.limit }),
    env_pinned: (p) => t("cost.err_env_pinned", { env: p.env }),
    // #472: heartbeat intervals (seconds from the server, shown as minutes / hours)
    interval_out_of_range: (p) => t("hb.err_out_of_range", { min: fmtDuration(p.min), max: fmtDuration(p.max) }),
    floor_above_ceil: (p) => t("hb.err_floor_above_ceil", { limit: fmtDuration(p.limit) }),
    not_integer: () => t("hub.err_not_integer"),
  };
  const PRESET_TEXT = {
    safe: () => [t("hub.preset_safe"), t("hub.preset_safe_desc")],
    standard: () => [t("hub.preset_standard"), t("hub.preset_standard_desc")],
    developer: () => [t("hub.preset_developer"), t("hub.preset_developer_desc")],
  };
  const DANGER_TEXT = {
    trust_all: () => t("hub.danger_trust_all"),
    breaker_low: () => t("hub.danger_breaker_low"),
    sycophancy_off: () => t("hub.danger_sycophancy_off"),
    tool_policy_loosened: () => t("policy.danger_loosened"),
    tool_rules_recover: () => t("policy.danger_recover"),
    board_svg: () => t("hub.danger_board_svg"),
    agent_jobs_on: () => t("hub.danger_agent_jobs_on"),
    read_roots_widened: () => t("hub.danger_read_roots_widened"),
    write_roots_widened: () => t("hub.danger_write_roots_widened"),
  };
  // Always / Ask / Never (#190, ADR 0010). The server computes the effective stack;
  // the page only mirrors the saved rule to show unsaved drafts.
  const POLICY_TEXT = {
    always: () => t("policy.always"),
    ask: () => t("policy.ask"),
    never: () => t("policy.never"),
  };
  const DECISION_TEXT = {
    allow: () => [t("policy.decision_allow"), "ok"],
    prompt: () => [t("policy.decision_prompt"), "warn"],
    forbidden: () => [t("policy.decision_forbidden"), "crit"],
  };
  const LAYER_TEXT = {
    rule: () => t("policy.layer_rule"),
    breaker: () => t("policy.layer_breaker"),
    floor: () => t("policy.layer_floor"),
  };
  const REASON_TEXT = {
    rule_always: () => t("policy.reason_rule_always"),
    rule_ask: () => t("policy.reason_rule_ask"),
    rule_never: () => t("policy.reason_rule_never"),
    rule_locked: () => t("policy.reason_rule_locked"),
    rule_always_capped: () => t("policy.reason_rule_always_capped"),
    breaker_critical_halt: () => t("policy.reason_breaker_critical_halt"),
    breaker_critical_readonly: () => t("policy.reason_breaker_critical_readonly"),
    breaker_critical_read: () => t("policy.reason_breaker_critical_read"),
    breaker_normal: () => t("policy.reason_breaker_normal"),
    breaker_none: () => t("policy.reason_breaker_normal"),
    breaker_coherence: () => t("policy.reason_breaker_coherence"),
    breaker_warning: () => t("policy.reason_breaker_warning"),
    breaker_warning_read: () => t("policy.reason_breaker_warning_read"),
    breaker_critical: () => t("policy.reason_breaker_critical"),
    breaker_unknown: () => t("policy.reason_breaker_unknown"),
    exec_not_exposed: () => t("policy.reason_exec_not_exposed"),
    not_configured: () => t("policy.reason_not_configured"),
    floor_constraints: () => t("policy.reason_floor_constraints"),
    floor_none: () => t("policy.reason_floor_none"),
  };
  const ISOLATION_TEXT = {
    os_sandbox: () => [t("policy.isolation_os_sandbox"), "ok"],
    path_confined: () => [t("policy.isolation_path_confined"), "info"],
    none: () => [t("policy.isolation_none"), "warn"],
  };
  const SAVE_ERROR_TEXT = {
    needs_confirmation: () => t("hub.save_needs_confirmation"),
    breaker_active: () => t("policy.err_breaker_active"),
    needs_tool_names: () => t("policy.err_needs_tool_names"),
    audit_failed: () => t("policy.err_audit_failed"),
    tool_rules_locked: () => t("policy.err_locked"),
    // ADR 0012 §8b (only with STIRA_YOLO_ALLOW): the host confirmation's outcome.
    host_confirmation_required: () => t("host.code_host_confirmation_required"),
    stale_operation: () => t("host.code_stale_operation"),
    host_operation_expired: () => t("host.code_host_operation_expired"),
    host_operation_denied: () => t("host.code_host_operation_denied"),
    host_operation_cancelled: () => t("host.code_host_operation_cancelled"),
    host_operation_unknown: () => t("host.code_host_operation_unknown"),
    host_operation_pending: () => t("host.code_host_operation_pending"),
    host_operation_used: () => t("host.code_host_operation_used"),
    host_operation_mismatch: () => t("host.code_host_operation_mismatch"),
    host_rate_limited: () => t("host.code_host_rate_limited"),
    host_channel_unavailable: () => t("host.code_host_channel_unavailable"),
    host_not_acknowledged: () => t("host.code_host_not_acknowledged"),
    host_browser_refused: () => t("host.code_host_browser_refused"),
    host_confirm_on_host: () => t("host.code_host_confirm_on_host"),
    host_operation_incarnation: () => t("host.code_host_operation_incarnation"),
    tool_call_running: () => t("host.code_tool_call_running"),
  };
  const TIMING_TEXT = {
    next_turn: () => t("hub.timing_next_turn"),
    restart: () => t("hub.timing_restart"),
    next_start: () => t("hub.timing_next_start"),
    reload: () => t("hub.timing_reload"),
  };
  const STATUS_TEXT = {
    saved: () => t("hub.status_saved"),
    pending: (s) => t("hub.status_pending", { when: (TIMING_TEXT[s.applies] || TIMING_TEXT.next_start)() }),
    applied: () => t("hub.status_applied"),
    failed: (s) => t("hub.status_failed", {
      reason: (STATUS_REASON_TEXT[s.reason] || (() => s.reason || "?"))(),
    }),
  };
  // Reasons the server names with a code (#512); anything else is shown as sent.
  const STATUS_REASON_TEXT = {
    breaker_pairs_reset: () => t("hub.status_reason_breaker_pairs_reset"),
  };
  const STATUS_STATE = { saved: "info", pending: "warn", applied: "ok", failed: "crit" };
  const FIELD_LABEL = {
    "classifier.choice": () => t("hub.sec_classifier"),
    "breaker.stability_warning": () => t("hub.warn_label"),
    "breaker.stability_critical": () => t("hub.crit_label"),
    "breaker.bc_threshold": () => t("hub.bc_label"),
    "breaker.stability_warning_exit": () => t("hub.warn_exit_label"),
    "breaker.stability_critical_exit": () => t("hub.crit_exit_label"),
    "breaker.bc_exit_threshold": () => t("hub.bc_exit_label"),
    "breaker_details.bc_window_size": () => t("hub.det_bc_window"),
    "breaker_details.critical_cooldown_turns": () => t("hub.det_cooldown"),
    "breaker_details.decay_min_interval_sec": () => t("hub.det_decay"),
    "breaker_details.sycophancy_threshold": () => t("hub.det_syc"),
    "breaker_details.sycophancy_window_size": () => t("hub.det_syc_window"),
    "breaker_details.hitl_timeout_sec": () => t("hub.det_hitl_timeout"),
    "breaker_details.hitl_max_edit_rounds": () => t("hub.det_hitl_rounds"),
    "tools.mode": () => t("hub.mode_legend"),
    "tools.enabled_tools": () => t("hub.tools_title"),
    "tools.ask_tools": () => t("policy.ask"),
    "tools.shell_allowlist": () => t("hub.allowlist_legend"),
    "write.roots": () => t("hub.write_title"),
    "read.roots": () => t("hub.read_title"),
    "board.svg": () => t("hub.board_svg"),
    "output.max_output_tokens": () => t("cost.output_title"),
    "cache.enabled": () => t("cost.cache_title"),
    "cache.ttl": () => t("cost.cache_ttl"),
    "context.enabled": () => t("cost.context_title"),
    ...Object.fromEntries(CONTEXT_OPTIONS.map((name) => [`context.${name}`, () => t(`cost.ctx_${name}`)])),
    "heartbeat.enabled": () => t("hb.enabled"),
    "heartbeat.interval_floor_sec": () => t("hb.floor_label"),
    "heartbeat.interval_ceil_sec": () => t("hb.ceil_label"),
    "delegation.enabled": () => t("hub.delegation_enabled"),
    "delegation.max_tool_calls": () => t("hub.delegation_max_tool_calls"),
    "delegation.max_seconds": () => t("hub.delegation_max_seconds"),
    "delegation.max_children_per_turn": () => t("hub.delegation_max_children"),
    "agent_jobs.enabled": () => t("hub.agent_jobs_enabled"),
    "agent_jobs.agent_daily_token_budget": () => t("hub.agent_jobs_budget"),
    "model.provider": () => t("settings.provider"),
    "model.model_id": () => t("settings.model"),
  };
  const SOURCE_TEXT = {
    manual: () => t("hub.src_manual"),
    defaults: () => t("hub.src_defaults"),
    model: () => t("hub.src_model"),
    "api:tools": () => t("policy.src_api"),
    // ADR 0025 §9: "Make Always" pressed on an approval card.
    approval_card: () => t("hub.src_approval_card"),
  };

  function fmt(value) {
    return typeof value === "number" ? value.toFixed(2) : String(value);
  }
  function toolText(name) {
    return TOOL_TEXT[name] ? TOOL_TEXT[name]() : [name, ""];
  }
  function toast(message, isError = false) {
    const el = $("toast");
    el.textContent = message;
    el.classList.toggle("error", isError);
    el.classList.add("show");
    clearTimeout(toast.timer);
    toast.timer = setTimeout(() => el.classList.remove("show"), 3600);
  }
  async function request(url, options = {}) {
    const response = await stiraFetch(url, { cache: "no-store", credentials: "same-origin", ...options });
    const data = await response.json().catch(() => ({}));
    return { ok: response.ok, status: response.status, data };
  }

  // --- Draft bookkeeping ---------------------------------------------------

  function changeList(saved = state && state.values, next = draft) {
    if (!saved || !next) return [];
    const out = [];
    const draft = next;
    if (draft.classifier.choice !== saved.classifier.choice) {
      out.push({ field: "classifier.choice", from: saved.classifier.choice, to: draft.classifier.choice });
    }
    BREAKER_SET.forEach((key) => {
      if (key in draft.breaker && draft.breaker[key] !== saved.breaker[key]) {
        out.push({ field: `breaker.${key}`, from: saved.breaker[key], to: draft.breaker[key] });
      }
    });
    DETAILS.forEach(([key]) => {
      const before = (saved.breaker_details || {})[key], after = (draft.breaker_details || {})[key];
      if (after !== before) out.push({ field: `breaker_details.${key}`, from: before, to: after });
    });
    if (draft.tools.mode !== saved.tools.mode) {
      out.push({ field: "tools.mode", from: saved.tools.mode, to: draft.tools.mode });
    }
    (state ? state.tools : []).forEach((row) => {
      const from = policyOf(saved.tools, row), to = policyOf(draft.tools, row);
      if (from !== to) out.push({ field: "tools.policy", tool: row.name, from, to });
    });
    const cmdBefore = new Set(saved.tools.shell_allowlist), cmdAfter = new Set(draft.tools.shell_allowlist);
    [...cmdAfter].filter((c) => !cmdBefore.has(c)).forEach((c) => out.push({ field: "tools.shell_allowlist", command: c, to: true }));
    [...cmdBefore].filter((c) => !cmdAfter.has(c)).forEach((c) => out.push({ field: "tools.shell_allowlist", command: c, to: false }));
    const rootBefore = new Set(saved.write.roots), rootAfter = new Set(draft.write.roots);
    [...rootAfter].filter((r) => !rootBefore.has(r)).forEach((r) => out.push({ field: "write.roots", root: r, to: true }));
    [...rootBefore].filter((r) => !rootAfter.has(r)).forEach((r) => out.push({ field: "write.roots", root: r, to: false }));
    const readBefore = new Set(saved.read.roots), readAfter = new Set(draft.read.roots);
    [...readAfter].filter((r) => !readBefore.has(r)).forEach((r) => out.push({ field: "read.roots", root: r, to: true }));
    [...readBefore].filter((r) => !readAfter.has(r)).forEach((r) => out.push({ field: "read.roots", root: r, to: false }));
    if (draft.board.svg !== saved.board.svg) out.push({ field: "board.svg", from: saved.board.svg, to: draft.board.svg });
    ["output", "cache", "context", "heartbeat"].forEach((group) => {
      Object.keys(saved[group] || {}).forEach((key) => {
        if (!same(draft[group][key], saved[group][key])) {
          out.push({ field: `${group}.${key}`, from: saved[group][key], to: draft[group][key] });
        }
      });
    });
    ["enabled", ...DELEGATION_LIMITS.map(([key]) => key)].forEach((key) => {
      if (draft.delegation[key] !== saved.delegation[key]) out.push({ field: `delegation.${key}`, from: saved.delegation[key], to: draft.delegation[key] });
    });
    ["enabled", "agent_daily_token_budget"].forEach((key) => {
      if (draft.agent_jobs[key] !== saved.agent_jobs[key]) out.push({ field: `agent_jobs.${key}`, from: saved.agent_jobs[key], to: draft.agent_jobs[key] });
    });
    return out;
  }
  // Mirror of tool_policy.rule_policy (display of drafts only; the server decides).
  function policyOf(tools, row) {
    const on = tools.mode === "trust_all" || (tools.enabled_tools || []).includes(row.name);
    if (!on) return "never";
    return (tools.ask_tools || []).includes(row.name) || row.always_allowed === false ? "ask" : "always";
  }
  function setPolicy(name, policy) {
    const enabled = new Set(draft.tools.enabled_tools), ask = new Set(draft.tools.ask_tools || []);
    if (policy === "never") { enabled.delete(name); ask.delete(name); }
    else {
      enabled.add(name);
      if (policy === "ask") ask.add(name); else ask.delete(name);
    }
    draft.tools.enabled_tools = [...enabled].sort();
    draft.tools.ask_tools = [...ask].sort();
  }
  const POLICY_RANK = { always: 0, ask: 1, never: 2 };
  // Write/external tools the draft makes Always: their names must be typed (#190).
  function alwaysWrites(saved, next) {
    return state.tools.filter((row) => row.category !== "read"
      && policyOf(next.tools, row) === "always" && policyOf(saved.tools, row) !== "always")
      .map((row) => row.name).sort();
  }
  function typedNames(text) {
    return [...new Set(text.split(/[\s,、]+/).map((n) => n.trim()).filter(Boolean))].sort();
  }
  // Mirror of tool_config.rules_loosening (display only; the server decides).
  function loosensPolicy(saved, next) {
    const before = new Set(saved.tools.shell_allowlist || []);
    if ((next.tools.shell_allowlist || []).some((cmd) => !before.has(cmd))) return true;
    if (saved.tools.mode !== "trust_all" && next.tools.mode === "trust_all") return true;
    return state.tools.some((row) => POLICY_RANK[policyOf(next.tools, row)] < POLICY_RANK[policyOf(saved.tools, row)]);
  }
  function modelDirty() {
    return Boolean(window.StiraModelSection && window.StiraModelSection.dirty());
  }
  function dirtyCount() {
    return changeList().length + (modelDirty() ? 1 : 0);
  }
  function hubChanges() {
    const changes = {};
    HUB_GROUPS.forEach((group) => {
      if (!same(draft[group], state.values[group])) changes[group] = clone(draft[group]);
    });
    return changes;
  }

  // Mirror of settings_hub.validate_values (the server re-checks everything).
  function validate() {
    const errors = [];
    const c = state.constraints;
    const err = (field, code, params = {}) => errors.push({ field, code, params });
    const choice = draft.classifier.choice;
    const option = state.classifiers.find((item) => item.id === choice);
    if (!option) err("classifier.choice", "unknown_choice");
    else if (!option.available && choice !== state.values.classifier.choice) err("classifier.choice", "not_installed");
    const numbers = {};
    BREAKER_SET.forEach((key) => {
      const value = key in draft.breaker ? draft.breaker[key] : state.values.breaker[key];
      if (typeof value !== "number" || !Number.isFinite(value)) return err(`breaker.${key}`, "not_number");
      if (value < 0 || value > 1) return err(`breaker.${key}`, "out_of_range", { min: 0, max: 1 });
      numbers[key] = value;
    });
    // Mirror of settings_hub.validate_values: the pair rules only when a threshold moves
    // (#512: a set reset on load is shown as the file holds it and must not block other saves).
    // Only the pair rules are skipped; the shape and range checks above ran (review r591 m-3).
    const pairs = moved(...BREAKER_SET.map((key) => `breaker.${key}`)) ? numbers : {};
    // An entry below the exit saved with it.
    BREAKER.forEach(([key]) => {
      const exit = EXIT_OF[key];
      if (!(key in pairs) || !(exit in pairs) || pairs[key] < pairs[exit]) return;
      if (pairs[exit] !== state.values.breaker[exit]) err(`breaker.${exit}`, "must_be_above", { limit: pairs[key] });
      else err(`breaker.${key}`, "must_be_below", { limit: pairs[exit] });
    });
    if ("stability_critical" in pairs && "stability_warning" in pairs
        && pairs.stability_critical >= pairs.stability_warning) {
      err("breaker.stability_critical", "must_be_below_field", { limit: pairs.stability_warning });
    }
    if ("stability_critical_exit" in pairs && "stability_warning_exit" in pairs
        && pairs.stability_critical_exit >= pairs.stability_warning_exit) {
      err("breaker.stability_critical_exit", "must_be_below_field", {
        other: "stability_warning_exit", limit: pairs.stability_warning_exit,
      });
    }
    const rules = c.breaker_details || {};
    DETAILS.forEach(([key]) => {
      const value = (draft.breaker_details || {})[key];
      const rule = rules[key];
      if (!rule || value === undefined) return;
      if (rule.integer ? !Number.isInteger(value) : (typeof value !== "number" || !Number.isFinite(value))) {
        return err(`breaker_details.${key}`, rule.integer ? "not_integer" : "not_number");
      }
      const tooLow = value < rule.min || (rule.min_exclusive && value <= rule.min);
      // A value saved above the limit (before it existed) is only checked once it is moved,
      // like the server (#471 review M-1): an unrelated save still works.
      const kept = !moved(`breaker_details.${key}`);
      if (tooLow || (rule.max !== null && rule.max !== undefined && value > rule.max && !kept)) {
        err(`breaker_details.${key}`, "out_of_range", { min: rule.min, max: rule.max });
      }
    });
    if (draft.tools.shell_allowlist.some((cmd) => !cmd.trim() || SHELL_OPERATORS.test(cmd))) {
      err("tools.shell_allowlist", "invalid_command");
    }
    DELEGATION_LIMITS.forEach(([key]) => {
      const value = draft.delegation[key];
      const rule = (c.delegation || {})[key];
      if (!Number.isInteger(value)) return err(`delegation.${key}`, "not_integer");
      if (rule && (value < rule.min || value > rule.max)) err(`delegation.${key}`, "out_of_range", { min: rule.min, max: rule.max });
    });
    {
      const value = draft.agent_jobs.agent_daily_token_budget;
      const rule = (c.agent_jobs || {}).agent_daily_token_budget;
      if (!Number.isInteger(value)) err("agent_jobs.agent_daily_token_budget", "not_integer");
      else if (rule && (value < rule.min || value > rule.max)) err("agent_jobs.agent_daily_token_budget", "out_of_range", { min: rule.min, max: rule.max });
    }
    const roots = draft.write.roots;
    if (roots.length > c.max_write_roots) err("write.roots", "too_many", { max: c.max_write_roots });
    const badRoots = roots.filter((root) => !validRoot(root));
    if (badRoots.length) err("write.roots", "invalid_root", { roots: badRoots });
    // Obvious broad roots; the server also refuses home and its ancestors.
    const broad = roots.filter((root) => ["/", "~", "~/"].includes(root) || /^.:[\\/]?$/.test(root));
    if (!badRoots.length && broad.length) err("write.roots", "broad_root", { roots: broad });
    // #457: read roots may be broad ("/", "~"); the secret-path floor stays.
    const readRoots = draft.read.roots;
    if (!readRoots.length) err("read.roots", "empty");
    else if (readRoots.length > c.max_read_roots) err("read.roots", "too_many", { max: c.max_read_roots });
    const badRead = readRoots.filter((root) => !validRoot(root));
    if (badRead.length) err("read.roots", "invalid_root", { roots: badRead });
    validateCost(err);
    // #472: the heartbeat minutes are rounded into the limits when typed (the other bound
    // follows), so the page has nothing left to refuse; the server re-checks every value.
    return errors;
  }
  // Mirror of settings_hub.field_moved (#470 review M2, #471 review M-1), one rule for every
  // section: the shape of a value is always checked, a bound only on a field the draft moves,
  // so a value already in the file (e.g. a cap above the sanity bound, which the chat server
  // accepts) does not block saving something else. true and 1 differ (typeof).
  function moved(...fields) {
    return fields.some((field) => {
      const [group, key] = field.split(".");
      const saved = (state.values || {})[group] || {};
      if (!(key in saved)) return true;
      const after = ((draft || {})[group] || {})[key];
      return typeof saved[key] !== typeof after || !same(saved[key], after);
    });
  }
  // Mirror of settings_hub._validate_cost (#470); the server re-checks everything.
  function validateCost(report) {
    const c = state.constraints;
    const bound = (field, code, params) => {
      const related = code === "must_be_below_other"
        ? ["context.preview_tokens", "context.offload_threshold_tokens"] : [field];
      if (moved(...related)) report(field, code, params);
    };
    const cap = draft.output.max_output_tokens;
    if (cap !== null) {
      if (!Number.isInteger(cap)) report("output.max_output_tokens", "not_integer");
      else if (cap < c.max_output_tokens.min || cap > c.max_output_tokens.max) {
        bound("output.max_output_tokens", "out_of_range", c.max_output_tokens);
      }
    }
    const good = {};
    CONTEXT_OPTIONS.forEach((name) => {
      const rule = c.context[name], value = draft.context[name], field = `context.${name}`;
      if (value === null && rule.nullable) { good[name] = value; return; }
      if (rule.kind === "int" && !Number.isInteger(value)) return report(field, "not_integer");
      if (typeof value !== "number" || !Number.isFinite(value)) return report(field, "not_number");
      const low = rule.min_open ? value <= rule.min : value < rule.min;
      if (low || (rule.max !== null && value > rule.max)) {
        if (rule.max === null) return bound(field, "too_small", { min: rule.min });
        return bound(field, rule.min_open ? "out_of_range_open" : "out_of_range", { min: rule.min, max: rule.max });
      }
      good[name] = value;
    });
    if (typeof good.preview_tokens === "number" && typeof good.offload_threshold_tokens === "number"
        && good.preview_tokens >= good.offload_threshold_tokens) {
      bound("context.preview_tokens", "must_be_below_other", { limit: good.offload_threshold_tokens });
    }
  }
  // Mirror of settings_hub._valid_root: @workspace, an absolute path or ~/...
  function validRoot(root) {
    if (root === "@workspace") return true;
    if (!root.trim() || root !== root.trim() || root.includes("\0")) return false;
    const text = root.replace(/\\/g, "/");
    return text.startsWith("/") || text.startsWith("~/") || text === "~" || /^.:\//.test(text);
  }
  function dangersOf(values) {
    const found = [];
    if (values.tools.mode === "trust_all") found.push("trust_all");
    const floors = state.constraints.danger_floors;
    if (Object.keys(floors).some((key) => values.breaker[key] < floors[key])) found.push("breaker_low");
    // Mirror of settings_hub.sycophancy_off (#471 review M-1): the alert practically off.
    const syc = state.constraints.sycophancy_danger, details = values.breaker_details || {};
    if (syc && ((typeof details.sycophancy_threshold === "number"
        && details.sycophancy_threshold >= syc.threshold_at_least)
      || (Number.isInteger(details.sycophancy_window_size) && details.sycophancy_window_size < syc.window_below))) {
      found.push("sycophancy_off");
    }
    return found;
  }
  function newDangers() {
    const before = new Set(dangersOf(state.values));
    const found = dangersOf(draft).filter((code) => !before.has(code));
    // Mirror of settings_hub.board_turned_on (#453): turning the board's pictures back on
    // (on is the default, ADR 0012 addendum 2026-10-24) is confirmed; turning them off is not.
    if (draft.board && draft.board.svg === true && !(state.values.board && state.values.board.svg === true)) {
      found.push("board_svg");
    }
    // Mirror of settings_hub.new_dangers: loosening a rule needs the typed confirmation.
    if (!found.includes("trust_all") && loosensPolicy(state.values, draft)) found.push("tool_policy_loosened");
    // Mirror of settings_hub.read_roots_widened (#457): every added read root is confirmed.
    const readSaved = new Set(state.values.read.roots);
    if (draft.read.roots.some((root) => !readSaved.has(root))) found.push("read_roots_widened");
    // Mirror of settings_hub.write_roots_widened (#536): every added write root too.
    const writeSaved = new Set(state.values.write.roots);
    if (draft.write.roots.some((root) => !writeSaved.has(root))) found.push("write_roots_widened");
    return found;
  }

  // --- Rendering -----------------------------------------------------------

  function renderErrors(clientErrors) {
    const all = [...clientErrors, ...serverErrors];
    document.querySelectorAll(".field-error").forEach((el) => { el.hidden = true; el.textContent = ""; });
    document.querySelectorAll("[aria-invalid]").forEach((el) => el.removeAttribute("aria-invalid"));
    all.forEach((error) => {
      const target = $(`err-${error.field.replace(".", "-")}`) || $(`err-${error.field.split(".")[0]}`);
      if (!target) return;
      const text = (ERROR_TEXT[error.code] || (() => t("hub.err_invalid")))(error.params || {});
      target.textContent = target.textContent ? `${target.textContent} ${text}` : text;
      target.hidden = false;
      const [group, key] = error.field.split(".");
      if (group === "breaker" && key && INPUT_OF[key]) $(INPUT_OF[key]).setAttribute("aria-invalid", "true");
      if (group === "breaker_details" && key) {
        const row = DETAILS.find(([name]) => name === key);
        if (row) $(row[1]).setAttribute("aria-invalid", "true");
      }
      const costInput = COST_INPUTS[error.field] && $(COST_INPUTS[error.field]);
      if (costInput) costInput.setAttribute("aria-invalid", "true");
      const hbInput = HB_INPUTS[error.field] && $(HB_INPUTS[error.field]);
      if (hbInput) hbInput.setAttribute("aria-invalid", "true");
    });
    return all.length;
  }

  function renderClassifier() {
    const box = $("classifier-choices");
    box.querySelectorAll(".choice").forEach((el) => el.remove());
    state.classifiers.forEach((item) => {
      const [label, desc] = CLASSIFIER_TEXT[item.id] ? CLASSIFIER_TEXT[item.id]() : [item.id, ""];
      const row = document.createElement("label");
      row.className = "choice";
      const input = document.createElement("input");
      input.type = "radio";
      input.name = "classifier-choice";
      input.value = item.id;
      input.checked = draft.classifier.choice === item.id;
      input.disabled = !item.available && state.values.classifier.choice !== item.id;
      input.addEventListener("change", () => {
        draft.classifier.choice = item.id;
        serverErrors = [];
        update();
      });
      const text = document.createElement("span");
      const strong = document.createElement("strong");
      strong.textContent = label;
      const hint = document.createElement("span");
      hint.className = "hint";
      hint.textContent = item.available ? desc : `${desc} ${t("settings.not_installed")}`;
      const code = document.createElement("code");
      code.className = "internal-name";
      code.textContent = item.id;
      text.append(strong, hint, code);
      row.append(input, text);
      box.append(row);
    });
    const status = state.apply.classifier || {};
    const name = (id) => (CLASSIFIER_NAMES[id] || (() => id))();
    if (!status.effective) $("classifier-effective").textContent = t("hub.cls_running_unknown");
    else if (status.effective === "unresolved") {
      $("classifier-effective").textContent = t("hub.cls_running_expected", { name: name(status.expected) });
    } else $("classifier-effective").textContent = t("hub.cls_running", { name: name(status.effective) });
  }

  // #471: what the last rounding changed, per pair / detail (cleared with the draft).
  let breakerNotes = {};
  const PAIR_SUMMARY = {
    stability_warning: (p) => t("hub.pair_sum_warning", p),
    stability_critical: (p) => t("hub.pair_sum_critical", p),
    bc_threshold: (p) => t("hub.pair_sum_bc", p),
  };
  const NOTE_TEXT = {
    exit_raised: (p) => t("hub.note_exit_raised", p),
    exit_clamped: (p) => t("hub.note_exit_clamped", p),
    warning_exit_raised: (p) => t("hub.note_warning_exit_raised", p),
    rounded: (p) => t("hub.note_rounded", p),
    saved_above_limit: (p) => t("hub.note_saved_above_limit", p),
    not_number: () => t("hub.err_not_number"),
  };
  const FIXED_TEXT = {
    protected_layers: () => t("hub.fixed_protected_layers"),
    critical_action: () => t("hub.fixed_critical_action"),
    blocked_tool_names: () => t("hub.fixed_blocked_tools"),
  };
  const FIXED_REASON = {
    layer_protection: () => t("hub.fixed_reason_layers"),
    server_fixed: () => t("hub.fixed_reason_server"),
  };
  const CRITICAL_ACTION_TEXT = {
    halt: () => t("hub.critical_action_halt"),
    readonly: () => t("hub.critical_action_readonly"),
  };
  const CLAMP_TEXT = {
    bc_window_clamped: () => t("hub.clamped_bc_window"),
    hitl_timeout_clamped: () => t("hub.clamped_hitl_timeout"),
    protected_layers_clamped: () => t("hub.clamped_protected_layers"),
  };
  function fmtDetail(value) {
    if (typeof value !== "number") return String(value);
    return Number.isInteger(value) ? String(value) : String(Math.round(value * 100) / 100);
  }
  function showNote(id, text) {
    const el = $(id);
    if (!el) return;
    el.textContent = text || "";
    el.hidden = !text;
  }

  function renderBreaker() {
    BREAKER.forEach(([key, id]) => {
      const exit = EXIT_OF[key];
      const entryValue = draft.breaker[key], exitValue = draft.breaker[exit];
      $(id).value = entryValue;
      $(`${id}-value`).textContent = fmt(entryValue);
      $(`${id}-exit`).value = exitValue;
      $(`${id}-exit-value`).textContent = fmt(exitValue);
      const band = Pairs.band(entryValue, exitValue);
      $(`${id}-band`).style.left = `${band.left}%`;
      $(`${id}-band`).style.width = `${band.width}%`;
      $(`${id}-mark`).style.left = `${band.left}%`;
      $(`${id}-exit-mark`).style.left = `${band.left + band.width}%`;
      $(`pair-sum-${id}`).textContent = PAIR_SUMMARY[key]({
        enter: fmt(entryValue), exit: fmt(exitValue),
        turns: (draft.breaker_details || {}).critical_cooldown_turns,
      });
      const note = breakerNotes[key];
      showNote(`note-${id}`, note ? NOTE_TEXT[note.code](note.params) : "");
    });
    DETAILS.forEach(([key, id]) => {
      const input = $(id);
      const rule = (state.constraints.breaker_details || {})[key] || {};
      input.min = rule.min_exclusive ? String(rule.min + 1) : String(rule.min);
      if (rule.max !== null && rule.max !== undefined) input.max = String(rule.max);
      if (document.activeElement !== input) {
        const value = (draft.breaker_details || {})[key];
        input.value = value === null || value === undefined ? "" : fmtDetail(value);
      }
      const savedValue = (state.values.breaker_details || {})[key];
      const aboveLimit = rule.max !== null && rule.max !== undefined && typeof savedValue === "number"
        && savedValue > rule.max && (draft.breaker_details || {})[key] === savedValue;
      const note = breakerNotes[key]
        || (aboveLimit ? { code: "saved_above_limit", params: { value: fmtDetail(savedValue), max: fmtDetail(rule.max) } } : null);
      showNote(`note-${id}`, note ? NOTE_TEXT[note.code](note.params) : "");
    });
    // #512: the file's set broke a pair rule; its values are shown, the defaults run.
    showNote("breaker-reset-note", state.breaker_pairs_reset === true ? t("hub.note_breaker_pairs_reset") : "");
    const clampList = $("breaker-clamp-notes");
    clampList.replaceChildren();
    Object.keys(CLAMP_TEXT).filter((flag) => state[flag] === true).forEach((flag) => {
      const li = document.createElement("li");
      li.textContent = CLAMP_TEXT[flag]();
      clampList.append(li);
    });
    clampList.hidden = clampList.children.length === 0;
    if (!clampList.hidden) $("breaker-details").open = true;
    const fixed = $("breaker-fixed");
    fixed.replaceChildren();
    (state.breaker_fixed || []).forEach((row) => {
      const div = document.createElement("div");
      const dt = document.createElement("dt");
      const name = document.createElement("span");
      name.textContent = (FIXED_TEXT[row.name] || (() => row.name))();
      const reason = document.createElement("span");
      reason.className = "hint";
      reason.textContent = (FIXED_REASON[row.reason] || (() => row.reason))();
      const code = document.createElement("code");
      code.className = "internal-name";
      code.textContent = row.name;
      dt.append(name, reason, code);
      const dd = document.createElement("dd");
      if (row.name === "critical_action") {
        dd.textContent = (CRITICAL_ACTION_TEXT[row.value] || (() => String(row.value)))();
      } else if (Array.isArray(row.value) && row.name === "blocked_tool_names") {
        // The list is long: a count, with the names one click away.
        const names = document.createElement("details");
        const summary = document.createElement("summary");
        summary.textContent = t("hub.fixed_count", { n: row.value.length });
        const list = document.createElement("code");
        list.className = "fixed-names";
        list.textContent = row.value.join(", ");
        names.append(summary, list);
        dd.append(names);
      } else {
        dd.textContent = Array.isArray(row.value) ? row.value.join(", ") : String(row.value);
      }
      div.append(dt, dd);
      fixed.append(div);
    });
  }

  // A moved slider (#471): keep the pair valid on the safe side and say what moved.
  function moveSlider(key, which, raw) {
    const entryKey = which === "entry" ? key : Object.keys(EXIT_OF).find((name) => EXIT_OF[name] === key);
    const exitKey = EXIT_OF[entryKey];
    const before = { entry: draft.breaker[entryKey], exit: draft.breaker[exitKey] };
    const gap = state.values.breaker[exitKey] - state.values.breaker[entryKey];
    const next = Pairs.roundPair(
      which === "entry" ? raw : before.entry, which === "exit" ? raw : before.exit, which, gap,
    );
    draft.breaker[entryKey] = next.entry;
    draft.breaker[exitKey] = next.exit;
    delete breakerNotes[entryKey];
    if (next.note) breakerNotes[entryKey] = { code: next.note, params: { exit: fmt(next.exit), enter: fmt(next.entry) } };
    if (entryKey === "stability_critical" || entryKey === "stability_warning") {
      const order = Pairs.orderExits(draft.breaker.stability_warning_exit, draft.breaker.stability_critical_exit);
      if (order.note) {
        draft.breaker.stability_warning_exit = order.warningExit;
        breakerNotes.stability_warning = { code: order.note, params: { exit: fmt(order.warningExit) } };
      }
    }
  }

  function badge(text, stateName, extraClass = "") {
    const el = document.createElement("span");
    el.className = `badge ${extraClass}`.trim();
    el.dataset.state = stateName;
    el.textContent = text;
    return el;
  }

  // One line per fact: effective decision (+ deciding layers), isolation, grant.
  // #281: the three lines sit in a <details> (closed by default) whose summary keeps the
  // effective decision visible; a tool whose effective decision is stricter than the
  // current choice (breaker / floor / not exposed) is always open and marked. Display only.
  const DECISION_RANK = { allow: 0, prompt: 1, forbidden: 2 };
  const RULE_DECISION = { always: "allow", ask: "prompt", never: "forbidden" };
  const factsOpen = new Map();  // tool name -> open/closed chosen by the user (kept across re-renders)
  // Compared with the current choice (the draft), not only the saved rule.
  function stricterThanRule(row, choice) {
    const eff = (row.effective || {}).decision;
    const rank = Object.hasOwn(DECISION_RANK, eff) ? DECISION_RANK[eff] : DECISION_RANK.forbidden;
    const rule = RULE_DECISION[choice];
    return rule !== undefined && rank > DECISION_RANK[rule];
  }
  function stackFacts(row, draftPolicy) {
    const facts = document.createElement("dl");
    facts.className = "policy-facts";
    const add = (label, ...nodes) => {
      const wrap = document.createElement("div");
      const dt = document.createElement("dt");
      dt.textContent = label;
      const dd = document.createElement("dd");
      dd.append(...nodes);
      wrap.append(dt, dd);
      facts.append(wrap);
    };
    const eff = row.effective || { decision: "forbidden", decided_by: [], reasons: [] };
    const [decision, decisionState] = (DECISION_TEXT[eff.decision] || DECISION_TEXT.forbidden)();
    const why = document.createElement("span");
    why.className = "hint";
    why.textContent = t("policy.decided_by", {
      layers: eff.decided_by.map((key) => (LAYER_TEXT[key] || (() => key))()).join(" + "),
      reasons: eff.reasons.map((code) => (REASON_TEXT[code] || (() => code))()).join(" / "),
    });
    const nodes = [badge(decision, decisionState, "effective"), " ", why];
    const unsaved = draftPolicy !== row.policy;
    if (unsaved) nodes.push(" ", badge(t("hub.tool_unsaved"), "warn"));
    add(t("policy.effective"), ...nodes);
    const [iso, isoState] = (ISOLATION_TEXT[(row.isolation || {}).kind] || ISOLATION_TEXT.none)();
    add(t("policy.isolation"), badge(iso, isoState), " ",
      Object.assign(document.createElement("span"), { className: "hint", textContent: t("policy.isolation_not_approval") }));
    if (row.grant) {
      // The scope / expiry / revoke text is the same for every tool: shown once at the top.
      const grant = document.createElement("span");
      grant.className = "hint";
      grant.textContent = t("policy.grant_short");
      add(t("policy.grant"), grant);
    } else if (row.policy === "always" && row.always_allowed === false) {
      add(t("policy.grant"), document.createTextNode(t("policy.grant_unbounded")));
    }
    const stricter = stricterThanRule(row, draftPolicy);
    const details = document.createElement("details");
    details.className = "policy-details";
    details.dataset.tool = row.name;
    if (stricter) details.dataset.stricter = "true";
    // Stricter than the choice: always open (over any remembered state). Otherwise the
    // user's last open/close for this tool, closed by default.
    const initial = stricter || (factsOpen.has(row.name) && factsOpen.get(row.name));
    details.open = initial;
    // Remember only the user's toggles on rows that are not forced open; the "toggle"
    // queued by opening it here is skipped.
    let ownToggle = initial;
    details.addEventListener("toggle", () => {
      if (ownToggle) { ownToggle = false; if (details.open) return; }
      if (!stricter) factsOpen.set(row.name, details.open);
    });
    const summary = document.createElement("summary");
    const caption = document.createElement("span");
    caption.className = "policy-details-label";
    caption.textContent = t("hub.details");
    summary.append(caption, " ", document.createTextNode(`${t("policy.effective")}: `),
      badge(decision, decisionState, "effective"));
    if (stricter) summary.append(" ", badge(t("policy.stricter_than_rule"), "warn", "stricter"));
    if (unsaved) summary.append(" ", badge(t("hub.tool_unsaved"), "warn"));
    details.append(summary, facts);
    return details;
  }

  function policyControl(row, label, disabled) {
    const group = document.createElement("fieldset");
    group.className = "policy-choice";
    const legend = document.createElement("legend");
    legend.className = "visually-hidden";
    legend.textContent = t("policy.legend", { name: label });
    group.append(legend);
    const current = policyOf(draft.tools, row);
    ["always", "ask", "never"].forEach((policy) => {
      const option = document.createElement("label");
      option.className = "policy-option";
      const input = document.createElement("input");
      input.type = "radio";
      input.name = `policy-${row.name}`;
      input.value = policy;
      input.checked = current === policy;
      // Exec-class tools cannot be Always: no bounded scope can be stated (ADR 0010).
      input.disabled = disabled || (policy === "always" && row.always_allowed === false);
      input.addEventListener("change", () => {
        if (!input.checked) return;
        setPolicy(row.name, policy);
        serverErrors = [];
        update();
        const again = document.querySelector(`input[name="policy-${CSS.escape(row.name)}"][value="${policy}"]`);
        if (again) again.focus();
      });
      const text = document.createElement("span");
      text.textContent = POLICY_TEXT[policy]();
      option.append(input, text);
      group.append(option);
    });
    return group;
  }

  function renderTools() {
    const trust = draft.tools.mode === "trust_all";
    document.querySelectorAll('input[name="tool-mode"]').forEach((input) => {
      input.checked = input.value === draft.tools.mode;
    });
    ["read", "write", "exec"].forEach((category) => $(`${category}-tools`).replaceChildren());
    state.tools.forEach((row) => {
      const [label, desc] = toolText(row.name);
      const item = document.createElement("li");
      item.className = "tool-item";
      item.dataset.search = `${row.name} ${label} ${desc}`.toLowerCase();
      const wrap = document.createElement("div");
      wrap.className = "tool-row policy-row";
      const text = document.createElement("span");
      text.className = "tool-text";
      const strong = document.createElement("strong");
      strong.textContent = label;
      const hint = document.createElement("span");
      hint.className = "hint";
      hint.textContent = desc;
      const code = document.createElement("code");
      code.className = "internal-name";
      code.textContent = row.name;
      text.append(strong, code, hint);
      wrap.append(text, policyControl(row, label, trust || row.exposed === false));
      item.append(wrap, stackFacts(row, policyOf(draft.tools, row)));
      $(`${row.category}-tools`).append(item);
    });
    renderFloor();
    const locked = $("tools-locked");
    locked.hidden = !state.tools_locked;
    locked.textContent = state.tools_locked ? t("policy.locked_notice", { reason: state.tools_locked }) : "";
    $("exec-tools-notice").textContent = t(
      state.exec_tools_enabled ? "tools.exec_on_hint" : "tools.exec_hint",
      { env: state.exec_env_var || "STIRA_ENABLE_EXEC_TOOLS" },
    );
    const shell = state.tools.find((row) => row.name === "shell");
    $("shell-editor").disabled = trust || !shell || shell.exposed === false
      || !draft.tools.enabled_tools.includes("shell");
    renderAllowlist();
    applyFilter();
  }

  // Read-only: no setting or approval lifts the floor (#190). No inputs here.
  function renderFloor() {
    const list = $("floor-list");
    if (!list) return;
    list.replaceChildren();
    (state.floor || []).forEach((item) => {
      const li = document.createElement("li");
      const strong = document.createElement("strong");
      const hint = document.createElement("span");
      hint.className = "hint";
      const extra = [];
      if (item.id === "identity_read_only") {
        strong.textContent = t("policy.floor_identity");
        hint.textContent = t("policy.floor_identity_desc", {
          files: (item.files || []).join(", "), n: item.extra_count || 0,
        });
      } else if (item.id === "os_protection") {
        strong.textContent = t("policy.floor_os");
        hint.textContent = t("policy.floor_os_desc", { commands: (item.commands || []).join(", ") });
      } else if (item.id === "data_directory") {
        strong.textContent = t("policy.floor_data_dir");
        const dirs = item.writable || [];
        hint.textContent = dirs.length
          ? t("policy.floor_data_dir_desc", { dirs: dirs.join(", ") })
          : t("policy.floor_data_dir_none");
        if (Array.isArray(item.readable)) {
          // Where file tools may read, next to where they may write (#312). Read-only.
          const readHint = document.createElement("span");
          readHint.className = "hint";
          readHint.textContent = item.readable.length
            ? t("policy.floor_data_dir_read_desc", { dirs: item.readable.join(", ") })
            : t("policy.floor_data_dir_read_none");
          extra.push(readHint);
        }
      } else if (item.id === "exec_exposure") {
        strong.textContent = t("policy.floor_exec");
        hint.textContent = t(item.exposed ? "policy.floor_exec_on" : "policy.floor_exec_off", { env: item.env });
      } else {
        strong.textContent = item.id;
      }
      li.append(strong, badge(t("policy.floor_locked"), "info"), hint, ...extra);
      list.append(li);
    });
  }

  // Saved allowlist entries holding a secret (#270): command -> masked text from the
  // server. Shown masked; the tool rules are not saved again until they are removed.
  function savedSecrets() {
    const saved = (state && state.values.tools.shell_allowlist) || [];
    const masks = new Map();
    ((state && state.secret_allowlist) || []).forEach((mark) => {
      if (typeof saved[mark.index] === "string") masks.set(saved[mark.index], mark.masked);
    });
    return masks;
  }
  function shownCommand(command) {
    const masks = savedSecrets();
    return masks.has(command) ? masks.get(command) : command;
  }
  // Outside the shell editor, so the removal works even when the editor is disabled.
  function renderSecretNotice() {
    const masks = savedSecrets();
    const left = draft.tools.shell_allowlist.filter((c) => masks.has(c)).length;
    $("allowlist-secrets").hidden = left === 0;
    $("allowlist-secrets-text").textContent = left ? t("policy.secret_saved_notice", { count: left }) : "";
  }

  function renderAllowlist() {
    const list = $("allowlist");
    list.replaceChildren();
    const masks = savedSecrets();
    draft.tools.shell_allowlist.forEach((command) => {
      const item = document.createElement("li");
      const code = document.createElement("code");
      const remove = document.createElement("button");
      const shown = masks.has(command) ? masks.get(command) : command;
      code.textContent = shown;
      remove.type = "button";
      remove.className = "btn btn-ghost";
      remove.textContent = t("tools.remove");
      remove.setAttribute("aria-label", t("tools.remove_command", { command: shown }));
      remove.addEventListener("click", () => {
        draft.tools.shell_allowlist = draft.tools.shell_allowlist.filter((c) => c !== command);
        update();
        $("new-command").focus();
      });
      if (masks.has(command)) item.append(code, badge(t("policy.secret_saved_badge"), "crit"), remove);
      else item.append(code, remove);
      list.append(item);
    });
    renderSecretNotice();
  }

  function writeScopeOf(roots) {
    const scopes = state.constraints.write_scopes;
    return Object.keys(scopes).find((id) => same(scopes[id], roots)) || "custom";
  }
  function renderWrite() {
    const scope = writeScopeChoice || writeScopeOf(draft.write.roots);
    document.querySelectorAll('input[name="write-scope"]').forEach((input) => {
      input.checked = input.value === scope;
    });
    const box = $("write-roots");
    if (document.activeElement !== box) box.value = draft.write.roots.join("\n");
    // Built-in scopes are shown, not edited; "custom" makes the list editable.
    box.readOnly = scope !== "custom";
    $("write-workspace-path").textContent = state.workspace
      ? t("hub.write_workspace_path", { path: state.workspace })
      : t("hub.write_workspace_unknown");
  }
  function renderRead() {
    const scopes = state.constraints.read_scopes;
    const derived = Object.keys(scopes).find((id) => same(scopes[id], draft.read.roots)) || "custom";
    const scope = readScopeChoice || derived;
    document.querySelectorAll('input[name="read-scope"]').forEach((input) => {
      input.checked = input.value === scope;
    });
    const box = $("read-roots");
    if (document.activeElement !== box) box.value = draft.read.roots.join("\n");
    box.readOnly = scope !== "custom";
  }

  // #453: the Stage board's static SVG switch (saved with the footer's "Save", confirmed)
  function renderBoard() {
    $("board-svg").checked = draft.board.svg === true;
    const note = $("board-svg-stage");
    note.hidden = state.stage_enabled !== false;
    note.textContent = state.stage_enabled === false ? t("hub.board_stage_off") : "";
  }

  // #470: output cap (automatic = recommended), prompt cache and context management.
  function fmtTokens(n) {
    return typeof n === "number" ? n.toLocaleString(document.documentElement.lang || undefined) : String(n);
  }
  function capWarning(cap, info) {
    // #416: automatic with no recorded limit = the provider's default; say what that risks.
    if (cap === null && info.auto_max_output_source === "provider_default") return t("cost.output_auto_warn_unknown");
    if (cap === null || !Number.isInteger(cap)) return "";
    if (info.model_max_output === null || info.model_max_output === undefined) {
      return t("cost.output_warn_unknown", { auto: fmtTokens(info.auto_max_output_tokens) });
    }
    if (cap > info.model_max_output) return t("cost.output_warn_over", { limit: fmtTokens(info.model_max_output) });
    return "";
  }
  function renderCost() {
    const info = (state.cost && state.cost.model) || {};
    const surfaceFields = (state.surface && state.surface.fields) || {};
    const cap = draft.output.max_output_tokens;
    const auto = cap === null;
    $("cost-output-auto").checked = auto;
    $("cost-output-fixed").checked = !auto;
    const autoN = fmtTokens(info.auto_max_output_tokens);
    $("cost-output-auto-desc").textContent = info.auto_max_output_source === "provider_default"
      ? t("cost.output_auto_desc_unknown", { n: autoN })
      : Number.isInteger(info.thinking_reserve) && info.thinking_reserve > 0
        ? t("cost.output_auto_desc_thinking", { n: autoN, reserve: fmtTokens(info.thinking_reserve) })
        : t("cost.output_auto_desc", { n: autoN });
    const input = $("cost-max-output");
    input.disabled = auto;
    if (document.activeElement !== input) input.value = auto ? String(info.auto_max_output_tokens ?? "") : String(cap);
    const limit = info.model_max_output;
    $("cost-output-desc").textContent = [
      limit ? t("cost.output_model_limit", { limit: fmtTokens(limit) }) : t("cost.output_model_limit_unknown"),
      info.thinking_uses_output_budget ? t("cost.output_thinking") : "",
    ].filter(Boolean).join(" ");
    const warning = capWarning(cap, info);
    $("cost-max-output-warn").hidden = !warning;
    $("cost-max-output-warn").textContent = warning;

    $("cost-cache-enabled").checked = draft.cache.enabled === true;
    $("cost-cache-ttl").value = draft.cache.ttl;
    const unsupported = $("cost-cache-unsupported");
    unsupported.hidden = info.prompt_cache_supported !== false;
    unsupported.textContent = info.prompt_cache_supported === false ? t("cost.cache_unsupported") : "";

    $("cost-context-enabled").checked = draft.context.enabled === true;
    renderContextProfile((state.cost && state.cost.context_profile) || {});
    renderTaskModels((state.cost && state.cost.task_models) || {});
    CONTEXT_OPTIONS.forEach((name) => {
      const el = $(`cost-ctx-${name}`);
      const rule = state.constraints.context[name];
      if (rule.max !== null) el.max = String(rule.max);
      const value = draft.context[name];
      if (document.activeElement !== el) el.value = value === null || value === undefined ? "" : String(value);
    });
    // Pinned by an environment variable: the shared surface part (settings_surface.js
    // applyPins, run on every "stira:hub-state" this page sends after a render) turns the
    // input off and names the variable (never its value). Here only: a pinned control shows
    // what the running agent uses (the saved value is kept for later).
    Object.entries(COST_PIN_TARGETS).forEach(([field, [inputId, liveId]]) => {
      const input = $(inputId);
      const meta = surfaceFields[input.getAttribute("data-surface-field")] || {};
      const pinned = Boolean(meta.pinned_by);
      const [group, key] = field.split(".");
      const running = (state.apply[group] && state.apply[group].running) || {};
      const live = pinned ? running[key] : undefined;
      if (input.type === "checkbox") input.indeterminate = false;
      const known = live !== undefined && live !== null;
      if (known) {
        if (input.type === "checkbox") input.checked = live === true;
        else input.value = live;
      } else if (pinned) {
        // No running agent to ask: the env value is not shown (and the saved value would be
        // the wrong one), so the control shows "not known here" instead.
        if (input.type === "checkbox") { input.checked = false; input.indeterminate = true; }
        else input.selectedIndex = -1;
      }
      $(liveId).hidden = !pinned;
      $(liveId).textContent = !pinned ? "" : known ? t("cost.pinned_live") : t("cost.pinned_unknown");
    });
  }
  // #368: read-only. What the running server started with, and the next start's profile
  // when it differs (a saved file or the variable changed since).
  function contextProfileText(p) {
    if (!p) return t("cost.profile_unknown");
    const parts = [];
    if (p.max_tokens) parts.push(t("cost.profile_budget", { tokens: fmtTokens(p.max_tokens) }));
    if (p.episodic_limit !== null && p.episodic_limit !== undefined) parts.push(t("cost.profile_episodic", { n: p.episodic_limit }));
    if (p.procedural_limit !== null && p.procedural_limit !== undefined) parts.push(t("cost.profile_procedural", { n: p.procedural_limit }));
    if (p.send_allowed_tools_only) parts.push(t("cost.profile_tools_allowed"));
    if (p.short_tool_schemas) parts.push(t("cost.profile_tools_short"));
    const detail = parts.length ? parts.join(t("cost.profile_sep")) : t("cost.profile_no_change");
    const pin = p.pinned_by ? t("cost.profile_pinned", { name: p.pinned_by }) : "";
    return [t("cost.profile_current", { name: p.name, detail }), pin].filter(Boolean).join(" ");
  }
  function renderContextProfile(info) {
    const running = info.running || null;
    const next = info.next_start || null;
    $("cost-profile-current").textContent = contextProfileText(running || next);
    const differs = running && next && (running.name !== next.name || running.send_allowed_tools_only !== next.send_allowed_tools_only || Boolean(running.short_tool_schemas) !== Boolean(next.short_tool_schemas));
    $("cost-profile-next").hidden = !differs;
    $("cost-profile-next").textContent = differs ? t("cost.profile_next", { name: next.name }) : "";
  }
  // docs/task-models.md (read-only): the model each side task uses and where it comes from.
  function taskModelText(row) {
    if (row.error) return t("cost.task_model_error", { task: row.task, code: row.error });
    const model = row.model_id ? (row.provider ? row.provider + ":" + row.model_id : row.model_id) : t("cost.task_model_chat");
    const key = row.inherit ? "cost.task_model_inherit" : row.source === "env" ? "cost.task_model_env" : "cost.task_model_config";
    return t(key, { task: row.task, model, env: row.env || "" });
  }
  function renderTaskModels(info) {
    const rows = info.running || info.next_start || [];
    const list = $("cost-task-models-list");
    if (!list) return;
    list.replaceChildren(...rows.map((row) => {
      const li = document.createElement("li");
      li.textContent = taskModelText(row);
      return li;
    }));
    const next = info.next_start || [];
    const live = Array.isArray(info.running);
    // A saved setting that would refuse the next start, named with its code (the running
    // rows above are fine); else a short note when the next start differs.
    const refused = live ? next.filter((row) => row.error) : [];
    const differs = live && next.length > 0 && next.some((row, i) => {
      const now = info.running[i] || {};
      return row.task !== now.task || row.model_id !== now.model_id || row.provider !== now.provider || row.error !== now.error;
    });
    const text = refused.length
      ? refused.map((row) => t("cost.task_model_next_error", { task: row.task, code: row.error })).join(" ")
      : differs ? t("cost.task_models_next") : "";
    $("cost-task-models-next").hidden = !text;
    $("cost-task-models-next").textContent = text;
  }
  function numberOrNaN(text) {
    const trimmed = String(text).trim();
    return trimmed === "" ? NaN : Number(trimmed);
  }
  // #472: the heartbeat (zero-model tick): switch, interval bounds, the running status.
  let hbNote = null; // what the last rounding changed: { code, params }
  function fmtDuration(seconds) {
    if (typeof seconds !== "number" || !Number.isFinite(seconds)) return String(seconds);
    const lang = document.documentElement.lang || undefined;
    if (seconds >= 3600 && seconds % 3600 === 0) return t("hb.unit_hours", { n: (seconds / 3600).toLocaleString(lang) });
    const minutes = Math.round((seconds / 60) * 100) / 100;
    return t("hb.unit_minutes", { n: minutes.toLocaleString(lang) });
  }
  function minutesOf(seconds) {
    return typeof seconds === "number" && Number.isFinite(seconds) ? String(Math.round((seconds / 60) * 100) / 100) : "";
  }
  function fmtClock(epoch) {
    if (typeof epoch !== "number" || !Number.isFinite(epoch)) return null;
    const lang = document.documentElement.lang || undefined;
    return new Date(epoch * 1000).toLocaleString(lang, { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" });
  }
  const HB_ERROR_TEXT = {
    HeartbeatLockError: () => t("hb.err_lock"),
    UnsafeLockFileError: () => t("hb.err_unsafe_lock"),
    HeartbeatIntervalsError: () => t("hb.err_intervals_reset"),
  };
  const HB_LEVEL_TEXT = {
    normal: () => t("hb.level_normal"),
    warning: () => t("hb.level_warning"),
    critical: () => t("hb.level_critical"),
  };
  function renderHeartbeatLive() {
    const live = state.heartbeat && state.heartbeat.live;
    const summary = $("hb-live-summary");
    const facts = $("hb-live-facts");
    facts.textContent = "";
    const fact = (label, value) => {
      if (value === null || value === undefined || value === "") return;
      const row = document.createElement("div");
      const dt = document.createElement("dt");
      dt.textContent = label;
      const dd = document.createElement("dd");
      dd.textContent = value;
      row.append(dt, dd);
      facts.append(row);
    };
    if (!live) {
      summary.dataset.state = "info";
      summary.textContent = t("hb.live_unknown");
      facts.hidden = true;
      return;
    }
    let text, stateName;
    if (live.running) {
      stateName = live.degraded ? "warn" : "ok";
      text = live.degraded ? t("hb.live_degraded", { n: live.consecutive_failures || 0 }) : t("hb.live_running");
    } else if (live.enabled && live.last_error) {
      stateName = "crit";
      const reason = (HB_ERROR_TEXT[live.last_error] || (() => live.last_error))();
      // "loop:<Class>": it ran and then ended; otherwise it never started.
      text = t(live.last_error.startsWith("loop:") ? "hb.live_ended" : "hb.live_not_started", { reason });
    } else if (live.enabled) {
      stateName = "warn";
      text = t("hb.live_stopped");
    } else {
      stateName = "info";
      text = t("hb.live_off");
    }
    summary.dataset.state = stateName;
    summary.textContent = text;
    if (live.running) {
      fact(t("hb.live_last"), fmtClock(live.last_tick_at) || t("hb.live_none_yet"));
      fact(t("hb.live_next"), fmtClock(live.next_tick_at));
      fact(t("hb.live_interval"), typeof live.interval_sec === "number" ? fmtDuration(live.interval_sec) : null);
      fact(t("hb.live_level"), live.last_level && HB_LEVEL_TEXT[live.last_level] ? HB_LEVEL_TEXT[live.last_level]() : null);
      fact(t("hb.live_count"), typeof live.tick_count === "number" ? String(live.tick_count) : null);
    }
    facts.hidden = facts.childElementCount === 0;
  }
  function renderHeartbeat() {
    const hb = draft.heartbeat;
    const input = $("hb-enabled");
    input.checked = hb.enabled === true;
    input.indeterminate = false;
    [["interval_floor_sec", "hb-floor"], ["interval_ceil_sec", "hb-ceil"]].forEach(([name, id]) => {
      const el = $(id);
      const rule = (state.constraints.heartbeat || {})[name];
      if (rule) { el.min = String(rule.min / 60); el.max = String(rule.max / 60); }
      if (document.activeElement !== el) el.value = minutesOf(hb[name]);
    });
    // Pinned by STIRA_HEARTBEAT: settings_surface.js applyPins turns the switch off and names
    // the variable. Here only: show what the running server uses (never the env value).
    const meta = ((state.surface && state.surface.fields) || {})["gateway.hb_enabled"] || {};
    const pinned = Boolean(meta.pinned_by);
    const running = ((state.apply.heartbeat || {}).running || {}).enabled;
    const known = pinned && typeof running === "boolean";
    if (known) input.checked = running;
    else if (pinned) { input.checked = false; input.indeterminate = true; }
    $("hb-enabled-live").hidden = !pinned;
    $("hb-enabled-live").textContent = !pinned ? "" : known ? t("cost.pinned_live") : t("cost.pinned_unknown");
    const note = $("hb-interval-note");
    const show = hbNote && !same(draft.heartbeat, state.values.heartbeat);
    // #512: the file's bounds were inverted; the defaults are shown until bounds are saved.
    const reset = state.hb_intervals_reset === true
      && hb.interval_floor_sec === state.values.heartbeat.interval_floor_sec
      && hb.interval_ceil_sec === state.values.heartbeat.interval_ceil_sec;
    note.textContent = show ? (HB_NOTE_TEXT[hbNote.code] || (() => ""))(hbNote.params)
      : reset ? t("hb.note_intervals_reset") : "";
    renderHeartbeatLive();
  }
  const HB_NOTE_TEXT = {
    rounded: (p) => t("hub.note_rounded", p),
    ceil_raised: (p) => t("hb.note_ceil_raised", p),
    floor_lowered: (p) => t("hb.note_floor_lowered", p),
    not_number: () => t("hub.err_not_number"),
  };
  // #486: the delegate tool (sub-agents): on/off and the caps of one child
  function renderDelegation() {
    $("delegation-enabled").checked = draft.delegation.enabled === true;
    DELEGATION_LIMITS.forEach(([key, id]) => {
      const input = $(id);
      const rule = (state.constraints.delegation || {})[key];
      if (rule) { input.min = rule.min; input.max = rule.max; }
      if (document.activeElement !== input) input.value = draft.delegation[key];
      input.disabled = draft.delegation.enabled !== true;
    });
  }

  // ADR 0021 S2: scheduled agent jobs (on/off, default off) and their daily pool
  function renderAgentJobs() {
    $("agent-jobs-enabled").checked = draft.agent_jobs.enabled === true;
    const input = $("agent-jobs-budget");
    const rule = (state.constraints.agent_jobs || {}).agent_daily_token_budget;
    if (rule) { input.min = rule.min; input.max = rule.max; }
    if (document.activeElement !== input) input.value = draft.agent_jobs.agent_daily_token_budget;
    input.disabled = draft.agent_jobs.enabled !== true;
  }

  function applyFilter() {
    const query = $("tool-filter").value.trim().toLowerCase();
    let shown = 0, total = 0;
    document.querySelectorAll(".tool-item").forEach((item) => {
      total += 1;
      const match = !query || item.dataset.search.includes(query);
      item.hidden = !match;
      if (match) shown += 1;
    });
    $("tool-filter-count").textContent = query ? t("hub.filter_count", { shown, total }) : "";
  }

  function renderStatus() {
    document.querySelectorAll(".timing-badge[data-group]").forEach((el) => {
      const status = state.apply[el.dataset.group];
      el.className = "badge timing-badge";
      el.dataset.state = "info";
      el.textContent = status ? (TIMING_TEXT[status.applies] || TIMING_TEXT.next_start)() : "";
    });
    document.querySelectorAll(".apply-status[data-group]").forEach((el) => {
      const status = state.apply[el.dataset.group];
      el.className = "badge apply-status";
      if (!status) { el.textContent = ""; return; }
      el.dataset.state = STATUS_STATE[status.status] || "info";
      el.textContent = (STATUS_TEXT[status.status] || STATUS_TEXT.saved)(status);
    });
  }

  function renderSummary(count) {
    const s = state.summary;
    $("sum-model").textContent = `${s.model.provider} / ${s.model.model_id}`;
    const cls = (CLASSIFIER_SHORT[s.classifier.choice] || (() => s.classifier.choice))();
    const live = s.classifier.effective === "unresolved" ? s.classifier.expected : s.classifier.effective;
    const eff = live && CLASSIFIER_NAMES[live] ? CLASSIFIER_NAMES[live]() : null;
    $("sum-classifier").textContent = eff && s.classifier.choice === "auto" ? `${cls} · ${eff}` : cls;
    $("sum-breaker").textContent = t("hub.sum_breaker_value", {
      warn: fmt(s.breaker.stability_warning), crit: fmt(s.breaker.stability_critical),
    });
    $("sum-tools").textContent = t("hub.sum_tools_value", { n: s.tools_effective, total: s.tools_total });
    $("sum-unsaved").textContent = count ? t("hub.unsaved_n", { n: count }) : t("hub.unsaved_none");
    $("sum-unsaved").dataset.dirty = String(count > 0);
  }

  function renderDanger() {
    const saved = dangersOf(state.values), pending = dangersOf(draft);
    // the board's pictures (#453) are confirmed when saved, not a standing safety warning
    const codes = [...new Set([...saved, ...pending])].filter((code) => code !== "board_svg");
    const banner = $("danger-banner");
    banner.hidden = codes.length === 0;
    banner.textContent = codes.length
      ? `${codes.map((code) => DANGER_TEXT[code]()).join(" ")} ${t("hub.danger_banner_hint")}` : "";
  }

  function describeChange(change) {
    const label = (FIELD_LABEL[change.field] || (() => change.field))();
    if (change.field === "tools.policy") {
      return t("policy.diff", {
        name: toolText(change.tool)[0],
        from: (POLICY_TEXT[change.from] || (() => change.from))(),
        to: (POLICY_TEXT[change.to] || (() => change.to))(),
      });
    }
    if (change.field === "tools.ask_tools" && change.tool) {
      return t(change.to ? "policy.diff_ask_on" : "policy.diff_ask_off", { name: toolText(change.tool)[0] });
    }
    if (change.tool) {
      return t(change.to ? "hub.diff_tool_on" : "hub.diff_tool_off", { name: toolText(change.tool)[0] });
    }
    if (change.command) {
      return t(change.to ? "hub.diff_cmd_add" : "hub.diff_cmd_remove", { command: shownCommand(change.command) });
    }
    if (change.root) {
      return t(change.to ? "hub.diff_root_add" : "hub.diff_root_remove", { root: change.root });
    }
    const show = (value) => {
      if (change.field === "classifier.choice") return CLASSIFIER_TEXT[value] ? CLASSIFIER_TEXT[value]()[0] : value;
      if (change.field === "tools.mode") return value === "trust_all" ? t("hub.mode_trust_all") : t("hub.mode_custom");
      if (change.field === "board.svg") return value ? t("hub.value_on") : t("hub.value_off");
      if (change.field === "delegation.enabled") return value ? t("hub.value_on") : t("hub.value_off");
      if (change.field === "agent_jobs.enabled") return value ? t("hub.value_on") : t("hub.value_off");
      if (change.field.startsWith("breaker_details.")) return fmtDetail(value);
      if (change.field === "output.max_output_tokens") return value === null ? t("cost.value_auto") : fmtTokens(value);
      if (change.field === "context.context_window_tokens" && value === null) return t("cost.value_auto");
      if (typeof value === "boolean") return value ? t("hub.value_on") : t("hub.value_off");
      if (change.field === "cache.ttl") return ({ "5m": () => t("cost.ttl_5m"), "1h": () => t("cost.ttl_1h") }[value] || (() => String(value)))();
      if (change.field.startsWith("context.")) return String(value);
      if (change.field.startsWith("heartbeat.interval_")) return fmtDuration(value);
      return fmt(value);
    };
    return t("hub.diff_value", { label, from: show(change.from), to: show(change.to) });
  }

  // Changes the preset would make to the current draft (not to the saved state).
  function presetChanges(preset) {
    return changeList(draft, { ...draft, tools: clone(preset.values.tools), breaker: clone(preset.values.breaker) });
  }

  function matchesDraft(preset) {
    const enabled = new Set(draft.tools.enabled_tools.filter((n) => state.tools.some((r) => r.name === n)));
    const want = new Set(preset.values.tools.enabled_tools);
    return draft.tools.mode === "custom" && enabled.size === want.size
      && [...want].every((n) => enabled.has(n))
      && BREAKER_SET.every((key) => !(key in preset.values.breaker)
        || Math.abs(draft.breaker[key] - preset.values.breaker[key]) < 1e-9);
  }

  function renderPresets() {
    const list = $("preset-list");
    list.replaceChildren();
    const active = state.summary.preset;
    $("preset-current").textContent = active
      ? t("hub.preset_current", { name: PRESET_TEXT[active]()[0] }) : t("hub.preset_custom");
    state.presets.forEach((preset) => {
      const [name, desc] = PRESET_TEXT[preset.id] ? PRESET_TEXT[preset.id]() : [preset.id, ""];
      const card = document.createElement("button");
      card.type = "button";
      card.className = "preset-card";
      const inDraft = matchesDraft(preset);
      card.setAttribute("aria-pressed", String(inDraft));
      card.dataset.preset = preset.id;
      const title = document.createElement("strong");
      title.textContent = preset.default ? `${name} ${t("hub.preset_default_mark")}` : name;
      const hint = document.createElement("span");
      hint.className = "hint";
      hint.textContent = desc;
      const facts = document.createElement("span");
      facts.className = "preset-facts";
      facts.textContent = t("hub.preset_facts", {
        n: preset.tools_enabled,
        warn: fmt(preset.values.breaker.stability_warning),
        crit: fmt(preset.values.breaker.stability_critical),
      });
      card.append(title, hint, facts);
      if (active === preset.id) {
        const badge = document.createElement("span");
        badge.className = "badge";
        badge.dataset.state = "ok";
        badge.textContent = t("hub.preset_saved_badge");
        card.append(badge);
      }
      card.addEventListener("click", () => openPreview(preset.id));
      list.append(card);
    });
  }

  function openPreview(id) {
    const preset = state.presets.find((p) => p.id === id);
    if (!preset) return;
    previewing = id;
    const changes = presetChanges(preset);
    $("preset-preview-title").textContent = t("hub.preset_preview_title", { name: PRESET_TEXT[id]()[0] });
    const list = $("preset-preview-list");
    list.replaceChildren();
    // Thresholds and mode first, then per-tool switches; long lists fold away.
    const ordered = [...changes.filter((c) => !c.tool && !c.command), ...changes.filter((c) => c.tool || c.command)];
    const items = ordered.length ? ordered.map(describeChange) : [t("hub.preset_no_change")];
    items.slice(0, 8).forEach((text) => {
      const li = document.createElement("li");
      li.textContent = text;
      list.append(li);
    });
    if (items.length > 8) {
      const li = document.createElement("li");
      const more = document.createElement("details");
      const summary = document.createElement("summary");
      summary.textContent = t("hub.history_more", { n: items.length - 8 });
      const rest = document.createElement("ul");
      items.slice(8).forEach((text) => {
        const item = document.createElement("li");
        item.textContent = text;
        rest.append(item);
      });
      more.append(summary, rest);
      li.append(more);
      list.append(li);
    }
    $("preset-apply").disabled = changes.length === 0;
    $("preset-preview").hidden = false;
    $("preset-preview").focus();
  }

  function closePreview() {
    previewing = null;
    $("preset-preview").hidden = true;
  }

  // --- Tool profiles (#450) ----------------------------------------------------
  const profileText = (id) => (PROFILE_TEXT[id] ? PROFILE_TEXT[id]() : [id, ""]);
  const sameSet = (a, b) => {
    const x = new Set(a || []), y = new Set(b || []);
    return x.size === y.size && [...x].every((n) => y.has(n));
  };
  function profileInDraft(profile) {
    return draft.tools.mode === profile.tools.mode
      && sameSet(draft.tools.enabled_tools, profile.tools.enabled_tools)
      && sameSet(draft.tools.ask_tools, profile.tools.ask_tools);
  }
  function profileList() {
    return state && Array.isArray(state.tool_profiles) ? state.tool_profiles : [];
  }

  function renderProfiles() {
    const list = $("profile-list");
    if (!list) return;
    list.replaceChildren();
    profileList().forEach((profile) => {
      const [name, desc] = profileText(profile.id);
      const card = document.createElement("button");
      card.type = "button";
      card.className = "preset-card profile-card";
      card.dataset.profile = profile.id;
      card.setAttribute("aria-pressed", String(profileInDraft(profile)));
      const title = document.createElement("strong");
      title.textContent = name;
      const hint = document.createElement("span");
      hint.className = "hint";
      hint.textContent = desc;
      const facts = document.createElement("span");
      facts.className = "preset-facts";
      facts.textContent = t("profile.facts", {
        always: profile.counts.always, ask: profile.counts.ask, never: profile.counts.never,
      });
      card.append(title, hint, facts);
      if (profile.saved) {
        const badge = document.createElement("span");
        badge.className = "badge";
        badge.dataset.state = "ok";
        badge.textContent = t("hub.preset_saved_badge");
        card.append(badge);
      }
      card.addEventListener("click", () => openProfile(profile.id));
      list.append(card);
    });
  }

  function profileGroup(profile, policy) {
    const rows = profile.rows.filter((row) => row.policy === policy);
    const box = document.createElement("div");
    box.className = "profile-group";
    box.dataset.policy = policy;
    const head = document.createElement("h6");
    head.id = `profile-group-${policy}`;
    head.textContent = t("profile.group", { policy: POLICY_TEXT[policy](), n: rows.length });
    const list = document.createElement("ul");
    list.className = "profile-tools";
    list.setAttribute("aria-labelledby", head.id);
    rows.forEach((row) => {
      const toolRow = (state.tools || []).find((r) => r.name === row.name) || { name: row.name };
      const before = policyOf(draft.tools, toolRow);
      const li = document.createElement("li");
      li.dataset.tool = row.name;
      const label = document.createElement("span");
      label.className = "profile-tool-name";
      label.textContent = toolText(row.name)[0];
      const code = document.createElement("code");
      code.className = "internal-name";
      code.textContent = row.name;
      li.append(label, " ", code);
      if (before !== row.policy) {
        const was = document.createElement("span");
        was.className = "hint profile-was";
        was.textContent = t("profile.was", { policy: POLICY_TEXT[before]() });
        li.append(" ", was);
      }
      if (!row.in_table) li.append(" ", badge(t("profile.kept"), "info"));
      if (row.needs_env) {
        li.append(" ", badge(t(row.env_set ? "profile.env_set" : "profile.needs_env",
          { env: state.exec_env_var || "STIRA_ENABLE_EXEC_TOOLS" }), row.env_set ? "info" : "warn", "env"));
      }
      // What YOLO would do with this Ask tool (the "needs the variable" badge says it for an
      // exec tool that is not exposed).
      if (row.policy === "ask" && row.yolo && row.yolo !== "exec_not_exposed" && PROFILE_YOLO_TEXT[row.yolo]) {
        const scope = ["internal", "external", "exec"].includes(row.yolo);
        li.append(" ", badge(PROFILE_YOLO_TEXT[row.yolo](), scope ? "info" : "warn", "profile-yolo"));
      }
      list.append(li);
    });
    if (!rows.length) {
      const li = document.createElement("li");
      li.className = "hint";
      li.textContent = t("profile.none");
      list.append(li);
    }
    box.append(head, list);
    return box;
  }

  function openProfile(id) {
    const profile = profileList().find((p) => p.id === id);
    if (!profile) return;
    profilePreviewing = id;
    const [name] = profileText(id);
    $("profile-preview-title").textContent = t("profile.preview_title", { name });
    const changed = profile.rows.filter((row) => {
      const toolRow = (state.tools || []).find((r) => r.name === row.name) || { name: row.name };
      return policyOf(draft.tools, toolRow) !== row.policy;
    }).length;
    const modeChange = draft.tools.mode !== profile.tools.mode;
    $("profile-preview-summary").textContent = changed || modeChange
      ? t("profile.preview_changes", { n: changed }) : t("hub.preset_no_change");
    $("profile-preview-yolo").textContent = profile.yolo.external === 0
      ? t("profile.preview_yolo_none")
      : t("profile.preview_yolo", { internal: profile.yolo.internal, external: profile.yolo.external, exec: profile.yolo.exec });
    $("profile-preview-yolo").dataset.state = profile.yolo.external === 0 ? "warn" : "info";
    const groups = $("profile-preview-groups");
    groups.replaceChildren(...["always", "ask", "never"].map((policy) => profileGroup(profile, policy)));
    $("profile-apply").disabled = !(changed || modeChange);
    $("profile-preview").hidden = false;
    $("profile-preview").focus();
  }

  function closeProfile() {
    profilePreviewing = null;
    $("profile-preview").hidden = true;
  }

  function renderFooter(count, errorCount) {
    $("hub-dirty-count").textContent = count ? t("hub.unsaved_n", { n: count }) : t("hub.unsaved_none");
    $("hub-footer").dataset.dirty = String(count > 0);
    const list = $("hub-diff-list");
    list.replaceChildren();
    const items = changeList().map(describeChange);
    if (modelDirty()) items.unshift(t("hub.diff_model", { label: window.StiraModelSection.label() }));
    items.forEach((text) => {
      const li = document.createElement("li");
      li.textContent = text;
      list.append(li);
    });
    $("hub-diff").hidden = items.length === 0;
    $("hub-save").disabled = saving || count === 0 || errorCount > 0;
    $("hub-discard").disabled = saving || count === 0;
    $("hub-defaults").disabled = saving || same(draft, { ...draft, ...pickDefaults() });
  }

  function pickDefaults() {
    return {
      classifier: clone(state.defaults.classifier), tools: clone(state.defaults.tools),
      breaker: clone(state.defaults.breaker), breaker_details: clone(state.defaults.breaker_details),
      write: clone(state.defaults.write), read: clone(state.defaults.read), board: clone(state.defaults.board),
      output: clone(state.defaults.output), cache: clone(state.defaults.cache), context: clone(state.defaults.context),
      heartbeat: clone(state.defaults.heartbeat),
      delegation: clone(state.defaults.delegation),
      agent_jobs: clone(state.defaults.agent_jobs),
    };
  }

  function update({ full = true } = {}) {
    if (!state) return;
    if (full) { renderClassifier(); renderBreaker(); renderTools(); renderWrite(); renderRead(); renderBoard(); renderCost(); renderHeartbeat(); renderDelegation(); renderAgentJobs(); }
    renderPresets();
    renderProfiles();
    const errorCount = renderErrors(validate());
    const count = dirtyCount();
    renderStatus();
    renderSummary(count);
    renderDanger();
    renderFooter(count, errorCount);
    renderHistory(historyEntries);
    // #469: settings_surface.js shows source / applies / restart-pending from state.surface.
    document.dispatchEvent(new CustomEvent("stira:hub-state", { detail: state }));
  }

  // --- Loading / saving ------------------------------------------------------

  async function load({ keepDraft = false } = {}) {
    const result = await request("/api/config/hub");
    if (!result.ok) throw new Error(result.data.error || `HTTP ${result.status}`);
    state = result.data;
    if (!keepDraft || !draft) { draft = clone(state.values); breakerNotes = {}; writeScopeChoice = null; readScopeChoice = null; }
    update();
    schedulePoll();
  }

  function schedulePoll() {
    clearTimeout(pollTimer);
    const pending = Object.values(state.apply).some((s) => s.status === "pending");
    if (!pending) return;
    pollTimer = setTimeout(async () => {
      const result = await request("/api/config/hub").catch(() => null);
      if (result && result.ok) {
        state.apply = result.data.apply;
        state.surface = result.data.surface;
        state.summary = result.data.summary;
        state.tools = result.data.tools;
        update({ full: false });
      }
      schedulePoll();
    }, 5000);
  }

  // Resolves true/false; with ``names`` the typed tool names must match too (#190).
  // #282: rendered by the shared StiraConfirm dialog (confirm_dialog.js); same checks.
  function confirmDangers(codes, names = [], typedWord = null) {
    if (typeof StiraConfirm === "undefined") return Promise.resolve(false);
    // #453: turning on the board's pictures alone is confirmed with one box (lighter than a
    // typed word: it widens what the page draws, not what the agent may do)
    if (codes.length === 1 && codes[0] === "board_svg" && !names.length && !typedWord) {
      return StiraConfirm.open({
        title: t("hub.board_confirm_title"),
        items: [DANGER_TEXT.board_svg()],
        checks: [t("hub.board_confirm_check")],
        confirmLabel: t("hub.confirm_save"),
        cancelLabel: t("hub.cancel"),
        tone: "danger",
      });
    }
    const word = typedWord || t("hub.confirm_word");
    return StiraConfirm.open({
      title: t("hub.danger_title"),
      items: codes.map((code) => (DANGER_TEXT[code] || (() => code))()),
      typed: { word, prompt: t("hub.confirm_prompt", { word }) },
      names: names.length ? {
        expected: names,
        prompt: t("policy.names_prompt", { names: names.join(", ") }),
        hint: t("policy.names_hint"),
        parse: typedNames,
      } : null,
      confirmLabel: t("hub.confirm_save"),
      cancelLabel: t("hub.cancel"),
      tone: "danger",
    });
  }

  // #601 E9: the inputs stay editable while a save is on the way; a value changed since the
  // save sent its draft is kept on top of the saved state (per group, per key) instead of
  // being replaced by the answer. Everything else follows the saved state.
  function keepEditsSince(sent, live, saved) {
    Object.keys(live || {}).forEach((group) => {
      const before = sent ? sent[group] : undefined;
      const now = live[group];
      if (same(before, now)) return;
      const plain = (v) => v !== null && typeof v === "object" && !Array.isArray(v);
      if (plain(before) && plain(now) && plain(saved[group])) {
        Object.keys(now).forEach((key) => {
          if (!same(before[key], now[key])) saved[group][key] = clone(now[key]);
        });
      } else saved[group] = clone(now);
    });
    return saved;
  }

  // After a successful save: the saved state, plus what was changed while it was on the way
  // (values: keepEditsSince; each scope radio on its own). Unchanged ones follow the state.
  function rebaseAfterSave(sent) {
    const edited = !same(draft, sent.draft);
    draft = keepEditsSince(sent.draft, draft, clone(state.values));
    if (!edited) breakerNotes = {};
    if (writeScopeChoice === sent.write) writeScopeChoice = null;
    if (readScopeChoice === sent.read) readScopeChoice = null;
  }

  async function save() {
    if (saving || !state) return;
    const errors = validate();
    if (errors.length) { renderErrors(errors); return; }
    const changes = hubChanges();
    const acknowledged = newDangers();
    const confirmAlways = alwaysWrites(state.values, draft);
    // Locked rules (#190): saving them is a recovery, confirmed by typing RECOVER.
    const recovering = Boolean(state.tools_locked) && !same(draft.tools, state.values.tools);
    const codes = recovering ? [...acknowledged, "tool_rules_recover"] : acknowledged;
    if (codes.length && !(await confirmDangers(codes, confirmAlways, recovering ? "RECOVER" : null))) {
      $("hub-save-status").textContent = t("hub.save_cancelled");
      return;
    }
    saving = true;
    // #601 E9: what this save sends (edits after it are kept): the draft and the scope radios
    // (a radio without a preset, e.g. "custom", changes the choice only, not the draft).
    const sent = { draft: clone(draft), write: writeScopeChoice, read: readScopeChoice };
    update({ full: false });
    $("hub-save-status").textContent = t("tools.saving");
    try {
      if (modelDirty() && !(await window.StiraModelSection.save())) {
        $("hub-save-status").textContent = t("hub.save_failed_model");
        return;
      }
      if (Object.keys(changes).length) {
        const result = await request("/api/config/hub", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            changes, base_version: state.version, acknowledged_dangers: acknowledged, source: draftSource,
            confirm_always: confirmAlways,
            ...(recovering ? { recover: "RECOVER" } : {}),
          }),
        });
        if (result.status === 409 && result.data.state) {
          // Someone else saved: show their values as the new baseline, keep this draft.
          state = result.data.state;
          serverErrors = [];
          $("hub-save-status").textContent = t("hub.save_conflict");
          return;
        }
        if (!result.ok) {
          serverErrors = Array.isArray(result.data.errors) ? result.data.errors : [];
          const reason = SAVE_ERROR_TEXT[result.data.error_code]
            ? SAVE_ERROR_TEXT[result.data.error_code]() : (result.data.error || `HTTP ${result.status}`);
          $("hub-save-status").textContent = t("settings.save_failed_message", { message: reason });
          return;
        }
        state = result.data.state;
        rebaseAfterSave(sent);
        draftSource = "manual";
      } else {
        await load({ keepDraft: true });
        rebaseAfterSave(sent);
      }
      serverErrors = [];
      $("hub-save-status").textContent = t("hub.saved_at", { time: new Date().toLocaleTimeString() });
      toast(t("tools.saved"));
    } catch (error) {
      $("hub-save-status").textContent = t("settings.save_failed_message", { message: error.message });
    } finally {
      saving = false;
      update();
      schedulePoll();
      loadHistory();
    }
  }

  // --- Change history (#195) -------------------------------------------------

  function expandChanges(entry) {
    // Single values (thresholds, mode, classifier, model) first, then per-tool switches.
    const scalars = [], lists = [];
    (entry.changes || []).forEach((change) => {
      if (change.added || change.removed) {
        const kind = { "tools.shell_allowlist": "command", "write.roots": "root", "read.roots": "root" }[change.field] || "tool";
        (change.added || []).forEach((name) => lists.push({ field: change.field, [kind]: name, to: true }));
        (change.removed || []).forEach((name) => lists.push({ field: change.field, [kind]: name, to: false }));
      } else if ("from" in change || "to" in change) {
        scalars.push(change);
      } else {
        scalars.push({ field: change.field, from: "…", to: "…" });
      }
    });
    return [...scalars, ...lists];
  }

  function sourceText(source) {
    if (SOURCE_TEXT[source]) return SOURCE_TEXT[source]();
    if (source.startsWith("preset:") && PRESET_TEXT[source.slice(7)]) {
      return t("hub.src_preset", { name: PRESET_TEXT[source.slice(7)]()[0] });
    }
    if (source.startsWith("profile:") && PROFILE_TEXT[source.slice(8)]) {
      return t("profile.src", { name: PROFILE_TEXT[source.slice(8)]()[0] });
    }
    if (source.startsWith("rollback:")) return t("hub.src_rollback");
    return source;
  }

  function renderHistory(entries) {
    const list = $("history-list");
    list.replaceChildren();
    $("history-empty").hidden = entries.length > 0;
    const dirty = dirtyCount() > 0;
    entries.forEach((entry) => {
      const li = document.createElement("li");
      li.className = "history-item";
      const head = document.createElement("div");
      head.className = "history-head";
      const time = document.createElement("time");
      time.dateTime = entry.at;
      time.textContent = new Date(entry.at).toLocaleString();
      const source = document.createElement("span");
      source.className = "badge";
      source.dataset.state = "info";
      source.textContent = sourceText(entry.source || "manual");
      head.append(time, source);
      const changes = expandChanges(entry).map(describeChange);
      const ul = document.createElement("ul");
      changes.slice(0, 4).forEach((text) => {
        const item = document.createElement("li");
        item.textContent = text;
        ul.append(item);
      });
      li.append(head, ul);
      if (changes.length > 4) {
        const more = document.createElement("details");
        const summary = document.createElement("summary");
        summary.textContent = t("hub.history_more", { n: changes.length - 4 });
        const rest = document.createElement("ul");
        changes.slice(4).forEach((text) => {
          const item = document.createElement("li");
          item.textContent = text;
          rest.append(item);
        });
        more.append(summary, rest);
        li.append(more);
      }
      const restorable = (entry.changes || []).some((c) => {
        const [group] = c.field.split(".");
        return HUB_GROUPS.includes(group)
          && !(entry.redacted_fields || []).includes(c.field);
      });
      if (restorable) {
        const button = document.createElement("button");
        button.type = "button";
        button.className = "btn btn-secondary";
        button.textContent = t("hub.rollback");
        button.disabled = dirty || saving;
        if (dirty) button.title = t("hub.rollback_needs_clean");
        button.addEventListener("click", () => rollback(entry));
        li.append(button);
      }
      list.append(li);
    });
  }

  async function loadHistory() {
    const result = await request("/api/config/history?limit=20").catch(() => null);
    if (result && result.ok) {
      historyEntries = result.data.entries || [];
      renderHistory(historyEntries);
    }
  }

  async function rollback(entry, acknowledged = [], confirmAlways = []) {
    if (!state || saving) return;
    saving = true;
    let retried = false;
    update({ full: false }); // the history and footer buttons say a save is on the way
    $("history-status").textContent = t("tools.saving");
    try {
      const result = await request(`/api/config/history/${encodeURIComponent(entry.id)}/rollback`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          base_version: state.version, acknowledged_dangers: acknowledged, confirm_always: confirmAlways,
        }),
      });
      const needsNames = result.data.error_code === "needs_tool_names" && !confirmAlways.length;
      if ((result.data.error_code === "needs_confirmation" && !acknowledged.length) || needsNames) {
        saving = false;
        const codes = needsNames ? acknowledged : result.data.needs_confirmation;
        const names = needsNames ? result.data.needs_tool_names : [];
        if (await confirmDangers(codes.length ? codes : ["tool_policy_loosened"], names)) {
          // Resolved true only when the typed names matched ``names`` exactly (sorted, deduped).
          // #601 E10: awaited, and the retry's own ``finally`` ends the save (this one would
          // otherwise release ``saving`` while the retried POST is still on the way).
          retried = true;
          return await rollback(entry, codes, needsNames ? names : []);
        }
        $("history-status").textContent = t("hub.save_cancelled");
        return;
      }
      if (result.status === 409 && result.data.state) {
        state = result.data.state;
        draft = clone(state.values); breakerNotes = {};
        writeScopeChoice = null; readScopeChoice = null;
        $("history-status").textContent = t("hub.rollback_conflict");
        return;
      }
      if (!result.ok) {
        // Field errors (e.g. a switch now pinned by an environment variable) say why.
        const reasons = (result.data.errors || []).map((error) => {
          const label = FIELD_LABEL[error.field] ? `${FIELD_LABEL[error.field]()}: ` : "";
          return label + (ERROR_TEXT[error.code] || (() => t("hub.err_invalid")))(error.params || {});
        });
        $("history-status").textContent = t("settings.save_failed_message", {
          message: reasons.length ? reasons.join(" ")
            : SAVE_ERROR_TEXT[result.data.error_code]
              ? SAVE_ERROR_TEXT[result.data.error_code]() : (result.data.error || `HTTP ${result.status}`),
        });
        return;
      }
      state = result.data.state;
      draft = clone(state.values); breakerNotes = {};
      writeScopeChoice = null; readScopeChoice = null;
      $("history-status").textContent = t("hub.rollback_done");
      toast(t("hub.rollback_done"));
    } finally {
      if (!retried) {
        saving = false;
        update();
        schedulePoll();
        loadHistory();
      }
    }
  }

  // --- Wiring ----------------------------------------------------------------

  BREAKER.forEach(([key, id]) => {
    [[key, id, "entry"], [EXIT_OF[key], `${id}-exit`, "exit"]].forEach(([field, inputId, which]) => {
      $(inputId).addEventListener("input", () => {
        moveSlider(field, which, Number($(inputId).value));
        serverErrors = [];
        renderBreaker();
        update({ full: false });
      });
    });
  });
  DETAILS.forEach(([key, id]) => {
    // Typed values are rounded into the limits when the field is left (or Enter), not
    // while typing, so "1" on the way to "120" is not turned into a minimum.
    const commit = () => {
      const rule = (state.constraints.breaker_details || {})[key];
      if (!rule) return;
      const raw = $(id).value;
      const result = Pairs.roundDetail(raw, rule);
      delete breakerNotes[key];
      if (result.note === "not_number") {
        draft.breaker_details[key] = state.values.breaker_details[key];
        breakerNotes[key] = { code: "not_number", params: {} };
      } else {
        draft.breaker_details[key] = result.value;
        if (result.note) breakerNotes[key] = { code: "rounded", params: { from: String(raw).trim(), to: fmtDetail(result.value) } };
      }
      $(id).value = fmtDetail(draft.breaker_details[key]);
      serverErrors = [];
      renderBreaker();
      update({ full: false });
    };
    $(id).addEventListener("change", commit);
    $(id).addEventListener("keydown", (event) => {
      if (event.key === "Enter") { event.preventDefault(); commit(); }
    });
  });
  document.querySelectorAll('input[name="tool-mode"]').forEach((input) => {
    input.addEventListener("change", () => {
      draft.tools.mode = input.value;
      serverErrors = [];
      update();
    });
  });
  document.querySelectorAll('input[name="write-scope"]').forEach((input) => {
    input.addEventListener("change", () => {
      writeScopeChoice = input.value;
      const preset = state.constraints.write_scopes[input.value];
      if (preset) draft.write.roots = clone(preset);
      serverErrors = [];
      update();
    });
  });
  document.querySelectorAll('input[name="read-scope"]').forEach((input) => {
    input.addEventListener("change", () => {
      readScopeChoice = input.value;
      const preset = state.constraints.read_scopes[input.value];
      if (preset) draft.read.roots = clone(preset);
      serverErrors = [];
      update();
    });
  });
  // #470: cost settings (saved with the footer's "Save"; nothing here needs a confirmation)
  const costChanged = () => {
    serverErrors = [];
    update({ full: false });
    renderCost();
  };
  ["cost-output-auto", "cost-output-fixed"].forEach((id) => $(id).addEventListener("change", () => {
    if ($("cost-output-auto").checked) {
      if (draft.output.max_output_tokens !== null) lastFixedCap = draft.output.max_output_tokens;
      draft.output.max_output_tokens = null;
    } else {
      const info = (state.cost && state.cost.model) || {};
      draft.output.max_output_tokens = lastFixedCap ?? state.values.output.max_output_tokens ?? info.auto_max_output_tokens ?? 8192;
      costChanged();
      $("cost-max-output").focus();
      $("cost-max-output").select();
      return;
    }
    costChanged();
  }));
  $("cost-max-output").addEventListener("input", () => {
    draft.output.max_output_tokens = numberOrNaN($("cost-max-output").value);
    costChanged();
  });
  $("cost-cache-enabled").addEventListener("change", () => {
    draft.cache.enabled = $("cost-cache-enabled").checked;
    costChanged();
  });
  $("cost-cache-ttl").addEventListener("change", () => {
    draft.cache.ttl = $("cost-cache-ttl").value;
    costChanged();
  });
  $("cost-context-enabled").addEventListener("change", () => {
    draft.context.enabled = $("cost-context-enabled").checked;
    costChanged();
  });
  CONTEXT_OPTIONS.forEach((name) => {
    const el = document.getElementById(`cost-ctx-${name}`);
    el.addEventListener("input", () => {
      const rule = state.constraints.context[name];
      const value = numberOrNaN(el.value);
      draft.context[name] = Number.isNaN(value) && rule.nullable && !el.value.trim() ? null : value;
      costChanged();
    });
  });
  // #472: heartbeat. Saved with the footer's "Save"; turning it on needs no confirmation
  // (a tick never calls a model). Minutes are rounded into the limits when the field is
  // left (or Enter); the other bound follows so the floor never passes the ceiling.
  $("hb-enabled").addEventListener("change", () => {
    draft.heartbeat.enabled = $("hb-enabled").checked;
    serverErrors = [];
    update({ full: false });
    renderHeartbeat();
  });
  [["interval_floor_sec", "hb-floor"], ["interval_ceil_sec", "hb-ceil"]].forEach(([name, id]) => {
    const commit = () => {
      const rule = (state.constraints.heartbeat || {})[name];
      if (!rule) return;
      const raw = $(id).value;
      // Left as shown (e.g. a saved 90 s is "1.5"): the saved seconds stay as they are.
      if (String(raw).trim() === minutesOf(draft.heartbeat[name])) return;
      const result = Pairs.roundDetail(raw, { integer: true, min: rule.min / 60, max: rule.max / 60 });
      hbNote = null;
      if (result.note === "not_number") {
        draft.heartbeat[name] = state.values.heartbeat[name];
        hbNote = { code: "not_number", params: {} };
      } else {
        const seconds = result.value * 60;
        draft.heartbeat[name] = seconds;
        if (result.note) hbNote = { code: "rounded", params: { from: String(raw).trim(), to: fmtDuration(seconds) } };
        const hb = draft.heartbeat;
        if (hb.interval_floor_sec > hb.interval_ceil_sec) {
          if (name === "interval_floor_sec") {
            hb.interval_ceil_sec = hb.interval_floor_sec;
            hbNote = { code: "ceil_raised", params: { to: fmtDuration(hb.interval_ceil_sec) } };
          } else {
            hb.interval_floor_sec = hb.interval_ceil_sec;
            hbNote = { code: "floor_lowered", params: { to: fmtDuration(hb.interval_floor_sec) } };
          }
        }
      }
      $(id).value = minutesOf(draft.heartbeat[name]);
      serverErrors = [];
      update({ full: false });
      renderHeartbeat();
    };
    $(id).addEventListener("change", commit);
    $(id).addEventListener("keydown", (event) => {
      if (event.key === "Enter") { event.preventDefault(); commit(); }
    });
  });
  $("board-svg").addEventListener("change", () => {
    draft.board.svg = $("board-svg").checked;
    serverErrors = [];
    update({ full: false });
  });
  $("delegation-enabled").addEventListener("change", () => {
    draft.delegation.enabled = $("delegation-enabled").checked;
    serverErrors = [];
    update();
  });
  DELEGATION_LIMITS.forEach(([key, id]) => {
    $(id).addEventListener("input", () => {
      const text = $(id).value.trim();
      // an empty or partial entry is kept as text so that validate() names it
      draft.delegation[key] = /^[0-9]+$/.test(text) ? Number(text) : text;
      serverErrors = [];
      update({ full: false });
    });
  });
  $("agent-jobs-enabled").addEventListener("change", () => {
    draft.agent_jobs.enabled = $("agent-jobs-enabled").checked;
    serverErrors = [];
    update();
  });
  $("agent-jobs-budget").addEventListener("input", () => {
    const text = $("agent-jobs-budget").value.trim();
    draft.agent_jobs.agent_daily_token_budget = /^[0-9]+$/.test(text) ? Number(text) : text;
    serverErrors = [];
    update({ full: false });
  });
  $("write-roots").addEventListener("input", () => {
    draft.write.roots = $("write-roots").value.split("\n").map((line) => line.trim()).filter(Boolean);
    serverErrors = [];
    update({ full: false });
  });
  $("read-roots").addEventListener("input", () => {
    draft.read.roots = $("read-roots").value.split("\n").map((line) => line.trim()).filter(Boolean);
    serverErrors = [];
    update({ full: false });
  });
  $("tool-filter").addEventListener("input", applyFilter);
  function addCommand() {
    const command = $("new-command").value.trim();
    if (!command || SHELL_OPERATORS.test(command)) {
      const el = $("err-tools-shell_allowlist");
      el.textContent = t("tools.enter_a_literal_command_without_shell_op");
      el.hidden = false;
      $("new-command").setAttribute("aria-invalid", "true");
      return;
    }
    $("new-command").removeAttribute("aria-invalid");
    if (!draft.tools.shell_allowlist.includes(command)) draft.tools.shell_allowlist.push(command);
    $("new-command").value = "";
    update();
    $("new-command").focus();
  }
  $("add-command").addEventListener("click", addCommand);
  $("allowlist-secrets-remove").addEventListener("click", () => {
    const masks = savedSecrets();
    draft.tools.shell_allowlist = draft.tools.shell_allowlist.filter((c) => !masks.has(c));
    serverErrors = [];
    update();
  });
  $("new-command").addEventListener("keydown", (event) => {
    if (event.key === "Enter") { event.preventDefault(); addCommand(); }
  });
  $("hub-save").addEventListener("click", save);
  $("preset-apply").addEventListener("click", () => {
    const preset = state.presets.find((p) => p.id === previewing);
    if (!preset) return;
    draft = { ...draft, tools: clone(preset.values.tools), breaker: clone(preset.values.breaker) }; breakerNotes = {};
    draftSource = `preset:${preset.id}`;
    serverErrors = [];
    const name = PRESET_TEXT[preset.id]()[0];
    closePreview();
    update();
    $("hub-save-status").textContent = t("hub.preset_staged", { name });
    $("hub-save").focus();
  });
  $("profile-apply").addEventListener("click", () => {
    const profile = profileList().find((p) => p.id === profilePreviewing);
    if (!profile) return;
    draft = { ...draft, tools: clone(profile.tools) };
    draftSource = `profile:${profile.id}`;
    serverErrors = [];
    const [name] = profileText(profile.id);
    closeProfile();
    update();
    $("hub-save-status").textContent = t("profile.staged", { name });
    $("hub-save").focus();
  });
  $("profile-cancel").addEventListener("click", () => {
    const id = profilePreviewing;
    closeProfile();
    const card = document.querySelector(`.profile-card[data-profile="${id}"]`);
    if (card) card.focus();
  });
  $("profile-preview").addEventListener("keydown", (event) => {
    if (event.key === "Escape") $("profile-cancel").click();
  });
  $("preset-cancel").addEventListener("click", () => {
    const id = previewing;
    closePreview();
    const card = document.querySelector(`.preset-card[data-preset="${id}"]`);
    if (card) card.focus();
  });
  $("preset-preview").addEventListener("keydown", (event) => {
    if (event.key === "Escape") $("preset-cancel").click();
  });
  $("hub-discard").addEventListener("click", () => {
    draft = clone(state.values); breakerNotes = {};
    writeScopeChoice = null; readScopeChoice = null;
    serverErrors = [];
    if (window.StiraModelSection) window.StiraModelSection.reset();
    $("hub-save-status").textContent = t("hub.discarded");
    update();
  });
  $("hub-defaults").addEventListener("click", () => {
    draft = { ...draft, ...pickDefaults() }; breakerNotes = {};
    writeScopeChoice = null; readScopeChoice = null;
    draftSource = "defaults";
    serverErrors = [];
    $("hub-save-status").textContent = t("hub.defaults_staged");
    update();
  });
  document.addEventListener("stira:model-draft", () => update({ full: false }));
  // #601 E7: while this page's own save is on the way it re-reads the state itself (the POST
  // answer, or load() after a model-only save); a GET started here could answer after the
  // POST and put the older state back.
  document.addEventListener("stira:model-saved", () => { if (state && !saving) load({ keepDraft: true }).catch(() => {}); });
  window.addEventListener("beforeunload", (event) => {
    if (dirtyCount() > 0) { event.preventDefault(); event.returnValue = ""; }
  });

  // Display preferences: browser-local and immediate.
  $("hub-lang").value = (window.i18n && window.i18n.lang) || "ja";
  $("hub-lang").addEventListener("change", () => window.i18n && window.i18n.setLang($("hub-lang").value));
  const showInternal = (() => { try { return localStorage.getItem(INTERNAL_KEY) === "1"; } catch (e) { return false; } })();
  $("show-internal").checked = showInternal;
  document.body.classList.toggle("show-internal", showInternal);
  $("show-internal").addEventListener("change", () => {
    const on = $("show-internal").checked;
    document.body.classList.toggle("show-internal", on);
    try { localStorage.setItem(INTERNAL_KEY, on ? "1" : "0"); } catch (e) { /* ignore */ }
  });

  load()
    .then(() => { $("load-status").textContent = ""; $("load-status").hidden = true; loadHistory(); })
    .catch((error) => {
      $("load-status").textContent = t("settings.loading_failed_message", { message: error.message });
    });
})();
