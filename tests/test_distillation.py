"""End-to-end invariants for episodic-to-semantic distillation."""

import hashlib

from stiracore.control.distillers import RuleBasedDistiller
from stiracore.control.harvest import HarvestScheduler
from stiracore.core.amygdala import AmygdalaPass
from stiracore.memory_origin import ORIGIN_EXTRACTED, record_origin
from stiracore.tools.memory_store import MemoryLayer, MemoryStore


def test_harvest_integration_list_score_route_write_and_relist(tmp_path):
    """Freeze the real list_entries -> score -> route -> write contract."""
    store = MemoryStore(tmp_path, "stira")
    store.write(
        MemoryLayer.EPISODIC,
        "decision-1",
        "A durable architecture decision.",
        {"importance": 0.7, "tags": ["architecture"]},
    )
    record_origin(
        store, MemoryLayer.EPISODIC, "decision-1", ORIGIN_EXTRACTED, "web-chat"
    )  # server-written (#506)

    result = HarvestScheduler(store, AmygdalaPass(), "stira").run_distillation()

    assert result.semantic == 1
    semantic = store.list_entries(MemoryLayer.SEMANTIC, limit=20)
    promoted = next(entry for entry in semantic if entry.key != "index")
    assert promoted.content == "A durable architecture decision."
    assert promoted.metadata["score"] == 0.7
    assert promoted.metadata["source_id"] == "decision-1"
    source = store.read(MemoryLayer.EPISODIC, "decision-1")
    assert source is not None
    assert source.metadata["harvested"] is True
    assert source.content == "A durable architecture decision."


def test_harvest_novelty_search_skips_duplicate_without_deleting_source(tmp_path):
    store = MemoryStore(tmp_path, "stira")
    content = "A reusable confirmed decision about memory architecture."
    store.write(MemoryLayer.EPISODIC, "first", content, {"importance": 0.7})
    scheduler = HarvestScheduler(store, AmygdalaPass(), "stira")
    assert scheduler.run_distillation().semantic == 1

    store.write(MemoryLayer.EPISODIC, "duplicate", content, {"importance": 0.7})
    result = scheduler.run_distillation()

    assert result.skipped == 2  # the first source is harvested; the second is duplicate
    promoted = [
        entry
        for entry in store.list_entries(MemoryLayer.SEMANTIC, limit=20)
        if entry.key != "index"
    ]
    assert len(promoted) == 1
    duplicate = store.read(MemoryLayer.EPISODIC, "duplicate")
    assert duplicate is not None
    assert duplicate.metadata["harvest_route"] == "duplicate"


def test_mid_score_writes_domain_knowledge_and_searchable_index(tmp_path):
    store = MemoryStore(tmp_path, "stira")
    store.write(
        MemoryLayer.EPISODIC,
        "knowledge-1",
        "A reusable implementation note.",
        {"importance": 0.4, "tags": ["Architecture"]},
    )
    record_origin(
        store, MemoryLayer.EPISODIC, "knowledge-1", ORIGIN_EXTRACTED, "web-chat"
    )  # server-written (#506)

    # stage_auto=False: the direct auto-promotion path (default stages it, #293).
    result = HarvestScheduler(store, AmygdalaPass(), "stira", stage_auto=False).run_distillation()

    assert result.knowledge == 1
    source_digest = hashlib.sha256(b"knowledge-1").hexdigest()[:16]
    promoted_key = f"knowledge/architecture/harvest-knowledge-1-{source_digest}"
    promoted = store.read(MemoryLayer.SEMANTIC, promoted_key)
    assert promoted is not None
    index = store.read(MemoryLayer.SEMANTIC, "index")
    assert index is not None
    assert "| title | tags | score | source_id | path |" in index.content
    assert f"semantic/{promoted_key}.md" in index.content
    assert store.search("knowledge-1", layers=[MemoryLayer.SEMANTIC])


def test_hierarchical_key_rejects_traversal_and_backslashes(tmp_path):
    import pytest

    store = MemoryStore(tmp_path, "stira")
    for key in ("../escape", "knowledge/../escape", r"knowledge\escape", "/absolute"):
        with pytest.raises(ValueError):
            store.write(MemoryLayer.SEMANTIC, key, "blocked")


def test_read_and_search_hits_update_last_accessed(tmp_path, monkeypatch):
    import stiracore.tools.memory_store as memory_store_module

    store = MemoryStore(tmp_path, "stira")
    written = store.write(MemoryLayer.SEMANTIC, "accessed", "Needle memory")
    first = written.last_accessed.timestamp() + 100
    monkeypatch.setattr(memory_store_module.time, "time", lambda: first)
    read_entry = store.read(MemoryLayer.SEMANTIC, "accessed")
    assert read_entry is not None
    assert read_entry.last_accessed.timestamp() == first

    second = first + 100
    monkeypatch.setattr(memory_store_module.time, "time", lambda: second)
    (search_entry,) = store.search("needle", layers=[MemoryLayer.SEMANTIC])
    assert search_entry.last_accessed.timestamp() == second


