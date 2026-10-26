"""Real SDK delegation/Swarm tests with deterministic offline model streams."""

import asyncio
import json
from unittest.mock import Mock

import pytest
from strands.models import Model

from stiracore.agent.delegation import (
    create_nxp_swarm,
    register_spawn_tool,
    register_subagent_tool,
)
from stiracore.agent.nxp_agent import NXPAgent
from stiracore.agent.subagent import create_nxp_subagent
from stiracore.core import BreakLevel, NeuralState


class ScriptedModel(Model):
    def __init__(self, responses=None):
        self.responses = list(responses or [[{"text": "done"}]])
        self.calls = []

    def get_config(self):
        return {"model_id": "offline"}

    def update_config(self, **kwargs):
        pass

    async def structured_output(self, *args, **kwargs):
        yield {}

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        self.calls.append(tool_specs or [])
        response = self.responses.pop(0)
        yield {"messageStart": {"role": "assistant"}}
        reason = "end_turn"
        for block in response:
            if "toolUse" in block:
                reason = "tool_use"
                use = block["toolUse"]
                yield {
                    "contentBlockStart": {
                        "start": {
                            "toolUse": {
                                "name": use["name"],
                                "toolUseId": use["toolUseId"],
                            }
                        }
                    }
                }
                yield {
                    "contentBlockDelta": {
                        "delta": {
                            "toolUse": {
                                "input": json.dumps(use["input"]),
                            }
                        }
                    }
                }
            else:
                yield {"contentBlockStart": {"start": {}}}
                yield {"contentBlockDelta": {"delta": block}}
            yield {"contentBlockStop": {}}
        yield {"messageStop": {"stopReason": reason}}


@pytest.fixture
def parent(monkeypatch):
    monkeypatch.setattr("stiracore.agent.nxp_agent.BedrockModel", lambda **kwargs: ScriptedModel())
    return NXPAgent("offline", "us-east-1", stimulus_classifier=Mock(classify=lambda **kw: None))


def child(parent, name="worker", **kwargs):
    return create_nxp_subagent(
        parent, "task", name=name, stimulus_classifier=Mock(classify=lambda **kw: None), **kwargs
    )


def test_parent_child_state_config_and_history_are_independent(parent):
    worker = child(parent, state=parent.state)
    assert worker.state is not parent.state
    assert worker.config is not parent.config
    assert worker.config.identity_anchors is not parent.config.identity_anchors
    assert worker.engine is not parent.engine
    worker.state.stability = 0.1
    worker.agent.state.set("nxp", {"stability": 0.01})
    assert parent.state.stability == NeuralState().stability
    assert worker.state.stability == 0.1  # JSON state cannot mutate the live engine
    assert parent.agent is None


def test_child_breaker_does_not_propagate_to_parent(parent):
    worker = child(parent)
    worker.state.stability = 0.1
    worker.config.critical_action = "halt"
    with pytest.raises(RuntimeError, match="halted"):
        worker()
    assert worker.engine.breaker_status().level == BreakLevel.CRITICAL
    assert parent.engine.breaker_status().level == BreakLevel.NORMAL
    assert not worker.model.calls


def test_completion_callback_receives_result_after_post_turn(parent):
    calls = []
    worker = child(parent, on_complete=lambda c, r: calls.append((c, str(r))))
    post = Mock(wraps=worker.engine.post_turn)
    worker.engine.post_turn = post
    result = worker()
    assert str(result).strip() == "done"
    assert calls == [(worker, "done\n")]
    post.assert_called_once()
    assert worker.agent.state.get("nxp")["stability"] == worker.state.stability


@pytest.mark.asyncio
async def test_agent_as_tool_returns_result_and_parent_blocks_alias(parent):
    worker = child(parent)
    delegate = register_subagent_tool(parent, worker, name="research")
    events = [
        event
        async for event in delegate.stream(
            {"toolUseId": "call-1", "name": "research", "input": {"input": "task"}},
            {"agent": None},
        )
    ]
    assert events[-1]["tool_result"]["content"] == [{"text": "done\n"}]
    assert "research" in parent.config.blocked_tool_names
    parent.state.stability = 0.4
    tools, _ = parent._prepare_turn(parent.engine.pre_turn())
    assert delegate not in tools
    parent.engine.post_turn(stimulus=None)


