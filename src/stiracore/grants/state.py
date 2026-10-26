"""The goal × grant state machine as pure functions (ADR 0011 §8, §11; W4 of #296).

Every function takes a :class:`~.schema.Grant` and returns a new one (or raises
:class:`~.schema.GrantRefused` with a fixed code). Nothing here reads a clock, a file or a
lock: the service passes the clock reading and the breaker level in. Each rule is pinned
row by row by ``tests/test_grants_state.py``.

**Grant states** (``status`` + ``reason``, fixed codes):

* ``active``;
* ``paused(user | goal_paused | goal_blocked | goal_budget_limited)``;
* ``halted(breaker_warning | breaker_critical)`` — v1 emotional stop = the breaker only
  (WARNING / COHERENCE → ``breaker_warning``; CRITICAL or an unknown level →
  ``breaker_critical``; operator decision 2026-10-24);
* terminal, **final**: ``revoked(user | kill_switch | restart | session_reset |
  clock_anomaly | unhealthy)``, ``expired(ttl)``, ``ended(complete)``.

**Goal states**: ``active``, ``paused(user)``, ``blocked(no_progress | loop_detected |
max_iterations | same_blocker | approval_timeout | verification)``, ``budget_limited``,
``awaiting_verification``, ``complete(verified)``, ``unmet(<the grant's end code>)``.

Rules:

* a goal that is not active (``paused`` / ``blocked`` / ``budget_limited``) pauses its grant:
  ``paused(goal_<status>)`` (:func:`couple`); ``awaiting_verification`` does not (only the
  verifier may act, and the user verifies while the grant is ``active``);
* a grant pause never overwrites the goal's ``blocked`` / ``budget_limited``: the two
  states are separate fields; the headline is derived (:func:`headline`) and never
  written back;
* terminal grant states are final: no later event changes the grant's or the goal's state.
  Two things are still recorded on an ended record: the first verification after the end
  (``late_verification`` ``pass`` / ``fail``; later ones change nothing) and tokens spent
  (:func:`charge` keeps counting, never reopening anything);
* ``complete`` only when the user confirmed while the grant was ``active`` and the goal
  ``awaiting_verification``; an agent claim (:func:`claim`) only moves the goal to
  ``awaiting_verification``;
* a resume returns to the state before the stop (``resume_status`` / ``resume_reason``); a
  halt resume needs the breaker ``normal`` and sets ``recheck`` (the next W5 tick checks
  again before anything runs);
* the budget accumulates per grant and never resets (:func:`charge`); the TTL never
  extends (no function changes ``ttl_sec`` or the issue clocks);
* clocks (:func:`clock_check`): wall + sleep-including (``boot``) + sleep-excluding
  (``mono``) readings. Elapsed = the largest of the three deltas (a sleep counts). The wall
  clock going back since the last check, or wall and boot deltas since issue disagreeing
  by more than :data:`CLOCK_SLACK_SEC` (a forward or backward jump) → ``revoked
  (clock_anomaly)``; elapsed ≥ TTL → ``expired(ttl)``. Where the host has no separate
  boot clock (it then equals ``mono``), a sleep looks like a jump and revokes (fail
  closed).
"""

from __future__ import annotations

import math
from dataclasses import replace

from stiracore.authority import AuthorityError, GrantRecord
from stiracore.yolo.allowance import ClockReading

from .schema import (
    CLAIM_CHECKS,
    CLAIM_PROGRESS,
    CLAIM_ROWS_KEPT,
    CLAIM_WORKSPACE,
    KIND,
    ClaimEvidence,
    Grant,
    GrantRefused,
    IssueRequest,
    evidence_consistent,
)

__all__ = [
    "AGENT_BLOCKED_CODES",
    "CLOCK_SLACK_SEC",
    "GOAL_INACTIVE",
    "GOAL_REASONS",
    "GOAL_STATUSES",
    "GRANT_REASONS",
    "GRANT_STATUSES",
    "TERMINAL",
    "all_reason_codes",
    "authority_record",
    "block_goal",
    "charge",
    "claim",
    "clock_check",
    "count_iteration",
    "couple",
    "elapsed",
    "expire",
    "halt",
    "halt_reason_for",
    "headline",
    "is_live",
    "is_terminal",
    "measure_claim",
    "new_grant",
    "pause_goal",
    "pause_user",
    "restart",
    "resume",
    "revoke",
    "satisfy_recheck",
    "transitions",
    "ttl_left",
    "verify",
]

