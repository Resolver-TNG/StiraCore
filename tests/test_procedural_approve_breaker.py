"""#463 (operator decision 2026-10-20): approving a procedural proposal needs a NORMAL breaker.

Approving is a loosening (the rule enters every system prompt), so — like the other
loosenings (tool rules, scheduler resume, YOLO resume) — it is refused at any other breaker
level, on the service (so the review panel and the immediate-reflection path both) and on
the real chat server app. Reject only narrows and works at every level. A refusal writes
nothing, refreshes no prompt and leaves one ``audit.procedural.refused`` row with the key and
the code — never the body.
"""

from __future__ import annotations

import hashlib
import sqlite3
from types import SimpleNamespace

import pytest

from stiracore.control.procedural_proposals import (
    REVIEW_LOG_NAME,
    ProcedureProposals,
    ProposalRefused,
)
from stiracore.core.engine import BreakLevel
from stiracore.observability.audit_chain import AuditChain
from stiracore.observability.event_store import SQLiteEventStore
from stiracore.tools.memory_store import MemoryLayer, MemoryStore
from tests.test_new_session import make_stira

BODY = "BREAKER-BODY-MARKER-463: read the file before editing it."
SHA = hashlib.sha256(BODY.encode()).hexdigest()
APPROVE = "/api/memory/procedural/proposals/{}/approve"
REJECT = "/api/memory/procedural/proposals/{}/reject"
NOT_NORMAL = [BreakLevel.WARNING, BreakLevel.CRITICAL, BreakLevel.COHERENCE]


@pytest.fixture
def env(tmp_path):
    store = MemoryStore(tmp_path / "memory", "stira")
    events = SQLiteEventStore(tmp_path / "events.db")
    level = SimpleNamespace(value=BreakLevel.NORMAL)
    reflected = []
    proposals = ProcedureProposals(
        store,
        review_log=tmp_path / REVIEW_LOG_NAME,
        audit=AuditChain(events).record_procedural_write,
        reflect=lambda: reflected.append(1),
        breaker_level=lambda: level.value,
    )
    proposals.propose("safe_edit", BODY, "RATIONALE-MARKER", source_turn="turn-b1")
    yield SimpleNamespace(
        store=store,
        events=events,
        proposals=proposals,
        level=level,
        reflected=reflected,
        tmp=tmp_path,
    )
    events.close()


def _procedural_rows(env):
    return [e for e in env.events.get_events() if e.event_type.startswith("audit.procedural.")]


@pytest.mark.parametrize("level", list(BreakLevel), ids=lambda level: level.name)
def test_only_normal_approves(env, level):
    env.level.value = level
    if level is BreakLevel.NORMAL:
        result = env.proposals.approve("safe_edit", sha256=SHA)
        assert result["written"] is True
        assert env.store.read(MemoryLayer.PROCEDURAL, "safe_edit").content == BODY
        assert env.reflected == [1]
        return
    with pytest.raises(ProposalRefused) as refused:
        env.proposals.approve("safe_edit", sha256=SHA)
    assert refused.value.code == "E_BREAKER_NOT_NORMAL"
    # Nothing written, the proposal still waits, the prompt was not refreshed.
    assert env.store.read(MemoryLayer.PROCEDURAL, "safe_edit") is None
    assert env.store.read(MemoryLayer.PROCEDURAL, "staging/safe_edit") is not None
    assert env.reflected == []
    assert env.proposals.overview()["breaker"] == {
        "level": level.value,
        "approve_allowed": False,
    }


@pytest.mark.parametrize("level", ["normal", "NORMAL", " Normal "])
def test_a_level_name_works_like_the_enum(env, level):
    env.level.value = level
    assert env.proposals.approve("safe_edit", sha256=SHA)["written"] is True


@pytest.mark.parametrize("level", list(BreakLevel), ids=lambda level: level.name)
def test_reject_works_at_every_level(env, level):
    env.level.value = level
    result = env.proposals.reject("safe_edit", sha256=SHA, reason="not now")
    assert result["rejected"] == "safe_edit"
    assert env.store.read(MemoryLayer.PROCEDURAL, "staging/safe_edit") is None
    assert env.store.read(MemoryLayer.PROCEDURAL, "safe_edit") is None


def test_an_unreadable_level_fails_closed(env):
    def broken():
        raise RuntimeError("engine gone")

    for source in (broken, lambda: None, lambda: 3, lambda: ""):
        env.proposals.breaker_level = source
        with pytest.raises(ProposalRefused) as refused:
            env.proposals.approve("safe_edit", sha256=SHA)
        assert refused.value.code == "E_BREAKER_UNAVAILABLE"
        assert env.proposals.overview()["breaker"]["approve_allowed"] is False
    assert env.store.read(MemoryLayer.PROCEDURAL, "safe_edit") is None


