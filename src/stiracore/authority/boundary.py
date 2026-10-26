"""The authority boundary: permits, single-use consume, epoch, kill flag, in-flight set.

ADR 0011 §5 / §13 item 2, ADR 0012 §13, #276. :class:`AuthorityBoundary` is the
in-memory core; :class:`stiracore.authority.service.AuthorityService` is the one
process-wide instance that also persists the epoch. ``stiracore.yolo.authority``
re-exports this module unchanged for older importers.

* **``kind`` is a mandatory discriminator** of every grant record (``goal`` | ``yolo``); a
  record without it, or with another value, cannot be built. Functions for one kind refuse
  the other (:func:`require_kind`): a YOLO window can never be handled as a goal grant,
  nor the reverse.
* **Incarnation**: 128 bits from a CSPRNG, never read back from storage — every permit
  carries it, so nothing from an earlier process is accepted.
* **Authority epoch**: monotonic; it only moves up by one per narrowing.
* **Single-use ``consume``**: a permit is bound to ``(incarnation, kind, grant_id,
  generation, epoch)``, to an ADR 0013 receipt reference and optionally to a policy
  version, a budget reservation id and a TTL pair; :meth:`consume` re-checks all of it
  under the lock against the current record and accepts each permit once.
* **Kill flag**: a plain attribute (one atomic store / load under the interpreter), set
  before any lock is taken and read **first** by :meth:`consume`, outside every lock, and
  once more under the leaf lock. A kill therefore takes effect while any other thread
  holds any lock, the leaf lock included.
* **Unhealthy**: a reason code (also a lock-free attribute). While it is set every
  :meth:`issue_permit` and :meth:`consume` refuses (``authority_unhealthy``). Nothing in
  this module clears it.
* **In-flight set**: a successful :meth:`consume` may register the effect (``track`` /
  ``on_cancel``); a later narrowing takes the whole set under the lock and calls the
  cancel callbacks after releasing it. An effect that ends after the stop is recorded as
  ``completed_after_stop``. The set is bounded (:data:`MAX_IN_FLIGHT`); a full set
  refuses the consume before the permit is used. An effect must call ``done()``; a stopped
  effect stays in the set until it does. The record of consumed nonces is bounded too
  (:data:`MAX_CONSUMED`; an older reuse is still refused, as ``permit_unknown``).

**Lock order.** One leaf lock (``_lock``) guards the in-memory compare-and-set only: no
I/O (entropy included: nonces and effect ids are drawn before it is taken), no callback,
no clock reading and no other lock acquisition happens while it is held; the kill flag,
the unhealthy latch, the epoch and every binding are re-checked inside it. Callers may hold their own locks when they call in (scheduler lock → authority
lock; the YOLO request lock → authority lock; the engine ``state_lock`` → authority
lock). Persistence (the service's ``_io_lock``) and cancellation run after the leaf lock
is released and never take it while doing I/O.
"""

from __future__ import annotations

import logging
import math
import re
import secrets
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

__all__ = [
    "GRANT_KINDS",
    "MAX_IN_FLIGHT",
    "AuthorityBoundary",
    "AuthorityError",
    "ClockPair",
    "GrantRecord",
    "InFlight",
    "Permit",
    "Receipt",
    "TtlPair",
    "require_kind",
]

logger = logging.getLogger(__name__)

GRANT_KINDS = ("goal", "yolo")
MAX_IN_FLIGHT = 64
# Consumed nonces kept to answer ``permit_consumed``; an older one is answered
# ``permit_unknown`` (still refused: a consumed permit is no longer in ``_issued``).
MAX_CONSUMED = 4096
STOP_RECORDS = 256
_USABLE_STATES = frozenset({"active"})
_HEX32 = re.compile(r"[0-9a-f]{32}")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_EVENT_REF = re.compile(r"op:[0-9a-f]{32}:(decision|intent|outcome|transition)(:[0-9]{1,6})?")
_REASON = re.compile(r"[a-z][a-z0-9_]{0,47}")


