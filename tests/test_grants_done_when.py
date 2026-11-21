"""#683: ``done_when`` — the completion checks a person declares at issue, run by the harness
itself at a claim (verifier ``checks`` → ``user``). Offline: a scripted model and a fake
run_checks runner; no network, no real checks, no wall clock.
"""

from __future__ import annotations

import pytest
from grants_exec_helpers import grant, make_env, run_one, use
from grants_helpers import issue_body, make_service

from stiracore.grants import progress as pg
from stiracore.grants import round_ledger as rl
from stiracore.grants import schema
from stiracore.grants.schema import GrantRefused, parse_issue
from stiracore.grants.service import GrantConfirmation

DONE = {"verifier": "checks", "done_when": ["pytest", "ruff"]}


def _row(kind, status="passed"):
    ok = status == "passed"
    return {
        "kind": kind,
        "status": status,
        "reason": None,
        "counts": {"passed": 3 if ok else 0, "failed": 0 if ok else 1},
        "failures": [],
    }


def _result(*rows):
    order = {"passed": 1, "failed": 2, "error": 3, "limited": 4}
    worst = max((r["status"] for r in rows), key=order.__getitem__)
    return {"status": worst, "reason": None, "checks": list(rows)}


def _runner(env, *results):
    queue = list(results)

    def runner(arguments, run_policy, host=None):
        env.checks.append((dict(arguments), host))
        return queue.pop(0)

    env.executor._run_checks_runner = runner


def _claim(n=1):
    return use("goal_report", {"kind": "claim_complete", "code": "goal_met"}, n)


def _tool_texts(env):
    """Every tool result text the model was shown (its last request)."""
    out = []
    for message in env.provider.seen[-1]:
        for block in message.get("content", []):
            result = block.get("toolResult")
            if result:
                out.extend(c.get("text", "") for c in result.get("content", []))
    return out


# --- the issue request -------------------------------------------------------------------


def _parse(tmp_path, **overrides):
    sub = tmp_path / "proj"
    sub.mkdir(exist_ok=True)
    scope = {"tools": ["file_read", "editor", "run_checks"], "workspace_subtree": str(sub)}
    body = issue_body(sub, scope=scope, **overrides)
    return parse_issue(body, roots=[tmp_path])


def test_a_declared_done_when_is_parsed_sorted_with_verifier_checks(tmp_path):
    request = _parse(tmp_path, verifier="checks", done_when=["ruff", "pytest"])
    assert request.done_when == ("pytest", "ruff") and request.verifier == "checks"
    assert _parse(tmp_path).done_when == ()  # absent: none (the v1 shape)


@pytest.mark.parametrize(
    ("overrides", "code"),
    [
        ({"verifier": "checks", "done_when": []}, "grant_field_invalid"),
        ({"verifier": "checks", "done_when": "pytest"}, "grant_field_invalid"),
        ({"verifier": "checks", "done_when": ["pytest", "pytest"]}, "grant_field_invalid"),
        ({"verifier": "checks", "done_when": ["auto"]}, "grant_field_invalid"),
        ({"verifier": "checks", "done_when": ["pytest tests/"]}, "grant_field_invalid"),
        ({"verifier": "checks", "done_when": [{"kind": "pytest"}]}, "grant_field_invalid"),
        ({"verifier": "checks", "done_when": ["pytest"] * 6}, "grant_field_invalid"),
        ({"verifier": "checks"}, "grant_verifier_invalid"),  # checks without done_when
        ({"verifier": "user", "done_when": ["pytest"]}, "grant_verifier_invalid"),
        ({"verifier": "model", "done_when": ["pytest"]}, "grant_verifier_invalid"),
    ],
)
def test_a_malformed_declaration_refuses_the_issue(tmp_path, overrides, code):
    with pytest.raises(GrantRefused) as refused:
        _parse(tmp_path, **overrides)
    assert refused.value.code == code


def test_done_when_needs_run_checks_in_the_scope(tmp_path):
    sub = tmp_path / "proj"
    sub.mkdir()
    body = issue_body(sub, verifier="checks", done_when=["pytest"])  # file_read, editor
    with pytest.raises(GrantRefused) as refused:
        parse_issue(body, roots=[tmp_path])
    assert refused.value.code == "grant_field_invalid"


# --- the record ---------------------------------------------------------------------------


