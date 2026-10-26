"""The YOLO request, its confirmation and the window's life (ADR 0012 §1a, §6–§7, §11).

``POST /api/yolo/request {tier}`` creates a request (the press): the allowance, the tier,
the latch, no window, no other request, the rate limits (§1a 2.: one pending; no new
request for :data:`COOLDOWN_SEC` after a denial or an expiry; at most :data:`HOURLY_MAX`
per hour) and the platform's clocks are checked, ``t0`` is read and
``audit.yolo.requested`` is written — a request whose row cannot be written does not
exist. A person then confirms or denies it **on the host** (:mod:`.host_channel`, the
command or the desktop dialog), within :data:`EXPIRY_SEC`.

**Phase 3: the window opens and closes, nothing is approved automatically.** A
confirmation runs §6a: (1) under the lock the CAS ``pending → issuing`` recording fence
``f`` and epoch ``e``; (2) outside it ``audit.yolo.confirmed``, the marker ``open``
(durable, :mod:`.window`), ``audit.yolo.started``, and the read-back of both rows (their
receipts: seq, hash, type, request / window id, incarnation reference); (3) under the
lock the final CAS (:func:`state.issue_commit`: same request and window id, fence and
epoch unchanged, no window, latch clear, the writer healthy, the receipts present, the
clocks before the end and without an anomaly) → window ``active`` (``paused`` when the
breaker is not NORMAL). Anything else: the window never becomes usable; a stop, ``/new``,
cancel or a moved epoch is a normal ``ended(issue_aborted_*)``, every other failure
``aborted(issue_failed)`` (abnormal, latched). The window ends on time (six hours from
the press, the largest of three clocks), on a sleep longer than five minutes, on a clock
anomaly (latched), on stop (``POST /api/yolo/stop``, server stop), on ``/new`` — then the
close sequence: ``audit.yolo.ended``, and **only for a normal end** the marker ``closed``.
The window is in memory only: a restart never brings it back, and an unclean end leaves
the marker ``open`` (latched until a person unlatches it). The window changes no tool
call: nothing outside this package reads it (phase 5 connects the approver).

Narrowings (deny, expiry, cancel, ``/new``, stop) take effect in memory first and are never
undone; a row that cannot be written afterwards latches (fail closed). A restart forgets
the request (nothing is read back; every id belongs to this incarnation).

**Publication is a compare-and-set** (revision 6, review of phase 2): every change of
``machine`` moves a revision; a press that wrote its ``requested`` row publishes its
candidate only if the revision is the one it was built on, so a latch or a fence that
happened while the row was written is never rolled back by an older candidate.

**Two confirmation channels** (revision 6): ``browser`` (the default) — the press's answer
carries a one-time confirmation token (kept only as a digest) and the page confirms with
it and the dialog's checkbox (+ a second checkbox for tier external; phase 2c: checkboxes
instead of typed words),
:meth:`YoloRequests.confirm_in_browser`; ``command`` / ``dialog`` — the host decides
(:meth:`YoloRequests.decide`) and the browser confirmation is refused.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import secrets
import threading
import time as _time
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

from ..core import breaker_reasons
from . import origin as yolo_origin
from . import state as sm
from . import tool_stats
from .allowance import DEFAULT_CAP, DEFAULT_EXEC_CAP, DEFAULT_EXTERNAL_CAP, ClockReading
from .audit_schema import EXPECTED_IDLE, REGISTRY, SCHEMA
from .authority import AuthorityBoundary, AuthorityError, GrantRecord, Permit, Receipt

# W1 (#296): refusals of the shared authority service itself, recorded as their own
# ``audit.yolo.void`` reason codes (not ``permit_stale`` / ``consume_refused``).
_SERVICE_STOPS = frozenset({"killed", "authority_unhealthy"})
from .host_ops import (
    HostOpRefused,
    default_clock,
    elapsed,
    instance_matches,
    instance_ref,
    is_acknowledged,
    token_digest,
    token_matches,
)
from .tiers import EXEC, EXEC_AUTO_TOOLS, EXTERNAL, EXTERNAL_TOOLS, INTERNAL, tool_class
from .values import cap_value, non_negative_int
from .window import Latch, MarkerRead, WindowMarker, judge

__all__ = [
    "CLOCK_SLACK_SEC",
    "COOLDOWN_SEC",
    "DWELL_SEC",
    "EXPIRY_SEC",
    "FLAP_MAX",
    "FLAP_WINDOW_SEC",
    "HOURLY_MAX",
    "SLEEP_SEC",
    "WATCHDOG_SEC",
    "WINDOW_HOURS",
    "WORD",
    "AutoGrant",
    "YoloRequests",
    "clock_detail",
    "clock_verdict",
]

EXPIRY_SEC = 120.0  # Open (5)
COOLDOWN_SEC = 300.0  # Open (5): after a denial or an expiry
HOURLY_MAX = 3  # Open (5)
WINDOW_HOURS = 6  # requirement 1: six hours from the press
WINDOW_SEC = WINDOW_HOURS * 3600.0
SLEEP_SEC = 300.0  # §7 / Open (5): a sleep longer than this ends the window
CLOCK_SLACK_SEC = 2.0  # §7: wall clock backwards / behind the boot clock beyond this
WATCHDOG_SEC = 30.0  # §7: how often the server re-checks an open window's clocks
DWELL_SEC = 600.0  # §6 / Open (5): NORMAL this long (every sample) before a resume
FLAP_WINDOW_SEC = 3600.0  # §6: "> 3 pauses in 60 min" ends the window (revoked_flapping)
FLAP_MAX = 3
logger = logging.getLogger(__name__)


def _observe(count: Callable[..., Any], *args: Any) -> Any:
    """#448: the window's tool-call counter is observation only. A failure is logged (one
    line, no value) and returns ``None``; it never changes an approval, a slot, a cap end
    or the close sequence."""
    try:
        return count(*args)
    except Exception:  # noqa: BLE001 - observation only
        logger.warning("YOLO: counting the window's tool calls failed (counts skipped)")
        return None


# Phase 5b (§12): how many automatic-approval log lines status keeps (the page shows the
# new ones; older lines are already on the screen or in the server log).
RECENT_LINES = 20
_LINE_TOOL = re.compile(r"[a-z][a-z0-9_]{0,47}")
_SYSTEM_ACTOR = "system:yolo"
WORD = "YOLO"  # typed at the host command's confirmation of a YOLO request
_BROWSER_PRINCIPAL = "browser:dashboard"


@dataclass(frozen=True)
class _WindowRef:
    """What :meth:`YoloRequests._row` reads of a window for an outcome row."""

    window_id: str


@dataclass(frozen=True)
class AutoGrant:
    """One automatic approval handed to the gate (§6a): the single-use permit, the
    ``audit.yolo.auto_approved`` row it was recorded by (``intent``, its operation id) and
    the window it belongs to. Consumed once at the execution boundary, or voided."""

    permit: Permit
    intent: str
    window_id: str
    tool: str
    # Phase 6: an external tool's approval also holds one slot of the external sub-cap.
    external: bool = False
    # Phase 7: an exec tool's approval (a window with the exec flag) holds one slot of the
    # exec sub-cap; it is what lets the verifier issue the exec gate mark (#362).
    exec_class: bool = False


class YoloRequests:
    """One per process (only with ``STIRA_YOLO_ALLOW``). ``record`` writes one row
    (``AuditChain.record_yolo``) and raises on any failure."""

    def __init__(
        self,
        *,
        allowance: str,
        incarnation: str,
        record: Callable[..., str] | None,
        actor: str,
        clocks_supported: bool,
        clock: Callable[[], ClockReading] = default_clock,
        on_created: Callable[[dict], None] | None = None,
        channel: str = "command",
        token: Callable[[], str] = lambda: secrets.token_hex(16),
        ledger: Any = None,
        marker: WindowMarker | None = None,
        breaker: Callable[[], Any] | None = None,
        breaker_reasons: Callable[[], Any] | None = None,
        cap: int = DEFAULT_CAP,
        external_cap: int = DEFAULT_EXTERNAL_CAP,
        exec_offered: bool = False,
        exec_cap: int = DEFAULT_EXEC_CAP,
        authority: AuthorityBoundary | None = None,
    ) -> None:
        if authority is not None and authority.incarnation != incarnation:
            # The injected process service carries the one incarnation (W1, #296).
            raise ValueError("incarnation differs from the authority service")
        self.incarnation = incarnation
        self.instance = instance_ref(incarnation)
        self.channel = channel
        self._record = record
        self._actor = actor
        self._clocks_supported = clocks_supported
        self._clock = clock
        self._on_created = on_created
        self._token = token
        self._key = secrets.token_bytes(32)  # keyed hashes of this process only
        self._key_id = "proc-" + hashlib.sha256(self._key).hexdigest()[:12]
        self._lock = threading.Lock()
        self._busy = False  # a press or a confirmation is between its steps
        self.machine = sm.initial(allowance)
        self._rev = 0  # moved by every change of ``machine`` (the CAS of a press)
        self._confirm_digest: tuple[str, str] | None = None  # (request id, token digest)
        self._t0: ClockReading | None = None
        self._presses: list[ClockReading] = []
        self._negative: ClockReading | None = None  # last denial / expiry
        # Phase 3 (§6a, §11). ``ledger``: the audit chain's read side (``yolo_receipt``,
        # ``yolo_healthy``, ``yolo_history``, ``yolo_head_seq``); ``marker``: the window
        # marker. Without either nothing can open (the receipts / the marker fail closed).
        self._ledger = ledger
        self._marker = marker
        self._marker_lock = threading.Lock()  # every marker read-modify-write
        self._breaker = breaker
        # #455: why the breaker paused the window (codes and numbers; display only).
        self._breaker_reasons = breaker_reasons
        self.cap = cap
        self.external_cap = min(external_cap, cap)
        # Phase 7 (§9 option C): whether a press may include exec (exec tools exposed and
        # the allowance ``external``, read once at start-up), and the exec sub-cap.
        self.exec_offered = exec_offered is True and allowance == "external"
        self.exec_cap = min(exec_cap, cap)
        self._incarnation_ref = self._ref("incarnation:" + incarnation)
        self._win: dict | None = None  # the open window's clocks, receipt, slots, pauses
        # Phase 5a (§6a, §13): the shared authority boundary — the permits of automatic
        # approvals, single-use and bound to (incarnation, window, generation, epoch).
        # W1 (#296): the server injects the process-wide service (its epoch also moves on
        # breaker transitions, rules saves and the kill switch, and is persisted); without
        # one (unit tests, tools) a private in-memory boundary as before.
        if authority is None:
            authority = AuthorityBoundary()
            authority.incarnation = incarnation
        self._authority = authority
        self._last_end: dict | None = None  # the last window end (status)
        self._last_end_window: str | None = None  # its full id (#448, a late failure)
        # Phase 5b (§12): the log lines of the latest window's automatic approvals (codes
        # and counts only — never arguments), kept after its end until the next opens.
        self._recent: dict = {"window": None, "items": []}
        self._closing = 0  # close sequences in progress (unlatch waits for them)
        # window id (or ``unreadable:<reason>``) -> Latch: every latch, each closed by
        # its own unlatch; ``machine.latched`` is true while any is left.
        self._latches: dict[str, Latch] = {}
        self._issue_authority_epoch: int | None = None  # read at confirm_begin (W1)
        self._judge_storage()

    # -- rows ------------------------------------------------------------------------

    def _ref(self, text: str) -> str:
        return hmac.new(self._key, text.encode("ascii"), hashlib.sha256).hexdigest()

    def _row(
        self,
        event_type: str,
        request,
        *,
        human: bool,
        surface: str,
        op: str | None = None,
        **extra,
    ):
        spec = REGISTRY[event_type]
        operation_id = secrets.token_hex(16)
        fields = {
            "schema": SCHEMA,
            "operation_id": operation_id,
            "phase": spec.phase,
            "event_id": f"op:{operation_id}:{spec.phase}",
            "actor_kind": "human" if human else "system",
            "actor": self._actor if human else _SYSTEM_ACTOR,
            "surface": surface,
            "incarnation_ref": self._ref("incarnation:" + self.incarnation),
            "key_id": self._key_id,
            "delta_class": spec.delta_class,
            "window_id": request.window_id,
            "generation_from": 0,
            "generation_to": 0,
            "authority_epoch": self.machine.epoch,
            **extra,
        }
        if spec.human_op:
            fields["op_token_ref"] = self._ref("op:" + (op or request.request_id))
        return event_type, fields

    def _write(self, row) -> str | None:
        """Write one row; its event id, or ``None`` on any failure."""
        if self._record is None:
            return None
        event_type, fields = row
        try:
            event_id = self._record(event_type, **fields)
        except Exception:  # noqa: BLE001 - any failure is an audit-write failure
            return None
        return event_id if type(event_id) is str and event_id else None

    def _publish(self, machine: sm.Machine) -> None:
        """Under the lock: the one way ``machine`` changes (moves the revision).

        Every move of the machine's authority epoch (pause, end, rules save …) moves
        the boundary's epoch with it: a permit handed out before is refused at consume."""
        for _ in range(max(0, machine.epoch - self.machine.epoch)):
            self._authority.advance_epoch()
        self.machine = machine
        self._rev += 1

    def _narrowing_rows(self, rows) -> None:
        """Rows of narrowings already in effect: a failed write latches (fail closed).

        A pause whose ``audit.yolo.paused`` row cannot be written also ends the window
        (``revoked_audit_failed``, §6a): a window paused without its row could later be
        resumed with no record of the pause. A written pause row is what a resume binds
        to (``paused_ref``)."""
        for row in rows:
            event_id = self._write(row)
            if not event_id:
                with self._lock:
                    self._latch_locked(row[1]["window_id"], "window", "revoked_audit_failed")
                if row[0] == "audit.yolo.paused":
                    self.fail_closed("revoked_audit_failed")
            elif row[0] == "audit.yolo.paused":
                with self._lock:
                    info, window = self._win, self.machine.window
                    if (
                        info is not None
                        and window is not None
                        and info["window_id"] == row[1]["window_id"]
                        and window.generation == row[1]["generation_to"]
                    ):
                        info["paused_ref"] = (window.generation, event_id)

    # -- latches (§11) ----------------------------------------------------------------

    def _latch_locked(self, window_id: str | None, subject: str, reason: str) -> None:
        key = window_id if window_id is not None else f"unreadable:{reason}"
        self._latches.setdefault(key, Latch(subject, window_id, reason))
        first = next(iter(self._latches.values()))
        if not self.machine.latched or self.machine.latch_reason != first.reason:
            self._publish(replace(self.machine, latched=True, latch_reason=first.reason))

    def _judge_storage(self) -> None:
        """The §11 judgement from the chain and the marker (at start and before a press).

        Without a ledger and a marker (a unit test of the request alone) there is
        nothing to judge — and nothing can open either."""
        if self._ledger is None or self._marker is None:
            return
        try:
            rows = self._ledger.yolo_history()
        except Exception:  # noqa: BLE001 - an unreadable chain latches
            rows = None
        with self._marker_lock:
            marker = self._marker.read()
            verdict = judge(rows, marker)
            latches = list(verdict.latches)
            if verdict.repair is not None:
                try:
                    self._marker.write(
                        verdict.repair,
                        marker.window_id,
                        marker.data["incarnation_ref"],
                        operation_id=marker.data.get("operation_id"),
                    )
                except (OSError, ValueError):
                    latches.append(Latch("unreadable", None, "marker_repair_failed"))
        with self._lock:
            for latch in latches:
                self._latch_locked(latch.window_id, latch.subject, latch.reason)

    def _marker_write(self, state: str, window_id: str, operation_id: str | None = None) -> bool:
        if self._marker is None:
            return False
        with self._marker_lock:
            try:
                self._marker.write(
                    state, window_id, self._incarnation_ref, operation_id=operation_id
                )
            except (OSError, ValueError):
                return False
        return True

    def _receipt(self, event_id: str, event_type: str, request: sm.Request) -> Receipt | None:
        """ADR 0013 §2b (A): read the row back; its receipt only if every binding matches."""
        if self._ledger is None:
            return None
        try:
            found = self._ledger.yolo_receipt(event_id)
        except Exception:  # noqa: BLE001 - an unreadable row has no receipt
            return None
        if type(found) is not dict or type(found.get("attributes")) is not dict:
            return None
        receipt = Receipt(event_id, found.get("seq"), found.get("hash"))
        attributes = found["attributes"]
        if not (
            receipt.valid()
            and found.get("event_type") == event_type
            and attributes.get("request_id") == request.request_id
            and attributes.get("window_id") == request.window_id
            and attributes.get("incarnation_ref") == self._incarnation_ref
        ):
            return None
        return receipt

    def _before_final_cas(self) -> None:
        """Test seam: called just before the final CAS takes the lock (no-op)."""

    def _healthy(self) -> bool:
        try:
            return self._ledger is not None and self._ledger.yolo_healthy() is True
        except Exception:  # noqa: BLE001 - unknown health is unhealthy
            return False

    def _breaker_level(self) -> str:
        """The live breaker level as ``normal`` / ``warning`` / ``coherence`` /
        ``critical``; an exception, no reader or any other value: ``unknown``."""
        if self._breaker is None:
            return "unknown"
        try:
            level = self._breaker()
        except Exception:  # noqa: BLE001 - unknown is not NORMAL
            return "unknown"
        name = str(getattr(level, "value", level)).lower()
        return name if name in ("normal", "warning", "coherence", "critical") else "unknown"

    def _breaker_reason_codes(self) -> dict:
        """``{"reasons", "inputs"}`` of the live breaker level (#455): allow-listed codes and
        numbers; empty without a reader or on any failure (the pause itself never depends
        on it)."""
        if self._breaker_reasons is None:
            return {"reasons": [], "inputs": {}}
        try:
            detail = self._breaker_reasons() or {}
            reasons, inputs = breaker_reasons.clean(detail.get("reasons"), detail.get("inputs"))
        except Exception:  # noqa: BLE001 - display only
            reasons, inputs = [], {}
        return {"reasons": reasons, "inputs": inputs}

    def _breaker_normal(self) -> bool:
        """The live breaker level is NORMAL; an exception, no reader or any other value:
        not NORMAL (the window opens paused)."""
        return self._breaker_level() == "normal"

    # -- expiry ----------------------------------------------------------------------

    def _expire_locked(self, now: ClockReading) -> list:
        request = self.machine.request
        if request is None or request.state != "pending" or self._t0 is None:
            return []
        if elapsed(self._t0, now) < EXPIRY_SEC:
            return []
        self._publish(sm.request_timeout(self.machine, request.request_id))
        self._negative = now
        return [
            self._row(
                "audit.yolo.request_expired",
                request,
                human=False,
                surface="system",
                request_id=request.request_id,
                reason_code="request_expired",
            )
        ]

    def _now(self) -> ClockReading | None:
        """The three clocks, or ``None`` when reading them failed (never raises)."""
        try:
            reading = self._clock()
        except Exception:  # noqa: BLE001 - a clock that cannot be read is an anomaly
            return None
        return reading if isinstance(reading, ClockReading) else None

    def _due_locked(self):
        """Under the lock: read the clocks **here** (never a reading taken before the
        lock), expire the request, and end an open window whose clocks say so, whose
        clocks cannot be read (``revoked_clock_anomaly``, latched) or whose audit writer
        is no longer healthy (``revoked_audit_failed``, latched). Returns
        ``(now or None, rows to write, close-sequence arguments or None)``; the caller
        writes and closes outside the lock."""
        now = self._now()
        rows: list = []
        closing = None
        if now is None:
            # #596: why the clocks ended it (a fixed word; nothing could be read).
            self._clock_why_locked({"clock_check": "clock_unreadable"})
            request = self.machine.request
            if request is not None and request.state == "pending":
                # Its two minutes cannot be told: it can never be confirmed (fail closed).
                self._publish(sm.request_timeout(self.machine, request.request_id))
                rows.append(
                    self._row(
                        "audit.yolo.request_expired",
                        request,
                        human=False,
                        surface="system",
                        request_id=request.request_id,
                        reason_code="request_expired",
                    )
                )
            closing = self._fail_locked("revoked_clock_anomaly")
        else:
            rows = self._expire_locked(now)
            closing = self._clock_end_locked(now)
        if closing is None and not self._healthy():
            closing = self._fail_locked("revoked_audit_failed")
        if closing is None and now is not None:
            closing = self._breaker_sample_locked(now, self._breaker_level(), rows)
        return now, rows, closing

    # -- pause / resume (§6, phase 5a) -------------------------------------------------

    def _breaker_sample_locked(self, now: ClockReading, level: str, rows: list):
        """One sample of the breaker for an open window (every look, the watchdog, every
        automatic approval). ``active`` and not NORMAL → paused (a narrowing; the row is
        appended to ``rows``) — or, for the fourth pause within :data:`FLAP_WINDOW_SEC`,
        ended ``revoked_flapping`` (returns the close-sequence arguments). ``paused``:
        a NORMAL sample starts the dwell, any other sample restarts it."""
        window, info = self.machine.window, self._win
        if window is None or not window.open or info is None:
            return None
        if info["window_id"] != window.window_id:
            return None
        if window.state == "paused":
            if level != "normal":
                info["normal_since"] = None
            elif info.get("normal_since") is None:
                info["normal_since"] = now
            return None
        if level == "normal":
            return None
        recent = [t for t in info["pauses"] if elapsed(t, now) < FLAP_WINDOW_SEC]
        if len(recent) >= FLAP_MAX:
            info["pauses"] = recent
            return self._end_locked("revoked_flapping")
        info["pauses"] = [*recent, now]
        reason = f"breaker_{level}" if level != "normal" else "breaker_unknown"
        if reason not in sm.PAUSE_REASONS:
            reason = "breaker_unknown"
        generation = window.generation
        self._publish(sm.pause(self.machine, reason))
        info["pause_breaker"] = self._breaker_reason_codes()
        info["normal_since"] = None
        info["paused_ref"] = None  # set once the paused row is written
        rows.append(
            self._row(
                "audit.yolo.paused",
                window,
                human=False,
                surface="system",
                reason_code=reason,
                generation_from=generation,
                generation_to=generation + 1,
            )
        )
        return None

    def _snapshot(self, window: sm.Window, state: str) -> dict:
        """The window as the one loosening judgement sees it (ADR 0012 §2)."""
        info = self._win or {}
        t0 = info.get("t0")
        return {
            "yolo": window.tier,
            "state": state,
            "cap": info.get("cap", self.cap),
            "expires_at": (t0.wall if t0 is not None else 0.0) + WINDOW_SEC,
            "grant_id": window.window_id,
            "incarnation": self.incarnation,
            "exec": window.with_exec,
        }

    def _loosening_allowed(self, before: dict, after: dict, context: str) -> bool:
        """``rules_loosening`` (the one function, §2) with a YOLO context: allowed only
        when the result does not contain ``yolo_not_settable``."""
        from ..tools.tool_config import rules_loosening
        from .loosening import NOT_SETTABLE

        try:
            reasons = rules_loosening(
                before, after, context=context, allowance=self.machine.allowance
            )
        except Exception:  # noqa: BLE001 - a judgement that fails refuses
            return False
        return NOT_SETTABLE not in reasons

    def _resume_prepare_locked(self, now: ClockReading | None):
        """Under the lock, §6a *Resume* (1): paused at generation ``g``, its pause row
        written, NORMAL for :data:`DWELL_SEC` (every sample), the transition allowed by
        the one loosening judgement (``breaker_resume``). Returns the ``resumed`` row and
        ``(window id, g)``, or ``None``."""
        window, info = self.machine.window, self._win
        if now is None or window is None or window.state != "paused" or info is None:
            return None
        if info["window_id"] != window.window_id or info.get("resuming"):
            return None
        since, paused = info.get("normal_since"), info.get("paused_ref")
        if since is None or elapsed(since, now) < DWELL_SEC:
            return None
        if paused is None or paused[0] != window.generation:
            return None  # the pause row is not written (yet): nothing to bind to
        if not self._loosening_allowed(
            self._snapshot(window, "paused"), self._snapshot(window, "active"), "breaker_resume"
        ):
            return None
        info["resuming"] = True
        row = self._row(
            "audit.yolo.resumed",
            window,
            human=False,
            surface="system",
            reason_code="breaker_resume",
            paused_ref=paused[1],
            generation_from=window.generation,
            generation_to=window.generation + 1,
        )
        return row, window.window_id, window.generation

    def _resume(self) -> bool:
        """§6a *Resume*: (1) prepare under the lock, (2) write ``audit.yolo.resumed``,
        (3) the CAS ``paused(g) → active(g+1)`` under the lock with the clocks and the
        **live** breaker level read there (expiry first). A failed CAS leaves the newer
        state (an outcome row ``void`` follows, best effort); a failed write revokes
        (``revoked_audit_failed``). Returns whether the window is active again."""
        with self._lock:
            now, rows, closing = self._due_locked()
            prepared = None if closing is not None else self._resume_prepare_locked(now)
        self._after(rows, closing)
        if prepared is None:
            return False
        row, window_id, generation = prepared
        event_id = self._write(row)
        if event_id is None:
            with self._lock:
                if self._win is not None:
                    self._win["resuming"] = False
            self.fail_closed("revoked_audit_failed")
            return False
        resumed = False
        with self._lock:
            # Expiry and the clocks first, then the breaker level read here, live.
            now, rows, closing = self._due_locked()
            same = self._win is not None and self._win["window_id"] == window_id
            if same:
                self._win["resuming"] = False
            # #601 A7: a non-NORMAL sample while the row was written restarted the dwell
            # (it changes no generation): the dwell is checked again here.
            since = self._win.get("normal_since") if same else None
            dwelled = since is not None and now is not None and elapsed(since, now) >= DWELL_SEC
            if closing is None and now is not None and dwelled:
                try:
                    machine = sm.resume(
                        self.machine,
                        window_id,
                        generation,
                        breaker_normal=self._breaker_normal(),
                        before_expiry=True,
                    )
                except sm.Refused:
                    machine = None
                if machine is not None:
                    self._publish(machine)
                    self._win["normal_since"] = None
                    resumed = True
        self._after(rows, closing)
        if not resumed:
            self._outcome_row(
                "audit.yolo.void",
                window_id,
                generation,
                intent_ref=row[1]["operation_id"],
                reason_code="permit_stale",
                surface="system",
            )
        return resumed

    def _fail_locked(self, reason: str):
        """End an open window abnormally (latched by its close sequence); else ``None``."""
        window = self.machine.window
        if window is None or not window.open:
            return None
        return self._end_locked(reason)

    def _after(self, rows, closing) -> None:
        """Outside the lock: the rows of narrowings already in effect, then the close."""
        self._narrowing_rows(rows)
        if closing is not None:
            self._close(*closing, human=False)

    def _settle_due(self) -> ClockReading | None:
        with self._lock:
            now, rows, closing = self._due_locked()
        self._after(rows, closing)
        return now

    def tick(self) -> None:
        """The watchdog (§7, every :data:`WATCHDOG_SEC`): request expiry, window clocks,
        writer health, the breaker sample (pause) and — after the dwell — the resume CAS.
        A clock that cannot be read ends the window (never raises)."""
        self._settle_due()
        self._resume()

    def fail_closed(self, reason: str = "revoked_clock_anomaly") -> None:
        """End an open window abnormally (the watchdog's answer to its own failure)."""
        with self._lock:
            if reason == "revoked_clock_anomaly":
                # #596: the watchdog's own check failed (a fixed word, never its error).
                self._clock_why_locked({"clock_check": "watchdog_failed"})
            closing = self._fail_locked(reason)
        self._after([], closing)

    def _clock_why_locked(self, detail: dict) -> None:
        """#596: keep why the clocks end the open window, for its ``audit.yolo.ended``
        row (fixed words and numbers only). The first reason is kept."""
        window, info = self.machine.window, self._win
        if window is None or not window.open or info is None:
            return
        if info["window_id"] == window.window_id and "clock_detail" not in info:
            info["clock_detail"] = detail

    def _clock_end_locked(self, now: ClockReading):
        """An open window whose clocks say it ended (§7): end it now (a narrowing in
        memory first); returns the close-sequence arguments, or ``None``."""
        window = self.machine.window
        info = self._win
        if window is None or not window.open or info is None:
            return None
        last = info["last"]
        reason = clock_verdict(info["t0"], last, now)
        info["last"] = now
        if reason is None:
            return None
        if reason == "revoked_clock_anomaly":
            self._clock_why_locked(clock_detail(info["t0"], last, now))
        return self._end_locked(reason)

    def _end_locked(self, reason: str):
        window = self.machine.window
        generation = window.generation
        self._publish(sm.end_window(self.machine, reason))
        self._closing += 1
        return self.machine.window, generation, generation + 1

    def _abnormal_cap_end_locked(self, closing):
        """#601 A8, under the lock: the cap ended the window (``expired_cap``, its close
        sequence not run yet) and then an audit row of that last call failed — the end
        is ``revoked_audit_failed`` instead (stricter: abnormal, latched, the marker is
        not closed). The window is already ended: no generation or epoch moves."""
        window, gen_from, gen_to = closing
        current = self.machine.window
        if (
            current is None
            or current.window_id != window.window_id
            or current.state != "ended"
            or current.end_reason != "expired_cap"
        ):
            return closing
        ended = replace(current, end_reason="revoked_audit_failed")
        self._publish(replace(self.machine, window=ended))
        return ended, gen_from, gen_to

    # -- automatic approval of one call (§6a, phase 5a) --------------------------------

    def _outcome_row(self, event_type: str, window_id: str, generation: int, **fields) -> bool:
        """An outcome row (``consumed`` / ``void``) of an automatic approval or a resume.

        A failed write ends the window ``revoked_audit_failed`` (fail closed: the audit
        stops the loosening). Returns whether the row was written."""
        surface = fields.pop("surface", "chat")
        # A row carries the window id only (no tier or state field, §11): the stand-in
        # names nothing else, so an external window's rows cannot be mislabelled.
        row = self._row(
            event_type,
            _WindowRef(window_id),
            human=False,
            surface=surface,
            generation_from=generation,
            generation_to=generation,
            **fields,
        )
        if self._write(row) is not None:
            return True
        self.fail_closed("revoked_audit_failed")
        return False

    def _tool_receipt(self, event_id: str | None, row) -> Receipt | None:
        """ADR 0013 §2b (A) for a tool row: read it back; a receipt only if the type,
        operation, window and incarnation reference match what was written."""
        if event_id is None or self._ledger is None:
            return None
        try:
            found = self._ledger.yolo_receipt(event_id)
        except Exception:  # noqa: BLE001 - an unreadable row has no receipt
            return None
        if type(found) is not dict or type(found.get("attributes")) is not dict:
            return None
        receipt = Receipt(event_id, found.get("seq"), found.get("hash"))
        attributes, written = found["attributes"], row[1]
        if not (
            receipt.valid()
            and found.get("event_type") == row[0]
            and attributes.get("operation_id") == written["operation_id"]
            and attributes.get("window_id") == written["window_id"]
            and attributes.get("incarnation_ref") == self._incarnation_ref
        ):
            return None
        return receipt

    def _release_locked(
        self, window_id: str, external: bool = False, exec_class: bool = False
    ) -> None:
        info = self._win
        if info is None or info["window_id"] != window_id:
            return
        if info["reserved"] > 0:
            info["reserved"] -= 1
        if external and info["external_reserved"] > 0:
            info["external_reserved"] -= 1
        if exec_class and info["exec_reserved"] > 0:
            info["exec_reserved"] -= 1

    def approve(
        self,
        *,
        tool_name: Any,
        args_digest: Any,
        tool_use_id: Any,
        level: Any = None,
        rule_generation: Any = None,
        agent_id: Any = None,
        destination: Any = None,
    ) -> tuple[AutoGrant | None, str]:
        """The YOLO approver at the rule layer (§2 step 5): an automatic answer to one
        ``RULE_ASK`` request, or ``(None, code)`` — the call then goes to a person.

        Answered only for a **running human chat / Stage turn of ``agent_id``**
        (:mod:`.origin`, §3), an
        **internal** tool — or, in a window of tier external, a tool of
        ``EXTERNAL_TOOLS`` (phase 6; exec and the unclassified rest never; the gate has
        already sent calls with secret-like arguments to a person,
        ``tiers.external_refusal``) — an ``active`` window and a NORMAL breaker
        (``level``: the gate's turn snapshot; the live level is sampled too — anything
        else pauses the window).
        §6a: (1) under the lock check the window and reserve one slot (``used +
        reserved`` at the cap: *temporarily full*, a card, the window stays active; an
        external tool also one slot of the external sub-cap — used up or temporarily
        full: a card, the window stays active for everything else);
        (2) write ``audit.yolo.auto_approved`` and read it back — a failure releases the
        slot and revokes the window (``revoked_audit_failed``); (3) under the lock
        re-verify incarnation, generation, ``active`` and the clocks, then hand out the
        single-use permit (a change: the slot is released, ``void(permit_stale)``)."""
        if not yolo_origin.covered(agent_id):
            # A running human turn of this agent only (the mark is bound to its turn and
            # agent: a context copied out of the turn, or another agent's gate: no).
            return None, "origin_not_covered"
        klass = tool_class(tool_name)
        external = klass == EXTERNAL and tool_name in EXTERNAL_TOOLS
        # Phase 7 (§9 option C): ``shell`` / ``batch`` only, in a window with the exec flag
        # (the gate has already sent cron, the child-agent tools and secret-like
        # commands to a person, ``tiers.exec_refusal``).
        exec_class = klass == EXEC and tool_name in EXEC_AUTO_TOOLS
        if klass != INTERNAL and not external and not exec_class:
            return None, "tier_not_covered"
        # Only a host name as a plain ``str`` is recorded, as a keyed hash (Open (10) (b)).
        destination_ref = (
            self._ref("destination:" + destination)
            if external and type(destination) is str and destination.isascii() and destination
            else None
        )
        if type(args_digest) is not str or len(args_digest) != 64:
            return None, "unbindable"
        if type(tool_use_id) is not str or not tool_use_id:
            return None, "unbindable"
        gate_level = str(getattr(level, "value", level)).lower() if level is not None else ""
        rows: list = []
        closing = None
        code = None
        row = None
        with self._lock:
            now, rows, closing = self._due_locked()
            window, info = self.machine.window, self._win
            usable = closing is None and now is not None and window is not None and window.open
            if not usable or info is None or info["window_id"] != window.window_id:
                code = "no_window"
            elif window.state == "active" and gate_level != "normal":
                # The gate's snapshot is stricter than the live sample: pause now.
                level_name = gate_level if gate_level else "unknown"
                closing = self._breaker_sample_locked(now, level_name, rows)
                code = "paused"
            elif window.state != "active":
                code = "paused"
            elif external and window.tier != EXTERNAL:
                code = "tier_not_covered"  # a window of tier internal: external tools ask
            elif exec_class and not (window.with_exec and window.tier == EXTERNAL):
                code = "tier_not_covered"  # no "Include exec" at the press: exec asks
            elif info["used"] >= info["cap"]:
                code = "cap_used"
            elif info["used"] + info["reserved"] >= info["cap"]:
                code = "cap_full"  # temporarily full: a card; the window stays active
            elif external and info["external_used"] >= info["external_cap"]:
                code = "external_cap_used"  # external tools ask again; the window goes on
            elif external and (
                info["external_used"] + info["external_reserved"] >= info["external_cap"]
            ):
                code = "external_cap_full"
            elif exec_class and info["exec_used"] >= info["exec_cap"]:
                code = "exec_cap_used"  # exec tools ask again; the window goes on
            elif exec_class and info["exec_used"] + info["exec_reserved"] >= info["exec_cap"]:
                code = "exec_cap_full"
            else:
                info["reserved"] += 1
                if external:
                    info["external_reserved"] += 1
                exec_slot = None
                if exec_class:
                    info["exec_reserved"] += 1
                    exec_slot = info["exec_used"] + info["exec_reserved"]
                generation = window.generation
                rules = rule_generation if type(rule_generation) is int else 0
                row = self._row(
                    "audit.yolo.auto_approved",
                    window,
                    human=False,
                    surface="chat",
                    operation=tool_name,
                    target_kind="tool",
                    target="none",
                    args_ref=self._ref("args:" + args_digest),
                    decision="auto_approved",
                    authorizer_kind="yolo",
                    policy={
                        "rules": max(rules, 0),
                        "epoch": self.machine.epoch,
                        "window": generation,
                        "breaker": "normal",
                    },
                    tool_use_ref=self._ref("tool_use:" + tool_use_id),
                    origin_kind="turn",
                    tier=EXTERNAL if external or exec_class else INTERNAL,
                    external=external,
                    destination_ref=destination_ref,
                    # Phase 7: the tool's class and, for exec, the exec slot it holds
                    # (never the command: only ``args_ref``, a keyed hash).
                    tool_class=EXEC if exec_class else (EXTERNAL if external else INTERNAL),
                    exec_slot=exec_slot,
                    exec_limit=info["exec_cap"] if exec_class else None,
                    generation_from=generation,
                    generation_to=generation,
                )
        self._after(rows, closing)
        if row is None:
            return None, code or "no_window"
        window_id = window.window_id
        receipt = self._tool_receipt(self._write(row), row)
        if receipt is None:
            # No row, no approval: the slot goes back, the window is revoked (latched).
            with self._lock:
                self._release_locked(window_id, external, exec_class)
            self.fail_closed("revoked_audit_failed")
            return None, "audit_failed"
        grant = None
        stopped = None  # W1: the shared service refused (``killed`` / ``authority_unhealthy``)
        with self._lock:
            now, rows, closing = self._due_locked()
            current = self.machine.window
            if (
                closing is None
                and now is not None
                and current is not None
                and current.window_id == window_id
                and current.state == "active"
                and current.generation == generation
            ):
                record = GrantRecord(
                    kind="yolo",
                    grant_id=window_id,
                    incarnation=self._authority.incarnation,
                    generation=generation,
                    state=current.state,
                )
                try:
                    permit = self._authority.issue_permit(record, kind="yolo", receipt=receipt)
                except AuthorityError as error:
                    permit = None
                    if error.code in _SERVICE_STOPS:
                        stopped = error.code
                if permit is not None:
                    grant = AutoGrant(
                        permit,
                        row[1]["operation_id"],
                        window_id,
                        tool_name,
                        external,
                        exec_class,
                    )
                    self._win["approved"] += 1
                    count = (self._win["used"], self._win["reserved"], self._win["cap"])
            if grant is None:
                self._release_locked(window_id, external, exec_class)
        self._after(rows, closing)
        if grant is None:
            self._outcome_row(
                "audit.yolo.void",
                window_id,
                generation,
                intent_ref=row[1]["operation_id"],
                reason_code=stopped or "permit_stale",
            )
            return None, "permit_stale"
        # One log line per automatic approval: codes and counts only, never arguments
        # and never the destination (only its keyed hash, in the row).
        logger.info(
            "Auto-approved (YOLO %s): %s #%s — %d+%d/%d",
            EXEC if exec_class else (EXTERNAL if external else INTERNAL),
            tool_name,
            row[1]["operation_id"][:4],
            *count,
        )
        return grant, "approved"

    def consume(self, grant: Any) -> tuple[bool, str]:
        """At the execution boundary (ADR 0011 §5): re-check incarnation, generation,
        epoch, the clocks and ``active`` and turn ``reserved`` into ``used`` — exactly
        once per permit. ``used == cap`` ends the window (``expired_cap``) right after.
        A refused permit returns its slot (once) and records ``void``."""
        if not isinstance(grant, AutoGrant):
            return False, "consume_refused"
        closing_cap = None
        refused = None
        with self._lock:
            now, rows, closing = self._due_locked()
            window, info = self.machine.window, self._win
            if window is None or window.window_id != grant.window_id or now is None:
                refused = "window_ended"
            else:
                record = GrantRecord(
                    kind="yolo",
                    grant_id=window.window_id,
                    incarnation=self._authority.incarnation,
                    generation=window.generation,
                    state=window.state,
                )
                try:
                    self._authority.consume(grant.permit, record, kind="yolo")
                except AuthorityError as error:
                    refused = error.code
            if refused is not None:
                if refused != "permit_consumed" and self._authority.void(grant.permit):
                    self._release_locked(grant.window_id, grant.external, grant.exec_class)
            elif info is not None and info["window_id"] == grant.window_id:
                info["reserved"] = max(0, info["reserved"] - 1)
                info["used"] += 1
                if grant.external:
                    info["external_reserved"] = max(0, info["external_reserved"] - 1)
                    info["external_used"] += 1
                if grant.exec_class:
                    info["exec_reserved"] = max(0, info["exec_reserved"] - 1)
                    info["exec_used"] += 1
                self._log_line_locked(grant, info)
                # #448: the call is counted with its slot, under the same lock, so a
                # window its cap ends right below still includes this last call. Counting
                # never stands between the slot and the cap's end (``_observe``).
                _observe(tool_stats.add, info["calls"], grant.tool, "auto_approved")
                if info["used"] >= info["cap"] and closing is None and window.open:
                    closing_cap = self._end_locked("expired_cap")
        self._after(rows, closing)
        generation = grant.permit.generation
        if refused is not None:
            if refused != "permit_consumed":
                self._outcome_row(
                    "audit.yolo.void",
                    grant.window_id,
                    generation,
                    intent_ref=grant.intent,
                    reason_code=refused if refused in _SERVICE_STOPS else "consume_refused",
                )
            if closing_cap is not None:
                self._close(*closing_cap, human=False)
            return False, refused
        written = self._outcome_row(
            "audit.yolo.consumed", grant.window_id, generation, intent_ref=grant.intent
        )
        if not written:
            # #601 A8: no ``consumed`` row, no call (an intent row without it reads as not
            # run). The window is revoked by the failed row — or, when its cap already
            # ended it, its end becomes ``revoked_audit_failed`` here, before the close
            # sequence writes the ended row: abnormal (§11), so the marker stays open and
            # the chain latches the window again after a restart.
            with self._lock:
                self._latch_locked(grant.window_id, "window", "revoked_audit_failed")
                if closing_cap is not None:
                    closing_cap = self._abnormal_cap_end_locked(closing_cap)
        if closing_cap is not None:
            self._close(*closing_cap, human=False)
        return (True, "consumed") if written else (False, "audit_failed")

    def _log_line_locked(self, grant: AutoGrant, info: dict) -> None:
        """§12's log line for a consumed automatic approval: the tool (a registry name;
        anything else a fixed word), the ``#xxxx`` of its ``auto_approved`` row (as in the
        server log), the count after it and the cap; for an external tool (phase 6) the
        tier ``external`` and the sub-cap's count — never its destination. The page
        renders only these."""
        used, cap = info["used"], info["cap"]
        if self._recent["window"] != grant.window_id[:8]:
            self._recent = {"window": grant.window_id[:8], "items": []}
        tool = (
            grant.tool if type(grant.tool) is str and _LINE_TOOL.fullmatch(grant.tool) else "tool"
        )
        items = self._recent["items"]
        item = {"n": used, "tool": tool, "ref": grant.intent[:4], "limit": cap, "tier": INTERNAL}
        if grant.external:
            item.update(
                tier=EXTERNAL,
                external_n=info["external_used"],
                external_limit=info["external_cap"],
            )
        if grant.exec_class:
            # Phase 7: the tool name and ``#ref`` only — never the command.
            item.update(tier=EXEC, exec_n=info["exec_used"], exec_limit=info["exec_cap"])
        items.append(item)
        del items[:-RECENT_LINES]

    def void(self, grant: Any, reason: str = "binding_mismatch") -> bool:
        """The gate refused the call after the approval (a later check, another hook):
        the permit returns its slot (once) and ``void`` is recorded."""
        if not isinstance(grant, AutoGrant):
            return False
        with self._lock:
            returned = self._authority.void(grant.permit)
            if returned:
                self._release_locked(grant.window_id, grant.external, grant.exec_class)
        if returned:
            self._outcome_row(
                "audit.yolo.void",
                grant.window_id,
                grant.permit.generation,
                intent_ref=grant.intent,
                reason_code=reason
                if reason in ("binding_mismatch", "window_ended")
                else "binding_mismatch",
            )
        return returned

    # -- what ran in the window (#448): counts only, never an argument ---------------

    def record_call(self, tool_name: Any, decision: Any) -> str | None:
        """The gate decided one tool call (``tool_stats.DECISIONS``; an automatic approval
        is counted by :meth:`consume`). Counted only while a window is open (active or
        paused); returns its window id (for :meth:`record_failure`), else ``None``."""
        window_id = None
        with self._lock:
            now, rows, closing = self._due_locked()
            window, info = self.machine.window, self._win
            if (
                closing is None
                and now is not None
                and window is not None
                and window.open
                and info is not None
                and info["window_id"] == window.window_id
            ):
                _observe(tool_stats.add, info["calls"], tool_name, decision)
                window_id = window.window_id
        self._after(rows, closing)
        return window_id

    def record_failure(self, window_id: Any) -> None:
        """A call counted in ``window_id`` ran and returned an error.

        The call belongs to the window it was counted in, also when that window ended
        while it ran (its cap ended it at this very call's ``consume``, or a person
        stopped it): the failure goes to the window's counter while its record is there
        (open, or still closing), else to ``last_end`` (memory) if that is this window.
        An ``audit.yolo.ended`` row already written is never rewritten."""
        if type(window_id) is not str:
            return
        with self._lock:
            info = self._win
            if info is not None and info["window_id"] == window_id and "calls" in info:
                _observe(tool_stats.add_failure, info["calls"])
            elif (
                self._last_end is not None
                and self._last_end_window == window_id
                and type(self._last_end.get("tool_calls")) is dict
            ):
                # A fresh dict: a ``status()`` already returned keeps what it saw.
                calls = _observe(tool_stats.snapshot, self._last_end["tool_calls"])
                if calls is not None:
                    _observe(tool_stats.add_failure, calls)
                    self._last_end["tool_calls"] = calls

    def record_turn(self) -> None:
        """An agent turn started (counted while a window is open)."""
        with self._lock:
            window, info = self.machine.window, self._win
            if (
                window is not None
                and window.open
                and info is not None
                and info["window_id"] == window.window_id
            ):
                info["turns"] += 1

    # -- the close sequence (§11 2.) -------------------------------------------------

    def _close(self, window: sm.Window, gen_from: int, gen_to: int, *, human: bool) -> None:
        """After the narrowing took effect in memory: ``audit.yolo.ended``, then — only
        for a normal end whose row was written — the marker ``closed`` (durable). Anything
        else latches (an abnormal end also when its row was written). Never undone."""
        reason = window.end_reason or "unknown"
        klass = sm.end_class(reason)
        with self._lock:
            info = self._win
            same = info is not None and info["window_id"] == window.window_id
            used = info.get("used", 0) if same else 0
            reserved = info.get("reserved", 0) if same else 0
            external_used = info.get("external_used", 0) if same else 0
            exec_used = info.get("exec_used", 0) if same else 0
            # #448: read under the lock, before anything that can fail (the row's write);
            # a snapshot that fails leaves the row without the optional counts.
            calls = (
                _observe(tool_stats.snapshot, info["calls"]) if same and "calls" in info else None
            )
            turns = info.get("turns", 0) if same and calls is not None else None
            # #596 (additive): only an end by the clocks carries why (fixed words, numbers).
            clock = (
                dict(info.get("clock_detail") or {})
                if same and reason == "revoked_clock_anomaly"
                else {}
            )
        row = self._row(
            "audit.yolo.ended",
            window,
            human=human,
            surface="admin" if human else "system",
            end_reason=reason,
            end_class=klass,
            used=used,
            reserved=reserved,
            external_used=external_used if window.tier == EXTERNAL else None,
            exec_used=exec_used if window.with_exec else None,
            tool_calls=calls,
            turns=turns,
            generation_from=gen_from,
            generation_to=gen_to,
            **clock,
        )
        written = self._write(row) is not None
        durable = written and klass == "normal" and self._marker_write("closed", window.window_id)
        now = self._now()
        with self._lock:
            try:
                if not written:
                    self._latch_locked(window.window_id, "window", "revoked_audit_failed")
                elif klass != "normal":
                    self._latch_locked(window.window_id, "window", reason)
                elif not durable:
                    self._latch_locked(window.window_id, "window", "marker_not_durable")
                current = self.machine.window
                if current is not None and current.window_id == window.window_id:
                    try:
                        self._publish(sm.close_done(self.machine, durable=durable))
                    except sm.Refused:
                        self._latch_locked(window.window_id, "window", "close_refused")
                if (
                    calls is not None
                    and self._win is not None
                    and self._win["window_id"] == window.window_id
                ):
                    # #448: a failure that arrived while the row was written (memory only).
                    calls = _observe(tool_stats.snapshot, self._win["calls"]) or calls
                self._last_end_window = window.window_id
                self._last_end = {
                    "id": window.window_id[:8],
                    "tier": window.tier,
                    "reason": reason,
                    "class": klass,
                    "at": None if now is None else now.wall,
                    # #448: what ran in the window, after its end too (until the next).
                    "tool_calls": calls,
                    "turns": turns,
                }
                if self._win is not None and self._win["window_id"] == window.window_id:
                    self._win = None
            finally:
                self._closing -= 1

    # -- views -----------------------------------------------------------------------

    def _view_locked(self, now: ClockReading | None) -> dict | None:
        request = self.machine.request
        if request is None:
            return None
        left = 0
        if request.state == "pending" and self._t0 is not None and now is not None:
            left = max(0, int(EXPIRY_SEC - elapsed(self._t0, now) + 0.999))
        t0 = self._t0.wall if self._t0 is not None else None
        return {
            "id": request.request_id,
            "short": request.request_id[:8],
            "kind": "yolo_request",
            "tier": request.tier,
            "state": request.state,
            "reason": request.reason,
            "expires_in": left,
            "t0": t0,
            "ends_at": None if t0 is None else t0 + WINDOW_HOURS * 3600,
            "channel": self.channel,
            # Phase 7: the person chose "Include exec"; the host command asks for EXEC.
            "exec": request.with_exec,
            "word": "EXEC" if request.with_exec else "YOLO",
        }

    def _status_request_locked(self, now: ClockReading | None) -> dict | None:
        """#597: the request as ``status`` shows it. An ``opened`` request is shown while
        the window it opened is open (active / paused) and is ``None`` once that window has
        ended or is gone — never "an open request" for a window that is over. Every other
        state is shown as it is (its own word). View only: the machine is not changed."""
        request = self.machine.request
        if request is not None and request.state == "opened":
            window = self.machine.window
            if window is None or not window.open or window.window_id != request.window_id:
                return None
        return self._view_locked(now)

    def _window_view_locked(self, now: ClockReading | None) -> dict | None:
        window = self.machine.window
        if window is None:
            return None
        info = self._win if self._win and self._win["window_id"] == window.window_id else None
        t0 = info["t0"].wall if info else None
        left = 0
        if info and window.open and now is not None:
            left = max(0, int(WINDOW_SEC - elapsed(info["t0"], now) + 0.999))
        pause_detail = (
            (info.get("pause_breaker") or {}) if info and window.state == "paused" else {}
        )
        return {
            "id": window.window_id[:8],
            "state": window.state,
            "tier": window.tier,
            "reason": window.end_reason,
            "end_class": None if window.open else sm.end_class(window.end_reason or ""),
            "pause_reason": window.pause_reason,
            # #455 (additive): the breaker's reason codes behind ``pause_reason``.
            "pause_breaker_reasons": list(pause_detail.get("reasons", ())),
            "pause_breaker_inputs": dict(pause_detail.get("inputs", {})),
            "t0": t0,
            "ends_at": None if t0 is None else t0 + WINDOW_SEC,
            "remaining_sec": left,
            "cap": info["cap"] if info else None,
            "used": info.get("used", 0) if info else 0,
            # Phase 5a (additive): the slots of §6a, the pause state and the count of
            # automatic approvals handed out (consumed or still reserved).
            "reserved": info.get("reserved", 0) if info else 0,
            "limit": info["cap"] if info else None,
            "auto_approved": info.get("approved", 0) if info else 0,
            "paused": window.state == "paused",
            "pauses_last_hour": len(
                [
                    t
                    for t in info.get("pauses", ())
                    if now is not None and elapsed(t, now) < FLAP_WINDOW_SEC
                ]
            )
            if info
            else 0,
            "resume_in_sec": self._resume_in_locked(window, info, now),
            # Phase 6 (additive): the external sub-cap of a window of tier external
            # (``external_limit`` 0 / ``None`` in a window of tier internal), and the tier
            # in effect: ``external`` while sub-cap slots are left, else ``internal`` —
            # external tools ask again once it is used up (the window goes on).
            "external_used": info.get("external_used", 0) if info else 0,
            "external_reserved": info.get("external_reserved", 0) if info else 0,
            "external_limit": info.get("external_cap") if info else None,
            "effective_tier": EXTERNAL
            if info
            and window.tier == EXTERNAL
            and info.get("external_used", 0) < info.get("external_cap", 0)
            else INTERNAL,
            # Phase 7 (additive): the window's exec flag and its exec sub-cap
            # (``exec_limit`` 0 without the flag); ``exec_effective`` while slots are left.
            "exec": window.with_exec,
            "exec_used": info.get("exec_used", 0) if info else 0,
            "exec_reserved": info.get("exec_reserved", 0) if info else 0,
            "exec_limit": info.get("exec_cap") if info else None,
            "exec_effective": bool(
                info and window.with_exec and info.get("exec_used", 0) < info.get("exec_cap", 0)
            ),
            # #448 (additive): every tool call of the window's period by decision (the
            # caps above count automatic approvals only), and the turns it saw.
            "tool_calls": _observe(tool_stats.snapshot, info.get("calls") if info else None),
            "turns": info.get("turns", 0) if info else 0,
        }

    def _resume_in_locked(self, window, info, now) -> int | None:
        """Seconds of NORMAL left before a paused window resumes; ``None`` if the dwell
        has not started (the breaker is not NORMAL) or the window is not paused."""
        if window.state != "paused" or not info or now is None:
            return None
        since = info.get("normal_since")
        if since is None:
            return None
        return max(0, int(DWELL_SEC - elapsed(since, now) + 0.999))

    def _latch_views_locked(self) -> list[dict]:
        return [
            {
                "subject": latch.subject,
                "window_id": latch.window_id,
                "reason": latch.reason,
            }
            for latch in self._latches.values()
        ]

    def _open_locked(self) -> bool:
        request = self.machine.request
        return bool(
            self._busy
            or self._closing
            or self.machine.latched
            or self.machine.window is not None
            or self._win is not None
            or (request is not None and request.current)
        )

    def open_window_id(self) -> str | None:
        """#486: the id of the window that is open now (active or paused), else ``None``.

        Read only, under the lock (no expiry check, no row): ``delegate`` stops its child
        when this stops being the id the child started under."""
        with self._lock:
            window = self.machine.window
            return window.window_id if window is not None and window.open else None

    def is_open(self) -> bool:
        """Phase 7c: a request, a window, a close or a latch exists (settings refused)."""
        with self._lock:
            return self._open_locked()

    def set_caps(self, cap: Any, external_cap: Any, exec_cap: Any) -> bool:
        """Phase 7c: the caps of the **next** window (a person's confirmed settings save).

        Only while nothing is open — no request (pending or issuing), no window, no press
        or confirmation between its steps — checked under the lock the issuance reads the
        caps under, so a window always opens with one set of caps and an open window is
        never changed. Values are checked again here (built-in ints, ``1 ≤ cap ≤ CAP_MAX``,
        sub-caps ``0 ≤ … ≤ cap``); anything else changes nothing (``False``).
        """
        if not (
            cap_value(cap)
            and non_negative_int(external_cap, cap)
            and non_negative_int(exec_cap, cap)
        ):
            return False
        with self._lock:
            if self._open_locked():
                return False
            self.cap = cap
            self.external_cap = external_cap
            self.exec_cap = exec_cap
            return True

    def status(self) -> dict:
        # A look may narrow (expiry, clocks, a breaker sample pauses — fail closed) but
        # never loosens: the resume CAS runs only in the watchdog (``tick``), so a GET
        # poll never writes ``audit.yolo.resumed`` (PR #435 review; ≤ WATCHDOG_SEC late).
        with self._lock:
            now, rows, closing = self._due_locked()
            view = {
                "request": self._status_request_locked(now),
                "latched": self.machine.latched,
                "latch_reason": self.machine.latch_reason,
                "latches": self._latch_views_locked(),
                "window": self._window_view_locked(now),
                "last_end": None if self._last_end is None else dict(self._last_end),
                "auto_approvals": {
                    "window": self._recent["window"],
                    "items": [dict(item) for item in self._recent["items"]],
                },
            }
        self._after(rows, closing)
        return view

    def find(self, request_id: Any) -> bool:
        with self._lock:
            request = self.machine.request
            return (
                type(request_id) is str
                and request is not None
                and (request.request_id == request_id)
            )

    def window_short_of(self, request_id: Any) -> str | None:
        """The short id (as in the window's view) of the window this current request would
        open — allocated at its press — or ``None``. Read only (ADR 0014 phase 1: the
        dialog's tick is bound to its own press, never carried to another window)."""
        with self._lock:
            request = self.machine.request
            if type(request_id) is not str or request is None:
                return None
            if request.request_id != request_id:
                return None
            return request.window_id[:8]

    def ids(self) -> list[str]:
        with self._lock:
            request = self.machine.request
            return [] if request is None else [request.request_id]

    def pending(self) -> list[dict]:
        now = self._settle_due()
        with self._lock:
            view = self._view_locked(now)
            return [view] if view is not None and view["state"] == "pending" else []

    def show(self, request_id: str) -> dict:
        now = self._settle_due()
        with self._lock:
            view = self._view_locked(now)
            if view is None or view["id"] != request_id:
                raise HostOpRefused("host_operation_unknown")
            return view

    # -- the press -------------------------------------------------------------------

    def _press_check_locked(self, tier: str, now: ClockReading | None, with_exec: bool = False):
        """Under the lock: every refusal of a press (a refused state wins over the
        rate). Returns ``(request_id, window_id, candidate)``."""
        if now is None:
            raise sm.Refused("yolo_clock_failed")
        if self._busy:
            raise sm.Refused("yolo_request_pending")
        self._presses = [t for t in self._presses if elapsed(t, now) < 3600.0]
        limited = (
            self._negative is not None and elapsed(self._negative, now) < COOLDOWN_SEC
        ) or len(self._presses) >= HOURLY_MAX
        request_id, window_id = self._token(), self._token()
        candidate = sm.press(
            self.machine,
            request_id=request_id,
            window_id=window_id,
            tier=tier,
            with_exec=with_exec,
        )
        if limited:
            raise sm.Refused("yolo_rate_limited")
        return request_id, window_id, candidate

    def press(self, tier: Any, with_exec: Any = False) -> dict:
        """The dashboard's request. Raises :class:`state.Refused` with a ``yolo_*`` code.

        ``with_exec`` (phase 7, §9 option C): exactly ``True`` asks for a window that also
        answers ``shell`` / ``batch``; refused (``yolo_exec_not_allowed``) unless the exec
        choice is offered (exec tools exposed, allowance ``external``) and the tier is
        ``external``. Anything but a built-in ``bool`` is refused too."""
        if type(tier) is not str:
            raise sm.Refused("yolo_tier_not_allowed")
        if type(with_exec) is not bool or (with_exec and not self.exec_offered):
            raise sm.Refused("yolo_exec_not_allowed")
        if not self._clocks_supported:
            raise sm.Refused("clock_not_measured")
        # Cheap refusals first (state, latch already known, rate limits): a refused retry
        # never pays for the full chain verification of the judgement below.
        rows, closing = [], None
        try:
            with self._lock:
                now, rows, closing = self._due_locked()
                self._press_check_locked(tier, now, with_exec)
                self._busy = True  # nothing opens or closes while the storage is read
        finally:
            self._after(rows, closing)
        try:
            self._judge_storage()  # §11: again for every request
        finally:
            with self._lock:
                self._busy = False
        rows, closing = [], None
        try:
            with self._lock:
                # The clocks again, under the lock: never a reading from before it.
                now, rows, closing = self._due_locked()
                request_id, _window_id, candidate = self._press_check_locked(tier, now, with_exec)
                self._busy = True
                built_on = self._rev
        finally:
            self._after(rows, closing)
        row = self._row(
            "audit.yolo.requested",
            candidate.request,
            human=True,
            surface="admin",
            request_id=request_id,
            tier=tier,
            expected=EXPECTED_IDLE,
            exec=with_exec,
        )
        written = self._write(row)
        token = secrets.token_hex(16) if self.channel == "browser" else None
        with self._lock:
            self._busy = False
            if not written:
                raise sm.Refused("yolo_audit_failed")
            moved = self._rev != built_on
            if not moved:
                self._publish(candidate)
                self._t0 = now
                self._presses.append(now)
                self._confirm_digest = None if token is None else (request_id, token_digest(token))
                view = self._view_locked(now)
            else:
                # Something narrowed (a latch, a fence) while the row was written: the
                # older candidate is never published over it (CAS on the revision).
                code = "yolo_latched" if self.machine.latched else "yolo_request_stale"
        if moved:
            # The requested row exists for a request that never became current: close it.
            self._narrowing_rows(
                [
                    self._row(
                        "audit.yolo.request_fenced",
                        candidate.request,
                        human=False,
                        surface="system",
                        request_id=request_id,
                        reason_code="cancel",
                    )
                ]
            )
            raise sm.Refused(code)
        if self._on_created is not None:
            self._on_created(dict(view))
        if token is not None:
            # Only in the press's answer; never in a status or a list.
            return {**view, "confirm_token": token}
        return view

    def cancel(self, request_id: Any = None) -> dict:
        """The dashboard's cancel (a narrowing, never needs the host)."""
        if request_id is not None and type(request_id) is not str:
            raise sm.Refused("yolo_request_stale")
        # A narrowing: it never waits for (or fails on) the clocks.
        with self._lock:
            request = self.machine.request
            self._publish(sm.cancel(self.machine, request_id))
            now = self._now()
            pending = request is not None and request.state == "pending"
            row = self._row(
                "audit.yolo.request_cancelled" if pending else "audit.yolo.request_fenced",
                request,
                human=True,
                surface="admin",
                request_id=request.request_id,
                reason_code="cancel",
            )
            view = self._view_locked(now)
        self._narrowing_rows([row])
        return view

    # -- the host side ---------------------------------------------------------------

    def decide(
        self,
        request_id: Any,
        *,
        incarnation: Any,
        decision: Any,
        principal: Any,
        channel: Any,
    ) -> dict:
        """A person's answer on the host (``confirm`` / ``deny``; also the browser
        confirmation after its checks, ``channel == "browser"``). A confirmation runs the
        issuance of §6a (phase 3)."""
        if type(incarnation) is not str or incarnation != self.incarnation:
            raise HostOpRefused("host_operation_incarnation")
        if type(decision) is not str or decision not in ("confirm", "deny"):
            raise HostOpRefused("host_operation_mismatch")
        if type(principal) is not str or type(channel) is not str:
            raise HostOpRefused("host_operation_mismatch")
        surface = "admin" if channel == "browser" else "cli"
        due_rows, due_closing = [], None
        try:
            with self._lock:
                # The clocks under the lock: the 2-minute expiry is judged at this CAS.
                now, due_rows, due_closing = self._due_locked()
                row = self._decide_locked(request_id, decision, surface, now)
                request = self.machine.request
        finally:
            self._after(due_rows, due_closing)
        if decision == "deny":
            self._narrowing_rows([row])
            with self._lock:
                return self._view_locked(now)
        confirmed_id = self._write(row)
        if confirmed_id is None:
            with self._lock:
                self._busy = False
                self._publish(sm.issue_failed(self.machine, request_id, started_written=False))
                # Not durable anywhere (no marker, no started row): this latch does not
                # survive a restart — the explicit exception of ADR 0012 §11.
                self._latch_locked(request.window_id, "window", "aborted_issue_failed")
                return self._view_locked(now)
        return self._issue(request, confirmed_id)

    def _decide_locked(self, request_id, decision, surface, now):
        """Under the lock: the checks and the CAS of a decision; the row to write."""
        if now is None:
            raise HostOpRefused("host_channel_unavailable")
        request = self.machine.request
        if type(request_id) is not str or request is None or (request.request_id != request_id):
            raise HostOpRefused("host_operation_unknown")
        if request.state != "pending":
            code = (
                "host_operation_expired"
                if request.state == "expired"
                else ("host_operation_decided")
            )
            raise HostOpRefused(code, state=request.state)
        if self._busy:
            raise HostOpRefused("host_operation_decided", state="issuing")
        if decision == "deny":
            self._publish(sm.deny(self.machine, request_id))
            self._negative = now
            row = self._row(
                "audit.yolo.denied",
                request,
                human=True,
                surface=surface,
                request_id=request_id,
                reason_code="denied",
            )
        else:
            try:
                issuing = sm.confirm_begin(self.machine, request_id)
            except sm.Refused as error:
                raise HostOpRefused("host_operation_decided", state=error.code) from None
            # W1 (#296): ``issuing`` is published and the shared authority epoch read in
            # one step under the authority leaf lock (§6a (1)); a move of the shared
            # epoch after it fences the issuance at the final CAS.
            _, self._issue_authority_epoch = self._swap_at_shared_epoch_locked(issuing)
            self._busy = True
            row = self._row(
                "audit.yolo.confirmed",
                request,
                human=True,
                surface=surface,
                request_id=request_id,
                tier=request.tier,
                expected=f"pending:{self.machine.request.fence_seen}",
                exec=request.with_exec,
            )
        return row

    # -- §6a issuance, steps (2) and (3) ----------------------------------------------

    def _issue(self, request: sm.Request, confirmed_id: str) -> dict:
        """Outside the lock: marker ``open`` → ``started`` → read-back; then the final CAS.

        Runs to the end also when a stop, ``/new``, cancel or an epoch move fenced the
        request meanwhile: the final CAS sees the fence and the window never becomes
        usable (a normal ``ended(issue_aborted_*)`` closes the marker again)."""
        t0 = self._t0
        receipts = [self._receipt(confirmed_id, "audit.yolo.confirmed", request)]
        marker_open = self._marker_write("open", request.window_id)
        started_id = None
        if marker_open:
            started_id = self._write(
                self._row(
                    "audit.yolo.started",
                    request,
                    human=False,
                    surface="system",
                    request_id=request.request_id,
                    tier=request.tier,
                    cap=self.cap,
                    external_cap=self.external_cap if request.tier == "external" else 0,
                    exec=request.with_exec,
                    exec_cap=self.exec_cap if request.with_exec else 0,
                    t0=t0.wall,
                    expires_at=t0.wall + WINDOW_SEC,
                    confirmed_ref=confirmed_id,
                    generation_from=0,
                    generation_to=1,
                )
            )
        if started_id is not None:
            receipts.append(self._receipt(started_id, "audit.yolo.started", request))
        self._before_final_cas()
        closing = None
        with self._lock:
            # §6a (3): every input of the final CAS is read **here, under the lock** — the
            # clocks, the writer's sticky health flag, the breaker level — never a value
            # taken before the lock (a lock wait or a failed audit write in between would
            # otherwise publish a window on stale facts). This is the linearization point:
            # a writer failure flagged before it refuses the window; one after it ends the
            # open window at the next look (``revoked_audit_failed``, ``_due_locked``).
            self._busy = False
            # W1 (#296): the shared epoch read with ``issuing`` (``confirm_begin``). If it
            # moved (a breaker change, a rules save, the kill switch — not only this
            # machine's moves) the commit below is refused atomically and the request is
            # fenced, as by ``advance_epoch`` (§6b row 7).
            seen, self._issue_authority_epoch = self._issue_authority_epoch, None
            fence_rows = []
            now = self._now()
            healthy = self._healthy()
            breaker_normal = self._breaker_normal()
            clock = "revoked_clock_anomaly" if now is None else clock_verdict(t0, t0, now)
            if started_id is None:
                self._publish(
                    sm.issue_failed(self.machine, request.request_id, started_written=False)
                )
                # An open marker with no started row is an orphan (§11): latched across
                # a restart too, until a person unlatches it.
                subject = "orphan_marker" if marker_open else "window"
                self._latch_locked(request.window_id, subject, "aborted_issue_failed")
            else:
                # The issuance is judged by the one loosening function too (§2:
                # ``host_confirmed_request``, tier within the allowance).
                window_after = {
                    "yolo": request.tier,
                    "state": "active",
                    "cap": self.cap,
                    "expires_at": t0.wall + WINDOW_SEC,
                    "grant_id": request.window_id,
                    "incarnation": self.incarnation,
                    "exec": request.with_exec,
                }
                allowed = self._loosening_allowed(
                    {"yolo": "off"}, window_after, "host_confirmed_request"
                )
                if not allowed:
                    # Not an audit failure: an invariant (the tier and the window fields
                    # were checked when the request was made). Same latch, own log line.
                    logger.warning(
                        "YOLO: the issuance was refused by the loosening judgement "
                        "(policy, not the audit); latched as aborted_issue_failed"
                    )
                machine, outcome = sm.issue_commit(
                    self.machine,
                    request.request_id,
                    request.window_id,
                    started_written=True,
                    receipts_ok=all(receipts) and len(receipts) == 2 and allowed,
                    writer_healthy=healthy,
                    incarnation_same=True,  # checked in each receipt (incarnation_ref)
                    before_expiry=clock is None,
                    breaker_normal=breaker_normal,
                )
                if outcome == "opened":
                    # The commit itself, atomic with the shared epoch: no narrowing can
                    # land between the check and the swap (everything above — clocks,
                    # health, breaker, loosening — was gathered before this lock).
                    committed, _ = self._swap_at_shared_epoch_locked(
                        machine, expected=-1 if seen is None else seen
                    )
                    if not committed:
                        fence_rows += self._fence_issuing_locked(request)
                        machine, outcome = sm.issue_commit(
                            self.machine,
                            request.request_id,
                            request.window_id,
                            started_written=True,
                            receipts_ok=all(receipts) and len(receipts) == 2 and allowed,
                            writer_healthy=healthy,
                            incarnation_same=True,
                            before_expiry=clock is None,
                            breaker_normal=breaker_normal,
                        )
                        self._publish(machine)
                else:
                    self._publish(machine)
                if outcome == "opened":
                    self._recent = {"window": request.window_id[:8], "items": []}
                    self._win = {
                        "window_id": request.window_id,
                        "t0": t0,
                        "last": now,
                        "cap": self.cap,
                        "started": receipts[1],  # the window's standing receipt
                        # Phase 5a (§6a slots, §6 pause / resume).
                        "used": 0,
                        "reserved": 0,
                        "approved": 0,
                        # Phase 6: the external sub-cap (0 in a window of tier internal).
                        "external_cap": self.external_cap if request.tier == EXTERNAL else 0,
                        "external_used": 0,
                        "external_reserved": 0,
                        # Phase 7: the exec sub-cap (0 without "Include exec").
                        "exec_cap": self.exec_cap if request.with_exec else 0,
                        "exec_used": 0,
                        "exec_reserved": 0,
                        # #448: what ran in the window (counts only).
                        "calls": _observe(tool_stats.empty) or {},
                        "turns": 0,
                        "pauses": [],
                        "normal_since": None,
                        # A window that opens paused resumes against its started row.
                        "paused_ref": None
                        if machine.window.state == "active"
                        else (machine.window.generation, receipts[1].event_id),
                        "resuming": False,
                    }
                else:
                    if outcome == "aborted_issue_failed":
                        self._latch_locked(request.window_id, "window", outcome)
                    self._closing += 1
                    closing = (machine.window, 1, 2)
            view = self._view_locked(now)
        self._narrowing_rows(fence_rows)
        if closing is not None:
            self._close(*closing, human=False)
        return view

    def _swap_at_shared_epoch_locked(self, machine: sm.Machine, *, expected=None):
        """Under the lock: publish ``machine`` (its own epoch unchanged) inside the
        authority leaf lock, atomically with reading the shared epoch — refused when
        ``expected`` is given and the shared epoch is not it (or the kill flag is set).
        ``(published, shared epoch)``. The swap is in memory only (lock order: this lock
        → the leaf lock)."""
        if machine.epoch != self.machine.epoch:
            raise ValueError("a swap never moves the machine's epoch")

        def swap(_epoch: int) -> None:
            self.machine = machine
            self._rev += 1

        published, epoch, _ = self._authority.commit_at_epoch(swap, expected=expected)
        return published, epoch

    def _fence_issuing_locked(self, request: sm.Request) -> list:
        """Under the lock: fence the ``issuing`` request (``fenced(epoch)``, as by
        :meth:`advance_epoch`); the ``request_fenced`` row to write after the lock."""
        current = self.machine.request
        if current is None or current.request_id != request.request_id:
            return []
        if current.state != "issuing":
            return []
        self._publish(sm.advance_epoch(self.machine))
        return [
            self._row(
                "audit.yolo.request_fenced",
                current,
                human=False,
                surface="system",
                request_id=current.request_id,
                reason_code="epoch",
            )
        ]

    # -- the browser side (revision 6) ----------------------------------------------

    def confirm_in_browser(
        self,
        request_id: Any,
        *,
        token: Any,
        acknowledged: Any,
        external_acknowledged: Any,
        instance: Any,
        exec_acknowledged: Any = None,
    ) -> dict:
        """The person's confirmation in the dashboard (browser channel only): the press's
        token, the dialog's checkbox and — for tier external — the second checkbox ("sending
        outside becomes possible"), and — for a request that includes exec (phase 7) — the
        third ("commands run and can bypass Stira's protections"), each exactly ``True``.
        A missing tick leaves the request pending (``host_not_acknowledged``)."""
        if self.channel != "browser":
            raise HostOpRefused("host_confirm_on_host")
        if not instance_matches(self.instance, instance):
            raise HostOpRefused("host_operation_incarnation")
        self._settle_due()
        with self._lock:
            request = self.machine.request
            bound = self._confirm_digest
            if (
                type(request_id) is not str
                or request is None
                or request.request_id != request_id
                or bound is None
                or bound[0] != request_id
                or not token_matches(bound[1], token)
            ):
                raise HostOpRefused("host_operation_mismatch")
            if request.state == "pending" and not (
                is_acknowledged(acknowledged)
                and (request.tier != "external" or is_acknowledged(external_acknowledged))
                and (not request.with_exec or is_acknowledged(exec_acknowledged))
            ):
                raise HostOpRefused("host_not_acknowledged", state=request.state)
        return self.decide(
            request_id,
            incarnation=self.incarnation,
            decision="confirm",
            principal=_BROWSER_PRINCIPAL,
            channel="browser",
        )

    # -- stop / /new (revision 6: the operation stores are revoked with them) --------

    def fence(self, reason: str, *, human: bool = True) -> dict | None:
        """Stop / ``/new``: everything current — the request (pending or issuing) and the
        window — is fenced or ended (§6, §6b); never refused, names no id, needs no
        confirmation and no breaker check. ``human=False``: the server's own stop.
        Returns the request's view, or ``None``."""
        if reason not in ("stop", "new_session"):
            raise ValueError("reason")
        with self._lock:
            # The narrowing comes first and depends on nothing that can fail (no clock,
            # no audit write, no breaker): the window is unusable from this line on.
            request = self.machine.request
            current = request is not None and request.current
            window = self.machine.window
            was_open = window is not None and window.open
            self._publish(
                sm.new_session(self.machine) if reason == "new_session" else sm.stop(self.machine)
            )
            rows = (
                [
                    self._row(
                        "audit.yolo.request_fenced",
                        request,
                        human=human,
                        surface="admin" if human else "system",
                        request_id=request.request_id,
                        reason_code=reason,
                    )
                ]
                if current
                else []
            )
            closing = None
            if was_open:
                self._closing += 1
                closing = (self.machine.window, window.generation, window.generation + 1)
            view = self._view_locked(self._now())
        self._narrowing_rows(rows)
        if closing is not None:
            self._close(*closing, human=human)
        return view

    def advance_epoch(self) -> None:
        """The authority epoch moved (§6b row 7): an ``issuing`` request is fenced
        (``fenced(epoch)``, a normal end); nothing else changes."""
        with self._lock:
            request = self.machine.request
            issuing = request is not None and request.state == "issuing"
            self._publish(sm.advance_epoch(self.machine))
            rows = (
                [
                    self._row(
                        "audit.yolo.request_fenced",
                        request,
                        human=False,
                        surface="system",
                        request_id=request.request_id,
                        reason_code="epoch",
                    )
                ]
                if issuing
                else []
            )
        self._narrowing_rows(rows)

    # -- unlatch (§11): one latch per decision, append-only ---------------------------

    def unlatch_target(self, window_id: Any) -> dict:
        """What an unlatch of ``window_id`` would close, as the confirmed operation's
        target (§2f): the latch, the marker's digest and the chain head. Refused for an
        id that is not latched, and while anything is open or being opened / closed."""
        if type(window_id) is not str:
            raise sm.Refused("yolo_not_latched")
        with self._lock:
            latch = self._latches.get(window_id)
            machine = self.machine
            if latch is None:
                raise sm.Refused("yolo_not_latched")
            if (
                self._busy
                or self._closing
                or (machine.request is not None and machine.request.current)
                or (machine.window is not None and machine.window.open)
            ):
                raise sm.Refused("yolo_busy")
        with self._marker_lock:
            marker = self._marker.read() if self._marker is not None else MarkerRead("missing")
        # Bound to the YOLO state only (the marker and the chain's YOLO rows), not to the
        # whole chain head: an unrelated event written while the person confirms does not
        # make the unlatch stale; any YOLO change does.
        try:
            history = self._ledger.yolo_history() if self._ledger is not None else []
        except Exception:  # noqa: BLE001 - an unreadable chain refuses
            history = None
        if history is None:
            raise sm.Refused("yolo_audit_failed")
        yolo_state = hashlib.sha256(
            json.dumps(history, sort_keys=True, default=str).encode()
        ).hexdigest()
        return {
            "window_id": window_id,
            "subject": latch.subject,
            "reason": latch.reason,
            "marker": marker.digest,
            "yolo_state": yolo_state,
        }

    def _head_seq(self) -> int | None:
        try:
            seq = self._ledger.yolo_head_seq() if self._ledger is not None else 0
        except Exception:  # noqa: BLE001 - an unreadable head refuses
            return None
        return seq if type(seq) is int and seq >= 0 else None

    def unlatch(self, window_id: Any, *, expected: dict, op_id: str) -> dict:
        """A person's unlatch of exactly one latch (after the confirmed operation):
        re-read the target (any change → ``stale_operation``), commit
        ``audit.yolo.unlatched`` (failure → refused, nothing changed), then the marker
        ``closed_by_unlatch`` when it is this window's and open, then
        ``audit.yolo.unlatch_applied`` (best effort). Never opens a window."""
        target = self.unlatch_target(window_id)
        if target != expected:
            raise sm.Refused("stale_operation")
        seq = self._head_seq()
        if seq is None:
            raise sm.Refused("yolo_audit_failed")
        subject = sm.Window(window_id, "internal", "ended", 0)
        row = self._row(
            "audit.yolo.unlatched",
            subject,
            human=True,
            surface="admin",
            op=op_id,
            latch_subject=target["subject"],
            marker_digest=target["marker"],
            chain_seq_seen=seq,
            expected="marker:" + target["marker"],
        )
        if self._write(row) is None:
            raise sm.Refused("yolo_audit_failed")
        operation_id = row[1]["operation_id"]
        with self._marker_lock:
            marker = self._marker.read() if self._marker is not None else MarkerRead("missing")
        if marker.status == "ok" and marker.window_id == window_id and marker.state == "open":
            # A failure here is repaired by the next judgement: the unlatched row closes it.
            self._marker_write("closed_by_unlatch", window_id, operation_id=operation_id)
        self._write(
            self._row(
                "audit.yolo.unlatch_applied",
                subject,
                human=True,
                surface="admin",
                latch_subject=row[1]["latch_subject"],
                intent_ref=operation_id,
            )
        )
        with self._lock:
            self._latches.pop(window_id, None)
            if self._latches:
                first = next(iter(self._latches.values()))
                self._publish(replace(self.machine, latch_reason=first.reason))
            elif self.machine.latched:
                self._publish(sm.unlatch(self.machine))
            return {
                "unlatched": window_id,
                "latched": self.machine.latched,
                "latches": self._latch_views_locked(),
            }


