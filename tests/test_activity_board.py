"""#446: the Stage board's activity mode — ``GET /api/activity`` and ``observability/activity``.

Fake event sequences (no model): a YOLO window with no turn, a long model call, an
approval waiting, a running tool, a paused window, a CRITICAL breaker. The route is read
with a scripted runner that writes tool events through the real recorder mid-turn, so the
"current tool" is read the way the board reads it. Nothing secret, no argument, output,
preview, command or reason text is ever in an answer.
"""

from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace

import pytest
from flask import Flask

from stiracore.observability import activity, chat_api
from stiracore.observability.chat_api import create_chat_blueprint
from stiracore.observability.turn_events import TurnEventRecorder, TurnEventStore

WAIT = 10.0
SECRET = "AKIAABCDEFGHIJKLMNOP"
COMMAND = "rm -rf /tmp/secret-dir"


def _until(predicate, timeout=WAIT, step=0.02):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(step)
    raise AssertionError("condition not reached in time")


def _event(kind, data, ts=1000.0, turn="t1", seq=1):
    return {"type": kind, "data": data, "ts": ts, "turn_id": turn, "seq": seq}


def _tool(kind, use_id, name, status, ts, **extra):
    data = {
        "tool_use_id": use_id,
        "name": name,
        "status": status,
        "input_preview": f'{{"command": "{COMMAND}", "key": "{SECRET}"}}',
        "output_preview": f"file contents {SECRET}",
        **extra,
    }
    return _event(kind, data, ts)


# --- rows ------------------------------------------------------------------------------------


def test_rows_hold_codes_counts_and_tool_names_only():
    rows = [
        _event("turn.started", {"prompt_chars": 40, "auto_continue": "ab12"}, 1.0),
        _tool("tool_call.created", "u1", "file_write", "running", 2.0),
        _tool("tool_call.updated", "u1", "file_write", "done", 3.0, duration_ms=420),
        _tool("tool_call.created", "u2", "shell", "denied", 4.0),
        _tool("tool_call.updated", "u2", "shell", "denied", 4.0),
        _tool("tool_call.updated", "u3", "weird name;rm", "error", 5.0),
        _event("approval.requested", {"request_id": "r", "tool_name": "shell", "reason": SECRET}),
        _event("approval.resolved", {"request_id": "r", "decision": "deny", "reason": SECRET}),
        _event("breaker.transition", {"from": "NORMAL", "to": "WARNING", "reasons": [SECRET]}),
        _event("model.started", {"call_id": "c1"}),
        _event("model.progress", {"call_id": "c1", "elapsed_ms": 10000}),
        _event("model.completed", {"call_id": "c1", "duration_ms": 61000, "status": "success"}),
        _event("turn.summary", {"emotion_transcription": SECRET}),
        _event("progress.report", {"report_id": "x", "trigger": "time", "end": "soft"}),
        _event("progress.reply", {"report_id": "x", "answer": "redirect"}),
        _event("turn.completed", {"stop": "checkpoint", "error": False, "usage": {}}),
    ]
    items = [item for i, ev in enumerate(rows) if (item := activity.activity_item(i + 1, ev))]
    text = json.dumps(items)
    assert SECRET not in text and COMMAND not in text and "contents" not in text
    assert [(i["kind"], i["what"]) for i in items] == [
        ("turn", "started"),
        ("tool", "done"),
        ("tool", "denied"),
        ("tool", "error"),
        ("approval", "requested"),
        ("approval", "deny"),
        ("breaker", "WARNING"),
        ("model", "success"),
        ("progress", "report"),
        ("progress", "redirect"),
        ("turn", "completed"),
    ]
    assert items[0]["auto"] is True
    assert items[1] == {
        "id": 3,
        "ts": 3.0,
        "turn": "t1",
        "kind": "tool",
        "what": "done",
        "name": "file_write",
        "ok": True,
        "duration_ms": 420,
    }
    assert items[3]["name"] == "other" and items[3]["ok"] is False  # not a plain tool name
    assert items[7]["duration_ms"] == 61000
    assert items[-1]["stop"] == "checkpoint" and items[-1]["ok"] is True
    for item in items:
        assert set(item) <= {
            "id", "ts", "turn", "kind", "what", "ok", "name", "duration_ms", "stop",
            "from", "trigger", "auto", "count",
        }  # fmt: skip


# --- the state now ---------------------------------------------------------------------------


