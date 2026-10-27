"""The process-wide authority service (ADR 0011 §5, §13 item 2; #296, W1).

One :class:`AuthorityService` per server process (:func:`install` at start-up,
:func:`current` everywhere else). It is the :class:`~.boundary.AuthorityBoundary` every
subsystem shares — today the YOLO desk (injected into ``YoloRequests``) and, without the
desk, the epoch sources below — plus the persisted epoch:

* **incarnation**: :func:`stiracore.authority.incarnation.current`, the same value as
  ``stiracore.heartbeat.daemon.INCARNATION``; never settable, never read back.
* **epoch at start**: the stored epoch + 1 (written before the service is used), or 1 on
  a first start (no file). Epoch values handed out but not yet written when the process
  dies (e.g. a ``SIGKILL`` before a deferred write) can be handed out again by the next
  process: identity is always ``(incarnation, epoch)``, never the epoch alone.
* **persistence**: after every narrowing (after its cancellations), outside the leaf lock
  and under ``_io_lock`` (``epoch_store.write``: ``0600``, fsync, seq + previous hash,
  atomic replace). Writes are coalesced: a write always stores the current in-memory
  epoch. Before every replace the current file is read again and must be the one this
  service wrote last (same device / inode, same records), else nothing is written.
* **unhealthy** (fail closed, sticky): an unreadable, unsafe, malformed or
  hash-mismatched file, or a sequence gap, at start or found before a write; a file
  replaced or changed behind the service (``epoch_store_replaced``); a failed write; a
  settle worker that cannot start (``epoch_settle_unavailable``) or fails
  (``epoch_settle_failed``). Every ``issue_permit`` / ``consume`` refuses
  (``authority_unhealthy``) and :meth:`AuthorityService.status` shows the reason code.
  Nothing clears it in the process — a later successful write does not; the evidence is
  never overwritten. A person repairs the file and restarts.
* **epoch sources**: :meth:`narrow` (a tool-rules save, ``tool_rules_saved``),
  :meth:`breaker_transition` (any breaker change away from NORMAL or between non-NORMAL
  levels, from the engine's ``on_breaker_transition`` hook), :meth:`kill` (the kill
  switch) and YOLO's own moves (``advance_epoch``, called under the YOLO request lock).

**Lock order.** Caller locks (scheduler, YOLO request lock, engine ``state_lock``) →
leaf lock. ``_io_lock`` is taken only with no leaf lock held; it is never taken by
``consume``.

**Settlement.** Every narrowing queues one batch ``(epoch, in-flight effects taken)`` in
the same leaf-lock step that moves the epoch. Exactly one *owner* at a time settles the
batches, in order: the settle worker (started on demand for :meth:`advance_epoch` /
:meth:`breaker_transition`, which run under foreign locks and never raise), a
synchronous :meth:`narrow` / :meth:`kill` (only when nobody else owns the queue; else
its batch waits for the current owner), or :meth:`flush_result` without a deadline when
no worker can start. Per batch: cancel, then advance the **frontier** (the newest epoch
whose cancellations, and all older ones, completed), then write the frontier — the file
never shows an epoch whose cancellations are still running (ADR 0011 §5 order). A worker
that cannot start latches ``epoch_settle_unavailable`` and leaves the work queued.
:meth:`flush_result` waits for every owner and reports ``clean`` only when nothing is
queued or running **and** the epoch is on disk (``unhealthy``: drained, not durable;
``pending``: work left at the deadline). The in-memory narrowing (epoch, kill flag) is
in effect before any I/O starts, and a failed write never undoes it.

**W2 preconditions.** A cancel callback must be non-blocking or bounded: one blocked
callback holds every later batch (and the write) of this process behind it, while the
service stays ``healthy``. No code registers one in W1.

Nothing here starts work: no thread except the settle worker, no timer, no route.
"""

from __future__ import annotations

import logging
import os
import stat
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any

from stiracore.durable_dir import fsync_dir

from . import epoch_store, incarnation
from .boundary import _REASON, AuthorityBoundary, AuthorityError
from .latches import LatchStore
from .lifecycle_events import LifecycleLog, latch_state

__all__ = ["AuthorityService", "current", "install", "uninstall"]

logger = logging.getLogger(__name__)

_NORMAL = "normal"
_REASONS_KEPT = 32
_FLUSH_POLL_SEC = 0.05
# W2: a cancel callback that runs longer than this is abandoned by :meth:`rescue_overdue`
# (recorded ``cancel_overdue``); the remaining work of its batch and every later batch go
# on with another owner.
CANCEL_DEADLINE_SEC = 5.0
# W2 fix 1: ``narrow`` / ``kill_switch`` never run a cancel callback on the caller's thread;
# they wait at most this long for the settlement and otherwise report ``cancelling``.
SETTLE_WAIT_SEC = 1.0
# W2 fix 3 (A-M1): the kill's batch is held at most this long for its persistence; then
# the cancellations run anyway, the kill stays set and the service latches unhealthy.
KILL_PERSIST_DEADLINE_SEC = 5.0
_BREAKER_STATES = frozenset({"normal", "warning", "coherence", "critical"})
# W2 fix 2 (Opus minor): a second kill marker that needs no free data blocks — an empty
# file made at start and *renamed* by the kill (best effort, before the latch write).
KILL_ARMED = "authority-kill-armed"
KILL_FIRED = "authority-kill-fired"


