"""ADR 0018 phase 0: memory jobs (plan files only) and the provable tick guarantee.

Every job kind runs through the real scheduler with a fake model; the whole memory tree
(all three layers, the search index, the emotion records, the forgetting archive) is hashed
before and after each run. Each boundary also has a mutation check: the guard removed or
widened with ``monkeypatch``, and the same assertion fails.
"""

from __future__ import annotations

import ast
import hashlib
import json
import threading
from pathlib import Path

import pytest

from stiracore.control import memory_jobs
from stiracore.control.harvest import store_lock
from stiracore.control.memory_jobs import (
    MemoryJobExecutor,
    ModelNotAllowed,
    ModelReply,
    plan_counts,
)
from stiracore.memory_autonomy import settings_from
from stiracore.memory_origin import ORIGIN_EXTRACTED, record_origin
from stiracore.observability.audit_chain import AuditChain
from stiracore.observability.event_store import SQLiteEventStore
from stiracore.scheduler import Scheduler, SchedulerRefused, parse_job
from stiracore.scheduler import scheduler as scheduler_module
from stiracore.scheduler.jobs import MEMORY_JOB_CAPS, MODEL_CALLING_KINDS, JobError
from stiracore.tools.episodic_formatter import record_episode
from stiracore.tools.memory_store import MemoryLayer, MemoryStore

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
T0 = 1_800_000_000.0
HUMAN = "local:operator"
ALL_KINDS = ("memory_rate_backfill", "memory_consolidate", "memory_tidy")
CANARY = "EPISODE-TEXT-CANARY-0018"


class Clock:
    def __init__(self, now: float = T0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


class Breaker:
    def __init__(self, level: str = "normal") -> None:
        self.level = level

    def __call__(self) -> str:
        return self.level


class FakeModel:
    """Answers per kind; records the thread and the prompts; reports a usage."""

    def __init__(self, *, usage: tuple[int, int] | None = (100, 20), answer=None) -> None:
        self.calls: list[dict] = []
        self.usage = usage
        self.answer = answer
        self.on_call = None

    def __call__(self, system: str, prompt: str, max_output_tokens: int) -> ModelReply:
        self.calls.append(
            {"thread": threading.current_thread().name, "prompt": prompt, "max": max_output_tokens}
        )
        if self.on_call is not None:
            self.on_call(len(self.calls))
        if self.answer is not None:
            text = self.answer
        elif '"summary"' in prompt:
            text = json.dumps({"summary": "Consolidated: the garden facts."})
        else:
            text = json.dumps({"importance": 0.6, "reason": "fact"})
        if self.usage is None:
            return ModelReply(text)
        return ModelReply(text, *self.usage)


def _store(tmp_path) -> MemoryStore:
    store = MemoryStore(tmp_path / "memory", "a")
    for index in range(6):
        record_episode(
            store, f"Question {index} {CANARY}?", f"We decided item {index}.", key=f"chat-{index}"
        )
    for index, text in enumerate(
        (
            "garden tomatoes watering schedule morning",
            "garden tomatoes watering schedule evening",
            "release checklist deploy steps",
        )
    ):
        store.write(
            MemoryLayer.SEMANTIC, f"knowledge/general/k{index}", text, {"title": f"k{index}"}
        )
    store.write(MemoryLayer.SEMANTIC, "staging/s0", "staged", {"source_fingerprint": "f" * 64})
    store.write(MemoryLayer.SEMANTIC, "staging/s1", "staged", {"source_fingerprint": "f" * 64})
    store.write(MemoryLayer.PROCEDURAL, "rule", "1. do it", {"source_turn": "t"})
    (store.memory_dir / "emotion").mkdir(exist_ok=True)
    (store.memory_dir / "emotion" / "chat-0.json").write_text("{}", encoding="utf-8")
    store.search("refresh")  # the index as a running server leaves it
    return store


def _tree_hash(store) -> str:
    """Every file of every memory layer, the index, emotion records — not memory-plans/."""
    digest = hashlib.sha256()
    for path in sorted(store.memory_dir.rglob("*")):
        relative = path.relative_to(store.memory_dir).as_posix()
        if relative.startswith(memory_jobs.PLANS_DIRNAME):
            continue
        digest.update(relative.encode())
        if path.is_file():
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def _settings(*kinds, budget=100_000):
    jobs = {kind: kind in kinds for kind in ALL_KINDS}
    return settings_from({"phase": 0, "jobs": jobs, "daily_token_budget": budget}, {})


def _executor(store, model=None, *, settings=None, breaker=None, owner=True, audit=None):
    model = model if model is not None else FakeModel()
    factory_calls: list[str] = []

    def factory(kind):
        factory_calls.append(kind)
        return model

    executor = MemoryJobExecutor(
        store,
        settings=lambda: settings or _settings(*ALL_KINDS),
        model_factory=factory,
        breaker_level=breaker or Breaker(),
        owner_held=lambda: owner,
        audit=audit,
        now_fn=lambda: T0,
    )
    executor.factory_calls = factory_calls
    return executor


def _scheduler(tmp_path, clock, executor, *, kinds=ALL_KINDS, budget=100_000, breaker=None):
    scheduler = Scheduler(
        tmp_path / "state",
        now_fn=clock,
        breaker_level=breaker or Breaker(),
        dispatch=lambda work: work(),
        memory_executor=executor,
        memory_kinds=frozenset(kinds),
        memory_daily_token_budget=budget,
    )
    scheduler.start(background=False)
    return scheduler


def _job(kind, **extra):
    return {
        "job_id": kind.replace("_", "-"),
        "name": kind,
        "layer": "memory",
        "action": kind,
        "schedule": {"type": "interval", "every_sec": 600, "anchor": T0},
        **extra,
    }


def _run(scheduler, clock, kind, **extra):
    scheduler.create_job(_job(kind, **extra), by=HUMAN, confirm=True)
    clock.now += 600
    scheduler.tick()
    return scheduler.runs()[0]


def _plans(store, kind):
    directory = store.memory_dir / memory_jobs.PLANS_DIRNAME / kind
    return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(directory.glob("*.json"))]


