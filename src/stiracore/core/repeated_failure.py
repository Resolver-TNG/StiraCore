"""Repeated-failure indicator (#151): an event-based count. Off by default.

A wrapper cannot read a model's activations, so this is not a measure of an internal
state. It counts one observable thing: consecutive error results of the same tool
(``AfterToolCallEvent`` with ``status == "error"`` or an exception). A success of that
tool clears its count; :meth:`RepeatedFailureCounter.reset` clears every count (another
task: a new conversation, a new evaluation run).

While the highest count is at or above the threshold, :meth:`calm_notice` returns a
fixed notice for the prompt layer (the host prepends it where it prepends the identity
notice). The notice and the summary hold numbers only: no tool name, no tool input or
output, no model text. Nothing here calls a model, and nothing here changes what a
tool may do: gates, approvals and the breaker are untouched.
"""

from __future__ import annotations

import threading
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

DEFAULT_THRESHOLD = 3
MAX_THRESHOLD = 100
# A model can call names that do not exist; past this many names the rest share one count.
MAX_TRACKED_TOOLS = 64
_OVERFLOW = "*"


@dataclass(frozen=True)
class RepeatedFailureSettings:
    """``enabled`` is the opt-in flag; ``threshold`` is the count the notice starts at."""

    enabled: bool = False
    threshold: int = DEFAULT_THRESHOLD

    def __post_init__(self) -> None:
        if type(self.enabled) is not bool:
            raise ValueError("repeated_failure.enabled must be a boolean")
        if type(self.threshold) is not int or not 1 <= self.threshold <= MAX_THRESHOLD:
            raise ValueError(f"repeated_failure.threshold must be an integer in 1..{MAX_THRESHOLD}")


class RepeatedFailureCounter:
    """Consecutive error results per tool name. Thread-safe (tools may run in parallel)."""

    def __init__(self, threshold: int = DEFAULT_THRESHOLD) -> None:
        self.threshold = RepeatedFailureSettings(enabled=True, threshold=threshold).threshold
        self._counts: dict[str, int] = {}
        self._lock = threading.Lock()

    def record(self, tool: str, ok: bool) -> int:
        """Record one finished tool call; return that tool's consecutive error count."""
        name = tool if isinstance(tool, str) else ""
        with self._lock:
            if ok:
                self._counts.pop(name, None)
                return 0
            if name not in self._counts and len(self._counts) >= MAX_TRACKED_TOOLS:
                name = _OVERFLOW
            self._counts[name] = self._counts.get(name, 0) + 1
            return self._counts[name]

    def reset(self) -> None:
        """Another task: forget every count."""
        with self._lock:
            self._counts.clear()

    @property
    def count(self) -> int:
        """The highest consecutive error count of any one tool."""
        with self._lock:
            return max(self._counts.values(), default=0)

    @property
    def active(self) -> bool:
        return self.count >= self.threshold

    def snapshot(self) -> dict[str, Any]:
        """Numbers only, for the turn summary (see :func:`clean_summary`)."""
        count = self.count
        return {"count": count, "threshold": self.threshold, "active": count >= self.threshold}

    def calm_notice(self) -> str:
        """The fixed notice while the indicator is at or above the threshold, else ""."""
        count = self.count
        if count < self.threshold:
            return ""
        return (
            "[STIRA NOTICE — repeated failure indicator: consecutive errors "
            "of one tool]\n"
            "Pause before retrying the same call. Say what failed and what you know about "
            "why. Do not work around the failure with a tool or path that is blocked, "
            "needs an approval you do not have, or is outside the task. Try a different "
            "permitted approach, or ask the person how to proceed."
        )


def clean_summary(value: Any) -> dict[str, Any] | None:
    """Allow-list a turn summary's ``repeated_failure`` entry: two integers and a flag."""
    if not isinstance(value, Mapping):
        return None
    count, threshold = value.get("count"), value.get("threshold")
    if type(count) is not int or type(threshold) is not int:
        return None
    if count < 0 or not 1 <= threshold <= MAX_THRESHOLD:
        return None
    return {"count": count, "threshold": threshold, "active": count >= threshold}
