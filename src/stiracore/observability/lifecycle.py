"""Ordered, bounded, idempotent stop steps for one server process (#285).

A server registers each resource *as it creates it* (before starting it, where it
has a thread), so a stop that arrives in the middle of start-up — e.g. SIGTERM
while ``create_app`` still runs — finds exactly what exists and nothing else.

* ``shutdown()`` runs the steps once, in ``order`` (ties: registration order).
  Each step runs on its own daemon thread and is waited for at most
  ``step_timeout`` seconds, the whole run at most ``total_timeout``; a step that
  raises, returns late or hangs is reported and the next one still runs.
* A second or concurrent ``shutdown()`` (``atexit`` after the entry point's own
  call, or another thread) does not run anything again: it waits for the first
  run (bounded) and returns its result.
* ``abort()`` is the failed-start path: it also runs the ``abort_only`` steps
  (closing stores a half-built app would otherwise leak in a long-lived process)
  and releases the owner lock — at most once, synchronously — but only if that
  run was an abort run and every step finished cleanly. After a normal
  ``shutdown()`` the abort-only steps never ran, so a later ``abort()`` keeps the
  lock. On the normal stop path the lock is left to the kernel, which releases
  it after the process has exited.
* Once a shutdown has started, :meth:`add` refuses new steps
  (:class:`LifecycleClosedError`): the caller must not start what nobody would stop.
* A step whose thread cannot be started (e.g. no new threads at interpreter
  shutdown) is reported as not run; it is never run inline without a bound.

Known limit: a resource is registered right after it is created (the owner lock,
the stores) or right before it is started (heartbeat, scheduler). A SIGTERM that
lands between creating a resource and registering it relies on that resource's
own error path and on process exit; the window is a few bytecodes, not atomic.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

# Labels a step may return that count as "finished" for abort()'s lock release.
CLEAN = frozenset({"closed", "stopped", "off"})


class LifecycleClosedError(RuntimeError):
    """A step was added after the shutdown had started; it is not registered."""


@dataclass(frozen=True)
class _Step:
    order: int
    seq: int
    name: str
    run: Callable[[float], str]
    abort_only: bool


class ServerLifecycle:
    def __init__(
        self,
        *,
        step_timeout: float = 4.0,
        total_timeout: float = 9.0,
        now_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self._step_timeout = max(0.0, float(step_timeout))
        self._total_timeout = max(0.0, float(total_timeout))
        self._now = now_fn
        self._lock = threading.Lock()
        self._steps: list[_Step] = []
        self._owner_lock: Any = None
        self._started = False
        self._done = threading.Event()
        self._result: dict[str, str] = {}
        self._aborted_run = False  # the one run that happened included abort_only steps
        self._release_lock = threading.Lock()
        self._lock_released = False

    def add(
        self,
        name: str,
        run: Callable[[float], str],
        *,
        order: int = 50,
        abort_only: bool = False,
    ) -> None:
        """Register ``run(timeout) -> label`` (e.g. ``"stopped"``, ``"stopping"``).

        Raises :class:`LifecycleClosedError` once a shutdown has started.
        """
        with self._lock:
            if self._started:
                raise LifecycleClosedError(f"shutdown already started; {name!r} not registered")
            self._steps.append(_Step(order, len(self._steps), name, run, abort_only))

    def own_lock(self, lock: Any) -> None:
        """The data-directory owner lock; released only by :meth:`abort`."""
        with self._lock:
            self._owner_lock = lock

    @property
    def stopped(self) -> bool:
        return self._done.is_set()

    def shutdown(self, *, abort: bool = False) -> dict[str, str]:
        with self._lock:
            first = not self._started
            self._started = True
            steps = sorted(self._steps, key=lambda step: (step.order, step.seq))
        if not first:
            if not self._done.wait(self._total_timeout + 1.0):
                return {"shutdown": "in progress"}
            return dict(self._result)
        result: dict[str, str] = {}
        self._aborted_run = abort
        try:
            deadline = self._now() + self._total_timeout
            for step in steps:
                if step.abort_only and not abort:
                    continue
                remaining = deadline - self._now()
                if remaining <= 0:
                    result[step.name] = "skipped (deadline)"
                    continue
                result[step.name] = self._run_bounded(step.run, min(self._step_timeout, remaining))
        finally:
            self._result = result
            self._done.set()
        return dict(result)

    def abort(self) -> dict[str, str]:
        """Failed start: stop and close what exists; release the lock if all is clean.

        The lock is released only when the shutdown that ran was this abort run (so
        the abort-only closes ran) and every step is clean; at most once.
        """
        result = self.shutdown(abort=True)
        with self._release_lock:
            lock = self._owner_lock
            if (
                lock is not None
                and not self._lock_released
                and self._done.is_set()
                and self._aborted_run
                and all(label in CLEAN for label in result.values())
            ):
                lock.release()
                self._lock_released = True
        return result

    @staticmethod
    def _run_bounded(run: Callable[[float], str], timeout: float) -> str:
        """Run one step on its own thread; never waits longer than ``timeout``.

        The step gets a slightly shorter budget so a step that honours it is
        normally joined in time; the join itself is ``timeout``, so the sum of the
        joins never exceeds the run's total deadline.
        """
        box: dict[str, str] = {}
        budget = max(0.0, timeout - min(0.2, timeout / 4))

        def target() -> None:
            try:
                box["label"] = str(run(budget))
            except Exception as error:  # noqa: BLE001 - reported, the next step still runs
                box["label"] = f"failed ({type(error).__name__})"

        try:
            thread = threading.Thread(target=target, name="stira-shutdown-step", daemon=True)
            thread.start()
        except RuntimeError as error:
            # e.g. no new threads at interpreter shutdown (atexit on 3.12+). Running it
            # inline would lose the bound: report it and go on with the next step.
            return f"not run ({type(error).__name__})"
        thread.join(timeout)
        if thread.is_alive():
            return "timed out"
        return box.get("label", "failed (unknown)")
