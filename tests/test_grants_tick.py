"""W6d (#296): the heartbeat-driven start — the scheduler's model-free ``grant_tick``.

Offline and deterministic: fake clocks, a scripted provider, no sleeps, no Bedrock.
"""

from __future__ import annotations

import dataclasses
import json
import threading

import pytest
from grants_exec_helpers import MARKER, ScriptedProvider, make_env, use
from grants_helpers import confirm_and_issue, confirm_and_resume, events, issue_body

from stiracore.grants.schema import GrantRefused
from stiracore.grants.service import AutonomySettings, settings_from
from stiracore.scheduler import grant_tick
from stiracore.scheduler.grant_tick import GrantTicker
from stiracore.scheduler.scheduler import RunToken

ON = {
    "stage_c": True,
    "allow_browser_confirm": True,
    "stage_c_exec": True,
    "iteration_wall_sec": 600,
    "heartbeat_start": True,
}


class Healthy:
    def __init__(self) -> None:
        self.value = True
        self.reads = 0

    def __call__(self):
        self.reads += 1
        return self.value


def _issue_flagged(env, *, flag=True, **overrides):
    """Revoke ``make_env``'s grant and issue a new one (with the heartbeat start)."""
    env.service.revoke(env.grant_id)
    body = issue_body(
        env.sub,
        goal=f"tidy the notes {MARKER}",
        scope={"tools": ["file_read", "editor"], "workspace_subtree": str(env.sub)},
        budget={"total_tokens": 10_000, "per_tick_tokens": 2_000},
        **({"heartbeat_start": True} if flag else {}),
        **overrides,
    )
    env.grant_id = confirm_and_issue(env.service, body)["grant"]["grant_id"]
    return env.grant_id


def tick_env(tmp_path, *, flag=True, spacing=300, script=None, issue=None, fresh=False, **kw):
    """A flagged grant; unless ``fresh``, the grant's clocks are moved past the spacing
    since its issue (decision 3: no heartbeat iteration within it)."""
    env = make_env(tmp_path, script=script, **kw)
    env.settings = AutonomySettings(**ON, tick_min_spacing_sec=spacing)
    env.service.settings = env.settings
    env.executor.settings = env.settings
    _issue_flagged(env, flag=flag, **(issue or {}))
    if not fresh:
        env.clocks.tick(spacing)
    env.healthy = Healthy()
    env.token = RunToken()  # the scheduler's run token (valid until it stops)
    env.ticker = GrantTicker(
        env.service,
        env.executor,
        settings=env.settings,
        authority=env.authority,
        recorder=env.recorder,
    )
    return env


def fire(env, source="periodic"):
    return env.ticker.fire(source=source, heartbeat_healthy=env.healthy, run_token=env.token)


def fire_and_wait(env, source="periodic"):
    code = fire(env, source)
    assert env.executor.wait_idle(20)
    return code


def grant_of(env):
    return env.service._get(env.grant_id)


def text_only():
    return [{"text": "nothing to do"}]


def write(env, n, text=None):
    path = str(env.sub / f"note{n}.md")
    return [use("editor", {"command": "create", "path": path, "file_text": text or f"n{n}"}, n)]


def scripted(env, *entries):
    env.provider.script = [e(env) if callable(e) else e for e in entries]
    return env


def w(n):
    return lambda env: write(env, n)


# --- settings ---------------------------------------------------------------------------------


def test_the_switch_defaults_off_and_needs_stage_c_exec():
    assert settings_from({}).heartbeat_start is False
    assert settings_from(None).tick_min_spacing_sec == 300
    alone = settings_from({"stage_c": True, "heartbeat_start": True})
    assert alone.heartbeat_start is False
    assert "heartbeat_start_needs_stage_c_exec" in alone.warnings
    on = settings_from({"stage_c": True, "stage_c_exec": True, "heartbeat_start": True})
    assert on.heartbeat_start is True and not on.warnings
    for bad in (1, "true", None, [True]):
        got = settings_from({"stage_c": True, "stage_c_exec": True, "heartbeat_start": bad})
        assert got.heartbeat_start is False and "heartbeat_start_invalid" in got.warnings


@pytest.mark.parametrize(
    ("value", "expected"),
    [(30, 30), (29, 300), (86400, 86400), (86401, 300), (True, 300), (60.0, 300), ("60", 300)],
)
def test_the_spacing_has_a_floor_a_cap_and_a_default(value, expected):
    got = settings_from({"stage_c": True, "tick_min_spacing_sec": value})
    assert got.tick_min_spacing_sec == expected
    assert ("tick_min_spacing_sec_invalid" in got.warnings) is (expected != value)


