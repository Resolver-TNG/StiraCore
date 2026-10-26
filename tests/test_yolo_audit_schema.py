"""ADR 0012 §11 — every ``audit.yolo.*`` row of the field table, validated by the registry."""

from __future__ import annotations

import secrets

import pytest

from stiracore.yolo import audit_schema
from stiracore.yolo.audit_schema import REGISTRY, AuditContractError, build

OP = "1" * 32
INC = "2" * 64
WID = "3" * 32
RID = "4" * 32
TOKEN = "5" * 64
HASH = "6" * 64

_PHASE = {
    "intent": "intent",
    "decision": "decision",
    "outcome": "outcome",
    "transition": "transition",
}


def common(phase, delta, actor_kind="system", surface="system", g_from=0, g_to=0):
    actor = "local:operator" if actor_kind == "human" else "system:yolo"
    return {
        "schema": "audit/1",
        "operation_id": OP,
        "phase": phase,
        "event_id": f"op:{OP}:{phase}",
        "actor_kind": actor_kind,
        "actor": actor,
        "surface": surface,
        "incarnation_ref": INC,
        "key_id": "k1",
        "delta_class": delta,
        "window_id": WID,
        "generation_from": g_from,
        "generation_to": g_to,
        "authority_epoch": 0,
    }


POLICY = {"rules": 3, "epoch": 0, "window": 1, "breaker": "normal"}

EXAMPLES = {
    "audit.yolo.requested": {
        **common("intent", "loosening", "human", "admin"),
        "request_id": RID,
        "tier": "internal",
        "expected": "idle",
        "op_token_ref": TOKEN,
    },
    "audit.yolo.confirmed": {
        **common("decision", "loosening", "human", "cli"),
        "request_id": RID,
        "tier": "external",
        "expected": "pending:0",
        "op_token_ref": TOKEN,
    },
    "audit.yolo.denied": {
        **common("transition", "narrowing", "human", "cli"),
        "request_id": RID,
        "reason_code": "denied",
    },
    "audit.yolo.request_expired": {
        **common("transition", "narrowing"),
        "request_id": RID,
        "reason_code": "request_expired",
    },
    "audit.yolo.request_cancelled": {
        **common("transition", "narrowing", "human", "admin"),
        "request_id": RID,
        "reason_code": "cancel",
    },
    "audit.yolo.request_fenced": {
        **common("transition", "narrowing"),
        "request_id": RID,
        "reason_code": "stop",
    },
    "audit.yolo.started": {
        **common("decision", "loosening", "system", "cli", 0, 1),
        "request_id": RID,
        "tier": "internal",
        "cap": 2000,
        "t0": 1_760_000_000.5,
        "confirmed_ref": f"op:{'7' * 32}:decision",
    },
    "audit.yolo.paused": {
        **common("transition", "narrowing", g_from=1, g_to=2),
        "reason_code": "breaker_warning",
    },
    "audit.yolo.resumed": {
        **common("transition", "loosening", g_from=2, g_to=3),
        "reason_code": "breaker_resume",
        "paused_ref": f"op:{'8' * 32}:transition",
    },
    "audit.yolo.ended": {
        **common("transition", "narrowing", g_from=1, g_to=2),
        "end_reason": "expired_time",
        "end_class": "normal",
        "used": 37,
    },
    "audit.yolo.auto_approved": {
        **common("decision", "loosening", "system", "chat", 1, 1),
        "operation": "file_write",
        "target_kind": "path",
        "target": f"workspace:{HASH}",
        "args_ref": HASH,
        "decision": "auto_approved",
        "authorizer_kind": "yolo",
        "policy": POLICY,
    },
    "audit.yolo.consumed": {
        **common("outcome", "loosening", "system", "chat", 1, 1),
        "intent_ref": "9" * 32,
    },
    "audit.yolo.void": {
        **common("outcome", "narrowing", "system", "chat", 1, 1),
        "intent_ref": "9" * 32,
        "reason_code": "permit_stale",
    },
    "audit.yolo.unlatched": {
        **common("decision", "narrowing", "human", "cli", 2, 2),
        "latch_subject": "window",
        "marker_digest": HASH,
        "chain_seq_seen": 41,
        "expected": f"marker:{HASH}",
        "op_token_ref": TOKEN,
    },
    "audit.yolo.unlatch_applied": {
        **common("outcome", "narrowing", "human", "cli", 2, 2),
        "latch_subject": "window",
        "intent_ref": "9" * 32,
    },
    "audit.yolo.settings_saved": {
        **common("decision", "loosening", "human", "admin"),
        "window_id": "0" * 32,
        "fields": ["allow", "max_external"],
        "changed": ["allow", "max_auto_approvals", "max_external"],
    },
}


