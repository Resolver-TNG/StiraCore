"""Reversible, audited Semantic forgetting (#294).

``--apply-forgetting`` archives the original before it compresses an entry, refuses
to compress without an intact archive (fail-closed) and can only be undone through
the explicit, audited ``--undo-forgetting <id>``.
"""

from __future__ import annotations

import json
import os
import sqlite3
import stat
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from stiracore.control import forgetting_archive as archive_module
from stiracore.control.distillation_cli import main
from stiracore.control.distillers import DistillationDecision
from stiracore.control.forgetting_archive import (
    ARCHIVE_DIRNAME,
    AUDIT_LOG_NAME,
    ForgettingArchive,
    ForgettingArchiveError,
)
from stiracore.control.harvest import HarvestScheduler
from stiracore.control.memory_maintenance import SELECTION_BASIS, MemoryMaintenance
from stiracore.tools.memory_store import MemoryLayer, MemoryStore

NOW = datetime(2026, 9, 30, tzinfo=UTC)
MIDDLE = "needle-in-the-forgotten-middle"


def _body() -> str:
    return "Head of the fact.\n" + "x" * 1_500 + f" {MIDDLE} " + "y" * 1_500 + "\nTail."


def _age(store: MemoryStore, key: str, days: int, *, now: datetime = NOW) -> None:
    with sqlite3.connect(store.index_path) as connection:
        connection.execute(
            "UPDATE memory_entries SET last_accessed = ? WHERE layer = ? AND key = ?",
            ((now - timedelta(days=days)).timestamp(), MemoryLayer.SEMANTIC.value, key),
        )


def _store(tmp_path: Path) -> MemoryStore:
    return MemoryStore(tmp_path / "memory", "stira")


def _archive_dir(store: MemoryStore) -> Path:
    return store.memory_dir / ARCHIVE_DIRNAME


def test_apply_then_undo_restores_the_original_byte_for_byte(tmp_path):
    store = _store(tmp_path)
    store.write(MemoryLayer.SEMANTIC, "facts/one", _body(), {"title": "one", "source_id": "ep-1"})
    original = store.read_text(MemoryLayer.SEMANTIC, "facts/one")
    _age(store, "facts/one", 100)
    maintenance = MemoryMaintenance(store)

    result = maintenance.run_forgetting(now=NOW, apply=True)

    assert (result.applied, result.refused, result.error) == (1, 0, None)
    (item,) = result.candidates
    assert item.stage == "compact" and item.archive_id
    compressed = store.read(MemoryLayer.SEMANTIC, "facts/one")
    assert len(compressed.content) <= 2_000
    assert MIDDLE not in compressed.content
    assert compressed.metadata["forgetting_archive_id"] == item.archive_id
    record_path = _archive_dir(store) / f"{item.archive_id}.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert record["original_text"] == original
    assert record["key"] == "facts/one" and record["source_id"] == "ep-1"
    assert record["previous_stage"] == "none" and record["stage"] == "compact"
    # The archive is not a memory layer: the memory tools cannot find the original.
    assert store.search(MIDDLE) == []

    undone = maintenance.undo_forgetting(item.archive_id)

    assert undone.restored and undone.refused is None
    assert store.read_text(MemoryLayer.SEMANTIC, "facts/one") == original
    restored = store.read(MemoryLayer.SEMANTIC, "facts/one")
    assert "forgetting_stage" not in restored.metadata
    assert not record_path.exists()
    # Undo counts as a read: the next preview does not select the entry again.
    assert maintenance.run_forgetting(now=datetime.now(tz=UTC)).candidates == ()


def test_every_step_is_in_the_hash_chained_audit_log_without_content(tmp_path):
    store = _store(tmp_path)
    store.write(MemoryLayer.SEMANTIC, "one", _body())
    _age(store, "one", 40)
    maintenance = MemoryMaintenance(store)
    (item,) = maintenance.run_forgetting(now=NOW, apply=True).candidates
    assert not maintenance.undo_forgetting("20260101T000000Z-0000000000000000").restored
    assert maintenance.undo_forgetting(item.archive_id).restored

    events = ForgettingArchive(store).events()

    assert [event["kind"] for event in events] == [
        "forget.started",
        "forget.applied",
        "undo.refused",
        "undo.started",
        "undo.applied",
    ]
    assert events[1]["body"]["id"] == item.archive_id
    assert events[-1]["body"]["key"] == "one"
    raw = (_archive_dir(store) / AUDIT_LOG_NAME).read_text(encoding="utf-8")
    assert MIDDLE not in raw and "Head of the fact" not in raw


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission bits")
def test_archive_is_owner_only(tmp_path):
    store = _store(tmp_path)
    store.write(MemoryLayer.SEMANTIC, "one", _body())
    _age(store, "one", 40)
    (item,) = MemoryMaintenance(store).run_forgetting(now=NOW, apply=True).candidates

    directory = _archive_dir(store)
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    for name in (f"{item.archive_id}.json", AUDIT_LOG_NAME):
        assert stat.S_IMODE((directory / name).stat().st_mode) == 0o600


