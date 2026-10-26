"""#601 (control + memory): regressions for the pre-release review items that reproduced.

Measured offline (temporary data directories, fake models, no cloud call). Every test
here failed on main except the D12 range/NaN cases, which main already refused (in
``AmygdalaPass.route``): they pin that guarantee.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from stiracore.control import memory_forget, memory_removal
from stiracore.control.distillers import LLMDistiller
from stiracore.control.forgetting_archive import ForgettingArchiveError
from stiracore.control.memory_forget import (
    ForgetRequest,
    apply_forget,
    plan_forget,
    resume_forget,
)
from stiracore.control.memory_jobs import MemoryJobExecutor, _Stop
from stiracore.control.memory_layout import DataLayout, contains, needles_for
from stiracore.control.memory_removal import RemovalPlan, execute, restore
from stiracore.control.procedural_proposals import ProcedureProposals
from stiracore.control.removal_archive import RemovalArchive
from stiracore.memory_origin import ORIGIN_UNKNOWN, origin_of
from stiracore.tools.memory_store import MemoryLayer, MemoryStore

NOW = datetime.now(tz=UTC)
CANARY = "Zebra-Canary-601"


# -- D26: origin_of with an unhashable front-matter value ------------------------------


@pytest.mark.parametrize("value", [[], ["person"], {}, {"a": 1}])
def test_origin_of_an_unhashable_value_is_unknown(value):
    assert origin_of({"origin": value}) == ORIGIN_UNKNOWN


# -- D24: a damaged byte in the procedural review log ----------------------------------


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_a_damaged_review_log_neither_breaks_an_approval_nor_the_history(tmp_path):
    store = MemoryStore(tmp_path / "memory", "stira")
    log = tmp_path / "procedural-reviews.jsonl"
    log.write_bytes(
        b'{"action": "rejected", "key": "old"}\n\xff\xfe broken\n'
        # Still valid JSON once the bad byte is replaced: skipped too (strict per line).
        b'{"action": "rejected", "key": "o\xffld"}\n'
    )
    proposals = ProcedureProposals(store, review_log=log)
    body = "1. Read the file first."
    proposals.propose("rule", body, "because", source_turn="turn-601")

    out = proposals.approve("rule", sha256=_sha(body))

    assert out["written"] is True and out["review_log"] == "ok"
    raw = log.read_bytes()
    assert b"\xff\xfe broken" in raw and b"o\xffld" in raw  # damaged bytes kept as they were
    assert [r["action"] for r in proposals.review_history()] == ["approved", "rejected"]
    assert proposals.overview() is not None


# -- D12: the LLM distiller's JSON and score -------------------------------------------


def _distiller(reply: str) -> LLMDistiller:
    return LLMDistiller(agent_factory=lambda _model, _region: lambda _prompt: reply)


def test_a_brace_in_the_reason_does_not_cut_the_json_short():
    decision = _distiller('{"score": 0.9, "reason": "uses {braces}"}').distill("text", {})
    assert decision.score == 0.9 and decision.reason == "uses {braces}"


def test_the_first_object_with_a_score_is_used():
    reply = 'Here: {"note": 1} then {"score": 0.1, "reason": "low"}'
    assert _distiller(reply).distill("text", {}).score == 0.1


@pytest.mark.parametrize("score", ["5", "NaN", '"NaN"', "-0.1", "true", '"0.5"', "null"])
def test_a_score_outside_the_unit_interval_or_not_a_number_is_an_error(score):
    with pytest.raises(ValueError):
        _distiller(f'{{"score": {score}, "reason": "r"}}').distill("text", {})


# -- D16: JSON-escaped capitals are found case-insensitively ---------------------------


def test_an_escaped_capital_matches_a_lower_case_query():
    needles = needles_for(("äbc",))
    assert contains(json.dumps({"text": "Äbc"}), needles)  # stored as \u00c4bc
    assert contains(json.dumps({"text": "x 😀ÄBC"}), needles_for(("😀äbc",)))
    assert contains(json.dumps({"t": 'Ä"bc'}), needles_for(('ä"bc',)))
    # An escaped backslash before "u00c4" is not an escape of Ä.
    assert not contains(r'{"t": "\\u00c4bc"}', needles)
    assert not contains(json.dumps({"text": "Äbd"}), needles)


# -- D13: a resume keeps the job's own compaction --------------------------------------


def _small_data(tmp_path: Path) -> tuple[Path, MemoryStore, DataLayout]:
    data = (tmp_path / "stira").resolve()
    data.mkdir()
    store = MemoryStore(data / "memory", "stira")
    store.write(MemoryLayer.EPISODIC, "chat-1", f"my word is {CANARY}", {"harvested": True})
    store.write(MemoryLayer.EPISODIC, "chat-2", "unrelated", {"harvested": True})
    return data, store, DataLayout.at(data)


def test_a_resume_runs_a_compaction_that_failed_after_every_removal(tmp_path, monkeypatch):
    _data, store, layout = _small_data(tmp_path)
    real = memory_forget._run_step

    def compact_fails(layout, store, archive, step, at):
        if step["store"] == "compact":
            raise OSError("busy")
        return real(layout, store, archive, step, at)

    monkeypatch.setattr(memory_forget, "_run_step", compact_fails)
    first = apply_forget(layout, plan_forget(layout, ForgetRequest.parse([], [CANARY])), at=NOW)
    monkeypatch.undo()
    assert first["job"]["state"] == "failed"
    assert {"store": "compact", "error": "E_OS (OSError)"} in first["job"]["failed_steps"]
    assert store.read_text(MemoryLayer.EPISODIC, "chat-1") is None  # removed already

    compacted: list[str] = []
    real_compact = memory_removal._compact

    def spy(path, *, wal):
        compacted.append(Path(path).name)
        return real_compact(path, wal=wal)

    monkeypatch.setattr(memory_removal, "_compact", spy)
    again = resume_forget(layout, first["job"]["id"], at=NOW, queries=(CANARY,))

    assert again["job"]["state"] == "completed"
    assert compacted == [store.index_path.name]


# -- D18: a busy WAL checkpoint is not counted as done ---------------------------------


def test_a_busy_wal_checkpoint_raises(tmp_path, monkeypatch):
    path = tmp_path / "turns.db"
    writer = sqlite3.connect(path)
    writer.execute("PRAGMA journal_mode = WAL")
    writer.execute("CREATE TABLE t (x TEXT)")
    writer.execute("INSERT INTO t VALUES ('a')")
    writer.commit()
    reader = sqlite3.connect(path)
    reader.execute("BEGIN")
    reader.execute("SELECT * FROM t").fetchall()  # holds a read snapshot of the WAL
    writer.execute("DELETE FROM t")
    writer.commit()
    monkeypatch.setattr(memory_removal, "_BUSY_TIMEOUT_SEC", 0.1, raising=False)
    try:
        with pytest.raises(sqlite3.OperationalError):
            memory_removal._compact(path, wal=True)
    finally:
        reader.rollback()
        reader.close()
        writer.close()
    memory_removal._compact(path, wal=True)  # nothing holds it now: no error


# -- D21/D22: the audit line after a removal ------------------------------------------


def _removed(tmp_path: Path):
    data, store, layout = _small_data(tmp_path)
    (data / "checkpoints").mkdir(mode=0o700)
    checkpoint = data / "checkpoints" / "cp-1.json"
    checkpoint.write_bytes(b'{"history": "' + b"x" * 4096 + b'"}')
    plan = RemovalPlan(
        memory=[(MemoryLayer.EPISODIC, "chat-1")], files=[("checkpoint", checkpoint)]
    )
    return data, store, layout, plan, checkpoint


def test_a_failed_applied_audit_line_still_returns_the_record_id(tmp_path, monkeypatch):
    _data, store, layout, plan, _cp = _removed(tmp_path)
    real = RemovalArchive.audit

    def audit(self, event, **fields):
        if event == "remove.applied":
            raise ForgettingArchiveError("audit log not writable")
        return real(self, event, **fields)

    monkeypatch.setattr(RemovalArchive, "audit", audit)
    result = execute(layout, plan, purpose="retention", reason="test", at=NOW)

    assert result.applied is True
    assert result.record_id is not None
    assert result.error == "E_AUDIT: remove.applied not logged"
    assert store.read_text(MemoryLayer.EPISODIC, "chat-1") is None
    monkeypatch.undo()
    assert restore(layout, result.record_id).refused is None  # the id still restores it


# -- D20/D23: a write that fails part way through a restore ---------------------------


def test_a_failed_file_write_leaves_no_partial_file_and_the_restore_can_be_rerun(
    tmp_path, monkeypatch
):
    _data, store, layout, plan, checkpoint = _removed(tmp_path)
    original = checkpoint.read_bytes()
    removal = execute(layout, plan, purpose="retention", reason="test", at=NOW)
    assert removal.applied and not checkpoint.exists()
    real_write = memory_removal.os.write

    checkpoint_fd = []

    def short_then_full_disk(fd, data):
        inode = os.fstat(fd).st_ino
        if not checkpoint_fd and bytes(data[:10]).startswith(b'{"history'):
            checkpoint_fd.append(inode)  # the checkpoint: a short write, then a full disk
            return real_write(fd, bytes(data[:1024]))
        if checkpoint_fd and inode == checkpoint_fd[0]:
            raise OSError(28, "No space left on device")
        return real_write(fd, data)  # anything else (the audit log) is written

    monkeypatch.setattr(memory_removal.os, "write", short_then_full_disk)
    first = restore(layout, removal.record_id)
    monkeypatch.undo()

    assert first.refused is not None and "partly restored" in first.refused
    assert first.restored.get("memory") == 1  # what was written is reported
    assert not checkpoint.exists()  # no partial copy blocks the next run
    assert RemovalArchive(layout).ids() == [removal.record_id]  # the record stays
    assert '"restore.refused"' in layout.audit_log.read_text(encoding="utf-8")

    again = restore(layout, removal.record_id)
    assert again.refused is None and again.cleanup_error is None
    assert checkpoint.read_bytes() == original
    assert store.read_text(MemoryLayer.EPISODIC, "chat-1") is not None


@pytest.mark.parametrize("event", ["restore.started", "restore.applied"])
def test_a_failed_restore_audit_line_is_a_result_not_an_exception(tmp_path, monkeypatch, event):
    _data, _store, layout, plan, checkpoint = _removed(tmp_path)
    removal = execute(layout, plan, purpose="retention", reason="test", at=NOW)
    real = RemovalArchive.audit

    def audit(self, name, **fields):
        if name == event:
            raise ForgettingArchiveError("audit log not writable")
        return real(self, name, **fields)

    monkeypatch.setattr(RemovalArchive, "audit", audit)
    result = restore(layout, removal.record_id)
    monkeypatch.undo()

    assert RemovalArchive(layout).ids() == [removal.record_id]  # kept either way
    if event == "restore.started":
        assert result.refused is not None and result.restored == {}
        assert not checkpoint.exists()  # nothing was written
    else:
        assert result.refused is None
        assert result.cleanup_error == "E_AUDIT: restore.applied not logged; the record is kept"
        assert checkpoint.exists()
    again = restore(layout, removal.record_id)  # finishes (and logs) it
    assert again.refused is None and again.cleanup_error is None
    assert RemovalArchive(layout).ids() == []


# -- D14/D15: the consolidation's pair comparison checks cancel/breaker/time ----------


def test_clustering_checks_for_a_stop_while_it_compares_pairs():
    entries = [
        SimpleNamespace(key=f"k{n:04d}", content=f"alpha beta gamma {n}") for n in range(200)
    ]
    calls = []

    def check():
        calls.append(1)
        if len(calls) == 2:
            raise _Stop("cancelled")

    with pytest.raises(_Stop):
        MemoryJobExecutor._clusters(entries, check)
    assert len(calls) == 2
    # Without a check the result is unchanged (one cluster of all similar entries).
    assert len(MemoryJobExecutor._clusters(entries[:5])) == 1


def test_a_stop_during_clustering_ends_the_run_before_any_model_call(tmp_path):
    """The breaker leaves NORMAL while pairs are compared (the second check inside the
    comparison): the run is cancelled with that reason, its plan says so, no model call."""
    from tests.test_memory_jobs_phase0 import FakeModel, _executor, _plans

    store = MemoryStore(tmp_path / "memory", "a")
    for n in range(70):  # two checks inside the comparison (rows 0 and 64)
        store.write(MemoryLayer.SEMANTIC, f"knowledge/general/g{n:03d}", f"garden tomato {n}", {})
    levels = iter(["normal", "normal"])  # the run's start, then row 0
    model = FakeModel()
    executor = _executor(store, model, breaker=lambda: next(levels, "warning"))
    ctx = SimpleNamespace(
        job=SimpleNamespace(
            action="memory_consolidate",
            job_id="j1",
            budget=SimpleNamespace(max_tokens_per_run=100_000),
        ),
        run_id="r1",
        cancelled=False,
    )
    try:
        outcome = executor(ctx)
    finally:
        executor.close()
    assert (outcome.status, outcome.reason) == ("cancelled", "breaker")
    assert model.calls == []
    assert _plans(store, "memory_consolidate")[0]["stopped_by"] == "breaker"


def test_clustering_with_a_check_keeps_every_member():
    entries = [
        SimpleNamespace(key=f"k{n:04d}", content=f"alpha beta gamma {n}") for n in range(150)
    ]
    entries += [
        SimpleNamespace(key=f"z{n:04d}", content=f"delta epsilon zeta {n}") for n in range(3)
    ]
    calls = []
    with_check = MemoryJobExecutor._clusters(entries, lambda: calls.append(1))
    assert len(calls) == 3  # rows 0, 64, 128
    assert with_check == MemoryJobExecutor._clusters(entries)
    assert [len(group) for group in with_check] == [150, 3]


# -- review round 1: a restore that failed part way is finished by running it again ------


def _semantic_removed(tmp_path: Path):
    from stiracore.control.harvest import write_semantic_index
    from stiracore.memory_origin import record_origin

    _data, store, layout = _small_data(tmp_path)
    store.write(MemoryLayer.SEMANTIC, "knowledge/general/fact", "a restored fact", {"title": "f"})
    record_origin(store, MemoryLayer.SEMANTIC, "knowledge/general/fact", "person", "web-chat")
    write_semantic_index(store)
    path = store._path(MemoryLayer.SEMANTIC, "knowledge/general/fact")
    mtime = path.stat().st_mtime_ns - 5_000_000_000  # an older mtime the record keeps
    os.utime(path, ns=(mtime, mtime))
    plan = RemovalPlan(memory=[(MemoryLayer.SEMANTIC, "knowledge/general/fact")])
    removal = execute(layout, plan, purpose="retention", reason="test", at=NOW)
    assert removal.applied and removal.error is None
    return store, layout, removal.record_id, path, mtime


def _index_lists_fact(store: MemoryStore) -> bool:
    text = store.read_text(MemoryLayer.SEMANTIC, "index") or ""
    return "| semantic/knowledge/general/fact.md |" in text


def test_a_failed_index_regeneration_is_finished_by_the_next_restore(tmp_path, monkeypatch):
    from stiracore.control import harvest

    store, layout, record_id, _path, _mtime = _semantic_removed(tmp_path)
    assert not _index_lists_fact(store)

    def broken(_store):
        raise OSError("disk")

    monkeypatch.setattr(harvest, "write_semantic_index", broken)
    for _attempt in range(2):  # the entry is back after the first; the index still fails
        result = restore(layout, record_id)
        assert result.refused is not None and "partly restored" in result.refused
        assert RemovalArchive(layout).ids() == [record_id]  # not a success: the record stays
    assert store.read_text(MemoryLayer.SEMANTIC, "knowledge/general/fact") is not None
    assert not _index_lists_fact(store)
    monkeypatch.undo()

    again = restore(layout, record_id)
    assert again.refused is None and again.cleanup_error is None
    assert _index_lists_fact(store)
    assert RemovalArchive(layout).ids() == []


@pytest.mark.parametrize("failing", ["mtime", "origin"])
def test_a_failed_attribute_after_the_body_is_finished_by_the_next_restore(
    tmp_path, monkeypatch, failing
):
    from stiracore import memory_origin
    from stiracore.memory_origin import verified_origin

    store, layout, record_id, path, mtime = _semantic_removed(tmp_path)
    if failing == "mtime":
        target, name = memory_removal, "utime_nofollow"
    else:
        target, name = memory_origin, "record_origin"
    real = getattr(target, name)
    failures = []

    def once(*args, **kwargs):
        if not failures:
            failures.append(1)
            raise OSError("busy")
        return real(*args, **kwargs)

    monkeypatch.setattr(target, name, once)
    first = restore(layout, record_id)
    monkeypatch.undo()

    assert first.refused is not None and "partly restored" in first.refused
    assert first.restored == {"memory": 1}  # the body is back and counted
    assert RemovalArchive(layout).ids() == [record_id]  # unfinished: the record stays

    again = restore(layout, record_id)
    assert again.refused is None and again.cleanup_error is None
    assert again.restored == {} and again.skipped == 1
    assert path.stat().st_mtime_ns == mtime
    assert verified_origin(store, MemoryLayer.SEMANTIC, "knowledge/general/fact") == {
        "origin": "person",
        "source": "web-chat",
    }
    assert _index_lists_fact(store)
    assert RemovalArchive(layout).ids() == []
