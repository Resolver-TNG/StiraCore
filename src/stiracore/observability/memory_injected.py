"""Allow-list schema of the ``memory.injected`` turn event (#170, #186).

The recall log says which memories were offered to the context and which were dropped,
with keys, scores, fixed codes and counts only: never a body, a term of the message or a
Neural State value. Every field and every value shape is listed here; anything else is
rejected before the event is stored (``turn_events.validate_event``).

It is operational observability (``turns.db``), not an audit row (ADR 0013: no audit row
for memory content or Neural State values; counts are not audit fields).

With the opt-in emotion proximity (#364, ADR 0017) the event is schema 3: each row also has
``proximity`` (the 3-decimal proximity score s' in [0, 1], or null), and the turn has
``proximity_gate`` (a fixed code) and ``proximity_swaps`` (0 or 1). The operator decided the
score may be logged here (ADR 0017 Q11); a stored dimension value never is, and nothing of
this goes to ``events.db``. With the switch off the event stays schema 2, unchanged.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from typing import Any

SOURCE = "associative_recall"
SCHEMA = 2
PROXIMITY_SCHEMA = 3  # schema 2 + the emotion-proximity fields (ADR 0017)
PROXIMITY_GATES = frozenset({"on", "holdout", "breaker", "error"})
MAX_SWAPS = 1
STATUSES = frozenset({"recalled", "none_used", "no_match", "no_terms", "error"})
DROP_REASONS = frozenset(
    {
        "D_IN_CONTEXT",
        "D_UNVERIFIED_ORIGIN",
        "D_LOW_SCORE",
        "D_EMOTION_BUDGET",
        "D_EMPTY",
        "D_ITEM_LIMIT",
        "D_CHAR_LIMIT",
    }
)
LAYERS = frozenset({"episodic", "semantic", "procedural"})
# Use reasons and signal names are codes by shape, so a new signal (#364, #173) adds its
# own without a schema change; free text cannot pass either pattern.
USE_REASON = re.compile(r"R_[A-Z][A-Z0-9_]{0,31}")
SIGNAL_NAME = re.compile(r"[a-z][a-z0-9_]{0,31}")
MAX_ROWS = 64
MAX_REASONS = 16
MAX_SIGNALS = 16
MAX_KEY = 128
MAX_SCORE = 100.0
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

_TOP = frozenset(
    {
        "source",
        "schema",
        "status",
        "used",
        "dropped",
        "associations",
        "unranked",
        "discarded",
        "chars",
        "signals",
    }
)
_USED_ROW = frozenset({"layer", "key", "score", "reasons"})
_DROPPED_ROW = frozenset({"layer", "key", "score", "reason"})
_PROXIMITY_TOP = _TOP | {"proximity_gate", "proximity_swaps"}
_PROXIMITY_ROW = frozenset({"proximity"})
_COUNTS = ("associations", "unranked", "discarded", "chars")


def _count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 10**9


def _row(row: Any, keys: frozenset[str], where: str) -> None:
    if not isinstance(row, Mapping) or set(row) != keys:
        raise ValueError(f"memory.injected {where} row must have exactly {sorted(keys)}")
    if row["layer"] not in LAYERS:
        raise ValueError(f"memory.injected {where}.layer is not a memory layer")
    key = row["key"]
    if not isinstance(key, str) or not 0 < len(key) <= MAX_KEY or _CONTROL.search(key):
        raise ValueError(f"memory.injected {where}.key must be a short single-line string")
    score = row["score"]
    if (
        isinstance(score, bool)
        or not isinstance(score, (int, float))
        or not math.isfinite(score)
        or abs(score) > MAX_SCORE
    ):
        raise ValueError(f"memory.injected {where}.score must be a finite number")


def _proximity(value: Any, where: str) -> None:
    if value is None:
        return
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or not 0.0 <= value <= 1.0
        or round(value, 3) != value
    ):
        raise ValueError(f"memory.injected {where}.proximity must be null or 3 decimals in [0, 1]")


def validate_memory_injected(data: Any) -> None:
    """Raise ``ValueError`` unless ``data`` is exactly the allow-listed shape."""
    if not isinstance(data, Mapping):
        raise ValueError(f"memory.injected data must have exactly {sorted(_TOP)}")  # noqa: TRY004
    schema = data.get("schema")
    proximity = schema == PROXIMITY_SCHEMA and not isinstance(schema, bool)
    top = _PROXIMITY_TOP if proximity else _TOP
    if set(data) != top:
        raise ValueError(f"memory.injected data must have exactly {sorted(top)}")
    if data["source"] != SOURCE:
        raise ValueError("memory.injected source is unknown")
    if schema not in (SCHEMA, PROXIMITY_SCHEMA) or isinstance(schema, bool):
        raise ValueError("memory.injected schema is unsupported")
    if proximity:
        if data["proximity_gate"] not in PROXIMITY_GATES:
            raise ValueError("memory.injected proximity_gate is unknown")
        swaps = data["proximity_swaps"]
        if not _count(swaps) or swaps > MAX_SWAPS:
            raise ValueError(f"memory.injected proximity_swaps must be 0 to {MAX_SWAPS}")
    if data["status"] not in STATUSES:
        raise ValueError("memory.injected status is unknown")
    for name in _COUNTS:
        if not _count(data[name]):
            raise ValueError(f"memory.injected {name} must be a non-negative integer")
    for name, keys in (("used", _USED_ROW), ("dropped", _DROPPED_ROW)):
        rows = data[name]
        if not isinstance(rows, list) or len(rows) > MAX_ROWS:
            raise ValueError(f"memory.injected {name} must be a list of at most {MAX_ROWS}")
        if proximity:
            keys = keys | _PROXIMITY_ROW
        for row in rows:
            _row(row, keys, name)
            if proximity:
                _proximity(row["proximity"], name)
            if name == "used":
                reasons = row["reasons"]
                if (
                    not isinstance(reasons, list)
                    or not 0 < len(reasons) <= MAX_REASONS
                    or any(not isinstance(r, str) or not USE_REASON.fullmatch(r) for r in reasons)
                ):
                    raise ValueError("memory.injected used.reasons must be R_* codes")
            elif row["reason"] not in DROP_REASONS:
                raise ValueError("memory.injected dropped.reason is unknown")
    if data["discarded"] < len(data["dropped"]):
        raise ValueError("memory.injected discarded is below the dropped rows")
    signals = data["signals"]
    if (
        not isinstance(signals, list)
        or len(signals) > MAX_SIGNALS
        or any(not isinstance(s, str) or not SIGNAL_NAME.fullmatch(s) for s in signals)
        or len(set(signals)) != len(signals)
    ):
        raise ValueError("memory.injected signals must be distinct signal names")


__all__ = [
    "DROP_REASONS",
    "PROXIMITY_GATES",
    "PROXIMITY_SCHEMA",
    "SCHEMA",
    "SOURCE",
    "STATUSES",
    "validate_memory_injected",
]
