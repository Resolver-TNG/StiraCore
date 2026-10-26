from unittest.mock import MagicMock, Mock

import pytest

from stiracore.agent import nxp_agent
from stiracore.core.engine import BreakAction, BreakLevel, BreakResult


@pytest.fixture
def lifecycle(monkeypatch):
    monkeypatch.setattr(nxp_agent, "SoulCompactionGuard", Mock())
    engine = MagicMock(name="engine")
    engine.pre_turn.return_value = BreakResult(BreakLevel.NORMAL, BreakAction.NONE)
    engine.blocked_tools.return_value = set()
    monkeypatch.setattr(nxp_agent, "StiraEngine", Mock(return_value=engine))
    monkeypatch.setattr(nxp_agent, "NeuralState", MagicMock())
    model = Mock(name="offline_model")
    monkeypatch.setattr(nxp_agent, "BedrockModel", Mock(return_value=model))

    instances = []
    failure = {"exception": None}

    def build_agent(**kwargs):
        inner = MagicMock(name=f"inner_agent_{len(instances)}")
        inner.messages = list(kwargs.get("messages") or [])

        def run(*args, **call_kwargs):
            if failure["exception"] is not None:
                raise failure["exception"]
            inner.messages.append({"role": "assistant", "content": [{"text": "offline reply"}]})
            result = MagicMock()
            result.__str__.return_value = "offline reply"
            return result

        inner.side_effect = run
        instances.append(inner)
        return inner

    constructor = Mock(side_effect=build_agent)
    monkeypatch.setattr(nxp_agent, "Agent", constructor)

    def file_read():
        return "read"

    file_read.tool_spec = {"name": "file_read"}

    def file_write():
        return "write"

    file_write.tool_spec = {"name": "file_write"}

    wrapper = nxp_agent.NXPAgent(
        model_id="offline-model",
        region_name="us-east-1",
        system_prompt="Keep the configured identity.",
        tools=[file_read, file_write],
    )
    engine.reset_mock()
    constructor.reset_mock()
    return wrapper, engine, constructor, instances, failure


@pytest.mark.parametrize("raises", [False, True])
def test_call_runs_post_turn_in_finally(lifecycle, raises):
    wrapper, engine, constructor, instances, failure = lifecycle
    error = RuntimeError("mock agent failure")
    if raises:
        failure["exception"] = error
        with pytest.raises(RuntimeError) as caught:
            wrapper("hello")
        assert caught.value is error
    else:
        wrapper("hello")

    engine.pre_turn.assert_called_once()
    engine.post_turn.assert_called_once()
    constructor.assert_called_once()
    instances[-1].assert_called_once()


def test_halt_runs_model_with_write_tools_for_operation_level_hitl(lifecycle):
    wrapper, engine, constructor, instances, _ = lifecycle
    engine.pre_turn.return_value = BreakResult(
        BreakLevel.CRITICAL,
        BreakAction.HALT | BreakAction.BLOCK_WRITES,
    )
    wrapper._stimulus_classifier = Mock(classify=lambda **kwargs: None)
    wrapper._classifier_resolved = True

    wrapper("gate writes individually")

    constructor.assert_called_once()
    assert len(instances) == 1
    assert {nxp_agent._tool_name(tool) for tool in constructor.call_args.kwargs["tools"]} == {
        "file_read",
        "file_write",
    }


def test_agent_is_rebuilt_and_write_tools_recover(lifecycle):
    wrapper, engine, constructor, instances, _ = lifecycle
    engine.pre_turn.side_effect = [
        BreakResult(BreakLevel.NORMAL, BreakAction.NONE),
        BreakResult(
            BreakLevel.WARNING,
            BreakAction.BLOCK_WRITES | BreakAction.REQUIRE_CONFIRMATION,
        ),
        BreakResult(BreakLevel.NORMAL, BreakAction.NONE),
    ]

    for prompt in ("first", "restricted", "recovered"):
        wrapper(prompt)

    assert constructor.call_count == 3
    assert len({id(inner) for inner in instances}) == 3
    tool_sets = [
        {nxp_agent._tool_name(tool) for tool in call.kwargs["tools"]}
        for call in constructor.call_args_list
    ]
    assert tool_sets == [
        {"file_read", "file_write"},
        {"file_read"},
        {"file_read", "file_write"},
    ]
    assert engine.post_turn.call_count == 3


