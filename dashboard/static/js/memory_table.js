// Memory table on /memory (#652): one row per entry with its key, importance and
// retention, read from GET /api/memory/entries. The API sends no body and this script
// draws only those fields, as text. Values outside the known vocabulary are drawn as "—".
(function () {
  "use strict";
  const $ = id => document.getElementById(id);
  const LAYERS = ["episodic", "semantic", "procedural"];
  const NONE = "—";
  const table = $("memtable");
  const body = $("memtable-body");
  const status = $("memtable-status");
  if (!table || !body || !status) return;
  let generation = 0;

  function importanceText(row) {
    if (row.importance_state === "unverified") return t("memtable.unverified");
    const shown = row.importance_state === "verified" || row.importance_state === "recorded";
    if (shown && typeof row.importance === "number" && Number.isFinite(row.importance)) {
      return row.importance.toFixed(2);
    }
    return NONE;
  }

  function retentionText(row) {
    const retention = row.retention || {};
    const state = retention.state;
    if (state === "unverified") return t("memtable.unverified");
    if (retention.kind === "invariant") {
      if (state === "changed") return t("memtable.ret_invariant_changed");
      return state === "tagged" || state === "verified" ? t("memtable.ret_invariant") : NONE;
    }
    if (retention.kind !== "derived") return NONE;
    const days = Number.isInteger(retention.half_life_days) ? retention.half_life_days : null;
    if (state === "invalid") return t("memtable.ret_invalid");
    if (state === "tagged" || state === "verified" || state === "changed") {
      return days === null ? t("memtable.ret_derived") : t("memtable.ret_half_life", { days });
    }
    if (state !== "default") return NONE;
    return days === null ? t("memtable.ret_default") : t("memtable.ret_default_half_life", { days });
  }

  function cell(text, className) {
    const node = document.createElement("td");
    if (className) node.className = className;
    node.textContent = text;
    return node;
  }

  async function fetchLayer(layer) {
    const response = await fetch(`/api/memory/entries?layer=${layer}`, {
      headers: { Accept: "application/json" }, cache: "no-store"
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const payload = await response.json();
    if (!payload || !Array.isArray(payload.entries)) throw new Error("invalid");
    return payload;
  }

  async function load() {
    const mine = ++generation;
    status.textContent = t("memtable.loading");
    let payloads;
    try {
      payloads = await Promise.all(LAYERS.map(fetchLayer));
    } catch (error) {
      if (mine !== generation) return;
      body.replaceChildren();
      table.hidden = true;
      status.textContent = t("memtable.load_failed");
      return;
    }
    if (mine !== generation) return;
    const rows = [];
    let total = 0;
    payloads.forEach((payload, index) => {
      const count = Number(payload.total);
      total += Number.isFinite(count) ? Math.max(count, payload.entries.length) : payload.entries.length;
      for (const row of payload.entries) {
        if (!row || typeof row.key !== "string") continue;
        const line = document.createElement("tr");
        line.append(
          cell(t(`forest.${LAYERS[index]}`)),
          cell(row.key, "memtable-key"),
          cell(importanceText(row)),
          cell(retentionText(row))
        );
        rows.push(line);
      }
    });
    body.replaceChildren(...rows);
    table.hidden = rows.length === 0;
    if (!rows.length) status.textContent = t("forest.empty");
    else if (total > rows.length) status.textContent = t("memtable.loaded_truncated", { shown: rows.length, total });
    else status.textContent = t("memtable.loaded", { shown: rows.length });
  }

  const refresh = $("refresh");
  if (refresh) refresh.addEventListener("click", () => { void load(); });
  void load();
})();
