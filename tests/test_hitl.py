"""#94: CRITICAL HALT is an operation-local synchronous HITL boundary."""

import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock

import pytest
from flask import Flask
from strands import tool
from test_subagent import ScriptedModel

from stiracore.agent.nxp_agent import NXPAgent
from stiracore.core import (
    BreakLevel,
    FileCheckpointStore,
    HITLAction,
    HITLDecision,
    HITLRequest,
    NeuralState,
    StiraConfig,
)
from stiracore.core.approval import args_hash
from stiracore.middleware.identity_guard import IdentityGuard
from stiracore.observability.chat_api import HITLApprovalBroker, create_chat_blueprint

ROOT = Path(__file__).resolve().parents[1]


def _critical_agent(monkeypatch, tmp_path, decision):
    writes = []
    reads = []

    @tool
    def file_write(value: str) -> str:
        """Write one value."""
        writes.append(value)
        return f"wrote {value}"

    @tool
    def file_read(path: str) -> str:
        """Read one path."""
        reads.append(path)
        return f"read {path}"

    model = ScriptedModel(
        [
            [
                {
                    "toolUse": {
                        "toolUseId": "write-1",
                        "name": "file_write",
                        "input": {"value": "original"},
                    }
                },
                {
                    "toolUse": {
                        "toolUseId": "read-1",
                        "name": "file_read",
                        "input": {"path": "notes.md"},
                    }
                },
            ],
            [{"text": "turn continued"}],
        ]
    )
    monkeypatch.setattr("stiracore.agent.nxp_agent.BedrockModel", lambda **kwargs: model)
    if decision is None:
        callback = None
    elif isinstance(decision, list):
        callback = Mock(side_effect=decision)
    else:
        callback = Mock(return_value=decision)
    agent = NXPAgent(
        "offline",
        "us-east-1",
        state=NeuralState(stability=0.1),
        config=StiraConfig(critical_action="halt"),
        on_hitl_request=callback,
        tools=[file_write, file_read],
        agent_id="hitl-test",
        checkpoint_store=FileCheckpointStore(tmp_path),
        stimulus_classifier=Mock(classify=lambda **kwargs: None),
    )
    return agent, callback, writes, reads


def _tool_results(agent):
    return {
        block["toolResult"]["toolUseId"]: block["toolResult"]
        for message in agent.agent.messages
        for block in message["content"]
        if "toolResult" in block
    }


@pytest.mark.parametrize(
    ("decision", "expected_writes", "expected_status", "expected_text"),
    [
        (HITLDecision(HITLAction.APPROVE), ["original"], "success", "wrote original"),
        (
            # #146: an edit is re-reviewed as a new request; only its approval runs it.
            [
                HITLDecision(HITLAction.EDIT, edited_input={"value": "edited"}),
                HITLDecision(HITLAction.APPROVE),
            ],
            ["edited"],
            "success",
            "wrote edited",
        ),
        (HITLDecision(HITLAction.REJECT), [], "error", "HITL rejected"),
        (
            HITLDecision(HITLAction.RESPOND, response="operator response"),
            [],
            "success",
            "operator response",
        ),
    ],
)
def test_critical_halt_hitl_four_way_branch_keeps_turn_alive(
    monkeypatch,
    tmp_path,
    decision,
    expected_writes,
    expected_status,
    expected_text,
):
    agent, callback, writes, reads = _critical_agent(monkeypatch, tmp_path, decision)

    assert str(agent("do both operations")).strip() == "turn continued"

    assert writes == expected_writes
    assert reads == ["notes.md"], "reject/respond must not kill the rest of the tool batch"
    request = callback.call_args_list[0].args[0]
    assert (request.tool_name, request.tool_input, request.break_level, request.tool_use_id) == (
        "file_write",
        {"value": "original"},
        BreakLevel.CRITICAL,
        "write-1",
    )
    assert request.reason == "CRITICAL HALT requires human approval for this write operation."
    # Bound to agent + operation + normalized-argument hash + expiry (#146).
    assert request.agent_id == "hitl-test"
    assert request.args_hash == args_hash({"value": "original"})
    assert request.expires_at > time.time()
    if isinstance(decision, list):
        assert callback.call_count == 2
        second = callback.call_args_list[1].args[0]
        assert second.tool_input == {"value": "edited"}
        assert second.supersedes == request.request_id
        assert second.request_id != request.request_id
        assert second.args_hash == args_hash({"value": "edited"})
    else:
        callback.assert_called_once()
    result = _tool_results(agent)["write-1"]
    assert result["status"] == expected_status
    assert expected_text in result["content"][0]["text"]


def test_critical_halt_without_human_rejects_write_and_continues(monkeypatch, tmp_path):
    agent, callback, writes, reads = _critical_agent(monkeypatch, tmp_path, None)

    assert str(agent("do both operations")).strip() == "turn continued"

    assert callback is None
    assert writes == []
    assert reads == ["notes.md"]
    result = _tool_results(agent)["write-1"]
    assert result["status"] == "error"
    assert "HITL rejected" in result["content"][0]["text"]


