"""ADR 0014 phase 4 (#451): count triggers — N tool calls / N model tokens bring a checkpoint.

The same offline chat server app as ``tests/test_progress_report.py`` (scripted model, fake
clock, a YOLO window opened through the browser confirmation). Each trigger gives exactly
one ``progress.report`` per turn, the call at the checkpoint never runs, and with the
counts off (the default) nothing changes. The activity board (#446) lists the report and
shows the pending report as the state.
"""

from __future__ import annotations

import json

from stiracore.progress import ProgressTracker, parse_settings
from stiracore.progress.settings import ProgressSettings
from stiracore.yolo import origin
from tests.provider_fakes import text_turn
from tests.test_progress_report import (  # noqa: F401 - the fixtures
    ClockModel,
    _chat,
    _on,
    _open_window,
    _progress_events,
    _turn_frame,
    _workspace,
    _write,
    clock,
    make_app,
)

AGENT = "agent-1"


def _usage(tokens):
    return {"usage": {"inputTokens": tokens, "outputTokens": 0, "totalTokens": tokens}}


# --- settings ------------------------------------------------------------------------------


def test_counts_are_off_by_default_and_read_like_the_other_values():
    settings = parse_settings({}, None)
    assert (settings.count_tools, settings.count_tokens) == (0, 0)
    settings = parse_settings(
        {"STIRA_PROGRESS_COUNT_TOOLS": "12", "STIRA_PROGRESS_COUNT_TOKENS": "50"},
        {"count_tools": 3},
    )
    assert (settings.count_tools, settings.count_tokens) == (12, 1000)  # env wins; floor
    assert settings.sources["count_tools"] == "env"
    assert "count_tokens_rounded" in settings.warnings
    settings = parse_settings({}, {"count_tools": 5000, "count_tokens": "x"})
    assert settings.count_tools == 1000 and settings.count_tokens == 0
    assert {"count_tools_rounded", "count_tokens_invalid"} <= set(settings.warnings)
    assert parse_settings({}, {"count_tools": 0}).count_tools == 0


# --- the tracker as a unit -----------------------------------------------------------------


class Clock:
    now = 100.0

    def __call__(self):
        return self.now


def _tracker(**settings):
    tracker = ProgressTracker(
        ProgressSettings(enabled=True, **settings),
        window_reader=lambda: {"id": "w1", "state": "active"},
        continue_gate=lambda: True,
        clock=Clock(),
        timer=False,
    )
    tracker.turn_started("t1")
    tracker.batch()
    return tracker


def test_n_tool_calls_then_the_next_one_is_the_checkpoint():
    tracker = _tracker(count_tools=3)
    with origin.human_turn(AGENT):
        assert [tracker.check(f"u{n}", AGENT) for n in range(3)] == [None, None, None]
        assert tracker.check("u3", AGENT) is not None
    done = tracker.turn_finished("t1")
    assert (done["trigger"], done["count"], done["cancelled"]) == ("count", "tools", ["u3"])


def test_tokens_since_the_turn_began_bring_the_checkpoint():
    tracker = _tracker(count_tokens=1000)
    with origin.human_turn(AGENT):
        tracker.note_event({"type": "model.completed", "data": {"usage": {"input_tokens": 600}}})
        assert tracker.check("u1", AGENT) is None
        tracker.note_event(
            {
                "type": "model.completed",
                "data": {"usage": {"input_tokens": 300, "output_tokens": 150}},
            }
        )
        tracker.note_event({"type": "model.progress", "data": {"usage": {"input_tokens": 9999}}})
        assert tracker.check("u2", AGENT) is not None
    done = tracker.turn_finished("t1")
    assert (done["trigger"], done["count"]) == ("count", "tokens")


def test_a_person_acting_restarts_the_counts():
    tracker = _tracker(count_tools=2)
    with origin.human_turn(AGENT):
        assert tracker.check("u1", AGENT) is None
        assert tracker.check("u2", AGENT) is None
        tracker.note_contact()  # an approval decision, an answer
        assert tracker.check("u3", AGENT) is None
        assert tracker.check("u4", AGENT) is None
        assert tracker.check("u5", AGENT) is not None


