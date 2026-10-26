"""W5 (#296): the goal iteration executor — end to end with a scripted provider (offline).

Admission, one permit per effect consumed right before the I/O, stops injected between
authorize and consume at every I/O point, budget reservations, the tool allow-list, the
subtree and containment re-checks, goal_report, pre-emption, the approval wait, the wall
time, the goal-text marker, and no thread outliving a stop.
"""

from __future__ import annotations

import json
import os
import threading

import pytest
from grants_exec_helpers import MARKER, Broker, ScriptedProvider, grant, make_env, run_one, use

from stiracore.authority.lifecycle_events import ITERATION_REASONS, PLAYBOOK_CODE
from stiracore.core import hitl_reasons
from stiracore.core.engine import HITLAction, HITLDecision
from stiracore.grants import executor as ex_mod
from stiracore.grants.executor import (
    BLOCK_FOR_END,
    END_CODES,
    IterationBudget,
    IterationStop,
    load_playbook,
)
from stiracore.grants.schema import GrantRefused


def _edit(env, n=1, name="notes.md", text="x\n"):
    return use("editor", {"command": "create", "path": str(env.sub / name), "file_text": text}, n)


def _refusal(env) -> str:
    with pytest.raises(GrantRefused) as info:
        env.executor.step(env.grant_id)
    assert info.value.code == "grant_iteration_refused"
    return info.value.extra["reason"]


def _threads():
    return [t for t in threading.enumerate() if t.name.startswith("grants-iteration")]


# --- end to end ---------------------------------------------------------------------------


def test_three_iterations_edit_check_then_a_kill_mid_iteration_stops_every_effect(tmp_path):
    env = make_env(tmp_path)
    for n in range(3):
        env.provider.script = [
            [_edit(env, n, f"step{n}.md", f"step {n}\n")],
            [use("run_checks", {"path": str(env.sub)}, n)],
            [{"text": "done for now"}],
        ]
        view = run_one(env)
        assert view["end"] == "done" and view["writes"] == 1 and view["checks"] == 1, view
        assert (env.sub / f"step{n}.md").read_text() == f"step {n}\n"
    g = grant(env)
    assert g.iterations == 3 and g.no_progress == 0 and g.used_tokens == 9 * 10
    # Iteration 4: the kill arrives while the first model call streams.
    calls = env.provider.calls
    env.provider.on_call = lambda n: env.authority.kill_switch() if n == calls + 1 else None
    env.provider.script = [
        [_edit(env, 9, "late.md")],
        [use("run_checks", {"path": str(env.sub)}, 9)],
    ]
    checks = len(env.checks)
    view = run_one(env)
    assert view["end"] in ("stopped", "permit_stale"), view
    assert not (env.sub / "late.md").exists() and len(env.checks) == checks
    assert env.provider.calls == calls + 1  # no further model call
    assert grant(env).status == "revoked" and grant(env).reason == "kill_switch"
    assert _threads() == []


def test_the_goal_reaches_the_model_input_only_as_the_trailing_block(tmp_path):
    env = make_env(tmp_path, script=[[{"text": "hi"}]])
    run_one(env)
    last = env.provider.seen[-1][-1]
    assert last["role"] == "user"
    tail = last["content"][-1]["text"]
    assert tail.startswith("<grant-context>") and tail.endswith("</grant-context>")
    assert MARKER in tail.split("The goal")[-1]
    assert env.executor.playbook.text.splitlines()[0] in tail


# --- admission ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("setup", "reason"),
    [
        (lambda e: e.service.pause(e.grant_id), "grant_not_active"),
        (lambda e: e.service.claim_complete(e.grant_id), "goal_not_active"),
        (lambda e: e.service.block_goal(e.grant_id, "needs_decision"), "grant_not_active"),
        (lambda e: e.authority.kill_switch(), "killed"),
        (lambda e: e.authority.report_unhealthy("cancel_overdue"), "authority_unhealthy"),
        (lambda e: setattr(e.breaker, "level", "warning"), "grant_not_active"),
        (lambda e: e.clocks.tick(7201), "grant_not_active"),
        (lambda e: setattr(e, "busy", True), "user_turn"),
        (
            lambda e: setattr(e.counter, "tokens", None) or setattr(e.executor, "_estimator", None),
            "token_count_unavailable",
        ),
        (lambda e: setattr(e.executor, "playbook", None), "playbook_unavailable"),
    ],
)
def test_admission_refuses_each_clause(tmp_path, setup, reason):
    env = make_env(tmp_path)
    setup(env)
    assert _refusal(env) == reason
    assert env.provider.calls == 0 and _threads() == []


def test_admission_checks_the_ttl_on_the_monotonic_clock_too(tmp_path):
    env = make_env(tmp_path)
    g = grant(env)
    # The look's clocks still agree, but the permit's monotonic deadline has passed.
    env.service._grants[env.grant_id] = g.__class__(
        **{**g.__dict__, "issued_mono": g.issued_mono - 7200}
    )
    assert _refusal(env) in ("ttl", "grant_not_active")


def test_max_iterations_admits_no_more(tmp_path):
    env = make_env(tmp_path, max_iterations=1)
    env.provider.script = [[_edit(env)], [{"text": "ok"}]]
    run_one(env)
    assert grant(env).goal_reason == "max_iterations"
    assert _refusal(env) == "grant_not_active"


def test_one_iteration_at_a_time(tmp_path):
    env = make_env(tmp_path)
    gate = threading.Event()
    env.provider.on_call = lambda n: gate.wait(10)
    env.executor.step(env.grant_id)
    try:
        assert _refusal(env) == "iteration_running"
        assert env.executor.view(env.grant_id)["state"] == "running"
    finally:
        gate.set()
        assert env.executor.wait_idle(10)


def test_a_halt_resume_recheck_is_satisfied_once_at_admission(tmp_path):
    from grants_helpers import confirm_and_resume

    env = make_env(tmp_path)
    env.breaker.level = "warning"
    env.service.check_now(env.grant_id)
    env.breaker.level = "normal"
    confirm_and_resume(env.service, env.grant_id, {"confirm": "RESUME"})
    assert grant(env).recheck
    try:
        run_one(env)
    except GrantRefused as refused:
        raise AssertionError(refused.extra) from None
    assert not grant(env).recheck


# --- permits at every I/O point -----------------------------------------------------------

STOPS = {
    "kill": lambda e: e.authority.kill_switch(),
    "revoke": lambda e: e.service.revoke(e.grant_id),
    "ttl": lambda e: e.clocks.tick(7201),
    "breaker": lambda e: setattr(e.breaker, "level", "critical"),
    "clock_jump": lambda e: e.clocks.jump_wall(3600),
    "session_reset": lambda e: e.service.session_reset(),
    "verification": lambda e: e.service.claim_complete(e.grant_id),
    "goal_blocked": lambda e: e.service.block_goal(e.grant_id, "out_of_scope"),
    "epoch_move": lambda e: e.authority.narrow("rules_saved"),
}
POINTS = {
    "model:authorized": lambda e: [[_edit(e)]],
    "tool:authorized": lambda e: [[_edit(e)]],
    "report:authorized": lambda e: [
        [use("goal_report", {"kind": "claim_complete", "code": "goal_met"})]
    ],
}


@pytest.mark.parametrize("stop", sorted(STOPS))
@pytest.mark.parametrize("point", sorted(POINTS))
def test_a_stop_between_authorize_and_consume_is_permit_stale_with_no_effect(tmp_path, point, stop):
    fired = []

    def probe(where, it):
        if where == point and not fired:
            fired.append(where)
            STOPS[stop](env)

    env = make_env(tmp_path, probe=probe)
    env.provider.script = POINTS[point](env)
    calls_before = env.provider.calls
    view = run_one(env)
    assert fired and view["end"] == "permit_stale", view
    assert not (env.sub / "notes.md").exists()
    assert grant(env).goal_status != "awaiting_verification" or stop == "verification"
    if point == "model:authorized":
        assert env.provider.calls == calls_before  # the request was never sent
    assert _threads() == []


@pytest.mark.parametrize("stop", ["revoke", "ttl"])
def test_a_stop_during_an_approval_wait_needs_a_new_permit_after_it(tmp_path, stop):
    def answer(request):
        # While the person looks at the card: a revoke (a narrowing), or the TTL passing
        # (no narrowing yet: only the next permit's look sees it).
        STOPS[stop](env)
        return HITLDecision(HITLAction.APPROVE, request_id=request.request_id)

    env = make_env(tmp_path, rules={"editor": "ask"}, broker=Broker(answer=answer))
    env.provider.script = [[_edit(env)]]
    view = run_one(env)
    assert env.broker.requests and view["end"] in ("permit_stale", "stopped"), view
    assert not (env.sub / "notes.md").exists()


