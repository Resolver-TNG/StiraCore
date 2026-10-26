"""The request × window × latch state machine of ADR 0012 §6b — pure, no I/O.

Every function takes a :class:`Machine` and returns a new one (or raises
:class:`Refused` with a reason code); nothing reads a clock, a file or the network.
The caller (phase 2+) does the I/O between the steps and feeds the results in
(``issue_commit``'s arguments, ``close_done``'s ``durable``). Nothing in this module
opens a window on its own: ``issue_commit`` is the only path to ``active`` and needs
a host-confirmed request that went through ``confirm_begin``.

The fence (§6b) is one monotonic counter for the request and the window: every stop,
``/new``, cancel, denial, request expiry and window end increments it. A breaker pause
moves the window generation and the authority epoch, never the fence. A step that
started under fence ``f`` and epoch ``e`` commits only if both are unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from .allowance import TIERS, tier_rank

__all__ = [
    "ABNORMAL_END_REASONS",
    "NORMAL_END_REASONS",
    "PAUSE_REASONS",
    "REQUEST_STATES",
    "WINDOW_STATES",
    "Machine",
    "Refused",
    "Request",
    "Window",
    "advance_epoch",
    "cancel",
    "close_done",
    "confirm_begin",
    "deny",
    "end_class",
    "end_window",
    "initial",
    "issue_commit",
    "issue_failed",
    "new_session",
    "pause",
    "press",
    "request_timeout",
    "resume",
    "stop",
    "unlatch",
]

REQUEST_STATES = (
    "pending",
    "issuing",
    "opened",
    "denied",
    "expired",
    "cancelled",
    "fenced",
    "aborted",
)
_CURRENT_REQUEST = frozenset({"pending", "issuing"})
WINDOW_STATES = ("active", "paused", "ended")

# §6 end classes. Only a normal end can close the marker; everything else latches.
NORMAL_END_REASONS = frozenset(
    {
        "expired_time",
        "expired_sleep",
        "expired_cap",
        "revoked_stop",
        "revoked_new_session",
        "revoked_flapping",
        "issue_aborted_stop",
        "issue_aborted_new_session",
        "issue_aborted_cancel",
        "issue_aborted_epoch",
    }
)
ABNORMAL_END_REASONS = frozenset(
    {"revoked_audit_failed", "revoked_clock_anomaly", "aborted_issue_failed"}
)
PAUSE_REASONS = frozenset(
    {"breaker_warning", "breaker_coherence", "breaker_critical", "breaker_unknown"}
)
# Fence reasons of a request (§6b) and the matching window end of an aborted issuance.
_FENCE_TO_ABORT = {
    "stop": "issue_aborted_stop",
    "new_session": "issue_aborted_new_session",
    "cancel": "issue_aborted_cancel",
    "epoch": "issue_aborted_epoch",
}


def end_class(reason: str) -> str:
    """``normal`` only for a reason in the normal list; any other reason is abnormal."""
    return "normal" if reason in NORMAL_END_REASONS else "abnormal"


class Refused(Exception):
    """A transition that is not allowed from the current state; ``code`` says why."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Request:
    request_id: str
    tier: str
    window_id: str  # allocated at the press (§11 schema)
    state: str = "pending"
    reason: str | None = None  # fence / end reason of a terminal request
    fence_seen: int | None = None  # read at confirm_begin (§6a (1))
    epoch_seen: int | None = None
    # The issuance outcome once applied (commit or failure): a repeated or late commit /
    # failure report for the same request is refused.
    issue_outcome: str | None = None
    # Phase 7 (§9 option C): the person chose "Include exec" at the press (tier external
    # only). Carried into the window; never added later (no route changes it).
    with_exec: bool = False

    @property
    def current(self) -> bool:
        return self.state in _CURRENT_REQUEST


@dataclass(frozen=True)
class Window:
    window_id: str
    tier: str
    state: str  # active | paused | ended
    generation: int
    end_reason: str | None = None
    pause_reason: str | None = None
    with_exec: bool = False  # phase 7: exec-class Ask may be answered (§9 option C)

    @property
    def open(self) -> bool:
        return self.state in ("active", "paused")


@dataclass(frozen=True)
class Machine:
    allowance: str = "off"
    request: Request | None = None
    window: Window | None = None
    fence: int = 0
    epoch: int = 0
    latched: bool = False
    latch_reason: str | None = None
    # Every request and window id this process has seen: an id is never reused, so a
    # delayed confirmation or resume for an old request / window can never match a new one.
    seen_ids: frozenset = frozenset()


