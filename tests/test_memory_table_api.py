"""#652: the read-only table behind the Memory page (key, importance, retention; no body)."""

from __future__ import annotations

import os

import pytest

flask = pytest.importorskip("flask")

from stiracore.memory_importance import bind_retention
from stiracore.memory_origin import ORIGIN_EXTRACTED, record_origin
from stiracore.observability.memory_api import (
    ENTRIES_ROUTE,
    KEY_MAX_CHARS,
    create_memory_table_blueprint,
)
from stiracore.tools.memory_store import MemoryLayer, MemoryStore

BODY = "BODY-TEXT-must-never-leave-the-store"
ROW_KEYS = {"key", "importance", "importance_state", "retention"}
RETENTION_KEYS = {"kind", "half_life_days", "state"}


@pytest.fixture
def store(tmp_path):
    return MemoryStore(tmp_path / "memory", "stira")


@pytest.fixture
def client(store):
    app = flask.Flask(__name__)
    app.register_blueprint(create_memory_table_blueprint(store))
    return app.test_client()


def _rows(client, layer, **query):
    response = client.get(ENTRIES_ROUTE, query_string={"layer": layer, **query})
    assert response.status_code == 200, response.get_data(as_text=True)
    assert BODY not in response.get_data(as_text=True)
    data = response.get_json()
    for row in data["entries"]:
        assert set(row) == ROW_KEYS
        assert set(row["retention"]) == RETENTION_KEYS
    return data


def _by_key(data):
    return {row["key"]: row for row in data["entries"]}


def test_rows_are_an_allow_list_without_body_or_front_matter(store, client):
    store.write(
        MemoryLayer.SEMANTIC,
        "knowledge/rule",
        BODY,
        {"importance": 0.83, "note": "FREE-TEXT-note", "source": "FREE-TEXT-source"},
    )
    data = _rows(client, "semantic")
    assert data["layer"] == "semantic" and data["total"] == 1 and data["limit"] == 50
    assert data["entries"] == [
        {
            "key": "knowledge/rule",
            "importance": 0.83,
            "importance_state": "recorded",
            "retention": {"kind": "derived", "half_life_days": 30, "state": "default"},
        }
    ]
    assert "FREE-TEXT" not in client.get(ENTRIES_ROUTE + "?layer=semantic").get_data(as_text=True)


def test_semantic_retention_tags(store, client):
    store.write(MemoryLayer.SEMANTIC, "kept", BODY, {"retention": "invariant"})
    store.write(
        MemoryLayer.SEMANTIC, "fades", BODY, {"retention": "derived", "retention_half_life_days": 7}
    )
    store.write(MemoryLayer.SEMANTIC, "bad", BODY, {"retention": "forever", "importance": "high"})
    rows = _by_key(_rows(client, "semantic"))
    assert rows["kept"]["retention"] == {
        "kind": "invariant",
        "half_life_days": None,
        "state": "tagged",
    }
    assert rows["fades"]["retention"] == {"kind": "derived", "half_life_days": 7, "state": "tagged"}
    assert rows["bad"]["retention"] == {"kind": "derived", "half_life_days": 30, "state": "invalid"}
    assert (rows["bad"]["importance"], rows["bad"]["importance_state"]) == (None, "none")


def test_episodic_front_matter_claims_are_not_shown_as_values(store, client):
    # The Episodic layer is agent-writable: a claim is reported as unverified (#506, #173).
    store.write(
        MemoryLayer.EPISODIC, "claimed", BODY, {"importance": 0.99, "retention": "invariant"}
    )
    store.write(MemoryLayer.EPISODIC, "plain", BODY)
    rows = _by_key(_rows(client, "episodic"))
    assert rows["claimed"]["importance"] is None
    assert rows["claimed"]["importance_state"] == "unverified"
    assert rows["claimed"]["retention"] == {
        "kind": "derived",
        "half_life_days": None,
        "state": "unverified",
    }
    assert rows["plain"]["importance_state"] == "none"
    assert rows["plain"]["retention"]["state"] == "default"


