"""#259 (ADR 0010 addendum, option B): the breaker refuses write tools at the call.

The tool list the model sees is the same at every breaker level; a write-class call at
WARNING / CRITICAL read-only is refused by ``IdentityGuard`` with the fixed reason code
``breaker_write_refused``. Offline (FakeModel), no network.
"""

from __future__ import annotations

from unittest.mock import Mock

import pytest
from provider_fakes import FakeModel, text_turn, tool_turn
from strands import tool

from stiracore.agent import nxp_agent
from stiracore.core import (
    BreakAction,
    BreakLevel,
    BreakResult,
    HITLAction,
    HITLDecision,
    NeuralState,
    StiraConfig,
)
from stiracore.middleware.identity_guard import BREAKER_WRITE_REFUSED

LEVELS = {"normal": 0.9, "warning": 0.4, "critical": 0.1}


@tool
def file_read(path: str) -> str:
    """Read a file."""
    return "read ok"


def _agent(level, calls, *, critical_action="readonly", hitl=None, tools=None):
    @tool
    def file_write(path: str) -> str:
        """Write a file."""
        calls.append(path)
        return "written"

    model = FakeModel([tool_turn("file_write", {"path": "notes/x.md"}), text_turn("done")])
    agent = nxp_agent.NXPAgent(
        model=model,
        state=NeuralState(stability=LEVELS[level]),
        config=StiraConfig(critical_action=critical_action),
        tools=tools if tools is not None else [file_read, file_write],
        on_hitl_request=hitl,
        stimulus_classifier=Mock(classify=lambda **kwargs: None),
    )
    return agent, model


def _result_text(agent) -> tuple[str, str]:
    [result] = [
        block["toolResult"]
        for message in agent.agent.messages
        for block in message["content"]
        if "toolResult" in block
    ]
    return result["status"], result["content"][0].get("text", "")


@pytest.mark.parametrize(
    ("level", "action"), [("warning", "readonly"), ("warning", "halt"), ("critical", "readonly")]
)
def test_write_is_offered_and_refused_with_the_reason_code(level, action):
    calls: list = []
    approver = Mock(return_value=HITLDecision(HITLAction.APPROVE))
    agent, model = _agent(level, calls, critical_action=action, hitl=approver)
    agent("go")
    assert set(model.requests[0]["tool_names"]) == {"file_read", "file_write"}
    status, text = _result_text(agent)
    assert status == "error" and BREAKER_WRITE_REFUSED in text
    assert level.upper() in text
    assert calls == [] and not approver.called  # no approval lifts the breaker's refusal


def test_normal_runs_the_write():
    calls: list = []
    agent, _model = _agent("normal", calls)
    agent("go")
    status, text = _result_text(agent)
    assert status == "success" and calls == ["notes/x.md"]
    assert BREAKER_WRITE_REFUSED not in text


def test_critical_halt_still_goes_to_the_human_gate():
    calls: list = []
    approver = Mock(return_value=HITLDecision(HITLAction.APPROVE))
    agent, _ = _agent("critical", calls, critical_action="halt", hitl=approver)
    agent("go")
    assert approver.called and calls == ["notes/x.md"]


def test_tool_list_is_identical_at_every_level():
    lists = []
    for level in ("normal", "warning", "critical"):
        agent, model = _agent(level, [])
        agent("go")
        lists.append(model.requests[0]["tool_names"])
    assert lists[0] == lists[1] == lists[2]


def test_editor_read_command_is_refused_by_name_while_writes_are_withheld():
    """Not weaker than the old list filter: a name that was hidden is refused whatever
    its arguments (``editor view`` included)."""
    agent, _ = _agent("warning", [])
    breaker = BreakResult(BreakLevel.WARNING, agent.engine._actions_for(BreakLevel.WARNING))
    agent.engine._current_break_result = breaker
    try:
        result = agent.identity_guard.check_tool_call("editor", {"command": "view", "path": "a"})
        assert not result.allowed and BREAKER_WRITE_REFUSED in result.reason
        assert agent.identity_guard.check_tool_call("file_read", {"path": "a"}).allowed
    finally:
        agent.engine._current_break_result = None


def test_unnamed_tool_is_still_left_out_while_writes_are_withheld():
    """Fail closed: the call-time check decides by name, so a tool whose name cannot be
    resolved (e.g. a ToolProvider) is not offered while the breaker withholds writes."""
    unnamed = object()
    agent, _ = _agent("normal", [], tools=[file_read, unnamed])
    normal = BreakResult(BreakLevel.NORMAL, BreakAction.NONE)
    warning = BreakResult(BreakLevel.WARNING, agent.engine._actions_for(BreakLevel.WARNING))
    assert unnamed in agent._turn_tools(normal)
    assert agent._turn_tools(warning) == [file_read]


@pytest.mark.parametrize(
    ("level", "action", "expected"),
    [
        (BreakLevel.NORMAL, "readonly", None),
        (BreakLevel.COHERENCE, "readonly", None),
        (BreakLevel.WARNING, "readonly", "WARNING"),
        (BreakLevel.CRITICAL, "readonly", "CRITICAL"),
        (BreakLevel.CRITICAL, "halt", None),  # HALT: a human approval, not a refusal
    ],
)
def test_runtime_notice_only_while_writes_are_refused(level, action, expected):
    agent, _ = _agent("normal", [], critical_action=action)
    notice = nxp_agent._breaker_tool_notice(BreakResult(level, agent.engine._actions_for(level)))
    if expected is None:
        assert notice == ""
    else:
        assert notice.startswith(f"Circuit breaker {expected}:")
        assert BREAKER_WRITE_REFUSED in notice and "\n" not in notice


def test_an_mcp_write_closed_mid_turn_stays_refused_until_the_turn_ends(monkeypatch):
    """``close_mcp_tools`` during a turn drops the name from ``WRITE_TOOLS``; the guard
    keeps the names the breaker refused at the turn's start (union with the live set)."""
    from stiracore.tools import tool_registry

    monkeypatch.setitem(tool_registry.WRITE_TOOLS, "put_object", object())
    agent, _ = _agent("warning", [])
    warning = BreakResult(BreakLevel.WARNING, agent.engine._actions_for(BreakLevel.WARNING))
    agent._turn_tools(warning)  # the turn starts while the MCP tool is registered
    tool_registry.WRITE_TOOLS.pop("put_object")  # what close_mcp_tools does mid-turn
    agent.engine._current_break_result = warning
    try:
        result = agent.identity_guard.check_tool_call("put_object", {"key": "k"})
        assert not result.allowed and BREAKER_WRITE_REFUSED in result.reason
        # the next turn starts without it: no longer a registered write name
        agent._turn_tools(warning)
        assert "put_object" not in agent.identity_guard.turn_write_names
    finally:
        agent.engine._current_break_result = None
