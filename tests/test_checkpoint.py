"""Checkpoint storage, policy-preserving recovery and real SDK tool replay."""

import json
import os
from copy import deepcopy
from unittest.mock import Mock

import pytest
from strands import tool
from strands.hooks import BeforeToolCallEvent, HookProvider
from strands.types.exceptions import EventLoopException
from test_subagent import ScriptedModel

from stiracore.agent.nxp_agent import NXPAgent
from stiracore.core import (
    BreakAction,
    BreakLevel,
    Checkpoint,
    FileCheckpointStore,
    NeuralState,
    StiraConfig,
    StiraEngine,
)


def snapshot(timestamp=1.0, thread_id="agent"):
    return Checkpoint(
        NeuralState(trust=0.823456, bc_window=[0.8, 0.7], emotion_transcription="記憶"),
        [{"role": "user", "content": [{"text": "hello"}]}],
        [{"toolUseId": "t1", "name": "read", "input": {"path": "notes.md"}}],
        timestamp,
        thread_id,
    )


def test_round_trip_and_detached_snapshot(tmp_path):
    store = FileCheckpointStore(tmp_path)
    original = snapshot()
    checkpoint_id = store.save(original)
    assert store.load(checkpoint_id) == original
    original.neural_state.trust = 0.1
    original.conversation_history.clear()
    assert store.load(checkpoint_id).neural_state.trust == 0.823456
    assert store.load(checkpoint_id).conversation_history
    state = NeuralState()
    history = [{"role": "user", "content": [{"text": "before"}]}]
    saved = Checkpoint(state, history, [], 1, "agent")
    history[0]["content"].clear()
    state.stability = 0.1
    assert saved.neural_state.stability == 0.7
    assert saved.conversation_history[0]["content"]


def test_multimodal_bytes_and_tag_collision_round_trip(tmp_path):
    store = FileCheckpointStore(tmp_path)
    checkpoint = snapshot()
    checkpoint.conversation_history[0]["content"].extend(
        [{"image": {"source": {"bytes": b"\x00\xffimage"}}}, {"$bytes": "literal"}]
    )
    assert store.load(store.save(checkpoint)) == checkpoint


def test_multiple_generations_and_per_thread_cleanup(tmp_path):
    store = FileCheckpointStore(tmp_path, max_checkpoints=3)
    other = store.save(snapshot(0, "other"))
    ids = [store.save(snapshot(index)) for index in range(6)]
    assert store.list("agent") == ids[-3:][::-1]
    assert store.list("other") == [other]
    assert len(list(tmp_path.glob("*.json"))) == 4
    with pytest.raises(FileNotFoundError):
        store.load(ids[0])
    store.max_checkpoints = 1
    assert store.cleanup("agent") == 2
    store.delete(ids[-1])
    store.delete(ids[-1])
    assert store.list("agent") == []


@pytest.mark.parametrize("identifier", ["../escape", "/absolute", "", "a" * 32 + "/x"])
def test_reject_path_traversal(tmp_path, identifier):
    store = FileCheckpointStore(tmp_path)
    with pytest.raises(ValueError):
        store.load(identifier)
    with pytest.raises(ValueError):
        store.delete(identifier)


def test_invalid_schema_and_atomic_write_failure(tmp_path, monkeypatch):
    store = FileCheckpointStore(tmp_path)
    checkpoint_id = store.save(snapshot())
    old = store.load(checkpoint_id)
    monkeypatch.setattr("stiracore.core.checkpoint.os.replace", Mock(side_effect=OSError("disk")))
    with pytest.raises(OSError, match="disk"):
        store.save(snapshot(2))
    assert store.load(checkpoint_id) == old
    assert list(tmp_path.glob("*.tmp")) == []
    path = tmp_path / f"{checkpoint_id}.json"
    document = json.loads(path.read_text())
    document["schema_version"] = 99
    path.write_text(json.dumps(document))
    with pytest.raises(ValueError, match="schema"):
        store.load(checkpoint_id)


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_private_file_permissions(tmp_path):
    store = FileCheckpointStore(tmp_path / "private")
    checkpoint_id = store.save(snapshot())
    assert (store.base_dir / f"{checkpoint_id}.json").stat().st_mode & 0o077 == 0
    assert store.base_dir.stat().st_mode & 0o077 == 0


@pytest.mark.parametrize("action", ["halt", "readonly"])
def test_critical_checkpoint_precedes_notification(tmp_path, action):
    store = FileCheckpointStore(tmp_path)
    notifications = []
    engine = StiraEngine(
        NeuralState(stability=0.1),
        StiraConfig(critical_action=action),
        checkpoint_store=store,
        agent_id="agent",
        on_breaker=lambda *args: notifications.append(store.list("agent")),
    )
    result = engine.pre_turn(conversation_history=snapshot().conversation_history)
    assert result.level == BreakLevel.CRITICAL
    assert bool(result.actions & BreakAction.HALT) == (action == "halt")
    assert notifications == [[engine.last_checkpoint_id]]
    checkpoint = store.load(engine.last_checkpoint_id)
    assert checkpoint.neural_state == engine.state
    assert checkpoint.conversation_history == snapshot().conversation_history


