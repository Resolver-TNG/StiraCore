"""Measure the per-event cost of the #171 hash chain (ADR 0009 §6).

Offline; writes only to a temporary directory. Compares, on the same machine and file
system:

* ``legacy``  - the pre-#171 ``emit`` (same pragmas, one INSERT per transaction);
* ``chained`` - ``SQLiteEventStore.emit`` (masking, BEGIN IMMEDIATE, tail + head check,
  INSERT + read back + hash UPDATE, atomic head write with fsync + directory fsync);
* ``chained_without_head_file`` - the same file store with the head write/check
  disabled, only to separate the cost of the head file (not a supported mode);
* ``verify_chain`` / migration of a legacy DB, divided by the number of rows.

Usage: ``PYTHONPATH=src:. python scripts/eventstore_chain_bench.py [N]``
"""

from __future__ import annotations

import json
import platform
import sqlite3
import statistics
import sys
import tempfile
import time
from pathlib import Path

from stiracore.observability.event_store import SQLiteEventStore, _json_dumps, event_to_dict
from stiracore.observability.hooks import ObservabilityEvent

_SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL UNIQUE,
    timestamp TEXT NOT NULL, event_type TEXT NOT NULL, turn_id TEXT, duration_ms REAL,
    model_id TEXT, tool_name TEXT, tool_use_id TEXT, status TEXT, usage_json TEXT,
    message TEXT, attributes_json TEXT
);
CREATE INDEX IF NOT EXISTS events_turn_seq ON events (turn_id, seq);
CREATE INDEX IF NOT EXISTS events_type_seq ON events (event_type, seq);
"""
_INSERT = (
    "INSERT INTO events (event_id, timestamp, event_type, turn_id, duration_ms, model_id,"
    " tool_name, tool_use_id, status, usage_json, message, attributes_json)"
    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(event_id) DO NOTHING"
)


def _event(n: int) -> ObservabilityEvent:
    return ObservabilityEvent(
        event_type="tool_end",
        turn_id=f"turn-{n // 10}",
        duration_ms=12.5,
        model_id="model-x",
        tool_name="memory_read",
        tool_use_id=f"tooluse_{n:08d}",
        status="success",
        usage={"inputTokens": 1200, "outputTokens": 80},
        attributes={"stop_reason": "tool_use", "retry": False, "n": n},
    )


def _legacy_values(event: ObservabilityEvent) -> tuple:
    d = event_to_dict(event)
    return (
        d["event_id"],
        d["timestamp"],
        d["event_type"],
        d["turn_id"],
        d["duration_ms"],
        d["model_id"],
        d["tool_name"],
        d["tool_use_id"],
        d["status"],
        _json_dumps(d["usage"]),
        d["message"],
        _json_dumps(d["attributes"]),
    )


def _legacy_connection(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), timeout=30.0, check_same_thread=False)
    with conn:
        conn.execute("PRAGMA busy_timeout = 30000")
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(_SCHEMA)
    return conn


def _summary(samples: list[float]) -> dict:
    ordered = sorted(samples)
    return {
        "mean_us": statistics.fmean(ordered) * 1e6,
        "median_us": statistics.median(ordered) * 1e6,
        "p95_us": ordered[int(len(ordered) * 0.95) - 1] * 1e6,
    }


def run(n: int) -> dict:
    events = [_event(i) for i in range(n)]
    results: dict = {"n": n}
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)

        conn = _legacy_connection(root / "legacy.db")
        samples = []
        for event in events:
            values = _legacy_values(event)
            start = time.perf_counter()
            with conn:
                conn.execute(_INSERT, values)
            samples.append(time.perf_counter() - start)
        conn.close()
        results["legacy"] = _summary(samples)

        store = SQLiteEventStore(root / "chained.db")
        samples = []
        for event in events:
            start = time.perf_counter()
            store.emit(event)
            samples.append(time.perf_counter() - start)
        results["chained"] = _summary(samples)

        start = time.perf_counter()
        ok, error = store.verify_chain()
        elapsed = time.perf_counter() - start
        assert ok, error
        results["verify_chain_us_per_event"] = elapsed / n * 1e6
        store.close()

        start = time.perf_counter()
        reopened = SQLiteEventStore(root / "chained.db")
        results["open_us_per_event"] = (time.perf_counter() - start) / n * 1e6
        assert reopened.healthy
        reopened.close()

        headless = SQLiteEventStore(root / "headless.db")
        headless._write_head_locked = lambda prev, nxt: None  # isolate the head cost
        headless._check_head_locked = lambda state: None
        samples = []
        for event in events:
            start = time.perf_counter()
            headless.emit(event)
            samples.append(time.perf_counter() - start)
        headless.close()
        results["chained_without_head_file"] = _summary(samples)

        conn = _legacy_connection(root / "migrate.db")
        with conn:
            conn.executemany(
                _INSERT.replace(" ON CONFLICT(event_id) DO NOTHING", ""),
                [_legacy_values(event) for event in events],
            )
        conn.close()
        start = time.perf_counter()
        migrated = SQLiteEventStore(root / "migrate.db")
        results["migration_us_per_row"] = (time.perf_counter() - start) / n * 1e6
        assert migrated.healthy
        migrated.close()

    results["platform"] = {
        "python": platform.python_version(),
        "sqlite": sqlite3.sqlite_version,
        "machine": platform.machine(),
        "system": platform.system(),
    }
    return results


if __name__ == "__main__":
    count = int(sys.argv[1]) if len(sys.argv) > 1 else 2000
    print(json.dumps(run(count), indent=2))
