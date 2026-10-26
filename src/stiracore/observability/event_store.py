"""Thread-safe SQLite persistence for observability events.

Events are ordered by an internal, monotonically increasing sequence, not by
their public IDs or timestamps. Public event IDs may therefore be UUIDs and
can also serve as SSE reconnect cursors.

The shared observability contracts are imported from .events.

Tamper evidence (#171, ADR 0009): every row carries ``prev_hash``/``hash`` over its
exact stored values, and a sidecar ``<db>.head`` anchors the tail. ``verify_chain()``
re-hashes everything; a store found inconsistent refuses writes for the life of the
instance. ``message``, ``usage`` and ``attributes`` pass ``mask_secrets`` before they
are stored, so no plaintext secret is ever part of the hashed material. The chain is
evidence only: nothing derives a permission from it.
"""

from __future__ import annotations

import contextlib
import json
import logging
import sqlite3
import time
import types
from dataclasses import asdict, fields, is_dataclass
from datetime import UTC, date, datetime
from enum import Enum
from pathlib import Path
from threading import RLock
from typing import Any, Union, get_args, get_origin, get_type_hints
from uuid import uuid4

from .event_chain import (
    ChainState,
    EventChainError,
    genesis_hash,
    read_head,
    row_hash,
    validate_namespace,
    write_head,
)
from .hooks import EventSink, ObservabilityEvent
from .masking import mask_secrets

logger = logging.getLogger(__name__)

DEFAULT_NAMESPACE = "observability"
RECOVERED_EVENT_TYPE = "chain.recovered"
# Exact stored columns, in hash order. seq is included so rows cannot be reordered.
_HASHED_COLUMNS = (
    "seq",
    "event_id",
    "timestamp",
    "event_type",
    "turn_id",
    "duration_ms",
    "model_id",
    "tool_name",
    "tool_use_id",
    "status",
    "usage_json",
    "message",
    "attributes_json",
)
_TRANSIENT_CODES = frozenset({5, 6})  # SQLITE_BUSY, SQLITE_LOCKED
_REQUIRED_COLUMNS = frozenset(_HASHED_COLUMNS) | {"prev_hash", "hash"}
# Identifier-like text columns: never masked silently, a secret-looking value is refused
# (new events) or replaced (legacy rows) before anything is hashed (ADR 0009 §5).
_ID_COLUMNS = (
    "timestamp",
    "event_type",
    "turn_id",
    "model_id",
    "tool_name",
    "tool_use_id",
    "status",
)
_PURGE_ATTEMPTS = 3
SCHEMA_ERROR = "events table schema is not the chained schema"

_EVENT_FIELDS = (
    "event_id",
    "timestamp",
    "event_type",
    "turn_id",
    "duration_ms",
    "model_id",
    "tool_name",
    "tool_use_id",
    "status",
    "usage",
    "message",
    "attributes",
)

_START_TYPES = {"turn_start", "turn_started"}
_END_TYPES = {
    "turn_end",
    "turn_ended",
    "turn_complete",
    "turn_completed",
    "turn_failed",
    "turn_error",
    "turn_cancelled",
    "turn_canceled",
}