def test_archive_is_on_the_read_and_write_floor_of_the_file_tools(tmp_path):
    from stiracore.config.data_dir import build_policy
    from stiracore.config.models import AgentConfig

    data_dir = (tmp_path / "stira").resolve()
    (data_dir / "workspace").mkdir(parents=True)
    store = MemoryStore(data_dir / "memory", "stira")
    store.write(MemoryLayer.SEMANTIC, "one", _body())
    _age(store, "one", 40)
    (item,) = MemoryMaintenance(store).run_forgetting(now=NOW, apply=True).candidates
    policy, _ = build_policy(AgentConfig(), data_dir)

    for name in (f"{item.archive_id}.json", AUDIT_LOG_NAME):
        target = str(_archive_dir(store) / name)
        assert policy.check(target).code == "floor"
        assert not policy.check_read(target).allowed


def test_preview_lists_reasons_and_writes_nothing(tmp_path):
    store = _store(tmp_path)
    store.write(MemoryLayer.SEMANTIC, "useful-but-unsearched", _body())
    original = store.read_text(MemoryLayer.SEMANTIC, "useful-but-unsearched")
    _age(store, "useful-but-unsearched", 200)

    (item,) = MemoryMaintenance(store).run_forgetting(now=NOW).candidates

    shown = item.as_dict()
    assert shown["stage"] == "archive" and shown["threshold_days"] == 180
    assert shown["current_stage"] == "none" and shown["limit"] == 800
    assert shown["chars"] == len(_body())
    assert "last_accessed 200 days ago >= 180 days" in shown["reason"]
    assert shown["archive_id"] is None
    assert store.read_text(MemoryLayer.SEMANTIC, "useful-but-unsearched") == original
    assert not _archive_dir(store).exists()


@pytest.mark.parametrize("breakage", ["file", "symlink", "broken_log"])
def test_apply_is_refused_without_a_usable_archive(tmp_path, breakage):
    store = _store(tmp_path)
    store.write(MemoryLayer.SEMANTIC, "one", _body())
    store.write(MemoryLayer.SEMANTIC, "two", _body())
    before = {key: store.read_text(MemoryLayer.SEMANTIC, key) for key in ("one", "two")}
    for key in before:
        _age(store, key, 100)
    directory = _archive_dir(store)
    if breakage == "file":
        directory.write_text("not a directory", encoding="utf-8")
    elif breakage == "symlink":
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        try:
            directory.symlink_to(elsewhere, target_is_directory=True)
        except OSError:
            pytest.skip("symlinks unavailable")
    else:
        directory.mkdir()
        (directory / AUDIT_LOG_NAME).write_text('{"seq": 1}\n', encoding="utf-8")

    result = MemoryMaintenance(store).run_forgetting(now=NOW, apply=True)

    assert (result.applied, result.refused) == (0, 2)
    assert result.error
    assert all(item.refused and item.archive_id is None for item in result.candidates)
    assert {key: store.read_text(MemoryLayer.SEMANTIC, key) for key in before} == before


def test_a_record_that_cannot_be_written_stops_before_the_overwrite(tmp_path, monkeypatch):
    store = _store(tmp_path)
    for key in ("a", "b", "c"):
        store.write(MemoryLayer.SEMANTIC, key, _body())
        _age(store, key, 100)
    before = {key: store.read_text(MemoryLayer.SEMANTIC, key) for key in ("a", "b", "c")}
    real_write = archive_module._write_all
    calls = []

    def flaky(fd, data):
        if b'"original_text"' in data:  # archive records only, not audit lines
            calls.append(fd)
            if len(calls) == 2:
                raise OSError("disk full")
        real_write(fd, data)

    monkeypatch.setattr(archive_module, "_write_all", flaky)

    result = MemoryMaintenance(store).run_forgetting(now=NOW, apply=True)

    assert (result.applied, result.refused) == (1, 2)
    applied = [item.key for item in result.candidates if item.archive_id]
    untouched = [key for key in before if key not in applied]
    assert len(applied) == 1 and len(untouched) == 2
    for key in untouched:
        assert store.read_text(MemoryLayer.SEMANTIC, key) == before[key]
    assert store.read_text(MemoryLayer.SEMANTIC, applied[0]) != before[applied[0]]
    kinds = [event["kind"] for event in ForgettingArchive(store).events()]
    assert kinds == ["forget.started", "forget.applied", "forget.refused"]


