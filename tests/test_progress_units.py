"""ADR 0014 phase 1 (#451): the progress tracker, settings and facts as units."""

from __future__ import annotations

import json
import threading

import pytest

from stiracore.progress import NUDGE_TEXT, ProgressTracker, facts, parse_settings
from stiracore.progress.settings import ProgressSettings
from stiracore.progress.tracker import AGAIN_TEXT, HARD_END_TEXT
from stiracore.yolo import origin

AGENT = "agent-1"


class Clock:
    now = 100.0

    def __call__(self):
        return self.now


def _tracker(window=None, **settings):
    clock = Clock()
    state = {
        "window": window if window is not None else {"id": "w1", "state": "active"},
        "gate": True,
    }

    def gate():
        if isinstance(state["gate"], Exception):
            raise state["gate"]
        return state["gate"]

    tracker = ProgressTracker(
        ProgressSettings(enabled=True, **settings),
        window_reader=lambda: state["window"],
        continue_gate=gate,
        clock=clock,
        timer=False,
    )
    return tracker, clock, state


def _due(tracker, clock):
    tracker.turn_started("t1")
    tracker.batch()
    clock.now += 30 * 60


def test_only_a_human_turn_of_this_agent_meets_a_checkpoint():
    tracker, clock, _ = _tracker()
    _due(tracker, clock)
    assert tracker.check("u1", AGENT) is None  # no mark: a webhook / job / bot turn
    with origin.human_turn("another agent"):
        assert tracker.check("u1", AGENT) is None
    with origin.human_turn(AGENT):
        assert tracker.check("u1", AGENT) == NUDGE_TEXT


def test_no_window_or_an_ended_one_has_no_checkpoint():
    for window in ({"id": "w1", "state": "ended"}, {"state": "active"}, {}):
        tracker, clock, state = _tracker(window)
        state["window"] = window
        _due(tracker, clock)
        with origin.human_turn(AGENT):
            assert tracker.check("u1", AGENT) is None, window
    tracker, clock, state = _tracker()
    state["window"] = None
    _due(tracker, clock)
    with origin.human_turn(AGENT):
        assert tracker.check("u1", AGENT) is None


def test_same_batch_is_nudged_a_later_batch_ends_hard_and_never_twice():
    tracker, clock, _ = _tracker()
    _due(tracker, clock)
    with origin.human_turn(AGENT):
        assert tracker.check("u1", AGENT) == NUDGE_TEXT
        assert tracker.check("u2", AGENT) == NUDGE_TEXT  # a parallel call of the same batch
        assert tracker.end_turn_text() is None
        tracker.batch()
        assert tracker.check("u3", AGENT) == AGAIN_TEXT
    assert tracker.end_turn_text() == HARD_END_TEXT
    assert tracker.end_turn_text() is None  # once
    done = tracker.turn_finished("t1")
    assert done["end"] == "hard" and done["cancelled"] == ["u1", "u2", "u3"]
    assert tracker.turn_finished("t1") is None


def test_a_persons_contact_restarts_the_interval():
    tracker, clock, _ = _tracker()
    _due(tracker, clock)
    tracker.note_contact()  # e.g. an approval card decided just now
    with origin.human_turn(AGENT):
        assert tracker.check("u1", AGENT) is None
        clock.now += 30 * 60
        assert tracker.check("u2", AGENT) == NUDGE_TEXT


def test_auto_continue_needs_an_active_window_not_a_paused_one():
    tracker, _clock, state = _tracker()
    state["window"] = {"id": "w1", "state": "paused"}
    assert tracker.report_made("r1", "t1")["auto"] == "window"
    tracker, clock, state = _tracker()
    tracker.window_state()  # binds the window's choice
    assert tracker.report_made("r1", "t1")["auto"] == "scheduled"
    tracker.set_starter(lambda auto: True)
    clock.now += 10 * 60
    state["window"] = {"id": "w1", "state": "paused"}
    assert tracker.tick() is False
    state["window"] = {"id": "w1", "state": "active"}  # resumed later: the continue is gone
    assert tracker.tick() is False


def test_the_tick_rechecks_the_row_count_and_reset_clears_everything():
    tracker, clock, _ = _tracker()
    tracker.window_state()
    tracker.report_made("r1", "t1")
    tracker.set_starter(lambda auto: True)
    tracker._consecutive = 3  # e.g. counted by a continuation that raced the schedule
    clock.now += 10 * 60
    assert tracker.tick() is False
    tracker.report_made("r2", "t2")
    tracker.reset()  # /new
    assert tracker.consecutive() == 0 and tracker.auto_view("r2") is None


