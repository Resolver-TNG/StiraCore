"""Host-confirmed operations (ADR 0012 §8b, Open (12) (v), phase 2).

While ``STIRA_YOLO_ALLOW`` is set, a dashboard request for a loosening human operation —
``RECOVER``, a loosening tool-rule / hub / rollback save, a scheduler create / resume /
confirmed update — does not apply anything (an approve / edit of an approval card is one
press, as without the allowance: phase 2c, operator decision 2026-10-19). It only
**requests**: the server issues a one-time operation id bound to

* the operation kind and a digest of the request (route + JSON body: the *binding*),
* a digest of the target's state at the request (the *target generation*),
* the incarnation of this process (128 random bits, never read back),

and answers 428 ``host_confirmation_required``. A person confirms the operation in the
configured channel (the browser dialog by default; a host command on a TTY or the desktop
dialog with ``STIRA_YOLO_HOST_CONFIRM``). The page then sends the same
request again with the header :data:`HEADER` naming the operation; only that request,
with the same binding and an unchanged target generation, applies — once.

Contract (each pinned by ``tests/test_yolo_host_ops.py``):

* **single use**: a confirmed operation is consumed by the first matching request; a
  second one is refused (``host_operation_used``), and a second decision on the host is
  refused (``host_operation_decided``, with the first state);
* **expiry**: a confirmation later than :data:`EXPIRY_SEC` after the request is refused,
  and a confirmed operation must be applied within :data:`APPLY_GRACE_SEC`; elapsed time is
  the largest of the wall, sleep-including and sleep-excluding clock deltas (§7), so no
  clock can make an operation live longer;
* **other incarnation**: a decision quoting another incarnation is refused, and a restart
  forgets every operation (ids are only kept in memory);
* **stale target**: a target generation that changed since the request →
  ``stale_operation`` (the operation is spent);
* **rate limits**: at most :data:`MAX_OPEN` open operation(s) and :data:`RATE_MAX`
  requests per :data:`RATE_WINDOW_SEC` (``host_rate_limited``); pressing the same thing
  again while it is open returns the same operation.

**Only built-in values are evaluated** (review lesson): ids, digests and decisions must
be exactly ``str``; the binding digest accepts exactly ``dict`` / ``list`` / ``str`` /
``int`` / ``float`` / ``bool`` / ``None`` (no subclass, whose ``__eq__`` / ``__str__`` /
``__getitem__`` could answer differently each time) — anything else cannot be bound and
is refused (fail closed). No method of a caller-supplied object is called.

Cancelling an operation is a narrowing and stays a dashboard operation.

**Two channels (revision 6).** ``browser`` (the default): the 428 answer that creates the
operation carries a one-time **confirmation token** (only there; the server keeps its
digest); the page shows the fixed-format confirmation with a checkbox ("I checked what
this is") and sends ``POST /api/host-operations/<id>/confirm`` with the token,
``acknowledged: true`` and the instance reference (:meth:`HostOperations.confirm_in_browser`;
phase 2c, operator decision 2026-10-19: a checkbox instead of a typed word). Every new request
**replaces** the open one (it ends ``cancelled``): a confirmation is only ever shown for
the operation the last press created, never for one created earlier (by anyone).
``command`` / ``dialog`` (``STIRA_YOLO_HOST_CONFIRM``): the host decides
(:meth:`HostOperations.decide`) and the browser confirmation is refused. Single use,
expiry, staleness, the rate limits and the restart are the same for both.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
import threading
import time as _time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .allowance import ClockReading, clock_support, read_clocks

__all__ = [
    "APPLY_GRACE_SEC",
    "BROWSER_RATE_MAX",
    "CHANNELS",
    "CODES",
    "EXPIRY_SEC",
    "HEADER",
    "KINDS",
    "MAX_OPEN",
    "RATE_MAX",
    "RATE_WINDOW_SEC",
    "HostOpRefused",
    "HostOperations",
    "Unbindable",
    "binding_digest",
    "default_clock",
    "elapsed",
    "target_digest",
]

HEADER = "X-Stira-Host-Operation"
EXPIRY_SEC = 120.0  # ADR 0012 Open (5): request expiry 2 min
APPLY_GRACE_SEC = 30.0  # a confirmed operation must be applied this soon
MAX_OPEN = 1  # one open (pending or confirmed, not applied) operation at a time
RATE_WINDOW_SEC = 600.0
RATE_MAX = 12
# The browser channel shows nothing a request did not come from (no pop-up to spam), so
# its limit only bounds churn; it is looser so a person approving cards is not stopped.
BROWSER_RATE_MAX = 30
CHANNELS = ("browser", "command", "dialog")
_KEEP = 64  # settled operations kept to answer late requests with their state

# Kind -> the word a person types at the host command (the browser channel uses a checkbox).
# An approval card's approve / edit is not here: one press, as without the allowance (2c).
KINDS = {
    "rules_loosen": "APPROVE",
    "recover": "RECOVER",
    "scheduler_loosen": "APPROVE",
    # Phase 3 (§11): unlatching one latch after an abnormal window end (opens nothing).
    "yolo_unlatch": "UNLATCH",
    # Phase 7c: a loosening save of the YOLO values on the settings page.
    "yolo_settings": "APPROVE",
    # ADR 0011 W2 (#296): ``stira authority unlatch`` (the host command, server stopped).
    "authority_unlatch": "UNLATCH",
    # ADR 0011 W4 (#296): issuing a goal grant and resuming a stopped one (the grants
    # control plane's own browser-channel operations; nothing here is a YOLO operation).
    "grant_issue": "APPROVE",
    "grant_resume": "APPROVE",
    "grant_verify": "APPROVE",
}

# Refusal code -> HTTP status (the page wording keys are ``host.code_<code>``).
CODES = {
    "host_confirmation_required": 428,
    "stale_operation": 409,
    "host_operation_expired": 409,
    "host_operation_denied": 409,
    "host_operation_cancelled": 409,
    "host_operation_unknown": 409,
    "host_operation_pending": 409,
    "host_operation_used": 409,
    "host_operation_mismatch": 409,
    "host_rate_limited": 429,
    "host_channel_unavailable": 503,
    # The browser confirmation (revision 6): the checkbox not ticked (the operation stays open),
    # a request that is not a same-origin browser fetch, a channel that needs the host,
    # an agent tool call running, an instance reference of another process.
    "host_not_acknowledged": 400,
    "host_browser_refused": 403,
    "host_confirm_on_host": 409,
    "host_operation_incarnation": 409,
    # Host side only (the command / dialog), never sent to a page:
    "host_operation_decided": 409,
}

_HEX32 = re.compile(r"[0-9a-f]{32}")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_PREFIX = re.compile(r"[0-9a-f]{8,32}")
_TOKEN = re.compile(r"[0-9a-f]{32}")
_INSTANCE = re.compile(r"[0-9a-f]{16}")
_SUMMARY_TEXT = re.compile(r"[a-z0-9_:#.+-]{1,48}")
_SUMMARY_KEYS = frozenset(
    {"action", "tool", "args", "card", "source", "changes", "operation", "job", "count"}
)
_MAX_DEPTH = 32


class HostOpRefused(Exception):
    """A refused host-operation step; ``code`` is a key of :data:`CODES`."""

    def __init__(self, code: str, *, state: str | None = None):
        super().__init__(code)
        self.code = code
        self.state = state
        self.status = CODES.get(code, 409)


class Unbindable(ValueError):
    """A request body that cannot be bound (not built-in JSON values): refused."""


# --- digests (built-in values only) ---------------------------------------------------


def _plain(value: Any, depth: int = 0) -> Any:
    """A copy made of exactly the built-in JSON types; anything else raises Unbindable.

    ``type(x) is ...`` only: a subclass (``dict`` with a ``__getitem__``, ``str`` with an
    ``__eq__``) or any other object is never evaluated — it is refused.
    """
    if depth > _MAX_DEPTH:
        raise Unbindable("too deep")
    kind = type(value)
    if value is None or kind is bool or kind is str:
        return value
    if kind is int:
        return value
    if kind is float:
        if not math.isfinite(value):
            raise Unbindable("not a finite number")
        return value
    if kind is list:
        return [_plain(item, depth + 1) for item in list.__iter__(value)]
    if kind is dict:
        out = {}
        for key, item in dict.items(value):
            if type(key) is not str:
                raise Unbindable("a key that is not a string")
            out[key] = _plain(item, depth + 1)
        return out
    raise Unbindable("not a JSON value")


def _digest(value: Any) -> str:
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(text.encode("ascii")).hexdigest()


def binding_digest(kind: str, route: str, body: Any) -> str:
    """What the person confirms: the kind, the concrete route and the JSON body."""
    if type(kind) is not str or kind not in KINDS or type(route) is not str:
        raise Unbindable("kind or route")
    return _digest({"kind": kind, "route": route, "body": _plain(body)})


def target_digest(state: Any) -> str:
    """The target generation: a digest of server-side state (never caller objects).

    Server state may hold tuples / sets / other values; they are reduced to their type
    name and a sorted list, so two different states never digest alike by accident.
    """

    def norm(value: Any, depth: int = 0) -> Any:
        if depth > _MAX_DEPTH:
            return "<depth>"
        if value is None or type(value) in (bool, int, str):
            return value
        if type(value) is float:
            return repr(value)
        if isinstance(value, dict):
            return {str(k): norm(v, depth + 1) for k, v in sorted(value.items(), key=_key)}
        if isinstance(value, (list, tuple)):
            return [norm(v, depth + 1) for v in value]
        if isinstance(value, (set, frozenset)):
            return sorted((norm(v, depth + 1) for v in value), key=repr)
        return f"<{type(value).__name__}>{value!r}"

    return _digest(norm(state))


def _key(item):
    return str(item[0])


# --- clocks ---------------------------------------------------------------------------


def default_clock() -> ClockReading:
    """The §7 readings where measured; elsewhere wall and ``time.monotonic`` twice."""
    if clock_support().supported:
        return read_clocks()
    mono = _time.monotonic()
    return ClockReading(wall=_time.time(), boot=mono, mono=mono)


def elapsed(start: ClockReading, now: ClockReading) -> float:
    """The largest of the three deltas (§7): no clock makes it smaller than another says."""
    return max(now.wall - start.wall, now.boot - start.boot, now.mono - start.mono)


# --- the operations -------------------------------------------------------------------


@dataclass
class _Op:
    op_id: str
    kind: str
    binding: str
    generation: str
    summary: dict
    issued: ClockReading
    state: str = "pending"  # pending | confirmed | denied | cancelled | expired | used | stale
    decided: ClockReading | None = None
    principal: str | None = None
    channel: str | None = None
    seq: int = 0
    token_digest: str | None = None  # browser channel: digest of the confirmation token
    extra: dict = field(default_factory=dict)


def token_digest(token: str) -> str:
    return hashlib.sha256(("host-op-token:" + token).encode("ascii")).hexdigest()


def instance_ref(incarnation: str) -> str:
    """A short, non-secret reference to this process's incarnation for the page."""
    return hashlib.sha256(("instance:" + incarnation).encode("ascii")).hexdigest()[:16]


