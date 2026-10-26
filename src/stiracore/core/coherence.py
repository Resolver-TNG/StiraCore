"""Behavioral coherence score (``bc_score``) — the reference calculation (#168).

The score measures one thing: how much ``stability`` has *moved* over the recent
window of attempted turns. It is not a measure of the identity itself, and it does
not describe or encode the identity anchors.

Definition (``docs/coherence.md``):

- input: the stability samples of the window, one per ``StiraEngine.pre_turn``
  (taken after decay, before the turn's stimulus), at most ``bc_window_size``;
- fewer than ``BC_MIN_SAMPLES`` samples: unmeasured, ``BC_UNMEASURED`` (1.0);
- otherwise ``round(max(0, 1 - BC_STDDEV_SCALE * sqrt(pvariance(window))), 4)``
  with the population variance over the samples present (no padding).

``NeuralState.update_bc_window`` and ``NeuralState.load`` compute the same value;
``tests/test_coherence_score.py`` keeps the three in step.
"""

from __future__ import annotations

import math
from collections.abc import Iterable

BC_MIN_SAMPLES = 5
"""A window with fewer samples is unmeasured and scores ``BC_UNMEASURED``."""

BC_UNMEASURED = 1.0
"""The score of an unmeasured window (and of a fresh ``NeuralState``)."""

BC_STDDEV_SCALE = 5.0
"""A population standard deviation of ``1 / BC_STDDEV_SCALE`` (0.2) or more scores 0."""

BC_DECIMALS = 4
"""The score is rounded to this many decimal places."""


def _sample(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(  # noqa: TRY004 - one ValueError contract, as NeuralState
            "bc window samples must be numbers in [0.0, 1.0]"
        )
    number = float(value)
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        raise ValueError("bc window samples must be finite numbers in [0.0, 1.0]")
    return number


def compute_bc_score(window: Iterable[float]) -> float:
    """Return the coherence score of a window of stability samples.

    Pure: reads only ``window`` (any iterable of numbers in [0.0, 1.0], oldest
    first; the order does not change the result) and returns a value in
    [0.0, 1.0] rounded to ``BC_DECIMALS`` places. The caller owns the window:
    pass the most recent ``bc_window_size`` samples. Raises ``ValueError`` for a
    sample that is not a finite number in [0.0, 1.0] (bool included).
    """
    samples = [_sample(value) for value in window]
    if len(samples) < BC_MIN_SAMPLES:
        return BC_UNMEASURED
    mean = sum(samples) / len(samples)
    variance = sum((x - mean) ** 2 for x in samples) / len(samples)
    return round(max(0.0, 1.0 - math.sqrt(variance) * BC_STDDEV_SCALE), BC_DECIMALS)


def bc_is_measured(sample_count: int) -> bool:
    """Whether ``sample_count`` stability samples are enough to measure (#337).

    A score of 1.0 means two different things: "fewer than ``BC_MIN_SAMPLES`` samples,
    not measured yet" and "measured, no movement". This tells them apart for display; it
    reads nothing and changes nothing. Raises ``ValueError`` unless ``sample_count`` is a
    nonnegative ``int`` (bool excluded).
    """
    if type(sample_count) is not int or sample_count < 0:
        raise ValueError("sample_count must be a nonnegative integer")
    return sample_count >= BC_MIN_SAMPLES


def bc_measurement(sample_count: int) -> dict[str, int | bool]:
    """The display fields every status view adds next to ``bc_score`` (#337).

    ``bc_samples`` (stability samples the score rests on), ``bc_measured`` (enough of
    them) and ``bc_min_samples`` (the minimum). Additive: ``bc_score`` itself is unchanged.
    """
    return {
        "bc_samples": sample_count,
        "bc_measured": bc_is_measured(sample_count),
        "bc_min_samples": BC_MIN_SAMPLES,
    }