@pytest.mark.asyncio
async def test_make_subagent_creates_fresh_workers_and_returns_result(parent):
    pytest.importorskip("strands_harness")
    completed = []
    delegate = register_spawn_tool(parent, on_complete=lambda c, r: completed.append(c))
    for index in range(2):
        events = [
            event
            async for event in delegate.stream(
                {"toolUseId": str(index), "name": "subagent", "input": {"task": "focused task"}},
                {"agent": None},
            )
        ]
        assert events[-1]["tool_result"]["status"] == "success"
    assert len(completed) == 2
    assert completed[0].state is not completed[1].state
    assert completed[0].agent.state.get("subagent_depth") == 0
    assert parent.agent is None


def test_swarm_handoff_runs_multiple_independent_children(parent):
    first = child(parent, "researcher")
    second = child(parent, "writer")
    first.model.responses = [
        [
            {
                "toolUse": {
                    "name": "handoff_to_agent",
                    "toolUseId": "handoff-1",
                    "input": {"agent_name": "writer", "message": "write the result"},
                }
            }
        ],
        [{"text": "handed off"}],
    ]
    post1 = Mock(wraps=first.engine.post_turn)
    post2 = Mock(wraps=second.engine.post_turn)
    first.engine.post_turn = post1
    second.engine.post_turn = post2
    swarm = create_nxp_swarm([first, second], entry_point=first, max_iterations=3)
    result = swarm("research and write")
    assert set(result.results) == {"researcher", "writer"}
    assert first.state is not second.state
    assert first.agent.state is not second.agent.state
    post1.assert_called_once()
    post2.assert_called_once()
    assert parent.agent is None


@pytest.mark.asyncio
async def test_separate_children_can_run_concurrently(parent):
    first, second = child(parent, "one"), child(parent, "two")
    results = await asyncio.gather(first.agent.invoke_async("a"), second.agent.invoke_async("b"))
    assert [str(r).strip() for r in results] == ["done", "done"]
    first.state.stability = 0.1
    assert second.state.stability > 0.5


def test_warning_filters_child_writes_and_swarm_handoff(parent):
    from strands import tool

    @tool
    def file_write() -> str:
        """Write a file."""
        return "written"

    worker = child(parent, tools=[file_write], state=NeuralState(stability=0.4))
    create_nxp_swarm([worker])
    worker()
    assert "file_write" not in {t["name"] for t in worker.model.calls[0]}
    assert "handoff_to_agent" not in {t["name"] for t in worker.model.calls[0]}
    assert "file_write" in worker.agent.tool_registry.registry


def test_nxp_session_continues_messages_with_latest_state_and_soul(parent, tmp_path):
    from strands.storage import InMemoryStorage

    from stiracore.agent.session_manager import NXPSessionManager
    from stiracore.agent.state_persistence import StatePersistence

    storage = InMemoryStorage()
    path = tmp_path / "state.json"
    soul = tmp_path / "soul.md"
    soul.write_text("old soul")
    state = NeuralState()
    persistence = StatePersistence(state, path)
    manager = NXPSessionManager(
        "conversation",
        state_persistence=persistence,
        system_prompt_provider=lambda: soul.read_text(),
        storage=storage,
    )
    original = NXPAgent(
        "offline",
        "us-east-1",
        state=state,
        session_manager=manager,
        stimulus_classifier=Mock(classify=lambda **kw: None),
    )
    original("remember this")
    soul.write_text("latest soul")
    state.stability = 0.4
    persistence.save()
    fresh_state = NeuralState()
    fresh_persistence = StatePersistence(fresh_state, path)
    restored = NXPAgent(
        "offline",
        "us-east-1",
        state=fresh_state,
        session_manager=NXPSessionManager(
            "conversation",
            state_persistence=fresh_persistence,
            system_prompt_provider=lambda: soul.read_text(),
            storage=storage,
        ),
        stimulus_classifier=Mock(classify=lambda **kw: None),
    )
    restored("continue")
    assert len(restored.agent.messages) == 4
    assert restored.agent.messages[0]["content"][0]["text"] == "remember this"
    assert "latest soul" in restored.agent.system_prompt
    assert "old soul" not in restored.agent.system_prompt
    assert restored.engine.breaker_status().level == BreakLevel.WARNING
    assert restored.agent.state.get("nxp")["stability"] == fresh_state.stability
    restored.model.responses.append([{"text": "third turn"}])
    restored("again")
    assert len(restored.agent.messages) == 6