# -- plan files only; the memory tree unchanged -----------------------------------------------


@pytest.mark.parametrize("kind", ALL_KINDS)
def test_every_kind_writes_a_plan_file_and_nothing_else(tmp_path, kind):
    store = _store(tmp_path)
    before = _tree_hash(store)
    clock = Clock()
    executor = _executor(store)
    scheduler = _scheduler(tmp_path, clock, executor)
    run = _run(scheduler, clock, kind)
    assert run["status"] == "completed", run
    assert _tree_hash(store) == before
    [plan] = _plans(store, kind)
    assert plan["applied"] is False and plan["phase"] == 0 and plan["kind"] == kind
    assert plan["run_id"] == run["run_id"]
    path = store.memory_dir / memory_jobs.PLANS_DIRNAME / kind / f"{run['run_id']}.json"
    assert path.stat().st_mode & 0o777 == 0o600
    assert plan_counts(executor.plans_dir)[kind] == 1
    assert executor.status.status()["last_runs"][kind]["plan_written"] is True
    scheduler.stop()


def test_plan_contents(tmp_path):
    store = _store(tmp_path)
    clock = Clock()
    scheduler = _scheduler(tmp_path, clock, _executor(store))
    for kind in ALL_KINDS:
        _run(scheduler, clock, kind)
    backfill = _plans(store, "memory_rate_backfill")[0]
    assert backfill["counts"]["proposed"] == 6
    assert {i["rated_by"] for i in backfill["items"]} == {"agent_job"}
    consolidate = _plans(store, "memory_consolidate")[0]
    assert [len(i["sources"]) for i in consolidate["items"]] == [2]  # the two garden notes
    tidy = _plans(store, "memory_tidy")[0]
    assert tidy["counts"]["staging_duplicate_groups"] == 1
    assert tidy["counts"]["model_calls"] == 0
    scheduler.stop()


def test_the_tree_hash_catches_a_write(tmp_path, monkeypatch):
    """Mutation: a job that wrote into a memory layer would fail the check above."""
    store = _store(tmp_path)
    before = _tree_hash(store)
    real = MemoryJobExecutor._write_plan

    def also_writes(self, ctx, kind, items, counts, stopped_by):
        self.store.write(MemoryLayer.SEMANTIC, "staging/oops", "x", {})
        return real(self, ctx, kind, items, counts, stopped_by)

    monkeypatch.setattr(MemoryJobExecutor, "_write_plan", also_writes)
    clock = Clock()
    scheduler = _scheduler(tmp_path, clock, _executor(store))
    _run(scheduler, clock, "memory_tidy")
    assert _tree_hash(store) != before
    scheduler.stop()


# -- only MODEL_CALLING_KINDS build a model ----------------------------------------------------


