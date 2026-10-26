"""#240: durable HITL approvals, restart + asynchronous resume, fail-closed store.

Every path is offline: a scripted model, a mock external service, real SQLite files.
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest
from test_hitl_binding import (
    CALL,
    Clock,
    ExternalService,
    _agent,
    _app,
    _external_tool,
    _request,
    _results,
    _wait_pending,
)

from stiracore.agent.hitl_resume import resume_approved_request
from stiracore.core import HITLAction, HITLDecision, StiraEngine
from stiracore.core.approval import OperationLedger, args_hash
from stiracore.core.approval_store import (
    APPROVED,
    CANCELLED,
    EDITED,
    EXPIRED,
    PENDING,
    ApprovalStore,
    ApprovalStoreError,
    StoreOutcome,
    is_human_principal,
)
from stiracore.observability.chat_api import DecideOutcome, HITLApprovalBroker

INPUT = {"record": "r1", "value": "v1"}
DIGEST = args_hash(INPUT)


def _submit(store, request_id="req-1", **overrides):
    fields = {
        "request_id": request_id,
        "agent_id": "ext-agent",
        "tool_name": "external_update",
        "tool_input": INPUT,
        "expires_at": 2000.0,
        "tool_use_id": "ext-1",
        "correlation_id": "turn-1",
        "origin": {"kind": "turn", "id": "turn-1"},
        "checkpoint_id": None,
    }
    fields.update(overrides)
    return store.submit(**fields)


# --- store: persistence, binding, idempotency -----------------------------------------


def test_pending_request_survives_reopen_with_owner_only_files(tmp_path):
    path = tmp_path / "approvals.db"
    store = ApprovalStore(path, now_fn=Clock())
    _submit(store)
    store.close()

    reopened = ApprovalStore(path, now_fn=Clock())
    assert reopened.healthy, reopened.error
    (record,) = reopened.pending()
    assert (record.request_id, record.agent_id, record.tool_name) == (
        "req-1",
        "ext-agent",
        "external_update",
    )
    assert (record.args_hash, record.tool_use_id, record.expires_at) == (DIGEST, "ext-1", 2000.0)
    assert record.origin == {"kind": "turn", "id": "turn-1"}
    assert record.correlation_id == "turn-1"
    for file in (path, path.with_name(path.name + ".head")):
        assert stat.S_IMODE(os.stat(file).st_mode) == 0o600


def test_arguments_are_stored_masked_and_hash_bound(tmp_path):
    path = tmp_path / "approvals.db"
    store = ApprovalStore(path, now_fn=Clock())
    secret = {"url": "https://example.test", "api_key": "sk-" + "A1b2C3d4E5f6G7h8"}
    record = _submit(store, tool_input=secret)
    assert (
        record.masked
        and "sk-A1b2" not in record.display
        and record.args_hash == (args_hash(secret))
    )
    store.close()
    raw = b"".join(p.read_bytes() for p in tmp_path.iterdir() if p.is_file())
    assert b"A1b2C3d4E5f6G7h8" not in raw


def test_a_decision_is_recorded_once_and_only_for_the_displayed_arguments(tmp_path):
    store = ApprovalStore(tmp_path / "a.db", now_fn=Clock())
    _submit(store)
    other = args_hash({"record": "r1", "value": "OTHER"})
    assert store.decide("req-1", "approve", args_hash=other, approver="local:operator") is (
        StoreOutcome.BINDING_MISMATCH
    )
    assert store.get("req-1").status == PENDING
    assert store.decide("req-1", "approve", args_hash=DIGEST, approver="local:operator")
    for action in ("approve", "reject"):
        assert store.decide("req-1", action, args_hash=DIGEST, approver="local:operator") is (
            StoreOutcome.ALREADY_DECIDED
        )
    kinds = [event["kind"] for event in store.audit_events()]
    assert kinds.count("decided") == 1
    assert store.consume("req-1") is True
    assert store.consume("req-1") is False, "an approval is handed out at most once"


def test_concurrent_store_instances_hand_out_an_approval_once(tmp_path):
    path = tmp_path / "a.db"
    first = ApprovalStore(path, now_fn=Clock())
    _submit(first)
    assert first.decide("req-1", "approve", args_hash=DIGEST, approver="local:operator")
    stores = [ApprovalStore(path, now_fn=Clock()) for _ in range(6)]
    barrier = threading.Barrier(len(stores))

    def take(store):
        barrier.wait()
        return store.consume("req-1")

    with ThreadPoolExecutor(max_workers=len(stores)) as pool:
        results = list(pool.map(take, stores))
    assert results.count(True) == 1


def test_only_a_human_principal_can_approve_and_no_environment_switch_bypasses_it(
    tmp_path, monkeypatch
):
    for name in ("STIRA_HITL_AUTO_APPROVE", "STIRA_AUTO_APPROVE", "STIRA_HITL_BYPASS"):
        monkeypatch.setenv(name, "1")
    store = ApprovalStore(tmp_path / "a.db", now_fn=Clock())
    _submit(store)
    for approver in ("system", "system:scheduler", "", "local:", "remote:phone", None):
        for action in ("approve", "edit"):
            outcome = store.decide("req-1", action, args_hash=DIGEST, approver=approver)
            assert outcome is StoreOutcome.REFUSED
    assert not is_human_principal("system") and is_human_principal("local:operator")
    assert store.get("req-1").status == PENDING
    # A system rejection is allowed (fail-closed direction).
    assert store.decide("req-1", "reject", approver="system")
    # And a broker never answers by itself: with nobody deciding, the request expires.
    broker = HITLApprovalBroker(
        ttl_sec=0.05, store=ApprovalStore(tmp_path / "b.db"), now_fn=time.time
    )
    decision = broker.request(_request())
    assert decision.action == HITLAction.REJECT and "expired" in decision.reason


def test_expiry_and_cancellation_are_terminal(tmp_path):
    clock = Clock()
    store = ApprovalStore(tmp_path / "a.db", now_fn=clock)
    _submit(store, "late", expires_at=1010.0)
    _submit(store, "cancel-me", tool_use_id="ext-2")
    clock.now = 1011.0
    assert store.expire_due() == ["late"]
    assert store.decide("late", "approve", args_hash=DIGEST, approver="local:operator") is (
        StoreOutcome.EXPIRED
    )
    assert store.cancel("cancel-me", by="local:operator", reason="not needed")
    assert store.get("cancel-me").status == CANCELLED
    assert store.decide("cancel-me", "approve", args_hash=DIGEST, approver="local:operator") is (
        StoreOutcome.ALREADY_DECIDED
    )
    assert store.pending() == []


def test_clock_running_backwards_refuses_writes_without_breaking_the_store(tmp_path):
    clock = Clock()
    store = ApprovalStore(tmp_path / "a.db", now_fn=clock)
    _submit(store)
    clock.now = 900.0
    with pytest.raises(ApprovalStoreError, match="backwards"):
        store.decide("req-1", "approve", args_hash=DIGEST, approver="local:operator")
    clock.now = float("nan")
    with pytest.raises(ApprovalStoreError, match="not usable"):
        store.decide("req-1", "approve", args_hash=DIGEST, approver="local:operator")
    assert store.get("req-1").status == PENDING
    clock.now = 1001.0
    assert store.decide("req-1", "approve", args_hash=DIGEST, approver="local:operator")


# --- store: tamper evidence and corruption --------------------------------------------


def _decided_store(path):
    store = ApprovalStore(path, now_fn=Clock())
    _submit(store, "req-1")
    _submit(store, "req-2", tool_use_id="ext-2")
    store.decide("req-1", "approve", args_hash=DIGEST, approver="local:operator")
    store.decide("req-2", "reject", approver="local:operator", reason="no")
    ok, error = store.verify_chain()
    assert ok, error
    store.close()


def _sql(path, statement, *params):
    conn = sqlite3.connect(path)
    with conn:
        conn.execute(statement, params)
    conn.close()


@pytest.mark.parametrize(
    "tamper",
    [
        # A decision rewritten from "reject" to "approve".
        lambda p: _sql(
            p,
            "UPDATE approval_events SET body = replace(body, '\"reject\"', '\"approve\"')"
            " WHERE kind = 'decided' AND request_id = 'req-2'",
        ),
        # A row removed from the middle.
        lambda p: _sql(p, "DELETE FROM approval_events WHERE seq = 2"),
        # Two rows swapped (reordering).
        lambda p: (
            _sql(p, "UPDATE approval_events SET seq = -1 WHERE seq = 3"),
            _sql(p, "UPDATE approval_events SET seq = 3 WHERE seq = 4"),
            _sql(p, "UPDATE approval_events SET seq = 4 WHERE seq = -1"),
        ),
        # The newest rows deleted (the chain itself still verifies: the head anchor catches it).
        lambda p: _sql(p, "DELETE FROM approval_events WHERE seq >= 3"),
        # The head anchor removed.
        lambda p: p.with_name(p.name + ".head").unlink(),
    ],
    ids=["modified", "deleted", "reordered", "truncated", "head-missing"],
)
def test_tampering_with_the_decision_history_is_detected_and_fails_closed(tmp_path, tamper):
    path = tmp_path / "approvals.db"
    _decided_store(path)
    tamper(path)
    store = ApprovalStore(path, now_fn=Clock())
    assert not store.healthy and store.error
    assert store.verify_chain()[0] is False
    with pytest.raises(ApprovalStoreError):
        store.pending()
    with pytest.raises(ApprovalStoreError):
        _submit(store, "req-3")
    broker = HITLApprovalBroker(store=store, ttl_sec=5)
    decision = broker.request(_request())
    assert decision.action == HITLAction.REJECT
    assert decision.reason == "approval store unavailable"
    assert broker.pending() == [] and broker.store_status()["ok"] is False
    assert broker.decide("req-1", HITLDecision(HITLAction.APPROVE), args_hash=DIGEST) is (
        DecideOutcome.STORE_UNAVAILABLE
    )


def test_a_garbage_store_file_fails_closed_and_is_not_overwritten(tmp_path):
    path = tmp_path / "approvals.db"
    path.write_bytes(b"not a database, just bytes" * 100)
    store = ApprovalStore(path)
    assert not store.healthy
    broker = HITLApprovalBroker(store=store)
    assert broker.request(_request()).action == HITLAction.REJECT
    assert path.read_bytes().startswith(b"not a database")
    client = _app(broker)
    response = client.post("/api/hitl/x/decision", json={"action": "reject"})
    assert (response.status_code, response.json["code"]) == (503, "store_unavailable")
    assert client.get("/api/hitl/pending").json["store"]["ok"] is False


def test_a_modified_edit_payload_is_refused(tmp_path):
    path = tmp_path / "a.db"
    store = ApprovalStore(path, now_fn=Clock())
    _submit(store, "edit-2", tool_input={"record": "r1", "value": "v2"}, store_payload=True)
    assert store.payload("edit-2") == {"record": "r1", "value": "v2"}
    _sql(path, 'UPDATE approval_payloads SET input = \'{"record":"r1","value":"EVIL"}\'')
    with pytest.raises(ApprovalStoreError, match="modified"):
        store.payload("edit-2")
    assert not store.healthy


# --- broker: synchronous behaviour is unchanged ---------------------------------------


def test_sync_request_hitl_without_an_approver_still_rejects():
    engine = StiraEngine()
    decision = engine.request_hitl(_request())
    assert decision.action == HITLAction.REJECT
    assert decision.reason == "no approver is connected"


def _wait_live_pending(broker, timeout=5.0):
    """The request as the *live* worker lists it (no ``restored`` flag).

    Between the worker saving a request to the store and registering it in memory, the
    store already lists it and ``pending()`` reports it as ``restored``: a few
    milliseconds, widened by CPU load (#356). Wait for the registered live entry.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        live = [item for item in broker.pending() if "restored" not in item]
        if live:
            return live[0]
        time.sleep(0.01)
    raise AssertionError("live approval request did not appear")


def test_live_approval_is_recorded_and_consumed(tmp_path):
    store = ApprovalStore(tmp_path / "a.db")
    broker = HITLApprovalBroker(store=store, ttl_sec=5)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(broker.request, _request(agent_id="ext-agent"))
        pending = _wait_live_pending(broker)
        assert broker.decide(
            pending["id"], HITLDecision(HITLAction.APPROVE), args_hash=pending["args_hash"]
        )
        assert future.result(timeout=2).action == HITLAction.APPROVE
    record = store.get(pending["id"])
    assert (record.status, record.consumed, record.approver) == (APPROVED, True, "local:operator")
    assert broker.pending() == []


def test_shutdown_rejects_the_worker_but_keeps_the_request_for_after_restart(tmp_path):
    path = tmp_path / "a.db"
    broker = HITLApprovalBroker(store=ApprovalStore(path), ttl_sec=30)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(broker.request, _request(agent_id="ext-agent"))
        pending = _wait_pending(broker)[0]
        broker.close()
        assert future.result(timeout=2).action == HITLAction.REJECT
    restarted = HITLApprovalBroker(store=ApprovalStore(path), ttl_sec=30)
    (restored,) = restarted.pending()
    assert restored["id"] == pending["id"] and restored["restored"] is True
    assert restored["args_hash"] == pending["args_hash"]
    assert restored["normalized_input"] == pending["normalized_input"]
    response = _app(restarted).get("/api/hitl/pending").json
    assert response["store"]["ok"] is True and response["requests"][0]["restored"] is True


# --- end to end: pause -> restart -> restore -> approve -> resume ---------------------


class Host:
    """One 'process': agent + broker + store + ledger on files under tmp_path."""

    def __init__(self, tmp_path, responses, service, *, clock=None):
        self.store = ApprovalStore(tmp_path / "approvals.db", now_fn=clock)
        self.resumes = []
        self.broker = HITLApprovalBroker(
            ttl_sec=60,
            store=self.store,
            now_fn=clock,
            checkpoint_id_provider=lambda: self.agent.engine.last_checkpoint_id,
            on_resume=lambda request_id: self.resumes.append(
                resume_approved_request(self.agent, self.store, request_id)
            ),
        )
        self.agent = _agent(
            tmp_path,
            responses,
            [_external_tool(service)],
            approver=self.broker.request,
            ledger=OperationLedger(tmp_path / "operations.db"),
        )
        if clock is not None:
            self.agent.engine._now_fn = clock


def _pause_then_shut_down(tmp_path, service, calls=None, approve_first=False):
    """Run a turn that pauses on approval; shut the 'process' down while it waits."""
    calls = calls or [CALL]
    host = Host(tmp_path, [[{"toolUse": c} for c in calls], [{"text": "stopped"}]], service)
    with ThreadPoolExecutor(max_workers=1) as pool:
        turn = pool.submit(host.agent, "update")
        try:
            pending = _wait_pending(host.broker)
            if approve_first:
                (first,) = pending
                assert first["tool_use_id"] == calls[0]["toolUseId"]
                assert host.broker.decide(
                    first["id"], HITLDecision(HITLAction.APPROVE), args_hash=first["args_hash"]
                )
                deadline = time.monotonic() + 2
                while time.monotonic() < deadline:
                    pending = [p for p in host.broker.pending() if p["id"] != first["id"]]
                    if pending:
                        break
                    time.sleep(0.01)
        finally:
            host.broker.close()  # shutdown: the waiting worker is rejected
        turn.result(timeout=5)
    host.store.close()
    return pending


def test_pending_approval_survives_restart_and_approval_resumes_the_paused_call(tmp_path):
    service = ExternalService()
    (paused,) = _pause_then_shut_down(tmp_path, service)
    assert service.applied == [], "nothing runs before approval"

    host = Host(tmp_path, [[{"text": "resumed"}]], service)
    (restored,) = host.broker.pending()
    assert restored["id"] == paused["id"] and restored["restored"] and restored["resumable"]
    client = _app(host.broker)
    url = f"/api/hitl/{restored['id']}/decision"
    response = client.post(url, json={"action": "approve", "args_hash": restored["args_hash"]})
    assert response.status_code == 202
    (outcome,) = host.resumes
    assert outcome.resumed, outcome.reason
    assert str(outcome.result).strip() == "resumed"
    assert [(r, v) for r, v, _key in service.applied] == [("r1", "v1")]
    assert _results(host.agent)["ext-1"]["status"] == "success"

    # Idempotent: a second approval or a second resume never runs it again.
    again = client.post(url, json={"action": "approve", "args_hash": restored["args_hash"]})
    assert (again.status_code, again.json["code"]) == (409, "already_decided")
    second = resume_approved_request(host.agent, host.store, restored["id"])
    assert not second.resumed
    assert len(service.applied) == 1
    record = host.store.get(restored["id"])
    assert (record.consumed, record.resume_claimed, record.approver) == (
        True,
        True,
        "local:operator",
    )
    kinds = [e["kind"] for e in reversed(host.store.audit_events())]
    assert kinds == ["requested", "decided", "resume_claimed", "consumed"]
    assert host.store.verify_chain() == (True, None)
    (entry,) = host.broker.history()
    assert entry["status"] == "approved" and entry["resolver"] == "operator"


def test_resume_does_not_repeat_a_side_effect_committed_before_the_interruption(tmp_path):
    service = ExternalService()
    second = {
        "toolUseId": "ext-2",
        "name": "external_update",
        "input": {"record": "r2", "value": "v2"},
    }
    (paused,) = _pause_then_shut_down(tmp_path, service, calls=[CALL, second], approve_first=True)
    assert [r for r, _v, _k in service.applied] == ["r1"]

    host = Host(tmp_path, [[{"text": "resumed"}]], service)
    (restored,) = host.broker.pending()
    assert restored["id"] == paused["id"] and restored["tool_use_id"] == "ext-2"
    assert host.broker.decide(
        restored["id"], HITLDecision(HITLAction.APPROVE), args_hash=restored["args_hash"]
    )
    assert host.resumes[0].resumed, host.resumes[0].reason
    assert sorted(r for r, _v, _k in service.applied) == ["r1", "r2"], "r1 ran once, r2 once"
    assert "Already applied" in _results(host.agent)["ext-1"]["content"][0]["text"]


@pytest.mark.parametrize("path", ["expired", "cancelled", "rejected", "edited-pending"])
def test_no_execution_before_approval_on_expiry_cancel_reject_or_edit(tmp_path, path):
    service = ExternalService()
    _pause_then_shut_down(tmp_path, service)
    clock = Clock(time.time())
    host = Host(tmp_path, [[{"text": "resumed"}]], service, clock=clock)
    (restored,) = host.broker.pending()
    decide = host.broker.decide
    if path == "expired":
        clock.now = restored["expires_at"] + 1
        outcome = decide(
            restored["id"], HITLDecision(HITLAction.APPROVE), args_hash=restored["args_hash"]
        )
        assert outcome is DecideOutcome.EXPIRED
        assert host.store.get(restored["id"]).status == EXPIRED
    elif path == "cancelled":
        assert host.store.cancel(restored["id"], by="local:operator")
        outcome = decide(
            restored["id"], HITLDecision(HITLAction.APPROVE), args_hash=restored["args_hash"]
        )
        assert outcome is DecideOutcome.ALREADY_DECIDED
    elif path == "rejected":
        assert decide(restored["id"], HITLDecision(HITLAction.REJECT))
    else:
        edited = {"record": "r1", "value": "edited"}
        assert decide(
            restored["id"],
            HITLDecision(HITLAction.EDIT, edited_input=edited),
            args_hash=restored["args_hash"],
        )
        (successor,) = host.broker.pending()
        assert successor["supersedes"] == restored["id"] and successor["restored"]
        assert successor["args_hash"] == args_hash(edited)
        assert host.store.get(restored["id"]).status == EDITED
    assert host.resumes == [] and service.applied == []
    assert host.broker.pending() == [] or path == "edited-pending"


def test_approved_edit_of_a_restored_request_runs_only_the_edited_input_once(tmp_path):
    service = ExternalService()
    _pause_then_shut_down(tmp_path, service)
    host = Host(tmp_path, [[{"text": "resumed"}]], service)
    (restored,) = host.broker.pending()
    edited = {"record": "r1", "value": "edited"}
    assert host.broker.decide(
        restored["id"],
        HITLDecision(HITLAction.EDIT, edited_input=edited),
        args_hash=restored["args_hash"],
    )
    (successor,) = host.broker.pending()
    # The edit itself must be approved, bound to the edited hash.
    stale = host.broker.decide(
        successor["id"], HITLDecision(HITLAction.APPROVE), args_hash=restored["args_hash"]
    )
    assert stale is DecideOutcome.BINDING_MISMATCH and service.applied == []
    assert host.broker.decide(
        successor["id"], HITLDecision(HITLAction.APPROVE), args_hash=successor["args_hash"]
    )
    assert host.resumes[0].resumed, host.resumes[0].reason
    assert [(r, v) for r, v, _k in service.applied] == [("r1", "edited")]
    assert host.store.get(successor["id"]).consumed
    assert host.store.payload(successor["id"]) is None, "the plaintext edit is purged once used"


def test_respond_and_masked_edit_are_refused_for_a_restored_request(tmp_path):
    store = ApprovalStore(tmp_path / "a.db")
    _submit(store, "masked", tool_input={"api_key": "x" * 12}, expires_at=time.time() + 60)
    broker = HITLApprovalBroker(store=store)
    (restored,) = broker.pending()
    assert restored["editable"] is False
    edit = HITLDecision(HITLAction.EDIT, edited_input={"api_key": "***"})
    assert broker.decide("masked", edit, args_hash=restored["args_hash"]) is (
        DecideOutcome.UNSUPPORTED
    )
    respond = HITLDecision(HITLAction.RESPOND, response="hi")
    assert broker.decide("masked", respond, args_hash=restored["args_hash"]) is (
        DecideOutcome.UNSUPPORTED
    )
    assert store.get("masked").status == PENDING


# --- binding on resume ---------------------------------------------------------------


def test_resume_refuses_a_checkpoint_whose_call_was_changed(tmp_path):
    service = ExternalService()
    (paused,) = _pause_then_shut_down(tmp_path, service)
    record = ApprovalStore(tmp_path / "approvals.db").get(paused["id"])
    checkpoint_file = tmp_path / "checkpoints" / f"{record.checkpoint_id}.json"
    checkpoint_file.write_text(
        checkpoint_file.read_text(encoding="utf-8").replace('"v1"', '"EVIL"'), encoding="utf-8"
    )
    host = Host(tmp_path, [[{"text": "resumed"}]], service)
    (restored,) = host.broker.pending()
    assert host.broker.decide(
        restored["id"], HITLDecision(HITLAction.APPROVE), args_hash=restored["args_hash"]
    )
    (outcome,) = host.resumes
    assert not outcome.resumed and "differ" in outcome.reason
    assert service.applied == []
    assert host.store.get(restored["id"]).status == CANCELLED
    kinds = [e["kind"] for e in host.store.audit_events()]
    assert "binding_mismatch" in kinds and "resume_refused" in kinds


def test_resume_refuses_when_the_conversation_moved_on_or_the_checkpoint_is_gone(tmp_path):
    service = ExternalService()
    _pause_then_shut_down(tmp_path, service)
    host = Host(tmp_path, [[{"text": "chatting"}], [{"text": "unused"}]], service)
    host.agent("a newer turn creates a newer checkpoint")
    (restored,) = host.broker.pending()
    assert host.broker.decide(
        restored["id"], HITLDecision(HITLAction.APPROVE), args_hash=restored["args_hash"]
    )
    assert not host.resumes[0].resumed and "moved on" in host.resumes[0].reason
    assert service.applied == []

    service2 = ExternalService()
    other = tmp_path / "other"
    _pause_then_shut_down(other, service2)
    for file in (other / "checkpoints").iterdir():
        file.unlink()
    host2 = Host(other, [[{"text": "resumed"}]], service2)
    (restored2,) = host2.broker.pending()
    assert host2.broker.decide(
        restored2["id"], HITLDecision(HITLAction.APPROVE), args_hash=restored2["args_hash"]
    )
    assert not host2.resumes[0].resumed and "unavailable" in host2.resumes[0].reason
    assert service2.applied == []


def test_an_approval_is_not_reusable_for_other_arguments(tmp_path):
    store = ApprovalStore(tmp_path / "a.db")
    _submit(store, expires_at=time.time() + 60)
    assert store.decide("req-1", "approve", args_hash=DIGEST, approver="local:operator")
    broker = HITLApprovalBroker(store=store, ttl_sec=0.2)
    assert store.claim_resume("req-1")
    # Even inside its own claimed resume, the same call id with different arguments is
    # refused, and the approval is revoked.
    with store.resume_context("req-1"):
        other = broker.request(
            _request({"record": "r1", "value": "OTHER"}, tool_use_id="ext-1", agent_id="ext-agent")
        )
    assert other.action == HITLAction.REJECT and "different arguments" in other.reason
    assert store.get("req-1").status == CANCELLED
    # Another operation never matches the durable approval: it needs its own (none comes).
    store2 = ApprovalStore(tmp_path / "b.db")
    _submit(store2, expires_at=time.time() + 60)
    store2.decide("req-1", "approve", args_hash=DIGEST, approver="local:operator")
    broker2 = HITLApprovalBroker(store=store2, ttl_sec=0.2)
    elsewhere = broker2.request(
        _request(INPUT, tool_name="memory_write", tool_use_id="ext-1", agent_id="ext-agent")
    )
    assert elsewhere.action == HITLAction.REJECT and "expired" in elsewhere.reason
    assert store2.get("req-1").consumed is False


def test_restored_history_is_loaded_from_the_store(tmp_path):
    path = tmp_path / "a.db"
    _decided_store(path)
    broker = HITLApprovalBroker(store=ApprovalStore(path, now_fn=Clock()))
    statuses = sorted(entry["status"] for entry in broker.history())
    assert statuses == ["approved", "rejected"]
    assert json.loads(broker.history()[0]["normalized_input"]) == INPUT


# --- review findings (PR #263): each was first reproduced by a failing test -----------


def _forge_approval(path, request_id="req-1"):
    """Append a correctly chained 'decided/approve' row with SQL only (no head change)."""
    from stiracore.core.approval_store import chain_hash

    conn = sqlite3.connect(path)
    seq, prev_hash, ts = conn.execute(
        "SELECT seq, hash, ts FROM approval_events ORDER BY seq DESC LIMIT 1"
    ).fetchone()
    body = {
        "action": "approve",
        "approver": "local:operator",
        "client_id": None,
        "reason": None,
        "args_hash": DIGEST,
    }
    digest = chain_hash(prev_hash, seq + 1, ts, request_id, "decided", body)
    with conn:
        conn.execute(
            "INSERT INTO approval_events VALUES (?, ?, ?, ?, ?, ?, ?)",
            (seq + 1, ts, request_id, "decided", canonical(body), prev_hash, digest),
        )
    conn.close()


def canonical(value):
    from stiracore.core.approval import canonical_json

    return canonical_json(value)


def test_review4_a_forged_approval_row_without_a_head_update_is_refused(tmp_path):
    path = tmp_path / "a.db"
    store = ApprovalStore(path, now_fn=Clock())
    _submit(store)
    store.close()
    _forge_approval(path)
    reopened = ApprovalStore(path, now_fn=Clock())
    assert not reopened.healthy, "a row the head never covered is tampering, not a crash"
    with pytest.raises(ApprovalStoreError):
        reopened.consume("req-1")


def test_review4_a_crash_between_head_and_commit_loses_only_the_uncommitted_write(tmp_path):
    path = tmp_path / "a.db"
    store = ApprovalStore(path, now_fn=Clock())
    _submit(store)

    def crash(statement):
        raise sqlite3.OperationalError("simulated crash at COMMIT")

    real_execute = store._conn.execute
    store._conn = Mock(wraps=store._conn)
    store._conn.execute = lambda sql, *args: (
        crash(sql) if sql == "COMMIT" else real_execute(sql, *args)
    )
    with pytest.raises(ApprovalStoreError):  # normalized; this instance is now unhealthy
        store.decide("req-1", "approve", args_hash=DIGEST, approver="local:operator")
    assert not store.healthy
    reopened = ApprovalStore(path, now_fn=Clock())
    assert reopened.healthy, reopened.error
    record = reopened.get("req-1")
    assert record.action is None, "the uncommitted approval is gone"
    # Head ahead of the DB cannot be told apart from deleted rows: open requests need a
    # fresh human decision (review round 2, A).
    assert record.status == CANCELLED and "interrupted" in record.resolution_reason


@pytest.mark.parametrize("tamper", ["modify", "delete", "head"])
def test_review3_live_tampering_is_detected_before_an_approval_is_handed_out(tmp_path, tamper):
    path = tmp_path / "a.db"
    store = ApprovalStore(path, now_fn=Clock())
    _submit(store)
    _submit(store, "req-2", tool_use_id="ext-2")
    assert store.decide("req-1", "approve", args_hash=DIGEST, approver="local:operator")
    if tamper == "modify":
        _sql(
            path, "UPDATE approval_events SET body = replace(body, 'ext-2', 'ext-9') WHERE seq = 2"
        )
    elif tamper == "delete":
        _sql(path, "DELETE FROM approval_events WHERE seq = 2")
    else:
        path.with_name(path.name + ".head").unlink()
    with pytest.raises(ApprovalStoreError):
        store.consume("req-1")
    assert not store.healthy


def test_review2_a_stored_approval_is_not_handed_to_another_turn(tmp_path):
    store = ApprovalStore(tmp_path / "a.db")
    _submit(store, expires_at=time.time() + 60, correlation_id="turn-A", checkpoint_id="c" * 32)
    assert store.decide("req-1", "approve", args_hash=DIGEST, approver="local:operator")
    broker = HITLApprovalBroker(store=store, ttl_sec=0.2, turn_id_provider=lambda: "turn-B")
    decision = broker.request(_request(INPUT, tool_use_id="ext-1", agent_id="ext-agent"))
    assert decision.action == HITLAction.REJECT, "turn B must ask a human, not reuse turn A's"
    assert store.get("req-1").consumed is False


def test_review1_free_text_is_masked_and_closed_payloads_leave_no_plaintext(tmp_path):
    path = tmp_path / "a.db"
    clock = Clock()
    store = ApprovalStore(path, now_fn=clock)
    secret = "sk-" + "Zq9Xw8Vu7Ts6Rp5On4Ml"
    _submit(store, "r", reason=f"needs api_key={secret}")
    store.decide("r", "reject", approver="local:operator", reason=f"leaked {secret}")
    # Edit payloads (non-secret: secret ones are never persisted, round 2 C) are purged
    # from the db and the WAL once closed.
    marker = "edited-value-PLAINMARKER-0042"
    for name, status in (("e1", "reject"), ("e2", "expire"), ("e3", "cancel")):
        _submit(
            store,
            name,
            tool_use_id=name,
            tool_input={"record": "r1", "value": marker},
            store_payload=True,
            expires_at=1500.0,
        )
        assert store.payload(name)["value"] == marker
        if status == "reject":
            store.decide(name, "reject", approver="local:operator")
        elif status == "cancel":
            store.cancel(name, by="local:operator", reason=f"cancel {secret}")
    clock.now = 1600.0
    store.expire_due()
    raw = b"".join(p.read_bytes() for p in tmp_path.iterdir() if p.is_file())
    assert secret.encode() not in raw, "no plaintext left in the db, WAL or head"
    # (The marker also appears in the masked display copy, which is not secret.)
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT COUNT(*) FROM approval_payloads").fetchone() == (0,)
    conn.close()
    wal = path.with_name(path.name + "-wal")
    assert not wal.exists() or wal.stat().st_size == 0, "WAL truncated after the purge"


def test_review_major_a_failed_resume_does_not_leave_a_claimed_unused_approval(tmp_path):
    service = ExternalService()
    _pause_then_shut_down(tmp_path, service)
    host = Host(tmp_path, [[{"text": "resumed"}]], service)
    (restored,) = host.broker.pending()

    def broken(_self, _prompt=None):
        raise RuntimeError("model client failed")

    host.agent.__class__ = type("Broken", (host.agent.__class__,), {"__call__": broken})
    assert host.broker.decide(
        restored["id"], HITLDecision(HITLAction.APPROVE), args_hash=restored["args_hash"]
    )
    (outcome,) = host.resumes
    assert not outcome.resumed and "failed" in outcome.reason
    record = host.store.get(restored["id"])
    assert record.status == CANCELLED and record.resume_refused
    assert service.applied == []


def test_review_concurrent_writers_keep_the_head_consistent(tmp_path):
    path = tmp_path / "a.db"
    stores = [ApprovalStore(path) for _ in range(4)]
    barrier = threading.Barrier(len(stores))

    def write(index):
        barrier.wait()
        for n in range(15):
            _submit(stores[index], f"w{index}-{n}", tool_use_id=f"{index}-{n}", expires_at=9e9)

    with ThreadPoolExecutor(max_workers=len(stores)) as pool:
        list(pool.map(write, range(len(stores))))
    reopened = ApprovalStore(path)
    assert reopened.healthy, reopened.error
    assert len(reopened.pending()) == 60


def test_review_decision_api_requires_same_origin_and_csrf(tmp_path, monkeypatch):
    from test_admin_api_permissions import stira_app as _fixture

    app = _fixture.__wrapped__(tmp_path, monkeypatch)
    store = app.extensions["approval_store"]
    _submit(store, "csrf-1", expires_at=time.time() + 60)
    client = app.test_client()
    url = "/api/hitl/csrf-1/decision"
    body = {"action": "approve", "args_hash": DIGEST}
    token = app.config["STIRA_CSRF_TOKEN"]
    no_token = {"X-Test-No-Csrf": "1"}  # the root conftest otherwise attaches the token
    assert client.post(url, json=body, headers=no_token).status_code == 403
    cross = {"X-CSRF-Token": token, "Origin": "https://evil.example"}
    assert client.post(url, json=body, headers=cross).status_code == 403
    fetch = {"X-CSRF-Token": token, "Sec-Fetch-Site": "cross-site"}
    assert client.post(url, json=body, headers=fetch).status_code == 403
    assert store.get("csrf-1").status == PENDING
    same = {"X-CSRF-Token": token, "Origin": "http://localhost"}
    assert client.post(url, json=body, headers=same).status_code == 202
    assert store.get("csrf-1").approver == "local:operator"


def test_review2_a_restored_approval_needs_its_own_claimed_resume_context(tmp_path):
    store = ApprovalStore(tmp_path / "a.db")
    _submit(store, "a", expires_at=time.time() + 60, correlation_id="turn-A")
    _submit(store, "b", tool_use_id="ext-2", expires_at=time.time() + 60, correlation_id="turn-B")
    for request_id in ("a", "b"):
        assert store.decide(request_id, "approve", args_hash=DIGEST, approver="local:operator")
    with store.resume_context("a"):
        assert store.consume("a", resumed=True) is False, "not claimed yet"
    assert store.claim_resume("a") and store.claim_resume("b")
    assert store.consume("a", resumed=True) is False, "claimed, but not resuming in-process"
    with store.resume_context("a"):
        assert store.consume("b", resumed=True) is False, "another request's resume"
        assert store.consume("a", resumed=True) is True
        assert store.consume("a", resumed=True) is False, "at most once"
    assert not store.resuming("a")


# --- review round 2 (PR #263) ---------------------------------------------------------


def test_round2_A_deleting_the_last_cancel_row_does_not_revive_the_approval(tmp_path):
    path = tmp_path / "a.db"
    store = ApprovalStore(path, now_fn=Clock())
    _submit(store)
    assert store.decide("req-1", "approve", args_hash=DIGEST, approver="local:operator")
    assert store.cancel("req-1", by="local:operator", reason="changed my mind")
    store.close()
    _sql(path, "DELETE FROM approval_events WHERE kind = 'cancelled'")
    reopened = ApprovalStore(path, now_fn=Clock())
    if reopened.healthy:
        record = reopened.get("req-1")
        assert record.status == CANCELLED, "a dropped tail must not bring an approval back"
        assert not reopened.claim_resume("req-1")
        assert not reopened.consume("req-1")
        assert "interrupted" in record.resolution_reason
        assert reopened.verify_chain() == (True, None)


def test_round2_A_crash_recovery_requires_a_fresh_human_decision_for_everything_open(tmp_path):
    path = tmp_path / "a.db"
    store = ApprovalStore(path, now_fn=Clock())
    _submit(store, "open")
    _submit(store, "granted", tool_use_id="ext-2")
    _submit(store, "done", tool_use_id="ext-3")
    store.decide("granted", "approve", args_hash=DIGEST, approver="local:operator")
    store.decide("done", "reject", approver="local:operator")
    _submit(store, "last", tool_use_id="ext-4")
    store.close()
    _sql(path, "DELETE FROM approval_events WHERE request_id = 'last'")  # "crash" tail
    reopened = ApprovalStore(path, now_fn=Clock())
    assert reopened.healthy, reopened.error
    assert reopened.get("last") is None
    assert {r: reopened.get(r).status for r in ("open", "granted", "done")} == {
        "open": CANCELLED,
        "granted": CANCELLED,
        "done": "rejected",
    }
    assert reopened.pending() == []


def test_round2_B_a_resume_context_is_not_usable_from_another_thread(tmp_path):
    store = ApprovalStore(tmp_path / "a.db")
    _submit(store, expires_at=time.time() + 60)
    assert store.decide("req-1", "approve", args_hash=DIGEST, approver="local:operator")
    assert store.claim_resume("req-1")
    broker = HITLApprovalBroker(store=store, ttl_sec=0.2, turn_id_provider=lambda: "turn-B")
    results = {}
    with store.resume_context("req-1"):
        thief = threading.Thread(
            target=lambda: results.update(
                decision=broker.request(_request(INPUT, tool_use_id="ext-1", agent_id="ext-agent"))
            )
        )
        thief.start()
        thief.join()
        assert results["decision"].action == HITLAction.REJECT
        assert store.get("req-1").consumed is False
        # The resuming context itself still gets it (the SDK copies contextvars).
        assert store.consume("req-1", resumed=True)


def test_round2_C_secret_edit_arguments_are_never_persisted(tmp_path):
    path = tmp_path / "a.db"
    store = ApprovalStore(path, now_fn=Clock())
    secret = "s" + "k-" + "Yq8Wx7Vt6Us5Rp4Oo3Nm"
    with pytest.raises(ApprovalStoreError, match="secret"):
        _submit(store, "e", tool_input={"value": secret}, store_payload=True)
    raw = b"".join(p.read_bytes() for p in tmp_path.iterdir() if p.is_file())
    assert secret.encode() not in raw


def test_round2_C_a_live_edit_with_secrets_still_works_but_does_not_survive_restart(tmp_path):
    path = tmp_path / "a.db"
    secret = "s" + "k-" + "Yq8Wx7Vt6Us5Rp4Oo3Nm"
    broker = HITLApprovalBroker(store=ApprovalStore(path), ttl_sec=30)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(
            broker.request,
            _request({"value": secret}, agent_id="ext-agent", supersedes="earlier"),
        )
        (pending,) = _wait_pending(broker)
        while not path.exists():
            time.sleep(0.01)
        raw = b"".join(p.read_bytes() for p in tmp_path.iterdir() if p.is_file())
        assert secret.encode() not in raw, "not even while pending"
        broker.close()
        future.result(timeout=2)
    restarted = HITLApprovalBroker(store=ApprovalStore(path), ttl_sec=30)
    assert restarted.pending() == [], "a live-only request is not restored"
    assert restarted._store.get(pending["id"]).status == CANCELLED


def test_round2_D_a_failing_on_replay_callback_revokes_the_claimed_approval(tmp_path):
    service = ExternalService()
    _pause_then_shut_down(tmp_path, service)
    host = Host(tmp_path, [[{"text": "resumed"}]], service)
    (restored,) = host.broker.pending()
    host.store.decide(
        restored["id"], "approve", args_hash=restored["args_hash"], approver="local:operator"
    )

    def boom():
        raise RuntimeError("telemetry down")

    outcome = resume_approved_request(host.agent, host.store, restored["id"], on_replay=boom)
    assert not outcome.resumed
    assert host.store.get(restored["id"]).status == CANCELLED
    assert service.applied == []


def test_round2_D_a_replay_that_never_used_the_approval_revokes_it(tmp_path):
    service = ExternalService()
    _pause_then_shut_down(tmp_path, service)
    host = Host(tmp_path, [[{"text": "resumed"}]], service)
    (restored,) = host.broker.pending()
    host.store.decide(
        restored["id"], "approve", args_hash=restored["args_hash"], approver="local:operator"
    )
    host.agent.state.stability = 0.9  # recovered: no HITL gate, so the approval is not used
    host.agent.engine.config.critical_action = "readonly"
    outcome = resume_approved_request(host.agent, host.store, restored["id"])
    record = host.store.get(restored["id"])
    assert record.consumed or record.status == CANCELLED, outcome


@pytest.mark.parametrize("head", ["not json", "[]", '{"seq": "1", "hash": 5}'])
def test_round2_E_a_malformed_head_fails_closed_and_stays_failed(tmp_path, head):
    path = tmp_path / "a.db"
    head_path = path.with_name(path.name + ".head")
    store = ApprovalStore(path, now_fn=Clock())
    _submit(store)
    good = head_path.read_text()
    head_path.write_text(head)
    with pytest.raises(ApprovalStoreError):
        store.decide("req-1", "approve", args_hash=DIGEST, approver="local:operator")
    head_path.write_text(good)
    assert not store.healthy, "once unhealthy, the instance stays unhealthy"
    with pytest.raises(ApprovalStoreError):
        store.decide("req-1", "approve", args_hash=DIGEST, approver="local:operator")


def test_round2_E_listing_reverifies_the_chain(tmp_path):
    path = tmp_path / "a.db"
    store = ApprovalStore(path, now_fn=Clock())
    _submit(store)
    _sql(path, "UPDATE approval_events SET body = replace(body, 'external_update', 'x')")
    for read in (
        store.pending,
        lambda: store.get("req-1"),
        store.history,
        lambda: store.for_tool_use("ext-agent", "external_update", "ext-1"),
    ):
        with pytest.raises(ApprovalStoreError):
            read()


# --- review round 3 (PR #263) ---------------------------------------------------------


def test_round3_1_a_dropped_table_makes_the_store_unhealthy_for_good(tmp_path):
    path = tmp_path / "a.db"
    store = ApprovalStore(path, now_fn=Clock())
    _submit(store)
    conn = sqlite3.connect(path)
    rows = conn.execute("SELECT * FROM approval_events").fetchall()
    with conn:
        conn.execute("DROP TABLE approval_events")
    with pytest.raises(ApprovalStoreError):
        store.get("req-1")
    assert not store.healthy
    with conn:  # put everything back: the instance must not come back to life
        conn.execute(
            "CREATE TABLE approval_events (seq INTEGER PRIMARY KEY, ts REAL NOT NULL,"
            " request_id TEXT NOT NULL, kind TEXT NOT NULL, body TEXT NOT NULL,"
            " prev_hash TEXT NOT NULL, hash TEXT NOT NULL)"
        )
        conn.executemany("INSERT INTO approval_events VALUES (?, ?, ?, ?, ?, ?, ?)", rows)
    conn.close()
    with pytest.raises(ApprovalStoreError):
        store.decide("req-1", "approve", args_hash=DIGEST, approver="local:operator")
    broker = HITLApprovalBroker(store=store)
    assert broker.request(_request()).action == HITLAction.REJECT


def test_round3_1_a_busy_database_is_transient_not_a_verdict(tmp_path):
    path = tmp_path / "a.db"
    store = ApprovalStore(path, now_fn=Clock())
    _submit(store)
    blocker = sqlite3.connect(path, timeout=0, isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")
    store._conn.execute("PRAGMA busy_timeout = 0")
    with pytest.raises(ApprovalStoreError, match="busy"):
        store.decide("req-1", "approve", args_hash=DIGEST, approver="local:operator")
    blocker.execute("ROLLBACK")
    blocker.close()
    assert store.healthy
    assert store.decide("req-1", "approve", args_hash=DIGEST, approver="local:operator")


def test_round3_2_audit_fields_never_store_secrets(tmp_path):
    path = tmp_path / "a.db"
    store = ApprovalStore(path, now_fn=Clock())
    secret = "s" + "k-" + "Pq7Ow6Nv5Mu4Lt3Ks2Jr"
    _submit(store, "a")
    _submit(store, "b", tool_use_id="ext-2")
    _submit(store, "c", tool_use_id="ext-3")
    assert store.decide(
        "a", "approve", args_hash=DIGEST, approver=f"local:{secret}", client_id=secret
    )
    assert store.decide("b", "reject", approver="local:operator", client_id="tab bad\nid")
    assert store.cancel("c", by=secret)
    raw = b"".join(p.read_bytes() for p in tmp_path.iterdir() if p.is_file())
    assert secret.encode() not in raw and b"tab bad" not in raw
    a, b = store.get("a"), store.get("b")
    assert (a.approver, a.client_id, b.client_id) == ("local:unknown", "unknown", "unknown")
    assert store.get("c").approver == "unknown"
    ok = "tab-1a2b.c3:x@y_z"
    _submit(store, "d", tool_use_id="ext-4")
    store.decide("d", "reject", approver="local:operator", client_id=ok)
    assert store.get("d").client_id == ok


def test_round3_3_a_claimed_approval_that_cannot_be_revoked_quarantines_the_store(tmp_path):
    service = ExternalService()
    _pause_then_shut_down(tmp_path, service)
    clock = Clock(time.time())
    host = Host(tmp_path, [[{"text": "resumed"}]], service, clock=clock)
    (restored,) = host.broker.pending()
    host.store.decide(
        restored["id"], "approve", args_hash=restored["args_hash"], approver="local:operator"
    )

    def rewind():
        clock.now -= 3600  # the store refuses writes: it cannot record the revocation
        raise RuntimeError("callback failed")

    outcome = resume_approved_request(host.agent, host.store, restored["id"], on_replay=rewind)
    assert not outcome.resumed
    assert not host.store.healthy, "an unrevocable claim must stop every further grant"
    clock.now += 3600
    with pytest.raises(ApprovalStoreError):
        host.store.consume(restored["id"])
    assert service.applied == []


def test_round3_4_a_chain_of_edits_resumes_with_the_last_approved_input(tmp_path):
    service = ExternalService()
    _pause_then_shut_down(tmp_path, service)
    host = Host(tmp_path, [[{"text": "resumed"}]], service)
    (a,) = host.broker.pending()
    first = {"record": "r1", "value": "edit-1"}
    second = {"record": "r1", "value": "edit-2"}
    assert host.broker.decide(
        a["id"], HITLDecision(HITLAction.EDIT, edited_input=first), args_hash=a["args_hash"]
    )
    (b,) = host.broker.pending()
    assert host.broker.decide(
        b["id"], HITLDecision(HITLAction.EDIT, edited_input=second), args_hash=b["args_hash"]
    )
    (c,) = host.broker.pending()
    assert c["supersedes"] == b["id"]
    assert host.store.payload(b["id"]) is None, "a superseded edit's payload is purged"
    assert host.broker.decide(c["id"], HITLDecision(HITLAction.APPROVE), args_hash=c["args_hash"])
    (outcome,) = host.resumes
    assert outcome.resumed, outcome.reason
    assert [(r, v) for r, v, _k in service.applied] == [("r1", "edit-2")]
    assert host.store.get(c["id"]).consumed
