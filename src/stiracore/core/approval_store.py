"""Durable, hash-chained HITL approval store (#240, ADR 0006).

The store is an append-only event chain in one SQLite file. Every request's state
(pending, approved, edited, rejected, responded, expired, cancelled; consumed; resume
claimed) is a projection of that chain, rebuilt at open time and re-synchronized inside
every write transaction, so the audit trail and the queue cannot disagree.

* Each event row carries ``hash = sha256(prev_hash || canonical_json([seq, ts,
  request_id, kind, body]))``; a sidecar ``<db>.head`` is written *before* each COMMIT
  (under the write lock) and anchors the newest event, so appended forgeries and deleted
  rows are detected. Every write re-verifies the whole chain and head first. Any mismatch
  marks the store unhealthy: every write and read then raises
  :class:`ApprovalStoreError` and callers fail closed.
* Arguments are stored only as their hash plus a masked display copy, and free-text
  reasons are masked too (#185). The one plaintext exception is the replacement input of
  an EDIT (``approval_payloads``), bound by its hash in the chain and securely deleted
  (``secure_delete`` + WAL truncate) once consumed or terminal.
* Only a human principal (``local:<name>``) can approve or edit. ``system`` may expire,
  cancel or reject. There is no switch, argument or environment variable that changes
  this.
* A clock that is not finite, or that runs behind the newest event by more than
  :data:`CLOCK_SKEW_TOLERANCE_SEC`, refuses the write.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import math
import os
import re
import secrets
import sqlite3
import tempfile
import threading
import time
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from stiracore.durable_dir import fsync_dir

from .approval import args_hash, canonical_json
from .hitl_reasons import BREAKER_STRICTER

__all__ = [
    "BREAKER_REFUSAL",
    "CLOCK_SKEW_TOLERANCE_SEC",
    "MAX_CHAIN_LINKS",
    "ApprovalRecord",
    "ApprovalStore",
    "ApprovalStoreError",
    "StoreOutcome",
    "breaker_rank",
    "is_human_principal",
    "normalize_origin",
    "request_rank",
    "stricter_than_requested",
]

logger = logging.getLogger(__name__)

GENESIS_HASH = "0" * 64
RECOVERY_REASON = (
    "an interrupted write (or deleted rows) was recovered; a fresh human decision is required"
)
# The resume token of the execution context running a claimed resume (#263 review): the
# SDK copies contextvars into its worker thread / tasks, an unrelated thread has none.
_ACTIVE_RESUME: ContextVar[str | None] = ContextVar("stira_hitl_active_resume", default=None)
CLOCK_SKEW_TOLERANCE_SEC = 2.0
DISPLAY_LIMIT = 2000

PENDING = "pending"
APPROVED = "approved"
EDITED = "edited"
REJECTED = "rejected"
RESPONDED = "responded"
EXPIRED = "expired"
CANCELLED = "cancelled"
TERMINAL = frozenset({REJECTED, RESPONDED, EXPIRED, CANCELLED})
DECISION_STATUS = {"approve": APPROVED, "edit": EDITED, "reject": REJECTED, "respond": RESPONDED}
EVENT_KINDS = frozenset(
    {
        "requested",
        "decided",
        "expired",
        "cancelled",
        "consumed",
        "resume_claimed",
        "resume_refused",
        "binding_mismatch",
    }
)
# Remote approvers need a verified signature first (ADR 0006 §4); not accepted yet.
HUMAN_PRINCIPAL_KINDS = frozenset({"local"})
# ADR 0024 §7: every ancestor walk over EDIT links stops here (a longer chain refuses).
MAX_CHAIN_LINKS = 16
# ADR 0024 §3: breaker levels ranked for the freeze (``warning`` = ``coherence``).
BREAK_RANKS = {"normal": 0, "warning": 1, "coherence": 1, "critical": 2}
# Reason code of a resume refused because the breaker got stricter (``resume_refused``).
BREAKER_REFUSAL = "breaker"
_ORIGIN_ID = re.compile(r"[A-Za-z0-9._:-]{1,128}")
_INCARNATION = re.compile(r"[0-9a-f]{32}")


class ApprovalStoreError(RuntimeError):
    """The store cannot be trusted or written right now; callers must fail closed."""


class _ApprovalStoreBusy(ApprovalStoreError):
    """SQLite busy/locked: transient, the instance stays usable (#601 B2)."""


class StoreOutcome(Enum):
    ACCEPTED = "accepted"
    NOT_FOUND = "not_found"
    ALREADY_DECIDED = "already_decided"
    BINDING_MISMATCH = "binding_mismatch"
    EXPIRED = "expired"
    REFUSED = "refused"

    def __bool__(self) -> bool:
        return self is StoreOutcome.ACCEPTED


_TRANSIENT_CODES = frozenset({5, 6})  # SQLITE_BUSY, SQLITE_LOCKED (primary result codes)
_AUDIT_FIELD = re.compile(r"[A-Za-z0-9._:@-]{1,64}")


def _is_transient(error: sqlite3.Error) -> bool:
    code = getattr(error, "sqlite_errorcode", None)
    if isinstance(code, int):
        return code & 0xFF in _TRANSIENT_CODES
    message = str(error).lower()
    return "database is locked" in message or "database table is locked" in message


def is_human_principal(principal: Any) -> bool:
    """``local:<name>`` only. ``system``, empty, or unknown kinds are not humans."""
    if not isinstance(principal, str):
        return False
    kind, sep, name = principal.partition(":")
    return bool(sep) and kind in HUMAN_PRINCIPAL_KINDS and bool(name.strip())


def _level_name(level: Any) -> Any:
    return getattr(level, "value", level)


def breaker_rank(level: Any) -> int:
    """Rank of the *current* breaker level (ADR 0024 §3). Unknown counts as critical."""
    return BREAK_RANKS.get(_level_name(level), BREAK_RANKS["critical"])


def request_rank(level: Any) -> int:
    """Rank of the level a request was raised at. A request that does not say (a row
    from before ADR 0024, or a caller that left it out) counts as raised at ``normal``:
    it is frozen at every stricter level (the strict reading; see ``docs/hitl-approvals.md``)."""
    return BREAK_RANKS.get(_level_name(level), BREAK_RANKS["normal"])


def stricter_than_requested(current: Any, requested: Any) -> bool:
    """Whether ``current`` ranks above the level the request was raised at (freeze)."""
    return breaker_rank(current) > request_rank(requested)


def normalize_origin(origin: Any) -> dict[str, Any]:
    """The closed ``origin`` forms of ADR 0024 §2; anything else raises ``ValueError``.

    ``{}`` (no turn id, accepted as before), ``{"kind": "turn", "id": <id>}`` or
    ``{"kind": "job", "job_id", "run_id", "generation", "incarnation"}``. Unknown keys
    are refused. Ids are short identifiers; ``incarnation`` is 128-bit lowercase hex.
    """
    if origin is None:
        return {}
    if not isinstance(origin, dict):
        raise ValueError("origin must be an object")  # noqa: TRY004 - one error type
    if not origin:
        return {}
    kind = origin.get("kind")
    if kind == "turn":
        if set(origin) != {"kind", "id"}:
            raise ValueError("a turn origin has exactly kind and id")
        if not isinstance(origin["id"], str) or not _ORIGIN_ID.fullmatch(origin["id"]):
            raise ValueError("a turn origin needs a short turn id")
        return {"kind": "turn", "id": origin["id"]}
    if kind == "job":
        keys = {"kind", "job_id", "run_id", "generation", "incarnation"}
        if set(origin) != keys:
            raise ValueError(
                "a job origin has exactly kind, job_id, run_id, generation, incarnation"
            )
        for key in ("job_id", "run_id"):
            if not isinstance(origin[key], str) or not _ORIGIN_ID.fullmatch(origin[key]):
                raise ValueError(f"a job origin needs a short {key}")
        generation = origin["generation"]
        if type(generation) is not int or generation < 0:
            raise ValueError("a job origin needs a non-negative integer generation")
        incarnation = origin["incarnation"]
        if not isinstance(incarnation, str) or not _INCARNATION.fullmatch(incarnation):
            raise ValueError("a job origin needs a 128-bit hex incarnation")
        return {key: origin[key] for key in sorted(keys)}
    raise ValueError("origin kind must be turn or job")


def _default_redact(value: Any) -> Any:
    from stiracore.observability.masking import mask_secrets

    return mask_secrets(value)


def chain_hash(prev_hash: str, seq: int, ts: float, request_id: str, kind: str, body: Any) -> str:
    material = prev_hash + "\n" + canonical_json([seq, ts, request_id, kind, body])
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


@dataclass
class ApprovalRecord:
    request_id: str
    agent_id: str
    tool_name: str
    args_hash: str
    tool_use_id: str | None
    expires_at: float
    created_at: float
    correlation_id: str | None = None
    origin: dict[str, Any] = field(default_factory=dict)
    checkpoint_id: str | None = None
    supersedes: str | None = None
    display: str = ""
    masked: bool = False
    has_payload: bool = False
    reason: str | None = None
    break_level: str | None = None
    status: str = PENDING
    action: str | None = None
    decided_at: float | None = None
    approver: str | None = None
    client_id: str | None = None
    resolution_reason: str | None = None
    superseded_by: str | None = None
    consumed: bool = False
    resume_claimed: bool = False
    resume_refused: str | None = None
    # Not resumable after a restart (secret arguments are never persisted).
    live_only: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_REQUEST_FIELDS = (
    "agent_id",
    "tool_name",
    "args_hash",
    "tool_use_id",
    "expires_at",
    "created_at",
    "correlation_id",
    "origin",
    "checkpoint_id",
    "supersedes",
    "display",
    "masked",
    "has_payload",
    "reason",
    "break_level",
)


class ApprovalStore:
    """Durable approval queue + tamper-evident decision history (one writer at a time).

    ``path=None`` keeps everything in memory (tests); the semantics are the same, minus
    survival across processes.
    """

    def __init__(
        self,
        path: str | Path | None,
        *,
        now_fn: Callable[[], float] | None = None,
        redact: Callable[[Any], Any] | None = None,
        clock_skew_tolerance: float = CLOCK_SKEW_TOLERANCE_SEC,
    ) -> None:
        self.path = None if path is None else Path(path)
        self.head_path = (
            None if self.path is None else self.path.with_name(self.path.name + ".head")
        )
        self._now = now_fn or time.time
        self._redact = redact or _default_redact
        self._tolerance = float(clock_skew_tolerance)
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None
        self._error: str | None = None
        self._records: dict[str, ApprovalRecord] = {}
        self._order: list[str] = []
        self._seq = 0
        self._head = GENESIS_HASH
        self._last_ts: float | None = None
        self._purged = False
        # In-process resume contexts: request id -> expected correlation id (#263 review).
        self._resuming: dict[str, tuple[str, str | None]] = {}
        try:
            self._open()
        except ApprovalStoreError as error:
            if self._error is None:
                self._fail(str(error))
        except (sqlite3.Error, OSError, ValueError, TypeError, KeyError) as error:
            self._fail(f"approval store unreadable: {type(error).__name__}: {error}")

    # --- opening, verification and projection -------------------------------------

    def _open(self) -> None:
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if self.path.is_symlink():
                raise ValueError("approval store must not be a symlink")
        conn = sqlite3.connect(
            ":memory:" if self.path is None else str(self.path),
            check_same_thread=False,
            isolation_level=None,
        )
        self._conn = conn
        # Deleted payload rows are overwritten, not just unlinked from the b-tree (#185).
        conn.execute("PRAGMA secure_delete=ON")
        if self.path is not None:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=FULL")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS approval_events ("
            " seq INTEGER PRIMARY KEY, ts REAL NOT NULL, request_id TEXT NOT NULL,"
            " kind TEXT NOT NULL, body TEXT NOT NULL, prev_hash TEXT NOT NULL,"
            " hash TEXT NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS approval_payloads ("
            " request_id TEXT PRIMARY KEY, input TEXT NOT NULL)"
        )
        if self.path is not None:
            for candidate in (self.path, Path(f"{self.path}-wal"), Path(f"{self.path}-shm")):
                with contextlib.suppress(OSError):
                    candidate.chmod(0o600)
        self._verified(lambda: None)

    def _fail(self, message: str) -> None:
        self._error = message

    @property
    def healthy(self) -> bool:
        return self._error is None

    @property
    def error(self) -> str | None:
        return self._error

    def status(self) -> dict[str, Any]:
        return {"ok": self.healthy, "error": self._error, "events": self._seq}

    def _require_healthy(self) -> sqlite3.Connection:
        if self._error is not None or self._conn is None:
            raise ApprovalStoreError(self._error or "approval store is closed")
        return self._conn

    def _rows_after(self, seq: int) -> list[tuple]:
        conn = self._require_healthy()
        return conn.execute(
            "SELECT seq, ts, request_id, kind, body, prev_hash, hash FROM approval_events"
            " WHERE seq > ? ORDER BY seq",
            (seq,),
        ).fetchall()

    def _sync_locked(self) -> None:
        """Verify and apply events written since the last sync (this or another process)."""
        for row in self._rows_after(self._seq):
            self._verify_and_apply(row)

    def _verify_and_apply(self, row: tuple) -> None:
        seq, ts, request_id, kind, body_text, prev_hash, digest = row
        if seq != self._seq + 1:
            self._broken(f"approval chain has a gap at seq {self._seq + 1}")
        if prev_hash != self._head:
            self._broken(f"approval chain is broken at seq {seq}")
        if not isinstance(ts, (int, float)) or not math.isfinite(ts):
            self._broken(f"approval event {seq} has an invalid timestamp")
        try:
            body = json.loads(body_text)
        except (TypeError, ValueError):
            self._broken(f"approval event {seq} is not valid JSON")
        if chain_hash(prev_hash, seq, ts, request_id, kind, body) != digest:
            self._broken(f"approval event {seq} was modified")
        try:
            self._apply(request_id, kind, body, ts)
        except (KeyError, TypeError, ValueError) as error:
            self._broken(f"approval event {seq} is inconsistent: {error}")
        self._seq, self._head = seq, digest
        self._last_ts = ts if self._last_ts is None else max(self._last_ts, ts)

    def _broken(self, message: str):
        self._fail(message)
        raise ApprovalStoreError(message)

    def _apply(self, request_id: str, kind: str, body: dict, ts: float) -> None:
        if kind not in EVENT_KINDS or not isinstance(body, dict):
            raise ValueError(f"unknown event {kind!r}")
        if kind == "requested":
            if request_id in self._records:
                raise ValueError("duplicate request id")
            record = ApprovalRecord(
                request_id=request_id,
                live_only=bool(body.get("live_only", False)),
                **{k: body[k] for k in _REQUEST_FIELDS},
            )
            self._records[request_id] = record
            self._order.append(request_id)
            previous = self._records.get(record.supersedes or "")
            if previous is not None:
                previous.superseded_by = request_id
            return
        record = self._records[request_id]
        if kind == "decided":
            record.status = DECISION_STATUS[body["action"]]
            record.action = body["action"]
            record.decided_at = ts
            record.approver = body.get("approver")
            record.client_id = body.get("client_id")
            record.resolution_reason = body.get("reason")
        elif kind in ("expired", "cancelled"):
            record.status = EXPIRED if kind == "expired" else CANCELLED
            record.decided_at = record.decided_at if record.action else ts
            record.approver = record.approver or body.get("by", "system")
            record.resolution_reason = body.get("reason") or record.resolution_reason
        elif kind == "consumed":
            record.consumed = True
        elif kind == "resume_claimed":
            record.resume_claimed = True
        elif kind == "resume_refused":
            record.resume_refused = body.get("reason") or "refused"
        if kind != "binding_mismatch" and (
            record.status in TERMINAL or record.status == EDITED or record.consumed
        ):
            # Superseded edits keep no payload either: the successor holds the input.
            record.has_payload = False

    def _read_head(self) -> dict | None:
        if self.head_path is None or not self.head_path.exists():
            return None
        head = json.loads(self.head_path.read_text(encoding="utf-8"))
        if (
            not isinstance(head, dict)
            or set(head) != {"seq", "hash", "base_seq", "base_hash"}
            or any(type(head[k]) is not int or head[k] < 0 for k in ("seq", "base_seq"))
            or any(
                not isinstance(head[k], str) or len(head[k]) != 64 for k in ("hash", "base_hash")
            )
        ):
            raise ValueError("approval head anchor is malformed")
        return head

    def _check_head_locked(self) -> None:
        """Compare the anchor with the verified chain (caller holds the DB write lock).

        The head is written *before* each COMMIT and names the chain tail it was
        built on (``base``). So:

        * head == DB tail: consistent;
        * head ahead, built on the DB tail: either a crash between the head write and
          COMMIT, or rows deleted from the end. The two cannot be told apart, so the
          state is not trusted as is: every unfinished request (pending, or approved /
          edited and not consumed) is cancelled in new events and needs a fresh human
          decision. Deleting any tail row can therefore never bring an approval back;
        * anything else (rows the head never covered, a missing/older head, a head
          on another base): tampering or loss -> fail closed.
        """
        if self.head_path is None:
            return
        head = self._read_head()
        if head is None:
            if self._seq:
                self._broken("approval head anchor is missing")
            return
        if head["seq"] == self._seq and head["hash"] == self._head:
            return
        if (
            head["seq"] > self._seq
            and head["base_seq"] == self._seq
            and head["base_hash"] == self._head
        ):
            self._recover_locked()
            return
        self._broken("approval head anchor does not match the chain (rows added or deleted?)")

    def _recover_locked(self) -> None:
        """Interrupted write (or deleted tail): revoke everything unfinished (ADR 0006 §1)."""
        base = (self._seq, self._head)
        try:
            now = self._checked_now()
            for request_id in list(self._order):
                record = self._records[request_id]
                unfinished = record.status == PENDING or (
                    record.status in (APPROVED, EDITED) and not record.consumed
                )
                if unfinished:
                    body = {"by": "system", "reason": RECOVERY_REASON}
                    self._append(now, request_id, "cancelled", body)
            self._write_head_locked(self._seq, self._head, base)
        except (ApprovalStoreError, sqlite3.Error, OSError, ValueError) as error:
            self._broken(f"approval store recovery could not be recorded: {error}")

    def _write_head_locked(
        self, seq: int, digest: str, base: tuple[int, str] | None = None
    ) -> None:
        if self.head_path is None:
            return
        base_seq, base_hash = base if base is not None else (seq, digest)
        text = json.dumps(
            {"seq": seq, "hash": digest, "base_seq": base_seq, "base_hash": base_hash}
        )
        fd, tmp = tempfile.mkstemp(dir=self.head_path.parent, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.head_path)
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)
        # Make the rename itself durable (power loss right after the replace).
        # POSIX only: Windows cannot open a directory (#543).
        fsync_dir(self.head_path.parent)

    def _verify_locked(self) -> None:
        """Re-read + re-hash the whole chain and check the head; failures are sticky."""
        try:
            self._reload_locked()
            self._check_head_locked()
        except ApprovalStoreError:
            raise
        except sqlite3.Error as error:
            raise self._sqlite_failure(error) from error
        except (OSError, ValueError, TypeError, KeyError) as error:
            self._broken(f"approval store unreadable: {type(error).__name__}: {error}")

    def _sqlite_failure(self, error: sqlite3.Error) -> ApprovalStoreError:
        """Normalize a SQLite error: busy/locked is transient, anything else is sticky.

        A dropped table, a schema change or I/O corruption is a verdict on the data
        (#263 review): the instance stays unhealthy even if the table comes back.
        """
        if _is_transient(error):
            return _ApprovalStoreBusy(f"approval store busy: {error}")
        if self._error is None:
            self._fail(f"approval store database error: {type(error).__name__}: {error}")
        return ApprovalStoreError(self._error)

    def quarantine(self, reason: str) -> None:
        """Stop this instance for good (e.g. a claimed approval could not be revoked)."""
        if self._error is None:
            self._fail(f"approval store quarantined: {reason}")

    def _reload_locked(self) -> None:
        self._records, self._order = {}, []
        self._seq, self._head, self._last_ts = 0, GENESIS_HASH, None
        self._sync_locked()

    # --- writes -------------------------------------------------------------------

    def would_mask(self, value: Any) -> bool:
        """Whether storing ``value`` would hide something (it contains a secret)."""
        return self._redact(value) != value

    def _audit_field(self, value: Any) -> str | None:
        """Short identifiers only (``[A-Za-z0-9._:@-]``, <= 64), never secret-looking."""
        if value is None:
            return None
        if (
            not isinstance(value, str)
            or not _AUDIT_FIELD.fullmatch(value)
            or self._redact(value) != value
        ):
            return "unknown"
        return value

    def _audit_principal(self, principal: Any) -> str | None:
        """Keep the principal kind (``local``/``system``); sanitize the name."""
        if not isinstance(principal, str):
            return self._audit_field(principal)
        kind, sep, name = principal.partition(":")
        if sep and kind in ("local", "remote", "system"):
            return f"{kind}:{self._audit_field(name) or 'unknown'}"
        return self._audit_field(principal)

    def _mask_text(self, text: Any) -> Any:
        """Free text (reasons) is stored masked, like the argument display (#185)."""
        return self._redact(text) if isinstance(text, str) else text

    def _checked_now(self) -> float:
        now = self._now()
        if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(now):
            raise ApprovalStoreError("clock is not usable; refusing to record approvals")
        if self._last_ts is not None and now < self._last_ts - self._tolerance:
            raise ApprovalStoreError("clock went backwards; refusing to record approvals")
        return float(now)

    def _verified(self, operation: Callable[[], Any]) -> Any:
        """Run ``operation`` on a freshly verified chain + head, under the DB write lock.

        The whole chain is re-read and re-hashed every time (the store is small), so
        a row changed or deleted by another connection after it was first read, or a
        removed head, is caught before anything is recorded or handed out.
        """
        with self._lock:
            conn = self._require_healthy()
            try:
                conn.execute("BEGIN IMMEDIATE")
            except sqlite3.Error as error:
                raise self._sqlite_failure(error) from error
            try:
                self._verify_locked()
                result = operation()
                conn.execute("COMMIT")
            except BaseException as error:
                with contextlib.suppress(sqlite3.Error):
                    conn.execute("ROLLBACK")
                if isinstance(error, sqlite3.Error):
                    raise self._sqlite_failure(error) from error
                raise
            return result

    def _write(self, operation: Callable[[float], Any]) -> Any:
        with self._lock:
            conn = self._require_healthy()
            try:
                conn.execute("BEGIN IMMEDIATE")
            except sqlite3.Error as error:
                raise self._sqlite_failure(error) from error
            self._purged = False
            try:
                self._reload_locked()
                # The committed tail, before any recovery events of this transaction.
                base = (self._seq, self._head)
                self._verify_locked()
                result = operation(self._checked_now())
                if (self._seq, self._head) != base:
                    # Anchor first: a crash before COMMIT leaves the head *ahead* of
                    # the DB (recoverable, nothing approved); rows the head never
                    # covered can then only be forgeries (#263 review).
                    self._write_head_locked(self._seq, self._head, base)
                conn.execute("COMMIT")
            except BaseException as error:
                with contextlib.suppress(sqlite3.Error):
                    conn.execute("ROLLBACK")
                failure = self._sqlite_failure(error) if isinstance(error, sqlite3.Error) else None
                if self._error is None:
                    with contextlib.suppress(ApprovalStoreError, sqlite3.Error):
                        self._reload_locked()
                if failure is not None:
                    raise failure from error
                raise
            if self._purged and self.path is not None:
                # Move the overwritten pages out of the WAL and empty it (#185).
                try:
                    busy = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]
                except sqlite3.Error:
                    busy = 1
                if busy:
                    logger.warning(
                        "approval store: WAL checkpoint was blocked; purged payload pages may "
                        "remain in the WAL until the next checkpoint"
                    )
            return result

    def _append(self, now: float, request_id: str, kind: str, body: dict) -> None:
        seq = self._seq + 1
        ts = max(now, self._last_ts or now)  # never record a decreasing timestamp
        digest = chain_hash(self._head, seq, ts, request_id, kind, body)
        self._require_healthy().execute(
            "INSERT INTO approval_events VALUES (?, ?, ?, ?, ?, ?, ?)",
            (seq, ts, request_id, kind, canonical_json(body), self._head, digest),
        )
        self._apply(request_id, kind, body, ts)
        self._seq, self._head, self._last_ts = seq, digest, ts
        record = self._records.get(request_id)
        if record is not None and not record.has_payload:
            cursor = self._conn.execute(
                "DELETE FROM approval_payloads WHERE request_id = ?", (request_id,)
            )
            self._purged = self._purged or cursor.rowcount > 0

    def submit(
        self,
        *,
        request_id: str,
        agent_id: str,
        tool_name: str,
        tool_input: Any,
        expires_at: float,
        tool_use_id: str | None = None,
        correlation_id: str | None = None,
        origin: dict[str, Any] | None = None,
        checkpoint_id: str | None = None,
        supersedes: str | None = None,
        reason: str | None = None,
        break_level: str | None = None,
        store_payload: bool = False,
        live_only: bool = False,
        display: str | None = None,
    ) -> ApprovalRecord:
        """Persist a new pending request. Raises :class:`ApprovalStoreError` on failure.

        ``store_payload`` keeps the arguments for a later resume (EDIT replacements
        only) and is refused when they contain anything ``mask_secrets`` would hide:
        such a request must be handled live (``live_only``) and does not survive a
        restart.
        """
        digest = args_hash(tool_input)  # raises for unnormalizable input
        display_value = self._redact(tool_input)
        # ADR 0011 W5: a caller-chosen display (codes only) replaces the arguments in the
        # record — a grant iteration's card keeps its arguments in memory only.
        override = display
        display = canonical_json(display_value) if override is None else str(override)
        if store_payload and not isinstance(tool_input, dict):
            # payload() only hands back a dict; refuse here instead of quarantining the
            # whole store when it is read (#601 B3).
            raise ApprovalStoreError("a stored replacement input must be an object")
        if store_payload and override is not None:
            raise ApprovalStoreError("a stored replacement input keeps its own display")
        if store_payload and display_value != tool_input:
            raise ApprovalStoreError(
                "arguments containing secrets are not persisted; approve them live"
            )
        if not isinstance(expires_at, (int, float)) or not math.isfinite(expires_at):
            raise ApprovalStoreError("approval expiry must be finite")
        if not isinstance(request_id, str) or not request_id:
            raise ApprovalStoreError("request id is required")
        try:
            origin_form = normalize_origin(origin)
        except ValueError as error:
            raise ApprovalStoreError(f"approval origin refused: {error}") from None
        if origin_form.get("kind") == "job" and correlation_id != origin_form["run_id"]:
            raise ApprovalStoreError("a job request is correlated by its run id")
        break_level = _level_name(break_level)
        if break_level is not None and break_level not in BREAK_RANKS:
            raise ApprovalStoreError("unknown breaker level")

        def operation(now: float) -> ApprovalRecord:
            if request_id in self._records:
                raise ApprovalStoreError("duplicate approval request id")
            body = {
                "agent_id": agent_id or "",
                "tool_name": tool_name,
                "args_hash": digest,
                "tool_use_id": tool_use_id or None,
                "expires_at": float(expires_at),
                "created_at": now,
                "correlation_id": correlation_id,
                "origin": origin_form,
                "checkpoint_id": checkpoint_id,
                "supersedes": supersedes,
                "display": display[:DISPLAY_LIMIT],
                "masked": override is None and display_value != tool_input,
                "has_payload": bool(store_payload),
                "reason": self._mask_text(reason),
                "break_level": break_level,
                "live_only": bool(live_only),
            }
            if store_payload:
                self._conn.execute(
                    "INSERT INTO approval_payloads VALUES (?, ?)",
                    (request_id, canonical_json(tool_input)),
                )
            self._append(now, request_id, "requested", body)
            return self._copy(self._records[request_id])

        return self._write(operation)

    def decide(
        self,
        request_id: str,
        action: str,
        *,
        args_hash: str | None = None,
        approver: str,
        client_id: str | None = None,
        reason: str | None = None,
    ) -> StoreOutcome:
        """Record one decision, exactly once, only for the displayed arguments."""
        if action not in DECISION_STATUS:
            raise ValueError(f"unknown decision {action!r}")
        if action in ("approve", "edit") and not is_human_principal(approver):
            return StoreOutcome.REFUSED

        def operation(now: float) -> StoreOutcome:
            record = self._records.get(request_id)
            if record is None:
                return StoreOutcome.NOT_FOUND
            if record.status != PENDING:
                return (
                    StoreOutcome.EXPIRED
                    if record.status == EXPIRED
                    else (StoreOutcome.ALREADY_DECIDED)
                )
            if now > record.expires_at:
                self._append(now, request_id, "expired", {"by": "system", "reason": "expired"})
                return StoreOutcome.EXPIRED
            if action != "reject" and args_hash != record.args_hash:
                return StoreOutcome.BINDING_MISMATCH
            body = {
                "action": action,
                "approver": self._audit_principal(approver),
                "client_id": self._audit_field(client_id),
                "reason": self._mask_text(reason),
                "args_hash": record.args_hash,
            }
            self._append(now, request_id, "decided", body)
            return StoreOutcome.ACCEPTED

        return self._write(operation)

    def expire_due(self) -> list[str]:
        """Expire every pending request past its deadline (rejection, never execution)."""
        with self._lock:
            self._require_healthy()
            now = self._now()
            due = [
                r.request_id
                for r in self._records.values()
                if r.status == PENDING and isinstance(now, (int, float)) and now > r.expires_at
            ]
        if not due:
            return []

        def operation(now: float) -> list[str]:
            expired = []
            for request_id in due:
                record = self._records[request_id]
                if record.status == PENDING and now > record.expires_at:
                    self._append(now, request_id, "expired", {"by": "system", "reason": "expired"})
                    expired.append(request_id)
            return expired

        return self._write(operation)

    def expire(self, request_id: str) -> bool:
        def operation(now: float) -> bool:
            record = self._records.get(request_id)
            if record is None or record.status != PENDING:
                return False
            self._append(now, request_id, "expired", {"by": "system", "reason": "expired"})
            return True

        return self._write(operation)

    def cancel(self, request_id: str, *, by: str = "system", reason: str | None = None) -> bool:
        """Cancel a pending request, or revoke an approval that was never used."""

        def operation(now: float) -> bool:
            record = self._records.get(request_id)
            if record is None:
                return False
            revocable = record.status in (APPROVED, EDITED) and not record.consumed
            if record.status != PENDING and not revocable:
                return False
            body = {"by": self._audit_principal(by), "reason": self._mask_text(reason)}
            self._append(now, request_id, "cancelled", body)
            return True

        return self._write(operation)

    def consume(
        self, request_id: str, *, by_request_id: str | None = None, resumed: bool = False
    ) -> bool:
        """Hand an approval to the gate once. False if used, expired or not approved.

        ``resumed=True`` (a restored approval answering a replayed call) also requires
        this request's claimed resume to be running in-process, with the same
        correlation id as the original request.
        """

        def operation(now: float) -> bool:
            record = self._records.get(request_id)
            if record is None or record.status != APPROVED or record.consumed:
                return False
            lent = self._lent_here(request_id) if resumed else None
            if resumed and (
                lent is None or not record.resume_claimed or record.correlation_id != lent[1]
            ):
                return False
            if now > record.expires_at:
                self._append(now, request_id, "expired", {"by": "system", "reason": "expired"})
                return False
            self._append(now, request_id, "consumed", {"by_request_id": by_request_id})
            return True

        return self._write(operation)

    def claim_resume(
        self, request_id: str, *, breaker_level: Callable[[], Any] | None = None
    ) -> bool:
        """Claim the single resume of an approved request.

        ``breaker_level`` (ADR 0024 §3) reads the current breaker level under the store's
        write lock. When it ranks above the level the request was raised at (or cannot be
        read), the resume is refused (``resume_refused`` with reason ``breaker``, the two
        levels only) and the approval is revoked; False is returned. The approver of this
        request decided under a calmer breaker than the one the replay would run under.
        """

        def operation(now: float) -> bool:
            record = self._records.get(request_id)
            if record is None or record.status != APPROVED or record.resume_claimed:
                return False
            if record.consumed or now > record.expires_at:
                return False
            if breaker_level is not None:
                try:
                    current = _level_name(breaker_level())
                except Exception:  # noqa: BLE001 - an unreadable breaker counts as critical
                    current = None
                if stricter_than_requested(current, record.break_level):
                    body = {
                        "reason": BREAKER_REFUSAL,
                        "level": current if current in BREAK_RANKS else "unknown",
                        "requested_level": record.break_level
                        if record.break_level in BREAK_RANKS
                        else "unknown",
                    }
                    self._append(now, request_id, "resume_refused", body)
                    self._append(
                        now, request_id, "cancelled", {"by": "system", "reason": BREAKER_STRICTER}
                    )
                    return False
            self._append(now, request_id, "resume_claimed", {})
            return True

        return self._write(operation)

    def refuse_resume(self, request_id: str, reason: str) -> None:
        """Record why a resume did not run, and revoke the unused approval."""

        def operation(now: float) -> None:
            record = self._records.get(request_id)
            if record is None:
                return
            reason_text = self._mask_text(reason)
            self._append(now, request_id, "resume_refused", {"reason": reason_text})
            if record.status in (PENDING, APPROVED, EDITED) and not record.consumed:
                body = {"by": "system", "reason": reason_text}
                self._append(now, request_id, "cancelled", body)

        self._write(operation)

    def note_binding_mismatch(self, request_id: str, observed_hash: str | None) -> None:
        def operation(now: float) -> None:
            if request_id in self._records:
                self._append(now, request_id, "binding_mismatch", {"observed": observed_hash})

        self._write(operation)

    # --- reads --------------------------------------------------------------------

    @staticmethod
    def _copy(record: ApprovalRecord) -> ApprovalRecord:
        return ApprovalRecord(**record.to_dict())

    def _synced(self) -> None:
        """Reads re-verify the whole chain and head too (the store is small)."""
        self._verified(lambda: None)

    def get(self, request_id: str) -> ApprovalRecord | None:
        with self._lock:
            self._synced()
            record = self._records.get(request_id)
            return None if record is None else self._copy(record)

    def get_many(self, request_ids) -> dict[str, ApprovalRecord]:
        """Several records after one verification (unknown ids are left out, #287)."""
        with self._lock:
            self._synced()
            return {
                request_id: self._copy(self._records[request_id])
                for request_id in dict.fromkeys(request_ids)
                if request_id in self._records
            }

    def pending(self) -> list[ApprovalRecord]:
        with self._lock:
            self._synced()
            return [
                self._copy(self._records[i])
                for i in self._order
                if self._records[i].status == PENDING
            ]

    def open_records(self) -> list[ApprovalRecord]:
        """Every request that could still let a call run: pending, or approved/edited and
        not used. Unbounded (no page limit), oldest first (#190: revoke them all)."""
        with self._lock:
            self._synced()
            return [
                self._copy(self._records[i])
                for i in self._order
                if self._records[i].status == PENDING
                or (self._records[i].status in (APPROVED, EDITED) and not self._records[i].consumed)
            ]

    def history(self, limit: int = 200) -> list[ApprovalRecord]:
        """Decided/expired/cancelled requests, newest first."""
        with self._lock:
            self._synced()
            done = [self._records[i] for i in self._order if self._records[i].status != PENDING]
            done.sort(key=lambda r: r.decided_at or r.created_at, reverse=True)
            return [self._copy(r) for r in done[:limit]]

    def for_tool_use(self, agent_id: str, tool_name: str, tool_use_id: str) -> list[ApprovalRecord]:
        with self._lock:
            self._synced()
            return [
                self._copy(self._records[i])
                for i in self._order
                if self._records[i].tool_use_id == tool_use_id
                and self._records[i].tool_name == tool_name
                and self._records[i].agent_id == (agent_id or "")
            ]

    def chain_root(self, request_id: str) -> ApprovalRecord | None:
        """The original request an EDIT chain started from."""
        with self._lock:
            self._synced()
            record = self._records.get(request_id)
            seen = set()
            while record is not None and record.supersedes and record.request_id not in seen:
                if len(seen) >= MAX_CHAIN_LINKS:
                    return None  # ADR 0024 §7: a longer chain is not followed (refused)
                seen.add(record.request_id)
                record = self._records.get(record.supersedes)
            return None if record is None else self._copy(record)

    def payload(self, request_id: str) -> dict[str, Any] | None:
        """The EDIT replacement input, verified against its hash in a re-verified chain."""

        def read() -> dict[str, Any] | None:
            record = self._records.get(request_id)
            if record is None or not record.has_payload:
                return None
            row = self._conn.execute(
                "SELECT input FROM approval_payloads WHERE request_id = ?", (request_id,)
            ).fetchone()
            if row is None:
                self._broken(f"approval payload for {request_id} is missing")
            try:
                value = json.loads(row[0])
                digest = args_hash(value)
            except (TypeError, ValueError):
                self._broken(f"approval payload for {request_id} is unreadable")
            if digest != record.args_hash or not isinstance(value, dict):
                self._broken(f"approval payload for {request_id} was modified")
            return value

        return self._verified(read)

    def audit_events(self, limit: int = 200) -> list[dict[str, Any]]:
        """Newest-first events, read inside one re-verification of the chain and head
        (ADR 0024 §7): a row changed since the last check is never returned as history."""

        def read() -> list[tuple]:
            return (
                self._require_healthy()
                .execute(
                    "SELECT seq, ts, request_id, kind, body, hash FROM approval_events"
                    " ORDER BY seq DESC LIMIT ?",
                    (limit,),
                )
                .fetchall()
            )

        rows = self._verified(read)
        return [
            {"seq": s, "ts": t, "request_id": r, "kind": k, "body": json.loads(b), "hash": h}
            for s, t, r, k, b, h in rows
        ]

    def verify_chain(self) -> tuple[bool, str | None]:
        """Re-verify the whole chain and head anchor from disk."""
        if self._error is not None or self._conn is None:
            return False, self._error or "approval store is closed"
        try:
            self._verified(lambda: None)
        except _ApprovalStoreBusy as error:
            # Only a busy/locked database is transient: not verified now, the store is not
            # stopped for good (#601 B2). Anything else below stays sticky.
            return False, self._error or str(error)
        except (ApprovalStoreError, sqlite3.Error, OSError, ValueError) as error:
            if self._error is None:
                self._fail(str(error))
            return False, self._error
        return True, None

    @contextlib.contextmanager
    def resume_context(self, request_id: str):
        """Lend the restored approval of ``request_id`` to *this* execution context only.

        A random token is bound to the request (and its EDIT ancestors) and set in a
        context variable. :meth:`consume` hands out a restored approval only when the
        caller's context carries that token, the resume was claimed and the
        correlation id matches. The SDK's tool path copies context variables (its
        worker thread and asyncio tasks); any other thread does not have the token.
        """
        root = self.chain_root(request_id)
        expected = None if root is None else root.correlation_id
        chain = []
        record = self.get(request_id)
        while record is not None and record.request_id not in chain:
            if len(chain) > MAX_CHAIN_LINKS:
                raise ApprovalStoreError("the edit chain is too long to resume")
            chain.append(record.request_id)
            record = self.get(record.supersedes) if record.supersedes else None
        token = secrets.token_hex(16)
        with self._lock:
            for item in chain:
                self._resuming[item] = (token, expected)
        context_token = _ACTIVE_RESUME.set(token)
        try:
            yield
        finally:
            _ACTIVE_RESUME.reset(context_token)
            with self._lock:
                for item in chain:
                    if self._resuming.get(item, (None,))[0] == token:
                        self._resuming.pop(item, None)

    def _lent_here(self, request_id: str) -> tuple[str, str | None] | None:
        entry = self._resuming.get(request_id)
        if entry is None or _ACTIVE_RESUME.get() != entry[0]:
            return None
        return entry

    def resuming(self, request_id: str) -> bool:
        """Whether ``request_id`` is being resumed by the calling execution context."""
        with self._lock:
            return self._lent_here(request_id) is not None

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                with contextlib.suppress(sqlite3.Error):
                    self._conn.close()
            self._conn = None
