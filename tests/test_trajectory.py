"""#171: pure trajectory metrics (formulas in ADR 0009 §7)."""

from __future__ import annotations

import pytest

from stiracore.core.neural_state import DIMS
from stiracore.observability.trajectory import (
    breaker_transitions,
    export_trajectory,
    normalize_samples,
    recovery_episodes,
    saturation,
    total_variation,
)

BASE = dict.fromkeys(DIMS, 0.5)


def _row(ts, stimulus=None, breaker=None, **values):
    row = {"timestamp": ts, "state": {**BASE, **values}, "stimulus": stimulus}
    if breaker is not None:
        row["breaker"] = breaker
    return row


def test_invalid_rows_are_dropped_and_rows_sorted():
    rows = [
        _row(3.0, stability=0.4),
        {"timestamp": float("nan"), "state": BASE},
        {"timestamp": 1.0, "state": {**BASE, "trust": 1.5}},
        {"timestamp": 1.0, "state": {"trust": 0.5}},
        "garbage",
        _row(1.0),
    ]
    samples = normalize_samples(rows)
    assert [s["timestamp"] for s in samples] == [1.0, 3.0]


def test_total_variation_sums_absolute_steps():
    rows = [_row(0, stability=0.7), _row(10, stability=0.4), _row(20, stability=0.6)]
    result = total_variation(rows)
    assert result["per_dim"]["stability"] == pytest.approx(0.5)
    assert result["per_dim"]["trust"] == 0.0
    assert result["total"] == pytest.approx(0.5)
    assert (result["samples"], result["duration_sec"]) == (3, 20.0)
    assert total_variation([])["total"] == 0.0
    with pytest.raises(ValueError):
        total_variation(rows, dims=["mood"])


def test_recovery_episodes_with_hysteresis_and_censoring():
    rows = [
        _row(0, stability=0.45),  # starts below: censored
        _row(5, stability=0.55),  # >= below but < recover_at: not recovered
        _row(9, stability=0.65),  # recovered
        _row(20, stability=0.7),
        _row(30, stability=0.2),
        _row(40, stability=0.3),  # still below at the end
    ]
    result = recovery_episodes(rows, below=0.5, recover_at=0.6)
    assert result["below"] == 0.5 and result["recover_at"] == 0.6
    first, second = result["episodes"]
    assert first == {
        "start": 0.0,
        "end": 9.0,
        "duration_sec": 9.0,
        "samples": 2,
        "min": 0.45,
        "recovered": True,
        "censored_start": True,
    }
    assert second == {
        "start": 30.0,
        "end": None,
        "duration_sec": None,
        "samples": 2,
        "min": 0.2,
        "recovered": False,
        "censored_start": False,
    }
    # Without hysteresis 0.55 already ends the first episode.
    assert recovery_episodes(rows, below=0.5)["episodes"][0]["end"] == 5.0
    with pytest.raises(ValueError):
        recovery_episodes(rows, below=0.5, recover_at=0.4)


def test_saturation_counts_samples_at_the_bounds():
    rows = [
        _row(0, attachment=1.0, stability=0.01),
        _row(1, attachment=0.99, stability=0.5),
        _row(2, attachment=0.5, stability=0.02),
        _row(3, attachment=0.97, stability=0.03),
    ]
    result = saturation(rows, eps=0.02)
    assert result["eps"] == 0.02 and result["samples"] == 4
    assert result["per_dim"]["attachment"] == {"low": 0.0, "high": 0.5, "total": 0.5}
    assert result["per_dim"]["stability"] == {"low": 0.5, "high": 0.0, "total": 0.5}
    assert saturation([], eps=0.02)["per_dim"]["trust"]["total"] == 0.0
    with pytest.raises(ValueError):
        saturation(rows, eps=0.5)


def test_export_contains_series_stimuli_and_recorded_breaker_transitions():
    rows = [
        _row(0, breaker="NORMAL"),
        _row(1, stimulus="hostile", breaker="NORMAL", stability=0.45),
        _row(2, breaker="WARNING", stability=0.4),
        _row(3, stimulus="praise"),  # no recorded level: skipped, not inferred
        _row(4, breaker="NORMAL", stability=0.65),
    ]
    exported = export_trajectory(rows)
    assert exported["dimensions"] == list(DIMS)
    assert len(exported["samples"]) == 5
    assert exported["stimuli"] == [
        {"timestamp": 1.0, "stimulus": "hostile"},
        {"timestamp": 3.0, "stimulus": "praise"},
    ]
    assert exported["breaker_transitions"] == [
        {"timestamp": 2.0, "from": "NORMAL", "to": "WARNING"},
        {"timestamp": 4.0, "from": "WARNING", "to": "NORMAL"},
    ]
    assert breaker_transitions([_row(0), _row(1)]) == []


def test_neural_history_rows_are_accepted_as_recorded():
    from stiracore.core.neural_state import NeuralState
    from stiracore.observability.neural_history import NeuralHistory

    history = NeuralHistory()
    state = NeuralState()
    history.record(state, stimulus="neutral")
    history.record(state)
    exported = export_trajectory(history.snapshot())
    assert len(exported["samples"]) == 2
    assert exported["stimuli"][0]["stimulus"] == "neutral"


@pytest.mark.parametrize(
    "bad",
    [10**1000, 1e308, -1e308, float("inf"), True],
    ids=["huge-int", "1e308", "-1e308", "inf", "bool"],
)
def test_extreme_timestamps_are_dropped_and_outputs_stay_finite(bad):
    import math

    rows = [_row(0, stability=0.4), {**_row(1), "timestamp": bad}, _row(2, stability=0.7)]
    assert [s["timestamp"] for s in normalize_samples(rows)] == [0.0, 2.0]
    result = total_variation(rows)
    assert math.isfinite(result["total"]) and math.isfinite(result["duration_sec"])
    (episode,) = recovery_episodes(rows, below=0.5)["episodes"]
    assert episode["duration_sec"] == 2.0


def test_extreme_state_values_and_thresholds_are_rejected_cleanly():
    rows = [_row(0), {"timestamp": 1.0, "state": {**BASE, "trust": 10**1000}}]
    assert len(normalize_samples(rows)) == 1
    with pytest.raises(ValueError):
        recovery_episodes(rows, below=10**1000)
    with pytest.raises(ValueError):
        saturation(rows, eps=10**1000)
