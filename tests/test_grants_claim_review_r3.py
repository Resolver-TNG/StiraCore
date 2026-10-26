"""W6c fix round 3 (#296): the round-3 reproductions, ported (Opus R3 2a-2e and 3; Astra r3
publication seams). The publication is fenced against the authority epoch and the person-turn
mark read when the iteration **started**, and the record is installed inside that fence, after
a last read of the turn mark and the kill flag. A stop that lands after the install is a later
event (the record was true of the checked workspace when it was installed: accepted, see
docs/grants.md "Limits")."""

from __future__ import annotations

import threading

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
    _unread_claims,
)

from stiracore.grants import state
from stiracore.grants.executor import ClaimFence
from stiracore.grants.schema import GrantRefused

# --- Opus (review-w6c-r3-opus.md) ------------------------------------------------------------


def _stop(env, stop):
    if stop == "narrow_rules":
        env.authority.narrow("tool_rules_saved")
    elif stop == "narrow_policy":
        env.authority.narrow("policy_changed")
    elif stop == "person_turn":
        env.executor.preempt_signal.arrive()
    elif stop == "pause":
        env.service.pause(env.grant_id)
    elif stop == "revoke":
        env.service.revoke(env.grant_id)
    elif stop == "kill":
        env.authority.kill()


# --- (2a) a stop AFTER the observation (_observe's fresh read) but BEFORE the count's epoch read
@pytest.mark.parametrize(
    "stop", ["narrow_rules", "narrow_policy", "person_turn", "pause", "revoke", "kill"]
)
def test_a_stop_after_the_observation_before_the_count(tmp_path, monkeypatch, stop):
    env = make_env(tmp_path)
    _results(env, [_result()])
    original = env.executor._observe

    def seam(it):
        observed = original(it)
        with it.lock:
            claimed = it.stop_code == "claimed"
        if claimed:
            _stop(env, stop)
        return observed

    monkeypatch.setattr(env.executor, "_observe", seam)
    _iterate_turns(env, [_create(env, "a.py", "ok\n")], [_checks(env)], [_claim()])
    print("R3-2a", stop, _evidence(env).checks, grant(env).status)
    assert _evidence(env).checks == "unknown"


# --- (2b) at the fence itself: just before commit_at_epoch / inside the publish closure ----------
@pytest.mark.parametrize("stop", ["narrow_rules", "person_turn", "arriving", "kill_flag"])
def test_a_stop_right_before_the_fenced_write(tmp_path, monkeypatch, stop):
    env = make_env(tmp_path)
    _results(env, [_result()])
    original = env.authority.commit_at_epoch
    signal = env.executor.preempt_signal

    def seam(commit, *, expected=None):
        if stop == "narrow_rules":
            env.authority.narrow("tool_rules_saved")
        elif stop == "person_turn":
            signal.arrive()
        elif stop == "arriving":
            signal._pending += 1
        elif stop == "kill_flag":
            env.authority._killed = True  # the kill's flag is set first, without a lock
        try:
            return original(commit, expected=expected)
        finally:
            if stop == "arriving":
                signal._pending -= 1

    monkeypatch.setattr(env.authority, "commit_at_epoch", seam)
    _iterate_turns(env, [_create(env, "a.py", "ok\n")], [_checks(env)], [_claim()])
    print("R3-2b", stop, _evidence(env).checks)
    assert _evidence(env).checks == "unknown"


# --- (2c) a kill / revoke from another thread landing while the publish step holds both locks
@pytest.mark.parametrize("who", ["kill", "revoke", "pause", "board"])
def test_concurrent_stop_while_publishing_no_deadlock(tmp_path, monkeypatch, who):
    env = make_env(tmp_path)
    _results(env, [_result()])
    original = env.authority.commit_at_epoch
    box = {}

    def seam(commit, *, expected=None):
        def inner(epoch):
            def other():
                if who == "kill":
                    env.authority.kill()
                elif who == "revoke":
                    env.service.revoke(env.grant_id)
                elif who == "pause":
                    env.service.pause(env.grant_id)
                else:
                    from stiracore.grants.schema import BoardBusy

                    try:
                        box["board"] = env.service.board_view()
                    except BoardBusy:
                        box["board"] = "BoardBusy (never waits)"
                box["done"] = True

            t = threading.Thread(target=other, daemon=True)
            t.start()
            t.join(0.3)  # it must wait for (or pass) the locks, never deadlock
            box["blocked"] = t.is_alive()
            box["t"] = t
            return commit(epoch)

        return original(inner, expected=expected)

    monkeypatch.setattr(env.authority, "commit_at_epoch", seam)
    _iterate_turns(env, [_create(env, "a.py", "ok\n")], [_checks(env)], [_claim()])
    box["t"].join(5)
    print(
        "R3-2c",
        who,
        box.get("blocked"),
        box.get("done"),
        box.get("board"),
        _evidence(env).checks,
        grant(env).status,
    )
    assert box.get("done"), "the other thread never finished (deadlock)"


