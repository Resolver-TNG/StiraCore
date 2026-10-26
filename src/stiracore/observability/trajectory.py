"""Pure trajectory metrics over NeuralState time series (#171, ADR 0009 §7).

Rows whose timestamp is not a finite number within ±1e12 s, or whose state values are
not finite numbers in ``[0, 1]``, are dropped, so every result is finite.

Input rows have the shape :class:`NeuralHistory` records: ``{"timestamp": float,
"state": {dim: float in [0, 1]}, "stimulus": str | None}``, optionally with a recorded
``"breaker"`` level. Nothing here reads a clock, a store or the engine.

The metrics are defined by the formulas below and nothing more. #165 has no definition
for them, so they carry no interpretation (e.g. "exhaustion") and no thresholds of
their own: every threshold is a caller argument and is echoed in the result.

* **Total variation** of dimension *d*: ``TV_d = Σ_i |x_d(i+1) − x_d(i)|`` over samples
  in time order; ``total = Σ_d TV_d``.
* **Recovery episode** of dimension *d* for ``below`` / ``recover_at`` (default
  ``recover_at = below``): starts at the first sample with ``x_d < below`` (or at the
  first sample, marked ``censored_start``), ends at the first later sample with
  ``x_d >= recover_at``. ``duration_sec`` is the timestamp difference; an episode with
  no such sample is ``recovered: False`` with ``end``/``duration_sec`` = ``None``.
  This is a threshold crossing of the *state*, not the breaker level (the breaker also
  has cooldown turns); recorded breaker levels are reported separately.
* **Saturation** of dimension *d* for ``eps``: fraction of samples with ``x_d <= eps``
  (``low``) and with ``x_d >= 1 − eps`` (``high``); ``total = low + high``. Samples are
  counted, not weighted by time.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Sequence
from itertools import pairwise
from typing import Any

from stiracore.core.neural_state import DIMS

__all__ = [
    "breaker_transitions",
    "export_trajectory",
    "normalize_samples",
    "recovery_episodes",
    "saturation",
    "total_variation",
]


# Timestamps beyond this (about year 33658) are not real clock values; bounding them keeps
# every difference finite (1e308 - (-1e308) would be inf).
_TIMESTAMP_LIMIT = 1e12


def _finite(value: Any) -> bool:
    """A real int/float (not bool) that converts to a finite float (10**1000 does not)."""
    if type(value) not in (int, float):
        return False
    try:
        return math.isfinite(float(value))
    except OverflowError:
        return False


def _timestamp(value: Any) -> bool:
    return _finite(value) and abs(value) <= _TIMESTAMP_LIMIT


def normalize_samples(rows: Iterable[Any]) -> list[dict[str, Any]]:
    """Valid rows only (finite timestamp, all six dimensions in ``[0, 1]``), time order.

    Rows that fail validation are dropped, as ``history_payload`` does; the sort is
    stable, so rows with equal timestamps keep their recorded order.
    """
    samples = []
    for row in rows:
        if not isinstance(row, dict) or not _timestamp(row.get("timestamp")):
            continue
        state = row.get("state")
        if not isinstance(state, dict) or not all(
            _finite(state.get(dim)) and 0 <= state[dim] <= 1 for dim in DIMS
        ):
            continue
        stimulus = row.get("stimulus")
        breaker = row.get("breaker")
        samples.append(
            {
                "timestamp": float(row["timestamp"]),
                "state": {dim: float(state[dim]) for dim in DIMS},
                "stimulus": stimulus if isinstance(stimulus, str) else None,
                "breaker": breaker if isinstance(breaker, str) else None,
            }
        )
    samples.sort(key=lambda sample: sample["timestamp"])
    return samples


def _dims(dims: Sequence[str] | None) -> tuple[str, ...]:
    chosen = tuple(DIMS if dims is None else dims)
    unknown = [dim for dim in chosen if dim not in DIMS]
    if unknown or not chosen:
        raise ValueError(f"unknown dimensions: {unknown}")
    return chosen


def total_variation(rows: Iterable[Any], dims: Sequence[str] | None = None) -> dict[str, Any]:
    samples = normalize_samples(rows)
    chosen = _dims(dims)
    per_dim = dict.fromkeys(chosen, 0.0)
    for before, after in pairwise(samples):
        for dim in chosen:
            per_dim[dim] += abs(after["state"][dim] - before["state"][dim])
    return {
        "per_dim": per_dim,
        "total": sum(per_dim.values()),
        "samples": len(samples),
        "duration_sec": samples[-1]["timestamp"] - samples[0]["timestamp"] if samples else 0.0,
    }


def recovery_episodes(
    rows: Iterable[Any],
    *,
    below: float,
    dim: str = "stability",
    recover_at: float | None = None,
) -> dict[str, Any]:
    _dims([dim])
    recover = below if recover_at is None else recover_at
    if not (_finite(below) and _finite(recover)) or recover < below:
        raise ValueError("below/recover_at must be finite and recover_at >= below.")
    samples = normalize_samples(rows)
    episodes: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    for index, sample in enumerate(samples):
        value = sample["state"][dim]
        if current is None:
            if value < below:
                current = {
                    "start": sample["timestamp"],
                    "end": None,
                    "duration_sec": None,
                    "samples": 1,
                    "min": value,
                    "recovered": False,
                    "censored_start": index == 0,
                }
            continue
        if value >= recover:
            current.update(
                end=sample["timestamp"],
                duration_sec=sample["timestamp"] - current["start"],
                recovered=True,
            )
            episodes.append(current)
            current = None
            continue
        current["samples"] += 1
        current["min"] = min(current["min"], value)
    if current is not None:
        episodes.append(current)
    return {"dim": dim, "below": below, "recover_at": recover, "episodes": episodes}


def saturation(
    rows: Iterable[Any], *, eps: float = 0.02, dims: Sequence[str] | None = None
) -> dict[str, Any]:
    if not _finite(eps) or not 0 <= eps < 0.5:
        raise ValueError("eps must be in [0, 0.5).")
    samples = normalize_samples(rows)
    chosen = _dims(dims)
    count = len(samples)
    per_dim = {}
    for dim in chosen:
        low = sum(1 for sample in samples if sample["state"][dim] <= eps)
        high = sum(1 for sample in samples if sample["state"][dim] >= 1 - eps)
        per_dim[dim] = {
            "low": low / count if count else 0.0,
            "high": high / count if count else 0.0,
            "total": (low + high) / count if count else 0.0,
        }
    return {"eps": eps, "samples": count, "per_dim": per_dim}


def breaker_transitions(rows: Iterable[Any]) -> list[dict[str, Any]]:
    """Changes of the *recorded* breaker level (rows without one are skipped)."""
    transitions = []
    previous = None
    for sample in normalize_samples(rows):
        level = sample["breaker"]
        if level is None:
            continue
        if previous is not None and level != previous:
            transitions.append({"timestamp": sample["timestamp"], "from": previous, "to": level})
        previous = level
    return transitions


def export_trajectory(rows: Iterable[Any]) -> dict[str, Any]:
    """NeuralState series + stimuli + recorded breaker transitions, as plain data."""
    samples = normalize_samples(rows)
    return {
        "dimensions": list(DIMS),
        "samples": [{"timestamp": s["timestamp"], "state": s["state"]} for s in samples],
        "stimuli": [
            {"timestamp": s["timestamp"], "stimulus": s["stimulus"]}
            for s in samples
            if s["stimulus"] is not None
        ],
        "breaker_transitions": breaker_transitions(samples),
    }
