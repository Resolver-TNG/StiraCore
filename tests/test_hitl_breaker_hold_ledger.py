"""ADR 0024 phase 1b (#240): breaker hold of pending approvals, ledger for ungated writes
and ``delegate`` on replay, closed origin forms, bounded chain walks.

Offline: a scripted model, mock tools, real SQLite files.
"""

from __future__ import annotations

import contextlib
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from unittest.mock import Mock

import pytest
from strands import ToolContext, tool
from test_hitl_binding import ExternalService, _app, _external_tool, _request, _results
from test_subagent import ScriptedModel

from stiracore.agent import checkpoint_hooks
from stiracore.agent.hitl_resume import (
    BREAKER_STRICTER,
    check_resumable,
    replay_preview,
    resume_approved_request,
)
from stiracore.agent.nxp_agent import NXPAgent
from stiracore.core import (
    BreakLevel,
    FileCheckpointStore,
    HITLAction,
    HITLDecision,
    NeuralState,
    StiraConfig,
    hitl_reasons,
)
from stiracore.core.approval import (
    LEDGER_COMMITTED,
    LEDGER_STARTED,
    OperationLedger,
    args_hash,
    operation_key,
)
from stiracore.core.approval_store import (
    APPROVED,
    CANCELLED,
    EXPIRED,
    MAX_CHAIN_LINKS,
    PENDING,
    ApprovalStore,
    ApprovalStoreError,
    normalize_origin,
)
from stiracore.observability.chat_api import DecideOutcome, HITLApprovalBroker
from stiracore.tools.delegate_tool import DELEGATE_TOOL_NAME

INPUT = {"record": "r1", "value": "v1"}
DIGEST = args_hash(INPUT)
TURN = {"kind": "turn", "id": "turn-1"}
JOB = {
    "kind": "job",
    "job_id": "job-1",
    "run_id": "run-1",
    "generation": 3,
    "incarnation": "0123456789abcdef0123456789abcdef",
}


def _submit(store, request_id="req-1", **overrides):
    fields = {
        "request_id": request_id,
        "agent_id": "ext-agent",
        "tool_name": "external_update",
        "tool_input": INPUT,
        "expires_at": time.time() + 60,
        "tool_use_id": "ext-1",
        "correlation_id": "turn-1",
        "origin": TURN,
        "break_level": "normal",
    }
    fields.update(overrides)
    return store.submit(**fields)


class Level:
    """A breaker reader the test moves (a string, or an exception to raise)."""

    def __init__(self, value="normal"):
        self.value = value

    def __call__(self):
        if isinstance(self.value, Exception):
            raise self.value
        return BreakLevel(self.value)


# --- §2 origin forms -------------------------------------------------------------------


def test_only_the_closed_origin_forms_are_accepted(tmp_path):
    assert normalize_origin(None) == {} and normalize_origin({}) == {}
    assert normalize_origin(TURN) == TURN
    assert normalize_origin(JOB) == JOB
    refused = [
        "turn",
        {"kind": "turn"},
        {"kind": "turn", "id": "t", "extra": 1},
        {"kind": "turn", "id": "has space"},
        {"kind": "turn", "id": "x" * 129},
        {"kind": "scheduler", "id": "t"},
        {**JOB, "generation": True},
        {**JOB, "generation": -1},
        {**JOB, "incarnation": "ABCDEF0123456789ABCDEF0123456789"},
        {**JOB, "incarnation": "00"},
        {**JOB, "note": "x"},
        {k: v for k, v in JOB.items() if k != "run_id"},
    ]
    for origin in refused:
        with pytest.raises(ValueError):
            normalize_origin(origin)
    store = ApprovalStore(tmp_path / "a.db")
    for index, origin in enumerate(refused[1:]):
        with pytest.raises(ApprovalStoreError):
            _submit(store, f"bad-{index}", origin=origin)
    with pytest.raises(ApprovalStoreError):
        _submit(store, "job-other-correlation", origin=JOB, correlation_id="turn-1")
    with pytest.raises(ApprovalStoreError):
        _submit(store, "bad-level", break_level="panic")
    _submit(store, "job-ok", origin=JOB, correlation_id="run-1")
    _submit(store, "legacy", origin={}, correlation_id=None)
    assert store.get("job-ok").origin == JOB and store.get("legacy").origin == {}
    assert [r.request_id for r in store.pending()] == ["job-ok", "legacy"]
    assert store.verify_chain() == (True, None)


