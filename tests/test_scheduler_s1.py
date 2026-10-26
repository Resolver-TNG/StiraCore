"""ADR 0021 phase S1 (#239): schema v2, catch_up refused for model-calling jobs, the
heartbeat requirement, the Agent-layer pool and the new notifications.

S1 only refuses or records more: every test here either checks a refusal, a record, or that
an existing behaviour (version 1 definitions, Gateway jobs, model-free kinds, a scheduler
without a heartbeat) is unchanged.
"""

from __future__ import annotations

import pytest
from test_scheduler import HUMAN, T0, Breaker, Clock, _events, _job, _make, _tick_at

from stiracore.scheduler.jobs import (
    SCHEMA_VERSION,
    JobError,
    parse_job,
)
from stiracore.scheduler.scheduler import (
    AGENT_DAILY_TOKEN_BUDGET,
    RunOutcome,
    Scheduler,
    SchedulerRefused,
    _heartbeat_health,
)

ALL_MEMORY = frozenset({"memory_rate_backfill", "memory_consolidate", "memory_tidy"})
HEALTHY = {"enabled": True, "running": True, "degraded": False, "interval_sec": 60.0}


class Heartbeat:
    """A heartbeat status whose last tick follows the clock unless told otherwise."""

    def __init__(self, clock: Clock, **status) -> None:
        self.clock = clock
        self.status = {**HEALTHY, **status}
        self.fresh = True
        self.calls = 0

    def __call__(self) -> dict:
        self.calls += 1
        status = dict(self.status)
        if self.fresh and "last_tick_at" not in status:
            status["last_tick_at"] = self.clock.now
        return status


def _parse(data, **kw):
    kw.setdefault("now", T0)
    kw.setdefault("gateway_actions", {"probe"})
    kw.setdefault("agent_jobs", True)
    kw.setdefault("memory_kinds", ALL_MEMORY)
    return parse_job(data, **kw)


AGENT = {
    "job_id": "digest",
    "name": "digest",
    "layer": "agent",
    "prompt": "summarize today",
    "tools": ["current_time"],
    "schedule": {"type": "cron", "expr": "0 * * * *"},
    "budget": {"max_tokens_per_run": 500, "max_tokens_per_day": 2000},
}


def _memory(kind="memory_rate_backfill", **extra):
    return {
        "job_id": kind.replace("_", "-"),
        "name": kind,
        "layer": "memory",
        "action": kind,
        "schedule": {"type": "interval", "every_sec": 600, "anchor": T0},
        **extra,
    }


def _memory_scheduler(tmp_path, clock, *, health_source=None, start=True, ran=None, **kw):
    ran = ran if ran is not None else []

    def executor(ctx):
        ran.append(ctx.job.job_id)
        return RunOutcome("completed", tokens=10 if ctx.job.action != "memory_tidy" else 0)

    scheduler = Scheduler(
        tmp_path,
        now_fn=clock,
        breaker_level=Breaker(),
        dispatch=kw.pop("dispatch", lambda work: work()),
        gateway_actions={"probe": lambda ctx: "ok"},
        memory_executor=executor,
        memory_kinds=ALL_MEMORY,
        memory_daily_token_budget=100_000,
        health_source=health_source,
        **kw,
    )
    if start:
        scheduler.start(background=False)
    return scheduler, ran


def _skips(scheduler, reason):
    return [e for e in _events(scheduler, "run_skipped") if e["body"]["reason"] == reason]


# --- §2 schema version 2 --------------------------------------------------------------------


def test_version_1_definitions_stay_valid_and_mean_the_same_thing():
    v1 = _parse({**_job(), "schema_version": 1})
    assert v1.schema_version == 1
    data = v1.to_dict()
    # A version 1 definition is written back without the version 2 fields.
    assert not {"unattended_approval", "approval_wait_max_sec", "requires_heartbeat"} & set(data)
    again = _parse(data, stored=True)
    assert again == v1
    agent_v1 = _parse({**AGENT, "schema_version": 1})
    assert agent_v1.needs_heartbeat and agent_v1.unattended_approval == "deny"


