"""ADR 0022 phase 0 (#458): the weekly summary reads taint.observed rows only."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from stiracore.control import taint_cli
from stiracore.observability.event_store import SQLiteEventStore
from stiracore.observability.hooks import ObservabilityEvent
from stiracore.observability.taint_api import clean_summary


def _row(trust="untrusted", recall=False, held=None, sensitive=False):
    return {
        "context_trust": trust,
        "context_sensitive": sensitive,
        "input_labels": {"operator": 1, "memory": 1} if recall else {"operator": 1},
        "untrusted_recall_only": recall,
        "frame_version": 0,
        "egress_calls": 2,
        "flow_would_hold": held or {},
    }


def test_summary_counts_turns_recall_only_and_would_hold(tmp_path, capsys):
    store = SQLiteEventStore(tmp_path / "events.db")
    base = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
    try:
        for when, attributes in (
            (base, _row(recall=True)),
            (base.replace(hour=10), _row(held={"untrusted_send": 2, "bogus": 5})),
            (base.replace(day=21), _row(trust="trusted", sensitive=True)),
            (base.replace(day=1), _row()),  # outside the 7 days
        ):
            store.emit(
                ObservabilityEvent(
                    event_type="taint.observed", timestamp=when, attributes=attributes
                )
            )
        store.emit(ObservabilityEvent(event_type="tool.completed", timestamp=base))
    finally:
        store.close()
    argv = ["--data-dir", str(tmp_path), "summary", "--now", "2026-10-22T00:00:00+00:00"]
    assert taint_cli.main(argv) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["totals"] == {
        "egress_calls": 6,
        "sensitive": 1,
        "turns": 3,
        "untrusted": 2,
        "untrusted_recall_only": 1,
        "would_hold": 2,
    }
    assert out["flow_would_hold"] == {"untrusted_send": 2}
    assert out["turns_with_source"] == {"memory": 1, "operator": 3}


def test_missing_database_is_exit_1(tmp_path, capsys):
    assert taint_cli.main(["--data-dir", str(tmp_path), "summary"]) == 1


def test_clean_summary_refuses_odd_rows():
    assert clean_summary({"context_trust": "maybe"}) is None
    assert clean_summary("x") is None
    cleaned = clean_summary({**_row(), "input_labels": {"operator": 1, "url": "x"}})
    assert cleaned["input_labels"] == {"operator": 1}
