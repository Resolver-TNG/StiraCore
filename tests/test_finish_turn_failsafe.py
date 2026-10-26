"""Fail-safe tests for NXPAgent._finish_turn classifier errors (#141)."""

from unittest.mock import MagicMock

import pytest

from stiracore.agent import nxp_agent as nxp_mod


class _RaisingClassifier:
    name = "raising"

    def __init__(self, exc: BaseException) -> None:
        self.exc = exc
        self.calls = 0

    def classify(self, **kwargs):
        self.calls += 1
        raise self.exc


class _Result:
    stop_reason = "end_turn"

    def __str__(self) -> str:
        return "agent says hi"


def _make_agent(monkeypatch, classifier, agent_behavior=None):
    monkeypatch.setattr(nxp_mod, "BedrockModel", MagicMock())
    inner = MagicMock()
    inner.messages = []
    if agent_behavior is not None:
        inner.side_effect = agent_behavior
    else:
        inner.return_value = _Result()
    monkeypatch.setattr(nxp_mod, "Agent", MagicMock(return_value=inner))
    agent = nxp_mod.NXPAgent(model_id="m", region_name="us-east-1", stimulus_classifier=classifier)
    engine = MagicMock()
    engine.pre_turn.return_value = MagicMock(name="breaker")
    engine.post_turn.return_value = {"deltas": {}}
    agent.engine = engine
    monkeypatch.setattr(agent, "_prepare_turn", lambda breaker: ([], ""))
    monkeypatch.setattr(agent, "_snapshot_context", lambda a, p: None)
    return agent, engine


@pytest.mark.parametrize(
    "exc",
    [
        RuntimeError("boom"),
        ConnectionError("down"),
        ImportError("no sokudan"),
        TypeError("bad"),
        ValueError("bad"),
    ],
)
def test_classifier_failure_falls_back_and_post_turn_called_once(monkeypatch, exc):
    classifier = _RaisingClassifier(exc)
    agent, engine = _make_agent(monkeypatch, classifier)

    result = agent("hello")

    assert isinstance(result, _Result)
    assert classifier.calls == 1
    engine.post_turn.assert_called_once_with(stimulus=None, intensity=0.0)
    summary = agent.last_turn_summary
    assert summary is not None
    assert summary["category"] is None
    assert summary["emotion_transcription"] is None


def test_original_exception_not_masked_by_classifier(monkeypatch):
    classifier = _RaisingClassifier(RuntimeError("classifier"))

    def _fail(prompt):
        raise ConnectionError("model call failed")

    agent, engine = _make_agent(monkeypatch, classifier, agent_behavior=_fail)

    with pytest.raises(ConnectionError, match="model call failed"):
        agent("hello")
    engine.post_turn.assert_called_once_with(stimulus=None)
    assert classifier.calls == 0


def test_finish_turn_direct_runtime_error(monkeypatch):
    classifier = _RaisingClassifier(RuntimeError("boom"))
    agent, engine = _make_agent(monkeypatch, classifier)

    agent._finish_turn("hi", _Result(), MagicMock(), completed=True)

    engine.post_turn.assert_called_once_with(stimulus=None, intensity=0.0)


def test_keyboard_interrupt_still_propagates(monkeypatch):
    classifier = _RaisingClassifier(KeyboardInterrupt())
    agent, _engine = _make_agent(monkeypatch, classifier)

    with pytest.raises(KeyboardInterrupt):
        agent._finish_turn("hi", _Result(), MagicMock(), completed=True)
