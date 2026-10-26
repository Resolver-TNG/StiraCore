"""W6c (#296): the evidence snapshot of a completion claim and its visible waiting state.

The agent only *claims* (``goal_report(claim_complete)`` → goal ``awaiting_verification``);
the person verifies. At the claim the harness stores ONE bounded evidence record on the
grant (the grant store, replaced at the next claim): the iteration k / max, the grant's last
``run_checks`` outcome code with its per-check rows (codes and counts), whether the workspace
changed since the previous claim (the W6a identity — no digest, no path), the claiming
iteration's progress code, tokens used / budget left, TTL left and the claim instant. All of
it is measured by the harness; no model text, no goal. Offline, scripted, no sleeps.
"""

from __future__ import annotations

import json
import logging
from dataclasses import replace

import pytest
from grants_exec_helpers import MARKER, grant, make_env, run_one, use
from grants_helpers import FakeClocks, confirm_and_resume, confirm_and_verify, events, request

from stiracore.grants import progress as pg
from stiracore.grants import schema, state
from stiracore.grants.schema import ClaimEvidence, GrantRefused
from stiracore.grants.service import AutonomySettings, settings_from
from stiracore.observability import activity

ID = "a" * 32


def _create(env, name, text, n=1):
    return use("editor", {"command": "create", "path": str(env.sub / name), "file_text": text}, n)


def _checks(env, n=1):
    return use("run_checks", {"path": str(env.sub)}, n)


def _claim(n=1):
    return use("goal_report", {"kind": "claim_complete", "code": "goal_met"}, n)


def _report(kind, code, n=1):
    return use("goal_report", {"kind": kind, "code": code}, n)


def _row(kind="pytest", status="passed", passed=4, failed=0, **extra):
    return {
        "kind": kind,
        "status": status,
        "reason": None,
        "exit_code": 0 if status == "passed" else 1,
        "duration_ms": 12,
        "counts": {"passed": passed, "failed": failed, "errors": 0, "skipped": 1, **extra},
        "failures": [{"id": "t[/tmp/x]", "message": "boom", "text": "/tmp/x"}],
    }


_SEVERITY = {"passed": 1, "failed": 2, "error": 3, "limited": 4}  # run_checks' order


def _result(*rows, status=None):
    rows = rows or (_row(),)
    worst = max((r["status"] for r in rows), key=lambda s: _SEVERITY.get(s, 0))
    return {"status": status or worst, "reason": None, "checks": list(rows), "output_tail": "x"}


def _results(env, results):
    queue = list(results)

    def runner(arguments, run_policy, host=None):
        env.checks.append((dict(arguments), host))
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    env.executor._run_checks_runner = runner


def _iterate(env, *steps):
    """One iteration whose model answers with ``steps`` in one turn (then nothing)."""
    env.provider.script = [list(steps), [{"text": "ok"}]]
    return run_one(env)


def _iterate_turns(env, *turns):
    """One iteration over several model turns (each a list of tool uses)."""
    env.provider.script = [*(list(t) for t in turns), [{"text": "ok"}]]
    return run_one(env)


def _evidence(env):
    return grant(env).claim_evidence


def _goal_events(env, code):
    return [e for e in events(env.recorder) if e["reason_code"] == code]


# --- the checks code of one run_checks result (pure) ------------------------------------------


def test_claim_checks_pass_fail_not_run_and_unknown():
    assert pg.claim_checks(_result()) == ("pass", (("pytest", "passed", 4, 0, 0, 1, 0),))
    code, rows = pg.claim_checks(_result(_row(), _row("ruff", "failed", 0, 0, findings=3)))
    assert code == "fail" and rows == (
        ("pytest", "passed", 4, 0, 0, 1, 0),
        ("ruff", "failed", 0, 0, 0, 1, 3),
    )
    for status in ("error", "limited"):
        assert pg.claim_checks(_result(_row(status=status)))[0] == "fail"
    assert pg.claim_checks({"status": "refused", "reason": "outside"}) == ("not_run", ())


@pytest.mark.parametrize(
    "result",
    [
        None,
        "passed",
        {"status": "weird"},
        {"status": "passed", "checks": []},  # no check rows: nothing ran that passed
        {"status": "passed"},
        {"status": "error", "reason": "internal_error"},
        _result(_row(status="weird")),
        _result({**_row(), "counts": {"passed": "4"}}),  # a count that is not a number
        _result({**_row(), "counts": {"passed": -1}}),
        _result(*[_row() for _ in range(17)]),  # more rows than kept
        {"status": "passed", "checks": ["not a row"]},
    ],
)
def test_an_unreadable_check_result_is_unknown_never_pass(result):
    assert pg.claim_checks(result) == ("unknown", ())


def test_claim_check_rows_keep_codes_and_clamped_counts_only():
    noisy = _row(kind=MARKER, passed=10**9)
    noisy["reason"] = MARKER
    noisy["failures"] = [{"id": MARKER, "message": MARKER, "text": "/tmp/" + MARKER}]
    code, rows = pg.claim_checks(_result(noisy))
    assert code == "pass" and rows == (("other", "passed", pg.COUNT_CAP, 0, 0, 1, 0),)
    assert MARKER not in repr(rows)