def test_record_that_does_not_read_back_intact_blocks_the_overwrite(tmp_path, monkeypatch):
    store = _store(tmp_path)
    store.write(MemoryLayer.SEMANTIC, "one", _body())
    before = store.read_text(MemoryLayer.SEMANTIC, "one")
    _age(store, "one", 100)
    real_write = archive_module._write_all

    def truncated(fd, data):
        real_write(fd, data[: len(data) // 2] if b'"original_text"' in data else data)

    monkeypatch.setattr(archive_module, "_write_all", truncated)

    result = MemoryMaintenance(store).run_forgetting(now=NOW, apply=True)

    assert (result.applied, result.refused) == (0, 1)
    assert store.read_text(MemoryLayer.SEMANTIC, "one") == before


def test_undo_is_refused_for_a_damaged_or_missing_record(tmp_path):
    store = _store(tmp_path)
    store.write(MemoryLayer.SEMANTIC, "one", _body())
    _age(store, "one", 100)
    maintenance = MemoryMaintenance(store)
    (item,) = maintenance.run_forgetting(now=NOW, apply=True).candidates
    compressed = store.read_text(MemoryLayer.SEMANTIC, "one")
    path = _archive_dir(store) / f"{item.archive_id}.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    record["original_text"] = record["original_text"].replace("Head", "Forged")
    path.write_text(json.dumps(record), encoding="utf-8")

    assert "integrity" in maintenance.undo_forgetting(item.archive_id).refused
    assert "not a forgetting archive id" in maintenance.undo_forgetting("../one").refused
    path.unlink()
    assert "no archive record" in maintenance.undo_forgetting(item.archive_id).refused
    assert store.read_text(MemoryLayer.SEMANTIC, "one") == compressed


def test_undo_is_refused_when_the_entry_changed_and_steps_undo_in_order(tmp_path):
    store = _store(tmp_path)
    store.write(MemoryLayer.SEMANTIC, "one", _body())
    original = store.read_text(MemoryLayer.SEMANTIC, "one")
    maintenance = MemoryMaintenance(store)
    _age(store, "one", 40)
    (light,) = maintenance.run_forgetting(now=NOW, apply=True).candidates
    after_light = store.read_text(MemoryLayer.SEMANTIC, "one")
    _age(store, "one", 100)
    (compact,) = maintenance.run_forgetting(now=NOW, apply=True).candidates
    assert (light.stage, compact.stage, compact.current_stage) == ("light", "compact", "light")

    refused = maintenance.undo_forgetting(light.archive_id)
    assert not refused.restored and "undo the later step first" in refused.refused

    assert maintenance.undo_forgetting(compact.archive_id).restored
    assert store.read_text(MemoryLayer.SEMANTIC, "one") == after_light
    assert maintenance.undo_forgetting(light.archive_id).restored
    assert store.read_text(MemoryLayer.SEMANTIC, "one") == original

    # An entry edited after it was forgotten is never silently overwritten by undo.
    _age(store, "one", 40, now=datetime.now(tz=UTC))
    (again,) = maintenance.run_forgetting(now=datetime.now(tz=UTC), apply=True).candidates
    store.write(MemoryLayer.SEMANTIC, "one", "edited by a person")
    result = maintenance.undo_forgetting(again.archive_id)
    assert not result.restored
    assert store.read(MemoryLayer.SEMANTIC, "one").content == "edited by a person"


class _AutoDistiller:
    version = "test/auto-v1"

    def distill(self, content, metadata):
        return DistillationDecision(0.55, "auto", "durable fact")


def test_harvest_marks_stay_consistent_through_forgetting_and_undo(tmp_path):
    store = _store(tmp_path)
    store.write(MemoryLayer.EPISODIC, "chat-1", _body(), {"tags": ["design"]})
    # The auto band stages by default now (#293); this test is about forgetting a
    # harvested entry, so it pins the direct-write behaviour.
    harvest = HarvestScheduler(store, None, "stira", distiller=_AutoDistiller(), stage_auto=False)
    assert harvest.run_distillation().knowledge == 1
    (semantic,) = [
        entry
        for entry in store.list_entries(MemoryLayer.SEMANTIC, limit=10)
        if entry.key != "index"
    ]
    harvested_text = store.read_text(MemoryLayer.SEMANTIC, semantic.key)
    episode_before = store.read_text(MemoryLayer.EPISODIC, "chat-1")
    _age(store, semantic.key, 100)
    maintenance = MemoryMaintenance(store)

    (item,) = maintenance.run_forgetting(now=NOW, apply=True).candidates
    # While compressed: the episode stays marked, Harvest does not re-distil it.
    second = harvest.run_distillation()
    assert (second.skipped, second.knowledge, second.semantic) == (1, 0, 0)
    assert maintenance.undo_forgetting(item.archive_id).restored

    restored = store.read(MemoryLayer.SEMANTIC, semantic.key)
    assert store.read_text(MemoryLayer.SEMANTIC, semantic.key) == harvested_text
    assert restored.metadata["source_id"] == "chat-1"
    assert restored.metadata["harvest_version"] == _AutoDistiller.version
    episode = store.read(MemoryLayer.EPISODIC, "chat-1")
    assert store.read_text(MemoryLayer.EPISODIC, "chat-1") == episode_before
    assert episode.metadata["harvested"] is True
    third = harvest.run_distillation()
    assert (third.skipped, third.knowledge, third.semantic, third.errors) == (1, 0, 0, 0)
    keys = [e.key for e in store.list_entries(MemoryLayer.SEMANTIC, limit=10) if e.key != "index"]
    assert keys == [semantic.key]


def test_cli_preview_apply_list_and_undo(tmp_path, capsys):
    base = tmp_path / "memory"
    config = tmp_path / "agent.json"
    config.write_text("{}", encoding="utf-8")
    store = MemoryStore(base, "stira")
    store.write(MemoryLayer.SEMANTIC, "one", _body())
    original = store.read_text(MemoryLayer.SEMANTIC, "one")
    _age(store, "one", 100, now=datetime.now(tz=UTC))
    common = ["--base-dir", str(base), "--config", str(config)]

    assert main(common) == 0
    preview = json.loads(capsys.readouterr().out)["forgetting"]
    assert preview["selection"] == SELECTION_BASIS
    assert (preview["candidates"], preview["applied"]) == (1, 0)
    assert preview["items"][0]["key"] == "one" and preview["items"][0]["reason"]
    assert MIDDLE not in json.dumps(preview)

    assert main([*common, "--apply-forgetting"]) == 0
    applied = json.loads(capsys.readouterr().out)["forgetting"]
    archive_id = applied["items"][0]["archive_id"]
    assert applied["applied"] == 1 and archive_id

    assert main(["--base-dir", str(base), "--list-forgotten"]) == 0
    listed = json.loads(capsys.readouterr().out)["forgotten"]
    assert [entry["id"] for entry in listed] == [archive_id]
    assert "original_text" not in listed[0]

    assert main(["--base-dir", str(base), "--undo-forgetting", archive_id]) == 0
    assert json.loads(capsys.readouterr().out)["undo"]["restored"] is True
    assert store.read_text(MemoryLayer.SEMANTIC, "one") == original
    assert main(["--base-dir", str(base), "--undo-forgetting", archive_id]) == 1
    assert "no archive record" in json.loads(capsys.readouterr().out)["undo"]["refused"]


def test_cli_apply_exits_non_zero_when_refused(tmp_path, capsys):
    base = tmp_path / "memory"
    config = tmp_path / "agent.json"
    config.write_text("{}", encoding="utf-8")
    store = MemoryStore(base, "stira")
    store.write(MemoryLayer.SEMANTIC, "one", _body())
    _age(store, "one", 100, now=datetime.now(tz=UTC))
    (store.memory_dir / ARCHIVE_DIRNAME).write_text("x", encoding="utf-8")

    code = main(["--base-dir", str(base), "--config", str(config), "--apply-forgetting"])

    payload = json.loads(capsys.readouterr().out)["forgetting"]
    assert code == 1 and payload["refused"] == 1 and payload["applied"] == 0


def test_archive_never_replaces_an_existing_record(tmp_path):
    store = _store(tmp_path)
    store.write(MemoryLayer.SEMANTIC, "one", _body())
    _age(store, "one", 100)
    (item,) = MemoryMaintenance(store).run_forgetting(now=NOW, apply=True).candidates
    archive = ForgettingArchive(store)
    record = archive.load(item.archive_id)

    with pytest.raises(ForgettingArchiveError, match="FileExistsError"):
        archive.save(record)
    assert archive.load(item.archive_id) == record


# --- review round 1 (#320): no-follow opens, audit failure after a change, fsync, keys ----

posix_only = pytest.mark.skipif(os.name != "posix", reason="POSIX links/permissions/dir fds")


def _aged_store(tmp_path: Path, *keys: str) -> MemoryStore:
    store = _store(tmp_path)
    for key in keys or ("one",):
        store.write(MemoryLayer.SEMANTIC, key, _body())
        _age(store, key, 100)
    return store


@posix_only
@pytest.mark.parametrize("kind", ["symlink", "dangling_symlink", "loose_mode", "directory"])
def test_an_audit_log_that_is_not_a_private_regular_file_refuses(tmp_path, kind):
    store = _aged_store(tmp_path)
    before = store.read_text(MemoryLayer.SEMANTIC, "one")
    directory = _archive_dir(store)
    directory.mkdir(mode=0o700)
    log = directory / AUDIT_LOG_NAME
    outside = tmp_path / "outside.jsonl"
    if kind == "symlink":
        outside.write_text("", encoding="utf-8")
        log.symlink_to(outside)
    elif kind == "dangling_symlink":
        log.symlink_to(outside)
    elif kind == "loose_mode":
        log.write_text("", encoding="utf-8")
        log.chmod(0o644)
    else:
        log.mkdir()

    result = MemoryMaintenance(store).run_forgetting(now=NOW, apply=True)

    assert (result.applied, result.refused) == (0, 1)
    expected = {
        "symlink": "symbolic link",
        "dangling_symlink": "symbolic link",
        "loose_mode": "not private",
        "directory": "not a regular file",
    }[kind]
    assert expected in result.error
    assert store.read_text(MemoryLayer.SEMANTIC, "one") == before
    if kind == "symlink":
        assert outside.read_text(encoding="utf-8") == ""
    if kind == "dangling_symlink":
        assert not outside.exists()
    assert [p.name for p in directory.iterdir() if p.suffix == ".json"] == []


@posix_only
def test_archive_records_are_never_written_or_read_through_a_link(tmp_path):
    store = _aged_store(tmp_path)
    maintenance = MemoryMaintenance(store)
    (item,) = maintenance.run_forgetting(now=NOW, apply=True).candidates
    archive = ForgettingArchive(store)
    record = archive.load(item.archive_id)
    directory = _archive_dir(store)

    # Read: a record replaced by a link (to a valid copy) is refused, also for undo.
    copy = tmp_path / "copy.json"
    (directory / f"{item.archive_id}.json").rename(copy)
    (directory / f"{item.archive_id}.json").symlink_to(copy)
    with pytest.raises(ForgettingArchiveError, match="symbolic link"):
        ForgettingArchive(store).load(item.archive_id)
    assert "symbolic link" in maintenance.undo_forgetting(item.archive_id).refused

    # Write: a planted (dangling) link at the record's name is not followed.
    target = tmp_path / "planted-target.json"
    planted = replace(record, id="20260101T000000Z-1111111111111111")
    (directory / f"{planted.id}.json").symlink_to(target)
    with pytest.raises(ForgettingArchiveError, match="not written"):
        archive.save(planted)
    archive.close()
    assert not target.exists()


@posix_only
def test_a_record_with_loose_permissions_is_refused(tmp_path):
    store = _aged_store(tmp_path)
    maintenance = MemoryMaintenance(store)
    (item,) = maintenance.run_forgetting(now=NOW, apply=True).candidates
    (_archive_dir(store) / f"{item.archive_id}.json").chmod(0o644)

    assert "not private" in maintenance.undo_forgetting(item.archive_id).refused


def _fail_kinds(monkeypatch, *kinds):
    real = archive_module._AuditLog.append

    def append(self, kind, body):
        if kind in kinds:
            raise ForgettingArchiveError(f"injected failure for {kind}")
        return real(self, kind, body)

    monkeypatch.setattr(archive_module._AuditLog, "append", append)


def test_audit_failure_after_compression_reports_applied_and_reconciles(
    tmp_path, monkeypatch, capsys
):
    store = _aged_store(tmp_path, "a", "b")
    before = {key: store.read_text(MemoryLayer.SEMANTIC, key) for key in ("a", "b")}
    _fail_kinds(monkeypatch, "forget.applied")

    result = MemoryMaintenance(store).run_forgetting(now=NOW, apply=True)

    assert (result.applied, result.audit_incomplete, result.refused) == (1, 1, 1)
    first, second = result.candidates
    assert first.archive_id and "injected" in first.audit_error and first.refused is None
    assert second.archive_id is None and second.refused.startswith("stopped")
    assert store.read_text(MemoryLayer.SEMANTIC, first.key) != before[first.key]
    assert store.read_text(MemoryLayer.SEMANTIC, second.key) == before[second.key]
    assert (_archive_dir(store) / f"{first.archive_id}.json").exists()  # record kept

    monkeypatch.undo()
    rerun = MemoryMaintenance(store).run_forgetting(now=NOW, apply=True)

    assert rerun.reconciled == ({"id": first.archive_id, "action": "forget.recovered"},)
    assert rerun.applied == 1 and rerun.audit_incomplete == 0
    kinds = [e["kind"] for e in ForgettingArchive(store).events()]
    assert kinds.count("forget.recovered") == 1
    assert MemoryMaintenance(store).undo_forgetting(first.archive_id).restored
    assert store.read_text(MemoryLayer.SEMANTIC, first.key) == before[first.key]


def _why_not_reconciled(store, archive_id):
    """What :meth:`MemoryMaintenance.reconcile` saw, for a failure message (#662).

    The CI job that runs the whole suite on 3.12 reports ``reconciled == []`` here, while
    the test passes alone and with its own file on 3.12 and 3.11 and in the full suite on
    3.11. The branch that is taken depends on the kinds logged for the record and on the
    entry's current digest, so the message shows both instead of only the empty list.
    """
    from stiracore.control.forgetting_archive import sha256_text

    maintenance = MemoryMaintenance(store)
    try:
        maintenance.archive.prepare()
        kinds = sorted(
            event["kind"]
            for event in maintenance.archive.logged()
            if event["body"].get("id") == archive_id
        )
        record = maintenance.archive.load(archive_id)
        current = store.read_text(MemoryLayer.SEMANTIC, record.key)
        return (
            f"ids={maintenance.archive.ids()} kinds={kinds} key={record.key!r} "
            f"digest={None if current is None else sha256_text(current)} "
            f"compressed={record.compressed_sha256} original={record.original_sha256}"
        )
    except Exception as error:  # noqa: BLE001 - a diagnosis, never the result
        return f"diagnosis failed: {type(error).__name__}: {error}"
    finally:
        maintenance.archive.close()


def test_cli_exits_non_zero_when_the_audit_line_after_a_change_fails(tmp_path, monkeypatch, capsys):
    base = tmp_path / "memory"
    config = tmp_path / "agent.json"
    config.write_text("{}", encoding="utf-8")
    store = MemoryStore(base, "stira")
    store.write(MemoryLayer.SEMANTIC, "one", _body())
    _age(store, "one", 100, now=datetime.now(tz=UTC))
    _fail_kinds(monkeypatch, "forget.applied")

    code = main(["--base-dir", str(base), "--config", str(config), "--apply-forgetting"])

    payload = json.loads(capsys.readouterr().out)["forgetting"]
    assert code == 1
    assert (payload["applied"], payload["audit_incomplete"]) == (1, 1)
    archive_id = payload["items"][0]["archive_id"]
    assert payload["items"][0]["audit_error"]

    _fail_kinds(monkeypatch, "undo.applied")
    code = main(["--base-dir", str(base), "--undo-forgetting", archive_id])
    undo = json.loads(capsys.readouterr().out)["undo"]
    assert code == 1 and undo["restored"] is True and undo["refused"] is None
    assert "injected" in undo["audit_error"]
    # The interrupted apply was completed in the log by this run before the undo.
    assert undo["reconciled"] == [{"id": archive_id, "action": "forget.recovered"}], (
        _why_not_reconciled(store, archive_id)
    )


def test_audit_failure_after_restore_reports_restored_and_reconciles(tmp_path, monkeypatch):
    store = _aged_store(tmp_path, "one", "two")
    original = store.read_text(MemoryLayer.SEMANTIC, "one")
    maintenance = MemoryMaintenance(store)
    first, second = maintenance.run_forgetting(now=NOW, apply=True).candidates
    one = first if first.key == "one" else second
    other = second if one is first else first
    _fail_kinds(monkeypatch, "undo.applied")

    result = maintenance.undo_forgetting(one.archive_id)

    assert result.restored and result.refused is None and "injected" in result.audit_error
    assert store.read_text(MemoryLayer.SEMANTIC, "one") == original
    assert (_archive_dir(store) / f"{one.archive_id}.json").exists()  # kept for the log

    monkeypatch.undo()
    later = maintenance.undo_forgetting(other.archive_id)  # any later apply/undo completes it

    assert later.restored
    assert later.reconciled == ({"id": one.archive_id, "action": "undo.recovered"},)
    assert not (_archive_dir(store) / f"{one.archive_id}.json").exists()
    assert [e["kind"] for e in ForgettingArchive(store).events()][-3:] == [
        "undo.recovered",
        "undo.started",
        "undo.applied",
    ]


def test_audit_failure_before_any_change_changes_nothing_and_leaves_no_copy(tmp_path, monkeypatch):
    store = _aged_store(tmp_path)
    before = store.read_text(MemoryLayer.SEMANTIC, "one")
    _fail_kinds(monkeypatch, "forget.started")

    result = MemoryMaintenance(store).run_forgetting(now=NOW, apply=True)

    assert (result.applied, result.refused, result.audit_incomplete) == (0, 1, 0)
    assert store.read_text(MemoryLayer.SEMANTIC, "one") == before
    assert list(_archive_dir(store).glob("*.json")) == []

    monkeypatch.undo()
    maintenance = MemoryMaintenance(store)
    (item,) = maintenance.run_forgetting(now=NOW, apply=True).candidates
    _fail_kinds(monkeypatch, "undo.started")
    refused = maintenance.undo_forgetting(item.archive_id)
    assert not refused.restored and "injected" in refused.refused
    assert store.read_text(MemoryLayer.SEMANTIC, "one") != before


@posix_only
def test_the_directory_is_fsynced_after_a_record_is_created(tmp_path, monkeypatch):
    store = _aged_store(tmp_path)
    synced = []
    real_fsync = os.fsync

    def spy(fd):
        mode = os.fstat(fd).st_mode
        synced.append("dir" if stat.S_ISDIR(mode) else "file")
        real_fsync(fd)

    monkeypatch.setattr(archive_module.os, "fsync", spy)

    MemoryMaintenance(store).run_forgetting(now=NOW, apply=True)

    # memory_dir (new forgotten/) → log created (file, dir) → record (file, dir) → …
    assert synced[:3] == ["dir", "file", "dir"]
    record_sync = synced.index("file", 3)
    assert synced[record_sync + 1] == "dir"


@pytest.mark.parametrize(
    "key",
    ["../procedural/rule", "/etc/passwd", "a\\b", "x/../../y", "", "a//b", "C:/x", "a\0b"],
)
def test_undo_refuses_a_record_key_outside_the_semantic_layer(tmp_path, key):
    store = _aged_store(tmp_path)
    maintenance = MemoryMaintenance(store)
    (item,) = maintenance.run_forgetting(now=NOW, apply=True).candidates
    path = _archive_dir(store) / f"{item.archive_id}.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    record["key"] = key
    path.write_text(json.dumps(record), encoding="utf-8")
    procedural = sorted(p.name for p in (store.memory_dir / "procedural").rglob("*"))

    result = maintenance.undo_forgetting(item.archive_id)

    if key == "C:/x":  # a plain relative key on POSIX ("C:" is one component)
        assert not result.restored and "no longer what this forgetting step wrote" in (
            result.refused
        )
    else:
        assert not result.restored and "outside the Semantic memory" in result.refused
    assert sorted(p.name for p in (store.memory_dir / "procedural").rglob("*")) == procedural
    assert not (tmp_path / "y.md").exists()


def test_undo_refuses_a_record_of_another_agent_or_layer(tmp_path):
    store = _aged_store(tmp_path)
    maintenance = MemoryMaintenance(store)
    (item,) = maintenance.run_forgetting(now=NOW, apply=True).candidates
    path = _archive_dir(store) / f"{item.archive_id}.json"
    good = json.loads(path.read_text(encoding="utf-8"))
    for change in ({"agent_id": "other"}, {"layer": "procedural"}, {"stage": "../x"}):
        path.write_text(json.dumps({**good, **change}), encoding="utf-8")
        refused = maintenance.undo_forgetting(item.archive_id).refused
        assert "another layer or agent" in refused or "integrity" in refused


@posix_only
def test_undo_refuses_a_key_that_reaches_outside_through_a_semantic_symlink(tmp_path):
    store = _aged_store(tmp_path)
    maintenance = MemoryMaintenance(store)
    (item,) = maintenance.run_forgetting(now=NOW, apply=True).candidates
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "x.md").write_text("victim", encoding="utf-8")
    (store.memory_dir / "semantic" / "link").symlink_to(outside, target_is_directory=True)
    path = _archive_dir(store) / f"{item.archive_id}.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    record["key"] = "link/x"
    record["compressed_sha256"] = archive_module.sha256_text("victim")
    path.write_text(json.dumps(record), encoding="utf-8")

    result = maintenance.undo_forgetting(item.archive_id)

    assert not result.restored and "outside the Semantic memory" in result.refused
    assert (outside / "x.md").read_text(encoding="utf-8") == "victim"


@pytest.mark.parametrize("agent_id", ["..", ".", "a/b", "/abs", "a\\b", ""])
def test_agent_id_cannot_leave_the_memory_base(tmp_path, agent_id, capsys):
    with pytest.raises(ValueError, match="agent_id"):
        MemoryStore(tmp_path / "memory", agent_id)
    with pytest.raises(ValueError, match="agent_id"):
        main(["--base-dir", str(tmp_path / "memory"), "--agent-id", agent_id, "--list-forgotten"])
    assert not (tmp_path / "forgotten").exists()


# --- review round 2 (#320): idempotent reconcile, clean-up failures, parent fsync, FIFOs ----


def _kinds(store: MemoryStore) -> list[str]:
    return [event["kind"] for event in ForgettingArchive(store).events()]


def _fail_remove(monkeypatch):
    def remove(self, archive_id):
        raise OSError("injected unlink failure")

    monkeypatch.setattr(ForgettingArchive, "remove", remove)


def test_reconcile_logs_forget_recovered_once_across_repeated_runs(tmp_path, monkeypatch):
    store = _aged_store(tmp_path)
    original = store.read_text(MemoryLayer.SEMANTIC, "one")
    _fail_kinds(monkeypatch, "forget.applied")
    (item,) = MemoryMaintenance(store).run_forgetting(now=NOW, apply=True).candidates
    monkeypatch.undo()

    for _ in range(3):
        MemoryMaintenance(store).run_forgetting(now=NOW, apply=True)
    assert _kinds(store).count("forget.recovered") == 1
    lines = len(_kinds(store))
    MemoryMaintenance(store).run_forgetting(now=NOW, apply=True)
    assert len(_kinds(store)) == lines  # nothing left to complete: no new line

    assert MemoryMaintenance(store).undo_forgetting(item.archive_id).restored
    assert store.read_text(MemoryLayer.SEMANTIC, "one") == original
    for _ in range(2):
        MemoryMaintenance(store).run_forgetting(now=NOW, apply=True)
    assert _kinds(store).count("forget.recovered") == 1
    assert _kinds(store).count("undo.applied") == 1


def test_reconcile_resumes_only_the_clean_up_after_a_failed_removal(tmp_path, monkeypatch):
    store = _aged_store(tmp_path)
    maintenance = MemoryMaintenance(store)
    (item,) = maintenance.run_forgetting(now=NOW, apply=True).candidates
    _fail_kinds(monkeypatch, "undo.applied")
    assert maintenance.undo_forgetting(item.archive_id).audit_error
    monkeypatch.undo()
    record = _archive_dir(store) / f"{item.archive_id}.json"

    _fail_remove(monkeypatch)
    first = MemoryMaintenance(store).run_forgetting(now=NOW, apply=True)
    monkeypatch.undo()

    (entry,) = first.reconciled
    assert entry["action"] == "undo.recovered" and "cleanup_error" in entry
    assert record.exists()
    second = MemoryMaintenance(store).run_forgetting(now=NOW, apply=True)
    assert second.reconciled == ({"id": item.archive_id, "action": "cleanup"},)
    assert not record.exists()
    assert MemoryMaintenance(store).run_forgetting(now=NOW, apply=True).reconciled == ()
    assert _kinds(store).count("undo.recovered") == 1


def test_an_interrupted_compression_is_abandoned_once(tmp_path, monkeypatch):
    store = _aged_store(tmp_path)
    before = store.read_text(MemoryLayer.SEMANTIC, "one")

    def crash(self, layer, key, text):
        raise RuntimeError("process killed before the overwrite")

    monkeypatch.setattr(MemoryStore, "write_text", crash)
    with pytest.raises(RuntimeError):
        MemoryMaintenance(store).run_forgetting(now=NOW, apply=True)
    monkeypatch.undo()
    assert store.read_text(MemoryLayer.SEMANTIC, "one") == before
    assert len(list(_archive_dir(store).glob("*.json"))) == 1

    _fail_remove(monkeypatch)
    first = MemoryMaintenance(store).run_forgetting(now=NOW, apply=True)
    monkeypatch.undo()
    assert first.reconciled[0]["action"] == "forget.abandoned"
    assert "cleanup_error" in first.reconciled[0]
    # The failed clean-up stopped nothing else: this run still compressed the entry.
    assert first.applied == 1

    MemoryMaintenance(store).run_forgetting(now=NOW, apply=True)
    assert _kinds(store).count("forget.abandoned") == 1
    abandoned = first.reconciled[0]["id"]
    assert not (_archive_dir(store) / f"{abandoned}.json").exists()


def test_undo_reports_restored_when_only_the_record_removal_fails(tmp_path, monkeypatch, capsys):
    base = tmp_path / "memory"
    store = MemoryStore(base, "stira")
    store.write(MemoryLayer.SEMANTIC, "one", _body())
    original = store.read_text(MemoryLayer.SEMANTIC, "one")
    _age(store, "one", 100)
    (item,) = MemoryMaintenance(store).run_forgetting(now=NOW, apply=True).candidates
    _fail_remove(monkeypatch)

    code = main(["--base-dir", str(base), "--undo-forgetting", item.archive_id])

    undo = json.loads(capsys.readouterr().out)["undo"]
    assert code == 1
    assert undo["restored"] is True and undo["refused"] is None and undo["audit_error"] is None
    assert "record not removed" in undo["cleanup_error"]
    assert store.read_text(MemoryLayer.SEMANTIC, "one") == original
    lines = len(_kinds(store))

    monkeypatch.undo()
    rerun = MemoryMaintenance(store).run_forgetting(now=NOW, apply=True)
    assert rerun.reconciled == ({"id": item.archive_id, "action": "cleanup"},)
    assert len(_kinds(store)) == lines  # clean-up only, no second undo line
    assert not (_archive_dir(store) / f"{item.archive_id}.json").exists()


def test_cli_fails_on_a_damaged_log_even_without_candidates(tmp_path, capsys):
    base = tmp_path / "memory"
    config = tmp_path / "agent.json"
    config.write_text("{}", encoding="utf-8")
    store = MemoryStore(base, "stira")
    directory = _archive_dir(store)
    directory.mkdir(mode=0o700)
    log = directory / AUDIT_LOG_NAME
    log.write_text("not json\n", encoding="utf-8")
    log.chmod(0o600)

    code = main(["--base-dir", str(base), "--config", str(config), "--apply-forgetting"])

    payload = json.loads(capsys.readouterr().out)["forgetting"]
    assert payload["candidates"] == 0 and payload["error"]
    assert code == 1


@posix_only
def test_memory_dir_is_fsynced_after_forgotten_is_created(tmp_path, monkeypatch):
    store = _aged_store(tmp_path)
    memory_ino = store.memory_dir.stat().st_ino
    synced = []
    real_fsync = os.fsync

    def spy(fd):
        synced.append(os.fstat(fd).st_ino)
        real_fsync(fd)

    monkeypatch.setattr(archive_module.os, "fsync", spy)

    MemoryMaintenance(store).run_forgetting(now=NOW, apply=True)

    forgotten_ino = _archive_dir(store).stat().st_ino
    assert memory_ino in synced and forgotten_ino in synced
    # mkdir → fsync(memory_dir) before anything inside forgotten/ is written or synced.
    assert synced.index(memory_ino) == min(
        i for i, ino in enumerate(synced) if ino in (memory_ino, forgotten_ino)
    )
    assert synced.index(memory_ino) < synced.index(forgotten_ino)
    assert synced[0] == memory_ino


def _finishes(target, timeout=5.0):
    import threading

    outcome = {}

    def run():
        try:
            outcome["value"] = target()
        except Exception as error:  # noqa: BLE001 - reported to the test
            outcome["error"] = error

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(timeout)
    assert not thread.is_alive(), "blocked on a FIFO"
    return outcome


@posix_only
def test_a_fifo_as_audit_log_is_refused_without_blocking(tmp_path):
    store = _aged_store(tmp_path)
    before = store.read_text(MemoryLayer.SEMANTIC, "one")
    directory = _archive_dir(store)
    directory.mkdir(mode=0o700)
    os.mkfifo(directory / AUDIT_LOG_NAME, 0o600)

    outcome = _finishes(lambda: MemoryMaintenance(store).run_forgetting(now=NOW, apply=True))

    result = outcome["value"]
    assert result.applied == 0 and "not a regular file" in result.error
    assert store.read_text(MemoryLayer.SEMANTIC, "one") == before


@posix_only
def test_a_fifo_as_archive_record_is_refused_without_blocking(tmp_path):
    store = _aged_store(tmp_path)
    maintenance = MemoryMaintenance(store)
    (item,) = maintenance.run_forgetting(now=NOW, apply=True).candidates
    compressed = store.read_text(MemoryLayer.SEMANTIC, "one")
    record = _archive_dir(store) / f"{item.archive_id}.json"
    record.unlink()
    os.mkfifo(record, 0o600)

    undo = _finishes(lambda: maintenance.undo_forgetting(item.archive_id))["value"]
    listed = _finishes(lambda: ForgettingArchive(store).summaries())["value"]

    assert not undo.restored and "not a regular file" in undo.refused
    assert listed == [{"id": item.archive_id, "error": f"{record.name} is not a regular file"}]
    assert store.read_text(MemoryLayer.SEMANTIC, "one") == compressed