def test_the_fixed_list_of_kinds_allowed_to_call_a_model():
    assert MODEL_CALLING_KINDS == frozenset({"memory_rate_backfill", "memory_consolidate"})
    assert memory_jobs.MODEL_CALLING_KINDS is MODEL_CALLING_KINDS


def test_a_model_free_kind_never_builds_a_model(tmp_path):
    store = _store(tmp_path)
    executor = _executor(store)
    with pytest.raises(ModelNotAllowed):
        executor._model_for("memory_tidy")
    clock = Clock()
    scheduler = _scheduler(tmp_path, clock, executor)
    assert _run(scheduler, clock, "memory_tidy")["status"] == "completed"
    assert executor.factory_calls == []
    scheduler.stop()


def test_model_list_mutation(tmp_path, monkeypatch):
    executor = _executor(_store(tmp_path))
    monkeypatch.setattr(memory_jobs, "MODEL_CALLING_KINDS", MODEL_CALLING_KINDS | {"memory_tidy"})
    executor._model_for("memory_tidy")  # the list is the only thing that refused it
    assert executor.factory_calls == ["memory_tidy"]


def test_the_scheduler_refuses_tokens_from_a_model_free_kind(tmp_path):
    class Liar:
        def __call__(self, ctx):
            return scheduler_module.RunOutcome("completed", tokens=5)

    clock = Clock()
    scheduler = _scheduler(tmp_path, clock, Liar())
    run = _run(scheduler, clock, "memory_tidy")
    assert (run["status"], run["reason"]) == ("failed", "model_call_not_allowed")
    scheduler.stop()


# -- token caps -----------------------------------------------------------------------------


def test_the_run_cap_stops_before_the_call_that_would_exceed_it(tmp_path):
    store = _store(tmp_path)
    model = FakeModel(usage=(800, 50))
    clock = Clock()
    scheduler = _scheduler(tmp_path, clock, _executor(store, model))
    run = _run(scheduler, clock, "memory_rate_backfill", budget={"max_tokens_per_run": 2_000})
    assert run["status"] == "completed" and run["tokens"] <= 2_000, run
    assert len(model.calls) == 2  # a third call's worst case would not fit
    assert _plans(store, "memory_rate_backfill")[0]["stopped_by"] == "token_cap"
    scheduler.stop()


def test_run_cap_mutation(tmp_path, monkeypatch):
    store = _store(tmp_path)
    model = FakeModel(usage=(800, 50))
    monkeypatch.setattr(memory_jobs._Meter, "fits", lambda self, estimate: True)
    clock = Clock()
    scheduler = _scheduler(tmp_path, clock, _executor(store, model))
    run = _run(scheduler, clock, "memory_rate_backfill", budget={"max_tokens_per_run": 2_000})
    assert len(model.calls) > 2  # without the check the worst case is not kept
    assert run["reason"] == "budget_exceeded"
    scheduler.stop()


def test_a_reported_usage_over_the_cap_fails_and_pauses_the_job(tmp_path):
    store = _store(tmp_path)
    model = FakeModel(usage=(5_000, 100))
    clock = Clock()
    scheduler = _scheduler(tmp_path, clock, _executor(store, model))
    run = _run(scheduler, clock, "memory_rate_backfill", budget={"max_tokens_per_run": 3_000})
    assert (run["status"], run["reason"]) == ("failed", "budget_exceeded")
    assert len(model.calls) == 1
    assert scheduler.jobs()[0]["state"] == "paused"
    scheduler.stop()


def test_an_unknown_usage_is_charged_at_the_worst_case(tmp_path):
    store = _store(tmp_path)
    model = FakeModel(usage=None)
    clock = Clock()
    scheduler = _scheduler(tmp_path, clock, _executor(store, model))
    run = _run(scheduler, clock, "memory_rate_backfill", budget={"max_tokens_per_run": 2_000})
    per_call = len(memory_jobs.SYSTEM_PROMPT) + len(model.calls[0]["prompt"]) + 128 + 64
    assert run["tokens"] >= per_call * len(model.calls) - 64 * len(model.calls)
    assert run["tokens"] <= 2_000
    scheduler.stop()