def test_failed_child_does_not_report_completion_and_closes_turn(parent):
    done = Mock()
    worker = child(parent, on_complete=done)
    worker.model.responses.clear()
    post = Mock(wraps=worker.engine.post_turn)
    worker.engine.post_turn = post
    with pytest.raises(IndexError):
        worker()
    post.assert_called_once_with(stimulus=None)
    done.assert_not_called()


@pytest.mark.asyncio
async def test_spawn_depth_limit_refuses_before_constructing_child(parent):
    pytest.importorskip("strands_harness")
    delegate = register_spawn_tool(parent, max_depth=1)
    exhausted = child(parent)
    exhausted.agent.state.set("subagent_depth", 0)
    events = [
        event
        async for event in delegate.stream(
            {"toolUseId": "depth", "name": "subagent", "input": {"task": "nested"}},
            {"agent": exhausted.agent},
        )
    ]
    assert events[-1]["tool_result"]["status"] == "error"
    assert "depth limit" in events[-1]["tool_result"]["content"][0]["text"]


@pytest.mark.asyncio
async def test_cancelled_child_closes_turn_without_success_callback(parent):
    from threading import Event

    done = Mock()
    worker = child(parent, on_complete=done)
    signal = Event()
    signal.set()
    result = await worker.agent.invoke_async("cancel me", cancel_signal=signal)
    assert result.stop_reason == "cancelled"
    assert worker.engine.breaker_status().level == BreakLevel.NORMAL
    done.assert_not_called()


def test_graph_uses_guarded_child_agent(parent):
    from strands.multiagent import GraphBuilder

    worker = child(parent)
    builder = GraphBuilder()
    builder.add_node(worker.agent, "worker")
    builder.set_entry_point("worker")
    result = builder.build()("task")
    assert "worker" in result.results
    assert worker.agent.state.get("nxp")["stability"] == worker.state.stability


def test_parent_model_invokes_registered_child_and_receives_tool_result(parent):
    worker = child(parent)
    register_subagent_tool(parent, worker, name="review_task")
    parent.model.responses = [
        [
            {
                "toolUse": {
                    "name": "review_task",
                    "toolUseId": "review-1",
                    "input": {"input": "review"},
                }
            }
        ],
        [{"text": "review received"}],
    ]
    result = parent("Delegate this review")
    assert str(result).strip() == "review received"
    returned = [
        block["toolResult"]
        for message in parent.agent.messages
        for block in message["content"]
        if "toolResult" in block
    ]
    assert returned[0]["status"] == "success"
    assert returned[0]["content"] == [{"text": "done\n"}]
    assert len(worker.agent.messages) == 2
    assert worker.agent.state is not parent.agent.state


def test_spawn_without_optional_harness_gives_actionable_error(parent, monkeypatch):
    import builtins

    original = builtins.__import__

    def without_harness(name, *args, **kwargs):
        if name == "strands_harness" or name.startswith("strands_harness."):
            raise ModuleNotFoundError("optional Harness not installed")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_harness)
    with pytest.raises(ImportError, match=r"stiracore\[harness\]"):
        register_spawn_tool(parent)
    # No registration or policy mutation occurs on dependency failure.
    assert parent.tools == ()
