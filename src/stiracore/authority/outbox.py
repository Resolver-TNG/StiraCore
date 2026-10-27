"""The lifecycle recorder and the outbox (ADR 0011 §12; W2 of #296).

:class:`LifecycleRecorder` is the one writer of lifecycle events of a process:

* :meth:`LifecycleRecorder.post` validates an event and queues it — **no I/O**, its own
  small lock only, so it may be called under a caller's lock (the engine's
  ``state_lock`` for a breaker change, the YOLO request lock). The queue is bounded
  (:data:`MAX_PENDING`); an overflow is a failed write;
* :meth:`LifecycleRecorder.drain` writes what is queued (the log, then the outbox) under
  the recorder's I/O lock — called by the watchdog thread (woken by every post), by the
  kill route right after the kill, and by the shutdown step. It reports ``clean`` only
  when nothing is left **and** the recorder is healthy;
* any failure — the log or the outbox cannot be written or read, an event outside the
  schema, a full queue — calls ``on_failure(reason)`` (the service latches ``unhealthy``)
  and is logged; it is never swallowed. The recorder stays unusable (sticky).

**The outbox** ``authority-outbox.json`` (``0600``, atomic replace, a hash over the
content): one notification per **dedupe key** ``(subject_kind, subject_id, incarnation,
epoch, to)``; an event with a key already present only joins that notification (at most
:data:`MAX_IDS` ids kept per notice, the rest counted). Each notification is unread until
a person acknowledges it (``POST /api/authority/outbox/ack`` by any of its event ids;
idempotent). Notifications survive a restart.

**Bounded, never resurrecting (W2 fix 1, M2 / M5).** Besides the notices the file keeps
``seen`` (every logged event id that already made or joined a notice) and ``acked``
(acknowledged event ids, the tombstones), both pruned to the ids still in the lifecycle
log — so the rebuild never makes an acknowledged or trimmed notice unread again, and an
old acknowledged id still answers ``already``. At most :data:`MAX_ITEMS` notices are
shown: acknowledged ones are dropped first; beyond that the oldest unread
**informational** ones are coalesced into one unread "N earlier notices" item with a count
per reason code (their events stay in the log segments). A stop notice (a latch, the
authority, a stop / unhealthy transition, a kill / reset / unlatch, an abnormal YOLO end)
is never coalesced; it stays individually shown and acknowledgeable. Nothing unread is
silently dropped and the file stays far below the reader's limit (a write that would not
be readable is refused, not written).

**Rebuild / repair** (:meth:`LifecycleRecorder.repair`, at start and on every watchdog
pass): every event in the log without a notification gets one (unread); every set latch
whose stop id ``(incarnation, epoch)`` has no lifecycle record gets a synthesized event
``latch: unknown → stopped (reason_not_recorded)`` (recorded in the log like any other).
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import threading
import time
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import private_file
from .latches import Stop
from .lifecycle_events import REASONS, LifecycleLog, LifecycleSchemaError, make_event, validate

__all__ = [
    "MAX_IDS",
    "MAX_ITEMS",
    "MAX_PENDING",
    "OUTBOX_FILE",
    "LifecycleRecorder",
    "critical",
    "dedupe_key",
    "notice_key",
]

logger = logging.getLogger(__name__)

OUTBOX_FILE = "authority-outbox.json"
MAX_PENDING = 256
MAX_ITEMS = 256
MAX_IDS = 16
_MAX_BYTES = 4 << 20
_HEX32 = re.compile(r"[0-9a-f]{32}")
_HEX64 = re.compile(r"[0-9a-f]{64}")
_ITEM_KEYS = frozenset(
    {"key", "event_ids", "count", "event", "acked", "synthesized", "coalesced", "reasons"}
)
# Reason codes of stop notices that are never coalesced (plus every latch / authority
# subject and every ``stopped`` / ``unhealthy`` transition).
_CRITICAL_REASONS = frozenset(
    {"kill_switch", "reason_not_recorded", "latch_store_reset", "human_unlatch"}
    | {"revoked_audit_failed", "revoked_clock_anomaly", "aborted_issue_failed"}
)


# Subject kinds whose ids are a fixed set (a stop notice keeps them apart); a YOLO window
# id is random, so its stop notices are keyed by the kind and the reason only.
_FIXED_SUBJECTS = frozenset({"authority", "latch", "breaker", "heartbeat", "gateway", "grants"})


def notice_key(event: dict) -> str:
    """A stop notice: ``stop | subject kind | subject id (fixed kinds only) | reason`` —
    no incarnation, no window id, so the stop notices are bounded by the fixed codes
    (W2 fix 4); a repeat joins it. Any other notice: :func:`dedupe_key`."""
    if critical(event):
        kind = event["subject_kind"]
        ident = event["subject_id"] if kind in _FIXED_SUBJECTS else "*"
        return "|".join(("stop", kind, ident, event["reason_code"]))
    return dedupe_key(event)


def _add_id(item: dict, event_id: str) -> None:
    """Keep the first ids and always the newest last (the one an acknowledgement names)."""
    if len(item["event_ids"]) < MAX_IDS:
        item["event_ids"].append(event_id)
    else:
        item["event_ids"][-1] = event_id


# W4: a grant or goal moving into one of these states is a stop notice (ADR 0011 §12
# "covered" states). Grant ids are random, so their stop notices are keyed by kind and
# reason only (``notice_key``), like a YOLO window's.
_GRANT_SUBJECTS = frozenset({"grant", "goal"})
_GRANT_STOP_STATES = frozenset(
    {
        "paused",
        "halted",
        "revoked",
        "expired",
        "blocked",
        "budget_limited",
        "awaiting_verification",
        "unmet",
    }
)


def critical(event: dict) -> bool:
    """A stop notice: a latch or the authority itself, a ``stopped`` / ``unhealthy``
    transition, a kill / reset / unlatch, an abnormal YOLO end, a grant / goal stop."""
    return (
        event["subject_kind"] in ("latch", "authority")
        or event["to"] in ("stopped", "unhealthy")
        or event["reason_code"] in _CRITICAL_REASONS
        or (event["subject_kind"] in _GRANT_SUBJECTS and event["to"] in _GRANT_STOP_STATES)
    )


_COALESCED = "coalesced"


def dedupe_key(event: dict) -> str:
    """subject + incarnation + epoch + to (ADR 0011 §12), as one string."""
    return "|".join(
        (
            event["subject_kind"],
            event["subject_id"],
            event["incarnation"],
            str(event["epoch"]),
            event["to"],
        )
    )


def _body_digest(body: dict) -> str:
    text = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(("stiracore-outbox:v2:" + text).encode("ascii")).hexdigest()


def _ids_ok(ids, *, empty: bool) -> bool:
    return (
        type(ids) is list
        and (empty or bool(ids))
        and all(type(i) is str and _HEX32.fullmatch(i) for i in ids)
    )


def _parse_outbox(raw: bytes) -> tuple[dict | None, str | None]:
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None, "outbox_malformed"
    if (
        type(data) is not dict
        or set(data) != {"v", "items", "seen", "acked", "hash"}
        or data["v"] != 2
        or type(data["items"]) is not list
        or type(data["hash"]) is not str
        or not _ids_ok(data["seen"], empty=True)
        or not _ids_ok(data["acked"], empty=True)
    ):
        return None, "outbox_malformed"
    for item in data["items"]:
        if type(item) is not dict or set(item) != _ITEM_KEYS:
            return None, "outbox_malformed"
        if type(item["acked"]) is not bool or type(item["synthesized"]) is not bool:
            return None, "outbox_malformed"
        if not _ids_ok(item["event_ids"], empty=False) or len(item["event_ids"]) > MAX_IDS:
            return None, "outbox_malformed"
        for key in ("count", "coalesced"):
            if type(item[key]) is not int or not 0 <= item[key] < (1 << 53):
                return None, "outbox_malformed"
        reasons = item["reasons"]
        if type(reasons) is not dict or not all(
            type(k) is str and k in REASONS and type(v) is int and 0 <= v < (1 << 53)
            for k, v in reasons.items()
        ):
            return None, "outbox_malformed"
        try:
            validate(item["event"])
        except LifecycleSchemaError:
            return None, "outbox_malformed"
        expected = _COALESCED if item["coalesced"] else notice_key(item["event"])
        if item["key"] != expected:
            return None, "outbox_malformed"
    body = {k: data[k] for k in ("v", "items", "seen", "acked")}
    if data["hash"] != _body_digest(body):
        return None, "outbox_malformed"
    return data, None


def _shown(event: dict) -> dict:
    shown = {k: v for k, v in event.items() if k != "incarnation"}
    shown["instance"] = hashlib.sha256(
        ("instance:" + event["incarnation"]).encode("ascii")
    ).hexdigest()[:16]
    return shown


class LifecycleRecorder:
    """Lifecycle log + outbox of one data directory (``None``: in memory, for tests)."""

    def __init__(
        self,
        data_dir: Path | None,
        *,
        on_failure: Callable[[str], None] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._dir = None if data_dir is None else Path(data_dir)
        self._on_failure = on_failure
        self._clock = clock
        self._queue_lock = threading.Lock()  # post(): no I/O under it
        self._pending: deque[dict] = deque()
        self._io_lock = threading.Lock()
        self._wake = threading.Event()
        self._bad: str | None = None
        self._failures = 0
        self.log = LifecycleLog(self._dir)
        self._items: list[dict] = []
        self._seen: set[str] = set()
        self._acked: set[str] = set()
        self._outbox_identity: tuple[int, ...] | None = None
        self._outbox_seen: bytes | None = None
        if self.log.unhealthy_reason is not None:
            self._bad = self.log.unhealthy_reason
        if self._dir is not None:
            loaded = private_file.read(
                self._dir / OUTBOX_FILE, max_bytes=_MAX_BYTES, prefix="outbox"
            )
            if loaded.status == "bad":
                self._bad = self._bad or loaded.reason
            elif loaded.status == "ok":
                data, reason = _parse_outbox(loaded.raw)
                if data is None:
                    self._bad = self._bad or reason
                else:
                    self._items = data["items"]
                    self._seen = set(data["seen"])
                    self._acked = set(data["acked"])
                    self._outbox_identity = loaded.identity
                    self._outbox_seen = loaded.raw

    # -- health ----------------------------------------------------------------------------

    @property
    def unhealthy_reason(self) -> str | None:
        return self._bad

    @property
    def wake(self) -> threading.Event:
        """Set by every :meth:`post` (the watchdog waits on it)."""
        return self._wake

    def _fail(self, reason: str) -> None:
        """Sticky; reported (the service latches unhealthy), never swallowed."""
        if self._bad is None:
            self._bad = reason
        self._failures += 1
        logger.warning("authority: a lifecycle record could not be written (%s); unhealthy", reason)
        if self._on_failure is not None:
            try:
                self._on_failure(reason)
            except Exception:  # noqa: BLE001 - the report itself must not raise further
                logger.warning("authority: the unhealthy latch could not be reported")

    # -- posting (no I/O) ------------------------------------------------------------------

    def post(self, event: dict) -> bool:
        """Queue one event (validated now). False when refused — an event outside the
        schema or a full queue — which is reported as a failure (unhealthy)."""
        try:
            event = validate(event)
        except LifecycleSchemaError:
            self._fail("lifecycle_event_invalid")
            return False
        with self._queue_lock:
            full = len(self._pending) >= MAX_PENDING
            if not full:
                self._pending.append(event)
        if full:
            self._fail("lifecycle_queue_full")
            return False
        self._wake.set()
        return True

    def pending_count(self) -> int:
        with self._queue_lock:
            return len(self._pending)

    # -- writing ---------------------------------------------------------------------------

    def drain(self) -> str:
        """Write every queued event (log, then the outbox): ``clean`` only when nothing is
        left **and** the recorder is healthy; else ``unhealthy`` (a failed write keeps the
        event queued; "queue drained" is not "durable")."""
        with self._io_lock:
            wrote = False
            while True:
                with self._queue_lock:
                    if not self._pending:
                        break
                    event = self._pending[0]
                if self._bad is not None:
                    break
                try:
                    self.log.append(event)
                except (OSError, LifecycleSchemaError):
                    self._fail(self.log.unhealthy_reason or "lifecycle_write_failed")
                    break
                with self._queue_lock:
                    self._pending.popleft()
                self._notify_locked(event)
                wrote = True
            if wrote and self._bad is None:
                self._save_outbox_locked()
            with self._queue_lock:
                left = bool(self._pending)
        return "clean" if not left and self._bad is None else "unhealthy"

    def _notify_locked(self, event: dict) -> bool:
        """One notice per key (:func:`notice_key`); True when a new notice was made. An
        event already seen (also one whose notice was trimmed or acknowledged) never
        makes one again. A repeat joins its notice (count + 1, its id is the newest); a
        stop that repeats also shows its newest event and is unread again."""
        event_id = event["event_id"]
        if event_id in self._seen:
            return False
        self._seen.add(event_id)
        key = notice_key(event)
        target = next((item for item in self._items if item["key"] == key), None)
        if target is not None:
            _add_id(target, event_id)
            target["count"] += 1
            if critical(event):
                target["event"] = dict(event)  # the newest subject / incarnation / time
                target["acked"] = False  # a stop that happened again is shown again
            return False
        self._items.append(
            {
                "key": key,
                "event_ids": [event_id],
                "count": 1,
                "event": dict(event),
                "acked": False,
                "synthesized": event["reason_code"] == "reason_not_recorded",
                "coalesced": 0,
                "reasons": {},
            }
        )
        self._trim_locked()
        return True

    def _trim_locked(self) -> None:
        """At most :data:`MAX_ITEMS` notices: the oldest acknowledged go first; then the
        oldest unread **informational** notices are coalesced into one unread item with
        a count per reason code. A stop notice (:func:`critical`: a latch, the authority,
        a stop / unhealthy transition, an abnormal YOLO end) is never merged away — it
        stays individually shown and acknowledgeable (W2 fix 2, A-M3)."""
        while len(self._items) > MAX_ITEMS:
            for index, item in enumerate(self._items):
                if item["acked"]:
                    del self._items[index]
                    break
            else:
                if not self._coalesce_one_locked():
                    return  # only stop notices are left: they are kept (human-bounded)

    def _coalesce_one_locked(self) -> bool:
        """Merge the oldest unread informational notice into the coalesced item (made
        from the oldest one when there is none). False when nothing can be merged."""
        victims = [
            index
            for index, item in enumerate(self._items)
            if not item["coalesced"] and not item["acked"] and not critical(item["event"])
        ]
        target = next(
            (item for item in self._items if item["coalesced"] and not item["acked"]), None
        )
        if target is None:
            if len(victims) < 2:
                return False
            target = self._items[victims[0]]
            target["key"] = _COALESCED
            target["coalesced"] = target["count"]
            target["reasons"] = {target["event"]["reason_code"]: target["count"]}
            victims = victims[1:]
        if not victims:
            return False
        other = self._items.pop(victims[0])
        target["coalesced"] += other["count"]
        target["count"] += other["count"]
        code = other["event"]["reason_code"]
        target["reasons"][code] = target["reasons"].get(code, 0) + other["count"]
        for event_id in other["event_ids"]:
            _add_id(target, event_id)
        return True

    def _save_outbox_locked(self) -> bool:
        if self._dir is None:
            self._trim_locked()
            return True
        if self._bad is not None:
            return False
        current = private_file.read(self._dir / OUTBOX_FILE, max_bytes=_MAX_BYTES, prefix="outbox")
        if self._outbox_identity is None:
            replaced = current.status != "missing"
        else:
            replaced = current.status != "ok" or (
                current.identity != self._outbox_identity or current.raw != self._outbox_seen
            )
        if replaced:
            self._fail(current.reason if current.status == "bad" else "outbox_replaced")
            return False
        self._trim_locked()
        content = {
            "v": 2,
            "items": self._items,
            "seen": sorted(self._seen),
            "acked": sorted(self._acked),
        }
        body = json.dumps({**content, "hash": _body_digest(content)}, sort_keys=True).encode(
            "ascii"
        )
        if len(body) > _MAX_BYTES:
            # Never write what the reader would refuse (cannot happen within the bounds).
            self._fail("outbox_write_failed")
            return False
        try:
            self._outbox_identity = private_file.replace(self._dir, OUTBOX_FILE, body)
        except OSError:
            self._fail("outbox_write_failed")
            return False
        self._outbox_seen = body
        return True

    def verify(self) -> str | None:
        """The watchdog's pass (M3): re-read the lifecycle log and the outbox and compare
        them with what this recorder read or wrote last; a difference is a failure
        (reported, sticky). The reason, or None."""
        if self._bad is not None:
            return self._bad
        problem = self.log.verify()
        if problem is None and self._dir is not None:
            with self._io_lock:
                current = private_file.read(
                    self._dir / OUTBOX_FILE, max_bytes=_MAX_BYTES, prefix="outbox"
                )
                if self._outbox_identity is None:
                    replaced = current.status != "missing"
                else:
                    replaced = current.status != "ok" or (
                        current.identity != self._outbox_identity
                        or current.raw != self._outbox_seen
                    )
                if replaced:
                    problem = current.reason if current.status == "bad" else "outbox_replaced"
        if problem is not None:
            self._fail(problem)
        return self._bad

    # -- the outbox ------------------------------------------------------------------------

    def outbox(self, *, unread_only: bool = False) -> list[dict]:
        """Notifications, newest first: the event (codes and ids; the incarnation only as
        a short ``instance`` reference, as the YOLO page shows it), its event ids, the
        count, unread / acknowledged, synthesized or recorded."""
        with self._io_lock:
            items = [
                {
                    "event": _shown(item["event"]),
                    "event_ids": list(item["event_ids"]),
                    "count": item["count"],
                    "unread": not item["acked"],
                    "synthesized": item["synthesized"],
                    "coalesced": item["coalesced"],
                    "reasons": dict(item["reasons"]),
                }
                for item in self._items
                if not (unread_only and item["acked"])
            ]
        return items[::-1]

    def unread_count(self) -> int:
        with self._io_lock:
            return sum(1 for item in self._items if not item["acked"])

    def ack(self, event_id: Any) -> dict:
        """Acknowledge a notification by its **newest** event id (a watermark, fix 4):
        ``{"acked": True, "already": bool}``. An id already acknowledged answers
        ``already`` and changes nothing — a replayed or stale acknowledgement never hides
        a newer occurrence; an older id of a notice that has a newer, unacknowledged one is
        ``outbox_stale`` (reload); an unknown id ``outbox_unknown``; a failed save
        ``outbox_write_failed`` (it stays unread)."""
        if type(event_id) is not str or not _HEX32.fullmatch(event_id):
            return {"acked": False, "code": "outbox_unknown"}
        with self._io_lock:
            if event_id in self._acked:
                return {"acked": True, "already": True}
            for item in self._items:
                if event_id in item["event_ids"]:
                    if item["event_ids"][-1] != event_id:
                        return {"acked": False, "code": "outbox_stale"}
                    # (an acknowledged newest id is in ``_acked``: answered above)
                    item["acked"] = True
                    before = set(self._acked)
                    self._acked.update(item["event_ids"])
                    if not self._save_outbox_locked():
                        item["acked"] = False
                        self._acked = before
                        return {"acked": False, "code": "outbox_write_failed"}
                    return {"acked": True, "already": False}
        return {"acked": False, "code": "outbox_unknown"}

    # -- rebuild / repair ------------------------------------------------------------------

    def repair(self, latched: dict[str, Stop | None]) -> int:
        """Every logged event gets a notification; every set latch without a record of
        its stop gets a synthesized ``reason_not_recorded`` event (posted and written
        now). The number of notifications added."""
        added = 0
        with self._io_lock:
            events = self.log.events()
            changed = False
            for event in events:
                before = len(self._seen)
                if self._notify_locked(event):
                    added += 1
                changed = changed or len(self._seen) != before
            # Bounded: only ids still in the log can come back, so only they are kept.
            in_log = {event["event_id"] for event in events}
            if not self._seen <= in_log or not self._acked <= in_log:
                self._seen &= in_log
                self._acked &= in_log
                changed = True
            recorded = {
                (e["subject_id"], e["incarnation"], e["epoch"])
                for e in events
                if e["subject_kind"] == "latch"
            }
            with self._queue_lock:
                queued = {
                    (e["subject_id"], e["incarnation"], e["epoch"])
                    for e in self._pending
                    if e["subject_kind"] == "latch"
                }
            missing = []
            for name, stop in sorted(latched.items()):
                if stop is None:
                    continue  # an unusable store: the service's unhealthy event covers it
                ref = (name, stop.incarnation, stop.epoch)
                if ref in recorded or ref in queued:
                    continue
                missing.append(
                    make_event(
                        "latch",
                        name,
                        "unknown",
                        "stopped",
                        "reason_not_recorded",
                        incarnation=stop.incarnation,
                        epoch=stop.epoch,
                        ts_wall=self._clock(),
                    )
                )
            if changed and self._bad is None:
                self._save_outbox_locked()
        for event in missing:
            if self.post(event):
                added += 1
        if missing:
            self.drain()
        return added
