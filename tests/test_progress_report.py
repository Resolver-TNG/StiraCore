"""ADR 0014 phase 1 (#451): progress reports in a YOLO window, on the real chat server app.

Offline: a scripted model (``tests/provider_fakes.FakeModel``) and a fake clock for the
progress tracker (its timer thread off; the tests call ``tick()``). The YOLO window is
opened through the browser confirmation like a person would. Each acceptance line of the
ADR / the operator decision of 2026-10-19 is one test here.
"""

from __future__ import annotations

import functools
import itertools
import json
import threading
from unittest.mock import Mock

import pytest

from stiracore.progress import NUDGE_TEXT, ProgressTracker
from stiracore.progress.tracker import HARD_END_TEXT
from stiracore.yolo import own_dashboard, runtime
from stiracore.yolo.allowance import YoloSettings
from tests.provider_fakes import FakeModel, text_turn, tool_turn
from tests.test_yolo_browser_confirm import BROWSER, _confirm_request

MIN = 60.0


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class ClockModel(FakeModel):
    """FakeModel whose script entries may hold ``{"advance": sec}`` (the clock moves when
    that model call starts) and ``{"wait": event}`` (the call blocks until it is set)."""

    def __init__(self, clock, script=None):
        super().__init__(script or [])
        self.clock = clock

    async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
        if self.script:
            turn = self.script[0]
            for block in [b for b in turn if "advance" in b or "wait" in b]:
                turn.remove(block)
                if "advance" in block:
                    self.clock.now += block["advance"]
                else:
                    assert block["wait"].wait(10)
        async for event in super().stream(messages, tool_specs, system_prompt, **kwargs):
            yield event


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def make_app(tmp_path, monkeypatch, clock):
    made = []
    monkeypatch.setattr(own_dashboard, "armed", lambda: object())

    def make(model, *, env=None, reuse=None):
        from dashboard import stira_server
        from stiracore.agent import nxp_agent, stimulus_classifier
        from stiracore.agent.stimulus_classifier import StimulusResult
        from stiracore.observability.config_api import ConfigStore

        for name in (
            "STIRA_PROGRESS_REPORTS",
            "STIRA_PROGRESS_INTERVAL_MIN",
            "STIRA_PROGRESS_CONTINUE_AFTER_MIN",
            "STIRA_PROGRESS_MAX_AUTO_CONTINUES",
            "STIRA_PROGRESS_COUNT_TOOLS",
            "STIRA_PROGRESS_COUNT_TOKENS",
            "STIRA_PROGRESS_DECLARED",
        ):
            monkeypatch.delenv(name, raising=False)
        for name, value in (env or {}).items():
            monkeypatch.setenv(name, value)
        base = tmp_path / str(len(made) if reuse is None else reuse)
        monkeypatch.setattr(stira_server, "DATA_DIR", base / "data")
        monkeypatch.setattr(nxp_agent, "BedrockModel", Mock(return_value=model))
        monkeypatch.setattr(
            stimulus_classifier,
            "get_classifier",
            lambda: Mock(
                model_id=None,
                classify=Mock(return_value=StimulusResult(category="joy", intensity=0.5)),
            ),
        )
        monkeypatch.setattr(
            stira_server,
            "ProgressTracker",
            functools.partial(ProgressTracker, clock=clock, timer=False),
        )
        runtime._reset_for_tests(YoloSettings("internal", host_confirm="browser"))
        store = ConfigStore(base / "gw.json", base / "agent.json", base / "tools.json")
        real = stira_server.NXPAgent
        agents = []

        def capture(*args, **kwargs):
            agents.append(real(*args, **kwargs))
            return agents[-1]

        monkeypatch.setattr(stira_server, "NXPAgent", capture)
        share = {} if reuse is None else {"_tests_share_data_dir": True}  # a "restart"
        app = stira_server.create_app(config_store=store, **share)
        monkeypatch.setattr(stira_server, "NXPAgent", real)
        app.extensions["test_agent"] = agents[0]
        made.append(app)
        return app

    try:
        yield make
    finally:
        for app in made:
            app.extensions["lifecycle"].shutdown()
        runtime._reset_for_tests()
        runtime.TRACKER._calls.clear()


def _headers(app, key=None):
    headers = {**BROWSER, "X-CSRF-Token": app.config["STIRA_CSRF_TOKEN"]}
    if key:
        headers["Idempotency-Key"] = key
    return headers


