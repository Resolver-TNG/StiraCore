"""Offline tests: no agents, model clients, credentials, or network calls."""

from concurrent.futures import ThreadPoolExecutor
from contextvars import Context
from dataclasses import asdict
from datetime import timedelta
from types import SimpleNamespace
from uuid import UUID

import pytest
from strands.hooks import HookProvider
from strands.hooks.events import (
    AfterInvocationEvent,
    AfterModelCallEvent,
    AfterToolCallEvent,
    BeforeInvocationEvent,
    BeforeModelCallEvent,
    BeforeToolCallEvent,
    MessageAddedEvent,
)

from stiracore.observability import (
    DashboardHookProvider,
    InMemoryEventSink,
    ObservabilityEvent,
    create_dashboard_hooks,
)
from stiracore.observability import hooks as hooks_module


@pytest.fixture
def setup_hooks():
    sink = InMemoryEventSink()
    provider = create_dashboard_hooks(sink)
    agent = SimpleNamespace(model=SimpleNamespace(config={"model_id": "offline-model"}))
    state = {}

    def event(**kwargs):
        return SimpleNamespace(
            agent=agent,
            invocation_state=state,
            **kwargs,
        )

    return provider, sink, agent, event


def test_registration():
    class Registry:
        def __init__(self):
            self.callbacks = {}

        def add_callback(self, event_type, callback):
            self.callbacks[event_type] = callback

    provider = create_dashboard_hooks(InMemoryEventSink())
    registry = Registry()
    provider.register_hooks(registry)

    assert isinstance(provider, HookProvider)
    assert isinstance(provider, DashboardHookProvider)
    assert registry.callbacks == {
        BeforeInvocationEvent: provider.on_before_invocation,
        AfterInvocationEvent: provider.on_after_invocation,
        BeforeModelCallEvent: provider.on_before_model_call,
        AfterModelCallEvent: provider.on_after_model_call,
        BeforeToolCallEvent: provider.on_before_tool_call,
        AfterToolCallEvent: provider.on_after_tool_call,
        MessageAddedEvent: provider.on_message_added,
    }


def test_model_and_invocation_lifecycle(setup_hooks, monkeypatch):
    provider, sink, _, event = setup_hooks
    times = iter([10.0, 10.1, 10.4, 10.8])
    monkeypatch.setattr(hooks_module, "perf_counter", lambda: next(times))

    provider.on_before_invocation(event(messages=[], cancel=False))
    provider.on_before_model_call(event(projected_input_tokens=27, cancel=False))
    provider.on_after_model_call(
        event(
            stop_response={
                "stop_reason": "end_turn",
                "message": {"content": [{"text": "PRIVATE RESPONSE"}]},
                "usage": {"inputTokens": 30, "outputTokens": 8, "totalTokens": 38},
            },
            exception=None,
            retry=False,
        )
    )
    provider.on_after_invocation(
        event(
            result=SimpleNamespace(
                stop_reason="end_turn",
                metrics=SimpleNamespace(accumulated_usage={"inputTokens": 30, "outputTokens": 8}),
                message={"content": [{"text": "PRIVATE RESULT"}]},
            ),
            resume=False,
        )
    )

    events = sink.get_events()
    assert [item.event_type for item in events] == [
        "invocation.started",
        "model.started",
        "model.completed",
        "invocation.completed",
    ]
    assert len({item.turn_id for item in events}) == 1
    assert events[0].turn_id is not None
    assert events[1].attributes["projected_input_tokens"] == 27
    assert events[2].duration_ms == pytest.approx(300)
    assert events[3].duration_ms == pytest.approx(800)
    assert events[2].attributes["stop_response_present"] is True
    assert events[2].attributes["stop_reason"] == "end_turn"
    assert events[3].attributes["result_present"] is True
    assert (
        events[2].usage
        == events[3].usage
        == {
            "inputTokens": 30,
            "outputTokens": 8,
        }
    )
    assert all(item.model_id == "offline-model" for item in events)
    assert all(item.status == "success" for item in events)
    assert "PRIVATE" not in repr([asdict(item) for item in events])
    assert not provider._turns

    for item in events:
        assert UUID(item.event_id).version == 4
        assert item.timestamp.utcoffset() == timedelta(0)
    assert len({item.event_id for item in events}) == len(events)