GRANT_STATUSES = ("active", "paused", "halted", "revoked", "expired", "ended")
TERMINAL = frozenset({"revoked", "expired", "ended"})
GRANT_REASONS = {
    "active": frozenset({""}),
    "paused": frozenset({"user", "goal_paused", "goal_blocked", "goal_budget_limited"}),
    "halted": frozenset({"breaker_warning", "breaker_critical"}),
    "revoked": frozenset(
        {"user", "kill_switch", "restart", "session_reset", "clock_anomaly", "unhealthy"}
    ),
    "expired": frozenset({"ttl"}),
    "ended": frozenset({"complete"}),
}
GOAL_STATUSES = (
    "active",
    "paused",
    "blocked",
    "budget_limited",
    "awaiting_verification",
    "complete",
    "unmet",
)
GOAL_INACTIVE = frozenset({"paused", "blocked", "budget_limited"})
# W5: the blocker codes the agent may report (``goal_report``); a fixed enum, no text.
AGENT_BLOCKED_CODES = ("needs_decision", "out_of_scope", "missing_input")
_END_CODES = frozenset(
    f"{status}_{reason}" for status in TERMINAL - {"ended"} for reason in GRANT_REASONS[status]
)
GOAL_REASONS = {
    "active": frozenset({""}),
    "paused": frozenset({"user"}),
    "blocked": frozenset(
        {
            "no_progress",
            "loop_detected",
            "max_iterations",
            "same_blocker",
            "approval_timeout",
            "verification",
            # W5: the executor's own stops (a code each, never free text).
            "run_checks_unavailable",
            "iteration_timeout",
            "workspace_changed",
            # W5: what the agent may report through ``goal_report(kind="blocked")``.
            *AGENT_BLOCKED_CODES,
        }
    ),
    "budget_limited": frozenset({""}),
    "awaiting_verification": frozenset({""}),
    "complete": frozenset({"verified"}),
    "unmet": _END_CODES,
}
CLOCK_SLACK_SEC = 5.0
_HALT_RANK = {"breaker_warning": 1, "breaker_critical": 2}


def _require_goal(grant: object) -> Grant:
    """The kind discriminator: only a ``kind="goal"`` record is handled here (a YOLO
    window can never be, ADR 0011 addendum §2)."""
    if not isinstance(grant, Grant) or grant.kind != KIND:
        raise AuthorityError("grant_kind_mismatch")
    return grant


def is_terminal(grant: Grant) -> bool:
    return _require_goal(grant).status in TERMINAL


def is_live(grant: Grant) -> bool:
    return not is_terminal(grant)


def authority_record(grant: Grant, incarnation: str) -> GrantRecord:
    """The authority's view of the grant (``kind="goal"``) — what a W5 permit binds."""
    _require_goal(grant)
    return GrantRecord(
        kind=KIND,
        grant_id=grant.grant_id,
        incarnation=incarnation,
        generation=grant.generation,
        state=grant.status,
    )


def _bump(grant: Grant, **changes) -> Grant:
    return replace(grant, generation=grant.generation + 1, **changes)


def new_grant(request: IssueRequest, *, grant_id: str, issuer: str, now: ClockReading) -> Grant:
    """A freshly issued grant: ``active`` with its goal ``active``."""
    return Grant(
        grant_id=grant_id,
        kind=KIND,
        session_ref="grant:" + grant_id,
        tools=tuple(request.tools),
        workspace_subtree=request.workspace_subtree,
        total_tokens=request.total_tokens,
        per_tick_tokens=request.per_tick_tokens,
        ttl_sec=request.ttl_sec,
        max_iterations=request.max_iterations,
        no_progress_ticks=request.no_progress_ticks,
        verifier=request.verifier,
        issuer=issuer,
        issued_wall=float(now.wall),
        issued_boot=float(now.boot),
        issued_mono=float(now.mono),
        last_wall=float(now.wall),
        last_boot=float(now.boot),
        last_mono=float(now.mono),
        generation=1,
        status="active",
        reason="",
        resume_status="",
        resume_reason="",
        goal_status="active",
        goal_reason="",
        goal_resume="",
        used_tokens=0,
        iterations=0,
        no_progress=0,
        stop_epoch=0,
        recheck=False,
        late_verification="",
        ended_wall=0.0,
        heartbeat_start=request.heartbeat_start is True,
    )


