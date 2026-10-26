"""#574: the memory tree and ``memory_list`` say when the newest-20 cut-off hides entries.

``/api/memory/tree`` returns the newest 20 entries per layer (the read cap of #289). It did not
say how many exist, so the memory page showed "(20)" for an episodic layer of 80 and the layer
looked stuck. The cap stays; the response now carries the total of each layer, and
``memory_list`` names the total when its result was cut.
"""

from __future__ import annotations

import pytest

from stiracore.tools.memory_store import MemoryLayer, MemoryStore
from stiracore.tools.memory_tools import create_memory_tools
from tests.test_new_session import make_stira


@pytest.fixture
def stira(tmp_path, monkeypatch):
    yield from make_stira(tmp_path, monkeypatch)


def _seed(stira, layer, count, prefix="e"):
    store = MemoryStore(stira["data"] / "memory", "stira")
    for index in range(count):
        store.write(layer, f"{prefix}{index:03d}", f"entry {index}")
    return store


def test_tree_reports_the_total_when_the_list_is_cut(stira):
    _seed(stira, MemoryLayer.EPISODIC, 30)
    tree = stira["client"].get("/api/memory/tree").get_json()
    assert len(tree["episodic"]) == 20  # the read cap is unchanged
    assert tree["totals"]["episodic"] == 30
    assert tree["limit"] == 20


def test_tree_total_equals_the_list_below_the_cap(stira):
    _seed(stira, MemoryLayer.SEMANTIC, 3, prefix="s")
    tree = stira["client"].get("/api/memory/tree").get_json()
    assert len(tree["semantic"]) == 3
    assert tree["totals"] == {"episodic": 0, "semantic": 3, "procedural": 0}


def test_procedural_total_leaves_out_staged_proposals(stira):
    store = MemoryStore(stira["data"] / "memory", "stira")
    stira["app"].extensions["procedure_proposals"].propose(
        "safe_edit", "Read the file before and after editing.", "seen twice", source_turn="t-1"
    )
    assert store.count_entries(MemoryLayer.PROCEDURAL) >= 1  # the proposal is a file on disk
    tree = stira["client"].get("/api/memory/tree").get_json()
    assert tree["procedural"] == []
    assert tree["totals"]["procedural"] == 0


def test_count_entries_can_leave_out_a_key_prefix(tmp_path):
    store = MemoryStore(tmp_path / "memory", "stira")
    store.write(MemoryLayer.EPISODIC, "keep/a", "a")
    store.write(MemoryLayer.EPISODIC, "skip/b", "b")
    assert store.count_entries(MemoryLayer.EPISODIC) == 2
    assert store.count_entries(MemoryLayer.EPISODIC, exclude_prefix="skip/") == 1


def _memory_list(store):
    return next(tool for tool in create_memory_tools(store) if tool.tool_name == "memory_list")


def test_memory_list_names_the_total_when_it_cuts(tmp_path):
    store = MemoryStore(tmp_path / "memory", "stira")
    for index in range(25):
        store.write(MemoryLayer.EPISODIC, f"e{index:03d}", "x")
    text = _memory_list(store)(layer="episodic", limit=10)
    assert text.count("- episodic/") == 10
    assert "10 of 25" in text and "limit" in text


def test_memory_list_is_unchanged_when_nothing_was_cut(tmp_path):
    store = MemoryStore(tmp_path / "memory", "stira")
    for index in range(4):
        store.write(MemoryLayer.EPISODIC, f"e{index:03d}", "x")
    text = _memory_list(store)(layer="episodic", limit=20)
    assert text.count("- episodic/") == 4
    assert " of " not in text
