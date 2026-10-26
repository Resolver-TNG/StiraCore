"""ADR 0014 phase 3 (#451): the declared checkpoint — the agent reports a milestone itself.

The agent declares with the existing ``handoff_to_user(breakout_of_loop: true)``; with
``progress.declared`` on (off by default) and a YOLO window with reports on, the harness
cancels that call before the rule / card / YOLO approver, ends the turn with the call's
message (masked, code dropped, capped) as the one assistant line, and records exactly one
``progress.report`` (``trigger: "declared"``). At most one declared report every 10 minutes.
One fixed guidance paragraph is added to the static prompt while such a window is open —
the same bytes on every turn (#254).

Offline: the scripted model and fake clock of ``tests/test_progress_report.py``.
"""

from __future__ import annotations

import itertools
import json

from stiracore.agent.context_assembler import estimate_tokens
from stiracore.progress import ProgressTracker, parse_settings
from stiracore.progress.settings import ProgressSettings
from stiracore.progress.tracker import (
    AGAIN_TEXT,
    DECLARED_GUIDANCE,
    DECLARED_MIN_GAP_SEC,
    DECLARED_TEXT,
    DECLARED_TOO_SOON_TEXT,
    HARD_END_TEXT,
    NUDGE_TEXT,
)
from stiracore.yolo import origin
from tests.provider_fakes import text_turn, tool_turn
from tests.test_progress_report import (  # noqa: F401 - the fixtures
    MIN,
    ClockModel,
    _chat,
    _events,
    _headers,
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
SECRET = "AKIA" + "ABCDEFGHIJKLMNOP"


def _declared(**extra):
    return _on(STIRA_PROGRESS_DECLARED="1", **extra)


def _handoff(message, use="h1", breakout=True):
    return tool_turn("handoff_to_user", {"message": message, "breakout_of_loop": breakout}, use)


# --- settings ------------------------------------------------------------------------------


def test_declared_is_off_by_default_and_read_like_the_other_switches():
    settings = parse_settings({}, None)
    assert settings.declared is False and settings.view()["declared"] is False
    assert parse_settings({"STIRA_PROGRESS_DECLARED": "1"}, None).declared is True
    on = parse_settings({}, {"declared": True})
    assert on.declared is True and on.sources["declared"] == "config"
    off = parse_settings({"STIRA_PROGRESS_DECLARED": "0"}, {"declared": True})
    assert off.declared is False and off.sources["declared"] == "env"
    bad = parse_settings({}, {"declared": "maybe"})
    assert bad.declared is False and "declared_invalid" in bad.warnings


# --- the tracker as a unit -----------------------------------------------------------------


class Clock:
    now = 100.0

    def __call__(self):
        return self.now


def _tracker(window=None, **settings):
    state = {"window": {"id": "w1", "state": "active"} if window is None else window}
    tracker = ProgressTracker(
        ProgressSettings(enabled=True, **settings),
        window_reader=lambda: state["window"],
        continue_gate=lambda: True,
        clock=Clock(),
        timer=False,
    )
    tracker.window_box = state
    tracker.turn_started("t1")
    tracker.batch()
    return tracker


def test_a_declaration_is_ignored_with_declared_off():
    tracker = _tracker()
    with origin.human_turn(AGENT):
        assert tracker.check("h1", AGENT, declaration="done") is None
    assert tracker.turn_finished("t1") is None
    assert tracker.guidance() == ""


def test_one_declaration_is_one_checkpoint_with_the_cleaned_note():
    tracker = _tracker(declared=True)
    message = f"Done: a.md\n```python\nprint('file text')\n```\nkey {SECRET}"
    with origin.human_turn(AGENT):
        assert tracker.check("w1", AGENT) is None  # an ordinary call runs
        assert tracker.check("h1", AGENT, declaration=message) == DECLARED_TEXT
        assert tracker.check("w2", AGENT) == AGAIN_TEXT  # the rest of the batch: not run
        assert tracker.check("h2", AGENT, declaration="again") == AGAIN_TEXT
    note = tracker.end_turn_text()
    assert note.startswith("Done: a.md") and "[code omitted]" in note
    assert "print" not in note and SECRET not in note
    assert tracker.end_turn_text() is None  # once
    done = tracker.turn_finished("t1")
    assert (done["trigger"], done["end"], done["note"]) == ("declared", "soft", note)
    assert done["cancelled"] == ["h1", "h2", "w2"]


def test_an_empty_message_is_a_hard_end_with_the_fixed_line():
    tracker = _tracker(declared=True)
    with origin.human_turn(AGENT):
        assert tracker.check("h1", AGENT, declaration="   ") == DECLARED_TEXT
    assert tracker.end_turn_text() == HARD_END_TEXT
    done = tracker.turn_finished("t1")
    assert (done["trigger"], done["end"]) == ("declared", "hard") and "note" not in done


def test_only_a_human_turn_in_a_window_with_reports_on_declares():
    tracker = _tracker(declared=True)
    assert tracker.check("h1", AGENT, declaration="done") is None  # not a human turn
    tracker = _tracker(window={"id": "w1", "state": "ended"}, declared=True)
    with origin.human_turn(AGENT):
        assert tracker.check("h1", AGENT, declaration="done") is None
    tracker = _tracker(declared=True)
    tracker.note_press(False, window="w1")  # the dialog's tick off for this window
    with origin.human_turn(AGENT):
        assert tracker.check("h1", AGENT, declaration="done") is None


def test_at_most_one_declared_report_every_ten_minutes():
    tracker = _tracker(declared=True)
    with origin.human_turn(AGENT):
        assert tracker.check("h1", AGENT, declaration="first") == DECLARED_TEXT
    assert tracker.turn_finished("t1")["trigger"] == "declared"
    tracker._clock.now += DECLARED_MIN_GAP_SEC - 1
    tracker.turn_started("t2")
    tracker.batch()
    with origin.human_turn(AGENT):
        assert tracker.check("h2", AGENT, declaration="second") == DECLARED_TOO_SOON_TEXT
        assert tracker.check("w1", AGENT) is None  # the turn goes on
    assert tracker.end_turn_text() is None
    assert tracker.turn_finished("t2") is None  # no report
    tracker._clock.now += 1
    tracker.turn_started("t3")
    tracker.batch()
    with origin.human_turn(AGENT):
        assert tracker.check("h3", AGENT, declaration="third") == DECLARED_TEXT
    assert tracker.turn_finished("t3")["trigger"] == "declared"


def test_the_gap_counts_from_a_time_report_too_and_new_clears_it():
    tracker = _tracker(declared=True)
    tracker._clock.now += 31 * 60
    with origin.human_turn(AGENT):
        assert tracker.check("w1", AGENT) == NUDGE_TEXT
    assert tracker.turn_finished("t1")["trigger"] == "time"
    tracker.turn_started("t2")
    tracker.batch()
    with origin.human_turn(AGENT):
        assert tracker.check("h1", AGENT, declaration="x") == DECLARED_TOO_SOON_TEXT
    tracker.turn_finished("t2")
    tracker.reset()  # /new: a new conversation
    tracker.turn_started("t3")
    tracker.batch()
    with origin.human_turn(AGENT):
        assert tracker.check("h2", AGENT, declaration="y") == DECLARED_TEXT


def test_a_declaration_after_the_time_nudge_is_the_note_it_asked_for():
    tracker = _tracker(declared=True)
    tracker._clock.now += 31 * 60
    with origin.human_turn(AGENT):
        assert tracker.check("w1", AGENT) == NUDGE_TEXT
        tracker.batch()  # the model answered the nudge with the handoff
        assert tracker.check("h1", AGENT, declaration="Done: a.md") == DECLARED_TEXT
    assert tracker.end_turn_text() == "Done: a.md"
    done = tracker.turn_finished("t1")
    assert (done["trigger"], done["end"], done["note"]) == ("time", "soft", "Done: a.md")


def test_the_guidance_is_fixed_bytes_while_a_window_with_reports_is_open():
    tracker = _tracker(declared=True)
    box = tracker.window_box
    first = tracker.guidance()
    assert first == DECLARED_GUIDANCE and "handoff_to_user" in first
    box["window"] = {"id": "w1", "state": "paused"}  # a breaker pause: unchanged
    assert tracker.guidance().encode() == first.encode()
    box["window"] = {"id": "w1", "state": "ended"}
    assert tracker.guidance() == ""
    box["window"] = None
    assert tracker.guidance() == ""
    tracker.note_press(False, window="w2")
    box["window"] = {"id": "w2", "state": "active"}  # reports off for this window
    assert tracker.guidance() == ""
    # Fixed text: no date, no count, no state (the only number is the fixed gap).
    assert DECLARED_GUIDANCE.isascii()
    assert not any(ch.isdigit() for ch in DECLARED_GUIDANCE.replace("10 minutes", ""))


# --- whole turns (offline chat server) ----------------------------------------------------------


def _static(app):
    return app.extensions["test_agent"].prompt_parts_provider().static


def test_one_declaration_gives_exactly_one_report_and_ends_the_turn(make_app, clock):  # noqa: F811
    model = ClockModel(clock)
    app = make_app(model, env=_declared())
    client = app.test_client()
    _open_window(client)
    note = f"Done: a.md is written.\n```\nSECRET-CONTENT-123\n```\nKey {SECRET}. Next: b.md?"
    model.script = [
        _write(app, "a.md", "SECRET-CONTENT-123\n", "w1"),
        [*_handoff(note), *_write(app, "b.md", "never\n", "w2")],
        text_turn("never reached"),
    ]
    frame = _turn_frame(_chat(app, client, "k1"))
    assert frame["stop"] == "checkpoint", frame
    assert len(model.requests) == 2  # ended by the harness after the declaring batch
    assert not (_workspace(app) / "b.md").exists()  # nothing after the declaration ran
    events = _progress_events(client, frame["turn_id"])
    assert [e["type"] for e in events] == ["progress.report"]
    data = events[0]["data"]
    assert (data["trigger"], data["end"]) == ("declared", "soft")
    assert data["tools"] == {"file_write": [1, 0]}  # the cancelled calls are not counted
    text = json.dumps(events, ensure_ascii=False)
    assert "SECRET-CONTENT" not in text and SECRET not in text and "Done" not in text
    # The declaration was cancelled before the rule, the card and the YOLO approver.
    window = client.get("/api/yolo/status").get_json()["window"]
    assert (window["used"], window["reserved"]) == (1, 0)
    assert app.extensions["hitl_broker"].pending() == []
    report = client.get("/api/progress/current").get_json()["report"]
    assert report["pending"] is True and report["trigger"] == "declared"
    assert report["note"].startswith("Done: a.md is written.")
    assert "SECRET-CONTENT" not in report["note"] and SECRET not in report["note"]
    # The note is the turn's last line in the chat history too (the person sees it).
    turn = client.get("/api/chat/history").get_json()["turns"][-1]
    assert turn["response"].endswith(report["note"]) and turn["stop"] == "checkpoint"
    # The next message alternates (the note closed the history as an assistant line).
    model.script = [text_turn("continuing")]
    _chat(
        app,
        client,
        "k2",
        "続けてください。",
        progress_reply={"report_id": report["report_id"], "answer": "continue"},
    )
    roles = [m["role"] for m in model.requests[-1]["messages"]]
    assert all(a != b for a, b in itertools.pairwise(roles)), roles
    [_r, reply] = _progress_events(client, frame["turn_id"])
    assert reply["data"]["answer"] == "continue"


def test_the_declaration_with_text_before_it_ends_with_the_note(make_app, clock):  # noqa: F811
    """The turn's response is its last assistant message, as for every turn: here the
    note (the text streamed before the call is not repeated)."""
    model = ClockModel(clock)
    app = make_app(model, env=_declared())
    client = app.test_client()
    _open_window(client)
    model.script = [[{"text": "Finished the outline."}, *_handoff("Report: outline.md done.")]]
    frame = _turn_frame(_chat(app, client, "k1"))
    assert frame["stop"] == "checkpoint"
    turn = client.get("/api/chat/history").get_json()["turns"][-1]
    assert turn["response"].endswith("Report: outline.md done.")
    assert turn["response"].count("Report: outline.md done.") == 1
    report = client.get("/api/progress/current").get_json()["report"]
    assert report["note"] == "Report: outline.md done."


def test_stop_ends_the_window_after_a_declared_report(make_app, clock):  # noqa: F811
    model = ClockModel(clock)
    app = make_app(model, env=_declared())
    client = app.test_client()
    _open_window(client)
    model.script = [_handoff("Done.")]
    _chat(app, client, "k1")
    report = client.get("/api/progress/current").get_json()["report"]
    response = client.post(f"/api/progress/{report['report_id']}/stop", headers=_headers(app))
    assert response.status_code == 200 and response.get_json()["yolo_stopped"] is True
    window = client.get("/api/yolo/status").get_json()["window"]
    assert window is None or window["state"] not in ("active", "paused")


def test_a_second_declaration_within_ten_minutes_does_not_end_the_turn(make_app, clock):  # noqa: F811
    model = ClockModel(clock)
    app = make_app(model, env=_declared())
    client = app.test_client()
    _open_window(client)
    model.script = [_handoff("First milestone.")]
    first = _turn_frame(_chat(app, client, "k1"))
    report = client.get("/api/progress/current").get_json()["report"]
    model.script = [
        [{"advance": 3 * MIN}, *_handoff("Second milestone.", "h2")],
        _write(app, "c.md", use="w3"),
        text_turn("c.md written"),
    ]
    body = _chat(
        app,
        client,
        "k2",
        "続けてください。",
        progress_reply={"report_id": report["report_id"], "answer": "continue"},
    )
    second = _turn_frame(body)
    assert "stop" not in second  # the turn went on
    assert (_workspace(app) / "c.md").exists()
    assert DECLARED_TOO_SOON_TEXT in json.dumps(model.requests[-2]["messages"][-1])
    assert _progress_events(client, second["turn_id"]) == []
    assert [e["type"] for e in _progress_events(client, first["turn_id"])] == [
        "progress.report",
        "progress.reply",
    ]


def test_declared_off_keeps_handoff_to_user_and_the_prompt_unchanged(make_app, clock):  # noqa: F811
    model = ClockModel(clock)
    app = make_app(model, env=_on())
    client = app.test_client()
    _open_window(client)
    assert DECLARED_GUIDANCE not in _static(app)  # no guidance with declared off
    model.script = [_handoff("over to you"), text_turn("never reached")]
    frame = _turn_frame(_chat(app, client, "k1"))
    assert "stop" not in frame and len(model.requests) == 1
    assert _progress_events(client, frame["turn_id"]) == []
    assert DECLARED_GUIDANCE not in (model.requests[0]["system_prompt"] or "")


def test_breakout_false_is_not_a_declaration(make_app, clock, monkeypatch):  # noqa: F811
    """``breakout_of_loop: false`` is the upstream "wait for input" — unchanged and not a
    report (its stdin read is replaced here: the server has none)."""
    import strands_tools.handoff_to_user as upstream

    monkeypatch.setattr(upstream, "get_user_input", lambda *a, **k: "fine")
    model = ClockModel(clock)
    app = make_app(model, env=_declared())
    client = app.test_client()
    _open_window(client)
    model.script = [_handoff("question?", breakout=False), text_turn("ok")]
    frame = _turn_frame(_chat(app, client, "k1"))
    assert "stop" not in frame and len(model.requests) == 2
    assert _progress_events(client, frame["turn_id"]) == []


def test_the_static_guidance_changes_only_when_a_window_opens_or_ends(make_app, clock):  # noqa: F811
    model = ClockModel(clock)
    app = make_app(model, env=_declared())
    client = app.test_client()
    closed = _static(app)
    assert DECLARED_GUIDANCE not in closed
    model.script = [text_turn("one")]
    _chat(app, client, "k0")
    assert _static(app) == closed
    _open_window(client)
    opened = _static(app)
    assert opened.endswith(DECLARED_GUIDANCE) and opened.startswith(closed[:200])
    model.script = [text_turn("two"), text_turn("three")]
    _chat(app, client, "k1")
    _chat(app, client, "k2")
    one, two = (r["system_prompt"] for r in model.requests[-2:])
    assert DECLARED_GUIDANCE in one and DECLARED_GUIDANCE in two
    assert _static(app).encode("utf-8") == opened.encode("utf-8")  # same bytes per turn
    assert client.post("/api/yolo/stop").status_code == 200
    assert _static(app) == closed  # ended: as before
    # The measured size of the paragraph (the PR body quotes it): "\n\n" + the paragraph,
    # after the Tool Availability section (whose YOLO line changes with the window anyway).
    assert opened.endswith("\n\n" + DECLARED_GUIDANCE)
    assert estimate_tokens(DECLARED_GUIDANCE) < 150


def test_the_report_turn_events_hold_no_note(make_app, clock):  # noqa: F811
    model = ClockModel(clock)
    app = make_app(model, env=_declared())
    client = app.test_client()
    _open_window(client)
    model.script = [_handoff("Milestone: wrote notes/plan.md; question: go on?")]
    frame = _turn_frame(_chat(app, client, "k1"))
    for event in _events(client, frame["turn_id"]):
        if event["type"].startswith("progress."):
            assert "Milestone" not in json.dumps(event, ensure_ascii=False)


def test_the_board_lists_the_declaration_once_as_a_checkpoint(make_app, clock):  # noqa: F811
    """#545: the cancelled declaration is one row ``declared`` (shown 「区切り」), not two
    ``denied`` rows; another call the checkpoint cancelled is one ``denied`` row; the YOLO
    window does not count the declaration as denied."""
    model = ClockModel(clock)
    app = make_app(model, env=_declared())
    client = app.test_client()
    _open_window(client)
    model.script = [
        _write(app, "a.md", "x\n", "w1"),
        [*_handoff("Done."), *_write(app, "b.md", "never\n", "w2")],
        text_turn("never reached"),
    ]
    _turn_frame(_chat(app, client, "k1"))
    items = client.get("/api/activity").get_json()["items"]
    tools = [(i["name"], i["what"], i.get("ok")) for i in items if i["kind"] == "tool"]
    assert tools == [
        ("file_write", "done", True),
        ("handoff_to_user", "declared", None),
        ("file_write", "denied", False),
    ]
    calls = client.get("/api/yolo/status").get_json()["window"]["tool_calls"]
    assert calls["by_decision"]["denied"] == 1  # b.md only (the declaration is not)
    assert calls["by_tool"] == {"file_write": 2}