def test_the_refusal_is_audited_with_the_code_and_never_the_body(env):
    env.level.value = BreakLevel.WARNING
    with pytest.raises(ProposalRefused):
        env.proposals.approve("safe_edit", sha256=SHA)
    rows = _procedural_rows(env)
    assert [r.event_type.rsplit(".", 1)[1] for r in rows] == ["proposed", "refused"]
    refused = rows[1].attributes
    assert refused["code"] == "E_BREAKER_NOT_NORMAL"
    assert refused["entry_key"] == "safe_edit"
    assert refused.get("content_hash") is None and refused.get("approved_by") is None
    raw = b"".join(
        bytes(str(row), "utf-8")
        for row in sqlite3.connect(env.tmp / "events.db").execute("SELECT * FROM events")
    )
    for text in ("BREAKER-BODY-MARKER-463", "RATIONALE-MARKER"):
        assert text.encode() not in raw, text
    # The refusal is not a review: the review log has no entry.
    assert env.proposals.review_history() == []


def test_approving_after_the_breaker_returns_to_normal(env):
    env.level.value = BreakLevel.CRITICAL
    with pytest.raises(ProposalRefused):
        env.proposals.approve("safe_edit", sha256=SHA)
    env.level.value = BreakLevel.NORMAL
    assert env.proposals.approve("safe_edit", sha256=SHA)["written"] is True


# --- the real chat server app ------------------------------------------------------------------


@pytest.fixture
def stira(tmp_path, monkeypatch):
    yield from make_stira(tmp_path, monkeypatch)


def _post(stira, url, body):
    return stira["client"].post(url, json=body, headers={"X-CSRF-Token": stira["token"]})


def _service(stira):
    return stira["app"].extensions["procedure_proposals"]


def _stage(stira):
    _service(stira).propose("safe_edit", BODY, "seen twice", source_turn="turn-1")


@pytest.mark.parametrize("level", NOT_NORMAL, ids=lambda level: level.name)
def test_the_panel_refuses_to_approve_unless_normal_and_rejects_at_any_level(stira, level):
    _stage(stira)
    _service(stira).breaker_level = lambda: level
    review = stira["client"].get("/api/memory/procedural/review").get_json()
    assert review["breaker"] == {"level": level.value, "approve_allowed": False}
    response = _post(stira, APPROVE.format("safe_edit"), {"sha256": SHA, "confirm": True})
    assert response.status_code == 409
    assert response.get_json()["code"] == "E_BREAKER_NOT_NORMAL"
    assert BODY not in response.get_data(as_text=True)
    assert _service(stira).store.read(MemoryLayer.PROCEDURAL, "safe_edit") is None
    response = _post(stira, REJECT.format("safe_edit"), {"sha256": SHA, "confirm": True})
    assert response.status_code == 200, response.get_json()
    assert _service(stira).store.read(MemoryLayer.PROCEDURAL, "staging/safe_edit") is None


def test_the_server_reads_the_live_engine_level(stira):
    """Wired to the engine (made stricter by the live stability, as YOLO samples it):
    a stability below the critical threshold refuses the approval and the next turn's
    prompt has no rule. The engine's hysteresis holds: with the stability restored, the
    turn that saw the drop leaves the breaker at CRITICAL (recovery needs protected
    turns), and the approval stays refused until the engine reports NORMAL."""
    _stage(stira)
    review = stira["client"].get("/api/memory/procedural/review").get_json()
    assert review["breaker"] == {"level": "normal", "approve_allowed": True}
    engine = stira["app"].extensions["new_session"]._agent.engine
    stability = engine.state.stability
    engine.state.stability = engine.config.stability_critical / 2
    try:
        review = stira["client"].get("/api/memory/procedural/review").get_json()
        assert review["breaker"]["approve_allowed"] is False
        response = _post(stira, APPROVE.format("safe_edit"), {"sha256": SHA, "confirm": True})
        assert (response.status_code, response.get_json()["code"]) == (
            409,
            "E_BREAKER_NOT_NORMAL",
        )
    finally:
        engine.state.stability = stability
    count = len(stira["model"].requests)
    stira["chat"]("hello")
    assert BODY not in (stira["model"].requests[count]["system_prompt"] or "")
    assert engine.breaker_status().level is not BreakLevel.NORMAL
    response = _post(stira, APPROVE.format("safe_edit"), {"sha256": SHA, "confirm": True})
    assert (response.status_code, response.get_json()["code"]) == (409, "E_BREAKER_NOT_NORMAL")
    assert _service(stira).store.read(MemoryLayer.PROCEDURAL, "safe_edit") is None


def test_the_panel_never_approves_without_a_wired_breaker(stira):
    _stage(stira)
    _service(stira).breaker_level = None
    response = _post(stira, APPROVE.format("safe_edit"), {"sha256": SHA, "confirm": True})
    assert (response.status_code, response.get_json()["code"]) == (503, "E_BREAKER_UNAVAILABLE")
    assert _service(stira).store.read(MemoryLayer.PROCEDURAL, "safe_edit") is None
