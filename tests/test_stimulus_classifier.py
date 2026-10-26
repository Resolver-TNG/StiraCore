"""Tests for pluggable stimulus classifiers without loading a sokudan model."""

from __future__ import annotations

from typing import Any

import pytest

from stiracore.agent import stimulus_classifier
from stiracore.agent.stimulus_classifier import (
    RuleBasedClassifier,
    SokudanClassifier,
)


class FakeSokudanAgent:
    def __init__(self, *, category: str, confidence: float, score: float) -> None:
        self.category = category
        self.confidence = confidence
        self.score = score
        self.calls: list[tuple[str, dict[str, dict[str, Any]]]] = []

    def predict(self, state: str, questions: dict[str, dict[str, Any]]) -> dict[str, Any]:
        self.calls.append((state, questions))
        return {
            "answers": {
                "emotion": {
                    "type": "choice",
                    "choice": self.category,
                    "probabilities": {self.category: self.confidence},
                    "confidence": self.confidence,
                },
                "intensity": {
                    "type": "score",
                    "score": self.score,
                    "probabilities": {"0": 0.1, "1": 0.2, "2": 0.7},
                    "legend": {"0": "弱い", "1": "中程度", "2": "強い"},
                    "confidence": 0.7,
                },
            },
            "calibrated": False,
            "usage": {},
        }


def _classifier_with(agent: FakeSokudanAgent, threshold: float = 0.5) -> SokudanClassifier:
    classifier = SokudanClassifier(confidence_threshold=threshold)
    classifier._agent = agent
    return classifier


def test_rule_based_classifier_delegates_to_detector(monkeypatch: pytest.MonkeyPatch) -> None:
    received: dict[str, Any] = {}

    def fake_detect_stimulus_explained(**kwargs: Any) -> tuple[str, float, str]:
        received.update(kwargs)
        return ("connection", 0.5, "gratitude keyword")

    monkeypatch.setattr(
        stimulus_classifier.stimulus_detector,
        "detect_stimulus_explained",
        fake_detect_stimulus_explained,
    )

    result = RuleBasedClassifier().classify(
        "thanks",
        agent_response="done",
        tool_results=["ok"],
        breaker_result="NORMAL",
    )

    assert result is not None
    assert result.as_tuple() == ("connection", 0.5)
    assert result.transcription == "rule: connection (gratitude keyword)"
    assert result.classifier_name == "rule"
    assert received == {
        "user_message": "thanks",
        "agent_response": "done",
        "tool_results": ["ok"],
        "breaker_result": "NORMAL",
    }


def test_get_classifier_falls_back_when_sokudan_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing_sokudan(name: str) -> Any:
        if name == "sokudan":
            raise ModuleNotFoundError("No module named 'sokudan'")
        raise AssertionError(f"unexpected import: {name}")

    monkeypatch.setattr(stimulus_classifier, "import_module", missing_sokudan)

    classifier = stimulus_classifier.get_classifier(prefer_sokudan=True)

    assert isinstance(classifier, RuleBasedClassifier)


@pytest.mark.parametrize(
    ("score", "expected_intensity"),
    [(0.0, 0.2), (1.0, 0.5), (2.0, 0.8)],
)
def test_sokudan_classifier_maps_intensity_levels(
    score: float,
    expected_intensity: float,
) -> None:
    agent = FakeSokudanAgent(category="connection", confidence=0.9, score=score)
    classifier = _classifier_with(agent)

    result = classifier.classify("ありがとう")
    assert result is not None
    assert result.as_tuple() == ("connection", expected_intensity)
    state, questions = agent.calls[0]
    assert state == "ありがとう"
    assert set(questions["emotion"]["criteria"]) == {
        "joy",
        "threat",
        "achievement",
        "loss",
        "uncertainty",
        "connection",
    }
    assert all(questions["emotion"]["criteria"].values())
    assert questions["intensity"]["criteria"] == ["弱い", "中程度", "強い"]


def test_sokudan_classifier_abstains_below_threshold() -> None:
    agent = FakeSokudanAgent(category="joy", confidence=0.49, score=2.0)

    assert _classifier_with(agent).classify("楽しい") is None


def test_sokudan_classifier_abstains_for_unknown_category() -> None:
    agent = FakeSokudanAgent(category="surprise", confidence=0.99, score=2.0)

    assert _classifier_with(agent).classify("びっくりした") is None


def test_sokudan_classifier_uses_response_when_user_message_is_empty() -> None:
    agent = FakeSokudanAgent(category="achievement", confidence=0.9, score=1.0)
    classifier = _classifier_with(agent)

    result = classifier.classify("", agent_response="修正が完了した")
    assert result is not None
    assert result.as_tuple() == ("achievement", 0.5)
    assert agent.calls[0][0] == "修正が完了した"
