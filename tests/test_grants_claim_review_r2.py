"""W6c fix round 2 (#296): the round-2 reproductions, ported (Astra r2 B1/B2 seams; Opus
R1-R4). A recorded fail is never forgotten on an unread workspace; the measured codes are
published only if nothing changed up to the publication itself (grant version, authority
epoch / kill, a person's turn); a verification acknowledges only the claim it verified."""

from __future__ import annotations

import pytest
from grants_exec_helpers import grant, make_env
from grants_helpers import confirm_and_verify
from test_grants_claim_evidence import (
    _checks,
    _claim,
    _create,
    _evidence,
    _iterate,
    _iterate_turns,
    _result,
    _results,
    _row,
    _unread_claims,
)

from stiracore.grants import progress as pg
from stiracore.observability import activity

# --- Astra (review-w6c-r2-astra.md) ---------------------------------------------------------


@pytest.mark.parametrize("stop", ["person_turn", "narrow", "pause", "revoke", "kill", "verify"])
def test_person_turn_after_count_before_publication(tmp_path, monkeypatch, stop):
    env = make_env(tmp_path)
    _results(env, [_result()])
    original = env.executor._count_outcome

    def seam(it, end, observed):
        result = original(it, end, observed)
        if end == "claimed":
            if stop == "person_turn":
                env.executor.preempt_signal.arrive()
            elif stop == "narrow":
                env.authority.narrow("policy_changed")
            elif stop == "pause":
                env.service.pause(env.grant_id)
            elif stop == "revoke":
                env.service.revoke(env.grant_id)
            elif stop == "kill":
                env.authority.kill()
            elif stop == "verify":
                confirm_and_verify(env.service, env.grant_id, "confirm")
        return result

    monkeypatch.setattr(env.executor, "_count_outcome", seam)
    _iterate_turns(env, [_create(env, "a.py", "ok\n")], [_checks(env)], [_claim()])
    print("stop after count:", stop, _evidence(env))
    assert _evidence(env).checks == "unknown"


def test_new_claim_between_reject_commit_and_notice_ack(tmp_path, monkeypatch):
    env = make_env(tmp_path)
    _iterate(env, _claim())
    first = env.service._claim_events[env.grant_id][1]  # (claimed_at, event id)
    original = env.service._ack_claim
    seen = {}

    def seam(gid, *args, **kwargs):
        env.clocks.tick(1)
        _iterate(env, _claim(2))  # actual newly admitted executor iteration
        seen["new"] = env.service._claim_events[gid][1]
        original(gid, *args, **kwargs)

    monkeypatch.setattr(env.service, "_ack_claim", seam)
    confirm_and_verify(env.service, env.grant_id, "reject")
    unread = _unread_claims(env)
    print("old, new, waiting, unread:", first, seen, grant(env).goal_status, unread)
    assert first != seen["new"]
    assert grant(env).goal_status == "awaiting_verification"
    assert len(unread) == 1 and unread[0]["event_ids"][-1] == seen["new"]


# --- Opus (review-w6c-r2-opus.md) ---------------------------------------------------------


def _ev(env):
    return grant(env).claim_evidence


def _interrupt_after(env, n):
    def on_call(k):
        if k == n:
            env.executor.preempt_signal.arrive()

    env.provider.on_call = on_call


def test_r1_fail_in_interrupted_iteration_then_narrow_pass_same_files(tmp_path):
    """it1 create; it2 pytest FAILS, then a person's message interrupts (identity unread);
    it3 a narrow compileall pass on the SAME files (nobody edited); it4 claim."""
    env = make_env(tmp_path)
    _results(
        env,
        [
            _result(_row(status="failed", passed=3, failed=5)),
            _result(_row(kind="compileall", passed=0, failed=0)),
        ],
    )
    _iterate(env, _create(env, "a.py", "x\n"))
    before = (env.sub / "a.py").read_bytes()
    _interrupt_after(env, env.provider.calls + 2)
    v2 = _iterate_turns(env, [_checks(env)], [{"text": "more"}])
    env.provider.on_call = None
    v3 = _iterate(env, _checks(env, 2))
    _iterate(env, _claim())
    assert (env.sub / "a.py").read_bytes() == before  # the identical files
    ev = _ev(env)
    shown = activity.grant_view(env.service.board_view(), env.executor.board(env.grant_id))
    print(
        "R1 ends",
        v2["end"],
        v3["end"],
        "evidence",
        ev.checks,
        ev.check_rows,
        shown["claim"]["checks"],
    )
    assert ev.checks != "pass", f"a failing pytest on the identical files is hidden: {ev}"


