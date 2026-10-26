"""Harvest marking must not reorder Episodic memory (#292).

``HarvestScheduler`` rewrites each episode's frontmatter with its harvest
marks. ``list_entries`` orders by modification time, so before #292 the
newest episode (processed first) ended up the oldest file and
``ContextAssembler._memory_text("episodic", 5)`` put the five *oldest* turns
into the prompt. The marks are metadata: the rewrite keeps the file's
modification time.
"""

from __future__ import annotations

import os
import sqlite3
import stat
import time
from pathlib import Path

import pytest

from stiracore.agent.context_assembler import ContextAssembler
from stiracore.config import AgentConfig
from stiracore.control.distillation_cli import main
from stiracore.control.distillers import DistillationDecision, RuleBasedDistiller
from stiracore.control.harvest import HarvestScheduler
from stiracore.control.memory_maintenance import MemoryMaintenance
from stiracore.control.staging_review import StagingReview
from stiracore.core.amygdala import AmygdalaPass
from stiracore.tools.episodic_formatter import record_episode
from stiracore.tools.memory_store import MemoryLayer, MemoryStore

TURNS = 21


class _Engine:
    state = None
    last_breaker = None

    def identity_injection(self) -> str:
        return ""


def _record_turns(store: MemoryStore, count: int = TURNS) -> list[str]:
    """``count`` turns through the real per-turn path, one second apart (oldest first)."""
    base = time.time_ns() - (count + 10) * 1_000_000_000
    keys: list[str] = []
    for turn in range(count):
        key = f"chat-{base + turn * 1_000_000_000}"
        # Every third turn is long and structured so that some leave the discard band.
        user = (
            f"## Decision {turn}\n- keep the design small\n- verify first\n```py\nx = {turn}\n```\n"
            + "detail " * 80
            + "Decision: adopt it."
            if turn % 3 == 0
            else f"turn {turn}: quick question"
        )
        entry = record_episode(store, user, f"answer {turn}", key=key)
        assert entry is not None
        stamp = base + turn * 1_000_000_000
        os.utime(entry.path, ns=(stamp, stamp))
        keys.append(key)
    return keys


def _newest(store: MemoryStore, limit: int) -> list[str]:
    return [entry.key for entry in store.list_entries(MemoryLayer.EPISODIC, limit)]


def _mtimes(store: MemoryStore) -> dict[str, int]:
    directory = store.memory_dir / MemoryLayer.EPISODIC.value
    return {path.stem: path.stat().st_mtime_ns for path in directory.glob("*.md")}


def _cli(tmp_path: Path, *extra: str) -> int:
    config_path = tmp_path / "agent-config.json"
    if not config_path.exists():
        AgentConfig(path=config_path).save()
    return main(
        [
            "--base-dir",
            str(tmp_path / "memory"),
            "--agent-id",
            "stira",
            "--config",
            str(config_path),
            *extra,
        ]
    )


def test_issue_292_repro_cli_harvest_keeps_the_newest_episodes_first(tmp_path, capsys):
    store = MemoryStore(tmp_path / "memory", "stira")
    keys = _record_turns(store)
    newest_three = list(reversed(keys[-3:]))
    assert _newest(store, 3) == newest_three
    before = _mtimes(store)

    assert _cli(tmp_path) == 0
    capsys.readouterr()

    assert _newest(store, 3) == newest_three
    assert _mtimes(store) == before
    # The marks were written (the rewrite happened), only the order is unchanged.
    marked = store.list_entries(MemoryLayer.EPISODIC, TURNS)
    assert all(entry.metadata.get("harvested") is True for entry in marked)


def test_context_assembler_episodic_section_is_the_newest_five_after_harvest(tmp_path, capsys):
    store = MemoryStore(tmp_path / "memory", "stira")
    keys = _record_turns(store)
    assembler = ContextAssembler("stira", store, _Engine(), tmp_path / "soul.md")
    expected = "\n".join(f"- {_content(store, key)}" for key in reversed(keys[-5:]))
    assert assembler._memory_text("episodic", 5) == expected

    assert _cli(tmp_path) == 0
    capsys.readouterr()

    assert assembler._memory_text("episodic", 5) == expected


def _content(store: MemoryStore, key: str) -> str:
    entry = store._read_entry(MemoryLayer.EPISODIC, key)
    assert entry is not None
    return entry.content.strip()


class _VersionedDistiller:
    """Always routes to staging; a new ``version`` makes Harvest re-mark every episode."""

    def __init__(self, version: str) -> None:
        self.version = version

    def distill(self, content, metadata):
        return DistillationDecision(score=0.8, route="staging", reason="test")


