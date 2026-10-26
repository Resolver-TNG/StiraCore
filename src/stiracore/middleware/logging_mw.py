"""Structured operational events, with no prompt or response logging."""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Mapping
from datetime import UTC, datetime
from enum import Enum, Flag
from numbers import Real
from typing import Any


def _label(value: Any) -> str | int | None:
    if isinstance(value, Enum):
        return value.name
    if value is None or isinstance(value, (str, int)):
        return value
    # Never stringify arbitrary objects: their repr may contain content.
    return type(value).__name__


def _numeric(value: Any) -> float | int | None:
    if isinstance(value, bool) or not isinstance(value, Real):
        return None
    if not math.isfinite(float(value)):
        return None
    return int(value) if isinstance(value, int) else float(value)


# Only state metrics may enter stimulus/delta payloads. Arbitrary keys and
# string values are discarded, even when nested in otherwise valid input.
_METRICS = frozenset(
    {
        "stability",
        "trust",
        "curiosity",
        "vigilance",
        "attachment",
        "satisfaction",
        "energy",
        "coherence",
        "arousal",
        "valence",
        "cognitive_load",
        "cognitiveLoad",
        "bc_score",
        "bcScore",
        "stress",
        "fatigue",
        "confidence",
        "intensity",
        "novelty",
        "salience",
        "reward",
        "threat",
        "social",
        "cognitive",
        "emotional",
        "physical",
        "before",
        "after",
        "delta",
    }
)


def _metrics(value: Any) -> Any:
    number = _numeric(value)
    if number is not None:
        return number
    if value is None:
        return None
    if isinstance(value, Enum):
        return value.name
    if isinstance(value, Mapping):
        result = {}
        for key, item in value.items():
            if isinstance(key, str) and key in _METRICS:
                clean = _metrics(item)
                if clean is not None:
                    result[key] = clean
        return result
    return None


def _actions(value: Any) -> Any:
    if isinstance(value, Flag):
        return value.name.split("|") if value.name else []
    if isinstance(value, (list, tuple, set, frozenset)):
        labels = [_label(item) for item in value]
        if isinstance(value, (set, frozenset)):
            labels.sort(key=str)
        return labels
    return _label(value)


class StiraEventLogger:
    """Emit JSON through Python logging without configuring handlers.

    Labels, turn IDs, tool names, and reasons must be application-owned
    metadata, never user text, tool arguments, prompts, or responses.
    Stimulus and delta payloads are restricted to numeric state metrics.
    """

    def __init__(self, logger_name: str = "stira.events") -> None:
        self.logger = logging.getLogger(logger_name)

    def _emit(
        self,
        event_type: str,
        data: dict[str, Any],
        *,
        turn_id: Any = None,
        level: int = logging.INFO,
    ) -> None:
        entry: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(timespec="milliseconds"),
            "event_type": event_type,
            "data": data,
        }
        if turn_id is not None:
            entry["turn_id"] = _label(turn_id)
        self.logger.log(
            level,
            json.dumps(entry, ensure_ascii=True, allow_nan=False, separators=(",", ":")),
        )

    def log_turn_start(self, turn_id: Any, breaker_level: Any, actions: Any) -> None:
        self._emit(
            "turn_start",
            {"breaker_level": _label(breaker_level), "actions": _actions(actions)},
            turn_id=turn_id,
        )

    def log_turn_end(
        self,
        turn_id: Any,
        stimulus: Any,
        deltas: Any,
        duration_ms: float,
    ) -> None:
        self._emit(
            "turn_end",
            {
                "stimulus": _metrics(stimulus),
                "deltas": _metrics(deltas),
                "duration_ms": _numeric(duration_ms),
            },
            turn_id=turn_id,
        )

    def log_breaker_transition(
        self,
        from_level: Any,
        to_level: Any,
        stability: float,
        bc_score: float,
    ) -> None:
        self._emit(
            "breaker_transition",
            {
                "from_level": _label(from_level),
                "to_level": _label(to_level),
                "stability": _numeric(stability),
                "bc_score": _numeric(bc_score),
            },
        )

    def log_tool_blocked(self, tool_name: str, reason: str) -> None:
        self._emit(
            "tool_blocked",
            {"tool_name": _label(tool_name), "reason": _label(reason)},
            level=logging.WARNING,
        )

    def log_identity_injection(self, level: Any, anchors_count: int) -> None:
        self._emit(
            "identity_injection",
            {"level": _label(level), "anchors_count": _numeric(anchors_count)},
        )

    def log_sycophancy_alert(self, score: float, threshold: float, window_size: int) -> None:
        self._emit(
            "sycophancy_alert",
            {
                "score": _numeric(score),
                "threshold": _numeric(threshold),
                "window_size": _numeric(window_size),
            },
            level=logging.WARNING,
        )
