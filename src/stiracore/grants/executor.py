"""The goal iteration executor (ADR 0011 §5, §8, §11, §13; W5 of #296).

The first code path that writes files without a person watching each call. It runs
**one** goal iteration of an active grant, started only by a person's "Run one iteration"
press (``POST /api/grants/<id>/step``; no heartbeat, no scheduler, no model-visible
tool). Built only with the gateway switch ``autonomy.stage_c_exec`` (default off; it needs
``autonomy.stage_c``): with it off there is no executor object, no route, no thread.

**Admission** (:meth:`GoalIterationExecutor.step`): at that instant the grant record from
the grant service's look (``executor_record`` = ``authority_record``: ``check_now``) is
``active`` **and** its goal ``active``; no kill, the authority and the grant store
healthy, the breaker NORMAL, iterations below the maximum, TTL left on the wall **and**
the monotonic clock, budget left, no chat turn running (the server's turn lock), and a
``recheck`` left by a halt resume satisfied (the checks above run again, then it is
cleared). One iteration at a time per server (and so per grant; v1 has one live grant).

**One permit per effect.** Every model call, every tool call, every approval wait and
every ``goal_report`` takes a W1 permit bound to (incarnation, epoch, grant id,
generation, TTL pair; a model call also its budget reservation) and **consumes** it right
before the I/O — after any wait (an approval card, a long model response) a new one is
taken. Before the consume the look runs again (clocks, TTL, kill, health, breaker, grant
and goal state) and the iteration's own stops are read (a person's chat turn, the wall
time, shutdown). A refusal at the consume ends the iteration as ``permit_stale`` with no
effect. A consumed effect is in the W1 in-flight set: a later narrowing (kill, revoke,
breaker, ``/new``, …) cancels the child at its next safe point and withdraws a pending
card; an effect that still finished is recorded as a residual code.

**Budget** (:class:`IterationBudget`): before each provider request the final input
(the exact messages, tool specs and system prompt that are sent, the context block
included) gets a **local** upper bound (:func:`request_bound`: one token per UTF-8 byte of
its serialized form plus fixed overheads — no provider request is ever made for a count)
and ``bound + max_output_tokens`` is reserved against the grant total, the per-tick (= per
iteration) limit and any shared pool; a call that does not fit is not sent. No bound (a
request that does not serialize) or no ``max_tokens`` → no call. Settled with the reported
usage (the difference is refunded; unknown usage: the whole reservation) through
``state.charge``; every reservation ends exactly once. W6-F (#640): a stop during a model
call lets the call finish (the provider is not cancelled; the iteration waits up to
:data:`STOP_DRAIN_SEC` for it), so a call that completes is charged its reported usage; only
a call given up (still running then, or shutdown / the wall time) keeps the whole
reservation.

**Context**: the iteration's model input ends with one ephemeral block, appended to a
copy of the latest user message for that call only: the trusted playbook
(``dev_playbook.md``, a file only a person edits, loaded read-only; its SHA-256 is the
``playbook_<12 hex>`` code of the iteration's start event) and the goal as the trailing
part (#254: the cached prefix is untouched). The goal is never in the child's history,
the main session, a checkpoint, an event, an error or a log line.

**Records are codes and counters only**: the iteration view, the lifecycle events
(subject ``grant_iteration``) and the agent's ``goal_report(kind, code)`` (fixed enums,
no free text). A completion claim moves the goal to ``awaiting_verification`` — only the
user completes it.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import math
import os
import secrets
import stat
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

from stiracore.authority import AuthorityError, ClockPair, Receipt, TtlPair

from . import progress as outcome
from . import state
from .schema import TOOLS, BoardBusy, GrantRefused

__all__ = [
    "BLOCK_FOR_END",
    "END_CODES",
    "PLAYBOOK_FILE",
    "REPORT_TOOL",
    "ClaimFence",
    "GoalIterationExecutor",
    "IterationBudget",
    "IterationStop",
    "Playbook",
    "PreemptSignal",
    "formatted_max_tokens",
    "load_playbook",
    "request_bound",
]

logger = logging.getLogger(__name__)

PLAYBOOK_FILE = Path(__file__).with_name("dev_playbook.md")
PLAYBOOK_MAX_BYTES = 8192
# After a stop, how long the iteration thread waits for its provider-call thread before
# it abandons it (the call cannot be cancelled inside a blocked socket read).
ABANDON_GRACE_SEC = 0.5
# #640 (W6-F): after a stop, how much longer it waits for a model call that is reading its
# response, so a call that completes is charged its reported usage (not the reservation).
STOP_DRAIN_SEC = 5.0
REPORT_TOOL = "goal_report"
REPORT_CODES = {
    "progress": ("step_done", "tests_written", "checks_run", "investigating"),
    "blocked": state.AGENT_BLOCKED_CODES,
    "claim_complete": ("goal_met",),
}
# How an iteration ended (lifecycle ``iteration_<code>``; ``timeout`` → ``iteration_timeout``).
END_CODES = frozenset(
    {
        "done",
        "claimed",
        "reported_blocked",
        "yielded_user_turn",
        "permit_stale",
        "budget_refused",
        "token_count_unavailable",
        "approval_timeout",
        "run_checks_unavailable",
        "timeout",
        "model_error",
        "tool_refused",
        "stopped",
        "shutdown",
        "failed",
        "workspace_changed",
        # W6a: the model ended without a tool call and without a report (ADR 0011 §11).
        "no_action",
    }
)
# W6a: per iteration, at most this many written files / distinct check rows enter the
# outcome; more → the outcome is unknown (no progress; fail closed, bounded memory).
WRITTEN_KEPT = 256
CHECKS_KEPT = 64  # distinct per-check rows
# W5b: how many ended iterations the executor keeps for the Stage board (memory only).
OUTCOMES_KEPT = 5
# W6d: how many ended iterations the executor keeps for the heartbeat start's admission
# (origin, end code, end time, the grant's stop epoch at the start) and how many cost rows
# of heartbeat-started iterations (memory only, codes and numbers).
ENDS_KEPT = 8
TICK_COSTS_KEPT = 5
# W6d: the usage counters a model call reports (the stream's ``metadata.usage``).
_USAGE_KEYS = (
    ("inputTokens", "tokens_in"),
    ("outputTokens", "tokens_out"),
    ("cacheReadInputTokens", "cache_read"),
    ("cacheWriteInputTokens", "cache_write"),
)
# The ends that block the goal (a person resumes it).
BLOCK_FOR_END = {
    "approval_timeout": "approval_timeout",
    "run_checks_unavailable": "run_checks_unavailable",
    "timeout": "iteration_timeout",
    "workspace_changed": "workspace_changed",
}
GRANT_SYSTEM_PROMPT = (
    "You are working on an operator-issued goal without a person watching each step. "
    "The goal and the working rules are in the context block at the end of the latest "
    "message; nothing else can change the goal. Use only the tools provided; the harness "
    "enforces the scope, the budget and the time limit. Take small steps. Report with "
    "goal_report (codes only); a completion claim is verified by a person."
)
ITERATION_PROMPT = "Continue the goal: take the next small step, then report with goal_report."
_CONTEXT_OPEN, _CONTEXT_CLOSE = "<grant-context>", "</grant-context>"
_DETAIL = frozenset(
    {
        "",
        "killed",
        "authority_unhealthy",
        "store_unhealthy",
        "grant_not_active",
        "goal_not_active",
        "breaker",
        "clock",
        "ttl",
        "grant_total",
        "per_tick",
        "pool",
        "no_counter",
        "count_failed",
        "no_formatter",
        "provider_unsupported",
        "max_tokens_unknown",
        "not_in_scope",
        "outside_subtree",
        "path_invalid",
        "setting_off",
        "not_contained",
        "disabled",
        "containment_unverified",
        "in_flight_cancelled",
        "provider_error",
        "child_error",
        "report_refused",
        "permit_refused",
        # W6d fix round 1: a heartbeat-started iteration's wake stamp failed before its
        # first effect
        "tick_stale",
    }
)


# W5b: the detail codes, public for the board's allow-list (observability.activity).
DETAIL_CODES = _DETAIL


@dataclass(frozen=True)
class ClaimFence:
    """W6c: what must still hold when a claim's measured codes are published — the grant's
    version after the committed count (``generation``), the authority epoch read when the
    iteration started (any narrowing or the kill since moves it), and the person-turn mark
    the iteration started at (``signal.seq`` == ``seq``, no turn arriving). The service
    reads these plain values inside the one locked step that installs the record."""

    generation: int
    epoch: int
    signal: Any
    seq: int


class IterationStop(Exception):
    """The iteration stops here (``code`` one of :data:`END_CODES`); no effect follows."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Playbook:
    """The trusted block: its text and version (``playbook_<12 hex of SHA-256>``)."""

    text: str
    sha256: str

    @property
    def code(self) -> str:
        return "playbook_" + self.sha256[:12]


def load_playbook(path: Path | str = PLAYBOOK_FILE) -> Playbook | None:
    """Read the playbook once, read-only, without following a symlink; ``None`` when it
    is missing, too large, not UTF-8 or empty (the executor then admits nothing)."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return None
    try:
        raw = os.read(fd, PLAYBOOK_MAX_BYTES + 1)
    except OSError:
        return None
    finally:
        os.close(fd)
    if len(raw) > PLAYBOOK_MAX_BYTES:
        return None
    try:
        text = raw.decode("utf-8").strip()
    except UnicodeDecodeError:
        return None
    if not text:
        return None
    return Playbook(text=text, sha256=hashlib.sha256(raw).hexdigest())


class PreemptSignal:
    """A turn arrived: every turn-lock holder (``core.turn_lock.TurnLock``, W6b) enters
    :meth:`arriving` before it tries the turn lock; an iteration that started at another
    value yields at its next boundary, and no press is admitted while :attr:`pending`."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._seq = 0
        self._pending = 0
        self._listeners: list[Callable[[], Any]] = []

    @property
    def seq(self) -> int:
        return self._seq

    @property
    def pending(self) -> int:
        """Acquirers between their signal and the end of their lock attempt."""
        return self._pending

    @contextlib.contextmanager
    def arriving(self):
        """Around one turn-lock attempt: the mark first, then the signal; the mark ends
        once the attempt has ended (the lock is then held, or not taken)."""
        with self._lock:
            self._pending += 1
        try:
            self.arrive()
            yield
        finally:
            with self._lock:
                self._pending -= 1

    def arrive(self) -> None:
        with self._lock:
            self._seq += 1
            listeners = list(self._listeners)
        for listener in listeners:
            try:
                listener()
            except Exception:  # noqa: BLE001 - never raise into the chat route
                logger.warning("grants: a pre-emption listener failed")

    def listen(self, listener: Callable[[], Any]) -> None:
        with self._lock:
            self._listeners.append(listener)


# --- budget --------------------------------------------------------------------------------


@dataclass
class _Reservation:
    id: str
    tokens: int
    pools: list = field(default_factory=list)
    open: bool = True