def initial(allowance: str = "off", *, latched: bool = False, latch_reason: str | None = None):
    """The state at process start: no request, no window (nothing survives a restart)."""
    tier_rank(allowance)
    return Machine(
        allowance=allowance,
        latched=latched,
        latch_reason=latch_reason if latched else None,
    )


def _fence(machine: Machine, **changes) -> Machine:
    return replace(machine, fence=machine.fence + 1, **changes)


def _current_request(machine: Machine, request_id: str | None) -> Request:
    request = machine.request
    if request is None or not request.current:
        raise Refused("yolo_request_stale")
    if request_id is not None and request.request_id != request_id:
        raise Refused("yolo_request_stale")
    return request


# --- press and the request ------------------------------------------------------------


def press(
    machine: Machine, *, request_id: str, window_id: str, tier: str, with_exec: bool = False
) -> Machine:
    """Row 1: no request, no window, latch clear → request ``pending``.

    ``with_exec`` (phase 7): exactly ``True`` / ``False``; ``True`` only with tier
    ``external`` (the caller checks that the exec choice is offered at all)."""
    if machine.allowance == "off":
        raise Refused("yolo_not_allowed")
    if tier not in TIERS[1:] or tier_rank(tier) > tier_rank(machine.allowance):
        raise Refused("yolo_tier_not_allowed")
    if type(with_exec) is not bool or (with_exec and tier != "external"):
        raise Refused("yolo_exec_not_allowed")
    if machine.latched:
        raise Refused("yolo_latched")
    if machine.window is not None:
        # An open window (no extension, §1a 5.) or one whose close sequence is not done.
        raise Refused("yolo_active" if machine.window.open else "yolo_closing")
    if machine.request is not None and machine.request.current:
        raise Refused("yolo_request_pending")
    previous = machine.request
    if previous is not None and previous.fence_seen is not None and not previous.issue_outcome:
        # A fenced issuance whose outcome (commit / failure) has not been applied yet.
        raise Refused("yolo_closing")
    if not request_id or not window_id or request_id == window_id:
        raise Refused("yolo_request_invalid")
    if request_id in machine.seen_ids or window_id in machine.seen_ids:
        raise Refused("yolo_request_invalid")
    return replace(
        machine,
        request=Request(request_id, tier, window_id, with_exec=with_exec),
        seen_ids=machine.seen_ids | {request_id, window_id},
    )


def confirm_begin(machine: Machine, request_id: str) -> Machine:
    """Row 2: the host confirmation's CAS ``pending → issuing``; records fence and epoch."""
    request = machine.request
    if request is None or request.request_id != request_id or request.state != "pending":
        raise Refused("yolo_request_stale")
    if machine.latched:
        raise Refused("yolo_latched")
    issuing = replace(request, state="issuing", fence_seen=machine.fence, epoch_seen=machine.epoch)
    return replace(machine, request=issuing)


def cancel(machine: Machine, request_id: str | None = None) -> Machine:
    """Rows 3 / 6: cancel of a pending (``cancelled``) or issuing (``fenced(cancel)``) request."""
    request = _current_request(machine, request_id)
    if request.state == "pending":
        return _fence(machine, request=replace(request, state="cancelled", reason="cancel"))
    return _fence(machine, request=replace(request, state="fenced", reason="cancel"))


def deny(machine: Machine, request_id: str) -> Machine:
    """Row 4: a denial at the host → ``denied``."""
    request = _current_request(machine, request_id)
    if request.state != "pending":
        raise Refused("yolo_request_stale")
    return _fence(machine, request=replace(request, state="denied", reason="denied"))


def request_timeout(machine: Machine, request_id: str) -> Machine:
    """Row 4: the 2 minutes passed without a confirmation → ``expired``."""
    request = _current_request(machine, request_id)
    if request.state != "pending":
        raise Refused("yolo_request_stale")
    return _fence(machine, request=replace(request, state="expired", reason="request_expired"))


def advance_epoch(machine: Machine) -> Machine:
    """Row 7: the authority epoch moved (breaker change, rules save, …).

    An ``issuing`` request is fenced (``fenced(epoch)``); a pending one is not (it reads
    the epoch at its confirmation).
    """
    moved = replace(machine, epoch=machine.epoch + 1)
    request = moved.request
    if request is not None and request.state == "issuing":
        return _fence(moved, request=replace(request, state="fenced", reason="epoch"))
    return moved