def test_model_retry_does_not_reuse_previous_start(setup_hooks, monkeypatch):
    provider, sink, _, event = setup_hooks
    times = iter([1.0, 1.2, 2.0, 2.5])
    monkeypatch.setattr(hooks_module, "perf_counter", lambda: next(times))

    provider.on_before_model_call(event(projected_input_tokens=0))
    provider.on_after_model_call(
        event(stop_response=None, exception=RuntimeError("PRIVATE"), retry=True)
    )
    provider.on_before_model_call(event(projected_input_tokens=10))
    provider.on_after_model_call(
        event(stop_response={"stop_reason": "end_turn"}, exception=None, retry=False)
    )

    failed, completed = sink.get_events()[1::2]
    assert failed.status == "retry"
    assert failed.duration_ms == pytest.approx(200)
    assert failed.attributes["exception_type"] == "RuntimeError"
    assert failed.attributes["retry"] is True
    assert completed.status == "success"
    assert completed.duration_ms == pytest.approx(500)
    assert "PRIVATE" not in repr(asdict(failed))


def test_parallel_tools_are_timed_by_tool_use_id(setup_hooks, monkeypatch):
    provider, sink, _, event = setup_hooks
    times = iter([1.0, 1.1, 1.3, 1.8])
    monkeypatch.setattr(hooks_module, "perf_counter", lambda: next(times))
    first = {"name": "lookup", "toolUseId": "first", "input": {"secret": "PRIVATE"}}
    second = {"name": "lookup", "toolUseId": "second", "input": {}}

    provider.on_before_tool_call(event(tool_use=first, cancel_tool=False))
    provider.on_before_tool_call(event(tool_use=second, cancel_tool=False))
    provider.on_after_tool_call(
        event(
            tool_use=second,
            result={"status": "success", "content": [{"text": "PRIVATE"}]},
            exception=None,
            retry=False,
            cancel_message=None,
        )
    )
    provider.on_after_tool_call(
        event(
            tool_use=first,
            result={"status": "error", "content": [{"text": "PRIVATE"}]},
            exception=ValueError("PRIVATE"),
            retry=False,
            cancel_message=None,
        )
    )

    second_done, first_done = sink.get_events()[2:]
    assert second_done.tool_use_id == "second"
    assert second_done.duration_ms == pytest.approx(200)
    assert second_done.status == "success"
    assert first_done.tool_use_id == "first"
    assert first_done.tool_name == "lookup"
    assert first_done.duration_ms == pytest.approx(800)
    assert first_done.status == "error"
    assert first_done.attributes["result_status"] == "error"
    assert first_done.attributes["exception_type"] == "ValueError"
    assert "PRIVATE" not in repr([asdict(item) for item in sink.get_events()])


@pytest.mark.parametrize(
    ("retry", "cancel_message", "expected"),
    [(True, None, "retry"), (False, "PRIVATE cancellation reason", "cancelled")],
)
def test_tool_retry_and_cancellation(setup_hooks, retry, cancel_message, expected):
    provider, sink, _, event = setup_hooks
    tool_use = {"name": "lookup", "toolUseId": "one"}
    provider.on_before_tool_call(event(tool_use=tool_use))
    provider.on_after_tool_call(
        event(
            tool_use=tool_use,
            result=None,
            exception=RuntimeError("PRIVATE"),
            retry=retry,
            cancel_message=cancel_message,
        )
    )
    latest = sink.get_latest()
    assert latest.status == expected
    assert latest.attributes["retry"] is retry
    assert "PRIVATE" not in repr(asdict(latest))


def test_messages_capture_only_role_and_content_types(setup_hooks):
    provider, sink, agent, event = setup_hooks
    provider.on_before_invocation(event(cancel=False))
    turn_id = sink.get_latest().turn_id
    provider.on_message_added(
        SimpleNamespace(
            agent=agent,
            message={
                "role": "assistant",
                "content": [
                    {"text": "PRIVATE"},
                    {"toolUse": {"name": "PRIVATE", "input": {"key": "PRIVATE"}}},
                    {"image": {"source": {"bytes": b"PRIVATE"}}},
                    {"PRIVATE_CUSTOM_KEY": "PRIVATE"},
                ],
            },
        )
    )
    latest = sink.get_latest()
    assert latest.event_type == "message.added"
    assert latest.turn_id == turn_id
    assert latest.attributes == {
        "role": "assistant",
        "content_types": ["image", "text", "toolUse", "unknown"],
    }
    assert "PRIVATE" not in repr(asdict(latest))

    provider.on_after_invocation(event(result={"stop_reason": "end_turn"}))
    provider.on_message_added(SimpleNamespace(agent=agent, message={"role": "user", "content": []}))
    assert sink.get_latest().turn_id is None