def _open_window(client, **extra):
    response = client.post("/api/yolo/request", json={"tier": "internal", **extra})
    assert response.status_code == 202, response.get_json()
    confirmed = _confirm_request(client, response.get_json())
    assert confirmed.status_code == 200, confirmed.get_json()
    window = client.get("/api/yolo/status").get_json()["window"]
    assert window["state"] == "active"
    return window


def _chat(app, client, key, message="please work", **extra):
    response = client.post(
        "/api/chat", json={"message": message, **extra}, headers=_headers(app, key)
    )
    body = response.get_data(as_text=True)  # the turn runs while the stream is read
    assert response.status_code == 200, body
    return body


def _turn_frame(body):
    for block in body.split("\n\n"):
        if block.startswith("event: turn\n"):
            return json.loads(block.split("data: ", 1)[1])
    raise AssertionError(body)


def _events(client, turn_id):
    return client.get(f"/api/turns/{turn_id}/events").get_json()["events"]


def _progress_events(client, turn_id):
    return [e for e in _events(client, turn_id) if e["type"].startswith("progress.")]


def _workspace(app):
    return app.extensions["test_agent"].write_policy.workspace


def _write(app, name, content="note\n", use="w"):
    return tool_turn("file_write", {"path": str(_workspace(app) / name), "content": content}, use)


def _report_turn(app, client, model, *, note="Done: a.md. Next: b.md. Question: keep going?"):
    """One turn that meets the checkpoint: a write, 31 minutes, a second write (cancelled),
    the agent's note. -> (turn frame, the report)."""
    model.script = [
        _write(app, "a.md", "SECRET-CONTENT-123\n", "w1"),
        [{"advance": 31 * MIN}, *_write(app, "b.md", "never\n", "w2")],
        text_turn(note),
    ]
    frame = _turn_frame(_chat(app, client, f"k{model.clock.now}"))
    assert frame["stop"] == "checkpoint", frame
    current = client.get("/api/progress/current").get_json()
    return frame, current["report"]


def _on(**extra):
    return {"STIRA_PROGRESS_REPORTS": "1", **extra}


# --- the checkpoint -----------------------------------------------------------------------


def test_thirty_minutes_give_exactly_one_report_and_no_tool_runs_after_it(make_app, clock):
    model = ClockModel(clock)
    app = make_app(model, env=_on())
    client = app.test_client()
    _open_window(client)
    frame, report = _report_turn(app, client, model)
    workspace = _workspace(app)
    assert (workspace / "a.md").read_text() == "SECRET-CONTENT-123\n"
    assert not (workspace / "b.md").exists()  # the call at the checkpoint never ran
    events = _progress_events(client, frame["turn_id"])
    assert [e["type"] for e in events] == ["progress.report"]
    data = events[0]["data"]
    assert (data["trigger"], data["end"]) == ("time", "soft")
    assert data["tools"] == {"file_write": [1, 0]}  # the cancelled call is not counted
    assert data["elapsed_sec"] >= 31 * 60
    # The cancelled call was cancelled before the YOLO approver: no cap slot, no card.
    window = client.get("/api/yolo/status").get_json()["window"]
    assert (window["used"], window["reserved"]) == (1, 0)
    assert app.extensions["hitl_broker"].pending() == []
    # The model got the fixed nudge as the tool result, then wrote the note and stopped.
    assert len(model.requests) == 3
    last = model.requests[2]["messages"][-1]
    assert NUDGE_TEXT in json.dumps(last, ensure_ascii=False)
    assert report["pending"] is True and report["note"].startswith("Done: a.md")
    completed = [e for e in _events(client, frame["turn_id"]) if e["type"] == "turn.completed"]
    assert completed[0]["data"]["stop"] == "checkpoint" and completed[0]["data"]["error"] is False


def test_before_the_interval_nothing_happens(make_app, clock):
    model = ClockModel(clock)
    app = make_app(model, env=_on())
    client = app.test_client()
    _open_window(client)
    model.script = [
        _write(app, "a.md", use="w1"),
        [{"advance": 29 * MIN}, *_write(app, "b.md", use="w2")],
        text_turn("all done"),
    ]
    frame = _turn_frame(_chat(app, client, "k1"))
    assert "stop" not in frame and (_workspace(app) / "b.md").exists()
    assert _progress_events(client, frame["turn_id"]) == []