def test_a_stop_before_the_approval_wait_shows_no_card(tmp_path):
    fired = []

    def probe(where, it):
        if where == "wait:authorized" and not fired:
            fired.append(1)
            env.service.revoke(env.grant_id)

    env = make_env(tmp_path, rules={"editor": "ask"}, probe=probe)
    env.provider.script = [[_edit(env)]]
    view = run_one(env)
    assert fired and view["end"] == "permit_stale" and env.broker.requests == []


def test_an_approved_card_runs_the_write_under_a_fresh_permit(tmp_path):
    seen = []

    def answer(request):
        seen.append(request.agent_id)
        return HITLDecision(HITLAction.APPROVE, request_id=request.request_id)

    env = make_env(tmp_path, rules={"editor": "ask"}, broker=Broker(answer=answer))
    env.provider.script = [[_edit(env)], [{"text": "ok"}]]
    view = run_one(env)
    assert view["writes"] == 1 and view["waits"] == 1, view
    assert seen == [f"grant:{env.grant_id}"]  # the card names the grant session


def test_an_unanswered_card_blocks_the_goal_approval_timeout(tmp_path):
    env = make_env(
        tmp_path,
        rules={"editor": "ask"},
        broker=Broker(
            answer=lambda r: HITLDecision(HITLAction.REJECT, reason=hitl_reasons.EXPIRED)
        ),
    )
    env.provider.script = [[_edit(env)], [{"text": "x"}]]
    view = run_one(env)
    assert view["end"] == "approval_timeout"
    g = grant(env)
    assert (g.goal_status, g.goal_reason) == ("blocked", "approval_timeout")
    assert g.status == "paused"  # no busy loop: nothing is admitted until a person resumes
    assert _refusal(env) == "grant_not_active"


def test_a_kill_while_a_card_waits_withdraws_it_and_ends_the_thread(tmp_path):
    env = make_env(tmp_path, rules={"editor": "ask"})
    env.provider.script = [[_edit(env)]]
    env.executor.step(env.grant_id)
    for _ in range(200):
        if env.broker.requests:
            break
        threading.Event().wait(0.01)
    env.authority.kill_switch()
    assert env.executor.wait_idle(10)
    assert env.broker.withdrawn and not (env.sub / "notes.md").exists()
    assert _threads() == []


def test_a_kill_during_the_editor_write_records_the_residual_and_stops(tmp_path):
    from stiracore.tools import file_editor_tool

    env = make_env(tmp_path)
    original = file_editor_tool.StiraWriteSandbox.write_file

    async def write_file(self, *args, **kwargs):
        env.authority.kill_switch()  # the effect is already running
        return await original(self, *args, **kwargs)

    file_editor_tool.StiraWriteSandbox.write_file = write_file
    try:
        env.provider.script = [[_edit(env)], [_edit(env, 2, "second.md")]]
        calls = env.provider.calls
        view = run_one(env)
    finally:
        file_editor_tool.StiraWriteSandbox.write_file = original
    assert "editor_completed_after_stop" in view["residual"], view
    assert not (env.sub / "second.md").exists() and env.provider.calls == calls + 1


# --- budget --------------------------------------------------------------------------------


def test_budget_exact_fit_one_over_and_release_once():
    budget = IterationBudget(per_tick=100)
    r = budget.reserve(100, used=0, total=100)  # exact fit
    with pytest.raises(IterationStop):
        budget.reserve(1, used=0, total=1000)  # one over the tick
    assert budget.refusal == "per_tick"
    assert budget.release(r) and not budget.release(r) and budget.releases == 1
    r2 = budget.reserve(60, used=40, total=100)
    with pytest.raises(IterationStop):
        budget.reserve(1, used=40, total=100)
    assert budget.refusal in ("grant_total", "per_tick")
    assert budget.settle(r2, 5) and not budget.release(r2) and budget.charged == 5


def test_a_shared_pool_refusal_releases_everything_once():
    class Pool:
        def __init__(self, ok):
            self.ok, self.released, self.reserved = ok, 0, 0

        def reserve(self, tokens):
            self.reserved += 1
            return object() if self.ok else None

        def settle(self, handle, tokens):
            pass

        def release(self, handle):
            self.released += 1

    good, bad = Pool(True), Pool(False)
    budget = IterationBudget(per_tick=100, pools=[good, bad])
    with pytest.raises(IterationStop):
        budget.reserve(10, used=0, total=100)
    assert budget.refusal == "pool" and good.released == 1 and budget.outstanding() == 0


def test_a_call_that_does_not_fit_is_never_sent(tmp_path):
    env = make_env(tmp_path, per_tick=59)  # count 10 + max_tokens 50 = 60
    env.provider.script = [[_edit(env)]]
    view = run_one(env)
    assert view["end"] == "budget_refused" and view["detail"] == "per_tick"
    assert env.provider.calls == 0 and grant(env).used_tokens == 0


def test_the_exact_fit_is_sent_and_settled_with_the_real_usage(tmp_path):
    env = make_env(tmp_path, per_tick=60)
    env.provider.script = [[{"text": "ok"}]]
    view = run_one(env)
    # W6a: a reply with no tool call and no report ends as no_action (was "done").
    assert view["end"] == "no_action" and env.provider.calls == 1
    assert grant(env).used_tokens == 10  # 7 in + 3 out, not the reservation


def test_unknown_usage_is_charged_the_whole_reservation(tmp_path):
    env = make_env(tmp_path)
    env.provider.usage = None
    env.provider.script = [[{"text": "ok"}]]
    run_one(env)
    assert grant(env).used_tokens == 60


def test_no_usable_token_count_fails_closed(tmp_path):
    env = make_env(tmp_path)
    env.counter.broken = True
    view = run_one(env)
    assert view["end"] == "token_count_unavailable" and env.provider.calls == 0


def test_no_max_tokens_fails_closed(tmp_path):
    env = make_env(tmp_path)
    env.provider.max_tokens = None
    view = run_one(env)
    assert view["detail"] == "max_tokens_unknown" and env.provider.calls == 0


def test_the_grant_total_accumulates_and_never_resets(tmp_path):
    env = make_env(tmp_path, total=130, per_tick=130)
    # W6a: two text-only iterations in a row would block the goal (no_action ×2); a report
    # each keeps it active (two model calls per iteration).
    for n in range(2):
        env.provider.script = [
            [use("goal_report", {"kind": "progress", "code": "investigating"}, n)],
            [{"text": "ok"}],
        ]
        run_one(env)
    assert grant(env).used_tokens == 40 and grant(env).goal_status == "active"
    env.provider.script = [[{"text": "ok"}]]
    env.counter.tokens = 41  # 41 + 50 + 40 > 130
    view = run_one(env)
    assert view["end"] == "budget_refused" and view["detail"] == "grant_total"
    assert grant(env).used_tokens == 40


def test_a_restart_never_runs_an_iteration_of_the_old_grant(tmp_path):
    from grants_helpers import make_service

    env = make_env(tmp_path)
    env.provider.script = [[{"text": "ok"}]]
    run_one(env)
    env.service.stop(5)
    again, *_ = make_service(tmp_path)
    g = again._get(env.grant_id)
    assert (g.status, g.reason) == ("revoked", "restart") and g.used_tokens == 10


def test_a_model_error_is_not_retried(tmp_path):
    env = make_env(tmp_path)

    def boom(_messages):
        raise RuntimeError(f"provider echo {MARKER}")

    env.provider.script = [boom]
    view = run_one(env)
    assert view["end"] == "model_error" and env.provider.calls == 1


# --- tools ---------------------------------------------------------------------------------


def test_the_childs_tool_list_is_the_scope_allow_list_only(tmp_path):
    from strands.tools.tools import AgentTool

    class Named(AgentTool):
        def __init__(self, name):
            super().__init__()
            self._name = name

        @property
        def tool_name(self):
            return self._name

        @property
        def tool_spec(self):
            return {
                "name": self._name,
                "description": "x",
                "inputSchema": {"json": {"type": "object"}},
            }

        @property
        def tool_type(self):
            return "python"

        async def stream(self, *a, **k):  # pragma: no cover
            yield None

    forbidden = [
        "shell",
        "http_request",
        "perplexity_search",
        "delegate",
        "handoff_to_agent",
        "allow_again",
        "memory_write",
        "file_write",
        "page_check",
        "mcp_github",
        "schedule_job",
    ]
    env = make_env(
        tmp_path, tools=("file_read", "editor", "run_checks", "memory_read", "current_time")
    )
    rules = {name: "always" for name in forbidden}
    env.rules.update(rules)
    from grants_exec_helpers import server_tools

    from stiracore.tools import tool_registry

    everything = [
        *server_tools(env.policy),
        *tool_registry.get_tools(None, include_exec=True),
        *[Named(n) for n in forbidden],
    ]
    env.executor._tool_provider = lambda names: everything
    grant_obj, names = env.executor._admit(env.grant_id)
    from stiracore.grants.executor import _Iteration

    it = _Iteration(grant_obj, 1, 1e9, env.executor.preempt_signal.seq, IterationBudget(100))
    it.tools = names
    child = env.executor._make_child(it)
    offered = set(child.agent.tool_registry.registry)
    assert offered <= {
        "file_read",
        "editor",
        "run_checks",
        "memory_read",
        "memory_search",
        "memory_list",
        "current_time",
        "goal_report",
    }, offered
    assert "goal_report" in offered and not offered & set(forbidden)
    assert child.agent_id == child.engine.agent_id == f"grant:{env.grant_id}"