def test_a_new_window_does_not_inherit_the_auto_continue():
    tracker, clock, state = _tracker()
    tracker.window_state()
    tracker.report_made("r1", "t1")
    tracker.set_starter(lambda auto: True)
    state["window"] = {"id": "w2", "state": "active"}
    clock.now += 10 * 60
    assert tracker.tick() is False


def test_zero_max_auto_continues_never_schedules():
    tracker, _clock, _ = _tracker(max_auto_continues=0)
    tracker.window_state()
    assert tracker.report_made("r1", "t1")["auto"] == "off"


def test_a_starter_that_fails_starts_nothing_and_counts_nothing():
    tracker, clock, _ = _tracker()
    tracker.window_state()
    tracker.report_made("r1", "t1")

    def broken(auto):
        raise RuntimeError("no")

    tracker.set_starter(broken)
    clock.now += 10 * 60
    assert tracker.tick() is False and tracker.consecutive() == 0


@pytest.mark.parametrize("gate", [False, None, "yes", RuntimeError("unreadable")])
def test_no_continue_unless_the_gate_says_true_and_the_continue_is_dropped(gate):
    """PR #489 review (major 2): the breaker not NORMAL / a latch (the gate) at the due
    time drops the continue; NORMAL again later does not bring it back."""
    tracker, clock, state = _tracker()
    tracker.window_state()
    tracker.report_made("r1", "t1")
    started = []
    tracker.set_starter(lambda auto: started.append(auto) or True)
    state["gate"] = gate
    clock.now += 10 * 60
    assert tracker.tick() is False
    state["gate"] = True
    clock.now += 60 * 60
    assert tracker.tick() is False and started == [] and tracker.consecutive() == 0
    assert tracker.auto_view("r1") is None


def test_without_a_gate_there_is_no_automatic_continue():
    clock = Clock()
    tracker = ProgressTracker(
        ProgressSettings(enabled=True),
        window_reader=lambda: {"id": "w1", "state": "active"},
        clock=clock,
        timer=False,
    )
    tracker.window_state()
    tracker.report_made("r1", "t1")
    tracker.set_starter(lambda auto: True)
    clock.now += 10 * 60
    assert tracker.tick() is False


def test_a_paused_window_drops_the_continue_even_when_it_resumes():
    tracker, clock, state = _tracker()
    tracker.window_state()
    tracker.report_made("r1", "t1")
    tracker.set_starter(lambda auto: True)
    state["window"] = {"id": "w1", "state": "paused"}
    clock.now += 10 * 60
    assert tracker.tick() is False
    state["window"] = {"id": "w1", "state": "active"}
    clock.now += 60
    assert tracker.tick() is False


def test_the_starter_runs_outside_the_state_lock_but_a_cancel_waits_for_it():
    """PR #489 review (minor): the starter's I/O does not hold the state lock (a tool
    boundary's check and the page's poll go on), while a Stop / answer waits for the start
    to finish — it then sees the started turn (never a turn that starts after a Stop)."""
    tracker, clock, _ = _tracker()
    tracker.window_state()
    tracker.report_made("r1", "t1")
    inside, release = threading.Event(), threading.Event()

    def slow(auto):
        inside.set()
        assert release.wait(5)
        return True

    tracker.set_starter(slow)
    clock.now += 10 * 60
    ticking = threading.Thread(target=tracker.tick)
    ticking.start()
    assert inside.wait(5)
    seen = {}
    poll = threading.Thread(target=lambda: seen.update(state=tracker.window_state()))
    poll.start()
    poll.join(2)
    assert not poll.is_alive() and seen["state"]["reports_on"] is True
    cancelled = threading.Event()
    stopping = threading.Thread(target=lambda: (tracker.cancel_auto("stop"), cancelled.set()))
    stopping.start()
    assert not cancelled.wait(0.2)  # waits for the start
    release.set()
    ticking.join(5)
    assert cancelled.wait(5) and tracker.consecutive() == 1


def test_the_dialog_choice_binds_to_its_own_window_only():
    tracker, _clock, state = _tracker()
    tracker.settings = ProgressSettings(enabled=False)
    tracker.note_press(True, window="w1")
    assert tracker.window_state()["reports_on"] is True
    state["window"] = {"id": "w2", "state": "active"}
    assert tracker.window_state()["reports_on"] is False  # no press: the default (off)
    tracker.note_press("yes", window="w3")  # not a bool: no choice
    state["window"] = {"id": "w3", "state": "active"}
    assert tracker.window_state()["reports_on"] is False


