"""The ``audit.yolo.*`` row contract (ADR 0012 §11 on ADR 0013 §1a / §1b / §1d).

ADR 0013's general registry is not implemented yet; this is the YOLO part of it as an
allow-list, small enough to be replaced by ``audit_record.build`` later without changing
a row. :func:`build` is the only way to make a row:

* every common field (§1a) and every field the row's registry entry requires must be
  present and valid — otherwise :class:`AuditContractError` (an audit-write failure:
  no row, and the operation it was for is refused or, for a narrowing, the writer latches);
* a value that ``mask_secrets`` would change is never accepted;
* an optional field with a bad value is stored as the word ``invalid``;
* a field that is not in the row's entry is **refused** (ADR 0013 Open (2) leaves
  "dropped and counted" or "refused"; refusing is the fail-closed choice until decided);
* ``destination`` (ADR 0012 Open (10)) is in no entry: input text never reaches a row;
  ``audit.yolo.auto_approved`` carries only ``destination_ref``, the keyed hash of the
  host (option (b), phase 6).

Values are codes, generated ids, integers and keyed hashes only — never user or model
text (ADR 0013 §1d). Nothing here writes; :meth:`AuditChain.record_yolo` does.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from stiracore.observability.masking import mask_secrets

from .allowance import CAP_MAX
from .config_file import FIELDS as SETTING_FIELDS
from .state import ABNORMAL_END_REASONS, NORMAL_END_REASONS, PAUSE_REASONS, end_class
from .values import finite_time

__all__ = [
    "EXPECTED_IDLE",
    "REGISTRY",
    "SCHEMA",
    "AuditContractError",
    "RowSpec",
    "build",
]

SCHEMA = "audit/1"
# The expected state of a press (§1a 1.): no window, no request, latch clear.
EXPECTED_IDLE = "idle"

_HEX32 = re.compile(r"[0-9a-f]{32}")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_PRINCIPAL = re.compile(r"[A-Za-z0-9._:@-]{1,64}")
_KEY_ID = re.compile(r"[A-Za-z0-9._-]{1,32}")
_TOOL = re.compile(r"[a-z][a-z0-9_]{0,63}")
_EVENT_REF = re.compile(r"op:[0-9a-f]{32}:(decision|intent|outcome|transition)(:[0-9]{1,6})?")
_EXPECTED_PENDING = re.compile(r"pending:[0-9]{1,12}")
# Tool names a row may carry in clear: a code constant (an allow-list, not a pattern).
# ``tests/test_yolo_audit_schema.py`` fails when a registered tool is missing here.
# Anything else (MCP, dynamic names) only as ``mcp:<keyed hash>``.
KNOWN_TOOLS = frozenset(
    {
        "batch",
        "cron",
        "current_time",
        "diagram",
        "editor",
        "file_read",
        "file_write",
        "graph",
        "handoff_read",
        "handoff_to_user",
        "handoff_write",
        "http_request",
        "image_reader",
        "journal",
        "lsp_diagnostics",
        "lsp_hover",
        "memory",
        "memory_index",
        "memory_list",
        "memory_rate",
        "memory_read",
        "memory_search",
        "memory_write",
        "oauth_api_call",
        "page_check",
        "perplexity_search",
        "propose_procedure",
        "rss",
        "run_checks",
        "shell",
        "speak",
        "stop",
        "swarm",
        "think",
        "use_agent",
        "view_image",
        "web_fetch",
        "web_search",
        "webhook_send",
    }
)
# Root labels of a path target (``<label>:<keyed hash>``, ADR 0013 Open (11)); never a
# path, a file name or any other text in clear.
PATH_LABELS = frozenset({"workspace", "write_root", "data_dir", "memory", "outside"})
# (target_kind, target) pairs a tool row may carry.
_TOOL_TARGET_KINDS = frozenset({"path", "tool", "none"})

TARGET_KINDS = frozenset(
    {"tool", "path", "job", "approval", "grant", "window", "session", "setting", "store", "none"}
)
BREAKER_LEVELS = frozenset({"normal", "warning", "coherence", "critical", "unknown"})
VOID_REASONS = frozenset(
    {
        "permit_stale",
        "cap_full",
        "window_ended",
        "binding_mismatch",
        "consume_refused",
        # W1 (#296): the shared authority service refused (kill switch / unhealthy).
        "killed",
        "authority_unhealthy",
    }
)


class AuditContractError(ValueError):
    """A row that breaks the contract; never written (an audit-write failure, §1d)."""


# --- value checks ---------------------------------------------------------------------

Check = Callable[[Any], bool]


def _text(pattern: re.Pattern) -> Check:
    return lambda v: isinstance(v, str) and bool(pattern.fullmatch(v)) and mask_secrets(v) == v


def _one_of(values) -> Check:
    values = frozenset(values)
    return lambda v: isinstance(v, str) and v in values


def _integer(low: int, high: int | None = None) -> Check:
    return lambda v: (
        type(v) is int and v >= low and (high is None or v <= high)  # bool is not an int here
    )


def _number(v: Any) -> bool:
    return finite_time(v)


def _delta(v: Any) -> bool:
    """A finite clock delta in seconds, which may be negative (a clock that went back)."""
    return type(v) in (int, float) and math.isfinite(v) and abs(v) <= 10**10


# #596: why the clocks ended a window (``revoked_clock_anomaly``) — fixed words only.
CLOCK_CHECKS = frozenset(
    {"wall_backwards", "wall_behind_boot", "clock_unreadable", "watchdog_failed"}
)


def _operation(v: Any) -> bool:
    """A tool name from :data:`KNOWN_TOOLS`, or ``mcp:<keyed hash>`` — never another name."""
    if not isinstance(v, str):
        return False
    if v.startswith("mcp:"):
        return bool(_HEX64.fullmatch(v[4:]))
    return v in KNOWN_TOOLS


def _target(v: Any) -> bool:
    """``none``, or ``<path label>:<keyed hash>`` with a label from :data:`PATH_LABELS`."""
    if not isinstance(v, str):
        return False
    if v == "none":
        return True
    label, sep, digest = v.partition(":")
    return bool(sep and label in PATH_LABELS and _HEX64.fullmatch(digest))


def _tool_target_rule(row: Mapping[str, Any]) -> str | None:
    # The target's form must match its kind: a path only as label + digest; a tool or
    # nothing only as "none". A plain code or file name is never a target.
    kind, target = row["target_kind"], row["target"]
    if kind == "path":
        return None if target != "none" else "a path target needs a label and a digest"
    return None if target == "none" else "only a path target carries a value"


def _policy(v: Any) -> bool:
    if not isinstance(v, Mapping) or set(v) != {"rules", "epoch", "window", "breaker"}:
        return False
    integers = all(type(v[key]) is int and v[key] >= 0 for key in ("rules", "epoch", "window"))
    return integers and v["breaker"] in BREAKER_LEVELS


_COUNT = _integer(0, 10**9)
_CALL_DECISIONS = ("auto_approved", "always", "manual", "denied", "allowed_again")
_CALL_CATEGORIES = ("internal", "external", "exec")
_CALL_KEYS = frozenset({"total", "by_decision", "failed", "by_category", "by_tool"})


def _counts(v: Any, keys) -> bool:
    return type(v) is dict and set(v) == set(keys) and all(_COUNT(v[key]) for key in keys)


def _tool_calls(v: Any) -> bool:
    """#448: the window's tool calls — counts only, keyed by fixed codes and registry tool
    names (:data:`KNOWN_TOOLS` or ``other``); never an argument, output or destination."""
    if type(v) is not dict or set(v) != _CALL_KEYS:
        return False
    by_tool = v["by_tool"]
    return bool(
        _COUNT(v["total"])
        and _COUNT(v["failed"])
        and v["failed"] <= v["total"]
        and _counts(v["by_decision"], _CALL_DECISIONS)
        and sum(v["by_decision"].values()) == v["total"]
        and _counts(v["by_category"], _CALL_CATEGORIES)
        and sum(v["by_category"].values()) == v["total"]
        and type(by_tool) is dict
        and all(type(k) is str and (k in KNOWN_TOOLS or k == "other") for k in by_tool)
        and all(_COUNT(n) for n in by_tool.values())
        and sum(by_tool.values()) == v["total"]
    )


_HEX32_CHECK = _text(_HEX32)
_HEX64_CHECK = _text(_HEX64)
_TIER = _one_of(("internal", "external"))
_GEN = _integer(0)
_BOOL: Check = lambda v: type(v) is bool
# Phase 7 (ADR 0012 §9 option C): the person chose "Include exec" at the press. Optional
# (absent = no exec) on the request rows and ``started``.
_EXEC_FLAG: dict[str, Check] = {"exec": _BOOL}

# Common fields of every row (§1a) and of the ``audit.yolo.*`` family (§1b).
_COMMON: dict[str, Check] = {
    "schema": lambda v: v == SCHEMA,
    "operation_id": _HEX32_CHECK,
    "phase": _one_of(("decision", "intent", "outcome", "transition")),
    "event_id": _text(_EVENT_REF),
    "actor_kind": _one_of(("human", "system", "agent", "job")),
    "actor": _text(_PRINCIPAL),
    "surface": _one_of(("chat", "job", "admin", "cli", "system")),
    "incarnation_ref": _HEX64_CHECK,
    "key_id": _text(_KEY_ID),
    "delta_class": _one_of(("loosening", "narrowing")),
    "window_id": _HEX32_CHECK,
    "generation_from": _GEN,
    "generation_to": _GEN,
    "authority_epoch": _GEN,
}
_HUMAN_OP: dict[str, Check] = {"op_token_ref": _HEX64_CHECK}
# A settings row is about the next windows, not one: the zero window id (PR #444 M1).
NO_WINDOW = "0" * 32


def _setting_names(v: Any) -> bool:
    """A non-empty, sorted list of distinct ``yolo`` field names — names, never values."""
    return (
        type(v) is list
        and bool(v)
        and all(type(name) is str and name in SETTING_FIELDS for name in v)
        and v == sorted(set(v))
    )


@dataclass(frozen=True)
class RowSpec:
    """One registry entry: fixed values, allowed actors/surfaces, generation rule, fields."""

    phase: str
    delta_class: str
    actor_kinds: frozenset
    surfaces: frozenset
    generation: str  # zero (0→0) | start (0→1) | step (g→g+1, g≥1) | same (g→g, g≥1) | same0
    required: Mapping[str, Check] = field(default_factory=dict)
    optional: Mapping[str, Check] = field(default_factory=dict)
    human_op: bool = False
    rule: Callable[[Mapping[str, Any]], str | None] | None = None  # cross-field check
    # ADR 0025 PR 1: ``window_id`` is required only for rows of a YOLO window; the
    # ``audit.allow.*`` / ``audit.tool.*`` rows of a session allowance carry none.
    window: bool = True


def _ended_rule(row: Mapping[str, Any]) -> str | None:
    # An end_class that disagrees with the reason is refused: a writer can never close
    # a window "normally" for an abnormal reason (§6, §11).
    if row["end_class"] != end_class(row["end_reason"]):
        return "end_class does not match end_reason"
    return None


def _started_rule(row: Mapping[str, Any]) -> str | None:
    expires = row.get("expires_at")
    if expires is not None and expires != "invalid" and not expires > row["t0"]:
        return "expires_at must be after t0"
    external = row.get("external_cap")
    if isinstance(external, int) and external > row["cap"]:
        return "external_cap exceeds cap"
    exec_cap = row.get("exec_cap")
    if isinstance(exec_cap, int) and not isinstance(exec_cap, bool) and exec_cap > row["cap"]:
        return "exec_cap exceeds cap"
    if row.get("exec") is True and row["tier"] != "external":
        return "exec needs tier external"
    return None


def _exec_request_rule(row: Mapping[str, Any]) -> str | None:
    # Exec only with tier external (§9 option C): a row saying otherwise is refused.
    if row.get("exec") is True and row["tier"] != "external":
        return "exec needs tier external"
    return None


def _auto_approved_rule(row: Mapping[str, Any]) -> str | None:
    problem = _tool_target_rule(row)
    if problem is not None:
        return problem
    exec_row = row.get("tool_class") == "exec"
    if exec_row != ("exec_slot" in row) or exec_row != ("exec_limit" in row):
        return "an exec approval (and only one) names its exec slot and limit"
    return None


def _spec(phase, delta, actors, surfaces, generation, required=None, optional=None, **kw):
    return RowSpec(
        phase,
        delta,
        frozenset(actors),
        frozenset(surfaces),
        generation,
        dict(required or {}),
        dict(optional or {}),
        **kw,
    )


_TOOL_FIELDS: dict[str, Check] = {
    "operation": _operation,
    "target_kind": _one_of(_TOOL_TARGET_KINDS),
    "target": _target,
    "args_ref": _HEX64_CHECK,
    "decision": _one_of(("auto_approved",)),
    "authorizer_kind": _one_of(("yolo",)),
    "policy": _policy,
}

REGISTRY: dict[str, RowSpec] = {
    "audit.yolo.requested": _spec(
        "intent",
        "loosening",
        ["human"],
        ["admin"],
        "zero",
        {"request_id": _HEX32_CHECK, "tier": _TIER, "expected": _one_of((EXPECTED_IDLE,))},
        _EXEC_FLAG,
        human_op=True,
        rule=_exec_request_rule,
    ),
    "audit.yolo.confirmed": _spec(
        "decision",
        "loosening",
        ["human"],
        # cli: the host command / dialog; admin: the dashboard's checkbox confirmation
        # (ADR 0012 revision 6, the default channel). ADR 0013 change listed in ADR 0012.
        ["cli", "admin"],
        "zero",
        {"request_id": _HEX32_CHECK, "tier": _TIER, "expected": _text(_EXPECTED_PENDING)},
        _EXEC_FLAG,
        human_op=True,
        rule=_exec_request_rule,
    ),
    "audit.yolo.denied": _spec(
        "transition",
        "narrowing",
        ["human"],
        ["cli"],
        "zero",
        {"request_id": _HEX32_CHECK, "reason_code": _one_of(("denied",))},
    ),
    "audit.yolo.request_expired": _spec(
        "transition",
        "narrowing",
        ["system"],
        ["system"],
        "zero",
        {"request_id": _HEX32_CHECK, "reason_code": _one_of(("request_expired",))},
    ),
    "audit.yolo.request_cancelled": _spec(
        "transition",
        "narrowing",
        ["human"],
        ["admin"],
        "zero",
        {"request_id": _HEX32_CHECK, "reason_code": _one_of(("cancel",))},
    ),
    "audit.yolo.request_fenced": _spec(
        "transition",
        "narrowing",
        ["human", "system"],
        ["admin", "cli", "system"],
        "zero",
        {
            "request_id": _HEX32_CHECK,
            "reason_code": _one_of(("stop", "new_session", "cancel", "epoch")),
        },
    ),
    "audit.yolo.started": _spec(
        "decision",
        "loosening",
        ["system"],
        ["cli", "system"],
        "start",
        {
            "request_id": _HEX32_CHECK,
            "tier": _TIER,
            "cap": _integer(1, CAP_MAX),
            "t0": _number,
            "confirmed_ref": _text(_EVENT_REF),
        },
        {
            "external_cap": _integer(0, CAP_MAX),
            "expires_at": _number,
            **_EXEC_FLAG,
            "exec_cap": _integer(0, CAP_MAX),
        },
        rule=_started_rule,
    ),
    "audit.yolo.paused": _spec(
        "transition",
        "narrowing",
        ["system"],
        ["system"],
        "step",
        {"reason_code": _one_of(PAUSE_REASONS)},
    ),
    "audit.yolo.resumed": _spec(
        "transition",
        "loosening",
        ["system"],
        ["system"],
        "step",
        {"reason_code": _one_of(("breaker_resume",)), "paused_ref": _text(_EVENT_REF)},
    ),
    "audit.yolo.ended": _spec(
        "transition",
        "narrowing",
        ["system", "human"],
        ["admin", "cli", "system"],
        "step",
        {
            "end_reason": _one_of(NORMAL_END_REASONS | ABNORMAL_END_REASONS),
            "end_class": _one_of(("normal", "abnormal")),
            "used": _integer(0, CAP_MAX),
        },
        {
            "reserved": _integer(0, CAP_MAX),
            "external_used": _integer(0, CAP_MAX),
            "exec_used": _integer(0, CAP_MAX),
            # #448 (optional, additive): every tool call of the window's period by
            # decision / class / tool (counts only) and the turns it saw. A row without
            # them (older rows, a window that never opened) stays valid.
            "tool_calls": _tool_calls,
            "turns": _COUNT,
            # #596 (optional, additive; only on an end ``revoked_clock_anomaly``): which
            # clock test fired, the deltas since the press (``d_*``) and since the last
            # check (``last_d_*``) in seconds, and the slack it compared against.
            "clock_check": _one_of(CLOCK_CHECKS),
            "d_wall": _delta,
            "d_boot": _delta,
            "last_d_wall": _delta,
            "last_d_boot": _delta,
            "clock_slack_sec": _number,
        },
        rule=_ended_rule,
    ),
    "audit.yolo.auto_approved": _spec(
        "decision",
        "loosening",
        ["system"],
        ["chat", "job"],
        "same",
        _TOOL_FIELDS,
        {
            "tool_use_ref": _HEX64_CHECK,
            "turn_id": _text(_PRINCIPAL),
            "origin_kind": _one_of(("turn", "job")),
            # Phase 6: the tool's class, and for an external tool the keyed hash of its
            # destination host (Open (10) (b)) — never the host in clear.
            "tier": _TIER,
            "external": lambda v: type(v) is bool,
            "destination_ref": _HEX64_CHECK,
            # Phase 7: the tool's class, and for an exec tool the exec slot it holds
            # (1-based) and the exec sub-cap — never the command (``args_ref`` only).
            "tool_class": _one_of(("internal", "external", "exec")),
            "exec_slot": _integer(1, CAP_MAX),
            "exec_limit": _integer(0, CAP_MAX),
        },
        rule=_auto_approved_rule,
    ),
    "audit.yolo.consumed": _spec(
        "outcome",
        "loosening",
        ["system"],
        ["chat", "job"],
        "same",
        {"intent_ref": _HEX32_CHECK},
    ),
    "audit.yolo.void": _spec(
        "outcome",
        "narrowing",
        ["system"],
        ["chat", "job", "system"],
        "same",
        {"intent_ref": _HEX32_CHECK, "reason_code": _one_of(VOID_REASONS)},
    ),
    "audit.yolo.unlatched": _spec(
        "decision",
        "narrowing",
        ["human"],
        # cli: a host command; admin: the dashboard's confirmed operation (phase 3).
        ["cli", "admin"],
        "same0",
        {
            "latch_subject": _one_of(("window", "orphan_marker")),
            "marker_digest": _HEX64_CHECK,
            "chain_seq_seen": _integer(0),
            "expected": _text(re.compile(r"marker:[0-9a-f]{64}")),
        },
        human_op=True,
    ),
    "audit.yolo.unlatch_applied": _spec(
        "outcome",
        "narrowing",
        ["human"],
        ["cli", "admin"],
        "same0",
        {"latch_subject": _one_of(("window", "orphan_marker")), "intent_ref": _HEX32_CHECK},
    ),
    # Phase 7c (PR #444 M1): a person's confirmed save on the settings page that loosens
    # the ``yolo`` section of gateway-config.json, written before the file (a failed row
    # refuses the save). Field names only, never a value; no window (``NO_WINDOW``, 0 → 0).
    "audit.yolo.settings_saved": _spec(
        "decision",
        "loosening",
        ["human"],
        ["admin"],
        "zero",
        {
            "window_id": _one_of((NO_WINDOW,)),
            "fields": _setting_names,
            "changed": _setting_names,
        },
        rule=lambda row: (
            None if set(row["fields"]) <= set(row["changed"]) else "a loosened field is unchanged"
        ),
    ),
}


def _generation_problem(rule: str, start: int, end: int) -> str | None:
    if rule == "zero":
        ok = start == 0 and end == 0
    elif rule == "start":
        ok = start == 0 and end == 1
    elif rule == "step":
        ok = start >= 1 and end == start + 1
    elif rule == "same":
        ok = start >= 1 and end == start
    elif rule == "any":  # ADR 0025 rows: the record's own generation, checked by the rule
        ok = True
    else:  # same0
        ok = end == start
    return None if ok else f"generation {start} -> {end} breaks the {rule!r} rule"


def build(event_type: str, **fields: Any) -> dict[str, Any]:
    """Validate one ``audit.yolo.*`` row; returns ``{event_type, event_id, attributes}``."""
    spec = REGISTRY.get(event_type)
    if spec is None:
        from .allow_audit_schema import ALLOW_REGISTRY  # ADR 0025 PR 1 (same allow-list)

        spec = ALLOW_REGISTRY.get(event_type)
    if spec is None:
        raise AuditContractError("event type is not in the registry")
    required: dict[str, Check] = {**_COMMON, **spec.required}
    if not spec.window:
        del required["window_id"]
    if spec.human_op:
        required.update(_HUMAN_OP)
    unknown = sorted(set(fields) - set(required) - set(spec.optional))
    if unknown:
        raise AuditContractError("field(s) not in the registry entry: " + ", ".join(unknown))
    row: dict[str, Any] = {}
    for name, check in required.items():
        if name not in fields or fields[name] is None:
            raise AuditContractError(f"required field missing: {name}")
        if not check(fields[name]):
            raise AuditContractError(f"required field invalid: {name}")
        row[name] = fields[name]
    for name, check in spec.optional.items():
        if name in fields and fields[name] is not None:
            row[name] = fields[name] if check(fields[name]) else "invalid"
    if row["phase"] != spec.phase or row["delta_class"] != spec.delta_class:
        raise AuditContractError("phase or delta_class does not match the event type")
    if row["actor_kind"] not in spec.actor_kinds or row["surface"] not in spec.surfaces:
        raise AuditContractError("actor_kind or surface not allowed for the event type")
    kind_prefix = {"human": "local:", "system": "system:"}.get(row["actor_kind"])
    if kind_prefix and not row["actor"].startswith(kind_prefix):
        raise AuditContractError("actor does not match actor_kind")
    expected_id = f"op:{row['operation_id']}:{row['phase']}"
    if row["event_id"] != expected_id and not row["event_id"].startswith(expected_id + ":"):
        raise AuditContractError("event_id does not belong to operation_id and phase")
    problem = _generation_problem(spec.generation, row["generation_from"], row["generation_to"])
    if problem is None and spec.rule is not None:
        problem = spec.rule(row)
    if problem is not None:
        raise AuditContractError(problem)
    attributes = {key: value for key, value in row.items() if key != "event_id"}
    return {"event_type": event_type, "event_id": row["event_id"], "attributes": attributes}
