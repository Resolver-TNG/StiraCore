"""Heartbeat-driven start of a goal iteration (ADR 0011 §8, ADR 0021; W6d of #296).

The scheduler's ``grant_tick`` fire kind: on every scheduler wake (its periodic loop, at
most every ``max_wait_sec``, and an *event-driven* wake after each heartbeat tick) the
:class:`GrantTicker` evaluates — **without any model call**, without building a model or a
client, without touching the executor's parent agent — whether the one live grant may
start an iteration now. If it may, it presses through the **same**
:meth:`~stiracore.grants.executor.GoalIterationExecutor.step` entry a person's "Run one
iteration" press uses (the same permits, budget reservation, tool allow-list,
pre-emption and commit rules), passing its own clauses, which ``step`` checks again on
the admission's own look. At most one iteration per wake.

Built only with ``autonomy.heartbeat_start`` (default off; it needs ``stage_c`` and
``stage_c_exec``) **and** a running scheduler; a grant takes part only when it was issued
with its own ``heartbeat_start`` flag (host-confirmed at issue, never changed afterwards).
With either off there is no ticker (global) or nothing visible (the grant).

**Admission** (all required; the first failing clause is the skip reason):
the switch on, the executor there and not stopping, no kill, the authority and the grant
store healthy, a live grant, its flag on, grant ``active``, goal ``active`` (not
``awaiting_verification`` — only the user verifies), the breaker NORMAL, the heartbeat
healthy (the scheduler's ADR 0021 §8 test), a clock reading with TTL left on both clocks,
iterations below the maximum, budget left, no iteration running or starting, no abandoned
provider call, no person's turn arriving or holding the turn lock, the minimum spacing
(``autonomy.tick_min_spacing_sec``) since the grant's issue, since the previous
iteration's end (any origin) and since the grant's last resume (#644 U3), the
**no-action gate** (two consecutive ``iteration_no_action`` ends since the last person's
press or resume: ticks stay skipped until a person presses or resumes) and, on an
event-driven wake only, the **flood guard** (at most :data:`FLOOD_STARTS` heartbeat starts
in :data:`FLOOD_WINDOW_SEC`).

**A skip is recorded, never deferred**: ``tick_skipped(<reason>)`` (:data:`SKIP_REASONS`)
counted per grant; there is no queue, no backlog, no catch-up and no replay — a wake after
a sleep or a restart is one wake (missed ones coalesce; ADR 0021 forbids catch-up for
model-calling work). After a start every grant is ``revoked(restart)``, so the first tick
does nothing (``no_grant``). Unknown is a skip (fail closed).

**Records** are codes and counters only, in memory (nothing is persisted): the counts per
reason, the last reason, the heartbeat starts. A *state-class* skip (one that lasts until
a state changes, :data:`STATE_SKIPS`) also posts one lifecycle event ``grant_tick``
``active → paused`` ``tick_skipped_<reason>`` — again for the same reason only after a
heartbeat start or another state-class reason in between (at most
:data:`EVENTS_PER_GRANT` per grant in this process); a heartbeat start posts ``idle →
running`` ``tick_started``. ``unavailable`` (:data:`UNAVAILABLE_CODES`): the server's
start-up finding that nothing can start (the heartbeat is off), shown on a flagged grant.

**Locks**: ``_lock`` is a leaf (counters only): no I/O, no callback and no other lock
under it; the reads of the grant service and the executor, the press and the lifecycle
posts all run outside it, on the scheduler's own thread (no new thread).
"""

from __future__ import annotations

import logging
import math
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from typing import Any

__all__ = [
    "EVENT_REASONS",
    "FLOOD_STARTS",
    "FLOOD_WINDOW_SEC",
    "NO_ACTION_LIMIT",
    "SKIP_REASONS",
    "STATE_SKIPS",
    "UNAVAILABLE_CODES",
    "GrantTicker",
    "WakeStamp",
]

logger = logging.getLogger(__name__)