def test_new_definitions_are_version_2_and_round_trip():
    assert SCHEMA_VERSION == 2
    gateway = _parse(_job())
    assert gateway.schema_version == 2
    assert gateway.to_dict()["unattended_approval"] is None
    agent = _parse(AGENT)
    data = agent.to_dict()
    assert (data["unattended_approval"], data["approval_wait_max_sec"]) == ("deny", None)
    assert data["requires_heartbeat"] is None
    assert _parse(data, stored=True) == agent


@pytest.mark.parametrize(
    "field,value",
    [("unattended_approval", "deny"), ("requires_heartbeat", True), ("approval_wait_max_sec", 60)],
)
def test_version_2_fields_need_schema_version_2(field, value):
    with pytest.raises(JobError, match="schema_version 2") as error:
        _parse({**AGENT, "schema_version": 1, field: value})
    assert error.value.code == "invalid"


@pytest.mark.parametrize("version", [0, 3, "2", True, None])
def test_unknown_schema_versions_are_refused(version):
    with pytest.raises(JobError, match="schema_version"):
        _parse({**_job(), "schema_version": version})


def test_unattended_wait_parses_but_needs_a_wired_approval_store(monkeypatch, tmp_path):
    # ADR 0024 phase 2: the definition is accepted; a scheduler without a durable approval
    # store (``approval_waits``) still refuses it (scope), whatever the environment says.
    for name in ("STIRA_ENABLE_EXEC_TOOLS", "BYPASS_TOOL_CONSENT", "STIRA_HITL_AUTO_APPROVE"):
        monkeypatch.setenv(name, "1")
    assert _parse({**AGENT, "unattended_approval": "wait"}).approval_wait_max_sec == 3600
    assert _parse({**AGENT, "unattended_approval": "deny"}).unattended_approval == "deny"
    from stiracore.scheduler.scheduler import Scheduler, SchedulerRefused

    scheduler = Scheduler(tmp_path, agent_executor=lambda ctx: None)
    scheduler.start(background=False)
    try:
        with pytest.raises(SchedulerRefused) as error:
            scheduler.create_job(
                {**AGENT, "unattended_approval": "wait"}, by="local:operator", confirm=True
            )
        assert error.value.code == "scope"
    finally:
        scheduler.stop()


@pytest.mark.parametrize(
    "extra",
    [
        {"unattended_approval": "ask"},
        {"unattended_approval": 1},
        {"approval_wait_max_sec": 600},  # only with wait
        {"unattended_approval": "deny", "approval_wait_max_sec": 600},
        {"requires_heartbeat": "yes"},
        {"requires_heartbeat": 1},
    ],
)
def test_invalid_version_2_values_are_refused(extra):
    with pytest.raises(JobError) as error:
        _parse({**AGENT, **extra})
    assert error.value.code == "invalid"


@pytest.mark.parametrize("data", [_job(), _memory("memory_tidy"), _memory()])
def test_approval_fields_apply_to_agent_jobs_only(data):
    for extra in ({"unattended_approval": "deny"}, {"approval_wait_max_sec": 600}):
        with pytest.raises(JobError, match="agent jobs"):
            _parse({**data, **extra})


def test_a_stored_wait_definition_replays_and_keeps_its_limits():
    # Written by a server with ADR 0024 phase 2; replayed here it is not dropped.
    stored = _parse(
        {**AGENT, "unattended_approval": "wait", "approval_wait_max_sec": 600}, stored=True
    )
    assert (stored.unattended_approval, stored.approval_wait_max_sec) == ("wait", 600)
    default = _parse({**AGENT, "unattended_approval": "wait"}, stored=True)
    assert default.approval_wait_max_sec == 3600
    with pytest.raises(JobError):
        _parse({**AGENT, "unattended_approval": "wait", "approval_wait_max_sec": 59}, stored=True)


