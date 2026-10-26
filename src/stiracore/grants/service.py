"""The grant control plane (ADR 0011 §8, §9, §11, §12; W4 of #296).

One :class:`GrantService` per server process, built only when the gateway switch
``autonomy.stage_c`` is on (:func:`settings_from`). It holds the grants in memory, writes
them to the grant store (:mod:`.store`) and records every transition as a W2 lifecycle
event. **It runs nothing itself**: no model, no tool, no goal iteration (the W5 executor,
:mod:`.executor`, behind its own switch ``autonomy.stage_c_exec``, does). Its
only thread is the bounded single-flight store writer (``grants-persist``, the W2 kill
persistence pattern); the periodic look is a step of the authority watchdog's pass
(:meth:`GrantService.sync`).

Human operations (routes in :mod:`.routes`, behind the server's Host / Origin / CSRF
checks; none is a model-visible tool). Every **loosening** — issue, resume, verify — is a
host-confirmed operation of :class:`stiracore.yolo.host_ops.HostOperations`: the YOLO
desk's own operations (and so its effective channel: under ``STIRA_YOLO_HOST_CONFIRM=
command|dialog`` a browser confirmation is refused, ``host_confirm_on_host``) or, without
YOLO, a browser-channel table of its own. **A browser channel refuses the loosenings**
(``host_channel_required``, operator decision 2026-10-25) unless the user chose it with
``autonomy.allow_browser_confirm`` — the weaker layer a same-user process can drive. Every operation step is refused while an agent
tool call runs (the tool-call tracker is registered for grants with or without YOLO; no
tracker → ``host_channel_unavailable``). Single use; a stale target is refused; the same op
id with the same body returns the original result, another body is refused.

* **issue** — typed confirmation (``confirm: "ISSUE"``) + the operation. **Fenced**: the
  authority epoch is captured at the start; the grant is written durably first (as
  ``revoked(restart)``, never as active) and then published with the W1
  ``commit_at_epoch`` — a breaker departure, ``/new``, the kill switch or an unhealthy
  authority in between aborts the issuance (``grant_issue_aborted_epoch``). Nothing is
  usable before the durable write returned. At most one live grant (v1).
* **revoke** / **pause** — one press. The stop is in memory and the epoch has advanced
  before any store I/O; the write is bounded (never waits on a hung disk);
* **resume** — the operation (a halt also ``confirm: "RESUME"``, refused unless the
  breaker is NORMAL; sets ``recheck``);
* **verify** — the operation; only while the goal is ``awaiting_verification`` and the
  grant ``active``; after the end recorded once (``late_verification``).

Automatic stops (in memory at once from the hooks; every watchdog pass and every request
otherwise): the kill switch (a kill listener of the authority service) → ``revoked
(kill_switch)``; an unhealthy authority or grant store → ``revoked(unhealthy)``; the breaker
leaving NORMAL → ``halted(breaker_*)``; a clock anomaly → ``revoked(clock_anomaly)``; the
TTL → ``expired(ttl)``; ``/new`` → ``revoked(session_reset)`` (always an epoch advance).
:meth:`GrantService.check_now` runs these checks for one grant; ``authority_record`` and
``claim_complete`` (the W5 entry points) go through it.

**Locks.** ``_lock`` is a leaf: in-memory compare-and-set only — no I/O, no callback, no
breaker or authority call while it is held, except the W1 ``commit_at_epoch`` (a leaf CAS,
lock order grants → authority). The clock reads under it are the look's (#627) and the
verify commit's (#620 W5-2): the in-memory clocks (``clock_gettime``; no file, no lock),
read inside the same hold that applies them, so readings reach the grants in the order
they were taken (a later reading applied first made the earlier one look like a monotonic
clock going back → a false ``clock_anomaly``). ``_cond`` guards the writer's counters only.

**The goal text** lives in ``_goals`` and in the grant store only. Views, events, errors
and logs carry the ``grant_id``; the goal is shown only by :meth:`goal_view`, once, to the
holder of the issuer's one-time view token, with the typed word ``SHOW``.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from typing import Any

from stiracore.authority.lifecycle_events import make_event
from stiracore.yolo.host_ops import (
    HostOperations,
    HostOpRefused,
    Unbindable,
    binding_digest,
    default_clock,
    instance_ref,
    target_digest,
)

from . import state
from .schema import (
    RESUME_WORD,
    SHOW_WORD,
    TOOLS,
    BoardBusy,
    Grant,
    GrantRefused,
    evidence_to_dict,
    grant_to_dict,
    parse_issue,
)
from .store import MAX_GRANTS, GrantStore

__all__ = [
    "AutonomySettings",
    "GrantConfirmation",
    "GrantService",
    "settings_from",
]

logger = logging.getLogger(__name__)

ISSUER = "dashboard"
_RESULTS_KEPT = 64
# How long a request waits for its store write (the write itself runs on the writer and
# may take longer; a stop never waits for it). Issue needs the write; the others do not.
CHANNEL_HINT = (
    "Stage C needs a host confirmation: set STIRA_YOLO_ALLOW and STIRA_YOLO_HOST_CONFIRM="
    "auto|dialog|command (macOS auto = the desktop dialog), or choose the weaker browser "
    "confirmation with gateway autonomy.allow_browser_confirm: true."
)
WRITE_WAIT_SEC = 1.0
ISSUE_WRITE_WAIT_SEC = 5.0


# W5: the iteration's wall time (``autonomy.iteration_wall_sec``): default 10 min, bounded.
ITERATION_WALL_DEFAULT_SEC = 600
ITERATION_WALL_MIN_SEC = 60
ITERATION_WALL_MAX_SEC = 3600
# W6c: after how long an unverified completion claim says "still waiting for you"
# (``autonomy.claim_stale_sec``): default 30 min, bounded. Display only.
CLAIM_STALE_DEFAULT_SEC = 1800
CLAIM_STALE_MIN_SEC = 60
CLAIM_STALE_MAX_SEC = 86400
# W6d: the shortest time between an iteration's end and a heartbeat-started one
# (``autonomy.tick_min_spacing_sec``): default 5 min, floor 30 s, cap 1 day.
TICK_SPACING_DEFAULT_SEC = 300
TICK_SPACING_MIN_SEC = 30
TICK_SPACING_MAX_SEC = 86400
_AUTONOMY_KEYS = frozenset(
    {
        "stage_c",
        "run_checks_unattended",
        "allow_browser_confirm",
        "stage_c_exec",
        "iteration_wall_sec",
        "claim_stale_sec",
        "heartbeat_start",
        "tick_min_spacing_sec",
    }
)


@dataclass(frozen=True)
class AutonomySettings:
    """``gateway-config.json`` ``autonomy`` (read at start). ``stage_c``: exactly ``true``
    turns the control plane on (default off). ``run_checks_unattended`` (default true):
    a grant may name ``run_checks`` — effective only on a verified ``os_sandbox`` host;
    on ``limits_only`` such a grant is refused whatever the setting says.

    ``stage_c_exec`` (W5, default off): exactly ``true`` **and** ``stage_c`` on builds the
    goal iteration executor (a person's "Run one iteration" press runs one iteration);
    without ``stage_c`` it stays off (``stage_c_exec_needs_stage_c``).
    ``iteration_wall_sec`` (W5): the longest an iteration may run (60–3600 s, default 600);
    anything else is the default (with a warning code).
    ``claim_stale_sec`` (W6c): after how long an unverified completion claim is shown as
    "still waiting for you" (60–86400 s, default 1800); display only — nothing acts on it;
    anything else is the default (with a warning code).
    ``heartbeat_start`` (W6d, default off): exactly ``true`` **and** ``stage_c_exec`` on
    lets a grant issued with its own ``heartbeat_start`` flag be started by the
    scheduler's model-free ``grant_tick`` (at most one iteration per wake); otherwise off
    (``heartbeat_start_needs_stage_c_exec``). ``tick_min_spacing_sec`` (W6d): the shortest
    time from an iteration's end to a heartbeat-started one (30–86400 s, default 300);
    anything else is the default (with a warning code)."""

    stage_c: bool = False
    run_checks_unattended: bool = True
    # The browser confirmation channel for issue / resume / verify (default false): the
    # weaker, user-chosen layer — a same-user process can drive it.
    allow_browser_confirm: bool = False
    stage_c_exec: bool = False
    iteration_wall_sec: int = ITERATION_WALL_DEFAULT_SEC
    warnings: tuple[str, ...] = ()
    claim_stale_sec: int = CLAIM_STALE_DEFAULT_SEC
    heartbeat_start: bool = False
    tick_min_spacing_sec: int = TICK_SPACING_DEFAULT_SEC


def settings_from(section: Any) -> AutonomySettings:
    """Lenient, fail closed: anything but ``"stage_c": true`` is off; anything but a
    boolean for ``run_checks_unattended`` is false (with a warning code); anything but
    ``"stage_c_exec": true`` together with stage C on is off."""
    if not isinstance(section, dict):
        return AutonomySettings(warnings=() if section in (None, {}) else ("autonomy_invalid",))
    warnings = []
    stage_c = section.get("stage_c", False)
    if type(stage_c) is not bool:
        warnings.append("stage_c_invalid")
    unattended = section.get("run_checks_unattended", True)
    if type(unattended) is not bool:
        warnings.append("run_checks_unattended_invalid")
        unattended = False
    browser = section.get("allow_browser_confirm", False)
    if type(browser) is not bool:
        warnings.append("allow_browser_confirm_invalid")
    executor = section.get("stage_c_exec", False)
    if type(executor) is not bool:
        warnings.append("stage_c_exec_invalid")
    elif executor is True and stage_c is not True:
        warnings.append("stage_c_exec_needs_stage_c")
    wall = section.get("iteration_wall_sec", ITERATION_WALL_DEFAULT_SEC)
    if type(wall) is not int or not ITERATION_WALL_MIN_SEC <= wall <= ITERATION_WALL_MAX_SEC:
        warnings.append("iteration_wall_sec_invalid")
        wall = ITERATION_WALL_DEFAULT_SEC
    stale = section.get("claim_stale_sec", CLAIM_STALE_DEFAULT_SEC)
    if type(stale) is not int or not CLAIM_STALE_MIN_SEC <= stale <= CLAIM_STALE_MAX_SEC:
        warnings.append("claim_stale_sec_invalid")
        stale = CLAIM_STALE_DEFAULT_SEC
    exec_on = executor is True and stage_c is True
    tick = section.get("heartbeat_start", False)
    if type(tick) is not bool:
        warnings.append("heartbeat_start_invalid")
    elif tick is True and not exec_on:
        warnings.append("heartbeat_start_needs_stage_c_exec")
    spacing = section.get("tick_min_spacing_sec", TICK_SPACING_DEFAULT_SEC)
    if type(spacing) is not int or not TICK_SPACING_MIN_SEC <= spacing <= TICK_SPACING_MAX_SEC:
        warnings.append("tick_min_spacing_sec_invalid")
        spacing = TICK_SPACING_DEFAULT_SEC
    if set(section) - _AUTONOMY_KEYS:
        warnings.append("autonomy_unknown_key")
    return AutonomySettings(
        stage_c is True,
        unattended,
        browser is True,
        exec_on,
        wall,
        tuple(warnings),
        stale,
        tick is True and exec_on,
        spacing,
    )


class GrantConfirmation(Exception):
    """The request needs its host-confirmed operation: 428 with the operation view (and,
    for the browser channel, in this answer only, its one-time confirmation token)."""

    def __init__(self, view: dict) -> None:
        super().__init__("grant_confirmation_required")
        self.view = view


_NEWEST = object()  # board_view(): the newest record


def _now_or_none(clock: Callable[[], Any]):
    try:
        return clock()
    except Exception:  # noqa: BLE001 - an unreadable clock is an anomaly, judged by state
        return None


def _token_digest(token: str) -> str:
    return hashlib.sha256(("grant-goal-view:" + token).encode("ascii")).hexdigest()


class GrantService:
    """The grants of one process (see the module docstring)."""

    def __init__(
        self,
        *,
        data_dir: Path | None,
        authority,
        recorder=None,
        settings: AutonomySettings | None = None,
        clock: Callable[[], Any] = default_clock,
        breaker: Callable[[], Any] | None = None,
        containment_verified: Callable[[], bool] | None = None,
        write_roots: Callable[[], Iterable[str | Path]] | None = None,
        write_policy: Callable[[], Any] | None = None,
        operations: HostOperations | None = None,
        tool_call_running: Callable[[], bool] | None = None,
    ) -> None:
        self._authority = authority
        self._recorder = recorder
        self.settings = settings or AutonomySettings(stage_c=True)
        self._clock = clock
        self._breaker = breaker or (lambda: "normal")
        self._contained = containment_verified or (lambda: False)
        self._roots = write_roots or (lambda: ())
        self._policy = write_policy or (lambda: None)
        # No tracker: every operation step is refused (host_channel_unavailable).
        self._tool_call_running = tool_call_running
        self._store = GrantStore(data_dir)
        self._lock = threading.Lock()  # leaf: memory only
        self._grants: OrderedDict[str, Grant] = OrderedDict()
        self._goals: dict[str, str] = {}
        # #644 U3 (W6-F): the clock reading of each grant's last resume (memory only; a grant
        # never survives a restart): the heartbeat start keeps its spacing from it too.
        self._resumed: dict[str, Any] = {}
        self._view_tokens: dict[str, str] = {}
        # W6c: per grant, its latest claim (``claimed_at``) and that claim's event id
        # (``goal_claimed``), memory only — the person's verification of **that** claim
        # acknowledges its stop notice.
        self._claim_events: dict[str, tuple[float, str]] = {}
        self._pending: tuple[Grant, str] | None = None  # an issuance being written
        self._generation = 0
        self._unhealthy: str | None = None
        self._results: OrderedDict[str, tuple[tuple[str, str], Any]] = OrderedDict()
        # The bounded single-flight writer (W2 kill-persistence pattern): ``_want`` is
        # bumped by every change, ``_written`` is what a successful write covered.
        self._cond = threading.Condition()
        self._want = 0
        self._written = 0
        self._verify_wanted = False
        self._writer: threading.Thread | None = None
        self._ops = operations or HostOperations(
            incarnation=authority.incarnation, clock=clock, channel="browser"
        )
        self.instance = instance_ref(authority.incarnation)
        self._started = False

    @property
    def channel(self) -> str:
        return self._ops.channel

    @property
    def confirm_layer(self) -> str:
        """``host`` (dialog / command), ``browser_weak`` (the user chose it) or
        ``unavailable`` (issue / resume / verify are refused)."""
        if self.channel != "browser":
            return "host"
        return "browser_weak" if self.settings.allow_browser_confirm else "unavailable"

    # -- start / stop ----------------------------------------------------------------------

    def start(self) -> None:
        """Load the store once. Every grant that was live when it was written comes back
        ``revoked(restart)`` (it was stored that way) and the transition is recorded.
        Registers the kill listener of the authority service."""
        if self._started:
            raise RuntimeError("grants_started")
        self._started = True
        listeners = getattr(self._authority, "kill_listeners", None)
        if isinstance(listeners, list):
            listeners.append(self.on_kill)
        loaded = self._store.load()
        if loaded.reason is not None:
            self._unhealthy = loaded.reason
            self._post_store(loaded.reason)
            logger.warning(
                "grants: the grant store is unusable (%s); nothing is issued", loaded.reason
            )
            return
        restarted = []
        with self._lock:
            for grant, goal, restart_from in loaded.entries:
                self._grants[grant.grant_id] = grant
                self._goals[grant.grant_id] = goal
                if restart_from:
                    restarted.append((grant, restart_from))
        epoch = self._authority.epoch
        for grant, restart_from in restarted:
            self._post(grant.grant_id, "grant", restart_from, "revoked", "revoked_restart", epoch)
            self._post(grant.grant_id, "goal", "unknown", "unmet", "goal_unmet", epoch)
        if restarted:
            self._wait_written(self._request_write(), WRITE_WAIT_SEC)

    def stop(self, timeout: float | None = None) -> str:
        """The shutdown step: write what is not on disk yet, waiting at most ``timeout``
        (a write already running on a hung disk is left behind: ``pending``).
        ``stopped`` only when everything is written **and** the store is healthy
        (``unhealthy``: drained, not durable)."""
        wait = WRITE_WAIT_SEC if timeout is None else max(0.0, timeout)
        with self._cond:
            behind = self._written < self._want
        if behind and self._store.unhealthy_reason is None:
            self._request_write()
        self._wait_idle(wait)
        if self._store.unhealthy_reason is not None or self._unhealthy is not None:
            return "unhealthy"
        with self._cond:
            return "pending" if self._written < self._want or self._writer else "stopped"

    # -- health ----------------------------------------------------------------------------

    @property
    def unhealthy_reason(self) -> str | None:
        return self._unhealthy or self._store.unhealthy_reason

    def _post_store(self, reason: str) -> None:
        self._post("store", "grants", "healthy", "unhealthy", reason, self._authority.epoch)

    def _fail(self, reason: str) -> None:
        """The stage is unhealthy (sticky): every live grant is revoked (``unhealthy``)."""
        first = False
        with self._lock:
            if self._unhealthy is None:
                self._unhealthy = reason
                first = True
        if first:
            self._post_store(reason)
            logger.warning("grants: unhealthy (%s); every grant is revoked", reason)
        wall = _wall(self._clock)
        self._apply(lambda g: state.revoke(g, "unhealthy", wall=wall))

    # -- events, epoch, persistence --------------------------------------------------------

    def _post(
        self,
        ident: str,
        kind: str,
        before: str,
        after: str,
        code: str,
        epoch: int,
        claim: float | None = None,
    ) -> None:
        recorder = self._recorder
        if recorder is None:
            return
        try:
            event = make_event(
                kind,
                ident,
                before,
                after,
                code,
                incarnation=self._authority.incarnation,
                epoch=epoch,
                ts_wall=time.time(),
            )
            recorder.post(event)
            if code == "goal_claimed" and claim is not None:
                with self._lock:
                    self._claim_events[ident] = (claim, event["event_id"])
        except Exception:  # noqa: BLE001 - a refused event latches the authority unhealthy
            self._authority.report_unhealthy("lifecycle_event_invalid")

    def _claim_event_locked(self, grant: Grant) -> str | None:
        """W6c, under the leaf lock: the event id of ``grant``'s own claim — only when the
        recorded claim is that record's claim (``claimed_at`` equal), never "the latest"."""
        entry = self._claim_events.get(grant.grant_id)
        evidence = grant.claim_evidence
        if entry is None or evidence is None or entry[0] != evidence.claimed_at:
            return None
        return entry[1]

    def _ack_claim(self, grant_id: str, event_id: str | None) -> None:
        """W6c: the person verified (confirmed, rejected, or recorded late) the claim whose
        event id ``event_id`` was read in the verification's own locked step: its stop notice
        is acknowledged by that id — which the outbox accepts only while it is that notice's
        newest, so a newer claim's occurrence (even one posted since) is never acknowledged
        by it (``outbox_stale``). Outside every lock of this service; never raises."""
        with self._lock:
            if event_id is not None and self._claim_events.get(grant_id, (0.0, ""))[1] == event_id:
                del self._claim_events[grant_id]
        recorder = self._recorder
        if event_id is None or recorder is None:
            return
        try:
            drain, ack = getattr(recorder, "drain", None), getattr(recorder, "ack", None)
            if callable(drain):
                drain()  # the claim's event reaches the outbox first
            if callable(ack):
                ack(event_id)
        except Exception:  # noqa: BLE001 - the notice stays unread; the person can ack it
            logger.warning("grants: a claim's notice could not be acknowledged")

    def _apply(
        self,
        step: Callable[[Grant], Grant],
        ids: Iterable[str] | None = None,
        *,
        deferred: bool = False,
        epoch: int | None = None,
        later: list | None = None,
        clocked: bool = False,
    ) -> tuple[list, int | None]:
        """Run ``step`` over the live grants (or ``ids``) under the leaf lock, then commit
        the changes outside it. Returns ``(changes, write target)``; the target is
        ``None`` when nothing needs writing (a clock reading alone stays in memory).
        ``later``: see :meth:`_commit`. ``clocked``: ``step(grant, now)`` takes the clock
        reading made here, under the lock (#627: read and applied as one step)."""
        with self._lock:
            if clocked:
                now = _now_or_none(self._clock)  # under the leaf lock (#627)
                step = partial(step, now=now)
            changes = []
            for grant_id in list(ids) if ids is not None else list(self._grants):
                old = self._grants.get(grant_id)
                if old is None or (ids is None and old.status in state.TERMINAL):
                    continue
                new = step(old)
                if new is not old and new != old:
                    self._grants[grant_id] = new
                    changes.append((old, new))
        return changes, self._commit(changes, deferred=deferred, epoch=epoch, later=later)

    def _commit(self, changes, *, deferred: bool = False, epoch: int | None = None, later=None):
        """Outside the leaf lock: one epoch advance for the stops among ``changes``
        (deferred under a foreign lock; none when ``epoch`` is given — the kill's), the
        stop epoch on those grants, the lifecycle events, then a store write request
        (never waited for here). With ``later`` (a list; the kill listener, #620 W5-3)
        the events and the write request are appended to it as one callable instead of
        run now: memory only here."""
        state_changes = [(o, n) for o, n in changes if o.generation != n.generation]
        if not state_changes:
            return None
        stops = [
            new
            for old, new in state_changes
            if (old.status, old.reason) != (new.status, new.reason) and new.status != "active"
        ]
        if epoch is None:
            epoch = self._authority.epoch
            if stops:
                try:
                    epoch = (
                        self._authority.advance_epoch()
                        if deferred
                        else self._authority.narrow("grant_stop")
                    )
                except Exception:  # noqa: BLE001 - the stop is in memory already
                    self._authority.report_unhealthy("epoch_settle_failed")
                    epoch = self._authority.epoch
        if stops:
            with self._lock:
                for new in stops:
                    current = self._grants.get(new.grant_id)
                    if current is not None and current.generation == new.generation:
                        self._grants[new.grant_id] = replace(current, stop_epoch=epoch)

        def finish() -> int:
            for old, new in state_changes:
                for kind, before, after, code in state.transitions(old, new):
                    claim = None
                    if code == "goal_claimed" and new.claim_evidence is not None:
                        claim = new.claim_evidence.claimed_at
                    self._post(new.grant_id, kind, before, after, code, epoch, claim)
            return self._request_write()

        if later is not None:
            later.append(finish)
            return None
        return finish()

    def _entries_locked(self) -> list[dict]:
        """The stored form; a live grant is written as what the next start makes of it."""
        self._trim_locked()
        items = list(self._grants.items())
        if self._pending is not None:
            items.append((self._pending[0].grant_id, self._pending[0]))
        out = []
        for grant_id, grant in items:
            goal = self._goals.get(grant_id)
            if goal is None and self._pending is not None and self._pending[0] is grant:
                goal = self._pending[1]
            if grant.status in state.TERMINAL:
                out.append({"grant": grant_to_dict(grant, goal), "restart_from": ""})
            else:
                stored = state.restart(grant, wall=grant.last_wall)
                out.append({"grant": grant_to_dict(stored, goal), "restart_from": grant.status})
        return out

    def _trim_locked(self) -> None:
        """Retention: at most ``MAX_GRANTS``; the oldest ended ones (and their goal) go."""
        limit = MAX_GRANTS - (1 if self._pending is not None else 0)
        while len(self._grants) > limit:
            victim = next((k for k, g in self._grants.items() if g.status in state.TERMINAL), None)
            if victim is None:
                return
            del self._grants[victim]
            self._goals.pop(victim, None)
            self._view_tokens.pop(victim, None)

    def _request_write(self, *, verify: bool = False) -> int:
        """Ask the single writer for a write (and a re-read check); never waits. A writer
        that cannot start never writes inline: the stage is unhealthy
        (``grant_persist_unavailable``) and the next look revokes."""
        with self._cond:
            if verify:
                self._verify_wanted = True  # a re-read only: nothing new to write
            else:
                self._want += 1
            target = self._want
            if self._writer is not None or self._store.unhealthy_reason is not None:
                return target
            worker = threading.Thread(target=self._write_loop, name="grants-persist", daemon=True)
            self._writer = worker
            try:
                worker.start()
            except Exception:  # noqa: BLE001 - e.g. "can't start new thread"
                self._writer = None
                self._cond.notify_all()
                failed = True
            else:
                failed = False
        if failed:
            self._unhealthy = self._unhealthy or "grant_persist_unavailable"
            logger.warning("grants: the store writer could not start; unhealthy")
        return target

    def _write_loop(self) -> None:
        try:
            self._write_loop_body()
        except Exception:
            # Anything unexpected: ownership is released and the stage latches unhealthy
            # (diagnostics say what happened; the next request may start a new writer).
            with self._cond:
                self._writer = None
                self._cond.notify_all()
            with self._lock:
                self._unhealthy = self._unhealthy or "grant_persist_failed"
            logger.exception("grants: the store writer failed unexpectedly; unhealthy")
            # #620 R1: every live grant is revoked now, not at the next look (the latch
            # above is set first: ``grant_persist_failed`` is no lifecycle reason code, so
            # ``_fail`` posts no store event for it — the revocations are posted).
            try:
                self._fail("grant_persist_failed")
            except Exception:  # noqa: BLE001 - latched above; the next look revokes
                logger.warning("grants: the failed writer could not revoke; latched unhealthy")

    def _write_loop_body(self) -> None:
        while True:
            with self._cond:
                done = self._written >= self._want and not self._verify_wanted
                if done or self._store.unhealthy_reason is not None:
                    self._writer = None
                    self._cond.notify_all()
                    return
                target, verify = self._want, self._verify_wanted
                self._verify_wanted = False
            problem = self._store.verify() if verify else None
            ok = problem is None
            if ok and self._written < target:
                with self._lock:
                    entries = self._entries_locked()
                ok = self._store.write(entries)
            with self._cond:
                if ok:
                    self._written = max(self._written, target)
                self._cond.notify_all()
            if not ok:
                self._fail(self._store.unhealthy_reason or "grant_store_write_failed")

    def _wait_written(self, target: int | None, timeout: float) -> bool:
        """Wait (bounded) until a write covering ``target`` succeeded."""
        if target is None:
            return True
        with self._cond:
            self._cond.wait_for(
                lambda: self._written >= target or self._writer is None,
                timeout=max(0.0, timeout),
            )
            return self._written >= target

    def _wait_idle(self, timeout: float) -> bool:
        """Wait (bounded) until the writer has nothing left (written or failed)."""
        with self._cond:
            return self._cond.wait_for(lambda: self._writer is None, timeout=max(0.0, timeout))

    # -- the look (watchdog pass, requests) and the hooks ----------------------------------

    def _look(self, ids: Iterable[str] | None = None):
        """Memory only (no store I/O): kill / unhealthy authority, clocks, TTL, breaker."""
        return self._apply(self._look_step(), ids, clocked=True)

    def _look_step(self) -> Callable[[Grant, Any], Grant]:
        """Read the breaker and the flags now (outside every lock) and return the pure
        step that applies them to one grant together with a clock reading ``now``. The
        caller reads ``now`` under the leaf lock, in the same hold that applies it (#627):
        a reading taken outside it could reach the grant after a later one and look like
        a monotonic clock going back."""
        try:
            level = self._breaker()
        except Exception:  # noqa: BLE001 - an unreadable breaker is not NORMAL
            level = "unknown"
        killed = self._authority.killed
        sick = self._authority.unhealthy_reason is not None or self.unhealthy_reason is not None

        def step(grant: Grant, now: Any) -> Grant:
            wall = now.wall if now is not None else _wall(None)
            if killed:
                return state.revoke(grant, "kill_switch", wall=wall)
            if sick:
                return state.revoke(grant, "unhealthy", wall=wall)
            grant = state.clock_check(grant, now)
            reason = state.halt_reason_for(level)
            return grant if reason is None else state.halt(grant, reason)

        return step

    def sync(self) -> None:
        """One model-free look (the watchdog's pass and the list view): the in-memory checks
        now, then a re-read of the store on the writer (never waited for here, so a hung
        disk never holds the pass). Never raises."""
        try:
            self._look()
            self._request_write(verify=True)
        except Exception:  # noqa: BLE001
            logger.warning("grants: a look failed; unhealthy")
            with self._lock:
                self._unhealthy = self._unhealthy or "grant_sync_failed"
            try:
                self._fail("grant_sync_failed")
            except Exception:  # noqa: BLE001 - already latched above; the look never raises
                logger.warning("grants: the failed look could not revoke; latched unhealthy")

    def check_now(self, grant_id: Any) -> Grant:
        """The look for one grant, then its record (what W5 must use before any effect)."""
        self._get(grant_id)
        self._look([grant_id])
        return self._get(grant_id)

    def on_breaker_transition(self, before: Any, after: Any) -> None:
        """The engine's hook (under its ``state_lock``): a level away from NORMAL halts
        every live grant now, in memory; the epoch advance is deferred, the write follows
        on the writer. Never raises."""
        try:
            reason = state.halt_reason_for(after)
            if reason is None:
                return
            self._apply(lambda g: state.halt(g, reason), deferred=True)
        except Exception:  # noqa: BLE001 - never raise into the engine
            with self._lock:
                self._unhealthy = self._unhealthy or "grant_sync_failed"

    def on_kill(self, epoch: int):
        """The authority's kill listener (after the kill flag and its epoch, before any of
        its I/O): every live grant ``revoked(kill_switch)`` in memory at that epoch —
        nothing else here (#620 W5-3). The lifecycle events and the store write request
        are returned as one callable the kill switch runs after its persistence attempt
        and its cancel kickoff (a blocking recorder delays neither)."""
        later: list = []
        try:
            wall = _wall(self._clock)
            self._apply(
                lambda g: state.revoke(g, "kill_switch", wall=wall),
                deferred=True,
                epoch=epoch,
                later=later,
            )
        except Exception:  # noqa: BLE001 - never raise into the kill switch
            with self._lock:
                self._unhealthy = self._unhealthy or "grant_sync_failed"

        def follow_up() -> None:
            for finish in later:
                finish()

        return follow_up

    def session_reset(self) -> None:
        """``/new`` (under the chat turn lock): the epoch advances (always — also with no
        live grant, so an issuance in flight is fenced) and every live grant is
        ``revoked(session_reset)`` in memory now. Never raises."""
        try:
            epoch = self._authority.advance_epoch()
            wall = _wall(self._clock)
            self._apply(
                lambda g: state.revoke(g, "session_reset", wall=wall), deferred=True, epoch=epoch
            )
        except Exception:  # noqa: BLE001
            with self._lock:
                self._unhealthy = self._unhealthy or "grant_sync_failed"

    # -- the operation gate ---------------------------------------------------------------

    def _host_channel(self) -> None:
        """Stage C loosenings need a host confirmation (the YOLO desk's ``dialog`` or
        ``command`` channel, operator decision 2026-10-25). The browser channel — every
        proof is handed to the requesting process, so a same-user process can drive it —
        only when the user chose it explicitly (``autonomy.allow_browser_confirm``)."""
        if self.channel != "browser":
            return
        if not self.settings.allow_browser_confirm:
            raise GrantRefused(
                "grant_operation_refused", host_code="host_channel_required", hint=CHANNEL_HINT
            )
        logger.warning("grants: browser confirmation (the weaker layer the user chose)")

    def require_no_tool_call(self) -> None:
        """W5 ``/step``: refused while an agent tool call runs (the issue / resume / verify
        guard): no host operation is needed, but an agent's own HTTP tool can never press
        it."""
        self._no_tool_call()

    def _no_tool_call(self) -> None:
        running = self._tool_call_running
        if running is None:
            raise GrantRefused("grant_operation_refused", host_code="host_channel_unavailable")
        try:
            busy = bool(running())
        except Exception:  # noqa: BLE001 - an unreadable tracker counts as busy
            busy = True
        if busy:
            raise GrantRefused("grant_operation_refused", host_code="tool_call_running")

    def _gate(self, kind: str, route: str, body: Any, target: dict, summary: dict, op_id: Any):
        """Returns → apply now (a confirmed, matching, fresh operation, now consumed);
        raises :class:`GrantConfirmation` (no operation named) or :class:`GrantRefused`.
        (An operation already applied is answered by :meth:`_replay` before this.)"""
        self._host_channel()
        self._no_tool_call()
        try:
            binding = binding_digest(kind, route, body)
        except Unbindable as error:
            raise GrantRefused("grant_body_invalid") from error
        generation = target_digest(target)
        if op_id is None or op_id == "":
            try:
                view = self._ops.request(
                    kind, binding=binding, generation=generation, summary=summary
                )
            except HostOpRefused as error:
                raise GrantRefused("grant_operation_refused", host_code=error.code) from error
            raise GrantConfirmation(view)
        try:
            self._ops.consume(op_id, kind=kind, binding=binding, generation=generation)
        except HostOpRefused as error:
            raise GrantRefused("grant_operation_refused", host_code=error.code) from error

    def _remember(self, op_id: str, key: tuple[str, str], result: Any) -> None:
        with self._lock:
            self._results[op_id] = (key, result)
            while len(self._results) > _RESULTS_KEPT:
                self._results.popitem(last=False)

    def _replay(self, kind: str, route: str, body: Any, op_id: Any) -> dict | None:
        """An operation already applied: the same body → its original result (or its
        original refusal), before any other check — nothing is applied twice; another
        body → refused. (The goal view token is never in a replayed answer.) A replayed
        answer is marked ``historical: true``: it describes the grant as it was then."""
        if type(op_id) is not str or not op_id:
            return None
        with self._lock:
            cached = self._results.get(op_id)
        if cached is None:
            return None
        try:
            key = (kind, binding_digest(kind, route, body))
        except Unbindable as error:
            raise GrantRefused("grant_body_invalid") from error
        if cached[0] != key:
            raise GrantRefused("grant_operation_refused", host_code="host_operation_mismatch")
        outcome = cached[1]
        if isinstance(outcome, GrantRefused):
            raise GrantRefused(outcome.code, **outcome.extra)
        # #620 R2: the original answer, as it was then — not the grant's state now.
        return {**outcome, "replayed": True, "historical": True}

    def _run_gated(self, kind, route, body, target, summary, op_id, apply: Callable[[], dict]):
        self._gate(kind, route, body, target, summary, op_id)
        key = (kind, binding_digest(kind, route, body))
        try:
            result = apply()
        except GrantRefused as error:
            self._remember(op_id, key, error)
            raise
        self._remember(op_id, key, {k: v for k, v in result.items() if k != "goal_view_token"})
        return result

    def confirm_operation(
        self, op_id: Any, *, token: Any, acknowledged: Any, instance: Any
    ) -> dict:
        """The browser channel's confirmation; refused (``host_confirm_on_host``) when the
        effective channel is the host command or dialog, and while a tool call runs."""
        self._host_channel()
        self._no_tool_call()
        try:
            return self._ops.confirm_in_browser(
                op_id, token=token, acknowledged=acknowledged, instance=instance
            )
        except HostOpRefused as error:
            raise GrantRefused("grant_operation_refused", host_code=error.code) from error

    def operation_status(self, op_id: Any) -> dict:
        try:
            return self._ops.status(op_id)
        except HostOpRefused as error:
            raise GrantRefused("grant_operation_refused", host_code=error.code) from error

    def cancel_operation(self, op_id: Any) -> dict:
        try:
            return self._ops.cancel(op_id)
        except HostOpRefused as error:
            raise GrantRefused("grant_operation_refused", host_code=error.code) from error

    # -- human operations -----------------------------------------------------------------

    def _live_locked(self) -> list[Grant]:
        return [g for g in self._grants.values() if g.status not in state.TERMINAL]

    def _loosening_preconditions(self) -> None:
        """Issue, resume, verify: refused while killed or unhealthy (authority or store)."""
        if self._authority.killed:
            raise GrantRefused("grant_authority_killed")
        if self._authority.unhealthy_reason is not None:
            raise GrantRefused("grant_authority_unhealthy")
        if self.unhealthy_reason is not None:
            raise GrantRefused("grant_store_unhealthy")

    def _breaker_level(self) -> Any:
        try:
            return self._breaker()
        except Exception:  # noqa: BLE001
            return "unknown"

    def issue(self, body: Any, *, op_id: Any = None, route: str = "/api/grants/issue") -> dict:
        """Issue a grant (see the module docstring). Raises :class:`GrantConfirmation`
        until the operation is confirmed, :class:`GrantRefused` with a fixed code."""
        self._look()
        epoch0 = self._authority.epoch  # the fence: captured before every check below
        replayed = self._replay("grant_issue", route, body, op_id)
        if replayed is not None:
            return replayed
        request = parse_issue(body, roots=self._roots(), policy=self._policy())
        if request.heartbeat_start and not self.settings.heartbeat_start:
            # W6d: the per-grant flag needs the global switch (and so stage_c_exec).
            raise GrantRefused("grant_heartbeat_start_off")
        if "run_checks" in request.tools:
            # The safety floor the setting cannot lift: only a verified os_sandbox.
            if not self.settings.run_checks_unattended:
                raise GrantRefused("grant_run_checks_setting_off")
            if not self._contained():
                raise GrantRefused("grant_run_checks_not_contained")
        self._loosening_preconditions()
        if state.halt_reason_for(self._breaker_level()) is not None:
            raise GrantRefused("grant_breaker_not_normal")
        with self._lock:
            if self._live_locked() or self._pending is not None:
                raise GrantRefused("grant_already_active")
            generation = self._generation
        target = {
            "subject": "grants",
            "incarnation": self._authority.incarnation,
            "generation": generation,
        }
        summary = {
            "action": "grant_issue",
            "tool": list(request.tools),
            "count": request.ttl_sec // 60,
        }
        if request.heartbeat_start:
            # W6d: the host-confirmed view names the heartbeat start (the body, and so the
            # flag, is bound to the operation's digest as well).
            summary["source"] = "heartbeat_start"

        def apply() -> dict:
            return self._issue_now(request, generation, epoch0)

        return self._run_gated("grant_issue", route, body, target, summary, op_id, apply)

    def _issue_now(self, request, generation: int, epoch0: int) -> dict:
        now = _now_or_none(self._clock)
        if now is None:
            raise GrantRefused("grant_clock_unavailable")
        self._loosening_preconditions()
        grant = state.new_grant(request, grant_id=secrets.token_hex(16), issuer=ISSUER, now=now)
        with self._lock:
            if self._live_locked() or self._pending is not None or self._generation != generation:
                raise GrantRefused("grant_already_active")
            self._pending = (grant, request.goal)
        # 1. Durable first (as revoked(restart): never as active); nothing is usable yet.
        written = self._wait_written(self._request_write(), ISSUE_WRITE_WAIT_SEC)
        if not written:
            with self._lock:
                self._pending = None
            self._request_write()  # the next write drops the unpublished entry
            if self.unhealthy_reason is not None:
                raise GrantRefused("grant_store_unhealthy")
            raise GrantRefused("grant_store_timeout")
        # 2. Publish against the fence: no stop since epoch0 (W1 commit_at_epoch).
        token = secrets.token_hex(16)
        digest = _token_digest(token)

        def publish(_epoch: int) -> bool:
            # Under the authority's leaf lock (and ours): attribute reads and dict stores
            # only — no I/O, no callback, no clock. The grant exists from this instant.
            if self._authority.unhealthy_reason is not None:
                return False
            self._grants[grant.grant_id] = grant
            self._goals[grant.grant_id] = request.goal
            self._generation += 1
            self._view_tokens[grant.grant_id] = digest
            return True

        with self._lock:
            self._pending = None
            published = False
            if (
                self.unhealthy_reason is None
                and not self._live_locked()
                and self._generation == generation
            ):
                # The W1 fence: refused (publish not run) on any epoch move or kill since
                # epoch0. Boundary left: the lifecycle events and the post-publication
                # look below run after it (a stop then reaches the grant at once).
                fenced, _epoch, done = self._authority.commit_at_epoch(publish, expected=epoch0)
                published = bool(fenced and done)
        if not published:
            self._request_write()  # drop the unpublished entry from the store
            raise GrantRefused("grant_issue_aborted_epoch")
        for kind, before, after, code in state.transitions(None, grant):
            self._post(grant.grant_id, kind, before, after, code, self._authority.epoch)
        # 3. Anything that came after the fence reaches the grant now, not at a later look.
        current = self.check_now(grant.grant_id)
        return {"grant": self._view(current), "goal_view_token": token}

    def _get(self, grant_id: Any) -> Grant:
        if type(grant_id) is not str:
            raise GrantRefused("grant_unknown")
        with self._lock:
            grant = self._grants.get(grant_id)
        if grant is None:
            raise GrantRefused("grant_unknown")
        return grant

    def revoke(self, grant_id: Any) -> dict:
        """One press, always available (also when the store or the authority is unhealthy,
        and on a hung disk): the revocation and its epoch advance happen in memory before
        any store I/O; the write is waited for at most ``WRITE_WAIT_SEC`` (a live grant is
        stored as revoked anyway)."""
        self._get(grant_id)
        wall = _wall(self._clock)
        changes, target = self._apply(lambda g: state.revoke(g, "user", wall=wall), [grant_id])
        written = self._wait_written(target, WRITE_WAIT_SEC)
        return {
            "changed": bool(changes),
            "persisted": written,
            "grant": self._view(self._get(grant_id)),
        }

    def pause(self, grant_id: Any) -> dict:
        grant = self._get(grant_id)
        if grant.status in state.TERMINAL:
            raise GrantRefused("grant_terminal")
        changes, target = self._apply(state.pause_user, [grant_id])
        written = self._wait_written(target, WRITE_WAIT_SEC)
        return {
            "changed": bool(changes),
            "persisted": written,
            "grant": self._view(self._get(grant_id)),
        }

    def resume(self, grant_id: Any, body: Any, *, op_id: Any = None) -> dict:
        grant = self.check_now(grant_id)
        replayed = self._replay("grant_resume", f"/api/grants/{grant.grant_id}/resume", body, op_id)
        if replayed is not None:
            return replayed
        if type(body) is not dict:
            raise GrantRefused("grant_body_invalid")
        halted = grant.status == "halted"
        allowed = {"confirm"} if halted else set()
        if set(body) - allowed:
            raise GrantRefused("grant_field_unknown")
        if halted and body.get("confirm") != RESUME_WORD:
            raise GrantRefused("grant_confirm_mismatch")
        self._loosening_preconditions()
        level = self._breaker_level()
        state.resume(grant, breaker_level=level)  # refused now, before any operation
        target = {
            "subject": "grant:" + grant.grant_id,
            "incarnation": self._authority.incarnation,
            "generation": grant.generation,
            "status": grant.status,
            "reason": grant.reason,
            "stop_epoch": grant.stop_epoch,
        }
        summary = {"action": "grant_resume", "operation": "grant#" + grant.grant_id[:8]}
        route = f"/api/grants/{grant.grant_id}/resume"

        def apply() -> dict:
            self._loosening_preconditions()
            now_level = self._breaker_level()

            def step(current: Grant, now: Any) -> Grant:
                if current.generation != grant.generation:
                    raise GrantRefused("grant_operation_refused", host_code="stale_operation")
                resumed = state.resume(current, breaker_level=now_level)
                # U3: read and recorded under the leaf lock, with the resume itself (None:
                # the clock was unreadable — the heartbeat start then skips, fail closed).
                for gone in [g for g in self._resumed if g not in self._grants]:
                    del self._resumed[gone]
                self._resumed[current.grant_id] = now
                return resumed

            self._apply(step, [grant.grant_id], clocked=True)
            return {"changed": True, "grant": self._view(self.check_now(grant.grant_id))}

        return self._run_gated("grant_resume", route, body, target, summary, op_id, apply)

    def verify(self, grant_id: Any, body: Any, *, op_id: Any = None) -> dict:
        """The user's verification — a host-confirmed operation (a forged confirm or reject
        could fake a "verified by the user" or reopen the goal)."""
        grant = self.check_now(grant_id)
        route = f"/api/grants/{grant.grant_id}/verify"
        replayed = self._replay("grant_verify", route, body, op_id)
        if replayed is not None:
            return replayed
        if type(body) is not dict or set(body) != {"decision"}:
            raise GrantRefused("grant_body_invalid")
        decision = body["decision"]
        if type(decision) is not str or decision not in ("confirm", "reject"):
            raise GrantRefused("grant_field_invalid")
        late = grant.status in state.TERMINAL
        if not late:
            self._loosening_preconditions()
            if grant.goal_status != "awaiting_verification":
                raise GrantRefused("grant_not_awaiting")
            if grant.status != "active":
                raise GrantRefused("grant_not_active")
        elif grant.late_verification:
            raise GrantRefused("grant_terminal")  # the first late verification stays
        target = {
            "subject": "grant:" + grant.grant_id,
            "incarnation": self._authority.incarnation,
            "generation": grant.generation,
            "status": grant.status,
            "goal_status": grant.goal_status,
        }
        summary = {"action": "grant_verify", "operation": "grant#" + grant.grant_id[:8]}
        wall = _wall(self._clock)

        def apply() -> dict:
            return self._verify_now(grant, decision, wall)

        return self._run_gated("grant_verify", route, body, target, summary, op_id, apply)

    def _verify_now(self, grant: Grant, decision: str, wall: float) -> dict:
        """Apply a consumed verification. Fresh clocks / kill / health / breaker first (the
        look step, read now); a grant that expired or stopped since the check takes that
        stop and the verification is **recorded only** (never ``complete``, never an
        active goal again). A timely one commits under the W1 fence (no epoch move or kill
        since this step began, authority healthy at that instant)."""
        epoch0 = self._authority.epoch
        step = self._look_step()
        grant_id = grant.grant_id
        refusal: str | None = None
        with self._lock:
            current = self._grants.get(grant_id)
            if current is None:
                raise GrantRefused("grant_operation_refused", host_code="stale_operation")
            claim_event = self._claim_event_locked(current)  # W6c: this claim's own id
            if current.generation != grant.generation and current.status not in state.TERMINAL:
                raise GrantRefused("grant_operation_refused", host_code="stale_operation")
            # The clocks under the leaf lock, in this hold (#627).
            looked = step(current, _now_or_none(self._clock))  # ended meanwhile: recorded only
            recorded = looked.status in state.TERMINAL
            if recorded:
                new = state.verify(looked, decision, wall=wall)  # late: recorded only
            elif looked.status != "active":
                new, refusal = looked, "grant_not_active"  # a stop arrived: take only it
            else:
                verified = state.verify(looked, decision, wall=wall)

                late: list = []

                def commit(_epoch: int) -> bool:
                    # Leaf: an attribute read, the in-memory clocks (#620 W5-2: no gap
                    # between the TTL check and the commit) and a dict store only.
                    if self._authority.unhealthy_reason is not None:
                        return False
                    fresh = state.clock_check(looked, _now_or_none(self._clock))
                    if fresh.status != "active":
                        late.append(fresh)  # past the TTL (or a clock anomaly) at commit
                        return False
                    self._grants[grant_id] = verified
                    return True

                fenced, _epoch, done = self._authority.commit_at_epoch(commit, expected=epoch0)
                if fenced and done:
                    new = verified
                elif late:
                    new = late[0]
                    recorded = new.status in state.TERMINAL
                    if recorded:
                        new = state.verify(new, decision, wall=wall)  # recorded only
                    else:
                        refusal = "grant_not_active"
                else:
                    new, refusal = looked, "fenced"
            if self._grants[grant_id] is not new:
                self._grants[grant_id] = new
        self._commit([(current, new)] if new != current else [])
        if refusal == "fenced":
            # A stop since the step began: take it, then record the verification late if
            # the grant ended; otherwise it does not apply (stale).
            self._look([grant_id])
            ended = self._get(grant_id)
            if ended.status not in state.TERMINAL:
                raise GrantRefused("grant_operation_refused", host_code="stale_operation")
            self._apply(lambda g: state.verify(g, decision, wall=wall), [grant_id])
            recorded = True
        elif refusal is not None:
            raise GrantRefused(refusal)
        self._ack_claim(grant_id, claim_event)  # W6c: the person acted on this claim
        return {"recorded_only": recorded, "grant": self._view(self._get(grant_id))}

    def claim_complete(self, grant_id: Any) -> dict:
        """The agent's completion claim — **for the W5 executor only** (no route, not a
        model-visible tool): the look first (kill / clocks / TTL / breaker), then the goal
        moves to ``awaiting_verification``, never further. W6c: the same step replaces the
        grant's evidence record with the one of this claim, from a clock reading taken under
        the leaf lock in that hold (``claimed_at``, TTL left; #627)."""
        self.check_now(grant_id)
        self._apply(lambda g, now: state.claim(g, now=now), [grant_id], clocked=True)
        return self._view(self._get(grant_id))

    def measure_claim(self, grant_id: Any, claimed_at: Any, *, fence: Any, **codes: Any) -> Grant:
        """W6c, the executor only: the claiming iteration's measured codes for the evidence
        of **its** claim (``state.measure_claim``: any other record is unchanged). Published
        only if, **at the publication** — one step under the leaf lock and inside the
        authority's W1 fence (``commit_at_epoch``), which also **installs** the record — the
        grant is still the version the committed count left (``fence.generation``), the
        authority epoch has not moved since the iteration started and there is no kill
        (``fence.epoch``: any narrowing moves it), no person's turn has signalled or is
        arriving since the iteration started (``fence.signal.seq == fence.seq``,
        ``pending`` 0; plain integer reads), and the claim still waits on an active grant.
        The turn mark and the kill flag (both set without a lock) are read **last**, right
        before the install. Otherwise nothing is published (the record keeps its
        ``unknown`` codes; a confirmed claim's audit is what the person decided on). Not a
        state change — no generation, no lifecycle event — but the record is written (the
        evidence lives in the grant store); the write is requested, never waited for."""
        self._get(grant_id)
        try:
            generation, epoch = int(fence.generation), int(fence.epoch)
            signal, seq = fence.signal, int(fence.seq)
        except (AttributeError, TypeError, ValueError):
            return self._get(grant_id)  # no usable fence: nothing is published
        authority = self._authority
        with self._lock:
            current = self._grants.get(grant_id)
            waiting = current is not None and (
                current.generation,
                current.status,
                current.goal_status,
            ) == (generation, "active", "awaiting_verification")
            installed = False
            if waiting:
                measured = state.measure_claim(current, claimed_at, **codes)

                def publish(_epoch: int) -> bool:
                    # Under the authority's leaf lock (and ours): integer and flag reads, a
                    # dict store — no I/O, no callback. The lock-free marks are read last.
                    if signal.seq != seq or signal.pending or authority.killed:
                        return False  # a person's turn, or a kill flag set, since the start
                    self._grants[grant_id] = measured
                    return True

                if measured != current:
                    fenced, _epoch, done = authority.commit_at_epoch(publish, expected=epoch)
                    installed = bool(fenced and done)
        if installed:
            self._request_write()
        return self._get(grant_id)

    def authority_record(self, grant_id: Any):
        """The ``kind="goal"`` record a W5 permit binds (generation, state), after the
        look for this grant (so an expired or killed grant is never ``active`` here)."""
        return self.executor_record(grant_id)[1]

    # -- the W5 executor's entry points (no route, not model-visible) -------------------

    def executor_record(self, grant_id: Any):
        """``(grant, authority record)`` from **one** look (``check_now``: kill, health,
        clocks, TTL, breaker), the pair a W5 permit is issued and consumed against."""
        grant = self.check_now(grant_id)
        return grant, state.authority_record(grant, self._authority.incarnation)

    def executor_goal(self, grant_id: Any) -> str:
        """The goal text, for the executor's ephemeral context block only (in memory; the
        caller never stores, logs or returns it)."""
        grant = self._get(grant_id)
        with self._lock:
            goal = self._goals.get(grant.grant_id)
        if goal is None:
            raise GrantRefused("grant_unknown")
        return goal

    def tick_record(self) -> Grant | None:
        """W6d: the newest grant that has not ended (v1: at most one), for the scheduler's
        model-free ``grant_tick`` — memory only: no look, no I/O, no event (the admission
        inside ``step`` makes the look). ``None``: no live grant."""
        with self._lock:
            live = self._live_locked()
        return live[-1] if live else None

    def resumed_at(self, grant_id: Any) -> tuple[bool, Any]:
        """#644 U3: ``(True, clock reading)`` of the grant's last resume in this process
        (the reading ``None``: unreadable at the resume), ``(False, None)``: never resumed.
        Memory only."""
        with self._lock:
            if grant_id in self._resumed:
                return True, self._resumed[grant_id]
        return False, None

    def live_ids(self) -> frozenset[str]:
        """The ids of the grants that have not ended (W6a: their outcome trackers stay)."""
        with self._lock:
            return frozenset(g.grant_id for g in self._live_locked())

    def clock_reading(self):
        """One reading of the service's clocks (``None``: unreadable)."""
        return _now_or_none(self._clock)

    def breaker_level(self) -> Any:
        return self._breaker_level()

    def satisfy_recheck(self, grant_id: Any) -> Grant:
        """The executor's admission re-checked breaker, budget and TTL after a halt
        resume: ``recheck`` is cleared (``state.satisfy_recheck``)."""
        self._get(grant_id)
        self._apply(state.satisfy_recheck, [grant_id])
        return self._get(grant_id)

    def charge(self, grant_id: Any, tokens: int) -> Grant:
        """Tokens one model call spent (``state.charge``: never resets)."""
        self._get(grant_id)
        self._apply(lambda g: state.charge(g, tokens), [grant_id])
        return self._get(grant_id)

    def count_iteration(self, grant_id: Any, *, progress: bool | None) -> Grant:
        """One finished iteration (``state.count_iteration``; ``progress`` is the W6a
        outcome verdict, ``None`` an interrupted iteration that consumed an effect)."""
        self._get(grant_id)
        self._apply(lambda g: state.count_iteration(g, progress=progress), [grant_id])
        return self._get(grant_id)

    def commit_iteration(
        self,
        grant_id: Any,
        *,
        stamp: int,
        progress: bool,
        block: str | None = None,
        plain: str | None = None,
    ) -> tuple[bool, Grant]:
        """W6-H (#632-2): an iteration's commit as **one step** under the leaf lock. When
        the grant is still the record the executor's validation read (its ``generation``
        equals ``stamp``: the record is replaced, with a new generation, on every change):
        its own block (``block``) and ``count_iteration(progress)``. Otherwise someone
        changed the grant in between (a person's block, a pause, a stop): an interruption —
        the plain block (``plain``; a no-op on a goal that is no longer active) and
        ``count_iteration(progress=None)``. Returns ``(committed, grant)``."""
        self._get(grant_id)
        committed: list[tuple[bool, Grant]] = []

        def step(g: Grant) -> Grant:
            ok = g.generation == stamp
            code = block if ok else plain
            if code is not None:
                g = state.block_goal(g, code)
            g = state.count_iteration(g, progress=progress if ok else None)
            committed.append((ok, g))
            return g

        self._apply(step, [grant_id])
        if not committed:
            raise GrantRefused("grant_unknown")
        return committed[0]

    def block_goal(self, grant_id: Any, reason: str, *, stamp: int | None = None) -> Grant:
        """The goal → ``blocked(reason)`` (a fixed code); the grant pauses with it. With
        ``stamp``: only while the grant is still that ``generation`` (W6-H: a detector's
        block never lands on a goal someone changed after the commit)."""
        self._get(grant_id)

        def step(g: Grant) -> Grant:
            if stamp is not None and g.generation != stamp:
                return g
            return state.block_goal(g, reason)

        self._apply(step, [grant_id])
        return self._get(grant_id)

    # -- views (structured only) -----------------------------------------------------------

    def _view(self, grant: Grant) -> dict:
        now = _now_or_none(self._clock)
        ttl_left = state.ttl_left(grant, now) if now is not None else 0
        view = {
            "grant_id": grant.grant_id,
            "short": grant.grant_id[:8],
            "kind": grant.kind,
            "session_ref": grant.session_ref,
            "state": grant.status,
            "reason": grant.reason,
            "goal_state": grant.goal_status,
            "goal_reason": grant.goal_reason,
            "headline": state.headline(grant),
            "resume_to": {"state": grant.resume_status, "reason": grant.resume_reason},
            "tools": sorted(grant.tools),
            "ttl_sec": grant.ttl_sec,
            "ttl_left_sec": ttl_left,
            "budget_total": grant.total_tokens,
            "budget_per_tick": grant.per_tick_tokens,
            "budget_used": grant.used_tokens,
            "budget_left": max(0, grant.total_tokens - grant.used_tokens),
            "iterations": grant.iterations,
            "max_iterations": grant.max_iterations,
            "no_progress": grant.no_progress,
            "no_progress_ticks": grant.no_progress_ticks,
            "verifier": grant.verifier,
            "generation": grant.generation,
            "stop_epoch": grant.stop_epoch,
            "recheck": grant.recheck,
            "late_verification": grant.late_verification,
            "issued_wall": grant.issued_wall,
            "ended_wall": grant.ended_wall,
            "claim_evidence": self._claim_view(grant, now),
        }
        if grant.heartbeat_start:
            view["heartbeat_start"] = True  # W6d: only a flagged grant carries the key
        return view

    def _claim_view(self, grant: Grant, now: Any) -> dict | None:
        """W6c: the evidence of the last claim (codes, counters, the claim instant) or
        None. While the claim waits (goal ``awaiting_verification``, grant live) also how
        long it has waited (from ``claimed_at``, this view's clock reading) and whether that
        is past ``autonomy.claim_stale_sec`` (``stale``: display only)."""
        evidence = evidence_to_dict(grant.claim_evidence)
        if evidence is None:
            return None
        threshold = self.settings.claim_stale_sec
        evidence["stale_after_sec"] = threshold
        # (a grant that ended never awaits: its goal is complete or unmet)
        if grant.goal_status == "awaiting_verification" and now is not None:
            seconds = max(0, int(now.wall - evidence["claimed_at"]))
            evidence["waiting_sec"] = seconds
            evidence["stale"] = seconds >= threshold
        return evidence

    def list_view(self) -> dict:
        self.sync()
        with self._lock:
            grants = list(self._grants.values())
        return {
            "enabled": True,
            "healthy": self.unhealthy_reason is None,
            "unhealthy_reason": self.unhealthy_reason,
            "authority_killed": self._authority.killed,
            "authority_healthy": self._authority.unhealthy_reason is None,
            "run_checks_unattended": self.settings.run_checks_unattended,
            "max_live": 1,
            "tools": sorted(TOOLS),
            "instance": self.instance,
            "channel": self.channel,
            "confirm_layer": self.confirm_layer,
            "grants": [self._view(g) for g in reversed(grants)],
        }

    def view(self, grant_id: Any) -> dict:
        return self._view(self.check_now(grant_id))

    def board_record(self) -> Grant | None:
        """The newest grant record (immutable), for the Stage board (W5b). Memory only; it
        never waits: a held lock raises :class:`BoardBusy` at once. Every change of a grant
        replaces its record, so the identity of two reads tells whether it moved between
        them (the board's consistency check)."""
        if not self._lock.acquire(blocking=False):
            raise BoardBusy()
        try:
            return next(reversed(self._grants.values()), None)
        finally:
            self._lock.release()

    def board_view(self, record: Any = _NEWEST) -> dict | None:
        """The Stage board's read (W5b): the structured view of ``record`` (default: the
        newest, :meth:`board_record`), from memory only — no look, no store read or
        write, no event, no callback (the watchdog's pass and every control request do the
        looks). ``None``: no grant."""
        newest = self.board_record() if record is _NEWEST else record
        return None if newest is None else self._view(newest)

    def goal_view(self, grant_id: Any, *, confirm: Any, token: Any) -> dict:
        """The goal text, for the issuer only and **once**: the typed word ``SHOW`` and the
        one-time view token the issue answer gave (its digest, in memory, is consumed by
        the first successful view; a restart ends it too). Anything else:
        ``grant_view_refused`` (an unknown id: ``grant_unknown``)."""
        grant = self._get(grant_id)
        if type(confirm) is not str or confirm != SHOW_WORD:
            raise GrantRefused("grant_view_refused")
        if type(token) is not str or len(token) != 32:
            raise GrantRefused("grant_view_refused")
        try:
            presented = _token_digest(token)
        except (TypeError, UnicodeEncodeError):
            raise GrantRefused("grant_view_refused") from None
        with self._lock:
            digest = self._view_tokens.get(grant.grant_id)
            goal = self._goals.get(grant.grant_id)
            match = digest is not None and secrets.compare_digest(presented, digest)
            if match and goal is not None:
                del self._view_tokens[grant.grant_id]  # one time
        if not match or goal is None:
            raise GrantRefused("grant_view_refused")
        return {
            "grant_id": grant.grant_id,
            "goal": goal,
            "workspace_subtree": grant.workspace_subtree,
        }

    def status(self) -> dict:
        """Codes and counters (no goal, no path)."""
        with self._lock:
            live = self._live_locked()
            grants = len(self._grants)
        with self._cond:
            dirty = self._written < self._want
        return {
            "healthy": self.unhealthy_reason is None,
            "unhealthy_reason": self.unhealthy_reason,
            "grants": grants,
            "live": len(live),
            "dirty": dirty,
        }


def _wall(clock) -> float:
    if clock is not None:
        now = _now_or_none(clock)
        if now is not None:
            return float(now.wall)
    return time.time()