def test_a_tool_call_after_the_nudge_is_a_hard_end_with_alternating_history(make_app, clock):
    model = ClockModel(clock)
    app = make_app(model, env=_on())
    client = app.test_client()
    _open_window(client)
    model.script = [
        _write(app, "a.md", use="w1"),
        [{"advance": 31 * MIN}, *_write(app, "b.md", use="w2")],
        _write(app, "c.md", use="w3"),  # the model ignores the nudge
    ]
    frame = _turn_frame(_chat(app, client, "k1"))
    assert frame["stop"] == "checkpoint"
    assert not (_workspace(app) / "b.md").exists() and not (_workspace(app) / "c.md").exists()
    report = client.get("/api/progress/current").get_json()["report"]
    assert report["end"] == "hard" and report["note"] is None
    assert len(model.requests) == 3  # ended by the harness: no fourth model call
    # The next person's message keeps user / assistant alternating (Bedrock refuses two
    # user messages in a row): the hard end closed the history with one assistant line.
    model.script = [text_turn("ok")]
    _chat(app, client, "k2", "続けてください。")
    roles = [m["role"] for m in model.requests[-1]["messages"]]
    assert all(a != b for a, b in itertools.pairwise(roles)), roles
    assert HARD_END_TEXT in json.dumps(model.requests[-1]["messages"][-2])


# --- answers ---------------------------------------------------------------------------------


def test_continue_starts_a_new_turn_and_records_the_answer(make_app, clock):
    model = ClockModel(clock)
    app = make_app(model, env=_on())
    client = app.test_client()
    _open_window(client)
    frame, report = _report_turn(app, client, model)
    model.script = [text_turn("continuing")]
    body = _chat(
        app,
        client,
        "k2",
        "続けてください。",
        progress_reply={"report_id": report["report_id"], "answer": "continue"},
    )
    nxt = _turn_frame(body)
    assert "stop" not in nxt
    [_report, reply] = _progress_events(client, frame["turn_id"])
    assert reply["data"] == {
        "report_id": report["report_id"],
        "answer": "continue",
        "next_turn_id": nxt["turn_id"],
    }
    current = client.get("/api/progress/current").get_json()["report"]
    assert current["pending"] is False and current["answer"]["answer"] == "continue"


def test_redirect_line_is_the_next_model_calls_instruction(make_app, clock):
    model = ClockModel(clock)
    app = make_app(model, env=_on())
    client = app.test_client()
    _open_window(client)
    _frame, report = _report_turn(app, client, model)
    model.script = [text_turn("ok, b first")]
    line = "方向修正: b.md を先に仕上げてください"
    _chat(
        app,
        client,
        "k2",
        line,
        progress_reply={"report_id": report["report_id"], "answer": "redirect"},
    )
    last = model.requests[-1]["messages"][-1]
    assert last["role"] == "user" and last["content"][0]["text"] == line


@pytest.mark.parametrize("prefix", ["方向修正: ", "Change of direction: ", ""])
def test_a_redirect_line_over_300_characters_is_refused_by_the_server(make_app, clock, prefix):
    """PR #489 review (minor): the 300-character limit holds on the server too (the page's
    ``maxLength`` alone is not enough); exactly 300 after the prefix still goes."""
    model = ClockModel(clock)
    app = make_app(model, env=_on())
    client = app.test_client()
    _open_window(client)
    _frame, report = _report_turn(app, client, model)
    calls = len(model.requests)
    reply = {"report_id": report["report_id"], "answer": "redirect"}
    response = client.post(
        "/api/chat",
        json={"message": prefix + "あ" * 301, "progress_reply": reply},
        headers=_headers(app, "long"),
    )
    assert response.status_code == 400
    assert response.get_json()["code"] == "redirect_too_long"
    assert len(model.requests) == calls  # no model call
    assert client.get("/api/progress/current").get_json()["report"]["pending"] is True
    model.script = [text_turn("ok")]
    _chat(app, client, "ok-300", prefix + "あ" * 300, progress_reply=reply)
    assert len(model.requests) == calls + 1