# --- coupling, ends -----------------------------------------------------------------------


def couple(grant: Grant) -> Grant:
    """A goal that is not active pauses its grant (``paused(goal_<status>)``); where the
    grant is already stopped, the state it would resume to becomes that pause instead of
    ``active``. Idempotent; terminal grants are untouched."""
    grant = _require_goal(grant)
    if grant.status in TERMINAL or grant.goal_status not in GOAL_INACTIVE:
        return grant
    target = "goal_" + grant.goal_status
    if grant.status == "active":
        return _bump(
            grant, status="paused", reason=target, resume_status="active", resume_reason=""
        )
    if grant.status == "paused" and grant.reason.startswith("goal_"):
        return grant if grant.reason == target else _bump(grant, reason=target)
    if grant.resume_status == "active":
        return _bump(grant, resume_status="paused", resume_reason=target)
    return grant


def _end(grant: Grant, status: str, reason: str, wall: float) -> Grant:
    if grant.goal_status == "complete":
        goal_status, goal_reason = grant.goal_status, grant.goal_reason
    else:
        goal_status, goal_reason = "unmet", f"{status}_{reason}"
    return _bump(
        grant,
        status=status,
        reason=reason,
        resume_status="",
        resume_reason="",
        goal_status=goal_status,
        goal_reason=goal_reason,
        recheck=False,
        ended_wall=float(wall),
    )


def revoke(grant: Grant, reason: str, *, wall: float) -> Grant:
    """→ ``revoked(reason)`` (final). A terminal grant stays as it ended."""
    grant = _require_goal(grant)
    if reason not in GRANT_REASONS["revoked"]:
        raise ValueError("revoke reason")
    if grant.status in TERMINAL:
        return grant
    return _end(grant, "revoked", reason, wall)


def expire(grant: Grant, *, wall: float) -> Grant:
    grant = _require_goal(grant)
    if grant.status in TERMINAL:
        return grant
    return _end(grant, "expired", "ttl", wall)


def restart(grant: Grant, *, wall: float) -> Grant:
    """Every start: a live grant is ``revoked(restart)`` — never revived (ADR 0011 §6)."""
    return revoke(grant, "restart", wall=wall)


# --- stops and resume ---------------------------------------------------------------------


def halt_reason_for(level: object) -> str | None:
    """The halt reason for a breaker level (``None``: NORMAL). Anything unknown is
    critical (fail closed)."""
    name = str(getattr(level, "value", level)).lower()
    if name == "normal":
        return None
    if name in ("warning", "coherence"):
        return "breaker_warning"
    return "breaker_critical"


def halt(grant: Grant, reason: str) -> Grant:
    """→ ``halted(reason)``; remembers the state to resume to. A stronger halt replaces a
    weaker one (WARNING → CRITICAL) and keeps the resume target; terminal: unchanged."""
    grant = _require_goal(grant)
    if reason not in GRANT_REASONS["halted"]:
        raise ValueError("halt reason")
    if grant.status in TERMINAL:
        return grant
    if grant.status == "halted":
        if _HALT_RANK[reason] > _HALT_RANK.get(grant.reason, 0):
            return _bump(grant, reason=reason)
        return grant
    return _bump(
        grant,
        status="halted",
        reason=reason,
        resume_status=grant.status,
        resume_reason=grant.reason,
        recheck=False,
    )


def pause_user(grant: Grant) -> Grant:
    """The user's pause (one press): ``active`` → ``paused(user)``. A grant that is already
    stopped is unchanged (the stronger stop stays); terminal: refused."""
    grant = _require_goal(grant)
    if grant.status in TERMINAL:
        raise GrantRefused("grant_terminal")
    if grant.status != "active":
        return grant
    return _bump(grant, status="paused", reason="user", resume_status="active", resume_reason="")