def test_a_never_rule_removes_the_tool(tmp_path):
    env = make_env(tmp_path, rules={"editor": "never"})
    env.provider.script = [[_edit(env)], [{"text": "x"}]]
    view = run_one(env)
    assert "editor" not in view["tools"] and not (env.sub / "notes.md").exists()


def test_the_editor_cannot_leave_the_subtree(tmp_path):
    env = make_env(tmp_path)
    outside = env.ws / "outside.md"
    env.provider.script = [
        [use("editor", {"command": "create", "path": str(outside), "file_text": "x"})]
    ]
    run_one(env)
    assert not outside.exists()  # the child's narrowed write roots and the executor


def test_the_executors_own_subtree_check(tmp_path):
    from stiracore.grants.executor import _Iteration

    env = make_env(tmp_path)
    it = _Iteration(grant(env), 1, 1e12, 0, IterationBudget(10))
    it.tools = ("editor",)
    env.executor._check_tool(it, "editor", {"path": str(env.sub / "ok.md")})
    (env.sub / "link").symlink_to(env.ws)
    for bad in (
        str(env.ws / "x.md"),
        str(env.sub / "link" / "x.md"),
        str(env.sub / "link"),
        "",
        "a\0b",
    ):
        it.stop_code = None
        with pytest.raises(IterationStop):
            env.executor._check_tool(it, "editor", {"path": bad.replace("\\0", "\0")})
        assert it.stop_code == "tool_refused"


def test_a_symlink_swapped_in_between_authorize_and_consume_is_refused(tmp_path):
    fired = []
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()

    def probe(where, it):
        if where == "tool:authorized" and not fired:
            fired.append(1)
            os.symlink(elsewhere, env.sub / "inner")

    env = make_env(tmp_path, probe=probe)
    env.provider.script = [
        [
            use(
                "editor",
                {"command": "create", "path": str(env.sub / "inner" / "f.md"), "file_text": "x"},
            )
        ]
    ]
    view = run_one(env)
    assert fired and view["detail"] == "outside_subtree", view
    assert not (elsewhere / "f.md").exists()


def test_containment_downgraded_between_permit_and_run_blocks_run_checks(tmp_path):
    fired = []

    def probe(where, it):
        if where == "tool:authorized" and not fired:
            fired.append(1)
            env.host.verified = False

    env = make_env(tmp_path, probe=probe)
    env.provider.script = [[use("run_checks", {"path": str(env.sub)})]]
    view = run_one(env)
    assert view["end"] == "run_checks_unavailable" and view["detail"] == "not_contained"
    assert env.checks == []
    g = grant(env)
    assert (g.goal_status, g.goal_reason) == ("blocked", "run_checks_unavailable")


def test_run_checks_refused_when_the_setting_is_off(tmp_path):
    from dataclasses import replace

    env = make_env(tmp_path)
    env.executor.settings = replace(env.executor.settings, run_checks_unattended=False)
    env.provider.script = [[use("run_checks", {"path": str(env.sub)})]]
    view = run_one(env)
    assert view["detail"] == "setting_off" and env.checks == []


def test_a_containment_failure_inside_the_run_blocks_too(tmp_path):
    env = make_env(tmp_path)
    env.executor._run_checks_runner = lambda a, p, host=None: {
        "status": "refused",
        "reason": "containment_unverified",
    }
    env.provider.script = [[use("run_checks", {"path": str(env.sub)})], [{"text": "x"}]]
    view = run_one(env)
    assert view["end"] == "run_checks_unavailable"


# --- goal_report ---------------------------------------------------------------------------


def test_a_completion_claim_only_awaits_verification(tmp_path):
    env = make_env(tmp_path)
    env.provider.script = [
        [use("goal_report", {"kind": "claim_complete", "code": "goal_met"})],
        [{"text": "x"}],
    ]
    view = run_one(env)
    g = grant(env)
    assert view["end"] == "claimed" and g.goal_status == "awaiting_verification"
    assert g.status == "active"
    assert _refusal(env) == "goal_not_active"


def test_a_blocked_report_uses_the_fixed_code(tmp_path):
    env = make_env(tmp_path)
    env.provider.script = [[use("goal_report", {"kind": "blocked", "code": "needs_decision"})]]
    view = run_one(env)
    assert (grant(env).goal_status, grant(env).goal_reason) == ("blocked", "needs_decision")
    assert (
        view["end"] == "reported_blocked"
    )  # W6a: was recorded as "stopped" (the block's own narrowing)


@pytest.mark.parametrize(
    "bad",
    [
        {"kind": "claim_complete", "code": "step_done"},
        {"kind": "progress", "code": "free text"},
        {"kind": "progress", "code": "step_done", "note": "free text"},
        {"kind": "done", "code": "goal_met"},
    ],
)
def test_a_report_outside_the_enum_changes_nothing(tmp_path, bad):
    env = make_env(tmp_path)
    env.provider.script = [[use("goal_report", bad)], [{"text": "x"}]]
    view = run_one(env)
    assert grant(env).goal_status == "active" and view["reports"] == 0


# --- pre-emption, wall time, shutdown ------------------------------------------------------


def test_a_user_turn_yields_at_the_next_boundary(tmp_path):
    env = make_env(tmp_path)
    env.provider.on_call = lambda n: env.executor.preempt_signal.arrive()
    env.provider.script = [[_edit(env)], [{"text": "x"}]]
    view = run_one(env)
    assert view["end"] == "yielded_user_turn" and view["state"] == "yielded"
    assert not (env.sub / "notes.md").exists()  # the next effect never ran
    assert env.provider.calls == 1
    assert grant(env).goal_status == "active"


def test_session_reset_revokes_and_stops_the_iteration(tmp_path):
    env = make_env(tmp_path)
    env.provider.on_call = lambda n: env.service.session_reset()
    env.provider.script = [[_edit(env)], [{"text": "x"}]]
    view = run_one(env)
    assert view["end"] in ("stopped", "permit_stale")
    assert grant(env).reason == "session_reset" and not (env.sub / "notes.md").exists()


def test_the_wall_time_ends_the_iteration_as_blocked(tmp_path):
    env = make_env(tmp_path)
    gate = threading.Event()

    def slow(n):
        env.mono[0] += 601
        env.executor.poll()
        gate.set()

    env.provider.on_call = slow
    env.provider.script = [[_edit(env)], [{"text": "x"}]]
    view = run_one(env)
    assert view["end"] == "timeout"
    assert (grant(env).goal_status, grant(env).goal_reason) == ("blocked", "iteration_timeout")


def test_shutdown_joins_the_thread_within_its_bound(tmp_path):
    env = make_env(tmp_path)
    started = threading.Event()
    release = threading.Event()

    def hold(n):
        started.set()
        release.wait(0.5)

    env.provider.on_call = hold
    env.provider.script = [[_edit(env)]]
    env.executor.step(env.grant_id)
    assert started.wait(10)
    assert env.executor.stop(5) in ("stopped", "abandoned")
    assert _threads() == []
    assert env.executor.wait_abandoned(5)
    assert not (env.sub / "notes.md").exists()
    assert _refusal(env) == "executor_stopped"


@pytest.mark.parametrize("stop", ["revoke", "kill"])
def test_no_thread_outlives_a_revoke_or_a_kill(tmp_path, stop):
    env = make_env(tmp_path)
    started = threading.Event()
    release = threading.Event()

    def hold(n):
        started.set()
        release.wait(10)

    env.provider.on_call = hold
    env.provider.script = [[_edit(env)], [_edit(env, 2, "b.md")]]
    env.executor.step(env.grant_id)
    assert started.wait(10)
    STOPS[stop](env)
    release.set()
    assert env.executor.wait_idle(10) and _threads() == []
    assert not (env.sub / "notes.md").exists()


# --- records: codes only; the goal marker --------------------------------------------------