# --- the per-grant flag at issue ----------------------------------------------------------------


def test_the_flag_needs_the_global_switch(tmp_path):
    env = make_env(tmp_path)
    env.service.revoke(env.grant_id)
    body = issue_body(env.sub, heartbeat_start=True)
    try:
        env.service.issue(body)
        got = None
    except GrantRefused as refused:
        got = refused.code
    except Exception as other:  # noqa: BLE001 - e.g. the confirmation: not refused
        got = type(other).__name__
    assert got == "grant_heartbeat_start_off"
    for bad in (1, "true", None):
        with pytest.raises(GrantRefused) as refused:
            env.service.issue(issue_body(env.sub, heartbeat_start=bad))
        assert refused.value.code == "grant_field_invalid"


def test_the_flag_is_in_the_host_confirmed_view_and_bound_to_it(tmp_path):
    from stiracore.grants.service import GrantConfirmation

    env = tick_env(tmp_path)
    env.service.revoke(env.grant_id)
    body = issue_body(env.sub, heartbeat_start=True)
    with pytest.raises(GrantConfirmation) as needed:
        env.service.issue(body)
    view = needed.value.view
    assert view["summary"].get("source") == "heartbeat_start"
    env.service.confirm_operation(
        view["id"], token=view["confirm_token"], acknowledged=True, instance=view["instance"]
    )
    # The confirmation was for the flagged body: the same op with the flag off is refused.
    with pytest.raises(GrantRefused) as refused:
        env.service.issue({**body, "heartbeat_start": False}, op_id=view["id"])
    assert refused.value.code == "grant_operation_refused"
    # Without the flag the view names no source (as before W6d).
    with pytest.raises(GrantConfirmation) as plain:
        env.service.issue(issue_body(env.sub))
    assert "source" not in plain.value.view["summary"]


def test_the_stored_form_of_the_flag():
    from grants_helpers import request

    from stiracore.grants import state
    from stiracore.grants.schema import grant_from_dict, grant_to_dict
    from stiracore.yolo.allowance import ClockReading

    now = ClockReading(wall=1e9, boot=10.0, mono=10.0)
    flagged = state.new_grant(request(heartbeat_start=True), grant_id="a" * 32, issuer="t", now=now)
    plain = state.new_grant(request(), grant_id="b" * 32, issuer="t", now=now)
    assert (flagged.heartbeat_start, plain.heartbeat_start) == (True, False)
    stored = grant_to_dict(flagged, "g")
    assert stored.get("heartbeat_start") is True
    assert grant_from_dict(stored)[0].heartbeat_start is True
    stored = grant_to_dict(plain, "g")
    assert "heartbeat_start" not in stored  # an unflagged record is as before W6d
    assert grant_from_dict(stored)[0].heartbeat_start is False
    for bad in (False, 1, "true"):
        try:
            grant_from_dict({**stored, "heartbeat_start": bad})  # written only as true
            refused = False
        except GrantRefused:
            refused = True
        assert refused, bad


def test_only_a_flagged_grant_carries_the_key_in_views(tmp_path):
    env = tick_env(tmp_path)
    assert env.service.view(env.grant_id).get("heartbeat_start") is True
    env.service.revoke(env.grant_id)
    _issue_flagged(env, flag=False)
    assert "heartbeat_start" not in env.service.view(env.grant_id)


def test_the_flag_cannot_change_after_issue(tmp_path):
    """No route or service call sets it; a resume keeps it as issued."""
    env = tick_env(tmp_path, flag=False)
    env.service.pause(env.grant_id)
    confirm_and_resume(env.service, env.grant_id, {})
    assert grant_of(env).heartbeat_start is False
    assert fire(env) == "not_enabled"
    import inspect

    from stiracore.grants import service, state

    # The record's flag is set in one place only: a new grant, from its issue request.
    assert inspect.getsource(state).count("heartbeat_start=") == 1
    assert "heartbeat_start=request.heartbeat_start" in inspect.getsource(state.new_grant)
    assert "heartbeat_start=" not in inspect.getsource(service)


# --- the admitted path --------------------------------------------------------------------------


