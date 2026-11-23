"""ADR 0015 rev 6, slice S5 (#784): ``page_look`` and the narrowed ``view_image`` in a grant.

Offline: a scripted provider, a scripted axis and a scripted ``page_look`` runner (no
browser, no model). The screenshots are written by ``page_check._save_screenshot`` itself,
so R1 holds what a real call would have noted.
"""

from __future__ import annotations

import json
import os
import shutil

import pytest
from grants_exec_helpers import (
    Broker,
    Counter,
    Env,
    ScriptedProvider,
    grant,
    make_parent,
    run_one,
    server_tools,
    use,
)
from grants_helpers import confirm_and_issue, issue_body, make_service
from strands.tools.tools import AgentTool
from strands.types._events import ToolResultEvent

from stiracore.grants import executor as ex
from stiracore.grants import round_ledger as rl
from stiracore.grants import schema
from stiracore.grants.schema import GrantRefused
from stiracore.grants.service import AutonomySettings, settings_from
from stiracore.tools import page_check as pc
from stiracore.tools import page_look as pl
from stiracore.tools import page_review
from stiracore.tools.page_shot_registry import registry as shots

PNG = b"\x89PNG\r\n\x1a\n-not-a-real-image-"
STICKY = (
    "browser_guard_unverified",
    "rules_locked",
    "record_unhealthy",
    "ledger_unhealthy",
    "write_floor_changed",
    "reviewer_changed",
    "platform_unmeasured",
)
PAGE_SCOPE = ("file_read", "editor", "page_look", "view_image")


class FakePageTool(AgentTool):
    """Only the name, the spec and ``policy`` matter: the executor never calls it."""

    def __init__(self, name, policy):
        super().__init__()
        self._name, self.policy = name, policy

    @property
    def tool_name(self):
        return self._name

    @property
    def tool_spec(self):
        properties = {"target": {"type": "string"}, "path": {"type": "string"}}
        return {
            "name": self._name,
            "description": self._name,
            "inputSchema": {"json": {"type": "object", "properties": properties}},
        }

    @property
    def tool_type(self):
        return "python"

    async def stream(self, tool_use, invocation_state, **kwargs):  # pragma: no cover
        raise AssertionError("the executor must not call the wrapped page tool")
        yield ToolResultEvent({})


class Axis:
    """The executor's axis reader: ``plan(n, full) -> code`` for its n-th call."""

    def __init__(self):
        self.calls, self.plan = [], None

    def __call__(self, tools, ledger, *, full=False):
        self.calls.append((frozenset(tools), full))
        if "file_read" not in tools:
            return "not_in_scope"  # A9, as ``page_check.Axis._file_read`` answers a grant
        return self.plan(len(self.calls), full) if self.plan else "pass"


def page_env(tmp_path, monkeypatch, script=None, *, tools=PAGE_SCOPE, rules=None, setting=True):
    tmp_path.mkdir(parents=True, exist_ok=True)
    service, authority, recorder, _clocks, _breaker, ws = make_service(tmp_path, contained=True)
    service.settings = AutonomySettings(
        stage_c=True,
        allow_browser_confirm=True,
        stage_c_exec=True,
        page_look_unattended=setting,
    )
    env = Env()
    env.issue_axis = "pass"
    service._page_axis = lambda scope: (
        "not_in_scope" if "file_read" not in scope else env.issue_axis
    )
    sub = ws / "proj"
    sub.mkdir(exist_ok=True)
    (sub / "index.html").write_text("<!doctype html><title>t</title>", encoding="utf-8")
    (ws / "other.html").write_text("<!doctype html><title>o</title>", encoding="utf-8")
    parent, policy, env.rules = make_parent(
        tmp_path, ws, rules={"page_look": "ask", "view_image": "always", **(rules or {})}
    )
    env.service, env.ws, env.sub, env.policy, env.recorder = service, ws, sub, policy, recorder
    env.provider = ScriptedProvider(script)
    env.broker = Broker(answer=None)
    env.axis = Axis()
    env.looks, env.shots, env.look_result, env.rows = [], [], None, []

    def runner(target, run_policy, *, browser=None, wait_ms=1000, actions=None):
        env.looks.append(target)
        scripted = env.look_result(len(env.looks)) if env.look_result else None
        if scripted is not None:
            return scripted
        relative, error = pc._save_screenshot(run_policy, PNG + bytes([len(env.looks)]))
        assert error is None
        env.shots.append(relative)
        return {"status": "ok", "screenshots": [relative], "elapsed_ms": 3}

    def record(tool, status, **fields):
        env.rows.append((tool, status, fields))

    monkeypatch.setitem(pl._AUDIT, "record", record)

    def provide(_names):
        plain = [t for t in server_tools(policy, run_checks=False)]
        plain = [t for t in plain if t.tool_name not in schema.PAGE_TOOLS]
        return [FakePageTool("page_look", policy), FakePageTool("view_image", policy), *plain]

    env.executor = ex.GoalIterationExecutor(
        service,
        authority,
        parent=lambda: parent,
        tool_provider=provide,
        broker=env.broker,
        recorder=recorder,
        estimator=Counter(),
        inner_model=lambda _parent: env.provider,
        page_axis=env.axis,
        page_look_runner=runner,
        stop_drain_sec=2.0,
    )
    env.body = issue_body(
        sub,
        scope={"tools": list(tools), "workspace_subtree": str(sub)},
        budget={"total_tokens": 20_000, "per_tick_tokens": 4_000},
    )
    return env


def issued(env):
    env.grant_id = confirm_and_issue(env.service, env.body)["grant"]["grant_id"]
    return env


