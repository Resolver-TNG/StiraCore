"""Archive-then-remove and explicit restore for retention runs and delete requests (#294).

One :class:`RemovalPlan` names everything a run takes out of the live stores:
Episodic/Semantic entries (and their search-index rows), checkpoint files,
context-stash files, ``turns.db`` rows (UI events, tool payloads) and lines of the
staging-rejection ledger. :func:`execute` copies all of it into one removal record
(:mod:`stiracore.control.removal_archive`), proves the record reads back, logs
``remove.started``, removes the items, compacts the SQLite files the rows lived in
(``secure_delete`` + ``VACUUM``, WAL truncated, so the text does not linger in free
pages), regenerates ``semantic/index.md`` when Semantic changed and logs
``remove.applied``. :func:`restore` is the only way back (``restore.*``). Callers
hold the data directory's owner lock (#285): one writer.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import sqlite3
import stat
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from stiracore._platform import utime_nofollow
from stiracore.control.forgetting_archive import ForgettingArchiveError, sha256_text
from stiracore.control.memory_layout import DataLayout, UnsafePathError, read_bytes
from stiracore.control.removal_archive import RemovalArchive, RemovalRecord
from stiracore.tools.memory_store import MemoryLayer, MemoryStore


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


__all__ = ["RemovalPlan", "RemovalResult", "RestoreResult", "execute", "restore"]

# Whole files a removal record holds (Base64): unlinked by digest, written back by restore.
FILE_STORES = ("checkpoint", "context_stash", "emotion_record", "memory_plan")
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_BINARY = getattr(os, "O_BINARY", 0)
_UI_COLUMNS = ("id", "turn_id", "seq", "type", "ts", "payload")
_PAYLOAD_COLUMNS = (
    "turn_id",
    "tool_use_id",
    "kind",
    "text",
    "total_chars",
    "total_lines",
    "retained_truncated",
)


@dataclass
class RemovalPlan:
    memory: list[tuple[MemoryLayer, str]] = field(default_factory=list)
    files: list[tuple[str, Path]] = field(default_factory=list)  # (one of FILE_STORES, path)
    turn_events: list[int] = field(default_factory=list)  # ui_events.id
    tool_payloads: list[tuple[str, str, str]] = field(default_factory=list)
    rejection_lines: list[int] = field(default_factory=list)  # 0-based line numbers
    index_rows: list[tuple[str, str]] = field(default_factory=list)  # stale rows only
    regenerate_index: bool = False
    # Stores the planner could not inspect: an --apply built on this plan is refused.
    skipped: list[str] = field(default_factory=list)

    def empty(self) -> bool:
        return not (
            self.memory
            or self.files
            or self.turn_events
            or self.tool_payloads
            or self.rejection_lines
            or self.index_rows
            or self.regenerate_index
        )


@dataclass(frozen=True)
class RemovalResult:
    record_id: str | None
    counts: dict[str, int]
    applied: bool
    error: str | None = None


@dataclass(frozen=True)
class RestoreResult:
    record_id: str
    restored: dict[str, int]
    skipped: int = 0
    refused: str | None = None
    cleanup_error: str | None = None


# -- capture ----------------------------------------------------------------


def _file_item(layout: DataLayout, store_name: str, path: Path) -> dict[str, Any] | None:
    data = read_bytes(layout, path)
    if data is None:
        return None
    info = os.lstat(path)
    return {
        "store": store_name,
        "path": layout.relative(path),
        "data_b64": base64.b64encode(data).decode("ascii"),
        "sha256": _digest(data),
        "mtime_ns": info.st_mtime_ns,
    }


def _memory_item(store: MemoryStore, layer: MemoryLayer, key: str) -> dict[str, Any] | None:
    text = store.read_text(layer, key)
    if text is None:
        return None
    path = store._path(layer, key)
    from stiracore.memory_origin import verified_origin

    return {
        "store": "memory",
        "layer": layer.value,
        "key": key,
        "text": text,
        "sha256": sha256_text(text),
        "mtime_ns": os.lstat(path).st_mtime_ns,
        # The verified origin binding (#294) is restored with the entry.
        "provenance": verified_origin(store, layer, key, text),
    }


def _turn_rows(layout: DataLayout, plan: RemovalPlan) -> list[dict[str, Any]]:
    if not (plan.turn_events or plan.tool_payloads) or not layout.turns.exists():
        return []
    layout.check_inside(layout.turns)
    items: list[dict[str, Any]] = []
    connection = sqlite3.connect(layout.turns, timeout=30)
    connection.row_factory = sqlite3.Row
    try:
        for row_id in plan.turn_events:
            row = connection.execute("SELECT * FROM ui_events WHERE id = ?", (row_id,)).fetchone()
            if row is not None:
                items.append(
                    {"store": "turn_event", "row": {name: row[name] for name in _UI_COLUMNS}}
                )
        for key in plan.tool_payloads:
            row = connection.execute(
                "SELECT * FROM tool_payloads WHERE turn_id = ? AND tool_use_id = ? AND kind = ?",
                key,
            ).fetchone()
            if row is not None:
                items.append(
                    {
                        "store": "tool_payload",
                        "row": {name: row[name] for name in _PAYLOAD_COLUMNS},
                    }
                )
    finally:
        connection.close()
    return items


def _rejection_lines(layout: DataLayout, numbers: list[int]) -> list[dict[str, Any]]:
    if not numbers:
        return []
    data = read_bytes(layout, layout.rejections)
    if data is None:
        return []
    lines = data.decode("utf-8").splitlines()
    return [
        {"store": "staging_rejection", "line": lines[number]}
        for number in sorted(set(numbers))
        if 0 <= number < len(lines)
    ]


def capture(layout: DataLayout, store: MemoryStore, plan: RemovalPlan) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for layer, key in plan.memory:
        item = _memory_item(store, layer, key)
        if item is not None:
            items.append(item)
    for store_name, path in plan.files:
        item = _file_item(layout, store_name, path)
        if item is not None:
            items.append(item)
    items.extend(_turn_rows(layout, plan))
    items.extend(_rejection_lines(layout, plan.rejection_lines))
    return items


def _counts(items: list[dict[str, Any]] | tuple[dict[str, Any], ...]) -> dict[str, int]:
    found: dict[str, int] = {}
    for item in items:
        found[item["store"]] = found.get(item["store"], 0) + 1
    return found


# -- removal ----------------------------------------------------------------


_BUSY_TIMEOUT_SEC = 30.0


def _compact(path: Path, *, wal: bool) -> None:
    """Rewrite a SQLite file so removed rows leave no copy in free pages or the WAL.

    A WAL checkpoint that another connection keeps from finishing reports it in its
    result row (``busy``), not by an exception: that is raised, so the caller does not
    count the text as gone from the WAL.
    """
    connection = sqlite3.connect(path, timeout=_BUSY_TIMEOUT_SEC)
    try:
        connection.execute("PRAGMA secure_delete = ON")
        if wal:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.execute("VACUUM")
        if wal:
            busy = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
            if busy is not None and busy[0]:
                raise sqlite3.OperationalError("WAL checkpoint busy: the WAL was not truncated")
    finally:
        connection.close()


def _unlink_checked(layout: DataLayout, path: Path, expected_sha: str) -> None:
    data = read_bytes(layout, path)
    if data is None:
        return
    if _digest(data) != expected_sha:
        raise ForgettingArchiveError(f"{layout.relative(path)} changed after it was archived")
    os.unlink(path)


def _remove_memory(store: MemoryStore, item: dict[str, Any]) -> None:
    layer = MemoryLayer(item["layer"])
    path = store._path(layer, item["key"])
    store._require_writable(path)
    current = store.read_text(layer, item["key"])
    if current is not None:
        if sha256_text(current) != item["sha256"]:
            raise ForgettingArchiveError("a memory entry changed after it was archived")
        path.unlink()
    with store._connection() as connection:
        connection.execute("PRAGMA secure_delete = ON")
        connection.execute(
            "DELETE FROM memory_entries WHERE layer = ? AND key = ?", (layer.value, item["key"])
        )


def _rewrite_ledger(layout: DataLayout, drop: set[str]) -> None:
    data = read_bytes(layout, layout.rejections)
    if data is None:
        return
    lines = data.decode("utf-8").splitlines()
    kept = [line for line in lines if line not in drop]
    if len(kept) == len(lines):
        return
    _atomic_write(layout.rejections, ("\n".join(kept) + "\n" if kept else "").encode("utf-8"))


def _atomic_write(path: Path, data: bytes) -> None:
    mode = 0o600
    try:
        mode = stat.S_IMODE(os.lstat(path).st_mode)
    except FileNotFoundError:
        pass
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".stira-", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _remove_items(
    layout: DataLayout, store: MemoryStore, items: tuple[dict[str, Any], ...], plan: RemovalPlan
) -> bool:
    """Remove what the record holds; return whether Semantic changed."""
    semantic = plan.regenerate_index
    rows = [item["row"] for item in items if item["store"] == "turn_event"]
    payloads = [item["row"] for item in items if item["store"] == "tool_payload"]
    for item in items:
        if item["store"] == "memory":
            _remove_memory(store, item)
            semantic = semantic or item["layer"] == MemoryLayer.SEMANTIC.value
        elif item["store"] in FILE_STORES:
            _unlink_checked(layout, layout.data_dir / item["path"], item["sha256"])
    if rows or payloads:
        layout.check_inside(layout.turns)
        connection = sqlite3.connect(layout.turns, timeout=30)
        try:
            with connection:
                connection.execute("PRAGMA secure_delete = ON")
                connection.executemany(
                    "DELETE FROM ui_events WHERE id = ?", [(row["id"],) for row in rows]
                )
                connection.executemany(
                    "DELETE FROM tool_payloads WHERE turn_id = ? AND tool_use_id = ? AND kind = ?",
                    [(row["turn_id"], row["tool_use_id"], row["kind"]) for row in payloads],
                )
        finally:
            connection.close()
        _compact(layout.turns, wal=True)
    drop = {item["line"] for item in items if item["store"] == "staging_rejection"}
    if drop:
        _rewrite_ledger(layout, drop)
    if plan.index_rows:
        with store._connection() as connection:
            connection.execute("PRAGMA secure_delete = ON")
            connection.executemany(
                "DELETE FROM memory_entries WHERE layer = ? AND key = ?", plan.index_rows
            )
    if semantic:
        from stiracore.control.harvest import write_semantic_index

        write_semantic_index(store)
    if any(item["store"] == "memory" for item in items) or plan.index_rows:
        _compact(store.index_path, wal=False)
    return semantic


def open_store(layout: DataLayout) -> MemoryStore:
    return MemoryStore(layout.memory_base, layout.agent_id)


def execute(
    layout: DataLayout,
    plan: RemovalPlan,
    *,
    purpose: str,
    reason: str,
    at: datetime,
    audit: dict[str, Any] | None = None,
    archive: RemovalArchive | None = None,
) -> RemovalResult:
    """Archive everything in ``plan``, then remove it. Fail-closed before any removal."""
    if archive is not None:
        return _execute(layout, plan, purpose, reason, at, audit, archive)
    archive = RemovalArchive(layout)
    try:
        return _execute(layout, plan, purpose, reason, at, audit, archive)
    finally:
        archive.close()


def _execute(layout, plan, purpose, reason, at, audit, archive: RemovalArchive) -> RemovalResult:
    store = open_store(layout)
    try:
        items = capture(layout, store, plan)
    except (OSError, ValueError, sqlite3.Error) as error:
        # Type only: a message can name a path (and a path can carry the words removed).
        return RemovalResult(None, {}, False, f"E_NOT_CAPTURED ({type(error).__name__})")
    if not items and not plan.index_rows and not plan.regenerate_index:
        return RemovalResult(None, {}, True)
    try:
        archive.prepare()
        record = archive.save(purpose, reason, items, at)
        archive.audit(
            "remove.started",
            id=record.id,
            purpose=purpose,
            counts=_counts(items),
            items_sha256=record.items_sha256,
            index_rows=len(plan.index_rows),
            **(audit or {}),
        )
    except ForgettingArchiveError as error:
        return RemovalResult(None, _counts(items), False, str(error))
    try:
        _remove_items(layout, store, record.items, plan)
    except (OSError, ValueError, sqlite3.Error, ForgettingArchiveError) as error:
        message = f"E_STOPPED ({type(error).__name__}); restore {record.id} to undo"
        try:
            archive.audit("remove.refused", id=record.id, reason=message)
        except ForgettingArchiveError:
            pass
        return RemovalResult(record.id, _counts(items), False, message)
    try:
        archive.audit("remove.applied", id=record.id, counts=_counts(items))
    except ForgettingArchiveError:
        # Removed (the record id is what restores it): reported, not raised.
        return RemovalResult(record.id, _counts(items), True, "E_AUDIT: remove.applied not logged")
    return RemovalResult(record.id, _counts(items), True)


# -- restore ----------------------------------------------------------------


def _present(layout: DataLayout, store: MemoryStore, item: dict[str, Any]) -> bool | None:
    """True: already there identically; False: absent; None: something else is there."""
    if item["store"] == "memory":
        current = store.read_text(MemoryLayer(item["layer"]), item["key"])
        if current is None:
            return False
        return True if sha256_text(current) == item["sha256"] else None
    if item["store"] in FILE_STORES:
        path = layout.data_dir / item["path"]
        if not path.resolve().is_relative_to(layout.data_dir):
            raise UnsafePathError("record names a path outside the data directory")
        data = read_bytes(layout, path)
        if data is None:
            return False
        return True if _digest(data) == item["sha256"] else None
    return False  # rows and lines are checked in their own transaction / by content


def _write_new_file(layout: DataLayout, item: dict[str, Any]) -> None:
    path = layout.data_dir / item["path"]
    layout.check_inside(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    layout.check_inside(path)
    data = base64.b64decode(item["data_b64"], validate=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW | _BINARY, 0o600)
    try:
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view) :]
            os.fsync(fd)
        finally:
            os.close(fd)
    except BaseException:
        # Created by this call (O_EXCL): a partial file would block every later restore.
        with contextlib.suppress(OSError):
            os.unlink(path)
        raise


def _restore_attributes(layout: DataLayout, store: MemoryStore, item: dict[str, Any]) -> None:
    """The mtime (and an entry's recorded origin) of an item whose bytes are back.

    Idempotent: also run for an item an earlier, interrupted restore already wrote (its
    bytes are there, its attributes maybe not), so running the restore again finishes it.
    """
    if item["store"] == "memory":
        layer = MemoryLayer(item["layer"])
        path = store._path(layer, item["key"])
        utime_nofollow(path, item["mtime_ns"], item["mtime_ns"])
        bound = item.get("provenance")
        if isinstance(bound, dict) and bound.get("origin"):
            from stiracore.memory_origin import record_origin

            record_origin(store, layer, item["key"], bound["origin"], bound.get("source"))
    elif item["store"] in FILE_STORES:
        utime_nofollow(layout.data_dir / item["path"], item["mtime_ns"], item["mtime_ns"])


class _Conflict(ForgettingArchiveError):
    """Something else is where an item goes; described by a fixed area and a digest."""

    def __init__(self, layout: DataLayout | None, item: dict[str, Any]) -> None:
        from stiracore.control.memory_layout import area_of

        if "row" in item:
            area, where = "turns.db", json.dumps(item["row"], sort_keys=True, default=str)
        elif item["store"] == "memory":
            area = item["layer"]
            where = f"{item['layer']}/{item['key']}"
        else:
            area = area_of(layout, item["path"]) if layout else "other"
            where = item["path"]
        self.area = area
        self.where_sha256 = hashlib.sha256(where.encode("utf-8")).hexdigest()
        super().__init__(f"E_CONFLICT: {area} {self.where_sha256[:16]}")


def _refusal(error: BaseException) -> str:
    """What a refusal may say (output and audit log): fixed text and codes only.

    A conflict names its place by a fixed area label and a digest; the archive's own
    errors are fixed sentences; anything else (an ``OSError`` or ``UnsafePathError``
    can carry a path, and a path can carry the words of a memory) is code and type.
    """
    from stiracore.control.memory_layout import UnsafePathError

    if isinstance(error, ForgettingArchiveError):
        return str(error)
    if isinstance(error, UnsafePathError):
        return f"E_UNSAFE_PATH ({type(error).__name__})"
    if isinstance(error, sqlite3.Error):
        return f"E_SQLITE ({type(error).__name__})"
    if isinstance(error, OSError):
        return f"E_OS ({type(error).__name__})"
    return f"E_FAILED ({type(error).__name__})"


def _same(current: Any, row: dict[str, Any], columns: tuple[str, ...]) -> bool:
    return all(current[name] == row[name] for name in columns)


def _row_state(connection, kind: str, row: dict[str, Any]) -> bool:
    """True: already there with every column equal; False: free. Conflict: raises.

    A UI event is matched by its id *and* by its (turn_id, seq); a payload by its key.
    Only a row equal in **every** column counts as restored — anything else in an
    item's place is a conflict, so the record (the only copy) is never dropped for a
    row that merely shares an id or a payload.
    """
    if kind == "turn_event":
        found = connection.execute(
            "SELECT * FROM ui_events WHERE id = ? OR (turn_id = ? AND seq = ?)",
            (row["id"], row["turn_id"], row["seq"]),
        ).fetchall()
        columns = _UI_COLUMNS
    else:
        found = connection.execute(
            "SELECT * FROM tool_payloads WHERE turn_id = ? AND tool_use_id = ? AND kind = ?",
            (row["turn_id"], row["tool_use_id"], row["kind"]),
        ).fetchall()
        columns = _PAYLOAD_COLUMNS
    if not found:
        return False
    if len(found) == 1 and _same(found[0], row, columns):
        return True
    raise _Conflict(None, {"store": kind, "row": row})


def _check_rows(layout: DataLayout, items: tuple[dict[str, Any], ...]) -> None:
    """Refuse a restore whose ``turns.db`` rows conflict, before any change (read only)."""
    wanted = [(item["store"], item["row"]) for item in items if item["store"] in _ROW_STORES]
    if not wanted:
        return
    from stiracore.control.memory_layout import connect_read_only

    connection = connect_read_only(layout, layout.turns)
    if connection is None:
        return  # a deleted turns.db: every row goes into a fresh one
    try:
        tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master")}
        for kind, row in wanted:
            if _ROW_STORES[kind] in tables:
                _row_state(connection, kind, row)
    finally:
        connection.close()


_ROW_STORES = {"turn_event": "ui_events", "tool_payload": "tool_payloads"}


def _restore_rows(layout: DataLayout, items: tuple[dict[str, Any], ...]) -> tuple[int, int]:
    wanted = [(item["store"], item["row"]) for item in items if item["store"] in _ROW_STORES]
    if not wanted:
        return 0, 0
    from stiracore.observability.turn_events import TurnEventStore

    layout.check_inside(layout.turns)
    TurnEventStore(layout.turns).close()  # schema, also for a deleted turns.db
    connection = sqlite3.connect(layout.turns, timeout=30)
    connection.row_factory = sqlite3.Row
    counts = {"turn_event": 0, "tool_payload": 0}
    try:
        with connection:  # one transaction: a conflict found now rolls every row back
            for kind, row in wanted:
                if _row_state(connection, kind, row):
                    continue
                columns = _UI_COLUMNS if kind == "turn_event" else _PAYLOAD_COLUMNS
                connection.execute(
                    f"INSERT INTO {_ROW_STORES[kind]} ({', '.join(columns)})"
                    f" VALUES ({', '.join('?' for _ in columns)})",
                    tuple(row[name] for name in columns),
                )
                counts[kind] += 1
    except sqlite3.IntegrityError:
        raise ForgettingArchiveError("turns.db rows conflict with the record") from None
    finally:
        connection.close()
    return counts["turn_event"], counts["tool_payload"]


def restore(layout: DataLayout, removal_id: str, *, archive: RemovalArchive | None = None):
    """Put back every item of one removal record (explicit, audited, all-or-nothing check).

    Refused before anything changes when the record is missing or damaged, or when
    something *different* now sits where an item goes (an item already there
    byte-for-byte counts as restored: an interrupted run or a second restore). The
    record is removed after ``restore.applied``.
    """
    archive = archive or RemovalArchive(layout)
    try:
        archive.prepare()
        record: RemovalRecord = archive.load(removal_id)
    except ForgettingArchiveError as error:
        text = _refusal(error)
        try:
            archive.audit("restore.refused", id=str(removal_id), reason=text)
        except ForgettingArchiveError:
            pass
        archive.close()
        return RestoreResult(str(removal_id), {}, refused=text)
    try:
        return _restore(layout, archive, record)
    finally:
        archive.close()


def _restore(layout: DataLayout, archive: RemovalArchive, record: RemovalRecord) -> RestoreResult:
    store = open_store(layout)
    pending: list[dict[str, Any]] = []
    already: list[dict[str, Any]] = []  # bytes back; attributes are set again
    skipped = 0
    try:
        for item in record.items:
            state = _present(layout, store, item)
            if state is None:
                raise _Conflict(layout, item)
            if state:
                skipped += 1
                already.append(item)
            else:
                pending.append(item)
        _check_rows(layout, record.items)  # also the rows, before anything is written
    except (ForgettingArchiveError, ValueError, OSError, sqlite3.Error) as error:
        text = _refusal(error)
        try:
            archive.audit("restore.refused", id=record.id, reason=text)
        except ForgettingArchiveError:
            pass
        return RestoreResult(record.id, {}, skipped, refused=text)
    try:
        archive.audit("restore.started", id=record.id, counts=record.counts())
    except ForgettingArchiveError as error:  # nothing written yet
        return RestoreResult(record.id, {}, skipped, refused=_refusal(error))
    restored: dict[str, int] = {}
    # Rows first, in one transaction checked again row by row: a conflict that appeared
    # after the pre-check rolls them back before any file is written; the record stays.
    try:
        rows, payloads = _restore_rows(layout, record.items)
    except (ForgettingArchiveError, sqlite3.Error, OSError) as error:
        text = _refusal(error)
        try:
            archive.audit("restore.refused", id=record.id, reason=text)
        except ForgettingArchiveError:
            pass
        return RestoreResult(record.id, {}, skipped, refused=text)
    if rows:
        restored["turn_event"] = rows
    if payloads:
        restored["tool_payload"] = payloads
    semantic = any(
        item["store"] == "memory" and item["layer"] == MemoryLayer.SEMANTIC.value
        for item in record.items
    )
    lines_skipped = 0
    try:
        lines_skipped = _restore_items(layout, store, pending, restored)
        for item in already:
            _restore_attributes(layout, store, item)
        if semantic:
            # From the record, not from what this run wrote: an earlier run that put the
            # entries back but failed before (or while) regenerating is finished here.
            from stiracore.control.harvest import write_semantic_index

            write_semantic_index(store)
    except (_PartlyRestored, ForgettingArchiveError, ValueError, OSError, sqlite3.Error) as error:
        # The record stays: running the restore again counts what is back as restored,
        # sets its attributes again and regenerates the index.
        cause = error.error if isinstance(error, _PartlyRestored) else error
        text = f"{_refusal(cause)}; partly restored, run the restore again"
        if semantic:
            from stiracore.control.harvest import write_semantic_index

            with contextlib.suppress(Exception):  # best effort now; the next run must
                write_semantic_index(store)
        with contextlib.suppress(ForgettingArchiveError):
            archive.audit("restore.refused", id=record.id, reason=text, restored=restored)
        partial = error.skipped if isinstance(error, _PartlyRestored) else lines_skipped
        return RestoreResult(record.id, restored, skipped + partial, refused=text)
    skipped += lines_skipped
    try:
        archive.audit("restore.applied", id=record.id, restored=restored, skipped=skipped)
    except ForgettingArchiveError:
        # Restored, but not logged: the record is kept, so running the restore again
        # (every item counts as already there) logs it and removes the record.
        return RestoreResult(
            record.id,
            restored,
            skipped,
            cleanup_error="E_AUDIT: restore.applied not logged; the record is kept",
        )
    cleanup_error = None
    try:
        archive.remove(record.id)
    except (OSError, ForgettingArchiveError) as error:
        cleanup_error = f"record not removed ({type(error).__name__})"
    return RestoreResult(record.id, restored, skipped, cleanup_error=cleanup_error)


class _PartlyRestored(Exception):
    def __init__(self, error: BaseException, skipped: int) -> None:
        super().__init__(type(error).__name__)
        self.error = error
        self.skipped = skipped


def _restore_items(layout, store, pending, restored: dict[str, int]) -> int:
    """Write ``pending`` back, counted in ``restored`` as soon as its bytes are back.

    Returns the ledger lines already there. A failure part way raises
    :class:`_PartlyRestored` (what was written stays counted).
    """
    skipped = 0
    try:
        for item in pending:
            if item["store"] == "memory":
                layer = MemoryLayer(item["layer"])
                store.write_text(layer, item["key"], item["text"], exclusive=True)
            elif item["store"] in FILE_STORES:
                _write_new_file(layout, item)
            elif item["store"] == "staging_rejection":
                data = read_bytes(layout, layout.rejections) or b""
                lines = data.decode("utf-8").splitlines()
                if item["line"] in lines:
                    skipped += 1
                    continue
                text = "\n".join([*lines, item["line"]]) + "\n"
                _atomic_write(layout.rejections, text.encode())
            else:
                continue
            restored[item["store"]] = restored.get(item["store"], 0) + 1
            _restore_attributes(layout, store, item)
    except (ForgettingArchiveError, ValueError, OSError, sqlite3.Error) as error:
        raise _PartlyRestored(error, skipped) from None
    return skipped


def describe(record: RemovalRecord) -> list[dict[str, Any]]:
    """Item locations of a record without their text (``removed`` listing)."""
    shown = []
    for item in record.items:
        where = {"store": item["store"]}
        for name in ("layer", "key", "path"):
            if name in item:
                where[name] = item[name]
        if "row" in item:
            where["turn_id"] = item["row"].get("turn_id")
        shown.append(where)
    return shown
