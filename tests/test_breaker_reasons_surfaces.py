"""#455: the breaker's reasons on the turn events, the observability page and the status."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from flask import Flask

from stiracore.observability.chat_api import create_chat_blueprint
from stiracore.observability.turn_events import TurnEventRecorder, TurnEventStore
from stiracore.observability.turns_view import page_event
from tests.test_yolo_host_ops import Clocks
from tests.test_yolo_requests import Recorder


@pytest.fixture
def clock():
    return Clocks()


@pytest.fixture
def audit():
    return Recorder()


def _client(recorder, summary):
    app = Flask(__name__)
    app.register_blueprint(
        create_chat_blueprint(
            SimpleNamespace(),
            runner=lambda message: iter(["ok"]),
            turn_summary_provider=lambda: summary,
            turn_recorder=recorder,
        )
    )
    return app.test_client()


def _last_events(client):
    turns = client.get("/api/turns?limit=1").get_json()["turns"]
    return client.get(f"/api/turns/{turns[0]['turn_id']}/events").get_json()["events"]


def test_transition_event_keeps_from_to_and_adds_reasons(tmp_path):
    recorder = TurnEventRecorder(TurnEventStore(tmp_path / "t.db"))
    summary = {"category": "loss", "deltas": {"stability": -0.03}, "breaker": "normal"}
    client = _client(recorder, summary)
    client.post("/api/chat", json={"message": "private words"}).get_data(True)
    summary.update(
        breaker="warning",
        breaker_reasons=["stability_below_warning", "not a code"],
        breaker_inputs={"stability": 0.4912345, "stability_warning": 0.5, "text": "x"},
        breaker_transition={
            "from": "normal",
            "to": "warning",
            "reasons": ["stability_below_warning"],
            "inputs": {"stability": 0.4912, "stability_warning": 0.5},
        },
    )
    client.post("/api/chat", json={"message": "more private words"}).get_data(True)
    events = _last_events(client)
    transition = next(e for e in events if e["type"] == "breaker.transition")
    assert transition["data"] == {
        "from": "normal",
        "to": "warning",
        "reasons": ["stability_below_warning"],
        "inputs": {"stability": 0.4912, "stability_warning": 0.5},
    }
    summary_event = next(e for e in events if e["type"] == "turn.summary")
    assert summary_event["data"]["breaker_reasons"] == ["stability_below_warning"]
    assert summary_event["data"]["breaker_inputs"] == {
        "stability": 0.4912,
        "stability_warning": 0.5,
    }
    assert "private" not in json.dumps(transition) and "not a code" not in json.dumps(events)


def test_transition_from_a_pre_turn_change_uses_the_engine_record(tmp_path):
    recorder = TurnEventRecorder(TurnEventStore(tmp_path / "t.db"))
    summary = {"breaker": "warning"}
    client = _client(recorder, summary)
    client.post("/api/chat", json={"message": "a"}).get_data(True)
    # Recovered during pre_turn: the turn's own evaluation says "within thresholds", the
    # engine's record of the change names the latch release.
    summary.update(
        breaker="normal",
        breaker_reasons=["within_thresholds"],
        breaker_inputs={"stability": 0.62},
        breaker_transition={
            "from": "warning",
            "to": "normal",
            "reasons": ["stability_above_warning_exit"],
            "inputs": {"stability": 0.61, "stability_warning_exit": 0.6},
        },
    )
    client.post("/api/chat", json={"message": "b"}).get_data(True)
    transition = next(e for e in _last_events(client) if e["type"] == "breaker.transition")
    assert transition["data"]["reasons"] == ["stability_above_warning_exit"]
    assert transition["data"]["inputs"] == {"stability": 0.61, "stability_warning_exit": 0.6}


def test_transition_without_reasons_is_the_old_shape(tmp_path):
    recorder = TurnEventRecorder(TurnEventStore(tmp_path / "t.db"))
    summary = {"breaker": "normal"}
    client = _client(recorder, summary)
    client.post("/api/chat", json={"message": "a"}).get_data(True)
    summary["breaker"] = "critical"
    client.post("/api/chat", json={"message": "b"}).get_data(True)
    transition = next(e for e in _last_events(client) if e["type"] == "breaker.transition")
    assert transition["data"] == {"from": "normal", "to": "critical"}


def test_observability_page_event_carries_allow_listed_reasons():
    event = {
        "event_id": "e" * 32,
        "turn_id": "t" * 32,
        "seq": 3,
        "ts": 1.0,
        "type": "breaker.transition",
        "data": {
            "from": "normal",
            "to": "WARNING",
            "reasons": ["stability_below_warning", "<script>"],
            "inputs": {"stability": 0.31, "stability_warning": 0.35, "memo": "secret"},
        },
    }
    out = page_event(7, event)
    assert out["message"] == "normal → WARNING"  # unchanged
    assert out["reasons"] == ["stability_below_warning"]
    assert out["inputs"] == {"stability": 0.31, "stability_warning": 0.35}
    assert out["to_level"] == "warning"
    old = page_event(8, {**event, "data": {"from": "normal", "to": "warning"}})
    assert "reasons" not in old and "inputs" not in old


# --- YOLO: the pause carries the breaker's codes (additive to pause_reason) -----------------


def test_yolo_pause_carries_the_breaker_reason_codes(audit, clock, tmp_path):
    from stiracore.yolo import origin
    from stiracore.yolo.window import WindowMarker
    from tests.test_yolo_window import _open, _service

    level = {"now": "normal"}
    detail = {
        "reasons": ["stability_below_warning", "free text"],
        "inputs": {"stability": 0.31, "stability_warning": 0.35, "note": "x"},
    }
    yolo = _service(
        audit,
        clock,
        WindowMarker(tmp_path),
        breaker=lambda: level["now"],
        breaker_reasons=lambda: detail,
    )
    with origin.human_turn("nxp-agent"):
        _open(yolo)
        assert yolo.status()["window"]["pause_breaker_reasons"] == []
        level["now"] = "warning"
        yolo.tick()
    window = yolo.status()["window"]
    assert window["pause_reason"] == "breaker_warning"  # unchanged code
    assert window["pause_breaker_reasons"] == ["stability_below_warning"]
    assert window["pause_breaker_inputs"] == {"stability": 0.31, "stability_warning": 0.35}


def test_yolo_pause_survives_a_failing_reason_reader(audit, clock, tmp_path):
    from stiracore.yolo import origin
    from stiracore.yolo.window import WindowMarker
    from tests.test_yolo_window import _open, _service

    level = {"now": "normal"}

    def boom():
        raise RuntimeError("no")

    yolo = _service(
        audit, clock, WindowMarker(tmp_path), breaker=lambda: level["now"], breaker_reasons=boom
    )
    with origin.human_turn("nxp-agent"):
        _open(yolo)
        level["now"] = "critical"
        yolo.tick()
    window = yolo.status()["window"]
    assert window["pause_reason"] == "breaker_critical" and window["state"] == "paused"
    assert window["pause_breaker_reasons"] == []