def test_forgetting_uses_last_accessed_and_never_touches_episodic(tmp_path):
    import sqlite3
    from datetime import UTC, datetime, timedelta

    from stiracore.control.memory_maintenance import MemoryMaintenance

    store = MemoryStore(tmp_path, "stira")
    store.write(MemoryLayer.SEMANTIC, "old-semantic", "S" * 3_000)
    store.write(MemoryLayer.EPISODIC, "raw-episode", "raw and immutable")
    now = datetime(2026, 9, 30, tzinfo=UTC)
    last_accessed = (now - timedelta(days=100)).timestamp()
    with sqlite3.connect(store.index_path) as connection:
        connection.execute(
            "UPDATE memory_entries SET last_accessed = ? WHERE layer = ? AND key = ?",
            (last_accessed, MemoryLayer.SEMANTIC.value, "old-semantic"),
        )

    maintenance = MemoryMaintenance(store)
    preview = maintenance.run_forgetting(now=now)
    assert preview.candidates[0].stage == "compact"
    assert preview.applied == 0

    applied = maintenance.run_forgetting(now=now, apply=True)
    assert applied.applied == 1
    semantic = next(
        entry
        for entry in store.list_entries(MemoryLayer.SEMANTIC, limit=10)
        if entry.key == "old-semantic"
    )
    assert semantic.metadata["forgetting_stage"] == "compact"
    assert len(semantic.content) <= 2_000
    episode = store.read(MemoryLayer.EPISODIC, "raw-episode")
    assert episode is not None
    assert episode.content == "raw and immutable"
    assert "forgetting_stage" not in episode.metadata


def test_semantic_duplicate_phase_returns_candidates_without_deleting(tmp_path):
    from stiracore.control.memory_maintenance import MemoryMaintenance

    store = MemoryStore(tmp_path, "stira")
    store.write(MemoryLayer.SEMANTIC, "one", "same semantic fact")
    store.write(MemoryLayer.SEMANTIC, "two", "same semantic fact")

    candidates = MemoryMaintenance(store).find_deletion_candidates()

    assert len(candidates) == 1
    assert candidates[0].reason == "duplicate semantic content"
    assert store.read(MemoryLayer.SEMANTIC, candidates[0].key) is not None


def test_amygdala_routes_structured_japanese_memory_to_semantic():
    scorer = AmygdalaPass()
    content = (
        "# 設計判断\n\n"
        "- 未解決の問題と実装方針を確認した。\n" + "この観測結果は今後の改善で再利用する。" * 30
    )

    score = scorer.score(content, {})

    assert score >= scorer.review_threshold
    assert scorer.route(score) == "staging"


def test_harvest_reprocesses_entry_from_unknown_rule_version(tmp_path):
    store = MemoryStore(tmp_path, "stira")
    store.write(
        MemoryLayer.EPISODIC,
        "legacy-discard",
        "Durable decision",
        {"importance": 0.7, "harvested": True, "harvest_route": "discard"},
    )

    result = HarvestScheduler(store, AmygdalaPass(), "stira").run_distillation()

    assert result.semantic == 1
    source = store.read(MemoryLayer.EPISODIC, "legacy-discard")
    assert source is not None
    assert source.metadata["harvest_version"] == RuleBasedDistiller.version


def test_harvest_preserves_sources_with_colliding_slugs(tmp_path):
    store = MemoryStore(tmp_path, "stira")
    sources = {
        "a.b": "First distinct fact",
        "a-b": "Second distinct fact",
        "x" * 65: "Third distinct fact",
        "x" * 66: "Fourth distinct fact",
    }
    for key, content in sources.items():
        store.write(MemoryLayer.EPISODIC, key, content, {"importance": 0.7})
        record_origin(
            store, MemoryLayer.EPISODIC, key, ORIGIN_EXTRACTED, "web-chat"
        )  # server-written (#506)
    scheduler = HarvestScheduler(store, AmygdalaPass(), "stira")

    result = scheduler.run_distillation()

    assert result.semantic == len(sources)
    promoted = [e for e in store.list_entries(MemoryLayer.SEMANTIC) if e.key != "index"]
    assert {e.metadata["source_id"]: e.content for e in promoted} == sources
    expected_keys = {
        source_id: (
            f"staging/harvest-{HarvestScheduler._slug(source_id, 'memory')[:24]}-"
            f"{hashlib.sha256(source_id.encode('utf-8')).hexdigest()[:16]}"
        )
        for source_id in sources
    }
    assert {e.metadata["source_id"]: e.key for e in promoted} == expected_keys
    assert all(len(e.key.rsplit("/", 1)[-1]) <= 49 for e in promoted)
    assert scheduler.run_distillation().skipped == len(sources)
