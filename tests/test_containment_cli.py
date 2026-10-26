"""ADR 0023 phase 0 (#460-0): the weekly summary CLI reads containment.observed rows only."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from stiracore.control import containment_cli
from stiracore.observability.event_store import SQLiteEventStore
from stiracore.observability.hooks import ObservabilityEvent


def _emit(store, when, code, would_be="restricted", score=2.0):
    store.emit(
        ObservabilityEvent(
            event_type="containment.observed",
            timestamp=when,
            attributes={
                "signal": "G1",
                "code": code,
                "weight": 2.0,
                "score": score,
                "would_be": would_be,
                "window_seconds": 600,
                "phase": 0,
            },
        )
    )


def test_summary_counts_by_day_signal_and_code_and_replays_windows(tmp_path, capsys):
    store = SQLiteEventStore(tmp_path / "events.db")
    try:
        base = datetime(2026, 10, 20, 9, 0, tzinfo=UTC)
        _emit(store, base, "E_READ_SECRET_PATH")
        _emit(store, base.replace(minute=5), "write_floor", "closed", 4.0)
        _emit(store, base.replace(day=21, minute=0), "text_cue", "open", 0.0)
        _emit(store, base.replace(day=1), "E_READ_SECRET_PATH")  # outside the 7 days
        store.emit(ObservabilityEvent(event_type="tool.completed", timestamp=base))
    finally:
        store.close()
    before = (tmp_path / "events.db").read_bytes()
    code = containment_cli.main(
        [
            "--data-dir",
            str(tmp_path),
            "summary",
            "--now",
            "2026-10-22T00:00:00+00:00",
            "--windows",
            "60,600",
        ]
    )
    assert code == 0
    out = json.loads(capsys.readouterr().out)
    assert out["rows"] == 3
    assert out["codes"] == {"E_READ_SECRET_PATH": 1, "text_cue": 1, "write_floor": 1}
    assert out["signals"]["G1"] == 1 and out["signals"]["G2"] == 1 and out["signals"]["T"] == 1
    assert out["peak_would_be"] == "closed"
    assert out["days"]["2026-10-20"]["peak_would_be"] == "closed"
    assert out["days"]["2026-10-21"] == {"codes": {"text_cue": 1}, "peak_would_be": "open"}
    # with a 60 s window the two signals 5 minutes apart never add up
    assert out["what_if"]["60"]["peak_would_be"] == "restricted"
    assert out["what_if"]["600"]["peak_would_be"] == "restricted"  # 2 + 2 = 4 < 5
    assert (tmp_path / "events.db").read_bytes() == before  # read only


def test_missing_database_and_usage_errors(tmp_path, capsys):
    assert containment_cli.main(["--data-dir", str(tmp_path), "summary"]) == 1
    assert "not found" in capsys.readouterr().out
    import pytest

    for argv in (["--days", "0"], ["--windows", "5"], ["--windows", "x"], ["--now", "nope"]):
        with pytest.raises(SystemExit) as exit_info:
            containment_cli.main(["--data-dir", str(tmp_path), "summary", *argv])
        assert exit_info.value.code == 2