def test_resume_exact_state_shared_reference_and_cooldown(tmp_path):
    store = FileCheckpointStore(tmp_path)
    shared = NeuralState(stability=0.1)
    engine = StiraEngine(shared, checkpoint_store=store, agent_id="agent")
    engine.pre_turn()
    # Recovery has begun, but CRITICAL cooldown must survive process restart.
    shared.stability = 0.8
    engine.record_agreement(True)
    checkpoint_id = engine.save_checkpoint(
        conversation_history=snapshot().conversation_history,
        pending_tool_calls=snapshot().pending_tool_calls,
    )
    expected = deepcopy(shared)
    engine.post_turn("threat")
    engine.resume_from_checkpoint(checkpoint_id)
    assert engine.state is shared
    assert shared == expected
    assert engine.conversation_history == snapshot().conversation_history
    assert engine.pending_tool_calls == snapshot().pending_tool_calls
    restarted = StiraEngine(checkpoint_store=store, agent_id="agent")
    restarted.resume_from_checkpoint(checkpoint_id)
    assert restarted.state == expected
    assert restarted.breaker_status().level == BreakLevel.CRITICAL
    assert restarted._critical_turns == 1
    assert restarted._agreement_window == [True]
    other = StiraEngine(checkpoint_store=store, agent_id="other")
    before = deepcopy(other.state)
    with pytest.raises(ValueError, match="another"):
        other.resume_from_checkpoint(checkpoint_id)
    assert other.state == before


def offline_agent(monkeypatch, tmp_path, responses=None, **kwargs):
    model = ScriptedModel(responses)
    monkeypatch.setattr("stiracore.agent.nxp_agent.BedrockModel", lambda **kw: model)
    return NXPAgent(
        "offline",
        "us-east-1",
        agent_id="agent",
        checkpoint_store=FileCheckpointStore(tmp_path),
        stimulus_classifier=Mock(classify=lambda **kw: None),
        **kwargs,
    )


def test_halt_checkpoint_replay_rechecks_operation_level_policy(monkeypatch, tmp_path):
    agent = offline_agent(
        monkeypatch,
        tmp_path,
        [[{"text": "first"}], [{"text": "replayed"}]],
        state=NeuralState(stability=0.1),
        config=StiraConfig(critical_action="halt"),
    )
    assert str(agent("keep this request")).strip() == "first"
    checkpoint_id = agent.engine.last_checkpoint_id
    agent.resume_from_checkpoint(checkpoint_id)
    assert str(agent()).strip() == "replayed"
    assert len(agent.model.calls) == 2
    assert agent.engine.conversation_history == [
        {"role": "user", "content": [{"text": "keep this request"}]}
    ]


def test_real_sdk_snapshots_before_tools_and_replays_without_model_regeneration(
    monkeypatch, tmp_path
):
    calls = []
    pending = {"toolUseId": "t1", "name": "remember", "input": {"value": "hello"}}

    @tool
    def remember(value: str) -> str:
        """Record a value."""
        checkpoint = agent.engine.checkpoint_store.load(agent.engine.last_checkpoint_id)
        assert checkpoint.pending_tool_calls == [pending]
        assert checkpoint.conversation_history[-1]["content"] == [{"toolUse": pending}]
        calls.append(value)
        return value

    agent = offline_agent(
        monkeypatch, tmp_path, [[{"toolUse": pending}], [{"text": "done"}]], tools=[remember]
    )
    assert str(agent("remember hello")).strip() == "done"
    checkpoint_id = agent.engine.last_checkpoint_id
    assert calls == ["hello"]
    # New process/agent with only a final model response. SDK executes the stored
    # toolUse first; it must not ask the model to regenerate the tool request.
    restarted = offline_agent(monkeypatch, tmp_path, [[{"text": "resumed"}]], tools=[remember])
    restarted.resume_from_checkpoint(checkpoint_id)
    assert str(restarted()).strip() == "resumed"
    assert calls == ["hello", "hello"]  # documented at-least-once replay
    assert len(restarted.model.calls) == 1


def test_crash_before_tool_keeps_replayable_batch(monkeypatch, tmp_path):
    pending = {"toolUseId": "t1", "name": "read", "input": {}}
    agent = offline_agent(monkeypatch, tmp_path, [[{"toolUse": pending}]])

    class Crash(HookProvider):
        def register_hooks(self, registry, **kwargs):
            registry.add_callback(BeforeToolCallEvent, self.fail)

        def fail(self, event):
            raise RuntimeError("simulated interruption")

    original = agent._snapshot_context

    def install(inner, prompt):
        original(inner, prompt)
        inner.hooks.add_hook(Crash())

    monkeypatch.setattr(agent, "_snapshot_context", install)
    with pytest.raises(EventLoopException, match="simulated interruption"):
        agent("read")
    checkpoint = agent.engine.checkpoint_store.load(agent.engine.last_checkpoint_id)
    assert checkpoint.pending_tool_calls == [pending]
    assert checkpoint.conversation_history[-1]["content"] == [{"toolUse": pending}]