def test_several_calls_combine_fail_closed():
    passed = ("pass", (("pytest", "passed", 4, 0, 0, 0, 0),))
    narrow = ("pass", (("compileall", "passed", 0, 0, 0, 0, 0),))
    failed = ("fail", (("pytest", "failed", 3, 2, 0, 0, 0),))
    unknown = ("unknown", ())
    refused = ("not_run", ())
    assert pg.combine_checks(None, refused) == ("not_run", ())
    assert pg.combine_checks(failed, narrow)[0] == "fail"  # a narrow pass never hides it
    assert pg.combine_checks(narrow, failed)[0] == "fail"
    assert pg.combine_checks(passed, narrow) == ("pass", tuple(sorted(passed[1] + narrow[1])))
    assert pg.combine_checks(passed, unknown) == ("unknown", ())
    assert pg.combine_checks(unknown, passed) == ("unknown", ())
    assert pg.combine_checks(failed, unknown)[0] == "fail"
    assert pg.combine_checks(passed, refused) == passed  # a refusal adds nothing
    many = ("pass", tuple(("pytest", "passed", n, 0, 0, 0, 0) for n in range(16)))
    assert pg.combine_checks(many, narrow) == ("unknown", ())  # more rows than kept
    code, rows = pg.combine_checks(("fail", many[1][:15] + failed[1]), narrow)
    assert code == "fail" and len(rows) == 16 and failed[1][0] in rows


@pytest.mark.parametrize(
    "result",
    [
        _result({**_row(), "consistent": False}),  # exit code and report disagree
        _result(_row(failed=1)),  # a pass that reports a failure
        _result({**_row(), "counts": None}),  # a pass without counts
        _result({**_row(), "counts": "4"}),
        _result(_row(), status="error"),  # the overall status is not the rows' worst
        _result(_row(status="failed"), status="passed"),
        {**_result(), "real_tree_changed": True},  # the tree changed during the run
    ],
)
def test_an_inconsistent_result_is_unknown_never_pass(result):
    assert pg.claim_checks(result) == ("unknown", ())


def test_a_limit_without_counts_is_still_a_fail():
    limited = {**_row(status="limited"), "counts": None}
    assert pg.claim_checks(_result(limited)) == ("fail", (("pytest", "limited", 0, 0, 0, 0, 0),))


def test_the_check_kinds_are_run_checks_kinds():
    from stiracore.tools import run_checks

    assert tuple(schema.CHECK_KINDS) == tuple(run_checks.KINDS)
    assert set(schema.CHECK_STATUSES) == set(run_checks._ORDER)


# --- the state machine (pure) ----------------------------------------------------------------


def fresh(clocks=None, **overrides):
    clocks = clocks or FakeClocks()
    return state.new_grant(request(**overrides), grant_id=ID, issuer="dashboard", now=clocks())


def test_a_claim_takes_its_evidence_at_the_claim_instant():
    clocks = FakeClocks()
    g = replace(fresh(clocks, total_tokens=1000), used_tokens=300, iterations=2)
    clocks.tick(600)
    claimed = state.claim(g, now=clocks())
    ev = claimed.claim_evidence
    assert (claimed.goal_status, claimed.status) == ("awaiting_verification", "active")
    assert ev == ClaimEvidence(
        claimed_at=clocks.wall,
        iteration=3,
        max_iterations=10,
        used_tokens=300,
        budget_left=700,
        ttl_left_sec=7200 - 600,
    )
    assert (ev.checks, ev.workspace, ev.progress, ev.check_rows) == (
        "unknown",
        "unknown",
        "unknown",
        (),
    )


def test_a_claim_without_a_clock_reading_is_refused_and_changes_nothing():
    g = fresh()
    with pytest.raises(GrantRefused, match="grant_clock_unavailable"):
        state.claim(g, now=None)
    assert g.goal_status == "active" and g.claim_evidence is None


def test_the_next_claim_replaces_the_record_never_appends():
    clocks = FakeClocks()
    g = state.claim(fresh(clocks), now=clocks())
    first = g.claim_evidence
    g = state.measure_claim(
        g, first.claimed_at, checks="fail", rows=(), workspace="changed", progress="new"
    )
    g = state.verify(g, "reject", wall=clocks.wall)
    assert g.goal_status == "active" and g.claim_evidence.workspace == "changed"  # kept (audit)
    clocks.tick(30)
    g = state.claim(g, now=clocks())
    assert g.claim_evidence.claimed_at == first.claimed_at + 30
    assert g.claim_evidence.workspace == "unknown"  # a fresh record: nothing of the first


def test_measure_claim_fills_only_its_own_claim_and_is_no_state_change():
    clocks = FakeClocks()
    g = state.claim(fresh(clocks), now=clocks())
    at = g.claim_evidence.claimed_at
    rows = (("pytest", "passed", 4, 0, 0, 0, 0),)
    assert (
        state.measure_claim(
            g, at + 1.0, checks="pass", rows=rows, workspace="changed", progress="new"
        )
        is g
    )
    assert (
        state.measure_claim(
            fresh(), at, checks="pass", workspace="changed", progress="new"
        ).claim_evidence
        is None
    )
    m = state.measure_claim(g, at, checks="pass", rows=rows, workspace="changed", progress="new")
    assert (m.claim_evidence.checks, m.claim_evidence.check_rows) == ("pass", rows)
    assert m.generation == g.generation and state.transitions(g, m) == []
    assert (m.status, m.goal_status) == (g.status, g.goal_status)


