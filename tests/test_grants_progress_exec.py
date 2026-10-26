"""W6a (#296): progress by outcome in the goal iteration executor (offline, scripted).

An iteration counts as progress only when its keyed outcome fingerprint (changed files'
final bytes, normalized check outcomes, the report) is new to the grant's recent history.
Rewrites that leave the same bytes, reruns with the same normalized result and reading
only are no progress; a recurrence within six iterations is a loop; the same agent
blocker three times in a row is ``same_blocker``; two no-action iterations block the goal;
a person's turn and other interruptions never touch the streaks. No path, content,
command or output reaches a store, an event, a view or a log.
"""

from __future__ import annotations

import logging

import pytest
from grants_exec_helpers import MARKER, grant, make_env, run_one, use

from stiracore.grants import executor as ex_mod
from stiracore.grants import progress as pg
from stiracore.observability import activity


def _create(env, name, text, n=1):
    return use("editor", {"command": "create", "path": str(env.sub / name), "file_text": text}, n)


def _replace(env, name, old, new, n=1):
    return use(
        "editor",
        {"command": "str_replace", "path": str(env.sub / name), "old_str": old, "new_str": new},
        n,
    )


def _checks_call(env, n=1):
    return use("run_checks", {"path": str(env.sub)}, n)


def _report(code="step_done", kind="progress", n=1):
    return use("goal_report", {"kind": kind, "code": code}, n)


def _result(failed=2, duration=5, tmp="/tmp/a", pid=1):
    return {
        "status": "failed" if failed else "passed",
        "reason": None,
        "checks": [
            {
                "kind": "pytest",
                "status": "failed" if failed else "passed",
                "reason": None,
                "exit_code": 1 if failed else 0,
                "duration_ms": duration,
                "counts": {"passed": 4, "failed": failed, "errors": 0, "skipped": 0},
                "failures": [{"id": f"t[{tmp}]", "message": f"pid {pid}", "text": tmp}],
            }
        ],
        "output_tail": f"pid={pid} {tmp} {duration}ms",
        "_directory": tmp,
        "duration_ms": duration,
    }


def _results(env, results):
    queue = list(results)

    def runner(arguments, run_policy, host=None):
        env.checks.append((dict(arguments), host))
        return queue.pop(0)

    env.executor._run_checks_runner = runner


def _resume(env):
    from grants_helpers import confirm_and_resume

    confirm_and_resume(env.service, env.grant_id, {})


def _iterate(env, *steps):
    env.provider.script = [*([s] for s in steps), [{"text": "ok"}]]
    return run_one(env)


# --- what counts as progress ------------------------------------------------------------------


def test_an_edit_then_a_changed_check_result_is_progress(tmp_path):
    env = make_env(tmp_path)
    _results(env, [_result(failed=2), _result(failed=1)])
    _iterate(env, _create(env, "a.py", "v1\n"), _checks_call(env))
    assert grant(env).no_progress == 0
    _iterate(env, _replace(env, "a.py", "v1", "v2"), _checks_call(env, 2))
    g = grant(env)
    assert g.no_progress == 0 and g.iterations == 2 and g.goal_status == "active"


def test_a_different_edit_with_a_different_failure_is_progress(tmp_path):
    env = make_env(tmp_path)
    _results(env, [_result(failed=3), _result(failed=5)])
    _iterate(env, _create(env, "a.py", "one\n"), _checks_call(env))
    _iterate(env, _create(env, "b.py", "two\n"), _checks_call(env, 2))
    assert grant(env).no_progress == 0 and grant(env).goal_status == "active"


def test_a_write_that_leaves_the_same_bytes_is_no_progress(tmp_path):
    env = make_env(tmp_path)
    (env.sub / "a.md").write_text("same\n")
    view = _iterate(
        env, _replace(env, "a.md", "same", "other"), _replace(env, "a.md", "other", "same", 2)
    )
    assert view["writes"] == 2  # two writes happened (activity) ...
    assert grant(env).no_progress == 1  # ... but nothing changed (outcome)


def test_only_reading_is_no_progress_and_three_block_the_goal(tmp_path):
    env = make_env(tmp_path)
    (env.sub / "a.md").write_text("x\n")
    read = use("file_read", {"path": str(env.sub / "a.md")})
    for n in (1, 2):
        _iterate(env, read, _report("investigating"))
        assert grant(env).no_progress == n and grant(env).goal_status == "active"
    _iterate(env, read, _report("investigating"))
    g = grant(env)
    assert (g.goal_status, g.goal_reason, g.no_progress) == ("blocked", "no_progress", 3)