def look(n=1, target="proj/index.html"):
    return [use("page_look", {"target": target}, n)]


def refused_step(env):
    with pytest.raises(GrantRefused) as caught:
        env.executor.step(env.grant_id)
    assert caught.value.code == "grant_iteration_refused"
    return caught.value.extra["reason"]


def rounds(env):
    return env.executor.ledger_view(env.grant_id)["rounds"]


# --- at issue --------------------------------------------------------------------------------


def test_grant_page_look_needs_setting_and_axis(tmp_path, monkeypatch):
    assert settings_from({"stage_c": True}).page_look_unattended is False  # default off
    assert settings_from({"stage_c": True, "page_look_unattended": True}).page_look_unattended
    loose = settings_from({"stage_c": True, "page_look_unattended": "yes"})
    assert not loose.page_look_unattended and "page_look_unattended_invalid" in loose.warnings

    def code_of(env):
        with pytest.raises(GrantRefused) as caught:
            env.service.issue(env.body)
        return caught.value.code, caught.value.extra

    off = page_env(tmp_path / "off", monkeypatch, setting=False)
    assert code_of(off) == ("grant_page_look_setting_off", {})
    env = page_env(tmp_path / "on", monkeypatch)
    env.service._page_axis = lambda scope: None  # page_look is not registered (no browser)
    assert code_of(env)[0] == "grant_page_look_unavailable"
    for bad, shown in (("guard_pending", "guard_pending"), ("weird", "axis_unreadable")):
        env.service._page_axis = lambda scope, bad=bad: bad
        assert code_of(env) == ("grant_page_look_axis", {"reason": shown})

    def raising(scope):
        raise RuntimeError("axis")

    env.service._page_axis = raising
    assert code_of(env) == ("grant_page_look_axis", {"reason": "axis_unreadable"})
    # the service's own default: nothing wired on this server -> not registered
    monkeypatch.setitem(pl._AUDIT, "record", None)
    monkeypatch.setitem(pl._AXIS, "scope", None)
    from stiracore.grants.service import _default_page_axis

    assert _default_page_axis(frozenset(PAGE_SCOPE)) is None
    env.service._page_axis = lambda scope: "pass"
    assert issued(env).grant_id


def test_grant_page_look_setting_is_read_at_every_call(tmp_path, monkeypatch):
    env = issued(page_env(tmp_path, monkeypatch, [look()]))
    env.axis.plan = lambda n, full: "pass"
    from dataclasses import replace

    env.executor.settings = replace(env.executor.settings, page_look_unattended=False)
    assert refused_step(env) == "axis_setting_off"  # the round boundary
    assert grant(env).goal_reason == "axis_failed" and env.axis.calls == []
    assert ex.GoalIterationExecutor._page_axis_code(env.executor, PAGE_SCOPE, full=False) == (
        "setting_off"
    )


def test_page_review_not_nameable_in_a_grant(tmp_path, monkeypatch):
    env = page_env(tmp_path, monkeypatch)
    assert "page_review" not in schema.TOOLS and "page_check" not in schema.TOOLS
    for tools in (["file_read", "page_review"], ["file_read", "page_check"]):
        env.body["scope"]["tools"] = tools
        with pytest.raises(GrantRefused) as caught:
            env.service.issue(env.body)
        assert caught.value.code == "grant_tool_invalid"
    env.body["scope"]["tools"] = ["file_read", "view_image"]  # only with page_look
    with pytest.raises(GrantRefused) as caught:
        env.service.issue(env.body)
    assert caught.value.code == "grant_tool_invalid"


def test_grant_page_look_requires_file_read_in_scope(tmp_path, monkeypatch):
    env = page_env(tmp_path / "a", monkeypatch, tools=("editor", "page_look"))
    with pytest.raises(GrantRefused) as caught:
        env.service.issue(env.body)
    assert (caught.value.code, caught.value.extra) == (
        "grant_page_look_axis",
        {"reason": "not_in_scope"},
    )
    # in the scope, but the person's rule for file_read reads Never now: A9 is measured
    # over the tools the live rules leave, so the round is refused and the goal blocked
    env = issued(page_env(tmp_path / "b", monkeypatch, [look()], rules={"file_read": "never"}))
    assert refused_step(env) == "axis_not_in_scope"
    assert "file_read" not in env.axis.calls[0][0] and env.axis.calls[0][1] is True
    assert grant(env).goal_reason == "axis_failed" and env.provider.calls == 0


def test_grant_axis_reads_the_wired_scope_as_a_grant(monkeypatch):
    seen = []

    class Stub:
        def check(self, scope, *, full=False):
            seen.append((scope, full))
            return pc.AxisResult("pass", (), 0.0)

    monkeypatch.setattr(pc, "axis", Stub())
    monkeypatch.setitem(pl._AXIS, "scope", None)
    assert not pl.wired() and pl.grant_axis({"file_read"}, None, full=True) == "axis_unreadable"
    assert seen == []  # nothing wired: nothing measured, never pass
    monkeypatch.setitem(pl._AXIS, "scope", lambda: pc.AxisScope(file_read_rule=lambda: "ask"))
    ledger = object()
    assert pl.wired() and pl.grant_axis({"file_read", "page_look"}, ledger, full=True) == "pass"
    scope, full = seen[0]
    assert scope.grant_tools == {"file_read", "page_look"} and scope.ledger is ledger and full
    # A9 for a grant, as the real axis answers it: in the scope, and the live rule Always
    from stiracore.tools.page_check_guard import Axis as RealAxis

    assert RealAxis()._file_read(scope)[1:] == (False, "file_read_not_always")
    always = pc.AxisScope(grant_tools=scope.grant_tools, file_read_rule=lambda: "always")
    assert RealAxis()._file_read(always)[1] is True
    assert RealAxis()._file_read(pc.AxisScope(grant_tools=frozenset({"editor"})))[1:] == (
        False,
        "not_in_scope",
    )


