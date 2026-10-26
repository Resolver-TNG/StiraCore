"""Why the breaker is at its level (#455): reason codes and the numbers that decided it.

The explanation is derived from the same values the engine compared, after it decided:
it never changes the decision. It holds fixed codes and rounded numbers only, never
conversation text, memory content or identifiers.

Codes (the level they explain):

- ``stability_below_critical`` (CRITICAL) — stability < stability_critical
- ``stability_below_warning`` (WARNING) — stability < stability_warning
- ``bc_score_below_coherence`` (COHERENCE) — bc_score < bc_threshold
- ``hysteresis_hold`` (CRITICAL / WARNING / COHERENCE) — the entry condition no longer
  holds, but the value has not yet passed the recovery threshold of that level
- ``cooldown_active`` (CRITICAL) — the protected turns are not yet served
- ``stability_above_critical_exit`` / ``cooldown_served`` — CRITICAL was left
- ``stability_above_warning_exit`` — the WARNING latch was released
- ``bc_score_above_coherence_exit`` — the COHERENCE latch was released
- ``within_thresholds`` (NORMAL) — nothing is latched
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from typing import Any

REASON_CODES = (
    "stability_below_critical",
    "stability_below_warning",
    "bc_score_below_coherence",
    "hysteresis_hold",
    "cooldown_active",
    "stability_above_critical_exit",
    "cooldown_served",
    "stability_above_warning_exit",
    "bc_score_above_coherence_exit",
    "within_thresholds",
)
INPUT_KEYS = (
    "stability",
    "bc_score",
    "stability_warning",
    "stability_warning_exit",
    "stability_critical",
    "stability_critical_exit",
    "bc_threshold",
    "bc_exit_threshold",
    "critical_turns",
    "critical_cooldown_turns",
)
_INT_INPUTS = frozenset({"critical_turns", "critical_cooldown_turns"})
LEVELS = ("normal", "coherence", "warning", "critical")
DIGITS = 4


def _number(key: str, value: Any) -> float | int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if key in _INT_INPUTS:
        return int(value) if isinstance(value, int) and 0 <= value <= 1_000_000 else None
    number = float(value)
    if not math.isfinite(number) or not -1e6 <= number <= 1e6:
        return None
    return round(number, DIGITS)


def clean(reasons: Any, inputs: Any) -> tuple[list[str], dict[str, float | int]]:
    """Allow-listed codes (known, in order, no repeats) and numeric inputs of known names.

    Anything else is dropped, so a caller can forward the result to an event or API as is.
    """
    codes: list[str] = []
    if isinstance(reasons, Iterable) and not isinstance(reasons, (str, bytes, Mapping)):
        for code in reasons:
            if isinstance(code, str) and code in REASON_CODES and code not in codes:
                codes.append(code)
    values: dict[str, float | int] = {}
    if isinstance(inputs, Mapping):
        for key in INPUT_KEYS:
            number = _number(key, inputs.get(key))
            if number is not None:
                values[key] = number
    return codes, values


def clean_transition(record: Any) -> dict[str, Any] | None:
    """``{"from", "to", "reasons", "inputs"}`` with known levels only, else None."""
    if not isinstance(record, Mapping):
        return None
    start, end = record.get("from"), record.get("to")
    if start not in LEVELS or end not in LEVELS:
        return None
    reasons, inputs = clean(record.get("reasons"), record.get("inputs"))
    return {"from": start, "to": end, "reasons": reasons, "inputs": inputs}


def explain(
    *,
    level: str,
    prior_level: str,
    prior_critical_turns: int,
    warning_was: bool,
    warning_now: bool,
    coherence_was: bool,
    coherence_now: bool,
    stability: float,
    bc_score: float,
    config: Any,
) -> dict[str, Any]:
    """The codes and numbers for one evaluation whose result was ``level``.

    ``prior_*`` / ``*_was`` are the engine's latches before the evaluation, ``*_now`` after.
    """
    reasons: list[str] = []
    inputs: dict[str, Any] = {}

    def add(code: str, **values: Any) -> None:
        if code not in reasons:
            reasons.append(code)
        inputs.update(values)

    if level != prior_level:
        if prior_level == "critical":
            add(
                "stability_above_critical_exit",
                stability=stability,
                stability_critical_exit=config.stability_critical_exit,
            )
            add(
                "cooldown_served",
                critical_turns=prior_critical_turns,
                critical_cooldown_turns=config.critical_cooldown_turns,
            )
        if warning_was and not warning_now:
            add(
                "stability_above_warning_exit",
                stability=stability,
                stability_warning_exit=config.stability_warning_exit,
            )
        if coherence_was and not coherence_now:
            add(
                "bc_score_above_coherence_exit",
                bc_score=bc_score,
                bc_exit_threshold=config.bc_exit_threshold,
            )

    if level == "critical":
        if stability < config.stability_critical:
            add(
                "stability_below_critical",
                stability=stability,
                stability_critical=config.stability_critical,
            )
        elif stability <= config.stability_critical_exit:
            add(
                "hysteresis_hold",
                stability=stability,
                stability_critical_exit=config.stability_critical_exit,
            )
        if prior_level == "critical" and prior_critical_turns < config.critical_cooldown_turns:
            add(
                "cooldown_active",
                critical_turns=prior_critical_turns,
                critical_cooldown_turns=config.critical_cooldown_turns,
            )
    elif level == "warning":
        if stability < config.stability_warning:
            add(
                "stability_below_warning",
                stability=stability,
                stability_warning=config.stability_warning,
            )
        else:
            add(
                "hysteresis_hold",
                stability=stability,
                stability_warning_exit=config.stability_warning_exit,
            )
    elif level == "coherence":
        if bc_score < config.bc_threshold:
            add(
                "bc_score_below_coherence",
                bc_score=bc_score,
                bc_threshold=config.bc_threshold,
            )
        else:
            add(
                "hysteresis_hold",
                bc_score=bc_score,
                bc_exit_threshold=config.bc_exit_threshold,
            )
    elif not reasons:
        add(
            "within_thresholds",
            stability=stability,
            stability_warning=config.stability_warning,
            bc_score=bc_score,
            bc_threshold=config.bc_threshold,
        )

    codes, values = clean(reasons, inputs)
    return {"level": level, "reasons": codes, "inputs": values}