class AuthorityService(AuthorityBoundary):
    """The shared boundary of one process with its persisted epoch."""

    def __init__(self, data_dir: Path | None) -> None:
        super().__init__()
        self._data_dir = None if data_dir is None else Path(data_dir)
        self._io_lock = threading.Lock()
        self._records: tuple[epoch_store.EpochRecord, ...] = ()
        self._identity: tuple[int, ...] | None = None  # the file this service wrote last
        self._persisted = 0
        self._reasons: deque[tuple[int, str]] = deque(maxlen=_REASONS_KEPT)
        # Settlement (all under the leaf lock). Every narrowing queues one batch
        # ``(epoch, taken)``; exactly one *owner* at a time (``worker`` | ``sync`` |
        # ``flush``) settles the batches in order: cancel, then advance ``_frontier`` (the
        # newest epoch whose cancellations — and all older ones — completed), then write
        # the frontier. ``_idle`` (its own lock; set / cleared only with the leaf lock
        # released) wakes :meth:`flush`.
        self._batches: deque[tuple[int, list]] = deque()
        self._owner: str | None = None
        self._frontier = 0
        self._idle = threading.Event()
        self._idle.set()
        self._local = threading.local()
        # W2 (bounded cancel callbacks, under the leaf lock): ``_owner_seq`` changes when
        # :meth:`rescue_overdue` takes the queue from an owner stuck in a callback; that
        # owner then stops at its next check. ``_running`` = (owner seq, monotonic start,
        # effect) of the callback being run; ``_current`` = (epoch, the rest of its batch).
        self._owner_seq = 0
        self._running: tuple[int, float, Any] | None = None
        self._current: tuple[int, list] | None = None
        self._overdue = 0
        # Owner sequences whose abandoned callback has not returned yet: while any is
        # left nothing is "clean" or "settled" (W2 fix 1, M1).
        self._abandoned: set[int] = set()
        # W2 fix 4: the kill cancels its own effects on their own threads (never through
        # the settlement queue or behind its epoch write): how many still run.
        self._kill_pending = 0
        # Single-flight kill persistence: (thread, done event, result) of the one attempt.
        self._persist_lock = threading.Lock()
        self._persist_worker: tuple[threading.Thread | None, threading.Event, dict] | None = None
        # W4.5 (#618 K2): an attempt whose thread could not start, handed to the running
        # watchdog (:meth:`persist_handed_kill`); ``_kill_durable``: the kill latch is on
        # disk (``status()["kill_durable"]``).
        self._kill_handoff = None
        self._kill_durable = False
        self.cancel_deadline = CANCEL_DEADLINE_SEC
        self.settle_wait = SETTLE_WAIT_SEC
        self.kill_persist_deadline = KILL_PERSIST_DEADLINE_SEC
        # W2: the latch store (kill / halted / unhealthy, cleared only by a person) and the
        # lifecycle recorder (attached by the server; ``None`` = nothing is recorded).
        self.latches: LatchStore | None = None
        self.recorder = None
        # W4: called with the kill's epoch right after the flag and the epoch moved, before
        # any of the kill's I/O, with no lock held. Each must be in memory only and never
        # raise (the grants revoke themselves: ``GrantService.on_kill``); a callable it
        # returns (its I/O: posts, a write request) runs after the persistence attempt and
        # the cancel kickoff (W4.5, #620 W5-3).
        self.kill_listeners: list = []
        self._start()

    # -- identity --------------------------------------------------------------------------

    @property
    def incarnation(self) -> str:
        return incarnation.current()

    @incarnation.setter
    def incarnation(self, value: str) -> None:
        raise AuthorityError("incarnation_not_settable")

    # -- start -----------------------------------------------------------------------------

    def _start(self) -> None:
        """Process start is a narrowing (ADR 0011 §5): stored epoch + 1. Then the latches
        (W2): the store is checked against the epoch store's commitment and the lifecycle
        log (missing, older or different = every latch set); a set ``kill`` feeds the
        kill flag, a set ``unhealthy`` starts the service unhealthy. Then the start epoch
        is written, with the latch commitment."""
        commitment = self._load_epoch()
        self.latches = LatchStore(self._data_dir)
        self._check_latches(commitment)
        self._check_kill_marker()
        latched = self.latches.latched()
        if "kill" in latched:
            self._killed = True
        if self._unhealthy is None:
            if self.latches.unhealthy_reason is not None:
                self._unhealthy = self.latches.unhealthy_reason
            elif "unhealthy" in latched:
                self._unhealthy = "latched_unhealthy"
        if latched:
            logger.warning(
                "authority: latched at start (%s); a person clears them (stira authority unlatch)",
                ", ".join(sorted(latched)),
            )
        if self._data_dir is not None:
            self._kill_durable = self._killed  # the evidence that killed it is on disk
            self._persist()

    def _check_kill_marker(self) -> None:
        """A fired kill marker means a kill was pressed: the service starts killed, and a
        latch store without ``kill`` is refused (the latch write was lost, e.g. on a full
        disk). Without it, the marker is armed for this process and must be a safe file
        (regular, one link, this user, ``0600``, no symlink — the epoch store's rules);
        one that cannot be made or is unsafe starts the service **unhealthy**
        (``kill_marker_unavailable`` / ``kill_marker_unsafe``, fix 3 A-M2)."""
        if self._data_dir is None:
            return
        fired = self._data_dir / KILL_FIRED
        if os.path.lexists(fired):
            self._killed = True
            if self.latches.unhealthy_reason is None and "kill" not in self.latches.latched():
                self.latches.poison("latch_store_rolled_back")
                logger.warning("authority: a kill marker without its latch; every latch set")
            return
        armed = self._data_dir / KILL_ARMED
        problem = None
        try:
            if not os.path.lexists(armed):
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
                os.close(os.open(armed, flags, 0o600))
                fsync_dir(self._data_dir)
            info = os.lstat(armed)
            if stat.S_ISLNK(info.st_mode) or epoch_store._unsafe(info):
                problem = "kill_marker_unsafe"
        except OSError:
            problem = "kill_marker_unavailable"
        if problem is not None:
            if self._unhealthy is None:
                self._unhealthy = problem
            logger.warning("authority: the kill marker is not usable (%s); unhealthy", problem)

    def _fire_kill_marker(self) -> None:
        """Best effort, no lock held: rename the armed marker (no data blocks needed), or
        create the fired one; never raises."""
        if self._data_dir is None:
            return
        armed, fired = self._data_dir / KILL_ARMED, self._data_dir / KILL_FIRED
        try:
            try:
                os.rename(armed, fired)
            except FileNotFoundError:
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
                try:
                    os.close(os.open(fired, flags, 0o600))
                except FileExistsError:
                    pass
            fsync_dir(self._data_dir)
        except OSError:
            logger.warning("authority: the kill marker could not be written")

    def _load_epoch(self) -> tuple[int, str] | None:
        """The stored epoch (+ 1) and the latch commitment it carries (``None``: a W1-era
        or new data directory, or an unusable epoch file)."""
        if self._data_dir is None:
            self._epoch = self._frontier = 1  # in memory only (tests, no data directory)
            self._reasons.append((1, "process_start"))
            return None
        loaded = epoch_store.load(self._data_dir)
        if loaded.status == "bad":
            self._unhealthy = loaded.reason or "epoch_store_unreadable"
            self._epoch = self._frontier = 1
            self._reasons.append((1, "process_start"))
            logger.warning("authority: the epoch store is unusable (%s)", self._unhealthy)
            return None
        self._records = loaded.records
        self._identity = loaded.identity
        last = loaded.last
        self._epoch = 1 if last is None else last.epoch + 1
        self._frontier = self._epoch  # nothing was in flight before the start
        self._reasons.append((self._epoch, "process_start"))
        return None if last is None else last.commitment

    def _check_latches(self, commitment: tuple[int, str] | None) -> None:
        """B2: a latch store that does not descend from the epoch store's commitment, or
        that lacks a latch the lifecycle log shows as set and never cleared, is refused —
        every latch then counts as set (:func:`latch_store_problem`)."""
        if self._data_dir is None or self.latches.unhealthy_reason is not None:
            return
        problem = latch_store_problem(self._data_dir, self.latches, commitment)
        if problem is not None:
            self.latches.poison(problem)
            logger.warning("authority: the latch store is not the committed one (%s)", problem)

    # -- narrowing -------------------------------------------------------------------------

    def _latch(self, reason: str) -> None:
        with self._lock:
            if self._unhealthy is None:
                self._unhealthy = reason

    def _advance_locked(self, reason: str):
        """Under the leaf lock: the epoch moves and its batch is queued in the same step,
        so no drain can ever see a moved epoch without the batch that covers it."""
        epoch, taken = super()._advance_locked(reason)
        self._batches.append((epoch, list(taken)))
        return epoch, taken

    def _after_narrowing(self, epoch: int, taken, *, deferred: bool, reason: str) -> None:
        """Queue the batch of this narrowing; settle it now (``narrow`` / ``kill``, no
        foreign lock held) or hand it to the worker (under a foreign lock). A batch is
        always settled by the one current owner, in order — a synchronous caller that
        finds another owner leaves its batch to it. Never raises on the deferred path."""
        spawn = False
        try:
            with self._lock:
                self._reasons.append((epoch, reason))
                if self._owner is None:
                    # W2 fix 1: a synchronous owner hands the first batch with a cancel
                    # callback to the worker (``_drain``): no callback on its thread.
                    self._owner = "worker" if deferred else "sync"
                    spawn = deferred
                    own = not deferred
                else:
                    own = False
            if own:
                self._idle.clear()
                self._drain("sync")
            elif spawn:
                self._spawn_worker()
        except Exception:
            if not deferred:
                raise
            with self._lock:
                if spawn and self._owner == "worker":
                    self._owner = None  # nobody owns the queue: flush() takes it
                if self._unhealthy is None:
                    self._unhealthy = "epoch_settle_unavailable"
            self._idle.set()
            logger.warning("authority: a deferred narrowing could not be queued; unhealthy")

    def narrow(self, reason: str) -> int:
        """A narrowing (rules save, revoke, …): the epoch moves at once; the batch is
        settled here when it has no cancel callback, else by the worker — this thread
        never runs a callback. Waits at most :attr:`settle_wait` for the settlement."""
        epoch = super().narrow(reason)
        self._wait_settled(epoch)
        return epoch

    def _wait_settled(self, epoch: int) -> bool:
        """Wait (bounded, no lock held) until ``epoch``'s cancellations ran and it is on
        disk (or the service is unhealthy). True when settled."""
        if getattr(self._local, "settling", False):
            return False  # never wait for one's own settlement
        deadline = time.monotonic() + max(0.0, self.settle_wait)
        while True:
            with self._lock:
                settled = (
                    self._frontier >= epoch and not self._abandoned and self._kill_pending == 0
                )
                durable = (
                    self._data_dir is None
                    or self._persisted >= epoch
                    or self._unhealthy is not None
                )
            if settled and durable:
                return True
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.005)

    def advance_epoch(self) -> int:
        """The YOLO path (under its request lock): in memory now, settled by the worker.
        Never raises (a failure latches unhealthy; the narrowing stays in effect)."""
        try:
            return super().advance_epoch()
        except Exception:  # noqa: BLE001 - see above
            self._latch("epoch_settle_failed")
            return self._epoch

    def _spawn_worker(self) -> bool:
        """Start the worker for an owner slot already claimed as ``worker``. On failure
        the slot is released and the work stays queued (fail closed)."""
        try:
            self._idle.clear()
            worker = threading.Thread(target=self._worker, name="authority-settle", daemon=True)
            worker.start()
            return True
        except Exception:  # noqa: BLE001 - e.g. "can't start new thread"
            with self._lock:
                if self._owner == "worker":
                    self._owner = None
                if self._unhealthy is None:
                    self._unhealthy = "epoch_settle_unavailable"
            self._idle.set()
            logger.warning("authority: the settle worker could not start; unhealthy")
            return False

    def _worker(self) -> None:
        self._drain("worker")

    def _drain(self, owner: str, deadline: float | None = None) -> bool:
        """Settle queued batches as ``owner`` until the queue is empty (True) or the
        deadline passed between two batches (False, ownership handed back). The owner
        slot was claimed by the caller. An owner whose queue was taken by
        :meth:`rescue_overdue` (stuck in a callback) stops at its next check and touches
        nothing more (False)."""
        self._local.settling = True
        handoff = False
        with self._lock:
            seq = self._owner_seq
        self._local.owner_seq = seq
        try:
            while True:
                with self._lock:
                    if self._owner_seq != seq:
                        return False  # rescued: another owner has the queue
                    if not self._batches or (deadline is not None and time.monotonic() >= deadline):
                        done = not self._batches
                        if self._owner == owner:
                            self._owner = None
                        return done
                    epoch, taken = self._batches[0]
                    if owner == "sync" and _has_callback(taken):
                        # Never a callback on a synchronous caller's thread: hand over.
                        self._owner = "worker"
                        handoff = True
                        return False
                    self._batches.popleft()
                    self._current = (epoch, list(taken))
                try:
                    self._run_cancels(taken, "narrowing")
                    with self._lock:
                        self._current = None
                        self._frontier = max(self._frontier, epoch)
                    self._persist()
                except _Rescued:
                    return False  # the queue was taken while a callback ran: touch nothing
                except Exception:  # noqa: BLE001 - a failing round latches, the owner goes on
                    with self._lock:
                        if self._owner_seq != seq:
                            return False
                        self._current = None
                        self._frontier = max(self._frontier, epoch)
                    self._latch("epoch_settle_failed")
                    logger.warning("authority: a settlement failed; unhealthy")
        finally:
            self._local.settling = False
            self._local.owner_seq = None
            with self._lock:
                rescued = self._owner_seq != seq
            if handoff:
                self._spawn_worker()  # it clears / sets the idle state itself
            elif not rescued:  # a rescued owner leaves the new owner's idle state alone
                self._idle.set()

    def _run_cancels(self, taken, reason: str) -> None:
        """As the boundary's, plus (inside a drain) the bookkeeping that lets
        :meth:`rescue_overdue` abandon a callback past :attr:`cancel_deadline`: before
        each callback the rest of the batch is published, after it the owner checks it
        still owns the queue."""
        seq = getattr(self._local, "owner_seq", None)
        if seq is None:
            super()._run_cancels(taken, reason)
            return
        for index, (entry, cancel) in enumerate(taken):
            started = time.monotonic()  # the clock before the leaf lock, never under it
            with self._lock:
                if self._owner_seq != seq:
                    raise _Rescued
                if self._current is not None:
                    self._current = (self._current[0], list(taken[index + 1 :]))
                if entry.state != "stopping" or cancel is None:
                    continue
                self._running = (seq, started, entry)
            try:
                self._call_cancel(cancel, reason)
            finally:
                with self._lock:
                    if self._running is not None and self._running[0] == seq:
                        self._running = None
                    self._abandoned.discard(seq)  # an abandoned callback has returned
                    rescued = self._owner_seq != seq
            if rescued:
                raise _Rescued  # the rest of this batch belongs to the new owner

    def rescue_overdue(self) -> bool:
        """W2 (bounded callbacks): if the current owner has been inside one cancel
        callback for longer than :attr:`cancel_deadline`, take the queue from it — the
        rest of its batch goes back to the front of the queue, the stuck effect is
        recorded ``cancel_overdue`` (it stays in the in-flight set until it ends), the
        service latches ``unhealthy`` (``cancel_overdue``: the cancellation could not be
        established) and a new worker settles the rest. The abandoned callback is
        counted until it returns; until then nothing is ``clean`` or ``settled``.
        Called by the watchdog and by :meth:`flush_result`. True when it took over.
        Never raises."""
        now = time.monotonic()
        try:
            with self._lock:
                running = self._running
                if running is None or now - running[1] < self.cancel_deadline:
                    return False
                self._abandoned.add(running[0])
                if self._unhealthy is None:
                    # The cancellation could not be established: fail closed.
                    self._unhealthy = "cancel_overdue"
                self._owner_seq += 1
                self._running = None
                current, self._current = self._current, None
                if current is not None:
                    self._batches.appendleft(current)
                self._overdue += 1
                entry = running[2]
                self._stops.append(
                    {
                        "effect": entry.effect_id[:8],
                        "epoch": current[0] if current else self._epoch,
                        "outcome": "cancel_overdue",
                    }
                )
                self._owner = "worker"
        except Exception:  # noqa: BLE001 - never raises into the watchdog
            return False
        logger.warning("authority: a cancel callback is overdue; it is abandoned")
        self._spawn_worker()
        return True

    def flush_result(self, timeout: float | None = None) -> str:
        """Drain every settlement — the worker's, a synchronous ``narrow`` / ``kill`` on
        another thread, and work nobody owns (the worker could not start) — and report:

        * ``clean``: nothing queued or running, and the current epoch is on disk;
        * ``unhealthy``: drained, but the service is unhealthy (the epoch is not durable);
        * ``pending``: work still queued or running at the deadline — also while an
          abandoned cancel callback has not returned (at once without a deadline);
        * ``settle_unavailable``: work nobody owns and no worker can start (with a
          deadline it is not run inline: a callback could block past it);
        * ``reentrant``: called from a settlement itself (a cancel callback): it never
          waits for its own thread.

        Never waits while holding the leaf lock; ownership is checked under it."""
        if getattr(self._local, "settling", False):
            return "reentrant"
        deadline = None if timeout is None else time.monotonic() + max(0.0, timeout)
        while True:
            with self._lock:
                owner = self._owner
                queued = bool(self._batches)
                if owner is None and queued:
                    self._owner = "worker"
                running = bool(self._abandoned) or self._kill_pending > 0
                if owner is None and not queued and not running:
                    if self._unhealthy is not None:
                        return "unhealthy"
                    if self._data_dir is not None and self._persisted < self._epoch:
                        return "unhealthy"  # cannot happen while healthy; fail closed
                    return "clean"
            if owner is None and not queued:
                # Only abandoned or kill callbacks are left: never clean while one runs.
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is None or remaining <= 0:
                    return "pending"
                time.sleep(min(_FLUSH_POLL_SEC, remaining))
                continue
            if owner is None:  # queued work without an owner: a worker takes it
                if not self._spawn_worker():
                    if deadline is not None:
                        return "settle_unavailable"
                    with self._lock:
                        if self._owner is not None:
                            continue
                        self._owner = "flush"
                    self._drain("flush")  # no deadline asked for: settle it here
                continue
            if self.rescue_overdue():
                continue  # a stuck callback was abandoned; a new owner settles the rest
            remaining = None if deadline is None else deadline - time.monotonic()
            if remaining is not None and remaining <= 0:
                return "pending"
            wait = _FLUSH_POLL_SEC if remaining is None else min(_FLUSH_POLL_SEC, remaining)
            self._idle.wait(wait)

    def flush(self, timeout: float | None = None) -> bool:
        """True only when :meth:`flush_result` is ``clean``."""
        return self.flush_result(timeout) == "clean"

    def breaker_transition(self, before: str, after: str) -> None:
        """The engine's hook: a breaker change away from NORMAL or between non-NORMAL
        levels moves the epoch (ADR 0011 §5). A return to NORMAL does not. Called under
        the engine's ``state_lock``: in memory now, the write follows on the worker.
        Never raises."""
        try:
            before, after = str(before).lower(), str(after).lower()
            if after == _NORMAL or after == before:
                return
            reason = "breaker_" + after if after.isalpha() else "breaker"
            epoch, taken = self._advance(reason)
        except Exception:  # noqa: BLE001 - the hook never raises into the engine
            self._latch("epoch_settle_failed")
            return
        self._after_narrowing(epoch, taken, deferred=True, reason=reason)
        # W2: one lifecycle event (queued, no I/O: this runs under the engine's lock).
        self._record(
            "breaker",
            "engine",
            before if before in _BREAKER_STATES else "unknown",
            after if after in _BREAKER_STATES else "unknown",
            reason if after in _BREAKER_STATES else "breaker_unknown",
            epoch,
        )

    # -- W2: kill switch, narrowing report, latches, lifecycle -----------------------------

    def narrow_result(self, reason: str) -> dict[str, Any]:
        """:meth:`narrow` with its settlement state: ``settled`` when this narrowing's
        cancellations (and every older one) have run when it returns, ``cancelling``
        when another owner was settling and still is (W1 reviews, W2 item 6 (iii))."""
        epoch = self.narrow(reason)
        return {"epoch": epoch, "settlement": self._settlement(epoch)}

    def _settlement(self, epoch: int) -> str:
        with self._lock:
            settled = self._frontier >= epoch and not self._abandoned and self._kill_pending == 0
            return "settled" if settled else "cancelling"

    def kill(self, reason: str = "kill_switch") -> int:
        return self.kill_switch(reason)["epoch"]

    def kill_switch(self, reason: str = "kill_switch") -> dict[str, Any]:
        """The kill switch (ADR 0011 §5, §12). One rule (W2 fix 4): the in-memory flag
        first (no lock), the epoch through the leaf lock, then the kill is persisted (the
        marker, the ``kill`` latch, its commitment — one attempt at a time, waited for at
        most :attr:`kill_persist_deadline`), then **the cancel callbacks run on their own
        executor** — never through the settlement queue, its owner or its epoch write, so
        neither a hung disk nor a stuck owner can hold them (:meth:`_cancel_for_kill`).
        They are the kill's own effects **and** (W4.5, #618 K1) those of narrowings still
        queued behind the current owner; a batch an owner is running stays with it.
        The epoch itself is settled as any narrowing. Past the deadline the kill stays
        set and the service latches ``kill_persist_timeout``. ``latch``: ``set`` /
        ``already_set`` / ``failed`` / ``timeout`` / ``pending`` (its thread could not
        start: handed to the watchdog) / ``not_persisted``; ``settlement``:
        ``cancelling`` while a kill callback (or the epoch) is not done.

        W4.5 (#620 W5-3): kill listeners do the in-memory part only; what they return
        (a callable: lifecycle posts, a store write request) runs — like this service's
        own lifecycle records — after the persistence attempt and the cancel kickoff, so
        a blocking recorder delays neither. The callbacks are dispatched in ``finally``
        (#618 K4): nothing raised before can leave ``_kill_pending`` counted forever."""
        self._killed = True
        if type(reason) is not str or not _REASON.fullmatch(reason):
            reason = "kill_switch"
        with self._lock:
            epoch, _taken = self._advance_locked(reason)  # its batch is queued last
            # The kill's effects leave the settlement queue: its batch keeps the epoch only;
            # so do the effects of every batch still queued (K1; not ``_current``).
            queued = []
            for index in range(len(self._batches)):
                old_epoch, old_taken = self._batches[index]
                if old_taken:
                    queued.extend(old_taken)
                    self._batches[index] = (old_epoch, [])
            calls = [(entry, cancel) for entry, cancel in queued if cancel is not None]
            self._kill_pending += len(calls)
        followups = []
        latch = "failed"
        try:
            for listener in list(self.kill_listeners):
                try:
                    later = listener(epoch)
                    if callable(later):
                        followups.append(later)
                except Exception:  # noqa: BLE001 - a listener never stops the kill
                    logger.warning("authority: a kill listener failed")
            latch = self._persist_kill_bounded(epoch)
        except BaseException:
            self._latch("kill_persist_unavailable")
            raise
        finally:
            self._cancel_for_kill(calls)
        self._record("latch", "kill", "cleared", "set", "kill_switch", epoch)
        if latch in ("timeout", "pending"):
            code = "kill_persist_timeout" if latch == "timeout" else "kill_persist_unavailable"
            self._record("authority", "service", "healthy", "unhealthy", code, epoch)
        for later in followups:
            try:
                later()
            except Exception:  # noqa: BLE001
                logger.warning("authority: a kill listener's follow-up failed")
        self._after_narrowing(epoch, [], deferred=False, reason=reason)
        self._wait_settled(epoch)
        return {"epoch": epoch, "settlement": self._settlement(epoch), "latch": latch}

    def _cancel_for_kill(self, calls) -> None:
        """The kill's callbacks, in order, on one executor thread that waits on no queue,
        lock or I/O: each callback runs on its own thread and is waited for at most
        :attr:`cancel_deadline`; one that is still running then is left behind (counted
        until it returns, the service latches ``cancel_overdue``) and the next one starts.
        A thread that cannot start: that callback is not run (``cancel_unavailable``).
        Threads: ``authority-kill-cancel`` (the executor), ``authority-kill-callback``."""

        def call(entry, cancel) -> None:
            try:
                with self._lock:
                    stopping = entry.state == "stopping"
                if stopping:
                    self._call_cancel(cancel, "narrowing")
            finally:
                with self._lock:
                    self._kill_pending -= 1

        def executor() -> None:
            for entry, cancel in calls:
                worker = threading.Thread(
                    target=call, args=(entry, cancel), name="authority-kill-callback", daemon=True
                )
                try:
                    worker.start()
                except Exception:  # noqa: BLE001 - e.g. "can't start new thread"
                    with self._lock:
                        self._kill_pending -= 1
                    self._latch("cancel_unavailable")
                    continue
                worker.join(max(0.0, self.cancel_deadline))
                if worker.is_alive():
                    self._latch("cancel_overdue")  # it goes on; the next one starts now
                    logger.warning("authority: a kill callback is overdue; unhealthy")

        if not calls:
            return
        try:
            threading.Thread(target=executor, name="authority-kill-cancel", daemon=True).start()
        except Exception:  # noqa: BLE001
            with self._lock:
                self._kill_pending -= len(calls)
            self._latch("cancel_unavailable")
            logger.warning("authority: the kill callbacks could not be started; unhealthy")

    def _persist_kill_bounded(self, epoch: int) -> str:
        """The marker and the kill latch on **one** persistence attempt at a time: a kill
        while an earlier attempt still runs (a hung disk) waits for that attempt — no
        thread per kill. Waited for at most :attr:`kill_persist_deadline`; past it
        ``timeout`` and the service latches ``kill_persist_timeout``. A thread that cannot
        start: nothing runs inline — the service latches ``kill_persist_unavailable`` and
        the attempt is **handed to the running watchdog** (#618 K2,
        :meth:`persist_handed_kill`): ``pending`` (also for a later kill while it waits).
        Until a write lands, ``status()["kill_durable"]`` is false. The kill flag never
        depends on it. (The lifecycle records are the caller's, after the cancel kickoff.)"""
        with self._persist_lock:
            worker = self._persist_worker
            if worker is None or worker[1].is_set():
                done = threading.Event()
                result: dict[str, str] = {}

                def persist() -> None:
                    try:
                        self._fire_kill_marker()
                        result["latch"] = self._persist_kill(epoch)
                    finally:
                        done.set()

                thread = threading.Thread(
                    target=persist, name="authority-kill-persist", daemon=True
                )
                try:
                    thread.start()
                except Exception:  # noqa: BLE001 - never inline: it could hang
                    self._latch("kill_persist_unavailable")
                    logger.warning(
                        "authority: the kill persistence could not start; handed to the watchdog"
                    )
                    self._persist_worker = (None, done, result)
                    self._kill_handoff = persist
                    return "pending"
                worker = self._persist_worker = (thread, done, result)
        thread, done, result = worker
        if thread is None and not done.is_set():
            return "pending"  # the watchdog has it (single flight)
        if done.wait(max(0.0, self.kill_persist_deadline)):
            return result.get("latch", "failed")
        self._latch("kill_persist_timeout")
        logger.warning("authority: the kill could not be persisted in time; unhealthy")
        return "timeout"

    def persist_handed_kill(self) -> str | None:
        """The watchdog's pass (#618 K2): run the one kill persistence whose thread could
        not start, on the watchdog's own thread (single flight: taken once; a hung disk
        holds this pass as any of the pass's writes would). Its latch result, or None
        when nothing was handed over. Never raises."""
        with self._persist_lock:
            handoff, self._kill_handoff = self._kill_handoff, None
            worker = self._persist_worker
        if handoff is None:
            return None
        try:
            handoff()
        except Exception:  # noqa: BLE001 - already unhealthy; the result says failed
            logger.warning("authority: the handed-over kill persistence failed")
        return None if worker is None else worker[2].get("latch", "failed")

    def _persist_kill(self, epoch: int) -> str:
        store = self.latches
        if store is None or self._data_dir is None:
            if store is not None:
                store.set("kill", incarnation=self.incarnation, epoch=epoch)
            return "not_persisted"
        try:
            fresh = store.set("kill", incarnation=self.incarnation, epoch=epoch)
        except Exception:  # noqa: BLE001 - never un-kills; unhealthy instead
            code = store.unhealthy_reason or "latch_store_write_failed"
            self._latch(code)
            logger.warning("authority: the kill latch could not be persisted; unhealthy")
            # Opus residual: say so in the outbox too (the latch event below may be all
            # a restart would otherwise show).
            from .lifecycle_events import UNHEALTHY_REASONS

            self._record(
                "authority",
                "service",
                "healthy",
                "unhealthy",
                code if code in UNHEALTHY_REASONS else "latch_store_write_failed",
                epoch,
            )
            return "failed"
        self._kill_durable = True  # the latch file holds the kill (a restart is killed)
        self._commit_latches()
        return "set" if fresh else "already_set"

    def _commit_latches(self) -> bool:
        """Write the latch store's commitment into the epoch store now (a record that may
        repeat the epoch). Skipped — the start / the next epoch write commits it — when
        the epoch file itself is in doubt (an ``epoch_*`` reason) or not written yet.
        Never raises; a failed write latches unhealthy."""
        if self._data_dir is None or self.latches is None:
            return False
        with self._io_lock:
            with self._lock:
                reason = self._unhealthy
            if (reason is not None and reason.startswith("epoch_")) or not self._records:
                return False
            commitment = self.latches.commitment()
            last = self._records[-1]
            if commitment is None or commitment == last.commitment:
                return commitment is not None
            problem = self._expected_on_disk()
            if problem is not None:
                self._latch(problem)
                return False
            record = epoch_store.next_record(last, last.epoch, commitment)
            records = (*self._records, record)[-epoch_store.KEEP_RECORDS :]
            try:
                identity = epoch_store.write(self._data_dir, records)
            except OSError:
                self._latch("epoch_store_write_failed")
                return False
            self._records = records
            self._identity = identity
            return True

    def verify_files(self) -> str | None:
        """The watchdog's pass (W2 fix 1, M3): re-read the epoch file and the latch store
        and compare them with what this service read or wrote last. A difference latches
        unhealthy (and a changed latch store sets every latch: the kill flag too); the
        lock-free permit check reads that flag. The unhealthy reason, or None."""
        if self._data_dir is None:
            return self._unhealthy
        problem = None
        with self._io_lock:
            with self._lock:
                reason = self._unhealthy
            if not (reason is not None and reason.startswith("epoch_")):
                problem = self._expected_on_disk()
        if problem is not None:
            self._latch(problem)
            logger.warning("authority: the epoch file changed (%s); unhealthy", problem)
        store = self.latches
        if store is not None:
            bad = store.verify()
            if bad is not None:
                self._killed = True  # an unusable latch store: every latch is set
                self._latch(bad)
        return self._unhealthy

    def report_unhealthy(self, reason: str) -> None:
        """A failure reported by a W2 writer (lifecycle log, outbox, watchdog): the
        service latches unhealthy (in memory now; the watchdog persists the latch)."""
        self._latch(reason if isinstance(reason, str) and reason else "unhealthy_other")

    def persist_unhealthy(self) -> str | None:
        """Persist the ``unhealthy`` latch when the service is unhealthy (the watchdog's
        pass). The reason recorded, or None. Never raises; a failure is itself
        unhealthy (it already is) and returned as ``failed``."""
        reason = self._unhealthy
        store = self.latches
        if reason is None or store is None or store.unhealthy_reason is not None:
            return None
        if "unhealthy" in store.latched():
            return None
        epoch = self.epoch
        try:
            store.set("unhealthy", incarnation=self.incarnation, epoch=epoch)
        except Exception:  # noqa: BLE001
            logger.warning("authority: the unhealthy latch could not be persisted")
            return "failed"
        self._commit_latches()  # Opus r2: a rollback of the file must not clear it either
        from .lifecycle_events import UNHEALTHY_REASONS

        code = reason if reason in UNHEALTHY_REASONS else "unhealthy_other"
        self._record("latch", "unhealthy", "cleared", "set", code, epoch)
        return code

    def _record(self, kind, ident, before, after, reason, epoch) -> None:
        """Queue one lifecycle event (no I/O). Never raises; a refused event is reported
        by the recorder (unhealthy)."""
        recorder = self.recorder
        if recorder is None:
            return
        try:
            from .lifecycle_events import make_event

            event = make_event(
                kind,
                ident,
                before,
                after,
                reason,
                incarnation=self.incarnation,
                epoch=epoch,
                ts_wall=time.time(),
            )
        except Exception:  # noqa: BLE001 - outside the schema: a failed record
            self._latch("lifecycle_event_invalid")
            logger.warning("authority: a lifecycle event was outside the schema; unhealthy")
            return
        try:
            recorder.post(event)
        except Exception:  # noqa: BLE001
            self._latch("lifecycle_write_failed")

    # -- persistence -----------------------------------------------------------------------

    def _expected_on_disk(self) -> str | None:
        """Under ``_io_lock``: the reason the current file is not the one this service
        wrote last (or expects to be missing), or None."""
        loaded = epoch_store.load(self._data_dir)
        if self._identity is None and not self._records:
            return None if loaded.status == "missing" else "epoch_store_replaced"
        if loaded.status == "bad":
            return loaded.reason or "epoch_store_unreadable"
        if loaded.status != "ok":
            return "epoch_store_replaced"  # vanished
        if loaded.identity != self._identity or loaded.records != self._records:
            return "epoch_store_replaced"
        return None

    def _persist(self) -> None:
        if self._data_dir is None:
            return
        with self._io_lock:
            with self._lock:
                # Never beyond the cancellation frontier (ADR 0011 §5: cancel, then write).
                target = min(self._frontier, self._epoch)
                broken = self._unhealthy is not None
            if broken or target <= self._persisted:
                return  # never overwrite a broken file; never write after a failure
            problem = self._expected_on_disk()
            if problem is not None:
                self._latch(problem)
                logger.warning("authority: the epoch file changed (%s); unhealthy", problem)
                return
            last = self._records[-1] if self._records else None
            commitment = self.latches.commitment() if self.latches is not None else None
            if commitment is None and last is not None:
                commitment = last.commitment  # never lower what was committed
            record = epoch_store.next_record(last, target, commitment)
            records = (*self._records, record)[-epoch_store.KEEP_RECORDS :]
            try:
                identity = epoch_store.write(self._data_dir, records)
            except OSError as error:
                self._latch("epoch_store_write_failed")
                logger.warning(
                    "authority: the epoch could not be written (%s); unhealthy",
                    type(error).__name__,
                )
                return
            self._records = records
            self._identity = identity
            self._persisted = target

    # -- status ----------------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        """Codes and numbers only (never the incarnation)."""
        with self._lock:
            return {
                "healthy": self._unhealthy is None and not self._killed,
                "unhealthy_reason": self._unhealthy,
                "killed": self._killed,
                # #618 K2: a kill is durable only once its latch is written (a restart
                # before that can come up not killed: docs, Known limits).
                "kill_durable": self._killed and self._kill_durable,
                "epoch": self._epoch,
                "persisted_epoch": self._persisted,
                "in_flight": len(self._in_flight),
                "settle_pending": self._owner is not None or bool(self._batches),
                "frontier": self._frontier,
                "cancel_overdue": self._overdue,
                "cancel_abandoned": len(self._abandoned),
                "recent": [{"epoch": e, "reason": r} for e, r in self._reasons],
            }