def test_a_pending_job_origin_request_is_cancelled_at_start(tmp_path):
    store = ApprovalStore(tmp_path / "a.db")
    _submit(store, "job-req", origin=JOB, correlation_id="run-1")
    _submit(store, "turn-req", tool_use_id="ext-2")
    broker = HITLApprovalBroker(store=store, ttl_sec=30)
    assert [p["id"] for p in broker.pending()] == ["turn-req"]
    record = store.get("job-req")
    assert (record.status, record.resolution_reason) == (CANCELLED, hitl_reasons.JOB_RESTART)
    assert hitl_reasons.resolution_code(record.resolution_reason, "system") == "job_restart"


# --- §3 breaker hold -------------------------------------------------------------------


@pytest.mark.parametrize("current", ["warning", "coherence", "critical"])
def test_a_restored_request_is_held_while_the_breaker_is_stricter(tmp_path, current):
    store = ApprovalStore(tmp_path / "a.db")
    record = _submit(store, break_level="normal")
    level = Level(current)
    resumes = []
    broker = HITLApprovalBroker(
        store=store, ttl_sec=30, breaker_level=level, on_resume=resumes.append
    )
    (listed,) = broker.pending()
    assert listed["restored"] and listed["breaker_hold"] is True
    before = [e["kind"] for e in store.audit_events()]
    approve = HITLDecision(HITLAction.APPROVE)
    assert broker.decide("req-1", approve, args_hash=DIGEST) is DecideOutcome.BREAKER
    edit = HITLDecision(HITLAction.EDIT, edited_input={"record": "r1", "value": "v2"})
    assert broker.decide("req-1", edit, args_hash=DIGEST) is DecideOutcome.BREAKER
    response = _app(broker).post(
        "/api/hitl/req-1/decision", json={"action": "approve", "args_hash": DIGEST}
    )
    assert (response.status_code, response.json["code"]) == (409, "breaker")
    # Nothing recorded, still pending, expiry unchanged, nothing resumed.
    after = store.get("req-1")
    assert (after.status, after.expires_at) == (PENDING, record.expires_at)
    assert [e["kind"] for e in store.audit_events()] == before and resumes == []
    # Recovery: the same card can be approved.
    level.value = "normal"
    (listed,) = broker.pending()
    assert listed["breaker_hold"] is False
    assert broker.decide("req-1", approve, args_hash=DIGEST) is DecideOutcome.ACCEPTED
    assert store.get("req-1").status == APPROVED and resumes == ["req-1"]


def test_reject_is_never_held(tmp_path):
    store = ApprovalStore(tmp_path / "a.db")
    _submit(store, break_level="normal")
    broker = HITLApprovalBroker(store=store, ttl_sec=30, breaker_level=Level("critical"))
    assert broker.decide("req-1", HITLDecision(HITLAction.REJECT)) is DecideOutcome.ACCEPTED
    assert store.get("req-1").status == "rejected"


def test_a_request_raised_under_critical_halt_stays_decidable_at_critical(tmp_path):
    store = ApprovalStore(tmp_path / "a.db")
    _submit(store, break_level="critical")
    broker = HITLApprovalBroker(store=store, ttl_sec=30, breaker_level=Level("critical"))
    assert broker.pending()[0]["breaker_hold"] is False
    approve = HITLDecision(HITLAction.APPROVE)
    assert broker.decide("req-1", approve, args_hash=DIGEST) is DecideOutcome.ACCEPTED


@pytest.mark.parametrize("reader", [RuntimeError("no engine"), "nonsense"])
def test_an_unreadable_breaker_counts_as_critical(tmp_path, reader):
    store = ApprovalStore(tmp_path / "a.db")
    _submit(store, "warn", break_level="warning")
    _submit(store, "crit", tool_use_id="ext-2", break_level="critical")
    broker = HITLApprovalBroker(
        store=store,
        ttl_sec=30,
        breaker_level=(lambda: reader) if isinstance(reader, str) else Level(reader),
    )
    holds = {p["id"]: p["breaker_hold"] for p in broker.pending()}
    assert holds == {"warn": True, "crit": False}
    approve = HITLDecision(HITLAction.APPROVE)
    assert broker.decide("warn", approve, args_hash=DIGEST) is DecideOutcome.BREAKER