def test_repeated_harvest_review_and_forgetting_undo_keep_the_order(tmp_path, capsys):
    store = MemoryStore(tmp_path / "memory", "stira")
    keys = _record_turns(store)
    expected = list(reversed(keys))
    before = _mtimes(store)

    # 1st sweep (rule distiller, via the CLI) and a re-run that skips everything.
    assert _cli(tmp_path) == 0
    assert _cli(tmp_path) == 0
    # A new distiller version re-marks every episode (new staged entries too).
    for version in ("v-a", "v-b"):
        HarvestScheduler(
            store, None, "stira", distiller=_VersionedDistiller(version)
        ).run_distillation()
        assert _newest(store, TURNS) == expected
    # Staging review: approve one, reject one, then harvest again (rejected route).
    review = StagingReview(store)
    staged = review.list_staged()
    assert len(staged) >= 2
    review.approve(staged[0].key)
    review.reject(staged[1].key, "test")
    HarvestScheduler(store, None, "stira", distiller=_VersionedDistiller("v-c")).run_distillation()
    # Forgetting apply + undo (#320) on a Semantic entry, then the CLI once more.
    semantic = next(
        entry
        for entry in store.list_entries(MemoryLayer.SEMANTIC, 100)
        if entry.key != "index" and not entry.key.startswith("staging/")
    )
    store.write(MemoryLayer.SEMANTIC, semantic.key, "x" * 5_000, semantic.metadata)
    with sqlite3.connect(store.index_path) as connection:
        connection.execute(
            "UPDATE memory_entries SET last_accessed = ? WHERE layer = ? AND key = ?",
            (time.time() - 100 * 86_400, MemoryLayer.SEMANTIC.value, semantic.key),
        )
    maintenance = MemoryMaintenance(store)
    applied = maintenance.run_forgetting(apply=True)
    assert applied.applied >= 1
    for item in applied.candidates:
        if item.archive_id:
            assert maintenance.undo_forgetting(item.archive_id).restored
    assert _cli(tmp_path) == 0
    capsys.readouterr()

    assert _newest(store, TURNS) == expected
    assert _newest(store, 5) == expected[:5]
    assert _mtimes(store) == before
    # The last CLI run re-marked every episode with the rule distiller's version.
    marks = [entry.metadata for entry in store.list_entries(MemoryLayer.EPISODIC, TURNS)]
    assert {metadata.get("harvest_version") for metadata in marks} == {
        RuleBasedDistiller(AmygdalaPass()).version
    }


def test_new_turn_after_harvest_is_the_newest(tmp_path, capsys):
    store = MemoryStore(tmp_path / "memory", "stira")
    _record_turns(store)
    assert _cli(tmp_path) == 0
    capsys.readouterr()

    entry = record_episode(store, "a later turn", "later answer", key=f"chat-{time.time_ns()}")

    assert entry is not None
    assert _newest(store, 1) == [entry.key]


def test_plain_write_still_updates_the_modification_time(tmp_path):
    store = MemoryStore(tmp_path / "memory", "stira")
    first = store.write(MemoryLayer.EPISODIC, "one", "first")
    old = first.path.stat().st_mtime_ns - 5_000_000_000
    os.utime(first.path, ns=(old, old))

    rewritten = store.write(MemoryLayer.EPISODIC, "one", "second")

    assert rewritten.path.stat().st_mtime_ns > old


def test_preserving_write_keeps_mtime_and_indexes_it(tmp_path):
    store = MemoryStore(tmp_path / "memory", "stira")
    first = store.write(MemoryLayer.EPISODIC, "one", "first", {"a": 1})
    old = first.path.stat().st_mtime_ns - 5_000_000_000
    os.utime(first.path, ns=(old, old))

    kept = store.write(
        MemoryLayer.EPISODIC, "one", "first", {"a": 1, "marked": True}, preserve_mtime=True
    )

    assert kept.path.stat().st_mtime_ns == old
    assert kept.metadata["marked"] is True
    assert store.search("marked", layers=[MemoryLayer.EPISODIC])[0].modified_at == kept.modified_at
    # A new entry has nothing to preserve: it gets the current time.
    fresh = store.write(MemoryLayer.EPISODIC, "two", "new", preserve_mtime=True)
    assert fresh.path.stat().st_mtime_ns > old


# --- preserve_mtime write path (review of #323) ---------------------------------

needs_fd_utime = pytest.mark.skipif(
    os.utime not in os.supports_fd, reason="descriptor-based utime (POSIX)"
)


def _aged_entry(store: MemoryStore, key: str = "one") -> tuple[Path, int]:
    entry = store.write(MemoryLayer.EPISODIC, key, "first", {"a": 1})
    old = entry.path.stat().st_mtime_ns - 50_000_000_000
    os.utime(entry.path, ns=(old, old))
    return entry.path, old


def _temporaries(store: MemoryStore) -> list[Path]:
    return sorted((store.memory_dir / MemoryLayer.EPISODIC.value).glob(".memory-*"))


