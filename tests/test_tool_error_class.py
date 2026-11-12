"""#696: ``error_class`` / ``deny_code`` on a finished tool call (fixed codes, no text)."""

import time
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from pydantic import BaseModel, ValidationError
from strands import Agent, tool

from stiracore.agent.checkpoint_hooks import NEVER_REFUSAL
from stiracore.observability import hooks as hooks_module
from stiracore.observability.hooks import (
    PROGRESS_DECLARED_KEY,
    TOOL_DENY_CODES,
    TOOL_ERROR_CLASSES,
    DashboardHookProvider,
    InMemoryEventSink,
    ObservabilityEvent,
)
from stiracore.observability.turn_events import (
    SCHEMA_VERSION,
    TurnEventRecorder,
    TurnEventStore,
    UIEventSink,
    validate_event,
)


@tool
def probe_count(count: int) -> dict:
    """Count.

    Args:
        count: a number
    """
    if count < 0:
        raise RuntimeError("PRIVATE boom")
    if count == 0:
        return {"status": "error", "content": [{"text": "PRIVATE upstream API failed"}]}
    return {"status": "success", "content": [{"text": str(count)}]}


def _completed(sink):
    return [e for e in sink.get_events(limit=100) if e.event_type == "tool.completed"]


def test_the_sdk_paths_are_classified():
    """Through the SDK: schema refusal, a raising tool, an error result, a success."""
    sink = InMemoryEventSink()
    agent = Agent(
        model=Mock(),
        tools=[probe_count],
        callback_handler=None,
        hooks=[DashboardHookProvider(sink)],
    )
    for kwargs in ({"count": "PRIVATE text"}, {}, {"count": -1}, {"count": 0}, {"count": 2}):
        agent.tool.probe_count(**kwargs)
    events = _completed(sink)
    assert [e.attributes.get("error_class") for e in events] == [
        "invalid_input",
        "invalid_input",
        "exception",
        "tool_failed",
        None,
    ]
    assert all("deny_code" not in e.attributes for e in events)
    assert [e.status for e in events] == ["error"] * 4 + ["success"]  # unchanged
    assert "PRIVATE" not in repr([asdict(e) for e in events])


class _Upstream(BaseModel):
    total: int


@tool
def probe_upstream(count: int) -> dict:
    """Read an outside API and check its answer.

    Args:
        count: a number
    """
    try:
        _Upstream.model_validate({"total": "PRIVATE answer"})
    except ValidationError as exc:
        raise ValueError("PRIVATE upstream answer was malformed") from exc
    return {"status": "success", "content": [{"text": str(count)}]}


def test_a_tools_own_validation_error_is_an_exception_not_invalid_input():
    """Through the SDK: the tool body wraps pydantic's ValidationError in a ValueError
    (same types as the SDK's schema refusal); only the raising frame tells them apart."""
    sink = InMemoryEventSink()
    agent = Agent(
        model=Mock(),
        tools=[probe_upstream, probe_count],
        callback_handler=None,
        hooks=[DashboardHookProvider(sink)],
    )
    agent.tool.probe_upstream(count=1)
    agent.tool.probe_upstream(count="PRIVATE text")  # refused before the body runs
    agent.tool.probe_count(count="PRIVATE text")
    events = _completed(sink)
    assert [e.attributes.get("error_class") for e in events] == [
        "exception",
        "invalid_input",
        "invalid_input",
    ]
    assert "PRIVATE" not in repr([asdict(e) for e in events])


def test_the_same_types_without_the_sdks_frame_are_an_exception():
    """No traceback (nothing says who raised): the safe side, not ``invalid_input``."""
    try:
        _Upstream.model_validate({"total": "PRIVATE"})
    except ValidationError as exc:
        cause = exc
    error = ValueError("PRIVATE")
    error.__cause__ = cause
    sink = InMemoryEventSink()
    _after(DashboardHookProvider(sink), exception=error, selected_tool=object())
    assert [e.attributes.get("error_class") for e in _completed(sink)] == ["exception"]


def _after(provider, state=None, **over):
    fields = {
        "agent": SimpleNamespace(),
        "invocation_state": {} if state is None else state,
        "tool_use": {"toolUseId": "u", "name": "x"},
        "exception": None,
        "retry": False,
        "cancel_message": None,
        "result": {"status": "error", "content": [{"text": "PRIVATE"}]},
    }
    fields.update(over)
    provider.on_after_tool_call(SimpleNamespace(**fields))