def resume(grant: Grant, *, breaker_level: object) -> Grant:
    """A person's resume (the service has checked the operation token, and the typed
    confirmation for a halt). Returns to the state before the stop:

    * ``halted(*)``: refused unless the breaker is NORMAL now (``grant_still_dangerous``);
      returns to the remembered state and sets ``recheck``;
    * ``paused(user)``: → ``active``;
    * ``paused(goal_paused)`` / ``paused(goal_blocked)``: goal and grant active together
      (the no-progress streak restarts); ``blocked(max_iterations)`` needs a new grant in
      v1 (``grant_resume_needs_new_grant``);
    * ``paused(goal_budget_limited)``: refused (``grant_budget_exhausted``: the total never
      resets, a new grant is needed);
    * active: ``grant_not_stopped``; terminal: ``grant_terminal``."""
    grant = _require_goal(grant)
    if grant.status in TERMINAL:
        raise GrantRefused("grant_terminal")
    if grant.status == "active":
        raise GrantRefused("grant_not_stopped")
    if grant.status == "halted":
        if halt_reason_for(breaker_level) is not None:
            raise GrantRefused("grant_still_dangerous")
        target = (grant.resume_status or "active", grant.resume_reason)
        resumed = _bump(
            grant,
            status=target[0],
            reason=target[1] if target[0] != "active" else "",
            resume_status="active" if target[0] != "active" else "",
            resume_reason="",
            recheck=True,
        )
        return couple(resumed)
    if grant.reason == "user":
        return couple(_bump(grant, status="active", reason="", resume_status="", resume_reason=""))
    if grant.reason == "goal_budget_limited":
        raise GrantRefused("grant_budget_exhausted")
    if grant.reason == "goal_blocked" and grant.goal_reason == "max_iterations":
        raise GrantRefused("grant_resume_needs_new_grant")
    return _bump(
        grant,
        status="active",
        reason="",
        resume_status="",
        resume_reason="",
        # A goal pause that interrupted awaiting_verification returns to it (ADR §8).
        goal_status=grant.goal_resume or "active",
        goal_reason="",
        goal_resume="",
        no_progress=0,
    )


# --- goal side ----------------------------------------------------------------------------


def claim(grant: Grant, *, now: ClockReading | None = None) -> Grant:
    """The agent's completion claim (W5): goal ``active`` → ``awaiting_verification``, only
    while the grant is ``active``. Never ``complete``.

    W6c: the claim replaces the grant's evidence record with a fresh one taken now — the
    claim instant ``now.wall``, the iteration ``k`` (the one claiming: counted at its end)
    of the maximum, tokens used / budget left and the TTL left at ``now``; the measured
    codes are ``unknown`` until :func:`measure_claim`. No clock reading: refused
    (``grant_clock_unavailable``), nothing changes."""
    grant = _require_goal(grant)
    if grant.status != "active" or grant.goal_status != "active":
        raise GrantRefused("grant_not_active")
    if now is None or not all(_finite(v) for v in (now.wall, now.boot, now.mono)):
        raise GrantRefused("grant_clock_unavailable")
    evidence = ClaimEvidence(
        claimed_at=float(now.wall),
        iteration=min(grant.iterations + 1, grant.max_iterations),
        max_iterations=grant.max_iterations,
        used_tokens=grant.used_tokens,
        budget_left=max(0, grant.total_tokens - grant.used_tokens),
        ttl_left_sec=ttl_left(grant, now),
    )
    return _bump(
        grant, goal_status="awaiting_verification", goal_reason="", claim_evidence=evidence
    )