#: ``tick_skipped(<reason>)``: why a wake did not start an iteration (fixed codes).
SKIP_REASONS = (
    "not_enabled",
    "no_grant",
    "grant_state",
    "goal_state",
    "awaiting_verification",
    "heartbeat_unhealthy",
    "breaker",
    "kill",
    "authority_unhealthy",
    "store_unhealthy",
    "clock",
    "ttl",
    "max_iterations",
    "budget",
    "turn_busy",
    "iteration_running",
    "abandoned_call",
    "min_spacing",
    "no_action",
    "flood",
    "executor_unavailable",
    "executor_refused",
    # fix round 1: the wake's stamp no longer holds, the executor or the scheduler stopping,
    # repeated failed iterations
    "narrowed",
    "scheduler_stopped",
    "shutdown",
    "repeated_failure",
)
#: The skips that last until a state changes: one lifecycle event per (grant, reason).
STATE_SKIPS = frozenset(
    {
        "grant_state",
        "goal_state",
        "awaiting_verification",
        "heartbeat_unhealthy",
        "breaker",
        "kill",
        "authority_unhealthy",
        "store_unhealthy",
        "clock",
        "ttl",
        "max_iterations",
        "budget",
        "no_action",
        "repeated_failure",
        "executor_unavailable",
    }
)
#: The lifecycle reason codes this module posts (``authority.lifecycle_events`` keeps
#: the same set; a test keeps them in step).
EVENT_REASONS = frozenset({"tick_started"} | {f"tick_skipped_{r}" for r in STATE_SKIPS})

FLOOD_STARTS = 5
FLOOD_WINDOW_SEC = 60.0
#: Consecutive ``iteration_no_action`` ends that stop the heartbeat start.
NO_ACTION_LIMIT = 2
#: Consecutive failed ends (:data:`FAILED_ENDS`) that stop the heartbeat start until a
#: person presses or resumes (fix round 1, Opus N5: a throttled provider never loops).
FAILURE_LIMIT = 3
FAILED_ENDS = frozenset({"failed", "model_error", "timeout"})
# The same evaluation failure is logged at most once per this many seconds (Opus N8).
WARN_EVERY_SEC = 300.0
# How many grants keep counters (v1 has one live grant; older ones are dropped).
GRANTS_KEPT = 4
# Lifecycle events per grant in a process, at most (a backstop for a flapping reason).
EVENTS_PER_GRANT = 32
#: Why the heartbeat start cannot start anything on this server (shown on a flagged grant).
UNAVAILABLE_CODES = ("scheduler_off", "heartbeat_off")

# The executor's refusal codes (``GoalIterationExecutor.step``) → skip reasons; anything
# else is ``executor_refused``. A refusal by the tick's own re-check passes its reason.
_REFUSALS = {
    "killed": "kill",
    "authority_unhealthy": "authority_unhealthy",
    "store_unhealthy": "store_unhealthy",
    "grant_not_active": "grant_state",
    "goal_not_active": "goal_state",
    "breaker": "breaker",
    "clock": "clock",
    "ttl": "ttl",
    "max_iterations": "max_iterations",
    "budget_exhausted": "budget",
    "iteration_running": "iteration_running",
    "abandoned_call": "abandoned_call",
    "user_turn": "turn_busy",
    "executor_stopped": "shutdown",
    "playbook_unavailable": "executor_unavailable",
    "token_count_unavailable": "executor_unavailable",
    "provider_unsupported": "executor_unavailable",
    "thread_unavailable": "executor_unavailable",
    "workspace_changed": "goal_state",
    "recheck": "grant_state",
}


