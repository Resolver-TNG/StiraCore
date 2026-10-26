// Breaker pairs and details (#471): the page's rounding helpers, pure (no DOM).
// The server validates every value again; these only keep a draft inside the limits and
// say what was changed, so a slider or a typed number never leaves the operator stuck on
// an error they cannot see the way out of. Rounding only ever moves a value to the safe
// side of a pair (an exit goes up with its entry; an entry is never lowered for an exit).
(() => {
  "use strict";
  const STEP = 0.01;
  const round2 = (value) => Math.round(value * 100) / 100;

  // ``which`` = "entry" | "exit": the slider the operator moved.
  // Returns { entry, exit, note } with note "exit_raised" | "exit_clamped" | null.
  function roundPair(entry, exit, which, gap) {
    let note = null;
    entry = round2(Math.min(Math.max(entry, 0), 1 - STEP));
    exit = round2(Math.min(Math.max(exit, STEP), 1));
    if (entry >= exit) {
      if (which === "exit") {
        exit = round2(entry + STEP);
        note = "exit_clamped";
      } else {
        const keep = Number.isFinite(gap) && gap >= STEP ? gap : STEP;
        exit = round2(Math.min(1, entry + keep));
        note = "exit_raised";
      }
    }
    return { entry, exit, note };
  }

  // The critical exit must stay below the warning exit: raise the warning exit (safe side).
  // Returns { warningExit, note } with note "warning_exit_raised" | null.
  function orderExits(warningExit, criticalExit) {
    if (criticalExit < warningExit) return { warningExit, note: null };
    const raised = round2(criticalExit + STEP);
    if (raised > 1) return { warningExit, note: null }; // left to the validation message
    return { warningExit: raised, note: "warning_exit_raised" };
  }

  // A typed detail value into its rule ({integer, min, max, min_exclusive, clamp}).
  // Returns { value, note } with note "rounded" | "not_number" | null.
  // ``clamp: false`` (the sycophancy threshold): the value is kept as typed and the
  // validation shows the error, because the end of its range is not the safe side.
  function roundDetail(raw, rule) {
    const text = typeof raw === "string" ? raw.trim() : raw;
    const number = typeof text === "number" ? text : text === "" ? NaN : Number(text);
    if (!Number.isFinite(number)) return { value: null, note: "not_number" };
    let value = rule.integer ? Math.round(number) : number;
    if (rule.clamp === false) return { value, note: value !== number ? "rounded" : null };
    // An exclusive lower bound (the approval timeout > 0) rounds to the smallest whole step.
    const low = rule.min_exclusive ? rule.min + 1 : rule.min;
    if (value < low || (rule.min_exclusive && value <= rule.min)) value = low;
    if (rule.max !== null && rule.max !== undefined && value > rule.max) value = rule.max;
    return { value, note: value !== number ? "rounded" : null };
  }

  // Where the entry/exit markers sit on a 0..1 bar, in percent (the hysteresis picture).
  function band(entry, exit) {
    const left = Math.max(0, Math.min(1, entry)) * 100;
    const right = Math.max(0, Math.min(1, exit)) * 100;
    return { left: round2(left), width: round2(Math.max(0, right - left)) };
  }

  const api = { roundPair, orderExits, roundDetail, band, STEP };
  if (typeof window !== "undefined") window.StiraBreakerPairs = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})();
