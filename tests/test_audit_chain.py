"""#171: audit adapters (approval / tool execution / procedural write) and mirroring.

The adapters only add evidence; the approval store stays the authority (ADR 0006).
"""

from __future__ import annotations

import json
import sqlite3

import pytest
from test_hitl_binding import Clock

from stiracore.core.approval_store import ApprovalStore
from stiracore.observability.audit_chain import AuditChain, audit_digest, audit_id
from stiracore.observability.event_chain import EventChainError
from stiracore.observability.event_store import SQLiteEventStore

SECRET = "sk-" + "Zq9Xw8Vu7Ts6Rp5O"
DIGEST = "ab" * 32


def _chain(tmp_path):
    store = SQLiteEventStore(tmp_path / "audit.db", namespace="audit")
    return store, AuditChain(store)


def _submit(store, request_id="req-1", tool_input=None):
    return store.submit(
        request_id=request_id,
        agent_id="agent-1",
        tool_name="external_update",
        tool_input=tool_input or {"record": "r1"},
        expires_at=2000.0,
        tool_use_id="use-1",
        correlation_id="turn-1",
        origin={"kind": "turn", "id": "turn-1"},
    )


def test_identifiers_and_hashes_are_sanitized():
    assert audit_id("tool_use-1:a@b") == "tool_use-1:a@b"
    assert audit_id("has space") == "unknown"
    assert audit_id(SECRET) == "unknown"
    assert audit_id("x" * 65) == "unknown"
    assert audit_id(None) is None
    assert audit_digest(DIGEST) == DIGEST
    assert audit_digest("AB" * 32) == "invalid"
    assert audit_digest(SECRET) == "invalid"


def test_tool_and_procedural_events_carry_ids_and_hashes_only(tmp_path):
    store, chain = _chain(tmp_path)
    chain.record_tool_execution(
        "started", tool_name="file_editor", tool_use_id="use-1", args_hash=DIGEST, turn_id="t1"
    )
    chain.record_tool_execution(
        "committed", tool_name="file_editor", tool_use_id=SECRET, args_hash=SECRET
    )
    chain.record_procedural_write(
        "written",
        entry_key="skill.deploy",
        content_hash=DIGEST,
        source_turn="t1",
        approved_by="local:resolver",
        request_id="req-9",
    )
    started, committed, written = store.get_events()
    assert (started.event_type, started.tool_name, started.tool_use_id, started.turn_id) == (
        "audit.tool.started",
        "file_editor",
        "use-1",
        "t1",
    )
    assert started.attributes == {"args_hash": DIGEST}
    assert committed.tool_use_id == "unknown"
    assert committed.attributes == {"args_hash": "invalid"}
    assert written.event_type == "audit.procedural.written"
    assert written.attributes == {
        "entry_key": "skill.deploy",
        "content_hash": DIGEST,
        "source_turn": "t1",
        "approved_by": "local:resolver",
        "request_id": "req-9",
    }
    assert store.verify_chain() == (True, None)
    store.close()
    assert SECRET.encode() not in (tmp_path / "audit.db").read_bytes()


def test_unknown_kinds_are_refused(tmp_path):
    _store, chain = _chain(tmp_path)
    with pytest.raises(ValueError):
        chain.record_tool_execution("executed", tool_name="t", tool_use_id=None, args_hash=None)
    with pytest.raises(ValueError):
        chain.record_procedural_write("auto", entry_key=None, content_hash=None, source_turn=None)
    with pytest.raises(ValueError):
        chain.record_approval("approved_by_system", request_id="r")


def test_unhealthy_chain_refuses_audit_events(tmp_path):
    store, chain = _chain(tmp_path)
    chain.record_tool_execution("started", tool_name="t", tool_use_id="u", args_hash=DIGEST)
    conn = sqlite3.connect(tmp_path / "audit.db")
    with conn:
        conn.execute("UPDATE events SET tool_name = 'other'")
    conn.close()
    assert store.verify_chain()[0] is False
    with pytest.raises(EventChainError):
        chain.record_tool_execution("committed", tool_name="t", tool_use_id="u", args_hash=DIGEST)


def test_approval_store_is_mirrored_once_without_arguments(tmp_path):
    approvals = ApprovalStore(tmp_path / "approvals.db", now_fn=Clock())
    record = _submit(approvals, tool_input={"record": "r1", "api_key": SECRET})
    approvals.decide(
        "req-1",
        "approve",
        args_hash=record.args_hash,
        approver="local:resolver",
        reason=f"ok {SECRET}",
    )
    store, chain = _chain(tmp_path)
    result = chain.mirror_approval_store(approvals)
    assert (result.ok, result.mirrored) == (True, 2)
    requested, decided = store.get_events()
    assert requested.event_type == "audit.approval.requested"
    assert requested.tool_name == "external_update"
    assert requested.attributes["args_hash"] == record.args_hash
    assert requested.attributes["source_seq"] == 1
    assert decided.attributes["action"] == "approve"
    assert decided.attributes["principal"] == "local:resolver"
    # Mirroring again adds nothing (deterministic event ids).
    assert chain.mirror_approval_store(approvals).mirrored == 0
    _submit(approvals, request_id="req-2")
    assert chain.mirror_approval_store(approvals).mirrored == 1
    assert store.verify_chain() == (True, None)
    store.close()
    stored = (tmp_path / "audit.db").read_bytes()
    assert SECRET.encode() not in stored and b"r1" not in stored


def test_unverified_approval_store_is_not_mirrored(tmp_path):
    approvals = ApprovalStore(tmp_path / "approvals.db", now_fn=Clock())
    _submit(approvals)
    approvals.close()
    conn = sqlite3.connect(tmp_path / "approvals.db")
    with conn:
        conn.execute("UPDATE approval_events SET kind = 'decided'")
    conn.close()
    store, chain = _chain(tmp_path)
    result = chain.mirror_approval_store(ApprovalStore(tmp_path / "approvals.db", now_fn=Clock()))
    assert (result.ok, result.mirrored) == (False, 0)
    (event,) = store.get_events()
    assert event.event_type == "audit.approval.source_unverified"


def test_consistently_rebuilt_approval_store_is_caught_by_the_mirror(tmp_path):
    """ADR 0006's limitation: db + head rewritten together verify on their own."""
    path = tmp_path / "approvals.db"
    approvals = ApprovalStore(path, now_fn=Clock())
    _submit(approvals, request_id="req-1")
    store, chain = _chain(tmp_path)
    assert chain.mirror_approval_store(approvals).mirrored == 1
    approvals.close()
    path.unlink()
    path.with_name(path.name + ".head").unlink()
    rebuilt = ApprovalStore(path, now_fn=Clock())
    _submit(rebuilt, request_id="req-forged")
    assert rebuilt.verify_chain() == (True, None)
    result = chain.mirror_approval_store(rebuilt)
    assert (result.ok, result.mirrored) == (False, 0)
    assert result.error == "approval event 1 changed after it was mirrored"
    assert store.get_events()[-1].event_type == "audit.approval.source_diverged"
    assert json.dumps(store.get_events()[-1].attributes) == json.dumps(
        {"reason": "approval event 1 changed after it was mirrored"}
    )
