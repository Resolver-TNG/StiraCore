"""R001 (#697): a rejected claim, claimed again over the same outcome, ends as no progress.

Finding (code review of the executor, 2026-11-10, #697): the agent claims the goal
complete, the person rejects the claim, and the agent claims again without changing
anything — the same files, the same check rows, so the same outcome fingerprint.

Expected: the second claim still goes to the person (a claim is never blocked by a
detector; the person decides), it counts as no progress, and the second reject blocks the
goal as ``no_progress``. It is **not** ``loop_detected``, although the loop detector does
see the repeated fingerprint. Only the person's decision moves the goal out of
``awaiting_verification``; the agent's own claim never completes it.

Offline: a scripted provider, a fake ``run_checks``, hand-moved clocks. No model, no sleep.
"""

from __future__ import annotations

from grants_exec_helpers import grant, make_env, run_one, use
from grants_helpers import confirm_and_verify, events

_ROW = {
    "kind": "pytest",
    "status": "passed",
    "reason": None,
    "exit_code": 0,
    "duration_ms": 12,
    "counts": {"passed": 4, "failed": 0, "errors": 0, "skipped": 0},
    "failures": [],
}


def _same_checks(env) -> None:
    """``run_checks`` answers every call with the same passing row."""

    def runner(arguments, run_policy, host=None):
        env.checks.append((dict(arguments), host))
        return {"status": "passed", "reason": None, "checks": [dict(_ROW)]}

    env.executor._run_checks_runner = runner


def _iterate(env, *turns):
    env.provider.script = [*(list(turn) for turn in turns), [{"text": "ok"}]]
    return run_one(env)


def _checks(env, n):
    return use("run_checks", {"path": str(env.sub)}, n)


def _claim(n):
    return use("goal_report", {"kind": "claim_complete", "code": "goal_met"}, n)


def _reasons(env) -> list[str]:
    return [event["reason_code"] for event in events(env.recorder)]


def test_a_rejected_claim_claimed_again_over_the_same_outcome_blocks_as_no_progress(tmp_path):
    env = make_env(tmp_path)
    _same_checks(env)
    create = {"command": "create", "path": str(env.sub / "a.py"), "file_text": "x\n"}

    view = _iterate(env, [use("editor", create)], [_checks(env, 1)], [_claim(1)])
    assert view["end"] == "claimed"
    g = grant(env)
    assert (g.goal_status, g.no_progress) == ("awaiting_verification", 0)

    confirm_and_verify(env.service, env.grant_id, "reject")
    g = grant(env)
    assert (g.status, g.goal_status, g.no_progress) == ("active", "active", 1)

    env.clocks.tick(5)
    view = _iterate(env, [_checks(env, 2)], [_claim(2)])  # nothing changed: the same outcome
    assert view["end"] == "claimed"
    tracker = env.executor._tracker(env.grant_id)
    ring = list(tracker.ring)
    # The loop detector saw the repeat (its rule fires here; only the claim is exempt: with
    # that exemption removed from the executor this scenario fails at the next assert).
    assert ring[-1] is not None and ring[-1] == ring[-2]
    g = grant(env)
    # ... and the claim still went to the person: counted as no progress, not blocked.
    assert (g.status, g.goal_status, g.goal_reason) == ("active", "awaiting_verification", "")
    assert g.no_progress == 2
    assert (g.claim_evidence.progress, g.claim_evidence.workspace) == ("repeat", "unchanged")

    confirm_and_verify(env.service, env.grant_id, "reject")
    g = grant(env)
    assert (g.status, g.goal_status, g.goal_reason) == ("paused", "blocked", "no_progress")
    assert g.no_progress == 3 and g.iterations == 2

    reasons = _reasons(env)
    assert "blocked_no_progress" in reasons
    assert "blocked_loop_detected" not in reasons
    # The agent's claims completed nothing: only a person's "confirm" does.
    assert reasons.count("goal_claimed") == 2
    assert "goal_verified" not in reasons and "ended_complete" not in reasons
