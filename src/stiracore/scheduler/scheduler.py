"""Operator-defined job scheduler (#239, ADR 0008).

The *Gateway layer* (this module) never calls a model: it decides, on an injectable
clock, whether a human-defined job fires, and records every decision in the
hash-chained :class:`SchedulerEventLog`. Model-calling jobs (*Agent layer*) are executed
by an injected executor with token budgets (``executors.AgentJobExecutor``).

Fail-closed rules: an unverifiable log means nothing runs; an unknown breaker level is
CRITICAL; a job that would need a human approval stops; widening changes (create,
update, resume) need a human principal, an explicit confirmation and a NORMAL breaker;
pausing, cancelling and stopping are always allowed.
"""

from __future__ import annotations

import contextlib
import logging
import re
import secrets
import threading
import time
from collections import OrderedDict, deque
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any

from stiracore.core.approval import args_hash
from stiracore.heartbeat.daemon import HeartbeatLockError, HeartbeatOwnerLock
from stiracore.owner_lock import UnsafeLockFileError

from .event_log import SchedulerEventLog, SchedulerLogError
from .jobs import (
    JOB_READ_ONLY_TOOLS,
    MEMORY_JOB_KINDS,
    MODEL_CALLING_KINDS,
    JobDefinition,
    JobError,
    is_model_calling,
    parse_job,
)

__all__ = [
    "RunContext",
    "RunOutcome",
    "Scheduler",
    "SchedulerLockError",
    "SchedulerLockFileUnsafeError",
    "SchedulerRefused",
    "SchedulerState",
]

logger = logging.getLogger(__name__)

LOCK_FILENAME = "scheduler.lock"
LOG_FILENAME = "scheduler-events.jsonl"
GRACE_SEC = 60.0
# Fires older than this are not enumerated one by one after a long outage.
MAX_LOOKBACK_SEC = 7 * 86400.0
SCAN_LIMIT = 1000
RUN_HISTORY = 500
NOTIFICATION_HISTORY = 200
SUMMARY_LIMIT = 500
_PRINCIPAL = re.compile(r"[A-Za-z0-9._:@-]{1,64}")
_NOTICE_CODE = re.compile(r"[a-z][a-z0-9_]{0,39}")
# Codes the scheduler itself raises (#279). The UI words each of them from the catalog and
# shows any other code as "other (code: ...)"; ``tests/test_scheduler_ui_codes.py`` keeps
# these sets, the JS tables and both catalogs in step.
ALERT_CODES = frozenset(
    {"breaker_critical", "heartbeat_stalled", "job_failing", "heartbeat_required"}
)
NOTIFICATION_CODES = ALERT_CODES | {
    "log_recovered",
    "budget_exhausted",
    "approval_required",
    "jobs_paused_at_start",
    "approval_waiting",
}
RUN_REASON_CODES = frozenset(
    {
        "interrupted",
        "shutdown",
        "breaker_critical",
        "operator",
        "approval_required",
        "budget_exceeded",
        "budget",
        "dispatch_failed",
        "action_unavailable",
        "agent_jobs_disabled",
        "invalid_outcome",
        "log_unavailable",
        "job_updated",
        "unavailable",
        "recovering",
        "paused",
        "cancel_backlog",
        "memory_jobs_disabled",
        "model_call_not_allowed",
        "heartbeat",
        # ADR 0024 phase 2 (S3): how a run that waited for an approval ended.
        "approval_rejected",
        "approval_expired",
        "approval_cancelled",
        "approval_unavailable",
        "binding_mismatch",
    }
)
# ADR 0021 Q3: one pool for every Agent-layer job of a UTC day; never unlimited.
AGENT_DAILY_TOKEN_BUDGET = 50_000
# Why a memory job is paused when the scheduler starts (ADR 0018 §2e, commander decision Q4).
RESTART_PAUSE_REASON = "restart: model-calling memory jobs wait for a person to resume them"
# ADR 0021 §9: Agent-layer jobs come up paused after every start; a person resumes them.
AGENT_RESTART_PAUSE_REASON = "restart: agent jobs wait for a person to resume them"
MEMORY_OFF_PAUSE_REASON = "memory_autonomy does not enable this memory job kind on this server"
# Notification variables: an allowlist of names, each with a strict value shape. Free text
# never becomes a variable (the sentence in ``message`` stays as masked audit text).
BUDGET_KINDS = frozenset(
    {
        "max_runs_per_day",
        "max_tokens_per_day",
        "daily_token_budget",
        "max_tokens_per_run",
        "agent_daily_token_budget",
    }
)
_BUDGET_REASON = re.compile(
    r"(max_runs_per_day|max_tokens_per_day|daily_token_budget|agent_daily_token_budget)"
    r" \((\d{1,12})\)"
)
_TOOL_NAME = re.compile(r"[A-Za-z0-9_.-]{1,64}")
# Set while a job runs: a job can never widen the schedule (no self-extension).
_IN_JOB: ContextVar[str | None] = ContextVar("stira_scheduler_in_job", default=None)

NORMAL, WARNING, COHERENCE, CRITICAL = "normal", "warning", "coherence", "critical"


class RunToken:
    """W6d fix round 1: one run of the scheduler (``start`` → ``stop``). ``valid`` turns
    false at the very start of ``stop`` (a plain attribute write, no lock), so a
    ``grant_tick`` wake already in progress — its stamp carries the token — is refused at
    its publication and before its first effect."""

    __slots__ = ("valid",)

    def __init__(self) -> None:
        self.valid = True


class SchedulerLockError(HeartbeatLockError):
    """Another scheduler already owns this state directory."""


class SchedulerLockFileUnsafeError(SchedulerLockError, UnsafeLockFileError):
    """``scheduler.lock`` is not a plain file of ours (#322); nothing was written.

    Also a :class:`SchedulerLockError` (and so a ``HeartbeatLockError``) for callers
    that catch those; catch :class:`UnsafeLockFileError` first to tell them apart.
    """


class SchedulerRefused(RuntimeError):
    """A schedule change was refused. ``code`` maps to an HTTP status in the API."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class SchedulerState(str, Enum):
    STOPPED = "stopped"
    RUNNING = "running"
    STOPPING = "stopping"
    UNAVAILABLE = "unavailable"


@dataclass
class RunOutcome:
    status: str  # completed / failed / cancelled
    reason: str | None = None
    tokens: int = 0
    summary: str | None = None
    # False when the run may have used more than ``tokens`` (it raised, crashed or was
    # cut off before its final usage was known): it is then charged at least its
    # reservation (ADR 0008 §6).
    usage_known: bool = True


@dataclass
class RunContext:
    """What a job sees while it runs. Thread-safe cancellation."""

    job: JobDefinition
    run_id: str
    run_key: str
    scheduled_at: float
    started_at: float
    scheduler: Scheduler
    generation: int = 1
    day_ordinal: int = 1  # n-th start of this job on its charge day
    charge_day: str = ""  # UTC day the run's reservation and usage are booked on
    cancel_event: threading.Event = field(default_factory=threading.Event)
    cancel_reason: str | None = None
    awaiting_approval: dict | None = None
    # ADR 0024 §6: the request id this run waits on (``unattended_approval: wait``), and
    # the tokens of the segments that already ended.
    waiting_request: str | None = None
    segment_tokens: int = 0
    # A parked segment whose usage was not known for sure (#601 review): the run's final
    # settlement is then max(observed, reservation), whatever the last segment reports.
    segment_usage_unknown: bool = False
    # Set when the continuation segment is scheduled: the run never waits again.
    continued: bool = False
    _callbacks: list = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    @property
    def cancelled(self) -> bool:
        return self.cancel_event.is_set()

    def on_cancel(self, callback: Callable[[], None]) -> None:
        with self._lock:
            if not self.cancel_event.is_set():
                self._callbacks.append(callback)
                return
        _safe_call(callback)

    def cancel(self, reason: str) -> None:
        with self._lock:
            if self.cancel_event.is_set():
                return
            self.cancel_reason = reason
            self.cancel_event.set()
            callbacks, self._callbacks = self._callbacks, []
        if callbacks:
            # Never run a callback synchronously: cancel() is called with the scheduler
            # lock held (tick, stop, log failure), and a callback may block. At most one
            # tracked thread per run (the event is set once).
            self.scheduler._spawn_cancel(self.run_id, callbacks)

    def allowed_tools(self) -> frozenset[str]:
        # Re-intersected at run time: a definition that bypassed parse_job still
        # cannot reach a tool outside the read-only set.
        return frozenset(self.job.tools) & JOB_READ_ONLY_TOOLS

    def notify(self, code: str, message: str, severity: str = "warning") -> None:
        """Raise a notification for this run's job (logged, kept in the event log)."""
        self.scheduler._run_notice(self, code, message, severity)

    def require_approval(self, tool_name: str, tool_input: Any) -> None:
        """The run hit a call that needs a human: record it, stop the run (ADR 0008 §8)."""
        try:
            digest = args_hash(tool_input)
        except (TypeError, ValueError):
            digest = None
        self.awaiting_approval = {"tool": _mask_text(tool_name, 64), "args_hash": digest}
        self.scheduler._awaiting_approval(self)
        self.cancel("approval_required")

    def wait_for_approval(self, request_id: str, tool_name: str, digest: str) -> None:
        """The run waits for a person's approval of ``request_id`` (ADR 0024 §6.1)."""
        self.waiting_request = request_id
        self.scheduler._waiting_approval(self, tool_name, digest)


def _safe_call(callback: Callable[[], None]) -> None:
    try:
        callback()
    except Exception as error:  # noqa: BLE001 - a cancel hook must not break cancellation
        logger.info("scheduler: cancel callback failed (%s)", type(error).__name__)