def test_the_daily_total_across_memory_jobs(tmp_path):
    store = _store(tmp_path)
    clock = Clock()
    scheduler = _scheduler(tmp_path, clock, _executor(store), budget=20_000)
    first = _run(scheduler, clock, "memory_rate_backfill")  # reserves 15,000
    assert first["status"] == "completed"
    scheduler.create_job(_job("memory_consolidate"), by=HUMAN, confirm=True)
    clock.now += 600
    scheduler.tick()
    jobs = {job["job_id"]: job for job in scheduler.jobs()}
    # used + 30,000 reserved would pass 20,000: never started, paused for the budget.
    assert jobs["memory-consolidate"]["state"] == "paused"
    assert "daily_token_budget (20000)" in jobs["memory-consolidate"]["paused_reason"]
    assert [r["job_id"] for r in scheduler.runs()].count("memory-consolidate") == 0
    scheduler.stop()


def test_daily_total_mutation(tmp_path):
    store = _store(tmp_path)
    clock = Clock()
    scheduler = _scheduler(tmp_path, clock, _executor(store), budget=None)
    _run(scheduler, clock, "memory_rate_backfill")
    scheduler.create_job(_job("memory_consolidate"), by=HUMAN, confirm=True)
    clock.now += 600
    scheduler.tick()
    assert [r["job_id"] for r in scheduler.runs()].count("memory-consolidate") == 1
    scheduler.stop()


@pytest.mark.parametrize("kind", ["memory_rate_backfill", "memory_consolidate"])
def test_per_kind_ceilings(kind):
    caps = MEMORY_JOB_CAPS[kind]
    job = parse_job(_job(kind), now=T0, gateway_actions=None, agent_jobs=False, memory_kinds=None)
    assert job.budget.to_dict() == caps
    with pytest.raises(JobError):
        parse_job(
            _job(kind, budget={"max_tokens_per_run": caps["max_tokens_per_run"] + 1}),
            now=T0,
            gateway_actions=None,
            agent_jobs=False,
            memory_kinds=None,
        )


# -- breaker NORMAL only ---------------------------------------------------------------------


def test_a_run_starts_only_at_normal(tmp_path):
    store = _store(tmp_path)
    breaker = Breaker("warning")
    executor = _executor(store, breaker=breaker)
    clock = Clock()
    scheduler = _scheduler(tmp_path, clock, executor, breaker=Breaker())
    run = _run(scheduler, clock, "memory_tidy")
    assert (run["status"], run["reason"]) == ("cancelled", "breaker_warning")
    assert _plans(store, "memory_tidy") == []
    scheduler.stop()


def test_leaving_normal_stops_at_the_item_boundary(tmp_path):
    store = _store(tmp_path)
    breaker = Breaker()
    model = FakeModel()
    model.on_call = lambda count: setattr(breaker, "level", "warning")
    clock = Clock()
    scheduler = _scheduler(tmp_path, clock, _executor(store, model, breaker=breaker))
    run = _run(scheduler, clock, "memory_rate_backfill")
    assert (run["status"], run["reason"]) == ("cancelled", "breaker")
    assert len(model.calls) == 1
    assert _plans(store, "memory_rate_backfill")[0]["stopped_by"] == "breaker"
    scheduler.stop()


def test_breaker_item_check_mutation(tmp_path, monkeypatch):
    store = _store(tmp_path)
    breaker = Breaker()
    model = FakeModel()
    model.on_call = lambda count: setattr(breaker, "level", "warning")
    monkeypatch.setattr(MemoryJobExecutor, "_check_item", lambda self, ctx, started: None)
    clock = Clock()
    scheduler = _scheduler(tmp_path, clock, _executor(store, model, breaker=breaker))
    _run(scheduler, clock, "memory_rate_backfill")
    assert len(model.calls) > 1
    scheduler.stop()


# -- restart: model-calling kinds wait for a person --------------------------------------------


def test_model_calling_jobs_are_paused_after_a_restart(tmp_path):
    store = _store(tmp_path)
    clock = Clock()
    scheduler = _scheduler(tmp_path, clock, _executor(store))
    for kind in ALL_KINDS:
        scheduler.create_job(_job(kind), by=HUMAN, confirm=True)
    scheduler.stop()
    restarted = _scheduler(tmp_path, clock, _executor(store))
    jobs = {job["action"]: job for job in restarted.jobs()}
    for kind in MODEL_CALLING_KINDS:
        assert (jobs[kind]["state"], jobs[kind]["paused_by"]) == ("paused", "system")
        assert jobs[kind]["paused_reason"] == scheduler_module.RESTART_PAUSE_REASON
    assert jobs["memory_tidy"]["state"] == "active"  # model-free: ADR 0008 behaviour
    with pytest.raises(SchedulerRefused):
        restarted.resume_job("memory-rate-backfill", by="system:scheduler", confirm=True)
    with pytest.raises(SchedulerRefused):
        restarted.resume_job("memory-rate-backfill", by=HUMAN)  # no confirmation
    assert restarted.resume_job("memory-rate-backfill", by=HUMAN, confirm=True)["state"] == "active"
    restarted.stop()


