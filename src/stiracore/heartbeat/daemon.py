"""Opt-in background driver for :meth:`HeartbeatGateway.tick` (#238).

The daemon only schedules the existing zero-model tick (decay, snapshot,
breaker query). It never calls a model, sends a notification or acts on its
own; scheduling of operator-defined jobs belongs to #239.

Guarantees:

* one owner per state directory (an OS file lock, released by the kernel if
  the process dies, so a crash never leaves a stale lock behind). The lock
  excludes heartbeats only; it does not make a process the sole state writer;
* an injectable clock. A clock that moves backwards keeps the *remaining*
  wait measured at the last reading (no stall until the old timestamp, no
  re-waiting already elapsed time, a pending failure backoff is kept); decay
  itself resynchronizes in ``NeuralState.apply_decay``.
  A long sleep is caught up with a single, baseline-clamped tick;
* a failing tick never kills the loop: consecutive failures back off
  exponentially (bounded, no overflow) and are reported as ``degraded``;
  an unexpected loop error ends the thread *and* releases the lock, so the
  status never claims a running heartbeat that is gone;
* one lifecycle lock guards a state machine STOPPED → STARTING → RUNNING →
  STOPPING → STOPPED. ``start()`` is a transaction (any failure rolls back
  to STOPPED and releases the owner lock); ``stop(timeout)`` uses a single
  monotonic deadline for every wait and never blocks past it. If a tick is
  still in flight it returns ``False`` (STOPPING) and the owner lock stays
  held until that tick ends, which then completes the stop by itself;
* while the background worker runs it is the only ticker: an external
  ``poll()`` is refused, in the same lifecycle-lock section that reserves the
  tick (no check-then-act gap a restart could slip into);
* every ``start()`` opens a new *generation* ``(incarnation, number)``. The
  incarnation is 128 random bits drawn once per process (never read back
  from storage); the number only grows within it. A ``stop()`` targets the
  generation that was current when it was called: it never finishes a later
  one, and it waits for the end of its generation on an event (not by
  re-acquiring the lifecycle lock), so it returns within its deadline;
* a stop request is recorded on its generation *before* the lifecycle lock
  is taken. If the lock stays busy past the deadline, ``stop()`` returns
  ``False`` but the request is kept: the end of ``start()``, the worker and
  every ``poll()`` honour it, so it is never lost. Recording a request and
  reserving a tick (check "no request" + set ``_ticking``) are one step under
  a leaf *request lock*, the single linearization point: a request recorded
  first means no tick; a tick reserved first is in flight and the stop
  completes after it. The request lock is held only for that in-memory
  check-and-set (never across a clock reading, I/O or another lock), so
  ``stop()`` takes it without a timeout. Lock order: lifecycle → request.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections.abc import Callable
from enum import Enum
from pathlib import Path
from typing import Any

from stiracore.authority import incarnation as _process_incarnation
from stiracore.owner_lock import (  # noqa: F401 - re-exported for older importers
    _CONTENTION_ERRNOS,
    OwnerLockFile,
    UnsafeLockFileError,
    _try_lock,
    _unlock,
)

from .gateway import HeartbeatGateway

logger = logging.getLogger(__name__)

LOCK_FILENAME = "heartbeat.lock"
# 2**_MAX_BACKOFF_EXPONENT * any sane base already exceeds every cap.
_MAX_BACKOFF_EXPONENT = 32

# Per-process start identity (ADR 0011 §5 "Incarnation"): 128 CSPRNG bits, regenerated
# in a forked child, never persisted or read back. Generation numbers are only unique
# together with it. The single source is ``stiracore.authority.incarnation`` (W1, #296):
# the authority service and the YOLO desk carry the same value.
INCARNATION = _process_incarnation.current()


def _new_incarnation() -> None:
    global INCARNATION
    INCARNATION = _process_incarnation.current()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_new_incarnation)


class HeartbeatLockError(RuntimeError):
    """Another heartbeat already owns this state directory."""


class HeartbeatLockFileUnsafeError(HeartbeatLockError, UnsafeLockFileError):
    """``heartbeat.lock`` is not a plain file of ours (#322); nothing was written.

    Also a :class:`HeartbeatLockError`, so callers that catch that (the heartbeat does
    not start) keep working; catch :class:`UnsafeLockFileError` first to tell the two
    apart.
    """


class HeartbeatOwnerLock(OwnerLockFile):
    """Exclusive, non-blocking owner lock on ``<state_dir>/heartbeat.lock``.

    The shared owner-lock implementation (:mod:`stiracore.owner_lock`, #322): the
    file is opened without following a symlink and must be a regular, singly linked
    file of this user, else :class:`UnsafeLockFileError` and nothing is written.
    ``filename`` lets the scheduler use the same class for ``scheduler.lock``.
    """

    def __init__(self, state_dir: str | Path, filename: str = LOCK_FILENAME) -> None:
        super().__init__(Path(state_dir) / filename)

    def _contended(self) -> BaseException:
        return HeartbeatLockError(
            f"heartbeat already running for this state directory ({self.path.name})"
        )

    def _unsafe(self, reason: str) -> BaseException:
        return HeartbeatLockFileUnsafeError(self.path, reason)


class DaemonState(str, Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"


class _Generation:
    """One ``start()`` .. STOPPED span; its events are leaf locks (no lock held across)."""

    __slots__ = ("key", "stop_requested", "stopped")

    def __init__(self, number: int) -> None:
        self.key = (INCARNATION, number)
        self.stop_requested = threading.Event()
        self.stopped = threading.Event()


class HeartbeatDaemon:
    """Run ``gateway.tick()`` at its adaptive interval until stopped."""

    def __init__(
        self,
        gateway: HeartbeatGateway,
        *,
        state_dir: str | Path,
        now_fn: Callable[[], float] | None = None,
        on_tick: Callable[[dict[str, Any]], None] | None = None,
        failure_backoff_sec: float = 60.0,
        max_backoff_sec: float | None = None,
        warn_after_failures: int = 3,
        min_wait_sec: float = 1.0,
        max_wait_sec: float = 30.0,
    ) -> None:
        self.gateway = gateway
        self._now = now_fn or time.time
        self._on_tick = on_tick
        self._owner_lock = HeartbeatOwnerLock(state_dir)
        self._failure_backoff_sec = max(0.0, float(failure_backoff_sec))
        configured_cap = (
            max_backoff_sec
            if max_backoff_sec is not None
            else gateway.gateway_config.hb_interval_ceil_sec
        )
        # Effective cap: max(failure_backoff_sec, max_backoff_sec or ceil).
        self._backoff_cap_sec = max(self._failure_backoff_sec, float(configured_cap))
        self._warn_after = max(1, int(warn_after_failures))
        self._min_wait_sec = max(0.0, float(min_wait_sec))
        # Re-read the clock at least this often, so a wall-clock jump or a
        # host sleep is noticed without waiting out a multi-hour interval.
        self._max_wait_sec = max(self._min_wait_sec, float(max_wait_sec))

        # The single lifecycle lock: state machine, owner lock, schedule and
        # status fields. Never held while a tick or on_tick runs.
        self._lock = threading.Condition(threading.Lock())
        # Leaf lock: the stop-request record vs. the tick reservation (see module doc).
        self._request_lock = threading.Lock()
        self._state = DaemonState.STOPPED
        self._background = False
        self._ticking = False
        self._ticker: threading.Thread | None = None  # the thread running the tick
        self._generation: _Generation | None = None
        self._generation_count = 0
        self._wake = threading.Event()  # interrupts the worker's wait
        self._thread: threading.Thread | None = None
        self._next_due: float | None = None
        self._last_seen: float | None = None  # latest clock reading
        self._last_tick_at: float | None = None
        self._last_level: str | None = None
        self._last_priority: str | None = None
        self._last_interval: float | None = None
        self._last_duration_ms: float | None = None
        self._tick_count = 0
        self._failures = 0
        self._clock_resyncs = 0
        self._last_error: str | None = None

    # -- lifecycle -----------------------------------------------------------

    @property
    def state(self) -> DaemonState:
        return self._state

    @property
    def running(self) -> bool:
        return self._state is DaemonState.RUNNING

    @property
    def stopping(self) -> bool:
        return self._state is DaemonState.STOPPING

    @property
    def generation(self) -> tuple[str, int] | None:
        """``(incarnation, number)`` of the latest ``start()``; ``None`` before the first."""
        generation = self._generation
        return generation.key if generation is not None else None

    def start(self, *, background: bool = True) -> None:
        """Acquire the owner lock and schedule the first tick immediately.

        A transaction: if anything fails (owner lock, first clock reading,
        thread creation/start) the daemon is back to STOPPED and the owner
        lock is released. Refused unless STOPPED (e.g. while STOPPING).
        ``background=False`` leaves the scheduling to explicit :meth:`poll`
        calls (deterministic tests with a fake clock).
        """
        with self._lock:
            if self._state is not DaemonState.STOPPED:
                raise RuntimeError(f"heartbeat daemon is {self._state.value}")
            self._state = DaemonState.STARTING
            # First, so a stop() racing this start targets the new generation.
            self._generation_count += 1
            with self._request_lock:
                self._generation = _Generation(self._generation_count)
            try:
                self._owner_lock.acquire()
                now = self._now()
                self._wake.clear()
                self._next_due = self._last_seen = now
                self._last_error = None
                self._background = background
                if background:
                    thread = threading.Thread(target=self._run, name="stira-heartbeat", daemon=True)
                    # The worker blocks on this lock until the transaction ends.
                    thread.start()
                    self._thread = thread
                self._state = DaemonState.RUNNING
            except BaseException:
                self._finish_locked()
                raise
            # A stop() that found the lock busy during this transaction (kept request).
            self._apply_stop_request_locked()

    def stop(self, timeout: float = 5.0) -> bool:
        """Request a stop; ``True`` once STOPPED within ``timeout`` seconds.

        All waits share one monotonic deadline. ``False`` means the stop is
        pending: the lifecycle lock was busy, or a tick (or ``on_tick``) is
        still in flight. The request is kept on its generation either way: no
        new tick starts, the owner lock stays held until the in-flight tick
        (or the next lifecycle step) completes the stop. A stop never ends a
        generation started after it was called.
        """
        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._request_lock:  # leaf, held briefly: no deadline needed
            generation = self._generation  # the target of this stop
            if generation is None:
                return True  # never started
            generation.stop_requested.set()  # kept even if the lock below is busy
        self._wake.set()
        if not self._lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
            return False
        try:
            if self._generation is not generation or self._state is DaemonState.STOPPED:
                return True  # our generation ended (a later one is not ours to stop)
            self._apply_stop_request_locked()
            if self._state is DaemonState.STOPPED:
                return True
            thread = self._thread if self._background else None
            ticker = self._ticker
        finally:
            self._lock.release()
        current = threading.current_thread()
        if thread is None:  # poll()-driven: the in-flight poll completes the stop
            if ticker is current:
                return False  # called from on_tick: the poll stops after it
            return generation.stopped.wait(max(0.0, deadline - time.monotonic()))
        if thread is current:
            return False  # called from on_tick: the worker stops after it
        thread.join(max(0.0, deadline - time.monotonic()))
        return not thread.is_alive()

    def _apply_stop_request_locked(self) -> None:
        """Caller holds the lifecycle lock. Honour a kept stop request of this generation."""
        generation = self._generation
        if generation is None or not generation.stop_requested.is_set():
            return
        if self._state is DaemonState.RUNNING:
            self._state = DaemonState.STOPPING
            self._wake.set()
        if self._state is DaemonState.STOPPING and not self._background and not self._ticking:
            self._finish_locked()  # poll-driven, nothing in flight

    def _finish_locked(self) -> None:
        """Caller holds the lifecycle lock. -> STOPPED, owner lock released."""
        self._state = DaemonState.STOPPED
        self._background = False
        self._next_due = None
        self._owner_lock.release()
        if self._generation is not None:
            self._generation.stopped.set()
        self._lock.notify_all()

    # -- scheduling ----------------------------------------------------------

    def poll(self) -> bool:
        """Tick if one is due; return whether a tick (or failed attempt) ran.

        Only for ``start(background=False)``: while the background worker
        runs it is the only ticker, so an external call is refused.
        """
        return self._poll(external=True)

    def _poll(self, *, external: bool = False) -> bool:
        # One lifecycle-lock section from the "who may tick" check to the
        # reservation: a restart cannot slip in between (#238 review, TOCTOU).
        with self._lock:
            if external and self._background:
                raise RuntimeError("heartbeat daemon is driven by its background worker")
            if self._state is not DaemonState.RUNNING or self._ticking:
                return False
            now = self._now()
            if self._last_seen is not None and now < self._last_seen:
                self._rebase_locked(now)
            self._last_seen = now
            due = self._next_due is None or now >= self._next_due
            # The linearization point against stop(): check and reserve as one step
            # under the request lock. A stop recorded meanwhile (even one that found
            # the lifecycle lock busy, or came during a slow clock reading) wins.
            with self._request_lock:
                requested = self._generation.stop_requested.is_set()
                if due and not requested:
                    self._ticking = True
                    self._ticker = threading.current_thread()
            if requested:
                self._apply_stop_request_locked()
                return False
            if not due:
                return False
        try:
            self._tick_once(now)
        finally:
            with self._lock:
                self._ticking = False
                self._ticker = None
                self._apply_stop_request_locked()  # requested during the tick
                if self._state is DaemonState.STOPPING and not self._background:
                    # A poll-driven stop that timed out completes here.
                    self._finish_locked()
                self._lock.notify_all()
        return True

    def _rebase_locked(self, now: float) -> None:
        """The clock moved backwards: keep the wait remaining at the last reading.

        Without this the loop would stall until the old timestamp; ticking
        at once would defeat a pending failure backoff; re-applying the whole
        delay would re-wait time that had already elapsed.
        """
        self._clock_resyncs += 1
        if self._next_due is not None and self._last_seen is not None:
            remaining = max(0.0, self._next_due - self._last_seen)
            self._next_due = now + remaining
        logger.info("heartbeat: clock moved backwards; schedule re-based")

    def _tick_once(self, now: float) -> None:
        started = time.perf_counter()
        try:
            result = self.gateway.tick()
            # tick() alone: the span that holds the turn/heartbeat state lock
            # (plus the snapshot sink); on_tick persistence is not included.
            duration_ms = (time.perf_counter() - started) * 1000.0
            if self._on_tick is not None:
                self._on_tick(result)
            interval = float(result["next_interval_sec"])
        except Exception as error:  # noqa: BLE001 - a failing tick must not kill the loop
            self._record_failure(now, error)
            return
        with self._lock:
            if self._failures:
                logger.info("heartbeat recovered after %d failed tick(s)", self._failures)
            self._failures = 0
            self._last_error = None
            self._tick_count += 1
            self._last_tick_at = now
            self._last_level = result["level"]
            self._last_priority = result["priority"]
            self._last_interval = interval
            self._last_duration_ms = round(duration_ms, 3)
            self._next_due = now + interval

    def backoff_delay(self, failures: int) -> float:
        """Delay after ``failures`` consecutive failures; bounded, never overflows."""
        exponent = min(max(0, failures - 1), _MAX_BACKOFF_EXPONENT)
        return min(self._failure_backoff_sec * (2.0**exponent), self._backoff_cap_sec)

    def _record_failure(self, now: float, error: Exception) -> None:
        with self._lock:
            self._failures += 1
            failures = self._failures
            # Class name only: an exception message may carry state or paths.
            self._last_error = type(error).__name__
            delay = self.backoff_delay(failures)
            self._next_due = now + delay
        if failures >= self._warn_after:
            logger.warning(
                "heartbeat tick failed %d times in a row (%s); backing off %.0fs",
                failures,
                type(error).__name__,
                delay,
            )
        else:
            logger.info(
                "heartbeat tick failed (%s); retrying in %.0fs", type(error).__name__, delay
            )

    def _run(self) -> None:
        try:
            while True:
                # Before the request check in _poll: a stop() that sets its request
                # after that check sets the wake after this clear (no lost wake-up,
                # no busy loop on a stale one).
                self._wake.clear()
                self._poll()
                with self._lock:
                    if self._state is not DaemonState.RUNNING:
                        break
                    due = self._next_due
                wait = min(max(due - self._now(), self._min_wait_sec), self._max_wait_sec)
                self._wake.wait(wait)
        except BaseException as error:  # noqa: BLE001 - report, then release below
            with self._lock:
                self._last_error = f"loop:{type(error).__name__}"
            logger.error("heartbeat loop ended unexpectedly (%s)", type(error).__name__)
        finally:
            # The worker is the only ticker in background mode, so its exit is
            # the one point where no tick can still touch state.
            with self._lock:
                self._finish_locked()

    # -- read-only view ------------------------------------------------------

    def status(self) -> dict[str, Any]:
        """Read-only snapshot for the dashboard; no field controls the loop."""
        with self._lock:
            running = self._state is DaemonState.RUNNING
            return {
                "enabled": True,
                "state": self._state.value,
                "running": running,
                "stopping": self._state is DaemonState.STOPPING,
                "last_tick_at": self._last_tick_at,
                "next_tick_at": self._next_due if running else None,
                "interval_sec": self._last_interval,
                "last_level": self._last_level,
                "last_priority": self._last_priority,
                "last_duration_ms": self._last_duration_ms,
                "tick_count": self._tick_count,
                "consecutive_failures": self._failures,
                "degraded": self._failures >= self._warn_after,
                "clock_resyncs": self._clock_resyncs,
                "last_error": self._last_error,
            }
