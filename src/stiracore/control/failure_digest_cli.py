"""Failure digest of a data directory, printed once (#695).

    python -m stiracore.control.failure_digest_cli --data-dir DIR [--days 7] [--now ISO]
        [--format md|json] [--top 3] [--examples 3]

Reads the ``tool_call.updated`` rows of ``<data dir>/turns.db`` (from a copy in a private
temporary directory, removed afterwards: the database is not opened in place, so no
``-shm`` / ``-wal`` file appears) and the lifecycle log ``authority-lifecycle*.jsonl``
(read without rotation; nothing is appended), and prints the digest of
:mod:`stiracore.observability.failure_digest` as Markdown (or JSON): which failure
happened how often, what the largest patterns sum to, and representative event ids.

``turns.db`` holds masked previews next to the codes, so each row is reduced to the
allow-list ``failure_digest.TOOL_FIELDS`` as it is read; the output has codes, counts,
timestamps and ids only. An operator command: no agent tool runs it. Calls no model and
writes nothing into the data directory (and nothing to memory or a prompt). The copy is a
snapshot taken while a server may be writing: a copy torn by a concurrent checkpoint is
reported as ``turns_db_unreadable``. Counts are a lower bound (see the notes
in the output). Exit codes: 0 done, 1 the data directory or one of the sources cannot be
read (the digest of the other source is still printed), 2 usage error.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import tempfile
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

from stiracore.authority.lifecycle_events import LifecycleLog
from stiracore.observability import failure_digest

TURNS_DB = "turns.db"


def _seconds(value: str) -> float | None:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


def read_tool_rows(database: Path, since: float, until: float) -> list[dict]:
    """``failure_digest.TOOL_FIELDS`` of the ``tool_call.updated`` rows in ``[since, until)``.

    The database is never opened where it is (SQLite can create ``-shm`` / ``-wal`` files
    next to a WAL database even with ``mode=ro``): the file and its ``-wal``, if any, are
    copied to a private temporary directory, the copy is opened read only and removed.
    Raises ``OSError`` (the copy) or ``sqlite3.Error`` (the read)."""
    with tempfile.TemporaryDirectory(prefix="stira-failure-digest-") as scratch:
        copy = Path(scratch) / TURNS_DB
        shutil.copyfile(database, copy)
        wal = database.with_name(database.name + "-wal")
        if wal.is_file():
            shutil.copyfile(wal, copy.with_name(copy.name + "-wal"))
        connection = sqlite3.connect(f"{copy.resolve().as_uri()}?mode=ro", uri=True)
        try:
            rows = connection.execute(
                "SELECT payload FROM ui_events WHERE type = 'tool_call.updated'"
                " AND ts >= ? AND ts < ? ORDER BY id",
                (since, until),
            ).fetchall()
        finally:
            connection.close()
    out = []
    for (raw,) in rows:
        try:
            event = json.loads(raw)
        except (TypeError, ValueError):
            continue
        row = failure_digest.tool_row(event)
        if row is not None:
            out.append(row)
    return out


def read_lifecycle_rows(data_dir: Path) -> tuple[list[dict], str | None]:
    """The lifecycle events reduced to ``failure_digest.LIFECYCLE_FIELDS``, and the reason
    code when the log cannot be used (then no rows)."""
    try:
        log = LifecycleLog(data_dir, rotate=False)
    except OSError:
        return [], "lifecycle_log_unreadable"
    if log.unhealthy_reason is not None:
        return [], log.unhealthy_reason
    fields = failure_digest.LIFECYCLE_FIELDS
    return [{key: event.get(key) for key in fields} for event in log.events()], None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Failure digest of a data directory (#695)")
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--now", default=None, help="end of the period (ISO 8601; default now)")
    parser.add_argument("--format", choices=("md", "json"), default="md")
    parser.add_argument("--top", type=int, default=3)
    parser.add_argument("--examples", type=int, default=3)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not 1 <= args.days <= 366:
        parser.error("--days must be from 1 to 366")
    if not 1 <= args.top <= 100:
        parser.error("--top must be from 1 to 100")
    if not 0 <= args.examples <= 20:
        parser.error("--examples must be from 0 to 20")
    until = _seconds(args.now) if args.now else datetime.now(UTC).timestamp()
    if until is None:
        parser.error("--now must be an ISO 8601 time")
    since = until - timedelta(days=args.days).total_seconds()
    if since < 0:
        parser.error("--now is too early for --days")
    if not args.data_dir.is_dir():
        print("error: the data directory was not found", file=sys.stderr)
        return 1
    problems: list[str] = []
    tool_rows: list[dict] = []
    database = args.data_dir / TURNS_DB
    if not database.is_file():
        problems.append("turns_db_missing")
    else:
        try:
            tool_rows = read_tool_rows(database, since, until)
        except (OSError, sqlite3.Error):
            problems.append("turns_db_unreadable")
    lifecycle_rows, reason = read_lifecycle_rows(args.data_dir)
    if reason is not None:
        problems.append(reason)
    try:
        digest = failure_digest.build(
            tool_rows=tool_rows,
            lifecycle_rows=lifecycle_rows,
            since=since,
            until=until,
            source_problems=problems,
            top=args.top,
            examples=args.examples,
        )
    except ValueError:
        parser.error("--now is outside the supported range")
    if args.format == "json":
        print(json.dumps(digest, indent=2))
    else:
        print(failure_digest.to_markdown(digest), end="")
    return 1 if problems else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