def is_acknowledged(value: Any) -> bool:
    """The browser dialog's checkbox: exactly the built-in ``True`` (not ``1``, not
    ``"true"``, not a subclass)."""
    return value is True


def instance_matches(expected: str, value: Any) -> bool:
    """The page's instance reference: exactly a ``str`` of 16 lowercase hex characters,
    checked **before** ``compare_digest`` (which raises ``TypeError`` on non-ASCII text)."""
    if type(value) is not str or not _INSTANCE.fullmatch(value):
        return False
    return secrets.compare_digest(value, expected)


def token_matches(digest: str | None, token: Any) -> bool:
    if digest is None or type(token) is not str or not _TOKEN.fullmatch(token):
        return False
    return secrets.compare_digest(token_digest(token), digest)


def _summary_ok(summary: Any) -> bool:
    if type(summary) is not dict or not set(summary) <= _SUMMARY_KEYS:
        return False
    for value in summary.values():
        if type(value) is int:
            if not 0 <= value <= 1_000_000:
                return False
        elif type(value) is str:
            if not _SUMMARY_TEXT.fullmatch(value):
                return False
        elif type(value) is list:
            if len(value) > 8 or not all(
                type(v) is str and _SUMMARY_TEXT.fullmatch(v) for v in value
            ):
                return False
        else:
            return False
    return True