def test_a_request_without_a_level_is_held_at_every_stricter_level(tmp_path):
    store = ApprovalStore(tmp_path / "a.db")
    _submit(store, break_level=None)
    broker = HITLApprovalBroker(store=store, ttl_sec=30, breaker_level=Level("warning"))
    approve = HITLDecision(HITLAction.APPROVE)
    assert broker.decide("req-1", approve, args_hash=DIGEST) is DecideOutcome.BREAKER


def test_a_held_request_still_expires_at_its_own_deadline(tmp_path):
    now = [1000.0]
    store = ApprovalStore(tmp_path / "a.db", now_fn=lambda: now[0])
    _submit(store, expires_at=1010.0)
    broker = HITLApprovalBroker(
        store=store, ttl_sec=30, now_fn=lambda: now[0], breaker_level=Level("critical")
    )
    approve = HITLDecision(HITLAction.APPROVE)
    assert broker.decide("req-1", approve, args_hash=DIGEST) is DecideOutcome.BREAKER
    now[0] = 1011.0
    assert broker.pending() == []
    assert store.get("req-1").status == EXPIRED


def test_a_live_request_is_held_too_and_its_worker_keeps_waiting(tmp_path):
    store = ApprovalStore(tmp_path / "a.db")
    level = Level("normal")
    broker = HITLApprovalBroker(store=store, ttl_sec=5, breaker_level=level)
    with ThreadPoolExecutor(max_workers=1) as pool:
        request = replace(_request(agent_id="ext-agent"), break_level=BreakLevel.NORMAL)
        future = pool.submit(broker.request, request)
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            live = [p for p in broker.pending() if not p.get("restored")]
            if live:
                break
            time.sleep(0.01)
        (pending,) = live
        level.value = "warning"
        approve = HITLDecision(HITLAction.APPROVE)
        digest = pending["args_hash"]
        assert broker.decide(pending["id"], approve, args_hash=digest) is DecideOutcome.BREAKER
        assert not future.done()
        level.value = "normal"
        assert broker.decide(pending["id"], approve, args_hash=digest) is DecideOutcome.ACCEPTED
        assert future.result(timeout=2).action == HITLAction.APPROVE


def test_without_a_breaker_reader_nothing_is_held(tmp_path):
    store = ApprovalStore(tmp_path / "a.db")
    _submit(store)
    broker = HITLApprovalBroker(store=store, ttl_sec=30)
    assert "breaker_hold" not in broker.pending()[0]
    approve = HITLDecision(HITLAction.APPROVE)
    assert broker.decide("req-1", approve, args_hash=DIGEST) is DecideOutcome.ACCEPTED


def test_the_resume_claim_refuses_a_breaker_that_rose_after_the_decision(tmp_path):
    store = ApprovalStore(tmp_path / "a.db")
    _submit(store, break_level="normal")
    assert store.decide("req-1", "approve", args_hash=DIGEST, approver="local:operator")
    assert not store.claim_resume("req-1", breaker_level=Level("warning"))
    record = store.get("req-1")
    assert (record.status, record.resume_claimed, record.resume_refused) == (
        CANCELLED,
        False,
        "breaker",
    )
    assert record.resolution_reason == hitl_reasons.BREAKER_STRICTER
    refused, cancelled = [e for e in store.audit_events() if e["seq"] > 2][::-1]
    assert refused["kind"] == "resume_refused" and cancelled["kind"] == "cancelled"
    # Levels only: no arguments, no display text.
    assert refused["body"] == {"reason": "breaker", "level": "warning", "requested_level": "normal"}
    assert not store.claim_resume("req-1"), "revoked for good"


def test_the_resume_claim_with_an_unreadable_breaker_refuses(tmp_path):
    store = ApprovalStore(tmp_path / "a.db")
    _submit(store, break_level="warning")
    assert store.decide("req-1", "approve", args_hash=DIGEST, approver="local:operator")
    assert not store.claim_resume("req-1", breaker_level=Level(RuntimeError("x")))
    body = next(e for e in store.audit_events() if e["kind"] == "resume_refused")["body"]
    assert body["level"] == "unknown"


