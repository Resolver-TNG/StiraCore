"""#705: TurnEventRecorder counts the events it drops, per reason; nothing else changes."""

import logging
import threading
from types import SimpleNamespace

import pytest
from flask import Flask

from stiracore.observability.chat_api import create_chat_blueprint
from stiracore.observability.hooks import ObservabilityEvent
from stiracore.observability.turn_events import (
    DROP_REASONS,
    TurnEventRecorder,
    TurnEventStore,
    UIEventSink,
)

_LOGGER = "stiracore.observability.turn_events"
_CALL_ID = "0123456789abcdef0123456789abcdef"


@pytest.fixture
def recorder(tmp_path):
    return TurnEventRecorder(TurnEventStore(tmp_path / "t.db"))


def _drops(**counted):
    return {**dict.fromkeys(DROP_REASONS, 0), **counted}


def _warnings(caplog):
    return [record.getMessage() for record in caplog.records]


def test_a_new_recorder_has_every_reason_at_zero(recorder):
    assert set(DROP_REASONS) == {
        "no_active_turn",
        "invalid_event",
        "record_failed",
        "no_call_id",
        "sink_failed",
        "payload_failed",
    }
    assert recorder.drop_counts() == _drops()
    recorder.drop_counts()["no_active_turn"] = 9  # a copy: the recorder's own count stays
    assert recorder.drop_counts() == _drops()


def test_emit_without_an_active_turn_counts_no_active_turn(recorder, caplog):
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        assert recorder.emit("turn.started", {}) is None
    assert recorder.drop_counts() == _drops(no_active_turn=1)
    assert recorder.store.counts()["event_count"] == 0
    assert _warnings(caplog) == []  # as before: no log line on this branch


@pytest.mark.parametrize(
    ("event_type", "data"),
    [
        ("nope", {}),  # unknown type
        ("tool_call.created", {"tool_use_id": "u"}),  # required data missing
        ("turn.started", {"blob": "x" * 70000}),  # over the size limit
    ],
)
def test_an_event_the_contract_refuses_counts_invalid_event(recorder, caplog, event_type, data):
    recorder.active.set("t1")
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        assert recorder.emit(event_type, data) is None
    assert recorder.drop_counts() == _drops(invalid_event=1)
    assert _warnings(caplog) == [f"UI turn event dropped: {event_type}"]
    assert recorder.store.events("t1") == []


def test_a_store_failure_counts_record_failed(recorder, caplog):
    recorder.active.set("t1")
    recorder.store.close()  # the next write raises sqlite3.ProgrammingError
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        assert recorder.emit("turn.started", {}) is None
    assert recorder.drop_counts() == _drops(record_failed=1)
    assert _warnings(caplog) == ["UI turn event dropped: turn.started"]


@pytest.mark.parametrize("event_type", ["model.started", "model.completed"])
def test_a_model_event_without_a_call_id_counts_no_call_id(recorder, event_type):
    recorder.active.set("t1")
    UIEventSink(recorder, progress=False).emit(ObservabilityEvent(event_type=event_type))
    assert recorder.drop_counts() == _drops(no_call_id=1)
    assert recorder.store.events("t1") == []


def test_a_payload_the_store_cannot_keep_counts_payload_failed(recorder, caplog, monkeypatch):
    def broken(*args):
        raise OSError("disk")

    recorder.active.set("t1")
    monkeypatch.setattr(recorder.store, "put_payload", broken)
    captured = {"text": "x", "total_chars": 1, "total_lines": 1, "retained_truncated": False}
    with caplog.at_level(logging.WARNING, logger=_LOGGER):
        UIEventSink(recorder, progress=False).emit(
            ObservabilityEvent(
                event_type="tool.started",
                tool_name="x",
                tool_use_id="u1",
                attributes={"input": captured},
            )
        )
    assert recorder.drop_counts() == _drops(payload_failed=1)
    assert _warnings(caplog) == ["UI tool payload dropped"]
    (event,) = recorder.store.events("t1")  # the event itself is written, without the mark
    assert event["data"]["input_preview"] == "x" and "input_stored" not in event["data"]


def test_a_payload_that_is_not_attempted_counts_nothing(recorder):
    assert recorder.store_payload("u1", "input", {"text": "x"}) is False  # no active turn
    recorder.active.set("t1")
    assert recorder.store_payload("", "input", {"text": "x"}) is False
    assert recorder.store_payload("u1", "input", None) is False
    assert recorder.drop_counts() == _drops()


