"""#367: say which classifier drives Neural State, and why a turn left it unchanged.

A live run (19 turns, Sonnet, no sokudan installed) moved the emotions in 7 turns; nothing on
screen said that classification ran on the rule fallback. These tests pin the additive,
display-only fields: the classifier mode (``rule_only`` / ``sokudan`` / ``unknown``), the
fallback flag, the per-turn "no update" reason (a fixed code, never text) and the counts
since start-up. Classification itself (rules, thresholds, EMOTION_MATRIX, NeuralState) is
not changed: the tests read it only.
"""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from stiracore.agent import nxp_agent
from stiracore.agent.stimulus_classifier import (
    CLASSIFIER_MODES,
    NO_UPDATE_REASONS,
    RuleBasedClassifier,
    SokudanClassifier,
    StimulusResult,
    classifier_mode,
)
from stiracore.core import NeuralState


class SequenceClassifier:
    name = "rule"

    def __init__(self, *results):
        self._results = list(results)

    def classify(self, **_kwargs):
        result = self._results.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


@pytest.fixture
def offline_agent(monkeypatch):
    monkeypatch.setattr(nxp_agent, "BedrockModel", Mock(return_value="offline-model"))
    failures = []

    class OfflineStrandsAgent:
        def __init__(self, **kwargs):
            self.messages = list(kwargs.get("messages") or [])
            self.system_prompt = kwargs.get("system_prompt")

        def __call__(self, _prompt):
            if failures and failures.pop(0):
                raise RuntimeError("model failed")
            return "offline reply"

    monkeypatch.setattr(nxp_agent, "Agent", OfflineStrandsAgent)

    def build(classifier, *, state=None, fail=()):
        failures[:] = list(fail)
        return nxp_agent.NXPAgent(
            model_id="offline-model",
            region_name="us-east-1",
            state=state,
            stimulus_classifier=classifier,
        )

    return build


def test_the_vocabularies_are_fixed():
    assert CLASSIFIER_MODES == ("rule_only", "sokudan", "unknown")
    assert NO_UPDATE_REASONS == (
        "no_stimulus",
        "classifier_error",
        "unknown_stimulus",
        "no_change",
        "turn_incomplete",
    )


def test_classifier_mode_names_the_classifier_in_use():
    assert classifier_mode(RuleBasedClassifier()) == "rule_only"
    assert classifier_mode(SokudanClassifier(lazy=True)) == "sokudan"
    assert classifier_mode(None) == "unknown"
    assert classifier_mode(object()) == "unknown"


def test_an_applied_stimulus_has_no_reason(offline_agent):
    agent = offline_agent(SequenceClassifier(StimulusResult("connection", 0.3, "rule")))
    agent("ありがとう")
    summary = agent.last_turn_summary
    assert summary["deltas"]
    assert summary["no_update_reason"] is None


@pytest.mark.parametrize(
    ("verdict", "reason"),
    [
        (None, "no_stimulus"),
        (RuntimeError("classifier down"), "classifier_error"),
        (StimulusResult("not-a-stimulus", 0.5, "rule"), "unknown_stimulus"),
        # A known stimulus at intensity 0 moves nothing (also: every value at its limit).
        (StimulusResult("connection", 0.0, "rule"), "no_change"),
    ],
)
def test_a_turn_without_an_update_says_why(offline_agent, verdict, reason):
    agent = offline_agent(SequenceClassifier(verdict))
    agent("ok")
    summary = agent.last_turn_summary
    assert summary["deltas"] == {}
    assert summary["no_update_reason"] == reason


def test_a_failed_turn_is_incomplete(offline_agent):
    classifier = SequenceClassifier()  # never asked: the turn did not complete
    agent = offline_agent(classifier, fail=[True])
    with pytest.raises(RuntimeError):
        agent("ok")
    assert agent.last_turn_summary["no_update_reason"] == "turn_incomplete"


def test_counts_since_start_up(offline_agent):
    agent = offline_agent(
        SequenceClassifier(
            StimulusResult("connection", 0.3, "rule"),
            None,
            None,
            RuntimeError("down"),
            StimulusResult("joy", 0.5, "rule"),
        ),
        fail=[False, False, False, False, False, True],
    )
    assert agent.emotion_updates == {
        "turns": 0,
        "updated": 0,
        "reasons": dict.fromkeys(NO_UPDATE_REASONS, 0),
        "last_reason": None,
    }
    for _ in range(5):
        agent("x")
    with pytest.raises(RuntimeError):
        agent("x")
    counts = agent.emotion_updates
    assert counts["turns"] == 6
    assert counts["updated"] == 2
    assert counts["reasons"] == {
        "no_stimulus": 2,
        "classifier_error": 1,
        "unknown_stimulus": 0,
        "no_change": 0,
        "turn_incomplete": 1,
    }
    assert counts["last_reason"] == "turn_incomplete"
    counts["reasons"]["no_stimulus"] = 99
    assert agent.emotion_updates["reasons"]["no_stimulus"] == 2, "a defensive copy"


def test_neural_state_is_only_read(offline_agent):
    """The visibility fields add nothing to the saved state (#367 is display only)."""
    state = NeuralState()
    before = set(state.to_dict())
    agent = offline_agent(SequenceClassifier(None, StimulusResult("joy", 0.5, "rule")), state=state)
    agent("a")
    agent("b")
    assert set(state.to_dict()) == before


def test_zero_valued_deltas_are_not_an_update(offline_agent, monkeypatch):
    """Review m-1: an entry that moved nothing (0.0) is not counted as an update."""
    agent = offline_agent(SequenceClassifier(StimulusResult("connection", 0.3, "rule")))
    post_turn = agent.engine.post_turn

    def zero_deltas(*args, **kwargs):
        summary = post_turn(*args, **kwargs)
        summary["deltas"] = dict.fromkeys(summary["deltas"] or {"trust": 0.0}, 0.0)
        return summary

    monkeypatch.setattr(agent.engine, "post_turn", zero_deltas)
    agent("thanks")
    assert agent.last_turn_summary["no_update_reason"] == "no_change"
    assert agent.emotion_updates["updated"] == 0
