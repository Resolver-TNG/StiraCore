"""Lightweight heuristic Neural State stimulus detector (zero extra LLM calls).

Fix #63: NXPAgent.__call__ always called post_turn(stimulus=None), so
apply_stimulus was skipped and Neural State never changed. This module
derives a conservative (category, intensity) stimulus from conversation
content so callers can actually drive state updates.
"""

from __future__ import annotations

import re
from typing import Any

# Stimulus categories are the plain lowercase string keys of EMOTION_MATRIX
# (the single source of truth in core.neural_state). apply_stimulus looks the
# stimulus up in that dict by string key, so a detector MUST return those exact
# strings — an Enum member does not match the str keys and silently no-ops
# (this was the #63 regression: state never moved).
try:
    from stiracore.core.neural_state import EMOTION_MATRIX as _MATRIX

    _KEYS = set(_MATRIX)
except ImportError:  # pragma: no cover - optional standalone import fallback
    _KEYS = {"joy", "threat", "achievement", "loss", "uncertainty", "connection"}


def _cat(name: str) -> str:
    """Return a validated EMOTION_MATRIX category key."""
    if name not in _KEYS:  # guard against a category the matrix does not know
        raise ValueError(f"Unknown stimulus category {name!r}; known: {sorted(_KEYS)}")
    return name


JOY = _cat("joy")
THREAT = _cat("threat")
ACHIEVEMENT = _cat("achievement")
LOSS = _cat("loss")
UNCERTAINTY = _cat("uncertainty")
CONNECTION = _cat("connection")

MAX_INTENSITY = 0.8

_QUESTION = re.compile(
    r"\b(why|how|what if|wonder|curious|unsure|confused|not sure)\b", re.IGNORECASE
)
_GRATITUDE = re.compile(
    r"\b(thank you|thanks|appreciate|great job|well done|awesome|amazing)\b", re.IGNORECASE
)
_GREETING = re.compile(r"^\s*(hi|hey|hello|good morning|good evening|sup|yo)\b", re.IGNORECASE)
_ERROR = re.compile(
    r"\b(error|fail(ed|ure)?|exception|traceback|crash(ed)?|denied|timeout)\b", re.IGNORECASE
)
_SUCCESS = re.compile(
    r"\b(success(fully)?|done|completed|passed|fixed|resolved|works)\b", re.IGNORECASE
)
_JOY = re.compile(r"\b(happy|excited|glad|delighted|joy|fun|lol|haha)\b", re.IGNORECASE)
_LOSS = re.compile(r"\b(sad|sorry to hear|lost|miss(ed)?|regret|unfortunate(ly)?)\b", re.IGNORECASE)

# A stimulus is (EMOTION_MATRIX category key, intensity).
Stimulus = tuple[str, float]
# An explained stimulus adds the name of the rule that fired (never message text).
ExplainedStimulus = tuple[str, float, str]


def cap(intensity: float) -> float:
    return max(0.0, min(intensity, MAX_INTENSITY))


def _tool_signal(tool_results: list[Any] | None) -> ExplainedStimulus | None:
    if not tool_results:
        return None
    text = " ".join(str(r) for r in tool_results)
    if _ERROR.search(text):
        return (THREAT, cap(0.4), "tool error")
    if _SUCCESS.search(text):
        return (ACHIEVEMENT, cap(0.4), "tool success")
    return None


def _breaker_signal(breaker_result: Any | None) -> ExplainedStimulus | None:
    if breaker_result is None:
        return None
    level = str(getattr(breaker_result, "level", breaker_result)).upper()
    if "CRITICAL" in level:
        return (THREAT, cap(0.8), "breaker CRITICAL")
    if "WARNING" in level:
        return (THREAT, cap(0.6), "breaker WARNING")
    return None


def detect_stimulus_explained(
    user_message: str | None = None,
    agent_response: str | None = None,
    tool_results: list[Any] | None = None,
    breaker_result: Any | None = None,
) -> ExplainedStimulus | None:
    """Like detect_stimulus, but also name the rule that fired.

    The rule name is a fixed label, never an excerpt of the conversation, so
    it is safe to persist as the Neural State emotion transcription (#75).
    """
    signal = _breaker_signal(breaker_result) or _tool_signal(tool_results)
    if signal:
        return signal

    msg = user_message or ""
    resp = agent_response or ""
    combined = f"{msg}\n{resp}"

    if _GRATITUDE.search(msg):
        return (CONNECTION, cap(0.5), "gratitude keyword")
    if _JOY.search(combined):
        return (JOY, cap(0.4), "joy keyword")
    if _LOSS.search(combined):
        return (LOSS, cap(0.4), "loss keyword")
    if _GREETING.search(msg):
        return (CONNECTION, cap(0.2), "greeting")

    is_question = "?" in msg or bool(_QUESTION.search(msg))
    is_long = len(resp) > 800

    if is_question and is_long:
        return (UNCERTAINTY, cap(0.3), "question + long response")
    if is_question:
        return (UNCERTAINTY, cap(0.35), "question")
    if is_long:
        return (CONNECTION, cap(0.3), "long response")

    return None


def detect_stimulus(
    user_message: str | None = None,
    agent_response: str | None = None,
    tool_results: list[Any] | None = None,
    breaker_result: Any | None = None,
) -> Stimulus | None:
    """Derive a conservative (category, intensity) stimulus, or None if there
    is no clear signal (callers should then skip stimulus application)."""
    explained = detect_stimulus_explained(
        user_message=user_message,
        agent_response=agent_response,
        tool_results=tool_results,
        breaker_result=breaker_result,
    )
    if explained is None:
        return None
    return explained[0], explained[1]
