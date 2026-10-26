"""Plan files of memory jobs (ADR 0018 phase 0) under delete requests and retention.

The plan files are written by the real executor through the real scheduler (fake model), in a
data directory laid out as the server lays it out. After a delete request no file of the data
directory may name a forgotten entry's key in ``memory-plans/`` or hold its text; retention
lists old plan files by default (90 days) and removes them only with ``--apply``, archived
and restorable. Each rule has a mutation check: the rule removed with ``monkeypatch`` and the
same assertion fails.
"""

from __future__ import annotations

import base64
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from stiracore.control import memory_forget, memory_retention
from stiracore.control.memory_forget import ForgetRequest, apply_forget, plan_forget
from stiracore.control.memory_jobs import MemoryJobExecutor, ModelReply
from stiracore.control.memory_layout import DataLayout
from stiracore.control.memory_plans import PLAN_MAX_AGE_DAYS_DEFAULT, PLANS_DIRNAME
from stiracore.control.memory_removal import restore
from stiracore.control.memory_retention import (
    RetentionPolicy,
    apply_retention,
    retention_report,
)
from stiracore.control.removal_archive import RemovalArchive
from stiracore.memory_autonomy import settings_from
from stiracore.scheduler import Scheduler
from stiracore.tools.episodic_formatter import record_episode
from stiracore.tools.memory_store import MemoryLayer, MemoryStore

