"""Follow-up guards for importance tags (#173), on top of ``test_memory_importance.py``.

* ``touch_recalled`` — the reinforcement hook for associative recall (#170, not wired):
  it restarts a Semantic entry's half-life clock, never moves it backwards, never changes
  the file or the tag, and creates nothing for an unknown key.
* Backward compatibility: an untagged entry and a tagged one round-trip through the store
  byte for byte; a tag survives a forgetting step and its undo.
* The agent's memory tools cannot change an existing tag, and none of them is a
  tag/touch tool.
* Forgetting still goes through the archive and the audit log first (a step with no
  ``forget.started`` line or no archive record cannot overwrite the entry).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from stiracore.control.forgetting_archive import ARCHIVE_DIRNAME, AUDIT_LOG_NAME
from stiracore.control.memory_cli import main as memory_main
from stiracore.control.memory_maintenance import MemoryMaintenance
from stiracore.memory_importance import (
    DERIVED,
    INVARIANT,
    REFERENCE_HALF_LIFE_DAYS,
    Retention,
    retention_of,
    touch_recalled,
)
from stiracore.tools.memory_store import MemoryLayer, MemoryStore

NOW = datetime(2026, 10, 18, tzinfo=UTC)


def _data(tmp_path: Path) -> tuple[Path, MemoryStore]:
    data = (tmp_path / "stira").resolve()
    data.mkdir()
    return data, MemoryStore(data / "memory", "stira")


def _row(store: MemoryStore, layer: str, key: str):
    with sqlite3.connect(store.index_path) as connection:
        return connection.execute(
            "SELECT search_text, modified_at, last_accessed FROM memory_entries"
            " WHERE layer = ? AND key = ?",
            (layer, key),
        ).fetchone()


def _set_access(store: MemoryStore, layer: str, key: str, when: datetime) -> None:
    with sqlite3.connect(store.index_path) as connection:
        connection.execute(
            "UPDATE memory_entries SET last_accessed = ? WHERE layer = ? AND key = ?",
            (when.timestamp(), layer, key),
        )


def _semantic(store, key, days, metadata=None, body=None):
    store.write(MemoryLayer.SEMANTIC, key, body or f"fact {key} " + "z" * 5_000, metadata or {})
    _set_access(store, "semantic", key, NOW - timedelta(days=days))


def _selected(store) -> dict[str, str]:
    result = MemoryMaintenance(store).run_forgetting(now=NOW, apply=False)
    return {item.key: item.stage for item in result.candidates}


def _stale(store) -> set[str]:
    return {c.key for c in MemoryMaintenance(store).find_deletion_candidates(now=NOW)}


# -- touch_recalled ------------------------------------------------------------------------


def test_touch_recalled_restarts_the_half_life_clock(tmp_path):
    _, store = _data(tmp_path)
    _semantic(store, "facts/old", 400)
    _semantic(store, "facts/fast", 45, {"retention_half_life_days": 10})
    assert _selected(store) == {"facts/old": "archive", "facts/fast": "compact"}
    assert _stale(store) == {"facts/old"}

    assert touch_recalled(store, "facts/old", at=NOW) is True
    assert touch_recalled(store, "facts/fast", at=NOW - timedelta(days=5)) is True
    preview = MemoryMaintenance(store).run_forgetting(now=NOW, apply=False)
    assert [c.key for c in preview.candidates] == []
    assert _stale(store) == set()
    # Five days on a half-life of 10 is not yet one half-life: no stage.
    assert _row(store, "semantic", "facts/fast")[2] == (NOW - timedelta(days=5)).timestamp()


def test_touch_recalled_defaults_to_now(tmp_path):
    _, store = _data(tmp_path)
    _semantic(store, "facts/x", 400)
    before = datetime.now(UTC).timestamp()
    assert touch_recalled(store, "facts/x") is True
    assert _row(store, "semantic", "facts/x")[2] >= before


def test_touch_recalled_never_moves_the_clock_backwards(tmp_path):
    """A stale or replayed time cannot make an entry fade faster."""
    _, store = _data(tmp_path)
    _semantic(store, "facts/recent", 10)
    recent = _row(store, "semantic", "facts/recent")[2]
    assert touch_recalled(store, "facts/recent", at=NOW - timedelta(days=400)) is True
    assert _row(store, "semantic", "facts/recent")[2] == recent
    assert _selected(store) == {}
    assert _stale(store) == set()


def test_touch_recalled_changes_no_bytes_no_mtime_and_not_the_tag(tmp_path):
    _, store = _data(tmp_path)
    tag = {"retention": DERIVED, "retention_half_life_days": 90, "tags": ["t"]}
    _semantic(store, "facts/tagged", 400, dict(tag))
    _semantic(store, "facts/keep", 400, {"retention": INVARIANT})
    path = store._path(MemoryLayer.SEMANTIC, "facts/tagged")
    keep = store._path(MemoryLayer.SEMANTIC, "facts/keep")
    files = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in (path, keep)}
    search_text, modified_at, _ = _row(store, "semantic", "facts/tagged")

    assert touch_recalled(store, "facts/tagged", at=NOW) is True
    assert touch_recalled(store, "facts/keep", at=NOW) is True
    assert {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in (path, keep)} == files
    assert _row(store, "semantic", "facts/tagged")[:2] == (search_text, modified_at)
    entry = store.read_text(MemoryLayer.SEMANTIC, "facts/tagged")
    assert retention_of(store._parse(entry)[1]) == Retention(DERIVED, 90, tagged=True)
    assert retention_of(store._parse(keep.read_text())[1]).invariant


def test_touch_recalled_creates_nothing_and_touches_only_semantic(tmp_path):
    _, store = _data(tmp_path)
    store.write(MemoryLayer.EPISODIC, "facts/x", "an episode with the same key")
    _set_access(store, "episodic", "facts/x", NOW - timedelta(days=100))
    episodic = _row(store, "episodic", "facts/x")
    with sqlite3.connect(store.index_path) as connection:
        rows = connection.execute("SELECT COUNT(*) FROM memory_entries").fetchone()[0]

    assert touch_recalled(store, "facts/x", at=NOW) is False  # no Semantic entry
    assert touch_recalled(store, "facts/missing", at=NOW) is False
    with sqlite3.connect(store.index_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM memory_entries").fetchone()[0] == rows
    assert _row(store, "episodic", "facts/x") == episodic


@pytest.mark.parametrize("key", ["", "../x", "a//b", "a\\b", "a/./b", 7])
def test_touch_recalled_rejects_a_bad_key(tmp_path, key):
    _, store = _data(tmp_path)
    with pytest.raises(ValueError):
        touch_recalled(store, key, at=NOW)


def test_touch_recalled_rejects_a_naive_time(tmp_path):
    _, store = _data(tmp_path)
    _semantic(store, "facts/x", 1)
    with pytest.raises(ValueError):
        touch_recalled(store, "facts/x", at=NOW.replace(tzinfo=None))


# -- backward compatibility and round trips -------------------------------------------------


def test_an_untagged_entry_round_trips_byte_for_byte(tmp_path):
    _, store = _data(tmp_path)
    metadata = {"title": "t", "tags": ["a", "b"], "source_id": "chat-1"}
    store.write(MemoryLayer.SEMANTIC, "facts/plain", "body\nline 2\n", dict(metadata))
    path = store._path(MemoryLayer.SEMANTIC, "facts/plain")
    original = path.read_bytes()
    assert b"retention" not in original

    entry = store.read(MemoryLayer.SEMANTIC, "facts/plain")
    assert entry.metadata == metadata
    assert retention_of(entry.metadata) == Retention()  # untagged = derived, 30 days
    assert Retention().half_life_days == REFERENCE_HALF_LIFE_DAYS == 30
    assert store.render(entry.content, entry.metadata).encode() == original
    store.write(MemoryLayer.SEMANTIC, "facts/plain", entry.content, entry.metadata)
    assert path.read_bytes() == original


@pytest.mark.parametrize(
    ("tag", "expected"),
    [
        ({"retention": INVARIANT}, Retention(INVARIANT, tagged=True)),
        ({"retention": DERIVED}, Retention(DERIVED, tagged=True)),
        (
            {"retention": DERIVED, "retention_half_life_days": 90},
            Retention(DERIVED, 90, tagged=True),
        ),
        ({"retention_half_life_days": 7}, Retention(DERIVED, 7, tagged=True)),
    ],
)
def test_a_tag_round_trips_through_the_store(tmp_path, tag, expected):
    _, store = _data(tmp_path)
    store.write(MemoryLayer.SEMANTIC, "facts/t", "body\n", {"title": "t", **tag})
    path = store._path(MemoryLayer.SEMANTIC, "facts/t")
    original = path.read_bytes()

    entry = store.read(MemoryLayer.SEMANTIC, "facts/t")
    assert {k: entry.metadata[k] for k in tag} == tag
    assert retention_of(entry.metadata) == expected
    store.write(MemoryLayer.SEMANTIC, "facts/t", entry.content, entry.metadata)
    assert path.read_bytes() == original


def test_clearing_an_absent_tag_leaves_the_bytes_alone(tmp_path, capsys):
    data, store = _data(tmp_path)
    _semantic(store, "facts/plain", 3, {"title": "t"})
    path = store._path(MemoryLayer.SEMANTIC, "facts/plain")
    original = path.read_bytes()
    args = ["tag", "--data-dir", str(data), "--id", "semantic/facts/plain"]
    assert memory_main([*args, "--retention", "none", "--apply"]) == 0
    capsys.readouterr()
    assert path.read_bytes() == original


def test_a_tag_survives_a_forgetting_step_and_its_undo(tmp_path):
    _, store = _data(tmp_path)
    _semantic(store, "facts/fast", 45, {"retention_half_life_days": 10, "title": "f"})
    original = store.read_text(MemoryLayer.SEMANTIC, "facts/fast")

    applied = MemoryMaintenance(store).run_forgetting(now=NOW, apply=True)
    assert applied.applied == 1
    compressed = store.read(MemoryLayer.SEMANTIC, "facts/fast")
    assert compressed.metadata["forgetting_stage"] == "compact"
    assert retention_of(compressed.metadata) == Retention(DERIVED, 10, tagged=True)

    undone = MemoryMaintenance(store).undo_forgetting(applied.candidates[0].archive_id)
    assert undone.restored is True
    assert store.read_text(MemoryLayer.SEMANTIC, "facts/fast") == original


# -- forgetting goes through the archive and the audit log first ----------------------------


def test_a_forgetting_step_is_archived_and_logged_before_the_overwrite(tmp_path):
    _, store = _data(tmp_path)
    _semantic(store, "facts/fade", 200)
    _semantic(store, "facts/keep", 400, {"retention": INVARIANT})
    original = store.read_text(MemoryLayer.SEMANTIC, "facts/fade")
    seen: list[tuple[str, bool]] = []
    maintenance = MemoryMaintenance(store)
    audit = maintenance.archive.audit

    def spy(kind, **body):
        current = store.read_text(MemoryLayer.SEMANTIC, "facts/fade")
        seen.append((kind, current == original))
        if kind == "forget.started":
            # The archive record holds the original before anything is overwritten.
            record = maintenance.archive.load(body["id"])
            assert record.original_text == original
        return audit(kind, **body)

    maintenance.archive.audit = spy
    result = maintenance.run_forgetting(now=NOW, apply=True)
    assert result.applied == 1
    assert [c.key for c in result.candidates] == ["facts/fade"]
    assert ("forget.started", True) in seen  # logged while the entry was still intact
    assert ("forget.applied", False) in seen
    assert seen.index(("forget.started", True)) < seen.index(("forget.applied", False))

    log = store.memory_dir / ARCHIVE_DIRNAME / AUDIT_LOG_NAME
    kinds = [json.loads(line)["kind"] for line in log.read_text().splitlines()]
    assert kinds.count("forget.started") == kinds.count("forget.applied") == 1
    assert store.read_text(MemoryLayer.SEMANTIC, "facts/keep") is not None


def test_no_overwrite_when_the_audit_log_cannot_be_written(tmp_path):
    from stiracore.control.forgetting_archive import ForgettingArchiveError

    _, store = _data(tmp_path)
    _semantic(store, "facts/fade", 200)
    original = store.read_text(MemoryLayer.SEMANTIC, "facts/fade")
    maintenance = MemoryMaintenance(store)
    audit = maintenance.archive.audit

    def refuse(kind, **body):
        if kind == "forget.started":
            raise ForgettingArchiveError("audit log not writable")
        return audit(kind, **body)

    maintenance.archive.audit = refuse
    result = maintenance.run_forgetting(now=NOW, apply=True)
    assert result.applied == 0 and result.refused == 1
    assert store.read_text(MemoryLayer.SEMANTIC, "facts/fade") == original
    assert maintenance.archive.ids() == []  # the spare copy was dropped


# -- the agent's memory tools ---------------------------------------------------------------


def test_the_agent_memory_tools_cannot_change_an_existing_tag(tmp_path):
    from stiracore.tools.memory_tools import create_memory_tools

    _, store = _data(tmp_path)
    _semantic(store, "facts/keep", 400, {"retention": INVARIANT})
    _semantic(store, "facts/slow", 10, {"retention_half_life_days": 365})
    paths = [store._path(MemoryLayer.SEMANTIC, k) for k in ("facts/keep", "facts/slow")]
    before = [p.read_bytes() for p in paths]
    tools = {t.tool_name: t for t in create_memory_tools(store)}

    # No tool of the agent tags, untags or touches.
    for name in tools:
        assert not any(word in name for word in ("tag", "retention", "touch", "recall"))

    lifted = "---\nretention: derived\nretention_half_life_days: 1\n---\nshort-lived now"
    for layer in ("semantic", "procedural", "SEMANTIC", "semantic/../semantic"):
        for key in ("facts/keep", "facts/slow"):
            result = str(tools["memory_write"](layer=layer, key=key, content=lifted))
            assert "failed" in result.lower() or "denied" in result.lower(), result
    tools["memory_read"](layer="semantic", key="facts/keep")
    tools["memory_search"](query="fact", layer="all")
    tools["memory_list"](layer="semantic")

    assert [p.read_bytes() for p in paths] == before
    assert retention_of(store.read(MemoryLayer.SEMANTIC, "facts/keep").metadata).invariant
    slow = retention_of(store.read(MemoryLayer.SEMANTIC, "facts/slow").metadata)
    assert slow == Retention(DERIVED, 365, tagged=True)
