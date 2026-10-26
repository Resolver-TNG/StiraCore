from __future__ import annotations

from stiracore.agent.context_assembler import ContextAssembler
from stiracore.control.harvest import HarvestScheduler
from stiracore.control.staging_review import StagingReview, load_rejections
from stiracore.core.amygdala import AmygdalaPass
from stiracore.tools.memory_store import MemoryLayer, MemoryStore

CONTENT = "Critical decision about StagingReview design"


class _Engine:
    state = {"ok": True}  # noqa: RUF012
    last_breaker = None

    def identity_injection(self) -> str:
        return ""


def _setup(tmp_path):
    store = MemoryStore(tmp_path, "agent")
    store.write(MemoryLayer.EPISODIC, "ep1", CONTENT, {"importance": 0.9})
    harvest = HarvestScheduler(store, AmygdalaPass(), "agent")
    harvest.run_distillation()
    return store, harvest, StagingReview(store)


def _visible_semantic(store):
    return [
        e
        for e in store.list_entries(MemoryLayer.SEMANTIC, limit=1000)
        if not e.key.startswith("staging/") and e.key != "index"
    ]


def _context(store):
    assembler = ContextAssembler("agent", store, _Engine(), episodic_limit=0)
    return assembler.build_system_prompt()


def test_staged_not_in_context(tmp_path):
    store, _, review = _setup(tmp_path)
    staged = review.list_staged()
    assert len(staged) == 1
    assert staged[0].key.startswith("staging/")
    assert CONTENT not in _context(store)
    assert _visible_semantic(store) == []
    index = store.read(MemoryLayer.SEMANTIC, "index")
    assert index is None or "staging/" not in index.content


def test_approve_promotes(tmp_path):
    store, _, review = _setup(tmp_path)
    key = review.list_staged()[0].key
    promoted = review.approve(key)
    assert not promoted.key.startswith("staging/")
    assert review.list_staged() == []
    assert [e.content for e in _visible_semantic(store)] == [CONTENT]


def test_reject_removes_and_records(tmp_path):
    store, _, review = _setup(tmp_path)
    key = review.list_staged()[0].key
    review.reject(key, reason="noise")
    assert review.list_staged() == []
    assert _visible_semantic(store) == []
    ledger = load_rejections(store)
    assert len(ledger) == 1
    assert ledger[0]["source_id"] == "ep1"
    assert ledger[0]["reason"] == "noise"


def test_rejected_not_reproposed(tmp_path):
    store, harvest, review = _setup(tmp_path)
    review.reject(review.list_staged()[0].key)
    store.write(MemoryLayer.EPISODIC, "ep2", CONTENT, {"importance": 0.9})
    result = harvest.run_distillation()
    assert result.semantic == 0
    assert review.list_staged() == []
    episode = store.read(MemoryLayer.EPISODIC, "ep2")
    assert episode.metadata["harvest_route"] == "rejected"