def test_a_tick_starts_one_iteration_through_the_same_step(tmp_path):
    env = scripted(tick_env(tmp_path), w(1), text_only())
    assert fire_and_wait(env) == "started"
    view = env.executor.view(env.grant_id)
    assert view.get("origin") == "tick" and view["number"] == 1
    assert (view.get("tokens_in"), view.get("tokens_out")) == (14, 6)  # two calls, 7 + 3 each
    assert "cache_read" not in view  # not reported by this provider
    assert grant_of(env).iterations == 1
    tick = env.ticker.view(env.grant_id)
    assert tick["started"] == 1 and tick["last"] == "started"
    assert tick["costs"][0]["model_calls"] == 2 and tick["costs"][0]["tokens_in"] == 14
    assert "wall_ms" in tick["costs"][0]
    kinds = [(e["subject_kind"], e["reason_code"]) for e in events(env.recorder)]
    assert ("grant_tick", "tick_started") in kinds
    assert MARKER not in json.dumps(events(env.recorder)) + json.dumps(tick)


def test_a_press_is_unchanged_by_w6d(tmp_path):
    env = scripted(tick_env(tmp_path), w(1), text_only())
    env.executor.step(env.grant_id)
    assert env.executor.wait_idle(20)
    view = env.executor.view(env.grant_id)
    assert "origin" not in view and "tokens_in" not in view
    assert "tick_costs" not in env.executor.board(env.grant_id)
    assert env.ticker.view(env.grant_id)["costs"] == []


def test_cache_counters_are_kept_when_the_provider_reports_them(tmp_path):
    env = scripted(tick_env(tmp_path), text_only())

    class Cached(ScriptedProvider):
        async def stream(self, messages, tool_specs=None, system_prompt=None, **kwargs):
            async for event in super().stream(messages, tool_specs, system_prompt, **kwargs):
                if "metadata" in event:
                    event["metadata"]["usage"].update(
                        cacheReadInputTokens=40, cacheWriteInputTokens=5
                    )
                yield event

    env.provider = Cached([text_only()])
    assert fire_and_wait(env) == "started"
    cost = env.ticker.view(env.grant_id)["costs"][-1]
    assert (cost.get("cache_read"), cost.get("cache_write")) == (40, 5)
    assert env.executor.board(env.grant_id)["tick_costs"][-1].get("cache_read") == 40


def test_one_iteration_per_wake_and_the_minimum_spacing(tmp_path):
    env = scripted(tick_env(tmp_path), w(1), text_only(), w(2), text_only())
    assert fire(env) == "started"
    assert fire(env) == "iteration_running"  # never a second one in the same window
    assert env.executor.wait_idle(20)
    assert fire(env) == "min_spacing"
    env.mono[0] += 299
    assert fire(env) == "min_spacing"
    env.mono[0] += 1
    assert fire_and_wait(env) == "started"
    counts = env.ticker.view(env.grant_id)["skipped"]
    assert counts == {"iteration_running": 1, "min_spacing": 2}


def test_an_unreadable_end_time_is_a_skip(tmp_path):
    env = scripted(tick_env(tmp_path), text_only())
    env.executor._ends.append((env.grant_id, "press", "done", float("nan"), 0))
    assert fire(env) == "min_spacing"


def test_an_approval_is_never_waited_for(tmp_path):
    env = scripted(tick_env(tmp_path, rules={"editor": "ask"}), w(1), text_only())
    assert fire_and_wait(env) == "started"
    view = env.executor.view(env.grant_id)
    assert view["end"] == "approval_timeout" and view["card"] == "none"
    assert env.broker.requests == []  # no card, no self-approval
    record = grant_of(env)
    assert (record.goal_status, record.goal_reason) == ("blocked", "approval_timeout")
    assert fire(env) == "grant_state"


def test_a_persons_turn_during_a_tick_started_iteration_yields_it(tmp_path):
    from stiracore.core.turn_lock import TurnLock

    env = scripted(tick_env(tmp_path), w(1), w(2), text_only())
    # Only the TurnLock's own pre-emption signal tells the iteration (no lock read wired).
    lock = TurnLock(threading.Lock(), env.executor.preempt_signal)
    taken = []

    def on_call(n):
        if n == 2:
            taken.append(lock.acquire())

    env.provider.on_call = on_call
    assert fire_and_wait(env) == "started"
    assert env.executor.view(env.grant_id)["end"] == "yielded_user_turn"
    assert taken == [True]
    env.executor._turn_busy = lock.locked
    assert fire(env) == "turn_busy"  # the holder still has it
    lock.release()
    env.mono[0] += 300
    assert fire(env) != "turn_busy"


