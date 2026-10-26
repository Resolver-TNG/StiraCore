"""Pure heartbeat pacing calculations."""

from __future__ import annotations


def compute_interval(stability: float, floor_sec: float, ceil_sec: float) -> float:
    """Return a stability-weighted interval clamped to the configured bounds."""
    floor = float(floor_sec)
    ceil = float(ceil_sec)
    if floor > ceil:
        floor, ceil = ceil, floor

    bounded_stability = max(0.0, min(1.0, float(stability)))
    interval = ceil * bounded_stability**2
    return max(floor, min(ceil, interval))
