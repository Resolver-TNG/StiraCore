"""A job run waiting for a person's approval, and its continuation (ADR 0024 §4 / §6, S3).

Opt-in per job (``unattended_approval: "wait"``); the default stays deny. When a job's call
hits an Ask rule, :class:`JobApprovalGate` stores the request in the durable approval store
with a ``kind: "job"`` origin, ends the child's invocation (the worker is released) and the
run becomes ``awaiting_approval`` (still active for the overlap rule). Only an approval in
the dashboard continues it, through the scheduler (``Scheduler.resume_waiting_run``): the
continuation segment replays the paused call, which the gate lets through only when it is
the very call that was approved (same tool, ``toolUseId`` and argument hash) and the store
hands the approval out once (``claim_resume`` + ``consume(resumed=True)`` inside
``resume_context``). Anything else refuses and ends the run.

Within one process lifetime only: paused children live in memory, and at every start
every open ``kind: "job"`` request is cancelled (``cancel_at_start``). A run interrupted by a
crash or a restart is closed by the scheduler and never continued (ADR 0008 §4, 0021 §9).
"""

from __future__ import annotations

import contextlib
import logging
import secrets
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from stiracore.core.approval import args_hash
from stiracore.core.approval_store import APPROVED, PENDING, ApprovalStore, ApprovalStoreError

__all__ = [
    "MAX_WAITING_RUNS",
    "JobApprovalGate",
    "JobApprovalWaits",
    "PausedRun",
]

logger = logging.getLogger(__name__)

MAX_WAITING_RUNS = 3  # ADR 0024 §6: at most three waiting runs per server
WAIT_MAX_CAP = 86400


@dataclass
class PausedRun:
    request_id: str
    run_id: str
    job_id: str
    tool: str
    args_hash: str
    tool_use_id: str
    deadline: float
    child: Any = None
    checkpoint_id: str | None = None
    claimed: bool = False


class JobApprovalWaits:
    """The in-process registry of job runs waiting for an approval (one per run)."""

    def __init__(
        self,
        store: ApprovalStore,
        *,
        hitl_timeout_sec: float | Callable[[], float] = 3600.0,
        now_fn: Callable[[], float] | None = None,
        max_waiting: int = MAX_WAITING_RUNS,
    ) -> None:
        import time

        self.store = store
        # The server's ``hitl_timeout_sec``, read at each pause (a callable: the live value).
        self._hitl_timeout = hitl_timeout_sec
        self._now = now_fn or time.time
        self.max_waiting = max(1, int(max_waiting))
        # The origin's incarnation: a request of an earlier process never matches.
        self.incarnation = secrets.token_hex(16)
        self._lock = threading.Lock()
        self._paused: dict[str, PausedRun] = {}  # request_id -> paused run

    # -- start-up -----------------------------------------------------------------

    def cancel_at_start(self) -> int:
        """Cancel every open ``kind: "job"`` request (ADR 0024 §4). Raises
        :class:`ApprovalStoreError` when that cannot be recorded (the scheduler then does
        not start: fail closed, §7)."""
        count = 0
        for record in self.store.open_records():
            if (record.origin or {}).get("kind") != "job":
                continue
            if self.store.cancel(record.request_id, by="system", reason="restart"):
                count += 1
        return count

    # -- pausing --------------------------------------------------------------------

    def park(self, ctx: Any, tool: str, tool_input: Any, tool_use_id: str) -> PausedRun:
        """Store the request (``kind: "job"``) and remember the paused run.

        Raises :class:`ApprovalStoreError` (store down, too many waiting runs, one
        request per run, unnormalizable arguments): the caller then denies."""
        digest = args_hash(tool_input)
        timeout = self._hitl_timeout() if callable(self._hitl_timeout) else self._hitl_timeout
        wait = min(
            float(timeout),
            float(min(ctx.job.approval_wait_max_sec or 3600, WAIT_MAX_CAP)),
        )
        with self._lock:
            if any(p.run_id == ctx.run_id for p in self._paused.values()):
                raise ApprovalStoreError("one waiting request per run")
            if len(self._paused) >= self.max_waiting:
                raise ApprovalStoreError("too many job runs are waiting for approval")
            request_id = f"job-{secrets.token_hex(12)}"
            now = self._now()
            paused = PausedRun(
                request_id=request_id,
                run_id=ctx.run_id,
                job_id=ctx.job.job_id,
                tool=tool,
                args_hash=digest,
                tool_use_id=tool_use_id,
                deadline=now + wait,
            )
            self.store.submit(
                request_id=request_id,
                agent_id=f"job:{ctx.job.job_id}",
                tool_name=tool,
                tool_input=tool_input,
                expires_at=now + wait,
                tool_use_id=tool_use_id,
                correlation_id=ctx.run_id,
                origin={
                    "kind": "job",
                    "job_id": ctx.job.job_id,
                    "run_id": ctx.run_id,
                    "generation": int(ctx.generation),
                    "incarnation": self.incarnation,
                },
                reason="a scheduled job needs a person",
            )
            self._paused[request_id] = paused
            return paused

    def attach(self, request_id: str, child: Any, checkpoint_id: str | None) -> None:
        with self._lock:
            paused = self._paused.get(request_id)
            if paused is not None:
                paused.child, paused.checkpoint_id = child, checkpoint_id

    def for_run(self, run_id: str) -> PausedRun | None:
        with self._lock:
            return next((p for p in self._paused.values() if p.run_id == run_id), None)

    def get(self, request_id: str) -> PausedRun | None:
        with self._lock:
            return self._paused.get(request_id)

    def waiting_count(self) -> int:
        with self._lock:
            return len(self._paused)

    # -- ending -----------------------------------------------------------------------

    def release(self, request_id: str, reason: str | None = None) -> None:
        """Forget the paused run; revoke its request unless it was used (best effort:
        a failure quarantines the store — an approval must never stay lendable)."""
        with self._lock:
            paused = self._paused.pop(request_id, None)
        if paused is None:
            return
        try:
            record = self.store.get(request_id)
            if record is not None and not record.consumed:
                self.store.cancel(request_id, by="system", reason=reason or "run_ended")
        except Exception as error:  # noqa: BLE001 - fail closed for good
            self.store.quarantine(f"could not revoke job approval {request_id}: {error}")

    def state_of(self, request_id: str) -> str:
        """``pending`` / ``approved`` / ``rejected`` / ``expired`` / ``cancelled`` /
        ``unavailable``, as the scheduler's sweep reads it."""
        try:
            record = self.store.get(request_id)
        except ApprovalStoreError:
            return "unavailable"
        if record is None:
            return "cancelled"
        if record.status == PENDING:
            paused = self.get(request_id)
            if paused is not None and self._now() > paused.deadline:
                with contextlib.suppress(ApprovalStoreError):
                    self.store.expire(request_id)
                return "expired"
            return "pending"
        if record.status == APPROVED and not record.consumed:
            return "approved"
        return {"rejected": "rejected", "expired": "expired"}.get(record.status, "cancelled")


