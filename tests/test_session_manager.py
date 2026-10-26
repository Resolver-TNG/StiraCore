"""Tests for NXPSessionManager: snapshot restore + live SOUL/NeuralState reinjection.

Uses the real Strands SDK (``strands.session.SnapshotSessionManager``,
``strands.storage.InMemoryStorage``/``LocalFileStorage``) with an offline,
in-process fake model — no network calls.
"""

import json
from collections.abc import AsyncGenerator, Sequence
from typing import Any

import pytest
from strands.agent.agent import Agent
from strands.storage import InMemoryStorage, LocalFileStorage
from strands.types.content import Messages
from strands.types.streaming import StreamEvent
from strands.types.tools import ToolSpec

from stiracore.agent.session_manager import NXPSessionManager
from stiracore.agent.state_persistence import StatePersistence
from stiracore.core import NeuralState


class _FakeModel:
    """Minimal offline ``Model`` stand-in: replays fixed assistant replies.

    Implements only what ``Agent`` needs to complete a turn (``stream``,
    ``format_request`` is unused since ``format_chunk``/config hooks are not
    invoked in this path). No network access; no external SDK test fixtures.
    """

    stateful = False

    def __init__(self, replies: Sequence[str]) -> None:
        self._replies = list(replies)
        self.index = 0

    def format_chunk(self, event: Any) -> StreamEvent:
        return event

    def get_config(self) -> Any:
        return {}

    def update_config(self, **model_config: Any) -> None:
        return None

    async def structured_output(self, *args: Any, **kwargs: Any) -> AsyncGenerator[Any, None]:
        return
        yield  # pragma: no cover - generator shape only

    async def stream(
        self,
        messages: Messages,
        tool_specs: list[ToolSpec] | None = None,
        system_prompt: str | None = None,
        tool_choice: Any | None = None,
        *,
        system_prompt_content: Any = None,
        **kwargs: Any,
    ) -> AsyncGenerator[dict[str, Any], None]:
        text = self._replies[self.index]
        self.index += 1
        yield {"messageStart": {"role": "assistant"}}
        yield {"contentBlockStart": {"start": {}}}
        yield {"contentBlockDelta": {"delta": {"text": text}}}
        yield {"contentBlockStop": {}}
        yield {"messageStop": {"stopReason": "end_turn"}}


def _texts(agent: Agent) -> list[str]:
    return [
        content["text"]
        for message in agent.messages
        for content in message["content"]
        if "text" in content
    ]


@pytest.fixture
def neural_state_path(tmp_path):
    return tmp_path / "neural_state.json"


@pytest.fixture
def persistence(neural_state_path):
    return StatePersistence(NeuralState(), neural_state_path)


def test_wraps_official_snapshot_session_manager():
    """NXPSessionManager is a SnapshotSessionManager, keeping its restore-by-id contract."""
    from strands.session.snapshot_session_manager import SnapshotSessionManager

    manager = NXPSessionManager("s1", storage=InMemoryStorage())
    assert isinstance(manager, SnapshotSessionManager)
    assert manager.session_id == "s1"


def test_conversation_restored_by_session_id_across_agents():
    """A second Agent built with the same session_id + storage resumes the conversation."""
    storage = InMemoryStorage()

    manager1 = NXPSessionManager("shared-session", storage=storage)
    agent1 = Agent(model=_FakeModel(["hello there"]), session_manager=manager1, agent_id="a1")
    agent1("hi")
    assert "hello there" in _texts(agent1)

    manager2 = NXPSessionManager("shared-session", storage=storage)
    agent2 = Agent(model=_FakeModel(["unused"]), session_manager=manager2, agent_id="a1")

    assert "hello there" in _texts(agent2)
    assert "hi" in _texts(agent2)


def test_initialize_reinjects_live_system_prompt(persistence):
    """After restore-on-init, agent.system_prompt reflects the provider, not the snapshot's."""
    storage = InMemoryStorage()
    calls = {"n": 0}

    def provider() -> str:
        calls["n"] += 1
        return f"LIVE SOUL v{calls['n']}"

    manager1 = NXPSessionManager(
        "sess",
        storage=storage,
        state_persistence=persistence,
        system_prompt_provider=provider,
    )
    agent1 = Agent(
        model=_FakeModel(["ok"]),
        session_manager=manager1,
        agent_id="a1",
        system_prompt="stale prompt frozen in snapshot",
    )
    agent1("go")

    # A fresh agent restoring the same session gets the *current* provider output,
    # not the prompt that was persisted in the snapshot.
    manager2 = NXPSessionManager(
        "sess",
        storage=storage,
        state_persistence=persistence,
        system_prompt_provider=provider,
    )
    agent2 = Agent(model=_FakeModel(["unused"]), session_manager=manager2, agent_id="a1")

    assert agent2.system_prompt == f"LIVE SOUL v{calls['n']}"
    assert agent2.system_prompt != "stale prompt frozen in snapshot"