def test_state_idle_with_a_yolo_window_and_no_turn():
    window = activity.window_view(
        {
            "window": {
                "state": "active",
                "tier": "internal",
                "remaining_sec": 21000,
                "used": 3,
                "limit": 50,
                "external_used": 0,
                "external_limit": None,
                "t0": 1000.0,
                "ends_at": 22600.0,
                "id": "abcd1234",
            }
        }
    )
    assert window == {
        "state": "active",
        "tier": "internal",
        "remaining_sec": 21000,
        "used": 3,
        "limit": 50,
        "external_used": 0,
        "t0": 1000.0,
        "ends_at": 22600.0,
    }
    state = activity.live_state(None, window=window, breaker="NORMAL")
    assert state["code"] == "idle" and state["running"] is False


def test_state_long_model_call_counts_from_its_start_and_ignores_progress_ticks():
    events = [
        _event("turn.started", {}, 100.0),
        _event("model.started", {"call_id": "c1", "started_at": 101.0}, 101.2),
        _event("model.progress", {"call_id": "c1", "elapsed_ms": 10000}, 111.0),
        _event("model.progress", {"call_id": "c1", "elapsed_ms": 200000}, 301.0),
    ]
    state = activity.live_state(events)
    assert (state["code"], state["since"], state["last_event"]) == ("generating", 101.0, 101.2)


def test_state_running_tool_then_between_steps():
    events = [
        _event("turn.started", {}, 100.0),
        _tool("tool_call.created", "u1", "web_fetch", "running", 102.0),
    ]
    state = activity.live_state(events)
    assert (state["code"], state["tool"], state["since"]) == ("tool", "web_fetch", 102.0)
    assert SECRET not in json.dumps(state)
    events.append(_tool("tool_call.updated", "u1", "web_fetch", "done", 104.0))
    assert activity.live_state(events)["code"] == "working"


def test_state_approval_wins_and_names_the_tool_only():
    pending = [{"tool_name": "shell", "created_at": 150.0, "normalized_input": COMMAND}]
    state = activity.live_state([_event("turn.started", {}, 100.0)], pending=pending)
    assert (state["code"], state["tool"], state["waiting"], state["since"]) == (
        "approval",
        "shell",
        1,
        150.0,
    )
    assert COMMAND not in json.dumps(state)


def test_state_paused_window_and_critical_breaker():
    paused = activity.window_view(
        {
            "window": {
                "state": "paused",
                "pause_reason": "breaker",
                "pause_breaker_reasons": ["vigilance_high", "not a code!"],
            }
        }
    )
    assert paused["pause_breaker_reasons"] == ["vigilance_high"]
    state = activity.live_state(None, window=paused)
    assert (state["code"], state["reason"]) == ("paused", "breaker")
    state = activity.live_state(None, window=paused, breaker="CRITICAL")
    assert (state["code"], state["reason"]) == ("stopped", "breaker_critical")
    assert activity.live_state(None, latched=True)["code"] == "stopped"


def test_state_a_pending_progress_report_waits_for_the_answer():
    report = {"report_id": "x", "trigger": "count", "count": "tools", "ts": 900.0}
    state = activity.live_state(None, report=report, breaker="NORMAL")
    assert (state["code"], state["trigger"], state["count"], state["since"]) == (
        "report",
        "count",
        "tools",
        900.0,
    )
    # a running turn (a continuation) wins over the report
    assert activity.live_state([_event("turn.started", {}, 1.0)], report=report)["code"] == (
        "working"
    )


def test_totals_and_cost_only_with_rates():
    done = [
        {"usage": {"input_tokens": 100, "output_tokens": 10, "cache_read_tokens": 50}},
        {"usage": {"input_tokens": 200, "output_tokens": 20, "context_tokens": 4321}},
        {"usage": {"input_tokens": None}},
    ]
    totals = activity.totals_from(done)
    assert totals == {
        "turns": 3,
        "input": 300,
        "output": 30,
        "cache_read": 50,
        "cache_write": 0,
        "context_tokens": 4321,
        "cost": None,
    }
    assert activity.totals_from(done, (3.0, 15.0))["cost"] == pytest.approx(0.00135)


# --- the route -------------------------------------------------------------------------------


class ToolTurn:
    """A runner that writes a tool start, waits, then writes its end (real recorder)."""

    def __init__(self, recorder):
        self.recorder = recorder
        self.reached = threading.Event()
        self.release = threading.Event()

    def __call__(self, _message):
        yield "working "
        self.recorder.emit(
            "tool_call.created",
            {
                "tool_use_id": "u1",
                "name": "file_write",
                "status": "running",
                "input_preview": f"{SECRET} {COMMAND}",
            },
        )
        self.reached.set()
        assert self.release.wait(WAIT)
        self.recorder.emit(
            "tool_call.updated",
            {"tool_use_id": "u1", "name": "file_write", "status": "done", "duration_ms": 5},
        )
        yield "done"


