// Chat core (#350): the send / stream / stop / re-send / image logic shared by the chat page
// (chat.js) and the Stage (dashboard/stage/js/talk.js). No DOM, no global state: every
// network call goes through the injected fetch (the page's stiraFetch, which adds the CSRF
// token), so both pages talk to the same POST /api/chat with the same protections and the
// module runs in Node tests (tests/chat_core_test.cjs).
//
// Loaded as a classic script (window.StiraChatCore) and, in Node, as a CommonJS module.
(function (root) {
  "use strict";
  const MAX_IMAGE_BYTES = 10 * 1024 * 1024;
  const MAX_IMAGES = 5;
  const IMAGE_TYPES = new Set(["image/png", "image/jpeg", "image/gif", "image/webp"]);
  // Refusals after which the server guarantees nothing ran (validation, CSRF/Origin,
  // unknown route/method, too large, bad type): the key may go. 409 duplicate_request
  // is handled on its own; 5xx, other 4xx and SSE errors keep the key (#185).
  const NO_EFFECT_STATUS = new Set([400, 403, 404, 405, 413, 415, 422]);
  const tr = (t, key, vars) => (typeof t === "function" ? t(key, vars) : key);
  // Why the model call failed (code model_failure): a fixed reason in the page's language,
  // with the provider and host the server named (never the provider's own text).
  const MODEL_FAILURES = new Set(["refused", "dns_failed", "unreachable", "timeout", "auth_failed",
    "model_not_found", "throttled", "server_error", "missing_credential", "provider_unavailable",
    "bad_request", "context_overflow", "ends_with_assistant", "context_window_too_small"]);
  function modelFailureText(t, data) {
    if (data.code !== "model_failure" || !MODEL_FAILURES.has(data.reason) || typeof t !== "function") return "";
    const key = `chat.model_failure_${data.reason}`;
    const text = t(key);
    if (text === key) return "";
    const where = [data.provider, data.host].filter(part => typeof part === "string" && part).join(" · ");
    return where ? text + t("chat.model_failure_where", { where }) : text;
  }

  function newKey() {
    const c = root.crypto;
    return c && c.randomUUID ? c.randomUUID()
      : `k${Date.now().toString(36)}${Math.random().toString(36).slice(2, 10)}`;
  }

  // Enter sends, Shift+Enter is a new line. While an IME is composing (compositionstart ..
  // compositionend, event.isComposing, or the legacy keyCode 229 some browsers report for
  // the confirming Enter) Enter belongs to the IME and never sends.
  function isSendKey(event, composing) {
    return event.key === "Enter" && !event.shiftKey && !event.isComposing && !composing
      && event.keyCode !== 229;
  }

  // Image limits of POST /api/chat: at most 5 images, 10 MiB together, PNG/JPEG/GIF/WebP.
  // Returns the catalog key of the refusal, or null.
  function imageError(existing, files) {
    const size = list => list.reduce((total, file) => total + file.size, 0);
    if (existing.length + files.length > MAX_IMAGES
        || size(existing) + size(files) > MAX_IMAGE_BYTES) return "chat.up_to_5_images_10_mib_total";
    if (files.some(file => !IMAGE_TYPES.has(file.type) || !file.size)) return "chat.supported_images_only";
    return null;
  }

  // files -> [{ name, size, url (data: URL for the preview), block (the API's image block) }]
  function readImages(files, { FileReader, t }) {
    return Promise.all(files.map(file => new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onerror = () => reject(new Error(tr(t, "chat.image_read_failed")));
      reader.onabort = reader.onerror;
      reader.onload = () => resolve({ name: file.name, size: file.size, url: reader.result,
        block: { type: "image", source: { type: "base64", media_type: file.type,
          data: reader.result.split(",")[1] } } });
      reader.readAsDataURL(file);
    })));
  }

  function payloadMessage(payload) {
    try {
      const body = JSON.parse(payload);
      return body && typeof body.message === "string" ? body.message : "";
    } catch {
      return "";
    }
  }

  function payloadOf(message, images) {
    return JSON.stringify({ message, ...(images.length ? { images: images.map(image => image.block) } : {}) });
  }

  async function readSSE(response, onEvent, t) {
    if (!response.body || !response.headers.get("content-type")?.includes("text/event-stream"))
      throw new Error(tr(t, "chat.missing_event_stream"));
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    function dispatch(block) {
      let event = "message";
      const data = [];
      for (const line of block.split(/\r?\n/)) {
        if (line.startsWith("event:")) event = line.slice(6).trim();
        if (line.startsWith("data:")) data.push(line.slice(5).replace(/^ /, ""));
      }
      if (data.length) onEvent(event, JSON.parse(data.join("\n")));
    }
    try {
      while (true) {
        const { value, done } = await reader.read();
        buffer += done ? decoder.decode() : decoder.decode(value, { stream: true });
        let boundary;
        while ((boundary = /\r?\n\r?\n/.exec(buffer))) {
          dispatch(buffer.slice(0, boundary.index));
          buffer = buffer.slice(boundary.index + boundary[0].length);
        }
        if (done) break;
      }
    } finally {
      await reader.cancel().catch(() => {});
      reader.releaseLock();
    }
  }

  // One conversation's sender. A message whose outcome is unknown (network error, stream cut
  // or error event, 5xx) is never re-sent automatically: it keeps its Idempotency-Key
  // (`unsettled`) until it is re-sent with the same key (the server refuses a second run,
  // 409 duplicate_request) or discarded; until then no other message may be sent.
  //
  // send() resolves to { ok, completed, delivery, stopped, error, key } (key: the
  // Idempotency-Key this attempt used):
  //   delivery "unknown" | "refused" (nothing ran) | "duplicate" (already received)
  //   stopped  true when stop() closed the stream. That only stops this page's reading: the
  //            server is not asked to cancel and may finish and record the turn, so the
  //            outcome is unknown and the key stays (re-sending the same payload reuses it and
  //            the server deduplicates; only an explicit discard() drops it).
  function createSender({ fetch, t, makeKey = newKey }) {
    let unsettled = null; // { body, key, message, images }
    let controller = null;
    let stopped = false;
    return {
      get unsettled() { return unsettled; },
      get busy() { return controller !== null; },
      // Another payload while one is unsettled would hide it: re-send or discard first.
      blocked(payload) { return Boolean(unsettled && unsettled.body !== payload); },
      discard() { unsettled = null; },
      stop() {
        if (!controller) return false;
        stopped = true;
        if (controller.abort) controller.abort();
        return true;
      },
      async send({ message, images = [], payload = payloadOf(message, images) }, on = {}) {
        // `/new` is never a message for the model, whatever the page did (and with images too):
        // refused here, before a key or a request exists (#350). The pages run it as the
        // new-conversation command (confirmation, then POST /api/chat/session/new).
        if (commandOf(message) || commandOf(payloadMessage(payload))) {
          return { ok: false, completed: false, delivery: "refused", stopped: false, command: "new",
            error: new Error(tr(t, "chat.new_not_a_message")), key: null };
        }
        if (!unsettled || unsettled.body !== payload) {
          unsettled = { body: payload, key: makeKey(), message, images };
        }
        const key = unsettled.key;
        const Abort = root.AbortController;
        controller = Abort ? new Abort() : {};
        stopped = false;
        let completed = false;
        let delivery = "unknown";
        try {
          const response = await fetch("/api/chat", {
            method: "POST",
            headers: { "Content-Type": "application/json", Accept: "text/event-stream", "Idempotency-Key": key },
            body: payload,
            ...(controller.signal ? { signal: controller.signal } : {}),
          });
          if (!response.ok) {
            const error = await response.json().catch(() => ({}));
            if (response.status === 409 && error.code === "duplicate_request") delivery = "duplicate";
            else if (NO_EFFECT_STATUS.has(response.status)
              || (response.status === 409 && error.code === "idempotency_key_reused")) delivery = "refused";
            throw new Error(delivery === "duplicate" ? tr(t, "chat.already_delivered")
              : error.error || tr(t, "chat.request_failed_status", { status: response.status }));
          }
          await readSSE(response, (eventName, data) => {
            if (eventName === "delta") on.delta?.(data);
            else if (eventName === "turn") {
              completed = true;
              on.turn?.(data);
            } else if (eventName === "ui_event") on.uiEvent?.(data);
            else if (eventName === "error") {
              // A turn cut off at the output limit says why (#363); other errors keep their text.
              // #416: cut before any reply or tool call began = thinking used the limit.
              const cut = data.code === "max_tokens";
              const early = cut && data.cut === "before_reply" && Number.isInteger(data.limit);
              const failure = new Error(early ? tr(t, "chat.max_tokens_before_reply", { limit: data.limit.toLocaleString() })
                : cut ? tr(t, "chat.max_tokens_cut_off") : modelFailureText(t, data) || data.error || tr(t, "chat.response_failed"));
              if (data.code) failure.code = data.code;
              if (early) failure.cut = data.cut;
              throw failure;
            }
          }, t);
          if (!completed) throw new Error(tr(t, "chat.response_interrupted"));
          unsettled = null;
          return { ok: true, completed, delivery, stopped: false, error: null, key };
        } catch (error) {
          // Only "nothing ran" or "already received" settles the send; otherwise the key
          // stays until the operator re-sends or discards it.
          // A turn cut off at the output limit ran and ended: its outcome is known (#363).
          // So did a turn whose model call failed (model_failure): the server recorded it.
          if (completed || delivery !== "unknown" || error.code === "max_tokens"
            || error.code === "model_failure") unsettled = null;
          return { ok: false, completed, delivery, stopped: stopped && !completed, error, key };
        } finally {
          controller = null;
        }
      },
    };
  }

  // --- #350: the composer's own command, the new conversation and the context gauge ----------

  // `/new` typed alone is the page's command (a new conversation), never a message for the
  // model. Returns "new" or null. The pages open the confirmation (confirmNewSession) and call
  // POST /api/chat/session/new (createSessionStarter); the sender never sends it.
  function commandOf(message) {
    return /^\/new$/i.test(String(message ?? "").trim()) ? "new" : null;
  }

  // Why "new conversation" cannot run now (a catalog key), or null. Fail closed: a turn running
  // here or anywhere (turnOpen), an approval waiting, a send whose outcome is unknown (it may
  // still be recorded in the old conversation: re-send or discard it first), offline, or a
  // reset already in progress. s: { ready, busy, turnOpen, approval, unsettled, offline, starting }
  function newSessionBlock(s = {}) {
    if (s.starting) return "chat.new_blocked_starting";
    if (!s.ready) return "chat.new_blocked_loading";
    if (s.busy || s.turnOpen) return "chat.new_blocked_turn";
    if (s.approval) return "chat.new_blocked_approval";
    if (s.unsettled) return "chat.new_blocked_unsettled";
    if (s.offline) return "chat.new_blocked_offline";
    return null;
  }

  // The confirmation (the shell's StiraConfirm: Cancel is the default and the initial focus,
  // auto-repeated Enter / Space are ignored). Without the dialog nothing starts (fail closed).
  async function confirmNewSession(confirm, t) {
    if (!confirm || typeof confirm.open !== "function") return false;
    try {
      return (await confirm.open({
        title: tr(t, "chat.new_title"),
        intro: tr(t, "chat.new_intro"),
        items: [tr(t, "chat.new_item_history"), tr(t, "chat.new_item_kept")],
        confirmLabel: tr(t, "chat.new_confirm"),
        cancelLabel: tr(t, "hub.cancel"),
      })) === true;
    } catch {
      return false;
    }
  }

  // The server's refusal codes (chat_api.NEW_SESSION_REFUSALS, the 400s, audit_failed):
  // nothing changed.
  const NEW_SESSION_CODES = new Set(["turn_running", "approval_pending", "resume_pending",
    "approvals_unavailable", "audit_unavailable", "decision_in_progress", "invalid_body",
    "idempotency_key_required", "invalid_idempotency_key", "audit_failed"]);

  // How the server names a reset request in the history (chat_api.request_digest): the
  // first 32 hex digits of SHA-256(key). null when Web Crypto is not available.
  async function requestDigest(key) {
    const subtle = root.crypto && root.crypto.subtle;
    if (!subtle || typeof key !== "string") return null;
    const bytes = new Uint8Array(await subtle.digest("SHA-256", new TextEncoder().encode(key)));
    return Array.from(bytes, (b) => b.toString(16).padStart(2, "0")).join("").slice(0, 32);
  }

  // POST /api/chat/session/new with an Idempotency-Key. start() resolves to
  //   { ok: true, outcome: "started", session }   this request reset the conversation
  //   { ok: true, outcome: "already", session: { id, current } }  this key's reset happened
  //     earlier (the server remembers its keys for the process lifetime); current = it is
  //     still the server's conversation. The page re-reads the history instead of guessing.
  //   { ok: false, outcome: "refused", code, error }   nothing changed (409 / 400 / 403 / 503,
  //     500 audit_failed)
  //   { ok: false, outcome: "unknown", error }   network error, 5xx, or the same key still in
  //     progress: the key is kept (pendingKey). Until it is resolved — the next start() repeats
  //     the same key, or resolve() finds this key's digest in the history — the pages send no
  //     message (it could land in the conversation that is about to be dropped).
  function createSessionStarter({ fetch, t, makeKey = newKey }) {
    let key = null;
    let running = false;
    const settle = () => { key = null; };
    return {
      get pendingKey() { return key; },
      get running() { return running; },
      async start() {
        if (running) return { ok: false, outcome: "refused", code: "starting", error: new Error(tr(t, "chat.new_blocked_starting")) };
        running = true;
        key = key || makeKey();
        try {
          const response = await fetch("/api/chat/session/new", {
            method: "POST",
            headers: { "Content-Type": "application/json", "Idempotency-Key": key },
            body: "{}",
          });
          const data = await response.json().catch(() => ({}));
          if (response.ok) {
            settle();
            return { ok: true, outcome: "started", session: data && data.session ? data.session : null, error: null };
          }
          if (response.status === 409 && data.code === "duplicate_request") {
            if (data.in_progress || !data.session || typeof data.session.id !== "string") {
              return { ok: false, outcome: "unknown", error: new Error(tr(t, "chat.new_in_progress")) };
            }
            settle();
            return { ok: true, outcome: "already", session: { id: data.session.id, current: data.session.current === true }, error: null };
          }
          if (NEW_SESSION_CODES.has(data.code) || NO_EFFECT_STATUS.has(response.status)) {
            settle();
            const known = NEW_SESSION_CODES.has(data.code) ? `chat.new_refused_${data.code}` : null;
            return { ok: false, outcome: "refused", code: data.code || null,
              error: new Error(known ? tr(t, known) : data.error || tr(t, "chat.request_failed_status", { status: response.status })) };
          }
          return { ok: false, outcome: "unknown", error: new Error(data.error || tr(t, "chat.request_failed_status", { status: response.status })) };
        } catch (error) {
          return { ok: false, outcome: "unknown", error };
        } finally {
          running = false;
        }
      },
      // After an unknown outcome: the history names the request that started its session.
      // Only this key's own digest resolves it (another tab's reset does not); the key is
      // then finished. -> { ok: true, outcome: "already", session: { id, current: true } } | null
      async resolve(conversation) {
        const s = conversation && conversation.session;
        if (!key || !s || typeof s.request !== "string") return null;
        const mine = await requestDigest(key);
        if (!mine || mine !== s.request) return null;
        settle();
        return { ok: true, outcome: "already", session: { id: s.id, current: true }, error: null };
      },
    };
  }

  // GET /api/chat/history -> { turns, session } (session: { id, started_at, reason "start" |
  // "new", cleared, request (the digest of the key that started it) }; null from an older server).
  async function fetchConversation(fetch, timeoutMs = 8000) {
    const signal = root.AbortSignal?.timeout ? root.AbortSignal.timeout(timeoutMs) : undefined;
    const response = await fetch("/api/chat/history", { cache: "no-store", ...(signal ? { signal } : {}) });
    if (!response.ok) throw new Error(String(response.status));
    const body = await response.json();
    const s = body && body.session && typeof body.session === "object" ? body.session : null;
    return { turns: Array.isArray(body && body.turns) ? body.turns : [],
      session: s && typeof s.id === "string" ? { id: s.id, reason: s.reason === "new" ? "new" : "start",
        startedAt: typeof s.started_at === "number" ? s.started_at : null,
        cleared: Number.isInteger(s.cleared) && s.cleared >= 0 ? s.cleared : 0,
        request: typeof s.request === "string" ? s.request : null } : null };
  }

  // "New conversation (HH:MM)" for a session started with /new; null for the process start.
  function sessionNote(session, t, locale) {
    if (!session || session.reason !== "new") return null;
    const at = session.startedAt ? new Date(session.startedAt * 1000) : null;
    const time = at && !Number.isNaN(at.getTime())
      ? at.toLocaleTimeString(locale || undefined, { hour: "2-digit", minute: "2-digit" }) : "";
    return time ? tr(t, "chat.new_started_at", { time }) : tr(t, "chat.new_started");
  }

  // Providers whose input count already includes the cached tokens (the same list as
  // stiracore/providers/usage.py INPUT_INCLUDES_CACHE; chat_api passes Strands' raw
  // inputTokens through).
  const INPUT_INCLUDES_CACHE = new Set(["openai", "openai_responses", "gemini", "litellm", "llamacpp"]);
  const count = (v) => (Number.isInteger(v) && v >= 0 ? v : null);

  // The prompt size of a turn (its `turn` event or history record). Preferred: `context_tokens`,
  // the turn's *last* model call (uncached + cache read + cache write, normalized on the
  // server), which is what the context holds. Otherwise (an older record, or the SDK reported
  // no per-call usage) the turn's sum: a turn with tool calls adds every model call, so it reads
  // higher than the context really is. null when the provider reported no input count.
  function promptTokens(turn, provider) {
    const last = count(turn && turn.context_tokens);
    if (last !== null) return last;
    const input = count(turn && turn.input_tokens);
    if (input === null) return null;
    if (INPUT_INCLUDES_CACHE.has(provider)) return input;
    return input + (count(turn.cache_read_tokens) || 0) + (count(turn.cache_write_tokens) || 0);
  }

  // What /api/gateway/status says about the context: the provider, whether the history is
  // summarized automatically (context management), and the window (`context_window`:
  // { tokens, source, compression_threshold }, read-only; the same resolution summarization
  // uses — source "default" means none was configured or known and 200,000 is assumed).
  const WINDOW_SOURCES = new Set(["setting", "model", "capability", "default"]);
  function contextOf(status) {
    const s = status && typeof status === "object" ? status : {};
    const w = s.context_window && typeof s.context_window === "object" ? s.context_window : {};
    const tokens = count(w.tokens);
    const threshold = typeof w.compression_threshold === "number" && w.compression_threshold > 0
      && w.compression_threshold <= 1 ? w.compression_threshold : 0.85;
    const window = tokens && tokens > 0 ? tokens : null;
    return {
      provider: typeof s.provider === "string" ? s.provider : null,
      window,
      source: window && WINDOW_SOURCES.has(w.source) ? w.source : null,
      threshold,
      managed: Boolean(s.context_management && s.context_management.enabled === true),
    };
  }

  // -> null (no count) | { tokens, window, ratio (0.. or null), level "ok" | "warn" (>= 80 %),
  //    near (the automatic summary is close: managed and >= 95 % of the threshold), managed,
  //    threshold, source, lastCall (tokens are the last model call's, not the turn's sum) }
  const WARN_AT = 0.8;
  function contextGauge({ turn, provider = null, window = null, threshold = 0.85, managed = false, source = null } = {}) {
    const tokens = promptTokens(turn, provider);
    if (tokens === null) return null;
    const ratio = window ? tokens / window : null;
    const level = ratio !== null && ratio >= WARN_AT ? "warn" : "ok";
    const near = Boolean(managed && ratio !== null && ratio >= threshold * 0.95);
    const lastCall = count(turn && turn.context_tokens) !== null;
    return { tokens, window, ratio, level, near, managed: Boolean(managed), threshold, source: window ? source : null, lastCall };
  }

  // Short labels: "ctx 38%" (or "ctx 12.3k" without a window) and the full sentence. Near the
  // summary threshold (context management on): "auto-summary soon". At 80 % without context
  // management: "auto-summary off" (never "soon" when nothing summarizes).
  function gaugeText(g, t) {
    const short = (n) => (n >= 1e6 ? `${(n / 1e6).toFixed(1)}M` : n >= 1e3 ? `${(n / 1e3).toFixed(1)}k` : String(n));
    const num = (n) => n.toLocaleString("en-US");
    let text = g.ratio !== null ? tr(t, "chat.ctx_percent", { percent: Math.round(g.ratio * 100) })
      : tr(t, "chat.ctx_tokens", { tokens: short(g.tokens) });
    if (g.near) text = `${text} · ${tr(t, "chat.ctx_near")}`;
    else if (g.level === "warn" && !g.managed) text = `${text} · ${tr(t, "chat.ctx_summary_off_short")}`;
    const lines = [g.ratio !== null
      ? tr(t, g.lastCall ? "chat.ctx_title_window_last" : "chat.ctx_title_window", { tokens: num(g.tokens), window: num(g.window) })
      : tr(t, g.lastCall ? "chat.ctx_title_last" : "chat.ctx_title", { tokens: num(g.tokens) })];
    if (!g.lastCall) lines.push(tr(t, "chat.ctx_title_sum"));
    if (g.source === "default") lines.push(tr(t, "chat.ctx_title_default"));
    if (g.window) lines.push(g.managed ? tr(t, "chat.ctx_title_summary", { percent: Math.round(g.threshold * 100) }) : tr(t, "chat.ctx_title_summary_off"));
    return { text, title: lines.join("\n") };
  }

  // #367: which classifier drives Neural State, from /api/gateway/status `classifier`.
  // `fallback` (rules although Sokudan was asked for) is said in words, with the install
  // hint as the title. An older server without `mode` keeps the name-based labels.
  function classifierText(info, t) {
    const name = info && typeof info.name === "string" ? info.name : "unresolved";
    const mode = info && info.mode;
    if ((info && info.is_jev) || mode === "sokudan") {
      const model = info.model ? ` (${info.model})` : "";
      return { text: tr(t, "chat.sokudan_model", { model }), title: "", state: "jev", fallback: false };
    }
    if (mode === "rule_only" || name === "RuleBasedClassifier") {
      const fallback = info.fallback === true;
      return fallback
        ? { text: tr(t, "chat.rule_only_fallback"), title: tr(t, "chat.rule_only_fallback_title"), state: "rule", fallback }
        : { text: tr(t, "chat.rule_fallback"), title: "", state: "rule", fallback };
    }
    return { text: tr(t, "chat.unresolved"), title: "", state: "unresolved", fallback: false };
  }

  // #367: "changed in n of m turns · last: <reason>" from `emotion_updates`. Reasons are
  // the server's fixed codes; anything else reads "unknown" and is never echoed.
  const NO_UPDATE_REASONS = Object.freeze(["no_stimulus", "classifier_error", "unknown_stimulus",
    "no_change", "turn_incomplete"]);
  const reasonKey = (code) => `chat.no_update_${NO_UPDATE_REASONS.includes(code) ? code : "unknown"}`;
  function emotionUpdatesText(updates, t) {
    const turns = updates ? count(updates.turns) : null;
    if (turns === null) return { text: "—", title: "", reason: null };
    if (turns === 0) return { text: tr(t, "chat.emotion_updates_none"), title: "", reason: null };
    const updated = count(updates.updated) ?? 0;
    const last = updates.last;
    const reason = last && last.updated === false
      ? (NO_UPDATE_REASONS.includes(last.reason) ? last.reason : "unknown") : null;
    const lastText = !last ? null : reason === null ? tr(t, "chat.emotion_last_updated")
      : tr(t, "chat.emotion_last_reason", { reason: tr(t, reasonKey(reason)) });
    const text = [tr(t, "chat.emotion_updates_count", { updated, turns }), lastText].filter(Boolean).join(" · ");
    const reasons = updates.reasons || {};
    const breakdown = NO_UPDATE_REASONS.map((code) => `${tr(t, reasonKey(code))} ${count(reasons[code]) ?? 0}`).join(" · ");
    return { text, title: tr(t, "chat.emotion_updates_title", { breakdown }), reason };
  }

  // POST /api/chat/cancel (#452): the explicit stop of the running turn. Closing a page or
  // sender.stop() never stops it (the server runs the turn on its own worker). The server
  // stops at the SDK's next safe point and records the turn as "cancelled"; the stream of the
  // page that sent it then ends with its `turn` event (`stop: "cancelled"`).
  // -> { ok, code, error } (code "no_turn": nothing was running, e.g. it had just finished).
  async function cancelTurn(fetch, turnId = null, t = null) {
    try {
      const response = await fetch("/api/chat/cancel", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(turnId ? { turn_id: turnId } : {}),
      });
      const body = await response.json().catch(() => ({}));
      if (response.ok) return { ok: true, code: null, error: null };
      return { ok: false, code: body.code || null,
        error: new Error(body.error || tr(t, "chat.request_failed_status", { status: response.status })) };
    } catch (error) {
      return { ok: false, code: null, error };
    }
  }

  // #445: the model call that is running, from the turn's model.* events (ids and numbers).
  // prev / result: { callId, since } (since: ms on this page's clock) or null.
  const LONG_GENERATION_SEC = 60;
  function generationOf(prev, event, nowMs) {
    if (!event || typeof event !== "object") return prev;
    const d = event.data && typeof event.data === "object" ? event.data : {};
    const id = typeof d.call_id === "string" && /^[0-9a-f]{32}$/.test(d.call_id) ? d.call_id : null;
    switch (event.type) {
      case "model.started":
        // a progress event (sent at once) may have been read before the queued start
        return !id ? prev : prev && prev.callId === id ? prev : { callId: id, since: nowMs };
      case "model.progress":
        return id && Number.isFinite(d.elapsed_ms) && d.elapsed_ms >= 0
          ? { callId: id, since: nowMs - d.elapsed_ms } : prev;
      case "model.completed":
        return prev && (!id || prev.callId === id) ? null : prev;
      case "turn.completed":
        return null;
      default:
        return prev;
    }
  }

  // "生成中 12 秒", and from LONG_GENERATION_SEC on the note to consider limits / splitting.
  function generationText(gen, nowMs, t) {
    if (!gen || !Number.isFinite(gen.since)) return "";
    const s = Math.max(0, Math.floor((nowMs - gen.since) / 1000));
    const text = tr(t, "chat.generating", { s });
    return s >= LONG_GENERATION_SEC ? `${text} — ${tr(t, "chat.generating_long")}` : text;
  }

  // #555: a turn this page did not send (another tab, a reload) read from its turn events
  // (/api/turns/<id>/events?after_seq=): the last seq, when it started and the open model
  // call. Times are the server's ``ts`` (seconds; the dashboard runs on this machine).
  function turnProgressOf(prev, turnId, events) {
    const base = prev && prev.turnId === turnId ? prev : { turnId, seq: 0, startedMs: null, generation: null };
    let { seq, startedMs, generation } = base;
    for (const event of Array.isArray(events) ? events : []) {
      if (!event || typeof event !== "object" || event.turn_id !== turnId) continue;
      if (Number.isInteger(event.seq) && event.seq > seq) seq = event.seq;
      const ts = Number.isFinite(event.ts) && event.ts > 0 ? event.ts * 1000 : null;
      if (ts === null) continue;
      if (event.type === "turn.started") startedMs = ts;
      else if (startedMs === null) startedMs = ts; // read mid-turn: the first event seen
      generation = generationOf(generation, event, ts);
    }
    return { turnId, seq, startedMs, generation };
  }

  // "…続いています · 生成中 12 秒" while a model call is open, "… · 開始から 40 秒" between.
  function elsewhereText(progress, nowMs, t) {
    const base = tr(t, "chat.turn_running_elsewhere");
    if (!progress) return base;
    let detail = generationText(progress.generation, nowMs, t);
    if (!detail && Number.isFinite(progress.startedMs)) {
      detail = tr(t, "chat.turn_elapsed", { s: Math.max(0, Math.floor((nowMs - progress.startedMs) / 1000)) });
    }
    return detail ? `${base} · ${detail}` : base;
  }

  // Backwards-compatible: the turns only.
  async function fetchHistory(fetch, timeoutMs = 8000) {
    return (await fetchConversation(fetch, timeoutMs)).turns;
  }

  const api = { MAX_IMAGE_BYTES, MAX_IMAGES, IMAGE_TYPES, NO_EFFECT_STATUS, newKey, isSendKey,
    imageError, readImages, payloadOf, readSSE, createSender, cancelTurn, fetchHistory, fetchConversation,
    commandOf, newSessionBlock, confirmNewSession, createSessionStarter, requestDigest, sessionNote,
    INPUT_INCLUDES_CACHE, promptTokens, contextOf, contextGauge, gaugeText,
    classifierText, NO_UPDATE_REASONS, emotionUpdatesText,
    LONG_GENERATION_SEC, generationOf, generationText, turnProgressOf, elsewhereText };
  root.StiraChatCore = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