# --- the round boundary ----------------------------------------------------------------------


def test_grant_page_look_axis_failure_refuses_round(tmp_path, monkeypatch):
    env = issued(page_env(tmp_path, monkeypatch, [look()]))
    env.axis.plan = lambda n, full: "rules_locked"
    assert refused_step(env) == "axis_rules_locked"
    assert env.axis.calls == [(frozenset(PAGE_SCOPE), True)]  # one full check
    assert env.provider.calls == 0 and env.looks == [] and rounds(env) == []
    # a grant that names no page tool never runs the check
    plain = issued(page_env(tmp_path / "plain", monkeypatch, tools=("file_read", "editor")))
    plain.axis.plan = lambda n, full: "rules_locked"
    assert run_one(plain)["end"] == "no_action" and plain.axis.calls == []
    assert rounds(plain)[0]["axis"] == {"start": "not_run", "end": "not_run"}


@pytest.mark.parametrize("code", [*STICKY, "axis_unreadable", "not_run", "weird"])
def test_grant_page_look_sticky_failure_blocks_goal(tmp_path, monkeypatch, code):
    env = issued(page_env(tmp_path, monkeypatch, [look()]))
    env.axis.plan = lambda n, full: code
    known = code if code in rl.AXIS_CODES else "axis_unreadable"
    assert refused_step(env) == ex.axis_reason(known)
    state = grant(env)
    assert (state.goal_status, state.goal_reason) == ("blocked", "axis_failed")
    assert env.provider.calls == 0
    env.axis.plan = None  # the cause is gone: still blocked until a person resumes
    assert refused_step(env) == "grant_not_active"  # paused(goal_blocked)


@pytest.mark.parametrize("code", sorted(ex.AXIS_NONSTICKY))
def test_grant_page_look_nonsticky_failure_refuses_without_blocking(tmp_path, monkeypatch, code):
    from stiracore.scheduler import grant_tick

    assert {"guard_pending", "breaker_not_normal", "rules_changed", "axis_stale"} == (
        ex.AXIS_NONSTICKY
    )
    assert not ex.AXIS_NONSTICKY & set(STICKY)
    env = issued(page_env(tmp_path, monkeypatch, [look(), [{"text": "done"}]]))
    env.axis.plan = lambda n, full: code
    reason = refused_step(env)
    assert reason == ex.axis_reason(code) and reason in grant_tick.SKIP_REASONS
    assert reason not in grant_tick.STATE_SKIPS
    state = grant(env)
    assert (state.goal_status, state.goal_reason) == ("active", "")
    env.axis.plan = None  # measured again at the next press: it runs
    assert run_one(env)["end"] == "done" and len(env.looks) == 1


def test_a_heartbeat_start_is_skipped_with_the_axis_code():
    from stiracore.scheduler import grant_tick

    class Stamp:
        grant_id, now = "ab" * 16, 0.0

    def press(reason):
        ticker = grant_tick.GrantTicker.__new__(grant_tick.GrantTicker)

        class Refusing:
            def step(self, grant_id, *, tick):
                raise GrantRefused("grant_iteration_refused", reason=reason)

        ticker._executor = Refusing()
        return ticker._press(Stamp())

    for code in ex.AXIS_NONSTICKY:
        assert press(ex.axis_reason(code)) == ex.axis_reason(code)  # the same code
    for code in (*STICKY, "axis_unreadable", "setting_off"):
        assert press(ex.axis_reason(code)) == "goal_state"  # the goal is blocked now
    assert press("something_else") == "executor_refused"


# --- inside the round ------------------------------------------------------------------------


def test_a_grant_answers_page_look_without_a_card(tmp_path, monkeypatch):
    env = issued(page_env(tmp_path, monkeypatch, [look(), [{"text": "done"}]]))
    view = run_one(env)
    assert view["end"] == "done" and view["tool_calls"] == 1
    assert env.looks == [str(env.sub / "index.html")]  # the resolved path, not the call's text
    assert env.broker.requests == []  # rule Ask: the grant answered, no card
    tool, status, fields = env.rows[0]
    assert (tool, status) == ("page_look", "ok")
    assert fields["answered_by"] == "grant" and fields["origin"] == "grant"
    assert fields["grant"] == env.grant_id and fields["round"] == 1 and "window" not in fields
    # the row is inside the audit contract's allow-list (fixed forms)
    from stiracore.observability.audit_chain import AuditChain

    class Sink:
        def _emit(self, *args, **kwargs):
            return "event"

    assert AuditChain.record_page(Sink(), tool, status, **fields) == "event"
    assert shots.grant_of(pl.run_of(env.shots[0])) == env.grant_id  # R1's grant tag


def test_a_never_rule_keeps_page_look_out_of_the_round(tmp_path, monkeypatch):
    env = issued(
        page_env(tmp_path, monkeypatch, [look(), [{"text": "x"}]], rules={"page_look": "never"})
    )
    view = run_one(env)
    assert "page_look" not in view["tools"] and env.looks == []
    assert view["end"] == "tool_refused" and view["detail"] == "not_in_scope"