def test_a_rerun_differing_only_in_volatile_fields_is_the_same_outcome(tmp_path):
    env = make_env(tmp_path)
    _results(
        env,
        [_result(duration=5, tmp="/tmp/x1", pid=11), _result(duration=9999, tmp="/var/y2", pid=77)],
    )
    _iterate(env, _checks_call(env))
    assert grant(env).no_progress == 0  # the first outcome is new
    _iterate(env, _checks_call(env, 2))
    g = grant(env)
    assert g.no_progress == 1 and (g.goal_status, g.goal_reason) == ("blocked", "loop_detected")


def test_a_real_change_in_the_failed_count_is_progress(tmp_path):
    env = make_env(tmp_path)
    _results(env, [_result(failed=4), _result(failed=3)])
    _iterate(env, _checks_call(env))
    _iterate(env, _checks_call(env, 2))
    assert grant(env).no_progress == 0 and grant(env).goal_status == "active"


def test_reverting_to_an_earlier_state_is_a_loop(tmp_path):
    env = make_env(tmp_path)
    _iterate(env, _create(env, "a.md", "v1\n"))
    _iterate(env, _replace(env, "a.md", "v1", "v2"))
    assert grant(env).goal_status == "active" and grant(env).no_progress == 0
    _iterate(env, _replace(env, "a.md", "v2", "v1"))  # A-B-A
    g = grant(env)
    assert (g.goal_status, g.goal_reason) == ("blocked", "loop_detected")


def test_an_alternating_report_alone_never_counts(tmp_path):
    """The report is part of the fingerprint, but with nothing observed it is ``none``."""
    env = make_env(tmp_path)
    for n, code in enumerate(("step_done", "tests_written", "checks_run")):
        _iterate(env, _report(code, n=n))
    g = grant(env)
    assert (g.goal_status, g.goal_reason) == ("blocked", "no_progress")


def test_an_unreadable_digest_is_no_progress(tmp_path, monkeypatch):
    env = make_env(tmp_path)
    real = pg.file_digest
    calls = []

    def digest(path):
        calls.append(path)
        return real(path) if len(calls) == 1 else None  # the final digest is unreadable

    monkeypatch.setattr(pg, "file_digest", digest)
    _results(env, [_result()])
    _iterate(env, _create(env, "a.md", "v1\n"), _checks_call(env))  # a check outcome too
    assert len(calls) == 2 and grant(env).no_progress == 1


def test_an_unreadable_check_result_is_no_progress(tmp_path, monkeypatch):
    env = make_env(tmp_path)
    _results(env, [_result()])

    def broken(_result):
        raise RuntimeError("unreadable")

    monkeypatch.setattr(pg, "normalize_checks", broken)
    _iterate(env, _create(env, "a.md", "v1\n"), _checks_call(env))
    assert grant(env).no_progress == 1


# --- the rules ---------------------------------------------------------------------------------


def test_two_no_action_iterations_block_no_progress(tmp_path):
    env = make_env(tmp_path)
    view = run_one(env)  # the scripted provider answers text only
    assert view["end"] == "no_action" and grant(env).goal_status == "active"
    run_one(env)
    g = grant(env)
    assert (g.goal_status, g.goal_reason) == ("blocked", "no_progress")
    _resume(env)
    run_one(env)  # one after the resume is not enough
    assert grant(env).goal_status == "active"


def test_the_same_blocker_three_times_in_a_row_is_same_blocker(tmp_path):
    env = make_env(tmp_path)
    for n in range(2):
        _iterate(env, _report("needs_decision", "blocked", n))
        assert (grant(env).goal_status, grant(env).goal_reason) == ("blocked", "needs_decision")
        _resume(env)
    _iterate(env, _report("needs_decision", "blocked", 9))
    assert (grant(env).goal_status, grant(env).goal_reason) == ("blocked", "same_blocker")


def test_the_same_blocker_streak_resets_on_another_outcome(tmp_path):
    env = make_env(tmp_path)
    _iterate(env, _report("needs_decision", "blocked", 1))
    _resume(env)
    _iterate(env, _report("needs_decision", "blocked", 2))
    _resume(env)
    _iterate(env, _create(env, "a.md", "v1\n"))  # something else in between
    _iterate(env, _report("needs_decision", "blocked", 3))
    assert grant(env).goal_reason == "needs_decision"


def test_a_claim_is_never_blocked_by_the_loop_detector(tmp_path):
    from grants_helpers import confirm_and_verify

    env = make_env(tmp_path)
    _results(env, [_result(failed=0), _result(failed=0)])
    claim = _report("goal_met", "claim_complete", 2)
    _iterate(env, _checks_call(env), claim)
    confirm_and_verify(env.service, env.grant_id, "reject")
    assert grant(env).goal_status == "active"
    _iterate(env, _checks_call(env, 2), claim)  # the same outcome again: A-A
    assert grant(env).goal_status == "awaiting_verification"  # the person decides