def test_the_record_keeps_done_when_and_refuses_a_tampered_one(tmp_path):
    env = make_env(tmp_path, issue_extra=DONE)
    g = grant(env)
    assert (g.done_when, g.verifier) == (("pytest", "ruff"), "checks")
    stored = schema.grant_to_dict(g, "goal")
    assert stored["done_when"] == ["pytest", "ruff"]
    assert schema.grant_from_dict(stored)[0] == g
    for spoil in (
        {"done_when": []},
        {"done_when": ["pytest", "node_test", "ruff", "unittest", "compileall", "x"]},
        {"done_when": ["ruff", "pytest"]},  # not as written (sorted)
        {"verifier": "user"},  # the pair must match
        {"tools": ["file_read", "editor"]},  # no run_checks in the scope
    ):
        with pytest.raises(GrantRefused):
            schema.grant_from_dict({**stored, **spoil})
    without = {k: v for k, v in stored.items() if k != "done_when"}
    with pytest.raises(GrantRefused):
        schema.grant_from_dict(without)  # verifier checks without a declaration


def test_a_grant_without_done_when_is_unchanged(tmp_path):
    env = make_env(tmp_path)
    g = grant(env)
    assert (g.done_when, g.verifier) == ((), "user")
    assert "done_when" not in schema.grant_to_dict(g, "goal")
    assert "done_when" not in env.service._view(g)


# --- the host-confirmed issue -------------------------------------------------------------


def test_the_host_confirmed_summary_names_the_declared_kinds(tmp_path):
    service, *_rest, ws = make_service(tmp_path, contained=True)
    sub = ws / "proj"
    sub.mkdir()
    scope = {"tools": ["file_read", "run_checks"], "workspace_subtree": str(sub)}
    with pytest.raises(GrantConfirmation) as needed:
        service.issue(issue_body(sub, scope=scope, **DONE))
    assert needed.value.view["summary"]["changes"] == ["done_when:pytest", "done_when:ruff"]
    with pytest.raises(GrantConfirmation) as plain:
        service.issue(issue_body(sub, scope=scope))
    assert "changes" not in plain.value.view["summary"]


def test_a_changed_declaration_does_not_ride_on_the_confirmed_operation(tmp_path):
    """The body is bound to the operation: a declaration added or widened after the
    person's confirmation is refused."""
    service, *_rest, ws = make_service(tmp_path, contained=True)
    sub = ws / "proj"
    sub.mkdir()
    scope = {"tools": ["file_read", "run_checks"], "workspace_subtree": str(sub)}
    body = issue_body(sub, scope=scope, verifier="checks", done_when=["pytest"])
    with pytest.raises(GrantConfirmation) as needed:
        service.issue(body)
    view = needed.value.view
    service.confirm_operation(
        view["id"], token=view["confirm_token"], acknowledged=True, instance=view["instance"]
    )
    widened = {**body, "done_when": ["pytest", "ruff"]}
    with pytest.raises(GrantRefused):
        service.issue(widened, op_id=view["id"])


# --- the claim gate -----------------------------------------------------------------------


def test_a_claim_whose_checks_fail_is_refused_and_the_iteration_goes_on(tmp_path):
    env = make_env(tmp_path, issue_extra=DONE)
    _runner(env, _result(_row("pytest"), _row("ruff", "failed")))
    env.provider.script = [[_claim()], [{"text": "ok"}]]
    view = run_one(env)
    g = grant(env)
    assert g.goal_status == "active" and g.claim_evidence is None  # never reached a person
    assert view["end"] == "done"  # not "claimed": the iteration went on to the next turn
    assert env.provider.calls == 2
    (text,) = _tool_texts(env)
    assert text.startswith("not_proven") and "unmet: ruff)" in text
    ((arguments, host),) = env.checks
    assert arguments["kinds"] == ["pytest", "ruff"] and arguments["path"] == str(env.sub)
    assert "plan" in arguments and host is env.host  # the harness's plan, the verified host
    (line,) = env.executor.ledger_view(env.grant_id)["rounds"]
    assert line["done_when"] == {"result": "not_proven", "unmet": ["ruff"]}


