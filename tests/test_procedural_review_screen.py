"""#463: the review routes of the memory browser's procedural tab, on the real chat server app.

Offline model (``tests/test_new_session.make_stira``); proposals are staged through the
same service the agent's tool calls. Checked: confirmation, the shown hash, the turn lock,
no approver in the body, the approver = the OS account (like the CLI), the reason kept,
the memory tree no longer shows proposals as rules, and the next turn's prompt.
"""

from __future__ import annotations

import hashlib

import pytest

from stiracore.control import procedural_cli
from stiracore.tools.memory_store import MemoryLayer
from tests.test_new_session import make_stira

BODY = "Before editing, read the file; after editing, read it back."
SHA = hashlib.sha256(BODY.encode()).hexdigest()
APPROVE = "/api/memory/procedural/proposals/{}/approve"
REJECT = "/api/memory/procedural/proposals/{}/reject"


@pytest.fixture
def stira(tmp_path, monkeypatch):
    yield from make_stira(tmp_path, monkeypatch)


def _post(stira, url, body):
    return stira["client"].post(url, json=body, headers={"X-CSRF-Token": stira["token"]})


def _stage(stira, key="safe_edit", body=BODY, turn="turn-1"):
    proposals = stira["app"].extensions["procedure_proposals"]
    return proposals.propose(key, body, "seen twice", source_turn=turn)


def _store(stira):
    return stira["app"].extensions["procedure_proposals"].store


def test_review_lists_the_proposal_and_the_tree_does_not_show_it_as_a_rule(stira):
    _stage(stira)
    data = stira["client"].get("/api/memory/procedural/review").get_json()
    assert [p["key"] for p in data["proposals"]] == ["safe_edit"]
    proposal = data["proposals"][0]
    assert proposal["body"] == BODY and proposal["sha256"] == SHA
    assert proposal["source_turn"] == "turn-1" and proposal["rationale"] == "seen twice"
    assert data["effective"] == "next_turn" and data["prompt_limit"] == 5
    tree = stira["client"].get("/api/memory/tree").get_json()
    assert tree["procedural"] == []


@pytest.mark.parametrize(
    ("body", "status", "code"),
    [
        ({"sha256": SHA}, 428, "confirmation_required"),
        ({"sha256": SHA, "confirm": "yes"}, 428, "confirmation_required"),
        ({"sha256": "0" * 64, "confirm": True}, 409, "E_STALE_PROPOSAL"),
        # The request cannot name an approver (or anything else).
        ({"sha256": SHA, "confirm": True, "approved_by": "local:x"}, 400, "invalid_request"),
        ({"sha256": SHA, "confirm": True, "replace": "true"}, 400, "invalid_request"),
        ({"confirm": True}, 400, "invalid_request"),
    ],
)
def test_approve_refusals_write_nothing(stira, body, status, code):
    _stage(stira)
    response = _post(stira, APPROVE.format("safe_edit"), body)
    assert (response.status_code, response.get_json()["code"]) == (status, code)
    assert _store(stira).read(MemoryLayer.PROCEDURAL, "safe_edit") is None


def test_approve_is_refused_while_a_turn_holds_the_lock(stira):
    _stage(stira)
    lock = stira["app"].extensions["turn_lock"]
    assert lock.acquire(timeout=1)
    try:
        response = _post(stira, APPROVE.format("safe_edit"), {"sha256": SHA, "confirm": True})
    finally:
        lock.release()
    assert (response.status_code, response.get_json()["code"]) == (409, "turn_running")
    assert _store(stira).read(MemoryLayer.PROCEDURAL, "safe_edit") is None


def test_approve_writes_provenance_and_applies_from_the_next_turn(stira):
    stira["chat"]("first")
    _stage(stira)
    stira["chat"]("second")
    model = stira["model"]
    assert all(BODY not in (r["system_prompt"] or "") for r in model.requests)
    response = _post(stira, APPROVE.format("safe_edit"), {"sha256": SHA, "confirm": True})
    assert response.status_code == 200, response.get_json()
    data = response.get_json()
    assert data["approved_by"] == procedural_cli.operator_principal()
    assert data["effective"] == "next_turn" and data["audit"] == "ok"
    rule = _store(stira).read(MemoryLayer.PROCEDURAL, "safe_edit")
    assert rule.content == BODY
    assert rule.metadata["source_turn"] == "turn-1"
    assert rule.metadata["approved_by"] == procedural_cli.operator_principal()
    assert rule.metadata["approved_at"] and rule.metadata["review"] == "approved"
    count = len(model.requests)
    stira["chat"]("third")
    assert BODY in (model.requests[count]["system_prompt"] or "")
    # The proposal is gone from the review list and the rule is in the tree.
    review = stira["client"].get("/api/memory/procedural/review").get_json()
    assert review["proposals"] == []
    assert review["reviews"][0]["action"] == "approved"
    assert [n["id"] for n in stira["client"].get("/api/memory/tree").get_json()["procedural"]] == [
        "safe_edit"
    ]


def test_reject_needs_confirmation_keeps_the_reason_and_writes_no_rule(stira):
    _stage(stira)
    response = _post(stira, REJECT.format("safe_edit"), {"sha256": SHA, "reason": "vague"})
    assert response.status_code == 428
    response = _post(
        stira, REJECT.format("safe_edit"), {"sha256": SHA, "confirm": True, "reason": "vague"}
    )
    assert response.status_code == 200, response.get_json()
    assert "vague" not in response.get_data(as_text=True)  # never echoed
    review = stira["client"].get("/api/memory/procedural/review").get_json()
    assert review["proposals"] == []
    assert review["reviews"][0]["reason"] == "vague"
    assert review["reviews"][0]["by"] == procedural_cli.operator_principal()
    store = _store(stira)
    assert store.read(MemoryLayer.PROCEDURAL, "safe_edit") is None
    assert store.read(MemoryLayer.PROCEDURAL, "staging/safe_edit") is None


def test_unknown_and_invalid_keys(stira):
    response = _post(stira, APPROVE.format("nothing"), {"sha256": SHA, "confirm": True})
    assert (response.status_code, response.get_json()["code"]) == (404, "E_NO_PROPOSAL")
    response = _post(stira, APPROVE.format("Bad.Key"), {"sha256": SHA, "confirm": True})
    assert (response.status_code, response.get_json()["code"]) == (400, "E_INVALID_KEY")


def test_a_request_sent_from_inside_a_turn_cannot_approve(stira):
    """The agent's tools run only inside a turn: an approve sent while the model call runs
    (as an agent tool or a program it started would) is refused, nothing written."""
    _stage(stira)
    seen = {}

    def during():
        response = _post(stira, APPROVE.format("safe_edit"), {"sha256": SHA, "confirm": True})
        seen["approve"] = (response.status_code, response.get_json()["code"])

    stira["model"].during = during
    stira["chat"]("hello")
    stira["model"].during = None
    assert seen["approve"] == (409, "turn_running")
    assert _store(stira).read(MemoryLayer.PROCEDURAL, "safe_edit") is None
