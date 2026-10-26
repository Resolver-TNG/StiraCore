"""Integration bug hunt 2026-10-15: listing/counting memory does not read every entry.

stira_server's refresh_status counted the three layers with
``len(list_entries(layer, limit=9999))`` and the context assembler asked for the
newest few entries, and both read + YAML-parsed every file of the layer. With the
heartbeat (#238) refresh_status also runs on every tick, so the cost of each turn
and tick grew linearly with the episodic layer (measured: ~0.1 s per turn at 60
episodes, ~0.26 s at 210; 95 % of the time in yaml.safe_load).
"""

from __future__ import annotations

import os

from stiracore import MemoryLayer, MemoryStore


def _store(tmp_path, count=40):
    store = MemoryStore(tmp_path / "memory", "stira")
    for index in range(count):
        entry = store.write(MemoryLayer.EPISODIC, f"day/{index:03d}", f"episode {index}")
        # Distinct, out-of-key-order mtimes plus one tie, to pin the ordering.
        stamp = 1_700_000_000 + (index * 7919) % count
        os.utime(entry.path, (stamp, stamp))
    return store


def _reference(store, layer, limit):
    """The previous implementation: read everything, sort, slice."""
    entries = sorted(
        store._entries(layer), key=lambda entry: (-entry.modified_at.timestamp(), entry.key)
    )
    return [entry.key for entry in entries[:limit]]


def test_list_entries_reads_only_the_newest_limit(tmp_path, monkeypatch):
    store = _store(tmp_path)
    expected = _reference(store, MemoryLayer.EPISODIC, 5)
    reads = []
    original = store._read_entry
    monkeypatch.setattr(
        store, "_read_entry", lambda layer, key: reads.append(key) or original(layer, key)
    )
    listed = store.list_entries(MemoryLayer.EPISODIC, limit=5)
    assert [entry.key for entry in listed] == expected
    assert len(reads) == 5


def test_list_entries_order_matches_the_full_sort(tmp_path):
    store = _store(tmp_path, count=25)
    for limit in (1, 3, 25, 100):
        listed = [entry.key for entry in store.list_entries(MemoryLayer.EPISODIC, limit)]
        assert listed == _reference(store, MemoryLayer.EPISODIC, limit), limit
    assert store.list_entries(MemoryLayer.EPISODIC, 0) == []


def test_count_entries_opens_no_file(tmp_path, monkeypatch):
    store = _store(tmp_path, count=12)
    monkeypatch.setattr(
        store, "_read_entry", lambda *_a: (_ for _ in ()).throw(AssertionError("read"))
    )
    assert store.count_entries(MemoryLayer.EPISODIC) == 12
    assert store.count_entries(MemoryLayer.SEMANTIC) == 0


def test_a_symlinked_entry_is_skipped_and_a_deleted_one_tolerated(tmp_path):
    store = _store(tmp_path, count=3)
    directory = store.memory_dir / MemoryLayer.EPISODIC.value
    outside = tmp_path / "outside.md"
    outside.write_text("---\n{}\n---\nnot mine", encoding="utf-8")
    (directory / "link.md").symlink_to(outside)
    assert store.count_entries(MemoryLayer.EPISODIC) == 3
    assert "link" not in [e.key for e in store.list_entries(MemoryLayer.EPISODIC, 10)]


