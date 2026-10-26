"""``memory_rate``: the agent rates the importance of this turn's memory (ADR 0018 §1, phase 0).

The model supplies ``importance`` (0..1), ``reason`` (a code) and optionally
``half_life_days``; the harness (``memory_autonomy.RatingSession``) stamps who, when, which
turn and the turn's trust, and binds the rating to the exact text of the episode the server
writes at the end of the turn. The tool has no text argument, writes no memory text and no
file, cannot set ``invariant`` or any retention tag, and creates no job.

**Phase 0 — observe only:** the rating is stored and shown in the status counts; nothing uses
it yet (no routing, recall or retention effect).

Like ``propose_procedure`` (#463), the registry carries an **unbound** copy (same name and
spec) so the tool rules, profiles and the YOLO classes know the name; ``get_tools`` never
hands it out (``tool_registry.HOST_BOUND_TOOLS``). Only the chat server binds a working one,
and only when ``memory_autonomy`` is on (``in_turn_rating``).

**Rule class.** Listed in the tool rules with the **read** category and default **Always**
(commander decision Q3): its only effect is one bounded number per turn about the turn's own
episode, in the search index on the data-directory floor. YOLO class **internal** (pathless):
unlike ``propose_procedure`` it puts no text in front of a person, unlike ``cron`` it starts
nothing later, unlike ``delegate`` it runs no child — so a saved Ask may be answered by a
window like ``memory_read``. Outside a NORMAL breaker the harness refuses it.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from strands import tool

from stiracore.memory_rating import (
    HALF_LIFE_MAX_DAYS,
    HALF_LIFE_MIN_DAYS,
    REASONS,
    RatingRefused,
)

MEMORY_RATE_TOOL_NAME = "memory_rate"

DESCRIPTION = (
    "Rate how important this turn is to remember, once per turn. The rating is stored with "
    "this turn's memory for later review; it does not change what is remembered or recalled "
    "yet. Rate honestly: most turns are routine."
)
_SCHEMA = {
    "json": {
        "type": "object",
        "properties": {
            "importance": {
                "type": "number",
                "description": "0.0 (routine, can fade) to 1.0 (essential to keep).",
            },
            "reason": {
                "type": "string",
                "enum": list(REASONS),
                "description": "Why, as one code.",
            },
            "half_life_days": {
                "type": "integer",
                "description": (
                    f"Optional: how long it stays useful, {HALF_LIFE_MIN_DAYS} to "
                    f"{HALF_LIFE_MAX_DAYS} days (derived from importance when omitted)."
                ),
            },
        },
        "required": ["importance", "reason"],
    }
}


def _make(handler: Callable[[Any, Any, Any], str]):
    @tool(name=MEMORY_RATE_TOOL_NAME, description=DESCRIPTION, inputSchema=_SCHEMA)
    def memory_rate(importance: float, reason: str, half_life_days: int | None = None) -> str:
        return handler(importance, reason, half_life_days)

    return memory_rate


def _unbound(importance: Any, reason: Any, half_life_days: Any) -> str:
    raise RuntimeError("E_NOT_BOUND: memory ratings are not available here")


# The registry's copy: name and spec only (never offered by get_tools).
unbound_memory_rate = _make(_unbound)


def create_memory_rate_tool(session: Any) -> Any:
    """The working tool, bound to a ``RatingSession`` (the server's turn id and store)."""

    def handler(importance: Any, reason: Any, half_life_days: Any) -> str:
        try:
            result = session.rate(importance, reason, half_life_days)
        except RatingRefused as error:
            # An error result: nothing is pending for this turn.
            raise RuntimeError(f"Memory rating refused: {error.code}") from None
        note = f" (adjusted: {', '.join(result['capped'])})" if result["capped"] else ""
        return (
            f"Rating noted for this turn's memory: importance {result['importance']:.2f}, "
            f"reason {result['reason']}, half-life {result['half_life_days']} days{note}. "
            "It is stored with the episode; it does not change memory behaviour yet."
        )

    return _make(handler)


__all__ = [
    "DESCRIPTION",
    "MEMORY_RATE_TOOL_NAME",
    "create_memory_rate_tool",
    "unbound_memory_rate",
]
