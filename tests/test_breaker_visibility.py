"""#78: the chat server's system prompt shows the live breaker level/actions.

Offline: Bedrock and the Strands Agent are replaced; the StiraEngine,
ContextAssembler and MemoryStore are real.
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path
from unittest.mock import Mock

import pytest

from stiracore.agent import nxp_agent
from stiracore.agent.context_assembler import ContextAssembler
from stiracore.core import BreakLevel, NeuralState, StiraConfig, StiraEngine
from stiracore.tools.memory_store import MemoryStore

ROOT = Path(__file__).resolve().parent.parent

# Level -> (stability, expected action names). Defaults: warning<0.5, critical<0.3,
# critical_action="readonly".
CASES = {
    BreakLevel.NORMAL: (0.9, []),
    BreakLevel.WARNING: (0.4, ["INJECT_IDENTITY", "REQUIRE_CONFIRMATION", "NOTIFY_OPERATOR"]),
    BreakLevel.CRITICAL: (0.2, ["BLOCK_WRITES", "INJECT_IDENTITY", "NOTIFY_OPERATOR"]),
}


@pytest.fixture(scope="module")
def stira_server():
    """Load dashboard/stira_server.py without running create_app()."""
    spec = importlib.util.spec_from_file_location(
        "stira_server_under_test", ROOT / "dashboard" / "stira_server.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _breaker_section(prompt: str) -> dict:
    match = re.search(r"## Breaker\n(\{.*?\n\})", prompt, re.DOTALL)
    assert match, f"Breaker section missing or not a snapshot:\n{prompt}"
    return json.loads(match.group(1))


class _NullStore:
    def list_entries(self, layer, limit=20):
        return []


@pytest.mark.parametrize("level", list(CASES))
def test_breaker_section_renders_level_and_named_actions(tmp_path, level):
    stability, action_names = CASES[level]
    engine = StiraEngine(state=NeuralState(stability=stability), config=StiraConfig())
    assembler = ContextAssembler(
        "stira",
        _NullStore(),
        engine,
        tmp_path / "missing.md",
        breaker_state_provider=engine.breaker_status,
    )
    prompt = assembler.build_system_prompt()
    assert "Unavailable" not in prompt
    snapshot = _breaker_section(prompt)
    assert snapshot == {"level": level.value, "actions": action_names}


def test_provider_uses_breaker_status_and_never_pre_turn(tmp_path):
    engine = StiraEngine(state=NeuralState(stability=0.4))
    engine.pre_turn = Mock(side_effect=AssertionError("must not call pre_turn"))
    spy = Mock(wraps=engine.breaker_status)
    assembler = ContextAssembler(
        "stira", _NullStore(), engine, tmp_path / "x.md", breaker_state_provider=spy
    )
    assembler.build_turn_context()
    assembler.build_system_prompt()
    assert spy.call_count == 2
    engine.pre_turn.assert_not_called()


def test_breaker_status_is_read_only():
    fired = []
    engine = StiraEngine(state=NeuralState(stability=0.2), on_breaker=lambda *a: fired.append(a))
    engine.pre_turn()  # enter CRITICAL: first protected turn
    engine.post_turn(stimulus=None)
    fired.clear()
    before = (engine._turn_count, engine._critical_turns, engine._last_break_level)

    results = {engine.breaker_status() for _ in range(10)}

    assert results == {engine.breaker_status()}
    assert (engine._turn_count, engine._critical_turns, engine._last_break_level) == before
    assert fired == []


def test_breaker_status_returns_guarded_turn_snapshot():
    engine = StiraEngine(state=NeuralState(stability=0.2))
    snapshot = engine.pre_turn()
    engine.state.stability = 0.99  # mid-turn change must not leak into this turn
    assert engine.breaker_status() is snapshot
    engine.post_turn(stimulus=None)


def test_fallback_without_provider_is_unchanged(tmp_path):
    engine = StiraEngine()
    assembler = ContextAssembler("stira", _NullStore(), engine, tmp_path / "x.md")
    assert (
        "Unavailable: no read-only breaker snapshot has been supplied."
        in assembler.build_turn_context()
    )


def test_include_identity_false_drops_static_identity_section(tmp_path):
    engine = StiraEngine(state=NeuralState(stability=0.4))
    with_identity = ContextAssembler("p", _NullStore(), engine, tmp_path / "x.md")
    without = ContextAssembler("p", _NullStore(), engine, tmp_path / "x.md", include_identity=False)
    assert "## Identity Anchors" in with_identity.build_system_prompt()
    assert "## Identity Anchors" not in without.build_system_prompt()


# --- Integration: chat server wiring through NXPAgent ---------------------------


@pytest.fixture
def wired(tmp_path, monkeypatch, stira_server):
    monkeypatch.setattr(nxp_agent, "BedrockModel", Mock(return_value=Mock()))
    calls = []

    def build_agent(**kwargs):
        calls.append(kwargs)
        inner = Mock(return_value="offline reply")
        inner.messages = list(kwargs.get("messages") or [])
        inner.system_prompt = kwargs.get("system_prompt", "")
        return inner

    monkeypatch.setattr(nxp_agent, "Agent", Mock(side_effect=build_agent))

    def file_read():
        return "read"

    file_read.tool_spec = {"name": "file_read"}

    def file_write():
        return "write"

    file_write.tool_spec = {"name": "file_write"}

    classifier = Mock()
    classifier.classify.return_value = None
    agent = nxp_agent.NXPAgent(
        model_id="offline",
        region_name="ap-northeast-1",
        state=NeuralState(stability=0.9),
        config=StiraConfig(blocked_tool_names=frozenset({"file_write"})),
        tools=[file_read, file_write],
        stimulus_classifier=classifier,
    )
    (tmp_path / "memory").mkdir()
    store = MemoryStore(tmp_path / "memory", "stira")
    soul = tmp_path / "SOUL.md"
    soul.write_text("I am Resolver.", encoding="utf-8")
    stira_server._wire_system_prompt(agent, store, soul)
    return agent, calls


def test_prompt_breaker_matches_enforced_tools_each_turn(wired):
    agent, calls = wired
    sequence = [BreakLevel.NORMAL, BreakLevel.WARNING, BreakLevel.CRITICAL]
    for level in sequence:
        # Hysteresis: CRITICAL must be entered from below its threshold.
        agent.state.stability = CASES[level][0]
        agent("hello")

    assert len(calls) == len(sequence)
    for level, call in zip(sequence, calls, strict=True):
        prompt = call["system_prompt"]
        snapshot = _breaker_section(prompt)
        tool_names = {nxp_agent._tool_name(t) for t in call["tools"]}
        assert snapshot["level"] == level.value
        assert snapshot["actions"] == CASES[level][1]

        writes_restricted = bool(
            {"BLOCK_WRITES", "REQUIRE_CONFIRMATION"} & set(snapshot["actions"])
        )
        # What the prompt tells the model is exactly what the router enforced.
        assert ("file_write" in tool_names) is (not writes_restricted)
        assert "file_read" in tool_names

        # Identity: only NXPAgent's live notice, never a second static copy.
        assert "## Identity Anchors" not in prompt
        assert prompt.count("[STIRA SAFETY NOTICE") == (0 if level is BreakLevel.NORMAL else 1)
        assert "I am Resolver." in prompt


def test_prompt_is_not_frozen_at_startup(wired):
    agent, calls = wired
    startup = agent.system_prompt
    assert _breaker_section(startup)["level"] == "normal"
    agent.state.stability = 0.2
    agent("hello")
    assert _breaker_section(calls[-1]["system_prompt"])["level"] == "critical"