def _fence_everything(machine: Machine, reason: str) -> Machine:
    """Stop and ``/new`` act on everything current; never refused, name no id (§6)."""
    request = machine.request
    if request is not None and request.current:
        request = replace(request, state="fenced", reason=reason)
    window = machine.window
    moved_epoch = False
    if window is not None and window.open:
        window = replace(
            window,
            state="ended",
            end_reason=f"revoked_{reason}",
            generation=window.generation + 1,
        )
        moved_epoch = True
    return replace(
        machine,
        request=request,
        window=window,
        fence=machine.fence + 1,
        epoch=machine.epoch + (1 if moved_epoch else 0),
    )


def stop(machine: Machine) -> Machine:
    return _fence_everything(machine, "stop")


def new_session(machine: Machine) -> Machine:
    return _fence_everything(machine, "new_session")


# --- issuance -------------------------------------------------------------------------


def _issuance_request(machine: Machine, request_id: str) -> Request:
    """The request of a running issuance: ``issuing``, or fenced after ``confirm_begin``
    (``fence_seen`` recorded), whose outcome was not applied yet."""
    request = machine.request
    if (
        request is None
        or request.request_id != request_id
        or request.issue_outcome is not None
        or request.fence_seen is None
        or request.state not in ("issuing", "fenced")
    ):
        raise Refused("yolo_request_stale")
    return request


def issue_failed(machine: Machine, request_id: str, *, started_written: bool) -> Machine:
    """Row 8: a write or receipt check failed, the writer's health latched, a clock
    anomaly → ``aborted(issue_failed)``: abnormal, latched (whether or not ``started``
    was written; with it, the ``ended`` row still follows — it does not unlatch).

    Also for an issuance a stop / ``/new`` / cancel / epoch move fenced meanwhile: the
    fence still wins (no window), but the failure is not hidden behind a normal end.
    """
    request = _issuance_request(machine, request_id)
    if request.state == "fenced":
        failed = replace(request, issue_outcome="aborted_issue_failed")
    else:
        failed = replace(
            request, state="aborted", reason="issue_failed", issue_outcome="aborted_issue_failed"
        )
    window = None
    if started_written:
        window = Window(
            request.window_id,
            request.tier,
            "ended",
            1,
            end_reason="aborted_issue_failed",
            with_exec=request.with_exec,
        )
    return _fence(
        machine, request=failed, window=window, latched=True, latch_reason="aborted_issue_failed"
    )


def issue_commit(
    machine: Machine,
    request_id: str,
    window_id: str,
    *,
    started_written: bool,
    receipts_ok: bool,
    writer_healthy: bool,
    incarnation_same: bool,
    before_expiry: bool,
    breaker_normal: bool,
) -> tuple[Machine, str]:
    """§6a (3), the final linearization point. Returns ``(machine, outcome)``.

    ``outcome`` is ``opened`` (window ``active``, or ``paused`` when the breaker is not
    NORMAL), ``issue_aborted_<fence reason>`` (a normal end: stop, ``/new``, cancel or a
    moved epoch since ``confirm_begin``, **and** no audit anomaly) or
    ``aborted_issue_failed`` (abnormal, latched). An audit anomaly — a receipt that did
    not verify, an unhealthy writer, another incarnation, another window id — latches
    also when a fence won the race: the stop wins (nothing opens) and the failure stays
    visible. ``started_written``: whether ``audit.yolo.started`` was written; then a
    failed commit leaves an ended window that needs its close sequence (``close_done``).
    Each issuance is settled once: a repeated or late commit is refused.
    """
    request = _issuance_request(machine, request_id)
    anomaly = not (
        receipts_ok and writer_healthy and incarnation_same and window_id == request.window_id
    )
    if anomaly:
        return issue_failed(machine, request_id, started_written=started_written), (
            "aborted_issue_failed"
        )
    if request.state == "fenced":
        reason = _FENCE_TO_ABORT.get(request.reason)
        if reason is None:  # not a fence reason the table knows: fail closed
            return issue_failed(machine, request_id, started_written=started_written), (
                "aborted_issue_failed"
            )
        settled = replace(machine, request=replace(request, issue_outcome=reason))
        if not started_written:
            return settled, reason
        window = Window(
            request.window_id,
            request.tier,
            "ended",
            1,
            end_reason=reason,
            with_exec=request.with_exec,
        )
        return replace(settled, window=window), reason
    fence_moved = request.fence_seen != machine.fence or request.epoch_seen != machine.epoch
    ok = (
        not fence_moved  # cannot happen without a fence row; fail closed anyway
        and started_written
        and before_expiry
        and machine.window is None
        and not machine.latched
    )
    if not ok:
        return issue_failed(machine, request_id, started_written=started_written), (
            "aborted_issue_failed"
        )
    state = "active" if breaker_normal else "paused"
    window = Window(
        request.window_id,
        request.tier,
        state,
        1,
        pause_reason=None if breaker_normal else "breaker_unknown",
        with_exec=request.with_exec,
    )
    opened = replace(request, state="opened", issue_outcome="opened")
    return replace(machine, request=opened, window=window), "opened"


