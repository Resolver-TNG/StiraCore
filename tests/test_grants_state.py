"""The goal × grant state machine, row by row (ADR 0011 §8, §11; W4 of #296).

Pure functions, fake wall + boot + mono clocks; nothing here reads a real clock."""

from __future__ import annotations

from dataclasses import replace

import pytest

from stiracore.authority import AuthorityError, GrantRecord, require_kind
from stiracore.authority.lifecycle_events import GRANT_REASONS
from stiracore.grants import state
from stiracore.grants.schema import GrantRefused
from tests.grants_helpers import FakeClocks, request

ID = "a" * 32


def fresh(clocks=None, **overrides):
    clocks = clocks or FakeClocks()
    return state.new_grant(request(**overrides), grant_id=ID, issuer="dashboard", now=clocks())


def _claim(g):
    """The agent's claim with a clock reading of now (W6c: the claim takes one)."""
    return state.claim(g, now=FakeClocks()())


def refused(code):
    return pytest.raises(GrantRefused, match=code)


# --- issue, kind discriminator -------------------------------------------------------------


def test_a_new_grant_is_active_with_an_active_goal():
    g = fresh()
    assert (g.status, g.goal_status, g.kind, g.session_ref) == (
        "active",
        "active",
        "goal",
        "grant:" + ID,
    )
    assert g.generation == 1 and g.used_tokens == 0 and g.iterations == 0


def test_the_kind_discriminator_keeps_goal_grants_and_yolo_windows_apart():
    g = fresh()
    record = state.authority_record(g, "b" * 32)
    assert require_kind(record, "goal") is record
    with pytest.raises(AuthorityError, match="grant_kind_mismatch"):
        require_kind(record, "yolo")
    window = GrantRecord(
        kind="yolo", grant_id=ID, incarnation="b" * 32, generation=1, state="active"
    )
    for fn in (state.pause_user, state.claim, state.is_terminal, state.headline):
        with pytest.raises(AuthorityError, match="grant_kind_mismatch"):
            fn(window)
    with pytest.raises(AuthorityError, match="grant_kind_mismatch"):
        state.revoke(replace(g, kind="yolo"), "user", wall=0.0)


# --- goal not active => grant paused(goal_<status>) ---------------------------------------


@pytest.mark.parametrize(
    ("make", "goal", "reason"),
    [
        (lambda g: state.block_goal(g, "no_progress"), "blocked", "goal_blocked"),
        (lambda g: state.pause_goal(g), "paused", "goal_paused"),
        (lambda g: state.charge(g, 1000), "budget_limited", "goal_budget_limited"),
    ],
)
def test_a_goal_that_is_not_active_pauses_its_grant(make, goal, reason):
    g = make(fresh())
    assert (g.goal_status, g.status, g.reason) == (goal, "paused", reason)
    assert state.headline(g)["subject"] == "goal"  # the higher-priority one is shown


def test_awaiting_verification_does_not_pause_the_grant():
    g = _claim(fresh())
    assert (g.goal_status, g.status) == ("awaiting_verification", "active")


def test_a_grant_pause_never_overwrites_the_goals_blocked_or_budget_limited():
    for make in (lambda g: state.block_goal(g, "loop_detected"), lambda g: state.charge(g, 5000)):
        g = make(fresh())
        before = (g.goal_status, g.goal_reason)
        g = state.halt(g, "breaker_warning")
        g = state.pause_user(g)
        assert (g.goal_status, g.goal_reason) == before
        assert g.status == "halted"


def test_a_goal_stop_while_halted_changes_what_the_halt_resumes_to():
    g = state.halt(fresh(), "breaker_warning")
    g = state.block_goal(g, "same_blocker")
    assert (g.status, g.resume_status, g.resume_reason) == ("halted", "paused", "goal_blocked")
    g = state.resume(g, breaker_level="normal")
    assert (g.status, g.reason) == ("paused", "goal_blocked")


# --- terminal states are final ------------------------------------------------------------


