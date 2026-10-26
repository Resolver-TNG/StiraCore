"""Shared value checks for YOLO numbers and ids (one place, fail closed).

``bool`` is never a number here, ``NaN`` / ``±inf`` / negative values are refused, and
every check returns ``False`` for a value it does not understand — callers treat that
as "unknown", which is never a narrowing.
"""

from __future__ import annotations

import math
import re
from typing import Any

from .allowance import CAP_MAX

__all__ = ["cap_value", "finite_time", "hex_id", "non_negative_int"]

_HEX32 = re.compile(r"[0-9a-f]{32}")


def non_negative_int(value: Any, high: int | None = None) -> bool:
    return type(value) is int and value >= 0 and (high is None or value <= high)


def cap_value(value: Any, low: int = 1) -> bool:
    """An integer cap from ``low`` to ``CAP_MAX`` (bool, float, NaN refused)."""
    return type(value) is int and low <= value <= CAP_MAX


def finite_time(value: Any) -> bool:
    """A finite, non-negative int or float (bool, NaN, ±inf, negative refused)."""
    return type(value) in (int, float) and math.isfinite(value) and value >= 0


def hex_id(value: Any) -> bool:
    """A 128-bit id as 32 lowercase hex characters."""
    return isinstance(value, str) and bool(_HEX32.fullmatch(value))