class HostOperations:
    """One per process. The lock is held only for the checks and the state change."""

    def __init__(
        self,
        *,
        incarnation: str,
        clock: Callable[[], ClockReading] = default_clock,
        channel: str = "command",
        on_created: Callable[[dict], None] | None = None,
        expiry: float = EXPIRY_SEC,
        apply_grace: float = APPLY_GRACE_SEC,
        max_open: int = MAX_OPEN,
        rate_window: float = RATE_WINDOW_SEC,
        rate_max: int = RATE_MAX,
    ) -> None:
        if type(incarnation) is not str or not _HEX32.fullmatch(incarnation):
            raise ValueError("incarnation must be 32 hex characters")
        if channel not in CHANNELS:
            raise ValueError("unknown channel")
        self.incarnation = incarnation
        self.instance = instance_ref(incarnation)
        self.channel = channel
        self._clock = clock
        self._on_created = on_created
        self._expiry = expiry
        self._grace = apply_grace
        self._max_open = max_open
        self._rate_window = rate_window
        self._rate_max = (
            BROWSER_RATE_MAX if channel == "browser" and rate_max == RATE_MAX else rate_max
        )
        self._lock = threading.Lock()
        self._ops: dict[str, _Op] = {}
        self._requested: list[ClockReading] = []
        self._seq = 0

    # -- helpers (under the lock) --

    def _expire(self, now: ClockReading) -> None:
        for op in self._ops.values():
            if (
                op.state == "pending"
                and elapsed(op.issued, now) >= self._expiry
                or (
                    op.state == "confirmed"
                    and op.decided is not None
                    and elapsed(op.decided, now) >= self._grace
                )
            ):
                op.state = "expired"
        settled = [k for k, op in self._ops.items() if op.state not in ("pending", "confirmed")]
        for key in settled[: max(0, len(settled) - _KEEP)]:
            del self._ops[key]
        self._requested = [t for t in self._requested if elapsed(t, now) < self._rate_window]

    def _view(self, op: _Op, now: ClockReading) -> dict:
        if op.state == "pending":
            left = self._expiry - elapsed(op.issued, now)
        elif op.state == "confirmed" and op.decided is not None:
            left = self._grace - elapsed(op.decided, now)
        else:
            left = 0.0
        return {
            "id": op.op_id,
            "short": op.op_id[:8],
            "kind": op.kind,
            "state": op.state,
            "expires_in": max(0, math.ceil(left)),
            "summary": dict(op.summary),
            "code": op.binding[:8],
            "channel": self.channel,
            "word": KINDS[op.kind],
            "instance": self.instance,
        }

    def _get(self, op_id: Any) -> _Op:
        if type(op_id) is not str or not _HEX32.fullmatch(op_id):
            raise HostOpRefused("host_operation_unknown")
        op = self._ops.get(op_id)
        if op is None:
            raise HostOpRefused("host_operation_unknown")
        return op

    # -- the dashboard side --

    def request(self, kind: str, *, binding: str, generation: str, summary: dict) -> dict:
        """Issue (or return the identical open) operation; the caller answers 428."""
        if type(kind) is not str or kind not in KINDS:
            raise HostOpRefused("host_operation_mismatch")
        for digest in (binding, generation):
            if type(digest) is not str or not _HEX64.fullmatch(digest):
                raise HostOpRefused("host_operation_mismatch")
        if not _summary_ok(summary):
            raise HostOpRefused("host_operation_mismatch")
        now = self._clock()
        browser = self.channel == "browser"
        with self._lock:
            self._expire(now)
            open_ops = [op for op in self._ops.values() if op.state in ("pending", "confirmed")]
            if not browser:
                for op in open_ops:
                    if (op.kind, op.binding, op.generation) == (kind, binding, generation):
                        return self._view(op, now)
            if len(self._requested) >= self._rate_max or (
                not browser and len(open_ops) >= self._max_open
            ):
                raise HostOpRefused("host_rate_limited")
            if browser:
                # A new press replaces what is open: the page confirms only what it just
                # created (an operation made earlier, by anyone, is never shown).
                for op in open_ops:
                    op.state = "cancelled"
            self._seq += 1
            token = secrets.token_hex(16) if browser else None
            op = _Op(
                secrets.token_hex(16),
                kind,
                binding,
                generation,
                dict(summary),
                now,
                seq=self._seq,
                token_digest=None if token is None else token_digest(token),
            )
            self._ops[op.op_id] = op
            self._requested.append(now)
            view = self._view(op, now)
        if token is not None:
            # Only in the answer that created it; never in a status, a list or a log.
            return {**view, "confirm_token": token}
        if self._on_created is not None:
            self._on_created(dict(view))
        return view

    def consume(self, op_id: Any, *, kind: str, binding: str, generation: str) -> None:
        """The re-sent request: applies only for a confirmed, matching, fresh operation."""
        if type(kind) is not str or type(binding) is not str or type(generation) is not str:
            raise HostOpRefused("host_operation_mismatch")
        now = self._clock()
        with self._lock:
            self._expire(now)
            op = self._get(op_id)
            if op.kind != kind or op.binding != binding:
                raise HostOpRefused("host_operation_mismatch")
            refusal = {
                "pending": "host_operation_pending",
                "denied": "host_operation_denied",
                "cancelled": "host_operation_cancelled",
                "expired": "host_operation_expired",
                "used": "host_operation_used",
                "stale": "stale_operation",
            }.get(op.state)
            if refusal is not None:
                raise HostOpRefused(refusal, state=op.state)
            if op.generation != generation:
                op.state = "stale"
                raise HostOpRefused("stale_operation", state="stale")
            op.state = "used"

    def cancel(self, op_id: Any) -> dict:
        """A narrowing: an open operation ends ``cancelled``; never needs the host."""
        now = self._clock()
        with self._lock:
            self._expire(now)
            op = self._get(op_id)
            if op.state in ("pending", "confirmed"):
                op.state = "cancelled"
            return self._view(op, now)

    def revoke_all(self) -> int:
        """Stop / ``/new`` / server stop: every open operation ends ``cancelled`` (a
        narrowing; a confirmed one not yet applied is never applied). Never depends on
        the clocks: a clock that cannot be read skips only the expiry bookkeeping."""
        try:
            now = self._clock()
        except Exception:  # noqa: BLE001 - the narrowing below must still happen
            now = None
        with self._lock:
            if now is not None:
                self._expire(now)
            count = 0
            for op in self._ops.values():
                if op.state in ("pending", "confirmed"):
                    op.state = "cancelled"
                    count += 1
            return count

    def status(self, op_id: Any) -> dict:
        now = self._clock()
        with self._lock:
            self._expire(now)
            return self._view(self._get(op_id), now)

    # -- the host side --

    def pending(self) -> list[dict]:
        now = self._clock()
        with self._lock:
            self._expire(now)
            ops = sorted(
                (op for op in self._ops.values() if op.state == "pending"), key=lambda o: o.seq
            )
            return [self._view(op, now) for op in ops]

    def resolve(self, prefix: Any) -> str | None:
        """The one known id starting with ``prefix`` (8-32 lowercase hex), else ``None``."""
        if type(prefix) is not str or not _PREFIX.fullmatch(prefix):
            return None
        with self._lock:
            found = [key for key in self._ops if key.startswith(prefix)]
        return found[0] if len(found) == 1 else None

    def decide(
        self,
        op_id: Any,
        *,
        incarnation: Any,
        decision: Any,
        principal: Any,
        channel: Any,
    ) -> dict:
        """A person's answer on the host. Refused: another incarnation, an unknown id, a
        second decision (with the first state), a decision after the expiry."""
        if type(incarnation) is not str or incarnation != self.incarnation:
            raise HostOpRefused("host_operation_incarnation")
        if type(decision) is not str or decision not in ("confirm", "deny"):
            raise HostOpRefused("host_operation_mismatch")
        if type(principal) is not str or type(channel) is not str:
            raise HostOpRefused("host_operation_mismatch")
        now = self._clock()
        with self._lock:
            self._expire(now)
            op = self._get(op_id)
            if op.state != "pending":
                raise HostOpRefused(
                    "host_operation_expired" if op.state == "expired" else "host_operation_decided",
                    state=op.state,
                )
            op.state = "confirmed" if decision == "confirm" else "denied"
            op.decided, op.principal, op.channel = now, principal, channel
            return self._view(op, now)

    # -- the browser side (revision 6) --

    def confirm_in_browser(
        self, op_id: Any, *, token: Any, acknowledged: Any, instance: Any
    ) -> dict:
        """The person's confirmation in the dashboard (browser channel only): the dialog's
        checkbox (``acknowledged`` exactly ``True``) and the operation's token.

        Refused: the host channel is configured (``host_confirm_on_host``), another
        process's (or a malformed) instance reference, an unknown id, a wrong token (also
        for an operation another press replaced), anything but ``pending``. A missing tick
        leaves the operation open (``host_not_acknowledged``): it is not a denial."""
        if self.channel != "browser":
            raise HostOpRefused("host_confirm_on_host")
        if not instance_matches(self.instance, instance):
            raise HostOpRefused("host_operation_incarnation")
        now = self._clock()
        with self._lock:
            self._expire(now)
            op = self._get(op_id)
            if not token_matches(op.token_digest, token):
                raise HostOpRefused("host_operation_mismatch")
            if op.state != "pending":
                code = {
                    "expired": "host_operation_expired",
                    "cancelled": "host_operation_cancelled",
                    "denied": "host_operation_denied",
                    "used": "host_operation_used",
                    "stale": "stale_operation",
                }.get(op.state, "host_operation_decided")
                raise HostOpRefused(code, state=op.state)
            if not is_acknowledged(acknowledged):
                raise HostOpRefused("host_not_acknowledged", state=op.state)
            op.state = "confirmed"
            op.decided, op.principal, op.channel = now, "browser:dashboard", "browser"
            return self._view(op, now)