def test_requires_heartbeat_defaults_by_layer_and_is_ignored_for_model_free_jobs():
    assert _parse(AGENT).needs_heartbeat
    assert _parse(_memory()).needs_heartbeat
    assert _parse(_memory("memory_consolidate")).needs_heartbeat
    assert not _parse({**AGENT, "requires_heartbeat": False}).needs_heartbeat
    for data in (_job(), _memory("memory_tidy")):
        assert not _parse(data).needs_heartbeat
        assert not _parse({**data, "requires_heartbeat": True}).needs_heartbeat


# --- §2 / Q4 catch_up for model-calling jobs ------------------------------------------------


@pytest.mark.parametrize("data", [AGENT, _memory(), _memory("memory_consolidate")])
def test_catch_up_is_refused_for_model_calling_jobs(data):
    with pytest.raises(JobError, match="catch_up") as error:
        _parse({**data, "missed_policy": "catch_up"})
    assert error.value.code == "scope"
    for policy in ("skip", "latest"):
        assert _parse({**data, "missed_policy": policy}).missed_policy == policy


@pytest.mark.parametrize("data", [_job(), _memory("memory_tidy")])
def test_catch_up_stays_allowed_for_model_free_jobs(data):
    job = _parse({**data, "missed_policy": "catch_up"})
    assert job.effective_missed_policy == "catch_up"


def test_catch_up_is_refused_through_the_scheduler_on_create_and_update(tmp_path):
    clock = Clock()
    scheduler, _ran = _memory_scheduler(tmp_path, clock)
    with pytest.raises(SchedulerRefused) as error:
        scheduler.create_job(_memory(missed_policy="catch_up"), by=HUMAN, confirm=True)
    assert error.value.code == "scope"
    scheduler.create_job(_memory(missed_policy="latest"), by=HUMAN, confirm=True)
    with pytest.raises(SchedulerRefused) as error:
        scheduler.update_job(
            "memory-rate-backfill", {"missed_policy": "catch_up"}, by=HUMAN, confirm=True
        )
    assert error.value.code == "scope"
    scheduler.stop()


def test_a_stored_catch_up_model_job_runs_at_most_the_newest_missed_fire(tmp_path):
    """A definition written before S1 still says catch_up: after an outage it runs once."""
    clock = Clock()
    scheduler, ran = _memory_scheduler(tmp_path, clock)
    old = _parse({**_memory(), "missed_policy": "catch_up", "max_catch_up": 5}, stored=True)
    with scheduler.exclusive():
        scheduler._record(
            "job_created", {"job_id": old.job_id, "job": old.to_dict(), "by": HUMAN}, clock.now
        )
    assert scheduler.jobs()[0]["missed_policy"] == "catch_up"
    _tick_at(scheduler, clock, T0 + 600 * 6 + 300)  # six fires missed, none due
    assert ran == ["memory-rate-backfill"]
    missed = _events(scheduler, "run_missed")[-1]["body"]
    assert (missed["policy"], missed["count"]) == ("latest", 5)
    scheduler.stop()


def test_a_stored_catch_up_gateway_job_keeps_catching_up(tmp_path):
    clock = Clock()
    scheduler, probe = _make(tmp_path, clock)
    scheduler.create_job(_job(missed_policy="catch_up", max_catch_up=3), by=HUMAN, confirm=True)
    _tick_at(scheduler, clock, T0 + 3600 + 120)
    assert len(probe.calls) == 3
    scheduler.stop()


# --- §8 heartbeat requirement ---------------------------------------------------------------


