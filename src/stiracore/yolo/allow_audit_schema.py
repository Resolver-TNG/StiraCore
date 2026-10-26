"""The ``audit.allow.*`` and session-allowance ``audit.tool.*`` rows (ADR 0025 §7, PR 1).

Registered in the same allow-list machinery as ``audit.yolo.*``
(:func:`.audit_schema.build`), not in ADR 0013's general registry. Values are codes,
random ids, integers and keyed hashes only — never a path, a host, an argument or any
other text in clear (§7 "No free text"). These rows carry no ``window_id`` (it is
required only for YOLO rows).

| Row | phase / delta | written for |
|---|---|---|
| ``audit.allow.decided`` | decision / loosening | a card decided with one of the two new buttons (the press's own decision; "Approve once" alone keeps today's path) |
| ``audit.allow.issued`` | decision / loosening | §4a step 2, the allowance's ``standing`` receipt |
| ``audit.allow.ended`` | transition / narrowing | §5, one record or ``all`` |
| ``audit.tool.started`` | intent / loosening | an allowance-authorized call, and the press's own once-approved call |
| ``audit.tool.committed`` / ``.failed`` / ``.void`` | outcome | the call's terminal result |
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from .allow_class import CLASS_KINDS
from .audit_schema import (
    _EVENT_REF,
    _HEX32,
    _HEX32_CHECK,
    _HEX64,
    _HEX64_CHECK,
    BREAKER_LEVELS,
    RowSpec,
    _integer,
    _number,
    _one_of,
    _operation,
    _text,
)
from .session_allow import END_REASONS

__all__ = ["ALLOW_REGISTRY", "BUTTONS", "TOOL_VOID_REASONS"]

BUTTONS = ("allow_again", "make_always")
TOOL_VOID_REASONS = frozenset(
    {"ended", "moved", "time", "binding_spent", "binding_invalid", "audit_failed", "refused"}
)
_GEN = _integer(0)
_REF = _text(_EVENT_REF)
_LAYERS = frozenset({"episodic"})
_EXPECTED_CARD = re.compile(r"pending:(" + "|".join(sorted(CLASS_KINDS)) + r")")


def _allow_target(v: Any) -> bool:
    """``dir:<keyed hash>``, ``host:<keyed hash>``, ``layer:<enum>`` or ``tool:<keyed
    hash>`` — never the directory, the host or the tool's arguments in clear."""
    if not isinstance(v, str):
        return False
    label, sep, rest = v.partition(":")
    if not sep:
        return False
    if label == "layer":
        return rest in _LAYERS
    return label in ("dir", "host", "tool") and bool(_HEX64.fullmatch(rest))


def _allow_policy(v: Any) -> bool:
    return (
        isinstance(v, Mapping)
        and set(v) == {"rules", "breaker"}
        and type(v["rules"]) is int
        and v["rules"] >= 0
        and v["breaker"] in BREAKER_LEVELS
    )


def _allow_ref(v: Any) -> bool:
    return v == "all" or (isinstance(v, str) and bool(_HEX32.fullmatch(v)))


def _started_rule(row: Mapping[str, Any]) -> str | None:
    if row["authorizer_kind"] == "session_allow":
        if row["decision"] != "auto_approved" or "issued_ref" not in row:
            return "an allowance call names its issued row"
        if "allow_id" not in row:
            return "an allowance call names its allowance"
    elif row["decision"] != "approved_once" or "decision_ref" not in row:
        return "a card call names the card's decision row"
    return None


def _ended_rule(row: Mapping[str, Any]) -> str | None:
    if row["allow_ref"] == "all":
        return None
    return None if row["generation_to"] == row["generation_from"] + 1 else "an end steps once"


def _spec(phase, delta, actors, surfaces, generation, required, optional=None, **kw):
    return RowSpec(
        phase,
        delta,
        frozenset(actors),
        frozenset(surfaces),
        generation,
        dict(required),
        dict(optional or {}),
        window=False,
        **kw,
    )


ALLOW_REGISTRY: dict[str, RowSpec] = {
    "audit.allow.decided": _spec(
        "decision",
        "loosening",
        ["human"],
        ["chat"],
        "zero",
        {
            "source_request_ref": _HEX64_CHECK,
            "operation": _operation,
            "args_ref": _HEX64_CHECK,
            "button": _one_of(BUTTONS),
            "session_ref": _HEX64_CHECK,
            "expected": _text(_EXPECTED_CARD),
        },
        {"allow_id": _HEX32_CHECK},
        human_op=True,
    ),
    "audit.allow.issued": _spec(
        "decision",
        "loosening",
        ["human"],
        ["chat"],
        "start",
        {
            "allow_id": _HEX32_CHECK,
            "source_request_ref": _HEX64_CHECK,
            "decision_receipt_ref": _REF,
            "operation": _operation,
            "class_kind": _one_of(CLASS_KINDS),
            "class_version": _integer(1, 1000),
            "target": _allow_target,
            "policy": _allow_policy,
            "fence": _GEN,
            "expires_at": _number,
            "session_ref": _HEX64_CHECK,
            "expected": _text(_EXPECTED_CARD),
        },
        human_op=True,
    ),
    "audit.allow.ended": _spec(
        "transition",
        "narrowing",
        ["system", "human"],
        ["chat", "admin", "system"],
        "any",
        {
            "allow_ref": _allow_ref,
            "end_reason": _one_of(END_REASONS),
            "uses": _integer(0, 10**9),
        },
        {"fence": _GEN},
        rule=_ended_rule,
    ),
    "audit.tool.started": _spec(
        "intent",
        "loosening",
        ["system"],
        ["chat"],
        "any",
        {
            "operation": _operation,
            "args_ref": _HEX64_CHECK,
            "decision": _one_of(("auto_approved", "approved_once")),
            "authorizer_kind": _one_of(("session_allow", "human_card")),
        },
        {
            "allow_id": _HEX32_CHECK,
            "issued_ref": _REF,
            "decision_ref": _REF,
            "tool_use_ref": _HEX64_CHECK,
        },
        rule=_started_rule,
    ),
    "audit.tool.committed": _spec(
        "outcome", "loosening", ["system"], ["chat"], "any", {"intent_ref": _REF}
    ),
    "audit.tool.failed": _spec(
        "outcome", "loosening", ["system"], ["chat"], "any", {"intent_ref": _REF}
    ),
    "audit.tool.void": _spec(
        "outcome",
        "narrowing",
        ["system"],
        ["chat", "system"],
        "any",
        {"intent_ref": _REF, "reason_code": _one_of(TOOL_VOID_REASONS)},
    ),
}
