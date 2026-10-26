"""W6c fix round 1 (#296): the reviewers' reproductions, ported (Astra B1-B3 and the notice
recommendation; Opus P1-P8). The checks evidence is fail-closed: ``pass`` only when positively
established — every check since the last write combined, bound to the workspace identity at the
claim, a consistent result, and published only for a committed claim."""

from __future__ import annotations

import asyncio
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
    _row,
)

from stiracore.grants import progress as pg
from stiracore.observability import activity

# --- Astra (review-w6c-astra.md) ------------------------------------------------------------


def test_external_edit_invalidates_last_pass(tmp_path):
    env = make_env(tmp_path)
    _results(env, [_result()])
    _iterate_turns(env, [_create(env, "a.py", "good\n")], [_checks(env)])
    (env.sub / "a.py").write_text("broken\n")
    _iterate(env, _claim())
    ev = _evidence(env)
    print("external edit evidence:", ev)
    assert ev.checks != "pass", "pass survived a detected external edit after checks"


@pytest.mark.parametrize("counts", [None, "unreadable", {"passed": 4, "failed": 2, "errors": 1}])
def test_unreadable_or_contradictory_counts_never_pass(counts):
    r = _row()
    r["counts"] = counts
    code, rows = pg.claim_checks(_result(r))
    print("unreadable counts:", counts, code, rows)
    assert code == "unknown"


def test_overall_error_with_partial_pass_rows_never_pass():
    code, rows = pg.claim_checks(_result(_row(), status="error"))
    print("overall error:", code, rows)
    assert code != "pass"


def test_interrupted_check_then_external_edit_then_claim(tmp_path):
    env = make_env(tmp_path)
    _iterate(env, _create(env, "a.py", "good\n"))

    def runner(*args, **kwargs):
        env.executor.preempt_signal.arrive()
        return _result()

    env.executor._run_checks_runner = runner
    result = _iterate(env, _checks(env))
    print("check iteration:", result["end"])
    (env.sub / "a.py").write_text("bad\n")
    _iterate(env, _claim())
    print("claim after interrupted check:", _evidence(env))
    assert _evidence(env).checks != "pass"


def test_revoke_between_claim_and_fill_never_publishes_pass(tmp_path):
    env = make_env(tmp_path)
    _results(env, [_result()])
    original = env.service.claim_complete

    def claim_then_revoke(gid):
        view = original(gid)
        env.service.revoke(gid)
        return view

    env.service.claim_complete = claim_then_revoke
    _iterate_turns(env, [_create(env, "a.py", "good\n")], [_checks(env)], [_claim()])
    print("revoked evidence:", grant(env).status, env.service.view(env.grant_id)["claim_evidence"])
    assert _evidence(env).checks == "unknown"


def test_verified_claim_notice_is_acknowledged(tmp_path):
    env = make_env(tmp_path)
    _iterate(env, _claim())
    confirm_and_verify(env.service, env.grant_id, "confirm")
    env.recorder.drain()
    notices = [
        n
        for n in env.recorder.outbox(unread_only=True)
        if n["event"]["reason_code"] == "goal_claimed"
    ]
    print("unread after confirm:", len(notices))
    assert not notices


def test_old_store_real_load(tmp_path):
    from test_grants_claim_evidence import fresh

    from stiracore.grants import schema, state
    from stiracore.grants.store import GrantStore

    g = state.restart(fresh(), wall=1.0)
    stored = schema.grant_to_dict(g, "goal")
    stored.pop("claim_evidence")
    store = GrantStore(tmp_path)
    assert store.write([{"grant": stored, "restart_from": "active"}])
    loaded = GrantStore(tmp_path).load()
    assert loaded.reason is None and loaded.entries[0][0].claim_evidence is None


def test_real_parser_inconsistent_report_is_not_claim_pass(tmp_path):
    from stiracore.tools.run_checks import _Ran, interpret

    out = tmp_path / "out"
    out.mkdir()
    (out / "junit.xml").write_text(
        '<testsuite><testcase name="broken"><failure message="failed"/></testcase></testsuite>'
    )
    row = interpret("pytest", _Ran(0, None, "", 0, 1), out)
    print("production parser:", row)
    env = make_env(tmp_path)
    _results(env, [_result(row)])
    _iterate_turns(env, [_checks(env)], [_claim()])
    print("persistable evidence:", _evidence(env))
    assert _evidence(env).checks != "pass"