@pytest.mark.parametrize(
    "end",
    [
        lambda g: state.revoke(g, "user", wall=1.0),
        lambda g: state.expire(g, wall=1.0),
        lambda g: state.revoke(g, "kill_switch", wall=1.0),
        lambda g: state.restart(g, wall=1.0),
    ],
)
def test_terminal_grant_states_are_final(end):
    g = end(fresh())
    assert state.is_terminal(g) and g.goal_status == "unmet"
    assert g.goal_reason == f"{g.status}_{g.reason}"
    for later in (
        lambda x: state.halt(x, "breaker_critical"),
        lambda x: state.revoke(x, "session_reset", wall=2.0),
        lambda x: state.expire(x, wall=2.0),
        lambda x: state.block_goal(x, "no_progress"),
        lambda x: state.couple(x),
        lambda x: state.clock_check(x, FakeClocks(wall=0.0)()),
    ):
        assert later(g) == g
    # #643: a stopped iteration that consumed an effect still counts; nothing else moves.
    for progress in (None, False, True):
        counted = state.count_iteration(g, progress=progress)
        assert counted.iterations == g.iterations + 1
        assert replace(counted, iterations=g.iterations, generation=g.generation) == g
    with refused("grant_terminal"):
        state.resume(g, breaker_level="normal")
    with refused("grant_terminal"):
        state.pause_user(g)
    with refused("grant_not_active"):
        _claim(g)


def test_a_late_verification_is_recorded_and_never_revives():
    g = state.revoke(_claim(fresh()), "user", wall=1.0)
    passed = state.verify(g, "confirm", wall=2.0)
    assert (passed.status, passed.goal_status, passed.late_verification) == (
        "revoked",
        "unmet",
        "pass",
    )
    failed = state.verify(state.expire(fresh(), wall=1.0), "reject", wall=2.0)
    assert (failed.status, failed.late_verification) == ("expired", "fail")
    events = state.transitions(g, passed)
    assert events == [("goal", "unmet", "unmet", "late_verification_pass")]


# --- verification ------------------------------------------------------------------------


def test_complete_only_when_the_user_confirmed_while_the_grant_was_active():
    g = _claim(fresh())
    done = state.verify(g, "confirm", wall=3.0)
    assert (done.goal_status, done.goal_reason, done.status, done.reason) == (
        "complete",
        "verified",
        "ended",
        "complete",
    )
    with refused("grant_not_awaiting"):
        state.verify(fresh(), "confirm", wall=3.0)  # no claim: never complete
    halted = state.halt(g, "breaker_warning")
    with refused("grant_not_active"):
        state.verify(halted, "confirm", wall=3.0)
    paused = state.pause_user(g)
    with refused("grant_not_active"):
        state.verify(paused, "confirm", wall=3.0)


def test_an_agent_claim_only_moves_the_goal_to_awaiting_verification():
    g = _claim(fresh())
    assert g.goal_status == "awaiting_verification"
    with refused("grant_not_active"):
        _claim(g)  # never further than that
    with refused("grant_not_active"):
        _claim(state.pause_user(fresh()))


def test_a_rejection_is_a_no_progress_tick_and_three_block_the_goal():
    g = fresh(no_progress_ticks=3)
    for streak in (1, 2):
        g = state.verify(_claim(g), "reject", wall=1.0)
        assert (g.goal_status, g.no_progress, g.status) == ("active", streak, "active")
    g = state.verify(_claim(g), "reject", wall=1.0)
    assert (g.goal_status, g.goal_reason, g.status, g.reason) == (
        "blocked",
        "no_progress",
        "paused",
        "goal_blocked",
    )


def test_a_halt_during_awaiting_verification_resumes_to_it():
    g = state.halt(_claim(fresh()), "breaker_critical")
    g = state.resume(g, breaker_level="normal")
    assert (g.status, g.goal_status, g.recheck) == ("active", "awaiting_verification", True)


# --- stops and resume ----------------------------------------------------------------------


def test_halted_resume_returns_to_the_state_before_the_stop():
    g = state.halt(state.pause_user(fresh()), "breaker_warning")
    assert (g.status, g.resume_status, g.resume_reason) == ("halted", "paused", "user")
    g = state.resume(g, breaker_level="normal")
    assert (g.status, g.reason, g.recheck) == ("paused", "user", True)
    g = state.resume(g, breaker_level="normal")
    assert (g.status, g.reason) == ("active", "")


def test_a_halt_resume_is_refused_while_the_breaker_is_not_normal():
    g = state.halt(fresh(), "breaker_warning")
    for level in ("warning", "coherence", "critical", "unknown", None):
        with refused("grant_still_dangerous"):
            state.resume(g, breaker_level=level)


def test_a_stronger_halt_replaces_a_weaker_one_and_keeps_the_target():
    g = state.halt(state.pause_user(fresh()), "breaker_warning")
    g = state.halt(g, "breaker_critical")
    assert (g.reason, g.resume_status, g.resume_reason) == ("breaker_critical", "paused", "user")
    assert state.halt(g, "breaker_warning") == g  # never weaker


