"""#75: classifier rationale must reach NeuralState.emotion_transcription.

These run the real classify -> NXPAgent -> StiraEngine.post_turn -> NeuralState
path (only the Strands Agent and BedrockModel are faked), so a wiring break
between modules fails here even if each unit test is green.
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import MagicMock, Mock

import pytest

from stiracore.agent import nxp_agent
from stiracore.agent.state_persistence import StatePersistence
from stiracore.agent.stimulus_classifier import (
    RuleBasedClassifier,
    SokudanClassifier,
    StimulusResult,
    coerce_stimulus_result,
)
from stiracore.core import NeuralState
from stiracore.core.engine import MAX_TRANSCRIPTION_LEN


class FakeSokudanAgent:
    def __init__(self, category: str = "connection", confidence: float = 0.97, score: float = 2.0):
        self.category = category
        self.confidence = confidence
        self.score = score

    def predict(self, state: str, questions: dict[str, Any]) -> dict[str, Any]:
        return {
            "answers": {
                "emotion": {"choice": self.category, "confidence": self.confidence},
                "intensity": {"score": self.score},
            }
        }


def _sokudan(**kw: Any) -> SokudanClassifier:
    classifier = SokudanClassifier()
    classifier._agent = FakeSokudanAgent(**kw)
    return classifier


class SequenceClassifier:
    """Returns (or raises) the queued values in order."""

    name = "seq"

    def __init__(self, *values: Any) -> None:
        self.values = list(values)

    def classify(self, user_message, agent_response="", tool_results=None, breaker_result=None):
        value = self.values.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


@pytest.fixture
def make_agent(monkeypatch):
    """Real NXPAgent + real engine/state; Strands Agent returns a fixed reply."""
    monkeypatch.setattr(nxp_agent, "BedrockModel", Mock())

    def build(**kwargs):
        inner = MagicMock()
        inner.messages = list(kwargs.get("messages") or [])
        inner.return_value = "offline reply"
        return inner

    monkeypatch.setattr(nxp_agent, "Agent", Mock(side_effect=build))

    def factory(classifier: Any, state: NeuralState | None = None) -> nxp_agent.NXPAgent:
        return nxp_agent.NXPAgent(
            model_id="offline",
            region_name="us-east-1",
            state=state if state is not None else NeuralState(),
            stimulus_classifier=classifier,
        )

    return factory


# --- classifier units -------------------------------------------------------


def test_sokudan_transcription_is_short_rationale_without_message_text() -> None:
    result = _sokudan(confidence=0.97, score=2.0).classify("本当にありがとう、助かった")

    assert isinstance(result, StimulusResult)
    assert result.transcription == "sokudan: connection (conf 0.97, 強い)"
    assert result.confidence == pytest.approx(0.97)
    assert result.classifier_name == "sokudan"
    assert "ありがとう" not in result.transcription


@pytest.mark.parametrize(
    ("kwargs", "rule"),
    [
        ({"user_message": "thank you so much"}, "gratitude keyword"),
        ({"user_message": "why?"}, "question"),
        ({"user_message": "ok", "tool_results": ["Traceback: error"]}, "tool error"),
        ({"user_message": "ok", "breaker_result": "CRITICAL"}, "breaker CRITICAL"),
    ],
)
def test_rule_based_transcription_names_fired_rule(kwargs: dict[str, Any], rule: str) -> None:
    result = RuleBasedClassifier().classify(**kwargs)

    assert isinstance(result, StimulusResult)
    assert result.transcription == f"rule: {result.category} ({rule})"
    assert kwargs["user_message"] not in result.transcription


def test_rule_based_abstains_without_signal() -> None:
    assert RuleBasedClassifier().classify("ok", agent_response="sure") is None


def test_coerce_rejects_unknown_shapes() -> None:
    assert coerce_stimulus_result(None) is None
    legacy = coerce_stimulus_result(("joy", 0.4), classifier_name="custom")
    assert legacy == StimulusResult("joy", 0.4, "custom: joy", None, "custom")
    with pytest.raises(ValueError):
        coerce_stimulus_result("joy")


# --- engine contract --------------------------------------------------------


def test_post_turn_sanitizes_and_falls_back(make_engine) -> None:
    engine = make_engine()

    engine.post_turn(stimulus="joy", intensity=0.4, transcription="a\nb\t c" + "x" * 500)
    assert "\n" not in engine.state.emotion_transcription
    assert engine.state.emotion_transcription.startswith("a b c")
    assert len(engine.state.emotion_transcription) == MAX_TRANSCRIPTION_LEN

    engine.post_turn(stimulus="loss", intensity=0.25)
    assert engine.state.emotion_transcription == "loss (intensity 0.25)"


def test_post_turn_unknown_stimulus_keeps_previous(make_engine) -> None:
    engine = make_engine()
    engine.post_turn(stimulus="joy", intensity=0.4, transcription="rule: joy (joy keyword)")

    engine.post_turn(stimulus="__unknown__", intensity=0.4, transcription="should not land")

    assert engine.state.emotion_transcription == "rule: joy (joy keyword)"


# --- classify -> post_turn -> NeuralState (integration) ---------------------


@pytest.mark.parametrize(
    ("classifier", "expected"),
    [
        (lambda: _sokudan(), "sokudan: connection (conf 0.97, 強い)"),
        (RuleBasedClassifier, "rule: connection (gratitude keyword)"),
    ],
)
def test_turn_writes_transcription_to_state(make_agent, classifier, expected) -> None:
    agent = make_agent(classifier())
    before_trust = agent.state.trust

    agent("thank you so much")

    assert agent.state.emotion_transcription == expected
    assert agent.state.trust != before_trust  # the stimulus really applied


def test_abstain_and_failure_keep_previous_transcription(make_agent) -> None:
    classifier = SequenceClassifier(
        StimulusResult("joy", 0.4, "seq: joy"),
        None,  # abstain
        ValueError("model exploded"),  # classifier failure
        "not-a-result",  # malformed return value
    )
    agent = make_agent(classifier)

    agent("turn 1")
    assert agent.state.emotion_transcription == "seq: joy"
    for prompt in ("turn 2", "turn 3", "turn 4"):
        agent(prompt)
        assert agent.state.emotion_transcription == "seq: joy"
    assert "exploded" not in agent.state.emotion_transcription


def test_aborted_turn_keeps_previous_transcription(make_agent, monkeypatch) -> None:
    state = NeuralState(emotion_transcription="rule: joy (joy keyword)")
    agent = make_agent(RuleBasedClassifier(), state=state)
    monkeypatch.setattr(nxp_agent, "Agent", Mock(side_effect=RuntimeError("bedrock down")))

    with pytest.raises(RuntimeError):
        agent("thank you")

    assert state.emotion_transcription == "rule: joy (joy keyword)"


def test_transcription_survives_persistence_roundtrip(make_agent, tmp_path) -> None:
    agent = make_agent(_sokudan())
    agent("ありがとう")
    path = tmp_path / "neural_state.json"
    StatePersistence(agent.state, path).save()

    restored = NeuralState()
    StatePersistence(restored, path).load()

    assert restored.emotion_transcription == "sokudan: connection (conf 0.97, 強い)"


def test_load_drops_non_string_transcription(tmp_path) -> None:
    path = tmp_path / "state.json"
    NeuralState().save(path)
    data = json.loads(path.read_text(encoding="utf-8"))
    data["emotion_transcription"] = {"bad": 1}
    path.write_text(json.dumps(data), encoding="utf-8")

    assert NeuralState.load(path).emotion_transcription == ""