def test_the_resume_claim_at_the_same_or_a_calmer_level_is_claimed(tmp_path):
    store = ApprovalStore(tmp_path / "a.db")
    _submit(store, "a", break_level="critical")
    _submit(store, "b", tool_use_id="ext-2", break_level="warning")
    for request_id in ("a", "b"):
        assert store.decide(request_id, "approve", args_hash=DIGEST, approver="local:operator")
    assert store.claim_resume("a", breaker_level=Level("critical"))
    assert store.claim_resume("b", breaker_level=Level("coherence"))


# --- §7 fail-closed additions ----------------------------------------------------------


def test_audit_events_re_verify_the_chain(tmp_path):
    store = ApprovalStore(tmp_path / "a.db")
    _submit(store)
    assert [e["kind"] for e in store.audit_events()] == ["requested"]
    with sqlite3.connect(tmp_path / "a.db") as conn:
        conn.execute("UPDATE approval_events SET body = replace(body, 'ext-agent', 'evil')")
    with pytest.raises(ApprovalStoreError):
        store.audit_events()
    assert not store.healthy


def test_an_edit_chain_longer_than_the_limit_is_not_followed(tmp_path):
    store = ApprovalStore(tmp_path / "a.db")
    _submit(store, "e0", checkpoint_id="c" * 32)
    for index in range(1, MAX_CHAIN_LINKS + 2):
        _submit(store, f"e{index}", supersedes=f"e{index - 1}", checkpoint_id="c" * 32)
    assert store.chain_root(f"e{MAX_CHAIN_LINKS}").request_id == "e0"
    assert store.chain_root(f"e{MAX_CHAIN_LINKS + 1}") is None
    agent = Mock(agent_id="ext-agent")
    refusal = check_resumable(agent, store, f"e{MAX_CHAIN_LINKS + 1}")
    assert refusal == "the edit chain is too long to resume"
    with pytest.raises(ApprovalStoreError), store.resume_context(f"e{MAX_CHAIN_LINKS + 1}"):
        pass


# --- end to end: a paused batch, a restart, a resume -----------------------------------


def _note_tool(service):
    @tool(context=True)
    def external_note(record: str, value: str, tool_context: ToolContext) -> str:
        """Write a note in the external system (rule Always)."""
        service.apply(record, value, tool_context.tool_use["toolUseId"])
        return f"noted {record}={value}"

    return external_note


def _lookup_tool(reads):
    @tool
    def lookup(record: str) -> str:
        """Read one record (read-only)."""
        reads.append(record)
        return f"read {record}"

    return lookup


def _delegate_tool(children):
    @tool(name=DELEGATE_TOOL_NAME)
    def delegate(task: str) -> str:
        """Run a child agent (stand-in: counts the children it starts)."""
        children.append(task)
        return "child done"

    return delegate


def _policy(name):
    return "ask" if name == "external_update" else "always"


class Host:
    """One 'process' at NORMAL: an Ask-rule write pauses the batch on its card."""

    def __init__(self, tmp_path, responses, tools, *, stability=0.9, clock=None):
        self.store = ApprovalStore(tmp_path / "approvals.db", now_fn=clock)
        self.resumes = []
        self.scheduled = []
        self.broker = HITLApprovalBroker(
            ttl_sec=60,
            store=self.store,
            now_fn=clock,
            checkpoint_id_provider=lambda: self.agent.engine.last_checkpoint_id,
            on_resume=self.scheduled.append,
            breaker_level=lambda: self.agent.engine.breaker_status().level,
            replay_preview=lambda record: replay_preview(self.agent, record),
        )
        config = StiraConfig(critical_action="halt")
        config.blocked_tool_names = set(config.blocked_tool_names) | {
            "external_update",
            "external_note",
        }
        self.agent = NXPAgent(
            model=ScriptedModel(responses),
            state=NeuralState(stability=stability),
            config=config,
            on_hitl_request=self.broker.request,
            tools=tools,
            agent_id="ext-agent",
            checkpoint_store=FileCheckpointStore(tmp_path / "checkpoints"),
            operation_ledger=OperationLedger(tmp_path / "operations.db"),
            tool_policy=_policy,
            stimulus_classifier=Mock(classify=lambda **kwargs: None),
        )

    def resume(self, request_id):
        outcome = resume_approved_request(self.agent, self.store, request_id)
        self.resumes.append(outcome)
        return outcome


PAUSED = {"toolUseId": "ext-9", "name": "external_update", "input": INPUT}


