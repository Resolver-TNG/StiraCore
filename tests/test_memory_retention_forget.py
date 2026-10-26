"""Retention report and delete requests that reach every copy (#294).

The data directory is built in ``tmp_path`` with the real stores (``MemoryStore`` /
``record_episode``, ``FileCheckpointStore``, ``TurnEventStore``, the context stash
directory, the #320 forgetting archive, a retention removal record, the
staging-rejection ledger) and one canary phrase spread over all of them; the tests
then read the raw bytes of every file. The counter-example they guard against: a
delete that removes the entry but leaves its text in a history table, an archive, a
derived summary or a checkpoint.
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from stiracore.config.data_dir_lock import DataDirOwnerLock
from stiracore.config.models import AgentConfig
from stiracore.control import memory_forget
from stiracore.control.distillers import RuleBasedDistiller
from stiracore.control.forgetting_archive import ARCHIVE_DIRNAME, AUDIT_LOG_NAME
from stiracore.control.harvest import HarvestScheduler, write_semantic_index
from stiracore.control.memory_cli import main as memory_main
from stiracore.control.memory_forget import (
    ForgetRequest,
    apply_forget,
    list_jobs,
    plan_forget,
    residual,
    resume_forget,
)
from stiracore.control.memory_layout import DataLayout
from stiracore.control.memory_maintenance import MemoryMaintenance
from stiracore.control.memory_removal import RemovalPlan, execute, restore
from stiracore.control.memory_retention import (
    RetentionPolicy,
    apply_retention,
    retention_report,
)
from stiracore.control.removal_archive import RemovalArchive
from stiracore.core.amygdala import AmygdalaPass
from stiracore.core.checkpoint import Checkpoint, FileCheckpointStore
from stiracore.core.neural_state import DIMS, EMOTION_MATRIX, NeuralState
from stiracore.diagnostics.__main__ import main as stira_main
from stiracore.observability.turn_events import TurnEventStore
from stiracore.tools.episodic_emotion import (
    delete_orphan_records,
    emotion_record_path,
    find_orphan_records,
    normalize_snapshot,
)
from stiracore.tools.episodic_formatter import record_episode
from stiracore.tools.memory_store import MemoryLayer, MemoryStore
from stiracore.tools.memory_tools import create_memory_tools

CANARY = "Zebra-Canary-7781"
NOW = datetime.now(tz=UTC)
ARCHIVE = f"memory/agents/stira/memory/{ARCHIVE_DIRNAME}"
NOT_REACHED = {"scheduler-events.jsonl", "workspace/notes.txt"}


def _files(data: Path) -> dict[str, bytes]:
    found = {}
    for path in sorted(data.rglob("*")):
        if path.is_file() and not path.is_symlink():
            found[path.relative_to(data).as_posix()] = path.read_bytes()
    return found


def _holding(data: Path, needle: str = CANARY) -> set[str]:
    """Files whose raw bytes contain the needle (case-insensitive)."""
    return {
        name
        for name, raw in _files(data).items()
        if needle.casefold() in raw.decode("utf-8", "replace").casefold()
    }


def _emotion():
    return normalize_snapshot(
        {
            "state": dict.fromkeys(DIMS, 0.5),
            "category": next(iter(EMOTION_MATRIX)),
            "intensity": 0.4,
            "breaker": "normal",
            "turn": 3,
            "turn_id": "t-x",
        }
    )


def _digests(paths) -> set[str]:
    from stiracore.control.forgetting_archive import sha256_text

    return {sha256_text(path) for path in paths}


def _where(result: dict) -> set[str]:
    """The residual of a job output: digests and areas only, never a path."""
    assert all("where" not in item for item in result["residual"])
    return {item["where_sha256"] for item in result["residual"]}


def _build(tmp_path: Path, *, removal_record: bool = True) -> tuple[Path, MemoryStore, dict]:
    data = (tmp_path / "stira").resolve()
    data.mkdir()
    store = MemoryStore(data / "memory", "stira")
    keys: dict[str, str] = {}
    plain = record_episode(
        store, "How is the weather today?", "Sunny, I think.", key="chat-1", emotion=_emotion()
    )
    # #364: the episode's emotion values live in emotion/<digest>.json (no text).
    canary_ep = record_episode(
        store,
        f"My locker code word is {CANARY}, keep it.",
        "Noted.",
        key="chat-2",
        emotion=_emotion(),
    )
    # One orphan cleaned up, so emotion-log.jsonl exists (file names and reasons only).
    orphan = record_episode(store, "temporary", "gone", key="chat-orphan", emotion=_emotion())
    store._path(MemoryLayer.EPISODIC, orphan.key).unlink()
    assert delete_orphan_records(store, find_orphan_records(store))
    keys["plain"], keys["episode"] = plain.key, canary_ep.key
    # Derived copies: a staged and an approved Semantic entry.
    store.write(
        MemoryLayer.SEMANTIC,
        "staging/harvest-locker",
        "The user has a locker code word.",
        {"title": "locker", "source_id": canary_ep.key},
    )
    store.write(
        MemoryLayer.SEMANTIC,
        "knowledge/general/harvest-word",
        f"Code word: {CANARY}.",
        {"title": "word", "source_id": "chat-other"},
    )
    store.write(MemoryLayer.SEMANTIC, "knowledge/general/keep", "Unrelated fact.", {"title": "k"})
    write_semantic_index(store)
    # A forgetting step of #320 archived an original that holds the canary.
    long_body = "Head.\n" + "x" * 3_000 + f" {CANARY} " + "y" * 3_000 + "\nTail."
    store.write(MemoryLayer.SEMANTIC, "facts/old", long_body, {"title": "old"})
    with sqlite3.connect(store.index_path) as connection:
        connection.execute(
            "UPDATE memory_entries SET last_accessed = ? WHERE key = 'facts/old'",
            ((NOW - timedelta(days=120)).timestamp(),),
        )
    assert MemoryMaintenance(store).run_forgetting(now=NOW, apply=True).applied == 1
    if removal_record:
        # An earlier retention run archived an episode holding the canary (and another).
        store.write(MemoryLayer.EPISODIC, "old-a", f"old note on {CANARY}", {"harvested": True})
        store.write(MemoryLayer.EPISODIC, "old-b", "old unrelated note", {"harvested": True})
        layout = DataLayout.at(data)
        plan = RemovalPlan(
            memory=[(MemoryLayer.EPISODIC, "old-a"), (MemoryLayer.EPISODIC, "old-b")]
        )
        result = execute(layout, plan, purpose="retention", reason="test", at=NOW)
        assert result.applied
        keys["removal"] = result.record_id
    ledger = store.memory_dir / "staging_rejections.jsonl"
    ledger.write_text(
        json.dumps({"key": "staging/x", "source_id": canary_ep.key, "reason": "no"})
        + "\n"
        + json.dumps({"key": "staging/y", "source_id": "chat-1", "reason": "dup"})
        + "\n",
        encoding="utf-8",
    )
    # Conversation copies: a checkpoint, a stash file, turns.db rows.
    checkpoints = FileCheckpointStore(data / "checkpoints")
    history = [{"role": "user", "content": [{"text": f"My locker code word is {CANARY}"}]}]
    keys["checkpoint"] = checkpoints.save(
        Checkpoint(NeuralState(), history, [], time.time(), "stira")
    )
    checkpoints.save(Checkpoint(NeuralState(), [], [], time.time() + 1, "stira"))
    stash = data / "context-stash"
    stash.mkdir(mode=0o700)
    (stash / "tool-result-1").write_text(f"output mentions {CANARY}", encoding="utf-8")
    (stash / "tool-result-2").write_text("other output", encoding="utf-8")
    turns = TurnEventStore(data / "turns.db")
    turns.append("t-1", "turn.started", {})
    turns.append(
        "t-1",
        "tool_call.created",
        {"tool_use_id": "u1", "name": "memory_read", "status": "done", "output_preview": CANARY},
    )
    turns.put_payload("t-1", "u1", "output", {"text": f"memory says {CANARY}"})
    turns.append("t-2", "turn.started", {})
    turns.put_payload("t-2", "u2", "output", {"text": "nothing here"})
    turns.close()
    # Not reached by design: a hash-chained log and the agent's workspace.
    (data / "scheduler-events.jsonl").write_text(
        json.dumps({"seq": 1, "ts": time.time(), "kind": "x", "body": {"s": CANARY}}) + "\n",
        encoding="utf-8",
    )
    (data / "workspace").mkdir()
    (data / "workspace" / "notes.txt").write_text(CANARY, encoding="utf-8")
    return data, store, keys


# -- delete requests -------------------------------------------------------------


def test_dry_run_finds_every_copy_and_changes_nothing(tmp_path):
    data, _store, keys = _build(tmp_path)
    assert emotion_record_path(_store, keys["episode"]).is_file()
    before = _files(data)
    layout = DataLayout.at(data)

    report = plan_forget(layout, ForgetRequest.parse([], [CANARY.lower()])).report()

    assert _files(data) == before
    reached = report["reached"]
    assert reached["episodic"] == [keys["episode"]]
    emotion = emotion_record_path(_store, keys["episode"]).relative_to(data).as_posix()
    assert reached["emotion_records"] == [emotion]
    assert reached["semantic_staging"] == ["staging/harvest-locker"]  # derived (source_id)
    # facts/old was compressed by #320 and no longer holds the phrase, but its archived
    # original does: it is the same memory, so the compressed entry goes too.
    assert reached["semantic"] == ["facts/old", "knowledge/general/harvest-word"]
    assert reached["semantic_index_md"] == ["index"]
    assert "episodic/chat-2" in reached["search_index_rows"]
    assert reached["staging_rejections"] == ["line 1"]
    assert reached["checkpoint"] == [f"checkpoints/{keys['checkpoint']}.json"]
    assert reached["context_stash"] == ["context-stash/tool-result-1"]
    assert reached["tool_payloads"] == ["t-1/u1/output"]
    assert len(reached["turn_events"]) == 1
    assert len(report["archive_copies"]["forgetting_records"]) == 1
    assert report["archive_copies"]["removal_records"] == [keys["removal"]]
    assert {item["where"] for item in report["not_reached"]} == NOT_REACHED
    # The request itself is never printed back, only its digest.
    assert CANARY.lower() not in json.dumps(report).lower()


def test_delete_leaves_no_copy_in_index_archive_history_or_summaries(tmp_path):
    """The counter-example: no "old value" of the deleted text survives anywhere."""
    data, store, keys = _build(tmp_path)
    assert {n for n in _holding(data) if n.startswith(ARCHIVE)}  # archived copies exist
    layout = DataLayout.at(data)
    plan = plan_forget(layout, ForgetRequest.parse([], [CANARY]))

    result = apply_forget(layout, plan, at=NOW)

    assert result["error"] is None and result["job"]["state"] == "completed"
    summary = result["job"]["summary"]
    assert {name: counts["done"] for name, counts in summary.items()} == {
        "checkpoint": 1,
        "compact": 2,
        "context_stash": 1,
        "emotion_record": 1,
        "forgetting_record": 1,
        "index_row": 4,
        "memory": 4,
        "removal_record": 1,
        "semantic_index_md": 1,
        "staging_rejection": 1,
        "tool_payload": 1,
        "turn_event": 1,
    }
    # Raw bytes of every file: only the places that are not reached still hold it —
    # not the index (SQLite free pages, WAL), the archive, the job file, the audit log.
    assert _holding(data) == NOT_REACHED
    assert _where(result) == _digests(NOT_REACHED)
    assert store.search(CANARY) == []
    assert "harvest-locker" not in store.read_text(MemoryLayer.SEMANTIC, "index")
    # The emotion record (#364) of the deleted episode is gone too; the other one stays,
    # and emotion-log.jsonl (an audit log, not edited) never held the deleted text.
    assert not emotion_record_path(store, keys["episode"]).exists()
    assert emotion_record_path(store, keys["plain"]).is_file()
    emotion_log = (store.memory_dir / "emotion-log.jsonl").read_text(encoding="utf-8")
    assert emotion_log and CANARY.casefold() not in emotion_log.casefold()
    assert keys["episode"] not in emotion_log
    # What was not asked for stays.
    assert store.read_text(MemoryLayer.EPISODIC, keys["plain"]) is not None
    assert store.read_text(MemoryLayer.SEMANTIC, "knowledge/general/keep") is not None
    assert (data / "context-stash" / "tool-result-2").exists()
    ledger = (store.memory_dir / "staging_rejections.jsonl").read_text(encoding="utf-8")
    assert keys["episode"] not in ledger and "chat-1" in ledger
    # The older retention record was rewritten without the match: its other item stays
    # restorable, the deleted one can never come back through it (no resurrection).
    archive = RemovalArchive(layout)
    (rewritten,) = archive.ids()
    assert rewritten != keys["removal"]
    assert [item["key"] for item in archive.load(rewritten).items] == ["old-b"]
    assert restore(layout, rewritten).refused is None
    assert store.read_text(MemoryLayer.EPISODIC, "old-b") is not None
    assert _holding(data) == NOT_REACHED
    # Audit: started/completed with the query's digest, never its text.
    events = [json.loads(line) for line in layout.audit_log.read_text().splitlines()]
    kinds = [event["kind"] for event in events]
    assert "delete.started" in kinds and "delete.completed" in kinds
    assert kinds.count("purge.applied") == 2
    job_file = data / ARCHIVE / f"delete-{result['job']['id']}.json"
    job = json.loads(job_file.read_text())
    assert job["state"] == "completed" and job["steps"] == [] and job["attempts"] == 1
    assert stat.S_IMODE(job_file.stat().st_mode) == 0o600


def test_a_failed_delete_job_resumes_without_bringing_anything_back(tmp_path, monkeypatch):
    data, store, keys = _build(tmp_path)
    layout = DataLayout.at(data)
    plan = plan_forget(layout, ForgetRequest.parse([], [CANARY]))
    real = memory_forget._run_step

    def flaky(layout, store, archive, step, at):
        if step["store"] in ("checkpoint", "forgetting_record"):
            raise OSError("disk went away")
        return real(layout, store, archive, step, at)

    monkeypatch.setattr(memory_forget, "_run_step", flaky)
    first = apply_forget(layout, plan, at=NOW)
    assert first["job"]["state"] == "failed"
    assert {s["store"] for s in first["job"]["failed_steps"]} == {
        "checkpoint",
        "forgetting_record",
    }
    # The search sources went first: the entry is out of search while the job is open.
    assert store.search(CANARY) == []
    assert store.read_text(MemoryLayer.EPISODIC, keys["episode"]) is None
    job_id = first["job"]["id"]
    assert list_jobs(layout)[0]["state"] == "failed"
    monkeypatch.undo()

    # The job keeps only the query's digest: resuming without the same query is refused.
    refused = resume_forget(layout, job_id, at=NOW)
    assert refused["job"] is None and "--query" in refused["error"]
    assert resume_forget(layout, job_id, at=NOW, queries=("other",))["job"] is None

    second = resume_forget(layout, job_id, at=NOW, queries=(CANARY,))

    assert second["job"]["state"] == "completed" and second["job"]["attempts"] == 2
    assert _holding(data) == NOT_REACHED
    # A completed job is not run again; nothing reappears.
    third = resume_forget(layout, job_id, at=NOW, queries=(CANARY,))
    assert third["job"]["attempts"] == 2 and _holding(data) == NOT_REACHED


def test_a_changed_item_is_left_and_the_step_fails(tmp_path, monkeypatch):
    data, _store, keys = _build(tmp_path)
    layout = DataLayout.at(data)
    plan = plan_forget(layout, ForgetRequest.parse([], [CANARY]))
    path = data / "checkpoints" / f"{keys['checkpoint']}.json"
    inventory = memory_forget._inventory

    def then_change(layout, plan):
        steps = inventory(layout, plan)
        path.write_text(path.read_text() + " ", encoding="utf-8")
        return steps

    monkeypatch.setattr(memory_forget, "_inventory", then_change)
    result = apply_forget(layout, plan, at=NOW)
    assert result["job"]["state"] == "failed"
    assert result["job"]["failed_steps"][0]["store"] == "checkpoint"
    assert result["job"]["failed_steps"][0]["error"] == "E_CHANGED: file_changed_since_inventory"
    assert path.exists()
    assert _where(result) == _digests(NOT_REACHED | {path.relative_to(data).as_posix()})


def test_forget_by_id_reaches_what_was_derived_from_the_entry(tmp_path):
    data, store, keys = _build(tmp_path, removal_record=False)
    layout = DataLayout.at(data)
    plan = plan_forget(layout, ForgetRequest.parse([f"episodic/{keys['episode']}"], []))
    report = plan.report()

    assert report["reached"]["semantic_staging"] == ["staging/harvest-locker"]
    assert report["reached"]["staging_rejections"] == ["line 1"]
    assert "checkpoint" not in report["reached"]
    assert report["hint"]  # --id alone does not search the conversation copies

    result = apply_forget(layout, plan, at=NOW)

    assert result["job"]["state"] == "completed"
    assert result["job"]["summary"]["memory"]["done"] == 2
    assert store.read_text(MemoryLayer.EPISODIC, keys["episode"]) is None
    assert store.read_text(MemoryLayer.SEMANTIC, "staging/harvest-locker") is None


def test_apply_refuses_while_the_server_owns_the_data_directory(tmp_path, capsys):
    data, _store, keys = _build(tmp_path)
    before = _files(data)
    lock = DataDirOwnerLock(data)
    lock.acquire()
    try:
        code = memory_main(["forget", "--data-dir", str(data), "--query", CANARY, "--apply"])
        assert code == 3
        assert json.loads(capsys.readouterr().out)["refused"]["code"] == "E_DATA_DIR_OWNED"
        assert memory_main(["restore", "--data-dir", str(data), keys["removal"]]) == 3
        assert memory_main(["retention", "--data-dir", str(data), "--apply"]) == 3
    finally:
        lock.release()
    after = _files(data)
    after.pop("server.lock", None)
    assert after == before


def test_cli_forget_dry_run_apply_jobs_resume(tmp_path, capsys):
    data, _store, _keys = _build(tmp_path)
    assert stira_main(["memory", "forget", "--data-dir", str(data), "--query", CANARY]) == 0
    dry = json.loads(capsys.readouterr().out)
    assert dry["dry_run"] and dry["counts"]["episodic"] == 1
    assert memory_main(["forget", "--data-dir", str(data), "--query", CANARY, "--apply"]) == 0
    applied = json.loads(capsys.readouterr().out)
    job_id = applied["result"]["job"]["id"]
    assert memory_main(["jobs", "--data-dir", str(data)]) == 0
    jobs = json.loads(capsys.readouterr().out)["jobs"]
    assert [(j["id"], j["state"]) for j in jobs] == [(job_id, "completed")]
    assert memory_main(["forget", "--data-dir", str(data), "--resume", job_id]) == 0
    capsys.readouterr()
    assert memory_main(["forget", "--data-dir", str(data), "--query", "ab"]) == 2
    assert memory_main(["forget", "--data-dir", str(data), "--id", "nolayer/x"]) == 2
    assert memory_main(["forget", "--data-dir", str(data)]) == 2
    assert memory_main(["forget", "--data-dir", str(data), "--resume", job_id, "--apply"]) == 2
    assert _holding(data) == NOT_REACHED


def test_links_are_not_followed(tmp_path):
    data, _store, _keys = _build(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text(CANARY, encoding="utf-8")
    (data / "context-stash" / "link").symlink_to(outside)
    layout = DataLayout.at(data)
    plan = plan_forget(layout, ForgetRequest.parse([], [CANARY]))
    assert "context-stash/link" not in plan.report()["reached"]["context_stash"]
    assert apply_forget(layout, plan, at=NOW)["job"]["state"] == "completed"
    assert outside.read_text(encoding="utf-8") == CANARY
    assert (data / "context-stash" / "link").is_symlink()


# -- retention ----------------------------------------------------------------------


def _age_file(path: Path, days: int) -> None:
    moment = (NOW - timedelta(days=days)).timestamp()
    os.utime(path, (moment, moment))


def test_retention_report_reads_every_store_and_changes_nothing(tmp_path):
    data, _store, _keys = _build(tmp_path)
    before = _files(data)
    report, plan = retention_report(DataLayout.at(data), RetentionPolicy.from_options({}))

    assert _files(data) == before
    assert plan.empty() and report["policy_configured"] is False
    assert report["will_remove"] == {
        "checkpoints": 0,
        "emotion_records": 0,
        "episodic": 0,
        "memory_plans": 0,
        "tool_payloads": 0,
        "turn_events": 0,
    }
    stores = report["stores"]
    assert stores["episodic"]["count"] == 2
    assert stores["episodic"]["provenance"] == {
        "origin": {"extracted": 2},
        "source": {"web-chat": 2},
    }
    assert stores["semantic"]["provenance"]["origin"] == {"unknown": 4}
    assert stores["semantic"]["staging"]["count"] == 1
    assert stores["semantic"]["forgetting_compressed"] == 1
    assert stores["turns_db"]["turns"] == 2 and stores["turns_db"]["tool_payloads"] == 2
    assert stores["checkpoints"]["count"] == 2 and stores["checkpoints"]["threads"] == 1
    assert stores["context_stash"]["count"] == 2
    assert stores["scheduler_log"]["lines"] == 1
    assert stores["archive"]["forgetting_records"] == 1
    assert stores["archive"]["removal_records"] == 1
    assert stores["staging_rejections"]["lines"] == 2
    assert stores["search_index"]["rows"] >= 5


def test_retention_report_on_an_empty_directory_creates_nothing(tmp_path, capsys):
    data = (tmp_path / "empty").resolve()
    data.mkdir()
    assert stira_main(["memory", "retention", "--data-dir", str(data)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["dry_run"] and report["stores"]["episodic"]["count"] == 0
    assert list(data.iterdir()) == []


def test_retention_apply_archives_over_limit_items_and_restore_undoes_it(tmp_path):
    data, store, keys = _build(tmp_path, removal_record=False)
    for key in (keys["plain"], keys["episode"]):
        _age_file(store._path(MemoryLayer.EPISODIC, key), 400)
    # chat-1 was harvested, chat-2 not: only chat-1 may go (keep_unharvested default).
    entry = store.read(MemoryLayer.EPISODIC, keys["plain"])
    store.write(
        MemoryLayer.EPISODIC,
        keys["plain"],
        entry.content,
        {**entry.metadata, "harvested": True},
        preserve_mtime=True,
    )
    with sqlite3.connect(data / "turns.db") as connection:
        connection.execute(
            "UPDATE ui_events SET ts = ? WHERE turn_id = 't-1'",
            ((NOW - timedelta(days=100)).timestamp(),),
        )
        connection.execute(
            "UPDATE ui_events SET ts = ? WHERE turn_id = 't-2'",
            ((NOW - timedelta(days=40)).timestamp(),),
        )
    for _ in range(2):
        FileCheckpointStore(data / "checkpoints").save(
            Checkpoint(NeuralState(), [], [], time.time() + 5, "stira")
        )
    policy = RetentionPolicy.from_options(
        {
            "episodic": {"max_age_days": 365},
            "turns": {"max_age_days": 90},
            "tool_payloads": {"max_age_days": 30},
            "checkpoints": {"max_per_thread": 2},
        }
    )
    layout = DataLayout.at(data)
    report, plan = retention_report(layout, policy, now=NOW)
    assert report["will_remove"] == {
        "checkpoints": 2,
        "emotion_records": 1,  # chat-1's emotion record goes (and comes back) with it
        "episodic": 1,
        "memory_plans": 0,
        "tool_payloads": 2,
        "turn_events": 2,
    }
    assert report["stores"]["episodic"]["held_unharvested"] == 1
    before = _files(data)

    result = apply_retention(layout, plan, at=NOW)

    assert result["applied"], result
    assert store.read_text(MemoryLayer.EPISODIC, keys["plain"]) is None
    assert not emotion_record_path(store, keys["plain"]).exists()
    assert store.read_text(MemoryLayer.EPISODIC, keys["episode"]) is not None
    assert len(list((data / "checkpoints").glob("*.json"))) == 2
    turns = TurnEventStore(data / "turns.db")
    try:
        assert turns.events("t-1") == [] and len(turns.events("t-2")) == 1
        assert turns.payload_page("t-2", "u2", "output") is None
    finally:
        turns.close()
    kinds = [json.loads(line)["kind"] for line in layout.audit_log.read_text().splitlines()]
    assert kinds[-2:] == ["remove.started", "remove.applied"]
    record = data / ARCHIVE / f"removed-{result['removal_id']}.json"
    assert stat.S_IMODE(record.stat().st_mode) == 0o600

    undone = restore(layout, result["removal_id"])

    assert undone.refused is None and undone.cleanup_error is None
    assert undone.restored == {
        "checkpoint": 2,
        "emotion_record": 1,
        "memory": 1,
        "tool_payload": 2,
        "turn_event": 2,
    }
    assert emotion_record_path(store, keys["plain"]).is_file()
    after = _files(data)
    for name, raw in before.items():
        if name.endswith((".sqlite3", ".db", "-wal", "-shm", AUDIT_LOG_NAME)):
            continue
        assert after.get(name) == raw, name
    turns = TurnEventStore(data / "turns.db")
    try:
        assert len(turns.events("t-1")) == 2
        assert turns.payload_page("t-1", "u1", "output")["text"] == f"memory says {CANARY}"
    finally:
        turns.close()
    assert not record.exists()
    kinds = [json.loads(line)["kind"] for line in layout.audit_log.read_text().splitlines()]
    assert kinds[-2:] == ["restore.started", "restore.applied"]


def test_restore_refuses_when_something_else_took_the_place(tmp_path):
    data, store, keys = _build(tmp_path)
    layout = DataLayout.at(data)
    store.write(MemoryLayer.EPISODIC, "old-a", "the agent wrote a new one")
    before = _files(data)

    undone = restore(layout, keys["removal"])

    assert undone.refused.startswith("E_CONFLICT: episodic ")  # area + digest, no key
    after = _files(data)
    changed = {name for name in set(before) | set(after) if before.get(name) != after.get(name)}
    assert changed == {f"{ARCHIVE}/{AUDIT_LOG_NAME}"}
    last = json.loads(layout.audit_log.read_text().splitlines()[-1])
    assert last["kind"] == "restore.refused"


def test_retention_cli_reads_the_policy_from_agent_config(tmp_path, capsys):
    data, _store, _keys = _build(tmp_path)
    (data / "agent-config.json").write_text(
        json.dumps({"memory_retention": {"tool_payloads": {"max_age_days": 1}}}),
        encoding="utf-8",
    )
    with sqlite3.connect(data / "turns.db") as connection:
        connection.execute("UPDATE ui_events SET ts = ?", ((NOW - timedelta(days=3)).timestamp(),))
    assert memory_main(["retention", "--data-dir", str(data)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["policy_configured"] and report["will_remove"]["tool_payloads"] == 2
    assert memory_main(["retention", "--data-dir", str(data), "--apply"]) == 0
    applied = json.loads(capsys.readouterr().out)
    assert applied["result"]["counts"] == {"tool_payload": 2}
    assert memory_main(["removed", "--data-dir", str(data)]) == 0
    listing = json.loads(capsys.readouterr().out)["removed"]
    assert len(listing) == 2 and CANARY.lower() not in json.dumps(listing).lower()


@pytest.mark.parametrize(
    "options",
    [
        [],
        {"nope": {}},
        {"episodic": {"max_age": 3}},
        {"episodic": {"max_age_days": 0}},
        {"episodic": {"max_age_days": True}},
        {"episodic": {"keep_unharvested": "yes"}},
        {"turns": []},
    ],
)
def test_policy_validation(options):
    with pytest.raises(ValueError, match="memory_retention"):
        RetentionPolicy.from_options(options)


def test_policy_is_part_of_agent_config_and_off_by_default(tmp_path):
    assert AgentConfig().memory_retention == {}
    path = tmp_path / "agent-config.json"
    path.write_text(json.dumps({"memory_retention": {"turns": {"max_age_days": 7}}}))
    loaded = AgentConfig.load(path, tool_config_path=tmp_path / "tool-config.json")
    assert loaded.memory_retention == {"turns": {"max_age_days": 7}}
    assert loaded.as_dict()["memory_retention"] == {"turns": {"max_age_days": 7}}
    with pytest.raises(ValueError):
        AgentConfig(memory_retention={"turns": {"max_age_days": -1}})


def test_expires_at_is_honoured_only_when_opted_in(tmp_path):
    data, store, _keys = _build(tmp_path)
    store.write(
        MemoryLayer.EPISODIC,
        "ttl-1",
        "short lived",
        {"source": "memory_write", "expires_at": "2000-01-01T00:00:00+00:00", "harvested": True},
    )
    layout = DataLayout.at(data)
    off, _ = retention_report(layout, RetentionPolicy.from_options({}))
    assert off["stores"]["episodic"]["expires_at_passed"] == 1
    assert off["will_remove"]["episodic"] == 0
    on, plan = retention_report(
        layout, RetentionPolicy.from_options({"episodic": {"honour_expires_at": True}})
    )
    assert on["will_remove"]["episodic"] == 1
    assert plan.memory == [(MemoryLayer.EPISODIC, "ttl-1")]


# -- provenance (origin / source) ---------------------------------------------------


def test_writers_tag_origin_and_source_and_harvest_copies_them(tmp_path):
    store = MemoryStore(tmp_path / "memory", "stira")
    tools = {t.tool_name: t for t in create_memory_tools(store)}
    tools["memory_write"](layer="episodic", key="note", content="agent note")
    assert store.read(MemoryLayer.EPISODIC, "note").metadata == {
        "source": "memory_write",
        "origin": "model",
    }
    turn = record_episode(
        store,
        "# Decision\n- adopt the plan\n- write tests\n```py\nx = 1\n```\nWe decided: adopt it.",
        "Agreed, adopting the plan with tests.",
        key="chat-9",
    )
    assert turn.metadata["origin"] == "extracted" and turn.metadata["source"] == "web-chat"
    HarvestScheduler(
        store, None, "stira", distiller=RuleBasedDistiller(AmygdalaPass())
    ).run_distillation()
    derived = [
        e
        for e in store.list_entries(MemoryLayer.SEMANTIC, limit=100)
        if e.metadata.get("source_id") == "chat-9"
    ]
    (staged,) = derived  # score 0.6 (measured): staged for review
    assert staged.metadata["origin"] == "extracted"
    assert staged.metadata["source"] == "web-chat"


# -- review round (B1-B4, M) -------------------------------------------------------


def _job_file(data: Path, job_id: str) -> str:
    return (data / ARCHIVE / f"delete-{job_id}.json").read_text(encoding="utf-8")


def test_a_secret_archived_by_retention_does_not_come_back_through_restore(tmp_path):
    """B1: archived files are Base64 in the record; the delete must look inside them."""
    data, store, keys = _build(tmp_path, removal_record=False)
    layout = DataLayout.at(data)
    # Retention archives the older checkpoint (it holds the canary) and the harvested,
    # aged chat-1 with its emotion record.
    entry = store.read(MemoryLayer.EPISODIC, keys["plain"])
    store.write(
        MemoryLayer.EPISODIC, keys["plain"], entry.content, {**entry.metadata, "harvested": True}
    )
    _age_file(store._path(MemoryLayer.EPISODIC, keys["plain"]), 400)
    policy = RetentionPolicy.from_options(
        {"checkpoints": {"max_per_thread": 1}, "episodic": {"max_age_days": 365}}
    )
    report, plan = retention_report(layout, policy, now=NOW)
    assert report["will_remove"]["checkpoints"] == 1
    kept = apply_retention(layout, plan, at=NOW)
    record = RemovalArchive(layout).load(kept["removal_id"])
    (checkpoint,) = [item for item in record.items if item["store"] == "checkpoint"]
    assert CANARY not in json.dumps(checkpoint)  # only Base64 holds it: the old blind spot

    result = apply_forget(layout, plan_forget(layout, ForgetRequest.parse([], [CANARY])), at=NOW)

    assert result["job"]["state"] == "completed"
    assert _holding(data) == NOT_REACHED
    (rewritten,) = RemovalArchive(layout).ids()  # chat-1 and its emotion record stay
    assert {i["store"] for i in RemovalArchive(layout).load(rewritten).items} == {
        "memory",
        "emotion_record",
    }
    assert restore(layout, kept["removal_id"]).refused  # the old record is gone
    assert restore(layout, rewritten).refused is None
    assert _holding(data) == NOT_REACHED  # nothing came back


def test_an_id_reaches_copies_that_only_survive_in_the_archive(tmp_path):
    """B2: the request is tracked independently of which files exist now."""
    data, store, keys = _build(tmp_path, removal_record=False)
    layout = DataLayout.at(data)
    store.write(
        MemoryLayer.SEMANTIC,
        "knowledge/general/from-weather",
        "Weather talk derived.",
        {"title": "w", "source_id": keys["plain"]},
    )
    entry = store.read(MemoryLayer.EPISODIC, keys["plain"])
    store.write(
        MemoryLayer.EPISODIC, keys["plain"], entry.content, {**entry.metadata, "harvested": True}
    )
    _age_file(store._path(MemoryLayer.EPISODIC, keys["plain"]), 400)
    _, plan = retention_report(
        layout, RetentionPolicy.from_options({"episodic": {"max_age_days": 365}}), now=NOW
    )
    archived = apply_retention(layout, plan, at=NOW)["removal_id"]
    assert store.read_text(MemoryLayer.EPISODIC, keys["plain"]) is None  # archive only now
    # The compressed facts/old is gone from the layer: only its #320 record is left.
    store._path(MemoryLayer.SEMANTIC, "facts/old").unlink()

    by_episode = plan_forget(layout, ForgetRequest.parse([f"episodic/{keys['plain']}"], []))
    assert by_episode.report()["reached"]["semantic"] == ["knowledge/general/from-weather"]
    assert by_episode.purge_removal == {archived: [0, 1]}  # the episode and its emotion record
    by_key = plan_forget(layout, ForgetRequest.parse(["semantic/facts/old"], []))
    assert len(by_key.purge_forgetting) == 1

    for plan_ in (by_episode, by_key):
        assert apply_forget(layout, plan_, at=NOW)["job"]["state"] == "completed"
    assert RemovalArchive(layout).ids() == [] and RemovalArchive(layout).forgetting_ids() == []
    raw = [blob.decode("utf-8", "replace") for blob in _files(data).values()]
    assert not any("How is the weather" in text for text in raw)
    assert not any("x" * 3_000 in text for text in raw)  # facts/old's archived original


def test_a_failed_semantic_step_keeps_the_index_step_waiting_until_resume(tmp_path, monkeypatch):
    """B3: the regenerated index is checked, and re-run after the failed removal."""
    data, store, _keys = _build(tmp_path, removal_record=False)
    layout = DataLayout.at(data)
    real = memory_forget._run_step

    def semantic_fails(layout, store, archive, step, at):
        if step["store"] == "memory" and step["target"]["layer"] == "semantic":
            raise OSError("busy")
        return real(layout, store, archive, step, at)

    monkeypatch.setattr(memory_forget, "_run_step", semantic_fails)
    plan = plan_forget(layout, ForgetRequest.parse([], [CANARY]))
    first = apply_forget(layout, plan, at=NOW)
    assert first["job"]["state"] == "failed"
    assert "semantic_index_md" in first["job"]["waiting_steps"]
    assert "| semantic/knowledge/general/harvest-word.md |" in store.read_text(
        MemoryLayer.SEMANTIC, "index"
    )
    monkeypatch.undo()

    second = resume_forget(layout, first["job"]["id"], at=NOW, queries=(CANARY,))

    assert second["job"]["state"] == "completed", second
    assert "harvest-word" not in store.read_text(MemoryLayer.SEMANTIC, "index")
    assert _holding(data) == NOT_REACHED
    job = json.loads(_job_file(data, first["job"]["id"]))
    assert "ids" not in job and job["steps"] == []


def test_a_check_finds_what_a_step_reported_as_done(tmp_path, monkeypatch):
    """B3: a step that 'succeeds' without removing anything is not counted as done."""
    data, _store, _keys = _build(tmp_path, removal_record=False)
    layout = DataLayout.at(data)
    real = memory_forget._run_step

    def stash_noop(layout, store, archive, step, at):
        if step["store"] == "context_stash":
            return None  # claims success, removes nothing
        return real(layout, store, archive, step, at)

    monkeypatch.setattr(memory_forget, "_run_step", stash_noop)
    result = apply_forget(layout, plan_forget(layout, ForgetRequest.parse([], [CANARY])), at=NOW)
    assert result["job"]["state"] == "failed"
    assert result["job"]["failed_steps"] == [
        {"store": "context_stash", "error": "E_CHANGED: file_still_there"}
    ]


def test_restore_checks_rows_before_writing_anything(tmp_path):
    """B4: a conflicting turns.db row refuses the restore before any file is written."""
    data, _store, _keys = _build(tmp_path, removal_record=False)
    layout = DataLayout.at(data)
    connection = sqlite3.connect(data / "turns.db")
    with connection:
        connection.execute(
            "UPDATE ui_events SET ts = ?", ((NOW - timedelta(days=100)).timestamp(),)
        )
    connection.close()
    policy = RetentionPolicy.from_options(
        {"turns": {"max_age_days": 90}, "checkpoints": {"max_per_thread": 1}}
    )
    _, plan = retention_report(layout, policy, now=NOW)
    removal_id = apply_retention(layout, plan, at=NOW)["removal_id"]
    record = RemovalArchive(layout).load(removal_id)
    taken = next(item["row"] for item in record.items if item["store"] == "turn_event")
    connection = sqlite3.connect(data / "turns.db")
    with connection:
        connection.execute(
            "INSERT INTO ui_events (id, turn_id, seq, type, ts, payload) VALUES (?,?,?,?,?,?)",
            (taken["id"], "t-new", 1, "turn.started", 1.0, "{}"),
        )
    connection.close()  # stopped server: no live -wal/-shm
    before = _files(data)

    undone = restore(layout, removal_id)

    assert undone.refused and "turns.db" in undone.refused
    after = _files(data)
    changed = {name for name in set(before) | set(after) if before.get(name) != after.get(name)}
    assert changed == {f"{ARCHIVE}/{AUDIT_LOG_NAME}"}  # no checkpoint was written back


def _wal_without_shm(data: Path) -> None:
    """turns.db with rows only in its WAL and no -shm (e.g. copied from a live server)."""
    import shutil

    path = data / "turns.db"
    snap = data.parent / "snap"
    snap.mkdir()
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA wal_autocheckpoint = 0")
    connection.execute(
        "INSERT INTO tool_payloads VALUES ('t-3', 'u3', 'output', ?, 1, 1, 0)",
        (f"late {CANARY}",),
    )
    connection.commit()
    shutil.copy(path, snap / "turns.db")
    shutil.copy(f"{path}-wal", snap / "turns.db-wal")
    connection.close()
    for name in ("turns.db", "turns.db-wal"):
        shutil.copy(snap / name, data / name)
    Path(f"{path}-shm").unlink(missing_ok=True)
    shutil.rmtree(snap)


def test_a_wal_without_shm_is_not_touched_by_a_dry_run_and_blocks_an_unlocked_apply(
    tmp_path, capsys
):
    """M: reading such a database would create -shm: reported, not inspected."""
    data, _store, _keys = _build(tmp_path, removal_record=False)
    _wal_without_shm(data)
    before = _files(data)
    layout = DataLayout.at(data)

    plan = plan_forget(layout, ForgetRequest.parse([], [CANARY]))
    report, _ = retention_report(layout, RetentionPolicy.from_options({}))
    assert memory_main(["forget", "--data-dir", str(data), "--query", CANARY]) == 0
    capsys.readouterr()

    assert _files(data) == before
    assert any("-wal without its -shm" in reason for reason in plan.report()["skipped"])
    assert any("-wal without its -shm" in reason for reason in report["skipped"])
    refused = apply_forget(layout, plan, at=NOW)  # cannot see everywhere: not started
    assert refused["job"] is None and "inspected" in refused["error"]
    assert RemovalArchive(layout).job_ids() == []
    # The CLI recovers the WAL under the owner lock and then reaches the late row.
    assert memory_main(["forget", "--data-dir", str(data), "--query", CANARY, "--apply"]) == 0
    applied = json.loads(capsys.readouterr().out)
    assert applied["result"]["job"]["summary"]["tool_payload"]["done"] == 2
    assert _holding(data) == NOT_REACHED


def test_a_completed_job_keeps_no_key_of_what_it_deleted(tmp_path):
    data, _store, _keys = _build(tmp_path, removal_record=False)
    layout = DataLayout.at(data)
    request = ForgetRequest.parse(["semantic/knowledge/general/harvest-word"], [])
    result = apply_forget(layout, plan_forget(layout, request), at=NOW)
    text = _job_file(data, result["job"]["id"])
    assert result["job"]["state"] == "completed"
    assert "harvest-word" not in text and "ids_sha256" in text


def test_the_documented_policy_examples_load():
    import re

    for name in ("memory-maintenance.md", "memory-maintenance_ja.md"):
        text = (Path(__file__).resolve().parents[1] / "docs" / name).read_text(encoding="utf-8")
        (block,) = re.findall(r"```json\n(\{\"memory_retention\".*?)```", text, re.DOTALL)
        RetentionPolicy.from_options(json.loads(block)["memory_retention"])


def test_a_forged_origin_in_episodic_front_matter_is_not_trusted(tmp_path):
    """M: the agent may write episodic/ with its file tools; the origin it claims there
    is not believed (not by the report, not by Harvest)."""
    from stiracore.config.data_dir import build_policy

    data, store, keys = _build(tmp_path, removal_record=False)
    (data / "workspace").mkdir(exist_ok=True)
    policy, _ = build_policy(AgentConfig(), data)
    path = store._path(MemoryLayer.EPISODIC, keys["plain"])
    assert policy.check(str(path)).allowed  # the episodic layer is a write root
    assert not policy.check(str(store.index_path)).allowed  # the binding is on the floor
    original = path.read_text(encoding="utf-8")
    forged = original.replace("origin: extracted", "origin: person").replace(
        "source: web-chat", "source: typed-by-owner"
    )
    assert forged != original
    path.write_text(forged, encoding="utf-8")  # what file_write would do
    # Even a forged copy of a whole server-written episode under a new key is unbound.
    store._path(MemoryLayer.EPISODIC, "chat-forged").write_text(original, encoding="utf-8")
    assert store.read(MemoryLayer.EPISODIC, keys["plain"]).metadata["origin"] == "person"

    report, _ = retention_report(DataLayout.at(data), RetentionPolicy.from_options({}))
    assert report["stores"]["episodic"]["provenance"] == {
        "origin": {"extracted": 1, "unverified": 2},
        "source": {"unverified": 2, "web-chat": 1},
    }
    HarvestScheduler(
        store, None, "stira", distiller=RuleBasedDistiller(AmygdalaPass()), stage_auto=True
    ).run_distillation()
    for entry in store.list_entries(MemoryLayer.SEMANTIC, limit=100):
        if entry.metadata.get("source_id") in (keys["plain"], "chat-forged"):
            assert "origin" not in entry.metadata and "source" not in entry.metadata


# -- final round (B1-B3, M) ---------------------------------------------------------


def _age_turns(data: Path, days: int) -> None:
    connection = sqlite3.connect(data / "turns.db")
    with connection:
        connection.execute(
            "UPDATE ui_events SET ts = ?", ((NOW - timedelta(days=days)).timestamp(),)
        )
    connection.close()


@pytest.mark.parametrize("change", ["turn_id", "seq", "type", "ts", "payload_meta"])
def test_restore_refuses_a_row_that_differs_in_any_column(tmp_path, change):
    """B1: same id (and payload) but another turn/seq/type/ts is a conflict, not 'there'."""
    data, _store, _keys = _build(tmp_path, removal_record=False)
    layout = DataLayout.at(data)
    _age_turns(data, 100)
    _, plan = retention_report(
        layout, RetentionPolicy.from_options({"turns": {"max_age_days": 90}}), now=NOW
    )
    removal_id = apply_retention(layout, plan, at=NOW)["removal_id"]
    record = RemovalArchive(layout).load(removal_id)
    event = next(i["row"] for i in record.items if i["store"] == "turn_event")
    payload = next(i["row"] for i in record.items if i["store"] == "tool_payload")
    connection = sqlite3.connect(data / "turns.db")
    with connection:
        if change == "payload_meta":  # same key and text, other counters
            connection.execute(
                "INSERT INTO tool_payloads VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    payload["turn_id"],
                    payload["tool_use_id"],
                    payload["kind"],
                    payload["text"],
                    payload["total_chars"] + 1,
                    payload["total_lines"],
                    payload["retained_truncated"],
                ),
            )
        else:
            row = dict(event)
            row[change] = {"turn_id": "t-other", "seq": 99, "type": "turn.summary", "ts": 1.0}[
                change
            ]
            connection.execute(
                "INSERT INTO ui_events (id, turn_id, seq, type, ts, payload)"
                " VALUES (:id, :turn_id, :seq, :type, :ts, :payload)",
                row,
            )
    connection.close()
    before = _files(data)

    undone = restore(layout, removal_id)

    assert undone.refused and "turns.db" in undone.refused
    after = _files(data)
    changed = {name for name in set(before) | set(after) if before.get(name) != after.get(name)}
    assert changed == {f"{ARCHIVE}/{AUDIT_LOG_NAME}"}
    assert RemovalArchive(layout).load(removal_id).items == record.items  # the copy is kept


def test_retention_apply_is_refused_when_a_store_could_not_be_read(tmp_path, capsys):
    """B2: fail closed, as a delete request."""
    data, store, keys = _build(tmp_path, removal_record=False)
    entry = store.read(MemoryLayer.EPISODIC, keys["plain"])
    store.write(
        MemoryLayer.EPISODIC, keys["plain"], entry.content, {**entry.metadata, "harvested": True}
    )
    _age_file(store._path(MemoryLayer.EPISODIC, keys["plain"]), 400)
    (data / "turns.db").unlink()
    broken = sqlite3.connect(data / "turns.db")  # a turns.db without its tables
    broken.execute("CREATE TABLE other (x)")
    broken.commit()
    broken.close()
    (data / "agent-config.json").write_text(
        json.dumps({"memory_retention": {"episodic": {"max_age_days": 365}}}), encoding="utf-8"
    )
    before = _files(data)

    assert memory_main(["retention", "--data-dir", str(data), "--apply"]) == 1

    out = json.loads(capsys.readouterr().out)
    assert out["result"]["applied"] is False
    assert out["result"]["error"].startswith("E_NOT_INSPECTED")
    assert {item["area"] for item in out["skipped"]} == {"turns.db"}
    after = _files(data)
    after.pop("server.lock", None)
    assert after == before  # the over-age episode is still there, no record written


def test_dot_files_and_stray_copies_are_reached_and_a_leftover_fails_the_job(tmp_path, capsys):
    """B3: the plan sees the files the residual check sees; what stays fails the job."""
    data, store, _keys = _build(tmp_path, removal_record=False)
    (data / "context-stash" / ".leftover").write_text(f"x {CANARY}", encoding="utf-8")
    (store.memory_dir / "episodic" / ".memory-crash.tmp").write_text(CANARY, encoding="utf-8")
    (store.memory_dir / ".stira-crash.tmp").write_text(CANARY, encoding="utf-8")
    layout = DataLayout.at(data)
    plan = plan_forget(layout, ForgetRequest.parse([], [CANARY]))
    assert "context-stash/.leftover" in plan.report()["reached"]["context_stash"]
    assert len(plan.report()["reached"]["stray_files"]) == 2
    assert memory_main(["forget", "--data-dir", str(data), "--query", CANARY, "--apply"]) == 0
    capsys.readouterr()
    assert _holding(data) == NOT_REACHED

    # A copy no step can remove (a table the planner does not know, inside the index):
    # the residual check sees it, so the job fails instead of reporting success.
    connection = sqlite3.connect(store.index_path)
    with connection:
        connection.execute("CREATE TABLE notes (t TEXT)")
        connection.execute("INSERT INTO notes VALUES (?)", (CANARY,))
    connection.close()
    assert memory_main(["forget", "--data-dir", str(data), "--query", CANARY, "--apply"]) == 1
    out = json.loads(capsys.readouterr().out)["result"]
    assert out["job"]["state"] == "failed"
    assert {"what": "residual", "area": "index.sqlite3"}.items() <= next(
        item for item in out["job"]["left"] if item.get("what") == "residual"
    ).items()
    job_id = out["job"]["id"]
    # Resume checks again, and still fails while it is there.
    again = resume_forget(layout, job_id, at=NOW, queries=(CANARY,))
    assert again["job"]["state"] == "failed" and again["job"]["attempts"] == 2


def test_an_unfinished_job_keeps_digests_only_and_resume_needs_the_same_ids(tmp_path, monkeypatch):
    """M: no key, path or text in the job file, in any state."""
    data, _store, keys = _build(tmp_path, removal_record=False)
    layout = DataLayout.at(data)
    wanted = ["semantic/knowledge/general/harvest-word", f"episodic/{keys['episode']}"]
    real = memory_forget._run_step

    def flaky(layout, store, archive, step, at):
        if step["store"] == "context_stash" or step["store"] == "checkpoint":
            raise OSError("busy")
        return real(layout, store, archive, step, at)

    monkeypatch.setattr(memory_forget, "_run_step", flaky)
    plan = plan_forget(layout, ForgetRequest.parse(wanted, [CANARY]))
    first = apply_forget(layout, plan, at=NOW)
    monkeypatch.undo()
    assert first["job"]["state"] == "failed"
    text = _job_file(data, first["job"]["id"])
    for secret_part in ("harvest-word", keys["episode"], CANARY, "checkpoints/", "tool-result"):
        assert secret_part.casefold() not in text.casefold(), secret_part
    assert "target_sha256" in text and "ids_sha256" in text

    job_id = first["job"]["id"]
    assert resume_forget(layout, job_id, at=NOW, queries=(CANARY,))["job"] is None  # ids missing
    done = resume_forget(layout, job_id, at=NOW, queries=(CANARY,), ids=tuple(wanted))
    assert done["job"]["state"] == "completed"
    assert _holding(data) == NOT_REACHED


# -- round 4: nothing secret in the job file or its output; unreadable dirs ------


def test_a_secret_in_a_file_name_never_reaches_the_job_file_or_the_output(tmp_path, capsys):
    """A real PermissionError names the file; the residual names it too. Neither the
    job file nor the CLI output may hold it (codes, fixed labels and digests only)."""
    data, store, _keys = _build(tmp_path, removal_record=False)
    emotion = store.memory_dir / "emotion"
    (emotion / f"{CANARY}.json").write_text(json.dumps({"note": CANARY}), encoding="utf-8")
    os.chmod(emotion, 0o500)  # listable and readable, but nothing can be unlinked
    try:
        code = memory_main(["forget", "--data-dir", str(data), "--query", CANARY, "--apply"])
        out = capsys.readouterr().out
    finally:
        os.chmod(emotion, 0o700)
    assert code == 1
    result = json.loads(out)["result"]
    assert result["job"]["state"] == "failed"
    assert {"store": "stray_file", "error": "E_PERMISSION (PermissionError)"} in result["job"][
        "failed_steps"
    ]
    assert any(item.get("area") == "emotion" for item in result["job"]["left"])
    assert CANARY.casefold() not in out.casefold()
    raw = (data / ARCHIVE / f"delete-{result['job']['id']}.json").read_bytes()
    assert CANARY.lower().encode() not in raw.lower()
    assert b"where_sha256" in raw  # the leftover is recorded, as a digest
    log = (data / ARCHIVE / AUDIT_LOG_NAME).read_bytes()
    assert log and CANARY.lower().encode() not in log.lower()
    # With the directory writable again, a resume finishes it.
    again = resume_forget(DataLayout.at(data), result["job"]["id"], at=NOW, queries=(CANARY,))
    assert again["job"]["state"] == "completed"
    assert _holding(data) == NOT_REACHED


def test_an_unreadable_directory_is_never_skipped_silently(tmp_path, capsys):
    """os.walk would skip it: the plan reports it, --apply is refused, retention too."""
    data, _store, _keys = _build(tmp_path, removal_record=False)
    hidden = data / "context-stash" / "sub"
    hidden.mkdir()
    (hidden / "copy").write_text(CANARY, encoding="utf-8")
    sealed = data / "checkpoints" / "old"
    sealed.mkdir()
    os.chmod(hidden, 0o000)
    os.chmod(sealed, 0o000)
    try:
        layout = DataLayout.at(data)
        plan = plan_forget(layout, ForgetRequest.parse([], [CANARY]))
        assert "context-stash/sub not readable" in plan.report()["skipped"]
        before = _files(data)
        result = apply_forget(layout, plan, at=NOW)
        assert result["job"] is None and result["error"].startswith("E_NOT_INSPECTED")
        assert RemovalArchive(layout).job_ids() == []
        # The residual check does not call an unreadable reached store clean either.
        assert any(
            item["reason"].startswith("a reached store could not be read")
            for item in residual(layout, ForgetRequest.parse([], [CANARY]))
        )
        report, retention_plan = retention_report(
            layout, RetentionPolicy.from_options({"checkpoints": {"max_per_thread": 1}}), now=NOW
        )
        assert "checkpoints/old not readable" in report["skipped"]
        assert apply_retention(layout, retention_plan, at=NOW)["applied"] is False
        assert _files(data) == before
    finally:
        os.chmod(hidden, 0o700)
        os.chmod(sealed, 0o700)


# -- round 5: refusals print codes, areas and digests; derived targets survive resume --


def test_a_refused_retention_apply_prints_no_path(tmp_path, capsys):
    data, _store, _keys = _build(tmp_path, removal_record=False)
    sealed = data / "checkpoints" / CANARY
    sealed.mkdir()
    (data / "agent-config.json").write_text(
        json.dumps({"memory_retention": {"checkpoints": {"max_per_thread": 1}}}), encoding="utf-8"
    )
    os.chmod(sealed, 0o000)
    try:
        code = memory_main(["retention", "--data-dir", str(data), "--apply"])
        out = capsys.readouterr().out
    finally:
        os.chmod(sealed, 0o700)
    assert code == 1
    result = json.loads(out)
    assert result["result"]["error"].startswith("E_NOT_INSPECTED")
    assert {item["area"] for item in result["skipped"]} == {"checkpoints"}
    assert CANARY.casefold() not in out.casefold()
    log = DataLayout.at(data).audit_log
    assert not log.exists() or CANARY.lower() not in log.read_text().lower()


def test_a_restore_conflict_prints_no_key_or_path(tmp_path, capsys):
    data, store, _keys = _build(tmp_path, removal_record=False)
    key = f"note-{CANARY}"
    store.write(MemoryLayer.EPISODIC, key, "old body", {"harvested": True})
    _age_file(store._path(MemoryLayer.EPISODIC, key), 400)
    layout = DataLayout.at(data)
    _, plan = retention_report(
        layout, RetentionPolicy.from_options({"episodic": {"max_age_days": 365}}), now=NOW
    )
    removal_id = apply_retention(layout, plan, at=NOW)["removal_id"]
    store.write(MemoryLayer.EPISODIC, key, "a new body under the same key")

    assert memory_main(["restore", "--data-dir", str(data), removal_id]) == 1

    out = capsys.readouterr().out
    refused = json.loads(out)["restore"]["refused"]
    assert refused.startswith("E_CONFLICT: episodic ")
    assert CANARY.casefold() not in out.casefold()
    events = [json.loads(line) for line in layout.audit_log.read_text().splitlines()]
    assert events[-1]["kind"] == "restore.refused"
    assert events[-1]["body"]["reason"] == refused
    assert CANARY.lower() not in layout.audit_log.read_text().lower()


def test_a_derived_entry_whose_deletion_failed_is_still_deleted_on_resume(tmp_path, monkeypatch):
    """The derived staging entry holds no query text; it was found only through its
    source episode, which the first attempt deleted. The job keeps the digest of what
    it resolved, so the resume still finds it."""
    data, store, keys = _build(tmp_path, removal_record=False)
    layout = DataLayout.at(data)
    derived = "staging/harvest-locker"
    assert CANARY not in store.read_text(MemoryLayer.SEMANTIC, derived)
    real = memory_forget._run_step

    def derived_fails(layout, store, archive, step, at):
        if step["store"] == "memory" and step["target"].get("key") == derived:
            raise OSError("busy")
        return real(layout, store, archive, step, at)

    monkeypatch.setattr(memory_forget, "_run_step", derived_fails)
    first = apply_forget(layout, plan_forget(layout, ForgetRequest.parse([], [CANARY])), at=NOW)
    monkeypatch.undo()
    assert first["job"]["state"] == "failed"
    assert store.read_text(MemoryLayer.EPISODIC, keys["episode"]) is None  # source gone
    assert store.read_text(MemoryLayer.SEMANTIC, derived) is not None
    # Without the job's digests the request alone no longer reaches it.
    assert (
        "semantic_staging"
        not in plan_forget(layout, ForgetRequest.parse([], [CANARY])).report()["reached"]
    )
    text = _job_file(data, first["job"]["id"])
    assert "harvest-locker" not in text and "resolved_sha256" in text

    done = resume_forget(layout, first["job"]["id"], at=NOW, queries=(CANARY,))

    assert done["job"]["state"] == "completed", done
    assert store.read_text(MemoryLayer.SEMANTIC, derived) is None
    assert _holding(data) == NOT_REACHED


# -- #402: pre-apply recovery prints no path; a resume deletes only what it inventoried --


def _symlinked_index(tmp_path: Path) -> tuple[Path, str]:
    """A data directory whose agent id holds the canary, its index.sqlite3 a link."""
    agent = f"agent-{CANARY}"
    data = (tmp_path / "stira").resolve()
    data.mkdir()
    store = MemoryStore(data / "memory", agent)
    store.write(MemoryLayer.EPISODIC, "chat-1", "a note", {"harvested": True})
    outside = tmp_path / "elsewhere.sqlite3"
    os.replace(store.index_path, outside)
    store.index_path.symlink_to(outside)
    return data, agent


_APPLY_COMMANDS = (
    ["retention", "--apply"],
    ["forget", "--query", "a note", "--apply"],
    ["forget", "--resume", "20261018T000000Z-0000", "--query", "a note"],
    ["restore", "20261018T000000Z-0000"],
)


@pytest.mark.parametrize("command", _APPLY_COMMANDS, ids=lambda c: "-".join(c[:2]))
def test_a_link_refused_by_the_pre_apply_recovery_prints_a_code_only(tmp_path, capsys, command):
    data, agent = _symlinked_index(tmp_path)
    before = _files(data)
    code = memory_main([*command, "--data-dir", str(data), "--agent-id", agent])
    captured = capsys.readouterr()
    assert code == 1
    assert json.loads(captured.out) == {"refused": {"code": "E_UNSAFE_PATH (UnsafePathError)"}}
    for stream in (captured.out, captured.err):
        assert CANARY.casefold() not in stream.casefold()
    after = _files(data)
    after.pop("server.lock", None)
    assert after == before  # nothing written: no job file, no audit line
    assert not DataLayout.at(data, agent).audit_log.exists()
    # The owner lock was released: another owner (the server) can take it again.
    lock = DataDirOwnerLock(data)
    lock.acquire()
    lock.release()


def test_the_real_cli_process_prints_no_path_on_stderr_either(tmp_path):
    """Not only the formatted stdout: nothing reaches stderr (no traceback)."""
    import subprocess
    import sys

    data, agent = _symlinked_index(tmp_path)
    env = {**os.environ, "PYTHONPATH": os.pathsep.join(p for p in sys.path if p)}
    proc = subprocess.run(
        [sys.executable, "-m", "stiracore.control.memory_cli", "restore", "x"]
        + ["--data-dir", str(data), "--agent-id", agent],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
        check=False,
    )
    assert proc.returncode == 1, proc.stderr
    assert "Traceback" not in proc.stderr
    assert CANARY.casefold() not in (proc.stdout + proc.stderr).casefold()


def _failed_derived_job(tmp_path, monkeypatch):
    data, store, keys = _build(tmp_path, removal_record=False)
    layout = DataLayout.at(data)
    derived = "staging/harvest-locker"
    real = memory_forget._run_step

    def derived_fails(layout, store, archive, step, at):
        if step["store"] == "memory" and step["target"].get("key") == derived:
            raise OSError("busy")
        return real(layout, store, archive, step, at)

    monkeypatch.setattr(memory_forget, "_run_step", derived_fails)
    first = apply_forget(layout, plan_forget(layout, ForgetRequest.parse([], [CANARY])), at=NOW)
    monkeypatch.undo()
    assert first["job"]["state"] == "failed"
    assert store.read_text(MemoryLayer.EPISODIC, keys["episode"]) is None  # source gone
    return data, store, layout, derived, first["job"]["id"]


def _index_keys(store: MemoryStore) -> set[str]:
    with sqlite3.connect(store.index_path) as connection:
        return {row[0] for row in connection.execute("SELECT key FROM memory_entries")}


def test_a_resume_keeps_a_derived_entry_rewritten_since_the_inventory(
    tmp_path, monkeypatch, capsys
):
    """The job resolved the derived key; before the resume a legitimate writer replaced
    it with new content (no query text, no source). The resume must compare with the
    digest the job saw, leave the entry (and its index row) and fail."""
    data, store, layout, derived, job_id = _failed_derived_job(tmp_path, monkeypatch)
    store.write(MemoryLayer.SEMANTIC, derived, "A new, unrelated fact.", {"title": "new"})
    rewritten = store.read_text(MemoryLayer.SEMANTIC, derived)

    code = memory_main(["forget", "--data-dir", str(data), "--resume", job_id, "--query", CANARY])
    captured = capsys.readouterr()

    assert code == 1
    again = json.loads(captured.out)["result"]
    assert again["error"] is None
    assert again["job"]["state"] == "failed"
    assert {"store": "memory", "error": "E_CHANGED: entry_changed_since_inventory"} in again["job"][
        "failed_steps"
    ]
    assert store.read_text(MemoryLayer.SEMANTIC, derived) == rewritten
    assert derived in _index_keys(store)  # still searchable: nothing of it was removed
    # The key and the query stay out of the output, the job file and the audit log.
    log = layout.audit_log.read_text(encoding="utf-8")
    for text in (captured.out, captured.err, _job_file(data, job_id), log):
        assert CANARY.casefold() not in text.casefold()
        assert "harvest-locker" not in text


def test_a_resume_does_not_inventory_a_copy_the_job_never_saw(tmp_path, monkeypatch):
    """A resume finishes the job's own steps with the job's digests; a copy that turned
    up later is left (the verification fails the job) for a new delete request."""
    data, store, layout, derived, job_id = _failed_derived_job(tmp_path, monkeypatch)
    late = data / "context-stash" / "tool-result-late"
    late.write_text(f"late copy of {CANARY}", encoding="utf-8")

    again = resume_forget(layout, job_id, at=NOW, queries=(CANARY,))

    assert again["job"]["state"] == "failed"
    assert again["job"]["failed_steps"] == []
    assert store.read_text(MemoryLayer.SEMANTIC, derived) is None  # its own step finished
    assert late.exists()
    assert {"what": "reached", "area": "context_stash"} in again["job"]["left"]
    # A new delete request reaches it.
    fresh = apply_forget(layout, plan_forget(layout, ForgetRequest.parse([], [CANARY])), at=NOW)
    assert fresh["job"]["state"] == "completed"
    assert _holding(data) == NOT_REACHED


def _ledger_line(key: str, reason: str) -> str:
    return json.dumps({"key": key, "source_id": "chat-none", "reason": reason})


def _failed_rejection_job(tmp_path, monkeypatch, lines: list[str]):
    """A job whose staging-rejection steps all failed (everything else done)."""
    data, store, _keys = _build(tmp_path, removal_record=False)
    ledger = store.memory_dir / "staging_rejections.jsonl"
    ledger.write_text("".join(f"{line}\n" for line in lines), encoding="utf-8")
    layout = DataLayout.at(data)
    real = memory_forget._run_step

    def rejection_fails(layout, store, archive, step, at):
        if step["store"] == "staging_rejection":
            raise OSError("busy")
        return real(layout, store, archive, step, at)

    monkeypatch.setattr(memory_forget, "_run_step", rejection_fails)
    first = apply_forget(layout, plan_forget(layout, ForgetRequest.parse([], [CANARY])), at=NOW)
    monkeypatch.undo()
    assert first["job"]["state"] == "failed"
    assert {s["store"] for s in first["job"]["failed_steps"]} == {"staging_rejection"}
    return data, layout, ledger, first["job"]["id"]


def _resume_cli(data: Path, job_id: str, capsys) -> tuple[int, dict]:
    code = memory_main(["forget", "--data-dir", str(data), "--resume", job_id, "--query", CANARY])
    captured = capsys.readouterr()
    log = DataLayout.at(data).audit_log.read_text(encoding="utf-8")
    for text in (captured.out, captured.err, _job_file(data, job_id), log):
        assert CANARY.casefold() not in text.casefold()
    return code, json.loads(captured.out)["result"]


def test_a_resume_removes_an_inventoried_rejection_line_that_moved_up(
    tmp_path, monkeypatch, capsys
):
    """Line 1 (A) went away before the resume, so the unchanged line B now sits where A
    was: B is matched by its own content digest, not by A's (#411 review)."""
    a = _ledger_line("staging/a", f"first {CANARY}")
    b = _ledger_line("staging/b", f"second {CANARY}")
    keep = _ledger_line("staging/k", "unrelated")
    data, _layout, ledger, job_id = _failed_rejection_job(tmp_path, monkeypatch, [a, b, keep])
    ledger.write_text(f"{b}\n{keep}\n", encoding="utf-8")  # A removed: B is line 1 now

    code, result = _resume_cli(data, job_id, capsys)

    assert code == 0, result
    assert result["job"]["state"] == "completed"
    assert ledger.read_text(encoding="utf-8") == f"{keep}\n"
    assert _holding(data) == NOT_REACHED