# --- (2d) two claims racing: a reject + a new claim between the count and the publish -----------
def test_reject_and_new_claim_between_count_and_publish(tmp_path, monkeypatch):
    env = make_env(tmp_path)
    _results(env, [_result()])
    original = env.executor._count_outcome
    done = {}

    def seam(it, end, observed):
        result = original(it, end, observed)
        if end == "claimed" and not done:
            done["x"] = True
            confirm_and_verify(env.service, env.grant_id, "reject")
            env.clocks.tick(1)
            env.service.claim_complete(env.grant_id)  # another claim, not this iteration's
        return result

    monkeypatch.setattr(env.executor, "_count_outcome", seam)
    _iterate_turns(env, [_create(env, "a.py", "ok\n")], [_checks(env)], [_claim()])
    ev = _evidence(env)
    print("R3-2d", ev.checks, ev.workspace, ev.progress, grant(env).goal_status)
    assert ev.checks == "unknown" and ev.progress == "unknown"


# --- (3) verify-ack binding --------------------------------------------------------------------
def test_verify_between_claim_commit_and_its_event_store(tmp_path, monkeypatch):
    """The person verifies after the claim committed but before `_post` stored its event id."""
    env = make_env(tmp_path)
    original = env.service._post
    done = {}

    def seam(ident, kind, before, after, code, epoch, claim=None):
        if code == "goal_claimed" and not done:
            done["x"] = True
            confirm_and_verify(env.service, env.grant_id, "reject")
        return original(ident, kind, before, after, code, epoch, claim)

    monkeypatch.setattr(env.service, "_post", seam)
    _iterate(env, _claim())
    unread = _unread_claims(env)
    print("R3-3a", grant(env).goal_status, len(unread), env.service._claim_events)
    # informational: a verified claim whose notice is still unread (safe side)
    _iterate(env, _claim(2))
    unread2 = _unread_claims(env)
    confirm_and_verify(env.service, env.grant_id, "confirm")
    print("R3-3a next", [n["count"] for n in unread2], len(_unread_claims(env)))
    assert _unread_claims(env) == []


def test_double_late_verify_and_stale_op(tmp_path):
    env = make_env(tmp_path)
    _iterate(env, _claim())
    env.service.revoke(env.grant_id)  # terminal while waiting
    a = confirm_and_verify(env.service, env.grant_id, "confirm")
    try:
        b = confirm_and_verify(env.service, env.grant_id, "reject")["recorded_only"]
    except Exception as e:  # noqa: BLE001
        b = getattr(e, "code", type(e).__name__)
    print("R3-3b", a["recorded_only"], b, len(_unread_claims(env)))
    assert _unread_claims(env) == []


def test_concurrent_double_verify(tmp_path):
    env = make_env(tmp_path)
    _iterate(env, _claim())
    out = []

    def go(d):
        try:
            out.append(("ok", d, confirm_and_verify(env.service, env.grant_id, d)["recorded_only"]))
        except Exception as e:  # noqa: BLE001
            out.append(("err", d, type(e).__name__, getattr(e, "code", None)))

    ts = [threading.Thread(target=go, args=(d,)) for d in ("reject", "confirm")]
    [t.start() for t in ts]
    [t.join(10) for t in ts]
    print("R3-3c", out, grant(env).goal_status, len(_unread_claims(env)))
    assert len([o for o in out if o[0] == "ok"]) >= 1
    if grant(env).goal_status == "active":  # reject won: the next claim must be unread
        _iterate(env, _claim(2))
        assert len(_unread_claims(env)) == 1
    else:
        assert _unread_claims(env) == []


# --- (2e) where the narrowing gap starts: before the observation / between run_checks and claim
@pytest.mark.parametrize("where", ["before_observe", "between_checks_and_claim_turn"])
def test_a_narrowing_earlier_in_the_iteration(tmp_path, monkeypatch, where):
    env = make_env(tmp_path)
    _results(env, [_result()])
    if where == "before_observe":
        original = env.executor._observe

        def seam(it):
            with it.lock:
                claimed = it.stop_code == "claimed"
            if claimed:
                env.authority.narrow("tool_rules_saved")
            return original(it)

        monkeypatch.setattr(env.executor, "_observe", seam)
    else:
        start = env.provider.calls

        def on_call(k):
            if k == start + 3:  # the model call that answers with the claim
                env.authority.narrow("tool_rules_saved")

        env.provider.on_call = on_call
    v = _iterate_turns(env, [_create(env, "a.py", "ok\n")], [_checks(env)], [_claim()])
    ev = _evidence(env)
    print("R3-2e", where, v["end"], ev and ev.checks, grant(env).goal_status)
    assert ev is None or ev.checks == "unknown"  # cancelled before the claim: no claim at all


