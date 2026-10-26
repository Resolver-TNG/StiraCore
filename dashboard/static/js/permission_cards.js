/* Permission cards for failed writes (#244), in the same frame as the approval cards (#189).
 *
 * StiraPermissions.create({ list, panel, live, onRetry }) renders every open
 * /api/diagnostics/write-failures card: what failed (operation, path), why (category and
 * evidence), whose permission is needed (the estimated responsible process) and how a
 * person fixes it (text only — Stira never changes OS protections itself), plus a settings
 * deep link when one exists. "Fixed it → retry once" consumes the card's single retry on the
 * server, which re-checks the cause read-only; unless the cause is still there, onRetry(prompt)
 * sends one ordinary chat message (fixed words, no model-supplied text) asking the agent to
 * retry; every gate applies again.
 */
(function (root) {
  "use strict";
  const LINK_PREFIXES = ["x-apple.systempreferences:", "ms-settings:"];
  const CATEGORIES = new Set(["locked_flag", "protected_by_os", "readonly_volume", "wrong_cwd",
    "outside_write_roots", "identity_protected", "consent_blocked", "permission_denied", "other"]);
  const REMEDIES = new Set(["locked_flag", "locked_flag_system", "readonly_attr", "tcc", "sip", "os",
    "readonly_volume", "wrong_cwd", "outside_write_roots", "identity_protected", "secret_path", "os_protection",
    "consent_blocked", "breaker", "permission_denied", "acl", "other"]);
  const PRINCIPALS = new Set(["terminal", "app", "launchd", "process", "user"]);

  function make(doc, tag, className, content) {
    const node = doc.createElement(tag);
    if (className) node.className = className;
    if (content !== undefined && content !== null) node.textContent = content;
    return node;
  }

  function safeLink(href) {
    return typeof href === "string" && LINK_PREFIXES.some(prefix => href.startsWith(prefix)) ? href : null;
  }

  function categoryLabel(category) {
    return t(`diagnostics.cat_${CATEGORIES.has(category) ? category : "other"}`);
  }

  function principalText(info) {
    const kind = PRINCIPALS.has(info?.kind) ? info.kind : "process";
    return t(`diagnostics.principal_${kind}`, { name: info?.name || "?", executable: info?.executable || "?" });
  }

  function create(options = {}) {
    const doc = options.document || root.document;
    const list = options.list, panel = options.panel, live = options.live;
    const headingLevel = Number.isInteger(options.headingLevel) ? options.headingLevel : 4;
    const cards = root.StiraCards;
    const getJSON = options.getJSON || (async url => {
      const response = await fetch(url, { cache: "no-store", headers: { Accept: "application/json" } });
      if (!response.ok) throw new Error(String(response.status));
      return response.json();
    });
    const post = options.post || (url => root.stiraFetch(url, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: "{}",
    }));
    const el = (tag, className, content) => make(doc, tag, className, content);
    const entries = new Map(); // id -> { card, parts, data }
    let loading = false;

    function announce(text) {
      if (!live) return;
      live.textContent = "";
      live.textContent = text;
    }

    function fact(dl, label, value) {
      const row = el("div");
      const node = value !== null && typeof value === "object";
      const dd = el("dd", null, node ? null : (value ?? "—"));
      if (node) dd.append(value);
      if (value === null || value === undefined) dd.dataset.trend = "missing";
      row.append(el("dt", null, label), dd);
      dl.append(row);
      return dd;
    }

    function button(label, action, variant) {
      const node = el("button", `btn ${variant}`, label);
      node.type = "button";
      node.dataset.permissionAction = action;
      return node;
    }

    function recheckText(recheck) {
      if (!recheck) return "";
      if (recheck.state === "still_blocked") {
        return t("diagnostics.still_blocked", { category: categoryLabel(recheck.category) });
      }
      return t(recheck.state === "clear" ? "diagnostics.recheck_clear" : "diagnostics.recheck_unverified");
    }

    function build(data) {
      const safe = String(data.id).replace(/[^\w-]/g, "");
      const spec = { kind: "permission", title: data.tool || "?", status: "error",
        brief: categoryLabel(data.category), meta: data.errno || null };
      const box = cards.card(doc, spec);
      box.className = "stira-card approval-card permission-card";
      box.open = true;
      box.dataset.cardId = data.id;
      box.dataset.category = data.category;
      box.setAttribute("aria-labelledby", `permission-title-${safe}`);
      const heading = el(`h${headingLevel}`, "approval-heading", t("diagnostics.card_title", { tool: data.tool || "?" }));
      heading.id = `permission-title-${safe}`;
      const dl = el("dl", "facts approval-facts");
      fact(dl, t("diagnostics.what"), [data.operation, data.path].filter(Boolean).join(" · ") || null);
      fact(dl, t("diagnostics.why"), categoryLabel(data.category));
      const evidence = (Array.isArray(data.evidence) ? data.evidence : []).filter(Boolean);
      if (data.errno && !evidence.some(item => String(item).includes(data.errno))) evidence.push(data.errno);
      fact(dl, t("diagnostics.evidence"), evidence.length ? evidence.join(", ") : null);
      fact(dl, t("diagnostics.who"), principalText(data.principal));
      fact(dl, t("diagnostics.how"), t(`diagnostics.fix_${REMEDIES.has(data.remedy) ? data.remedy : "other"}`));
      const href = safeLink(data.deep_link);
      if (href) {
        const link = el("a", "permission-link", t("diagnostics.open_settings"));
        link.setAttribute("href", href);
        fact(dl, t("diagnostics.settings"), link);
      }
      const message = el("pre", "tool-payload-text approval-args", data.message || "");
      message.setAttribute("tabindex", "0");
      message.setAttribute("aria-label", t("diagnostics.message"));
      const note = el("p", "tool-payload-note", t("diagnostics.human_only"));
      const actions = el("div", "approval-actions");
      const retry = button(t("diagnostics.retry_once"), "retry", "btn-primary");
      const dismiss = button(t("diagnostics.dismiss"), "dismiss", "btn-secondary");
      actions.append(retry, dismiss);
      const status = el("p", "approval-note permission-status");
      status.setAttribute("role", "status");
      const error = el("p", "error approval-error");
      error.setAttribute("role", "alert");
      box.parts.body.append(heading, dl, message, note, actions, status, error);
      const parts = { retry, dismiss, status, error };
      retry.addEventListener("click", () => void doRetry(data.id));
      dismiss.addEventListener("click", () => void doDismiss(data.id));
      return { box, parts };
    }

    function apply(entry, data) {
      entry.data = data;
      entry.parts.retry.hidden = data.retry !== "available";
      entry.parts.status.textContent = data.retry === "unavailable" ? t("diagnostics.no_retry")
        : data.retry === "retried" ? recheckText(data.recheck) : "";
    }

    function render(items) {
      const seen = new Set();
      for (const data of items) {
        if (!data || typeof data.id !== "string") continue;
        seen.add(data.id);
        let entry = entries.get(data.id);
        if (!entry) {
          const built = build(data);
          entry = { card: built.box, parts: built.parts, data };
          entries.set(data.id, entry);
          list.append(entry.card);
          announce(t("diagnostics.announce_new", { tool: data.tool || "?", category: categoryLabel(data.category) }));
        }
        if (!entry.busy) apply(entry, data);
      }
      for (const [id, entry] of entries) {
        if (!seen.has(id)) { entry.card.remove(); entries.delete(id); }
      }
      if (panel) panel.hidden = entries.size === 0;
      return entries.size;
    }

    async function refresh() {
      if (loading || !list) return null;
      loading = true;
      try {
        const payload = await getJSON("/api/diagnostics/write-failures");
        return render(Array.isArray(payload?.cards) ? payload.cards : []);
      } catch {
        return null;
      } finally { loading = false; }
    }

    async function doRetry(id) {
      const entry = entries.get(id);
      if (!entry || entry.busy) return;
      entry.busy = true;
      entry.parts.retry.disabled = true;
      entry.parts.error.textContent = "";
      try {
        const response = await post(`/api/diagnostics/write-failures/${encodeURIComponent(id)}/retry`);
        const body = await response.json().catch(() => ({}));
        if (body?.card) apply(entry, body.card);
        if (!response.ok) {
          entry.parts.error.textContent = t(body?.code === "already_retried" ? "diagnostics.error_already_retried"
            : body?.code === "not_retryable" ? "diagnostics.no_retry" : "diagnostics.error_failed");
          return;
        }
        const recheck = body.card?.recheck;
        announce(recheckText(recheck));
        if (recheck?.state !== "still_blocked") {
          // Fixed words only: the path and the error text come from the model's own call and
          // must not be relayed as if the person had written them (prompt laundering).
          const tool = entry.data.tool === "editor" ? "editor" : "file_write";
          const operation = /^[a-z_]{1,16}$/.test(entry.data.operation || "") ? entry.data.operation : "write";
          options.onRetry?.(t("diagnostics.retry_prompt", { tool, operation }));
        }
      } catch {
        entry.parts.error.textContent = t("diagnostics.error_failed");
      } finally {
        entry.busy = false;
        entry.parts.retry.disabled = false;
        entry.parts.retry.hidden = entry.data.retry !== "available";
      }
    }

    async function doDismiss(id) {
      const entry = entries.get(id);
      if (!entry) return;
      try {
        const response = await post(`/api/diagnostics/write-failures/${encodeURIComponent(id)}/dismiss`);
        if (!response.ok && response.status !== 404) throw new Error(String(response.status));
        entry.card.remove();
        entries.delete(id);
        if (panel) panel.hidden = entries.size === 0;
        options.onEmpty?.(entries.size);
      } catch {
        entry.parts.error.textContent = t("diagnostics.error_failed");
      }
    }

    return { refresh, render, retry: doRetry, dismiss: doDismiss, entries };
  }

  root.StiraPermissions = { create, safeLink, categoryLabel, principalText };
})(typeof window !== "undefined" ? window : globalThis);