def test_a_sink_that_raises_counts_sink_failed_and_still_raises(recorder):
    recorder.active.set("t1")
    sink = UIEventSink(recorder, progress=False)
    broken = ObservabilityEvent(event_type="tool.started", tool_name="x", tool_use_id="u1")
    broken.timestamp = None  # the conversion raises (AttributeError)
    with pytest.raises(AttributeError):
        sink.emit(broken)
    assert recorder.drop_counts() == _drops(sink_failed=1)
    assert recorder.store.events("t1") == []


def test_a_sink_failure_under_the_hook_provider_is_counted_and_logged_as_before(recorder, caplog):
    from stiracore.observability.hooks import DashboardHookProvider

    recorder.active.set("t1")
    provider = DashboardHookProvider(UIEventSink(recorder, progress=False))
    broken = ObservabilityEvent(event_type="tool.completed", tool_name="x", tool_use_id="u1")
    broken.timestamp = None
    with caplog.at_level(logging.WARNING, logger="stiracore.observability.hooks"):
        provider._emit(broken)  # never raises into the agent
    assert _warnings(caplog) == ["Dashboard event sink failed; event dropped"]
    assert recorder.drop_counts() == _drops(sink_failed=1)


def test_append_to_writes_a_named_turn_and_counts_what_it_cannot_write(recorder):
    seen = []
    recorder.subscribe(seen.append)
    reply = {"report_id": "r1", "answer": "stop"}
    event = recorder.append_to("t0", "progress.reply", reply)  # no active turn is needed
    assert event["turn_id"] == "t0" and recorder.store.events("t0") == [event]
    assert seen == []  # as the direct store write it replaces: no subscriber
    assert recorder.drop_counts() == _drops()
    assert recorder.append_to("t0", "progress.reply", {"report_id": "r1"}) is None  # no answer
    assert recorder.drop_counts() == _drops(invalid_event=1)
    recorder.store.close()
    assert recorder.append_to("t0", "progress.reply", reply) is None
    assert recorder.drop_counts() == _drops(invalid_event=1, record_failed=1)


def test_recorded_events_count_nothing(recorder):
    seen = []
    recorder.subscribe(seen.append)
    recorder.subscribe(lambda event: 1 / 0)  # a failing subscriber: the event is still stored
    recorder.active.set("t1")
    sink = UIEventSink(recorder, progress=False)
    assert recorder.emit("turn.started", {}) is not None
    sink.emit(ObservabilityEvent(event_type="model.started", attributes={"call_id": _CALL_ID}))
    sink.emit(ObservabilityEvent(event_type="tool.started", tool_name="x", tool_use_id="u1"))
    sink.emit(
        ObservabilityEvent(
            event_type="tool.completed", tool_name="x", tool_use_id="u1", status="retry"
        )
    )
    sink.emit(ObservabilityEvent(event_type="model.completed", attributes={"call_id": _CALL_ID}))
    sink.emit(ObservabilityEvent(event_type="invocation.started"))  # not a turn event: ignored
    assert recorder.emit("turn.completed", {}) is not None
    events = recorder.store.events("t1")
    assert [event["type"] for event in events] == [
        "turn.started",
        "model.started",
        "tool_call.created",
        "tool_call.updated",
        "model.completed",
        "turn.completed",
    ]
    assert events[3]["data"]["status"] == "running"  # a retry is not an end (TOOL_STATUSES note)
    assert len(seen) == len(events)
    assert recorder.drop_counts() == _drops()


def test_a_normal_chat_turn_counts_nothing(recorder):
    app = Flask(__name__)
    app.register_blueprint(
        create_chat_blueprint(
            SimpleNamespace(),
            runner=lambda message: iter(["ok"]),
            turn_summary_provider=lambda: {"category": "praise", "breaker": "normal"},
            turn_recorder=recorder,
        )
    )
    app.test_client().post("/api/chat", json={"message": "hi"}).get_data(True)
    assert recorder.store.counts()["event_count"] >= 3  # started, summary, completed
    assert recorder.drop_counts() == _drops()


def test_drops_from_several_threads_are_all_counted(recorder):
    threads = [
        threading.Thread(target=lambda: [recorder.emit("turn.started", {}) for _ in range(50)])
        for _ in range(8)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert recorder.drop_counts() == _drops(no_active_turn=400)