def test_a_reply_whose_turn_could_not_start_is_not_recorded(make_app, clock, monkeypatch):
    """PR #489 review (minor): the answer is recorded only once its turn has started."""
    from stiracore.observability import chat_api

    model = ClockModel(clock)
    app = make_app(model, env=_on())
    client = app.test_client()
    _open_window(client)
    frame, report = _report_turn(app, client, model)
    real = chat_api.threading.Thread

    class NoStart(real):
        def start(self):
            if self.name.startswith("chat-turn-"):
                raise RuntimeError("cannot start a thread")
            super().start()

    monkeypatch.setattr(chat_api.threading, "Thread", NoStart)
    reply = {"report_id": report["report_id"], "answer": "continue"}
    try:
        response = client.post(
            "/api/chat",
            json={"message": "続けて", "progress_reply": reply},
            headers=_headers(app, "nostart"),
        )
        assert response.status_code == 500
    except RuntimeError:
        pass  # the test app may propagate it
    monkeypatch.setattr(chat_api.threading, "Thread", real)
    assert [e["type"] for e in _progress_events(client, frame["turn_id"])] == ["progress.report"]
    assert client.get("/api/progress/current").get_json()["report"]["pending"] is True
    assert app.extensions["turn_lock"].acquire(timeout=1)  # the lock is free again
    app.extensions["turn_lock"].release()


def test_an_ordinary_message_answers_the_report(make_app, clock):
    model = ClockModel(clock)
    app = make_app(model, env=_on())
    client = app.test_client()
    _open_window(client)
    frame, _report = _report_turn(app, client, model)
    model.script = [text_turn("ok")]
    _chat(app, client, "k2", "別の話")
    assert _progress_events(client, frame["turn_id"])[-1]["data"]["answer"] == "message"


def test_stop_ends_the_window_normally_and_cancels_the_auto_continue(make_app, clock):
    model = ClockModel(clock)
    app = make_app(model, env=_on())
    client = app.test_client()
    _open_window(client)
    frame, report = _report_turn(app, client, model)
    assert report["auto"] is not None  # scheduled
    response = client.post(f"/api/progress/{report['report_id']}/stop", headers=_headers(app))
    assert response.status_code == 200 and response.get_json()["yolo_stopped"] is True
    status = client.get("/api/yolo/status").get_json()
    assert status["window"] is None, status["window"]
    assert status["last_end"]["reason"] == "revoked_stop", status["last_end"]
    reply = _progress_events(client, frame["turn_id"])[-1]["data"]
    assert (reply["answer"], reply["yolo_stopped"]) == ("stop", True)
    clock.now += 11 * MIN
    assert app.extensions["progress"].tick() is False
    assert len(model.requests) == 3


def test_stop_holds_even_when_the_window_stop_fails(make_app, clock, monkeypatch):
    """The report's Stop never depends on the YOLO stop: the continue is cancelled first."""

    def broken():
        raise RuntimeError("desk gone")

    monkeypatch.setattr(runtime, "stop", broken)
    model = ClockModel(clock)
    app = make_app(model, env=_on())
    client = app.test_client()
    _open_window(client)
    _frame, report = _report_turn(app, client, model)
    response = client.post(f"/api/progress/{report['report_id']}/stop", headers=_headers(app))
    assert response.status_code == 200 and response.get_json()["yolo_stopped"] is False
    clock.now += 11 * MIN
    assert app.extensions["progress"].tick() is False


# --- the automatic continue (operator decision 2026-10-19) ----------------------------------


def test_without_an_answer_one_automatic_continue_after_ten_minutes(make_app, clock):
    model = ClockModel(clock)
    app = make_app(model, env=_on())
    client = app.test_client()
    _open_window(client)
    frame, report = _report_turn(app, client, model)
    tracker = app.extensions["progress"]
    model.script = [text_turn("continued on my own")]
    clock.now += 9 * MIN
    assert tracker.tick() is False
    clock.now += 1 * MIN
    assert tracker.tick() is True
    lock = app.extensions["turn_lock"]
    assert lock.acquire(timeout=10)  # the continuation turn has ended
    lock.release()
    assert tracker.tick() is False  # once per report
    auto = _progress_events(client, frame["turn_id"])[-1]
    assert (auto["type"], auto["data"]["n"]) == ("progress.auto_continue", 1)
    last = model.requests[-1]["messages"][-1]["content"][0]["text"]
    assert last.startswith("Please continue.")  # the person wrote in English
    history = client.get("/api/chat/history").get_json()["turns"]
    assert history[-1]["auto_continue"] is True
    assert history[-1]["turn_id"] == auto["data"]["next_turn_id"]
    assert report["auto"]["n"] == 1