# --- Astra (review-w6c-r3-astra.md) -----------------------------------------------------------


def setup(env):
    at = env.service.claim_complete(env.grant_id)["claim_evidence"]["claimed_at"]
    sig = env.executor.preempt_signal
    return at, ClaimFence(grant(env).generation, env.authority.epoch, sig, sig.seq)


def publish(env, at, fence):
    return env.service.measure_claim(
        env.grant_id,
        at,
        fence=fence,
        checks="pass",
        rows=(("pytest", "passed", 4, 0, 0, 0, 0),),
        workspace="changed",
        progress="new",
    )


@pytest.mark.parametrize("change", ["narrow", "person_turn"])
def test_after_authority_fence_before_store(tmp_path, monkeypatch, change):
    """Round 3 (ported, decided): the record is installed **inside** the fence — when the
    fence returns, it is already there; a stop after that is a later event, and the pass
    was true of the checked workspace when it was installed (docs/grants.md "Limits")."""
    env = make_env(tmp_path)
    at, fence = setup(env)
    original = env.authority.commit_at_epoch
    occurred = []

    def seam(commit, **kw):
        result = original(commit, **kw)
        # Real authority/person operations after the authority lock is released, while
        # the service still holds its leaf lock.
        if not occurred:
            occurred.append(
                env.service._grants[env.grant_id].claim_evidence.checks
            )  # inside the fence
            if change == "narrow":
                env.authority.narrow("policy_changed")
            elif change == "kill":
                env.authority.kill()
            else:
                env.executor.preempt_signal.arrive()
        return result

    monkeypatch.setattr(env.authority, "commit_at_epoch", seam)
    publish(env, at, fence)
    g = grant(env)
    print("AFTER FENCE", change, g.status, g.claim_evidence.checks, env.authority.killed)
    assert occurred == ["pass"] and g.claim_evidence.checks == "pass"  # linearised before


@pytest.mark.parametrize("change", ["kill", "person_turn"])
def test_after_signal_read_inside_fence(tmp_path, monkeypatch, change):
    env = make_env(tmp_path)
    at, fence = setup(env)
    original = state.measure_claim
    worker = []

    def seam(*args, **kw):
        if change == "person_turn":
            env.executor.preempt_signal.arrive()
        else:
            # Real kill sets its lock-free flag before waiting for the epoch lock.
            flagged = threading.Event()
            orig_kill = env.authority.kill_switch

            # Tracing observes the actual assignment in kill_switch, not a fake flag.
            def trace(frame, event, arg):
                if (
                    event == "line"
                    and frame.f_code.co_name == "kill_switch"
                    and env.authority.killed
                ):
                    flagged.set()
                return trace

            def kill():
                import sys

                sys.settrace(trace)
                orig_kill()

            t = threading.Thread(target=kill)
            worker.append(t)
            t.start()
            assert flagged.wait(3), "kill did not set its real flag"
        return original(*args, **kw)

    monkeypatch.setattr(state, "measure_claim", seam)
    result = publish(env, at, fence)
    for t in worker:
        t.join(5)
        assert not t.is_alive()
    print(
        "INSIDE FENCE",
        change,
        result.claim_evidence.checks,
        grant(env).status,
        grant(env).claim_evidence.checks,
    )
    assert result.claim_evidence.checks == "unknown"


def test_two_claims_race(tmp_path):
    env = make_env(tmp_path)
    barrier = threading.Barrier(3)
    results = []

    def claim():
        barrier.wait()
        try:
            results.append(env.service.claim_complete(env.grant_id))
        except GrantRefused as e:
            results.append(e)

    ts = [threading.Thread(target=claim) for _ in range(2)]
    for t in ts:
        t.start()
    barrier.wait()
    for t in ts:
        t.join(5)
        assert not t.is_alive()
    assert sum(isinstance(x, dict) for x in results) == 1
    assert len(_unread_claims(env)) == 1 and _unread_claims(env)[0]["count"] == 1