def test_the_goal_marker_is_in_no_store_but_the_grant_store(tmp_path, caplog):
    import logging

    caplog.set_level(logging.DEBUG)
    env = make_env(tmp_path)
    histories = []
    original = env.executor._make_child

    def keep(it):
        child = original(it)
        histories.append(child)
        return child

    env.executor._make_child = keep
    logging.getLogger("strands.models.bedrock").debug("request=<%s>", f"dump {MARKER}")
    # botocore's transport dump of the Converse request (Astra B6).
    logging.getLogger("botocore.endpoint").debug("Making request with params: %s", MARKER)
    env.provider.script = [
        [_edit(env)],
        [use("goal_report", {"kind": "progress", "code": "step_done"}, 2)],
        [{"text": "ok"}],
    ]
    view = run_one(env)
    assert MARKER in str(env.provider.seen[-1])  # the model saw it
    env.service.stop(5)
    stores = []
    for path in (tmp_path / "data").rglob("*"):
        if path.is_file() and path.name != "grants.json":
            stores.append(path.read_bytes())
    assert all(MARKER.encode() not in raw for raw in stores)
    assert MARKER.encode() in (tmp_path / "data" / "grants.json").read_bytes()
    assert MARKER not in caplog.text
    assert MARKER not in str(view) and MARKER not in str(env.executor.status())
    assert MARKER not in str(env.service.view(env.grant_id))
    for child in histories:
        assert child.agent.messages and MARKER not in str(child.agent.messages)
        assert MARKER not in str(child.engine.conversation_history)
    for path in tmp_path.rglob("*"):
        if path.is_file() and path.name not in ("grants.json",) and path.suffix != ".md":
            assert MARKER.encode() not in path.read_bytes(), path


def test_a_model_error_never_carries_the_providers_text(tmp_path, caplog):
    env = make_env(tmp_path)

    def boom(_messages):
        raise RuntimeError(f"echo {MARKER}")

    env.provider.script = [boom]
    view = run_one(env)
    assert MARKER not in caplog.text and MARKER not in str(view)


def test_lifecycle_events_are_codes_with_the_playbook_version(tmp_path):
    env = make_env(tmp_path)
    posted = []
    env.executor._recorder = type("R", (), {"post": lambda self, event: posted.append(event)})()
    env.provider.script = [[{"text": "ok"}]]
    run_one(env)
    kinds = [(e["subject_kind"], e["to"], e["reason_code"]) for e in posted]
    assert kinds[0] == ("grant_iteration", "running", env.executor.playbook.code)
    assert kinds[-1] == ("grant_iteration", "idle", "iteration_no_action")  # W6a: was _done
    assert PLAYBOOK_CODE.fullmatch(env.executor.playbook.code)


def test_the_end_codes_are_the_lifecycle_contracts():
    codes = {"iteration_timeout" if c == "timeout" else f"iteration_{c}" for c in END_CODES}
    assert codes == ITERATION_REASONS
    assert set(BLOCK_FOR_END) <= END_CODES


def test_the_trusted_playbook_is_pinned_and_short():
    playbook = load_playbook()
    assert playbook is not None
    # Only a person edits dev_playbook.md: update this pin with the edit.
    assert playbook.code == "playbook_49b43d2b6f3f"
    assert len(playbook.text) < 2000


def test_an_unusable_playbook_admits_nothing(tmp_path):
    link = tmp_path / "link.md"
    link.symlink_to(ex_mod.PLAYBOOK_FILE)
    assert load_playbook(link) is None
    assert load_playbook(tmp_path / "missing.md") is None
    big = tmp_path / "big.md"
    big.write_text("x" * 9000)
    assert load_playbook(big) is None


def test_a_user_turn_between_authorize_and_consume_yields_with_no_effect(tmp_path):
    fired = []

    def probe(where, it):
        if where == "tool:authorized" and not fired:
            fired.append(1)
            env.executor.preempt_signal.arrive()

    env = make_env(tmp_path, probe=probe)
    env.provider.script = [[_edit(env)], [{"text": "x"}]]
    view = run_one(env)
    assert fired and view["end"] == "yielded_user_turn"
    assert not (env.sub / "notes.md").exists()


def test_a_user_turn_withdraws_a_waiting_card(tmp_path):
    env = make_env(tmp_path, rules={"editor": "ask"})
    env.provider.script = [[_edit(env)], [{"text": "x"}]]
    env.executor.step(env.grant_id)
    for _ in range(500):
        if env.broker.requests:
            break
        threading.Event().wait(0.01)
    env.executor.preempt_signal.arrive()
    assert env.executor.wait_idle(5)
    assert env.broker.withdrawn and not (env.sub / "notes.md").exists()
    assert env.executor.view(env.grant_id)["end"] == "yielded_user_turn"


def test_admission_refuses_a_running_chat_turn_and_a_second_press(tmp_path):
    env = make_env(tmp_path)
    env.busy = True
    assert _refusal(env) == "user_turn"
    env.busy = False
    env.provider.script = [[{"text": "ok"}]]
    run_one(env)
    assert grant(env).iterations == 1


# --- fix round 1 (#622 reviews) ------------------------------------------------------------


def test_a_user_turn_during_the_admission_is_not_lost(tmp_path):
    """Astra B2 / Opus B1: the message counter is read before the admission."""
    env = make_env(tmp_path)
    real = env.executor._admit

    def admit(grant_id):
        result = real(grant_id)
        env.executor.preempt_signal.arrive()  # the chat route: the signal, then the lock
        env.busy = True
        return result

    env.executor._admit = admit
    env.provider.script = [[_edit(env, 1, "late.md", "late")], [{"text": "done"}]]
    assert _refusal(env) == "user_turn"
    assert env.provider.calls == 0 and not (env.sub / "late.md").exists()
    assert _threads() == []


def test_a_user_turn_after_the_admission_ends_at_the_first_boundary(tmp_path):
    env = make_env(tmp_path)
    real = env.executor._post

    def post(it, before, after, code):
        if after == "running":
            env.executor.preempt_signal.arrive()  # after the press was admitted
        return real(it, before, after, code)

    env.executor._post = post
    env.provider.script = [[_edit(env)], [{"text": "x"}]]
    view = run_one(env)
    assert view["end"] == "yielded_user_turn" and env.provider.calls == 0


@pytest.mark.parametrize("order", ["stop_during_start", "stop_after_start"])
def test_shutdown_racing_a_press_never_raises(tmp_path, order):
    """Opus p3: a stop that lands while a press starts its thread."""
    env = make_env(tmp_path)
    env.provider.script = [[{"text": "x"}]]
    out = {}
    real = env.executor._post

    def post(it, before, after, code):
        if order == "stop_during_start" and after == "running":
            worker = threading.Thread(target=lambda: out.setdefault("r", env.executor.stop(1.0)))
            worker.start()
            worker.join(0.2)  # it waits for the press to finish starting
            out["worker"] = worker
        return real(it, before, after, code)

    env.executor._post = post
    env.executor.step(env.grant_id)
    if order == "stop_after_start":
        out["r"] = env.executor.stop(5.0)
    else:
        out["worker"].join(5)
    assert out.get("r") == "stopped", out
    assert env.executor.wait_idle(5) and _threads() == []


def test_no_provider_request_precedes_the_permit(tmp_path):
    """Astra B4 / Opus B2: the bound is local; a revoke before the call means no request."""
    env = make_env(tmp_path)
    real = env.executor._context

    def context(*args):
        out = real(*args)
        env.service.revoke(env.grant_id)
        return out

    env.executor._context = context
    view = run_one(env)
    assert env.provider.calls == 0 and view["end"] in ("stopped", "permit_stale")


def test_the_ttl_passing_in_a_tool_sends_no_further_provider_request(tmp_path):
    """Opus p2: the TTL passes during an effect (no narrowing); the next call is refused
    before anything reaches the provider."""
    from strands.tools.tools import AgentTool
    from strands.types._events import ToolResultEvent

    class Tick(AgentTool):
        @property
        def tool_name(self):
            return "current_time"

        @property
        def tool_spec(self):
            return {
                "name": "current_time",
                "description": "t",
                "inputSchema": {"json": {"type": "object"}},
            }

        @property
        def tool_type(self):
            return "python"

        async def stream(self, tool_use, invocation_state, **kw):
            env.clocks.tick(7201)
            yield ToolResultEvent(
                {
                    "toolUseId": tool_use["toolUseId"],
                    "status": "success",
                    "content": [{"text": "t"}],
                }
            )

    env = make_env(tmp_path, tools=("file_read", "current_time"))
    env.executor._tool_provider = lambda names: [Tick()]
    env.provider.script = [[use("current_time", {})], [{"text": "x"}]]
    view = run_one(env)
    assert env.provider.calls == 1 and view["end"] in ("stopped", "permit_stale")


def test_the_local_bound_is_an_upper_bound_and_fails_closed():
    import json as _json

    from stiracore.grants.executor import request_bound

    request = {
        "messages": [{"role": "user", "content": [{"text": "日本語 and ascii " * 50}]}],
        "system": [{"text": "system"}],
        "toolConfig": {"tools": [{"toolSpec": {"name": "t"}}]},
    }
    json_size = len(_json.dumps(request["messages"], ensure_ascii=False).encode())
    assert request_bound(request) >= json_size > 0
    for bad in (b"\xff", float("nan"), "\ud800"):
        with pytest.raises((TypeError, ValueError, UnicodeError)):
            request_bound({"messages": [{"role": "user", "content": [{"text": bad}]}]})
    # A reference to an object store is sized as what is sent (only raw bytes refuse).
    assert (
        request_bound(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": [{"image": {"source": {"s3Location": {"uri": "s3://x/i"}}}}],
                    }
                ]
            }
        )
        < 1000
    )