def test_three_automatic_continues_in_a_row_then_it_waits(make_app, clock):
    model = ClockModel(clock)
    app = make_app(model, env=_on())
    client = app.test_client()
    _open_window(client)
    _report_turn(app, client, model)
    tracker = app.extensions["progress"]
    lock = app.extensions["turn_lock"]
    for n in (1, 2, 3):
        # Each continuation works on and meets the next checkpoint.
        model.script = [
            _write(app, f"x{n}.md", use=f"x{n}"),
            [{"advance": 31 * MIN}, *_write(app, f"y{n}.md", use=f"y{n}")],
            text_turn(f"report {n}"),
        ]
        clock.now += 10 * MIN
        assert tracker.tick() is True, n
        assert lock.acquire(timeout=10)
        lock.release()
        report = client.get("/api/progress/current").get_json()["report"]
        assert report["pending"] is True and report["after_auto"] == n
    assert report["auto"] is None  # the fourth report waits for a person
    assert client.get("/api/progress/current").get_json()["consecutive_auto"] == 3
    clock.now += 60 * MIN
    assert tracker.tick() is False
    # A person's answer resets the count: the next report schedules again.
    model.script = [
        _write(app, "z.md", use="z1"),
        [{"advance": 31 * MIN}, *_write(app, "z2.md", use="z2")],
        text_turn("report after the person"),
    ]
    _chat(
        app,
        client,
        "k-person",
        "続けて",
        progress_reply={"report_id": report["report_id"], "answer": "continue"},
    )
    assert client.get("/api/progress/current").get_json()["report"]["auto"]["n"] == 1


def test_no_automatic_continue_without_an_active_window(make_app, clock):
    model = ClockModel(clock)
    app = make_app(model, env=_on())
    client = app.test_client()
    _open_window(client)
    _report_turn(app, client, model)
    runtime.stop()  # the window ends (the YOLO badge's stop)
    clock.now += 11 * MIN
    assert app.extensions["progress"].tick() is False
    assert len(model.requests) == 3


@pytest.mark.parametrize("level", ["warning", "coherence", "critical", "unknown"])
def test_no_automatic_continue_while_the_breaker_is_not_normal(make_app, clock, monkeypatch, level):
    """PR #489 review (major 2), measured on the real app: the breaker leaves NORMAL after
    the report; at the due time no continuation starts, the scheduled continue is dropped
    (not kept for later) and the report waits for a person — also once NORMAL is back."""
    from dashboard import stira_server

    model = ClockModel(clock)
    app = make_app(model, env=_on())
    client = app.test_client()
    _open_window(client)
    _report_turn(app, client, model)
    tracker = app.extensions["progress"]
    calls = len(model.requests)
    monkeypatch.setattr(stira_server, "_yolo_breaker_level", lambda engine: level)
    clock.now += 11 * MIN
    assert tracker.tick() is False
    assert client.get("/api/yolo/status").get_json()["window"]["state"] == "paused"
    monkeypatch.setattr(stira_server, "_yolo_breaker_level", lambda engine: "normal")
    clock.now += 60 * MIN
    assert tracker.tick() is False
    assert len(model.requests) == calls  # no model call
    report = client.get("/api/progress/current").get_json()["report"]
    assert report["pending"] is True and report["auto"] is None


def test_no_automatic_continue_while_latched(make_app, clock, monkeypatch):
    """A latch with the window still shown ``active`` (the status as read): no continue."""
    model = ClockModel(clock)
    app = make_app(model, env=_on())
    client = app.test_client()
    _open_window(client)
    _report_turn(app, client, model)
    calls = len(model.requests)
    real = runtime.status
    monkeypatch.setattr(runtime, "status", lambda: {**real(), "latched": True})
    clock.now += 11 * MIN
    assert app.extensions["progress"].tick() is False
    assert len(model.requests) == calls


def test_new_conversation_cancels_the_automatic_continue(make_app, clock):
    model = ClockModel(clock)
    app = make_app(model, env=_on())
    client = app.test_client()
    _open_window(client)
    _report_turn(app, client, model)
    response = client.post("/api/chat/session/new", json={}, headers=_headers(app, "new-1"))
    assert response.status_code == 200, response.get_json()
    assert client.get("/api/progress/current").get_json()["report"] is None
    clock.now += 11 * MIN
    assert app.extensions["progress"].tick() is False