def test_external_edit_shown_in_api_and_dom_fixture(tmp_path):
    env = make_env(tmp_path)
    _results(env, [_result()])
    _iterate_turns(env, [_create(env, "a.py", "good\n")], [_checks(env)])
    (env.sub / "a.py").write_text("broken\n")
    _iterate(env, _claim())
    shown = activity.grant_view(env.service.board_view(), env.executor.board(env.grant_id))
    assert shown["claim"]["checks"] != "pass"


def test_unreadable_workspace_never_keeps_previous_pass(tmp_path, monkeypatch):
    env = make_env(tmp_path)
    _results(env, [_result()])
    _iterate_turns(env, [_create(env, "a.py", "good\n")], [_checks(env)])
    monkeypatch.setattr(pg, "read_files", lambda *a, **kw: None)
    _iterate(env, _claim())
    print("unreadable workspace:", _evidence(env))
    assert _evidence(env).checks != "pass"


def test_checks_real_tree_changed_invalidates_pass(tmp_path):
    env = make_env(tmp_path)

    def runner(*args, **kwargs):
        (env.sub / "a.py").write_text("edited after sandbox snapshot\n")
        return {**_result(), "real_tree_changed": True}

    env.executor._run_checks_runner = runner
    _iterate_turns(env, [_create(env, "a.py", "good\n")], [_checks(env)], [_claim()])
    print("real_tree_changed evidence:", _evidence(env))
    assert _evidence(env).checks != "pass"


def test_revoke_after_observation_before_fill_stays_unknown(tmp_path):
    env = make_env(tmp_path)
    _results(env, [_result()])
    original = env.executor._observe

    def observe_then_revoke(it):
        observed = original(it)
        env.service.revoke(env.grant_id)
        return observed

    env.executor._observe = observe_then_revoke
    _iterate_turns(env, [_create(env, "a.py", "good\n")], [_checks(env)], [_claim()])
    print("stop seam:", grant(env).status, env.service.view(env.grant_id)["claim_evidence"])
    assert _evidence(env).checks == "unknown"


def test_external_edit_and_revoke_after_observation_before_fill(tmp_path):
    env = make_env(tmp_path)
    _results(env, [_result()])
    original = env.executor._observe

    def observe_then_revoke(it):
        observed = original(it)
        (env.sub / "a.py").write_text("person changed it after stopping\n")
        env.service.revoke(env.grant_id)
        return observed

    env.executor._observe = observe_then_revoke
    _iterate_turns(env, [_create(env, "a.py", "good\n")], [_checks(env)], [_claim()])
    print("edit + revoke seam:", env.service.view(env.grant_id)["claim_evidence"])
    assert _evidence(env).checks == "unknown"


# --- Opus (review-w6c-opus.md) ------------------------------------------------------------


def _ev(env):
    return grant(env).claim_evidence


def test_p1_concurrent_edit_and_check_race(tmp_path, monkeypatch):
    """editor(create) + run_checks in ONE model turn (strands runs them concurrently);
    the write lands only after the check finished -> the check never saw it."""
    from stiracore.tools import file_editor_tool as fe

    env = make_env(tmp_path)
    done = threading.Event()
    seen = {}

    def runner(arguments, run_policy, host=None):
        seen["file_at_check"] = (env.sub / "a.py").exists()
        done.set()
        return _result(_row(passed=3))

    env.executor._run_checks_runner = runner
    orig = fe.StiraWriteSandbox.write_file

    async def slow(self, path, content, **kw):
        for _ in range(1000):
            if done.is_set():
                break
            await asyncio.sleep(0.005)
        return await orig(self, path, content, **kw)

    monkeypatch.setattr(fe.StiraWriteSandbox, "write_file", slow)
    _iterate_turns(env, [_create(env, "a.py", "x\n"), _checks(env)], [_claim(2)])
    ev = _ev(env)
    print("P1", seen, ev.checks, ev.check_rows, ev.workspace, ev.progress)
    assert (env.sub / "a.py").exists()
    assert seen["file_at_check"] is False
    # BUG if pass: the passing check never saw the claimed file
    assert ev.checks != "pass", f"pass shown though the check ran before the edit: {ev}"


def test_p2_mixed_checks_last_narrow_pass_hides_failure(tmp_path):
    env = make_env(tmp_path)
    _results(
        env,
        [
            _result(_row(status="failed", passed=3, failed=5)),
            _result(_row(kind="compileall", passed=0, failed=0)),
        ],
    )
    _iterate_turns(
        env, [_create(env, "a.py", "x\n")], [_checks(env)], [_checks(env, 2)], [_claim()]
    )
    ev = _ev(env)
    shown = activity.grant_view(env.service.board_view(), env.executor.board(env.grant_id))
    print("P2", ev.checks, ev.check_rows, shown["claim"]["checks"])
    assert ev.checks != "pass", f"a failing pytest on the same files is hidden: {ev}"