def test_episodic_verified_importance_and_human_tag(store, client):
    store.write(MemoryLayer.EPISODIC, "turn", BODY, {"importance": 0.456})
    record_origin(store, MemoryLayer.EPISODIC, "turn", ORIGIN_EXTRACTED, "chat")
    text = store.read_text(MemoryLayer.EPISODIC, "turn")
    bind_retention(store, MemoryLayer.EPISODIC, "turn", "invariant", text)
    row = _by_key(_rows(client, "episodic"))["turn"]
    assert (row["importance"], row["importance_state"]) == (0.46, "verified")
    assert row["retention"] == {"kind": "invariant", "half_life_days": None, "state": "verified"}
    # Edited since: the score no longer counts, the human's tag stays in force (changed).
    store.write(MemoryLayer.EPISODIC, "turn", BODY + " edited", {"importance": 1.0})
    row = _by_key(_rows(client, "episodic"))["turn"]
    assert (row["importance"], row["importance_state"]) == (None, "unverified")
    assert row["retention"]["kind"] == "invariant" and row["retention"]["state"] == "changed"


def test_staged_procedural_proposals_are_not_rows(store, client):
    store.write(MemoryLayer.PROCEDURAL, "rule", BODY, {"retention_half_life_days": 7})
    store.write(MemoryLayer.PROCEDURAL, "staging/proposal", BODY)
    data = _rows(client, "procedural")
    assert [row["key"] for row in data["entries"]] == ["rule"] and data["total"] == 1
    # Half-lives apply to the Semantic layer only.
    assert data["entries"][0]["retention"]["half_life_days"] is None


def test_key_is_masked_and_cut(store, client):
    secret = "sk-test-" + "a1B2c3D4e5F6g7H8" * 2
    store.write(MemoryLayer.EPISODIC, f"token={secret}", BODY)
    store.write(MemoryLayer.EPISODIC, "k" * 240, BODY)
    response = client.get(ENTRIES_ROUTE + "?layer=episodic")
    assert secret not in response.get_data(as_text=True)
    assert len(response.get_json()["entries"]) == 2
    assert max(len(row["key"]) for row in response.get_json()["entries"]) <= KEY_MAX_CHARS


def test_limit_and_total(store, client):
    for index in range(3):
        store.write(MemoryLayer.EPISODIC, f"e{index}", BODY)
        path = store.memory_dir / "episodic" / f"e{index}.md"
        os.utime(path, (1_700_000_000 + index, 1_700_000_000 + index))
    data = _rows(client, "episodic", limit="2")
    assert [row["key"] for row in data["entries"]] == ["e2", "e1"]
    assert (data["total"], data["limit"]) == (3, 2)


@pytest.mark.parametrize(
    ("query", "code"),
    [
        ("layer=<script>", "invalid_layer"),
        ("layer=episodic&limit=0", "invalid_limit"),
        ("layer=episodic&limit=101", "invalid_limit"),
        ("layer=episodic&limit=-1", "invalid_limit"),
        ("layer=episodic&limit=x", "invalid_limit"),
        ("layer=episodic&limit=000000001000000", "invalid_limit"),
    ],
)
def test_refusals_are_fixed_codes(client, query, code):
    response = client.get(f"{ENTRIES_ROUTE}?{query}")
    assert response.status_code == 400
    assert response.get_json() == {"error": code, "code": code}
    assert "script" not in response.get_data(as_text=True)


def test_route_is_get_only_and_listing_does_not_touch_access(store, client):
    assert client.post(ENTRIES_ROUTE).status_code == 405
    store.write(MemoryLayer.SEMANTIC, "note", BODY)
    before = store.list_entries(MemoryLayer.SEMANTIC)[0].last_accessed
    _rows(client, "semantic")
    assert store.list_entries(MemoryLayer.SEMANTIC)[0].last_accessed == before