def test_time_wins_when_both_are_due_and_the_counts_off_change_nothing():
    tracker = _tracker(count_tools=1)
    tracker._clock.now += 31 * 60
    with origin.human_turn(AGENT):
        assert tracker.check("u1", AGENT) is not None
    assert tracker.turn_finished("t1")["trigger"] == "time"
    tracker = _tracker()
    with origin.human_turn(AGENT):
        assert all(tracker.check(f"u{n}", AGENT) is None for n in range(200))
        tracker.note_tokens(10**9)
        assert tracker.check("last", AGENT) is None


# --- whole turns (offline chat server) ----------------------------------------------------------


def test_three_tool_calls_give_exactly_one_count_report(make_app, clock):  # noqa: F811 - the imported fixtures
    model = ClockModel(clock)
    app = make_app(model, env=_on(STIRA_PROGRESS_COUNT_TOOLS="3"))
    client = app.test_client()
    _open_window(client)
    model.script = [
        _write(app, "a.md", use="w1"),
        _write(app, "b.md", use="w2"),
        _write(app, "c.md", use="w3"),
        _write(app, "d.md", use="w4"),
        text_turn("Done: a-c. Next: d. Question: go on?"),
    ]
    frame = _turn_frame(_chat(app, client, "k1"))
    assert frame["stop"] == "checkpoint"
    workspace = _workspace(app)
    assert [(workspace / f"{n}.md").exists() for n in "abcd"] == [True, True, True, False]
    events = _progress_events(client, frame["turn_id"])
    assert [e["type"] for e in events] == ["progress.report"]
    data = events[0]["data"]
    assert (data["trigger"], data["count"], data["end"]) == ("count", "tools", "soft")
    assert data["tools"] == {"file_write": [3, 0]}
    # the activity board (#446): the report is a row and the state waits for the answer
    board = client.get("/api/activity").get_json()
    report_rows = [i for i in board["items"] if i["kind"] == "progress"]
    assert report_rows[-1]["what"] == "report" and report_rows[-1]["count"] == "tools"
    assert (board["state"]["code"], board["state"]["count"]) == ("report", "tools")
    # answered: the state is no longer "report" and the answer is a row
    report_id = client.get("/api/progress/current").get_json()["report"]["report_id"]
    model.script = [text_turn("ok")]
    _chat(app, client, "k2", progress_reply={"report_id": report_id, "answer": "continue"})
    board = client.get("/api/activity").get_json()
    assert board["state"]["code"] != "report"
    assert ("progress", "continue") in [(i["kind"], i["what"]) for i in board["items"]]
    assert "SECRET" not in json.dumps(board)


def test_tokens_give_exactly_one_count_report(make_app, clock):  # noqa: F811 - the imported fixtures
    model = ClockModel(clock)
    app = make_app(model, env=_on(STIRA_PROGRESS_COUNT_TOKENS="5000"))
    client = app.test_client()
    _open_window(client)
    model.script = [
        [*_write(app, "a.md", use="w1"), _usage(3000)],
        [*_write(app, "b.md", use="w2"), _usage(3000)],
        _write(app, "c.md", use="w3"),
        text_turn("Report."),
    ]
    frame = _turn_frame(_chat(app, client, "k1"))
    assert frame["stop"] == "checkpoint"
    workspace = _workspace(app)
    assert [(workspace / f"{n}.md").exists() for n in "abc"] == [True, False, False]
    events = _progress_events(client, frame["turn_id"])
    assert [e["type"] for e in events] == ["progress.report"]
    assert (events[0]["data"]["trigger"], events[0]["data"]["count"]) == ("count", "tokens")


def test_counts_off_by_default_a_long_tool_run_has_no_report(make_app, clock):  # noqa: F811 - the imported fixtures
    model = ClockModel(clock)
    app = make_app(model, env=_on())
    client = app.test_client()
    _open_window(client)
    model.script = [*(_write(app, f"f{n}.md", use=f"w{n}") for n in range(8)), text_turn("done")]
    frame = _turn_frame(_chat(app, client, "k1"))
    assert "stop" not in frame
    assert _progress_events(client, frame["turn_id"]) == []