@pytest.mark.parametrize(
    "status,stalled,healthy",
    [
        ({**HEALTHY, "last_tick_at": T0}, False, True),
        ({**HEALTHY, "last_tick_at": T0 - 179}, False, True),  # within 3 x 60 s
        ({**HEALTHY, "last_tick_at": T0 - 181}, True, False),
        ({**HEALTHY, "last_tick_at": T0 - 100, "interval_sec": 10}, False, True),  # 120 s floor
        ({**HEALTHY, "last_tick_at": T0 - 121, "interval_sec": 10}, True, False),
        ({**HEALTHY, "last_tick_at": T0, "running": False}, True, False),
        ({**HEALTHY, "last_tick_at": T0, "degraded": True}, True, False),
        ({**HEALTHY}, False, False),  # never ticked: not stalled, not healthy either
        ({**HEALTHY, "last_tick_at": True}, False, False),
        ({**HEALTHY, "last_tick_at": T0, "interval_sec": "x"}, False, True),
        ({"enabled": False, "running": False}, False, False),
        ({"enabled": "yes", "running": True, "last_tick_at": T0}, False, False),
        ({}, False, False),
    ],
)
def test_heartbeat_health(status, stalled, healthy):
    assert _heartbeat_health(status, T0) == (stalled, healthy)


@pytest.mark.parametrize(
    "status",
    [{"enabled": False, "running": False}, {"running": False}, {"degraded": True}],
)
def test_a_model_calling_fire_without_a_healthy_heartbeat_is_skipped_and_notified_once(
    tmp_path, status
):
    clock = Clock()
    heartbeat = Heartbeat(clock, **status)
    scheduler, ran = _memory_scheduler(tmp_path, clock, health_source=heartbeat)
    scheduler.create_job(_memory(), by=HUMAN, confirm=True)
    _tick_at(scheduler, clock, T0 + 600)
    _tick_at(scheduler, clock, T0 + 1200)
    assert ran == []
    assert [e["body"]["scheduled_at"] for e in _skips(scheduler, "heartbeat")] == [
        T0 + 600,
        T0 + 1200,
    ]
    raised = [n for n in scheduler.notifications() if n["code"] == "heartbeat_required"]
    assert [n["state"] for n in raised] == ["raised"]
    assert raised[0]["job_id"] == "memory-rate-backfill"
    assert "heartbeat_required" in scheduler.status()["alerts"]
    assert scheduler.status()["heartbeat_healthy"] is False
    # Healthy again: the alert resolves and the next fire runs.
    heartbeat.status = dict(HEALTHY)
    _tick_at(scheduler, clock, T0 + 1800)
    assert ran == ["memory-rate-backfill"]
    assert "heartbeat_required" not in scheduler.status()["alerts"]
    states = [n["state"] for n in scheduler.notifications() if n["code"] == "heartbeat_required"]
    assert states == ["resolved", "raised"]
    assert scheduler.status()["heartbeat_healthy"] is True
    scheduler.stop()


def test_an_overdue_heartbeat_blocks_model_calling_runs(tmp_path):
    clock = Clock()
    heartbeat = Heartbeat(clock, last_tick_at=T0)
    scheduler, ran = _memory_scheduler(tmp_path, clock, health_source=heartbeat)
    scheduler.create_job(_memory(), by=HUMAN, confirm=True)
    _tick_at(scheduler, clock, T0 + 600)  # last tick 600 s ago > max(180, 120)
    assert ran == [] and _skips(scheduler, "heartbeat")
    scheduler.stop()


def test_model_free_jobs_and_opted_out_jobs_ignore_the_heartbeat(tmp_path):
    clock = Clock()
    heartbeat = Heartbeat(clock, enabled=False, running=False)
    scheduler, ran = _memory_scheduler(tmp_path, clock, health_source=heartbeat)
    scheduler.create_job(_memory("memory_tidy"), by=HUMAN, confirm=True)
    scheduler.create_job(
        _memory("memory_consolidate", requires_heartbeat=False), by=HUMAN, confirm=True
    )
    scheduler.create_job(
        {"job_id": "gw", "name": "gw", "action": "probe", "schedule": _memory()["schedule"]},
        by=HUMAN,
        confirm=True,
    )
    _tick_at(scheduler, clock, T0 + 600)
    assert sorted(ran) == ["memory-consolidate", "memory-tidy"]
    assert [r["status"] for r in scheduler.runs()] == ["completed"] * 3
    assert not _skips(scheduler, "heartbeat")
    assert "heartbeat_required" not in scheduler.status()["alerts"]
    scheduler.stop()