def test_grant_answered_never_passes_a_never_rule(tmp_path, monkeypatch):
    """The one new row of the approval gate: in a grant a saved Ask (or Always) for
    ``page_look`` reads ``always`` — the grant answers, no card — and a saved Never, a
    tool outside the scope and an unreadable rule read ``never``."""
    from types import SimpleNamespace

    it = SimpleNamespace(tools=PAGE_SCOPE)
    for rule, read in (("ask", "always"), ("always", "always"), ("never", "never")):
        env = page_env(tmp_path / rule, monkeypatch, rules={"page_look": rule})
        assert env.executor._rule_reader(it)("page_look") == read
    assert env.executor._rule_reader(SimpleNamespace(tools=("file_read",)))("page_look") == "never"
    env = page_env(tmp_path / "unreadable", monkeypatch)

    def raising(_name):
        raise RuntimeError("rules")

    monkeypatch.setattr(env.executor._parent().checkpoint_hooks, "policy_check", raising)
    assert env.executor._rule_reader(it)("page_look") == "never"
    # only page_look is answered by the grant, and only in the grant's child
    env = issued(page_env(tmp_path / "child", monkeypatch, [[{"text": "done"}]]))
    assert env.executor._parent().checkpoint_hooks.grant_answered == frozenset()
    built = []
    real = env.executor._make_child
    monkeypatch.setattr(env.executor, "_make_child", lambda it: built.append(real(it)) or built[0])
    run_one(env)
    assert built[0].checkpoint_hooks.grant_answered == frozenset({"page_look"})
    assert env.executor._parent().checkpoint_hooks.grant_answered == frozenset()


def test_grant_answered_does_not_pass_setting_off_axis_failure_or_out_of_scope(
    tmp_path, monkeypatch
):
    """The saved rule is Ask and the grant answers ``page_look`` only while the grant's own
    conditions hold: with the setting off, a failing axis check, a target outside the
    subtree or a scope without the tool, nothing runs — and still no card is shown."""
    from dataclasses import replace

    def run(name, arrange=None, target="proj/index.html", **kwargs):
        script = [look(target=target), [{"text": "x"}]]
        env = issued(page_env(tmp_path / name, monkeypatch, script, **kwargs))
        if arrange is not None:
            arrange(env)
        view = run_one(env)
        assert env.looks == [] and env.rows == [] and env.broker.requests == []
        return view

    def setting_off(env):
        def flip(_messages):  # after the round's admission, before the call
            env.executor.settings = replace(env.executor.settings, page_look_unattended=False)
            return look()

        env.provider.script = [flip, [{"text": "x"}]]

    view = run("setting", setting_off)
    assert (view["end"], view["detail"]) == ("page_tools_unavailable", "setting_off")

    def axis_fails(env):
        env.axis.plan = lambda n, full: "rules_locked" if n >= 2 else "pass"

    view = run("axis", axis_fails)
    assert (view["end"], view["detail"]) == ("page_tools_unavailable", "rules_locked")
    view = run("subtree", target="other.html")
    assert (view["end"], view["detail"]) == ("tool_refused", "outside_subtree")
    view = run("scope", tools=("file_read", "editor"))  # the scope does not name it
    assert "page_look" not in view["tools"] and view["tool_calls"] == 0


@pytest.mark.parametrize(
    ("target", "detail"),
    [
        ("other.html", "outside_subtree"),
        ("proj/../other.html", "outside_subtree"),
        ("FILE", "outside_subtree"),  # file:// of a workspace page outside the subtree
        ("http://localhost:8000/", "path_invalid"),
        ("proj/missing.html", "path_invalid"),
    ],
)
def test_grant_page_look_outside_subtree_refused(tmp_path, monkeypatch, target, detail):
    env = page_env(tmp_path, monkeypatch)
    if target == "FILE":
        target = (env.ws / "other.html").as_uri()
    env.provider.script = [look(target=target), [{"text": "x"}]]
    view = run_one(issued(env))
    assert (view["end"], view["detail"]) == ("tool_refused", detail)
    assert env.looks == [] and env.rows == []
    row = rounds(env)[0]
    assert row["tools"]["page_look"] == 0 and row["axis"] == {"start": "pass", "end": "pass"}


def test_axis_fails_again_after_the_permit_consume(tmp_path, monkeypatch):
    env = issued(page_env(tmp_path, monkeypatch, [look(), [{"text": "x"}]]))
    # 1: the boundary (full), 2: before the permit, 3: after the consume
    env.axis.plan = lambda n, full: "write_floor_changed" if n >= 3 else "pass"
    view = run_one(env)
    assert (view["end"], view["detail"]) == ("page_tools_unavailable", "write_floor_changed")
    assert [full for _tools, full in env.axis.calls] == [True, False, False]
    assert env.looks == [] and env.rows == []  # no browser, although the permit was consumed
    row = rounds(env)[0]
    assert row["end_code"] == "page_tools_unavailable" and row["detail"] == "write_floor_changed"
    assert row["axis"] == {"start": "pass", "end": "write_floor_changed"}
    assert grant(env).goal_status == "active"  # the next boundary refuses and blocks
    assert refused_step(env) == "axis_write_floor_changed"
    assert grant(env).goal_reason == "axis_failed"


def test_axis_failure_before_the_permit_takes_none(tmp_path, monkeypatch):
    env = issued(page_env(tmp_path, monkeypatch, [look(), [{"text": "x"}]]))
    env.axis.plan = lambda n, full: "guard_pending" if n >= 2 else "pass"
    view = run_one(env)
    assert (view["end"], view["detail"]) == ("page_tools_unavailable", "guard_pending")
    assert len(env.axis.calls) == 2 and env.looks == [] and view["tool_calls"] == 0


