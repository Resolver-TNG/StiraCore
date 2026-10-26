"""Which saved ``stira_config_overrides`` the chat server gives to its engine (#272).

``AgentConfig.stira_config_overrides`` is validated, saved and shown for every
``StiraConfig`` field. ``dashboard/stira_server.py`` builds its own ``StiraConfig``
(``critical_action="halt"``, the identity anchors); this module is the one place that says,
per field, how a saved value reaches the engine:

- ``next_turn``: copied onto the running engine before each turn, all together (the entry
  thresholds with their exits, so a pair that is valid in the file is never half applied).
- ``restart``: read once when the server starts.
- ``never``: not given to the engine by the chat server; the server chooses
  (``critical_action``, ``blocked_tool_names``). A separate decision.

The values a human saved are the values the engine runs on. The safety valves stay in
the validation that every save and every load goes through (``StiraConfig``,
``AgentConfig``): the cooldown is refused below 3, ``bc_window_size`` below 5, the exits must
stay above their entries, the approval timeout must be positive, and
``protected_layers`` must keep layers 3, 4 and 5 (any other positive layer may be added,
which only protects more). ``tests/test_stira_config_overrides.py`` pins the table and the valves.
"""

from __future__ import annotations

import math
from dataclasses import fields
from typing import Any

from stiracore.core.engine import StiraConfig

# Copied onto the running engine each turn: the settings hub's thresholds (#227) and their
# recovery thresholds, applied as one set (#272).
NEXT_TURN_FIELDS = (
    "stability_warning",
    "stability_critical",
    "bc_threshold",
    "stability_warning_exit",
    "stability_critical_exit",
    "bc_exit_threshold",
)
# Read when the server starts (#272).
START_FIELDS = (
    "bc_window_size",
    "decay_min_interval_sec",
    "critical_cooldown_turns",
    "sycophancy_threshold",
    "sycophancy_window_size",
    "protected_layers",
    "hitl_timeout_sec",
    "hitl_max_edit_rounds",
)
# What the chat server sets itself whatever the file says (``never`` fields it chooses). The
# settings page shows these when no running engine can be asked (#471 review m-3).
SERVER_CHOSEN = {"critical_action": "halt"}
# What a saved ``protected_layers`` must keep: the default set (L3+), the protection of the
# Neural State architecture. Other positive layers may be added; these are never removed.
REQUIRED_PROTECTED_LAYERS = frozenset(StiraConfig().protected_layers)
# Not an override: the file's own ``identity_anchors`` key, read when the server starts.
_RESTART_ONLY = ("identity_anchors",)


def applies_when(name: str) -> str:
    """``next_turn``, ``restart`` or ``never`` for a ``StiraConfig`` field."""
    if name in NEXT_TURN_FIELDS:
        return "next_turn"
    if name in START_FIELDS or name in _RESTART_ONLY:
        return "restart"
    return "never"


def never_applied(overrides: dict[str, Any] | None) -> list[str]:
    """Saved override names the chat server does not give to its engine, sorted."""
    return sorted(name for name in (overrides or {}) if applies_when(name) == "never")


def startup_values(overrides: dict[str, Any] | None) -> dict[str, Any]:
    """The saved overrides the engine is built with: start-up and per-turn fields."""
    saved = overrides or {}
    return {
        name: _engine_value(name, saved[name])
        for name in (*START_FIELDS, *NEXT_TURN_FIELDS)
        if name in saved
    }


def _engine_value(name: str, value: Any) -> Any:
    return frozenset(value) if name == "protected_layers" else value


# An approval cannot wait for ever: a saved timeout above this is capped (24 h); one that is
# not a positive number (or NaN) is the engine's default (#427 review).
HITL_TIMEOUT_DEFAULT_SEC = float(StiraConfig().hitl_timeout_sec)
HITL_TIMEOUT_MAX_SEC = 86_400.0


def bounded_hitl_timeout(value: Any) -> float | None:
    """The timeout the engine gets for a saved ``hitl_timeout_sec``, or ``None`` if as saved."""
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or math.isnan(value)
        or value <= 0
    ):
        return HITL_TIMEOUT_DEFAULT_SEC
    if value > HITL_TIMEOUT_MAX_SEC:
        return HITL_TIMEOUT_MAX_SEC
    return None


def completed_protected_layers(layers: Any) -> list[int] | None:
    """``layers`` plus the missing required layers, or ``None`` if nothing needs adding.

    Only for a list/tuple/set of positive ints: anything else is left to the validation,
    which refuses it. Used by the load path, which completes a hand-edited file instead of
    stopping the start; a settings save still goes through ``check_protected_layers``.
    """
    if not isinstance(layers, (list, tuple, set, frozenset)) or any(
        type(layer) is not int or layer < 1 for layer in layers
    ):
        return None
    if REQUIRED_PROTECTED_LAYERS <= set(layers):
        return None
    return sorted(set(layers) | REQUIRED_PROTECTED_LAYERS)


def check_protected_layers(layers: Any) -> None:
    """Refuse a saved ``protected_layers`` that drops L3+ (the architecture protection)."""
    if not REQUIRED_PROTECTED_LAYERS <= set(layers):
        raise ValueError(
            "protected_layers must keep layers "
            + ", ".join(str(layer) for layer in sorted(REQUIRED_PROTECTED_LAYERS))
            + " (other positive layers may be added)"
        )


def field_names() -> tuple[str, ...]:
    return tuple(item.name for item in fields(StiraConfig))


def _plain(value: Any) -> Any:
    """JSON/compare form: sets and frozensets as sorted lists."""
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    if isinstance(value, (list, tuple)):
        return list(value)
    return value


def plain_values(config: StiraConfig) -> dict[str, Any]:
    """Every ``StiraConfig`` field of ``config`` in JSON form (the running engine's, #272)."""
    return {name: _plain(getattr(config, name)) for name in field_names()}


def apply_rows(
    saved: StiraConfig,
    running: dict[str, Any] | None,
    overrides: dict[str, Any] | None = None,
    error: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Per field: the saved value, the running engine's, when it applies and the status.

    The delivery table decides first; equal values never turn a field the chat server does
    not apply into ``applied``:

    - ``never`` fields: ``not_applied`` when the saved overrides set the field,
      ``server_fixed`` when they do not (the server chooses, e.g. ``critical_action="halt"``).
    - ``next_turn`` / ``restart`` fields: ``applied`` when equal; otherwise ``pending`` (a
      turn or a restart will apply it) or, for a ``next_turn`` field while the last apply
      failed (``error``, the hub's reason), ``failed`` with ``reason``: it does not apply on
      the next turn or after a restart as long as the saved values stay as they are.
    - ``saved``: no running engine to compare with (e.g. the observer console).
    """
    rows: dict[str, dict[str, Any]] = {}
    for name in field_names():
        value = _plain(getattr(saved, name))
        when = applies_when(name)
        row: dict[str, Any] = {"saved": value, "applies": when, "effective": None}
        if running is None or name not in running:
            row["status"] = "saved"
        else:
            row["effective"] = running[name]
            if when == "never":
                row["status"] = "not_applied" if name in (overrides or {}) else "server_fixed"
            elif running[name] == value:
                row["status"] = "applied"
            elif error and when == "next_turn":
                row["status"] = "failed"
                row["reason"] = error
            else:
                row["status"] = "pending"
        rows[name] = row
    return rows