class IterationBudget:
    """The reservations of one iteration (ADR 0011 §5 hard budget).

    ``reserve(need, used, total)`` fits ``need`` into the grant total (``used`` + every
    open reservation), the per-tick limit (settled + open in this iteration) and every
    shared pool (objects with ``reserve(tokens) -> handle | None``, ``settle(handle,
    tokens)``, ``release(handle)``), or raises ``IterationStop("budget_refused")`` with
    nothing held. A reservation ends exactly once: :meth:`settle` or :meth:`release`
    (a second call does nothing and returns False)."""

    def __init__(self, per_tick: int, pools: Iterable[Any] = ()) -> None:
        self._lock = threading.Lock()
        self.per_tick = int(per_tick)
        self.pools = list(pools)
        self.charged = 0
        self.releases = 0
        self.refusal = ""
        self._open: dict[str, _Reservation] = {}

    def outstanding(self) -> int:
        with self._lock:
            return sum(r.tokens for r in self._open.values())

    def try_used(self) -> int | None:
        """W5b, the board: settled + open tokens of this iteration, without waiting
        (``None``: the lock is held)."""
        if not self._lock.acquire(blocking=False):
            return None
        try:
            return self.charged + sum(r.tokens for r in self._open.values())
        finally:
            self._lock.release()

    def reserve(self, need: int, *, used: int, total: int) -> _Reservation:
        if type(need) is not int or need < 0:
            raise IterationStop("budget_refused")
        with self._lock:
            held = sum(r.tokens for r in self._open.values())
            if self.charged + held + need > self.per_tick:
                self.refusal = "per_tick"
            elif used + held + need > total:
                self.refusal = "grant_total"
            else:
                self.refusal = ""
            if self.refusal:
                raise IterationStop("budget_refused")
            reservation = _Reservation(secrets.token_hex(16), need)
            self._open[reservation.id] = reservation
        for pool in self.pools:
            try:
                handle = pool.reserve(need)
            except Exception:  # noqa: BLE001 - an unreadable pool refuses
                handle = None
            if handle is None:
                self.refusal = "pool"
                self.release(reservation)
                raise IterationStop("budget_refused")
            reservation.pools.append((pool, handle))
        return reservation

    def _take(self, reservation: _Reservation) -> bool:
        with self._lock:
            if self._open.pop(reservation.id, None) is None:
                return False
            reservation.open = False
            return True

    def settle(self, reservation: _Reservation, tokens: int) -> bool:
        """Replace the reservation by what was spent (once)."""
        if not self._take(reservation):
            return False
        with self._lock:
            self.charged += tokens
        for pool, handle in reservation.pools:
            try:
                pool.settle(handle, tokens)
            except Exception:  # noqa: BLE001
                logger.warning("grants: a shared pool could not settle")
        return True

    def release(self, reservation: _Reservation) -> bool:
        """Give the reservation back unspent (once)."""
        if not self._take(reservation):
            return False
        with self._lock:
            self.releases += 1
        for pool, handle in reservation.pools:
            try:
                pool.release(handle)
            except Exception:  # noqa: BLE001
                logger.warning("grants: a shared pool could not release")
        return True

    def release_all(self) -> int:
        with self._lock:
            left = list(self._open.values())
        return sum(1 for r in left if self.release(r))

    def settle_open(self) -> list[int]:
        """Charge every open reservation in full (its call's outcome is unknown); the
        tokens settled, one entry per reservation."""
        with self._lock:
            left = list(self._open.values())
        return [r.tokens for r in left if self.settle(r, r.tokens)]


# The local, conservative bound of a request's input tokens (no provider request): every
# token of a byte-level tokenizer is at least one UTF-8 byte, so the serialized size is an
# upper bound; a fixed overhead per message and per tool covers role and framing tokens.
REQUEST_OVERHEAD = 64
MESSAGE_OVERHEAD = 16
TOOL_OVERHEAD = 16


