/* Breaker reasons in words (#455).
 *
 * The server sends why the breaker is at a level as fixed codes and rounded numbers
 * (core/breaker_reasons.py): `reasons` ["stability_below_warning"] and `inputs`
 * {stability: 0.31, stability_warning: 0.35}. This turns them into one line such as
 * "WARNING: 安定度 0.31 < 0.35". A code is never shown as is (#279): an unknown one reads
 * as a fixed word. Text only (callers set it with textContent).
 *
 * StiraBreakerReasons.describe(level, reasons, inputs, t) -> "安定度 0.31 < 0.35; ..." or ""
 * StiraBreakerReasons.line(level, reasons, inputs, t)     -> "WARNING: ..." or ""
 * `t(key, vars)` is the page's translator (window.t, or Stage's S.t).
 */
(function (root) {
  "use strict";
  const LEVELS = new Set(["normal", "coherence", "warning", "critical"]);

  function num(value, digits) {
    if (typeof value !== "number" || !Number.isFinite(value)) return "?";
    return Number.isInteger(value) && digits === 0 ? String(value) : value.toFixed(digits);
  }

  function count(value) {
    return Number.isInteger(value) && value >= 0 ? String(value) : "?";
  }

  function metric(name, t) {
    return t(name === "bc_score" ? "breaker_reason.metric_bc" : "breaker_reason.metric_stability");
  }

  // The recovery threshold that holds `level` (hysteresis_hold).
  const HOLD = {
    critical: ["stability", "stability_critical_exit"],
    warning: ["stability", "stability_warning_exit"],
    coherence: ["bc_score", "bc_exit_threshold"],
  };

  function phrase(code, level, inputs, t) {
    const v = (key) => num(inputs[key], 2);
    const cmp = (key, value, threshold, op) =>
      t(`breaker_reason.${op}`, { metric: metric(key, t), value: v(value), threshold: v(threshold) });
    switch (code) {
      case "stability_below_critical": return cmp("stability", "stability", "stability_critical", "below");
      case "stability_below_warning": return cmp("stability", "stability", "stability_warning", "below");
      case "bc_score_below_coherence": return cmp("bc_score", "bc_score", "bc_threshold", "below");
      case "hysteresis_hold": {
        const pair = HOLD[level];
        return pair ? cmp(pair[0], pair[0], pair[1], "hold") : t("breaker_reason.unknown_code");
      }
      case "cooldown_active":
        return t("breaker_reason.cooldown_active", {
          turns: count(inputs.critical_turns), needed: count(inputs.critical_cooldown_turns),
        });
      case "cooldown_served":
        return t("breaker_reason.cooldown_served", { needed: count(inputs.critical_cooldown_turns) });
      case "stability_above_critical_exit": return cmp("stability", "stability", "stability_critical_exit", "above_exit");
      case "stability_above_warning_exit": return cmp("stability", "stability", "stability_warning_exit", "above_exit");
      case "bc_score_above_coherence_exit": return cmp("bc_score", "bc_score", "bc_exit_threshold", "above_exit");
      case "within_thresholds":
        return t("breaker_reason.within", { stability: v("stability"), bc: v("bc_score") });
      default:
        return t("breaker_reason.unknown_code");
    }
  }

  function describe(level, reasons, inputs, t) {
    if (!Array.isArray(reasons) || !reasons.length || typeof t !== "function") return "";
    const lv = typeof level === "string" ? level.toLowerCase() : "";
    const values = inputs && typeof inputs === "object" ? inputs : {};
    const parts = [];
    for (const code of reasons.slice(0, 8)) {
      const text = phrase(typeof code === "string" ? code : "", lv, values, t);
      if (!parts.includes(text)) parts.push(text);
    }
    return parts.join(t("breaker_reason.separator"));
  }

  function line(level, reasons, inputs, t) {
    const why = describe(level, reasons, inputs, t);
    if (!why) return "";
    const lv = typeof level === "string" ? level.toLowerCase() : "";
    const name = LEVELS.has(lv) ? t(`breaker_reason.level_${lv}`) : t("breaker_reason.level_unknown");
    return `${name}: ${why}`;
  }

  const api = { describe, line };
  root.StiraBreakerReasons = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof globalThis !== "undefined" ? globalThis : this);
