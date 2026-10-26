/* Settings page: the taint observation (ADR 0022 phase 0, #458). Read only.
 *
 * GET /api/taint → { enabled, forced_off, observation: { last_turn, totals } | null }.
 * Phase 0 enforces nothing: this card only shows what the observer counted.
 *
 * StiraTaint.render(doc, data) is pure (tests); StiraTaint.load(doc, getJSON).
 */
(function (root) {
  "use strict";
  const tr = (key, vars) => (typeof t === "function" ? t(key, vars) : key);
  const ROUTE = "/api/taint";
  const n = (value) => (Number.isFinite(Number(value)) ? Number(value) : 0);

  function lines(data) {
    if (!data || typeof data !== "object" || data.error) return [tr("taint.unavailable")];
    if (!data.enabled) {
      return [data.forced_off ? tr("taint.state_forced_off") : tr("taint.state_off")];
    }
    const out = [tr("taint.state_on")];
    const seen = data.observation || {};
    const totals = seen.totals || {};
    out.push(
      tr("taint.totals", {
        turns: n(totals.turns),
        untrusted: n(totals["trust.untrusted"]),
        sensitive: n(totals.sensitive),
        recall: n(totals.untrusted_recall_only),
      }),
    );
    const held = Object.keys(totals)
      .filter((key) => key.startsWith("flow.") && n(totals[key]) > 0)
      .map((key) => tr("taint.count", { code: key.slice(5), n: n(totals[key]) }));
    out.push(held.length ? tr("taint.would_hold", { list: held.join(", ") }) : tr("taint.none"));
    return out;
  }

  function render(doc, data) {
    const box = doc.getElementById("taint-view");
    if (!box) return;
    box.textContent = "";
    for (const text of lines(data)) {
      const p = doc.createElement("p");
      p.className = "hint";
      p.textContent = text;
      box.appendChild(p);
    }
  }

  async function getJSON(url) {
    const response = await root.fetch(url, { headers: { Accept: "application/json" } });
    return response.json();
  }

  async function load(doc, get) {
    let data = null;
    try {
      data = await (get || getJSON)(ROUTE);
    } catch (_error) {
      data = null;
    }
    render(doc, data);
  }

  function start() {
    const doc = root.document;
    if (!doc || !doc.getElementById("taint-view")) return;
    load(doc);
  }

  root.StiraTaint = { render, lines, load };
  if (root.document && root.document.readyState === "loading") {
    root.document.addEventListener("DOMContentLoaded", start);
  } else if (root.document) {
    start();
  }
})(typeof window !== "undefined" ? window : globalThis);