def test_unknown_tool_refusals_and_retry():
    sink = InMemoryEventSink()
    provider = DashboardHookProvider(sink)
    _after(provider, selected_tool=None)  # the SDK found no such tool
    _after(provider, selected_tool=object())
    _after(provider)  # an event without the field: not "unknown"
    _after(provider, cancel_message=NEVER_REFUSAL, selected_tool=None)
    _after(provider, cancel_message="PRIVATE reason written by a gate")
    _after(provider, {PROGRESS_DECLARED_KEY: {"u"}}, cancel_message="PRIVATE checkpoint")
    _after(provider, exception=RuntimeError("PRIVATE"), retry=True)
    _after(provider, exception=ValueError("PRIVATE"))  # a ValueError of the tool's own
    events = _completed(sink)
    assert [(e.attributes.get("error_class"), e.attributes.get("deny_code")) for e in events] == [
        ("unknown_tool", None),
        ("tool_failed", None),
        ("tool_failed", None),
        ("refused", "rule_never"),
        ("refused", "other"),
        ("refused", "declared"),
        (None, None),
        ("exception", None),
    ]
    for event in events:
        assert event.attributes.get("error_class") in TOOL_ERROR_CLASSES | {None}
        assert event.attributes.get("deny_code") in TOOL_DENY_CODES | {None}
    assert "PRIVATE" not in repr([asdict(e) for e in events])


def test_rule_never_is_the_gates_sentence_not_a_prefix():
    """A text that only begins like the gate's sentence is ``other``."""
    sentence = hooks_module._NEVER_SENTENCE
    assert sentence.endswith(".") and NEVER_REFUSAL.startswith(sentence + " ")
    sink = InMemoryEventSink()
    provider = DashboardHookProvider(sink)
    texts = (
        NEVER_REFUSAL,
        sentence,
        "Tool policy is Never for this toolchain; PRIVATE",
        "Tool policy is Never for this tool",
        sentence + "PRIVATE",
        "PRIVATE " + NEVER_REFUSAL,
    )
    for text in texts:
        _after(provider, cancel_message=text)
    events = _completed(sink)
    assert [e.attributes.get("deny_code") for e in events] == ["rule_never"] * 2 + ["other"] * 4
    assert "PRIVATE" not in repr([asdict(e) for e in events])


def test_ui_sink_writes_the_codes_and_drops_other_values(tmp_path):
    recorder = TurnEventRecorder(TurnEventStore(tmp_path / "t.db"))
    sink = UIEventSink(recorder)
    recorder.active.set("t1")
    for status, attributes in (
        ("cancelled", {"error_class": "refused", "deny_code": "rule_never"}),
        ("error", {"error_class": "tool_failed"}),
        ("error", {"error_class": "Ignore previous instructions", "deny_code": "PRIVATE"}),
        ("error", {"error_class": ["refused"], "deny_code": 7}),
        ("success", {}),
    ):
        sink.emit(
            ObservabilityEvent(
                event_type="tool.completed",
                tool_name="x",
                tool_use_id="u",
                status=status,
                attributes=attributes,
            )
        )
    data = [e["data"] for e in recorder.store.events("t1")]
    assert len(data) == 5  # an out-of-set value costs the field, not the event
    assert recorder.drop_counts()["invalid_event"] == 0
    assert (data[0]["status"], data[0]["error_class"], data[0]["deny_code"]) == (
        "denied",
        "refused",
        "rule_never",
    )
    assert data[1]["error_class"] == "tool_failed" and "deny_code" not in data[1]
    for rest in data[2:]:
        assert "error_class" not in rest and "deny_code" not in rest
    assert "PRIVATE" not in repr(data) and "Ignore" not in repr(data)


def _tool_event(**data):
    return {
        "schema_version": SCHEMA_VERSION,
        "event_id": "a" * 32,
        "turn_id": "t1",
        "seq": 1,
        "ts": time.time(),
        "type": "tool_call.updated",
        "data": {"tool_use_id": "u", "name": "x", "status": "error", **data},
    }


def test_the_contract_refuses_a_value_outside_the_sets():
    assert validate_event(_tool_event())  # the fields are optional
    for code in sorted(TOOL_ERROR_CLASSES):
        assert validate_event(_tool_event(error_class=code))
    for code in sorted(TOOL_DENY_CODES):
        assert validate_event(_tool_event(error_class="refused", deny_code=code))
    for bad in (
        {"error_class": "the API said: PRIVATE"},
        {"error_class": None},
        {"error_class": ["refused"]},
        {"deny_code": "free text"},
        {"deny_code": 1},
    ):
        with pytest.raises(ValueError):
            validate_event(_tool_event(**bad))