def request_bound(request: Mapping) -> int:
    """An upper bound of the input tokens of one **formatted** request (exactly what the
    provider client is called with — overrides such as ``additional_args`` included): one
    token per UTF-8 byte of its JSON form plus fixed overheads. Anything that does not
    serialize to JSON (raw bytes, e.g. an inline image or document; NaN; a lone surrogate)
    raises — no bound, no call (fail closed)."""
    import json

    if not isinstance(request, Mapping):
        raise TypeError("request")
    raw = json.dumps(request, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    size = len(raw.encode("utf-8"))
    messages = request.get("messages") or []
    tools = (request.get("toolConfig") or {}).get("tools") or request.get("tools") or []
    return size + REQUEST_OVERHEAD + MESSAGE_OVERHEAD * len(messages) + TOOL_OVERHEAD * len(tools)


def formatted_max_tokens(request: Mapping) -> int | None:
    """The output limit the formatted request itself carries (``None``: none found)."""
    config = request.get("inferenceConfig")
    for value in (
        config.get("maxTokens") if isinstance(config, Mapping) else None,
        request.get("max_tokens"),
        request.get("max_completion_tokens"),
    ):
        if type(value) is int and value > 0:
            return value
    return None


def _root_intact_path(root: str) -> bool:
    try:
        info = os.lstat(root)
    except OSError:
        return False
    # lstat's S_ISDIR is false for a symlink; the resolved path catches a swapped parent.
    return stat.S_ISDIR(info.st_mode) and os.path.realpath(root) == root


def usage_tokens(usage: Any) -> int | None:
    """Tokens a model call spent, from the stream's usage (``None``: not reported)."""
    if not isinstance(usage, Mapping):
        return None
    parts = [
        usage.get(k)
        for k in ("inputTokens", "outputTokens", "cacheReadInputTokens", "cacheWriteInputTokens")
    ]
    if type(parts[0]) is not int or type(parts[1]) is not int:
        return None
    summed = sum(p for p in parts if type(p) is int and p > 0)
    total = usage.get("totalTokens")
    return max(summed, total) if type(total) is int else summed


# --- one iteration -------------------------------------------------------------------------


class _Iteration:
    """The in-memory record of one iteration (codes and counters only)."""

    def __init__(
        self, grant: Any, number: int, deadline: float, seq0: int, budget, epoch0: int = 0
    ) -> None:
        self.grant_id = grant.grant_id
        self.session_ref = grant.session_ref
        self.subtree = grant.workspace_subtree
        self.number = number
        self.deadline = deadline
        self.seq0 = seq0
        self.epoch0 = epoch0  # W6c: the authority epoch at the start (a claim's fence)
        self.budget = budget
        self.op = secrets.token_hex(16)
        self.cancel_event = threading.Event()
        # ``wake``: the iteration thread's one signal (the call ended, or a stop).
        self.wake = threading.Event()
        self.call_done = threading.Event()
        # #640 (W6-F): the provider call's own cancel (the executor gives the call up) and
        # how many model calls are reading a response now (a stop then waits for it).
        self.abort = threading.Event()
        self.model_reading = 0
        self.abandoned = False
        self.worker: threading.Thread | None = None
        self.lock = threading.Lock()
        self.stop_code: str | None = None
        self.detail = ""
        self.state = "running"
        self.counts = dict.fromkeys(
            ("model_calls", "tool_calls", "writes", "checks", "waits", "reports"), 0
        )
        self.residual: list[str] = []
        self.report = ""
        # W6a, memory only (never in a summary, an event or a log): the effects consumed,
        # the grant's stop epoch at the start (a stop by anyone else since: interrupted),
        # each written file's digest before the iteration's first write to it (resolved
        # path → digest: did this iteration change it?), the files it wrote, the per-check
        # rows of all its run_checks calls, whether anything was unreadable, the report.
        self.effects = 0
        self.own_blocked = False  # the iteration's own block applied (once)
        self.read_budget = outcome.ReadBudget()  # W6a: bytes this iteration may digest
        self.stop_epoch0 = getattr(grant, "stop_epoch", 0)  # the grant's stops so far
        self.pre: dict[str, bytes] = {}
        self.wrote: list[str] = []
        self.check_outcomes: set[tuple] = set()  # per-check rows, the union of all calls
        self.unreadable = False
        self.report_kind = ""
        # W6c (memory only): the combined verdict of the run_checks calls since this
        # iteration's last editor write (None: none), the write sequence (moved at the start
        # and at the end of every editor write) and the writes in flight (a check that
        # overlaps a write never counts), whether the editor wrote at all, and the claim
        # instant (the evidence record this iteration's end measures).
        self.claim_checks: tuple[str, tuple] | None = None
        self.write_seq = 0
        self.writing = 0
        self.wrote_any = False
        self.claimed_at: float | None = None
        self.tools: tuple[str, ...] = ()
        self.called: list[str] = []  # W5b: the scope tools that ran, in call order
        self.child: Any = None
        self.card: str | None = None
        self.broker: Any = None
        self.thread: threading.Thread | None = None
        self._receipts = 0
        self.started: float | None = None  # the executor's mono clock (W5b: the duration)
        # W6d: who started it — ``press`` (a person) or ``tick`` (the scheduler's
        # grant_tick); the provider's usage counters of a tick-started one (numbers only).
        self.origin = "press"
        self.stamp: Any = None  # W6d fix round 1: the wake's stamp (tick only)
        self.stamp_gates: set[str] = set()  # W6d fix round 2: the gates passed once
        self.usage: dict[str, int] = {}

    def add_usage(self, usage: Any) -> None:
        """W6d: add one model call's reported counters (built-in non-negative ints only;
        cache counters only when the provider reports them)."""
        if not isinstance(usage, Mapping):
            return
        with self.lock:
            for source, key in _USAGE_KEYS:
                value = usage.get(source)
                if type(value) is int and 0 <= value < (1 << 53):
                    self.usage[key] = self.usage.get(key, 0) + value

    def receipt(self, kind: str) -> Receipt:
        """The audit reference a permit carries: this iteration's op id and step number."""
        with self.lock:
            self._receipts += 1
            n = self._receipts
        digest = hashlib.sha256(f"grant-iteration:{self.op}:{n}:{kind}".encode()).hexdigest()
        return Receipt(event_id=f"op:{self.op}:intent:{min(n, 999999)}", seq=n, hash=digest)

    def stop(self, code: str, detail: str = "") -> bool:
        """The first stop wins. Cancels the child at its next safe point and withdraws a
        pending card. Memory and flags only (it runs on the authority's cancel path)."""
        if code not in END_CODES:
            code = "failed"
        with self.lock:
            first = self.stop_code is None
            if first:
                self.stop_code = code
                self.detail = detail if detail in _DETAIL else ""
            child = self.child
        self.cancel_event.set()
        self.wake.set()
        if child is not None:
            try:
                child.cancel()
            except Exception:  # noqa: BLE001 - the flag above is set anyway
                logger.info("grants: the iteration's child could not be cancelled")
        self.withdraw_card()
        return first

    def on_cancel(self, _reason: Any = None) -> None:
        self.stop("stopped", "in_flight_cancelled")

    def withdraw_card(self) -> None:
        with self.lock:
            card, broker = self.card, self.broker
        if card is not None and broker is not None:
            withdraw = getattr(broker, "withdraw", None)
            if callable(withdraw):
                try:
                    withdraw(card)
                except Exception:  # noqa: BLE001
                    logger.info("grants: a pending card could not be withdrawn")

    def note(self, counter: str) -> None:
        with self.lock:
            self.counts[counter] += 1

    def called_tool(self, name: str) -> None:
        """W5b: a scope tool that ran (past its consume), once per name, in call order —
        the board's outcome row lists these, never the admitted scope. Bounded by
        :data:`TOOLS` (a scope name only, each once)."""
        with self.lock:
            if name in TOOLS and name not in self.called:
                self.called.append(name)

    def summary(self) -> dict:
        with self.lock:
            return self._summary_locked()

    def try_summary(self) -> dict | None:
        """W5b, the board: :meth:`summary` without waiting (``None``: the lock is held)."""
        if not self.lock.acquire(blocking=False):
            return None
        try:
            return self._summary_locked()
        finally:
            self.lock.release()

    def _summary_locked(self) -> dict:
        """The caller holds ``self.lock``."""
        return {
            "state": self.state,
            "number": self.number,
            "end": self.stop_code or ("" if self.state == "running" else "done"),
            "detail": self.detail,
            "card": "waiting" if self.card is not None else "none",
            "abandoned": self.abandoned,
            "residual": list(self.residual),
            "report": self.report,
            "tools": list(self.tools),
            **self.counts,
            # W6d: only a heartbeat-started iteration carries these (a press is unchanged).
            **({"origin": "tick", **self.usage} if self.origin == "tick" else {}),
        }


# --- the child's model, tools and hooks (permit-taking wrappers) ---------------------------

from strands.models.model import Model
from strands.tools.tools import AgentTool
from strands.types._events import ToolResultEvent


def _refused(tool_use: Mapping, code: str) -> ToolResultEvent:
    return ToolResultEvent(
        {
            "toolUseId": tool_use.get("toolUseId"),
            "status": "error",
            "content": [{"text": f"Not run: the grant iteration refused this call ({code})."}],
        }
    )


@dataclass(frozen=True)
class _Outcome:
    """W6a: what an iteration's end observed (memory only; ``added``: the paths to track,
    never shown or logged)."""

    interrupted: bool
    kind: str  # "none" | "unknown" | "known"
    fingerprint: bytes | None
    added: list
    cache: dict
    identity: bytes | None = None  # the workspace identity (known / none outcomes)
    # W6-H: path -> (relative path, digest): behind ``identity`` (known / none), or the
    # before-write digests of the paths an unobserved (unknown / interrupted) end wrote
    basis: dict | None = None
    seeds: dict | None = None


class GrantModel(Model):
    """The child's model: every provider request is counted, reserved, authorized and
    consumed here, right before it is sent; the context block is added to a copy."""

    def __init__(self, inner: Any, executor: GoalIterationExecutor, it: _Iteration) -> None:
        self.inner = inner
        self._ex = executor
        self._it = it

    def update_config(self, **model_config: Any) -> None:
        self.inner.update_config(**model_config)

    def get_config(self) -> Any:
        return self.inner.get_config()

    async def structured_output(self, *_args: Any, **_kwargs: Any):
        raise IterationStop("model_error")
        yield {}  # pragma: no cover - makes this an async generator

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        ex, it = self._ex, self._it
        reservation = None
        try:
            ex._precheck(it)
            final = ex._context(messages, it)
            # Sized from the request exactly as the provider will be called with it.
            request = ex._format(it, self.inner, final, tool_specs, system_prompt, kwargs)
            need = ex._count(it, request) + ex._max_output(it, request)
            reservation = ex._reserve(it, need)
            permit = ex._authorize(it, "model", reservation.id)
            entry = ex._consume(it, permit, "model", reservation.id)
        except IterationStop:
            if reservation is not None:
                it.budget.release(reservation)
            raise
        usage = None
        failed = False
        settled = False
        # #640 (W6-F): the provider's own cancel is the iteration's ``abort`` (set only when
        # the executor gives the call up: shutdown, the wall time, or a call still running
        # after the stop's drain) — not the child's cancel, so a stop lets a running call
        # finish and report its usage. The whole response is read here before the child sees
        # any of it, so the usage is known whether or not the child stops reading.
        if "cancel_signal" in kwargs:
            kwargs = {**kwargs, "cancel_signal": it.abort}
        events: list = []
        with it.lock:
            it.model_reading += 1
        try:
            # Right before the request: a turn that arrived after the consume still stops it.
            ex._precheck(it)
            ex._stamp_gate(it, "request")  # W6d: the first provider request
            async for event in self.inner.stream(final, tool_specs, system_prompt, **kwargs):
                if isinstance(event, Mapping) and isinstance(event.get("metadata"), Mapping):
                    usage = event["metadata"].get("usage", usage)
                events.append(event)
        except IterationStop:
            failed = True  # nothing was sent: the reservation is charged in full below
            raise
        except Exception:  # noqa: BLE001 - the provider's text never travels on
            failed = True
            it.stop("model_error", "provider_error")
            raise IterationStop("model_error") from None
        finally:
            with it.lock:
                it.model_reading -= 1
            if entry.done() == "completed_after_stop":
                with it.lock:
                    it.residual.append("model_completed_after_stop")
            # Reported usage → that is charged and the rest of the reservation released (also
            # for a call that completed after a stop); unknown → the whole reservation. A call
            # the iteration's end already settled in full (abandoned) changes nothing here.
            spent = None if failed else usage_tokens(usage)
            settled = ex._settle(it, reservation, reservation.tokens if spent is None else spent)
            it.note("model_calls")
            if it.origin == "tick" and not failed and settled:
                it.add_usage(usage)
        # Review round 1 (Astra B1): a stop or a person's turn that arrived while the call ran
        # discards the completed response (charged above) — the child never sees it, so a
        # text-only answer cannot enter its history or its turn processing either.
        ex._precheck(it)
        for event in events:
            yield event


class GrantTool(AgentTool):
    """One scope tool of the child: the allow-list, the subtree (``editor``) and the
    containment (``run_checks``) are checked, a permit is taken and consumed, and the same
    checks run again **after** the consume, right before the call."""

    def __init__(self, inner: Any, executor: GoalIterationExecutor, it: _Iteration) -> None:
        super().__init__()
        self.inner = inner
        self._ex = executor
        self._it = it

    @property
    def tool_name(self) -> str:
        return self.inner.tool_name

    @property
    def tool_spec(self) -> Any:
        return self.inner.tool_spec

    @property
    def tool_type(self) -> str:
        return self.inner.tool_type

    async def stream(self, tool_use: dict, invocation_state: dict, **kwargs: Any):
        import asyncio

        ex, it, name = self._ex, self._it, self.tool_name
        arguments = tool_use.get("input")
        arguments = arguments if isinstance(arguments, Mapping) else {}
        entry = None
        try:
            ex._precheck(it)
            ex._check_tool(it, name, arguments)
            permit = ex._authorize(it, "tool")
            entry = ex._consume(it, permit, "tool")
            host = ex._check_tool(it, name, arguments)  # re-verified at the consume
        except IterationStop as stop:
            if entry is not None:
                entry.done("cancelled")
            yield _refused(tool_use, stop.code)
            return
        it.note("tool_calls")
        it.called_tool(name)
        # Counted when the result is seen (the consumer may stop at the result event).
        try:
            if name == "run_checks":
                token = ex._checks_started(it)
                try:
                    result = await asyncio.to_thread(ex._run_checks, self.inner, arguments, host)
                except BaseException:
                    ex._claim_call(it, None, token)  # it ran, its outcome is unknown
                    raise
                status = result.get("status")
                if status == "refused" and result.get("reason") == "containment_unverified":
                    it.stop("run_checks_unavailable", "containment_unverified")
                elif status in ("passed", "failed"):
                    it.note("checks")
                ex._observe_checks(it, result)
                ex._claim_call(it, result, token)
                yield ToolResultEvent(ex._check_result(tool_use, result))
            else:
                write = name == "editor" and not ex._read_only(arguments)
                target = ex._before_write(it, arguments) if write else None
                try:
                    async for event in self.inner.stream(tool_use, invocation_state, **kwargs):
                        if isinstance(event, ToolResultEvent):
                            ok = (event.tool_result or {}).get("status") == "success"
                            if ok and write:
                                it.note("writes")
                                ex._wrote(it, target)
                        yield event
                finally:
                    if write:
                        ex._write_ended(it)
        finally:
            # Cancellation is at tool boundaries: an effect already running finishes. If a
            # stop arrived meanwhile — a narrowing (in-flight cancel) or a stop nothing
            # narrows (the TTL, a person's turn, the wall time) — it is a residual.
            after_stop = entry.done() == "completed_after_stop" or ex._stopped_since(it)
            if after_stop:
                with it.lock:
                    it.residual.append(f"{name}_completed_after_stop")


class _GuardedClient:
    """The child's provider client (a Bedrock client): every **terminal** request
    (``converse_stream`` / ``converse``) is checked right before it leaves — the
    iteration not stopped nor abandoned, no turn, the grant admissible — and takes its own
    permit. So an SDK-internal re-issue (e.g. the tool-result turn recovery) after a stop
    sends nothing. Transport retries of the same HTTP request inside botocore are not
    separate requests. Everything else is the real client's."""

    _TERMINAL = frozenset({"converse_stream", "converse"})

    def __init__(self, inner: Any, executor: GoalIterationExecutor, it: _Iteration) -> None:
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_ex", executor)
        object.__setattr__(self, "_it", it)

    def __getattr__(self, name: str) -> Any:
        if name in self._TERMINAL:

            def terminal(*args: Any, **kwargs: Any) -> Any:
                self._ex._terminal(self._it)
                return getattr(self._inner, name)(*args, **kwargs)

            return terminal
        return getattr(self._inner, name)


class GoalReportTool(AgentTool):
    """``goal_report(kind, code)``: the agent's only way to report — fixed enums, no free
    text. ``claim_complete`` moves the goal to ``awaiting_verification`` (never
    ``complete``); ``blocked`` to ``blocked(<code>)``. Both end the iteration."""

    SPEC: ClassVar[dict] = {
        "name": REPORT_TOOL,
        "description": (
            "Report the state of the goal with fixed codes (no text is kept). kind: "
            "progress (code: step_done, tests_written, checks_run, investigating), blocked "
            "(code: needs_decision, out_of_scope, missing_input) or claim_complete (code: "
            "goal_met; a person verifies it). blocked and claim_complete end this iteration."
        ),
        "inputSchema": {
            "json": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": sorted(REPORT_CODES)},
                    "code": {
                        "type": "string",
                        "enum": sorted({c for codes in REPORT_CODES.values() for c in codes}),
                    },
                },
                "required": ["kind", "code"],
                "additionalProperties": False,
            }
        },
    }

    def __init__(self, executor: GoalIterationExecutor, it: _Iteration) -> None:
        super().__init__()
        self._ex = executor
        self._it = it

    @property
    def tool_name(self) -> str:
        return REPORT_TOOL

    @property
    def tool_spec(self) -> Any:
        return self.SPEC

    @property
    def tool_type(self) -> str:
        return "python"

    async def stream(self, tool_use: dict, invocation_state: dict, **kwargs: Any):
        ex, it = self._ex, self._it
        arguments = tool_use.get("input")
        kind = arguments.get("kind") if isinstance(arguments, Mapping) else None
        code = arguments.get("code") if isinstance(arguments, Mapping) else None
        if (
            not isinstance(arguments, Mapping)
            or set(arguments) != {"kind", "code"}
            or kind not in REPORT_CODES
            or code not in REPORT_CODES[kind]
        ):
            yield _refused(tool_use, "report_invalid")
            return
        try:
            ex._precheck(it)
            permit = ex._authorize(it, "report")
            entry = ex._consume(it, permit, "report")
        except IterationStop as stop:
            yield _refused(tool_use, stop.code)
            return
        try:
            ex._apply_report(it, kind, code)
        except GrantRefused:
            it.stop("tool_refused", "report_refused")
            yield _refused(tool_use, "report_refused")
            return
        finally:
            entry.done()
        it.note("reports")
        yield ToolResultEvent(
            {
                "toolUseId": tool_use.get("toolUseId"),
                "status": "success",
                "content": [{"text": "recorded"}],
            }
        )


class IterationGuard:
    """Registered before the child's gate: before every model call and every tool call
    the iteration's stops are read (so no card is shown for a stopped iteration) and a
    tool outside the iteration's list is refused."""

    def __init__(self, executor: GoalIterationExecutor, it: _Iteration) -> None:
        self._ex = executor
        self._it = it

    def register_hooks(self, registry: Any, **_: Any) -> None:
        from strands.hooks import BeforeModelCallEvent, BeforeToolCallEvent

        registry.add_callback(BeforeModelCallEvent, self.before_model)
        registry.add_callback(BeforeToolCallEvent, self.before_tool)

    def before_model(self, event: Any) -> None:
        try:
            self._ex._precheck(self._it)
        except IterationStop as stop:
            event.cancel = f"the grant iteration stopped ({stop.code})"

    def before_tool(self, event: Any) -> None:
        name = str(event.tool_use.get("name", ""))
        if name != REPORT_TOOL and name not in self._it.tools:
            self._it.stop("tool_refused", "not_in_scope")
            event.cancel_tool = "Not available in this grant iteration."
            return
        try:
            self._ex._precheck(self._it)
        except IterationStop as stop:
            event.cancel_tool = f"Not run: the grant iteration stopped ({stop.code})."


# --- the executor ---------------------------------------------------------------------------


def _default_inner_model(parent: Any) -> Any:
    """The parent's own provider object (a Strands model holds configuration and a
    client, not conversation state); it is only ever called through :class:`GrantModel`."""
    return parent.model


