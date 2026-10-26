"""The ``lifecycle_event`` contract and its hash-chained log (ADR 0011 §12; W2 of #296).

"Never stop silently": every stop, latch, health change and gap is one
``lifecycle_event`` with one schema — **codes and ids only**:

``{event_id, subject_kind, subject_id, from, to, reason_code, incarnation, epoch,
ts_wall}`` (+ ``gap_from_wall`` only on ``gateway_down``, the start of the gap).

* ``event_id``: 32 hex; ``incarnation``: 32 hex (written for the record, never read back
  as an identity); ``epoch``: an integer ≥ 0; ``ts_wall``: a finite number ≥ 0;
* ``subject_kind`` one of :data:`SUBJECTS`, ``subject_id`` checked **per kind** — a fixed
  word (``service``, ``engine``, ``daemon``, ``process``), a latch name, or a 32-hex id
  (a YOLO window). No goal text, no tool argument, no message can be a subject id;
* ``from`` / ``to`` one of :data:`STATES`, ``reason_code`` one of :data:`REASONS`;
* every value is exactly a built-in ``str`` / ``int`` / ``float`` (no subclass), and
  **any other key is refused** (:class:`LifecycleSchemaError`).

**The log** ``authority-lifecycle.jsonl`` (data directory, ``0600``): one JSON line per
event, ``{"seq", "prev", "hash", "event"}``, ``hash`` = ``sha256("stiracore-lifecycle:v1:"
+ canonical JSON without "hash")``, ``prev`` the previous line's hash. Appended with
``O_APPEND``, flushed per line, opened under the same safe-open rules as the epoch store
(:mod:`.private_file`); a file replaced behind the writer, a broken chain, a torn last
line or an unsafe file make the log unusable (sticky; nothing more is written, the file
is never repaired here). An append that would take the file past :data:`ROTATE_BYTES`
first moves it to ``authority-lifecycle.1.jsonl`` (replacing an older one) and the new
file continues the chain (its first ``prev`` is the last hash of the old one); the same
happens at start for a larger file.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import threading
from pathlib import Path
from typing import Any

from . import private_file
from .latches import LATCHES

__all__ = [
    "GRANT_REASONS",
    "ITERATION_REASONS",
    "LOG_FILE",
    "OLD_LOG_FILE",
    "PLAYBOOK_CODE",
    "REASONS",
    "ROTATE_BYTES",
    "STATES",
    "SUBJECTS",
    "TICK_REASONS",
    "LifecycleLog",
    "LifecycleSchemaError",
    "latch_state",
    "make_event",
    "validate",
]

LOG_FILE = "authority-lifecycle.jsonl"
OLD_LOG_FILE = "authority-lifecycle.1.jsonl"
ROTATE_BYTES = 1 << 20
_MAX_BYTES = 2 << 20  # the reader's limit per segment: above ROTATE_BYTES + one line
_ZERO = "0" * 64
_HEX32 = re.compile(r"[0-9a-f]{32}")
_HEX64 = re.compile(r"[0-9a-f]{64}")

# subject_kind -> the allowed subject ids (a set of words) or the pattern of an id.
SUBJECTS: dict[str, frozenset[str] | re.Pattern] = {
    "authority": frozenset({"service"}),
    "latch": frozenset(LATCHES),
    "breaker": frozenset({"engine"}),
    "yolo_window": _HEX32,
    "heartbeat": frozenset({"daemon"}),
    "gateway": frozenset({"process"}),
    # W4: a goal grant and its goal (the grant id, 32 hex — never the goal text) and the
    # grant store's health.
    "grant": _HEX32,
    "goal": _HEX32,
    "grants": frozenset({"store"}),
    # W5: one goal iteration of a grant (the grant id; codes only).
    "grant_iteration": _HEX32,
    # W6d: the scheduler's heartbeat start of a grant (the grant id; codes only).
    "grant_tick": _HEX32,
}

STATES = frozenset(
    {
        "running",
        "stopped",
        "set",
        "cleared",
        "healthy",
        "unhealthy",
        "stale",
        "normal",
        "warning",
        "coherence",
        "critical",
        "unknown",
        "active",
        "paused",
        "ended",
        "down",
        "up",
        # W4: grant and goal states (``stiracore.grants.state``).
        "halted",
        "revoked",
        "expired",
        "blocked",
        "budget_limited",
        "awaiting_verification",
        "complete",
        "unmet",
        # W5: a goal iteration (``stiracore.grants.executor``).
        "idle",
        "yielded",
    }
)

# The YOLO window codes observed (ADR 0012 §6; ``stiracore.yolo.state`` — a test keeps
# them in step without importing YOLO here).
YOLO_REASONS = frozenset(
    {
        "breaker_warning",
        "breaker_coherence",
        "breaker_critical",
        "breaker_unknown",
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
        "revoked_audit_failed",
        "revoked_clock_anomaly",
        "aborted_issue_failed",
    }
)
# Unhealthy reason codes of the authority service (W1 + W2).
UNHEALTHY_REASONS = frozenset(
    {
        "epoch_store_unsafe",
        "epoch_store_unreadable",
        "epoch_store_malformed",
        "epoch_store_hash_mismatch",
        "epoch_store_sequence_gap",
        "epoch_store_not_monotonic",
        "epoch_store_replaced",
        "epoch_store_write_failed",
        "epoch_settle_unavailable",
        "epoch_settle_failed",
        "latch_store_unsafe",
        "latch_store_unreadable",
        "latch_store_malformed",
        "latch_store_hash_mismatch",
        "latch_store_sequence_gap",
        "latch_store_replaced",
        "latch_store_write_failed",
        "latched_unhealthy",
        "lifecycle_log_unsafe",
        "lifecycle_log_unreadable",
        "lifecycle_log_malformed",
        "lifecycle_log_hash_mismatch",
        "lifecycle_log_sequence_gap",
        "lifecycle_log_torn",
        "lifecycle_log_replaced",
        "lifecycle_write_failed",
        "lifecycle_event_invalid",
        "lifecycle_queue_full",
        "outbox_unsafe",
        "outbox_unreadable",
        "outbox_malformed",
        "outbox_write_failed",
        "outbox_replaced",
        "watchdog_failed",
        "watchdog_unavailable",
        "watchdog_record_failed",
        "cancel_unavailable",
        "cancel_overdue",
        "kill_persist_timeout",
        "kill_persist_unavailable",
        "kill_marker_unavailable",
        "kill_marker_unsafe",
        "latch_store_rolled_back",
        "unhealthy_other",
    }
)
# The grant / goal transition codes (W4; ``stiracore.grants.state.all_reason_codes`` and
# the grant store's health — a test keeps them in step without importing grants here).
GRANT_REASONS = frozenset(
    {
        "grant_issued",
        "grant_resumed",
        "goal_issued",
        "goal_resumed",
        "verification_rejected",
        "goal_paused",
        "goal_budget_limited",
        "goal_claimed",
        "goal_verified",
        "goal_unmet",
        "late_verification_pass",
        "late_verification_fail",
        "paused_user",
        "paused_goal_paused",
        "paused_goal_blocked",
        "paused_goal_budget_limited",
        "halted_breaker_warning",
        "halted_breaker_critical",
        "revoked_user",
        "revoked_kill_switch",
        "revoked_restart",
        "revoked_session_reset",
        "revoked_clock_anomaly",
        "revoked_unhealthy",
        "expired_ttl",
        "ended_complete",
        "blocked_no_progress",
        "blocked_loop_detected",
        "blocked_max_iterations",
        "blocked_same_blocker",
        "blocked_approval_timeout",
        "blocked_verification",
        # W5: the executor's stops and the agent's fixed blocker codes.
        "blocked_run_checks_unavailable",
        "blocked_iteration_timeout",
        "blocked_workspace_changed",
        "blocked_needs_decision",
        "blocked_out_of_scope",
        "blocked_missing_input",
        "grant_store_unsafe",
        "grant_store_unreadable",
        "grant_store_malformed",
        "grant_store_hash_mismatch",
        "grant_store_sequence_gap",
        "grant_store_replaced",
        "grant_store_write_failed",
        "grant_sync_failed",
        "grant_persist_unavailable",
    }
)
# W5: how a goal iteration ended (``stiracore.grants.executor.END_CODES`` — a test keeps
# them in step without importing grants here). Its start carries the trusted playbook's
# version as a code: ``playbook_<first 12 hex of its SHA-256>`` (:data:`PLAYBOOK_CODE`).
ITERATION_REASONS = frozenset(
    {
        "iteration_done",
        "iteration_claimed",
        "iteration_reported_blocked",
        "iteration_yielded_user_turn",
        "iteration_permit_stale",
        "iteration_budget_refused",
        "iteration_token_count_unavailable",
        "iteration_approval_timeout",
        "iteration_run_checks_unavailable",
        "iteration_timeout",
        "iteration_model_error",
        "iteration_tool_refused",
        "iteration_stopped",
        "iteration_shutdown",
        "iteration_failed",
        "iteration_workspace_changed",
        "iteration_no_action",
    }
)
# W6d: the heartbeat start's codes (``stiracore.scheduler.grant_tick.EVENT_REASONS`` — a
# test keeps them in step without importing the scheduler here): a heartbeat-started
# iteration, and the state-class skips (one event per grant and reason in a process).
TICK_REASONS = frozenset(
    {
        "tick_started",
        "tick_skipped_grant_state",
        "tick_skipped_goal_state",
        "tick_skipped_awaiting_verification",
        "tick_skipped_heartbeat_unhealthy",
        "tick_skipped_breaker",
        "tick_skipped_kill",
        "tick_skipped_authority_unhealthy",
        "tick_skipped_store_unhealthy",
        "tick_skipped_clock",
        "tick_skipped_ttl",
        "tick_skipped_max_iterations",
        "tick_skipped_budget",
        "tick_skipped_no_action",
        "tick_skipped_repeated_failure",
        "tick_skipped_executor_unavailable",
    }
)
PLAYBOOK_CODE = re.compile(r"playbook_[0-9a-f]{12}")
REASONS = (
    frozenset(
        {
            "kill_switch",
            "human_unlatch",
            "heartbeat_stale",
            "heartbeat_resolved",
            "gateway_down",
            "reason_not_recorded",
            "latch_store_reset",
        }
    )
    | YOLO_REASONS
    | UNHEALTHY_REASONS
    | GRANT_REASONS
    | ITERATION_REASONS
    | TICK_REASONS
)

_KEYS = frozenset(
    {
        "event_id",
        "subject_kind",
        "subject_id",
        "from",
        "to",
        "reason_code",
        "incarnation",
        "epoch",
        "ts_wall",
    }
)
_OPTIONAL = frozenset({"gap_from_wall"})


class LifecycleSchemaError(ValueError):
    """An event outside the contract (a code / id outside the lists, a free-text field,
    an unknown key): refused, nothing is written."""


_MAX_EPOCH = 1 << 53
_MAX_WALL = 1e11  # seconds: far beyond any real wall clock, small enough to stay exact


def _number(value: Any) -> bool:
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(value) and 0 <= value <= _MAX_WALL
    except OverflowError:  # a huge int
        return False


def validate(event: Any) -> dict:
    """The event as a plain ``dict`` if it is inside the contract, else raise."""
    if type(event) is not dict:
        raise LifecycleSchemaError("not a dict")
    keys = set(event)
    if not _KEYS <= keys or not keys <= _KEYS | _OPTIONAL:
        raise LifecycleSchemaError("keys")
    for key in event:
        if type(key) is not str:
            raise LifecycleSchemaError("key type")
    for key in ("event_id", "incarnation"):
        if type(event[key]) is not str or not _HEX32.fullmatch(event[key]):
            raise LifecycleSchemaError(key)
    kind = event["subject_kind"]
    if type(kind) is not str or kind not in SUBJECTS:
        raise LifecycleSchemaError("subject_kind")
    allowed = SUBJECTS[kind]
    ident = event["subject_id"]
    if type(ident) is not str:
        raise LifecycleSchemaError("subject_id")
    if isinstance(allowed, frozenset):
        if ident not in allowed:
            raise LifecycleSchemaError("subject_id")
    elif not allowed.fullmatch(ident):
        raise LifecycleSchemaError("subject_id")
    for key in ("from", "to"):
        if type(event[key]) is not str or event[key] not in STATES:
            raise LifecycleSchemaError(key)
    code = event["reason_code"]
    if type(code) is not str or not (
        code in REASONS or (kind == "grant_iteration" and PLAYBOOK_CODE.fullmatch(code))
    ):
        raise LifecycleSchemaError("reason_code")
    if type(event["epoch"]) is not int or not 0 <= event["epoch"] < _MAX_EPOCH:
        raise LifecycleSchemaError("epoch")
    if not _number(event["ts_wall"]):
        raise LifecycleSchemaError("ts_wall")
    if "gap_from_wall" in event and (
        event["reason_code"] != "gateway_down" or not _number(event["gap_from_wall"])
    ):
        raise LifecycleSchemaError("gap_from_wall")
    return {key: event[key] for key in sorted(event)}


def make_event(
    subject_kind: str,
    subject_id: str,
    from_state: str,
    to_state: str,
    reason_code: str,
    *,
    incarnation: str,
    epoch: int,
    ts_wall: float,
    event_id: str | None = None,
    gap_from_wall: float | None = None,
) -> dict:
    """A validated event (a fresh random ``event_id`` unless given)."""
    event = {
        "event_id": event_id or os.urandom(16).hex(),
        "subject_kind": subject_kind,
        "subject_id": subject_id,
        "from": from_state,
        "to": to_state,
        "reason_code": reason_code,
        "incarnation": incarnation,
        "epoch": epoch,
        "ts_wall": ts_wall,
    }
    if gap_from_wall is not None:
        event["gap_from_wall"] = gap_from_wall
    return validate(event)


def latch_state(events) -> dict[str, bool]:
    """Per latch name, whether the log's last ``latch`` event leaves it set (``set`` /
    ``stopped``) or cleared (``cleared``) — for the start-up cross-check (W2 fix 1)."""
    state: dict[str, bool] = {}
    for event in events:
        if event.get("subject_kind") != "latch":
            continue
        if event["to"] in ("set", "stopped"):
            state[event["subject_id"]] = True
        elif event["to"] == "cleared":
            state[event["subject_id"]] = False
    return state


def _digest(line: dict) -> str:
    body = {k: v for k, v in line.items() if k != "hash"}
    text = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(("stiracore-lifecycle:v1:" + text).encode("ascii")).hexdigest()


def _parse(raw: bytes, prev: str | None, seq: int | None) -> tuple[list[dict] | None, str | None]:
    """Lines of one segment. ``prev`` / ``seq``: where the chain must continue from (the
    previous segment's end), or ``None`` for an unknown start (an old segment)."""
    if not raw:
        return [], None
    if not raw.endswith(b"\n"):
        return None, "lifecycle_log_torn"
    lines: list[dict] = []
    for text in raw.split(b"\n")[:-1]:
        try:
            item = json.loads(text.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return None, "lifecycle_log_malformed"
        if type(item) is not dict or set(item) != {"seq", "prev", "hash", "event"}:
            return None, "lifecycle_log_malformed"
        if type(item["seq"]) is not int or item["seq"] < 1:
            return None, "lifecycle_log_malformed"
        if not all(type(item[k]) is str and _HEX64.fullmatch(item[k]) for k in ("prev", "hash")):
            return None, "lifecycle_log_malformed"
        try:
            validate(item["event"])
        except LifecycleSchemaError:
            return None, "lifecycle_log_malformed"
        if item["hash"] != _digest(item):
            return None, "lifecycle_log_hash_mismatch"
        if lines:
            expected_prev, expected_seq = lines[-1]["hash"], lines[-1]["seq"] + 1
        else:
            expected_prev, expected_seq = prev, seq
        if expected_seq is not None and item["seq"] != expected_seq:
            return None, "lifecycle_log_sequence_gap"
        if expected_prev is not None and item["prev"] != expected_prev:
            return None, "lifecycle_log_hash_mismatch"
        lines.append(item)
    return lines, None


class LifecycleLog:
    """The append-only, hash-chained lifecycle log of one data directory: the current
    segment and at most one older one. Every method takes the log's own I/O lock (never
    the authority's leaf lock).

    **Bounded (W2 fix 1, M5):** an append that would take the current segment past
    :data:`ROTATE_BYTES` first moves it to the older segment (replacing the one before)
    and starts a new file that continues the chain — at write time, not only at start —
    so a segment never exceeds ``ROTATE_BYTES`` + one line, below the reader's limit
    (:data:`_MAX_BYTES`). Events of a dropped segment leave the log (their notices stay
    in the outbox)."""

    def __init__(self, data_dir: Path | None, *, rotate: bool = True) -> None:
        self._dir = None if data_dir is None else Path(data_dir)
        self._lock = threading.Lock()
        self._bad: str | None = None
        self._identity: tuple[int, int] | None = None
        self._old_identity: tuple[int, int] | None = None
        self._size = 0
        self._last_hash = _ZERO
        self._last_seq = 0
        self._old_tail: tuple[str, int] | None = None  # (hash, seq) the current continues
        self._old_events: list[dict] = []
        self._cur_events: list[dict] = []
        # W2 fix 2: what each segment must contain — a running digest of the current one
        # (every append adds its line) and the sealed digest + size of the older one —
        # so a pass detects any in-place change of either (A-M2).
        self._cur_hash = hashlib.sha256()
        self._old_digest: str | None = None
        self._old_size = 0
        if self._dir is None:
            return
        old = private_file.read(
            self._dir / OLD_LOG_FILE, max_bytes=_MAX_BYTES, prefix="lifecycle_log"
        )
        old_lines: list[dict] = []
        if old.status == "bad":
            self._bad = old.reason
            return
        if old.status == "ok":
            old_lines, reason = _parse(old.raw, None, None)
            if old_lines is None:
                self._bad = reason
                return
            self._old_identity = old.identity
            self._old_digest = hashlib.sha256(old.raw).hexdigest()
            self._old_size = len(old.raw)
        current = private_file.read(
            self._dir / LOG_FILE, max_bytes=_MAX_BYTES, prefix="lifecycle_log"
        )
        if current.status == "bad":
            self._bad = current.reason
            return
        lines: list[dict] = []
        if old_lines:
            self._old_tail = (old_lines[-1]["hash"], old_lines[-1]["seq"])
        if current.status == "ok":
            lines, reason = self._parse_current(current.raw)
            if lines is None:
                self._bad = reason
                return
            self._identity = current.identity
            self._size = len(current.raw)
            self._cur_hash = hashlib.sha256(current.raw)
        tail = lines or old_lines
        if tail:
            self._last_hash, self._last_seq = tail[-1]["hash"], tail[-1]["seq"]
        self._old_events = [item["event"] for item in old_lines]
        self._cur_events = [item["event"] for item in lines]
        # ``rotate=False`` (the offline host commands) skips only this start-up rotation;
        # every append is bounded the same way, with or without a server (A-M4). A file
        # over the cap (an older writer) still opens (the reader allows 2 MiB) and is
        # rotated here at the next server start.
        if rotate and current.status == "ok" and self._size > ROTATE_BYTES:
            try:
                self._rotate_locked()
            except OSError:
                self._bad = "lifecycle_write_failed"

    def _parse_current(self, raw: bytes):
        start_prev = self._old_tail[0] if self._old_tail else None
        start_seq = self._old_tail[1] + 1 if self._old_tail else None
        lines, reason = _parse(raw, start_prev, start_seq)
        if lines is None:
            return None, reason
        if self._old_tail is None and lines and (lines[0]["seq"] != 1 or lines[0]["prev"] != _ZERO):
            # Without an older segment the chain must start at its beginning.
            return None, "lifecycle_log_sequence_gap"
        return lines, None

    def _rotate_locked(self) -> None:
        """The current segment becomes the older one (replacing the one before); the
        next append creates a new current file continuing the chain."""
        os.replace(self._dir / LOG_FILE, self._dir / OLD_LOG_FILE)
        from stiracore.durable_dir import fsync_dir

        fsync_dir(self._dir)
        self._old_identity = self._identity
        self._old_digest = self._cur_hash.hexdigest()
        self._old_size = self._size
        self._identity = None
        self._size = 0
        self._cur_hash = hashlib.sha256()
        self._old_tail = (self._last_hash, self._last_seq) if self._last_seq else None
        self._old_events, self._cur_events = self._cur_events, []

    @property
    def unhealthy_reason(self) -> str | None:
        return self._bad

    def events(self) -> list[dict]:
        with self._lock:
            return [dict(event) for event in (*self._old_events, *self._cur_events)]

    def event_ids(self) -> set[str]:
        with self._lock:
            return {e["event_id"] for e in (*self._old_events, *self._cur_events)}

    def verify(self) -> str | None:
        """Re-read both segments and compare them with what this log read or wrote last
        (the watchdog's pass). A difference makes the log unusable (sticky)."""
        if self._dir is None:
            return self._bad
        with self._lock:
            if self._bad is not None:
                return self._bad
            problem = self._verify_locked()
            if problem is not None:
                self._bad = problem
            return self._bad

    def _verify_locked(self) -> str | None:
        """Both segments: the same file (identity), the same size and the same bytes
        (digest) as this log read, sealed or wrote last."""
        for name, identity, size, digest in (
            (OLD_LOG_FILE, self._old_identity, self._old_size, self._old_digest),
            (LOG_FILE, self._identity, self._size, self._cur_hash.hexdigest()),
        ):
            loaded = private_file.read(
                self._dir / name, max_bytes=_MAX_BYTES, prefix="lifecycle_log"
            )
            if loaded.status == "bad":
                return loaded.reason
            if identity is None:
                if loaded.status != "missing":
                    return "lifecycle_log_replaced"
                continue
            if loaded.status != "ok" or loaded.identity != identity:
                return "lifecycle_log_replaced"
            if len(loaded.raw) != size or hashlib.sha256(loaded.raw).hexdigest() != digest:
                return "lifecycle_log_replaced"
        return None

    def append(self, event: dict) -> None:
        """Validate and append one event, durably (rotating first when the segment is
        full). Raises :class:`LifecycleSchemaError` (nothing written) or ``OSError``
        (the log is then unusable, sticky)."""
        event = validate(event)
        with self._lock:
            if self._bad is not None:
                raise OSError(self._bad)
            line = {"seq": self._last_seq + 1, "prev": self._last_hash, "event": event}
            line["hash"] = _digest(line)
            body = (json.dumps(line, sort_keys=True, separators=(",", ":")) + "\n").encode("ascii")
            if self._dir is not None:
                try:
                    if self._size and self._size + len(body) > ROTATE_BYTES:
                        self._rotate_locked()
                    self._identity = private_file.append_line(
                        self._dir / LOG_FILE, body, self._identity
                    )
                except private_file.Replaced as error:
                    self._bad = "lifecycle_log_replaced"
                    raise OSError(self._bad) from error
                except OSError:
                    self._bad = "lifecycle_write_failed"
                    raise
                self._size += len(body)
                self._cur_hash.update(body)
            self._last_seq, self._last_hash = line["seq"], line["hash"]
            self._cur_events.append(event)