T0 = 1_800_000_000.0
KINDS = ("memory_rate_backfill", "memory_consolidate", "memory_tidy")
SUMMARY = "Consolidated: Plan-Summary-Canary-4471 garden facts."
EPISODE = "chat-2"
GARDEN = ("knowledge/general/k0", "knowledge/general/k1")
NOW = datetime.now(tz=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = T0

    def __call__(self) -> float:
        return self.now


def _model(system: str, prompt: str, max_output_tokens: int) -> ModelReply:
    if '"summary"' in prompt:
        return ModelReply(json.dumps({"summary": SUMMARY}), 100, 20)
    return ModelReply(json.dumps({"importance": 0.6, "reason": "fact"}), 100, 20)


def _data(tmp_path: Path) -> tuple[Path, MemoryStore]:
    data = (tmp_path / "stira").resolve()
    data.mkdir()
    store = MemoryStore(data / "memory", "stira")
    for index in range(1, 4):
        record_episode(store, f"Question {index}?", f"Answer {index}.", key=f"chat-{index}")
    for key, text in zip(
        GARDEN,
        (
            "garden tomatoes watering schedule morning",
            "garden tomatoes watering schedule evening",
        ),
        strict=True,
    ):
        store.write(MemoryLayer.SEMANTIC, key, text, {"title": key})
    store.write(MemoryLayer.SEMANTIC, "knowledge/general/k2", "release checklist", {})
    store.search("refresh")
    return data, store


def _run_jobs(tmp_path: Path, store: MemoryStore) -> None:
    """One run of every kind: the plan files a phase-0 server leaves."""
    executor = MemoryJobExecutor(
        store,
        settings=lambda: settings_from({"phase": 0, "jobs": dict.fromkeys(KINDS, True)}, {}),
        model_factory=lambda kind: _model,
        breaker_level=lambda: "normal",
        owner_held=lambda: True,
        now_fn=lambda: T0,
    )
    clock = Clock()
    scheduler = Scheduler(
        tmp_path / "state",
        now_fn=clock,
        breaker_level=lambda: "normal",
        dispatch=lambda work: work(),
        memory_executor=executor,
        memory_kinds=frozenset(KINDS),
        memory_daily_token_budget=100_000,
    )
    scheduler.start(background=False)
    try:
        for kind in KINDS:
            scheduler.create_job(
                {
                    "job_id": kind.replace("_", "-"),
                    "name": kind,
                    "layer": "memory",
                    "action": kind,
                    "schedule": {"type": "interval", "every_sec": 600, "anchor": T0},
                },
                by="local:operator",
                confirm=True,
            )
        clock.now += 600
        scheduler.tick()
        assert {run["status"] for run in scheduler.runs()} == {"completed"}
    finally:
        scheduler.stop()
        executor.close()


def _plans(store: MemoryStore) -> dict[str, str]:
    root = store.memory_dir / PLANS_DIRNAME
    return {
        path.relative_to(root).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def _holding(data: Path, needle: str) -> set[str]:
    return {
        path.relative_to(data).as_posix()
        for path in data.rglob("*")
        if path.is_file()
        and not path.is_symlink()
        and needle.casefold() in path.read_bytes().decode("utf-8", "replace").casefold()
    }


def _naming(store: MemoryStore, *keys: str) -> set[str]:
    return {
        name
        for name, text in _plans(store).items()
        if any(json.dumps(key, ensure_ascii=False) in text for key in keys)
    }


@pytest.fixture
def built(tmp_path):
    data, store = _data(tmp_path)
    _run_jobs(tmp_path, store)
    assert sorted(name.split("/")[0] for name in _plans(store)) == sorted(KINDS)
    return data, store


# -- a delete request reaches the plan files ---------------------------------------------------


def test_delete_by_id_removes_every_plan_file_naming_the_entry(built):
    data, store = built
    assert _naming(store, EPISODE) == {
        name for name in _plans(store) if name.startswith("memory_rate_backfill/")
    }
    layout = DataLayout.at(data)
    plan = plan_forget(layout, ForgetRequest.parse([f"episodic/{EPISODE}"], []))
    assert len(plan.report()["reached"]["memory_plans"]) == 1
    result = apply_forget(layout, plan, at=NOW)
    assert result["job"]["state"] == "completed", result
    assert _naming(store, EPISODE) == set()
    # Plans that do not name the entry stay.
    assert {name.split("/")[0] for name in _plans(store)} == {"memory_consolidate", "memory_tidy"}


def test_delete_of_a_source_removes_the_derived_summary_everywhere(built):
    data, store = built
    assert _holding(data, "Plan-Summary-Canary-4471") == {
        f"memory/agents/stira/memory/{PLANS_DIRNAME}/{name}"
        for name in _plans(store)
        if name.startswith("memory_consolidate/")
    }
    layout = DataLayout.at(data)
    result = apply_forget(
        layout, plan_forget(layout, ForgetRequest.parse([f"semantic/{GARDEN[0]}"], [])), at=NOW
    )
    assert result["job"]["state"] == "completed", result
    assert _holding(data, "Plan-Summary-Canary-4471") == set()
    assert _naming(store, GARDEN[0]) == set()


def test_delete_by_query_reaches_plan_text_and_leaves_no_residual(built):
    data, _store = built
    layout = DataLayout.at(data)
    plan = plan_forget(layout, ForgetRequest.parse([], ["Plan-Summary-Canary-4471"]))
    assert list(plan.report()["reached"]) == ["memory_plans"]
    result = apply_forget(layout, plan, at=NOW)
    assert result["job"]["state"] == "completed", result
    assert result["residual"] == []
    assert _holding(data, "Plan-Summary-Canary-4471") == set()


def test_an_interrupted_plan_write_is_reached_too(built):
    data, store = built
    stray = store.memory_dir / PLANS_DIRNAME / "memory_rate_backfill" / ".plan-x"
    stray.write_text(json.dumps({"items": [{"key": EPISODE}]}), encoding="utf-8")
    layout = DataLayout.at(data)
    apply_forget(
        layout, plan_forget(layout, ForgetRequest.parse([f"episodic/{EPISODE}"], [])), at=NOW
    )
    assert not stray.exists()
    assert _naming(store, EPISODE) == set()


def test_mutation_without_the_plan_step_the_key_stays(built, monkeypatch):
    data, store = built
    monkeypatch.setattr(memory_forget, "_plan_memory_plans", lambda *a, **k: None)
    layout = DataLayout.at(data)
    apply_forget(
        layout, plan_forget(layout, ForgetRequest.parse([f"episodic/{EPISODE}"], [])), at=NOW
    )
    assert _naming(store, EPISODE) != set()


def test_mutation_without_key_matching_an_id_request_misses_the_plan(built, monkeypatch):
    data, _store = built
    monkeypatch.setattr(memory_forget, "names_any", lambda text, keys: False)
    layout = DataLayout.at(data)
    plan = plan_forget(layout, ForgetRequest.parse([f"episodic/{EPISODE}"], []))
    assert "memory_plans" not in plan.report()["reached"]


def test_mutation_the_residual_check_fails_a_job_that_skips_plan_files(built, monkeypatch):
    data, _store = built
    monkeypatch.setattr(memory_forget, "_plan_memory_plans", lambda *a, **k: None)
    layout = DataLayout.at(data)
    plan = plan_forget(layout, ForgetRequest.parse([], ["Plan-Summary-Canary-4471"]))
    result = apply_forget(layout, plan, at=NOW)
    assert result["job"]["state"] == "failed"  # memory-plans/ is a reached store


# -- retention ---------------------------------------------------------------------------------


def _age(store: MemoryStore, days: int) -> None:
    moment = (NOW - timedelta(days=days)).timestamp()
    for path in (store.memory_dir / PLANS_DIRNAME).rglob("*.json"):
        os.utime(path, (moment, moment))


def test_retention_default_lists_old_plan_files_and_changes_nothing(built):
    data, store = built
    layout = DataLayout.at(data)
    policy = RetentionPolicy.from_options({})
    assert policy.get("memory_plans", "max_age_days") == PLAN_MAX_AGE_DAYS_DEFAULT == 90
    _age(store, 89)
    report, plan = retention_report(layout, policy, now=NOW)
    assert report["will_remove"]["memory_plans"] == 0 and plan.empty()
    _age(store, 91)
    before = _plans(store)
    report, plan = retention_report(layout, policy, now=NOW)
    assert report["will_remove"]["memory_plans"] == 3
    assert report["stores"]["memory_plans"]["count"] == 3
    assert _plans(store) == before  # the report removes nothing


def test_retention_limit_is_configurable_and_validated(built):
    data, store = built
    _age(store, 8)
    layout = DataLayout.at(data)
    policy = RetentionPolicy.from_options({"memory_plans": {"max_age_days": 7}})
    assert retention_report(layout, policy, now=NOW)[0]["will_remove"]["memory_plans"] == 3
    with pytest.raises(ValueError, match="memory_plans.max_age_days"):
        RetentionPolicy.from_options({"memory_plans": {"max_age_days": 0}})
    with pytest.raises(ValueError, match="unknown setting"):
        RetentionPolicy.from_options({"memory_plans": {"max_files": 3}})


def test_retention_apply_archives_plan_files_and_restore_brings_them_back(built):
    data, store = built
    _age(store, 120)
    before = _plans(store)
    layout = DataLayout.at(data)
    _report, plan = retention_report(layout, RetentionPolicy.from_options({}), now=NOW)
    result = apply_retention(layout, plan, at=NOW)
    assert result["applied"] and result["counts"] == {"memory_plan": 3}, result
    assert _plans(store) == {}
    record = RemovalArchive(layout).load(result["removal_id"])
    assert record.counts() == {"memory_plan": 3}
    assert restore(layout, result["removal_id"]).restored == {"memory_plan": 3}
    assert _plans(store) == before


def test_mutation_without_the_default_no_plan_file_is_a_candidate(built, monkeypatch):
    data, store = built
    _age(store, 120)
    monkeypatch.setattr(memory_retention, "_DEFAULTS", {})
    report, _plan = retention_report(DataLayout.at(data), RetentionPolicy.from_options({}), now=NOW)
    assert report["will_remove"]["memory_plans"] == 0


def test_a_delete_purges_the_archived_copy_of_a_plan_file(built):
    data, store = built
    _age(store, 120)
    layout = DataLayout.at(data)
    _report, plan = retention_report(layout, RetentionPolicy.from_options({}), now=NOW)
    removal_id = apply_retention(layout, plan, at=NOW)["removal_id"]
    plan = plan_forget(layout, ForgetRequest.parse([f"episodic/{EPISODE}"], []))
    assert plan.report()["archive_copies"] == {"removal_records": [removal_id]}
    assert apply_forget(layout, plan, at=NOW)["job"]["state"] == "completed"
    archive = RemovalArchive(layout)
    [kept] = archive.ids()
    items = archive.load(kept).items
    assert len(items) == 2
    texts = [base64.b64decode(item["data_b64"]).decode("utf-8") for item in items]
    assert not any(json.dumps(EPISODE) in text for text in texts)
    assert all(
        json.dumps(EPISODE) not in text for text in _plans(store).values()
    )  # nothing came back