def test_injected_classifier_drives_post_turn(lifecycle):
    """An injected classifier's result is fed to post_turn (the #65 wiring)."""
    wrapper, engine, _constructor, _instances, _failure = lifecycle

    class StubClassifier:
        def __init__(self):
            self.calls = []

        def classify(self, user_message, agent_response="", tool_results=None, breaker_result=None):
            self.calls.append(user_message)
            return ("connection", 0.5)

    stub = StubClassifier()
    wrapper._stimulus_classifier = stub
    wrapper._classifier_resolved = True

    wrapper("ありがとう")

    assert stub.calls == ["ありがとう"]
    # A legacy (category, intensity) tuple is still accepted (#75 back-compat).
    engine.post_turn.assert_called_once_with(
        stimulus="connection",
        intensity=0.5,
        transcription="StubClassifier: connection",
    )


def test_classifier_abstain_yields_neutral_post_turn(lifecycle):
    """When the classifier abstains (None), post_turn gets no stimulus."""
    wrapper, engine, _constructor, _instances, _failure = lifecycle

    class AbstainClassifier:
        def classify(self, user_message, agent_response="", tool_results=None, breaker_result=None):
            return None

    wrapper._stimulus_classifier = AbstainClassifier()
    wrapper._classifier_resolved = True

    wrapper("ちょっと確認して")

    engine.post_turn.assert_called_once_with(stimulus=None, intensity=0.0)


def test_injected_classifier_is_not_overridden_by_resolution(lifecycle, monkeypatch):
    """A classifier passed at construction must not be replaced by get_classifier."""
    wrapper, _engine, _constructor, _instances, _failure = lifecycle

    def boom():  # get_classifier must never be called when one was injected
        raise AssertionError("get_classifier should not run for an injected classifier")

    monkeypatch.setattr("stiracore.agent.stimulus_classifier.get_classifier", boom)

    class StubClassifier:
        def classify(self, *a, **k):
            return None

    wrapper._stimulus_classifier = StubClassifier()
    wrapper._classifier_resolved = True

    wrapper("hello")  # must not raise


def test_factory_passes_injected_classifier(monkeypatch):
    from stiracore.agent.nxp_agent import create_nxp_agent

    monkeypatch.setattr("stiracore.agent.nxp_agent.BedrockModel", MagicMock())
    classifier = MagicMock()
    wrapper = create_nxp_agent("offline-model", "us-east-1", stimulus_classifier=classifier)
    assert wrapper._get_classifier() is classifier


def test_default_classifier_is_resolved_once(lifecycle, monkeypatch):
    wrapper, engine, _constructor, _instances, _failure = lifecycle
    classifier = MagicMock()
    classifier.classify.return_value = ("joy", 0.4)
    factory = MagicMock(return_value=classifier)
    monkeypatch.setattr("stiracore.agent.stimulus_classifier.get_classifier", factory)
    wrapper("first")
    wrapper("second")
    factory.assert_called_once_with()
    assert classifier.classify.call_count == 2
    assert engine.post_turn.call_count == 2


def test_invalid_classifier_value_logs_and_finishes_turn(lifecycle, caplog):
    wrapper, engine, _constructor, _instances, _failure = lifecycle
    classifier = MagicMock()
    classifier.classify.side_effect = ValueError("bad result")
    wrapper._stimulus_classifier = classifier
    wrapper._classifier_resolved = True
    wrapper("hello")
    engine.post_turn.assert_called_once_with(stimulus=None, intensity=0.0)
    assert "Stimulus detection failed" in caplog.text
