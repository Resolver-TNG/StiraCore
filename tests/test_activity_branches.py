"""W5b Part B (#296): every branch of ``activity_item``, ``live_state`` and ``window_view``.

``tests/test_activity_board.py`` covers the main paths; these pin the rest one branch per
case (an exact dict each), so a dropped or added field of the allow-list fails a test.
"""

from __future__ import annotations

import pytest

from stiracore.observability import activity


def _ev(kind, data=None, ts=10.0, turn="t1"):
    return {"type": kind, "data": data, "ts": ts, "turn_id": turn}


BASE = {"id": 7, "ts": 10.0, "turn": "t1"}


@pytest.mark.parametrize(
    ("event", "want"),
    [
        ("not a mapping", None),
        (_ev("model.progress", {}), None),
        (_ev("turn.summary", {}), None),
        (_ev("turn.started", None), {"kind": "turn", "what": "started"}),
        (_ev("turn.started", {"auto_continue": "bad code!"}), {"kind": "turn", "what": "started"}),
        (
            _ev("turn.completed", {"error": "boom", "stop": "max_tokens"}),
            {"kind": "turn", "what": "completed", "ok": False, "stop": "max_tokens"},
        ),
        (
            _ev("turn.completed", {"error": True}),
            {"kind": "turn", "what": "completed", "ok": False},
        ),
        (_ev("turn.completed", {"stop": "a b"}), {"kind": "turn", "what": "completed", "ok": True}),
        (_ev("tool_call.updated", {"status": "running", "name": "x"}), None),
        (_ev("tool_call.created", {"status": "done", "name": "x"}), None),
        (
            _ev(
                "tool_call.updated",
                {"status": "denied", "name": "handoff_to_user", "cancel": "declared"},
            ),
            {"kind": "tool", "what": "declared", "name": "handoff_to_user"},
        ),
        (
            _ev("tool_call.updated", {"status": "timeout", "name": "x", "duration_ms": -1}),
            {"kind": "tool", "what": "timeout", "name": "x", "ok": False},
        ),
        (
            _ev("approval.requested", {"tool_name": 5}),
            {"kind": "approval", "what": "requested", "name": "other"},
        ),
        (
            _ev("approval.resolved", {"decision": "edit"}),
            {"kind": "approval", "what": "edit", "ok": True},
        ),
        (
            _ev("approval.resolved", {"decision": None}),
            {"kind": "approval", "what": "other", "ok": False},
        ),
        (_ev("breaker.transition", {"to": "x y"}), {"kind": "breaker", "what": "other"}),
        (
            _ev("breaker.transition", {"to": "CRITICAL", "from": "WARNING"}),
            {"kind": "breaker", "what": "CRITICAL", "from": "WARNING"},
        ),
        (
            _ev("model.completed", {"status": None, "duration_ms": 2.5}),
            {"kind": "model", "what": "error", "ok": False},
        ),
        (
            _ev("progress.report", {"trigger": "a b", "count": "tokens"}),
            {"kind": "progress", "what": "report", "count": "tokens"},
        ),
        (
            _ev("progress.report", {"trigger": "tools", "count": "free text"}),
            {"kind": "progress", "what": "report", "trigger": "tools"},
        ),
        (_ev("progress.reply", {"answer": 3}), {"kind": "progress", "what": "other"}),
        (_ev("progress.auto_continue", {"x": "y"}), {"kind": "progress", "what": "auto_continue"}),
    ],
)
def test_activity_item_every_branch(event, want):
    got = activity.activity_item(7, event)
    assert got == (None if want is None else {**BASE, **want})


def test_activity_item_bad_ts_and_turn():
    got = activity.activity_item(1, {"type": "turn.started", "ts": float("nan"), "turn_id": "a b"})
    assert got == {"id": 1, "ts": None, "turn": None, "kind": "turn", "what": "started"}
    long = "x" * 40
    assert activity.activity_item(1, _ev("turn.started", turn=long))["turn"] == "x" * 32


def test_live_state_branches():
    events = [
        "not a mapping",
        _ev("turn.started", {}, ts=1.0),
        _ev("tool_call.created", {"tool_use_id": 5, "name": "x", "status": "running"}, ts=2.0),
        _ev("tool_call.updated", {"tool_use_id": "u", "name": "x", "status": "running"}, ts=3.0),
        _ev("model.started", {"started_at": None}, ts=4.0),
        _ev("model.progress", {}, ts=9.0),
    ]
    assert activity.live_state(events) == {
        "code": "generating", "since": 4.0, "last_event": 4.0, "running": True,
    }  # fmt: skip
    ended = events + [_ev("model.completed", {}, ts=5.0)]
    assert activity.live_state(ended)["code"] == "working"
    assert activity.live_state(ended)["since"] == 1.0
    done = ended + [_ev("turn.completed", {}, ts=6.0)]
    assert activity.live_state(done) == {
        "code": "idle", "since": None, "last_event": 6.0, "running": False,
    }  # fmt: skip
    report = activity.live_state(None, report={"ts": 8.0, "trigger": "x y", "count": "tools"})
    assert report == {
        "code": "report", "since": 8.0, "last_event": None, "count": "tools", "running": False,
    }  # fmt: skip
    plain = activity.live_state(None, report={"ts": "x", "trigger": "time", "count": "x"})
    assert plain["since"] is None and plain["trigger"] == "time" and "count" not in plain
    paused = activity.live_state(None, window={"state": "paused", "pause_reason": "a b"})
    assert paused["code"] == "paused" and paused["reason"] == "paused"
    latched = activity.live_state(None, latched=True, breaker="bad level!")
    assert latched["reason"] == "latched" and "breaker" not in latched
    waiting = activity.live_state(None, pending=["x", {"tool_name": "shell", "created_at": 3.0}])
    assert waiting["code"] == "approval" and waiting["waiting"] == 1 and waiting["since"] == 3.0


def test_window_view_branches():
    assert activity.window_view(None) is None
    assert activity.window_view({"window": "x"}) is None
    assert activity.window_view({"window": {}}) == {}
    full = activity.window_view(
        {"window": {"state": "paused", "exec": True, "pause_breaker_reasons": ["a", "b c", 3],
                    "used": -1, "t0": 5.0, "ends_at": "x", "limit": 2**60}}
    )  # fmt: skip
    assert full == {"state": "paused", "exec": True, "pause_breaker_reasons": ["a"], "t0": 5.0}
    none = activity.window_view({"window": {"exec": "yes", "pause_breaker_reasons": ["a b"]}})
    assert none == {}
    many = activity.window_view({"window": {"pause_breaker_reasons": [f"r{n}" for n in range(20)]}})
    assert many == {"pause_breaker_reasons": [f"r{n}" for n in range(8)]}


def test_grant_view_remaining_branches():
    view = {"short": "abcdef01", "state": "halted", "issued_wall": 5.0, "headline": "x"}
    out = activity.grant_view(view, {"iteration": {}, "outcomes": [{"end": "done", "tools": "x"}]})
    assert "headline" not in out  # not a mapping: left out
    assert out["outcomes"] == [{"what": "done", "tools": []}]  # tools not a list: none
    reasoned = activity.grant_view(
        {**view, "headline": {"subject": "grant", "state": "halted", "reason": "breaker_warning"}}
    )
    assert reasoned["headline"] == {
        "subject": "grant",
        "state": "halted",
        "reason": "breaker_warning",
    }