def test_without_the_executor_every_memory_job_is_paused(tmp_path):
    store = _store(tmp_path)
    clock = Clock()
    scheduler = _scheduler(tmp_path, clock, _executor(store))
    scheduler.create_job(_job("memory_tidy"), by=HUMAN, confirm=True)
    scheduler.stop()
    off = _scheduler(tmp_path, clock, None)
    [job] = off.jobs()
    assert (job["state"], job["paused_reason"]) == (
        "paused",
        scheduler_module.MEMORY_OFF_PAUSE_REASON,
    )
    with pytest.raises(SchedulerRefused):
        off.resume_job(job["job_id"], by=HUMAN, confirm=True)
    off.stop()
    # The same for a kind the settings no longer enable (a fresh state directory).
    other = tmp_path / "other"
    first = _scheduler(other, clock, _executor(store))
    first.create_job(_job("memory_tidy"), by=HUMAN, confirm=True)
    first.stop()
    narrowed = _scheduler(other, clock, _executor(store), kinds=("memory_rate_backfill",))
    [job] = narrowed.jobs()
    assert (job["state"], job["paused_reason"]) == (
        "paused",
        scheduler_module.MEMORY_OFF_PAUSE_REASON,
    )
    narrowed.stop()


def test_restart_pause_mutation(tmp_path, monkeypatch):
    store = _store(tmp_path)
    clock = Clock()
    scheduler = _scheduler(tmp_path, clock, _executor(store))
    scheduler.create_job(_job("memory_rate_backfill"), by=HUMAN, confirm=True)
    scheduler.stop()
    monkeypatch.setattr(Scheduler, "_pause_memory_jobs_at_start", lambda self, now: None)
    restarted = _scheduler(tmp_path, clock, _executor(store))
    assert restarted.jobs()[0]["state"] == "active"
    restarted.stop()


# -- creation: a person, confirm:true, fixed kinds only -----------------------------------------


def test_only_a_person_with_confirmation_creates_a_memory_job(tmp_path):
    clock = Clock()
    scheduler = _scheduler(tmp_path, clock, _executor(_store(tmp_path)))
    for by, confirm in (("system:agent", True), ("local:unknown", True), (HUMAN, False)):
        with pytest.raises(SchedulerRefused):
            scheduler.create_job(_job("memory_tidy"), by=by, confirm=confirm)
    for extra in ({"prompt": "do anything"}, {"tools": ["memory_read"]}, {"action": "free"}):
        with pytest.raises(SchedulerRefused):
            scheduler.create_job(_job("memory_tidy", **extra), by=HUMAN, confirm=True)
    scheduler.stop()


def test_a_kind_not_enabled_in_the_settings_cannot_be_created(tmp_path):
    clock = Clock()
    scheduler = _scheduler(tmp_path, clock, _executor(_store(tmp_path)), kinds=("memory_tidy",))
    with pytest.raises(SchedulerRefused):
        scheduler.create_job(_job("memory_consolidate"), by=HUMAN, confirm=True)
    scheduler.create_job(_job("memory_tidy"), by=HUMAN, confirm=True)
    scheduler.stop()
    off = _scheduler(tmp_path / "off", clock, None)
    assert off.status()["memory_job_kinds"] == []
    with pytest.raises(SchedulerRefused):
        off.create_job(_job("memory_tidy"), by=HUMAN, confirm=True)
    off.stop()


def test_a_job_cannot_create_or_widen_a_job(tmp_path):
    clock = Clock()
    created = {}

    class SelfExtending:
        def __call__(self, ctx):
            try:
                ctx.scheduler.create_job(_job("memory_consolidate"), by=HUMAN, confirm=True)
            except SchedulerRefused as error:
                created["code"] = error.code
            return scheduler_module.RunOutcome("completed")

    scheduler = _scheduler(tmp_path, clock, SelfExtending())
    _run(scheduler, clock, "memory_tidy")
    assert created == {"code": "scope"}
    scheduler.stop()