def test_stira_status_counts_without_reading_entries(tmp_path, monkeypatch):
    from dashboard import stira_server
    from stiracore.observability.config_api import ConfigStore

    reads = []
    original = MemoryStore._read_entry

    def counting(self, layer, key):
        reads.append(key)
        return original(self, layer, key)

    class _Agent:
        def __init__(self, *, tools=(), state=None, config=None, **_kwargs):
            from stiracore.core import StiraEngine

            self.engine = StiraEngine(state=state, config=config)
            self.tools = tuple(tools)
            self.prompt_cache = self.context_management = None

    data_dir = tmp_path / "stira"
    seed = MemoryStore(data_dir / "memory", "stira")
    for index in range(30):
        seed.write(MemoryLayer.EPISODIC, f"e{index}", "x")
    store = ConfigStore(
        data_dir / "gateway-config.json",
        data_dir / "agent-config.json",
        data_dir / "tool-config.json",
    )
    monkeypatch.setattr(stira_server, "DATA_DIR", data_dir)
    monkeypatch.setattr(stira_server, "NXPAgent", _Agent)
    monkeypatch.setattr(stira_server, "_wire_system_prompt", lambda *a, **k: None)
    monkeypatch.setattr(MemoryStore, "_read_entry", counting)
    app = stira_server.create_app(store)
    assert reads == []  # start-up refresh_status counted 30 entries without reading one
    memory_tree = app.test_client().get("/api/memory/tree").get_json()
    assert len(memory_tree["episodic"]) == 20
    assert len(reads) == 20


# --- No cache: every call re-reads the directory, so outside changes are seen ----------


def test_an_outside_write_is_seen_by_the_next_call(tmp_path):
    """Another MemoryStore (another process) or a hand-written file: no stale view."""
    store = _store(tmp_path, count=5)
    assert store.count_entries(MemoryLayer.EPISODIC) == 5
    other = MemoryStore(tmp_path / "memory", "stira")
    other.write(MemoryLayer.EPISODIC, "from-elsewhere", "written outside this store")
    (store.memory_dir / "episodic" / "by-hand.md").write_text("---\n{}\n---\nhand", "utf-8")
    assert store.count_entries(MemoryLayer.EPISODIC) == 7
    newest = [e.key for e in store.list_entries(MemoryLayer.EPISODIC, 2)]
    assert set(newest) == {"from-elsewhere", "by-hand"}


def test_an_updated_entry_moves_to_the_front_with_its_new_content(tmp_path):
    store = _store(tmp_path, count=6)
    oldest = _reference(store, MemoryLayer.EPISODIC, 6)[-1]
    path = store.memory_dir / "episodic" / f"{oldest}.md"
    path.write_text("---\n{}\n---\nrewritten", encoding="utf-8")
    os.utime(path, (1_800_000_000, 1_800_000_000))
    first = store.list_entries(MemoryLayer.EPISODIC, 1)[0]
    assert (first.key, first.content) == (oldest, "rewritten")


def test_a_deleted_entry_is_skipped_without_losing_the_limit(tmp_path):
    store = _store(tmp_path, count=8)
    expected = _reference(store, MemoryLayer.EPISODIC, 4)
    original = store._read_entry

    def vanishing(layer, key):
        if key == expected[0]:
            (store.memory_dir / "episodic" / f"{key}.md").unlink()  # deleted after stat
        return original(layer, key)

    store._read_entry = vanishing
    listed = [e.key for e in store.list_entries(MemoryLayer.EPISODIC, 3)]
    assert listed == expected[1:4]


def test_listing_while_another_thread_writes_never_fails(tmp_path):
    import threading

    store = _store(tmp_path, count=10)
    writer = MemoryStore(tmp_path / "memory", "stira")
    stop = threading.Event()
    errors: list[BaseException] = []

    def write_loop():
        index = 0
        while not stop.is_set():
            writer.write(MemoryLayer.EPISODIC, f"live/{index % 7}", f"v{index}")
            index += 1

    thread = threading.Thread(target=write_loop)
    thread.start()
    try:
        for _ in range(200):
            try:
                listed = store.list_entries(MemoryLayer.EPISODIC, 5)
                assert len(listed) == 5
                stamps = [(-e.modified_at.timestamp(), e.key) for e in listed]
                assert stamps == sorted(stamps)
                assert store.count_entries(MemoryLayer.EPISODIC) >= 10
            except BaseException as error:  # noqa: BLE001 - collected for the assert
                errors.append(error)
    finally:
        stop.set()
        thread.join()
    assert not errors, errors[:3]