@pytest.mark.parametrize(
    ("checks", "rows", "expected"),
    [
        ("pass", (("pytest", "failed", 1, 1, 0, 0, 0),), "unknown"),  # a pass that did not pass
        ("fail", (("pytest", "passed", 1, 0, 0, 0, 0),), "unknown"),  # a fail that passed
        ("pass", (), "unknown"),  # no rows
        ("pass", (("pytest", MARKER, 1, 0, 0, 0, 0),), "unknown"),  # a text in a row
        ("pass", (("/etc/" + MARKER, "passed", 1, 0, 0, 0, 0),), "unknown"),
        ("great", (), "unknown"),
        ("not_run", (("pytest", "passed", 1, 0, 0, 0, 0),), "not_run"),  # rows dropped
    ],
)
def test_measure_claim_never_stores_an_inconsistent_or_unknown_code(checks, rows, expected):
    clocks = FakeClocks()
    g = state.claim(fresh(clocks), now=clocks())
    m = state.measure_claim(
        g, g.claim_evidence.claimed_at, checks=checks, rows=rows, workspace="x", progress=MARKER
    )
    ev = m.claim_evidence
    assert (ev.checks, ev.workspace, ev.progress) == (expected, "unknown", "unknown")
    assert ev.check_rows == () and schema.evidence_consistent(ev)


def test_confirm_keeps_the_record_and_a_terminal_grant_is_never_revived():
    clocks = FakeClocks()
    g = state.claim(fresh(clocks), now=clocks())
    at = g.claim_evidence.claimed_at
    done = state.verify(g, "confirm", wall=clocks.wall)
    assert (done.status, done.goal_status) == ("ended", "complete")
    assert done.claim_evidence == g.claim_evidence  # the audit trail
    late = state.measure_claim(done, at, checks="not_run", workspace="unchanged", progress="none")
    assert (late.status, late.goal_status, late.generation) == (
        "ended",
        "complete",
        done.generation,
    )
    assert late.claim_evidence.progress == "none"
    expired = state.expire(state.claim(fresh(clocks), now=clocks()), wall=clocks.wall)
    recorded = state.verify(expired, "confirm", wall=clocks.wall)
    assert (recorded.status, recorded.goal_status, recorded.late_verification) == (
        "expired",
        "unmet",
        "pass",
    )
    assert recorded.claim_evidence is not None
    with pytest.raises(GrantRefused):
        state.claim(recorded, now=clocks())  # a new claim never reopens it


# --- the stored form ------------------------------------------------------------------------


def _measured():
    clocks = FakeClocks()
    g = state.claim(fresh(clocks), now=clocks())
    rows = (("pytest", "passed", 4, 0, 0, 1, 0), ("ruff", "failed", 0, 0, 0, 0, 3))
    return state.measure_claim(
        g,
        g.claim_evidence.claimed_at,
        checks="fail",
        rows=rows,
        workspace="changed",
        progress="new",
    )


def test_the_record_round_trips_and_an_older_record_without_it_loads():
    def read(data):
        try:
            return schema.grant_from_dict(data)
        except GrantRefused as refused:
            return refused.code

    g = _measured()
    stored = schema.grant_to_dict(g, "the goal")
    assert read(json.loads(json.dumps(stored))) == (g, "the goal")
    older = {k: v for k, v in stored.items() if k != "claim_evidence"}  # written before W6c
    loaded = read(older)
    assert loaded != "grant_body_invalid" and loaded[0].claim_evidence is None


@pytest.mark.parametrize(
    "mutate",
    [
        lambda ev: ev.update(note=MARKER),
        lambda ev: ev.pop("progress"),
        lambda ev: ev.update(checks=MARKER),
        lambda ev: ev.update(workspace="maybe"),
        lambda ev: ev.update(checks="pass"),  # pass with a failed row
        lambda ev: ev["check_rows"][0].update(kind=MARKER),
        lambda ev: ev["check_rows"][0].update(path="/tmp"),
        lambda ev: ev["check_rows"][0]["counts"].update(passed="4"),
        lambda ev: ev.update(iteration=-1),
        lambda ev: ev.update(claimed_at=float("nan")),
        lambda ev: ev.update(check_rows=ev["check_rows"] * 9),
    ],
)
def test_the_reader_refuses_anything_outside_the_record(mutate):
    stored = json.loads(json.dumps(schema.grant_to_dict(_measured(), "goal")))
    mutate(stored["claim_evidence"])
    with pytest.raises(GrantRefused):
        schema.grant_from_dict(stored)


def test_the_store_keeps_the_record_with_the_grant(tmp_path):
    from stiracore.grants.store import GrantStore

    g = state.restart(_measured(), wall=1.0)
    store = GrantStore(tmp_path)
    assert store.write([{"grant": schema.grant_to_dict(g, "goal"), "restart_from": "active"}])
    loaded = GrantStore(tmp_path).load()
    assert loaded.reason is None and loaded.entries[0][0].claim_evidence == g.claim_evidence