def measure_claim(
    grant: Grant,
    claimed_at: float,
    *,
    checks: str,
    rows: tuple = (),
    workspace: str,
    progress: str,
) -> Grant:
    """W6c: the claiming iteration's end fills in the measured codes of **its** claim's
    evidence (``claimed_at`` names it) — any other record (no evidence, another claim's)
    is unchanged. Not a state change: no generation, no event; a terminal grant keeps its
    state (the record is audit only, it never revives anything). A code outside its set is
    ``unknown`` (never ``pass``); rows only with ``pass`` / ``fail``, and a ``pass`` whose
    rows did not all pass is ``unknown``."""
    grant = _require_goal(grant)
    current = grant.claim_evidence
    if current is None or type(claimed_at) is not float or current.claimed_at != claimed_at:
        return grant
    rows = tuple(rows) if type(rows) is tuple else ()
    measured = replace(
        current,
        checks=checks if checks in CLAIM_CHECKS else "unknown",
        check_rows=rows[:CLAIM_ROWS_KEPT] if checks in ("pass", "fail") else (),
        workspace=workspace if workspace in CLAIM_WORKSPACE else "unknown",
        progress=progress if progress in CLAIM_PROGRESS else "unknown",
    )
    if not evidence_consistent(measured):
        measured = replace(measured, checks="unknown", check_rows=())
    if not evidence_consistent(measured):
        return grant  # never a record the reader would refuse
    return replace(grant, claim_evidence=measured)


def verify(grant: Grant, decision: str, *, wall: float) -> Grant:
    """The user's verification (``confirm`` / ``reject``).

    * grant terminal: recorded only — ``late_verification`` ``pass`` / ``fail``; the grant
      and the goal keep their states (never revived);
    * goal not ``awaiting_verification``: ``grant_not_awaiting``; grant not ``active``:
      ``grant_not_active``;
    * confirm: goal ``complete(verified)``, grant ``ended(complete)``;
    * reject: goal ``active`` again, counted as a no-progress tick (``no_progress_ticks``
      of them block the goal: ``blocked(no_progress)``)."""
    grant = _require_goal(grant)
    if decision not in ("confirm", "reject"):
        raise GrantRefused("grant_field_invalid")
    if grant.status in TERMINAL:
        if grant.late_verification:
            return grant  # the first late verification stays
        return _bump(grant, late_verification="pass" if decision == "confirm" else "fail")
    if grant.goal_status != "awaiting_verification":
        raise GrantRefused("grant_not_awaiting")
    if grant.status != "active":
        raise GrantRefused("grant_not_active")
    if decision == "confirm":
        done = replace(grant, goal_status="complete", goal_reason="verified")
        return _end(done, "ended", "complete", wall)
    streak = grant.no_progress + 1
    if streak >= grant.no_progress_ticks:
        blocked = _bump(grant, goal_status="blocked", goal_reason="no_progress", no_progress=streak)
        return couple(blocked)
    return _bump(grant, goal_status="active", goal_reason="", no_progress=streak)


def block_goal(grant: Grant, reason: str) -> Grant:
    """The goal → ``blocked(reason)`` (W5/W6 detectors); the grant pauses with it."""
    grant = _require_goal(grant)
    if reason not in GOAL_REASONS["blocked"]:
        raise ValueError("block reason")
    if grant.status in TERMINAL or grant.goal_status not in ("active", "awaiting_verification"):
        return grant
    return couple(_bump(grant, goal_status="blocked", goal_reason=reason))


def pause_goal(grant: Grant) -> Grant:
    """The goal → ``paused(user)``; the grant pauses with it. A pause that interrupts
    ``awaiting_verification`` remembers it (``goal_resume``): the resume returns there."""
    grant = _require_goal(grant)
    if grant.status in TERMINAL or grant.goal_status not in ("active", "awaiting_verification"):
        return grant
    back = "awaiting_verification" if grant.goal_status == "awaiting_verification" else ""
    return couple(_bump(grant, goal_status="paused", goal_reason="user", goal_resume=back))


def satisfy_recheck(grant: Grant) -> Grant:
    """W5: the first iteration after a halt resume has checked the breaker, the budget and
    the TTL again (the executor's admission): ``recheck`` is cleared. Only for an active
    grant; anything else is unchanged."""
    grant = _require_goal(grant)
    if not grant.recheck or grant.status != "active":
        return grant
    return _bump(grant, recheck=False)