class AuthorityError(Exception):
    """A refused step at the authority boundary; ``code`` says why (never a secret)."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class GrantRecord:
    """A grant (goal) or window (yolo). ``kind`` has no default: it must be stated."""

    kind: str
    grant_id: str
    incarnation: str
    generation: int
    state: str

    def __post_init__(self):
        if self.kind not in GRANT_KINDS:
            raise AuthorityError("grant_kind_invalid")
        if not isinstance(self.grant_id, str) or not _HEX32.fullmatch(self.grant_id):
            raise AuthorityError("grant_id_invalid")
        if type(self.generation) is not int or self.generation < 0:
            raise AuthorityError("generation_invalid")


def require_kind(record: object, kind: str) -> GrantRecord:
    """The type check every kind-specific function starts with."""
    if kind not in GRANT_KINDS:
        raise AuthorityError("grant_kind_invalid")
    if not isinstance(record, GrantRecord) or record.kind != kind:
        raise AuthorityError("grant_kind_mismatch")
    return record


@dataclass(frozen=True)
class Receipt:
    """ADR 0013 §2a, the part a permit carries: the audit row that authorized it."""

    event_id: str
    seq: int
    hash: str

    def valid(self) -> bool:
        return (
            isinstance(self.event_id, str)
            and bool(_EVENT_REF.fullmatch(self.event_id))
            and type(self.seq) is int
            and self.seq >= 1
            and isinstance(self.hash, str)
            and bool(_HEX64.fullmatch(self.hash))
        )


def _finite(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


@dataclass(frozen=True)
class TtlPair:
    """ADR 0011 §5 clock rule: a deadline on the wall clock **and** one on the monotonic
    clock; the earlier one ends the grant. Never extended."""

    wall_deadline: float
    monotonic_deadline: float

    def valid(self) -> bool:
        return _finite(self.wall_deadline) and _finite(self.monotonic_deadline)


@dataclass(frozen=True)
class ClockPair:
    """One reading of both clocks, taken by the caller before :meth:`consume`."""

    wall: float
    monotonic: float

    def valid(self) -> bool:
        return _finite(self.wall) and _finite(self.monotonic)


@dataclass(frozen=True)
class Permit:
    nonce: str
    incarnation: str
    kind: str
    grant_id: str
    generation: int
    epoch: int
    receipt: Receipt
    # ADR 0011 §5 (optional; no caller binds them yet).
    policy_version: int | None = None
    reservation_id: str | None = None
    ttl: TtlPair | None = None


def _check_bindings(policy_version: Any, reservation_id: Any, ttl: Any) -> None:
    if policy_version is not None and (type(policy_version) is not int or policy_version < 0):
        raise AuthorityError("policy_version_invalid")
    if reservation_id is not None and (
        type(reservation_id) is not str or not _HEX32.fullmatch(reservation_id)
    ):
        raise AuthorityError("reservation_invalid")
    if ttl is not None and (type(ttl) is not TtlPair or not ttl.valid()):
        raise AuthorityError("ttl_invalid")


class InFlight:
    """One effect that started after a successful :meth:`AuthorityBoundary.consume`.

    ``state``: ``running`` → ``completed`` (normal end), or ``running`` → ``stopping``
    (a narrowing took it) → ``cancelled`` / ``completed_after_stop``."""

    __slots__ = ("_cancel", "_owner", "effect_id", "epoch", "grant_id", "kind", "state")

    def __init__(self, owner: AuthorityBoundary, permit: Permit, cancel, effect_id: str) -> None:
        self._owner = owner
        self._cancel = cancel
        self.effect_id = effect_id  # drawn before the leaf lock (no entropy under it)
        self.kind = permit.kind
        self.grant_id = permit.grant_id
        self.epoch = permit.epoch
        self.state = "running"

    def set_cancel(self, cancel: Callable[[str], Any]) -> None:
        """Register (or replace) the cancel callback. If a narrowing already took this
        effect, the callback runs now (in the caller's thread, no lock held)."""
        self._owner._set_cancel(self, cancel)

    def done(self, outcome: str = "completed") -> str:
        """The effect ended (``completed`` or ``cancelled``); the final state."""
        return self._owner._done(self, outcome)


class AuthorityBoundary:
    """One per process. Every method takes the leaf lock only for its compare-and-set."""

    def __init__(self) -> None:
        self._incarnation = secrets.token_hex(16)  # never from storage (ADR 0011 §5)
        self._lock = threading.Lock()  # the leaf lock
        self._epoch = 0
        # nonce -> the permit as issued (immutable binding); compared field by field at
        # consume, so a permit rebuilt with another epoch / generation / kind / grant /
        # receipt is refused, also with a valid nonce.
        self._issued: dict[str, Permit] = {}
        self._consumed: set[str] = set()
        self._consumed_order: deque[str] = deque()
        # Lock-free flags: one attribute store / load each (atomic in CPython).
        self._killed = False
        self._unhealthy: str | None = None
        self._in_flight: dict[str, InFlight] = {}
        self._stops: deque[dict] = deque(maxlen=STOP_RECORDS)
        self.max_in_flight = MAX_IN_FLIGHT

    # -- identity, epoch, flags ----------------------------------------------------------

    @property
    def incarnation(self) -> str:
        return self._incarnation

    @incarnation.setter
    def incarnation(self, value: str) -> None:
        # Kept for the private boundary of ``YoloRequests`` (it adopts its desk's value).
        self._incarnation = value

    @property
    def epoch(self) -> int:
        with self._lock:
            return self._epoch

    @property
    def killed(self) -> bool:
        return self._killed

    @property
    def unhealthy_reason(self) -> str | None:
        return self._unhealthy

    def _advance_locked(self, reason: str) -> tuple[int, list[tuple[InFlight, Any]]]:
        """Under the leaf lock: +1 and take the in-flight set (marked ``stopping``)."""
        self._epoch += 1
        taken = []
        for entry in self._in_flight.values():
            if entry.state == "running":
                entry.state = "stopping"
                taken.append((entry, entry._cancel))
                self._stops.append(
                    {"effect": entry.effect_id[:8], "epoch": self._epoch, "reason": reason}
                )
        return self._epoch, taken

    def _advance(self, reason: str) -> tuple[int, list[tuple[InFlight, Any]]]:
        if type(reason) is not str or not _REASON.fullmatch(reason):
            reason = "narrowing"
        with self._lock:
            return self._advance_locked(reason)

    def advance_epoch(self) -> int:
        """Every narrowing moves the epoch up by one; it never goes back. In-flight work
        is cancelled after the lock is released."""
        epoch, taken = self._advance("narrowing")
        self._after_narrowing(epoch, taken, deferred=True, reason="narrowing")
        return epoch

    def narrow(self, reason: str) -> int:
        """A narrowing with a reason code (rules save, breaker, revoke, …): the epoch
        moves, in-flight work is cancelled, then (service) the epoch is persisted. For
        callers that hold no other lock."""
        epoch, taken = self._advance(reason)
        self._after_narrowing(epoch, taken, deferred=False, reason=reason)
        return epoch

    def commit_at_epoch(self, commit: Callable[[int], Any], *, expected: int | None = None):
        """Run ``commit(epoch)`` under the leaf lock — the lock every narrowing takes — so
        nothing can move the epoch in between: ``(True, epoch, result)``. With
        ``expected``: refused (``(False, epoch, None)``, ``commit`` not called) when the
        epoch is not ``expected`` or the kill flag is set.

        ``commit`` must be a small in-memory state swap: no I/O, no callback, no logging
        and no other lock (callers gather all of that before, outside this lock; lock
        order stays caller lock → leaf lock). The one exception (#620 W5-2): a re-read of
        the in-memory clocks (``clock_gettime``; no file, no lock) so that a deadline
        check and the commit are one step; nothing else is read there."""
        with self._lock:
            epoch = self._epoch
            if expected is not None and (epoch != expected or self._killed):
                return False, epoch, None
            return True, epoch, commit(epoch)

    def kill(self, reason: str = "kill_switch") -> int:
        """The kill switch: the flag first (no lock), then the epoch through the leaf
        lock. Never cleared in this process (a latch store is ADR 0011 §13, W2)."""
        self._killed = True
        return self.narrow(reason)

    def _after_narrowing(self, epoch: int, taken, *, deferred: bool, reason: str) -> None:
        """Outside the leaf lock. The boundary has nothing to persist: cancel only."""
        self._run_cancels(taken, "narrowing")

    def _run_cancels(self, taken, reason: str) -> None:
        for entry, cancel in taken:
            with self._lock:
                finished = entry.state != "stopping"
            if finished or cancel is None:
                continue
            self._call_cancel(cancel, reason)

    @staticmethod
    def _call_cancel(cancel, reason: str) -> None:
        try:
            cancel(reason)
        except Exception:  # noqa: BLE001 - a cancel callback never raises into the caller
            logger.warning("authority: an in-flight cancel callback failed")

    def _refuse_if_stopped(self) -> None:
        if self._killed:
            raise AuthorityError("killed")
        if self._unhealthy is not None:
            raise AuthorityError("authority_unhealthy")

    # -- permits -------------------------------------------------------------------------

    def issue_permit(
        self,
        record: GrantRecord,
        *,
        kind: str,
        receipt: Receipt,
        policy_version: int | None = None,
        reservation_id: str | None = None,
        ttl: TtlPair | None = None,
    ) -> Permit:
        """A permit for one call under ``record`` (still unconsumed, single-use)."""
        self._refuse_if_stopped()
        require_kind(record, kind)
        if not isinstance(receipt, Receipt) or not receipt.valid():
            raise AuthorityError("receipt_missing")
        _check_bindings(policy_version, reservation_id, ttl)
        nonce = secrets.token_hex(16)  # entropy before the leaf lock, never under it
        with self._lock:
            self._refuse_if_stopped()
            if record.incarnation != self.incarnation:
                raise AuthorityError("incarnation_mismatch")
            if record.state not in _USABLE_STATES:
                raise AuthorityError("grant_not_active")
            permit = Permit(
                nonce=nonce,
                incarnation=self.incarnation,
                kind=record.kind,
                grant_id=record.grant_id,
                generation=record.generation,
                epoch=self._epoch,
                receipt=receipt,
                policy_version=policy_version,
                reservation_id=reservation_id,
                ttl=ttl,
            )
            self._issued[permit.nonce] = permit
            return permit

    def consume(
        self,
        permit: Permit,
        current: GrantRecord,
        *,
        kind: str,
        policy_version: int | None = None,
        reservation_id: str | None = None,
        clock: ClockPair | None = None,
        track: bool = False,
        on_cancel: Callable[[str], Any] | None = None,
    ) -> InFlight | None:
        """Turn the permit into a use, exactly once; any mismatch refuses (fail closed).

        The kill flag is read first, before any other check and outside every lock.
        ``policy_version`` / ``reservation_id`` must equal what the permit was issued
        with (both absent, or both the same); a permit with a TTL pair needs ``clock``
        and is refused once either deadline is reached. ``track`` / ``on_cancel``
        register the effect in the in-flight set (its handle is returned)."""
        self._refuse_if_stopped()
        require_kind(current, kind)
        if not isinstance(permit, Permit) or permit.kind != kind:
            raise AuthorityError("grant_kind_mismatch")
        if not isinstance(permit.receipt, Receipt) or not permit.receipt.valid():
            raise AuthorityError("receipt_missing")
        _check_bindings(policy_version, reservation_id, None)
        if clock is not None and (type(clock) is not ClockPair or not clock.valid()):
            raise AuthorityError("clock_invalid")
        if on_cancel is not None and not callable(on_cancel):
            raise AuthorityError("cancel_invalid")
        tracked = track or on_cancel is not None
        effect_id = secrets.token_hex(16) if tracked else None  # before the leaf lock
        with self._lock:
            self._refuse_if_stopped()
            nonce = getattr(permit, "nonce", None)
            if nonce in self._consumed:
                raise AuthorityError("permit_consumed")
            issued = self._issued.get(nonce)
            if issued is None:
                raise AuthorityError("permit_unknown")
            if type(permit) is not Permit or permit != issued:
                # Not the permit that was issued: some binding field was rewritten.
                raise AuthorityError("permit_tampered")
            if permit.incarnation != self.incarnation or current.incarnation != self.incarnation:
                raise AuthorityError("incarnation_mismatch")
            if permit.grant_id != current.grant_id:
                raise AuthorityError("grant_mismatch")
            if permit.generation != current.generation:
                raise AuthorityError("permit_stale")
            if permit.epoch != self._epoch:
                raise AuthorityError("epoch_moved")
            if current.state not in _USABLE_STATES:
                raise AuthorityError("grant_not_active")
            if permit.policy_version != policy_version:
                raise AuthorityError("policy_moved")
            if permit.reservation_id != reservation_id:
                raise AuthorityError("reservation_mismatch")
            if permit.ttl is not None:
                if clock is None:
                    raise AuthorityError("clock_missing")
                if (
                    clock.wall >= permit.ttl.wall_deadline
                    or clock.monotonic >= permit.ttl.monotonic_deadline
                ):
                    raise AuthorityError("ttl_reached")
            entry = None
            if tracked:
                if len(self._in_flight) >= self.max_in_flight:
                    raise AuthorityError("in_flight_full")
                entry = InFlight(self, permit, on_cancel, effect_id)
                self._in_flight[entry.effect_id] = entry
            self._consumed.add(nonce)
            self._consumed_order.append(nonce)
            if len(self._consumed_order) > MAX_CONSUMED:
                self._consumed.discard(self._consumed_order.popleft())
            del self._issued[nonce]
            return entry

    def void(self, permit: Permit) -> bool:
        """Return an unconsumed permit (its slot goes back); False if already consumed."""
        with self._lock:
            nonce = getattr(permit, "nonce", None)
            if nonce in self._consumed or self._issued.get(nonce) != permit:
                return False
            del self._issued[nonce]
            return True

    # -- the in-flight set ---------------------------------------------------------------

    def _set_cancel(self, entry: InFlight, cancel) -> None:
        if not callable(cancel):
            raise AuthorityError("cancel_invalid")
        with self._lock:
            entry._cancel = cancel
            stopping = entry.state == "stopping"
        if stopping:
            self._call_cancel(cancel, "narrowing")

    def _done(self, entry: InFlight, outcome: str) -> str:
        with self._lock:
            if entry.state == "running":
                entry.state = "completed"
            elif entry.state == "stopping":
                entry.state = "cancelled" if outcome == "cancelled" else "completed_after_stop"
                self._stops.append(
                    {"effect": entry.effect_id[:8], "epoch": self._epoch, "outcome": entry.state}
                )
            self._in_flight.pop(entry.effect_id, None)
            return entry.state

    def in_flight_count(self) -> int:
        with self._lock:
            return len(self._in_flight)

    def stop_records(self) -> list[dict]:
        """The latest stops of in-flight effects (codes and short ids only)."""
        with self._lock:
            return [dict(item) for item in self._stops]