def test_a_claim_whose_checks_pass_reaches_the_person(tmp_path):
    env = make_env(tmp_path, issue_extra=DONE)
    _runner(env, _result(_row("pytest"), _row("ruff")))
    env.provider.script = [[_claim()], [{"text": "ok"}]]
    view = run_one(env)
    g = grant(env)
    assert (g.status, g.goal_status) == ("active", "awaiting_verification")
    assert view["end"] == "claimed"
    assert g.claim_evidence.checks == "pass"  # the harness's own run is the evidence
    (line,) = env.executor.ledger_view(env.grant_id)["rounds"]
    assert line["done_when"] == {"result": "met", "unmet": []}


def test_the_model_cannot_prove_a_claim_with_its_own_checks(tmp_path):
    """A passing run_checks of the model's own choice (ruff only) does not count: the
    harness runs the declared kinds again at the claim, and the claim's arguments cannot
    carry anything else."""
    env = make_env(tmp_path, issue_extra=DONE)
    _runner(env, _result(_row("ruff")), _result(_row("ruff")))
    env.provider.script = [
        [use("run_checks", {"path": str(env.sub), "kinds": ["ruff"]})],
        [
            use(
                "goal_report",
                {"kind": "claim_complete", "code": "goal_met", "done_when": []},
                2,
            )
        ],
        [_claim(3)],
        [{"text": "ok"}],
    ]
    run_one(env)
    assert grant(env).goal_status == "active"
    assert grant(env).done_when == ("pytest", "ruff")
    assert env.checks[-1][0]["kinds"] == ["pytest", "ruff"]
    texts = _tool_texts(env)
    assert any("report_invalid" in t for t in texts)
    assert texts[-1].startswith("not_proven") and "unmet: pytest)" in texts[-1]


def test_an_unreadable_or_raising_run_proves_nothing(tmp_path):
    env = make_env(tmp_path, issue_extra=DONE)

    def runner(arguments, run_policy, host=None):
        raise RuntimeError("boom")

    env.executor._run_checks_runner = runner
    env.provider.script = [[_claim()], [{"text": "ok"}]]
    run_one(env)
    assert grant(env).goal_status == "active"
    (line,) = env.executor.ledger_view(env.grant_id)["rounds"]
    assert line["done_when"] == {"result": "not_proven", "unmet": ["pytest", "ruff"]}


@pytest.mark.parametrize("rule", ["never", "ask"])
def test_run_checks_not_always_now_proves_nothing_and_runs_nothing(tmp_path, rule):
    env = make_env(tmp_path, issue_extra=DONE)
    env.rules["run_checks"] = rule
    env.provider.script = [[_claim()], [{"text": "ok"}]]
    run_one(env)
    assert grant(env).goal_status == "active" and env.checks == []
    assert env.broker.requests == []  # no card was shown for the harness's checks


def test_without_done_when_a_claim_runs_no_check_and_the_round_has_no_key(tmp_path):
    env = make_env(tmp_path)
    env.provider.script = [[_claim()], [{"text": "ok"}]]
    view = run_one(env)
    assert grant(env).goal_status == "awaiting_verification" and view["end"] == "claimed"
    assert env.checks == []
    (line,) = env.executor.ledger_view(env.grant_id)["rounds"]
    assert "done_when" not in line


# --- a stop or a write while the gate runs (security review of PR #839) -------------------


def _stopping_runner(env, stop):
    def runner(arguments, run_policy, host=None):
        env.checks.append((dict(arguments), host))
        stop()  # the stop arrives while the harness's checks run
        return _result(_row("pytest"), _row("ruff"))

    env.executor._run_checks_runner = runner


@pytest.mark.parametrize(
    ("stop", "end", "residual"),
    [
        (lambda env: setattr(env, "busy", True), "yielded_user_turn", False),
        (lambda env: env.executor.preempt_signal.arrive(), "yielded_user_turn", True),
        (lambda env: env.mono.__setitem__(0, env.mono[0] + 10_000), "timeout", True),
        (lambda env: env.authority.kill_switch(), "stopped", True),
        (lambda env: env.authority.narrow("rules_saved"), None, True),
        (
            lambda env: (env.mono.__setitem__(0, env.mono[0] + 10_000), env.executor.poll()),
            "timeout",
            True,
        ),
    ],
    ids=["turn_busy", "persons_turn", "wall_time", "in_flight_cancel", "narrow", "watchdog_poll"],
)
def test_a_stop_while_the_gate_runs_records_no_claim(tmp_path, stop, end, residual):
    env = make_env(tmp_path, issue_extra=DONE)
    _stopping_runner(env, lambda: stop(env))
    env.provider.script = [[_claim()], [{"text": "ok"}]]
    view = run_one(env)
    g = grant(env)
    assert g.claim_evidence is None and g.goal_status != "awaiting_verification"
    assert view["end"] != "claimed"
    if end is not None:
        assert view["end"] == end
    # the gate's run finished after a stop that signals (like a model's own run_checks,
    # :meth:`_stopped_since`; a bare busy lock without its signal is read at the boundary)
    assert ("run_checks_completed_after_stop" in view["residual"]) is residual, view