def test_without_a_heartbeat_source_model_calling_jobs_run_as_before(tmp_path):
    clock = Clock()
    scheduler, ran = _memory_scheduler(tmp_path, clock)
    scheduler.create_job(_memory(), by=HUMAN, confirm=True)
    _tick_at(scheduler, clock, T0 + 600)
    assert ran == ["memory-rate-backfill"]
    assert scheduler.status()["heartbeat_healthy"] is None
    scheduler.stop()


def test_the_heartbeat_is_checked_again_right_before_the_body(tmp_path):
    clock = Clock()
    heartbeat = Heartbeat(clock)
    pending = []
    scheduler, ran = _memory_scheduler(
        tmp_path, clock, health_source=heartbeat, dispatch=pending.append
    )
    scheduler.create_job(_memory(), by=HUMAN, confirm=True)
    _tick_at(scheduler, clock, T0 + 600)
    assert len(pending) == 1
    heartbeat.status["running"] = False  # stops between the start and the body
    pending[0]()
    assert ran == []
    run = scheduler.runs()[0]
    assert (run["status"], run["reason"]) == ("cancelled", "heartbeat")
    scheduler.stop()


def test_an_agent_job_needs_the_heartbeat_too(tmp_path):
    clock = Clock()
    heartbeat = Heartbeat(clock, running=False)
    calls = []
    scheduler = Scheduler(
        tmp_path,
        now_fn=clock,
        breaker_level=Breaker(),
        agent_executor=lambda ctx: calls.append(ctx.job.job_id) or RunOutcome("completed"),
        dispatch=lambda work: work(),
        health_source=heartbeat,
    )
    scheduler.start(background=False)
    scheduler.create_job(AGENT, by=HUMAN, confirm=True)
    _tick_at(scheduler, clock, T0 + 3600)
    assert calls == [] and _skips(scheduler, "heartbeat")
    heartbeat.status["running"] = True
    _tick_at(scheduler, clock, T0 + 7200)
    assert calls == ["digest"]
    scheduler.stop()


# --- §4 / Q3 the Agent-layer pool -----------------------------------------------------------


def _agent_scheduler(tmp_path, clock, **kw):
    calls = []

    def executor(ctx):
        calls.append(ctx.job.job_id)
        return RunOutcome("completed", tokens=ctx.job.budget.max_tokens_per_run)

    scheduler = Scheduler(
        tmp_path,
        now_fn=clock,
        breaker_level=Breaker(),
        agent_executor=executor,
        dispatch=lambda work: work(),
        **kw,
    )
    scheduler.start(background=False)
    return scheduler, calls


def test_agent_pool_defaults_to_50000_and_is_never_unlimited(tmp_path):
    assert AGENT_DAILY_TOKEN_BUDGET == 50_000
    scheduler, _ = _agent_scheduler(tmp_path / "a", Clock())
    assert scheduler.status()["agent_daily_token_budget"] == 50_000
    scheduler.stop()
    for bad in (None, 0, -1, True, 1.5, "50000", 10_000_001):
        with pytest.raises(ValueError):
            Scheduler(tmp_path / "b", agent_daily_token_budget=bad)


def test_agent_jobs_share_one_daily_pool(tmp_path):
    clock = Clock()
    scheduler, calls = _agent_scheduler(tmp_path, clock, agent_daily_token_budget=1000)
    budget = {"max_tokens_per_run": 600, "max_tokens_per_day": 5000}
    for name in ("a", "b"):
        scheduler.create_job(
            {**AGENT, "job_id": name, "name": name, "budget": budget}, by=HUMAN, confirm=True
        )
    _tick_at(scheduler, clock, T0 + 3600)
    assert calls == ["a"]  # 600 used; b's 600 would exceed the pool of 1000
    note = next(n for n in scheduler.notifications() if n["code"] == "budget_exhausted")
    assert note["job_id"] == "b"
    assert note["params"] == {"kind": "agent_daily_token_budget", "limit": 1000}
    paused = {j["job_id"]: j["state"] for j in scheduler.jobs()}
    assert paused == {"a": "active", "b": "paused"}
    scheduler.stop()