@pytest.fixture
def recorder(tmp_path):
    return TurnEventRecorder(TurnEventStore(tmp_path / "turns.db"))


def _app(runner, recorder):
    app = Flask(__name__)
    app.register_blueprint(
        create_chat_blueprint(SimpleNamespace(), runner=runner, turn_recorder=recorder)
    )
    return app


def _start(client):
    response = client.post("/api/chat", json={"message": "x"}, buffered=False)
    iterator = iter(response.response)
    next(iterator)
    return response


def test_route_follows_a_turn_and_restores_the_list_after_a_reload(recorder, monkeypatch):
    monkeypatch.setattr(chat_api.yolo_runtime, "status", lambda: {"window": None})
    runner = ToolTurn(recorder)
    client = _app(runner, recorder).test_client()
    first = client.get("/api/activity").get_json()
    assert first["reset"] is True and first["items"] == [] and first["state"]["code"] == "idle"
    assert first["yolo"] is None and first["stall_sec"] == activity.STALL_SEC
    response = _start(client)
    assert runner.reached.wait(WAIT)
    during = _until(
        lambda: (
            (body := client.get(f"/api/activity?after_id={first['cursor']}").get_json())
            and body["state"]["code"] == "tool"
            and body
        )
    )
    assert during["state"]["tool"] == "file_write" and during["state"]["turn"]
    assert during["state"]["last_event"] is not None
    assert [(i["kind"], i["what"]) for i in during["items"]] == [("turn", "started")]
    runner.release.set()
    response.close()
    _until(lambda: recorder.store.recent_turns(1)[0]["completed"])
    after = client.get(f"/api/activity?after_id={during['cursor']}").get_json()
    assert after["state"]["code"] == "idle" and after["reset"] is False
    assert [(i["kind"], i["what"]) for i in after["items"]] == [
        ("tool", "done"),
        ("turn", "completed"),
    ]
    # nothing new: an empty diff, the same cursor
    again = client.get(f"/api/activity?after_id={after['cursor']}").get_json()
    assert again["items"] == [] and again["cursor"] == after["cursor"]
    # a reload (no cursor): the list comes back from turns.db
    reload = client.get("/api/activity").get_json()
    assert [(i["kind"], i["what"]) for i in reload["items"]] == [
        ("turn", "started"),
        ("tool", "done"),
        ("turn", "completed"),
    ]
    assert reload["totals"]["turns"] == 1 and reload["cursor"] == after["cursor"]
    text = json.dumps([first, during, after, reload])
    assert SECRET not in text and COMMAND not in text


def test_route_reads_the_yolo_window_and_refuses_a_bad_cursor(recorder, monkeypatch):
    monkeypatch.setattr(
        chat_api.yolo_runtime,
        "status",
        lambda: {
            "latched": False,
            "window": {"state": "active", "tier": "external", "remaining_sec": 3000, "used": 2,
                       "limit": 40, "external_used": 1, "external_limit": 5},
        },
    )  # fmt: skip
    client = _app(lambda _m: iter(["x"]), recorder).test_client()
    body = client.get("/api/activity").get_json()
    assert body["yolo"]["remaining_sec"] == 3000 and body["yolo"]["external_limit"] == 5
    assert body["state"]["code"] == "idle"
    for bad in ("-1", "abc", "1" * 20, "1.5"):
        assert client.get(f"/api/activity?after_id={bad}").status_code == 400
    # a cursor from another store starts over (the last rows again)
    assert client.get("/api/activity?after_id=999").get_json()["reset"] is True


def test_route_without_turn_events_is_404():
    app = Flask(__name__)
    app.register_blueprint(create_chat_blueprint(SimpleNamespace(), runner=lambda m: iter([])))
    assert app.test_client().get("/api/activity").status_code == 404


def test_first_read_skips_model_progress_rows(recorder, monkeypatch):
    monkeypatch.setattr(chat_api.yolo_runtime, "status", dict)
    store = recorder.store
    store.append("t1", "turn.started", {})
    for n in range(200):
        store.append("t1", "model.progress", {"call_id": "c", "elapsed_ms": n * 10000})
    store.append("t1", "turn.completed", {"usage": {}, "stop": "completed", "error": False})
    client = _app(lambda _m: iter(["x"]), recorder).test_client()
    body = client.get("/api/activity").get_json()
    assert [(i["kind"], i["what"]) for i in body["items"]] == [
        ("turn", "started"),
        ("turn", "completed"),
    ]
    assert body["cursor"] == store.counts()["latest_id"]