class _ProviderDebugFilter(logging.Filter):
    """The provider transport's DEBUG lines carry the model input (the goal of an
    iteration's context block) and its output: never emitted while the executor exists
    (``botocore``'s request / response dumps, the Strands providers' ``request=<…>``)."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno >= logging.INFO:
            return True
        if record.name.startswith(("botocore", "strands.tools.executors")):
            return False
        return not (isinstance(record.msg, str) and record.msg.startswith("request=<"))


_DEBUG_FILTER = _ProviderDebugFilter()
_PROVIDER_LOGGERS = (
    "strands.tools.executors._executor",
    "botocore.endpoint",
    "botocore.parsers",
    "botocore.hooks",
    "botocore.retryhandler",
    "strands.models.bedrock",
    "strands.models.anthropic",
    "strands.models.openai",
    "strands.models.openai_responses",
    "strands.models.litellm",
    "strands.models.ollama",
    "strands.models.llamacpp",
    "strands.models.llamaapi",
    "strands.models.mistral",
    "strands.models.sagemaker",
    "strands.models.writer",
)


class GoalIterationExecutor:
    """One goal iteration at a time, on a person's press (see the module docstring).

    ``parent()`` is the server's agent (provider, floors, write policy, rule reader);
    ``tool_provider(names)`` the server's tool objects; ``broker`` the HITL approval
    broker (``request`` blocks until a person decides or the card expires; ``withdraw``
    ends a pending card). Tests replace ``estimator``, ``inner_model``,
    ``run_checks_runner`` / ``containment`` and inject stops with ``probe(point, it)``."""

    def __init__(
        self,
        grants: Any,
        authority: Any,
        *,
        parent: Callable[[], Any],
        tool_provider: Callable[[list[str]], list[Any]],
        broker: Any = None,
        settings: Any = None,
        recorder: Any = None,
        preempt: PreemptSignal | None = None,
        turn_busy: Callable[[], bool] | None = None,
        playbook: Playbook | None | str = "default",
        estimator: Callable[..., int] | None = request_bound,
        inner_model: Callable[[Any], Any] = _default_inner_model,
        run_checks_runner: Callable[..., dict] | None = None,
        containment: Callable[[], Any] | None = None,
        run_checks_enabled: Callable[[], bool] | None = None,
        pools: Iterable[Any] = (),
        mono: Callable[[], float] = time.monotonic,
        probe: Callable[[str, Any], Any] | None = None,
        build_child: Callable[..., Any] | None = None,
        stop_drain_sec: float | None = None,
    ) -> None:
        self._grants = grants
        self._authority = authority
        self._parent = parent
        self._tool_provider = tool_provider
        self._broker = broker
        self.settings = settings if settings is not None else grants.settings
        self._recorder = recorder
        self.preempt_signal = preempt or PreemptSignal()
        self.preempt_signal.listen(self._on_preempt)
        self._turn_busy = turn_busy or (lambda: False)
        self.playbook = load_playbook() if playbook == "default" else playbook
        self._estimator = estimator
        self._inner_model = inner_model
        self._run_checks_runner = run_checks_runner
        self._containment = containment
        self._run_checks_enabled = run_checks_enabled
        self._pools = list(pools)
        self._mono = mono
        self._probe = probe or (lambda _point, _it: None)
        self._build = build_child
        self.stop_drain_sec = STOP_DRAIN_SEC if stop_drain_sec is None else float(stop_drain_sec)
        self._lock = threading.Lock()  # leaf: memory only
        self._cond = threading.Condition(self._lock)
        self._current: _Iteration | None = None
        self._starting = False
        self._stopping = False
        self._last: dict[str, dict] = {}
        # Provider-call threads left behind by a stop (a blocked socket cannot be
        # cancelled): while one lives, no new iteration is admitted.
        self._abandoned: list[threading.Thread] = []
        self.abandoned_total = 0
        self._numbers: dict[str, int] = {}
        # W5b: the last iterations' ends (codes, tool names, counters) for the Stage board.
        self._outcomes: deque[tuple[str, dict]] = deque(maxlen=OUTCOMES_KEPT)
        # W6d: the last iterations' ends for the heartbeat start's admission and the cost
        # rows of heartbeat-started ones (memory only, under ``_lock``; bounded).
        self._ends: deque[tuple[str, str, str, float, int]] = deque(maxlen=ENDS_KEPT)
        self._tick_costs: deque[tuple[str, dict]] = deque(maxlen=TICK_COSTS_KEPT)
        # W6a: per grant, the recent outcome fingerprints, the files it wrote (read fresh at
        # every end) and the streaks (memory only, under ``_lock``; a grant never survives a
        # restart, nor does its history).
        self._trackers = outcome.Trackers()
        # W5b: the board's consistency check — a seqlock read without waiting. Every change
        # the board shows that spans several pieces — a call's settle + charge, an
        # iteration's end (charges, the goal and iteration counts on the service, the
        # summary, the outcome row) — runs as a board write. ``_board_seq`` is ONE integer:
        # even = stable, odd = a write is open; it moves when the first open write begins
        # and when the last one ends (writes can overlap: a late settle on the call thread
        # and an end; ``_board_open`` counts them, both under ``_lock``). The reader reads the
        # integer once (odd -> busy), its pieces, then the integer once more (moved -> busy):
        # BoardBusy, never a wait, never a retry, never a mix.
        self._board_seq = 0
        self._board_open = 0
        for name in _PROVIDER_LOGGERS:
            logging.getLogger(name).addFilter(_DEBUG_FILTER)

    # -- admission and start ---------------------------------------------------------------

    def _refuse(self, reason: str) -> GrantRefused:
        return GrantRefused("grant_iteration_refused", reason=reason)

    def _admissible(self, grant: Any, now: Any) -> str | None:
        """Why no effect may run for ``grant`` now (``None``: it may). Pure reads."""
        if self._authority.killed:
            return "killed"
        if self._authority.unhealthy_reason is not None:
            return "authority_unhealthy"
        if self._grants.unhealthy_reason is not None:
            return "store_unhealthy"
        if grant.status != "active":
            return "grant_not_active"
        if grant.goal_status != "active":
            return "goal_not_active"
        if state.halt_reason_for(self._grants.breaker_level()) is not None:
            return "breaker"
        if now is None:
            return "clock"
        if (
            now.wall >= grant.issued_wall + grant.ttl_sec
            or now.mono >= grant.issued_mono + grant.ttl_sec
            or state.ttl_left(grant, now) <= 0
        ):
            return "ttl"
        return None

    def _tool_names(self, grant: Any) -> tuple[str, ...]:
        """scope ∩ the v1 allow-list ∩ the live rule (Never, or no rule reader: absent)."""
        from stiracore.agent.checkpoint_hooks import read_policy

        policy_check = getattr(
            getattr(self._parent(), "checkpoint_hooks", None), "policy_check", None
        )
        names = []
        for name in sorted(set(grant.tools) & TOOLS):
            read = read_policy(policy_check, name)
            if read is not None and read[0] in ("always", "ask"):
                names.append(name)
        return tuple(names)

    def _admit(self, grant_id: Any, tick: Any = None):
        if self.playbook is None:
            raise self._refuse("playbook_unavailable")
        grant, _record = self._grants.executor_record(grant_id)
        code = self._admissible(grant, self._grants.clock_reading())
        if code is not None:
            raise self._refuse(code)
        if tick is not None:
            # W6d: a heartbeat-started press — the wake's stamp and the tick's clauses, read
            # again on this look's record, before anything else here (before the provider).
            try:
                code = tick.recheck(grant)
            except Exception:  # noqa: BLE001 - unreadable: not admitted (fail closed)
                code = "tick_unreadable"
            if code is not None:
                raise self._refuse(code if type(code) is str else "tick_unreadable")
        if grant.iterations >= grant.max_iterations:
            raise self._refuse("max_iterations")
        if grant.used_tokens >= grant.total_tokens:
            raise self._refuse("budget_exhausted")
        if self._estimator is None:
            raise self._refuse("token_count_unavailable")
        try:
            supported = self._provider_supported(self._inner_model(self._parent()))
        except Exception:  # noqa: BLE001 - unreadable: unsupported
            supported = False
        if not supported:
            raise self._refuse("provider_unsupported")
        if not _root_intact_path(grant.workspace_subtree):
            self._grants.block_goal(grant.grant_id, "workspace_changed")
            raise self._refuse("workspace_changed")
        if grant.recheck:
            # A halt resume: the checks above ran now (breaker, budget, TTL); clear it.
            grant = self._grants.satisfy_recheck(grant.grant_id)
            if grant.recheck or self._admissible(grant, self._grants.clock_reading()):
                raise self._refuse("recheck")
        return grant, self._tool_names(grant)

    def step(self, grant_id: Any, *, tick: Any = None) -> dict:
        """A person's press: admit and start exactly one iteration in a server-owned
        thread; returns at once with the structured state. Refused: ``GrantRefused
        ("grant_iteration_refused", reason=<code>)``.

        The pre-emption counter is read **before** the admission: a person's message that
        arrives at any point after it (during the admission, before the thread starts)
        refuses the press or ends the iteration at its first boundary.

        W6d: ``tick`` — the scheduler's ``grant_tick`` presses through this same entry
        (the same permits, budget, tools, pre-emption and commit rules) with the wake's
        stamp (``scheduler.grant_tick.WakeStamp``): its person's-turn counter and authority
        epoch are this iteration's ``seq0`` / ``epoch0`` (read at the wake, so a turn or a
        narrowing since the wake refuses); it is checked on the admission's look
        (``recheck``), again after it (``fresh``: the heartbeat too), **inside** the
        publication's authority fence (``commit_at_epoch(expected=epoch)`` + ``live``,
        memory only) and right before the iteration's first effect (``_consume``: a permit
        of the wake's epoch and ``fresh``). The iteration is then ``origin: tick``: an
        approval it would need is never waited for (``approval_timeout``) and its usage
        counters are kept."""
        if tick is None:
            seq0 = self.preempt_signal.seq
            epoch0 = self._authority.epoch  # W6c: read with seq0 — any narrowing after it moves it
        else:
            seq0, epoch0 = tick.seq, tick.epoch  # the wake's marks (fix round 1)
            if tick.executor is not self or tick.used:
                raise self._refuse("narrowed")  # a stamp for another executor, or reused
        with self._lock:
            if self._stopping:
                raise self._refuse("executor_stopped")
            if self._current is not None or self._starting:
                raise self._refuse("iteration_running")
            if self._live_abandoned():
                raise self._refuse("abandoned_call")
            self._starting = True
        try:
            # (A press calls ``_admit`` exactly as before W6d.)
            grant, tools = self._admit(grant_id) if tick is None else self._admit(grant_id, tick)
            if self.preempt_signal.seq != seq0 or self._busy():
                raise self._refuse("user_turn")
            if tick is not None:
                code = tick.fresh()  # outside every lock: the heartbeat read now
                if code is not None:
                    raise self._refuse(code)
            number = self._numbers.get(grant.grant_id, 0) + 1
            it = _Iteration(
                grant,
                number,
                self._mono() + float(self.settings.iteration_wall_sec),
                seq0,
                IterationBudget(grant.per_tick_tokens, self._pools),
                epoch0,
            )
            it.tools = tools
            it.broker = self._broker
            it.started = self._mono()
            it.origin = "press" if tick is None else "tick"
            it.stamp = tick
            thread = threading.Thread(
                target=self._run,
                args=(it,),
                name=f"grants-iteration-{it.grant_id[:8]}",
                daemon=True,
            )
            it.thread = thread
            with self._lock:
                if self._stopping:
                    raise self._refuse("executor_stopped")
                if tick is None:
                    self._current = it
                    self._numbers[grant.grant_id] = number
                else:
                    # W6d fix rounds 1–2: the wake's stamp and the publication are one step
                    # — under the authority's leaf (lock order: ours → the leaf), memory
                    # only; the stamp is single use.
                    def publish(_epoch: int) -> bool:
                        if tick.used or tick.live(fenced=True) is not None:
                            return False
                        tick.used = True
                        self._current = it
                        self._numbers[grant.grant_id] = number
                        return True

                    fenced, _epoch, done = self._authority.commit_at_epoch(
                        publish, expected=tick.epoch
                    )
                    if not (fenced and done):
                        raise self._refuse(tick.live() or "narrowed")
            self._post(it, "idle", "running", self.playbook.code)
            try:
                thread.start()
            except Exception:  # noqa: BLE001 - e.g. "can't start new thread"
                with self._lock:
                    self._current = None
                raise self._refuse("thread_unavailable") from None
        finally:
            with self._cond:
                self._starting = False
                self._cond.notify_all()
        return self.view(grant.grant_id)

    def _live_abandoned(self) -> int:
        """Under ``_lock``: the abandoned provider-call threads still alive."""
        self._abandoned = [t for t in self._abandoned if t.is_alive()]
        return len(self._abandoned)

    def _abandoned_count(self) -> int:
        with self._lock:
            return self._live_abandoned()

    def _busy(self) -> bool:
        # The arriving mark first, then the lock (W6b): an acquirer is seen in one of the
        # two at every instant from its signal until it releases the lock.
        if self.preempt_signal.pending:
            return True
        try:
            return bool(self._turn_busy())
        except Exception:  # noqa: BLE001 - unreadable: a turn may be running
            return True

    # -- the boundaries --------------------------------------------------------------------

    def _precheck(self, it: _Iteration) -> None:
        """The iteration's own stops (memory reads only)."""
        if it.stop_code is not None:
            raise IterationStop(it.stop_code)
        if self._stopping:
            it.stop("shutdown")
        elif self.preempt_signal.seq != it.seq0 or self._busy():
            # A turn-lock holder signalled (W6b: every one does, through ``TurnLock``), is
            # arriving or holds the lock: the iteration yields.
            it.stop("yielded_user_turn")
        elif self._mono() >= it.deadline:
            it.stop("timeout")
        elif it.stamp is not None and it.stamp.live() is not None:
            # W6d fix round 2: a heartbeat-started iteration — at every boundary the wake's
            # memory-only marks (the switch, the scheduler's run token, the executor, the
            # kill, a person's turn, the authority epoch) must still hold.
            it.stop("stopped", "tick_stale")
        if it.stop_code is not None:
            raise IterationStop(it.stop_code)

    def _look(self, it: _Iteration):
        """One look of the grant service and the reason no effect may run (or None)."""
        try:
            grant, record = self._grants.executor_record(it.grant_id)
        except GrantRefused:
            return None, None, None, "grant_not_active"
        now = self._grants.clock_reading()
        return grant, record, now, self._admissible(grant, now)

    def _authorize(self, it: _Iteration, kind: str, reservation_id: str | None = None):
        """Take a permit for one effect (``kind``: model / tool / wait / report)."""
        self._precheck(it)
        grant, record, _now, code = self._look(it)
        if code is not None:
            it.stop("stopped", code)
            raise IterationStop("stopped")
        self._stamp_gate(it, "permit")  # W6d: right before the first permit is taken
        try:
            return self._authority.issue_permit(
                record,
                kind="goal",
                receipt=it.receipt(kind),
                reservation_id=reservation_id,
                ttl=TtlPair(grant.issued_wall + grant.ttl_sec, grant.issued_mono + grant.ttl_sec),
            )
        except AuthorityError:
            it.stop("stopped", "permit_refused")
            raise IterationStop("stopped") from None

    def _consume(self, it: _Iteration, permit: Any, kind: str, reservation_id: str | None = None):
        """Consume right before the I/O: everything is read again first."""
        self._probe(f"{kind}:authorized", it)
        try:
            self._precheck(it)
        except IterationStop:
            self._authority.void(permit)
            raise
        _grant, record, now, code = self._look(it)
        if code is None:
            try:
                # W6d: the wake's whole record, the last read before the first consume
                self._stamp_gate(it, "consume", permit)
            except IterationStop:
                self._authority.void(permit)
                raise
            try:
                entry = self._authority.consume(
                    permit,
                    record,
                    kind="goal",
                    reservation_id=reservation_id,
                    clock=ClockPair(now.wall, now.mono),
                    track=True,
                    on_cancel=it.on_cancel,
                )
                with it.lock:
                    it.effects += 1
                return entry
            except AuthorityError as error:
                code = error.code if error.code in _DETAIL else "permit_refused"
        self._authority.void(permit)
        it.stop("permit_stale", code)
        raise IterationStop("permit_stale")

    def _stamp_gate(self, it: _Iteration, gate: str, permit: Any = None) -> None:
        """W6d fix rounds 1–2: a heartbeat-started iteration's wake record, read whole
        (:meth:`WakeStamp.fresh`: the memory marks and the heartbeat now) at the **first**
        pass of each gate — the first permit (``permit``), the first consume (``consume``:
        its permit must also be of the wake's epoch; ``consume`` then checks that epoch is
        current, atomically), the first provider request (``request``: right before the
        provider's stream) and the first terminal request (``terminal``: in the client
        proxy's own permit step). After these, :meth:`_precheck` keeps reading the memory
        marks at every boundary. A heartbeat loss after the last read before a request is
        the accepted residual (docs/grants.md)."""
        stamp = it.stamp
        if stamp is None:
            return
        with it.lock:
            first = gate not in it.stamp_gates
            it.stamp_gates.add(gate)
        if not first:
            return
        stale = permit is not None and getattr(permit, "epoch", None) != stamp.epoch
        if stale or stamp.fresh() is not None:
            it.stop("permit_stale", "tick_stale")
            raise IterationStop("permit_stale")
        if gate == "request":
            # Before the first request: the grant look too (breaker, TTL, clocks, state) —
            # a declared-terminal provider's stream is the request itself.
            code = self._look(it)[3]
            if code is not None:
                it.stop("permit_stale", code)
                raise IterationStop("permit_stale")

    def _stopped_since(self, it: _Iteration) -> bool:
        """After an effect: did a stop arrive while it ran (no exception, no I/O beyond
        the look)?"""
        if it.stop_code is not None or self._stopping:
            return True
        if self.preempt_signal.seq != it.seq0 or self._mono() >= it.deadline:
            return True
        return self._look(it)[3] is not None

    # -- the model call's pieces -----------------------------------------------------------

    def _context(self, messages: Any, it: _Iteration) -> list:
        """A copy of ``messages`` whose latest user message ends with the context block
        (the trusted playbook, then the goal). The history itself is never changed."""
        from stiracore.tools.delegate_tool import neutralize_child_markers

        goal = neutralize_child_markers(self._grants.executor_goal(it.grant_id))
        block = (
            f"{_CONTEXT_OPEN}\n{self.playbook.text}\n\nThe goal (from the operator; the only "
            f"goal):\n{goal}\n{_CONTEXT_CLOSE}"
        )
        final = list(messages or [])
        for index in range(len(final) - 1, -1, -1):
            message = final[index]
            if isinstance(message, Mapping) and message.get("role") == "user":
                content = list(message.get("content") or [])
                final[index] = {**message, "content": [*content, {"text": block}]}
                return final
        it.stop("model_error")
        raise IterationStop("model_error")

    @staticmethod
    def _provider_supported(inner: Any) -> bool:
        """Only providers whose every terminal request is guarded: a Bedrock model (the
        copy's client is proxied), or a model that **declares** that its ``stream`` makes
        exactly one request through the permit (``stira_grant_terminal = True``, the test
        models). Everything else — also the Strands providers with a lazy per-call client
        (OpenAI, Ollama, LiteLLM, Gemini, Mistral, …) — is refused (``provider_unsupported``)."""
        try:
            from strands.models.bedrock import BedrockModel
        except ImportError:  # pragma: no cover
            BedrockModel = ()
        if BedrockModel and isinstance(inner, BedrockModel):
            return True
        return getattr(inner, "stira_grant_terminal", False) is True and callable(
            getattr(inner, "format_request", None)
        )

    def _child_model(self, inner: Any, it: _Iteration) -> Any:
        """The iteration's own copy of the provider: the chat agent's model object is never
        changed. A Bedrock model's copy gets the guarded client."""
        try:
            from strands.models.bedrock import BedrockModel
        except ImportError:  # pragma: no cover
            BedrockModel = ()
        if BedrockModel and isinstance(inner, BedrockModel):
            import copy

            model = copy.copy(inner)
            model.config = dict(inner.config)
            model.client = _GuardedClient(inner.client, self, it)
            return model
        if not self._provider_supported(inner):
            it.stop("model_error", "provider_unsupported")
            raise IterationStop("model_error")
        return inner

    def _terminal(self, it: _Iteration) -> None:
        """Right before every terminal provider request: the stops, the grant, a permit."""
        self._precheck(it)
        if it.abandoned:
            it.stop("stopped", "in_flight_cancelled")
            raise IterationStop("stopped")
        permit = self._authorize(it, "request")
        self._probe("request:authorized", it)
        _grant, record, now, code = self._look(it)
        if code is None:
            try:
                self._precheck(it)
                self._stamp_gate(it, "terminal", permit)  # W6d: the first terminal request
                self._authority.consume(
                    permit, record, kind="goal", clock=ClockPair(now.wall, now.mono)
                )
                return
            except AuthorityError as error:
                code = error.code if error.code in _DETAIL else "permit_refused"
            except IterationStop:
                self._authority.void(permit)
                raise
        self._authority.void(permit)
        it.stop("permit_stale", code)
        raise IterationStop("permit_stale")

    def _format(self, it, inner, messages, tool_specs, system_prompt, kwargs) -> Mapping:
        """The final request, formatted by the model's own formatter (no formatter, or one
        that fails: no call)."""
        content = kwargs.get("system_prompt_content")
        if content is None and system_prompt:
            content = [{"text": system_prompt}]
        try:
            request = inner.format_request(messages, tool_specs, content, kwargs.get("tool_choice"))
        except Exception:  # noqa: BLE001 - an unsizable request is never sent
            request = None
        if isinstance(request, Mapping):
            return request
        it.stop("token_count_unavailable", "no_formatter")
        raise IterationStop("token_count_unavailable")

    def _count(self, it, request: Mapping) -> int:
        """The local upper bound of the final request (no provider request)."""
        detail = "no_counter"
        try:
            if self._estimator is not None:
                tokens = self._estimator(request)
                if type(tokens) is int and tokens >= 0:
                    return tokens
                detail = "count_failed"
        except Exception:  # noqa: BLE001 - no usable bound: no call
            detail = "count_failed"
        it.stop("token_count_unavailable", detail)
        raise IterationStop("token_count_unavailable")

    def _max_output(self, it: _Iteration, request: Mapping) -> int:
        """The output limit of the formatted request (an override included)."""
        value = formatted_max_tokens(request)
        if value is not None:
            return value
        it.stop("token_count_unavailable", "max_tokens_unknown")
        raise IterationStop("token_count_unavailable")

    def _reserve(self, it: _Iteration, need: int):
        grant, _record, _now, code = self._look(it)
        if code is not None:
            it.stop("stopped", code)
            raise IterationStop("stopped")
        try:
            return it.budget.reserve(need, used=grant.used_tokens, total=grant.total_tokens)
        except IterationStop:
            it.stop("budget_refused", it.budget.refusal)
            raise

    def _settle(self, it: _Iteration, reservation: Any, tokens: int) -> bool:
        """Settle one reservation (once); False: it had ended already (nothing changed)."""
        with self._board_write():  # the tick's tokens and the grant's budget move together
            if not it.budget.settle(reservation, int(tokens)):
                return False
            try:
                self._grants.charge(it.grant_id, int(tokens))
            except GrantRefused:
                logger.warning("grants: an iteration's usage could not be charged")
            return True

    # -- the tools' pieces -----------------------------------------------------------------

    @staticmethod
    def _read_only(arguments: Mapping) -> bool:
        return arguments.get("command") in ("view", "find_line")

    def _policy(self) -> Any:
        return getattr(self._parent(), "write_policy", None)

    def _root_intact(self, it: _Iteration) -> bool:
        """The grant's subtree is still the directory resolved at issue: a real directory
        (no symlink) whose resolved path is the stored one. Never re-resolved into a new
        authority."""
        return _root_intact_path(it.subtree)

    def _require_root(self, it: _Iteration) -> None:
        if not self._root_intact(it):
            it.stop("workspace_changed")
            raise IterationStop("workspace_changed")

    def _absolute(self, raw: Any) -> str | None:
        """The absolute form of a tool's path argument (``None``: not a usable path)."""
        from stiracore.tools.file_editor_tool import from_editor_path

        if not isinstance(raw, str) or not raw.strip() or "\0" in raw:
            return None
        policy = self._policy()
        native = from_editor_path(raw)
        absolute = str(policy.absolute(native)) if policy is not None else native
        return absolute if os.path.isabs(absolute) else None

    def _inside(self, it: _Iteration, raw: Any) -> None:
        """``raw`` resolves (symlinks followed) inside the grant's subtree, and is no
        symlink itself; otherwise the call is refused (``tool_refused``)."""
        absolute = self._absolute(raw)
        if absolute is None:
            it.stop("tool_refused", "path_invalid")
            raise IterationStop("tool_refused")
        root = it.subtree  # the issue-time resolved root (never a fresh realpath)
        resolved = os.path.realpath(absolute)
        if not (resolved == root or resolved.startswith(root + os.sep)) or os.path.islink(absolute):
            it.stop("tool_refused", "outside_subtree")
            raise IterationStop("tool_refused")

    def _host(self) -> Any:
        if self._containment is not None:
            return self._containment()
        from stiracore.tools import run_checks

        return run_checks.containment()

    def _check_tool(self, it: _Iteration, name: str, arguments: Mapping) -> Any:
        """The per-tool scope checks (run before the permit and again after the consume).
        Returns the verified containment for ``run_checks``."""
        if name not in it.tools:
            it.stop("tool_refused", "not_in_scope")
            raise IterationStop("tool_refused")
        if name in ("editor", "run_checks"):
            self._require_root(it)
        if name == "editor":
            self._inside(it, arguments.get("path"))
            return None
        if name != "run_checks":
            return None
        detail = ""
        if not self.settings.run_checks_unattended:
            detail = "setting_off"
        else:
            try:
                if self._run_checks_enabled is not None:
                    enabled = bool(self._run_checks_enabled())
                else:
                    from stiracore.tools import run_checks

                    enabled = run_checks.is_enabled()
                host = self._host()
                contained = bool(getattr(host, "verified", False))
            except Exception:  # noqa: BLE001 - unreadable: not contained
                enabled, host, contained = False, None, False
            if not enabled:
                detail = "disabled"
            elif not contained:
                detail = "not_contained"
        if detail:
            it.stop("run_checks_unavailable", detail)
            raise IterationStop("run_checks_unavailable")
        path = arguments.get("path", ".")
        policy = self._policy()
        workspace = getattr(policy, "workspace", None)
        if isinstance(path, str) and not os.path.isabs(path) and workspace is not None:
            path = str(Path(workspace) / path)
        self._inside(it, path)
        return host

    def _run_checks(self, inner: Any, arguments: Mapping, host: Any) -> dict:
        runner = self._run_checks_runner
        if runner is None:
            from stiracore.tools import run_checks

            runner = run_checks.run
        policy = getattr(inner, "policy", None) or self._policy()
        return runner(dict(arguments), policy, host=host)

    @staticmethod
    def _check_result(tool_use: Mapping, result: Mapping) -> dict:
        try:
            from stiracore.tools.run_checks import _tool_result

            return _tool_result(tool_use.get("toolUseId"), result)
        except Exception:  # noqa: BLE001
            status = "error" if result.get("status") in ("refused", "error") else "success"
            return {
                "toolUseId": tool_use.get("toolUseId"),
                "status": status,
                "content": [{"text": str(result.get("status"))}],
            }

    def _apply_report(self, it: _Iteration, kind: str, code: str) -> None:
        if kind == "progress":
            with it.lock:
                it.report, it.report_kind = code, kind
            return
        if kind == "claim_complete":
            # → awaiting_verification, never further; W6c: with this claim's evidence record
            view = self._grants.claim_complete(it.grant_id)
            evidence = view.get("claim_evidence") if isinstance(view, Mapping) else None
            claimed_at = evidence.get("claimed_at") if isinstance(evidence, Mapping) else None
            with it.lock:
                it.report, it.report_kind = code, kind
                it.claimed_at = claimed_at if type(claimed_at) is float else None
            it.stop("claimed")
            return
        # W6a: the goal is blocked at the iteration's end, once its outcome is committed
        # (``_count_outcome``): with the report's code, or ``same_blocker`` when the
        # detector fires — never for an unknown or interrupted outcome.
        with it.lock:
            it.report, it.report_kind = code, kind
        it.stop("reported_blocked")

    # -- W6a: the outcome of an iteration (memory only; reads with no lock held) -----------

    def _tracker(self, grant_id: str) -> outcome.ProgressTracker:
        """The grant's outcome tracker; a new one (its key drawn from the OS) is made
        outside the leaf lock. The trackers of live grants are never evicted."""
        with self._lock:
            if self._trackers.find(grant_id) is not None:
                return self._trackers.get(grant_id)  # an existing one: now the most recent
        fresh = outcome.ProgressTracker()
        try:
            keep = self._grants.live_ids()  # the service's lock, never under ours
        except Exception:  # noqa: BLE001 - unreadable: keep every tracker known now
            with self._lock:
                keep = self._trackers.ids()
        with self._lock:
            return self._trackers.get(grant_id, fresh, keep | {grant_id})

    def _before_write(self, it: _Iteration, arguments: Mapping) -> str | None:
        """Right before an editor write (after its consume): the file's digest before the
        iteration's first write to it. I/O on the tool's thread, no lock held. Anything
        unreadable marks the iteration ``unreadable`` (no progress). W6c: a write makes
        every check before it stale (and one that overlaps it never counts)."""
        with it.lock:
            it.claim_checks, it.wrote_any = None, True
            it.write_seq += 1
            it.writing += 1
        absolute = self._absolute(arguments.get("path"))
        target = os.path.realpath(absolute) if absolute is not None else None
        with it.lock:
            known = target is not None and target in it.pre
        if target is None or known:
            if target is None:
                with it.lock:
                    it.unreadable = True
            return target
        before = outcome.file_identity(target, it.read_budget)
        with it.lock:
            if before is None or len(it.pre) >= WRITTEN_KEPT:
                it.unreadable = True
            else:
                it.pre.setdefault(target, before)
        return target

    @staticmethod
    def _wrote(it: _Iteration, target: str | None) -> None:
        with it.lock:
            if target is None or target not in it.pre:
                it.unreadable = True  # its before-digest is unknown: the outcome is unknown
            if target is not None and target not in it.wrote and len(it.wrote) < WRITTEN_KEPT:
                it.wrote.append(target)  # tracked and read fresh from the next end on

    @staticmethod
    def _write_ended(it: _Iteration) -> None:
        """W6c: an editor write ended (whatever its result): a check that overlapped it
        never counts (its write sequence moved), later ones may again."""
        with it.lock:
            it.write_seq += 1
            it.writing = max(0, it.writing - 1)

    @staticmethod
    def _checks_started(it: _Iteration) -> tuple[int, bool]:
        """W6c: a run_checks call starts (past its consume): the write sequence now and
        whether a write is in flight (its result counts only if neither moved)."""
        with it.lock:
            return it.write_seq, it.writing == 0

    @staticmethod
    def _claim_call(it: _Iteration, result: Any, token: tuple[int, bool]) -> None:
        """W6c: one run_checks call's verdict (:func:`~.progress.claim_checks`; a runner
        that raised: ``unknown``) joins the iteration's combined verdict
        (:func:`~.progress.combine_checks`) — only when no editor write started or ended
        while it ran (otherwise it describes files that are no longer there)."""
        try:
            code = outcome.claim_checks(result) if result is not None else ("unknown", ())
        except Exception:  # noqa: BLE001 - unreadable: unknown, never pass
            code = ("unknown", ())
        seq, quiet = token
        with it.lock:
            if quiet and it.write_seq == seq and it.writing == 0:
                it.claim_checks = outcome.combine_checks(it.claim_checks, code)

    @staticmethod
    def _observe_checks(it: _Iteration, result: Any) -> None:
        try:
            normalized = outcome.normalize_checks(result)
        except Exception:  # noqa: BLE001 - an unreadable result is never progress
            normalized, failed = None, True
        else:
            failed = False
        with it.lock:
            if failed:
                it.unreadable = True
            elif normalized is not None:
                it.check_outcomes.update(normalized)
                if len(it.check_outcomes) > CHECKS_KEPT:
                    it.unreadable = True
                    it.check_outcomes = set(list(it.check_outcomes)[:CHECKS_KEPT])

    def _interrupted(self, it: _Iteration) -> bool:
        """Read again at the end and at the commit (after the digest reads): an end in
        :data:`~.progress.INTERRUPTED_ENDS`, an abandoned call, shutdown, the kill, or a
        person's turn since the start — never a no-progress tick."""
        with it.lock:
            end, abandoned = it.stop_code, it.abandoned
        if end in outcome.INTERRUPTED_ENDS or abandoned:
            return True
        if self._stopping or self._authority.killed:
            return True
        return self.preempt_signal.seq != it.seq0 or self._busy()

    def _observe(self, it: _Iteration) -> _Outcome:
        """At the iteration's end, before the board write (I/O, no lock held): whether it
        was interrupted, and its outcome over a **fresh read** of every file the grant has
        written (a missing one an explicit marker) and the iteration's check rows —
        ``unknown`` (something could not be read: never progress), ``none`` (no check and
        the workspace identity is the last known one: only reading, a same-bytes rewrite),
        or the fingerprint. A change by anyone (the agent, a person) counts."""
        interrupted = self._interrupted(it)
        with it.lock:
            unreadable = it.unreadable
            pre, wrote = dict(it.pre), list(it.wrote)
            rows = list(it.check_outcomes)
            it.pre, it.wrote, it.check_outcomes = {}, [], set()
        tracker = self._tracker(it.grant_id)
        # W6-H: not observed — the paths it wrote are tracked from their before-write bytes
        seeds = {p: (os.path.relpath(p, it.subtree), pre[p]) for p in wrote if p in pre}
        kept: dict = {}  # W6-H: the cache entries validated so far stay, even when unknown
        unknown = _Outcome(False, "unknown", None, wrote, kept, None, seeds=seeds)
        if interrupted:  # not read: the paths it wrote are read fresh next time
            return _Outcome(True, "unknown", None, wrote, {}, None, seeds=seeds)
        if unreadable:
            return unknown
        with self._lock:
            cache, last = tracker.cache(), tracker.last_identity
        first = outcome.read_files(list(dict.fromkeys(wrote)), cache, it.read_budget, kept)
        if first is None:
            return unknown
        digests = first[0]
        changed = [path for path in wrote if digests[path] != pre.get(path)]
        with self._lock:
            retained = tracker.retained(changed)  # the files about to leave are not read
        rest = outcome.read_files(
            [p for p in retained if p not in digests], cache, it.read_budget, kept
        )
        if rest is None:
            return unknown
        digests.update(rest[0])
        basis = {p: (os.path.relpath(p, it.subtree), digests[p]) for p in retained}
        try:
            identity = tracker.identity(basis.values())
            if not rows and identity == last:
                return _Outcome(False, "none", None, changed, kept, identity, basis)
            fingerprint = tracker.fingerprint(identity, rows)
        except Exception:  # noqa: BLE001 - no fingerprint: unknown, never progress
            return unknown
        return _Outcome(False, "known", fingerprint, changed, kept, identity, basis)

    # -- the approval wait (an Ask answered by a person's card) ----------------------------

    def _approver(self, it: _Iteration) -> Callable[[Any], Any]:
        from stiracore.core import hitl_reasons
        from stiracore.core.engine import HITLAction, HITLDecision

        def approve(request: Any) -> Any:
            if it.origin == "tick":
                # W6d: nobody watches a heartbeat-started iteration — no card, no wait, no
                # self-approval: it ends ``approval_timeout`` (the goal blocks; a person
                # resumes it or presses).
                it.stop("approval_timeout")
                return HITLDecision(HITLAction.REJECT, reason="the grant iteration stopped")
            if self._broker is None:
                return HITLDecision(HITLAction.REJECT, reason="no approver is connected")
            try:
                permit = self._authorize(it, "wait")
                entry = self._consume(it, permit, "wait")
            except IterationStop:
                return HITLDecision(HITLAction.REJECT, reason="the grant iteration stopped")
            with it.lock:
                it.card = request.request_id
            try:
                if it.stop_code is not None:
                    decision = HITLDecision(HITLAction.REJECT, reason="the grant iteration stopped")
                else:
                    decision = self._broker.request(
                        request, durable_display=self._card_summary(it, request)
                    )
            finally:
                with it.lock:
                    it.card = None
                entry.done()
            it.note("waits")
            if decision.action == HITLAction.REJECT and decision.reason == hitl_reasons.EXPIRED:
                it.stop("approval_timeout")
            return decision

        return approve

    @staticmethod
    def _card_summary(it: _Iteration, request: Any) -> dict:
        """What the durable approval record keeps of a card: **no model-chosen string** —
        the tool (an allow-list name), whether the path is inside the subtree, digest
        prefixes of the path and of the arguments, their size, the grant. The arguments and
        the path stay in the live card in memory."""
        import json

        tool_input = getattr(request, "tool_input", None)
        try:
            raw = json.dumps(tool_input, sort_keys=True, ensure_ascii=False).encode("utf-8")
        except (TypeError, ValueError):
            raw = b""
        path = tool_input.get("path") if isinstance(tool_input, Mapping) else None
        inside = False
        path_digest = ""
        if isinstance(path, str) and path:
            absolute = os.path.normpath(
                path if os.path.isabs(path) else os.path.join(it.subtree, path)
            )
            rel = os.path.relpath(absolute, it.subtree)
            inside = not (rel == ".." or rel.startswith(".." + os.sep))
            path_digest = hashlib.sha256(path.encode("utf-8", "replace")).hexdigest()[:12]
        tool = str(getattr(request, "tool_name", ""))
        return {
            "grant_id": it.grant_id,
            "tool": tool if tool in TOOLS else "other",
            "inside": inside,
            "path_sha256": path_digest,
            "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest()[:16],
        }

    # -- the run ---------------------------------------------------------------------------

    def _rule_reader(self, it: _Iteration) -> Callable[[str], Any]:
        parent_check = getattr(
            getattr(self._parent(), "checkpoint_hooks", None), "policy_check", None
        )

        def rule_of(name: str) -> Any:
            if name == REPORT_TOOL:
                return "always"
            if name not in it.tools or parent_check is None:
                return "never"
            return parent_check(name)

        return rule_of

    def _child_tools(self, it: _Iteration) -> list[Any]:
        wanted = set(it.tools)
        tools, seen = [], set()
        for tool in self._tool_provider(sorted(wanted)) or ():
            name = getattr(tool, "tool_name", None)
            if name in wanted and name not in seen:
                seen.add(name)
                tools.append(GrantTool(tool, self, it))
        with it.lock:
            it.tools = tuple(sorted(seen))
        return [*tools, GoalReportTool(self, it)]

    def _make_child(self, it: _Iteration) -> Any:
        parent = self._parent()
        model = GrantModel(self._child_model(self._inner_model(parent), it), self, it)
        kwargs = {
            "session_ref": it.session_ref,
            "model": model,
            "tools": self._child_tools(it),
            "system_prompt": GRANT_SYSTEM_PROMPT,
            "rule_of": self._rule_reader(it),
            "write_roots": [it.subtree],
            "approver": self._approver(it),
            "hitl_timeout_sec": max(
                1.0,
                min(float(parent.config.hitl_timeout_sec), it.deadline - self._mono()),
            ),
            "pre_hooks": [IterationGuard(self, it)],
            "input_preparers": dict(
                getattr(getattr(parent, "checkpoint_hooks", None), "input_preparers", {}) or {}
            ),
        }
        if self._build is not None:
            return self._build(parent, **kwargs)
        from stiracore.agent.grant_child import create_grant_child

        return create_grant_child(parent, **kwargs)

    def _run(self, it: _Iteration) -> None:
        """The iteration thread: it owns the iteration's state. The child runs on its own
        provider-call thread; this thread waits for it **or** a stop (the in-flight
        cancel, a person's turn at a card, the wall time, shutdown) and ends promptly
        either way. A call thread still blocked after a stop (a socket Python cannot
        cancel) is abandoned: every later boundary in it refuses (no effect, no state
        change), its reservations stay charged, and no new iteration is admitted while it
        lives."""
        try:
            child = self._make_child(it)
            with it.lock:
                it.child = child
            self._precheck(it)
            worker = threading.Thread(
                target=self._call,
                args=(it, child),
                name=f"grants-call-{it.grant_id[:8]}",
                daemon=True,
            )
            it.worker = worker
            worker.start()
            while not it.call_done.is_set():
                remaining = it.deadline - self._mono()
                if remaining <= 0:
                    it.stop("timeout")
                it.wake.wait(timeout=max(0.01, remaining))
                it.wake.clear()
                if it.stop_code is not None and not it.call_done.is_set():
                    if not self._await_call(it):
                        with self._lock:
                            self._abandoned.append(worker)
                            self.abandoned_total += 1
                        with it.lock:
                            it.abandoned = True
                            it.residual.append("abandoned_call")
                    break
        except IterationStop:
            pass
        except Exception as error:  # noqa: BLE001 - codes only, never the message
            if it.stop("failed", "child_error"):
                logger.warning("grants: an iteration failed (%s)", type(error).__name__)
        finally:
            self._finish(it)

    def _await_call(self, it: _Iteration) -> bool:
        """#640 (W6-F), after a stop: wait for the provider-call thread — the grace, and
        while a model call is reading its response also up to ``stop_drain_sec`` more, so a
        call that completes reports its usage (charged instead of the whole reservation).
        No drain for shutdown or the wall time. A call still running then is given up: its
        provider request is told to stop (``abort``) and it is abandoned (False). Every
        boundary in the call thread still refuses meanwhile (no effect after the stop)."""
        with it.lock:
            drain = it.model_reading > 0 and it.stop_code not in ("shutdown", "timeout")
        if not drain or self._stopping:
            it.abort.set()
        if it.call_done.wait(ABANDON_GRACE_SEC):
            return True
        if drain:
            until = time.monotonic() + self.stop_drain_sec
            while not self._stopping:
                left = until - time.monotonic()
                if left <= 0:
                    break
                if it.call_done.wait(min(left, 0.05)):
                    return True
        it.abort.set()
        return it.call_done.is_set()

    def _call(self, it: _Iteration, child: Any) -> None:
        """The provider-call thread: the child's invocation (every effect in it takes
        its own permit)."""
        try:
            child.agent(ITERATION_PROMPT, cancel_signal=it.cancel_event)
        except IterationStop:
            pass
        except Exception as error:  # noqa: BLE001 - codes only, never the message
            if it.stop("failed", "child_error"):
                logger.warning("grants: an iteration failed (%s)", type(error).__name__)
        finally:
            it.call_done.set()
            it.wake.set()

    def _finish(self, it: _Iteration) -> None:
        with it.lock:
            if it.stop_code is None and it.counts["tool_calls"] == 0 and not it.counts["reports"]:
                it.stop_code = "no_action"  # W6a: no tool call, no report (ADR 0011 §11)
        try:
            observed = self._observe(it)
        except Exception:  # noqa: BLE001 - fail closed: no progress
            logger.warning("grants: an iteration's outcome could not be read")
            observed = _Outcome(False, "unknown", None, [], {}, None)
        # W5b fix round 2: one board write — the charges, the goal and iteration counts on
        # the service, the summary and the outcome row change together (never shown mixed).
        with self._board_write():
            self._finish_writes(it, observed)

    def _count_outcome(self, it: _Iteration, end: str, observed: _Outcome) -> tuple[str, Any]:
        """W6a: count the iteration by its outcome (the commit: after the digest reads,
        outside the leaf lock).

        * **No effect consumed** (whatever the end): not counted at all — no iteration, no
          streak, no history (a plain end; the person presses again).
        * **Interrupted** — read again here: a stop flag, a person's turn, the kill, or the
          grant not ``active`` / stopped by anyone since the start: toward
          ``max_iterations`` only, never a streak or the history.
        * **Unknown** (something unreadable): no progress; not in the history; no other
          streak changes.
        * Otherwise ``progress`` = a new fingerprint, and the detectors (loop, no action,
          same blocker) may block the goal.

        The iteration's **own** block (its blocked report, :data:`BLOCK_FOR_END`) is
        applied here, after the validation: so a stop by anyone else is never mistaken for
        it, and ``same_blocker`` fires only for a known, committed outcome.

        W6-H (#632-2): the validation reads the grant's version stamp (``generation``);
        the own block and the count are then **one** service step that compares it
        (:meth:`GrantService.commit_iteration`). A change by anyone in between (a person's
        block, a pause) makes it an interruption there, and only a committed count enters
        the history and the streaks.

        Returns ``(progress code, fence)`` for W6c: the iteration's outcome class (``new`` /
        ``repeat`` / ``none``, or ``unknown`` for an interrupted, unknown or uncounted one)
        and the :class:`ClaimFence` of its **committed** count — the grant's version after
        it, and the authority epoch and the person-turn mark read when the iteration
        **started** (None: not committed — an interruption, a change after the validation,
        or no count). A claim's evidence is published only if all three still hold at the
        publication itself: a narrowing or a person's turn anywhere since the start (also
        after the claim, during the end's reads) publishes nothing."""
        if it.effects == 0:
            return "unknown", None
        epoch = it.epoch0
        stamp = None
        if not (observed.interrupted or self._interrupted(it)):
            stamp = self._own_state(it, end)
        tracker = self._tracker(it.grant_id)
        with self._lock:
            tracker.adopt(observed.added, observed.cache, observed.seeds)
        if stamp is None:  # interrupted
            self._own_block(it, end)
            self._grants.count_iteration(it.grant_id, progress=None)
            return "unknown", None
        plain = self._block_code(it, end)
        if observed.kind == "unknown":
            committed, counted = self._commit(it, stamp, False, plain, plain)
            return "unknown", self._fence(it, counted, epoch) if committed else None
        fingerprint = observed.fingerprint
        with it.lock:
            report = (it.report_kind, it.report) if it.report_kind else ("", "")
        with self._lock:
            code = tracker.classify(fingerprint)
            progress = code == "new"
            reason = tracker.blocker_code(report[1]) if end == "reported_blocked" else None
        committed, counted = self._commit(
            it, stamp, progress, self._block_code(it, end, reason), plain
        )
        if not committed:  # someone changed the grant after the validation
            return "unknown", None
        with self._lock:
            _code, rule = tracker.record(
                end, report, fingerprint, counted.no_progress, observed.identity, observed.basis
            )
        if rule and end != "claimed":  # a claim goes to the person who verifies it
            self._grants.block_goal(it.grant_id, rule, stamp=counted.generation)
        return code, self._fence(it, counted, epoch)

    def _fence(self, it: _Iteration, counted: Any, epoch: int) -> ClaimFence:
        return ClaimFence(counted.generation, epoch, self.preempt_signal, it.seq0)

    def _commit(self, it: _Iteration, stamp: int, progress: bool, block, plain):
        committed = self._grants.commit_iteration(
            it.grant_id, stamp=stamp, progress=progress, block=block, plain=plain
        )
        with it.lock:
            it.own_blocked = True  # applied in the same step (or nothing to apply)
        return committed

    def _block_code(self, it: _Iteration, end: str, reason: str | None = None) -> str | None:
        """The iteration's own block code (``reason``: the report's code or
        ``same_blocker``), or ``None`` (none for this end, or applied already)."""
        code = (reason or it.report) if end == "reported_blocked" else BLOCK_FOR_END.get(end)
        with it.lock:
            return None if it.own_blocked else code

    def _claim_evidence(
        self, it: _Iteration, end: str, observed: _Outcome, progress: str, fence: Any
    ) -> None:
        """W6c, at every iteration's end (inside its board write): the grant's checks so far
        (``merge_checks`` with the identity this end read, whatever the end); for this
        iteration's own claim, the measured codes of its evidence record — the checks
        verdict bound to the claim's workspace identity, the workspace against the previous
        claim's, the outcome class ``progress`` — published only for a committed count
        (``fence``: :class:`ClaimFence`, checked again by the service at the publication).
        Codes the harness observed, never a text of the model; I/O-free (the reads were
        ``_observe``'s)."""
        with it.lock:
            checks, wrote, claimed_at = it.claim_checks, it.wrote_any, it.claimed_at
        tracker = self._tracker(it.grant_id)
        with self._lock:
            tracker.merge_checks(checks, wrote, observed.identity)
            if end != "claimed" or claimed_at is None:
                return
            codes, rows, workspace = tracker.claim_codes(observed.identity)
        if fence is None:
            return  # not committed (interrupted): the record keeps its unknown codes
        self._grants.measure_claim(
            it.grant_id,
            claimed_at,
            fence=fence,
            checks=codes,
            rows=rows,
            workspace=workspace,
            progress=progress,
        )

    def _own_block(self, it: _Iteration, end: str, reason: str | None = None) -> None:
        """The iteration's own block, once: its blocked report (``reason``: the report's
        code, or ``same_blocker`` from the committed detector) or :data:`BLOCK_FOR_END`."""
        code = self._block_code(it, end, reason)
        with it.lock:
            if code is None or it.own_blocked:
                return
            it.own_blocked = True
        self._grants.block_goal(it.grant_id, code)

    def _own_state(self, it: _Iteration, end: str) -> int | None:
        """At the commit (before the iteration's own block): is the grant still as this
        iteration left it — ``active``, no stop by anyone since the start (its
        ``stop_epoch``), the goal ``active`` (or ``awaiting_verification`` after its own
        claim)? Its version stamp (``generation``, compared again inside the commit's
        step); ``None`` for anything else or an unreadable grant: an interruption."""
        try:
            grant, _record = self._grants.executor_record(it.grant_id)
        except Exception:  # noqa: BLE001 - unreadable: an interruption (fail closed)
            return None
        if grant.status != "active" or grant.stop_epoch != it.stop_epoch0:
            return None
        goal = grant.goal_status
        if goal == "active" or (end == "claimed" and goal == "awaiting_verification"):
            return grant.generation
        return None

    def _finish_writes(self, it: _Iteration, observed: _Outcome) -> None:
        try:
            # A call still open (abandoned, or an exit before its settle): its whole
            # reservation stays charged (unknown outcome); a late settle is a no-op.
            for tokens in it.budget.settle_open():
                try:
                    self._grants.charge(it.grant_id, tokens)
                except GrantRefused:
                    logger.warning("grants: an open reservation could not be charged")
            end = it.stop_code or "done"
            progress, fence = "unknown", None
            try:
                progress, fence = self._count_outcome(it, end, observed)
            except Exception:  # noqa: BLE001 - the grant may have ended meanwhile
                logger.warning("grants: an iteration's end could not be recorded")
            try:
                self._own_block(it, end)  # if the commit did not apply it: the plain code
            except Exception:  # noqa: BLE001
                logger.warning("grants: an iteration's block could not be recorded")
            try:
                self._claim_evidence(it, end, observed, progress, fence)
            except Exception:  # noqa: BLE001 - the record keeps its unknown codes
                logger.warning("grants: a claim's evidence could not be recorded")
            final = "yielded" if end == "yielded_user_turn" else "idle"
            code = "iteration_timeout" if end == "timeout" else f"iteration_{end}"
            self._post(it, "running", final, code)
        finally:
            with it.lock:
                it.state = "yielded" if it.stop_code == "yielded_user_turn" else "idle"
                if it.stop_code is None:
                    it.stop_code = "done"
                it.child = None  # the transcript goes with it (in memory only)
            try:
                outcome = self._outcome(it)
            except Exception:  # noqa: BLE001 - the board's row never keeps the executor busy
                logger.warning("grants: an iteration's board row could not be built")
                outcome = None
            ended, cost = self._end_row(it, outcome)
            with self._cond:
                self._last[it.grant_id] = it.summary()
                if outcome is not None:
                    self._outcomes.append((it.grant_id, outcome))
                # W6d: every end (any origin) — the spacing and the no-action rule of the
                # heartbeat start read these; an unreadable clock leaves no end time (the
                # spacing then refuses: unknown is a skip).
                with it.lock:
                    end_code, origin, epoch0 = it.stop_code or "done", it.origin, it.stop_epoch0
                self._ends.append((it.grant_id, origin, end_code, ended, epoch0))
                if cost is not None:
                    self._tick_costs.append((it.grant_id, cost))
                if self._current is it:
                    self._current = None
                self._cond.notify_all()

    def _end_row(self, it: _Iteration, outcome: dict | None) -> tuple[float, dict | None]:
        """W6d, outside every lock: the end time (the executor's mono clock; NaN when it
        cannot be read) and, for a heartbeat-started iteration, its cost row — numbers
        only: model calls, the reported tokens (cache counters only when reported) and
        the wall time."""
        try:
            ended = float(self._mono())
        except Exception:  # noqa: BLE001 - no end time: the spacing refuses (unknown)
            ended = math.nan
        if it.origin != "tick":
            return ended, None
        with it.lock:
            cost = {"number": it.number, "model_calls": it.counts["model_calls"], **it.usage}
        if isinstance(outcome, dict) and type(outcome.get("duration_ms")) is int:
            cost["wall_ms"] = outcome["duration_ms"]
        return ended, cost

    def tick_facts(self, grant_id: Any) -> dict:
        """W6d: what the scheduler's ``grant_tick`` reads of the executor (memory only, no
        callback under ``_lock``): an iteration running or starting, live abandoned
        provider calls, a person's turn arriving or holding the lock, the stop flag, and
        this grant's last ends ``(origin, end, ended, stop_epoch)``, newest last, with the
        executor's clock reading now."""
        with self._lock:
            running = self._current is not None or self._starting
            abandoned = self._live_abandoned()
            stopping = self._stopping
            ends = [e[1:] for e in self._ends if e[0] == grant_id]
        try:
            now = float(self._mono())
        except Exception:  # noqa: BLE001 - unreadable: the spacing refuses
            now = math.nan
        return {
            "running": running,
            "abandoned": abandoned,
            "turn_busy": self._busy(),
            "stopping": stopping,
            "ends": ends,
            "now": now,
        }

    def tick_costs(self, grant_id: Any) -> list[dict]:
        """W6d: the cost rows of this grant's last heartbeat-started iterations (numbers
        only, oldest first)."""
        with self._lock:
            return [dict(c) for g, c in self._tick_costs if g == grant_id]

    def _post(self, it: _Iteration, before: str, after: str, code: str) -> None:
        recorder = self._recorder
        if recorder is None:
            return
        from stiracore.authority.lifecycle_events import make_event

        try:
            recorder.post(
                make_event(
                    "grant_iteration",
                    it.grant_id,
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

    # -- views, the watchdog look, pre-emption, shutdown -----------------------------------

    def view(self, grant_id: Any) -> dict:
        """The iteration state of one grant: codes and counters only."""
        with self._lock:
            it = self._current
            last = self._last.get(grant_id)
        if it is not None and it.grant_id == grant_id:
            return it.summary()
        if last is not None:
            return dict(last)
        return {"state": "idle", "number": 0, "end": "", "detail": "", "card": "none"}

    @contextlib.contextmanager
    def _board_write(self):
        """A change of several board pieces at once (see ``_board_seq``)."""
        with self._lock:
            self._board_open += 1
            if self._board_open == 1:
                self._board_seq += 1  # odd: a write is open
        try:
            yield
        finally:  # never left odd, whatever the write raised
            with self._cond:
                self._board_open -= 1
                if self._board_open == 0:
                    self._board_seq += 1  # even: stable again, and moved
                self._cond.notify_all()  # wait_idle: an iteration's end includes its write

    def board_seq(self) -> int:
        """The board's version (W5b): even = stable, odd = a write is open. One read."""
        return self._board_seq

    def _outcome(self, it: _Iteration) -> dict:
        """One ended iteration for the board (W5b): codes, tool names and counters."""
        try:
            took = None if it.started is None else max(0, int((self._mono() - it.started) * 1000))
        except Exception:  # noqa: BLE001 - an unreadable clock: no duration
            took = None
        ended = time.time()
        with it.lock:
            out = {
                "number": it.number,
                "end": it.stop_code or "done",
                "detail": it.detail,
                "tools": list(it.called),
                "tick_used": it.budget.charged,
                "ts": ended,
            }
        if took is not None:
            out["duration_ms"] = took
        return out

    def board(self, grant_id: Any) -> dict:
        """The Stage board's read (W5b): :meth:`view` plus the tokens the running (or
        last) iteration charged and the last ends of this grant. Memory only; codes,
        tool names and counters (``observability.activity.grant_view`` allow-lists it)."""
        # One snapshot under one non-blocking acquisition (W5b fix round 1): the current
        # iteration, the last summary and the outcomes; the iteration's own numbers come
        # from that iteration only. A held lock raises BoardBusy at once — never a wait.
        if not self._lock.acquire(blocking=False):
            raise BoardBusy()
        try:
            seq = self._board_seq
            if seq % 2:
                raise BoardBusy()  # a write is open: its pieces are half-written
            it = self._current
            last = self._last.get(grant_id)
            outcomes = [dict(o) for g, o in self._outcomes if g == grant_id]
            costs = [dict(c) for g, c in self._tick_costs if g == grant_id]
        finally:
            self._lock.release()
        if it is not None and it.grant_id == grant_id:
            iteration = it.try_summary()
            used = it.budget.try_used()
            if iteration is None or used is None:
                raise BoardBusy()
            iteration["tick_used"] = used
        elif last is not None:
            iteration = dict(last)
            if outcomes:
                iteration["tick_used"] = outcomes[-1]["tick_used"]
        else:
            iteration = {"state": "idle", "number": 0, "end": "", "detail": "", "card": "none"}
        if self._board_seq != seq:
            # a write began (and maybe ended) while the pieces were read: never a mix
            raise BoardBusy()
        out = {"iteration": iteration, "outcomes": outcomes}
        if costs:
            out["tick_costs"] = costs  # W6d: only after a heartbeat-started iteration
        return out

    def status(self) -> dict:
        with self._lock:
            it = self._current
        return {
            "enabled": True,
            "running": it is not None,
            "abandoned_calls": self._abandoned_count(),
            "abandoned_total": self.abandoned_total,
            "playbook": self.playbook.code if self.playbook is not None else "",
            "iteration_wall_sec": self.settings.iteration_wall_sec,
        }

    def poll(self) -> None:
        """The watchdog's pass: an iteration past its wall time ends (``timeout``); one
        whose grant is no longer live is stopped. Never raises."""
        try:
            with self._lock:
                it = self._current
            if it is None:
                return
            if self._mono() >= it.deadline:
                it.stop("timeout")
                return
            _grant, _record, _now, code = self._look(it)
            if code is not None:
                it.stop("stopped", code)
        except Exception:  # noqa: BLE001
            logger.warning("grants: the iteration look failed")

    def _on_preempt(self) -> None:
        """A turn-lock holder arrived (a chat turn, a webhook, the automatic continue, a
        resume, ...): a pending card is withdrawn now; the iteration yields at its next
        boundary (the running effect is not interrupted)."""
        with self._lock:
            it = self._current
        if it is not None:
            it.withdraw_card()

    def stop(self, timeout: float | None = None) -> str:
        """The shutdown step; **never raises**. No new iteration; the running one is
        stopped and joined within ``timeout``: ``stopped``, ``abandoned`` (a provider call
        thread left behind) or ``pending`` (the iteration thread, or a press still
        starting, outlived the bound — that press refuses at publication)."""
        self._stopping = True  # first: whatever follows, no new effect is admitted
        try:
            try:
                wait = 5.0 if timeout is None else float(timeout)
            except (TypeError, ValueError):
                wait = 5.0  # an unreadable bound: the default one
            wait = max(0.0, wait) if math.isfinite(wait) else 5.0
            with self._cond:
                # A press between its admission and its thread start finishes first (it
                # sees ``_stopping`` and refuses, or publishes a started thread).
                starting = not self._cond.wait_for(lambda: not self._starting, timeout=wait)
                it = self._current
            if it is not None:
                it.stop("shutdown")
                thread = it.thread
                if thread is not None and thread is not threading.current_thread():
                    # A published thread not started yet raises here (caught: pending; it
                    # exits at once when it starts).
                    thread.join(wait)
                    if thread.is_alive():
                        return "pending"
            if starting:
                return "pending"
            with self._lock:
                return "abandoned" if self._live_abandoned() else "stopped"
        except Exception:  # noqa: BLE001 - a shutdown step never raises
            logger.warning("grants: the executor's stop step failed; pending")
            return "pending"

    def wait_abandoned(self, timeout: float) -> bool:
        """Tests: wait until every abandoned provider-call thread has ended."""
        with self._lock:
            threads = list(self._abandoned)
        for thread in threads:
            thread.join(timeout)
        with self._lock:
            return self._live_abandoned() == 0

    def wait_idle(self, timeout: float) -> bool:
        """Tests and the shutdown: wait (on the signal, no sleep) until no iteration runs."""
        with self._cond:
            return self._cond.wait_for(
                lambda: self._current is None and self._board_seq % 2 == 0, timeout=timeout
            )