def test_the_subtree_itself_retargeted_after_issue_is_refused_and_blocks(tmp_path):
    """Astra B3: the authorized root replaced by a symlink to a sibling in the write roots."""
    env = make_env(tmp_path)
    elsewhere = env.ws / "elsewhere"
    elsewhere.mkdir()
    env.sub.rmdir()
    env.sub.symlink_to(elsewhere, target_is_directory=True)
    env.provider.script = [
        [
            use(
                "editor",
                {"command": "create", "path": str(env.sub / "escape.md"), "file_text": "escape"},
            )
        ]
    ]
    assert _refusal(env) == "workspace_changed"
    assert not (elsewhere / "escape.md").exists()
    assert (grant(env).goal_status, grant(env).goal_reason) == ("blocked", "workspace_changed")


@pytest.mark.parametrize("tool", ["editor", "run_checks"])
def test_the_subtree_retargeted_mid_iteration_is_refused_at_the_consume(tmp_path, tool):
    fired = []
    elsewhere = tmp_path / "ws" / "elsewhere"

    def probe(where, it):
        if where == "tool:authorized" and not fired:
            fired.append(1)
            elsewhere.mkdir()
            os.rename(env.sub, tmp_path / "moved")
            env.sub.symlink_to(elsewhere, target_is_directory=True)

    env = make_env(tmp_path, probe=probe)
    args = (
        {"command": "create", "path": str(env.sub / "escape.md"), "file_text": "x"}
        if tool == "editor"
        else {"path": str(env.sub)}
    )
    env.provider.script = [[use(tool, args)], [{"text": "x"}]]
    view = run_one(env)
    assert fired and view["end"] == "workspace_changed", view
    assert not (elsewhere / "escape.md").exists() and env.checks == []
    assert grant(env).goal_reason == "workspace_changed"


# --- approval cards with the real broker and store (Astra B1, Opus p5 / N2) -----------------


def _real_broker(tmp_path, ttl=60.0):
    from stiracore.core.approval_store import ApprovalStore
    from stiracore.observability.chat_api import HITLApprovalBroker

    store = ApprovalStore(tmp_path / "data" / "approvals.db")
    # A chat turn is "running" for the broker: a grant card must still name none.
    broker = HITLApprovalBroker(store=store, ttl_sec=ttl, turn_id_provider=lambda: "chatturn1")
    return broker, store


def _wait_card(broker, timeout=10.0):
    import time as _time

    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        live = [p for p in broker.pending() if not p.get("restored")]
        if live:
            return live[0]
        threading.Event().wait(0.01)
    raise AssertionError("no card")


def test_a_grant_card_keeps_the_echoed_goal_in_memory_only(tmp_path):
    broker, store = _real_broker(tmp_path)
    env = make_env(tmp_path, rules={"editor": "ask"}, broker=broker)
    echoed = f"goal was: tidy the notes {MARKER}"
    env.provider.script = [
        [use("editor", {"command": "create", "path": str(env.sub / "n.md"), "file_text": echoed})],
        [{"text": "x"}],
    ]
    env.executor.step(env.grant_id)
    try:
        card = _wait_card(broker)
        assert MARKER in card["normalized_input"]  # the live card a person decides on
        record = store.pending()[0]
        assert record.live_only and MARKER not in record.display
        assert '"grant_card":true' in record.display and '"inside":true' in record.display
        broker.decide(card["id"], HITLDecision(HITLAction.REJECT, reason="no"))
        assert env.executor.wait_idle(10)
        assert MARKER not in str(broker.history())
        assert MARKER not in str(broker.pending())
    finally:
        broker.close()
        env.service.stop(5)
        store.close()
    for path in tmp_path.rglob("*"):
        if path.is_file() and path.name != "grants.json":
            assert MARKER.encode() not in path.read_bytes(), path


@pytest.mark.parametrize("how", ["kill", "user_turn", "revoke"])
def test_the_real_broker_ends_a_withdrawn_card_at_once(tmp_path, how):
    import time as _time

    broker, store = _real_broker(tmp_path)
    env = make_env(tmp_path, rules={"editor": "ask"}, broker=broker)
    env.provider.script = [[_edit(env)], [{"text": "x"}]]
    env.executor.step(env.grant_id)
    try:
        card = _wait_card(broker)
        assert card["turn_id"] is None  # a grant card belongs to no chat turn
        assert store.pending()[0].live_only
        start = _time.monotonic()
        {
            "kill": env.authority.kill_switch,
            "user_turn": env.executor.preempt_signal.arrive,
            "revoke": lambda: env.service.revoke(env.grant_id),
        }[how]()
        assert env.executor.wait_idle(5) and _time.monotonic() - start < 2.0
        late = broker.decide(
            card["id"], HITLDecision(HITLAction.APPROVE), args_hash=card["args_hash"]
        )
        assert late.name != "APPLIED" and not (env.sub / "notes.md").exists()
        entry = next(e for e in broker.history() if e["id"] == card["id"])
        assert entry["resolution_reason_code"] == "grant_stopped"
    finally:
        broker.close()
        env.service.stop(5)
        store.close()


def test_a_grant_card_left_in_the_store_is_cancelled_at_the_next_start(tmp_path):
    from stiracore.core.approval_store import ApprovalStore
    from stiracore.observability.chat_api import HITLApprovalBroker

    db = tmp_path / "approvals.db"
    store = ApprovalStore(db)
    store.submit(
        request_id="g1",
        agent_id="grant:" + "a" * 32,
        tool_name="editor",
        tool_input={"path": "x"},
        expires_at=1e12,
        display='{"grant_card":true}',
    )
    store.submit(
        request_id="t1",
        agent_id="nxp-agent",
        tool_name="editor",
        tool_input={"path": "x"},
        expires_at=1e12,
    )
    store.close()
    store = ApprovalStore(db)
    broker = HITLApprovalBroker(store=store)
    try:
        ids = {p["id"] for p in broker.pending()}
        assert "g1" not in ids and "t1" in ids
        assert store.get("g1").status == "cancelled"
    finally:
        broker.close()
        store.close()


# --- a stalled provider call (Astra B5) ----------------------------------------------------

STALL_STOPS = {
    "revoke": lambda e: e.service.revoke(e.grant_id),
    "kill": lambda e: e.authority.kill_switch(),
    "shutdown": lambda e: e.executor.stop(0.05),
    # The TTL passes; the watchdog's pass (the look) expires the grant and its narrowing
    # cancels the in-flight model call.
    "ttl": lambda e: (e.clocks.tick(7201), e.service.sync()),
}


@pytest.mark.parametrize("stop", sorted(STALL_STOPS))
def test_a_stalled_provider_call_is_abandoned_promptly_and_has_no_effect(tmp_path, stop):
    import time as _time

    env = make_env(tmp_path)
    started, release = threading.Event(), threading.Event()
    env.provider.on_call = lambda n: (started.set(), release.wait(20)) if n == 1 else None
    # The stalled call would answer with an edit; the next call too.
    env.provider.script = [[_edit(env)], [_edit(env, 2, "b.md")]]
    env.executor.step(env.grant_id)
    try:
        assert started.wait(10)
        t0 = _time.monotonic()
        STALL_STOPS[stop](env)
        assert env.executor.wait_idle(5), "the iteration thread did not end"
        # W6-F (#640): the grace, plus the drain a stop gives a model call still reading
        assert _time.monotonic() - t0 < ex_mod.ABANDON_GRACE_SEC + env.executor.stop_drain_sec + 1
        view = env.executor.view(env.grant_id)
        assert (
            view["state"] == "idle" and view["abandoned"] and "abandoned_call" in view["residual"]
        )
        assert _threads() == []
        used = grant(env).used_tokens
        assert used >= 60  # the abandoned call's whole reservation stays charged
        if stop not in ("shutdown",):
            assert _refusal(env) in ("abandoned_call", "grant_not_active", "killed")
        assert env.executor.status()["abandoned_calls"] == 1
    finally:
        release.set()
        assert env.executor.wait_abandoned(10)
    # Released: the late answer produces no effect, no charge, no second call.
    assert not (env.sub / "notes.md").exists() and not (env.sub / "b.md").exists()
    assert env.provider.calls == 1 and grant(env).used_tokens == used
    assert env.executor.status()["abandoned_calls"] == 0