@pytest.mark.parametrize(
    ("level", "reason"),
    [
        ("normal", None),
        ("NORMAL", None),
        ("warning", "breaker_warning"),
        ("coherence", "breaker_warning"),
        ("critical", "breaker_critical"),
        ("unknown", "breaker_critical"),
        (None, "breaker_critical"),
    ],
)
def test_the_breaker_level_maps_to_a_halt(level, reason):
    assert state.halt_reason_for(level) == reason


def test_resume_rules_for_pauses():
    with refused("grant_not_stopped"):
        state.resume(fresh(), breaker_level="normal")
    blocked = state.block_goal(fresh(), "approval_timeout")
    g = state.resume(blocked, breaker_level="normal")
    assert (g.status, g.goal_status, g.no_progress) == ("active", "active", 0)
    with refused("grant_budget_exhausted"):
        state.resume(state.charge(fresh(), 1000), breaker_level="normal")
    with refused("grant_resume_needs_new_grant"):
        state.resume(state.block_goal(fresh(), "max_iterations"), breaker_level="normal")


def test_a_user_pause_of_a_stopped_grant_keeps_the_stronger_stop():
    g = state.halt(fresh(), "breaker_critical")
    assert state.pause_user(g) == g


# --- budget, iterations, TTL ---------------------------------------------------------------


def test_the_budget_accumulates_per_grant_and_never_resets():
    g = fresh()
    g = state.charge(g, 400)
    g = state.halt(g, "breaker_warning")
    g = state.resume(g, breaker_level="normal")
    g = state.charge(g, 300)
    assert g.used_tokens == 700  # a resume reset nothing
    g = state.charge(g, 300)
    assert (g.used_tokens, g.goal_status, g.reason) == (
        1000,
        "budget_limited",
        "goal_budget_limited",
    )
    with refused("grant_budget_exhausted"):
        state.resume(g, breaker_level="normal")
    ended = state.revoke(g, "user", wall=1.0)
    assert state.charge(ended, 10).used_tokens == 1010  # still counted, still ended
    with pytest.raises(ValueError):
        state.charge(g, -1)


def test_iterations_block_at_the_maximum_and_no_progress_ticks_block():
    g = fresh(max_iterations=3, no_progress_ticks=5)
    for _ in range(2):
        g = state.count_iteration(g, progress=True)
    g = state.count_iteration(g, progress=True)
    assert (g.iterations, g.goal_status, g.goal_reason) == (3, "blocked", "max_iterations")
    g = fresh(no_progress_ticks=2)
    g = state.count_iteration(state.count_iteration(g, progress=False), progress=False)
    assert (g.goal_status, g.goal_reason, g.status) == ("blocked", "no_progress", "paused")


def test_ttl_never_extends():
    clocks = FakeClocks()
    g = fresh(clocks, ttl_sec=600)
    g = state.halt(g, "breaker_warning")
    g = state.resume(g, breaker_level="normal")
    assert (g.ttl_sec, g.issued_wall) == (600, clocks.wall)
    clocks.tick(599)
    assert state.clock_check(g, clocks()).status == "active"
    clocks.tick(1)
    assert (state.clock_check(g, clocks()).status, state.clock_check(g, clocks()).reason) == (
        "expired",
        "ttl",
    )


# --- clocks: sleep, backwards, forward jump, restart --------------------------------------


def test_a_sleep_keeps_the_ttl_running_without_an_anomaly():
    clocks = FakeClocks()
    g = fresh(clocks, ttl_sec=3600)
    clocks.sleep(1800)
    g = state.clock_check(g, clocks())
    assert g.status == "active" and state.ttl_left(g, clocks()) == 1800
    clocks.sleep(1800)
    g = state.clock_check(g, clocks())
    assert (g.status, g.reason) == ("expired", "ttl")


def test_the_wall_clock_going_back_is_a_clock_anomaly():
    clocks = FakeClocks()
    g = fresh(clocks)
    clocks.tick(60)
    g = state.clock_check(g, clocks())
    clocks.jump_wall(-30)
    g = state.clock_check(g, clocks())
    assert (g.status, g.reason, g.goal_reason) == (
        "revoked",
        "clock_anomaly",
        "revoked_clock_anomaly",
    )


def test_a_forward_wall_jump_is_a_clock_anomaly():
    clocks = FakeClocks()
    g = fresh(clocks)
    clocks.jump_wall(3 * 3600)
    g = state.clock_check(g, clocks())
    assert (g.status, g.reason) == ("revoked", "clock_anomaly")


