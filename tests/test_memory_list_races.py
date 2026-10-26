"""#291 (review of #289, Minor): the memory listing under races, and its specification.

Deterministic: each race is injected at an exact point (after the enumeration's
symlink check, after a read's path check), never with timing.

Specification pinned here:

- ``list_entries(layer, limit)`` orders from one ``stat`` pass (newest first, then
  key). An entry touched after that pass keeps its place in this result and moves at
  the *next* call; nothing is retried.
- A name that becomes a symlink (the file itself or a parent directory) after the
  enumeration is skipped by the listing and refused by a read; its target is never
  read.
- ``count_entries`` counts every regular ``*.md`` file of the layer without opening
  it; there is no upper bound (the old ``list_entries(layer, 9999)`` count stopped
  at 9999).
"""

from __future__ import annotations

import os
import time

import pytest

from stiracore.tools.memory_store import MemoryLayer, MemoryStore

pytestmark = pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")

EPISODIC = MemoryLayer.EPISODIC
OUTSIDE = "OUTSIDE-SECRET-291"


def _store(tmp_path, count=3):
    store = MemoryStore(tmp_path / "home", "stira")
    for index in range(count):
        store.write(EPISODIC, f"e{index}", f"entry {index}", {})
        stamp = time.time() - 100 + index
        os.utime(store._path(EPISODIC, f"e{index}"), (stamp, stamp))
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "e1.md").write_text(f"{OUTSIDE}\n", encoding="utf-8")
    return store, outside


def _swap_file_after_enumeration(store, monkeypatch, key, target):
    original = store._entry_keys

    def keys(layer):
        for name in original(layer):
            if name == key:  # checked as a regular file; now it becomes a symlink
                path = store._path(layer, name)
                path.unlink()
                path.symlink_to(target)
            yield name

    monkeypatch.setattr(store, "_entry_keys", keys)


def test_a_file_swapped_for_a_symlink_after_enumeration_is_skipped(tmp_path, monkeypatch):
    store, outside = _store(tmp_path)
    _swap_file_after_enumeration(store, monkeypatch, "e1", outside / "e1.md")
    entries = store.list_entries(EPISODIC, 5)
    assert [e.key for e in entries] == ["e2", "e0"]
    assert all(OUTSIDE not in e.content for e in entries)


def test_a_symlink_swapped_in_after_the_read_check_is_not_followed(tmp_path, monkeypatch):
    """Between ``_path``'s symlink check and the open: the final component."""
    store, outside = _store(tmp_path)
    original = store._path

    def path_then_swap(layer, key):
        path = original(layer, key)
        if key == "e1" and not path.is_symlink():
            path.unlink()
            path.symlink_to(outside / "e1.md")
        return path

    monkeypatch.setattr(store, "_path", path_then_swap)
    entries = store.list_entries(EPISODIC, 5)
    assert [e.key for e in entries] == ["e2", "e0"]
    with pytest.raises(ValueError, match="symbolic links"):
        store.read(EPISODIC, "e1")


def test_a_parent_directory_swapped_after_the_read_check_is_refused(tmp_path, monkeypatch):
    """Between the check and the open, ``episodic/sub`` becomes a symlink elsewhere."""
    store, outside = _store(tmp_path, count=0)
    store.write(EPISODIC, "sub/e1", "inside", {})
    store.write(EPISODIC, "top", "kept", {})
    original = store._path

    def path_then_swap_parent(layer, key):
        path = original(layer, key)
        parent = path.parent
        if key == "sub/e1" and not parent.is_symlink():
            parent.rename(parent.with_name("sub-moved"))
            parent.symlink_to(outside, target_is_directory=True)
        return path

    monkeypatch.setattr(store, "_path", path_then_swap_parent)
    entries = store.list_entries(EPISODIC, 5)
    assert [e.key for e in entries] == ["top"]
    assert all(OUTSIDE not in e.content for e in entries)


def test_an_update_after_the_stat_pass_shows_at_the_next_call(tmp_path, monkeypatch):
    store, _outside = _store(tmp_path, count=4)
    original = store._read_entry
    touched = {"done": False}

    def read_then_touch(layer, key):
        entry = original(layer, key)
        if not touched["done"]:
            touched["done"] = True
            # e0 (the oldest, outside the top 2) is rewritten while the listing runs.
            store.write(EPISODIC, "e0", "updated", {})
        return entry

    monkeypatch.setattr(store, "_read_entry", read_then_touch)
    first = store.list_entries(EPISODIC, 2)
    assert [e.key for e in first] == ["e3", "e2"], "this call: the stat snapshot"
    monkeypatch.setattr(store, "_read_entry", original)
    second = store.list_entries(EPISODIC, 2)
    assert second[0].key == "e0" and second[0].content == "updated"


def test_count_entries_counts_every_regular_entry_without_a_cap(tmp_path, monkeypatch):
    store, outside = _store(tmp_path, count=12)
    (store.memory_dir / EPISODIC.value / "link.md").symlink_to(outside / "e1.md")
    opened = []
    monkeypatch.setattr(store, "_read_entry", lambda *a: opened.append(a))
    assert store.count_entries(EPISODIC) == 12
    assert opened == [], "counting opens no file"
    # No 9999 cap: the count is the number of regular *.md files the layer holds.
    monkeypatch.setattr(store, "_entry_keys", lambda layer: iter(f"k{i}" for i in range(10_001)))
    assert store.count_entries(EPISODIC) == 10_001