def test_agent_tools_offer_no_job_or_retention_control():
    from stiracore.tools import tool_registry

    names = set(tool_registry.READ_TOOLS) | set(tool_registry.WRITE_TOOLS)
    for name in names:
        assert not any(word in name for word in ("schedule", "job", "retention", "forget")), name
    # The rating tool's module never reaches the scheduler, retention or forgetting code.
    source = (SRC / "stiracore/tools/memory_rate_tool.py").read_text(encoding="utf-8")
    source += (SRC / "stiracore/memory_autonomy.py").read_text(encoding="utf-8")
    for module in ("scheduler", "memory_importance", "memory_maintenance", "memory_retention"):
        assert f"import {module}" not in source and f".{module} import" not in source, module


# -- one owner, own thread -------------------------------------------------------------------


def test_one_owner(tmp_path):
    store = _store(tmp_path)
    clock = Clock()
    scheduler = _scheduler(tmp_path, clock, _executor(store, owner=False))
    run = _run(scheduler, clock, "memory_tidy")
    assert (run["status"], run["reason"]) == ("failed", "data_dir_not_owned")
    scheduler.stop()
    lock = store_lock(store)
    with lock:
        holder = threading.Thread(target=lambda: None)
        holder.start()
        holder.join()
        executor = _executor(store)
        clock = Clock()
        busy = _scheduler(tmp_path / "b", clock, executor)
        # The Harvest lock is re-entrant per thread; the run happens on the executor's own
        # thread, so a sweep held here blocks it.
        run = _run(busy, clock, "memory_tidy")
        assert (run["status"], run["reason"]) == ("cancelled", "harvest_in_progress")
        busy.stop()
    mutex = memory_jobs._job_mutex(store)
    with mutex:
        clock = Clock()
        busy = _scheduler(tmp_path / "c", clock, _executor(store))
        run = _run(busy, clock, "memory_tidy")
        assert (run["status"], run["reason"]) == ("cancelled", "memory_job_in_progress")
        busy.stop()


def test_runs_on_the_executors_own_thread(tmp_path):
    store = _store(tmp_path)
    model = FakeModel()
    clock = Clock()
    scheduler = _scheduler(tmp_path, clock, _executor(store, model))
    _run(scheduler, clock, "memory_rate_backfill")
    assert {call["thread"] for call in model.calls} and all(
        call["thread"].startswith("stira-memory-job") for call in model.calls
    )
    scheduler.stop()


# -- what is sent; strict output -------------------------------------------------------------


def test_the_prompt_is_masked_data_and_bad_output_is_refused_per_item(tmp_path):
    store = MemoryStore(tmp_path / "memory", "a")
    record_episode(store, "my key sk-ant-api03-" + "A" * 40, "noted", key="chat-0")
    model = FakeModel(answer='Sure! {"importance": 0.9, "reason": "fact"}')
    clock = Clock()
    scheduler = _scheduler(tmp_path, clock, _executor(store, model))
    _run(scheduler, clock, "memory_rate_backfill")
    assert "sk-ant-api03-" + "A" * 40 not in model.calls[0]["prompt"]
    [plan] = _plans(store, "memory_rate_backfill")
    assert plan["items"] == [{"key": "chat-0", "code": "E_OUTPUT_SCHEMA"}]
    scheduler.stop()


def test_an_unverified_episode_is_not_sent(tmp_path):
    store = MemoryStore(tmp_path / "memory", "a")
    store.write(MemoryLayer.EPISODIC, "hand", "edited by hand", {})
    record_episode(store, "q", "a", key="chat-1")
    record_origin(store, MemoryLayer.EPISODIC, "chat-1", ORIGIN_EXTRACTED, "web-chat")
    model = FakeModel()
    clock = Clock()
    scheduler = _scheduler(tmp_path, clock, _executor(store, model))
    _run(scheduler, clock, "memory_rate_backfill")
    assert [i["key"] for i in _plans(store, "memory_rate_backfill")[0]["items"]] == ["chat-1"]
    scheduler.stop()


# -- audit rows: kind, ids, counts --------------------------------------------------------------