def test_every_registered_row_has_an_example_and_the_table_rows_are_registered():
    # ADR 0012 §11 table (incl. the request rows, `request_fenced` and `unlatch_applied`).
    table = {
        "requested",
        "confirmed",
        "denied",
        "request_expired",
        "request_cancelled",
        "request_fenced",
        "started",
        "paused",
        "resumed",
        "ended",
        "auto_approved",
        "consumed",
        "void",
        "unlatched",
        "unlatch_applied",
        "settings_saved",  # phase 7c (PR #444 M1)
    }
    assert set(REGISTRY) == {f"audit.yolo.{name}" for name in table}
    assert set(EXAMPLES) == set(REGISTRY)


@pytest.mark.parametrize("event_type", sorted(EXAMPLES))
def test_valid_row_builds(event_type):
    row = build(event_type, **EXAMPLES[event_type])
    assert row["event_type"] == event_type
    assert row["event_id"] == f"op:{OP}:{EXAMPLES[event_type]['phase']}"
    assert row["attributes"]["schema"] == "audit/1"
    assert "event_id" not in row["attributes"]


def _required(event_type):
    spec = REGISTRY[event_type]
    names = set(audit_schema._COMMON) | set(spec.required)
    if spec.human_op:
        names.add("op_token_ref")
    return sorted(names)


@pytest.mark.parametrize(
    "event_type, name",
    [(event_type, name) for event_type in sorted(EXAMPLES) for name in _required(event_type)],
)
def test_missing_or_none_required_field_is_a_failed_audit(event_type, name):
    fields = dict(EXAMPLES[event_type])
    fields.pop(name)
    with pytest.raises(AuditContractError, match="required field missing"):
        build(event_type, **fields)
    with pytest.raises(AuditContractError):
        build(event_type, **{**EXAMPLES[event_type], name: None})


@pytest.mark.parametrize("event_type", sorted(EXAMPLES))
def test_unknown_field_and_destination_are_refused(event_type):
    with pytest.raises(AuditContractError, match="not in the registry entry"):
        build(event_type, **EXAMPLES[event_type], note="free text")
    # Open (10) is undecided: no row stores a destination (input text).
    with pytest.raises(AuditContractError, match="not in the registry entry"):
        build(event_type, **EXAMPLES[event_type], destination="example.com")


@pytest.mark.parametrize("event_type", sorted(EXAMPLES))
def test_phase_delta_actor_surface_must_match_the_entry(event_type):
    spec = REGISTRY[event_type]
    base = EXAMPLES[event_type]
    other_phase = next(
        p for p in ("decision", "intent", "outcome", "transition") if p != spec.phase
    )
    with pytest.raises(AuditContractError):
        build(event_type, **{**base, "phase": other_phase, "event_id": f"op:{OP}:{other_phase}"})
    other_delta = "narrowing" if spec.delta_class == "loosening" else "loosening"
    with pytest.raises(AuditContractError, match="delta_class"):
        build(event_type, **{**base, "delta_class": other_delta})
    other_actor = next(a for a in ("human", "system", "agent", "job") if a not in spec.actor_kinds)
    with pytest.raises(AuditContractError):
        build(event_type, **{**base, "actor_kind": other_actor})
    other_surface = next(
        s for s in ("chat", "job", "admin", "cli", "system") if s not in spec.surfaces
    )
    with pytest.raises(AuditContractError, match="surface"):
        build(event_type, **{**base, "surface": other_surface})


@pytest.mark.parametrize("event_type", sorted(EXAMPLES))
def test_generation_rule_of_each_row(event_type):
    spec = REGISTRY[event_type]
    base = EXAMPLES[event_type]
    bad = {
        "zero": (0, 1),
        "start": (1, 2),
        "step": (2, 2),
        "same": (1, 2),
        "same0": (2, 3),
    }[spec.generation]
    with pytest.raises(AuditContractError, match="generation"):
        build(event_type, **{**base, "generation_from": bad[0], "generation_to": bad[1]})
    with pytest.raises(AuditContractError):
        build(event_type, **{**base, "generation_from": True})  # a bool is not an int


