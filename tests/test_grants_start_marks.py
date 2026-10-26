"""W6c non-blocking (Opus r4), ported for W6d: the iteration's start marks — the authority
epoch (the claim's fence) and the person's-turn counter — are read **before** the
admission, so a narrowing or a person's turn during the admission is seen."""

from __future__ import annotations

import pytest
from grants_exec_helpers import make_env
from test_grants_claim_evidence import (
    _checks,
    _claim,
    _create,
    _evidence,
    _iterate_turns,
    _result,
    _results,
)

from stiracore.grants.schema import GrantRefused


def _run(env):
    _iterate_turns(env, [_create(env, "a.py", "ok\n")], [_checks(env)], [_claim()])
    return _evidence(env).checks


def test_control_a_quiet_admission_publishes_pass(tmp_path):
    env = make_env(tmp_path)
    _results(env, [_result()])
    assert _run(env) == "pass"


@pytest.mark.parametrize("reason", ["tool_rules_saved", "policy_changed"])
def test_a_narrowing_during_the_admission_publishes_nothing(tmp_path, monkeypatch, reason):
    env = make_env(tmp_path)
    _results(env, [_result()])
    original = env.executor._admit

    def seam(grant_id, *args):
        out = original(grant_id, *args)
        env.authority.narrow(reason)  # after the epoch was read, before the thread starts
        return out

    monkeypatch.setattr(env.executor, "_admit", seam)
    assert _run(env) == "unknown"


def test_a_narrowing_before_the_press_is_not_a_stop(tmp_path):
    env = make_env(tmp_path)
    _results(env, [_result()])
    env.authority.narrow("tool_rules_saved")
    assert _run(env) == "pass"


def test_a_persons_turn_during_the_admission_refuses_the_press(tmp_path, monkeypatch):
    """The counter is read before the admission: a turn whose signal came during it (and
    already left the arriving mark) still refuses."""
    env = make_env(tmp_path)
    original = env.executor._admit

    def seam(grant_id, *args):
        out = original(grant_id, *args)
        env.executor.preempt_signal.arrive()  # moves the counter only (no arriving mark)
        return out

    monkeypatch.setattr(env.executor, "_admit", seam)
    try:
        env.executor.step(env.grant_id)
        reason = None
    except GrantRefused as refused:
        reason = refused.extra.get("reason")
    assert reason == "user_turn"
    assert env.executor.wait_idle(5) and env.provider.calls == 0