def test_job_audit_rows_carry_no_text(tmp_path):
    store = _store(tmp_path)
    events = SQLiteEventStore(tmp_path / "events.db")
    clock = Clock()
    executor = _executor(store, audit=AuditChain(events).record_memory)
    scheduler = _scheduler(tmp_path, clock, executor)
    for kind in ALL_KINDS:
        _run(scheduler, clock, kind)
    rows = [e for e in events.get_events(limit=100) if e.event_type.startswith("audit.memory.")]
    assert len(rows) >= 3 and {e.event_type for e in rows} == {"audit.memory.planned"}
    assert {e.attributes["kind"] for e in rows} == set(ALL_KINDS)
    dumped = json.dumps([e.attributes for e in rows])
    for needle in (CANARY, "chat-", "garden", "Consolidated", "knowledge/", str(store.memory_dir)):
        assert needle not in dumped, needle
    for row in rows:
        assert set(row.attributes) <= {"kind", "job_id", "run_id", "reason_code", "counts"}
        assert all(type(v) is int for v in row.attributes["counts"].values())
    scheduler.stop()


# -- the heartbeat tick: provably model-free ------------------------------------------------------

FORBIDDEN = (
    "strands",
    "stiracore.providers",
    "stiracore.agent",
    "stiracore.scheduler",
    "stiracore.control",
    "stiracore.memory_autonomy",
    "stiracore.memory_rating",
)


def _module_path(src: Path, name: str) -> Path | None:
    base = src.joinpath(*name.split("."))
    if (base / "__init__.py").is_file():
        return base / "__init__.py"
    if base.with_suffix(".py").is_file():
        return base.with_suffix(".py")
    return None


def _imports(src: Path, name: str) -> set[str]:
    """Modules ``name`` imports at module level (relative imports resolved)."""
    path = _module_path(src, name)
    tree = ast.parse(path.read_text(encoding="utf-8"))
    package = name if path.name == "__init__.py" else name.rpartition(".")[0]
    found: set[str] = set()
    for node in tree.body:
        for inner in [node] if not isinstance(node, (ast.If, ast.Try)) else list(ast.walk(node)):
            if isinstance(inner, ast.Import):
                found |= {alias.name for alias in inner.names}
            elif isinstance(inner, ast.ImportFrom):
                if inner.level:
                    parts = package.split(".")[: len(package.split(".")) - (inner.level - 1)]
                    module = ".".join(parts + ([inner.module] if inner.module else []))
                else:
                    module = inner.module or ""
                found.add(module)
                found |= {
                    f"{module}.{alias.name}"
                    for alias in inner.names
                    if _module_path(src, f"{module}.{alias.name}")
                }
    return found


def _closure(src: Path, start: str) -> set[str]:
    """Everything the heartbeat package imports, transitively, inside the source tree (the
    root ``stiracore`` facade excluded: it is a re-export list, not a dependency)."""
    seen: set[str] = set()
    todo = [start]
    while todo:
        name = todo.pop()
        if name in seen:
            continue
        seen.add(name)
        for module in _imports(src, name):
            parts = module.split(".")
            if parts[0] != "stiracore":
                seen.add(module)
                continue
            for end in range(2, len(parts) + 1):
                candidate = ".".join(parts[:end])
                if _module_path(src, candidate) and candidate not in seen:
                    todo.append(candidate)
    return seen


def _forbidden(modules: set[str]) -> set[str]:
    return {m for m in modules if any(m == f or m.startswith(f + ".") for f in FORBIDDEN)}


def test_the_heartbeat_import_graph_reaches_no_model_or_memory_job_code():
    modules = _closure(SRC, "stiracore.heartbeat")
    assert "stiracore.heartbeat.gateway" in modules  # the walk works
    assert _forbidden(modules) == set(), _forbidden(modules)


def test_import_graph_mutation(tmp_path):
    """The same walk finds one added import of the memory job module."""
    import shutil

    copy = tmp_path / "src"
    shutil.copytree(SRC / "stiracore", copy / "stiracore")
    pacing = copy / "stiracore" / "heartbeat" / "pacing.py"
    pacing.write_text(
        pacing.read_text(encoding="utf-8") + "\nfrom stiracore.control import memory_jobs\n",
        encoding="utf-8",
    )
    assert "stiracore.control.memory_jobs" in _forbidden(_closure(copy, "stiracore.heartbeat"))


# -- measured on the chat server: a tick with memory autonomy on --------------------------------


