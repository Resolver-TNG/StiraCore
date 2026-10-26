import json
import threading
import time
from types import SimpleNamespace

import pytest
from flask import Flask

from stiracore.core import HITLAction, HITLDecision
from stiracore.observability.chat_api import HITLApprovalBroker, create_chat_blueprint
from stiracore.observability.hooks import ObservabilityEvent
from stiracore.observability.turn_events import (
    PREVIEW_LIMIT,
    SCHEMA_VERSION,
    TurnEventRecorder,
    TurnEventStore,
    UIEventSink,
    validate_event,
)


def _event(**overrides):
    event = {
        "schema_version": SCHEMA_VERSION,
        "event_id": "a" * 32,
        "turn_id": "t1",
        "seq": 1,
        "ts": time.time(),
        "type": "turn.started",
        "data": {},
    }
    event.update(overrides)
    return event


_INJECTED = {
    "source": "associative_recall",
    "schema": 2,
    "status": "no_terms",
    "used": [],
    "dropped": [],
    "associations": 0,
    "unranked": 0,
    "discarded": 0,
    "chars": 0,
    "signals": [],
}


def test_validate_ok_and_invalid():
    assert validate_event(_event())
    assert validate_event(_event(type="memory.injected", data=dict(_INJECTED)))
    for bad in (
        _event(type="nope"),
        _event(type="memory.injected"),  # data outside the allow-list (#170)
        _event(seq=0),
        _event(schema_version=2),
        _event(data=[]),
        _event(turn_id=""),
        _event(type="tool_call.created", data={"tool_use_id": "x", "name": "n", "status": "?"}),
        _event(data={"blob": "x" * 70000}),
    ):
        with pytest.raises(ValueError):
            validate_event(bad)
    with pytest.raises(ValueError):
        validate_event({"type": "turn.started"})


def test_preview_truncated_and_masked(tmp_path):
    store = TurnEventStore(tmp_path / "t.db")
    event = store.append(
        "t1",
        "tool_call.created",
        {
            "tool_use_id": "u",
            "name": "n",
            "status": "running",
            "input_preview": {"api_key": "zzz", "q": "Bearer abc.def " + "x" * 3000},
            "output_preview": "AKIAABCDEFGHIJKLMNOP",
        },
    )
    data = event["data"]
    assert data["truncated"] is True
    assert len(data["input_preview"]) == PREVIEW_LIMIT
    assert "zzz" not in data["input_preview"] and "abc.def" not in data["input_preview"]
    assert "ABCDEFGHIJKLMNOP" not in data["output_preview"]


