"""Scheduler core (#239, ADR 0008): fake clock, missed runs, overlap, budget, breaker,
approval fail-closed, chained log and restart consistency."""

from __future__ import annotations

import json
import threading
from datetime import UTC

import pytest

from stiracore.scheduler import (
    JOB_READ_ONLY_TOOLS,
    CronExpression,
    JobError,
    RunOutcome,
    Scheduler,
    SchedulerLockError,
    SchedulerLogError,
    SchedulerRefused,
    parse_job,
)
from stiracore.scheduler.event_log import SchedulerEventLog
from stiracore.scheduler.schedule import ScheduleError, parse_schedule

T0 = 1_800_000_000.0  # 2027-01-15 08:00:00 UTC, a minute boundary
HUMAN = "local:operator"


class Clock:
    def __init__(self, now: float = T0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class Breaker:
    def __init__(self, level: str = "normal") -> None:
        self.level = level

    def __call__(self) -> str:
        if isinstance(self.level, Exception):
            raise self.level
        return self.level


class Probe:
    """A Gateway action that records each call; ``block`` keeps a run active."""

    def __init__(self) -> None:
        self.calls: list[float] = []
        self.fail = False

    def __call__(self, ctx) -> str:
        self.calls.append(ctx.scheduled_at)
        if self.fail:
            raise RuntimeError("probe failed")
        return f"probe at {ctx.scheduled_at:.0f}"


def _job(**overrides):
    data = {
        "job_id": "probe",
        "name": "probe",
        "action": "probe",
        "schedule": {"type": "cron", "expr": "*/5 * * * *"},
    }
    data.update(overrides)
    return data


def _make(tmp_path, clock, *, probe=None, breaker=None, start=True, **kwargs):
    probe = probe or Probe()
    kwargs.setdefault("dispatch", lambda work: work())
    scheduler = Scheduler(
        tmp_path,
        now_fn=clock,
        breaker_level=breaker or Breaker(),
        gateway_actions={"probe": probe, **kwargs.pop("actions", {})},
        **kwargs,
    )
    if start:
        scheduler.start(background=False)
    return scheduler, probe


def _events(scheduler, kind=None):
    events = scheduler.log.events()
    return [e for e in events if kind is None or e["kind"] == kind]


def _tick_at(scheduler, clock, ts):
    clock.now = ts
    scheduler.tick()


# --- cron / schedule parsing --------------------------------------------------------


def test_cron_fields_steps_ranges_and_dom_dow_or():
    expr = CronExpression.parse("*/15 9-10 * * *")
    assert expr.next_after(T0, __import__("datetime").timezone.utc) == T0 + 3600  # 09:00
    # Both day fields restricted: either matches (Vixie cron). 2027-01-15 is a Friday.
    both = parse_schedule({"type": "cron", "expr": "0 0 1 * 1"}, default_anchor=T0)
    fire = both.next_after(T0)
    # Next Monday (2027-01-18) comes before the 1st of February.
    assert fire == T0 + 2 * 86400 + 16 * 3600
    for bad in ("* * * *", "61 * * * *", "*/0 * * * *", "a * * * *", "5-1 * * * *", "0 0 30 2 *"):
        with pytest.raises(ScheduleError):
            parse_schedule({"type": "cron", "expr": bad}, default_anchor=T0)


def test_cron_dst_gap_is_skipped_and_timezone_applies():
    # 2027-03-14 02:30 does not exist in America/New_York (spring forward).
    schedule = parse_schedule(
        {"type": "cron", "expr": "30 2 * * *", "timezone": "America/New_York"},
        default_anchor=T0,
    )
    from datetime import datetime
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("America/New_York")
    before = datetime(2027, 3, 13, 3, 0, tzinfo=tz).timestamp()
    fire = schedule.next_after(before)
    assert datetime.fromtimestamp(fire, tz).strftime("%m-%d %H:%M") == "03-15 02:30"
    tokyo = parse_schedule(
        {"type": "cron", "expr": "0 9 * * *", "timezone": "Asia/Tokyo"}, default_anchor=T0
    )
    # 08:00 UTC = 17:00 JST, so 09:00 JST is the next day at 00:00 UTC.
    assert tokyo.next_after(T0) == T0 + 16 * 3600


def test_interval_and_once_schedules():
    interval = parse_schedule({"type": "interval", "every_sec": 300}, default_anchor=T0)
    assert interval.next_after(T0) == T0 + 300
    assert interval.fires_between(T0, T0 + 3000, 3) == [T0 + 2400, T0 + 2700, T0 + 3000]
    with pytest.raises(ScheduleError):
        parse_schedule({"type": "interval", "every_sec": 10}, default_anchor=T0)
    once = parse_schedule({"type": "once", "at": T0 + 60}, default_anchor=T0)
    assert once.next_after(T0) == T0 + 60 and once.next_after(T0 + 60) is None


def test_job_schema_is_strict_and_tools_are_read_only_regardless_of_env(monkeypatch):
    monkeypatch.setenv("STIRA_ENABLE_EXEC_TOOLS", "1")
    monkeypatch.setenv("BYPASS_TOOL_CONSENT", "true")
    kwargs = {"now": T0, "gateway_actions": {"probe"}, "agent_jobs": True}
    agent = {
        "name": "a",
        "layer": "agent",
        "prompt": "summarize",
        "schedule": {"type": "interval", "every_sec": 3600},
        "budget": {"max_tokens_per_run": 100, "max_tokens_per_day": 1000},
    }
    assert parse_job(agent, **kwargs).tools == ()
    for tool in ("shell", "cron", "file_write", "memory_write", "http_request", "use_agent"):
        with pytest.raises(JobError) as error:
            parse_job({**agent, "tools": [tool]}, **kwargs)
        assert error.value.code == "scope"
    assert JOB_READ_ONLY_TOOLS == {
        "current_time",
        "memory_read",
        "memory_search",
        "memory_list",
        "file_read",
    }
    with pytest.raises(JobError, match="budget"):
        parse_job({**agent, "budget": {}}, **kwargs)
    with pytest.raises(JobError) as error:
        parse_job({**_job(), "state": "active"}, **kwargs)
    assert error.value.code == "scope"
    with pytest.raises(JobError, match="secret"):
        parse_job({**agent, "prompt": "use api_key=sk-abcdefghijklmnopqrstuvwxyz123456"}, **kwargs)
    with pytest.raises(JobError) as error:
        parse_job(agent, now=T0, gateway_actions={"probe"}, agent_jobs=False)
    assert error.value.code == "scope"
    with pytest.raises(JobError) as error:
        parse_job(_job(action="shell"), **kwargs)
    assert error.value.code == "scope"


# --- AC1: cron firing, pause, resume --------------------------------------------------


def test_fires_on_the_cron_schedule_not_while_paused_and_again_after_resume(tmp_path):
    clock = Clock()
    scheduler, probe = _make(tmp_path, clock)
    scheduler.create_job(_job(), by=HUMAN, confirm=True)
    for minute in range(1, 11):  # 08:01 .. 08:10
        _tick_at(scheduler, clock, T0 + minute * 60)
    assert probe.calls == [T0 + 300, T0 + 600]

    scheduler.pause_job("probe", by=HUMAN)
    for minute in range(11, 21):
        _tick_at(scheduler, clock, T0 + minute * 60)
    assert probe.calls == [T0 + 300, T0 + 600]
    assert scheduler.jobs()[0]["next_run_at"] is None

    clock.now = T0 + 21 * 60
    scheduler.resume_job("probe", by=HUMAN, confirm=True)
    for minute in range(21, 31):
        _tick_at(scheduler, clock, T0 + minute * 60)
    # Paused fires (08:15, 08:20) are not replayed; 08:25 and 08:30 run.
    assert probe.calls == [T0 + 300, T0 + 600, T0 + 1500, T0 + 1800]
    assert not _events(scheduler, "run_missed")
    runs = scheduler.runs()
    assert [r["status"] for r in runs] == ["completed"] * 4
    assert runs[0]["summary"] == f"probe at {T0 + 1800:.0f}"
    scheduler.stop()


# --- AC2: missed runs ----------------------------------------------------------------


def _outage(tmp_path, policy, **extra):
    clock = Clock()
    scheduler, probe = _make(tmp_path, clock)
    scheduler.create_job(_job(missed_policy=policy, **extra), by=HUMAN, confirm=True)
    _tick_at(scheduler, clock, T0 + 300)
    assert probe.calls == [T0 + 300]
    scheduler.stop()
    clock.now = T0 + 3600 + 90  # down for ~an hour: 11 fires missed, none due
    restarted, probe2 = _make(tmp_path, clock)
    restarted.tick()
    return restarted, probe2


def test_missed_runs_default_skip_records_and_runs_nothing(tmp_path):
    scheduler, probe = _outage(tmp_path, "skip")
    assert probe.calls == []
    (missed,) = _events(scheduler, "run_missed")
    assert missed["body"]["count"] == 11
    assert (missed["body"]["first"], missed["body"]["last"]) == (T0 + 600, T0 + 3600)
    scheduler.tick()  # idempotent: nothing more recorded
    assert len(_events(scheduler, "run_missed")) == 1
    _tick_at(scheduler, scheduler._now, T0 + 3900)
    assert probe.calls == [T0 + 3900]  # the schedule continues normally
    scheduler.stop()


def test_missed_runs_default_policy_is_skip():
    job = parse_job(_job(), now=T0, gateway_actions={"probe"}, agent_jobs=False)
    assert job.missed_policy == "skip" and job.overlap_policy == "skip"


def test_missed_runs_latest_and_bounded_catch_up(tmp_path):
    scheduler, probe = _outage(tmp_path / "latest", "latest")
    assert probe.calls == [T0 + 3600]
    assert _events(scheduler, "run_missed")[0]["body"]["count"] == 10
    scheduler.stop()

    scheduler, probe = _outage(tmp_path / "catch", "catch_up", max_catch_up=3)
    assert probe.calls == [T0 + 3000, T0 + 3300, T0 + 3600]
    assert _events(scheduler, "run_missed")[0]["body"]["count"] == 8
    scheduler.stop()


# --- AC3: overlap and idempotent delivery --------------------------------------------


def test_overlapping_fire_is_skipped_while_a_run_is_active(tmp_path):
    clock = Clock()
    pending = []
    scheduler, probe = _make(tmp_path, clock, dispatch=pending.append)
    scheduler.create_job(_job(), by=HUMAN, confirm=True)
    _tick_at(scheduler, clock, T0 + 300)
    assert len(pending) == 1 and scheduler.status()["active_runs"] == 1
    _tick_at(scheduler, clock, T0 + 600)  # first run still active
    assert len(pending) == 1
    (skipped,) = _events(scheduler, "run_skipped")
    assert skipped["body"]["reason"] == "overlap"
    pending.pop()()
    _tick_at(scheduler, clock, T0 + 900)
    pending.pop()()
    assert probe.calls == [T0 + 300, T0 + 900]
    scheduler.stop()


def test_duplicate_delivery_is_idempotent_across_ticks_clock_rewind_and_restart(tmp_path):
    clock = Clock()
    scheduler, probe = _make(tmp_path, clock)
    scheduler.create_job(_job(), by=HUMAN, confirm=True)
    _tick_at(scheduler, clock, T0 + 300)
    scheduler.tick()
    _tick_at(scheduler, clock, T0 + 250)  # clock moved backwards
    _tick_at(scheduler, clock, T0 + 310)
    assert probe.calls == [T0 + 300]
    scheduler.stop()
    restarted, probe2 = _make(tmp_path, clock)
    _tick_at(restarted, clock, T0 + 320)
    assert probe2.calls == []
    assert [e["body"]["run_key"] for e in _events(restarted, "run_started")] == [
        f"probe@{T0 + 300:.0f}"
    ]
    restarted.stop()


def test_owner_lock_refuses_a_second_scheduler_on_the_same_state_dir(tmp_path):
    clock = Clock()
    first, _ = _make(tmp_path, clock)
    second, _ = _make(tmp_path, clock, start=False)
    with pytest.raises(SchedulerLockError):
        second.start(background=False)
    with pytest.raises(SchedulerRefused) as error:
        second.create_job(_job(), by=HUMAN, confirm=True)
    assert error.value.code == "unavailable"
    first.stop()
    second.start(background=False)  # released on stop
    second.stop()


# --- AC4: budgets ------------------------------------------------------------------


def test_max_runs_per_day_stops_the_job_and_notifies_and_survives_restart(tmp_path):
    clock = Clock()
    scheduler, probe = _make(tmp_path, clock)
    scheduler.create_job(_job(budget={"max_runs_per_day": 2}), by=HUMAN, confirm=True)
    for minute in (5, 10, 15, 20):
        _tick_at(scheduler, clock, T0 + minute * 60)
    assert probe.calls == [T0 + 300, T0 + 600]
    job = scheduler.jobs()[0]
    assert (job["state"], job["paused_by"]) == ("paused", "system")
    assert "max_runs_per_day" in job["paused_reason"]
    notes = [n for n in scheduler.notifications() if n["code"] == "budget_exhausted"]
    assert len(notes) == 1 and notes[0]["job_id"] == "probe"
    scheduler.stop()

    restarted, probe2 = _make(tmp_path, clock)
    clock.now = T0 + 21 * 60
    restarted.resume_job("probe", by=HUMAN, confirm=True)
    _tick_at(restarted, clock, T0 + 25 * 60)
    assert probe2.calls == []  # the day's budget was replayed from the log
    assert restarted.jobs()[0]["state"] == "paused"
    restarted.stop()


# --- AC5: breaker ------------------------------------------------------------------


def test_breaker_warning_holds_then_releases_and_critical_stops(tmp_path):
    clock = Clock()
    breaker = Breaker()
    scheduler, probe = _make(tmp_path, clock, breaker=breaker)
    scheduler.create_job(_job(), by=HUMAN, confirm=True)
    breaker.level = "warning"
    _tick_at(scheduler, clock, T0 + 300)
    assert probe.calls == [] and scheduler.jobs()[0]["held_since"] == T0 + 300
    breaker.level = "normal"
    _tick_at(scheduler, clock, T0 + 360)
    assert probe.calls == [T0 + 300]  # the held fire ran once the breaker recovered

    breaker.level = "coherence"
    _tick_at(scheduler, clock, T0 + 600)
    breaker.level = "critical"
    _tick_at(scheduler, clock, T0 + 900)
    reasons = [e["body"]["reason"] for e in _events(scheduler, "run_skipped")]
    assert reasons == ["breaker_critical", "breaker_critical"]  # held 08:10 + fire 08:15
    assert probe.calls == [T0 + 300]
    assert "breaker_critical" in scheduler.status()["alerts"]

    # Widening changes are refused; stop-like operations always pass.
    for call in (
        lambda: scheduler.create_job(_job(job_id="other"), by=HUMAN, confirm=True),
        lambda: scheduler.update_job("probe", {"name": "x"}, by=HUMAN, confirm=True),
    ):
        with pytest.raises(SchedulerRefused) as error:
            call()
        assert error.value.code == "breaker"
    assert scheduler.pause_job("probe", by=HUMAN)["state"] == "paused"
    with pytest.raises(SchedulerRefused) as error:
        scheduler.resume_job("probe", by=HUMAN, confirm=True)
    assert error.value.code == "breaker"
    assert scheduler.stop() is True
    assert scheduler.state.value == "stopped"


def test_critical_cancels_an_active_run_and_operator_cancel_always_works(tmp_path):
    clock = Clock()
    breaker = Breaker()
    pending = []
    scheduler, _ = _make(tmp_path, clock, breaker=breaker, dispatch=pending.append)
    scheduler.create_job(_job(), by=HUMAN, confirm=True)
    _tick_at(scheduler, clock, T0 + 300)
    run_id = scheduler.runs()[0]["run_id"]
    breaker.level = "critical"
    _tick_at(scheduler, clock, T0 + 310)
    pending.pop()()
    run = scheduler.runs()[0]
    assert (run["status"], run["reason"]) == ("cancelled", "breaker_critical")

    breaker.level = "normal"
    _tick_at(scheduler, clock, T0 + 600)
    second = scheduler.runs()[0]["run_id"]
    breaker.level = "critical"
    assert scheduler.cancel_run(second, by=HUMAN)["cancel_requested"] is True
    pending.pop()()
    assert scheduler.runs()[0]["status"] == "cancelled"
    with pytest.raises(SchedulerRefused):
        scheduler.cancel_run(run_id, by=HUMAN)
    scheduler.stop()


def test_unknown_breaker_level_or_error_is_critical(tmp_path):
    clock = Clock()
    breaker = Breaker()
    scheduler, probe = _make(tmp_path, clock, breaker=breaker)
    scheduler.create_job(_job(), by=HUMAN, confirm=True)
    breaker.level = RuntimeError("engine gone")
    _tick_at(scheduler, clock, T0 + 300)
    breaker.level = "panic"
    _tick_at(scheduler, clock, T0 + 600)
    assert probe.calls == []
    assert [e["body"]["reason"] for e in _events(scheduler, "run_skipped")] == [
        "breaker_critical",
        "breaker_critical",
    ]
    scheduler.stop()


def test_held_fire_expires_after_hold_max(tmp_path):
    clock = Clock()
    breaker = Breaker("normal")
    scheduler, probe = _make(tmp_path, clock, breaker=breaker)
    scheduler.create_job(_job(hold_max_sec=60, missed_policy="skip"), by=HUMAN, confirm=True)
    breaker.level = "warning"
    _tick_at(scheduler, clock, T0 + 300)
    breaker.level = "normal"
    _tick_at(scheduler, clock, T0 + 420)
    assert probe.calls == []
    assert _events(scheduler, "run_skipped")[0]["body"]["reason"] == "held_expired"
    scheduler.stop()


# --- operations: confirmation, principal, self-extension -----------------------------


def test_widening_changes_need_a_confirmed_human_and_never_come_from_a_job(tmp_path):
    clock = Clock()
    attempts = []

    def self_extend(ctx):
        try:
            ctx.scheduler.create_job(_job(job_id="spawned"), by=HUMAN, confirm=True)
        except SchedulerRefused as error:
            attempts.append(error.code)
        return "tried"

    scheduler, _ = _make(tmp_path, clock, actions={"self_extend": self_extend})
    with pytest.raises(SchedulerRefused) as error:
        scheduler.create_job(_job(), by=HUMAN)
    assert error.value.code == "confirmation_required"
    for principal in ("system", "", "remote:x", "local:", None):
        with pytest.raises(SchedulerRefused) as error:
            scheduler.create_job(_job(), by=principal, confirm=True)
        assert error.value.code == "scope"
    assert _events(scheduler) == []
    scheduler.create_job(
        _job(job_id="ext", action="self_extend", schedule={"type": "once", "at": T0 + 60}),
        by=HUMAN,
        confirm=True,
    )
    _tick_at(scheduler, clock, T0 + 60)
    assert attempts == ["scope"]
    assert [j["job_id"] for j in scheduler.jobs()] == ["ext"]
    assert scheduler.jobs()[0]["state"] == "finished"
    created = _events(scheduler, "job_created")[0]["body"]
    assert created["by"] == HUMAN
    with pytest.raises(SchedulerRefused) as error:
        scheduler.update_job("ext", {"action": "probe"}, by=HUMAN, confirm=True)
    assert error.value.code in ("scope", "conflict")
    scheduler.stop()


# --- AC7: approval-gated tools in an unattended job ---------------------------------


def _agent_scheduler(tmp_path, clock, script, *, tools, monkeypatch, **kwargs):
    from strands import tool

    from stiracore.scheduler.executors import AgentJobExecutor
    from tests.provider_fakes import FakeModel

    model = FakeModel(script)
    executed = []

    @tool
    def memory_write(layer: str, key: str, content: str) -> str:
        """Write a memory (approval-gated)."""
        executed.append(("memory_write", key))
        return "written"

    @tool
    def current_time() -> str:
        """Current time."""
        executed.append(("current_time", None))
        return "08:00"

    @tool
    def shell(command: str) -> str:
        """Run a command (exec)."""
        executed.append(("shell", command))
        return "ran"

    available = {"memory_write": memory_write, "current_time": current_time, "shell": shell}
    # A careless provider hands out everything it was not asked for.
    executor = AgentJobExecutor(lambda job: model, lambda names: list(available.values()))
    scheduler = Scheduler(
        tmp_path,
        now_fn=clock,
        breaker_level=lambda: "normal",
        agent_executor=executor,
        dispatch=lambda work: work(),
        **kwargs,
    )
    scheduler.start(background=False)
    return scheduler, model, executed


AGENT_JOB = {
    "job_id": "digest",
    "name": "digest",
    "layer": "agent",
    "prompt": "summarize today",
    "tools": ["current_time"],
    "schedule": {"type": "cron", "expr": "0 * * * *"},
    "budget": {"max_tokens_per_run": 500, "max_tokens_per_day": 2000},
}


@pytest.mark.parametrize("tool_name", ["memory_write", "shell"])
def test_job_tool_needing_approval_fails_closed_even_with_bypass_env(
    tmp_path, monkeypatch, tool_name
):
    from tests.provider_fakes import text_turn, tool_turn

    for name, value in (
        ("STIRA_ENABLE_EXEC_TOOLS", "1"),
        ("BYPASS_TOOL_CONSENT", "true"),
        ("STIRA_HITL_AUTO_APPROVE", "1"),
    ):
        monkeypatch.setenv(name, value)
    clock = Clock()
    tool_input = (
        {"layer": "semantic", "key": "k", "content": "x"}
        if tool_name == "memory_write"
        else {"command": "rm -rf /tmp/x"}
    )
    scheduler, model, executed = _agent_scheduler(
        tmp_path,
        clock,
        [tool_turn(tool_name, tool_input), text_turn("done anyway")],
        tools=[tool_name],
        monkeypatch=monkeypatch,
    )
    scheduler.create_job(AGENT_JOB, by=HUMAN, confirm=True)
    _tick_at(scheduler, clock, T0 + 3600)
    assert executed == []
    # Only the read-only tool was offered to the model.
    assert model.requests[0]["tool_names"] == ["current_time"]
    run = scheduler.runs()[0]
    assert (run["status"], run["reason"]) == ("failed", "approval_required")
    (waiting,) = _events(scheduler, "run_awaiting_approval")
    assert waiting["body"]["tool"] == tool_name
    assert "rm -rf" not in json.dumps(scheduler.log.events())  # arguments never stored
    assert len(waiting["body"]["args_hash"]) == 64
    assert any(n["code"] == "approval_required" for n in scheduler.notifications())
    (needed,) = [n for n in scheduler.notifications() if n["code"] == "approval_required"]
    assert needed["params"] == {"tool": tool_name}  # #279: the UI words it from code + tool
    scheduler.stop()


def test_job_definition_forged_with_a_write_tool_still_cannot_call_it(tmp_path, monkeypatch):
    from dataclasses import replace

    from tests.provider_fakes import tool_turn

    clock = Clock()
    scheduler, _model, executed = _agent_scheduler(
        tmp_path,
        clock,
        [tool_turn("memory_write", {"layer": "semantic", "key": "k", "content": "x"})],
        tools=[],
        monkeypatch=monkeypatch,
    )
    scheduler.create_job(AGENT_JOB, by=HUMAN, confirm=True)
    job = scheduler._jobs["digest"]
    job.definition = replace(job.definition, tools=("current_time", "memory_write"))
    _tick_at(scheduler, clock, T0 + 3600)
    assert executed == []
    assert scheduler.runs()[0]["reason"] == "approval_required"
    scheduler.stop()


def test_agent_job_read_only_tool_runs_and_usage_counts_toward_the_budget(tmp_path, monkeypatch):
    from tests.provider_fakes import text_turn, tool_turn

    clock = Clock()
    usage = {"inputTokens": 100, "outputTokens": 20, "totalTokens": 120}
    scheduler, _model, executed = _agent_scheduler(
        tmp_path,
        clock,
        [tool_turn("current_time", {}) + [{"usage": usage}], text_turn("all quiet", usage=usage)],
        tools=["current_time"],
        monkeypatch=monkeypatch,
    )
    scheduler.create_job(AGENT_JOB, by=HUMAN, confirm=True)
    _tick_at(scheduler, clock, T0 + 3600)
    assert executed == [("current_time", None)]
    run = scheduler.runs()[0]
    assert (run["status"], run["tokens"], run["summary"]) == ("completed", 240, "all quiet\n")
    assert scheduler.jobs()[0]["usage_today"] == {"runs": 1, "tokens": 240}
    scheduler.stop()


def test_agent_job_over_its_per_run_token_cap_is_stopped_paused_and_notified(tmp_path, monkeypatch):
    from tests.provider_fakes import text_turn, tool_turn

    clock = Clock()
    big = {"inputTokens": 500, "outputTokens": 100, "totalTokens": 600}
    scheduler, model, _executed = _agent_scheduler(
        tmp_path,
        clock,
        [tool_turn("current_time", {}) + [{"usage": big}], text_turn("never reached")],
        tools=["current_time"],
        monkeypatch=monkeypatch,
    )
    scheduler.create_job(AGENT_JOB, by=HUMAN, confirm=True)
    _tick_at(scheduler, clock, T0 + 3600)
    assert len(model.requests) == 1  # cancelled before the second model call
    run = scheduler.runs()[0]
    assert (run["status"], run["reason"]) == ("failed", "budget_exceeded")
    assert scheduler.jobs()[0]["state"] == "paused"
    assert any(n["code"] == "budget_exhausted" for n in scheduler.notifications())
    scheduler.stop()


def test_daily_token_budget_refuses_a_start_that_could_exceed_it(tmp_path, monkeypatch):
    clock = Clock()
    scheduler, model, _executed = _agent_scheduler(
        tmp_path, clock, [], tools=[], monkeypatch=monkeypatch, daily_token_budget=400
    )
    scheduler.create_job(AGENT_JOB, by=HUMAN, confirm=True)  # per run 500 > 400
    _tick_at(scheduler, clock, T0 + 3600)
    assert model.requests == []
    assert _events(scheduler, "run_skipped")[0]["body"]["reason"] == "budget"
    assert scheduler.jobs()[0]["state"] == "paused"
    scheduler.stop()


def test_unattended_approval_modes_other_than_deny_are_refused():
    from stiracore.scheduler.executors import AgentJobExecutor

    with pytest.raises(ValueError, match="deny"):
        AgentJobExecutor(lambda job: None, lambda names: [], unattended_approval="wait")


def test_agent_jobs_are_refused_without_an_agent_executor(tmp_path):
    scheduler, _ = _make(tmp_path, Clock())
    with pytest.raises(SchedulerRefused) as error:
        scheduler.create_job(AGENT_JOB, by=HUMAN, confirm=True)
    assert error.value.code == "scope"
    scheduler.stop()


# --- health notifications ------------------------------------------------------------


def test_health_notifications_raise_once_and_resolve(tmp_path):
    clock = Clock()
    heartbeat = {"enabled": True, "running": True, "last_tick_at": T0, "interval_sec": 60.0}
    scheduler, probe = _make(tmp_path, clock, health_source=lambda: dict(heartbeat))
    scheduler.create_job(_job(), by=HUMAN, confirm=True)
    probe.fail = True
    for minute in (5, 10, 15, 20):
        _tick_at(scheduler, clock, T0 + minute * 60)
    codes = [(n["code"], n["state"]) for n in scheduler.notifications()][::-1]
    assert ("heartbeat_stalled", "raised") in codes  # last tick older than 3 intervals
    assert codes.count(("job_failing", "raised")) == 1
    assert codes.count(("heartbeat_stalled", "raised")) == 1
    heartbeat["last_tick_at"] = T0 + 1500
    probe.fail = False
    _tick_at(scheduler, clock, T0 + 1500)
    codes = [(n["code"], n["state"]) for n in scheduler.notifications()]
    assert ("heartbeat_stalled", "resolved") in codes
    assert ("job_failing", "resolved") in codes
    assert scheduler.status()["alerts"] == []
    scheduler.stop()


def test_disabled_heartbeat_raises_no_stall_alert(tmp_path):
    scheduler, _ = _make(tmp_path, Clock(), health_source=lambda: {"enabled": False})
    scheduler.tick()
    assert scheduler.notifications() == []
    scheduler.stop()


# --- AC8: events survive a restart; tampering fails closed ---------------------------


def test_events_replay_consistently_after_restart(tmp_path):
    clock = Clock()
    scheduler, _ = _make(tmp_path, clock)
    scheduler.create_job(_job(), by=HUMAN, confirm=True)
    scheduler.create_job(_job(job_id="second", name="second"), by=HUMAN, confirm=True)
    scheduler.pause_job("second", by=HUMAN, reason="later")
    _tick_at(scheduler, clock, T0 + 300)
    before = (scheduler.jobs(), scheduler.runs(), scheduler.notifications())
    scheduler.stop()
    assert (tmp_path / "scheduler-events.jsonl").stat().st_mode & 0o777 == 0o600
    restarted, _ = _make(tmp_path, clock)
    assert (restarted.jobs(), restarted.runs(), restarted.notifications()) == before
    ok, error = restarted.log.verify()
    assert ok and error is None
    restarted.stop()


def test_a_run_interrupted_by_a_crash_is_closed_not_retried(tmp_path):
    clock = Clock()
    pending = []
    scheduler, probe = _make(tmp_path, clock, dispatch=pending.append)
    scheduler.create_job(_job(), by=HUMAN, confirm=True)
    _tick_at(scheduler, clock, T0 + 300)
    scheduler._owner.release()  # simulate the process dying mid-run (no stop())
    restarted, probe2 = _make(tmp_path, clock)
    run = restarted.runs()[0]
    assert (run["status"], run["reason"]) == ("cancelled", "interrupted")
    _tick_at(restarted, clock, T0 + 330)
    assert probe2.calls == [] and probe.calls == []
    restarted.stop()


def _lines(path):
    return path.read_text(encoding="utf-8").splitlines(keepends=True)


def test_tampered_log_makes_the_scheduler_unavailable(tmp_path):
    clock = Clock()
    scheduler, _ = _make(tmp_path, clock)
    scheduler.create_job(_job(budget={"max_runs_per_day": 1}), by=HUMAN, confirm=True)
    scheduler.pause_job("probe", by=HUMAN)
    scheduler.stop()
    log = tmp_path / "scheduler-events.jsonl"
    original = log.read_text(encoding="utf-8")

    # 1) Edit a definition in place (raise the budget): chain broken.
    log.write_text(original.replace('"max_runs_per_day":1', '"max_runs_per_day":9'))
    broken, _ = _make(tmp_path, clock, start=False)
    with pytest.raises(SchedulerLogError):
        broken.start(background=False)
    assert broken.status()["state"] == "unavailable"
    with pytest.raises(SchedulerRefused) as error:
        broken.create_job(_job(job_id="x"), by=HUMAN, confirm=True)
    assert error.value.code == "unavailable"
    assert log.read_text(encoding="utf-8") != original  # kept for inspection, not rewritten

    # 2) Append a forged, correctly hashed "job_resumed" without the head.
    from stiracore.scheduler.event_log import line_hash

    log.write_text(original)
    last = json.loads(_lines(log)[-1])
    body = {"job_id": "probe", "by": HUMAN}
    digest = line_hash(last["hash"], last["seq"] + 1, T0, "job_resumed", body)
    forged = {"seq": last["seq"] + 1, "ts": T0, "kind": "job_resumed", "body": body}
    forged.update(prev=last["hash"], hash=digest)
    with log.open("a") as handle:
        handle.write(json.dumps(forged) + "\n")
    forged_scheduler, _ = _make(tmp_path, clock, start=False)
    with pytest.raises(SchedulerLogError):
        forged_scheduler.start(background=False)

    # 3) Delete the head anchor: refused as well.
    log.write_text(original)
    (tmp_path / "scheduler-events.jsonl.head").unlink()
    headless, _ = _make(tmp_path, clock, start=False)
    with pytest.raises(SchedulerLogError):
        headless.start(background=False)


def test_deleting_the_last_line_cannot_reenable_a_job(tmp_path):
    clock = Clock()
    scheduler, _ = _make(tmp_path, clock)
    scheduler.create_job(_job(), by=HUMAN, confirm=True)
    scheduler.pause_job("probe", by=HUMAN)
    scheduler.stop()
    log = tmp_path / "scheduler-events.jsonl"
    lines = _lines(log)
    assert json.loads(lines[-1])["kind"] == "job_paused"
    log.write_text("".join(lines[:-1]))  # drop the pause
    restarted, probe = _make(tmp_path, clock)
    job = restarted.jobs()[0]
    assert (job["state"], job["paused_by"]) == ("paused", "system")
    assert any(n["code"] == "log_recovered" for n in restarted.notifications())
    _tick_at(restarted, clock, T0 + 300)
    assert probe.calls == []
    restarted.stop()
    again, _ = _make(tmp_path, clock)  # the recovery itself is a consistent chain
    assert again.log.verify() == (True, None)
    again.stop()


def test_crash_with_partial_line_recovers_fail_closed(tmp_path):
    clock = Clock()
    scheduler, _ = _make(tmp_path, clock)
    scheduler.create_job(_job(), by=HUMAN, confirm=True)
    log_obj = scheduler.log
    # Head written for the next event, then the process dies mid-append.
    log_obj._write_head(
        {"seq": 2, "hash": "f" * 64, "base_seq": 1, "base_hash": log_obj._tail_hash}
    )
    with (tmp_path / "scheduler-events.jsonl").open("a") as handle:
        handle.write('{"seq":2,"ts":')
    scheduler._owner.release()
    restarted, _probe = _make(tmp_path, clock)
    assert restarted.jobs()[0]["state"] == "paused"
    assert restarted.log.verify() == (True, None)
    restarted.stop()


def test_external_write_while_running_stops_the_scheduler(tmp_path):
    clock = Clock()
    scheduler, probe = _make(tmp_path, clock)
    scheduler.create_job(_job(), by=HUMAN, confirm=True)
    with (tmp_path / "scheduler-events.jsonl").open("a") as handle:
        handle.write("{}\n")
    _tick_at(scheduler, clock, T0 + 300)
    assert probe.calls == []
    assert scheduler.status()["state"] == "unavailable"
    with pytest.raises(SchedulerRefused):
        scheduler.create_job(_job(job_id="y"), by=HUMAN, confirm=True)
    assert scheduler.stop() is True
    assert not scheduler._owner.held


def test_background_worker_runs_and_stops(tmp_path):
    clock = Clock()
    probe = Probe()
    done = threading.Event()

    def action(ctx):
        probe(ctx)
        done.set()
        return "ok"

    scheduler = Scheduler(
        tmp_path,
        now_fn=clock,
        breaker_level=lambda: "normal",
        gateway_actions={"probe": action},
        max_wait_sec=1.0,
    )
    scheduler.start()
    scheduler.create_job(_job(), by=HUMAN, confirm=True)
    clock.now = T0 + 300
    scheduler._wake.set()
    assert done.wait(5)
    assert scheduler.stop(timeout=5) is True
    assert not any(t.name == "stira-scheduler" for t in threading.enumerate())
    assert scheduler.runs()[0]["status"] == "completed"


def test_shutdown_cancels_an_active_run(tmp_path):
    clock = Clock()
    pending = []
    scheduler, _ = _make(tmp_path, clock, dispatch=pending.append)
    scheduler.create_job(_job(), by=HUMAN, confirm=True)
    _tick_at(scheduler, clock, T0 + 300)
    # The dispatched worker has not finished: the stop is pending, ownership kept.
    assert scheduler.stop(timeout=0.1) is False
    assert scheduler._owner.held
    pending.pop()()  # the worker sees the shutdown and does not run the job body
    run = scheduler.runs()[0]
    assert (run["status"], run["reason"]) == ("cancelled", "shutdown")
    assert scheduler.state.value == "stopped" and not scheduler._owner.held


def test_event_log_rejects_append_before_open(tmp_path):
    log = SchedulerEventLog(tmp_path / "x.jsonl")
    with pytest.raises(SchedulerLogError):
        log.append("k", {}, T0)


def test_runoutcome_defaults():
    assert RunOutcome("completed").tokens == 0


# --- attacker-view regressions --------------------------------------------------------


def test_cancel_takes_effect_even_if_recording_it_fails(tmp_path, monkeypatch):
    clock = Clock()
    pending = []
    scheduler, probe = _make(tmp_path, clock, dispatch=pending.append)
    scheduler.create_job(_job(), by=HUMAN, confirm=True)
    _tick_at(scheduler, clock, T0 + 300)
    run_id = scheduler.runs()[0]["run_id"]
    ctx = scheduler._active[run_id]

    def broken_append(*args, **kwargs):
        raise SchedulerLogError("disk full")

    monkeypatch.setattr(scheduler.log, "append", broken_append)
    # Stop-like operations are never refused; recording them is best effort.
    assert scheduler.cancel_run(run_id, by=HUMAN)["cancel_requested"] is True
    assert ctx.cancelled
    pending.pop()()
    assert probe.calls == []  # the job body never ran
    assert scheduler.status()["state"] == "unavailable"
    scheduler.stop()


def test_a_budget_pause_that_cannot_be_recorded_stops_everything(tmp_path, monkeypatch):
    clock = Clock()
    scheduler, probe = _make(tmp_path, clock)
    scheduler.create_job(_job(budget={"max_runs_per_day": 1}), by=HUMAN, confirm=True)
    _tick_at(scheduler, clock, T0 + 300)
    real_append = scheduler.log.append

    def append(kind, body, ts):
        if kind in ("job_paused", "run_skipped"):
            raise SchedulerLogError("disk full")
        return real_append(kind, body, ts)

    monkeypatch.setattr(scheduler.log, "append", append)
    for minute in (10, 15, 20):
        _tick_at(scheduler, clock, T0 + minute * 60)
    assert probe.calls == [T0 + 300]
    assert scheduler.status()["state"] == "unavailable"
    scheduler.stop()


def test_free_text_is_masked_before_it_reaches_the_log(tmp_path):
    clock = Clock()
    scheduler, _ = _make(tmp_path, clock)
    scheduler.create_job(_job(), by=HUMAN, confirm=True)
    secret = "sk-" + "a" * 40
    scheduler.pause_job("probe", by=HUMAN, reason=f"rotating api_key={secret}")
    raw = (tmp_path / "scheduler-events.jsonl").read_text(encoding="utf-8")
    assert secret not in raw
    with pytest.raises(SchedulerRefused):
        scheduler.create_job(_job(job_id="s2", name=f"token {secret}"), by=HUMAN, confirm=True)
    assert secret not in (tmp_path / "scheduler-events.jsonl").read_text(encoding="utf-8")
    scheduler.stop()


def test_principal_names_are_sanitized_for_the_audit_trail(tmp_path):
    clock = Clock()
    scheduler, _ = _make(tmp_path, clock)
    with pytest.raises(SchedulerRefused):
        scheduler.create_job(_job(), by="local:op\nINJECTED", confirm=True)
    scheduler.create_job(_job(), by=HUMAN, confirm=True)
    scheduler.pause_job("probe", by="local:x\x1b[2J")
    paused = _events(scheduler, "job_paused")[0]["body"]
    assert paused["by"] == "local:unknown"
    scheduler.stop()


# --- review round 1 (#265): reproductions --------------------------------------------


@pytest.mark.parametrize(
    ("expr", "expected_days"),
    [
        # Vixie cron: a field starting with "*" (also "*/n") is a star -> AND.
        ("0 0 */1 * 1", ["Mon"]),
        ("0 0 * * 1", ["Mon"]),
        # Both restricted -> OR (the window excludes both Sundays at 00:00).
        ("0 0 1-31 * 1", ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat"]),
        ("0 0 18,20 * 5", ["Mon", "Wed", "Fri"]),
        ("0 0 18 * *", ["Mon"]),
        ("0 0 */2 * */1", ["Tue", "Thu", "Sat"]),  # DOW is a star: odd days only (AND)
        ("0 0 */1 * 1-5", ["Mon", "Tue", "Wed", "Thu", "Fri"]),
    ],
)
def test_cron_star_step_fields_are_stars_for_the_dom_dow_rule(expr, expected_days):
    from datetime import datetime

    schedule = parse_schedule({"type": "cron", "expr": expr}, default_anchor=T0)
    # One week from Sunday 2027-01-17 00:00 UTC (exclusive) to the next Sunday.
    start = datetime(2027, 1, 17, 0, 0, tzinfo=UTC).timestamp()
    fires = schedule.fires_between(start, start + 7 * 86400 - 1, 100)
    days = [datetime.fromtimestamp(f, UTC).strftime("%a") for f in fires]
    assert sorted(set(days)) == sorted(expected_days)
    assert len(days) == len(set(days))


def test_bad_field_types_are_invalid_not_server_errors():
    kwargs = {"now": T0, "gateway_actions": {"probe"}, "agent_jobs": True}
    for bad in (
        {**_job(), "layer": []},
        {**_job(), "missed_policy": {}},
        {**_job(), "schedule": {"type": []}},
        {**_job(), "schedule": {"type": "cron", "expr": ["*"]}},
        {**_job(), "tools": "file_read"},
        {**_job(), "budget": []},
    ):
        with pytest.raises(JobError):
            parse_job(bad, **kwargs)


def test_secret_looking_identifiers_are_refused_and_free_text_is_masked(tmp_path):
    clock = Clock()

    def leaky(ctx):
        ctx.require_approval("ghp_" + "a" * 36, {"x": 1})
        raise RuntimeError("unused")

    scheduler, _ = _make(tmp_path, clock, actions={"leaky": leaky})
    with pytest.raises(SchedulerRefused) as error:
        scheduler.create_job(_job(job_id="sk-" + "a" * 40), by=HUMAN, confirm=True)
    assert error.value.code == "invalid"
    scheduler.create_job(
        _job(job_id="leak", action="leaky", schedule={"type": "once", "at": T0 + 60}),
        by=HUMAN,
        confirm=True,
    )
    _tick_at(scheduler, clock, T0 + 60)
    raw = (tmp_path / "scheduler-events.jsonl").read_text(encoding="utf-8")
    assert "sk-" + "a" * 40 not in raw
    assert "ghp_" + "a" * 36 not in raw
    assert scheduler.runs()[0]["reason"] == "approval_required"
    scheduler.stop()


def test_a_run_dispatched_before_critical_never_executes(tmp_path):
    clock = Clock()
    breaker = Breaker()
    pending = []
    scheduler, probe = _make(tmp_path, clock, breaker=breaker, dispatch=pending.append)
    scheduler.create_job(_job(), by=HUMAN, confirm=True)
    _tick_at(scheduler, clock, T0 + 300)
    breaker.level = "critical"  # no tick in between
    pending.pop()()
    assert probe.calls == []
    run = scheduler.runs()[0]
    assert (run["status"], run["reason"]) == ("cancelled", "breaker_critical")
    scheduler.stop()


def test_a_run_dispatched_before_a_pause_never_executes(tmp_path):
    clock = Clock()
    pending = []
    scheduler, probe = _make(tmp_path, clock, dispatch=pending.append)
    scheduler.create_job(_job(), by=HUMAN, confirm=True)
    _tick_at(scheduler, clock, T0 + 300)
    scheduler.pause_job("probe", by=HUMAN)
    pending.pop()()
    assert probe.calls == []
    assert scheduler.runs()[0]["reason"] == "paused"
    scheduler.stop()


def test_global_token_budget_is_reserved_across_jobs_started_in_one_tick(tmp_path, monkeypatch):
    clock = Clock()
    pending = []
    scheduler, _model, _executed = _agent_scheduler(
        tmp_path,
        clock,
        [],
        tools=[],
        monkeypatch=monkeypatch,
        daily_token_budget=600,
    )
    scheduler._dispatch = pending.append
    scheduler.create_job(AGENT_JOB, by=HUMAN, confirm=True)  # 500 per run
    scheduler.create_job({**AGENT_JOB, "job_id": "digest2", "name": "d2"}, by=HUMAN, confirm=True)
    _tick_at(scheduler, clock, T0 + 3600)
    started = _events(scheduler, "run_started")
    skipped = [e["body"]["reason"] for e in _events(scheduler, "run_skipped")]
    assert len(started) == 1 and skipped == ["budget"]
    assert len(pending) == 1
    scheduler.stop(timeout=0)


def test_interrupted_agent_run_is_charged_its_full_reservation(tmp_path, monkeypatch):
    clock = Clock()
    pending = []
    scheduler, _model, _executed = _agent_scheduler(
        tmp_path, clock, [], tools=[], monkeypatch=monkeypatch
    )
    scheduler._dispatch = pending.append
    scheduler.create_job(AGENT_JOB, by=HUMAN, confirm=True)
    _tick_at(scheduler, clock, T0 + 3600)
    scheduler._owner.release()  # the process dies mid-run
    restarted, _ = _make(tmp_path, clock)
    run = restarted.runs()[0]
    assert (run["status"], run["tokens"]) == ("cancelled", 500)
    assert restarted.jobs()[0]["usage_today"]["tokens"] == 500
    restarted.stop()


@pytest.mark.parametrize("crash_after", [1, 2, 3, 4])
def test_a_crash_during_recovery_never_reenables_a_job(tmp_path, crash_after):
    clock = Clock()
    scheduler, _ = _make(tmp_path, clock)
    for name in ("a1", "a2", "a3"):
        scheduler.create_job(_job(job_id=name, name=name), by=HUMAN, confirm=True)
    scheduler.pause_job("a3", by=HUMAN)
    scheduler.stop()
    log = tmp_path / "scheduler-events.jsonl"
    log.write_text("".join(_lines(log)[:-1]))  # drop a3's pause: recovery needed

    crashing, _ = _make(tmp_path, clock, start=False)
    real_append = crashing.log.append
    calls = {"n": 0}

    def append(kind, body, ts):
        if calls["n"] >= crash_after:
            raise SchedulerLogError("process died")
        calls["n"] += 1
        return real_append(kind, body, ts)

    crashing.log.append = append
    with pytest.raises(SchedulerLogError):
        crashing.start(background=False)
    # head == tail now (the appends that happened were complete).
    restarted, probe = _make(tmp_path, clock)
    assert {j["job_id"]: j["state"] for j in restarted.jobs()} == {
        "a1": "paused",
        "a2": "paused",
        "a3": "paused",
    }
    _tick_at(restarted, clock, T0 + 300)
    assert probe.calls == []
    restarted.stop()


def test_owner_lock_is_kept_while_a_worker_ignoring_cancel_is_alive(tmp_path):
    clock = Clock()
    release = threading.Event()
    entered = threading.Event()
    calls = []

    def stubborn(ctx):
        calls.append(ctx.scheduled_at)
        entered.set()
        release.wait(10)  # ignores ctx.cancelled
        return "late"

    scheduler = Scheduler(
        tmp_path,
        now_fn=clock,
        breaker_level=lambda: "normal",
        gateway_actions={"probe": stubborn},
    )
    scheduler.start(background=False)
    scheduler.create_job(_job(), by=HUMAN, confirm=True)
    _tick_at(scheduler, clock, T0 + 300)
    assert entered.wait(5)
    assert scheduler.stop(timeout=0) is False
    assert scheduler.state.value == "stopping"
    other = Scheduler(tmp_path, now_fn=clock, gateway_actions={"probe": stubborn})
    with pytest.raises(SchedulerLockError):
        other.start(background=False)
    release.set()
    deadline = __import__("time").monotonic() + 5
    while scheduler.state.value != "stopped" and __import__("time").monotonic() < deadline:
        __import__("time").sleep(0.01)
    assert scheduler.state.value == "stopped"
    other.start(background=False)
    _tick_at(other, clock, T0 + 330)
    assert calls == [T0 + 300]  # no duplicate run of the same fire
    other.stop()


def test_a_blocking_cancel_callback_does_not_block_stop(tmp_path):
    clock = Clock()
    pending = []
    scheduler, _ = _make(tmp_path, clock, dispatch=pending.append)
    scheduler.create_job(_job(), by=HUMAN, confirm=True)
    _tick_at(scheduler, clock, T0 + 300)
    ctx = next(iter(scheduler._active.values()))
    gate = threading.Event()
    ctx.on_cancel(lambda: gate.wait(10))
    started = __import__("time").monotonic()
    assert scheduler.stop(timeout=0.2) is False  # the dispatched work has not run yet
    assert __import__("time").monotonic() - started < 2
    gate.set()
    pending.pop()()
    assert scheduler.state.value == "stopped"


def test_same_size_rewrite_of_the_tail_is_detected_while_running(tmp_path):
    clock = Clock()
    scheduler, probe = _make(tmp_path, clock)
    scheduler.create_job(_job(), by=HUMAN, confirm=True)
    log = tmp_path / "scheduler-events.jsonl"
    data = log.read_text(encoding="utf-8")
    log.write_text(data.replace('"name":"probe"', '"name":"prob3"'))
    assert log.stat().st_size == len(data.encode())
    _tick_at(scheduler, clock, T0 + 300)
    assert probe.calls == []
    assert scheduler.status()["state"] == "unavailable"
    scheduler.stop()


def test_pause_and_cancel_take_effect_even_when_the_log_is_unavailable(tmp_path):
    clock = Clock()
    pending = []
    scheduler, probe = _make(tmp_path, clock, dispatch=pending.append)
    scheduler.create_job(_job(), by=HUMAN, confirm=True)
    scheduler.create_job(_job(job_id="other", name="other"), by=HUMAN, confirm=True)
    _tick_at(scheduler, clock, T0 + 300)
    run_ids = [r["run_id"] for r in scheduler.runs()]
    scheduler._fail_unavailable("simulated")
    assert scheduler.pause_job("other", by=HUMAN)["state"] == "paused"
    assert scheduler.cancel_run(run_ids[0], by=HUMAN)["cancel_requested"] is True
    for work in pending:
        work()
    assert probe.calls == []
    scheduler.stop()


# --- review round 2 (#265): reproductions --------------------------------------------


def test_agent_failure_after_usage_is_charged_at_least_the_reservation(tmp_path, monkeypatch):
    from tests.provider_fakes import tool_turn

    clock = Clock()
    used = {"inputTokens": 150, "outputTokens": 50, "totalTokens": 200}
    scheduler, model, _executed = _agent_scheduler(
        tmp_path,
        clock,
        [tool_turn("current_time", {}) + [{"usage": used}], [{"raise": RuntimeError("boom")}]],
        tools=["current_time"],
        monkeypatch=monkeypatch,
    )
    job = {**AGENT_JOB, "budget": {"max_tokens_per_run": 500, "max_tokens_per_day": 1000}}
    scheduler.create_job(job, by=HUMAN, confirm=True)
    _tick_at(scheduler, clock, T0 + 3600)
    assert len(model.requests) == 2
    run = scheduler.runs()[0]
    assert run["status"] == "failed"
    assert run["tokens"] == 500  # unknown final usage: max(observed 200, reservation 500)
    assert scheduler.jobs()[0]["usage_today"]["tokens"] == 500
    # 500 used + 500 reserved for the next run = the 1000 cap: one more run still fits,
    # a third would not.
    assert scheduler._budget_refusal(scheduler._jobs["digest"].definition, clock()) is None
    scheduler.stop()


def test_agent_failure_charges_the_reservation_above_the_observed_usage(tmp_path, monkeypatch):
    from tests.provider_fakes import tool_turn

    clock = Clock()
    used = {"inputTokens": 600, "outputTokens": 100, "totalTokens": 700}
    scheduler, _model, _executed = _agent_scheduler(
        tmp_path,
        clock,
        [tool_turn("current_time", {}) + [{"usage": used}], [{"raise": RuntimeError("boom")}]],
        tools=["current_time"],
        monkeypatch=monkeypatch,
    )
    job = {**AGENT_JOB, "budget": {"max_tokens_per_run": 800, "max_tokens_per_day": 5000}}
    scheduler.create_job(job, by=HUMAN, confirm=True)
    _tick_at(scheduler, clock, T0 + 3600)
    assert scheduler.runs()[0]["tokens"] == 800  # max(700 observed, 800 reserved)
    scheduler.stop()


def test_crash_settlement_uses_the_reservation_recorded_at_start(tmp_path, monkeypatch):
    clock = Clock()
    pending = []
    scheduler, _model, _executed = _agent_scheduler(
        tmp_path, clock, [], tools=[], monkeypatch=monkeypatch
    )
    scheduler._dispatch = pending.append
    scheduler.create_job(AGENT_JOB, by=HUMAN, confirm=True)  # 500 per run
    _tick_at(scheduler, clock, T0 + 3600)
    (started,) = _events(scheduler, "run_started")
    assert started["body"]["reserved_tokens"] == 500
    assert started["body"]["charge_day"] == "2027-01-15"
    clock.now = T0 + 3610
    scheduler.update_job(
        "digest",
        {"budget": {"max_tokens_per_run": 100, "max_tokens_per_day": 2000}},
        by=HUMAN,
        confirm=True,
    )
    scheduler._owner.release()  # the process dies with the old run unsettled
    restarted, _ = _make(tmp_path, clock)
    run = restarted.runs()[0]
    assert (run["status"], run["tokens"]) == ("cancelled", 500)
    restarted.stop()


def _pending_agent(tmp_path, clock, monkeypatch, script, **job_overrides):
    pending = []
    scheduler, model, executed = _agent_scheduler(
        tmp_path, clock, script, tools=[], monkeypatch=monkeypatch
    )
    scheduler._dispatch = pending.append
    scheduler.create_job({**AGENT_JOB, **job_overrides}, by=HUMAN, confirm=True)
    _tick_at(scheduler, clock, T0 + 3600)
    assert len(pending) == 1
    return scheduler, model, executed, pending


@pytest.mark.parametrize(
    "changes",
    [
        {"tools": []},
        {"budget": {"max_tokens_per_run": 100, "max_tokens_per_day": 2000}},
        {"prompt": "something else"},
    ],
)
def test_a_queued_run_never_executes_under_an_outdated_definition(tmp_path, monkeypatch, changes):
    from tests.provider_fakes import text_turn, tool_turn

    clock = Clock()
    scheduler, model, executed, pending = _pending_agent(
        tmp_path, clock, monkeypatch, [tool_turn("current_time", {}), text_turn("done")]
    )
    clock.now = T0 + 3610
    scheduler.update_job("digest", changes, by=HUMAN, confirm=True)
    pending.pop()()
    assert model.requests == [] and executed == []
    run = scheduler.runs()[0]
    assert (run["status"], run["reason"]) == ("cancelled", "job_updated")
    scheduler.stop()


def test_a_queued_gateway_run_respects_a_lowered_max_runs_per_day(tmp_path):
    clock = Clock()
    pending = []
    scheduler, probe = _make(tmp_path, clock, dispatch=pending.append)
    scheduler.create_job(_job(budget={"max_runs_per_day": 5}), by=HUMAN, confirm=True)
    _tick_at(scheduler, clock, T0 + 300)
    pending.pop()()
    _tick_at(scheduler, clock, T0 + 600)  # second run of the day, queued
    # Lowered to the number already used. (A budget-only update keeps the generation
    # check out of the way only if the run limit itself is re-checked.)
    scheduler._jobs["probe"].definition = __import__("dataclasses").replace(
        scheduler._jobs["probe"].definition,
        budget=__import__("stiracore.scheduler.jobs", fromlist=["Budget"]).Budget(
            max_runs_per_day=1
        ),
    )
    pending.pop()()
    assert probe.calls == [T0 + 300]
    assert scheduler.runs()[0]["reason"] == "budget"
    scheduler.stop()


class _PoolWatch:
    """Every cancel-worker thread a scheduler created, kept after it left the registry."""

    def __init__(self):
        self.threads = []
        self.fail_start = False

    def alive(self):
        return [t for t in self.threads if t.is_alive()]


@pytest.fixture
def pool_watch(monkeypatch):
    """Track the cancel workers created through the scheduler module (#356).

    ``status()["cancel_workers"]`` is the scheduler's own registry; a thread that left it but
    is still running (or one that was registered but never started) must not go unnoticed,
    so the tests also look at the real threads. Only ``Thread`` is swapped, and only inside
    the scheduler module, so other tests' (and other schedulers') threads are never counted.
    """
    from stiracore.scheduler import scheduler as scheduler_module

    watch = _PoolWatch()

    class RecordingThread(threading.Thread):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            if self.name.startswith("stira-job-cancel"):
                watch.threads.append(self)

        def start(self):
            if watch.fail_start and self.name.startswith("stira-job-cancel"):
                raise RuntimeError("can't start new thread: secret-detail")
            super().start()

    class ThreadingShim:
        Thread = RecordingThread

        def __getattr__(self, name):
            return getattr(threading, name)

    monkeypatch.setattr(scheduler_module, "threading", ThreadingShim())
    return watch


def _pool_size(scheduler):
    """Cancel-callback workers *this* scheduler has registered.

    Not a count of every ``stira-job-cancel*`` thread in the process: workers of an earlier
    test's scheduler linger up to a second after it ends and exit on their own, which made a
    process-wide count drift mid-test (#356).
    """
    return scheduler.status()["cancel_workers"]


def _pool_gone(scheduler, watch, timeout=10):
    """The registry is empty *and* every real worker thread has ended."""
    assert _wait_for(lambda: _pool_size(scheduler) == 0, timeout=timeout)
    for thread in list(watch.threads):
        if thread.ident is not None:  # one whose start() failed has nothing to join
            thread.join(timeout)
    assert watch.alive() == []


def _wait_for(predicate, timeout=5.0):
    import time

    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        time.sleep(0.01)
    return predicate()


def test_cancel_backlog_is_tracked_and_refuses_new_bodies_at_the_cap(tmp_path):
    clock = Clock()
    pending = []
    gate = threading.Event()
    scheduler, probe = _make(tmp_path, clock, dispatch=pending.append, max_cancel_backlog=2)
    for name in ("c1", "c2", "c3"):
        scheduler.create_job(_job(job_id=name, name=name), by=HUMAN, confirm=True)
    _tick_at(scheduler, clock, T0 + 300)
    contexts = list(scheduler._active.values())
    assert len(contexts) == 3
    for ctx in contexts[:2]:
        ctx.on_cancel(lambda: gate.wait(10))
        for _ in range(3):  # repeated cancels never queue the callbacks again
            scheduler.cancel_run(ctx.run_id, by=HUMAN)
    assert scheduler.status()["cancel_backlog"] == 2
    for work in pending:
        work()
    pending.clear()
    assert probe.calls == []
    reasons = sorted(r["reason"] for r in scheduler.runs())
    assert reasons == ["cancel_backlog", "operator", "operator"]
    assert scheduler.stop(timeout=0.5) is True  # workers done; callbacks hold nothing
    gate.set()
    assert _wait_for(lambda: scheduler.status()["cancel_backlog"] == 0)


def test_cancel_callbacks_use_a_fixed_worker_pool_and_none_is_dropped(tmp_path, pool_watch):
    clock = Clock()
    pending = []
    gate = threading.Event()
    ran = []
    scheduler, _ = _make(tmp_path, clock, dispatch=pending.append, cancel_workers=4)
    for index in range(17):
        name = f"w{index}"
        scheduler.create_job(_job(job_id=name, name=name), by=HUMAN, confirm=True)
    _tick_at(scheduler, clock, T0 + 300)
    contexts = list(scheduler._active.values())
    assert len(contexts) == 17
    assert _pool_size(scheduler) == 0
    for ctx in contexts:
        ctx.on_cancel(lambda: (gate.wait(10), ran.append(1)))
    for ctx in contexts:
        scheduler.cancel_run(ctx.run_id, by=HUMAN)
    assert _wait_for(lambda: _pool_size(scheduler) == 4 and len(pool_watch.alive()) == 4)
    assert _pool_size(scheduler) <= 4 and len(pool_watch.alive()) <= 4
    assert scheduler.status()["cancel_backlog"] == 17
    gate.set()
    assert _wait_for(lambda: len(ran) == 17)  # every callback ran, none dropped
    assert _wait_for(lambda: scheduler.status()["cancel_backlog"] == 0)
    for work in pending:
        work()
    scheduler.stop()
    _pool_gone(scheduler, pool_watch)


class _GatedLock:
    """A lock that holds one chosen thread back at its next blocking ``acquire``.

    Used to freeze a worker in the window the pool race lives in: woken for an item, still
    counted idle, not yet holding the lock that lets it take the item.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self.gated = None
        self.open = threading.Event()

    def acquire(self, blocking=True, timeout=-1):
        if blocking and self.gated is threading.current_thread():
            self.open.wait(10)
        return self._lock.acquire(blocking, timeout)

    def release(self):
        self._lock.release()

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()


def test_a_woken_idle_worker_never_strands_a_later_cancel_callback(tmp_path, pool_watch):
    scheduler, _ = _make(tmp_path, Clock(), start=False)
    lock = _GatedLock()
    scheduler._cancel_lock = lock
    scheduler._cancel_wakeup = threading.Condition(lock)
    ran = []
    a_started, release_a = threading.Event(), threading.Event()

    def callback_a():
        a_started.set()
        release_a.wait(10)
        ran.append("A")

    # A real worker, parked idle.
    scheduler._spawn_cancel("r0", [lambda: ran.append("r0")])
    assert _wait_for(lambda: ran == ["r0"] and scheduler._cancel_idle == 1)
    (idle_worker,) = pool_watch.alive()
    lock.gated = idle_worker  # it cannot take an item until we open the gate
    scheduler._spawn_cancel("A", [callback_a])  # wakes it; it is held back, still "idle"
    assert scheduler._cancel_idle == 1
    scheduler._spawn_cancel("B", [lambda: ran.append("B")])  # queued behind A
    lock.open.set()
    assert a_started.wait(5)  # one worker is now stuck inside A ...
    assert _wait_for(lambda: "B" in ran)  # ... and B still runs, on the other worker
    assert "A" not in ran
    release_a.set()
    assert _wait_for(lambda: "A" in ran)
    assert _wait_for(lambda: scheduler.status()["cancel_backlog"] == 0)
    _pool_gone(scheduler, pool_watch)


def test_a_worker_that_fails_to_start_is_unregistered_and_its_work_is_kept(
    tmp_path, pool_watch, caplog
):
    import logging

    scheduler, _ = _make(tmp_path, Clock(), start=False, cancel_workers=2)
    ran = []
    pool_watch.fail_start = True
    with caplog.at_level(logging.WARNING, logger="stiracore.scheduler.scheduler"):
        for index in range(2):  # as many failures as the pool has slots
            scheduler._spawn_cancel(f"r{index}", [lambda: ran.append(1)])
    assert _pool_size(scheduler) == 0 and pool_watch.alive() == []
    assert scheduler.status()["cancel_backlog"] == 2  # queued, not dropped
    messages = " ".join(r.getMessage() for r in caplog.records)
    assert "RuntimeError" in messages and "secret-detail" not in messages
    assert ran == []
    pool_watch.fail_start = False
    scheduler._spawn_cancel("r2", [lambda: ran.append(1)])  # the next submission recovers
    assert _wait_for(lambda: len(ran) == 3)  # the kept work ran too
    assert _wait_for(lambda: scheduler.status()["cancel_backlog"] == 0)
    _pool_gone(scheduler, pool_watch)


def _active_contexts(tmp_path, count, **kwargs):
    clock = Clock()
    scheduler, _ = _make(tmp_path, clock, dispatch=lambda work: None, **kwargs)
    for index in range(count):
        name = f"w{index}"
        scheduler.create_job(_job(job_id=name, name=name), by=HUMAN, confirm=True)
    _tick_at(scheduler, clock, T0 + 300)
    contexts = list(scheduler._active.values())
    assert len(contexts) == count
    return scheduler, contexts


def test_stop_racing_with_cancels_runs_every_callback_exactly_once(tmp_path, pool_watch):
    scheduler, contexts = _active_contexts(tmp_path, 17, cancel_workers=3)
    gate = threading.Event()
    ran = []
    for ctx in contexts:
        ctx.on_cancel(lambda run_id=ctx.run_id: (gate.wait(10), ran.append(run_id)))
    barrier = threading.Barrier(5)

    def canceller(batch):
        barrier.wait(5)
        for ctx in batch:
            try:
                scheduler.cancel_run(ctx.run_id, by=HUMAN)
            except SchedulerRefused:
                pass  # stop() got to it first

    threads = [threading.Thread(target=canceller, args=(contexts[i::4],)) for i in range(4)]
    for thread in threads:
        thread.start()
    barrier.wait(5)
    scheduler.stop(timeout=2)
    for thread in threads:
        thread.join(5)
    assert _wait_for(lambda: scheduler.status()["cancel_backlog"] == 17)  # none lost to the race
    assert len(pool_watch.alive()) <= 3
    gate.set()
    assert _wait_for(lambda: len(ran) == 17)
    assert sorted(ran) == sorted(ctx.run_id for ctx in contexts)  # each exactly once
    assert _wait_for(lambda: scheduler.status()["cancel_backlog"] == 0)
    _pool_gone(scheduler, pool_watch)


def test_stop_with_unprocessed_cancel_work_still_runs_all_of_it(tmp_path, pool_watch):
    scheduler, _ = _make(tmp_path, Clock(), cancel_workers=2, max_cancel_backlog=100)
    gate = threading.Event()
    ran = []
    for index in range(10):
        scheduler._spawn_cancel(f"r{index}", [lambda: (gate.wait(10), ran.append(1))])
    assert _wait_for(lambda: len(pool_watch.alive()) == 2)
    assert scheduler.status()["cancel_backlog"] == 10  # 2 running, 8 queued
    assert scheduler.stop(timeout=1) is True  # stopping does not drop or wait for them
    assert ran == []
    gate.set()
    assert _wait_for(lambda: len(ran) == 10)
    assert _wait_for(lambda: scheduler.status()["cancel_backlog"] == 0)
    _pool_gone(scheduler, pool_watch)


def test_a_raising_cancel_callback_does_not_stop_the_rest(tmp_path, pool_watch):
    scheduler, _ = _make(tmp_path, Clock(), start=False, cancel_workers=1)
    ran = []

    def boom():
        raise RuntimeError("hook failed")

    scheduler._spawn_cancel("r0", [boom, lambda: ran.append("same batch")])
    scheduler._spawn_cancel("r1", [lambda: ran.append("next run")])
    assert _wait_for(lambda: ran == ["same batch", "next run"])
    assert _wait_for(lambda: scheduler.status()["cancel_backlog"] == 0)
    _pool_gone(scheduler, pool_watch)


def test_agent_failure_keeps_observed_usage_above_the_reservation(tmp_path):
    clock = Clock()
    scheduler = Scheduler(
        tmp_path,
        now_fn=clock,
        breaker_level=lambda: "normal",
        # Observed 700 before an error; the reservation is only 500.
        agent_executor=lambda ctx: RunOutcome("failed", "Boom", 700, usage_known=False),
        dispatch=lambda work: work(),
    )
    scheduler.start(background=False)
    scheduler.create_job(AGENT_JOB, by=HUMAN, confirm=True)  # 500 per run
    _tick_at(scheduler, clock, T0 + 3600)
    run = scheduler.runs()[0]
    assert (run["status"], run["tokens"]) == ("failed", 700)
    assert scheduler.jobs()[0]["usage_today"]["tokens"] == 700
    scheduler.stop()


def test_a_run_queued_across_midnight_is_checked_against_its_charge_day(tmp_path):
    from datetime import datetime

    clock = Clock(datetime(2027, 1, 15, 23, 59, tzinfo=UTC).timestamp())
    pending = []

    def executor(ctx):
        if ctx.job.job_id == "a":
            return RunOutcome("completed", tokens=800)
        raise AssertionError("B must not run")

    scheduler = Scheduler(
        tmp_path,
        now_fn=clock,
        breaker_level=lambda: "normal",
        agent_executor=executor,
        dispatch=pending.append,
        daily_token_budget=1000,
    )
    scheduler.start(background=False)
    once = {"type": "once", "at": clock.now + 30}
    budget = {"max_tokens_per_run": 500, "max_tokens_per_day": 5000}
    for name in ("a", "b"):
        scheduler.create_job(
            {**AGENT_JOB, "job_id": name, "name": name, "schedule": once, "budget": budget},
            by=HUMAN,
            confirm=True,
        )
    _tick_at(scheduler, clock, clock.now + 30)  # 23:59:30: both start, 500 + 500 reserved
    assert len(pending) == 2
    pending[0]()  # A runs and settles at 800 (over its cap: failed, charged 800)
    clock.now = datetime(2027, 1, 16, 0, 0, tzinfo=UTC).timestamp()
    pending[1]()  # B on the next calendar day: its charge day already holds 800 + 500
    runs = {r["job_id"]: r for r in scheduler.runs()}
    assert runs["a"]["tokens"] == 800
    assert (runs["b"]["status"], runs["b"]["reason"]) == ("cancelled", "budget")
    scheduler.stop()


def test_a_deny_mode_run_never_shows_awaiting_approval(tmp_path):
    """W5 gate (attack A7 seen 'awaiting_approval'): an unattended job that hits a call
    needing a person is stopped, it does not wait — between ``require_approval`` and the
    run's end its status stays ``running`` (only a ``wait`` job is ``awaiting_approval``),
    so a reader polling for the end never sees a waiting state that is not one."""
    from stiracore.scheduler.scheduler import RunOutcome

    clock = Clock()
    seen = []
    holder = {}

    def executor(ctx):
        ctx.require_approval("shell", {"command": "x"})
        seen.append(holder["scheduler"].runs()[0]["status"])
        return RunOutcome("failed", "approval_required")

    scheduler = Scheduler(
        tmp_path,
        now_fn=clock,
        breaker_level=lambda: "normal",
        agent_executor=executor,
        dispatch=lambda work: work(),
    )
    holder["scheduler"] = scheduler
    scheduler.start(background=False)
    scheduler.create_job(AGENT_JOB, by=HUMAN, confirm=True)
    _tick_at(scheduler, clock, T0 + 3600)
    assert seen == ["running"]
    run = scheduler.runs()[0]
    assert (run["status"], run["reason"]) == ("failed", "approval_required")
    assert _events(scheduler, "run_awaiting_approval")  # the stop is still recorded
    scheduler.stop()