def _run_until_paused(tmp_path, tools, calls):
    host = Host(tmp_path, [[{"toolUse": c} for c in calls], [{"text": "stopped"}]], tools)
    with ThreadPoolExecutor(max_workers=1) as pool:
        turn = pool.submit(host.agent, "go")
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not host.broker.pending():
                time.sleep(0.01)
            (pending,) = host.broker.pending()
            assert pending["tool_use_id"] == "ext-9"
            # Every ledgered call of the batch has finished before the restart.
            recorded = len([c for c in calls if c["name"] in ("external_note", "delegate")])
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and len(_done(host)) < recorded:
                time.sleep(0.01)
        finally:
            host.broker.close()
        turn.result(timeout=5)
    host.store.close()
    return pending


def _done(host):
    ledger = host.agent.engine.operation_ledger
    with ledger._lock:
        return ledger._conn.execute("SELECT key FROM operations").fetchall()


def _restart_and_approve(tmp_path, tools):
    host = Host(tmp_path, [[{"text": "resumed"}]], tools)
    (restored,) = host.broker.pending()
    approve = HITLDecision(HITLAction.APPROVE)
    assert host.broker.decide(restored["id"], approve, args_hash=restored["args_hash"])
    outcome = host.resume(restored["id"])
    assert outcome.resumed, outcome.reason
    host.restored = restored
    return host


NOTE = {"toolUseId": "ext-1", "name": "external_note", "input": {"record": "n1", "value": "v"}}
CHILD = {"toolUseId": "ext-2", "name": DELEGATE_TOOL_NAME, "input": {"task": "summarize"}}
READ = {"toolUseId": "ext-3", "name": "lookup", "input": {"record": "r0"}}


def _ungated_tools(service, children, reads):
    return [
        _external_tool(service),
        _note_tool(service),
        _delegate_tool(children),
        _lookup_tool(reads),
    ]


@pytest.mark.parametrize("ledger_on", [True, False], ids=["ledger", "mutant-no-ledger"])
def test_a_resume_does_not_repeat_ungated_writes_or_delegate(tmp_path, monkeypatch, ledger_on):
    """ADR 0024 §5 (the hole: rule Always writes and ``delegate`` ran again on a replay).

    The mutant run (the ungated ledger entry switched off) shows the repetition the
    ledger prevents: the same assertions with the ledger removed see two runs."""
    if not ledger_on:
        monkeypatch.setattr(
            checkpoint_hooks.ApprovalBindingVerifier, "_claim_ungated", lambda self, event: None
        )
    service, children, reads = ExternalService(), [], []
    tools = _ungated_tools(service, children, reads)
    _run_until_paused(tmp_path, tools, [NOTE, CHILD, READ, PAUSED])
    assert [r for r, _v, _k in service.applied] == ["n1"] and children == ["summarize"]
    host = _restart_and_approve(tmp_path, tools)
    applied = sorted(r for r, _v, _k in service.applied)
    if not ledger_on:
        assert applied == ["n1", "n1", "r1"] and children == ["summarize", "summarize"]
        return
    assert host.restored["replay"] == {"from_ledger": 2, "unknown": 0}
    assert applied == ["n1", "r1"], "the Always write ran once, the approved call once"
    assert children == ["summarize"], "no second child"
    assert reads == ["r0", "r0"], "a read-only call runs again (documented)"
    results = _results(host.agent)
    assert "Already applied" in results["ext-1"]["content"][0]["text"]
    assert "not repeated after an interruption" in results["ext-2"]["content"][0]["text"]
    ledger = host.agent.engine.operation_ledger
    note_key = operation_key("ext-agent", "external_note", args_hash(NOTE["input"]), "ext-1")
    record = ledger.get(note_key)
    assert record.ungated and record.state == LEDGER_COMMITTED and record.request_id is None
    gated = ledger.get(operation_key("ext-agent", "external_update", DIGEST, "ext-9"))
    assert gated is not None and not gated.ungated
    # Identity and state only: no argument or result text in the ledger.
    with ledger._lock:
        rows = ledger._conn.execute("SELECT * FROM operations").fetchall()
    assert not any("n1" in str(value) or "summarize" in str(value) for r in rows for value in r)


