"""Write one recorded turn as an ATIF trajectory file, codes only (#700).

    python -m stiracore.control.atif_export_cli --data-dir DIR --out FILE
        [--turn-id ID] [--include-untrusted]

Reads the turn events of one turn from ``<data dir>/turns.db`` (from a copy in a private
temporary directory, removed afterwards: the database is not opened in place) and writes
the trajectory of :mod:`stiracore.observability.atif_export` to ``FILE``. Without
``--turn-id`` the last turn that has ended is taken.

Off unless an operator runs it: no agent tool runs it, no setting turns it on, and the
server never calls it. It calls no model, sends nothing over the network and writes
nothing into the data directory: ``FILE`` must not exist yet and must be outside the data
directory (created with mode 0600). The file holds ids, numbers and codes only — no
prompt, reply, tool argument or tool result. A turn whose recorded taint label is
``untrusted`` is refused unless ``--include-untrusted`` is given.

Exit codes: 0 written, 1 not written (a reason code on standard error), 2 usage error.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

from stiracore.observability import atif_export

TURNS_DB = "turns.db"
# Why nothing was written, next to ``atif_export.REFUSALS``.
PROBLEMS = frozenset(
    {
        "data_dir_missing",
        "turns_db_missing",
        "turns_db_unreadable",
        "turn_not_found",
        "out_exists",
        "out_inside_data_dir",
        "out_unwritable",
    }
)


def read_turn(database: Path, turn_id: str | None) -> list[dict]:
    """The events of ``turn_id`` (default: the last turn with a ``turn.completed``).

    As ``failure_digest_cli.read_tool_rows``: the file and its ``-wal``, if any, are copied
    to a private temporary directory and the copy is opened read only. Raises ``OSError``
    (the copy) or ``sqlite3.Error`` (the read)."""
    with tempfile.TemporaryDirectory(prefix="stira-atif-export-") as scratch:
        copy = Path(scratch) / TURNS_DB
        shutil.copyfile(database, copy)
        wal = database.with_name(database.name + "-wal")
        if wal.is_file():
            shutil.copyfile(wal, copy.with_name(copy.name + "-wal"))
        connection = sqlite3.connect(f"{copy.resolve().as_uri()}?mode=ro", uri=True)
        try:
            if turn_id is None:
                row = connection.execute(
                    "SELECT turn_id FROM ui_events WHERE type = 'turn.completed'"
                    " ORDER BY id DESC LIMIT 1"
                ).fetchone()
                if row is None:
                    return []
                turn_id = row[0]
            rows = connection.execute(
                "SELECT payload FROM ui_events WHERE turn_id = ? ORDER BY seq", (turn_id,)
            ).fetchall()
        finally:
            connection.close()
    events = []
    for (raw,) in rows:
        try:
            events.append(json.loads(raw))
        except (TypeError, ValueError):
            events.append(None)  # counted as skipped by the adapter
    return events


def _inside(path: Path, directory: Path) -> bool:
    try:
        path.resolve().relative_to(directory.resolve())
    except ValueError:
        return False
    return True


def _write_new(path: Path, text: str) -> None:
    """Create ``path`` (it must not exist; mode 0600) and write ``text``."""
    descriptor = os.open(
        path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600
    )
    with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
        handle.write(text)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="One turn as an ATIF trajectory file (#700)")
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True, help="a new file outside the data dir")
    parser.add_argument("--turn-id", default=None, help="default: the last turn that has ended")
    parser.add_argument(
        "--include-untrusted",
        action="store_true",
        help="also export a turn whose recorded taint label is untrusted (codes only)",
    )
    return parser


def _fail(code: str) -> int:
    print(f"error: {code}", file=sys.stderr)
    return 1


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.data_dir.is_dir():
        return _fail("data_dir_missing")
    if _inside(args.out, args.data_dir):
        return _fail("out_inside_data_dir")
    if args.out.exists() or args.out.is_symlink():
        return _fail("out_exists")
    database = args.data_dir / TURNS_DB
    if not database.is_file():
        return _fail("turns_db_missing")
    try:
        events = read_turn(database, args.turn_id)
    except (OSError, sqlite3.Error):
        return _fail("turns_db_unreadable")
    if not events:
        return _fail("turn_not_found")
    try:
        document = atif_export.build(events, include_untrusted=args.include_untrusted)
    except atif_export.ExportRefused as refused:
        return _fail(refused.code)
    text = json.dumps(document, indent=2, ensure_ascii=True, allow_nan=False) + "\n"
    try:
        _write_new(args.out, text)
    except FileExistsError:
        return _fail("out_exists")
    except OSError:
        return _fail("out_unwritable")
    print(f"wrote {len(document['steps'])} step(s) of turn {document['session_id']}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