def test_an_abandoned_call_blocks_the_next_press_until_it_ends(tmp_path):
    env = make_env(tmp_path)
    started, release = threading.Event(), threading.Event()
    env.provider.on_call = lambda n: (started.set(), release.wait(20)) if n == 1 else None
    env.provider.script = [[{"text": "x"}], [{"text": "y"}]]
    env.executor.step(env.grant_id)
    assert started.wait(10)
    env.executor.preempt_signal.arrive()  # a person's turn: it does not interrupt a call
    # ... but the wall time does: the call is abandoned, the grant stays active.
    env.mono[0] += 601
    env.executor.poll()
    assert env.executor.wait_idle(5)
    g = grant(env)
    if g.goal_status == "blocked":  # iteration_timeout blocks the goal; resume it
        from grants_helpers import confirm_and_resume

        confirm_and_resume(env.service, env.grant_id, {})
    try:
        assert _refusal(env) == "abandoned_call"
    finally:
        release.set()
        assert env.executor.wait_abandoned(10)
    env.mono[0] += 1
    run_one(env)
    assert env.provider.calls == 2


@pytest.mark.parametrize("stop", ["revoke", "ttl", "user_turn"])
def test_a_stop_during_a_running_write_is_a_residual_and_ends_the_iteration(tmp_path, stop):
    """Astra: cancellation is at tool boundaries — the running write finishes, the stop is
    recorded as a residual, and nothing runs after it."""
    from stiracore.tools.file_editor_tool import StiraWriteSandbox

    env = make_env(tmp_path)
    original = StiraWriteSandbox.write_file
    fired = []

    async def intercept(self, path, content, **kwargs):
        if not fired:
            fired.append(True)
            {
                "revoke": lambda: env.service.revoke(env.grant_id),
                "ttl": lambda: env.clocks.tick(7201),
                "user_turn": env.executor.preempt_signal.arrive,
            }[stop]()
        return await original(self, path, content, **kwargs)

    StiraWriteSandbox.write_file = intercept
    try:
        env.provider.script = [[_edit(env, 1, "late.md", "late")], [_edit(env, 2, "after.md")]]
        view = run_one(env)
    finally:
        StiraWriteSandbox.write_file = original
    assert fired and (env.sub / "late.md").read_text() == "late"
    assert "editor_completed_after_stop" in view["residual"], view
    assert not (env.sub / "after.md").exists() and env.provider.calls == 1


def test_admission_refuses_at_max_iterations_with_the_goal_still_active(tmp_path):
    from dataclasses import replace

    env = make_env(tmp_path, max_iterations=2)
    g = grant(env)
    env.service._grants[env.grant_id] = replace(g, iterations=2)
    assert _refusal(env) == "max_iterations"


@pytest.mark.parametrize("clock", ["wall", "mono"])
def test_the_admissible_check_reads_both_ttl_clocks(tmp_path, clock):
    from dataclasses import replace

    from stiracore.yolo.allowance import ClockReading

    env = make_env(tmp_path)
    g = grant(env)
    now = env.clocks()
    late = g.ttl_sec + 1
    reading = ClockReading(
        wall=g.issued_wall + (late if clock == "wall" else 1),
        boot=now.boot,
        mono=g.issued_mono + (late if clock == "mono" else 1),
    )
    assert env.executor._admissible(replace(g), reading) == "ttl"


# --- fix round 2 ---------------------------------------------------------------------------


@pytest.mark.parametrize("where", ["filename", "cwd", "content"])
def test_no_model_chosen_string_reaches_the_durable_card(tmp_path, where):
    """Astra r2 B1 / Opus B1, M1: the goal echoed as a file name, as a ``cwd`` or as the
    content never reaches approvals.db (+ WAL), the history or any other file."""
    broker, store = _real_broker(tmp_path)
    env = make_env(tmp_path, rules={"editor": "ask"}, broker=broker)
    name = f"{MARKER}.md" if where == "filename" else "n.md"
    args = {"command": "create", "path": str(env.sub / name), "file_text": "x"}
    if where == "cwd":
        args["cwd"] = f"/tmp/{MARKER}"
    if where == "content":
        args["file_text"] = f"goal was: {MARKER}"
    env.provider.script = [[use("editor", args)], [{"text": "x"}]]
    env.executor.step(env.grant_id)
    try:
        card = _wait_card(broker)
        record = store.pending()[0]
        assert MARKER not in record.display, record.display
        broker.decide(card["id"], HITLDecision(HITLAction.REJECT, reason="no"))
        assert env.executor.wait_idle(10)
        assert MARKER not in str(broker.history())
    finally:
        broker.close()
        env.service.stop(5)
        store.close()
    for path in tmp_path.rglob("*"):
        if path.is_file() and path.name != "grants.json":
            assert MARKER.encode() not in path.read_bytes(), path


def test_the_card_summary_is_codes_only_also_for_an_outside_path(tmp_path):
    from types import SimpleNamespace

    from stiracore.grants.executor import _Iteration
    from stiracore.observability.chat_api import _grant_card_display

    env = make_env(tmp_path)
    it = _Iteration(grant(env), 1, 1e12, 0, IterationBudget(10))
    request = SimpleNamespace(tool_name="editor", tool_input={"path": f"/elsewhere/{MARKER}.md"})
    summary = env.executor._card_summary(it, request)
    assert summary["inside"] is False and MARKER not in str(summary)
    inside = env.executor._card_summary(
        it, SimpleNamespace(tool_name="editor", tool_input={"path": str(env.sub / "a.md")})
    )
    assert inside["inside"] is True
    # The broker keeps fixed fields only, whatever a caller passes.
    shown = _grant_card_display({**summary, "path": MARKER, "tool": f"x {MARKER}", "note": MARKER})
    assert MARKER not in str(shown) and shown["grant_card"] is True and "tool" not in shown


def test_the_live_card_is_memory_only_and_gone_after_withdraw_resolve_restart(tmp_path):
    """Astra r2 B2 (design decision): the live card shows the arguments to the person who
    decides; nothing of it survives a withdraw, a decision or a restart."""
    from stiracore.core.approval_store import ApprovalStore
    from stiracore.observability.chat_api import HITLApprovalBroker

    for end in ("withdraw", "resolve", "restart"):
        base = tmp_path / end
        base.mkdir()
        broker, store = _real_broker(base)
        env = make_env(base, rules={"editor": "ask"}, broker=broker)
        env.provider.script = [
            [
                use(
                    "editor",
                    {"command": "create", "path": str(env.sub / "n.md"), "file_text": MARKER},
                )
            ],
            [{"text": "x"}],
        ]
        env.executor.step(env.grant_id)
        card = _wait_card(broker)
        assert MARKER in str(broker.pending())  # the live card: in memory
        if end == "withdraw":
            env.executor.preempt_signal.arrive()
        elif end == "resolve":
            broker.decide(card["id"], HITLDecision(HITLAction.REJECT, reason="no"))
        else:
            again = HITLApprovalBroker(store=ApprovalStore(base / "data" / "approvals.db"))
            assert MARKER not in str(again.pending()) and MARKER not in str(again.history())
            again.close()
            env.service.revoke(env.grant_id)
        assert env.executor.wait_idle(10)
        assert MARKER not in str(broker.pending()) and MARKER not in str(broker.history())
        broker.close()
        env.service.stop(5)
        store.close()


def test_a_webhook_turn_holding_the_lock_stops_a_running_iteration(tmp_path, monkeypatch):
    """Astra r2 B3: a turn that takes the shared turn lock without the chat signal (here a
    real webhook turn) — the started iteration yields at its next boundary; no write."""
    import hashlib
    import hmac

    from flask import Flask

    from stiracore.integrations.webhook import WebhookReceiver

    lock = threading.Lock()
    release, started = threading.Event(), threading.Event()
    env = make_env(tmp_path)
    env.executor._turn_busy = lock.locked
    env.provider.on_call = lambda n: (started.set(), release.wait(10)) if n == 1 else None
    env.provider.script = [[_edit(env, 1, "during.md", "during webhook")], [{"text": "done"}]]
    seen = {}

    def webhook_runner(prompt):
        release.set()
        seen["idle"] = env.executor.wait_idle(5)

    app = Flask(__name__)
    app.register_blueprint(WebhookReceiver(runner=webhook_runner, turn_lock=lock))
    monkeypatch.setenv("WEBHOOK_SECRET_GENERIC", "offline-key")
    raw = b'{"event":"ping"}'
    sig = "sha256=" + hmac.new(b"offline-key", raw, hashlib.sha256).hexdigest()
    try:
        env.executor.step(env.grant_id)
        assert started.wait(5)
        response = app.test_client().post(
            "/api/webhooks/generic",
            data=raw,
            content_type="application/json",
            headers={"X-Hub-Signature-256": sig},
        )
        assert response.status_code == 200 and seen["idle"]
        view = env.executor.view(env.grant_id)
        assert view["end"] == "yielded_user_turn", view
        assert not (env.sub / "during.md").exists() and env.provider.calls == 1
    finally:
        release.set()
        env.executor.wait_idle(5)


def test_a_turn_after_the_consume_stops_the_request(tmp_path):
    """Astra r2 non-blocking: a turn between the model call's consume and its request."""
    env = make_env(tmp_path)
    consume = env.executor._consume

    def race(it, permit, kind, reservation_id=None):
        entry = consume(it, permit, kind, reservation_id)
        if kind == "model":
            env.busy = True
            env.executor.preempt_signal.arrive()
        return entry

    env.executor._consume = race
    view = run_one(env)
    assert env.provider.calls == 0 and view["end"] == "yielded_user_turn"