def test_small_wall_drift_within_the_slack_is_no_anomaly():
    clocks = FakeClocks()
    g = fresh(clocks)
    clocks.tick(100)
    clocks.jump_wall(state.CLOCK_SLACK_SEC - 0.5)
    assert state.clock_check(g, clocks()).status == "active"


def test_an_unreadable_or_non_finite_clock_is_an_anomaly():
    clocks = FakeClocks()
    g = fresh(clocks)
    assert state.clock_check(g, None).reason == "clock_anomaly"
    clocks.wall = float("nan")
    assert state.clock_check(g, clocks()).reason == "clock_anomaly"
    clocks2 = FakeClocks()
    g2 = fresh(clocks2)
    clocks2.mono -= 1  # a monotonic clock going back
    assert state.clock_check(g2, clocks2()).reason == "clock_anomaly"


def test_restart_revokes_every_live_grant():
    for g in (fresh(), state.pause_user(fresh()), state.halt(fresh(), "breaker_warning")):
        out = state.restart(g, wall=9.0)
        assert (out.status, out.reason, out.goal_status) == ("revoked", "restart", "unmet")


def test_a_clock_check_is_no_state_change():
    clocks = FakeClocks()
    g = fresh(clocks)
    clocks.tick(5)
    checked = state.clock_check(g, clocks())
    assert checked.generation == g.generation and checked.last_wall == clocks.wall


# --- derived headline, events ---------------------------------------------------------------


def test_the_headline_is_derived_and_follows_the_priority():
    assert state.headline(fresh()) == {"subject": "grant", "state": "active", "reason": ""}
    blocked = state.block_goal(fresh(), "no_progress")
    assert state.headline(blocked) == {
        "subject": "goal",
        "state": "blocked",
        "reason": "no_progress",
    }
    halted = state.halt(_claim(fresh()), "breaker_warning")
    assert state.headline(halted)["state"] == "halted"  # halt beats awaiting
    budget = state.halt(state.charge(fresh(), 1000), "breaker_warning")
    assert state.headline(budget)["state"] == "halted"  # warning beats budget_limited
    killed = state.revoke(state.charge(fresh(), 1000), "kill_switch", wall=1.0)
    assert state.headline(killed) == {
        "subject": "grant",
        "state": "revoked",
        "reason": "kill_switch",
    }
    assert state.headline(_claim(fresh()))["state"] == "awaiting_verification"
    # the headline is never written back
    assert (blocked.status, blocked.goal_status) == ("paused", "blocked")


def test_every_transition_code_is_in_the_lifecycle_contract():
    assert state.all_reason_codes() <= GRANT_REASONS
    g = fresh()
    assert state.transitions(None, g) == [
        ("grant", "unknown", "active", "grant_issued"),
        ("goal", "unknown", "active", "goal_issued"),
    ]
    halted = state.halt(g, "breaker_warning")
    assert state.transitions(g, halted) == [("grant", "active", "halted", "halted_breaker_warning")]
    resumed = state.resume(halted, breaker_level="normal")
    assert state.transitions(halted, resumed) == [("grant", "halted", "active", "grant_resumed")]


def test_the_wall_clock_going_back_since_the_last_look_alone_is_an_anomaly():
    """Within the slack against issue, but back by more than the slack since the last look."""
    clocks = FakeClocks()
    g = fresh(clocks)
    clocks.tick(100)
    clocks.jump_wall(4)  # ahead by 4 s: within the slack
    g = state.clock_check(g, clocks())
    assert g.status == "active"
    clocks.tick(0.5)
    clocks.jump_wall(-6)  # 2 s behind the boot clock since issue, 5.5 s back since the look
    out = state.clock_check(g, clocks())
    assert (out.status, out.reason) == ("revoked", "clock_anomaly")


def test_a_goal_pause_during_awaiting_verification_resumes_to_it():
    """Astra N5 / ADR §8: claim → pause_goal → resume returns to awaiting_verification."""
    g = state.pause_goal(_claim(fresh()))
    assert (g.goal_status, g.goal_resume, g.status, g.reason) == (
        "paused",
        "awaiting_verification",
        "paused",
        "goal_paused",
    )
    g = state.resume(g, breaker_level="normal")
    assert (g.goal_status, g.goal_resume, g.status) == ("awaiting_verification", "", "active")
    plain = state.resume(state.pause_goal(fresh()), breaker_level="normal")
    assert plain.goal_status == "active"


def test_the_first_late_verification_stays():
    g = state.verify(state.revoke(fresh(), "user", wall=1.0), "confirm", wall=2.0)
    again = state.verify(g, "reject", wall=3.0)
    assert again == g and again.late_verification == "pass"
