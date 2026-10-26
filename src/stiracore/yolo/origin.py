"""Which kind of turn a tool call belongs to (ADR 0012 §3, coverage by origin).

YOLO answers Ask only for a **human chat / Stage turn**. The chat route marks its turn
with :func:`human_turn`; every other origin — a webhook, a bot, a durable-approval
resume, a scheduler job, a sub-agent — leaves the mark unset, and an unset mark is never
covered (fail closed). A context variable, so the mark follows the turn into the SDK's
worker thread and its tool tasks (``contextvars.copy_context``) and never leaks into a
concurrent request of another thread.

The mark is bound to **its turn and its agent** (PR #435 review M2): it names the agent
the route ran (``agent_id``) and holds a token that is live only while the ``with``
block runs. A context copied during the turn and run after it (a task or an executor
job that outlives the turn) carries a dead token; a gate of another agent (a child
with its own engine) has another agent id — neither is covered. Paths checked in code
(2026-10): the SDK's worker thread and tool tasks run inside the turn; threads started
by tools, the scheduler, the approval-resume thread and the Discord bot start with an
empty context; ``NXPSubagent`` gates have no approver; the post-turn episode code calls
no tool. ``tests/test_yolo_auto_approve.py`` pins these.
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any

__all__ = ["HUMAN_TURN", "covered", "current", "human_turn", "started_by_person"]

HUMAN_TURN = "turn"


@dataclass(frozen=True, eq=False)
class _Mark:
    agent_id: Any  # the agent the route ran (``None``: unnamed)
    automatic: bool = False  # a window's automatic continue (ADR 0014), not a message


_ORIGIN: ContextVar[_Mark | None] = ContextVar("stira_yolo_origin", default=None)
_LIVE: set[_Mark] = set()  # the marks whose ``with`` block is running (by identity)
_LIVE_LOCK = threading.Lock()


def _live_mark() -> _Mark | None:
    mark = _ORIGIN.get()
    if mark is None:
        return None
    with _LIVE_LOCK:
        return mark if mark in _LIVE else None


def current() -> str | None:
    """``HUMAN_TURN`` inside a running human turn, else ``None``."""
    return HUMAN_TURN if _live_mark() is not None else None


def started_by_person() -> bool:
    """Inside a running human turn that a person's message started — not a window's
    automatic continue (ADR 0014 §4), which YOLO covers but nobody may be watching (PR
    #493 review round 2: ``delegate`` is offered only here)."""
    mark = _live_mark()
    return mark is not None and not mark.automatic


def covered(agent_id: Any = None) -> bool:
    """Whether a tool call of the gate of ``agent_id`` belongs to a running human turn
    of that same agent (exact equality; a dead or missing mark: no)."""
    mark = _live_mark()
    return mark is not None and mark.agent_id == agent_id


@contextmanager
def human_turn(agent_id: Any = None, *, automatic: bool = False) -> Iterator[None]:
    mark = _Mark(agent_id, automatic is True)
    with _LIVE_LOCK:
        _LIVE.add(mark)
    token = _ORIGIN.set(mark)
    try:
        yield
    finally:
        _ORIGIN.reset(token)
        with _LIVE_LOCK:
            _LIVE.discard(mark)