def charge(grant: Grant, tokens: int) -> Grant:
    """Add spent tokens (W5). The total accumulates per grant and **never resets** (no
    function lowers ``used_tokens``); at the total the goal is ``budget_limited`` and the
    grant ``paused(goal_budget_limited)``. A terminal grant still counts what it spent."""
    grant = _require_goal(grant)
    if type(tokens) is not int or tokens < 0:
        raise ValueError("tokens")
    used = grant.used_tokens + tokens
    charged = _bump(grant, used_tokens=used)
    if (
        used >= grant.total_tokens
        and grant.status not in TERMINAL
        and grant.goal_status in ("active", "awaiting_verification", "paused", "blocked")
    ):
        charged = replace(charged, goal_status="budget_limited", goal_reason="")
    return couple(charged)


def count_iteration(grant: Grant, *, progress: bool | None) -> Grant:
    """One finished iteration (W5): ``max_iterations`` → ``blocked(max_iterations)``;
    ``no_progress_ticks`` ticks without progress in a row → ``blocked(no_progress)``.

    ``progress`` (W6a) is the executor's outcome verdict (a new keyed fingerprint), never
    activity; ``None``: an interrupted iteration (a person's turn, shutdown, a stop) that
    consumed an effect — it counts toward ``max_iterations`` and leaves the streak as it
    was.

    #643 (W6-F): the count does not depend on what ended the iteration — an iteration that
    consumed an effect counts also when its stop ended the grant (revoke, ``/new``, TTL,
    the kill): on a terminal grant only ``iterations`` moves (no streak, no goal or grant
    state change; a terminal grant stays final)."""
    grant = _require_goal(grant)
    if type(progress) not in (bool, type(None)):
        raise ValueError("progress")
    if grant.status in TERMINAL:
        return _bump(grant, iterations=grant.iterations + 1)
    iterations = grant.iterations + 1
    if progress is None:
        streak = grant.no_progress
    else:
        streak = 0 if progress else grant.no_progress + 1
    counted = _bump(grant, iterations=iterations, no_progress=streak)
    if grant.goal_status != "active":
        return couple(counted)
    if iterations >= grant.max_iterations:
        counted = replace(counted, goal_status="blocked", goal_reason="max_iterations")
    elif progress is not None and streak >= grant.no_progress_ticks:
        counted = replace(counted, goal_status="blocked", goal_reason="no_progress")
    return couple(counted)


# --- clocks -------------------------------------------------------------------------------


def elapsed(grant: Grant, now: ClockReading) -> float:
    """Seconds since issue: the largest of the three clock deltas (no clock shortens it)."""
    return max(
        now.wall - grant.issued_wall, now.boot - grant.issued_boot, now.mono - grant.issued_mono
    )


def ttl_left(grant: Grant, now: ClockReading) -> int:
    if grant.status in TERMINAL:
        return 0
    return max(0, int(grant.ttl_sec - elapsed(grant, now)))


def _finite(value: object) -> bool:
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def _clock_anomaly(grant: Grant, now: ClockReading) -> bool:
    values = (now.wall, now.boot, now.mono)
    if not all(_finite(v) for v in values):
        return True
    if now.wall < grant.last_wall - CLOCK_SLACK_SEC:
        return True  # the wall clock went back since the last check
    if now.boot < grant.last_boot or now.mono < grant.last_mono:
        return True  # a monotonic clock cannot go back
    d_wall, d_boot = now.wall - grant.issued_wall, now.boot - grant.issued_boot
    return abs(d_wall - d_boot) > CLOCK_SLACK_SEC  # a forward or backward wall jump


def clock_check(grant: Grant, now: ClockReading | None) -> Grant:
    """The clocks at one look (watchdog pass, every request): ``None`` (unreadable) or an
    anomaly → ``revoked(clock_anomaly)``; elapsed ≥ TTL → ``expired(ttl)``; otherwise the
    reading becomes the last one (not a state change: the generation stays)."""
    grant = _require_goal(grant)
    if grant.status in TERMINAL:
        return grant
    if now is None or _clock_anomaly(grant, now):
        wall = grant.last_wall if now is None else now.wall
        return revoke(grant, "clock_anomaly", wall=wall)
    if elapsed(grant, now) >= grant.ttl_sec:
        return expire(grant, wall=now.wall)
    return replace(grant, last_wall=now.wall, last_boot=now.boot, last_mono=now.mono)