# --- a real (offline) Bedrock model: the final request, SDK retries, transport dumps --------


def _offline_bedrock(**config):
    import boto3
    from strands.models.bedrock import BedrockModel

    session = boto3.Session(
        aws_access_key_id="offline", aws_secret_access_key="offline", region_name="us-east-1"
    )
    model = BedrockModel(
        boto_session=session, model_id="anthropic.claude-offline", max_tokens=50, **config
    )

    def no_send(*_a, **_k):
        raise RuntimeError("offline terminal")

    model.client._endpoint._send_request = no_send  # nothing ever leaves the process
    return model


def test_the_final_request_with_overrides_is_sized_and_refused(tmp_path):
    """Astra r2 B4: ``additional_args`` replacing the messages and maxTokens: the bound and
    the output limit come from the formatted request, so the call is refused."""
    from stiracore.grants.executor import request_bound

    extra = [{"role": "user", "content": [{"text": "X" * 100000}]}]
    model = _offline_bedrock(
        additional_args={"messages": extra, "inferenceConfig": {"maxTokens": 100000}}
    )
    env = make_env(tmp_path, total=50000, per_tick=40000)
    env.executor._estimator = request_bound
    env.executor._inner_model = lambda _parent: model
    sent = []
    model.client.converse_stream = lambda **request: sent.append(request)
    view = run_one(env)
    assert sent == [] and view["end"] == "budget_refused", view
    assert grant(env).used_tokens == 0


def test_an_sdk_internal_re_request_after_a_stop_sends_nothing(tmp_path):
    """Astra r2 B5: Strands' Bedrock model re-issues the request after a tool-result
    validation error, inside one model call; after the revoke the guarded client refuses."""
    from botocore.exceptions import ClientError

    model = _offline_bedrock()
    env = make_env(tmp_path)
    env.executor._inner_model = lambda _parent: model
    first, release = threading.Event(), threading.Event()
    calls = []
    context = env.executor._context

    def mixed(messages, it):
        out = context(messages, it)
        out.insert(
            0,
            {
                "role": "user",
                "content": [
                    {
                        "toolResult": {
                            "toolUseId": "previous",
                            "content": [{"text": "ok"}],
                            "status": "success",
                        }
                    }
                ],
            },
        )
        return out

    env.executor._context = mixed

    def converse(**request):
        calls.append(env.service._get(env.grant_id).status)
        if len(calls) == 1:
            first.set()
            release.wait(10)
            raise ClientError(
                {
                    "Error": {
                        "Code": "ValidationException",
                        "Message": "Conversation blocks and tool result blocks cannot be provided in the same turn.",
                    }
                },
                "ConverseStream",
            )
        raise RuntimeError("offline terminal")

    model.client.converse_stream = converse
    try:
        env.executor.step(env.grant_id)
        assert first.wait(5)
        env.service.revoke(env.grant_id)
        assert env.executor.wait_idle(5)
        release.set()
        assert env.executor.wait_abandoned(10)
        assert calls == ["active"], calls
    finally:
        release.set()
        env.executor.wait_abandoned(10)


def test_the_chat_agents_model_object_is_never_changed(tmp_path):
    model = _offline_bedrock()
    client = model.client
    env = make_env(tmp_path)
    env.executor._inner_model = lambda _parent: model
    run_one(env)
    assert model.client is client


def test_a_real_bedrock_request_leaves_no_goal_in_any_debug_log(tmp_path, caplog):
    import logging

    model = _offline_bedrock()
    env = make_env(tmp_path)
    env.executor._inner_model = lambda _parent: model
    caplog.set_level(logging.DEBUG)
    run_one(env)
    leaks = [(r.name, r.getMessage()[:80]) for r in caplog.records if MARKER in r.getMessage()]
    assert not leaks, leaks


def test_stop_never_raises_while_a_press_outlives_its_wait(tmp_path):
    """Opus r2 p3 / M2: a press still starting after the stop's wait ran out — the stop
    answers ``pending`` (no RuntimeError) and the press refuses at its publication."""
    env = make_env(tmp_path)
    entered, release = threading.Event(), threading.Event()
    real = env.executor._admit

    def admit(grant_id):
        entered.set()
        release.wait(5)
        return real(grant_id)

    env.executor._admit = admit
    out = {}
    press = threading.Thread(target=lambda: out.setdefault("press", _press(env)))
    press.start()
    assert entered.wait(5)
    assert env.executor.stop(0.05) == "pending"
    release.set()
    press.join(5)
    assert out["press"] == "executor_stopped" and _threads() == []
    assert env.provider.calls == 0


def test_stop_never_raises_for_a_published_thread_not_yet_started(tmp_path):
    env = make_env(tmp_path)
    env.provider.script = [[{"text": "x"}]]
    real = env.executor._post
    results = []

    def post(it, before, after, code):
        if after == "running":
            env.executor._starting = False  # as if the stop's wait had already ended
            try:
                results.append(env.executor.stop(0.05))
            except Exception as error:  # noqa: BLE001 - the test's own assertion
                results.append(f"raised {type(error).__name__}")
        return real(it, before, after, code)

    env.executor._post = post
    env.executor.step(env.grant_id)
    assert results == ["pending"], results
    assert env.executor.wait_idle(5) and env.provider.calls == 0


def _press(env):
    try:
        env.executor.step(env.grant_id)
    except GrantRefused as refused:
        return refused.extra.get("reason")
    return "started"


def test_a_parent_of_the_subtree_swapped_for_a_symlink_is_refused_at_admission(tmp_path):
    """Opus r2 M3 (c1): a parent component replaced by a symlink — lstat of the root sees a
    directory, but its resolved path is no longer the stored one."""
    env = make_env(tmp_path)
    other = tmp_path / "other"
    (other / "proj").mkdir(parents=True)
    os.rename(env.ws, tmp_path / "ws_moved")
    os.symlink(other, env.ws)
    assert _refusal(env) == "workspace_changed"
    assert not list(other.rglob("*.md"))


@pytest.mark.xfail(
    reason="same-user filesystem actor, out of scope: another process of the server's user "
    "renames the authorized directory between the sandbox's final check and the write (the "
    "agent's own tools cannot rename; docs/grants.md Known limits)",
    strict=True,
)
def test_rename_after_the_final_file_check_by_a_same_user_process(tmp_path, monkeypatch):
    """Astra r2 B6, documented disposition."""
    from stiracore.tools import checked_open

    env = make_env(tmp_path)
    moved = env.ws / "moved-outside"
    truncate = checked_open.os.ftruncate
    fired = []

    def race(fd, length):
        if not fired:
            fired.append(True)
            env.sub.rename(moved)
            env.sub.mkdir()
        return truncate(fd, length)

    monkeypatch.setattr(checked_open.os, "ftruncate", race)
    env.provider.script = [[_edit(env, 1, "escape.md", "outside")]]
    run_one(env)
    assert fired
    assert not (moved / "escape.md").exists()


# --- fix round 3 ---------------------------------------------------------------------------

_MODEL_INPUTS = {
    "tool_use_id": lambda env: _with_id(
        use("editor", {"command": "create", "path": str(env.sub / "a.md"), "file_text": "x"}),
        MARKER,
    ),
    "tool_name": lambda env: use(MARKER.replace("-", "_"), {"path": str(env.sub / "a.md")}),
    "path": lambda env: use(
        "editor", {"command": "create", "path": str(env.sub / f"{MARKER}.md"), "file_text": "x"}
    ),
    "cwd": lambda env: use(
        "editor",
        {"command": "create", "path": str(env.sub / "a.md"), "file_text": "x", "cwd": MARKER},
    ),
    "content": lambda env: use(
        "editor", {"command": "create", "path": str(env.sub / "a.md"), "file_text": MARKER}
    ),
    "error_text": lambda env: use(
        "editor",
        {
            "command": "str_replace",
            "path": str(env.sub / f"{MARKER}-missing.md"),
            "old_str": MARKER,
            "new_str": "y",
        },
    ),
}


def _with_id(block, tool_use_id):
    block["toolUse"]["toolUseId"] = tool_use_id
    return block