def test_round_record_carries_axis(tmp_path, monkeypatch):
    script = [
        look(),
        lambda _messages: [use("view_image", {"path": env.shots[-1]})],
        [{"text": "done"}],
        look(2),
        [{"text": "done"}],
    ]
    env = issued(page_env(tmp_path, monkeypatch, script))
    assert run_one(env)["end"] == "done"
    first = rounds(env)[0]
    assert first["axis"] == {"start": "pass", "end": "pass"}
    assert (first["tools"]["page_look"], first["tools"]["view_image"]) == (1, 1)
    assert first["tools"]["page_review"] == 0
    assert env.axis.calls[0][1] is True and env.axis.calls[-1][1] is False  # the end: light
    # the second round: the light check at its end fails (nothing stopped the round)
    count = len(env.axis.calls)
    env.axis.plan = lambda n, full: "breaker_not_normal" if n == count + 4 else "pass"
    assert run_one(env)["end"] == "done"
    assert rounds(env)[1]["axis"] == {"start": "pass", "end": "breaker_not_normal"}
    # a code the enum does not hold is never written as given
    assert rl.axis_code("setting_off") == "axis_unreadable"


def test_grant_round_bound_on_page_look_calls_and_failures(tmp_path, monkeypatch):
    assert (ex.PAGE_LOOK_CALLS_PER_ROUND, ex.PAGE_LOOK_FAILURES_PER_ROUND) == (12, 3)
    ok = {"status": "ok", "screenshots": [], "elapsed_ms": 1}
    script = [look(n) for n in range(1, 15)] + [[{"text": "done"}], look(20), [{"text": "done"}]]
    env = issued(page_env(tmp_path / "calls", monkeypatch, script))
    env.look_result = lambda n: dict(ok)
    view = run_one(env)
    assert len(env.looks) == 12 and view["tool_calls"] == 12  # the 13th and 14th: refused
    assert view["end"] == "done"  # the round goes on without them
    assert "page_look_call_limit" in json.dumps(env.provider.seen[-1])
    assert rounds(env)[0]["tools"]["page_look"] == 12
    run_one(env)  # a new round has its own slots
    assert len(env.looks) == 13

    # failures: any status but ok / truncated; a success between takes nothing back
    script = [look(n) for n in range(1, 7)] + [[{"text": "done"}]]
    env = issued(page_env(tmp_path / "fail", monkeypatch, script))
    results = ["error", "ok", "refused", "truncated", "error"]
    env.look_result = lambda n: {"status": results[n - 1], "reason": "page_error"}
    view = run_one(env)
    assert len(env.looks) == 5 and view["end"] == "done"  # the 6th call: refused
    assert "page_look_repeated_failure" in json.dumps(env.provider.seen[-1])

    # a runner that raises is a failure too
    script = [look(n) for n in range(1, 5)] + [[{"text": "done"}]]
    env = issued(page_env(tmp_path / "raise", monkeypatch, script))

    def boom(n):
        raise RuntimeError("browser")

    env.look_result = boom
    run_one(env)
    assert len(env.looks) == 3
    assert "page_look_repeated_failure" in json.dumps(env.provider.seen[-1])


# --- the narrowed view_image -------------------------------------------------------------------


def test_grant_view_image_only_registered_shots(tmp_path, monkeypatch):
    script = [
        look(),
        lambda _messages: [use("view_image", {"path": env.shots[-1]})],
        [use("view_image", {"path": "proj/index.html"}, 2)],
        [{"text": "done"}],
    ]
    env = issued(page_env(tmp_path, monkeypatch, script))
    assert run_one(env)["end"] == "done"
    shot = env.shots[0]
    run = pl.run_of(shot)
    views = [(status, fields) for tool, status, fields in env.rows if tool == "view_image"]
    assert [status for status, _fields in views] == ["ok", "refused"]
    assert views[0][1]["shots"] == [shots.shots(run)[0].sha256]
    assert views[1][1]["reason"] == "run_unknown" and views[1][1]["shots"] is None
    assert all(f["answered_by"] == "grant" and f["grant"] == env.grant_id for _s, f in views)

    mine, policy = env.grant_id, env.policy
    code, found = pl.grant_shot(shot, policy, mine)
    assert code is None and found[0] == (env.ws / shot).read_bytes()
    assert pl.grant_shot(str(env.ws / shot), policy, mine)[0] is None  # the absolute form
    # another grant's run, and a run no grant made (a chat call's)
    assert pl.grant_shot(shot, policy, "cd" * 16) == ("run_other_grant", None)
    plain, error = pc._save_screenshot(policy, PNG + b"chat")
    assert error is None and shots.grant_of(pl.run_of(plain)) is None
    assert pl.grant_shot(plain, policy, mine) == ("run_other_grant", None)
    assert not shots.tag(run, "cd" * 16) and shots.grant_of(run) == mine  # tagged once
    assert not shots.tag("20260101-000000-abcdef", mine)  # R1 does not hold it
    # not a screenshot of R1 at all
    for path in ("proj/index.html", ".stira-verify/../proj/x.png", f"{shot}/..", None, "a\0b"):
        assert pl.grant_shot(path, policy, mine) == ("run_unknown", None)
    assert pl.grant_shot(shot.replace("1.png", "2.png"), policy, mine)[0] == "run_unknown"
    # a changed file (same size), a link in its place, then a pruned run
    target = env.ws / shot
    original = target.read_bytes()
    target.write_bytes(original[:-1] + b"X")
    assert pl.grant_shot(shot, policy, mine) == ("shot_changed", None)
    target.unlink()
    os.symlink(env.sub / "index.html", target)
    assert pl.grant_shot(shot, policy, mine) == ("shot_changed", None)
    shutil.rmtree(target.parent)
    assert pl.grant_shot(shot, policy, mine) == ("shot_missing", None)