def _day(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%d")


def _note_params(params: Any) -> dict[str, Any]:
    """The allowlisted variables of a notification (``count``, ``tool``, ``kind``, ``limit``)."""
    out: dict[str, Any] = {}
    if not isinstance(params, dict):
        return out
    for key in ("count", "limit"):
        value = params.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 10**12:
            out[key] = value
    kind = params.get("kind")
    if isinstance(kind, str) and kind in BUDGET_KINDS:
        out["kind"] = kind
    tool = params.get("tool")
    if isinstance(tool, str) and _TOOL_NAME.fullmatch(tool) and _mask_text(tool, 64) == tool:
        out["tool"] = tool
    return out


def _budget_params(reason: str) -> dict[str, Any]:
    match = _BUDGET_REASON.match(reason)
    if match:
        return {"kind": match.group(1), "limit": int(match.group(2))}
    if reason.startswith("max_tokens_per_run"):
        return {"kind": "max_tokens_per_run"}
    return {}


def _mask_text(text: Any, limit: int = SUMMARY_LIMIT) -> str | None:
    if text is None:
        return None
    from stiracore.observability.masking import mask_secrets

    return mask_secrets(str(text))[:limit]


def _heartbeat_health(heartbeat: dict[str, Any], now: float) -> tuple[bool, bool]:
    """``(stalled, healthy)`` of a heartbeat status (ADR 0008 §11, ADR 0021 §8).

    *stalled* (the ``heartbeat_stalled`` alert): enabled but stopped, degraded or overdue.
    *healthy* (the start condition of a model-calling run): enabled, running, not degraded
    and its last tick within ``max(3 x interval, 120 s)``; a heartbeat that is off, has not
    ticked yet or reports anything unreadable is not healthy."""
    enabled = heartbeat.get("enabled") is True
    last = heartbeat.get("last_tick_at")
    if isinstance(last, bool) or not isinstance(last, (int, float)):
        last = None
    try:
        interval = float(heartbeat.get("interval_sec") or 60.0)
    except (TypeError, ValueError):
        interval = 60.0
    limit = max(3.0 * interval, 120.0)
    overdue = last is not None and now - float(last) > limit
    running = bool(heartbeat.get("running"))
    degraded = bool(heartbeat.get("degraded"))
    stalled = bool(heartbeat.get("enabled")) and (not running or degraded or overdue)
    healthy = enabled and running and not degraded and last is not None and not overdue
    return stalled, healthy


def normalize_level(level: Any) -> str:
    name = getattr(level, "value", level)
    name = str(name).rsplit(".", 1)[-1].strip().lower()
    return name if name in (NORMAL, WARNING, COHERENCE, CRITICAL) else CRITICAL


def audit_principal(principal: Any) -> str:
    """``local:<name>`` restricted to an audit-safe alphabet, else ``local:unknown``."""
    from stiracore.observability.masking import mask_secrets

    if (
        isinstance(principal, str)
        and _PRINCIPAL.fullmatch(principal)
        and mask_secrets(principal) == principal
    ):
        return principal
    kind = principal.partition(":")[0] if isinstance(principal, str) else ""
    return f"{kind}:unknown" if kind in ("local", "system") else "local:unknown"


@dataclass
class _Job:
    definition: JobDefinition
    created_at: float
    created_by: str
    updated_at: float
    cursor: float
    state: str = "active"  # active / paused / finished
    paused_by: str | None = None
    paused_reason: str | None = None
    consecutive_failures: int = 0
    last_run_id: str | None = None
    generation: int = 1  # +1 per job_updated; a queued run of an older one never runs


class Scheduler:
    def __init__(
        self,
        state_dir: str | Path,
        *,
        now_fn: Callable[[], float] | None = None,
        breaker_level: Callable[[], Any] | None = None,
        gateway_actions: dict[str, Callable[[RunContext], Any]] | None = None,
        agent_executor: Callable[[RunContext], RunOutcome] | None = None,
        health_source: Callable[[], dict[str, Any]] | None = None,
        daily_token_budget: int | None = None,
        failure_alert_after: int = 3,
        grace_sec: float = GRACE_SEC,
        dispatch: Callable[[Callable[[], None]], Any] | None = None,
        event_log: SchedulerEventLog | None = None,
        max_wait_sec: float = 30.0,
        cancel_workers: int = 4,
        max_cancel_backlog: int = 16,
        action_checks: dict[str, Callable[[], str | None]] | None = None,
        memory_executor: Callable[[RunContext], RunOutcome] | None = None,
        memory_kinds: frozenset[str] | set[str] = frozenset(),
        memory_daily_token_budget: int | None = None,
        agent_daily_token_budget: int = AGENT_DAILY_TOKEN_BUDGET,
        approval_waits: Any = None,
    ) -> None:
        self.state_dir = Path(state_dir)
        # ADR 0021 §4 / Q3: every Agent-layer job of a UTC day shares this pool (no
        # "unlimited"); ``daily_token_budget`` stays the ceiling over every layer.
        if (
            isinstance(agent_daily_token_budget, bool)
            or not isinstance(agent_daily_token_budget, int)
            or not 1 <= agent_daily_token_budget <= 10_000_000
        ):
            raise ValueError("agent_daily_token_budget must be an integer between 1 and 10000000")
        self._agent_daily_token_budget = agent_daily_token_budget
        # ADR 0018: fixed-kind memory jobs, run by their own executor (never the heartbeat
        # tick). ``memory_kinds`` = the kinds a person may create here (empty = none);
        # ``memory_daily_token_budget`` = one cap for all memory jobs of a UTC day.
        self._memory_executor = memory_executor
        self._memory_kinds = (
            frozenset(memory_kinds) & frozenset(MEMORY_JOB_KINDS)
            if memory_executor is not None
            else frozenset()
        )
        self._memory_daily_token_budget = memory_daily_token_budget
        # Per-action precondition for create / update / resume (#251): a non-None result
        # is a reason code and the change is refused (scope). Never called from a run.
        self._action_checks = dict(action_checks or {})
        self._now = now_fn or time.time
        self._breaker_level = breaker_level or (lambda: NORMAL)
        self._actions = dict(gateway_actions or {})
        self._agent_executor = agent_executor
        # ADR 0024 phase 2: runs waiting for approval (``JobApprovalWaits``); None = deny.
        self._approval_waits = approval_waits
        self._health_source = health_source
        self._daily_token_budget = daily_token_budget
        self._failure_alert_after = max(1, int(failure_alert_after))
        self._grace = max(0.0, float(grace_sec))
        self._dispatch = dispatch
        self._max_wait = max(1.0, float(max_wait_sec))
        self.log = event_log or SchedulerEventLog(self.state_dir / LOG_FILENAME)
        # The shared owner-lock file implementation (no-follow, #322).
        self._owner = HeartbeatOwnerLock(self.state_dir, LOCK_FILENAME)

        self._lock = threading.RLock()
        self._state = SchedulerState.STOPPED
        self._error: str | None = None
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        # Dispatched batches whose worker has not finished. While > 0 after a stop
        # request, the owner lock is kept (a late worker must not overlap a new owner).
        self._live_workers = 0
        # Cancellation-callback threads (one per cancelled run, bounded). They hold no
        # scheduler lock and cannot run a job body; at the cap, new runs are refused.
        # Cancellation callbacks are never dropped: they are queued and run by at most
        # ``cancel_workers`` threads (started on demand, exit when idle).
        self._cancel_lock = threading.Lock()
        self._cancel_wakeup = threading.Condition(self._cancel_lock)
        self._cancel_queue: deque = deque()  # guarded by _cancel_lock
        self._cancel_workers: set[threading.Thread] = set()
        self._cancel_idle = 0
        self._cancel_backlog = 0  # queued + running callback batches
        self._max_cancel_workers = max(1, int(cancel_workers))
        self._max_cancel_backlog = max(1, int(max_cancel_backlog))
        self._stop_requested = False
        self._idle = threading.Condition(self._lock)
        # run_id -> (job_id, UTC day, tokens) reserved for agent runs not yet settled.
        self._reserved: dict[str, tuple[str, str, int]] = {}
        # An open recovery (recovery_started without recovery_completed) bans every run.
        self._recovering = False
        self._jobs: dict[str, _Job] = {}
        self._runs: OrderedDict[str, dict[str, Any]] = OrderedDict()
        # run_key -> scheduled time; keys older than the look-back window are pruned.
        self._run_keys: dict[str, float] = {}
        self._active: dict[str, RunContext] = {}
        # Active runs waiting for an approval, with no worker (ADR 0024 §6.1).
        self._parked: set[str] = set()
        # run_id -> (request_id, approver): an approval recorded while the run's first
        # segment was still ending (its request already stored and listed, the run not yet
        # parked). The continuation is scheduled when the run parks (``_park``), never
        # earlier, and never for a run that ends instead (``_finish`` drops it).
        self._resume_on_park: dict[str, tuple[str, str]] = {}
        self._held: dict[str, tuple[float, str]] = {}
        self._usage: dict[tuple[str, str], dict[str, int]] = {}
        self._global_tokens: dict[str, int] = {}
        self._notifications: deque[dict[str, Any]] = deque(maxlen=NOTIFICATION_HISTORY)
        self._alerts: dict[str, str] = {}
        self._last_tick_at: float | None = None
        self._last_level: str | None = None
        # ADR 0021 §8: the heartbeat health read at the last tick (None = no heartbeat is
        # wired to this scheduler, e.g. an embedder without one; the chat server always wires it).
        self._heartbeat_healthy: bool | None = None
        self._tick_failures = 0
        # W6d (ADR 0011 §8): the ``grant_tick`` fire kind, attached by the chat server only
        # with autonomy.heartbeat_start (None: nothing of it runs); ``_grant_event`` marks
        # an event-driven wake (a heartbeat tick) until the next tick reads it.
        self._grant_tick: Any = None
        self._grant_event = False
        self._run_token = RunToken()
        self._run_token.valid = False  # not started

    # -- properties ----------------------------------------------------------

    @property
    def state(self) -> SchedulerState:
        return self._state

    @property
    def running(self) -> bool:
        return self._state is SchedulerState.RUNNING

    @property
    def agent_jobs_enabled(self) -> bool:
        return self._agent_executor is not None

    def attach_grant_tick(self, ticker: Any) -> None:
        """W6d: the ``grant_tick`` fire kind (a ``GrantTicker``), evaluated after the jobs
        on every tick of this scheduler's own loop — outside the scheduler lock, on its
        own thread (no new thread). Once only."""
        with self._lock:
            if self._grant_tick is not None:
                raise RuntimeError("grant_tick attached")
            self._grant_tick = ticker

    def wake_grant_tick(self) -> None:
        """W6d: an event-driven wake (the heartbeat ticked): the loop ticks now and the
        ``grant_tick`` fire is marked as an event (its flood guard applies). Memory only;
        never runs anything on the caller's thread."""
        if self._grant_tick is None:
            return
        with self._lock:
            self._grant_event = True
        self._wake.set()

    # -- lifecycle -----------------------------------------------------------

    def start(self, *, background: bool = True) -> None:
        """Take the owner lock, verify and replay the log, close interrupted runs.

        ``SchedulerLockError``: another owner. ``SchedulerLockFileUnsafeError`` (also a
        ``SchedulerLockError`` and an ``UnsafeLockFileError``): ``scheduler.lock`` is a
        symlink or not a plain file of this user (#322; nothing written, still STOPPED). ``SchedulerLogError``: the log cannot be trusted (state UNAVAILABLE,
        nothing runs, the file is kept).
        """
        with self._lock:
            if self._state is not SchedulerState.STOPPED:
                raise RuntimeError(f"scheduler is {self._state.value}")
            try:
                self._owner.acquire()
            except UnsafeLockFileError as error:
                raise SchedulerLockFileUnsafeError(error.path, error.reason) from None
            except HeartbeatLockError:
                raise SchedulerLockError(
                    "scheduler already running for this state directory"
                ) from None
            try:
                events = self.log.open()
                for event in events:
                    self._apply(event)
                now = self._now()
                self._state = SchedulerState.RUNNING
                self._run_token = RunToken()  # W6d: this run's token (valid until stop)
                self._stop_requested = False
                if self.log.needs_recovery or self._recovering:
                    self._recover(now)
                interrupted = [
                    r
                    for r, rec in self._runs.items()
                    if rec["status"] in ("running", "awaiting_approval")
                ]
                for run_id in interrupted:
                    run = self._runs[run_id]
                    self._record(
                        "run_cancelled",
                        {
                            "run_id": run_id,
                            "job_id": run["job_id"],
                            "by": "system",
                            "reason": "interrupted",
                            # Usage of a run that died is unknown: charge the reservation
                            # recorded when it started (not today's definition).
                            "tokens": int(run.get("reserved") or 0),
                        },
                        now,
                    )
                self._pause_memory_jobs_at_start(now)
                if self._approval_waits is not None:
                    try:
                        self._approval_waits.cancel_at_start()
                    except Exception as error:  # noqa: BLE001 - fail closed (ADR 0024 §7)
                        raise SchedulerRefused(
                            "unavailable",
                            f"job approvals could not be cancelled at start: {type(error).__name__}",
                        ) from None
                if background:
                    self._wake.clear()
                    thread = threading.Thread(
                        target=self._run_loop, name="stira-scheduler", daemon=True
                    )
                    thread.start()
                    self._thread = thread
            except (SchedulerLogError, SchedulerRefused) as error:
                self._run_token.valid = False
                self._state = SchedulerState.UNAVAILABLE
                self._error = str(error)
                self._owner.release()
                if isinstance(error, SchedulerLogError):
                    raise
                raise SchedulerLogError(str(error)) from None
            except BaseException:
                self._run_token.valid = False
                self._state = SchedulerState.STOPPED
                self._owner.release()
                raise

    def _pause_memory_jobs_at_start(self, now: float) -> None:
        """ADR 0018 §2e / Q4: a model-calling memory job never resumes by itself after a
        restart, and no memory job runs on a server without the memory executor.

        ADR 0021 §10: one ``jobs_paused_at_start`` notification per start with the count."""
        paused = 0
        for job_id in sorted(self._jobs):
            job = self._jobs[job_id]
            definition = job.definition
            if job.state != "active" or definition.layer not in ("memory", "agent"):
                continue
            if definition.layer == "agent":
                # ADR 0021 §9: every start, with or without the Agent layer on.
                reason = AGENT_RESTART_PAUSE_REASON
            elif self._memory_executor is None or definition.action not in self._memory_kinds:
                reason = MEMORY_OFF_PAUSE_REASON
            elif definition.action in MODEL_CALLING_KINDS:
                reason = RESTART_PAUSE_REASON
            else:
                continue
            self._record("job_paused", {"job_id": job_id, "by": "system", "reason": reason}, now)
            paused += 1
        if paused:
            self._notify(
                "jobs_paused_at_start",
                "info",
                f"{paused} job(s) were paused at start; a person resumes them",
                now,
                params={"count": paused},
            )

    def stop(self, timeout: float = 5.0) -> bool:
        """Request a stop; ``True`` once stopped and the owner lock released.

        Always accepted (any state, any breaker level). Active runs are cancelled. A
        worker that is still alive at the deadline keeps the owner lock: the state stays
        STOPPING (``False``) and the last worker to finish completes the stop, so a new
        owner can never overlap a run of this one.
        """
        self._run_token.valid = False  # W6d: first, lock-free — a wake in progress is void
        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._lock:
            if self._state is SchedulerState.STOPPED:
                return True
            if self._state is SchedulerState.UNAVAILABLE and not self._owner.held:
                return True
            if self._state is SchedulerState.RUNNING:
                self._state = SchedulerState.STOPPING
            self._stop_requested = True
            self._wake.set()
            thread = self._thread
            for ctx in list(self._active.values()):
                ctx.cancel("shutdown")  # callbacks run on their own thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(max(0.0, deadline - time.monotonic()))
        with self._lock:
            if self._thread is not None and not self._thread.is_alive():
                self._thread = None
            while self._live_workers > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                self._idle.wait(remaining)
            if not self._stop_requested:
                return not self._owner.held  # completed by the worker / tick thread
            return self._maybe_complete_stop()

    def _spawn_cancel(self, run_id: str, callbacks: list[Callable[[], None]]) -> None:
        """Queue one run's callbacks; a bounded pool runs them (never synchronously)."""
        with self._cancel_lock:
            self._cancel_backlog += 1
            self._cancel_queue.append((run_id, callbacks))
            # ``_cancel_idle`` counts workers parked in ``wait`` that have not yet woken to
            # take an item, so a worker is only spoken for when an item is queued per idle
            # worker. Spawn whenever more items are queued than idle workers (up to the cap):
            # a stale idle count may spawn a worker early but can never leave an item without
            # a worker while the pool is below its cap (#356).
            if (
                len(self._cancel_queue) > self._cancel_idle
                and len(self._cancel_workers) < self._max_cancel_workers
            ):
                thread = threading.Thread(
                    target=self._cancel_worker,
                    name=f"stira-job-cancel-{len(self._cancel_workers)}",
                    daemon=True,
                )
                self._cancel_workers.add(thread)
                try:
                    thread.start()
                except Exception as error:  # noqa: BLE001 - e.g. "can't start new thread"
                    # Never leave a registered worker that does not exist: it would count
                    # toward the cap forever. The item stays queued (and counted in the
                    # backlog); the next put, or a worker finishing, takes it up again.
                    self._cancel_workers.discard(thread)
                    logger.warning(
                        "scheduler: cancellation worker did not start (%s); callbacks stay queued",
                        type(error).__name__,
                    )
            self._cancel_wakeup.notify()
            if self._cancel_backlog >= self._max_cancel_backlog:
                logger.warning(
                    "scheduler: %d cancellation callbacks pending; new runs are refused",
                    self._cancel_backlog,
                )

    def _cancel_worker(self) -> None:
        current = threading.current_thread()
        while True:
            with self._cancel_lock:
                if not self._cancel_queue:
                    self._cancel_idle += 1
                    try:
                        self._cancel_wakeup.wait(timeout=1.0)
                    finally:
                        self._cancel_idle -= 1
                    if not self._cancel_queue:
                        # Puts happen under this lock, so an empty queue here stays drained
                        # until the next put, which starts a worker if none is left.
                        self._cancel_workers.discard(current)
                        return
                _run_id, callbacks = self._cancel_queue.popleft()
            try:
                for callback in callbacks:
                    _safe_call(callback)
            finally:
                with self._cancel_lock:
                    self._cancel_backlog -= 1

    def _maybe_complete_stop(self) -> bool:
        """Caller holds the lock. Release ownership only when nothing can still run."""
        if not self._stop_requested:
            return False
        if self._live_workers > 0:
            return False
        thread = self._thread
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            return False
        now = self._now()
        for run_id, ctx in list(self._active.items()):
            if run_id in self._parked and self._approval_waits is not None:
                # A waiting run ends with the process: its request is revoked now.
                self._parked.discard(run_id)
                self._approval_waits.release(ctx.waiting_request or "", "shutdown")
            # Started but never dispatched (e.g. dispatch failed): close, charge, no retry.
            self._record_safely(
                "run_cancelled",
                {
                    "run_id": run_id,
                    "job_id": ctx.job.job_id,
                    "by": "system",
                    "reason": "shutdown",
                    "tokens": self._reserved.get(run_id, ("", "", 0))[2],
                },
                now,
            )
            self._active.pop(run_id, None)
        self._state = SchedulerState.UNAVAILABLE if self._error else SchedulerState.STOPPED
        self._thread = None
        self._stop_requested = False
        if self._owner.held:
            self._owner.release()
        self._idle.notify_all()
        return True

    def _run_loop(self) -> None:
        try:
            self._loop()
        finally:
            with self._lock:
                self._thread = None
                self._maybe_complete_stop()

    def _loop(self) -> None:
        while True:
            with self._lock:
                if self._state is not SchedulerState.RUNNING:
                    return
            try:
                self.tick()
                self._tick_failures = 0
            except Exception as error:  # noqa: BLE001 - a failing tick must not kill the loop
                self._tick_failures += 1
                logger.warning("scheduler tick failed (%s)", type(error).__name__)
            self._wake.wait(self._next_wait())
            self._wake.clear()

    def _next_wait(self) -> float:
        now = self._now()
        soonest = None
        with self._lock:
            for job in self._jobs.values():
                if job.state != "active":
                    continue
                fire = job.definition.schedule.next_after(max(job.cursor, now))
                if fire is not None and (soonest is None or fire < soonest):
                    soonest = fire
        wait = self._max_wait if soonest is None else soonest - now
        return min(max(wait, 1.0), self._max_wait)

    # -- event application (replay and live share this path) -----------------

    def _record(self, kind: str, body: dict[str, Any], now: float) -> dict[str, Any]:
        if self._state not in (SchedulerState.RUNNING, SchedulerState.STOPPING):
            raise SchedulerRefused("unavailable", "the scheduler is not running")
        try:
            event = self.log.append(kind, body, now)
        except SchedulerLogError as error:
            self._fail_unavailable(str(error))
            raise SchedulerRefused("unavailable", "the scheduler log is unavailable") from None
        self._apply(event)
        return event

    def _record_safely(self, kind: str, body: dict[str, Any], now: float) -> None:
        try:
            self._record(kind, body, now)
        except SchedulerRefused:
            pass

    def _fail_unavailable(self, message: str) -> None:
        """The log cannot be extended: nothing more may run (fail-closed)."""
        self._error = message
        if self._state is SchedulerState.RUNNING:
            self._state = SchedulerState.UNAVAILABLE
            self._wake.set()
        for ctx in list(self._active.values()):
            ctx.cancel("log_unavailable")

    def _apply(self, event: dict[str, Any]) -> None:
        kind, body, ts = event["kind"], event["body"], float(event["ts"])
        job = self._jobs.get(body.get("job_id", ""))
        if kind == "recovery_started":
            self._recovering = True
            return
        if kind == "recovery_completed":
            self._recovering = False
            return
        if kind == "job_created":
            try:
                definition = parse_job(
                    body["job"],
                    now=ts,
                    gateway_actions=None,
                    agent_jobs=True,
                    memory_kinds=None,
                    stored=True,
                )
            except (JobError, KeyError, TypeError):
                logger.error("scheduler: stored job definition invalid; ignored")
                return
            self._jobs[definition.job_id] = _Job(
                definition=definition,
                created_at=ts,
                created_by=body.get("by", "unknown"),
                updated_at=ts,
                cursor=ts,
            )
        elif job is None:
            if kind == "notification":
                self._apply_notification(event)
            return
        elif kind == "job_updated":
            try:
                job.definition = parse_job(
                    body["job"],
                    now=ts,
                    gateway_actions=None,
                    agent_jobs=True,
                    memory_kinds=None,
                    stored=True,
                )
            except (JobError, KeyError, TypeError):
                job.state, job.paused_by = "paused", "system"
                job.paused_reason = "stored definition invalid"
                return
            job.updated_at = ts
            job.generation += 1
            job.cursor = max(job.cursor, ts)
            self._held.pop(job.definition.job_id, None)
        elif kind == "job_paused":
            job.state = "paused"
            job.paused_by, job.paused_reason = body.get("by"), body.get("reason")
            self._held.pop(job.definition.job_id, None)
        elif kind == "job_resumed":
            job.state, job.paused_by, job.paused_reason = "active", None, None
            job.cursor = max(job.cursor, ts)
            job.consecutive_failures = 0
        elif kind == "job_finished":
            job.state = "finished"
        elif kind in ("run_started", "run_skipped", "run_held"):
            key, scheduled = body.get("run_key"), body.get("scheduled_at")
            if key and isinstance(scheduled, (int, float)):
                self._run_keys[key] = float(scheduled)
            if isinstance(scheduled, (int, float)):
                job.cursor = max(job.cursor, float(scheduled))
            if kind == "run_held":
                self._held[job.definition.job_id] = (float(scheduled), key)
            elif self._held.get(job.definition.job_id, (None, None))[1] == key:
                self._held.pop(job.definition.job_id, None)
            if kind == "run_started":
                self._runs[body["run_id"]] = {
                    "run_id": body["run_id"],
                    "job_id": job.definition.job_id,
                    "run_key": key,
                    "scheduled_at": scheduled,
                    "started_at": ts,
                    "ended_at": None,
                    "status": "running",
                    "reason": None,
                    "tokens": 0,
                    "summary": None,
                }
                job.last_run_id = body["run_id"]
                day = body.get("charge_day") if isinstance(body.get("charge_day"), str) else None
                day = day or _day(ts)
                reserved = body.get("reserved_tokens")
                reserved = reserved if type(reserved) is int and reserved > 0 else 0
                self._runs[body["run_id"]].update(charge_day=day, reserved=reserved)
                if reserved:
                    # Same path live and on replay: an unsettled run keeps holding it.
                    self._reserved[body["run_id"]] = (job.definition.job_id, day, reserved)
                usage = self._usage.setdefault((job.definition.job_id, day), {})
                usage["runs"] = usage.get("runs", 0) + 1
                while len(self._runs) > RUN_HISTORY:
                    oldest = next(iter(self._runs))
                    # An unsettled run (running, or parked waiting for an approval) is
                    # kept: its end event settles the reservation (#601 B15).
                    if self._runs[oldest]["status"] in ("running", "awaiting_approval"):
                        break
                    self._runs.popitem(last=False)
        elif kind == "run_missed":
            last = body.get("last")
            if isinstance(last, (int, float)):
                job.cursor = max(job.cursor, float(last))
        elif kind == "run_awaiting_approval":
            run = self._runs.get(body.get("run_id", ""))
            # Only a run that really waits (``wait`` job, ADR 0024) is awaiting approval. A
            # deny-mode run is being stopped: it stays ``running`` until its end event, so
            # a reader never sees a waiting state that is none (W5 gate, attack A7).
            if run is not None and body.get("wait") is True:
                run["status"] = "awaiting_approval"
        elif kind == "run_resumed":
            run = self._runs.get(body.get("run_id", ""))
            if run is not None and run["status"] == "awaiting_approval":
                run["status"] = "running"
        elif kind in ("run_completed", "run_failed", "run_cancelled"):
            run = self._runs.get(body.get("run_id", ""))
            if run is None or run["status"] not in ("running", "awaiting_approval"):
                return
            tokens = int(body.get("tokens") or 0)
            run.update(
                status=kind.removeprefix("run_"),
                reason=body.get("reason"),
                ended_at=ts,
                tokens=tokens,
                summary=body.get("summary"),
            )
            self._reserved.pop(run["run_id"], None)
            day = run.get("charge_day") or _day(run["started_at"])
            usage = self._usage.setdefault((job.definition.job_id, day), {})
            usage["tokens"] = usage.get("tokens", 0) + tokens
            if tokens:
                self._global_tokens[day] = self._global_tokens.get(day, 0) + tokens
            if kind == "run_completed":
                job.consecutive_failures = 0
            elif kind == "run_failed":
                job.consecutive_failures += 1
        if kind == "notification":
            self._apply_notification(event)

    def _apply_notification(self, event: dict[str, Any]) -> None:
        body = event["body"]
        note = {
            "id": event["seq"],
            "at": event["ts"],
            "code": body.get("code"),
            "key": body.get("key"),
            "severity": body.get("severity"),
            "state": body.get("state"),
            "message": body.get("message"),
            "job_id": body.get("job_id"),
            # #279: variables for the UI wording (allowlisted; {} for older entries).
            "params": _note_params(body.get("params")),
        }
        self._notifications.append(note)
        key = body.get("key")
        if key and body.get("state") == "raised":
            self._alerts[key] = body.get("code")
        elif key and body.get("state") == "resolved":
            self._alerts.pop(key, None)

    # -- notifications --------------------------------------------------------

    def _notify(
        self,
        code: str,
        severity: str,
        message: str,
        now: float,
        *,
        key: str | None = None,
        state: str = "event",
        job_id: str | None = None,
        params: dict[str, Any] | None = None,
    ) -> None:
        body = {
            "code": code,
            "key": key,
            "severity": severity,
            "state": state,
            "message": _mask_text(message, 300),
            "job_id": job_id,
        }
        if params:
            body["params"] = _note_params(params)
        log = logger.warning if severity in ("warning", "critical") else logger.info
        log("scheduler notification [%s] %s: %s", severity, code, body["message"])
        self._record_safely("notification", body, now)

    def _alert(self, key: str, code: str, active: bool, message: str, now: float, **kw) -> None:
        if active and key not in self._alerts:
            self._notify(
                code, kw.pop("severity", "warning"), message, now, key=key, state="raised", **kw
            )
        elif not active and key in self._alerts:
            kw.pop("severity", None)
            self._notify(code, "info", f"resolved: {message}", now, key=key, state="resolved", **kw)

    def _recover(self, now: float) -> None:
        """First a durable ban on every run, then pause every job, then lift the ban.

        A crash anywhere in between leaves ``recovery_started`` open, so the next start
        repeats the recovery instead of trusting a partially paused set of jobs.
        """
        if not self._recovering:
            self._record(
                "recovery_started",
                {"reason": "log interrupted or last entry deleted; pausing every job"},
                now,
            )
        self._notify(
            "log_recovered",
            "warning",
            "the scheduler log was interrupted or its last entry deleted; every active "
            "job was paused and needs a human to resume it",
            now,
        )
        for job in sorted(self._jobs.values(), key=lambda j: j.definition.job_id):
            if job.state == "active":
                self._record(
                    "job_paused",
                    {
                        "job_id": job.definition.job_id,
                        "by": "system",
                        "reason": "log recovered; a human must resume",
                    },
                    now,
                )
        self._record("recovery_completed", {}, now)

    def _health(self, now: float, level: str) -> None:
        self._alert(
            "breaker_critical",
            "breaker_critical",
            level == CRITICAL,
            "breaker is CRITICAL: no job starts; active runs were cancelled",
            now,
            severity="critical",
        )
        if self._health_source is None:
            self._heartbeat_healthy = None
            return
        heartbeat = self._read_heartbeat()
        stalled, healthy = _heartbeat_health(heartbeat, now)
        self._heartbeat_healthy = healthy
        if healthy:
            self._alert(
                "heartbeat_required",
                "heartbeat_required",
                False,
                "the heartbeat is healthy again; model-calling jobs may start",
                now,
            )
        self._alert(
            "heartbeat_stalled",
            "heartbeat_stalled",
            stalled,
            "heartbeat is enabled but not ticking (stopped, degraded or overdue)",
            now,
        )

    def _read_heartbeat(self) -> dict[str, Any]:
        try:
            return dict(self._health_source() or {})
        except Exception as error:  # noqa: BLE001 - health must not break the tick
            return {"enabled": True, "running": False, "last_error": type(error).__name__}

    def _heartbeat_refusal(self, definition: JobDefinition, healthy: bool | None) -> bool:
        """ADR 0021 §8: ``True`` when a model-calling run may not start now."""
        return healthy is False and definition.needs_heartbeat

    def _current_heartbeat(self) -> bool | None:
        """A fresh read for the pre-execution re-check (``None`` = no heartbeat wired)."""
        if self._health_source is None:
            return None
        return _heartbeat_health(self._read_heartbeat(), self._now())[1]

    # -- tick ----------------------------------------------------------------

    def _level(self) -> str:
        try:
            return normalize_level(self._breaker_level())
        except Exception:  # noqa: BLE001 - unknown breaker = most restrictive
            return CRITICAL

    def tick(self) -> None:
        """Evaluate every job once at the current clock reading."""
        level = self._level()
        launches: list[list[RunContext]] = []
        with self._lock:
            if self._state is not SchedulerState.RUNNING:
                return
            now = self._now()
            self._last_tick_at, self._last_level = now, level
            self._prune_run_keys(now)
            self._health(now, level)
            self._sweep_waiting()
            if level == CRITICAL:
                for ctx in list(self._active.values()):
                    ctx.cancel("breaker_critical")
                for job_id, (scheduled, key) in list(self._held.items()):
                    self._record_safely(
                        "run_skipped",
                        {
                            "job_id": job_id,
                            "run_key": key,
                            "scheduled_at": scheduled,
                            "reason": "breaker_critical",
                        },
                        now,
                    )
                    self._held.pop(job_id, None)
            for job_id in sorted(self._jobs):
                if self._state is not SchedulerState.RUNNING:
                    break
                batch = self._evaluate(self._jobs[job_id], now, level)
                if batch:
                    launches.append(batch)
        for batch in launches:
            self._launch(batch)
        self._fire_grant_tick()

    def _fire_grant_tick(self) -> None:
        """W6d: one ``grant_tick`` fire per tick (none when not attached or not running),
        outside the scheduler lock. The heartbeat health is read fresh (ADR 0021 §8; no
        heartbeat wired = not healthy: the tick is skipped)."""
        ticker = self._grant_tick
        if ticker is None:
            return
        with self._lock:
            if self._state is not SchedulerState.RUNNING:
                return
            source = "event" if self._grant_event else "periodic"
            self._grant_event = False
            token = self._run_token
        try:
            ticker.fire(
                source=source,
                heartbeat_healthy=lambda: self._current_heartbeat(),
                run_token=token,
            )
        except Exception as error:  # noqa: BLE001 - a fire never breaks the jobs' tick
            logger.warning("scheduler: the grant tick failed (%s)", type(error).__name__)

    def _prune_run_keys(self, now: float) -> None:
        """Fires are only enumerated after ``max(cursor, now - MAX_LOOKBACK_SEC)``, so a
        key older than twice the window can never be produced again."""
        if len(self._run_keys) <= 10_000:
            return
        horizon = now - 2 * MAX_LOOKBACK_SEC
        held = {key for _scheduled, key in self._held.values()}
        for key, scheduled in list(self._run_keys.items()):
            if scheduled < horizon and key not in held:
                del self._run_keys[key]

    def _evaluate(self, job: _Job, now: float, level: str) -> list[RunContext]:
        if job.state != "active" or self._recovering:
            return []
        definition = job.definition
        job_id = definition.job_id
        candidates: list[tuple[float, str]] = []
        held = self._held.get(job_id)
        if held is not None and level == NORMAL:
            scheduled, key = held
            if now - scheduled > definition.hold_max_sec:
                self._held.pop(job_id, None)
                self._skip(job_id, key, scheduled, "held_expired", now)
            else:
                candidates.append((scheduled, key))

        start = max(job.cursor, now - MAX_LOOKBACK_SEC)
        truncated = start > job.cursor
        fires = definition.schedule.fires_between(start, now, SCAN_LIMIT)
        job.cursor = max(job.cursor, now)
        new = [(t, f"{job_id}@{t:.0f}") for t in fires]
        new = [(t, key) for t, key in new if key not in self._run_keys]
        due = [item for item in new if now - item[0] <= self._grace]
        missed = [item for item in new if now - item[0] > self._grace]
        run: list[tuple[float, str]] = []
        policy = definition.effective_missed_policy
        if policy == "catch_up":
            run = missed[-definition.max_catch_up :]
            missed = missed[: max(0, len(missed) - definition.max_catch_up)]
            run += due[-1:]
        elif policy == "latest" and not due:
            run, missed = missed[-1:], missed[:-1]
        else:
            run = due[-1:]
        coalesced = due[:-1]
        if missed or truncated:
            self._record_safely(
                "run_missed",
                {
                    "job_id": job_id,
                    "count": len(missed),
                    "truncated": truncated,
                    "first": missed[0][0] if missed else None,
                    "last": missed[-1][0] if missed else None,
                    "policy": policy,
                },
                now,
            )
        for scheduled, key in coalesced:
            self._skip(job_id, key, scheduled, "coalesced", now)
        candidates += run

        batch: list[RunContext] = []
        active_run = any(ctx.job.job_id == job_id for ctx in self._active.values())
        for scheduled, key in candidates:
            if level == CRITICAL:
                self._skip(job_id, key, scheduled, "breaker_critical", now)
                continue
            if level != NORMAL:
                previous = self._held.get(job_id)
                if previous is not None and previous[1] != key:
                    self._skip(job_id, previous[1], previous[0], "held_replaced", now)
                if previous is None or previous[1] != key:
                    self._record_safely(
                        "run_held",
                        {
                            "job_id": job_id,
                            "run_key": key,
                            "scheduled_at": scheduled,
                            "level": level,
                        },
                        now,
                    )
                continue
            if self._heartbeat_refusal(definition, self._heartbeat_healthy):
                self._skip(job_id, key, scheduled, "heartbeat", now)
                self._alert(
                    "heartbeat_required",
                    "heartbeat_required",
                    True,
                    "a model-calling job was skipped: it needs a healthy heartbeat "
                    "(enabled, running, not degraded, ticking on time)",
                    now,
                    job_id=job_id,
                )
                continue
            if active_run or batch and policy != "catch_up":
                self._skip(job_id, key, scheduled, "overlap", now)
                continue
            refusal = self._budget_refusal(definition, now)
            if refusal is not None:
                self._skip(job_id, key, scheduled, "budget", now)
                self._pause_for_budget(job, refusal, now)
                break
            ctx = self._start_run(definition, scheduled, key, now)
            if ctx is None:
                break
            batch.append(ctx)
        if (
            definition.schedule.type == "once"
            and definition.schedule.next_after(job.cursor) is None
            and job.state == "active"
            and job_id not in self._held
        ):
            self._record_safely("job_finished", {"job_id": job_id, "reason": "once"}, now)
        return batch

    def _skip(self, job_id: str, key: str, scheduled: float, reason: str, now: float) -> None:
        self._record_safely(
            "run_skipped",
            {"job_id": job_id, "run_key": key, "scheduled_at": scheduled, "reason": reason},
            now,
        )

    @staticmethod
    def _spends_tokens(definition: JobDefinition) -> bool:
        """Agent jobs and the model-calling memory kinds (ADR 0018 §2d); nothing else."""
        return is_model_calling(definition.layer, definition.action)

    def _reservation_of(self, job_id: str) -> int:
        job = self._jobs.get(job_id)
        if job is None or not self._spends_tokens(job.definition):
            return 0
        return int(job.definition.budget.max_tokens_per_run or 0)

    def _layer_tokens(self, day: str, layer: str) -> int:
        """Tokens used and reserved by every job of ``layer`` on ``day`` (caller holds the lock)."""
        memory = {j for j, job in self._jobs.items() if job.definition.layer == layer}
        used = sum(
            usage.get("tokens", 0)
            for (job_id, used_day), usage in self._usage.items()
            if used_day == day and job_id in memory
        )
        reserved = sum(
            tokens
            for owner, reserved_day, tokens in self._reserved.values()
            if reserved_day == day and owner in memory
        )
        return used + reserved

    def _reserved_tokens(self, day: str, job_id: str | None = None) -> int:
        return sum(
            tokens
            for owner, reserved_day, tokens in self._reserved.values()
            if reserved_day == day and (job_id is None or owner == job_id)
        )

    def _budget_refusal(
        self, definition: JobDefinition, now: float, *, extra: int = 0, day: str | None = None
    ) -> str | None:
        """``None`` when one more run fits. Caller holds the lock.

        Started runs are already counted (``run_started``), and every unsettled agent
        run holds a reservation of its ``max_tokens_per_run``, across all jobs, so two
        jobs starting in the same tick cannot both spend the same remaining budget.
        ``extra`` = tokens the caller itself already reserved (pre-execution re-check;
        its reservation is then already inside the reserved totals of ``day``, together
        with every other unsettled reservation of that day).
        """
        budget = definition.budget
        day = day or _day(now)
        usage = self._usage.get((definition.job_id, day), {})
        max_runs = budget.max_runs_per_day
        if extra == 0 and max_runs is not None and usage.get("runs", 0) >= max_runs:
            return f"max_runs_per_day ({budget.max_runs_per_day}) reached"
        if not self._spends_tokens(definition):
            return None
        per_run = 0 if extra else int(budget.max_tokens_per_run or 0)
        day_cap = budget.max_tokens_per_day
        job_total = usage.get("tokens", 0) + self._reserved_tokens(day, definition.job_id)
        if day_cap is not None and job_total + per_run > day_cap:
            return f"max_tokens_per_day ({day_cap}) would be exceeded"
        if self._daily_token_budget is not None:
            total = self._global_tokens.get(day, 0) + self._reserved_tokens(day)
            if total + per_run > self._daily_token_budget:
                return f"daily_token_budget ({self._daily_token_budget}) would be exceeded"
        cap = self._memory_daily_token_budget
        if (
            definition.layer == "memory"
            and cap is not None
            and self._layer_tokens(day, "memory") + per_run > cap
        ):
            return f"daily_token_budget ({cap}) would be exceeded"
        cap = self._agent_daily_token_budget
        if definition.layer == "agent" and self._layer_tokens(day, "agent") + per_run > cap:
            return f"agent_daily_token_budget ({cap}) would be exceeded"
        return None

    def _pause_for_budget(self, job: _Job, reason: str, now: float) -> None:
        job_id = job.definition.job_id
        if job.state == "active":
            self._record_safely(
                "job_paused", {"job_id": job_id, "by": "system", "reason": f"budget: {reason}"}, now
            )
        self._notify(
            "budget_exhausted",
            "warning",
            f"job {job_id} stopped: {reason}; a human must resume it",
            now,
            job_id=job_id,
            params=_budget_params(reason),
        )

    def _start_run(
        self, definition: JobDefinition, scheduled: float, key: str, now: float
    ) -> RunContext | None:
        run_id = f"run-{secrets.token_hex(8)}"
        job = self._jobs[definition.job_id]
        day = _day(now)
        try:
            self._record(
                "run_started",
                {
                    "run_id": run_id,
                    "job_id": definition.job_id,
                    "run_key": key,
                    "scheduled_at": scheduled,
                    "layer": definition.layer,
                    "generation": job.generation,
                    # Persisted so crash settlement never depends on a later definition.
                    "reserved_tokens": self._reservation_of(definition.job_id),
                    "charge_day": day,
                },
                now,
            )
        except SchedulerRefused:
            return None
        ctx = RunContext(
            job=definition,
            run_id=run_id,
            run_key=key,
            scheduled_at=scheduled,
            started_at=now,
            scheduler=self,
            generation=job.generation,
            day_ordinal=self._usage.get((definition.job_id, day), {}).get("runs", 1),
            charge_day=day,
        )
        self._active[run_id] = ctx
        return ctx

    # -- execution -------------------------------------------------------------

    def _launch(self, batch: list[RunContext]) -> None:
        def work() -> None:
            try:
                for ctx in batch:
                    self._execute(ctx)
            finally:
                with self._lock:
                    self._live_workers -= 1
                    if self._live_workers == 0:
                        self._idle.notify_all()
                        self._maybe_complete_stop()
                self._wake.set()

        with self._lock:
            self._live_workers += 1
        try:
            if self._dispatch is not None:
                self._dispatch(work)
            else:
                threading.Thread(target=work, name="stira-job", daemon=True).start()
        except BaseException:
            with self._lock:
                self._live_workers -= 1
                for ctx in batch:
                    ctx.cancel("dispatch_failed")
                    self._finish(ctx, RunOutcome("failed", "dispatch_failed"))
                self._idle.notify_all()
            raise

    def _preflight(self, ctx: RunContext, level: str, heartbeat: bool | None = None) -> str | None:
        """Re-check, immediately before the job body runs, everything that was true at
        start: scheduler running, job not paused, breaker NORMAL, heartbeat healthy for a
        model-calling job (ADR 0021 §8), budget."""
        if ctx.cancelled:
            return ctx.cancel_reason or "cancelled"
        if self._state is not SchedulerState.RUNNING:
            return "shutdown" if self._stop_requested else "unavailable"
        if self._recovering:
            return "recovering"
        job = self._jobs.get(ctx.job.job_id)
        if job is None or job.state == "paused":
            return "paused"
        if job.generation != ctx.generation:
            return "job_updated"  # tools, prompt or limits changed after the start
        if level != NORMAL:
            return f"breaker_{level}"
        if self._heartbeat_refusal(ctx.job, heartbeat):
            return "heartbeat"
        with self._cancel_lock:
            backlog = self._cancel_backlog
        if backlog >= self._max_cancel_backlog:
            return "cancel_backlog"
        max_runs = job.definition.budget.max_runs_per_day
        if max_runs is not None and ctx.day_ordinal > max_runs:
            return "budget"
        if self._spends_tokens(ctx.job):
            own = self._reserved.get(ctx.run_id, ("", "", 0))[2]
            # Booked on the run's persisted charge day, not "today": a run queued across
            # midnight is checked against the day that holds its reservation.
            day = ctx.charge_day or _day(ctx.started_at)
            refusal = self._budget_refusal(job.definition, self._now(), extra=own, day=day)
            if refusal is not None:
                return "budget"
        return None

    def _execute(self, ctx: RunContext) -> None:
        level = self._level()  # outside the scheduler lock (engine lock order)
        heartbeat = self._current_heartbeat() if ctx.job.needs_heartbeat else None
        with self._lock:
            refusal = self._preflight(ctx, level, heartbeat)
        if refusal is not None:
            ctx.cancel(refusal)
            self._finish(ctx, RunOutcome("cancelled", refusal))
            return
        token = _IN_JOB.set(ctx.run_id)
        outcome: RunOutcome
        try:
            if ctx.cancelled:
                outcome = RunOutcome("cancelled", ctx.cancel_reason)
            elif ctx.job.layer == "gateway":
                action = self._actions.get(ctx.job.action or "")
                if action is None:
                    outcome = RunOutcome("failed", "action_unavailable")
                else:
                    result = action(ctx)
                    if isinstance(result, RunOutcome):
                        # An action may report its own status and reason (#251). Gateway
                        # actions are model-free: never any tokens (ADR 0008 §6).
                        outcome = RunOutcome(
                            result.status, result.reason, 0, _mask_text(result.summary)
                        )
                    else:
                        outcome = RunOutcome("completed", summary=_mask_text(result))
            elif ctx.job.layer == "memory":
                outcome = self._execute_memory(ctx)
            elif self._agent_executor is None:
                outcome = RunOutcome("failed", "agent_jobs_disabled")
            else:
                outcome = self._agent_executor(ctx)
                if not isinstance(outcome, RunOutcome):
                    outcome = RunOutcome("failed", "invalid_outcome")
        except Exception as error:  # noqa: BLE001 - a job failure is recorded, never raised
            outcome = RunOutcome("failed", type(error).__name__, usage_known=False)
        finally:
            _IN_JOB.reset(token)
        known = outcome.usage_known
        if outcome.status == "awaiting_approval":
            if ctx.waiting_request is not None and not ctx.cancelled:
                self._park(ctx, outcome)
                return
            outcome = RunOutcome("failed", "approval_required", outcome.tokens)
        if ctx.awaiting_approval is not None:
            outcome = RunOutcome("failed", "approval_required", outcome.tokens, usage_known=known)
        elif ctx.cancelled and outcome.status == "completed":
            outcome = RunOutcome(
                "cancelled", ctx.cancel_reason, outcome.tokens, outcome.summary, known
            )
        cap = ctx.job.budget.max_tokens_per_run
        if cap is not None and outcome.tokens > cap and outcome.status != "failed":
            outcome = RunOutcome("failed", "budget_exceeded", outcome.tokens, usage_known=known)
        self._finish(ctx, outcome)

    def _execute_memory(self, ctx: RunContext) -> RunOutcome:
        """A memory job runs in the memory executor (ADR 0018 §2a), never in the tick."""
        if self._memory_executor is None or ctx.job.action not in self._memory_kinds:
            return RunOutcome("failed", "memory_jobs_disabled")
        outcome = self._memory_executor(ctx)
        if not isinstance(outcome, RunOutcome):
            return RunOutcome("failed", "invalid_outcome")
        if ctx.job.action not in MODEL_CALLING_KINDS and outcome.tokens:
            # A model-free kind that reports tokens called a model: never accepted.
            return RunOutcome("failed", "model_call_not_allowed", outcome.tokens)
        return RunOutcome(
            outcome.status,
            outcome.reason,
            outcome.tokens,
            _mask_text(outcome.summary),
            outcome.usage_known,
        )

    def _finish(self, ctx: RunContext, outcome: RunOutcome) -> None:
        # ``segment_tokens`` / ``segment_usage_unknown`` are written by ``_park`` (under
        # the lock) and read here without it, as before for ``segment_tokens``: a run's
        # segments never overlap (the continuation starts only after the park, and only
        # one ``_finish`` passes the ``_active`` pop below), so there is no concurrent write.
        if ctx.segment_tokens or ctx.segment_usage_unknown:
            outcome = RunOutcome(
                outcome.status,
                outcome.reason,
                outcome.tokens + ctx.segment_tokens,
                outcome.summary,
                outcome.usage_known and not ctx.segment_usage_unknown,
            )
            ctx.segment_tokens = 0
        if ctx.waiting_request is not None and self._approval_waits is not None:
            self._approval_waits.release(ctx.waiting_request, outcome.reason)
            forget = getattr(self._agent_executor, "forget", None)
            if callable(forget):
                forget(ctx.run_id)
        with self._lock:
            self._resume_on_park.pop(ctx.run_id, None)
            if self._active.pop(ctx.run_id, None) is None:
                return  # already closed
            # Settled: the reservation is replaced by the reported usage, or, when the
            # usage is not known for sure, by max(observed, reservation).
            reserved = self._reserved.get(ctx.run_id, ("", "", 0))[2]
            tokens = max(0, int(outcome.tokens or 0))
            if not outcome.usage_known:
                tokens = max(tokens, reserved)
            now = self._now()
            status = outcome.status if outcome.status in ("completed", "failed") else "cancelled"
            body = {
                "run_id": ctx.run_id,
                "job_id": ctx.job.job_id,
                "reason": _mask_text(
                    (outcome.reason or ctx.cancel_reason)
                    if status == "cancelled"
                    else outcome.reason,
                    120,
                ),
                "tokens": tokens,
                "summary": _mask_text(outcome.summary),
            }
            if status == "cancelled":
                body["by"] = "system" if ctx.cancel_reason != "operator" else "operator"
            self._record_safely(f"run_{status}", body, now)
            job = self._jobs.get(ctx.job.job_id)
            if job is None:
                return
            job_id = ctx.job.job_id
            if outcome.reason == "budget_exceeded":
                self._pause_for_budget(job, "max_tokens_per_run reached during a run", now)
            if status == "failed":
                self._alert(
                    f"job_failing:{job_id}",
                    "job_failing",
                    job.consecutive_failures >= self._failure_alert_after,
                    f"job {job_id} failed {job.consecutive_failures} times in a row",
                    now,
                    job_id=job_id,
                    params={"count": job.consecutive_failures},
                )
            elif status == "completed":
                self._alert(
                    f"job_failing:{job_id}",
                    "job_failing",
                    False,
                    f"job {job_id} succeeded",
                    now,
                    job_id=job_id,
                )

    def _run_notice(self, ctx: RunContext, code: str, message: str, severity: str) -> None:
        code = code if _NOTICE_CODE.fullmatch(str(code)) else "job_notice"
        severity = severity if severity in ("info", "warning", "critical") else "warning"
        with self._lock:
            self._notify(code, severity, message, self._now(), job_id=ctx.job.job_id)

    def _awaiting_approval(self, ctx: RunContext) -> None:
        with self._lock:
            now = self._now()
            self._record_safely(
                "run_awaiting_approval",
                {"run_id": ctx.run_id, "job_id": ctx.job.job_id, **(ctx.awaiting_approval or {})},
                now,
            )
            tool = (ctx.awaiting_approval or {}).get("tool")
            self._notify(
                "approval_required",
                "warning",
                f"job {ctx.job.job_id} needed an approval for {tool}; unattended jobs do not "
                "wait for approvals, the run was stopped",
                now,
                job_id=ctx.job.job_id,
                params={"tool": tool},
            )

    # -- waiting for an approval (ADR 0024 phase 2, ADR 0021 S3) -----------------

    def _waiting_approval(self, ctx: RunContext, tool_name: str, digest: str) -> None:
        with self._lock:
            now = self._now()
            tool = _mask_text(tool_name, 64)
            self._record_safely(
                "run_awaiting_approval",
                {
                    "run_id": ctx.run_id,
                    "job_id": ctx.job.job_id,
                    "tool": tool,
                    "args_hash": digest,
                    "request_id": ctx.waiting_request,
                    "wait": True,
                },
                now,
            )
            self._notify(
                "approval_waiting",
                "warning",
                f"job {ctx.job.job_id} is waiting for an approval of {tool} in the dashboard",
                now,
                job_id=ctx.job.job_id,
                params={"tool": tool},
            )

    def _park(self, ctx: RunContext, outcome: RunOutcome) -> None:
        """The worker is released; the run stays active (overlap) until it is continued
        or ends. Any cancellation while it waits ends it here."""
        with self._lock:
            ctx.segment_tokens += max(0, int(outcome.tokens or 0))
            if not outcome.usage_known:
                ctx.segment_usage_unknown = True
            self._parked.add(ctx.run_id)
            early = self._resume_on_park.pop(ctx.run_id, None)

        def ended() -> None:
            with self._lock:
                if ctx.run_id not in self._parked:
                    return  # continued meanwhile: the segment's worker finishes it
                self._parked.discard(ctx.run_id)
            self._finish(ctx, RunOutcome("cancelled", ctx.cancel_reason))

        ctx.on_cancel(ended)
        if early is not None:
            # The person approved before the run was parked: continue it now, with every
            # check of a resume (a refusal cancels the run; a run ended meanwhile is gone).
            with contextlib.suppress(SchedulerRefused):
                self.resume_waiting_run(early[0], by=early[1])

    def _sweep_waiting(self) -> None:
        """Caller holds the lock. End waiting runs whose request was rejected, expired or
        cancelled, or whose store cannot be read (fail closed)."""
        waits = self._approval_waits
        if waits is None:
            return
        for run_id in list(self._parked):
            ctx = self._active.get(run_id)
            if ctx is None or ctx.waiting_request is None:
                self._parked.discard(run_id)
                continue
            try:
                state = waits.state_of(ctx.waiting_request)
            except Exception as error:  # noqa: BLE001 - unreadable: end the run (#601 B14)
                logger.warning(
                    "approval state of a waiting run unreadable (%s); ending it",
                    type(error).__name__,
                )
                state = "unavailable"
            if state in ("pending", "approved"):
                continue
            reason = {"rejected": "approval_rejected", "expired": "approval_expired"}.get(
                state, "approval_cancelled" if state == "cancelled" else "approval_unavailable"
            )
            ctx.cancel(reason)

    def note_approval_rejected(self, request_id: str) -> bool:
        """A person rejected a request in the dashboard: end the run waiting on it now,
        not at the next tick (#556 follow-up). Only the waiting-run sweep runs (it reads the
        store, so only a non-pending state ends a run); nothing is approved or started.
        True when a parked run waited on ``request_id``."""
        if self._approval_waits is None or not isinstance(request_id, str):
            return False
        with self._lock:
            if self._state is not SchedulerState.RUNNING:
                return False
            found = any(
                r in self._active and self._active[r].waiting_request == request_id
                for r in self._parked
            )
            if found:
                self._sweep_waiting()
        if found:
            self._wake.set()
        return found

    def waiting_runs(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {
                    "run_id": r,
                    "job_id": self._active[r].job.job_id,
                    "request_id": self._active[r].waiting_request,
                }
                for r in sorted(self._parked)
                if r in self._active
            ]

    def resume_waiting_run(self, request_id: str, *, by: str) -> dict[str, Any]:
        """Schedule the continuation segment of the run waiting on ``request_id`` (ADR 0024
        §6.2-3), after the dashboard recorded the approval. Every check of a start is
        repeated; a failure ends the run ``cancelled`` and revokes the approval."""
        waits = self._approval_waits
        if waits is None or not isinstance(request_id, str):
            raise SchedulerRefused("not_found", "no run waits for this approval")
        level = self._level()
        heartbeat = None
        with self._lock:
            ctx = next(
                (
                    self._active[r]
                    for r in self._parked
                    if r in self._active and self._active[r].waiting_request == request_id
                ),
                None,
            )
            if ctx is None:
                ending = self._ending_first_segment(request_id)
                if ending is None:
                    raise SchedulerRefused("not_found", "no run waits for this approval")
                # Its request is stored (the dashboard lists it) but the run's first
                # segment has not parked yet: the approval is not lost, the run continues
                # as soon as it parks (``_park``), after the same checks.
                if waits.state_of(request_id) != "approved":
                    ending.cancel("approval_unavailable")
                    return {
                        "run_id": ending.run_id,
                        "resumed": False,
                        "reason": "approval_unavailable",
                    }
                self._resume_on_park[ending.run_id] = (request_id, by)
                return {"run_id": ending.run_id, "resumed": True, "deferred": True}
            if ctx.job.needs_heartbeat:
                heartbeat = self._heartbeat_healthy
            state = waits.state_of(request_id)
            refusal = self._preflight(ctx, level, heartbeat) if state == "approved" else None
            if state != "approved":
                refusal = "approval_unavailable"
            if refusal is not None:
                ctx.cancel(refusal)
                return {"run_id": ctx.run_id, "resumed": False, "reason": refusal}
            self._parked.discard(ctx.run_id)
            ctx.continued = True
            self._record_safely(
                "run_resumed",
                {
                    "run_id": ctx.run_id,
                    "job_id": ctx.job.job_id,
                    "request_id": request_id,
                    "by": audit_principal(by),
                },
                self._now(),
            )
        self._launch_segment(ctx, request_id)
        return {"run_id": ctx.run_id, "resumed": True}

    def _ending_first_segment(self, request_id: str) -> RunContext | None:
        """Caller holds the lock. The active run whose first segment stored ``request_id``
        (``JobApprovalWaits.park``) and has not parked yet (it is ending to wait on it);
        never a run already continued (its continuation is running)."""
        get = getattr(self._approval_waits, "get", None)
        paused = get(request_id) if callable(get) else None
        run_id = getattr(paused, "run_id", None)
        ctx = self._active.get(run_id) if isinstance(run_id, str) else None
        return None if ctx is None or ctx.continued else ctx

    def _launch_segment(self, ctx: RunContext, request_id: str) -> None:
        def work() -> None:
            try:
                self._execute_segment(ctx, request_id)
            finally:
                with self._lock:
                    self._live_workers -= 1
                    if self._live_workers == 0:
                        self._idle.notify_all()
                        self._maybe_complete_stop()
                self._wake.set()

        with self._lock:
            self._live_workers += 1
        try:
            if self._dispatch is not None:
                self._dispatch(work)
            else:
                threading.Thread(target=work, name="stira-job-resume", daemon=True).start()
        except BaseException:
            with self._lock:
                self._live_workers -= 1
            self._finish(ctx, RunOutcome("failed", "dispatch_failed"))
            raise

    def _execute_segment(self, ctx: RunContext, request_id: str) -> None:
        # #601 B16: what was checked when the approval was resumed is checked again right
        # before the continuation runs (a cancel, stop or breaker change meanwhile).
        level = self._level()  # outside the scheduler lock (engine lock order)
        heartbeat = self._current_heartbeat() if ctx.job.needs_heartbeat else None
        with self._lock:
            refusal = self._preflight(ctx, level, heartbeat)
        if refusal is not None:
            ctx.cancel(refusal)
            self._finish(ctx, RunOutcome("cancelled", refusal))
            return
        token = _IN_JOB.set(ctx.run_id)
        try:
            resume = getattr(self._agent_executor, "resume", None)
            outcome = (
                resume(ctx, request_id)
                if callable(resume)
                else RunOutcome("cancelled", "approval_unavailable")
            )
            if not isinstance(outcome, RunOutcome) or outcome.status == "awaiting_approval":
                outcome = RunOutcome("failed", "invalid_outcome")
        except Exception as error:  # noqa: BLE001 - recorded, never raised
            outcome = RunOutcome("failed", type(error).__name__, usage_known=False)
        finally:
            _IN_JOB.reset(token)
        known = outcome.usage_known  # kept through every rewrite below (#601 B17)
        if ctx.awaiting_approval is not None:
            outcome = RunOutcome("failed", "approval_required", outcome.tokens, usage_known=known)
        elif ctx.cancelled and outcome.status == "completed":
            outcome = RunOutcome(
                "cancelled", ctx.cancel_reason, outcome.tokens, outcome.summary, known
            )
        cap = ctx.job.budget.max_tokens_per_run
        total = outcome.tokens + ctx.segment_tokens
        if cap is not None and total > cap and outcome.status != "failed":
            outcome = RunOutcome("failed", "budget_exceeded", outcome.tokens, usage_known=known)
        self._finish(ctx, outcome)

    # -- operations ------------------------------------------------------------

    def _require_open(self) -> None:
        if self._state is not SchedulerState.RUNNING:
            raise SchedulerRefused("unavailable", "the scheduler is not running")

    def _require_widening(self, by: str, confirm: bool) -> str:
        from stiracore.core.approval_store import is_human_principal

        if _IN_JOB.get() is not None:
            raise SchedulerRefused("scope", "a job cannot change the schedule")
        self._require_open()
        principal = audit_principal(by)
        if not is_human_principal(principal) or principal == "local:unknown":
            raise SchedulerRefused("scope", "only a human operator can change the schedule")
        if confirm is not True:
            raise SchedulerRefused("confirmation_required", "operator confirmation required")
        level = self._level()
        if level != NORMAL:
            raise SchedulerRefused(
                "breaker", f"schedule changes are refused while the breaker is {level}"
            )
        return principal

    def _check_action(self, definition: JobDefinition) -> None:
        if definition.unattended_approval == "wait" and self._approval_waits is None:
            # ADR 0024 §4: wait exists only where a durable approval store is wired.
            raise SchedulerRefused("scope", "unattended_approval wait is not available here")
        check = self._action_checks.get(definition.action or "")
        if check is None:
            return
        try:
            reason = check()
        except Exception as error:  # noqa: BLE001 - fail closed, class name only
            reason = f"check_{type(error).__name__}"
        if reason is not None:
            raise SchedulerRefused("scope", f"{definition.action}: {_mask_text(reason, 80)}")

    def create_job(self, data: Any, *, by: str, confirm: bool = False) -> dict[str, Any]:
        with self._lock:
            principal = self._require_widening(by, confirm)
            now = self._now()
            try:
                job = parse_job(
                    data,
                    now=now,
                    gateway_actions=frozenset(self._actions),
                    agent_jobs=self.agent_jobs_enabled,
                    memory_kinds=self._memory_kinds,
                )
            except JobError as error:
                raise SchedulerRefused(error.code, str(error)) from None
            if job.job_id in self._jobs:
                raise SchedulerRefused("conflict", "a job with this id exists")
            self._check_action(job)
            self._record(
                "job_created", {"job_id": job.job_id, "job": job.to_dict(), "by": principal}, now
            )
            self._wake.set()
            return self._job_view(self._jobs[job.job_id], now)

    def update_job(self, job_id: str, data: Any, *, by: str, confirm: bool = False) -> dict:
        with self._lock:
            principal = self._require_widening(by, confirm)
            job = self._get(job_id)
            if job.state == "finished":
                raise SchedulerRefused("conflict", "the job has finished")
            now = self._now()
            try:
                updated = parse_job(
                    data,
                    now=now,
                    gateway_actions=frozenset(self._actions),
                    agent_jobs=self.agent_jobs_enabled,
                    existing=job.definition,
                    memory_kinds=self._memory_kinds,
                )
            except JobError as error:
                raise SchedulerRefused(error.code, str(error)) from None
            self._check_action(updated)
            self._record(
                "job_updated", {"job_id": job_id, "job": updated.to_dict(), "by": principal}, now
            )
            # Runs started under the previous definition stop (queued ones never start).
            for ctx in list(self._active.values()):
                if ctx.job.job_id == job_id and ctx.generation != job.generation:
                    ctx.cancel("job_updated")
            self._wake.set()
            return self._job_view(job, now)

    def resume_job(self, job_id: str, *, by: str, confirm: bool = False) -> dict[str, Any]:
        with self._lock:
            principal = self._require_widening(by, confirm)
            job = self._get(job_id)
            if job.state != "paused":
                raise SchedulerRefused("conflict", f"the job is {job.state}")
            if self._recovering:
                raise SchedulerRefused("unavailable", "log recovery is not complete")
            if job.definition.layer == "memory" and job.definition.action not in self._memory_kinds:
                raise SchedulerRefused("scope", "this memory job kind is not enabled here")
            if job.definition.layer == "agent" and not self.agent_jobs_enabled:
                raise SchedulerRefused("scope", "agent jobs are not enabled on this server")
            self._check_action(job.definition)
            now = self._now()
            self._record("job_resumed", {"job_id": job_id, "by": principal}, now)
            self._wake.set()
            return self._job_view(job, now)

    def pause_job(self, job_id: str, *, by: str, reason: str | None = None) -> dict[str, Any]:
        """Always allowed (any state or breaker level, inside a job too): it only narrows.

        The pause takes effect in memory first; recording it is best effort (a log that
        cannot be written already stops the scheduler, and a restart recovers or refuses).
        """
        with self._lock:
            job = self._get(job_id)
            now = self._now()
            if job.state == "active":
                principal, text = audit_principal(by), _mask_text(reason, 200)
                job.state, job.paused_by, job.paused_reason = "paused", principal, text
                self._held.pop(job_id, None)
                self._record_safely(
                    "job_paused", {"job_id": job_id, "by": principal, "reason": text}, now
                )
                for run_id in list(self._parked):
                    ctx = self._active.get(run_id)
                    if ctx is not None and ctx.job.job_id == job_id:
                        ctx.cancel("paused")  # ADR 0024 §6.5: a pause ends a waiting run
            return self._job_view(job, now)

    def cancel_run(self, run_id: str, *, by: str) -> dict[str, Any]:
        """Always allowed (any state). The run ends as ``cancelled``; nothing is retried."""
        with self._lock:
            ctx = self._active.get(run_id) if isinstance(run_id, str) else None
            if ctx is None:
                if run_id in self._runs:
                    raise SchedulerRefused("conflict", "the run is not active")
                raise SchedulerRefused("not_found", "no such run")
            # Cancel first: a failure to record the request must not leave it running.
            ctx.cancel("operator")
            self._record_safely(
                "run_cancel_requested",
                {"run_id": run_id, "job_id": ctx.job.job_id, "by": audit_principal(by)},
                self._now(),
            )
        return {"run_id": run_id, "cancel_requested": True}

    def _get(self, job_id: str) -> _Job:
        job = self._jobs.get(job_id) if isinstance(job_id, str) else None
        if job is None:
            raise SchedulerRefused("not_found", "no such job")
        return job

    # -- read-only views --------------------------------------------------------

    def _job_view(self, job: _Job, now: float) -> dict[str, Any]:
        definition = job.definition
        next_at = None
        if job.state == "active":
            next_at = definition.schedule.next_after(max(job.cursor, now))
        usage = self._usage.get((definition.job_id, _day(now)), {})
        held = self._held.get(definition.job_id)
        return {
            **definition.to_dict(),
            "state": job.state,
            "created_at": job.created_at,
            "created_by": job.created_by,
            "updated_at": job.updated_at,
            "paused_by": job.paused_by,
            "paused_reason": job.paused_reason,
            "next_run_at": next_at,
            "held_since": held[0] if held else None,
            "last_run_id": job.last_run_id,
            "consecutive_failures": job.consecutive_failures,
            "usage_today": {"runs": usage.get("runs", 0), "tokens": usage.get("tokens", 0)},
        }

    def exclusive(self):
        """The scheduler's own (re-entrant) lock, for a caller that must read a job and
        change it as one step (ADR 0012 §8b: the confirmed target, then the change). Held
        only for in-memory checks and the log append the change makes anyway."""
        return self._lock

    def jobs(self) -> list[dict[str, Any]]:
        with self._lock:
            now = self._now()
            return [self._job_view(self._jobs[j], now) for j in sorted(self._jobs)]

    def runs(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in list(self._runs.values())[-max(0, int(limit)) :]][::-1]

    def notifications(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(n) for n in list(self._notifications)[-max(0, int(limit)) :]][::-1]

    def status(self) -> dict[str, Any]:
        with self._lock:
            return {
                "enabled": True,
                "state": self._state.value,
                "running": self._state is SchedulerState.RUNNING,
                "error": "log_unavailable" if self._error else None,
                "last_tick_at": self._last_tick_at,
                "breaker_level": self._last_level,
                "jobs": len(self._jobs),
                "active_jobs": sum(1 for j in self._jobs.values() if j.state == "active"),
                "active_runs": len(self._active),
                "cancel_workers": len(self._cancel_workers),
                "cancel_backlog": self._cancel_backlog,
                "alerts": sorted(self._alerts),
                "agent_jobs": self.agent_jobs_enabled,
                # ADR 0018: the memory job kinds a person may create here (empty = none).
                "memory_job_kinds": sorted(self._memory_kinds),
                "actions": sorted(self._actions),
                "read_only_tools": sorted(JOB_READ_ONLY_TOOLS),
                "daily_token_budget": self._daily_token_budget,
                "tokens_today": self._global_tokens.get(_day(self._now()), 0),
                # ADR 0021 §4 / §8: the Agent-layer pool and the heartbeat the model-calling
                # jobs need (``None`` = no heartbeat wired to this scheduler).
                "agent_daily_token_budget": self._agent_daily_token_budget,
                "heartbeat_healthy": self._heartbeat_healthy,
            }