@pytest.mark.parametrize("field", sorted(_MODEL_INPUTS))
def test_no_model_controlled_input_reaches_a_durable_or_evented_field(tmp_path, field):
    """Astra r3 B1 (+ the table): the marker in every model-controlled input of a grant
    card. Each stored / evented field is a fixed code, a harness id, a digest, a number or a
    flag: approvals.db (+ WAL), every file, the history, the events, the pending list once
    the card ended."""
    from stiracore.core.approval_store import ApprovalStore
    from stiracore.observability.chat_api import HITLApprovalBroker

    events = []
    store = ApprovalStore(tmp_path / "data" / "approvals.db")
    broker = HITLApprovalBroker(
        store=store,
        ttl_sec=60,
        on_event=lambda k, d: events.append((k, d)),
        turn_id_provider=lambda: "chatturn1",
    )
    env = make_env(tmp_path, rules={"editor": "ask"}, broker=broker)
    env.provider.script = [[_MODEL_INPUTS[field](env)], [{"text": f"reply {MARKER}"}]]
    env.executor.step(env.grant_id)
    try:
        cards = []
        for _ in range(300):
            cards = [p for p in broker.pending() if not p.get("restored")]
            if cards or env.executor.view(env.grant_id)["state"] != "running":
                break
            threading.Event().wait(0.01)
        # Every case but an unknown tool name (refused before the gate) shows a card.
        assert bool(cards) == (field != "tool_name"), (field, cards)
        for card in cards:
            assert card["tool_use_id"].startswith("grant-") and MARKER not in card["tool_use_id"]
            for record in store.pending():
                assert MARKER not in json.dumps(record.__dict__, default=str)
            broker.decide(card["id"], HITLDecision(HITLAction.REJECT, reason="no"))
        assert env.executor.wait_idle(10)
        assert MARKER not in json.dumps(broker.history(), default=str)
        assert MARKER not in json.dumps(broker.pending(), default=str)
        assert events == []  # a grant card is never a chat turn's event
    finally:
        broker.close()
        env.service.stop(5)
        store.close()
    for path in tmp_path.rglob("*"):
        if path.is_file() and path.name != "grants.json":
            assert MARKER.encode() not in path.read_bytes(), path


@pytest.mark.parametrize("bad", ["invalid", float("nan"), object()])
def test_stop_with_an_unreadable_timeout_still_stops_first(tmp_path, bad):
    """Astra r3: the stop flag is set before the timeout is read; an unreadable timeout is
    the default bound, so the running iteration ends and its stalled call has no effect."""
    env = make_env(tmp_path)
    entered, release = threading.Event(), threading.Event()
    env.provider.on_call = lambda n: (entered.set(), release.wait(5)) if n == 1 else None
    env.provider.script = [[_edit(env, 1, "after-stop.md", "effect")], [{"text": "done"}]]
    try:
        env.executor.step(env.grant_id)
        assert entered.wait(5)
        status = env.executor.stop(bad)
        assert status in ("stopped", "abandoned"), status
    finally:
        release.set()
        env.executor.wait_abandoned(10)
    assert env.executor.wait_idle(5)
    assert not (env.sub / "after-stop.md").exists()


# --- fix round 3 addendum (Opus r3 N1-N3) -----------------------------------------------------


@pytest.mark.parametrize("decide_during_turn", [True, False])
def test_a_grant_cards_events_never_land_in_an_active_turn(tmp_path, decide_during_turn):
    """Opus r3 N1: a turn (a webhook-like one: the lock held, no signal) is active while a
    grant card waits; the card ends during it — the turn's stream keeps only its own
    events (contiguous seq), no grant event, nothing for the ribbon."""
    from stiracore.observability.chat_api import HITLApprovalBroker
    from stiracore.observability.turn_events import TurnEventRecorder, TurnEventStore

    tstore = TurnEventStore(tmp_path / "turns.db")
    recorder = TurnEventRecorder(tstore)
    broker = HITLApprovalBroker(on_event=recorder.emit)
    env = make_env(tmp_path, rules={"editor": "ask"}, broker=broker)
    env.provider.script = [
        [
            use(
                "editor",
                {"command": "create", "path": str(env.sub / f"{MARKER}.md"), "file_text": MARKER},
            )
        ],
        [{"text": "x"}],
    ]
    env.executor.step(env.grant_id)
    card = _wait_card(broker)
    recorder.active.set("turn-T")
    tstore.append("turn-T", "turn.started", {})
    env.busy = True
    if decide_during_turn:
        broker.decide(card["id"], HITLDecision(HITLAction.REJECT, reason="no"))
    else:
        env.executor.preempt_signal.arrive()
    assert env.executor.wait_idle(10)
    tstore.append("turn-T", "model.started", {"call_id": "c1"})
    events = tstore.events("turn-T")
    broker.close()
    env.service.stop(5)
    assert [(e["seq"], e["type"]) for e in events] == [(1, "turn.started"), (2, "model.started")]
    assert MARKER not in json.dumps(events)


def test_a_non_streaming_sdk_re_request_after_a_revoke_sends_nothing(tmp_path):
    """Opus r3 S1: ``streaming=False`` — the re-request goes through ``client.converse``."""
    from botocore.exceptions import ClientError

    model = _offline_bedrock(streaming=False)
    env = make_env(tmp_path)
    env.executor._inner_model = lambda _p: model
    context = env.executor._context

    def mixed(messages, it):
        out = context(messages, it)
        out.insert(
            0,
            {
                "role": "user",
                "content": [
                    {
                        "toolResult": {
                            "toolUseId": "p",
                            "content": [{"text": "ok"}],
                            "status": "success",
                        }
                    }
                ],
            },
        )
        return out

    env.executor._context = mixed
    first, release = threading.Event(), threading.Event()
    calls = []

    def converse(**request):
        calls.append(env.service._get(env.grant_id).status)
        if len(calls) == 1:
            first.set()
            release.wait(10)
            raise ClientError(
                {
                    "Error": {
                        "Code": "ValidationException",
                        "Message": "Conversation blocks and tool result blocks cannot be provided in the same turn.",
                    }
                },
                "Converse",
            )
        raise RuntimeError("offline terminal")

    model.client.converse = converse
    model.client.converse_stream = lambda **r: calls.append("STREAM")
    try:
        env.executor.step(env.grant_id)
        assert first.wait(5)
        env.service.revoke(env.grant_id)
        assert env.executor.wait_idle(5)
        release.set()
        assert env.executor.wait_abandoned(10)
    finally:
        release.set()
        env.executor.wait_abandoned(10)
    assert calls == ["active"], calls


def test_every_terminal_request_consumes_its_own_permit(tmp_path):
    """Opus r3 S2: a change only the authority sees (the grant's generation moved; the look
    still says active) between the terminal request's permit and its consume: no request."""
    model = _offline_bedrock()
    env = make_env(tmp_path)
    env.executor._inner_model = lambda _p: model
    sent = []
    model.client.converse_stream = lambda **r: sent.append(r)

    def probe(where, it):
        if where == "request:authorized":
            env.service.charge(env.grant_id, 0)  # a new generation, still active

    env.executor._probe = probe
    view = run_one(env)
    assert sent == [] and view["end"] == "permit_stale", view


def test_an_unguarded_provider_is_refused_at_admission_and_at_build(tmp_path):
    """Opus r3 S3 / N3: only Bedrock (proxied) or a model declaring one guarded request."""
    from stiracore.grants.executor import _Iteration

    class LazyClient(ScriptedProvider):
        stira_grant_terminal = False  # e.g. a Strands provider that creates its client per call

    class OwnClient(ScriptedProvider):
        client = object()
        stira_grant_terminal = False

    for unguarded in (LazyClient(), OwnClient()):
        base = tmp_path / type(unguarded).__name__
        base.mkdir()
        env = make_env(base)
        env.executor._inner_model = lambda _p, m=unguarded: m
        assert _refusal(env) == "provider_unsupported"
        it = _Iteration(grant(env), 1, 1e12, 0, IterationBudget(10))
        with pytest.raises(IterationStop):
            env.executor._child_model(unguarded, it)
        assert it.detail == "provider_unsupported"


def test_the_iterations_model_copy_has_its_own_config(tmp_path):
    """Opus r3 S4: a config update on the iteration's copy never reaches the chat model."""
    from stiracore.grants.executor import _Iteration

    model = _offline_bedrock()
    env = make_env(tmp_path)
    it = _Iteration(grant(env), 1, 1e12, 0, IterationBudget(10))
    child = env.executor._child_model(model, it)
    child.update_config(max_tokens=1)
    assert model.config["max_tokens"] == 50 and child is not model


def test_a_formatter_failure_sends_nothing(tmp_path):
    """Opus r3 S5."""
    env = make_env(tmp_path)

    def broken(*_a, **_k):
        raise RuntimeError("cannot format")

    env.provider.format_request = broken
    view = run_one(env)
    assert view["end"] == "token_count_unavailable" and view["detail"] == "no_formatter"
    assert env.provider.calls == 0


def test_the_tool_executors_debug_lines_are_dropped(tmp_path, caplog):
    """Opus r3 S6: ``strands.tools.executors`` logs a tool use (the model's arguments) at DEBUG."""
    import logging

    make_env(tmp_path)  # the executor exists: the filter is installed
    caplog.set_level(logging.DEBUG)
    logging.getLogger("strands.tools.executors._executor").debug("tool_use=<%s>", MARKER)
    logging.getLogger("strands.tools.executors._executor").info("an info line stays")
    assert MARKER not in caplog.text and "an info line stays" in caplog.text