def test_the_measured_codes_are_written_to_the_grant_store(tmp_path):
    from grants_helpers import settle

    env = make_env(tmp_path)
    view = env.service.claim_complete(env.grant_id)
    at = view["claim_evidence"]["claimed_at"]
    assert settle(env.service) == "stopped"  # the claim's own write is on disk

    def stored():
        records = json.loads((tmp_path / "data" / "grants.json").read_text())
        return records[-1]["grants"][0]["grant"]["claim_evidence"]

    assert stored()["checks"] == "unknown"
    rows = (("pytest", "passed", 4, 0, 0, 0, 0),)
    generation = grant(env).generation
    from stiracore.grants.executor import ClaimFence

    fence = ClaimFence(
        generation,
        env.authority.epoch,
        env.executor.preempt_signal,
        env.executor.preempt_signal.seq,
    )
    env.service.measure_claim(
        env.grant_id,
        at,
        fence=fence,
        checks="pass",
        rows=rows,
        workspace="changed",
        progress="new",
    )
    assert grant(env).generation == generation  # not a state change
    assert settle(env.service) == "stopped"
    assert (stored()["checks"], stored()["workspace"], stored()["progress"]) == (
        "pass",
        "changed",
        "new",
    )
    assert len(_goal_events(env, "goal_claimed")) == 1


@pytest.mark.parametrize("change", ["none", "generation", "epoch", "kill", "turn", "arriving"])
def test_the_publication_is_fenced_at_the_publication_itself(tmp_path, change):
    from stiracore.grants.executor import ClaimFence

    env = make_env(tmp_path)
    at = env.service.claim_complete(env.grant_id)["claim_evidence"]["claimed_at"]
    signal = env.executor.preempt_signal
    fence = ClaimFence(grant(env).generation, env.authority.epoch, signal, signal.seq)
    if change == "generation":
        fence = ClaimFence(fence.generation - 1, fence.epoch, signal, fence.seq)  # another version
    elif change == "epoch":
        env.authority.narrow("policy_changed")
    elif change == "kill":
        env.authority.kill()
    elif change == "turn":
        signal.arrive()
    elif change == "arriving":
        signal._pending += 1  # a turn between its mark and its lock attempt
    rows = (("pytest", "passed", 4, 0, 0, 0, 0),)
    env.service.measure_claim(
        env.grant_id, at, fence=fence, checks="pass", rows=rows, workspace="changed", progress="new"
    )
    expected = "pass" if change == "none" else "unknown"
    assert grant(env).claim_evidence.checks == expected


# --- the executor: measured at the claiming iteration's end -------------------------------------


def test_a_claim_on_passing_checks_after_the_edit(tmp_path):
    env = make_env(tmp_path)
    _results(env, [_result(_row(passed=7))])
    at = env.service.clock_reading()
    view = _iterate_turns(env, [_create(env, "a.py", "x\n")], [_checks(env)], [_claim()])
    assert view["end"] == "claimed"
    g = grant(env)
    assert (g.goal_status, g.status, g.iterations) == ("awaiting_verification", "active", 1)
    ev = g.claim_evidence
    assert ev.claimed_at == at.wall and ev.iteration == 1 and ev.max_iterations == 10
    assert (ev.checks, ev.check_rows) == ("pass", (("pytest", "passed", 7, 0, 0, 1, 0),))
    assert (ev.workspace, ev.progress) == ("changed", "new")
    assert ev.used_tokens > 0 and ev.used_tokens + ev.budget_left == 10_000
    assert ev.ttl_left_sec == 7200


def test_a_claim_on_failing_checks_is_shown_amber(tmp_path):
    env = make_env(tmp_path)
    _results(env, [_result(_row(status="failed", passed=3, failed=2))])
    _iterate_turns(env, [_create(env, "a.py", "x\n")], [_checks(env)], [_claim()])
    ev = _evidence(env)
    assert ev.checks == "fail" and ev.check_rows == (("pytest", "failed", 3, 2, 0, 1, 0),)
    shown = activity.grant_view(env.service.board_view(), env.executor.board(env.grant_id))
    assert shown["claim"]["checks"] == "fail"
    assert shown["claim"]["check_rows"] == [
        {
            "kind": "pytest",
            "status": "failed",
            "counts": {"passed": 3, "failed": 2, "errors": 0, "skipped": 1, "findings": 0},
        }
    ]


def test_an_edit_after_the_last_check_makes_it_not_run(tmp_path):
    env = make_env(tmp_path)
    _results(env, [_result()])
    _iterate_turns(env, [_checks(env)], [_create(env, "a.py", "x\n")], [_claim()])
    assert (_evidence(env).checks, _evidence(env).check_rows) == ("not_run", ())


def test_the_last_check_of_the_grant_counts_across_iterations(tmp_path):
    env = make_env(tmp_path)
    _results(env, [_result(_row(status="failed", failed=1)), _result()])
    _iterate_turns(env, [_create(env, "a.py", "x\n")], [_checks(env)])
    _iterate_turns(env, [_create(env, "b.py", "y\n", 2)], [_checks(env, 2)])
    _iterate(env, _claim())  # the claiming iteration itself ran nothing
    ev = _evidence(env)
    assert (ev.checks, ev.workspace, ev.progress, ev.iteration) == ("pass", "changed", "none", 3)
    (tmp_path / "two").mkdir()
    env2 = make_env(tmp_path / "two")
    _results(env2, [_result()])
    _iterate_turns(env2, [_checks(env2)])
    _iterate(env2, _create(env2, "a.py", "x\n"))  # a later iteration wrote: stale
    _iterate(env2, _claim())
    assert _evidence(env2).checks == "not_run"