def test_p3_external_edit_after_the_check(tmp_path):
    env = make_env(tmp_path)
    _results(env, [_result(_row(passed=3))])
    _iterate_turns(env, [_create(env, "a.py", "x\n")], [_checks(env)])
    (env.sub / "a.py").write_text("broken(\n")  # a person / another process
    _iterate(env, _claim())
    ev = _ev(env)
    print("P3", ev.checks, ev.workspace, ev.progress)
    assert ev.checks != "pass", f"external edit after the check: {ev}"


def test_p4_check_in_an_interrupted_iteration(tmp_path):
    env = make_env(tmp_path)
    _results(env, [_result(_row(passed=3))])

    def on_call(n):
        if n == 3:  # after the check's result, a person's message arrives
            env.executor.preempt_signal.arrive()

    env.provider.on_call = on_call
    view = _iterate_turns(env, [_create(env, "a.py", "x\n")], [_checks(env)], [{"text": "x"}])
    env.provider.on_call = None
    print("P4 end", view["end"])
    _iterate(env, _claim())
    ev = _ev(env)
    print("P4", ev.checks, ev.workspace, ev.progress, ev.iteration)
    # the check's iteration was interrupted (its workspace identity was not read): unknown
    assert ev.checks == "unknown"


def test_p5_check_then_edit_in_interrupted_iteration_then_claim(tmp_path):
    env = make_env(tmp_path)
    _results(env, [_result(_row(passed=3))])

    def on_call(n):
        if n == 4:
            env.executor.preempt_signal.arrive()

    env.provider.on_call = on_call
    view = _iterate_turns(
        env,
        [_create(env, "a.py", "x\n")],
        [_checks(env)],
        [_create(env, "b.py", "y\n", 2)],
        [{"text": "more"}],
    )
    env.provider.on_call = None
    print("P5 end", view["end"], (env.sub / "b.py").exists())
    _iterate(env, _claim())
    ev = _ev(env)
    print("P5", ev.checks, ev.workspace, ev.progress)
    if (env.sub / "b.py").exists():
        assert ev.checks != "pass"


def test_p6_claim_measure_fails_then_board(tmp_path, monkeypatch):
    """The second step never runs (raises): the record stays unknown; board says unknown."""
    env = make_env(tmp_path)
    _results(env, [_result(_row(passed=3))])
    monkeypatch.setattr(
        env.service, "measure_claim", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x"))
    )
    _iterate_turns(env, [_create(env, "a.py", "x\n")], [_checks(env)], [_claim()])
    ev = _ev(env)
    shown = activity.grant_view(env.service.board_view(), env.executor.board(env.grant_id))
    print("P6", ev.checks, ev.workspace, ev.progress, shown["claim"])
    assert ev.checks == "unknown" and shown["claim"]["checks"] == "unknown"


def test_p8_late_measure_after_confirm_rewrites_audit(tmp_path, monkeypatch):
    """The person confirms before the claiming iteration's end measured: audit record later
    changes from what the person saw (unknown) to the measured codes."""
    from grants_helpers import confirm_and_verify

    env = make_env(tmp_path)
    _results(env, [_result(_row(passed=3))])
    orig = env.service.measure_claim
    seen = {}

    def measure(grant_id, claimed_at, **codes):
        seen["before"] = grant(env).claim_evidence.checks
        confirm_and_verify(env.service, env.grant_id, "confirm")
        return orig(grant_id, claimed_at, **codes)

    monkeypatch.setattr(env.service, "measure_claim", measure)
    _iterate_turns(env, [_create(env, "a.py", "x\n")], [_checks(env)], [_claim()])
    g = grant(env)
    print("P8", seen, g.status, g.goal_status, g.claim_evidence.checks, g.generation)
    assert g.goal_status == "complete" and g.status == "ended"
    assert seen["before"] == "unknown" and g.claim_evidence.checks == "unknown"  # as decided on


def test_p1b_natural_order_no_patch(tmp_path):
    env = make_env(tmp_path)
    seen = {}

    def runner(arguments, run_policy, host=None):
        seen["file_at_check"] = (env.sub / "a.py").exists()
        return _result(_row(passed=3))

    env.executor._run_checks_runner = runner
    _iterate_turns(env, [_create(env, "a.py", "x\n"), _checks(env)], [_claim(2)])
    ev = _ev(env)
    print("P1b", seen, ev.checks, ev.workspace)