def latch_store_problem(data_dir: Path, store: LatchStore, commitment) -> str | None:
    """The one check of a latch store against its evidence — the server's start and the
    host commands (``stira authority unlatch``) use the same: an unusable store; one that
    does not descend from the epoch store's commitment (missing, older, same generation
    with other contents, another instance: ``latch_store_rolled_back``); one that lacks a
    latch the lifecycle log shows as set and never cleared. ``None`` when it is fine. No
    commitment yet (a W1-era or new directory): only the log is checked."""
    if store.unhealthy_reason is not None:
        return store.unhealthy_reason
    if commitment is not None and not store.descends_from(commitment):
        return "latch_store_rolled_back"
    log = LifecycleLog(data_dir, rotate=False)
    if log.unhealthy_reason is not None:
        return None  # the recorder reports the log; nothing can be reconciled
    latched = store.latched()
    for name, is_set in latch_state(log.events()).items():
        if is_set and name not in latched:
            return "latch_store_rolled_back"
    return None


def kill_marker_problem(data_dir: Path, store: LatchStore) -> str | None:
    """The kill marker as the server's start sees it, for the host commands: a fired
    marker without the ``kill`` latch (the start refuses the store:
    ``latch_store_rolled_back``), or a marker entry that is not a safe private file
    (``kill_marker_unsafe``). ``None`` when it is fine (a missing armed marker is: the
    next start makes it)."""
    for name in (KILL_FIRED, KILL_ARMED):
        path = Path(data_dir) / name
        try:
            info = os.lstat(path)
        except FileNotFoundError:
            continue
        except OSError:
            return "kill_marker_unavailable"
        if stat.S_ISLNK(info.st_mode) or epoch_store._unsafe(info):
            return "kill_marker_unsafe"
        if name == KILL_FIRED and "kill" not in store.latched():
            return "latch_store_rolled_back"
    return None


class _Rescued(Exception):
    """The queue was taken from this owner while one of its callbacks ran."""


def _has_callback(taken) -> bool:
    return any(cancel is not None and entry.state == "stopping" for entry, cancel in taken)


_SERVICE: AuthorityService | None = None
_SERVICE_LOCK = threading.Lock()


def install(data_dir: Path | None) -> AuthorityService:
    """Build the process's service (server start-up); replaces an earlier one."""
    global _SERVICE
    service = AuthorityService(data_dir)
    with _SERVICE_LOCK:
        _SERVICE = service
    if service.unhealthy_reason is not None:
        logger.warning(
            "authority: unhealthy at start (%s); automatic approvals and permits are refused",
            service.unhealthy_reason,
        )
    return service


def current() -> AuthorityService | None:
    return _SERVICE


def uninstall() -> None:
    global _SERVICE
    with _SERVICE_LOCK:
        _SERVICE = None