def test_checks_combine_across_iterations_on_the_same_files_and_restart_on_others(tmp_path):
    env = make_env(tmp_path)
    narrow = _result(_row(kind="compileall", passed=0))
    _results(env, [_result(_row(status="failed", failed=2)), narrow, _result()])
    _iterate_turns(env, [_create(env, "a.py", "x\n")], [_checks(env)])
    _iterate(env, _checks(env, 2))  # the same files, a narrow pass: the failure stays
    _iterate(env, _claim())
    assert _evidence(env).checks == "fail"
    confirm_and_verify(env.service, env.grant_id, "reject")
    (env.sub / "a.py").write_text("fixed by a person\n")  # another workspace
    _iterate_turns(env, [_checks(env, 3)], [_claim(2)])
    ev = _evidence(env)
    assert (ev.checks, ev.check_rows) == ("pass", (("pytest", "passed", 4, 0, 0, 1, 0),))


def test_a_later_write_that_leaves_the_same_bytes_still_makes_the_check_stale(tmp_path):
    env = make_env(tmp_path)
    _results(env, [_result()])
    _iterate_turns(env, [_create(env, "a.py", "x\n")], [_checks(env)])
    path = str(env.sub / "a.py")
    there = {"command": "str_replace", "path": path, "old_str": "x", "new_str": "y"}
    back = {"command": "str_replace", "path": path, "old_str": "y", "new_str": "x"}
    _iterate_turns(env, [use("editor", there, 2)], [use("editor", back, 3)])
    assert (env.sub / "a.py").read_text() == "x\n"  # the same bytes as at the check
    _iterate(env, _claim())
    assert _evidence(env).checks == "not_run"


def test_a_runner_that_raises_or_returns_garbage_is_unknown_never_pass(tmp_path):
    env = make_env(tmp_path)
    _results(env, [_result(), RuntimeError("boom")])
    _iterate(env, _checks(env))
    _iterate_turns(env, [_checks(env, 2)], [_claim()])
    assert _evidence(env).checks == "unknown"
    (tmp_path / "two").mkdir()
    env2 = make_env(tmp_path / "two")
    _results(env2, [{"status": "passed", "checks": "lots"}])
    _iterate_turns(env2, [_checks(env2)], [_claim()])
    assert _evidence(env2).checks == "unknown"


def test_an_immediate_claim_with_no_other_effect(tmp_path):
    env = make_env(tmp_path)
    view = _iterate(env, _claim())
    assert view["end"] == "claimed" and view["tool_calls"] == 0
    ev = _evidence(env)
    assert (ev.checks, ev.workspace, ev.progress, ev.iteration) == (
        "not_run",
        "unchanged",
        "none",
        1,
    )
    assert len(_goal_events(env, "goal_claimed")) == 1
    assert grant(env).goal_status == "awaiting_verification"


def test_two_claims_in_one_turn_are_one_claim(tmp_path):
    env = make_env(tmp_path)
    view = _iterate(env, _claim(1), _claim(2))
    assert view["end"] == "claimed" and view["reports"] == 1
    assert len(_goal_events(env, "goal_claimed")) == 1
    assert grant(env).iterations == 1 and _evidence(env).iteration == 1


def test_a_claim_after_a_blocked_report_is_not_a_claim(tmp_path):
    env = make_env(tmp_path)
    view = _iterate(env, _report("blocked", "needs_decision"), _claim(2))
    assert view["end"] == "reported_blocked"
    g = grant(env)
    assert (g.goal_status, g.goal_reason) == ("blocked", "needs_decision")
    assert g.claim_evidence is None and not _goal_events(env, "goal_claimed")


def test_a_claim_while_a_persons_turn_runs_is_refused(tmp_path):
    env = make_env(tmp_path)
    env.provider.on_call = lambda n: setattr(env, "busy", True)
    view = _iterate(env, _claim())
    assert view["end"] == "yielded_user_turn"
    assert grant(env).goal_status == "active" and grant(env).claim_evidence is None


def test_a_persons_turn_after_the_claim_leaves_the_measured_codes_unknown(tmp_path):
    env = make_env(tmp_path)
    claim_complete = env.service.claim_complete

    def claim_then_turn(grant_id):
        out = claim_complete(grant_id)
        env.executor.preempt_signal.arrive()  # a person's message right after the claim
        return out

    env.service.claim_complete = claim_then_turn
    _results(env, [_result()])
    _iterate_turns(env, [_create(env, "a.py", "x\n")], [_checks(env)], [_claim()])
    g = grant(env)
    assert g.goal_status == "awaiting_verification" and g.iterations == 1
    ev = g.claim_evidence
    # not committed (an interruption): nothing measured is published
    assert (ev.checks, ev.workspace, ev.progress) == ("unknown", "unknown", "unknown")
    assert len(_goal_events(env, "goal_claimed")) == 1