def test_a_resume_leaves_a_new_rejection_line_at_an_inventoried_place(
    tmp_path, monkeypatch, capsys
):
    """A was replaced in place by C (holding the phrase too, never inventoried): C stays
    and the job fails; the unchanged B is still removed."""
    a = _ledger_line("staging/a", f"first {CANARY}")
    b = _ledger_line("staging/b", f"second {CANARY}")
    c = _ledger_line("staging/c", f"newer {CANARY}")
    data, _layout, ledger, job_id = _failed_rejection_job(tmp_path, monkeypatch, [a, b])
    ledger.write_text(f"{c}\n{b}\n", encoding="utf-8")

    code, result = _resume_cli(data, job_id, capsys)

    assert code == 1
    assert result["job"]["state"] == "failed"
    assert ledger.read_text(encoding="utf-8") == f"{c}\n"
    assert {"what": "reached", "area": "staging_rejections"} in result["job"]["left"]


def test_a_resume_removes_as_many_identical_lines_as_the_job_inventoried(
    tmp_path, monkeypatch, capsys
):
    """Two identical lines inventoried, a third identical one appended later: the resume
    removes two (the job's own), the third stays and fails the job."""
    x = _ledger_line("staging/x", f"same {CANARY}")
    keep = _ledger_line("staging/k", "unrelated")
    data, _layout, ledger, job_id = _failed_rejection_job(tmp_path, monkeypatch, [x, keep, x])
    ledger.write_text(f"{x}\n{keep}\n{x}\n{x}\n", encoding="utf-8")

    code, result = _resume_cli(data, job_id, capsys)

    assert code == 1
    assert result["job"]["state"] == "failed"
    assert ledger.read_text(encoding="utf-8") == f"{keep}\n{x}\n"

    # Without the late copy, both inventoried lines go and the job completes.
    (tmp_path / "second").mkdir()
    data2, _l2, ledger2, job2 = _failed_rejection_job(
        tmp_path / "second", monkeypatch, [x, keep, x]
    )
    code, result = _resume_cli(data2, job2, capsys)
    assert code == 0 and result["job"]["state"] == "completed"
    assert ledger2.read_text(encoding="utf-8") == f"{keep}\n"