def test_stira_tick_with_memory_autonomy_on_reaches_no_memory_job_or_model(tmp_path, monkeypatch):
    """The chat server with memory autonomy, every job kind, the scheduler and the heartbeat
    on: one tick reaches neither the memory executor, its model factory, a Strands Agent nor
    any model (tripwires that raise, not Mocks that answer)."""
    from unittest.mock import Mock

    from strands.agent import agent as strands_agent

    from dashboard import stira_server
    from stiracore.agent import nxp_agent, stimulus_classifier
    from stiracore.agent.stimulus_classifier import StimulusResult
    from stiracore.observability.config_api import ConfigStore

    hits: list[str] = []

    def tripwire(name):
        def fired(*_args, **_kwargs):
            hits.append(name)
            raise AssertionError(f"{name} reached")

        return fired

    monkeypatch.setattr(stira_server, "DATA_DIR", tmp_path / "data")
    monkeypatch.setenv("STIRA_SCHEDULER", "1")
    monkeypatch.delenv("STIRA_MEMORY_AUTONOMY", raising=False)
    model = Mock(side_effect=tripwire("model()"))
    for method in ("stream", "structured_output", "converse"):
        getattr(model, method).side_effect = tripwire(f"model.{method}")
    monkeypatch.setattr(nxp_agent, "BedrockModel", Mock(return_value=model))
    monkeypatch.setattr(
        stimulus_classifier,
        "get_classifier",
        lambda **_kw: Mock(model_id=None, classify=Mock(return_value=StimulusResult("joy", 0.5))),
    )
    monkeypatch.setattr(MemoryJobExecutor, "__call__", tripwire("memory executor"))
    monkeypatch.setattr(MemoryJobExecutor, "_model_for", tripwire("memory model"))
    monkeypatch.setattr(
        stira_server,
        "distiller_model_factory",
        lambda *a, **k: tripwire("memory model factory"),
    )
    store = ConfigStore(
        tmp_path / "gateway-config.json",
        tmp_path / "agent-config.json",
        tmp_path / "tool-config.json",
    )
    store.gateway_path.write_text(json.dumps({"hb_enabled": True}), encoding="utf-8")
    store.agent_path.write_text(
        json.dumps({"memory_autonomy": {"phase": 0, "jobs": dict.fromkeys(ALL_KINDS, True)}}),
        encoding="utf-8",
    )
    app = stira_server.create_app(config_store=store)
    try:
        monkeypatch.setattr(strands_agent.Agent, "__init__", tripwire("strands Agent"))
        scheduler = app.extensions["scheduler"]
        assert scheduler.status()["memory_job_kinds"] == sorted(ALL_KINDS)
        daemon = app.extensions["heartbeat"]
        assert daemon is not None and daemon.running
        daemon._on_tick(daemon.gateway.tick())
        assert hits == []
        status = app.test_client().get("/api/gateway/status").get_json()["memory_autonomy"]
        assert status["enabled"] and status["phase"] == 0 and status["in_turn_rating"]
        assert status["ratings"]["total"] == 0 and status["plans"] == dict.fromkeys(ALL_KINDS, 0)
    finally:
        if app.extensions.get("heartbeat") is not None:
            app.extensions["heartbeat"].stop()
        if app.extensions.get("scheduler") is not None:
            app.extensions["scheduler"].stop()


def test_stira_without_the_block_has_no_memory_kind_and_no_status(tmp_path, monkeypatch):
    from unittest.mock import Mock

    from dashboard import stira_server
    from stiracore.agent import nxp_agent, stimulus_classifier
    from stiracore.agent.stimulus_classifier import StimulusResult
    from stiracore.observability.config_api import ConfigStore

    monkeypatch.setattr(stira_server, "DATA_DIR", tmp_path / "data")
    monkeypatch.setenv("STIRA_SCHEDULER", "1")
    monkeypatch.setattr(nxp_agent, "BedrockModel", Mock(return_value=Mock()))
    monkeypatch.setattr(
        stimulus_classifier,
        "get_classifier",
        lambda **_kw: Mock(model_id=None, classify=Mock(return_value=StimulusResult("joy", 0.5))),
    )
    store = ConfigStore(
        tmp_path / "gateway-config.json",
        tmp_path / "agent-config.json",
        tmp_path / "tool-config.json",
    )
    app = stira_server.create_app(config_store=store)
    try:
        scheduler = app.extensions["scheduler"]
        assert scheduler.status()["memory_job_kinds"] == []
        with pytest.raises(SchedulerRefused):
            scheduler.create_job(_job("memory_tidy"), by=HUMAN, confirm=True)
        assert app.test_client().get("/api/gateway/status").get_json()["memory_autonomy"] is None
    finally:
        if app.extensions.get("scheduler") is not None:
            app.extensions["scheduler"].stop()