def _finite(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def _since(then: Any, now: Any) -> float:
    """#644 U3: seconds from one clock reading to another — the largest of the three clock
    deltas, as for the issue's spacing (``state.elapsed``); unreadable: ``-inf`` (a skip)."""
    try:
        deltas = [now.wall - then.wall, now.boot - then.boot, now.mono - then.mono]
    except Exception:  # noqa: BLE001 - unreadable: never past the spacing
        return -math.inf
    return max(deltas) if all(_finite(d) for d in deltas) else -math.inf


class GrantTicker:
    """The ``grant_tick`` fire kind (see the module docstring). ``grants``: the grant
    service; ``executor``: the goal iteration executor (``None``: every wake is skipped
    ``executor_unavailable``); ``settings``: the autonomy settings (read at start);
    ``authority``: the authority service (kill / health / incarnation / epoch);
    ``recorder``: the lifecycle recorder (``None``: no events)."""

    def __init__(
        self,
        grants: Any,
        executor: Any,
        *,
        settings: Any,
        authority: Any,
        recorder: Any = None,
        unavailable: str | None = None,
        flood_starts: int = FLOOD_STARTS,
        flood_window_sec: float = FLOOD_WINDOW_SEC,
    ) -> None:
        self._grants = grants
        self._executor = executor
        self._settings = settings
        self._authority = authority
        self._recorder = recorder
        self._flood_starts = max(1, int(flood_starts))
        self._flood_window = float(flood_window_sec)
        self._lock = threading.Lock()  # leaf: counters only
        # grant id -> {"counts": {reason: n}, "started": n, "last": code}; bounded.
        self._per_grant: OrderedDict[str, dict] = OrderedDict()
        self._no_grant: dict[str, int] = {}
        self._starts: deque[float] = deque(maxlen=self._flood_starts)
        # W6d decision (4): a state-class reason is posted again only after a heartbeat start
        # or another state-class reason in between (per grant: the last one posted).
        self.unavailable = unavailable if unavailable in UNAVAILABLE_CODES else None
        self._warned: dict[str, float] = {}
        self.wakes = 0

    @property
    def spacing_sec(self) -> int:
        return int(self._settings.tick_min_spacing_sec)

    # -- the wake --------------------------------------------------------------------------

    def fire(
        self,
        *,
        source: str = "periodic",
        heartbeat_healthy: Callable[[], Any],
        run_token: Any = None,
    ) -> str:
        """One wake (``source``: ``periodic`` or ``event``; ``run_token``: the scheduler's
        run token — ``valid`` until it stops; none: ``scheduler_stopped``): at most one
        press. Returns ``"started"`` or the skip reason. Never raises; no model call."""
        event = source == "event"
        grant = None
        try:
            code, grant, stamp = self._admission(heartbeat_healthy, event, run_token)
            if code is None:
                code = self._press(stamp)
        except Exception as error:  # noqa: BLE001 - unknown is a skip (fail closed)
            self._warn(type(error).__name__)
            code = "executor_refused"
        self._record(grant, code)
        return code

    def _warn(self, kind: str) -> None:
        """One warning per failure kind per :data:`WARN_EVERY_SEC` (memory only)."""
        now = time.monotonic()
        with self._lock:
            last = self._warned.get(kind)
            if last is not None and now - last < WARN_EVERY_SEC:
                return
            self._warned[kind] = now
            while len(self._warned) > 16:
                self._warned.pop(next(iter(self._warned)))
        logger.warning("grants: a grant tick could not be evaluated (%s); skipped", kind)

    def _admission(self, heartbeat_healthy, event: bool, run_token: Any):
        """The clauses at the wake: ``(skip reason or None, grant, wake stamp)``. The
        stamp's marks — the authority epoch and the person's-turn counter — are read
        first, before any clause (W6b order: the signal, then the lock)."""
        executor = self._executor
        if self._settings.heartbeat_start is not True:
            return "not_enabled", None, None
        if executor is None:
            return "executor_unavailable", None, None
        if getattr(run_token, "valid", False) is not True:
            return "scheduler_stopped", None, None
        seq = executor.preempt_signal.seq
        epoch = self._authority.epoch
        code = self._health()
        if code is not None:
            return code, None, None
        grant = self._grants.tick_record()
        if grant is None:
            return "no_grant", None, None
        if grant.heartbeat_start is not True:
            return "not_enabled", grant, None
        code = self._grant_clauses(grant, heartbeat_healthy)
        if code is not None:
            return code, grant, None
        facts = executor.tick_facts(grant.grant_id)
        if facts.get("stopping") is not False:
            return "shutdown", grant, None
        if facts.get("running") is not False:
            return "iteration_running", grant, None
        if facts.get("abandoned") != 0:
            return "abandoned_call", grant, None
        if facts.get("turn_busy") is not False:
            return "turn_busy", grant, None
        code = self._history_clauses(grant, facts)
        if code is not None:
            return code, grant, None
        if event and self._flooded(facts.get("now")):
            return "flood", grant, None
        stamp = WakeStamp(self, grant, epoch, seq, heartbeat_healthy, run_token, facts.get("now"))
        code = stamp.live()  # nothing moved while the clauses were read
        return code, grant, (None if code else stamp)

    def _health(self) -> str | None:
        if self._authority.killed:
            return "kill"
        if self._authority.unhealthy_reason is not None:
            return "authority_unhealthy"
        if self._grants.unhealthy_reason is not None:
            return "store_unhealthy"
        return None

    def _grant_clauses(self, grant: Any, heartbeat_healthy) -> str | None:
        """The clauses on one grant record (at the wake, and again on ``step``'s look)."""
        from stiracore.grants import state

        if grant.heartbeat_start is not True:
            return "not_enabled"
        if grant.status != "active":
            return "grant_state"
        if grant.goal_status == "awaiting_verification":
            return "awaiting_verification"
        if grant.goal_status != "active":
            return "goal_state"
        if state.halt_reason_for(self._grants.breaker_level()) is not None:
            return "breaker"
        if heartbeat_healthy() is not True:
            return "heartbeat_unhealthy"
        now = self._grants.clock_reading()
        if now is None:
            return "clock"
        if state.ttl_left(grant, now) <= 0:  # both clocks (the largest delta since issue)
            return "ttl"
        # W6d decision (3): no heartbeat iteration within the spacing of the grant's issue
        # (the person who just issued it may still be editing); the grant's clocks.
        if state.elapsed(grant, now) < self.spacing_sec:
            return "min_spacing"
        # #644 U3 (W6-F): nor within the spacing of the grant's last resume (the person who
        # just resumed it may still be changing a rule); measured like the issue's spacing.
        resumed, at = self._grants.resumed_at(grant.grant_id)
        if resumed and (at is None or _since(at, now) < self.spacing_sec):
            return "min_spacing"
        if grant.iterations >= grant.max_iterations:
            return "max_iterations"
        if grant.used_tokens >= grant.total_tokens:
            return "budget"
        return None

    def _history_clauses(self, grant: Any, facts: dict) -> str | None:
        """The minimum spacing and the no-action gate, from the executor's ends."""
        ends = facts.get("ends")
        if not isinstance(ends, list):
            return "min_spacing"  # unreadable: a skip
        if ends:
            ended = ends[-1][2]
            now = facts.get("now")
            if not (_finite(ended) and _finite(now)) or now - ended < self.spacing_sec:
                return "min_spacing"
        if self._no_action_streak(grant, ends) >= NO_ACTION_LIMIT:
            return "no_action"
        if self._streak(grant, ends, FAILED_ENDS) >= FAILURE_LIMIT:
            return "repeated_failure"
        return None

    @staticmethod
    def _streak(grant: Any, ends: list, codes: frozenset) -> int:
        """Consecutive ends with one of ``codes``, newest first, since the last person's
        press (its own end counts) or resume (the grant's stop epoch moved)."""
        streak = 0
        for origin, end, _ended, stop_epoch in reversed(ends):
            if stop_epoch != grant.stop_epoch or end not in codes:
                break
            streak += 1
            if origin != "tick":
                break
        return streak

    @classmethod
    def _no_action_streak(cls, grant: Any, ends: list) -> int:
        """Consecutive ``no_action`` ends, newest first, since the last person's press
        (its own end counts, then the count stops) or resume (a resume follows a stop,
        and every stop moves the grant's ``stop_epoch``: an end from before it is not
        counted)."""
        return cls._streak(grant, ends, frozenset({"no_action"}))

    def _flooded(self, now: Any) -> bool:
        if not _finite(now):
            return True
        with self._lock:
            recent = [t for t in self._starts if now - self._flood_window < t <= now]
        return len(recent) >= self._flood_starts

    # -- the press ---------------------------------------------------------------------------

    def _press(self, stamp: WakeStamp) -> str:
        """The same entry as a person's press, carrying the wake's stamp: ``step`` checks
        it on its admission's look, at the publication (inside the authority's fence) and
        right before the iteration's first effect."""
        from stiracore.grants.schema import GrantRefused

        try:
            self._executor.step(stamp.grant_id, tick=stamp)
        except GrantRefused as error:
            reason = error.extra.get("reason")
            if reason in SKIP_REASONS:
                return reason
            return _REFUSALS.get(reason, "executor_refused")
        now = stamp.now
        with self._lock:
            self._starts.append(now if _finite(now) else math.inf)
        self._post(stamp.grant_id, "idle", "running", "tick_started")
        return "started"

    # -- records ---------------------------------------------------------------------------

    def _record(self, grant: Any, code: str) -> None:
        if code not in SKIP_REASONS and code != "started":
            code = "executor_refused"
        grant_id = getattr(grant, "grant_id", None)
        post = False
        with self._lock:
            self.wakes += 1
            if type(grant_id) is not str:
                self._no_grant[code] = self._no_grant.get(code, 0) + 1
                return
            entry = self._per_grant.get(grant_id)
            if entry is None:
                entry = {"counts": {}, "started": 0, "last": "", "posted": "", "events": 0}
                self._per_grant[grant_id] = entry
                while len(self._per_grant) > GRANTS_KEPT:
                    self._per_grant.popitem(last=False)
            if code == "started":
                entry["started"] += 1
                entry["posted"] = ""  # a start: the same reason may be posted again later
            else:
                entry["counts"][code] = entry["counts"].get(code, 0) + 1
            entry["last"] = code
            if (
                code in STATE_SKIPS
                and code != entry["posted"]
                and entry["events"] < EVENTS_PER_GRANT
            ):
                entry["posted"] = code
                entry["events"] += 1
                post = True
        if post:
            self._post(grant_id, "active", "paused", f"tick_skipped_{code}")

    def _post(self, grant_id: str, before: str, after: str, code: str) -> None:
        recorder = self._recorder
        if recorder is None:
            return
        from stiracore.authority.lifecycle_events import make_event

        try:
            recorder.post(
                make_event(
                    "grant_tick",
                    grant_id,
                    before,
                    after,
                    code,
                    incarnation=self._authority.incarnation,
                    epoch=self._authority.epoch,
                    ts_wall=time.time(),
                )
            )
        except Exception:  # noqa: BLE001 - a refused event latches the authority unhealthy
            self._authority.report_unhealthy("lifecycle_event_invalid")

    # -- views (codes and counters only) ---------------------------------------------------

    def _view_locked(self, grant_id: Any) -> dict:
        entry = self._per_grant.get(grant_id) if type(grant_id) is str else None
        counts = dict(entry["counts"]) if entry else {}
        return {
            "spacing_sec": self.spacing_sec,
            "started": entry["started"] if entry else 0,
            "last": entry["last"] if entry else "",
            "skipped": {r: counts[r] for r in SKIP_REASONS if r in counts},
            **({"unavailable": self.unavailable} if self.unavailable else {}),
        }

    def view(self, grant_id: Any) -> dict:
        """The tick block of one flagged grant (``/api/grants``): the counts per skip
        reason, the last reason, the starts, the spacing and the cost rows of its last
        heartbeat-started iterations (numbers only)."""
        with self._lock:
            out = self._view_locked(grant_id)
        out["costs"] = [] if self._executor is None else self._executor.tick_costs(grant_id)
        return out

    def board(self, grant_id: Any) -> dict:
        """The Stage board's read: :meth:`view` without the costs (the executor's board
        read carries them, in its own consistent snapshot); never waits — a held lock
        raises ``BoardBusy`` at once."""
        from stiracore.grants.schema import BoardBusy

        if not self._lock.acquire(blocking=False):
            raise BoardBusy()
        try:
            return self._view_locked(grant_id)
        finally:
            self._lock.release()

    def status(self) -> dict:
        """The settings panel's line: the wakes and the skips with no live grant."""
        with self._lock:
            return {
                "enabled": True,
                "wakes": self.wakes,
                "spacing_sec": self.spacing_sec,
                "no_grant": {r: self._no_grant[r] for r in SKIP_REASONS if r in self._no_grant},
            }


class WakeStamp:
    """One wake's admission, carried into ``GoalIterationExecutor.step`` (fix round 1 of
    W6d: the check and the act are one fenced step). Its marks were read at the wake:

    * ``epoch`` — the authority epoch: the publication runs under
      ``commit_at_epoch(expected=epoch)`` and the iteration's first effect needs a permit
      of this epoch (any narrowing since the wake — a stop, a pause and resume, a breaker
      change, the kill — refuses);
    * ``seq`` — the person's-turn counter: the iteration's ``seq0`` (a turn that came and
      went since the wake refuses / yields at the first boundary);
    * ``stop_epoch`` — the grant's own stops; the scheduler's ``run_token`` (cleared at
      its stop) and the switch.

    :meth:`live` is memory only (attribute reads; it runs inside the authority's fence);
    :meth:`fresh` adds the heartbeat health (read fresh, outside every lock);
    :meth:`recheck` the grant clauses on ``step``'s own look."""

    __slots__ = (
        "_heartbeat",
        "_ticker",
        "_token",
        "epoch",
        "executor",
        "grant_id",
        "now",
        "seq",
        "stop_epoch",
        "used",
    )

    def __init__(self, ticker, grant, epoch, seq, heartbeat, token, now) -> None:
        self._ticker = ticker
        self.executor = ticker._executor  # fix round 2: the executor this wake is for
        self.used = False  # set at the publication (single use)
        self.grant_id = grant.grant_id
        self.stop_epoch = grant.stop_epoch
        self.epoch = epoch
        self.seq = seq
        self._heartbeat = heartbeat
        self._token = token
        self.now = now

    def live(self, *, fenced: bool = False) -> str | None:
        """Memory only (no I/O, no lock, no callback): the switch, the scheduler's token,
        the kill flag, the person's-turn counter and mark, and — unless ``fenced`` (called
        inside ``commit_at_epoch(expected=epoch)``, which holds the authority's leaf lock
        and has checked the epoch itself) — the authority epoch."""
        ticker = self._ticker
        if ticker._settings.heartbeat_start is not True:
            return "not_enabled"
        if getattr(self._token, "valid", False) is not True:
            return "scheduler_stopped"
        if ticker._executor is not self.executor:
            return "executor_unavailable"  # the executor was replaced since the wake
        if ticker._authority.killed:
            return "kill"
        signal = self.executor.preempt_signal
        if signal.seq != self.seq or signal.pending:
            return "turn_busy"
        if not fenced and ticker._authority.epoch != self.epoch:
            return "narrowed"
        return None

    def fresh(self) -> str | None:
        """:meth:`live` and the heartbeat health read now (outside every lock)."""
        code = self.live()
        if code is not None:
            return code
        try:
            healthy = self._heartbeat() is True
        except Exception:  # noqa: BLE001 - unreadable: not healthy
            healthy = False
        return None if healthy else "heartbeat_unhealthy"

    def recheck(self, grant: Any) -> str | None:
        """On ``step``'s own look: the same grant (no stop or resume since the wake), its
        clauses and the executor's history clauses, then :meth:`fresh`."""
        ticker = self._ticker
        if grant.grant_id != self.grant_id or grant.stop_epoch != self.stop_epoch:
            return "grant_state"
        code = ticker._grant_clauses(grant, self._heartbeat)
        if code is None:
            code = ticker._history_clauses(grant, ticker._executor.tick_facts(grant.grant_id))
        return code or self.fresh()