def test_page_review_refuses_a_grants_run(tmp_path, monkeypatch):
    env = page_env(tmp_path, monkeypatch)
    relative, _error = pc._save_screenshot(env.policy, PNG + b"review")
    run = pl.run_of(relative)
    reviewer = page_review.Reviewer(None, lambda: None)
    assert reviewer._images(run, env.policy)[0] is None  # no tag: as before
    assert shots.tag(run, "ef" * 16)
    assert reviewer._images(run, env.policy) == ("run_other_grant", [])


# --- the ledger's older readers ----------------------------------------------------------------


def test_old_reader_compat(tmp_path, monkeypatch):
    """``VERSION`` stays 1. The reader of the build before S5 (#778) has every axis code,
    the page tool counts, ``page_tools_unavailable`` and the axis detail codes already; the
    one enum S5 widened is ``STOP_CODES`` (``axis_failed``). So the previous reader — this
    module with that member removed — reads every row a round of S5 writes, and refuses
    (the ledger is then unhealthy: fail closed) only a row whose ``stop`` is
    ``axis_failed``."""
    assert rl.VERSION == 1
    env = issued(page_env(tmp_path, monkeypatch, [look(), [{"text": "x"}]]))
    env.axis.plan = lambda n, full: "write_floor_changed" if n >= 3 else "pass"
    run_one(env)
    env.axis.plan = None
    path = env.service.data_dir / rl.LEDGER_FILE
    lines = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    row = {k: v for k, v in lines[-1].items() if k not in ("v", "seq", "prev", "hash")}
    assert row["axis"] == {"start": "pass", "end": "write_floor_changed"}
    assert row["end_code"] == "page_tools_unavailable"

    monkeypatch.setattr(rl, "STOP_CODES", rl.STOP_CODES - {"axis_failed"})  # the reader before
    assert rl.validate(row) == row
    reader = rl.RoundLedger.open(env.service.data_dir)
    assert reader.unhealthy_reason is None and len(reader.records(env.grant_id)) == 1
    for start in sorted(rl.AXIS_CODES):
        rl.validate({**row, "axis": {"start": start, "end": "pass"}})
    rl.validate({**row, "tools": {**row["tools"], "page_look": 12, "view_image": 3}})
    with pytest.raises(rl.LedgerSchemaError):
        rl.validate({**row, "stop": "axis_failed"})


# --- #784 fix round: A9 in a grant is ``file_read``'s live rule Always -------------------------


def test_grant_page_look_capped_when_file_read_ask(tmp_path, monkeypatch):
    """A page can ``fetch`` any workspace file under ``file_read``'s floor. So a grant opens
    a page without a card only while ``file_read`` itself needs no card (its live rule is
    Always): with Ask the round is refused, nothing is read and no card is shown."""
    script = [look(), [{"text": "done"}]]
    env = issued(page_env(tmp_path / "ask", monkeypatch, script, rules={"file_read": "ask"}))
    assert refused_step(env) == "axis_file_read_not_always"
    assert env.provider.calls == 0 and env.looks == [] and env.broker.requests == []
    assert env.axis.calls == []  # the executor's own read of the rule, before the axis
    state = grant(env)
    assert (state.goal_status, state.goal_reason) == ("blocked", "axis_failed")
    assert "file_read_not_always" not in ex.AXIS_NONSTICKY
    # Always: it runs, still without a card (page_look's own rule is Ask)
    env = issued(page_env(tmp_path / "always", monkeypatch, script))
    assert run_one(env)["end"] == "done" and len(env.looks) == 1 and env.broker.requests == []
    # an unreadable rule is not Always
    env = issued(page_env(tmp_path / "unreadable", monkeypatch, script))
    hooks = env.executor._parent().checkpoint_hooks
    real = hooks.policy_check

    def raising(name):
        if name == "file_read":
            raise RuntimeError("rules")
        return real(name)

    monkeypatch.setattr(hooks, "policy_check", raising)
    assert refused_step(env).startswith("axis_") and env.looks == []


@pytest.mark.parametrize(
    ("rule", "detail"), [("ask", "file_read_not_always"), ("never", "not_in_scope")]
)
def test_file_read_rule_is_read_live_at_every_page_call(tmp_path, monkeypatch, rule, detail):
    """Inside a round: the rule is read again at each call (not the round's tool list), so
    a ``file_read`` rule lowered after the first look stops the next one — with the
    scripted axis still answering ``pass``."""

    def lowered(_messages):
        env.rules["file_read"] = rule
        return look(2)

    env = issued(page_env(tmp_path, monkeypatch, [look(), lowered, [{"text": "x"}]]))
    view = run_one(env)
    assert (view["end"], view["detail"]) == ("page_tools_unavailable", detail)
    assert len(env.looks) == 1 and env.broker.requests == []  # the first ran, the second not
    assert rounds(env)[0]["axis"] == {"start": "pass", "end": detail}
    if rule == "never":  # the light check is given the tools the live rules leave
        assert "file_read" not in env.axis.calls[-1][0] and env.axis.calls[-1][1] is False
    assert all("file_read" in tools for tools, _full in env.axis.calls[:-1])


def test_view_image_stops_too_when_file_read_is_lowered(tmp_path, monkeypatch):
    def lowered(_messages):
        env.rules["file_read"] = "ask"
        return [use("view_image", {"path": env.shots[-1]})]

    env = issued(page_env(tmp_path, monkeypatch, [look(), lowered, [{"text": "x"}]]))
    view = run_one(env)
    assert (view["end"], view["detail"]) == ("page_tools_unavailable", "file_read_not_always")
    assert [tool for tool, _status, _fields in env.rows] == ["page_look"]  # nothing was shown