def test_a_press_replaces_the_last_press_and_another_window_never_takes_it():
    """PR #489 review, major 1: a tick from a press whose window never opened (cancelled,
    denied, expired) is not used for a later window; a press without the field clears it."""
    tracker, _clock, state = _tracker()
    tracker.settings = ProgressSettings(enabled=False)
    state["window"] = None
    tracker.note_press(True, window="w-cancelled")
    state["window"] = {"id": "w-next", "state": "active"}
    assert tracker.window_state()["reports_on"] is False
    state["window"] = None
    tracker.note_press(True, window="w4")
    tracker.note_press(None, window="w5")  # the next press, no field: the tick is gone
    state["window"] = {"id": "w4", "state": "active"}
    assert tracker.window_state()["reports_on"] is False
    tracker.note_press(True)  # no window: nothing to bind to
    state["window"] = {"id": "w6", "state": "active"}
    assert tracker.window_state()["reports_on"] is False


# --- settings -------------------------------------------------------------------------------


def test_settings_default_off_env_over_config_and_rounding():
    assert parse_settings({}, None).view() == {
        "enabled": False,
        "interval_min": 30,
        "continue_after_min": 10,
        "max_auto_continues": 3,
        # phase 4 (#451): the count triggers, off by default
        "count_tools": 0,
        "count_tokens": 0,
        # phase 3 (#451): the declared checkpoint, off by default
        "declared": False,
        "sources": dict.fromkeys(
            (
                "enabled",
                "interval_min",
                "continue_after_min",
                "max_auto_continues",
                "count_tools",
                "count_tokens",
                "declared",
            ),
            "default",
        ),
    }
    settings = parse_settings(
        {"STIRA_PROGRESS_INTERVAL_MIN": "15"},
        {"enabled": True, "interval_min": 5, "max_auto_continues": True, "x": 1},
    )
    assert settings.enabled is True and settings.sources["enabled"] == "config"
    assert settings.interval_min == 15 and settings.sources["interval_min"] == "env"
    assert settings.max_auto_continues == 3  # a bool is never a number
    assert set(settings.warnings) == {
        "interval_min_rounded",
        "max_auto_continues_invalid",
        "unknown_x",
    }
    assert parse_settings({}, {"interval_min": 1000}).interval_min == 360
    assert parse_settings({"STIRA_PROGRESS_REPORTS": "maybe"}, None).enabled is False
    assert parse_settings({}, "not an object").warnings == ("section",)


# --- facts ----------------------------------------------------------------------------------


def test_tool_counts_read_names_and_statuses_only():
    events = [
        {
            "type": "tool_call.created",
            "data": {
                "tool_use_id": "a",
                "name": "file_write",
                "status": "running",
                "input_preview": "SECRET body",
            },
        },
        {
            "type": "tool_call.updated",
            "data": {
                "tool_use_id": "a",
                "name": "file_write",
                "status": "done",
                "output_preview": "SECRET out",
            },
        },
        {
            "type": "tool_call.created",
            "data": {
                "tool_use_id": "b",
                "name": "shell",
                "status": "running",
                "input_preview": "rm -rf /",
            },
        },
        {
            "type": "tool_call.updated",
            "data": {"tool_use_id": "b", "name": "shell", "status": "error"},
        },
        {
            "type": "tool_call.created",
            "data": {"tool_use_id": "c", "name": "evil name; ls", "status": "done"},
        },
        {
            "type": "tool_call.created",
            "data": {"tool_use_id": "d", "name": "file_write", "status": "denied"},
        },
    ]
    counts = facts.tool_counts(events, skip=["d"])
    assert counts == {"file_write": [1, 0], "other": [1, 0], "shell": [1, 1]}
    assert "SECRET" not in json.dumps(counts) and "rm" not in json.dumps(counts)


def test_clean_note_masks_drops_code_and_caps():
    secret = "AKIA" + "ABCDEFGHIJKLMNOP"
    note = facts.clean_note(f"done\n```python\nprint('x')\n```\nkey {secret}\n" + "l\n" * 30)
    assert "print" not in note and facts.CODE_OMITTED in note and secret not in note
    assert len(note.splitlines()) <= facts.NOTE_LINES + 1
    assert len(facts.clean_note("a" * 5000)) == facts.NOTE_CHARS
    assert facts.clean_note(None) is None and facts.clean_note("  ") is None
    unclosed = facts.clean_note("ok\n```\nleaked file text")
    assert "leaked" not in unclosed


def test_yolo_facts_keep_numbers_and_plain_codes():
    out = facts.yolo_facts(
        {
            "tier": "internal",
            "remaining_sec": 10,
            "used": True,
            "pause_reason": "a b",
            "state": "active",
        }
    )
    assert out == {"tier": "internal", "remaining_sec": 10, "state": "active"}
    assert facts.yolo_facts(None) is None


@pytest.mark.parametrize(
    "usage,expected",
    [
        (
            {"input_tokens": 3, "output_tokens": None, "cache_read_tokens": 2},
            {"input": 3, "cache_read": 2},
        ),
        (None, {}),
    ],
)
def test_usage_facts(usage, expected):
    assert facts.usage_facts(usage) == expected