def test_hitl_broker_blocks_until_http_decision():
    broker = HITLApprovalBroker()
    app = Flask(__name__)
    app.register_blueprint(
        create_chat_blueprint(object(), runner=lambda message: "unused", hitl_broker=broker)
    )
    hitl_request = HITLRequest(
        tool_name="file_write",
        tool_input={"value": "before"},
        break_level=BreakLevel.CRITICAL,
        reason="test",
        tool_use_id="write-1",
    )

    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(broker.request, hitl_request)
        deadline = time.monotonic() + 2
        pending = []
        while time.monotonic() < deadline and not pending:
            pending = app.test_client().get("/api/hitl/pending").json["requests"]
            if not pending:
                time.sleep(0.01)
        assert pending[0]["tool_input"] == {"value": "before"}
        assert not future.done()
        response = app.test_client().post(
            f"/api/hitl/{pending[0]['id']}/decision",
            json={
                "action": "edit",
                "edited_input": {"value": "after"},
                "args_hash": pending[0]["args_hash"],
            },
        )
        assert response.status_code == 202
        decided = future.result(timeout=2)
        assert (decided.action, decided.edited_input) == (HITLAction.EDIT, {"value": "after"})
        assert decided.request_id == pending[0]["id"]
        assert decided.args_hash == args_hash({"value": "before"})

    assert app.test_client().get("/api/hitl/pending").json["requests"] == []


def test_identity_guard_requires_per_tool_approval_and_never_overrides_protected_path(tmp_path):
    protected = tmp_path / "SOUL.md"
    from stiracore.core import StiraEngine

    policy = StiraEngine(
        NeuralState(stability=0.1),
        StiraConfig(critical_action="halt"),
    )
    policy.pre_turn()
    guard = IdentityGuard(policy, read_only_paths=[str(protected)])

    assert not guard.check_tool_call("file_write", {"path": "ordinary.txt"}).allowed
    assert guard.check_tool_call("file_write", {"path": "ordinary.txt"}, hitl_approved=True).allowed
    protected_result = guard.check_tool_call(
        "file_write", {"path": str(protected)}, hitl_approved=True
    )
    assert not protected_result.allowed
    assert protected_result.layer == 3


def test_chat_ui_exposes_all_four_hitl_actions_and_api_contract():
    html = (ROOT / "dashboard/templates/chat.html").read_text(encoding="utf-8")
    js = (ROOT / "dashboard/static/js/approvals.js").read_text(encoding="utf-8")

    # #189: the chat queue renders approval cards from approvals.js.
    assert {"hitl-panel", "approval-list", "approval-live"} <= set(
        re.findall(r'id="([^"]+)"', html)
    )
    assert "js/approvals.js" in html
    assert set(re.findall(r'\), "(approve|edit|reject|respond)", "btn-', js)) == {
        action.value for action in HITLAction
    }
    assert 'getJSON("/api/hitl/pending")' in js
    assert "/api/hitl/${encodeURIComponent(id)}/decision" in js
    assert "args_hash: request.args_hash" in js


def test_stira_keeps_write_tools_visible_only_for_critical_hitl(tmp_path):
    from dashboard.stira_server import _stira_tools
    from stiracore.tools import MemoryStore, create_memory_tools

    store = MemoryStore(tmp_path / "memory", "stira-test")
    bound_tools = tuple(create_memory_tools(store))
    write_names = {"memory_write"}
    hidden = {tool.tool_name for tool in _stira_tools(bound_tools, write_names, "critical")}
    gated = {
        tool.tool_name
        for tool in _stira_tools(bound_tools, write_names, "critical", hitl_enabled=True)
    }
    assert "memory_write" not in hidden
    assert "memory_write" in gated


def test_hitl_http_rejects_invalid_or_stale_decisions():
    broker = HITLApprovalBroker()
    app = Flask(__name__)
    app.register_blueprint(
        create_chat_blueprint(object(), runner=lambda message: "unused", hitl_broker=broker)
    )
    client = app.test_client()

    assert client.post("/api/hitl/missing/decision", json={"action": "reject"}).status_code == 404
    assert client.post("/api/hitl/missing/decision", json={"action": "approve"}).status_code == 400
    assert (
        client.post(
            "/api/hitl/missing/decision", json={"action": "approve", "args_hash": "x"}
        ).status_code
        == 404
    )
    assert client.post("/api/hitl/missing/decision", json={"action": "edit"}).status_code == 400
    assert client.post("/api/hitl/missing/decision", json={"action": "respond"}).status_code == 400
    assert client.post("/api/hitl/missing/decision", json={"action": "other"}).status_code == 400