# --- #784 fix round: the bound, the record, the root -------------------------------------------


def test_running_calls_are_busy_not_failures(tmp_path, monkeypatch):
    """A parallel batch: the calls over the running bound are refused ``page_look_busy``
    (nothing failed), take no slot, and a later call of the round still runs."""
    import threading

    it = type("It", (), {})()
    it.lock, it.page_calls, it.page_running, it.page_failures = threading.Lock(), 0, 0, 0
    slot, done = ex.GoalIterationExecutor._page_slot, ex.GoalIterationExecutor._page_done
    assert [slot(it) for _ in range(4)] == [None, None, None, "page_look_busy"]
    assert it.page_calls == 3  # the refused call took no slot
    done(it, False)
    assert slot(it) is None and slot(it) == "page_look_busy"
    for _ in range(3):
        done(it, True)
    assert (it.page_running, it.page_failures) == (0, 3)
    assert slot(it) == "page_look_repeated_failure"  # three calls ended in a failure

    busy, release = [], threading.Event()

    def counting(iteration):
        code = slot(iteration)
        if code is not None:
            busy.append(code)
            if len(busy) == 5:
                release.set()
        return code

    monkeypatch.setattr(ex.GoalIterationExecutor, "_page_slot", staticmethod(counting))
    batch = [use("page_look", {"target": "proj/index.html"}, n) for n in range(1, 9)]
    env = issued(page_env(tmp_path, monkeypatch, [batch, look(9), [{"text": "done"}]]))

    def result(n):
        if n <= 3:
            assert release.wait(20)  # the first three run until the other five were refused
        return {"status": "ok", "screenshots": [], "elapsed_ms": 1}

    env.look_result = result
    view = run_one(env)
    assert view["end"] == "done" and busy == ["page_look_busy"] * 5
    assert len(env.looks) == 4  # three of the batch, and the call after it
    seen = json.dumps(env.provider.seen[-1])
    assert "page_look_busy" in seen and "page_look_repeated_failure" not in seen
    assert rounds(env)[0]["tools"]["page_look"] == 4


def test_failures_stay_bounded_when_a_batch_runs_in_parallel(tmp_path, monkeypatch):
    """#790 N18: a running call holds one of the places a failure would take, so the bound
    is failures + running — a check of the running calls alone would let a batch start
    three more calls after two failures (five failures in the round)."""
    import threading

    slot, done = ex.GoalIterationExecutor._page_slot, ex.GoalIterationExecutor._page_done
    limit = ex.PAGE_LOOK_FAILURES_PER_ROUND
    for failures in range(limit + 1):
        it = type("It", (), {})()
        it.lock, it.page_calls, it.page_running = threading.Lock(), failures, 0
        it.page_failures = failures
        free = limit - failures
        refusal = "page_look_busy" if free else "page_look_repeated_failure"
        assert [slot(it) for _ in range(free + 2)] == [None] * free + [refusal] * 2, failures
        assert it.page_running == free
        for _ in range(free):
            done(it, True)  # every call that could start fails
        assert (it.page_running, it.page_failures) == (0, limit)
        assert slot(it) == "page_look_repeated_failure"

    # through the executor: two failures, then a batch of six that all would fail
    codes, ended, refused = [], [], threading.Event()

    def counting(iteration):
        code = slot(iteration)
        if code is not None:
            codes.append(code)
            if len(codes) == 5:
                refused.set()
        return code

    def ending(iteration, failed):
        ended.append(failed)
        done(iteration, failed)

    monkeypatch.setattr(ex.GoalIterationExecutor, "_page_slot", staticmethod(counting))
    monkeypatch.setattr(ex.GoalIterationExecutor, "_page_done", staticmethod(ending))
    batch = [use("page_look", {"target": "proj/index.html"}, n) for n in range(3, 9)]
    script = [look(1), look(2), batch, look(9), [{"text": "done"}]]
    env = issued(page_env(tmp_path, monkeypatch, script))

    def result(n):
        if n >= 3:
            refused.wait(10)  # a call of the batch runs until the rest of it was refused
        return {"status": "error", "reason": "page_error"}

    env.look_result = result
    view = run_one(env)
    assert view["end"] == "done"
    assert ended == [True] * limit and len(env.looks) == limit  # never a fourth failure
    assert codes == ["page_look_busy"] * 5 + ["page_look_repeated_failure"]
    assert rounds(env)[0]["tools"]["page_look"] == limit


def _lower(rule):
    return lambda env: env.rules.__setitem__("file_read", rule)


def _setting_off(env):
    from dataclasses import replace

    env.executor.settings = replace(env.executor.settings, page_look_unattended=False)


def _axis(answer):
    def change(env):
        env.axis.plan = answer

    return change


def _axis_raises(n, full):
    raise RuntimeError("axis")