def test_a_persons_turn_after_the_reads_publishes_nothing(tmp_path):
    env = make_env(tmp_path)
    observe = env.executor._observe

    def observe_then_turn(it):
        observed = observe(it)  # the identity is read ...
        env.executor.preempt_signal.arrive()  # ... then a person's message arrives
        return observed

    env.executor._observe = observe_then_turn
    _results(env, [_result()])
    _iterate_turns(env, [_create(env, "a.py", "x\n")], [_checks(env)], [_claim()])
    g = grant(env)
    assert g.goal_status == "awaiting_verification" and g.status == "active"
    ev = g.claim_evidence
    assert (ev.checks, ev.workspace, ev.progress) == ("unknown", "unknown", "unknown")


def test_a_marker_in_every_report_field_is_refused_and_stored_nowhere(tmp_path):
    env = make_env(tmp_path)
    hostile = [
        use("goal_report", {"kind": MARKER, "code": "goal_met"}, 1),
        use("goal_report", {"kind": "claim_complete", "code": MARKER}, 2),
        use("goal_report", {"kind": "claim_complete", "code": "goal_met", "note": MARKER}, 3),
        use("goal_report", MARKER, 4),
    ]
    view = _iterate(env, *hostile)
    assert view["reports"] == 0 and view["end"] != "claimed"
    assert grant(env).goal_status == "active"
    assert grant(env).claim_evidence is None and not _goal_events(env, "goal_claimed")
    assert MARKER not in json.dumps(schema.grant_to_dict(grant(env), "x"))


def test_a_reject_then_a_new_claim_replaces_the_record_and_compares_with_the_last_claim(tmp_path):
    env = make_env(tmp_path)
    _iterate_turns(env, [_create(env, "a.py", "x\n")], [_claim()])
    first = _evidence(env)
    assert first.workspace == "changed"
    confirm_and_verify(env.service, env.grant_id, "reject")
    g = grant(env)
    assert (g.goal_status, g.no_progress) == ("active", 1)  # W6a: a reject is no progress
    env.clocks.tick(5)
    _iterate(env, _claim())  # nothing changed since the last claim
    second = _evidence(env)
    assert second.claimed_at == first.claimed_at + 5 and second.workspace == "unchanged"
    assert second.iteration == 2 and second.progress == "none"
    confirm_and_verify(env.service, env.grant_id, "reject")
    g = grant(env)
    assert (g.goal_status, g.goal_reason) == ("blocked", "no_progress")  # 3 without progress
    confirm_and_resume(env.service, env.grant_id, {})
    _iterate_turns(env, [_create(env, "b.py", "y\n", 2)], [_claim()])
    assert _evidence(env).workspace == "changed"
    assert len(_goal_events(env, "goal_claimed")) == 3


def test_a_confirmed_claim_keeps_its_record_as_the_audit_trail(tmp_path):
    env = make_env(tmp_path)
    _iterate(env, _claim())
    before = _evidence(env)
    confirm_and_verify(env.service, env.grant_id, "confirm")
    g = grant(env)
    assert (g.status, g.goal_status) == ("ended", "complete") and g.claim_evidence == before
    shown = activity.grant_view(env.service.board_view())
    assert shown["claim"]["checks"] == "not_run" and "waiting_sec" not in shown["claim"]
    assert shown["claim"]["stale"] is False


def _unread_claims(env):
    env.recorder.drain()
    return [
        n
        for n in env.recorder.outbox(unread_only=True)
        if n["event"]["reason_code"] == "goal_claimed"
    ]


@pytest.mark.parametrize("decision", ["confirm", "reject"])
def test_the_persons_verification_acknowledges_the_claims_notice(tmp_path, decision):
    env = make_env(tmp_path)
    _iterate(env, _claim())
    assert len(_unread_claims(env)) == 1
    confirm_and_verify(env.service, env.grant_id, decision)
    assert _unread_claims(env) == []
    if decision == "reject":  # the next claim is unread again, and its verify acks it
        _iterate(env, _claim(2))
        notices = _unread_claims(env)
        assert len(notices) == 1 and notices[0]["count"] == 2
        confirm_and_verify(env.service, env.grant_id, "confirm")
        assert _unread_claims(env) == []


def test_a_verification_never_acknowledges_a_newer_claim(tmp_path):
    env = make_env(tmp_path)
    _iterate(env, _claim())
    first = env.service._claim_events[env.grant_id]
    confirm_and_verify(env.service, env.grant_id, "reject")
    _iterate(env, _claim(2))
    env.service._claim_events[env.grant_id] = first  # a stale id: the first claim's
    confirm_and_verify(env.service, env.grant_id, "confirm")
    assert len(_unread_claims(env)) == 1  # the newer occurrence stays unread


def test_a_claim_posted_right_after_the_reject_keeps_its_notice(tmp_path):
    """A new claim lands between the reject's commit and its acknowledgement: the reject
    acknowledges the claim it verified (read in its own locked step), never the new one."""
    env = make_env(tmp_path)
    _iterate(env, _claim())
    commit = env.service._commit
    done = {}

    def commit_then_claim(changes, **kwargs):
        target = commit(changes, **kwargs)
        rejected = any(new.goal_status == "active" for _old, new in changes)
        if rejected and not done:
            done["new"] = True
            env.clocks.tick(1)
            _iterate(env, _claim(2))  # a real next iteration claims at once
        return target

    env.service._commit = commit_then_claim
    confirm_and_verify(env.service, env.grant_id, "reject")
    assert done and grant(env).goal_status == "awaiting_verification"
    notices = _unread_claims(env)
    assert len(notices) == 1 and notices[0]["count"] == 2  # the new claim waits, unread


