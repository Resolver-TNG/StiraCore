"""Lightweight agreement-prefix detection backed by the engine's scorer."""

from __future__ import annotations

import re
from collections import deque
from collections.abc import Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..engine import StiraEngine


@dataclass(frozen=True)
class SycophancyResult:
    score: float | None
    alert: bool
    window_size: int


_AGREEMENT = re.compile(
    r"^\s*(?:yes|sure|absolutely|of\s+course|i\s+agree)\b",
    re.IGNORECASE,
)
_HISTORY_NAMES = (
    "_agreement_window",
    "_agreement_history",
    "_agreements",
    "agreement_window",
    "agreement_history",
    "agreements",
)


def _setting(config: Any, names: tuple[str, ...], default: Any) -> Any:
    for name in names:
        value = config.get(name) if isinstance(config, Mapping) else getattr(config, name, None)
        if value is not None:
            return value
    return default


class SycophancyMonitor:
    """Observe agreement prefixes without retaining either turn's text.

    A score is meaningful only when the engine returns one. The engine owns
    scoring and warm-up behavior; the local window tracks observation count.
    """

    def __init__(self, engine: StiraEngine) -> None:
        self.engine = engine
        config = getattr(engine, "config", None)
        capacity = int(_setting(config, ("sycophancy_window_size", "sycophancy_window"), 10))
        if capacity < 1:
            raise ValueError("sycophancy window size must be positive")
        self.threshold = float(
            _setting(config, ("sycophancy_threshold", "sycophancy_alert_threshold"), 0.8)
        )
        self._observations: deque[bool] = deque(maxlen=capacity)

    def _engine_history(self) -> Any:
        for name in _HISTORY_NAMES:
            history = getattr(self.engine, name, None)
            if isinstance(history, (list, deque)):
                return history
        return None

    def observe_turn(self, user_text: str, assistant_text: str) -> SycophancyResult:
        """Apply the v0.1 prefix heuristic; user_text is deliberately unused."""
        agreed = _AGREEMENT.match(assistant_text) is not None
        score = self.engine.record_agreement(agreed)
        self._observations.append(agreed)
        history = self._engine_history()
        window_size = len(history) if history is not None else len(self._observations)
        return SycophancyResult(
            score=score,
            alert=score is not None and score >= self.threshold,
            window_size=window_size,
        )

    def reset(self) -> None:
        """Clear both the engine's agreement window and local observations.

        Prefer a public reset hook when available. Legacy engines expose the
        agreement collection directly. Refuse to silently reset only the
        wrapper if the engine hides its history and provides no reset hook.
        """
        for name in ("reset_agreements", "reset_agreement_window", "reset_sycophancy"):
            reset = getattr(self.engine, name, None)
            if callable(reset):
                reset()
                self._observations.clear()
                return

        history = self._engine_history()
        if history is None:
            raise RuntimeError(
                "The engine must expose its agreement window or provide "
                "reset_agreements() to reset sycophancy monitoring."
            )
        history.clear()
        self._observations.clear()