def test_agent_actor_is_never_allowed_on_a_yolo_row():
    for event_type, spec in REGISTRY.items():
        assert "agent" not in spec.actor_kinds, event_type


def test_human_loosening_rows_need_the_operation_token():
    for event_type in ("audit.yolo.requested", "audit.yolo.confirmed", "audit.yolo.unlatched"):
        assert REGISTRY[event_type].human_op


@pytest.mark.parametrize(
    "field, value",
    [
        ("actor", "local:AKIAIOSFODNN7EXAMPLE"),
        ("request_id", "A" * 32),
        ("window_id", "x" * 32),
        ("operation_id", "1" * 31),
        ("event_id", f"op:{'0' * 32}:intent"),  # another operation's id
        ("incarnation_ref", "short"),
        ("tier", "all"),
        ("expected", "something"),
        ("schema", "audit/2"),
    ],
)
def test_invalid_required_values_refuse(field, value):
    with pytest.raises(AuditContractError):
        build("audit.yolo.requested", **{**EXAMPLES["audit.yolo.requested"], field: value})


def test_actor_must_match_actor_kind():
    with pytest.raises(AuditContractError, match="actor does not match"):
        build(
            "audit.yolo.requested",
            **{**EXAMPLES["audit.yolo.requested"], "actor": "system:yolo"},
        )


def test_ended_class_must_match_the_reason():
    base = EXAMPLES["audit.yolo.ended"]
    with pytest.raises(AuditContractError, match="end_class"):
        build("audit.yolo.ended", **{**base, "end_reason": "revoked_audit_failed"})
    build(
        "audit.yolo.ended",
        **{**base, "end_reason": "revoked_audit_failed", "end_class": "abnormal"},
    )
    with pytest.raises(AuditContractError):
        build("audit.yolo.ended", **{**base, "end_reason": "made_up", "end_class": "abnormal"})


def test_started_cross_field_checks_and_optional_invalid():
    base = EXAMPLES["audit.yolo.started"]
    with pytest.raises(AuditContractError, match="expires_at"):
        build("audit.yolo.started", **{**base, "expires_at": base["t0"] - 1})
    with pytest.raises(AuditContractError, match="external_cap"):
        build("audit.yolo.started", **{**base, "external_cap": base["cap"] + 1})
    row = build("audit.yolo.started", **{**base, "expires_at": "tomorrow"})
    assert row["attributes"]["expires_at"] == "invalid"  # optional: stored as the word
    row = build("audit.yolo.started", **{**base, "expires_at": base["t0"] + 6 * 3600})
    assert row["attributes"]["expires_at"] == base["t0"] + 6 * 3600


def test_dynamic_tool_names_only_as_keyed_hash():
    base = EXAMPLES["audit.yolo.auto_approved"]
    build("audit.yolo.auto_approved", **{**base, "operation": "mcp:" + HASH})
    for operation in ("mcp:server.tool", "My Tool", "mcp:" + "g" * 64):
        with pytest.raises(AuditContractError, match="operation"):
            build("audit.yolo.auto_approved", **{**base, "operation": operation})
    for policy in ({**POLICY, "breaker": "green"}, {"rules": 1}, {**POLICY, "extra": 1}):
        with pytest.raises(AuditContractError, match="policy"):
            build("audit.yolo.auto_approved", **{**base, "policy": policy})


@pytest.mark.parametrize(
    "target_kind, target",
    [
        ("path", "payroll"),  # a relative file name: shape-valid code before, never now
        ("path", "notes.txt"),
        ("path", "workspace"),
        ("path", "none"),
        ("path", f"secrets:{HASH}"),  # not a root label
        ("path", f"workspace:{'G' * 64}"),
        ("tool", "file_write"),
        ("none", f"workspace:{HASH}"),
        ("window", "none"),  # not a tool-row target kind
        ("setting", "none"),
    ],
)
def test_tool_target_is_a_code_allow_list_never_plain_text(target_kind, target):
    base = EXAMPLES["audit.yolo.auto_approved"]
    with pytest.raises(AuditContractError):
        build("audit.yolo.auto_approved", **{**base, "target_kind": target_kind, "target": target})


@pytest.mark.parametrize(
    "target_kind, target",
    [("path", f"{label}:{HASH}") for label in sorted(audit_schema.PATH_LABELS)]
    + [("tool", "none"), ("none", "none")],
)
def test_allowed_targets(target_kind, target):
    base = EXAMPLES["audit.yolo.auto_approved"]
    build("audit.yolo.auto_approved", **{**base, "target_kind": target_kind, "target": target})