def test_initialize_reloads_neural_state_from_disk_into_same_object(neural_state_path):
    """StatePersistence.load() is called on init and restores fields into the same object."""
    state = NeuralState()
    persistence = StatePersistence(state, neural_state_path)

    # Simulate a prior process having persisted a different trust value to disk.
    on_disk_state = NeuralState(trust=0.9)
    StatePersistence(on_disk_state, neural_state_path).save()

    assert state.trust != 0.9  # sanity: in-memory default differs from what's on disk

    manager = NXPSessionManager("sess", storage=InMemoryStorage(), state_persistence=persistence)
    agent = Agent(model=_FakeModel(["ok"]), session_manager=manager, agent_id="a1")
    agent("go")

    # Same object identity, fields updated from disk.
    assert persistence.state is state
    assert state.trust == 0.9


def test_restore_snapshot_reinjects_neural_state_and_prompt(neural_state_path):
    """Explicit restore_snapshot (time travel) also triggers reinjection."""
    import asyncio

    state = NeuralState()
    persistence = StatePersistence(state, neural_state_path)
    calls = {"n": 0}

    def provider() -> str:
        calls["n"] += 1
        return f"prompt-{calls['n']}"

    storage = InMemoryStorage()
    manager = NXPSessionManager(
        "sess",
        storage=storage,
        state_persistence=persistence,
        system_prompt_provider=provider,
    )
    agent = Agent(model=_FakeModel(["a", "b"]), session_manager=manager, agent_id="a1")
    agent("first")
    snapshot_id = asyncio.run(manager.save_snapshot(agent, is_latest=False))
    assert snapshot_id is not None

    # Mutate disk state after the checkpoint, then explicitly restore that checkpoint.
    StatePersistence(NeuralState(trust=0.15), neural_state_path).save()
    calls_before = calls["n"]

    restored = asyncio.run(manager.restore_snapshot(agent, snapshot_id=snapshot_id))

    assert restored is True
    assert state.trust == 0.15
    assert calls["n"] == calls_before + 1
    assert agent.system_prompt == f"prompt-{calls['n']}"


def test_reinject_runs_even_without_optional_hooks():
    """Manager works with neither state_persistence nor system_prompt_provider configured."""
    manager = NXPSessionManager("sess", storage=InMemoryStorage())
    agent = Agent(model=_FakeModel(["ok"]), session_manager=manager, agent_id="a1")
    # No exception on init or on an explicit restore of a nonexistent snapshot.
    import asyncio

    with pytest.raises(ValueError):
        asyncio.run(manager.restore_snapshot(agent, snapshot_id="not-a-uuid7"))


def test_new_session_has_no_prior_conversation_but_still_reinjects(persistence):
    """A brand-new session_id restores no messages, but still runs the reinjection hook."""

    def provider() -> str:
        return "fresh soul"

    manager = NXPSessionManager(
        "brand-new",
        storage=InMemoryStorage(),
        state_persistence=persistence,
        system_prompt_provider=provider,
    )
    agent = Agent(model=_FakeModel(["ok"]), session_manager=manager, agent_id="a1")

    assert agent.messages == []
    assert agent.system_prompt == "fresh soul"


def test_local_file_storage_round_trip(tmp_path, persistence):
    """End-to-end with LocalFileStorage (on disk), matching a real deployment path."""
    storage = LocalFileStorage(str(tmp_path / "sessions"))

    manager1 = NXPSessionManager("disk-sess", storage=storage, state_persistence=persistence)
    agent1 = Agent(model=_FakeModel(["remembered"]), session_manager=manager1, agent_id="a1")
    agent1("hi")

    manager2 = NXPSessionManager("disk-sess", storage=storage, state_persistence=persistence)
    agent2 = Agent(model=_FakeModel(["unused"]), session_manager=manager2, agent_id="a1")

    assert "remembered" in _texts(agent2)


def test_state_persistence_still_json_round_trips(neural_state_path):
    """Sanity: StatePersistence JSON format remains compatible; core logic unchanged."""
    state = NeuralState(trust=0.42, curiosity=0.77)
    persistence = StatePersistence(state, neural_state_path)
    persistence.save()

    raw = json.loads(neural_state_path.read_text())
    assert raw["trust"] == 0.42
    assert raw["curiosity"] == 0.77


def test_restore_overwrites_stale_sdk_neural_namespace(neural_state_path):
    import asyncio

    state = NeuralState(trust=0.2)
    persistence = StatePersistence(state, neural_state_path)
    persistence.save()
    manager = NXPSessionManager(
        "namespace", storage=InMemoryStorage(), state_persistence=persistence
    )
    agent = Agent(model=_FakeModel(["ok"]), session_manager=manager, agent_id="a")
    checkpoint = asyncio.run(manager.save_snapshot(agent, is_latest=False))
    StatePersistence(NeuralState(trust=0.9), neural_state_path).save()
    asyncio.run(manager.restore_snapshot(agent, snapshot_id=checkpoint))
    assert state.trust == 0.9
    assert agent.state.get("nxp")["trust"] == 0.9
    copied = agent.state.get("nxp")
    copied["trust"] = 0.1
    assert state.trust == 0.9


def test_sessions_do_not_share_conversations():
    storage = InMemoryStorage()
    first = Agent(
        model=_FakeModel(["private history"]),
        session_manager=NXPSessionManager("one", storage=storage),
        agent_id="a",
    )
    first("hello")
    second = Agent(
        model=_FakeModel(["unused"]),
        session_manager=NXPSessionManager("two", storage=storage),
        agent_id="a",
    )
    assert second.messages == []