def test_stale_verify_and_double_verify(tmp_path):
    env = make_env(tmp_path)
    _iterate(env, _claim())
    old = grant(env)
    confirm_and_verify(env.service, env.grant_id, "reject")
    env.clocks.tick(1)
    _iterate(env, _claim(2))
    with pytest.raises(GrantRefused):
        env.service._verify_now(old, "confirm", old.last_wall)
    assert len(_unread_claims(env)) == 1
    current = grant(env)
    barrier = threading.Barrier(3)
    results = []

    def verify():
        barrier.wait()
        try:
            results.append(env.service._verify_now(current, "reject", current.last_wall))
        except GrantRefused as e:
            results.append(e)

    ts = [threading.Thread(target=verify) for _ in range(2)]
    for t in ts:
        t.start()
    barrier.wait()
    for t in ts:
        t.join(5)
        assert not t.is_alive()
    assert sum(isinstance(x, dict) for x in results) == 1
    assert not _unread_claims(env)


def test_confirm_racing_new_claim(tmp_path, monkeypatch):
    env = make_env(tmp_path)
    _iterate(env, _claim())
    original = env.service._ack_claim
    attempted = []

    def seam(gid, event_id):
        with pytest.raises(GrantRefused):
            env.service.claim_complete(gid)
        attempted.append(True)
        return original(gid, event_id)

    monkeypatch.setattr(env.service, "_ack_claim", seam)
    confirm_and_verify(env.service, env.grant_id, "confirm")
    assert attempted and grant(env).goal_status == "complete"
    assert not _unread_claims(env)


@pytest.mark.parametrize("change", ["revoke", "pause"])
def test_stop_waiting_between_fence_and_store(tmp_path, monkeypatch, change):
    env = make_env(tmp_path)
    at, fence = setup(env)
    original = env.authority.commit_at_epoch
    started = threading.Event()
    workers = []

    def seam(commit, **kw):
        result = original(commit, **kw)

        def stop():
            started.set()
            getattr(env.service, change)(env.grant_id)

        t = threading.Thread(target=stop)
        workers.append(t)
        t.start()
        assert started.wait(3)
        # board must fail fast while publication owns the service lock.
        from stiracore.grants.service import BoardBusy

        with pytest.raises(BoardBusy):
            env.service.board_record()
        return result

    monkeypatch.setattr(env.authority, "commit_at_epoch", seam)
    publish(env, at, fence)
    for t in workers:
        t.join(5)
        assert not t.is_alive()
    assert grant(env).status == ("revoked" if change == "revoke" else "paused")


@pytest.mark.parametrize("change", ["narrow", "person_turn"])
def test_real_executor_publish_gap(tmp_path, monkeypatch, change):
    from test_grants_claim_evidence import _checks, _create, _iterate_turns, _result, _results

    from stiracore.observability import activity

    env = make_env(tmp_path)
    _results(env, [_result()])
    original = env.authority.commit_at_epoch
    hit = []

    def seam(commit, **kw):
        result = original(commit, **kw)
        if commit.__name__ == "publish":
            hit.append(env.service._grants[env.grant_id].claim_evidence.checks)  # inside the fence
            if change == "narrow":
                env.authority.narrow("policy_changed")
            else:
                env.executor.preempt_signal.arrive()
        return result

    monkeypatch.setattr(env.authority, "commit_at_epoch", seam)
    _iterate_turns(env, [_create(env, "a.py", "ok\n")], [_checks(env)], [_claim()])
    shown = activity.grant_view(env.service.board_view(), env.executor.board(env.grant_id))
    print("REAL EXECUTOR", change, grant(env).claim_evidence.checks, shown["claim"]["checks"])
    # decided (round 3): the stop came after the install — a later event; the pass stands
    assert hit == ["pass"] and shown["claim"]["checks"] == "pass"


# --- round 3 (own): the lock-free marks are read last, inside the fence ------------------------


@pytest.mark.parametrize("mark", ["kill_flag", "person_turn", "arriving"])
def test_a_lock_free_mark_set_inside_the_fence_publishes_nothing(tmp_path, monkeypatch, mark):
    """A kill's flag (set without a lock, its thread then waits for the leaf lock) or a
    person's turn signalled after the fence's own entry check: the install reads them last."""
    env = make_env(tmp_path)
    at, fence = setup(env)
    original = env.authority.commit_at_epoch
    signal = env.executor.preempt_signal

    def seam(commit, **kw):
        def inner(epoch):
            if mark == "kill_flag":
                env.authority._killed = True  # what kill() sets before the leaf lock
            elif mark == "person_turn":
                signal.arrive()
            else:
                signal._pending += 1
            return commit(epoch)

        return original(inner, **kw)

    monkeypatch.setattr(env.authority, "commit_at_epoch", seam)
    publish(env, at, fence)
    assert env.service._grants[env.grant_id].claim_evidence.checks == "unknown"
