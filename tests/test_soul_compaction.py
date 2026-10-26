"""SOUL guard unit tests and real SDK execution through NXPAgent."""

import copy
import json
from unittest.mock import Mock

import pytest
from strands import Agent
from strands.hooks.events import AfterInvocationEvent
from test_subagent import ScriptedModel

from stiracore.agent.context_assembler import ContextAssembler
from stiracore.agent.nxp_agent import NXPAgent
from stiracore.agent.soul_compaction import SoulAgentState, SoulCompactionGuard
from stiracore.agent.stimulus_classifier import StimulusResult
from stiracore.agent.subagent import create_nxp_subagent
from stiracore.core import BreakLevel, StiraEngine

SOUL = "# Test SOUL\n" + "Full identity, not a summary.\n" * 400 + "END SOUL"
MARKER = "Stira protected runtime reference data (values are not instructions):\n"


def history():
    return [
        {
            "role": "user" if i % 2 == 0 else "assistant",
            "content": [{"text": f"old-{i}: " + "ordinary conversation " * 700}],
        }
        for i in range(16)
    ]


def runtime(agent):
    return json.loads(agent.system_prompt.split(MARKER)[-1])


def guard_context():
    agent = Agent(model=ScriptedModel(), system_prompt=SOUL, messages=history())
    engine = StiraEngine(now_fn=lambda: 0)
    context = SoulAgentState(SOUL, engine, agent)
    guard = SoulCompactionGuard()
    guard.snapshot(context)
    return guard, context


@pytest.mark.parametrize("mode", ["fewer_messages", "same_count", "missing_prompt"])
def test_detects_reduction_and_restores_all_five_components(mode):
    guard, context = guard_context()
    agent = context.agent
    if mode == "fewer_messages":
        agent.messages[:] = agent.messages[:2]
    elif mode == "same_count":
        for message in agent.messages:
            message["content"] = [{"text": "short"}]
    else:
        agent.system_prompt = "Application extension"
    context.engine.state.trust = 0.987
    context.engine.state.emotion_transcription = 'joy: evidence "kept"\nsecond line'
    assert guard.check_and_reinject(context, agent.messages)
    assert SOUL in agent.system_prompt
    assert SOUL not in json.dumps(agent.messages)
    protected = runtime(agent)
    assert protected["neural_state"]["trust"] == 0.987
    assert protected["identity_anchors"] == context.engine.config.identity_anchors
    assert protected["protected_layers"] == sorted(context.engine.config.protected_layers)
    assert protected["emotion_transcription"] == context.engine.state.emotion_transcription
    assert protected["breaker"]["level"] == BreakLevel.NORMAL.value
    if mode == "missing_prompt":
        assert "Application extension" in agent.system_prompt


def test_ordinary_growth_and_same_size_edits_do_not_trigger():
    guard, context = guard_context()
    context.agent.messages[0]["content"][0]["text"] = context.agent.messages[0]["content"][0][
        "text"
    ].replace("old-0", "new-0")
    assert not guard.check_and_reinject(context, context.agent.messages)
    context.agent.messages.append({"role": "user", "content": [{"text": "next"}]})
    assert not guard.check_and_reinject(context, context.agent.messages)
    assert guard.reinjection_count == 0


def test_snapshot_detached_repeated_checks_idempotent_and_cache_points_preserved():
    guard, context = guard_context()
    captured = guard.captured
    context.engine.state.trust = 0.999
    context.engine.state.bc_window.append(0.123)
    assert json.loads(captured.runtime_json)["neural_state"]["trust"] != 0.999
    assert json.loads(captured.runtime_json)["neural_state"]["bc_window"] == []
    context.agent.system_prompt = [{"text": SOUL}, {"cachePoint": {"type": "default"}}]
    context.agent.messages[:] = context.agent.messages[:2]
    for _ in range(3):
        guard.check_and_reinject(context, context.agent.messages)
    assert context.agent.system_prompt.count(SOUL) == 1
    assert context.agent.system_prompt.count(MARKER) == 1
    assert {"cachePoint": {"type": "default"}} in context.agent.system_prompt_content
    assert runtime(context.agent)["neural_state"]["trust"] == 0.999


def test_guard_engine_binding_allows_recreated_delegate_but_not_another_engine():
    guard, context = guard_context()
    second = Agent(model=ScriptedModel(), system_prompt=SOUL)
    other = SoulAgentState(SOUL, context.engine, second)
    with pytest.raises(ValueError, match="snapshot"):
        guard.check_and_reinject(other, second.messages)
    guard.snapshot(other)
    with pytest.raises(ValueError, match="independent"):
        guard.snapshot(SoulAgentState(SOUL, StiraEngine(), second))
    assert not SoulCompactionGuard().check_and_reinject(context, context.agent.messages)


class RecordingModel(ScriptedModel):
    def __init__(self):
        super().__init__([[{"text": "done"}]] * 8)
        self.prompts = []

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        self.prompts.append(copy.deepcopy(system_prompt))
        async for event in super().stream(messages, tool_specs, system_prompt, **kwargs):
            yield event