def test_an_ungated_write_whose_outcome_is_unknown_is_refused_on_replay(tmp_path):
    service, children, reads = ExternalService(), [], []
    tools = _ungated_tools(service, children, reads)
    _run_until_paused(tmp_path, tools, [NOTE, PAUSED])
    ledger = OperationLedger(tmp_path / "operations.db")
    key = operation_key("ext-agent", "external_note", args_hash(NOTE["input"]), "ext-1")
    with ledger._lock:  # as if the process died while the write ran
        ledger._conn.execute("UPDATE operations SET state = ? WHERE key = ?", (LEDGER_STARTED, key))
    ledger.close()
    host = _restart_and_approve(tmp_path, tools)
    assert host.restored["replay"] == {"from_ledger": 0, "unknown": 1}
    assert [r for r, _v, _k in service.applied] == ["n1", "r1"]
    result = _results(host.agent)["ext-1"]
    assert result["status"] == "error" and "outcome is unknown" in result["content"][0]["text"]


def test_an_always_write_in_a_fresh_turn_still_runs_and_is_recorded(tmp_path):
    service = ExternalService()
    host = Host(tmp_path, [[{"toolUse": NOTE}], [{"text": "done"}]], [_note_tool(service)])
    host.agent("go")
    assert [r for r, _v, _k in service.applied] == ["n1"]
    key = operation_key("ext-agent", "external_note", args_hash(NOTE["input"]), "ext-1")
    assert host.agent.engine.operation_ledger.get(key).state == LEDGER_COMMITTED


def test_a_refused_always_write_leaves_no_ledger_entry(tmp_path):
    service = ExternalService()
    host = Host(
        tmp_path,
        [[{"toolUse": NOTE}], [{"text": "done"}]],
        [_note_tool(service)],
        stability=0.4,  # WARNING: writes are blocked
    )
    host.agent("go")
    assert service.applied == []
    assert _done(host) == []


def test_a_resume_is_refused_when_the_breaker_rose_after_the_approval(tmp_path):
    service, children, reads = ExternalService(), [], []
    tools = _ungated_tools(service, children, reads)
    _run_until_paused(tmp_path, tools, [PAUSED])
    host = Host(tmp_path, [[{"text": "resumed"}]], tools)
    (restored,) = host.broker.pending()
    approve = HITLDecision(HITLAction.APPROVE)
    assert host.broker.decide(restored["id"], approve, args_hash=restored["args_hash"])
    host.agent.engine.state.stability = 0.1  # CRITICAL between the decision and the claim
    outcome = host.resume(restored["id"])
    assert (outcome.resumed, outcome.reason) == (False, BREAKER_STRICTER)
    assert service.applied == []
    assert host.store.get(restored["id"]).status == CANCELLED


def test_a_restored_card_is_held_at_a_stricter_breaker_end_to_end(tmp_path):
    service, children, reads = ExternalService(), [], []
    tools = _ungated_tools(service, children, reads)
    _run_until_paused(tmp_path, tools, [PAUSED])
    host = Host(tmp_path, [[{"text": "resumed"}]], tools, stability=0.1)
    (restored,) = host.broker.pending()
    assert restored["break_level"] == "normal" and restored["breaker_hold"] is True
    approve = HITLDecision(HITLAction.APPROVE)
    outcome = host.broker.decide(restored["id"], approve, args_hash=restored["args_hash"])
    assert outcome is DecideOutcome.BREAKER and host.scheduled == []
    assert host.broker.decide(restored["id"], HITLDecision(HITLAction.REJECT))
    assert service.applied == []


