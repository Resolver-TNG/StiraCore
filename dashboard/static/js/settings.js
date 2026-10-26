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
    if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
    return data;
  }

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
  ["model-list", "model-custom-radio", "custom-base-url", "custom-api-key-env"].forEach((id) => {
    $(id).addEventListener("change", notifyDraft);
  });

  async function useModel() {
    const button = $("model-use");
    const body = chosenSelection();
    if (!body.model_id) {
      toast(t("settings.model_id_required"), true);
      return false;
    }
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
        throw new Error(data.reason ? reasonText(data) : data.error || `HTTP ${response.status}`);
      }
      toast(t("settings.model_switched_id", { id: `${body.provider} / ${body.model_id}` }));
      await loadModels();
      document.dispatchEvent(new CustomEvent("stira:model-saved"));
      return true;
    } catch (error) {
      toast(t("settings.switch_failed_message", { message: error.message }), true);
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
        || "base_url" in body || "api_key_env" in body;
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
        out.textContent = t("settings.test_failed", { reason: data.error || `HTTP ${response.status}` });
      }
    } catch (error) {
      out.className = "test-fail";
      out.textContent = t("settings.test_failed", { reason: error.message });
    } finally {
      button.disabled = false;
    }
  });

  // --- SOUL preview --------------------------------------------------------

  async function loadSoul() {
    const data = await api("/api/config/soul");
    $("soul-path").textContent = data.path;
    $("soul-exists").textContent = data.exists ? t("chat.loaded") : t("settings.not_found");
    $("soul-preview").value = data.preview || "";
  }

  async function initialize() {
    try {
      await Promise.all([loadModels(), loadSoul()]);
      notifyDraft();
    } catch (error) {
      toast(t("settings.loading_failed_message", { message: error.message }), true);
    }
  }

  initialize();
})();
