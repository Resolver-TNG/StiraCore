"""The one turn lock (ADR 0011 W6b of #296): every holder pre-empts a goal iteration.

Every turn on the server's agent and everything that must not overlap one (a person's
chat turn, a webhook turn, the automatic continue, the resume of a restored card, ``/new``,
a procedure approval) holds the same lock. :class:`TurnLock` is the only way to take it:
:meth:`TurnLock.acquire` raises the pre-emption signal **before** it tries the lock
(``preempt.arriving()``: the signal's counter moves and its "an acquirer is arriving"
mark is set until the attempt has ended), so

* an iteration already running sees the counter moved and yields at its next effect
  boundary (``yielded(user_turn)``; a waiting card is withdrawn at once);
* a press admitted at any point after the mark is set — while the acquirer waits, or
  in the instant between the signal and the lock — is refused (``user_turn``): the
  executor reads the mark first, then the lock;
* an effect that still finished while the turn ran is recorded as a residual.

Without a signal (the autonomy switches off: no executor) it is exactly the plain lock:
nothing else is signalled, read or changed. The signal is raised on every attempt, also
one that then finds the lock taken (the chat route's order since W5).
"""

from __future__ import annotations

import threading
from typing import Any

__all__ = ["TurnLock"]


class TurnLock:
    """The shared turn lock with its pre-emption signal (``preempt``: an object with
    ``arriving()`` — a context manager — or ``None``). A drop-in for ``threading.Lock``:
    ``acquire`` / ``release`` / ``locked`` / ``with`` give the plain lock's return values
    and raise its exception types (tested side by side). The plain lock inside is
    private."""

    def __init__(self, raw: Any = None, preempt: Any = None) -> None:
        self._plain_lock = threading.Lock() if raw is None else raw
        self._preempt = preempt

    @classmethod
    def of(cls, lock: Any) -> TurnLock:
        """``lock`` as a turn lock: itself when it is one; a plain lock (or ``None``: a
        new one) wrapped without a signal."""
        return lock if isinstance(lock, TurnLock) else cls(lock)

    @property
    def signalled(self) -> bool:
        return self._preempt is not None

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        preempt = self._preempt
        if preempt is None:
            return self._plain_lock.acquire(blocking, timeout)
        with preempt.arriving():
            return self._plain_lock.acquire(blocking, timeout)

    def release(self) -> None:
        self._plain_lock.release()

    def locked(self) -> bool:
        return self._plain_lock.locked()

    def __enter__(self) -> bool:
        # As a plain lock: ``with lock as value`` gives acquire()'s result (True).
        return self.acquire()

    def __exit__(self, *_exc: object) -> None:
        self.release()
