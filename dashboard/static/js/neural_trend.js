/* Collapsible Neural State trend on /chat (#303).
 * Loaded before chat.js. chat.js calls StiraTrend.create() once and then
 *   update(state, level)  - live gateway values (neural_state, breaker level)
 * Collapsed by default; the open/closed choice is kept in localStorage.
 * The one-line summary shows the current stability and, for non-normal
 * breaker levels, a text badge (never colour alone). Display only: the level
 * comes from the engine as-is and nothing here changes breaker behaviour.
 * The panel never opens on its own (a WARNING/CRITICAL badge is the signal).
 */
(function (root) {
  "use strict";
  const STORAGE_KEY = "stira.chat.neuralTrend";
  // Glyphs are shapes (not colours); the visible label carries the meaning.
  const BADGES = {
    warning: ["▲", "shell.warning"],
    critical: ["■", "shell.critical"],
    coherence: ["◆", "shell.coherence"],
  };
  const finite = value => typeof value === "number" && Number.isFinite(value);

  function create(options = {}) {
    const doc = options.document || root.document;
    const storage = Object.hasOwn(options, "storage") ? options.storage : safeStorage();
    const translate = options.t || root.t || (key => key);
    const $ = id => doc.getElementById(id);
    const section = $("neural-trend");
    const toggle = $("neural-toggle");
    const body = $("neural-body");
    const value = $("neural-summary-stability");
    const badge = $("neural-summary-badge");
    if (!section || !toggle || !body) return null;

    function read() {
      try { return storage && storage.getItem(STORAGE_KEY) === "expanded"; } catch (e) { return false; }
    }
    function write(open) {
      try { if (storage) storage.setItem(STORAGE_KEY, open ? "expanded" : "collapsed"); } catch (e) { /* */ }
    }
    function apply(open) {
      toggle.setAttribute("aria-expanded", String(open));
      body.hidden = !open;
      section.dataset.expanded = String(open);
    }
    const isOpen = () => toggle.getAttribute("aria-expanded") === "true";

    apply(read());
    toggle.addEventListener("click", () => {
      const open = !isOpen();
      apply(open);
      write(open);
    });

    function update(state, level) {
      const raw = state && typeof state === "object" ? state.stability : undefined;
      if (value) value.textContent = finite(raw) ? `${Math.round(raw * 100)}%` : "—";
      if (!badge) return;
      const key = typeof level === "string" ? level.toLowerCase() : "";
      const entry = Object.hasOwn(BADGES, key) ? BADGES[key] : null;
      badge.hidden = !entry;
      badge.dataset.level = entry ? key : "";
      if (!entry) { badge.replaceChildren(); return; }
      const glyph = doc.createElement("span");
      glyph.setAttribute("aria-hidden", "true");
      glyph.textContent = entry[0];
      const label = doc.createElement("span");
      label.textContent = translate(entry[1]);
      badge.replaceChildren(glyph, label);
    }

    return { update, isOpen, setOpen: open => { apply(Boolean(open)); write(Boolean(open)); } };
  }

  function safeStorage() {
    try { return root.localStorage || null; } catch (e) { return null; }
  }

  root.StiraTrend = { create, STORAGE_KEY };
})(typeof window !== "undefined" ? window : globalThis);