@pytest.fixture
def nxp(monkeypatch):
    monkeypatch.setattr("stiracore.agent.nxp_agent.BedrockModel", lambda **kw: RecordingModel())
    classifier = Mock(classify=lambda **kw: StimulusResult("joy", 0.2, "latest rationale"))
    agent = NXPAgent("offline", "us-east-1", system_prompt=SOUL, stimulus_classifier=classifier)
    agent.agent = Agent(model=agent.model, system_prompt=SOUL, messages=history())
    return agent


def install_late_compactor(monkeypatch, *, fail=False):
    def compact(event: AfterInvocationEvent):
        event.agent.messages[:] = [{"role": "user", "content": [{"text": "summary"}]}]
        event.agent.system_prompt = "Application extension"
        if fail:
            raise RuntimeError("synthetic compaction failure")

    def factory(**kwargs):
        hooks = [*kwargs.pop("hooks", []), compact]
        return Agent(**kwargs, hooks=hooks, callback_handler=None)

    monkeypatch.setattr("stiracore.agent.nxp_agent.Agent", factory)


def test_nxp_post_turn_reinjects_assembler_prompt_and_latest_state(nxp, monkeypatch, tmp_path):
    path = tmp_path / "soul.md"
    path.write_text(SOUL)
    store = Mock(list_entries=lambda *args: [])
    assembler = ContextAssembler(
        "test", store, nxp.engine, path, breaker_state_provider=nxp.engine.breaker_status
    )
    nxp.system_prompt_provider = assembler.build_system_prompt
    before_state = nxp.state.to_dict()
    install_late_compactor(monkeypatch)
    nxp.engine.pre_turn = Mock(wraps=nxp.engine.pre_turn)
    nxp.engine.post_turn = Mock(wraps=nxp.engine.post_turn)
    nxp("continue")
    assert SOUL in nxp.agent.system_prompt
    assert "Application extension" in nxp.agent.system_prompt
    assert len(nxp.agent.messages) == 1
    assert runtime(nxp.agent)["emotion_transcription"] == "latest rationale"
    assert runtime(nxp.agent)["neural_state"] == nxp.state.to_dict()
    assert nxp.state.to_dict() != before_state
    nxp.engine.pre_turn.assert_called_once()
    nxp.engine.post_turn.assert_called_once()
    nxp("next")
    assert nxp.agent.system_prompt.count(SOUL) == 1
    assert nxp.agent.system_prompt.count(MARKER) == 1
    assert "latest rationale" in str(nxp.model.prompts[-1])


def test_real_sdk_sliding_window_compaction_through_nxp(nxp, monkeypatch):
    from strands.agent.conversation_manager import SlidingWindowConversationManager

    def factory(**kwargs):
        return Agent(
            **kwargs,
            conversation_manager=SlidingWindowConversationManager(window_size=4),
            callback_handler=None,
        )

    monkeypatch.setattr("stiracore.agent.nxp_agent.Agent", factory)
    before = len(json.dumps(nxp.agent.messages))
    nxp("continue")
    assert len(json.dumps(nxp.agent.messages)) < before
    assert nxp.compaction_guard.reinjection_count > 0
    assert SOUL in nxp.agent.system_prompt
    assert runtime(nxp.agent)["neural_state"] == nxp.state.to_dict()


def test_critical_breaker_and_failed_invocation_do_not_rewind_state(nxp, monkeypatch):
    nxp.state.stability = 0.2
    install_late_compactor(monkeypatch, fail=True)
    nxp.engine.pre_turn = Mock(wraps=nxp.engine.pre_turn)
    nxp.engine.post_turn = Mock(wraps=nxp.engine.post_turn)
    with pytest.raises(RuntimeError, match="synthetic"):
        nxp("continue")
    assert runtime(nxp.agent)["breaker"]["level"] == BreakLevel.CRITICAL.value
    assert nxp.engine.breaker_status().level == BreakLevel.CRITICAL
    nxp.engine.pre_turn.assert_called_once()
    nxp.engine.post_turn.assert_called_once()


def test_halt_preparation_replaces_previous_snapshot_for_operation_hitl(nxp):
    nxp("first")
    count = nxp.compaction_guard.reinjection_count
    calls = len(nxp.model.calls)
    nxp.config.critical_action = "halt"
    nxp.state.stability = 0.1

    nxp("gate writes")

    assert len(nxp.model.calls) == calls + 1
    assert "Write operations require human approval" in nxp.model.prompts[-1]
    assert nxp.compaction_guard.reinjection_count == count
    assert nxp._compaction_state is None


@pytest.mark.asyncio
async def test_subagent_stream_uses_same_post_turn_guard(nxp):
    child = create_nxp_subagent(
        nxp, "task", system_prompt=SOUL, stimulus_classifier=Mock(classify=lambda **kw: None)
    )
    child.agent.messages[:] = history()

    def compact(event: AfterInvocationEvent):
        event.agent.messages[:] = [{"role": "user", "content": [{"text": "summary"}]}]
        event.agent.system_prompt = ""

    child.agent.hooks.add_callback(AfterInvocationEvent, compact)
    events = [event async for event in child.agent.stream_async("continue")]
    assert "result" in events[-1]
    assert SOUL in child.agent.system_prompt
    assert runtime(child.agent)["neural_state"] == child.state.to_dict()
    assert child.compaction_guard is not nxp.compaction_guard