def clock_verdict(t0: ClockReading, last: ClockReading, now: ClockReading) -> str | None:
    """§7: how the clocks end a window, or ``None``. Checked at every look (status,
    press, the watchdog). ``t0`` the press, ``last`` the previous check.

    * **anomaly** (abnormal, latched): the wall clock went back by more than
      :data:`CLOCK_SLACK_SEC` since the last check, or the wall clock's delta since the
      press fell behind the sleep-including clock's by more than that;
    * **sleep**: the sleep-including clock moved more than :data:`SLEEP_SEC` beyond the
      sleep-excluding one since the last check;
    * **time**: the largest of the three deltas since the press reached six hours.

    The anomaly wins over the normal ends (fail closed: it latches)."""
    d_wall, d_boot = now.wall - t0.wall, now.boot - t0.boot
    if now.wall < last.wall - CLOCK_SLACK_SEC or d_wall < d_boot - CLOCK_SLACK_SEC:
        return "revoked_clock_anomaly"
    if (now.boot - last.boot) - (now.mono - last.mono) > SLEEP_SEC:
        return "expired_sleep"
    if elapsed(t0, now) >= WINDOW_SEC:
        return "expired_time"
    return None


def clock_detail(t0: ClockReading, last: ClockReading, now: ClockReading) -> dict:
    """#596: why :func:`clock_verdict` said ``revoked_clock_anomaly`` — a fixed word and
    the deltas it compared (seconds, rounded to ms), for the ``audit.yolo.ended`` row.

    ``clock_check``: ``wall_backwards`` (the wall clock went back beyond the slack since
    the last check, ``last_d_wall``) or ``wall_behind_boot`` (``d_wall`` fell behind
    ``d_boot`` since the press beyond the slack). Same test, same order as the verdict;
    the verdict itself is not changed."""
    d_wall, d_boot = now.wall - t0.wall, now.boot - t0.boot
    backwards = now.wall < last.wall - CLOCK_SLACK_SEC
    return {
        "clock_check": "wall_backwards" if backwards else "wall_behind_boot",
        "d_wall": round(d_wall, 3),
        "d_boot": round(d_boot, 3),
        "last_d_wall": round(now.wall - last.wall, 3),
        "last_d_boot": round(now.boot - last.boot, 3),
        "clock_slack_sec": CLOCK_SLACK_SEC,
    }


def wall_clock_text(seconds: float | None) -> str:
    """``HH:MM`` local time (fixed format for the host confirmation)."""
    if seconds is None:
        return "--:--"
    return _time.strftime("%H:%M", _time.localtime(seconds))