def test_the_claim_emits_one_event_and_one_stop_notice_until_acknowledged(tmp_path):
    env = make_env(tmp_path)
    _iterate(env, _claim())
    env.recorder.drain()
    claimed = _goal_events(env, "goal_claimed")
    assert len(claimed) == 1
    assert (claimed[0]["subject_kind"], claimed[0]["subject_id"]) == ("goal", env.grant_id)
    assert (claimed[0]["from"], claimed[0]["to"]) == ("active", "awaiting_verification")
    notices = [n for n in env.recorder.outbox() if n["event"]["reason_code"] == "goal_claimed"]
    assert len(notices) == 1 and notices[0]["unread"] and notices[0]["count"] == 1
    # the measured codes (a second write of the record) add no event and no notice
    assert [e["reason_code"] for e in events(env.recorder)].count("goal_claimed") == 1
    env.service.view(env.grant_id)  # a look does not repeat it either
    env.recorder.drain()
    assert (
        len([n for n in env.recorder.outbox() if n["event"]["reason_code"] == "goal_claimed"]) == 1
    )
    newest = notices[0]["event_ids"][-1]
    assert env.recorder.ack(newest)["acked"] is True
    assert not [
        n
        for n in env.recorder.outbox(unread_only=True)
        if n["event"]["reason_code"] == "goal_claimed"
    ]
    # a new claim after a reject is a new event: the stop notice is shown (unread) again
    confirm_and_verify(env.service, env.grant_id, "reject")
    env.recorder.drain()
    assert not [
        n
        for n in env.recorder.outbox(unread_only=True)
        if n["event"]["reason_code"] == "goal_claimed"
    ]
    _iterate(env, _claim())
    env.recorder.drain()
    again = [
        n
        for n in env.recorder.outbox(unread_only=True)
        if n["event"]["reason_code"] == "goal_claimed"
    ]
    assert len(again) == 1 and again[0]["count"] == 2 and again[0]["event_ids"][-1] != newest


# --- the waiting state: since, TTL, "still waiting for you" ------------------------------------


def test_the_view_says_how_long_the_claim_has_waited_and_when_it_is_stale(tmp_path):
    env = make_env(tmp_path)
    _iterate(env, _claim())
    ev = env.service.view(env.grant_id)["claim_evidence"]
    assert ev["stale_after_sec"] == 1800 and ev["waiting_sec"] == 0 and ev["stale"] is False
    env.clocks.tick(1799)
    ev = env.service.view(env.grant_id)["claim_evidence"]
    assert (ev["waiting_sec"], ev["stale"]) == (1799, False)
    env.clocks.tick(1)
    view = env.service.view(env.grant_id)
    assert (view["claim_evidence"]["waiting_sec"], view["claim_evidence"]["stale"]) == (1800, True)
    assert view["ttl_left_sec"] == 7200 - 1800 and view["goal_state"] == "awaiting_verification"
    shown = activity.grant_view(env.service.board_view())
    assert shown["claim"]["stale"] is True and shown["claim"]["waiting_sec"] == 1800
    # display only: nothing acted (no model call, no transition, no event)
    assert env.provider.calls == 1 and len(_goal_events(env, "goal_claimed")) == 1
    assert grant(env).goal_status == "awaiting_verification"


def test_the_stale_threshold_is_a_bounded_setting():
    assert settings_from({"stage_c": True}).claim_stale_sec == 1800
    assert settings_from({"stage_c": True, "claim_stale_sec": 600}).claim_stale_sec == 600
    for bad in (59, 86401, "600", True, 600.0):
        parsed = settings_from({"stage_c": True, "claim_stale_sec": bad})
        assert parsed.claim_stale_sec == 1800 and "claim_stale_sec_invalid" in parsed.warnings
    assert AutonomySettings().claim_stale_sec == 1800


def test_a_custom_threshold_reaches_the_view(tmp_path):
    env = make_env(tmp_path)
    env.service.settings = replace(env.service.settings, claim_stale_sec=60)
    _iterate(env, _claim())
    env.clocks.tick(60)
    ev = env.service.view(env.grant_id)["claim_evidence"]
    assert (ev["stale_after_sec"], ev["stale"]) == (60, True)


def test_no_waiting_numbers_once_the_grant_ended(tmp_path):
    env = make_env(tmp_path)
    _iterate(env, _claim())
    env.service.revoke(env.grant_id)
    ev = env.service.view(env.grant_id)["claim_evidence"]
    assert "waiting_sec" not in ev and "stale" not in ev
    assert activity.grant_view(env.service.board_view()).get("claim") is None  # unmet: hidden


# --- the board's allow-list --------------------------------------------------------------------


