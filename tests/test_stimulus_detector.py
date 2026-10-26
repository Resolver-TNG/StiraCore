"""Tests for stimulus_detector — including the #63 regression guard.

The original #63 bug: detect_stimulus returned an Enum member that did not
match EMOTION_MATRIX's plain-string keys, so apply_stimulus silently no-oped
and Neural State never moved. The unit tests at the time only checked the
detector's return value, not the integration with post_turn — so the dead
wiring passed CI. These tests assert the *integration*: a detected stimulus
must actually drive a Neural State change.
"""

from __future__ import annotations

from stiracore.agent.stimulus_detector import (
    CONNECTION,
    THREAT,
    detect_stimulus,
)
from stiracore.core import NeuralState, StiraEngine
from stiracore.core.neural_state import EMOTION_MATRIX


def test_detected_category_is_a_valid_matrix_key() -> None:
    """Every category constant MUST be an EMOTION_MATRIX key (the #63 invariant)."""
    for category in (CONNECTION, THREAT):
        assert isinstance(category, str)
        assert category in EMOTION_MATRIX


def test_detect_returns_plain_string_category() -> None:
    stim = detect_stimulus(user_message="thank you so much")
    assert stim is not None
    category, intensity = stim
    assert type(category) is str  # NOT an Enum — must be a raw matrix key
    assert category in EMOTION_MATRIX
    assert 0.0 <= intensity <= 0.8


def test_detected_stimulus_actually_moves_neural_state() -> None:
    """#63 regression guard: detect -> post_turn -> state MUST change."""
    stim = detect_stimulus(user_message="thank you, that was a great job")
    assert stim is not None

    state = NeuralState()
    engine = StiraEngine(state=state)
    before = dict(state.snapshot())

    engine.pre_turn()
    engine.post_turn(stimulus=stim[0], intensity=stim[1])
    after = dict(state.snapshot())

    deltas = {k: after[k] - before[k] for k in after if abs(after[k] - before[k]) > 1e-9}
    assert deltas, "detected stimulus did not move Neural State (the #63 dead-wiring bug)"


def test_gratitude_maps_to_connection() -> None:
    stim = detect_stimulus(user_message="thanks, I appreciate it")
    assert stim is not None
    assert stim[0] == CONNECTION


def test_error_tool_result_maps_to_threat() -> None:
    stim = detect_stimulus(
        user_message="run it",
        tool_results=["Traceback: RuntimeError: boom"],
    )
    assert stim is not None
    assert stim[0] == THREAT


def test_no_signal_returns_none() -> None:
    # Neutral short statement, no question mark, no keywords, short response.
    assert detect_stimulus(user_message="ok", agent_response="sure") is None