def test_the_agent_pool_counts_reservations_of_unsettled_runs(tmp_path):
    clock = Clock()
    pending = []
    scheduler = Scheduler(
        tmp_path,
        now_fn=clock,
        breaker_level=Breaker(),
        agent_executor=lambda ctx: RunOutcome("completed", tokens=1),
        dispatch=pending.append,
        agent_daily_token_budget=1000,
    )
    scheduler.start(background=False)
    budget = {"max_tokens_per_run": 600, "max_tokens_per_day": 5000}
    for name in ("a", "b"):
        scheduler.create_job(
            {**AGENT, "job_id": name, "name": name, "budget": budget}, by=HUMAN, confirm=True
        )
    _tick_at(scheduler, clock, T0 + 3600)
    assert len(pending) == 1  # a holds 600 reserved; b does not fit next to it
    assert [e["body"]["job_id"] for e in _skips(scheduler, "budget")] == ["b"]
    pending[0]()
    scheduler.stop()


def test_memory_jobs_do_not_count_against_the_agent_pool(tmp_path):
    clock = Clock()
    agent_calls = []
    scheduler = Scheduler(
        tmp_path,
        now_fn=clock,
        breaker_level=Breaker(),
        agent_executor=lambda ctx: agent_calls.append(1) or RunOutcome("completed", tokens=400),
        memory_executor=lambda ctx: RunOutcome("completed", tokens=15_000),
        memory_kinds=ALL_MEMORY,
        dispatch=lambda work: work(),
        agent_daily_token_budget=500,
    )
    scheduler.start(background=False)
    scheduler.create_job(_memory(), by=HUMAN, confirm=True)
    scheduler.create_job(
        {**AGENT, "budget": {"max_tokens_per_run": 500, "max_tokens_per_day": 500}},
        by=HUMAN,
        confirm=True,
    )
    _tick_at(scheduler, clock, T0 + 3600)
    assert agent_calls == [1]
    scheduler.stop()


# --- §10 jobs_paused_at_start ---------------------------------------------------------------


def test_one_jobs_paused_at_start_notification_per_start_with_the_count(tmp_path):
    clock = Clock()
    scheduler, _ = _memory_scheduler(tmp_path, clock)
    scheduler.create_job(_memory(), by=HUMAN, confirm=True)
    scheduler.create_job(_memory("memory_consolidate"), by=HUMAN, confirm=True)
    scheduler.create_job(_memory("memory_tidy"), by=HUMAN, confirm=True)
    assert not [n for n in scheduler.notifications() if n["code"] == "jobs_paused_at_start"]
    scheduler.stop()

    restarted, _ = _memory_scheduler(tmp_path, clock)
    notes = [n for n in restarted.notifications() if n["code"] == "jobs_paused_at_start"]
    assert len(notes) == 1
    assert (notes[0]["params"], notes[0]["severity"]) == ({"count": 2}, "info")
    states = {j["job_id"]: j["state"] for j in restarted.jobs()}
    assert states == {
        "memory-rate-backfill": "paused",
        "memory-consolidate": "paused",
        "memory-tidy": "active",
    }
    restarted.stop()

    # Nothing newly paused: no further notification.
    again, _ = _memory_scheduler(tmp_path, clock)
    notes = [n for n in again.notifications() if n["code"] == "jobs_paused_at_start"]
    assert len(notes) == 1
    again.stop()


def test_a_gateway_only_restart_raises_no_paused_at_start_notification(tmp_path):
    clock = Clock()
    scheduler, _ = _make(tmp_path, clock)
    scheduler.create_job(_job(), by=HUMAN, confirm=True)
    scheduler.stop()
    restarted, _ = _make(tmp_path, clock)
    assert not [n for n in restarted.notifications() if n["code"] == "jobs_paused_at_start"]
    assert restarted.jobs()[0]["state"] == "active"
    restarted.stop()