def test_chat_stop_of_a_continuation_turn_ends_the_chain(make_app, clock):
    model = ClockModel(clock)
    app = make_app(model, env=_on())
    client = app.test_client()
    _open_window(client)
    _report_turn(app, client, model)
    gate = threading.Event()
    model.script = [[{"wait": gate}, *text_turn("working")]]
    clock.now += 10 * MIN
    tracker = app.extensions["progress"]
    assert tracker.tick() is True
    response = client.post("/api/chat/cancel", json={}, headers=_headers(app))
    assert response.status_code == 202, response.get_json()
    gate.set()
    lock = app.extensions["turn_lock"]
    assert lock.acquire(timeout=10)
    lock.release()
    turns = client.get("/api/turns?limit=1").get_json()["turns"]
    assert turns[0]["stop"] == "cancelled"
    clock.now += 60 * MIN
    assert tracker.tick() is False


def test_hold_keeps_the_report_and_cancels_only_the_continue(make_app, clock):
    model = ClockModel(clock)
    app = make_app(model, env=_on())
    client = app.test_client()
    _open_window(client)
    _frame, report = _report_turn(app, client, model)
    held = client.post(f"/api/progress/{report['report_id']}/hold", headers=_headers(app))
    assert held.get_json()["held"] is True
    current = client.get("/api/progress/current").get_json()["report"]
    assert current["pending"] is True and current["auto"] is None
    clock.now += 11 * MIN
    assert app.extensions["progress"].tick() is False


# --- what a report holds, and its restore ---------------------------------------------------


def test_report_holds_no_file_content_command_or_secret(make_app, clock):
    model = ClockModel(clock)
    app = make_app(model, env=_on())
    client = app.test_client()
    _open_window(client)
    secret = "AKIA" + "ABCDEFGHIJKLMNOP"
    note = f"Wrote a.md.\nkey={secret}\n```\nSECRET-CONTENT-123\nrm -rf /tmp/x\n```\nNext: b"
    frame, report = _report_turn(app, client, model, note=note)
    recorded = json.dumps(_progress_events(client, frame["turn_id"]), ensure_ascii=False)
    shown = json.dumps(report, ensure_ascii=False)
    for text in (recorded, shown):
        assert "SECRET-CONTENT-123" not in text and secret not in text and "rm -rf" not in text
    assert "[code omitted]" in report["note"]


def test_a_reload_restores_the_pending_report_from_the_turn_events(make_app, clock):
    model = ClockModel(clock)
    app = make_app(model, env=_on())
    client = app.test_client()
    _open_window(client)
    frame, report = _report_turn(app, client, model)
    again = app.test_client().get("/api/progress/current").get_json()["report"]
    assert again["report_id"] == report["report_id"] and again["pending"] is True
    assert again["turn_id"] == frame["turn_id"] and again["tools"] == {"file_write": [1, 0]}
    assert again["yolo"]["tier"] == "internal" and again["yolo"]["remaining_sec"] > 0


def test_after_a_server_restart_current_is_null_and_nothing_continues(make_app, clock):
    """PR #489 review (minor), measured: phase 1 restores after a page reload only. After a
    restart the window has ended (``revoked(stop)``), ``current`` is null and no continue
    starts; the report stays in the turn events (restore across a restart: #446)."""
    model = ClockModel(clock)
    app = make_app(model, env=_on())
    client = app.test_client()
    _open_window(client)
    frame, _report = _report_turn(app, client, model)
    app.extensions["lifecycle"].shutdown()
    again = make_app(ClockModel(clock), env=_on(), reuse=0)
    fresh = again.test_client()
    body = fresh.get("/api/progress/current").get_json()
    assert body["report"] is None and body["window_open"] is False
    kept = [e["type"] for e in _progress_events(fresh, frame["turn_id"])]
    assert kept == ["progress.report"]
    clock.now += 11 * MIN
    assert again.extensions["progress"].tick() is False


# --- off by default, the dialog's tick, unchanged tools ------------------------------------


def test_off_by_default_nothing_changes(make_app, clock):
    model = ClockModel(clock)
    app = make_app(model)
    client = app.test_client()
    _open_window(client)
    model.script = [
        _write(app, "a.md", use="w1"),
        [{"advance": 120 * MIN}, *_write(app, "b.md", use="w2")],
        text_turn("done"),
    ]
    frame = _turn_frame(_chat(app, client, "k1"))
    assert "stop" not in frame and (_workspace(app) / "b.md").exists()
    assert _progress_events(client, frame["turn_id"]) == []
    current = client.get("/api/progress/current").get_json()
    assert current["report"] is None and current["reports_on"] is False


