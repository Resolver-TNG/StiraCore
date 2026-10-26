"""#135: turn deltas flow from Neural State into the chat transport."""

from __future__ import annotations

import json
from unittest.mock import Mock

import pytest
from flask import Flask

from stiracore.agent import nxp_agent
from stiracore.agent.stimulus_classifier import StimulusResult
from stiracore.core import NeuralState
from stiracore.observability.chat_api import create_chat_blueprint


class SequenceClassifier:
    def __init__(self, *results):
        self._results = list(results)

    def classify(self, **_kwargs):
        return self._results.pop(0)


@pytest.fixture
def offline_agent(monkeypatch):
    monkeypatch.setattr(nxp_agent, "BedrockModel", Mock(return_value="offline-model"))

    class OfflineStrandsAgent:
        def __init__(self, **kwargs):
            self.messages = list(kwargs.get("messages") or [])

        def __call__(self, _prompt):
            return "offline reply"

    monkeypatch.setattr(nxp_agent, "Agent", OfflineStrandsAgent)

    def build(classifier, *, state=None):
        return nxp_agent.NXPAgent(
            model_id="offline-model",
            region_name="us-east-1",
            state=state,
            stimulus_classifier=classifier,
        )

    return build


def _app(agent):
    app = Flask(__name__)
    app.register_blueprint(
        create_chat_blueprint(
            agent,
            runner=agent,
            turn_summary_provider=lambda: agent.last_turn_summary,
        )
    )
    return app


def _event(response, name):
    marker = f"event: {name}\n"
    block = next(
        block for block in response.get_data(as_text=True).split("\n\n") if marker in block
    )
    data = next(line[6:] for line in block.splitlines() if line.startswith("data: "))
    return json.loads(data)


def test_detected_stimulus_summary_flows_from_post_turn_through_agent_to_chat_sse(
    offline_agent,
):
    agent = offline_agent(
        SequenceClassifier(StimulusResult("threat", 1.0, "rule: threat (tool error)"))
    )
    client = _app(agent).test_client()

    response = client.post("/api/chat", json={"message": "the tool failed"})

    assert response.status_code == 200
    summary = _event(response, "turn")["turn_summary"]
    assert summary == {
        "category": "threat",
        "deltas": {
            "trust": -0.01,
            "vigilance": 0.08,
            "attachment": 0.01,
            "stability": -0.02,
            "satisfaction": -0.03,
        },
        "emotion_transcription": "rule: threat (tool error)",
        "breaker": "normal",
    }
    assert agent.last_turn_summary["deltas"] == summary["deltas"]
    history = client.get("/api/chat/history").get_json()["turns"]
    assert history[-1]["turn_summary"] == summary

    summary["deltas"].clear()
    assert agent.last_turn_summary["deltas"], "Public summaries must be defensive copies"


def test_abstain_turn_exposes_empty_summary_without_stale_transcription(offline_agent):
    state = NeuralState(emotion_transcription="rule: joy (previous turn)")
    agent = offline_agent(SequenceClassifier(None), state=state)
    client = _app(agent).test_client()

    response = client.post("/api/chat", json={"message": "ok"})

    summary = _event(response, "turn")["turn_summary"]
    assert summary == {
        "category": None,
        "deltas": {},
        "emotion_transcription": None,
        "breaker": "normal",
    }
    assert state.emotion_transcription == "rule: joy (previous turn)"


def test_turn_summary_carries_classifier_verdict_for_the_ribbon(offline_agent):
    """#187: the ribbon shows confidence; abstain leaves it unrecorded (None, never 0)."""
    agent = offline_agent(
        SequenceClassifier(
            StimulusResult("threat", 0.5, "rule: threat", confidence=0.82, classifier_name="rule"),
            None,
        )
    )
    agent("the tool failed")
    summary = agent.last_turn_summary
    assert (summary["intensity"], summary["confidence"], summary["classifier"]) == (
        0.5,
        0.82,
        "rule",
    )
    agent("ok")
    summary = agent.last_turn_summary
    assert summary["intensity"] is None and summary["confidence"] is None
    assert summary["classifier"] is None