def test_a_met_gate_whose_write_seq_moved_before_recording_is_not_proven(tmp_path):
    """``_done_when_holds`` is False: a write began/ended between the gate's run and the
    claim's record — the claim is refused, every declared kind unmet."""
    env = make_env(tmp_path, issue_extra=DONE)
    _runner(env, _result(_row("pytest"), _row("ruff")))
    gate = env.executor._done_when_gate

    def moved(it):
        verdict = gate(it)
        with it.lock:
            it.write_seq += 1
        return verdict

    env.executor._done_when_gate = moved
    env.provider.script = [[_claim()], [{"text": "ok"}]]
    view = run_one(env)
    g = grant(env)
    assert g.goal_status == "active" and g.claim_evidence is None and view["end"] != "claimed"
    (line,) = env.executor.ledger_view(env.grant_id)["rounds"]
    assert line["done_when"] == {"result": "not_proven", "unmet": ["pytest", "ruff"]}
    assert "run_checks_completed_after_stop" not in view["residual"]


def test_a_transient_runner_error_is_not_reused_the_next_claim_runs_again(tmp_path):
    env = make_env(tmp_path, issue_extra=DONE)
    calls = []

    def runner(arguments, run_policy, host=None):
        calls.append(arguments)
        if len(calls) == 1:
            raise RuntimeError("container hiccup")
        return _result(_row("pytest"), _row("ruff"))

    env.executor._run_checks_runner = runner
    env.provider.script = [[_claim()], [_claim(2)], [{"text": "ok"}]]
    view = run_one(env)
    assert len(calls) == 2 and view["end"] == "claimed"
    assert grant(env).goal_status == "awaiting_verification"


def test_an_unreadable_result_is_not_reused_either(tmp_path):
    env = make_env(tmp_path, issue_extra=DONE)
    _runner(env, {"status": "passed"}, _result(_row("pytest"), _row("ruff")))
    env.provider.script = [[_claim()], [_claim(2)], [{"text": "ok"}]]
    view = run_one(env)
    assert len(env.checks) == 2 and view["end"] == "claimed"


def test_an_editor_write_in_the_same_turn_makes_the_gate_not_proven(tmp_path):
    """The child runs one turn's tools concurrently: a passing run that overlaps a write
    of the same turn proves nothing (every declared kind unmet)."""
    import time

    env = make_env(tmp_path, issue_extra=DONE)
    target = env.sub / "notes.md"

    def runner(arguments, run_policy, host=None):
        env.checks.append((dict(arguments), host))
        for _ in range(500):  # until the concurrent write happened (bounded)
            if target.exists():
                break
            time.sleep(0.01)
        return _result(_row("pytest"), _row("ruff"))

    env.executor._run_checks_runner = runner
    edit = use("editor", {"command": "create", "path": str(target), "file_text": "x\n"})
    env.provider.script = [[_claim(), edit], [{"text": "ok"}]]
    run_one(env)
    assert target.exists()
    g = grant(env)
    assert g.goal_status == "active" and g.claim_evidence is None
    (line,) = env.executor.ledger_view(env.grant_id)["rounds"]
    assert line["done_when"] == {"result": "not_proven", "unmet": ["pytest", "ruff"]}


def test_a_repeated_claim_with_no_write_since_reuses_the_refusal(tmp_path):
    env = make_env(tmp_path, issue_extra=DONE)
    _runner(env, _result(_row("pytest"), _row("ruff", "failed")))  # one run only
    env.provider.script = [[_claim()], [_claim(2)], [{"text": "ok"}]]
    view = run_one(env)
    assert len(env.checks) == 1 and grant(env).goal_status == "active"
    texts = _tool_texts(env)
    assert len(texts) == 2 and all("unmet: ruff)" in t for t in texts)
    # the reused verdict ran nothing; neither claim leaves a residual
    assert "run_checks_completed_after_stop" not in view["residual"]


