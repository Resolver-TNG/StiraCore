"""Session allowances: the token base, the fence, the counters and the CAS (ADR 0025 PR 1).

Everything here is **memory only** and bound to this process (ADR 0025 §6): a restart is
the strongest end, and nothing is read back from storage, so a data-directory rollback
cannot replay a token or a counter.

* **One leaf lock** (:attr:`AllowanceTable._lock`) guards the allowance table, the
  fence, the breaker departure counter, the roots counter (and the last roots digest),
  the audit health epoch, the view tokens and the save tokens. Nothing else is acquired
  while it is held and it is never held across I/O. Lock order: ``engine.state_lock`` →
  ``ConfigStore.lock`` → this lock, never the reverse (this module takes no other lock).
* **Bumps first.** Every end publishes itself (``bump_*``) under the lock **before** it
  takes effect; every compare-and-set (reserve, commit, use, consume, the save token)
  compares those in-memory integers under the same lock. So an end and a consumption are
  always ordered, and a value that went and came back is still seen as changed.
* **Evidence vs CAS** (§4.0). The steps that need I/O — the eligibility function, path
  resolution, rule / roots reads, the live breaker, audit rows and their receipts — run
  outside the lock (:class:`.allow_service.AllowanceService`); the methods here only
  compare and apply.

:data:`ENABLED` turns both card buttons on together (one code constant, not a setting; PR 3).
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import secrets
import threading
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import Any

from .allow_class import CLASS_VERSION, TargetClass, class_contains
from .host_ops import default_clock, elapsed

__all__ = [
    "DURATION_SEC",
    "ENABLED",
    "END_REASONS",
    "GRANT_TRANSITIONS",
    "KIND",
    "MAX_PER_SESSION",
    "NOT_ENABLED",
    "Allowance",
    "AllowanceTable",
    "Snapshot",
    "UseBinding",
    "roots_digest",
    "table",
]

# PR 3 turns both buttons on together (ADR 0025 "Implementation order"): one code
# constant, not a setting. ``False`` makes the decision route's ``allow_again`` and a
# card-origin hub save return 409 ``not_enabled`` again, and the page draws neither button.
ENABLED = True
NOT_ENABLED = "not_enabled"
KIND = "session_allow"
DURATION_SEC = 2 * 60 * 60
MAX_PER_SESSION = 20
CLOCK_SLACK_SEC = 2.0  # as ``requests.CLOCK_SLACK_SEC`` (§7): wall clock backwards
END_REASONS = frozenset(
    {
        "new_session",
        "breaker",
        "containment",
        "stop",
        "revoked",
        "time",
        "rule_changed",
        "roots_changed",
        "read_failed",
        "clock_anomaly",
        "audit_failed",
        "issue_aborted_fence",
        "issue_aborted_departure",
        "issue_aborted_audit",
    }
)


# Every public transition that grants or confirms something; each one enters through
# ``AllowanceTable._grant_section`` (pinned by a structural and a behavioural test).
GRANT_TRANSITIONS = (
    "issue_view_token",
    "spend_view",
    "reserve",
    "commit",
    "use",
    "consume",
    "issue_save_token",
    "spend_save_token",
    "audit_still_healthy",
)


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("ascii", "replace")).hexdigest()


def roots_digest(policy: Any) -> str:
    """A digest of write roots, read roots and ``protected_dirs`` of a path policy.

    Raises on a policy whose values cannot be read: the caller treats it as a failed read
    (every allowance ends, ``read_failed``)."""
    parts = []
    for name in ("write_roots", "read_roots", "protected_dirs", "workspace"):
        value = getattr(policy, name, None)
        if value is None:
            parts.append(f"{name}=none")
        elif isinstance(value, (str, bytes)) or not hasattr(value, "__iter__"):
            parts.append(f"{name}={value!s}")
        else:
            parts.append(f"{name}=" + "\x1f".join(sorted(str(item) for item in value)))
    return hashlib.sha256("\x1e".join(parts).encode("utf-8", "surrogatepass")).hexdigest()


@dataclass(frozen=True)
class Snapshot:
    """The in-memory values every CAS compares (§4.0)."""

    fence: int
    departures: int
    roots_generation: int
    health_epoch: int


@dataclass
class _ViewToken:
    request_id: str
    args_hash: str
    session_epoch: int
    target: TargetClass
    rule_generation: Any
    snapshot: Snapshot


@dataclass
class _SaveToken:
    request_id: str
    tool: str
    hub_version: str
    rule_generation: Any
    fence: int


@dataclass
class Allowance:
    """One press (ADR 0025 Terms). ``generation``: 0 reserved, 1 active, +1 on its end."""

    allow_id: str
    incarnation: str
    session_epoch: int
    target: TargetClass
    fence: int
    breaker_departures: int
    rule_generation: Any
    roots_generation: int
    roots_digest: str
    health_epoch: int
    source_request_id: str
    issued_at: Any  # ClockReading
    expires_at: float  # wall time, display only (the deadline is ``elapsed`` ≥ 2 h)
    generation: int = 0
    uses: int = 0
    state: str = "reserved"  # reserved | active | ended
    end_reason: str | None = None
    issued_ref: str | None = None  # event id of ``audit.allow.issued``
    decision_ref: str | None = None  # event id of ``audit.allow.decided``


@dataclass(frozen=True)
class UseBinding:
    """An automatic answer of an allowance (§4b), spent once at the consume step (§4c)."""

    nonce: str
    allow_id: str
    generation: int
    snapshot: Snapshot
    tool: str
    target_key: str
    args_hash: str


@dataclass
class _Ended:
    allow_id: str
    uses: int
    generation_from: int
    generation_to: int


@dataclass
class EndNotice:
    """What a bump ended (written as ``audit.allow.ended`` rows after the lock)."""

    reason: str
    fence_from: int
    fence_to: int
    ended: list[_Ended] = field(default_factory=list)


class AllowanceTable:
    """One per process (:func:`table`). Every method takes the leaf lock briefly."""

    def __init__(
        self,
        *,
        clock: Callable[[], Any] = default_clock,
        token: Callable[[], str] = lambda: secrets.token_hex(16),
        duration_sec: float = DURATION_SEC,
    ) -> None:
        self.incarnation = secrets.token_hex(16)  # never from storage (ADR 0011 §5)
        self._lock = threading.Lock()
        self._clock = clock
        self._token = token
        self.duration_sec = duration_sec
        self._fence = 0
        self._departures = 0
        self._roots_generation = 0
        self._roots_digest: str | None = None
        self._health_epoch = 0
        self._audit_failed = False  # sticky until restart (the buttons stay hidden)
        self._session_epoch = 0
        self._records: dict[str, Allowance] = {}
        self._views: dict[str, _ViewToken] = {}  # token digest -> view
        self._view_of: dict[str, str] = {}  # request id -> token digest
        self._saves: dict[str, _SaveToken] = {}
        self._spent: set[str] = set()  # spent token digests (``op_token_used``)
        self._bindings: dict[str, UseBinding] = {}  # nonce -> unspent use binding
        self._last_clock: Any = None
        self.on_end: Callable[[EndNotice], None] | None = None  # audit rows, after the lock
        # The audit writer's *current* health, read inside every grant transition (review
        # round 4). It must be a flag read — no I/O, no lock (``EventStore.healthy`` is an
        # attribute). The service sets it; a bare table (tests) has no writer to ask.
        self.writer_probe: Callable[[], bool] = lambda: True

    # -- the one gate of every grant transition -----------------------------------------

    def _grant_gate_locked(self, epoch: int | None) -> tuple[str | None, EndNotice | None]:
        """Under the lock: refuse unless the writer is healthy **now**, the sticky audit
        failure is clear, and (when given) the health epoch is still ``epoch``. An
        unhealthy writer seen here is published at once (sticky, every allowance ends)."""
        try:
            writer_ok = self.writer_probe() is True
        except Exception:  # noqa: BLE001 - unknown health is unhealthy
            writer_ok = False
        notice = None
        if not writer_ok and not self._audit_failed:
            self._audit_failed = True
            self._health_epoch += 1
            notice = self._end_all_locked("audit_failed")
        if self._audit_failed or (epoch is not None and epoch != self._health_epoch):
            return "audit_failed", notice
        return None, notice

    @contextlib.contextmanager
    def _grant_section(self, epoch: int | None = None):
        """**The only way into a transition that grants or confirms anything** (a view or
        save token issued or spent, a reserve, ``reserved → active``, a use binding, a
        consumption, a final confirmation). Takes the leaf lock, runs
        :meth:`_grant_gate_locked` first and yields its refusal code (``None`` = go on);
        the end notice of a failure it published is delivered after the lock.
        ``tests/test_session_allow.py`` fails for a granting function that bypasses it."""
        notice = None
        try:
            with self._lock:
                refusal, notice = self._grant_gate_locked(epoch)
                yield refusal
        finally:
            self._notify(notice)

    # -- reads ----------------------------------------------------------------------

    def snapshot(self) -> Snapshot:
        with self._lock:
            return self._snapshot_locked()

    def _snapshot_locked(self) -> Snapshot:
        return Snapshot(self._fence, self._departures, self._roots_generation, self._health_epoch)

    @property
    def session_epoch(self) -> int:
        with self._lock:
            return self._session_epoch

    @property
    def audit_failed(self) -> bool:
        with self._lock:
            return self._audit_failed

    def get(self, allow_id: str) -> Allowance | None:
        with self._lock:
            record = self._records.get(allow_id)
            return None if record is None else replace(record)

    def active(self) -> list[Allowance]:
        """Live allowances of the current session (copies; the page's list in PR 2)."""
        with self._lock:
            return [replace(r) for r in self._records.values() if self._live_locked(r)]

    def _live_locked(self, record: Allowance) -> bool:
        return (
            record.state == "active"
            and record.generation == 1
            and record.fence == self._fence
            and record.session_epoch == self._session_epoch
        )

    # -- bumps (narrowings; never refused, never I/O) --------------------------------

    def _end_all_locked(self, reason: str) -> EndNotice:
        before = self._fence
        self._fence += 1
        notice = EndNotice(reason, before, self._fence)
        for record in self._records.values():
            if record.state in ("reserved", "active"):
                notice.ended.append(
                    _Ended(record.allow_id, record.uses, record.generation, record.generation + 1)
                )
                record.generation += 1
                record.state = "ended"
                record.end_reason = reason
        # Every view and save token carries the old fence: dead (kept out of the table).
        self._views.clear()
        self._view_of.clear()
        self._saves.clear()
        return notice

    def _notify(self, notice: EndNotice | None) -> None:
        hook = self.on_end
        if notice is None or hook is None or not notice.ended:
            return  # nothing ended: no row (a WARNING turn bumps on every evaluation)
        with contextlib.suppress(Exception):  # the bump already happened; rows best-effort
            hook(notice)

    def bump_fence(self, reason: str) -> int:
        """End every allowance (and every view / save token) before ``reason`` takes effect."""
        if reason not in END_REASONS:
            reason = "read_failed"
        with self._lock:
            notice = self._end_all_locked(reason)
            fence = self._fence
        self._notify(notice)
        return fence

    def bump_departure(self) -> int:
        """The breaker left NORMAL (or could not be read): called by the engine inside the
        critical section that commits that level (lock order ``state_lock`` → this lock)."""
        with self._lock:
            self._departures += 1
            notice = self._end_all_locked("breaker")
            departures = self._departures
        self._notify(notice)
        return departures

    def bump_roots(self) -> int:
        """A save that writes write roots, read roots or ``protected_dirs``: before the
        file is replaced. Irreversible: changing the file back does not revive anything."""
        with self._lock:
            self._roots_generation += 1
            notice = self._end_all_locked("roots_changed")
            generation = self._roots_generation
        self._notify(notice)
        return generation

    def observe_roots(self, digest: str) -> bool:
        """A roots digest seen by the loader or an evidence step. The first observation
        sets the baseline; any later value different from the last published one bumps
        (§5: a hand edit A → B → A ends everything once seen)."""
        with self._lock:
            if self._roots_digest is None:
                self._roots_digest = digest
                return False
            if digest == self._roots_digest:
                return False
            self._roots_digest = digest
            self._roots_generation += 1
            notice = self._end_all_locked("roots_changed")
        self._notify(notice)
        return True

    def mark_audit_failed(self) -> None:
        """The audit writer is unhealthy or a row failed (§7): sticky until restart."""
        with self._lock:
            if self._audit_failed:
                return
            self._audit_failed = True
            self._health_epoch += 1
            notice = self._end_all_locked("audit_failed")
        self._notify(notice)

    def audit_epoch(self) -> int | None:
        """The audit health epoch, or ``None`` once the audit failed (sticky)."""
        with self._lock:
            return None if self._audit_failed else self._health_epoch

    def audit_still_healthy(self, epoch: int | None) -> bool:
        """The final confirmation of a step that wrote and verified its rows (no I/O):
        the writer is healthy now, the audit has not failed and its health epoch is still
        the one read at the start."""
        if epoch is None:
            return False
        with self._grant_section(epoch) as refusal:
            return refusal is None

    def new_session(self) -> int:
        """``/new``: fence + 1 before the conversation is reset; a new session epoch."""
        with self._lock:
            notice = self._end_all_locked("new_session")
            self._session_epoch += 1
            epoch = self._session_epoch
        self._notify(notice)
        return epoch

    def revoke(self, allow_id: str) -> bool:
        """The person revokes one allowance on the list: its own generation + 1."""
        with self._lock:
            record = self._records.get(allow_id)
            if record is None or record.state not in ("reserved", "active"):
                return False
            notice = EndNotice("revoked", self._fence, self._fence)
            notice.ended.append(
                _Ended(record.allow_id, record.uses, record.generation, record.generation + 1)
            )
            record.generation += 1
            record.state = "ended"
            record.end_reason = "revoked"
        self._notify(notice)
        return True

    # -- view tokens (§4a step 1) ---------------------------------------------------

    def issue_view_token(
        self,
        *,
        request_id: str,
        args_hash: str,
        target: TargetClass,
        rule_generation: Any,
    ) -> str | None:
        """A one-time token for the card's offer, bound to the view snapshot. A newer view
        of the same card replaces (voids) the older token. ``None``: nothing is offered
        (the audit writer failed: the buttons stay hidden until restart)."""
        token = self._token()
        key = _digest(token)
        with self._grant_section() as refusal:
            if refusal is not None:
                return None
            old = self._view_of.pop(request_id, None)
            if old is not None:
                self._views.pop(old, None)
            self._views[key] = _ViewToken(
                request_id=request_id,
                args_hash=args_hash,
                session_epoch=self._session_epoch,
                target=target,
                rule_generation=rule_generation,
                snapshot=self._snapshot_locked(),
            )
            self._view_of[request_id] = key
        return token

    def view_live(self, request_id: str, token: Any, *, rule_generation: Any) -> bool:
        """Whether ``token`` is still the card's unspent view token and its snapshot and
        rule generation are still current — the page may keep it (one token per card for
        every tab that polls; a change issues a new one). Reads only; grants nothing."""
        if type(token) is not str or not token:
            return False
        key = _digest(token)
        with self._lock:
            view = self._views.get(key)
            return (
                view is not None
                and self._view_of.get(request_id) == key
                and view.session_epoch == self._session_epoch
                and view.snapshot == self._snapshot_locked()
                and view.rule_generation == rule_generation
                and not self._audit_failed
            )

    def void_view(self, request_id: str) -> None:
        """The card ended (decided, expired, cancelled): its token is dead."""
        with self._lock:
            key = self._view_of.pop(request_id, None)
            if key is not None:
                self._views.pop(key, None)

    def spend_view(
        self,
        token: Any,
        *,
        request_id: str,
        args_hash: str,
        target: TargetClass,
        rule_generation: Any,
    ) -> str | None:
        """Make Always's press (§9): the view token is checked like :meth:`reserve` and
        spent (single use) — no record is created. ``None``: the press may decide."""
        if type(token) is not str or not token:
            return "stale_view"
        key = _digest(token)
        with self._grant_section() as refusal:
            if key in self._spent:
                return "op_token_used"
            view = self._views.get(key)
            if view is None:
                return "stale_view"
            del self._views[key]
            self._view_of.pop(view.request_id, None)
            self._spent.add(key)
            if refusal is not None:
                return refusal
            if (
                view.request_id != request_id
                or view.args_hash != args_hash
                or view.session_epoch != self._session_epoch
                or view.snapshot != self._snapshot_locked()
                or view.rule_generation != rule_generation
            ):
                return "stale_view"
            if view.target != target:
                return "class_changed"
            return None

    def reserve(
        self,
        token: Any,
        *,
        request_id: str,
        args_hash: str,
        target: TargetClass,
        rule_generation: Any,
        roots_digest_value: str,
    ) -> tuple[Allowance | None, str | None]:
        """CAS of §4a step 1: the token is unused, of this incarnation and session, its
        view snapshot equals the current values, and the recomputed class and rule
        generation equal the view's. The token is spent (single use) either way once it
        was found; nothing is approved on a mismatch."""
        if type(token) is not str or not token:
            return None, "stale_view"
        key = _digest(token)
        # The issuance's clock reading goes through the common check and becomes the base
        # (review M2): an unreadable clock or a wall clock that went back ends everything.
        clock = self.check_clock()
        if clock is None:
            return None, "clock_anomaly"
        with self._grant_section() as refusal:
            if key in self._spent:
                return None, "op_token_used"
            view = self._views.get(key)
            if view is None:
                return None, "stale_view"
            # Found: spent now, whatever the result (a delayed click never retries it).
            del self._views[key]
            self._view_of.pop(view.request_id, None)
            self._spent.add(key)
            if refusal is not None:
                return None, refusal
            if (
                view.request_id != request_id
                or view.args_hash != args_hash
                or view.session_epoch != self._session_epoch
                or view.snapshot != self._snapshot_locked()
                or view.rule_generation != rule_generation
            ):
                return None, "stale_view"
            if view.target != target:
                return None, "class_changed"
            # Reserved records hold a slot too (released by commit failure / abort), so
            # concurrent presses past their reserve never exceed the limit.
            live = sum(
                1
                for r in self._records.values()
                if r.state in ("reserved", "active")
                and r.fence == self._fence
                and r.session_epoch == self._session_epoch
            )
            if live >= MAX_PER_SESSION:
                return None, "list_full"
            snap = view.snapshot
            record = Allowance(
                allow_id=self._token(),
                incarnation=self.incarnation,
                session_epoch=self._session_epoch,
                target=target,
                fence=snap.fence,
                breaker_departures=snap.departures,
                rule_generation=rule_generation,
                roots_generation=snap.roots_generation,
                roots_digest=roots_digest_value,
                health_epoch=snap.health_epoch,
                source_request_id=request_id,
                issued_at=clock,
                expires_at=float(clock.wall) + self.duration_sec,
            )
            self._records[record.allow_id] = record
            return replace(record), None

    def commit(
        self, allow_id: str, *, breaker_normal: bool, issued_ref: str, decision_ref: str
    ) -> str | None:
        """CAS of §4a step 3: still ``reserved`` and every counter still the reserved
        value, the live breaker read after the record step NORMAL → ``active``. Anything
        else removes the record; the reason (``issue_aborted_<cause>``) is returned."""
        with self._grant_section() as refusal:
            record = self._records.get(allow_id)
            if record is None:
                return "issue_aborted_fence"
            cause = None
            if (
                refusal is not None
                or self._audit_failed
                or record.health_epoch != self._health_epoch
            ):
                cause = "issue_aborted_audit"
            elif record.state != "reserved" or record.generation != 0:
                cause = (
                    "issue_aborted_departure"
                    if record.end_reason == "breaker"
                    else "issue_aborted_fence"
                )
            elif not breaker_normal or record.breaker_departures != self._departures:
                cause = "issue_aborted_departure"
            elif record.fence != self._fence or record.roots_generation != self._roots_generation:
                cause = "issue_aborted_fence"
            if cause is not None:
                del self._records[allow_id]
                return cause
            record.state = "active"
            record.generation = 1
            record.issued_ref = issued_ref
            record.decision_ref = decision_ref
            return None

    def abort(self, allow_id: str) -> None:
        with self._lock:
            self._records.pop(allow_id, None)

    # -- use and consumption (§4b, §4c) ---------------------------------------------

    def _deadline_passed(self, record: Allowance, now: Any) -> bool:
        return elapsed(record.issued_at, now) >= self.duration_sec

    def check_clock(self) -> Any:
        """Read the clocks; a wall clock that went back (or an unreadable clock) is an
        anomaly: every allowance ends (``clock_anomaly``) and ``None`` is returned."""
        notice = None
        with self._lock:
            now = self._clock_locked()
            if now is None:
                notice = self._end_all_locked("clock_anomaly")
        self._notify(notice)
        return now

    def _clock_locked(self) -> Any:
        """One clock reading under the lock (a system call, not I/O), checked against the
        last one: ``None`` when it cannot be read or the wall clock went back — the caller
        then ends everything (irreversible). Every reading becomes the new base, the one
        taken at issuance included, so a jump right after a press is seen too."""
        try:
            now = self._clock()
            wall = float(now.wall)
        except Exception:  # noqa: BLE001 - an unreadable clock is an anomaly
            return None
        last = self._last_clock
        self._last_clock = now
        if last is not None and wall < float(last.wall) - CLOCK_SLACK_SEC:
            return None
        return now

    def find(self, tool: str, call: TargetClass, *, rule_generation: Any) -> Allowance | None:
        """The live allowance whose class contains this call (evidence helper)."""
        with self._lock:
            for record in self._records.values():
                if (
                    self._live_locked(record)
                    and record.rule_generation == rule_generation
                    and record.target.class_version == CLASS_VERSION
                    and class_contains(record.target, call)
                ):
                    return replace(record)
        return None

    def use(
        self,
        allow_id: str,
        *,
        snapshot: Snapshot,
        call: TargetClass,
        args_hash: str,
    ) -> tuple[UseBinding | None, str | None]:
        """CAS of §4b: the record is ``active`` at generation 1, the counters equal both
        the record's and the evidence snapshot's, and the deadline has not passed (the
        clock is read inside the CAS)."""
        notice = None
        with self._grant_section() as refusal:
            now = None if refusal is not None else self._clock_locked()
            if refusal is not None:
                code = refusal
            elif now is None:
                notice = self._end_all_locked("clock_anomaly")
                code = "clock_anomaly"
            else:
                binding, code, notice = self._use_locked(allow_id, snapshot, call, args_hash, now)
                if binding is not None:
                    return binding, None
        self._notify(notice)
        return None, code

    def _use_locked(self, allow_id, snapshot, call, args_hash, now):
        record = self._records.get(allow_id)
        if record is None or not self._live_locked(record):
            return None, "ended", None
        current = self._snapshot_locked()
        if current != snapshot or not self._matches_record_locked(record, current):
            return None, "moved", None
        if not class_contains(record.target, call):
            return None, "class_mismatch", None
        if self._deadline_passed(record, now):
            notice = EndNotice("time", self._fence, self._fence)
            notice.ended.append(
                _Ended(record.allow_id, record.uses, record.generation, record.generation + 1)
            )
            record.generation += 1
            record.state = "ended"
            record.end_reason = "time"
            return None, "time", notice
        binding = UseBinding(
            nonce=self._token(),
            allow_id=allow_id,
            generation=record.generation,
            snapshot=current,
            tool=call.tool,
            target_key=call.class_key,
            args_hash=args_hash,
        )
        self._bindings[binding.nonce] = binding
        return binding, None, None

    def _matches_record_locked(self, record: Allowance, current: Snapshot) -> bool:
        return (
            record.fence == current.fence
            and record.breaker_departures == current.departures
            and record.roots_generation == current.roots_generation
            and record.health_epoch == current.health_epoch
            and not self._audit_failed
        )

    def consume(self, binding: Any, *, snapshot: Snapshot) -> tuple[Allowance | None, str | None]:
        """CAS of §4c step 2: exactly once per binding; the record still ``active`` at the
        bound generation; the counters equal the binding's **and** the evidence
        snapshot's; the deadline has not passed by a clock read **inside** this CAS (no
        reading taken before the lock decides it) → ``uses += 1``."""
        if not isinstance(binding, UseBinding):
            return None, "binding_invalid"
        notice = None
        with self._grant_section() as refusal:
            now = None if refusal is not None else self._clock_locked()
            if refusal is not None:
                self._bindings.pop(binding.nonce, None)
                code = refusal
            elif now is None:
                self._bindings.pop(binding.nonce, None)
                notice = self._end_all_locked("clock_anomaly")
                code = "clock_anomaly"
            else:
                record, code = self._consume_locked(binding, snapshot, now)
                if record is not None:
                    return record, None
        self._notify(notice)
        return None, code

    def _consume_locked(self, binding, snapshot, now):
        issued = self._bindings.get(binding.nonce)
        if issued is None:
            return None, "binding_spent"
        if issued != binding:
            return None, "binding_invalid"
        del self._bindings[binding.nonce]  # spent: a second consume never runs
        record = self._records.get(binding.allow_id)
        if record is None or not self._live_locked(record):
            return None, "ended"
        if record.generation != binding.generation:
            return None, "ended"
        current = self._snapshot_locked()
        if (
            current != binding.snapshot
            or current != snapshot
            or not self._matches_record_locked(record, current)
        ):
            return None, "moved"
        if self._deadline_passed(record, now):
            return None, "time"
        record.uses += 1
        return replace(record), None

    def void(self, binding: Any) -> bool:
        """An unconsumed binding is dropped (the call was refused by a later check)."""
        with self._lock:
            nonce = getattr(binding, "nonce", None)
            return self._bindings.pop(nonce, None) is not None

    # -- save tokens (§9, the Make Always confirmation) -----------------------------

    def issue_save_token(
        self, *, request_id: str, tool: str, hub_version: str, rule_generation: Any
    ) -> str | None:
        token = self._token()
        with self._grant_section() as refusal:
            if refusal is not None:
                return None
            self._saves[_digest(token)] = _SaveToken(
                request_id, tool, hub_version, rule_generation, self._fence
            )
        return token

    def spend_save_token(
        self,
        token: Any,
        *,
        request_id: str,
        tool: str,
        hub_version: str,
        rule_generation: Any,
    ) -> str | None:
        """Checked and spent in the save's precommit, before the audit row and the file.
        ``None``: accepted. ``op_token_used``: a second send. ``stale_confirmation``: a
        stop, ``/new``, a departure, a rule or roots save (the fence), another save (the
        version / generation), or a token of another process (a restart)."""
        if type(token) is not str or not token:
            return "stale_confirmation"
        key = _digest(token)
        with self._grant_section() as refusal:
            if key in self._spent:
                return "op_token_used"
            found = self._saves.pop(key, None)
            if found is None:
                return "stale_confirmation"
            self._spent.add(key)
            if (
                found.request_id != request_id
                or found.tool != tool
                or found.hub_version != hub_version
                or found.rule_generation != rule_generation
                or found.fence != self._fence
                or refusal is not None
            ):
                return "stale_confirmation"
            return None


def keyed_ref(key: bytes, text: str) -> str:
    return hmac.new(key, text.encode("utf-8", "surrogatepass"), hashlib.sha256).hexdigest()


_TABLE: AllowanceTable | None = None
_TABLE_LOCK = threading.Lock()


def table() -> AllowanceTable:
    """The process's one table (created on first use; in memory only)."""
    global _TABLE
    if _TABLE is None:
        with _TABLE_LOCK:
            if _TABLE is None:
                _TABLE = AllowanceTable()
    return _TABLE


def publish_end(reason: str) -> None:
    """End every allowance for ``reason`` (stop, ``/new``, a rule save …). No-op cost when
    nothing exists: only in-memory integers move. Never raises."""
    with contextlib.suppress(Exception):  # a narrowing never fails its caller
        if reason == "new_session":
            table().new_session()
        else:
            table().bump_fence(reason)


def publish_departure() -> None:
    """The breaker is not NORMAL (see :meth:`AllowanceTable.bump_departure`). Never raises."""
    with contextlib.suppress(Exception):
        table().bump_departure()


def publish_roots(policy_or_digest: Any) -> None:
    """A roots save (before the file is replaced) or a loader observation."""
    try:
        digest = (
            policy_or_digest
            if isinstance(policy_or_digest, str)
            else roots_digest(policy_or_digest)
        )
        table().observe_roots(digest)
    except Exception:  # noqa: BLE001 - an unreadable policy ends everything instead
        publish_end("read_failed")