# --- interrupted iterations ----------------------------------------------------------------------


def test_five_user_turns_leave_the_goal_active(tmp_path):
    env = make_env(tmp_path)
    env.provider.on_call = lambda n: env.executor.preempt_signal.arrive()
    for n in range(5):
        assert grant(env).goal_status == "active"
        env.provider.script = [[_create(env, f"f{n}.md", "x\n")]]
        view = run_one(env)
        assert view["end"] == "yielded_user_turn", view
    g = grant(env)
    # each consumed its model call: counted toward max_iterations, never a streak
    assert g.goal_status == "active" and g.no_progress == 0 and g.iterations == 5


def test_a_yield_before_any_effect_is_not_counted(tmp_path):
    env = make_env(tmp_path)

    def probe(point, it):
        if point == "model:authorized":
            env.executor.preempt_signal.arrive()

    env.executor._probe = probe
    for _ in range(3):
        view = run_one(env)
        assert view["end"] == "yielded_user_turn" and env.provider.calls == 0
    g = grant(env)
    assert g.iterations == 0 and g.no_progress == 0 and g.goal_status == "active"


def test_interruptions_keep_the_history_and_the_streaks(tmp_path):
    env = make_env(tmp_path)
    read = use("file_read", {"path": str(env.sub)})
    _iterate(env, read, _report("investigating"))
    assert grant(env).no_progress == 1
    env.provider.on_call = lambda n: env.executor.preempt_signal.arrive()
    env.provider.script = [[_create(env, "y.md", "y\n")]]
    assert run_one(env)["end"] == "yielded_user_turn"
    env.provider.on_call = None
    assert grant(env).no_progress == 1 and grant(env).iterations == 2  # streak untouched
    tracker = env.executor._trackers.get(env.grant_id)
    assert len(tracker.ring) == 1  # the yield added nothing to the history


def test_max_iterations_counts_interrupted_iterations_with_an_effect(tmp_path):
    env = make_env(tmp_path, max_iterations=2)
    env.provider.on_call = lambda n: env.executor.preempt_signal.arrive()
    for n in range(2):
        env.provider.script = [[_create(env, f"f{n}.md", "x\n")]]
        assert run_one(env)["end"] == "yielded_user_turn"
    assert grant(env).goal_reason == "max_iterations"


# --- nothing leaves memory -------------------------------------------------------------------------

PATH_MARKER = "w6amarker-5b0e3c9d71f84a26be"


def test_no_path_content_command_or_output_reaches_any_store(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    env = make_env(tmp_path, rules={"editor": "ask"})
    from stiracore.core.engine import HITLAction, HITLDecision

    env.broker.answer = lambda request: HITLDecision(HITLAction.APPROVE)
    name = f"{PATH_MARKER}.py"
    noisy = _result(tmp=f"/tmp/{PATH_MARKER}", pid=31337)
    noisy["checks"][0]["kind"] = PATH_MARKER
    noisy["reason"] = PATH_MARKER
    _results(env, [noisy, _result(failed=1, tmp=PATH_MARKER)])
    _iterate(env, _create(env, name, f"{PATH_MARKER}\n"), _checks_call(env), _report())
    _iterate(env, _replace(env, name, PATH_MARKER, f"{PATH_MARKER}2"), _checks_call(env, 2))
    assert (env.sub / name).read_text().startswith(PATH_MARKER)
    board = env.executor.board(env.grant_id)
    shown = activity.grant_view(env.service.board_view(), board)
    env.service.stop(5)
    texts = [
        str(board),
        str(shown),
        str(env.executor.view(env.grant_id)),
        str(env.executor.status()),
        str(env.service.view(env.grant_id)),
        repr(env.executor._trackers.get(env.grant_id)),
        caplog.text,
    ]
    for text in texts:
        assert PATH_MARKER not in text
    scanned = 0
    for path in tmp_path.rglob("*"):
        if path.is_file() and env.sub not in path.parents:
            assert PATH_MARKER.encode() not in path.read_bytes(), path
            scanned += 1
    assert scanned  # the grant store, the lifecycle events, the outbox, the approvals db
    assert MARKER not in caplog.text
    tracker = env.executor._trackers.get(env.grant_id)
    assert all(fp is None or (type(fp) is bytes and len(fp) == 32) for fp in tracker.ring)


def test_the_tracker_is_bounded_and_memory_only(tmp_path):
    env = make_env(tmp_path)
    tracker = env.executor._trackers.get(env.grant_id)
    assert tracker.ring.maxlen == pg.RING_SIZE
    assert not hasattr(ex_mod, "open_store")  # no new store
    with pytest.raises(ValueError):
        from stiracore.grants import state

        state.count_iteration(grant(env), progress="yes")