# --- derived views and events -------------------------------------------------------------

_GRANT_RANK = {
    ("revoked", "kill_switch"): 100,
    ("expired", "ttl"): 80,
    ("revoked", "unhealthy"): 70,
    ("revoked", "clock_anomaly"): 70,
    ("halted", "breaker_critical"): 60,
    ("halted", "breaker_warning"): 50,
    ("paused", "user"): 20,
    ("ended", "complete"): 5,
}
_GOAL_RANK = {"budget_limited": 40, "blocked": 30, "awaiting_verification": 15, "paused": 12}


def headline(grant: Grant) -> dict:
    """The displayed state: the higher-priority of the grant's and the goal's actual
    states (ADR 0011 §11 order: kill switch > revoked > expired > unhealthy / clock >
    breaker critical > breaker warning > budget_limited > blocked > paused). Derived on
    every read, never stored."""
    grant = _require_goal(grant)
    key = (grant.status, grant.reason)
    if grant.status == "revoked" and key not in _GRANT_RANK:
        grant_rank = 90
    else:
        grant_rank = _GRANT_RANK.get(key, 10 if grant.status == "paused" else 0)
    goal_rank = _GOAL_RANK.get(grant.goal_status, 0)
    if grant.status not in TERMINAL and goal_rank > grant_rank:
        return {"subject": "goal", "state": grant.goal_status, "reason": grant.goal_reason}
    return {"subject": "grant", "state": grant.status, "reason": grant.reason}


def _goal_code(old: Grant | None, new: Grant) -> str:
    status = new.goal_status
    if status == "active":
        if old is None:
            return "goal_issued"
        if old.goal_status == "awaiting_verification":
            return "verification_rejected"
        return "goal_resumed"
    if status == "blocked":
        return "blocked_" + new.goal_reason
    return {
        "paused": "goal_paused",
        "budget_limited": "goal_budget_limited",
        "awaiting_verification": "goal_claimed",
        "complete": "goal_verified",
        "unmet": "goal_unmet",
    }[status]


def _grant_code(old: Grant | None, new: Grant) -> str:
    if new.status == "active":
        return "grant_issued" if old is None else "grant_resumed"
    return f"{new.status}_{new.reason}"


def transitions(old: Grant | None, new: Grant) -> list[tuple[str, str, str, str]]:
    """The lifecycle events of one change: ``(subject_kind, from, to, reason_code)`` for
    the grant and the goal — codes only (ADR 0011 §12)."""
    new = _require_goal(new)
    out: list[tuple[str, str, str, str]] = []
    if old is None:
        out.append(("grant", "unknown", new.status, _grant_code(None, new)))
        out.append(("goal", "unknown", new.goal_status, _goal_code(None, new)))
        return out
    if (old.status, old.reason) != (new.status, new.reason):
        out.append(("grant", old.status, new.status, _grant_code(old, new)))
    if (old.goal_status, old.goal_reason) != (new.goal_status, new.goal_reason):
        out.append(("goal", old.goal_status, new.goal_status, _goal_code(old, new)))
    if new.late_verification and old.late_verification != new.late_verification:
        code = "late_verification_" + new.late_verification
        out.append(("goal", new.goal_status, new.goal_status, code))
    return out


def all_reason_codes() -> frozenset[str]:
    """Every reason code :func:`transitions` can produce (kept in step with the lifecycle
    contract's list by a test)."""
    codes = {"grant_issued", "grant_resumed", "goal_issued", "goal_resumed"}
    codes |= {"verification_rejected", "goal_paused", "goal_budget_limited", "goal_claimed"}
    codes |= {"goal_verified", "goal_unmet", "late_verification_pass", "late_verification_fail"}
    for status in ("paused", "halted", "revoked", "expired", "ended"):
        codes |= {f"{status}_{reason}" for reason in GRANT_REASONS[status]}
    codes |= {"blocked_" + reason for reason in GOAL_REASONS["blocked"]}
    return frozenset(codes)