@pytest.mark.parametrize("forced", [True, False], ids=["gate", "mutant-no-gate"])
def test_breaker_recovery_and_an_edit_resume_never_run_the_original_input(
    tmp_path, monkeypatch, forced
):
    """ADR 0024 §7 (left open by ADR 0006): paused under CRITICAL halt, edited after a
    restart, resumed after the breaker recovered — only the edited input runs.

    The mutant (the paused call not forced through the gate on resume) runs the
    original input: the hole this closes."""
    if not forced:
        monkeypatch.setattr(
            checkpoint_hooks.CheckpointHooks,
            "gate_on_resume",
            lambda self, tool_use_id: contextlib.nullcontext(),
        )
    service = ExternalService()
    tools = [_external_tool(service)]
    host = Host(tmp_path, [[{"toolUse": PAUSED}], [{"text": "stopped"}]], tools, stability=0.1)
    host.agent.checkpoint_hooks.policy_check = None  # rule-free: the breaker's halt asks
    host.agent.checkpoint_hooks.binding_verifier.policy_check = None
    with ThreadPoolExecutor(max_workers=1) as pool:
        turn = pool.submit(host.agent, "go")
        try:
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and not host.broker.pending():
                time.sleep(0.01)
            (pending,) = host.broker.pending()
            assert pending["break_level"] == "critical"
        finally:
            host.broker.close()
        turn.result(timeout=5)
    host.store.close()

    host = Host(tmp_path, [[{"text": "resumed"}]], tools, stability=0.9)  # recovered
    host.agent.checkpoint_hooks.policy_check = None
    host.agent.checkpoint_hooks.binding_verifier.policy_check = None
    engine = host.agent.engine
    restore = engine.resume_from_checkpoint

    def restore_then_recovered(checkpoint_id):
        # The checkpoint brings back the CRITICAL state of the pause; the replay runs
        # as if the breaker had recovered meanwhile (NORMAL: the write is not gated).
        checkpoint = restore(checkpoint_id)
        engine.state.stability = 0.95
        engine._active_level = engine._last_break_level = BreakLevel.NORMAL
        engine._critical_turns, engine._warning_active = 0, False
        return checkpoint

    engine.resume_from_checkpoint = restore_then_recovered
    (restored,) = host.broker.pending()
    edited = {"record": "r1", "value": "edited"}
    edit = HITLDecision(HITLAction.EDIT, edited_input=edited)
    assert host.broker.decide(restored["id"], edit, args_hash=restored["args_hash"])
    (successor,) = host.broker.pending()
    approve = HITLDecision(HITLAction.APPROVE)
    assert host.broker.decide(successor["id"], approve, args_hash=successor["args_hash"])
    outcome = host.resume(successor["id"])
    assert outcome.resumed, outcome.reason
    ran = [(r, v) for r, v, _k in service.applied]
    if not forced:
        assert ran == [("r1", "v1")], "without the gate the original input runs"
        return
    assert ran == [("r1", "edited")]
    assert host.store.get(successor["id"]).consumed


def test_the_replay_counts_of_a_restored_card_are_read_once(tmp_path):
    store = ApprovalStore(tmp_path / "a.db")
    _submit(store, checkpoint_id="c" * 32)
    calls = []

    def preview(record):
        calls.append(record.request_id)
        return {"from_ledger": 1, "unknown": 0}

    broker = HITLApprovalBroker(store=store, ttl_sec=30, replay_preview=preview)
    assert broker.pending()[0]["replay"] == {"from_ledger": 1, "unknown": 0}
    assert broker.pending()[0]["replay"] == {"from_ledger": 1, "unknown": 0}
    assert calls == ["req-1"]
    failing = HITLApprovalBroker(
        store=store, ttl_sec=30, replay_preview=Mock(side_effect=RuntimeError("x"))
    )
    assert failing.pending()[0]["replay"] is None


def test_a_provider_repeating_a_tool_use_id_in_fresh_turns_is_not_a_replay(tmp_path):
    """Outside a replay an ungated entry never blocks a new call with the same key."""
    service = ExternalService()
    host = Host(
        tmp_path,
        [[{"toolUse": NOTE}], [{"text": "one"}], [{"toolUse": NOTE}], [{"text": "two"}]],
        [_note_tool(service)],
    )
    host.agent("first")
    host.agent("second")
    assert [r for r, _v, _k in service.applied] == ["n1", "n1"]
    key = operation_key("ext-agent", "external_note", args_hash(NOTE["input"]), "ext-1")
    assert host.agent.engine.operation_ledger.get(key).state == LEDGER_COMMITTED


def test_a_replay_of_a_restored_checkpoint_answers_ungated_calls_from_the_ledger(tmp_path):
    """The generic crash-resume path (``resume_from_checkpoint`` + ``agent(None)``)."""
    service = ExternalService()
    host = Host(
        tmp_path,
        [[{"toolUse": NOTE}], [{"text": "one"}], [{"text": "replayed"}]],
        [_note_tool(service)],
    )
    host.agent("first")
    (checkpoint_id, *_rest) = host.agent.engine.checkpoint_store.list("ext-agent")
    host.agent.resume_from_checkpoint(checkpoint_id)
    host.agent(None)
    assert [r for r, _v, _k in service.applied] == ["n1"]
    assert "Already applied" in _results(host.agent)["ext-1"]["content"][0]["text"]
