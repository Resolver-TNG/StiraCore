"""Runner lifecycle, persistence and isolation tests using the real NXP wrapper."""

from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest

from stiracore import (
    MemoryLayer,
    MemoryStore,
    NeuralState,
    NXPAgent,
    StatePersistence,
    StiraConfig,
)
from stiracore.integrations import runtime


def test_runner_real_lifecycle_and_episodic_save(monkeypatch, tmp_path):
    from stiracore.agent import nxp_agent

    monkeypatch.setattr(nxp_agent, "BedrockModel", Mock())
    monkeypatch.setattr(
        nxp_agent, "Agent", Mock(return_value=Mock(return_value="hello back", messages=[]))
    )
    agent = NXPAgent(
        model_id="test", region_name="test", stimulus_classifier=Mock(return_value=None)
    )
    agent._stimulus_classifier.classify.return_value = None
    store = MemoryStore(tmp_path / "memory", "test")
    persistence = StatePersistence(agent.state, tmp_path / "state.json")
    session = runtime.ChatSession(agent, store=store, persistence=persistence, source="discord")
    result = session.runner("hello")
    assert result.text == "hello back"
    assert agent.engine._turn_count == 1  # No double pre/post-turn.
    entries = list(store.list_entries(MemoryLayer.EPISODIC))
    assert len(entries) == 1 and "hello back" in entries[0].content
    assert (tmp_path / "state.json").is_file()
    for _ in range(3):
        session.status()
    assert agent.engine._turn_count == 1


def test_halt_runs_model_for_operation_gate_and_saves(monkeypatch, tmp_path):
    from stiracore.agent import nxp_agent

    monkeypatch.setattr(nxp_agent, "BedrockModel", Mock())
    instances = []

    def build_agent(**kwargs):
        inner = Mock(
            messages=[],
            system_prompt=kwargs.get("system_prompt", ""),
            return_value="safe reply",
        )
        instances.append(inner)
        return inner

    constructor = Mock(side_effect=build_agent)
    monkeypatch.setattr(nxp_agent, "Agent", constructor)
    state = NeuralState(stability=0.2)
    agent = NXPAgent(
        model_id="test",
        region_name="test",
        state=state,
        config=StiraConfig(critical_action="halt"),
        stimulus_classifier=Mock(classify=lambda **kwargs: None),
    )
    persistence = StatePersistence(state, tmp_path / "state.json")
    result = runtime.ChatSession(agent, persistence=persistence).runner("hello")
    constructor.assert_called_once()
    instances[0].assert_called_once_with("hello")
    assert result.text == "safe reply"
    assert result.after["breaker"] == "critical"
    assert agent.engine._turn_count == 1
    assert (tmp_path / "state.json").is_file()


def test_factory_isolates_memory_and_restores_state(monkeypatch, tmp_path):
    from stiracore.agent import nxp_agent
    from stiracore.config import AgentConfig, GatewayConfig

    monkeypatch.setattr(nxp_agent, "BedrockModel", Mock())
    monkeypatch.setenv("STIRA_CHAT_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(runtime.GatewayConfig, "load", lambda: GatewayConfig())
    monkeypatch.setattr(runtime.AgentConfig, "load", lambda: AgentConfig())
    first = runtime.create_session("slack", "T1:C1")
    second = runtime.create_session("slack", "T2:C1")
    assert first.agent is not second.agent
    assert first.agent.state is not second.agent.state
    assert first.agent.tools == ()
    first.agent.state.stability = 0.65
    first.persistence.save()
    restored = runtime.create_session("slack", "T1:C1")
    assert restored.agent.state.stability == 0.65
    assert second.agent.state.stability == NeuralState().stability
    assert "stability" in restored.agent.system_prompt_provider()
    assert len(list(tmp_path.glob("slack/*/neural-state.json"))) == 1


def test_registry_creation_is_thread_safe():
    factory = Mock(side_effect=lambda *args: object())
    registry = runtime.ChatSessions("slack", factory)
    with ThreadPoolExecutor(max_workers=4) as pool:
        sessions = list(pool.map(registry.get, ["T:C"] * 12))
    assert all(session is sessions[0] for session in sessions)
    factory.assert_called_once_with("slack", "T:C")
    assert registry.get("T:other") is not sessions[0]


def test_save_failure_preserves_reply(caplog):
    state = NeuralState()
    from stiracore.core import StiraEngine

    agent = Mock(state=state, engine=StiraEngine(state=state), return_value="reply")
    persistence = Mock()
    persistence.save.side_effect = OSError("private path")
    result = runtime.ChatSession(agent, persistence=persistence).runner("hello")
    assert result.text == "reply"
    assert "private path" not in caplog.text


@pytest.mark.parametrize("platform", ["slack", "discord"])
def test_tokenless_module_startup(platform, monkeypatch):
    # Subprocess verifies entry points work without constructing an AWS client.
    import os
    import subprocess
    import sys

    for name in ("SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "DISCORD_BOT_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    result = subprocess.run(
        [sys.executable, "-m", f"stiracore.integrations.{platform}_bot"],
        env=os.environ.copy(),
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0
    assert "skipped" in result.stderr
