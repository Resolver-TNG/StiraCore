"""#165: the saturation report is generated from real engine runs and stays in sync."""

from __future__ import annotations

from pathlib import Path

import pytest

from stiracore.core.neural_state import BASELINES, DECAY_RATES, EMOTION_MATRIX

report = pytest.importorskip("scripts.saturation_report")

DOC = Path(__file__).resolve().parents[1] / "docs" / "research" / "neural-saturation-2026-10-16.md"


DOC_SKIP = "docs/research/ is not in this checkout (public release snapshot, release-exclude.txt)"


def _committed_report() -> str:
    """The committed report; skip (not fail) only where docs/research/ is absent (snapshot)."""
    if not DOC.is_file():
        pytest.skip(DOC_SKIP)
    return DOC.read_text(encoding="utf-8")


def test_committed_report_matches_the_generator():
    assert _committed_report() == report.render_report()


def test_report_generation_is_deterministic_and_leaves_the_engine_constants_alone():
    matrix, rates, baselines = dict(EMOTION_MATRIX), dict(DECAY_RATES), dict(BASELINES)
    assert report.render_report() == report.render_report()
    assert (dict(EMOTION_MATRIX), dict(DECAY_RATES), dict(BASELINES)) == (
        matrix,
        rates,
        baselines,
    )


def test_heartbeat_pacing_does_not_change_the_idle_recovery_curve():
    # Same stability at the same elapsed time for 5-minute and 3-hour ticks (linear decay);
    # only the tick count and the (quantised) moment a level change is observed differ.
    fast = report._idle_with_heartbeat(report._conversation(report.HOSTILE, 240, 60.0), 300, 300)
    slow = report._idle_with_heartbeat(
        report._conversation(report.HOSTILE, 240, 60.0), 10800, 10800
    )
    for hours in (24, 72, 240):
        assert fast["at"][hours] == pytest.approx(slow["at"][hours], abs=1e-9)
    assert fast["ticks"] > slow["ticks"]
    assert abs(fast["first"]["normal"] - slow["first"]["normal"]) <= 10800


def test_long_hostile_conversation_saturates_and_pins_attachment():
    run = report._conversation(report.HOSTILE, 120, 60.0)
    assert run.engine.state.stability < 0.05
    assert run.engine.state.attachment == 1.0
    assert run.attachment_lost > 0