def _json_default(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    if isinstance(value, (set, frozenset)):
        return sorted(value, key=str)
    if callable(getattr(value, "model_dump", None)):
        return value.model_dump(mode="json")
    raise TypeError(f"{type(value).__name__} is not JSON serializable.")


def _json_dumps(value: Any) -> str:
    return json.dumps(
        value,
        default=_json_default,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    )


def event_to_dict(event: ObservabilityEvent) -> dict[str, Any]:
    """Return the event's persisted fields as JSON-compatible values."""
    payload = {name: getattr(event, name, None) for name in _EVENT_FIELDS}
    return json.loads(_json_dumps(payload))


def _type_hints(cls: type) -> dict[str, Any]:
    try:
        return get_type_hints(cls)
    except (NameError, TypeError):
        return getattr(cls, "__annotations__", {})


def _restore(value: Any, annotation: Any) -> Any:
    """Restore common typed event fields without requiring a schema library."""
    if value is None:
        return None

    origin = get_origin(annotation)
    if origin in (Union, types.UnionType):
        for candidate in get_args(annotation):
            if candidate is type(None):
                continue
            try:
                return _restore(value, candidate)
            except (TypeError, ValueError, KeyError):
                continue
        return value

    if annotation is datetime and isinstance(value, str):
        return datetime.fromisoformat(value)
    if annotation is date and isinstance(value, str):
        return date.fromisoformat(value)
    if annotation in (int, float) and isinstance(value, str):
        return annotation(value)
    if isinstance(annotation, type) and issubclass(annotation, Enum):
        return annotation(value)
    if isinstance(annotation, type) and is_dataclass(annotation) and isinstance(value, dict):
        hints = _type_hints(annotation)
        return annotation(
            **{
                field.name: _restore(value[field.name], hints.get(field.name))
                for field in fields(annotation)
                if field.init and field.name in value
            }
        )
    return value


def _validate_limit(limit: int) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("limit must be a positive integer.")


def _event_kind(value: str) -> str:
    return value.lower().replace(".", "_").replace("-", "_")


def _timestamp_seconds(value: str) -> float:
    try:
        return float(value)
    except (ValueError, TypeError):
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.timestamp()


def _duration_ms(start: str, end: str) -> float | None:
    try:
        return max(0.0, (_timestamp_seconds(end) - _timestamp_seconds(start)) * 1000)
    except (TypeError, ValueError, OverflowError):
        return None


def _is_transient(error: sqlite3.Error) -> bool:
    code = getattr(error, "sqlite_errorcode", None)
    if isinstance(code, int):
        return code & 0xFF in _TRANSIENT_CODES
    message = str(error).lower()
    return "database is locked" in message or "database table is locked" in message


def _mask_json_text(text: Any) -> Any:
    """Mask a stored JSON column (legacy rows); non-JSON text is masked as text."""
    if not isinstance(text, str):
        return text
    try:
        value = json.loads(text)
    except ValueError:
        return mask_secrets(text)
    masked = mask_secrets(value)
    return text if masked == value else _json_dumps(masked)


class SQLiteEventStore(EventSink):
    """Persist events with serialized connection access and WAL journaling.

    Duplicate event IDs are ignored, making delivery retries idempotent.
    get_events returns insertion order, oldest first. An unknown since_id
    starts from the oldest retained event, including after clear().

    The rows form a hash chain (ADR 0009). A legacy database is migrated in place on
    first open (its existing rows are hashed and the start point is recorded). If the
    chain or its head does not verify, the store stays readable but every write raises
    :class:`EventChainError` until an operator inspects the files and reopens.
    """

    def __init__(
        self,
        db_path: Path,
        *,
        namespace: str = DEFAULT_NAMESPACE,
        head_path: Path | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.namespace = validate_namespace(namespace)
        self.db_path = Path(db_path)
        in_memory = str(db_path) == ":memory:"
        if not in_memory:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.head_path: Path | None = (
            None
            if in_memory
            else Path(head_path)
            if head_path is not None
            else self.db_path.with_name(self.db_path.name + ".head")
        )
        self._genesis = genesis_hash(self.namespace)
        self._state: ChainState | None = None
        self._error: str | None = None
        self._meta: dict[str, str] = {}
        self._timeout = float(timeout)
        # Set when the head was one transaction ahead at open (ADR 0009 §4).
        self.recovery: dict[str, Any] | None = None

        self._lock = RLock()
        self._hints = _type_hints(ObservabilityEvent)
        self._connection = sqlite3.connect(
            str(db_path),
            timeout=self._timeout,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row

        self._open_chain()

    # --- hash chain (ADR 0009) ------------------------------------------------

    @property
    def healthy(self) -> bool:
        return self._error is None and self._state is not None

    @property
    def error(self) -> str | None:
        return self._error

    def _broken(self, message: str) -> EventChainError:
        if self._error is None:
            self._error = message
            logger.error("event store %s unavailable: %s", self.namespace, message)
        return EventChainError(self._error)

    def _sqlite_failure(self, error: sqlite3.Error) -> Exception:
        """Busy/locked is transient (raised as is); any other database error is sticky."""
        if _is_transient(error):
            return error
        return self._broken(f"database error ({type(error).__name__})")

    def _columns(self) -> set[str]:
        return {row[1] for row in self._connection.execute("PRAGMA table_info(events)")}

    def _check_schema_locked(self) -> None:
        if not _REQUIRED_COLUMNS <= self._columns():
            raise self._broken(SCHEMA_ERROR)

    def _read_meta(self) -> dict[str, str] | None:
        exists = self._connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'event_chain'"
        ).fetchone()
        if exists is None:
            return None
        rows = self._connection.execute("SELECT key, value FROM event_chain").fetchall()
        return {row[0]: row[1] for row in rows}

    def _anchor_from_meta(self, meta: dict[str, str]) -> tuple[int, str]:
        if meta.get("version") != "1":
            raise self._broken("chain metadata is missing or has an unknown version")
        if meta.get("namespace") != self.namespace:
            raise self._broken("chain metadata belongs to another namespace")
        try:
            state = ChainState.from_dict(
                {
                    "seq": int(meta["anchor_seq"]),
                    "hash": meta["anchor_hash"],
                    "anchor_seq": int(meta["anchor_seq"]),
                    "anchor_hash": meta["anchor_hash"],
                }
            )
        except (KeyError, TypeError, ValueError):
            raise self._broken("chain metadata is malformed") from None
        if state.anchor_seq == 0 and state.anchor_hash != self._genesis:
            raise self._broken("chain metadata does not start at this namespace's genesis")
        return state.anchor_seq, state.anchor_hash

    def _link(self, rows, prev_seq: int, prev_hash: str) -> tuple[int, str, int]:
        """Verify ``rows`` (seq order) as the continuation of ``(prev_seq, prev_hash)``."""
        count = 0
        try:
            for row in rows:
                prev_seq, prev_hash = self._link_row(row, prev_seq, prev_hash)
                count += 1
        except (IndexError, KeyError):
            raise self._broken(SCHEMA_ERROR) from None
        return prev_seq, prev_hash, count

    def _link_row(self, row, prev_seq: int, prev_hash: str) -> tuple[int, str]:
        seq = row["seq"]
        if type(seq) is not int or seq <= prev_seq:
            raise self._broken(f"chain broken after seq {prev_seq} (row order)")
        if row["prev_hash"] != prev_hash:
            raise self._broken(f"chain broken at seq {seq} (a row was removed or moved)")
        try:
            digest = row_hash(prev_hash, [row[name] for name in _HASHED_COLUMNS])
        except (TypeError, ValueError):
            raise self._broken(f"event {seq} is not canonical") from None
        if row["hash"] != digest:
            raise self._broken(f"event {seq} was modified")
        return seq, digest

    def _verify_all_locked(self) -> ChainState:
        meta = self._read_meta()
        if meta is None:
            raise self._broken("chain metadata is missing")
        anchor_seq, anchor_hash = self._anchor_from_meta(meta)
        self._check_schema_locked()
        rows = self._connection.execute("SELECT * FROM events ORDER BY seq ASC")
        seq, digest, _count = self._link(rows, anchor_seq, anchor_hash)
        self._meta = meta
        return ChainState(seq, digest, anchor_seq, anchor_hash)

    def _refresh_locked(self) -> ChainState:
        try:
            return self._refresh_rows_locked()
        except (IndexError, KeyError):
            raise self._broken(SCHEMA_ERROR) from None

    def _refresh_rows_locked(self) -> ChainState:
        """Cheap per-write check (caller holds the write lock).

        Rows appended by another instance are verified; the known tail row must still
        be there unchanged, and the head must name the database state. A same-size
        rewrite of an older row is caught by :meth:`verify_chain` and at the next open.
        """
        known = self._state
        meta = self._read_meta()
        if meta is None:
            raise self._broken("chain metadata is missing")
        anchor = self._anchor_from_meta(meta)
        # No PRAGMA per write: a dropped column surfaces in _link / the INSERT instead.
        if anchor != (known.anchor_seq, known.anchor_hash):
            state = self._verify_all_locked()  # another instance cleared the store
        else:
            if known.seq > known.anchor_seq:
                tail = self._connection.execute(
                    "SELECT * FROM events WHERE seq = ?", (known.seq,)
                ).fetchone()
                if tail is None:
                    raise self._broken(f"event {known.seq} was deleted")
                self._link([tail], known.seq - 1, tail["prev_hash"])
                if tail["hash"] != known.hash:
                    raise self._broken(f"event {known.seq} was modified")
            rows = self._connection.execute(
                "SELECT * FROM events WHERE seq > ? ORDER BY seq ASC", (known.seq,)
            )
            seq, digest, _count = self._link(rows, known.seq, known.hash)
            state = ChainState(seq, digest, known.anchor_seq, known.anchor_hash)
        self._check_head_locked(state)
        return state

    def _check_head_locked(self, state: ChainState) -> None:
        if self.head_path is None:
            return
        head = self._read_head_locked()
        if head is None:
            raise self._broken("head anchor is missing")
        if head[1] != state:
            raise self._broken("head anchor does not match the chain (rows added or deleted?)")

    def _read_head_locked(self):
        try:
            return read_head(self.head_path, self.namespace)
        except EventChainError as error:
            raise self._broken(str(error)) from None

    def _write_head_locked(self, prev: ChainState | None, nxt: ChainState) -> None:
        if self.head_path is not None:
            write_head(self.head_path, self.namespace, prev, nxt)

    def _insert_locked(self, values: tuple, prev: ChainState) -> ChainState:
        """Insert one row linked to ``prev``; hash its values exactly as stored."""
        cursor = self._connection.execute(
            """
            INSERT INTO events (
                event_id, timestamp, event_type, turn_id, duration_ms,
                model_id, tool_name, tool_use_id, status, usage_json,
                message, attributes_json, prev_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (*values, prev.hash),
        )
        seq = cursor.lastrowid
        # Read back so column affinity (e.g. REAL) is what gets hashed.
        row = self._connection.execute("SELECT * FROM events WHERE seq = ?", (seq,)).fetchone()
        if row is None or seq <= prev.seq:
            raise self._broken("inserted event could not be read back in order")
        try:
            digest = row_hash(prev.hash, [row[name] for name in _HASHED_COLUMNS])
        except (TypeError, ValueError) as error:
            raise ValueError(f"event is not canonical ({error})") from None
        self._connection.execute("UPDATE events SET hash = ? WHERE seq = ?", (digest, seq))
        return ChainState(seq, digest, prev.anchor_seq, prev.anchor_hash)

    def _mask_row_locked(self, row: sqlite3.Row) -> bool:
        """Redact a legacy row in place before it is hashed (ADR 0009 §5).

        Payload columns are masked; a secret-looking identifier is masked too, and a
        secret-looking ``event_id`` (UNIQUE) is replaced by ``redacted-<seq>`` (a numeric
        suffix is added on a clash). The replacement is deterministic, so a migration
        redone after a crash hashes exactly the same.
        """
        updates: dict[str, Any] = {}
        event_id = row["event_id"]
        if isinstance(event_id, str) and mask_secrets(event_id) != event_id:
            # Deterministic (a redone migration must reproduce the head exactly) and
            # derived from nothing secret: the row's seq, plus a counter on collision.
            candidate, suffix = f"redacted-{row['seq']}", 0
            while self._connection.execute(
                "SELECT 1 FROM events WHERE event_id = ?", (candidate,)
            ).fetchone():
                suffix += 1
                candidate = f"redacted-{row['seq']}-{suffix}"
            updates["event_id"] = candidate
        for name in _ID_COLUMNS:
            value = row[name]
            if isinstance(value, str) and mask_secrets(value) != value:
                updates[name] = mask_secrets(value)
        for name in ("usage_json", "attributes_json"):
            masked = _mask_json_text(row[name])
            if masked != row[name]:
                updates[name] = masked
        message = row["message"]
        if isinstance(message, str) and mask_secrets(message) != message:
            updates["message"] = mask_secrets(message)
        if not updates:
            return False
        assignments = ", ".join(f"{name} = ?" for name in updates)
        self._connection.execute(
            f"UPDATE events SET {assignments} WHERE seq = ?", (*updates.values(), row["seq"])
        )
        return True

    def _migrate_locked(self) -> tuple[ChainState, bool]:
        """Start the chain on a new or legacy database (one transaction)."""
        columns = self._columns()
        if "prev_hash" in columns or "hash" in columns:
            # Columns and metadata are created in the same transaction.
            raise self._broken("chain columns exist without chain metadata (tampering)")
        conn = self._connection
        conn.execute("ALTER TABLE events ADD COLUMN prev_hash TEXT")
        conn.execute("ALTER TABLE events ADD COLUMN hash TEXT")
        conn.execute("CREATE TABLE event_chain (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        legacy = [row["seq"] for row in conn.execute("SELECT seq FROM events ORDER BY seq")]
        masked = 0
        prev = ChainState(0, self._genesis, 0, self._genesis)
        for seq in legacy:
            row = conn.execute("SELECT * FROM events WHERE seq = ?", (seq,)).fetchone()
            if self._mask_row_locked(row):
                masked += 1
                row = conn.execute("SELECT * FROM events WHERE seq = ?", (seq,)).fetchone()
            try:
                digest = row_hash(prev.hash, [row[name] for name in _HASHED_COLUMNS])
            except (TypeError, ValueError):
                raise self._broken(f"legacy event {seq} is not canonical") from None
            conn.execute(
                "UPDATE events SET prev_hash = ?, hash = ? WHERE seq = ?",
                (prev.hash, digest, seq),
            )
            prev = ChainState(seq, digest, 0, self._genesis)
        meta = {
            "version": "1",
            "namespace": self.namespace,
            "anchor_seq": "0",
            "anchor_hash": self._genesis,
            # Informational: rows up to here existed before the chain; their integrity
            # is attested from the migration on, not before.
            "migrated_through_seq": str(prev.seq),
            "migrated_rows": str(len(legacy)),
            "masked_rows": str(masked),
            # Durable "redacted values may still be in the file/WAL" marker: cleared only
            # after a WAL checkpoint(TRUNCATE) completed (retried at every open).
            "purge_pending": "1" if masked else "0",
            "migrated_at": datetime.now(UTC).isoformat(),
        }
        conn.executemany("INSERT INTO event_chain (key, value) VALUES (?, ?)", meta.items())
        self._meta = meta
        if self.head_path is not None:
            head = self._read_head_locked()
            # Only a crash inside this very migration leaves a head without metadata;
            # it must then describe exactly this result.
            if head is not None and (head[0] is not None or head[1] != prev):
                raise self._broken("head anchor exists but the database has no chain")
        self._write_head_locked(None, prev)
        return prev, masked > 0

    def _recover_locked(self, state: ChainState, lost: ChainState) -> ChainState:
        """Head one transaction ahead: record it, never treat it as nothing happened."""
        attributes = {
            "reason": "interrupted write or deleted tail",
            "head_seq": lost.seq,
            "head_hash": lost.hash,
            "head_anchor_seq": lost.anchor_seq,
        }
        values = (
            f"chain-recovered-{uuid4().hex}",
            datetime.now(UTC).isoformat(),
            RECOVERED_EVENT_TYPE,
            None,
            None,
            None,
            None,
            None,
            "recovered",
            _json_dumps(None),
            None,
            _json_dumps(attributes),
        )
        new = self._insert_locked(values, state)
        self._write_head_locked(state, new)
        self.recovery = {"recovered_at_seq": new.seq, **attributes}
        logger.warning(
            "event store %s: head was ahead of the database (seq %s); recorded as %s",
            self.namespace,
            lost.seq,
            RECOVERED_EVENT_TYPE,
        )
        return new

    def _prepare_schema(self) -> bool:
        """Pragmas + DDL. Busy/locked leaves the store pending; anything else is sticky."""
        try:
            with self._connection:
                self._connection.execute(f"PRAGMA busy_timeout = {int(self._timeout * 1000)}")
                self._connection.execute("PRAGMA journal_mode = WAL")
                # Redacted or cleared rows are overwritten, not just unlinked (#185).
                self._connection.execute("PRAGMA secure_delete = ON")
                self._connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS events (
                        seq INTEGER PRIMARY KEY AUTOINCREMENT,
                        event_id TEXT NOT NULL UNIQUE,
                        timestamp TEXT NOT NULL,
                        event_type TEXT NOT NULL,
                        turn_id TEXT,
                        duration_ms REAL,
                        model_id TEXT,
                        tool_name TEXT,
                        tool_use_id TEXT,
                        status TEXT,
                        usage_json TEXT,
                        message TEXT,
                        attributes_json TEXT
                    );

                    CREATE INDEX IF NOT EXISTS events_turn_seq
                        ON events (turn_id, seq);
                    CREATE INDEX IF NOT EXISTS events_type_seq
                        ON events (event_type, seq);
                    """
                )
        except sqlite3.Error as error:
            if _is_transient(error):
                logger.warning(
                    "event store %s: database busy at initialization; will retry", self.namespace
                )
            else:
                self._broken(f"database could not be initialized ({type(error).__name__})")
            return False
        return True

    def _open_chain(self) -> None:
        """Verify (or migrate) and classify the head. Busy/locked leaves it pending."""
        with self._lock:
            if not self._prepare_schema():
                return
            conn = self._connection
            try:
                conn.execute("BEGIN IMMEDIATE")
            except sqlite3.Error as error:
                if _is_transient(error):
                    logger.warning(
                        "event store %s: database busy at open; will retry", self.namespace
                    )
                else:
                    self._broken(f"database error at open ({type(error).__name__})")
                return
            try:
                if self._read_meta() is None:
                    state, _masked = self._migrate_locked()
                else:
                    state = self._verify_all_locked()
                    head = None if self.head_path is None else self._read_head_locked()
                    if self.head_path is None or (head is not None and head[1] == state):
                        pass
                    elif head is None:
                        raise self._broken("head anchor is missing")
                    elif head[0] is not None and head[0] == state:
                        state = self._recover_locked(state, head[1])
                    else:
                        raise self._broken("head anchor does not match the chain")
                conn.commit()
            except EventChainError as error:
                with contextlib.suppress(sqlite3.Error):
                    conn.rollback()
                self._broken(str(error))
                return
            except sqlite3.Error as error:
                with contextlib.suppress(sqlite3.Error):
                    conn.rollback()
                if _is_transient(error):
                    logger.warning(
                        "event store %s: database busy at open; will retry", self.namespace
                    )
                else:
                    self._broken(f"event chain could not be opened ({type(error).__name__})")
                return
            except Exception as error:  # noqa: BLE001 - any surprise is a verdict on the data
                with contextlib.suppress(sqlite3.Error):
                    conn.rollback()
                self._broken(f"event chain could not be opened ({type(error).__name__})")
                return
            self._state = state
        if self._meta.get("purge_pending") == "1":
            self.purge_secrets()

    def _ensure_open_locked(self) -> None:
        """Retry an open that was busy; raise (not sticky) while it still is."""
        if self._error is not None:
            raise EventChainError(self._error)
        if self._state is None:
            self._open_chain()
        if self._error is not None:
            raise EventChainError(self._error)
        if self._state is None:
            raise EventChainError("event store is busy; retry later")

    def purge_secrets(self) -> bool:
        """Finish erasing redacted legacy values (WAL checkpoint TRUNCATE).

        Returns True when nothing is pending. A checkpoint blocked by a reader is logged
        and retried (here, at every open and at close); the durable marker stays set.
        """
        with self._lock:
            if self._meta.get("purge_pending") != "1" or self._state is None:
                return self._meta.get("purge_pending") != "1"
            conn = self._connection
            done = False
            try:
                conn.execute(f"PRAGMA busy_timeout = {int(min(self._timeout, 1.0) * 1000)}")
                for attempt in range(_PURGE_ATTEMPTS):
                    busy = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()[0]
                    if busy == 0:
                        done = True
                        break
                    if attempt + 1 < _PURGE_ATTEMPTS:
                        time.sleep(0.05)
                if done:
                    conn.execute("BEGIN IMMEDIATE")
                    conn.execute("UPDATE event_chain SET value = '0' WHERE key = 'purge_pending'")
                    conn.commit()
                    self._meta = {**self._meta, "purge_pending": "0"}
                    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except sqlite3.Error as error:
                with contextlib.suppress(sqlite3.Error):
                    conn.rollback()
                logger.warning(
                    "event store %s: purge of redacted values failed (%s); will retry",
                    self.namespace,
                    type(error).__name__,
                )
                return False
            finally:
                with contextlib.suppress(sqlite3.Error):
                    conn.execute(f"PRAGMA busy_timeout = {int(self._timeout * 1000)}")
            if not done:
                logger.warning(
                    "event store %s: WAL checkpoint was blocked; redacted legacy values may "
                    "remain in the file/WAL until it completes (will retry)",
                    self.namespace,
                )
            return done

    def _write(self, operation) -> Any:
        """Run ``operation(state) -> (new_state, result)`` under the write lock.

        The head is written before COMMIT; any failure after that point leaves the
        instance unhealthy (the next open classifies the files).
        """
        with self._lock:
            self._ensure_open_locked()
            conn = self._connection
            try:
                conn.execute("BEGIN IMMEDIATE")
            except sqlite3.Error as error:
                raise self._sqlite_failure(error) from None
            head_written = False
            stage = "verify"
            try:
                state = self._refresh_locked()
                stage = "operation"
                new, result = operation(state)
                if new != state:
                    stage = "anchor"
                    head_written = True
                    self._write_head_locked(state, new)
                conn.commit()
            except BaseException as error:
                with contextlib.suppress(sqlite3.Error):
                    conn.rollback()
                if head_written:
                    raise self._broken("write failed after the head was anchored") from None
                if isinstance(error, EventChainError):
                    raise self._broken(str(error)) from None
                if isinstance(error, sqlite3.Error):
                    raise self._sqlite_failure(error) from None
                if stage == "operation" and isinstance(error, ValueError):
                    raise  # the event itself was refused; the chain is untouched
                if isinstance(error, Exception):
                    raise self._broken(f"write failed ({type(error).__name__})") from None
                raise
            self._state = new
            return result

    def verify_chain(self) -> tuple[bool, str | None]:
        """Re-read and re-hash every row and compare with the head. Failures are sticky."""
        with self._lock:
            try:
                self._ensure_open_locked()
            except EventChainError as error:
                return False, str(error)
            conn = self._connection
            try:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    state = self._verify_all_locked()
                    self._check_head_locked(state)
                finally:
                    with contextlib.suppress(sqlite3.Error):
                        conn.rollback()
            except EventChainError as error:
                self._broken(str(error))
                return False, self._error
            except sqlite3.Error as error:
                failure = self._sqlite_failure(error)
                return False, str(failure) if self._error is None else self._error
            except Exception as error:  # noqa: BLE001 - any surprise is a verdict on the data
                self._broken(f"verification failed ({type(error).__name__})")
                return False, self._error
            self._state = state
            return True, None

    def chain_status(self) -> dict[str, Any]:
        """Small status for diagnostics (no paths, no event content)."""
        state = self._state
        return {
            "ok": self.healthy,
            "error": self._error,
            "namespace": self.namespace,
            "seq": None if state is None else state.seq,
            "anchor_seq": None if state is None else state.anchor_seq,
            "migrated_through_seq": self._meta.get("migrated_through_seq"),
            "recovered": self.recovery is not None,
            "anchored": self.head_path is not None,
            "purge_pending": self._meta.get("purge_pending") == "1",
        }

    def emit(self, event: ObservabilityEvent) -> None:
        data = event_to_dict(event)
        event_id = data["event_id"]
        if (
            not isinstance(event_id, str)
            or not event_id.strip()
            or any(character in event_id for character in ("\r", "\n", "\0"))
        ):
            raise ValueError("event_id must be a nonempty, SSE-safe string.")
        if mask_secrets(event_id) != event_id:
            # Ids are hashed and served as SSE cursors; never a secret-looking value.
            raise ValueError("event_id must not look like a secret.")
        if data["timestamp"] is None:
            raise ValueError("timestamp must not be None.")
        if not isinstance(data["event_type"], str) or not data["event_type"]:
            raise ValueError("event_type must be a nonempty string.")
        # Defence in depth (#185, ADR 0009 §5): free text and structured payloads are
        # masked here too, so the hashed material never holds a plaintext secret.
        # Identifier columns are not rewritten silently: a secret-looking one is refused.
        for name in _ID_COLUMNS:
            if mask_secrets(data[name]) != data[name]:
                raise ValueError(f"{name} must not look like a secret.")
        for name in ("message", "usage", "attributes"):
            data[name] = mask_secrets(data[name])

        values = (
            event_id,
            data["timestamp"],
            data["event_type"],
            data["turn_id"],
            data["duration_ms"],
            data["model_id"],
            data["tool_name"],
            data["tool_use_id"],
            data["status"],
            _json_dumps(data["usage"]),
            data["message"],
            _json_dumps(data["attributes"]),
        )

        for value in values:
            # #601: a value SQLite cannot bind (a huge int, a dict in a text column) is the
            # event's fault: refused here, so it never marks a healthy store broken.
            if value is None or isinstance(value, (str, float)):
                continue
            if isinstance(value, int) and -(2**63) <= value < 2**63:
                continue
            raise ValueError("event field has a type or size the store cannot hold.")

        def append(state: ChainState) -> tuple[ChainState, None]:
            duplicate = self._connection.execute(
                "SELECT 1 FROM events WHERE event_id = ?", (event_id,)
            ).fetchone()
            if duplicate is not None:
                return state, None
            return self._insert_locked(values, state), None

        self._write(append)

    def receipt(self, event_id: str) -> dict[str, Any] | None:
        """The stored row of one event as an ADR 0013 §2b (A) read-back, or ``None``.

        ``{seq, hash, event_type, attributes}`` exactly as committed (the caller compares
        them with what it wrote). Reads only; any database error is ``None``.
        """
        if type(event_id) is not str:
            return None
        with self._lock:
            try:
                row = self._connection.execute(
                    "SELECT seq, hash, event_type, attributes_json FROM events WHERE event_id = ?",
                    (event_id,),
                ).fetchone()
            except sqlite3.Error:
                return None
        if row is None:
            return None
        try:
            attributes = json.loads(row["attributes_json"]) if row["attributes_json"] else {}
        except (TypeError, ValueError):
            return None
        return {
            "seq": row["seq"],
            "hash": row["hash"],
            "event_type": row["event_type"],
            "attributes": attributes,
        }

    def _to_event(self, row: sqlite3.Row) -> ObservabilityEvent:
        data = {name: row[name] for name in _EVENT_FIELDS if name not in {"usage", "attributes"}}
        data["usage"] = json.loads(row["usage_json"]) if row["usage_json"] is not None else None
        data["attributes"] = (
            json.loads(row["attributes_json"]) if row["attributes_json"] is not None else None
        )
        return ObservabilityEvent(
            **{name: _restore(value, self._hints.get(name)) for name, value in data.items()}
        )

    def get_events(
        self,
        since_id: str | None = None,
        limit: int = 50,
        event_type: str | Enum | None = None,
        turn_id: str | None = None,
    ) -> list[ObservabilityEvent]:
        """Return events strictly after since_id, with optional filters."""
        _validate_limit(limit)
        conditions: list[str] = []
        parameters: list[Any] = []

        if since_id is not None:
            conditions.append("seq > COALESCE((SELECT seq FROM events WHERE event_id = ?), 0)")
            parameters.append(since_id)
        if event_type is not None:
            conditions.append("event_type = ?")
            parameters.append(event_type.value if isinstance(event_type, Enum) else event_type)
        if turn_id is not None:
            conditions.append("turn_id = ?")
            parameters.append(turn_id)

        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        parameters.append(limit)
        with self._lock:
            rows = self._connection.execute(
                f"SELECT * FROM events{where} ORDER BY seq ASC LIMIT ?",
                parameters,
            ).fetchall()
        return [self._to_event(row) for row in rows]

    def get_turn_events(self, turn_id: str) -> list[ObservabilityEvent]:
        """Return all events for a turn, without the pagination limit."""
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM events WHERE turn_id = ? ORDER BY seq ASC",
                (turn_id,),
            ).fetchall()
        return [self._to_event(row) for row in rows]

    @staticmethod
    def _summarize_turn(rows: list[sqlite3.Row]) -> dict[str, Any]:
        start = next(
            (row for row in rows if _event_kind(row["event_type"]) in _START_TYPES),
            rows[0],
        )
        end = next(
            (row for row in reversed(rows) if _event_kind(row["event_type"]) in _END_TYPES),
            None,
        )

        duration = None
        status = "running"
        if end is not None:
            kind = _event_kind(end["event_type"])
            if kind in {"turn_failed", "turn_error"}:
                status = "failed"
            elif kind in {"turn_cancelled", "turn_canceled"}:
                status = "cancelled"
            else:
                status = "completed"
            status = end["status"] or status
            duration = end["duration_ms"]
            if duration is None:
                duration = _duration_ms(start["timestamp"], end["timestamp"])

        model = next(
            (row["model_id"] for row in reversed(rows) if row["model_id"]),
            None,
        )
        return {
            "id": rows[0]["turn_id"],
            "start": start["timestamp"],
            "end": end["timestamp"] if end is not None else None,
            "duration": duration,
            "status": status,
            "model": model,
            "event_count": len(rows),
        }

    def _get_turns_locked(self, limit: int) -> list[dict[str, Any]]:
        turns = self._connection.execute(
            """
            SELECT turn_id
            FROM events
            WHERE turn_id IS NOT NULL
            GROUP BY turn_id
            ORDER BY MAX(seq) DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

        summaries = []
        for turn in turns:
            rows = self._connection.execute(
                "SELECT * FROM events WHERE turn_id = ? ORDER BY seq ASC",
                (turn["turn_id"],),
            ).fetchall()
            summaries.append(self._summarize_turn(rows))
        return summaries

    def get_turns(self, limit: int = 20) -> list[dict[str, Any]]:
        """Return recently active turns. The duration field is milliseconds."""
        _validate_limit(limit)
        with self._lock:
            return self._get_turns_locked(limit)

    def get_snapshot(self) -> dict[str, Any]:
        """Return counts, recent events, turn summaries, and an SSE cursor."""
        with self._lock:
            # A read transaction also keeps this snapshot consistent with
            # writers using a different store instance or process.
            self._connection.execute("BEGIN")
            try:
                counts = self._connection.execute(
                    """
                    SELECT COUNT(*) AS event_count,
                           COUNT(DISTINCT turn_id) AS turn_count
                    FROM events
                    """
                ).fetchone()
                rows = self._connection.execute(
                    "SELECT * FROM events ORDER BY seq DESC LIMIT 50"
                ).fetchall()
                turns = self._get_turns_locked(20)
                result = {
                    "event_count": counts["event_count"],
                    "turn_count": counts["turn_count"],
                    "latest_event_id": rows[0]["event_id"] if rows else None,
                    "events": [event_to_dict(self._to_event(row)) for row in reversed(rows)],
                    "turns": turns,
                }
                self._connection.commit()
                return result
            except Exception:
                self._connection.rollback()
                raise

    def clear(self) -> None:
        """Delete events without resetting the internal sequence.

        The chain is not restarted: its anchor moves to the last deleted row, so the
        next event still links to it (ADR 0009 §3).
        """

        def operation(state: ChainState) -> tuple[ChainState, None]:
            if state.seq == state.anchor_seq:
                return state, None
            self._connection.execute("DELETE FROM events")
            self._connection.executemany(
                "UPDATE event_chain SET value = ? WHERE key = ?",
                ((str(state.seq), "anchor_seq"), (state.hash, "anchor_hash")),
            )
            self._meta = {**self._meta, "anchor_seq": str(state.seq), "anchor_hash": state.hash}
            return ChainState(state.seq, state.hash, state.seq, state.hash), None

        self._write(operation)

    def close(self) -> None:
        """Close the store after its emitters and SSE consumers have stopped."""
        with self._lock:
            if self._meta.get("purge_pending") == "1":
                self.purge_secrets()
            self._connection.close()


__all__ = ["DEFAULT_NAMESPACE", "EventChainError", "SQLiteEventStore", "event_to_dict"]
