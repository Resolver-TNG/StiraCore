"""#296: a link *above* the memory root is the operator's choice; at or below it, refused.

``MemoryStore`` resolves the ancestors of its configured root once (macOS ``/tmp`` and
``/var`` are links; a home or project folder may be one). A link at the root itself,
at ``agents/<id>/memory``, at a layer directory, at ``index.sqlite3`` or at an entry
file is still refused, also when it is swapped in after the store was built. The
target of a refused link is never read or written.
"""

from __future__ import annotations

import os
import shutil

import pytest

from stiracore.tools import episodic_emotion
from stiracore.tools.memory_store import MemoryLayer, MemoryStore, MemorySymlinkError

pytestmark = pytest.mark.skipif(os.name == "nt", reason="POSIX symlinks")

EPISODIC = MemoryLayer.EPISODIC
SECRET = "OUTSIDE-SECRET-296"


@pytest.fixture
def outside(tmp_path):
    directory = tmp_path / "outside"
    directory.mkdir()
    (directory / "note.md").write_text(f"{SECRET}\n", encoding="utf-8")
    return directory


def _untouched(outside):
    assert sorted(p.name for p in outside.iterdir()) == ["note.md"]
    assert (outside / "note.md").read_text(encoding="utf-8") == f"{SECRET}\n"


def test_a_link_above_the_root_is_resolved_and_the_store_works(tmp_path):
    real = tmp_path / "real"
    (real / "data").mkdir(parents=True)
    (tmp_path / "link").symlink_to(real, target_is_directory=True)

    store = MemoryStore(tmp_path / "link" / "data" / "memory", "stira")
    store.write(EPISODIC, "e1", "hello", {})

    assert store.base_dir == (real / "data" / "memory").resolve()
    assert store.read(EPISODIC, "e1").content == "hello"
    assert [e.key for e in store.list_entries(EPISODIC, 5)] == ["e1"]
    assert (real / "data" / "memory" / "agents" / "stira" / "memory" / "episodic").is_dir()


def test_a_link_at_the_root_is_refused(tmp_path, outside):
    (tmp_path / "memory").symlink_to(outside, target_is_directory=True)
    with pytest.raises(MemorySymlinkError):
        MemoryStore(tmp_path / "memory", "stira")
    _untouched(outside)


def test_a_link_at_the_root_below_a_linked_ancestor_is_refused(tmp_path, outside):
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "link").symlink_to(real, target_is_directory=True)
    (real / "memory").symlink_to(outside, target_is_directory=True)
    with pytest.raises(MemorySymlinkError):
        MemoryStore(tmp_path / "link" / "memory", "stira")
    _untouched(outside)


@pytest.mark.parametrize(
    "relative",
    ["agents", "agents/stira", "agents/stira/memory", "agents/stira/memory/episodic"],
)
def test_a_link_below_the_root_is_refused_at_construction(tmp_path, outside, relative):
    root = tmp_path / "memory"
    link = root / relative
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(outside, target_is_directory=True)
    with pytest.raises(MemorySymlinkError):
        MemoryStore(root, "stira")
    _untouched(outside)


def test_a_linked_index_is_refused_at_construction(tmp_path, outside):
    memory = tmp_path / "memory" / "agents" / "stira" / "memory"
    memory.mkdir(parents=True)
    (memory / "index.sqlite3").symlink_to(outside / "index.sqlite3")
    with pytest.raises(MemorySymlinkError):
        MemoryStore(tmp_path / "memory", "stira")
    _untouched(outside)


def _built(tmp_path):
    store = MemoryStore(tmp_path / "memory", "stira")
    store.write(EPISODIC, "e1", "inside", {})
    return store


def _swap_for_link(path, target):
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()
    path.symlink_to(target, target_is_directory=target.is_dir())


@pytest.mark.parametrize(
    "relative",
    ["", "agents/stira/memory", "agents/stira/memory/episodic"],
    ids=["root", "agent-memory", "layer"],
)
def test_a_directory_swapped_for_a_link_after_construction_is_refused(tmp_path, outside, relative):
    store = _built(tmp_path)
    (outside / "e1.md").write_text(f"{SECRET}\n", encoding="utf-8")
    directory = store.base_dir / relative if relative else store.base_dir
    copy = tmp_path / "copy"
    shutil.copytree(directory, copy)
    _swap_for_link(directory, copy if relative != "agents/stira/memory/episodic" else outside)

    with pytest.raises(MemorySymlinkError):
        store.read(EPISODIC, "e1")
    with pytest.raises(MemorySymlinkError):
        store.write(EPISODIC, "e2", "new", {})
    assert sorted(p.name for p in outside.iterdir()) == ["e1.md", "note.md"]
    assert not list(copy.rglob("e2.md"))


def test_an_index_swapped_for_a_link_after_construction_is_refused(tmp_path, outside):
    store = _built(tmp_path)
    _swap_for_link(store.index_path, outside / "note.md")
    with pytest.raises(MemorySymlinkError):
        store.read(EPISODIC, "e1")
    _untouched(outside)


def test_an_entry_swapped_for_a_link_after_construction_is_refused(tmp_path, outside):
    store = _built(tmp_path)
    _swap_for_link(store._path(EPISODIC, "e1"), outside / "note.md")
    with pytest.raises(MemorySymlinkError):
        store.read(EPISODIC, "e1")
    assert [e.key for e in store.list_entries(EPISODIC, 5)] == []
    _untouched(outside)


def test_a_linked_emotion_directory_is_still_refused(tmp_path, outside):
    store = _built(tmp_path)
    (store.memory_dir / episodic_emotion.EMOTION_DIRNAME).symlink_to(
        outside, target_is_directory=True
    )
    with pytest.raises(MemorySymlinkError):
        episodic_emotion._private_directory(store)
    with pytest.raises(MemorySymlinkError):
        episodic_emotion._read_record(
            episodic_emotion.emotion_record_path(store, "e1"), episodic_emotion._store_root(store)
        )
    _untouched(outside)


def test_a_path_outside_the_root_is_checked_from_the_filesystem_root(tmp_path, outside):
    base = tmp_path.resolve()  # a real path, so only the planted link can refuse
    root = base / "root"
    root.mkdir()
    (base / "link").symlink_to(outside, target_is_directory=True)
    MemoryStore._reject_symlinks(base / "root" / "x", root=root)
    with pytest.raises(MemorySymlinkError):
        MemoryStore._reject_symlinks(base / "link" / "note.md", root=root)
    with pytest.raises(MemorySymlinkError):  # ".." never shortens the walk
        MemoryStore._reject_symlinks(root / ".." / "link" / "note.md", root=root)