# --- the window -----------------------------------------------------------------------


def _open_window(machine: Machine) -> Window:
    window = machine.window
    if window is None or not window.open:
        raise Refused("yolo_no_window")
    return window


def pause(machine: Machine, reason: str) -> Machine:
    """``active → paused(reason)``: a narrowing; generation and epoch move, fence does not.

    Also on an already paused window: every non-NORMAL sample is a new pause (the dwell
    restarts, §6), so a resume prepared for the earlier pause no longer matches.
    """
    if reason not in PAUSE_REASONS:
        raise Refused("yolo_reason_unknown")
    window = _open_window(machine)
    paused = replace(window, state="paused", pause_reason=reason, generation=window.generation + 1)
    return replace(machine, window=paused, epoch=machine.epoch + 1)


def resume(
    machine: Machine,
    window_id: str,
    generation_seen: int,
    *,
    breaker_normal: bool,
    before_expiry: bool,
) -> Machine:
    """CAS ``paused(window_id, g) → active(g+1)`` (§6 ``breaker_resume``).

    Bound to the window id (an old window's resume never matches a new window: ids are
    never reused) and to the generation of the pause it was prepared for (any later pause,
    stop or end moved it). The live breaker level, read again at the CAS, must be NORMAL,
    and the window must not have expired (the caller ends it then; expiry first).
    """
    window = machine.window
    if (
        window is None
        or window.window_id != window_id
        or window.state != "paused"
        or window.generation != generation_seen
    ):
        raise Refused("yolo_resume_stale")
    if not before_expiry:
        raise Refused("yolo_expired")
    if not breaker_normal:
        raise Refused("yolo_breaker_not_normal")
    active = replace(window, state="active", pause_reason=None, generation=window.generation + 1)
    return replace(machine, window=active)


def end_window(machine: Machine, reason: str) -> Machine:
    """A narrowing end of an open window (§6): never refused for a known reason."""
    window = _open_window(machine)
    if reason not in NORMAL_END_REASONS and reason not in ABNORMAL_END_REASONS:
        reason_used = "unknown"  # not a known reason: abnormal (fail closed)
    else:
        reason_used = reason
    ended = replace(window, state="ended", end_reason=reason_used, generation=window.generation + 1)
    return _fence(machine, window=ended, epoch=machine.epoch + 1)


def close_done(machine: Machine, *, durable: bool) -> Machine:
    """The close sequence of an ended window finished (§11).

    Normal end and durable → **no window** (a new press is accepted from here). Abnormal
    end, or a close sequence that was not durable → **latched** until ``unlatch``.
    """
    window = machine.window
    if window is None or window.open:
        raise Refused("yolo_no_ended_window")
    if durable and end_class(window.end_reason or "") == "normal":
        return replace(machine, window=None)
    reason = window.end_reason if end_class(window.end_reason or "") == "abnormal" else "close"
    return replace(machine, latched=True, latch_reason=reason or "unknown")


def unlatch(machine: Machine) -> Machine:
    """A person's unlatch (§11, host command): clears the latch; never opens a window."""
    if not machine.latched:
        raise Refused("yolo_not_latched")
    if machine.request is not None and machine.request.current:
        raise Refused("yolo_request_pending")
    window = machine.window
    if window is not None and window.open:
        raise Refused("yolo_active")
    return replace(machine, latched=False, latch_reason=None, window=None)
