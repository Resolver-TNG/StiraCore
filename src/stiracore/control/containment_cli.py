"""Weekly summary of the containment observation (ADR 0023 phase 0, #460-0).

    python -m stiracore.control.containment_cli --data-dir DIR summary [--days 7]
        [--windows 300,600,1800] [--now ISO]

Reads the ``containment.observed`` rows of ``<data dir>/events.db`` (read only: the
database is opened with ``mode=ro`` and nothing is written) and prints JSON: counts by day,
signal and code, the highest ``would_be`` the live window reached, and a **what-if** replay
of the same codes with other windows — the numbers the commander asked for before the
weights, thresholds and window are adjusted for phase 1 (ADR 0023 Q3).

Codes and numbers only (the rows hold nothing else). An operator command: no agent tool
runs it. Calls no model. Exit codes: 0 done, 1 the database cannot be read, 2 usage error.
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

from stiracore.core import containment

_RANK = {stage.value: stage.rank for stage in containment.Stage}


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
    """``(timestamp, attributes)`` of the ``containment.observed`` rows in ``[since, until)``."""
    uri = f"{database.resolve().as_uri()}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    try:
        rows = connection.execute(
            "SELECT timestamp, attributes_json FROM events "
            "WHERE event_type = 'containment.observed' ORDER BY seq"
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
        if isinstance(attributes, dict) and attributes.get("code") in containment.CODE_SIGNALS:
            out.append((at, attributes))
    return out


def summarize(
    rows: Sequence[tuple[float, dict]], *, since: float, until: float, windows: Sequence[int]
) -> dict[str, Any]:
    by_day: dict[str, Counter] = {}
    peak_by_day: dict[str, str] = {}
    signals: Counter = Counter()
    codes: Counter = Counter()
    peak = "open"
    for at, row in rows:
        day = datetime.fromtimestamp(at, UTC).date().isoformat()
        by_day.setdefault(day, Counter())[row["code"]] += 1
        signals[containment.CODE_SIGNALS[row["code"]]] += 1
        codes[row["code"]] += 1
        stage = row.get("would_be")
        if stage in _RANK:
            if _RANK[stage] > _RANK[peak]:
                peak = stage
            if _RANK[stage] > _RANK.get(peak_by_day.get(day, "open"), 0):
                peak_by_day[day] = stage
    what_if = {}
    pairs = [(at, row["code"]) for at, row in rows]
    for window in windows:
        replayed = containment.replay(pairs, window_seconds=window)
        reached = Counter(row["would_be"] for row in replayed)
        top = max((row["would_be"] for row in replayed), key=_RANK.get, default="open")
        what_if[str(window)] = {
            "peak_would_be": top,
            "rows_by_would_be": dict(sorted(reached.items())),
        }
    return {
        "since": datetime.fromtimestamp(since, UTC).isoformat(),
        "until": datetime.fromtimestamp(until, UTC).isoformat(),
        "rows": len(rows),
        "signals": {name: signals.get(name, 0) for name in containment.SIGNAL_WEIGHTS},
        "codes": dict(sorted(codes.items())),
        "peak_would_be": peak,
        "days": {
            day: {
                "codes": dict(sorted(counts.items())),
                "peak_would_be": peak_by_day.get(day, "open"),
            }
            for day, counts in sorted(by_day.items())
        },
        "what_if": what_if,
        "tables": containment.describe(),
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Containment observation summary (#460-0)")
    parser.add_argument("--data-dir", type=Path, required=True)
    commands = parser.add_subparsers(dest="command", required=True)
    summary = commands.add_parser("summary", help="counts of the last days (default 7)")
    summary.add_argument("--days", type=int, default=7)
    summary.add_argument("--windows", default="300,600,1800", help="what-if windows (seconds)")
    summary.add_argument("--now", default=None, help="end of the period (ISO 8601; default now)")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not 1 <= args.days <= 366:
        parser.error("--days must be from 1 to 366")
    try:
        windows = [int(part) for part in args.windows.split(",") if part.strip()]
    except ValueError:
        parser.error("--windows must be integers (seconds)")
    if any(not containment.WINDOW_MIN <= w <= containment.WINDOW_MAX for w in windows):
        parser.error(
            f"--windows must be from {containment.WINDOW_MIN} to {containment.WINDOW_MAX} s"
        )
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
    print(json.dumps(summarize(rows, since=since, until=until, windows=windows), indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