def test_the_dialog_tick_decides_for_its_window(make_app, clock):
    model = ClockModel(clock)
    app = make_app(model)  # settings off
    client = app.test_client()
    _open_window(client, progress=True)
    assert client.get("/api/progress/current").get_json()["reports_on"] is True
    _report_turn(app, client, model)


def test_the_dialog_untick_wins_over_the_setting(make_app, clock):
    model = ClockModel(clock)
    app = make_app(model, env=_on())
    client = app.test_client()
    _open_window(client, progress=False)
    assert client.get("/api/progress/current").get_json()["reports_on"] is False


def test_a_tick_of_a_cancelled_press_never_reaches_the_next_window(make_app, clock):
    """Review of PR #489 (major 1): the tick belongs to its own press. A press with the tick
    that is cancelled, then a press without the field: the next window keeps the default
    (off here) — the old ``true`` is not carried over to a window nobody ticked."""
    model = ClockModel(clock)
    app = make_app(model)  # settings off
    client = app.test_client()
    first = client.post("/api/yolo/request", json={"tier": "internal", "progress": True})
    assert first.status_code == 202, first.get_json()
    cancelled = client.post(
        "/api/yolo/request/cancel", json={"request_id": first.get_json()["request"]["id"]}
    )
    assert cancelled.status_code == 200, cancelled.get_json()
    _open_window(client)  # no ``progress`` field
    assert client.get("/api/progress/current").get_json()["reports_on"] is False


def test_an_untick_of_a_cancelled_press_never_reaches_the_next_window(make_app, clock):
    model = ClockModel(clock)
    app = make_app(model, env=_on())  # settings on
    client = app.test_client()
    first = client.post("/api/yolo/request", json={"tier": "internal", "progress": False})
    client.post("/api/yolo/request/cancel", json={"request_id": first.get_json()["request"]["id"]})
    _open_window(client)
    assert client.get("/api/progress/current").get_json()["reports_on"] is True


def test_tool_specs_and_prompt_are_the_same_with_reports_on_and_off(make_app, clock):
    """Phase 1 adds no tool and no prompt text (ADR 0014 §7): the tool list equals the one
    with reports off, the tools hash of #254 does not change across a plain turn, a
    checkpoint turn and the turn after it, and the checkpoint's words reach the model only
    as the cancelled call's tool result — never in a system prompt. (The static system hash
    itself moves every turn on main too: it holds the latest episodes.)"""
    model = ClockModel(clock)
    off_app = make_app(model)
    _open_window(off_app.test_client())
    model.script = [text_turn("hi")]
    _chat(off_app, off_app.test_client(), "k0")
    off_tools = model.requests[-1]["tool_names"]

    model = ClockModel(clock)
    app = make_app(model, env=_on())
    client = app.test_client()
    _open_window(client)
    agent = app.extensions["test_agent"]
    hashes = []
    model.script = [text_turn("hi")]
    _chat(app, client, "k1")
    hashes.append(agent.last_prompt_cache["tools_hash"])
    _report_turn(app, client, model)
    hashes.append(agent.last_prompt_cache["tools_hash"])
    model.script = [text_turn("ok")]
    _chat(app, client, "k3", "続けてください。")
    hashes.append(agent.last_prompt_cache["tools_hash"])
    assert len(set(hashes)) == 1, hashes
    assert all(r["tool_names"] == off_tools for r in model.requests)
    assert not any("Checkpoint" in (r["system_prompt"] or "") for r in model.requests)
    nudged = [
        m
        for r in model.requests
        for m in r["messages"]
        if NUDGE_TEXT in json.dumps(m, ensure_ascii=False)
    ]
    assert nudged and all("toolResult" in m["content"][0] for m in nudged)


# --- the three points ADR 0014 marked "to be measured in code" ------------------------------


def _sdk_agent(model, hooks=(), tools=()):
    from strands import Agent

    return Agent(model=model, tools=list(tools), hooks=list(hooks), callback_handler=None)


