"""#330 review: a tool-rule save whose revocation fails is fail-closed, and saving Never
races approvals without anything running.

Wired like ``stira_server`` (``_tool_policy_reader`` + ``_tool_rules_saved_hook`` +
the durable broker + the config blueprint + a real NXPAgent gate on FakeModel).
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest
from flask import Flask
from test_tool_policy import RecordingTool

from dashboard.stira_server import _tool_policy_reader, _tool_rules_saved_hook
from stiracore.agent.nxp_agent import NXPAgent
from stiracore.core import HITLAction, HITLDecision, NeuralState, StiraConfig
from stiracore.core.approval import args_hash
from stiracore.core.approval_store import ApprovalStore
from stiracore.observability.chat_api import (
    DecideOutcome,
    HITLApprovalBroker,
    create_chat_blueprint,
)
from stiracore.observability.config_api import ConfigStore, create_config_blueprint
from stiracore.tools import tool_registry
from stiracore.tools.tool_config import ToolConfig
from tests.provider_fakes import FakeModel, text_turn, tool_turn

NAME = "race_probe_write"
RACE_OUTCOMES: list = []  # (accepted, runs, final status) per round, for the report
INPUT = {"path": "notes/x.md"}
DIGEST = args_hash(INPUT)


class Host:
    def __init__(self, tmp_path):
        self.calls: list = []
        self.tool = RecordingTool(NAME, self.calls)
        tool_registry.WRITE_TOOLS[NAME] = self.tool
        self.config = ConfigStore(tmp_path / "g.json", tmp_path / "a.json", tmp_path / "t.json")
        ToolConfig(
            mode="custom", enabled_tools={NAME}, ask_tools={NAME}, path=self.config.tool_path
        ).save()
        self.approvals = ApprovalStore(tmp_path / "approvals.db")
        self.broker = HITLApprovalBroker(store=self.approvals, ttl_sec=20)
        self.reader = _tool_policy_reader(
            self.config,
            self.approvals,
            tmp_path / "marker.json",
            on_reconciled=self.broker.resume_approvals,
        )
        self.reader.sync()
        self.sync_from_store = Mock(wraps=self.broker.sync_from_store)
        self.broker.sync_from_store = self.sync_from_store
        app = Flask(__name__)
        app.register_blueprint(
            create_config_blueprint(
                self.config, on_tool_rules_saved=_tool_rules_saved_hook(self.reader, self.broker)
            )
        )
        app.register_blueprint(
            create_chat_blueprint(object(), runner=lambda m: "unused", hitl_broker=self.broker)
        )
        self.client = app.test_client()

    def agent(self, hitl=None):
        return NXPAgent(
            model=FakeModel([tool_turn(NAME, INPUT), text_turn("done")]),
            state=NeuralState(stability=0.9),
            config=StiraConfig(critical_action="halt"),
            tools=[self.tool],
            on_hitl_request=hitl or self.broker.request,
            stimulus_classifier=Mock(classify=lambda **kwargs: None),
            tool_policy=self.reader,
        )

    def save_never(self):
        return self.client.post("/api/config/tools", json={"name": NAME, "policy": "never"})

    def decide(self, request_id, action="approve"):
        body = {"action": action, "args_hash": DIGEST}
        return self.client.post(f"/api/hitl/{request_id}/decision", json=body)

    def submit_restored(self, request_id):
        self.approvals.submit(
            request_id=request_id,
            agent_id="stira",
            tool_name=NAME,
            tool_input=INPUT,
            expires_at=time.time() + 600,
            tool_use_id=f"use-{request_id}",
            correlation_id="turn-1",
            origin={"kind": "turn", "id": "turn-1"},
            checkpoint_id=None,
        )


@pytest.fixture
def host(tmp_path, monkeypatch):
    monkeypatch.setitem(tool_registry.WRITE_TOOLS, NAME, None)
    return Host(tmp_path)


def _wait_pending(broker, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pending = broker.pending()
        if pending:
            return pending
        time.sleep(0.005)
    raise AssertionError("no pending request")


def _results_text(agent):
    return [
        block["toolResult"]["content"][0].get("text", "")
        for message in agent.agent.messages
        for block in message["content"]
        if "toolResult" in block
    ]


# --- 1. revocation fails: the save stands, the response is an error, nothing is approvable


def _break_revocation(host, monkeypatch):
    def broken(*_args, **_kwargs):
        raise RuntimeError("approval store write failed")

    monkeypatch.setattr(host.approvals, "cancel", broken)


def test_a_failed_revocation_is_an_error_and_a_restored_card_cannot_be_approved(host, monkeypatch):
    host.submit_restored("r1")
    _break_revocation(host, monkeypatch)
    response = host.save_never()
    assert response.status_code == 500
    assert response.json["error_code"] == "approvals_not_revoked"
    assert response.json["rules_saved"] is True
    assert host.config.load_tools().policy_of(NAME) == "never", "the save itself stands"
    assert host.sync_from_store.call_count == 1, "the queue is reconciled even so"
    pending = host.client.get("/api/hitl/pending").json
    assert pending["approvals_suspended"] is True
    decision = host.decide("r1")
    assert (decision.status_code, decision.json["code"]) == (503, "suspended")
    assert host.approvals.get("r1").status == "pending", "nothing was recorded as approved"
    # A rejection stays possible.
    assert host.decide("r1", "reject").status_code == 202
    assert host.calls == []


def test_a_failed_revocation_refuses_a_live_card_and_the_tool_never_runs(host, monkeypatch):
    agent = host.agent()
    with ThreadPoolExecutor(max_workers=1) as pool:
        turn = pool.submit(agent, "go")
        (pending,) = _wait_pending(host.broker)
        _break_revocation(host, monkeypatch)
        assert host.save_never().status_code == 500
        assert host.decide(pending["id"]).status_code == 503
        assert host.decide(pending["id"], "reject").status_code == 202
        turn.result(timeout=10)
    assert host.calls == []


def test_the_suspension_lifts_once_the_open_approvals_are_reconciled(host, monkeypatch):
    host.submit_restored("r2")
    _break_revocation(host, monkeypatch)
    assert host.save_never().status_code == 500
    monkeypatch.undo()  # the store works again (cancel restored); the rule is retained
    monkeypatch.setitem(tool_registry.WRITE_TOOLS, NAME, host.tool)
    again = host.client.post("/api/config/tools", json={"name": NAME, "policy": "never"})
    assert again.status_code == 200, again.json
    assert host.broker.approvals_suspended() is None
    assert host.approvals.get("r2").status == "cancelled"
    assert "approvals_suspended" not in host.client.get("/api/hitl/pending").json


def test_the_hook_reconciles_the_queue_even_when_the_revocation_raises():
    reader = Mock()
    reader.sync.side_effect = RuntimeError("boom")
    broker = Mock()
    hook = _tool_rules_saved_hook(reader, broker)
    with pytest.raises(RuntimeError):
        hook()
    broker.suspend_approvals.assert_called_once()
    broker.sync_from_store.assert_called_once()
    # A failing reconciliation never hides the revocation's own error.
    broker.sync_from_store.side_effect = OSError("queue")
    with pytest.raises(RuntimeError):
        hook()


# --- 2. Never saved while approvals race -----------------------------------------------


def test_an_approval_given_just_before_the_save_is_not_used(host):
    """The human approves; the save lands before the gate re-checks the rule."""

    def approve_then_save(request):
        decision = host.broker.request(request)
        assert decision.action == HITLAction.APPROVE
        assert host.save_never().status_code == 200
        return decision

    def operator():
        (pending,) = _wait_pending(host.broker)
        host.decide(pending["id"])

    worker = threading.Thread(target=operator)
    worker.start()
    agent = host.agent(hitl=approve_then_save)
    agent("go")
    worker.join(timeout=5)
    assert host.calls == []
    assert any("changed while waiting" in text for text in _results_text(agent))


@pytest.mark.parametrize("round_", range(12))
def test_double_approval_racing_a_never_save_runs_at_most_once(host, round_):
    """Two approvals and a Never save at the same moment (measured, not timed)."""
    agent = host.agent()
    barrier = threading.Barrier(3)
    outcomes = []

    def approve(request_id):
        barrier.wait()
        outcomes.append(host.decide(request_id).status_code)

    def save():
        barrier.wait()
        outcomes.append(("save", host.save_never().status_code))

    with ThreadPoolExecutor(max_workers=4) as pool:
        turn = pool.submit(agent, "go")
        (pending,) = _wait_pending(host.broker)
        racers = [pool.submit(approve, pending["id"]) for _ in range(2)] + [pool.submit(save)]
        for racer in racers:
            racer.result(timeout=10)
        turn.result(timeout=20)
    accepted = outcomes.count(202)
    assert ("save", 200) in outcomes
    assert accepted <= 1, outcomes
    assert len(host.calls) <= accepted, (outcomes, host.calls)
    record = host.approvals.get(pending["id"])
    if host.calls:
        # It ran only under the rule it was approved under (the save came after the
        # gate's last check), with the approval consumed exactly once.
        assert record.consumed and record.status == "approved"
    # Not run: refused at the decision (revoked first) or at the gate's re-check
    # (approved and handed over, then voided by the newer generation).
    RACE_OUTCOMES.append((accepted, len(host.calls), record.status))


@pytest.mark.parametrize("round_", range(12))
def test_double_approval_of_a_restored_card_racing_a_never_save_is_never_usable(host, round_):
    host.submit_restored("r3")
    barrier = threading.Barrier(3)
    outcomes = []

    def approve():
        barrier.wait()
        outcomes.append(
            host.broker.decide("r3", HITLDecision(HITLAction.APPROVE), args_hash=DIGEST)
        )

    def save():
        barrier.wait()
        outcomes.append(host.save_never().status_code)

    threads = [threading.Thread(target=approve) for _ in range(2)]
    threads.append(threading.Thread(target=save))
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert 200 in outcomes
    assert sum(1 for o in outcomes if o is DecideOutcome.ACCEPTED) <= 1, outcomes
    record = host.approvals.get("r3")
    # Approved before the save: revoked by it. After: refused. Either way unusable.
    assert record.status == "cancelled", (outcomes, record.status)
    assert host.approvals.consume("r3") is False
