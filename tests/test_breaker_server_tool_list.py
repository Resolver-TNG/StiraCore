"""#259 on the chat server: ``_stira_tools`` keeps one list at every breaker level.

Premise first (security review, PR #838): every tool the old level filter
(``get_tools(level)`` + the bound memory write names + ``delegate``) removed is refused at
the call at WARNING / CRITICAL without ``halt`` when the agent is set up like the server
(``blocked_tool_names`` = registry writes ∪ bound memory writes ∪ ``delegate``). Then the
server-like turn loop: NORMAL → WARNING → CRITICAL keeps ``tools_hash``; a write is refused
with ``breaker_write_refused`` at WARNING and runs at NORMAL. Offline (FakeModel).
"""

from __future__ import annotations

from unittest.mock import Mock

import pytest
from provider_fakes import FakeModel, text_turn, tool_turn

from dashboard.stira_server import _registered_tool_name, _stira_tools
from stiracore.agent.nxp_agent import NXPAgent
from stiracore.core import BreakLevel, BreakResult, NeuralState, StiraConfig
from stiracore.middleware.identity_guard import BREAKER_WRITE_REFUSED
from stiracore.tools import MemoryStore, create_memory_tools, tool_registry
from stiracore.tools.delegate_tool import DELEGATE_TOOL_NAME, DelegateTool, DelegationSettings
from stiracore.yolo import origin

LEVELS = {"normal": 0.9, "warning": 0.4, "critical": 0.1}
WRITE_NAMES = {"memory_write"}


def _names(tools) -> list:
    return [_registered_tool_name(tool) for tool in tools]


@pytest.fixture
def bound(tmp_path):
    return tuple(create_memory_tools(MemoryStore(tmp_path / "memory", "stira-test")))


def _server_like_agent(bound, script, *, critical_action, stability=0.9):
    config = StiraConfig(critical_action=critical_action)
    config.blocked_tool_names = (
        set(config.blocked_tool_names)
        | set(tool_registry.WRITE_TOOLS)
        | WRITE_NAMES
        | {DELEGATE_TOOL_NAME}
    )
    model = FakeModel(script)
    agent = NXPAgent(
        model=model,
        state=NeuralState(stability=stability),
        config=config,
        tools=_stira_tools(bound, WRITE_NAMES),
        stimulus_classifier=Mock(classify=lambda **kwargs: None),
        callback_handler=None,
    )
    return agent, model


def _old_filter_removed() -> set:
    """What the level filter used to remove at WARNING (registry side)."""
    full = {_registered_tool_name(t) for t in tool_registry.get_tools(None)}
    warning = {_registered_tool_name(t) for t in tool_registry.get_tools("warning")}
    return full - warning


def test_the_server_list_is_the_same_at_every_level(bound):
    delegate = DelegateTool(settings=DelegationSettings)
    with origin.human_turn("stira"):
        lists = {
            level: _names(
                _stira_tools(bound, WRITE_NAMES, level, hitl_enabled=True, delegate_tool=delegate)
            )
            for level in ("normal", "warning", "critical")
        }
        without = _names(_stira_tools(bound, WRITE_NAMES, "critical", hitl_enabled=True))
    assert lists["normal"] == lists["warning"]
    assert DELEGATE_TOOL_NAME in lists["warning"]
    # CRITICAL keeps the rest of the list; only ``delegate`` stays out (fail closed: under
    # the server's ``halt`` its call would reach a card, not a refusal).
    assert lists["critical"] == [n for n in lists["normal"] if n != DELEGATE_TOOL_NAME]
    assert without == lists["critical"]
    assert _old_filter_removed() and _old_filter_removed() <= set(lists["warning"])
    assert WRITE_NAMES <= set(lists["warning"])


@pytest.mark.parametrize("level", ["warning", "critical"])
def test_every_formerly_hidden_tool_is_refused_at_the_call(bound, level):
    agent, _ = _server_like_agent(bound, [text_turn("x")], critical_action="readonly")
    hidden = _old_filter_removed() | WRITE_NAMES | {DELEGATE_TOOL_NAME}
    breaker_level = BreakLevel[level.upper()]
    breaker = BreakResult(breaker_level, agent.engine._actions_for(breaker_level))
    agent.engine._current_break_result = breaker
    try:
        for name in sorted(hidden):
            result = agent.identity_guard.check_tool_call(name, {})
            assert not result.allowed, name
            assert BREAKER_WRITE_REFUSED in result.reason, (name, result.reason)
        normal = BreakResult(BreakLevel.NORMAL, agent.engine._actions_for(BreakLevel.NORMAL))
        agent.engine._current_break_result = normal
        for name in sorted(hidden):
            result = agent.identity_guard.check_tool_call(name, {})
            assert BREAKER_WRITE_REFUSED not in result.reason, name
    finally:
        agent.engine._current_break_result = None


def _write_turn(use_id):
    return tool_turn(
        "memory_write", {"layer": "episodic", "key": use_id, "content": "note"}, use_id
    )


def _result(agent, use_id) -> tuple[str, str]:
    for message in agent.agent.messages:
        for block in message["content"]:
            result = block.get("toolResult")
            if result and result["toolUseId"] == use_id:
                return result["status"], result["content"][0].get("text", "")
    raise AssertionError(use_id)


def test_normal_warning_critical_keep_tools_hash_and_refuse_the_write(bound):
    """The server's turn loop (``runner``: rebuild ``agent.tools`` from the level, then
    run the turn) with the server's ``critical_action="halt"``."""
    script = [
        _write_turn("w-normal"),
        text_turn("ok"),
        _write_turn("w-warning"),
        text_turn("ok"),
        text_turn("ok"),
    ]
    agent, model = _server_like_agent(bound, script, critical_action="halt")
    hashes, tool_lists = [], []
    for level in ("normal", "warning", "critical"):
        agent.state.stability = LEVELS[level]
        agent.tools = tuple(_stira_tools(bound, WRITE_NAMES, level, hitl_enabled=True))
        agent(f"turn at {level}")
        hashes.append(agent.last_prompt_cache["tools_hash"])
        tool_lists.append(model.requests[-1]["tool_names"])
        if level == "normal":
            status, text = _result(agent, "w-normal")
            assert status == "success" and "Memory written" in text
        if level == "warning":
            status, text = _result(agent, "w-warning")
            assert status == "error" and BREAKER_WRITE_REFUSED in text
    assert hashes[0] is not None and len(set(hashes)) == 1
    assert tool_lists[0] == tool_lists[1] == tool_lists[2]
    assert "memory_write" in tool_lists[1]
