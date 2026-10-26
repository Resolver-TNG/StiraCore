"""Per-turn lifecycle management and Neural State prompt context."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import asdict, is_dataclass
from enum import Enum
from itertools import islice
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..engine import BreakResult, StiraEngine


def _get(obj: Any, *names: str, default: Any = None) -> Any:
    for name in names:
        value = obj.get(name) if isinstance(obj, Mapping) else getattr(obj, name, None)
        if value is not None:
            return value
    return default


def _snapshot(value: Any) -> Any:
    """Accept mapping, dataclass, Pydantic, and plain-object state models."""
    snapshot = getattr(value, "snapshot", None)
    if callable(snapshot):
        value = snapshot()
    if isinstance(value, Mapping):
        return value
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return model_dump()
    if is_dataclass(value) and not isinstance(value, type):
        return asdict(value)
    return value


def _number(value: Any) -> str:
    if value is None:
        return "unknown"
    try:
        return f"{float(value):.2f}"
    except (TypeError, ValueError):
        return "unknown"


def _level_name(value: Any) -> str:
    if isinstance(value, Enum):
        return value.name
    return "UNKNOWN" if value is None else str(value)


class NeuralStateMiddleware:
    """Wrap a turn without interpreting or retaining its prompt."""

    @staticmethod
    @contextmanager
    def wrap_turn(engine: StiraEngine, prompt: str) -> Iterator[BreakResult]:
        """Finalize every successfully entered turn, including failed turns.

        ``prompt`` is intentionally unused: v0.1 does not derive a stimulus
        from prompt content. If pre_turn itself fails, no turn was entered.
        """
        result = engine.pre_turn()
        try:
            yield result
        finally:
            engine.post_turn(stimulus=None)

    @staticmethod
    def build_state_context(engine: StiraEngine, max_episodic: int = 3) -> str:
        """Build context without running pre_turn or modifying engine state.

        Missing metrics are reported as unknown rather than fabricated.
        Episodic entries, when exposed by the state, are assumed to be stored
        oldest-first. This context is intended for injection, not logging.
        """
        if max_episodic < 0:
            raise ValueError("max_episodic must be non-negative")

        state = _get(engine, "state", "neural_state")
        if state is None:
            state = engine
        state = _snapshot(state)

        fields = [
            f"stability={_number(_get(state, 'stability'))}",
            f"trust={_number(_get(state, 'trust'))}",
        ]
        for label, aliases in (
            ("energy", ("energy",)),
            ("coherence", ("coherence",)),
            ("arousal", ("arousal",)),
            ("valence", ("valence",)),
            ("cognitive_load", ("cognitive_load", "cognitiveLoad")),
        ):
            value = _get(state, *aliases)
            if value is not None:
                fields.append(f"{label}={_number(value)}")

        bc_score = _get(
            state,
            "bc_score",
            "bcScore",
            default=_get(engine, "bc_score", "bcScore"),
        )
        result = _get(engine, "_current_break_result")
        breaker = _get(engine, "breaker", "circuit_breaker")
        level = _get(
            result,
            "level",
            default=_get(
                breaker,
                "level",
                default=_get(state, "breaker_level", default=_get(engine, "breaker_level")),
            ),
        )
        fields.extend((f"bcScore={_number(bc_score)}", f"breaker={_level_name(level)}"))
        context = "[Neural State] " + " ".join(fields)

        if max_episodic:
            episodic = _get(state, "episodic", "episodic_memory", default=())
            if isinstance(episodic, Mapping):
                episodic = episodic.get("entries", ())
            if isinstance(episodic, (str, bytes)):
                episodic = (episodic,)
            try:
                # Reversible collections avoid copying a potentially large history.
                recent = list(islice(reversed(episodic), max_episodic))
                recent.reverse()
            except TypeError:
                recent = []
            for entry in recent:
                text = _get(entry, "summary", "content", "text")
                if text is None and isinstance(entry, str):
                    text = entry
                if text:
                    context += "\n[Episode] " + " ".join(str(text).split())

        return context
