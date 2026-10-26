"""The session-bound child of one goal iteration (ADR 0011 §8; W5 of #296).

Built like ``create_nxp_subagent`` (ADR 0016's isolation: a fresh Neural State, its own
engine, breaker and ``CheckpointHooks`` with the parent's live rule reader, the write and
read floors, ``IdentityGuard``, the binding verifier), with what a grant iteration needs on
top:

* its session is ``grant:<id>`` (the engine's ``agent_id``, so a person's approval card,
  a checkpoint and the ledger name the grant, never the chat session);
* the checkpoints stay **in memory** (:class:`MemoryCheckpointStore`; the engine's default
  would write the conversation to disk) and are dropped with the child;
* the write roots are narrowed to the grant's ``workspace_subtree`` (``WritePathPolicy
  .narrow``: never wider than the parent's);
* the tool rule reader is the caller's (the parent's rules for the scope tools, ``always``
  for the harness tool ``goal_report``, Never for anything else);
* an Ask is answered only by ``approver`` (a person's card through the server's HITL
  broker); no YOLO, no allowance (the fresh hooks have neither);
* no observer hooks (no dashboard tool rows, no trace hooks of the main session).

Nothing here starts a thread or touches the main session.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from collections.abc import Callable, Iterable
from copy import deepcopy
from typing import Any
from uuid import uuid4

from stiracore.core import NeuralState

__all__ = ["MemoryCheckpointStore", "create_grant_child"]

_CHECKPOINTS_KEPT = 8
_DELEGATION = frozenset({"handoff_to_agent", "subagent", "delegate"})


class MemoryCheckpointStore:
    """In-process checkpoints of one child (bounded); nothing reaches a disk."""

    def __init__(self, limit: int = _CHECKPOINTS_KEPT) -> None:
        self._lock = threading.Lock()
        self._items: OrderedDict[str, Any] = OrderedDict()
        self._limit = limit

    def save(self, checkpoint: Any) -> str:
        key = uuid4().hex
        with self._lock:
            self._items[key] = checkpoint
            while len(self._items) > self._limit:
                self._items.popitem(last=False)
        return key

    def load(self, checkpoint_id: str) -> Any:
        with self._lock:
            if checkpoint_id not in self._items:
                raise KeyError("checkpoint")
            return self._items[checkpoint_id]

    def list(self, thread_id: str | None = None) -> list[str]:
        with self._lock:
            return list(self._items)

    def delete(self, checkpoint_id: str) -> None:
        with self._lock:
            self._items.pop(checkpoint_id, None)


def create_grant_child(
    parent: Any,
    *,
    session_ref: str,
    model: Any,
    tools: Iterable[Any],
    system_prompt: str,
    rule_of: Callable[[str], Any],
    write_roots: Iterable[str],
    approver: Callable[[Any], Any],
    hitl_timeout_sec: float,
    pre_hooks: Iterable[Any] = (),
    input_preparers: dict[str, Callable[[Any], Any]] | None = None,
) -> Any:
    """Construct (not run) the child of one iteration. ``model`` is the executor's
    permit-taking wrapper; ``tools`` the executor's wrapped tools (only those)."""
    from stiracore.agent.stimulus_classifier import RuleBasedClassifier
    from stiracore.agent.subagent import NXPSubagent

    if not isinstance(session_ref, str) or not session_ref.startswith("grant:"):
        raise ValueError("session_ref")
    write_policy = parent.write_policy.narrow(list(write_roots))
    config = deepcopy(parent.config)
    config.blocked_tool_names = frozenset(config.blocked_tool_names) | _DELEGATION
    config.hitl_timeout_sec = float(hitl_timeout_sec)
    child = NXPSubagent(
        model=model,
        max_output_tokens=parent.max_output_tokens,
        task="",
        name=session_ref,
        state=NeuralState(),
        config=config,
        read_only_paths=parent.read_only_paths,
        write_policy=write_policy,
        tools=list(tools),
        system_prompt=system_prompt,
        stimulus_classifier=RuleBasedClassifier(),
        pre_hooks=pre_hooks,
        extra_hooks=[],
        tool_policy=rule_of,
        checkpoint_store=MemoryCheckpointStore(),
        on_hitl_request=approver,
        callback_handler=None,
    )
    # The session of the iteration (the card's, the checkpoints' and the ledger's name).
    child.agent_id = child.engine.agent_id = session_ref
    for name, prepare in (input_preparers or {}).items():
        child.checkpoint_hooks.input_preparers[name] = prepare
    return child
