"""#287: saving a tool rule voids the open approvals at once, and the history says so.

Before: the approval store revoked them only at the next gate call, so a restored
card stayed approvable after a rule was saved Never (``POST /decision`` -> 202),
and ``/api/hitl/history`` kept ``approved`` after the store had cancelled it.
"""

from __future__ import annotations

import threading
import time
from unittest.mock import Mock

import pytest

from stiracore.core import BreakLevel, HITLAction, HITLDecision, HITLRequest
from stiracore.core.approval import args_hash
from stiracore.core.approval_store import ApprovalStore
from stiracore.observability.chat_api import HITLApprovalBroker
from stiracore.observability.config_api import ConfigStore

INPUT = {"path": "notes/x.md", "content": "hello"}
DIGEST = args_hash(INPUT)
REASON = "tool rules changed (#190)"


def _submit(store, request_id, tool_name="file_write", **overrides):
    fields = {
        "request_id": request_id,
        "agent_id": "stira",
        "tool_name": tool_name,
        "tool_input": INPUT,
        "expires_at": time.time() + 600,
        "tool_use_id": f"use-{request_id}",
        "correlation_id": "turn-1",
        "origin": {"kind": "turn", "id": "turn-1"},
        "checkpoint_id": None,
    }
    fields.update(overrides)
    return store.submit(**fields)


@pytest.fixture
def stira(tmp_path, monkeypatch):
    from dashboard import stira_server
    from stiracore.agent import nxp_agent, stimulus_classifier
    from stiracore.agent.stimulus_classifier import StimulusResult

    monkeypatch.setattr(stira_server, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(nxp_agent, "BedrockModel", Mock(return_value=Mock()))
    monkeypatch.setattr(nxp_agent, "Agent", Mock(side_effect=AssertionError("model called")))
    monkeypatch.setattr(
        stimulus_classifier,
        "get_classifier",
        lambda: Mock(
            model_id=None, classify=Mock(return_value=StimulusResult(category="joy", intensity=0.5))
        ),
    )
    store = ConfigStore(tmp_path / "gw.json", tmp_path / "agent.json", tmp_path / "tools.json")
    return stira_server.create_app(config_store=store)


def test_saving_never_removes_a_restored_card_and_refuses_its_approval(stira):
    approvals = stira.extensions["approval_store"]
    _submit(approvals, "restored-1")
    client = stira.test_client()
    assert [r["id"] for r in client.get("/api/hitl/pending").json["requests"]] == ["restored-1"]

    saved = client.post("/api/config/tools", json={"name": "file_write", "policy": "never"})
    assert saved.status_code == 200, saved.json

    # Gone from the queue at once, not at the next gate call.
    assert client.get("/api/hitl/pending").json["requests"] == []
    record = approvals.get("restored-1")
    assert record.status == "cancelled" and record.resolution_reason == REASON
    decision = client.post(
        "/api/hitl/restored-1/decision", json={"action": "approve", "args_hash": DIGEST}
    )
    assert decision.status_code == 409, decision.json
    assert approvals.get("restored-1").status == "cancelled"
    history = client.get("/api/hitl/history").json["history"]
    assert [(h["id"], h["status"], h["resolution_reason"]) for h in history] == [
        ("restored-1", "cancelled", REASON)
    ]


def test_an_approved_unused_approval_is_revoked_by_the_save_and_shown_cancelled(stira):
    approvals = stira.extensions["approval_store"]
    _submit(approvals, "restored-2")
    assert approvals.decide("restored-2", "approve", args_hash=DIGEST, approver="local:operator")
    client = stira.test_client()
    saved = client.post("/api/config/tools", json={"name": "file_write", "policy": "never"})
    assert saved.status_code == 200, saved.json
    assert approvals.get("restored-2").status == "cancelled"
    entry = client.get("/api/hitl/history").json["history"][0]
    assert (entry["id"], entry["status"], entry["action"]) == ("restored-2", "cancelled", "reject")


def test_history_follows_the_store_after_a_later_cancellation(tmp_path):
    """The reported path: approved in this process, then cancelled by the resume."""
    store = ApprovalStore(tmp_path / "a.db")
    _submit(store, "r1")
    broker = HITLApprovalBroker(store=store)
    outcome = broker.decide("r1", HITLDecision(HITLAction.APPROVE), args_hash=DIGEST)
    assert outcome
    assert broker.history()[0]["status"] == "approved"
    # resume -> gate sync -> cancelled(tool rules changed) -> resume_refused
    assert store.cancel("r1", reason=REASON)
    history = broker.history()
    assert [(h["id"], h["status"], h["action"], h["resolution_reason"]) for h in history] == [
        ("r1", "cancelled", "reject", REASON)
    ]


def test_a_live_waiting_request_is_rejected_when_the_store_revokes_it(tmp_path):
    store = ApprovalStore(tmp_path / "a.db")
    broker = HITLApprovalBroker(store=store, ttl_sec=30)
    result: dict = {}

    def worker():
        result["decision"] = broker.request(
            HITLRequest(
                tool_name="file_write",
                tool_input=INPUT,
                break_level=BreakLevel.NORMAL,
                reason="Ask",
                tool_use_id="use-live",
                agent_id="stira",
                request_id="live-1",
            )
        )

    thread = threading.Thread(target=worker)
    thread.start()
    deadline = time.time() + 5
    while not broker.pending() and time.time() < deadline:
        time.sleep(0.01)
    assert [p["id"] for p in broker.pending()] == ["live-1"]
    assert store.cancel("live-1", reason=REASON)
    broker.sync_from_store()
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert result["decision"].action == HITLAction.REJECT
    assert broker.pending() == []
    assert broker.history()[0]["status"] == "cancelled"
    assert broker.decide("live-1", HITLDecision(HITLAction.APPROVE), args_hash=DIGEST).value in (
        "already_decided",
        "expired",
    )


def test_a_failing_save_hook_keeps_the_save_but_answers_with_an_error(tmp_path):
    """Fail-closed (#330 review): the rule stands, the response says the revocation failed."""
    from flask import Flask

    from stiracore.observability.config_api import create_config_blueprint

    store = ConfigStore(tmp_path / "g.json", tmp_path / "a.json", tmp_path / "t.json")
    hook = Mock(side_effect=RuntimeError("store down"))
    app = Flask(__name__)
    app.register_blueprint(create_config_blueprint(store, on_tool_rules_saved=hook))
    response = app.test_client().post(
        "/api/config/tools", json={"name": "file_write", "policy": "never"}
    )
    assert response.status_code == 500
    assert response.json["error_code"] == "approvals_not_revoked"
    assert "store down" not in response.get_data(as_text=True)
    assert hook.call_count == 1
    assert store.load_tools().policy_of("file_write") == "never"