@pytest.mark.parametrize("operation", ["payroll", "my_tool", "file_writer", "mcp_server_tool"])
def test_operation_is_a_registry_allow_list(operation):
    base = EXAMPLES["audit.yolo.auto_approved"]
    with pytest.raises(AuditContractError, match="operation"):
        build("audit.yolo.auto_approved", **{**base, "operation": operation})


def test_every_registered_tool_is_in_the_allow_list():
    from stiracore.tools import tool_registry

    registered = set(tool_registry.READ_TOOLS) | set(tool_registry.WRITE_TOOLS)
    registered |= set(tool_registry.EXEC_TOOL_NAMES)
    assert registered <= audit_schema.KNOWN_TOOLS, sorted(registered - audit_schema.KNOWN_TOOLS)
    for name in ("memory_read", "memory_write", "memory_search", "memory_list"):
        assert name in audit_schema.KNOWN_TOOLS


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1, True, "1"])
def test_time_values_must_be_finite_numbers(value):
    base = EXAMPLES["audit.yolo.started"]
    with pytest.raises(AuditContractError):
        build("audit.yolo.started", **{**base, "t0": value})


@pytest.mark.parametrize(
    "changes",
    [
        {"fields": []},
        {"fields": ["max_external", "allow"]},  # not sorted
        {"fields": ["allow", "allow"]},
        {"fields": ["unknown"]},
        {"fields": "allow"},
        {"fields": [{"allow": "external"}]},  # a value never fits
        {"fields": ["host_confirm"]},  # loosened but not changed
        {"window_id": "3" * 32},  # a settings row is about no window
    ],
)
def test_the_settings_row_carries_sorted_field_names_only(changes):
    with pytest.raises(AuditContractError):
        build("audit.yolo.settings_saved", **{**EXAMPLES["audit.yolo.settings_saved"], **changes})


def test_unregistered_event_type_is_refused():
    with pytest.raises(AuditContractError, match="not in the registry"):
        build("audit.yolo.extended", **EXAMPLES["audit.yolo.started"])


def test_record_yolo_writes_one_chained_row_and_nothing_on_a_contract_break(tmp_path):
    from stiracore.observability.audit_chain import AuditChain
    from stiracore.observability.event_store import SQLiteEventStore

    store = SQLiteEventStore(tmp_path / "events.db")
    try:
        chain = AuditChain(store)
        operation = secrets.token_hex(16)
        fields = {
            **EXAMPLES["audit.yolo.requested"],
            "operation_id": operation,
            "event_id": f"op:{operation}:intent",
        }
        event_id = chain.record_yolo("audit.yolo.requested", **fields)
        assert event_id == f"op:{operation}:intent"
        rows = store.get_events(event_type="audit.yolo.requested")
        assert len(rows) == 1
        assert rows[0].message == "" and rows[0].usage is None
        assert rows[0].attributes["tier"] == "internal"
        with pytest.raises(AuditContractError):
            chain.record_yolo("audit.yolo.requested", **{**fields, "tier": "all"})
        # The deterministic event id: a second write of the same step adds no row.
        chain.record_yolo("audit.yolo.requested", **fields)
        assert len(store.get_events(event_type="audit.yolo.requested")) == 1
        ok, _ = store.verify_chain()
        assert ok
    finally:
        store.close()


def test_an_external_window_ended_row_with_external_used_passes_the_real_schema():
    """Phase 6 (PR #438 review): ``external_used`` is an optional field of ``ended`` — an
    external tier's closing row is recorded, not refused. The chain is the real registry
    (not a fake): a field the entry does not list is still refused, and the count is
    bounded like the others."""
    base = EXAMPLES["audit.yolo.ended"]
    assert "external_used" in REGISTRY["audit.yolo.ended"].optional
    row = build("audit.yolo.ended", **{**base, "external_used": 5})
    assert row["attributes"]["external_used"] == 5
    build("audit.yolo.ended", **{**base, "external_used": 0})
    # An optional field with a bad value is stored as the word ``invalid`` (never refused);
    # a field the entry does not list is refused — so the check above is not vacuous.
    bad = build("audit.yolo.ended", **{**base, "external_used": -1})
    assert bad["attributes"]["external_used"] == "invalid"
    with pytest.raises(AuditContractError):
        build("audit.yolo.ended", **{**base, "external_total": 5})
