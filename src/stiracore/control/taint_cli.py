"""Weekly summary of the taint observation (ADR 0022 phase 0, #458).

    python -m stiracore.control.taint_cli --data-dir DIR summary [--days 7] [--now ISO]

Reads the ``taint.observed`` rows of ``<data dir>/events.db`` (opened ``mode=ro``; nothing
is written) and prints JSON: turns, how many contexts were untrusted / sensitive, how many
were untrusted **only** because of recalled memory (ADR 0022 Q3), counts per source, and
the egress calls a flow layer would have held per code — the numbers asked for before
phase 1 / 2. Codes and numbers only. An operator command; calls no model.
Exit codes: 0 done, 1 the database cannot be read, 2 usage error.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import Counter
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from stiracore.observability.taint_api import clean_summary


def _seconds(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        pass
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.timestamp()


def read_rows(database: Path, since: float, until: float) -> list[tuple[float, dict]]:
    uri = f"{database.resolve().as_uri()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        rows = connection.execute(
            "SELECT timestamp, attributes_json FROM events "
            "WHERE event_type = 'taint.observed' ORDER BY seq"
        ).fetchall()
    finally:
        connection.close()
    out = []
    for stamp, raw in rows:
        at = _seconds(stamp)
        if at is None or not since <= at < until:
            continue
        try:
            attributes = json.loads(raw) if raw else {}
        except ValueError:
            continue
        row = clean_summary(attributes)
        if row is not None:
            row["egress_calls"] = attributes.get("egress_calls")
            out.append((at, row))
    return out


def summarize(rows: Sequence[tuple[float, dict]], *, since: float, until: float) -> dict:
    by_day: dict[str, Counter] = {}
    sources: Counter = Counter()
    held: Counter = Counter()
    totals: Counter = Counter()
    for at, row in rows:
        day = datetime.fromtimestamp(at, UTC).date().isoformat()
        counts = by_day.setdefault(day, Counter())
        for bucket in (counts, totals):
            bucket["turns"] += 1
            bucket["untrusted"] += row["context_trust"] == "untrusted"
            bucket["sensitive"] += row["context_sensitive"]
            bucket["untrusted_recall_only"] += row["untrusted_recall_only"]
            bucket["would_hold"] += sum(row["flow_would_hold"].values())
        calls = row.get("egress_calls")
        if type(calls) is int and calls >= 0:
            totals["egress_calls"] += calls
        sources.update({k: 1 for k in row["input_labels"]})  # turns in which a source appeared
        held.update(row["flow_would_hold"])
    return {
        "since": datetime.fromtimestamp(since, UTC).isoformat(),
        "until": datetime.fromtimestamp(until, UTC).isoformat(),
        "totals": dict(sorted(totals.items())),
        "turns_with_source": dict(sorted(sources.items())),
        "flow_would_hold": dict(sorted(held.items())),
        "by_day": {day: dict(sorted(c.items())) for day, c in sorted(by_day.items())},
        "observe_only": True,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Taint observation summary (#458)")
    parser.add_argument("--data-dir", type=Path, required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    summary = commands.add_parser("summary", help="counts of the last days (default 7)")
    summary.add_argument("--days", type=int, default=7)
    summary.add_argument("--now", default=None, help="end of the period (ISO 8601; default now)")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not 1 <= args.days <= 366:
        parser.error("--days must be from 1 to 366")
    until = _seconds(args.now) if args.now else datetime.now(UTC).timestamp()
    if until is None:
        parser.error("--now must be an ISO 8601 time")
    since = until - timedelta(days=args.days).total_seconds()
    database = args.data_dir / "events.db"
    if not database.is_file():
        print(json.dumps({"error": "events.db not found in the data directory"}))
        return 1
    try:
        rows = read_rows(database, since, until)
    except sqlite3.Error as error:
        print(json.dumps({"error": f"events.db cannot be read ({type(error).__name__})"}))
        return 1
    print(json.dumps(summarize(rows, since=since, until=until), indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