def test_r1b_same_in_one_iteration_after_interrupted_fail(tmp_path):
    """Variant: the narrow pass and the claim in the same (normal) iteration."""
    env = make_env(tmp_path)
    _results(
        env,
        [
            _result(_row(status="failed", passed=3, failed=5)),
            _result(_row(kind="compileall", passed=0, failed=0)),
        ],
    )
    _iterate(env, _create(env, "a.py", "x\n"))
    _interrupt_after(env, env.provider.calls + 2)
    v2 = _iterate_turns(env, [_checks(env)], [{"text": "more"}])
    env.provider.on_call = None
    _iterate_turns(env, [_checks(env, 2)], [_claim()])
    ev = _ev(env)
    print("R1b", v2["end"], ev.checks, ev.check_rows)
    assert ev.checks != "pass", f"hidden failure: {ev}"


def test_r1c_tracker_unit(tmp_path):
    """The tracker alone: fail on an unread identity, then pass on identity X, claim on X."""
    t = pg.ProgressTracker.__new__(pg.ProgressTracker)
    t.check_codes, t.check_identity, t.claim_identity = None, None, b"i0"
    fail = ("fail", (("pytest", "failed", 3, 5, 0, 1, 0),))
    narrow = ("pass", (("compileall", "passed", 0, 0, 0, 0, 0),))
    t.merge_checks(fail, False, None)
    t.merge_checks(narrow, False, b"X")
    codes = t.claim_codes(b"X")
    print("R1c", codes)
    assert codes[0] != "pass"


def test_r2_write_after_claim_in_the_same_turn(tmp_path):
    env = make_env(tmp_path)
    _results(env, [_result()])
    _iterate_turns(
        env,
        [_create(env, "a.py", "x\n")],
        [_checks(env)],
        [_claim(), _create(env, "b.py", "y\n", 2)],
    )
    ev = _ev(env)
    print("R2", (env.sub / "b.py").exists(), ev.checks, ev.workspace)
    if (env.sub / "b.py").exists():
        assert ev.checks != "pass"


def test_r3_reject_then_claim_ack_and_record(tmp_path):
    env = make_env(tmp_path)
    _results(env, [_result(_row(status="failed", failed=1)), _result()])
    _iterate_turns(env, [_create(env, "a.py", "x\n")], [_checks(env)], [_claim()])
    assert _ev(env).checks == "fail"
    confirm_and_verify(env.service, env.grant_id, "reject")
    shown = activity.grant_view(env.service.board_view(), env.executor.board(env.grant_id))
    assert shown.get("claim") is None  # hidden while the goal runs again
    _iterate_turns(env, [_create(env, "a.py", "z\n", 2)], [_checks(env, 3)], [_claim(4)])
    ev = _ev(env)
    env.recorder.drain()
    unread = [
        n
        for n in env.recorder.outbox(unread_only=True)
        if n["event"]["reason_code"] == "goal_claimed"
    ]
    print("R3", ev.checks, ev.workspace, ev.iteration, len(unread))
    assert ev.checks == "pass" and len(unread) == 1  # the second claim's notice is unread


def test_r4_pause_between_commit_and_measure_publishes_nothing(tmp_path, monkeypatch):
    env = make_env(tmp_path)
    _results(env, [_result()])
    orig = env.service.measure_claim

    def measure(gid, claimed_at, **kw):
        env.service.pause(gid) if hasattr(env.service, "pause") else None
        return orig(gid, claimed_at, **kw)

    monkeypatch.setattr(env.service, "measure_claim", measure)
    _iterate_turns(env, [_create(env, "a.py", "x\n")], [_checks(env)], [_claim()])
    g = grant(env)
    print("R4", g.status, g.goal_status, g.claim_evidence.checks)
    assert g.claim_evidence.checks == "unknown" or g.status == "active"


def test_r1d_known_fail_then_interrupted_pass_then_pass(tmp_path):
    """it1 create + pytest FAIL (normal end, identity read); it2 a narrow pass, interrupted;
    it3 another narrow pass (normal) on the same files; it4 claim."""
    env = make_env(tmp_path)
    narrow = _result(_row(kind="compileall", passed=0, failed=0))
    _results(env, [_result(_row(status="failed", passed=3, failed=5)), narrow, narrow])
    _iterate_turns(env, [_create(env, "a.py", "x\n")], [_checks(env)])
    _interrupt_after(env, env.provider.calls + 2)
    _iterate_turns(env, [_checks(env, 2)], [{"text": "more"}])
    env.provider.on_call = None
    _iterate(env, _checks(env, 3))
    _iterate(env, _claim())
    ev = _ev(env)
    print("R1d", ev.checks, ev.check_rows)
    assert ev.checks != "pass", f"hidden failure: {ev}"