@needs_fd_utime
def test_preserved_time_goes_to_the_descriptor_not_a_swapped_path(tmp_path, monkeypatch):
    store = MemoryStore(tmp_path / "memory", "stira")
    path, old = _aged_entry(store)
    victim = tmp_path / "victim.txt"
    victim.write_text("not a memory", encoding="utf-8")
    victim_time = victim.stat().st_mtime_ns
    real_utime = os.utime
    swapped: list[str] = []

    def swap_then_utime(target, *args, **kwargs):
        # Swap every temporary file's path for a link to another file, then set the time.
        (temporary,) = _temporaries(store)
        aside = temporary.with_name(temporary.name + ".aside")
        temporary.rename(aside)
        temporary.symlink_to(victim)
        try:
            real_utime(target, *args, **kwargs)
        finally:
            temporary.unlink()
            aside.rename(temporary)
        swapped.append(type(target).__name__)

    monkeypatch.setattr(os, "utime", swap_then_utime)

    kept = store.write(MemoryLayer.EPISODIC, "one", "first", {"a": 1, "m": 1}, preserve_mtime=True)

    assert swapped == ["int"]  # by descriptor
    assert victim.stat().st_mtime_ns == victim_time
    assert path.stat().st_mtime_ns == old and kept.metadata["m"] == 1
    assert not path.is_symlink() and _temporaries(store) == []


def test_utime_failure_replaces_nothing_and_removes_the_temporary(tmp_path, monkeypatch):
    store = MemoryStore(tmp_path / "memory", "stira")
    path, old = _aged_entry(store)
    original = store.read_text(MemoryLayer.EPISODIC, "one")
    real_replace = os.replace
    replaced: list[str] = []

    def failing_utime(*args, **kwargs):
        raise PermissionError("injected")

    def recording_replace(source, target, *args, **kwargs):
        replaced.append(str(target))
        return real_replace(source, target, *args, **kwargs)

    monkeypatch.setattr(os, "utime", failing_utime)
    monkeypatch.setattr(os, "replace", recording_replace)

    with pytest.raises(PermissionError):
        store.write(MemoryLayer.EPISODIC, "one", "first", {"a": 1, "m": 1}, preserve_mtime=True)

    assert replaced == []
    assert store.read_text(MemoryLayer.EPISODIC, "one") == original
    assert path.stat().st_mtime_ns == old
    assert _temporaries(store) == []


@needs_fd_utime
def test_preserving_write_order_is_utime_fsync_close_replace_dirsync(tmp_path, monkeypatch):
    store = MemoryStore(tmp_path / "memory", "stira")
    path, old = _aged_entry(store)
    events: list[str] = []
    real_utime, real_fsync, real_replace = os.utime, os.fsync, os.replace
    descriptor: list[int] = []

    def utime(target, *args, **kwargs):
        assert isinstance(target, int)
        descriptor.append(target)
        events.append("utime(fd)")
        return real_utime(target, *args, **kwargs)

    def fsync(fd):
        events.append("fsync(dir)" if stat.S_ISDIR(os.fstat(fd).st_mode) else "fsync(file)")
        return real_fsync(fd)

    def replace(source, target, *args, **kwargs):
        try:
            os.fstat(descriptor[0])
        except OSError:
            events.append("close")
        events.append("replace")
        return real_replace(source, target, *args, **kwargs)

    monkeypatch.setattr(os, "utime", utime)
    monkeypatch.setattr(os, "fsync", fsync)
    monkeypatch.setattr(os, "replace", replace)

    store.write(MemoryLayer.EPISODIC, "one", "first", {"a": 1, "m": 1}, preserve_mtime=True)

    expected = ["utime(fd)", "fsync(file)", "close", "replace"]
    if os.name == "posix":
        expected.append("fsync(dir)")
    assert events == expected
    assert path.stat().st_mtime_ns == old


def test_leftover_temporary_from_a_killed_write_is_not_an_entry(tmp_path, capsys):
    """What a hard kill between create and replace leaves behind (measured in #323)."""
    store = MemoryStore(tmp_path / "memory", "stira")
    _record_turns(store, 3)
    leftover = store.memory_dir / MemoryLayer.EPISODIC.value / ".memory-killed.tmp"
    leftover.write_text("---\nharvested: false\n---\nleftover-needle\n", encoding="utf-8")

    assert store.count_entries(MemoryLayer.EPISODIC) == 3
    assert ".memory-killed" not in {e.key for e in store.list_entries(MemoryLayer.EPISODIC, 10)}
    assert store.search("leftover-needle") == []
    assert _cli(tmp_path) == 0
    assert '"skipped": 0' in capsys.readouterr().out
    with sqlite3.connect(store.index_path) as connection:
        keys = [row[0] for row in connection.execute("SELECT key FROM memory_entries")]
    assert all("killed" not in key for key in keys)
    # It is not removed automatically either (remove it with the server stopped).
    assert leftover.exists()