def test_concurrent_invocations_have_independent_turns(setup_hooks):
    provider, sink, agent, _ = setup_hooks
    first_state, second_state = {}, {}
    first_context, second_context = Context(), Context()

    def event(state, **kwargs):
        return SimpleNamespace(agent=agent, invocation_state=state, **kwargs)

    first_context.run(provider.on_before_invocation, event(first_state))
    first_id = sink.get_latest().turn_id
    second_context.run(provider.on_before_invocation, event(second_state))
    second_id = sink.get_latest().turn_id
    assert first_id != second_id

    first_context.run(
        provider.on_message_added,
        SimpleNamespace(agent=agent, message={"role": "user", "content": []}),
    )
    assert sink.get_latest().turn_id == first_id

    # With no execution-context association, multiple active turns are ambiguous.
    Context().run(
        provider.on_message_added,
        SimpleNamespace(agent=agent, message={"role": "user", "content": []}),
    )
    assert sink.get_latest().turn_id is None

    for context, state, expected in (
        (second_context, second_state, second_id),
        (first_context, first_state, first_id),
    ):
        context.run(
            provider.on_after_invocation,
            event(state, result={"stop_reason": "end_turn"}),
        )
        assert sink.get_latest().turn_id == expected
    assert not provider._turns


def test_completion_without_start_has_no_fabricated_duration(setup_hooks):
    provider, sink, _, event = setup_hooks
    provider.on_after_model_call(
        event(stop_response=None, exception=RuntimeError("PRIVATE"), retry=False)
    )
    latest = sink.get_latest()
    assert latest.duration_ms is None
    assert latest.turn_id is None
    assert latest.status == "error"


def test_cancelled_invocation(setup_hooks):
    provider, sink, _, event = setup_hooks
    provider.on_before_invocation(event(cancel=True))
    provider.on_after_invocation(event(result=None, resume=False))
    assert sink.get_latest().status == "cancelled"
    assert sink.get_latest().attributes["result_present"] is False


def test_sink_pagination_capacity_and_clear():
    sink = InMemoryEventSink()
    assert sink.get_latest() is None
    assert sink.get_events() == []

    events = [ObservabilityEvent(event_type="test") for _ in range(1005)]
    for event in events:
        sink.emit(event)

    retained = sink.get_events(limit=2000)
    assert retained == events[5:]
    assert sink.get_latest() is events[-1]
    assert sink.get_events() == events[5:55]
    assert sink.get_events(since_id=events[10].event_id, limit=2) == events[11:13]
    assert sink.get_events(since_id=events[-1].event_id) == []
    assert sink.get_events(since_id=events[0].event_id, limit=1) == events[5:6]
    assert sink.get_events(since_id="unknown", limit=1) == events[5:6]
    assert sink.get_events(limit=0) == []
    assert sink.get_events(limit=-1) == []

    sink.clear()
    assert sink.get_latest() is None
    assert sink.get_events() == []


def test_sink_is_thread_safe():
    sink = InMemoryEventSink()

    def writer(worker):
        for index in range(200):
            sink.emit(
                ObservabilityEvent(
                    event_type="test",
                    attributes={"worker": worker, "index": index},
                )
            )
            sink.get_events(limit=3)
            sink.get_latest()

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(writer, range(8)))

    events = sink.get_events(limit=2000)
    assert len(events) == 1000
    assert len({event.event_id for event in events}) == 1000


def test_sink_failure_does_not_interrupt_hooks_or_log_payload(caplog):
    class BrokenSink:
        def emit(self, event):
            raise RuntimeError("PRIVATE sink failure")

    provider = create_dashboard_hooks(BrokenSink())
    provider.on_message_added(
        SimpleNamespace(
            agent=SimpleNamespace(model=None),
            message={"role": "user", "content": [{"text": "PRIVATE"}]},
        )
    )
    assert "Dashboard event sink failed" in caplog.text
    assert "PRIVATE" not in caplog.text
