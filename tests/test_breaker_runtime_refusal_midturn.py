"""#259: an MCP write closed mid-turn is refused by the gate before any card.

``close_mcp_tools`` during a turn drops the name from ``WRITE_TOOLS``; at WARNING with an
Ask rule, ``CheckpointHooks`` still reads the names refused at the turn's start, so no
approval card is shown and the tool does not run. Offline (FakeModel), no network.
"""

from __future__ import annotations

from unittest.mock import Mock

from provider_fakes import FakeModel, text_turn, tool_turn
from strands import tool

from stiracore.agent import nxp_agent
from stiracore.core import HITLAction, HITLDecision, NeuralState, StiraConfig
from stiracore.middleware.identity_guard import BREAKER_WRITE_REFUSED
from stiracore.tools import tool_registry


def test_mcp_write_closed_mid_turn_is_refused_before_the_card(monkeypatch):
    calls: list = []

    @tool
    def file_read(path: str) -> str:
        """Read a file (closes the MCP tool, as ``close_mcp_tools`` would mid-turn)."""
        tool_registry.WRITE_TOOLS.pop("put_object", None)
        return "read ok"

    @tool
    def put_object(key: str) -> str:
        """Write an object."""
        calls.append(key)
        return "put"

    monkeypatch.setitem(tool_registry.WRITE_TOOLS, "put_object", object())
    model = FakeModel(
        [
            tool_turn("file_read", {"path": "a"}, "tool-1"),
            tool_turn("put_object", {"key": "k"}, "tool-2"),
            text_turn("done"),
        ]
    )
    approver = Mock(return_value=HITLDecision(HITLAction.APPROVE))
    agent = nxp_agent.NXPAgent(
        model=model,
        state=NeuralState(stability=0.4),
        config=StiraConfig(),
        tools=[file_read, put_object],
        on_hitl_request=approver,
        stimulus_classifier=Mock(classify=lambda **kwargs: None),
        tool_policy=lambda name: "ask" if name == "put_object" else "always",
    )
    agent("go")
    assert "put_object" not in tool_registry.WRITE_TOOLS
    results = {
        block["toolResult"]["toolUseId"]: block["toolResult"]
        for message in agent.agent.messages
        for block in message["content"]
        if "toolResult" in block
    }
    refused = results["tool-2"]
    assert refused["status"] == "error"
    assert BREAKER_WRITE_REFUSED in refused["content"][0]["text"]
    assert "not sent for approval" in refused["content"][0]["text"]
    assert calls == [] and not approver.called
