/* Procedural-memory proposals on /memory (#463): list, body, diff, source turn, approve /
 * reject. The agent can only propose (propose_procedure → procedural/staging/); a person
 * decides here. Approving widens what the agent is told (a rule in every system prompt), so
 * it goes through the shared confirmation dialog with a tick box (Cancel is the default) and
 * is bound to the SHA-256 of the text shown. Rejecting only narrows: one dialog, no tick.
 * Bodies, rationales and diffs are rendered with textContent only (never as HTML).
 */
(function () {
  "use strict";
  const tr = (key, vars) => (typeof t === "function" ? t(key, vars) : key);
  const $ = (id) => document.getElementById(id);
  const list = $("proposals-list");
  const status = $("proposals-status");
  const count = $("proposals-count");
  const note = $("proposals-effective");
  const history = $("proposals-history");
  const breakerNote = $("proposals-breaker");
  if (!list) return;
  let busy = false;
  let lastKey = null;
  // Approving needs the breaker at NORMAL (the server decides; this only explains it).
  let approveAllowed = true;
  const REFUSAL_KEYS = {
    E_BREAKER_NOT_NORMAL: "procreview.refused_breaker",
    E_BREAKER_UNAVAILABLE: "procreview.refused_breaker_unavailable",
  };

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function short(sha) {
    return typeof sha === "string" ? sha.slice(0, 12) : "—";
  }

  function when(value) {
    if (!value) return "—";
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString();
  }

  function meta(items) {
    const dl = el("dl", "proposal-meta");
    for (const [label, value] of items) {
      dl.append(el("dt", "", label));
      const dd = el("dd");
      if (value instanceof Node) dd.append(value);
      else dd.textContent = value === null || value === undefined || value === "" ? "—" : String(value);
      dl.append(dd);
    }
    return dl;
  }

  function turnLink(turn) {
    if (typeof turn !== "string" || !turn) return "—";
    // A chat turn id links to its record; an operator's own value (a seed) is plain text.
    if (!/^[0-9a-f]{32}$/.test(turn)) return turn;
    const link = el("a", "", turn);
    link.href = `/api/turns/${encodeURIComponent(turn)}/events`;
    link.setAttribute("aria-label", tr("procreview.turn_link_label", { turn }));
    return link;
  }

  function diffBlock(lines) {
    const pre = el("pre", "proposal-diff");
    for (const line of lines) {
      const kind = { "+": "add", "-": "del", "@": "hunk" }[line.op] || "same";
      const prefix = { add: "+ ", del: "- ", hunk: "", same: "  " }[kind];
      const row = el("span", `diff-${kind}`, prefix + line.text);
      pre.append(row, "\n");
    }
    return pre;
  }

  async function post(url, body, doneKey, vars) {
    busy = true;
    try {
      const response = await window.stiraFetch(url, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      let data = {};
      try { data = await response.json(); } catch (error) { data = {}; }
      if (!response.ok) {
        const code = data.code || `HTTP ${response.status}`;
        const known = Object.prototype.hasOwnProperty.call(REFUSAL_KEYS, code);
        status.textContent = known ? tr(REFUSAL_KEYS[code], { code }) : tr("procreview.refused", { code });
        return false;
      }
      status.textContent = tr(doneKey, vars);
      return true;
    } catch (error) {
      status.textContent = tr("procreview.request_failed");
      return false;
    } finally {
      busy = false;
      lastKey = null;
      void load();
    }
  }

  async function approve(proposal) {
    if (busy || typeof StiraConfirm === "undefined") return;
    const items = [
      `${tr("procreview.key")}: ${proposal.key}`,
      `${tr("procreview.sha")}: ${short(proposal.sha256)}`,
      `${tr("procreview.source_turn")}: ${proposal.source_turn || "—"}`,
    ];
    if (proposal.revision) items.push(tr("procreview.replaces"));
    items.push(tr(effectiveKey === "next_turn" ? "procreview.effect_next_turn" : "procreview.effect_next_start"));
    await StiraConfirm.open({
      title: tr("procreview.approve_title", { key: proposal.key }),
      intro: tr("procreview.approve_intro"),
      items,
      checks: [tr("procreview.approve_check")],
      confirmLabel: tr("procreview.approve"),
      cancelLabel: tr("hub.cancel"),
      tone: "primary",
      onConfirm: () => post(
        `/api/memory/procedural/proposals/${encodeURIComponent(proposal.key)}/approve`,
        { sha256: proposal.sha256, confirm: true, replace: proposal.revision === true },
        "procreview.approved_done", { key: proposal.key }),
    });
  }

  async function reject(proposal, reasonField) {
    if (busy || typeof StiraConfirm === "undefined") return;
    const reason = reasonField.value.trim();
    await StiraConfirm.open({
      title: tr("procreview.reject_title", { key: proposal.key }),
      intro: tr("procreview.reject_intro"),
      items: [
        `${tr("procreview.key")}: ${proposal.key}`,
        `${tr("procreview.reason")}: ${reason || tr("procreview.no_reason")}`,
      ],
      confirmLabel: tr("procreview.reject"),
      cancelLabel: tr("hub.cancel"),
      tone: "primary",
      onConfirm: () => post(
        `/api/memory/procedural/proposals/${encodeURIComponent(proposal.key)}/reject`,
        { sha256: proposal.sha256, confirm: true, reason },
        "procreview.rejected_done", { key: proposal.key }),
    });
  }

  function card(proposal, index) {
    const article = el("article", "proposal");
    article.dataset.key = proposal.key;
    const titleId = `proposal-${index}-title`;
    article.setAttribute("aria-labelledby", titleId);
    const head = el("div", "proposal-head");
    const title = el("h3", "", proposal.key);
    title.id = titleId;
    head.append(title, el("span", "badge", tr(proposal.revision ? "procreview.kind_revision" : "procreview.kind_new")));
    if (proposal.proposed_via === "propose_procedure") head.append(el("span", "badge", tr("procreview.by_agent")));
    article.append(head);
    article.append(meta([
      [tr("procreview.proposed_at"), when(proposal.proposed_at)],
      [tr("procreview.source_turn"), turnLink(proposal.source_turn)],
      [tr("procreview.sha"), short(proposal.sha256)],
      [tr("procreview.length"), tr("procreview.chars", { n: proposal.length })],
    ]));
    article.append(el("h4", "", tr("procreview.rationale")));
    article.append(el("p", "proposal-rationale", proposal.rationale || tr("procreview.no_rationale")));
    article.append(el("h4", "", tr("procreview.body")));
    article.append(el("pre", "proposal-body", proposal.body));
    if (proposal.existing) {
      article.append(el("h4", "", tr("procreview.diff")));
      article.append(diffBlock(proposal.diff || []));
    }
    if (proposal.problem) {
      article.append(el("p", "proposal-problem", tr(`procreview.problem_${proposal.problem}`)));
    }
    const actions = el("div", "proposal-actions");
    const reasonId = `proposal-${index}-reason`;
    const label = el("label", "", tr("procreview.reason_label"));
    label.setAttribute("for", reasonId);
    const reason = el("textarea", "input");
    reason.id = reasonId;
    reason.rows = 2;
    reason.maxLength = 500;
    const buttons = el("div", "proposal-buttons");
    const ok = el("button", "btn btn-primary", tr("procreview.approve"));
    ok.type = "button";
    ok.setAttribute("aria-label", tr("procreview.approve_label", { key: proposal.key }));
    ok.disabled = proposal.approvable === false || !approveAllowed;
    ok.addEventListener("click", () => { void approve(proposal); });
    const no = el("button", "btn btn-secondary", tr("procreview.reject"));
    no.type = "button";
    no.setAttribute("aria-label", tr("procreview.reject_label", { key: proposal.key }));
    no.addEventListener("click", () => { void reject(proposal, reason); });
    buttons.append(ok, no);
    actions.append(label, reason, buttons);
    article.append(actions);
    return article;
  }

  function renderHistory(reviews) {
    history.replaceChildren();
    if (!reviews.length) {
      history.append(el("li", "empty", tr("procreview.no_history")));
      return;
    }
    for (const review of reviews) {
      const action = review.action === "approved" ? tr("procreview.approved") : tr("procreview.rejected");
      const line = `${when(review.at)} — ${review.key} — ${action} (${review.by || "—"})`;
      const item = el("li", "", line);
      if (review.action === "rejected") {
        item.append(el("span", "review-reason", ` ${tr("procreview.reason")}: ${review.reason || tr("procreview.no_reason")}`));
      }
      history.append(item);
    }
  }

  let effectiveKey = "next_start";

  async function load() {
    if (busy) return null;
    try {
      const response = await fetch("/api/memory/procedural/review", {
        headers: { Accept: "application/json" }, cache: "no-store",
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const data = await response.json();
      const key = JSON.stringify(data);
      if (key === lastKey) return false;
      lastKey = key;
      effectiveKey = data.effective === "next_turn" ? "next_turn" : "next_start";
      const breaker = data.breaker && typeof data.breaker === "object" ? data.breaker : {};
      approveAllowed = breaker.approve_allowed === true;
      if (breakerNote) {
        breakerNote.hidden = approveAllowed;
        breakerNote.textContent = approveAllowed ? "" : (typeof breaker.level === "string"
          ? tr("procreview.breaker_notice", { level: breaker.level.toUpperCase() })
          : tr("procreview.breaker_unknown"));
      }
      note.textContent = tr(effectiveKey === "next_turn" ? "procreview.effective_next_turn" : "procreview.effective_next_start",
        { n: data.prompt_limit });
      const proposals = Array.isArray(data.proposals) ? data.proposals : [];
      count.textContent = `(${proposals.length})`;
      // A new proposal arriving must not wipe a reason being typed for another one.
      const typed = new Map();
      list.querySelectorAll("article.proposal").forEach((article) => {
        const field = article.querySelector("textarea");
        if (field && field.value) typed.set(article.dataset.key, field.value);
      });
      list.replaceChildren();
      if (!proposals.length) list.append(el("p", "empty", tr("procreview.empty")));
      proposals.forEach((proposal, index) => {
        const article = card(proposal, index);
        if (typed.has(proposal.key)) article.querySelector("textarea").value = typed.get(proposal.key);
        list.append(article);
      });
      renderHistory(Array.isArray(data.reviews) ? data.reviews : []);
      return true;
    } catch (error) {
      status.textContent = tr("procreview.load_failed");
      return null;
    }
  }

  const refresh = $("refresh");
  if (refresh) refresh.addEventListener("click", () => { lastKey = null; void load(); });
  if (typeof StiraPoll !== "undefined") {
    StiraPoll.keep(load, 15000, 60000, undefined, document);
  } else {
    void load();
  }
})();
