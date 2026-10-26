/* In-chat tool cards (#188).
 * StiraCards.card()/update() build a generic collapsible card (<details>) with a one-line
 * summary and a text status badge; approval cards (#189) reuse the same parts.
 * StiraCards.create().renderTurn(rec, reply) renders one card per tool call of a turn
 * (records come from turn_ribbon.js) and folds them into "N calls · M failed" when done.
 */
(function (root) {
  "use strict";
  const STATUS = {
    pending: ["turns.tool_status_pending", "warn"],
    running: ["turns.tool_status_running", "info"],
    done: ["turns.tool_status_done", "ok"],
    error: ["turns.tool_status_error", "crit"],
    denied: ["turns.tool_status_denied", "crit"],
    timeout: ["turns.tool_status_timeout", "warn"],
    unknown: ["turns.tool_status_unknown", "neutral"],
  };
  const FAILED = new Set(["error", "timeout"]);
  const finite = value => typeof value === "number" && Number.isFinite(value);

  function make(doc, tag, className, content) {
    const node = doc.createElement(tag);
    if (className) node.className = className;
    if (content !== undefined && content !== null) node.textContent = content;
    return node;
  }

  function statusOf(status) {
    return Object.hasOwn(STATUS, status) ? status : "unknown";
  }

  /** Generic card: { kind, title, brief, status, meta } -> <details class="stira-card">. */
  function card(doc, spec) {
    const el = make(doc, "details", "stira-card");
    const summary = make(doc, "summary", "stira-card-summary");
    const body = make(doc, "div", "stira-card-body");
    el.append(summary, body);
    el.parts = { summary, body };
    update(doc, el, spec);
    return el;
  }

  function update(doc, el, spec) {
    const status = statusOf(spec.status);
    const [key, state] = STATUS[status];
    el.dataset.kind = spec.kind || "tool";
    el.dataset.status = status;
    const badge = make(doc, "span", "badge", t(key));
    badge.dataset.state = state;
    const title = make(doc, "code", "stira-card-title", spec.title || "?");
    const parts = [badge, title];
    if (spec.brief) parts.push(make(doc, "span", "stira-card-brief", spec.brief));
    if (spec.meta) parts.push(make(doc, "span", "stira-card-meta", spec.meta));
    el.parts.summary.replaceChildren(...parts);
    return el;
  }

  function oneLine(text, limit = 80) {
    if (typeof text !== "string") return null;
    const line = text.replace(/\s+/g, " ").trim();
    return line.length > limit ? `${line.slice(0, limit - 1)}…` : line;
  }

  function create(options = {}) {
    const doc = options.document || root.document;
    const fetchJSON = options.fetchJSON || (async url => {
      const response = await fetch(url, { cache: "no-store" });
      if (!response.ok) throw new Error(String(response.status));
      return response.json();
    });
    const el = (tag, className, content) => make(doc, tag, className, content);
    const missing = () => t("turns.not_recorded");

    function effectiveStatus(rec, tool) {
      const approval = rec.approvals.find(item => item.tool_use_id && item.tool_use_id === tool.tool_use_id);
      if (approval && !approval.decision && tool.status !== "done" && tool.status !== "error") return "pending";
      if (approval?.decision === "deny" && tool.status !== "done") return "denied";
      const status = statusOf(tool.status);
      // A finished turn with a call still "running" has an unknown outcome.
      if (rec.completed && (status === "running" || status === "pending")) return "unknown";
      return status;
    }

    function payloadBlock(rec, tool, kind) {
      const box = el("section", "tool-payload");
      box.dataset.kind = kind;
      // #601 E11: what this block was drawn from; the same payload keeps its block (and the
      // pages already loaded into it) when the card is rebuilt for a new status or output.
      box.payloadSig = JSON.stringify([tool[`${kind}_preview`], tool[`${kind}_truncated`], tool[`${kind}_stored`],
        tool[`${kind}_lines`], tool[`${kind}_chars`], rec.id, tool.tool_use_id]);
      box.append(el("h4", null, t(kind === "input" ? "cards.input" : "cards.output")));
      const preview = tool[`${kind}_preview`];
      if (typeof preview !== "string") {
        const empty = el("p", "tool-payload-note", missing());
        empty.dataset.trend = "missing";
        box.append(empty);
        return box;
      }
      const pre = el("pre", "tool-payload-text", preview);
      pre.setAttribute("tabindex", "0");
      pre.setAttribute("aria-label", t(kind === "input" ? "cards.input" : "cards.output"));
      box.append(pre);
      const lines = tool[`${kind}_lines`], chars = tool[`${kind}_chars`];
      if (tool[`${kind}_truncated`]) {
        const note = el("p", "tool-payload-note", t("cards.truncated", {
          lines: finite(lines) ? lines.toLocaleString() : "?",
          chars: finite(chars) ? chars.toLocaleString() : "?",
        }));
        box.append(note);
        if (tool[`${kind}_stored`] && rec.id && tool.tool_use_id) {
          const more = el("button", "btn btn-secondary tool-payload-more", t("cards.load_more"));
          more.type = "button";
          let offset = 0, first = true;
          more.addEventListener("click", async () => {
            more.disabled = true;
            try {
              const page = await fetchJSON(`/api/turns/${encodeURIComponent(rec.id)}/tools/${encodeURIComponent(tool.tool_use_id)}/${kind}?offset=${offset}`);
              const text = typeof page?.text === "string" ? page.text : "";
              pre.textContent = first ? text : `${pre.textContent}\n${text}`;
              first = false;
              const shown = pre.textContent ? pre.textContent.split("\n").length : 0;
              const parts = [t("cards.shown_lines", { shown: shown.toLocaleString(), lines: finite(page?.total_lines) ? page.total_lines.toLocaleString() : "?" })];
              if (page?.line_cut) parts.push(t("cards.line_cut"));
              if (page?.next_offset === null || page?.next_offset === undefined) {
                parts.push(t(page?.retained_truncated ? "cards.retained_truncated" : "cards.all_loaded"));
                more.remove();
              } else {
                offset = page.next_offset;
                more.textContent = t("cards.load_next");
                more.disabled = false;
              }
              note.textContent = parts.join(" ");
            } catch {
              note.textContent = t("cards.load_failed");
              more.disabled = false;
            }
          });
          box.append(more);
        }
      }
      return box;
    }

    function toolCard(rec, tool) {
      const status = effectiveStatus(rec, tool);
      const duration = finite(tool.duration_ms) ? `${Math.round(tool.duration_ms).toLocaleString()} ms` : null;
      const spec = { kind: "tool", title: tool.name || "?", status,
        brief: oneLine(tool.input_preview), meta: duration };
      const box = card(doc, spec);
      box.dataset.toolUseId = tool.tool_use_id || "";
      const dl = el("dl", "facts");
      const exit = [tool.result_status, tool.result_code, tool.exception_type].filter(Boolean).join(" · ") || null;
      for (const [label, value] of [
        [t("cards.status"), t(STATUS[status][0])],
        [t("cards.duration"), duration],
        [t("cards.exit"), exit],
        [t("cards.tool_use_id"), tool.tool_use_id || null],
      ]) {
        const row = el("div");
        const dd = el("dd", null, value ?? missing());
        if (value === null) dd.dataset.trend = "missing";
        row.append(el("dt", null, label), dd);
        dl.append(row);
      }
      const payloads = { input: payloadBlock(rec, tool, "input"), output: payloadBlock(rec, tool, "output") };
      box.parts.body.append(dl, payloads.input, payloads.output);
      box.parts.payloads = payloads;
      return box;
    }

    function signature(rec, tool) {
      return JSON.stringify([effectiveStatus(rec, tool), tool.duration_ms, tool.input_preview,
        tool.output_preview, tool.result_status, tool.result_code, tool.exception_type]);
    }

    function renderTurn(rec, reply) {
      if (!reply?.article || !rec?.tools) return null;
      if (!rec.tools.size) return null;
      let group = reply.toolGroup;
      if (!group) {
        group = el("details", "tool-group");
        group.open = !rec.completed;
        const summary = el("summary", "tool-group-summary");
        const list = el("div", "tool-group-body");
        group.append(summary, list);
        group.parts = { summary, list, cards: new Map() };
        if (reply.body && reply.article.insertBefore) reply.article.insertBefore(group, reply.body);
        else reply.article.append(group);
        reply.toolGroup = group;
      }
      const { summary, list, cards } = group.parts;
      let failed = 0, running = 0, denied = 0;
      for (const [key, tool] of rec.tools) {
        const status = effectiveStatus(rec, tool);
        if (FAILED.has(status)) failed++;
        if (status === "running" || status === "pending") running++;
        if (status === "denied") denied++;
        const known = cards.get(key);
        const sig = signature(rec, tool);
        if (known && known.sig === sig) continue;
        const fresh = toolCard(rec, tool);
        fresh.sig = sig;
        if (known) {
          fresh.open = known.open;
          // #601 E11: an unchanged payload keeps the block the person may have paged into.
          for (const kind of ["input", "output"]) {
            const before = known.parts.payloads?.[kind], next = fresh.parts.payloads[kind];
            if (before && before.payloadSig === next.payloadSig) {
              fresh.parts.body.insertBefore(before, next);
              next.remove();
              fresh.parts.payloads[kind] = before;
            }
          }
          list.insertBefore(fresh, known);
          known.remove();
        }
        else list.append(fresh);
        cards.set(key, fresh);
      }
      const count = rec.tools.size;
      summary.textContent = rec.completed
        ? t(denied ? "cards.group_done_denied" : "cards.group_done", { count, failed, denied })
        : t("cards.group_running", { count, running });
      group.dataset.failed = String(failed);
      if (rec.completed && !group.folded) {
        group.open = false; // fold to "N calls · M failed" when the turn ends
        group.folded = true;
      }
      return group;
    }

    return { renderTurn, effectiveStatus };
  }

  root.StiraCards = { card, update, create, statusOf, STATUS };
})(typeof window !== "undefined" ? window : globalThis);