def test_seq_monotonic_threadsafe_and_replay(tmp_path):
    path = tmp_path / "t.db"
    store = TurnEventStore(path)
    threads = [
        threading.Thread(target=lambda: [store.append("t1", "turn.started", {}) for _ in range(20)])
        for _ in range(5)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    seqs = [e["seq"] for e in store.events("t1")]
    assert seqs == list(range(1, 101))
    store.close()
    reopened = TurnEventStore(path)
    assert [e["seq"] for e in reopened.events("t1", after_seq=95)] == [96, 97, 98, 99, 100]
    assert reopened.append("t1", "turn.completed", {})["seq"] == 101


def test_ui_event_sink_tool_conversion(tmp_path):
    recorder = TurnEventRecorder(TurnEventStore(tmp_path / "t.db"))
    sink = UIEventSink(recorder)
    sink.emit(ObservabilityEvent(event_type="tool.started", tool_name="x", tool_use_id="u1"))
    assert recorder.store.recent_turns(5) == []  # no active turn: dropped
    recorder.active.set("t1")
    sink.emit(ObservabilityEvent(event_type="tool.started", tool_name="x", tool_use_id="u1"))
    sink.emit(
        ObservabilityEvent(
            event_type="tool.completed",
            tool_name="x",
            tool_use_id="u1",
            duration_ms=12.0,
            status="error",
        )
    )
    sink.emit(ObservabilityEvent(event_type="model.started"))
    events = recorder.store.events("t1")
    assert [e["type"] for e in events] == ["tool_call.created", "tool_call.updated"]
    assert events[0]["data"]["status"] == "running"
    assert events[1]["data"]["status"] == "error"
    assert events[1]["data"]["duration_ms"] == 12.0


def test_ui_event_sink_carries_a_tool_failure_code(tmp_path):
    """#584: ``result_code`` (a short identifier) reaches tool_call.updated; absent otherwise."""
    recorder = TurnEventRecorder(TurnEventStore(tmp_path / "t.db"))
    sink = UIEventSink(recorder)
    recorder.active.set("t1")
    for use_id, attributes in (
        ("u1", {"result_status": "error", "result_code": "redirect"}),
        ("u2", {"result_status": "success"}),
    ):
        sink.emit(
            ObservabilityEvent(
                event_type="tool.completed",
                tool_name="web_fetch",
                tool_use_id=use_id,
                status="error",
                attributes=attributes,
            )
        )
    first, second = (e["data"] for e in recorder.store.events("t1"))
    assert first["result_code"] == "redirect"
    assert "result_code" not in second


def test_hitl_broker_approval_events():
    seen = []
    broker = HITLApprovalBroker(on_event=lambda t, d: seen.append((t, d)))
    req = SimpleNamespace(
        tool_name="memory_write",
        tool_input={"k": 1},
        break_level=SimpleNamespace(value="critical"),
        reason="r",
        tool_use_id="u1",
    )
    result = {}
    thread = threading.Thread(target=lambda: result.setdefault("d", broker.request(req)))
    thread.start()
    for _ in range(100):
        if broker.pending():
            break
        time.sleep(0.01)
    request_id = broker.pending()[0]["id"]
    assert broker.decide(request_id, HITLDecision(HITLAction("reject")))
    thread.join(2)
    assert [t for t, _ in seen] == ["approval.requested", "approval.resolved"]
    assert seen[0][1]["request_id"] == request_id and seen[0][1]["break_level"] == "critical"
    assert seen[1][1]["decision"] == "deny"


def _parse(body):
    out = []
    for block in body.split("\n\n"):
        name = data = None
        for line in block.splitlines():
            if line.startswith("event: "):
                name = line[7:]
            elif line.startswith("data: "):
                data = json.loads(line[6:])
        if name:
            out.append((name, data))
    return out


def _client(recorder=None, runner=None, summary=None):
    app = Flask(__name__)
    app.register_blueprint(
        create_chat_blueprint(
            SimpleNamespace(),
            runner=runner or (lambda message: iter(["he", "llo"])),
            turn_summary_provider=lambda: summary,
            turn_recorder=recorder,
        )
    )
    return app.test_client()


def test_chat_emits_ui_events(tmp_path):
    recorder = TurnEventRecorder(TurnEventStore(tmp_path / "t.db"))
    summary = {"category": "praise", "deltas": {"trust": 0.1}, "breaker": "normal"}
    client = _client(recorder, summary=summary)
    events = _parse(client.post("/api/chat", json={"message": "secret prompt"}).get_data(True))
    assert [d["text"] for n, d in events if n == "delta"] == ["he", "llo"]
    turn = next(d for n, d in events if n == "turn")
    ui = [d for n, d in events if n == "ui_event"]
    assert [e["type"] for e in ui] == ["turn.started", "turn.summary", "turn.completed"]
    assert turn["turn_id"] == ui[0]["turn_id"] and "status" in turn and "turn_summary" in turn
    assert "secret prompt" not in json.dumps(ui)
    assert recorder.active.current() is None
    summary["breaker"] = "critical"
    client.post("/api/chat", json={"message": "x"}).get_data(True)
    turns = client.get("/api/turns?limit=5").get_json()["turns"]
    assert len(turns) == 2 and turns[0]["completed"] is True
    replay = client.get(f"/api/turns/{turns[0]['turn_id']}/events?after_seq=1").get_json()
    types = [e["type"] for e in replay["events"]]
    assert types == ["turn.summary", "breaker.transition", "turn.completed"]
    assert client.get("/api/turns?limit=x").status_code == 400


def test_chat_failure_still_completes(tmp_path):
    recorder = TurnEventRecorder(TurnEventStore(tmp_path / "t.db"))

    def boom(message):
        raise RuntimeError("x")

    client = _client(recorder, runner=boom)
    events = _parse(client.post("/api/chat", json={"message": "hi"}).get_data(True))
    ui = [d for n, d in events if n == "ui_event"]
    assert ui[-1]["type"] == "turn.completed" and ui[-1]["data"]["error"] is True
    assert any(n == "error" for n, _ in events)


def test_chat_without_recorder_is_unchanged():
    client = _client()
    events = _parse(client.post("/api/chat", json={"message": "hi"}).get_data(True))
    assert all(n != "ui_event" for n, _ in events)
    turn = next(d for n, d in events if n == "turn")
    assert "turn_id" not in turn
    assert client.get("/api/turns").status_code == 404
    assert client.get("/api/turns/abc/events").status_code == 404


def test_turn_summary_carries_before_after_snapshots(tmp_path):
    recorder = TurnEventRecorder(TurnEventStore(tmp_path / "t.db"))
    state = {"trust": 0.5, "stability": 0.7}

    def runner(message):
        state["stability"] = 0.65  # the turn changes the state
        return iter(["ok"])

    app = Flask(__name__)
    app.register_blueprint(
        create_chat_blueprint(
            SimpleNamespace(),
            runner=runner,
            status_provider=lambda: {"neural_state": dict(state)},
            turn_summary_provider=lambda: {"category": "threat", "deltas": {"stability": -0.05}},
            turn_recorder=recorder,
        )
    )
    events = _parse(app.test_client().post("/api/chat", json={"message": "x"}).get_data(True))
    summary = next(d for n, d in events if n == "ui_event" and d["type"] == "turn.summary")["data"]
    assert summary["before"]["stability"] == 0.7
    assert summary["after"]["stability"] == 0.65
    assert summary["before"]["trust"] == summary["after"]["trust"] == 0.5


def test_history_and_turn_list_expose_ribbon_fields(tmp_path):
    """#187: reload restores the ribbon from /api/chat/history + /api/turns."""
    recorder = TurnEventRecorder(TurnEventStore(tmp_path / "t.db"))
    summary = {
        "category": "threat",
        "intensity": 1.0,
        "confidence": 0.8,
        "classifier": "rule",
        "deltas": {"stability": -0.02},
        "breaker": "normal",
        "emotion_transcription": "rule: threat",
    }
    client = _client(recorder, summary=summary)
    events = _parse(client.post("/api/chat", json={"message": "x"}).get_data(True))
    turn_id = next(d for n, d in events if n == "turn")["turn_id"]
    assert client.get("/api/chat/history").get_json()["turns"][-1]["turn_id"] == turn_id
    item = client.get("/api/turns?limit=1").get_json()["turns"][0]
    assert item["turn_id"] == turn_id
    assert item["summary"]["confidence"] == 0.8
    assert item["summary"]["intensity"] == 1.0
    assert item["summary"]["deltas"] == {"stability": -0.02}
    assert set(item["summary"]) >= {"before", "after", "classifier", "breaker"}
    assert item["ended_at"] >= item["started_at"]
    assert item["usage"] == {"input_tokens": None, "output_tokens": None}


def test_history_without_recorder_has_no_turn_id():
    client = _client()
    client.post("/api/chat", json={"message": "hi"}).get_data(True)
    assert "turn_id" not in client.get("/api/chat/history").get_json()["turns"][-1]