@pytest.mark.parametrize(
    ("change", "end"),
    [
        (lambda env: None, "pass"),
        (_lower("ask"), "file_read_not_always"),
        (_lower("never"), "not_in_scope"),
        (_setting_off, "other"),
        (_axis(lambda n, full: "guard_pending"), "guard_pending"),
        (_axis(lambda n, full: "weird"), "axis_unreadable"),
        (_axis(_axis_raises), "axis_unreadable"),
    ],
    ids=["pass", "ask", "never", "setting_off", "axis_code", "unknown_code", "axis_raises"],
)
def test_axis_end_of_a_round_nothing_stopped_is_the_light_check_at_its_end(
    tmp_path, monkeypatch, change, end
):
    """#790 N8/N9: a round that ended by itself writes the light check read at its end —
    the setting, the live rules (A9) and the axis — also when that fails. A record only:
    the round's end code stays ``done`` and the goal is not blocked."""

    def last(_messages):
        change(env)  # after the round's last page call: only the end reads it
        return [{"text": "done"}]

    env = issued(page_env(tmp_path, monkeypatch, [look(), last]))
    view = run_one(env)
    assert (view["end"], len(env.looks)) == ("done", 1)
    row = rounds(env)[0]
    assert row["axis"] == {"start": "pass", "end": end}
    assert (row["end_code"], row["detail"], row["stop"]) == ("done", "none", None)
    assert grant(env).goal_status != "blocked"
    fulls = [full for _tools, full in env.axis.calls]
    assert fulls[0] is True and not any(fulls[1:])  # the boundary: full; every later: light


def test_axis_end_is_measured_without_a_page_call_and_after_another_stop(tmp_path, monkeypatch):
    """#790 P3: the end check does not depend on a page call having run, nor on how the
    round ended — only a page check's own stop is kept instead of it."""
    env = issued(page_env(tmp_path / "none", monkeypatch, [[{"text": "nothing"}]]))
    assert run_one(env)["end"] == "no_action" and env.looks == []
    assert rounds(env)[0]["axis"] == {"start": "pass", "end": "pass"}
    assert [full for _tools, full in env.axis.calls] == [True, False]
    # stopped by the target (not a page check): the end's light check is what is written
    script = [look(target="other.html"), [{"text": "x"}]]
    env = issued(page_env(tmp_path / "outside", monkeypatch, script))
    count = []

    def at_the_end(n, full):
        count.append(n)
        return "pass" if n <= 2 else "breaker_not_normal"  # 1: boundary, 2: the call

    env.axis.plan = at_the_end
    view = run_one(env)
    assert (view["end"], view["detail"]) == ("tool_refused", "outside_subtree")
    assert env.looks == [] and count == [1, 2, 3]
    assert rounds(env)[0]["axis"] == {"start": "pass", "end": "breaker_not_normal"}


def test_setting_off_inside_a_round_is_one_cause_in_the_record(tmp_path, monkeypatch):
    from dataclasses import replace

    def flip(_messages):
        env.executor.settings = replace(env.executor.settings, page_look_unattended=False)
        return look()

    env = issued(page_env(tmp_path, monkeypatch, [flip, [{"text": "x"}]]))
    view = run_one(env)
    assert (view["end"], view["detail"]) == ("page_tools_unavailable", "setting_off")
    row = rounds(env)[0]
    # the setting is not an axis result: ``other`` (the detail names it), never
    # ``axis_unreadable`` — nothing was unreadable
    assert row["detail"] == "setting_off" and row["axis"] == {"start": "pass", "end": "other"}


def test_audit_target_is_the_resolved_path_never_the_calls_text(tmp_path, monkeypatch):
    text = "proj/../proj/./index.html"
    env = issued(page_env(tmp_path, monkeypatch, [look(target=text), [{"text": "done"}]]))
    resolved = str(env.sub / "index.html")
    env.look_result = lambda n: {
        "status": "ok",
        "screenshots": [],
        "open_yourself": {"path": os.path.relpath(env.looks[-1], env.ws)},
    }
    recorded = []
    real = pl.record_call

    def spy(arguments, result, answered_by=None, **kwargs):
        recorded.append(dict(arguments))
        return real(arguments, result, answered_by, **kwargs)

    monkeypatch.setattr(pl, "record_call", spy)
    assert run_one(env)["end"] == "done"
    assert env.looks == [resolved]
    assert [call["target"] for call in recorded] == [resolved]  # what the record is given
    ((_tool, _status, fields),) = env.rows
    assert fields["target"] == pl.target_ref(os.path.join("proj", "index.html"))
    assert fields["target"] != pl.target_ref(text) and text not in json.dumps(fields)


@pytest.mark.parametrize("tool", ["page_look", "view_image"])
def test_page_tools_stop_when_the_subtree_root_was_replaced(tmp_path, monkeypatch, tool):
    """The grant's root replaced by a link inside a round (its files are all still there):
    a page tool call stops ``workspace_changed`` like ``editor`` — before any permit."""

    def swapped(_messages):
        moved = env.ws / "moved"
        os.rename(env.sub, moved)
        os.symlink(moved, env.sub, target_is_directory=True)
        if tool == "page_look":
            return look(2)
        return [use("view_image", {"path": env.shots[-1]})]

    if os.name == "nt":
        probe = tmp_path / "_symlink_probe"
        probe.mkdir()
        (tmp_path / "_symlink_probe_link").symlink_to(probe, target_is_directory=True)
    env = issued(page_env(tmp_path, monkeypatch, [look(), swapped, [{"text": "x"}]]))
    view = run_one(env)
    assert view["end"] == "workspace_changed" and view["tool_calls"] == 1
    assert len(env.looks) == 1 and [name for name, _s, _f in env.rows] == ["page_look"]


def test_a_raising_live_tool_list_still_ends_the_round_with_axis_unreadable(monkeypatch):
    """Review F7 (#784): ``_live_tools`` outside any try in ``_finish`` would leave the
    iteration open and refuse every later press ``iteration_running`` until a restart."""
    import inspect

    from stiracore.grants import executor as ex

    source = inspect.getsource(ex.GoalIterationExecutor._finish)
    start = source.index("axis_end = self._page_axis_code(self._live_tools(it)")
    # the call is guarded: a try/except Exception sits right around it
    window = source[max(0, start - 200) : start + 400]
    assert "try:" in window and "axis_unreadable" in window
