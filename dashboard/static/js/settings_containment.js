/* Settings page: the containment observation (ADR 0023 phase 0, #460-0). Read only.
 *
 * GET /api/containment → { enabled, forced_off, window_seconds, window: { score, would_be,
 * peak_would_be, signals: {G1..T} } | null }. Phase 0 enforces nothing: this card only
 * shows what the observer counted. There is no control here (a person's release / raise
 * are phase 1).
 *
 * StiraContainment.render(doc, data) is pure (tests); StiraContainment.load(doc, getJSON).
 */
(function (root) {
  "use strict";
  const tr = (key, vars) => (typeof t === "function" ? t(key, vars) : key);
  const ROUTE = "/api/containment";

  // Literal t() calls so `i18n_extract.py --check` sees every key.
  const STAGE_TEXT = {
    open: () => tr("contain.stage_open"),
    restricted: () => tr("contain.stage_restricted"),
    closed: () => tr("contain.stage_closed"),
    quarantine: () => tr("contain.stage_quarantine"),
    terminated: () => tr("contain.stage_terminated"),
  };
  const SIGNAL_TEXT = {
    G1: () => tr("contain.sig_g1"),
    G2: () => tr("contain.sig_g2"),
    G3: () => tr("contain.sig_g3"),
    G4: () => tr("contain.sig_g4"),
    G5: () => tr("contain.sig_g5"),
    G6: () => tr("contain.sig_g6"),
    T: () => tr("contain.sig_t"),
  };

  function stage(value) {
    const word = STAGE_TEXT[value];
    return word ? word() : tr("contain.stage_unknown");
  }

  function lines(data) {
    if (!data || typeof data !== "object" || data.error) return [tr("contain.unavailable")];
    if (!data.enabled) {
      return [data.forced_off ? tr("contain.state_forced_off") : tr("contain.state_off")];
    }
    const out = [tr("contain.state_on", { minutes: Math.round((data.window_seconds || 600) / 60) })];
    const window = data.window || {};
    out.push(tr("contain.now", { stage: stage(window.would_be), score: Number(window.score || 0) }));
    out.push(tr("contain.peak", { stage: stage(window.peak_would_be) }));
    const signals = window.signals || {};
    const counted = Object.keys(SIGNAL_TEXT)
      .filter((name) => Number(signals[name] || 0) > 0)
      .map((name) => tr("contain.count", { signal: SIGNAL_TEXT[name](), n: Number(signals[name]) }));
    out.push(counted.length ? counted.join(tr("contain.sep")) : tr("contain.none"));
    return out;
  }

  function render(doc, data) {
    const box = doc.getElementById("containment-view");
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
    if (!doc || !doc.getElementById("containment-view")) return;
    load(doc);
  }

  root.StiraContainment = { render, lines, load };
  if (root.document && root.document.readyState === "loading") {
    root.document.addEventListener("DOMContentLoaded", start);
  } else if (root.document) {
    start();
  }
})(typeof window !== "undefined" ? window : globalThis);
