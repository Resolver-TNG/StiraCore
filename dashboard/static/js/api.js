// Shared fetch wrapper: attaches the CSRF token to unsafe requests (#185).
// Usage: stiraFetch(url, { method: "POST", ..., idempotent: true })
//
// Confirmed operations (ADR 0012 §8b, only while the server runs with STIRA_YOLO_ALLOW):
// a loosening request (RECOVER, a loosening save, a scheduler create / resume; an approval
// card's approve / edit is one press since phase 2c) is answered 428
// `host_confirmation_required` with a one-time operation. Then:
// * channel "browser" (the default, revision 6): the shared confirmation dialog shows what
//   the operation is in a fixed format (enums and short codes from the server only) and
//   asks for one tick ("I checked what this is"; phase 2c: a checkbox instead of a typed
//   word, the confirm button is disabled until it is ticked); on confirm it posts the
//   operation's one-time token to
//   /api/host-operations/<id>/confirm. Only the operation this very request created is
//   ever shown.
// * channel "command" / "dialog" (STIRA_YOLO_HOST_CONFIRM): a notice says where to
//   confirm on the host and GET /api/host-operations/<id> is polled until it decided.
// Only when it was confirmed is the same request sent once more with the
// X-Stira-Host-Operation header. Cancel is final the moment it is pressed: whatever a
// later poll answers, nothing is re-sent (the server-side cancel is best effort). The page
// receives the final response, or a 409 with the outcome code (host_operation_cancelled /
// _denied / _expired / ...). Without the allowance the server never answers 428 this way
// and nothing here runs.
(function () {
  const meta = document.querySelector('meta[name="csrf-token"]');
  let token = meta ? meta.content : "";
  const HOST_HEADER = "X-Stira-Host-Operation";
  const HOST_ID = /^[0-9a-f]{32}$/;
  const SHORT = /^[0-9a-f]{8}$/;
  const COMMAND = /^stira approvals decide [0-9a-f]{8}$/;
  const HOST_POLL_MS = 1500;
  const TOKEN = /^[0-9a-f]{32}$/;
  const INSTANCE = /^[0-9a-f]{16}$/;
  const SAFE = /^[a-z0-9_:#.+-]{1,48}$/;
  const KINDS = ["rules_loosen", "recover", "scheduler_loosen", "yolo_unlatch", "yolo_settings"];
  // The fixed order of what the dialog lists (host_ops summary keys).
  const FIELDS = ["action", "operation", "tool", "args", "card", "source", "changes", "job", "count"];
  // Host outcome -> the code the page maps to its wording (host.code_<code>).
  const OUTCOME_CODES = {
    denied: "host_operation_denied",
    cancelled: "host_operation_cancelled",
    expired: "host_operation_expired",
    used: "host_operation_used",
    stale: "stale_operation",
    unknown: "host_operation_unknown",
  };
  // Refusals of the browser confirmation the pages map themselves (host.code_<code>).
  const BROWSER_REFUSALS = new Set([
    "host_not_acknowledged", "host_browser_refused", "host_confirm_on_host",
    "host_operation_incarnation", "host_operation_expired", "host_operation_cancelled",
    "host_operation_used", "host_operation_mismatch", "host_operation_unknown",
    "host_channel_unavailable", "stale_operation", "tool_call_running",
  ]);
  const tr = (key, vars) => (typeof window.t === "function" ? window.t(key, vars) : key);

  async function ensureToken() {
    if (token) return token;
    const response = await fetch("/api/csrf", { headers: { Accept: "application/json" } });
    token = (await response.json()).token;
    return token;
  }

  function hostNotice(operation, onCancel) {
    let box = document.getElementById("host-confirm-notice");
    if (!box) {
      box = document.createElement("div");
      box.id = "host-confirm-notice";
      box.className = "host-confirm-notice";
      box.setAttribute("role", "status");
      box.setAttribute("aria-live", "polite");
      const style = box.style;
      style.position = "fixed";
      style.right = "16px";
      style.bottom = "16px";
      style.maxWidth = "28rem";
      style.zIndex = "1000";
      style.padding = "12px 14px";
      style.border = "2px solid currentColor";
      style.borderRadius = "8px";
      style.background = "Canvas";
      style.color = "CanvasText";
      document.body.appendChild(box);
    }
    box.textContent = "";
    const text = document.createElement("p");
    text.className = "host-confirm-text";
    const button = document.createElement("button");
    button.type = "button";
    button.className = "host-confirm-cancel";
    button.textContent = tr("host.cancel");
    button.addEventListener("click", onCancel);
    box.appendChild(text);
    box.appendChild(button);
    const short = SHORT.test(operation.short) ? operation.short : "?";
    const code = SHORT.test(operation.code) ? operation.code : "?";
    const update = (seconds) => {
      const vars = { short, code, seconds: Number.isInteger(seconds) ? seconds : "?" };
      if (operation.channel === "dialog") {
        text.textContent = tr("host.waiting_dialog", vars);
      } else {
        const command = COMMAND.test(operation.command) ? operation.command : `stira approvals decide ${short}`;
        text.textContent = tr("host.waiting_command", { ...vars, command });
      }
    };
    update(operation.expires_in);
    return { update, close: () => box.remove() };
  }

  const pause = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  // Best effort: the operation also ends by its expiry; the page has already given up.
  function sendCancel(id) {
    Promise.resolve()
      .then(() => window.stiraFetch(`/api/host-operations/${id}/cancel`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: "{}",
      }))
      .catch(() => {});
  }

  function waitForHost(operation) {
    const id = encodeURIComponent(operation.id);
    const limit = Date.now() + ((Number(operation.expires_in) || 120) + 45) * 1000;
    return new Promise((resolve) => {
      let settled = false;
      let notice = null;
      const finish = (state) => {
        if (settled) return;
        settled = true;
        if (notice) notice.close();
        resolve(state);
      };
      // Cancel is final at once (review of phase 2): the notice closes, the page hears
      // "cancelled", and a later poll that says "confirmed" re-sends nothing.
      notice = hostNotice(operation, () => {
        if (settled) return;
        finish("cancelled");
        sendCancel(id);
      });
      (async () => {
        while (!settled && Date.now() < limit) {
          await pause(HOST_POLL_MS);
          if (settled) return;
          let data = null;
          let status = 0;
          try {
            const response = await fetch(`/api/host-operations/${id}`, { headers: { Accept: "application/json" }, cache: "no-store" });
            status = response.status;
            data = await response.json();
          } catch (e) {
            continue;
          }
          if (settled) return;
          if (status === 404) return finish("unknown");
          const state = data && data.operation && data.operation.state;
          if (state === "pending") {
            notice.update(data.operation.expires_in);
            continue;
          }
          if (typeof state === "string") return finish(state);
        }
        finish("expired");
      })();
    });
  }

  function dialogItems(operation) {
    const summary = operation.summary && typeof operation.summary === "object" ? operation.summary : {};
    const items = [];
    for (const key of FIELDS) {
      if (!Object.prototype.hasOwnProperty.call(summary, key)) continue;
      let value = summary[key];
      if (Array.isArray(value)) value = value.filter((v) => typeof v === "string" && SAFE.test(v)).join(", ") || "-";
      else if (Number.isInteger(value)) value = String(value);
      else if (typeof value !== "string" || !SAFE.test(value)) value = "?";
      items.push(`${tr(`host.field_${key}`)}: ${value}`);
    }
    items.push(tr("host.browser_code", { code: SHORT.test(operation.code) ? operation.code : "?" }));
    const seconds = Number.isInteger(operation.expires_in) ? operation.expires_in : 120;
    items.push(tr("host.browser_expires", { seconds }));
    return items;
  }

  // The browser channel (revision 6): the person reads the fixed-format confirmation of the
  // operation their press just created and ticks the box. -> "confirmed" | "cancelled" |
  // { code } (a refusal the page maps).
  async function confirmInBrowser(operation) {
    const id = encodeURIComponent(operation.id);
    const dialog = window.StiraConfirm;
    const kind = KINDS.includes(operation.kind) ? operation.kind : null;
    if (!dialog || typeof dialog.open !== "function" || !kind || !TOKEN.test(operation.confirm_token)
        || !INSTANCE.test(operation.instance)) {
      sendCancel(id);
      return { code: "host_channel_unavailable" };
    }
    const ok = await dialog.open({
      title: tr("host.browser_title", { what: tr(`host.kind_${kind}`) }),
      intro: tr("host.browser_intro"),
      items: dialogItems(operation),
      checks: [tr("host.browser_check")],
      confirmLabel: tr("host.browser_confirm"),
      cancelLabel: tr("host.cancel"),
      tone: "danger",
      // #650: the press that led here may still be inside its own dialog's busy window
      // (StiraConfirm onConfirm, e.g. Resume on /schedules); this is that request's question.
      followUp: true,
    });
    if (!ok) {
      sendCancel(id);
      return "cancelled";
    }
    try {
      const response = await window.stiraFetch(`/api/host-operations/${id}/confirm`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({ token: operation.confirm_token, acknowledged: true, instance: operation.instance }),
      });
      const data = await response.json().catch(() => ({}));
      if (response.ok && data && data.operation && data.operation.state === "confirmed") return "confirmed";
      const code = data && typeof data.code === "string" && BROWSER_REFUSALS.has(data.code) ? data.code : "host_operation_unknown";
      return { code };
    } catch (e) {
      sendCancel(id);
      return { code: "host_channel_unavailable" };
    }
  }

  // The YOLO request's confirmation (browser channel; ADR 0012 §1a step 3, Open (13)): the
  // press's 202 answer {request, confirm}. One box, and for tier external a second one
  // ("sending outside becomes possible"); the confirm button is disabled until every box is
  // ticked. -> "confirmed" | "cancelled" | { code }. The YOLO button itself is phase 3.
  async function confirmYolo(answer) {
    const request = answer && answer.request;
    const confirm = answer && answer.confirm;
    const dialog = window.StiraConfirm;
    if (!request || !HOST_ID.test(request.id) || !confirm || !TOKEN.test(confirm.token)
        || !INSTANCE.test(confirm.instance) || !dialog || typeof dialog.open !== "function") {
      return { code: "host_channel_unavailable" };
    }
    const id = encodeURIComponent(request.id);
    const external = confirm.external === true;
    // Phase 7 (ADR 0012 §9 option C): a request that includes exec (always tier external)
    // asks for a third box naming what commands can do.
    const exec = external && confirm.exec === true;
    const tier = exec ? "exec" : external ? "external" : "internal";
    const ends = Number.isFinite(request.ends_at) ? new Date(request.ends_at * 1000) : null;
    const clock = ends ? `${String(ends.getHours()).padStart(2, "0")}:${String(ends.getMinutes()).padStart(2, "0")}` : "--:--";
    const seconds = Number.isInteger(request.expires_in) ? request.expires_in : 120;
    const ok = await dialog.open({
      title: tr("host.browser_title", { what: tr("host.kind_yolo_request") }),
      intro: tr("host.browser_intro"),
      items: [
        `${tr("host.field_tier")}: ${tr(`host.tier_${tier}`)}`,
        `${tr("host.field_request")}: ${SHORT.test(request.short) ? request.short : "?"}`,
        tr("host.yolo_ends", { time: clock }),
        tr("host.browser_expires", { seconds }),
      ],
      checks: [
        tr("host.browser_check"),
        ...(external ? [tr("host.browser_check_external")] : []),
        ...(exec ? [tr("host.browser_check_exec")] : []),
      ],
      confirmLabel: tr("host.browser_confirm_yolo"),
      cancelLabel: tr("host.cancel"),
      tone: "danger",
    });
    const cancel = () => Promise.resolve()
      .then(() => window.stiraFetch("/api/yolo/request/cancel", {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({ request_id: request.id }),
      }))
      .catch(() => {});
    if (!ok) {
      cancel();
      return "cancelled";
    }
    try {
      const response = await window.stiraFetch(`/api/host-operations/${id}/confirm`, {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({
          token: confirm.token, acknowledged: true, external_acknowledged: external, instance: confirm.instance,
          ...(exec ? { exec_acknowledged: true } : {}),
        }),
      });
      const data = await response.json().catch(() => ({}));
      if (response.ok) return "confirmed";
      const code = data && typeof data.code === "string" && BROWSER_REFUSALS.has(data.code) ? data.code : "host_operation_unknown";
      return { code };
    } catch (e) {
      cancel();
      return { code: "host_channel_unavailable" };
    }
  }

  // Shared with the Stage's approval client (it has its own fetch) and the YOLO button.
  window.StiraHostConfirm = { browser: confirmInBrowser, yolo: confirmYolo };

  function hostRefusal(state) {
    const code = state && typeof state === "object" && typeof state.code === "string"
      ? state.code
      : (OUTCOME_CODES[state] || "host_operation_unknown");
    // ``local``: worded by this page (not a server sentence), so pages show no detail line.
    const body = { error: tr(`host.code_${code}`), code, error_code: code, errors: [], local: true };
    return new Response(JSON.stringify(body), { status: 409, headers: { "Content-Type": "application/json" } });
  }

  window.stiraFetch = async function stiraFetch(url, options = {}) {
    const { idempotent, ...init } = options;
    const method = (init.method || "GET").toUpperCase();
    if (method === "GET" || method === "HEAD" || method === "OPTIONS") return fetch(url, init);
    const headers = new Headers(init.headers || {});
    headers.set("X-CSRF-Token", await ensureToken());
    if (idempotent && !headers.has("Idempotency-Key")) {
      headers.set("Idempotency-Key", crypto.randomUUID());
    }
    const response = await fetch(url, { ...init, headers });
    if (response.status !== 428 || headers.has(HOST_HEADER)) return response;
    let data = null;
    try {
      data = await response.clone().json();
    } catch (e) {
      return response;
    }
    const operation = data && data.operation;
    if (!data || data.code !== "host_confirmation_required" || !operation || !HOST_ID.test(operation.id)) {
      return response;
    }
    const state = operation.channel === "browser" ? await confirmInBrowser(operation) : await waitForHost(operation);
    if (state !== "confirmed") return hostRefusal(state);
    const again = new Headers(headers);
    again.set(HOST_HEADER, operation.id);
    if (again.has("Idempotency-Key")) again.set("Idempotency-Key", crypto.randomUUID());
    return fetch(url, { ...init, headers: again });
  };
})();