class JobApprovalGate:
    """The rule of every call of a ``wait`` job: Never refuses, Ask pauses the run (once),
    and in a continuation only the approved call passes, once."""

    def __init__(self, ctx: Any, rule_of: Callable[[str], str | None], waits: JobApprovalWaits):
        self.ctx = ctx
        self.rule_of = rule_of
        self.waits = waits
        self.resuming: PausedRun | None = None  # set by the continuation segment
        self.paused: PausedRun | None = None
        # toolUseId -> args hash of the call this gate just let through with the consumed
        # approval; the child's own gate asks :meth:`approver` for it once.
        self._granted: dict[str, str] = {}

    def register_hooks(self, registry: Any, **_: Any) -> None:
        from strands.hooks import BeforeToolCallEvent

        registry.add_callback(BeforeToolCallEvent, self.before_tool)

    def before_tool(self, event: Any) -> None:
        from stiracore.control.agent_jobs import _deny

        if getattr(event, "cancel_tool", False):
            return
        name = str(event.tool_use.get("name", ""))
        tool_input = event.tool_use.get("input", {})
        tool_use_id = str(event.tool_use.get("toolUseId", ""))
        try:
            rule = self.rule_of(name)
        except Exception:  # noqa: BLE001 - unreadable rules: fail closed
            rule = "never"
        if rule in (None, "always"):
            return
        if rule != "ask":
            self.ctx.cancel("tool_never")
            _deny(event, f"{name} is set to Never", tool=True)
            return
        resume = self.resuming
        if resume is not None and tool_use_id == resume.tool_use_id and name == resume.tool:
            if not self._bound(resume, tool_input):
                with contextlib.suppress(ApprovalStoreError):
                    self.waits.store.note_binding_mismatch(resume.request_id, None)
                self.ctx.cancel("binding_mismatch")
                _deny(event, "the call differs from the approved one", tool=True)
                return
            if self.waits.store.consume(
                resume.request_id, by_request_id=resume.request_id, resumed=True
            ):
                self._granted[tool_use_id] = resume.args_hash
                return  # the approved call, exactly once
            self.ctx.cancel("approval_unavailable")
            _deny(event, "the approval is not available (used, expired or revoked)", tool=True)
            return
        if resume is not None or self.paused is not None:
            # A continuation does not open a second wait; one waiting request per run.
            self.ctx.require_approval(name, tool_input)
            _deny(event, f"{name} needs a person; this run already waited once", tool=True)
            return
        try:
            self.paused = self.waits.park(self.ctx, name, tool_input, tool_use_id)
        except (ApprovalStoreError, TypeError, ValueError):
            self.ctx.require_approval(name, tool_input)  # deny, as without wait
            _deny(event, f"{name} needs a person; the request could not be stored", tool=True)
            return
        self.ctx.wait_for_approval(self.paused.request_id, name, self.paused.args_hash)
        _deny(event, f"{name} is waiting for a person's approval", tool=True)

    def approver(self, request: Any) -> Any:
        """The child engine's ``on_hitl_request``: the child's own gate (CheckpointHooks)
        asks it for the Ask call this gate just let through. APPROVE only for that call
        (same toolUseId, tool and argument hash), once; anything else is a rejection."""
        from stiracore.core.engine import HITLAction, HITLDecision

        resume = self.resuming
        tool_use_id = str(getattr(request, "tool_use_id", "") or "")
        granted = self._granted.pop(tool_use_id, None)
        try:
            digest = args_hash(getattr(request, "tool_input", None))
        except (TypeError, ValueError):
            digest = None
        if (
            resume is None
            or granted is None
            or digest != granted
            or getattr(request, "tool_name", None) != resume.tool
        ):
            return HITLDecision(HITLAction.REJECT, reason="scheduled jobs have no approver here")
        return HITLDecision(
            HITLAction.APPROVE,
            request_id=getattr(request, "request_id", None),
            args_hash=getattr(request, "args_hash", None),
        )

    @staticmethod
    def _bound(resume: PausedRun, tool_input: Any) -> bool:
        try:
            return args_hash(tool_input) == resume.args_hash
        except (TypeError, ValueError):
            return False
