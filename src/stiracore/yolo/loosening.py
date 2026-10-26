"""The YOLO window as fields of the one loosening judgement (ADR 0012 §2).

:func:`stiracore.tools.tool_config.rules_loosening` is the one function; it calls
:func:`window_loosening` for the window fields. A window snapshot is a mapping::

    {"yolo": "off" | "internal" | "external", "state": "active" | "paused" | "ended",
     "cap": int, "expires_at": float, "grant_id": str, "incarnation": str,
     "exec": bool}  # "exec" optional (absent = False), phase 7

(``{"yolo": "off"}`` = no window). The ordering is ``off < internal < external`` and
``paused < active``; a higher cap and a later end are looser; ``exec`` ``False < True``
(phase 7, §9 option C: only at the issue, only with tier external — a window never gains
exec later).

| Transition | Loosens? | Allowed context |
|---|---|---|
| off → tier (+ exec) | yes | ``host_confirmed_request``, tier ≤ allowance; exec only with external |
| paused → active | yes | ``breaker_resume``, same grant id, incarnation, tier, end, cap |
| narrowing (pause, end, lower cap) | no | any YOLO context |
| anything else | yes | none |

Every other context — hub save, the tool-rule APIs, rollback, an interrupted save,
``RECOVER``, ``stira tool-rules`` … — treats ``yolo*`` as reserved: the reason list then
contains ``yolo_not_settable`` and the caller refuses.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .allowance import tier_rank
from .values import cap_value, finite_time, hex_id

__all__ = [
    "ALLOW_CONTEXT",
    "ALLOW_NOT_SETTABLE",
    "NOT_SETTABLE",
    "YOLO_CONTEXTS",
    "allow_loosening",
    "allow_reserved_keys",
    "window_loosening",
]

NOT_SETTABLE = "yolo_not_settable"
YOLO_CONTEXTS = frozenset({"host_confirmed_request", "breaker_resume", "yolo_narrowing"})
_STATE_RANK = {"ended": 0, "paused": 1, "active": 2}
_IDENTITY = ("grant_id", "incarnation")


def _tier(snapshot: Mapping) -> int:
    return tier_rank(snapshot.get("yolo", "off"))


def _valid_window(snapshot: Mapping) -> bool:
    """Every field of an open/ended window snapshot is present and well-formed.

    ``cap`` an int 1..CAP_MAX, ``expires_at`` finite and non-negative, ids 32 hex, a known
    state. NaN, ±inf, bool, a negative or a missing value: invalid — never a narrowing.
    """
    return (
        snapshot.get("state") in _STATE_RANK
        and type(snapshot.get("exec", False)) is bool
        and cap_value(snapshot.get("cap"))
        and finite_time(snapshot.get("expires_at"))
        and all(hex_id(snapshot.get(key)) for key in _IDENTITY)
    )


def window_loosening(
    before: Any, after: Any, context: str | None, *, allowance: str = "off"
) -> list[str]:
    """Loosening reasons of ``before → after``, plus ``yolo_not_settable`` if not allowed.

    Both snapshots are validated in full before anything is compared (a value that
    cannot be ordered — NaN, inf, a missing field — makes the transition invalid).
    """
    if context not in YOLO_CONTEXTS:
        return [NOT_SETTABLE]
    if not isinstance(before, Mapping) or not isinstance(after, Mapping):
        return ["snapshot", NOT_SETTABLE]
    try:
        tier_before, tier_after = _tier(before), _tier(after)
        allowed_rank = tier_rank(allowance)
    except ValueError:
        return ["snapshot", NOT_SETTABLE]
    if tier_after == 0:
        return []  # ends / no window: a narrowing
    if not _valid_window(after) or (tier_before != 0 and not _valid_window(before)):
        return ["snapshot", NOT_SETTABLE]
    state_after = _STATE_RANK[after["state"]]
    if tier_before == 0:
        # Issue: off → tier, into an open window within the allowance.
        # Exec (phase 7) only in a window of tier external.
        exec_ok = not after.get("exec", False) or tier_after == tier_rank("external")
        allowed = (
            context == "host_confirmed_request"
            and tier_after <= allowed_rank
            and state_after >= 1
            and exec_ok
        )
        reasons = ["yolo", "yolo_exec"] if after.get("exec", False) else ["yolo"]
        return reasons if allowed else [*reasons, NOT_SETTABLE]
    reasons = []
    if tier_after > tier_before:
        reasons.append("yolo")
    if after.get("exec", False) and not before.get("exec", False):
        reasons.append("yolo_exec")
    if state_after > _STATE_RANK[before["state"]]:
        reasons.append("yolo_state")
    for key in ("cap", "expires_at"):
        if after[key] > before[key]:
            reasons.append(f"yolo_{key}")
    if any(before[key] != after[key] for key in _IDENTITY):
        reasons.append("yolo_identity")
    if not reasons:
        return []
    resume = (
        context == "breaker_resume"
        and reasons == ["yolo_state"]
        and before["state"] == "paused"
        and after["state"] == "active"
    )
    return reasons if resume else [*reasons, NOT_SETTABLE]


# -- ADR 0025 §8: the session allowance as a field of the same judgement ---------------

ALLOW_CONTEXT = "card_allow_again"
ALLOW_NOT_SETTABLE = "allow_not_settable"
_ALLOW_PREFIX = "session_allow"
_ALLOW_RANK = {"none": 0, "active": 1}
_MAX_DEPTH = 12


def allow_reserved_keys(value: Any, _depth: int = 0) -> list[str]:
    """Every key (or dotted key part) that names ``session_allow*``, at any depth. Too
    deep a structure counts as reserved (it cannot be checked)."""
    if _depth > _MAX_DEPTH:
        return ["<depth>"]
    found: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            if isinstance(key, str) and any(
                part.strip().lower().startswith(_ALLOW_PREFIX)
                for part in key.replace("/", ".").split(".")
            ):
                found.append(key[:64])
            found.extend(allow_reserved_keys(item, _depth + 1))
    elif isinstance(value, (list, tuple)):
        for item in value:
            found.extend(allow_reserved_keys(item, _depth + 1))
    return found


def allow_loosening(before: Any, after: Any) -> list[str]:
    """``card_allow_again``: ``none → active`` is the one allowed loosening
    (``["session_allow"]``); an end (``active → none``) is a narrowing (``[]``); anything
    else cannot be ordered and is refused."""
    try:
        old = _ALLOW_RANK[before.get("session_allow", "none")]
        new = _ALLOW_RANK[after.get("session_allow", "none")]
        extra = (set(before) | set(after)) - {"session_allow"}
    except (AttributeError, KeyError, TypeError):
        return ["snapshot", ALLOW_NOT_SETTABLE]
    if extra:
        return ["snapshot", ALLOW_NOT_SETTABLE]
    return ["session_allow"] if new > old else []