def test_a_waiting_holder_or_a_held_lock_skips(tmp_path):
    env = tick_env(tmp_path)
    env.busy = True
    assert fire(env) == "turn_busy"
    env.busy = False
    with env.executor.preempt_signal.arriving():
        assert fire(env) == "turn_busy"
    assert env.provider.calls == 0


# --- every skip reason without touching a model ------------------------------------------------


class Untouchable:
    """A model / parent that raises if anything reads it."""

    def __getattr__(self, name):
        raise AssertionError(f"model touched: {name}")


def _untouchable(env):
    env.touched = []

    def parent():
        env.touched.append("parent")
        raise AssertionError("parent touched")

    def inner(_parent):
        env.touched.append("model")
        return Untouchable()

    env.executor._parent = parent
    env.executor._inner_model = inner


def _end(env, *rows):
    for origin, end in rows:
        env.executor._ends.append((env.grant_id, origin, end, env.mono[0], 0))


SETUPS = {
    "not_enabled": lambda env: setattr(env.ticker, "_settings", AutonomySettings(stage_c=True)),
    "executor_unavailable": lambda env: setattr(env.ticker, "_executor", None),
    "kill": lambda env: setattr(env.authority, "_killed", True),
    "store_unhealthy": lambda env: setattr(env.service, "_unhealthy", "grant_sync_failed"),
    "no_grant": lambda env: env.service.revoke(env.grant_id),
    "grant_state": lambda env: env.service.pause(env.grant_id),
    "awaiting_verification": lambda env: env.service.claim_complete(env.grant_id),
    "breaker": lambda env: setattr(env.breaker, "level", "warning"),
    "heartbeat_unhealthy": lambda env: setattr(env.healthy, "value", False),
    "clock": lambda env: setattr(env.clocks, "broken", True),
    "max_iterations": lambda env: env.service._apply(
        lambda g: dataclasses.replace(g, iterations=g.max_iterations), [env.grant_id]
    ),
    "budget": lambda env: env.service._apply(
        lambda g: dataclasses.replace(g, used_tokens=g.total_tokens), [env.grant_id]
    ),
    "turn_busy": lambda env: setattr(env, "busy", True),
    "iteration_running": lambda env: setattr(env.executor, "_starting", True),
    "abandoned_call": lambda env: env.executor._abandoned.append(_Alive()),
    "min_spacing": lambda env: _end(env, ("press", "done")),
    "no_action": lambda env: _end(env, ("tick", "no_action"), ("tick", "no_action")),
}


class _Alive:
    def is_alive(self):
        return True


@pytest.mark.parametrize("reason", sorted(SETUPS))
def test_every_skip_reason_is_recorded_without_touching_a_model(tmp_path, monkeypatch, reason):
    env = tick_env(tmp_path)
    _untouchable(env)
    SETUPS[reason](env)
    if reason == "no_action":
        env.mono[0] += 10_000
    steps = []
    monkeypatch.setattr(env.executor, "step", lambda *a, **k: steps.append(a))
    assert fire(env) == reason
    assert steps == []  # nothing was pressed
    assert env.provider.calls == 0 and env.touched == []
    if reason in ("not_enabled", "executor_unavailable", "kill", "store_unhealthy", "no_grant"):
        # a server-wide reason: decided before any grant is read
        assert env.ticker.status()["no_grant"] == {reason: 1}
    counts = env.ticker.view(env.grant_id)["skipped"] if reason != "no_grant" else {}
    if reason not in ("no_grant", "not_enabled", "executor_unavailable", "kill", "store_unhealthy"):
        assert counts == {reason: 1}


def test_the_ttl_clause_on_either_clock(tmp_path):
    env = tick_env(tmp_path)
    record = grant_of(env)
    now = env.service.clock_reading()
    reading = type(now)(wall=now.wall, boot=now.boot, mono=record.issued_mono + record.ttl_sec)
    env.service._clock = lambda: reading
    assert env.ticker._grant_clauses(record, env.healthy) == "ttl"
    reading = type(now)(wall=record.issued_wall + record.ttl_sec, boot=now.boot, mono=now.mono)
    env.service._clock = lambda: reading
    assert env.ticker._grant_clauses(record, env.healthy) == "ttl"


def test_the_admitted_path_reaches_step_without_touching_a_model(tmp_path, monkeypatch):
    env = tick_env(tmp_path)
    _untouchable(env)
    seen = []

    def step(grant_id, *, tick):
        seen.append((grant_id, tick.recheck(grant_of(env))))
        return {}

    monkeypatch.setattr(env.executor, "step", step)
    assert fire(env) == "started"
    assert seen == [(env.grant_id, None)] and env.touched == []
    assert env.healthy.reads == 3  # at the wake, then on step's look (its clauses and fresh)