def test_a_write_after_a_refusal_runs_the_checks_again_and_the_evidence_keeps_the_fail(
    tmp_path,
):
    """Pinned as specified: met after a not_proven reaches the person; the claim's
    evidence combines the iteration's runs since its last write (the earlier fail of a
    run before the write is stale, so only the later run counts)."""
    env = make_env(tmp_path, issue_extra=DONE)
    _runner(
        env, _result(_row("pytest"), _row("ruff", "failed")), _result(_row("pytest"), _row("ruff"))
    )
    edit = use(
        "editor", {"command": "create", "path": str(env.sub / "a.md"), "file_text": "x\n"}, 2
    )
    env.provider.script = [[_claim()], [edit], [_claim(3)], [{"text": "ok"}]]
    view = run_one(env)
    g = grant(env)
    assert len(env.checks) == 2 and view["end"] == "claimed"
    assert g.goal_status == "awaiting_verification" and g.claim_evidence.checks == "pass"


def test_two_runs_with_no_write_between_combine_and_keep_the_earlier_fail(tmp_path):
    """Pinned as the specification: the model's own failing run, no write, then a met gate —
    the claim reaches the person and its evidence keeps the earlier fail
    (``combine_checks``): the person sees both."""
    env = make_env(tmp_path, issue_extra=DONE)
    _runner(
        env, _result(_row("pytest"), _row("ruff", "failed")), _result(_row("pytest"), _row("ruff"))
    )
    own = use("run_checks", {"path": str(env.sub), "kinds": ["pytest", "ruff"]})
    env.provider.script = [[own], [_claim(2)], [{"text": "ok"}]]
    view = run_one(env)
    g = grant(env)
    assert view["end"] == "claimed" and g.goal_status == "awaiting_verification"
    assert g.claim_evidence.checks == "fail"
    first = pg.claim_checks(_result(_row("pytest"), _row("ruff", "failed")))
    second = pg.claim_checks(_result(_row("pytest"), _row("ruff")))
    assert pg.combine_checks(first, second)[0] == "fail"


def test_done_when_is_capped_at_what_one_run_checks_call_runs(tmp_path):
    from stiracore.tools import run_checks

    assert schema.DONE_WHEN_MAX == run_checks.MAX_CHECKS
    five = ["compileall", "node_test", "pytest", "ruff", "unittest"]
    with pytest.raises(GrantRefused):
        _parse(tmp_path, verifier="checks", done_when=five)
    assert len(_parse(tmp_path, verifier="checks", done_when=five[:4]).done_when) == 4


def test_the_confirmation_dialog_draws_exactly_the_servers_kinds():
    import re
    from pathlib import Path

    source = (
        Path(__file__).resolve().parents[1] / "dashboard/static/js/settings_grants.js"
    ).read_text(encoding="utf-8")
    (line,) = re.findall(r"const DONE_WHEN = \[(.*?)\];", source)
    drawn = set(re.findall(r'"done_when:([a-z_]+)"', line))
    assert drawn == set(schema.CHECK_KINDS)


# --- the verdict and the ledger's allow-list (pure) ---------------------------------------


def test_the_verdict_is_met_only_when_every_declared_kind_passed():
    declared = ("pytest", "ruff")
    assert pg.done_when_verdict(declared, _result(_row("pytest"), _row("ruff"))) == ("met", ())
    assert pg.done_when_verdict(declared, _result(_row("pytest"))) == ("not_proven", ("ruff",))
    failed = _result(_row("pytest", "failed"), _row("ruff"))
    assert pg.done_when_verdict(declared, failed) == ("not_proven", ("pytest",))
    for unreadable in (None, {"status": "refused", "reason": "x"}, {"status": "passed"}):
        assert pg.done_when_verdict(declared, unreadable) == ("not_proven", declared)


@pytest.mark.parametrize(
    "value",
    [
        {"result": "met", "unmet": ["ruff"]},
        {"result": "not_proven", "unmet": []},
        {"result": "passed", "unmet": []},
        {"result": "not_proven", "unmet": ["ruff", "pytest"]},
        {"result": "not_proven", "unmet": ["tests/test_x.py"]},
        {"result": "met", "unmet": [], "text": "x"},
    ],
)
def test_the_ledger_refuses_a_malformed_done_when(value):
    with pytest.raises(rl.LedgerSchemaError):
        rl._done_when(value)