def _view(**claim):
    base = {
        "claimed_at": 1_800_000_000.0,
        "iteration": 2,
        "max_iterations": 10,
        "used_tokens": 100,
        "budget_left": 900,
        "ttl_left_sec": 3600,
        "checks": "pass",
        "check_rows": [
            {
                "kind": "pytest",
                "status": "passed",
                "counts": {"passed": 1, "failed": 0, "errors": 0, "skipped": 0, "findings": 0},
            }
        ],
        "workspace": "changed",
        "progress": "new",
        "waiting_sec": 10,
        "stale_after_sec": 1800,
        "stale": False,
    }
    base.update(claim)
    return {
        "short": "abcdef01",
        "state": "active",
        "goal_state": "awaiting_verification",
        "issued_wall": 1.0,
        "claim_evidence": base,
    }


def test_the_board_block_is_an_allow_list_of_codes():
    shown = activity.grant_view(_view())["claim"]
    assert shown["checks"] == "pass" and shown["workspace"] == "changed"
    for bad in (
        {"checks": MARKER},
        {"checks": "pass", "check_rows": []},
        {"checks": "pass", "check_rows": [{"kind": "pytest", "status": "failed", "counts": {}}]},
        {"checks": "pass", "check_rows": "lots"},
        {
            "checks": "pass",
            "check_rows": [{**_view()["claim_evidence"]["check_rows"][0], "status": "failed"}],
        },
        {"checks": "fail", "check_rows": _view()["claim_evidence"]["check_rows"]},
        {
            "checks": "pass",
            "check_rows": [
                {
                    "kind": "pytest",
                    "status": "passed",
                    "counts": {
                        "passed": "1",
                        "failed": 0,
                        "errors": 0,
                        "skipped": 0,
                        "findings": 0,
                    },
                }
            ],
        },
    ):
        claim = activity.grant_view(_view(**bad))["claim"]
        assert (claim["checks"], claim["check_rows"]) == ("unknown", []), bad
    odd = activity.grant_view(
        _view(workspace=MARKER, progress=MARKER, note=MARKER, iteration=MARKER, stale="yes")
    )
    assert (odd["claim"]["workspace"], odd["claim"]["progress"]) == ("unknown", "unknown")
    assert "iteration" not in odd["claim"] and odd["claim"]["stale"] is False
    assert MARKER not in json.dumps(odd)
    row = _view()["claim_evidence"]["check_rows"][0]
    renamed = activity.grant_view(_view(check_rows=[{**row, "kind": MARKER, "path": MARKER}]))
    assert renamed["claim"]["check_rows"][0]["kind"] == "other" and MARKER not in json.dumps(
        renamed
    )


def test_the_board_block_is_absent_unless_the_claim_waits_or_was_verified():
    for goal in ("active", "blocked", "paused", "unmet", "budget_limited", MARKER):
        view = {**_view(), "goal_state": goal}
        assert "claim" not in activity.grant_view(view), goal
    assert "claim" in activity.grant_view({**_view(), "goal_state": "complete", "state": "ended"})
    assert "claim" not in activity.grant_view({**_view(), "claim_evidence": None})


# --- nothing of the model's or the goal's text anywhere ---------------------------------------


def test_no_model_text_goal_or_path_in_any_store_the_claim_touches(tmp_path, caplog):
    from stiracore.authority import AuthorityService
    from stiracore.authority.outbox import LifecycleRecorder

    caplog.set_level(logging.DEBUG)
    data = tmp_path / "data"
    data.mkdir()
    authority = AuthorityService(None)
    recorder = LifecycleRecorder(data, on_failure=authority.report_unhealthy)
    env = make_env(tmp_path, authority=authority, recorder=recorder)
    name = f"{MARKER}.py"
    noisy = _result(_row(kind=MARKER, status="failed", failed=1))
    noisy["reason"] = MARKER
    noisy["checks"][0]["failures"] = [{"id": MARKER, "message": MARKER, "text": MARKER}]
    _results(env, [noisy])
    env.provider.script = [
        [{"text": f"I am done {MARKER}"}, _create(env, name, f"{MARKER}\n")],
        [{"text": MARKER}, _checks(env)],
        [{"text": f"claiming {MARKER}"}, _claim()],
        [{"text": MARKER}],
    ]
    assert run_one(env)["end"] == "claimed"
    ev = _evidence(env)
    assert ev.checks == "fail" and ev.check_rows[0][0] == "other"
    board = env.executor.board(env.grant_id)
    shown = activity.grant_view(env.service.board_view(), board)
    listed = env.service.list_view()
    one = env.service.view(env.grant_id)
    recorder.drain()
    assert env.service.stop(5) == "stopped"
    for text in (str(shown), json.dumps(listed), json.dumps(one), str(board), caplog.text):
        assert MARKER not in text
        assert str(env.sub) not in text
    stored = (data / "grants.json").read_text()
    assert '"claim_evidence"' in stored and '"checks": "fail"' in stored.replace('":"', '": "')
    scanned = 0
    for path in tmp_path.rglob("*"):
        if path.is_file() and env.sub not in path.parents:
            raw = path.read_bytes()
            # the goal (with the marker) is stored in grants.json only, as its "goal" field
            if path.name == "grants.json":
                records = json.loads(raw)
                for record in records:
                    for entry in record["grants"]:
                        g = dict(entry["grant"])
                        g.pop("goal")
                        assert MARKER not in json.dumps(g)
            else:
                assert MARKER.encode() not in raw, path
            scanned += 1
    assert scanned >= 3  # the grant store, the lifecycle log, the outbox