def test_the_tick_never_builds_a_model_or_a_client(tmp_path, monkeypatch):
    """The ticker module imports nothing that builds a model; its source names none."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(grant_tick))
    names = {
        n.module if isinstance(n, ast.ImportFrom) else a.name
        for n in ast.walk(tree)
        if isinstance(n, ast.Import | ast.ImportFrom)
        for a in (n.names if isinstance(n, ast.Import) else [None])
    }
    assert names <= {
        "__future__",
        "logging",
        "math",
        "threading",
        "time",
        "collections",
        "collections.abc",
        "typing",
        "stiracore.grants",
        "stiracore.grants.schema",
        "stiracore.authority.lifecycle_events",
    }, names
    source = inspect.getsource(grant_tick)
    for word in ("_parent", "_inner_model", "BedrockModel", "boto3", "Agent("):
        assert word not in source


# --- re-check inside step, races --------------------------------------------------------------


@pytest.mark.parametrize("race", ["kill", "revoke", "pause_resume", "breaker", "new", "ttl"])
def test_a_change_between_the_wake_and_step_yields_without_an_effect(tmp_path, race):
    env = scripted(tick_env(tmp_path), w(1), text_only())
    real = env.service.tick_record

    def raced():
        record = real()
        if race == "kill":
            env.authority.kill()
        elif race == "revoke":
            env.service.revoke(env.grant_id)
        elif race == "pause_resume":
            env.service.pause(env.grant_id)
            confirm_and_resume(env.service, env.grant_id, {})
        elif race == "breaker":
            env.breaker.level = "critical"
        elif race == "new":
            env.service.session_reset()
        elif race == "ttl":
            env.clocks.tick(record.ttl_sec + 1)
        return record

    env.service.tick_record = raced
    code = fire(env)
    assert code != "started", code
    assert env.executor.wait_idle(5)
    assert env.provider.calls == 0 and grant_of(env).iterations == 0


def test_the_recheck_inside_step_refuses_a_heartbeat_gone_meanwhile(tmp_path):
    """…before the admission reads the provider (a skipped tick never touches a model)."""
    env = scripted(tick_env(tmp_path), w(1))
    _untouchable(env)
    reads = []

    def healthy():
        reads.append(1)
        return len(reads) == 1  # healthy at the wake only

    assert env.ticker.fire(heartbeat_healthy=healthy, run_token=env.token) == "heartbeat_unhealthy"
    assert env.provider.calls == 0 and env.touched == []


# --- the no-action gate ------------------------------------------------------------------------


def test_two_no_action_ends_stop_the_ticks_until_a_person_presses(tmp_path):
    env = scripted(
        tick_env(tmp_path, spacing=30), w(1), text_only(), text_only(), text_only(), w(2)
    )
    assert fire_and_wait(env) == "started"  # writes note1, then ends
    env.mono[0] += 31
    assert fire_and_wait(env) == "started"  # no tool call: no_action
    # Someone edits the grant's file: the next no-action end is a new outcome for W6a
    # (progress, no block) — only the gate stops the ticks.
    (env.sub / "note1.md").write_text("changed by a person", encoding="utf-8")
    env.mono[0] += 31
    assert fire_and_wait(env) == "started"
    ends = [e[1] for e in env.executor.tick_facts(env.grant_id)["ends"]]
    assert ends[-2:] == ["no_action", "no_action"], ends
    assert grant_of(env).goal_status == "active"
    env.mono[0] += 31
    assert fire(env) == "no_action"
    env.mono[0] += 10_000
    assert fire(env) == "no_action"  # time alone never clears it
    env.executor.step(env.grant_id)  # a person's press
    assert env.executor.wait_idle(20)
    env.mono[0] += 31
    assert fire_and_wait(env) == "started"


def test_the_gate_clears_on_a_resume(tmp_path):
    env = tick_env(tmp_path)
    _end(env, ("tick", "no_action"), ("tick", "no_action"))
    env.mono[0] += 1000
    assert fire(env) == "no_action"
    env.service.pause(env.grant_id)
    confirm_and_resume(env.service, env.grant_id, {})
    assert env.ticker._history_clauses(grant_of(env), env.executor.tick_facts(env.grant_id)) is None


@pytest.mark.parametrize(
    ("rows", "streak"),
    [
        ([("tick", "no_action")], 1),
        ([("tick", "no_action"), ("tick", "no_action")], 2),
        ([("tick", "done"), ("tick", "no_action")], 1),
        ([("press", "no_action"), ("tick", "no_action")], 2),
        ([("tick", "no_action"), ("press", "no_action")], 1),
        ([("tick", "no_action"), ("press", "done")], 0),
    ],
)
def test_the_no_action_streak(tmp_path, rows, streak):
    env = tick_env(tmp_path)
    record = grant_of(env)
    ends = [(o, e, 0.0, record.stop_epoch) for o, e in rows]
    assert GrantTicker._no_action_streak(record, ends) == streak
    moved = [(o, e, 0.0, record.stop_epoch + 1) for o, e in rows]
    assert GrantTicker._no_action_streak(record, moved) == 0


# --- the flood guard ------------------------------------------------------------------------------


def test_the_flood_guard_applies_to_event_wakes_only(tmp_path):
    entries = [e for n in range(1, 9) for e in (w(n), text_only())]
    env = scripted(tick_env(tmp_path, spacing=30), *entries)
    env.ticker = GrantTicker(
        env.service,
        env.executor,
        settings=env.settings,
        authority=env.authority,
        flood_starts=2,
        flood_window_sec=100,
    )
    assert fire_and_wait(env, "event") == "started"
    env.mono[0] += 31
    assert fire_and_wait(env, "event") == "started"
    env.mono[0] += 31
    assert fire(env, "event") == "flood"
    assert fire_and_wait(env, "periodic") == "started"  # the periodic path has none
    env.mono[0] += 31
    assert fire(env, "event") == "flood"  # the periodic start counts toward the window
    env.mono[0] += 100
    assert fire_and_wait(env, "event") == "started"  # the window has passed


def test_the_default_flood_guard():
    assert (grant_tick.FLOOD_STARTS, grant_tick.FLOOD_WINDOW_SEC) == (5, 60.0)


# --- start-up / recovery ----------------------------------------------------------------------


def test_the_first_tick_after_a_restart_does_nothing(tmp_path):
    from grants_helpers import make_service

    env = tick_env(tmp_path)
    assert grant_of(env).status == "active"
    env.service.stop()
    service, authority, recorder, *_ = make_service(tmp_path)
    service.settings = env.settings
    assert service.unhealthy_reason is None
    record = service._grants.get(env.grant_id)
    assert record is not None and (record.status, record.reason) == ("revoked", "restart")
    assert record.heartbeat_start is True  # kept as issued, but never revived
    ticker = GrantTicker(
        service, env.executor, settings=env.settings, authority=authority, recorder=recorder
    )
    assert ticker.fire(heartbeat_healthy=lambda: True, run_token=RunToken()) == "no_grant"
    assert env.provider.calls == 0


def test_a_long_gap_is_one_wake_no_catch_up(tmp_path):
    env = scripted(tick_env(tmp_path), w(1), text_only(), w(2), text_only())
    env.clocks.sleep(3000)  # the host slept; the executor's mono clock stood still
    env.mono[0] += 0
    assert fire(env) == "started"
    for _ in range(5):
        assert fire(env) in ("iteration_running", "min_spacing")
    assert env.executor.wait_idle(20)
    assert grant_of(env).iterations == 1


def test_the_ticker_starts_no_thread_and_keeps_nothing_on_disk(tmp_path):
    env = tick_env(tmp_path)
    env.healthy.value = False
    before = {t.ident for t in threading.enumerate()}
    files = sorted(p.name for p in (tmp_path / "data").iterdir())
    for _ in range(50):
        fire(env)
    assert {t.ident for t in threading.enumerate()} <= before
    assert sorted(p.name for p in (tmp_path / "data").iterdir()) == files


# --- records ------------------------------------------------------------------------------------


def _tick_events(env):
    return [
        e["reason_code"].removeprefix("tick_skipped_")
        for e in events(env.recorder)
        if e["subject_kind"] == "grant_tick"
    ]


def test_state_class_skips_post_one_event_each_transient_ones_none(tmp_path):
    env = tick_env(tmp_path)
    env.healthy.value = False
    for _ in range(5):
        fire(env)
    env.healthy.value = True
    env.busy = True
    for _ in range(5):
        fire(env)
    ticks = [e for e in events(env.recorder) if e["subject_kind"] == "grant_tick"]
    assert [(e["from"], e["to"], e["reason_code"]) for e in ticks] == [
        ("active", "paused", "tick_skipped_heartbeat_unhealthy")
    ]
    assert env.ticker.view(env.grant_id)["skipped"] == {"heartbeat_unhealthy": 5, "turn_busy": 5}


def test_a_reason_is_posted_again_only_after_a_start_or_another_reason(tmp_path):
    """Decision (4): a transient skip in between is not enough; another state-class reason
    or a heartbeat start is."""
    env = scripted(tick_env(tmp_path), w(1), text_only())
    env.healthy.value = False
    fire(env)
    env.healthy.value, env.busy = True, True
    fire(env)  # turn_busy: transient
    env.healthy.value, env.busy = False, False
    fire(env)  # the same reason again: not posted
    assert _tick_events(env) == ["heartbeat_unhealthy"]
    env.healthy.value = True
    env.breaker.level = "warning"
    fire(env)  # another state-class reason
    env.breaker.level = "normal"
    env.healthy.value = False
    fire(env)  # now posted again
    assert _tick_events(env) == ["heartbeat_unhealthy", "breaker", "heartbeat_unhealthy"]
    env.healthy.value = True
    assert fire_and_wait(env) == "started"
    env.healthy.value = False
    fire(env)  # after a start: posted again
    assert _tick_events(env)[-3:] == ["heartbeat_unhealthy", "tick_started", "heartbeat_unhealthy"]


def test_events_per_grant_are_capped(tmp_path):
    env = tick_env(tmp_path)
    for n in range(grant_tick.EVENTS_PER_GRANT * 2):
        # heartbeat_unhealthy and clock alternate: each is "another reason" for the other
        env.healthy.value = n % 2 == 1
        env.clocks.broken = n % 2 == 1
        fire(env)
    assert len(_tick_events(env)) == grant_tick.EVENTS_PER_GRANT


def test_the_first_tick_waits_the_spacing_after_the_issue(tmp_path):
    """Decision (3): a person who just issued may still be editing."""
    env = scripted(tick_env(tmp_path, fresh=True), w(1), text_only())
    assert fire(env) == "min_spacing"
    env.clocks.tick(299)
    assert fire(env) == "min_spacing"
    env.clocks.tick(1)
    assert fire_and_wait(env) == "started"


def test_an_unavailable_heartbeat_is_shown_on_a_flagged_grant(tmp_path):
    env = tick_env(tmp_path)
    assert "unavailable" not in env.ticker.view(env.grant_id)
    env.ticker = GrantTicker(
        env.service,
        env.executor,
        settings=env.settings,
        authority=env.authority,
        unavailable="heartbeat_off",
    )
    assert env.ticker.view(env.grant_id).get("unavailable") == "heartbeat_off"
    assert env.ticker.board(env.grant_id).get("unavailable") == "heartbeat_off"
    odd = GrantTicker(
        env.service, env.executor, settings=env.settings, authority=env.authority, unavailable="x"
    )
    assert "unavailable" not in odd.view(env.grant_id)


def test_the_event_codes_are_in_step_with_the_lifecycle_contract():
    from stiracore.authority import lifecycle_events

    assert grant_tick.EVENT_REASONS == lifecycle_events.TICK_REASONS
    assert grant_tick.STATE_SKIPS <= set(grant_tick.SKIP_REASONS)
    assert set(grant_tick._REFUSALS.values()) <= set(grant_tick.SKIP_REASONS)


def test_counters_are_bounded_and_codes_only(tmp_path):
    env = tick_env(tmp_path)
    env.healthy.value = False
    for _ in range(3):
        fire(env)
    for n in range(grant_tick.GRANTS_KEPT + 3):
        env.service.revoke(env.grant_id)
        _issue_flagged(env)
        fire(env)
    assert len(env.ticker._per_grant) == grant_tick.GRANTS_KEPT
    assert all(e["events"] <= grant_tick.EVENTS_PER_GRANT for e in env.ticker._per_grant.values())
    text = json.dumps(env.ticker.view(env.grant_id)) + json.dumps(env.ticker.status())
    assert MARKER not in text and str(env.sub) not in text


def test_a_failing_read_is_a_skip_never_a_raise(tmp_path):
    env = tick_env(tmp_path)

    def broken():
        raise RuntimeError("store")

    env.service.tick_record = broken
    try:
        code = fire(env)
    except Exception:  # noqa: BLE001 - a raise is the failure under test
        code = "raised"
    assert code == "executor_refused"
    assert env.ticker.status()["no_grant"] == {"executor_refused": 1}


def test_the_board_read_never_waits(tmp_path):
    from stiracore.grants.schema import BoardBusy

    env = tick_env(tmp_path)
    held, done = threading.Event(), threading.Event()

    def holder():
        with env.ticker._lock:
            held.set()
            done.wait(0.1)  # a reader that waited would get the lock after this

    thread = threading.Thread(target=holder)
    thread.start()
    assert held.wait(5)
    try:
        env.ticker.board(env.grant_id)
        busy = False
    except BoardBusy:
        busy = True
    done.set()
    thread.join(5)
    assert busy
    assert env.ticker.board(env.grant_id)["started"] == 0


def test_a_turn_arriving_during_steps_admission_refuses_the_tick(tmp_path):
    """The executor's own check after the admission (W6b's order: the signal, then the
    lock) refuses a heartbeat press exactly like a person's."""
    env = scripted(tick_env(tmp_path), w(1))
    reads = []

    def healthy():
        reads.append(1)
        if len(reads) == 2:  # inside step's look: a person's turn begins to arrive
            env.executor.preempt_signal.arrive()
        return True

    assert env.ticker.fire(heartbeat_healthy=healthy, run_token=env.token) == "turn_busy"
    assert env.executor.wait_idle(5)
    assert env.provider.calls == 0


