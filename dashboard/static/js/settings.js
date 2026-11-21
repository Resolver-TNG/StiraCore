(() => {
  "use strict";
  const $ = (id) => document.getElementById(id);
  let toastTimer = null;

  function toast(message, isError = false) {
    const el = $("toast");
    el.textContent = message;
    el.classList.toggle("error", isError);
    el.classList.add("show");
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => el.classList.remove("show"), 3200);
  }

  async function api(url, options = {}) {
    const response = await stiraFetch(url, { cache: "no-store", credentials: "same-origin", ...options });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) {
      const error = new Error("refused");
      error.shown = refusalText(data, response.status);
      throw error;
    }
    return data;
  }

  // What the page says for a failure: a refusal's own sentence, else "not reachable".
  // The server's sentence and a browser's exception text are never shown (#330).
  const shownText = (error) => (error && error.shown) || t("mset.err_network");

  // --- Model selection (provider-aware, #208) ------------------------------

  const CUSTOM = "__custom__";
  let providerData = null;

  // Literal t() calls so `i18n_extract.py --check` sees every key.
  const REASONS = {
    ok: () => t("settings.reason_ok"),
    provider_unavailable: () => t("settings.reason_provider_unavailable"),
    missing_credential: () => t("settings.reason_missing_credential"),
    invalid_config: () => t("settings.reason_invalid_config"),
    auth_failed: () => t("settings.reason_auth_failed"),
    model_not_found: () => t("settings.reason_model_not_found"),
    bad_request: () => t("settings.reason_bad_request"),
    throttled: () => t("settings.reason_throttled"),
    context_overflow: () => t("settings.reason_context_overflow"),
    unreachable: () => t("settings.reason_unreachable"),
    server_error: () => t("settings.reason_server_error"),
    timeout: () => t("settings.reason_timeout"),
    busy: () => t("settings.reason_busy"),
    error: () => t("settings.reason_error"),
  };
  const BADGES = {
    vision: () => t("settings.cap_vision"),
    tools: () => t("settings.cap_tools"),
    thinking: () => t("settings.cap_thinking"),
  };

  function reasonText(result) {
    const base = (REASONS[result.reason] || REASONS.error)();
    const parts = [base];
    if (result.env) parts.push(t("settings.env_name", { env: result.env }));
    if (result.extra) parts.push(t("settings.install_hint", { extra: result.extra }));
    if (result.http_status) parts.push(`HTTP ${result.http_status}`);
    if (result.error_type && result.reason !== "missing_credential") parts.push(result.error_type);
    return parts.join(" · ");
  }

  function selectionLabel(sel) {
    return sel ? `${sel.provider} / ${sel.model_id}` : "—";
  }

  // #476: one sentence per refusal code of the model save, in the page's language.
  // Literal t() calls so `i18n_extract.py --check` sees every key.
  const FIELD_LABELS = {
    provider: () => t("settings.provider"),
    model_id: () => t("settings.model_id"),
    base_url: () => t("settings.base_url"),
    api_key_env: () => t("settings.api_key_env"),
    organization_env: () => t("mset.organization_env"),
    region: () => t("mset.region"),
  };
  const fieldLabel = (field) => (FIELD_LABELS[field] ? FIELD_LABELS[field]() : "");
  const SAVE_ERRORS = {
    invalid_body: () => t("mset.err_request"),
    unknown_field: () => t("mset.err_request"),
    unknown_model: () => t("mset.err_unknown_model"),
    invalid_provider: () => t("mset.err_invalid_provider"),
    invalid_model_id: () => t("mset.err_invalid_model_id"),
    invalid_base_url: () => t("mset.err_invalid_base_url"),
    remote_base_url: () => t("mset.err_remote_base_url"),
    invalid_env_name: (d) => t("mset.err_invalid_env_name", { field: fieldLabel(d.field) }),
    invalid_region: () => t("mset.err_invalid_region"),
    field_not_used: (d) => t("mset.err_field_not_used", { field: fieldLabel(d.field) }),
    pinned_by_env: (d) => t("mset.err_pinned_by_env", { env: d.env || "" }),
    provider_unavailable: (d) => reasonText(d),
    missing_credential: (d) => t("mset.err_missing_env", { env: d.env || "" }),
    invalid_config: () => t("mset.err_invalid_config"),
    conflict: () => t("mset.err_conflict"),
    save_failed: () => t("mset.err_save_failed"),
    config_unavailable: () => t("mset.err_config_unavailable"),
  };

  function refusalText(data, status) {
    const code = data && typeof data.error_code === "string" ? data.error_code : "";
    return Object.prototype.hasOwnProperty.call(SAVE_ERRORS, code)
      ? SAVE_ERRORS[code](data) : t("mset.err_unknown", { status });
  }

  const FIELD_INPUTS = {
    model_id: "custom-model-id",
    base_url: "custom-base-url",
    api_key_env: "custom-api-key-env",
    organization_env: "custom-organization-env",
    region: "custom-region",
  };

  // The refusal next to the buttons (role="alert"), and the input it is about.
  function showModelError(text, field) {
    const el = $("model-error");
    el.textContent = text || "";
    el.hidden = !text;
    Object.keys(FIELD_INPUTS).forEach((name) => {
      const input = $(FIELD_INPUTS[name]);
      if (text && name === field) {
        input.setAttribute("aria-invalid", "true");
        const fold = input.closest("details");
        if (fold) fold.open = true;
      } else {
        input.removeAttribute("aria-invalid");
      }
    });
  }

  function credentialBadge(cred, local) {
    const el = $("provider-credentials");
    let text;
    let state = "info";
    if (local || cred.kind === "none") {
      text = t("settings.cred_not_needed");
      state = "ok";
    } else if (cred.configured === true) {
      text = t("settings.cred_configured");
      state = "ok";
    } else if (cred.configured === false) {
      text = t("settings.cred_not_configured");
      state = "warn";
    } else {
      text = cred.kind === "aws_chain" ? t("settings.cred_aws_chain") : t("settings.cred_unknown");
    }
    el.dataset.state = state;
    // Variable NAME only; the API never returns a value.
    el.textContent = cred.env ? `${text} (${cred.env})` : text;
  }

  function currentProvider() {
    const name = $("provider-select").value;
    return providerData.providers.find((p) => p.name === name);
  }

  function chosenSelection() {
    const provider = $("provider-select").value;
    const checked = document.querySelector('input[name="model-choice"]:checked');
    let modelId = checked ? checked.value : "";
    if (modelId === CUSTOM) modelId = $("custom-model-id").value.trim();
    const body = { provider, model_id: modelId };
    // Send endpoint/key-name fields only when changed; omitted = keep the current ones.
    const current = providerData.current;
    const same = current.provider === provider;
    const baseUrl = $("custom-base-url").value.trim();
    const keyEnv = $("custom-api-key-env").value.trim();
    if (baseUrl !== (same ? current.base_url || "" : "")) body.base_url = baseUrl;
    if (keyEnv !== (same ? current.api_key_env || "" : "")) body.api_key_env = keyEnv;
    // #476: only for a provider that reads the field, and only when changed. The region
    // is never sent while a variable pins it; an empty box keeps the saved region.
    const info = currentProvider();
    if (info && info.uses_organization) {
      const orgEnv = $("custom-organization-env").value.trim();
      if (orgEnv !== (same ? providerData.organization_env || "" : "")) body.organization_env = orgEnv;
    }
    if (info && info.uses_region && !providerData.region_pinned_by) {
      const region = $("custom-region").value.trim();
      if (region && region !== providerData.region) body.region = region;
    }
    return body;
  }

  function renderModels() {
    const provider = currentProvider();
    const list = $("model-list");
    list.replaceChildren();
    if (!provider) return;
    credentialBadge(provider.credentials, provider.local);
    const notes = [];
    if (!provider.installed) notes.push(t("settings.provider_not_installed", { extra: provider.extra || "" }));
    if (provider.local) notes.push(t("settings.provider_local_note", { url: provider.default_base_url || "" }));
    (provider.credentials.warnings || []).forEach((w) => notes.push(w));
    $("provider-note").textContent = notes.join(" ");
    const current = providerData.current;
    const sameProvider = current.provider === provider.name;
    $("custom-base-url").placeholder = provider.default_base_url || "";
    $("custom-base-url").value = sameProvider && current.base_url ? current.base_url : "";
    $("custom-api-key-env").value = sameProvider && current.api_key_env ? current.api_key_env : "";
    // #476: variable NAMES and the saved region only; no value of a variable is shown.
    $("organization-env-row").hidden = !provider.uses_organization;
    $("organization-env-hint").hidden = !provider.uses_organization;
    $("custom-organization-env").value = sameProvider && providerData.organization_env
      ? providerData.organization_env : "";
    const pin = providerData.region_pinned_by || "";
    $("region-row").hidden = !provider.uses_region;
    $("region-hint").hidden = !provider.uses_region;
    $("custom-region").value = providerData.region || "";
    $("custom-region").disabled = Boolean(pin);
    $("region-pinned").hidden = !(provider.uses_region && pin);
    $("region-pinned").textContent = pin ? t("mset.region_pinned", { env: pin }) : "";
    showModelError("");

    let anyChecked = false;
    provider.models.forEach((model) => {
      const label = document.createElement("label");
      label.className = `option-card${model.current ? " current" : ""}`;
      const input = document.createElement("input");
      input.type = "radio";
      input.name = "model-choice";
      input.value = model.id;
      if (model.current) {
        input.checked = true;
        anyChecked = true;
      }
      const info = document.createElement("span");
      info.className = "model-info";
      const name = document.createElement("strong");
      name.textContent = model.id;
      info.append(name);
      const badges = document.createElement("span");
      badges.className = "cap-badges";
      Object.keys(BADGES).forEach((cap) => {
        if (!model.capabilities[cap]) return;
        const badge = document.createElement("span");
        badge.className = "badge";
        badge.dataset.state = "info";
        badge.textContent = BADGES[cap]();
        badges.append(badge);
      });
      if (!model.capabilities.known) {
        const badge = document.createElement("span");
        badge.className = "badge";
        badge.dataset.state = "warn";
        badge.textContent = t("settings.cap_unknown");
        badges.append(badge);
      }
      if (model.current) {
        const badge = document.createElement("span");
        badge.className = "badge";
        badge.dataset.state = "ok";
        badge.textContent = t("settings.active");
        badges.append(badge);
      }
      if (model.roles.length) {
        const meta = document.createElement("span");
        // Internal role names are developer detail (#227): shown with "Show internal names".
        meta.className = "meta internal-name";
        meta.textContent = `roles: ${model.roles.join(", ")}`;
        info.append(meta);
      }
      info.append(badges);
      label.append(input, info);
      list.append(label);
    });
    if (!anyChecked) {
      const first = list.querySelector('input[name="model-choice"]');
      if (first) first.checked = true;
      else $("model-custom-radio").checked = true;
    }
    $("custom-model-id").value = "";
  }

  async function loadModels() {
    providerData = await api("/api/config/providers");
    const select = $("provider-select");
    const keep = select.value;
    select.replaceChildren();
    providerData.providers.forEach((p) => {
      const option = document.createElement("option");
      option.value = p.name;
      option.textContent = p.installed ? p.name : `${p.name} (${t("settings.not_installed")})`;
      select.append(option);
    });
    select.value = keep && providerData.providers.some((p) => p.name === keep)
      ? keep : providerData.current.provider;
    $("model-selected").textContent = selectionLabel(providerData.current);
    $("model-running").textContent = providerData.running
      ? selectionLabel(providerData.running) : t("settings.running_unknown");
    $("model-applies").textContent = providerData.applies === "next_turn"
      ? t("settings.takes_effect_from_the_next_turn_agentcon")
      : t("settings.applies_after_restart");
    const pinned = providerData.pinned_by_env || [];
    $("model-pinned").hidden = pinned.length === 0;
    $("model-pinned").textContent = pinned.length
      ? t("settings.pinned_by_env", { names: pinned.join(", ") }) : "";
    renderModels();
  }

  // The settings hub (#227) counts an unapplied model choice as an unsaved change
  // and can apply it from its footer; it listens for these events.
  const notifyDraft = () => document.dispatchEvent(new CustomEvent("stira:model-draft"));
  $("provider-select").addEventListener("change", () => { renderModels(); notifyDraft(); });
  $("custom-model-id").addEventListener("input", () => {
    $("model-custom-radio").checked = true;
    notifyDraft();
  });
  [
    "model-list", "model-custom-radio", "custom-base-url", "custom-api-key-env",
    "custom-organization-env", "custom-region",
  ].forEach((id) => {
    $(id).addEventListener("change", notifyDraft);
  });

  async function useModel() {
    const button = $("model-use");
    const selection = chosenSelection();
    if (!selection.model_id) {
      toast(t("settings.model_id_required"), true);
      return false;
    }
    if (!providerData) return false;
    // The version this page loaded: a save made elsewhere in between is refused (#195).
    const body = { ...selection, base_version: providerData.version };
    button.disabled = true;
    try {
      const response = await stiraFetch("/api/config/models", {
        method: "POST",
        cache: "no-store",
        credentials: "same-origin",
        idempotent: true,
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await response.json().catch(() => ({}));
      if (!response.ok) {
        const text = refusalText(data, response.status);
        if (data.error_code === "conflict") {
          // Show what was saved elsewhere; the person checks it and saves again.
          await loadModels().catch(() => {});
          notifyDraft();
        }
        showModelError(text, data.field);
        toast(t("settings.switch_failed_message", { message: text }), true);
        return false;
      }
      toast(t("settings.model_switched_id", { id: `${body.provider} / ${body.model_id}` }));
      await loadModels();
      document.dispatchEvent(new CustomEvent("stira:model-saved"));
      return true;
    } catch (error) {
      toast(t("settings.switch_failed_message", { message: shownText(error) }), true);
      return false;
    } finally {
      button.disabled = false;
    }
  }
  $("model-use").addEventListener("click", useModel);

  window.StiraModelSection = {
    dirty() {
      if (!providerData) return false;
      const body = chosenSelection();
      const current = providerData.current;
      return body.provider !== current.provider
        || Boolean(body.model_id && body.model_id !== current.model_id)
        || "base_url" in body || "api_key_env" in body
        || "organization_env" in body || "region" in body;
    },
    label() {
      const body = chosenSelection();
      return `${body.provider} / ${body.model_id || "—"}`;
    },
    save: useModel,
    reset() {
      if (!providerData) return;
      $("provider-select").value = providerData.current.provider;
      renderModels();
      notifyDraft();
    },
  };

  $("conn-test").addEventListener("click", async () => {
    const button = $("conn-test");
    const out = $("conn-test-result");
    const body = chosenSelection();
    if (!body.model_id) {
      out.textContent = t("settings.model_id_required");
      return;
    }
    button.disabled = true;
    out.className = "";
    out.textContent = t("settings.testing");
    try {
      const response = await stiraFetch("/api/config/models/test", {
        method: "POST",
        cache: "no-store",
        credentials: "same-origin",
        idempotent: true,
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await response.json().catch(() => ({}));
      if (data && typeof data.ok === "boolean") {
        out.className = data.ok ? "test-ok" : "test-fail";
        out.textContent = data.ok
          ? t("settings.test_ok", { ms: data.latency_ms })
          : t("settings.test_failed", { reason: reasonText(data) });
      } else {
        out.className = "test-fail";
        out.textContent = t("settings.test_failed", { reason: refusalText(data, response.status) });
      }
    } catch (error) {
      out.className = "test-fail";
      out.textContent = t("settings.test_failed", { reason: shownText(error) });
    } finally {
      button.disabled = false;
    }
  });

  // --- Token usage (#195) ----------------------------------------------------
  // Read-only: totals and one row per model from GET /api/observability/usage. A count
  // nobody reported arrives as null and reads "unknown"; it is never turned into 0.

  const USAGE_COUNTS = ["input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"];

  function usageNumber(value) {
    return typeof value === "number" && Number.isFinite(value)
      ? value.toLocaleString(document.documentElement.lang || undefined)
      : t("usage.unknown");
  }

  function usageRow(label, row) {
    const tr = document.createElement("tr");
    const head = document.createElement("th");
    head.scope = "row";
    head.textContent = label;
    tr.append(head);
    ["turns", ...USAGE_COUNTS].forEach((key) => {
      const cell = document.createElement("td");
      cell.textContent = usageNumber(row[key]);
      tr.append(cell);
    });
    return tr;
  }

  function showUsage(status, rows, note) {
    $("usage-status").textContent = status;
    $("usage-rows").replaceChildren(...rows);
    $("usage-table").hidden = rows.length === 0;
    $("usage-note").textContent = note;
    $("usage-note").hidden = !note;
  }

  function renderUsage(data) {
    const total = data.total && typeof data.total === "object" ? data.total : {};
    const models = Array.isArray(data.by_model) ? data.by_model : [];
    if (!(total.turns > 0)) {
      showUsage(t("usage.empty"), [], "");
      return;
    }
    const rows = [usageRow(t("usage.total"), total)];
    models.forEach((row) => {
      const name = typeof row.model === "string" && row.model ? row.model : t("usage.model_unknown");
      rows.push(usageRow(name, row));
    });
    const notes = [];
    if (total.turns_without_usage > 0) {
      notes.push(t("usage.without_usage", { n: usageNumber(total.turns_without_usage) }));
    }
    if (data.truncated === true) notes.push(t("usage.truncated"));
    showUsage("", rows, notes.join(" "));
  }

  async function loadUsage() {
    let response;
    try {
      response = await stiraFetch("/api/observability/usage", {
        cache: "no-store", credentials: "same-origin",
      });
    } catch (error) {
      showUsage(shownText(error), [], "");
      return;
    }
    if (response.status === 404) {
      // A server without turn records (the observation console) has no such route.
      showUsage(t("usage.unavailable"), [], "");
      return;
    }
    const data = response.ok ? await response.json().catch(() => null) : null;
    if (!data || typeof data !== "object") {
      showUsage(t("usage.load_failed", { status: response.status }), [], "");
      return;
    }
    renderUsage(data);
  }
  $("usage-refresh").addEventListener("click", loadUsage);

  // --- SOUL preview --------------------------------------------------------

  async function loadSoul() {
    const data = await api("/api/config/soul");
    $("soul-path").textContent = data.path;
    $("soul-exists").textContent = data.exists ? t("chat.loaded") : t("settings.not_found");
    $("soul-preview").value = data.preview || "";
  }

  async function initialize() {
    loadUsage();
    try {
      await Promise.all([loadModels(), loadSoul()]);
      notifyDraft();
    } catch (error) {
      toast(t("settings.loading_failed_message", { message: shownText(error) }), true);
    }
  }

  initialize();
})();