def test_measured_a_stop_flag_set_by_a_before_tool_hook_reaches_the_event_loop():
    """(1) ``invocation_state["request_state"]["stop_event_loop"]`` set in a
    ``BeforeToolCallEvent`` callback is the dict the event loop reads: no further model call.
    (3) That end leaves the history ending in the tool result (a ``user`` message), so the
    next message is a second ``user`` message in a row — which is why phase 1 ends a hard
    checkpoint with ``AfterToolsEvent.end_turn`` (one assistant line) instead."""
    from strands import tool
    from strands.hooks import BeforeToolCallEvent, HookProvider

    @tool
    def probe() -> str:
        """A tool that does nothing."""
        return "ran"

    class StopHook(HookProvider):
        def register_hooks(self, registry, **kwargs):
            registry.add_callback(BeforeToolCallEvent, self.before)

        def before(self, event):
            event.invocation_state.setdefault("request_state", {})["stop_event_loop"] = True

    model = FakeModel([tool_turn("probe", {}, "p1"), text_turn("never reached")])
    agent = _sdk_agent(model, [StopHook()], [probe])
    agent("go")
    assert len(model.requests) == 1  # (1) the flag stopped the loop after the tool round
    assert [m["role"] for m in agent.messages] == ["user", "assistant", "user"]
    assert "toolResult" in agent.messages[-1]["content"][0]
    model.script = [text_turn("ok")]
    agent("next")
    roles = [m["role"] for m in model.requests[-1]["messages"]]
    assert roles[-2:] == ["user", "user"]  # (3) two user messages in a row reach the model


def test_measured_end_turn_after_tools_keeps_the_history_alternating():
    from strands import tool
    from strands.hooks import AfterToolsEvent, HookProvider

    @tool
    def probe() -> str:
        """A tool that does nothing."""
        return "ran"

    class EndHook(HookProvider):
        def register_hooks(self, registry, **kwargs):
            registry.add_callback(AfterToolsEvent, self.after)

        def after(self, event):
            event.end_turn = HARD_END_TEXT

    model = FakeModel([tool_turn("probe", {}, "p1"), text_turn("never reached")])
    agent = _sdk_agent(model, [EndHook()], [probe])
    agent("go")
    assert len(model.requests) == 1
    assert [m["role"] for m in agent.messages] == ["user", "assistant", "user", "assistant"]
    assert agent.messages[-1]["content"] == [{"text": HARD_END_TEXT}]


def test_measured_handoff_to_user_with_breakout_ends_a_stira_turn(make_app, clock):
    """(2) ``handoff_to_user(breakout_of_loop: true)`` ends a Stira chat turn: the model is
    not called again (the upstream flag reaches Stira's event loop). Unchanged by reports."""
    model = ClockModel(clock)
    app = make_app(model, env=_on())
    client = app.test_client()
    _open_window(client)
    model.script = [
        tool_turn("handoff_to_user", {"message": "over to you", "breakout_of_loop": True}, "h1"),
        text_turn("never reached"),
    ]
    frame = _turn_frame(_chat(app, client, "k1"))
    assert "stop" not in frame
    assert len(model.requests) == 1
    assert _progress_events(client, frame["turn_id"]) == []


def test_the_stop_tool_still_ends_the_turn(make_app, clock):
    model = ClockModel(clock)
    app = make_app(model, env=_on())
    client = app.test_client()
    _open_window(client)
    model.script = [tool_turn("stop", {"reason": "done"}, "s1"), text_turn("never reached")]
    _chat(app, client, "k1")
    assert len(model.requests) == 1


def test_an_automatic_continue_is_not_offered_delegate(make_app, clock):
    """PR #493 review round 2, minor 3: ``delegate`` (a card that spends tokens) is offered
    in the turn a person started, not in the automatic continue of a window (nobody may
    be watching); the rest of the tool list is the same. YOLO still covers that turn."""
    model = ClockModel(clock)
    app = make_app(model, env=_on())
    client = app.test_client()
    _open_window(client)
    _frame, _report = _report_turn(app, client, model)
    person = model.requests[0]["tool_names"]
    assert "delegate" in person
    tracker = app.extensions["progress"]
    model.script = [text_turn("continued on my own")]
    clock.now += 10 * MIN
    assert tracker.tick() is True
    lock = app.extensions["turn_lock"]
    assert lock.acquire(timeout=10)  # the continuation turn has ended
    lock.release()
    history = client.get("/api/chat/history").get_json()["turns"]
    assert history[-1]["auto_continue"] is True
    automatic = model.requests[-1]["tool_names"]
    assert "delegate" not in automatic
    assert set(person) - set(automatic) == {"delegate"}
