"""Session management: snapshot-restore conversation + live NeuralState reinjection.

Wraps the official ``strands.session.SnapshotSessionManager`` so that, in
addition to the SDK's normal conversation/agent-state restore-by-session-id
behavior, every restore point also:

1. Reloads the latest on-disk :class:`~stiracore.core.NeuralState` into the
   *same* state object via :class:`~stiracore.agent.state_persistence.StatePersistence`
   (preserving object identity, so anything already holding a reference to the
   state — e.g. a running ``StiraEngine`` — observes the restored values).
2. Re-renders the agent's ``system_prompt`` from a supplied zero-argument
   prompt provider, so a freshly restored agent carries the current SOUL /
   identity / neural-state context rather than whatever was frozen into the
   persisted snapshot.

Both the automatic restore-on-init path (``initialize``) and the explicit
time-travel path (``restore_snapshot``) are covered, since either one can
hand the agent a stale ``system_prompt`` and a NeuralState object that has
drifted from disk.

NeuralState core logic is untouched: this module only calls
``StatePersistence.load()``, never mutates dimensions directly.

Recommended wiring (for the caller building an :class:`~strands.agent.Agent`
or an :class:`~stiracore.agent.nxp_agent.NXPAgent`):

    from strands.storage import LocalFileStorage
    from stiracore.agent.session_manager import NXPSessionManager
    from stiracore.agent.state_persistence import StatePersistence

    persistence = StatePersistence(state, Path("neural_state.json"))
    session_manager = NXPSessionManager(
        session_id="user-123",
        state_persistence=persistence,
        system_prompt_provider=context_assembler.build_system_prompt,
        storage=LocalFileStorage("./.strands/"),
    )

    # Plain strands.Agent:
    agent = Agent(session_manager=session_manager, ...)

    # NXPAgent: restore once before the first breaker evaluation; snapshots
    # are saved after post_turn, not during inner Agent execution.
    agent = NXPAgent(
        model_id="your-model", region_name="us-east-1", state=state,
        session_manager=session_manager, agent_id="assistant",
    )
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from strands.session.snapshot_session_manager import (
    MultiAgentSaveLatestStrategy,
    SaveLatestStrategy,
    SnapshotSessionManager,
    SnapshotTrigger,
)

from stiracore.agent.state_persistence import StatePersistence

if TYPE_CHECKING:
    from strands.agent.agent import Agent
    from strands.experimental.bidi.agent import BidiAgent
    from strands.storage.storage import Storage

__all__ = ["NXPSessionManager"]


class NXPSessionManager(SnapshotSessionManager):
    """``SnapshotSessionManager`` that reinjects live SOUL + NeuralState on restore.

    Conversation/agent-state restore-by-``session_id`` is delegated entirely to
    the wrapped ``SnapshotSessionManager`` (unchanged behavior: latest snapshot
    on init, explicit snapshot ids for time travel). This subclass only adds a
    post-restore hook that:

    - Reloads ``state_persistence``'s NeuralState from disk into the same
      object the engine already references.
    - Re-renders ``agent.system_prompt`` from ``system_prompt_provider`` (a
      zero-argument callable), so the restored agent uses the *current* SOUL/
      identity/neural-state context rather than the prompt frozen into the
      snapshot at save time.

    Both hooks are optional: omitting ``state_persistence`` or
    ``system_prompt_provider`` degrades gracefully to plain snapshot restore
    for that piece (no re-render, but conversation restore still runs).

    Args:
        session_id: Unique session identifier. Passed through to
            ``SnapshotSessionManager``; see its docstring for validation rules.
        state_persistence: Optional ``StatePersistence`` whose ``.load()`` is
            called after every restore point. Its wrapped ``NeuralState``
            object identity is preserved (fields are copied in place), so
            existing references (e.g. a running ``StiraEngine``) see the
            restored values without rebinding.
        system_prompt_provider: Optional zero-argument callable returning the
            current system prompt (typically
            ``ContextAssembler.build_system_prompt``). Called after every
            restore point and its result assigned to ``agent.system_prompt``.
        storage: Passed through to ``SnapshotSessionManager``. See its
            docstring: falls back to the agent's own storage, then
            ``LocalFileStorage``, when omitted.
        save_latest_on: Passed through to ``SnapshotSessionManager``.
        multi_agent_save_latest_on: Passed through to
            ``SnapshotSessionManager``.
        snapshot_trigger: Passed through to ``SnapshotSessionManager``.
        **kwargs: Forwarded to ``SnapshotSessionManager.__init__`` for future
            extensibility.
    """

    def __init__(
        self,
        session_id: str = "default-session",
        *,
        state_persistence: StatePersistence | None = None,
        system_prompt_provider: Callable[[], str] | None = None,
        storage: Storage | None = None,
        save_latest_on: SaveLatestStrategy = "invocation",
        multi_agent_save_latest_on: MultiAgentSaveLatestStrategy = "node",
        snapshot_trigger: SnapshotTrigger | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(
            session_id,
            storage=storage,
            save_latest_on=save_latest_on,
            multi_agent_save_latest_on=multi_agent_save_latest_on,
            snapshot_trigger=snapshot_trigger,
            **kwargs,
        )
        self.state_persistence = state_persistence
        self.system_prompt_provider = system_prompt_provider

    def initialize(self, agent: Agent | BidiAgent, **kwargs: Any) -> None:
        """Restore the agent from its latest snapshot, then reinject live context.

        Args:
            agent: Agent to restore.
            **kwargs: Forwarded to ``SnapshotSessionManager.initialize``.
        """
        super().initialize(agent, **kwargs)
        self._reinject(agent)

    async def restore_snapshot(self, agent: Agent, *, snapshot_id: str | None = None) -> bool:
        """Restore a specific (or latest) snapshot, then reinject live context.

        Args:
            agent: Agent to restore into.
            snapshot_id: Immutable snapshot id for time travel; omit for
                ``snapshot_latest`` (same as ``initialize``'s restore-on-init).

        Returns:
            True if a snapshot existed and was restored, False otherwise.
        """
        restored = await super().restore_snapshot(agent, snapshot_id=snapshot_id)
        self._reinject(agent)
        return restored

    def _reinject(self, agent: Any) -> None:
        """Reload NeuralState from disk and re-render the system prompt.

        Runs unconditionally after every restore point, including a no-op
        restore (fresh session, nothing on disk yet): ``StatePersistence.load``
        is a safe no-op when its file is missing, and re-rendering the system
        prompt from the current live context is correct either way.
        """
        if self.state_persistence is not None:
            self.state_persistence.inject_agent_state(agent, reload=True)
        if self.system_prompt_provider is not None:
            agent.system_prompt = self.system_prompt_provider()