# --- fix round 1: repeated failures, shutdown, the warning's rate ------------------------------


def test_three_failed_ends_stop_the_ticks_until_a_person_presses(tmp_path):
    """Opus N5: a provider that keeps failing does not burn one iteration per spacing."""
    # (W6a's no-progress rule would block the goal after 3 such ends: set it higher, so
    # that only the gate stops the ticks)
    env = scripted(tick_env(tmp_path, spacing=30, issue={"no_progress_ticks": 10}), w(1))

    def broken(_n):
        raise RuntimeError("throttled")

    env.provider.on_call = broken
    for _ in range(3):
        assert fire_and_wait(env) == "started"
        assert env.executor.view(env.grant_id)["end"] in grant_tick.FAILED_ENDS
        env.mono[0] += 31
    assert grant_of(env).goal_status == "active"  # only the gate stops the ticks
    assert fire(env) == "repeated_failure"
    env.mono[0] += 10_000
    assert fire(env) == "repeated_failure"  # time alone never clears it
    assert "repeated_failure" in _tick_events(env)
    env.provider.on_call = None
    env.executor.step(env.grant_id)  # a person's press
    assert env.executor.wait_idle(20)
    env.mono[0] += 31
    assert fire(env) != "repeated_failure"


@pytest.mark.parametrize(
    ("rows", "streak"),
    [
        ([("tick", "failed")] * 3, 3),
        ([("tick", "failed"), ("tick", "model_error"), ("tick", "timeout")], 3),
        ([("tick", "failed"), ("press", "failed"), ("tick", "failed")], 2),
        ([("tick", "failed"), ("tick", "done"), ("tick", "failed")], 1),
    ],
)
def test_the_failure_streak(tmp_path, rows, streak):
    env = tick_env(tmp_path)
    record = grant_of(env)
    ends = [(o, e, 0.0, record.stop_epoch) for o, e in rows]
    assert GrantTicker._streak(record, ends, grant_tick.FAILED_ENDS) == streak
    moved = [(o, e, 0.0, record.stop_epoch + 1) for o, e in rows]
    assert GrantTicker._streak(record, moved, grant_tick.FAILED_ENDS) == 0


def test_a_shutdown_posts_no_skip_event(tmp_path):
    """Opus N7: the executor stops before the scheduler — that is no stop notice."""
    env = tick_env(tmp_path)
    env.executor.stop(1)
    assert fire(env) == "shutdown"
    assert _tick_events(env) == []


def test_the_evaluation_warning_is_rate_limited(tmp_path, caplog):
    env = tick_env(tmp_path)

    def broken():
        raise RuntimeError("store")

    env.service.tick_record = broken
    with caplog.at_level("WARNING"):
        for _ in range(5):
            assert fire(env) == "executor_refused"
    lines = [r for r in caplog.records if "could not be evaluated" in r.getMessage()]
    assert len(lines) == 1 and "RuntimeError" in lines[0].getMessage()
    assert "store" not in lines[0].getMessage()  # the class name, never the message


def test_no_run_token_is_no_wake(tmp_path):
    env = tick_env(tmp_path)
    assert env.ticker.fire(heartbeat_healthy=env.healthy) == "scheduler_stopped"
    assert env.ticker.status()["no_grant"] == {"scheduler_stopped": 1}  # before any grant read
    env.token.valid = False
    assert fire(env) == "scheduler_stopped"
    assert env.provider.calls == 0
